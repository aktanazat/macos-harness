from __future__ import annotations

from dataclasses import dataclass

import pytest
import Quartz

from macos_harness.capture import (
    _await,
    _output_size,
    _sck_error,
    _window_configuration,
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
class _Window:
    bounds: tuple[float, float, float, float]
    attached: tuple[_Window, ...] = ()


def _drawn(window: _Window, config: object) -> object:
    """The picture ScreenCaptureKit drew of ``window`` on a 2x display
    under macOS 27: every window attached to it too, unless ``config``
    leaves them out, the group fitted into the configured pixels from the
    top left and never enlarged. The window draws white, anything attached
    gray."""
    drawn = (window, *window.attached) if config.includeChildWindows() else (window,)
    left = min(each.bounds[0] for each in drawn)
    top = min(each.bounds[1] for each in drawn)
    right = max(each.bounds[0] + each.bounds[2] for each in drawn)
    bottom = max(each.bounds[1] + each.bounds[3] for each in drawn)
    width, height = int(config.width()), int(config.height())
    scale = min(2.0, width / (right - left), height / (bottom - top))
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width, Quartz.CGColorSpaceCreateDeviceGray(),
        Quartz.kCGImageAlphaNone,
    )
    for each in drawn:
        x, y, w, h = each.bounds
        Quartz.CGContextSetGrayFillColor(context, 1.0 if each is window else 0.5, 1.0)
        Quartz.CGContextFillRect(context, Quartz.CGRectMake(
            (x - left) * scale, height - (y - top + h) * scale, w * scale, h * scale
        ))
    return Quartz.CGBitmapContextCreateImage(context)


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


def test_a_window_is_pictured_alone_at_full_size_with_a_tip_attached() -> None:
    # Calculator and the mode tip hanging over its top edge, as the window
    # list read them when the tip halved Calculator's picture.
    tip = _Window((384.0, 466.0, 468.0, 101.0))
    calculator = _Window((414.0, 559.0, 230.0, 408.0), attached=(tip,))

    picture = _drawn(calculator, _window_configuration(460, 816))

    looks = gray_print(picture, width=460, height=816, points=(230.0, 408.0))
    assert set(looks.pixels) == {255}
