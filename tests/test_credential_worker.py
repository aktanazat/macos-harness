"""Hermetic tests for ``macos_harness._credential_worker``: the one
process that ever holds a provisioned-credential value.

Every test that resolves a real secret uses a canary string distinct
enough (``CANARY_SECRET``) that its presence anywhere -- a raised
exception's ``str()``, the generated browser script, a real child's
captured argv/env, ``main()``'s captured stdout/stderr -- would be
unmistakable. No test here touches the real ``mem-secret`` vault, a real
Gmail mailbox, or a real browser.

Nothing is patched onto the module. The two pinned binaries reach the
outside world through `_credential_worker._Gws` and
`_credential_worker._EgoBrowser`, each of which owns its own absolute
path and ceiling, so a test names a small fake executable it wrote
itself and gets the real subprocess, the real pipe, the real timeout and
the real exit status. Stdin is the real descriptor 0. The clock is a
parameter. That is the whole seam list.

The browser half is not tested by asserting on script *text*. The
generated script is executed, by the real Node the real child would run
it under, against a fake helper runtime (`_HARNESS_JS`) that stands in
for ego's injected helpers and for one page: a fake DOM with real node
*identity* behind ``DOM.querySelectorAll``/``DOM.resolveNode``, a fake
CDP that really applies ``Input.insertText`` and really evaluates the
script's own functions against the resolved object, and a fake ambient
``T`` whose `session()` mirrors the real toolkit's ownership and id-race
semantics. The value reaches that script the same way it reaches the
real one -- across a real FIFO, written by the module's own `_hand_off`.
Every lifecycle, field, origin, identity and readback refusal below is
therefore a real refusal by the shipped script, not a string match.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import pytest

from macos_harness import _credential_worker as worker

CANARY_SECRET = "CANARY-3f1c9a7e-do-not-leak-8b2d"
SOURCE_ENV = "MACOS_HARNESS_CRED_" + "0123456789ABCDEF" * 2
AUTH_ENV = "MACOS_HARNESS_CRED_AUTH_" + "0123456789ABCDEF" * 2
POLICY_DIGEST = hashlib.sha256(b"acme-otp policy").hexdigest()
SPACE = "acme-space"
ORIGIN = "https://accounts.acme.example"
FIELD = "#password"
FAKE_FIFO = "/tmp/macos-harness-cred-test/fill"

_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(_NODE is None, reason="the browser script is executed by the real Node")

_MakeScript = Callable[[str], str]
_GwsRun = Callable[[list[str]], Mapping[str, object]]


def _browser_job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "version": 1,
        "kind": "password",
        "space": SPACE,
        "origins": [ORIGIN],
        "field": FIELD,
        "secret_env": SOURCE_ENV,
    }
    job.update(overrides)
    return job


def _gmail_job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "version": 1,
        "kind": "gmail_otp",
        "space": SPACE,
        "origins": [ORIGIN],
        "field": "#otp",
        "mailbox": "user@example.com",
        "sender": "noreply@acme.example",
        "subject_regex": "verification code",
        "body_regex": r"code is (?P<code>\d{6})",
        "max_age_seconds": 300,
        "auth_env": AUTH_ENV,
        "policy_digest": POLICY_DIGEST,
    }
    job.update(overrides)
    return job


class _Fill:
    """A fake fill runner: records the script and the value the real one
    would have handed across a FIFO, without spawning anything.
    """

    def __init__(self) -> None:
        self.script: str | None = None
        self.secret: str | None = None
        self.fifos: list[str] = []

    def __call__(self, make_script: _MakeScript, secret: str) -> None:
        self.script = make_script(FAKE_FIFO)
        self.secret = secret
        self.fifos.append(FAKE_FIFO)


def _refuse_fill(make_script: _MakeScript, secret: str) -> None:
    pytest.fail("must not reach the browser")


def _refuse_gws(args: list[str]) -> Mapping[str, object]:
    pytest.fail(f"must not reach gws: {args}")


@pytest.fixture
def enrolled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nonsecret Gmail authorization marker ``mem-secret`` injects.

    Its name is derived from the policy digest and its value *is* that
    digest, so this is exactly what a correctly enrolled Gmail entry
    looks like from inside the worker.
    """
    monkeypatch.setenv(AUTH_ENV, POLICY_DIGEST)


# --- where this process is allowed to look for anything ----------------


def test_account_home_comes_from_the_passwd_database_not_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """This process receives a live secret and then executes helpers out
    of the account's home directory. ``HOME`` is caller-settable (and is
    what `Path.home` reads), so it must have no say in which binaries
    those are.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    home = worker._account_home()

    assert home == Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert home != tmp_path
    for pinned in (worker._EGO_BROWSER_BIN, worker._EGO_TOOLKIT_PATH):
        assert pinned.is_absolute()
        assert pinned.is_relative_to(home)
    assert worker._Gws().binary == Path("/opt/homebrew/bin/gws")
    assert worker._Gws().binary.is_absolute()
    assert worker._EgoBrowser().binary == worker._EGO_BROWSER_BIN


# --- the stdin boundary -------------------------------------------------


def _feed_stdin(payload: bytes, tmp_path: Path) -> int:
    """Put `payload` on the real descriptor 0 and return the saved one."""
    source = tmp_path / f"stdin-{time.monotonic_ns()}"
    source.write_bytes(payload)
    saved = os.dup(0)
    with source.open("rb") as handle:
        os.dup2(handle.fileno(), 0)
    return saved


def _restore_stdin(saved: int) -> None:
    os.dup2(saved, 0)
    os.close(saved)


def test_read_job_takes_the_whole_job_off_descriptor_zero(tmp_path: Path) -> None:
    raw = json.dumps(_browser_job()).encode()
    saved = _feed_stdin(raw, tmp_path)
    try:
        assert worker._read_job() == raw.decode()
    finally:
        _restore_stdin(saved)


def test_read_job_stops_at_its_own_ceiling(tmp_path: Path) -> None:
    """Reading a pipe to EOF is the one unbounded input this process has.
    A job past the ceiling is truncated, which makes it unparseable --
    refused, never streamed into memory without limit.
    """
    saved = _feed_stdin(b"x" * (worker._MAX_JOB_BYTES * 2), tmp_path)
    try:
        raw = worker._read_job()
    finally:
        _restore_stdin(saved)

    assert len(raw) == worker._MAX_JOB_BYTES
    with pytest.raises(worker._WorkerError, match="bad_job"):
        worker._parse_job(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "42",
        '["not", "a", "dict"]',
        json.dumps({"version": 2, "kind": "password"}),
        json.dumps({"kind": "password"}),  # missing version
        json.dumps({"version": 1, "kind": "bogus"}),
        json.dumps({"version": 1, "kind": "native"}),  # the deleted sink is not a kind
        json.dumps({"version": 1}),  # missing kind
    ],
)
def test_parse_job_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(worker._WorkerError):
        worker._parse_job(raw)


def test_parse_job_accepts_the_brokers_exact_browser_job() -> None:
    job = worker._parse_job(json.dumps(_browser_job()))
    assert job["kind"] == "password"
    assert job["space"] == SPACE
    assert "ref" not in job  # the worker never had a use for it


@pytest.mark.parametrize(
    "overrides",
    [
        {"space": ""},
        {"space": None},
        {"space": 7},
        {"origins": []},
        {"origins": None},
        {"origins": [ORIGIN, ""]},
        {"origins": ORIGIN},  # a bare string, not a list
        {"field": ""},
        {"field": None},
        {"kind": "bogus"},
    ],
)
def test_execute_rejects_a_malformed_target_before_touching_a_secret(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    monkeypatch.setenv(SOURCE_ENV, CANARY_SECRET)
    with pytest.raises(worker._WorkerError, match="bad_job"):
        worker.execute(_browser_job(**overrides), run_gws=_refuse_gws, run_ego_browser=_refuse_fill)
    assert os.environ[SOURCE_ENV] == CANARY_SECRET  # never even looked up


# --- the secret's one and only channel ----------------------------------


def test_take_secret_pops_the_name_so_it_cannot_be_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_ENV, CANARY_SECRET)
    assert worker._take_secret(SOURCE_ENV) == CANARY_SECRET
    assert SOURCE_ENV not in os.environ


@pytest.mark.parametrize("value", [None, ""])
def test_take_secret_reports_secret_missing(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(SOURCE_ENV, value)
    with pytest.raises(worker._WorkerError, match="secret_missing"):
        worker._take_secret(SOURCE_ENV)


@pytest.mark.parametrize("name", [None, "", 7, ["A"]])
def test_take_secret_rejects_a_name_that_is_not_one(name: object) -> None:
    with pytest.raises(worker._WorkerError, match="bad_job"):
        worker._take_secret(name)


def test_execute_password_scrubs_the_source_name_and_puts_the_value_nowhere_but_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SOURCE_ENV, CANARY_SECRET)
    fill = _Fill()

    worker.execute(_browser_job(), run_gws=_refuse_gws, run_ego_browser=fill)

    assert fill.secret == CANARY_SECRET
    assert CANARY_SECRET not in str(fill.script)
    assert SOURCE_ENV not in str(fill.script)
    assert SOURCE_ENV not in os.environ
    assert CANARY_SECRET not in json.dumps(dict(os.environ))


def test_execute_reports_secret_missing_without_reaching_the_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    with pytest.raises(worker._WorkerError, match="secret_missing"):
        worker.execute(_browser_job(), run_ego_browser=_refuse_fill)


# --- RFC 6238 TOTP: SHA-1, 30s step, six digits -------------------------


# RFC 6238 Appendix B, SHA-1 mode: the 20-byte ASCII key "12345678901234567890",
# an 8-digit truncation at each vector time. This module always produces six
# digits, and 10**6 divides 10**8, so the 6-digit code is exactly the last six
# characters of the RFC's own 8-digit vector -- not a separately-sourced number.
_RFC6238_SEED = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize(
    ("unix_time", "expected_8_digit"),
    [
        (59, "94287082"),
        (1111111109, "07081804"),
        (1111111111, "14050471"),
        (1234567890, "89005924"),
        (2000000000, "69279037"),
        (20000000000, "65353130"),
    ],
)
def test_totp_code_matches_rfc6238_appendix_b_vectors(unix_time: int, expected_8_digit: str) -> None:
    assert worker._totp_code(_RFC6238_SEED, now=unix_time) == expected_8_digit[-6:]


def test_totp_code_is_deterministic_within_a_time_step_and_changes_across_one() -> None:
    # Step boundaries are multiples of 30s: 100 and 119 share counter 3;
    # 120 starts counter 4.
    assert worker._totp_code(_RFC6238_SEED, now=100) == worker._totp_code(_RFC6238_SEED, now=119)
    assert worker._totp_code(_RFC6238_SEED, now=100) != worker._totp_code(_RFC6238_SEED, now=120)


def test_totp_code_accepts_lowercase_whitespace_hyphens_and_missing_padding() -> None:
    messy = _RFC6238_SEED.lower()
    messy = messy[:4] + "-" + messy[4:8] + " " + messy[8:]
    assert worker._totp_code(messy, now=59) == "287082"
    assert worker._totp_code(messy.rstrip("="), now=59) == "287082"


@pytest.mark.parametrize("bad_seed", ["", "not-base32!!!", "12345", "========"])
def test_totp_code_rejects_invalid_base32(bad_seed: str) -> None:
    with pytest.raises(worker._WorkerError, match="bad_totp_secret"):
        worker._totp_code(bad_seed, now=59)


def test_execute_totp_hands_over_the_code_and_never_the_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SOURCE_ENV, _RFC6238_SEED)
    fill = _Fill()

    worker.execute(_browser_job(kind="totp", field="#otp"), run_ego_browser=fill, now=59)

    assert fill.secret == "287082"
    assert _RFC6238_SEED not in str(fill.script)
    assert SOURCE_ENV not in os.environ


# --- gmail_otp: authorized by an enrolled marker, then read live via gws -


_NOW = 1_800_000_000.0


def _part(mime: str, text: str, *, filename: str | None = None, attachment: bool = False) -> dict[str, object]:
    body: dict[str, object] = {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}
    if attachment:
        body["attachmentId"] = "att-1"
    part: dict[str, object] = {"mimeType": mime, "body": body}
    if filename is not None:
        part["filename"] = filename
    return part


def _gmail_message(
    *,
    subject: str = "Your verification code",
    body_text: str = "Your code is 123456, thanks",
    age_seconds: float = 60,
    sender: str = "noreply@acme.example",
    from_header: str | None = None,
    now: float = _NOW,
    parts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "internalDate": str(int((now - age_seconds) * 1000)),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_header if from_header is not None else f"Acme <{sender}>"},
            ],
            "mimeType": "multipart/alternative",
            "parts": parts if parts is not None else [_part("text/plain", body_text)],
        },
    }


def _fake_gws(
    messages: Mapping[str, Mapping[str, object]],
    ids: list[str],
    *,
    mailbox: str = "user@example.com",
    on_get: Callable[[str], None] | None = None,
) -> _GwsRun:
    def run(args: list[str]) -> Mapping[str, object]:
        if args[:3] == ["gmail", "users", "getProfile"]:
            return {"emailAddress": mailbox}
        if args[:4] == ["gmail", "users", "messages", "list"]:
            return {"messages": [{"id": message_id} for message_id in ids]}
        if args[:4] == ["gmail", "users", "messages", "get"]:
            params = json.loads(args[args.index("--params") + 1])
            if on_get is not None:
                on_get(params["id"])
            return messages[params["id"]]
        raise AssertionError(f"unexpected gws invocation: {args}")

    return run


def _resolve_gmail(
    messages: Mapping[str, Mapping[str, object]],
    ids: list[str],
    *,
    mailbox: str = "user@example.com",
    **overrides: object,
) -> str:
    fill = _Fill()
    worker.execute(
        _gmail_job(**overrides),
        run_gws=_fake_gws(messages, ids, mailbox=mailbox),
        run_ego_browser=fill,
        now=_NOW,
    )
    assert fill.secret is not None
    return fill.secret


def test_gmail_otp_extracts_the_code_from_a_single_qualifying_message(enrolled: None) -> None:
    assert _resolve_gmail({"m1": _gmail_message()}, ["m1"]) == "123456"


def test_gmail_otp_falls_back_to_the_html_part_only_when_there_is_no_plain_part(enrolled: None) -> None:
    html_only = _gmail_message(parts=[_part("text/html", "<b>Your code is 777777</b>")])
    assert _resolve_gmail({"m1": html_only}, ["m1"]) == "777777"


def test_gmail_otp_prefers_the_plain_part_when_the_message_carries_both(enrolled: None) -> None:
    both = _gmail_message(
        parts=[_part("text/html", "<b>Your code is 777777</b>"), _part("text/plain", "Your code is 123456")]
    )
    assert _resolve_gmail({"m1": both}, ["m1"]) == "123456"


# --- the authorization marker: policy, not just a manifest line ---------


def test_gmail_refuses_before_any_gws_call_when_the_marker_was_never_enrolled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Gmail entry has no vault secret, so nothing else would notice a
    manifest edited to point a live ref at another mailbox, sender,
    pattern or destination field. The enrolled marker is that binding,
    and it is checked before a single message is fetched.
    """
    monkeypatch.delenv(AUTH_ENV, raising=False)

    with pytest.raises(worker._WorkerError, match="not_authorized"):
        worker.execute(_gmail_job(), run_gws=_refuse_gws, run_ego_browser=_refuse_fill, now=_NOW)


def test_gmail_refuses_a_marker_that_is_not_this_policys_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence is not enough: a marker enrolled for one entry must not
    stand in for another.
    """
    monkeypatch.setenv(AUTH_ENV, hashlib.sha256(b"some other entry").hexdigest())

    with pytest.raises(worker._WorkerError, match="not_authorized"):
        worker.execute(_gmail_job(), run_gws=_refuse_gws, run_ego_browser=_refuse_fill, now=_NOW)


def test_gmail_pops_the_marker_so_no_child_of_this_process_inherits_it(enrolled: None) -> None:
    assert _resolve_gmail({"m1": _gmail_message()}, ["m1"]) == "123456"
    assert AUTH_ENV not in os.environ


@pytest.mark.parametrize(
    "overrides",
    [
        {"policy_digest": None},
        {"policy_digest": ""},
        {"policy_digest": POLICY_DIGEST.upper()},  # the digest is lower-case hex
        {"policy_digest": POLICY_DIGEST[:-1]},
        {"policy_digest": 7},
        {"auth_env": None},
        {"auth_env": ""},
        {"auth_env": 7},
    ],
)
def test_gmail_rejects_a_malformed_authorization_before_reading_the_environment(
    enrolled: None, overrides: dict[str, object]
) -> None:
    with pytest.raises(worker._WorkerError, match="bad_job"):
        worker.execute(_gmail_job(**overrides), run_gws=_refuse_gws, run_ego_browser=_refuse_fill, now=_NOW)
    assert os.environ[AUTH_ENV] == POLICY_DIGEST  # untouched


# --- which message, and which text inside it ----------------------------


def test_gmail_otp_never_reads_a_code_out_of_an_attachment(enrolled: None) -> None:
    """A part with a filename, or one whose bytes live behind an
    ``attachmentId``, is an attachment. A login code that only appears
    there is not a code this mailbox was sent.
    """
    messages = {
        "m1": _gmail_message(
            parts=[
                _part("text/plain", "Your code is 123456", filename="codes.txt"),
                _part("text/plain", "Your code is 654321", attachment=True),
            ]
        )
    }
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail(messages, ["m1"])


def test_gmail_otp_rejects_a_message_older_than_max_age_seconds(enrolled: None) -> None:
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": _gmail_message(age_seconds=999)}, ["m1"])


def test_gmail_otp_allows_a_minute_of_future_clock_skew(enrolled: None) -> None:
    assert _resolve_gmail({"m1": _gmail_message(age_seconds=-30)}, ["m1"]) == "123456"


def test_gmail_otp_rejects_a_message_stamped_further_ahead_than_the_skew_allows(enrolled: None) -> None:
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": _gmail_message(age_seconds=-90)}, ["m1"])


def test_gmail_otp_rejects_a_message_whose_subject_does_not_match(enrolled: None) -> None:
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": _gmail_message(subject="unrelated newsletter")}, ["m1"])


@pytest.mark.parametrize(
    "from_header",
    [
        "Acme Security <attacker@evil.example>",
        '"noreply@acme.example" <attacker@evil.example>',
        "noreply@acme.example.evil.example",
        "attacker@evil.example, noreply@acme.example",
        "not an address at all",
        "",
    ],
)
def test_gmail_otp_requires_the_parsed_from_address_to_be_exactly_the_configured_sender(
    enrolled: None, from_header: str
) -> None:
    """Gmail's ``from:`` search term matches display names and aliases, so
    a listing hit proves nothing on its own. Only the parsed ``From``
    mailbox does.
    """
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": _gmail_message(from_header=from_header)}, ["m1"])


@pytest.mark.parametrize("from_header", ["noreply@acme.example", "NoReply@Acme.Example"])
def test_gmail_otp_accepts_a_bare_or_differently_cased_from_header(enrolled: None, from_header: str) -> None:
    assert _resolve_gmail({"m1": _gmail_message(from_header=from_header)}, ["m1"]) == "123456"


def test_gmail_otp_rejects_a_mailbox_that_does_not_match_the_authenticated_account(enrolled: None) -> None:
    with pytest.raises(worker._WorkerError, match="mailbox_mismatch"):
        _resolve_gmail({"m1": _gmail_message()}, ["m1"], mailbox="someone-else@example.com")


@pytest.mark.parametrize("order", [["old", "new"], ["new", "old"]])
def test_gmail_otp_picks_the_newest_code_so_a_resend_wins(enrolled: None, order: list[str]) -> None:
    """Pressing "resend" leaves two live codes in the mailbox and only the
    later one still works. ``internalDate`` decides, not list position.
    """
    messages = {
        "old": _gmail_message(body_text="Your code is 111111", age_seconds=120),
        "new": _gmail_message(body_text="Your code is 222222", age_seconds=5),
    }
    assert _resolve_gmail(messages, order) == "222222"


@pytest.mark.parametrize("code", ["12", "1 3456", "1234\n56", "x" * 40, "12\u200b34"])
def test_gmail_otp_refuses_a_capture_that_is_not_code_shaped(enrolled: None, code: str) -> None:
    """A pattern that matched the wrong thing must fail the fill, not type
    a paragraph, a control character or a two-character fragment into a
    live login form.
    """
    messages = {"m1": _gmail_message(body_text=f"Your code is [{code}] thanks")}
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail(messages, ["m1"], body_regex=r"code is \[(?P<code>[^\]]*)\]")


def test_gmail_otp_accepts_an_alphanumeric_hyphenated_code(enrolled: None) -> None:
    messages = {"m1": _gmail_message(body_text="Your code is [ABCD-1234] thanks")}
    assert _resolve_gmail(messages, ["m1"], body_regex=r"code is \[(?P<code>[^\]]*)\]") == "ABCD-1234"


def test_gmail_otp_refuses_a_message_offering_two_different_codes(enrolled: None) -> None:
    """Two different code-shaped captures in one message is a message this
    policy cannot read unambiguously -- and choosing between them is how
    the wrong string gets typed into a live login form.
    """
    two = _gmail_message(body_text="Your code is 123456. If that expired, your code is 654321.")
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": two}, ["m1"])


def test_gmail_otp_accepts_the_same_code_repeated(enrolled: None) -> None:
    """One code stated twice -- body and footer, or a plain restatement --
    is still one code, and refusing it would fail an ordinary email.
    """
    repeated = _gmail_message(body_text="Your code is 123456. Again: your code is 123456.")
    assert _resolve_gmail({"m1": repeated}, ["m1"]) == "123456"


@pytest.mark.parametrize(
    "decoy",
    [
        '<div style="display:none">Your code is 999999</div>',
        '<div style="display: none !important">Your code is 999999</div>',
        '<div style="visibility:hidden">Your code is 999999</div>',
        "<div hidden>Your code is 999999</div>",
        '<div aria-hidden="true">Your code is 999999</div>',
        "<script>var t = 'Your code is 999999';</script>",
        "<style>/* Your code is 999999 */</style>",
        "<head><title>Your code is 999999</title></head>",
        "<noscript>Your code is 999999</noscript>",
        '<span style="display:none">Your code is 999999<span> nested </span></span>',
    ],
    ids=[
        "display-none",
        "spaced-display-none",
        "visibility-hidden",
        "hidden-attribute",
        "aria-hidden",
        "script",
        "style",
        "head",
        "noscript",
        "nested-inside-hidden",
    ],
)
def test_gmail_otp_reads_only_what_the_html_part_actually_shows(enrolled: None, decoy: str) -> None:
    """A code hidden in markup is a code the recipient never saw. If it
    counted, any sender could put a second one where only the parser
    looks -- and two codes refuse the fill outright, so a decoy that
    counted would also be a denial of service.
    """
    markup = f"<html><body>{decoy}<p>Your code is 123456</p></body></html>"
    message = _gmail_message(parts=[_part("text/html", markup)])

    assert _resolve_gmail({"m1": message}, ["m1"]) == "123456"


def test_gmail_otp_unescapes_entities_and_collapses_markup_whitespace(enrolled: None) -> None:
    """The pattern runs over the rendered approximation, so an entity is
    the character it stands for and a tag boundary is whitespace.
    """
    markup = "<p>Your\n  code\tis</p>\n<b>12345&#54;</b>"
    message = _gmail_message(parts=[_part("text/html", markup)])

    assert _resolve_gmail({"m1": message}, ["m1"]) == "123456"


def test_gmail_otp_still_refuses_two_codes_that_are_both_visible(enrolled: None) -> None:
    markup = "<p>Your code is 123456</p><p>Your code is 654321</p>"
    message = _gmail_message(parts=[_part("text/html", markup)])

    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": message}, ["m1"])


def test_gmail_otp_never_searches_more_body_text_than_the_bound_allows(enrolled: None) -> None:
    """A caller-authored regex only ever runs over a bounded string, so a
    message big enough to matter cannot turn one fill into a pathological
    scan. The code past the bound is simply not found.
    """
    padded = "x" * (worker._MAX_BODY_CHARS + 64) + " Your code is 123456"
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": _gmail_message(parts=[_part("text/plain", padded)])}, ["m1"])


def test_gmail_otp_abandons_a_pattern_that_outruns_its_wall_clock_bound(enrolled: None) -> None:
    """Pattern syntax is validated, but no syntactic screen proves a
    pattern matches in linear time. This one is exponential on purpose:
    the fill is refused because a real timer stopped the match, not
    because the engine eventually finished.
    """
    catastrophic = r"code is (?P<code>(a+)+b)"
    message = _gmail_message(body_text="Your code is " + "a" * 64)

    started = time.monotonic()
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        _resolve_gmail({"m1": message}, ["m1"], body_regex=catastrophic)
    elapsed = time.monotonic() - started

    assert elapsed < worker._REGEX_TIMEOUT_S * 3


def test_gmail_otp_disarms_the_timer_once_the_match_is_over(enrolled: None) -> None:
    """The alarm belongs to one message's matching and nothing else: it is
    cancelled whether the match finished, failed, or was cut off, so the
    next candidate -- and the browser step after it -- are never
    interrupted by a leftover timer.
    """
    slow = _gmail_message(body_text="Your code is " + "a" * 64)
    good = _gmail_message(body_text="Your code is 123456", age_seconds=5)
    both = r"code is (?P<code>\d{6}|(a+)+b)"

    assert _resolve_gmail({"m1": slow, "m2": good}, ["m1", "m2"], body_regex=both) == "123456"
    time.sleep(worker._REGEX_TIMEOUT_S * 1.2)  # nothing may fire after the block


def test_gmail_otp_never_fetches_more_candidates_than_the_bounded_limit(enrolled: None) -> None:
    ids = [f"m{i}" for i in range(worker._GMAIL_CANDIDATE_LIMIT + 5)]  # a hostile/buggy list response
    messages = {message_id: _gmail_message(subject="unrelated") for message_id in ids}
    fetched: list[str] = []
    with pytest.raises(worker._WorkerError, match="otp_not_found"):
        worker.execute(
            _gmail_job(),
            run_gws=_fake_gws(messages, ids, on_get=fetched.append),
            run_ego_browser=_refuse_fill,
            now=_NOW,
        )
    assert len(fetched) <= worker._GMAIL_CANDIDATE_LIMIT


def test_worst_case_provider_and_browser_fit_inside_the_brokers_deadlines() -> None:
    """The broker allows 45s for a password or TOTP fill and 120s for a
    Gmail one, and SIGKILLs the whole worker session at that point. Every
    bound here is fixed, so the worst case is arithmetic, not a hope: one
    profile call, one list call, `_GMAIL_CANDIDATE_LIMIT` gets, one
    bounded match per candidate, then one browser child.
    """
    provider = worker._GWS_TIMEOUT_S * (2 + worker._GMAIL_CANDIDATE_LIMIT)
    matching = worker._REGEX_TIMEOUT_S * worker._GMAIL_CANDIDATE_LIMIT

    assert provider + matching + worker._BROWSER_TIMEOUT_S < 120.0
    assert worker._BROWSER_TIMEOUT_S < 45.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"body_regex": r"code is (\d{6})"},  # unnamed group
        {"subject_regex": "[unclosed"},
        {"max_age_seconds": 0},
        {"max_age_seconds": -5},
        {"max_age_seconds": 3601},
        {"max_age_seconds": True},
        {"mailbox": ""},
        {"sender": ""},
        {"body_regex": ""},
    ],
)
def test_gmail_otp_rejects_malformed_policy_before_reaching_gws(enrolled: None, overrides: dict[str, object]) -> None:
    with pytest.raises(worker._WorkerError, match="bad_job"):
        worker.execute(_gmail_job(**overrides), run_gws=_refuse_gws, run_ego_browser=_refuse_fill)


# --- the gws subprocess boundary: a real child, a real pipe -------------


def _write_fake_executable(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{textwrap.dedent(body)}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _WriteExecutable(Protocol):
    def __call__(self, name: str, body: str) -> Path: ...


@pytest.fixture
def fake_binary(tmp_path: Path) -> _WriteExecutable:
    """Write a small executable a collaborator can be pointed at."""

    def install(name: str, body: str) -> Path:
        return _write_fake_executable(tmp_path / name, body)

    return install


def test_gws_parses_one_json_object_and_runs_only_the_binary_it_was_given(
    tmp_path: Path, fake_binary: _WriteExecutable
) -> None:
    seen = tmp_path / "argv.json"
    binary = fake_binary(
        "gws",
        f"""
        import json
        import os
        import sys

        json.dump({{"argv": sys.argv, "pgid": os.getpgid(0)}}, open({str(seen)!r}, "w"))
        print(json.dumps({{"ok": True}}))
        """,
    )

    assert worker._Gws(binary=binary)(["gmail", "users", "getProfile"]) == {"ok": True}

    recorded = json.loads(seen.read_text())
    assert recorded["argv"][0] == str(binary)
    assert Path(recorded["argv"][0]).is_absolute()
    assert recorded["argv"][1:] == ["gmail", "users", "getProfile"]
    # No session of its own: the broker's group owns every descendant.
    assert recorded["pgid"] == os.getpgid(0)


def test_gws_reports_failure_on_a_nonzero_exit_without_surfacing_stderr(fake_binary: _WriteExecutable) -> None:
    binary = fake_binary(
        "gws",
        f"""
        import sys

        print("boom {CANARY_SECRET}", file=sys.stderr)
        sys.exit(1)
        """,
    )

    with pytest.raises(worker._WorkerError, match="gws_failed") as excinfo:
        worker._Gws(binary=binary)(["gmail", "users", "getProfile"])

    assert CANARY_SECRET not in str(excinfo.value)


@pytest.mark.parametrize("body", ["print('not json')", "print('[1, 2, 3]')"])
def test_gws_reports_failure_on_output_that_is_not_one_json_object(fake_binary: _WriteExecutable, body: str) -> None:
    binary = fake_binary("gws", body)
    with pytest.raises(worker._WorkerError, match="gws_failed"):
        worker._Gws(binary=binary)(["gmail", "users", "getProfile"])


def test_gws_reports_failure_when_the_binary_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(worker._WorkerError, match="gws_failed"):
        worker._Gws(binary=tmp_path / "definitely-not-here")(["gmail", "users", "getProfile"])


def test_gws_reports_failure_when_the_call_outruns_its_ceiling(fake_binary: _WriteExecutable) -> None:
    binary = fake_binary("gws", "import time\ntime.sleep(30)")

    started = time.monotonic()
    with pytest.raises(worker._WorkerError, match="gws_failed"):
        worker._Gws(binary=binary, timeout=0.5)(["gmail", "users", "getProfile"])

    assert time.monotonic() - started < 5.0


# --- the ego-browser boundary: a real child, a real FIFO ---------------


def _fifo_reader_ego(out: Path) -> str:
    """A fake child that does exactly what the real script's handoff step
    does: pull the FIFO path out of the script it was handed on stdin,
    read the FIFO, and report what crossed.
    """
    return f"""
    import json
    import os
    import re
    import sys

    script = sys.stdin.read()
    path = re.search(r'const FIFO = "([^"]+)";', script).group(1)
    with open(path, "r") as handle:
        crossed = handle.read()
    json.dump(
        {{
            "crossed": crossed,
            "script": script,
            "argv": sys.argv[1:],
            "env": [name for name, value in os.environ.items() if {CANARY_SECRET!r} in value],
            "pgid": os.getpgid(0),
        }},
        open({str(out)!r}, "w"),
    )
    """


def test_ego_browser_hands_the_exact_bytes_across_a_one_use_fifo(
    tmp_path: Path, fake_binary: _WriteExecutable
) -> None:
    """The strongest available proof of the transport: a real OS
    subprocess, spawned exactly the way the real one is, reads the real
    FIFO and reports byte-for-byte what arrived -- while the script it
    was handed, its argv and its whole environment stay free of the
    value.
    """
    out = tmp_path / "seen.json"
    binary = fake_binary("ego-browser", _fifo_reader_ego(out))
    seen_paths: list[str] = []

    def make_script(fifo: str) -> str:
        seen_paths.append(fifo)
        return worker._browser_script(_browser_job(), fifo)

    worker._EgoBrowser(binary=binary)(make_script, CANARY_SECRET)

    seen = json.loads(out.read_text())
    assert seen["crossed"] == CANARY_SECRET  # exact bytes, no truncation, no newline
    assert seen["argv"] == ["nodejs"]
    assert CANARY_SECRET not in seen["script"]
    assert seen["env"] == []  # nothing in the child's environment carries it
    assert seen["pgid"] == os.getpgid(0)  # no session of its own: one owner for the tree
    fifo = seen_paths[0]
    assert not os.path.exists(fifo)  # one use only
    assert not os.path.exists(os.path.dirname(fifo))  # and the private dir goes with it


def test_ego_browser_leaves_no_fifo_dir_or_writer_behind_when_the_child_never_reads(
    fake_binary: _WriteExecutable,
) -> None:
    """A refused or crashed child leaves the handoff writer blocked on a
    reader that will never come. Cleanup has to release it, or every
    failed fill would strand a thread holding the value.
    """
    binary = fake_binary("ego-browser", "import sys\nsys.stdin.read()\nsys.exit(1)")
    seen_paths: list[str] = []
    before = threading.active_count()

    with pytest.raises(worker._WorkerError, match="sink_failed"):
        worker._EgoBrowser(binary=binary)(lambda fifo: seen_paths.append(fifo) or "script", CANARY_SECRET)

    fifo = seen_paths[0]
    assert not os.path.exists(fifo)
    assert not os.path.exists(os.path.dirname(fifo))
    assert threading.active_count() == before


def test_ego_browser_discards_the_childs_own_output(
    capfd: pytest.CaptureFixture[str], fake_binary: _WriteExecutable
) -> None:
    """A child that logs what it was inserting must not be able to put it
    anywhere this process (or the broker above it) can see.
    """
    binary = fake_binary(
        "ego-browser",
        f"""
        import sys

        sys.stdin.read()
        print("stdout leak {CANARY_SECRET}")
        print("stderr leak {CANARY_SECRET}", file=sys.stderr)
        """,
    )

    worker._EgoBrowser(binary=binary)(lambda fifo: "harmless", CANARY_SECRET)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_ego_browser_reports_sink_failed_when_the_binary_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(worker._WorkerError, match="sink_failed"):
        worker._EgoBrowser(binary=tmp_path / "definitely-not-here")(lambda fifo: "script", CANARY_SECRET)


def test_ego_browser_kills_the_child_it_started_when_the_fill_outruns_its_ceiling(
    tmp_path: Path, fake_binary: _WriteExecutable
) -> None:
    """The worker finishes first so the broker's deadline stays a
    backstop, and it kills the child it started -- but only that child:
    it shares the broker's session, and signalling that group from inside
    it would make this process a second owner of a tree it is part of.
    """
    facts = tmp_path / "child.json"
    binary = fake_binary(
        "ego-browser",
        f"""
        import json
        import os
        import sys
        import time

        json.dump({{"pid": os.getpid(), "pgid": os.getpgid(0)}}, open({str(facts)!r}, "w"))
        sys.stdin.read()
        time.sleep(60)
        """,
    )

    started = time.monotonic()
    with pytest.raises(worker._WorkerError, match="sink_failed"):
        worker._EgoBrowser(binary=binary, timeout=1.0)(lambda fifo: "script", CANARY_SECRET)
    elapsed = time.monotonic() - started

    recorded = json.loads(facts.read_text())
    assert elapsed < 10.0
    assert recorded["pgid"] == os.getpgid(0)  # inside the broker's group, not a nested one
    with pytest.raises(ProcessLookupError):
        os.kill(recorded["pid"], 0)


def test_hand_off_writes_nothing_anywhere_when_no_reader_ever_arrives(tmp_path: Path) -> None:
    """The value only ever exists in a kernel pipe buffer. Nothing about
    the handoff creates a regular file, so there is no file for it to be
    left in when the fill is refused.
    """
    fifo = tmp_path / "fill"
    os.mkfifo(fifo, 0o600)
    writer = threading.Thread(target=worker._hand_off, args=(str(fifo), CANARY_SECRET), daemon=True)
    writer.start()

    worker._release(str(fifo), writer)

    assert not writer.is_alive()
    assert not fifo.exists()
    assert list(tmp_path.iterdir()) == []


# --- main(): stdin-in, exit-code-out, nothing else ----------------------


def test_main_fills_and_reports_zero_without_printing_anything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    fake_binary: _WriteExecutable,
) -> None:
    """`main` -> `execute` -> the real handoff, with only the browser
    binary swapped: the strongest proof that the documented
    stdin-in/exit-code-out contract holds against the real dispatch path.
    """
    out = tmp_path / "seen.json"
    binary = fake_binary("ego-browser", _fifo_reader_ego(out))
    monkeypatch.setenv(SOURCE_ENV, CANARY_SECRET)
    saved = _feed_stdin(json.dumps(_browser_job()).encode(), tmp_path)
    try:
        code = worker.main(run_ego_browser=worker._EgoBrowser(binary=binary))
    finally:
        _restore_stdin(saved)

    assert code == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    seen = json.loads(out.read_text())
    assert seen["crossed"] == CANARY_SECRET
    assert CANARY_SECRET not in seen["script"]
    assert seen["env"] == []
    assert SOURCE_ENV not in os.environ


@pytest.mark.parametrize("raw", ["", "not json", json.dumps({"version": 1})])
def test_main_reports_one_and_prints_nothing_on_a_malformed_job(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], raw: str
) -> None:
    saved = _feed_stdin(raw.encode(), tmp_path)
    try:
        code = worker.main(run_gws=_refuse_gws, run_ego_browser=_refuse_fill)
    finally:
        _restore_stdin(saved)

    assert code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_never_prints_a_canary_carried_by_a_chained_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def _boom(make_script: _MakeScript, secret: str) -> None:
        try:
            raise RuntimeError(f"leak attempt {secret}")
        except RuntimeError as exc:
            raise worker._WorkerError("sink_failed") from exc

    monkeypatch.setenv(SOURCE_ENV, CANARY_SECRET)
    saved = _feed_stdin(json.dumps(_browser_job()).encode(), tmp_path)
    try:
        code = worker.main(run_ego_browser=_boom)
    finally:
        _restore_stdin(saved)

    assert code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_the_worker_runs_as_the_isolated_script_the_broker_spawns(tmp_path: Path) -> None:
    """The broker spawns this module as ``python -I <abs path>``, which
    drops the script's own directory from ``sys.path`` along with
    PYTHONPATH and user site-packages. An import of a sibling module
    would kill the worker before it ever read stdin -- so run it exactly
    that way, from a directory it has nothing to do with, with a hostile
    PYTHONPATH and HOME, and require the documented refusal rather than a
    crash.
    """
    proc = subprocess.run(
        [sys.executable, "-I", str(Path(worker.__file__).resolve())],
        input=b"not a job",
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(tmp_path), "HOME": str(tmp_path)},
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 1
    assert proc.stdout == b""
    assert proc.stderr == b""


# --- the generated script, executed by real Node against a fake page ---


_HARNESS_JS = r"""
import { existsSync, readFileSync, writeFileSync } from 'node:fs';

const S = JSON.parse(readFileSync(process.env.HARNESS_SCENARIO, 'utf8'));
const EXPECTED = process.env.HARNESS_EXPECT;

const trace = {
  cdp: [], pageInfo: 0, listTaskSpaces: 0, listTabs: 0,
  enters: [], sessions: [], lifecycle: [], inserts: 0,
  filledExactly: null, fifoGone: null, events: [],
};

// --- one fake page, with real node identity ---------------------------
const makeNode = (spec) => ({
  nodeType: spec.nodeType === undefined ? 1 : spec.nodeType,
  tagName: spec.tag === undefined ? 'INPUT' : spec.tag,
  isConnected: spec.connected !== false,
  disabled: !!spec.disabled,
  readOnly: !!spec.readOnly,
  value: spec.value === undefined ? '' : spec.value,
  maxLength: spec.maxLength === undefined ? null : spec.maxLength,
  focusable: spec.focusable !== false,
  style: {
    visibility: spec.visibility === undefined ? 'visible' : spec.visibility,
    display: spec.display === undefined ? 'block' : spec.display,
    opacity: spec.opacity === undefined ? '1' : spec.opacity,
  },
  box: {
    width: spec.width === undefined ? 180 : spec.width,
    height: spec.height === undefined ? 28 : spec.height,
  },
  type: spec.type === undefined ? 'text' : spec.type,
  getAttribute (name) { return name === 'type' ? this.type : null },
  getBoundingClientRect () { return this.box },
  focus () { if (this.focusable) document.activeElement = this },
  dispatchEvent (event) { trace.events.push(event.type); return true },
});

let nodes = (S.nodes || []).map(makeNode);
const document = {
  activeElement: null,
  querySelectorAll (selector) { return selector === S.field ? nodes : [] },
};
const getComputedStyle = (el) => el.style;
class Event { constructor (type) { this.type = type } }

// What the page does to itself while the worker is blocked reading the
// FIFO -- the one window the fill cannot hold still.
const mutate = (op) => {
  const el = nodes[0];
  if (op === 'detach') { el.isConnected = false }
  else if (op === 'blur') { document.activeElement = null }
  else if (op === 'disable') { el.disabled = true }
  else if (op === 'readonly') { el.readOnly = true }
  else if (op === 'hide') { el.style.display = 'none' }
  else if (op === 'retype') { el.type = 'password' }
  else if (op === 'refill') { el.value = 'prefilled' }
  else if (op === 'swap') {
    el.isConnected = false;
    nodes = [makeNode(S.swapWith || { type: 'text' })];
    document.activeElement = nodes[0];
  } else { throw new Error('unknown mutation: ' + op) }
};

// --- a fake CDP that keeps node identity straight ---------------------
const byNodeId = new Map();
const byObjectId = new Map();
let nextNodeId = 100;
let resolvedNode = null;

const dispatch = (method, params) => {
  if (method === 'Page.bringToFront') {
    if (S.failBringToFront) throw new Error('bring to front refused');
    return {};
  }
  if (method === 'Emulation.setDefaultBackgroundColorOverride') return {};
  if (method === 'DOM.getDocument') return { root: { nodeId: 1 } };
  if (method === 'DOM.querySelectorAll') {
    if (params.nodeId !== 1) return {};
    return {
      nodeIds: document.querySelectorAll(params.selector).map((node) => {
        const id = nextNodeId++;
        byNodeId.set(id, node);
        return id;
      }),
    };
  }
  if (method === 'DOM.resolveNode') {
    const node = byNodeId.get(params.nodeId);
    if (!node) return {};
    const objectId = 'obj-' + params.nodeId;
    byObjectId.set(objectId, node);
    resolvedNode = node;
    return { object: { objectId } };
  }
  if (method === 'Runtime.callFunctionOn') {
    const node = byObjectId.get(params.objectId);
    if (!node) return { exceptionDetails: { text: 'no such object' } };
    const fn = new Function(
      'document', 'getComputedStyle', 'Event', 'return (' + params.functionDeclaration + ')',
    )(document, getComputedStyle, Event);
    const args = (params.arguments || []).map((a) => a.value);
    try {
      return { result: { value: fn.apply(node, args) } };
    } catch (error) {
      return { exceptionDetails: { text: String(error) } };
    }
  }
  if (method === 'Input.insertText') {
    trace.inserts += 1;
    const el = document.activeElement;
    if (!el) return {};
    const room = el.maxLength === null ? Infinity : el.maxLength - el.value.length;
    el.value += String(params.text).slice(0, Math.max(0, room));
    return {};
  }
  throw new Error('unexpected cdp method: ' + method);
};

const cdp = async (method, params) => {
  trace.cdp.push(method);
  const result = dispatch(method, params);
  if (S.after && S.after.call === method) mutate(S.after.do);
  return result;
};

// --- ego's injected helpers -------------------------------------------
const js = async () => { throw new Error('the fill must not evaluate page source') };
const pageInfo = async () => {
  const seen = trace.pageInfo;
  trace.pageInfo += 1;
  return S.pageInfo[Math.min(seen, S.pageInfo.length - 1)];
};
const listTaskSpaces = async () => { trace.listTaskSpaces += 1; return S.spaces };
const listTabs = async () => { trace.listTabs += 1; return S.tabs };
const useOrCreateTaskSpace = async (nameOrId) => {
  trace.enters.push(nameOrId);
  const found = S.spaces.find((s) => s.name === nameOrId || s.id === nameOrId);
  return found ? { id: found.id } : { id: S.createdId === undefined ? 9999 : S.createdId };
};
const completeTaskSpace = async (id) => { trace.lifecycle.push(['completeTaskSpace', id]); return { done: true } };
const handOffTaskSpace = async (id) => { trace.lifecycle.push(['handOffTaskSpace', id]); return { done: true } };
const takeOverTaskSpace = async (id) => { trace.lifecycle.push(['takeOverTaskSpace', id]); return { done: true } };
const claimTaskSpace = async (id) => { trace.lifecycle.push(['claimTaskSpace', id]); return { done: true } };
const wait = async () => undefined;

// A stand-in for the hardened toolkit's own session(): refuse a
// same-named space that is not agent-owned, enter, then report the id the
// scenario says ego actually settled on.
const T = {
  async session (name) {
    trace.sessions.push(name);
    const named = S.spaces.filter((s) => s.name === name);
    if (named.length && !named.some((s) => s.ownership === 'agent')) throw new Error('not agent-owned');
    const created = await useOrCreateTaskSpace(name);
    const id = S.sessionId === undefined ? created.id : S.sessionId;
    return {
      id,
      name,
      async tabs () { await useOrCreateTaskSpace(id); return listTabs() },
      async close () { trace.lifecycle.push(['close', id]) },
    };
  },
};

process.on('exit', () => {
  const target = resolvedNode || nodes[0] || null;
  trace.filledExactly = target ? target.value === EXPECTED : null;
  trace.fifoGone = !existsSync(S.fifo);
  writeFileSync(process.env.HARNESS_TRACE, JSON.stringify(trace));
});

const dropped = new Set(S.withoutHelpers || []);
const supplied = {
  js, cdp, pageInfo, listTabs, listTaskSpaces, useOrCreateTaskSpace,
  completeTaskSpace, handOffTaskSpace, takeOverTaskSpace, claimTaskSpace, wait, T,
};
const names = Object.keys(supplied);
new Function(...names, readFileSync(process.env.HARNESS_SCRIPT, 'utf8'))(
  ...names.map((name) => (dropped.has(name) ? undefined : supplied[name])),
);
"""


def _scenario(**overrides: object) -> dict[str, object]:
    scenario: dict[str, object] = {
        "spaces": [{"id": 41, "name": SPACE, "ownership": "agent"}],
        "tabs": [{"id": 7, "url": f"{ORIGIN}/login"}],
        "pageInfo": [{"url": f"{ORIGIN}/login", "title": "Sign in"}],
        "field": FIELD,
        "nodes": [{"type": "password", "value": "stale"}],
    }
    scenario.update(overrides)
    return scenario


@dataclass(frozen=True, slots=True)
class _Run:
    """One execution of the generated script, as facts rather than JSON."""

    code: int
    out: str
    err: str
    cdp: list[str] = field(default_factory=list)
    inserts: int = 0
    page_info: int = 0
    sessions: list[str] = field(default_factory=list)
    enters: list[object] = field(default_factory=list)
    lifecycle: list[object] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    filled_exactly: bool | None = None
    fifo_gone: bool | None = None


class _RunScript(Protocol):
    def __call__(
        self,
        scenario: Mapping[str, object],
        *,
        job: Mapping[str, object] | None = None,
        secret: str | None = CANARY_SECRET,
    ) -> _Run: ...


@pytest.fixture
def run_script(tmp_path: Path) -> _RunScript:
    """Execute the generated script in real Node against the fake runtime,
    with the value crossing a real FIFO written by the module's own
    `_hand_off` -- the same transport the real child is fed by.
    """
    harness = tmp_path / "harness.mjs"
    harness.write_text(_HARNESS_JS)
    runs = 0

    def run(
        scenario: Mapping[str, object],
        *,
        job: Mapping[str, object] | None = None,
        secret: str | None = CANARY_SECRET,
    ) -> _Run:
        nonlocal runs
        runs += 1
        fifo = tmp_path / f"fill{runs}"
        writer: threading.Thread | None = None
        if secret is not None:
            os.mkfifo(fifo, 0o600)
            writer = threading.Thread(target=worker._hand_off, args=(str(fifo), secret), daemon=True)
            writer.start()

        script = tmp_path / f"script{runs}.js"
        script.write_text(worker._browser_script(dict(job or _browser_job()), str(fifo)))
        scenario_path = tmp_path / f"scenario{runs}.json"
        scenario_path.write_text(json.dumps({**scenario, "fifo": str(fifo)}))
        trace_path = tmp_path / f"trace{runs}.json"

        env = dict(os.environ)
        env["HARNESS_SCRIPT"] = str(script)
        env["HARNESS_SCENARIO"] = str(scenario_path)
        env["HARNESS_TRACE"] = str(trace_path)
        env["HARNESS_EXPECT"] = secret or ""
        try:
            proc = subprocess.run(
                [str(_NODE), str(harness)], env=env, capture_output=True, text=True, timeout=30, check=False
            )
        finally:
            if writer is not None:
                worker._release(str(fifo), writer)
        trace: Mapping[str, object] = json.loads(trace_path.read_text()) if trace_path.exists() else {}
        return _Run(
            code=proc.returncode,
            out=proc.stdout,
            err=proc.stderr,
            cdp=list(trace.get("cdp", [])),
            inserts=int(trace.get("inserts", 0)),
            page_info=int(trace.get("pageInfo", 0)),
            sessions=list(trace.get("sessions", [])),
            enters=list(trace.get("enters", [])),
            lifecycle=list(trace.get("lifecycle", [])),
            events=list(trace.get("events", [])),
            filled_exactly=trace.get("filledExactly"),
            fifo_gone=trace.get("fifoGone"),
        )

    return run


_HAPPY_PATH_CDP = [
    "DOM.getDocument",
    "DOM.querySelectorAll",
    "DOM.resolveNode",
    "Runtime.callFunctionOn",
    "Page.bringToFront",
    "Runtime.callFunctionOn",
    "Input.insertText",
    "Runtime.callFunctionOn",
]


@requires_node
def test_script_fills_the_field_with_a_constant_number_of_cdp_calls(run_script: _RunScript) -> None:
    """The happy path, end to end, in the real Node: the field is resolved
    to one object and armed, the document is force-armed, the value
    crosses a real FIFO, the same object is reproven, the value goes in
    with one trusted insertion, and the readback runs against that same
    object -- on a fixed CDP sequence, so a long value costs exactly what
    a short one does.
    """
    run = run_script(_scenario())

    assert run.code == 0
    assert run.out == ""
    assert run.err == ""
    assert run.cdp == _HAPPY_PATH_CDP
    assert run.inserts == 1
    assert run.filled_exactly is True
    assert run.fifo_gone is True  # single use: the script unlinks it
    assert run.page_info == 3  # before the field, after the handoff, after the value
    assert run.events == ["input"]  # the stale value was cleared

@requires_node
def test_script_uses_the_nonactivating_compositor_arm_fallback(run_script: _RunScript) -> None:
    run = run_script(_scenario(failBringToFront=True))

    arm = _HAPPY_PATH_CDP.index("Page.bringToFront")
    assert run.code == 0
    assert run.cdp == [
        *_HAPPY_PATH_CDP[: arm + 1],
        "Emulation.setDefaultBackgroundColorOverride",
        *_HAPPY_PATH_CDP[arm + 1 :],
    ]
    assert run.inserts == 1
    assert run.filled_exactly is True
    assert run.fifo_gone is True


@requires_node
def test_script_enters_the_caller_space_and_never_creates_completes_or_hands_one_off(
    run_script: _RunScript,
) -> None:
    """The space belongs to the caller's live, already-authenticated
    session. Entering it is the whole permitted interaction: creating one
    would fill into a blank space, and completing, claiming, taking over
    or handing one off would reach into a session this process was only
    lent.
    """
    run = run_script(_scenario())

    assert run.code == 0
    assert run.sessions == [SPACE]  # the ambient hardened T, not a fresh import
    assert run.enters == [SPACE, 41]  # entered by name, then re-selected by id
    assert run.lifecycle == []


@requires_node
@pytest.mark.parametrize(
    "spaces",
    [
        [],
        [{"id": 41, "name": "different-space", "ownership": "agent"}],
        [{"id": 41, "name": SPACE, "ownership": "agent"}, {"id": 42, "name": SPACE, "ownership": "agent"}],
        [{"id": 41, "name": SPACE, "ownership": "user"}],
        [{"id": 41, "name": SPACE, "ownership": "agentDelegatedToUser"}],
        [{"id": 41, "name": SPACE, "ownership": "agent"}, {"id": 42, "name": SPACE, "ownership": "user"}],
        [{"name": SPACE, "ownership": "agent"}],  # no numeric id
    ],
    ids=["missing", "other-name", "duplicate", "user-owned", "delegated", "duplicate-mixed", "no-id"],
)
def test_script_refuses_any_space_that_is_not_exactly_one_agent_owned_match(
    run_script: _RunScript, spaces: list[dict[str, object]]
) -> None:
    run = run_script(_scenario(spaces=spaces))

    assert run.code == 1
    assert run.out == ""
    assert run.err == ""
    assert run.cdp == []
    assert run.inserts == 0
    assert run.enters == []  # nothing was created to stand in for the missing one
    assert run.lifecycle == []
    assert run.fifo_gone is False  # a refused fill never even reads it


@requires_node
def test_script_refuses_when_entering_lands_on_a_different_space_id(run_script: _RunScript) -> None:
    """T.session creates on a miss and resolves same-name races by
    adopting the lowest agent-owned id, so an id that is not the one just
    vetted means this is a different space -- and one this process must
    not clean up either, since it cannot prove whose it is.
    """
    run = run_script(_scenario(sessionId=9999))

    assert run.code == 1
    assert run.cdp == []
    assert run.lifecycle == []


@requires_node
def test_script_refuses_a_space_with_no_live_tab(run_script: _RunScript) -> None:
    run = run_script(_scenario(tabs=[]))

    assert run.code == 1
    assert run.cdp == []


@requires_node
@pytest.mark.parametrize(
    "page",
    [
        [{"dialog": "confirm"}],
        [{"url": "https://evil.example/login"}],
        [{"url": "http://accounts.acme.example/login"}],
        [{"url": f"{ORIGIN}.evil.example/login"}],
        [{"title": "no url at all"}],
        [{"url": "not a url"}],
    ],
    ids=["dialog", "other-origin", "downgraded-scheme", "suffix-origin", "no-url", "unparseable"],
)
def test_script_refuses_a_page_that_is_not_exactly_an_allowlisted_origin(
    run_script: _RunScript, page: list[dict[str, object]]
) -> None:
    run = run_script(_scenario(pageInfo=page))

    assert run.code == 1
    assert run.cdp == []
    assert run.inserts == 0
    assert run.fifo_gone is False


@requires_node
@pytest.mark.parametrize(
    "nodes",
    [
        [],
        [{"type": "password"}, {"type": "password"}],
        [{"type": "password", "tag": "TEXTAREA"}],
        [{"type": "password", "tag": "DIV"}],
        [{"type": "password", "nodeType": 3}],
        [{"type": "password", "connected": False}],
        [{"type": "password", "disabled": True}],
        [{"type": "password", "readOnly": True}],
        [{"type": "text"}],
        [{"type": "hidden"}],
        [{"type": None}],
        [{"type": "password", "width": 0}],
        [{"type": "password", "height": 0}],
        [{"type": "password", "visibility": "hidden"}],
        [{"type": "password", "display": "none"}],
        [{"type": "password", "opacity": "0"}],
        [{"type": "password", "focusable": False}],
    ],
    ids=[
        "no-match",
        "duplicate",
        "textarea",
        "div",
        "not-an-element",
        "detached",
        "disabled",
        "readonly",
        "wrong-type",
        "hidden-type",
        "untyped",
        "zero-width",
        "zero-height",
        "visibility-hidden",
        "display-none",
        "transparent",
        "refuses-focus",
    ],
)
def test_script_refuses_any_field_that_is_not_one_writable_visible_expected_input(
    run_script: _RunScript, nodes: list[dict[str, object]]
) -> None:
    run = run_script(_scenario(nodes=nodes))

    assert run.code == 1
    assert run.out == ""
    assert run.err == ""
    assert run.inserts == 0
    assert run.fifo_gone is False  # refused before the value was ever collected


@requires_node
@pytest.mark.parametrize("mutation", ["detach", "swap", "blur", "disable", "readonly", "hide", "retype", "refill"])
def test_script_refuses_when_the_field_changed_while_the_handoff_was_blocking(
    run_script: _RunScript, mutation: str
) -> None:
    """Reading the FIFO waits on another process, so it is the one window
    in which the page can move on. The identity, focus and shape of the
    exact object that was armed are all reproven immediately before the
    single insertion -- a swapped, detached, blurred, disabled, readonly,
    hidden, retyped or refilled field never receives the value.
    """
    job = _browser_job(kind="totp", field="#otp")
    scenario = _scenario(
        field="#otp",
        nodes=[{"type": "text"}],
        after={"call": "Page.bringToFront", "do": mutation},
    )

    run = run_script(scenario, job=job, secret="123456")

    assert run.code == 1
    assert run.inserts == 0
    assert run.fifo_gone is True  # the value was collected, and then went nowhere
    assert run.cdp[-1] == "Runtime.callFunctionOn"


@requires_node
@pytest.mark.parametrize("kind", ["totp", "gmail_otp"])
def test_script_refuses_a_masked_field_for_a_one_time_code(run_script: _RunScript, kind: str) -> None:
    job = _browser_job(kind=kind, field="#otp")

    masked = run_script(_scenario(field="#otp", nodes=[{"type": "password"}]), job=job, secret="123456")

    assert masked.code == 1
    assert masked.inserts == 0


@requires_node
@pytest.mark.parametrize("accepted", ["text", "tel", "number", None])
def test_script_accepts_the_plain_field_shapes_a_one_time_code_belongs_in(
    run_script: _RunScript, accepted: str | None
) -> None:
    job = _browser_job(kind="gmail_otp", field="#otp")

    run = run_script(_scenario(field="#otp", nodes=[{"type": accepted}]), job=job, secret="123456")

    assert run.code == 0
    assert run.filled_exactly is True


@requires_node
def test_script_refuses_a_navigation_that_lands_before_the_value_goes_in(run_script: _RunScript) -> None:
    """The origin is reproven after the handoff and again at the end. A
    page that navigated while the handoff was blocking is no longer the
    page the field was validated on.
    """
    run = run_script(_scenario(pageInfo=[{"url": f"{ORIGIN}/login"}, {"url": "https://evil.example/login"}]))

    assert run.code == 1
    assert run.inserts == 0


@requires_node
def test_script_refuses_a_navigation_that_lands_after_the_value_went_in(run_script: _RunScript) -> None:
    run = run_script(
        _scenario(
            pageInfo=[
                {"url": f"{ORIGIN}/login"},
                {"url": f"{ORIGIN}/login"},
                {"url": "https://evil.example/done"},
            ]
        )
    )

    assert run.code == 1
    assert run.inserts == 1  # it went in, and the fill is still refused
    assert run.cdp[-1] == "Runtime.callFunctionOn"


@requires_node
def test_script_refuses_when_the_field_does_not_hold_exactly_what_was_inserted(run_script: _RunScript) -> None:
    """A maxlength that silently truncates is the exact shape of a fill
    that reports success and logs the user in with nothing. The readback is
    the only thing standing between that and an exit code of 0.
    """
    run = run_script(_scenario(nodes=[{"type": "password", "maxLength": 8}]))

    assert run.code == 1
    assert run.inserts == 1
    assert run.filled_exactly is False
    assert run.cdp[-1] == "Runtime.callFunctionOn"


@requires_node
@pytest.mark.parametrize("secret", ["", None], ids=["empty", "absent"])
def test_script_refuses_when_the_handoff_carried_nothing(run_script: _RunScript, secret: str | None) -> None:
    """An empty FIFO -- a writer that closed without writing -- is not a
    value, and neither is a FIFO that is not there at all.
    """
    run = run_script(_scenario(), secret=secret)

    assert run.code == 1
    assert run.inserts == 0
    assert run.cdp == _HAPPY_PATH_CDP[: _HAPPY_PATH_CDP.index("Page.bringToFront") + 1]


@requires_node
@pytest.mark.parametrize("missing", ["cdp", "pageInfo", "listTaskSpaces", "listTabs"])
def test_script_refuses_a_runtime_missing_a_helper_it_needs(run_script: _RunScript, missing: str) -> None:
    run = run_script(_scenario(withoutHelpers=[missing]))

    assert run.code == 1
    assert run.out == ""
    assert run.err == ""
    assert run.inserts == 0
