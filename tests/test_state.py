"""`mac.state` ids across runs: a later run's look at the same window
names each control by the id an earlier run printed, its diff lists only
the controls whose text changed, and an id whose line changed since the
agent's last look is refused rather than handed to another control."""

from __future__ import annotations

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOSError
from macos_harness.state import AppState, Memory, build_lines, diff

_WINDOW = {"x": 0.0, "y": 0.0, "width": 230.0, "height": 400.0}


def _calculator(shown: str, title: str) -> list[dict[str, object]]:
    """Calculator's keypad window as `_snapshot_tree` walks it. The app
    writes the shown number into the result's identifier too."""
    return [
        {"element_index": 0, "depth": 0, "role": "AXWindow", "title": title},
        {"element_index": 1, "depth": 1, "role": "AXScrollArea",
         "identifier": f"StandardInputView;value:{shown}"},
        {"element_index": 2, "depth": 2, "role": "AXStaticText",
         "description": "Edit field", "value": shown},
        {"element_index": 3, "depth": 1, "role": "AXButton", "description": "7"},
        {"element_index": 4, "depth": 1, "role": "AXButton", "description": "Equals"},
    ]


def _files(*names: str) -> list[dict[str, object]]:
    """A list of unlabeled rows, each holding a file name, as Finder's."""
    nodes: list[dict[str, object]] = [{"element_index": 0, "depth": 0, "role": "AXWindow"}]
    for name in names:
        nodes.append({"element_index": len(nodes), "depth": 1, "role": "AXRow"})
        nodes.append({"element_index": len(nodes), "depth": 2, "role": "AXTextField", "value": name})
    return nodes


def _look(tmp_path, nodes) -> list[tuple[int, str]]:
    with Memory.locked(tmp_path, "agent", 4512, 1.5) as memory:
        lines, _, memory.next_id = build_lines(nodes, memory.ids, memory.next_id, _WINDOW)
    return [(line.id, line.render()) for line in lines]


def test_a_later_run_diffs_a_changed_value_under_the_id_it_printed(tmp_path) -> None:
    first = _look(tmp_path, _calculator("42", "Calculator"))
    second = _look(tmp_path, _calculator("28", "Calculator — 28"))

    assert diff(first, second, set()) == (
        "Changes since your last look (~ changed, + added; diff=False prints the whole window):\n"
        '~[1] window "Calculator — 28"\n'
        '~  [2] staticText "Edit field" = "28"'
    )


def _state(lines: list[tuple[int, str]], seen: list[tuple[int, str]]) -> AppState:
    return AppState(
        "", {element_id: {"element_index": element_id} for element_id, _ in lines}, complete=True,
        lines=dict(lines), seen=dict(seen), on_show=None,
    )


def test_an_id_that_moved_to_another_row_is_refused_until_the_state_is_read(tmp_path) -> None:
    shown = _look(tmp_path, _files("Invoice.pdf", "Report.pdf"))
    report = next(element_id for element_id, line in shown if "Report.pdf" in line)
    state = _state(_look(tmp_path, _files("Alpha.pdf", "Invoice.pdf", "Report.pdf")), shown)

    with pytest.raises(MacOSError) as raised:
        state[report]
    assert raised.value.code == ErrorCode.ELEMENT_UNKNOWN
    assert raised.value.details["was"] == f'[{report}] textField = "Report.pdf"'

    str(state)
    assert state[report] == {"element_index": report}
