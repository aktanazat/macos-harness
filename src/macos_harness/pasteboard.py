"""The general pasteboard, saved before a paste and put back after it.

`save` keeps every item with the data of each of its types, `write` puts
a paste's payload in their place unless something copied since the save,
and `restore` puts the saved items back unless something copied since
the payload was written: its contents are then the newer ones, and stay.

macOS has no compare-and-swap for a pasteboard. Every clear moves its
change count by exactly one, so a clear of ours that moved it by more
proves that a copy landed between our last look and our clear, and was
cleared: `restore` reports that as ``overwrote_copy``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .errors import ErrorCode, MacOSError
from .receipts import JSONValue

PLAIN_TEXT = "public.utf8-plain-text"
HTML = "public.html"
RTF = "public.rtf"
TSV = "public.utf8-tab-separated-values-text"

_Items = tuple[tuple[tuple[str, bytes], ...], ...]


class _BoardItem(Protocol):
    def types(self) -> Sequence[str]: ...

    def dataForType_(self, kind: str) -> bytes | None: ...


class Board(Protocol):
    """The part of an ``NSPasteboard`` a paste uses."""

    def changeCount(self) -> int: ...

    def clearContents(self) -> int: ...

    def writeObjects_(self, objects: list[object]) -> bool: ...

    def pasteboardItems(self) -> Sequence[_BoardItem] | None: ...


@dataclass(frozen=True, slots=True)
class Saved:
    """A pasteboard's items, each as its types and their data in order,
    the types whose owner gave no data when read, and its change count
    before they were read."""

    items: _Items
    unreadable: tuple[str, ...]
    change_count: int


def general() -> Board:
    from AppKit import NSPasteboard

    return NSPasteboard.generalPasteboard()


def _read(board: Board) -> tuple[_Items, tuple[str, ...]]:
    items: list[tuple[tuple[str, bytes], ...]] = []
    unreadable: list[str] = []
    for item in board.pasteboardItems() or ():
        flavors: list[tuple[str, bytes]] = []
        for kind in item.types():
            data = item.dataForType_(kind)
            if data is None:
                unreadable.append(str(kind))
            else:
                flavors.append((str(kind), bytes(data)))
        items.append(tuple(flavors))
    return tuple(items), tuple(unreadable)


def save(board: Board) -> Saved:
    change_count = int(board.changeCount())
    return Saved(*_read(board), change_count)


def _item(flavors: Sequence[tuple[str, bytes]]) -> object:
    from AppKit import NSPasteboardItem
    from Foundation import NSData

    item = NSPasteboardItem.alloc().init()
    for kind, data in flavors:
        item.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), kind)
    return item


def _put(board: Board, objects: Sequence[object]) -> tuple[int, bool]:
    """Replace ``board``'s contents with ``objects``, built beforehand so
    nothing runs between the caller's last look and the clear: the change
    count the clear left, and whether the write held. macOS refuses the
    write once another clear lands after ours."""
    change_count = int(board.clearContents())
    return change_count, not objects or bool(board.writeObjects_(list(objects)))


def write(
    board: Board, saved: Saved, flavors: Sequence[tuple[str, bytes]], files: Sequence[Path]
) -> int:
    """Put one item holding ``flavors`` and one file URL item per path in
    ``files`` on ``board``; the change count they were written at. A copy
    made since ``saved`` was read stops it before anything is written; a
    refused write puts ``saved`` back and raises with that report."""
    from AppKit import NSURL

    objects: list[object] = [_item(flavors)] if flavors else []
    objects.extend(NSURL.fileURLWithPath_(str(path)) for path in files)
    now = int(board.changeCount())
    if now != saved.change_count:
        raise MacOSError(
            "Something was copied while the clipboard was being saved, so it was "
            "left as it is and nothing was pasted",
            code=ErrorCode.AX_ERROR,
            details={"reason": "copied_before_paste", "change_count": now},
        )
    written, wrote = _put(board, objects)
    if not wrote:
        raise MacOSError(
            "macOS refused to write the paste to the pasteboard, so nothing was pasted",
            code=ErrorCode.AX_ERROR,
            details={
                "reason": "pasteboard_write_refused",
                "pasteboard": restore(board, saved, written),
            },
        )
    return written


def restore(board: Board, saved: Saved, written: int) -> dict[str, JSONValue]:
    """Put ``saved`` back on ``board`` while its change count is still
    ``written``, the count `write` returned, and read it back."""
    kept = tuple(flavors for flavors in saved.items if flavors)
    objects = [_item(flavors) for flavors in kept]
    overwrote = written != saved.change_count + 1
    now = int(board.changeCount())
    report: dict[str, JSONValue]
    if now != written:
        report = {"restored": False, "reason": "copied_since_paste", "change_count": now}
    else:
        cleared, wrote = _put(board, objects)
        overwrote = overwrote or cleared != written + 1
        if wrote:
            items, _ = _read(board)
            report = {
                "restored": True,
                "items": len(kept),
                "exact": items == saved.items and not saved.unreadable,
            }
        else:
            report = {
                "restored": False,
                "reason": "copied_while_restoring",
                "change_count": int(board.changeCount()),
            }
    if overwrote:
        report["overwrote_copy"] = True
    if saved.unreadable:
        report["unreadable_types"] = list(saved.unreadable)
    return report
