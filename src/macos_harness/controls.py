"""The explicit Accessibility escape hatch for the macOS harness."""

from __future__ import annotations

import difflib
import re
import weakref
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import ApplicationServices as AS

from .errors import ErrorCode, MacOSError
from .macos import (
    _AX_ROLES,
    _AX_SAFE_ATTRIBUTES,
    _SEARCH_KEY_OF_ROLE,
    ElementRef,
    SearchMatches,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .macos import MacOS

# Each role a search can target, by its friendly name ("pop up button" ->
# "popupbutton") and by the AX name a match reports ("AXPopUpButton" ->
# "axpopupbutton"), so a role read off one match can find the next. A role
# with an Apple search key maps to the key; any other is itself the key,
# and the search keeps the matches of that role (`macos._role_filter`).
_ROLE_SEARCH_KEYS = {"any": "AXAnyTypeSearchKey"} | {
    alias: _SEARCH_KEY_OF_ROLE.get(ax_role, ax_role)
    for ax_role in _AX_ROLES
    for alias in (ax_role.casefold(), ax_role[2:].casefold())
}


class Accessibility:
    """Thin, opt-in access to Apple's AXUIElement operations."""

    _COMPACT_ATTRIBUTES = (
        "AXRole",
        "AXTitle",
        "AXDescription",
        "AXValue",
        "AXFrame",
    )
    _SAFE_ATTRIBUTES = _AX_SAFE_ATTRIBUTES

    def __init__(self, host: MacOS) -> None:
        # A weak reference, not `self._host = host`: `MacOS.__init__` does
        # `self.ax = Accessibility(self)`, so a strong reference back here
        # would form a MacOS<->Accessibility cycle. CPython's cyclic GC can
        # still collect such a cycle, but only on its own occasional pass
        # -- never promptly, the instant the last external reference to a
        # MacOS drops -- which would leave its native agent child (if any)
        # running for however long that takes. A weak reference here lets
        # plain refcounting collect a MacOS immediately, as if this escape
        # hatch did not exist at all.
        self._host_ref = weakref.ref(host)

    @property
    def _host(self) -> MacOS:
        host = self._host_ref()
        if host is None:
            raise MacOSError(
                "This Accessibility escape hatch's MacOS instance has "
                "already been closed or garbage collected; construct a "
                "new MacOS to use mac.ax again",
                code=ErrorCode.UNSUPPORTED_OP,
            )
        return host

    def at(
        self,
        x: float,
        y: float,
        *,
        app: str | None = None,
        screenshot: str | Path | None = None,
        coordinate_space: str = "screenshot",
        attributes: Iterable[str] = _COMPACT_ATTRIBUTES,
        include_actions: bool = True,
    ) -> dict[str, Any]:
        """The element of ``app`` at ``x``, ``y``, or of any app without one.

        A screenshot point maps through the last `see`, or the PNG
        ``screenshot`` names, which must still show ``app``'s window where
        it is now."""
        self._host._ensure_accessibility()
        self._host._use_screenshot(screenshot)
        pid = self._host._pid(app)
        point = self._host._screen_point(x, y, coordinate_space, pid=pid)
        root = (
            self._host._application_element(pid)
            if pid is not None
            else AS.AXUIElementCreateSystemWide()
        )
        error, element = AS.AXUIElementCopyElementAtPosition(
            root, point[0], point[1], None
        )
        if error != 0 or element is None:
            raise MacOSError(
                f"AX hit test failed with AXError {error}",
                code=ErrorCode.AX_ERROR,
                details={"ax_error": int(error)},
            )
        index = self._host._remember_element(element)
        return self._host._describe_element(
            element,
            index,
            attributes=attributes,
            include_actions=include_actions,
        )

    @staticmethod
    def _search_key(search_key: str | None, role: str | None) -> str:
        if search_key is not None and role is not None:
            raise MacOSError(
                "Pass role or search_key, not both",
                code=ErrorCode.BAD_REQUEST,
                details={"parameter": "role/search_key"},
            )
        if role is None:
            return search_key or "AXAnyTypeSearchKey"
        normalized = re.sub(r"[^a-z0-9]", "", role.casefold())
        if normalized in _ROLE_SEARCH_KEYS:
            return _ROLE_SEARCH_KEYS[normalized]
        if re.fullmatch(r"AX[A-Z][A-Za-z]*", role):
            return role
        named = {ax_role[2:].casefold(): ax_role for ax_role in _AX_ROLES}
        suggestions = [
            named[name] for name in difflib.get_close_matches(normalized, named, n=3, cutoff=0.5)
        ]
        hint = f"; did you mean {', '.join(suggestions)}?" if suggestions else "."
        raise MacOSError(
            f"Unknown AX role {role!r}{hint} A role is 'any' or any AX role, "
            "by its AX name ('AXPopUpButton', 'AXWindow') or in words "
            "('pop up button', 'window')",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "role", "value": role, "suggestions": suggestions},
        )

    def query(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        element_index: int | None = None,
        role: str | None = None,
        search_key: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] | None = None,
        include_actions: bool = False,
        max_nodes: int = 5000,
    ) -> SearchMatches:
        """Search one app, or every app (``all_apps=True``) or ``apps``.

        ``attributes`` defaults to role, title, description, value, and
        frame; a cross-app search leaves ``AXValue`` out by default.
        """
        if attributes is None:
            cross_process = all_apps or apps is not None
            attributes = self._SAFE_ATTRIBUTES if cross_process else self._COMPACT_ATTRIBUTES
        return self._host.ax_query(
            app=app,
            all_apps=all_apps,
            apps=apps,
            element_index=element_index,
            search_key=self._search_key(search_key, role),
            text=text,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            limit=limit,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
        )

    def query_all(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        limit: int = 20,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 5000,
    ) -> SearchMatches:
        """`query` that searches every running app unless a scope narrows it."""
        return self.query(
            text,
            app=app,
            all_apps=all_apps or (app is None and apps is None),
            apps=apps,
            role=role,
            search_key=search_key,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            limit=limit,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
        )

    def wait(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        include_actions: bool = False,
        max_nodes: int = 5000,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        return self._host.ax_wait(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            include_actions=include_actions,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )

    def wait_gone(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        max_nodes: int = 5000,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> None:
        self._host.ax_wait_gone(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )

    def press(
        self,
        text: str | None = None,
        *,
        app: str | int | None = None,
        all_apps: bool = False,
        apps: str | int | Iterable[str | int] | None = None,
        role: str | None = None,
        search_key: str | None = None,
        title: str | None = None,
        identifier: str | None = None,
        description: str | None = None,
        visible_only: bool = True,
        direction: str = "next",
        immediate_descendants_only: bool = False,
        attributes: Iterable[str] = _SAFE_ATTRIBUTES,
        max_nodes: int = 5000,
        timeout: float = 5.0,
        interval: float = 0.1,
    ) -> dict[str, Any]:
        return self._host.ax_press(
            app=app,
            all_apps=all_apps,
            apps=apps,
            search_key=self._search_key(search_key, role),
            text=text,
            title=title,
            identifier=identifier,
            description=description,
            visible_only=visible_only,
            direction=direction,
            immediate_descendants_only=immediate_descendants_only,
            attributes=attributes,
            max_nodes=max_nodes,
            timeout=timeout,
            interval=interval,
        )

    def get(
        self, element_index: ElementRef, attributes: str | Iterable[str] = "AXValue"
    ) -> Any:
        if isinstance(attributes, str):
            return self._host.get(element_index, attributes)
        return self._host.get_attributes(element_index, attributes)

    def set(self, element_index: ElementRef, attribute: str, value: Any) -> None:
        self._host.set(element_index, value, attribute)

    def actions(self, element_index: ElementRef) -> list[str]:
        element = self._host._local_element(element_index)
        return self._host._actions(element)

    def perform(self, element_index: ElementRef, action: str = "AXPress") -> None:
        self._host.perform_action(element_index, action)

    def parameterized(self, element_index: ElementRef, attribute: str, parameter: Any) -> Any:
        error, value = AS.AXUIElementCopyParameterizedAttributeValue(
            self._host._local_element(element_index), attribute, parameter, None
        )
        if error != 0:
            raise MacOSError(
                f"Parameterized AX read {attribute!r} failed with AXError {error}",
                code=ErrorCode.AX_ERROR,
                details={
                    "ax_error": int(error),
                    "element_index": element_index,
                    "attribute": attribute,
                },
            )
        return self._host._jsonable(value)

    def raw(self, element_index: ElementRef) -> Any:
        return self._host._local_element(element_index)

    def dump(self, app: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("screenshot", False)
        return self._host.get_app_state(app, **kwargs)
