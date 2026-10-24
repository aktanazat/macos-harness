"""`mac.place`: one of an app's windows moved or resized through
Accessibility. The app's AX windows are fakes the window server lists at
their current frames, so each test asserts the writes `place` made and
the frame it read back, never a sleep."""

from __future__ import annotations

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOS, MacOSError

_PID = 7
# A 1512 x 982-point main display and a 1920 x 1080-point one above it.
_MAIN = {"x": 0.0, "y": 0.0, "width": 1512.0, "height": 982.0}
_ABOVE = {"x": -200.0, "y": -1080.0, "width": 1920.0, "height": 1080.0}

_Point = tuple[float, float]


class _Window:
    """One AX window; its app keeps it at least ``min_width`` wide and
    will not let the attributes in ``fixed`` change. ``number`` is the
    window server's number for it, None when it cannot be read."""

    def __init__(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        minimized: bool = False,
        min_width: float = 0.0,
        fixed: tuple[str, ...] = (),
        number: int | None = None,
    ) -> None:
        self.frame = {"x": x, "y": y, "width": width, "height": height}
        self.minimized = minimized
        self.min_width = min_width
        self.fixed = fixed
        self.number = number


class _Desk(MacOS):
    """`MacOS` over app ``_PID`` and two displays. AX sees the windows in
    ``reachable``; the window server also lists those in ``elsewhere``, as
    it does a window on another Space. Records each write."""

    def __init__(self, *reachable: _Window, elsewhere: tuple[_Window, ...] = ()) -> None:
        super().__init__()
        self.reachable = list(reachable)
        self.listed = [*reachable, *elsewhere]
        self.writes: list[tuple[_Point | None, _Point | None]] = []

    def _ensure_accessibility(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[None, dict[str, object]]:
        return None, {"name": "Archives", "bundle_id": None, "pid": _PID, "path": None}

    def _displays(self) -> list[tuple[int, dict[str, float]]]:
        return [(1, _MAIN), (2, _ABOVE)]

    def _server_windows(self, *, on_screen_only: bool) -> list[dict[str, object]]:
        return [
            {
                "kCGWindowNumber": 100 + index,
                "kCGWindowOwnerPID": _PID,
                "kCGWindowLayer": 0,
                "kCGWindowBounds": {
                    "X": window.frame["x"],
                    "Y": window.frame["y"],
                    "Width": window.frame["width"],
                    "Height": window.frame["height"],
                },
                "kCGWindowIsOnscreen": not window.minimized,
            }
            for index, window in enumerate(self.listed)
            if not (on_screen_only and window.minimized)
        ]

    def _window_description(self, window_id: int) -> dict[str, object] | None:
        return next(
            (entry for entry in self._server_windows(on_screen_only=False)
             if entry["kCGWindowNumber"] == window_id),
            None,
        )

    def _ax_windows(self, pid: int) -> list[_Window]:
        return self.reachable

    def _window_geometry(self, window: _Window) -> tuple[dict[str, float], bool]:
        return dict(window.frame), window.minimized

    def _ax_window_id(self, window: _Window) -> int | None:
        return window.number

    def _settable(self, element: _Window, attribute: str) -> bool:
        return attribute not in element.fixed

    def _write_window(
        self, window: _Window, *, position: _Point | None, size: _Point | None
    ) -> None:
        self.writes.append((position, size))
        x, y = position or (window.frame["x"], window.frame["y"])
        width, height = size or (window.frame["width"], window.frame["height"])
        window.frame = {"x": x, "y": y, "width": max(width, window.min_width), "height": height}


def _frame(x: float, y: float, width: float, height: float) -> dict[str, float]:
    return {"x": x, "y": y, "width": width, "height": height}


@pytest.mark.parametrize(
    ("asked", "writes", "frame"),
    [
        pytest.param(
            {"x": 40, "y": 60}, [((40.0, 60.0), None)], _frame(40, 60, 600, 400),
            id="move",
        ),
        pytest.param(
            {"width": 800, "height": 500}, [(None, (800.0, 500.0))],
            _frame(100, 100, 800, 500),
            id="resize",
        ),
        pytest.param(
            {"x": 10, "y": 20, "display": 1}, [((-190.0, -1060.0), None)],
            _frame(-190, -1060, 600, 400),
            id="from-the-top-left-of-a-display",
        ),
        pytest.param(
            {"width": 100}, [(None, (100.0, 400.0))], _frame(100, 100, 300, 400),
            id="the-app-keeps-its-minimum-width",
        ),
        pytest.param(
            {"x": 100, "y": 100}, [(None, None)], _frame(100, 100, 600, 400),
            id="already-there",
        ),
    ],
)
def test_place_writes_what_changes_and_returns_the_frame_read_back(
    asked: dict[str, float], writes: list[tuple[_Point | None, _Point | None]],
    frame: dict[str, float],
) -> None:
    mac = _Desk(_Window(100, 100, 600, 400, min_width=300))

    placed = mac.place("Archives", **asked)

    assert (mac.writes, placed["frame"]) == (writes, frame)


@pytest.mark.parametrize(
    ("window", "writes", "frame"),
    [
        pytest.param(
            _Window(900, -600, 600, 400), [((456.0, 291.0), None)],
            _frame(456, 291, 600, 400),
            id="from-the-display-above",
        ),
        pytest.param(
            _Window(0, 0, 1600, 900), [((0.0, 41.0), None)], _frame(0, 41, 1600, 900),
            id="wider-than-the-display",
        ),
        pytest.param(
            _Window(100, 100, 600, 400), [(None, None)], _frame(100, 100, 600, 400),
            id="already-on-it",
        ),
    ],
)
def test_place_centers_on_the_display_a_window_that_does_not_fit_there(
    window: _Window, writes: list[tuple[_Point | None, _Point | None]],
    frame: dict[str, float],
) -> None:
    mac = _Desk(window)

    placed = mac.place("Archives", display=0)

    assert (mac.writes, placed["frame"]) == (writes, frame)


@pytest.mark.parametrize(
    ("window_index", "corners"),
    [
        pytest.param(0, {"small": (50, 50), "large": (0, 30)}, id="the-largest-first"),
        pytest.param(1, {"small": (0, 30), "large": (400, 100)}, id="then-the-next"),
    ],
)
def test_place_moves_the_window_that_see_numbers_window_index(
    window_index: int, corners: dict[str, tuple[float, float]],
) -> None:
    windows = {"small": _Window(50, 50, 300, 200), "large": _Window(400, 100, 800, 600)}
    mac = _Desk(windows["small"], windows["large"])

    mac.place("Archives", window_index=window_index, x=0, y=30)

    assert {
        name: (window.frame["x"], window.frame["y"]) for name, window in windows.items()
    } == corners


def test_place_moves_the_window_window_id_names_when_another_shares_its_frame() -> None:
    """Two maximized documents share bounds; the frame alone picked the
    first of them whichever window_id was asked for."""
    first = _Window(0, 30, 1512, 952, number=100)
    second = _Window(0, 30, 1512, 952, number=101)
    mac = _Desk(first, second)

    mac.place("Archives", window_id=101, x=40, y=60)

    assert ((first.frame["x"], first.frame["y"]), (second.frame["x"], second.frame["y"])) == (
        (0, 30), (40, 60),
    )


def test_place_refuses_windows_sharing_a_frame_it_cannot_tell_apart() -> None:
    mac = _Desk(_Window(0, 30, 1512, 952), _Window(0, 30, 1512, 952))

    with pytest.raises(MacOSError, match="cannot tell which") as error:
        mac.place("Archives", window_id=101, x=40, y=60)

    assert (error.value.code, mac.writes) == (ErrorCode.UNSUPPORTED_OP, [])


@pytest.mark.parametrize(
    ("desk", "asked", "reason"),
    [
        pytest.param(
            _Desk(_Window(100, 100, 600, 400, minimized=True)), {"x": 0, "y": 0},
            "is minimized",
            id="minimized",
        ),
        pytest.param(
            _Desk(elsewhere=(_Window(100, 100, 600, 400),)), {"x": 0, "y": 0},
            "another Space",
            id="on-another-space",
        ),
        pytest.param(
            _Desk(_Window(100, 100, 600, 400, fixed=("AXSize",))),
            {"x": 0, "y": 0, "width": 900}, "window 0 be resized",
            id="fixed-size",
        ),
        pytest.param(
            _Desk(_Window(100, 100, 600, 400, fixed=("AXPosition",))), {"x": 0, "y": 0},
            "window 0 be moved",
            id="fixed-position",
        ),
    ],
)
def test_place_refuses_a_window_it_cannot_move_and_writes_nothing(
    desk: _Desk, asked: dict[str, float], reason: str,
) -> None:
    with pytest.raises(MacOSError, match=reason) as error:
        desk.place("Archives", **asked)

    assert (error.value.code, desk.writes) == (ErrorCode.UNSUPPORTED_OP, [])


@pytest.mark.parametrize(
    ("asked", "missing"),
    [
        pytest.param({"x": 5}, "y", id="x-alone"),
        pytest.param({"y": 5}, "x", id="y-alone"),
    ],
)
def test_place_needs_x_and_y_together(asked: dict[str, float], missing: str) -> None:
    mac = _Desk(_Window(100, 100, 600, 400))

    with pytest.raises(MacOSError, match="give x and y together") as error:
        mac.place("Archives", **asked)

    assert (error.value.code, error.value.details["parameter"], mac.writes) == (
        ErrorCode.BAD_REQUEST, missing, [],
    )
