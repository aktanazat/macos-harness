"""One app's input at a time across this user's macos-harness processes,
and one paste at a time on the general pasteboard.

Keys go to whichever window an app has focused, so two processes typing
into two windows of one app mixed their text: each made its own window
the focused one and the other's keys followed it there. Holding the app's
lock while one call focuses a window and posts its input keeps them apart.

A paste borrows the clipboard from saving it to putting it back, so a
second paste in between saved the first one's payload as the user's
clipboard and put that back: the pasteboard's turn spans the whole loan.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress

from ._paths import user_temp_dir
from .errors import ErrorCode, MacOSError


class _Hold:
    """This process's hold on one app's lock: the thread lock that keeps
    its own threads apart, how deeply the holding thread has taken it,
    and the open file whose flock keeps other processes out."""

    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


_holds_guard = threading.Lock()
_holds: dict[str, _Hold] = {}


def _forget_holds_after_fork() -> None:
    """Start a forked child with no holds. Its copy of a held lock file
    would keep the app's input locked after the parent let go, and a
    hold's thread lock may belong to a thread the child does not have.
    Closing the copy, never unlocking it, leaves the parent's hold as it
    was, and a hold the forking thread is inside then ends without
    closing it again; a file whose holder was closing it as the child
    forked may be closed already."""
    global _holds, _holds_guard
    inherited, _holds, _holds_guard = _holds, {}, threading.Lock()
    for hold in inherited.values():
        fd, hold.fd = hold.fd, None
        if fd is not None:
            with suppress(OSError):
                os.close(fd)


os.register_at_fork(after_in_child=_forget_holds_after_fork)


@contextmanager
def app_input(pid: int, wait: float) -> Iterator[None]:
    """Hold ``pid``'s input lock, waiting at most ``wait`` seconds while
    another call has it; a thread already holding it takes it again."""
    with _turn(f"input-{pid}", wait, lambda: _busy(pid, wait), lambda error: _unavailable(pid, error)):
        yield


@contextmanager
def pasteboard_turn(wait: float) -> Iterator[None]:
    """Hold the general pasteboard for one paste, from saving what it
    holds to putting it back, waiting at most ``wait`` seconds while
    another call has it."""

    def busy() -> MacOSError:
        return MacOSError(
            f"Another macos-harness paste held the clipboard for {wait:g}s; a paste "
            "waits its turn so it never saves another paste's text as the user's "
            "clipboard, and nothing was pasted. Try again once that paste is done",
            code=ErrorCode.TIMEOUT,
            details={"waited": wait, "reason": "pasteboard_busy"},
        )

    def unavailable(error: OSError) -> MacOSError:
        return MacOSError(
            f"Could not open the lock that keeps pastes apart ({error}), so nothing "
            "was pasted",
            code=ErrorCode.AX_ERROR,
            details={"reason": "pasteboard_lock_unavailable", "error": str(error)},
        )

    with _turn("pasteboard", wait, busy, unavailable):
        yield


@contextmanager
def _turn(
    name: str,
    wait: float,
    busy: Callable[[], MacOSError],
    unavailable: Callable[[OSError], MacOSError],
) -> Iterator[None]:
    wait = max(wait, 0.0)
    end = time.monotonic() + wait
    with _holds_guard:
        hold = _holds.setdefault(name, _Hold())
    if not hold.thread_lock.acquire(timeout=wait):
        raise busy()
    try:
        if hold.depth == 0:
            hold.fd = _lock_file(name, end, busy, unavailable)
        hold.depth += 1
        try:
            yield
        finally:
            hold.depth -= 1
            if hold.depth == 0 and hold.fd is not None:
                os.close(hold.fd)
                hold.fd = None
    finally:
        hold.thread_lock.release()


def _lock_file(
    name: str,
    end: float,
    busy: Callable[[], MacOSError],
    unavailable: Callable[[OSError], MacOSError],
) -> int:
    try:
        fd = os.open(
            os.path.join(user_temp_dir(), f"macos-harness-{name}.lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as error:
        raise unavailable(error) from error
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise busy() from None
                time.sleep(0.01)
            else:
                return fd
    except BaseException:
        os.close(fd)
        raise


def _unavailable(pid: int, error: OSError) -> MacOSError:
    return MacOSError(
        f"Could not open the lock that keeps input to pid {pid} from "
        f"mixing with another call's ({error}), so nothing was sent",
        code=ErrorCode.AX_ERROR,
        details={"pid": pid, "reason": "app_input_lock_unavailable", "error": str(error)},
    )


def _busy(pid: int, wait: float) -> MacOSError:
    return MacOSError(
        f"Another macos-harness call kept sending input to pid {pid} for "
        f"{wait:g}s; input to one app waits its turn so the two cannot mix, "
        "and nothing was sent. Try again once that call is done",
        code=ErrorCode.TIMEOUT,
        details={"pid": pid, "waited": wait, "reason": "app_input_busy"},
    )
