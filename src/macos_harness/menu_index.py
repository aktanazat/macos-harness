"""An app's whole menu bar, read once through Accessibility without opening
a menu, kept per app version, and searched by text."""

from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ._paths import config_dir
from .errors import ErrorCode, MacOSError
from .macos import _menu_shortcut
from .receipts import JSONValue

_SCHEMA = 1
_ITEM_ATTRIBUTES = (
    "AXTitle", "AXDescription", "AXEnabled", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers",
    "AXChildren",
)
_MATCH_ATTRIBUTES = (
    "AXTitle", "AXDescription", "AXEnabled", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers",
)
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_AMBIGUOUS_SHOWN = 10


class _MenuHost(Protocol):
    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, JSONValue]]: ...
    def _bundle_version(self, path: str | None) -> tuple[str | None, str | None]: ...
    def _application_element(self, pid: int, *, enhance: bool = True) -> object: ...
    def _copy_attributes(
        self, element: object, attributes: Iterable[str], *, checked: bool = False
    ) -> Mapping[str, object]: ...
    def _menu_entries(self, node: object) -> list[object]: ...
    def _menu_entry_at(self, node: object, position: int) -> object | None: ...
    def _remember_element(self, element: object) -> int: ...
    def _describe_element(
        self, element: object, element_index: int, *, attributes: Iterable[str], include_actions: bool
    ) -> dict[str, JSONValue]: ...


@dataclass(frozen=True, slots=True)
class _Index:
    pid: int
    app: str
    key: dict[str, JSONValue]
    read_at: str
    items: list[dict[str, JSONValue]]
    cached: bool

    def to_json(self) -> dict[str, JSONValue]:
        return {
            "app": self.app, "pid": self.pid, **self.key, "read_at": self.read_at,
            "cached": self.cached, "items": [_public(item) for item in self.items],
        }


def _label(text: str) -> str:
    """``text`` as a search compares it: case, runs of spaces, and the
    "…" or "..." of an item that opens a dialog ignored."""
    return " ".join(text.replace("...", " ").replace("…", " ").split()).casefold()


def _path(item: Mapping[str, JSONValue]) -> str:
    steps = item["steps"]
    assert isinstance(steps, list)
    return " > ".join(str(step) for step in steps)


def _public(item: Mapping[str, JSONValue], *, enabled: bool | None = None) -> dict[str, JSONValue]:
    entry: dict[str, JSONValue] = {
        "path": _path(item),
        "enabled": item["enabled"] if enabled is None else enabled,
    }
    for key in ("shortcut", "submenu"):
        if key in item:
            entry[key] = item[key]
    return entry


def _read_items(host: _MenuHost, pid: int) -> list[dict[str, JSONValue]]:
    """Every titled item of ``pid``'s menu bar, menus first and each
    followed by what it holds, with the position of each step among its
    siblings. The Apple menu, the system's own, is left out."""
    root = host._application_element(pid, enhance=False)
    bar = host._copy_attributes(root, ("AXMenuBar",), checked=True).get("AXMenuBar")
    items: list[dict[str, JSONValue]] = []

    def walk(node: object, steps: list[JSONValue], at: list[JSONValue]) -> None:
        for position, entry in enumerate(host._menu_entries(node)):
            values = host._copy_attributes(entry, _ITEM_ATTRIBUTES)
            title = values["AXTitle"] or values["AXDescription"]
            if not isinstance(title, str) or not title or (not steps and position == 0 and title == "Apple"):
                continue
            item: dict[str, JSONValue] = {
                "steps": [*steps, title],
                "at": [*at, position],
                "enabled": values["AXEnabled"] is not False,
            }
            shortcut = _menu_shortcut(values["AXMenuItemCmdChar"], values["AXMenuItemCmdModifiers"])
            if shortcut is not None:
                item["shortcut"] = shortcut
            items.append(item)
            if values["AXChildren"]:
                item["submenu"] = True
                walk(entry, [*steps, title], [*at, position])

    walk(bar, [], [])
    return items


def _cache_file(bundle_id: object) -> Path | None:
    if not isinstance(bundle_id, str) or not _BUNDLE_ID.fullmatch(bundle_id):
        return None
    return config_dir() / "menus" / f"{bundle_id}.json"


def _well_formed(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    steps, at = item.get("steps"), item.get("at")
    return (
        isinstance(steps, list) and isinstance(at, list) and len(steps) == len(at) > 0
        and all(isinstance(step, str) for step in steps)
        and all(isinstance(position, int) and not isinstance(position, bool) for position in at)
        and isinstance(item.get("enabled"), bool)
    )


def _load(path: Path, key: Mapping[str, JSONValue]) -> tuple[str, list[dict[str, JSONValue]]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA or data.get("key") != key:
        return None
    items, read_at = data.get("items"), data.get("read_at")
    if not isinstance(items, list) or not isinstance(read_at, str) or not all(map(_well_formed, items)):
        return None
    return read_at, items


def _save(path: Path, key: Mapping[str, JSONValue], read_at: str, items: list[dict[str, JSONValue]]) -> None:
    text = json.dumps(
        {"schema": _SCHEMA, "key": key, "read_at": read_at, "items": items},
        ensure_ascii=False, separators=(",", ":"),
    )
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as destination:
                destination.write(text)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn(f"macos-harness menu index not cached: {exc}", RuntimeWarning, stacklevel=4)


def read_index(host: _MenuHost, app: str | int, *, refresh: bool = False) -> _Index:
    """``app``'s menu index: from the cache when one holds the same bundle
    version and ``refresh`` is False, else read now and cached."""
    _, info = host._resolve_app(app)
    pid = int(info["pid"])
    path_value = info.get("path")
    version, build = host._bundle_version(path_value if isinstance(path_value, str) else None)
    key: dict[str, JSONValue] = {"bundle_id": info.get("bundle_id"), "version": version, "build": build}
    cache = _cache_file(info.get("bundle_id"))
    stored = None if refresh or cache is None else _load(cache, key)
    if stored is not None:
        return _Index(pid, str(info["name"]), key, stored[0], stored[1], cached=True)
    items = _read_items(host, pid)
    read_at = datetime.now(UTC).isoformat(timespec="seconds")
    if cache is not None:
        _save(cache, key, read_at, items)
    return _Index(pid, str(info["name"]), key, read_at, items, cached=False)


def _ranked(items: list[dict[str, JSONValue]], text: str) -> list[tuple[int, dict[str, JSONValue]]]:
    """The items ``text`` matches, best first: its exact title or path,
    then a title it starts, then a title holding every word of it, then a
    path holding every word; shallower and earlier items first within each."""
    phrase = _label(text)
    words = [word for word in re.split(r"[\s>]+", phrase) if word]
    if not words:
        raise MacOSError(
            "Search the menus for some text, as in 'plain text'",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "text"},
        )
    scored: list[tuple[int, int, int, dict[str, JSONValue]]] = []
    for order, item in enumerate(items):
        steps = item["steps"]
        assert isinstance(steps, list)
        title, path = _label(str(steps[-1])), _label(_path(item))
        if phrase in (title, path):
            tier = 0
        elif title.startswith(phrase):
            tier = 1
        elif all(word in title for word in words):
            tier = 2
        elif all(word in path for word in words):
            tier = 3
        else:
            continue
        scored.append((tier, len(steps), order, item))
    scored.sort(key=lambda entry: entry[:3])
    return [(tier, item) for tier, _, _, item in scored]


def _live(host: _MenuHost, pid: int, item: Mapping[str, JSONValue]) -> object | None:
    """The element ``item`` names in ``pid``'s menu bar now, found by its
    positions and checked step by step against its titles; None when the
    menu no longer holds it there."""
    steps, at = item["steps"], item["at"]
    assert isinstance(steps, list) and isinstance(at, list)
    root = host._application_element(pid, enhance=False)
    node = host._copy_attributes(root, ("AXMenuBar",), checked=True).get("AXMenuBar")
    for position, title in zip(at, steps, strict=True):
        assert isinstance(position, int)
        node = host._menu_entry_at(node, position)
        if node is None:
            return None
        labels = host._copy_attributes(node, ("AXTitle", "AXDescription"))
        if (labels["AXTitle"] or labels["AXDescription"]) != title:
            return None
    return node


def _enabled(host: _MenuHost, node: object) -> bool:
    return host._copy_attributes(node, ("AXEnabled",))["AXEnabled"] is not False


def search(
    host: _MenuHost, app: str | int, text: str, *, limit: int, refresh: bool
) -> list[dict[str, JSONValue]]:
    """The ``limit`` items of ``app``'s menus that best match ``text``,
    each checked against the menu bar as it is now. A cached index that
    finds nothing, or an item the menus no longer hold, is read again."""
    if limit < 1:
        raise MacOSError(
            f"limit is how many items to return, at least 1, not {limit!r}",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "limit"},
        )
    index = read_index(host, app, refresh=refresh)
    while True:
        hits = [item for _, item in _ranked(index.items, text)[:limit]]
        nodes = [_live(host, index.pid, item) for item in hits]
        if not index.cached or (hits and None not in nodes):
            break
        index = read_index(host, index.pid, refresh=True)
    return [
        _public(item, enabled=_enabled(host, node))
        for item, node in zip(hits, nodes, strict=True)
        if node is not None
    ]


def find(host: _MenuHost, pid: int, text: str) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """The one item that best matches ``text`` in ``pid``'s menus, as a
    search match of its live element and as its index entry. Several
    equally good matches, or a best match that opens a submenu, are
    refused, so a weaker match is never chosen in its place; a cached
    index that finds none, or finds one the menus no longer hold, is read
    again."""
    index = read_index(host, pid)
    while True:
        ranked = _ranked(index.items, text)
        best = [item for tier, item in ranked if tier == ranked[0][0]] if ranked else []
        if len(best) > 1:
            paths = [_path(item) for item in best]
            raise MacOSError(
                f"{len(best)} menu items match {text!r} equally: "
                f"{'; '.join(paths[:_AMBIGUOUS_SHOWN])}; search for more of the one you mean",
                code=ErrorCode.BAD_REQUEST,
                details={"count": len(best), "paths": paths[:_AMBIGUOUS_SHOWN]},
            )
        node = _live(host, pid, best[0]) if best else None
        if node is not None and "submenu" in best[0]:
            raise MacOSError(
                f"{_path(best[0])} opens a submenu; search for the item in it you mean, "
                "as mac.menus(app, path) lists them",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "text", "path": _path(best[0])},
            )
        if node is not None:
            return (
                host._describe_element(
                    node, host._remember_element(node), attributes=_MATCH_ATTRIBUTES,
                    include_actions=False,
                ),
                best[0],
            )
        if not index.cached:
            raise MacOSError(
                f"No menu item of {index.app} matches {text!r}; mac.menu_index(app) lists them all",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"pid": pid, "text": text},
            )
        index = read_index(host, pid, refresh=True)
