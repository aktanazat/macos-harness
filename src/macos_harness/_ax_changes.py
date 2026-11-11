"""End an AX wait's rest the moment its app reports a change.

Between searches an AX wait rests. An observer on the app ends the rest
at the first accessibility notification the app posts: an element made
or destroyed, a value, title, selection or focus that moved, a window,
sheet or menu that opened. Some changes post none (an AppKit view added
to or hidden in its window), so a rest still ends after the wait's
interval.
"""

from __future__ import annotations

import time
from functools import cache
from typing import NamedTuple, Self

import ApplicationServices as AS
import objc

_NOTIFICATIONS = (
    "AXCreated",
    "AXUIElementDestroyed",
    "AXValueChanged",
    "AXTitleChanged",
    "AXLayoutChanged",
    "AXFocusedUIElementChanged",
    "AXFocusedWindowChanged",
    "AXWindowCreated",
    "AXSheetCreated",
    "AXMenuOpened",
    "AXMenuClosed",
    "AXSelectedChildrenChanged",
    "AXSelectedRowsChanged",
    "AXRowCountChanged",
)
# A run loop mode only the observer's source runs in, so a rest services
# nothing else the calling thread has scheduled.
_MODE = "com.macos-harness.app-changes"
# How long a hung app can hold up adding or removing the observer. A live
# app answers all of them in under a millisecond; a stopped one holds each
# call for the system's messaging timeout, 1.5 s in a macOS 27 VM.
_MESSAGING_TIMEOUT = 0.25


@cache
def _noted() -> object:
    """The observer's callback. Handling the source is the whole message:
    `AppChanges.rest` learns of a change from the run loop's return."""

    @objc.callbackFor(AS.AXObserverCreate)
    def noted(observer: object, element: object, notification: str, refcon: object) -> None:
        return None

    return noted


class _Watch(NamedTuple):
    observer: object
    app: object
    names: tuple[str, ...]
    loop: object
    source: object


class AppChanges:
    """The changes one app reports while a wait rests.

    `follow` names the app by pid. Without one, or for a process that
    takes no observer (one gone, one with no accessibility, or a caller
    without Accessibility trust), `rest` just sleeps. ``interval`` is the
    wait's. Use from one thread: the observer runs on that thread's run
    loop.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._pid: int | None = None
        self._watch: _Watch | None = None
        self._busy = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def follow(self, pid: int | None) -> None:
        """Watch the app ``pid`` from now on; None keeps the current one."""
        if pid is None or pid == self._pid:
            return
        self.close()
        self._pid = pid
        error, observer = AS.AXObserverCreate(pid, _noted(), None)
        if error != AS.kAXErrorSuccess:
            return
        app = AS.AXUIElementCreateApplication(pid)
        if AS.AXUIElementSetMessagingTimeout(app, _MESSAGING_TIMEOUT) != AS.kAXErrorSuccess:
            return
        names: list[str] = []
        for name in _NOTIFICATIONS:
            added = AS.AXObserverAddNotification(observer, app, name, None)
            if added == AS.kAXErrorSuccess:
                names.append(name)
            elif added != AS.kAXErrorNotificationUnsupported:
                # A process that cannot take one notification takes none.
                break
        if not names:
            return
        loop = AS.CFRunLoopGetCurrent()
        source = AS.AXObserverGetRunLoopSource(observer)
        AS.CFRunLoopAddSource(loop, source, _MODE)
        self._watch = _Watch(observer, app, tuple(names), loop, source)

    def rest(self, seconds: float, *, at_least: float) -> None:
        """Sleep ``seconds``, or end sooner, though not before ``at_least``,
        once the app has reported a change since the last rest ended. After
        a rest the app changed in, a rest also lasts half the interval: an
        app that never stops changing is searched at most twice per
        interval."""
        if self._watch is None:
            time.sleep(seconds)
            return
        began = time.monotonic()
        end = began + seconds
        floor = began + min(max(at_least, self._interval / 2) if self._busy else at_least, seconds)
        changed = False
        while (now := time.monotonic()) < floor:
            if AS.CFRunLoopRunInMode(_MODE, floor - now, True) == AS.kCFRunLoopRunHandledSource:
                changed = True
        if not changed and (now := time.monotonic()) < end:
            changed = AS.CFRunLoopRunInMode(_MODE, end - now, True) == AS.kCFRunLoopRunHandledSource
        # One burst of notifications wakes one search.
        woke = changed
        while woke:
            woke = AS.CFRunLoopRunInMode(_MODE, 0, True) == AS.kCFRunLoopRunHandledSource
        self._busy = changed

    def close(self) -> None:
        """Stop watching; a later `follow` may watch again."""
        watch, self._watch, self._pid, self._busy = self._watch, None, None, False
        if watch is None:
            return
        AS.CFRunLoopRemoveSource(watch.loop, watch.source, _MODE)
        for name in watch.names:
            if AS.AXObserverRemoveNotification(watch.observer, watch.app, name) != AS.kAXErrorSuccess:
                break
