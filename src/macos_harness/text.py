"""A window's text read from its pixels by Apple Vision, for a window
whose controls accessibility cannot see: iPhone Mirroring, a canvas, an
image, a game, a remote desktop.

`MacOS.text` captures the window at its full Retina resolution and hands
the picture here. Lines read ``[t3] "Continue"``, top to bottom and left
to right; ``text["t3"]`` is the line's match, which ``click(element=)``
clicks at its center once the same box, read again, still shows it.
Printing a read makes the lines it shows the agent's last look; until a
newer read is printed, an id whose line reads otherwise, or sits
elsewhere in the window, than that look showed is refused.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from .errors import ErrorCode, MacOSError
from .receipts import JSONValue
from .state import matching

_VISION = "/System/Library/Frameworks/Vision.framework"
# VNRequestTextRecognitionLevelAccurate: the fast level reads six
# Latin-script languages and drops small text.
_ACCURATE = 0

Match = Mapping[str, JSONValue]


class Frame(TypedDict):
    x: float
    y: float
    width: float
    height: float


class TextLine(TypedDict):
    text: str
    confidence: float
    frame: Frame


@functools.cache
def _vision() -> tuple[Any, Any]:
    """Vision's text request and image handler classes, from the system
    framework itself: about 8ms, where its pyobjc wrapper costs 100ms or
    more and brings Core ML along."""
    try:
        import objc
        from Foundation import NSBundle

        loaded = NSBundle.bundleWithPath_(_VISION).load()
    except ImportError:
        loaded = False
    if not loaded:
        raise MacOSError("Apple Vision is not available on this Mac", code=ErrorCode.UNSUPPORTED_OP)
    objc.registerMetaDataForSelector(
        b"VNImageRequestHandler", b"performRequests:error:",
        {"arguments": {3: {"type_modifier": b"o"}}},
    )
    return objc.lookUpClass("VNRecognizeTextRequest"), objc.lookUpClass("VNImageRequestHandler")


def recognize(image: object, area: Mapping[str, float]) -> list[TextLine]:
    """The lines Vision reads in ``image``, a picture of ``area`` (screen
    points), each with its ``text``, its ``confidence`` from 0 to 1, and
    its ``frame`` in screen points. A line Vision is unsure of stays: a
    short label often reads right at low confidence."""
    request_class, handler_class = _vision()
    request = request_class.alloc().init()
    request.setRecognitionLevel_(_ACCURATE)
    request.setAutomaticallyDetectsLanguage_(True)
    # None, not an empty dictionary: an empty one crashed Vision on macOS 27.
    handler = handler_class.alloc().initWithCGImage_options_(image, None)
    done, error = handler.performRequests_error_([request], None)
    if not done:
        reason = error.localizedDescription() if error is not None else "no reason given"
        raise MacOSError(f"Vision could not read the picture: {reason}", code=ErrorCode.UNSUPPORTED_OP)
    lines: list[TextLine] = []
    for observation in request.results() or ():
        candidates = observation.topCandidates_(1)
        if not candidates:
            continue
        # Vision measures in fractions of the picture from its bottom left.
        box = observation.boundingBox()
        lines.append({
            "text": str(candidates[0].string()),
            "confidence": round(float(candidates[0].confidence()), 2),
            "frame": {
                "x": round(area["x"] + box.origin.x * area["width"], 1),
                "y": round(area["y"] + (1 - box.origin.y - box.size.height) * area["height"], 1),
                "width": round(box.size.width * area["width"], 1),
                "height": round(box.size.height * area["height"], 1),
            },
        })
    return _reading_order(lines)


def _middle(line: TextLine) -> float:
    return line["frame"]["y"] + line["frame"]["height"] / 2


def _reading_order(lines: Sequence[TextLine]) -> list[TextLine]:
    """``lines`` in rows from the top, each row from the left. A line
    joins the row above when its middle lies within half the shorter
    line's height of that row's first line's middle."""
    rows: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda line: (line["frame"]["y"], line["frame"]["x"])):
        if rows:
            first = rows[-1][0]
            reach = min(line["frame"]["height"], first["frame"]["height"]) / 2
            if abs(_middle(line) - _middle(first)) < reach:
                rows[-1].append(line)
                continue
        rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda line: line["frame"]["x"])]


class WindowText:
    """What `MacOS.text` returns: printable lines, and the match each id
    on them names, ready for ``click(element=)``: ``text["t3"]``, or
    ``text[3]``. Printing it makes the lines it shows the agent's last
    look at the window."""

    __slots__ = ("_matches", "_on_show", "_seen", "_shown", "_text")

    def __init__(
        self, text: str, matches: Mapping[str, Match], *, seen: Mapping[str, Match], on_show: Callable[[], None],
    ) -> None:
        self._text = text
        self._matches = matches
        self._seen = seen
        self._on_show = on_show
        self._shown = False

    @property
    def text(self) -> str:
        if not self._shown:
            self._shown = True
            self._on_show()
        return self._text

    def __str__(self) -> str:
        return self.text

    __repr__ = __str__

    def __getitem__(self, line_id: int | str) -> Match:
        key = f"t{line_id}" if isinstance(line_id, int) else line_id
        try:
            match = self._matches[key]
        except KeyError:
            raise KeyError(f"[{key}] is not in this read; read mac.text again") from None
        # An id is a line's place in reading order, so a line appearing
        # above hands it to another line.
        was = self._seen.get(key)
        if not self._shown and was is not None and not _same_line(was, match):
            raise MacOSError(
                f"[{key}] changed since your last look: it was {_said(was)}, now {_said(match)} in the "
                "window. Print this read, then pick again.",
                code=ErrorCode.ELEMENT_UNKNOWN,
                details={"id": key, "was": was["text"], "now": match["text"]},
            )
        return match

    def __contains__(self, line_id: object) -> bool:
        return (f"t{line_id}" if isinstance(line_id, int) else line_id) in self._matches


def _spot(match: Match) -> tuple[float, float, float]:
    """``match``'s center in its window, in points, and its height."""
    frame, window = match["frame"], match["window"]
    return (
        frame["x"] + frame["width"] / 2 - window["x"],
        frame["y"] + frame["height"] / 2 - window["y"],
        frame["height"],
    )


def _same_line(was: Match, now: Match) -> bool:
    """Whether ``now`` is the line ``was`` showed: its text, ignoring case
    and spacing, centered within half a line's height of the same place
    in its window, wherever the window went."""
    (was_x, was_y, was_height), (now_x, now_y, now_height) = _spot(was), _spot(now)
    reach = min(was_height, now_height) / 2
    return (
        _folded(str(was["text"])) == _folded(str(now["text"]))
        and abs(was_x - now_x) < reach and abs(was_y - now_y) < reach
    )


def _said(match: Match) -> str:
    x, y, _ = _spot(match)
    return f"{json.dumps(match['text'], ensure_ascii=False)} at ({x:.0f}, {y:.0f})"


def window_text(
    lines: Sequence[TextLine],
    *,
    header: str,
    query: str | None,
    pid: int,
    window_id: int,
    window: Mapping[str, float],
    seen: Mapping[str, Match],
    remember: Callable[[Mapping[str, Match]], None],
) -> WindowText:
    """``lines`` `recognize` read in window ``window_id`` of process
    ``pid``, whose bounds were ``window``, as `MacOS.text` returns them.
    ``seen`` holds the line the agent last saw for each id there, and
    ``remember`` keeps the lines the text shows once it is read."""
    matches: dict[str, Match] = {}
    rendered: list[str] = []
    for number, line in enumerate(lines, 1):
        matches[f"t{number}"] = {**line, "pid": pid, "window_id": window_id, "window": dict(window)}
        rendered.append(f"[t{number}] {json.dumps(line['text'], ensure_ascii=False)}")
    shown = list(range(len(lines)))
    if query is None:
        body = rendered or ["No text was read in this window."]
    else:
        label = json.dumps(query, ensure_ascii=False)
        shown = matching([line["text"].lower() for line in lines], query)
        body = (
            [f"Query {label}: {len(shown)} of {len(rendered)} lines.", *(rendered[i] for i in shown)]
            if shown else
            [f"Query {label} matched nothing among {len(rendered)} lines; omit query to see the window."]
        )
    keys = [f"t{i + 1}" for i in shown]
    return WindowText(
        "\n".join([header, *body]), matches, seen=seen,
        on_show=lambda: remember({key: matches[key] for key in keys}),
    )


def is_text_match(element: object) -> bool:
    """Whether ``element`` is a line `MacOS.text` read: a match with text
    and a frame, and no accessibility role."""
    return (
        isinstance(element, Mapping) and "role" not in element
        and isinstance(element.get("text"), str) and isinstance(element.get("frame"), Mapping)
    )


def names_text_line(element: object) -> bool:
    """Whether ``element`` is a line `MacOS.text` read, or one of its ids
    as printed: ``"t3"`` or ``"[t3]"``."""
    return is_text_match(element) or (
        isinstance(element, str) and re.fullmatch(r"\[?t\d+\]?", element) is not None
    )


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def still_reads(match: Match, lines: Sequence[TextLine]) -> bool:
    """Whether one of ``lines``, read again from around ``match``'s box,
    is its text, ignoring case and spacing. A line that grew changed:
    "Share" now reading "Shared" is no longer the line read."""
    was = _folded(str(match["text"]))
    return any(_folded(line["text"]) == was for line in lines)
