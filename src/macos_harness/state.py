"""Compact window state for agents: one line per control, ids that hold
across runs, and after the first look only what changed.

`MacOS.state` walks one window and hands the nodes here. Lines read
``[12] button "All Clear"``; unlabeled layout containers are folded into
their children, and controls scrolled out of the window are left out.
Each control's id comes from its window and its path in that window's
tree, remembered per agent and process in the user's temporary
directory, so the id a program saw in one run names the same control in
the next, and no id names controls in two windows; a control whose label
changes in place in the window, as Calculator's "All Clear" turning
"Clear", keeps its id. The text an agent reads becomes its last look: a
named agent's next look returns the lines that changed since, or the
whole window when that is shorter. An id is handed out only as the agent
last saw it: one whose line changed since, or that no read of its window
showed, is refused until the agent reads again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .errors import ErrorCode, MacOSError
from .receipts import JSONValue

# One node of `MacOS._snapshot_tree`'s walk.
Node = Mapping[str, JSONValue]
# One line of a look: id, rendered text, outside the window.
Look = list[tuple[int, str, bool]]
# Each line's slot in a look, with its id and key part (`build_lines`).
Slots = dict[str, tuple[int, str]]

# Layout containers that carry nothing an agent reads or acts on unless
# they are labeled or selected.
_FOLDED_ROLES = frozenset({
    "AXGroup", "AXSplitGroup", "AXScrollArea", "AXLayoutArea", "AXUnknown", "AXCell", "AXStaticText",
})
# Shown with their value; their arrows and pages are left out (`mac.scroll`).
_LEAF_ROLES = frozenset({"AXScrollBar"})
_VALUE_LIMIT = 300
# A query match's subtree is shown with it when it is at most this many lines.
_SMALL_SUBTREE = 8
# Ids remembered per process: the newest this many survive.
_REMEMBERED_IDS = 20_000
# Windows remembered per agent and process: their last looks and newest reads.
_REMEMBERED_WINDOWS = 8
_WHOLE = "diff=False prints the whole window"


@dataclass(frozen=True)
class Line:
    id: int
    depth: int
    text: str
    haystack: str
    node: Node

    def render(self) -> str:
        return "  " * self.depth + self.text


class AppState:
    """What `MacOS.state` returns: printable text, and the match each id
    on it names, ready for ``click(element=)``, ``ax.perform`` or ``ax.set``.
    Reading the text makes it the agent's last look; ``shows`` are the ids
    whose lines it shows, ``owner`` names the window of an id that is not in
    this one, and ``latest`` reads the window's newest read in this process,
    which may have found an id's line changed since this one."""

    __slots__ = (
        "_latest", "_lines", "_matches", "_on_show", "_owner", "_seen", "_shown", "_shows", "_text", "complete",
    )

    def __init__(
        self,
        text: str,
        matches: Mapping[int, Node],
        *,
        complete: bool,
        lines: Mapping[int, str],
        seen: Mapping[int, str],
        shows: Container[int],
        on_show: Callable[[], None] | None,
        owner: Callable[[int], int | None],
        latest: Callable[[], tuple[Mapping[int, str], bool]],
    ) -> None:
        self._text = text
        self.complete = complete
        self._matches = matches
        self._lines = lines
        self._seen = seen
        self._shows = shows
        self._on_show = on_show
        self._owner = owner
        self._latest = latest
        self._shown = False

    @property
    def text(self) -> str:
        if not self._shown:
            self._shown = True
            if self._on_show is not None:
                self._on_show()
        return self._text

    def __str__(self) -> str:
        return self.text

    __repr__ = __str__

    def __getitem__(self, element_id: int) -> Node:
        try:
            match = self._matches[element_id]
        except KeyError:
            window = self._owner(element_id)
            raise KeyError(
                f"[{element_id}] is not in this window's state; read mac.state again" if window is None else
                f"[{element_id}] names a control in window_id {window}, not in this window; "
                f"pick it from mac.state(..., window_id={window})"
            ) from None
        latest, whole = self._latest()
        mine, newest = self._lines[element_id].strip(), latest.get(element_id, "").strip()
        if (newest or whole) and newest != mine:
            raise MacOSError(
                f"[{element_id}] changed since this state was read: it was {mine}, now {newest or 'gone'}. "
                "Read and print mac.state again, then pick again.",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"id": element_id, "was": mine, "now": newest},
            )
        if self._shown and element_id in self._shows:
            return match
        # An id of an unlabeled control is its place among its siblings, so
        # a row inserted above it hands its id to another control.
        was, now = self._seen.get(element_id, "").strip(), self._lines[element_id].strip()
        if was != now:
            change = f"changed since your last look: it was {was}, now {now}" if was else (
                f"is in no look you read at this window; it is now {now}"
            )
            raise MacOSError(
                f"[{element_id}] {change}. Print this state, then pick again.",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"id": element_id, "was": was, "now": now},
            )
        return match

    def __contains__(self, element_id: object) -> bool:
        return element_id in self._matches


class Reads:
    """Each window's lines at its newest `MacOS.state` read in this process,
    and whether that read was complete, for the last few windows read."""

    def __init__(self) -> None:
        self._reads: dict[tuple[int, str], tuple[Mapping[int, str], bool]] = {}

    def latest(
        self, window: tuple[int, str], lines: Mapping[int, str], *, complete: bool,
    ) -> Callable[[], tuple[Mapping[int, str], bool]]:
        """Make ``lines`` ``window``'s newest read; return what reads the
        newest one later."""
        read = (lines, complete)
        self._reads.pop(window, None)
        self._reads[window] = read
        while len(self._reads) > _REMEMBERED_WINDOWS:
            del self._reads[next(iter(self._reads))]
        return lambda: self._reads.get(window, read)


class _Parent(NamedTuple):
    key: str
    # Printed depth of its children.
    depth: int
    outside: bool
    children_left_out: bool
    # Label of the nearest printed ancestor, itself included.
    said: str
    in_menu: bool


class _Control(NamedTuple):
    node: Node
    key: str
    part: str
    slot: str
    depth: int
    outside: bool


def _role(node: Node) -> str:
    role = str(node.get("role") or "AXUnknown").removeprefix("AX")
    return role[:1].lower() + role[1:]


def _label(node: Node) -> str:
    for key in ("title", "description"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    value = node.get("value")
    if node.get("role") == "AXStaticText" and isinstance(value, str) and value.strip():
        return value
    subrole = node.get("subrole")
    return str(subrole).removeprefix("AX") if node.get("role") == "AXButton" and isinstance(subrole, str) else ""


def _quote(value: str) -> str:
    if len(value) > _VALUE_LIMIT:
        value = value[:_VALUE_LIMIT] + f"… [{len(value)} characters; pass query= to search the rest]"
    return json.dumps(value, ensure_ascii=False)


def _line_text(node: Node, element_id: int) -> str:
    parts = [f"[{element_id}]", _role(node)]
    label = _label(node)
    if label:
        parts.append(_quote(label))
    match node.get("value"):
        case bool() | int() as number:
            parts.append(f"= {int(number)}")
        case float() as number:
            parts.append(f"= {number:g}")
        case str() as text if text and text != label:
            parts.append("= " + _quote(text))
        case _ if "placeholder" in node:
            parts.append("placeholder " + _quote(str(node["placeholder"])))
    flags = [
        name for name, on in (
            ("disabled", node.get("enabled") is False),
            ("selected", node.get("selected") is True),
        ) if on
    ]
    if flags:
        parts.append(f"({', '.join(flags)})")
    return " ".join(parts)


def _key_part(node: Node) -> str:
    """What names a control among its siblings: its role and label, never
    its value. Not its identifier either: Calculator writes the shown
    number into one (``StandardInputView;value:28``). A window's title
    follows its content, so a window goes by its role alone."""
    names = [node.get(key) for key in ("title", "description")] if node.get("role") != "AXWindow" else []
    label = next((name for name in names if isinstance(name, str) and name), "")
    return f"{node.get('role')}:{node.get('subrole', '')}:{label}"


def _outside(frame: object, window: Mapping[str, float] | None) -> bool:
    if window is None or not isinstance(frame, Mapping):
        return False
    try:
        x, y, width, height = (float(frame[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return False
    if width <= 0 or height <= 0:
        return False
    return (
        x + width <= window["x"] or x >= window["x"] + window["width"]
        or y + height <= window["y"] or y >= window["y"] + window["height"]
    )


def build_lines(
    nodes: Sequence[Node],
    ids: dict[str, int],
    next_id: int,
    window_frame: Mapping[str, float] | None,
    last: Mapping[str, tuple[int, str]],
    window: str,
) -> tuple[list[Line], list[bool], int, Slots]:
    """Lines for ``nodes`` (a depth-first walk of ``window``), whether each
    lies outside ``window_frame``, the next free id, and each line's slot (a
    digest of its parent, its role, and how many of that role come before
    it there) with its id and key part.

    A control takes the id ``ids`` holds for its key, its window and path,
    else the id that held its slot in ``last``, the slots of the window's
    last printed look: under the same label, or under another so long as
    the control lies in the window, no control here still bears the old
    label, as one that moved does, and none claims the id by its key.
    Otherwise it takes a new id, saved in ``ids``. Outside the window, where
    a diff shows no change, a control never takes an id the last look showed
    under another label."""
    root = _Parent(window, 0, False, False, "", False)
    controls: list[_Control] = []
    stack: list[_Parent] = []
    siblings: dict[tuple[str, str], int] = {}
    kinds: dict[tuple[str, str], int] = {}
    for node in nodes:
        depth = int(node["depth"])
        del stack[depth:]
        parent = stack[-1] if stack else root
        part = _key_part(node)
        ordinal = siblings.get((parent.key, part), 0)
        siblings[(parent.key, part)] = ordinal + 1
        kind = f"{node.get('role')}:{node.get('subrole', '')}"
        position = kinds.get((parent.key, kind), 0)
        kinds[(parent.key, kind)] = position + 1
        key = f"{parent.key}/{part}#{ordinal}"
        # An open menu is on screen even where it runs past its window.
        in_menu = parent.in_menu or node.get("role") == "AXMenu"
        outside = not in_menu and (parent.outside or _outside(node.get("frame"), window_frame))
        label = _label(node)
        folded = parent.children_left_out or (
            node.get("role") in _FOLDED_ROLES
            and not node.get("selected")
            and (
                not any(node.get(name) for name in ("title", "description", "value"))
                # SwiftUI often nests a text in a text of the same words.
                or (label == parent.said and node.get("value") in (None, "", label))
            )
        )
        if folded:
            stack.append(parent._replace(key=key, outside=outside, in_menu=in_menu))
            continue
        slot = hashlib.blake2b(f"{parent.key}/{kind}@{position}".encode(), digest_size=8).hexdigest()
        controls.append(_Control(node, key, part, slot, parent.depth, outside))
        stack.append(_Parent(key, parent.depth + 1, outside, node.get("role") in _LEAF_ROLES, label, in_menu))
    was = {element_id: part for element_id, part in last.values()}
    claimed: list[int | None] = []
    for control in controls:
        element_id = ids.get(control.key)
        if element_id is not None and control.outside and was.get(element_id, control.part) != control.part:
            element_id = None
        claimed.append(element_id)
    taken = {element_id for element_id in claimed if element_id is not None}
    parts = {control.part for control in controls}
    lines: list[Line] = []
    offscreen: list[bool] = []
    slots: Slots = {}
    for control, element_id in zip(controls, claimed, strict=True):
        if element_id is None:
            held = last.get(control.slot)
            if held is not None and held[0] not in taken and (
                held[1] == control.part or not (control.outside or held[1] in parts)
            ):
                element_id = held[0]
            else:
                element_id, next_id = next_id, next_id + 1
                ids[control.key] = element_id
        slots[control.slot] = (element_id, control.part)
        node = control.node
        text = _line_text(node, element_id)
        full_value = node.get("value") if isinstance(node.get("value"), str) else ""
        lines.append(Line(element_id, control.depth, text, f"{text} {full_value}".lower(), node))
        offscreen.append(control.outside)
    return lines, offscreen, next_id, slots


def diff(old: Sequence[tuple[int, str]], new: Sequence[tuple[int, str]], present: Container[int]) -> str | None:
    """The change from ``old`` to ``new`` lines in the window, keyed by
    id: removed ids, then each changed (``~``) or added (``+``) line. An id
    still ``present``, as one scrolled out of the window, is not removed.
    None where the ids both hold moved, which a list of changes cannot say."""
    if list(old) == list(new):
        return f"No changes since your last look; {_WHOLE}."
    before, after = dict(old), dict(new)
    if [i for i, _ in old if i in after] != [i for i, _ in new if i in before]:
        return None
    out = [f"Changes since your last look (~ changed, + added; {_WHOLE}):"]
    removed = sorted(i for i in before if i not in after and i not in present)
    if removed:
        out.append("Removed: " + _ranges(removed))
    for element_id, line in new:
        if element_id not in before:
            out.append("+" + line)
        elif before[element_id] != line:
            out.append("~" + line)
    return "\n".join(out)


def _ranges(ids: Sequence[int]) -> str:
    spans: list[list[int]] = []
    for element_id in ids:
        if spans and element_id == spans[-1][1] + 1:
            spans[-1][1] = element_id
        else:
            spans.append([element_id, element_id])
    return ", ".join(f"[{a}]" if a == b else f"[{a}]-[{b}]" for a, b in spans)


def matching(haystacks: Sequence[str], query: str) -> list[int]:
    """The indexes of the lowercase ``haystacks`` that match ``query``:
    those holding the whole phrase, else any of its words of three or
    more letters."""
    phrase = query.strip().lower()
    matched = [i for i, haystack in enumerate(haystacks) if phrase and phrase in haystack]
    if not matched:
        words = [word for word in re.split(r"\W+", phrase) if len(word) >= 3]
        matched = [i for i, haystack in enumerate(haystacks) if any(word in haystack for word in words)]
    return matched


def project(lines: Sequence[Line], query: str) -> list[Line] | None:
    """The lines that match ``query`` (`matching`), their ancestors, and
    each match's subtree when small; None when nothing matches."""
    matched = matching([line.haystack for line in lines], query)
    if not matched:
        return None
    keep: set[int] = set()
    for index in matched:
        keep.add(index)
        depth = lines[index].depth
        for back in range(index - 1, -1, -1):
            if lines[back].depth < depth:
                keep.add(back)
                depth = lines[back].depth
        end = index + 1
        while end < len(lines) and lines[end].depth > lines[index].depth:
            end += 1
        if end - index - 1 <= _SMALL_SUBTREE:
            keep.update(range(index + 1, end))
        if end == index + 1 and end < len(lines) and lines[end].depth == lines[index].depth:
            keep.add(end)
    return [line for i, line in enumerate(lines) if i in keep]


class Memory:
    """One agent's ids and last looks for one process, kept as a file in
    the user's temporary directory, with the lines of each window it saw
    outside its last whole look there (``glances``) and the lines
    `MacOS.text` last showed it (``texts``). `Memory.locked` opens it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            saved = {}
        self.ids: dict[str, int] = dict(saved.get("ids", {}))
        self.next_id: int = int(saved.get("next_id", 1))
        self.looks: dict[str, Look] = {
            window: [(int(i), str(line), bool(out)) for i, line, out in look]
            for window, look in saved.get("looks", {}).items()
        }
        self.slots: dict[str, Slots] = {
            window: {slot: (int(i), str(part)) for slot, (i, part) in held.items()}
            for window, held in saved.get("slots", {}).items()
        }
        self.glances: dict[str, dict[int, str]] = {
            window: {int(i): str(line) for i, line in lines.items()}
            for window, lines in saved.get("glances", {}).items()
        }
        self.texts: dict[str, dict[str, JSONValue]] = {
            window: dict(lines) for window, lines in saved.get("texts", {}).items()
        }

    @classmethod
    @contextmanager
    def locked(cls, directory: Path, agent_id: str | None, pid: int, started: float) -> Iterator[Memory]:
        """The memory, held against other processes until the block ends,
        then saved."""
        agent = hashlib.sha256((agent_id or "").encode()).hexdigest()[:16]
        path = directory / agent / f"{pid}-{started:.6f}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            memory = cls(path)
            yield memory
            memory.save()
        finally:
            os.close(fd)

    def remember(self, window: str, look: Look, slots: Slots) -> None:
        seen = self.seen(window)
        self.looks.pop(window, None)
        self.looks[window] = look
        self.slots[window] = slots
        kept = {element_id: seen[element_id] for element_id, _, out in look if out and element_id in seen}
        self.glances.pop(window, None)
        if kept:
            self.glance(window, kept)
        while len(self.looks) > _REMEMBERED_WINDOWS:
            oldest = next(iter(self.looks))
            del self.looks[oldest]
            self.slots.pop(oldest, None)

    def glance(self, window: str, lines: Mapping[int, str]) -> None:
        """Make ``lines`` (id to line) what this agent last saw of those ids
        in ``window``, until a whole look there shows them."""
        self.glances[window] = {**self.glances.pop(window, {}), **lines}
        while len(self.glances) > _REMEMBERED_WINDOWS:
            del self.glances[next(iter(self.glances))]

    def seen(self, window: str) -> dict[int, str]:
        """Each id's line as this agent last saw it in ``window``: as its last
        whole look listed it, or as a later or earlier look showed it while
        that one left it outside the window."""
        look = self.looks.get(window, ())
        return {**{element_id: line for element_id, line, out in look if not out}, **self.glances.get(window, {})}

    def window_of(self, element_id: int) -> str | None:
        """The window whose control holds ``element_id``."""
        return next((key.partition("/")[0] for key, held in self.ids.items() if held == element_id), None)

    def remember_text(self, window: str, lines: Mapping[str, JSONValue]) -> None:
        """Make ``lines`` (id to match) what this agent last saw of each of
        those ids in ``window``; ids not among them keep what it saw before."""
        self.texts[window] = {**self.texts.pop(window, {}), **lines}
        while len(self.texts) > _REMEMBERED_WINDOWS:
            del self.texts[next(iter(self.texts))]

    def save(self) -> None:
        if len(self.ids) > _REMEMBERED_IDS:
            newest = sorted(self.ids.items(), key=lambda item: item[1])[-_REMEMBERED_IDS:]
            self.ids = dict(newest)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp") as handle:
            # json.dump writes token by token: twice as slow on a big window.
            handle.write(json.dumps({
                "ids": self.ids, "next_id": self.next_id, "looks": self.looks, "slots": self.slots,
                "glances": self.glances, "texts": self.texts,
            }))
        os.replace(handle.name, self.path)
