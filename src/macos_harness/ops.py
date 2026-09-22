"""``mac.do``: the recommended, receipted operation surface for macOS Harness.

The raw six primitives (``mac.click``/``mac.key``/``mac.type``/``mac.ax.*``/
``mac.script``/...) stay exactly as they are: thin, direct, and trusting --
they do what they are told and raise on the spot if that fails.
``Operations`` (``mac.do``) is a layer on top of a subset of them for
mutations and explicit observations. Once a call's arguments pass preflight and it
actually begins resolving or dispatching something, it always ends in --
or raises with -- a `Receipt` describing what was asked, what actually
happened, and how thoroughly that was confirmed, instead of a bare
success/failure a caller has to interpret blindly. A call rejected before
that point -- bad argument shape, a fork-boundary violation, an
invalidly-shaped deadline, or a ``once`` token already recorded for a
different request -- raises a plain `MacOSError` instead, with no
receipt: nothing was ever attempted (see `OperationError`).

``mac.do`` is deliberately small: `press`, `set`, `toggle`, `run`, `key`,
`click`, `type`, `fill`, `expect`, `expect_any`, and `recall`. It is not a workflow
engine, a selector language of its own,
or an app-adapter framework -- it reuses `MacOS.ax`'s role/search-key
vocabulary and `MacOS`'s own AX scope rules verbatim (see `_require_scope`
and `Accessibility._search_key`) rather than inventing a second one, and it
never adds another matcher. ``expect`` checks one condition and ``expect_any``
watches several named ones on a single budget; mutation calls resolve
one target, act once, and optionally confirm the effect.

Every mutating verb shares:
  - One monotonic, cooperative budget (`_Deadline`) across resolution,
    dispatch, and postcondition checks. It prevents a new mutation after
    expiry and bounds polling/scripts; a synchronous macOS AX/input call
    already in progress cannot be preempted safely and may return later.
  - `dry_run=True`: validate/resolve/compile, but never dispatch a mutating
    call and never touch the `once` ledger.
  - An optional `Postcondition` (`Present`/`Gone`/`Equals`) confirming the
    mutation's real effect, not just that the underlying call returned
    without raising; unscoped, it inherits the operation's own scope.
  - `press`/`run`/`key`/`click`/`type` additionally accept a nonempty `once` token for
    at-most-once dispatch (`_Ledger`); `set`/`toggle` are convergent
    (read, act only if needed, read back) and so are already safe to call
    more than once without one.
  - Every mutating verb's resolve->precheck->reserve->dispatch->verify
    pipeline is serialized against every other mutating call on the same
    `Operations` instance (`_dispatch_lock`), and fails closed instead of
    running at all if called from a different process than the one that
    constructed it (`_check_owner`) -- guarding against both same-process
    races and a `fork()` crossing a process boundary.
  - Every `Receipt` one call ends up building shares a single
    `_ReceiptBuilder`, so only what varies between outcomes is spelled
    out at the call site.

`press`, for a single explicit ``app`` that is not a dry run, dispatches
through `MacOS.ax_press` in one atomic find-then-press call instead of
resolving and pressing as two separate steps; every other scope, and
every dry run, still resolves first so it can report what it *would*
press without ever dispatching anything.

This module depends on `MacOS` only through `_Host`, a small structural
`Protocol` naming exactly the private/public surface `Operations` actually
calls -- a hermetic test double can satisfy it without ever importing
PyObjC or constructing a real `MacOS`.
"""


from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal, Protocol, TypeVar

from .errors import ApplicationNotFoundError, ErrorCode, FocusChangedError, MacOSError
from .receipts import (
    Acted,
    Equals,
    ErrorPayload,
    Executor,
    Gone,
    JSONValue,
    Observation,
    OperationError,
    Outcome,
    Postcondition,
    Present,
    Receipt,
    canonical_json,
    canonicalize,
    request_fingerprint,
    validate_exact_selectors,
)

if TYPE_CHECKING:
    from .macos import _AppIdentity

__all__ = ["Operations"]

_Reading = TypeVar("_Reading")


# --- small pure helpers ------------------------------------------------

def _source_summary(source: str) -> dict[str, JSONValue]:
    """A ``{"sha256", "length"}`` summary of a `run` request's ``source``
    for a `Receipt` -- never any of the source text itself, head included:
    a script's own body can carry secrets just as easily as a `set`/
    `toggle` value can (see `_value_summary`), so only enough to tell two
    sources apart, or confirm one did not change, is ever stored.
    """
    return {
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "length": len(source),
    }


def _args_summary(argv: Sequence[str]) -> list[JSONValue]:
    """A ``[{"sha256", "length"}, ...]`` summary of a `run` request's
    ``args`` for a `Receipt` -- never any argument's actual text: a
    script's own arguments can carry secrets just as easily as its
    source can (see `_source_summary`, which this reuses per item), so
    only enough to tell two argv lists apart, or confirm one did not
    change, is ever stored. The real ``argv`` this summarizes is still
    exactly what ``osascript`` itself receives -- only this receipt copy
    is redacted.
    """
    return [_source_summary(item) for item in argv]


def _validate_interval(interval: float) -> None:
    """Reject a non-finite or non-positive operation ``interval`` before
    any resolution, dispatch, or ``once``-token reservation.

    Mirrors `_Deadline`'s own finite/non-negative guard on ``timeout``:
    left unchecked, a ``nan``/``inf``/zero/negative interval would
    otherwise reach the host backend as a polling interval instead of
    failing fast as a plain bad request.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise MacOSError(
            f"interval must be a positive, finite number, not {interval!r}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "interval", "value": interval},
        )


def _validate_app_selector(value: str | int | None, *, parameter: str) -> None:
    """Reject an empty ``app``/``apps`` string or a non-positive pid
    before any resolution, dispatch, or ``once``-token reservation.

    A resolution failure downstream (``ApplicationNotFoundError`` and
    friends) already covers a selector that is merely *wrong*; this
    catches one that could never have been right -- ``""`` never names
    a running app, and a pid is never zero or negative -- so those fail
    fast as a plain `BAD_REQUEST`, before ever reaching `MacOS`'s own
    resolution machinery (or, worse, a live AX call).
    """
    if value is None:
        return
    if isinstance(value, str):
        if not value:
            raise MacOSError(
                f"{parameter} must be a nonempty string, not ''",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": parameter},
            )
        return
    if value <= 0:
        raise MacOSError(
            f"{parameter} must be a positive pid, not {value}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": parameter, "value": value},
        )


def _validate_apps_selector(apps: str | int | tuple[str | int, ...] | None) -> None:
    """Reject an empty string or non-positive pid inside an already
    materialized ``apps`` selector, item by item.

    Must run *after* `Operations._freeze_apps` has already turned any
    generator/iterable into a concrete `tuple` -- validating straight
    off the caller's own ``apps`` argument would risk consuming a
    generator a second time, silently losing items to
    `test_apps_generator_is_materialized_once_and_reused`'s
    single-materialization contract.
    """
    if apps is None or isinstance(apps, (str, int)):
        _validate_app_selector(apps, parameter="apps")
        return
    for item in apps:
        _validate_app_selector(item, parameter="apps")


def _validate_set_value(value: JSONValue) -> JSONValue:
    """Validate and freeze ``value`` for a ``set`` request's own receipt
    copy via `canonicalize`, before anything host-related happens.

    Only the receipt's copy of ``value`` is replaced by this frozen,
    JSON-safe form -- ``host.set`` itself always still receives the
    caller's own raw ``value`` object unchanged, since a canonicalized
    ``tuple``/``MappingProxyType`` copy is not guaranteed to bridge to
    the host the same way a plain ``list``/``dict`` would.
    """
    try:
        return canonicalize(value)
    except (TypeError, ValueError) as exc:
        raise MacOSError(
            f"set value is not JSON-safe: {exc}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "value"},
        ) from exc


def _value_summary(value: JSONValue) -> dict[str, JSONValue]:
    """A deterministic, non-reversible ``{"type", "length", "sha256"}``
    summary of a `set`/`toggle` attribute value for a `Receipt`, never
    the raw value itself.

    `set`/`toggle` still read and write the real value against the host
    to do their job -- an AX text field's actual password, say -- and
    every internal before/after/convergence comparison still uses that
    real value unchanged. Only what a `Receipt` (or the `error` it
    carries) -- something a caller may log, print, or persist -- ever
    stores about that value changes: enough to tell two values apart,
    or confirm one did not change, without ever reproducing it.
    """
    text = canonical_json(canonicalize(value))
    return {
        "type": type(value).__name__,
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


class _Deadline:
    """One cooperative budget shared across operation phases."""

    __slots__ = ("_deadline", "_monotonic", "_start")

    def __init__(self, timeout: float, monotonic: Callable[[], float]) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise MacOSError(
                f"timeout must be a non-negative, finite number, not {timeout!r}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "timeout", "value": timeout},
            )
        now = monotonic()
        self._monotonic = monotonic
        self._start = now
        self._deadline = now + timeout

    @property
    def expires_at(self) -> float:
        return self._deadline

    def check_dispatch(self, details: dict[str, JSONValue] | None = None) -> None:
        if self.exhausted():
            raise MacOSError(
                "Operation timed out: deadline exhausted before dispatch",
                code=ErrorCode.TIMEOUT,
                details={**(details or {}), "reason": "deadline_exhausted_before_dispatch"},
            )

    def remaining(self) -> float:
        return max(0.0, self._deadline - self._monotonic())

    def exhausted(self) -> bool:
        return self.remaining() <= 0.0

    def elapsed(self) -> float:
        return self._monotonic() - self._start


def _deadline_exhausted_error(reason: str, message: str) -> ErrorPayload:
    """A structured `ErrorPayload` for one of ``mac.do``'s two deadline-
    exhaustion outcomes -- see the ``deadline_exhausted_before_dispatch``
    and ``deadline_exhausted_before_verification`` reasons at each call
    site -- both `ErrorCode.TIMEOUT`, distinguished only by ``reason``.
    """
    return {"code": ErrorCode.TIMEOUT.value, "message": message, "details": {"reason": reason}}


def _changed_after_dispatch(postcondition: Postcondition | None, verified: _Verification) -> bool | None:
    """Whether a dispatched ``press``/``run`` effect is confirmed changed:
    `True` only once a postcondition has actually verified it, `None`
    otherwise.

    ``press``/``run`` have no attribute value to read back before and
    after the way `set`/`toggle` do, so a bare dispatch with no
    postcondition -- or one that failed to verify -- can never claim a
    confirmed `True`/`False`; only an explicit, *passed* postcondition
    check ever earns that confidence. The raw-input verbs add one more
    witness on top, the focus sample -- see `_changed_focus_fields`.
    """
    return True if postcondition is not None and verified.ok else None


# How a raw-input verb watches for its effect on focus. The first reading
# right after dispatch missed 70 of 80 effects that later showed; across
# 360 key, click and packed-typing trials over four apps the effect showed
# within 20ms in most, 47ms at p95 and 62ms at the latest. So when the
# caller gave no postcondition the receipt samples every 10ms for up to
# 100ms -- never past the shared deadline -- and stops at the first reading
# that differs from the one before dispatch. An explicit postcondition is
# the effect the caller asked about, so it is verified straight after the
# one immediate reading instead of waiting behind this witness.
_FOCUS_POLL_SECONDS = 0.01
_FOCUS_POLLS = 10


def _sample_focus(host: _Host, pid: int) -> dict[str, JSONValue] | None:
    """`MacOS._focus_sample`, or `None` when the app's AX tree would not
    answer (a non-finite geometry raises ``ax_error``, for one). The
    reading is a witness for the receipt, never the action, so a failed
    read must not fail -- or strand the `once` token of -- the input it
    was meant to observe."""
    try:
        return host._focus_sample(pid)
    except MacOSError:
        return None


def _changed_focus_fields(before: Mapping[str, JSONValue], after: Mapping[str, JSONValue]) -> list[str]:
    """The names of the `MacOS._focus_sample` fields that differ between
    ``before`` and ``after``, ``focused.<field>`` for the focused element's
    own attributes, in a stable order.

    A non-empty list is a confirmed change: the app's own focus state
    moved between the two readings. An empty one proves nothing -- a
    ``cmd+s`` saves without touching focus -- so it never becomes
    ``changed=False``.
    """
    changed = [name for name in ("frontmost_pid", "window") if before.get(name) != after.get(name)]
    focused_before = before.get("focused")
    focused_after = after.get("focused")
    if isinstance(focused_before, Mapping) and isinstance(focused_after, Mapping):
        changed.extend(
            f"focused.{name}"
            for name in sorted(focused_before.keys() | focused_after.keys())
            if focused_before.get(name) != focused_after.get(name)
        )
    elif focused_before != focused_after:
        changed.append("focused")
    return changed


def _redact_focus(sample: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    """A `MacOS._focus_sample` reading fit for a `Receipt`: the focused
    element's ``value`` reduced to its `_value_summary`, everything else
    as read. The raw reading is only ever compared, never stored."""
    focused = sample.get("focused")
    if not isinstance(focused, Mapping) or "value" not in focused:
        return dict(sample)
    return {**sample, "focused": {**focused, "value": _value_summary(focused["value"])}}


def _atomic_press_acted(exc: MacOSError) -> Acted:
    """Classify one atomic `MacOS.ax_press` failure for `press()`, where
    resolution and dispatch are one inseparable call and so cannot be
    told apart by which step actually failed.

    ``focus.changed`` only ever comes from the guard that runs *after*
    `AXPress` has landed, so it is the one code guaranteed to mean the
    press happened; ``element.unknown``/``bad_request``/
    ``unsupported_op`` are exactly as certain in the other direction --
    no match, an ambiguous one, or one without the action at all.
    A timeout explicitly marked as before dispatch also means no action.
    Other timeouts remain unknown because dispatch may already have happened.
    """
    if exc.code == ErrorCode.FOCUS_CHANGED.value:
        return Acted.YES
    if exc.code == ErrorCode.TIMEOUT.value and exc.details.get("reason") == "deadline_exhausted_before_dispatch":
        return Acted.NO
    if exc.code in {ErrorCode.ELEMENT_UNKNOWN.value, ErrorCode.BAD_REQUEST.value, ErrorCode.UNSUPPORTED_OP.value}:
        return Acted.NO
    return Acted.UNKNOWN


@dataclass(frozen=True, slots=True)
class _Verification:
    """The result of checking one optional `Postcondition`.

    ``state`` is the whole result: `Observation.MET` is the only
    verified outcome, and the other two record *why* the check did not
    verify in the one place that still knows -- the code that called the
    search and caught its error. ``ok`` is derived so the two can never
    drift apart.
    """

    state: Observation
    observed: JSONValue
    error: ErrorPayload | None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.state is Observation.MET


def _observation_of(postcondition: Postcondition, exc: MacOSError) -> tuple[Observation, str]:
    """Classify one failed `Postcondition` check as `Observation.UNMET`
    or `Observation.UNOBSERVABLE`, with the reason that decided it.

    This is the one place that reads a search's failure payload for
    meaning, and it sits next to the code that raised it. A caller that
    re-derives the same distinction from ``error.details`` downstream
    gets it wrong: a `Gone` check that timed out holding one complete
    empty poll looks exactly like a plain timeout, yet it saw the match
    *absent* -- it simply never got the second poll that confirms it.
    """
    details = exc.details
    if exc.code != ErrorCode.TIMEOUT.value:
        ambiguous = exc.code == ErrorCode.BAD_REQUEST.value and "count" in details
        return Observation.UNOBSERVABLE, "ambiguous_match" if ambiguous else "observation_refused"
    if "reason" in details:
        # `_deadline_exhausted_error`: no window to look in at all.
        return Observation.UNOBSERVABLE, "deadline_exhausted"
    if details.get("complete") is False:
        return Observation.UNOBSERVABLE, "incomplete_search"
    if isinstance(postcondition, Gone):
        polls = details.get("consecutive_empty_polls")
        if not isinstance(polls, int):
            return Observation.UNOBSERVABLE, "observation_refused"
        # A complete empty poll already saw the match gone; only `Gone`'s
        # two-poll confirmation rule is missing, so absence is unconfirmed
        # rather than contradicted.
        return (Observation.UNOBSERVABLE, "absence_unconfirmed") if polls else (
            Observation.UNMET, "still_present"
        )
    if "expected" in details:
        return Observation.UNMET, "value_differs"
    if "timeout" in details:
        # A complete search that ran out of time without a match.
        return Observation.UNMET, "searched_and_absent"
    return Observation.UNOBSERVABLE, "observation_refused"


def _perform_guarded(
    host: _Host, element_index: int, action: str, target_pid: int, operation: str,
    before: dict[str, JSONValue] | None,
) -> None:
    """Perform ``action`` on ``element_index``, then check focus.

    The focus guard only ever runs *after* ``action`` has returned
    successfully -- mirroring ``MacOS.ax_press``'s own dispatch-then-guard
    order -- so a caller catching `FocusChangedError` here always knows
    the action itself landed, and an exception ``action`` itself raises
    always propagates as-is, never replaced by whatever the guard would
    have raised for an action that never actually happened.
    """
    host.perform_action(element_index, action)
    host._guard_focus(before, target_pid, operation)


# --- `run` subprocess handling -------------------------------------------


#: Hard caps on a `run` request's ``source``/``args``, checked before any
#: subprocess spawn or ``once``-token reservation (see `_validate_run_input`):
#: past these sizes this is no longer "run a short script", and letting an
#: unbounded string reach ``osascript``'s stdin (or its argv) is its own
#: resource-exhaustion surface, independent of anything the script itself
#: goes on to do.
_MAX_SOURCE_CHARS = 262_144
_MAX_ARGS = 256
_MAX_ARG_CHARS = 65_536
# The most text one `type` call sends. Packed typing lands 128 characters
# in about 30ms, so this bound is a few seconds of key events at the
# 10ms-per-character pace an inactive app gets, not a ceiling anyone
# writing a form hits.
_MAX_TYPE_CHARS = 4096


def _validate_utf8_text(value: str, *, parameter: str, details: dict[str, JSONValue] | None = None) -> None:
    """Reject ``value`` if it contains a NUL byte or a lone surrogate that
    cannot round-trip through UTF-8 -- before this ever reaches a
    subprocess spawned with ``text=False`` (`_run_osascript` encodes
    stdin as UTF-8 itself) or gets reserved as part of a ``once`` token.
    """
    base = dict(details) if details else {}
    if "\x00" in value:
        raise MacOSError(
            f"{parameter} must not contain a NUL character",
            code=ErrorCode.BAD_REQUEST,
            details={**base, "parameter": parameter},
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MacOSError(
            f"{parameter} is not valid UTF-8 text: {exc}",
            code=ErrorCode.BAD_REQUEST,
            details={**base, "parameter": parameter},
        ) from exc


#: The only two languages ``osascript``/``osacompile`` actually support
#: (see ``osascript -l``) -- checked before any subprocess spawn or
#: ``once``-token reservation (see `_validate_language`).
_SUPPORTED_LANGUAGES = frozenset({"AppleScript", "JavaScript"})


def _validate_language(language: str) -> None:
    """Reject a ``run``/``run(dry_run=True)`` ``language`` that is not
    exactly one of ``osascript``'s two supported languages -- before
    anything else this operation does, including a ``once`` token
    reservation.

    ``language`` reaches `subprocess.Popen`'s own argv as a plain ``-l``
    element (never shell-interpolated), so this is not an injection
    risk -- but an unsupported value is always going to fail there
    (wasting a spawn and reservation on a doomed call), and a NUL byte
    specifically makes `subprocess.Popen` itself raise a bare
    `ValueError` outside `_run_osascript`'s own `OSError` handling,
    which would otherwise strand a reserved token.
    """
    if language not in _SUPPORTED_LANGUAGES:
        raise MacOSError(
            f"language must be one of {sorted(_SUPPORTED_LANGUAGES)}, not {language!r}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "language", "value": language},
        )


def _validate_run_input(source: str, argv: Sequence[str]) -> None:
    """Reject an oversized or malformed ``run`` ``source``/``args`` payload
    before anything else this operation does -- including a ``once``
    token reservation -- so a malformed request can never strand a token
    it will now never finish.
    """
    if len(source) > _MAX_SOURCE_CHARS:
        raise MacOSError(
            f"source must be at most {_MAX_SOURCE_CHARS} characters, not {len(source)}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "source", "length": len(source), "limit": _MAX_SOURCE_CHARS},
        )
    _validate_utf8_text(source, parameter="source")
    if len(argv) > _MAX_ARGS:
        raise MacOSError(
            f"args must have at most {_MAX_ARGS} items, not {len(argv)}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "args", "count": len(argv), "limit": _MAX_ARGS},
        )
    for index, item in enumerate(argv):
        if len(item) > _MAX_ARG_CHARS:
            raise MacOSError(
                f"args[{index}] must be at most {_MAX_ARG_CHARS} characters, not {len(item)}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "args", "index": index, "length": len(item), "limit": _MAX_ARG_CHARS},
            )
        _validate_utf8_text(item, parameter="args", details={"index": index})


def _validate_type_text(text: str) -> None:
    """Reject an oversized or malformed ``type`` ``text`` before the
    ``once`` token is reserved, for the same reason `_validate_run_input`
    runs first: the text is what the key events carry, and a lone
    surrogate cannot be encoded into one."""
    if len(text) > _MAX_TYPE_CHARS:
        raise MacOSError(
            f"text must be at most {_MAX_TYPE_CHARS} characters, not {len(text)}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "text", "length": len(text), "limit": _MAX_TYPE_CHARS},
        )
    _validate_utf8_text(text, parameter="text")


def _validate_fill_value(value: str) -> None:
    """Reject a ``fill`` ``value`` no keyboard input could carry: one
    `_validate_type_text` already refuses, or one holding a control
    character.

    `fill` types its value as key events and never submits, so a
    control character in it would press keys the caller did not ask for
    -- a newline submitting the form, a tab leaving the field. This is
    the whole rule for a fill value, in one place, so a caller that
    validates a value before some later fill (a recorded route replaying
    a parameterized input) rejects exactly what `fill` itself would,
    rather than carrying a second copy that drifts.
    """
    _validate_type_text(value)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MacOSError("fill does not accept control characters", code=ErrorCode.BAD_REQUEST)


class _Process(Protocol):
    pid: int
    returncode: int | None
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None

    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def kill(self) -> None: ...


class _Spawner(Protocol):
    def __call__(
        self,
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        shell: bool,
        start_new_session: bool,
    ) -> _Process: ...


#: How much of a `run` subprocess's stdout/stderr `_StreamBox` keeps
#: decoded text for, and only when a caller opts in with
#: ``capture_output=True``. Every byte is always hashed and counted (see
#: `_StreamBox.add`) regardless; the text itself is capped separately so a
#: captured stream can never grow a `Receipt` past this many characters.
_MAX_OUTPUT_CHARS = 8000

#: Raw-byte cap on `_StreamBox`'s buffer -- generous enough (4 bytes per
#: UTF-8 code point, worst case) to always cover a full `_MAX_OUTPUT_CHARS`
#: decode. Once a stream's buffer hits this, no further bytes are kept:
#: only its running sha256/length keep growing, so a child that goes on to
#: write gigabytes more can never grow this process's own memory past it.
_MAX_OUTPUT_BUFFER_BYTES = _MAX_OUTPUT_CHARS * 4

#: Size of each incremental read from a `run` subprocess's stdout/stderr
#: pipe (see `_drain_stream`): large enough that draining a normal-sized
#: script's output takes only a handful of reads, small enough that no
#: single read call ever needs to buffer more than this much at once.
_READ_CHUNK_BYTES = 65536

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _bounded_output(text: str, *, limit: int = _MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    suffix = "…(truncated)"
    return f"{text[: limit - len(suffix)]}{suffix}"


@dataclass(frozen=True, slots=True)
class _StreamCapture:
    """One drained stdout/stderr stream's receipt-safe summary.

    `length`/`sha256` always cover the *entire* stream, however long it
    ran -- `_StreamBox` hashes and counts every byte it drains regardless
    of `capture_output` -- while `text` is the actual decoded content and
    stays `None` unless a caller opted in with ``capture_output=True``: a
    script's own stdout/stderr can carry secrets just as easily as its
    source can (see `_source_summary`), so it is never surfaced by default.
    """

    length: int
    sha256: str
    text: str | None
    truncated: bool

    def payload(self) -> dict[str, JSONValue]:
        payload: dict[str, JSONValue] = {"bytes": self.length, "sha256": self.sha256, "truncated": self.truncated}
        if self.text is not None:
            payload["text"] = self.text
        return payload


def _empty_capture(capture_output: bool) -> _StreamCapture:
    return _StreamCapture(length=0, sha256=_EMPTY_SHA256, text="" if capture_output else None, truncated=False)


class _StreamBox:
    """Where one `_drain_stream` thread accumulates a running sha256, byte
    count, and (only when ``capture_output``) a bounded text buffer for a
    single stdout/stderr pipe, and where `_run_osascript` reads the result
    back -- safely, even having given up waiting on a thread that may
    still be writing to it (see `_abandon`).

    A child that writes gigabytes to its stdout/stderr can never grow this
    process's own memory past `_MAX_OUTPUT_BUFFER_BYTES`: every chunk
    updates the hash/count, and only the first `_MAX_OUTPUT_BUFFER_BYTES`
    worth of it is ever kept, no matter how long the stream runs for.
    """

    __slots__ = ("_buffer", "_capture_output", "_digest", "_length", "_lock")

    def __init__(self, *, capture_output: bool) -> None:
        self._capture_output = capture_output
        self._lock = threading.Lock()
        self._digest = hashlib.sha256()
        self._length = 0
        self._buffer = bytearray()

    def add(self, chunk: bytes) -> None:
        with self._lock:
            self._digest.update(chunk)
            self._length += len(chunk)
            if self._capture_output and len(self._buffer) < _MAX_OUTPUT_BUFFER_BYTES:
                self._buffer.extend(chunk[: _MAX_OUTPUT_BUFFER_BYTES - len(self._buffer)])

    def result(self) -> _StreamCapture:
        with self._lock:
            length = self._length
            sha256 = self._digest.hexdigest()
            raw = bytes(self._buffer)
        if not self._capture_output:
            return _StreamCapture(length=length, sha256=sha256, text=None, truncated=length > _MAX_OUTPUT_BUFFER_BYTES)
        decoded = raw.decode("utf-8", errors="replace")
        text = _bounded_output(decoded)
        truncated = length > len(raw) or len(decoded) > len(text)
        return _StreamCapture(length=length, sha256=sha256, text=text, truncated=truncated)


def _drain_stream(stream: IO[bytes] | None, box: _StreamBox) -> None:
    """Read `stream` to EOF in bounded chunks, off the calling thread --
    see `_StreamBox.add` for why this can never grow this process's own
    memory past a fixed bound no matter how much the child writes.

    An `OSError`/`ValueError` (a broken pipe, or this same pipe's own
    `.close()` racing this call from `_run_osascript`'s cleanup) ends the
    drain exactly like EOF would: whatever `box` already has stands.
    """
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read1(_READ_CHUNK_BYTES)
            if not chunk:
                return
            box.add(chunk)
    except (OSError, ValueError):
        return


def _feed_stdin(stream: IO[bytes] | None, data: bytes) -> None:
    """Write `data` to `stream` and close it, off the calling thread, so a
    child that starts producing output before it has finished reading a
    large ``source`` from stdin can never deadlock this call against its
    own stdin write filling that pipe's buffer.
    """
    if stream is None:
        return
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        return


_TERMINATE_GRACE_S = 0.2

#: Bound on how long `_abandon` waits for a killed process group and its
#: drain/write threads to actually finish. `SIGKILL` cannot be caught or
#: ignored, so the direct child (and every process still sharing its
#: group) cannot outlive it -- this only needs to cover the brief window
#: between the signal landing and the kernel tearing everything down,
#: never how long some unrelated process that merely inherited a pipe's
#: write end and then also escaped the group keeps that end open.
_KILL_REAP_TIMEOUT_S = 1.0


def _kill_process_group(proc: _Process, monotonic: Callable[[], float]) -> None:
    """Signal *every* process in `proc`'s own process group -- not just the
    direct child -- since `_run_osascript` always spawns with
    ``start_new_session=True``, making `proc.pid` that group's id too.

    `SIGTERM` first, so a well-behaved script's own cleanup can run, then
    `SIGKILL` -- which cannot be caught or ignored -- once a short grace
    period passes with the direct child still alive, or immediately if
    `SIGTERM` itself could not even be delivered (the group is already
    gone). `proc.kill()` afterward is a defense-in-depth fallback for the
    direct child specifically, in case `killpg` somehow did not reach it.
    """
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    else:
        deadline = monotonic() + _TERMINATE_GRACE_S
        while proc.poll() is None and monotonic() < deadline:
            time.sleep(0.01)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _close_pipes(proc: _Process) -> None:
    """Close this process's own copies of `proc`'s pipe file objects.

    Deterministic, rather than left to eventual garbage collection, so a
    long-lived `Operations` never accumulates open file descriptors across
    many `run` calls -- and, on the abandon path, closing here can help
    unstick a drain thread still blocked reading a pipe whose only other
    open end belongs to a descendant that outlived the process-group
    signal (see `_drain_stream`'s own `OSError`/`ValueError` handling).
    """
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass


def _abandon(
    proc: _Process,
    threads: tuple[threading.Thread, threading.Thread, threading.Thread],
    monotonic: Callable[[], float],
) -> int | None:
    """Kill `proc`'s entire process group and reap it within a short,
    fixed bound: the one cleanup path for a real timeout, a lingering
    descendant that alone kept a pipe open past the operation's own
    deadline, or an unexpected exception from `_run_osascript`'s own
    bookkeeping.

    Never blocks any longer than `_TERMINATE_GRACE_S` plus
    `_KILL_REAP_TIMEOUT_S` doing it: a daemon thread still stuck past
    that bound is abandoned, not waited on further. The final reap
    swallows a `TimeoutExpired` (the killed group's pipes are still
    lingering, see `_KILL_REAP_TIMEOUT_S`'s own docstring) or a
    `ProcessLookupError` (a real race: the child already exited and was
    reaped elsewhere between the kill above and this wait) -- either way
    this always returns, so a caller here can always still finish with a
    truthful receipt instead of leaving a ``once`` token stranded.
    """
    _kill_process_group(proc, monotonic)
    grace = monotonic() + _KILL_REAP_TIMEOUT_S
    for thread in threads:
        thread.join(timeout=max(grace - monotonic(), 0.0))
    try:
        proc.wait(timeout=max(grace - monotonic(), 0.0))
    except (subprocess.TimeoutExpired, ProcessLookupError):
        pass
    return proc.returncode


@dataclass(frozen=True, slots=True)
class _ScriptResult:
    ok: bool
    started: bool
    acted: Acted
    timed_out: bool
    returncode: int | None
    stdout: _StreamCapture
    stderr: _StreamCapture
    error: ErrorPayload | None


def _failure_message(command: list[str], returncode: int, stdout: _StreamCapture, stderr: _StreamCapture) -> str:
    # `.text` is `None` on both streams unless the caller opted in with
    # `capture_output=True` (see `_StreamCapture`), so this never quotes
    # the script's own output back in a receipt's error message by default.
    detail = ((stderr.text or "") or (stdout.text or "")).strip()
    return detail or f"{Path(command[0]).name} exited {returncode}"


def _run_osascript(
    command: list[str],
    source: str,
    deadline: _Deadline,
    spawn: _Spawner,
    *,
    capture_output: bool,
    monotonic: Callable[[], float],
) -> _ScriptResult:
    """Run one script with argv, stdin, one shared deadline, its own
    process group, and no shell.

    stdin is written and stdout/stderr are drained on dedicated daemon
    threads (`_feed_stdin`/`_drain_stream`) so a child that produces
    unbounded output can never grow this process's own memory, and a
    large ``source`` can never deadlock this call against the child's own
    stdout/stderr pipes filling up while stdin is still being written. A
    timeout -- including one caused only by a ``do shell script "... &"``
    descendant that outlived ``osascript`` itself and kept a pipe's write
    end open -- signals the *whole* process group (`_kill_process_group`),
    never just the direct child. This always returns a `_ScriptResult`
    instead of raising, with one deliberate exception: a genuine
    `KeyboardInterrupt`/`SystemExit` still gets the same cleanup below,
    but is then re-raised rather than swallowed into a receipt -- any
    *other* unexpected failure here still needs a truthful receipt,
    never a stranded ``once`` token (see `_abandon`).
    """
    try:
        proc = spawn(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        return _ScriptResult(
            ok=False,
            started=False,
            acted=Acted.NO,
            timed_out=False,
            returncode=None,
            stdout=_empty_capture(capture_output),
            stderr=_empty_capture(capture_output),
            error={"code": ErrorCode.AX_ERROR.value, "message": str(exc), "details": {}},
        )

    stdout_box = _StreamBox(capture_output=capture_output)
    stderr_box = _StreamBox(capture_output=capture_output)
    stdin_thread = threading.Thread(target=_feed_stdin, args=(proc.stdin, source.encode("utf-8")), daemon=True)
    stdout_thread = threading.Thread(target=_drain_stream, args=(proc.stdout, stdout_box), daemon=True)
    stderr_thread = threading.Thread(target=_drain_stream, args=(proc.stderr, stderr_box), daemon=True)
    threads = (stdin_thread, stdout_thread, stderr_thread)

    try:
        for thread in threads:
            thread.start()
        for thread in (stdout_thread, stderr_thread, stdin_thread):
            thread.join(timeout=deadline.remaining())
        clean = not any(thread.is_alive() for thread in threads)
        returncode: int | None = None
        if clean:
            try:
                returncode = proc.wait(timeout=deadline.remaining())
            except subprocess.TimeoutExpired:
                clean = False
        if not clean:
            returncode = _abandon(proc, threads, monotonic)
    except (KeyboardInterrupt, SystemExit):
        # A genuine interrupt still gets the same cleanup as any other
        # failure here -- never a leaked child or descendant -- but is
        # deliberately re-raised, not swallowed into a receipt: unlike
        # every other exception below, there is no well-defined
        # `_ScriptResult` for "the caller asked this whole process to
        # stop", and `once`-token stranding here mirrors the same
        # accepted, documented tradeoff other verbs already make for a
        # mid-dispatch `KeyboardInterrupt` (see `Operations._dispatch_lock`).
        _abandon(proc, threads, monotonic)
        _close_pipes(proc)
        raise
    except (RuntimeError, OSError) as exc:
        # `RuntimeError`: `Thread.start()` failing to allocate a new OS
        # thread. `OSError` (beyond the already-handled
        # `subprocess.TimeoutExpired`): a `Popen.wait()` race such as
        # `ProcessLookupError` if the child was reaped elsewhere.
        returncode = _abandon(proc, threads, monotonic)
        _close_pipes(proc)
        return _ScriptResult(
            ok=False,
            started=True,
            acted=Acted.UNKNOWN,
            timed_out=False,
            returncode=returncode,
            stdout=stdout_box.result(),
            stderr=stderr_box.result(),
            error={
                "code": ErrorCode.AX_ERROR.value,
                "message": f"{Path(command[0]).name} handling failed unexpectedly: {exc}",
                "details": {},
            },
        )

    _close_pipes(proc)
    stdout_capture = stdout_box.result()
    stderr_capture = stderr_box.result()

    if not clean:
        return _ScriptResult(
            ok=False,
            started=True,
            acted=Acted.UNKNOWN,
            timed_out=True,
            returncode=returncode,
            stdout=stdout_capture,
            stderr=stderr_capture,
            error={
                "code": ErrorCode.TIMEOUT.value,
                "message": f"{Path(command[0]).name} did not finish before the deadline and was killed",
                "details": {},
            },
        )
    if returncode != 0:
        return _ScriptResult(
            ok=False,
            started=True,
            acted=Acted.YES,
            timed_out=False,
            returncode=returncode,
            stdout=stdout_capture,
            stderr=stderr_capture,
            error={
                "code": ErrorCode.AX_ERROR.value,
                "message": _failure_message(command, returncode, stdout_capture, stderr_capture),
                "details": {"returncode": returncode},
            },
        )
    return _ScriptResult(
        ok=True,
        started=True,
        acted=Acted.YES,
        timed_out=False,
        returncode=returncode,
        stdout=stdout_capture,
        stderr=stderr_capture,
        error=None,
    )


# --- at-most-once ledger --------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    op: str
    fingerprint: str
    receipt: Receipt | None = None


class _Ledger:
    """Thread-safe at-most-once dispatch ledger for the ``once`` verbs:
    ``press``, ``run``, ``key``, ``click`` and ``type``.

    A single lock guards the bookkeeping only: `reserve` writes an entry
    (or reports what the caller should do instead) while holding the lock,
    then releases it immediately -- the mutating dispatch itself always
    runs outside the lock, so one slow operation never blocks a lookup for
    any other token, including a concurrent duplicate of the *same* token
    (handled by the ``"in_flight"`` report, which never blocks at all).

    Scoped to exactly one live `Operations` instance, for exactly that
    instance's process lifetime: `Operations.__init__` constructs a
    fresh, empty `_Ledger` every time, so a token is only ever
    "at-most-once" against the one instance it was used on -- a second,
    independent `Operations` (even for the same `MacOS`, even in the
    same process) never sees another instance's reservations. Token
    records are never evicted. A known pre-dispatch press timeout releases
    its unused reservation. The latest 256 non-replayed completions share
    their receipt objects; history eviction never frees a token.
    Neither collection is written to disk or shared across a process
    boundary. A restart or collection of the owning instance loses both.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _LedgerEntry] = {}
        self._completed: deque[Receipt] = deque(maxlen=256)

    @staticmethod
    def _classify(
        token: str,
        op: str,
        fingerprint: str,
        entry: _LedgerEntry | None,
    ) -> Literal["new", "replay", "in_flight"]:
        if entry is None:
            return "new"
        if entry.op != op or entry.fingerprint != fingerprint:
            raise MacOSError(
                f"once token {token!r} is already recorded for a different {entry.op} request",
                code=ErrorCode.BAD_REQUEST,
                details={"once": token, "op": entry.op},
            )
        return "in_flight" if entry.receipt is None else "replay"

    def inspect(
        self,
        token: str,
        op: str,
        fingerprint: str,
    ) -> Literal["new", "replay", "in_flight"]:
        with self._lock:
            return self._classify(token, op, fingerprint, self._entries.get(token))


    def reserve(self, token: str, op: str, fingerprint: str) -> Literal["dispatch", "replay", "in_flight"]:
        with self._lock:
            entry = self._entries.get(token)
            state = self._classify(token, op, fingerprint, entry)
            if state == "new":
                self._entries[token] = _LedgerEntry(op=op, fingerprint=fingerprint, receipt=None)
                return "dispatch"
            return state

    def stored(self, token: str) -> Receipt:
        with self._lock:
            receipt = self._entries[token].receipt
        assert receipt is not None
        return receipt

    def finalize(self, token: str, receipt: Receipt) -> None:
        with self._lock:
            entry = self._entries.get(token)
            fingerprint = entry.fingerprint if entry is not None else request_fingerprint(receipt.request)
            self._entries[token] = _LedgerEntry(op=receipt.op, fingerprint=fingerprint, receipt=receipt)

    def release(self, token: str) -> None:
        with self._lock:
            del self._entries[token]

    def peek(self, token: str) -> tuple[Literal["unknown", "in_flight", "done"], str | None, Receipt | None]:
        with self._lock:
            entry = self._entries.get(token)
            if entry is None:
                return "unknown", None, None
            if entry.receipt is None:
                return "in_flight", entry.op, None
            return "done", entry.op, entry.receipt

    def remember(self, receipt: Receipt) -> None:
        if not receipt.replayed:
            with self._lock:
                self._completed.append(receipt)

    def history(self) -> tuple[Receipt, ...]:
        with self._lock:
            return tuple(self._completed)


# --- the host surface `Operations` depends on -----------------------------


class _AXSurface(Protocol):
    def _search_key(self, search_key: str | None, role: str | None) -> str: ...


class _RoutedWindow(Protocol):
    """What `MacOS._target_window` resolves a click to: the window the
    events are bound to, of which only the id is recorded."""

    @property
    def window_id(self) -> int: ...


class _Host(Protocol):
    """The exact private/public `MacOS` surface `Operations` calls.

    Structural, not nominal: `MacOS` satisfies this without inheriting
    from it, and so can a hermetic test double, without importing PyObjC
    or constructing a real `MacOS` at all.
    """

    _backend: str
    ax: _AXSurface

    @property
    def _native_client(self) -> object | None: ...

    def _resolve_app(
        self,
        query: str | int | None,
    ) -> tuple[object, dict[str, JSONValue]]: ...
    def _identity_from_info(self, info: Mapping[str, JSONValue]) -> _AppIdentity: ...
    def _observe_process(self, expected: _AppIdentity) -> dict[str, JSONValue]: ...
    def _ensure_accessibility(self) -> None: ...
    def _ensure_post_events(self) -> None: ...
    def _validate_key(self, key: str) -> None: ...
    def _frontmost_app(self) -> dict[str, JSONValue] | None: ...
    def _guard_focus(
        self,
        before: dict[str, JSONValue] | None,
        target_pid: int,
        operation: str,
    ) -> None: ...
    def _element(self, element_index: int) -> object: ...
    def _is_ax_element(self, value: object) -> bool: ...
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
        include_actions: bool = False,
        max_nodes: int = 500,
        enhance: bool = True,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, JSONValue]: ...
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
        enhance: bool = True,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None: ...
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
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
        _deadline: _Deadline | None = None,
    ) -> dict[str, JSONValue]: ...
    def get(
        self, element_index: int, attribute: str = "AXValue", *, missing_ok: bool = False
    ) -> object: ...
    def set(self, element_index: int, value: object, attribute: str = "AXValue") -> None: ...
    def perform_action(self, element_index: int, action: str = "AXPress") -> None: ...
    def key(self, key: str, *, app: str | int | None = None) -> None: ...
    def type(self, text: str, *, app: str | int | None = None) -> None: ...
    def _validate_button(self, button: str) -> str: ...
    def _validate_clicks(self, clicks: int) -> int: ...
    def _focus_sample(self, pid: int) -> dict[str, JSONValue]: ...
    def _screen_point(
        self, x: float, y: float, coordinate_space: str, *, pid: int | None = None
    ) -> tuple[float, float]: ...
    def _target_window(self, pid: int, point: tuple[float, float]) -> _RoutedWindow: ...
    def _post_click(
        self,
        pid: int,
        point: tuple[float, float],
        window: _RoutedWindow,
        *,
        button: str,
        clicks: int,
    ) -> None: ...


# --- receipt construction ---------------------------------------------


def _utc_timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class _ReceiptBuilder:
    """Bundles what every `Receipt` a single verb call constructs shares
    -- `op`, `backend`, `executor`, `request`, `once`, and that call's
    own shared `_Deadline` -- so each call site spells out only what
    varies between one outcome and the next.

    `executor` starts as `_default_executor(host)`'s best guess and is
    updated when resolution identifies the backend that did the work.
    """

    op: str
    backend: str
    executor: Executor
    request: Mapping[str, JSONValue]
    once: str | None
    deadline: _Deadline
    wall_clock: Callable[[], float]
    host: _Host
    started_at: str = dataclasses.field(init=False)
    identity: _AppIdentity | None = None
    app_info: Mapping[str, JSONValue] | None = None
    process: JSONValue = None

    def bind_app(self, info: Mapping[str, JSONValue]) -> None:
        self.app_info = info
        try:
            self.identity = self.host._identity_from_info(info)
        except MacOSError as exc:
            self.process = {"pid": info.get("pid"), "state": "unknown", "error": exc.to_json()}

    def __post_init__(self) -> None:
        self.started_at = _utc_timestamp(self.wall_clock())

    def build(
        self,
        *,
        outcome: Outcome,
        acted: Acted,
        changed: bool | None,
        verified: bool,
        target: JSONValue = None,
        observed: JSONValue = None,
        error: ErrorPayload | None = None,
        duration_s: float | None = None,
    ) -> Receipt:
        process = self.process if self.identity is None else self.host._observe_process(self.identity)
        if (isinstance(process, Mapping) and process.get("state") == "exited"
                and outcome is Outcome.DONE and acted is Acted.YES and not verified):
            outcome = Outcome.FAILED
            error = MacOSError("The target app exited before its effect was confirmed",
                               code=ErrorCode.APP_EXITED).to_json()
        if target is None and self.app_info is not None:
            target = {"app": self.app_info}
        return Receipt(
            op=self.op,
            outcome=outcome,
            acted=acted,
            backend=self.backend,
            executor=self.executor,
            request=self.request,
            target=target,
            observed=observed,
            changed=changed,
            verified=verified,
            duration_s=self.deadline.elapsed() if duration_s is None else duration_s,
            process=process,
            started_at=self.started_at,
            finished_at=_utc_timestamp(self.wall_clock()),
            once=self.once,
            error=error,
        )


# --- Operations ------------------------------------------------------------


class Operations:
    """``mac.do``: receipted mutations, read-only assertions, and token recall.

    Once a call's arguments pass preflight and it actually begins
    resolving or dispatching something, it returns -- or raises with --
    a `Receipt`. A call rejected before that point -- bad argument
    shape, a fork-boundary violation, an invalidly-shaped deadline, or a
    ``once`` token already recorded for a different request -- raises a
    plain `MacOSError` instead, with no receipt to carry. See
    `receipts.py`'s module docstring for the full contract.
    """

    def __init__(
        self,
        host: _Host,
        *,
        _spawn: _Spawner = subprocess.Popen,
        _monotonic: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
        _wall_clock: Callable[[], float] = time.time,
    ) -> None:
        # A weak reference, exactly like `Accessibility` in controls.py:
        # `MacOS.__init__` does `self.do = Operations(self)`, so a strong
        # reference back here would keep a `MacOS` and its native agent
        # child alive until the next cyclic-GC pass instead of the instant
        # the last external reference drops.
        self._host_ref = weakref.ref(host)
        self._ledger = _Ledger()
        self._monotonic = _monotonic
        self._sleep = _sleep
        self._wall_clock = _wall_clock
        self._spawn = _spawn
        self._creator_pid = os.getpid()
        # Reentrant so a helper called while this thread already holds
        # the lock (from inside one verb's own resolve->verify pipeline)
        # can never self-deadlock by acquiring it again.
        self._dispatch_lock = threading.RLock()

    @property
    def _host(self) -> _Host:
        host = self._host_ref()
        if host is None:
            raise MacOSError(
                "This Operations (mac.do) surface's MacOS instance has already "
                "been closed or garbage collected; construct a new MacOS to use "
                "mac.do again",
                code=ErrorCode.UNSUPPORTED_OP,
            )
        return host

    def _check_owner(self) -> None:
        """Fail closed if used from a different process than created it.

        Mirrors `NativeClient._check_owner` in native.py: the only way
        this can happen is a `fork()` elsewhere in the embedding
        application, and a forked child inherits a byte-for-byte copy of
        this object, including `self._dispatch_lock` and `self._ledger`'s
        own lock, without ever having gone through `__init__` itself.
        Every verb below calls this as its literal first statement --
        before it ever touches `self._dispatch_lock` -- deliberately: if
        some other thread of the parent process held that lock at the
        exact instant of `fork()`, it stays held forever in a forked
        child (that thread does not exist here to release it), and
        acquiring it first would hang this check, and everything after
        it, rather than ever reaching this fail-closed raise.
        """
        pid = os.getpid()
        if pid != self._creator_pid:
            raise MacOSError(
                f"This Operations (mac.do) surface was created in pid "
                f"{self._creator_pid} and cannot be used from pid {pid} (a "
                "fork boundary was crossed); construct a fresh Operations "
                "in this process instead",
                code=ErrorCode.UNSUPPORTED_OP,
                details={"creator_pid": self._creator_pid, "pid": pid},
            )

    def history(self) -> tuple[Receipt, ...]:
        """Return the latest 256 non-replayed receipts in completion order."""
        self._check_owner()
        return self._ledger.history()

    def expect(self, condition: Postcondition) -> Receipt:
        """Observe an explicitly scoped condition without changing the app.

        Uses the same verifier as mutation postconditions. The condition's
        timeout defaults to five seconds; its interval controls polling.
        Accessibility enhancements and focus sampling are disabled.

        A condition that does not hold still raises `OperationError`, and
        that error's receipt says which kind of "no" it was: ``observed``
        carries ``{"state", "reason"}``, where ``state`` is
        `Observation.UNMET` when the check completed and the app really
        is in another state, and `Observation.UNOBSERVABLE` when it
        established nothing -- a truncated walk, more than one match, a
        refused read, or a deadline too short for `Gone`'s two
        confirming polls. Only ``UNMET`` licenses a caller to act as
        though the app disagreed; ``UNOBSERVABLE`` means look again. A
        verified check keeps ``observed`` as the match summary.
        """
        self._check_owner()
        host = self._host
        if condition is None:
            raise MacOSError(
                "expect requires a Present, Gone, or Equals condition",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "condition"},
            )
        self._validate_postcondition(host, condition)
        self._validate_postcondition_inheritance(condition, has_scope=False)
        timeout = 5.0 if condition.timeout is None else condition.timeout
        deadline = _Deadline(timeout, self._monotonic)
        request = {"op": "expect", "condition": self._postcondition_payload(condition)}
        builder = _ReceiptBuilder(
            op="expect",
            host=host,
            backend=host._backend,
            executor=self._default_executor(host),
            request=request,
            once=None,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )
        with self._dispatch_lock:
            verification: _Verification | None = None
            try:
                app = self._condition_app(condition)
                if app is not None:
                    try:
                        _, info = host._resolve_app(app)
                    except ApplicationNotFoundError:
                        if not isinstance(condition, Gone):
                            raise
                        if deadline.remaining() > 0:
                            verification = _Verification(
                                state=Observation.MET, observed=None, error=None
                            )
                    else:
                        builder.bind_app(info)
                        condition = dataclasses.replace(condition, app=info["pid"], apps=None)
                if verification is None:
                    verification = self._verify_postcondition(
                        host, condition, deadline, app=None, all_apps=False, apps=None, enhance=False
                    )
            except MacOSError as exc:
                state, reason = _observation_of(condition, exc)
                verification = _Verification(
                    state=state, observed=None, error=exc.to_json(), reason=reason
                )
            builder.executor = self._default_executor(host)
            receipt = builder.build(
                outcome=Outcome.DONE if verification.ok else Outcome.FAILED,
                acted=Acted.NO,
                changed=None,
                verified=verification.ok,
                observed=verification.observed if verification.ok else {
                    "state": verification.state.value,
                    "reason": verification.reason,
                },
                error=verification.error,
            )
            return self._finish(receipt)

    def expect_any(
        self,
        outcomes: Mapping[str, Postcondition],
        *,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> Receipt:
        """Watch several named conditions at once and report which held.

        ``outcomes`` maps a caller's own label -- ``"saved"``,
        ``"error_sheet"``, ``"login_again"`` -- to the explicitly scoped
        `Present`/`Gone`/`Equals` condition that would mean it happened.
        One monotonic budget (``timeout``) covers the whole wait and one
        `Receipt` records it: this is a single observation of a branching
        app, not several `expect` calls glued together, so `history`
        gets one entry however many outcomes were being watched.

        Each pass observes the outcomes in mapping order, with one poll
        apiece, then sleeps ``interval`` before the next pass. No outcome
        receives its own polling wait. The timeout is cooperative: an
        in-progress synchronous AX read cannot be interrupted and may
        delay the remaining outcomes.

        A pass where more than one condition holds reports all of them:
        ``observed`` is ``{"matched": [label, ...], "outcomes": {label:
        {"state", "reason", "observed", "polls"}}}``. ``matched`` lists
        every label that held in the winning pass, in mapping order.
        ``outcomes`` carries each label's last real observation -- its
        `Observation`, the reason that decided it, the match summary of
        a `MET` one, and how many times it was actually polled. The
        `Observation.UNMET`/`Observation.UNOBSERVABLE` split is the one
        `expect` documents: only ``UNMET`` licenses a caller to act as
        though the app disagreed, and ``UNOBSERVABLE`` -- a truncated
        walk, more than one match, a refused read -- means look again.

        `Gone` still needs the two spaced empty polls that confirm
        absence, counted across passes instead of inside one call: one
        complete empty poll leaves that label ``UNOBSERVABLE`` with
        ``absence_unconfirmed`` until the next pass repeats it, and
        anything else -- the match back, a walk ``max_nodes`` cut short
        -- resets the count, because a partial read cannot prove
        absence.

        Nothing is pressed, focused, activated, or enhanced. Every
        condition must carry its own scope (``app=``/``apps=``/
        ``all_apps=``), there being no operation scope to inherit, and
        must not set its own ``timeout``: this call owns the one budget,
        so a per-condition one is refused rather than quietly dropped. A
        condition's ``interval`` has nothing to pace here -- each look is
        a single poll, and the ``interval`` above spaces the passes --
        and cannot be refused the same way, because a `Postcondition`
        cannot tell an explicit ``0.1`` from its own default.

        Raises `OperationError` when the budget runs out with no outcome
        observed. That receipt still carries the whole ``observed`` map,
        and its error splits the labels into ``unmet`` and
        ``unobservable``.
        """
        self._check_owner()
        host = self._host
        _validate_interval(interval)
        self._validate_outcomes(host, outcomes)
        deadline = _Deadline(timeout, self._monotonic)
        request = {
            "op": "expect_any",
            "outcomes": {
                label: self._postcondition_payload(condition)
                for label, condition in outcomes.items()
            },
            "timeout": timeout,
            "interval": interval,
        }
        builder = _ReceiptBuilder(
            op="expect_any",
            host=host,
            backend=host._backend,
            executor=self._default_executor(host),
            request=request,
            once=None,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )
        with self._dispatch_lock:
            seen: dict[str, _Verification] = {}
            polls = dict.fromkeys(outcomes, 0)
            empty_polls = dict.fromkeys(outcomes, 0)
            first_pass = True
            while True:
                for label, condition in outcomes.items():
                    # The first pass always looks at every outcome, even
                    # with a zero budget -- one look each is the least
                    # this call can honestly report on, and it is what a
                    # zero timeout means to `ax_wait` too. A later pass
                    # only starts with time left (`_sleep_within` below
                    # is the gate), and stops looking the moment that
                    # runs out, leaving the rest of its labels on the
                    # real observation the previous pass made rather
                    # than spending a budget that is gone.
                    if not first_pass and deadline.exhausted():
                        break
                    seen[label], empty_polls[label] = self._observe_outcome(
                        host, condition, deadline, empty_polls[label]
                    )
                    polls[label] += 1
                matched = [label for label in outcomes if seen[label].ok]
                if matched or not self._sleep_within(deadline, interval):
                    break
                first_pass = False
            builder.executor = self._default_executor(host)
            receipt = builder.build(
                outcome=Outcome.DONE if matched else Outcome.FAILED,
                acted=Acted.NO,
                changed=None,
                verified=bool(matched),
                observed={
                    "matched": matched,
                    "outcomes": {
                        label: {
                            "state": seen[label].state.value,
                            "reason": seen[label].reason,
                            "observed": seen[label].observed,
                            "polls": polls[label],
                        }
                        for label in outcomes
                    },
                },
                error=None if matched else {
                    "code": ErrorCode.TIMEOUT.value,
                    "message": "No expected outcome was observed before the deadline",
                    "details": {
                        "reason": "no_outcome_observed",
                        "unmet": [
                            label for label in outcomes
                            if seen[label].state is Observation.UNMET
                        ],
                        "unobservable": [
                            label for label in outcomes
                            if seen[label].state is Observation.UNOBSERVABLE
                        ],
                    },
                },
            )
            return self._finish(receipt)

    # --- mutation verbs -------------------------------------------------

    def press(
        self,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Press one unique AX target -- the ``mac.do`` counterpart to
        ``mac.ax.press`` -- with a `Receipt`, a shared deadline, an
        optional postcondition, and (with ``once=``) at-most-once dispatch.

        A single, explicit ``app`` (not ``all_apps``/``apps``) that is not
        a dry run dispatches through `MacOS.ax_press` in one atomic
        find-then-press call -- the same primitive ``mac.ax.press`` uses,
        including its own wait/retry and focus guard -- instead of
        resolving and pressing as two separate steps. Every other scope,
        and every dry run, still resolves first and presses second, so a
        dry run can report what it *would* press without ever dispatching
        anything, and ``all_apps``/``apps`` can search more than the one
        app `MacOS.ax_press` presses.
        """
        self._check_owner()
        host = self._host
        self._require_scope(app=app, all_apps=all_apps, apps=apps)
        apps = self._freeze_apps(apps)
        _validate_apps_selector(apps)
        validate_exact_selectors(title=title, identifier=identifier, description=description)
        self._validate_postcondition(host, postcondition)
        self._validate_postcondition_inheritance(postcondition, has_scope=True)
        resolved_key = host.ax._search_key(search_key, role)
        once = self._normalize_once(once)
        _validate_interval(interval)
        deadline = _Deadline(timeout, self._monotonic)
        backend = host._backend
        request: dict[str, JSONValue] = {
            "op": "press",
            "scope": self._scope_payload(app, all_apps, apps),
            "search_key": resolved_key,
            "text": text,
            "title": title,
            "identifier": identifier,
            "description": description,
            "visible_only": visible_only,
            "direction": direction,
            "immediate_descendants_only": immediate_descendants_only,
            "max_nodes": max_nodes,
            "timeout": timeout,
            "interval": interval,
            "postcondition": self._postcondition_payload(postcondition),
        }
        fingerprint = request_fingerprint(request) if once is not None else None
        builder = _ReceiptBuilder(
            op="press",
            host=host,
            backend=backend,
            executor=self._default_executor(host),
            request=request,
            once=once,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )
        if once is not None and not dry_run:
            # Nonblocking on purpose: a concurrent duplicate of this same
            # token must be able to report "in_flight" immediately, even
            # while another thread holds `_dispatch_lock` for an
            # unrelated, still-running operation.
            action = self._ledger.inspect(once, "press", fingerprint)
            if action == "replay":
                return self._finish(self._ledger.stored(once).replayed_as())
            if action == "in_flight":
                return self._finish(self._in_flight_receipt(builder, target=None))

        # Serialized against every other mutating call on this one
        # `Operations` instance.
        with self._dispatch_lock:
            if app is not None and not dry_run:
                if deadline.exhausted():
                    return self._finish(
                        builder.build(
                            outcome=Outcome.FAILED,
                            acted=Acted.NO,
                            changed=False,
                            verified=False,
                            error=_deadline_exhausted_error(
                                "deadline_exhausted_before_dispatch",
                                "No time remained on the shared deadline to dispatch this press",
                            ),
                        )
                    )

                try:
                    _, app_info = host._resolve_app(app)
                    builder.bind_app(app_info)
                except MacOSError as exc:
                    return self._finish(builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, error=exc.to_json(),
                    ))

                if once is not None:
                    assert fingerprint is not None
                    action = self._ledger.reserve(once, "press", fingerprint)
                    if action == "replay":
                        return self._finish(self._ledger.stored(once).replayed_as())
                    if action == "in_flight":
                        return self._finish(self._in_flight_receipt(builder, target=None))

                try:
                    match = host.ax_press(
                        app=app_info["pid"],
                        search_key=resolved_key,
                        text=text,
                        title=title,
                        identifier=identifier,
                        description=description,
                        visible_only=visible_only,
                        direction=direction,
                        immediate_descendants_only=immediate_descendants_only,
                        max_nodes=max_nodes,
                        timeout=timeout,
                        interval=interval,
                        _deadline=deadline,
                    )
                except MacOSError as exc:
                    acted = _atomic_press_acted(exc)
                    receipt = builder.build(
                        outcome=Outcome.FAILED,
                        acted=acted,
                        changed=False if acted is Acted.NO else None,
                        verified=False,
                        error=exc.to_json(),
                    )
                    if once is not None and acted is Acted.NO and exc.code == ErrorCode.TIMEOUT.value:
                        self._ledger.release(once)
                        return self._finish(receipt)
                else:
                    element_index = int(match["element_index"])
                    owner = match.get("app")
                    app_info = owner if isinstance(owner, dict) else host._resolve_app(app)[1]
                    target = self._match_target(match, app_info)
                    builder.executor = self._executor_of(host, element_index)
                    verified = self._verify_postcondition(
                        host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
                    )
                    outcome = Outcome.DONE if verified.ok else Outcome.FAILED
                    receipt = builder.build(
                        outcome=outcome,
                        acted=Acted.YES,
                        changed=_changed_after_dispatch(postcondition, verified),
                        verified=postcondition is not None and verified.ok,
                        target=target,
                        observed=verified.observed,
                        error=None if verified.ok else verified.error,
                    )

                if once is not None:
                    self._ledger.finalize(once, receipt)
                return self._finish(receipt)

            # `all_apps`/`apps` scopes and every dry run: kept as the
            # original resolve -> precheck -> reserve -> dispatch ->
            # verify pipeline, since none of them is what `MacOS.ax_press`
            # -- one atomic call tied to exactly one app -- can serve.
            try:
                match, element_index, app_info, target = self._resolve_target(
                    host,
                    builder,
                    app=app,
                    all_apps=all_apps,
                    apps=apps,
                    search_key=resolved_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    max_nodes=max_nodes,
                    include_actions=True,
                    deadline=deadline,
                    interval=interval,
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, error=exc.to_json(),
                    )
                )

            builder.executor = self._executor_of(host, element_index)

            if dry_run:
                return self._finish(
                    builder.build(
                        outcome=Outcome.PLANNED, acted=Acted.NO, changed=False,
                        verified=False, target=target,
                    )
                )

            actions = match.get("actions") or ()
            if "AXPress" not in actions:
                error: ErrorPayload = {
                    "code": ErrorCode.UNSUPPORTED_OP.value,
                    "message": "Resolved AX target does not expose AXPress; "
                    f"available actions: {list(actions)}",
                    "details": {"actions": list(actions)},
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=error,
                    )
                )

            owner = match.get("app")
            try:
                target_pid = int(owner["pid"]) if isinstance(owner, dict) else int(app_info["pid"])
            except (TypeError, ValueError, KeyError) as exc:
                pid_error: ErrorPayload = {
                    "code": ErrorCode.AX_ERROR.value,
                    "message": f"Resolved target's owning app has a malformed pid: {exc}",
                    "details": {},
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=pid_error,
                    )
                )

            frontmost_before = None if deadline.exhausted() else host._frontmost_app()
            if deadline.exhausted():
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target,
                        error=_deadline_exhausted_error(
                            "deadline_exhausted_before_dispatch",
                            "No time remained on the shared deadline to dispatch this press",
                        ),
                    )
                )

            if once is not None:
                assert fingerprint is not None
                action = self._ledger.reserve(once, "press", fingerprint)
                if action == "replay":
                    return self._finish(self._ledger.stored(once).replayed_as())
                if action == "in_flight":
                    return self._finish(self._in_flight_receipt(builder, target=target))

            try:
                _perform_guarded(
                    host, element_index, "AXPress", target_pid, "mac.do.press", frontmost_before
                )
            except FocusChangedError as exc:
                receipt = builder.build(
                    outcome=Outcome.FAILED, acted=Acted.YES, changed=None, verified=False,
                    target=target, error=exc.to_json(),
                )
            except MacOSError as exc:
                receipt = builder.build(
                    outcome=Outcome.FAILED, acted=Acted.UNKNOWN, changed=None, verified=False,
                    target=target, error=exc.to_json(),
                )
            else:
                verified = self._verify_postcondition(
                    host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
                )
                outcome = Outcome.DONE if verified.ok else Outcome.FAILED
                receipt = builder.build(
                    outcome=outcome, acted=Acted.YES,
                    changed=_changed_after_dispatch(postcondition, verified),
                    verified=postcondition is not None and verified.ok,
                    target=target, observed=verified.observed,
                    error=None if verified.ok else verified.error,
                )

            if once is not None:
                self._ledger.finalize(once, receipt)
            return self._finish(receipt)

    def set(
        self,
        value: JSONValue,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        attribute: str = "AXValue",
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
        postcondition: Postcondition | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Converge one AX attribute to ``value``: read, ``ALREADY`` if it
        already matches, otherwise set once and read back every
        ``interval`` until the value shows or the deadline runs out.

        The mutation is never repeated: a readback that lags behind a
        synchronous set is given the remaining budget to catch up, and a
        value that never appears fails with the last reading. Naturally
        idempotent -- calling this twice with the same arguments is
        always safe -- so, unlike ``press``/``run``/``key``, it takes no
        ``once``.
        """
        self._check_owner()
        host = self._host
        canonical_value = _validate_set_value(value)
        self._require_scope(app=app, all_apps=all_apps, apps=apps)
        apps = self._freeze_apps(apps)
        _validate_apps_selector(apps)
        validate_exact_selectors(title=title, identifier=identifier, description=description)
        self._validate_postcondition(host, postcondition)
        self._validate_postcondition_inheritance(postcondition, has_scope=True)
        resolved_key = host.ax._search_key(search_key, role)
        _validate_interval(interval)
        deadline = _Deadline(timeout, self._monotonic)
        backend = host._backend
        request: dict[str, JSONValue] = {
            "op": "set",
            "scope": self._scope_payload(app, all_apps, apps),
            "search_key": resolved_key,
            "text": text,
            "title": title,
            "identifier": identifier,
            "description": description,
            "attribute": attribute,
            "value": _value_summary(canonical_value),
            "visible_only": visible_only,
            "direction": direction,
            "immediate_descendants_only": immediate_descendants_only,
            "max_nodes": max_nodes,
            "timeout": timeout,
            "interval": interval,
            "postcondition": self._postcondition_payload(postcondition),
        }
        builder = _ReceiptBuilder(
            op="set",
            host=host,
            backend=backend,
            executor=self._default_executor(host),
            request=request,
            once=None,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )

        # Resolve -> precheck -> dispatch -> verify, serialized against
        # every other mutating call on this one `Operations`. `set` takes
        # no `once`, so there is no reservation step here.
        with self._dispatch_lock:
            try:
                _match, element_index, _app_info, target = self._resolve_target(
                    host,
                    builder,
                    app=app,
                    all_apps=all_apps,
                    apps=apps,
                    search_key=resolved_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    max_nodes=max_nodes,
                    include_actions=False,
                    deadline=deadline,
                    interval=interval,
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, error=exc.to_json(),
                    )
                )

            builder.executor = self._executor_of(host, element_index)

            if dry_run:
                return self._finish(
                    builder.build(
                        outcome=Outcome.PLANNED, acted=Acted.NO, changed=False,
                        verified=False, target=target,
                    )
                )

            try:
                before = host.get(element_index, attribute)
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=exc.to_json(),
                    )
                )

            canonical_before = canonicalize(before)
            if canonical_before == canonical_value:
                verified = self._verify_postcondition(
                    host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
                )
                outcome = Outcome.ALREADY if verified.ok else Outcome.FAILED
                return self._finish(
                    builder.build(
                        outcome=outcome, acted=Acted.NO, changed=False,
                        verified=postcondition is not None and verified.ok,
                        target=target, observed=_value_summary(before),
                        error=None if verified.ok else verified.error,
                    )
                )

            if deadline.exhausted():
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target, observed=_value_summary(before),
                        error=_deadline_exhausted_error(
                            "deadline_exhausted_before_dispatch",
                            "No time remained on the shared deadline to dispatch this set",
                        ),
                    )
                )

            try:
                host.set(element_index, value, attribute)
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.UNKNOWN, changed=None, verified=False,
                        target=target, observed=_value_summary(before), error=exc.to_json(),
                    )
                )

            try:
                after, converged = self._read_until(
                    deadline,
                    interval,
                    lambda: host.get(element_index, attribute),
                    lambda reading: canonicalize(reading) == canonical_value,
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.YES, changed=None, verified=False,
                        target=target, observed=_value_summary(before), error=exc.to_json(),
                    )
                )
            changed = canonicalize(after) != canonical_before
            if not converged:
                error: ErrorPayload = {
                    "code": ErrorCode.AX_ERROR.value,
                    "message": f"{attribute} did not converge to the requested value after set",
                    "details": {
                        "attribute": attribute,
                        "requested": _value_summary(canonical_value),
                        "observed": _value_summary(after),
                    },
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.YES, changed=changed, verified=False,
                        target=target, observed=_value_summary(after), error=error,
                    )
                )

            verified = self._verify_postcondition(
                host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
            )
            outcome = Outcome.DONE if verified.ok else Outcome.FAILED
            return self._finish(
                builder.build(
                    outcome=outcome, acted=Acted.YES, changed=changed,
                    verified=postcondition is not None and verified.ok,
                    target=target, observed=_value_summary(after),
                    error=None if verified.ok else verified.error,
                )
            )

    def toggle(
        self,
        desired: bool,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        attribute: str = "AXValue",
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        max_nodes: int = 500,
        timeout: float = 5.0,
        interval: float = 0.1,
        postcondition: Postcondition | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Converge one AX target to a boolean ``desired`` state: read
        ``attribute``, do nothing if it already matches, otherwise press
        the target once and read back every ``interval`` until the state
        shows or the deadline runs out.

        For controls -- checkboxes, switches, menu items -- that only
        expose a press action, not a freely settable attribute. The press
        is never repeated, whatever the readback shows. Naturally
        idempotent, like ``set``; takes no ``once``.
        """
        self._check_owner()
        host = self._host
        self._require_scope(app=app, all_apps=all_apps, apps=apps)
        apps = self._freeze_apps(apps)
        _validate_apps_selector(apps)
        validate_exact_selectors(title=title, identifier=identifier, description=description)
        self._validate_postcondition(host, postcondition)
        self._validate_postcondition_inheritance(postcondition, has_scope=True)
        resolved_key = host.ax._search_key(search_key, role)
        _validate_interval(interval)
        deadline = _Deadline(timeout, self._monotonic)
        backend = host._backend
        request: dict[str, JSONValue] = {
            "op": "toggle",
            "scope": self._scope_payload(app, all_apps, apps),
            "search_key": resolved_key,
            "text": text,
            "title": title,
            "identifier": identifier,
            "description": description,
            "attribute": attribute,
            "desired": _value_summary(bool(desired)),
            "visible_only": visible_only,
            "direction": direction,
            "immediate_descendants_only": immediate_descendants_only,
            "max_nodes": max_nodes,
            "timeout": timeout,
            "interval": interval,
            "postcondition": self._postcondition_payload(postcondition),
        }
        builder = _ReceiptBuilder(
            op="toggle",
            host=host,
            backend=backend,
            executor=self._default_executor(host),
            request=request,
            once=None,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )

        # Resolve -> precheck -> dispatch -> verify, serialized against
        # every other mutating call on this one `Operations`. `toggle`
        # takes no `once`, so there is no reservation step here.
        with self._dispatch_lock:
            try:
                match, element_index, app_info, target = self._resolve_target(
                    host,
                    builder,
                    app=app,
                    all_apps=all_apps,
                    apps=apps,
                    search_key=resolved_key,
                    text=text,
                    title=title,
                    identifier=identifier,
                    description=description,
                    visible_only=visible_only,
                    direction=direction,
                    immediate_descendants_only=immediate_descendants_only,
                    max_nodes=max_nodes,
                    include_actions=True,
                    deadline=deadline,
                    interval=interval,
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, error=exc.to_json(),
                    )
                )

            builder.executor = self._executor_of(host, element_index)

            if dry_run:
                return self._finish(
                    builder.build(
                        outcome=Outcome.PLANNED, acted=Acted.NO, changed=False,
                        verified=False, target=target,
                    )
                )

            try:
                before = bool(host.get(element_index, attribute))
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=exc.to_json(),
                    )
                )

            if before == desired:
                verified = self._verify_postcondition(
                    host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
                )
                outcome = Outcome.ALREADY if verified.ok else Outcome.FAILED
                return self._finish(
                    builder.build(
                        outcome=outcome, acted=Acted.NO, changed=False,
                        verified=postcondition is not None and verified.ok,
                        target=target, observed=_value_summary(before),
                        error=None if verified.ok else verified.error,
                    )
                )

            actions = match.get("actions") or ()
            if "AXPress" not in actions:
                error: ErrorPayload = {
                    "code": ErrorCode.UNSUPPORTED_OP.value,
                    "message": "Resolved AX target does not expose AXPress; "
                    f"available actions: {list(actions)}",
                    "details": {"actions": list(actions)},
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target, observed=_value_summary(before), error=error,
                    )
                )

            owner = match.get("app")
            try:
                target_pid = int(owner["pid"]) if isinstance(owner, dict) else int(app_info["pid"])
            except (TypeError, ValueError, KeyError) as exc:
                pid_error: ErrorPayload = {
                    "code": ErrorCode.AX_ERROR.value,
                    "message": f"Resolved target's owning app has a malformed pid: {exc}",
                    "details": {},
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target, observed=_value_summary(before), error=pid_error,
                    )
                )

            frontmost_before = None if deadline.exhausted() else host._frontmost_app()
            if deadline.exhausted():
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target, observed=_value_summary(before),
                        error=_deadline_exhausted_error(
                            "deadline_exhausted_before_dispatch",
                            "No time remained on the shared deadline to dispatch this toggle",
                        ),
                    )
                )

            try:
                _perform_guarded(
                    host, element_index, "AXPress", target_pid, "mac.do.toggle", frontmost_before
                )
            except FocusChangedError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.YES, changed=None, verified=False,
                        target=target, observed=_value_summary(before), error=exc.to_json(),
                    )
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.UNKNOWN, changed=None, verified=False,
                        target=target, observed=_value_summary(before), error=exc.to_json(),
                    )
                )

            try:
                after, converged = self._read_until(
                    deadline,
                    interval,
                    lambda: bool(host.get(element_index, attribute)),
                    lambda reading: reading == desired,
                )
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.YES, changed=None, verified=False,
                        target=target, observed=_value_summary(before), error=exc.to_json(),
                    )
                )
            changed = after != before
            if not converged:
                error = {
                    "code": ErrorCode.AX_ERROR.value,
                    "message": f"{attribute} did not converge to the requested state after AXPress",
                    "details": {
                        "attribute": attribute,
                        "requested": _value_summary(bool(desired)),
                        "observed": _value_summary(after),
                    },
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.YES, changed=changed, verified=False,
                        target=target, observed=_value_summary(after), error=error,
                    )
                )

            verified = self._verify_postcondition(
                host, postcondition, deadline, app=app, all_apps=all_apps, apps=apps
            )
            outcome = Outcome.DONE if verified.ok else Outcome.FAILED
            return self._finish(
                builder.build(
                    outcome=outcome, acted=Acted.YES, changed=changed,
                    verified=postcondition is not None and verified.ok,
                    target=target, observed=_value_summary(after),
                    error=None if verified.ok else verified.error,
                )
            )

    def run(
        self,
        source: str,
        *,
        language: str = "AppleScript",
        args: Sequence[str] = (),
        timeout: float = 5.0,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
        capture_output: bool = False,
    ) -> Receipt:
        """Run ``source`` through ``osascript`` -- the ``mac.do`` counterpart
        to ``mac.script`` -- with a `Receipt`, a deadline that terminates
        and reaps a runaway script and its *entire* process group (never
        just the direct child), and no shell involved at any point.

        Unlike the AX verbs, ``run`` has no scope: it accepts no
        ``app``/``all_apps``/``apps``. ``args`` become the script's own
        ``argv`` (e.g. ``on run argv`` in AppleScript), passed as separate
        ``osascript`` arguments -- never concatenated into the source.
        ``dry_run=True`` only compiles ``source`` (via ``osacompile`` into a
        throwaway file) without running it.

        ``language``/``source``/``args`` are preflight-validated --
        ``language`` against ``osascript``'s exact two supported values,
        ``source``/``args`` for size, NUL bytes, and UTF-8 round-tripping
        -- before anything else, including a ``once`` token reservation,
        so a malformed request never reserves a token it can then never
        finish. A receipt's own copy of ``source``/each ``args`` item is
        always just a ``{sha256, length}`` summary, never the text
        itself, and its ``observed`` stdout/stderr are ``{bytes, sha256,
        truncated}`` metadata only -- a script's own arguments/output can
        carry secrets just as easily as its source can, so none of that
        actual text is ever included unless a caller opts in with
        ``capture_output=True`` (which only ever affects stdout/stderr,
        never ``args``).
        """
        self._check_owner()
        host = self._host
        self._validate_postcondition(host, postcondition)
        self._validate_postcondition_inheritance(postcondition, has_scope=False)
        argv = [str(item) for item in args]
        _validate_language(language)
        _validate_run_input(source, argv)
        once = self._normalize_once(once)
        deadline = _Deadline(timeout, self._monotonic)
        backend = host._backend
        request: dict[str, JSONValue] = {
            "op": "run",
            "language": language,
            "source": _source_summary(source),
            "args": _args_summary(argv),
            "timeout": timeout,
            "capture_output": capture_output,
            "postcondition": self._postcondition_payload(postcondition),
        }
        fingerprint = request_fingerprint(request) if once is not None else None
        builder = _ReceiptBuilder(
            op="run",
            host=host,
            backend=backend,
            executor=Executor.SCRIPT,
            request=request,
            once=once,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )
        if once is not None and not dry_run:
            # Nonblocking on purpose: a concurrent duplicate of this same
            # token must be able to report "in_flight" immediately, even
            # while another thread holds `_dispatch_lock` for an
            # unrelated, still-running operation.
            action = self._ledger.inspect(once, "run", fingerprint)
            if action == "replay":
                return self._finish(self._ledger.stored(once).replayed_as())
            if action == "in_flight":
                return self._finish(self._in_flight_receipt(builder, target=None))

        # Compile-or-run -> reserve -> dispatch -> verify, serialized
        # against every other mutating call on this one `Operations`.
        with self._dispatch_lock:
            if dry_run:
                with tempfile.TemporaryDirectory(prefix="macos-harness-do-") as tmp_dir:
                    output_path = Path(tmp_dir) / "check.scpt"
                    result = _run_osascript(
                        ["/usr/bin/osacompile", "-l", language, "-o", str(output_path), "-"],
                        source,
                        deadline,
                        self._spawn,
                        capture_output=capture_output,
                        monotonic=self._monotonic,
                    )
                outcome = Outcome.PLANNED if result.ok else Outcome.FAILED
                return self._finish(
                    builder.build(
                        outcome=outcome, acted=Acted.NO, changed=False, verified=False,
                        error=result.error,
                    )
                )

            if deadline.exhausted():
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        error=_deadline_exhausted_error(
                            "deadline_exhausted_before_dispatch",
                            "No time remained on the shared deadline to dispatch this run",
                        ),
                    )
                )

            if once is not None:
                assert fingerprint is not None
                action = self._ledger.reserve(once, "run", fingerprint)
                if action == "replay":
                    return self._finish(self._ledger.stored(once).replayed_as())
                if action == "in_flight":
                    return self._finish(self._in_flight_receipt(builder, target=None))

            result = _run_osascript(
                ["/usr/bin/osascript", "-l", language, "-", *argv],
                source,
                deadline,
                self._spawn,
                capture_output=capture_output,
                monotonic=self._monotonic,
            )
            observed: JSONValue = {
                "stdout": result.stdout.payload(),
                "stderr": result.stderr.payload(),
                "returncode": result.returncode,
            }

            if not result.ok:
                receipt = builder.build(
                    outcome=Outcome.FAILED, acted=result.acted,
                    changed=False if result.acted is Acted.NO else None,
                    verified=False, observed=observed, error=result.error,
                )
            else:
                verified = self._verify_postcondition(
                    host, postcondition, deadline, app=None, all_apps=False, apps=None
                )
                if verified.observed is not None:
                    observed = {**observed, "postcondition": verified.observed}
                outcome = Outcome.DONE if verified.ok else Outcome.FAILED
                receipt = builder.build(
                    outcome=outcome, acted=Acted.YES,
                    changed=_changed_after_dispatch(postcondition, verified),
                    verified=postcondition is not None and verified.ok,
                    observed=observed, error=None if verified.ok else verified.error,
                )

            if once is not None:
                self._ledger.finalize(once, receipt)
            return self._finish(receipt)

    def key(
        self,
        key: str,
        *,
        app: str | int,
        timeout: float = 5.0,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Send one key combo to exactly ``app`` -- the ``mac.do``
        counterpart to ``mac.key``.

        Unlike the AX verbs, ``key`` has no ``all_apps``/``apps`` scope: a
        key combo always targets one exact process, so ``app`` is required.
        """

        def prepare(host: _Host, pid: int, deadline: _Deadline) -> None:
            del pid, deadline
            host._validate_key(key)

        def dispatch(host: _Host, pid: int, deadline: _Deadline) -> None:
            del deadline
            host.key(key, app=pid)

        return self._input_verb(
            op="key",
            app=app,
            request={"key": key},
            timeout=timeout,
            postcondition=postcondition,
            once=once,
            dry_run=dry_run,
            prepare=prepare,
            dispatch=dispatch,
        )

    def click(
        self,
        x: float,
        y: float,
        *,
        app: str | int,
        button: str = "left",
        clicks: int = 1,
        coordinate_space: str = "screenshot",
        timeout: float = 5.0,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Post one coordinate click to exactly ``app`` -- the ``mac.do``
        counterpart to ``mac.click``.

        The point, the window of ``app`` under it, and the button and
        click count resolve before anything is reserved or posted, so a
        stale screenshot, a moved window, a point over none of the app's
        windows, or a click count past a triple fails with ``acted=NO``,
        and the resolved screen point and ``window_id`` show up in
        ``target`` even on a dry run. The dispatch then posts to that
        very window, so the id the receipt records is the one the
        events were bound to.
        """
        resolved: tuple[str, int, tuple[float, float], _RoutedWindow] | None = None

        def prepare(host: _Host, pid: int, deadline: _Deadline) -> dict[str, JSONValue]:
            nonlocal resolved
            del deadline
            normalized_button = host._validate_button(button)
            normalized_clicks = host._validate_clicks(clicks)
            screen = host._screen_point(x, y, coordinate_space, pid=pid)
            window = host._target_window(pid, screen)
            resolved = (normalized_button, normalized_clicks, screen, window)
            return {
                "point": {"x": screen[0], "y": screen[1]},
                "window_id": window.window_id,
            }

        def dispatch(host: _Host, pid: int, deadline: _Deadline) -> None:
            del deadline
            assert resolved is not None
            normalized_button, normalized_clicks, screen, window = resolved
            host._post_click(
                pid, screen, window, button=normalized_button, clicks=normalized_clicks
            )

        return self._input_verb(
            op="click",
            app=app,
            request={
                "x": x, "y": y, "button": button, "clicks": clicks,
                "coordinate_space": coordinate_space,
            },
            timeout=timeout,
            postcondition=postcondition,
            once=once,
            dry_run=dry_run,
            prepare=prepare,
            dispatch=dispatch,
        )

    def type(
        self,
        text: str,
        *,
        app: str | int,
        timeout: float = 5.0,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Type ``text`` into exactly ``app`` -- the ``mac.do`` counterpart
        to ``mac.type``.

        The receipt never carries ``text`` itself, only its
        `_value_summary`, the same rule `set` applies to a value. The
        text is bounded and checked (`_validate_type_text`) before the
        ``once`` token is reserved.
        """

        def prepare(host: _Host, pid: int, deadline: _Deadline) -> None:
            del host, pid, deadline
            _validate_type_text(text)

        def dispatch(host: _Host, pid: int, deadline: _Deadline) -> None:
            del deadline
            host.type(text, app=pid)

        return self._input_verb(
            op="type",
            app=app,
            request={"text": _value_summary(text)},
            timeout=timeout,
            postcondition=postcondition,
            once=once,
            dry_run=dry_run,
            prepare=prepare,
            dispatch=dispatch,
        )

    def fill(
        self,
        value: str,
        *,
        app: str | int,
        role: str = "text field",
        text: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        timeout: float = 5.0,
        interval: float = 0.05,
        postcondition: Postcondition | None = None,
        once: str | None = None,
        dry_run: bool = False,
    ) -> Receipt:
        """Replace one plain text control through keyboard input, then read it back.

        Unlike set, this sends editing events even when the visible value
        already matches: an AXValue assignment can leave an app's model stale.
        It never submits. Secure fields and control characters are refused.
        Supply an app-level postcondition to verify more than the text.
        """
        self._check_owner()
        _validate_interval(interval)
        _validate_fill_value(value)
        validate_exact_selectors(title=title, identifier=identifier, description=description)
        search_key = self._host.ax._search_key(None, role)
        element_index: int | None = None

        def prepare(host: _Host, pid: int, deadline: _Deadline) -> dict[str, JSONValue]:
            nonlocal element_index
            match = host.ax_wait(
                app=pid, search_key=search_key, text=text, title=title,
                identifier=identifier, description=description,
                timeout=deadline.remaining(), interval=interval,
            )
            element_index = int(match["element_index"])
            if (
                host.get(element_index, "AXRole") not in {"AXTextField", "AXTextArea"}
                or host.get(element_index, "AXSubrole", missing_ok=True)
                not in {None, "AXUnknown", "AXSearchField"}
                or host.get(element_index, "AXEnabled") is not True
            ):
                raise MacOSError(
                    "fill requires an enabled, non-secure text control",
                    code=ErrorCode.UNSUPPORTED_OP,
                )
            return {"element_index": element_index, "match": self._match_summary_payload(match)}

        def require(
            deadline: _Deadline, read: Callable[[], object], expected: object, stage: str,
        ) -> None:
            _, ok = self._read_until(deadline, interval, read, lambda result: result == expected)
            if not ok or deadline.exhausted():
                raise MacOSError(
                    f"fill could not confirm {stage} within its deadline",
                    code=ErrorCode.TIMEOUT, details={"stage": stage},
                )

        def dispatch(host: _Host, pid: int, deadline: _Deadline) -> dict[str, JSONValue]:
            assert element_index is not None
            frontmost = host._frontmost_app()
            if deadline.exhausted():
                raise MacOSError("fill deadline expired before focus", code=ErrorCode.TIMEOUT)
            host.set(element_index, True, "AXFocused")
            host._guard_focus(frontmost, pid, "fill")
            require(deadline, lambda: host.get(element_index, "AXFocused"), True, "field focus")
            before = host.get(element_index)
            if not isinstance(before, str):
                raise MacOSError("fill requires a string AXValue", code=ErrorCode.UNSUPPORTED_OP)
            selection = {"location": 0, "length": len(before.encode("utf-16-le")) // 2}
            if deadline.exhausted():
                raise MacOSError("fill deadline expired before selection", code=ErrorCode.TIMEOUT)
            host.set(element_index, selection, "AXSelectedTextRange")
            host._guard_focus(frontmost, pid, "fill")
            require(
                deadline, lambda: host.get(element_index, "AXSelectedTextRange"),
                selection, "full selection",
            )
            if host.get(element_index, "AXFocused") is not True or host.get(element_index) != before:
                raise FocusChangedError("fill target changed before typing")
            if deadline.exhausted():
                raise MacOSError("fill deadline expired before typing", code=ErrorCode.TIMEOUT)
            if value:
                host.type(value, app=pid)
            else:
                host.key("backspace", app=pid)
            require(deadline, lambda: host.get(element_index), value, "text readback")
            return {"before": _value_summary(before), "after": _value_summary(value)}

        return self._input_verb(
            op="fill", app=app,
            request={
                "value": _value_summary(value), "search_key": search_key, "text": text,
                "title": title, "identifier": identifier, "description": description,
                "interval": interval,
            },
            timeout=timeout, postcondition=postcondition, once=once, dry_run=dry_run,
            prepare=prepare, dispatch=dispatch,
        )

    def _input_verb(
        self,
        *,
        op: str,
        app: str | int,
        request: dict[str, JSONValue],
        timeout: float,
        postcondition: Postcondition | None,
        once: str | None,
        dry_run: bool,
        prepare: Callable[[_Host, int, _Deadline], JSONValue],
        dispatch: Callable[[_Host, int, _Deadline], dict[str, JSONValue] | None],
    ) -> Receipt:
        """The pipeline every raw-input verb (``key``, ``click``, ``type``)
        shares: one exact ``app``, no AX target to resolve, an `Executor.INPUT`
        receipt, and ``acted`` judged from whether the posted events raised.

        ``prepare`` runs once the app is resolved and before anything is
        reserved or dispatched: it validates what is about to be sent and
        may return extra ``target`` fields (a click's resolved screen
        point), so a dry run reports them too. ``dispatch`` posts the
        input to ``pid``.
        """
        self._check_owner()
        host = self._host
        _validate_app_selector(app, parameter="app")
        self._validate_postcondition(host, postcondition)
        self._validate_postcondition_inheritance(postcondition, has_scope=True)
        once = self._normalize_once(once)
        deadline = _Deadline(timeout, self._monotonic)
        backend = host._backend
        request = {
            "op": op,
            "app": app,
            **request,
            "timeout": timeout,
            "postcondition": self._postcondition_payload(postcondition),
        }
        fingerprint = request_fingerprint(request) if once is not None else None
        builder = _ReceiptBuilder(
            op=op,
            host=host,
            backend=backend,
            executor=Executor.INPUT,
            request=request,
            once=once,
            deadline=deadline,
            wall_clock=self._wall_clock,
        )
        if once is not None and not dry_run:
            # Nonblocking on purpose -- see the matching comment in `press`.
            action = self._ledger.inspect(once, op, fingerprint)
            if action == "replay":
                return self._finish(self._ledger.stored(once).replayed_as())
            if action == "in_flight":
                return self._finish(self._in_flight_receipt(builder, target=None))

        # Resolve -> precheck -> reserve -> dispatch -> verify, serialized
        # against every other mutating call on this one `Operations`.
        with self._dispatch_lock:
            try:
                host._ensure_accessibility()
                host._ensure_post_events()
                _, info = host._resolve_app(app)
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, error=exc.to_json(),
                    )
                )

            target: dict[str, JSONValue] = {"app": info}

            try:
                pid = int(info["pid"])
            except (TypeError, ValueError, KeyError) as exc:
                pid_error: ErrorPayload = {
                    "code": ErrorCode.AX_ERROR.value,
                    "message": f"Resolved app has a malformed pid: {exc}",
                    "details": {},
                }
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=pid_error,
                    )
                )

            builder.bind_app(info)
            try:
                extras = prepare(host, pid, deadline)
            except MacOSError as exc:
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False,
                        verified=False, target=target, error=exc.to_json(),
                    )
                )
            if isinstance(extras, Mapping):
                target.update(extras)

            if dry_run:
                return self._finish(
                    builder.build(
                        outcome=Outcome.PLANNED, acted=Acted.NO, changed=False,
                        verified=False, target=target,
                    )
                )

            # The focus reading before dispatch is a witness, but it is
            # AX work against the app, so it counts against the budget:
            # it runs only while time remains and the deadline is checked
            # again after it, before the token is reserved, so a slow
            # sample never turns into a late mutation or a spent token.
            before = None if deadline.exhausted() else _sample_focus(host, pid)
            if deadline.exhausted():
                return self._finish(
                    builder.build(
                        outcome=Outcome.FAILED, acted=Acted.NO, changed=False, verified=False,
                        target=target,
                        error=_deadline_exhausted_error(
                            "deadline_exhausted_before_dispatch",
                            f"No time remained on the shared deadline to dispatch this {op}",
                        ),
                    )
                )

            if once is not None:
                assert fingerprint is not None
                action = self._ledger.reserve(once, op, fingerprint)
                if action == "replay":
                    return self._finish(self._ledger.stored(once).replayed_as())
                if action == "in_flight":
                    return self._finish(self._in_flight_receipt(builder, target=target))

            try:
                input_observed = dispatch(host, pid, deadline)
            except FocusChangedError as exc:
                receipt = builder.build(
                    outcome=Outcome.FAILED, acted=Acted.YES, changed=None, verified=False,
                    target=target, error=exc.to_json(),
                )
            except MacOSError as exc:
                receipt = builder.build(
                    outcome=Outcome.FAILED, acted=Acted.UNKNOWN, changed=None, verified=False,
                    target=target, error=exc.to_json(),
                )
            else:
                focus = self._focus_effect(
                    host, pid, before, deadline,
                    poll=postcondition is None and input_observed is None
                )
                verified = self._verify_postcondition(
                    host, postcondition, deadline, app=app, all_apps=False, apps=None
                )
                outcome = Outcome.DONE if verified.ok else Outcome.FAILED
                changed = _changed_after_dispatch(postcondition, verified)
                if changed is None and focus["changed"]:
                    changed = True
                observed = {"focus": focus, "postcondition": verified.observed}
                if input_observed is not None:
                    observed["input"] = input_observed
                receipt = builder.build(
                    outcome=outcome, acted=Acted.YES, changed=changed,
                    verified=(postcondition is not None or input_observed is not None) and verified.ok,
                    target=target,
                    observed=observed,
                    error=None if verified.ok else verified.error,
                )

            if once is not None:
                self._ledger.finalize(once, receipt)
            return self._finish(receipt)

    def _focus_effect(
        self,
        host: _Host,
        pid: int,
        before: Mapping[str, JSONValue] | None,
        deadline: _Deadline,
        *,
        poll: bool,
    ) -> dict[str, JSONValue]:
        """Read focus again after a raw input landed and report what moved.

        Always takes one reading straight away. With ``poll`` it then
        re-reads every `_FOCUS_POLL_SECONDS` for up to `_FOCUS_POLLS`
        readings, stopping at the first that differs from ``before``, so
        the app's run loop gets up to 100ms to process the event before
        the receipt calls the effect unobserved; no sleep runs past
        ``deadline``. Either reading failing leaves ``changed`` empty:
        no witness, not a witness of nothing.
        """
        after = _sample_focus(host, pid)
        if poll and before is not None:
            for _ in range(_FOCUS_POLLS):
                if after is not None and after != before:
                    break
                if not self._sleep_within(deadline, _FOCUS_POLL_SECONDS):
                    break
                after = _sample_focus(host, pid)
        changed = [] if before is None or after is None else _changed_focus_fields(before, after)
        return {
            "before": None if before is None else _redact_focus(before),
            "after": None if after is None else _redact_focus(after),
            "changed": changed,
        }

    def recall(self, once: str) -> Receipt:
        """Look up the at-most-once ledger for ``once`` without
        dispatching anything: returns the finished receipt (replayed)
        for a completed ``press``/``run``/``key``/``click``/``type``.

        Raises `OperationError`, carrying a failed, ``acted=UNKNOWN``
        receipt, if the token's reservation is still in progress or was
        left unfinished; raises `MacOSError` with ``bad_request`` if
        ``once`` was never used at all.
        """
        self._check_owner()
        host = self._host
        token = self._normalize_once(once)
        if token is None:
            raise MacOSError("recall requires a nonempty once token", code=ErrorCode.BAD_REQUEST)
        status, op, receipt = self._ledger.peek(token)
        if status == "unknown":
            raise MacOSError(
                f"Unknown once token {token!r}", code=ErrorCode.BAD_REQUEST, details={"once": token}
            )
        if status == "in_flight":
            assert op is not None
            lookup_at = _utc_timestamp(self._wall_clock())
            return self._finish(
                Receipt(
                    op="recall",
                    outcome=Outcome.FAILED,
                    acted=Acted.UNKNOWN,
                    backend=host._backend,
                    executor=Executor.PYTHON,
                    request={"once": token},
                    changed=False,
                    verified=False,
                    duration_s=0.0,
                    once=token,
                    started_at=lookup_at,
                    finished_at=lookup_at,
                    error={
                        "code": ErrorCode.BAD_REQUEST.value,
                        "message": f"once token {token!r} (reserved for {op!r}) is still in "
                        "progress or was left unfinished",
                        "details": {"once": token, "op": op},
                    },
                )
            )
        assert receipt is not None
        return self._finish(receipt.replayed_as())

    # --- shared validation -------------------------------------------------

    @staticmethod
    def _require_scope(
        *, app: str | int | None, all_apps: bool, apps: str | int | Iterable[str | int] | None
    ) -> None:
        provided = sum((app is not None, bool(all_apps), apps is not None))
        if provided != 1:
            raise MacOSError(
                "Pass exactly one of app, all_apps=True, or apps", code=ErrorCode.BAD_REQUEST
            )
        _validate_app_selector(app, parameter="app")

    @staticmethod
    def _normalize_once(once: str | None) -> str | None:
        if once is None:
            return None
        if not once:
            raise MacOSError("once must be a nonempty token", code=ErrorCode.BAD_REQUEST)
        return once

    @staticmethod
    def _freeze_apps(
        apps: str | int | Iterable[str | int] | None,
    ) -> str | int | tuple[str | int, ...] | None:
        if apps is None or isinstance(apps, (str, int)):
            return apps
        return tuple(apps)

    @staticmethod
    def _validate_postcondition(host: _Host, postcondition: Postcondition | None) -> None:
        """Validate ``postcondition``'s runtime type and its ``role``/
        ``search_key`` pair before any verb ever dispatches its mutating
        call or reserves a ``once`` token.

        `_verify_postcondition` re-derives ``search_key`` from these same
        two fields, but only *after* the mutating call has already
        dispatched. Left unchecked until then, a malformed postcondition
        (the wrong type entirely, or an unknown ``role``/both ``role``
        and ``search_key`` set) would still cause a real effect -- a
        press, a set, a launched script -- before ever failing, and
        would leave any ``once`` token reserved with no receipt to
        finalize it. Checked here instead, a bad postcondition always
        fails as a clean `BAD_REQUEST` with zero effects, and its token
        stays free to reuse.
        """
        if postcondition is None:
            return
        if not isinstance(postcondition, Postcondition):
            raise MacOSError(
                f"postcondition must be a Present, Gone, or Equals, not {type(postcondition).__name__}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "postcondition"},
            )
        host.ax._search_key(postcondition.search_key, postcondition.role)

    @staticmethod
    def _validate_postcondition_inheritance(postcondition: Postcondition | None, *, has_scope: bool) -> None:
        if postcondition is None or has_scope:
            return
        explicit = postcondition.app is not None or postcondition.all_apps or postcondition.apps is not None
        if not explicit:
            raise MacOSError(
                "This operation has no scope for the postcondition to inherit; pass "
                "app=, all_apps=True, or apps= on the postcondition itself",
                code=ErrorCode.BAD_REQUEST,
            )

    def _validate_outcomes(self, host: _Host, outcomes: Mapping[str, Postcondition]) -> None:
        """Check every one of `expect_any`'s labelled conditions before
        the wait starts, for the reason `_validate_postcondition` states:
        a malformed condition should cost nothing and reach no receipt.

        A condition's own ``timeout`` is refused rather than ignored.
        `expect_any` spends one shared budget across repeated passes over
        every outcome, so a per-condition window has nowhere to apply --
        honoring it would mean letting one outcome hold the pass, which
        is the starvation this verb exists to avoid -- and silently
        dropping it would leave a caller believing a bound that never
        existed.
        """
        if not isinstance(outcomes, Mapping):
            raise MacOSError(
                "expect_any requires a mapping of label to Present, Gone, or Equals "
                f"condition, not {type(outcomes).__name__}",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "outcomes"},
            )
        if not outcomes:
            raise MacOSError(
                "expect_any requires at least one labelled outcome",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "outcomes"},
            )
        for label, condition in outcomes.items():
            if not isinstance(label, str) or not label.strip():
                raise MacOSError(
                    f"outcome labels must be non-blank strings, not {label!r}",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "outcomes"},
                )
            if not isinstance(condition, Postcondition):
                raise MacOSError(
                    f"outcome {label!r} must be a Present, Gone, or Equals, not "
                    f"{type(condition).__name__}",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "outcomes", "label": label},
                )
            self._validate_postcondition(host, condition)
            self._validate_postcondition_inheritance(condition, has_scope=False)
            if condition.timeout is not None:
                raise MacOSError(
                    f"outcome {label!r} sets its own timeout; expect_any watches every "
                    "outcome on one shared timeout, so pass it to expect_any instead",
                    code=ErrorCode.BAD_REQUEST,
                    details={"parameter": "outcomes", "label": label, "timeout": condition.timeout},
                )

    @staticmethod
    def _scope_payload(
        app: str | int | None, all_apps: bool, apps: str | int | Iterable[str | int] | None
    ) -> dict[str, JSONValue]:
        if apps is None:
            normalized_apps = None
        else:
            normalized_apps = [apps] if isinstance(apps, (str, int)) else list(apps)
        return {"app": app, "all_apps": bool(all_apps), "apps": normalized_apps}

    def _postcondition_payload(self, postcondition: Postcondition | None) -> JSONValue:
        if postcondition is None:
            return None
        payload: dict[str, JSONValue] = {
            "kind": type(postcondition).__name__.lower(),
            "text": postcondition.text,
            "role": postcondition.role,
            "search_key": postcondition.search_key,
            "title": postcondition.title,
            "identifier": postcondition.identifier,
            "description": postcondition.description,
            "scope": self._scope_payload(postcondition.app, postcondition.all_apps, postcondition.apps),
            "visible_only": postcondition.visible_only,
            "direction": postcondition.direction,
            "immediate_descendants_only": postcondition.immediate_descendants_only,
            "timeout": postcondition.timeout,
            "interval": postcondition.interval,
        }
        if isinstance(postcondition, Equals):
            payload["attribute"] = postcondition.attribute
            payload["value"] = _value_summary(postcondition.value)
        return payload

    # --- AX target resolution ------------------------------------------------

    def _resolve_target(
        self,
        host: _Host,
        builder: _ReceiptBuilder,
        *,
        app: str | int | None,
        all_apps: bool,
        apps: str | int | Iterable[str | int] | None,
        search_key: str,
        text: str | None,
        title: str | None,
        identifier: str | None,
        description: str | None,
        visible_only: bool,
        direction: str,
        immediate_descendants_only: bool,
        max_nodes: int,
        include_actions: bool,
        deadline: _Deadline,
        interval: float,
    ) -> tuple[dict[str, JSONValue], int, Mapping[str, JSONValue], JSONValue]:
        """Resolve exactly one AX match for a scoped operation.

        Raises whatever ``MacOS.ax_wait`` raises (not found, ambiguous, no
        permission, ...) -- always before any ledger reservation, so a
        resolution failure is always retry-free.
        """
        if app is not None:
            _, info = host._resolve_app(app)
            builder.bind_app(info)
            app = info["pid"]
        match = host.ax_wait(
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
            include_actions=include_actions,
            max_nodes=max_nodes,
            timeout=deadline.remaining(),
            interval=interval,
        )
        element_index = int(match["element_index"])
        owner = match.get("app")
        if isinstance(owner, dict):
            app_info: Mapping[str, JSONValue] = owner
        else:
            _, app_info = host._resolve_app(app)
        if builder.app_info is None:
            builder.bind_app(app_info)
        target = self._match_target(match, app_info)
        return match, element_index, app_info, target

    @staticmethod
    def _match_target(
        match: Mapping[str, JSONValue],
        app_info: Mapping[str, JSONValue],
    ) -> JSONValue:
        return {
            "app": dict(app_info),
            "role": match.get("role"),
            "title": match.get("title"),
            "description": match.get("description"),
            "identifier": match.get("identifier"),
        }

    @staticmethod
    def _executor_of(host: _Host, element_index: int) -> Executor:
        element = host._element(element_index)
        return Executor.PYTHON if host._is_ax_element(element) else Executor.NATIVE

    @staticmethod
    def _default_executor(host: _Host) -> Executor:
        """Use the resolved client for automatic backend selection."""
        native = host._backend == "native" or (
            host._backend == "auto" and host._native_client is not None
        )
        return Executor.NATIVE if native else Executor.PYTHON

    # --- postconditions --------------------------------------------------

    def _verify_postcondition(
        self,
        host: _Host,
        postcondition: Postcondition | None,
        deadline: _Deadline,
        *,
        app: str | int | None,
        all_apps: bool,
        apps: str | int | Iterable[str | int] | None,
        enhance: bool = True,
        budget: float | None = None,
    ) -> _Verification:
        if postcondition is None:
            return _Verification(state=Observation.MET, observed=None, error=None)
        explicit = postcondition.app is not None or postcondition.all_apps or postcondition.apps is not None
        scope_app = postcondition.app if explicit else app
        scope_all_apps = postcondition.all_apps if explicit else all_apps
        scope_apps = postcondition.apps if explicit else apps
        remaining = deadline.remaining()
        effective_timeout = remaining if postcondition.timeout is None else min(postcondition.timeout, remaining)
        if budget is not None:
            # `expect_any` spends the shared deadline one pass at a time:
            # ``budget`` caps this single check's window (at zero, one
            # poll) so no outcome can swallow a budget the others are
            # waiting on. ``deadline`` still bounds everything above it.
            effective_timeout = min(effective_timeout, budget)
        search_key = host.ax._search_key(postcondition.search_key, postcondition.role)
        try:
            if isinstance(postcondition, Present):
                match = host.ax_wait(
                    app=scope_app,
                    all_apps=scope_all_apps,
                    apps=scope_apps,
                    search_key=search_key,
                    text=postcondition.text,
                    title=postcondition.title,
                    identifier=postcondition.identifier,
                    description=postcondition.description,
                    visible_only=postcondition.visible_only,
                    direction=postcondition.direction,
                    immediate_descendants_only=postcondition.immediate_descendants_only,
                    timeout=effective_timeout,
                    interval=postcondition.interval,
                    enhance=enhance,
                )
                return _Verification(
                    state=Observation.MET, observed=self._match_summary_payload(match), error=None
                )
            if isinstance(postcondition, Equals):
                return self._verify_equals(
                    host,
                    postcondition,
                    effective_timeout,
                    search_key=search_key,
                    app=scope_app,
                    all_apps=scope_all_apps,
                    apps=scope_apps,
                    enhance=enhance,
                )
            # `postcondition` is a `Gone`: `_validate_postcondition` has
            # already guaranteed it is a `Present`, `Gone`, or `Equals`
            # before this method is ever reached, so this is the only
            # case left.
            if remaining <= 0:
                # `Gone` needs two *consecutive* empty polls to confirm
                # absence (see its own docstring); a shared deadline that
                # has run out between dispatch and verification can never
                # provide that second poll, so this fails deterministically
                # instead of handing `ax_wait_gone` a timeout it cannot
                # honor. The guard reads ``remaining`` rather than
                # ``effective_timeout`` only to leave a zero ``budget``
                # its one poll: a `Gone`'s own ``timeout`` can never be
                # zero (it refuses that at construction), so for every
                # caller that passes no ``budget`` the two spellings mean
                # exactly the same thing. `expect_any`, which does pass
                # one, counts the confirming pair itself, pass by pass.
                return _Verification(
                    state=Observation.UNOBSERVABLE,
                    reason="deadline_exhausted",
                    observed=None,
                    error=_deadline_exhausted_error(
                        "deadline_exhausted_before_verification",
                        "No time remained on the shared deadline to verify the Gone "
                        "postcondition, which needs two consecutive empty polls",
                    ),
                )
            host.ax_wait_gone(
                app=scope_app,
                all_apps=scope_all_apps,
                apps=scope_apps,
                search_key=search_key,
                text=postcondition.text,
                title=postcondition.title,
                identifier=postcondition.identifier,
                description=postcondition.description,
                visible_only=postcondition.visible_only,
                direction=postcondition.direction,
                immediate_descendants_only=postcondition.immediate_descendants_only,
                timeout=effective_timeout,
                interval=postcondition.interval,
                enhance=enhance,
            )
            return _Verification(state=Observation.MET, observed=None, error=None)
        except MacOSError as exc:
            state, reason = _observation_of(postcondition, exc)
            return _Verification(state=state, observed=None, error=exc.to_json(), reason=reason)

    @staticmethod
    def _condition_app(condition: Postcondition) -> str | int | None:
        """The single app a condition is scoped to, or ``None`` when it
        names several or all of them.

        A one-app scope is worth resolving up front: it binds the
        receipt to that app, and it is the only case where the app being
        missing outright is itself an answer (a `Gone` condition scoped
        to an app that is not running has nothing left to be present
        in).
        """
        if condition.app is not None:
            return condition.app
        if isinstance(condition.apps, (str, int)):
            return condition.apps
        if isinstance(condition.apps, tuple) and len(condition.apps) == 1:
            return condition.apps[0]
        return None

    def _observe_outcome(
        self,
        host: _Host,
        condition: Postcondition,
        deadline: _Deadline,
        empty_polls: int,
    ) -> tuple[_Verification, int]:
        """Look once at one of `expect_any`'s conditions and report what
        that single poll established, plus the running count of empty
        `Gone` polls behind it.

        The app is resolved on every look rather than once: across a
        multi-second wait an app can quit or be relaunched, and a pid
        pinned on the first pass would keep answering for a process that
        is gone. A missing app satisfies a `Gone` immediately, the same
        way `expect` reads it.

        `Gone` is the one condition a single poll cannot settle -- it
        takes two consecutive empty ones to tell absence from a walk
        that simply has not reached the element yet -- so the streak is
        carried here across passes. An empty poll with time left comes
        back as ``absence_unconfirmed``; the second consecutive one is
        the confirmation, and anything else resets the count to zero.
        """
        try:
            app = self._condition_app(condition)
            if app is not None:
                try:
                    _, info = host._resolve_app(app)
                except ApplicationNotFoundError:
                    if not isinstance(condition, Gone):
                        raise
                    return _Verification(state=Observation.MET, observed=None, error=None), 0
                condition = dataclasses.replace(condition, app=info["pid"], apps=None)
            verification = self._verify_postcondition(
                host, condition, deadline,
                app=None, all_apps=False, apps=None, enhance=False, budget=0.0,
            )
        except MacOSError as exc:
            state, reason = _observation_of(condition, exc)
            return _Verification(
                state=state, observed=None, error=exc.to_json(), reason=reason
            ), 0
        if not isinstance(condition, Gone) or verification.ok:
            return verification, 0
        if verification.reason != "absence_unconfirmed":
            return verification, 0
        empty_polls += 1
        if empty_polls >= 2:
            return _Verification(state=Observation.MET, observed=None, error=None), empty_polls
        return verification, empty_polls

    def _verify_equals(
        self,
        host: _Host,
        postcondition: Equals,
        timeout: float,
        *,
        search_key: str,
        app: str | int | None,
        all_apps: bool,
        apps: str | int | Iterable[str | int] | None,
        enhance: bool = True,
    ) -> _Verification:
        """Resolve the one match and read its attribute every
        ``postcondition.interval`` until the reading equals the expected
        value or ``timeout`` runs out.

        Each poll resolves the match again rather than holding the first
        element: an app that rebuilds a control after the action would
        otherwise leave a dead reference, and the one-match rule already
        refuses to certify a look-alike. A read the app refuses, and a
        deadline that runs out first, both raise out to the caller's
        ``except`` -- the same bounded failure a `Present` search gets.
        """
        attribute = postcondition.attribute
        deadline = _Deadline(timeout, self._monotonic)

        def read() -> tuple[Mapping[str, JSONValue], object]:
            match = host.ax_wait(
                app=app,
                all_apps=all_apps,
                apps=apps,
                search_key=search_key,
                text=postcondition.text,
                title=postcondition.title,
                identifier=postcondition.identifier,
                description=postcondition.description,
                visible_only=postcondition.visible_only,
                direction=postcondition.direction,
                immediate_descendants_only=postcondition.immediate_descendants_only,
                timeout=deadline.remaining(),
                interval=postcondition.interval,
                enhance=enhance,
            )
            return match, host.get(int(match["element_index"]), attribute)

        (match, observed), converged = self._read_until(
            deadline,
            postcondition.interval,
            read,
            lambda reading: canonicalize(reading[1]) == postcondition.value,
        )
        if not converged:
            raise MacOSError(
                f"{attribute} did not equal the expected value before the deadline",
                code=ErrorCode.TIMEOUT,
                details={
                    "attribute": attribute,
                    "expected": _value_summary(postcondition.value),
                    "observed": _value_summary(observed),
                },
            )
        return _Verification(
            state=Observation.MET,
            observed={
                **self._match_summary_payload(match),
                "attribute": attribute,
                "value": _value_summary(observed),
            },
            error=None,
        )

    def _read_until(
        self,
        deadline: _Deadline,
        interval: float,
        read: Callable[[], _Reading],
        converged: Callable[[_Reading], bool],
    ) -> tuple[_Reading, bool]:
        """Call ``read`` every ``interval`` until ``converged`` accepts a
        reading or ``deadline`` runs out, never sleeping past either; the
        last reading comes back with whether it converged. A refused read
        raises straight through."""
        while True:
            reading = read()
            if converged(reading):
                return reading, True
            if not self._sleep_within(deadline, interval):
                return reading, False

    def _sleep_within(self, deadline: _Deadline, interval: float) -> bool:
        """Sleep ``interval`` clipped to what is left of ``deadline``;
        `False`, without sleeping, once nothing is left."""
        remaining = deadline.remaining()
        if remaining <= 0:
            return False
        self._sleep(min(interval, remaining))
        return True

    @staticmethod
    def _match_summary_payload(match: Mapping[str, JSONValue]) -> JSONValue:
        return {
            "role": match.get("role"),
            "title": match.get("title"),
            "description": match.get("description"),
        }

    # --- at-most-once ledger plumbing ----------------------------------


    @staticmethod
    def _in_flight_receipt(builder: _ReceiptBuilder, *, target: JSONValue) -> Receipt:
        once = builder.once
        assert once is not None
        return builder.build(
            outcome=Outcome.FAILED,
            acted=Acted.UNKNOWN,
            changed=False,
            verified=False,
            target=target,
            duration_s=0.0,
            error={
                "code": ErrorCode.BAD_REQUEST.value,
                "message": f"once token {once!r} has a reservation still in progress or left "
                "unfinished; use a new token to retry",
                "details": {"once": once},
            },
        )

    def _finish(self, receipt: Receipt) -> Receipt:
        self._ledger.remember(receipt)
        if receipt.outcome is Outcome.FAILED:
            raise OperationError.from_receipt(receipt)
        return receipt
