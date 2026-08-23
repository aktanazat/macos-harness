"""CI-safe tests for the ``macos-harness credential`` subcommands.

Nothing here patches a module. ``cli._CredentialCli`` takes its three
collaborators -- the credential policy, a process spawner, and the source of
an operator-authored secret -- as constructor arguments, so every dispatch
test hands it fakes and reads back exactly what the CLI asked for: the argv,
the redirections, the deadlines, and the order the children ran in.

The production spawner is not faked away. ``_Subprocesses`` and ``_Child`` are
exercised against real, harmless binaries (``/bin/echo``, ``/usr/bin/grep``,
``/usr/bin/false``, ``/bin/sleep``), which is where the discard-the-output,
bound-the-wait, and kill-the-overstayer promises are actually proven.

A ``CANARY`` string stands in for a real secret: every test that touches
enrollment asserts it never appears in captured stdout, captured stderr, or
any recorded argv -- only ever as the payload handed straight to the vault.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from macos_harness import cli

CANARY = "sk_live_CANARY_DO_NOT_LEAK_7f3a9c2b91"
SECRET_ENV = "MACOS_HARNESS_CRED_0123456789ABCDEF0123456789ABCDEF"
AUTH_ENV = "MACOS_HARNESS_CRED_AUTH_0123456789ABCDEF0123456789ABCDEF"
MARKER = "9f" * 32
MEM_SECRET_SET = [cli._MEM_SECRET, "set", SECRET_ENV]

ECHO = "/bin/echo"
FALSE = "/usr/bin/false"
SLEEP = "/bin/sleep"
MISSING = "/nonexistent/macos-harness-not-a-binary"


def _fail(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("this call must never happen")


# --- doubles ---------------------------------------------------------------


class _FakeCredentialError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _FakeEnrollment:
    env: str
    marker: str | None


class _FakeManifest:
    """Both the loader and the manifest it loads, as the CLI sees them."""

    def __init__(
        self, *, enrollment: _FakeEnrollment | None = None, refusal: str | None = None
    ) -> None:
        self._enrollment = enrollment
        self._refusal = refusal
        self.requested: list[str] = []

    def load(self) -> _FakeManifest:
        return self

    def enrollment(self, ref: str) -> _FakeEnrollment:
        self.requested.append(ref)
        if self._refusal is not None:
            raise _FakeCredentialError(self._refusal)
        assert self._enrollment is not None
        return self._enrollment


class _Forbidden:
    """A collaborator this test forbids the CLI to touch at all."""

    def __call__(self) -> object:
        raise AssertionError("the broker must not be constructed")

    def load(self) -> object:
        raise AssertionError("the manifest must not be loaded")


class _FakeCredentials:
    """Exactly the credential-policy surface `_CredentialCli` may reach."""

    CredentialError = _FakeCredentialError

    def __init__(self, *, broker: object = None, manifest: object = None) -> None:
        self.CredentialBroker = broker if broker is not None else _Forbidden()
        self.CredentialManifest = manifest if manifest is not None else _Forbidden()


class _FakePipe:
    def __init__(self) -> None:
        self.closed = False


@dataclass(slots=True)
class _FakeChild:
    """A producer that records how it was drained and whether it overstayed."""

    argv: list[str]
    timeout: float
    status: int = 0
    hangs: bool = False
    pipe: _FakePipe = field(default_factory=_FakePipe)
    killed: bool = False
    finished: int | None = None

    def finish(self) -> int | None:
        self.pipe.closed = True
        if self.hangs:
            self.killed = True
            return None
        self.finished = self.status
        return self.status


@dataclass(frozen=True, slots=True)
class _Spawned:
    argv: list[str]
    payload: bytes | bytearray | None
    stdin: object
    timeout: float


class _FakeSpawner:
    """Records every child the CLI asks for, and scripts each outcome."""

    def __init__(
        self,
        *,
        vault: int | None = 0,
        producer: int = 0,
        producer_hangs: bool = False,
        grep: int | None = 1,
        pbcopy: int | None = 0,
        spawn_failures: int = 0,
    ) -> None:
        self.ran: list[_Spawned] = []
        self.children: list[_FakeChild] = []
        self._vault = vault
        self._producer = producer
        self._producer_hangs = producer_hangs
        self._grep = grep
        self._pbcopy = pbcopy
        self._spawn_failures = spawn_failures

    def run(
        self,
        argv: list[str],
        *,
        payload: bytes | bytearray | None = None,
        stdin: object = None,
        timeout: float,
    ) -> int | None:
        self.ran.append(_Spawned(list(argv), payload, stdin, timeout))
        if argv[0] == cli._MEM_SECRET:
            return self._vault
        if argv[0] == cli._GREP:
            return self._grep
        if argv[0] == cli._PBCOPY:
            return self._pbcopy
        raise AssertionError(f"unexpected child: {argv}")

    def spawn(self, argv: list[str], *, timeout: float) -> _FakeChild | None:
        if self._spawn_failures > 0:
            self._spawn_failures -= 1
            return None
        child = _FakeChild(
            list(argv),
            timeout,
            status=self._producer,
            hangs=self._producer_hangs,
        )
        self.children.append(child)
        return child

    def commands(self) -> list[list[str]]:
        return [spawned.argv for spawned in self.ran]

    def only(self, executable: str) -> _Spawned:
        matches = [spawned for spawned in self.ran if spawned.argv[0] == executable]
        assert len(matches) == 1, f"{executable} ran {len(matches)} times"
        return matches[0]


class _TtyStdin:
    def isatty(self) -> bool:
        return True


class _PipedStdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = _FakeBuffer(data)

    def isatty(self) -> bool:
        return False


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _RecordedSecret:
    def __init__(self, value: bytes) -> None:
        self._value = value
        self.reads = 0

    def read(self) -> bytearray:
        self.reads += 1
        return bytearray(self._value)


class _ExplodingSecret:
    """Proves an enrollment never asks a human for anything."""

    def read(self) -> bytearray:
        raise AssertionError("this enrollment must not read an operator secret")


def _cli(
    *,
    credentials: _FakeCredentials,
    spawner: object = None,
    secrets: object = None,
) -> cli._CredentialCli:
    return cli._CredentialCli(
        credentials=credentials,
        spawner=spawner if spawner is not None else _FakeSpawner(),
        secrets=secrets if secrets is not None else _ExplodingSecret(),
    )


def _args(*argv: str) -> argparse.Namespace:
    return cli._build_parser().parse_args(["credential", *argv])


# --- parser --------------------------------------------------------------


def test_credential_check_parses() -> None:
    args = cli._build_parser().parse_args(["credential", "check"])
    assert args.command == "credential"
    assert args.credential_command == "check"


def test_credential_fill_browser_parses() -> None:
    args = cli._build_parser().parse_args(
        ["credential", "fill-browser", "github", "--space", "work"]
    )
    assert args.credential_command == "fill-browser"
    assert args.ref == "github"
    assert args.space == "work"


def test_credential_fill_browser_requires_space() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli._build_parser().parse_args(["credential", "fill-browser", "github"])
    assert excinfo.value.code == 2


def test_credential_enroll_parses_with_clipboard_flag() -> None:
    args = cli._build_parser().parse_args(
        ["credential", "enroll", "github", "--clipboard"]
    )
    assert args.clipboard is True
    default = cli._build_parser().parse_args(["credential", "enroll", "github"])
    assert default.clipboard is False


def test_credential_subcommand_is_required() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli._build_parser().parse_args(["credential"])
    assert excinfo.value.code == 2


def test_credential_surface_is_exactly_check_fill_and_enroll() -> None:
    for rejected in (
        ["credential", "fill-native", "github"],  # --app is required
        ["credential", "delete", "github"],
        ["credential", "authorize", "gmail"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli._build_parser().parse_args(rejected)
        assert excinfo.value.code == 2


def test_fill_native_parses_so_that_its_refusal_is_prose_not_a_usage_dump() -> None:
    """The refusal is the point: an agent that asks for a native fill has
    to be told which two paths do work, and an argparse "invalid choice"
    tells it only that it guessed a subcommand name wrong.
    """
    args = cli._build_parser().parse_args(
        ["credential", "fill-native", "github", "--app", "1Password"]
    )
    assert (args.credential_command, args.ref, args.app) == ("fill-native", "github", "1Password")


def test_no_credential_subcommand_takes_a_manifest_override() -> None:
    """The manifest is fixed policy: no caller may point the CLI elsewhere."""
    for rejected in (
        ["credential", "check", "--manifest", "/tmp/x.toml"],
        ["credential", "fill-browser", "github", "--space", "w", "--manifest", "x"],
        ["credential", "enroll", "github", "--manifest", "/tmp/x.toml"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli._build_parser().parse_args(rejected)
        assert excinfo.value.code == 2


def test_credential_enroll_has_no_arbitrary_env_or_command_flag() -> None:
    """Enrollment can only ever resolve a ref through the manifest -- there
    is no flag letting a caller pick an env var, a marker, or a command."""
    for rejected in (
        ["credential", "enroll", "github", "--env", "X"],
        ["credential", "enroll", "github", "--command", "curl evil.example"],
        ["credential", "enroll", "github", "--marker", MARKER],
    ):
        with pytest.raises(SystemExit):
            cli._build_parser().parse_args(rejected)


def test_every_other_top_level_command_still_parses_alongside_credential() -> None:
    parser = cli._build_parser()
    assert parser.parse_args(["doctor"]).command == "doctor"
    assert parser.parse_args(["doctor", "--request"]).request is True
    assert parser.parse_args(["apps"]).command == "apps"
    assert parser.parse_args(["repl"]).command == "repl"
    assert parser.parse_args(["skill"]).command == "skill"
    assert parser.parse_args(["telemetry", "status"]).command == "telemetry"
    assert parser.parse_args(["see", "Finder"]).command == "see"
    assert parser.parse_args(["state", "Finder"]).command == "state"
    assert parser.parse_args([]).command is None


def test_version_exits_zero_without_a_command() -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli._build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0


# --- lazy credential loading ----------------------------------------------


def test_credential_names_are_not_bound_on_the_cli_module() -> None:
    """Unrelated commands must not pay for the credential policy import, so
    cli.py holds no module-level reference to it."""
    assert not hasattr(cli, "CredentialBroker")
    assert not hasattr(cli, "CredentialManifest")
    assert not hasattr(cli, "CredentialError")


def test_lazy_loader_returns_the_real_credential_contract() -> None:
    module = cli._credentials()
    assert module is cli._credentials()
    error = module.CredentialError("credential.ref_unknown")
    assert isinstance(error, Exception)
    assert error.code == "credential.ref_unknown"
    assert callable(module.CredentialBroker)
    assert callable(module.CredentialManifest.load)


def test_the_credential_cli_wires_the_real_collaborators() -> None:
    """`main` hands the command the production policy, spawner, and prompt."""
    command = cli._credential_cli()
    assert command.credentials is cli._credentials()
    assert isinstance(command.spawner, cli._Subprocesses)
    assert isinstance(command.secrets, cli._ConsoleSecret)
    assert command.secrets.stdin is sys.stdin


def test_mem_secret_is_pinned_to_the_account_home_not_home_or_the_path() -> None:
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert cli._MEM_SECRET == str(account_home / ".local" / "bin" / "mem-secret")
    assert Path(cli._MEM_SECRET).is_absolute()
    assert cli._MEM_SECRET == cli._credentials()._MEM_SECRET, (
        "the CLI and the broker must pin the same vault binary"
    )


def test_mem_secret_path_ignores_a_hostile_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`HOME` is inherited and editable; the password database is not."""
    monkeypatch.setenv("HOME", "/tmp/attacker")
    reloaded = str(
        Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "bin" / "mem-secret"
    )
    assert reloaded == cli._MEM_SECRET
    assert "/tmp/attacker" not in cli._MEM_SECRET


# --- check / fill-browser dispatch ----------------------------------------


def test_credential_check_prints_compact_json_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Broker:
        def check(self) -> tuple[str, ...]:
            return ("github", "totp-ref")

    command = _cli(credentials=_FakeCredentials(broker=Broker))
    assert command.run(_args("check")) == 0
    assert (
        capsys.readouterr().out == '{"state":"checked","refs":["github","totp-ref"]}\n'
    )


def test_credential_fill_browser_dispatches_and_prints_receipt_verbatim(
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt_payload = {
        "state": "filled",
        "credential_ref": "github",
        "provider": "password",
        "sink": "browser",
        "acted": True,
    }
    seen: dict[str, object] = {}

    class Receipt:
        def to_json(self) -> dict[str, object]:
            return dict(receipt_payload)

    class Broker:
        def fill_browser(self, ref: str, *, space: str) -> Receipt:
            seen["ref"] = ref
            seen["space"] = space
            return Receipt()

    command = _cli(credentials=_FakeCredentials(broker=Broker))
    assert command.run(_args("fill-browser", "github", "--space", "work")) == 0
    assert seen == {"ref": "github", "space": "work"}
    assert json.loads(capsys.readouterr().out) == receipt_payload


def test_credential_error_prints_redacted_code_and_exits_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Broker:
        def check(self) -> tuple[str, ...]:
            raise _FakeCredentialError("credential.manifest_invalid")

    command = _cli(credentials=_FakeCredentials(broker=Broker))
    assert command.run(_args("check")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "credential.manifest_invalid"


def test_fill_native_is_refused_by_the_broker_and_never_reaches_a_sink(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI does not own this refusal, and must not: it forwards the
    request and prints whatever the broker says, so there is exactly one
    place that decides a native fill is out of scope.
    """
    seen: dict[str, object] = {}

    class Broker:
        def fill_native(self, ref: str, *, app: str) -> object:
            seen["ref"] = ref
            seen["app"] = app
            raise _FakeCredentialError("credential.unsupported_sink")

    command = _cli(credentials=_FakeCredentials(broker=Broker))
    assert command.run(_args("fill-native", "github", "--app", "Slack")) == 1
    assert seen == {"ref": "github", "app": "Slack"}
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "credential.unsupported_sink"


def test_a_credential_failure_prints_its_fixed_message_beside_its_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A code alone tells an agent that something is wrong, not what to do
    instead. The message is fixed prose from a closed table -- here the
    real one -- so printing it cannot leak anything the run observed.
    """
    from macos_harness import credentials

    assert cli.main(["credential", "fill-native", "github", "--app", "Slack"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"] == "credential.unsupported_sink"
    assert payload["message"] == str(credentials.CredentialError("credential.unsupported_sink"))
    assert "AutoFill" in payload["message"] and "human" in payload["message"]


def test_main_dispatches_the_credential_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main` reaches the credential path and reports its code, not a crash."""
    assert cli.main(["credential", "enroll", "definitely-not-a-real-ref"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"].startswith("credential.")


# --- enrollment: an operator-authored secret --------------------------------


def test_console_secret_prompts_hidden_on_a_tty() -> None:
    prompts: list[str] = []

    def prompt(text: str) -> str:
        prompts.append(text)
        return CANARY

    secret = cli._ConsoleSecret(_TtyStdin(), prompt)
    assert bytes(secret.read()) == CANARY.encode()
    assert prompts == ["Secret value for enrollment (input hidden): "]


def test_console_secret_reads_raw_stdin_when_piped() -> None:
    secret = cli._ConsoleSecret(_PipedStdin(CANARY.encode()), _fail)
    assert bytes(secret.read()) == CANARY.encode()


def test_enroll_streams_the_secret_to_the_vault_and_zeroes_the_buffer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _FakeManifest(enrollment=_FakeEnrollment(SECRET_ENV, None))
    spawner = _FakeSpawner()
    secrets = _RecordedSecret(CANARY.encode())
    command = _cli(
        credentials=_FakeCredentials(manifest=manifest),
        spawner=spawner,
        secrets=secrets,
    )

    assert command.run(_args("enroll", "github")) == 0

    assert manifest.requested == ["github"]
    assert secrets.reads == 1
    assert spawner.commands() == [MEM_SECRET_SET]
    vault = spawner.only(cli._MEM_SECRET)
    assert vault.stdin is None
    assert vault.timeout == cli._MEM_SECRET_TIMEOUT == 30.0
    assert isinstance(vault.payload, bytearray)
    assert set(vault.payload) == {0}, "the secret buffer must be zeroed after use"
    assert spawner.children == []

    captured = capsys.readouterr()
    assert captured.out == '{"state":"enrolled","credential_ref":"github"}\n'
    assert CANARY not in captured.out + captured.err
    for argv in spawner.commands():
        assert CANARY not in " ".join(argv)


@pytest.mark.parametrize("status", [1, None], ids=["nonzero-exit", "hung-or-missing"])
def test_enroll_reports_a_redacted_failure_and_still_zeroes_the_secret(
    capsys: pytest.CaptureFixture[str], status: int | None
) -> None:
    spawner = _FakeSpawner(vault=status)
    command = _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(enrollment=_FakeEnrollment(SECRET_ENV, None))
        ),
        spawner=spawner,
        secrets=_RecordedSecret(CANARY.encode()),
    )

    assert command.run(_args("enroll", "github")) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error"] == "credential.enroll_failed"
    # This module's own code, so this module's own fixed prose -- and
    # still nothing the vault said, which is the point of redacting it.
    assert payload["message"] == cli._CLI_MESSAGES["credential.enroll_failed"]
    assert CANARY not in captured.err
    wiped = spawner.only(cli._MEM_SECRET).payload
    assert isinstance(wiped, bytearray)
    assert set(wiped) == {0}


@pytest.mark.parametrize(
    "code", ["credential.ref_unknown", "credential.manifest_missing"]
)
def test_enroll_rejects_a_bad_ref_without_spawning_anything(
    capsys: pytest.CaptureFixture[str], code: str
) -> None:
    spawner = _FakeSpawner()
    command = _cli(
        credentials=_FakeCredentials(manifest=_FakeManifest(refusal=code)),
        spawner=spawner,
        secrets=_ExplodingSecret(),
    )

    assert command.run(_args("enroll", "some-ref")) == 1
    assert spawner.ran == []
    assert spawner.children == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == code


# --- enrollment: a derived, nonsecret marker (Gmail) ------------------------


def test_enroll_writes_the_derived_marker_with_no_human_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One agent command: no prompt, no stdin, no pasteboard, no echo."""
    manifest = _FakeManifest(enrollment=_FakeEnrollment(AUTH_ENV, MARKER))
    spawner = _FakeSpawner()
    command = _cli(
        credentials=_FakeCredentials(manifest=manifest),
        spawner=spawner,
        secrets=_ExplodingSecret(),
    )

    assert command.run(_args("enroll", "gmail-login")) == 0

    assert manifest.requested == ["gmail-login"]
    assert spawner.commands() == [[cli._MEM_SECRET, "set", AUTH_ENV]]
    vault = spawner.only(cli._MEM_SECRET)
    assert vault.stdin is None
    assert vault.timeout == cli._MEM_SECRET_TIMEOUT
    assert spawner.children == [], "the pasteboard must stay untouched"

    captured = capsys.readouterr()
    assert captured.out == '{"state":"enrolled","credential_ref":"gmail-login"}\n'
    assert MARKER not in captured.out + captured.err
    assert AUTH_ENV not in captured.out + captured.err


def test_the_marker_reaching_the_vault_is_exactly_the_policy_digest() -> None:
    delivered: list[bytes] = []

    class Recorder(_FakeSpawner):
        def run(
            self,
            argv: list[str],
            *,
            payload: bytes | bytearray | None = None,
            stdin: object = None,
            timeout: float,
        ) -> int | None:
            assert payload is not None
            delivered.append(bytes(payload))
            return super().run(argv, payload=payload, stdin=stdin, timeout=timeout)

    command = _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(enrollment=_FakeEnrollment(AUTH_ENV, MARKER))
        ),
        spawner=Recorder(),
        secrets=_ExplodingSecret(),
    )
    assert command.run(_args("enroll", "gmail-login")) == 0
    assert delivered == [MARKER.encode("ascii")]


def test_enroll_refuses_the_clipboard_for_a_marker_it_authors_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A derived marker has no operator-authored value to paste, so the
    pasteboard is neither read nor destroyed."""
    spawner = _FakeSpawner()
    command = _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(enrollment=_FakeEnrollment(AUTH_ENV, MARKER))
        ),
        spawner=spawner,
        secrets=_ExplodingSecret(),
    )

    assert command.run(_args("enroll", "gmail-login", "--clipboard")) == 1
    assert spawner.ran == []
    assert spawner.children == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "credential.enroll_not_authored"


def test_marker_enrollment_reports_a_redacted_vault_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(enrollment=_FakeEnrollment(AUTH_ENV, MARKER))
        ),
        spawner=_FakeSpawner(vault=1),
        secrets=_ExplodingSecret(),
    )
    assert command.run(_args("enroll", "gmail-login")) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "credential.enroll_failed"


# --- enrollment: --clipboard -----------------------------------------------


def _clipboard_cli(spawner: _FakeSpawner) -> cli._CredentialCli:
    return _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(enrollment=_FakeEnrollment(SECRET_ENV, None))
        ),
        spawner=spawner,
        secrets=_ExplodingSecret(),
    )


def test_clipboard_enrollment_pipes_the_pasteboard_straight_into_the_vault(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawner = _FakeSpawner()
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 0

    producer, verifier = spawner.children
    assert producer.argv == [cli._PBPASTE]
    assert producer.timeout == cli._PASTEBOARD_TIMEOUT == 5.0
    assert producer.pipe.closed is True
    assert producer.finished == 0

    vault = spawner.only(cli._MEM_SECRET)
    assert vault.argv == MEM_SECRET_SET
    assert vault.stdin is producer.pipe
    assert vault.payload is None, "the secret must never be buffered in Python"
    assert vault.timeout == cli._MEM_SECRET_TIMEOUT

    assert spawner.commands() == [
        MEM_SECRET_SET,
        [cli._PBCOPY],
        [cli._GREP, "-q", "."],
    ]
    assert spawner.only(cli._PBCOPY).payload == b""
    assert spawner.only(cli._PBCOPY).timeout == cli._PASTEBOARD_TIMEOUT

    proof = spawner.only(cli._GREP)
    assert proof.stdin is verifier.pipe
    assert proof.timeout == cli._PASTEBOARD_TIMEOUT
    assert verifier.pipe.closed is True

    captured = capsys.readouterr()
    assert captured.out == '{"state":"enrolled","credential_ref":"github"}\n'
    assert CANARY not in captured.out + captured.err


@pytest.mark.parametrize("grep", [0, 2, None], ids=["data-remains", "error", "gone"])
def test_clipboard_enrollment_fails_when_the_pasteboard_is_not_proven_empty(
    capsys: pytest.CaptureFixture[str], grep: int | None
) -> None:
    """Only grep exiting 1 proves the pasteboard is empty; anything else --
    surviving data, or grep itself failing -- is not an enrollment."""
    spawner = _FakeSpawner(grep=grep)
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 1
    assert [cli._GREP, "-q", "."] in spawner.commands()
    assert json.loads(capsys.readouterr().err)["error"] == "credential.enroll_failed"


def test_clipboard_enrollment_fails_when_the_pasteboard_cannot_be_emptied(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawner = _FakeSpawner(pbcopy=1)
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 1
    assert [cli._GREP, "-q", "."] not in spawner.commands()
    assert json.loads(capsys.readouterr().err)["error"] == "credential.enroll_failed"


def test_clipboard_enrollment_drains_the_producer_when_the_vault_hangs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hung vault must not leave plaintext parked in a live pipe."""
    spawner = _FakeSpawner(vault=None, producer_hangs=True)
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 1

    producer = spawner.children[0]
    assert producer.pipe.closed is True
    assert producer.killed is True
    assert [cli._PBCOPY] in spawner.commands()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"] == "credential.enroll_failed"


def test_clipboard_enrollment_fails_when_the_producer_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawner = _FakeSpawner(producer=1)
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 1
    assert [cli._PBCOPY] in spawner.commands()
    assert json.loads(capsys.readouterr().err)["error"] == "credential.enroll_failed"


def test_clipboard_enrollment_clears_the_pasteboard_when_pbpaste_cannot_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawner = _FakeSpawner(spawn_failures=1)
    assert _clipboard_cli(spawner).run(_args("enroll", "github", "--clipboard")) == 1
    assert [cli._PBCOPY] in spawner.commands()
    assert json.loads(capsys.readouterr().err)["error"] == "credential.enroll_failed"


def test_clipboard_enrollment_leaves_the_pasteboard_alone_for_a_rejected_ref(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A typoed ref is resolved before anything reads the pasteboard, so it
    must not be destroyed for a secret nothing ever consumed."""
    spawner = _FakeSpawner()
    command = _cli(
        credentials=_FakeCredentials(
            manifest=_FakeManifest(refusal="credential.ref_unknown")
        ),
        spawner=spawner,
        secrets=_ExplodingSecret(),
    )

    assert command.run(_args("enroll", "typo", "--clipboard")) == 1
    assert spawner.ran == []
    assert spawner.children == []
    assert json.loads(capsys.readouterr().err)["error"] == "credential.ref_unknown"


# --- the production spawner, against real harmless binaries -----------------


def test_spawner_returns_the_child_exit_status() -> None:
    spawner = cli._Subprocesses()
    assert spawner.run([ECHO, "hello"], timeout=5.0) == 0
    assert spawner.run([FALSE], timeout=5.0) == 1


def test_spawner_discards_child_stdout_and_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    spawner = cli._Subprocesses()
    assert spawner.run([ECHO, CANARY], timeout=5.0) == 0
    assert spawner.run(["/bin/sh", "-c", f"echo {CANARY} >&2"], timeout=5.0) == 0
    captured = capfd.readouterr()
    assert CANARY not in captured.out
    assert CANARY not in captured.err


def test_spawner_feeds_the_payload_to_the_child_stdin() -> None:
    spawner = cli._Subprocesses()
    match = [cli._GREP, "-q", CANARY]
    assert spawner.run(match, payload=CANARY.encode(), timeout=5.0) == 0
    assert spawner.run(match, payload=b"something else", timeout=5.0) == 1
    assert spawner.run(match, payload=bytearray(CANARY.encode()), timeout=5.0) == 0


def test_spawner_reports_a_missing_binary_as_no_status() -> None:
    assert cli._Subprocesses().run([MISSING], payload=b"", timeout=5.0) is None
    assert cli._Subprocesses().spawn([MISSING], timeout=5.0) is None


def test_spawner_bounds_a_child_that_never_exits() -> None:
    started = time.monotonic()
    assert cli._Subprocesses().run([SLEEP, "30"], payload=b"", timeout=0.25) is None
    assert time.monotonic() - started < 10.0


def test_spawned_producer_streams_into_a_second_child() -> None:
    """The exact wiring clipboard enrollment depends on: one child's stdout
    is the next child's stdin, and nothing passes through Python."""
    spawner = cli._Subprocesses()
    producer = spawner.spawn([ECHO, CANARY], timeout=5.0)
    assert producer is not None
    assert spawner.run([cli._GREP, "-q", CANARY], stdin=producer.pipe, timeout=5.0) == 0
    assert producer.finish() == 0
    assert producer.pipe is not None
    assert producer.pipe.closed is True


def test_spawned_producer_is_killed_when_it_overstays() -> None:
    producer = cli._Subprocesses().spawn([SLEEP, "30"], timeout=0.25)
    assert producer is not None
    started = time.monotonic()
    assert producer.finish() is None
    assert time.monotonic() - started < 10.0
    assert producer.process.poll() is not None, "the overstayer must be dead"
    assert producer.process.returncode == -signal.SIGKILL


def test_spawned_producer_closes_our_read_end_even_when_it_is_killed() -> None:
    producer = cli._Subprocesses().spawn([SLEEP, "30"], timeout=0.25)
    assert producer is not None
    pipe = producer.pipe
    assert pipe is not None
    assert producer.finish() is None
    assert pipe.closed is True


def test_a_child_that_cannot_start_is_never_an_exception() -> None:
    spawner = cli._Subprocesses()
    for argv in ([MISSING], [MISSING, "--flag"]):
        assert spawner.run(argv, payload=b"", timeout=1.0) is None
        assert spawner.spawn(argv, timeout=1.0) is None
