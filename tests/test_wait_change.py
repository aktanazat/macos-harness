"""`mac.wait_change` and `mac.see(after_change=True)` against a scripted
window: each capture renders the next frame of a script and then holds
the last, and the clock moves only when the code under test pauses, so
every wait is counted, never slept. The frames are real images, so the
comparison under test is the one a live window gets."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import Quartz
from Foundation import NSURL

from macos_harness.capture import WindowCapture
from macos_harness.errors import ErrorCode
from macos_harness.macos import MacOS, MacOSError, _TreeSnapshot

_SRGB = Quartz.CGColorSpaceCreateWithName(Quartz.kCGColorSpaceSRGB)
# A 400 x 300-point window rendered 2x, as on a Retina display.
_BOUNDS = {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0}


def _frame(*blocks: tuple[float, float, float, float]) -> Any:
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

    def __init__(self, *frames: Any, texts: tuple[str, ...] = ()) -> None:
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

    def _resolve_app(self, query: str | int | None) -> tuple[None, dict[str, Any]]:
        return None, {"name": "Test", "pid": 42, "bundle_id": "test", "path": None}

    def windows(
        self, app: str | int | None = None, *, all_layers: bool = False
    ) -> list[dict[str, Any]]:
        return [{"window_id": 7, "title": "Doc", "on_screen": True, "layer": 0}]

    @classmethod
    def _frontmost_app(cls) -> dict[str, Any]:
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


def _outcome(call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
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


def test_wait_change_returns_once_the_new_look_has_held_still() -> None:
    """The result appears on the second look and holds: the wait returns
    on the first look half a second later, after five short pauses."""
    mac = _Screen(_BLANK, _BLANK, _RESULT)

    result = mac.wait_change("Test")

    assert (result["changed"], result["settled"], result["baseline"]) == (True, True, "now")
    assert result["polls"] == 5
    assert mac.pauses == [0.2] * 5


def test_wait_change_compares_against_the_last_see_of_the_window(tmp_path: Path) -> None:
    """The action changed the window before the wait began: measured
    against the last `see`, the first look already counts, and every look
    renders at that see's exact size so the two compare pixel for pixel."""
    mac = _Screen(_BLANK, _RESULT)
    mac.see("Test", path=tmp_path / "before.png")

    result = mac.wait_change("Test")

    assert (result["changed"], result["baseline"], result["polls"]) == (True, "see", 4)
    assert mac.pauses == [0.2] * 3
    assert mac.sizes == [None] + [(800, 600)] * 4


def test_a_second_wait_measures_from_where_the_first_one_settled(tmp_path: Path) -> None:
    """Nothing changes after the first wait saw the result, so the next
    wait times out instead of counting that same change again."""
    mac = _Screen(_BLANK, _RESULT)
    mac.see("Test", path=tmp_path / "before.png")
    mac.wait_change("Test")

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", timeout=1.0)

    assert (raised.value.code, raised.value.details["changed"]) == (ErrorCode.TIMEOUT, False)


def test_a_blinking_caret_is_no_change() -> None:
    mac = _Screen(_BLANK, _CARET, _BLANK, _CARET, _BLANK, _CARET)

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", timeout=1.0)

    assert raised.value.code == ErrorCode.TIMEOUT
    assert (raised.value.details["changed"], raised.value.details["polls"]) == (False, 5)


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
    tmp_path: Path, after: Any, changed: bool, gray: int
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

    assert (result["changed"], result["settled"], result["polls"]) == (True, True, 5)
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
    tmp_path: Path, seen: bool, options: dict[str, Any]
) -> None:
    mac = _Screen(_BLANK)
    if seen:
        mac.see("Test", path=tmp_path / "before.png")

    with pytest.raises(MacOSError) as raised:
        mac.wait_change("Test", **options)

    assert raised.value.code == ErrorCode.BAD_REQUEST
    assert mac.pauses == []
