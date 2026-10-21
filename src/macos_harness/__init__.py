"""Public Python interface for macOS Harness."""

from typing import TYPE_CHECKING

from ._version import __version__
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

if TYPE_CHECKING:
    from .macos import MacOS, SearchMatches
    from .routes import RouteResult

# Import the native runtime only when its public exports are requested.
# Type checkers use the imports above.


def __getattr__(name: str) -> object:
    if name in {"MacOS", "SearchMatches"}:
        from . import macos

        value = getattr(macos, name)
    elif name == "RouteResult":
        from .routes import RouteResult

        value = RouteResult
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(globals().keys() | {"MacOS", "SearchMatches", "RouteResult"})


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
    "SearchMatches",
    "__version__",
    "canonical_json",
    "canonicalize",
    "equals",
    "gone",
    "present",
    "request_fingerprint",
]
