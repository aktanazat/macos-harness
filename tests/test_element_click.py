"""A click aimed at an element instead of a point, for a control that
ignores AXPress. It lands on the center of the element's frame as the
element is now, in the element's own process. One app's row and window
are fakes; each test asserts where a click was posted, or that none was."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError, SearchMatches

_SONA, _NOTES = 4512, 4600
_WINDOW = 31
_APPS = {
    _SONA: {"name": "Sona", "bundle_id": "app.sona", "pid": _SONA, "path": None},
    _NOTES: {"name": "Notes", "bundle_id": "com.apple.Notes", "pid": _NOTES, "path": None},
}
_ROW = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 40.0}
_LOWER = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 40.0}
_COLLAPSED = {"x": 100.0, "y": 200.0, "width": 0.0, "height": 0.0}
# The row as an earlier run's search printed it.
_EARLIER = {
    "element_index": 176, "pid": _SONA, "role": "AXRow", "title": "Settings", "frame": _ROW,
}


class _Row:
    """Sona's one row; it answers reads itself, as the agent does for
    its handles."""

    def __init__(self, frame: dict[str, float]) -> None:
        self.frame = frame
        self.client = self

    def get(self, element: _Row, attribute: str) -> dict[str, float]:
        assert attribute == "AXFrame"
        return element.frame


class _Sona(MacOS):
    """`MacOS` over Sona's one window, 1000 x 800 points at the top left
    of the screen, holding one row titled "Settings" whose frame is
    ``frame`` now. Records each click it would post."""

    def __init__(self, frame: dict[str, float] = _ROW) -> None:
        super().__init__()
        self.row = _Row(frame)
        self.row_index = self._remember_element(self.row)
        self.posted: list[tuple[int, tuple[float, float], int]] = []
        # Hidden, the virtual pointer never starts its helper window.
        self.hide_pointer()

    def _ensure_accessibility(self) -> None:
        return None

    def _ensure_post_events(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, Any]]:
        """Each app by name or pid; no query is the last app a call named."""
        if query is None and self._last_app is not None:
            query = self._last_app["pid"]
        for pid, info in _APPS.items():
            if query in (pid, info["name"]):
                return object(), info
        raise ApplicationNotFoundError(f"No running app matches {query!r}")

    def _window_list(self, *, on_screen_only: bool) -> tuple[dict[str, Any], ...]:
        return (
            {
                "kCGWindowNumber": _WINDOW,
                "kCGWindowOwnerPID": _SONA,
                "kCGWindowOwnerName": "Sona",
                "kCGWindowName": "Sona",
                "kCGWindowLayer": 0,
                "kCGWindowBounds": {"X": 0.0, "Y": 0.0, "Width": 1000.0, "Height": 800.0},
                "kCGWindowIsOnscreen": True,
                "kCGWindowAlpha": 1.0,
            },
        )

    def ax_search(self, **query: object) -> SearchMatches:
        found = [
            {
                "element_index": self.row_index,
                "pid": _SONA,
                "role": "AXRow",
                "title": "Settings",
                "frame": self.row.frame,
            }
        ]
        return SearchMatches(found, complete=True, visited=len(found))

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> dict[str, Any]:
        raise MacOSError("Sona's focus is not read here", code=ErrorCode.AX_ERROR)

    def _post_click(
        self, pid: int, point: tuple[float, float], window: Any, *, button: str, clicks: int
    ) -> None:
        self._pointer_position = point
        self.posted.append((pid, point, window.window_id))


@pytest.mark.parametrize(
    ("aim", "app", "now", "center"),
    [
        (lambda mac: _EARLIER, None, _ROW, (250.0, 220.0)),
        (lambda mac: mac.row_index, "Sona", _ROW, (250.0, 220.0)),
        (lambda mac: _EARLIER, None, _LOWER, (250.0, 420.0)),
        (lambda mac: _EARLIER, "Sona", _ROW, (250.0, 220.0)),
    ],
    ids=["match-from-an-earlier-run", "index-in-its-app", "moved-since-the-match", "match-in-its-app"],
)
def test_a_click_on_an_element_lands_on_the_center_of_its_frame_now(
    aim: Callable[[_Sona], object], app: str | None, now: dict[str, float],
    center: tuple[float, float],
) -> None:
    mac = _Sona(now)

    mac.click(element=aim(mac), app=app)

    assert mac.posted == [(_SONA, center, _WINDOW)]


@pytest.mark.parametrize(
    ("now", "center"),
    [(_ROW, (250.0, 220.0)), (_LOWER, (250.0, 420.0))],
    ids=["where-the-match-was", "moved-since-the-match"],
)
def test_do_click_on_an_element_posts_at_the_point_its_receipt_reports(
    now: dict[str, float], center: tuple[float, float],
) -> None:
    mac = _Sona(now)

    receipt = mac.do.click(element=_EARLIER, app="Sona")

    assert mac.posted == [(_SONA, center, _WINDOW)]
    assert receipt.to_json()["target"]["point"] == {"x": center[0], "y": center[1]}


def test_a_receipt_names_the_clicked_element_but_never_its_value() -> None:
    mac = _Sona()

    receipt = mac.do.click(element={**_EARLIER, "value": "a private note"}, app="Sona")

    assert receipt.to_json()["request"]["element"]["title"] == "Settings"
    assert "a private note" not in json.dumps(receipt.to_json())


@pytest.mark.parametrize(
    "click",
    [
        lambda mac: mac.click(10.0, 20.0, element=_EARLIER),
        lambda mac: mac.click(10.0),
        lambda mac: mac.click(),
        lambda mac: mac.do.click(10.0, 20.0, element=_EARLIER, app="Sona"),
        lambda mac: mac.do.click(app="Sona"),
    ],
    ids=["point-and-element", "x-alone", "nothing", "do-point-and-element", "do-nothing"],
)
def test_a_click_aims_at_a_point_or_an_element_never_both_or_neither(
    click: Callable[[_Sona], object],
) -> None:
    mac = _Sona()

    with pytest.raises(MacOSError) as caught:
        click(mac)

    assert (caught.value.code, mac.posted) == (ErrorCode.BAD_REQUEST, [])


@pytest.mark.parametrize(
    "click",
    [
        lambda mac, element: mac.click(element=element, app="Sona"),
        lambda mac, element: mac.do.click(element=element, app="Sona"),
    ],
    ids=["click", "do-click"],
)
@pytest.mark.parametrize(
    ("now", "element", "code"),
    [
        (_ROW, {**_EARLIER, "pid": _NOTES}, ErrorCode.BAD_REQUEST),
        (_COLLAPSED, _EARLIER, ErrorCode.UNSUPPORTED_OP),
    ],
    ids=["another-apps-element", "no-area-on-screen"],
)
def test_a_click_on_an_element_it_cannot_land_on_posts_nothing(
    click: Callable[[_Sona, object], object], now: dict[str, float],
    element: dict[str, object], code: ErrorCode,
) -> None:
    """Another app's window may lie under the element's point; a click
    meant for one app never lands in it."""
    mac = _Sona(now)

    with pytest.raises(MacOSError) as caught:
        click(mac, element)

    assert (caught.value.code, mac.posted) == (code, [])
