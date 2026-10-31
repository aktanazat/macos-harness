"""A click aimed at an element instead of a point, for a control that
ignores AXPress. It lands on the center of the element's frame once the
frame holds still, in the element's own process, and only where
accessibility finds that element in the window the click goes to. One
app's row and window are fakes; each test asserts where a click was
posted, or that none was."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Sequence
from typing import NoReturn

import pytest

from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError, SearchMatches, _TargetWindow
from macos_harness.receipts import Acted, OperationError

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
# The row sliding down a point at every reading, for longer than a click
# waits for it to hold still, and the same row holding still after three.
_SLIDING = [{**_ROW, "y": _ROW["y"] + step} for step in range(100)]
_SETTLING = _SLIDING[:4]


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

    def _resolve_app(self, query: str | int | None) -> tuple[object, dict[str, object]]:
        """Each app by name or pid; no query is the last app a call named."""
        if query is None and self._last_app is not None:
            query = self._last_app["pid"]
        for pid, info in _APPS.items():
            if query in (pid, info["name"]):
                return object(), info
        raise ApplicationNotFoundError(f"No running app matches {query!r}")

    def _server_windows(self, *, on_screen_only: bool) -> tuple[dict[str, object], ...]:
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

    def _focus_sample(self, pid: int, *, include_values: bool = True) -> NoReturn:
        raise MacOSError("Sona's focus is not read here", code=ErrorCode.AX_ERROR)

    def _post_click(
        self,
        pid: int,
        point: tuple[float, float],
        window: _TargetWindow,
        *,
        button: str,
        clicks: int,
    ) -> None:
        self._pointer_position = point
        self.posted.append((pid, point, window.window_id))


class _Node:
    """An element accessibility answers for: its frame, its parent, and
    the number of the window it lies in."""

    def __init__(self, parent: _Node | None = None, window: int = _WINDOW) -> None:
        self.parent = parent
        self.window = window
        self.frame = _ROW


class _Covered(_Sona):
    """Sona read through accessibility instead of the agent: its row sits
    in its window, and at the row's center accessibility finds, at each
    ask, the element the next of ``answers`` picks, the last for every
    ask after, or nothing."""

    def __init__(self, *answers: Callable[[_Node, _Node], _Node | None]) -> None:
        super().__init__()
        window = _Node()
        self.row = _Node(window)
        self.row_index = self._remember_element(self.row)
        self.answers = deque(answer(self.row, window) for answer in answers)

    def _is_ax_element(self, value: object) -> bool:
        return isinstance(value, _Node)

    def get(self, element_index: object, attribute: str = "AXValue", *, missing_ok: bool = False) -> object:
        assert attribute == "AXFrame"
        return self._element(element_index).frame

    @staticmethod
    def _copy_attribute(element: object, attribute: str, *, checked: bool = False) -> object:
        if not isinstance(element, _Node):
            return None
        return {"AXParent": element.parent, "AXWindow": element.window}.get(attribute)

    @staticmethod
    def _ax_window_id(window: object) -> int | None:
        return window if isinstance(window, int) else None

    def _pause(self, seconds: float) -> None:
        return None

    def _hit(self, pid: int, point: tuple[float, float]) -> tuple[object | None, dict[str, object] | None]:
        found = self.answers.popleft() if len(self.answers) > 1 else self.answers[0]
        if found is None:
            return None, None
        return found, {"role": "AXButton", "title": "Cover", "description": None}


class _SlidingRow(_Row):
    """Sona's row sliding into place: each reading of its frame is the
    next of ``frames``, until the last holds."""

    def __init__(self, frames: Sequence[dict[str, float]]) -> None:
        super().__init__(frames[0])
        self.later = deque(frames[1:])

    def get(self, element: _Row, attribute: str) -> dict[str, float]:
        frame = super().get(element, attribute)
        if self.later:
            self.frame = self.later.popleft()
        return frame


class _Sliding(_Sona):
    """Sona whose row slides through ``frames`` before it holds still, on
    a clock only the click's own pauses move."""

    def __init__(self, frames: Sequence[dict[str, float]]) -> None:
        super().__init__(frames[0])
        self.row = _SlidingRow(frames)
        self.row_index = self._remember_element(self.row)
        self.clock = 0.0

    def _now(self) -> float:
        return self.clock

    def _pause(self, seconds: float) -> None:
        self.clock += seconds


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


@pytest.mark.parametrize(
    "wrap",
    [lambda match: match, lambda match: SearchMatches([match], complete=True, visited=1)],
    ids=["match", "one-match-search-result"],
)
def test_a_receipt_names_the_clicked_element_but_never_its_value(
    wrap: Callable[[dict[str, object]], object],
) -> None:
    mac = _Sona()

    receipt = mac.do.click(element=wrap({**_EARLIER, "value": "a private note"}), app="Sona")

    assert receipt.to_json()["request"]["element"]["title"] == "Settings"
    assert "a private note" not in json.dumps([receipt.to_json(), mac.timeline()])


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


@pytest.mark.parametrize(
    "click",
    [
        lambda mac: mac.click(element=_EARLIER, app="Sona"),
        lambda mac: mac.do.click(element=_EARLIER, app="Sona"),
    ],
    ids=["click", "do-click"],
)
@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: _Covered(lambda row, window: _Node(_Node())), ErrorCode.UNSUPPORTED_OP),
        (lambda: _Covered(lambda row, window: row, lambda row, window: _Node(_Node())), ErrorCode.UNSUPPORTED_OP),
        (lambda: _Sliding(_SLIDING), ErrorCode.TIMEOUT),
    ],
    ids=["another-element-over-it", "cached-match-now-covered", "still-moving"],
)
def test_a_click_that_would_land_on_something_else_posts_nothing(
    click: Callable[[_Sona], object], make: Callable[[], _Sona], code: ErrorCode,
) -> None:
    mac = make()

    with pytest.raises(MacOSError) as caught:
        click(mac)

    assert (caught.value.code, mac.posted) == (code, [])
    assert not isinstance(caught.value, OperationError) or caught.value.receipt.acted is Acted.NO


@pytest.mark.parametrize(
    "click",
    [
        lambda mac: mac.click(element=_EARLIER, app="Sona"),
        lambda mac: mac.do.click(element=_EARLIER, app="Sona"),
    ],
    ids=["click", "do-click"],
)
@pytest.mark.parametrize(
    ("make", "center"),
    [
        (lambda: _Covered(lambda row, window: _Node(row)), (250.0, 220.0)),
        (lambda: _Covered(lambda row, window: window), (250.0, 220.0)),
        (lambda: _Covered(lambda row, window: None), (250.0, 220.0)),
        (lambda: _Covered(lambda row, window: _Node(window=_WINDOW + 1)), (250.0, 220.0)),
        (lambda: _Covered(lambda row, window: _Node(_Node()), lambda row, window: row), (250.0, 220.0)),
        (lambda: _Sliding(_SETTLING), (250.0, 223.0)),
    ],
    ids=[
        "a-part-of-it", "what-holds-it", "nothing-named", "another-window-over-it",
        "found-on-asking-again", "held-still-after-sliding",
    ],
)
def test_a_click_lands_where_accessibility_finds_the_element_once_still(
    click: Callable[[_Sona], object], make: Callable[[], _Sona], center: tuple[float, float],
) -> None:
    """Another of the app's windows over the element, as a floating panel,
    takes no click routed to the element's window; and an app may name
    the element only on a second ask, as Chromium answers a first hit
    test from bounding boxes."""
    mac = make()

    click(mac)

    assert mac.posted == [(_SONA, center, _WINDOW)]
