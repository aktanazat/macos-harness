"""Geometry for the pointer drawn onto a captured screenshot.

The polygon traces the black core of the macOS arrow cursor in its 1x
28 x 40 point canvas, hotspot included, so the annotation matches the
live overlay (which draws the system cursor image itself). The overlay
helper imports the hotspot and press scale from here too, so this module
stays free of AppKit and Quartz.
"""

from __future__ import annotations

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
