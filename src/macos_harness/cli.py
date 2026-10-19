"""One Python execution surface for the browser, macOS, and local files."""

from __future__ import annotations

import argparse
import code
import difflib
import inspect
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import TextIO

from ._version import __version__
from .browser import BrowserHarness
from .errors import ErrorCode, MacOSError
from .receipts import OperationError
from .telemetry import capture_cli
from .telemetry import run_cli as run_telemetry_cli


@contextmanager
def _namespace() -> Iterator[dict[str, object]]:
    from .macos import MacOS

    with MacOS() as mac:
        yield {
            "__name__": "__macos_harness__",
            "browser": BrowserHarness(),
            "mac": mac,
            "Path": Path,
            "subprocess": subprocess,
        }


# A call with the wrong arguments raises before its body runs, naming the
# method by qualified name: "MacOS.scroll() got an unexpected keyword ...".
_BAD_CALL = re.compile(r"(\w+)\.(\w+)\(\) ")
# Its unknown keyword, and the hint Python 3.13+ appends by spelling alone.
_UNKNOWN_KEYWORD = re.compile(r"unexpected keyword argument '(\w+)'(?:\. Did you mean '\w+'\?)?$")


def _mac_surfaces(namespace: Mapping[str, object]) -> dict[str, object]:
    """The preloaded `mac` and the two namespaces programs call through it."""
    from .macos import MacOS

    mac = namespace.get("mac")
    if not isinstance(mac, MacOS):
        return {}
    return {"mac": mac, "mac.ax": mac.ax, "mac.do": mac.do}


def _plain_signature(method: Callable[..., object]) -> str:
    """``method``'s parameters as a call spells them, unannotated."""
    signature = inspect.signature(method)
    return str(
        signature.replace(
            parameters=[
                parameter.replace(annotation=parameter.empty)
                for parameter in signature.parameters.values()
            ],
            return_annotation=signature.empty,
        )
    )


def _abbreviated(keyword: str, parameters: Iterable[str]) -> str | None:
    """The one parameter whose words' initials spell ``keyword``, as
    ``delta_y`` does ``dy``; Python's own hint goes by spelling and offers
    ``y``."""
    found = [
        name for name in parameters
        if "_" in name and "".join(word[:1] for word in name.split("_")) == keyword
    ]
    return found[0] if len(found) == 1 else None


def _explain_bad_call(exc: TypeError, namespace: Mapping[str, object]) -> None:
    """End a misused harness call's error with the method's real signature,
    and point an abbreviated keyword at the parameter it stands for."""
    call = _BAD_CALL.match(str(exc))
    if call is None or call[2].startswith("_"):
        return
    surfaces = {**_mac_surfaces(namespace), "browser": namespace.get("browser")}
    for label, surface in surfaces.items():
        if type(surface).__name__ != call[1]:
            continue
        method = getattr(surface, call[2], None)
        if not callable(method):
            return
        unknown = _UNKNOWN_KEYWORD.search(str(exc))
        if unknown is not None:
            meant = _abbreviated(unknown[1], inspect.signature(method).parameters)
            if meant is not None:
                exc.args = (f"{str(exc)[: unknown.end(1) + 1]}. Did you mean '{meant}'?",)
        exc.add_note(f"Signature: {label}.{call[2]}{_plain_signature(method)}")
        return


def _nearness(missing: str, name: str) -> float:
    """How likely ``name`` is the call ``missing`` meant: a name one word
    apart from it ranks just below the same name, and any other by how
    alike the two are spelled."""
    if missing.startswith(f"{name}_") or name.startswith(f"{missing}_"):
        return 0.95
    return difflib.SequenceMatcher(None, missing, name).ratio()


def _call_text(label: str, surface: object, name: str) -> str:
    """``label.name``, with its parameters when it is a method; a property
    is named without being read."""
    raw = inspect.getattr_static(surface, name)
    if inspect.isfunction(raw) or isinstance(raw, (staticmethod, classmethod)):
        return f"{label}.{name}{_plain_signature(getattr(surface, name))}"
    return f"{label}.{name}"


def _point_at_nearest(exc: AttributeError, namespace: Mapping[str, object]) -> None:
    """End an error for a name `mac`, `mac.ax` or `mac.do` lacks with the
    nearest calls across all three, as they are called."""
    surfaces = _mac_surfaces(namespace)
    owner = next((label for label, surface in surfaces.items() if exc.obj is surface), None)
    if owner is None or not exc.name:
        return
    ranked = sorted(
        (-_nearness(exc.name, name), label != owner, label, name)
        for label, surface in surfaces.items()
        for name in dir(surface)
        if not name.startswith("_")
    )
    nearest = [
        _call_text(label, surfaces[label], name)
        for score, _elsewhere, label, name in ranked[:3]
        if -score >= 0.6
    ]
    if nearest:
        exc.add_note("Nearest calls:\n" + "\n".join(f"  {call}" for call in nearest))


def _execute(code: str) -> int:
    if not code.strip():
        print("No Python code received on stdin", file=sys.stderr)
        return 2
    with _namespace() as namespace:
        try:
            exec(compile(code, "<macos-harness>", "exec"), namespace, namespace)  # noqa: S102
        except TypeError as exc:
            _explain_bad_call(exc, namespace)
            raise
        except AttributeError as exc:
            _point_at_nearest(exc, namespace)
            raise
    return 0


def _skill_text(reference: str | None) -> str:
    root = resources.files("macos_harness").joinpath("skill")
    if not root.joinpath("SKILL.md").is_file():
        root = Path(__file__).resolve().parents[2] / "skills/macos-harness"
    if reference is None:
        return root.joinpath("SKILL.md").read_text(encoding="utf-8")
    references = root.joinpath("references")
    page = references.joinpath(f"{reference.removesuffix('.md')}.md")
    if not page.is_file():
        names = sorted(item.name.removesuffix(".md") for item in references.iterdir())
        raise MacOSError(
            f"no skill reference {reference!r}; choose one of: {', '.join(names)}",
            code=ErrorCode.BAD_REQUEST,
        )
    return page.read_text(encoding="utf-8")


def _app_query(value: str) -> str | int:
    """An app argument as `MacOS` reads it: all digits is a pid."""
    return int(value) if value.isascii() and value.isdigit() else value


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
    skill = subparsers.add_parser("skill", help="print the macOS Harness skill card or one reference")
    skill.add_argument("reference", nargs="?", help="a reference name, such as signatures")
    telemetry = subparsers.add_parser(
        "telemetry", help="inspect or change telemetry (off by default)"
    )
    telemetry.add_argument("action", nargs="?", choices=("status", "enable", "disable"))
    see = subparsers.add_parser(
        "see", help="capture an app's window, or any window by --window-id"
    )
    see.add_argument(
        "app", nargs="?", type=_app_query, help="an app name, bundle ID, path, or pid"
    )
    see.add_argument("--path", help="write the PNG here instead of a temporary file")
    see.add_argument("--window-index", type=int, default=0)
    see.add_argument(
        "--window-id", type=int, help="any window, panel, or menu from mac.windows()"
    )
    see.add_argument("--max-width", type=int, default=1280)
    see.add_argument("--max-height", type=int, default=1280)
    see.add_argument(
        "--pointer", action="store_true", help="draw the virtual pointer onto the image"
    )
    state = subparsers.add_parser(
        "state", help="print an application's AX state as JSON"
    )
    state.add_argument("app", type=_app_query)
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

            with MacOS() as mac:
                if args.request:
                    mac.request_permissions()
                print(json.dumps(mac.doctor(), indent=2))
            result = 0
            return result
        if args.command == "apps":
            from .macos import MacOS

            with MacOS() as mac:
                print(json.dumps(mac.list_apps(), indent=2))
            result = 0
            return result
        if args.command == "skill":
            print(_skill_text(args.reference), end="")
            result = 0
            return result
        if args.command == "repl":
            with _namespace() as namespace:
                code.interact(
                    banner=(
                        "macos-harness: mac.see/key/type/click/ax/script, browser, "
                        "Path, and subprocess are ready"
                    ),
                    local=namespace,
                    exitmsg="",
                )
            result = 0
            return result
        if args.command == "see":
            from .macos import MacOS

            with MacOS() as mac:
                result = mac.see(
                    args.app,
                    window_index=args.window_index,
                    window_id=args.window_id,
                    path=args.path,
                    max_width=args.max_width,
                    max_height=args.max_height,
                    show_pointer=args.pointer,
                )
            print(json.dumps(result, indent=2, ensure_ascii=False))
            result = 0
            return result
        if args.command == "state":
            from .macos import MacOS

            with MacOS() as mac:
                state = mac.get_app_state(
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
