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
    from .credentials import (
        DEFAULT_CREDENTIAL_MANIFEST,
        CredentialBroker,
        CredentialEnrollment,
        CredentialError,
        CredentialManifest,
        CredentialReceipt,
    )
    from .macos import MacOS, SearchMatches
    from .routes import RouteResult

# Import the native runtime and credential policy only when their public
# exports are requested. Type checkers use the imports above.
_CREDENTIAL_NAMES = frozenset(
    {
        "DEFAULT_CREDENTIAL_MANIFEST",
        "CredentialBroker",
        "CredentialEnrollment",
        "CredentialError",
        "CredentialManifest",
        "CredentialReceipt",
    }
)


def __getattr__(name: str) -> object:
    if name in {"MacOS", "SearchMatches"}:
        from . import macos

        value = getattr(macos, name)
    elif name == "RouteResult":
        from .routes import RouteResult

        value = RouteResult
    elif name in _CREDENTIAL_NAMES:
        from . import credentials

        value = getattr(credentials, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(globals().keys() | {"MacOS", "SearchMatches", "RouteResult"})


__all__ = [
    "DEFAULT_CREDENTIAL_MANIFEST",
    "AccessibilityPermissionError",
    "Acted",
    "ApplicationNotFoundError",
    "BrowserHarness",
    "CredentialBroker",
    "CredentialEnrollment",
    "CredentialError",
    "CredentialManifest",
    "CredentialReceipt",
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
