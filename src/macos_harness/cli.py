"""One Python execution surface for the browser, macOS, and local files."""

from __future__ import annotations

import argparse
import code
import getpass
import json
import os
import pwd
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import IO, Protocol, TextIO

from ._version import __version__
from .browser import BrowserHarness
from .errors import MacOSError
from .receipts import OperationError
from .telemetry import capture_cli
from .telemetry import run_cli as run_telemetry_cli


def _namespace() -> dict[str, object]:
    from .macos import MacOS

    return {
        "__name__": "__macos_harness__",
        "browser": BrowserHarness(),
        "mac": MacOS(),
        "Path": Path,
        "subprocess": subprocess,
    }


def _execute(code: str) -> int:
    if not code.strip():
        print("No Python code received on stdin", file=sys.stderr)
        return 2
    namespace = _namespace()
    exec(compile(code, "<macos-harness>", "exec"), namespace, namespace)  # noqa: S102
    return 0


def _skill_text() -> str:
    bundled = resources.files("macos_harness").joinpath("SKILL.md")
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    checkout = Path(__file__).resolve().parents[2] / "skills/macos-harness/SKILL.md"
    return checkout.read_text(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="macos-harness",
        description="Execute Python with browser, macOS, and filesystem access.",
        epilog=(
            "Typical usage:\n  macos-harness <<'PY'\n  print(mac.see('Spotify'))\n  PY"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--json-errors",
        action="store_true",
        help="write native stdin execution errors as JSON to stderr",
    )
    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor", help="check macOS permissions and runtime")
    doctor.add_argument(
        "--request", action="store_true", help="request missing global permissions"
    )
    subparsers.add_parser("apps", help="list running macOS applications")
    subparsers.add_parser("repl", help="start a persistent interactive Python session")
    subparsers.add_parser("skill", help="print the macOS Harness skill")
    telemetry = subparsers.add_parser(
        "telemetry", help="inspect or change telemetry (off by default)"
    )
    telemetry.add_argument("action", nargs="?", choices=("status", "enable", "disable"))
    see = subparsers.add_parser("see", help="capture a bounded application window")
    see.add_argument("app")
    see.add_argument("--max-width", type=int, default=1280)
    see.add_argument("--max-height", type=int, default=1280)
    see.add_argument(
        "--pointer", action="store_true", help="draw the virtual pointer onto the image"
    )
    state = subparsers.add_parser(
        "state", help="print an application's AX state as JSON"
    )
    state.add_argument("app")
    state.add_argument("--screenshot", action="store_true")
    state.add_argument("--max-depth", type=int, default=25)
    state.add_argument("--max-nodes", type=int, default=5000)
    state.add_argument("--include-menu-bar", action="store_true")
    credential = subparsers.add_parser(
        "credential", help="fill or enroll a provisioned credential"
    )
    credential_sub = credential.add_subparsers(dest="credential_command", required=True)
    credential_sub.add_parser("check", help="list configured credential refs")
    credential_fill_browser = credential_sub.add_parser(
        "fill-browser", help="fill a credential into a live ego-browser field"
    )
    credential_fill_browser.add_argument("ref")
    credential_fill_browser.add_argument("--space", required=True)
    credential_fill_native = credential_sub.add_parser(
        "fill-native",
        help="refuse a native-app fill, and say which two paths do work",
    )
    credential_fill_native.add_argument("ref")
    credential_fill_native.add_argument("--app", required=True)
    credential_enroll = credential_sub.add_parser(
        "enroll",
        help="store a secret, or authorize a derived credential, in the vault",
    )
    credential_enroll.add_argument("ref")
    credential_enroll.add_argument(
        "--clipboard",
        action="store_true",
        help=(
            "read an operator-authored secret from the system pasteboard "
            "instead of stdin/TTY"
        ),
    )
    return parser


def _print_json_line(payload: Mapping[str, object], *, file: TextIO) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), file=file)


#: This account's own home, from the password database. `HOME` is
#: inherited and freely editable, so resolving the vault binary through
#: it would let whatever launched this process decide what `mem-secret`
#: means.
_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

_MEM_SECRET = str(_HOME / ".local" / "bin" / "mem-secret")
_PBPASTE = "/usr/bin/pbpaste"
_PBCOPY = "/usr/bin/pbcopy"
_GREP = "/usr/bin/grep"
_MEM_SECRET_TIMEOUT = 30.0
_PASTEBOARD_TIMEOUT = 5.0
_ENROLL_FAILED = "credential.enroll_failed"
_ENROLL_NOT_AUTHORED = "credential.enroll_not_authored"

#: Fixed prose for this module's own two codes, so every credential
#: failure prints one machine code *and* one sentence a human can act
#: on. Both tables this envelope draws from -- here, and
#: `credentials._MESSAGES` -- are compile-time constants that interpolate
#: nothing, which is the whole reason printing the message is safe: there
#: is no path by which a vault diagnostic, a pasteboard's contents, or a
#: page's text becomes one of these strings.
_CLI_MESSAGES: Mapping[str, str] = {
    _ENROLL_FAILED: "The vault refused the enrollment",
    _ENROLL_NOT_AUTHORED: "That credential's value is derived from policy, so there is nothing to paste",
}


class _Receipt(Protocol):
    def to_json(self) -> Mapping[str, object]: ...


class _Broker(Protocol):
    def check(self) -> Sequence[str]: ...

    def fill_browser(self, ref: str, *, space: str) -> _Receipt: ...

    def fill_native(self, ref: str, *, app: str) -> _Receipt: ...


class _Enrollment(Protocol):
    """Which vault entry one ref writes, and who authors its value.

    `marker` is the entire difference between the two enrollments. `None`
    means an operator authors the value, so it may come from a hidden TTY
    prompt, a pipe, or the pasteboard. A string means the value is
    derived nonsecret policy that this CLI writes itself, with no human
    input at all.
    """

    env: str
    marker: str | None


class _Manifest(Protocol):
    def enrollment(self, ref: str) -> _Enrollment: ...


class _ManifestLoader(Protocol):
    def load(self) -> _Manifest: ...


class _Credentials(Protocol):
    """Exactly the credential-policy surface this CLI reaches for."""

    CredentialError: type[Exception]
    CredentialBroker: Callable[[], _Broker]
    CredentialManifest: _ManifestLoader


def _credentials() -> _Credentials:
    """Import the credential policy, on the credential path only.

    `credentials` imports `hashlib`, `json`, `re`, `subprocess`, and
    `tomllib`, and compiles the manifest's validators at module scope. It
    costs a measured ~7 ms on top of importing this module, which every
    other command -- `--version` included -- would otherwise pay.
    """
    from . import credentials

    return credentials


class _CredentialCliError(Exception):
    """A CLI-local, already-redacted enroll failure.

    Kept distinct from ``CredentialError`` because these codes describe
    this module's own plumbing (the vault or the pasteboard refusing to
    run, timing out, or exiting non-zero) and its own refusal to paste
    into an enrollment nobody authors, never something the broker or the
    manifest raises. Carries the same ``.code`` so one ``except`` clause
    can treat both uniformly and no subprocess text ever reaches stderr.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Producer(Protocol):
    """A child whose stdout another child drains, and that must be reaped."""

    @property
    def pipe(self) -> IO[bytes] | None: ...

    def finish(self) -> int | None: ...


class _Spawner(Protocol):
    """The two process primitives credential enrollment needs.

    Injected instead of reached for through the `subprocess` module, so a
    test observes the argv, redirections, and deadlines this module
    actually asks the operating system for. Neither call raises and
    neither returns child output: a vault that cannot start, hangs, or
    exits nonzero becomes one redacted code, never a traceback quoting
    whatever it printed.
    """

    def run(
        self,
        argv: list[str],
        *,
        payload: bytes | bytearray | None = None,
        stdin: IO[bytes] | None = None,
        timeout: float,
    ) -> int | None: ...

    def spawn(self, argv: list[str], *, timeout: float) -> _Producer | None: ...


@dataclass(frozen=True, slots=True)
class _Child:
    """One live producer, bounded at both ends."""

    process: subprocess.Popen[bytes]
    timeout: float

    @property
    def pipe(self) -> IO[bytes] | None:
        return self.process.stdout

    def finish(self) -> int | None:
        # A locked or hung consumer must not leave plaintext parked in a
        # live pipe: drop our read end first, then bound the wait and kill
        # the producer so it cannot sit blocked on a write nobody drains.
        if self.process.stdout is not None:
            self.process.stdout.close()
        try:
            return self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            return None


class _Subprocesses:
    """The production spawner: real children, output discarded."""

    def run(
        self,
        argv: list[str],
        *,
        payload: bytes | bytearray | None = None,
        stdin: IO[bytes] | None = None,
        timeout: float,
    ) -> int | None:
        try:
            completed = subprocess.run(
                argv,
                input=payload,
                stdin=stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.returncode

    def spawn(self, argv: list[str], *, timeout: float) -> _Child | None:
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except OSError:
            return None
        return _Child(process, timeout)


class _SecretSource(Protocol):
    """Where an operator-authored secret comes from, when one is needed."""

    def read(self) -> bytearray: ...


@dataclass(frozen=True, slots=True)
class _ConsoleSecret:
    """The operator's own secret: hidden at a TTY, raw bytes when piped."""

    stdin: TextIO
    prompt: Callable[[str], str] = getpass.getpass

    def read(self) -> bytearray:
        if self.stdin.isatty():
            typed = self.prompt("Secret value for enrollment (input hidden): ")
            return bytearray(typed.encode("utf-8"))
        return bytearray(self.stdin.buffer.read())


def _require_stored(status: int | None) -> None:
    """Anything but a clean vault exit is one redacted enrollment failure."""
    if status != 0:
        raise _CredentialCliError(_ENROLL_FAILED)


@dataclass(frozen=True, slots=True, kw_only=True)
class _CredentialCli:
    """One credential subcommand and every collaborator it may reach."""

    credentials: _Credentials
    spawner: _Spawner
    secrets: _SecretSource

    def run(self, args: argparse.Namespace) -> int:
        try:
            if args.credential_command == "check":
                refs = list(self.credentials.CredentialBroker().check())
                _print_json_line({"state": "checked", "refs": refs}, file=sys.stdout)
                return 0
            if args.credential_command == "fill-browser":
                receipt = self.credentials.CredentialBroker().fill_browser(
                    args.ref, space=args.space
                )
                _print_json_line(receipt.to_json(), file=sys.stdout)
                return 0
            if args.credential_command == "fill-native":
                # Always raises. The broker owns that refusal, so this
                # path is one call rather than a message duplicated here.
                self.credentials.CredentialBroker().fill_native(args.ref, app=args.app)
                return 1
            return self._enroll(args.ref, clipboard=args.clipboard)
        except (self.credentials.CredentialError, _CredentialCliError) as exc:
            # The code is the machine contract; the message says what to
            # do about it. Both come from closed compile-time tables, so
            # neither can carry a subprocess's or a page's own words.
            _print_json_line(
                {"error": exc.code, "message": _CLI_MESSAGES.get(exc.code) or str(exc)},
                file=sys.stderr,
            )
            return 1

    def _enroll(self, ref: str, *, clipboard: bool) -> int:
        # Resolving the ref first keeps a typo from destroying a pasteboard
        # nothing ever read, and settles which enrollment this is: only a
        # value an operator authors can come from a human or a pasteboard.
        enrollment = self.credentials.CredentialManifest.load().enrollment(ref)
        marker = enrollment.marker
        if marker is not None:
            if clipboard:
                raise _CredentialCliError(_ENROLL_NOT_AUTHORED)
            self._store(enrollment.env, bytearray(marker.encode("ascii")))
        elif clipboard:
            self._store_pasteboard(enrollment.env)
        else:
            self._store(enrollment.env, self.secrets.read())
        _print_json_line({"state": "enrolled", "credential_ref": ref}, file=sys.stdout)
        return 0

    def _store(self, env: str, value: bytearray) -> None:
        """Hand `value` to the vault under `env`, then wipe this copy."""
        try:
            status = self.spawner.run(
                [_MEM_SECRET, "set", env],
                payload=value,
                timeout=_MEM_SECRET_TIMEOUT,
            )
        finally:
            for index in range(len(value)):
                value[index] = 0
        _require_stored(status)

    def _store_pasteboard(self, env: str) -> None:
        """Stream the pasteboard into the vault, buffering nothing here.

        Past the first read the pasteboard's contents may already be
        consumed, so clearing it is a postcondition of enrollment: an
        unproven-empty pasteboard is a failure, not a success.
        """
        try:
            producer = self.spawner.spawn([_PBPASTE], timeout=_PASTEBOARD_TIMEOUT)
            if producer is None:
                raise _CredentialCliError(_ENROLL_FAILED)
            try:
                status = self.spawner.run(
                    [_MEM_SECRET, "set", env],
                    stdin=producer.pipe,
                    timeout=_MEM_SECRET_TIMEOUT,
                )
            finally:
                produced = producer.finish()
            _require_stored(status)
            if produced != 0:
                raise _CredentialCliError(_ENROLL_FAILED)
        finally:
            cleared = self._clear_pasteboard()
        if not cleared:
            raise _CredentialCliError(_ENROLL_FAILED)

    def _clear_pasteboard(self) -> bool:
        emptied = self.spawner.run([_PBCOPY], payload=b"", timeout=_PASTEBOARD_TIMEOUT)
        return emptied == 0 and self._pasteboard_is_empty()

    def _pasteboard_is_empty(self) -> bool:
        """Prove the pasteboard is empty without reading a byte of it here.

        A fresh pbpaste streams straight into `grep -q .`, whose exit
        status is the only thing that crosses back: 1 means nothing is
        left, 0 means data survived the clear. No shell, and the bytes go
        to `/dev/null` either way.
        """
        producer = self.spawner.spawn([_PBPASTE], timeout=_PASTEBOARD_TIMEOUT)
        if producer is None:
            return False
        try:
            looked = self.spawner.run(
                [_GREP, "-q", "."],
                stdin=producer.pipe,
                timeout=_PASTEBOARD_TIMEOUT,
            )
        finally:
            producer.finish()
        return looked == 1


def _credential_cli() -> _CredentialCli:
    return _CredentialCli(
        credentials=_credentials(),
        spawner=_Subprocesses(),
        secrets=_ConsoleSecret(sys.stdin),
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "telemetry":
        return run_telemetry_cli([args.action] if args.action else [])

    started = time.monotonic()
    result: int | None = None
    try:
        if args.command == "doctor":
            from .macos import MacOS

            mac = MacOS()
            if args.request:
                mac.request_permissions()
            print(json.dumps(mac.doctor(), indent=2))
            result = 0
            return result
        if args.command == "apps":
            from .macos import MacOS

            print(json.dumps(MacOS().list_apps(), indent=2))
            result = 0
            return result
        if args.command == "skill":
            print(_skill_text(), end="")
            result = 0
            return result
        if args.command == "repl":
            code.interact(
                banner=(
                    "macos-harness: mac.see/key/type/click/ax/script, browser, "
                    "Path, and subprocess are ready"
                ),
                local=_namespace(),
                exitmsg="",
            )
            result = 0
            return result
        if args.command == "see":
            from .macos import MacOS

            result = MacOS().see(
                args.app,
                max_width=args.max_width,
                max_height=args.max_height,
                show_pointer=args.pointer,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            result = 0
            return result
        if args.command == "state":
            from .macos import MacOS

            state = MacOS().get_app_state(
                args.app,
                screenshot=args.screenshot,
                max_depth=args.max_depth,
                max_nodes=args.max_nodes,
                include_menu_bar=args.include_menu_bar,
            )
            print(json.dumps(state, indent=2, ensure_ascii=False))
            result = 0
            return result
        if args.command == "credential":
            result = _credential_cli().run(args)
            return result
        if args.command is None:
            if sys.stdin.isatty():
                parser.print_help()
                result = 2
                return result
            result = _execute(sys.stdin.read())
            return result
        parser.error(f"unknown command: {args.command}")
    except (MacOSError, RuntimeError) as exc:
        if args.command is None and args.json_errors and isinstance(exc, MacOSError):
            payload = exc.to_json()
            if isinstance(exc, OperationError):
                payload["receipt"] = exc.receipt.to_json()
            _print_json_line(payload, file=sys.stderr)
        else:
            print(f"macos-harness: {exc}", file=sys.stderr)
        result = 1
        return result
    finally:
        capture_cli(args.command or "python", result == 0, time.monotonic() - started)


if __name__ == "__main__":
    raise SystemExit(main())
