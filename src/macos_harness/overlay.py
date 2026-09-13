"""A tiny click-through AppKit overlay for the harness pointer.

The helper draws the system arrow cursor at the user's cursor size, on
every Space, above ordinary windows, and hides itself after a few idle
seconds so a finished action never leaves a stray arrow on screen.
"""

from __future__ import annotations

import atexit
import json
import subprocess
import sys
import threading
import time
from typing import Any, TextIO

from .pointer import POINTER_HOTSPOT, POINTER_PRESS_SCALE

# Seconds without a move, show, or click before the helper hides the arrow.
IDLE_HIDE_SECONDS = 3.0


class LivePointerOverlay:
    """Send pointer updates to a disposable AppKit helper process.

    ``hide()`` is sticky: nothing is drawn, and no helper is spawned, until
    ``show()`` re-enables the pointer. ``visible`` mirrors the helper's own
    idle timer, so it reads ``False`` once the arrow has faded on screen.
    """

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._enabled = True
        self._last_shown: float | None = None
        atexit.register(self.close)

    @property
    def visible(self) -> bool:
        return (
            self._enabled
            and self._last_shown is not None
            and time.monotonic() - self._last_shown < IDLE_HIDE_SECONDS
        )

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def move(self, x: float, y: float, *, duration: float = 0.16) -> None:
        if not self._enabled:
            return
        self._last_shown = time.monotonic()
        self._send(
            {
                "cmd": "move",
                "x": float(x),
                "y": float(y),
                "duration": max(0.0, float(duration)),
            }
        )

    def show(self, x: float, y: float) -> None:
        self._enabled = True
        self._last_shown = time.monotonic()
        self._send({"cmd": "show", "x": float(x), "y": float(y)})

    def hide(self) -> None:
        self._enabled = False
        self._last_shown = None
        if self.running:
            self._send({"cmd": "hide"}, start=False)

    def click(self) -> None:
        if not self._enabled:
            return
        self._last_shown = time.monotonic()
        self._send({"cmd": "click"})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"cmd":"quit"}\n')
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass

    def _start(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-m", "macos_harness.overlay", "--helper"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._process = process
        return process

    def _send(self, payload: dict[str, Any], *, start: bool = True) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            if not start:
                return
            process = self._start()
        stream = process.stdin
        if stream is None:
            return
        try:
            stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
            stream.flush()
        except (BrokenPipeError, OSError):
            if not start:
                return
            self._process = None
            retry = self._start()
            assert retry.stdin is not None
            retry.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            retry.stdin.flush()


def _read_commands(stream: TextIO, controller: Any) -> None:
    for line in stream:
        try:
            command = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        controller.performSelectorOnMainThread_withObject_waitUntilDone_(
            "handleCommand:", command, False
        )
    controller.performSelectorOnMainThread_withObject_waitUntilDone_(
        "handleCommand:", {"cmd": "quit"}, False
    )


def _cursor_magnification() -> float:
    """The user's pointer size from Accessibility settings, 1x to 4x."""
    from CoreFoundation import CFPreferencesCopyAppValue

    value = CFPreferencesCopyAppValue(
        "mouseDriverCursorSize", "com.apple.universalaccess"
    )
    try:
        return min(4.0, max(1.0, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _run_helper() -> None:  # pragma: no cover - exercised by the live smoke test
    import AppKit
    import objc
    import Quartz

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    cursor = AppKit.NSCursor.arrowCursor()
    cursor_image = cursor.image()
    magnification = _cursor_magnification()
    size = cursor_image.size()
    width = float(size.width) * magnification
    height = float(size.height) * magnification
    hot_x = POINTER_HOTSPOT[0] * magnification
    hot_y = POINTER_HOTSPOT[1] * magnification
    reduce_motion = (
        AppKit.NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
    )

    class PointerView(AppKit.NSView):
        pressed = False

        def isOpaque(self) -> bool:
            return False

        def drawRect_(self, rect: Any) -> None:
            scale = POINTER_PRESS_SCALE if self.pressed else 1.0
            # View coordinates run bottom-up; the hotspot sits `hot_y`
            # below the top edge. Shrink the pressed arrow around it so
            # the tip stays put.
            anchor_x = hot_x
            anchor_y = height - hot_y
            target = AppKit.NSMakeRect(
                anchor_x * (1.0 - scale),
                anchor_y * (1.0 - scale),
                width * scale,
                height * scale,
            )
            cursor_image.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                target,
                AppKit.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver,
                1.0,
                True,
                None,
            )

        def endPress_(self, sender: Any) -> None:
            self.pressed = False
            self.setNeedsDisplay_(True)

    class OverlayController(AppKit.NSObject):
        def init(self) -> Any:
            controller = objc.super(OverlayController, self).init()
            if controller is None:
                return None
            frame = AppKit.NSMakeRect(-100, -100, width, height)
            style = (
                AppKit.NSWindowStyleMaskBorderless
                | AppKit.NSWindowStyleMaskNonactivatingPanel
            )
            controller.panel = (
                AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                    frame, style, AppKit.NSBackingStoreBuffered, False
                )
            )
            controller.view = PointerView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, width, height)
            )
            controller.panel.setContentView_(controller.view)
            controller.panel.setTitle_("macOS Harness Pointer")
            controller.panel.setOpaque_(False)
            controller.panel.setBackgroundColor_(AppKit.NSColor.clearColor())
            controller.panel.setHasShadow_(False)
            controller.panel.setIgnoresMouseEvents_(True)
            controller.panel.setHidesOnDeactivate_(False)
            controller.panel.setReleasedWhenClosed_(False)
            controller.panel.setLevel_(AppKit.NSStatusWindowLevel)
            controller.panel.setCollectionBehavior_(
                AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                | AppKit.NSWindowCollectionBehaviorCanJoinAllApplications
                | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                | AppKit.NSWindowCollectionBehaviorTransient
                | AppKit.NSWindowCollectionBehaviorIgnoresCycle
                | AppKit.NSWindowCollectionBehaviorStationary
            )
            controller.shown = False
            controller.animation_timer = None
            controller.animation_started = 0.0
            controller.animation_duration = 0.0
            controller.animation_start = (0.0, 0.0)
            controller.animation_end = (0.0, 0.0)
            return controller

        @staticmethod
        @objc.python_method
        def _origin(x: float, y: float) -> Any | None:
            for screen in AppKit.NSScreen.screens():
                display_id = int(screen.deviceDescription()["NSScreenNumber"])
                cg_bounds = Quartz.CGDisplayBounds(display_id)
                min_x = float(cg_bounds.origin.x)
                min_y = float(cg_bounds.origin.y)
                max_x = min_x + float(cg_bounds.size.width)
                max_y = min_y + float(cg_bounds.size.height)
                if min_x <= x < max_x and min_y <= y < max_y:
                    local_x = x - min_x
                    local_y = y - min_y
                    ns_frame = screen.frame()
                    return AppKit.NSMakePoint(
                        float(ns_frame.origin.x) + local_x - hot_x,
                        float(ns_frame.origin.y)
                        + float(ns_frame.size.height)
                        - local_y
                        - (height - hot_y),
                    )
            return None

        @objc.python_method
        def _stop_animation(self) -> None:
            if self.animation_timer is not None:
                self.animation_timer.invalidate()
                self.animation_timer = None

        @objc.python_method
        def _touch(self) -> None:
            AppKit.NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
                self, "idleHide:", None
            )
            self.performSelector_withObject_afterDelay_(
                "idleHide:", None, IDLE_HIDE_SECONDS
            )

        @objc.python_method
        def _hide(self) -> None:
            AppKit.NSObject.cancelPreviousPerformRequestsWithTarget_selector_object_(
                self, "idleHide:", None
            )
            self._stop_animation()
            self.panel.orderOut_(None)
            self.shown = False

        def idleHide_(self, sender: Any) -> None:
            self._hide()

        def tick_(self, timer: Any) -> None:
            elapsed = time.monotonic() - self.animation_started
            progress = min(1.0, elapsed / self.animation_duration)
            eased = 1.0 - (1.0 - progress) ** 3
            start_x, start_y = self.animation_start
            end_x, end_y = self.animation_end
            self.panel.setFrameOrigin_(
                AppKit.NSMakePoint(
                    start_x + (end_x - start_x) * eased,
                    start_y + (end_y - start_y) * eased,
                )
            )
            if progress >= 1.0:
                self._stop_animation()

        @objc.python_method
        def _place(self, command: dict[str, Any], *, animate: bool) -> None:
            origin = self._origin(float(command["x"]), float(command["y"]))
            if origin is None:
                self._hide()
                return
            duration = max(0.0, float(command.get("duration", 0.0)))
            if animate and self.shown and duration > 0 and not reduce_motion:
                self._stop_animation()
                current = self.panel.frame().origin
                self.animation_start = (float(current.x), float(current.y))
                self.animation_end = (float(origin.x), float(origin.y))
                self.animation_started = time.monotonic()
                self.animation_duration = duration
                self.animation_timer = AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / 60.0, self, "tick:", None, True
                )
            else:
                self._stop_animation()
                self.panel.setFrameOrigin_(origin)
            self.panel.orderFrontRegardless()
            self.shown = True
            self._touch()

        def handleCommand_(self, command: dict[str, Any]) -> None:
            action = command.get("cmd")
            if action in {"move", "show"}:
                self._place(command, animate=action == "move")
            elif action == "hide":
                self._hide()
            elif action == "click" and self.shown:
                self.view.pressed = True
                self.view.setNeedsDisplay_(True)
                self.view.performSelector_withObject_afterDelay_(
                    "endPress:", None, 0.11
                )
                self._touch()
            elif action == "quit":
                self._hide()
                AppKit.NSApplication.sharedApplication().terminate_(None)

    controller = OverlayController.alloc().init()
    reader = threading.Thread(
        target=_read_commands, args=(sys.stdin, controller), daemon=True
    )
    reader.start()
    app.run()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--helper"]:
        print("macos_harness.overlay is an internal helper", file=sys.stderr)
        return 2
    _run_helper()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
