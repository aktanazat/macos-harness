"""A window's text read from its pixels, and a click on a line read. The
app's window is a fake whose pixels AppKit draws here at two pixels a
point; Apple Vision reads them for real. Each test asserts what was read,
where a click was posted, or that none was. What an agent last saw stays
in the test's own directory, where a run keeps it in the user's
temporary directory, so a second fake reads as a later run."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
import Quartz
from AppKit import (
    NSBitmapImageRep,
    NSColor,
    NSDeviceRGBColorSpace,
    NSFont,
    NSFontAttributeName,
    NSGraphicsContext,
    NSMakeRect,
    NSRectFill,
)
from Foundation import NSAttributedString, NSMakePoint

from macos_harness.capture import WindowCapture
from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError, _TargetWindow
from macos_harness.receipts import Acted, OperationError

_PANEL = 4700
_WINDOW = 41
_INFO = {"name": "Panel", "bundle_id": "app.panel", "pid": _PANEL, "path": None}
# The window on screen, in points.
_BOUNDS = {"x": 300.0, "y": 200.0, "width": 480.0, "height": 240.0}
# Each line the window shows, at its top left in window points.
_LINES = {"Wi-Fi settings": (40.0, 40.0), "Continue": (40.0, 120.0), "Share": (300.0, 120.0)}


def _drawn(text: str) -> NSAttributedString:
    return NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: NSFont.systemFontOfSize_(20)}
    )


def _picture(lines: dict[str, tuple[float, float]], width: float, height: float) -> object:
    """``lines`` drawn black on white at two pixels a point."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, int(width * 2), int(height * 2), 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    try:
        NSGraphicsContext.setCurrentContext_(context)
        Quartz.CGContextScaleCTM(context.CGContext(), 2, 2)
        NSColor.whiteColor().set()
        NSRectFill(NSMakeRect(0, 0, width, height))
        for text, (x, y) in lines.items():
            drawn = _drawn(text)
            drawn.drawAtPoint_(NSMakePoint(x, height - y - drawn.size().height))
    finally:
        NSGraphicsContext.restoreGraphicsState()
    return rep.CGImage()


def _server_window(number: int, bounds: dict[str, float]) -> dict[str, object]:
    """Panel's window ``number`` at ``bounds``, as the window server lists it."""
    return {
        "kCGWindowNumber": number,
        "kCGWindowOwnerPID": _PANEL,
        "kCGWindowOwnerName": "Panel",
        "kCGWindowName": "Panel",
        "kCGWindowLayer": 0,
        "kCGWindowBounds": {
            "X": bounds["x"], "Y": bounds["y"], "Width": bounds["width"], "Height": bounds["height"],
        },
        "kCGWindowIsOnscreen": True,
        "kCGWindowAlpha": 1.0,
    }


class _Panel(MacOS):
    """`MacOS` over Panel's window at `_BOUNDS` on screen, whose pixels
    show ``lines``, below the windows ``in_front``. What each agent last
    saw stays in ``home``. Records each click it would post."""

    def __init__(self, home: Path) -> None:
        super().__init__()
        self.home = home
        self.lines = dict(_LINES)
        self.bounds = dict(_BOUNDS)
        self.in_front: list[dict[str, object]] = []
        self.posted: list[tuple[int, tuple[float, float], int]] = []
        # Hidden, the virtual pointer never starts its helper window.
        self.hide_pointer()

    def _ensure_accessibility(self) -> None:
        return None

    def _ensure_post_events(self) -> None:
        return None

    def _ensure_screen_recording(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        if query is None and self._last_app is not None:
            query = self._last_app["pid"]
        if query in (_PANEL, "Panel"):
            return object(), _INFO
        raise ApplicationNotFoundError(f"No running app matches {query!r}")

    def _memory_place(self, pid: int) -> tuple[Path, str | None, int, float]:
        return self.home, None, pid, 0.0

    def _server_windows(self, *, on_screen_only: bool) -> tuple[dict[str, object], ...]:
        return (*self.in_front, _server_window(_WINDOW, self.bounds))

    def _window_image(
        self, window_id: int, *, max_width: int | None, max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        assert (window_id, max_width, max_height, size) == (_WINDOW, None, None, None)
        width, height = self.bounds["width"], self.bounds["height"]
        return WindowCapture(
            _picture(self.lines, width, height), int(width * 2), int(height * 2),
            dict(self.bounds), True, time.time(),
        )

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> None:
        raise MacOSError("Panel's focus is not read here", code=ErrorCode.AX_ERROR)

    def _post_click(
        self, pid: int, point: tuple[float, float], window: _TargetWindow, *, button: str, clicks: int,
    ) -> bool:
        self._pointer_position = point
        self.posted.append((pid, point, window.window_id))
        return False


def _line(mac: _Panel, text: str) -> dict[str, object]:
    """The match of the line reading ``text`` in a new read of Panel."""
    read = mac.text("Panel")
    number = 1
    while read[number]["text"] != text:
        number += 1
    return read[number]


def _center(frame: dict[str, float]) -> tuple[float, float]:
    return frame["x"] + frame["width"] / 2, frame["y"] + frame["height"] / 2


def test_text_lists_the_lines_a_window_shows_top_to_bottom(tmp_path: Path) -> None:
    assert _Panel(tmp_path).text("Panel").text.splitlines() == [
        'Panel window "Panel" (window_id 41)',
        '[t1] "Wi-Fi settings"',
        '[t2] "Continue"',
        '[t3] "Share"',
    ]


@pytest.mark.parametrize("text", list(_LINES))
def test_a_line_read_has_its_box_where_the_screen_shows_it(text: str, tmp_path: Path) -> None:
    """Vision measures from the picture's bottom left; the box is in
    screen points from the top left."""
    x, y = _LINES[text]
    extent = _drawn(text).size()
    center = _center(_line(_Panel(tmp_path), text)["frame"])

    assert _BOUNDS["x"] + x < center[0] < _BOUNDS["x"] + x + extent.width
    assert _BOUNDS["y"] + y < center[1] < _BOUNDS["y"] + y + extent.height


def test_text_reads_a_line_in_chinese(tmp_path: Path) -> None:
    """Vision reads only Latin script unless it detects the language."""
    mac = _Panel(tmp_path)
    mac.lines = {"无线局域网": (40.0, 40.0)}

    assert mac.text("Panel").text.splitlines()[1:] == ['[t1] "无线局域网"']


def test_text_with_a_query_lists_only_the_lines_holding_it(tmp_path: Path) -> None:
    assert _Panel(tmp_path).text("Panel", query="continue").text.splitlines()[1:] == [
        'Query "continue": 1 of 3 lines.',
        '[t2] "Continue"',
    ]


_CLICKS = [
    lambda mac, line: mac.click(element=line, app="Panel"),
    lambda mac, line: mac.do.click(element=line, app="Panel"),
]


@pytest.mark.parametrize("click", _CLICKS, ids=["click", "do-click"])
def test_a_click_on_a_line_read_lands_on_the_center_of_its_box(
    click: Callable[[_Panel, object], object], tmp_path: Path,
) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, "Continue")

    click(mac, line)

    assert mac.posted == [(_PANEL, _center(line["frame"]), _WINDOW)]


@pytest.mark.parametrize("click", _CLICKS, ids=["click", "do-click"])
@pytest.mark.parametrize(
    ("was", "now"), [("Continue", "Delete"), ("Share", "Shared")], ids=["replaced", "grown"],
)
def test_a_click_on_a_line_that_reads_otherwise_now_posts_nothing(
    click: Callable[[_Panel, object], object], was: str, now: str, tmp_path: Path,
) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, was)
    mac.lines[now] = mac.lines.pop(was)

    with pytest.raises(MacOSError) as caught:
        click(mac, line)

    assert (caught.value.code, mac.posted) == (ErrorCode.ELEMENT_UNKNOWN, [])
    assert not isinstance(caught.value, OperationError) or caught.value.receipt.acted is Acted.NO


@pytest.mark.parametrize("click", _CLICKS, ids=["click", "do-click"])
def test_a_click_on_a_line_read_before_its_window_moved_posts_nothing(
    click: Callable[[_Panel, object], object], tmp_path: Path,
) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, "Continue")
    mac.bounds["x"] += 10

    with pytest.raises(MacOSError) as caught:
        click(mac, line)

    assert (caught.value.code, mac.posted) == (ErrorCode.WINDOW_CHANGED, [])
    assert not isinstance(caught.value, OperationError) or caught.value.receipt.acted is Acted.NO


@pytest.mark.parametrize("click", _CLICKS, ids=["click", "do-click"])
def test_a_click_on_a_line_read_before_another_window_came_over_it_posts_nothing(
    click: Callable[[_Panel, object], object], tmp_path: Path,
) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, "Continue")
    mac.in_front.append(_server_window(_WINDOW + 1, _BOUNDS))

    with pytest.raises(MacOSError) as caught:
        click(mac, line)

    assert (caught.value.code, caught.value.details["reason"], mac.posted) == (
        ErrorCode.WINDOW_CHANGED, "other_window", [],
    )
    assert not isinstance(caught.value, OperationError) or caught.value.receipt.acted is Acted.NO


@pytest.mark.parametrize("above", ["Delete account", "continue"], ids=["other-text", "same-text"])
def test_an_id_last_seen_on_a_line_clicks_nothing_once_a_line_appeared_above_it(
    above: str, tmp_path: Path,
) -> None:
    """Ids number lines in reading order, so a later run's read gives the
    new line t2, the id the agent last saw on "Continue". Lines compare
    ignoring case, as the click's own check does."""
    assert '[t2] "Continue"' in str(_Panel(tmp_path).text("Panel"))
    later = _Panel(tmp_path)
    later.lines[above] = (40.0, 80.0)

    with pytest.raises(MacOSError) as caught:
        later.do.click(element=later.text("Panel")[2], app="Panel")

    assert (caught.value.code, later.posted) == (ErrorCode.ELEMENT_UNKNOWN, [])


def test_an_id_last_seen_on_a_line_clicks_that_line_after_its_window_moved(tmp_path: Path) -> None:
    assert '[t2] "Continue"' in str(_Panel(tmp_path).text("Panel"))
    later = _Panel(tmp_path)
    later.bounds["x"] += 25
    line = later.text("Panel")[2]

    later.do.click(element=line, app="Panel")

    assert (line["text"], later.posted) == ("Continue", [(_PANEL, _center(line["frame"]), _WINDOW)])


def test_a_receipt_names_the_line_its_click_aimed_at(tmp_path: Path) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, "Continue")

    receipt = mac.do.click(element=line, app="Panel")

    assert receipt.request["element"] == {
        "pid": _PANEL, "window_id": _WINDOW, "text": "Continue", "frame": line["frame"],
    }


@pytest.mark.parametrize(
    "act",
    [
        lambda mac, line: mac.do.press(element=line, app="Panel"),
        lambda mac, line: mac.ax.perform(line, "AXPress"),
        lambda mac, line: mac.click(element="t2", app="Panel"),
    ],
    ids=["do-press", "ax-perform", "click-by-id"],
)
def test_a_line_read_goes_only_to_a_click_given_its_match(
    act: Callable[[_Panel, object], object], tmp_path: Path,
) -> None:
    mac = _Panel(tmp_path)
    line = _line(mac, "Continue")

    with pytest.raises(MacOSError) as caught:
        act(mac, line)

    assert (caught.value.code, mac.posted) == (ErrorCode.BAD_REQUEST, [])
    assert "click(element=)" in str(caught.value)
