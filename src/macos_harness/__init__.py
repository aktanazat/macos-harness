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
from .macos import MacOS, SearchMatches
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
from .routes import RouteResult

if TYPE_CHECKING:
    from .credentials import (
        DEFAULT_CREDENTIAL_MANIFEST,
        CredentialBroker,
        CredentialEnrollment,
        CredentialError,
        CredentialManifest,
        CredentialReceipt,
    )

# `credentials` is the only module here that parses TOML and shells out to
# the vault, and nothing but the credential commands ever touches it, so
# it is imported on first use rather than at package import. Type checkers
# read the block above; `from macos_harness import CredentialBroker` still
# works, because that is a `getattr` on this module.
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
    if name not in _CREDENTIAL_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import credentials

    value = getattr(credentials, name)
    globals()[name] = value
    return value


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
