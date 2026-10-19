"""An element passed as the match a search returned instead of its index.
An index lasts one run, so a match -- from this run or an earlier one --
is searched for again in its app by its role, labels, and frame. The
app's search is a fake over named elements; each test asserts which
element the match came back as, or that none could be told apart."""

from __future__ import annotations

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOS, MacOSError, SearchMatches

_PID = 4512
_LEFT = {"x": 10.0, "y": 20.0, "width": 80.0, "height": 24.0}
_RIGHT = {"x": 200.0, "y": 20.0, "width": 80.0, "height": 24.0}
_ELSEWHERE = {"x": 500.0, "y": 400.0, "width": 80.0, "height": 24.0}


class _Element:
    """One element of the fake app, named so a test can tell which came
    back; it answers reads itself, as the agent does for its handles."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.client = self

    def get_attributes(self, element: _Element, attributes: tuple[str, ...]) -> dict[str, str]:
        return {"name": element.name}


class _App(MacOS):
    """`MacOS` over one app of ``elements`` -- (name, role, title, frame)
    each -- whose search finds every element with the title asked for."""

    def __init__(
        self, *elements: tuple[str, str, str, dict[str, float]], complete: bool = True
    ) -> None:
        super().__init__()
        self.app_elements = elements
        self.complete = complete
        self.searched_pids: list[object] = []

    def ax_search(self, **query: object) -> SearchMatches:
        self.searched_pids.append(query["app_pid"])
        found = [
            {
                "element_index": self._remember_element(_Element(name)),
                "role": role,
                "title": title,
                "frame": frame,
                "pid": query["app_pid"],
            }
            for name, role, title, frame in self.app_elements
            if query["title"] in (None, title)
        ]
        return SearchMatches(found, complete=self.complete, visited=len(found))


def _button(**labels: object) -> dict[str, object]:
    """A button match as an earlier run's search printed it."""
    return {"element_index": 176, "pid": _PID, "role": "AXButton", **labels}


@pytest.mark.parametrize(
    ("elements", "match", "found"),
    [
        (
            [("save", "AXButton", "Save", _LEFT), ("cancel", "AXButton", "Cancel", _RIGHT)],
            _button(title="Save", frame=_LEFT),
            "save",
        ),
        ([("save", "AXButton", "Save", _ELSEWHERE)], _button(title="Save", frame=_LEFT), "save"),
        (
            [("first", "AXButton", "Delete", _LEFT), ("second", "AXButton", "Delete", _RIGHT)],
            _button(title="Delete", frame=_RIGHT),
            "second",
        ),
        (
            [("label", "AXStaticText", "Save", _LEFT), ("save", "AXButton", "Save", _RIGHT)],
            _button(title="Save", frame=_LEFT),
            "save",
        ),
    ],
    ids=["by-label", "moved", "twins-by-frame", "by-role"],
)
def test_a_match_from_an_earlier_run_finds_its_element_again(elements, match, found) -> None:
    app = _App(*elements)

    read = app.ax.get(match, ["AXTitle"])

    assert read == {"name": found}
    assert app.searched_pids == [_PID]


@pytest.mark.parametrize(
    ("elements", "match", "complete"),
    [
        ([], _button(title="Save", frame=_LEFT), True),
        (
            [("first", "AXButton", "Delete", _LEFT), ("second", "AXButton", "Delete", _RIGHT)],
            _button(title="Delete"),
            True,
        ),
        (
            [("first", "AXButton", "Delete", _LEFT), ("second", "AXButton", "Delete", _LEFT)],
            _button(title="Delete", frame=_LEFT),
            True,
        ),
        ([("save", "AXButton", "Save", _ELSEWHERE)], _button(title="Save", frame=_LEFT), False),
    ],
    ids=["gone", "twins-without-frame", "twins-at-one-frame", "moved-in-a-partial-search"],
)
def test_a_match_that_finds_no_single_element_is_unknown(elements, match, complete) -> None:
    """A partial search may hide a twin, so its one match must also be
    where the match was."""
    app = _App(*elements, complete=complete)

    with pytest.raises(MacOSError) as caught:
        app.ax.get(match, ["AXTitle"])

    assert caught.value.code == ErrorCode.ELEMENT_UNKNOWN


@pytest.mark.parametrize(
    "match",
    [{"role": "AXButton", "title": "Save"}, {"pid": _PID, "title": "Save"}],
    ids=["no-pid", "no-role"],
)
def test_a_match_without_its_pid_and_role_is_refused_unsearched(match) -> None:
    app = _App(("save", "AXButton", "Save", _LEFT))

    with pytest.raises(MacOSError) as caught:
        app.ax.get(match, ["AXTitle"])

    assert caught.value.code == ErrorCode.BAD_REQUEST
    assert app.searched_pids == []


def test_an_index_this_run_never_handed_out_names_passing_the_match() -> None:
    mac = MacOS()

    with pytest.raises(MacOSError) as caught:
        mac.ax.get(176, ["AXTitle"])

    assert caught.value.code == ErrorCode.ELEMENT_UNKNOWN
    assert "Pass the match itself" in str(caught.value)
