"""Shared location for explicitly persisted macOS Harness data."""

import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("MACOS_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "macos-harness"
