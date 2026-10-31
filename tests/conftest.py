from __future__ import annotations

import contextlib
import os
import subprocess
import sys

import pytest

import macos_harness._input_lock as input_lock_module
import macos_harness.capture as capture_module
import macos_harness.overlay as overlay_module


@pytest.fixture(autouse=True)
def own_capture_lock(monkeypatch, tmp_path):
    """Each test's process owns capture and input locks of its own, never
    the ones a live harness on this Mac holds."""
    monkeypatch.setattr(capture_module, "user_temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(input_lock_module, "user_temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(capture_module, "_capture_owner", None)
    yield
    if capture_module._capture_owner is not None:
        os.close(capture_module._capture_owner)


_HOLD_INPUT = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)

_TRY_INPUT = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "try:\n"
    "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "except BlockingIOError:\n"
    "    print('busy')\n"
    "else:\n"
    "    print('free')\n"
)


@pytest.fixture
def hold_app_input(tmp_path):
    """Hold an app's input from another process, as a second harness run
    sending that app input does; closing the holder's stdin lets go."""
    holders: list[subprocess.Popen[str]] = []

    def hold(pid: int) -> subprocess.Popen[str]:
        holder = subprocess.Popen(
            [sys.executable, "-c", _HOLD_INPUT, str(tmp_path / f"macos-harness-input-{pid}.lock")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        holders.append(holder)
        assert holder.stdout is not None
        assert holder.stdout.readline() == "held\n"
        return holder

    yield hold
    for holder in holders:
        assert holder.stdin is not None
        with contextlib.suppress(BrokenPipeError, OSError):
            holder.stdin.close()
        holder.wait(timeout=5)
        assert holder.stdout is not None
        holder.stdout.close()


@pytest.fixture
def app_input_free(tmp_path):
    """Whether another harness run could send an app input right now."""

    def free(pid: int) -> bool:
        probe = subprocess.run(
            [sys.executable, "-c", _TRY_INPUT, str(tmp_path / f"macos-harness-input-{pid}.lock")],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return probe.stdout == "free\n"

    return free


@pytest.fixture
def pointer_children(monkeypatch):
    """Exercise real pipe/child cleanup without creating native UI."""
    children: list[subprocess.Popen[str]] = []
    popen = subprocess.Popen
    program = (
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    if json.loads(line).get('cmd') == 'quit':\n"
        "        break\n"
    )

    def spawn(argv, **kwargs):
        if argv[-3:] != ["-m", "macos_harness.overlay", "--helper"]:
            return popen(argv, **kwargs)
        process = popen([sys.executable, "-c", program], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(overlay_module.subprocess, "Popen", spawn)
    try:
        yield children
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            if process.stdin is not None:
                with contextlib.suppress(BrokenPipeError, OSError):
                    process.stdin.close()
