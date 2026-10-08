"""One Python execution surface for the browser, macOS, and local files."""

from __future__ import annotations

import argparse
import code
import inspect
import json
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import TextIO

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


# A call with the wrong arguments raises before its body runs, naming the
# method by qualified name: "MacOS.scroll() got an unexpected keyword ...".
_BAD_CALL = re.compile(r"(\w+)\.(\w+)\(\) ")


def _signature_note(exc: TypeError, namespace: Mapping[str, object]) -> str | None:
    """The real signature of the preloaded harness method ``exc`` names."""
    from .macos import MacOS

    call = _BAD_CALL.match(str(exc))
    if call is None or call[2].startswith("_"):
        return None
    mac = namespace.get("mac")
    surfaces: dict[str, object] = {"mac": mac, "browser": namespace.get("browser")}
    if isinstance(mac, MacOS):
        surfaces.update({"mac.ax": mac.ax, "mac.do": mac.do})
    for label, surface in surfaces.items():
        if type(surface).__name__ != call[1]:
            continue
        method = getattr(surface, call[2], None)
        if not callable(method):
            return None
        signature = inspect.signature(method)
        plain = signature.replace(
            parameters=[
                parameter.replace(annotation=parameter.empty)
                for parameter in signature.parameters.values()
            ],
            return_annotation=signature.empty,
        )
        return f"Signature: {label}.{call[2]}{plain}"
    return None


def _execute(code: str) -> int:
    if not code.strip():
        print("No Python code received on stdin", file=sys.stderr)
        return 2
    namespace = _namespace()
    try:
        exec(compile(code, "<macos-harness>", "exec"), namespace, namespace)  # noqa: S102
    except TypeError as exc:
        note = _signature_note(exc, namespace)
        if note is not None:
            exc.add_note(note)
        raise
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
    return parser


def _print_json_line(payload: Mapping[str, object], *, file: TextIO) -> None:
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), file=file)


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
