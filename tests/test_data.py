from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from macos_harness.data import AppData
from macos_harness.errors import MacOSError


def test_read_defaults_refuses_a_container_it_cannot_read_instead_of_reading_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    domain = "org.macos-harness.test.sandboxed"
    preferences = tmp_path / "Library/Containers" / domain / "Data/Library/Preferences"
    preferences.mkdir(parents=True)
    (preferences / f"{domain}.plist").write_bytes(plistlib.dumps({"RichText": 0}))
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(MacOSError) as raised:
        AppData().read_defaults(domain, "RichText")

    assert raised.value.code == "unsupported_op"
    assert raised.value.details["path"] == str(preferences / f"{domain}.plist")
