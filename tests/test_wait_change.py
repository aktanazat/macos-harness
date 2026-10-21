"""`mac.wait_change` and `mac.see(after_change=True)` against a scripted
window: each capture renders the next frame of a script and then holds
the last, and the clock moves only when the code under test pauses, so
every wait is counted, never slept. The frames are real images, so the
comparison under test is the one a live window gets."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
import Quartz
from Foundation import NSURL

from macos_harness.capture import WindowCapture
from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOS, MacOSError, _TreeSnapshot

_SRGB = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
# A 400 x 300-point window rendered 2x, as on a Retina display.
_BOUNDS = {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0}


def _frame(*blocks: tuple[float, float, float, float]) -> object:
    """The white window with black ``blocks`` (x, y, width, height in
    points from its top left)."""
    context = Quartz.CGBitmapContextCreate(
        None, 800, 600, 8, 0, _SRGB, Quartz.kCGImageAlphaPremultipliedLast
    )
    Quartz.CGContextSetRGBFillColor(context, 1, 1, 1, 1)
    Quartz.CGContextFillRect(context, Quartz.CGRectMake(0, 0, 800, 600))
    Quartz.CGContextSetRGBFillColor(context, 0, 0, 0, 1)
    for x, y, width, height in blocks:
        Quartz.CGContextFillRect(
            context, Quartz.CGRectMake(2 * x, 600 - 2 * (y + height), 2 * width, 2 * height)
        )
    return Quartz.CGBitmapContextCreateImage(context)


_BLANK = _frame()
_RESULT = _frame((150, 100, 100, 40))
_CARET = _frame((200, 140, 1, 18))
_DOT = _frame((200, 140, 6, 6))


class _Screen(MacOS):
    """`MacOS` over one window whose captures follow ``frames``."""

    def __init__(self, *frames: object, texts: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.frames = frames
        self.sizes: list[tuple[int, int] | None] = []
        self.texts = texts
        self.walks = 0
        self.clock = 0.0
        self.pauses: list[float] = []

    def _now(self) -> float:
        return self.clock

    def _pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.clock += seconds

    def _ensure_screen_recording(self) -> None:
        return None

    def _ensure_accessibility(self) -> None:
        return None

    def _resolve_app(self, query: str | int | None) -> tuple[None, dict[str, object]]:
        return None, {"name": "Test", "pid": 42, "bundle_id": "test", "path": None}

    def windows(
        self, app: str | int | None = None, *, all_layers: bool = False
    ) -> list[dict[str, object]]:
        return [{"window_id": 7, "title": "Doc", "on_screen": True, "layer": 0}]

    @classmethod
    def _frontmost_app(cls) -> dict[str, object]:
        return {"name": "Test", "bundle_id": "test", "pid": 42, "path": None}

    def _window_image(
        self,
        window_id: int,
        *,
        max_width: int | None,
        max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        image = self.frames[min(len(self.sizes), len(self.frames) - 1)]
        self.sizes.append(size)
        return WindowCapture(
            image=image, width=800, height=600, bounds=_BOUNDS, on_screen=True, captured_at=0.0
        )

    def _snapshot_tree(self, root: object, **options: object) -> _TreeSnapshot:
        """A tree walk as the real one does it: it forgets every earlier
        element index and numbers the one text element it finds anew."""
        self._elements = {}
        text = self.texts[min(self.walks, len(self.texts) - 1)]
        self.walks += 1
        index = self._remember_element(object())
        node = {"element_index": index, "depth": 0, "role": "AXStaticText", "value": text}
        return _TreeSnapshot([node], False, False, False)


class _Keypad(_Screen):
    """`_Screen` whose window shows the result once a key reaches its app,
    as Calculator's does; each capture notes how many events had reached
    it by then, ``listings`` counts the window listings, and the first
    ``failing`` captures fail."""

    def __init__(self, *, failing: int = 0) -> None:
        super().__init__(_BLANK)
        self.posted = 0
        self.captured_after: list[int] = []
        self.listings = 0
        self.failing = failing

    def _ensure_post_events(self) -> None:
        return None

    def _post(self, event: object, pid: int | None) -> None:
        self.posted += 1

    def windows(
        self, app: str | int | None = None, *, all_layers: bool = False
    ) -> list[dict[str, object]]:
        self.listings += 1
        return super().windows(app, all_layers=all_layers)

    def _window_image(
        self,
        window_id: int,
        *,
        max_width: int | None,
        max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        if self.failing:
            self.failing -= 1
            raise MacOSError(
                f"Window capture returned no image for window {window_id}",
                code=ErrorCode.AX_ERROR,
            )
        self.captured_after.append(self.posted)
        return WindowCapture(
            image=_RESULT if self.posted else _BLANK, width=800, height=600, bounds=_BOUNDS,
            on_screen=True, captured_at=0.0,
        )


class _Slide(_Screen):
    """`_Screen` whose window shows a 40-point square sliding 200 points
    right over the first 0.3 seconds, an animation still running when the
    wait begins, and then resting."""

    def _window_image(
        self,
        window_id: int,
        *,
        max_width: int | None,
        max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        x = 100 + 200 * min(self.clock, 0.3) / 0.3
        return WindowCapture(
            image=_frame((x, 100, 40, 40)), width=800, height=600, bounds=_BOUNDS,
            on_screen=True, captured_at=0.0,
        )


_RETURN = 36  # kVK_Return
_TYPED = (150, 60, 100, 20)
_ANSWER = (150, 160, 100, 40)
_PRESSED = (150, 240, 100, 20)


class _Form(_Screen):
    """`_Screen` over a form whose typed text shows at once and whose
    answer to Return shows 0.3 seconds after it, as a server's does;
    with ``pressed``, Return's button also looks pressed at once."""

    def __init__(self, *, pressed: bool = False) -> None:
        super().__init__(_BLANK)
        self.pressed = pressed
        self.typed = False
        self.returned_at: float | None = None

    def _ensure_post_events(self) -> None:
        return None

    def _post(self, event: object, pid: int | None) -> None:
        if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode) != _RETURN:
            self.typed = True
        elif self.returned_at is None:
            self.returned_at = self.clock

    def _window_image(
        self,
        window_id: int,
        *,
        max_width: int | None,
        max_height: int | None,
        size: tuple[int, int] | None = None,
    ) -> WindowCapture:
        blocks = [_TYPED] if self.typed else []
        if self.pressed and self.returned_at is not None:
            blocks.append(_PRESSED)
        if self.returned_at is not None and self.clock >= self.returned_at + 0.3:
            blocks.append(_ANSWER)
        return WindowCapture(
            image=_frame(*blocks), width=800, height=600, bounds=_BOUNDS,
            on_screen=True, captured_at=0.0,
        )


def _outcome(call: Callable[[], Mapping[str, object]]) -> Mapping[str, object]:
    """What the wait reports, whether it returned or timed out."""
    try:
        return call()
    except MacOSError as exc:
        assert exc.code == ErrorCode.TIMEOUT
        return exc.details


def _png_gray(path: str, x: int, y: int) -> int:
    """The gray level of pixel (x, y) from the top left of the PNG at ``path``."""
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    height = Quartz.CGImageGetHeight(image)
    context = Quartz.CGBitmapContextCreate(
        None, 1, 1, 8, 1, Quartz.CGColorSpaceCreateDeviceGray(), Quartz.kCGImageAlphaNone
    )
    Quartz.CGContextDrawImage(
        context,
        Quartz.CGRectMake(-x, -(height - 1 - y), Quartz.CGImageGetWidth(image), height),
        image,
    )
    pixel = Quartz.CGBitmapContextCreateImage(context)
    return bytes(Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(pixel)))[0]


def test_wait_change_answers_within_a_quarter_second_of_a_change_that_holds() -> None:
    """The result shows on the first look and holds: the wait returns
    within four looks and a quarter of a second, the settle included."""
    mac = _Screen(_BLANK, _RESULT)

    result = mac.wait_change("Test")

    assert (result["changed"], result["settled"], result["baseline"]) == (True, True, "now")
    assert result["polls"] <= 4
    assert result["elapsed_ms"] <= 250


def test_wait_change_waits_for_a_moving_change_to_hold_still() -> None:
    """The square is still sliding when the first looks see it: the wait
    returns only once it has rested a settle after its last move at 0.3
    seconds, so the shorter settle never answers mid-animation."""
    mac = _Slide()

    result = mac.wait_change("Test")

    assert (result["changed"], result["settled"]) == (True, True)
    assert result["elapsed_ms"] >= 450


def test_wait_change_compares_against_the_last_see_of_the_window(tmp_path: Path) -> None:
    """The action changed the window before the wait began: measured
    against the last `see`, it counts, the wait answers within a quarter
    second, and every look renders at that see's exact size so the two
    compare pixel for pixel."""
    mac = _Screen(_BLANK, _RESULT)
    mac.see("Test", path=tmp_path / "before.png")

    result = mac.wait_change("Test")

    assert (result["changed"], result["baseline"]) == (True, "see")
    assert result["elapsed_ms"] <= 250
    assert (mac.sizes[0], set(mac.sizes[1:])) == (None, {(800, 600)})


def test_a_second_wait_measures_from_where_the_first_one_settled(tmp_path: Path) -> None:
    """Nothing changes after the first wait saw the result, so the next
    wait times out instead of counting that same change again."""
    mac = _Screen(_BLANK, _RESULT)
    mac.see("Test", path=tmp_path / "before.png")
    mac.wait_change("Test")

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", timeout=1.0)

    assert (raised.value.code, raised.value.details["changed"]) == (ErrorCode.TIMEOUT, False)


@pytest.mark.parametrize(
    "wait",
    [
        lambda mac, path: _outcome(lambda: mac.wait_change("Test", timeout=1.0))["changed"],
        lambda mac, path: mac.see("Test", path=path, after_change=True, timeout=1.0)["changed"],
    ],
    ids=["wait_change", "see-after-change"],
)
def test_a_new_session_sees_the_change_its_own_key_made(
    tmp_path: Path, wait: Callable[[MacOS, Path], object]
) -> None:
    """No picture of the window is held yet, so the key takes one just
    before it goes out: the wait measures from there instead of from
    after the result already showed, for the cost of that one capture."""
    mac = _Keypad()

    mac.key("5", app="Test")
    changed = wait(mac, tmp_path / "after.png")

    assert changed is True
    assert mac.captured_after.count(0) == 1


def test_a_wait_after_typing_and_return_measures_from_just_before_return(
    tmp_path: Path,
) -> None:
    """The typing showed at once and Return's answer comes 0.3 seconds
    later: Return pictures the window again, so the wait measures from
    the typed text and captures the answer instead of returning on the
    typing."""
    mac = _Form()

    mac.type("hello", app="Test")
    mac.key("Return", app="Test")
    shot = mac.see("Test", path=tmp_path / "after.png", after_change=True, timeout=2.0)

    assert _png_gray(shot["path"], 400, 360) == 0


def test_see_after_change_holds_its_settle_past_quick_feedback(tmp_path: Path) -> None:
    """Return's button looks pressed at once and its answer comes 0.3
    seconds later: a settle longer than that gap carries `see` past the
    pressed look to the answer."""
    mac = _Form(pressed=True)

    mac.key("Return", app="Test")
    shot = mac.see(
        "Test", path=tmp_path / "after.png", after_change=True, settle=0.5, timeout=2.0
    )

    assert _png_gray(shot["path"], 400, 360) == 0


def _see(mac: MacOS, path: Path) -> None:
    mac.see("Test", path=path)


def _key_and_wait(mac: MacOS, path: Path) -> None:
    mac.key("5", app="Test")
    mac.wait_change("Test")


@pytest.mark.parametrize("look", [_see, _key_and_wait], ids=["after-see", "after-wait"])
def test_a_key_right_after_a_look_lists_no_windows_and_takes_no_picture(
    tmp_path: Path, look: Callable[[MacOS, Path], None]
) -> None:
    """The look pictured the app's first window after every earlier
    input, so the key adds neither a window listing nor a capture."""
    mac = _Keypad()
    look(mac, tmp_path / "before.png")
    spent = (mac.listings, len(mac.captured_after))

    mac.key("5", app="Test")

    assert (mac.listings, len(mac.captured_after)) == spent


def test_a_key_whose_picture_fails_still_goes_out_and_the_wait_measures_from_now() -> None:
    """The capture before the key fails: the key still reaches the app,
    and the wait measures from when it starts, as it did before inputs
    were pictured."""
    mac = _Keypad(failing=1)

    mac.key("5", app="Test")
    result = _outcome(lambda: mac.wait_change("Test", timeout=1.0))

    assert (mac.posted, result["baseline"]) == (2, "now")


def test_a_blinking_caret_is_no_change() -> None:
    mac = _Screen(_BLANK, _CARET, _BLANK, _CARET, _BLANK, _CARET)

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", timeout=1.0)

    assert raised.value.code == ErrorCode.TIMEOUT
    assert (raised.value.details["changed"], raised.value.details["elapsed_ms"]) == (False, 1000)


@pytest.mark.parametrize(
    ("region", "changed"),
    [(None, False), ((380, 260, 40, 40), True)],
    ids=["whole-window", "region-around-it"],
)
def test_a_small_change_counts_inside_a_watched_region(
    tmp_path: Path, region: tuple[float, float, float, float] | None, changed: bool
) -> None:
    """A 6 x 6-point dot is below what counts for a whole window, but a
    region of 20 x 20 points around it watches for a smaller change."""
    mac = _Screen(_BLANK, _DOT)
    mac.see("Test", path=tmp_path / "before.png")

    result = _outcome(lambda: mac.wait_change("Test", region=region, timeout=1.0))

    assert result["changed"] is changed


@pytest.mark.parametrize(
    ("after", "changed", "gray"),
    [(_BLANK, False, 255), (_RESULT, True, 0)],
    ids=["no-change", "change"],
)
def test_see_after_change_captures_once_the_wait_ends(
    tmp_path: Path, after: object, changed: bool, gray: int
) -> None:
    """No change is no error here: the capture still comes, after the
    wait, and says whether the window changed."""
    mac = _Screen(_BLANK, after)
    mac.see("Test", path=tmp_path / "before.png")

    shot = mac.see("Test", path=tmp_path / "after.png", after_change=True, timeout=1.0)

    assert shot["changed"] is changed
    assert _png_gray(shot["path"], 400, 240) == gray


def test_wait_change_by_ax_compares_text_and_keeps_element_indexes() -> None:
    """The text changes on the second look and holds; the tree walks do
    not cost the caller the element index it already holds."""
    mac = _Screen(texts=("Idle", "Idle", "Done"))
    held = mac._remember_element("button")

    result = mac.wait_change("Test", by="ax")

    assert (result["changed"], result["settled"]) == (True, True)
    assert mac._element(held) == "button"


@pytest.mark.parametrize(
    ("seen", "options"),
    [
        (False, {"by": "words"}),
        (False, {"timeout": -1}),
        (False, {"region": (0, 0, 10, 10), "by": "ax"}),
        (False, {"region": (0, 0, 10, 10)}),
        (True, {"region": (790, 0, 20, 20)}),
    ],
    ids=["unknown-by", "negative-timeout", "region-with-ax", "region-before-see", "region-outside"],
)
def test_wait_change_refuses_bad_requests_before_waiting(
    tmp_path: Path, seen: bool, options: dict[str, object]
) -> None:
    mac = _Screen(_BLANK)
    if seen:
        mac.see("Test", path=tmp_path / "before.png")

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", **options)

    assert raised.value.code == ErrorCode.BAD_REQUEST
    assert mac.pauses == []
