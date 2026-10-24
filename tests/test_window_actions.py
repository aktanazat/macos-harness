"""`mac.do.window` and `mac.do.reveal` against a fake window: the title
bar buttons agents looked up by subrole in inspect output, and a list
whose rows sit below the visible part of its scroll area."""

from __future__ import annotations

import pytest

from macos_harness.errors import ErrorCode, MacOSError
from macos_harness.macos import MacOS, SearchMatches
from macos_harness.native import _NativeHandle
from macos_harness.receipts import Outcome

_PID = 4512
_INFO = {"name": "Sona", "bundle_id": "app.sona", "pid": _PID, "path": None}
_WINDOW = {"window_id": 7, "pid": _PID, "title": "Notes", "on_screen": True,
           "bounds": {"x": 0, "y": 0, "width": 400, "height": 400}}


class _Sona(MacOS):
    """One Sona window with close and zoom buttons but no minimize button,
    as a panel has. ``stays`` keeps it open after its close button is
    pressed, as a window asking to save does. A row ``below`` points under
    the top of a 400-point scroll area moves up by each scroll, at most
    300 points a scroll, never past ``scroll_limit`` points in all."""

    def __init__(self, *, stays: bool = False, below: float = 900, scroll_limit: float = 2000) -> None:
        super().__init__()
        self.stays = stays
        self.closed = False
        self.pressed: list[str] = []
        self.row_y = below
        self.scrolled = 0.0
        self.scroll_limit = scroll_limit
        self.scrolls = 0
        self.attributes: dict[str, dict[str, object]] = {
            "window": {"AXCloseButton": "close", "AXZoomButton": "zoom"},
            "close": {"AXEnabled": True},
            "zoom": {"AXEnabled": True},
            "row": {"AXParent": "list", "AXRole": "AXRow"},
            "list": {"AXParent": "area", "AXRole": "AXList"},
            "area": {"AXParent": "window", "AXRole": "AXScrollArea"},
        }

    def _ensure_accessibility(self) -> None:
        return None

    def _ensure_post_events(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        return object(), dict(_INFO)

    def windows(self, app: str | int | None = None, *, all_layers: bool = False) -> list[dict[str, object]]:
        return [] if self.closed else [dict(_WINDOW)]

    def _ax_window(self, info, target, window_index):
        return "window", target["bounds"], False

    def _copy_attribute(self, element: object, attribute: str, *, checked: bool = False) -> object:
        return self.attributes.get(str(element), {}).get(attribute)

    def _actions(self, element: object) -> list[str]:
        return []

    def _element_pid(self, element: object) -> int | None:
        return None

    def perform_action(self, element_index: int, action: str = "AXPress") -> None:
        button = self._element(element_index)
        self.pressed.append(button)
        self.closed = button == "close" and not self.stays

    def _window_description(self, window_id: int) -> object:
        return None if self.closed else window_id

    def _window_entry(self, value: object) -> dict[str, object]:
        return dict(_WINDOW)

    def get(self, element_index, attribute="AXValue", *, missing_ok=False):
        node = self._element(element_index)
        if node == "area":
            return {"x": 0.0, "y": 0.0, "width": 400.0, "height": 400.0}
        return {"x": 10.0, "y": self.row_y, "width": 380.0, "height": 20.0}

    def scroll(self, delta_y, delta_x=0, **where):
        self.scrolls += 1
        step = max(delta_y, -300, -(self.scroll_limit - self.scrolled))
        self.scrolled -= step
        self.row_y += step

    def _pause(self, seconds: float) -> None:
        return None

    def _frontmost_app(self) -> dict[str, object]:
        return {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty", "pid": 1, "path": None}

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> dict[str, object]:
        raise MacOSError("Sona's focus is not read here", code=ErrorCode.AX_ERROR)


@pytest.mark.parametrize(
    ("stays", "verified"), [(False, True), (True, False)], ids=["closed", "asking-to-save"]
)
def test_window_close_presses_the_close_button_and_waits_for_the_window_to_go(
    stays: bool, verified: bool
) -> None:
    """Agents found the close button by subrole in inspect output; a
    window still there after the press is no false success."""
    mac = _Sona(stays=stays)

    receipt = mac.do.window("close", app="Sona", timeout=0.2)

    assert (receipt.outcome, receipt.verified, mac.pressed) == (Outcome.DONE, verified, ["close"])


@pytest.mark.parametrize(
    ("action", "code"),
    [("minimize", ErrorCode.UNSUPPORTED_OP), ("hide", ErrorCode.BAD_REQUEST)],
    ids=["no-such-button", "not-an-action"],
)
def test_window_presses_nothing_it_cannot_do(action: str, code: ErrorCode) -> None:
    mac = _Sona()

    with pytest.raises(MacOSError) as caught:
        mac.do.window(action, app="Sona")

    assert (caught.value.code, mac.pressed) == (code, [])


def test_reveal_scrolls_a_row_into_its_scroll_area_and_gives_its_frame() -> None:
    mac = _Sona(below=1200)
    row = mac._remember_element("row")

    receipt = mac.do.reveal(row, app="Sona")

    frame = receipt.to_json()["observed"]["input"]["frame"]
    assert (receipt.outcome, receipt.verified) == (Outcome.DONE, True)
    assert 0 <= frame["y"] + frame["height"] / 2 <= 400
    assert mac.scrolls == 3


def test_reveal_stops_once_the_list_scrolls_no_further() -> None:
    mac = _Sona(below=900, scroll_limit=300)
    row = mac._remember_element("row")

    receipt = mac.do.reveal(row, app="Sona")

    assert (receipt.outcome, receipt.verified, mac.scrolls) == (Outcome.DONE, False, 2)


class _Agent:
    """The native agent: it reads a handle's attributes, never its
    parent, so it cannot say which scroll area holds the row."""

    generation = 1

    def get_attributes(self, handle: _NativeHandle, attributes: tuple[str, ...]) -> dict[str, object]:
        row = {"AXRole": "AXRow", "AXTitle": "Plan", "AXIdentifier": None, "AXDescription": None,
               "AXFrame": {"x": 10.0, "y": 1200.0, "width": 380.0, "height": 20.0}}
        return {name: row.get(name) for name in attributes}


class _NativeSona(_Sona):
    """Sona searched through the native agent; a search from the app's
    own element runs here and finds two Plan rows."""

    def _application_element(self, pid: int, **_: object) -> str:
        return "app"

    def ax_search(self, *, element_index: int | None = None, **_: object) -> SearchMatches:
        assert self._element(element_index) == "app"
        return SearchMatches(
            [
                {"element_index": self._remember_element("other"), "role": "AXRow",
                 "frame": {"x": 10.0, "y": 60.0, "width": 380.0, "height": 20.0}},
                {"element_index": self._remember_element("row"), "role": "AXRow",
                 "frame": {"x": 10.0, "y": 1200.0, "width": 380.0, "height": 20.0}},
            ],
            complete=True,
            visited=2,
        )


def test_reveal_scrolls_a_row_the_native_agent_found() -> None:
    mac = _NativeSona(below=1200)
    row = mac._remember_element(_NativeHandle(_Agent(), 3, 1))

    receipt = mac.do.reveal(row, app="Sona")

    assert (receipt.outcome, receipt.verified, mac.scrolls) == (Outcome.DONE, True, 3)
