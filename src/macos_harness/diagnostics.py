"""Explicit diagnostics; collected evidence never changes an action receipt."""

from __future__ import annotations

import json
import math
import os
import selectors
import stat
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path

from .errors import ErrorCode, MacOSError
from .ops import _Deadline, _utc_timestamp
from .receipts import Acted, JSONValue, Outcome, Receipt

_CONTROL_FIELDS = ("element_index", "role", "subrole", "title", "description", "identifier", "enabled", "frame")


@dataclass(frozen=True)
class _CommandRead:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    truncated: bool
    error: JSONValue = None


def _read_command(command: Sequence[str], *, timeout: float, max_bytes: int) -> _CommandRead:
    deadline = _Deadline(timeout, time.monotonic)
    if not 1 <= max_bytes <= 8 * 1024 * 1024:
        raise MacOSError("max_bytes must be between 1 and 8388608", code=ErrorCode.BAD_REQUEST)
    out, err = bytearray(), bytearray()
    truncated = False
    error = None
    process = None
    try:
        deadline.check_dispatch()
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, out)
            selector.register(process.stderr, selectors.EVENT_READ, err)
            while selector.get_map():
                deadline.check_dispatch()
                for key, _events in selector.select(deadline.remaining()):
                    remaining = max_bytes - len(out) - len(err)
                    chunk = os.read(key.fd, min(8192, remaining + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    key.data.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                process.kill()
            process.wait(timeout=deadline.remaining())
    except (subprocess.TimeoutExpired, MacOSError) as exc:
        error = MacOSError("Diagnostic command exceeded its deadline", code=ErrorCode.TIMEOUT).to_json()
        if isinstance(exc, MacOSError) and exc.code != ErrorCode.TIMEOUT:
            raise
    except OSError as exc:
        error = MacOSError(str(exc), code="diagnostic.command").to_json()
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    returncode = None if process is None else process.returncode
    if error is None and returncode != 0 and not truncated:
        error = MacOSError("Diagnostic command failed", code="diagnostic.command",
                           details={"returncode": returncode}).to_json()
    return _CommandRead(bytes(out), bytes(err), returncode, truncated, error)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("A diagnostic timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace(" -", "-").replace(" +", "+"))
    if parsed.tzinfo is None:
        raise ValueError("A diagnostic timestamp needs a timezone")
    return parsed.astimezone(UTC)


def _interval(subject: Receipt | tuple[str, str]) -> tuple[datetime, datetime]:
    if isinstance(subject, Receipt):
        if subject.started_at is None or subject.finished_at is None:
            raise MacOSError("Receipt has no completed wall-clock interval", code=ErrorCode.BAD_REQUEST)
        start, end = _timestamp(subject.started_at), _timestamp(subject.finished_at)
        start, end = start - timedelta(milliseconds=250), end + timedelta(milliseconds=250)
    else:
        try:
            start, end = (_timestamp(value) for value in subject)
        except (ValueError, TypeError) as exc:
            raise MacOSError("Pass a start/end pair of timezone-aware timestamps", code=ErrorCode.BAD_REQUEST) from exc
    if start > end:
        raise MacOSError("Diagnostic interval ends before it starts", code=ErrorCode.BAD_REQUEST)
    return start, end


def receipt_pid(receipt: Receipt) -> int:
    process, target = receipt.process, receipt.target
    info = process if isinstance(process, Mapping) else target.get("app") if isinstance(target, Mapping) else None
    pid = info.get("pid") if isinstance(info, Mapping) else None
    if type(pid) is not int or pid <= 0:
        raise MacOSError("Receipt has no resolved pid", code=ErrorCode.BAD_REQUEST)
    return pid


def collect_logs(
    subject: Receipt | tuple[str, str], pid: int, *, subsystem: str | None = None,
    category: str | None = None, level: str = "info", limit: int = 200,
    timeout: float = 5.0, max_bytes: int = 1024 * 1024,
) -> dict[str, JSONValue]:
    start, end = _interval(subject)
    if level not in ("default", "info", "debug") or not 1 <= limit <= 2000:
        raise MacOSError("Use default/info/debug and a row limit between 1 and 2000", code=ErrorCode.BAD_REQUEST)
    predicate = f"processID == {pid}"
    for key, value in (("subsystem", subsystem), ("category", category)):
        if value is not None:
            if not isinstance(value, str):
                raise MacOSError(f"{key} must be a string", code=ErrorCode.BAD_REQUEST)
            predicate += f" AND {key} == {json.dumps(value, ensure_ascii=False)}"
    query_end = datetime.fromtimestamp(math.ceil(end.timestamp()), UTC)
    command = ["/usr/bin/log", "show", "--start", start.strftime("%Y-%m-%d %H:%M:%S%z"),
               "--end", query_end.strftime("%Y-%m-%d %H:%M:%S%z"), "--timezone", "UTC",
               "--predicate", predicate, "--style", "ndjson"]
    if level != "default":
        command.append("--info")
    if level == "debug":
        command.append("--debug")
    raw = _read_command(command, timeout=timeout, max_bytes=max_bytes)
    rows: list[JSONValue] = []
    finished = False
    truncated = raw.truncated
    malformed = 0
    matching = 0
    for line in raw.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise TypeError("not an event")
            if "timestamp" not in event:
                finished = event.get("finished") == 1
                continue
            timestamp = _timestamp(event["timestamp"])
        except (ValueError, TypeError, KeyError):
            malformed += 1
            continue
        if not start <= timestamp <= end or event.get("processID") != pid:
            continue
        matching += 1
        if len(rows) == limit:
            truncated = True
            continue
        row = {key: event.get(key) for key in ("eventMessage", "messageType", "eventType", "processID", "subsystem", "category")}
        message = row["eventMessage"]
        if isinstance(message, str) and len(message) > 4000:
            row["eventMessage"] = message[:4000]
            row["message_truncated"] = True
            truncated = True
        rows.append({"timestamp": timestamp.isoformat(), **row})
    return {
        "kind": "logs", "status": "failed" if raw.error is not None else "partial" if truncated or malformed or not finished else "ok",
        "pid": pid, "requested": {"start": start.isoformat(), "end": end.isoformat()},
        "queried_at": _utc_timestamp(time.time()), "rows": rows, "truncated": truncated,
        "coverage": {"query_finished": finished, "matching_rows": matching, "malformed_rows": malformed,
                     "end_in_future": end.timestamp() > time.time()},
        "returncode": raw.returncode, "stderr": raw.stderr.decode("utf-8", errors="replace")[:2000],
        "error": raw.error,
    }


def _public_fields(value: object, names: Sequence[str]) -> dict[str, JSONValue]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, JSONValue] = {}
    for name in names:
        item = value.get(name)
        if isinstance(item, (str, int, bool)):
            result[name] = item[:500] if isinstance(item, str) else item
    return result


def _decode_crash(
    raw: bytes, pid: int, start: datetime, end: datetime, launched_at: float | None,
) -> dict[str, JSONValue] | None:
    text = raw.decode("utf-8")
    _header, offset = json.JSONDecoder().raw_decode(text)
    body = json.loads(text[offset:].lstrip())
    if not isinstance(body, Mapping):
        raise TypeError("Crash body is not an object")
    if body.get("pid") != pid:
        return None
    captured = _timestamp(body.get("captureTime"))
    if not start <= captured <= end:
        return None
    launch = _timestamp(body["procLaunch"]) if body.get("procLaunch") is not None else None
    if launched_at is not None and launch is not None and abs(launch.timestamp() - launched_at) > 0.001:
        return None
    threads, faulting = body.get("threads"), body.get("faultingThread")
    frames = []
    if isinstance(threads, list) and isinstance(faulting, int) and not isinstance(faulting, bool) and 0 <= faulting < len(threads):
        thread = threads[faulting]
        if isinstance(thread, Mapping) and isinstance(thread.get("frames"), list):
            frames = thread["frames"]
    images = body.get("usedImages")
    stack: list[JSONValue] = []
    for frame in frames[:10]:
        if not isinstance(frame, Mapping):
            continue
        row = _public_fields(frame, ("symbol", "symbolLocation", "imageOffset"))
        image_index = frame.get("imageIndex")
        if isinstance(images, list) and isinstance(image_index, int) and not isinstance(image_index, bool) and 0 <= image_index < len(images):
            image = images[image_index]
            if isinstance(image, Mapping) and isinstance(image.get("name"), str):
                row["image"] = Path(image["name"]).name[:200]
        stack.append(row)
    return {
        "pid": pid, "captured_at": captured.isoformat(),
        "identity_match": "pid_launch_and_time" if launched_at is not None and launch is not None else "pid_and_time",
        "exception": _public_fields(body.get("exception"), ("type", "signal")),
        "termination": _public_fields(body.get("termination"), ("namespace", "code", "indicator")),
        "frames": stack, "frames_truncated": len(frames) > 10,
    }


def collect_crashes(
    subject: Receipt | tuple[str, str], pid: int, *, directories: Sequence[Path],
    limit: int = 3, max_files: int = 128,
) -> dict[str, JSONValue]:
    start, end = _interval(subject)
    if not 1 <= limit <= 20 or not 1 <= max_files <= 512:
        raise MacOSError("Use a report limit of 1..20 and a file limit of 1..512", code=ErrorCode.BAD_REQUEST)
    process = subject.process if isinstance(subject, Receipt) else None
    launched = process.get("launched_at") if isinstance(process, Mapping) else None
    launched_at = float(launched) if isinstance(launched, (int, float)) and not isinstance(launched, bool) else None
    rows: list[JSONValue] = []
    entries_examined = files_read = bytes_read = malformed = unreadable = 0
    truncated = False
    byte_budget = 8 * 1024 * 1024
    for directory_index, directory in enumerate(directories):
        try:
            with os.scandir(directory) as entries:
                candidates = list(islice(entries, max_files - entries_examined + 1))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable += 1
            continue
        allowed = max_files - entries_examined
        if len(candidates) > allowed:
            truncated = True
            candidates = candidates[:allowed]
        entries_examined += len(candidates)
        for entry in candidates:
            if not entry.name.endswith(".ips"):
                continue
            if byte_budget <= 0:
                truncated = True
                break
            try:
                with os.fdopen(os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        continue
                    cap = min(byte_budget, 2 * 1024 * 1024)
                    raw = source.read(cap + 1)
            except OSError:
                unreadable += 1
                continue
            files_read += 1
            bytes_read += len(raw)
            byte_budget -= len(raw)
            if len(raw) > cap:
                truncated = True
                continue
            try:
                report = _decode_crash(raw, pid, start, end, launched_at)
            except (ValueError, TypeError, KeyError):
                malformed += 1
                continue
            if report is not None:
                rows.append({"file": entry.name, **report})
        if entries_examined == max_files or byte_budget <= 0:
            truncated = truncated or directory_index + 1 < len(directories) or byte_budget <= 0
            break
    rows.sort(key=lambda row: row["captured_at"], reverse=True)
    truncated = truncated or len(rows) > limit
    return {
        "kind": "crashes", "status": "partial" if truncated or malformed or unreadable else "ok",
        "pid": pid, "requested": {"start": start.isoformat(), "end": end.isoformat()},
        "queried_at": _utc_timestamp(time.time()), "rows": rows[:limit],
        "not_found_at_lookup": not rows, "truncated": truncated,
        "coverage": {"entries_examined": entries_examined, "files_read": files_read,
                     "bytes_read": bytes_read, "malformed": malformed, "unreadable": unreadable},
    }


def collect_sample(pid: int, *, duration: int = 1, timeout: float = 5.0, max_bytes: int = 1024 * 1024) -> dict[str, JSONValue]:
    if type(duration) is not int or not 1 <= duration <= 5:
        raise MacOSError("Sample duration must be 1..5 seconds", code=ErrorCode.BAD_REQUEST)
    raw = _read_command(["/usr/bin/sample", str(pid), str(duration), "10", "-mayDie", "-file", "/dev/stdout"],
                        timeout=timeout, max_bytes=max_bytes)
    lines = raw.stdout.decode("utf-8", errors="replace").splitlines()
    graph: list[str] = []
    collecting = False
    truncated = raw.truncated
    for line in lines:
        if line == "Call graph:":
            collecting = True
            continue
        if collecting and line.startswith(("Total number", "Binary Images:")):
            break
        if collecting:
            if len(graph) == 160:
                truncated = True
                break
            graph.append(line[:1000])
            truncated = truncated or len(line) > 1000
    error = raw.error
    if error is None and not collecting:
        error = MacOSError("Sample returned no call graph", code="diagnostic.parse").to_json()
    return {
        "kind": "sample", "status": "failed" if error is not None else "partial" if truncated else "ok",
        "pid": pid, "duration_s": duration, "interval_ms": 10,
        "queried_at": _utc_timestamp(time.time()), "call_graph": "\n".join(graph).strip(),
        "truncated": truncated, "returncode": raw.returncode,
        "stderr": raw.stderr.decode("utf-8", errors="replace")[:2000], "error": error,
    }


def explain(receipt: Receipt, evidence: Sequence[Mapping[str, JSONValue]]) -> dict[str, JSONValue]:
    findings: list[JSONValue] = []
    if isinstance(receipt.process, Mapping) and receipt.process.get("state") == "exited":
        findings.append({"kind": "app.exited", "observed_at": receipt.finished_at})
    for item in evidence:
        process = item.get("process")
        pid = item.get("pid")
        if pid is None and isinstance(process, Mapping):
            pid = process.get("pid")
        if pid != receipt_pid(receipt):
            raise MacOSError("Diagnostic evidence belongs to another pid", code=ErrorCode.BAD_REQUEST)
        if (isinstance(process, Mapping) and isinstance(receipt.process, Mapping)
                and process.get("launched_at") != receipt.process.get("launched_at")):
            raise MacOSError("Diagnostic evidence belongs to another process incarnation", code=ErrorCode.BAD_REQUEST)
        interval = item.get("requested")
        if isinstance(interval, Mapping) and receipt.started_at is not None and receipt.finished_at is not None:
            start, end = _interval(receipt)
            if _timestamp(interval.get("end")) < start or _timestamp(interval.get("start")) > end:
                raise MacOSError("Diagnostic evidence falls outside the action interval", code=ErrorCode.BAD_REQUEST)
        if isinstance(process, Mapping) and process.get("state") == "exited":
            findings.append({"kind": "app.exited", "observed_at": item.get("observed_at")})
        if item.get("blocked") is True:
            findings.append({"kind": "ui.blocked", "dialogs": item.get("dialogs")})
        build = item.get("build")
        if isinstance(build, Mapping) and build.get("potentially_stale") is True:
            findings.append({"kind": "build.modified_after_launch", "build": build})
        if item.get("kind") == "crashes" and item.get("rows"):
            findings.append({"kind": "crash.report_found", "reports": item["rows"]})
        if item.get("status") in ("partial", "failed"):
            findings.append({"kind": "diagnostic.incomplete", "source": item.get("kind"), "error": item.get("error")})
    return {"receipt": receipt.to_json(), "findings": findings,
            "input_may_have_happened": receipt.acted is not Acted.NO,
            "next": "Inspect the target before another input" if receipt.acted is not Acted.NO
                    else "Resolve the reported failure before retrying"}


def inspection_findings(state: Mapping[str, JSONValue], receipt: Receipt | None) -> dict[str, JSONValue]:
    nodes = state.get("nodes")
    controls = [node for node in nodes if isinstance(node, Mapping)] if isinstance(nodes, list) else []
    coverage = state.get("coverage")
    complete = isinstance(coverage, Mapping) and coverage.get("complete") is True
    blocking = [node for node in controls if node.get("role") == "AXSheet" or node.get("modal") is True]
    dialogs = [node for node in controls if node.get("role") == "AXSheet"
               or node.get("subrole") in ("AXDialog", "AXSystemDialog") or node.get("modal") is True]
    result: dict[str, JSONValue] = {
        "blocked": True if blocking else False if complete else None,
        "dialogs": [{key: node[key] for key in _CONTROL_FIELDS if key in node} for node in dialogs[:8]],
        "ax_status": "complete" if complete else "partial" if controls else "unavailable",
    }
    if receipt is not None and receipt.outcome is Outcome.FAILED:
        scope = receipt.request
        condition = scope.get("condition" if receipt.op == "expect" else "postcondition")
        if (receipt.op == "expect" or receipt.acted is not Acted.NO) and isinstance(condition, Mapping):
            scope = condition
        role = scope.get("role") or scope.get("search_key")
        if isinstance(role, str):
            role = role.removesuffix("SearchKey").removeprefix("AX").replace(" ", "").casefold()
        nearby = [node for node in controls if not role or role == "anytype"
                  or str(node.get("role", "")).removeprefix("AX").casefold() == role]
        result["nearby"] = [{key: node[key] for key in _CONTROL_FIELDS if key in node} for node in nearby[:8]]
        result["previous_observation"] = {"finished_at": receipt.finished_at, "observed": receipt.to_json()["observed"]}
    return result


def _window_index(state: Mapping[str, JSONValue]) -> dict[int, Mapping[str, JSONValue]]:
    windows = state.get("windows")
    if not isinstance(windows, (list, tuple)):
        raise MacOSError("Snapshot has no observed windows", code=ErrorCode.BAD_REQUEST)
    result: dict[int, Mapping[str, JSONValue]] = {}
    for window in windows:
        if not isinstance(window, Mapping) or type(window.get("window_id")) is not int:
            raise MacOSError("Snapshot contains a window without an integer id", code=ErrorCode.BAD_REQUEST)
        window_id = window["window_id"]
        if window_id in result:
            raise MacOSError("Snapshot contains duplicate window ids", code=ErrorCode.BAD_REQUEST)
        result[window_id] = window
    return result


def diff_windows(before: Mapping[str, JSONValue], after: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    for key, identity_field in (("app", "pid"), ("process", "launched_at")):
        left, right = before.get(key), after.get(key)
        if isinstance(left, Mapping) and isinstance(right, Mapping) and left.get(identity_field) != right.get(identity_field):
            raise MacOSError("Snapshots belong to different app processes", code=ErrorCode.BAD_REQUEST)
    old, new = _window_index(before), _window_index(after)
    changed: list[JSONValue] = []
    for window_id in sorted(old.keys() & new.keys()):
        fields = {key: {"before": old[window_id].get(key), "after": new[window_id].get(key)}
                  for key in ("title", "bounds", "on_screen", "alpha")
                  if old[window_id].get(key) != new[window_id].get(key)}
        if fields:
            changed.append({"window_id": window_id, "fields": fields})
    return {
        "opened": [dict(new[key]) for key in sorted(new.keys() - old.keys())],
        "closed": [dict(old[key]) for key in sorted(old.keys() - new.keys())],
        "changed": changed,
    }
