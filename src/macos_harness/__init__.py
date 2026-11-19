"""Public Python interface for macOS Harness."""

from typing import TYPE_CHECKING

from ._version import __version__

if TYPE_CHECKING:
    from .browser import BrowserHarness
    from .errors import (
        AccessibilityPermissionError,
        ApplicationNotFoundError,
        ErrorCode,
        FocusChangedError,
        MacOSError,
    )
    from .handoff import HandoffReason, HumanHandoff
    from .receipts import (
        Acted,
        Equals,
        ErrorPayload,
        Executor,
        Gone,
        JSONValue,
        Observation,
        OperationError,
        Outcome,
        Postcondition,
        Present,
        Receipt,
        canonical_json,
        canonicalize,
        equals,
        gone,
        present,
        request_fingerprint,
    )
    from .steps import StepsResult

if TYPE_CHECKING:
    from .macos import MacOS, SearchMatches
    from .routes import RouteResult
    from .watch import RouteWatch, WatchResult

_EXPORTS = {
    **dict.fromkeys(("BrowserHarness",), "browser"),
    **dict.fromkeys(
        (
            "AccessibilityPermissionError",
            "ApplicationNotFoundError",
            "ErrorCode",
            "FocusChangedError",
            "MacOSError",
        ),
        "errors",
    ),
    **dict.fromkeys(("HandoffReason", "HumanHandoff"), "handoff"),
    **dict.fromkeys(
        (
            "Acted",
            "Equals",
            "ErrorPayload",
            "Executor",
            "Gone",
            "JSONValue",
            "Observation",
            "OperationError",
            "Outcome",
            "Postcondition",
            "Present",
            "Receipt",
            "canonical_json",
            "canonicalize",
            "equals",
            "gone",
            "present",
            "request_fingerprint",
        ),
        "receipts",
    ),
    "StepsResult": "steps",
}


def __getattr__(name: str) -> object:
    if name in _EXPORTS:
        from importlib import import_module

        value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    elif name in {"MacOS", "SearchMatches"}:
        from . import macos

        value = getattr(macos, name)
    elif name == "RouteResult":
        from .routes import RouteResult

        value = RouteResult
    elif name in {"RouteWatch", "WatchResult"}:
        from . import watch

        value = getattr(watch, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(globals().keys() | set(__all__))


__all__ = [
    "AccessibilityPermissionError",
    "Acted",
    "ApplicationNotFoundError",
    "BrowserHarness",
    "Equals",
    "ErrorCode",
    "ErrorPayload",
    "Executor",
    "FocusChangedError",
    "Gone",
    "HandoffReason",
    "HumanHandoff",
    "JSONValue",
    "MacOS",
    "MacOSError",
    "Observation",
    "OperationError",
    "Outcome",
    "Postcondition",
    "Present",
    "Receipt",
    "RouteResult",
    "RouteWatch",
    "SearchMatches",
    "StepsResult",
    "WatchResult",
    "__version__",
    "canonical_json",
    "canonicalize",
    "equals",
    "gone",
    "present",
    "request_fingerprint",
]
