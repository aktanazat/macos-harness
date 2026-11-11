"""``mac.do.steps``: several ``mac.do`` verbs sent in order as one call.

Each step is its own verb call, with that verb's person pause, input
lock, ``once`` token and receipt. The first step that fails ends the run:
nothing after it is sent, and nothing is retried, so a dispatch whose
effect is unknown is never doubled. The run reads the app's focused
window before the first step and after the last, keeping the caller's
element indexes and last look as they were, and says what changed in
between. It pictures the window before each step, so a failed step
leaves its picture from before and after, and the window's text after
it, in files.
"""

from __future__ import annotations

import inspect
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .errors import ErrorCode, MacOSError
from .receipts import (
    Equals,
    ErrorPayload,
    Gone,
    JSONValue,
    OperationError,
    Present,
    Receipt,
    validate_app_selector,
)
from .state import AppState, changes

if TYPE_CHECKING:
    from .capture import WindowCapture
    from .ops import Operations, _Host

# The verbs a step may name. Each acts on the run's app; an expect step's
# condition is scoped to it when the condition names no app itself.
OPS = (
    "press", "set", "toggle", "click", "key", "type", "fill", "paste", "open_files", "menu", "menu_search",
    "status_item", "window", "reveal", "expect",
)
_SCOPES = ("app", "apps", "all_apps")
# A picture fits this many pixels a side: a sheet or dialog coming or
# going still shows, and one window renders in about 35 ms.
_PICTURE = 320


@dataclass(frozen=True, slots=True)
class StepsResult:
    """What `Operations.steps` did.

    ``receipts`` holds one receipt per step that ran, in order, the
    failing step's last when it got that far. ``stopped_at`` is the index
    of the step that ended the run and ``error`` why; the steps after it
    were not sent. ``changes`` is what changed in the app's focused window
    from before the first step to after the last one that ran. ``strip``
    names the files a failed step leaves: the window's picture before and
    after it, and a text file with ``changes`` and the window's lines
    after it; or it says why they could not be written.
    """

    status: Literal["done", "stopped"]
    steps: int
    receipts: tuple[Receipt, ...]
    changes: str
    duration_s: float
    stopped_at: int | None = None
    error: ErrorPayload | None = None
    strip: tuple[Path, ...] | str = ()

    @property
    def ok(self) -> bool:
        return self.status == "done"

    def __str__(self) -> str:
        if self.stopped_at is None or self.error is None:
            return f"{self.steps} steps done in {self.duration_s:.2f}s.\n{self.changes}"
        rest = self.stopped_at + 1
        lines = [
            f"Stopped at steps[{self.stopped_at}] of {self.steps} after {self.duration_s:.2f}s"
            + (f"; steps[{rest}:] were not sent." if rest < self.steps else "."),
            f"steps[{self.stopped_at}]: {self.error['message']}",
        ]
        if len(self.receipts) > self.stopped_at:
            lines.append(f"steps[{self.stopped_at}] {self.receipts[-1]}")
        lines.append(
            self.strip if isinstance(self.strip, str)
            else f"Files on steps[{self.stopped_at}]: " + " ".join(map(str, self.strip))
        )
        return "\n".join([*lines, self.changes])

    def to_json(self) -> dict[str, JSONValue]:
        return {
            "status": self.status,
            "steps": self.steps,
            "stopped_at": self.stopped_at,
            "error": None if self.error is None else {**self.error},
            "receipts": [receipt.to_json() for receipt in self.receipts],
            "changes": self.changes,
            "strip": self.strip if isinstance(self.strip, str) else [str(path) for path in self.strip],
            "duration_s": self.duration_s,
        }


def run_steps(ops: Operations, steps: Sequence[Mapping[str, object]], *, app: str | int) -> StepsResult:
    """Check every step against its verb, then send them in order; see
    `Operations.steps`."""
    if isinstance(app, bool) or not isinstance(app, str | int):
        raise MacOSError(
            "steps needs app=, the app every step acts on",
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "app"},
        )
    validate_app_selector(app, parameter="app")
    if isinstance(steps, str | bytes) or not isinstance(steps, Sequence) or not steps:
        raise MacOSError(
            'steps is a list of steps, each a dict naming its verb under "op"',
            code=ErrorCode.BAD_REQUEST,
            details={"parameter": "steps"},
        )
    calls = [_call(ops, index, step, app) for index, step in enumerate(steps)]
    host = ops._host
    started = ops._monotonic()
    receipts: list[Receipt] = []
    stop: tuple[int, MacOSError, WindowCapture | str] | None = None
    with ops._dispatch_lock:
        first = _state(host, app)
        for index, call in enumerate(calls):
            before = _picture(host, app)
            try:
                receipts.append(call())
            except OperationError as exc:
                receipts.append(exc.receipt)
                stop = index, exc, before
                break
            except MacOSError as exc:
                stop = index, exc, before
                break
            except (TypeError, ValueError, AttributeError) as exc:
                # Some verbs fail on a value of the wrong type with a plain
                # Python error; the steps before it were sent all the same.
                stop = index, MacOSError(f"{type(exc).__name__}: {exc}", code=ErrorCode.BAD_REQUEST), before
                break
        after = _state(host, app)
        change = _change(first, after)
        if stop is None:
            return StepsResult("done", len(calls), tuple(receipts), change, ops._monotonic() - started)
        index, error, before = stop
        strip = _strip(index, before, _picture(host, app), change, after)
    return StepsResult(
        "stopped", len(calls), tuple(receipts), change, ops._monotonic() - started,
        stopped_at=index, error=error.to_json(), strip=strip,
    )


def _call(ops: Operations, index: int, step: Mapping[str, object], app: str | int) -> Callable[[], Receipt]:
    op = step.get("op") if isinstance(step, Mapping) else None
    if op not in OPS:
        raise MacOSError(
            f'steps[{index}] must be a dict naming its verb under "op", one of {", ".join(OPS)}',
            code=ErrorCode.BAD_REQUEST,
            details={"step": index},
        )
    arguments = {key: value for key, value in step.items() if key != "op"}
    scoped = [key for key in _SCOPES if key in arguments]
    if scoped:
        raise MacOSError(
            f"steps[{index}] names {scoped[0]}=; every step acts on the app steps() names",
            code=ErrorCode.BAD_REQUEST,
            details={"step": index, "parameter": scoped[0]},
        )
    if op == "expect":
        condition = arguments.get("condition")
        if not isinstance(condition, Present | Gone | Equals):
            raise MacOSError(
                f"steps[{index}] needs condition=, a present(...), gone(...) or equals(...)",
                code=ErrorCode.BAD_REQUEST,
                details={"step": index, "parameter": "condition"},
            )
        if condition.app is None and condition.apps is None and not condition.all_apps:
            arguments["condition"] = replace(condition, app=app)
    else:
        arguments["app"] = app
    verb = getattr(ops, op)
    try:
        inspect.signature(verb).bind(**arguments)
    except TypeError as exc:
        raise MacOSError(
            f"steps[{index}] does not fit {op}: {exc}",
            code=ErrorCode.BAD_REQUEST,
            details={"step": index},
        ) from None
    return partial(verb, **arguments)


def _state(host: _Host, app: str | int) -> AppState | MacOSError:
    held = host._elements
    try:
        return host.state(app)
    except MacOSError as exc:
        return exc
    finally:
        host._elements = held


def _picture(host: _Host, app: str | int) -> WindowCapture | str:
    """The app's focused window, or why it has no picture."""
    try:
        _, info = host._resolve_app(app)
        window_id = host._ax_window_id(host._state_window(int(info["pid"]), None))
        if window_id is None:
            return "Accessibility gives the window no id to render"
        return host._window_image(window_id, max_width=_PICTURE, max_height=_PICTURE)
    except MacOSError as exc:
        return str(exc)


def _change(before: AppState | MacOSError, after: AppState | MacOSError) -> str:
    if isinstance(after, MacOSError):
        return f"No window to read now: {after}"
    now = list(after.lines.values())
    if isinstance(before, MacOSError):
        return "\n".join([f"No window to read before steps[0]: {before}. Now:", *now])
    if before.window_id != after.window_id:
        return "\n".join([f"Another window is in front now (window_id {after.window_id}):", *now])
    old, new = list(before.lines.items()), list(after.lines.items())
    cut = ""
    if not (before.complete and after.complete):
        cut = "; the window holds more than a look reads, so only what both read"
        old = [(element_id, line) for element_id, line in old if element_id in after.lines]
        new = [(element_id, line) for element_id, line in new if element_id in before.lines]
    moved = changes(old, new, ())
    if moved is None:
        return "\n".join([f"Controls moved since before steps[0]{cut}; the window now:", *now])
    if not moved:
        return f"No change from before steps[0]{cut}."
    return "\n".join([f"Changed from before steps[0] (~ changed, + added){cut}:", *moved])


def _strip(
    index: int, before: WindowCapture | str, after: WindowCapture | str, change: str, state: AppState | MacOSError,
) -> tuple[Path, ...] | str:
    from .capture import write_png

    files: list[Path] = []
    notes: list[str] = []
    try:
        folder = Path(tempfile.mkdtemp(prefix="macos-harness-steps-"))
        for name, picture in (("before", before), ("after", after)):
            if isinstance(picture, str):
                notes.append(f"No picture {name} steps[{index}]: {picture}")
            else:
                path = folder / f"steps-{index}-{name}.png"
                write_png(picture.image, path)
                files.append(path)
        window = [f"\nThe window after steps[{index}]:", *state.lines.values()] if isinstance(state, AppState) else []
        text = folder / f"steps-{index}-ax.txt"
        text.write_text("\n".join([*notes, change, *window]) + "\n")
    except (OSError, MacOSError) as exc:
        return f"No files on steps[{index}]: {exc}"
    return (*files, text)
