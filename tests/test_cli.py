"""CI-safe CLI parsing and stdin error-output tests."""

from __future__ import annotations

import json
import os
import re
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


def _skill(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = cli.main(["skill", *args])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_skill_prints_every_reference_the_card_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    _status, card, _err = _skill([], capsys)
    listed = card.split("## References", 1)[1]
    names = set(re.findall(r"^- `([a-z-]+)`:", listed, re.MULTILINE))
    names |= set(re.findall(r"references/([a-z-]+)\.md", card))

    unprintable = [name for name in sorted(names) if _skill([name], capsys)[0] != 0]

    assert len(names) >= 9
    assert unprintable == []


def test_skill_names_the_real_references_for_an_unknown_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    status, out, err = _skill(["native"], capsys)

    assert status == 1
    assert out == ""
    assert "no skill reference 'native'" in err
    assert "native-backend" in err


def test_skill_card_stays_within_its_size_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Agents reread the card after every compaction; detail belongs in a reference."""
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    _status, card, _err = _skill([], capsys)

    assert len(card.encode("utf-8")) <= 3500


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


@pytest.mark.parametrize(
    ("program", "error", "signature"),
    (
        pytest.param(
            'mac.scroll(dy=-300, app="Finder")\n',
            "TypeError: MacOS.scroll() got an unexpected keyword argument 'dy'",
            "Signature: mac.scroll(delta_y, delta_x=0, *, app=None, unit='pixel', "
            "x=None, y=None, coordinate_space='screenshot')",
            id="mac",
        ),
        pytest.param(
            'mac.ax.get(3, attribute="AXValue")\n',
            "TypeError: Accessibility.get() got an unexpected keyword argument 'attribute'",
            "Signature: mac.ax.get(element_index, attributes='AXValue')",
            id="mac.ax",
        ),
    ),
)
def test_stdin_misused_call_shows_the_real_signature(program, error, signature) -> None:
    """A call with a wrong keyword fails before it does anything; the error
    ends with the method's real signature so the retry can be right."""
    result = subprocess.run(
        [sys.executable, "-m", "macos_harness.cli"],
        input=program, text=True, capture_output=True, timeout=10, check=False,
        env={**os.environ, "DO_NOT_TRACK": "1", "MACOS_HARNESS_BACKEND": "python"},
    )

    assert result.returncode == 1
    *_, raised, shown = result.stderr.splitlines()
    # Python 3.13 adds a "Did you mean ...?" hint to the TypeError.
    assert raised.startswith(error)
    assert shown == signature
