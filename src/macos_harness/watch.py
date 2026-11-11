"""Learn a route by watching the person at this Mac do the task once.

A listen-only event tap sees their clicks and keys and passes each one on
unchanged; accessibility notifications report menus opening and closing
and where keyboard focus goes. A click is kept as the control under it, a
click on a menu bar item as a menu step, keys as key and type steps, and
nothing typed while a secure text field has focus: the route holds a
``secret`` step there instead. Each type step checks that its field has
keyboard focus, and so does the step before it.
The watch saves nothing once it falls behind the person, since what it
reads of the app may then show their later input, nor when keys came as
a secure text field took focus before the app showed it there: an app
whose accessibility trails its input, such as Chrome, may have put them
in the password.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

import ApplicationServices as AS
import objc

from .errors import AccessibilityPermissionError, ErrorCode, MacOSError
from .macos import _KEYCODES, _SECURE_SUBROLE, MacOS
from .ops import _MAX_TYPE_CHARS, _Deadline
from .receipts import (
    Equals,
    ErrorPayload,
    JSONValue,
    OperationError,
    Postcondition,
    Present,
    Receipt,
)
from .routes import (
    _condition,
    _invalid,
    _Key,
    _Locator,
    _Menu,
    _Press,
    _Route,
    _Secret,
    _Set,
    _Step,
    _step_json,
    _Type,
)

if TYPE_CHECKING:
    from .macos import _AppIdentity
    from .routes import Routes

# The watch's sources run in a mode of their own: reading the AX tree turns
# this thread's default-mode run loop to take in workspace changes, which
# would hand over the next event while this one is still being read.
_MODE = "com.github.macos-harness.watch"
_TURN_S = 0.1
_SAMPLE_S = 0.01
# The entry must hold when the watch starts and the goal when it ends; the
# operations' own default wait covers an app still drawing either.
_CHECK_S = 5.0
_CLICK_SLOP = 4.0
_CLIMB = 6
_MAX_STEPS = 64
_STOP_KEY = _KEYCODES["."]
_STOP_FLAGS = AS.kCGEventFlagMaskControl | AS.kCGEventFlagMaskAlternate | AS.kCGEventFlagMaskCommand
# The session names the process that turned on secure keyboard entry; while
# it is on, no event tap sees a key, the stop hotkey included.
_SECURE_INPUT = "kCGSSessionSecureInputPID"
_MODIFIERS = (
    ("cmd", AS.kCGEventFlagMaskCommand),
    ("ctrl", AS.kCGEventFlagMaskControl),
    ("alt", AS.kCGEventFlagMaskAlternate),
    ("shift", AS.kCGEventFlagMaskShift),
)
_KEY_NAMES = {code: name for name, code in reversed(_KEYCODES.items())}
_EDIT_KEYS = frozenset(_KEYCODES[name] for name in (
    "return", "tab", "escape", "backspace", "forward_delete", "home", "end",
    "pageup", "pagedown", "left", "right", "up", "down",
))
_BACKSPACE = _KEYCODES["backspace"]
_MENU_KEYS = frozenset(_KEYCODES[name] for name in ("escape", "left", "right", "up", "down"))
_FOCUS_KEYS = frozenset({"tab", "return", "escape"})
_FIELDS = frozenset({"AXTextField", "AXTextArea", "AXComboBox"})
_ADJUSTERS = frozenset({"AXSlider", "AXIncrementor"})
_MENU_PARTS = frozenset({"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"})
_TOPS = frozenset({"AXWindow", "AXSheet", "AXApplication"})
# Calculator on macOS 27 posted AXMenuItemSelected for a key equivalent and
# not for a pick made by clicking, so picks are read from the click itself.
_NOTICES = ("AXMenuOpened", "AXMenuClosed", "AXFocusedUIElementChanged")
_BEHIND = ("the watch fell behind the person's clicks and keys, so it could not tell for sure what "
           "each one went to; do the task again a little slower")
_UNSURE_SECRET = ("the app was slow to show which field had focus, so keys typed then may have gone into "
                  "another field, such as a password field; do the task again and pause a moment after "
                  "Tab or a click")

_Verb = Literal["press", "focus", "select", "key", "type", "menu", "secret"]
_Kind = Literal["press", "focus", "select", "item", "menu", "adjust"]
_Stop = Literal["hotkey", "timeout", "stop"]
# What the watch saw since the person last moved focus: keys it kept as
# text, or a secure field taking focus. ``_since_at`` holds when, on the
# event clock, the person moved focus or the first of those keys came.
# Keys kept after Tab or Return whose read still showed the field from
# before it may have gone where it moved focus: ``_unsure`` holds that
# field and its value at that key, and the person's next move or key in
# another field must find the value changed.
_Since = Literal["moved", "typed", "secret"]
# The bindings hand over accessibility elements and events as opaque
# Core Foundation objects, read only through ApplicationServices calls.
_Element: TypeAlias = object
_Event: TypeAlias = object


@dataclass(slots=True)
class _Seen:
    """One thing the person did, held until the next shows what to check
    after it: the control that next action went to, or this one still there."""

    verb: _Verb
    target: _Locator | None
    text: str
    anchor: _Locator | None
    after: _Locator | None = None

    def check(self, following: _Seen) -> Postcondition | None:
        """What to check once this is done: before typing, that the field
        typed into has keyboard focus; else what ``following`` went to."""
        if following.verb == "type" and following.target is not None:
            return _focused(following.target)
        return None if self.after is None else Present(**self.after.query())

    def step(self, expect: Postcondition | None, at: str) -> _Step:
        match self.verb, self.target:
            case "secret", _Locator() as target:
                return _Secret(target)
            case "type", _Locator() as target:
                return _Type(self.text, _focused(target))
            case "focus", _Locator() as target:
                return _Set(target, True, "AXFocused", expect)
            case "select", _Locator() as target:
                return _Set(target, True, "AXSelected", expect)
            case "press", _Locator() as target if expect is not None:
                return _Press(target, expect)
            case "key", None if expect is not None:
                return _Key(self.text, expect)
            case "menu", None if expect is not None:
                return _Menu(self.text, expect)
        raise _invalid(at, f"nothing the route could check was seen after this {self.verb}")


@dataclass(frozen=True, slots=True)
class WatchResult:
    """What a watch saved, or why it saved nothing."""

    name: str
    status: Literal["saved", "unsaved"]
    stopped_by: _Stop
    steps: tuple[dict[str, JSONValue], ...]
    skipped: tuple[dict[str, JSONValue], ...]
    path: str | None = None
    check: Receipt | None = None
    error: ErrorPayload | None = None

    def to_json(self) -> dict[str, JSONValue]:
        return {"name": self.name, "status": self.status, "stopped_by": self.stopped_by,
                "path": self.path, "steps": list(self.steps), "skipped": list(self.skipped),
                "check": None if self.check is None else self.check.to_json(),
                "error": self.error}


def _listen_error() -> AccessibilityPermissionError:
    return AccessibilityPermissionError(
        "macOS refused to let the watch listen to the person's keys. Grant Input Monitoring "
        "to the terminal or agent host in System Settings → Privacy & Security → Input "
        "Monitoring, then start it again.",
        details={"permission": "input_monitoring"},
    )


def _pid(element: _Element) -> int | None:
    error, pid = AS.AXUIElementGetPid(element, None)
    return int(pid) if error == 0 else None


def _point(event: _Event) -> tuple[float, float]:
    location = AS.CGEventGetLocation(event)
    return float(location.x), float(location.y)


def _focused(target: _Locator) -> Equals:
    return Equals(**target.query(), attribute="AXFocused", value=True)


def _secure_input_holder() -> int | None:
    holder = (AS.CGSessionCopyCurrentDictionary() or {}).get(_SECURE_INPUT)
    return None if holder is None else int(holder)


class _Watcher:
    """The tap, the observer and what they saw, all on one thread of their
    own, with a second that samples secure keyboard entry; the caller reads
    the results only once both have ended."""

    def __init__(self, pid: int, deadline: _Deadline) -> None:
        self._pid = pid
        self._deadline = deadline
        self._app = AS.AXUIElementCreateApplication(pid)
        self._system = AS.AXUIElementCreateSystemWide()
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.ready = threading.Event()
        self.seen: list[_Seen] = []
        self.skipped: list[dict[str, JSONValue]] = []
        self.error: MacOSError | None = None
        self.watching = False
        self.ended = False
        self.stopped_by: _Stop = "timeout"
        self._finder: MacOS | None = None
        self._tap: Any = None
        self._down: tuple[float, float, _Seen | None] | None = None
        self._read_at = 0
        self._stop_at = 0
        self._field: Any = None
        self._since: _Since = "moved"
        self._since_at = time.monotonic_ns()
        self._field_value: object = None
        self._unsure: tuple[_Element, object] | None = None
        self._hidden = (0, 0)
        self._hiding = False
        self._menus_open = 0
        self._closing = False
        self._callbacks: tuple[Callable[..., Any], ...] = ()

    def stop(self, reason: _Stop, at: int | None = None) -> None:
        """End the watch after the person's input up to ``at``, an event's
        timestamp, or else up to now."""
        with self._lock:
            if not self._stopping.is_set():
                self.stopped_by = reason
                # Quartz stamps events in nanoseconds of the clock monotonic_ns reads.
                self._stop_at = time.monotonic_ns() if at is None else at
                self._stopping.set()

    def _fail(self, message: str) -> None:
        if self.error is None:
            self.error = MacOSError(f"Watching stopped: {message}", code=ErrorCode.UNSUPPORTED_OP,
                                    details={"steps_seen": len(self.seen)})
        self.stop("stop")

    def _skip(self, event: str, reason: str) -> None:
        self.skipped.append({"event": event, "reason": reason})

    def run(self) -> None:
        try:
            with objc.autorelease_pool():
                finder = self._finder = MacOS(backend="python")
            try:
                self._watch()
            finally:
                finder.close()
            self.ended = True
        finally:
            self.ready.set()

    def _watch(self) -> None:
        def on_event(proxy: object, kind: int, event: _Event, refcon: object) -> _Event:
            if self.error is None and (not self._stopping.is_set()
                                       or AS.CGEventGetTimestamp(event) <= self._stop_at):
                self._event(kind, event)
            return event

        @objc.callbackFor(AS.AXObserverCreate)
        def on_notice(observer: object, element: _Element, notification: str, refcon: object) -> None:
            if not self._stopping.is_set():
                self._notice(element, str(notification))

        self._callbacks = (on_event, on_notice)
        mask = 0
        for kind in (AS.kCGEventLeftMouseDown, AS.kCGEventLeftMouseUp,
                     AS.kCGEventRightMouseDown, AS.kCGEventKeyDown):
            mask |= AS.CGEventMaskBit(kind)
        tap = AS.CGEventTapCreate(AS.kCGAnnotatedSessionEventTap, AS.kCGTailAppendEventTap,
                                  AS.kCGEventTapOptionListenOnly, mask, on_event, None)
        if tap is None:
            self.error = _listen_error()
            return
        error, observer = AS.AXObserverCreate(self._pid, on_notice, None)
        if error != 0:
            AS.CFMachPortInvalidate(tap)
            self.error = MacOSError("Watching could not observe the app's accessibility notifications",
                                    code=ErrorCode.AX_ERROR, details={"ax_error": int(error)})
            return
        loop = AS.CFRunLoopGetCurrent()
        sources = (AS.CFMachPortCreateRunLoopSource(None, tap, 0), AS.AXObserverGetRunLoopSource(observer))
        for source in sources:
            AS.CFRunLoopAddSource(loop, source, _MODE)
        refused = {notice: int(code) for notice in _NOTICES
                   if (code := AS.AXObserverAddNotification(observer, self._app, notice, None)) != 0}
        self._tap = tap
        done = threading.Event()
        sampler = threading.Thread(target=self._sample, args=(done,), daemon=True)
        sampler.start()
        try:
            if refused:
                self.error = MacOSError("Watching could not observe the app's accessibility notifications",
                                        code=ErrorCode.AX_ERROR, details={"refused": refused})
                return
            AS.CGEventTapEnable(tap, True)
            self.watching = True
            self.ready.set()
            focused = self._field = MacOS._copy_attribute(self._app, "AXFocusedUIElement")
            if focused is not None and self._secure(focused):
                self._secret(focused)
            while not self._stopping.is_set():
                if self._deadline.exhausted():
                    self.stop("timeout")
                    break
                with objc.autorelease_pool():
                    AS.CFRunLoopRunInMode(_MODE, _TURN_S, False)
                    if not self._stopping.is_set():
                        self._check_secure_input()
            # The tap's queue may still hold the person's input from before the stop.
            with objc.autorelease_pool():
                while AS.CFRunLoopRunInMode(_MODE, 0, True) == AS.kCFRunLoopRunHandledSource:
                    pass
            if self._typed_then_hidden(self._stop_at):
                self._fail(_UNSURE_SECRET)
            if self._down is not None:
                self._fail("the watch ended before a click finished")
        finally:
            done.set()
            sampler.join()
            AS.CGEventTapEnable(tap, False)
            for notice in _NOTICES:
                AS.AXObserverRemoveNotification(observer, self._app, notice)
            for source in sources:
                AS.CFRunLoopRemoveSource(loop, source, _MODE)
            AS.CFMachPortInvalidate(tap)

    def _event(self, kind: int, event: _Event) -> None:
        if kind in (AS.kCGEventTapDisabledByTimeout, AS.kCGEventTapDisabledByUserInput):
            AS.CGEventTapEnable(self._tap, True)
            self._fail("macOS paused the event tap, so some of the person's input went unseen")
        elif self._typed_then_hidden(AS.CGEventGetTimestamp(event)):
            self._fail(_UNSURE_SECRET)
        elif kind == AS.kCGEventKeyDown:
            self._key(event)
        elif kind == AS.kCGEventRightMouseDown:
            if self._hit(*_point(event)) is not None:
                self._fail("a right click opens a context menu, which a route cannot replay")
        elif kind == AS.kCGEventLeftMouseDown:
            self._mouse_down(event)
        elif kind == AS.kCGEventLeftMouseUp:
            self._mouse_up(event)

    def _notice(self, element: _Element, notice: str) -> None:
        if notice == "AXMenuOpened":
            self._menus_open += 1
            self._closing = False
        elif notice == "AXMenuClosed":
            self._menus_open = max(0, self._menus_open - 1)
        elif self._secure(element):
            self._secret(element)

    # --- what the person did ----------------------------------------------

    def _act(self, verb: _Verb, *, target: _Locator | None = None, text: str = "",
             anchor: _Locator | None) -> None:
        if len(self.seen) >= _MAX_STEPS:
            self._fail(f"a route holds at most {_MAX_STEPS} steps")
            return
        if self.seen and self.seen[-1].after is None:
            last = self.seen[-1]
            if anchor is not None:
                last.after = anchor
            elif last.anchor is not None and self._find(last.anchor) is not None:
                last.after = last.anchor
        self.seen.append(_Seen(verb, target, text, anchor))

    def _late(self, event: _Event) -> bool:
        """Whether ``event`` came before the watch's last read of the app,
        which may then have shown what ``event`` did: the watch is behind."""
        return AS.CGEventGetTimestamp(event) < self._read_at

    def _mouse_down(self, event: _Event) -> None:
        self._down = None
        if self._late(event):
            self._fail(_BEHIND)
            return
        x, y = _point(event)
        menu_open = self._menu_open()
        self._closing = False
        element = self._hit(x, y)
        found = None if element is None else self._control(element)
        self._read_at = time.monotonic_ns()
        if element is None:
            self._skip("click", "outside the app")
            return
        if not self._settled():
            return
        self._since, self._since_at = "moved", AS.CGEventGetTimestamp(event)
        if found is None:
            self._skip("click", "closed the open menu" if menu_open else "no control under the pointer")
            return
        kind, control = found
        if kind == "menu":
            self._down = (x, y, None)
            return
        if menu_open and kind != "item":
            self._skip("click", "closed the open menu")
            return
        if kind == "adjust":
            self._fail(f"a {MacOS._copy_attribute(control, 'AXRole')} set by clicking or dragging, "
                       "which a route cannot replay")
            return
        if AS.CGEventGetFlags(event) & (AS.kCGEventFlagMaskCommand | AS.kCGEventFlagMaskControl
                                        | AS.kCGEventFlagMaskAlternate | AS.kCGEventFlagMaskShift):
            self._fail("a click while holding modifier keys, which a route cannot replay")
            return
        clicks = AS.CGEventGetIntegerValueField(event, AS.kCGMouseEventClickState)
        if clicks > 1 and kind == "focus":
            self._fail("a double click in a text field selects text, which a route cannot repeat")
            return
        if clicks > 1 and kind == "select":
            self._fail("a double click on a row, which a route cannot replay")
            return
        if kind == "item":
            titles, top = self._menu_trail(control)
            if top == "AXMenuBar":
                if len(titles) < 2 or not all(titles):
                    self._fail("a menu item without a title was picked")
                else:
                    self._down = (x, y, _Seen("menu", None, " > ".join(str(title) for title in reversed(titles)), None))
                return
        target = self._locate(control)
        if target is None:
            role = MacOS._copy_attribute(control, "AXRole")
            self._fail(f"the {role} clicked has no unique title, description or identifier, or it "
                       "changed before the watch could read one; if clicking changes it, click a "
                       "little slower")
            return
        if kind != "focus":
            self._down = (x, y, _Seen("select" if kind == "select" else "press", target, "", target))
            return
        if self._secure(control):
            self._secret(control)
            return
        self._field = control
        last = self.seen[-1] if self.seen else None
        if last is None or last.verb != "focus" or last.target != target:
            self._act("focus", target=target, anchor=target)
        self._down = (x, y, _Seen("focus", target, "", target))

    def _mouse_up(self, event: _Event) -> None:
        if self._down is None:
            return
        x, y, seen = self._down
        self._down = None
        # A menu bar title opens its menu on the press, so its release
        # changes nothing the press read.
        if seen is not None and self._late(event):
            self._fail(_BEHIND)
            return
        dragged = math.dist((x, y), _point(event)) > _CLICK_SLOP
        if seen is None or seen.verb == "menu" or (seen.target is not None and seen.target.role == "AXMenuItem"):
            if dragged:
                self._fail("a menu item picked by dragging, which the watch cannot read; "
                           "click the menu, then click the item")
            elif seen is not None:
                self._act(seen.verb, target=seen.target, text=seen.text, anchor=seen.anchor)
                self._closing = True
            return
        if dragged and seen.verb == "select":
            self._fail("a drag across rows, which a route cannot replay")
        elif dragged and seen.verb == "focus":
            self._fail("a drag in a text field selects text, which a route cannot repeat")
        elif dragged:
            self._skip("click", "a drag, not a click")
        elif seen.verb != "focus":
            self._act(seen.verb, target=seen.target, anchor=seen.anchor)

    def _key(self, event: _Event) -> None:
        keycode = int(AS.CGEventGetIntegerValueField(event, AS.kCGKeyboardEventKeycode))
        flags = int(AS.CGEventGetFlags(event))
        if keycode == _STOP_KEY and flags & _STOP_FLAGS == _STOP_FLAGS:
            self.stop("hotkey", AS.CGEventGetTimestamp(event))
            return
        if AS.CGEventGetIntegerValueField(event, AS.kCGEventTargetUnixProcessID) != self._pid:
            return
        at = AS.CGEventGetTimestamp(event)
        late = self._late(event)
        menu_open = self._menu_open()
        error, focused = AS.AXUIElementCopyAttributeValue(self._app, "AXFocusedUIElement", None)
        self._read_at = time.monotonic_ns()
        if menu_open:
            if late:
                self._fail(_BEHIND)
            elif keycode not in _MENU_KEYS:
                self._fail("a key pressed while a menu was open, which the watch cannot read; "
                           "click the menu item instead")
            return
        if error not in (0, AS.kAXErrorNoValue):
            self._fail(f"could not tell which control a key went to (AX error {error})")
            return
        held = [name for name, flag in _MODIFIERS if flags & flag]
        typing = self.seen[-1] if self.seen and self.seen[-1].verb == "type" else None
        if focused is not None and self._secure(focused):
            moves = _KEY_NAMES.get(keycode) in _FOCUS_KEYS
            # A late key that types and finds a secure field after a step that
            # can move focus went there: a focus change after it came late
            # too, and stops the watch then. Keys kept as text since the
            # person last moved focus may have gone there as well.
            if late and (typing is not None or keycode in _EDIT_KEYS
                         or {"cmd", "ctrl"} & set(held)):
                self._fail(_BEHIND)
            elif self._since == "typed" and not moves:
                self._fail(_UNSURE_SECRET)
            elif self._settled():
                if moves:
                    self._since, self._since_at = "moved", at
                self._secret(focused)
            return
        if (not {"cmd", "ctrl"} & set(held) and focused is not None
                and MacOS._copy_attribute(focused, "AXRole") in _FIELDS):
            if (keycode == _BACKSPACE and not held and typing is not None
                    and self._field == focused and typing.text):
                typing.text = typing.text[:-1]
                if not typing.text:
                    self.seen.pop()
                    self._field = None
                return
            if keycode not in _EDIT_KEYS:
                length, chars = AS.CGEventKeyboardGetUnicodeString(event, 16, None, None)
                text = str(chars[:length]) if chars else ""
                # A key with Option set whose text is still the key's own
                # letter types that letter in some apps and an accent in others.
                if (not text or ("alt" in held and text.lower() == _KEY_NAMES.get(keycode))
                        or AS.CGEventGetIntegerValueField(event, AS.kCGKeyboardEventAutorepeat)):
                    self._fail("a key typed text the watch cannot read, such as an accent key or "
                               "a key held down until it repeats or opens the accent menu; a "
                               "route cannot replay it")
                    return
                if text.isprintable():
                    if late and self._stale(focused, typing):
                        self._fail(_BEHIND)
                        return
                    # Focus leaves a secure field unseen only through keys
                    # macOS hid; without those, the read may predate the field.
                    if self._since == "secret" and not self._hidden_between(self._since_at, at):
                        self._fail(_UNSURE_SECRET)
                        return
                    if self._since != "typed":
                        self._since, self._since_at = "typed", at
                        last = self.seen[-1] if self.seen else None
                        # Escape mostly closes something and leaves focus be.
                        if (last is not None and last.verb == "key" and focused == self._field
                                and last.text.rsplit("+", 1)[-1] in ("tab", "return")):
                            self._unsure = (focused, self._field_value)
                    elif self._unsure is not None and focused != self._unsure[0] and not self._settled():
                        return
                    self._type(text, focused, typing)
                    return
        if late:
            self._fail(_BEHIND)
            return
        name = _KEY_NAMES.get(keycode)
        if name is None:
            self._fail(f"a key the harness cannot press (keycode {keycode})")
            return
        types = keycode not in _EDIT_KEYS and not {"cmd", "ctrl"} & set(held)
        if types and self._since == "secret" and not self._hidden_between(self._since_at, at):
            self._fail(_UNSURE_SECRET)
            return
        if name in _FOCUS_KEYS and not self._settled():
            return
        self._act("key", text="+".join([*held, name]), anchor=self._anchor(focused))
        if name in _FOCUS_KEYS:
            self._since, self._since_at = "moved", at
            if self._field is not None:
                self._field_value = MacOS._copy_attribute(self._field, "AXValue")
        elif types and self._since != "typed":
            self._since, self._since_at = "typed", at

    def _stale(self, focused: _Element, typing: _Seen | None) -> bool:
        """Whether a late key's read of focus may come from before the app
        took in the person's last step, which may have moved focus into a
        secure field: the read should show the field a click went to, and
        not the field the person was in before Tab, a press or the like.
        ``_field`` is ``None`` once typing there showed where focus went."""
        if typing is not None or not self.seen:
            return False
        last = self.seen[-1]
        if last.verb == "secret":
            return True
        if self._field is None or (last.verb == "key"
                                   and last.text.rsplit("+", 1)[-1] not in _FOCUS_KEYS):
            return False
        if last.verb == "focus":
            return focused != self._field
        return focused == self._field

    def _type(self, text: str, focused: _Element, typing: _Seen | None) -> None:
        if (typing is not None and self._field == focused
                and len(typing.text) + len(text) <= _MAX_TYPE_CHARS):
            typing.text += text
            return
        target = self._locate(focused)
        if target is None:
            role = MacOS._copy_attribute(focused, "AXRole")
            self._fail(f"text was typed into a {role} with no unique title, description or identifier")
            return
        self._field = focused
        self._act("type", target=target, text=text, anchor=target)

    def _menu_open(self) -> bool:
        """Whether one of the app's menus is open and not just closing on a
        pick: a click then goes to the menu, not to the control under it."""
        if self._closing:
            return False
        if self._menus_open:
            return True
        windows = AS.CGWindowListCopyWindowInfo(AS.kCGWindowListOptionOnScreenOnly, AS.kCGNullWindowID) or ()
        return any(window.get(AS.kCGWindowOwnerPID) == self._pid
                   and window.get(AS.kCGWindowLayer) == AS.kCGPopUpMenuWindowLevel for window in windows)

    def _check_secure_input(self) -> None:
        """Fail once secure keyboard entry hides keys the route needs: on
        with no secure field of this app in focus. The second read follows
        the app's own answer about focus, so a field that just handed focus
        on has turned the entry off by then."""
        holder = _secure_input_holder()
        if holder is None:
            return
        if holder == self._pid:
            focused = MacOS._copy_attribute(self._app, "AXFocusedUIElement")
            if (focused is not None and self._secure(focused)) or _secure_input_holder() is None:
                return
        self._fail("macOS secure keyboard entry is on, which hides the person's keys from the watch")

    def _sample(self, done: threading.Event) -> None:
        """Note when macOS hides keys for this app, on a thread of its own:
        the watch's thread can spend longer reading the app than the person
        takes to type a password and leave the field."""
        while not done.wait(_SAMPLE_S):
            self._note_hidden()

    def _note_hidden(self) -> None:
        if _secure_input_holder() != self._pid:
            self._hiding = False
            return
        now = time.monotonic_ns()
        self._hidden = (self._hidden[0] if self._hiding else now, now)
        self._hiding = True

    def _hidden_between(self, start: int, end: int) -> bool:
        """Whether macOS hid keys for this app at some time from ``start``
        to ``end``, as far as the last stretch of samples that saw it tells."""
        first, last = self._hidden
        return first <= end and last >= start

    def _typed_then_hidden(self, before: int) -> bool:
        """Whether macOS hid keys for this app before ``before`` and after a
        key kept as text since the person last moved focus: the app may have
        put a secure field in focus without showing it, and that key in it."""
        return self._since == "typed" and self._hidden_between(self._since_at, before)

    def _settled(self) -> bool:
        """Whether keys kept after Tab or Return that still read as the field
        focus was in went there, as a change in its value shows by the
        person's next move or key in another field; if not, the watch stops."""
        if self._unsure is None:
            return True
        field, value = self._unsure
        self._unsure = None
        if MacOS._copy_attribute(field, "AXValue") == value:
            self._fail(_UNSURE_SECRET)
            return False
        return True

    def _secret(self, element: _Element) -> None:
        target = self._locate(element)
        if target is None:
            self._fail("a secure text field with no unique title, description or identifier took focus")
            return
        if self._since != "typed":
            self._since = "secret"
        last = self.seen[-1] if self.seen else None
        if last is None or last.verb != "secret" or last.target != target:
            self._act("secret", target=target, anchor=target)

    # --- reading the app --------------------------------------------------

    def _hit(self, x: float, y: float) -> _Element | None:
        error, element = AS.AXUIElementCopyElementAtPosition(self._system, x, y, None)
        if error != 0 or element is None or _pid(element) != self._pid:
            return None
        return element

    def _control(self, element: _Element) -> tuple[_Kind, _Element] | None:
        for _ in range(_CLIMB):
            values = MacOS._copy_attributes(element, ("AXRole", "AXParent", "AXEnabled", "AXChildren"))
            role = values.get("AXRole")
            if role == "AXMenuItem" and values.get("AXEnabled") is not False and not values.get("AXChildren"):
                return "item", element
            if role in _MENU_PARTS:
                return "menu", element
            if role in _FIELDS:
                return "focus", element
            if role == "AXRow":
                return "select", element
            if role in _ADJUSTERS:
                return "adjust", element
            if role in _TOPS:
                return None
            error, actions = AS.AXUIElementCopyActionNames(element, None)
            if error == 0 and actions and "AXPress" in actions:
                return "press", element
            element = values.get("AXParent")
            if element is None:
                return None
        return None

    def _menu_trail(self, item: _Element) -> tuple[list[str | None], str | None]:
        """The titles from ``item`` up through its menus, and the role of
        what holds the outermost menu: "AXMenuBar" for the app's menu bar."""
        titles: list[str | None] = []
        element = item
        for _ in range(16):
            values = MacOS._copy_attributes(element, ("AXRole", "AXTitle", "AXParent"))
            role = values.get("AXRole")
            if role in ("AXMenuItem", "AXMenuBarItem"):
                title = values.get("AXTitle")
                titles.append(str(title) if isinstance(title, str) and title else None)
            elif role != "AXMenu":
                return titles, None if role is None else str(role)
            element = values.get("AXParent")
            if element is None:
                return titles, None
        return titles, None

    def _secure(self, element: _Element) -> bool:
        return MacOS._copy_attribute(element, "AXSubrole") == _SECURE_SUBROLE

    def _anchor(self, focused: _Element | None) -> _Locator | None:
        """What a key went to: the focused control when the route can find
        it again, else the focused window."""
        if focused is not None and (target := self._locate(focused)) is not None:
            return target
        window = MacOS._copy_attribute(self._app, "AXFocusedWindow")
        return None if window is None else self._locate(window)

    def _locate(self, element: _Element) -> _Locator | None:
        """How a route finds ``element`` again: its role and the first of
        title, description and identifier that the same search a run makes
        resolves to exactly this element."""
        values = MacOS._copy_attributes(element, ("AXRole", "AXTitle", "AXDescription", "AXIdentifier"))
        role = values.get("AXRole")
        if not isinstance(role, str) or not role:
            return None
        for field, attribute in (("title", "AXTitle"), ("description", "AXDescription"),
                                 ("identifier", "AXIdentifier")):
            value = values.get(attribute)
            if isinstance(value, str) and value:
                locator = _Locator(str(role), field, str(value))
                if self._find(locator) == element:
                    return locator
        return None

    def _find(self, locator: _Locator) -> _Element | None:
        finder = self._finder
        if finder is None:
            return None
        try:
            match = finder.ax_wait(app=self._pid, search_key=locator.role, timeout=0.0, explain=False,
                                   **{locator.field: locator.value})
            return finder._element(int(match["element_index"]))
        except MacOSError:
            return None

    def steps(self, goal: Postcondition) -> list[_Step]:
        if not self.seen:
            raise _invalid("watch", "saw nothing done in the app")
        checks = [seen.check(following) for seen, following in pairwise(self.seen)]
        return [seen.step(check, f"steps[{index}]")
                for index, (seen, check) in enumerate(zip(self.seen, [*checks, goal], strict=True))]


class RouteWatch:
    """A watch under way. `wait` for the person's stop hotkey or the time
    limit, or `stop` it now; either saves the route once its goal holds."""

    def __init__(self, routes: Routes, name: str, app: str, identity: _AppIdentity,
                 entry: Postcondition, goal: Postcondition, version: tuple[str | None, str | None],
                 watcher: _Watcher, thread: threading.Thread) -> None:
        self._routes = routes
        self._name = name
        self._app = app
        self._identity = identity
        self._entry = entry
        self._goal = goal
        self._version = version
        self._watcher = watcher
        self._thread = thread
        self._lock = threading.Lock()
        self._result: WatchResult | None = None

    def stop(self) -> WatchResult:
        """End the watch now and save what it saw."""
        self._watcher.stop("stop")
        return self.wait()

    def wait(self) -> WatchResult:
        """Block until the watch ends, then save what it saw."""
        self._thread.join()
        with self._lock:
            if self._result is None:
                self._result = self._finish()
            return self._result

    def _finish(self) -> WatchResult:
        routes, watcher = self._routes, self._watcher
        ops = routes._host.do
        ops._check_owner()
        steps: list[_Step] = []
        check: Receipt | None = None
        path: str | None = None
        error = watcher.error
        if error is None and not watcher.ended:
            error = MacOSError("Watching ended on an unexpected error, so nothing was saved",
                               code=ErrorCode.UNSUPPORTED_OP)
        if error is None:
            try:
                steps = watcher.steps(self._goal)
                for step in steps:
                    routes._validate_step(step)
                with ops._dispatch_lock:
                    check = routes._expect(self._goal, self._identity, _Deadline(_CHECK_S, ops._monotonic))
                    route = _Route(self._name, self._app, self._identity.name,
                        datetime.fromtimestamp(ops._wall_clock(), UTC).isoformat(timespec="milliseconds"),
                        *self._version, self._entry, self._goal, tuple(steps))
                    path = str(routes._save(route))
            except MacOSError as exc:
                if isinstance(exc, OperationError) and exc.receipt.op == "expect":
                    check = exc.receipt
                error = exc
        return WatchResult(self._name, "saved" if path is not None else "unsaved", watcher.stopped_by,
                           tuple(_step_json(step) for step in steps), tuple(watcher.skipped),
                           path=path, check=check, error=None if error is None else error.to_json())


def watch(routes: Routes, name: str, *, app: str, entry: Postcondition, goal: Postcondition,
          timeout: float) -> RouteWatch:
    ops = routes._host.do
    ops._check_owner()
    routes._path(name, app)
    entry, goal = _condition(entry, app), _condition(goal, app)
    ops._validate_postcondition(ops._host, entry)
    ops._validate_postcondition(ops._host, goal)
    deadline = _Deadline(timeout, ops._monotonic)
    routes._host._ensure_accessibility()
    with ops._dispatch_lock:
        identity = routes._bind(app)
        routes._expect(entry, identity, _Deadline(_CHECK_S, ops._monotonic))
        version = routes._host._bundle_version(identity.path)
    watcher = _Watcher(identity.pid, deadline)
    thread = threading.Thread(target=watcher.run, name="macos-harness-watch", daemon=True)
    thread.start()
    watcher.ready.wait()
    if not watcher.watching:
        thread.join()
        raise watcher.error or MacOSError("Watching could not start", code=ErrorCode.UNSUPPORTED_OP)
    return RouteWatch(routes, name, app, identity, entry, goal, version, watcher, thread)
