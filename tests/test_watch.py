"""What a watched route checks between the person's steps, and what the
watch keeps of the keys it sees."""

from __future__ import annotations

import json
import time

import ApplicationServices as AS
import pytest

from macos_harness.macos import _KEYCODES, MacOS
from macos_harness.ops import _Deadline
from macos_harness.receipts import Equals, Present
from macos_harness.routes import _load_step, _Locator, _step_json
from macos_harness.watch import _Seen, _Watcher

_APP = "com.example.demo"
_NAME = _Locator("AXTextField", "identifier", "name")
_EMAIL = _Locator("AXTextField", "identifier", "email")
_SAVE = _Locator("AXButton", "title", "Save")


def _focused(field: _Locator) -> Equals:
    return Equals(**field.query(), attribute="AXFocused", value=True)


def test_typed_text_and_the_step_before_it_check_the_field_has_focus_in_the_route_file() -> None:
    watcher = _Watcher(41, _Deadline(1.0, lambda: 0.0))
    watcher.seen = [
        _Seen("focus", _NAME, "", _NAME, after=_NAME),
        _Seen("type", _NAME, "Ada", _NAME, after=_EMAIL),
        _Seen("key", None, "tab", _EMAIL, after=_EMAIL),
        _Seen("type", _EMAIL, "ada@x", _EMAIL, after=_SAVE),
        _Seen("press", _SAVE, "", _SAVE),
    ]
    goal = Present(role="AXStaticText", identifier="saved")

    saved = [_load_step(json.loads(json.dumps(_step_json(step))), _APP) for step in watcher.steps(goal)]

    assert [step.postcondition for step in saved] == [
        _focused(_NAME), _focused(_NAME), _focused(_EMAIL), _focused(_EMAIL), goal]


_PID = 41
_USER = object()
_PASSWORD = object()
_ROLES = {_USER: ("AXTextField", None), _PASSWORD: ("AXTextField", "AXSecureTextField")}
_PLACES = {_USER: _Locator("AXTextField", "identifier", "username"),
           _PASSWORD: _Locator("AXTextField", "identifier", "password")}


class _App:
    """The app as a key's handling reads it: which field has focus, what
    kind of field it is, and how a route finds it again."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.focused: object = _USER
        monkeypatch.setattr(AS, "AXUIElementCopyAttributeValue", lambda element, name, out: (0, self.focused))
        monkeypatch.setattr(MacOS, "_copy_attribute", staticmethod(
            lambda element, name, **kwargs: dict(zip(("AXRole", "AXSubrole"), _ROLES[element],
                                                     strict=True)).get(name)))
        self.watcher = _Watcher(_PID, _Deadline(1.0, lambda: 0.0))
        monkeypatch.setattr(self.watcher, "_locate", _PLACES.get)

    def key(self, name: str, text: str, *, late: bool = False, flags: int = 0, repeat: bool = False) -> None:
        """A key the person typed: ``late`` when it came before the watch's
        last read of the app."""
        event = AS.CGEventCreateKeyboardEvent(None, _KEYCODES[name], True)
        AS.CGEventSetFlags(event, flags)
        AS.CGEventKeyboardSetUnicodeString(event, len(text), text)
        AS.CGEventSetIntegerValueField(event, AS.kCGEventTargetUnixProcessID, _PID)
        AS.CGEventSetIntegerValueField(event, AS.kCGKeyboardEventAutorepeat, int(repeat))
        AS.CGEventSetTimestamp(event, 1 if late else time.monotonic_ns())
        self.watcher._key(event)

    def kept(self) -> list[tuple[str, str]]:
        return [(seen.verb, seen.text) for seen in self.watcher.seen]


def _username_then_tab(app: _App) -> None:
    for letter in "alice":
        app.key(letter, letter)
    app.key("tab", "\t")


def test_a_password_typed_before_the_watch_read_the_app_after_tab_is_kept_as_the_secret(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)
    app.focused = _PASSWORD

    for letter in "zqvw":
        app.key(letter, letter, late=True)

    assert app.watcher.error is None
    assert app.kept() == [("type", "alice"), ("key", "tab"), ("secret", "")]


def test_a_late_key_whose_read_still_shows_the_field_before_tab_stops_the_watch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)

    app.key("z", "z", late=True)

    assert app.watcher.error is not None
    assert app.kept() == [("type", "alice"), ("key", "tab")]


@pytest.mark.parametrize("kind", ["option set, plain text", "held until it repeats"])
def test_a_key_whose_text_is_not_what_the_app_shows_stops_the_watch(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    app = _App(monkeypatch)
    for letter in "caf":
        app.key(letter, letter)

    if kind == "option set, plain text":
        app.key("e", "e", flags=AS.kCGEventFlagMaskAlternate)
    else:
        app.key("e", "e", repeat=True)

    assert app.watcher.error is not None
    assert app.kept() == [("type", "caf")]
