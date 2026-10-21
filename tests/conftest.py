from __future__ import annotations

import contextlib
import subprocess
import sys

import pytest

import macos_harness.overlay as overlay_module


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
