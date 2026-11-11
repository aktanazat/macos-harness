"""A paste's pasteboard round trip on a real, private pasteboard: the
saved items come back exactly, and a copy made by someone else during the
paste is kept or reported, never silently replaced."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import objc
import pytest
from AppKit import NSPasteboard, NSPasteboardItem
from Foundation import NSObject

from macos_harness import pasteboard
from macos_harness.errors import MacOSError


@pytest.fixture
def board() -> Iterator[pasteboard.Board]:
    private = NSPasteboard.pasteboardWithUniqueName()
    yield private
    private.releaseGlobally()


def _copy(board: pasteboard.Board, text: str) -> None:
    item = NSPasteboardItem.alloc().init()
    item.setString_forType_(text, pasteboard.PLAIN_TEXT)
    board.clearContents()
    board.writeObjects_([item])


def _text(board: pasteboard.Board) -> list[str]:
    return [
        bytes(item.dataForType_(pasteboard.PLAIN_TEXT) or b"").decode()
        for item in board.pasteboardItems() or ()
    ]


def _two_items(board: pasteboard.Board) -> None:
    first = NSPasteboardItem.alloc().init()
    first.setString_forType_("original", pasteboard.PLAIN_TEXT)
    first.setString_forType_("<b>original</b>", pasteboard.HTML)
    second = NSPasteboardItem.alloc().init()
    second.setString_forType_("second", pasteboard.PLAIN_TEXT)
    board.clearContents()
    board.writeObjects_([first, second])


class _CopiesAfterLooking:
    """``board``, where someone else copies right after each look at the
    change count, as a copy that lands between a look and a clear does."""

    def __init__(self, board: pasteboard.Board) -> None:
        self._board = board

    def changeCount(self) -> int:
        now = int(self._board.changeCount())
        _copy(self._board, "copied by someone else")
        return now

    def clearContents(self) -> int:
        return self._board.clearContents()

    def writeObjects_(self, objects: list[object]) -> bool:
        return self._board.writeObjects_(objects)

    def pasteboardItems(self) -> Sequence[pasteboard._BoardItem] | None:
        return self._board.pasteboardItems()


def test_restore_puts_every_item_and_type_back(board: pasteboard.Board) -> None:
    _two_items(board)
    saved = pasteboard.save(board)

    written = pasteboard.write(board, saved, [(pasteboard.PLAIN_TEXT, b"payload")], [])
    report = pasteboard.restore(board, saved, written)

    assert report == {"restored": True, "items": 2, "exact": True}
    assert pasteboard.save(board).items == saved.items


def test_a_copy_made_after_the_paste_stays(board: pasteboard.Board) -> None:
    _two_items(board)
    saved = pasteboard.save(board)
    written = pasteboard.write(board, saved, [(pasteboard.PLAIN_TEXT, b"payload")], [])
    _copy(board, "copied by someone else")

    report = pasteboard.restore(board, saved, written)

    assert report == {
        "restored": False, "reason": "copied_since_paste", "change_count": written + 1,
    }
    assert _text(board) == ["copied by someone else"]


def test_a_copy_cleared_by_the_restore_is_reported(board: pasteboard.Board) -> None:
    _two_items(board)
    saved = pasteboard.save(board)
    written = pasteboard.write(board, saved, [(pasteboard.PLAIN_TEXT, b"payload")], [])

    report = pasteboard.restore(_CopiesAfterLooking(board), saved, written)

    assert report == {"restored": True, "items": 2, "exact": True, "overwrote_copy": True}


def test_a_copy_made_while_saving_stops_the_paste_untouched(board: pasteboard.Board) -> None:
    _two_items(board)
    saved = pasteboard.save(board)
    _copy(board, "copied by someone else")

    with pytest.raises(MacOSError) as stopped:
        pasteboard.write(board, saved, [(pasteboard.PLAIN_TEXT, b"payload")], [])

    assert stopped.value.details["reason"] == "copied_before_paste"
    assert _text(board) == ["copied by someone else"]


class _NeverSupplies(NSObject, protocols=[objc.protocolNamed("NSPasteboardItemDataProvider")]):
    """An owner that promised a type and never supplies its data."""

    def pasteboard_item_provideDataForType_(self, board: object, item: object, kind: str) -> None:
        del board, item, kind

    def pasteboardFinishedWithDataProvider_(self, board: object) -> None:
        del board


def test_a_type_never_supplied_makes_the_restore_inexact(board: pasteboard.Board) -> None:
    owner = _NeverSupplies.alloc().init()
    readable = NSPasteboardItem.alloc().init()
    readable.setString_forType_("kept", pasteboard.PLAIN_TEXT)
    readable.setDataProvider_forTypes_(owner, ["com.example.promised"])
    promised = NSPasteboardItem.alloc().init()
    promised.setDataProvider_forTypes_(owner, ["com.example.promised"])
    board.clearContents()
    board.writeObjects_([readable, promised])
    saved = pasteboard.save(board)
    written = pasteboard.write(board, saved, [(pasteboard.PLAIN_TEXT, b"payload")], [])

    report = pasteboard.restore(board, saved, written)

    assert _text(board) == ["kept"]
    assert report == {
        "restored": True, "items": 1, "exact": False,
        "unreadable_types": ["com.example.promised", "com.example.promised"],
    }
