"""One-shot window, display, and screen-region capture through
ScreenCaptureKit.

``SCScreenshotManager`` renders at the requested output size, so a bounded
``see()`` never decodes a full Retina PNG only to shrink it. The image is
encoded exactly once, by ImageIO, straight into the output file.
"""

from __future__ import annotations

import math
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
    """One rendered window or screen area: the ``CGImage`` plus the state
    it was taken in. ``bounds`` is the captured area in screen points."""

    image: Any
    width: int
    height: int
    bounds: dict[str, float]
    on_screen: bool
    captured_at: float


@dataclass(frozen=True, slots=True)
class GrayPrint:
    """A window's look reduced for comparison: 8-bit gray pixels row by
    row, and the square points of window each pixel stands for."""

    width: int
    height: int
    pixels: bytes
    point_area: float


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


def _shareable_content(timeout: float) -> Any:
    """What ScreenCaptureKit can capture now: every display, and every
    window except the desktop's own, on screen or not."""
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
    return content


def shareable_window(window_id: int, *, timeout: float = CAPTURE_TIMEOUT) -> Any:
    """Return the ``SCWindow`` for a Core Graphics window number."""
    for window in _shareable_content(timeout).windows():
        if int(window.windowID()) == window_id:
            return window
    raise MacOSError(
        f"Window {window_id} is not capturable; it may have closed",
        code=ErrorCode.ELEMENT_UNKNOWN,
        details={"window_id": window_id},
    )


def displays() -> list[tuple[int, dict[str, float]]]:
    """Every active display, the main one first: its id and its frame in
    global screen points, top-left origin like window bounds."""
    error, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    if error:
        raise MacOSError(
            f"Could not list displays (Core Graphics error {error})",
            code=ErrorCode.AX_ERROR,
            details={"error": int(error)},
        )
    return [(int(display_id), _points(Quartz.CGDisplayBounds(display_id)))
            for display_id in ids[:count]]


def _points(rect: Any) -> dict[str, float]:
    return {
        "x": float(rect.origin.x),
        "y": float(rect.origin.y),
        "width": float(rect.size.width),
        "height": float(rect.size.height),
    }


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
    size: tuple[int, int] | None = None,
    timeout: float = CAPTURE_TIMEOUT,
) -> WindowCapture:
    """Render one window at most ``max_width`` x ``max_height`` pixels.

    ``None`` for either bound keeps the window's native pixel size on that
    axis; ``size`` asks for exactly that many pixels instead, so a later
    capture compares pixel for pixel with an earlier one. The window is
    captured on its own (``desktopIndependentWindow``):
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
    bounds = _points(content_filter.contentRect())
    natural_width = bounds["width"] * scale
    natural_height = bounds["height"] * scale
    if natural_width < 1 or natural_height < 1:
        raise MacOSError(
            f"Window {window_id} has no drawable area",
            code=ErrorCode.ELEMENT_UNKNOWN,
            details={"window_id": window_id, "bounds": bounds},
        )
    width, height = size or _output_size(natural_width, natural_height, max_width, max_height)

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


def capture_screen(
    display_id: int,
    area: dict[str, float],
    display_bounds: dict[str, float],
    *,
    max_width: int | None,
    max_height: int | None,
    hide_pid: int | None,
    timeout: float = CAPTURE_TIMEOUT,
) -> WindowCapture:
    """Render ``area`` (screen points inside the display at
    ``display_bounds``) as it shows on screen: every window there,
    composited, except those of ``hide_pid``."""
    content = _shareable_content(timeout)
    display = next(
        (item for item in content.displays() if int(item.displayID()) == display_id), None
    )
    if display is None:
        raise MacOSError(
            f"Display {display_id} is not capturable; it may have been disconnected",
            code=ErrorCode.ELEMENT_UNKNOWN,
            details={"display_id": display_id},
        )
    hidden = [
        window for window in content.windows()
        if hide_pid is not None
        and window.owningApplication() is not None
        and int(window.owningApplication().processID()) == hide_pid
    ]
    content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(
        display, hidden
    )
    scale = float(content_filter.pointPixelScale())
    width, height = _output_size(
        area["width"] * scale, area["height"] * scale, max_width, max_height
    )
    config = SCK.SCStreamConfiguration.alloc().init()
    config.setSourceRect_(Quartz.CGRectMake(
        area["x"] - display_bounds["x"], area["y"] - display_bounds["y"],
        area["width"], area["height"],
    ))
    config.setWidth_(width)
    config.setHeight_(height)
    config.setPixelFormat_(_PIXEL_FORMAT_BGRA)
    config.setCaptureResolution_(SCK.SCCaptureResolutionBest)
    config.setShowsCursor_(False)

    image, error = _await(
        lambda handler: (
            SCK.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(
                content_filter, config, handler
            )
        ),
        operation="Screen capture",
        timeout=timeout,
    )
    captured_at = time.time()
    if error is not None:
        raise _sck_error("Screen capture", error, display_id=display_id)
    if image is None:
        raise MacOSError(
            f"Screen capture returned no image for display {display_id}",
            code=ErrorCode.AX_ERROR,
            details={"display_id": display_id},
        )
    return WindowCapture(
        image=image,
        width=int(Quartz.CGImageGetWidth(image)),
        height=int(Quartz.CGImageGetHeight(image)),
        bounds=dict(area),
        on_screen=True,
        captured_at=captured_at,
    )


def gray_print(
    image: Any,
    *,
    width: int,
    height: int,
    points: tuple[float, float],
    crop: tuple[float, float, float, float] | None = None,
) -> GrayPrint:
    """Draw ``image``, or its ``crop`` (x, y, width, height in image pixels
    from the top left), into a ``width`` x ``height`` gray bitmap.

    ``points`` is the size of the drawn part in screen points. Each pixel
    averages the area it covers, so a thin line keeps its weight when
    shrunk instead of vanishing or flickering between captures.
    """
    if crop is not None:
        image = Quartz.CGImageCreateWithImageInRect(image, Quartz.CGRectMake(*crop))
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, width, Quartz.CGColorSpaceCreateDeviceGray(),
        Quartz.kCGImageAlphaNone,
    )
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    drawn = Quartz.CGBitmapContextCreateImage(context)
    row = int(Quartz.CGImageGetBytesPerRow(drawn))
    raw = bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(drawn)))
    pixels = raw if row == width else b"".join(
        raw[top:top + width] for top in range(0, row * height, row)
    )
    return GrayPrint(width, height, pixels, points[0] / width * points[1] / height)


def changed_area(before: GrayPrint, after: GrayPrint) -> float:
    """Square points of window that changed, each pixel weighted by how far
    its gray level moved; prints of different sizes differ everywhere."""
    if (before.width, before.height) != (after.width, after.height):
        return math.inf
    moved = sum(abs(old - new) for old, new in zip(before.pixels, after.pixels, strict=True))
    return moved / 255 * before.point_area


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
