"""`mac.notifications` over Notification Center's tree as macOS 27 drew a
banner: a window, a hosting view, a scroll area, and the banner, whose
description joins its app and texts and whose children hold each text
under its identifier. Rows of other shapes are the ones agent-desktop's
reader takes: one known only by its stacking identifier, texts with no
identifier, and an alert's own buttons. Nothing is posted or shown; each
test reads the fake tree, which a search finds elements in by the exact
labels a match carries, as Accessibility's search does."""

from __future__ import annotations

import json

import pytest

import macos_harness.macos as macos_module
from macos_harness.errors import ErrorCode, MacOSError
from macos_harness.macos import MacOS, SearchMatches, _AttributeValues, _TreeSnapshot

_CENTER = 974
_BANNER = "586247AA-0D87-44F4-B10B-42173DDAFC56"
_CLOSE = "Name:Close\nTarget:0x0\nSelector:(null)"
_FIELDS = {"AXRole": "role", "AXIdentifier": "identifier", "AXTitle": "title", "AXDescription": "description"}


def _banner(description: str, **fields: object) -> dict[str, object]:
    return {
        "role": "AXGroup",
        "subrole": "AXNotificationCenterBanner",
        "description": description,
        "identifier": _BANNER,
        "actions": [
            "AXPress",
            "Name:Show Details\nTarget:0x0\nSelector:(null)",
            "Name:Show\nTarget:0x0\nSelector:(null)",
            _CLOSE,
        ],
        **fields,
    }


def _text(value: str, **fields: object) -> dict[str, object]:
    return {"role": "AXStaticText", "value": value, "actions": ["AXShowMenu"], **fields}


def _texts(texts: dict[str, str]) -> list[dict[str, object]]:
    return [_text(value, identifier=part) for part, value in texts.items()]


_BUILD = (
    _banner("Script Editor, Build done, CI, All tests passed"),
    _texts({"title": "Build done", "subtitle": "CI", "body": "All tests passed"}),
)


class _Center(MacOS):
    """`MacOS` over Notification Center showing one ``row``, a banner or
    an alert, with ``children`` under it; ``cut`` says how the read of it
    fell short."""

    def __init__(self, row: dict[str, object], children: list[dict[str, object]], **cut: object) -> None:
        super().__init__()
        self.cut = cut
        self.tree: list[tuple[int, dict[str, object]]] = [
            (0, {"role": "AXApplication"}),
            (1, {"role": "AXWindow", "subrole": "AXSystemDialog"}),
            (2, {"role": "AXGroup"}),
            (3, {"role": "AXGroup"}),
            (4, {"role": "AXScrollArea"}),
            (5, row),
            *((6, child) for child in children),
        ]
        self.nodes = [object() for _ in self.tree]

    def _ensure_accessibility(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        assert query == "com.apple.notificationcenterui"
        return object(), {"name": "Notification Center", "bundle_id": query, "pid": _CENTER, "path": None}

    def _application_element(
        self, pid: int, *, messaging_timeout: float | None = None, enhance: bool = True
    ) -> object:
        return self.nodes[0]

    def _snapshot_tree(self, root: object, **options: object) -> _TreeSnapshot:
        self._elements = {}
        nodes = [
            {"element_index": self._remember_element(node), "depth": depth, **fields}
            for node, (depth, fields) in zip(self.nodes, self.tree, strict=True)
        ]
        return _TreeSnapshot(
            nodes, **{"node_cut": False, "depth_cut": False, "read_cut": False, **self.cut}
        )

    def ax_search(self, **query: object) -> SearchMatches:
        labels = {key: query[key] for key in ("title", "identifier", "description") if query.get(key) is not None}
        found = [
            {"element_index": self._remember_element(node), "pid": _CENTER, "role": fields["role"]}
            for node, (_, fields) in zip(self.nodes, self.tree, strict=True)
            if fields["role"] == query["search_key"]
            and all(fields.get(key) == value for key, value in labels.items())
        ]
        return SearchMatches(found, complete=True, visited=len(self.tree))

    def _is_ax_element(self, value: object) -> bool:
        return value in self.nodes

    def _actions(self, element: object) -> list[str]:
        return list(self.tree[self.nodes.index(element)][1].get("actions", []))

    def _copy_attribute(self, element: object, attribute: str, *, checked: bool = False) -> object:
        assert attribute == "AXChildren"
        position = self.nodes.index(element)
        depth = self.tree[position][0]
        children = []
        for node, (child_depth, _) in zip(self.nodes[position + 1:], self.tree[position + 1:], strict=True):
            if child_depth <= depth:
                break
            if child_depth == depth + 1:
                children.append(node)
        return children

    def _copy_attributes(
        self, element: object, attributes: object, *, checked: bool = False
    ) -> _AttributeValues:
        fields = self.tree[self.nodes.index(element)][1]
        return _AttributeValues((name, fields.get(_FIELDS[name])) for name in attributes)

    def _hold_before_input(self, pid: int | None, window_id: int | None = None) -> None:
        """Nothing here is on screen to picture before an input."""


@pytest.mark.parametrize(
    ("description", "texts", "read"),
    [
        (
            "Script Editor, Build done, CI, All tests passed",
            {"title": "Build done", "subtitle": "CI", "body": "All tests passed"},
            ("Script Editor", "Build done", "CI", "All tests passed"),
        ),
        (
            "Script Editor, Build done, All tests passed",
            {"title": "Build done", "body": "All tests passed"},
            ("Script Editor", "Build done", None, "All tests passed"),
        ),
        # Without a title, the banner shows the app's name in its place,
        # and the description names the app once.
        (
            "Script Editor, All tests passed",
            {"title": "Script Editor", "body": "All tests passed"},
            ("Script Editor", "Script Editor", None, "All tests passed"),
        ),
        (
            "Acme, Inc. Builds, Build done, All tests passed",
            {"title": "Build done", "body": "All tests passed"},
            ("Acme, Inc. Builds", "Build done", None, "All tests passed"),
        ),
    ],
)
def test_notifications_reads_the_banner_on_screen(
    description: str, texts: dict[str, str], read: tuple[str, str, str | None, str]
) -> None:
    (note,) = _Center(_banner(description), _texts(texts)).notifications()

    assert (note["app"], note["title"], note["subtitle"], note["body"]) == read
    assert note["actions"] == ["AXPress", "Show Details", "Show", "Close"]


@pytest.mark.parametrize(
    ("description", "values", "read"),
    [
        (
            "Script Editor, Probe title, Probe body",
            ["Probe title", "Probe body", "now"],
            ("Script Editor", "Probe title", None, "Probe body"),
        ),
        (
            "Script Editor, Probe body",
            ["Probe body", "now"],
            ("Script Editor", "Script Editor", None, "Probe body"),
        ),
        (
            "Script Editor, Hello, world, Body, with, commas",
            ["Hello, world", "Body, with, commas", "2m ago"],
            ("Script Editor", "Hello, world", None, "Body, with, commas"),
        ),
    ],
)
def test_texts_without_identifiers_are_read_where_the_description_names_them(
    description: str, values: list[str], read: tuple[str, str, None, str]
) -> None:
    (note,) = _Center(_banner(description), [_text(value) for value in values]).notifications()

    assert (note["app"], note["title"], note["subtitle"], note["body"]) == read


def test_a_row_known_by_its_stacking_identifier_is_listed_with_its_words_in_its_element() -> None:
    row = {
        "role": "AXButton",
        "description": "Build done, CI, All tests passed",
        "attributes": {"AXStackingIdentifier": "bundleIdentifier=com.apple.ScriptEditor2"},
        "actions": ["AXPress", _CLOSE],
    }

    (note,) = _Center(row, []).notifications()

    assert (note["app"], note["title"], note["subtitle"], note["body"]) == (None, None, None, None)
    assert note["actions"] == ["AXPress", "Close"]
    assert note["element"]["description"] == "Build done, CI, All tests passed"


def test_an_alert_lists_its_apps_buttons_and_presses_one_by_name(monkeypatch) -> None:
    pressed: list[tuple[object, str]] = []
    monkeypatch.setattr(
        macos_module.AS,
        "AXUIElementPerformAction",
        lambda element, action: pressed.append((element, action)) or 0,
    )
    alert = _Center(
        _banner("Calendar, Standup, In 5 minutes", subrole="AXNotificationCenterAlert"),
        [
            *_texts({"title": "Standup", "body": "In 5 minutes"}),
            {"role": "AXButton", "identifier": "action_button", "title": "Snooze", "actions": ["AXPress"]},
            {"role": "AXButton", "identifier": "action_button", "description": "Join", "actions": ["AXPress"]},
            {"role": "AXButton", "identifier": "close_button", "description": "Dismiss", "actions": ["AXPress"]},
        ],
    )

    (note,) = alert.notifications()
    alert.ax.perform(note["element"], "snooze")

    assert note["actions"] == ["AXPress", "Show Details", "Show", "Close", "Snooze", "Join"]
    assert pressed == [(alert.nodes[8], "AXPress")]


def test_a_later_run_finds_a_notification_again_by_its_element() -> None:
    (note,) = _Center(*_BUILD).notifications()
    saved = json.loads(json.dumps(note["element"]))
    later = _Center(*_BUILD)

    assert later.ax.actions(saved)[-1] == _CLOSE


@pytest.mark.parametrize(
    "cut",
    [
        {"node_cut": True},
        {"depth_cut": True},
        {"read_cut": True, "read_failure": {"depth": 6, "attribute": "AXValue", "ax_error": -25204}},
    ],
)
def test_notifications_raise_when_notification_center_did_not_read_in_full(
    cut: dict[str, object],
) -> None:
    """A banner the read left out may still be on screen, so an empty or
    short list would claim what nobody saw."""
    with pytest.raises(MacOSError) as raised:
        _Center(*_BUILD, **cut).notifications()

    assert raised.value.code == ErrorCode.AX_ERROR
    assert {key: raised.value.details[key] for key in cut} == cut
