from __future__ import annotations

import base64
import json
import socket
import struct
import threading
from pathlib import Path

import pytest

from macos_harness import vm
from macos_harness.errors import MacOSError


def _fake_tart(tmp_path: Path) -> Path:
    tart = tmp_path / "tart"
    tart.write_text(
        '#!/bin/sh\ncase "$1" in\n  list) echo "[]" ;;\n  ip) echo 127.0.0.1 ;;\nesac\n',
        encoding="utf-8",
    )
    tart.chmod(0o755)
    return tart


def _guest_sending(relative: str) -> threading.Thread:
    """A guest that answers one `run` with a file at ``relative``."""
    listener = socket.create_server(("127.0.0.1", vm.GUEST_PORT))

    def answer() -> None:
        with listener, listener.accept()[0] as sock:
            (length,) = struct.unpack(">I", sock.recv(4))
            while length:
                length -= len(sock.recv(length))
            reply = json.dumps({
                "exit": 0, "stdout": "", "stderr": "",
                "files": {relative: base64.b64encode(b"from the guest").decode()},
            }).encode()
            sock.sendall(struct.pack(">I", len(reply)) + reply)

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("relative", ["../escape.txt", "/private/var/tmp/escape.txt"])
def test_run_refuses_a_guest_file_outside_the_vm_folder(tmp_path, monkeypatch, relative):
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(vm.TART_ENV, str(_fake_tart(tmp_path)))
    vm.create("t1")
    guest = _guest_sending(relative)

    with pytest.raises(MacOSError, match="outside its folder"):
        vm.run("t1", ["-c", "pass"], None)

    guest.join(timeout=5)
    files = tmp_path / "home" / "vms" / "t1" / "files"
    assert not (files / relative).resolve().exists()
