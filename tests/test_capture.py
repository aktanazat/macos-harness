from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass

import pytest
import Quartz

import macos_harness.capture as capture_module
from macos_harness._paths import user_temp_dir
from macos_harness.capture import (
    GrayPrint,
    WindowCatalog,
    _await,
    _crop_configuration,
    _crop_display,
    _output_size,
    _sck_error,
    _window_configuration,
    capture_window,
    gray_print,
)
from macos_harness.errors import AccessibilityPermissionError, ErrorCode, MacOSError


class _FakeNSError:
    def __init__(self, domain: str, code: int, description: str) -> None:
        self._domain = domain
        self._code = code
        self._description = description

    def domain(self) -> str:
        return self._domain

    def code(self) -> int:
        return self._code

    def localizedDescription(self) -> str:
        return self._description


@dataclass(frozen=True)
class _App:
    pid: int

    def processID(self) -> int:
        return self.pid


@dataclass(frozen=True)
class _Window:
    """A window as ScreenCaptureKit lists it, with the windows attached to
    it; all of them belong to one app."""

    bounds: tuple[float, float, float, float]
    attached: tuple[_Window, ...] = ()

    def frame(self) -> object:
        return Quartz.CGRectMake(*self.bounds)

    def isOnScreen(self) -> bool:
        return True

    def owningApplication(self) -> _App:
        return _App(501)


@dataclass(frozen=True)
class _Display:
    bounds: tuple[float, float, float, float]

    def frame(self) -> object:
        return Quartz.CGRectMake(*self.bounds)


_BUILT_IN = _Display((0.0, 0.0, 1728.0, 1117.0))
_WINDOW, _ATTACHED = 255, 128


def _picture(window: _Window, screen: _Display = _BUILT_IN) -> GrayPrint:
    """What ScreenCaptureKit drew of ``window`` on a 2x display under macOS
    27, by the route ``capture_window`` takes. Rendered by itself, the window
    brings every window attached to it, unless the configuration leaves them
    out, the group fitted into the configured pixels from the top left and
    never enlarged; cut out of the display, each sits where it is on screen.
    The window draws white, anything attached gray."""
    display = _crop_display(window, (window, *window.attached), (screen,))
    width, height = round(window.bounds[2] * 2), round(window.bounds[3] * 2)
    config = (
        _window_configuration(width, height)
        if display is None
        else _crop_configuration(window.frame(), display.frame(), width, height)
    )
    children = (
        not config.respondsToSelector_("includeChildWindows") or config.includeChildWindows()
    )
    drawn = (window, *window.attached) if children else (window,)
    width, height = int(config.width()), int(config.height())
    if display is None:
        left = min(each.bounds[0] for each in drawn)
        top = min(each.bounds[1] for each in drawn)
        right = max(each.bounds[0] + each.bounds[2] for each in drawn)
        bottom = max(each.bounds[1] + each.bounds[3] for each in drawn)
        scale = min(2.0, width / (right - left), height / (bottom - top))
    else:
        source = config.sourceRect()
        left = screen.bounds[0] + source.origin.x
        top = screen.bounds[1] + source.origin.y
        scale = width / source.size.width
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width, Quartz.CGColorSpaceCreateDeviceGray(),
        Quartz.kCGImageAlphaNone,
    )
    for each in drawn:
        x, y, w, h = each.bounds
        shade = _WINDOW if each is window else _ATTACHED
        Quartz.CGContextSetGrayFillColor(context, shade / 255, 1.0)
        Quartz.CGContextFillRect(context, Quartz.CGRectMake(
            (x - left) * scale, height - (y - top + h) * scale, w * scale, h * scale
        ))
    image = Quartz.CGBitmapContextCreateImage(context)
    return gray_print(image, width=width, height=height, points=window.bounds[2:])


@pytest.mark.parametrize(
    ("natural", "max_width", "max_height", "expected"),
    [
        ((1760, 896), None, None, (1760, 896)),
        ((1760, 896), 1280, None, (1280, 652)),
        ((1760, 896), None, 448, (880, 448)),
        ((1760, 896), 1280, 448, (880, 448)),
        ((1760, 896), 4000, 4000, (1760, 896)),
        ((3, 4000), 1, 1, (1, 1)),
    ],
    ids=[
        "native",
        "width bound",
        "height bound",
        "tighter bound wins",
        "never upscaled",
        "at least one pixel",
    ],
)
def test_output_size_fits_both_bounds_at_the_window_aspect_ratio(
    natural, max_width, max_height, expected
) -> None:
    assert _output_size(*natural, max_width, max_height) == expected


def test_user_declined_capture_is_the_screen_recording_permission_error() -> None:
    error = _sck_error(
        "Window capture",
        _FakeNSError("com.apple.ScreenCaptureKit.SCStreamErrorDomain", -3801, "declined"),
        window_id=7,
    )

    assert isinstance(error, AccessibilityPermissionError)
    assert error.details["permission"] == "screen_recording"


def test_other_capture_errors_keep_their_domain_and_code() -> None:
    error = _sck_error(
        "Window capture",
        _FakeNSError("NSOSStatusErrorDomain", -50, "bad window"),
        window_id=7,
    )

    assert isinstance(error, MacOSError)
    assert not isinstance(error, AccessibilityPermissionError)
    assert error.code == ErrorCode.AX_ERROR
    assert error.details["domain"] == "NSOSStatusErrorDomain"
    assert error.details["code"] == -50
    assert error.details["window_id"] == 7
    assert str(error) == "Window capture failed: bad window"


def test_await_returns_the_completion_value_and_error_in_order() -> None:
    result = _await(
        lambda handler: handler("content", None),
        operation="Window enumeration",
        timeout=1.0,
    )

    assert result == ("content", None)


def test_await_reports_a_capture_that_never_answers() -> None:
    with pytest.raises(MacOSError) as exc_info:
        _await(lambda handler: None, operation="Window capture", timeout=0.01)

    assert exc_info.value.code == ErrorCode.TIMEOUT
    assert exc_info.value.details["operation"] == "Window capture"
    assert exc_info.value.details["timeout"] == 0.01


_CAPTURE_AND_STAY = """
import sys
from macos_harness.capture import WindowCatalog, capture_window
capture_window(int(sys.argv[1]), max_width=200, max_height=None, catalog=WindowCatalog())
print("captured", flush=True)
sys.stdin.read()
"""


@pytest.mark.smoke
def test_a_window_captures_while_another_live_process_has_captured(monkeypatch) -> None:
    monkeypatch.setattr(capture_module, "user_temp_dir", user_temp_dir)
    window_id = next(
        int(each[Quartz.kCGWindowNumber])
        for each in Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        )
        if each.get(Quartz.kCGWindowLayer) == 0
    )
    other = subprocess.Popen(
        [sys.executable, "-c", _CAPTURE_AND_STAY, str(window_id)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert other.stdout.readline() == "captured\n"

        picture = capture_window(
            window_id, max_width=200, max_height=None, catalog=WindowCatalog()
        )
    finally:
        other.communicate("", timeout=30)

    assert picture.width == 200


def test_a_sheet_inside_a_window_is_in_its_picture() -> None:
    # TextEdit's save sheet over its document, as the window list read them.
    sheet = _Window((283.0, 168.0, 390.0, 318.0))
    document = _Window((185.0, 83.0, 586.0, 488.0), attached=(sheet,))

    looks = _picture(document)

    assert set(looks.pixels) == {_WINDOW, _ATTACHED}


def test_a_tip_hanging_past_a_window_leaves_the_window_full_size() -> None:
    # Calculator and the mode tip whose bottom 8 points cover its top edge,
    # as the window list read them when the tip halved Calculator's picture.
    tip = _Window((384.0, 466.0, 468.0, 101.0))
    calculator = _Window((414.0, 559.0, 230.0, 408.0), attached=(tip,))

    looks = _picture(calculator)

    assert set(looks.pixels[460 * 16:]) == {_WINDOW}


@pytest.mark.parametrize("left", [0.0, 1728.0], ids=["built-in display", "display to its right"])
def test_a_sheet_wider_than_its_window_is_drawn_across_it_at_full_size(left: float) -> None:
    # TextEdit's save sheet over its document narrowed to 300 points, as the
    # window list read them on the built-in display.
    sheet = _Window((left + 140.0, 168.0, 390.0, 318.0))
    document = _Window((left + 185.0, 83.0, 300.0, 488.0), attached=(sheet,))

    looks = _picture(document, _Display((left, 0.0, 1728.0, 1117.0)))

    sheet_top, sheet_bottom = 600 * 170, 600 * 806
    assert set(looks.pixels[sheet_top:sheet_bottom]) == {_ATTACHED}
    assert set(looks.pixels[:sheet_top] + looks.pixels[sheet_bottom:]) == {_WINDOW}
