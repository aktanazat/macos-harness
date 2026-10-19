"""`mac.launch`, `mac.quit`, and `mac.relaunch` against a scripted OS: an
installed bundle, a process that draws its first window after a few looks,
and a quit that takes a few looks to finish. The clock advances only when
the code under test pauses, so every wait is counted, never slept."""

from __future__ import annotations

from typing import Any

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import ApplicationNotFoundError, MacOS, MacOSError

_TEXTEDIT = "/System/Applications/TextEdit.app"
_WINDOW = {"window_id": 7, "title": "Untitled", "on_screen": True}


class _Process:
    """The NSRunningApplication surface `launch` and `quit` touch. Each
    quit request is recorded; the process leaves the table
    ``exits_after`` looks after a polite or forced request."""

    def __init__(self, pid: int, *, name: str, path: str, exits_after: int | None) -> None:
        self.pid, self.name, self.path = pid, name, path
        self.exits_after = exits_after
        self.requests: list[str] = []

    def localizedName(self) -> str:
        return self.name

    def bundleIdentifier(self) -> str:
        return f"com.example.{self.name}"

    def bundleURL(self) -> Any:
        return _Url(self.path)

    def processIdentifier(self) -> int:
        return self.pid

    def isTerminated(self) -> bool:
        return False

    def launchDate(self) -> None:
        return None

    def terminate(self) -> bool:
        self.requests.append("terminate")
        return True

    def forceTerminate(self) -> bool:
        self.requests.append("forceTerminate")
        return True


class _Url:
    def __init__(self, path: str) -> None:
        self._path = path

    def path(self) -> str:
        return self._path


class _ScriptedOS(MacOS):
    """`MacOS` over a scripted process table and window server.

    ``running`` is the process already up, if any. A launched process
    shows its first window on look number ``window_on``.
    """

    def __init__(
        self,
        *,
        running: _Process | None = None,
        window_on: int | None = 1,
        exits_after: int | None = 0,
        resolved: _Process | None = None,
    ) -> None:
        super().__init__()
        self.clock = 0.0
        self.pauses: list[float] = []
        self.opened: list[str] = []
        self.running = running
        self.resolved = resolved
        self.window_on = window_on
        self.exits_after = exits_after
        self.looks = 0
        self.exit_looks = 0

    def _now(self) -> float:
        return self.clock

    def _pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.clock += seconds

    def _running_copy(self, url: Any) -> _Process | None:
        if self.running is not None and self.running.path == str(url.path()):
            return self.running
        return None

    def _open_application(self, url: Any, *, timeout: float) -> _Process:
        self.opened.append(str(url.path()))
        self.running = _Process(
            4242, name="TextEdit", path=str(url.path()), exits_after=self.exits_after
        )
        self.looks = 0
        return self.running

    def _resolve_app(self, query: str | int | None) -> tuple[_Process, dict[str, Any]]:
        process = self.resolved or self.running
        if process is None:
            raise ApplicationNotFoundError(f"No running application matches {query!r}")
        return process, self._app_info(process)

    def windows(self, app: str | int | None = None) -> list[dict[str, Any]]:
        self.looks += 1
        if self.window_on is not None and self.looks >= self.window_on:
            return [_WINDOW]
        return []

    def _exited(self, pid: int) -> bool:
        process = self.resolved or self.running
        if process is None or not process.requests:
            return False
        self.exit_looks += 1
        if process.exits_after is None or self.exit_looks <= process.exits_after:
            return False
        self.running = None
        return True

    @classmethod
    def _frontmost_app(cls) -> dict[str, Any]:
        return {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty", "pid": 1, "path": None}


@pytest.mark.parametrize(
    "query", ["TextEdit", "com.apple.TextEdit", _TEXTEDIT], ids=["name", "bundle-id", "path"]
)
def test_launch_finds_the_installed_bundle_by_name_bundle_id_or_path(query: str) -> None:
    mac = _ScriptedOS()

    result = mac.launch(query)

    assert mac.opened == [_TEXTEDIT]
    assert result["launched"] is True
    assert result["app"]["pid"] == 4242


def test_launch_returns_on_the_look_that_finds_the_first_window() -> None:
    """The window appears on the third look: two short pauses, never a
    fixed sleep sized for the slowest app."""
    mac = _ScriptedOS(window_on=3)

    result = mac.launch("TextEdit")

    assert result["windows"] == [_WINDOW]
    assert mac.pauses == [0.05, 0.05]
    assert result["frontmost"]["name"] == "Ghostty"


def test_launch_uses_a_running_copy_instead_of_starting_another() -> None:
    running = _Process(11, name="TextEdit", path=_TEXTEDIT, exits_after=0)
    mac = _ScriptedOS(running=running)

    result = mac.launch("TextEdit")

    assert mac.opened == []
    assert (result["launched"], result["app"]["pid"], result["windows"]) == (False, 11, [_WINDOW])


def test_launch_names_the_missing_app() -> None:
    mac = _ScriptedOS()

    with pytest.raises(ApplicationNotFoundError, match="No installed application matches 'NoSuchAppXyz'"):
        mac.launch("NoSuchAppXyz")

    assert mac.opened == []


def test_launch_times_out_at_its_deadline_when_no_window_appears() -> None:
    mac = _ScriptedOS(window_on=None)

    with pytest.raises(MacOSError, match="showed no window within 1s") as caught:
        mac.launch("TextEdit", timeout=1.0)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert sum(mac.pauses) == pytest.approx(1.0)


def test_launch_without_window_returns_once_the_process_runs() -> None:
    mac = _ScriptedOS(window_on=None)

    result = mac.launch("TextEdit", window=False)

    assert (result["windows"], mac.looks, mac.pauses) == ([], 0, [])


@pytest.mark.parametrize(
    ("force", "sent"), [(False, "terminate"), (True, "forceTerminate")], ids=["polite", "forced"]
)
def test_quit_returns_once_the_process_has_exited(force: bool, sent: str) -> None:
    running = _Process(11, name="TextEdit", path=_TEXTEDIT, exits_after=2)
    mac = _ScriptedOS(running=running)

    result = mac.quit("TextEdit", force=force)

    assert running.requests == [sent]
    assert (result["forced"], mac.pauses) == (force, [0.05, 0.05])


def test_quit_that_times_out_never_forces() -> None:
    """A save prompt holds the app open: the call reports it and leaves
    the unsaved work alone."""
    running = _Process(11, name="TextEdit", path=_TEXTEDIT, exits_after=None)
    mac = _ScriptedOS(running=running)

    with pytest.raises(MacOSError, match="did not quit within 1s; it may be asking to save") as caught:
        mac.quit("TextEdit", timeout=1.0)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.details["forced"] is False
    assert running.requests == ["terminate"]


def test_quit_refuses_a_partial_name_match() -> None:
    helper = _Process(12, name="NotesHelper", path="/Applications/NotesHelper.app", exits_after=0)
    mac = _ScriptedOS(resolved=helper)

    with pytest.raises(ApplicationNotFoundError, match="named exactly 'Notes'") as caught:
        mac.quit("Notes")

    assert caught.value.code == ErrorCode.APP_NOT_FOUND
    assert helper.requests == []


def test_relaunch_starts_the_same_bundle_the_running_copy_came_from(tmp_path) -> None:
    build = (tmp_path / "DerivedData" / "Debug" / "TextEdit.app").resolve()
    build.mkdir(parents=True)
    running = _Process(11, name="TextEdit", path=str(build), exits_after=0)
    mac = _ScriptedOS(running=running)

    result = mac.relaunch("TextEdit")

    assert running.requests == ["terminate"]
    assert mac.opened == [str(build)]
    assert (result["launched"], result["previous_pid"], result["app"]["pid"]) == (True, 11, 4242)
