"""`mac.launch`, `mac.quit`, and `mac.relaunch` against a scripted OS: an
installed bundle, a process that draws its first window after a few looks,
and a quit that takes a few looks to finish. The clock advances only when
the code under test pauses, so every wait is counted, never slept."""

from __future__ import annotations

import os
import subprocess

import pytest

from macos_harness.errors import ErrorCode
from macos_harness.macos import ApplicationNotFoundError, MacOS, MacOSError

_CALCULATOR = "/System/Applications/Calculator.app"
_TEXTEDIT = "/System/Applications/TextEdit.app"
_DEVICE_HUB = "/Applications/Xcode.app/Contents/Applications/Device Hub.app"
_MESSAGES = "/System/Applications/Messages.app"
_WINDOW = {"window_id": 7, "title": "Untitled", "on_screen": True}


class _Process:
    """The NSRunningApplication surface `launch` and `quit` touch. Each
    quit request is recorded; the process leaves the table
    ``exits_after`` looks after a polite or forced request."""

    def __init__(
        self, pid: int, *, name: str, path: str, exits_after: int | None, policy: int = 0
    ) -> None:
        self.pid, self.name, self.path = pid, name, path
        self.exits_after = exits_after
        self.policy = policy
        self.requests: list[str] = []

    def localizedName(self) -> str:
        return self.name

    def bundleIdentifier(self) -> str:
        return f"com.example.{self.name}"

    def bundleURL(self) -> _Url:
        return _Url(self.path)

    def processIdentifier(self) -> int:
        return self.pid

    def isTerminated(self) -> bool:
        return False

    def activationPolicy(self) -> int:
        return self.policy

    def launchDate(self) -> None:
        return None

    def terminate(self) -> bool:
        self.requests.append("terminate")
        return True

    def forceTerminate(self) -> bool:
        self.requests.append("forceTerminate")
        return True


class _Launching(_Process):
    """A process macOS lists before it has one: its first ``unborn``
    reads of the pid say -1."""

    def __init__(self, pid: int, *, name: str, path: str, unborn: int) -> None:
        super().__init__(pid, name=name, path=path, exits_after=None)
        self.unborn = unborn

    def processIdentifier(self) -> int:
        if self.unborn:
            self.unborn -= 1
            return -1
        return self.pid


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

    def _running_copy(self, url: _Url) -> _Process | None:
        if self.running is not None and self.running.path == str(url.path()):
            return self.running
        return None

    def _open_application(self, url: _Url, *, timeout: float) -> _Process:
        self.opened.append(str(url.path()))
        self.running = _Process(
            4242, name="TextEdit", path=str(url.path()), exits_after=self.exits_after
        )
        self.looks = 0
        return self.running

    def _resolve_app(self, query: str | int | None) -> tuple[_Process, dict[str, object]]:
        process = self.resolved or self.running
        if process is None:
            raise ApplicationNotFoundError(f"No running application matches {query!r}")
        return process, self._app_info(process)

    def windows(
        self, app: str | int | None = None, *, all_layers: bool = False
    ) -> list[dict[str, object]]:
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
    def _frontmost_app(cls) -> dict[str, object]:
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


def test_launch_of_a_running_menu_bar_app_without_a_window_points_at_its_status_item() -> None:
    """An accessory app opens from its menu bar icon, which reopen never
    reaches; the error names the call that does."""
    running = _Process(11, name="Sona", path=_TEXTEDIT, exits_after=0, policy=1)
    mac = _ScriptedOS(running=running, window_on=None)

    with pytest.raises(MacOSError, match=r"menu bar app .*mac\.do\.status_item\(app=11\)") as caught:
        mac.launch("TextEdit")

    assert caught.value.code == ErrorCode.ELEMENT_UNKNOWN


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


class _AppKitMisses(MacOS):
    """AppKit's direct pid lookup knows no app while the workspace list
    holds ``listed``, as 2 of 5 live Calculator launches showed. Exits
    are read from the real kernel process table."""

    def __init__(self, listed: _Process) -> None:
        super().__init__()
        self.listed = listed
        self.clock = 0.0

    def _now(self) -> float:
        return self.clock

    def _pause(self, seconds: float) -> None:
        self.clock += seconds

    @staticmethod
    def _application_with_pid(pid: int) -> None:
        return None

    def _running_applications(self) -> list[_Process]:
        return [self.listed]


def test_quit_by_pid_reaches_an_app_only_the_workspace_list_holds() -> None:
    finished = subprocess.Popen(["/usr/bin/true"])
    finished.wait()
    listed = _Process(finished.pid, name="Calculator", path=_CALCULATOR, exits_after=0)

    result = _AppKitMisses(listed).quit(finished.pid)

    assert (listed.requests, result["app"]["pid"]) == (["terminate"], finished.pid)


def test_quit_keeps_waiting_while_the_kernel_still_runs_the_process() -> None:
    """This test's own process is alive and no app to AppKit: quit must
    not read the missing app as an exit."""
    alive = _Process(os.getpid(), name="Calculator", path=_CALCULATOR, exits_after=None)

    with pytest.raises(MacOSError, match="did not quit within 0.2s") as caught:
        _AppKitMisses(alive).quit(os.getpid(), timeout=0.2)

    assert caught.value.code == ErrorCode.TIMEOUT
    assert alive.requests == ["terminate"]


def test_launch_waits_while_macos_lists_the_app_without_a_process() -> None:
    """Device Hub showed up with pid -1, and every call after it failed on
    "No running application matches -1"; launch waits for the real pid."""
    mac = _ScriptedOS(running=_Launching(11, name="TextEdit", path=_TEXTEDIT, unborn=2))

    result = mac.launch("TextEdit")

    assert (result["app"]["pid"], mac.pauses) == (11, [0.05, 0.05])


def test_launch_gives_up_on_an_app_that_never_gets_a_process() -> None:
    mac = _ScriptedOS(running=_Launching(11, name="TextEdit", path=_TEXTEDIT, unborn=10**6))

    with pytest.raises(MacOSError, match="TextEdit was still launching after 0.2s") as caught:
        mac.launch("TextEdit", timeout=0.2)

    assert caught.value.code == ErrorCode.TIMEOUT


class _Workspace(MacOS):
    """`MacOS` over the apps AppKit lists, ``direct`` answers for AppKit's
    own pid lookup, and one on-screen window for each pid in ``windowed``."""

    def __init__(
        self,
        *processes: _Process,
        direct: dict[int, _Process] | None = None,
        windowed: tuple[int, ...] = (),
    ) -> None:
        super().__init__()
        self.processes = processes
        self.direct = direct or {}
        self.windowed = windowed

    def _application_with_pid(self, pid: int) -> _Process | None:
        listed = (process for process in self.processes if process.pid == pid)
        return self.direct.get(pid) or next(listed, None)

    def _running_applications(self) -> tuple[_Process, ...]:
        return self.processes

    def _server_windows(self, *, on_screen_only: bool) -> list[dict[str, object]]:
        return [
            {
                "kCGWindowNumber": pid,
                "kCGWindowOwnerPID": pid,
                "kCGWindowName": "",
                "kCGWindowLayer": 0,
                "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 800, "Height": 600},
                "kCGWindowIsOnscreen": True,
            }
            for pid in self.windowed
        ]

    def _frontmost_app(self) -> dict[str, object]:
        return {"name": "Ghostty", "bundle_id": "com.mitchellh.ghostty", "pid": 1, "path": None}


def test_a_pid_query_keeps_its_pid_when_appkit_reports_none() -> None:
    """AppKit answered Device Hub's real pid with an app whose pid read
    -1, so the windows of pid -1 were looked for, and none found."""
    hub = _Process(-1, name="Device Hub", path=_DEVICE_HUB, exits_after=None)

    windows = _Workspace(hub, direct={42107: hub}, windowed=(42107,)).windows(42107)

    assert [window["pid"] for window in windows] == [42107]


def test_a_name_without_a_process_yet_says_it_is_still_launching() -> None:
    hub = _Process(-1, name="Device Hub", path=_DEVICE_HUB, exits_after=None)

    with pytest.raises(ApplicationNotFoundError, match=r"Device Hub is still launching"):
        _Workspace(hub).windows("Device Hub")


@pytest.mark.parametrize("windowed_first", [True, False], ids=["listed-first", "listed-last"])
def test_exact_name_twins_resolve_to_the_only_one_with_a_window(windowed_first) -> None:
    """Two Messages processes, one with a window, raised APP_AMBIGUOUS."""
    shown = _Process(300, name="Messages", path=_MESSAGES, exits_after=None)
    hidden = _Process(301, name="Messages", path=_MESSAGES, exits_after=None)
    processes = (shown, hidden) if windowed_first else (hidden, shown)

    windows = _Workspace(*processes, windowed=(300,)).windows("Messages")

    assert [window["pid"] for window in windows] == [300]


@pytest.mark.parametrize(
    ("query", "names", "windowed"),
    [
        ("Safari", ("Safari", "Safari"), (400, 401)),
        ("Passwords", ("PasswordsMenuBarExtra", "PasswordsAgent"), (400,)),
    ],
    ids=["both-twins-windowed", "partial-names-one-windowed"],
)
def test_a_name_stays_ambiguous_without_one_windowed_exact_match(query, names, windowed) -> None:
    """Guard: the caller still picks between two windowed Safaris, and a
    name that matches only helpers never picks one of them."""
    processes = [
        _Process(400 + index, name=name, path=f"/Applications/{name}.app", exits_after=None)
        for index, name in enumerate(names)
    ]

    with pytest.raises(MacOSError) as caught:
        _Workspace(*processes, windowed=windowed).windows(query)

    assert caught.value.code == ErrorCode.APP_AMBIGUOUS
