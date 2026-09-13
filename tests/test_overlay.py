from __future__ import annotations

import json

import pytest

import macos_harness.overlay as overlay_module
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

    def poll(self) -> None:
        return None


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
