"""Opt-in local JSONL record of finished ``mac.do`` receipts.

Off unless ``MACOS_HARNESS_RECEIPT_LOG`` is ``1``, ``true``, ``yes``, or ``on``.
Each finished receipt then appends one line to
``~/Library/Logs/macos-harness/receipts.jsonl``: when it finished, the process,
the verb, the app, the control's role and labels, the outcome, the duration,
and the error. Requested and observed values, typed text, and script source are
never written; ``type`` and ``fill`` errors keep only their code, because an
app's error text can echo the input. The file moves to ``receipts.jsonl.1`` once
the next line would take it past ``MAX_BYTES``, so the log holds at most two
files of that size. Nothing leaves the Mac.
"""

from __future__ import annotations

import fcntl
import json
import os
import warnings
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .receipts import JSONValue, Receipt

ENV = "MACOS_HARNESS_RECEIPT_LOG"
MAX_BYTES = 5 * 1024 * 1024
_ON = frozenset({"1", "true", "yes", "on"})
_APP_FIELDS = ("name", "bundle_id", "pid")
_TARGET_FIELDS = ("role", "title", "identifier", "description")
_LABEL_LIMIT = 120
_TEXT_OPS = frozenset({"type", "fill"})


def enabled() -> bool:
    return os.environ.get(ENV, "").strip().lower() in _ON


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "macos-harness" / "receipts.jsonl"


def append(receipt: Receipt, *, now: float) -> None:
    """Append ``receipt`` when the log is enabled. A write failure warns and
    never fails the operation, which has already happened."""
    if not enabled():
        return
    line = json.dumps(_entry(receipt, now=now), ensure_ascii=False, separators=(",", ":"))
    try:
        _write(log_path(), (line + "\n").encode("utf-8"))
    except OSError as exc:
        warnings.warn(f"macos-harness receipt log not written: {exc}", RuntimeWarning, stacklevel=3)


def _entry(receipt: Receipt, *, now: float) -> dict[str, JSONValue]:
    target = receipt.target if isinstance(receipt.target, Mapping) else {}
    labels = {
        field: target[field][:_LABEL_LIMIT]
        for field in _TARGET_FIELDS
        if isinstance(target.get(field), str)
    }
    return {
        "time": datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds"),
        "process": os.getpid(),
        "op": receipt.op,
        "outcome": receipt.outcome.value,
        "acted": receipt.acted.value,
        "app": _app(_selector(receipt.request), target.get("app")),
        "target": labels or None,
        "duration_s": round(receipt.duration_s, 4),
        "verified": receipt.verified,
        "changed": receipt.changed,
        "replayed": receipt.replayed,
        "error": _error(receipt),
    }


def _selector(request: Mapping[str, JSONValue]) -> JSONValue:
    """The app the caller named: scoped searches keep it under ``scope``."""
    scope = request.get("scope")
    if isinstance(scope, Mapping):
        return scope.get("app")
    return request.get("app")


def _app(selector: JSONValue, info: JSONValue) -> JSONValue:
    if isinstance(info, Mapping):
        return {field: info[field] for field in _APP_FIELDS if field in info}
    if isinstance(selector, str):
        return selector[:_LABEL_LIMIT]
    if isinstance(selector, int):
        return selector
    return None


def _error(receipt: Receipt) -> JSONValue:
    if receipt.error is None:
        return None
    if receipt.op in _TEXT_OPS:
        return {"code": receipt.error["code"]}
    return {"code": receipt.error["code"], "message": receipt.error["message"]}


def _write(path: Path, data: bytes) -> None:
    """Append under an exclusive lock so concurrent harness processes neither
    interleave lines nor rotate the same file twice."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            opened = os.fstat(fd)
            try:
                current = os.stat(path).st_ino
            except FileNotFoundError:
                current = None
            if current != opened.st_ino:
                continue  # another process rotated while this one waited
            if opened.st_size and opened.st_size + len(data) > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
                continue
            os.write(fd, data)
            return
        finally:
            os.close(fd)
