from __future__ import annotations

import contextlib
import os
import subprocess
import sys

import pytest

import macos_harness.capture as capture_module
import macos_harness.overlay as overlay_module


@pytest.fixture(autouse=True)
def own_capture_lock(monkeypatch, tmp_path):
    """Each test's process owns capture through a lock of its own, never
    the one a live harness on this Mac holds."""
    monkeypatch.setattr(capture_module, "user_temp_dir", lambda: str(tmp_path))
    monkeypatch.setattr(capture_module, "_capture_owner", None)
    yield
    if capture_module._capture_owner is not None:
        os.close(capture_module._capture_owner)


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
