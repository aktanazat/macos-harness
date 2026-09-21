"""The one process that ever holds a provisioned-credential value.

``mem-secret`` -- the engram sops+age vault the broker
(``credentials.py``) delegates to -- has no ``get`` subcommand by design:
the only way to ever see a stored value is to *be* the subprocess
``mem-secret run VAR -- CMD`` execs directly, with the value injected
into that one subprocess's own environment. This module is that
subprocess.

The broker spawns it as an isolated script (``python -I <abs path>``),
which is why nothing here may import a sibling module: isolated mode
drops the script's own directory from ``sys.path`` along with
``PYTHONPATH``, ``PYTHONHOME`` and user site-packages. That is the
point -- nothing the caller's environment says can shadow a module
inside the process that just received the vault secret -- so every
import below is stdlib and absolute.

Contract with the broker (the broker owns the other side):

* Argv carries no job data, so there is no argv-shaped channel a secret
  could ever leak through. One compact UTF-8 JSON object arrives on file
  descriptor 0, closed once written; `_read_job` reads it to EOF.
* A password or TOTP secret reaches this process only by already sitting
  in `os.environ` under the broker-derived name the job names in
  ``secret_env``. `_take_secret` *pops* it, so that name is gone from
  this process's environment before any child is spawned. It is not
  handed on that way either: ego's ``nodejs`` runtime does not inherit
  custom environment variables from the process that invokes it, so the
  value crosses into the child through a one-use FIFO whose *path* is
  all the script ever names -- and for a TOTP entry what crosses is the
  six-digit code, never the seed.
* A ``gmail_otp`` job has no vault secret, but it still runs under
  ``mem-secret``: `_authorize_gmail` pops the enrolled authorization
  marker and requires it to equal the policy digest the job carries, so
  editing the manifest alone cannot repoint a live Gmail credential.
* Exit 0 means the fill genuinely completed -- every intended field held
  exactly the intended value, on the intended document, at the end.
  Every other outcome is a nonzero exit out of the closed `_EXITS` table
  naming *which stage* refused, and nothing at all on stdout or stderr:
  `main` never prints, and both of the children it spawns have their own
  output sent to ``/dev/null``, so there is no diagnostic channel a
  secret, a one-time code, an email body, or a child's own chatter could
  leak through. A stage name is a keyword compiled into this file, never
  anything a run observed, which is what makes saying it free.
* An entry may name more than one field. They are filled in the order
  the job lists them, inside one bounded window, on one document proven
  not to have changed between them -- and the digest that authorized the
  entry covered that whole ordered list, so filling the set is exactly
  what was enrolled.

The boundary this design does *not* claim: another process running as
this same user is not an adversary it can exclude. Such a process can
already invoke ``mem-secret`` and ``gws`` itself. What is defended here
is everything else -- a mistaken or hostile *instruction*, a rebound
``PATH`` or ``HOME``, an untrusted page or email, an accidental log, a
stale policy, a wedged child, and a value delivered to the wrong place.

Everything below the job-parsing boundary is ordinary dependency
injection, not test-only scaffolding: `main` and `execute` both take the
collaborators that reach the outside world (the ``gws`` CLI, the
``ego-browser`` CLI, a clock) as keyword-only overrides whose defaults
are the real ones.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import pwd
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from string import Template
from types import MappingProxyType


def _account_home() -> Path:
    """This account's home directory as the operating system records it.

    Deliberately not ``HOME`` and not `Path.home`, which reads ``HOME``
    first: this process receives a live secret and then executes helpers
    out of that directory, so an environment variable anyone upstream
    can set must not get to choose which binaries those are. The passwd
    database does not move when an environment variable does.
    """
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


_ACCOUNT_HOME = _account_home()

# --- bounds. Ownership first: the broker starts this process in a new
# session and is the only party that may SIGKILL that group, so ``gws``
# and ``ego-browser`` are started here *without* a session of their own
# and stay inside that one group -- one owner for every descendant, and
# no nested group this process could kill only half of.
#
# The numbers below are this process's promise to finish first. The
# broker allows 45s for a password or TOTP fill and 120s for a Gmail
# one; the worst case here is _BROWSER_TIMEOUT_S for the former, and for
# the latter one getProfile plus one list plus at most
# _GMAIL_CANDIDATE_LIMIT gets at _GWS_TIMEOUT_S each, plus one
# _REGEX_TIMEOUT_S per candidate, plus the browser. No retries and no
# sleeps anywhere: a credential fill that did not work the first time is
# a fill that must be refused, not repeated.
#
# Both binaries are pinned absolute paths, never PATH names: this
# process holds a live secret, and resolving a helper through a
# caller-supplied PATH would let whoever set that PATH choose what
# receives it.
_GWS_BIN = Path("/opt/homebrew/bin/gws")
_GWS_TIMEOUT_S = 10.0
_GMAIL_CANDIDATE_LIMIT = 4

_EGO_BROWSER_BIN = _ACCOUNT_HOME / ".local" / "bin" / "ego-browser"
_EGO_TOOLKIT_PATH = _ACCOUNT_HOME / ".local" / "share" / "ego" / "skills" / "ego-browser" / "lib" / "ego-toolkit.js"
_BROWSER_TIMEOUT_S = 40.0
_REAP_TIMEOUT_S = 2.0

#: A job is a few hundred bytes of validated policy. Reading a pipe to
#: EOF is the one unbounded input this process has; this bounds it.
_MAX_JOB_BYTES = 64 * 1024

#: The one job `version` this release understands, and the ceiling on the
#: ordered field list it carries. The broker owns the other side of both
#: (`credentials._JOB_VERSION`, `credentials._MAX_FIELDS`); a job stamped
#: with anything else is refused outright rather than read half-way.
_JOB_VERSION = 2
_MAX_FIELDS = 4

_MAX_AGE_SECONDS_CAP = 3600

#: A message may legitimately be stamped slightly in the future when the
#: sending server's clock leads this Mac's. Anything further ahead than
#: this is not a fresh code, it is a clock nobody should trust.
_CLOCK_SKEW_S = 60.0

#: The wall-clock ceiling on running one message's caller-authored
#: patterns. Pattern *syntax* is validated -- it must compile, and the
#: body pattern must carry a ``code`` group -- but no syntactic screen
#: proves a pattern matches in linear time, so the cost is bounded by a
#: real timer instead of by an argument. See `_bounded`.
_REGEX_TIMEOUT_S = 1.0

#: Ceilings on everything a caller-authored regex is ever run over, and
#: on the MIME tree walked to build it. The broker bounds the pattern;
#: these bound the subject.
_MAX_BODY_CHARS = 64 * 1024
_MAX_BODY_B64 = 4 * (_MAX_BODY_CHARS // 3 + 1)
_MAX_HEADER_CHARS = 1024
_MAX_MIME_PARTS = 64
_TEXT_MIME = ("text/plain", "text/html")

#: A one-time code is short and alphanumeric. Anything else -- a
#: whitespace run, a control character, a whole captured paragraph -- is
#: a pattern that matched the wrong thing, and typing it into a live
#: login form is strictly worse than refusing.
_CODE = re.compile(r"[A-Za-z0-9-]{4,32}")

#: The exact shape of the policy digest a Gmail job carries: SHA-256, in
#: lower-case hex.
_DIGEST = re.compile(r"[0-9a-f]{64}")

#: The two entries in the one private directory each fill creates.
#:
#: `_FIFO_NAME` is where the value crosses. A FIFO has a filesystem
#: *name* but no filesystem *contents*: the bytes live in a kernel pipe
#: buffer and pass straight from this process to the one child reading
#: the other end, so the value never touches disk, never appears in
#: argv, and never appears in the script text -- which carries only this
#: path.
#:
#: `_STAGE_NAME` is where the child says *which stage refused*, and it
#: is the only thing the browser side ever reports back. It has to be a
#: plain file rather than a second FIFO because writing a FIFO blocks
#: until a reader opens it, and the moment the child has to report is
#: precisely the moment it is about to exit. That is safe here for one
#: reason and one reason only: `_read_stage` accepts nothing outside
#: `_EXITS`, so the channel cannot carry a page's text, a value, or a
#: Node stack trace -- a token that is not already a compiled-in
#: keyword is discarded unread. ego's own exit status could not carry
#: this: measured 2026-08-22, `ego-browser nodejs` collapses every
#: nonzero script exit to 1 and prints the real code on stdout, which
#: this process deliberately routes to /dev/null.
_FIFO_NAME = "fill"
_STAGE_NAME = "stage"
_MAX_STAGE_BYTES = 64

#: The input types a fill may legitimately land in. A password goes
#: nowhere but a password field. A one-time code goes into an ordinary
#: short-text field; ``password`` is deliberately absent so one kind's
#: target shape can never stand in for the other's.
_INPUT_TYPES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "password": ("password",),
        "totp": ("text", "tel", "number"),
        "gmail_otp": ("text", "tel", "number"),
    }
)

_TOTP_STEP_SECONDS = 30
_TOTP_DIGITS = 6

#: Which stage refused, as one keyword. This is the whole
#: worker-to-broker channel, and it is a *file* the broker owns and names
#: in the job -- deliberately not this process's exit status.
#:
#: An exit status cannot carry it. `mem-secret run` execs, so a status
#: does survive the trip (measured), but the worker is not the only thing
#: that can produce one: mem-secret itself, sops, or the shell can exit
#: nonzero *before* this module runs at all, and a number chosen here
#: would then be read as a stage that never happened. A token this
#: process writes only when it has actually reached a failure cannot be
#: forged that way -- an absent or empty file is exactly the "something
#: upstream failed" case, and reads as such.
#:
#: A stage is a keyword chosen here at author time, so naming it
#: discloses nothing the caller did not already send: the two closed
#: sets below are the entire vocabulary.
_EXIT_OK = 0
_EXIT_FAILED = 1

#: Every stage this process may report to the broker. The broker owns the
#: matching table (`credentials._STAGE_CODES`); the two are checked
#: against each other by test rather than shared at runtime, because this
#: module is spawned as an isolated script and may not import a sibling.
_STAGES = frozenset(
    {
        # Refused here, before or instead of a browser.
        "bad_job",
        "not_authorized",
        "mailbox_mismatch",
        "secret_missing",
        "bad_totp_secret",
        "gws_failed",
        "otp_not_found",
        "unbounded_match",
        "sink_failed",
        # Refused by the browser side, relayed from its own stage file.
        "locate_space",
        "origin",
        "locate_field",
        "focus",
        "type_verify",
        "timeout_stage",
        "dialog_blocked",
        "handoff_passkey",
    }
)

#: The stages the browser side may report. A narrower set than `_STAGES`:
#: nothing the child says may claim a stage only this process can reach,
#: so a compromised or confused script cannot report "not_authorized"
#: for a fill that was never authorized.
_SINK_STAGES = frozenset(
    {
        "locate_space",
        "origin",
        "locate_field",
        "focus",
        "type_verify",
        "timeout_stage",
        "dialog_blocked",
        "handoff_passkey",
    }
)

#: What `execute` needs from the outside world, and all it needs. The
#: value is passed as a *resolver* rather than a string: see `execute`.
_GwsRunner = Callable[[list[str]], Mapping[str, object]]
_Resolver = Callable[[], str]
_FillRunner = Callable[[Callable[[str, str], str], _Resolver], None]


class _WorkerError(Exception):
    """An internal, fixed-keyword failure reason.

    Never serialized, printed, or otherwise let out of this process --
    `main` catches every exception at the top and reports nothing but an
    exit code -- so unlike `credentials.CredentialError` this carries no
    redaction discipline of its own; it exists purely so `execute`'s
    internal control flow (and this module's own tests) can distinguish
    failure modes without ever risking a caller-visible message.
    """


class _Expired(Exception):
    """Raised in the main thread by `_bounded`'s timer, and caught there."""


@contextmanager
def _bounded(seconds: float) -> Iterator[None]:
    """Run the enclosed block under a real wall-clock ceiling.

    ``SIGALRM`` via `signal.setitimer` is the only bound that actually
    holds against a caller-authored pattern: CPython's regex engine
    checks for pending signals while it matches, so a pattern that has
    gone exponential is interrupted mid-match instead of after it
    finishes. Both facilities are main-thread-only, which is where the
    worker does its matching; anywhere else the timer cannot be armed
    and the block is refused rather than run unbounded.
    """

    def _fire(signum: int, frame: object) -> None:
        raise _Expired

    try:
        previous = signal.signal(signal.SIGALRM, _fire)
    except ValueError as exc:  # not the main thread: no timer, so no match
        raise _WorkerError("unbounded_match") from exc
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# --- job parsing -------------------------------------------------------


def _read_job() -> str:
    """The job the broker wrote to this process's stdin, read to EOF.

    Straight off the descriptor: nothing in `sys` has to be intact for
    the one input this process takes to arrive exactly as it was sent.
    """
    chunks: list[bytes] = []
    total = 0
    while total < _MAX_JOB_BYTES:
        chunk = os.read(0, min(65536, _MAX_JOB_BYTES - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _WorkerError("bad_job") from exc


def _parse_job(raw: str) -> dict[str, object]:
    try:
        job = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _WorkerError("bad_job") from exc
    if not isinstance(job, dict) or job.get("version") != _JOB_VERSION:
        raise _WorkerError("bad_job")
    if job.get("kind") not in _INPUT_TYPES:
        raise _WorkerError("bad_job")
    return job


def _report_stage(job: Mapping[str, object] | None, stage: str) -> None:
    """Name the stage that refused, in the file the broker set aside.

    Best effort by design: the fill has already failed, and failing to
    say where must not turn into failing differently. A missing path, an
    unwritable file, a stage outside `_STAGES` -- each simply leaves the
    broker with an unnamed failure, which is exactly what it means.

    ``stage_path`` arrives in the job, so a job this process could not
    parse has nowhere to report to; that too is the right answer, since a
    job it could not read is a job whose stage it cannot vouch for.
    """
    if job is None or stage not in _STAGES:
        return
    path = job.get("stage_path")
    if not isinstance(path, str) or not path:
        return
    try:
        with open(path, "w", encoding="ascii") as stream:
            stream.write(stage)
    except OSError:
        pass


def _validate_job(job: Mapping[str, object]) -> str:
    """Confirm the target's shape before any secret is resolved.

    A malformed job is refused without ever touching the vault, computing
    a code, or querying Gmail.
    """
    kind = job.get("kind")
    if kind not in _INPUT_TYPES:
        raise _WorkerError("bad_job")
    space = job.get("space")
    origins = job.get("origins")
    fields = job.get("fields")
    if not isinstance(space, str) or not space:
        raise _WorkerError("bad_job")
    if not isinstance(origins, list) or not origins or not all(isinstance(o, str) and o for o in origins):
        raise _WorkerError("bad_job")
    # The broker already bounded and de-duplicated this list; refusing
    # the same shapes again is what makes the two sides independent
    # rather than one side trusting the other's validator.
    if not isinstance(fields, list) or not 1 <= len(fields) <= _MAX_FIELDS:
        raise _WorkerError("bad_job")
    if not all(isinstance(f, str) and f for f in fields) or len(set(fields)) != len(fields):
        raise _WorkerError("bad_job")
    return kind


# --- password / TOTP: value already sitting in this process's own env --


def _take_secret(name: object) -> str:
    """Remove and return the value the broker injected under `name`.

    Popping rather than reading is the whole point: this process spawns
    ``ego-browser``, which inherits a copy of this environment, and no
    name in that copy may still carry a value.
    """
    if not isinstance(name, str) or not name:
        raise _WorkerError("bad_job")
    value = os.environ.pop(name, None)
    if not value:
        raise _WorkerError("secret_missing")
    return value


def _hotp(key: bytes, counter: int, *, digits: int) -> str:
    """RFC 4226 HOTP: HMAC-SHA1 over the big-endian 8-byte counter, then
    the standard dynamic-truncation offset/mask, reduced mod ``10**digits``.
    """
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = (
        (digest[offset] & 0x7F) << 24
        | (digest[offset + 1] & 0xFF) << 16
        | (digest[offset + 2] & 0xFF) << 8
        | (digest[offset + 3] & 0xFF)
    )
    return str(binary % (10**digits)).zfill(digits)


def _base32_decode(secret: str) -> bytes:
    cleaned = re.sub(r"[\s-]", "", secret).upper()
    if not cleaned or not re.fullmatch(r"[A-Z2-7]+=*", cleaned):
        raise _WorkerError("bad_totp_secret")
    padded = cleaned + "=" * (-len(cleaned) % 8)
    try:
        return base64.b32decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise _WorkerError("bad_totp_secret") from exc


def _totp_code(seed: str, *, now: float | None = None) -> str:
    """RFC 6238 TOTP: SHA-1, a fixed 30-second step from the Unix epoch,
    six digits. ``now`` is an injectable clock (seconds since the epoch)
    so this stays a pure function -- production leaves it at `None`
    (real wall-clock time); tests pin it to the RFC 6238 Appendix B
    vector timestamps.
    """
    key = _base32_decode(seed)
    when = time.time() if now is None else now
    counter = int(when // _TOTP_STEP_SECONDS)
    return _hotp(key, counter, digits=_TOTP_DIGITS)


# --- Gmail OTP: nothing in the vault; read live via gws, fully inside
# this process -- the broker never sees a message id, a From header, a
# subject, a body, or the extracted code, only this function's final
# resolved string. ---


@dataclass(frozen=True, slots=True)
class _Gws:
    """The ``gws`` CLI, as one callable collaborator.

    One object owns the pinned binary and the per-call ceiling together,
    so "which gws, for how long" has exactly one answer and one place to
    read it from.
    """

    binary: Path = _GWS_BIN
    timeout: float = _GWS_TIMEOUT_S

    def __call__(self, args: list[str]) -> Mapping[str, object]:
        try:
            proc = subprocess.run(
                [str(self.binary), *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _WorkerError("gws_failed") from exc
        if proc.returncode != 0:
            raise _WorkerError("gws_failed")
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise _WorkerError("gws_failed") from exc
        if not isinstance(parsed, Mapping):
            raise _WorkerError("gws_failed")
        return parsed


@dataclass(frozen=True, slots=True)
class _GmailPolicy:
    """One Gmail entry's validated, entirely nonsecret search policy."""

    mailbox: str
    sender: str
    subject: re.Pattern[str]
    body: re.Pattern[str]
    max_age: int


def _authorize_gmail(job: Mapping[str, object]) -> None:
    """Refuse a Gmail read that was never enrolled for exactly this policy.

    The manifest is a plain file this account can edit, and a Gmail entry
    has no vault secret whose derived name would stop resolving if it
    were edited. This is that binding instead: enrolment stores one
    nonsecret marker -- the digest of the whole entry, mailbox, sender,
    patterns, origins and destination field included -- under a vault
    name derived from that same digest. Change any of it and the job
    derives a name ``mem-secret`` has never heard of, so nothing is
    injected and this refuses before a single Gmail call. The value is
    compared as well as its presence, so a marker enrolled for one entry
    cannot stand in for another.

    Popped, not read, for the same reason as the secret itself: no child
    of this process inherits it.
    """
    expected = job.get("policy_digest")
    name = job.get("auth_env")
    if not isinstance(expected, str) or _DIGEST.fullmatch(expected) is None:
        raise _WorkerError("bad_job")
    if not isinstance(name, str) or not name:
        raise _WorkerError("bad_job")
    marker = os.environ.pop(name, None)
    if marker is None or not hmac.compare_digest(marker, expected):
        raise _WorkerError("not_authorized")


def _gmail_policy(job: Mapping[str, object]) -> _GmailPolicy:
    mailbox = job.get("mailbox")
    sender = job.get("sender")
    subject_pattern = job.get("subject_regex")
    body_pattern = job.get("body_regex")
    max_age = job.get("max_age_seconds")
    if (
        not isinstance(mailbox, str)
        or not mailbox
        or not isinstance(sender, str)
        or not sender
        or not isinstance(subject_pattern, str)
        or not isinstance(body_pattern, str)
        or not body_pattern
        or isinstance(max_age, bool)
        or not isinstance(max_age, int)
        or not (0 < max_age <= _MAX_AGE_SECONDS_CAP)
    ):
        raise _WorkerError("bad_job")
    try:
        subject_re = re.compile(subject_pattern)
        body_re = re.compile(body_pattern)
    except re.error as exc:
        raise _WorkerError("bad_job") from exc
    if "code" not in body_re.groupindex:
        raise _WorkerError("bad_job")
    return _GmailPolicy(mailbox=mailbox, sender=sender, subject=subject_re, body=body_re, max_age=max_age)


def _gmail_header(payload: Mapping[str, object], name: str) -> str | None:
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return None
    lowered = name.casefold()
    for header in headers:
        if isinstance(header, Mapping) and str(header.get("name", "")).casefold() == lowered:
            value = header.get("value")
            return value[:_MAX_HEADER_CHARS] if isinstance(value, str) else None
    return None


def _from_address(payload: Mapping[str, object]) -> str | None:
    """The single mailbox this message's ``From`` header actually claims.

    A Gmail ``from:`` search term matches display names and aliases, so
    it narrows the candidate set but proves nothing. This is the proof:
    the header is parsed, must name exactly one mailbox, and that
    mailbox must be the configured sender.
    """
    header = _gmail_header(payload, "From")
    if header is None:
        return None
    parsed = getaddresses([header])
    if len(parsed) != 1:
        return None
    address = parsed[0][1].strip().casefold()
    return address or None


def _decode_gmail_body(data: str) -> str | None:
    clipped = data[:_MAX_BODY_B64]
    padded = clipped + "=" * (-len(clipped) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    return raw.decode("utf-8", errors="replace")


#: Element content a reader never sees, and the void elements that never
#: close. ``noscript`` is here because ego drives a browser with
#: scripting on, so its content is precisely what the reader does *not*
#: get.
_UNRENDERED_TAGS = frozenset({"script", "style", "head", "title", "template", "noscript"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
)
_WHITESPACE = re.compile(r"\s+")
_HIDDEN_STYLE = ("display:none", "visibility:hidden")


def _is_hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """Whether this start tag hides its own subtree from the reader."""
    for name, value in attrs:
        lowered = name.lower()
        if lowered == "hidden":
            return True
        text = _WHITESPACE.sub("", value or "").lower()
        if lowered == "aria-hidden" and text == "true":
            return True
        if lowered == "style" and any(rule in text for rule in _HIDDEN_STYLE):
            return True
    return False


class _VisibleText(HTMLParser):
    """The text an HTML part actually shows a reader, and nothing else.

    A one-time-code email is markup, and markup carries text the
    recipient never sees: a decoy in a ``display:none`` div, another
    code in a ``<script>`` block or a ``<head>``, an ``aria-hidden``
    duplicate. Matching the raw source would let any of those be chosen
    over the real code, so the pattern runs over the rendered
    approximation instead -- entities resolved (``convert_charrefs``),
    unrendered and hidden subtrees dropped, tag boundaries treated as
    whitespace, and whitespace runs collapsed the way a browser
    collapses them.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._open: list[tuple[str, bool]] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._chunks.append(" ")
        if tag in _VOID_TAGS:
            return
        hidden = tag in _UNRENDERED_TAGS or _is_hidden(attrs)
        self._open.append((tag, hidden))
        self._hidden += hidden

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        # Close to the nearest matching open tag, the way a parser
        # recovers from markup that never closed what it opened.
        self._chunks.append(" ")
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                self._hidden -= sum(hidden for _, hidden in self._open[index:])
                del self._open[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._hidden == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self._chunks)).strip()


def _visible_text(markup: str) -> str:
    parser = _VisibleText()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _gmail_part_text(part: Mapping[str, object]) -> tuple[str, str] | None:
    """One inline text part as ``(mime, text)``, or `None`.

    Only ``text/plain`` and ``text/html`` bodies count, and only when
    they are the message itself: a part with a filename or an
    ``attachmentId`` is an attachment, and an attachment is never a
    place to look for a login code.
    """
    mime = part.get("mimeType")
    if mime not in _TEXT_MIME or part.get("filename"):
        return None
    body = part.get("body")
    if not isinstance(body, Mapping) or body.get("attachmentId"):
        return None
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return None
    text = _decode_gmail_body(data)
    return None if text is None else (mime, text)


def _gmail_body_text(payload: Mapping[str, object]) -> str:
    """The message's readable text: ``text/plain``, or the *visible* text
    of ``text/html`` when there is no plain part at all.

    Bounded on every axis a hostile or merely enormous message could
    stretch -- parts walked, characters per flavour, base64 decoded --
    so the caller's regex always runs over a small, fixed-ceiling
    string.
    """
    texts: dict[str, list[str]] = {mime: [] for mime in _TEXT_MIME}
    sizes = dict.fromkeys(_TEXT_MIME, 0)
    queue: list[object] = [payload]
    cursor = 0
    while cursor < len(queue) and cursor < _MAX_MIME_PARTS:
        part = queue[cursor]
        cursor += 1
        if not isinstance(part, Mapping):
            continue
        found = _gmail_part_text(part)
        if found is not None:
            mime, text = found
            room = _MAX_BODY_CHARS - sizes[mime]
            if room > 0:
                texts[mime].append(text[:room])
                sizes[mime] += min(len(text), room)
        children = part.get("parts")
        if isinstance(children, list):
            queue.extend(children[:_MAX_MIME_PARTS])
    plain = "\n".join(texts["text/plain"])
    return plain or _visible_text("\n".join(texts["text/html"]))


def _gmail_code(payload: Mapping[str, object], policy: _GmailPolicy) -> str | None:
    """The one code this message offers, or `None` if it offers anything else.

    One: a message whose readable text yields two different code-shaped
    captures is a message this policy cannot read unambiguously -- a
    decoy beside the real code, or a pattern loose enough to match both
    -- and choosing between them is how the wrong string gets typed into
    a live login form. A capture that is not code-shaped at all rejects
    the message for the same reason.

    Both patterns are the caller's, so both run under one wall-clock
    ceiling; a pattern that blows through it takes its own message out
    of the running and nothing else.
    """
    subject = _gmail_header(payload, "Subject") or ""
    text = _gmail_body_text(payload)
    found: set[str] = set()
    try:
        with _bounded(_REGEX_TIMEOUT_S):
            if policy.subject.search(subject) is None:
                return None
            for match in policy.body.finditer(text):
                code = match.group("code")
                if not isinstance(code, str) or _CODE.fullmatch(code) is None:
                    return None
                found.add(code)
                if len(found) > 1:
                    return None
    except _Expired:
        return None
    return found.pop() if found else None


def _gmail_code_for(
    policy: _GmailPolicy,
    *,
    run_gws: _GwsRunner,
    now: float | None,
) -> str:
    """The newest code the configured sender has sent to the configured
    mailbox inside the configured window.

    Newest, not unique: a user who pressed "resend" has two live codes in
    the mailbox and only the later one still works, so treating the pair
    as ambiguous would fail exactly the flow resending exists to rescue.
    ``internalDate`` orders them, not list position.

    Takes an already-validated policy rather than the job, because
    `execute` authorizes the read and validates the policy up front and
    defers only the read itself -- so nothing here can run against a
    policy nobody enrolled, and the mailbox is not touched at all for a
    fill the browser side refuses.
    """

    profile = run_gws(["gmail", "users", "getProfile", "--params", json.dumps({"userId": "me"}), "--format", "json"])
    email = profile.get("emailAddress")
    if not isinstance(email, str) or email.strip().casefold() != policy.mailbox.strip().casefold():
        raise _WorkerError("mailbox_mismatch")

    listing = run_gws(
        [
            "gmail",
            "users",
            "messages",
            "list",
            "--params",
            json.dumps({"userId": "me", "q": f"from:{policy.sender}", "maxResults": _GMAIL_CANDIDATE_LIMIT}),
            "--format",
            "json",
        ]
    )
    entries = listing.get("messages")
    ids = (
        [entry["id"] for entry in entries if isinstance(entry, Mapping) and isinstance(entry.get("id"), str)]
        if isinstance(entries, list)
        else []
    )

    now_s = time.time() if now is None else now
    wanted_sender = policy.sender.strip().casefold()
    best: tuple[int, str] | None = None
    for message_id in ids[:_GMAIL_CANDIDATE_LIMIT]:
        message = run_gws(
            [
                "gmail",
                "users",
                "messages",
                "get",
                "--params",
                json.dumps({"userId": "me", "id": message_id, "format": "full"}),
                "--format",
                "json",
            ]
        )
        internal_date = message.get("internalDate")
        if isinstance(internal_date, bool) or not isinstance(internal_date, (str, int)):
            continue
        try:
            stamp_ms = int(internal_date)
        except ValueError:
            continue
        age = now_s - stamp_ms / 1000.0
        if not (-_CLOCK_SKEW_S <= age <= policy.max_age):
            continue
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if _from_address(payload) != wanted_sender:
            continue
        code = _gmail_code(payload, policy)
        if code is None:
            continue
        if best is None or stamp_ms > best[0]:
            best = (stamp_ms, code)

    if best is None:
        raise _WorkerError("otp_not_found")
    return best[1]


# --- browser sink: an ego-browser task space the caller already owns,
# entered but never created and never closed, and one trusted CDP
# insertion into each proven field on one proven document. The value
# crosses into the child through a one-use FIFO; the script text below
# names only that FIFO's path and the stage file's.

#: The browser side's own bounds, in milliseconds.
#:
#: Nothing here used to have a bound at all: the only ceiling was this
#: process's 40-second `_BROWSER_TIMEOUT_S`, so one ego helper call that
#: hung for fifteen seconds was invisible, unattributed, and spent out of
#: the whole fill's budget. ego's own CDP timeout is ~15s, several of its
#: helpers are documented as able to hang a caller indefinitely
#: (`ego-toolkit.js`: "Bounded pageInfo(): a wedged tab must not hang the
#: caller forever"), and helper calls issued while another is in flight
#: *queue behind it* -- so abandoning a stalled call cannot recover this
#: script's ability to do anything else. Exiting can. Every step
#: therefore races a timer and a blown bound exits immediately with the
#: ``timeout_stage`` stage, which turns a 15-to-40 second silent stall
#: into a bounded, named refusal the caller can act on.
#:
#: `_ARM_DEADLINE_MS` is tighter because arming is measured at 0-1ms
#: (`Page.bringToFront`, measured by the ego toolkit's own arm audit
#: after `Page.captureScreenshot` was found to stall multi-second-to-15s
#: about one call in five). Two seconds is a 2000x margin on a healthy
#: arm, and refusing at two seconds is strictly better than the fill it
#: replaces: a fill that had to wait fifteen seconds for a compositor
#: frame is one to run again deliberately, not one to finish blindly.
#:
#: `_SCRIPT_BUDGET_MS` is the whole script's share of
#: `_BROWSER_TIMEOUT_S`, kept well under it so the script always reports
#: its own stage before this process would otherwise SIGKILL it and have
#: nothing to report.
_STEP_DEADLINE_MS = 5000
_ARM_DEADLINE_MS = 2000
_SCRIPT_BUDGET_MS = 30000


_BROWSER_SCRIPT = Template(
    r"""'use strict';
(async () => {
  const SPACE = $space;
  const ORIGINS = $origins;
  const FIELDS = $fields;
  const TYPES = $types;
  const FIFO = $fifo;
  const STAGE = $stage;
  const STEP_MS = $step_ms;
  const ARM_MS = $arm_ms;
  const BUDGET_MS = $budget_ms;

  // The script's own budget for the work it controls. Reset once, after
  // the handoff, because the handoff is the one wait whose length belongs
  // to somebody else: a Gmail code takes as long as Gmail takes, and
  // charging that to the browser side would refuse a healthy fill for
  // being slow somewhere this script has no say over. The worker's
  // `_BROWSER_TIMEOUT_S` and the broker's per-kind deadline are what
  // bound the whole thing.
  let deadline = Date.now() + BUDGET_MS;

  // `fs` first, before anything can fail: it is both how the value
  // arrives and how a refusal is named, and a refusal nobody can name is
  // exactly the failure this script exists to stop reporting.
  let fs = null;
  try {
    fs = typeof process.getBuiltinModule === 'function'
      ? process.getBuiltinModule('node:fs')
      : (await import('node:fs')).default;
  } catch { /* no stage channel; the exit status still says "refused" */ }

  // The stage file is the only thing this script ever tells the worker,
  // and it can only ever hold one of the keywords written into the source
  // below -- never a URL, a selector, a page's text, or a value. The
  // worker refuses any token that is not already one of its own
  // compiled-in stages, so this channel cannot be widened from the page
  // side even in principle.
  const report = (stage) => {
    try { if (fs) fs.writeFileSync(STAGE, stage); } catch { /* unnamed, then */ }
  };
  class Refusal extends Error {
    constructor (stage) { super('refused'); this.stage = stage; }
  }
  const fail = (stage) => { throw new Refusal(stage); };

  // Every await in this script goes through here. Two jobs: give the
  // step a wall-clock ceiling, and give whatever it throws the name of
  // the stage it threw in, so no failure reaches the worker anonymous.
  // The extra no-op catch on the work promise is not decoration: once
  // the timer has won the race, the abandoned call's own rejection would
  // otherwise be an unhandled rejection printing a Node stack trace.
  const step = async (stage, ms, work) => {
    let timer = null;
    const running = (async () => {
      try {
        return await work();
      } catch (error) {
        throw error instanceof Refusal ? error : new Refusal(stage);
      }
    })();
    running.catch(() => {});
    try {
      return await Promise.race([
        running,
        new Promise((_, reject) => {
          const left = Math.min(ms, deadline - Date.now());
          timer = setTimeout(() => reject(new Refusal('timeout_stage')), Math.max(0, left));
        }),
      ]);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  };

  try {
    // The ego-browser wrapper injects an already-hardened T plus the raw
    // helpers into this scope. Capture them the way the wrapper itself
    // does -- typeof-guarded, so a helper this build never injected is
    // undefined instead of a ReferenceError -- and fall back to the
    // local toolkit only when there is no wrapper to inherit T from.
    const H = {
      js: typeof js === 'undefined' ? undefined : js,
      cdp: typeof cdp === 'undefined' ? undefined : cdp,
      pageInfo: typeof pageInfo === 'undefined' ? undefined : pageInfo,
      listTabs: typeof listTabs === 'undefined' ? undefined : listTabs,
      listTaskSpaces: typeof listTaskSpaces === 'undefined' ? undefined : listTaskSpaces,
      useOrCreateTaskSpace: typeof useOrCreateTaskSpace === 'undefined' ? undefined : useOrCreateTaskSpace,
      completeTaskSpace: typeof completeTaskSpace === 'undefined' ? undefined : completeTaskSpace,
      wait: typeof wait === 'undefined' ? undefined : wait,
    };
    if (!H.cdp || !H.pageInfo || !H.listTaskSpaces || !H.listTabs) fail('locate_space');
    let TK = typeof T === 'undefined' ? null : T;
    if (!TK || typeof TK.session !== 'function') {
      const mod = await step('locate_space', STEP_MS, () => import($toolkit));
      const init = mod && mod.default && mod.default.init;
      if (typeof init !== 'function') fail('locate_space');
      TK = init(H);
    }
    if (!TK || typeof TK.session !== 'function') fail('locate_space');

    // The caller's space must already exist, exactly once, and still be
    // the agent's own. A missing name would make T.session CREATE one, a
    // duplicate name makes "the" space meaningless, and a user-owned or
    // agentDelegatedToUser space is a session a human is driving right
    // now -- typing a password into any of those is not the fill that
    // was asked for.
    const spaces = await step('locate_space', STEP_MS, () => H.listTaskSpaces());
    if (!Array.isArray(spaces)) fail('locate_space');
    const matches = spaces.filter((s) => s && s.name === SPACE);
    if (matches.length !== 1) fail('locate_space');
    const wanted = matches[0];
    if (wanted.ownership !== 'agent' || !Number.isInteger(wanted.id)) fail('locate_space');

    // Enter through the toolkit's ownership-checked path, then prove the
    // numeric id did not move: T.session creates on a miss and resolves
    // same-name races by adopting the lowest agent-owned id, so an id
    // that is not the one just vetted means this is a different space.
    // Refused either way, and never completed or handed off -- the space
    // is the caller's, and its 30-minute lease expiring is ego-reap's
    // business, not this process's.
    const handle = await step('locate_space', STEP_MS, () => TK.session(SPACE));
    if (!handle || handle.id !== wanted.id) fail('locate_space');
    const tabs = await step('locate_space', STEP_MS, () => handle.tabs());
    if (!Array.isArray(tabs) || tabs.length === 0) fail('locate_space');

    // One pageInfo, for the one thing only pageInfo reports: whether a
    // native dialog is holding this renderer. Page script does not run
    // while one is up, so every check below would stall rather than
    // answer -- and clicking it away is not this harness's call, because
    // nothing here can read what it says. Its own stage, deliberately
    // separate from the passkey handoff: an alert is a page blocking
    // itself, not a physical authenticator only a person can satisfy,
    // and conflating the two would tell an operator to go press a button
    // that is not there.
    const info = await step('origin', STEP_MS, () => H.pageInfo());
    if (info && info.dialog) fail('dialog_blocked');

    // Identity of the document, not merely its origin: the main frame's
    // id plus its loaderId, both of which change on a real navigation
    // and differ between two tabs showing the same URL. One cheap CDP
    // query answers "is this still an allowed origin" and "is this still
    // the same document" together, so a reload of the same URL -- which
    // an origin comparison alone accepts -- is caught, and the two
    // rechecks that used to cost an ego helper call each now cost one
    // round trip each.
    const docKey = async (stage) => {
      const tree = await step(stage, STEP_MS, () => H.cdp('Page.getFrameTree', {}));
      const frame = tree && tree.frameTree && tree.frameTree.frame;
      if (!frame || !frame.id || typeof frame.url !== 'string') return null;
      let origin;
      try { origin = new URL(frame.url).origin; } catch { return null; }
      if (!ORIGINS.includes(origin)) return null;
      return frame.id + ':' + (frame.loaderId || '');
    };
    const opened = await docKey('origin');
    if (!opened) fail('origin');

    // Resolve every configured field to ONE remote object and hold those
    // objects for the rest of the fill. Every step after this -- the
    // shape check, the arm, the insertion, the readback -- is a call on
    // one of these objectIds, so a page that swaps a different element
    // in behind an identical selector cannot become the thing that
    // receives the value: the object held here is detached, and every
    // later check on it fails.
    const doc = await step('locate_field', STEP_MS, () => H.cdp('DOM.getDocument', { depth: 1 }));
    const rootId = doc && doc.root && doc.root.nodeId;
    if (!rootId) fail('locate_field');
    const targets = [];
    for (const selector of FIELDS) {
      const hits = await step('locate_field', STEP_MS,
        () => H.cdp('DOM.querySelectorAll', { nodeId: rootId, selector }));
      if (!hits || !Array.isArray(hits.nodeIds) || hits.nodeIds.length !== 1) fail('locate_field');
      const resolved = await step('locate_field', STEP_MS,
        () => H.cdp('DOM.resolveNode', { nodeId: hits.nodeIds[0] }));
      const objectId = resolved && resolved.object && resolved.object.objectId;
      if (!objectId) fail('locate_field');
      targets.push(objectId);
    }

    // One declaration, run against one object at a time, answering with
    // one small integer: 0 it is the field this entry was written for,
    // 1 it is not, 2 it is a field only a human can satisfy. It must be
    // an attached, enabled, writable, visible INPUT of an expected type;
    // a hidden or readonly field, a textarea, a contenteditable div, the
    // wrong input type, or an element detached since it was resolved is a
    // page that does not look like the one this entry was written for.
    // Armed, the field is additionally focused and cleared and has to end
    // up that way; unarmed, only the shape is judged, because focus can
    // only ever belong to one field and every field is armed in its turn.
    const CHECK = 'function (types, arm) {' +
      'const el = this;' +
      'if (!el || el.nodeType !== 1 || el.tagName !== "INPUT" || !el.isConnected) return 1;' +
      'if (el.disabled || el.readOnly) return 1;' +
      'const type = (el.getAttribute("type") || "text").toLowerCase();' +
      'if (!types.includes(type)) return 1;' +
      'const box = el.getBoundingClientRect();' +
      'if (box.width <= 0 || box.height <= 0) return 1;' +
      'const style = getComputedStyle(el);' +
      'if (style.visibility === "hidden" || style.display === "none") return 1;' +
      'if (Number(style.opacity) === 0) return 1;' +
      // A "webauthn" token in autocomplete is the page asking the browser
      // for conditional passkey mediation on this very field. Whatever is
      // typed there, the ceremony that follows is a platform-authenticator
      // sheet -- Touch ID, a security key -- that only the human at this
      // Mac can answer, and that this harness deliberately cannot drive.
      // Refusing here is free; typing first would spend the whole
      // deadline and still log nobody in.
      'const hint = (el.getAttribute("autocomplete") || "").toLowerCase();' +
      'if (hint.trim().split(/\\s+/).indexOf("webauthn") >= 0) return 2;' +
      'if (!arm) return 0;' +
      'el.focus({ preventScroll: false });' +
      'if (el.value !== "") { el.value = ""; el.dispatchEvent(new Event("input", { bubbles: true })); }' +
      'return document.activeElement === el && el.value === "" ? 0 : 1;' +
    '}';
    const check = async (objectId, arm) => {
      const done = await step('focus', STEP_MS, () => H.cdp('Runtime.callFunctionOn', {
        objectId,
        functionDeclaration: CHECK,
        arguments: [{ value: TYPES }, { value: arm }],
        returnByValue: true,
      }));
      if (!done || done.exceptionDetails || !done.result) return 1;
      return done.result.value === 0 || done.result.value === 2 ? done.result.value : 1;
    };
    for (const objectId of targets) {
      const verdict = await check(objectId, false);
      if (verdict === 2) fail('handoff_passkey');
      if (verdict !== 0) fail('locate_field');
    }

    // Force-arm this document: without a hit-tested compositor surface
    // the browser side can drop synthesized input and still report
    // success, which is the one failure mode a credential fill must
    // never report as done. Page.captureScreenshot used to arm it and
    // stalled; Page.bringToFront arms the compositor in about a
    // millisecond without activating the macOS app, and the default
    // background override is the measured fallback when CDP refuses it.
    // Bounded, because an arm that has to be waited for is not an arm --
    // and the readback below, not this, is what actually proves the
    // value landed.
    await step('focus', ARM_MS, async () => {
      try {
        await H.cdp('Page.bringToFront', {});
      } catch {
        await H.cdp('Emulation.setDefaultBackgroundColorOverride', {
          color: { r: 255, g: 255, b: 255, a: 1 },
        });
      }
    });

    // Collect the value only now, with every field already proven: a
    // refused fill never reads the FIFO at all. Reading it is what
    // releases the writer on the other end -- and what makes the worker
    // resolve the value in the first place, so a one-time code is
    // generated here rather than before any of the checks above.
    // Unlinking it right after makes the handoff single-use: a second
    // reader would find nothing to open. A handoff that carried nothing
    // is the sink's own failure, not a stage of the fill, so it is thrown
    // unnamed on purpose.
    if (!fs) throw new Error('no handoff');
    const secret = fs.readFileSync(FIFO, 'utf8');
    try { fs.unlinkSync(FIFO); } catch { /* the worker's own cleanup won the race */ }
    if (!secret) throw new Error('empty handoff');

    // That read is the one wait whose length is somebody else's: a Gmail
    // code takes as long as the mailbox takes. The budget for the work
    // this script controls starts again here, so a slow provider cannot
    // make the browser side look like it timed out.
    deadline = Date.now() + BUDGET_MS;

    // Each field after the first waits on the one before it, so between
    // any two insertions the page can have moved on. The document is
    // reproven, and the field armed and reproven, immediately before each
    // insertion -- as tight as this gets without the browser offering an
    // atomic check-and-insert.
    //
    // The one gap left is between that check and the insertion itself:
    // focus lives in the page and the insertion is a browser-level call,
    // so they cannot be one turn. A page that moves focus in that window
    // gets the keystrokes instead, which the readback below then catches
    // -- the fill is refused rather than reported done, but the value did
    // land somewhere on an allowed origin. That residual is named in
    // SECURITY.md rather than papered over; closing it would mean giving
    // up trusted input for a scripted value assignment, which real login
    // forms treat differently.
    //
    // So the readback proves three things in one page turn, on the object
    // that was armed: it is still that object, it is still the focused
    // one, and it holds exactly this string. The value is passed as a
    // call argument, never spliced into evaluated source, and never
    // returned across CDP -- knowing the field holds it requires learning
    // nothing else about it.
    const LANDED = 'function (expected) {'
      + 'return this.isConnected && document.activeElement === this && this.value === expected;'
    + '}';
    for (const objectId of targets) {
      if (await docKey('origin') !== opened) fail('origin');
      const verdict = await check(objectId, true);
      if (verdict === 2) fail('handoff_passkey');
      if (verdict !== 0) fail('focus');
      await step('type_verify', STEP_MS, () => H.cdp('Input.insertText', { text: secret }));
      const landed = await step('type_verify', STEP_MS, () => H.cdp('Runtime.callFunctionOn', {
        objectId,
        functionDeclaration: LANDED,
        arguments: [{ value: secret }],
        returnByValue: true,
      }));
      if (!landed || landed.exceptionDetails || !landed.result || landed.result.value !== true) fail('type_verify');
    }
    if (await docKey('origin') !== opened) fail('origin');
    process.exit(0);
  } catch (error) {
    if (error instanceof Refusal) report(error.stage);
    process.exit(1);
  }
})();
"""
)


def _browser_script(job: Mapping[str, object], fifo: str, stage: str) -> str:
    """The whole browser side of one fill, as one script for one child.

    Dispatch only: `_validate_job` has already confirmed
    ``kind``/``space``/``origins``/``fields``.
    """
    return _BROWSER_SCRIPT.substitute(
        space=json.dumps(job["space"]),
        origins=json.dumps(list(job["origins"])),
        fields=json.dumps(list(job["fields"])),
        types=json.dumps(list(_INPUT_TYPES[str(job["kind"])])),
        toolkit=json.dumps(_EGO_TOOLKIT_PATH.as_uri()),
        fifo=json.dumps(fifo),
        stage=json.dumps(stage),
        step_ms=_STEP_DEADLINE_MS,
        arm_ms=_ARM_DEADLINE_MS,
        budget_ms=_SCRIPT_BUDGET_MS,
    )


@dataclass(slots=True)
class _Handoff:
    """The writing half of one FIFO, and whatever went wrong in it.

    The value is resolved *inside* the thread, once a reader is on the
    other end, so nothing computes a one-time code for a fill the
    browser side is going to refuse -- and nothing computes one seconds
    before it is typed. `failure` is how a resolver's own refusal
    (a mailbox that does not match, a code that never arrived) reaches
    the main thread, which would otherwise see only an empty handoff and
    report the sink.
    """

    resolve: _Resolver
    failure: _WorkerError | None = None

    def __call__(self, fifo: str) -> None:
        """Write the value into `fifo` once, then close it.

        Opening a FIFO for writing blocks until a reader opens the other
        end, so this cannot run ahead of the child: by the time the write
        happens, the process on the other side is the Node reader that was
        just spawned. The value is far smaller than ``PIPE_BUF``, so the
        write is atomic and cannot block once that reader exists, and
        closing is what gives the reader its EOF.
        """
        try:
            handle = os.open(fifo, os.O_WRONLY)
        except OSError:
            return
        try:
            try:
                payload = self.resolve().encode()
            except _WorkerError as exc:
                self.failure = exc
                return
            except Exception:  # noqa: BLE001 - a resolver may only ever fail as a stage
                self.failure = _WorkerError("sink_failed")
                return
            while payload:
                payload = payload[os.write(handle, payload) :]
        except OSError:
            pass
        finally:
            os.close(handle)


def _release(fifo: str, writer: threading.Thread) -> None:
    """Unblock and reap a handoff whose reader never came.

    If the child died, was refused, or never got as far as reading, the
    writer thread is still blocked in `os.open` waiting for a reader that
    will never arrive. Opening the read end here is what releases it; the
    bytes it then writes go into a pipe this function immediately closes,
    so they are discarded unread rather than left in a live thread.
    """
    unblock = -1
    if writer.is_alive():
        try:
            unblock = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            unblock = -1
    writer.join(_REAP_TIMEOUT_S)
    if unblock >= 0:
        os.close(unblock)
    try:
        os.unlink(fifo)
    except OSError:
        pass


def _read_stage(path: str) -> str | None:
    """The stage the browser child named, if it named one this side knows.

    The whole redaction argument for this channel lives in these three
    lines: whatever the file holds is compared against `_SINK_STAGES`,
    and anything else -- a longer token, a page's text, a Node stack
    trace, an empty file, no file at all -- becomes `None`. The child can
    therefore only ever select one of this module's own compiled-in
    keywords, never contribute a string of its own.
    """
    try:
        with open(path, "rb") as stream:
            token = stream.read(_MAX_STAGE_BYTES + 1).decode("ascii", errors="replace")
    except OSError:
        return None
    return token if token in _SINK_STAGES else None


def _kill_child(process: subprocess.Popen[str]) -> None:
    """SIGKILL a wedged browser child, then reap it.

    The child only, never a group: this process shares the broker's
    session, and the broker's ``killpg`` is the one thing entitled to
    take the whole tree down. Signalling that group from inside it would
    make this process a second owner of a tree it is itself part of.
    """
    process.kill()
    if process.stdin is not None and not process.stdin.closed:
        try:
            process.stdin.close()
        except OSError:
            pass
    try:
        process.wait(timeout=_REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass


@dataclass(frozen=True, slots=True)
class _EgoBrowser:
    """The ``ego-browser`` CLI, as one callable collaborator.

    Hands one value to one child across a one-use FIFO. ego's ``nodejs``
    runtime does not inherit custom environment variables from the
    process that invokes it, so the environment is not a channel that
    exists here at all. The FIFO is: the script is handed nothing but its
    path, the bytes never reach a regular file, and the private 0700
    directory holding it -- named by `tempfile.mkdtemp`, so unguessable
    and unshared -- is gone before the call returns either way.

    Alongside it, in the same private directory, one 0600 file the child
    may write one keyword into: ego collapses every nonzero script exit
    to 1, so a browser-side refusal has no other way to say *which* stage
    refused, and a fill that fails without saying where is a fill nobody
    can act on. `_read_stage` is what keeps that channel to a keyword.
    """

    binary: Path = _EGO_BROWSER_BIN
    timeout: float = _BROWSER_TIMEOUT_S

    def __call__(self, make_script: Callable[[str, str], str], resolve: _Resolver) -> None:
        directory = tempfile.mkdtemp(prefix="macos-harness-cred-")
        fifo = os.path.join(directory, _FIFO_NAME)
        stage_path = os.path.join(directory, _STAGE_NAME)
        handoff = _Handoff(resolve)
        try:
            os.mkfifo(fifo, 0o600)
            os.close(os.open(stage_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
            writer = threading.Thread(target=handoff, args=(fifo,), daemon=True)
            writer.start()
            spawn_error: _WorkerError | None = None
            try:
                self._spawn(make_script(fifo, stage_path), stage_path)
            except _WorkerError as exc:
                spawn_error = exc
            finally:
                _release(fifo, writer)
            # A resolver that refused is the real reason the child had
            # nothing to read, and it names its own stage; the sink can
            # only report that the handoff was empty. So the resolver's
            # account wins whenever there is one.
            if handoff.failure is not None:
                raise handoff.failure
            if spawn_error is not None:
                raise spawn_error
        except OSError as exc:
            raise _WorkerError("sink_failed") from exc
        finally:
            try:
                os.unlink(stage_path)
            except OSError:
                pass
            try:
                os.rmdir(directory)
            except OSError:
                pass

    def _spawn(self, script: str, stage_path: str) -> None:
        """Run one script in one ego-browser child, deaf and mute.

        ``stdout``/``stderr`` go to ``/dev/null`` at the OS level rather
        than into a pipe, so there is no buffer in *this* process for the
        child's output -- an ego log line, a page's console noise, a Node
        stack trace quoting the value it was inserting -- to land in.

        No session of its own: the broker put this worker in one, and
        that single group is what the broker SIGKILLs. A nested group
        here would be a second owner of the same tree. The ceiling below
        is this process finishing first, not a second owner -- it kills
        the child it started and refuses the fill.

        A child that exited nonzero is asked which stage refused. A child
        this process had to kill is not: it never reached its own catch,
        so whatever is in that file belongs to no completed stage.
        """
        try:
            process = subprocess.Popen(
                [str(self.binary), "nodejs"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            raise _WorkerError("sink_failed") from exc
        try:
            process.communicate(input=script, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_child(process)
            raise _WorkerError("timeout_stage") from None
        if process.returncode != 0:
            raise _WorkerError(_read_stage(stage_path) or "sink_failed")


# --- orchestration -------------------------------------------------------

_GWS: _GwsRunner = _Gws()
_FILL: _FillRunner = _EgoBrowser()


def execute(
    job: Mapping[str, object],
    *,
    run_gws: _GwsRunner = _GWS,
    run_ego_browser: _FillRunner = _FILL,
    now: float | None = None,
) -> None:
    """Fill `job`'s provider value into `job`'s fields.

    The value is handed over as a *resolver*, not a string, and that is
    the point rather than a style choice. `_hand_off` calls it at the
    moment the browser child opens the FIFO, which is the moment every
    preflight check has already passed -- so a TOTP code is generated
    against the clock it will be typed under rather than against the one
    the fill started under, and a fill that was going to be refused
    never reads a mailbox or spends a code at all. A six-digit TOTP is
    valid for a 30-second step; a browser preflight that took four
    seconds used to eat an eighth of that window for nothing.

    Raises `_WorkerError` on any failure; returns normally only once the
    browser child reported every configured field actually holds it.
    """
    kind = _validate_job(job)
    if kind == "password":
        # Popped now, and nothing deferred: a stored password does not go
        # stale, and taking it here means an entry nobody enrolled is
        # refused before a browser child is ever spawned.
        stored = _take_secret(job.get("secret_env"))
        resolve: _Resolver = lambda: stored
    elif kind == "totp":
        # The seed is taken now -- popping it before any child is spawned
        # is what keeps it out of an inherited environment -- and only the
        # code is computed late.
        seed = _take_secret(job.get("secret_env"))
        resolve = lambda: _totp_code(seed, now=now)
    else:
        # The authorization marker is likewise popped now, so the read
        # itself is already authorized by the time it is deferred.
        _authorize_gmail(job)
        policy = _gmail_policy(job)
        resolve = lambda: _gmail_code_for(policy, run_gws=run_gws, now=now)
    run_ego_browser(lambda fifo, stage: _browser_script(job, fifo, stage), resolve)


def main(*, run_gws: _GwsRunner = _GWS, run_ego_browser: _FillRunner = _FILL) -> int:
    """Read one job from stdin, attempt the fill, report only an exit
    code and, in the file the broker named, which stage refused.
    Deliberately prints nothing on either stream in any outcome -- the
    broker discards both anyway, but nothing this process might have seen
    should depend on that.

    A stage is a keyword out of `_STAGES`, so an operator learns where a
    fill stopped without this process having to print one word about what
    it saw there. Anything unclassified -- an exception this module never
    raises on purpose -- names no stage, and the broker reports the
    failure unnamed rather than guessing.
    """
    job: dict[str, object] | None = None
    try:
        job = _parse_job(_read_job())
        execute(job, run_gws=run_gws, run_ego_browser=run_ego_browser)
    except _WorkerError as exc:
        stage = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
        _report_stage(job, stage)
        return _EXIT_FAILED
    except Exception:  # noqa: BLE001 - the process boundary: report exit code only, never a message
        return _EXIT_FAILED
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
