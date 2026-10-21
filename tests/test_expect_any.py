"""Contract tests for ``mac.do.expect_any``'s branching wait.

Only the edges the ordinary paths cannot catch live here: how the one
shared budget is spent across outcomes, and how `Gone`'s two-poll
absence rule survives being counted across passes instead of inside one
`ax_wait_gone` call. The verb's ordinary behaviour -- a matched label, a
receipt shape, a rejected request -- is exercised by the same machinery
`expect` already covers in ``test_ops.py``.
"""

from __future__ import annotations

from collections import deque

import pytest
from test_ops import FakeHost, _SleepClock

from macos_harness.errors import ErrorCode, MacOSError
from macos_harness.ops import Operations
from macos_harness.receipts import OperationError, gone, present


def _timeout(**details: object) -> MacOSError:
    return MacOSError("timed out", code=ErrorCode.TIMEOUT, details=dict(details))


def _match(title: str) -> dict[str, object]:
    return {
        "element_index": 7,
        "role": "AXStaticText",
        "title": title,
        "description": None,
        "app": {"pid": 41, "name": "Demo", "bundle_id": "com.example.demo"},
    }


class _ScriptHost(FakeHost):
    """A host whose every poll is scripted, so a test can say what the
    app looked like on each pass rather than only how it ended.

    A poll that finds nothing spends the window it was handed, the way a
    real `ax_wait` does: it keeps searching until its own ``timeout``
    runs out. That is what makes a shared budget observable here -- an
    outcome handed the whole deadline really does consume it, leaving
    the other outcomes nothing.
    """

    def __init__(self, clock: _SleepClock) -> None:
        super().__init__()
        self.clock = clock
        self.scripts: dict[str, deque[dict[str, object] | MacOSError]] = {}
        self.gone_results: deque[MacOSError | None] = deque()

    def _spend(self, timeout: object) -> None:
        if isinstance(timeout, (int, float)):
            self.clock.now += float(timeout)

    def ax_wait(self, **kwargs: object) -> dict[str, object]:
        self.wait_calls.append(dict(kwargs))
        script = self.scripts.get(str(kwargs.get("search_key")))
        answer = script.popleft() if script else _timeout(timeout=kwargs.get("timeout"))
        if isinstance(answer, MacOSError):
            self._spend(kwargs.get("timeout"))
            raise answer
        return dict(answer)

    def ax_wait_gone(self, **kwargs: object) -> None:
        self.gone_calls.append(dict(kwargs))
        answer = self.gone_results.popleft() if self.gone_results else None
        if answer is not None:
            self._spend(kwargs.get("timeout"))
            raise answer


def _ops(host: _ScriptHost, clock: _SleepClock) -> Operations:
    return Operations(host, _monotonic=clock.monotonic, _sleep=clock.sleep)


def test_expect_any_gives_no_outcome_the_whole_budget() -> None:
    clock = _SleepClock()
    host = _ScriptHost(clock)
    # The first-listed outcome never appears; the second appears on the
    # third pass. A wait that let the first outcome hold the budget would
    # spend all five seconds on it and never see the second at all.
    host.scripts["AXAnyTypeSearchKey"] = deque(
        [_timeout(timeout=0.0), _timeout(timeout=0.0), _match("Saved")]
    )
    receipt = _ops(host, clock).expect_any(
        {"crashed": present(role="button", app=41), "saved": present(text="Saved", app=41)},
        timeout=5.0,
        interval=0.25,
    )

    observed = receipt.to_json()["observed"]
    assert observed["matched"] == ["saved"]
    assert observed["outcomes"]["crashed"]["polls"] == 3
    assert observed["outcomes"]["saved"]["polls"] == 3
    assert clock.sleeps == [0.25, 0.25]


def test_expect_any_refuses_a_per_outcome_timeout() -> None:
    clock = _SleepClock()
    host = _ScriptHost(clock)
    # Honouring this would be the starvation above; dropping it silently
    # would leave the caller believing a bound that never applied.
    with pytest.raises(MacOSError) as caught:
        _ops(host, clock).expect_any({"saved": present(text="Saved", app=41, timeout=2.0)})

    assert not isinstance(caught.value, OperationError)
    assert caught.value.code == ErrorCode.BAD_REQUEST.value
    assert caught.value.details["label"] == "saved"
    assert host.wait_calls == []


def test_expect_any_confirms_gone_across_two_spaced_polls() -> None:
    clock = _SleepClock()
    host = _ScriptHost(clock)
    empty = _timeout(timeout=0.0, consecutive_empty_polls=1, complete=True)
    host.gone_results = deque([empty, empty])

    receipt = _ops(host, clock).expect_any({"dismissed": gone(text="Saving", app=41)}, timeout=2.0)

    observed = receipt.to_json()["observed"]
    assert observed["matched"] == ["dismissed"]
    assert observed["outcomes"]["dismissed"]["polls"] == 2
    assert clock.sleeps == [0.1]


def test_expect_any_resets_the_absence_streak_on_a_partial_read() -> None:
    clock = _SleepClock()
    host = _ScriptHost(clock)
    empty = _timeout(timeout=0.0, consecutive_empty_polls=1, complete=True)
    truncated = _timeout(timeout=0.0, consecutive_empty_polls=0, complete=False)
    # Two empty polls with a walk that read nothing between them: the
    # pair is not consecutive, so absence stays unconfirmed.
    host.gone_results = deque([empty, truncated, empty])

    with pytest.raises(OperationError) as caught:
        _ops(host, clock).expect_any({"dismissed": gone(text="Saving", app=41)}, timeout=0.25)

    payload = caught.value.receipt.to_json()
    assert payload["observed"]["matched"] == []
    assert payload["observed"]["outcomes"]["dismissed"]["reason"] == "absence_unconfirmed"
    assert payload["error"]["details"]["unobservable"] == ["dismissed"]
