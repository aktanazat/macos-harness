"""``mac.data``: what an app stored, read without its window.

Spotlight's index, Shortcuts, preference domains, property lists and
SQLite stores answer many questions faster than the GUI and, after a
save, say whether the data really changed. Every reader here is
read-only; nothing writes to an app's store.
"""

from __future__ import annotations

import errno
import plistlib
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import objc
from Foundation import (
    NSURL,
    NSArray,
    NSDate,
    NSDefaultRunLoopMode,
    NSDictionary,
    NSMetadataItem,
    NSMetadataQuery,
    NSMetadataQueryLocalComputerScope,
    NSPredicate,
    NSRunLoop,
)

from ._pool import pooled_methods
from .errors import ErrorCode, MacOSError
from .receipts import JSONValue

__all__ = ["AppData"]

_POLL_SECONDS = 0.1
_RUN_LOOP_SLICE = 0.005
_SHORTCUTS = "/usr/bin/shortcuts"
_DEFAULTS = "/usr/bin/defaults"
_NAMED_FIELDS = {
    "name": "kMDItemFSName",
    "kind": "kMDItemKind",
    "content_type": "kMDItemContentType",
    "content_types": "kMDItemContentTypeTree",
    "size": "kMDItemFSSize",
    "where_froms": "kMDItemWhereFroms",
    "authors": "kMDItemAuthors",
    "created": "kMDItemFSCreationDate",
    "modified": "kMDItemFSContentChangeDate",
    "added": "kMDItemDateAdded",
    "downloaded": "kMDItemDownloadedDate",
    "last_used": "kMDItemLastUsedDate",
}
_LIST_FIELDS = frozenset({"content_types", "where_froms", "authors"})


def _bad_request(message: str, **details: JSONValue) -> MacOSError:
    return MacOSError(message, code=ErrorCode.BAD_REQUEST, details=details)


def _hidden(path: Path) -> MacOSError:
    return MacOSError(
        f"macOS hides {str(path)!r} from this process: it is another app's data, readable only "
        "to a process granted access to other apps' data or Full Disk Access in Privacy & "
        "Security; read the value from the app's window instead",
        code=ErrorCode.UNSUPPORTED_OP,
        details={"path": str(path)},
    )


def _open(path: Path) -> BinaryIO:
    """``path`` opened for reading; macOS's privacy guard on another app's
    data refuses with EPERM, where file modes refuse with EACCES."""
    try:
        return path.open("rb")
    except PermissionError as exc:
        if exc.errno == errno.EPERM:
            raise _hidden(path) from None
        raise


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="milliseconds")


def _plain(value: object) -> JSONValue:
    """A Spotlight value as JSON: dates as UTC ISO text, arrays as lists."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, NSDate):
        return _iso(value.timeIntervalSince1970())
    if isinstance(value, NSArray):
        return [_plain(item) for item in value]
    if isinstance(value, NSDictionary):
        return {str(key): _plain(item) for key, item in value.items()}
    return None


def _literal(value: str) -> str:
    """``value`` escaped to match itself inside a Spotlight query's quotes."""
    return "".join("\\" + char if char in '\\"*?' else char for char in value)


def _query_text(query: str | None, name: str | None, kind: str | None, content: str | None) -> str:
    clauses = [f"({query})"] if query else []
    if name:
        clauses.append(f'kMDItemFSName == "*{_literal(name)}*"cd')
    if kind:
        clauses.append(f'kMDItemContentTypeTree == "{_literal(kind)}"')
    if content:
        clauses.append(f'kMDItemTextContent == "{_literal(content)}"cdw')
    if not clauses:
        raise _bad_request("find_files needs a query, name, kind, or content")
    return " && ".join(clauses)


def _predicate(text: str) -> NSPredicate:
    try:
        return NSPredicate.predicateFromMetadataQueryString_(text)
    except objc.error as exc:
        raise _bad_request(
            f"Spotlight cannot read the query {text!r}; query takes attribute syntax such as "
            "kMDItemDisplayName == \"*.pdf\"c, and words go in content= or name=",
            query=text,
        ) from exc


def _scopes(onlyin: str | Path | Iterable[str | Path] | None) -> list[str]:
    if onlyin is None:
        return [NSMetadataQueryLocalComputerScope]
    folders = [onlyin] if isinstance(onlyin, str | Path) else list(onlyin)
    scopes = []
    for folder in folders:
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise _bad_request(f"onlyin {str(folder)!r} is not a folder", onlyin=str(folder))
        scopes.append(str(path.resolve()))
    return scopes


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _stamp(path: Path | None) -> tuple[int, int] | None:
    """A file's modification time and size, or None when it is absent."""
    if path is None or not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _gather(predicate: NSPredicate, scopes: list[str], count: int, stop: threading.Event) -> tuple[list[str], int]:
    """Run one Spotlight query on this thread's run loop until it has
    gathered or ``stop`` is set; the first ``count`` paths and the total."""
    with objc.autorelease_pool():
        search = NSMetadataQuery.alloc().init()
        search.setPredicate_(predicate)
        search.setSearchScopes_(scopes)
        if not search.startQuery():
            raise _bad_request("Spotlight would not start the query", query=str(predicate))
        loop = NSRunLoop.currentRunLoop()
        try:
            while search.isGathering() and not stop.is_set():
                loop.runMode_beforeDate_(NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(_RUN_LOOP_SLICE))
        finally:
            search.stopQuery()
        total = search.resultCount()
        paths = [
            str(search.resultAtIndex_(index).valueForAttribute_("kMDItemPath"))
            for index in range(min(total, count))
        ]
        return paths, total


def _settle(future: Future[tuple[list[str], int]], work: Callable[[], tuple[list[str], int]]) -> None:
    try:
        future.set_result(work())
    except (MacOSError, objc.error) as exc:
        future.set_exception(exc)


_READS = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE})


def _reads_only(action: int, *_: str | None) -> int:
    return sqlite3.SQLITE_OK if action in _READS else sqlite3.SQLITE_DENY


def _domain_args(domain: str, current_host: bool) -> list[str]:
    if not domain or (domain.startswith("-") and domain != "-g"):
        raise _bad_request(f"{domain!r} is not a preference domain", domain=domain)
    return [_DEFAULTS, *(["-currentHost"] if current_host else []), "export", domain, "-"]


@pooled_methods
class AppData:
    """``mac.data``: Spotlight, Shortcuts, and read-only app stores."""

    # --- Spotlight -----------------------------------------------------

    def find_files(
        self,
        query: str | None = None,
        *,
        name: str | None = None,
        kind: str | None = None,
        content: str | None = None,
        onlyin: str | Path | Iterable[str | Path] | None = None,
        limit: int = 1000,
        timeout: float = 10.0,
    ) -> dict[str, JSONValue]:
        """Files Spotlight has indexed, as ``mdfind`` finds them, without
        starting a process. ``query`` is raw ``mdfind`` query syntax;
        ``name`` matches part of a file name, ``kind`` a content type such
        as ``com.adobe.pdf`` or ``public.image`` with its subtypes,
        ``content`` a word in the text. Every one given must match.
        Returns ``paths`` (at most ``limit``) and ``total``; raises
        ``timeout`` when Spotlight has not answered within ``timeout``."""
        text = _query_text(query, name, kind, content)
        scopes = _scopes(onlyin)
        if query:
            _predicate(query)
        predicate = _predicate(text)
        count = max(limit, 0)
        started = time.monotonic()
        stop = threading.Event()
        future: Future[tuple[list[str], int]] = Future()
        threading.Thread(
            target=_settle,
            args=(future, lambda: _gather(predicate, scopes, count, stop)),
            name="macos-harness-spotlight",
            daemon=True,
        ).start()
        try:
            paths, total = future.result(timeout=timeout)
        except FutureTimeout:
            stop.set()
            raise MacOSError(
                f"Spotlight did not answer within {timeout:g}s; narrow the query or onlyin, "
                "or wait for indexing to finish",
                code=ErrorCode.TIMEOUT,
                details={"query": text, "timeout": timeout},
            ) from None
        return {
            "query": text,
            "paths": list[JSONValue](paths),
            "total": total,
            "seconds": round(time.monotonic() - started, 4),
        }

    def file_info(self, path: str | Path) -> dict[str, JSONValue]:
        """One file's Spotlight metadata, as ``mdls`` prints it: where it
        was downloaded from, its authors, content type, and dates (UTC ISO
        text), then every public attribute under ``attributes``.
        ``indexed`` is False for a file Spotlight has not read, which
        carries only file-system fields."""
        resolved = Path(path).expanduser()
        item = NSMetadataItem.alloc().initWithURL_(NSURL.fileURLWithPath_(str(resolved)))
        if item is None:
            raise _bad_request(f"No file at {str(path)!r}", path=str(path))
        names = [name for name in item.attributes() if name.startswith("kMDItem") and not name.endswith("_Ranking")]
        values = item.valuesForAttributes_(names)
        attributes = {str(name): _plain(value) for name, value in sorted(values.items())}
        info: dict[str, JSONValue] = {"path": str(resolved), "indexed": "kMDItemContentType" in attributes}
        for field, attribute in _NAMED_FIELDS.items():
            match attributes.get(attribute):
                case list() as items if field in _LIST_FIELDS:
                    info[field] = items
                case _ if field in _LIST_FIELDS:
                    info[field] = []
                case [first, *_]:
                    info[field] = first
                case []:
                    info[field] = None
                case value:
                    info[field] = value
        info["attributes"] = attributes
        return info

    # --- Shortcuts -----------------------------------------------------

    def shortcuts(self) -> list[dict[str, JSONValue]]:
        """Every shortcut this user has, as ``{"name", "id"}``."""
        result = subprocess.run(
            [_SHORTCUTS, "list", "--show-identifiers"], capture_output=True, check=False, timeout=30
        )
        if result.returncode != 0:
            raise MacOSError(
                _text(result.stderr).strip() or f"shortcuts list exited {result.returncode}",
                details={"exit_status": result.returncode},
            )
        listed = []
        for line in _text(result.stdout).splitlines():
            name, _, identifier = line.rpartition(" (")
            listed.append({"name": name, "id": identifier.removesuffix(")")})
        return listed

    def run_shortcut(
        self,
        name: str,
        *,
        input: str | Path | Sequence[str | Path] | None = None,
        output: str | Path | None = None,
        output_type: str | None = None,
        timeout: float = 60.0,
    ) -> dict[str, JSONValue]:
        """Run a shortcut by name or id in the background, as ``shortcuts
        run`` does, with no Shortcuts window. ``input`` is one file or
        several, ``"-"`` for the caller's stdin, which the run otherwise
        never reads; ``output`` is where its result goes, else it comes back
        as ``stdout``. The result's ``output`` is that path only when the
        run wrote it, else None. A nonzero exit raises with
        ``exit_status`` and ``stderr`` in its details; at ``timeout`` the
        ``shortcuts`` command is stopped and the call raises ``timeout``,
        though actions the shortcut already ran stay done."""
        if not name:
            raise _bad_request("run_shortcut needs a shortcut name or id")
        inputs = [] if input is None else [input] if isinstance(input, str | Path) else list(input)
        command = [_SHORTCUTS, "run", name]
        for item in inputs:
            command += ["--input-path", str(Path(item).expanduser())]
        target = None if output is None else Path(output).expanduser()
        if target is not None:
            command += ["--output-path", str(target)]
        if output_type is not None:
            command += ["--output-type", output_type]
        before = _stamp(target)
        started = time.monotonic()
        try:
            # `shortcuts run` waits on a piped stdin as the shortcut's input.
            result = subprocess.run(
                command,
                stdin=None if "-" in map(str, inputs) else subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise MacOSError(
                f"Shortcut {name!r} was still running after {timeout:g}s; its shortcuts command was stopped",
                code=ErrorCode.TIMEOUT,
                details={"name": name, "timeout": timeout, "stderr": _text(exc.stderr or b"")},
            ) from None
        stderr = _text(result.stderr)
        outcome: dict[str, JSONValue] = {
            "name": name,
            "exit_status": result.returncode,
            "stdout": _text(result.stdout),
            "stderr": stderr,
            "output": str(target) if target is not None and _stamp(target) not in (None, before) else None,
            "seconds": round(time.monotonic() - started, 3),
        }
        if result.returncode != 0:
            raise MacOSError(
                stderr.strip() or f"Shortcut {name!r} exited {result.returncode}",
                details=outcome,
            )
        return outcome

    # --- app stores, read-only -----------------------------------------

    def read_defaults(self, domain: str, key: str | None = None, *, current_host: bool = False) -> object:
        """A preference domain as the app reads it, through ``defaults
        export`` (so values not yet flushed to disk count); one ``key``'s
        value, or None when unset. ``-g`` is the global domain. A sandboxed
        app keeps its settings in its container, which macOS hides from a
        process not granted access to other apps' data: that raises
        ``unsupported_op`` rather than reading as unset."""
        result = subprocess.run(
            _domain_args(domain, current_host), capture_output=True, check=False, timeout=30
        )
        if result.returncode != 0:
            raise MacOSError(
                _text(result.stderr).strip() or f"defaults export exited {result.returncode}",
                details={"domain": domain, "exit_status": result.returncode},
            )
        values = plistlib.loads(result.stdout)
        container = Path.home() / "Library/Containers" / domain / "Data/Library/Preferences" / f"{domain}.plist"
        if not values and container.exists():
            _open(container).close()
        return values if key is None else values.get(key)

    def read_plist(self, path: str | Path) -> object:
        """A property list file, XML or binary, as Python values."""
        with _open(Path(path).expanduser()) as handle:
            return plistlib.load(handle)

    def query_sqlite(
        self,
        path: str | Path,
        sql: str,
        params: Sequence[object] | dict[str, object] = (),
        *,
        immutable: bool = False,
    ) -> list[dict[str, object]]:
        """Rows of one query against an app's SQLite store, opened
        read-only; a statement other than a read (a write, ``attach``,
        ``vacuum``, ``pragma``) is refused as not authorized. A store an
        app has open is read with its write-ahead log, as the app sees it;
        one in that mode with no log beside it (the app closed it) is read
        as is, so no log files are made beside it.
        ``immutable=True`` skips locks and the log: only for a copy or a
        store no process is writing, since it misses recent writes."""
        resolved = Path(path).expanduser()
        if not resolved.is_file():
            raise _bad_request(f"No database at {str(path)!r}", path=str(path))
        with _open(resolved) as handle:
            logged = handle.read(20)[18:20] == b"\x02\x02"
        uri = f"file:{urllib.parse.quote(str(resolved))}?mode=ro"
        if immutable or (logged and not Path(f"{resolved}-wal").exists()):
            uri += "&immutable=1"
        try:
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                connection.set_authorizer(_reads_only)
                cursor = connection.execute(sql, params)
                columns = [column[0] for column in cursor.description or ()]
                return [dict(zip(columns, row, strict=True)) for row in cursor]
        except sqlite3.Error as exc:
            try:
                error = exc.sqlite_errorname
            except AttributeError:
                # Python's own binding and API errors carry no SQLite code.
                error = None
            if error in ("SQLITE_BUSY", "SQLITE_LOCKED"):
                raise MacOSError(
                    f"Another process kept {str(path)!r} locked: {exc}",
                    code=ErrorCode.TIMEOUT,
                    details={"path": str(path), "sqlite_error": error},
                ) from None
            raise _bad_request(f"SQLite refused the query: {exc}", path=str(path), sqlite_error=error) from None

    # --- verify a save against the data ----------------------------------

    def wait_file(
        self,
        path: str | Path,
        *,
        contains: str | bytes | None = None,
        since: float | None = None,
        timeout: float = 10.0,
    ) -> dict[str, JSONValue]:
        """Return once the file at ``path`` exists, was modified at or
        after ``since`` (a `time.time()` taken before the save), and holds
        ``contains``; raise ``timeout`` with what the file held instead.
        A save the app reported but never wrote fails here."""
        target = Path(path).expanduser()
        needle = contains.encode() if isinstance(contains, str) else contains
        started = time.monotonic()
        while True:
            reason = self._unsaved(target, needle, since)
            if reason is None:
                stat = target.stat()
                return {
                    "path": str(target),
                    "size": stat.st_size,
                    "modified": _iso(stat.st_mtime),
                    "seconds": round(time.monotonic() - started, 3),
                }
            if time.monotonic() - started >= timeout:
                break
            time.sleep(_POLL_SECONDS)
        details: dict[str, JSONValue] = {"path": str(target), "reason": reason, "timeout": timeout}
        if target.exists():
            details["modified"] = _iso(target.stat().st_mtime)
        if since is not None:
            details["since"] = _iso(since)
        raise MacOSError(
            f"The save did not reach {target} within {timeout:g}s: {reason.replace('_', ' ')}",
            code=ErrorCode.TIMEOUT,
            details=details,
        )

    @staticmethod
    def _unsaved(target: Path, needle: bytes | None, since: float | None) -> str | None:
        try:
            stat = target.stat()
            if since is not None and stat.st_mtime < since:
                return "not_modified_since"
            if needle is not None and needle not in target.read_bytes():
                return "content_missing"
        except FileNotFoundError:
            return "missing"
        return None

    def wait_value(
        self, read: Callable[[], object], expected: object, *, timeout: float = 10.0
    ) -> dict[str, object]:
        """Call ``read`` until it returns ``expected``, as for a setting or
        a row an app saves: ``read=lambda: mac.data.read_defaults(domain,
        key)``. A missing file counts as not saved yet. Raises ``timeout``
        with the last value read."""
        started = time.monotonic()
        reads = 0
        last: object = None
        missing: str | None = None
        while True:
            reads += 1
            try:
                last = read()
                missing = None
            except FileNotFoundError as exc:
                missing = str(exc)
            if missing is None and last == expected:
                return {
                    "value": last,
                    "reads": reads,
                    "seconds": round(time.monotonic() - started, 3),
                }
            if time.monotonic() - started >= timeout:
                break
            time.sleep(_POLL_SECONDS)
        raise MacOSError(
            f"The stored value was not {expected!r} within {timeout:g}s",
            code=ErrorCode.TIMEOUT,
            details={"last": repr(last), "missing": missing, "reads": reads, "timeout": timeout},
        )
