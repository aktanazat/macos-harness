"""A shell's spare runs exactly one step for that shell, then exits.

The test process plays the agent's shell: it starts ``macos-harness spare``
as its coprocess and keeps the other ends of both pipes, as ``coproc`` does,
and every step is a real ``macos-harness`` process started from it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import macos_harness
from macos_harness.spare import ENV

_PID = "import os\nprint(os.getpid(), flush=True)\n"
_STAND_IN_SHELL = """\
import os, subprocess, sys, time
requests_end, requests = os.pipe()
replies, replies_end = os.pipe()
broker = subprocess.Popen(
    [sys.executable, "-m", "macos_harness.cli", "spare"], stdin=requests_end, stdout=replies_end
)
os.close(requests_end)
os.close(replies_end)
server = subprocess.Popen(["sleep", "600"], pass_fds=(replies, requests))
print(broker.pid, server.pid, flush=True)
time.sleep(600)
"""
_STARTS_ITS_COPROCESS = "import os, subprocess, sys\nos.setsid()\nsys.exit(subprocess.call(sys.argv[1:]))\n"
_CLIENT_LOADS = """\
import atexit, sys
atexit.register(lambda: open({path!r}, "w").write("\\n".join(sys.modules)))
from macos_harness.cli import main
raise SystemExit(main())
"""
_SCRIPT = "import sys\nfrom macos_harness.cli import main\nsys.exit(main())\n"


def _env(**extra: str) -> dict[str, str]:
    """A step's environment: no spare yet, no telemetry, and Python's own
    output buffering whatever the test runner's shell sets."""
    inherited = {
        name: value
        for name, value in os.environ.items()
        if name not in (ENV, "PYTHONUNBUFFERED")
    }
    return {**inherited, "DO_NOT_TRACK": "1", **extra}


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "macos_harness.cli", *args]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _gone(pid: int) -> bool:
    """Whether ``pid`` is gone within ten seconds."""
    for _ in range(200):
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _child(pid: int) -> int:
    """The child ``pid`` started, once there is one."""
    for _ in range(200):
        found = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
        ).stdout.split()
        if found:
            return int(found[0])
        time.sleep(0.05)
    raise AssertionError(f"process {pid} started no spare")


def _appears(path: Path) -> bool:
    """Whether ``path`` exists within a second."""
    for _ in range(20):
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _cut_short(step: subprocess.Popen[bytes]) -> tuple[int, str]:
    """How a step that printed its first line ends once nothing reads its output."""
    assert step.stdout is not None and step.stderr is not None
    step.stdout.close()
    err = step.stderr.read().decode()
    return step.wait(timeout=60), err


class _Shell:
    def __init__(self, env: dict[str, str], broker: list[str] | None = None) -> None:
        requests_end, self.requests = os.pipe()
        self.replies, replies_end = os.pipe()
        self.broker = subprocess.Popen(
            broker or _cli("spare"), stdin=requests_end, stdout=replies_end, env=env
        )
        os.close(requests_end)
        os.close(replies_end)
        self.env = {**env, ENV: f"{self.replies},{self.requests}"}
        self._steps: list[subprocess.Popen[bytes]] = []

    def start(
        self,
        command: list[str],
        *,
        stdin: int = subprocess.PIPE,
        stderr: int = subprocess.PIPE,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        step = subprocess.Popen(
            command,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=cwd,
            env={**self.env, **(env or {})},
            pass_fds=(self.replies, self.requests),
        )
        self._steps.append(step)
        return step

    def step(
        self,
        program: str | None,
        command: list[str] | None = None,
        *,
        stderr: int = subprocess.PIPE,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.Popen[bytes], str, str]:
        """Run one step to its end; a None program leaves stdin empty."""
        step = self.start(
            command or _cli(),
            stdin=subprocess.DEVNULL if program is None else subprocess.PIPE,
            stderr=stderr,
            cwd=cwd,
            env=env,
        )
        out, err = step.communicate(None if program is None else program.encode(), timeout=60)
        return step, out.decode(), (err or b"").decode()

    def served(
        self,
        program: str | None,
        command: list[str] | None = None,
        *,
        stderr: int = subprocess.PIPE,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.Popen[bytes], str, str]:
        """Run a step that prints its pid first until a spare runs it."""
        for _ in range(100):
            step, out, err = self.step(program, command, stderr=stderr, cwd=cwd, env=env)
            if int(out.split()[0]) != step.pid:
                return step, out, err
            time.sleep(0.05)
        raise AssertionError("no spare ran a step")

    def held(
        self, program: str, command: list[str] | None = None
    ) -> tuple[subprocess.Popen[bytes], int]:
        """Start a step that prints its pid and keeps going until a spare runs it."""
        for _ in range(100):
            step = self.start(command or _cli())
            assert step.stdin is not None and step.stdout is not None
            step.stdin.write(program.encode())
            step.stdin.close()
            pid = int(step.stdout.readline())
            if pid != step.pid:
                return step, pid
            step.kill()
            step.wait()
            time.sleep(0.05)
        raise AssertionError("no spare ran a step")

    def close(self) -> None:
        for step in self._steps:
            if step.poll() is None:
                step.kill()
            step.wait()
            for pipe in (step.stdin, step.stdout, step.stderr):
                if pipe is not None:
                    pipe.close()
        os.close(self.requests)
        os.close(self.replies)
        self.broker.wait(timeout=10)


@pytest.fixture
def shell() -> Iterator[_Shell]:
    started = _Shell(_env())
    yield started
    started.close()


def test_each_step_gets_a_fresh_spare_that_exits_after_it(shell: _Shell) -> None:
    first, out, _err = shell.served(
        "import builtins, os\nprint(os.getpid())\nbuiltins.left_behind = True\n"
    )
    first_spare = int(out)
    second, out, _err = shell.served(
        "import builtins, os\nprint(os.getpid(), hasattr(builtins, 'left_behind'))\n"
    )
    second_spare, left_behind = out.split()

    assert first_spare != first.pid
    assert int(second_spare) not in (second.pid, first_spare)
    assert left_behind == "False"
    assert _gone(first_spare)


def test_a_served_step_keeps_its_output_status_directory_and_environment(
    shell: _Shell, tmp_path: Path
) -> None:
    program = (
        "import os, sys\n"
        "print(os.getpid())\n"
        "print(os.getcwd())\n"
        f"print(os.environ['STEP_NOTE'], os.environ.get({ENV!r}))\n"
        "print('on stderr', file=sys.stderr)\n"
        "raise SystemExit(3)\n"
    )

    step, out, err = shell.served(program, cwd=tmp_path, env={"STEP_NOTE": "kept"})

    pid, directory, note = out.splitlines()
    assert int(pid) != step.pid
    assert directory == os.path.realpath(tmp_path)
    assert note == "kept None"
    assert err == "on stderr\n"
    assert step.returncode == 3


def test_a_saved_program_is_served_with_its_arguments(shell: _Shell, tmp_path: Path) -> None:
    flow = tmp_path / "flow.py"
    flow.write_text("import os\nprint(os.getpid(), args['note'])\n")

    step, out, _err = shell.served(None, _cli("run", str(flow), "--arg", "note=kept"))

    pid, note = out.split()
    assert int(pid) != step.pid
    assert note == "kept"
    assert step.returncode == 0


def test_a_one_line_program_is_served(shell: _Shell) -> None:
    step, out, _err = shell.served(None, _cli("-c", "import os\nprint(os.getpid())"))

    assert int(out) != step.pid
    assert step.returncode == 0


def test_a_served_step_keeps_the_order_of_output_and_errors_sent_to_one_place(
    shell: _Shell,
) -> None:
    program = (
        "import os, sys\n"
        "print(os.getpid(), flush=True)\n"
        "for line in range(100):\n"
        "    print('out', line, flush=True)\n"
        "    print('err', line, file=sys.stderr, flush=True)\n"
    )

    _step, out, _err = shell.served(program, stderr=subprocess.STDOUT)

    assert out.splitlines()[1:] == [
        f"{stream} {line}" for line in range(100) for stream in ("out", "err")
    ]


@pytest.mark.parametrize("started_as", ["script", "module"])
def test_a_served_step_ends_its_output_as_its_own_process_would(
    started_as: str, shell: _Shell, tmp_path: Path
) -> None:
    """Python flushes a script's buffered output before it prints how the
    script ended, as for the installed command, and a module's after."""
    command = _cli()
    if started_as == "script":
        script = tmp_path / "macos-harness"
        script.write_text(_SCRIPT)
        command = [sys.executable, str(script)]
    program = (
        "import os\nprint(os.getpid(), flush=True)\nprint('printed')\nraise SystemExit('ended')\n"
    )
    alone = subprocess.run(
        command,
        input=program.encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_env(),
        check=False,
    )

    step, out, _err = shell.served(program, command, stderr=subprocess.STDOUT)

    assert out.splitlines()[1:] == alone.stdout.decode().splitlines()[1:]
    assert step.returncode == alone.returncode == 1


@pytest.mark.parametrize(
    "sends", ['"$@" 2>&1 | cat', '"$@" > output 2>&1; cat output'], ids=["pipe", "file"]
)
def test_a_served_step_lets_out_its_output_when_its_own_process_would(
    sends: str, shell: _Shell, tmp_path: Path
) -> None:
    """Python sizes a process's output buffers by where its output goes when
    it starts, and a spare starts with its output going nowhere."""
    command = ["/bin/sh", "-c", sends, "sh", *_cli()]
    program = (
        "import os, sys\n"
        "print(os.getppid(), flush=True)\n"
        "print('a' * 10000)\n"
        "print(1, file=sys.stderr)\n"
        "print('b' * 20000)\n"
        "print(2, file=sys.stderr)\n"
    )
    alone = subprocess.run(
        command,
        input=program.encode(),
        stdout=subprocess.PIPE,
        cwd=tmp_path,
        env=_env(),
        check=True,
    )

    _step, out, _err = shell.served(program, command, cwd=tmp_path)

    assert out.splitlines()[1:] == alone.stdout.decode().splitlines()[1:]


def test_a_served_step_has_the_output_streams_its_own_process_would(shell: _Shell) -> None:
    program = (
        "import os, sys\n"
        "print(os.getpid())\n"
        "for stream, first in ((sys.stdout, sys.__stdout__), (sys.stderr, sys.__stderr__)):\n"
        "    print(stream is first, stream.name, stream.mode, stream.encoding, stream.errors,\n"
        "          stream.line_buffering, stream.write_through)\n"
    )
    alone = subprocess.run(
        _cli(), input=program.encode(), capture_output=True, env=_env(), check=True
    )

    _step, out, _err = shell.served(program)

    assert out.splitlines()[1:] == alone.stdout.decode().splitlines()[1:]


def test_a_process_a_served_step_leaves_running_lives_on_when_it_writes(
    shell: _Shell, tmp_path: Path
) -> None:
    """As it would after a fresh step, and without its output reaching the
    shell's next step."""
    starts_writer = (
        "import os, pathlib, subprocess\n"
        "print(os.getpid(), flush=True)\n"
        f"if os.getppid() != {os.getpid()}:\n"
        "    writer = subprocess.Popen(\n"
        "        ['/bin/sh', '-c', 'while [ ! -e stop ]; do echo late; touch wrote; sleep 0.05; done']\n"
        "    )\n"
        "    pathlib.Path('writer').write_text(str(writer.pid))\n"
    )
    waits_for_a_write = (
        "import os, time\n"
        "print(os.getpid(), flush=True)\n"
        "started = time.time()\n"
        "for _ in range(200):\n"
        "    if os.path.exists('wrote') and os.stat('wrote').st_mtime > started:\n"
        "        break\n"
        "    time.sleep(0.01)\n"
    )
    shell.served(starts_writer, cwd=tmp_path)
    try:
        writer = int((tmp_path / "writer").read_text())
        after, out, err = shell.served(waits_for_a_write, cwd=tmp_path)
        writer_lived = _alive(writer)
    finally:
        (tmp_path / "stop").touch()

    assert writer_lived
    assert out.split()[1:] == []
    assert err == ""
    assert after.returncode == 0


def test_a_step_runs_in_its_own_process_while_no_spare_is_ready() -> None:
    requests_end, requests = os.pipe()
    replies, replies_end = os.pipe()
    step = subprocess.Popen(
        _cli(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(**{ENV: f"{replies},{requests}"}),
        pass_fds=(replies, requests),
    )
    broker = subprocess.Popen(
        _cli("spare"), stdin=requests_end, stdout=replies_end, env=_env()
    )
    for fd in (requests_end, replies_end):
        os.close(fd)

    out, err = step.communicate(_PID.encode(), timeout=60)
    for fd in (requests, replies):
        os.close(fd)
    broker.wait(timeout=10)

    assert int(out) == step.pid
    assert err == b""
    assert step.returncode == 0


@pytest.mark.parametrize("elsewhere", [False, True], ids=["same-tmpdir", "other-tmpdir"])
def test_a_second_step_at_once_runs_in_its_own_process_and_takes_no_output(
    elsewhere: bool, shell: _Shell, tmp_path: Path
) -> None:
    """Whatever temporary directory the second step names. The first step's
    output fills every pipe on its way, as when nothing reads it for a while."""
    release = tmp_path / "release"
    first, spare = shell.held(
        "import os\n"
        "from pathlib import Path\n"
        "print(os.getpid(), flush=True)\n"
        "count = 0\n"
        f"while not Path({str(release)!r}).exists():\n"
        "    print(count, 'x' * 1000, flush=True)\n"
        "    count += 1\n"
        "print('done', flush=True)\n"
    )

    second, out, _err = shell.step(_PID, env={"TMPDIR": str(tmp_path)} if elsewhere else None)
    release.touch()
    assert first.stdout is not None
    lines = first.stdout.read().decode().splitlines()
    first.wait(timeout=60)

    assert spare != first.pid
    assert int(out) == second.pid
    assert lines[-1] == "done"
    assert [line.split()[0] for line in lines[:-1]] == [
        str(count) for count in range(len(lines) - 1)
    ]
    assert first.returncode == 0


def test_a_killed_step_takes_its_spare_with_it(shell: _Shell) -> None:
    step, spare = shell.held("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(600)\n")

    step.kill()
    step.wait()

    assert _gone(spare)
    after, out, _err = shell.served(_PID)
    assert int(out) != after.pid


def test_a_step_whose_spare_is_killed_dies_as_its_own_process_would(shell: _Shell) -> None:
    step, spare = shell.held("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(600)\n")

    os.kill(spare, signal.SIGKILL)
    assert step.stderr is not None
    err = step.stderr.read().decode()

    assert step.wait(timeout=60) == -signal.SIGKILL
    assert err == ""


def test_a_served_step_whose_output_stops_being_read_ends_as_its_own_process_would(
    shell: _Shell,
) -> None:
    """As when its output goes to ``head``: its next write fails, it may
    handle that, and nothing else breaks."""
    program = (
        "import os, sys\n"
        "print(os.getpid(), flush=True)\n"
        "try:\n"
        "    while True:\n"
        "        print('line', flush=True)\n"
        "except BrokenPipeError:\n"
        "    print('cut short', file=sys.stderr, flush=True)\n"
    )
    alone = subprocess.Popen(
        _cli(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    assert alone.stdin is not None and alone.stdout is not None
    alone.stdin.write(program.encode())
    alone.stdin.close()
    alone.stdout.readline()
    ended_alone = _cut_short(alone)

    step, _spare = shell.held(program)

    assert _cut_short(step) == ended_alone
    assert ended_alone[1].startswith("cut short\n")


def test_ctrl_c_stops_a_served_step_from_a_shell_without_job_control() -> None:
    """Such a shell starts its coprocess ignoring Ctrl-C and Ctrl-\\; each
    step's own process would not, and the spare running it does not."""
    shell = _Shell(_env(), ["/bin/sh", "-c", 'trap "" INT QUIT; exec "$@"', "sh", *_cli("spare")])
    try:
        step, _spare = shell.held(
            "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(5)\nprint('slept')\n"
        )
        step.send_signal(signal.SIGINT)
        assert step.stderr is not None
        err = step.stderr.read().decode()
        status = step.wait(timeout=60)
    finally:
        shell.close()

    assert status == -signal.SIGINT
    assert err.splitlines()[-1] == "KeyboardInterrupt"


def test_a_served_step_ignores_the_hangup_its_own_process_would(shell: _Shell) -> None:
    """As one started with ``nohup`` does, wherever the hangup reaches it."""
    step, spare = shell.held(
        "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(1)\nprint('finished')\n",
        ["/bin/sh", "-c", 'trap "" HUP; exec "$@"', "sh", *_cli()],
    )

    os.kill(step.pid, signal.SIGHUP)
    os.kill(spare, signal.SIGHUP)
    assert step.stdout is not None
    out = step.stdout.read().decode()

    assert step.wait(timeout=60) == 0
    assert out == "finished\n"


def test_the_broker_and_its_spare_die_with_the_shell() -> None:
    """Even while something the shell started, such as a server, still
    holds the shell's ends of the pipes."""
    stand_in = subprocess.Popen(
        [sys.executable, "-c", _STAND_IN_SHELL], stdout=subprocess.PIPE, env=_env()
    )
    assert stand_in.stdout is not None
    broker, server = map(int, stand_in.stdout.readline().split())
    try:
        spare = _child(broker)
        stand_in.kill()
        stand_in.wait()
        broker_gone, spare_gone = _gone(broker), _gone(spare)
    finally:
        os.kill(server, signal.SIGKILL)
        stand_in.stdout.close()

    assert broker_gone
    assert spare_gone


def test_a_served_step_runs_to_its_end_after_its_shell_hangs_up_and_exits() -> None:
    """As a step started with ``nohup`` or disowned would. The shell hangs
    up its coprocess's whole process group as it goes."""
    shell = _Shell(_env(), [sys.executable, "-c", _STARTS_ITS_COPROCESS, *_cli("spare")])
    try:
        broker = _child(shell.broker.pid)
        step, spare = shell.held(
            "import os, time\n"
            "print(os.getpid(), flush=True)\n"
            "while True:\n"
            "    try:\n"
            f"        os.kill({shell.broker.pid}, 0)\n"
            "    except ProcessLookupError:\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "time.sleep(0.5)\n"
            "print('finished', flush=True)\n"
        )
        os.killpg(shell.broker.pid, signal.SIGHUP)
        shell.broker.wait()
        assert step.stdout is not None
        out = step.stdout.read().decode()
        status = step.wait(timeout=60)
        broker_gone, spare_gone = _gone(broker), _gone(spare)
    finally:
        shell.close()

    assert out == "finished\n"
    assert status == 0
    assert broker_gone
    assert spare_gone


def test_a_spare_dies_with_its_broker_even_mid_step(shell: _Shell) -> None:
    """Even while the step it serves is stopped and cannot notice."""
    step, spare = shell.held("import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(600)\n")

    os.kill(step.pid, signal.SIGSTOP)
    shell.broker.kill()
    shell.broker.wait()
    spare_gone = _gone(spare)
    os.kill(step.pid, signal.SIGCONT)
    assert step.stderr is not None
    held_err = step.stderr.read().decode()
    after, out, err = shell.step(_PID)

    assert spare_gone
    assert step.wait(timeout=60) == 1
    assert held_err == (
        "macos-harness: the spare running this step went away before it finished\n"
    )
    assert int(out) == after.pid
    assert err == ""
    assert after.returncode == 0


@pytest.mark.parametrize("module", ["_version", "ops"])
def test_a_step_after_a_source_change_runs_the_changed_code(
    module: str, tmp_path: Path
) -> None:
    package = tmp_path / "macos_harness"
    shutil.copytree(
        Path(macos_harness.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    source = package / f"{module}.py"
    program = (
        "import importlib, os\n"
        f"changed = importlib.import_module('macos_harness.{module}')\n"
        "print(os.getpid(), getattr(changed, 'CHANGED', 'never'))\n"
    )
    shell = _Shell(_env(PYTHONPATH=str(tmp_path)))
    try:
        shell.served(program)
        seen = []
        for change in range(3):
            # Long enough for the next spare to load the code about to change.
            time.sleep(0.5)
            source.write_text(source.read_text() + f"\nCHANGED = 'change{change}'\n")
            _step, out, _err = shell.step(program)
            seen.append(out.split()[1])
        _step, out, _err = shell.served(program)
    finally:
        shell.close()

    assert seen == ["change0", "change1", "change2"]
    assert out.split()[1] == "change2"


@pytest.mark.parametrize("module", ["ops", "_version"])
def test_a_spare_that_loaded_during_a_change_is_replaced_unasked(
    module: str, tmp_path: Path
) -> None:
    package = tmp_path / "macos_harness"
    shutil.copytree(
        Path(macos_harness.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    ops = package / "ops.py"
    # Every spare loads ops; half a second there leaves room for the change below.
    ops.write_text(ops.read_text() + "\nimport time\ntime.sleep(0.5)\n")
    source = package / f"{module}.py"
    shell = _Shell(_env(PYTHONPATH=str(tmp_path)))
    try:
        shell.served(_PID)
        loading = _child(shell.broker.pid)
        source.write_text(source.read_text() + "\nCHANGED = True\n")
        loading_gone = _gone(loading)
        replacement = _child(shell.broker.pid)
    finally:
        shell.close()

    assert loading_gone
    assert replacement != loading


def test_a_change_just_after_a_spare_checks_its_code_reaches_the_next_step(
    tmp_path: Path,
) -> None:
    """The moment after the spare checks its code against the broker's is
    held open here; a change made in it must not be served."""
    package = tmp_path / "macos_harness"
    shutil.copytree(
        Path(macos_harness.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    opened, shut = tmp_path / "opened", tmp_path / "shut"
    spare = package / "spare.py"
    checked = "        os._exit(_STALE)\n"
    assert spare.read_text().count(checked) == 1
    spare.write_text(
        spare.read_text().replace(
            checked,
            f"{checked}    open({str(opened)!r}, 'w').close()\n"
            f"    time.sleep(1)\n    open({str(shut)!r}, 'w').close()\n",
        )
    )
    source = package / "ops.py"
    program = (
        "import os\nfrom macos_harness import ops\n"
        "print(os.getpid(), getattr(ops, 'CHANGED', 'never'))\n"
    )
    shell = _Shell(_env(PYTHONPATH=str(tmp_path)))
    try:
        assert any(_appears(opened) for _ in range(10))
        source.write_text(source.read_text() + "\nCHANGED = 'changed'\n")
        assert any(_appears(shut) for _ in range(5))
        # Long enough for the spare to tell the broker it is ready.
        time.sleep(0.5)
        _step, out, _err = shell.step(program)
    finally:
        shell.close()

    assert out.split()[1] == "changed"


def test_a_step_that_loads_another_copy_of_the_package_leaves_no_spare_restarting(
    shell: _Shell, tmp_path: Path
) -> None:
    """The spare loaded for it takes that copy, finds it is not the
    broker's, and exits; the broker then waits for the next step instead of
    loading the same copy again and again."""
    package = tmp_path / "macos_harness"
    shutil.copytree(
        Path(macos_harness.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    loads = tmp_path / "loads"
    spare = package / "spare.py"
    spare.write_text(
        spare.read_text() + f"\nif sys.argv[0] == '-c':\n    open({str(loads)!r}, 'a').write('.')\n"
    )
    shell.served(_PID)
    for _ in range(20):
        # Long enough for the next spare to be ready for the step.
        time.sleep(0.5)
        shell.step(_PID, env={"PYTHONPATH": str(tmp_path)})
        if _appears(loads):
            break
    # Room for several more loads of the copy, had the broker restarted it.
    time.sleep(2)
    loaded = loads.read_text()
    after, out, _err = shell.served(_PID)

    assert loaded == "."
    assert int(out.split()[0]) != after.pid


def test_a_step_with_other_load_time_settings_never_gets_a_spare_loaded_without_them(
    shell: _Shell,
) -> None:
    shell.served(_PID)
    zone = {"TZ": "Asia/Tokyo"}

    step, out, _err = shell.served(
        "import os, time\nprint(os.getpid(), time.tzname[0])\n", env=zone
    )

    assert out.split()[1] == "JST"
    assert step.returncode == 0


@pytest.mark.parametrize(
    ("flags", "shows", "expected"),
    [(["-X", "utf8"], "sys.flags.utf8_mode", "1"), (["-u"], "sys.stdout.write_through", "True")],
    ids=["utf8", "unbuffered"],
)
def test_a_step_with_other_interpreter_flags_runs_in_its_own_process(
    flags: list[str], shows: str, expected: str, shell: _Shell
) -> None:
    shell.served(_PID)
    command = [sys.executable, *flags, "-m", "macos_harness.cli"]
    seen = []
    for _ in range(3):
        # Long enough for the next spare to be ready for the step.
        time.sleep(0.5)
        step, out, _err = shell.step(f"import os, sys\nprint(os.getpid(), {shows})\n", command)
        seen.append(out.split() == [str(step.pid), expected])

    assert seen == [True, True, True]


def test_a_served_step_leaves_the_frameworks_to_its_spare(
    shell: _Shell, tmp_path: Path
) -> None:
    loaded = tmp_path / "loaded"
    command = [sys.executable, "-c", _CLIENT_LOADS.format(path=str(loaded))]

    shell.served(_PID, command)

    modules = loaded.read_text().split()
    assert "macos_harness.spare" in modules
    assert [
        name
        for name in modules
        if name.split(".")[0] in {"AppKit", "Quartz", "objc", "ApplicationServices"}
        or name == "macos_harness.macos"
    ] == []
