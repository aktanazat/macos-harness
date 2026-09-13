from __future__ import annotations

import pytest

from macos_harness.capture import _await, _output_size, _sck_error
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
