from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Never

import pytest
import Quartz
from Foundation import NSURL

import macos_harness.macos as macos_module
from macos_harness.capture import WindowCapture
from macos_harness.errors import ErrorCode
from macos_harness.macos import (
    _KEYCODES,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOS,
    MacOSError,
    SearchMatches,
    _split_scroll_delta,
)
from macos_harness.ops import Operations
from macos_harness.receipts import Acted, Postcondition, equals, gone, present


def _on_screen_windows(monkeypatch, *windows: tuple[int | float, ...]) -> None:
    """Fake the window server's front-to-back on-screen list.

    Each entry is ``(pid, window_id, x, y, width, height)``, frontmost
    first, on the normal window layer unless a seventh item names one.
    """
    described = [
        {
            "kCGWindowOwnerPID": pid,
            "kCGWindowNumber": window_id,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": width, "Height": height},
            "kCGWindowIsOnscreen": True,
            "kCGWindowLayer": layer[0] if layer else 0,
        }
        for pid, window_id, x, y, width, height, *layer in windows
    ]
    monkeypatch.setattr(
        macos_module.AS, "CGWindowListCopyWindowInfo", lambda options, relative: described
    )


def _found(*matches: dict[str, object], complete: bool = True) -> SearchMatches:
    """A search answer for a fake ``ax_search``: ``matches`` from a search
    that saw every candidate unless ``complete`` says it was cut short."""
    return SearchMatches(matches, complete=complete, visited=len(matches))


def test_render_tree() -> None:
    text = MacOS._render_tree(
        [
            {"element_index": 0, "depth": 0, "role": "AXApplication", "title": "Notes"},
            {
                "element_index": 1,
                "depth": 1,
                "role": "AXButton",
                "title": "Save",
                "url": "https://example.com/save",
                "actions": ["AXPress"],
            },
        ]
    )
    assert text.splitlines() == [
        '0 AXApplication title="Notes"',
        '  1 AXButton title="Save" actions=AXPress',
    ]


def test_split_scroll_delta_preserves_exact_total() -> None:
    assert _split_scroll_delta(-235, 100) == [-100, -100, -35]
    assert _split_scroll_delta(21, 10) == [10, 10, 1]
    assert _split_scroll_delta(0, 10) == [0]


def test_common_navigation_keys_are_supported() -> None:
    assert {_KEYCODES[name] for name in ("home", "end", "pageup", "pagedown")} == {
        115,
        116,
        119,
        121,
    }


class _FakeDate:
    """The one NSDate method `_launched_seconds_ago` reads."""

    def __init__(self, seconds_ago: float) -> None:
        self._seconds_ago = seconds_ago

    def timeIntervalSinceNow(self) -> float:
        return -self._seconds_ago


class _FakeRunningApp:
    """Minimal stand-in for NSRunningApplication: only what `_app_info`,
    `_resolve_app`'s exact-pid fast path, and match ranking ever touch."""

    def __init__(
        self,
        pid: int,
        *,
        name: str = "HelperApp",
        terminated: bool = False,
        launched_seconds_ago: float | None = None,
    ) -> None:
        self._pid = pid
        self._name = name
        self._terminated = terminated
        self._launched_seconds_ago = launched_seconds_ago

    def localizedName(self) -> str:
        return self._name

    def bundleIdentifier(self) -> None:
        return None

    def bundleURL(self) -> None:
        return None

    def processIdentifier(self) -> int:
        return self._pid

    def isTerminated(self) -> bool:
        return self._terminated

    def launchDate(self) -> _FakeDate | None:
        if self._launched_seconds_ago is None:
            return None
        return _FakeDate(self._launched_seconds_ago)


def test_resolve_app_exact_pid_hit_never_enumerates_workspace(monkeypatch) -> None:
    """An int pid resolves directly by identity -- it must never scan
    NSWorkspace.runningApplications(), which is not just wasteful but can
    return stale, notification-cache-backed data in a process that never
    pumps its own run loop."""
    mac = MacOS()
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            assert pid == 4242
            return fake_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    app, info = mac._resolve_app(4242)

    assert app is fake_app
    assert info["pid"] == 4242
    assert info["name"] == "HelperApp"


def test_resolve_app_exact_pid_miss_raises_without_enumerating_workspace(
    monkeypatch,
) -> None:
    mac = MacOS()

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            return None

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    with pytest.raises(ApplicationNotFoundError, match="99999"):
        mac._resolve_app(99999)


def test_resolve_app_exact_pid_rejects_a_terminated_process_without_enumerating(
    monkeypatch,
) -> None:
    mac = MacOS()
    terminated_app = _FakeRunningApp(4242, terminated=True)

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            return terminated_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError(
                "must not enumerate NSWorkspace.runningApplications() for an int pid"
            )

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    with pytest.raises(ApplicationNotFoundError, match="4242"):
        mac._resolve_app(4242)


def test_resolve_app_string_query_still_enumerates_workspace(monkeypatch) -> None:
    """A name/bundle-id/path/stringified-pid query is unchanged: it still
    scans NSWorkspace.runningApplications(), never the exact-pid fast
    path."""
    mac = MacOS()
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> type[_FakeWorkspace]:
            return _FakeWorkspace

        @staticmethod
        def runningApplications() -> list[_FakeRunningApp]:
            return [fake_app]

    def _boom(pid: int) -> Never:
        raise AssertionError("string queries must never use the exact-pid fast path")

    class _FakeRunningApplication:
        runningApplicationWithProcessIdentifier_ = staticmethod(_boom)

    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)
    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)

    app, info = mac._resolve_app("HelperApp")

    assert app is fake_app
    assert info["pid"] == 4242


def test_resolve_app_last_app_reuse_passes_an_int_pid(monkeypatch) -> None:
    """Reusing `_last_app` (a falsy query with a prior resolution cached)
    must feed the exact-pid fast path an int, not a stringified one -- it
    benefits from the same identity lookup as an explicit int query."""
    mac = MacOS()
    mac._last_app = {"name": "HelperApp", "bundle_id": None, "pid": 4242, "path": None}
    fake_app = _FakeRunningApp(4242, name="HelperApp")

    seen: list[int] = []

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            seen.append(pid)
            return fake_app

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError("must not enumerate for a cached last_app pid reuse")

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    app, info = mac._resolve_app(None)

    assert app is fake_app
    assert info["pid"] == 4242
    assert seen == [4242]
    assert isinstance(seen[0], int)


def test_doctor_reports_real_permission_preflights(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "is_accessibility_trusted", lambda: True)
    monkeypatch.setattr(
        mac,
        "_preflight_permission",
        lambda name: {
            "CGPreflightScreenCaptureAccess": True,
            "CGPreflightPostEventAccess": False,
        }[name],
    )
    monkeypatch.setattr(mac, "list_apps", lambda: [{"name": "Test"}])

    result = mac.doctor()

    assert result["permissions"] == {
        "accessibility": True,
        "screen_recording": True,
        "post_events": False,
        "automation": "per-target; requested by macOS on first Apple Event",
    }
    assert result["input_monitoring_required"] is False


def test_application_element_enables_enhanced_ax(monkeypatch) -> None:
    root = object()
    writes = []
    monkeypatch.setattr(
        macos_module.AS, "AXUIElementCreateApplication", lambda pid: root
    )
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyAttributeValue",
        lambda element, attribute, error: (0, False),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementSetAttributeValue",
        lambda element, attribute, value: writes.append((element, attribute, value)),
    )
    timeouts = []
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementSetMessagingTimeout",
        lambda element, timeout: timeouts.append((element, timeout)) or 0,
    )
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    assert MacOS._application_element(42, messaging_timeout=0.5) is root
    assert timeouts == [(root, 0.5)]
    assert writes == [(root, "AXEnhancedUserInterface", True)]

    writes.clear()
    assert MacOS._application_element(42, enhance=False) is root
    assert writes == []


class _Refused(NamedTuple):
    """The AXError an app answers a read with, in place of a value."""

    code: int


def _focus_ax(
    monkeypatch,
    mac: MacOS,
    data: dict[object, dict[str, object]],
    *,
    batch: bool,
    focused: object,
) -> list[str]:
    """Answer `_focus_sample`'s AX reads from ``data`` -- ``{element:
    {attribute: value}}``, a missing attribute being AX's own "no value"
    -- at the ApplicationServices boundary, so the real readers run. With
    ``batch`` an element's batch read answers every slot at once, error
    sentinels included; without it the batch call is unsupported and the
    readers fall back to single reads. Only ``focused`` may be read in a
    batch: Safari answers a batched root read with no element while
    single reads find it, so the sample must never batch anything else.
    Returns the attribute names requested, in order, appended to as the
    sample runs."""
    AS = macos_module.AS
    requested: list[str] = []

    def answer(element, name):
        value = data[element].get(name, _Refused(AS.kAXErrorNoValue))
        return (value.code, None) if isinstance(value, _Refused) else (0, value)

    def copy_attribute(element, name, _out):
        requested.append(name)
        return answer(element, name)

    def copy_attributes(element, names, options, _out):
        if not batch:
            return AS.kAXErrorNotImplemented, None
        assert element is focused, "only the focused element is read in a batch"
        requested.extend(names)
        values = []
        for name in names:
            error, value = answer(element, name)
            values.append(
                value if error == 0 else AS.AXValueCreate(AS.kAXValueAXErrorType, error)
            )
        return 0, values

    monkeypatch.setattr(AS, "AXUIElementCopyAttributeValue", copy_attribute)
    monkeypatch.setattr(AS, "AXUIElementCopyMultipleAttributeValues", copy_attributes)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Demo", "pid": 42})
    return requested


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "single-reads"])
def test_focus_sample_reads_the_focused_element_without_enhancing_ax(monkeypatch, batch) -> None:
    mac = MacOS()
    root, window, field = object(), object(), object()
    roots: list[dict[str, object]] = []
    data: dict[object, dict[str, object]] = {
        root: {"AXFocusedWindow": window, "AXFocusedUIElement": field},
        window: {"AXTitle": "Login"},
        field: {"AXRole": "AXTextField", "AXValue": "hello", "AXNumberOfCharacters": 5},
    }

    def fake_root(pid, **kwargs):
        roots.append({"pid": pid, **kwargs})
        return root

    monkeypatch.setattr(mac, "_application_element", fake_root)
    requested = _focus_ax(monkeypatch, mac, data, batch=batch, focused=field)

    # An ordinary field has no subrole to report, which is an absence,
    # not a refusal: the sample goes on to read its details.
    assert mac._focus_sample(42) == {
        "frontmost_pid": 42,
        "window": "Login",
        "focused": {"role": "AXTextField", "value": "hello", "characters": 5},
    }
    assert roots == [{"pid": 42, "enhance": False}]

    # A password field reports its identity only: its value, length and
    # selection are never even requested from the app.
    data[field] = {
        "AXRole": "AXTextField",
        "AXSubrole": "AXSecureTextField",
        "AXTitle": "Password",
        "AXValue": "hunter2",
        "AXNumberOfCharacters": 7,
    }
    requested.clear()
    assert mac._focus_sample(42)["focused"] == {
        "role": "AXTextField",
        "subrole": "AXSecureTextField",
    }
    assert not {"AXValue", "AXNumberOfCharacters", "AXSelectedTextRange"} & set(requested)

    # Nothing focused is an absence too.
    data[root] = {}
    assert mac._focus_sample(42) == {"frontmost_pid": 42, "window": None, "focused": None}


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "single-reads"])
@pytest.mark.parametrize(
    ("refused", "code"),
    [
        ("AXFocusedUIElement", -25204),  # kAXErrorCannotComplete: the app did not answer
        ("AXTitle", -25202),  # kAXErrorInvalidUIElement: the window is gone
        ("AXValue", -25211),  # kAXErrorAPIDisabled
    ],
)
def test_focus_sample_raises_on_a_refused_read_instead_of_reporting_an_absence(
    monkeypatch, batch, refused, code
) -> None:
    """A refused read is no observation at all -- the whole sample fails
    -- never a sample in which the field happens to be missing, which a
    receipt would count as focus having moved."""
    mac = MacOS()
    root, window, field = object(), object(), object()
    data: dict[object, dict[str, object]] = {
        root: {"AXFocusedWindow": window, "AXFocusedUIElement": field},
        window: {"AXTitle": "Login"},
        field: {"AXRole": "AXTextField", "AXValue": "hello", "AXNumberOfCharacters": 5},
    }
    for element in data.values():
        if refused in element:
            element[refused] = _Refused(code)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    _focus_ax(monkeypatch, mac, data, batch=batch, focused=field)

    with pytest.raises(MacOSError) as excinfo:
        mac._focus_sample(42)
    assert excinfo.value.code == ErrorCode.AX_ERROR
    assert excinfo.value.details["ax_error"] == code
    assert "hello" not in str(excinfo.value)


@pytest.mark.parametrize("batch", [True, False], ids=["batch", "single-reads"])
def test_focus_sample_requests_no_text_after_a_refused_secure_field_check(
    monkeypatch, batch
) -> None:
    """Only a subrole the app actually reported can clear a field for its
    value, selection and length to be read: a refused subrole read fails
    the sample before any of them is requested."""
    mac = MacOS()
    root, window, field = object(), object(), object()
    data: dict[object, dict[str, object]] = {
        root: {"AXFocusedWindow": window, "AXFocusedUIElement": field},
        window: {"AXTitle": "Login"},
        field: {
            "AXRole": "AXTextField",
            "AXSubrole": _Refused(macos_module.AS.kAXErrorCannotComplete),
            "AXValue": "hunter2",
            "AXNumberOfCharacters": 7,
        },
    }
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    requested = _focus_ax(monkeypatch, mac, data, batch=batch, focused=field)

    with pytest.raises(MacOSError):
        mac._focus_sample(42)
    assert "AXSubrole" in requested
    assert not {"AXValue", "AXNumberOfCharacters", "AXSelectedTextRange"} & set(requested)


def _bounded_tree(monkeypatch, mac: MacOS, root: object, children: dict, data: dict) -> None:
    """Serve a fake AX tree through the bounded-walk fallback: the app's
    own search predicate is unsupported, so every query walks ``children``
    from ``root`` and reads each element's attributes from ``data``."""
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    monkeypatch.setattr(mac, "_is_ax_element", lambda value: value in data)
    monkeypatch.setattr(
        mac,
        "_copy_attribute",
        lambda element, attribute: (
            children.get(element, [])
            if attribute in {"AXChildren", "AXWindows"}
            else None
        ),
    )

    def fake_attributes(element, attributes):
        values = {
            **data.get(element, {}),
            "AXChildren": children.get(element),
            "AXWindows": None,
        }
        return macos_module._AttributeValues(
            (attribute, values.get(attribute)) for attribute in attributes
        )

    monkeypatch.setattr(mac, "_copy_attributes", fake_attributes)
    monkeypatch.setattr(mac, "_actions", lambda element: [])
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyParameterizedAttributeValue",
        lambda *args: (
            macos_module.AS.kAXErrorParameterizedAttributeUnsupported,
            None,
        ),
    )


def test_ax_query_falls_back_to_a_bounded_tree(monkeypatch) -> None:
    mac = MacOS()
    root, button, group, match, too_deep = (object() for _ in range(5))
    children = {root: [button, group], group: [match, too_deep]}
    data = {
        button: {"AXRole": "AXButton", "AXTitle": "Save"},
        group: {"AXRole": "AXGroup"},
        match: {"AXRole": "AXStaticText", "AXValue": "Alessia playlist"},
        too_deep: {"AXRole": "AXStaticText", "AXValue": "Alessia too deep"},
    }
    _bounded_tree(monkeypatch, mac, root, children, data)

    results = mac.ax.query(
        app="Spotify",
        text="Alessia",
        limit=20,
        max_nodes=4,
        include_actions=False,
    )

    assert results == [
        {
            "element_index": 3,
            "role": "AXStaticText",
            "value": "Alessia playlist",
        }
    ]
    # max_nodes stopped the walk one node short, so the fifth element was
    # never examined and this list may not be every match.
    assert results.complete is False
    assert results.visited == 4


@pytest.mark.parametrize(
    "condition",
    (
        present(app="Demo", title="Save", timeout=1),
        equals(value=False, app="Demo", title="Save", timeout=1),
        gone(app="Demo", title="Absent", timeout=1),
    ),
)
def test_expect_never_enables_app_accessibility_features(monkeypatch, condition: Postcondition) -> None:
    mac = MacOS()
    root, save = object(), object()
    data = {
        root: {"AXRole": "AXApplication", "AXEnhancedUserInterface": False},
        save: {"AXRole": "AXButton", "AXTitle": "Save", "AXValue": False},
    }
    info = {"pid": 42, "name": "Demo", "bundle_id": None, "path": None}
    identity = macos_module._AppIdentity(42, None, 1000.0, "Demo", None)
    monkeypatch.setattr(mac, "_resolve_app", lambda app: (None, info))
    monkeypatch.setattr(mac, "_identity_from_info", lambda app_info: identity)
    monkeypatch.setattr(mac, "_process_identity", lambda app: identity)
    _bounded_tree(monkeypatch, mac, root, {root: [save]}, data)
    monkeypatch.setattr(mac, "_application_element", MacOS._application_element)
    monkeypatch.setattr(macos_module.AS, "AXUIElementCreateApplication", lambda pid: root)
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyAttributeValue",
        lambda element, attribute, out: (0, data[element].get(attribute)),
    )

    def set_attribute(element, attribute, value):
        data[element][attribute] = value
        return 0

    monkeypatch.setattr(macos_module.AS, "AXUIElementSetAttributeValue", set_attribute)
    clock = iter(range(100))

    def monotonic():
        return next(clock) * 0.01

    monkeypatch.setattr(macos_module.time, "monotonic", monotonic)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    mac.do = Operations(mac, _monotonic=monotonic)

    receipt = mac.do.expect(condition)

    assert data[root]["AXEnhancedUserInterface"] is False
    assert receipt.verified is True
    assert receipt.acted is Acted.NO
    assert receipt.changed is None


def test_unsupported_fallback_predicate_never_matches_unrelated_controls(monkeypatch) -> None:
    mac = MacOS()
    root, save = object(), object()
    data = {save: {"AXRole": "AXButton", "AXTitle": "Save"}}
    _bounded_tree(monkeypatch, mac, root, {root: [save]}, data)

    with pytest.raises(MacOSError, match="AXHeadingSearchKey") as caught:
        mac.ax.query(
            app="Pages", search_key="AXHeadingSearchKey", include_actions=False
        )

    assert caught.value.code == ErrorCode.UNSUPPORTED_OP


def test_exact_title_rejects_substring_and_case_twins(monkeypatch) -> None:
    """Substring ``text`` is how an agent finds things; an exact ``title``
    is how it proves it found the right one. "Save" must not resolve to
    "Save As…" or "save", and the walk must say it saw every node."""
    mac = MacOS()
    root, save, save_as, lower = (object() for _ in range(4))
    children = {root: [save_as, lower, save]}
    data = {
        save: {"AXRole": "AXButton", "AXTitle": "Save"},
        save_as: {"AXRole": "AXButton", "AXTitle": "Save As…"},
        lower: {"AXRole": "AXStaticText", "AXTitle": "save"},
    }
    _bounded_tree(monkeypatch, mac, root, children, data)

    loose = mac.ax.query(app="Pages", text="save", include_actions=False)
    exact = mac.ax.query(app="Pages", title="Save", include_actions=False)

    assert [match["title"] for match in loose] == ["Save As…", "save", "Save"]
    assert [match["title"] for match in exact] == ["Save"]
    assert exact.complete is True
    assert exact.visited == 4


def test_exact_identifier_twins_are_ambiguous_not_first_match(monkeypatch) -> None:
    """Two elements with the same identifier is exactly the situation an
    exact selector exists to expose; ``wait`` must refuse rather than
    act on whichever the walk reached first."""
    mac = MacOS()
    root, first, second = (object() for _ in range(3))
    children = {root: [first, second]}
    data = {
        first: {"AXRole": "AXButton", "AXIdentifier": "_NS:9", "AXTitle": "OK"},
        second: {"AXRole": "AXButton", "AXIdentifier": "_NS:9", "AXTitle": "Cancel"},
    }
    _bounded_tree(monkeypatch, mac, root, children, data)

    with pytest.raises(MacOSError, match="found 2 matches") as exc_info:
        mac.ax.wait(app="Pages", identifier="_NS:9", timeout=0)

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["count"] == 2


def test_strict_wait_never_trusts_one_match_from_a_cut_search(monkeypatch) -> None:
    """One match from a walk ``max_nodes`` stopped early may have a twin
    in the part the walk never reached. A substring wait keeps its
    first-match contract; an exact wait keeps polling and times out with
    the bound that stopped the search."""
    mac = MacOS()
    match = {"element_index": 4, "role": "AXButton", "title": "Save"}
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: _found(match, complete=False))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    assert mac.ax.wait(app="Pages", text="Save", timeout=0) == match

    with pytest.raises(MacOSError, match="before a complete search") as exc_info:
        mac.ax.wait(app="Pages", title="Save", timeout=0, max_nodes=300)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details == {
        "timeout": 0,
        "complete": False,
        "visited": 1,
        "max_nodes": 300,
    }


@pytest.mark.parametrize("failure", ["children", "batch"])
def test_strict_wait_rejects_a_branch_that_refused_its_children(monkeypatch, failure) -> None:
    mac = MacOS()
    root, save, blocked = (object() for _ in range(3))
    data = {
        root: {
            "AXRole": "AXApplication",
            "AXChildren": [save, blocked],
        },
        save: {
            "AXRole": "AXButton",
            "AXTitle": "Save",
        },
        blocked: {
            "AXRole": "AXGroup",
            "AXChildren": _Refused(macos_module.AS.kAXErrorCannotComplete) if failure == "children" else [],
        },
    }
    copy_attributes = mac._copy_attributes
    _bounded_tree(monkeypatch, mac, root, {root: [save, blocked]}, data)
    monkeypatch.setattr(mac, "_copy_attributes", copy_attributes)
    _focus_ax(monkeypatch, mac, data, batch=False, focused=root)
    if failure == "batch":
        monkeypatch.setattr(
            macos_module.AS, "AXUIElementCopyMultipleAttributeValues",
            lambda element, *args: (
                macos_module.AS.kAXErrorCannotComplete if element is blocked
                else macos_module.AS.kAXErrorNotImplemented, None
            ),
        )

    with pytest.raises(MacOSError, match="complete search") as caught:
        mac.ax.wait(app="Pages", title="Save", timeout=0)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.details["complete"] is False


def test_repeated_child_at_the_node_budget_is_still_complete(monkeypatch) -> None:
    mac = MacOS()
    root, save = object(), object()
    data = {save: {"AXRole": "AXButton", "AXTitle": "Save"}}
    _bounded_tree(monkeypatch, mac, root, {root: [save, save]}, data)

    matches = mac.ax.query(
        app="Pages", title="Save", max_nodes=2, include_actions=False
    )

    assert [match["title"] for match in matches] == ["Save"]
    assert matches.complete is True


def test_wait_gone_never_certifies_absence_from_a_cut_search(monkeypatch) -> None:
    """An empty result from a walk that stopped at ``max_nodes`` says
    nothing about the unseen part, so it must not count toward the two
    empty polls that confirm the match is gone."""
    mac = MacOS()
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: _found(complete=False))
    clock = iter(range(100))
    monkeypatch.setattr(macos_module.time, "monotonic", lambda: next(clock) * 0.01)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    with pytest.raises(MacOSError, match="two consecutive empty polls") as exc_info:
        mac.ax.wait_gone("Not Now", app="Chrome", timeout=0.025, max_nodes=300)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details == {
        "timeout": 0.025,
        "consecutive_empty_polls": 0,
        "complete": False,
        "visited": 0,
        "max_nodes": 300,
    }


@pytest.mark.parametrize("field", ["title", "identifier", "description"])
def test_exact_selectors_reject_empty_strings(monkeypatch, field: str) -> None:
    """An empty exact selector can never equal an attribute a search
    keeps, so it is a bad request, not a silent never-match -- decided
    before the app is even resolved."""
    mac = MacOS()
    monkeypatch.setattr(mac, "_pid", _fail_if_called)
    monkeypatch.setattr(mac, "_acquire_native", _fail_if_called)

    with pytest.raises(MacOSError, match=f"{field} must be a non-empty str") as exc_info:
        mac.ax.query(app="Pages", **{field: ""})

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == field


def test_exact_selector_alone_scopes_a_cross_app_search(monkeypatch) -> None:
    """A cross-app search needs something to look for; an exact selector
    is that something even without substring ``text``."""
    mac = MacOS()
    arguments = {}

    def fake_search_all(**kwargs):
        arguments.update(kwargs)
        return _found()

    monkeypatch.setattr(mac, "ax_search_all", fake_search_all)

    mac.ax.query_all(identifier="_NS:9", apps="Pages")

    assert arguments["text"] is None
    assert arguments["identifier"] == "_NS:9"


def test_safe_ax_fallback_does_not_read_values(monkeypatch) -> None:
    mac = MacOS()
    root, button = object(), object()
    requested = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_application_element", lambda pid, **kwargs: root)
    monkeypatch.setattr(mac, "_is_ax_element", lambda value: value is button)

    def fake_attributes(element, attributes):
        requested.append(tuple(attributes))
        values = {
            root: {"AXRole": "AXApplication", "AXChildren": [button]},
            button: {"AXRole": "AXButton", "AXTitle": "Use Password"},
        }[element]
        return macos_module._AttributeValues(
            (attribute, values.get(attribute)) for attribute in attributes
        )

    monkeypatch.setattr(mac, "_copy_attributes", fake_attributes)
    monkeypatch.setattr(mac, "_actions", lambda element: [])
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyParameterizedAttributeValue",
        lambda *args: (
            macos_module.AS.kAXErrorParameterizedAttributeUnsupported,
            None,
        ),
    )

    results = mac.ax.query(
        app="Chrome",
        text="Use Password",
        attributes=mac.ax._SAFE_ATTRIBUTES,
        include_actions=False,
    )

    assert results[0]["title"] == "Use Password"
    assert all("AXValue" not in attributes for attributes in requested)


def test_snapshot_tree_can_preserve_earlier_element_handles(monkeypatch) -> None:
    mac = MacOS()
    first, second = object(), object()
    monkeypatch.setattr(
        mac,
        "_copy_attributes",
        lambda element, attributes: macos_module._AttributeValues(
            (attribute, "AXApplication" if attribute == "AXRole" else None)
            for attribute in attributes
        ),
    )
    monkeypatch.setattr(mac, "_actions", lambda element: [])

    first_snapshot = mac._snapshot_tree(
        first,
        max_depth=1,
        max_nodes=2,
        include_menu_bar=False,
        attributes=("AXRole",),
    )
    second_snapshot = mac._snapshot_tree(
        second,
        max_depth=1,
        max_nodes=2,
        include_menu_bar=False,
        attributes=("AXRole",),
        reset_elements=False,
    )

    assert [node["element_index"] for node in first_snapshot.nodes] == [0]
    assert [node["element_index"] for node in second_snapshot.nodes] == [1]
    assert mac._element(0) is first
    assert mac._element(1) is second


def test_cleared_element_handles_never_alias() -> None:
    mac = MacOS()
    old_index = mac._remember_element(object())
    mac._elements = {}
    new_element = object()
    new_index = mac._remember_element(new_element)

    assert new_index > old_index
    assert mac._element(new_index) is new_element
    with pytest.raises(MacOSError, match="Unknown element index"):
        mac._element(old_index)


def test_ax_query_all_scans_every_app_without_mutating_targets(monkeypatch) -> None:
    mac = MacOS()
    apps = [
        {"name": "First", "bundle_id": "one", "pid": 1, "path": "/First"},
        {"name": "Blocked", "bundle_id": "two", "pid": 2, "path": "/Blocked"},
        {
            "name": "Messages",
            "bundle_id": "com.apple.MobileSMS",
            "pid": 99,
            "path": "/Messages",
        },
        {"name": "Third", "bundle_id": "three", "pid": 3, "path": "/Third"},
    ]
    elements = {1: object(), 99: object(), 3: object()}
    calls = []
    original_target = {"name": "Current", "bundle_id": "current", "pid": 7}
    mac._last_app = original_target
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "list_apps", lambda: apps)

    def fake_search(*, app_pid, limit, reset_elements, attributes, **kwargs):
        calls.append(
            (
                app_pid,
                limit,
                reset_elements,
                tuple(attributes),
                kwargs["messaging_timeout"],
                kwargs["enhance"],
            )
        )
        if app_pid == 2:
            raise MacOSError("inaccessible")
        index = mac._remember_element(elements[app_pid])
        return _found({"element_index": index, "role": "AXButton"})

    monkeypatch.setattr(mac, "ax_search", fake_search)

    results = mac.ax.query_all("Not Now", limit=2)

    assert [result["app"]["pid"] for result in results] == [1, 99]
    assert [call[:3] for call in calls] == [
        (1, 2, False),
        (2, 1, False),
        (99, 1, False),
    ]
    assert all("AXValue" not in call[3] for call in calls)
    assert all(call[4:] == (0.5, False) for call in calls)
    assert mac._element(0) is elements[1]
    assert mac._element(1) is elements[99]
    assert mac._last_app is original_target

    with pytest.raises(MacOSError, match="requires non-empty text"):
        mac.ax.query_all()
    with pytest.raises(MacOSError, match="at least one"):
        mac.ax.query_all("Not Now", apps=[])


@pytest.mark.parametrize("method", ["wait", "wait_gone"])
def test_cross_app_wait_cannot_certify_a_search_that_skipped_an_app(monkeypatch, method) -> None:
    mac = MacOS()
    apps = [{"name": "Readable", "pid": 1}, {"name": "Unreadable", "pid": 2}]
    answer = _found({"element_index": 7, "role": "AXButton", "title": "Save"}) if method == "wait" else _found()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "list_apps", lambda: apps)

    def search(*, app_pid, **kwargs):
        if app_pid == 2:
            raise MacOSError("Unreadable app", code=ErrorCode.AX_ERROR)
        return answer

    monkeypatch.setattr(mac, "ax_search", search)
    clock = iter(range(100))
    monkeypatch.setattr(macos_module.time, "monotonic", lambda: next(clock) * 0.1)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    with pytest.raises(MacOSError, match="timed out") as failure:
        getattr(mac.ax, method)(all_apps=True, title="Save", timeout=0.3)

    assert failure.value.code == ErrorCode.TIMEOUT
    assert failure.value.details["complete"] is False


def test_ax_role_aliases_and_app_selectors_fail_closed(monkeypatch) -> None:
    mac = MacOS()
    arguments = {}

    def fake_search_all(**kwargs):
        arguments.update(kwargs)
        return _found()

    monkeypatch.setattr(mac, "ax_search_all", fake_search_all)

    mac.ax.query_all("Not Now", role="text_field", apps="Safari")

    assert arguments["text"] == "Not Now"
    assert arguments["search_key"] == "AXTextFieldSearchKey"
    assert arguments["apps"] == "Safari"
    assert mac.ax._search_key(None, "any") == "AXAnyTypeSearchKey"

    with pytest.raises(MacOSError, match="Unknown AX role"):
        mac.ax.query_all("Not Now", role="buton")
    with pytest.raises(MacOSError, match="role or search_key"):
        mac.ax.query_all(
            "Not Now",
            role="button",
            search_key="AXAnyTypeSearchKey",
        )
    with pytest.raises(MacOSError, match="at least one"):
        mac._normalize_apps([])


def test_ax_app_resolution_deduplicates_pids(monkeypatch) -> None:
    mac = MacOS()
    info = {"name": "Safari", "bundle_id": "com.apple.Safari", "pid": 42}
    monkeypatch.setattr(mac, "_resolve_app", lambda selector: (object(), info))

    resolved = mac._resolve_apps(("Safari", "com.apple.Safari", "42"))

    assert resolved == [info]


def test_ax_wait_retries_until_one_match(monkeypatch) -> None:
    mac = MacOS()
    responses = iter([_found(), _found({"element_index": 7, "role": "AXButton"})])
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: next(responses))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    match = mac.ax.wait(app="Chrome", text="Use Password", timeout=1.0)

    assert match["element_index"] == 7


def test_ax_wait_fails_closed_on_ambiguity_and_timeout(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac,
        "ax_search_all",
        lambda **kwargs: _found(
            {"element_index": 1},
            {"element_index": 2},
        ),
    )
    with pytest.raises(MacOSError, match="found 2 matches"):
        mac.ax.wait(all_apps=True, text="Not Now")

    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: _found())
    with pytest.raises(MacOSError, match="timed out"):
        mac.ax.wait(app="Chrome", text="Missing", timeout=0)

    with pytest.raises(MacOSError, match="exactly one"):
        mac.ax.wait(app="Chrome", all_apps=True, text="Not Now")
    with pytest.raises(MacOSError, match="all_apps=True or apps"):
        mac.ax.wait(all_apps=True, apps=["Chrome"], text="Not Now")
    with pytest.raises(MacOSError, match="requires non-empty text"):
        mac.ax.wait(all_apps=True)


def test_ax_press_supports_one_line_cross_app_use(monkeypatch) -> None:
    mac = MacOS()
    match = {
        "element_index": 4,
        "role": "AXButton",
        "actions": ["AXPress"],
        "app": {"name": "Chrome", "pid": 42},
    }
    wait_arguments = {}
    actions = []

    def fake_wait(**kwargs):
        wait_arguments.update(kwargs)
        return match

    monkeypatch.setattr(mac, "ax_wait", fake_wait)
    monkeypatch.setattr(
        mac,
        "_frontmost_app",
        lambda: {"name": "Ghostty", "pid": 1},
    )
    monkeypatch.setattr(
        mac,
        "perform_action",
        lambda element_index, action: actions.append((element_index, action)),
    )

    assert mac.ax.press("Not Now", role="button", all_apps=True) is match
    assert wait_arguments["text"] == "Not Now"
    assert wait_arguments["search_key"] == "AXButtonSearchKey"
    assert wait_arguments["include_actions"] is True
    assert actions == [(4, "AXPress")]


def test_ax_press_reports_target_activation(monkeypatch) -> None:
    mac = MacOS()
    frontmost = iter(
        [
            {"name": "Ghostty", "pid": 1},
            {"name": "Chrome", "pid": 42},
        ]
    )
    monkeypatch.setattr(
        mac,
        "ax_wait",
        lambda **kwargs: {
            "element_index": 4,
            "role": "AXButton",
            "app": {"name": "Chrome", "pid": 42},
        },
    )
    monkeypatch.setattr(mac, "_pid", lambda app: 99)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(frontmost))
    monkeypatch.setattr(mac, "perform_action", lambda element_index, action: None)

    with pytest.raises(FocusChangedError, match="became frontmost"):
        mac.ax.press(app="Chrome", text="Not Now")


def test_ax_press_keeps_a_failed_action_error_when_the_target_activates(monkeypatch) -> None:
    """The focus guard runs only after `AXPress` returned: an action that
    raised keeps its own error even when the target came frontmost
    meanwhile, since `FocusChangedError` is what `press()` reads as
    proof that the press landed. The guard's after-reading is never
    taken, so the second frontmost state stays unread."""
    mac = MacOS()
    frontmost = iter(
        [
            {"name": "Ghostty", "pid": 1},
            {"name": "Chrome", "pid": 42},
        ]
    )

    def failing_action(element_index, action):
        raise MacOSError("AXPress failed with AXError -25204", code=ErrorCode.AX_ERROR)

    monkeypatch.setattr(
        mac,
        "ax_wait",
        lambda **kwargs: {
            "element_index": 4,
            "role": "AXButton",
            "app": {"name": "Chrome", "pid": 42},
        },
    )
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(frontmost))
    monkeypatch.setattr(mac, "perform_action", failing_action)

    with pytest.raises(MacOSError, match="AXPress failed") as raised:
        mac.ax.press(app="Chrome", text="Not Now")

    assert raised.value.code == ErrorCode.AX_ERROR
    assert next(frontmost) == {"name": "Chrome", "pid": 42}


def test_ax_wait_gone_requires_two_empty_polls(monkeypatch) -> None:
    mac = MacOS()
    responses = iter(
        [
            _found({"element_index": 1}),
            _found(),
            _found({"element_index": 2}),
            _found(),
            _found(),
        ]
    )
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: next(responses))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    mac.ax.wait_gone("Not Now", app="Chrome", timeout=1.0)

    # The second consecutive empty poll is the fifth response; a wait that
    # returned on the first empty poll would leave three unread.
    assert list(responses) == []


def test_ax_wait_gone_handles_exit_and_timeout(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac,
        "ax_search",
        lambda **kwargs: (_ for _ in ()).throw(ApplicationNotFoundError("not running")),
    )
    mac.ax.wait_gone("Not Now", app="Chrome")

    monkeypatch.setattr(
        mac,
        "ax_search",
        lambda **kwargs: _found({"element_index": 1}),
    )
    with pytest.raises(MacOSError, match="AX wait timed out") as exc_info:
        mac.ax.wait_gone("Not Now", app="Chrome", timeout=0)
    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details["consecutive_empty_polls"] == 0


def test_ax_wait_gone_timeout_after_one_empty_poll_reports_truthful_count(
    monkeypatch,
) -> None:
    """A single empty poll before timeout must not be reported as if the
    match had remained present: only two consecutive empty polls confirm
    absence, so the message and details must reflect that exactly one
    empty poll -- not zero -- was observed."""
    mac = MacOS()
    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: _found())

    with pytest.raises(MacOSError, match="two consecutive empty polls") as exc_info:
        mac.ax.wait_gone("Not Now", app="Chrome", timeout=0)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details["consecutive_empty_polls"] == 1
    assert "match remained" not in str(exc_info.value)


def test_background_click_posts_to_pid_without_warp_or_activate(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))
    _on_screen_windows(monkeypatch, (42, 7, 0, 0, 800, 600))
    monkeypatch.setattr(
        macos_module.AS,
        "CGWarpMouseCursorPosition",
        lambda point: pytest.fail("background click must not warp the cursor"),
    )

    mac.click(
        10,
        20,
        app="Slack",
        coordinate_space="screen",
    )

    assert posted == [42, 42]


def _routed(monkeypatch, mac: MacOS) -> list[tuple[int, int, tuple[float, float]]]:
    """Capture ``(pid, window_id, window-local point)`` for every posted event."""
    routed: list[tuple[int, int, tuple[float, float]]] = []
    locals_by_event: dict[int, tuple[float, float]] = {}
    monkeypatch.setattr(
        macos_module,
        "_set_window_location",
        lambda event, point: locals_by_event.__setitem__(event, (point.x, point.y)),
    )

    def post(event, pid):
        window_id = macos_module.AS.CGEventGetIntegerValueField(
            event, macos_module._CG_EVENT_WINDOW_NUMBER
        )
        routed.append((pid, window_id, locals_by_event[macos_module.objc.pyobjc_id(event)]))

    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", post)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    return routed


def test_click_routes_to_the_apps_frontmost_window_under_the_point(monkeypatch) -> None:
    """AppKit hit-tests a posted click against the window number and
    window-local point carried by the event, so both name the app's own
    frontmost window under the point, even with another app's window on
    top of it."""
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(
        monkeypatch,
        (99, 1, 0, 0, 2000, 2000),  # another app covers everything
        (42, 8, 300, 100, 200, 200),  # the app's sheet, over its document
        (42, 7, 100, 50, 800, 600),
    )

    result = mac.click(310, 120, app="Demo", coordinate_space="screen")

    assert routed == [(42, 8, (10.0, 20.0)), (42, 8, (10.0, 20.0))]
    assert result["window_id"] == 8


def test_click_skips_the_apps_own_overlay_over_the_window(monkeypatch) -> None:
    """A same-app surface above the normal layer (a tooltip, a menu bar
    extra's popover) is not a window `see` would list, so input routes
    past it to the window underneath."""
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(
        monkeypatch,
        (42, 9, 300, 100, 200, 200, 25),  # the app's own overlay, layer 25
        (42, 7, 100, 50, 800, 600),
    )

    result = mac.click(310, 120, app="Demo", coordinate_space="screen")

    assert routed == [(42, 7, (210.0, 70.0)), (42, 7, (210.0, 70.0))]
    assert result["window_id"] == 7


def test_click_fails_before_posting_when_window_routing_is_unavailable(monkeypatch) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(monkeypatch, (42, 7, 100, 50, 800, 600))
    monkeypatch.setattr(macos_module, "_set_window_location", None)

    with pytest.raises(MacOSError) as excinfo:
        mac.click(310, 120, app="Demo", coordinate_space="screen")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_OP.value
    assert routed == []


@pytest.mark.parametrize("clicks", [0, 4, True])
def test_click_rejects_a_count_outside_one_to_three_before_posting(monkeypatch, clicks) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(monkeypatch, (42, 7, 100, 50, 800, 600))

    with pytest.raises(MacOSError) as excinfo:
        mac.click(310, 120, app="Demo", coordinate_space="screen", clicks=clicks)

    assert excinfo.value.code == ErrorCode.BAD_REQUEST.value
    assert excinfo.value.details == {"parameter": "clicks", "value": clicks, "limit": 3}
    assert routed == []


def test_click_outside_every_window_of_the_app_posts_nothing(monkeypatch) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(monkeypatch, (42, 7, 100, 50, 800, 600))

    with pytest.raises(MacOSError) as excinfo:
        mac.click(10, 20, app="Demo", coordinate_space="screen")

    assert excinfo.value.code == ErrorCode.BAD_REQUEST.value
    assert routed == []


def test_drag_keeps_every_event_on_the_window_that_took_the_mouse_down(monkeypatch) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(monkeypatch, (42, 7, 100, 50, 200, 200), (42, 8, 300, 50, 200, 200))

    mac.drag(110, 60, 310, 60, app="Demo", coordinate_space="screen", steps=2)

    assert routed == [
        (42, 7, (10.0, 10.0)),
        (42, 7, (110.0, 10.0)),
        (42, 7, (210.0, 10.0)),
        (42, 7, (210.0, 10.0)),
    ]


def test_scroll_without_a_point_targets_the_screenshot_window_center(monkeypatch) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    _on_screen_windows(monkeypatch, (42, 7, 100, 50, 800, 600))
    mac._last_screenshot = {
        "pid": 42,
        "window_id": 7,
        "bounds": {"x": 100.0, "y": 50.0, "width": 800.0, "height": 600.0},
        "width": 800,
        "height": 600,
        "scale_x": 1.0,
        "scale_y": 1.0,
    }
    monkeypatch.setattr(mac, "_require_window_unchanged", lambda shot: None)

    mac.scroll(-3, app="Demo", unit="line")

    assert routed == [(42, 7, (400.0, 300.0))]


def test_scroll_without_a_point_or_a_screenshot_of_the_app_is_refused(monkeypatch) -> None:
    mac = MacOS()
    routed = _routed(monkeypatch, mac)
    mac._last_screenshot = {"pid": 11, "bounds": {"x": 0.0, "y": 0.0, "width": 8.0, "height": 6.0}}

    with pytest.raises(MacOSError) as excinfo:
        mac.scroll(-3, app="Demo")

    assert excinfo.value.code == ErrorCode.BAD_REQUEST.value
    assert routed == []


@pytest.mark.parametrize(
    ("frontmost_after", "activated"),
    [
        ({"name": "Test", "bundle_id": None, "pid": 42, "path": None}, True),
        ({"name": "Other", "bundle_id": None, "pid": 7, "path": None}, False),
    ],
)
def test_activate_makes_one_request_and_reports_what_macos_did(
    monkeypatch, frontmost_after, activated
) -> None:
    mac = MacOS()
    requests = []
    other = {"name": "Other", "bundle_id": None, "pid": 7, "path": None}
    info = {"name": "Test", "bundle_id": None, "pid": 42, "path": None}

    class _Running:
        def activateWithOptions_(self, options: int) -> bool:
            requests.append(options)
            return True

    monkeypatch.setattr(mac, "_resolve_app", lambda app: (_Running(), info))
    # Frontmost stays on the other app for the first poll, then settles.
    polls = iter([other, other, frontmost_after])
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(polls, frontmost_after))
    clock = iter(range(10_000))
    monkeypatch.setattr(macos_module.time, "monotonic", lambda: next(clock) * 0.1)
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    result = mac.activate("Test", timeout=0.5)

    assert requests == [macos_module.NSApplicationActivateIgnoringOtherApps]
    assert result["activated"] is activated
    assert result["previous"] == other
    assert result["frontmost"] == frontmost_after
    assert result["app"] == info


def test_input_without_target_is_refused(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementCopyElementAtPosition",
        lambda *args: pytest.fail("untargeted input must stop before AX hit testing"),
    )

    with pytest.raises(MacOSError, match="requires an app"):
        mac.type("hello")
    with pytest.raises(MacOSError, match="requires an app"):
        mac.click(10, 20, coordinate_space="screen")


def _typing_fakes(monkeypatch, mac: MacOS, *, frontmost_pid: int) -> tuple[list, list[float]]:
    """Fake the keyboard event calls; return the posted events and sleeps."""
    posted: list = []
    sleeps: list[float] = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Front", "pid": frontmost_pid})
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: event.update(length=length, text=text),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )
    return posted, sleeps


def test_type_into_an_inactive_app_posts_one_event_pair_per_character(monkeypatch) -> None:
    mac = MacOS()
    posted, sleeps = _typing_fakes(monkeypatch, mac, frontmost_pid=7)

    mac.type("aB !🙂", app="Spotify")

    downs = [event for (event, pid) in posted if event["down"]]
    assert [event["keycode"] for event in downs] == [0, 11, 49, 18, 0]
    assert [event["text"] for event in downs] == ["a", "B", " ", "!", "🙂"]
    assert [event["length"] for event in downs] == [1, 1, 1, 1, 2]
    assert downs[0]["flags"] == 0
    assert downs[1]["flags"] == macos_module.AS.kCGEventFlagMaskShift
    assert downs[3]["flags"] == macos_module.AS.kCGEventFlagMaskShift
    assert all(pid == 42 for _, pid in posted)
    assert sleeps == [0.01] * 5


def test_type_into_the_frontmost_app_packs_runs_of_text_per_event(monkeypatch) -> None:
    """A frontmost app takes text in runs of up to 20 UTF-16 units per
    key event with no pause, split only between code points; a newline
    still travels alone as the Return key."""
    mac = MacOS()
    posted, sleeps = _typing_fakes(monkeypatch, mac, frontmost_pid=42)

    mac.type("a" * 19 + "😀" + "b\n" + "c", app="Spotify")

    downs = [event for (event, pid) in posted if event["down"]]
    assert [event["text"] for event in downs] == ["a" * 19, "😀b", "\n", "c"]
    assert [event["length"] for event in downs] == [19, 3, 1, 1]
    assert [event["keycode"] for event in downs] == [0, 0, 36, 0]
    assert [event["flags"] for event in downs] == [0, 0, 0, 0]
    assert [pid for _, pid in posted] == [42] * 8
    assert sleeps == []


def test_key_posts_real_modifier_transitions(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Other", "pid": 7})
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )

    mac.key("cmd+shift+a", app="Spotify")

    command = macos_module.AS.kCGEventFlagMaskCommand
    shift = macos_module.AS.kCGEventFlagMaskShift
    assert [event for event, _ in posted] == [
        {"keycode": 55, "down": True, "flags": command},
        {"keycode": 56, "down": True, "flags": command | shift},
        {"keycode": 0, "down": True, "flags": command | shift},
        {"keycode": 0, "down": False, "flags": command | shift},
        {"keycode": 56, "down": False, "flags": command},
        {"keycode": 55, "down": False, "flags": 0},
    ]
    assert all(pid == 42 for _, pid in posted)


def test_typing_stops_if_target_becomes_frontmost(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    focus = iter(
        [
            {"name": "Terminal", "pid": 7},
            {"name": "Spotify", "pid": 42},
        ]
    )
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: next(focus))
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append((event, pid)))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateKeyboardEvent",
        lambda source, keycode, down: {"keycode": keycode, "down": down},
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventKeyboardSetUnicodeString",
        lambda event, length, text: event.update(text=text),
    )
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventSetFlags",
        lambda event, flags: event.update(flags=flags),
    )

    with pytest.raises(FocusChangedError, match="became frontmost during typing"):
        mac.type("ab", app="Spotify")

    assert len(posted) == 2


def test_background_click_uses_private_event_source(monkeypatch) -> None:
    mac = MacOS()
    sources = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: None)
    monkeypatch.setattr(mac, "_route_to_window", lambda event, window, point: None)
    _on_screen_windows(monkeypatch, (42, 7, 0, 0, 800, 600))
    monkeypatch.setattr(
        macos_module.AS,
        "CGEventCreateMouseEvent",
        lambda source, event_type, point, button: sources.append(source) or object(),
    )

    mac.click(
        10,
        20,
        app="Slack",
        coordinate_space="screen",
    )

    assert sources == [mac._event_source, mac._event_source]


def test_coordinate_click_never_guesses_an_ax_action(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(
        mac,
        "_application_element",
        lambda pid: pytest.fail("raw click must not inspect AX"),
    )
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))
    _on_screen_windows(monkeypatch, (42, 7, 0, 0, 800, 600))

    mac.click(10, 20, app="Slack", coordinate_space="screen")

    assert posted == [42, 42]


def test_click_screen_space_omits_image_coordinates_from_a_different_app(
    monkeypatch,
) -> None:
    """A screenshot of app A must never leak into the pointer info of a
    screen-space click aimed at app B: the ``image``/``inside`` keys are
    only meaningful for the app the retained screenshot actually belongs
    to, and ``coordinate_space='screen'`` never even asserts they match."""
    mac = MacOS()
    mac._last_screenshot = {
        "pid": 111,  # app A
        "bounds": {"x": 0.0, "y": 0.0, "width": 400.0, "height": 300.0},
        "width": 800,
        "height": 600,
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 222)  # app B
    monkeypatch.setattr(mac, "_post", lambda event, pid: None)
    _on_screen_windows(monkeypatch, (222, 9, 0, 0, 800, 600))

    pointer = mac.click(10, 20, app="B", coordinate_space="screen")

    assert pointer == {"screen": {"x": 10.0, "y": 20.0}, "window_id": 9}
    assert "image" not in pointer
    assert "inside" not in pointer


def test_screen_point_requires_screenshot() -> None:
    mac = MacOS()
    with pytest.raises(MacOSError, match="Take a screenshot"):
        mac._screen_point(10, 20, "screenshot")


def test_screen_point_converts_retina_pixels() -> None:
    mac = MacOS()
    mac._last_screenshot = {
        "bounds": {"x": -100.0, "y": 50.0, "width": 800.0, "height": 600.0},
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    assert mac._screen_point(400, 200, "screenshot") == (100.0, 150.0)
    assert mac._screen_point(400, 200, "window") == (300.0, 250.0)
    assert mac._screen_point(400, 200, "screen") == (400.0, 200.0)


def test_move_is_logical_only(monkeypatch) -> None:
    mac = MacOS()
    overlay_moves = []
    mac._last_screenshot = {
        "pid": 42,
        "window_id": 7,
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 800,
        "height": 600,
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    monkeypatch.setattr(mac, "_require_window_unchanged", lambda shot: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(
        mac,
        "_post",
        lambda event, pid: pytest.fail("logical movement must not post an event"),
    )
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: overlay_moves.append((x, y, duration)),
    )

    position = mac.move(200, 100, app="Test", duration=0.3)

    assert position == {
        "screen": {"x": 200.0, "y": 250.0},
        "image": {"x": 200.0, "y": 100.0},
        "inside": True,
    }
    assert overlay_moves == [(200.0, 250.0, 0.3)]


def test_move_requires_an_app_for_non_screen_coordinates(monkeypatch) -> None:
    mac = MacOS()
    mac._last_screenshot = {
        "pid": 42,
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 800,
        "height": 600,
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: pytest.fail(
            "must reject before ever moving the overlay pointer"
        ),
    )

    # No app given and no prior app snapshot: a screenshot/window-relative
    # move has nothing to bind its conversion to, and must fail closed
    # rather than silently reusing whatever `_last_screenshot` holds.
    with pytest.raises(MacOSError, match="requires an app") as exc_info:
        mac.move(200, 100)
    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == "app"


def test_move_rejects_a_screenshot_bound_to_a_different_app(monkeypatch) -> None:
    mac = MacOS()
    mac._last_screenshot = {
        "pid": 99,
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 800,
        "height": 600,
        "scale_x": 2.0,
        "scale_y": 2.0,
    }
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: pytest.fail(
            "must reject before ever moving the overlay pointer"
        ),
    )

    with pytest.raises(MacOSError, match="targets pid 99") as exc_info:
        mac.move(200, 100, app="Other")
    assert exc_info.value.code == ErrorCode.BAD_REQUEST


def test_move_screen_space_stays_app_free(monkeypatch) -> None:
    mac = MacOS()
    overlay_moves = []
    monkeypatch.setattr(mac, "_pid", lambda app: pytest.fail("screen-space move must not resolve an app"))
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: overlay_moves.append((x, y, duration)),
    )

    position = mac.move(10, 20, coordinate_space="screen")

    assert position == {"screen": {"x": 10.0, "y": 20.0}}
    assert overlay_moves == [(10.0, 20.0, 0.16)]


def test_pointer_overlay_controls(monkeypatch) -> None:
    mac = MacOS()
    calls = []
    monkeypatch.setattr(
        mac._overlay,
        "move",
        lambda x, y, *, duration: calls.append(("move", x, y, duration)),
    )
    monkeypatch.setattr(
        mac._overlay,
        "show",
        lambda x, y: calls.append(("show", x, y)),
    )
    monkeypatch.setattr(mac._overlay, "hide", lambda: calls.append(("hide",)))

    mac.move(10, 20, coordinate_space="screen")
    mac.hide_pointer()
    mac.show_pointer()

    assert calls == [
        ("move", 10.0, 20.0, 0.16),
        ("hide",),
        ("show", 10.0, 20.0),
    ]


_SRGB = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
_RGBA = Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big


def _white_image(width: int, height: int):
    context = Quartz.CGBitmapContextCreate(None, width, height, 8, 0, _SRGB, _RGBA)
    Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, width, height))
    return Quartz.CGBitmapContextCreateImage(context)


def _png_pixel(path: Path, x: int, y: int) -> tuple[int, ...]:
    """Return the RGBA bytes of top-left pixel (x, y) in the PNG at ``path``."""
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    context = Quartz.CGBitmapContextCreate(None, 1, 1, 8, 4, _SRGB, _RGBA)
    # Core Graphics draws bottom-up; shift so the wanted pixel lands at (0, 0).
    Quartz.CGContextDrawImage(
        context, Quartz.CGRectMake(-x, -(height - 1 - y), width, height), image
    )
    pixel = Quartz.CGBitmapContextCreateImage(context)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(pixel))
    return tuple(bytes(data)[:4])


_WHITE = (255, 255, 255, 255)


def _fake_window_capture(mac: MacOS, monkeypatch) -> None:
    """Stand in for the OS: a 400x300pt window on another Space, rendered 2x."""
    monkeypatch.setattr(mac, "_ensure_screen_recording", lambda: None)
    monkeypatch.setattr(
        mac, "_resolve_app", lambda app: (object(), {"name": "Test", "pid": 42})
    )
    monkeypatch.setattr(
        mac, "windows", lambda app: [{"window_id": 7, "title": "Doc", "pid": 42}]
    )
    monkeypatch.setattr(
        mac,
        "_frontmost_app",
        lambda: {"name": "Test", "bundle_id": "test", "pid": 42, "path": None},
    )
    monkeypatch.setattr(mac._overlay, "_send", lambda payload, *, start=True: None)

    def capture_window(window_id, *, max_width, max_height, timeout=5.0):
        return WindowCapture(
            image=_white_image(800, 600),
            width=800,
            height=600,
            bounds={"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
            on_screen=False,
            captured_at=1234.5,
        )

    monkeypatch.setattr(macos_module, "capture_window", capture_window)


def test_see_reports_pointer_position_and_draws_it_only_on_request(
    tmp_path: Path, monkeypatch
) -> None:
    mac = MacOS()
    _fake_window_capture(mac, monkeypatch)
    path = tmp_path / "window.png"
    mac.move(300, 350, coordinate_space="screen")

    plain = mac.see("Test", path=path, max_width=800, max_height=800)

    assert (plain["width"], plain["height"]) == (800, 600)
    assert plain["virtual_pointer"] == {
        "screen": {"x": 300.0, "y": 350.0},
        "image": {"x": 400.0, "y": 300.0},
        "inside": True,
        "visible": True,
    }
    assert plain["focus"] == {
        "frontmost": {"name": "Test", "bundle_id": "test", "pid": 42, "path": None},
        "target_is_frontmost": True,
    }
    assert (plain["on_screen"], plain["captured_at"]) == (False, 1234.5)
    assert _png_pixel(path, 404, 312) == _WHITE

    drawn = mac.see("Test", path=path, max_width=800, max_height=800, show_pointer=True)

    assert drawn["virtual_pointer"]["visible"] is True
    assert _png_pixel(path, 404, 312) != _WHITE


def test_see_does_not_draw_a_hidden_pointer(tmp_path: Path, monkeypatch) -> None:
    mac = MacOS()
    _fake_window_capture(mac, monkeypatch)
    path = tmp_path / "window.png"
    mac.move(300, 350, coordinate_space="screen")
    mac.hide_pointer()

    hidden = mac.see(
        "Test", path=path, max_width=800, max_height=800, show_pointer=True
    )

    assert hidden["virtual_pointer"]["visible"] is False
    assert _png_pixel(path, 404, 312) == _WHITE


def test_unknown_element_index_carries_element_unknown_code() -> None:
    mac = MacOS()
    with pytest.raises(MacOSError, match="Unknown element index") as exc_info:
        mac._element(999)
    assert exc_info.value.code == ErrorCode.ELEMENT_UNKNOWN
    assert exc_info.value.details["element_index"] == 999


def test_resolve_app_not_found_carries_code_and_query_detail(monkeypatch) -> None:
    mac = MacOS()

    class _FakeRunningApplication:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int) -> _FakeRunningApp | None:
            return None

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> Never:
            raise AssertionError("must not enumerate workspace for an int pid")

    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)

    with pytest.raises(ApplicationNotFoundError) as exc_info:
        mac._resolve_app(99999)

    assert exc_info.value.code == ErrorCode.APP_NOT_FOUND
    assert exc_info.value.details["query"] == 99999


def test_resolve_app_ambiguous_carries_code_query_and_matches(monkeypatch) -> None:
    mac = MacOS()
    first = _FakeRunningApp(11, name="Helper One")
    second = _FakeRunningApp(22, name="Helper Two")

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> type[_FakeWorkspace]:
            return _FakeWorkspace

        @staticmethod
        def runningApplications() -> list[_FakeRunningApp]:
            return [first, second]

    class _FakeRunningApplication:
        runningApplicationWithProcessIdentifier_ = staticmethod(lambda pid: None)

    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)
    monkeypatch.setattr(macos_module, "NSRunningApplication", _FakeRunningApplication)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: None)
    monkeypatch.setattr(mac, "windows", lambda app: [])

    with pytest.raises(MacOSError, match="ambiguous") as exc_info:
        mac._resolve_app("Helper")

    assert exc_info.value.code == ErrorCode.APP_AMBIGUOUS
    assert exc_info.value.details["query"] == "Helper"
    assert {match["pid"] for match in exc_info.value.details["matches"]} == {11, 22}


def test_resolve_app_ambiguous_ranks_frontmost_then_windows_then_newest(
    monkeypatch,
) -> None:
    """Two TextEdits after a relaunch: the caller needs the likely one first,
    with the evidence, and no pid chosen on its behalf."""
    mac = MacOS()
    stale = _FakeRunningApp(44, name="Helper", launched_seconds_ago=900.0)
    frontmost = _FakeRunningApp(33, name="Helper", launched_seconds_ago=600.0)
    windowed = _FakeRunningApp(22, name="Helper", launched_seconds_ago=300.0)
    newest = _FakeRunningApp(11, name="Helper", launched_seconds_ago=2.0)
    on_screen = {"window_id": 1, "on_screen": True}
    hidden = {"window_id": 2, "on_screen": False}
    windows_by_pid = {44: [hidden], 33: [], 22: [on_screen, on_screen], 11: []}

    class _FakeWorkspace:
        @staticmethod
        def sharedWorkspace() -> type[_FakeWorkspace]:
            return _FakeWorkspace

        @staticmethod
        def runningApplications() -> list[_FakeRunningApp]:
            return [stale, frontmost, windowed, newest]

    monkeypatch.setattr(macos_module, "NSWorkspace", _FakeWorkspace)
    monkeypatch.setattr(mac, "_frontmost_app", lambda: {"name": "Helper", "pid": 33})
    monkeypatch.setattr(mac, "windows", lambda app: windows_by_pid[app])

    with pytest.raises(MacOSError, match=r"pass a pid: Helper \(33: frontmost") as exc_info:
        mac._resolve_app("Helper")

    matches = exc_info.value.details["matches"]
    assert [match["pid"] for match in matches] == [33, 22, 11, 44]
    assert matches[1] == {
        "name": "Helper",
        "bundle_id": None,
        "pid": 22,
        "path": None,
        "frontmost": False,
        "on_screen_windows": 2,
        "launched_seconds_ago": 300.0,
    }


def test_wait_for_window_returns_the_first_non_empty_poll(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac, "_resolve_app", lambda app: (object(), {"name": "TextEdit", "pid": 42})
    )
    polls = iter([[], [], [{"window_id": 7, "on_screen": True}]])
    monkeypatch.setattr(mac, "windows", lambda app: next(polls))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    assert mac.wait_for_window("TextEdit", timeout=1.0) == [{"window_id": 7, "on_screen": True}]


def test_wait_for_window_times_out_at_the_deadline_with_the_app_in_details(
    monkeypatch,
) -> None:
    mac = MacOS()
    info = {"name": "TextEdit", "pid": 42}
    monkeypatch.setattr(mac, "_resolve_app", lambda app: (object(), info))
    polls: list[int] = []
    monkeypatch.setattr(mac, "windows", lambda app: polls.append(app) or [])
    clock = iter(float(tick) * 0.5 for tick in range(100))
    monkeypatch.setattr(macos_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(macos_module.time, "sleep", lambda seconds: None)

    with pytest.raises(MacOSError, match="TextEdit showed no window within 1s") as exc_info:
        mac.wait_for_window("TextEdit", timeout=1.0)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details == {"app": info, "timeout": 1.0}
    # The clock reads 0.0 at the start and 1.0 after the second poll, which
    # is the deadline; a third poll would mean the wait overshot it.
    assert polls == [42, 42]


def test_ax_wait_ambiguous_and_timeout_carry_machine_readable_codes(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(
        mac,
        "ax_search_all",
        lambda **kwargs: _found(
            {"element_index": 1, "role": "AXButton"},
            {"element_index": 2, "role": "AXButton"},
        ),
    )
    with pytest.raises(MacOSError, match="found 2 matches") as ambiguous:
        mac.ax.wait(all_apps=True, text="Not Now")
    assert ambiguous.value.code == ErrorCode.BAD_REQUEST
    assert ambiguous.value.details["count"] == 2

    monkeypatch.setattr(mac, "ax_search", lambda **kwargs: _found())
    with pytest.raises(MacOSError, match="timed out") as timed_out:
        mac.ax.wait(app="Chrome", text="Missing", timeout=0)
    assert timed_out.value.code == ErrorCode.TIMEOUT
    assert timed_out.value.details["timeout"] == 0


_NONFINITE_TIMING_VALUES = (
    pytest.param(math.nan, id="nan"),
    pytest.param(math.inf, id="inf"),
    pytest.param(-math.inf, id="-inf"),
)


@pytest.mark.parametrize("value", _NONFINITE_TIMING_VALUES)
def test_jsonable_rejects_a_bare_nonfinite_float(value) -> None:
    with pytest.raises(MacOSError, match="non-finite") as exc_info:
        MacOS._jsonable(value)
    assert exc_info.value.code == ErrorCode.AX_ERROR
    assert exc_info.value.details["value"] == str(float(value))


def test_jsonable_preserves_finite_floats_and_scalars() -> None:
    assert MacOS._jsonable(3.5) == 3.5
    assert MacOS._jsonable(0.0) == 0.0
    assert MacOS._jsonable(-2) == -2
    assert MacOS._jsonable("ok") == "ok"
    assert MacOS._jsonable(True) is True
    assert MacOS._jsonable(None) is None


def test_jsonable_rejects_a_nonfinite_ax_point() -> None:
    point = macos_module.AS.AXValueCreate(
        macos_module.AS.kAXValueCGPointType,
        macos_module.AS.CGPoint(math.nan, 1.0),
    )

    with pytest.raises(MacOSError, match="non-finite") as exc_info:
        MacOS._jsonable(point)

    assert exc_info.value.code == ErrorCode.AX_ERROR
    assert exc_info.value.details["field"] == "x"


def test_jsonable_rejects_a_nonfinite_ax_rect_while_keeping_finite_fields() -> None:
    rect = macos_module.AS.AXValueCreate(
        macos_module.AS.kAXValueCGRectType,
        macos_module.AS.CGRect(
            macos_module.AS.CGPoint(0.0, 0.0),
            macos_module.AS.CGSize(math.inf, 5.0),
        ),
    )

    with pytest.raises(MacOSError, match="non-finite") as exc_info:
        MacOS._jsonable(rect)

    assert exc_info.value.code == ErrorCode.AX_ERROR
    assert exc_info.value.details["field"] == "width"


def test_jsonable_converts_a_finite_ax_rect_unchanged() -> None:
    rect = macos_module.AS.AXValueCreate(
        macos_module.AS.kAXValueCGRectType,
        macos_module.AS.CGRect(
            macos_module.AS.CGPoint(1.0, 2.0),
            macos_module.AS.CGSize(3.0, 4.0),
        ),
    )

    assert MacOS._jsonable(rect) == {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}


def test_jsonable_converts_an_ax_range_to_location_and_length() -> None:
    selected = macos_module.AS.AXValueCreate(
        macos_module.AS.kAXValueCFRangeType,
        macos_module.AS.CFRange(4, 3),
    )

    assert MacOS._jsonable(selected) == {"location": 4, "length": 3}


def _fail_if_called(*_args: object, **_kwargs: object) -> None:
    pytest.fail("must not act before timing validation")


@pytest.mark.parametrize("value", _NONFINITE_TIMING_VALUES)
@pytest.mark.parametrize("field", ["timeout", "interval"])
def test_ax_wait_rejects_nonfinite_timeout_and_interval(monkeypatch, field, value) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "ax_search", _fail_if_called)
    monkeypatch.setattr(mac, "ax_search_all", _fail_if_called)
    monkeypatch.setattr(macos_module.time, "sleep", _fail_if_called)
    kwargs = {"app": "Chrome", "text": "Not Now", "timeout": 1.0, "interval": 0.1}
    kwargs[field] = value

    with pytest.raises(MacOSError, match="finite") as exc_info:
        mac.ax.wait(**kwargs)

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == field
    assert str(exc_info.value.details["value"]) == str(value)


@pytest.mark.parametrize("value", _NONFINITE_TIMING_VALUES)
@pytest.mark.parametrize("field", ["timeout", "interval"])
def test_ax_wait_gone_rejects_nonfinite_timeout_and_interval(
    monkeypatch, field, value
) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "ax_search", _fail_if_called)
    monkeypatch.setattr(mac, "ax_search_all", _fail_if_called)
    monkeypatch.setattr(macos_module.time, "sleep", _fail_if_called)
    kwargs = {"app": "Chrome", "text": "Not Now", "timeout": 1.0, "interval": 0.1}
    kwargs[field] = value

    with pytest.raises(MacOSError, match="finite") as exc_info:
        mac.ax.wait_gone(**kwargs)

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == field
    assert str(exc_info.value.details["value"]) == str(value)


@pytest.mark.parametrize("value", _NONFINITE_TIMING_VALUES)
@pytest.mark.parametrize("field", ["timeout", "interval"])
def test_ax_press_rejects_nonfinite_timeout_and_interval(monkeypatch, field, value) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "ax_wait", _fail_if_called)
    monkeypatch.setattr(mac, "_pid", _fail_if_called)
    monkeypatch.setattr(mac, "perform_action", _fail_if_called)
    monkeypatch.setattr(macos_module.time, "sleep", _fail_if_called)
    kwargs = {"app": "Chrome", "text": "Not Now", "timeout": 1.0, "interval": 0.1}
    kwargs[field] = value

    with pytest.raises(MacOSError, match="finite") as exc_info:
        mac.ax.press(**kwargs)

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == field
    assert str(exc_info.value.details["value"]) == str(value)


def test_click_rejects_a_screenshot_from_a_different_app(monkeypatch) -> None:
    """A window/screenshot-relative coordinate computed from one app's
    screenshot must never be silently posted to a different app's pid --
    it fails closed instead of mapping through the wrong window."""
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 99)
    monkeypatch.setattr(
        mac,
        "_post",
        lambda event, pid: pytest.fail("must not dispatch to the wrong app"),
    )
    mac._last_screenshot = {
        "pid": 42,
        "bounds": {"x": 0.0, "y": 0.0, "width": 800.0, "height": 600.0},
        "scale_x": 1.0,
        "scale_y": 1.0,
    }

    with pytest.raises(MacOSError, match="not the pid") as exc_info:
        mac.click(10, 20, app="OtherApp")

    assert exc_info.value.code == ErrorCode.BAD_REQUEST
    assert exc_info.value.details["parameter"] == "coordinate_space"
    assert exc_info.value.details["screenshot_pid"] == 42
    assert exc_info.value.details["target_pid"] == 99


def test_click_accepts_a_screenshot_from_the_same_app(monkeypatch) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))
    mac._last_screenshot = {
        "pid": 42,
        "window_id": 7,
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 400,
        "height": 300,
        "scale_x": 1.0,
        "scale_y": 1.0,
    }
    monkeypatch.setattr(mac, "_require_window_unchanged", lambda shot: None)
    _on_screen_windows(monkeypatch, (42, 7, 100, 200, 400, 300))

    mac.click(10, 20, app="SameApp")

    assert posted == [42, 42]


@pytest.mark.parametrize(
    ("described", "reason"),
    [
        ([], "closed"),
        (
            [
                {
                    "kCGWindowBounds": {
                        "X": 100,
                        "Y": 240,
                        "Width": 400,
                        "Height": 300,
                    },
                    "kCGWindowIsOnscreen": True,
                }
            ],
            "moved",
        ),
        (
            [
                {
                    "kCGWindowBounds": {
                        "X": 100,
                        "Y": 200,
                        "Width": 400,
                        "Height": 300,
                    },
                    "kCGWindowIsOnscreen": False,
                }
            ],
            "off_screen",
        ),
    ],
)
def test_click_refuses_screenshot_coordinates_once_the_window_changed(
    monkeypatch, described, reason
) -> None:
    mac = MacOS()
    posted = []
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "_ensure_post_events", lambda: None)
    monkeypatch.setattr(mac, "_pid", lambda app: 42)
    monkeypatch.setattr(mac, "_post", lambda event, pid: posted.append(pid))
    monkeypatch.setattr(
        macos_module.AS,
        "CGWindowListCreateDescriptionFromArray",
        lambda window_ids: described if list(window_ids) == [7] else [],
    )
    mac._last_screenshot = {
        "pid": 42,
        "window_id": 7,
        "bounds": {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0},
        "width": 400,
        "height": 300,
        "scale_x": 1.0,
        "scale_y": 1.0,
    }

    with pytest.raises(
        MacOSError, match="take a fresh screenshot|cannot land"
    ) as exc_info:
        mac.click(10, 20, app="SameApp")

    assert exc_info.value.code == "window.changed"
    assert exc_info.value.details["reason"] == reason
    assert posted == []


def test_get_app_state_never_invalidates_a_still_valid_screenshot(monkeypatch) -> None:
    mac = MacOS()
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(
        mac, "_resolve_app", lambda app: (object(), {"name": "Chrome", "pid": 42})
    )
    monkeypatch.setattr(mac, "_application_element", lambda pid, *, enhance=True: object())
    monkeypatch.setattr(
        mac,
        "_snapshot_tree",
        lambda *args, **kwargs: macos_module._TreeSnapshot(
            [], node_cut=False, depth_cut=False, read_cut=False
        ),
    )
    monkeypatch.setattr(mac, "windows", lambda app: [])
    existing_screenshot = {"pid": 42, "path": "/tmp/x.png"}
    mac._last_screenshot = existing_screenshot

    state = mac.get_app_state("Chrome", screenshot=False)

    assert state["screenshot"] is None  # this call did not request a new one
    assert mac._last_screenshot is existing_screenshot  # but the prior one survives


# --- fork safety: never deadlock on an inherited _native_lock -------------
#
# `os.fork()` runs inside a small helper script launched via `subprocess.run`,
# never inside the pytest worker process itself -- the worker has other
# threads besides the one deliberate lock-holder this scenario needs (pytest's
# own capture machinery, etc.), so forking it directly triggers CPython's
# "process is multi-threaded, fork() may lead to deadlocks" `DeprecationWarning`
# on every run, unrelated to anything actually under test here. A fresh,
# single-purpose child interpreter has exactly the threads this scenario
# deliberately starts, and its own `os.fork()` call still exercises the exact
# same production code against a real fork boundary; only that warning
# (emitted to the *helper's* own stderr, which this test never inspects) is
# what moving the fork there avoids.

_MACOS_NATIVE_LOCK_FORK_SCRIPT = r'''
import os
import sys
import threading
import time

from macos_harness.macos import MacOS, MacOSError

mac = MacOS()

# A background thread holds `_native_lock` across the fork, exactly like a
# real concurrent `close()`/`_acquire_native()` caller could -- the one
# scenario that would deadlock a forked child if either acquired the lock
# before checking pid identity first.
lock_held = threading.Event()
release_lock = threading.Event()


def _hold_lock():
    with mac._native_lock:
        lock_held.set()
        release_lock.wait(timeout=5.0)


holder = threading.Thread(target=_hold_lock)
holder.start()
if not lock_held.wait(timeout=5.0):
    print("SETUP_FAILED", flush=True)
    sys.exit(2)

child_pid = os.fork()
if child_pid == 0:
    # Both `close()` and `_acquire_native()` on a forked child's copy of
    # this instance must raise the one specific, expected `MacOSError`
    # (fork boundary crossed) rather than ever acquiring the lock; any
    # other exception is left to crash this child loudly instead of
    # being hidden, which os.waitpid() below still observes as a
    # (non-hanging) exit.
    for label, action in (("close", mac.close), ("acquire", mac._acquire_native)):
        start = time.monotonic()
        try:
            action()
        except MacOSError as exc:
            print(
                "CHILD %s code=%s elapsed=%.3f" % (label, exc.code, time.monotonic() - start),
                flush=True,
            )
        else:
            print(
                "CHILD %s code=none elapsed=%.3f" % (label, time.monotonic() - start),
                flush=True,
            )
    os._exit(0)

deadline = time.monotonic() + 5.0
exited = False
status = 0
while time.monotonic() < deadline:
    done_pid, status = os.waitpid(child_pid, os.WNOHANG)
    if done_pid == child_pid:
        exited = True
        break
    time.sleep(0.01)

release_lock.set()
holder.join(timeout=5.0)

if not exited:
    os.kill(child_pid, 9)
    os.waitpid(child_pid, 0)
    print("CHILD_TIMED_OUT", flush=True)
    sys.exit(3)

print(
    "PARENT_DONE child_exit_ok=%s"
    % (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0),
    flush=True,
)
'''


def test_macos_native_lock_after_fork_fails_fast_instead_of_deadlocking() -> None:
    """A forked child inherits a byte-for-byte copy of a live ``MacOS``
    instance, including ``_native_lock``, which some other thread might
    hold at the exact instant of ``fork()``. Both ``close()`` and
    ``_acquire_native()`` acquire that lock, and both must recognize the
    fork boundary and raise *before* ever attempting to -- never hang
    waiting on a lock only a now-nonexistent parent thread could release.
    See ``_MACOS_NATIVE_LOCK_FORK_SCRIPT`` above for why the fork itself
    happens in a helper subprocess rather than inline here.
    """
    if not hasattr(os, "fork"):
        pytest.skip("os.fork() is not available on this platform")

    result = subprocess.run(
        [sys.executable, "-c", _MACOS_NATIVE_LOCK_FORK_SCRIPT],
        capture_output=True,
        text=True,
        timeout=15.0,
        check=False,
    )

    assert result.returncode == 0, (
        f"helper subprocess failed (exit {result.returncode}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "CHILD close code=unsupported_op" in result.stdout, result.stdout
    assert "CHILD acquire code=unsupported_op" in result.stdout, result.stdout
    assert "PARENT_DONE child_exit_ok=True" in result.stdout, result.stdout
