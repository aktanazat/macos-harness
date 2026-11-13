"""Disposable macOS virtual machines, so agent work stays off the host's desktop.

The host clones, starts, suspends, snapshots, resets and deletes guests with
tart (https://github.com/cirruslabs/tart), headless. Inside a guest,
`macos-harness vm serve` runs from the guest's Terminal, which holds the
guest's Accessibility and Screen Recording grants, and runs each program the
host forwards with the guest's own harness, or with the copy of a host checkout
the host sends it. Files a forwarded program writes to its temporary directory,
such as `mac.see` pictures, come back to the host.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import io
import json
import os
import plistlib
import re
import select
import shlex
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TypedDict

from ._paths import config_dir
from ._version import __version__
from .errors import ErrorCode, MacOSError
from .spare import ENV as SPARE_ENV

TART_ENV = "MACOS_HARNESS_TART"
DEFAULT_BASE = "macos-harness-base"
GUEST_PORT = 47821
GUEST_AGENT_LABEL = "com.github.macos-harness.vm-guest"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")
_SNAPSHOT = ".snapshot."
_RUN_FLAGS = (
    "--no-graphics", "--no-clipboard", "--no-audio", "--no-usb-accessories", "--suspendable"
)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SOURCES_KEPT = 3
_RELEASE_SECONDS = 30.0
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
    source: str | None


class _Record(TypedDict):
    base: str
    source: str | None


class _Ran(TypedDict):
    exit: int
    stdout: str
    stderr: str
    files: dict[str, str]


class Put(TypedDict):
    name: str
    path: str
    bytes: int
    resigned: bool
    dropped_entitlements: list[str]


_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
_PUT_CHUNK = _LARGEST_FILES
_PUT_APPEND = """\
import shutil
from pathlib import Path
staging = Path.home() / "Library" / "Caches" / "macos-harness-put" / {token!r}
staging.parent.mkdir(parents=True, exist_ok=True)
with open({part!r}, "rb") as part, staging.open("r+b" if {offset} else "wb") as out:
    out.seek({offset})
    shutil.copyfileobj(part, out)
"""
_PUT_FINISH = """\
import hashlib, json, shutil, subprocess
from pathlib import Path
staging = Path.home() / "Library" / "Caches" / "macos-harness-put" / {token!r}
folder = Path({folder!r}).expanduser()
target = folder / {item!r}
unpacked = folder / ("." + {token!r})
try:
    digest = hashlib.sha256()
    with staging.open("rb") as archive:
        for block in iter(lambda: archive.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != {digest!r}:
        raise SystemExit("the copy arrived damaged; run `vm put` again")
    listed = subprocess.run(
        ["/bin/ps", "-axww", "-o", "command="], capture_output=True, text=True, check=True
    ).stdout
    if any(line.startswith(str(target) + "/") for line in listed.splitlines()):
        raise SystemExit(f"{{target.name}} is running in the guest; quit it first")
    folder.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["/usr/bin/ditto", "-x", "-k", str(staging), str(unpacked)], capture_output=True, text=True
    )
    if done.returncode:
        raise SystemExit(f"unpacking failed: {{done.stderr.strip()}}")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    (unpacked / {item!r}).rename(target)
    if target.suffix == ".app":
        subprocess.run([{lsregister!r}, "-f", str(target)], check=True)
    print(json.dumps({{"path": str(target)}}))
finally:
    staging.unlink(missing_ok=True)
    shutil.rmtree(unpacked, ignore_errors=True)
"""
_PUT_DISCARD = """\
from pathlib import Path
(Path.home() / "Library" / "Caches" / "macos-harness-put" / {token!r}).unlink(missing_ok=True)
"""


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


def _tart_folder(name: str) -> Path:
    return Path(os.environ.get("TART_HOME") or Path.home() / ".tart") / "vms" / name


def _record(name: str) -> _Record:
    """What `create` and `start` noted about ``name``; refuses a VM `create`
    did not make."""
    _check_name(name, "VM name")
    try:
        saved = json.loads((_home(name) / "vm.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MacOSError(
            f"no VM named {name!r} was made with `macos-harness vm create`",
            code=ErrorCode.BAD_REQUEST,
        ) from None
    return {"base": saved["base"], "source": saved.get("source")}


def _save(name: str, record: _Record) -> None:
    (_home(name) / "vm.json").write_text(json.dumps(record), encoding="utf-8")


def _base(name: str) -> str:
    """The VM ``name`` was cloned from; refuses a VM `create` did not make."""
    return _record(name)["base"]


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
    _save(name, {"base": base, "source": None})
    try:
        _tart("clone", base, name)
    except BaseException:
        shutil.rmtree(home)
        raise
    return _describe(name, _tart_vms())


def _checked_source(source: str | os.PathLike[str]) -> str:
    found = Path(source).expanduser().resolve()
    if not (found / "src" / "macos_harness" / "__init__.py").is_file():
        raise MacOSError(
            f"{found} is not a macos-harness checkout: it has no src/macos_harness",
            code=ErrorCode.BAD_REQUEST,
        )
    return str(found)


def start(
    name: str, *, source: str | os.PathLike[str] | None = None, timeout: float = 300.0
) -> Guest:
    """Boot ``name`` without a window, or resume it if suspended, and return
    once its guest harness answers. With ``source``, a macos-harness checkout
    on this Mac, every later `run` sends the guest that checkout's code as it
    stands then, and the guest runs it in place of its installed harness;
    without it, the guest runs its own."""
    record = _record(name)
    record["source"] = _checked_source(source) if source is not None else None
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
            break
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
    if record["source"] is not None and reply.get("sources") is not True:
        raise MacOSError(
            f"{name!r} runs a harness too old to take a source; rebuild its base",
            code=ErrorCode.UNSUPPORTED_OP,
        )
    _save(name, record)
    return {
        "name": name,
        "version": str(reply["version"]),
        "address": str(reply["address"]),
        "source": record["source"],
    }


def suspend(name: str) -> None:
    """Save ``name``'s memory to disk and stop it; the next `start` resumes
    it where it was in a few seconds instead of booting."""
    _base(name)
    if _tart_vms().get(name) != "running":
        raise MacOSError(f"{name!r} is not running", code=ErrorCode.BAD_REQUEST)
    _tart("suspend", name)
    _wait_released(name)


def _wait_released(name: str) -> None:
    """Return once the tart process that ran ``name`` let go of its files;
    tart reports a VM stopped or suspended before it does, and a `tart run`
    started meanwhile fails to lock them."""
    deadline = time.monotonic() + _RELEASE_SECONDS
    while True:
        with (_tart_folder(name) / "nvram.bin").open("rb") as nvram:
            try:
                fcntl.flock(nvram, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                return
        if time.monotonic() > deadline:
            raise MacOSError(
                f"tart still holds {name!r} {_RELEASE_SECONDS:g}s after stopping it",
                code=ErrorCode.TIMEOUT,
            )
        time.sleep(0.1)


def stop(name: str) -> None:
    """Power ``name`` off. tart cuts the guest's power instead of shutting
    macOS down, so the guest flushes its disk first when it can. A suspended
    VM loses the memory it saved."""
    _base(name)
    state = _tart_vms().get(name)
    if state == "running":
        try:
            _call(name, {"op": "sync"}, wait=_SYNC_SECONDS)
        except (OSError, MacOSError):
            pass
    elif state != "suspended":
        return
    _tart("stop", name)
    _wait_released(name)


def _copy_vm(source: str, target: str) -> None:
    """Make tart VM ``target`` an APFS clone of stopped or suspended VM
    ``source``, saved memory and network identity included."""
    origin, copy = _tart_folder(source), _tart_folder(target)
    files = [str(path) for path in origin.iterdir() if path.is_file()]
    partial = copy.parent.parent / f"macos-harness-copy-{uuid.uuid4().hex}"
    partial.mkdir()
    done = subprocess.run(
        ["/bin/cp", "-c", *files, str(partial)], capture_output=True, text=True, check=False
    )
    if done.returncode:
        shutil.rmtree(partial, ignore_errors=True)
        raise MacOSError(
            f"copying {source!r} to {target!r} failed: {done.stderr.strip()}",
            code="vm.tart_failed",
        )
    shutil.rmtree(copy, ignore_errors=True)
    partial.rename(copy)


def snapshot(name: str, label: str) -> VM:
    """Keep ``name`` as snapshot ``label``, replacing one of that label. A
    running VM is suspended for the copy and resumed, so the snapshot keeps
    its open apps and memory and `reset` returns to them in seconds."""
    record = _record(name)
    target = _snapshot_name(name, label)
    running = _tart_vms().get(name) == "running"
    if running:
        suspend(name)
    _copy_vm(name, target)
    if running:
        start(name, source=record["source"])
    return _describe(name, _tart_vms())


def reset(name: str, *, to: str | None = None) -> VM:
    """Put ``name`` back to snapshot ``to``, or to its base when ``to`` is
    None. A VM that was running runs again afterward: from the snapshot's
    saved memory when it has one, else from a boot."""
    record = _record(name)
    origin = _snapshot_name(name, to) if to is not None else record["base"]
    vms = _tart_vms()
    if origin not in vms:
        raise MacOSError(f"{name!r} has no snapshot {to!r}", code=ErrorCode.BAD_REQUEST)
    if to is None:
        _refuse_running(origin, vms)
    running = vms.get(name) == "running"
    if vms.get(name) in ("running", "suspended"):
        _tart("stop", name)
        _wait_released(name)
    if to is None:
        _tart("clone", origin, name, "--overwrite")
    else:
        _copy_vm(origin, name)
    if running:
        start(name, source=record["source"])
    return _describe(name, _tart_vms())


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
    reply = _run(name, argv, stdin)
    sys.stdout.write(reply["stdout"])
    sys.stderr.write(reply["stderr"])
    return reply["exit"]


def _run(name: str, argv: Sequence[str], stdin: str | None) -> _Ran:
    source = _record(name)["source"]
    files = _home(name) / "files"
    files.mkdir(exist_ok=True)
    root = files.resolve()
    request: dict[str, object] = {
        "op": "run",
        "argv": list(argv),
        "stdin": stdin,
        "files": str(root),
        "inputs": _named_files(root, [*argv, stdin or ""]),
    }
    package = None if source is None else _package_files(source)
    if package is not None:
        request["source"] = _package_digest(package)
    try:
        if package is None:
            reply = _ran(name, _call(name, request, wait=None))
        else:
            answer = _call(name, request, wait=None)
            if answer.get("need_source") is True:
                request["source_zip"] = _package_zip(package)
                answer = _call(name, request, wait=None)
            reply = _ran(name, answer)
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
    return reply


def _guest_program(name: str, program: str) -> str:
    """Run ``program`` with the guest's harness and return what it printed;
    a failure raises with what it wrote to stderr."""
    reply = _run(name, [], program)
    if reply["exit"]:
        last = (reply["stderr"].strip().splitlines() or ["no error output"])[-1]
        raise MacOSError(f"{name}: {last}", code="vm.guest_failed")
    return reply["stdout"]


def put(name: str, path: str | os.PathLike[str], *, to: str | None = None) -> Put:
    """Copy ``path``, a file or folder on this Mac such as an app, into
    ``name``'s folder ``to``: /Applications for an app and the guest's home
    for anything else when None. It replaces what has its name there, and
    an app is registered so `mac.launch` finds it by name. An app whose
    provisioning profile leaves out the guest would not launch there, so its
    copy is signed ad hoc without the entitlements only a profile grants."""
    _record(name)
    source = Path(path).expanduser().resolve()
    if not source.exists():
        raise MacOSError(f"{source} does not exist", code=ErrorCode.BAD_REQUEST)
    folder = to if to is not None else "/Applications" if source.suffix == ".app" else "~"
    if not folder.startswith(("/", "~")):
        raise MacOSError(
            f"--to {folder!r} must be an absolute guest path or start with ~",
            code=ErrorCode.BAD_REQUEST,
        )
    token = f"put-{uuid.uuid4().hex}"
    staging = _home(name) / "files" / token
    staging.mkdir(parents=True)
    try:
        payload, dropped = source, None
        profiles = _profiles(source) if source.suffix == ".app" and source.is_dir() else []
        if profiles and not all(_provisions(profile, _guest_udid(name)) for profile in profiles):
            payload = staging / source.name
            subprocess.run(["/bin/cp", "-cR", str(source), str(payload)], check=True)
            dropped = _resign(payload)
        archive = staging / "archive.zip"
        # ditto keeps an app's symlinks, modes and signature; zipfile does not.
        # --keepParent names a folder's own top level, but a file's parent.
        keep = ["--keepParent"] if payload.is_dir() else []
        done = subprocess.run(
            ["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", *keep, str(payload), str(archive)],
            capture_output=True, text=True, check=False,
        )
        if done.returncode:
            raise MacOSError(f"packing {source} failed: {done.stderr.strip()}", code=ErrorCode.BAD_REQUEST)
        digest = hashlib.sha256()
        offset = 0
        try:
            with archive.open("rb") as data:
                while chunk := data.read(_PUT_CHUNK):
                    digest.update(chunk)
                    part = staging.resolve() / f"part-{offset}"
                    part.write_bytes(chunk)
                    _guest_program(name, _PUT_APPEND.format(token=token, part=str(part), offset=offset))
                    part.unlink()
                    offset += len(chunk)
            finish = _PUT_FINISH.format(
                token=token, digest=digest.hexdigest(), folder=folder, item=source.name, lsregister=_LSREGISTER
            )
            landed = json.loads(_guest_program(name, finish))
        except BaseException:
            with contextlib.suppress(MacOSError):
                _guest_program(name, _PUT_DISCARD.format(token=token))
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {
        "name": name,
        "path": str(landed["path"]),
        "bytes": offset,
        "resigned": dropped is not None,
        "dropped_entitlements": dropped or [],
    }


_GUEST_UDID = """\
import json, subprocess
shown = subprocess.run(
    ["/usr/sbin/system_profiler", "-json", "SPHardwareDataType"], capture_output=True, check=True
)
print(json.loads(shown.stdout)["SPHardwareDataType"][0]["provisioning_UDID"])
"""


def _guest_udid(name: str) -> str:
    """The device id a provisioning profile must list for ``name``."""
    return _guest_program(name, _GUEST_UDID).strip()


def _profiles(app: Path) -> list[Path]:
    return [
        Path(folder) / "embedded.provisionprofile"
        for folder, _, files in os.walk(app)
        if "embedded.provisionprofile" in files
    ]


def _provisions(profile: Path, udid: str) -> bool:
    """Whether ``profile`` lets code it signs run on device ``udid``."""
    done = subprocess.run(
        ["/usr/bin/security", "cms", "-D", "-i", str(profile)], capture_output=True, check=False
    )
    if done.returncode:
        return False
    found = plistlib.loads(done.stdout)
    return found.get("ProvisionsAllDevices") is True or udid in found.get("ProvisionedDevices", [])


_BUNDLES = frozenset({".app", ".appex", ".bundle", ".framework", ".plugin", ".systemextension", ".xpc"})
_MACHO = frozenset({b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"})


def _resign(app: Path) -> list[str]:
    """Sign ``app`` and the code inside it ad hoc, deepest first, without
    its profiles and the entitlements only a profile grants; returns the
    entitlements it dropped."""
    code: list[Path] = []
    for folder, dirs, files in os.walk(app):
        here = Path(folder)
        code += [here / d for d in dirs if Path(d).suffix in _BUNDLES and not (here / d).is_symlink()]
        for file in files:
            path = here / file
            if file == "embedded.provisionprofile":
                path.unlink()
            elif not path.is_symlink():
                with path.open("rb") as head:
                    if head.read(4) in _MACHO:
                        code.append(path)
    dropped: set[str] = set()
    for item in [*sorted(code, key=lambda item: len(item.parts), reverse=True), app]:
        dropped |= _sign_ad_hoc(item)
    return sorted(dropped)


def _sign_ad_hoc(item: Path) -> set[str]:
    shown = subprocess.run(
        ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(item)],
        capture_output=True, check=False,
    )
    entitlements = plistlib.loads(shown.stdout) if shown.stdout.strip() else {}
    kept = {key: value for key, value in entitlements.items() if key.startswith("com.apple.security.")}
    # No hardened runtime: ad hoc code has no team ID, so library validation
    # would refuse the app's own frameworks.
    with tempfile.NamedTemporaryFile(suffix=".entitlements") as granted:
        granted.write(plistlib.dumps(kept))
        granted.flush()
        done = subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", "--entitlements", granted.name, str(item)],
            capture_output=True, text=True, check=False,
        )
    if done.returncode:
        raise MacOSError(f"signing {item} failed: {done.stderr.strip()}", code=ErrorCode.BAD_REQUEST)
    return set(entitlements) - set(kept)


def _named_files(root: Path, texts: Sequence[str]) -> dict[str, str]:
    """The files under ``root`` that ``texts`` name, each picture with the
    capture record beside it, by their path under ``root``; the guest keeps
    nothing between runs."""
    named: dict[str, str] = {}
    total = 0
    for text in texts:
        for found in re.finditer(re.escape(str(root)) + r"/([^\s'\"]+)", text):
            for relative in (found[1], f"{found[1]}.json"):
                path = (root / relative).resolve()
                if not (path.is_relative_to(root) and path.is_file()) or relative in named:
                    continue
                total += path.stat().st_size
                if total > _LARGEST_FILES:
                    raise MacOSError(
                        f"the files this call names pass {_LARGEST_FILES >> 20} MiB",
                        code=ErrorCode.BAD_REQUEST,
                    )
                named[relative] = base64.b64encode(path.read_bytes()).decode()
    return named


def _package_files(source: str) -> dict[str, Path]:
    """The files of ``source``'s package, by their path in a zip."""
    root = Path(source) / "src"
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted((root / "macos_harness").rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def _package_digest(package: Mapping[str, Path]) -> str:
    stamps = [
        (name, stat.st_mtime_ns, stat.st_size, stat.st_mode)
        for name, path in package.items()
        for stat in (path.stat(),)
    ]
    return hashlib.sha256(repr(stamps).encode()).hexdigest()


def _package_zip(package: Mapping[str, Path]) -> str:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, path in package.items():
            archive.write(path, name)
    return base64.b64encode(buffer.getvalue()).decode()


def _guest_home() -> Path:
    return config_dir() / "vm-guest"


def install_guest() -> Path:
    """Inside a guest: start `vm serve` from Terminal at every login, and
    now. Returns the launch agent's path."""
    _refuse_host("install-guest")
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


def _refuse_host(command: str) -> None:
    """`vm serve` runs any program its gateway sends; on a Mac that is not a
    VM, the gateway is a router."""
    done = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.hv_vmm_present"], capture_output=True, text=True, check=False
    )
    if done.stdout.strip() != "1":
        raise MacOSError(
            f"`vm {command}` runs only inside a guest; this Mac is not a virtual machine",
            code=ErrorCode.BAD_REQUEST,
        )


def _gateway() -> str | None:
    done = subprocess.run(
        ["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, check=False
    )
    found = re.search(r"gateway: (\S+)", done.stdout)
    return found[1] if found else None


class _NeedSource(Exception):
    """The guest lacks the package a run names; the host sends it again."""


def _guest_source(request: Mapping[str, object], sources: Path) -> Path | None:
    """The folder holding the package the host sent for ``request``, None
    for the installed one; unpacked from ``source_zip`` when new."""
    digest = request.get("source")
    if digest is None:
        return None
    if not (isinstance(digest, str) and _DIGEST.fullmatch(digest)):
        raise ValueError("a source is a sha256 digest")
    folder = sources / digest
    if folder.is_dir():
        os.utime(folder)
        return folder
    data = request.get("source_zip")
    if data is None:
        raise _NeedSource
    if not isinstance(data, str):
        raise TypeError("source_zip is base64 text")
    unpacking = sources / f"{digest}.{uuid.uuid4().hex}"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(data))) as archive:
        for member in archive.infolist():
            # extract() ignores stored modes; the bundled agent must stay executable.
            Path(archive.extract(member, unpacking)).chmod(member.external_attr >> 16 & 0o777)
    try:
        unpacking.rename(folder)
    except OSError:
        shutil.rmtree(unpacking)
    kept = sorted(
        (path for path in sources.iterdir() if _DIGEST.fullmatch(path.name)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in kept[_SOURCES_KEPT:]:
        shutil.rmtree(stale, ignore_errors=True)
    return folder


class _Warm:
    """One `macos-harness spare` broker the guest's runs share, one run at a
    time, so a run skips loading the harness. Its runs share one temporary
    directory, which a spare fixes when it loads."""

    def __init__(self, requests: Path) -> None:
        self.lock = threading.Lock()
        self.folder = requests / "warm"
        self._broker: subprocess.Popen[bytes] | None = None
        self._pipes = (-1, -1)
        self._env: dict[str, str] = {}

    def channel(self, env: Mapping[str, str]) -> tuple[dict[str, str], tuple[int, int]]:
        """``env`` with the broker's pipes named, and those pipes; a broker
        that died or loaded other settings is replaced."""
        broker = self._broker
        if broker is None or broker.poll() is not None or env != self._env:
            if broker is not None:
                broker.kill()
                broker.wait()
                for fd in self._pipes:
                    os.close(fd)
            replies, its_replies = os.pipe()
            its_requests, requests = os.pipe()
            self._broker = subprocess.Popen(
                [sys.executable, "-m", "macos_harness.cli", "spare"],
                stdin=its_requests, stdout=its_replies, env=dict(env),
            )
            os.close(its_requests)
            os.close(its_replies)
            self._pipes = (replies, requests)
            self._env = dict(env)
        return {**env, SPARE_ENV: f"{self._pipes[0]},{self._pipes[1]}"}, self._pipes


def _run_forwarded(
    request: Mapping[str, object], requests: Path, warm: _Warm, host: socket.socket
) -> _Ran:
    host_root, argv, stdin, inputs = (
        request.get("files"), request.get("argv"), request.get("stdin"), request.get("inputs", {})
    )
    if not (
        isinstance(host_root, str)
        and isinstance(argv, list)
        and all(isinstance(arg, str) for arg in argv)
        and (stdin is None or isinstance(stdin, str))
        and isinstance(inputs, dict)
        and all(isinstance(data, str) for data in inputs.values())
    ):
        raise ValueError("a run needs files, argv and stdin")
    env = dict(os.environ)
    package = _guest_source(request, requests.parent / "sources")
    if package is not None:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(package), env.get("PYTHONPATH"))))
    placed = [_placed(requests, relative, data) for relative, data in inputs.items()]
    if stdin is not None and "/dev/stdin" in argv:
        program = requests / f"{uuid.uuid4().hex}.py"
        program.write_text(stdin, encoding="utf-8")
        placed.append(program)
        argv = [str(program) if arg == "/dev/stdin" else arg for arg in argv]
        stdin = None
    pipes: tuple[int, ...] = ()
    shared = warm.lock.acquire(blocking=False)
    if shared:
        work = warm.folder
        env, pipes = warm.channel({**env, "TMPDIR": f"{work}/"})
    else:
        work = requests / uuid.uuid4().hex
        env["TMPDIR"] = f"{work}/"
    try:
        work.mkdir(exist_ok=True)
        return _ran_in(work, requests, host_root, argv, stdin, env, pipes, host, set(placed))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for path in placed:
            path.unlink(missing_ok=True)
        if shared:
            warm.lock.release()


def _placed(requests: Path, relative: object, data: str) -> Path:
    """Write a file the host sent where the run's rewritten paths name it."""
    target = (requests / str(relative)).resolve()
    if not target.is_relative_to(requests.resolve()) or target == requests.resolve():
        raise ValueError(f"an input outside the run's folder: {relative!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(data))
    return target


def _hung_up(host: socket.socket) -> bool:
    """Whether the host closed its readable connection; a host sends
    nothing after its request."""
    try:
        return host.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


def _ran_in(
    work: Path,
    requests: Path,
    host_root: str,
    argv: Sequence[str],
    stdin: str | None,
    env: Mapping[str, str],
    pipes: tuple[int, ...],
    host: socket.socket,
    placed: set[Path],
) -> _Ran:
    def inward(text: str) -> str:
        return text.replace(host_root, str(requests))

    def outward(text: str) -> str:
        return text.replace(str(requests), host_root)

    process = subprocess.Popen(
        [sys.executable, "-m", "macos_harness.cli", *(inward(arg) for arg in argv)],
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, pass_fds=pipes, start_new_session=True,
    )
    output: list[str] = []
    finished, finishing = os.pipe()

    def read() -> None:
        try:
            output.extend(process.communicate(inward(stdin) if stdin is not None else None))
        finally:
            os.close(finishing)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        if host in select.select([host, finished], [], [])[0] and _hung_up(host):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    finally:
        reader.join()
        os.close(finished)
    stdout, stderr = output
    files: dict[str, str] = {}
    left_behind: list[str] = []
    total = 0
    for path in sorted(work.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.resolve() in placed:
            continue
        size = path.stat().st_size
        if total + size > _LARGEST_FILES:
            left_behind.append(path.name)
            continue
        total += size
        files[path.relative_to(requests).as_posix()] = base64.b64encode(path.read_bytes()).decode()
    note = (
        f"macos-harness vm: not sent back, past {_LARGEST_FILES >> 20} MiB: {', '.join(left_behind)}\n"
        if left_behind
        else ""
    )
    return {
        "exit": process.returncode,
        "stdout": outward(stdout),
        "stderr": outward(stderr) + note,
        "files": files,
    }


def serve(port: int = GUEST_PORT) -> int:
    """Inside a guest: answer the host until killed. Only the guest's
    default gateway, which is the host, is answered."""
    _refuse_host("serve")
    requests = _guest_home() / "requests"
    shutil.rmtree(requests, ignore_errors=True)
    requests.mkdir(parents=True)
    (requests.parent / "sources").mkdir(exist_ok=True)
    warm = _Warm(requests)
    # A guest left without input sleeps its display within minutes, which
    # locks the screen and leaves Screen Recording no display to capture.
    subprocess.Popen(
        ["/usr/bin/caffeinate", "-d", "-i", "-w", str(os.getpid())],
        stdin=subprocess.DEVNULL, start_new_session=True,
    )

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            if self.client_address[0] != _gateway():
                return
            sock: socket.socket = self.request
            request = _receive(sock)
            reply: Mapping[str, object]
            try:
                if request.get("op") == "ping":
                    reply = {
                        "version": __version__,
                        "address": sock.getsockname()[0],
                        "sources": True,
                    }
                elif request.get("op") == "sync":
                    os.sync()
                    reply = {"synced": True}
                elif request.get("op") == "run":
                    try:
                        reply = _run_forwarded(request, requests, warm, sock)
                    except _NeedSource:
                        reply = {"need_source": True}
                else:
                    reply = {"error": f"unknown op {request.get('op')!r}"}
            except Exception as exc:  # noqa: BLE001
                reply = {"error": f"{type(exc).__name__}: {exc}"}
            with contextlib.suppress(OSError):
                _send(sock, reply)

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server(("0.0.0.0", port), Handler) as server:
        print(f"macos-harness {__version__} serving the host on port {port}", flush=True)
        server.serve_forever()
    return 0
