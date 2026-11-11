from __future__ import annotations

import base64
import json
import os
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


def _checkout(root: Path, version: str) -> Path:
    package = root / "src" / "macos_harness"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    return root


def _guest_copy(checkout: Path, sources: Path) -> Path:
    """What a guest holds after the host's two-step send for ``checkout``."""
    package = vm._package_files(str(checkout))
    request = {"source": vm._package_digest(package)}
    with pytest.raises(vm._NeedSource):
        vm._guest_source(request, sources)
    folder = vm._guest_source({**request, "source_zip": vm._package_zip(package)}, sources)
    assert folder is not None
    assert vm._guest_source(request, sources) == folder
    return folder


def test_a_changed_checkout_reaches_the_guest_as_a_new_copy(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    checkout = _checkout(tmp_path / "checkout", "one")
    first = _guest_copy(checkout, sources)
    _checkout(checkout, "two")

    second = _guest_copy(checkout, sources)

    assert second != first
    assert "two" in (second / "macos_harness" / "__init__.py").read_text(encoding="utf-8")


def test_a_bundled_agent_made_executable_reaches_the_guest_executable(tmp_path):
    sources = tmp_path / "sources"
    sources.mkdir()
    checkout = _checkout(tmp_path / "checkout", "one")
    agent = checkout / "src" / "macos_harness" / "bin" / "macos-harness-agent"
    agent.parent.mkdir()
    agent.write_text("#!/bin/sh\necho agent\n", encoding="utf-8")
    agent.chmod(0o644)
    _guest_copy(checkout, sources)
    agent.chmod(0o755)

    folder = _guest_copy(checkout, sources)

    copied = folder / "macos_harness" / "bin" / "macos-harness-agent"
    assert os.access(copied, os.X_OK)


def test_a_picture_from_an_earlier_call_reaches_the_guest_with_its_record(tmp_path):
    root = (tmp_path / "files").resolve()
    picture = root / "earlier" / "macos-harness-see.png"
    picture.parent.mkdir(parents=True)
    picture.write_bytes(b"png")
    Path(f"{picture}.json").write_text('{"scale": 2}', encoding="utf-8")
    requests = tmp_path / "requests"
    requests.mkdir()

    sent = vm._named_files(root, ["-c", f"mac.click(x=1, y=2, screenshot='{picture}')"])
    placed = [vm._placed(requests, relative, data) for relative, data in sent.items()]

    assert sorted(path.name for path in placed) == ["macos-harness-see.png", "macos-harness-see.png.json"]
    assert (requests / "earlier" / "macos-harness-see.png").read_bytes() == b"png"
    with pytest.raises(ValueError):
        vm._placed(requests, "../outside", base64.b64encode(b"x").decode())
