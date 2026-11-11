"""`mac.state` ids across runs: a later run's look at the same window
names each control by the id an earlier run printed, even after its label
changed in place, its diff lists only the controls whose text changed, no
id names controls in two windows, and an id whose line the agent has not
seen as it is now, or a newer read found changed, is refused rather than
handed to another control."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOS, MacOSError, _TreeSnapshot
from macos_harness.state import AppState, Memory, build_lines, diff

_WINDOW = {"x": 0.0, "y": 0.0, "width": 230.0, "height": 400.0}


def _calculator(shown: str, title: str, clear: str = "All Clear", last: str | None = None) -> list[dict[str, object]]:
    """Calculator's keypad window as `_snapshot_tree` walks it. The app
    writes the shown number into the result's identifier too, calls its
    clear button Clear while a number is typed, and after = shows the last
    expression in a scroll area of its own above the result's."""
    nodes: list[dict[str, object]] = [{"depth": 0, "role": "AXWindow", "title": title}]
    if last is not None:
        nodes += [
            {"depth": 1, "role": "AXScrollArea", "identifier": "StandardResultView"},
            {"depth": 2, "role": "AXStaticText", "description": "Last Expression", "value": last},
        ]
    nodes += [
        {"depth": 1, "role": "AXScrollArea", "identifier": f"StandardInputView;value:{shown}"},
        {"depth": 2, "role": "AXStaticText", "description": "Edit field", "value": shown},
        {"depth": 1, "role": "AXButton", "description": clear},
        {"depth": 1, "role": "AXButton", "description": "7"},
        {"depth": 1, "role": "AXButton", "description": "Equals"},
    ]
    return [{"element_index": index, **node} for index, node in enumerate(nodes)]


def _files(*names: str) -> list[dict[str, object]]:
    """A list of unlabeled rows, each holding a file name, as Finder's."""
    nodes: list[dict[str, object]] = [{"element_index": 0, "depth": 0, "role": "AXWindow"}]
    for name in names:
        nodes.append({"element_index": len(nodes), "depth": 1, "role": "AXRow"})
        nodes.append({"element_index": len(nodes), "depth": 2, "role": "AXTextField", "value": name})
    return nodes


_ON = {"x": 0.0, "y": 50.0, "width": 50.0, "height": 20.0}
_OFF = {"x": 0.0, "y": -100.0, "width": 50.0, "height": 20.0}


def _toolbar(*buttons: str, outside: str = "") -> list[dict[str, object]]:
    """A window's toolbar of buttons; the one named ``outside`` lies above
    the window, scrolled out of it."""
    nodes: list[dict[str, object]] = [{"depth": 0, "role": "AXWindow", "title": "Mail"}, {"depth": 1, "role": "AXToolbar"}]
    nodes += [
        {"depth": 2, "role": "AXButton", "description": label, "frame": _OFF if label == outside else _ON}
        for label in buttons
    ]
    return [{"element_index": index, **node} for index, node in enumerate(nodes)]


def _look(tmp_path, nodes, window: str = "window") -> list[tuple[int, str]]:
    """A printed look, remembered as `MacOS.state` remembers it."""
    with Memory.locked(tmp_path, "agent", 4512, 1.5) as memory:
        lines, offscreen, memory.next_id, slots = build_lines(
            nodes, memory.ids, memory.next_id, _WINDOW, memory.slots.get(window, {}), window,
        )
        look = [(line.id, line.render(), out) for line, out in zip(lines, offscreen, strict=True)]
        memory.remember(window, look, slots)
    return [(element_id, text) for element_id, text, _ in look]


def test_a_later_run_diffs_a_changed_value_under_the_id_it_printed(tmp_path) -> None:
    first = _look(tmp_path, _calculator("42", "Calculator"))
    second = _look(tmp_path, _calculator("28", "Calculator — 28"))

    assert diff(first, second, set()) == (
        "Changes since your last look (~ changed, + added; diff=False prints the whole window):\n"
        '~[1] window "Calculator — 28"\n'
        '~  [2] staticText "Edit field" = "28"'
    )


def test_a_button_relabelled_in_place_keeps_its_id_both_ways(tmp_path) -> None:
    cleared = _look(tmp_path, _calculator("0", "Calculator"))
    typed = _look(tmp_path, _calculator("7", "Calculator", clear="Clear"))
    again = _look(tmp_path, _calculator("0", "Calculator"))

    assert diff(cleared, typed, set()) == (
        "Changes since your last look (~ changed, + added; diff=False prints the whole window):\n"
        '~  [2] staticText "Edit field" = "7"\n'
        '~  [3] button "Clear"'
    )
    assert diff(typed, again, set()) == (
        "Changes since your last look (~ changed, + added; diff=False prints the whole window):\n"
        '~  [2] staticText "Edit field" = "0"\n'
        '~  [3] button "All Clear"'
    )


def test_a_relabelled_button_keeps_its_id_in_the_next_look(tmp_path) -> None:
    _look(tmp_path, _toolbar("All Clear", "7"))
    typed = _look(tmp_path, _toolbar("Clear", "7"))

    assert _look(tmp_path, _toolbar("Clear", "7")) == typed


def test_a_relabelled_button_shown_beside_its_old_label_takes_its_own_id(tmp_path) -> None:
    _look(tmp_path, _toolbar("All Clear", "7"))
    _look(tmp_path, _toolbar("Clear", "7"))
    ids = [element_id for element_id, _ in _look(tmp_path, _toolbar("Clear", "All Clear"))]

    assert sorted(ids) == sorted(set(ids))


def test_a_new_control_in_the_place_of_one_that_moved_takes_a_new_id(tmp_path) -> None:
    cleared = _look(tmp_path, _calculator("0", "Calculator"))
    totalled = _look(tmp_path, _calculator("5", "Calculator", last="2 + 3"))

    assert diff(cleared, totalled, set()) == (
        "Changes since your last look (~ changed, + added; diff=False prints the whole window):\n"
        "Removed: [2]\n"
        '+  [6] staticText "Last Expression" = "2 + 3"\n'
        '+  [7] staticText "Edit field" = "5"'
    )


def _state(
    lines: list[tuple[int, str]], seen: list[tuple[int, str]], shows: set[int] | None = None,
) -> AppState:
    read = dict(lines)
    return AppState(
        "", {element_id: {"element_index": element_id} for element_id, _ in lines}, complete=True,
        lines=read, seen=dict(seen), shows=read if shows is None else shows, on_show=None,
        owner=lambda _: None, latest=lambda: (read, True),
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


@pytest.mark.parametrize(
    ("history", "seen"),
    [
        ([_toolbar("Archive", "Reply"), _toolbar("Delete", "Reply", outside="Delete")], "Archive"),
        (
            [_toolbar("Archive", "Reply"), _toolbar("Delete", "Reply"), _toolbar("Archive", "Reply", outside="Archive")],
            "Delete",
        ),
    ],
    ids=["replaced outside", "relabelled back outside"],
)
def test_an_id_never_takes_another_label_outside_the_window(tmp_path, history, seen) -> None:
    """A look lists no line outside the window, so an id that took another
    label there would press a control the agent never saw it name."""
    looks = [_look(tmp_path, nodes) for nodes in history]
    pressed = next(element_id for element_id, line in looks[-2] if f'"{seen}"' in line)
    state = _state(_look(tmp_path, history[-1]), looks[-1])

    with pytest.raises(KeyError, match="not in this window's state"):
        state[pressed]


def test_a_relabel_seen_in_one_window_lends_its_id_to_no_other(tmp_path) -> None:
    archive = next(element_id for element_id, line in _look(tmp_path, _toolbar("Archive", "Reply"), "100") if '"Archive"' in line)
    _look(tmp_path, _toolbar("Delete", "Reply"), "100")
    _look(tmp_path, _toolbar("Archive", "Reply"), "100")
    reopened = _state(_look(tmp_path, _toolbar("Delete", "Reply"), "101"), [])

    with pytest.raises(KeyError, match="not in this window's state"):
        reopened[archive]


def test_the_same_controls_in_two_windows_take_different_ids(tmp_path) -> None:
    """An id printed from one document window must not name the same
    button in another."""
    first = _look(tmp_path, _toolbar("Archive", "Reply"), "100")
    second = _look(tmp_path, _toolbar("Archive", "Reply"), "101")

    assert {element_id for element_id, _ in first} & {element_id for element_id, _ in second} == set()


@pytest.mark.parametrize("seen", [[(4, '[4] button "Forward"')], []], ids=["changed since seen", "never seen"])
def test_a_printed_query_vouches_only_for_the_lines_it_shows(seen) -> None:
    """A query print shows a few lines; an id it left out is checked
    against the agent's earlier looks as if nothing had been printed."""
    state = _state([(3, '[3] button "Archive"'), (4, '[4] button "Reply"')], seen, shows={3})

    str(state)

    assert state[3] == {"element_index": 3}
    with pytest.raises(MacOSError) as raised:
        state[4]
    assert raised.value.code == ErrorCode.ELEMENT_UNKNOWN


def _inbox(*names: str, inside: int) -> list[dict[str, object]]:
    """Unlabeled rows of mail, the first ``inside`` of them inside the
    window and the rest scrolled out of it."""
    nodes: list[dict[str, object]] = [{"depth": 0, "role": "AXWindow", "title": "Inbox"}]
    for place, name in enumerate(names):
        frame = {**_ON, "y": 50.0 + 30 * place} if place < inside else _OFF
        nodes += [
            {"depth": 1, "role": "AXRow", "frame": frame},
            {"depth": 2, "role": "AXTextField", "value": name, "frame": frame},
        ]
    return nodes


class _Mail(MacOS):
    """`MacOS` over one app whose `state` reads ``trees`` (window to nodes,
    as `_snapshot_tree` walks them) where it would read the screen; unless
    ``numbered``, the window server numbers none of its windows."""

    def __init__(self, tmp_path: Path, trees: dict[int, list[dict[str, object]]], *, numbered: bool = True) -> None:
        super().__init__(agent_id="agent")
        self.trees = trees
        self.memory_dir = tmp_path
        self.numbered = numbered

    def _ensure_accessibility(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[None, dict[str, object]]:
        return None, {"pid": 4512, "name": "Mail"}

    def _state_window(self, pid: int, window_id: int | None) -> int | None:
        return window_id

    def _window_geometry(self, window: int) -> tuple[dict[str, float], bool]:
        return _WINDOW, False

    def _snapshot_tree(self, root: int, **_: object) -> _TreeSnapshot:
        nodes = [{**node, "element_index": self._remember_element(object())} for node in self.trees[root]]
        return _TreeSnapshot(nodes, node_cut=False, depth_cut=False, read_cut=False)

    def _memory_place(self, pid: int) -> tuple[Path, str | None, int, float]:
        return self.memory_dir, "agent", pid, 1.5

    def _ax_window_id(self, window: int) -> int | None:
        return window if self.numbered else None


def _id(text: str, label: str) -> int:
    return int(re.findall(rf"^ *\[(\d+)\] .*{re.escape(label)}", text, re.MULTILINE)[0])


def test_a_print_vouches_for_no_line_it_left_outside_the_window(tmp_path) -> None:
    """A row a query showed stays usable while its line holds; once a row
    inserted above hands its id to another, a whole print that lists the
    row only as outside the window refuses it, and so does the next run."""
    windows = {100: _inbox("Alice", "Bob", "Carol", "Dave", inside=2)}
    mac = _Mail(tmp_path, windows)
    dave = _id(str(mac.state("Mail", window_id=100, query="Dave")), '"Dave"')
    str(mac.state("Mail", window_id=100, diff=False))

    assert mac.state("Mail", window_id=100)[dave]["role"] == "AXTextField"

    windows[100] = _inbox("Eve", "Alice", "Bob", "Carol", "Dave", inside=2)
    whole = mac.state("Mail", window_id=100, diff=False)
    str(whole)

    for state in (whole, mac.state("Mail", window_id=100)):
        with pytest.raises(MacOSError, match='it was .*"Dave", now .*"Carol"'):
            state[dave]


def test_a_whole_look_shows_no_line_it_left_outside_the_window(tmp_path) -> None:
    """A whole print only counts the controls outside the window, so the
    agent has seen none of their lines."""
    _look(tmp_path, _toolbar("Archive", "Reply", outside="Reply"))

    with Memory.locked(tmp_path, "agent", 4512, 1.5) as memory:
        seen = memory.seen("window").values()

    assert [line for line in seen if '"Reply"' in line] == []
    assert [line for line in seen if '"Archive"' in line] != []


def test_a_state_read_earlier_refuses_an_id_a_newer_read_found_changed(tmp_path) -> None:
    windows = {100: _inbox("Alice", "Bob", inside=9)}
    mac = _Mail(tmp_path, windows)
    first = mac.state("Mail", window_id=100, diff=False)
    bob, inbox = _id(str(first), '"Bob"'), _id(str(first), 'window "Inbox"')

    windows[100] = _inbox("Eve", "Alice", "Bob", inside=9)
    mac.state("Mail", window_id=100)

    assert first[inbox]["role"] == "AXWindow"
    with pytest.raises(MacOSError, match="changed since this state was read") as raised:
        first[bob]
    assert raised.value.code == ErrorCode.ELEMENT_UNKNOWN


def test_two_windows_without_a_window_number_take_different_ids(tmp_path) -> None:
    windows = {
        number: [
            {"depth": 0, "role": "AXWindow", "subrole": "AXStandardWindow", "title": title},
            {"depth": 1, "role": "AXButton", "description": "Send"},
        ]
        for number, title in ((100, "Draft A"), (200, "Draft B"))
    }
    mac = _Mail(tmp_path, windows, numbered=False)
    send = _id(str(mac.state("Mail", window_id=100)), '"Send"')

    with pytest.raises(KeyError, match="not in this window's state"):
        mac.state("Mail", window_id=200)[send]
