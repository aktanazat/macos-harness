"""A spare: a fresh interpreter, loaded ahead of time, that runs exactly
one step for the shell that started it and then exits.

``macos-harness spare`` runs as the shell's coprocess. The shell holds the
other ends of its two pipes, so only the commands that shell runs inherit
them; ``MACOS_HARNESS_SPARE`` names their numbers. A step started there
hands its program to the loaded spare instead of loading everything
itself, and runs in its own process as before whenever no spare is ready.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import select
import signal
import site
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from typing import NoReturn, TypedDict

ENV = "MACOS_HARNESS_SPARE"

_MAGIC = b"\x00MHSPR\x01\xfe"
_KIND = slice(len(_MAGIC), len(_MAGIC) + 1)
_LENGTH = struct.Struct(">I")
_HEAD = len(_MAGIC) + 1 + _LENGTH.size
_LARGEST = 1 << 30
_CHUNK = 1 << 16
_ANSWER_SECONDS = 0.5
_LOCK_SPAN = 1 << 62
_STALE = 3
_PACKAGE = os.path.dirname(os.path.abspath(__file__))
_PRELOAD = (
    "macos_harness.cli",
    "macos_harness.macos",
    "macos_harness.capture",
    "macos_harness.controls",
    "macos_harness.ops",
)
_STARTUP_PREFIXES = ("PYTHON", "PYOBJC", "LC_", "__CF")
_STARTUP_NAMES = frozenset({"HOME", "LANG", "TMPDIR", "TZ"})
_BROKER = "from macos_harness.spare import serve_shell; raise SystemExit(serve_shell())"
_SPARE = "import sys; from macos_harness.spare import serve_one_step; serve_one_step(sys.argv[1:])"


def _frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _MAGIC + kind + _LENGTH.pack(len(payload)) + payload


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class _Frames:
    """The frames arriving on one pipe. Bytes before a frame's start, such
    as the rest of a frame a killed reader left half read, are skipped."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._buffer = bytearray()

    def fill(self) -> bool:
        data = os.read(self.fd, _CHUNK)
        self._buffer += data
        return bool(data)

    def pop(self) -> tuple[bytes, bytes] | None:
        while (start := self._buffer.find(_MAGIC)) >= 0:
            del self._buffer[:start]
            if len(self._buffer) < _HEAD:
                return None
            (length,) = _LENGTH.unpack_from(self._buffer, _KIND.stop)
            if length > _LARGEST:
                del self._buffer[: len(_MAGIC)]
                continue
            if len(self._buffer) < _HEAD + length:
                return None
            frame = bytes(self._buffer[_KIND]), bytes(self._buffer[_HEAD : _HEAD + length])
            del self._buffer[: _HEAD + length]
            return frame
        del self._buffer[: max(0, len(self._buffer) - len(_MAGIC) + 1)]
        return None

    def next(self, deadline: float | None = None) -> tuple[bytes, bytes] | None:
        """The next frame; None at end of input or once ``deadline`` passes."""
        while (frame := self.pop()) is None:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                    return None
            if not self.fill():
                return None
        return frame


class _Lifeline:
    """Calls ``end`` from its own thread as soon as a watched process exits."""

    def __init__(self, end: Callable[[], object]) -> None:
        self._events = select.kqueue()
        self._end = end
        threading.Thread(target=self._wait, name="spare-lifeline", daemon=True).start()

    def watch(self, pid: int) -> None:
        event = select.kevent(
            pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD, select.KQ_NOTE_EXIT
        )
        try:
            self._events.control([event], 0)
        except ProcessLookupError:
            self._end()

    def _wait(self) -> None:
        while not self._events.control(None, 1):
            pass
        self._end()


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _sources() -> dict[str, tuple[int, int]]:
    """Every loaded module's file, with its modification time and size."""
    found = {}
    for module in list(sys.modules.values()):
        try:
            path = module.__file__
        except AttributeError:
            continue
        if not isinstance(path, str):
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        found[path] = (info.st_mtime_ns, info.st_size)
    return found


def _unchanged(sources: Mapping[str, tuple[int, int]]) -> bool:
    for path, stamp in sources.items():
        try:
            info = os.stat(path)
        except OSError:
            return False
        if (info.st_mtime_ns, info.st_size) != stamp:
            return False
    return True


def _code_digest() -> str:
    """A digest of the package's sources and the installed packages as they
    stand now. A spare whose loading straddled a change to either never serves."""
    stamps = []
    for root, _dirs, files in os.walk(_PACKAGE):
        for name in files:
            if name.endswith(".py"):
                stamps.append(_stamp(os.path.join(root, name)))
    stamps.extend(_stamp(directory) for directory in site.getsitepackages())
    return hashlib.sha256(repr(sorted(stamps)).encode()).hexdigest()


def _stamp(path: str) -> tuple[str, int, int]:
    try:
        info = os.stat(path)
    except OSError:
        return path, -1, -1
    return path, info.st_mtime_ns, info.st_size


def _startup(env: Mapping[str, str]) -> dict[str, str]:
    """The variables an interpreter, or a module it loads, reads once at load."""
    return {
        name: value
        for name, value in env.items()
        if name.startswith(_STARTUP_PREFIXES) or name in _STARTUP_NAMES
    }


def _interpreter() -> dict[str, object]:
    """How this interpreter was started, apart from how its import path was set."""
    return {
        "executable": sys.executable,
        "prefix": sys.prefix,
        "version": sys.version,
        "package": _PACKAGE,
        "flags": {
            name: getattr(sys.flags, name)
            for name in sys.flags.__match_args__
            if name != "safe_path"
        },
        "warnings": sys.warnoptions,
        "options": sys._xoptions,
    }


# The step's side: find the shell's spare, hand it this invocation, relay.


def run_in_spare(args: list[str]) -> int | None:
    """Run this invocation in the shell's spare and return its exit status,
    or None when no spare is ready for it and it should run here."""
    channel = _channel()
    if channel is None:
        return None
    replies, requests = channel
    shape = _shape(args)
    if shape is None or not _claim(requests):
        return None
    nonce = os.urandom(8).hex()
    frames = _Frames(replies)
    try:
        _write_all(requests, _frame(b"q", json.dumps(_request(nonce)).encode()))
        spare = _accepted(frames, nonce)
    except OSError:
        return None
    if spare is None:
        return None
    program = sys.stdin.buffer.read() if shape == "stdin" else b""
    try:
        _write_all(requests, _frame(b"i", program))
    except OSError:
        print("macos-harness: the spare went away before the step started", file=sys.stderr)
        return 1
    return _relay(frames, spare)


def _channel() -> tuple[int, int] | None:
    """The shell's ends of its spare's two pipes, when this process holds them."""
    try:
        replies, requests = (int(fd) for fd in os.environ.get(ENV, "").split(","))
        kinds = os.fstat(replies).st_mode, os.fstat(requests).st_mode
        modes = fcntl.fcntl(replies, fcntl.F_GETFL), fcntl.fcntl(requests, fcntl.F_GETFL)
    except (ValueError, OSError):
        return None
    if not (stat.S_ISFIFO(kinds[0]) and stat.S_ISFIFO(kinds[1])):
        return None
    if (modes[0] & os.O_ACCMODE, modes[1] & os.O_ACCMODE) != (os.O_RDONLY, os.O_WRONLY):
        return None
    return replies, requests


def _shape(args: list[str]) -> str | None:
    """How a spare can take this invocation: ``stdin`` for a program on
    stdin, ``run`` for a saved one with nothing on stdin, else None. A
    terminal keeps its own process, whose output it sees line by line."""
    command = next((i for i, arg in enumerate(args) if not arg.startswith("-")), len(args))
    if any(arg != "--json-errors" for arg in args[:command]):
        return None
    try:
        if os.isatty(1) or os.isatty(2):
            return None
        if command == len(args):
            return None if os.isatty(0) else "stdin"
        if args[command] == "run" and os.path.samestat(os.fstat(0), os.stat(os.devnull)):
            return "run"
    except OSError:
        return None
    return None


def _claim(requests: int) -> bool:
    """Hold this shell's spare until this process exits; False when another
    step from the same shell holds it."""
    path = os.path.join(tempfile.gettempdir(), "macos-harness-spare.lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        return False
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, os.fstat(requests).st_ino % _LOCK_SPAN)
    except OSError:
        os.close(fd)
        return False
    return True


class _Request(TypedDict):
    """A step as its own process would run it."""

    nonce: str
    pid: int
    argv: list[str]
    cwd: str
    env: dict[str, str]
    path: list[str]
    interpreter: dict[str, object]
    umask: int


def _request(nonce: str) -> _Request:
    mask = os.umask(0)
    os.umask(mask)
    return {
        "nonce": nonce,
        "pid": os.getpid(),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "path": sys.path,
        "interpreter": _interpreter(),
        "umask": mask,
    }


def _accepted(frames: _Frames, nonce: str) -> int | None:
    """The pid of the spare that took this step; None when it was turned
    down or no answer came in time. A killed step's leftovers are skipped."""
    deadline = time.monotonic() + _ANSWER_SECONDS
    while (frame := frames.next(deadline)) is not None:
        kind, payload = frame
        answer = payload.decode("ascii", "replace").split()
        if kind in (b"a", b"n") and answer[:1] == [nonce]:
            return int(answer[1]) if kind == b"a" else None
    return None


def _relay(frames: _Frames, spare: int) -> int:
    def forward(signum: int, _stack: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(spare, signum)

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, forward)
    while (frame := frames.next()) is not None:
        kind, payload = frame
        if kind == b"x":
            status = int(payload)
            if status < 0:
                signal.signal(-status, signal.SIG_DFL)
                os.kill(os.getpid(), -status)
                return 128 - status
            return status
        if kind in (b"o", b"e"):
            _write_all(1 if kind == b"o" else 2, payload)
    print("macos-harness: the spare running this step went away before it finished", file=sys.stderr)
    return 1


# The broker: the shell's coprocess, keeping one spare ready at a time.


def serve_shell() -> int:
    """Run as a shell's coprocess: keep one loaded spare ready, hand it the
    next step from this shell, and exit when the shell does."""
    if os.getppid() == 1:
        return 0
    return _Broker().run()


class _Spare:
    """One spare child, and the broker's ends of its four pipes."""

    def __init__(
        self, popen: subprocess.Popen[bytes], control: int, requests: int, out: int, err: int
    ) -> None:
        self.popen = popen
        self.pid = popen.pid
        self.control = _Frames(control)
        self.requests = requests
        self.outputs = {out: b"o", err: b"e"}
        self.ready = False
        self.listening = True
        self._fds = (control, requests, out, err)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)

    def close(self) -> int:
        status = self.popen.wait()
        for fd in self._fds:
            os.close(fd)
        return status


class _Broker:
    def __init__(self) -> None:
        self._shell = os.getppid()
        self._sources = _sources()
        self._env = dict(os.environ)
        self._inbox = _Frames(0)
        self._exits = select.kqueue()
        self._spare: _Spare | None = None
        self._serving = False
        self._awaiting_input = False
        _Lifeline(self._abandon).watch(self._shell)
        if os.getppid() != self._shell:
            self._abandon()

    def run(self) -> int:
        self._spawn()
        while True:
            spare = self._spare
            watched = [0, self._exits.fileno()]
            if spare is not None and spare.listening:
                watched.append(spare.control.fd)
            if spare is not None and self._serving:
                watched.extend(spare.outputs)
            ready = select.select(watched, [], [])[0]
            if spare is not None and self._serving:
                for fd in [fd for fd in ready if fd in spare.outputs]:
                    self._pass_on(spare, fd)
            if spare is not None and spare.control.fd in ready:
                self._hear(spare)
            if self._exits.fileno() in ready:
                self._reap()
            if 0 in ready and not self._take_requests():
                break
        self._retire()
        return 0

    def _abandon(self) -> NoReturn:
        spare = self._spare
        if spare is not None:
            spare.kill()
        os._exit(0)

    def _reply(self, kind: bytes, payload: bytes) -> None:
        try:
            _write_all(1, _frame(kind, payload))
        except BrokenPipeError:
            self._abandon()

    def _spawn(self) -> None:
        control, control_end = os.pipe()
        requests_end, requests = os.pipe()
        out, out_end = os.pipe()
        err, err_end = os.pipe()
        ends = (control_end, requests_end, out_end, err_end)
        try:
            popen = subprocess.Popen(
                [
                    sys.executable,
                    "-P",
                    "-c",
                    _SPARE,
                    *map(str, ends),
                    str(os.getpid()),
                    _code_digest(),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=ends,
                cwd="/",
                env=self._env,
            )
        except OSError:
            for fd in (control, requests, out, err):
                os.close(fd)
            return
        finally:
            for fd in ends:
                os.close(fd)
        spare = _Spare(popen, control, requests, out, err)
        exit_event = select.kevent(
            popen.pid,
            select.KQ_FILTER_PROC,
            select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
            select.KQ_NOTE_EXIT,
        )
        try:
            self._exits.control([exit_event], 0)
        except ProcessLookupError:
            spare.close()
            return
        self._spare = spare

    def _retire(self) -> None:
        spare, self._spare = self._spare, None
        if spare is not None:
            spare.kill()
            spare.close()

    def _hear(self, spare: _Spare) -> None:
        if not spare.control.fill():
            spare.listening = False
        while (frame := spare.control.pop()) is not None:
            if frame[0] == b"r":
                spare.ready = True

    def _pass_on(self, spare: _Spare, fd: int) -> None:
        data = os.read(fd, _CHUNK)
        if data:
            self._reply(spare.outputs[fd], data)
        else:
            del spare.outputs[fd]

    def _reap(self) -> None:
        exited = {event.ident for event in self._exits.control(None, 8, 0)}
        spare = self._spare
        if spare is None or spare.pid not in exited:
            return
        self._spare = None
        if not self._serving:
            if spare.close() == _STALE:
                self._renew()
                self._spawn()
            return
        for fd, kind in spare.outputs.items():
            os.set_blocking(fd, False)
            with contextlib.suppress(BlockingIOError):
                while data := os.read(fd, _CHUNK):
                    self._reply(kind, data)
        status = spare.close()
        self._serving = self._awaiting_input = False
        self._reply(b"x", str(status).encode())
        self._renew()
        self._spawn()

    def _take_requests(self) -> bool:
        if not self._inbox.fill():
            return False
        if not self._serving:
            self._renew()
        while (frame := self._inbox.pop()) is not None:
            kind, payload = frame
            if kind == b"q":
                self._answer(payload)
            elif kind == b"i" and self._awaiting_input and self._spare is not None:
                self._awaiting_input = False
                with contextlib.suppress(OSError):
                    _write_all(self._spare.requests, _frame(b"i", payload))
        return True

    def _answer(self, payload: bytes) -> None:
        try:
            request = json.loads(payload)
            nonce = str(request["nonce"]).encode()
            env = {str(name): str(value) for name, value in request["env"].items()}
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        self._env = env
        spare = self._spare
        if self._serving or spare is None or not spare.ready:
            self._reply(b"n", nonce)
            if spare is None:
                self._spawn()
            return
        try:
            _write_all(spare.requests, _frame(b"q", payload))
            answer = spare.control.next(time.monotonic() + _ANSWER_SECONDS / 2)
        except OSError:
            answer = None
        if answer is not None and answer[0] == b"k":
            self._serving = self._awaiting_input = True
            self._reply(b"a", nonce + b" " + str(spare.pid).encode())
            return
        self._reply(b"n", nonce)
        self._retire()
        self._spawn()

    def _renew(self) -> None:
        """Start over on the code now on disk if this broker's own code
        changed, keeping this pid and the shell's pipes; the steps asking
        meanwhile run in their own process."""
        if _unchanged(self._sources):
            return
        while (frame := self._inbox.pop()) is not None:
            if frame[0] == b"q":
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    self._reply(b"n", str(json.loads(frame[1])["nonce"]).encode())
        self._retire()
        os.execv(sys.executable, [sys.executable, "-P", "-c", _BROKER])


# The spare's side: load, wait for one step, become that step's process.


def serve_one_step(argv: list[str]) -> NoReturn:
    """A spare's whole life: load everything, take one step from the broker
    that started it, run it as that step's own process would, and exit."""
    control, requests, out, err, broker = map(int, argv[:5])
    lifeline = _Lifeline(_kill_self)
    lifeline.watch(broker)
    if os.getppid() != broker:
        _kill_self()
    for name in _PRELOAD:
        importlib.import_module(name)
    if _code_digest() != argv[5]:
        os._exit(_STALE)
    sources = _sources()
    _write_all(control, _frame(b"r"))
    frames = _Frames(requests)
    frame = frames.next()
    if frame is None or frame[0] != b"q":
        os._exit(0)
    request: _Request = json.loads(frame[1])
    if not _fits(request, sources):
        _write_all(control, _frame(b"s"))
        os._exit(0)
    lifeline.watch(int(request["pid"]))
    _write_all(control, _frame(b"k"))
    frame = frames.next()
    if frame is None or frame[0] != b"i":
        os._exit(0)
    os.close(control)
    os.close(requests)
    _become(request, frame[1], out, err)
    from .cli import main

    sys.exit(main(request["argv"][1:]))


def _fits(request: _Request, sources: Mapping[str, tuple[int, int]]) -> bool:
    """Whether this spare runs the step exactly as the step's own process
    would: the same interpreter, flags, import path and load-time settings,
    no loaded file changed since, and the step's directory still there."""
    if request["interpreter"] != _interpreter() or request["path"][1:] != sys.path:
        return False
    if _startup(request["env"]) != _startup(os.environ) or not _unchanged(sources):
        return False
    try:
        os.chdir(request["cwd"])
    except OSError:
        return False
    return True


def _become(request: _Request, stdin: bytes, out: int, err: int) -> None:
    with tempfile.TemporaryFile() as held:
        held.write(stdin)
        held.flush()
        os.lseek(held.fileno(), 0, os.SEEK_SET)
        os.dup2(held.fileno(), 0)
    os.dup2(out, 1)
    os.dup2(err, 2)
    os.close(out)
    os.close(err)
    env = dict(request["env"])
    env.pop(ENV, None)
    os.environ.clear()
    os.environ.update(env)
    os.umask(int(request["umask"]))
    sys.argv = list(request["argv"])
    sys.path[:] = request["path"]
