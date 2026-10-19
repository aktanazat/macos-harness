"""`see`, `windows`, and pointer input on surfaces off the normal window
layer: a layer-25 panel, the menu bar the window server owns, and whole
displays or screen regions; and input a later run aims through the PNG
an earlier run's `see` wrote. The window server is a fake list in its
own front-to-back order; captures are real images the fake records, so
each test asserts what was captured and where input was routed, never a
sleep."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import Quartz

from macos_harness.capture import WindowCapture
from macos_harness.errors import ApplicationNotFoundError, ErrorCode
from macos_harness.macos import MacOS, MacOSError

_FINDER, _NOTES, _SERVER = 42, 50, 99
_BAR, _PANEL, _NOTE, _DOC, _HIDDEN = 3, 25, 11, 7, 8
_APPS = {
    _FINDER: {"name": "Finder", "bundle_id": "com.apple.finder", "pid": _FINDER, "path": None},
    _NOTES: {"name": "Notes", "bundle_id": "com.apple.Notes", "pid": _NOTES, "path": None},
}
# Two 1000 x 800-point displays side by side, the main one first.
_DISPLAYS = [
    (1, {"x": 0.0, "y": 0.0, "width": 1000.0, "height": 800.0}),
    (3, {"x": 1000.0, "y": 0.0, "width": 1000.0, "height": 800.0}),
]


def _entry(
    pid: int, window_id: int, layer: int, frame: tuple[float, float, float, float],
    *, on_screen: bool = True,
) -> dict[str, Any]:
    owner = _APPS[pid]["name"] if pid in _APPS else "Window Server"
    x, y, width, height = frame
    return {
        "kCGWindowNumber": window_id,
        "kCGWindowOwnerPID": pid,
        "kCGWindowOwnerName": owner,
        "kCGWindowName": f"window {window_id}",
        "kCGWindowLayer": layer,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": width, "Height": height},
        "kCGWindowIsOnscreen": on_screen,
        "kCGWindowAlpha": 1.0,
    }


# Front to back: the menu bar, a Finder panel over Finder's document,
# a Notes window, the document spanning both displays, and a minimized
# Finder window.
_MENU_BAR = _entry(_SERVER, _BAR, 24, (0, 0, 1000, 24))
_FINDER_PANEL = _entry(_FINDER, _PANEL, 25, (300, 100, 200, 150))
_DESKTOP = (
    _MENU_BAR,
    _FINDER_PANEL,
    _entry(_NOTES, _NOTE, 0, (600, 300, 400, 400)),
    _entry(_FINDER, _DOC, 0, (0, 0, 2000, 800)),
    _entry(_FINDER, _HIDDEN, 0, (0, 0, 300, 300), on_screen=False),
)


def _white(width: int, height: int) -> Any:
    context = Quartz.CGBitmapContextCreate(
        None, width, height, 8, 0,
        Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB),
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, width, height))
    return Quartz.CGBitmapContextCreateImage(context)


def _capture(bounds: dict[str, float], *, on_screen: bool = True) -> WindowCapture:
    """``bounds`` rendered at 2 pixels a point, as on a Retina display."""
    width, height = int(2 * bounds["width"]), int(2 * bounds["height"])
    return WindowCapture(
        image=_white(width, height), width=width, height=height, bounds=bounds,
        on_screen=on_screen, captured_at=0.0,
    )


class _Running:
    """A running app old enough that it is done opening windows."""

    def launchDate(self) -> None:
        return None


class _Desk(MacOS):
    """`MacOS` over the fake window server ``entries`` and two displays,
    recording every capture, click, and event it would post."""

    def __init__(self, *entries: dict[str, Any]) -> None:
        super().__init__()
        self.entries = entries or _DESKTOP
        self.window_captures: list[int] = []
        self.screen_requests: list[tuple[int, dict[str, float]]] = []
        self.clicks: list[tuple[int, tuple[float, float], int, tuple[float, float]]] = []
        self.posted: list[tuple[int | None, tuple[float, float]]] = []
        self.clock = 0.0
        # Hidden, the virtual pointer never starts its helper window.
        self.hide_pointer()

    def _now(self) -> float:
        return self.clock

    def _pause(self, seconds: float) -> None:
        self.clock += seconds

    def _ensure_screen_recording(self) -> None:
        return None

    def _ensure_accessibility(self) -> None:
        return None

    def _ensure_post_events(self) -> None:
        return None

    def _frontmost_app(self) -> dict[str, Any]:
        return _APPS[_NOTES]

    def _resolve_app(self, query: str | int | None) -> tuple[_Running, dict[str, Any]]:
        """Each app by name or pid; no query is the last app a call named."""
        if query is None and self._last_app is not None:
            query = self._last_app["pid"]
        for pid, info in _APPS.items():
            if query in (pid, info["name"]):
                return _Running(), info
        raise ApplicationNotFoundError(f"No running app matches {query!r}")

    def _window_list(self, *, on_screen_only: bool) -> tuple[dict[str, Any], ...]:
        return tuple(
            entry for entry in self.entries
            if entry["kCGWindowIsOnscreen"] or not on_screen_only
        )

    def _window_description(self, window_id: int) -> dict[str, Any] | None:
        return next(
            (entry for entry in self.entries if entry["kCGWindowNumber"] == window_id), None
        )

    def _window_image(
        self,
        window_id: int,
        *,
        max_width: int | None,
        max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        self.window_captures.append(window_id)
        window = self._window_entry(self._window_description(window_id))
        return _capture(window["bounds"], on_screen=window["on_screen"])

    def _displays(self) -> list[tuple[int, dict[str, float]]]:
        return _DISPLAYS

    def _screen_image(
        self,
        display_id: int,
        area: dict[str, float],
        frame: dict[str, float],
        *,
        max_width: int | None,
        max_height: int | None,
    ) -> WindowCapture:
        self.screen_requests.append((display_id, area))
        return _capture(area)

    def _post_click(
        self, pid: int, point: tuple[float, float], window: Any, *, button: str, clicks: int
    ) -> None:
        self._pointer_position = point
        self.clicks.append((pid, point, window.window_id, window.origin))

    def _post(self, event: object, pid: int | None) -> None:
        location = Quartz.CGEventGetLocation(event)
        self.posted.append((pid, (location.x, location.y)))


@pytest.mark.parametrize(
    ("app", "all_layers", "listed"),
    [
        ("Finder", False, [_DOC, _HIDDEN]),
        ("Finder", True, [_DOC, _PANEL, _HIDDEN]),
        (None, False, [_NOTE, _DOC]),
        (None, True, [_BAR, _PANEL, _NOTE, _DOC]),
    ],
    ids=["app", "app-all-layers", "screen", "screen-all-layers"],
)
def test_windows_lists_panels_and_menus_only_when_asked(app, all_layers, listed) -> None:
    """With no app, every app's windows on screen, front to back; menus,
    panels, and the menu bar only with ``all_layers``."""
    mac = _Desk()

    assert [window["window_id"] for window in mac.windows(app, all_layers=all_layers)] == listed


def test_windows_names_each_window_owner_and_layer() -> None:
    panel = next(
        window for window in _Desk().windows(all_layers=True) if window["window_id"] == _PANEL
    )

    assert (panel["pid"], panel["owner"], panel["layer"]) == (_FINDER, "Finder", 25)


def test_see_window_id_captures_a_panel_and_reports_its_layer_and_owner(tmp_path: Path) -> None:
    mac = _Desk()

    shot = mac.see(window_id=_PANEL, path=tmp_path / "panel.png")

    assert mac.window_captures == [_PANEL]
    assert (shot["window_id"], shot["layer"], shot["pid"]) == (_PANEL, 25, _FINDER)
    assert shot["app"] == _APPS[_FINDER]
    assert shot["bounds"] == {"x": 300.0, "y": 100.0, "width": 200.0, "height": 150.0}


def test_see_window_id_of_a_surface_no_app_owns_names_its_owner(tmp_path: Path) -> None:
    shot = _Desk().see(window_id=_BAR, path=tmp_path / "bar.png")

    assert shot["app"] == {
        "name": "Window Server", "bundle_id": None, "pid": _SERVER, "path": None,
    }
    assert shot["layer"] == 24


def test_see_of_an_app_showing_only_a_panel_names_its_window_id() -> None:
    """An app whose only surface is a panel or prompt used to fail with
    no capturable window; the error now names the call that captures it."""
    mac = _Desk(_MENU_BAR, _FINDER_PANEL)

    with pytest.raises(MacOSError) as caught:
        mac.see("Finder")

    assert f"mac.see(window_id={_PANEL})" in str(caught.value)
    assert caught.value.details["window_ids"] == [_PANEL]
    assert mac.window_captures == []


@pytest.mark.parametrize(
    ("surface", "display_id", "origin"),
    [({"display": 1}, 3, (1000.0, 0.0)), ({"region": (100, 50, 200, 100)}, 1, (100.0, 50.0))],
    ids=["display", "region"],
)
def test_a_display_or_region_shot_maps_screenshot_clicks_for_any_app(
    tmp_path: Path, surface, display_id, origin
) -> None:
    """A screen shot belongs to no app: its pixels map a click for the app
    whose window is there, 2 pixels to the point."""
    mac = _Desk()

    shot = mac.see(**surface, path=tmp_path / "screen.png")
    mac.click(40, 20, app="Finder")

    assert (shot["app"], shot["pid"], shot["window_id"]) == (None, None, None)
    assert [request[0] for request in mac.screen_requests] == [display_id]
    assert mac.clicks == [(_FINDER, (origin[0] + 20, origin[1] + 10), _DOC, (0.0, 0.0))]


@pytest.mark.parametrize(
    ("window_id", "routed", "origin"),
    [(None, _DOC, (0.0, 0.0)), (_PANEL, _PANEL, (300.0, 100.0))],
    ids=["content-rule", "named-panel"],
)
def test_a_click_lands_in_a_panel_only_when_window_id_names_it(
    tmp_path: Path, window_id, routed, origin
) -> None:
    mac = _Desk()
    mac.see(window_id=_PANEL, path=tmp_path / "panel.png")

    mac.click(50, 40, window_id=window_id)

    assert mac.clicks == [(_FINDER, (325.0, 120.0), routed, origin)]


def test_window_id_alone_names_the_app_a_click_goes_to() -> None:
    mac = _Desk()

    mac.click(320, 110, window_id=_PANEL, coordinate_space="screen")

    assert mac.clicks == [(_FINDER, (320.0, 110.0), _PANEL, (300.0, 100.0))]


@pytest.mark.parametrize(
    ("call", "code"),
    [
        ({"x": 10, "y": 10, "window_id": _PANEL}, ErrorCode.BAD_REQUEST),
        ({"x": 320, "y": 110, "window_id": _PANEL, "app": "Notes"}, ErrorCode.BAD_REQUEST),
        ({"x": 10, "y": 10, "window_id": _HIDDEN}, ErrorCode.WINDOW_CHANGED),
        ({"x": 10, "y": 10, "window_id": 404}, ErrorCode.ELEMENT_UNKNOWN),
    ],
    ids=["outside", "other-owner", "off-screen", "closed"],
)
def test_a_click_refuses_a_named_window_it_cannot_land_in(call, code) -> None:
    mac = _Desk()

    with pytest.raises(MacOSError) as caught:
        mac.click(coordinate_space="screen", **call)

    assert caught.value.code == code
    assert mac.clicks == []


def test_a_click_over_no_window_names_window_id_for_panels() -> None:
    mac = _Desk()

    with pytest.raises(MacOSError) as caught:
        mac.click(1500, 900, app="Finder", coordinate_space="screen")

    assert f"mac.windows({_FINDER}, all_layers=True)" in str(caught.value)
    assert mac.clicks == []


@pytest.mark.parametrize(
    ("call", "code", "parameter"),
    [
        ({"window_id": 404}, ErrorCode.ELEMENT_UNKNOWN, None),
        ({"window_id": "25"}, ErrorCode.BAD_REQUEST, "window_id"),
        ({"window_id": _PANEL, "app": "Notes"}, ErrorCode.BAD_REQUEST, "window_id"),
        ({"window_id": _PANEL, "window_index": 1}, ErrorCode.BAD_REQUEST, "window_index"),
        ({"display": 0, "region": (0, 0, 10, 10)}, ErrorCode.BAD_REQUEST, "region"),
        ({"display": 0, "app": "Finder"}, ErrorCode.BAD_REQUEST, "display"),
        ({"region": (0, 0, 10, 10), "after_change": True}, ErrorCode.BAD_REQUEST, "after_change"),
        ({"display": 2}, ErrorCode.BAD_REQUEST, "display"),
        ({"region": (900, 0, 200, 100)}, ErrorCode.BAD_REQUEST, "region"),
        ({"region": (0, 0, 0, 10)}, ErrorCode.BAD_REQUEST, "region"),
    ],
    ids=[
        "closed", "not-a-number", "other-owner", "id-and-index", "display-and-region",
        "display-with-app", "region-after-change", "no-such-display", "region-spans-displays",
        "empty-region",
    ],
)
def test_see_refuses_a_surface_it_cannot_capture(call, code, parameter) -> None:
    mac = _Desk()

    with pytest.raises(MacOSError) as caught:
        mac.see(**call)

    assert (caught.value.code, caught.value.details.get("parameter")) == (code, parameter)
    assert (mac.window_captures, mac.screen_requests) == ([], [])


def test_see_after_change_watches_the_window_window_id_names(tmp_path: Path) -> None:
    mac = _Desk()
    mac.see(window_id=_PANEL, path=tmp_path / "before.png")

    shot = mac.see(window_id=_PANEL, after_change=True, timeout=0, path=tmp_path / "after.png")

    assert shot["changed"] is False
    assert set(mac.window_captures) == {_PANEL}


# Each way to aim at screenshot pixels, none naming the app: through the
# run's own last `see`, or through the PNG ``screenshot`` names.
_AIMS = {
    "click": lambda mac, **shot: mac.click(400, 300, **shot),
    "drag": lambda mac, **shot: mac.drag(400, 300, 600, 500, duration=0, **shot),
    "scroll": lambda mac, **shot: mac.scroll(-50, **shot),
    "move": lambda mac, **shot: mac.move(400, 300, **shot),
}
_FINDER_MOVED = (
    _MENU_BAR, _FINDER_PANEL, _entry(_NOTES, _NOTE, 0, (600, 300, 400, 400)),
    _entry(_FINDER, _DOC, 0, (100, 0, 2000, 800)),
)
_FINDER_CLOSED = (_MENU_BAR, _FINDER_PANEL, _entry(_NOTES, _NOTE, 0, (600, 300, 400, 400)))


@pytest.mark.parametrize("aim", list(_AIMS.values()), ids=list(_AIMS))
def test_a_later_run_aims_through_the_png_an_earlier_see_wrote(tmp_path: Path, aim) -> None:
    """The record `see` keeps beside its PNG maps the pixels, and names
    the app, in a run that never saw it: input lands where the run that
    took it aimed."""
    png = tmp_path / "finder.png"
    earlier = _Desk()
    earlier.see("Finder", path=png)
    aimed = (aim(earlier), earlier.clicks, earlier.posted)

    later = _Desk()

    assert (aim(later, screenshot=png), later.clicks, later.posted) == aimed


@pytest.mark.parametrize(
    "aim",
    [*_AIMS.values(), lambda mac, **shot: mac.ax.at(400, 300, **shot)],
    ids=[*_AIMS, "ax.at"],
)
@pytest.mark.parametrize("desk", [_FINDER_MOVED, _FINDER_CLOSED], ids=["moved", "closed"])
def test_a_later_run_refuses_a_png_of_a_window_that_has_since_moved(
    tmp_path: Path, aim, desk
) -> None:
    png = tmp_path / "finder.png"
    _Desk().see("Finder", path=png)
    later = _Desk(*desk)

    with pytest.raises(MacOSError) as caught:
        aim(later, screenshot=png)

    assert caught.value.code == ErrorCode.WINDOW_CHANGED
    assert (later.clicks, later.posted) == ([], [])


@pytest.mark.parametrize(
    "record", [None, "not json", '{"pid": 42}'], ids=["missing", "garbage", "partial"]
)
def test_a_png_without_its_capture_record_is_refused(tmp_path: Path, record) -> None:
    png = tmp_path / "other.png"
    if record is not None:
        Path(f"{png}.json").write_text(record, encoding="utf-8")
    mac = _Desk()

    with pytest.raises(MacOSError) as caught:
        mac.click(10, 10, app="Finder", screenshot=png)

    assert (caught.value.code, caught.value.details["parameter"]) == (
        ErrorCode.BAD_REQUEST, "screenshot",
    )
    assert mac.clicks == []


def test_screenshot_pixels_with_no_see_in_the_run_name_screenshot() -> None:
    """The refusal names the argument that reuses an earlier run's PNG."""
    with pytest.raises(MacOSError) as caught:
        _Desk().click(10, 10, app="Finder")

    assert "screenshot=" in str(caught.value)
    assert caught.value.details["parameter"] == "screenshot"
