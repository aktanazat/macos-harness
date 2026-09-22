"""CI-safe CLI parsing and stdin error-output tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from macos_harness import cli


def test_agent_is_not_a_recognized_subcommand() -> None:
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["agent", "start"])
    assert excinfo.value.code == 2


def test_agent_build_is_not_a_recognized_subcommand_either() -> None:
    """No shim, no partial cutover: not even ``agent build`` survives."""
    parser = cli._build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["agent", "build"])
    assert excinfo.value.code == 2


def test_every_other_subcommand_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MACOS_HARNESS_BACKEND", raising=False)
    parser = cli._build_parser()

    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["apps"]).command == "apps"
    assert parser.parse_args(["repl"]).command == "repl"
    assert parser.parse_args(["skill"]).command == "skill"
    assert parser.parse_args(["telemetry", "status"]).command == "telemetry"
    assert parser.parse_args(["see", "Finder"]).command == "see"
    assert parser.parse_args(["state", "Finder"]).command == "state"


def test_main_exits_with_argparse_error_for_a_stale_agent_invocation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real end-to-end entry point, not just the parser in isolation:
    a user typing the removed ``macos-harness agent ...`` invocation gets
    argparse's own usage error, never a branch that quietly does nothing
    or falls through to executing stdin as Python.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["agent", "status"])
    assert excinfo.value.code == 2
    assert "invalid choice: 'agent'" in capsys.readouterr().err


def test_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"macos-harness {cli.__version__}"


def test_stdin_json_errors_preserves_operation_receipt() -> None:
    program = """\
from macos_harness import Acted, Executor, OperationError, Outcome, Receipt

print("before failure")
raise OperationError.from_receipt(Receipt(
    op="press",
    outcome=Outcome.FAILED,
    acted=Acted.NO,
    backend="python",
    executor=Executor.PYTHON,
    request={"target": "Save"},
    changed=False,
    verified=False,
    duration_s=0.25,
    error={
        "code": "ax.error",
        "message": "search is incomplete",
        "details": {
            "candidates": [{"element_index": 11}, {"element_index": 12}],
            "complete": False,
        },
    },
))
"""
    result = subprocess.run(
        [sys.executable, "-m", "macos_harness.cli", "--json-errors"],
        input=program, text=True, capture_output=True, timeout=10, check=False,
        env={**os.environ, "DO_NOT_TRACK": "1", "MACOS_HARNESS_BACKEND": "python"},
    )

    assert result.returncode == 1
    assert result.stdout == "before failure\n"
    payload = json.loads(result.stderr)
    error = {
        "code": "ax.error",
        "message": "search is incomplete",
        "details": {
            "candidates": [{"element_index": 11}, {"element_index": 12}],
            "complete": False,
        },
    }
    assert {key: payload[key] for key in error} == error
    receipt = payload["receipt"]
    assert receipt["op"] == "press"
    assert receipt["outcome"] == "failed"
    assert receipt["acted"] == "no"
    assert receipt["request"] == {"target": "Save"}
    assert receipt["changed"] is False
    assert receipt["verified"] is False
    assert receipt["error"] == error
