"""One app's input at a time across this user's macos-harness processes.

Keys go to whichever window an app has focused, so two processes typing
into two windows of one app mixed their text: each made its own window
the focused one and the other's keys followed it there. Holding the app's
lock while one call focuses a window and posts its input keeps them apart.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
from collections.abc import Iterator
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
_holds: dict[int, _Hold] = {}


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
    wait = max(wait, 0.0)
    end = time.monotonic() + wait
    with _holds_guard:
        hold = _holds.setdefault(pid, _Hold())
    if not hold.thread_lock.acquire(timeout=wait):
        raise _busy(pid, wait)
    try:
        if hold.depth == 0:
            hold.fd = _lock_file(pid, wait, end)
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


def _lock_file(pid: int, wait: float, end: float) -> int:
    fd = os.open(
        os.path.join(user_temp_dir(), f"macos-harness-input-{pid}.lock"),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise _busy(pid, wait) from None
                time.sleep(0.01)
            else:
                return fd
    except BaseException:
        os.close(fd)
        raise


def _busy(pid: int, wait: float) -> MacOSError:
    return MacOSError(
        f"Another macos-harness call kept sending input to pid {pid} for "
        f"{wait:g}s; input to one app waits its turn so the two cannot mix, "
        "and nothing was sent. Try again once that call is done",
        code=ErrorCode.TIMEOUT,
        details={"pid": pid, "waited": wait, "reason": "app_input_busy"},
    )
