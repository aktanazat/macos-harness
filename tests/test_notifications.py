"""`mac.notifications` over Notification Center's tree as macOS 27 drew a
banner: a window, a hosting view, a scroll area, and the banner, whose
description joins its app and texts and whose children hold each text
under its identifier. Nothing is posted or shown; each test reads the
fake tree, which a search finds elements in by the exact labels a match
carries, as Accessibility's search does."""

from __future__ import annotations

import json

import pytest

from macos_harness.errors import ErrorCode, MacOSError
from macos_harness.macos import MacOS, SearchMatches, _TreeSnapshot

_CENTER = 974
_BANNER = "586247AA-0D87-44F4-B10B-42173DDAFC56"
_CLOSE = "Name:Close\nTarget:0x0\nSelector:(null)"
_BUILD = (
    "Script Editor, Build done, CI, All tests passed",
    {"title": "Build done", "subtitle": "CI", "body": "All tests passed"},
)


class _Center(MacOS):
    """`MacOS` over Notification Center showing one banner with
    ``description`` and, under it, the ``texts`` by identifier; ``cut``
    says how the read of it fell short."""

    def __init__(self, description: str, texts: dict[str, str], **cut: object) -> None:
        super().__init__()
        self.cut = cut
        banner = {
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
        }
        self.tree: list[tuple[int, dict[str, object]]] = [
            (0, {"role": "AXApplication"}),
            (1, {"role": "AXWindow", "subrole": "AXSystemDialog"}),
            (2, {"role": "AXGroup"}),
            (3, {"role": "AXGroup"}),
            (4, {"role": "AXScrollArea"}),
            (5, banner),
            *(
                (6, {"role": "AXStaticText", "value": value, "identifier": part, "actions": ["AXShowMenu"]})
                for part, value in texts.items()
            ),
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


@pytest.mark.parametrize(
    ("description", "texts", "read"),
    [
        (*_BUILD, ("Script Editor", "Build done", "CI", "All tests passed")),
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
    ],
)
def test_notifications_reads_the_banner_on_screen(
    description: str, texts: dict[str, str], read: tuple[str, str, str | None, str]
) -> None:
    (note,) = _Center(description, texts).notifications()

    assert (note["app"], note["title"], note["subtitle"], note["body"]) == read
    assert note["actions"] == ["AXPress", "Show Details", "Show", "Close"]


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
