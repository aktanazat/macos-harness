"""Menu commands and status items chosen by path, as in "File > Close".
One app's menu bar and status items are a fake Accessibility tree; each
test asserts which item was pressed, or that none was."""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError
from macos_harness.ops import Operations
from macos_harness.receipts import OperationError, Outcome, Receipt

_SONA = 4512
_INFO = {"name": "Sona", "bundle_id": "app.sona", "pid": _SONA, "path": None}
# Each entry is a name and the entries of the menu it opens.
_Entries = tuple[tuple[str, "_Entries"], ...]
_MENU_BAR: _Entries = (
    ("Sona", (("About Sona", ()), ("Settings…", ()), ("Sona beenden", ()))),
    (
        "File",
        (("New Note", ()), ("", ()), ("Export", (("PDF", ()), ("Markdown", ()))), ("Close", ())),
    ),
    ("Window", (("Minimize", ()), ("Close", ()), ("Untitled", ()), ("Untitled", ()))),
)
# Keyboard shortcuts as AX reports them: the key, and modifier bits (1
# shift, 2 option, 4 control, 8 no Command). A German Sona quits on "Sona
# beenden", which only its Command-Q names as Quit.
_SHORTCUTS = {"Sona > Sona beenden": ("Q", 0), "File > New Note": ("N", 0), "File > Export > PDF": ("P", 3)}
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
        if joined in _SHORTCUTS:
            attributes["AXMenuItemCmdChar"], attributes["AXMenuItemCmdModifiers"] = _SHORTCUTS[joined]
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


class _Quitting(_Sona):
    """Sona exiting once its Quit is pressed or Command-Q reaches it, or,
    with ``exits_on``, once that other item is pressed."""

    def __init__(self, *, exits_on: str = "Sona > Sona beenden") -> None:
        super().__init__()
        self.exits_on = exits_on
        self.gone = False

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        super().perform_action(element_index, action)
        self.gone = self.pressed[-1] == ("AXMenuBar", self.exits_on)

    def key(self, key: str, *, app: str | int | None = None, window_id: int | None = None) -> None:
        self.gone = key == "cmd+q"

    def _identity_from_info(self, info: object) -> object:
        return info

    def _observe_process(self, expected: object) -> dict[str, object]:
        return {"pid": _SONA, "state": "exited" if self.gone else "running"}

    def _exited(self, pid: int) -> bool:
        return self.gone


@pytest.mark.parametrize(
    "choose",
    [lambda do: do.menu("Sona > Sona beenden", app="Sona"), lambda do: do.key("cmd+q", app="Sona")],
    ids=["quit-item-by-its-shortcut", "command-q"],
)
def test_an_app_that_exits_on_its_own_quit_is_the_quit_done(
    choose: Callable[[Operations], Receipt],
) -> None:
    """Agents read a menu Quit that worked as a failure and retried it."""
    mac = _Quitting()

    receipt = choose(mac.do)

    assert (receipt.outcome, receipt.changed, receipt.verified) == (Outcome.DONE, True, True)


class _QuittingWatched(_Quitting):
    """Sona that, each time the harness looks whether it has exited,
    notes whether another harness run could send it input then."""

    def __init__(self, app_input_free: Callable[[int], bool]) -> None:
        super().__init__()
        self.app_input_free = app_input_free
        self.input_free: list[bool] = []

    def _exited(self, pid: int) -> bool:
        self.input_free.append(self.app_input_free(pid))
        return super()._exited(pid)


@pytest.mark.parametrize(
    "choose",
    [lambda do: do.menu("Sona > Sona beenden", app="Sona"), lambda do: do.key("cmd+q", app="Sona")],
    ids=["quit-item-by-its-shortcut", "command-q"],
)
def test_other_runs_send_an_app_input_while_its_quit_waits_for_it_to_exit(
    choose: Callable[[Operations], Receipt], app_input_free
) -> None:
    """An app asking to save before it quits keeps the wait going until
    the deadline, and the answer to that prompt is input to the app."""
    mac = _QuittingWatched(app_input_free)

    receipt = choose(mac.do)

    assert receipt.outcome is Outcome.DONE
    assert mac.input_free == [True]


def test_an_app_that_exits_on_another_item_fails_naming_mac_quit() -> None:
    mac = _Quitting(exits_on="File > Close")

    with pytest.raises(OperationError, match=r"mac\.quit\(app\)") as caught:
        mac.do.menu("File > Close", app="Sona")

    assert caught.value.code == ErrorCode.APP_EXITED


def test_menus_lists_a_menus_items_as_do_menu_presses_them() -> None:
    mac = _Sona(disabled="File > Close")

    items = mac.menus("Sona", "file")

    assert items == [
        {"title": "New Note", "enabled": True, "checked": False, "path": "File > New Note", "shortcut": "cmd+n"},
        {"title": "Export", "enabled": True, "checked": False, "path": "File > Export", "submenu": True},
        {"title": "Close", "enabled": False, "checked": False, "path": "File > Close"},
    ]
    assert mac.menus("Sona", "File > Export")[0]["shortcut"] == "alt+shift+cmd+p"
    mac.do.menu(items[0]["path"], app="Sona")
    assert mac.pressed == [("AXMenuBar", "File > New Note")]
