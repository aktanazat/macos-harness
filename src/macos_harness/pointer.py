"""Shared geometry and named-agent artwork for live and captured pointers."""

from __future__ import annotations

import colorsys
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import ErrorCode, MacOSError

if TYPE_CHECKING:
    import AppKit

POINTER_HOTSPOT = (5.0, 5.0)
POINTER_POINTS = (
    (5.0, 5.0),  # tip
    (5.0, 16.5),  # left base
    (8.0, 14.5),  # notch
    (10.0, 19.25),  # tail, bottom left
    (11.5, 18.75),  # tail, bottom right
    (10.0, 13.75),  # tail, top
    (13.0, 13.0),  # shoulder
)
POINTER_PRESS_SCALE = 0.9


def pointer_points(*, pressed: bool = False) -> tuple[tuple[float, float], ...]:
    """Return arrow points in top-left coordinates, scaled around its hotspot."""
    scale = POINTER_PRESS_SCALE if pressed else 1.0
    hot_x, hot_y = POINTER_HOTSPOT
    return tuple(
        (hot_x + (x - hot_x) * scale, hot_y + (y - hot_y) * scale)
        for x, y in POINTER_POINTS
    )


@dataclass(frozen=True, slots=True)
class PointerIdentity:
    agent_id: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip() or len(self.agent_id) > 64 or not self.agent_id.isprintable():
            raise MacOSError(
                "agent_id must contain 1–64 printable characters",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "agent_id"},
            )

    @property
    def color(self) -> tuple[float, float, float]:
        digest = hashlib.sha256(self.agent_id.encode("utf-8")).digest()
        hue = int.from_bytes(digest[:3]) / 0x1000000
        return colorsys.hsv_to_rgb(hue, 0.72, 0.58)

    def image(self) -> AppKit.NSImage:
        import AppKit

        text = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            self.agent_id,
            {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(
                    11, AppKit.NSFontWeightMedium
                ),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor(),
            },
        )
        text_size = text.size()
        label_width = float(text_size.width) + 16
        label_height = float(text_size.height) + 8
        image = AppKit.NSImage.alloc().initWithSize_((label_width + 14, label_height + 28))
        image.lockFocusFlipped_(True)
        try:
            arrow = AppKit.NSBezierPath.bezierPath()
            arrow.moveToPoint_(POINTER_POINTS[0])
            for point in POINTER_POINTS[1:]:
                arrow.lineToPoint_(point)
            arrow.closePath()
            arrow.setLineJoinStyle_(AppKit.NSRoundLineJoinStyle)
            arrow.setLineWidth_(2)
            AppKit.NSColor.whiteColor().setStroke()
            arrow.stroke()
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(*self.color, 1).setFill()
            arrow.fill()
            label = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                AppKit.NSMakeRect(12, 25, label_width, label_height), 6, 6
            )
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.1, 0.1, 0.1, 0.94).setFill()
            label.fill()
            text.drawAtPoint_((20, 29))
        finally:
            image.unlockFocus()
        return image
