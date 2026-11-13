from __future__ import annotations

import base64
import json
import os
import plistlib
import socket
import struct
import subprocess
import sys
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


def _stdlib_guest(home: Path) -> socket.socket:
    """A guest that runs each forwarded program with plain Python, its home
    at ``home``; closing the returned listener stops it."""
    listener = socket.create_server(("127.0.0.1", vm.GUEST_PORT))
    requests = home / "requests"
    requests.mkdir(parents=True)

    def serve() -> None:
        while True:
            try:
                sock = listener.accept()[0]
            except OSError:
                return
            with sock:
                request = vm._receive(sock)
                for relative, data in request["inputs"].items():
                    vm._placed(requests, relative, data)
                done = subprocess.run(
                    [sys.executable, "-"],
                    input=request["stdin"].replace(request["files"], str(requests)),
                    capture_output=True, text=True, check=False, env={**os.environ, "HOME": str(home)},
                )
                vm._send(sock, {"exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr, "files": {}})

    threading.Thread(target=serve, daemon=True).start()
    return listener


@pytest.fixture
def guest_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(vm.TART_ENV, str(_fake_tart(tmp_path)))
    vm.create("t1")
    home = tmp_path / "guest"
    with _stdlib_guest(home):
        yield home


def test_put_lands_a_folder_whole_across_several_chunks(tmp_path, guest_home):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "data.bin").write_bytes(os.urandom(vm._PUT_CHUNK + 4096))
    (kit / "tool").write_text("#!/bin/sh\necho tool\n", encoding="utf-8")
    (kit / "tool").chmod(0o755)
    (kit / "link").symlink_to("data.bin")

    put = vm.put("t1", kit, to="~/landing")

    landed = guest_home / "landing" / "kit"
    assert put["path"] == str(landed)
    assert put["bytes"] > vm._PUT_CHUNK
    assert (landed / "data.bin").read_bytes() == (kit / "data.bin").read_bytes()
    assert os.access(landed / "tool", os.X_OK)
    assert os.readlink(landed / "link") == "data.bin"
    assert not list((guest_home / "Library" / "Caches" / "macos-harness-put").iterdir())
    assert not list((tmp_path / "home" / "vms" / "t1" / "files").glob("put-*"))


def test_put_lands_a_file_itself_in_the_guest_home(tmp_path, guest_home):
    note = tmp_path / "deep" / "note.txt"
    note.parent.mkdir()
    note.write_text("hello", encoding="utf-8")

    put = vm.put("t1", note)

    assert put["path"] == str(guest_home / "note.txt")
    assert (guest_home / "note.txt").read_text(encoding="utf-8") == "hello"


def _signed_bundle(bundle: Path, entitlements: dict[str, object], work: Path) -> None:
    """A bundle whose executable prints what its own library returns, signed
    with the hardened runtime and ``entitlements``, as Xcode signs one."""
    frameworks = bundle / "Contents" / "Frameworks"
    executable = bundle / "Contents" / "MacOS" / bundle.stem
    frameworks.mkdir(parents=True)
    executable.parent.mkdir()
    source = work / bundle.stem
    source.mkdir()
    (source / "lib.c").write_text("int lib(void) { return 42; }\n", encoding="utf-8")
    (source / "main.c").write_text(
        '#include <stdio.h>\nint lib(void);\nint main(void) { printf("%d\\n", lib()); return 0; }\n',
        encoding="utf-8",
    )
    library = frameworks / "liblib.dylib"
    subprocess.run(
        ["clang", "-dynamiclib", "-o", str(library), "-install_name", "@rpath/liblib.dylib", str(source / "lib.c")],
        check=True,
    )
    subprocess.run(
        ["clang", "-o", str(executable), str(source / "main.c"), str(library),
         "-Wl,-rpath,@executable_path/../Frameworks"],
        check=True,
    )
    (bundle / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": bundle.stem, "CFBundleIdentifier": f"test.{bundle.stem}"})
    )
    granted = source / "granted.entitlements"
    granted.write_bytes(plistlib.dumps(entitlements))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(library)], check=True, capture_output=True)
    subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", "--options", "runtime",
         "--entitlements", str(granted), str(bundle)],
        check=True, capture_output=True,
    )


def _entitlements(code: Path) -> dict[str, object]:
    shown = subprocess.run(
        ["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(code)], capture_output=True, check=True
    )
    return plistlib.loads(shown.stdout)


def test_an_app_provisioned_elsewhere_is_resigned_to_run_without_profile_only_entitlements(tmp_path):
    entitlements = {"com.apple.application-identifier": "T.test.Probe", "com.apple.security.device.camera": True}
    app = tmp_path / "Probe.app"
    _signed_bundle(app / "Contents" / "Helpers" / "Core.app", entitlements, tmp_path)
    (app / "Contents" / "embedded.provisionprofile").write_bytes(b"profile")
    _signed_bundle(app, entitlements, tmp_path)

    dropped = vm._resign(app)

    assert dropped == ["com.apple.application-identifier"]
    assert not vm._profiles(app)
    for code in (app, app / "Contents" / "Helpers" / "Core.app"):
        assert _entitlements(code) == {"com.apple.security.device.camera": True}
        ran = subprocess.run([str(code / "Contents" / "MacOS" / code.stem)], capture_output=True, text=True, check=False)
        assert ran.stdout == "42\n", ran.stderr
    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def test_put_refuses_to_replace_a_build_running_in_the_guest(tmp_path, guest_home):
    running = guest_home / "C++ Lab (Beta).kit"
    running.mkdir()
    process = subprocess.Popen([str(running / "sleep"), "30"], executable="/bin/sleep")
    build = tmp_path / "C++ Lab (Beta).kit"
    build.mkdir()
    (build / "new").write_text("new build", encoding="utf-8")
    try:
        with pytest.raises(MacOSError, match="is running in the guest"):
            vm.put("t1", build)
    finally:
        process.kill()
        process.wait()

    assert running.is_dir()
    assert not (running / "new").exists()
