"""Contract tests for ``mac.do.steps``: what it sends, and what it never sends."""

from __future__ import annotations

import shutil

import pytest
from test_ops import FakeHost

from macos_harness.errors import ErrorCode, MacOSError
from macos_harness.ops import Operations
from macos_harness.state import AppState


class _StepsHost(FakeHost):
    """`FakeHost` with the focused-window reads a steps run makes around its steps."""

    def __init__(self) -> None:
        super().__init__()
        self._elements: dict[int, object] = {}

    def state(self, app: str | int) -> AppState:
        lines = {1: f'[1] text "{len(self.press_calls)} pressed"'}
        return AppState(
            "", {}, complete=True, lines=lines, seen={}, shows=lines, on_show=None,
            owner=lambda _: None, latest=lambda: (lines, True), window_id=5,
        )

    def _state_window(self, pid: int, window_id: int | None) -> object:
        return object()

    def _ax_window_id(self, window: object) -> int | None:
        return None


def test_steps_sends_nothing_after_a_step_whose_dispatch_is_uncertain() -> None:
    host = _StepsHost()
    host.press_results.extend([dict(host.match), MacOSError("press timed out", code=ErrorCode.TIMEOUT)])

    result = Operations(host, _sleep=lambda _seconds: None).steps(
        [{"op": "press", "text": "Save"}] * 3, app="Demo"
    )
    shutil.rmtree(result.strip[-1].parent)

    assert (result.status, result.stopped_at, len(host.press_calls)) == ("stopped", 1, 2)


def test_steps_checks_every_step_before_sending_the_first() -> None:
    host = _StepsHost()
    operations = Operations(host, _sleep=lambda _seconds: None)

    with pytest.raises(MacOSError, match=r"steps\[1\] does not fit press"):
        operations.steps([{"op": "press", "text": "Save"}, {"op": "press", "txt": "Save"}], app="Demo")

    assert host.press_calls == []
