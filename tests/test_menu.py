"""Menu commands and status items chosen by path, as in "File > Close".
One app's menu bar and status items are a fake Accessibility tree; each
test asserts which item was pressed, or that none was."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError
from macos_harness.ops import Operations
from macos_harness.receipts import Outcome, Receipt

_SONA = 4512
_INFO = {"name": "Sona", "bundle_id": "app.sona", "pid": _SONA, "path": None}
# Each entry is a name and the entries of the menu it opens.
_Entries = tuple[tuple[str, "_Entries"], ...]
_MENU_BAR: _Entries = (
    ("Sona", (("About Sona", ()), ("Settings…", ()))),
    ("File", (("New Note", ()), ("Export", (("PDF", ()), ("Markdown", ()))), ("Close", ()))),
    ("Window", (("Minimize", ()), ("Close", ()), ("Untitled", ()), ("Untitled", ()))),
)
# One icon with no title or description, as many status items are.
_ICON: _Entries = (("", (("Open Sona", ()), ("Settings…", ()))),)
_ICONS: _Entries = (("Sona", (("Open Sona", ()),)), ("Sona Timer", (("Start", ()),)))


class _Sona(MacOS):
    """`MacOS` over Sona's Accessibility tree: the menu bar ``_MENU_BAR``
    and the status items ``status_items``, which, as status items do,
    carry any name as a description. The item at ``disabled`` reads
    disabled. Records each item pressed by its bar and path."""

    def __init__(self, *, disabled: str | None = None, status_items: _Entries = _ICON) -> None:
        super().__init__()
        self.disabled = disabled
        self.tree: dict[object, dict[str, object]] = {}
        self.paths: dict[object, tuple[str, str]] = {}
        self.pressed: list[tuple[str, str]] = []
        self.root = self._node(
            {
                "AXRole": "AXApplication",
                "AXMenuBar": self._bar("AXMenuBar", _MENU_BAR, "AXTitle"),
                "AXExtrasMenuBar": (
                    self._bar("AXExtrasMenuBar", status_items, "AXDescription")
                    if status_items else None
                ),
            },
            ("", ""),
        )

    def _node(self, attributes: dict[str, object], path: tuple[str, str]) -> object:
        node = object()
        self.tree[node] = attributes
        self.paths[node] = path
        return node

    def _bar(self, bar: str, entries: _Entries, label: str) -> object:
        items = self._items(bar, (), entries, label, "AXMenuBarItem")
        return self._node({"AXRole": "AXMenuBar", "AXChildren": items}, (bar, ""))

    def _items(
        self, bar: str, under: tuple[str, ...], entries: _Entries, label: str, role: str
    ) -> list[object]:
        return [self._item(bar, (*under, name), children, label, role) for name, children in entries]

    def _item(
        self, bar: str, path: tuple[str, ...], children: _Entries, label: str, role: str
    ) -> object:
        joined = " > ".join(path)
        attributes: dict[str, object] = {"AXRole": role, label: path[-1], "AXEnabled": joined != self.disabled}
        if children:
            menu = {"AXRole": "AXMenu", "AXChildren": self._items(bar, path, children, "AXTitle", "AXMenuItem")}
            attributes["AXChildren"] = [self._node(menu, (bar, joined))]
        return self._node(attributes, (bar, joined))

    def _ensure_accessibility(self) -> None:
        return None

    def _ensure_post_events(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        if query in ("Sona", _SONA):
            return object(), dict(_INFO)
        raise ApplicationNotFoundError(f"No running app matches {query!r}")

    def _application_element(
        self, pid: int, *, messaging_timeout: float | None = None, enhance: bool = True
    ) -> object:
        return self.root

    def _copy_attributes(
        self, element: object, attributes: Iterable[str], *, checked: bool = False
    ) -> dict[str, object]:
        return {name: self.tree[element].get(name) for name in attributes}

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        assert action == "AXPress"
        self.pressed.append(self.paths[self._element(element_index)])

    def _frontmost_app(self) -> dict[str, object]:
        return {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty", "pid": 1, "path": None}

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> dict[str, object]:
        raise MacOSError("Sona's focus is not read here", code=ErrorCode.AX_ERROR)


@pytest.mark.parametrize(
    ("options", "choose", "pressed"),
    [
        ({}, lambda do: do.menu("Window > Close", app="Sona"), ("AXMenuBar", "Window > Close")),
        ({}, lambda do: do.menu("File > Close", app="Sona"), ("AXMenuBar", "File > Close")),
        ({}, lambda do: do.menu("File > Export > PDF", app="Sona"), ("AXMenuBar", "File > Export > PDF")),
        ({}, lambda do: do.menu("sona >  settings...", app="Sona"), ("AXMenuBar", "Sona > Settings…")),
        ({}, lambda do: do.status_item(app="Sona"), ("AXExtrasMenuBar", "")),
        ({}, lambda do: do.status_item("Settings…", app="Sona"), ("AXExtrasMenuBar", " > Settings…")),
        (
            {"status_items": _ICONS},
            lambda do: do.status_item(app="Sona", name="Sona Timer"),
            ("AXExtrasMenuBar", "Sona Timer"),
        ),
        (
            {"status_items": _ICONS},
            lambda do: do.status_item("start", app="Sona", name="sona timer"),
            ("AXExtrasMenuBar", "Sona Timer > Start"),
        ),
    ],
    ids=[
        "window-close", "file-close", "submenu-item", "any-case-and-three-dots",
        "the-only-status-item", "in-its-menu", "a-named-status-item", "in-a-named-ones-menu",
    ],
)
def test_a_menu_path_presses_the_one_item_it_leads_to(
    options: dict[str, object], choose: Callable[[Operations], Receipt], pressed: tuple[str, str]
) -> None:
    mac = _Sona(**options)

    receipt = choose(mac.do)

    assert (receipt.outcome, mac.pressed) == (Outcome.DONE, [pressed])


@pytest.mark.parametrize(
    ("options", "choose", "code"),
    [
        ({}, lambda do: do.menu("Window > Close All", app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        ({}, lambda do: do.menu("Edit > Copy", app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        ({}, lambda do: do.menu("Window > Untitled", app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        ({}, lambda do: do.menu("File > Close > Now", app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        ({"status_items": _ICONS}, lambda do: do.status_item(app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        ({"status_items": ()}, lambda do: do.status_item(app="Sona"), ErrorCode.ELEMENT_UNKNOWN),
        (
            {"status_items": _ICONS},
            lambda do: do.status_item(app="Sona", name="Notes"),
            ErrorCode.ELEMENT_UNKNOWN,
        ),
        (
            {"disabled": "File > Close"},
            lambda do: do.menu("File > Close", app="Sona"),
            ErrorCode.UNSUPPORTED_OP,
        ),
        ({}, lambda do: do.menu("File", app="Sona"), ErrorCode.BAD_REQUEST),
        ({}, lambda do: do.menu("File >  > Close", app="Sona"), ErrorCode.BAD_REQUEST),
        ({}, lambda do: do.status_item("", app="Sona"), ErrorCode.BAD_REQUEST),
        ({}, lambda do: do.status_item(app="Sona", name=" "), ErrorCode.BAD_REQUEST),
    ],
    ids=[
        "no-such-item", "no-such-menu", "two-items-of-that-name", "under-an-item-with-no-menu",
        "two-status-items-and-no-name", "no-status-items", "no-status-item-of-that-name",
        "disabled", "a-menu-not-an-item", "empty-step", "empty-status-path", "blank-name",
    ],
)
def test_a_menu_path_that_leads_to_no_one_enabled_item_presses_nothing(
    options: dict[str, object], choose: Callable[[Operations], Receipt], code: ErrorCode
) -> None:
    mac = _Sona(**options)

    with pytest.raises(MacOSError) as caught:
        choose(mac.do)

    assert (caught.value.code, mac.pressed) == (code, [])


def test_a_dry_run_names_the_item_a_menu_path_leads_to_and_presses_nothing() -> None:
    mac = _Sona()

    receipt = mac.do.menu("Window > Close", app="Sona", dry_run=True)

    assert (receipt.outcome, receipt.to_json()["target"]["title"], mac.pressed) == (
        Outcome.PLANNED, "Close", []
    )
