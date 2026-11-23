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
    assert isinstance(result.strip, tuple)
    shutil.rmtree(result.strip[-1].parent)

    assert (result.status, result.stopped_at, len(host.press_calls)) == ("stopped", 1, 2)


def test_steps_returns_what_ran_when_a_later_step_fails_with_a_plain_python_error() -> None:
    host = _StepsHost()

    result = Operations(host, _sleep=lambda _seconds: None).steps(
        [{"op": "press", "text": "1"}, {"op": "type", "text": 5}, {"op": "press", "text": "2"}], app="Demo"
    )
    assert isinstance(result.strip, tuple)
    shutil.rmtree(result.strip[-1].parent)

    assert (result.status, result.stopped_at, len(host.press_calls)) == ("stopped", 1, 1)


def test_steps_matches_every_step_to_its_verb_before_sending_the_first() -> None:
    host = _StepsHost()
    operations = Operations(host, _sleep=lambda _seconds: None)

    with pytest.raises(MacOSError, match=r"steps\[1\] does not fit press"):
        operations.steps([{"op": "press", "text": "Save"}, {"op": "press", "txt": "Save"}], app="Demo")

    assert host.press_calls == []


def test_steps_stops_at_a_type_step_refused_under_secure_input() -> None:
    host = _StepsHost()
    host.secret_refusal = MacOSError(
        "type refuses to send text while the app it targets holds secure keyboard entry",
        code=ErrorCode.UNSUPPORTED_OP,
        details={"reason": "secure_input_in_app", "operation": "type", "pid": 41, "holder_pid": 41},
    )

    result = Operations(host, _sleep=lambda _seconds: None).steps(
        [{"op": "type", "text": "dummy"}, {"op": "key", "key": "return"}], app="Demo"
    )
    if isinstance(result.strip, tuple):
        shutil.rmtree(result.strip[-1].parent)

    assert (result.status, result.stopped_at) == ("stopped", 0)
    assert result.receipts[0].error["details"]["reason"] == "secure_input_in_app"
    assert (host.type_calls, host.key_calls) == ([], [])
