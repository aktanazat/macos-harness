"""Compact window state for agents: one line per control, ids that hold
across runs, and after the first look only what changed.

`MacOS.state` walks one window and hands the nodes here. Lines read
``[12] button "All Clear"``; unlabeled layout containers are folded into
their children, and controls scrolled out of the window are left out.
Each control's id comes from its path in the tree, remembered per agent
and process in the user's temporary directory, so the id a program saw
in one run names the same control in the next. The text an agent reads
becomes its last look: a named agent's next look returns the lines that
changed since, or the whole window when that is shorter, and an id whose
line changed since that look is refused until the agent reads again.
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
# Windows whose last look is remembered per agent and process.
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
    Reading the text makes it the agent's last look."""

    __slots__ = ("_lines", "_matches", "_on_show", "_seen", "_shown", "_text", "complete")

    def __init__(
        self,
        text: str,
        matches: Mapping[int, Node],
        *,
        complete: bool,
        lines: Mapping[int, str],
        seen: Mapping[int, str],
        on_show: Callable[[], None] | None,
    ) -> None:
        self._text = text
        self.complete = complete
        self._matches = matches
        self._lines = lines
        self._seen = seen
        self._on_show = on_show
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
            raise KeyError(
                f"[{element_id}] is not in this window's state; read mac.state again"
            ) from None
        # An id of an unlabeled control is its place among its siblings, so
        # a row inserted above it hands its id to another control.
        was, now = self._seen.get(element_id, "").strip(), self._lines[element_id].strip()
        if not self._shown and was and was != now:
            raise MacOSError(
                f"[{element_id}] changed since your last look: it was {was}, now {now}. "
                "Print this state, then pick again.",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"id": element_id, "was": was, "now": now},
            )
        return match

    def __contains__(self, element_id: object) -> bool:
        return element_id in self._matches


class _Parent(NamedTuple):
    key: str
    # Printed depth of its children.
    depth: int
    outside: bool
    children_left_out: bool
    # Label of the nearest printed ancestor, itself included.
    said: str
    in_menu: bool


_ROOT = _Parent("", 0, False, False, "", False)


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
) -> tuple[list[Line], list[bool], int]:
    """Lines for ``nodes`` (a depth-first walk), ids from ``ids`` or new,
    and for each line whether it lies outside ``window_frame``."""
    lines: list[Line] = []
    offscreen: list[bool] = []
    stack: list[_Parent] = []
    siblings: dict[tuple[str, str], int] = {}
    for node in nodes:
        depth = int(node["depth"])
        del stack[depth:]
        parent = stack[-1] if stack else _ROOT
        part = _key_part(node)
        ordinal = siblings.get((parent.key, part), 0)
        siblings[(parent.key, part)] = ordinal + 1
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
        element_id = ids.get(key)
        if element_id is None:
            element_id, next_id = next_id, next_id + 1
            ids[key] = element_id
        text = _line_text(node, element_id)
        full_value = node.get("value") if isinstance(node.get("value"), str) else ""
        lines.append(Line(element_id, parent.depth, text, f"{text} {full_value}".lower(), node))
        offscreen.append(outside)
        stack.append(_Parent(key, parent.depth + 1, outside, node.get("role") in _LEAF_ROLES, label, in_menu))
    return lines, offscreen, next_id


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


def project(lines: Sequence[Line], query: str) -> list[Line] | None:
    """The lines that match ``query``, their ancestors, and each match's
    subtree when small; None when nothing matches. The whole phrase is
    tried first, then any of its words of three or more letters."""
    phrase = query.strip().lower()
    matched = [i for i, line in enumerate(lines) if phrase and phrase in line.haystack]
    if not matched:
        words = [word for word in re.split(r"\W+", phrase) if len(word) >= 3]
        matched = [i for i, line in enumerate(lines) if any(word in line.haystack for word in words)]
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
    the user's temporary directory. `Memory.locked` opens it."""

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

    def remember(self, window: str, look: Look) -> None:
        self.looks.pop(window, None)
        self.looks[window] = look
        while len(self.looks) > _REMEMBERED_WINDOWS:
            self.looks.pop(next(iter(self.looks)))

    def save(self) -> None:
        if len(self.ids) > _REMEMBERED_IDS:
            newest = sorted(self.ids.items(), key=lambda item: item[1])[-_REMEMBERED_IDS:]
            self.ids = dict(newest)
        with tempfile.NamedTemporaryFile("w", dir=self.path.parent, delete=False, suffix=".tmp") as handle:
            json.dump({"ids": self.ids, "next_id": self.next_id, "looks": self.looks}, handle)
        os.replace(handle.name, self.path)
