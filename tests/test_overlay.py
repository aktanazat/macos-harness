from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import weakref

import pytest

import macos_harness.overlay as overlay_module
from macos_harness.macos import MacOS
from macos_harness.overlay import IDLE_HIDE_SECONDS, LivePointerOverlay
from macos_harness.pointer import POINTER_HOTSPOT, POINTER_PRESS_SCALE, pointer_points


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


class _FakeStdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, value: str) -> int:
        self.lines.append(value)
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.returncode = None
        self.pid = 4321

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float) -> int:
        assert self.stdin.closed
        self.returncode = 0
        return self.returncode


def test_overlay_uses_tiny_json_protocol_and_closes_with_parent(monkeypatch) -> None:
    process = _FakeProcess()
    launches = []

    def fake_popen(argv, **kwargs):
        launches.append((argv, kwargs))
        return process

    monkeypatch.setattr(overlay_module.subprocess, "Popen", fake_popen)
    overlay = LivePointerOverlay()

    overlay.move(100, 200, duration=0.25)
    overlay.click()
    overlay.hide()
    overlay.show(300, 400)
    overlay.close()

    commands = [json.loads(line) for line in process.stdin.lines]
    assert [command["cmd"] for command in commands] == [
        "move",
        "click",
        "hide",
        "show",
        "quit",
    ]
    assert commands[0] == {
        "cmd": "move",
        "x": 100.0,
        "y": 200.0,
        "duration": 0.25,
    }
    assert launches[0][0][-3:] == ["-m", "macos_harness.overlay", "--helper"]
    assert process.stdin.closed


def test_pointer_reads_hidden_once_the_idle_timeout_passes(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(overlay_module, "time", clock)
    overlay = LivePointerOverlay()
    monkeypatch.setattr(overlay, "_send", lambda payload, **kwargs: None)

    overlay.show(10, 20)
    assert overlay.visible is True

    clock.now += IDLE_HIDE_SECONDS - 0.5
    overlay.click()
    clock.now += IDLE_HIDE_SECONDS - 0.5
    assert overlay.visible is True

    clock.now += 0.5
    assert overlay.visible is False


def test_hide_drops_pointer_updates_until_the_next_show(monkeypatch) -> None:
    sent = []
    overlay = LivePointerOverlay()
    monkeypatch.setattr(overlay, "_send", lambda payload, **kwargs: sent.append(payload["cmd"]))

    overlay.show(1, 2)
    overlay.hide()
    overlay.move(3, 4)
    overlay.click()

    assert overlay.visible is False
    assert sent == ["show"]

    overlay.show(5, 6)
    assert overlay.visible is True
    assert sent == ["show", "show"]


def test_pressed_pointer_scales_around_the_hotspot() -> None:
    normal = pointer_points()
    pressed = pointer_points(pressed=True)
    hot_x, hot_y = POINTER_HOTSPOT

    assert pressed[0] == POINTER_HOTSPOT
    for (x, y), (pressed_x, pressed_y) in zip(normal, pressed, strict=True):
        assert pressed_x - hot_x == pytest.approx((x - hot_x) * POINTER_PRESS_SCALE)
        assert pressed_y - hot_y == pytest.approx((y - hot_y) * POINTER_PRESS_SCALE)


def test_close_reaps_the_pointer_and_reports_it_hidden(pointer_children) -> None:
    overlay = LivePointerOverlay()
    try:
        overlay.show(10, 20)
        assert overlay.running

        overlay.close()

        assert pointer_children[0].returncode == 0
        assert overlay.running is False
        assert overlay.visible is False
    finally:
        overlay.close()


def test_dropping_an_overlay_reaps_its_child_without_interpreter_exit(pointer_children) -> None:
    overlay = LivePointerOverlay()
    overlay.show(10, 20)
    reference = weakref.ref(overlay)

    del overlay

    assert reference() is None
    assert pointer_children[0].returncode == 0


def test_closing_one_overlay_preserves_the_other_callers_pointer(pointer_children) -> None:
    first, second = LivePointerOverlay(), LivePointerOverlay()
    try:
        first.show(10, 20)
        second.show(30, 40)

        first.close()

        assert pointer_children[0].returncode == 0
        assert second.running
        assert pointer_children[1].returncode is None
    finally:
        first.close()
        second.close()


def test_closed_pointer_is_recreated_only_on_an_explicit_show(pointer_children) -> None:
    overlay = LivePointerOverlay()
    try:
        overlay.show(10, 20)
        overlay.close()
        overlay.close()
        assert len(pointer_children) == 1
        assert pointer_children[0].returncode == 0
        assert overlay.running is False

        overlay.show(30, 40)

        assert len(pointer_children) == 2
        assert overlay.running
    finally:
        overlay.close()
    assert pointer_children[1].returncode == 0


@pytest.mark.parametrize("error", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_macos_context_exit_reaps_its_disposable_pointer(pointer_children, error) -> None:
    expected = contextlib.nullcontext() if error is None else pytest.raises(error, match="stop")
    with expected, MacOS(backend="python") as mac:
        mac.move(10, 20, coordinate_space="screen")
        if error is not None:
            raise error("stop")

    assert pointer_children[0].returncode == 0


def test_dropping_macos_reaps_its_pointer_without_interpreter_exit(pointer_children) -> None:
    mac = MacOS(backend="python")
    mac.move(10, 20, coordinate_space="screen")
    reference = weakref.ref(mac)

    del mac

    assert reference() is None
    assert pointer_children[0].returncode == 0


def test_macos_close_also_reaps_a_pointer_explicitly_recreated_after_close(pointer_children) -> None:
    mac = MacOS(backend="python")
    try:
        mac.move(10, 20, coordinate_space="screen")
        mac.close()
        mac.show_pointer()
        assert len(pointer_children) == 2

        mac.close()

        assert [process.returncode for process in pointer_children] == [0, 0]
    finally:
        mac.close()


def test_forked_copy_cannot_reuse_the_parents_pointer() -> None:
    program = """
import json,os,subprocess,sys
from macos_harness.overlay import LivePointerOverlay
popen = subprocess.Popen
children = []
def spawn(argv, **kwargs):
    kwargs["stdout"] = subprocess.PIPE
    process = popen([sys.executable, "-u", "-c",
        "import json,sys\\n"
        "for line in sys.stdin:\\n"
        "    command=json.loads(line)['cmd']\\n"
        "    print(command,flush=True)\\n"
        "    if command=='quit': break\\n"], **kwargs)
    children.append(process)
    return process
subprocess.Popen = spawn
overlay = LivePointerOverlay()
try:
    overlay.show(10, 20)
    commands = [children[0].stdout.readline().strip()]
    pid = os.fork()
    if pid == 0:
        try:
            overlay.show(30, 40)
        except RuntimeError:
            pass
        else:
            os._exit(2)
        overlay.close()
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert status == 0
    overlay.move(30, 40)
    commands.append(children[0].stdout.readline().strip())
    print(json.dumps({"commands": commands, "helpers_created": len(children)}))
finally:
    overlay.close()
    for process in children:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stdout.close()
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=10, check=False,
        env={**os.environ, "DO_NOT_TRACK": "1"},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "commands": ["show", "move"], "helpers_created": 1,
    }


@pytest.mark.parametrize(
    ("environment", "explicit", "expected"),
    [
        ("research", None, "research"),
        ("research", "writer", "writer"),
        ("", None, None),
        (" ", "writer", "writer"),
    ],
)
def test_pointer_identity_uses_environment_unless_the_caller_names_it(
    monkeypatch, pointer_children, environment, explicit, expected
) -> None:
    monkeypatch.setenv("MACOS_HARNESS_AGENT_ID", environment)
    with MacOS(agent_id=explicit) as mac:
        pointer = mac.move(10, 20, coordinate_space="screen")

    assert pointer.get("agent_id") == expected
    assert pointer_children[0].returncode == 0
