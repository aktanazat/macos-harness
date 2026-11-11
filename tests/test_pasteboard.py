"""A paste's pasteboard round trip on a real, private pasteboard: the
saved items come back exactly, and a copy made by someone else during the
paste is kept or reported, never silently replaced."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from AppKit import NSPasteboard, NSPasteboardItem

from macos_harness import pasteboard


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
