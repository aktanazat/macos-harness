"""One-shot window, display, and screen-region capture through
ScreenCaptureKit.

``SCScreenshotManager`` renders at the requested output size, so a bounded
``see()`` never decodes a full Retina PNG only to shrink it. The image is
encoded exactly once, by ImageIO, straight into the output file.
"""

from __future__ import annotations

import fcntl
import math
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._paths import user_temp_dir
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
_CAPTURE_LOCK = "macos-harness-capture.lock"
_SCREENCAPTURE = "/usr/sbin/screencapture"
_capture_owner: int | None = None
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


def _owns_capture() -> bool:
    """Whether this process is the one that renders through
    ScreenCaptureKit, held from its first capture until it exits.

    macOS's capture service tells its clients apart by executable path, so
    two live processes of one Python are one client to it: once both had
    captured, every capture either asked for went unanswered, 30 seconds
    as surely as 5, until one exited. Another process captures through
    ``screencapture``, which ran beside a capturing harness 6 times in 6."""
    global _capture_owner
    if _capture_owner is not None:
        return True
    fd = os.open(
        os.path.join(user_temp_dir(), _CAPTURE_LOCK),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    _capture_owner = fd
    return True


def _screencapture(arguments: list[str], operation: str, timeout: float) -> object:
    """The image ``screencapture`` takes with ``arguments``, at full pixel size."""
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "capture.png")
        try:
            subprocess.run(
                [_SCREENCAPTURE, "-x", "-t", "png", *arguments, path],
                check=True, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise MacOSError(
                f"{operation} did not answer within {timeout:g}s",
                code=ErrorCode.TIMEOUT,
                details={"operation": operation, "timeout": timeout, "via": "screencapture"},
            ) from None
        except subprocess.CalledProcessError as error:
            raise MacOSError(
                f"{operation} failed: screencapture said {error.stderr.strip() or error.returncode}",
                code=ErrorCode.AX_ERROR,
                details={"operation": operation, "via": "screencapture"},
            ) from None
        encoded = Path(path).read_bytes() if os.path.exists(path) else b""
    source = Quartz.CGImageSourceCreateWithData(encoded, None)
    image = None if source is None else Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise MacOSError(
            f"{operation} wrote no image",
            code=ErrorCode.AX_ERROR,
            details={"operation": operation, "via": "screencapture"},
        )
    return image


def _resized(image: object, width: int, height: int) -> object:
    """``image`` drawn into exactly ``width`` x ``height`` pixels."""
    if (Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image)) == (width, height):
        return image
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0,
        Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    Quartz.CGContextSetInterpolationQuality(context, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(context, Quartz.CGRectMake(0, 0, width, height), image)
    return Quartz.CGBitmapContextCreateImage(context)


def _rendered(
    image: object,
    bounds: dict[str, float],
    *,
    max_width: int | None,
    max_height: int | None,
    size: tuple[int, int] | None,
    on_screen: bool,
) -> WindowCapture:
    """A ``screencapture`` image of ``bounds`` fitted to the size asked for."""
    width, height = size or _output_size(
        Quartz.CGImageGetWidth(image), Quartz.CGImageGetHeight(image), max_width, max_height
    )
    return WindowCapture(
        image=_resized(image, width, height), width=width, height=height,
        bounds=bounds, on_screen=on_screen, captured_at=time.time(),
    )


def reframed(
    capture: WindowCapture,
    area: Mapping[str, float],
    *,
    zoom: float,
    max_width: int | None,
    max_height: int | None,
) -> WindowCapture:
    """The part of ``capture`` that shows ``area`` (screen points inside
    its bounds), drawn at ``zoom`` times the capture's pixels per point,
    then shrunk to fit ``max_width`` x ``max_height``. ``bounds`` is the
    area the whole pixels cut cover, under a pixel wider than ``area``."""
    bounds = capture.bounds
    scale_x = capture.width / bounds["width"]
    scale_y = capture.height / bounds["height"]
    left = max(0, math.floor((area["x"] - bounds["x"]) * scale_x))
    top = max(0, math.floor((area["y"] - bounds["y"]) * scale_y))
    right = min(capture.width, math.ceil((area["x"] + area["width"] - bounds["x"]) * scale_x))
    bottom = min(
        capture.height, math.ceil((area["y"] + area["height"] - bounds["y"]) * scale_y)
    )
    cut = Quartz.CGImageCreateWithImageInRect(
        capture.image, Quartz.CGRectMake(left, top, right - left, bottom - top)
    )
    width, height = _output_size(
        (right - left) * zoom, (bottom - top) * zoom, max_width, max_height
    )
    return WindowCapture(
        image=_resized(cut, width, height),
        width=width,
        height=height,
        bounds={
            "x": bounds["x"] + left / scale_x,
            "y": bounds["y"] + top / scale_y,
            "width": (right - left) / scale_x,
            "height": (bottom - top) / scale_y,
        },
        on_screen=capture.on_screen,
        captured_at=capture.captured_at,
    )


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


def _shareable_content(timeout: float) -> object:
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


class WindowCatalog:
    """ScreenCaptureKit's list of what it can capture, kept for the next
    capture of the same window while the window server's own list, read in
    about 0.3ms where ScreenCaptureKit takes 16ms, shows nothing that
    capture reads has changed: the window's frame and whether it is on
    screen, the frames of its app's on-screen windows, and the displays."""

    def __init__(self) -> None:
        self._kept: tuple[int, tuple[object, ...], object] | None = None

    def content(self, window_id: int, timeout: float) -> object:
        _require_screencapturekit()
        layout = _layout(window_id)
        if self._kept is not None and self._kept[:2] == (window_id, layout):
            return self._kept[2]
        content = _shareable_content(timeout)
        # A window that moved while the list was taken may be listed where
        # it was or where it went, so that list serves this capture only.
        self._kept = (window_id, layout, content) if _layout(window_id) == layout else None
        return content

    def forget(self) -> None:
        self._kept = None


def _layout(window_id: int) -> tuple[object, ...]:
    window = next(iter(Quartz.CGWindowListCreateDescriptionFromArray([window_id]) or ()), {})
    pid = window.get(Quartz.kCGWindowOwnerPID)
    shown = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    ) or ()
    return (
        _frame(window.get(Quartz.kCGWindowBounds)),
        bool(window.get(Quartz.kCGWindowIsOnscreen, False)),
        sorted(
            (int(each[Quartz.kCGWindowNumber]), _frame(each.get(Quartz.kCGWindowBounds)))
            for each in shown
            if pid is not None and each.get(Quartz.kCGWindowOwnerPID) == pid
        ),
        displays(),
    )


def _frame(bounds: Mapping[str, float] | None) -> tuple[float, ...]:
    return tuple(float((bounds or {}).get(key, 0)) for key in ("X", "Y", "Width", "Height"))


def _window_in(windows: object, window_id: int) -> object:
    """The ``SCWindow`` for a Core Graphics window number."""
    for window in windows:
        if int(window.windowID()) == window_id:
            return window
    raise MacOSError(
        f"Window {window_id} is not capturable; it may have closed",
        code=ErrorCode.ELEMENT_UNKNOWN,
        details={"window_id": window_id},
    )


def _owner_pid(window: object) -> int | None:
    app = window.owningApplication()
    return None if app is None else int(app.processID())


def _crop_display(window: object, windows: object, displays: object) -> object | None:
    """The display to cut ``window`` out of, or ``None`` to render the
    window by itself.

    Rendered by itself, a window brings the sheets, popovers, and tips
    attached to it, and ScreenCaptureKit shrinks the whole group into the
    window's pixels: a tip over Calculator's top edge halved it. So when
    another on-screen window of the same app overlaps ``window`` and reaches
    past its edge, the window is cut out of the display it lies on instead,
    with what is attached drawn where it sits and the rest cut off. A window
    not wholly on one display is still rendered by itself.
    """
    frame = window.frame()
    pid = _owner_pid(window)
    overhung = pid is not None and any(
        other.isOnScreen()
        and Quartz.CGRectIntersectsRect(other.frame(), frame)
        and not Quartz.CGRectContainsRect(frame, other.frame())
        and _owner_pid(other) == pid
        for other in windows
    )
    if not overhung:
        return None
    return next(
        (display for display in displays if Quartz.CGRectContainsRect(display.frame(), frame)),
        None,
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


def _points(rect: object) -> dict[str, float]:
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


def _window_configuration(width: int, height: int) -> object:
    """How a window renders by itself: into exactly ``width`` x ``height``
    pixels, shrunk to fit but never enlarged, with no shadow and no cursor."""
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
    return config


def _crop_configuration(frame: object, screen: object, width: int, height: int) -> object:
    """How a window at ``frame`` is cut out of the display at ``screen``
    (both in global points): into exactly ``width`` x ``height`` pixels,
    with no shadow and no cursor."""
    config = SCK.SCStreamConfiguration.alloc().init()
    config.setSourceRect_(Quartz.CGRectOffset(frame, -screen.origin.x, -screen.origin.y))
    config.setWidth_(width)
    config.setHeight_(height)
    config.setPixelFormat_(_PIXEL_FORMAT_BGRA)
    config.setCaptureResolution_(SCK.SCCaptureResolutionBest)
    config.setShowsCursor_(False)
    config.setIgnoreShadowsDisplay_(True)
    config.setShouldBeOpaque_(False)
    return config


def capture_window(
    window_id: int,
    *,
    max_width: int | None,
    max_height: int | None,
    catalog: WindowCatalog,
    size: tuple[int, int] | None = None,
    timeout: float = CAPTURE_TIMEOUT,
) -> WindowCapture:
    """Render one window at most ``max_width`` x ``max_height`` pixels.

    ``None`` for either bound keeps the window's native pixel size on that
    axis; ``size`` asks for exactly that many pixels instead, so a later
    capture compares pixel for pixel with an earlier one. The window is
    captured on its own: no shadow and no other window, but any sheet,
    popover, or tip attached to it drawn where it sits over the window. A
    window rendered by itself (``desktopIndependentWindow``) is not clipped
    at the screen edge, and an off-screen one still renders, but from
    whatever the app last drew -- apps do not repaint hidden content, so
    ``on_screen`` is the caller's only freshness signal. ``catalog`` lends
    the window list of an earlier capture of the window while the window,
    its app's other windows, and the displays stand where they did.

    A process that does not own capture (see `_owns_capture`) renders
    the window through ``screencapture`` instead, by itself with no
    shadow, so an attached sheet is cut off where it overhangs.
    """
    if not _owns_capture():
        described = next(
            iter(Quartz.CGWindowListCreateDescriptionFromArray([window_id]) or ()), None
        )
        if described is None:
            raise MacOSError(
                f"Window {window_id} is not capturable; it may have closed",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"window_id": window_id},
            )
        x, y, width, height = _frame(described.get(Quartz.kCGWindowBounds))
        return _rendered(
            _screencapture(["-o", "-l", str(window_id)], "Window capture", timeout),
            {"x": x, "y": y, "width": width, "height": height},
            max_width=max_width, max_height=max_height, size=size,
            on_screen=bool(described.get(Quartz.kCGWindowIsOnscreen, False)),
        )
    content = catalog.content(window_id, timeout)
    windows = content.windows()
    window = _window_in(windows, window_id)
    display = _crop_display(window, windows, content.displays())
    content_filter = (
        SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        if display is None
        else SCK.SCContentFilter.alloc().initWithDisplay_includingWindows_(display, [window])
    )
    scale = float(content_filter.pointPixelScale())
    bounds = _points(window.frame())
    natural_width = bounds["width"] * scale
    natural_height = bounds["height"] * scale
    if natural_width < 1 or natural_height < 1:
        raise MacOSError(
            f"Window {window_id} has no drawable area",
            code=ErrorCode.ELEMENT_UNKNOWN,
            details={"window_id": window_id, "bounds": bounds},
        )
    width, height = size or _output_size(natural_width, natural_height, max_width, max_height)
    config = (
        _window_configuration(width, height)
        if display is None
        else _crop_configuration(window.frame(), display.frame(), width, height)
    )

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
        catalog.forget()
        raise _sck_error("Window capture", error, window_id=window_id)
    if image is None:
        catalog.forget()
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
    if not _owns_capture():
        return _rendered(
            _screencapture(
                ["-R", ",".join(f"{area[key]:g}" for key in ("x", "y", "width", "height"))],
                "Screen capture", timeout,
            ),
            dict(area), max_width=max_width, max_height=max_height, size=None, on_screen=True,
        )
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
    image: object,
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
