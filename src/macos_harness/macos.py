"""Direct macOS control through public ApplicationServices APIs.

No Codex or OpenAI Computer Use runtime is used here. Accessibility supplies
the semantic tree/actions; Core Graphics supplies raw input; ScreenCaptureKit
renders a specific window (see ``capture.py``).
"""

from __future__ import annotations

import ctypes
import errno
import json
import math
import os
import plistlib
import re
import subprocess
import tempfile
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple, Self

from .capture import capture_window, draw_pointer, write_png
from .errors import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    ErrorCode,
    FocusChangedError,
    MacOSError,
)
from .handoff import HandoffReason, HumanHandoff
from .ops import _Deadline, _utc_timestamp
from .overlay import LivePointerOverlay
from .receipts import JSONValue, Receipt, validate_exact_selectors

if TYPE_CHECKING:
    # Only for annotations -- `native.py` imports from this module at
    # runtime, so importing it back here for real would be circular.
    # `from __future__ import annotations` (above) already makes every
    # annotation in this file a deferred string, so this import never
    # needs to run outside a type checker.
    from .native import NativeClient

try:
    import ApplicationServices as AS
    import objc
    from AppKit import (
        NSApplicationActivateIgnoringOtherApps,
        NSRunningApplication,
        NSWorkspace,
    )
except ImportError as exc:  # pragma: no cover - exercised on non-macOS hosts
    AS = None  # type: ignore[assignment]
    objc = None  # type: ignore[assignment]
    NSApplicationActivateIgnoringOtherApps = 0
    NSRunningApplication = None  # type: ignore[assignment]
    NSWorkspace = None  # type: ignore[assignment]
    _IMPORT_ERROR: ImportError | None = exc
else:
    _IMPORT_ERROR = None


class _CGPoint(ctypes.Structure):
    _fields_ = (("x", ctypes.c_double), ("y", ctypes.c_double))


class _TargetWindow(NamedTuple):
    """The window a posted mouse or scroll event is bound to."""

    window_id: int
    origin: tuple[float, float]  # top-left screen point

class _AppIdentity(NamedTuple):
    pid: int
    bundle_id: str | None
    launched_at: float
    name: str
    path: str | None


# A mouse or scroll event posted with ``CGEventPostToPid`` skips the window
# server's hit test, so AppKit has to learn the destination window from the
# event itself: the window number in this private ``CGEventField`` plus the
# window-local point written by the private ``CGEventSetWindowLocation``.
# Without both, AppKit finds no window under the event and drops it, and
# the caret in TextEdit never moves (measured 2026-09-13; the field is the
# one ``NSEvent.mouseEventWithType:...windowNumber:`` fills, and the
# recipe is the one https://github.com/Lakr233/bgclick-rev-skill recovered).
_CG_EVENT_WINDOW_NUMBER = 51
_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


def _load_window_location_setter() -> Callable[[int, _CGPoint], None] | None:
    """Resolve ``CGEventSetWindowLocation``; ``None`` on a macOS that dropped it."""
    try:
        setter = ctypes.CDLL(_CORE_GRAPHICS).CGEventSetWindowLocation
    except (OSError, AttributeError):
        return None
    setter.argtypes = (ctypes.c_void_p, _CGPoint)
    setter.restype = None
    return setter


_set_window_location = _load_window_location_setter() if AS else None

class _ProcessBSDInfo(ctypes.Structure):
    # sys/proc_info.h: PROC_PIDTBSDINFO / struct proc_bsdinfo.
    _fields_ = (
        ("pbi_flags", ctypes.c_uint32), ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32), ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32), ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32), ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32), ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32), ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16), ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32), ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32), ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32), ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64), ("pbi_start_tvusec", ctypes.c_uint64),
    )


def _load_process_info() -> Callable[[int, int, int, object, int], int] | None:
    try:
        function = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_pidinfo
    except (OSError, AttributeError):
        return None
    function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int)
    function.restype = ctypes.c_int
    return function


_process_info = _load_process_info() if AS else None


def _process_start_time(pid: int) -> float:
    if _process_info is None:
        raise MacOSError("Process start identity is unavailable", code=ErrorCode.UNSUPPORTED_OP)
    info = _ProcessBSDInfo()
    size = ctypes.sizeof(info)
    returned = _process_info(pid, 3, 0, ctypes.byref(info), size)
    if returned != size:
        error = ctypes.get_errno()
        if returned == 0 and error == errno.ESRCH:
            raise ApplicationNotFoundError("The process exited before its identity could be read",
                                           details={"pid": pid})
        raise MacOSError("Could not read the process start identity", code=ErrorCode.UNSUPPORTED_OP,
                         details={"pid": pid, "errno": error, "bytes_read": returned})
    return info.pbi_start_tvsec + info.pbi_start_tvusec / 1_000_000


_AX_SUCCESS = 0
_AX_ATTRIBUTES = (
    "AXRole",
    "AXSubrole",
    "AXRoleDescription",
    "AXMain",
    "AXModal",
    "AXTitle",
    "AXDescription",
    "AXHelp",
    "AXIdentifier",
    "AXDOMIdentifier",
    "AXURL",
    "AXValue",
    "AXPlaceholderValue",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXHidden",
    "AXPosition",
    "AXSize",
    "AXFrame",
    "AXChildren",
    "AXWindows",
)
_AX_SAFE_ATTRIBUTES = (
    "AXRole",
    "AXSubrole",
    "AXMain",
    "AXModal",
    "AXTitle",
    "AXDescription",
    "AXPlaceholderValue",
    "AXHelp",
    "AXIdentifier",
    "AXDOMIdentifier",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXHidden",
    "AXFrame",
)
_AX_CROSS_APP_MESSAGING_TIMEOUT = 0.5
# A process younger than this with no window is presumed still launching,
# and a capture waits for its first window instead of failing at once.
_LAUNCH_GRACE_SECONDS = 5.0
_LAUNCH_WINDOW_TIMEOUT = 2.0
_AX_NODE_MAPPING = {
    "AXSubrole": "subrole",
    "AXMain": "main",
    "AXModal": "modal",
    "AXRoleDescription": "role_description",
    "AXTitle": "title",
    "AXDescription": "description",
    "AXHelp": "help",
    "AXIdentifier": "identifier",
    "AXDOMIdentifier": "dom_identifier",
    "AXURL": "url",
    "AXValue": "value",
    "AXPlaceholderValue": "placeholder",
    "AXEnabled": "enabled",
    "AXFocused": "focused",
    "AXSelected": "selected",
    "AXHidden": "hidden",
    "AXPosition": "position",
    "AXSize": "size",
    "AXFrame": "frame",
}
# What `_focus_sample` reads from the focused element, and the receipt
# name for each. Identity first, on its own: it decides whether the
# details may be read at all, so a secure field's value, length and
# selection are never requested, not merely dropped -- and an identity
# read that failed raises (see `_copy_attributes`), so it authorizes
# nothing either.
_FOCUS_IDENTITY_ATTRIBUTES = {
    "AXRole": "role",
    "AXSubrole": "subrole",
}
_FOCUS_DETAIL_ATTRIBUTES = {
    "AXTitle": "title",
    "AXDescription": "description",
    "AXPosition": "position",
    "AXSize": "size",
    "AXValue": "value",
    "AXSelectedTextRange": "selected_range",
    "AXNumberOfCharacters": "characters",
}
_SECURE_SUBROLE = "AXSecureTextField"
_SENSITIVE_ATTRIBUTES = frozenset({"AXValue", "AXSelectedText", "AXSelectedTextRange", "AXNumberOfCharacters"})
_SETTABLE_CANDIDATES = ("AXValue", "AXFocused", "AXSelected")
_ACTION_ALIASES = {
    "press": "AXPress",
    "show menu": "AXShowMenu",
    "confirm": "AXConfirm",
    "cancel": "AXCancel",
    "increment": "AXIncrement",
    "decrement": "AXDecrement",
    "raise": "AXRaise",
}
_BUTTONS = {
    "left": (AS.kCGMouseButtonLeft if AS else 0),
    "right": (AS.kCGMouseButtonRight if AS else 1),
    "middle": (AS.kCGMouseButtonCenter if AS else 2),
}
_MOUSE_EVENTS = {
    "left": (
        AS.kCGEventLeftMouseDown if AS else 1,
        AS.kCGEventLeftMouseUp if AS else 2,
        AS.kCGEventLeftMouseDragged if AS else 6,
    ),
    "right": (
        AS.kCGEventRightMouseDown if AS else 3,
        AS.kCGEventRightMouseUp if AS else 4,
        AS.kCGEventRightMouseDragged if AS else 7,
    ),
    "middle": (
        AS.kCGEventOtherMouseDown if AS else 25,
        AS.kCGEventOtherMouseUp if AS else 26,
        AS.kCGEventOtherMouseDragged if AS else 27,
    ),
}
_MODIFIER_FLAGS = {
    "cmd": AS.kCGEventFlagMaskCommand if AS else 0,
    "command": AS.kCGEventFlagMaskCommand if AS else 0,
    "super": AS.kCGEventFlagMaskCommand if AS else 0,
    "ctrl": AS.kCGEventFlagMaskControl if AS else 0,
    "control": AS.kCGEventFlagMaskControl if AS else 0,
    "alt": AS.kCGEventFlagMaskAlternate if AS else 0,
    "option": AS.kCGEventFlagMaskAlternate if AS else 0,
    "shift": AS.kCGEventFlagMaskShift if AS else 0,
}
_MODIFIER_KEYCODES = {
    "cmd": 55,
    "command": 55,
    "super": 55,
    "shift": 56,
    "alt": 58,
    "option": 58,
    "ctrl": 59,
    "control": 59,
}
_AX_SEARCH_ROLES = {
    f"AX{name}SearchKey": f"AX{name}"
    for name in (
        "Button",
        "CheckBox",
        "ComboBox",
        "Image",
        "Link",
        "List",
        "Menu",
        "MenuItem",
        "RadioButton",
        "StaticText",
        "Table",
        "TextArea",
        "TextField",
    )
}
_KEYCODES = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "enter": 36,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "tab": 48,
    "space": 49,
    "`": 50,
    "backspace": 51,
    "delete": 51,
    "escape": 53,
    "esc": 53,
    "home": 115,
    "pageup": 116,
    "page_up": 116,
    "forward_delete": 117,
    "end": 119,
    "pagedown": 121,
    "page_down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}
_SHIFTED_CHARACTERS = dict(
    zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./", strict=True)
)
# Characters `type` sends by key name rather than by character: a space
# so the keycode is right, and the three that must always arrive as a
# key press, never inside a packed run of text.
_KEY_NAMES = {" ": "space", "\n": "return", "\r": "return", "\t": "tab"}
_KEY_ONLY_CHARACTERS = frozenset("\n\r\t")
# The most UTF-16 units one packed key event carries: 20 landed exactly
# in every app measured, and larger payloads were not measured.
_TYPE_CHUNK_UNITS = 20
# macOS has no gesture past a triple click.
_MAX_CLICKS = 3


def _character_key(character: str) -> tuple[int, int]:
    """The keycode and modifier flags one typed ``character`` rides on."""
    base = _SHIFTED_CHARACTERS.get(character, _KEY_NAMES.get(character, character.casefold()))
    flags = (
        AS.kCGEventFlagMaskShift
        if character.isupper() or character in _SHIFTED_CHARACTERS
        else 0
    )
    return _KEYCODES.get(base, 0), flags


def _typing_chunks(text: str, *, packed: bool) -> Iterator[str]:
    """The strings each key event of `MacOS.type` carries, in order.

    A `_KEY_ONLY_CHARACTERS` member always travels alone; unpacked, so
    does every other character. Packed, the rest group into runs of at
    most `_TYPE_CHUNK_UNITS` UTF-16 units, split only between code
    points so a surrogate pair never straddles two events.
    """
    run: list[str] = []
    units = 0
    for character in text:
        if not packed or character in _KEY_ONLY_CHARACTERS:
            if run:
                yield "".join(run)
                run, units = [], 0
            yield character
            continue
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > _TYPE_CHUNK_UNITS:
            yield "".join(run)
            run, units = [], 0
        run.append(character)
        units += width
    if run:
        yield "".join(run)


def _parse_key(key: str) -> tuple[int, tuple[tuple[int, int], ...]]:
    parts = [part.casefold() for part in re.split(r"[+-]", key) if part]
    if not parts:
        raise MacOSError(
            "Key must not be empty",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "key"},
        )
    base = parts[-1]
    try:
        keycode = _KEYCODES[base]
    except KeyError as exc:
        raise MacOSError(
            f"Unsupported key {base!r}; use mac.type() for arbitrary text",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "key", "value": base},
        ) from exc

    parsed_modifiers: list[tuple[int, int]] = []
    seen_keycodes: set[int] = set()
    for modifier in parts[:-1]:
        try:
            modifier_keycode = _MODIFIER_KEYCODES[modifier]
            modifier_flag = _MODIFIER_FLAGS[modifier]
        except KeyError as exc:
            raise MacOSError(
                f"Unsupported modifier {modifier!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "modifier", "value": modifier},
            ) from exc
        if modifier_keycode not in seen_keycodes:
            seen_keycodes.add(modifier_keycode)
            parsed_modifiers.append((modifier_keycode, modifier_flag))
    return keycode, tuple(parsed_modifiers)


def _require_macos() -> None:
    if AS is None or NSWorkspace is None:
        raise MacOSError(
            "macOS ApplicationServices bindings are unavailable. Run on macOS "
            "after installing project dependencies with `uv sync`.",
            code=ErrorCode.AX_ERROR,
        ) from _IMPORT_ERROR


def _truncate(value: str, limit: int = 160) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _ax_error(operation: str, error: int, **details: object) -> MacOSError:
    return MacOSError(
        f"{operation} failed with AXError {error}",
        code=ErrorCode.AX_ERROR,
        details={"ax_error": int(error), "operation": operation, **details},
    )


def _ax_absent(error: int) -> bool:
    """Whether ``error`` is AX reporting that an attribute has nothing to
    say -- the element has no such attribute, or it has no value right
    now -- as opposed to a read that failed: the app did not answer, the
    element is gone, the API is off. A lossy read treats both as absent;
    a checked one (`MacOS._copy_attribute`) only the first."""
    return error in (AS.kAXErrorNoValue, AS.kAXErrorAttributeUnsupported)


class _AttributeValues(dict[str, Any | None]):
    """Best-effort attribute values without treating failed reads as absence."""

    __slots__ = ("complete",)

    def __init__(
        self, values: Iterable[tuple[str, Any | None]] = (), *, complete: bool = True
    ) -> None:
        super().__init__(values)
        self.complete = complete


class SearchMatches(list[dict[str, JSONValue]]):
    """The matches of one AX search, plus whether they are all of them.

    ``complete`` is True when no limit or failed read left part of the
    searched scope unexamined. ``visited`` counts the candidates returned
    by an app's optimized search, or nodes visited by the bounded walk.
    Exact waits require completeness before accepting one match;
    disappearance waits require it before accepting an empty search.
    """

    __slots__ = ("complete", "visited")

    def __init__(
        self,
        matches: Iterable[dict[str, JSONValue]] = (),
        *,
        complete: bool,
        visited: int,
    ) -> None:
        super().__init__(matches)
        self.complete = complete
        self.visited = visited


class _ExactSelector(NamedTuple):
    """The exact-equality half of a search: each field that is set must
    equal the element's attribute of the same name, character for
    character. Substring ``text`` narrows the candidates; this decides
    them."""

    title: str | None
    identifier: str | None
    description: str | None

    @classmethod
    def parse(
        cls, *, title: str | None, identifier: str | None, description: str | None
    ) -> _ExactSelector:
        validate_exact_selectors(
            title=title, identifier=identifier, description=description
        )
        return cls(title, identifier, description)

    @property
    def active(self) -> bool:
        return any(value is not None for value in self)

    @property
    def attributes(self) -> tuple[str, ...]:
        """The AX attributes a candidate must be read for before `matches`
        can judge it."""
        return tuple(
            source
            for source, target in _EXACT_ATTRIBUTES.items()
            if getattr(self, target) is not None
        )

    def matches(self, fields: Mapping[str, JSONValue]) -> bool:
        """Whether ``fields`` -- wire-named, as `_describe_element` builds
        them -- carries every set selector's value exactly."""
        return all(
            value is None or fields.get(name) == value
            for name, value in zip(self._fields, self, strict=True)
        )


_EXACT_ATTRIBUTES = {
    "AXTitle": "title",
    "AXIdentifier": "identifier",
    "AXDescription": "description",
}


class _TreeSnapshot(NamedTuple):
    """Visited nodes and the limits or failed reads that left gaps."""

    nodes: list[dict[str, JSONValue]]
    node_cut: bool
    depth_cut: bool
    read_cut: bool


_BACKENDS = ("python", "native", "auto")


def _resolve_backend(backend: str | None) -> str:
    """Resolve python/native/auto from an explicit value or MACOS_HARNESS_BACKEND."""
    if backend is None:
        backend = os.environ.get("MACOS_HARNESS_BACKEND", "python")
    normalized = str(backend).strip().casefold() or "python"
    if normalized not in _BACKENDS:
        raise MacOSError(
            f"Unknown backend {normalized!r}; choose one of: {', '.join(_BACKENDS)}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "backend", "value": normalized},
        )
    return normalized


def _split_scroll_delta(delta: int, maximum: int) -> list[int]:
    """Split a wheel delta into small exact steps accepted reliably by apps."""
    if maximum <= 0:
        raise ValueError("maximum must be positive")
    remaining = int(delta)
    steps: list[int] = []
    while remaining:
        step = max(-maximum, min(maximum, remaining))
        steps.append(step)
        remaining -= step
    return steps or [0]


class _Unresolved:
    """Sentinel type for ``MacOS._resolved_native_client``: its one
    instance below marks "no cached client, error, or fallback yet" --
    distinct from an ``auto`` backend's already-resolved ``None``.
    """

    __slots__ = ()


#: The single ``_Unresolved`` instance ever constructed.
_UNRESOLVED = _Unresolved()


def _finalize_native_session(session_box: list[Any | None]) -> None:
    """Finalizer callback for one ``MacOS`` instance's native agent child.

    Deliberately a free function taking only a plain mutable box, never a
    bound method or closure over the owning ``MacOS`` instance itself:
    ``MacOS`` and ``Accessibility`` hold references to each other
    (``self.ax = Accessibility(self)``), and a ``weakref.finalize``
    callback that captures its own target, even indirectly, keeps that
    target permanently unreachable-but-never-collectable. ``MacOS.close()``
    calls this same finalizer directly (idempotent: a finalizer only ever
    fires once), and it also runs automatically once the owning instance
    is unreachable or the interpreter exits, even if ``close()`` was
    never called at all.
    """
    session, session_box[0] = session_box[0], None
    if session is not None:
        session.close()


#: Every ``MacOS`` instance that might still hold a live native-agent
#: finalizer, tracked weakly so this registry never keeps an instance (or
#: anything it in turn keeps alive) reachable a moment longer than it
#: otherwise would be. Exists solely so a ``fork()`` elsewhere in the
#: embedding process can disarm every copied instance's finalizer before
#: any of the child's own code resumes -- see
#: ``_disarm_inherited_native_state_after_fork`` below.
_live_macos_instances: weakref.WeakSet[MacOS] = weakref.WeakSet()


def _disarm_inherited_native_state_after_fork() -> None:
    """Disarm every live ``MacOS`` instance's copied native-session
    finalizer in a freshly forked child, before any of the child's own
    code resumes.

    Registered once, process-wide, via ``os.register_at_fork`` below.
    Runs with exactly one thread alive (the thread that called
    ``fork()``), so it deliberately never acquires any instance's own
    ``_native_lock`` -- some *other* thread could have held it at the
    exact moment of ``fork()``, and that thread does not exist in this
    child at all. ``weakref.finalize.detach()`` marks a finalizer dead
    without ever invoking its callback: the ``AgentSession`` this child
    inherited a copy of is never closed, signaled, or otherwise touched
    from here -- it is the *parent* process's child to tear down, on the
    parent's own schedule. ``MacOS._check_native_owner`` independently
    fails closed if this child's own code goes on to use the inherited
    native state anyway (``close()``, ``_acquire_native()``), and
    ``native.py``'s own ``os.register_at_fork`` hook independently slams
    shut every live ``NativeClient``'s raw socket the same way.
    """
    for instance in list(_live_macos_instances):
        instance._native_finalizer.detach()


if hasattr(os, "register_at_fork"):  # pragma: no branch - always true on macOS
    os.register_at_fork(after_in_child=_disarm_inherited_native_state_after_fork)


class MacOS:
    """Low-level macOS observation and control for one persistent process."""

    # Identity for `inspect` and `diff`, held on the class so it reads as
    # "nothing inspected yet" and every write below replaces it with an
    # instance attribute. `_observation_refs` maps the accessibility
    # elements of the previous inspection to their `ref`; only that one
    # generation is kept.
    _observation_session: str | None = None
    _observation_sequence: int = 0
    _observation_refs: Mapping[Any, int] = MappingProxyType({})
    _observation_ref_seq: int = 0

    def __init__(self, *, backend: str | None = None) -> None:
        _require_macos()
        self._elements: dict[int, Any] = {}
        self._element_seq = 0
        self._last_app: dict[str, Any] | None = None
        self._last_windows: list[dict[str, Any]] = []
        self._last_screenshot: dict[str, Any] | None = None
        self._event_source = AS.CGEventSourceCreate(AS.kCGEventSourceStatePrivate)
        if self._event_source is None:
            raise MacOSError(
                "Could not create a private Core Graphics event source",
                code=ErrorCode.AX_ERROR,
            )
        # A fresh private source suppresses the user's own hardware input
        # for 250 ms after every posted event. Nothing here needs that
        # quiet window, and a typing loop would turn it into a stuck
        # keyboard, so let every local event through in both states.
        AS.CGEventSourceSetLocalEventsSuppressionInterval(self._event_source, 0.0)
        for state in (
            AS.kCGEventSuppressionStateRemoteMouseDrag,
            AS.kCGEventSuppressionStateSuppressionInterval,
        ):
            AS.CGEventSourceSetLocalEventsFilterDuringSuppressionState(
                self._event_source, AS.kCGEventFilterMaskPermitAllEvents, state
            )

        from .controls import Accessibility
        from .ops import Operations
        from .routes import Routes

        self._pointer_position: tuple[float, float] | None = None
        self._overlay = LivePointerOverlay()
        self.ax = Accessibility(self)
        self.do = Operations(self)
        self.route = Routes(self)
        self._backend = _resolve_backend(backend)
        self._native_client: NativeClient | None = None
        self._native_error: Exception | None = None
        self._native_closed = False
        self._native_lock = threading.Lock()
        # A plain mutable box, not a `self._native_session` attribute: the
        # finalizer below must never capture `self` (see
        # `_finalize_native_session`), so it is handed this box instead,
        # and `close()`/`_acquire_native()` mutate its one slot in place.
        self._native_session_box: list[Any | None] = [None]
        self._native_finalizer = weakref.finalize(
            self, _finalize_native_session, self._native_session_box
        )
        # See `_check_native_owner` below: a forked child inherits a
        # byte-for-byte copy of this instance, including `_native_lock`,
        # without ever going through `__init__` again -- recorded here so
        # every later native-session entry point can tell.
        self._creator_pid = os.getpid()
        _live_macos_instances.add(self)

    def timeline(self) -> list[dict[str, JSONValue]]:
        """Return recent receipted operations without observing the desktop."""
        return [receipt.to_json() for receipt in self.do.history()]

    def _diagnostic_identity(self, app: str | int | Receipt) -> _AppIdentity:
        if not isinstance(app, Receipt):
            return self._process_identity(app)
        process, target = app.process, app.target
        info = target.get("app") if isinstance(target, Mapping) else None
        if not isinstance(process, Mapping) or not isinstance(info, Mapping):
            raise MacOSError("Receipt has no bound app identity; select an app explicitly",
                             code=ErrorCode.BAD_REQUEST)
        pid, launched = process.get("pid"), process.get("launched_at")
        if type(pid) is not int or type(launched) not in (int, float):
            raise MacOSError("Receipt has no process launch identity", code=ErrorCode.BAD_REQUEST)
        bundle, name, path = info.get("bundle_id"), info.get("name"), info.get("path")
        return _AppIdentity(pid, bundle if isinstance(bundle, str) else None, launched,
                            name if isinstance(name, str) else "", path if isinstance(path, str) else None)

    def _status_identity(self, identity: _AppIdentity) -> dict[str, JSONValue]:
        return {
            "app": {"pid": identity.pid, "name": identity.name,
                    "bundle_id": identity.bundle_id, "path": identity.path},
            "process": self._observe_process(identity),
            "build": self._build_status(identity), "observed_at": _utc_timestamp(time.time()),
        }

    def status(self, app: str | int | Receipt) -> dict[str, JSONValue]:
        """Read app lifetime and on-disk build metadata without reading its UI."""
        self.do._check_owner()
        return self._status_identity(self._diagnostic_identity(app))

    def inspect(
        self, app: str | int | Receipt, *, max_depth: int = 12, max_nodes: int = 300,
        include_values: bool = False, screenshot: bool = False,
    ) -> dict[str, JSONValue]:
        """Collect a bounded, non-enhancing snapshot; values and capture are opt-in."""
        from .diagnostics import inspection_findings

        self.do._check_owner()
        if type(max_nodes) is not int or not 1 <= max_nodes <= 5000:
            raise MacOSError("max_nodes must be between 1 and 5000", code=ErrorCode.BAD_REQUEST)
        if type(max_depth) is not int or not 0 <= max_depth <= 25:
            raise MacOSError("max_depth must be between 0 and 25", code=ErrorCode.BAD_REQUEST)
        if not isinstance(include_values, bool) or not isinstance(screenshot, bool):
            raise MacOSError("include_values and screenshot must be booleans", code=ErrorCode.BAD_REQUEST)
        with self.do._dispatch_lock:
            identity = self._diagnostic_identity(app)
            status = self._status_identity(identity)
            process = status["process"]
            if isinstance(process, Mapping) and process.get("state") == "running":
                try:
                    state = self.get_app_state(
                        identity.pid, max_depth=max_depth, max_nodes=max_nodes,
                        include_values=include_values, screenshot=screenshot,
                        include_actions=False, include_settable=False, enhance=False,
                    )
                except MacOSError as exc:
                    state = {"error": exc.to_json()}
                try:
                    state["focus"] = self._focus_sample(identity.pid, include_values=include_values)
                except MacOSError as exc:
                    state["focus_error"] = exc.to_json()
                state.update(status)
                state["process"] = self._observe_process(identity)
            else:
                state = status
            state["observed_at"] = _utc_timestamp(time.time())
            self._identify_observation(state)
            state.update(inspection_findings(state, app if isinstance(app, Receipt) else None))
            return state

    def _identify_observation(self, state: dict[str, JSONValue]) -> None:
        """Give each inspected control a `ref` that survives one re-inspection.

        `element_index` cannot: `_snapshot_tree` clears `self._elements`
        and `_remember_element` hands out the next unused integer, so the
        same control is numbered differently in every snapshot and
        comparing those numbers would read an untouched window as wholly
        removed and re-added. A node keeps its previous `ref` whenever
        accessibility returns an element equal to one seen last time --
        `CFEqual`, the same equality the tree walk already uses to avoid
        visiting a control twice -- and gets a fresh one otherwise.

        Only the previous inspection's elements are retained, so this
        holds one extra reference per node and costs one dictionary
        lookup each. The price is that only consecutive inspections can
        be compared, which `state["observation"]` records: the session
        that numbered these refs, this inspection's number, and the
        number of the inspection its refs were matched against.
        `diagnostics.diff_inspections` refuses every other pair rather
        than trusting two integers that happen to agree.
        """
        nodes = state.get("nodes")
        if not isinstance(nodes, list):
            return
        previous, refs = self._observation_refs, {}
        ref_seq = self._observation_ref_seq
        for node in nodes:
            element = self._elements[node["element_index"]]
            ref = previous.get(element)
            if ref is None:
                ref, ref_seq = ref_seq, ref_seq + 1
            refs[element] = ref
            node["ref"] = ref
        if self._observation_session is None:
            self._observation_session = uuid.uuid4().hex
        state["observation"] = {
            "session": self._observation_session,
            "sequence": self._observation_sequence + 1,
            "previous": self._observation_sequence or None,
        }
        self._observation_refs, self._observation_ref_seq = refs, ref_seq
        self._observation_sequence += 1

    def _diagnostic_pid(self, subject: Receipt | tuple[str, str], app: str | int | None) -> int:
        from .diagnostics import receipt_pid

        if isinstance(subject, Receipt):
            if app is not None:
                raise MacOSError("A receipt already supplies the target app", code=ErrorCode.BAD_REQUEST)
            return receipt_pid(subject)
        if isinstance(app, int) and not isinstance(app, bool) and app > 0:
            return app
        if isinstance(app, str):
            return self._process_identity(app).pid
        raise MacOSError("An explicit time interval needs an app or pid", code=ErrorCode.BAD_REQUEST)

    def logs(
        self, subject: Receipt | tuple[str, str], *, app: str | int | None = None,
        subsystem: str | None = None, category: str | None = None, level: str = "info",
        limit: int = 200, timeout: float = 5.0, max_bytes: int = 1024 * 1024,
    ) -> dict[str, JSONValue]:
        """Read bounded unified logs for an action or explicit UTC interval."""
        from .diagnostics import collect_logs

        self.do._check_owner()
        return collect_logs(subject, self._diagnostic_pid(subject, app), subsystem=subsystem,
                            category=category, level=level, limit=limit, timeout=timeout, max_bytes=max_bytes)

    def crashes(
        self, subject: Receipt | tuple[str, str], *, app: str | int | None = None,
        limit: int = 3, max_files: int = 128,
    ) -> dict[str, JSONValue]:
        """Look up bounded modern crash reports; absence is only this lookup's result."""
        from .diagnostics import collect_crashes

        self.do._check_owner()
        return collect_crashes(subject, self._diagnostic_pid(subject, app), limit=limit, max_files=max_files,
                               directories=(Path.home() / "Library/Logs/DiagnosticReports",
                                            Path("/Library/Logs/DiagnosticReports")))

    def sample(
        self, app: str | int | Receipt, *, duration: int = 1, timeout: float = 5.0,
        max_bytes: int = 1024 * 1024,
    ) -> dict[str, JSONValue]:
        """Collect a bounded call graph explicitly, without diagnosing a hang from it."""
        from .diagnostics import collect_sample

        self.do._check_owner()
        identity = self._diagnostic_identity(app)
        process = self._observe_process(identity)
        if process.get("state") != "running":
            return {"kind": "sample", "status": "failed", "pid": identity.pid, "process": process,
                    "error": MacOSError("The bound app is no longer available for sampling",
                                        code=ErrorCode.APP_EXITED if process.get("state") == "exited"
                                        else ErrorCode.UNSUPPORTED_OP).to_json()}
        result = collect_sample(identity.pid, duration=duration, timeout=timeout, max_bytes=max_bytes)
        result["process"] = self._observe_process(identity)
        return result

    @staticmethod
    def explain(receipt: Receipt, *evidence: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Explain a receipt using only supplied evidence, without a fresh observation."""
        from .diagnostics import explain

        return explain(receipt, evidence)

    @staticmethod
    def diff_windows(before: Mapping[str, JSONValue], after: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Compare supplied window snapshots without observing the desktop."""
        from .diagnostics import diff_windows

        return diff_windows(before, after)

    @staticmethod
    def diff(before: Mapping[str, JSONValue], after: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
        """Compare two consecutive inspections of one app without observing it again."""
        from .diagnostics import diff_inspections

        return diff_inspections(before, after)

    def _check_native_owner(self) -> None:
        """Fail closed before touching ``_native_lock`` from a forked child.

        Called first thing by every method that acquires ``_native_lock``
        (``close()``, ``_resolved_native_client()`` on behalf of
        ``_acquire_native()``) -- never from inside the ``with
        self._native_lock:`` block itself. If some other thread of the
        parent process held that lock at the exact instant of a
        ``fork()`` elsewhere in the embedding application, it stays held
        forever in a forked child (that thread does not exist here to
        release it); checking ownership before ever attempting to
        acquire it turns what would otherwise be a silent, permanent
        hang into this immediate, explicit error instead.
        """
        pid = os.getpid()
        if pid != self._creator_pid:
            raise MacOSError(
                f"This MacOS instance was created in pid {self._creator_pid} "
                f"and cannot be used from pid {pid} (a fork boundary was "
                "crossed); construct a fresh MacOS in this process instead",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"creator_pid": self._creator_pid, "pid": pid},
            )

    def close(self) -> None:
        """Tear down this instance's private native agent child, if any.

        Idempotent: safe to call more than once, and safe when no native
        session was ever launched (the common, default ``python`` backend
        never opens a socket at all). Once closed, any further attempt to
        route a call through the native agent raises explicitly rather
        than silently relaunching a new child or falling back to another
        backend — construct a new ``MacOS`` to use ``native``/``auto``
        again. Also runs automatically, via the same finalizer this calls
        directly, if this instance becomes unreachable or the interpreter
        exits without an explicit ``close()``.

        Raises if called on a forked child's inherited copy of this
        instance (see ``_check_native_owner``); the automatic finalizer
        path a fork alone triggers is never affected by this raise, since
        ``_disarm_inherited_native_state_after_fork`` (module level, see
        above) already detaches every live instance's finalizer in the
        child before any of its own code -- including this method -- ever
        runs.
        """
        self._check_native_owner()
        with self._native_lock:
            self._native_closed = True
            self._native_client = None
        self._native_finalizer()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- permissions and app discovery ---------------------------------

    def is_accessibility_trusted(self) -> bool:
        return bool(AS.AXIsProcessTrusted())

    def request_accessibility_permission(self) -> bool:
        """Show Apple's Accessibility permission prompt and return current trust."""
        options = {AS.kAXTrustedCheckOptionPrompt: True}
        return bool(AS.AXIsProcessTrustedWithOptions(options))

    @staticmethod
    def _preflight_permission(name: str) -> bool | None:
        function = getattr(AS, name, None)
        return None if function is None else bool(function())

    def permissions(self) -> dict[str, bool | str | None]:
        """Return non-prompting checks for the permissions this harness uses."""
        return {
            "accessibility": self.is_accessibility_trusted(),
            "screen_recording": self._preflight_permission(
                "CGPreflightScreenCaptureAccess"
            ),
            "post_events": self._preflight_permission("CGPreflightPostEventAccess"),
            "automation": "per-target; requested by macOS on first Apple Event",
        }

    def request_permissions(self) -> dict[str, bool | str | None]:
        """Ask macOS for missing global permissions, then return current status."""
        self.request_accessibility_permission()
        for name in ("CGRequestScreenCaptureAccess", "CGRequestPostEventAccess"):
            function = getattr(AS, name, None)
            if function is not None:
                function()
        return self.permissions()

    def doctor(self) -> dict[str, Any]:
        return {
            "platform": "macOS",
            "permissions": self.permissions(),
            "input_monitoring_required": False,
        }

    def handoff(
        self,
        *,
        reason: HandoffReason | str,
        app: str | int,
    ) -> HumanHandoff:
        """Describe a known human-only security boundary without acting.

        This reads only running-application identity and current frontmost
        identity. It never opens, activates, inspects, or changes the target.
        """
        try:
            normalized_reason = HandoffReason(reason)
        except (TypeError, ValueError):
            raise MacOSError(
                "Handoff reason must be 'authentication_required' or "
                "'account_recovery_required'",
                code=ErrorCode.BAD_REQUEST,
                details={
                    "parameter": "reason",
                    "valid_reasons": [item.value for item in HandoffReason],
                },
            ) from None

        invalid_app = (
            isinstance(app, bool)
            or not isinstance(app, (str, int))
            or (isinstance(app, str) and not app.strip())
            or (isinstance(app, int) and app <= 0)
        )
        if invalid_app:
            raise MacOSError(
                "Handoff app must be a nonempty app name, bundle ID, path, or "
                "positive PID",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )

        try:
            _, target = self._resolve_app(app)
        except ApplicationNotFoundError:
            raise ApplicationNotFoundError(
                "Handoff target app is not running",
                details={"parameter": "app"},
            ) from None
        except MacOSError as exc:
            if exc.code != ErrorCode.APP_AMBIGUOUS:
                raise
            raise MacOSError(
                "Handoff target app is ambiguous",
                code=ErrorCode.APP_AMBIGUOUS,
                details={"parameter": "app"},
            ) from None
        frontmost = self._frontmost_app()
        return HumanHandoff(
            reason=normalized_reason,
            target_is_frontmost=(
                frontmost is not None and int(frontmost["pid"]) == int(target["pid"])
            ),
        )

    def list_apps(self) -> list[dict[str, Any]]:
        if self._backend != "python":
            client = self._acquire_native()
            if client is not None:
                return client.list_apps()
        apps: list[dict[str, Any]] = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            info = self._app_info(app)
            if not info["name"]:
                continue
            apps.append(info)
        return sorted(apps, key=lambda item: (item["name"].casefold(), item["pid"]))

    @staticmethod
    def _app_info(app: Any) -> dict[str, Any]:
        name = app.localizedName()
        bundle_id = app.bundleIdentifier()
        path = app.bundleURL().path() if app.bundleURL() is not None else None
        return {
            "name": str(name or bundle_id or path or ""),
            "bundle_id": str(bundle_id) if bundle_id else None,
            "pid": int(app.processIdentifier()),
            "path": str(path) if path else None,
        }

    def _process_identity(self, query: str | int) -> _AppIdentity:
        try:
            _, info = self._resolve_app(query)
        except ApplicationNotFoundError:
            if not isinstance(query, int) or isinstance(query, bool):
                raise
            # Command-line processes have a kernel identity without an AppKit record.
            info = {"pid": query}
        return self._identity_from_info(info)

    @staticmethod
    def _identity_from_info(info: Mapping[str, JSONValue]) -> _AppIdentity:
        pid = info.get("pid")
        if type(pid) is not int or pid <= 0:
            raise MacOSError("Resolved app has no valid pid", code=ErrorCode.AX_ERROR)
        bundle_id, name, path = info.get("bundle_id"), info.get("name"), info.get("path")
        return _AppIdentity(
            pid=pid, launched_at=_process_start_time(pid),
            bundle_id=bundle_id if isinstance(bundle_id, str) else None,
            name=name if isinstance(name, str) else "",
            path=path if isinstance(path, str) else None,
        )

    def _observe_process(self, expected: _AppIdentity) -> dict[str, JSONValue]:
        observation: dict[str, JSONValue] = {
            "pid": expected.pid, "launched_at": expected.launched_at,
        }
        try:
            observation["state"] = "running" if self._same_process(expected) else "exited"
        except MacOSError as exc:
            observation.update(state="unknown", error=exc.to_json())
        return observation

    def _same_process(self, expected: _AppIdentity) -> bool:
        try:
            current = self._process_identity(expected.pid)
        except ApplicationNotFoundError:
            return False
        return (current.pid, current.bundle_id, current.launched_at) == (
            expected.pid, expected.bundle_id, expected.launched_at
        )

    @staticmethod
    def _bundle_info(path: str | None) -> dict[str, object]:
        if path is None:
            return {}
        try:
            with (Path(path) / "Contents" / "Info.plist").open("rb") as source:
                info = plistlib.load(source)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return {}
        return info if isinstance(info, dict) else {}

    @classmethod
    def _bundle_version(cls, path: str | None) -> tuple[str | None, str | None]:
        info = cls._bundle_info(path)
        version = info.get("CFBundleShortVersionString")
        build = info.get("CFBundleVersion")
        return (version if isinstance(version, str) else None,
                build if isinstance(build, str) else None)

    def _build_status(self, identity: _AppIdentity) -> dict[str, JSONValue]:
        info = self._bundle_info(identity.path)
        version, build = info.get("CFBundleShortVersionString"), info.get("CFBundleVersion")
        result: dict[str, JSONValue] = {
            "on_disk_version": version if isinstance(version, str) else None,
            "on_disk_build": build if isinstance(build, str) else None,
            "executable_modified_at": None, "potentially_stale": None,
        }
        executable = info.get("CFBundleExecutable")
        if (identity.path is None or not isinstance(executable, str)
                or not executable or Path(executable).name != executable):
            return result
        try:
            modified = (Path(identity.path) / "Contents" / "MacOS" / executable).stat().st_mtime
        except OSError as exc:
            result["error"] = str(exc)
        else:
            result["executable_modified_at"] = _utc_timestamp(modified)
            result["potentially_stale"] = modified > identity.launched_at
        return result

    @classmethod
    def _frontmost_app(cls) -> dict[str, Any] | None:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return None if app is None else cls._app_info(app)

    def _guard_focus(
        self,
        before: dict[str, Any] | None,
        target_pid: int,
        operation: str,
    ) -> None:
        after = self._frontmost_app()
        if (
            before is not None
            and int(before["pid"]) != target_pid
            and after is not None
            and int(after["pid"]) == target_pid
        ):
            raise FocusChangedError(
                f"{after['name']} became frontmost during {operation}; stopped",
                details={
                    "operation": operation,
                    "target_pid": target_pid,
                    "frontmost": after,
                },
            )

    def _resolve_app(self, query: str | int | None) -> tuple[Any, dict[str, Any]]:
        if not query and self._last_app:
            query = self._last_app["pid"]
        if not query:
            raise MacOSError(
                "Specify an app name, bundle ID, path, or PID",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )

        if isinstance(query, int):
            # An exact pid resolves directly against the process table --
            # no need to enumerate every running application to find one
            # match by exact value, and no risk of the notification-cache
            # staleness `NSWorkspace.runningApplications()` can have in a
            # process that never pumps its own run loop (a freshly
            # launched app can otherwise appear to not exist yet, purely
            # as an artifact of when this process last observed a change).
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(query)
            if app is None or app.isTerminated():
                raise ApplicationNotFoundError(
                    f"No running application matches {query!r}",
                    details={"query": query},
                )
            return app, self._app_info(app)

        needle = str(query).casefold()
        candidates: list[tuple[Any, dict[str, Any]]] = []
        exact: list[tuple[Any, dict[str, Any]]] = []
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            info = self._app_info(app)
            values = [str(info["pid"]), info["name"], info["bundle_id"], info["path"]]
            lowered = [str(value).casefold() for value in values if value]
            if needle in lowered:
                exact.append((app, info))
            elif any(needle in value for value in lowered):
                candidates.append((app, info))

        matches = exact or candidates
        if not matches:
            raise ApplicationNotFoundError(
                f"No running application matches {query!r}",
                details={"query": query},
            )
        if len(matches) > 1:
            ranked = self._rank_matches(matches)
            names = ", ".join(self._describe_match(info) for info in ranked[:8])
            raise MacOSError(
                f"Application query {query!r} is ambiguous; pass a pid: {names}",
                code=ErrorCode.APP_AMBIGUOUS,
                details={"query": query, "matches": ranked},
            )
        return matches[0]

    @staticmethod
    def _launched_seconds_ago(app: Any) -> float | None:
        launched = app.launchDate()
        if launched is None:
            return None
        return max(0.0, -float(launched.timeIntervalSinceNow()))

    def _rank_matches(
        self, matches: list[tuple[Any, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Order ambiguous matches by the evidence a caller picks a pid on.

        Two processes with one name are common: a relaunch that has not
        exited yet, or a helper beside its parent. The frontmost one, then
        the one with on-screen windows, then the newest, is almost always
        the one meant -- but the choice stays with the caller, so this
        only ranks and reports, never picks.
        """
        frontmost = self._frontmost_app()
        frontmost_pid = None if frontmost is None else int(frontmost["pid"])
        ranked: list[dict[str, Any]] = []
        for app, info in matches:
            on_screen = [
                window for window in self.windows(info["pid"]) if window["on_screen"]
            ]
            ranked.append(
                {
                    **info,
                    "frontmost": info["pid"] == frontmost_pid,
                    "on_screen_windows": len(on_screen),
                    "launched_seconds_ago": self._launched_seconds_ago(app),
                }
            )
        ranked.sort(
            key=lambda item: (
                not item["frontmost"],
                -item["on_screen_windows"],
                math.inf
                if item["launched_seconds_ago"] is None
                else item["launched_seconds_ago"],
                item["pid"],
            )
        )
        return ranked

    @staticmethod
    def _describe_match(info: dict[str, Any]) -> str:
        notes = []
        if info["frontmost"]:
            notes.append("frontmost")
        notes.append(f"{info['on_screen_windows']} on-screen window(s)")
        age = info["launched_seconds_ago"]
        if age is not None:
            notes.append(f"launched {age:.0f}s ago")
        return f"{info['name']} ({info['pid']}: {', '.join(notes)})"

    # --- AX tree ---------------------------------------------------------

    def _ensure_accessibility(self) -> None:
        if not self.is_accessibility_trusted():
            raise AccessibilityPermissionError(
                "Accessibility permission is required. Grant it to the terminal or "
                "agent host in System Settings → Privacy & Security → Accessibility, "
                "or call mac.request_accessibility_permission() to show Apple's prompt.",
                details={"permission": "accessibility"},
            )

    def _ensure_screen_recording(self) -> None:
        if self._preflight_permission("CGPreflightScreenCaptureAccess") is False:
            raise AccessibilityPermissionError(
                "Screen Recording permission is required. Grant it to the terminal "
                "or agent host in System Settings → Privacy & Security → Screen & "
                "System Audio Recording, or call mac.request_permissions().",
                details={"permission": "screen_recording"},
            )

    def _ensure_post_events(self) -> None:
        if self._preflight_permission("CGPreflightPostEventAccess") is False:
            raise AccessibilityPermissionError(
                "Permission to post input events is required. Grant control access "
                "to the terminal or agent host, or call mac.request_permissions().",
                details={"permission": "post_events"},
            )

    @staticmethod
    def _application_element(
        pid: int,
        *,
        messaging_timeout: float | None = None,
        enhance: bool = True,
    ) -> Any:
        """Create the AX root for ``pid``.

        ``enhance`` sets ``AXEnhancedUserInterface``, the "a screen reader
        is running" signal Chromium and Electron use to build their full
        tree. It also changes how AppKit apps animate and lay out, so a
        caller that only needs the root can pass ``enhance=False``.
        """
        root = AS.AXUIElementCreateApplication(pid)
        if messaging_timeout is not None:
            error = AS.AXUIElementSetMessagingTimeout(root, messaging_timeout)
            if error != _AX_SUCCESS:
                raise _ax_error("Set AX messaging timeout", error, pid=pid)
        if not enhance:
            return root
        error, enhanced = AS.AXUIElementCopyAttributeValue(
            root, "AXEnhancedUserInterface", None
        )
        if error == _AX_SUCCESS and not enhanced:
            AS.AXUIElementSetAttributeValue(root, "AXEnhancedUserInterface", True)
            time.sleep(0.05)
        return root

    @staticmethod
    def _copy_attribute(element: Any, attribute: str, *, checked: bool = False) -> Any | None:
        """One attribute, ``None`` when the element has nothing to report
        for it. A read that failed outright is ``None`` too, unless
        ``checked``, where it raises: a witness must not pass a refused
        read off as an absence (see `_ax_absent`)."""
        error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        if error == _AX_SUCCESS:
            return value
        if checked and not _ax_absent(error):
            raise _ax_error(f"Read {attribute}", error)
        return None

    @staticmethod
    def _copy_attributes(
        element: Any, attributes: Iterable[str], *, checked: bool = False
    ) -> _AttributeValues:
        """Read AX attributes in one application round trip when supported.

        An unsupported batch API falls back to single reads. A failed
        batch stays incomplete rather than repeating its failure for each
        attribute. ``checked`` raises for failed reads on either path.
        """
        names = tuple(dict.fromkeys(str(attribute) for attribute in attributes))
        if not names:
            return _AttributeValues()
        try:
            error, values = AS.AXUIElementCopyMultipleAttributeValues(
                element, names, 0, None
            )
        except (AttributeError, TypeError, ValueError):
            error, values = AS.kAXErrorNotImplemented, None
        if error not in (_AX_SUCCESS, AS.kAXErrorNotImplemented, AS.kAXErrorAttributeUnsupported):
            if checked:
                raise _ax_error("Read attribute batch", error)
            return _AttributeValues(((name, None) for name in names), complete=False)
        result = _AttributeValues()
        if error != _AX_SUCCESS or values is None or len(values) != len(names):
            for name in names:
                try:
                    result[name] = MacOS._copy_attribute(element, name, checked=True)
                except MacOSError:
                    if checked:
                        raise
                    result[name] = None
                    result.complete = False
            return result

        for name, value in zip(names, values, strict=True):
            slot_error = MacOS._slot_error(value)
            if slot_error is not None:
                if not _ax_absent(slot_error):
                    if checked:
                        raise _ax_error(f"Read {name}", slot_error)
                    result.complete = False
                value = None
            result[name] = value
        return result

    @staticmethod
    def _slot_error(value: Any) -> int | None:
        """The AXError an ``AXUIElementCopyMultipleAttributeValues`` slot
        carries in place of a value, ``None`` for a real value."""
        try:
            value_type = AS.AXValueGetType(value)
        except (TypeError, ValueError):
            return None
        if value_type != AS.kAXValueAXErrorType:
            return None
        # The type was just checked, so the decode cannot fail.
        _, code = AS.AXValueGetValue(value, AS.kAXValueAXErrorType, None)
        return int(code)

    @staticmethod
    def _actions(element: Any) -> list[str]:
        error, value = AS.AXUIElementCopyActionNames(element, None)
        return [str(item) for item in value] if error == _AX_SUCCESS and value else []

    @staticmethod
    def _settable(element: Any, attribute: str) -> bool:
        error, value = AS.AXUIElementIsAttributeSettable(element, attribute, None)
        return bool(value) if error == _AX_SUCCESS else False

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> dict[str, JSONValue]:
        """What has keyboard focus in ``pid`` right now, in one cheap reading.

        Up to five AX calls and no ``AXEnhancedUserInterface`` toggle, so
        it costs about a millisecond and is safe to take before and after
        every posted input. The root's focused window and focused element
        are read one at a time: Safari answers a batched read of the two
        with no element (10 of 10 tries) while single reads find it every
        time. Then the window's title, the focused element's identity,
        and -- only when that identity is not a secure field -- its
        details, so a password's value, length and selection are never
        requested. ``value`` is the raw attribute; a receipt summarizes
        it before storing.

        Every read is checked: one the app refuses raises `MacOSError`
        rather than reading as an absence, so a sample is either a whole
        observation or no observation. A refused identity read therefore
        never lets the details be requested, and a focused element that
        stopped answering never counts as focus having moved.
        """
        root = self._application_element(pid, enhance=False)
        window = self._copy_attribute(root, "AXFocusedWindow", checked=True)
        focused = self._copy_attribute(root, "AXFocusedUIElement", checked=True)
        frontmost = self._frontmost_app()
        sample: dict[str, JSONValue] = {
            "frontmost_pid": None if frontmost is None else int(frontmost["pid"]),
            "window": None
            if window is None
            else self._jsonable(self._copy_attribute(window, "AXTitle", checked=True)),
            "focused": None,
        }
        if focused is None:
            return sample
        attributes = self._copy_attributes(focused, _FOCUS_IDENTITY_ATTRIBUTES, checked=True)
        if attributes.get("AXSubrole") != _SECURE_SUBROLE:
            details = _FOCUS_DETAIL_ATTRIBUTES if include_values else (
                name for name in _FOCUS_DETAIL_ATTRIBUTES if name not in _SENSITIVE_ATTRIBUTES
            )
            attributes.update(self._copy_attributes(focused, details, checked=True))
        names = {**_FOCUS_IDENTITY_ATTRIBUTES, **_FOCUS_DETAIL_ATTRIBUTES}
        sample["focused"] = {
            names[name]: self._jsonable(value)
            for name, value in attributes.items()
            if value is not None
        }
        return sample

    @staticmethod
    def _is_ax_element(value: Any) -> bool:
        try:
            return AS.CFGetTypeID(value) == AS.AXUIElementGetTypeID()
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _finite_float(value: float, *, field: str | None = None) -> float:
        """Coerce ``value`` to a JSON-safe finite ``float``.

        ``AXValueGetValue`` and plain AX attribute reads can hand back a
        NaN or infinity (a mis-measured element geometry, a stale layout
        pass, ...); those have no JSON representation and
        ``Receipt.canonicalize`` raises ``ValueError`` on them well after
        the fact, with no machine-readable code. Reject them here, the
        single place raw AX values become ``dict``/``list``/scalar JSON
        values, so every caller -- ``mac.do``, snapshotting, attribute
        reads -- gets a structured ``MacOSError`` before the value ever
        reaches a ``Receipt``.
        """
        value = float(value)
        if math.isfinite(value):
            return value
        details: dict[str, object] = {"value": str(value)}
        if field is not None:
            details["field"] = field
        raise MacOSError(
            "Accessibility API returned a non-finite value with no JSON "
            "representation",
            code=ErrorCode.AX_ERROR,
            details=details,
        )

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if value is None or isinstance(value, (int, bool)):
            return value
        if isinstance(value, str):
            # An AX string arrives as `pyobjc_unicode`; a receipt's value
            # summary names the type, so hand back the plain `str`.
            return str(value)
        if isinstance(value, float):
            return MacOS._finite_float(value)
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, (list, tuple)):
            return [
                MacOS._jsonable(item)
                for item in value
                if not MacOS._is_ax_element(item)
            ]
        if isinstance(value, dict):
            return {str(key): MacOS._jsonable(item) for key, item in value.items()}

        try:
            value_type = AS.AXValueGetType(value)
        except (TypeError, ValueError):
            return str(value)

        kinds = {
            AS.kAXValueCGPointType: "point",
            AS.kAXValueCGSizeType: "size",
            AS.kAXValueCGRectType: "rect",
            AS.kAXValueCFRangeType: "range",
        }
        kind = kinds.get(value_type)
        if kind is None:
            return str(value)
        ok, decoded = AS.AXValueGetValue(value, value_type, None)
        if not ok:
            return str(value)
        if kind == "point":
            return {
                "x": MacOS._finite_float(decoded.x, field="x"),
                "y": MacOS._finite_float(decoded.y, field="y"),
            }
        if kind == "size":
            return {
                "width": MacOS._finite_float(decoded.width, field="width"),
                "height": MacOS._finite_float(decoded.height, field="height"),
            }
        if kind == "rect":
            return {
                "x": MacOS._finite_float(decoded.origin.x, field="x"),
                "y": MacOS._finite_float(decoded.origin.y, field="y"),
                "width": MacOS._finite_float(decoded.size.width, field="width"),
                "height": MacOS._finite_float(decoded.size.height, field="height"),
            }
        # A decoded ``CFRange`` arrives as a plain ``(location, length)``
        # tuple, not a struct wrapper with named fields.
        location, length = decoded
        return {"location": int(location), "length": int(length)}

    def _snapshot_tree(
        self,
        root: Any,
        *,
        max_depth: int,
        max_nodes: int,
        include_menu_bar: bool,
        attributes: Iterable[str] = _AX_ATTRIBUTES,
        extra_attributes: Iterable[str] = (),
        include_actions: bool = True,
        include_settable: bool = True,
        reset_elements: bool = True,
    ) -> _TreeSnapshot:
        if reset_elements:
            self._elements = {}
        nodes: list[dict[str, Any]] = []
        seen: set[Any] = set()
        node_cut = depth_cut = read_cut = False
        requested_attributes = tuple(
            dict.fromkeys(
                (
                    *(str(item) for item in attributes),
                    "AXRole",
                    "AXChildren",
                    "AXWindows",
                    *(str(item) for item in extra_attributes),
                )
            )
        )
        standard_attributes = {
            *(str(item) for item in attributes),
            "AXRole",
            "AXChildren",
            "AXWindows",
        }

        safe_attributes = tuple(name for name in requested_attributes if name not in _SENSITIVE_ATTRIBUTES)
        reads_values = len(safe_attributes) != len(requested_attributes)

        def visit(element: Any, depth: int) -> None:
            nonlocal node_cut, depth_cut, read_cut
            if element in seen:
                return
            if depth > max_depth:
                depth_cut = True
                return
            if len(nodes) >= max_nodes:
                node_cut = True
                return
            seen.add(element)

            if reads_values:
                identity = self._copy_attributes(element, _FOCUS_IDENTITY_ATTRIBUTES)
                allowed = identity.complete and identity.get("AXSubrole") != _SECURE_SUBROLE
                raw = self._copy_attributes(element, requested_attributes if allowed else safe_attributes)
                raw.complete = raw.complete and identity.complete
            else:
                raw = self._copy_attributes(element, requested_attributes)
            read_cut = read_cut or not raw.complete
            if raw.get("AXRole") == "AXMenuBar" and not include_menu_bar:
                return
            index = self._remember_element(element)
            node: dict[str, Any] = {
                "element_index": index,
                "depth": depth,
                "role": self._jsonable(raw.get("AXRole")),
            }
            for source, target in _AX_NODE_MAPPING.items():
                value = self._jsonable(raw.get(source))
                if value not in (None, "", [], {}):
                    node[target] = value
            extra = {
                name: self._jsonable(raw.get(name))
                for name in requested_attributes
                if name not in standard_attributes
                and self._jsonable(raw.get(name)) not in (None, "", [], {})
            }
            if extra:
                node["attributes"] = extra
            if include_actions:
                actions = self._actions(element)
                if actions:
                    node["actions"] = actions
            if include_settable:
                settable = [
                    name
                    for name in _SETTABLE_CANDIDATES
                    if self._settable(element, name)
                ]
                if settable:
                    node["settable"] = settable
            nodes.append(node)

            children = raw.get("AXChildren")
            if not children and depth == 0:
                children = raw.get("AXWindows")
            if isinstance(children, Iterable) and not isinstance(
                children, (str, bytes, dict)
            ):
                for child in children:
                    if self._is_ax_element(child):
                        visit(child, depth + 1)

        visit(root, 0)
        return _TreeSnapshot(nodes, node_cut, depth_cut, read_cut)

    @staticmethod
    def _render_tree(nodes: list[dict[str, Any]], *, truncated: bool = False) -> str:
        lines: list[str] = []
        for node in nodes:
            parts = [str(node["element_index"]), str(node.get("role") or "AXUnknown")]
            for key in ("subrole", "title", "description", "value"):
                if key in node:
                    encoded = json.dumps(_truncate(str(node[key])), ensure_ascii=False)
                    parts.append(f"{key}={encoded}")
            if "frame" in node:
                frame = node["frame"]
                if isinstance(frame, dict):
                    parts.append(
                        "frame=({x:g},{y:g},{width:g},{height:g})".format(**frame)
                    )
            if node.get("settable"):
                parts.append(f"settable={','.join(node['settable'])}")
            if node.get("actions"):
                actions = ",".join(
                    _truncate(str(action), 80) for action in node["actions"]
                )
                parts.append(f"actions={actions}")
            lines.append("  " * int(node["depth"]) + " ".join(parts))
        if truncated:
            lines.append("… tree truncated by max_nodes or max_depth")
        return "\n".join(lines)

    def get_app_state(
        self,
        app: str | int,
        *,
        screenshot: bool = False,
        max_depth: int = 25,
        max_nodes: int = 5000,
        window_index: int = 0,
        include_menu_bar: bool = False,
        extra_attributes: Iterable[str] = (),
        include_actions: bool = True,
        include_settable: bool = True,
        include_values: bool = True,
        enhance: bool = True,
    ) -> dict[str, Any]:
        self._ensure_accessibility()
        _, info = self._resolve_app(app)
        root = self._application_element(info["pid"], enhance=enhance)
        snapshot = self._snapshot_tree(
            root,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_menu_bar=include_menu_bar,
            attributes=_AX_ATTRIBUTES if include_values else _AX_SAFE_ATTRIBUTES,
            extra_attributes=tuple(name for name in extra_attributes
                                   if include_values or name not in _SENSITIVE_ATTRIBUTES),
            include_actions=include_actions,
            include_settable=include_settable,
        )
        nodes = snapshot.nodes
        self._last_app = info
        self._last_windows = self.windows(info["pid"])
        state: dict[str, Any] = {
            "app": info,
            "nodes": nodes,
            "text": self._render_tree(
                nodes, truncated=snapshot.node_cut or snapshot.depth_cut or snapshot.read_cut
            ),
            "windows": self._last_windows,
            "coverage": {"complete": not (snapshot.node_cut or snapshot.depth_cut or snapshot.read_cut),
                         "node_cut": snapshot.node_cut, "depth_cut": snapshot.depth_cut,
                         "read_cut": snapshot.read_cut},
        }
        if screenshot:
            try:
                self._last_screenshot = self.capture_screenshot(
                    app, window_index=window_index
                )
                state["screenshot"] = self._last_screenshot
            except MacOSError as exc:
                state["screenshot"] = None
                state["screenshot_error"] = str(exc)
        else:
            state["screenshot"] = None
        return state

    snapshot = get_app_state

    # --- AX actions ------------------------------------------------------

    def _element(self, element_index: int) -> Any:
        try:
            return self._elements[int(element_index)]
        except (KeyError, ValueError) as exc:
            raise MacOSError(
                f"Unknown element index {element_index!r}; take a fresh snapshot first",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"element_index": element_index},
            ) from exc

    def _remember_element(self, element: Any) -> int:
        index = self._element_seq
        self._element_seq += 1
        self._elements[index] = element
        return index

    def _local_element(self, element_index: int) -> Any:
        """Resolve element_index to a local AXUIElement; never a native handle."""
        element = self._element(element_index)
        if not self._is_ax_element(element):
            raise MacOSError(
                f"Element {element_index} is a native agent handle; this "
                "operation is local-only and unsupported for native handles",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"element_index": element_index},
            )
        return element

    def _resolved_native_client(self) -> NativeClient | None | _Unresolved:
        """Return the already-resolved client or ``auto`` fallback, or
        ``_UNRESOLVED`` when a fresh ``agent.launch()`` attempt is still
        needed.

        Raises directly for the four states that must never fall through
        to another launch attempt: this call crossed a fork boundary (see
        ``_check_native_owner``, checked first so a forked child's copy
        of ``_native_client`` is never silently handed back to it), this
        instance's ``close()`` already ran, ``backend == "native"``
        already knows the agent is unavailable from an earlier call, or
        ``backend == "auto"`` has a cached failure that is not an
        ``AgentUnavailableError`` (a hard handshake/protocol mismatch
        never becomes fallback-eligible just because a later call asks
        again).
        """
        self._check_native_owner()
        if self._native_client is not None:
            return self._native_client
        if self._native_closed:
            raise MacOSError(
                "This MacOS instance's close() already tore down its "
                f"native agent child; construct a new MacOS to use "
                f"backend={self._backend!r} again",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"backend": self._backend},
            )
        if self._native_error is not None:
            from . import agent

            if self._backend == "auto" and isinstance(
                self._native_error, agent.AgentUnavailableError
            ):
                return None
            raise self._native_error
        return _UNRESOLVED

    def _acquire_native(self) -> NativeClient | None:
        """Lazily launch this instance's own private native agent child.

        Returns the connected, handshake-verified client, or ``None``
        when ``backend == "auto"`` and the agent could not be made
        available at all — an ``AgentUnavailableError`` ``agent.launch()``
        raised before a real handshake response was ever received (no
        override, bundled, or buildable executable; a spawn failure; or
        the child crashing, closing its socket, or never answering
        before its own timeout). ``backend == "native"`` always raises
        instead of returning ``None``.

        Every other native failure — a protocol-version mismatch or an
        ``expected_pid`` identity mismatch discovered by the very
        handshake the freshly spawned child just answered — means a real
        response already came back from *some* process, so it hard-fails
        even under ``auto`` rather than silently falling back to what
        could be a wrong or unexpected agent. Every ``agent.launch()``
        failure is cached here, so repeated calls never re-attempt a
        launch already known to fail; only ``AgentUnavailableError`` is
        ever fallback-eligible under ``auto`` -- a cached hard mismatch
        keeps raising on every later call instead of silently spawning
        another child.

        Concurrent first use from multiple threads on the same instance
        launches exactly one child: the actual launch only ever happens
        while holding ``self._native_lock``, and every caller re-checks
        under that lock in case another thread already resolved (or
        failed) it while this one was waiting.

        Each ``MacOS`` instance launches — and, via ``close()``, tears
        down — its own private child, never a client shared with any
        other instance or process.

        Ownership transfer from a successful ``agent.launch()`` into
        this instance's own storage (``_native_session_box``,
        ``_native_client``) is itself ``BaseException``-safe: a
        ``KeyboardInterrupt`` landing after ``launch()`` has already
        returned a live session but before that storage step finishes
        would otherwise orphan it -- a real child process this instance
        never records anywhere, so nothing (not even ``close()``) would
        ever reap it. Any such interruption instead clears whatever
        partial state was written, closes that exact session, and
        re-raises unchanged.
        """
        resolved = self._resolved_native_client()
        if resolved is not _UNRESOLVED:
            return resolved
        from . import agent

        with self._native_lock:
            resolved = self._resolved_native_client()
            if resolved is not _UNRESOLVED:
                return resolved
            try:
                session = agent.launch()
            except Exception as exc:
                self._native_error = exc
                if self._backend == "auto" and isinstance(
                    exc, agent.AgentUnavailableError
                ):
                    return None
                raise
            try:
                self._native_session_box[0] = session
                self._native_client = session.client
            except BaseException:
                self._native_session_box[0] = None
                self._native_client = None
                session.close()
                raise
            return self._native_client

    def _intern_native_match(self, raw: dict[str, Any], client: Any) -> dict[str, Any]:
        """Turn one wire match descriptor into a client-side element_index."""
        from .native import _NativeHandle

        match = dict(raw)
        handle = match.pop("handle")
        sentinel = _NativeHandle(client, handle, client.generation)
        match["element_index"] = self._remember_element(sentinel)
        return match

    def _native_query(
        self,
        client: Any,
        *,
        pid: int,
        search_key: str,
        text: str | None,
        exact: _ExactSelector,
        visible_only: bool,
        limit: int,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        include_actions: bool,
        max_nodes: int,
        reset_elements: bool,
        messaging_timeout: float | None,
        enhance: bool,
    ) -> SearchMatches:
        if reset_elements:
            self._elements = {}
        params = {
            "app_pid": pid,
            "search_key": search_key,
            "text": text,
            "title": exact.title,
            "identifier": exact.identifier,
            "description": exact.description,
            "visible_only": bool(visible_only),
            "limit": int(limit),
            "direction": direction,
            "immediate_descendants_only": bool(immediate_descendants_only),
            "attributes": [str(item) for item in attributes],
            "include_actions": bool(include_actions),
            "max_nodes": int(max_nodes),
            "reset_elements": bool(reset_elements),
            "messaging_timeout": messaging_timeout,
            "enhance": bool(enhance),
        }
        result = client.query(params)
        return SearchMatches(
            (self._intern_native_match(raw, client) for raw in result.matches),
            complete=result.complete,
            visited=result.visited,
        )

    def _native_press(
        self,
        client: Any,
        *,
        pid: int,
        search_key: str,
        text: str | None,
        exact: _ExactSelector,
        visible_only: bool,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        max_nodes: int,
        timeout: float,
        deadline: _Deadline,
        single_attempt: bool,
        interval: float,
    ) -> dict[str, Any]:
        """Press via the agent, retrying only a not-yet-unique match.

        Mirrors ``ax_wait``'s own deadline/retry shape: ``element.unknown``
        is the one outcome ``PressCoordinator`` (agent-side) guarantees
        happens before any ``AXPress`` is ever dispatched -- no match, or
        one match from a search an exact selector needs complete -- so it
        is the only failure retried here, from one monotonic deadline.
        Every other code -- ``focus.changed`` (the press already
        happened), ``permission.accessibility``, or any
        transport/protocol failure whose relation to dispatch is unknown
        -- propagates immediately: retrying those could fire ``AXPress``
        a second time. A timeout carries the agent's last reason
        (``complete``, ``visited``) beside ``max_nodes``.
        """
        self._elements = {}
        params = {
            "app_pid": pid,
            "search_key": search_key,
            "text": text,
            "title": exact.title,
            "identifier": exact.identifier,
            "description": exact.description,
            "visible_only": bool(visible_only),
            "limit": 2,
            "direction": direction,
            "immediate_descendants_only": bool(immediate_descendants_only),
            "attributes": [str(item) for item in attributes],
            "include_actions": True,
            "max_nodes": int(max_nodes),
            "reset_elements": True,
            "messaging_timeout": None,
            "enhance": True,
            "action_deadline": None if single_attempt else deadline.expires_at,
        }
        last_details: dict[str, JSONValue] | None = None
        while True:
            if not single_attempt:
                deadline.check_dispatch(last_details)
            try:
                match = client.press(params)
            except MacOSError as exc:
                if exc.code != ErrorCode.ELEMENT_UNKNOWN.value:
                    raise
                last_details = {
                    "timeout": timeout,
                    "pid": pid,
                    "max_nodes": max_nodes,
                    **exc.details,
                }
                deadline.check_dispatch(last_details)
                time.sleep(min(interval, deadline.remaining()))
                continue
            return self._intern_native_match(match, client)

    def _describe_element(
        self,
        element: Any,
        element_index: int,
        *,
        attributes: Iterable[str],
        include_actions: bool,
    ) -> dict[str, Any]:
        requested = tuple(
            dict.fromkeys(("AXRole", *(str(item) for item in attributes)))
        )
        raw = self._copy_attributes(element, requested)
        node: dict[str, Any] = {
            "element_index": element_index,
            "role": self._jsonable(raw.get("AXRole")),
        }
        for source, target in _AX_NODE_MAPPING.items():
            if source not in raw:
                continue
            value = self._jsonable(raw[source])
            if value not in (None, "", [], {}):
                node[target] = value
        extra = {
            name: self._jsonable(value)
            for name, value in raw.items()
            if name not in _AX_NODE_MAPPING
            and name != "AXRole"
            and self._jsonable(value) not in (None, "", [], {})
        }
        if extra:
            node["attributes"] = extra
        if include_actions:
            actions = self._actions(element)
            if actions:
                node["actions"] = actions
        return node

    def get(
        self, element_index: int, attribute: str = "AXValue", *, missing_ok: bool = False
    ) -> Any:
        """Read one attribute; missing_ok permits absence, never a failed read."""
        element = self._element(element_index)
        if not self._is_ax_element(element):
            try:
                return element.client.get(element, attribute)
            except MacOSError as exc:
                error = exc.details.get("ax_error")
                if missing_ok and isinstance(error, int) and _ax_absent(error):
                    return None
                raise
        error, value = AS.AXUIElementCopyAttributeValue(element, attribute, None)
        if missing_ok and _ax_absent(error):
            return None
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Read {attribute} from element {element_index}",
                error,
                element_index=element_index,
                attribute=attribute,
            )
        return self._jsonable(value)

    def get_attributes(
        self, element_index: int, attributes: Iterable[str]
    ) -> dict[str, Any | None]:
        """Read multiple raw AX attributes with Apple's batch API."""
        element = self._element(element_index)
        if not self._is_ax_element(element):
            return element.client.get_attributes(element, tuple(attributes))
        return {
            name: self._jsonable(value)
            for name, value in self._copy_attributes(element, attributes).items()
        }

    def ax_search(
        self,
        *,
        element_index: int | None = None,
        app: str | int | None = None,
        app_pid: int | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = False,
        limit: int = -1,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_ATTRIBUTES,
        include_actions: bool = True,
        max_nodes: int = 500,
        reset_elements: bool = True,
        messaging_timeout: float | None = None,
        enhance: bool = True,
    ) -> SearchMatches:
        """Search a Chromium/WebKit AX subtree, including virtualized nodes.

        ``text`` is the app's own substring search. ``title``,
        ``identifier`` and ``description`` are exact: a match must carry
        that attribute with exactly that value. With any of them set the
        app's search is asked for up to ``max_nodes`` candidates, which
        are then judged here, so ``max_nodes`` bounds that path too. The
        result's ``complete`` says whether it holds every match in scope.
        """
        exact = _ExactSelector.parse(
            title=title, identifier=identifier, description=description
        )
        attributes = tuple(str(item) for item in attributes)
        if exact.active and max_nodes <= 0:
            raise MacOSError(
                "AX search max_nodes must be positive with an exact selector",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "max_nodes", "value": max_nodes},
            )
        if element_index is not None and (app is not None or app_pid is not None):
            raise MacOSError(
                "AX search element_index cannot be combined with app",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "element_index"},
            )
        if app is not None and app_pid is not None:
            raise MacOSError(
                "AX search accepts app or app_pid, not both",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app_pid"},
            )

        if element_index is None and self._backend != "python":
            native_pid = app_pid if app_pid is not None else self._pid(app)
            if native_pid is None:
                raise MacOSError(
                    "AX search requires an app or a prior app snapshot",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            client = self._acquire_native()
            if client is not None:
                if direction.casefold() not in {"next", "previous"}:
                    raise MacOSError(
                        "AX search direction must be 'next' or 'previous'",
                        code=ErrorCode.BAD_REQUEST,
                        details={"parameter": "direction", "value": direction},
                    )
                return self._native_query(
                    client,
                    pid=native_pid,
                    search_key=search_key,
                    text=text,
                    exact=exact,
                    visible_only=visible_only,
                    limit=limit,
                    direction=direction.casefold(),
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                    reset_elements=reset_elements,
                    messaging_timeout=messaging_timeout,
                    enhance=enhance,
                )

        self._ensure_accessibility()
        if element_index is None:
            pid = app_pid if app_pid is not None else self._pid(app)
            if pid is None:
                raise MacOSError(
                    "AX search requires an app or a prior app snapshot",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            root = self._application_element(
                pid,
                messaging_timeout=messaging_timeout,
                enhance=enhance,
            )
        else:
            root = self._local_element(element_index)
        if reset_elements:
            self._elements = {}

        directions = {
            "next": "AXDirectionNext",
            "previous": "AXDirectionPrevious",
        }
        try:
            ax_direction = directions[direction.casefold()]
        except KeyError as exc:
            raise MacOSError(
                "AX search direction must be 'next' or 'previous'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "direction", "value": direction},
            ) from exc
        # With an exact selector the app's search only narrows; the
        # judgement happens here, so ask for every candidate the walk
        # bound allows and apply ``limit`` after judging.
        requested_limit = max_nodes if exact.active else int(limit)
        predicate: dict[str, Any] = {
            "AXSearchKey": search_key,
            "AXVisibleOnly": bool(visible_only),
            "AXResultsLimit": requested_limit,
            "AXDirection": ax_direction,
            "AXImmediateDescendantsOnly": bool(immediate_descendants_only),
        }
        if text is not None:
            predicate["AXSearchText"] = str(text)
        error, values = AS.AXUIElementCopyParameterizedAttributeValue(
            root, "AXUIElementsForSearchPredicate", predicate, None
        )
        if error == AS.kAXErrorParameterizedAttributeUnsupported:
            return self._bounded_ax_search(
                root,
                search_key=search_key,
                text=text,
                exact=exact,
                visible_only=visible_only,
                limit=limit,
                direction=direction,
                immediate_descendants_only=immediate_descendants_only,
                attributes=attributes,
                include_actions=include_actions,
                max_nodes=max_nodes,
                reset_elements=False,
            )
        if error != _AX_SUCCESS:
            raise _ax_error("AXUIElementsForSearchPredicate", error, element_index=element_index)

        candidates = [
            element for element in values or [] if self._is_ax_element(element)
        ]
        described = tuple(dict.fromkeys((*attributes, *exact.attributes)))
        matches: list[dict[str, Any]] = []
        read_complete = True
        for element in candidates:
            if exact.active:
                raw = self._copy_attributes(element, exact.attributes)
                read_complete = read_complete and raw.complete
                fields = {
                    _AX_NODE_MAPPING[name]: self._jsonable(value)
                    for name, value in raw.items()
                }
                if not exact.matches(fields):
                    continue
            index = self._remember_element(element)
            matches.append(
                self._describe_element(
                    element,
                    index,
                    attributes=described,
                    include_actions=include_actions,
                )
            )
        cut = 0 <= limit < len(matches)
        if cut:
            del matches[limit:]
        # The app returns at most ``requested_limit`` candidates and says
        # nothing more, so a full page may hide more; a short page is all
        # there was.
        complete = (
            requested_limit < 0 or len(candidates) < requested_limit
        ) and not cut and read_complete
        return SearchMatches(matches, complete=complete, visited=len(candidates))

    def _bounded_ax_search(
        self,
        root: Any,
        *,
        search_key: str,
        text: str | None,
        exact: _ExactSelector,
        visible_only: bool,
        limit: int,
        direction: str,
        immediate_descendants_only: bool,
        attributes: Iterable[str],
        include_actions: bool,
        max_nodes: int,
        reset_elements: bool,
    ) -> SearchMatches:
        """Search a small ordinary AX tree when optimized search is unavailable."""
        if max_nodes <= 0:
            raise MacOSError(
                "AX fallback max_nodes must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "max_nodes", "value": max_nodes},
            )
        role = _AX_SEARCH_ROLES.get(search_key)
        if role is None and search_key != "AXAnyTypeSearchKey":
            raise MacOSError(
                f"AX fallback does not support {search_key!r}",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"search_key": search_key},
            )
        result_limit = max_nodes if limit < 0 else int(limit)
        if result_limit <= 0:
            return SearchMatches(complete=False, visited=0)

        needle = text.casefold() if text is not None else None
        attributes = tuple(str(item) for item in attributes)
        described = tuple(dict.fromkeys((*attributes, *exact.attributes)))
        traversal_attributes = tuple(
            dict.fromkeys(
                (
                    *attributes,
                    *exact.attributes,
                    "AXHidden",
                )
            )
        )
        snapshot = self._snapshot_tree(
            root,
            max_depth=1 if immediate_descendants_only else 25,
            max_nodes=max_nodes,
            include_menu_bar=True,
            attributes=traversal_attributes,
            include_actions=False,
            include_settable=False,
            reset_elements=reset_elements,
        )
        nodes = snapshot.nodes[1:]
        if direction.casefold() == "previous":
            nodes.reverse()

        matches: list[dict[str, Any]] = []
        cut = False
        for node in nodes:
            values = (
                node.get("title"),
                node.get("description"),
                node.get("value"),
                node.get("help"),
                node.get("identifier"),
                node.get("dom_identifier"),
                node.get("placeholder"),
            )
            text_matches = needle is None or any(
                needle in str(value).casefold()
                for value in values
                if value not in (None, "")
            )
            if (
                (role is None or node.get("role") == role)
                and (not visible_only or not bool(node.get("hidden")))
                and text_matches
                and exact.matches(node)
            ):
                if len(matches) >= result_limit:
                    cut = True
                    break
                index = int(node["element_index"])
                matches.append(
                    self._describe_element(
                        self._element(index),
                        index,
                        attributes=described,
                        include_actions=include_actions,
                    )
                )
        # A depth cut only matters when the walk was meant to go deep: an
        # immediate-descendants search stops at depth 1 by design.
        complete = (
            not cut
            and not snapshot.node_cut
            and not snapshot.read_cut
            and (immediate_descendants_only or not snapshot.depth_cut)
        )
        return SearchMatches(matches, complete=complete, visited=len(snapshot.nodes))

    @staticmethod
    def _normalize_apps(
        apps: str | int | Iterable[str | int] | None,
    ) -> tuple[str, ...] | None:
        if apps is None:
            return None
        values = (apps,) if isinstance(apps, (str, int)) else tuple(apps)
        normalized = tuple(str(value).strip() for value in values)
        if not normalized or any(not value for value in normalized):
            raise MacOSError(
                "apps must contain at least one non-empty selector",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "apps"},
            )
        return normalized

    def _resolve_apps(self, selectors: tuple[str, ...] | None) -> list[dict[str, Any]]:
        if selectors is None:
            return self.list_apps()
        resolved: list[dict[str, Any]] = []
        seen: set[int] = set()
        for selector in selectors:
            _, info = self._resolve_app(selector)
            pid = int(info["pid"])
            if pid in seen:
                continue
            seen.add(pid)
            resolved.append(info)
        return resolved

    @classmethod
    def _ax_scope(
        cls,
        *,
        app: str | int | None,
        all_apps: bool,
        apps: str | int | Iterable[str | int] | None,
        text: str | None,
        exact: _ExactSelector,
    ) -> tuple[tuple[str, ...] | None, bool]:
        selectors = cls._normalize_apps(apps)
        if app is not None and (all_apps or selectors is not None):
            raise MacOSError(
                "Pass exactly one of app, all_apps=True, or apps",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "scope"},
            )
        if all_apps and selectors is not None:
            raise MacOSError(
                "Pass all_apps=True or apps, not both",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "scope"},
            )
        cross_process = all_apps or selectors is not None
        if cross_process and not text and not exact.active:
            raise MacOSError(
                "Cross-app AX search requires non-empty text or an exact selector",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "text"},
            )
        return selectors, cross_process

    def ax_search_all(
        self,
        *,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
    ) -> SearchMatches:
        """Search selected running AX trees without activation.

        The result is ``complete`` only when every app was searched to
        completion and ``limit`` did not stop the sweep early. A skipped
        app makes the result incomplete. Explicit ``apps`` failures raise;
        a broad sweep raises its first error only when no app was searched.
        """
        exact = _ExactSelector.parse(
            title=title, identifier=identifier, description=description
        )
        if self._backend == "python":
            self._ensure_accessibility()
        if not text and not exact.active:
            raise MacOSError(
                "Cross-app AX search requires non-empty text or an exact selector",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "text"},
            )
        if direction.casefold() not in {"next", "previous"}:
            raise MacOSError(
                "AX search direction must be 'next' or 'previous'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "direction", "value": direction},
            )
        if max_nodes <= 0:
            raise MacOSError(
                "AX fallback max_nodes must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "max_nodes", "value": max_nodes},
            )
        if limit <= 0:
            raise MacOSError(
                "Cross-app AX search limit must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "limit", "value": limit},
            )

        selectors = self._normalize_apps(apps)
        infos = self._resolve_apps(selectors)
        strict = selectors is not None
        self._elements = {}
        matches = SearchMatches(complete=True, visited=0)
        first_error: MacOSError | None = None
        searched = False
        for info in infos:
            remaining = limit - len(matches)
            if remaining == 0:
                # Apps after this one were never searched.
                matches.complete = False
                break
            try:
                app_matches = self.ax_search(
                    app_pid=int(info["pid"]),
                    search_key=search_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    limit=remaining,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                    reset_elements=False,
                    messaging_timeout=_AX_CROSS_APP_MESSAGING_TIMEOUT,
                    enhance=False,
                )
            except AccessibilityPermissionError:
                raise
            except MacOSError as exc:
                if strict:
                    raise MacOSError(
                        f"AX search failed for {info['name']} ({info['pid']}): {exc}",
                        code=exc.code,
                        details={**exc.details, "app": info},
                    ) from exc
                matches.complete = False
                if first_error is None:
                    first_error = exc
                continue
            searched = True
            matches.complete = matches.complete and app_matches.complete
            matches.visited += app_matches.visited
            for match in app_matches:
                match["app"] = dict(info)
                matches.append(match)
        if not searched and first_error is not None:
            raise first_error
        return matches

    @staticmethod
    def _match_summary(match: dict[str, Any]) -> str:
        owner = match.get("app")
        app_name = (
            owner.get("name", "current app")
            if isinstance(owner, dict)
            else "current app"
        )
        role = str(match.get("role") or "AXUnknown")
        label = str(
            match.get("title")
            or match.get("description")
            or match.get("identifier")
            or "untitled"
        )
        return f"{app_name}: {role} {label!r}"

    def ax_wait(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 500,
        enhance: bool = True,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        """Wait for exactly one AX match and fail closed on ambiguity.

        With an exact selector (``title``/``identifier``/``description``),
        a single match requires a complete search to rule out a twin.
        Substring-only waits retain best-effort uniqueness within the
        returned results.
        """
        exact = _ExactSelector.parse(
            title=title, identifier=identifier, description=description
        )
        selectors, cross_process = self._ax_scope(
            app=app,
            all_apps=all_apps,
            apps=apps,
            text=text,
            exact=exact,
        )
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX wait timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX wait interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )

        deadline = time.monotonic() + timeout
        while True:
            if cross_process:
                matches = self.ax_search_all(
                    apps=selectors,
                    search_key=search_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    limit=2,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                )
            else:
                matches = self.ax_search(
                    app=app,
                    search_key=search_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    limit=2,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    include_actions=include_actions,
                    max_nodes=max_nodes,
                    enhance=enhance,
                )
            if len(matches) == 1 and (matches.complete or not exact.active):
                return matches[0]
            if len(matches) > 1:
                summary = "; ".join(self._match_summary(match) for match in matches[:4])
                # More than one match is the caller's search criteria being too loose,
                # not an unknown element -- and not worth retrying, since a second
                # identical search will not resolve the ambiguity either. Mirrors the
                # native agent's `PressCoordinator`, which reports the same condition as
                # `bad_request` rather than `element.unknown` for exactly this reason.
                raise MacOSError(
                    f"AX wait found {len(matches)} matches: {summary}",
                    code=ErrorCode.BAD_REQUEST,
                    details={
                        "count": len(matches),
                        "matches": [
                            {
                                "element_index": match.get("element_index"),
                                "role": match.get("role"),
                                "app": match.get("app"),
                            }
                            for match in matches[:4]
                        ],
                    },
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if matches.complete:
                    raise MacOSError(
                        "AX wait timed out without a match",
                        code=ErrorCode.TIMEOUT,
                        details={"timeout": timeout},
                    )
                message = (
                    "AX wait timed out before a complete search confirmed "
                    "a unique match"
                    if matches
                    else "AX wait timed out without a match; the last search "
                    "was incomplete"
                )
                raise MacOSError(
                    message,
                    code=ErrorCode.TIMEOUT,
                    details={
                        "timeout": timeout,
                        "complete": False,
                        "visited": matches.visited,
                        "max_nodes": max_nodes,
                    },
                )
            time.sleep(min(interval, remaining))

    def ax_wait_gone(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        enhance: bool = True,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None:
        """Wait for an AX match to be absent in two consecutive polls.

        Only a complete empty search counts as an empty poll: a walk that
        ``max_nodes`` cut short before finding anything says nothing about
        the part it did not reach, so it resets the count.
        """
        exact = _ExactSelector.parse(
            title=title, identifier=identifier, description=description
        )
        selectors, cross_process = self._ax_scope(
            app=app,
            all_apps=all_apps,
            apps=apps,
            text=text,
            exact=exact,
        )
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX wait timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX wait interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )

        deadline = time.monotonic() + timeout
        empty_polls = 0
        while True:
            try:
                if cross_process:
                    matches = self.ax_search_all(
                        apps=selectors,
                        search_key=search_key,
                        text=text,
                        title=title,
                        identifier=identifier,
                        description=description,
                        visible_only=visible_only,
                        limit=1,
                        direction=direction,
                        immediate_descendants_only=immediate_descendants_only,
                        attributes=attributes,
                        max_nodes=max_nodes,
                    )
                else:
                    matches = self.ax_search(
                        app=app,
                        search_key=search_key,
                        text=text,
                        title=title,
                        identifier=identifier,
                        description=description,
                        visible_only=visible_only,
                        limit=1,
                        direction=direction,
                        immediate_descendants_only=immediate_descendants_only,
                        attributes=attributes,
                        max_nodes=max_nodes,
                        enhance=enhance,
                    )
            except ApplicationNotFoundError:
                if app is not None or (selectors is not None and len(selectors) == 1):
                    return
                raise

            empty_polls = empty_polls + 1 if not matches and matches.complete else 0
            if empty_polls >= 2:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                details: dict[str, JSONValue] = {
                    "timeout": timeout,
                    "consecutive_empty_polls": empty_polls,
                }
                if not matches and not matches.complete:
                    details.update(
                        complete=False, visited=matches.visited, max_nodes=max_nodes
                    )
                raise MacOSError(
                    "AX wait timed out before two consecutive empty polls "
                    "confirmed the match was gone",
                    code=ErrorCode.TIMEOUT,
                    details=details,
                )
            time.sleep(min(interval, remaining))

    def ax_press(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        search_key: str = "AXAnyTypeSearchKey",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _AX_SAFE_ATTRIBUTES,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
        _deadline: _Deadline | None = None,
    ) -> dict[str, Any]:
        """Press one unique AX target and detect foreground activation.

        Uniqueness follows `ax_wait`: an exact selector is pressed only
        once a complete search has shown its single match. The deadline
        includes app resolution, agent setup, search, and the focus reading.
        A raw zero timeout allows one attempt without retry; a deadline
        inherited from `mac.do` must still have time before dispatch.
        """
        exact = _ExactSelector.parse(
            title=title, identifier=identifier, description=description
        )
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                "AX press timeout must be finite and non-negative",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        if not math.isfinite(interval) or interval <= 0:
            raise MacOSError(
                "AX press interval must be finite and positive",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "interval", "value": interval},
            )
        single_attempt = _deadline is None and timeout == 0
        deadline = _deadline if _deadline is not None else _Deadline(timeout, time.monotonic)
        targeted = not all_apps and apps is None
        if targeted and self._backend != "python":
            if direction.casefold() not in {"next", "previous"}:
                raise MacOSError(
                    "AX search direction must be 'next' or 'previous'",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "direction", "value": direction},
                )
            target_pid = self._pid(app)
            if target_pid is None:
                raise MacOSError(
                    "AX press requires app, all_apps=True, or apps",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
            client = self._acquire_native()
            if client is not None:
                # A single agent-side search-then-press request settles
                # this directly; a client-side ax_wait traversal first
                # would just be a second, redundant round trip for the
                # same uniqueness check the agent already performs.
                return self._native_press(
                    client,
                    pid=target_pid,
                    search_key=search_key,
                    text=text,
                    exact=exact,
                    visible_only=visible_only,
                    direction=direction.casefold(),
                    immediate_descendants_only=immediate_descendants_only,
                    attributes=attributes,
                    max_nodes=max_nodes,
                    timeout=timeout,
                    deadline=deadline,
                    single_attempt=single_attempt,
                    interval=interval,
                )

        if not single_attempt:
            deadline.check_dispatch()
        match = self.ax_wait(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=search_key,
            text=text,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=True,
            max_nodes=max_nodes,
            timeout=deadline.remaining(),
            interval=interval,
        )
        owner = match.get("app")
        target_pid = int(owner["pid"]) if isinstance(owner, dict) else self._pid(app)
        if target_pid is None:
            raise MacOSError(
                "AX press requires app, all_apps=True, or apps",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )

        # Guard only after `AXPress` has returned: an action that raised
        # keeps its own error, so `FocusChangedError` from here always means
        # the press landed (`ops._atomic_press_acted` relies on that).
        before = self._frontmost_app()
        if not single_attempt:
            deadline.check_dispatch()
        self.perform_action(int(match["element_index"]), "AXPress")
        self._guard_focus(before, target_pid, "AX press")
        return match

    def set(self, element_index: int, value: Any, attribute: str = "AXValue") -> None:
        if attribute == "AXSelectedTextRange" and isinstance(value, Mapping):
            location, length = value.get("location"), value.get("length")
            if (
                set(value) != {"location", "length"}
                or not isinstance(location, int) or isinstance(location, bool)
                or not isinstance(length, int) or isinstance(length, bool)
                or location < 0 or length < 0 or location + length > (1 << 63) - 1
            ):
                raise MacOSError(
                    "AXSelectedTextRange requires nonnegative integer location and length",
                    code=ErrorCode.BAD_REQUEST,
                )
        element = self._element(element_index)
        if not self._is_ax_element(element):
            element.client.set(element, attribute, value)
            return
        if attribute == "AXSelectedTextRange" and isinstance(value, Mapping):
            value = AS.AXValueCreate(AS.kAXValueCFRangeType, (location, length))
        error = AS.AXUIElementSetAttributeValue(element, attribute, value)
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Set {attribute} on element {element_index}",
                error,
                element_index=element_index,
                attribute=attribute,
            )

    set_value = set

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        element = self._element(element_index)
        normalized = _ACTION_ALIASES.get(action.casefold(), action)
        if not self._is_ax_element(element):
            element.client.perform(element, normalized)
            return
        available = self._actions(element)
        if normalized not in available:
            raise MacOSError(
                f"Element {element_index} does not expose {normalized!r}; available actions: {available}",
                code=ErrorCode.UNSUPPORTED_OP,
                details={
                    "element_index": element_index,
                    "action": normalized,
                    "available_actions": available,
                },
            )
        error = AS.AXUIElementPerformAction(element, normalized)
        if error != _AX_SUCCESS:
            raise _ax_error(
                f"Perform {normalized} on element {element_index}",
                error,
                element_index=element_index,
                action=normalized,
            )

    # --- windows and screenshots ----------------------------------------

    @staticmethod
    def _is_content_window(pid: int, value: Mapping[str, Any]) -> bool:
        """Whether a `CGWindowListCopyWindowInfo` entry is one of ``pid``'s
        windows in the everyday sense: on the normal layer and at least
        40pt on a side. A menu bar extra, tooltip, or helper surface is
        not, so `see` never captures one and posted input never routes
        to one over the window it was aimed at.
        """
        if int(value.get(AS.kCGWindowOwnerPID, -1)) != pid:
            return False
        if int(value.get(AS.kCGWindowLayer, -1)) != 0:
            return False
        bounds = value.get(AS.kCGWindowBounds) or {}
        return float(bounds.get("Width", 0)) >= 40 and float(bounds.get("Height", 0)) >= 40

    def windows(self, app: str | int | None = None) -> list[dict[str, Any]]:
        _, info = self._resolve_app(app)
        values = AS.CGWindowListCopyWindowInfo(
            AS.kCGWindowListOptionAll, AS.kCGNullWindowID
        )
        windows: list[dict[str, Any]] = []
        for value in values or []:
            if not self._is_content_window(int(info["pid"]), value):
                continue
            bounds = value.get(AS.kCGWindowBounds) or {}
            width = float(bounds.get("Width", 0))
            height = float(bounds.get("Height", 0))
            windows.append(
                {
                    "window_id": int(value[AS.kCGWindowNumber]),
                    "title": str(value.get(AS.kCGWindowName) or ""),
                    "bounds": {
                        "x": float(bounds.get("X", 0)),
                        "y": float(bounds.get("Y", 0)),
                        "width": width,
                        "height": height,
                    },
                    "on_screen": bool(value.get(AS.kCGWindowIsOnscreen, False)),
                    "alpha": float(value.get(AS.kCGWindowAlpha, 1.0)),
                }
            )
        return sorted(
            windows,
            key=lambda window: (
                not window["on_screen"],
                -(window["bounds"]["width"] * window["bounds"]["height"]),
                not bool(window["title"]),
            ),
        )

    def wait_for_window(
        self, app: str | int | None = None, *, timeout: float = 2.0
    ) -> list[dict[str, Any]]:
        """Return ``windows(app)`` once it is non-empty, polling until ``timeout``.

        ``open -a`` returns before the app draws anything: TextEdit's first
        window appeared 335ms after launch and Notes' 792ms, so a capture
        issued straight after a launch finds nothing.
        """
        _, info = self._resolve_app(app)
        return self._wait_for_windows(info, timeout=timeout)

    def _wait_for_windows(
        self, info: dict[str, Any], *, timeout: float
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            windows = self.windows(int(info["pid"]))
            if windows:
                return windows
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MacOSError(
                    f"{info['name']} showed no window within {timeout:g}s",
                    code=ErrorCode.TIMEOUT,
                    details={"app": info, "timeout": timeout},
                )
            time.sleep(min(0.05, remaining))

    def capture_screenshot(
        self,
        app: str | None = None,
        *,
        window_index: int = 0,
        path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Capture one window at its native pixel size, without the pointer."""
        return self._capture(
            app,
            window_index=window_index,
            path=path,
            max_width=None,
            max_height=None,
            show_pointer=False,
        )

    def see(
        self,
        app: str | None = None,
        *,
        window_index: int = 0,
        path: str | Path | None = None,
        max_width: int = 1280,
        max_height: int = 1280,
        show_pointer: bool = False,
    ) -> dict[str, Any]:
        """Capture a bounded window image; ``show_pointer`` draws the pointer onto it.

        The result's ``on_screen`` is the only freshness signal: an
        off-screen window (minimized, hidden, or on another Space) still
        renders, but from whatever the app last drew.
        """
        if max_width <= 0 or max_height <= 0:
            raise MacOSError(
                "max_width and max_height must be positive",
                code=ErrorCode.BAD_REQUEST,
                details={"max_width": max_width, "max_height": max_height},
            )
        return self._capture(
            app,
            window_index=window_index,
            path=path,
            max_width=max_width,
            max_height=max_height,
            show_pointer=show_pointer,
        )

    def _capture(
        self,
        app: str | None,
        *,
        window_index: int,
        path: str | Path | None,
        max_width: int | None,
        max_height: int | None,
        show_pointer: bool,
    ) -> dict[str, Any]:
        self._ensure_screen_recording()
        running, info = self._resolve_app(app)
        windows = self.windows(int(info["pid"]))
        if not windows:
            # An app this young is most likely still drawing its first
            # window; wait for it rather than report an empty app.
            age = self._launched_seconds_ago(running)
            if age is not None and age < _LAUNCH_GRACE_SECONDS:
                windows = self._wait_for_windows(info, timeout=_LAUNCH_WINDOW_TIMEOUT)
            else:
                raise MacOSError(
                    f"No capturable windows found for {app or self._last_app}",
                    code=ErrorCode.ELEMENT_UNKNOWN,
                    details={"app": info},
                )
        try:
            window = windows[window_index]
        except IndexError as exc:
            raise MacOSError(
                f"Window index {window_index} is out of range; found {len(windows)} windows",
                code=ErrorCode.BAD_REQUEST,
                details={
                    "parameter": "window_index",
                    "value": window_index,
                    "count": len(windows),
                },
            ) from exc

        if path is None:
            with tempfile.NamedTemporaryFile(
                prefix="macos-harness-", suffix=".png", delete=False
            ) as handle:
                output = Path(handle.name)
        else:
            output = Path(path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)

        captured = capture_window(
            window["window_id"], max_width=max_width, max_height=max_height
        )
        bounds = captured.bounds
        frontmost = self._frontmost_app()
        screenshot: dict[str, Any] = {
            "path": str(output),
            "app": info,
            "pid": info["pid"],
            "window_id": window["window_id"],
            "title": window["title"],
            "width": captured.width,
            "height": captured.height,
            "bounds": bounds,
            "scale_x": captured.width / bounds["width"],
            "scale_y": captured.height / bounds["height"],
            "on_screen": captured.on_screen,
            "captured_at": captured.captured_at,
            "virtual_pointer": None,
            "focus": {
                "frontmost": frontmost,
                "target_is_frontmost": (
                    frontmost is not None and int(frontmost["pid"]) == int(info["pid"])
                ),
            },
        }

        image = captured.image
        if self._pointer_position is not None:
            pointer_x, pointer_y = self._pointer_position
            image_x, image_y, inside = self._image_point(screenshot, pointer_x, pointer_y)
            visible = self._overlay.visible
            screenshot["virtual_pointer"] = {
                "screen": {"x": pointer_x, "y": pointer_y},
                "image": {"x": image_x, "y": image_y},
                "inside": inside,
                "visible": visible,
            }
            if show_pointer and visible and inside:
                scale = (screenshot["scale_x"] + screenshot["scale_y"]) / 2
                image = draw_pointer(image, image_x, image_y, scale)
        write_png(image, output)

        self._last_app = info
        self._last_windows = windows
        self._last_screenshot = screenshot
        return screenshot

    # --- direct visual and keyboard input -------------------------------

    def _pid(self, app: str | int | None) -> int | None:
        if app is None and self._last_app is None:
            return None
        _, info = self._resolve_app(app)
        self._last_app = info
        return int(info["pid"])

    @staticmethod
    def _post(event: Any, pid: int | None) -> None:
        if pid is None:
            raise MacOSError(
                "Input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        AS.CGEventPostToPid(pid, event)

    @classmethod
    def _target_window(cls, pid: int, point: tuple[float, float]) -> _TargetWindow:
        """Find the frontmost on-screen window of ``pid`` under ``point``.

        Only a window `windows` would list counts, so input routes to a
        window `see` can capture and never to a same-app tooltip or
        helper surface over it. ``origin`` is the window's top-left
        screen point, which ``_route_to_window`` needs to express each
        event in window coordinates. Other apps' windows over the point
        do not matter: input posted to a pid only ever reaches that pid,
        so the covered window is still the one the event lands in.
        """
        if _set_window_location is None:
            raise MacOSError(
                "This macOS build lacks CGEventSetWindowLocation, so posted "
                "mouse input cannot be routed to a window",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"symbol": "CGEventSetWindowLocation"},
            )
        x, y = point
        values = AS.CGWindowListCopyWindowInfo(
            AS.kCGWindowListOptionOnScreenOnly, AS.kCGNullWindowID
        )
        for value in values or ():
            if not cls._is_content_window(pid, value):
                continue
            bounds = value.get(AS.kCGWindowBounds) or {}
            left = float(bounds.get("X", 0))
            top = float(bounds.get("Y", 0))
            if (
                left <= x < left + float(bounds.get("Width", 0))
                and top <= y < top + float(bounds.get("Height", 0))
            ):
                return _TargetWindow(int(value[AS.kCGWindowNumber]), (left, top))
        raise MacOSError(
            f"No on-screen window of pid {pid} contains screen point "
            f"({x:.0f}, {y:.0f}); pointer input there cannot land",
            code=ErrorCode.BAD_REQUEST,
            details={"pid": pid, "point": {"x": x, "y": y}},
        )

    @staticmethod
    def _route_to_window(
        event: Any, window: _TargetWindow, point: tuple[float, float]
    ) -> None:
        """Bind a mouse or scroll ``event`` at screen ``point`` to ``window``."""
        AS.CGEventSetIntegerValueField(event, _CG_EVENT_WINDOW_NUMBER, window.window_id)
        left, top = window.origin
        _set_window_location(
            objc.pyobjc_id(event), _CGPoint(point[0] - left, point[1] - top)
        )

    def _screen_point(
        self, x: float, y: float, coordinate_space: str, *, pid: int | None = None
    ) -> tuple[float, float]:
        """Resolve a coordinate to a screen point, bound to ``pid`` when given.

        ``pid`` -- the already-resolved target of the action this point is
        for -- is optional: callers with no dispatch target at all (``move``,
        an AX hit test) simply skip the binding check below. Callers that
        *do* dispatch to a pid (``click``, ``drag``, ``scroll``) must pass
        it, so a window/screenshot-relative coordinate computed from a
        screenshot of one app can never be silently posted to another.
        """
        if coordinate_space == "screen":
            return float(x), float(y)
        if self._last_screenshot is None:
            raise MacOSError(
                "Take a screenshot before using window or screenshot coordinates, "
                "or pass coordinate_space='screen'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "coordinate_space", "value": coordinate_space},
            )
        shot = self._last_screenshot
        if pid is not None and int(shot["pid"]) != int(pid):
            raise MacOSError(
                f"Last screenshot targets pid {shot['pid']}, not the pid {pid} "
                "this action targets; take a fresh screenshot for this app or "
                "pass coordinate_space='screen'",
                code=ErrorCode.BAD_REQUEST,
                details={
                    "parameter": "coordinate_space",
                    "screenshot_pid": int(shot["pid"]),
                    "target_pid": int(pid),
                },
            )
        if pid is not None:
            self._require_window_unchanged(shot)
        bounds = shot["bounds"]
        if coordinate_space == "screenshot":
            x = float(x) / float(shot["scale_x"])
            y = float(y) / float(shot["scale_y"])
        elif coordinate_space != "window":
            raise MacOSError(
                "coordinate_space must be 'screenshot', 'window', or 'screen'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "coordinate_space", "value": coordinate_space},
            )
        return bounds["x"] + float(x), bounds["y"] + float(y)

    @staticmethod
    def _require_window_unchanged(shot: dict[str, Any]) -> None:
        """Refuse screenshot coordinates once their window moved or left the screen.

        A coordinate taken from a screenshot is only meaningful while the
        window still sits where the screenshot saw it. A closed, moved,
        resized, minimized, or other-Space window gets a fresh ``see()``
        instead of a click that lands somewhere else or nowhere.
        """
        window_id = int(shot["window_id"])
        values = AS.CGWindowListCreateDescriptionFromArray([window_id])
        current = next(iter(values or ()), None)
        if current is None:
            raise MacOSError(
                f"Window {window_id} from the last screenshot is gone; "
                "take a fresh screenshot",
                code=ErrorCode.WINDOW_CHANGED,
                details={"window_id": window_id, "reason": "closed"},
            )
        raw = current.get(AS.kCGWindowBounds) or {}
        bounds = {
            "x": float(raw.get("X", 0)),
            "y": float(raw.get("Y", 0)),
            "width": float(raw.get("Width", 0)),
            "height": float(raw.get("Height", 0)),
        }
        if bounds != shot["bounds"]:
            raise MacOSError(
                f"Window {window_id} moved or resized since the last screenshot; "
                "take a fresh screenshot",
                code=ErrorCode.WINDOW_CHANGED,
                details={
                    "window_id": window_id,
                    "reason": "moved",
                    "was": shot["bounds"],
                    "now": bounds,
                },
            )
        if not bool(current.get(AS.kCGWindowIsOnscreen, False)):
            raise MacOSError(
                f"Window {window_id} is not on screen (minimized, hidden, or on "
                "another Space); input at its coordinates cannot land",
                code=ErrorCode.WINDOW_CHANGED,
                details={"window_id": window_id, "reason": "off_screen"},
            )

    @staticmethod
    def _image_point(
        shot: dict[str, Any], screen_x: float, screen_y: float
    ) -> tuple[float, float, bool]:
        """Map a screen point into ``shot``'s image pixels; the flag says it lands inside."""
        bounds = shot["bounds"]
        image_x = (screen_x - float(bounds["x"])) * float(shot["scale_x"])
        image_y = (screen_y - float(bounds["y"])) * float(shot["scale_y"])
        inside = 0 <= image_x < float(shot["width"]) and 0 <= image_y < float(
            shot["height"]
        )
        return image_x, image_y, inside

    def _pointer_info(self, *, pid: int | None = None) -> dict[str, object] | None:
        """Describe the current virtual pointer position.

        ``pid``, when given, is the OS process id the caller's action just
        targeted. ``self._last_screenshot`` only ever describes one specific
        app's window; a caller that already bound its own action to a
        different app (``click``, a non-screen ``move``) must never have
        ``image``/``inside`` silently computed against a screenshot of that
        *other* app, since those pixel coordinates would be meaningless (or
        worse, misleadingly plausible) for the app the caller actually
        targeted. Passing no ``pid`` (``show_pointer``, a screen-space
        ``move``, which target no particular app at all) keeps reporting
        against whatever screenshot happens to be retained, if any -- there
        is no other app identity to compare it against.
        """
        if self._pointer_position is None:
            return None
        screen_x, screen_y = self._pointer_position
        result: dict[str, object] = {"screen": {"x": screen_x, "y": screen_y}}
        shot = self._last_screenshot
        if shot is not None and (pid is None or int(shot["pid"]) == int(pid)):
            image_x, image_y, inside = self._image_point(shot, screen_x, screen_y)
            result["image"] = {"x": image_x, "y": image_y}
            result["inside"] = inside
        return result

    def move(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        coordinate_space: str = "screenshot",
        duration: float = 0.16,
    ) -> dict[str, Any]:
        """Animate the virtual pointer without moving the physical cursor.

        ``coordinate_space="screen"`` stays app-free, exactly like every
        other screen-space call in this file. Any other space converts
        through ``self._last_screenshot``, and that screenshot belongs to
        exactly one app -- so this requires ``app`` (or a prior app
        snapshot, exactly like ``click``/``drag``/``scroll``) to bind the
        conversion to, rather than silently reusing whatever screenshot
        ``self._last_screenshot`` last happened to hold regardless of
        which app it actually came from.
        """
        pid = None
        if coordinate_space != "screen":
            pid = self._pid(app)
            if pid is None:
                raise MacOSError(
                    "Move requires an app or prior app snapshot for "
                    "screenshot/window coordinates, or pass coordinate_space='screen'",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "app"},
                )
        self._pointer_position = self._screen_point(x, y, coordinate_space, pid=pid)
        self._overlay.move(*self._pointer_position, duration=duration)
        pointer = self._pointer_info(pid=pid)
        assert pointer is not None
        return pointer

    def show_pointer(self) -> dict[str, Any]:
        if self._pointer_position is None:
            raise MacOSError(
                "Move the virtual pointer before showing it",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "pointer_position"},
            )
        self._overlay.show(*self._pointer_position)
        pointer = self._pointer_info()
        assert pointer is not None
        return pointer

    def hide_pointer(self) -> None:
        self._overlay.hide()

    def activate(
        self, app: str | int | None = None, *, timeout: float = 0.5
    ) -> dict[str, Any]:
        """Ask macOS to bring ``app`` frontmost and report what actually happened.

        Since macOS 14 activation is a request the system may decline,
        typically while the user is busy in another app. One request is
        made and the frontmost app is observed for up to ``timeout``
        seconds; ``activated`` says whether it took. Nothing here retries
        or loops -- a declined request means the person at the keyboard
        has priority, so hand off instead of asking again.
        """
        running, info = self._resolve_app(app)
        previous = self._frontmost_app()
        started = time.monotonic()
        running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        deadline = started + max(0.0, timeout)
        while True:
            frontmost = self._frontmost_app()
            activated = frontmost is not None and int(frontmost["pid"]) == int(
                info["pid"]
            )
            if activated or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        self._last_app = info
        return {
            "app": info,
            "activated": activated,
            "previous": previous,
            "frontmost": frontmost,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        }

    @staticmethod
    def _validate_button(button: str) -> str:
        button = button.casefold()
        if button not in _BUTTONS:
            raise MacOSError(
                f"Unknown mouse button {button!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "button", "value": button},
            )
        return button

    @staticmethod
    def _validate_clicks(clicks: int) -> int:
        """``clicks`` as a count from 1 to `_MAX_CLICKS`: macOS has no
        gesture past a triple click, and every extra one is two more
        posted events and a 60ms wait."""
        if isinstance(clicks, bool) or not isinstance(clicks, int) or not 1 <= clicks <= _MAX_CLICKS:
            raise MacOSError(
                f"clicks must be a count from 1 to {_MAX_CLICKS}, not {clicks!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "clicks", "value": clicks, "limit": _MAX_CLICKS},
            )
        return clicks

    def click(
        self,
        x: float,
        y: float,
        *,
        app: str | int | None = None,
        button: str = "left",
        clicks: int = 1,
        coordinate_space: str = "screenshot",
    ) -> dict[str, Any]:
        """Send one raw coordinate click to an app PID; never guess an AX action.

        The result carries ``window_id``: the window of ``app`` the click
        was routed to. The click still reaches that window when another
        app is frontmost, but AppKit passes a plain first click on an
        inactive app's view only where that view accepts first mouse;
        elsewhere it is dropped, and ``activate`` first is the cure.
        """
        self._ensure_accessibility()
        self._ensure_post_events()
        button = self._validate_button(button)
        clicks = self._validate_clicks(clicks)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Pointer input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        point = self._screen_point(x, y, coordinate_space, pid=pid)
        window = self._target_window(pid, point)
        self._post_click(pid, point, window, button=button, clicks=clicks)
        pointer = self._pointer_info(pid=pid)
        assert pointer is not None
        pointer["window_id"] = window.window_id
        return pointer

    def _post_click(
        self,
        pid: int,
        point: tuple[float, float],
        window: _TargetWindow,
        *,
        button: str,
        clicks: int,
    ) -> None:
        """Post ``clicks`` presses of ``button`` at screen ``point``, each
        routed to ``window``. ``click`` resolves those three for a raw
        call; ``mac.do.click`` resolves them itself, before it reserves
        anything, and hands them over so the window it reports is the
        one the events carry."""
        focus_before = self._frontmost_app()
        self._pointer_position = point
        self._overlay.move(*point)
        down_type, up_type, _ = _MOUSE_EVENTS[button]
        for click_count in range(1, clicks + 1):
            for event_type in (down_type, up_type):
                self._post_mouse(
                    pid, window, button, event_type, point, click_state=click_count
                )
                time.sleep(0.03)
            self._guard_focus(focus_before, pid, "click")
        self._overlay.click()

    def _post_mouse(
        self,
        pid: int,
        window: _TargetWindow,
        button: str,
        event_type: int,
        point: tuple[float, float],
        *,
        click_state: int | None = None,
    ) -> None:
        """Create one ``button`` mouse event of ``event_type`` at screen
        ``point``, bind it to ``window``, and post it to ``pid``."""
        event = AS.CGEventCreateMouseEvent(
            self._event_source, event_type, point, _BUTTONS[button]
        )
        self._route_to_window(event, window, point)
        if click_state is not None:
            AS.CGEventSetIntegerValueField(event, AS.kCGMouseEventClickState, click_state)
        self._post(event, pid)

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        *,
        app: str | int | None = None,
        button: str = "left",
        coordinate_space: str = "screenshot",
        duration: float = 0.25,
        steps: int = 12,
    ) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        button = self._validate_button(button)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Pointer input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        start = self._screen_point(from_x, from_y, coordinate_space, pid=pid)
        end = self._screen_point(to_x, to_y, coordinate_space, pid=pid)
        # AppKit keeps a drag on the window that took the mouse-down, so
        # every event is routed there even once the point leaves it.
        window = self._target_window(pid, start)
        focus_before = self._frontmost_app()
        self._pointer_position = start
        self._overlay.move(*start, duration=0)
        self._overlay.move(*end, duration=duration)
        down_type, up_type, drag_type = _MOUSE_EVENTS[button]

        def post(event_type: int, point: tuple[float, float]) -> None:
            self._post_mouse(pid, window, button, event_type, point)

        post(down_type, start)
        self._pointer_position = end
        for index in range(1, max(1, steps) + 1):
            ratio = index / max(1, steps)
            post(
                drag_type,
                (
                    start[0] + (end[0] - start[0]) * ratio,
                    start[1] + (end[1] - start[1]) * ratio,
                ),
            )
            time.sleep(max(0.0, duration) / max(1, steps))
        post(up_type, end)
        self._guard_focus(focus_before, pid, "drag")

    def scroll(
        self,
        delta_y: int,
        delta_x: int = 0,
        *,
        app: str | int | None = None,
        unit: str = "pixel",
        x: float | None = None,
        y: float | None = None,
        coordinate_space: str = "screenshot",
    ) -> None:
        """Scroll ``app`` at ``x``/``y``; with no point, at the center of the
        window the last screenshot of ``app`` shows.

        A scroll event only lands in the window it is routed to, so the
        point decides which window scrolls; there is no app-wide scroll.
        """
        self._ensure_accessibility()
        self._ensure_post_events()
        units = {
            "pixel": AS.kCGScrollEventUnitPixel,
            "line": AS.kCGScrollEventUnitLine,
        }
        try:
            scroll_unit = units[unit]
        except KeyError as exc:
            raise MacOSError(
                "Scroll unit must be 'pixel' or 'line'",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "unit", "value": unit},
            ) from exc
        if (x is None) != (y is None):
            raise MacOSError(
                "Provide both x and y when targeting a scroll point",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "x/y"},
            )
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Scroll input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        if x is None or y is None:
            shot = self._last_screenshot
            if shot is None or int(shot["pid"]) != pid:
                raise MacOSError(
                    "Scroll needs x/y, or a prior screenshot of this app to "
                    "scroll at the center of",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "x/y"},
                )
            bounds = shot["bounds"]
            x, y = bounds["width"] / 2, bounds["height"] / 2
            coordinate_space = "window"
        point = self._screen_point(x, y, coordinate_space, pid=pid)
        window = self._target_window(pid, point)
        self._pointer_position = point
        self._overlay.move(*point)

        focus_before = self._frontmost_app()

        maximum = 100 if unit == "pixel" else 10
        y_steps = _split_scroll_delta(delta_y, maximum)
        x_steps = _split_scroll_delta(delta_x, maximum)
        count = max(len(y_steps), len(x_steps))
        y_steps.extend([0] * (count - len(y_steps)))
        x_steps.extend([0] * (count - len(x_steps)))

        for step_y, step_x in zip(y_steps, x_steps, strict=True):
            event = AS.CGEventCreateScrollWheelEvent(
                self._event_source, scroll_unit, 2, int(step_y), int(step_x)
            )
            AS.CGEventSetLocation(event, point)
            self._route_to_window(event, window, point)
            self._post(event, pid)
            time.sleep(0.01)
            self._guard_focus(focus_before, pid, "scroll")

    def type(self, text: str, *, app: str | int | None = None) -> None:
        """Type ``text`` into ``app`` as keyboard events.

        When ``app`` is frontmost the text goes in packed: each key event
        carries a run of up to `_TYPE_CHUNK_UNITS` UTF-16 units, split
        only between code points, with no pause between events. 128
        characters land in about 30ms that way instead of 1.6s, exact in
        TextEdit, Notes, Safari and Chrome (280 of 280 trials). An
        inactive app gets one event pair per character with a 10ms
        pause, because a background Chrome drops every event carrying
        more than one unit. Newline, carriage return and tab always
        travel alone as Return and Tab key events, so the app sees the
        key rather than a pasted character.
        """
        self._ensure_accessibility()
        self._ensure_post_events()
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Keyboard input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        focus_before = self._frontmost_app()
        packed = focus_before is not None and int(focus_before["pid"]) == pid
        for chunk in _typing_chunks(text, packed=packed):
            if packed and chunk not in _KEY_ONLY_CHARACTERS:
                keycode, flags = 0, 0
            else:
                keycode, flags = _character_key(chunk)
            down = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, True)
            up = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, False)
            length = len(chunk.encode("utf-16-le")) // 2
            AS.CGEventKeyboardSetUnicodeString(down, length, chunk)
            AS.CGEventKeyboardSetUnicodeString(up, length, chunk)
            AS.CGEventSetFlags(down, flags)
            AS.CGEventSetFlags(up, flags)
            self._post(down, pid)
            self._post(up, pid)
            if not packed:
                # A frontmost app took focus already, so the guard that
                # stops when the target steals it has nothing to catch.
                time.sleep(0.01)
                self._guard_focus(focus_before, pid, "typing")

    @staticmethod
    def _validate_key(key: str) -> None:
        _parse_key(key)

    def key(self, key: str, *, app: str | int | None = None) -> None:
        self._ensure_accessibility()
        self._ensure_post_events()
        keycode, parsed_modifiers = _parse_key(key)
        pid = self._pid(app)
        if pid is None:
            raise MacOSError(
                "Keyboard input requires an app or prior app snapshot",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "app"},
            )
        focus_before = self._frontmost_app()
        active_flags = 0
        pressed: list[tuple[int, int]] = []
        try:
            for modifier_keycode, modifier_flag in parsed_modifiers:
                active_flags |= modifier_flag
                event = AS.CGEventCreateKeyboardEvent(
                    self._event_source, modifier_keycode, True
                )
                AS.CGEventSetFlags(event, active_flags)
                self._post(event, pid)
                pressed.append((modifier_keycode, modifier_flag))
                time.sleep(0.005)

            down = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, True)
            up = AS.CGEventCreateKeyboardEvent(self._event_source, keycode, False)
            AS.CGEventSetFlags(down, active_flags)
            AS.CGEventSetFlags(up, active_flags)
            self._post(down, pid)
            self._post(up, pid)
        finally:
            for modifier_keycode, modifier_flag in reversed(pressed):
                active_flags &= ~modifier_flag
                event = AS.CGEventCreateKeyboardEvent(
                    self._event_source, modifier_keycode, False
                )
                AS.CGEventSetFlags(event, active_flags)
                self._post(event, pid)
        time.sleep(0.01)
        self._guard_focus(focus_before, pid, f"key {key!r}")

    # --- Apple Events escape hatch --------------------------------------

    def script(self, source: str, *, language: str = "AppleScript") -> str:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", language, "-"],
            input=source,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MacOSError(
                (result.stderr or result.stdout).strip(),
                code=ErrorCode.AX_ERROR,
                details={"returncode": result.returncode, "language": language},
            )
        return result.stdout.rstrip("\n")
