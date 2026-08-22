"""Nonsecret credential policy and the broker that fills from it.

Two objects live here, and neither one ever holds a secret value.

`CredentialManifest` parses one fixed policy file,
`<home>/.config/macos-harness/credentials.toml`, where `<home>` is this
uid's own entry in the password database rather than `HOME` -- an
inherited or edited `HOME` would otherwise move both the policy this
process trusts and the vault binary it launches. That file holds strictly
validated, entirely nonsecret rules saying *where* a value comes from --
a password or TOTP seed the shared mem-secret vault holds, or the shape
of a Gmail message carrying a one-time code -- and never the value
itself. There is no path parameter anywhere in this module, so no caller
can point the harness at a manifest of its own, and the fixed one is read
only when it is a regular, non-symlink, mode-0600 file owned by this uid
inside a directory no one else can write. Every entry is validated
eagerly, so a malformed manifest fails once, at the boundary, instead of
half-way through a login.

Every entry has one identity: the SHA-256 digest of its whole canonical
policy -- ref, kind, sorted origins, field selector, and, for a Gmail
entry, its mailbox, sender, patterns, and age bound. Both vault names
this module can ask mem-secret for are derived from that digest, so a
manifest edit alone rebinds nothing: change any source or any destination
and the entry names a different vault key, which stays empty until a
human runs `credential enroll` for the edited policy.

For a password or TOTP seed that derived name is the secret's own vault
key. A Gmail entry stores no secret -- its value exists only in an email
that already arrived -- so its derived name holds a nonsecret
*authorization marker*: the policy digest itself, written by `credential
enroll` and injected back into the worker, which refuses to read a single
message unless the injected marker matches the digest in its job. Editing
the manifest to aim a live Gmail code at another origin, field, or
mailbox therefore fails in the worker instead of being filled.

`CredentialBroker.fill_browser` turns one request into exactly one
short-lived worker subprocess and nothing else. Every kind runs as
`mem-secret run <DERIVED NAME> -- <python> -I <worker script>`, so what
mem-secret injects -- a secret for a vault entry, an authorization marker
for a Gmail one -- reaches only that child's environment: this process
never reads it, never stores it, and has no code path that could. Every
element of that command is an absolute, resolved path, and the
interpreter runs isolated (`-I`), so neither `PATH`, `PYTHONPATH`, nor
the working directory can decide what executes inside the process that
just received it.

The nonsecret job -- one compact JSON object of already-validated policy
plus the caller's taskspace -- goes to the child on stdin, so no policy
value, and certainly no secret, is ever visible in `argv`; the only
manifest-derived string in `argv` is the derived environment variable
*name* mem-secret needs in order to scope its injection.

The broker bounds a fill by kind, since a vault fill waits on no provider
and a Gmail fill waits for mail, and it owns the whole tree it started:
on timeout the worker's session is killed as a group, so nothing that was
handed an injected value outlives its deadline.

The worker's stdout and stderr are attached to `/dev/null`, so provider
prose (a browser error, an email body, a mem-secret diagnostic) cannot be
read back into this process even by accident, and every failure -- a
missing binary, a nonzero exit, a hung child -- collapses to one of the
closed set of fixed codes in `_Code`. What a caller gets back is fixed
vocabulary and nothing else: a five-field `CredentialReceipt`, or a
`CredentialError` whose message comes from the table in this module. No
manifest entry body, secret, one-time code, email body, provider output,
taskspace name, or caller-supplied prose ever reaches a return value, an
exception, `argv`, or this process's own stdout or stderr.

None of this defends against a hostile process running as this same user:
such a process can already invoke `mem-secret` and `gws` itself, which is
the authorization the user granted when they installed them. What it does
defend is policy drift -- a mistaken or injected edit pointing an
enrolled credential at a new origin, field, or mailbox without a human
authorizing that new policy under a new key.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol

from .errors import MacOSError

__all__ = [
    "DEFAULT_CREDENTIAL_MANIFEST",
    "CredentialBroker",
    "CredentialEnrollment",
    "CredentialError",
    "CredentialManifest",
    "CredentialReceipt",
]

#: This account's home directory, straight from the password database.
#: `Path.home()` and `expanduser` consult `HOME` first, so an inherited
#: or rewritten environment could otherwise move both the policy file
#: below and the vault binary this module launches.
_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

#: The only manifest this release reads. Nothing takes a path argument,
#: so this is the whole answer to "which policy is in force".
DEFAULT_CREDENTIAL_MANIFEST = _HOME / ".config" / "macos-harness" / "credentials.toml"

#: The manifest is opened without following a final symlink, and without
#: blocking on a fifo standing in for it -- `_require_trusted` then
#: refuses anything that is not a plain private file of this user's.
_MANIFEST_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_MANIFEST_MODE = 0o600
_FOREIGN_WRITE = stat.S_IWGRP | stat.S_IWOTH

#: The only manifest `version` this release accepts. A newer manifest is
#: rejected rather than partially understood.
_MANIFEST_VERSION = 1

#: The `version` stamped into every job handed to the worker, so the pair
#: can disagree in a later release without either side guessing.
_JOB_VERSION = 1

#: Both halves of the fill command are absolute and resolved, and the
#: interpreter is isolated (`-I` implies `-E` and `-s`, and keeps the
#: script's own directory off `sys.path`): once mem-secret has injected a
#: value into that child, no caller-controlled `PATH`, `PYTHONPATH`, or
#: working directory gets a say in what runs there.
_MEM_SECRET = str(_HOME / ".local" / "bin" / "mem-secret")
_WORKER = (
    sys.executable,
    "-I",
    str(Path(__file__).with_name("_credential_worker.py").resolve()),
)

#: Every vault name is derived from policy, never authored: 128 bits of
#: the entry's policy digest, far past collision reach and unforgeable
#: from a manifest edit alone. The two families cannot collide, because a
#: secret name continues with a hex digit where an authorization name
#: continues with `AUTH_`.
_ENV_PREFIX = "MACOS_HARNESS_CRED_"
_AUTH_ENV_PREFIX = "MACOS_HARNESS_CRED_AUTH_"
_ENV_DIGEST_LENGTH = 32

#: A backstop per kind, not the worker's own deadline: the worker bounds
#: each of its own steps well under these. A vault fill waits on no
#: provider at all; a Gmail fill waits for a message to show up. Blowing
#: either bound is a `credential.timeout`, and the whole worker session
#: is SIGKILLed as one process group -- see `_run_worker`.
_VAULT_TIMEOUT_SECONDS = 45.0
_GMAIL_TIMEOUT_SECONDS = 120.0

#: How long to wait for a SIGKILLed worker session to be reaped.
_REAP_TIMEOUT_SECONDS = 2.0

_MAX_SELECTOR = 256
_MAX_ORIGIN = 255
_MAX_EMAIL = 254
_MAX_PATTERN = 512
_MIN_MAX_AGE_SECONDS = 1
_MAX_MAX_AGE_SECONDS = 3600

_REF_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_EMAIL_ADDRESS = re.compile(
    r"[A-Za-z0-9._%+-]{1,64}@[a-z0-9][a-z0-9.-]{0,251}\.[a-z]{2,24}"
)

# An *exact* origin, in the RFC 6454 serialization a browser reports:
# `https://` plus a lowercase host plus an optional port. The shape alone
# rules out every ambiguity worth rejecting -- a wildcard host, userinfo,
# a path/query/fragment (including a bare trailing slash), a non-HTTPS
# scheme, and mixed case that would let two spellings of one origin
# disagree.
_ORIGIN = re.compile(
    r"https://"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"(?::(?P<port>[0-9]{1,5}))?"
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# A conservative screen over caller-authored regexes. It proves nothing
# about matching cost -- only the worker's own wall-clock bound does that
# -- but the shapes it refuses have no place in an OTP pattern anyway.
# Every catastrophic-backtracking shape worth naming is one group,
# quantified, whose body can match the same text more than one way -- an
# inner `*`/`+`/`?`, or an alternation -- so one pattern catches `(a+)+`,
# `(a?)*`, `(?:a|a)*`, and `(?:a|b){2,}` alike. A backreference makes
# matching cost unpredictable for no benefit. Everything that survives is
# additionally bounded in length and constrained to an exact
# capture-group shape (see `_require_pattern`).
_QUANTIFIED_GROUP = re.compile(r"\([^()]*[*+?|][^()]*\)\s*[*+{]")
_BACKREFERENCE = re.compile(r"\\[1-9]|\(\?P=")

_DOCUMENT_KEYS = frozenset({"version", "credentials"})
_COMMON_KEYS = frozenset({"kind", "origins", "field"})

#: The keys each kind adds to `_COMMON_KEYS`. A vault-backed entry adds
#: none: its one remaining input, the vault name, is derived rather than
#: declared, so `kind`, `origins`, and `field` are the whole entry.
_KIND_KEYS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "password": frozenset(),
        "totp": frozenset(),
        "gmail_otp": frozenset(
            {"mailbox", "sender", "subject_regex", "body_regex", "max_age_seconds"}
        ),
    }
)

_Kind = Literal["password", "totp", "gmail_otp"]
_Provider = Literal["mem-secret", "gmail"]
_PROVIDERS = frozenset({"mem-secret", "gmail"})

#: Every value shape a worker job may carry. Deliberately narrow: a job
#: is validated policy plus one target, never an arbitrary payload.
_JobValue = str | int | list[str]


class _Code(StrEnum):
    """The closed set of failure codes this module can report.

    A code is the whole machine contract of a failure; the matching
    message in `_MESSAGES` is fixed prose that never interpolates
    anything. Nothing outside this module can add a code, so nothing
    outside this module can put text into a `CredentialError`.
    """

    MANIFEST_MISSING = "credential.manifest_missing"
    MANIFEST_UNTRUSTED = "credential.manifest_untrusted"
    MANIFEST_UNREADABLE = "credential.manifest_unreadable"
    MANIFEST_INVALID = "credential.manifest_invalid"
    REF_INVALID = "credential.ref_invalid"
    REF_UNKNOWN = "credential.ref_unknown"
    TARGET_INVALID = "credential.target_invalid"
    WORKER_UNAVAILABLE = "credential.worker_unavailable"
    FILL_FAILED = "credential.fill_failed"
    TIMEOUT = "credential.timeout"
    UNAVAILABLE = "credential.unavailable"


_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        _Code.MANIFEST_MISSING: "No credential manifest is configured",
        _Code.MANIFEST_UNTRUSTED: "The credential manifest is not private to this user",
        _Code.MANIFEST_UNREADABLE: "The credential manifest cannot be read",
        _Code.MANIFEST_INVALID: "The credential manifest is not valid",
        _Code.REF_INVALID: "The credential ref is malformed",
        _Code.REF_UNKNOWN: "No credential is configured for that ref",
        _Code.TARGET_INVALID: "The fill target is malformed",
        _Code.WORKER_UNAVAILABLE: "The credential worker could not be started",
        _Code.FILL_FAILED: "The credential fill did not complete",
        _Code.TIMEOUT: "The credential fill exceeded its time bound",
        _Code.UNAVAILABLE: "The credential broker is unavailable",
    }
)


class CredentialError(MacOSError):
    """A credential failure reduced to one fixed code and one fixed message.

    Deliberately narrower than the `MacOSError` it extends: the only
    constructor argument is a code, the message is looked up from this
    module's closed table, and `details` is always empty. There is no
    parameter through which a secret, a provider's output, a manifest
    entry body, or a caller's own string could reach `str(exc)` or
    `exc.to_json()`.

    A code that is not in the table -- which can only happen if a caller
    outside this module constructs one -- normalizes to
    `credential.unavailable` rather than being echoed back, so even a
    misuse cannot smuggle text through `.code`.
    """

    def __init__(self, code: str = _Code.UNAVAILABLE) -> None:
        fixed = (
            code
            if isinstance(code, str) and code in _MESSAGES
            else _Code.UNAVAILABLE.value
        )
        super().__init__(_MESSAGES[fixed], code=fixed)


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialReceipt:
    """Proof that one configured credential was filled, and nothing more.

    Five fields, and only one of them is an argument: `state`, `acted`,
    and `sink` are constants, `provider` comes from a closed set, and
    `credential_ref` is the caller's own ref, re-validated here so a
    receipt cannot carry prose even if some future caller reaches this
    constructor directly. The receipt says a fill happened -- never what
    was filled, how long it was, where it came from, or what the provider
    said on the way.
    """

    credential_ref: str
    provider: _Provider
    sink: Literal["browser"] = field(default="browser", init=False)
    state: Literal["filled"] = field(default="filled", init=False)
    acted: Literal[True] = field(default=True, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.credential_ref, str) or not _REF_NAME.fullmatch(
            self.credential_ref
        ):
            raise ValueError("credential_ref must be a configured credential ref")
        if self.provider not in _PROVIDERS:
            raise ValueError("provider must be a known credential provider")

    def to_json(self) -> dict[str, str | bool]:
        """Return the fixed, JSON-safe machine contract."""
        return {
            "state": self.state,
            "credential_ref": self.credential_ref,
            "provider": self.provider,
            "sink": self.sink,
            "acted": self.acted,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialEnrollment:
    """What enrolling one ref has to write into the mem-secret vault.

    `env` is the derived, nonsecret vault name to write under. `marker`
    is the value when *policy*, not the operator, decides it: a Gmail
    entry's authorization marker is its own policy digest, so enrolling
    one records a human's authorization rather than storing a secret
    anybody typed. A `marker` of `None` means the value can only come
    from the operator, and is the only case in which reading a secret
    from stdin or the pasteboard means anything.
    """

    env: str
    marker: str | None = None


def _require_line(value: object, *, limit: int) -> str:
    """One nonempty, bounded, control-character-free line of text."""
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise CredentialError(_Code.MANIFEST_INVALID)
    if _CONTROL.search(value) is not None:
        raise CredentialError(_Code.MANIFEST_INVALID)
    return value


def _require_trimmed(value: object, *, limit: int) -> str:
    """`_require_line` that also refuses surrounding whitespace.

    Padding is never meaningful in an origin, selector, or address, and
    two spellings of one value that differ only by padding are exactly
    the kind of ambiguity this manifest exists to rule out.
    """
    text = _require_line(value, limit=limit)
    if text.strip() != text:
        raise CredentialError(_Code.MANIFEST_INVALID)
    return text


def _require_selector(value: object) -> str:
    """One field selector that can only ever mean one field.

    A `*` or a `,` would let the selector match a set of fields, and a
    fill with more than one candidate field is ambiguous by construction
    -- there is no safe way to pick.
    """
    selector = _require_trimmed(value, limit=_MAX_SELECTOR)
    if "*" in selector or "," in selector:
        raise CredentialError(_Code.MANIFEST_INVALID)
    return selector


def _require_origin(value: object) -> str:
    origin = _require_trimmed(value, limit=_MAX_ORIGIN)
    matched = _ORIGIN.fullmatch(origin)
    if matched is None:
        raise CredentialError(_Code.MANIFEST_INVALID)
    port = matched.group("port")
    if port is not None:
        number = int(port)
        # 443 is HTTPS's default, so `https://host:443` and
        # `https://host` are the same origin spelled two ways.
        if not 0 < number <= 65535 or number == 443:
            raise CredentialError(_Code.MANIFEST_INVALID)
    return origin


def _require_origins(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise CredentialError(_Code.MANIFEST_INVALID)
    origins = tuple(_require_origin(item) for item in value)
    if len(set(origins)) != len(origins):
        raise CredentialError(_Code.MANIFEST_INVALID)
    return origins


def _require_email(value: object) -> str:
    address = _require_trimmed(value, limit=_MAX_EMAIL)
    if _EMAIL_ADDRESS.fullmatch(address) is None:
        raise CredentialError(_Code.MANIFEST_INVALID)
    return address


def _require_pattern(value: object, *, code_group: bool) -> str:
    """One bounded, compilable regex with an exact capture-group shape.

    A body pattern must capture the code in exactly one group named
    `code`, so extraction is unambiguous; a subject pattern is a filter
    and must capture nothing at all. Both are screened for the
    pathological shapes in `_QUANTIFIED_GROUP`/`_BACKREFERENCE` before
    they are ever compiled, and the worker that runs them holds a
    wall-clock bound over the match itself.
    """
    pattern = _require_line(value, limit=_MAX_PATTERN)
    if (
        _QUANTIFIED_GROUP.search(pattern) is not None
        or _BACKREFERENCE.search(pattern) is not None
    ):
        raise CredentialError(_Code.MANIFEST_INVALID)
    try:
        compiled = re.compile(pattern)
    except re.error:
        raise CredentialError(_Code.MANIFEST_INVALID) from None
    expected = {"code"} if code_group else set()
    if set(compiled.groupindex) != expected or compiled.groups != len(expected):
        raise CredentialError(_Code.MANIFEST_INVALID)
    return pattern


def _require_ref(value: object) -> str:
    if not isinstance(value, str) or _REF_NAME.fullmatch(value) is None:
        raise CredentialError(_Code.REF_INVALID)
    return value


def _require_space(value: object) -> str:
    if not isinstance(value, str) or _SPACE_NAME.fullmatch(value) is None:
        raise CredentialError(_Code.TARGET_INVALID)
    return value


def _require_kind(value: object) -> _Kind:
    """The one kind this entry declares, as a closed literal."""
    if value == "password":
        return "password"
    if value == "totp":
        return "totp"
    if value == "gmail_otp":
        return "gmail_otp"
    raise CredentialError(_Code.MANIFEST_INVALID)


def _policy_digest(
    *,
    ref: str,
    kind: _Kind,
    origins: tuple[str, ...],
    field_selector: str,
    source: Mapping[str, _JobValue],
) -> str:
    """This entry's identity: a digest over its whole canonical policy.

    Everything the entry declares is an input -- where the value comes
    from *and* where it is allowed to go -- so no manifest edit can
    rebind a vault name to a policy nobody authorized: touch the ref, the
    kind, the origins, the field, or any Gmail source field, and the
    entry names a key that has never been enrolled. Origins are sorted,
    so tidying a list is free, and the entry-wide keys are written last,
    so a source can never shadow one.
    """
    policy = json.dumps(
        {
            **source,
            "ref": ref,
            "kind": kind,
            "origins": sorted(origins),
            "field": field_selector,
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(policy.encode("utf-8")).hexdigest()


def _vault_env(digest: str) -> str:
    """The vault name a secret enrolled for this policy is stored under."""
    return _ENV_PREFIX + digest[:_ENV_DIGEST_LENGTH].upper()


def _auth_env(digest: str) -> str:
    """The vault name this policy's authorization marker is stored under."""
    return _AUTH_ENV_PREFIX + digest[:_ENV_DIGEST_LENGTH].upper()


class _Source(Protocol):
    """The kind-specific half of a manifest entry.

    Polymorphic on purpose: the broker asks a source what policy the
    worker needs, which vault name mem-secret must scope, and what an
    enrollment writes -- so no code path downstream of parsing ever
    dispatches on `kind` again. A source derives names from its entry's
    policy digest rather than storing it, which is what lets that digest
    be computed over `source_policy()` in the first place.
    """

    provider: ClassVar[_Provider]
    timeout_seconds: ClassVar[float]

    def source_policy(self) -> dict[str, _JobValue]: ...

    def env_names(self, digest: str) -> tuple[str, ...]: ...

    def job_auth(self, digest: str) -> dict[str, _JobValue]: ...

    def enrollment(self, digest: str) -> CredentialEnrollment: ...


class _VaultSource:
    """A password or TOTP seed the shared mem-secret vault already holds.

    Declares nothing, and so is one shared instance: an entry's kind,
    origins, and field are its whole policy, and the vault key they
    derive is the only thing this source has to explain.
    """

    __slots__ = ()

    provider: ClassVar[_Provider] = "mem-secret"
    timeout_seconds: ClassVar[float] = _VAULT_TIMEOUT_SECONDS

    def source_policy(self) -> dict[str, _JobValue]:
        return {}

    def env_names(self, digest: str) -> tuple[str, ...]:
        return (_vault_env(digest),)

    def job_auth(self, digest: str) -> dict[str, _JobValue]:
        return {"secret_env": _vault_env(digest)}

    def enrollment(self, digest: str) -> CredentialEnrollment:
        return CredentialEnrollment(env=_vault_env(digest))


_VAULT_SOURCE = _VaultSource()


@dataclass(frozen=True, slots=True, kw_only=True)
class _GmailSource:
    """A one-time code read live out of a configured Gmail mailbox.

    No secret is ever enrolled for one of these: the value exists only in
    an email that already arrived. What its derived vault name holds
    instead is the nonsecret authorization marker -- this entry's own
    policy digest -- which mem-secret injects and the worker compares
    against the digest in its job before reading a single message, so a
    rebound mailbox, origin, or field fails there instead of being
    filled.
    """

    provider: ClassVar[_Provider] = "gmail"
    timeout_seconds: ClassVar[float] = _GMAIL_TIMEOUT_SECONDS

    mailbox: str
    sender: str
    subject_regex: str
    body_regex: str
    max_age_seconds: int

    def source_policy(self) -> dict[str, _JobValue]:
        return {
            "mailbox": self.mailbox,
            "sender": self.sender,
            "subject_regex": self.subject_regex,
            "body_regex": self.body_regex,
            "max_age_seconds": self.max_age_seconds,
        }

    def env_names(self, digest: str) -> tuple[str, ...]:
        return (_auth_env(digest),)

    def job_auth(self, digest: str) -> dict[str, _JobValue]:
        return {"auth_env": _auth_env(digest), "policy_digest": digest}

    def enrollment(self, digest: str) -> CredentialEnrollment:
        return CredentialEnrollment(env=_auth_env(digest), marker=digest)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Entry:
    """One fully validated, entirely nonsecret manifest entry.

    `policy_digest` is this entry's identity, computed once over
    everything it declares: every vault name it can reach comes from
    there, and nothing else does.
    """

    kind: _Kind
    origins: tuple[str, ...]
    field_selector: str
    source: _Source
    policy_digest: str


def _parse_gmail(table: Mapping[str, object]) -> _GmailSource:
    max_age = table.get("max_age_seconds")
    if isinstance(max_age, bool) or not isinstance(max_age, int):
        raise CredentialError(_Code.MANIFEST_INVALID)
    if not _MIN_MAX_AGE_SECONDS <= max_age <= _MAX_MAX_AGE_SECONDS:
        raise CredentialError(_Code.MANIFEST_INVALID)
    return _GmailSource(
        mailbox=_require_email(table.get("mailbox")),
        sender=_require_email(table.get("sender")),
        subject_regex=_require_pattern(table.get("subject_regex"), code_group=False),
        body_regex=_require_pattern(table.get("body_regex"), code_group=True),
        max_age_seconds=max_age,
    )


def _parse_entry(ref: str, table: Mapping[str, object]) -> _Entry:
    kind = _require_kind(table.get("kind"))
    # Closed key set per kind: an unknown key, or a key that belongs to a
    # different kind, is a manifest the author did not mean to write.
    if not set(table) <= (_COMMON_KEYS | _KIND_KEYS[kind]):
        raise CredentialError(_Code.MANIFEST_INVALID)
    origins = _require_origins(table.get("origins"))
    selector = _require_selector(table.get("field"))
    # The last time anything in this module looks at `kind`.
    source: _Source = _parse_gmail(table) if kind == "gmail_otp" else _VAULT_SOURCE
    return _Entry(
        kind=kind,
        origins=origins,
        field_selector=selector,
        source=source,
        policy_digest=_policy_digest(
            ref=ref,
            kind=kind,
            origins=origins,
            field_selector=selector,
            source=source.source_policy(),
        ),
    )


def _parse_document(document: Mapping[str, object]) -> dict[str, _Entry]:
    if not set(document) <= _DOCUMENT_KEYS or "version" not in document:
        raise CredentialError(_Code.MANIFEST_INVALID)
    version = document["version"]
    if isinstance(version, bool) or version != _MANIFEST_VERSION:
        raise CredentialError(_Code.MANIFEST_INVALID)
    credentials = document.get("credentials", {})
    if not isinstance(credentials, dict):
        raise CredentialError(_Code.MANIFEST_INVALID)
    entries: dict[str, _Entry] = {}
    for ref, table in credentials.items():
        if _REF_NAME.fullmatch(ref) is None or not isinstance(table, dict):
            raise CredentialError(_Code.MANIFEST_INVALID)
        entries[ref] = _parse_entry(ref, table)
    return entries


def _require_trusted(location: Path, info: os.stat_result) -> None:
    """Refuse a manifest that is not this user's own private file.

    Another uid's file, a group- or world-accessible one, a directory or
    device standing in for it, or a file inside a directory someone else
    can write could all have been substituted or read by another process
    -- so none of them is policy this harness will act on. `os.stat` on
    the parent is the only path-based check left, and it is the directory
    the already-open file was found in.
    """
    uid = os.getuid()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
        raise CredentialError(_Code.MANIFEST_UNTRUSTED)
    if stat.S_IMODE(info.st_mode) != _MANIFEST_MODE:
        raise CredentialError(_Code.MANIFEST_UNTRUSTED)
    parent = os.stat(location.parent)
    if parent.st_uid != uid or stat.S_IMODE(parent.st_mode) & _FOREIGN_WRITE:
        raise CredentialError(_Code.MANIFEST_UNTRUSTED)


def _read_trusted(location: Path) -> bytes:
    """The manifest's bytes, read from a descriptor proven to be its own.

    Ownership and mode are checked with `fstat` on the open descriptor
    rather than `stat` on the path, so what is validated and what is read
    are the same file even if the path is replaced in between.
    """
    try:
        handle = os.open(location, _MANIFEST_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        raise CredentialError(_Code.MANIFEST_MISSING) from None
    except OSError as exc:
        # A symlink at the final component is exactly the substitution
        # `O_NOFOLLOW` exists to refuse; anything else is ordinary I/O.
        raise CredentialError(
            _Code.MANIFEST_UNTRUSTED
            if exc.errno == errno.ELOOP
            else _Code.MANIFEST_UNREADABLE
        ) from None
    try:
        _require_trusted(location, os.fstat(handle))
        with open(handle, "rb", closefd=False) as stream:
            return stream.read()
    except OSError:
        raise CredentialError(_Code.MANIFEST_UNREADABLE) from None
    finally:
        os.close(handle)


class CredentialManifest:
    """The parsed, validated, entirely nonsecret credential policy file.

    Entries stay private. The only things this exposes are the ref names
    a caller already knows (`refs`) and, for one ref at a time, the
    `CredentialEnrollment` an enrollment has to write -- a derived vault
    name, and the policy marker when policy rather than an operator
    decides the value. Both are nonsecret, and both are what `mem-secret
    set` needs. No accessor returns an entry's origins, selectors,
    mailbox, or patterns, so a manifest cannot be read back out through
    this object.
    """

    __slots__ = ("_entries",)

    def __init__(self, entries: Mapping[str, _Entry]) -> None:
        """Wrap already-parsed entries; callers use `load` instead."""
        self._entries: Mapping[str, _Entry] = MappingProxyType(dict(entries))

    @classmethod
    def load(cls) -> CredentialManifest:
        """Read and fully validate the fixed manifest, or raise.

        Takes no path: `DEFAULT_CREDENTIAL_MANIFEST` is the only policy
        file this harness will ever act on, so there is no argument here
        through which a caller could substitute another one.
        """
        return cls._parse(_read_trusted(DEFAULT_CREDENTIAL_MANIFEST))

    @classmethod
    def _parse(cls, raw: bytes) -> CredentialManifest:
        """Validate manifest bytes `_read_trusted` has already vouched for."""
        try:
            document = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise CredentialError(_Code.MANIFEST_INVALID) from None
        return cls(_parse_document(document))

    @property
    def refs(self) -> tuple[str, ...]:
        """Every configured ref, sorted, and nothing else about them."""
        return tuple(sorted(self._entries))

    def enrollment(self, ref: str) -> CredentialEnrollment:
        """What enrolling `ref` has to write into the vault.

        Raises `CredentialError` for a malformed or unconfigured ref.
        """
        entry = self._require(ref)
        return entry.source.enrollment(entry.policy_digest)

    def _require(self, ref: str) -> _Entry:
        entry = self._entries.get(_require_ref(ref))
        if entry is None:
            raise CredentialError(_Code.REF_UNKNOWN)
        return entry

    def __repr__(self) -> str:
        return f"CredentialManifest({len(self._entries)} refs)"


class _WorkerRunner(Protocol):
    """Spawn one worker, feed it `job` on stdin, and return its exit status.

    Raises `OSError` when the process cannot be started at all and
    `subprocess.TimeoutExpired` when it outlives `timeout`; the broker
    maps both, and anything else, to a fixed code.
    """

    def __call__(self, command: list[str], *, job: bytes, timeout: float) -> int: ...


class _PolicyLoader(Protocol):
    """Return the policy in force right now, or raise `CredentialError`."""

    def __call__(self) -> CredentialManifest: ...


def _kill_session(process: subprocess.Popen[bytes]) -> None:
    """SIGKILL a wedged worker's whole session, then reap it.

    The session, not just the child: the direct child is always
    `mem-secret`, and the worker holding the injected value in its
    environment is *its* child, which may in turn have started `gws` or
    an ego utility. Killing only the parent would leave those running
    with that value live, which is exactly what a timeout has to prevent.
    `_run_worker` spawns with `start_new_session=True`, so this one group
    id covers every descendant and nothing below sets up a group of its
    own to escape into.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except OSError:
        process.kill()
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _run_worker(command: list[str], *, job: bytes, timeout: float) -> int:
    """The real runner: one worker, job on stdin, output to `/dev/null`.

    `stdout`/`stderr` go to `/dev/null` at the OS level rather than into
    a pipe, so there is no buffer in this process for a secret, a
    one-time code, an email body, or a provider's prose to land in --
    not even long enough to be logged by mistake. `stdin` is the one
    channel, and it carries only the nonsecret job.
    """
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.communicate(input=job, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_session(process)
        raise
    return process.returncode


def _command(entry: _Entry) -> list[str]:
    """The exact argv for one fill.

    Every kind is wrapped in `mem-secret run <name> --`, whose only
    manifest-derived argument is this entry's own derived vault name --
    never a value, and never a name belonging to another entry.
    """
    names = entry.source.env_names(entry.policy_digest)
    return [_MEM_SECRET, "run", *names, "--", *_WORKER]


def _job(entry: _Entry, *, space: str) -> dict[str, _JobValue]:
    """The nonsecret job the worker reads on stdin."""
    job: dict[str, _JobValue] = {
        "version": _JOB_VERSION,
        "kind": entry.kind,
        "space": space,
        "origins": list(entry.origins),
        "field": entry.field_selector,
    }
    job.update(entry.source.source_policy())
    job.update(entry.source.job_auth(entry.policy_digest))
    return job


class CredentialBroker:
    """Fills one configured credential by launching one worker subprocess.

    The broker validates policy and the caller's target, then gets out of
    the way: it never reads a secret, never talks to a browser itself,
    and never inspects what the worker did beyond that worker's exit
    status. Every failure -- an untrusted or unreadable manifest, an
    unknown ref, a malformed target, a missing `mem-secret`, a nonzero
    exit, a hung child -- surfaces as a `CredentialError` carrying one
    fixed code.

    Nothing is cached between calls. Policy is loaded and re-validated on
    every `check` and every `fill_browser`, so a manifest edited or
    revoked a moment ago governs the next fill instead of a snapshot
    taken when this object happened to be built.

    The two underscore-prefixed constructor arguments are this repo's own
    test seams -- the worker runner and the policy loader -- and neither
    widens what an in-process caller can do. Substituted policy still
    only names vault keys derived from itself, and a key nobody enrolled
    is empty; anyone who can pass either argument already decides what
    this process executes.
    """

    __slots__ = ("_policy", "_run")

    def __init__(
        self,
        *,
        _run: _WorkerRunner = _run_worker,
        _policy: _PolicyLoader = CredentialManifest.load,
    ) -> None:
        self._run = _run
        self._policy = _policy

    def check(self) -> tuple[str, ...]:
        """Every configured ref, sorted."""
        return self._policy().refs

    def fill_browser(self, ref: str, *, space: str) -> CredentialReceipt:
        """Fill `ref` into a field in a live ego-browser taskspace."""
        validated = _require_ref(ref)
        target = _require_space(space)
        entry = self._policy()._require(validated)
        job = json.dumps(
            _job(entry, space=target),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if self._invoke(_command(entry), job, entry.source.timeout_seconds) != 0:
            raise CredentialError(_Code.FILL_FAILED)
        return CredentialReceipt(
            credential_ref=validated,
            provider=entry.source.provider,
        )

    def _invoke(self, command: list[str], job: bytes, timeout: float) -> int:
        """Run the worker, collapsing every possible failure to a fixed code.

        The blanket `except Exception` is the point, not sloppiness: this
        is the redaction boundary, and a runner that raises something
        unforeseen must not be able to carry its message -- which could
        quote a command, an environment, or a provider's output -- past
        here. `from None` keeps the original exception out of the chained
        traceback for the same reason.
        """
        try:
            return self._run(command, job=job, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise CredentialError(_Code.TIMEOUT) from None
        except OSError:
            raise CredentialError(_Code.WORKER_UNAVAILABLE) from None
        except Exception:  # noqa: BLE001 - the redaction boundary: no message escapes
            raise CredentialError(_Code.UNAVAILABLE) from None
