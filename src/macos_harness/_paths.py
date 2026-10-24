"""Shared location for explicitly persisted macOS Harness data."""

import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("MACOS_HARNESS_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "macos-harness"


# _CS_DARWIN_USER_TEMP_DIR in <unistd.h>: the user's temporary directory,
# whatever TMPDIR names.
_USER_TEMP_DIR = 65537


def user_temp_dir() -> str:
    """The user's own temporary directory, for locks every process of this
    user shares; OSError when macOS names none."""
    directory = os.confstr(_USER_TEMP_DIR)
    if directory is None:
        raise OSError("macOS names no user temporary directory")
    return directory
