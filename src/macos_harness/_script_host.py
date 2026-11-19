"""Runs an AppleScript or JXA script where it cannot press a key by itself.

`mac.script` used to hand the script to ``osascript``, and whatever the
script sent went straight to the app: a script could bring a password
prompt forward and type into it, and no reading of the script's text could
tell, since the keystroke can be assembled at run time, sit inside
``run script`` or be sent by a process ``do shell script`` starts.

So the script runs in a child process under a sandbox profile, and that
changes what it can reach rather than what it may say:

* macOS refuses an Apple event sent by a sandboxed process, whatever app
  it addresses. The child's own events are instead handed, one at a time,
  to the harness over a pipe, and the harness decides and sends them.
  Events from anywhere else in the sandbox (``run script`` text, an
  ``osascript`` started by ``do shell script``) have no such way out.
* The profile denies the window server connection that ``CGEventPost``
  needs, the accessibility port that sets a field's value, and the HID
  parameter client, for the child and everything it starts.

This file is both halves: run as a program it is the sandboxed child;
imported, it is the side the harness runs (`run`). It imports nothing
outside the standard library so the child starts fast and needs nothing
the sandbox denies.
"""

from __future__ import annotations

import ctypes
import functools
import json
import os
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int16,
    c_int32,
    c_long,
    c_uint32,
    c_void_p,
)
from dataclasses import dataclass

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny appleevent-send)
(deny mach-lookup (global-name "com.apple.windowserver.active"))
(deny mach-lookup (local-name "com.apple.axserver"))
(deny iokit-open (iokit-user-client-class "IOHIDParamUserClient"))
"""
LANGUAGES = {"applescript": "ascr", "javascript": "jscr"}
SYSTEM_EVENTS = "com.apple.systemevents"
NOT_PERMITTED = -1743  # errAEEventNotPermitted
_WAIT_REPLY = 0x3
_REPLY_MASK = 0x3
_HEADER = struct.Struct(">cI")
_SEND = struct.Struct(">ihi")


class AEDesc(Structure):
    _fields_ = [("descriptorType", c_uint32), ("dataHandle", c_void_p)]


def _code(text: str) -> int:
    return struct.unpack(">I", text.encode("mac_roman"))[0]


def _text(code: int) -> str:
    return struct.pack(">I", code & 0xFFFFFFFF).decode("mac_roman", "replace")


@functools.cache
def _carbon() -> ctypes.CDLL:
    lib = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    desc = POINTER(AEDesc)
    lib.AECreateDesc.argtypes = [c_uint32, c_char_p, c_long, desc]
    lib.AEDisposeDesc.argtypes = [desc]
    lib.AEGetDescDataSize.restype = c_long
    lib.AEGetDescDataSize.argtypes = [desc]
    lib.AEGetDescData.argtypes = [desc, c_void_p, c_long]
    lib.AECoerceDesc.argtypes = [desc, c_uint32, desc]
    lib.AEGetAttributeDesc.argtypes = [desc, c_uint32, c_uint32, desc]
    lib.AEGetParamDesc.argtypes = [desc, c_uint32, c_uint32, desc]
    lib.AECountItems.argtypes = [desc, POINTER(c_long)]
    lib.AEGetNthDesc.argtypes = [desc, c_long, c_uint32, POINTER(c_uint32), desc]
    lib.AESizeOfFlattenedDesc.restype = c_long
    lib.AESizeOfFlattenedDesc.argtypes = [desc]
    lib.AEFlattenDesc.argtypes = [desc, c_void_p, c_long, POINTER(c_long)]
    lib.AEUnflattenDesc.argtypes = [c_char_p, desc]
    lib.AESend.restype = c_int16
    lib.AESend.argtypes = [desc, desc, c_int32, c_int16, c_int32, c_void_p, c_void_p]
    lib.GetProcessPID.argtypes = [c_void_p, POINTER(c_int32)]
    lib.OpenDefaultComponent.restype = c_void_p
    lib.OpenDefaultComponent.argtypes = [c_uint32, c_uint32]
    lib.OSACompile.argtypes = [c_void_p, desc, c_int32, POINTER(c_uint32)]
    lib.OSAExecute.argtypes = [c_void_p, c_uint32, c_uint32, c_int32, POINTER(c_uint32)]
    lib.OSADisplay.argtypes = [c_void_p, c_uint32, c_uint32, c_int32, desc]
    lib.OSAScriptError.argtypes = [c_void_p, c_uint32, c_uint32, desc]
    lib.OSASetSendProc.argtypes = [c_void_p, c_void_p, c_void_p]
    return lib


def _data(desc: AEDesc) -> bytes:
    lib = _carbon()
    size = lib.AEGetDescDataSize(byref(desc))
    buffer = ctypes.create_string_buffer(max(size, 1))
    lib.AEGetDescData(byref(desc), buffer, size)
    return buffer.raw[:size]


def _flatten(desc: AEDesc) -> bytes:
    lib = _carbon()
    size = lib.AESizeOfFlattenedDesc(byref(desc))
    buffer = ctypes.create_string_buffer(max(size, 1))
    actual = c_long()
    if lib.AEFlattenDesc(byref(desc), buffer, size, byref(actual)) != 0:
        return b""
    return buffer.raw[: actual.value]


def _coerced(desc: AEDesc, kind: str) -> bytes | None:
    """``desc``'s data as ``kind``, None where it does not convert."""
    lib = _carbon()
    out = AEDesc()
    if lib.AECoerceDesc(byref(desc), _code(kind), byref(out)) != 0:
        return None
    try:
        return _data(out)
    finally:
        lib.AEDisposeDesc(byref(out))


def _items(desc: AEDesc, kind: str) -> list[bytes] | None:
    """Each item of a list descriptor (or the one value) as ``kind``."""
    lib = _carbon()
    if _text(desc.descriptorType) != "list":
        one = _coerced(desc, kind)
        return None if one is None else [one]
    count = c_long()
    if lib.AECountItems(byref(desc), byref(count)) != 0:
        return None
    found = []
    for index in range(1, count.value + 1):
        item, keyword = AEDesc(), c_uint32()
        if lib.AEGetNthDesc(byref(desc), index, _code(kind), byref(keyword), byref(item)) != 0:
            return None
        found.append(_data(item))
        lib.AEDisposeDesc(byref(item))
    return found


def _attribute(event: AEDesc, keyword: str) -> AEDesc | None:
    out = AEDesc()
    if _carbon().AEGetAttributeDesc(byref(event), _code(keyword), _code("****"), byref(out)) != 0:
        return None
    return out


def _parameter(event: AEDesc, keyword: str) -> AEDesc | None:
    out = AEDesc()
    if _carbon().AEGetParamDesc(byref(event), _code(keyword), _code("****"), byref(out)) != 0:
        return None
    return out


def _four(event: AEDesc, keyword: str) -> str:
    desc = _attribute(event, keyword)
    if desc is None:
        return ""
    data = _data(desc)
    _carbon().AEDisposeDesc(byref(desc))
    return _text(struct.unpack("=I", data[:4])[0]) if len(data) >= 4 else ""


def _write(fd: int, kind: bytes, payload: bytes) -> None:
    data = _HEADER.pack(kind, len(payload)) + payload
    while data:
        data = data[os.write(fd, data):]


def _read_exactly(fd: int, size: int, deadline: float | None = None) -> bytes:
    data = b""
    while len(data) < size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def _read(fd: int, deadline: float | None = None) -> tuple[bytes, bytes]:
    kind, size = _HEADER.unpack(_read_exactly(fd, _HEADER.size, deadline))
    return kind, _read_exactly(fd, size, deadline)


# --- the harness's side ----------------------------------------------------


@dataclass(frozen=True)
class ScriptEvent:
    """One Apple event the script asked to send."""

    event_class: str
    event_id: str
    target_pid: int | None
    target_bundle: str | None
    text: str | None = None
    keycodes: tuple[int, ...] | None = None
    modifiers: tuple[str, ...] | None = ()

    @property
    def name(self) -> str:
        return f"{self.event_class}/{self.event_id}"


@dataclass(frozen=True)
class ScriptResult:
    ok: bool
    text: str
    number: int = 0


def _describe(event: AEDesc) -> ScriptEvent:
    lib = _carbon()
    pid: int | None = None
    bundle: str | None = None
    address = _attribute(event, "addr")
    if address is not None:
        kind, data = _text(address.descriptorType), _data(address)
        if kind == "psn " and len(data) == 8:
            found = c_int32()
            if lib.GetProcessPID(data, byref(found)) == 0:
                pid = found.value
        elif kind == "kpid" and len(data) == 4:
            pid = struct.unpack("=i", data)[0]
        elif kind == "bund":
            bundle = data.decode("utf-8", "replace")
        lib.AEDisposeDesc(byref(address))
    event_class, event_id = _four(event, "evcl"), _four(event, "evid")
    text: str | None = None
    keycodes: tuple[int, ...] | None = None
    modifiers: tuple[str, ...] | None = ()
    direct = _parameter(event, "----")
    if direct is not None:
        if (event_class, event_id) == ("prcs", "kprs"):
            raw = _coerced(direct, "utxt")
            text = None if raw is None else raw.decode("utf-16")
        elif (event_class, event_id) == ("prcs", "kcod"):
            codes = _items(direct, "long")
            keycodes = None if codes is None else tuple(
                struct.unpack("=i", code)[0] for code in codes
            )
        lib.AEDisposeDesc(byref(direct))
    using = _parameter(event, "faal")
    if using is not None:
        names = _items(using, "enum")
        modifiers = None if names is None else tuple(
            _text(struct.unpack("=I", name)[0]) for name in names
        )
        lib.AEDisposeDesc(byref(using))
    return ScriptEvent(event_class, event_id, pid, bundle, text, keycodes, modifiers)


def _forward(event: AEDesc, mode: int, priority: int, ticks: int) -> tuple[int, bytes]:
    """Send ``event`` and wait for its answer. The script may have asked
    not to wait (``ignoring application responses``); it waits anyway, so
    nothing it asked an app to show is still on its way when its next
    event is judged."""
    lib = _carbon()
    reply = AEDesc()
    status = lib.AESend(
        byref(event), byref(reply), (mode & ~_REPLY_MASK) | _WAIT_REPLY, priority, ticks, None, None
    )
    if status != 0:
        return status, b""
    try:
        return 0, _flatten(reply)
    finally:
        lib.AEDisposeDesc(byref(reply))


def command(language: str, read_fd: int, write_fd: int) -> list[str]:
    return [
        SANDBOX_EXEC, "-p", SANDBOX_PROFILE,
        sys.executable, "-I", os.path.abspath(__file__), language, str(read_fd), str(write_fd),
    ]


def run(
    source: str,
    language: str,
    timeout: float,
    decide: Callable[[ScriptEvent], bool],
) -> ScriptResult:
    """Run ``source`` in the sandboxed child and return what it produced.

    Each Apple event the script sends reaches ``decide`` first: True sends
    it on, False means ``decide`` did the event's work itself, and an
    exception stops the script there and propagates. `TimeoutError` after
    ``timeout`` seconds, the child and everything it started killed.
    """
    if language.casefold() not in LANGUAGES:
        raise ValueError(language)
    deadline = time.monotonic() + timeout
    to_child_read, to_child = os.pipe()
    from_child, from_child_write = os.pipe()
    try:
        child = subprocess.Popen(
            command(language, to_child_read, from_child_write),
            pass_fds=(to_child_read, from_child_write),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    finally:
        os.close(to_child_read)
        os.close(from_child_write)
    try:
        _write(to_child, b"S", source.encode())
        while True:
            kind, payload = _read(from_child, deadline)
            if kind == b"R":
                result = json.loads(payload)
                return ScriptResult(bool(result["ok"]), str(result["text"]), int(result["number"]))
            mode, priority, asked = _SEND.unpack_from(payload)
            event = AEDesc()
            if _carbon().AEUnflattenDesc(payload[_SEND.size:], byref(event)) != 0:
                _write(to_child, b"A", struct.pack(">i", NOT_PERMITTED))
                continue
            try:
                if decide(_describe(event)):
                    ticks = max(1, int((deadline - time.monotonic()) * 60))
                    if asked > 0:  # the script's own ``with timeout``
                        ticks = min(ticks, asked)
                    status, reply = _forward(event, mode, priority, ticks)
                else:
                    status, reply = 0, b""
            finally:
                _carbon().AEDisposeDesc(byref(event))
            _write(to_child, b"A", struct.pack(">i", status) + reply)
    except EOFError:
        child.wait()
        error = (child.stderr.read() if child.stderr else b"").decode("utf-8", "replace").strip()
        return ScriptResult(False, error or "the script's process ended without a result", 1)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.wait()
        if child.stderr:
            child.stderr.close()
        os.close(to_child)
        os.close(from_child)


# --- the sandboxed child ---------------------------------------------------


def _own_address(address: AEDesc | None) -> bool:
    """Whether an event is addressed to this process: a scripting addition
    such as ``do shell script`` runs here and needs no one's leave."""
    if address is None:
        return False
    kind, data = _text(address.descriptorType), _data(address)
    if kind == "psn " and len(data) == 8:
        return struct.unpack("=II", data) == (0, 2)
    if kind == "kpid" and len(data) == 4:
        return struct.unpack("=i", data)[0] == os.getpid()
    return False


def _child(language: str, read_fd: int, write_fd: int) -> None:
    for fd in (read_fd, write_fd):
        os.set_inheritable(fd, False)
    lib = _carbon()
    send_type = ctypes.CFUNCTYPE(
        c_int16, POINTER(AEDesc), POINTER(AEDesc), c_int32, c_int16, c_int32,
        c_void_p, c_void_p, c_void_p,
    )

    @send_type
    def send(event, reply, mode, priority, ticks, idle, filter_, refcon):  # type: ignore[no-untyped-def]
        address = _attribute(event.contents, "addr")
        try:
            if _own_address(address):
                return lib.AESend(event, reply, mode, priority, ticks, idle, filter_)
        finally:
            if address is not None:
                lib.AEDisposeDesc(byref(address))
        _write(write_fd, b"E", _SEND.pack(mode, priority, ticks) + _flatten(event.contents))
        _, payload = _read(read_fd)
        status = struct.unpack_from(">i", payload)[0]
        answer = payload[4:]
        if status != 0 or not answer or lib.AEUnflattenDesc(answer, reply) != 0:
            lib.AECreateDesc(_code("null"), None, 0, reply)
        return status

    _, source = _read(read_fd)
    component = lib.OpenDefaultComponent(_code("osa "), _code(LANGUAGES[language.casefold()]))
    lib.OSASetSendProc(component, send, None)

    def failure() -> dict[str, object]:
        message, number = AEDesc(), AEDesc()
        lib.OSAScriptError(component, _code("errs"), _code("utf8"), byref(message))
        lib.OSAScriptError(component, _code("errn"), _code("long"), byref(number))
        raw = _data(number)
        code = struct.unpack("=i", raw)[0] if len(raw) == 4 else 1
        return {
            "ok": False,
            "text": f"execution error: {_data(message).decode('utf-8', 'replace')} ({code})",
            "number": code,
        }

    text = AEDesc()
    lib.AECreateDesc(_code("utf8"), source, len(source), byref(text))
    script, value = c_uint32(0), c_uint32(0)
    if lib.OSACompile(component, byref(text), 0, byref(script)) != 0 or lib.OSAExecute(
        component, script.value, 0, 0, byref(value)
    ) != 0:
        result = failure()
    else:
        shown = AEDesc()
        printed = b""
        if value.value and lib.OSADisplay(
            component, value.value, _code("utf8"), 0x8, byref(shown)
        ) == 0:
            printed = _data(shown)
        result = {"ok": True, "text": printed.decode("utf-8", "replace"), "number": 0}
    _write(write_fd, b"R", json.dumps(result).encode())


if __name__ == "__main__":
    _child(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
