"""Disposable macOS virtual machines, so agent work stays off the host's desktop.

The host clones, starts, snapshots, resets and deletes guests with tart
(https://github.com/cirruslabs/tart), headless. Inside a guest,
`macos-harness vm serve` runs from the guest's Terminal, which holds the
guest's Accessibility and Screen Recording grants, and runs each program the
host forwards with the guest's own harness. Files a forwarded program writes
to its temporary directory, such as `mac.see` pictures, come back to the host.
"""

from __future__ import annotations

import base64
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from ._paths import config_dir
from ._version import __version__
from .errors import ErrorCode, MacOSError

TART_ENV = "MACOS_HARNESS_TART"
DEFAULT_BASE = "macos-harness-base"
GUEST_PORT = 47821
GUEST_AGENT_LABEL = "com.github.macos-harness.vm-guest"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")
_SNAPSHOT = ".snapshot."
_RUN_FLAGS = ("--no-graphics", "--no-clipboard", "--no-audio", "--no-usb-accessories")
_FRAME = struct.Struct(">I")
_LARGEST_FRAME = 1 << 28
_LARGEST_FILES = 1 << 26
_CONNECT_SECONDS = 5.0
_SYNC_SECONDS = 30.0


class VM(TypedDict):
    name: str
    state: str
    base: str
    snapshots: list[str]


class Guest(TypedDict):
    name: str
    version: str
    address: str


class _Ran(TypedDict):
    exit: int
    stdout: str
    stderr: str
    files: dict[str, str]


def _tart_path() -> str:
    found = os.environ.get(TART_ENV) or shutil.which("tart")
    if not found:
        raise MacOSError(
            f"tart is not installed: put it on PATH or name it in {TART_ENV}",
            code=ErrorCode.UNSUPPORTED_OP,
        )
    return found


def _tart_env() -> dict[str, str]:
    # Without this, `tart clone` deletes cached restore images and OCI
    # layers whenever it judges the disk too full for the clone.
    return {**os.environ, "TART_NO_AUTO_PRUNE": "1"}


def _tart(*args: str) -> str:
    done = subprocess.run(
        [_tart_path(), *args], capture_output=True, text=True, env=_tart_env(), check=False
    )
    if done.returncode:
        raise MacOSError(
            f"tart {args[0]} failed: {(done.stderr or done.stdout).strip()}",
            code="vm.tart_failed",
            details={"args": list(args), "exit": done.returncode},
        )
    return done.stdout


def _check_name(value: str, what: str) -> str:
    if not _NAME.fullmatch(value):
        raise MacOSError(
            f"{what} {value!r} must be letters, digits and dashes, starting with a letter or digit",
            code=ErrorCode.BAD_REQUEST,
        )
    return value


def _home(name: str) -> Path:
    return config_dir() / "vms" / name


def _tart_vms() -> dict[str, str]:
    """Every tart VM on this Mac, with its state."""
    return {vm["Name"]: vm["State"] for vm in json.loads(_tart("list", "--format", "json"))}


def _base(name: str) -> str:
    """The VM ``name`` was cloned from; refuses a VM `create` did not make."""
    _check_name(name, "VM name")
    try:
        return json.loads((_home(name) / "vm.json").read_text(encoding="utf-8"))["base"]
    except FileNotFoundError:
        raise MacOSError(
            f"no VM named {name!r} was made with `macos-harness vm create`",
            code=ErrorCode.BAD_REQUEST,
        ) from None


def _snapshot_name(name: str, label: str) -> str:
    return name + _SNAPSHOT + _check_name(label, "snapshot label")


def _describe(name: str, tart: Mapping[str, str]) -> VM:
    prefix = name + _SNAPSHOT
    return {
        "name": name,
        "state": tart.get(name, "missing"),
        "base": _base(name),
        "snapshots": sorted(vm[len(prefix):] for vm in tart if vm.startswith(prefix)),
    }


def list_vms() -> list[VM]:
    """The VMs `create` made, each with its state, base and snapshot labels."""
    names = sorted(record.parent.name for record in (config_dir() / "vms").glob("*/vm.json"))
    tart = _tart_vms()
    return [_describe(name, tart) for name in names]


def _refuse_running(source: str, vms: Mapping[str, str]) -> None:
    """A running VM's disk copies torn, so ``source`` must be stopped."""
    if vms.get(source) == "running":
        raise MacOSError(
            f"{source!r} is running; stop it before copying its disk", code=ErrorCode.BAD_REQUEST
        )


def create(name: str, *, base: str = DEFAULT_BASE) -> VM:
    """Clone ``base`` into a new stopped VM ``name``. The clone shares the
    base's disk blocks until either writes them."""
    _check_name(name, "VM name")
    vms = _tart_vms()
    if name in vms or _home(name).exists():
        raise MacOSError(f"a VM named {name!r} already exists", code=ErrorCode.BAD_REQUEST)
    _refuse_running(base, vms)
    home = _home(name)
    home.mkdir(parents=True)
    (home / "vm.json").write_text(json.dumps({"base": base}), encoding="utf-8")
    try:
        _tart("clone", base, name)
    except BaseException:
        shutil.rmtree(home)
        raise
    return _describe(name, _tart_vms())


def start(name: str, *, timeout: float = 300.0) -> Guest:
    """Boot ``name`` without a window and return once its guest harness
    answers: its address and harness version."""
    _base(name)
    deadline = time.monotonic() + timeout
    process = None
    if _tart_vms().get(name) != "running":
        with (_home(name) / "tart.log").open("ab") as log:
            process = subprocess.Popen(
                [_tart_path(), "run", *_RUN_FLAGS, name],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=_tart_env(), start_new_session=True,
            )
    while True:
        try:
            reply = _call(name, {"op": "ping"}, wait=_CONNECT_SECONDS)
            return {"name": name, "version": str(reply["version"]), "address": str(reply["address"])}
        except OSError:
            pass
        if process is not None and process.poll() is not None:
            raise MacOSError(
                f"tart stopped {name!r} while it booted; see {_home(name) / 'tart.log'}",
                code="vm.tart_failed",
            )
        if time.monotonic() > deadline:
            raise MacOSError(
                f"{name!r} did not answer within {timeout:g}s of starting",
                code=ErrorCode.TIMEOUT,
            )
        time.sleep(1)


def stop(name: str) -> None:
    """Power ``name`` off. tart cuts the guest's power instead of shutting
    macOS down, so the guest flushes its disk first when it can."""
    _base(name)
    if _tart_vms().get(name) != "running":
        return
    try:
        _call(name, {"op": "sync"}, wait=_SYNC_SECONDS)
    except (OSError, MacOSError):
        pass
    _tart("stop", name)


def _copy_disk(name: str, source: str, target: str) -> VM:
    """Clone ``source`` over ``target`` while ``name`` is shut down, then
    boot ``name`` again if it ran."""
    vms = _tart_vms()
    if source != name:
        _refuse_running(source, vms)
    running = vms.get(name) == "running"
    if running:
        stop(name)
    _tart("clone", source, target, "--overwrite")
    if running:
        start(name)
    return _describe(name, _tart_vms())


def snapshot(name: str, label: str) -> VM:
    """Keep ``name``'s disk as snapshot ``label``, replacing one of that
    label. A running VM shuts down for the copy and boots again."""
    _base(name)
    return _copy_disk(name, name, _snapshot_name(name, label))


def reset(name: str, *, to: str | None = None) -> VM:
    """Put ``name`` back to snapshot ``to``, or to its base when ``to`` is
    None. A running VM boots again afterward."""
    base = _base(name)
    source = _snapshot_name(name, to) if to is not None else base
    if source not in _tart_vms():
        raise MacOSError(f"{name!r} has no snapshot {to!r}", code=ErrorCode.BAD_REQUEST)
    return _copy_disk(name, source, name)


def delete(name: str) -> None:
    """Stop ``name`` and delete it, its snapshots, and the files its
    programs sent back."""
    _base(name)
    stop(name)
    prefix = name + _SNAPSHOT
    for vm in _tart_vms():
        if vm == name or vm.startswith(prefix):
            _tart("delete", vm)
    shutil.rmtree(_home(name))


def _send(sock: socket.socket, payload: Mapping[str, object]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode()
    sock.sendall(_FRAME.pack(len(data)) + data)


def _receive(sock: socket.socket) -> dict[str, object]:
    def exactly(count: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < count:
            chunk = sock.recv(min(count - len(buffer), 1 << 20))
            if not chunk:
                raise ConnectionError("the other side closed the connection")
            buffer += chunk
        return bytes(buffer)

    (length,) = _FRAME.unpack(exactly(_FRAME.size))
    if length > _LARGEST_FRAME:
        raise ConnectionError(f"a {length}-byte message is larger than {_LARGEST_FRAME}")
    try:
        message = json.loads(exactly(length))
    except ValueError as exc:
        raise ConnectionError(f"a message was not JSON: {exc}") from None
    if not isinstance(message, dict):
        raise ConnectionError("a message was not a JSON object")
    return message


def _address(name: str) -> str:
    done = subprocess.run(
        [_tart_path(), "ip", name], capture_output=True, text=True, env=_tart_env(), check=False
    )
    if done.returncode:
        raise ConnectionError(done.stderr.strip() or f"{name} has no address yet")
    return done.stdout.strip()


def _call(name: str, request: Mapping[str, object], *, wait: float | None) -> dict[str, object]:
    """Send ``request`` and return the reply, waiting ``wait`` seconds for
    it, or for as long as it takes when None."""
    with socket.create_connection((_address(name), GUEST_PORT), timeout=_CONNECT_SECONDS) as sock:
        sock.settimeout(wait)
        if wait is None:
            # tart cuts a guest's power without closing its connections;
            # keepalive notices the missing guest instead of waiting forever.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, 15)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        _send(sock, request)
        reply = _receive(sock)
    if "error" in reply:
        raise MacOSError(f"{name}: {reply['error']}", code="vm.guest_failed")
    return reply


def _ran(name: str, reply: Mapping[str, object]) -> _Ran:
    """``reply`` as a finished run, refused unless it has that shape: the
    guest is not trusted."""
    status, stdout, stderr, files = (reply.get(key) for key in ("exit", "stdout", "stderr", "files"))
    if (
        isinstance(status, int)
        and isinstance(stdout, str)
        and isinstance(stderr, str)
        and isinstance(files, dict)
        and all(isinstance(path, str) and isinstance(data, str) for path, data in files.items())
    ):
        return {"exit": status, "stdout": stdout, "stderr": stderr, "files": files}
    raise MacOSError(f"{name} sent a malformed reply", code="vm.guest_failed")


def run(name: str, argv: Sequence[str], stdin: str | None) -> int:
    """Run ``macos-harness`` with ``argv`` inside ``name``, print what it
    printed, and return its exit status. Files it wrote to its temporary
    directory land under this VM's folder on the host, and the paths it
    printed are rewritten to match; host paths under that folder in
    ``argv`` or ``stdin`` reach the guest as its own."""
    _base(name)
    files = _home(name) / "files"
    files.mkdir(exist_ok=True)
    root = files.resolve()
    request = {"op": "run", "argv": list(argv), "stdin": stdin, "files": str(root)}
    try:
        reply = _ran(name, _call(name, request, wait=None))
    except OSError as exc:
        raise MacOSError(
            f"{name} did not answer ({exc}); is it started? `macos-harness vm start {name}`",
            code="vm.unreachable",
        ) from None
    for relative, data in reply["files"].items():
        target = (root / relative).resolve()
        if not target.is_relative_to(root) or target == root:
            raise MacOSError(
                f"{name} sent a file outside its folder: {relative!r}", code="vm.guest_failed"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(data))
    sys.stdout.write(reply["stdout"])
    sys.stderr.write(reply["stderr"])
    return reply["exit"]


def _guest_home() -> Path:
    return config_dir() / "vm-guest"


def install_guest() -> Path:
    """Inside a guest: start `vm serve` from Terminal at every login, and
    now. Returns the launch agent's path."""
    home = _guest_home()
    home.mkdir(parents=True, exist_ok=True)
    command = home / "serve.command"
    command.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m macos_harness.cli vm serve\n",
        encoding="utf-8",
    )
    command.chmod(0o755)
    agent = Path.home() / "Library" / "LaunchAgents" / f"{GUEST_AGENT_LABEL}.plist"
    agent.parent.mkdir(parents=True, exist_ok=True)
    opener = ["/usr/bin/open", "-g", "-a", "Terminal", str(command)]
    with agent.open("wb") as handle:
        plistlib.dump({"Label": GUEST_AGENT_LABEL, "ProgramArguments": opener, "RunAtLoad": True}, handle)
    subprocess.run(opener, check=True)
    return agent


def _gateway() -> str | None:
    done = subprocess.run(
        ["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, check=False
    )
    found = re.search(r"gateway: (\S+)", done.stdout)
    return found[1] if found else None


def _run_forwarded(request: Mapping[str, object], requests: Path) -> _Ran:
    host_root, argv, stdin = request.get("files"), request.get("argv"), request.get("stdin")
    if not (
        isinstance(host_root, str)
        and isinstance(argv, list)
        and all(isinstance(arg, str) for arg in argv)
        and (stdin is None or isinstance(stdin, str))
    ):
        raise ValueError("a run needs files, argv and stdin")
    work = requests / uuid.uuid4().hex
    work.mkdir()

    def inward(text: str) -> str:
        return text.replace(host_root, str(requests))

    def outward(text: str) -> str:
        return text.replace(str(requests), host_root)

    done = subprocess.run(
        [sys.executable, "-m", "macos_harness.cli", *(inward(arg) for arg in argv)],
        input=inward(stdin) if stdin is not None else None,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True, text=True, check=False,
        env={**os.environ, "TMPDIR": f"{work}/"},
    )
    files: dict[str, str] = {}
    left_behind: list[str] = []
    total = 0
    for path in sorted(work.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        size = path.stat().st_size
        if total + size > _LARGEST_FILES:
            left_behind.append(path.name)
            continue
        total += size
        files[path.relative_to(requests).as_posix()] = base64.b64encode(path.read_bytes()).decode()
    shutil.rmtree(work)
    note = (
        f"macos-harness vm: not sent back, past {_LARGEST_FILES >> 20} MiB: {', '.join(left_behind)}\n"
        if left_behind
        else ""
    )
    return {
        "exit": done.returncode,
        "stdout": outward(done.stdout),
        "stderr": outward(done.stderr) + note,
        "files": files,
    }


def serve(port: int = GUEST_PORT) -> int:
    """Inside a guest: answer the host until killed. Only the guest's
    default gateway, which is the host, is answered."""
    requests = _guest_home() / "requests"
    shutil.rmtree(requests, ignore_errors=True)
    requests.mkdir(parents=True)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            if self.client_address[0] != _gateway():
                return
            sock: socket.socket = self.request
            request = _receive(sock)
            reply: Mapping[str, object]
            try:
                if request.get("op") == "ping":
                    reply = {"version": __version__, "address": sock.getsockname()[0]}
                elif request.get("op") == "sync":
                    os.sync()
                    reply = {"synced": True}
                elif request.get("op") == "run":
                    reply = _run_forwarded(request, requests)
                else:
                    reply = {"error": f"unknown op {request.get('op')!r}"}
            except Exception as exc:  # noqa: BLE001
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            _send(sock, reply)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("0.0.0.0", port), Handler) as server:
        print(f"macos-harness {__version__} serving the host on port {port}", flush=True)
        server.serve_forever()
    return 0
