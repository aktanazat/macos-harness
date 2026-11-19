"""Fresh interpreters exercise the public lazy exports and CLI startup."""

import os
import subprocess
import sys

import pytest


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "DO_NOT_TRACK": "1"},
        text=True, capture_output=True, check=True,
    )


@pytest.mark.parametrize("argv", [None, ["--help"], ["--version"], ["skill"], []])
def test_cli_does_not_load_unused_surfaces(argv) -> None:
    result = _run(f"""
import io
import sys
from macos_harness import cli
sys.stdin = io.StringIO('')
if {argv!r} is not None:
    try:
        status = cli.main({argv!r})
    except SystemExit as exc:
        status = exc.code
    assert status == (2 if {argv!r} == [] else 0), status
for name in ('browser', 'handoff', 'receipts', 'steps', 'macos'):
    assert 'macos_harness.' + name not in sys.modules, name
""")
    if argv == []:
        assert "No Python code received" in result.stderr


def test_lazy_exports_keep_identity_discovery_and_caching() -> None:
    _run("""
import importlib
import sys
import macos_harness as package
assert 'macos_harness.receipts' not in sys.modules
assert set(package.__all__) <= set(dir(package))
for name, module in package._EXPORTS.items():
    value = getattr(package, name)
    assert value is getattr(importlib.import_module('macos_harness.' + module), name)
    assert package.__dict__[name] is value
try:
    package.not_an_export
except AttributeError as exc:
    assert 'not_an_export' in str(exc)
else:
    raise AssertionError('missing export did not raise')
""")
