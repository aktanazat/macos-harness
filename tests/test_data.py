from __future__ import annotations

import plistlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from macos_harness.data import AppData


def _files(folder: Path) -> dict[str, int]:
    return {path.name: path.stat().st_size for path in folder.iterdir()}


def test_query_sqlite_refuses_attach_and_leaves_a_pending_log_in_place(tmp_path: Path) -> None:
    store = tmp_path / "app.sqlite"
    writer = (
        "import os, sqlite3, sys\n"
        "w = sqlite3.connect(sys.argv[1])\n"
        "w.execute('pragma journal_mode=wal')\n"
        "w.execute('pragma wal_autocheckpoint=0')\n"
        "w.execute('create table t(x)')\n"
        "w.executemany('insert into t values (?)', [(i,) for i in range(500)])\n"
        "w.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", writer, str(store)], check=True)
    before = _files(tmp_path)

    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        AppData().query_sqlite(store, f"attach 'file:{store}?mode=rw' as w")

    assert _files(tmp_path) == before
    assert AppData().query_sqlite(store, "select count(*) as n from t") == [{"n": 500}]


def test_query_sqlite_makes_no_files_beside_a_closed_store(tmp_path: Path) -> None:
    store = tmp_path / "app.sqlite"
    with sqlite3.connect(store) as connection:
        connection.execute("pragma journal_mode=wal")
        connection.execute("create table t(x)")
        connection.execute("insert into t values (1)")
    connection.close()

    rows = AppData().query_sqlite(store, "select x from t")

    assert rows == [{"x": 1}]
    assert sorted(_files(tmp_path)) == ["app.sqlite"]


def test_read_defaults_reads_an_empty_readable_container_domain_as_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    domain = "org.macos-harness.test.empty"
    preferences = tmp_path / "Library/Containers" / domain / "Data/Library/Preferences"
    preferences.mkdir(parents=True)
    (preferences / f"{domain}.plist").write_bytes(plistlib.dumps({}))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert AppData().read_defaults(domain, "RichText") is None
