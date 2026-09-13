"""One-shot window capture through ScreenCaptureKit.

``SCScreenshotManager`` renders one window at the requested output size, so
a bounded ``see()`` never decodes a full Retina PNG only to shrink it. The
image is encoded exactly once, by ImageIO, straight into the output file.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AccessibilityPermissionError, ErrorCode, MacOSError
from .pointer import POINTER_HOTSPOT, pointer_points

try:
    import Quartz
    import ScreenCaptureKit as SCK
    from Foundation import NSDataWritingAtomic, NSMutableData
except ImportError as exc:  # pragma: no cover - exercised on non-macOS hosts
    Quartz = None  # type: ignore[assignment]
    SCK = None  # type: ignore[assignment]
    NSMutableData = None  # type: ignore[assignment]
    NSDataWritingAtomic = 0
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None

# ScreenCaptureKit answers on a private dispatch queue. A capture that has
# not answered by then is reported, not cancelled: the native work finishes
# on its own and the late callback lands in a box nobody reads.
CAPTURE_TIMEOUT = 5.0
_SC_STREAM_ERROR_DOMAIN = "com.apple.ScreenCaptureKit.SCStreamErrorDomain"
_SC_ERROR_USER_DECLINED = -3801
_PIXEL_FORMAT_BGRA = 1111970369  # kCVPixelFormatType_32BGRA ('BGRA')


@dataclass(frozen=True, slots=True)
class WindowCapture:
    """One rendered window: the ``CGImage`` plus the state it was taken in."""

    image: Any
    width: int
    height: int
    bounds: dict[str, float]
    on_screen: bool
    captured_at: float


def _await(
    start: Callable[[Callable[[Any, Any], None]], None],
    *,
    operation: str,
    timeout: float,
) -> tuple[Any, Any]:
    done = threading.Event()
    box: list[Any] = [None, None]

    def handler(value: Any, error: Any) -> None:
        box[0] = value
        box[1] = error
        done.set()

    start(handler)
    if not done.wait(timeout):
        raise MacOSError(
            f"{operation} did not answer within {timeout:g}s",
            code=ErrorCode.TIMEOUT,
            details={"operation": operation, "timeout": timeout},
        )
    return box[0], box[1]


def _sck_error(operation: str, error: Any, **details: Any) -> MacOSError:
    domain = str(error.domain())
    code = int(error.code())
    if domain == _SC_STREAM_ERROR_DOMAIN and code == _SC_ERROR_USER_DECLINED:
        return AccessibilityPermissionError(
            "Screen Recording permission is required. Grant it to the terminal "
            "or agent host in System Settings → Privacy & Security → Screen & "
            "System Audio Recording, or call mac.request_permissions().",
            details={"permission": "screen_recording"},
        )
    return MacOSError(
        f"{operation} failed: {error.localizedDescription()}",
        code=ErrorCode.AX_ERROR,
        details={"domain": domain, "code": code, **details},
    )


def _require_screencapturekit() -> None:
    if SCK is None:
        raise MacOSError(
            "macOS ScreenCaptureKit bindings are unavailable. Run on macOS 14 or "
            "newer after installing project dependencies with `uv sync`.",
            code=ErrorCode.AX_ERROR,
        ) from _IMPORT_ERROR


def shareable_window(window_id: int, *, timeout: float = CAPTURE_TIMEOUT) -> Any:
    """Return the ``SCWindow`` for a Core Graphics window number."""
    _require_screencapturekit()
    content, error = _await(
        lambda handler: (
            SCK.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(
                True, False, handler
            )
        ),
        operation="Window enumeration",
        timeout=timeout,
    )
    if error is not None:
        raise _sck_error("Window enumeration", error)
    for window in content.windows():
        if int(window.windowID()) == window_id:
            return window
    raise MacOSError(
        f"Window {window_id} is not capturable; it may have closed",
        code=ErrorCode.ELEMENT_UNKNOWN,
        details={"window_id": window_id},
    )


def _output_size(
    natural_width: float,
    natural_height: float,
    max_width: int | None,
    max_height: int | None,
) -> tuple[int, int]:
    """Pixel size that fits inside both bounds at the window's aspect ratio.

    A window is never upscaled, and each axis is at least one pixel.
    """
    ratio = 1.0
    if max_width is not None:
        ratio = min(ratio, max_width / natural_width)
    if max_height is not None:
        ratio = min(ratio, max_height / natural_height)
    return (
        max(1, round(natural_width * ratio)),
        max(1, round(natural_height * ratio)),
    )


def capture_window(
    window_id: int,
    *,
    max_width: int | None,
    max_height: int | None,
    timeout: float = CAPTURE_TIMEOUT,
) -> WindowCapture:
    """Render one window at most ``max_width`` x ``max_height`` pixels.

    ``None`` for either bound keeps the window's native pixel size on that
    axis. The window is captured on its own (``desktopIndependentWindow``):
    no shadow, no other window, and no clipping at the screen edge. An
    off-screen window still renders, but from whatever the app last drew --
    apps do not repaint hidden content, so ``on_screen`` is the caller's
    only freshness signal.
    """
    window = shareable_window(window_id, timeout=timeout)
    content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(
        window
    )
    scale = float(content_filter.pointPixelScale())
    rect = content_filter.contentRect()
    bounds = {
        "x": float(rect.origin.x),
        "y": float(rect.origin.y),
        "width": float(rect.size.width),
        "height": float(rect.size.height),
    }
    natural_width = bounds["width"] * scale
    natural_height = bounds["height"] * scale
    if natural_width < 1 or natural_height < 1:
        raise MacOSError(
            f"Window {window_id} has no drawable area",
            code=ErrorCode.ELEMENT_UNKNOWN,
            details={"window_id": window_id, "bounds": bounds},
        )
    width, height = _output_size(natural_width, natural_height, max_width, max_height)

    config = SCK.SCStreamConfiguration.alloc().init()
    config.setWidth_(width)
    config.setHeight_(height)
    config.setPixelFormat_(_PIXEL_FORMAT_BGRA)
    config.setScalesToFit_(False)
    config.setPreservesAspectRatio_(True)
    config.setCaptureResolution_(SCK.SCCaptureResolutionBest)
    config.setShowsCursor_(False)
    config.setIgnoreShadowsSingleWindow_(True)
    config.setIgnoreGlobalClipSingleWindow_(True)
    config.setShouldBeOpaque_(False)

    image, error = _await(
        lambda handler: (
            SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, handler
            )
        ),
        operation="Window capture",
        timeout=timeout,
    )
    captured_at = time.time()
    if error is not None:
        raise _sck_error("Window capture", error, window_id=window_id)
    if image is None:
        raise MacOSError(
            f"Window capture returned no image for window {window_id}",
            code=ErrorCode.AX_ERROR,
            details={"window_id": window_id},
        )
    return WindowCapture(
        image=image,
        width=int(Quartz.CGImageGetWidth(image)),
        height=int(Quartz.CGImageGetHeight(image)),
        bounds=bounds,
        on_screen=bool(window.isOnScreen()),
        captured_at=captured_at,
    )


def draw_pointer(image: Any, x: float, y: float, scale: float) -> Any:
    """Return a copy of ``image`` with the virtual pointer drawn at (x, y) px.

    ``scale`` is image pixels per screen point. The arrow never shrinks
    below one pixel per point: it marks a position for a reader, it is not
    a screen replica.
    """
    width = Quartz.CGImageGetWidth(image)
    height = Quartz.CGImageGetHeight(image)
    context = Quartz.CGBitmapContextCreate(
        None,
        width,
        height,
        8,
        0,
        Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
        Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little,
    )
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    # Core Graphics puts the origin bottom-left; flip so pointer geometry
    # stays in the top-left image coordinates every caller reasons in.
    Quartz.CGContextTranslateCTM(context, 0, height)
    Quartz.CGContextScaleCTM(context, 1, -1)

    draw_scale = max(1.0, scale)
    hot_x, hot_y = POINTER_HOTSPOT
    points = [
        (x + (px - hot_x) * draw_scale, y + (py - hot_y) * draw_scale)
        for px, py in pointer_points()
    ]
    Quartz.CGContextBeginPath(context)
    Quartz.CGContextMoveToPoint(context, *points[0])
    for point in points[1:]:
        Quartz.CGContextAddLineToPoint(context, *point)
    Quartz.CGContextClosePath(context)
    Quartz.CGContextSetLineJoin(context, Quartz.kCGLineJoinRound)
    Quartz.CGContextSetRGBFillColor(context, 0, 0, 0, 1)
    Quartz.CGContextSetRGBStrokeColor(context, 1, 1, 1, 0.94)
    Quartz.CGContextSetLineWidth(context, 1.5 * draw_scale)
    Quartz.CGContextDrawPath(context, Quartz.kCGPathFillStroke)
    return Quartz.CGBitmapContextCreateImage(context)


def write_png(image: Any, path: Path) -> None:
    data = NSMutableData.data()
    destination = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(destination, image, None)
    if not Quartz.CGImageDestinationFinalize(destination):
        raise MacOSError("PNG encoding failed", code=ErrorCode.AX_ERROR)
    ok, error = data.writeToFile_options_error_(str(path), NSDataWritingAtomic, None)
    if not ok:
        raise MacOSError(
            f"Could not write {path}: {error.localizedDescription()}",
            code=ErrorCode.AX_ERROR,
            details={"path": str(path)},
        )
