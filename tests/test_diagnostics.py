"""Diagnostic contracts at the existing in-memory OS boundary."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest
from test_macos import _focus_ax, _Refused
from test_ops import FakeHost, _exit_script, _flood_script
from test_receipts import _make_receipt

from macos_harness import (
    Acted,
    ErrorCode,
    MacOSError,
    OperationError,
    Outcome,
    diagnostics,
    present,
)
from macos_harness import macos as macos_module
from macos_harness.macos import MacOS
from macos_harness.ops import Operations


class InspectionHost(FakeHost, MacOS):
    _focus_sample = MacOS._focus_sample

    def __init__(self) -> None:
        super().__init__()
        self.do = Operations(self)
        self._element_seq = 0
        self.root, self.field, self.button, self.sheet = object(), object(), object(), object()
        self.enhanced = False
        self.data = {
            self.root: {"AXRole": "AXApplication", "AXChildren": [self.field, self.button, self.sheet],
                        "AXFocusedUIElement": self.field},
            self.field: {"AXRole": "AXTextField", "AXIdentifier": "name", "AXValue": "private text",
                         "AXNumberOfCharacters": 12, "AXSelectedTextRange": [0, 4]},
            self.button: {"AXRole": "AXButton", "AXTitle": "Save as", "AXIdentifier": "save-as", "AXEnabled": False},
            self.sheet: {"AXRole": "AXSheet", "AXTitle": "Confirm"},
        }

    def _application_element(self, pid: int, *, enhance: bool = True) -> object:
        self.enhanced |= enhance
        return self.root

    def windows(self, app: str | int | None = None) -> list[dict[str, object]]:
        return [{"window_id": 7, "title": "Draft", "bounds": {"x": 0, "y": 0, "width": 300, "height": 200}}]

    def capture_screenshot(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("Tests must never request a desktop screenshot")


@pytest.fixture
def inspection(monkeypatch: pytest.MonkeyPatch):
    host = InspectionHost()
    requested = _focus_ax(monkeypatch, host, host.data, batch=False, focused=host.field)
    return host, requested


def test_default_inspection_never_acquires_values_or_enhances_accessibility(inspection) -> None:
    host, requested = inspection

    state = host.inspect("Demo")

    assert state["blocked"] is True
    assert state["dialogs"][0]["title"] == "Confirm"
    assert state["nodes"][2]["enabled"] is False
    assert host.enhanced is False
    assert not {"AXValue", "AXNumberOfCharacters", "AXSelectedTextRange"} & set(requested)
    assert "value" not in state["focus"]["focused"]


@pytest.mark.parametrize("refused_identity", [False, True], ids=["secure-field", "refused-identity"])
def test_value_inspection_never_reads_a_secure_or_unidentified_fields_content(inspection, refused_identity) -> None:
    host, _requested = inspection
    host.data[host.field]["AXSubrole"] = (
        _Refused(macos_module.AS.kAXErrorCannotComplete) if refused_identity else "AXSecureTextField"
    )
    # Only this field has a value. Other nodes need no value reads for this contract.
    original = macos_module.AS.AXUIElementCopyAttributeValue
    field_requests: list[str] = []

    def observe(element, name, output):
        if element is host.field:
            field_requests.append(name)
        return original(element, name, output)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(macos_module.AS, "AXUIElementCopyAttributeValue", observe)
        state = host.inspect("Demo", include_values=True)

    assert "value" not in state["nodes"][1]
    assert not {"AXValue", "AXNumberOfCharacters", "AXSelectedTextRange"} & set(field_requests)
    assert state["coverage"]["read_cut"] is refused_identity


def test_partial_inspection_does_not_claim_that_no_dialog_blocks_the_app(inspection) -> None:
    host, _requested = inspection

    partial = host.inspect("Demo", max_nodes=2)
    complete = host.inspect("Demo")

    assert partial["blocked"] is None
    assert partial["coverage"]["node_cut"] is True
    assert complete["blocked"] is True


def test_failed_search_inspection_shows_current_controls_without_retrying_the_action(inspection) -> None:
    host, _requested = inspection
    host.press_results.append(MacOSError("No matching control", code=ErrorCode.TIMEOUT))
    with pytest.raises(OperationError, match="No matching control") as failure:
        host.do.press(app="Demo", role="button", identifier="missing")
    receipt = failure.value.receipt

    state = host.inspect(receipt)

    assert [(node["identifier"], node["enabled"]) for node in state["nearby"]] == [("save-as", False)]
    assert receipt.error["code"] == ErrorCode.TIMEOUT
    assert host.do.history() == (receipt,)


def test_failed_expectation_retains_its_app_for_diagnostics(inspection) -> None:
    host, _requested = inspection
    host.wait_results.append(MacOSError("No matching control", code=ErrorCode.TIMEOUT))
    with pytest.raises(OperationError, match="No matching control") as failure:
        host.do.expect(present(app="Demo", role="button", identifier="missing", timeout=0))
    receipt = failure.value.receipt

    state = host.inspect(receipt)

    assert state["process"]["pid"] == 41
    assert state["process"]["launched_at"] == 1000.0
    assert [(node["identifier"], node["enabled"]) for node in state["nearby"]] == [("save-as", False)]
    assert receipt.error["code"] == ErrorCode.TIMEOUT
    assert host.do.history() == (receipt,)


def test_window_comparison_refuses_a_reused_pid_with_another_launch_identity() -> None:
    before = {"app": {"pid": 41}, "process": {"launched_at": 1000.0}, "windows": [{"window_id": 7}]}
    after = {**before, "process": {"launched_at": 2000.0}}

    with pytest.raises(MacOSError, match="different app processes") as failure:
        MacOS.diff_windows(before, after)

    assert failure.value.code == ErrorCode.BAD_REQUEST


def test_log_interval_and_row_cap_apply_after_second_rounded_collection(monkeypatch) -> None:
    events = [
        {"timestamp": "2026-09-14 10:00:00.200000+0000", "processID": 41, "eventMessage": "too early"},
        {"timestamp": "2026-09-14 10:00:00.300000+0000", "processID": 41, "eventMessage": "first", "eventType": "activityCreateEvent"},
        {"timestamp": "2026-09-14 10:00:00.700000+0000", "processID": 41, "eventMessage": "second", "eventType": "logEvent"},
        {"timestamp": "2026-09-14 10:00:00.800000+0000", "processID": 41, "eventMessage": "too late"},
        {"count": 4, "finished": 1},
    ]
    source = _exit_script(stdout="\n".join(json.dumps(event) for event in events).encode())
    read_command = diagnostics._read_command

    def emit(_command, *, timeout, max_bytes):
        return read_command([sys.executable, "-c", source], timeout=timeout, max_bytes=max_bytes)

    monkeypatch.setattr(diagnostics, "_read_command", emit)
    host = InspectionHost()
    result = host.logs(("2026-09-14T10:00:00.250+00:00", "2026-09-14T10:00:00.750+00:00"), app=41, limit=1)

    assert [row["eventMessage"] for row in result["rows"]] == ["first"]
    assert result["coverage"]["matching_rows"] == 2
    assert result["coverage"]["query_finished"] is True
    assert result["status"] == "partial"
    assert result["truncated"] is True


def test_diagnostic_output_is_bounded_while_the_real_child_is_drained() -> None:
    result = diagnostics._read_command([sys.executable, "-c", _flood_script(65536)], timeout=2, max_bytes=1024)

    assert len(result.stdout) + len(result.stderr) == 1024
    assert result.truncated is True
    assert result.error is None
    assert result.returncode is not None


def test_status_observes_a_kernel_process_without_an_appkit_record(monkeypatch) -> None:
    class RunningApplications:
        @staticmethod
        def runningApplicationWithProcessIdentifier_(pid: int):
            return None

    monkeypatch.setattr(macos_module, "NSRunningApplication", RunningApplications)
    monkeypatch.setattr(macos_module, "_process_start_time", lambda pid: 1000.0)
    mac = MacOS()

    status = mac.status(41)

    assert status["process"]["state"] == "running"
    assert status["app"]["bundle_id"] is None


def test_diagnostic_deadline_kills_and_reaps_a_silent_child() -> None:
    result = diagnostics._read_command([sys.executable, "-c", "import signal; signal.pause()"], timeout=0.05, max_bytes=1024)

    assert result.error["code"] == ErrorCode.TIMEOUT
    assert result.returncode is not None


@pytest.mark.parametrize("shape", ["empty-stack", "missing-termination", "populated-stack"])
def test_crash_lookup_preserves_optional_fields_and_excludes_another_incarnation(tmp_path, shape) -> None:
    body = {
        "pid": 98517, "captureTime": "2026-09-13 19:03:44.7509 -0700",
        "procLaunch": "2026-09-13 19:03:44.7479 -0700", "faultingThread": 0,
        "threads": [{"frames": []}], "usedImages": [{"name": "Demo", "path": "/private/not-exported"}],
        "exception": {"type": "EXC_CRASH", "signal": "SIGKILL", "rawCodes": [0, 0]},
    }
    if shape != "missing-termination":
        body["termination"] = {"namespace": "CODESIGNING", "code": 4, "indicator": "Launch Constraint Violation"}
    if shape == "populated-stack":
        body["threads"][0]["frames"] = [{"symbol": "applyChange", "imageIndex": 0} for _ in range(12)]
    header = json.dumps({"app_name": "Demo"}) + "\n"
    (tmp_path / "current.ips").write_text(header + json.dumps(body))
    old = {**body, "procLaunch": "2026-09-13 19:03:43.7479 -0700"}
    (tmp_path / "earlier.ips").write_text(header + json.dumps(old))
    receipt = _make_receipt(
        started_at="2026-09-13T19:03:44.700-07:00", finished_at="2026-09-13T19:03:44.800-07:00",
        process={"pid": 98517, "launched_at": datetime.fromisoformat("2026-09-13T19:03:44.7479-07:00").timestamp()},
    )

    result = diagnostics.collect_crashes(receipt, 98517, directories=[tmp_path])

    assert [row["file"] for row in result["rows"]] == ["current.ips"]
    report = result["rows"][0]
    assert report["identity_match"] == "pid_launch_and_time"
    assert report["termination"] == body.get("termination", {})
    assert report["frames"] == ([{"symbol": "applyChange", "image": "Demo"}] * 10 if shape == "populated-stack" else [])
    assert report["frames_truncated"] is (shape == "populated-stack")


def test_explanation_does_not_replace_the_action_error_with_a_diagnostic_failure() -> None:
    error = MacOSError("The app refused the action", code=ErrorCode.AX_ERROR).to_json()
    receipt = _make_receipt(outcome=Outcome.FAILED, acted=Acted.UNKNOWN, verified=False,
                            error=error, process={"pid": 41, "launched_at": 1000.0, "state": "unknown"})
    log_failure = {"kind": "logs", "pid": 41, "status": "failed", "error": {"code": "timeout"}}

    explanation = MacOS.explain(receipt, log_failure)

    assert explanation["receipt"] == receipt.to_json()
    assert explanation["receipt"]["error"] == error
    assert explanation["input_may_have_happened"] is True
    assert explanation["findings"][0]["kind"] == "diagnostic.incomplete"


@pytest.mark.parametrize("kind", ["reused-pid", "old-interval"])
def test_explanation_refuses_evidence_from_another_action(kind) -> None:
    receipt = _make_receipt(process={"pid": 41, "launched_at": 1000.0})
    evidence = {"pid": 41, "process": {"pid": 41, "launched_at": 2000.0}} if kind == "reused-pid" else {
        "pid": 41, "requested": {"start": "2026-09-13T10:00:00+00:00", "end": "2026-09-13T10:01:00+00:00"},
    }

    with pytest.raises(MacOSError, match="another process incarnation|outside the action interval") as failure:
        MacOS.explain(receipt, evidence)

    assert failure.value.code == ErrorCode.BAD_REQUEST
