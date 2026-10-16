"""Route contracts through real Operations and an in-memory app."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from test_ops import FakeHost, FormHost, _SleepClock

from macos_harness import (
    Acted,
    ErrorCode,
    MacOSError,
    OperationError,
    Outcome,
    equals,
    gone,
    present,
)
from macos_harness.macos import MacOS, _AppIdentity
from macos_harness.ops import Operations
from macos_harness.routes import Routes

_APP = "com.example.demo"


class RouteHost(FakeHost):
    def __init__(self) -> None:
        super().__init__()
        self.clock = _SleepClock()
        self.do = Operations(self, _monotonic=self.clock.monotonic, _sleep=self.clock.sleep)
        self.route = Routes(self)
        self.identity = _AppIdentity(41, _APP, 1000.0, "Demo", None)
        self.version = ("1", "10")
        self.page = 0
        self.pressed_pages: list[int] = []
        self.stuck_at: int | None = None
        self.replace_at: int | None = None
        self.goal_error: MacOSError | None = None
        self.enhanced = False
        self.identity_delay = 0.0

    def _process_identity(self, query: str | int) -> _AppIdentity:
        return self.identity

    def _same_process(self, expected: _AppIdentity) -> bool:
        self.clock.sleep(self.identity_delay)
        return MacOS._same_process(self, expected)

    def _bundle_version(self, path: str | None) -> tuple[str | None, str | None]:
        return self.version

    def _validate_key(self, key: str) -> None:
        MacOS._validate_key(key)

    def ax_wait(self, **kwargs: object) -> dict[str, object]:
        self.wait_calls.append(dict(kwargs))
        if kwargs.get("enhance", True):
            self.enhanced = True
        identifier = kwargs.get("identifier")
        if identifier == "page-3" and self.goal_error is not None:
            raise self.goal_error
        if identifier not in (f"page-{self.page}", f"next-{self.page}", "value"):
            timeout = kwargs.get("timeout", 0.0)
            self.clock.sleep(timeout)
            raise MacOSError("No matching control", code=ErrorCode.TIMEOUT,
                             details={"timeout": timeout})
        return {**self.match, "identifier": identifier, "title": identifier}

    def ax_press(self, **kwargs: object) -> dict[str, object]:
        match = self.ax_wait(**kwargs)
        self.perform_action(7)
        return match

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        super().perform_action(element_index, action)
        self.pressed_pages.append(self.page)
        if self.page != self.stuck_at:
            self.page += 1
        if self.page == self.replace_at:
            self.identity = self.identity._replace(launched_at=2000.0)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RouteHost:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    return RouteHost()


def record_navigation(host: RouteHost) -> Path:
    with host.route.record("navigate", app=_APP,
                           entry=present(role="button", identifier="page-0"),
                           goal=present(role="button", identifier="page-3")) as rec:
        for page in range(3):
            rec.press(role="button", identifier=f"next-{page}",
                      postcondition=present(role="button", identifier=f"page-{page + 1}"))
    host.page = 0
    host.pressed_pages.clear()
    host.enhanced = False
    return rec.path


def test_route_keeps_the_mutating_failure_and_never_attempts_the_next_step(host: RouteHost) -> None:
    record_navigation(host)
    host.stuck_at = 1

    result = host.route.run("navigate", app=_APP)

    assert result.status == "diverged"
    assert result.at == "steps[1]"
    assert host.pressed_pages == [0, 1]
    assert [receipt.outcome for receipt in result.steps_run] == [Outcome.DONE, Outcome.FAILED]
    assert result.steps_run[-1].acted is Acted.YES
    assert result.steps_run[-1].error == result.error


def test_route_cannot_cross_to_a_replacement_process_with_the_same_pid(host: RouteHost) -> None:
    record_navigation(host)
    host.replace_at = 1

    result = host.route.run("navigate", app=_APP)

    assert result.status == "diverged"
    assert result.error["details"]["reason"] == "process_changed"
    assert host.pressed_pages == [0]
    assert len(result.steps_run) == 1
    assert result.steps_run[0].acted is Acted.YES


def test_caught_recording_failure_preserves_the_existing_route(host: RouteHost) -> None:
    path = record_navigation(host)
    original = path.read_bytes()
    host.goal_error = MacOSError("Arrival check refused", code=ErrorCode.AX_ERROR)

    with (
        pytest.raises(MacOSError, match="failed or empty recording"),
        host.route.record("navigate", app=_APP,
                          entry=present(role="button", identifier="page-0"),
                          goal=present(role="button", identifier="page-2")) as rec,
    ):
        rec.press(role="button", identifier="next-0",
                  postcondition=present(role="button", identifier="page-1"))
        with pytest.raises(OperationError, match="Arrival check refused"):
            rec.press(role="button", identifier="next-1",
                      postcondition=present(role="button", identifier="page-3"))

    assert path.read_bytes() == original
    assert host.pressed_pages == [0, 1]
    assert [receipt.outcome for receipt in rec.receipts] == [Outcome.DONE, Outcome.FAILED]


def test_a_bad_later_step_is_rejected_before_the_first_mutation(host: RouteHost) -> None:
    path = record_navigation(host)
    data = json.loads(path.read_text())
    data["steps"][2] = {"verb": "set", "target": data["steps"][2]["target"],
                        "value": "route-secret-canary", "attribute": "AXValue", "expect": None}
    path.write_text(json.dumps(data))

    result = host.route.run("navigate", app=_APP)

    assert result.status == "invalid"
    assert result.error["details"]["field"] == "value"
    assert host.pressed_pages == []
    assert result.steps_run == ()


def test_caught_string_recording_attempt_does_not_replace_a_saved_route(host: RouteHost) -> None:
    path = record_navigation(host)
    original = path.read_bytes()

    with (
        pytest.raises(MacOSError, match="failed or empty recording"),
        host.route.record("navigate", app=_APP,
                          entry=present(role="button", identifier="page-0"),
                          goal=present(role="button", identifier="page-0")) as rec,
        pytest.raises(MacOSError, match="only boolean and numeric"),
    ):
        rec.set("route-secret-canary", role="button", identifier="value")

    assert path.read_bytes() == original
    assert host.value is False


def test_dry_run_checks_only_current_targets_without_enabling_accessibility(host: RouteHost) -> None:
    record_navigation(host)

    result = host.route.run("navigate", app=_APP, dry_run=True)

    assert result.status == "planned"
    assert host.page == 0
    assert host.pressed_pages == []
    assert host.enhanced is False
    assert result.steps_run == ()


def test_an_already_satisfied_goal_does_not_repeat_navigation(host: RouteHost) -> None:
    record_navigation(host)
    host.page = 3

    result = host.route.run("navigate", app=_APP)

    assert result.status == "already"
    assert result.steps_run == ()
    assert host.pressed_pages == []


def record_dismissal(host: RouteHost) -> Path:
    with host.route.record("dismiss", app=_APP,
                           entry=present(role="button", identifier="page-0"),
                           goal=gone(role="button", identifier="page-3")) as rec:
        rec.press(role="button", identifier="next-0",
                  postcondition=present(role="button", identifier="page-1"))
    host.page = 0
    host.pressed_pages.clear()
    host.enhanced = False
    return rec.path


@pytest.mark.parametrize(
    ("route", "record", "code", "details"),
    [
        ("navigate", record_navigation, ErrorCode.TIMEOUT, {"timeout": 0.0, "complete": False}),
        ("navigate", record_navigation, ErrorCode.AX_ERROR, {"ax_error": -25204}),
        # One empty poll saw the match absent, but the confirming second poll never
        # ran: absence is unconfirmed, so the route may not replay its presses.
        ("dismiss", record_dismissal, ErrorCode.TIMEOUT,
         {"timeout": 0.1, "consecutive_empty_polls": 1}),
    ],
)
def test_an_unobserved_goal_cannot_authorize_navigation(
    host: RouteHost,
    route: str,
    record: Callable[[RouteHost], Path],
    code: ErrorCode,
    details: dict[str, object],
) -> None:
    record(host)
    refused = MacOSError("Goal read refused", code=code, details=details)
    host.goal_error = refused
    host.gone_error = refused

    result = host.route.run(route, app=_APP)

    assert result.status == "diverged"
    assert result.at == "goal"
    assert result.error["code"] == code
    assert result.check.verified is False
    assert host.pressed_pages == []


def test_slow_identity_observation_cannot_extend_the_route_deadline(host: RouteHost) -> None:
    record_navigation(host)
    host.identity_delay = 0.06

    result = host.route.run("navigate", app=_APP, timeout=0.05)

    assert result.status == "diverged"
    assert result.error["details"]["reason"] == "deadline_exhausted_before_dispatch"
    assert host.pressed_pages == []


class FormRouteHost(FormHost):
    def __init__(self) -> None:
        super().__init__()
        self.identity = _AppIdentity(41, _APP, 1000.0, "Demo", None)
        clock = _SleepClock()
        self.do = Operations(self, _monotonic=clock.monotonic, _sleep=clock.sleep)
        self.route = Routes(self)

    def _process_identity(self, query: str | int) -> _AppIdentity:
        return self.identity

    def _same_process(self, expected: _AppIdentity) -> bool:
        return MacOS._same_process(self, expected)

    def _bundle_version(self, path: str | None) -> tuple[str | None, str | None]:
        return None, None


@pytest.fixture
def form_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FormRouteHost:
    monkeypatch.setenv("MACOS_HARNESS_HOME", str(tmp_path))
    return FormRouteHost()


def record_form(host: FormRouteHost) -> Path:
    with host.route.record("domain", app=_APP,
                           inputs={"domain": "recorded-domain-canary.invalid"},
                           entry=present(role="text field", identifier="domain"),
                           goal=equals(role="button", title="Check", attribute="AXEnabled", value=True)) as rec:
        rec.fill("domain", identifier="domain")
    return rec.path


def test_form_replay_uses_new_inputs_when_the_previous_goal_still_holds(form_host: FormRouteHost) -> None:
    record_form(form_host)

    result = form_host.route.run("domain", app=_APP, inputs={"domain": "new-domain.invalid"})

    assert form_host.model == "new-domain.invalid"
    assert result.status == "done"
    assert result.steps_run[0].verified is True


def test_recorded_forms_do_not_persist_the_entered_text(form_host: FormRouteHost) -> None:
    path = record_form(form_host)

    assert form_host.model == "recorded-domain-canary.invalid"
    assert "recorded-domain-canary.invalid" not in path.read_text()
    assert form_host.route.list(app=_APP)[0]["inputs"] == ["domain"]


@pytest.mark.parametrize("failure", ["missing", "unknown", "control-character"])
def test_bad_form_inputs_stop_before_earlier_navigation(host: RouteHost, failure: str) -> None:
    path = record_navigation(host)
    data = json.loads(path.read_text())
    data["steps"].append({"verb": "fill", "parameter": "domain", "expect": None,
                          "target": {"role": "text field", "field": "identifier", "value": "domain"}})
    path.write_text(json.dumps(data))
    inputs = {} if failure == "missing" else {"domain": "new-domain.invalid"}
    if failure == "unknown":
        inputs["unknown"] = "unused"
    elif failure == "control-character":
        inputs["domain"] += "\n"

    result = host.route.run("navigate", app=_APP, inputs=inputs)

    assert result.status == "invalid"
    assert result.error["code"] == ErrorCode.BAD_REQUEST
    assert host.page == 0
    assert result.steps_run == ()
