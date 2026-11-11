"""What a watched route checks between the person's steps."""

from __future__ import annotations

import json

from macos_harness.ops import _Deadline
from macos_harness.receipts import Equals, Present
from macos_harness.routes import _load_step, _Locator, _step_json
from macos_harness.watch import _Seen, _Watcher

_APP = "com.example.demo"
_NAME = _Locator("AXTextField", "identifier", "name")
_EMAIL = _Locator("AXTextField", "identifier", "email")
_SAVE = _Locator("AXButton", "title", "Save")


def _focused(field: _Locator) -> Equals:
    return Equals(**field.query(), attribute="AXFocused", value=True)


def test_typed_text_and_the_step_before_it_check_the_field_has_focus_in_the_route_file() -> None:
    watcher = _Watcher(41, _Deadline(1.0, lambda: 0.0))
    watcher.seen = [
        _Seen("focus", _NAME, "", _NAME, after=_NAME),
        _Seen("type", _NAME, "Ada", _NAME, after=_EMAIL),
        _Seen("key", None, "tab", _EMAIL, after=_EMAIL),
        _Seen("type", _EMAIL, "ada@x", _EMAIL, after=_SAVE),
        _Seen("press", _SAVE, "", _SAVE),
    ]
    goal = Present(role="AXStaticText", identifier="saved")

    saved = [_load_step(json.loads(json.dumps(_step_json(step))), _APP) for step in watcher.steps(goal)]

    assert [step.postcondition for step in saved] == [
        _focused(_NAME), _focused(_NAME), _focused(_EMAIL), _focused(_EMAIL), goal]
