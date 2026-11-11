"""What a watched route checks between the person's steps, and what the
watch keeps of the keys it sees."""

from __future__ import annotations

import json
import time

import ApplicationServices as AS
import pytest

from macos_harness.macos import _KEYCODES, MacOS
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


_PID = 41
_USER = object()
_MAIL = object()
_PASSWORD = object()
_NEXT = object()
_ROLES = {_USER: ("AXTextField", None), _MAIL: ("AXTextField", None),
          _PASSWORD: ("AXTextField", "AXSecureTextField"), _NEXT: ("AXButton", None)}
_PLACES = {_USER: _Locator("AXTextField", "identifier", "username"),
           _MAIL: _EMAIL,
           _PASSWORD: _Locator("AXTextField", "identifier", "password"),
           _NEXT: _Locator("AXButton", "title", "Next")}


class _App:
    """The app as the watch's handling of keys and clicks reads it: which
    field has focus or lies under a click, what kind of field it is and what
    it holds, how a route finds it again, and whether it has secure keyboard
    entry on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.focused: object = _USER
        self.values: dict[object, str] = {}
        self.secure_entry = False
        self.pointed: object = None
        monkeypatch.setattr(AS, "AXUIElementCopyAttributeValue", lambda element, name, out: (0, self.focused))
        monkeypatch.setattr(MacOS, "_copy_attribute", staticmethod(
            lambda element, name, **kwargs: self._read(element, name)))
        monkeypatch.setattr("macos_harness.watch._secure_input_holder",
                            lambda: _PID if self.secure_entry else None)
        self.watcher = _Watcher(_PID, _Deadline(1.0, lambda: 0.0))
        monkeypatch.setattr(self.watcher, "_locate", _PLACES.get)
        monkeypatch.setattr(self.watcher, "_hit", lambda x, y: self.pointed)
        monkeypatch.setattr(self.watcher, "_control", lambda element: ("focus", element))

    def _read(self, element: object, name: str) -> object:
        if name == "AXFocusedUIElement":
            return self.focused
        if name == "AXValue":
            return self.values.get(element)
        return dict(zip(("AXRole", "AXSubrole"), _ROLES[element], strict=True)).get(name)

    def key(self, name: str, text: str, *, late: bool = False, at: int | None = None, flags: int = 0,
            repeat: bool = False) -> None:
        """A key the person typed: ``late`` when it came before the watch's
        last read of the app, or ``at`` a time of its own."""
        event = AS.CGEventCreateKeyboardEvent(None, _KEYCODES[name], True)
        AS.CGEventSetFlags(event, flags)
        AS.CGEventKeyboardSetUnicodeString(event, len(text), text)
        AS.CGEventSetIntegerValueField(event, AS.kCGEventTargetUnixProcessID, _PID)
        AS.CGEventSetIntegerValueField(event, AS.kCGKeyboardEventAutorepeat, int(repeat))
        AS.CGEventSetTimestamp(event, 1 if late else time.monotonic_ns() if at is None else at)
        self.watcher._event(AS.kCGEventKeyDown, event)

    def click(self, field: object) -> None:
        """A click the app reports landing on ``field``."""
        self.pointed = field
        event = AS.CGEventCreateMouseEvent(None, AS.kCGEventLeftMouseDown, (0, 0), AS.kCGMouseButtonLeft)
        AS.CGEventSetTimestamp(event, time.monotonic_ns())
        self.watcher._event(AS.kCGEventLeftMouseDown, event)

    def hide_keys(self) -> None:
        """macOS hides the person's keys for a while, as the watch's sampler
        sees it."""
        self.secure_entry = True
        self.watcher._note_hidden()
        self.secure_entry = False
        self.watcher._note_hidden()

    def kept(self) -> list[tuple[str, str]]:
        return [(seen.verb, seen.text) for seen in self.watcher.seen]


def _username_then_tab(app: _App) -> None:
    for letter in "alice":
        app.key(letter, letter)
    app.key("tab", "\t")


def test_a_password_typed_before_the_watch_read_the_app_after_tab_is_kept_as_the_secret(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)
    app.focused = _PASSWORD

    for letter in "zqvw":
        app.key(letter, letter, late=True)

    assert app.watcher.error is None
    assert app.kept() == [("type", "alice"), ("key", "tab"), ("secret", "")]


def test_a_late_key_whose_read_still_shows_the_field_before_tab_stops_the_watch(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)

    app.key("z", "z", late=True)

    assert app.watcher.error is not None
    assert app.kept() == [("type", "alice"), ("key", "tab")]


def test_a_late_key_mid_word_that_finds_a_secure_field_stops_the_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    for letter in "alic":
        app.key(letter, letter)
    app.focused = _PASSWORD

    app.key("e", "e", late=True)

    assert app.watcher.error is not None
    assert app.kept() == [("type", "alic")]


def test_a_late_key_after_text_typed_and_deleted_past_tab_types_where_tab_went(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    for letter in "alice":
        app.key(letter, letter)
    app.focused = _MAIL
    app.key("tab", "\t")
    app.key("x", "x")
    app.key("backspace", "\x7f")

    app.key("y", "y", late=True)

    assert app.watcher.error is None
    assert app.kept() == [("type", "alice"), ("key", "tab"), ("type", "y")]


def test_a_late_key_after_keys_that_only_edit_the_field_types_into_it(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    for letter in "old":
        app.key(letter, letter)
    app.key("a", "a", flags=AS.kCGEventFlagMaskCommand)
    app.key("backspace", "\x7f")

    app.key("n", "n", late=True)

    assert app.watcher.error is None
    assert app.kept() == [("type", "old"), ("key", "cmd+a"), ("key", "backspace"), ("type", "n")]


@pytest.mark.parametrize("shown_by", ["the next key's read", "keys hidden, then a key in another field"])
@pytest.mark.parametrize("read_as", [_USER, _NEXT], ids=["the field", "a button"])
def test_a_key_kept_after_tab_stops_the_watch_once_the_password_field_shows_in_focus(
        monkeypatch: pytest.MonkeyPatch, shown_by: str, read_as: object) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)
    app.focused = read_as
    app.key("z", "z")
    app.focused = _PASSWORD

    if shown_by == "the next key's read":
        app.key("q", "q")
    else:
        app.hide_keys()
        app.focused = _MAIL
        app.key("b", "b")

    assert app.watcher.error is not None


_NEXT_STEPS = ["tab finds the password field", "tab reads as the same field", "a key reads as another field",
               "a click on another field"]


def _key_after_tab_or_return_then(app: _App, key: str, next_step: str, *, shown: bool) -> None:
    app.values[_USER] = "alice"
    for letter in "alice":
        app.key(letter, letter)
    app.key(key, {"tab": "\t", "return": "\r"}[key])
    app.key("z", "z")
    if shown:
        app.values[_USER] = "alicez"
    if next_step == "tab finds the password field":
        app.focused = _PASSWORD
        app.key("tab", "\t")
    elif next_step == "tab reads as the same field":
        app.key("tab", "\t")
    elif next_step == "a key reads as another field":
        app.focused = _MAIL
        app.key("b", "b")
    else:
        app.click(_MAIL)


@pytest.mark.parametrize("next_step", _NEXT_STEPS)
@pytest.mark.parametrize("key", ["tab", "return"])
def test_a_key_read_as_the_field_before_tab_or_return_stops_the_watch_by_the_next_step_when_that_field_does_not_show_it(
        monkeypatch: pytest.MonkeyPatch, key: str, next_step: str) -> None:
    app = _App(monkeypatch)

    _key_after_tab_or_return_then(app, key, next_step, shown=False)

    assert app.watcher.error is not None


@pytest.mark.parametrize("next_step", _NEXT_STEPS)
def test_a_key_read_as_the_field_before_return_is_kept_once_that_field_shows_it_by_the_next_step(
        monkeypatch: pytest.MonkeyPatch, next_step: str) -> None:
    app = _App(monkeypatch)

    _key_after_tab_or_return_then(app, "return", next_step, shown=True)

    assert app.watcher.error is None
    assert app.kept()[:3] == [("type", "alice"), ("key", "return"), ("type", "z")]


@pytest.mark.parametrize("hidden", ["no keys hidden", "keys hidden after it"])
@pytest.mark.parametrize("before", ["tab", "nothing"])
def test_a_key_that_reads_as_a_plain_field_once_the_password_field_took_focus_stops_the_watch(
        monkeypatch: pytest.MonkeyPatch, before: str, hidden: str) -> None:
    app = _App(monkeypatch)
    if before == "tab":
        _username_then_tab(app)
    app.watcher._notice(_PASSWORD, "AXFocusedUIElementChanged")
    typed_at = time.monotonic_ns()
    if hidden == "keys hidden after it":
        app.hide_keys()

    app.key("z", "z", at=typed_at)

    assert app.watcher.error is not None


@pytest.mark.parametrize("tab_read", ["the username field", "the password field"])
def test_a_login_typed_at_a_normal_pace_keeps_the_password_as_the_secret(
        monkeypatch: pytest.MonkeyPatch, tab_read: str) -> None:
    app = _App(monkeypatch)
    for letter in "alice":
        app.key(letter, letter)
    if tab_read == "the password field":
        app.focused = _PASSWORD
    app.key("tab", "\t")
    app.focused = _PASSWORD
    app.watcher._notice(_PASSWORD, "AXFocusedUIElementChanged")
    app.hide_keys()
    app.focused = _MAIL

    for letter in "bo":
        app.key(letter, letter)

    assert app.watcher.error is None
    assert app.kept()[-2:] == [("secret", ""), ("type", "bo")]


def test_a_password_after_a_field_filled_past_tab_is_kept_as_the_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    _username_then_tab(app)
    app.focused = _MAIL
    for letter in "ail":
        app.key(letter, letter)
    app.key("tab", "\t")
    app.hide_keys()
    app.focused = _USER

    app.key("b", "b")

    assert app.watcher.error is None


def test_a_password_after_keys_typed_past_escape_in_the_same_field_is_kept_as_the_secret(
        monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App(monkeypatch)
    for letter in "ali":
        app.key(letter, letter)
    app.key("escape", "\x1b")
    for letter in "ce":
        app.key(letter, letter)
    app.key("tab", "\t")
    app.hide_keys()
    app.focused = _MAIL

    app.key("b", "b")

    assert app.watcher.error is None


@pytest.mark.parametrize("kind", ["option set, plain text", "held until it repeats"])
def test_a_key_whose_text_is_not_what_the_app_shows_stops_the_watch(
        monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    app = _App(monkeypatch)
    for letter in "caf":
        app.key(letter, letter)

    if kind == "option set, plain text":
        app.key("e", "e", flags=AS.kCGEventFlagMaskAlternate)
    else:
        app.key("e", "e", repeat=True)

    assert app.watcher.error is not None
    assert app.kept() == [("type", "caf")]
