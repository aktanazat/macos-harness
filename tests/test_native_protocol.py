"""CI-safe protocol tests for the native agent client (no Swift, no AX).

These exercise ``macos_harness.native.NativeClient`` against a small,
fully-scripted fake agent that speaks the real NDJSON-over-``AF_UNIX``
wire protocol from a background thread in this process, over a
``socket.socketpair()`` -- the exact channel shape ``agent.launch()``
hands a real client in production, never a filesystem socket path. No
subprocess, no Swift toolchain, no Accessibility permission is required.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from typing import Any

import pytest

import macos_harness.native as native_module
from macos_harness.errors import ErrorCode
from macos_harness.macos import (
    AccessibilityPermissionError,
    ApplicationNotFoundError,
    FocusChangedError,
    MacOS,
    MacOSError,
)
from macos_harness.native import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    NativeClient,
    NativeConnectionError,
    NativeProtocolError,
    _NativeHandle,
)
from macos_harness.receipts import Acted, OperationError, Outcome, equals

#: Sentinel script entries.
CLOSE = object()  # drop the connection without responding
HANG = object()  # accept the line but never respond and never close


class _FakeAgent:
    """A scriptable stand-in for the native agent's NDJSON socket.

    Backed by a ``socket.socketpair()`` -- exactly the channel shape a
    real ``NativeClient(sock=...)`` adopts from ``agent.launch()`` -- so
    there is never a listen/accept step or a filesystem socket path: one
    fixed pair, one connection, matching production exactly.

    ``script`` supplies exactly one outcome per line the fake agent
    reads, in order: raw response bytes to send back, ``CLOSE`` to drop
    the connection without responding, or ``HANG`` to accept the line
    but never respond and never close (used to exercise the client's own
    timeout). Every fully-read line (valid JSON or not) is recorded in
    ``received`` before its outcome runs.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.received: list[bytes] = []
        self.server_sock, self.client_sock = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM
        )
        self.server_sock.settimeout(5.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        index = 0
        buffer = bytearray()
        try:
            while index < len(self._script):
                newline = buffer.find(b"\n")
                while newline == -1:
                    chunk = self.server_sock.recv(65536)
                    if not chunk:
                        return
                    buffer += chunk
                    newline = buffer.find(b"\n")
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                self.received.append(line)
                outcome = self._script[index]
                index += 1
                if outcome is CLOSE:
                    return
                if outcome is HANG:
                    time.sleep(1.0)
                    return
                self.server_sock.sendall(outcome)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                self.server_sock.close()

    def join(self, timeout: float = 5.0) -> None:
        with contextlib.suppress(RuntimeError):
            self._thread.join(timeout=timeout)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.client_sock.close()
        with contextlib.suppress(OSError):
            self.server_sock.close()
        with contextlib.suppress(RuntimeError):
            self._thread.join(timeout=2.0)


def _client(agent: _FakeAgent, *, timeout: float = 2.0, expected_pid: int = 4242) -> NativeClient:
    """A ``NativeClient`` adopting ``agent``'s socketpair end, with both
    the connect and request timeouts set to the same value by default --
    matching the old single-``timeout=`` call sites this replaces."""
    return NativeClient(
        sock=agent.client_sock,
        connect_timeout=timeout,
        request_timeout=timeout,
        expected_pid=expected_pid,
    )


def _ok(request_id: int, result: dict[str, Any]) -> bytes:
    payload = {"id": request_id, "ok": True, "result": result}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _err(
    request_id: int, code: str, message: str, ax_error: int | None = None
) -> bytes:
    error: dict[str, Any] = {"code": code, "message": message}
    if ax_error is not None:
        error["ax_error"] = ax_error
    payload = {"id": request_id, "ok": False, "error": error}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _ping_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "agent_version": "0.1.0",
        "pid": 4242,
        "trusted": True,
        "uptime_s": 12.5,
    }
    result.update(overrides)
    return result


def test_handshake_rejects_protocol_major_mismatch() -> None:
    agent = _FakeAgent([_ok(1, _ping_result(protocol=2))])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="protocol"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("permission.accessibility", AccessibilityPermissionError),
        ("app.not_found", ApplicationNotFoundError),
        ("focus.changed", FocusChangedError),
        ("app.ambiguous", MacOSError),
        ("ax.error", MacOSError),
        ("element.unknown", MacOSError),
        ("timeout", MacOSError),
        ("bad_request", MacOSError),
        ("unsupported_op", MacOSError),
    ],
)
def test_error_codes_map_to_expected_exception(
    code: str, expected: type[MacOSError]
) -> None:
    ax_error = {"ax.error": -25200, "timeout": -25204}.get(code)
    agent = _FakeAgent(
        [_ok(1, _ping_result()), _err(2, code, f"boom: {code}", ax_error=ax_error)]
    )
    try:
        client = _client(agent)
        with pytest.raises(expected, match="boom") as exc_info:
            client.query({"reset_elements": True})
        assert exc_info.value.code == code  # exact wire code preserved end-to-end
        if ax_error is None:
            assert "ax_error" not in exc_info.value.details
        else:
            assert exc_info.value.details["ax_error"] == ax_error
        assert (
            client.connected is True
        )  # error responses don't tear down the connection
    finally:
        agent.close()


def test_malformed_json_line_raises_and_connection_survives() -> None:
    agent = _FakeAgent(
        [
            _ok(1, _ping_result()),
            b"this is not json\n",
            _ok(3, {"apps": []}),
        ]
    )
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="Malformed JSON"):
            client.list_apps()
        assert client.connected is True
        assert client.generation == 1
        assert client.list_apps() == []
        assert client.generation == 1
    finally:
        agent.close()


def test_oversized_line_rejected_without_oom() -> None:
    oversized = b"x" * (MAX_LINE_BYTES + 2 * 1024 * 1024)  # no trailing newline
    agent = _FakeAgent([_ok(1, _ping_result()), oversized])
    try:
        client = _client(agent, timeout=5.0)
        started = time.monotonic()
        with pytest.raises(NativeProtocolError, match="exceeded"):
            client.list_apps()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, (
            "client must bail out without buffering the full oversized frame"
        )
        assert client.connected is False
    finally:
        agent.close()


def test_response_line_at_exactly_the_cap_including_newline_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read-side cap counts the response's trailing newline: a line
    of exactly ``MAX_LINE_BYTES`` bytes *including* that newline must be
    accepted as a line -- rejected only for not being valid JSON, never
    for its size.

    The patched cap (4096) is deliberately well above the real
    handshake ping's own request/response frames (well under 200 bytes
    each) so the handshake itself still succeeds normally; only the
    scripted ``list_apps`` response after it is sized to the boundary.
    """
    monkeypatch.setattr(native_module, "MAX_LINE_BYTES", 4096)
    exact = b"x" * 4095 + b"\n"  # 4096 bytes total, including the newline
    agent = _FakeAgent([_ok(1, _ping_result()), exact])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="Malformed JSON"):
            client.list_apps()
        assert client.connected is True
    finally:
        agent.close()


def test_response_line_one_byte_over_the_cap_including_newline_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_module, "MAX_LINE_BYTES", 4096)
    over = b"x" * 4096 + b"\n"  # 4097 bytes total, including the newline: one over
    agent = _FakeAgent([_ok(1, _ping_result()), over])
    try:
        client = _client(agent)
        with pytest.raises(NativeConnectionError, match="exceeded"):
            client.list_apps()
        assert client.connected is False
    finally:
        agent.close()


def test_oversized_request_is_rejected_before_sending() -> None:
    agent = _FakeAgent([_ok(1, _ping_result())])
    try:
        client = _client(agent)
        client.connect()
        huge_text = "x" * (MAX_LINE_BYTES + 1024)
        with pytest.raises(NativeProtocolError, match="over the"):
            client.query({"text": huge_text, "reset_elements": False})
        # Nothing was sent, so the connection is still perfectly usable.
        assert client.connected is True
    finally:
        agent.close()


def test_write_side_cap_counts_the_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write-side cap counts the request's trailing newline: a
    request whose encoded frame is exactly ``MAX_LINE_BYTES`` bytes
    *including* that newline must be sent, not rejected -- one byte more
    must be rejected before ever touching the socket.

    The patched cap (4096) stays well above the handshake ping's own
    ~110-byte response so ``connect()`` still succeeds normally.
    """
    monkeypatch.setattr(native_module, "MAX_LINE_BYTES", 4096)
    agent = _FakeAgent([_ok(1, _ping_result()), _ok(2, {"matches": []})])
    try:
        client = _client(agent)
        client.connect()

        def _frame_len(text: str) -> int:
            body = json.dumps(
                {
                    "v": PROTOCOL_VERSION,
                    "id": 2,
                    "op": "ax_query",
                    "params": {"text": text, "reset_elements": False},
                },
                separators=(",", ":"),
            ).encode("utf-8")
            return len(body) + 1  # + trailing newline

        text = ""
        while _frame_len(text) < 4096:
            text += "x"
        assert _frame_len(text) == 4096

        client.query({"text": text, "reset_elements": False})  # exactly at cap

        with pytest.raises(NativeProtocolError, match="over the"):
            client.query({"text": text + "x", "reset_elements": False})  # one over
        assert client.connected is True
    finally:
        agent.close()


def test_request_after_handshake_uses_request_timeout_not_connect_timeout() -> None:
    """A slow post-handshake response must time out against
    ``request_timeout``, not the (here, much longer) ``connect_timeout``
    the handshake itself used -- the socket's own timeout is swapped the
    moment the handshake succeeds."""
    agent = _FakeAgent([_ok(1, _ping_result()), HANG])
    try:
        client = NativeClient(
            sock=agent.client_sock,
            connect_timeout=30.0,
            request_timeout=0.2,
            expected_pid=4242,
        )
        client.connect()  # handshake succeeds fast, well within either timeout
        started = time.monotonic()
        with pytest.raises(NativeConnectionError):
            client.list_apps()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, (
            "must time out against request_timeout (0.2s), not "
            "connect_timeout (30s)"
        )
    finally:
        agent.close()


def test_response_id_mismatch_is_rejected_and_closes() -> None:
    agent = _FakeAgent([_ok(1, _ping_result()), _ok(999, {"apps": []})])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="did not match"):
            client.list_apps()
        assert client.connected is False
    finally:
        agent.close()


def test_ok_false_without_error_object_raises_clear_error() -> None:
    bad = (json.dumps({"id": 2, "ok": False}) + "\n").encode()
    agent = _FakeAgent([_ok(1, _ping_result()), bad])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="Malformed error envelope"):
            client.list_apps()
    finally:
        agent.close()


def test_response_missing_ok_field_is_a_protocol_error() -> None:
    bad = (json.dumps({"id": 2, "result": {}}) + "\n").encode()
    agent = _FakeAgent([_ok(1, _ping_result()), bad])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError, match="Malformed response envelope"):
            client.list_apps()
    finally:
        agent.close()


def test_no_retry_after_send_when_response_is_lost() -> None:
    agent = _FakeAgent([_ok(1, _ping_result()), CLOSE])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError):
            client.press({"text": "Not Now"})
        agent.join(timeout=2.0)
        # Handshake ping + exactly one press attempt: the lost response was
        # never resent.
        assert len(agent.received) == 2
        assert client.connected is False
    finally:
        agent.close()


def test_list_apps_is_sorted_client_side_regardless_of_wire_order() -> None:
    scrambled = [
        {"name": "Zebra", "bundle_id": "z", "pid": 1, "path": None},
        {"name": "apple", "bundle_id": "a", "pid": 5, "path": None},
        {"name": "Apple", "bundle_id": "a2", "pid": 2, "path": None},
    ]
    agent = _FakeAgent([_ok(1, _ping_result()), _ok(2, {"apps": scrambled})])
    try:
        client = _client(agent)
        apps = client.list_apps()
        assert [item["pid"] for item in apps] == [2, 5, 1]
    finally:
        agent.close()


def test_stale_generation_handle_rejected_without_wire_call() -> None:
    """A native client can only ever adopt its socket once, so a fresh
    generation here comes from a ``reset_elements=True`` query -- exactly
    like ``MacOS``'s own ``_elements`` reset -- not a reconnect."""
    agent = _FakeAgent(
        [
            _ok(1, _ping_result()),  # connect() handshake -> generation 1
            _ok(2, {"matches": []}),  # query(reset_elements=True) -> generation 2
        ]
    )
    try:
        client = _client(agent)
        client.connect()
        assert client.generation == 1
        stale_handle = _NativeHandle(client, 7, client.generation)

        client.query({"reset_elements": True})
        assert client.generation == 2

        with pytest.raises(MacOSError, match="stale"):
            client.get(stale_handle, "AXValue")

        agent.join(timeout=2.0)
        # Only the handshake ping and the query ever reached the wire; the
        # stale get() was rejected before touching the socket.
        assert len(agent.received) == 2
    finally:
        agent.close()


def test_handle_from_different_client_rejected_without_wire_call() -> None:
    agent = _FakeAgent([_ok(1, _ping_result())])
    try:
        client_a = _client(agent)
        client_a.connect()
        handle = _NativeHandle(client_a, 3, client_a.generation)

        # client_b never actually needs to talk to anything: the "belongs
        # to a different client" check is purely client-side, before any
        # socket I/O, so an unscripted, never-served socketpair end
        # suffices to prove no wire call was ever attempted.
        unused_server, unused_client = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM
        )
        client_b = NativeClient(
            sock=unused_client,
            connect_timeout=2.0,
            request_timeout=2.0,
            expected_pid=4242,
        )
        try:
            with pytest.raises(MacOSError, match="different") as exc_info:
                client_b.get(handle, "AXValue")
            assert exc_info.value.code == ErrorCode.ELEMENT_UNKNOWN
            assert exc_info.value.details["handle"] == 3
            assert client_b.connected is False
        finally:
            client_b.close()
            unused_server.close()
    finally:
        agent.close()


def test_full_operation_surface_round_trips() -> None:
    match = {"handle": 11, "role": "AXButton", "title": "Not Now"}
    responses = [
        _ok(1, _ping_result()),  # connect handshake
        _ok(2, {"matches": [match]}),  # query
        _ok(3, {"value": "hello"}),  # get
        _ok(
            4, {"attributes": {"AXValue": "hello", "AXTitle": "Not Now"}}
        ),  # get_attributes
        _ok(5, {}),  # set
        _ok(6, {}),  # perform
        _ok(7, {"match": match}),  # press
    ]
    agent = _FakeAgent(responses)
    try:
        client = _client(agent)
        matches = client.query({"text": "Not Now", "reset_elements": True})
        assert matches == [match]
        handle = _NativeHandle(client, matches[0]["handle"], client.generation)

        assert client.get(handle, "AXValue") == "hello"
        assert client.get_attributes(handle, ["AXValue", "AXTitle"]) == {
            "AXValue": "hello",
            "AXTitle": "Not Now",
        }
        client.set(handle, "AXValue", "world")
        client.perform(handle, "AXPress")
        pressed = client.press({"text": "Not Now"})
        assert pressed == match
        with pytest.raises(MacOSError, match="stale") as exc_info:
            client.get(handle, "AXValue")
        assert exc_info.value.code == ErrorCode.ELEMENT_UNKNOWN
        assert exc_info.value.details["handle"] == 11

        query_request = json.loads(agent.received[1])
        assert query_request["v"] == PROTOCOL_VERSION
        assert query_request["op"] == "ax_query"
        assert query_request["params"] == {"text": "Not Now", "reset_elements": True}

        get_request = json.loads(agent.received[2])
        assert get_request["op"] == "ax_element_get_value"
        assert get_request["params"] == {"handle": 11, "attribute": "AXValue"}

        get_attrs_request = json.loads(agent.received[3])
        assert get_attrs_request["op"] == "ax_element_get"
        assert get_attrs_request["params"] == {
            "handle": 11,
            "attributes": ["AXValue", "AXTitle"],
        }

        set_request = json.loads(agent.received[4])
        assert set_request["op"] == "ax_element_set"
        assert set_request["params"] == {
            "handle": 11,
            "attribute": "AXValue",
            "value": "world",
        }

        perform_request = json.loads(agent.received[5])
        assert perform_request["op"] == "ax_element_perform"
        assert perform_request["params"] == {"handle": 11, "action": "AXPress"}

        press_request = json.loads(agent.received[6])
        assert press_request["op"] == "ax_press"
        assert press_request["params"] == {"text": "Not Now"}
    finally:
        agent.close()


@pytest.mark.parametrize(
    "result",
    [
        {},
        # The best-effort batch shape, where a refused slot is a `null`:
        # the one answer a required read must never accept as a value.
        {"attributes": {"AXValue": None}},
    ],
    ids=["empty", "batch-shape"],
)
def test_single_read_result_without_a_value_key_is_a_protocol_error(
    result: dict[str, object],
) -> None:
    agent = _FakeAgent([_ok(1, _ping_result()), _ok(2, result)])
    try:
        client = _client(agent)
        client.connect()
        handle = _NativeHandle(client, 11, client.generation)
        with pytest.raises(NativeProtocolError, match="ax_element_get_value"):
            client.get(handle, "AXValue")
    finally:
        agent.close()


def test_single_read_explicit_null_is_a_value() -> None:
    agent = _FakeAgent([_ok(1, _ping_result()), _ok(2, {"value": None})])
    try:
        client = _client(agent)
        client.connect()
        handle = _NativeHandle(client, 11, client.generation)
        assert client.get(handle, "AXValue") is None
    finally:
        agent.close()


def test_single_read_against_an_older_agent_is_refused_not_downgraded() -> None:
    """An agent binary that predates ``ax_element_get_value`` answers
    ``unsupported_op``; the client surfaces that verbatim rather than
    retrying through the lossy batch op it could have used instead."""
    agent = _FakeAgent(
        [
            _ok(1, _ping_result()),
            _err(2, "unsupported_op", 'Unsupported operation "ax_element_get_value"'),
        ]
    )
    try:
        client = _client(agent)
        client.connect()
        handle = _NativeHandle(client, 11, client.generation)
        with pytest.raises(MacOSError, match="Unsupported operation") as exc_info:
            client.get(handle, "AXValue")
        assert exc_info.value.code == "unsupported_op"
        agent.join(timeout=2.0)
        assert [json.loads(line)["op"] for line in agent.received] == [
            "ping",
            "ax_element_get_value",
        ]
    finally:
        agent.close()


# --- consumer consequences: a refused single read reaches mac.do ----------


def _native_mac(
    monkeypatch: pytest.MonkeyPatch, agent: _FakeAgent
) -> tuple[MacOS, int]:
    """A real ``MacOS`` whose one interned element is a live native handle
    over ``agent``'s socket, with app resolution, target discovery, and the
    focus guard pinned so the only thing between ``mac.do`` and the wire is
    the production read path under test."""
    client = _client(agent)
    client.connect()
    mac = MacOS(backend="native")
    index = mac._remember_element(_NativeHandle(client, 11, client.generation))
    app_info = {"pid": 41, "name": "Demo", "bundle_id": "com.example.demo"}
    match = {
        "element_index": index,
        "role": "AXCheckBox",
        "title": "Enabled",
        "description": None,
        "identifier": None,
        "actions": ["AXPress"],
        "app": app_info,
    }
    monkeypatch.setattr(mac, "_resolve_app", lambda query: (object(), dict(app_info)))
    monkeypatch.setattr(mac, "_ensure_accessibility", lambda: None)
    monkeypatch.setattr(mac, "ax_wait", lambda **kwargs: dict(match))
    monkeypatch.setattr(mac, "_frontmost_app", lambda: dict(app_info))
    monkeypatch.setattr(mac, "_guard_focus", lambda before, target_pid, operation: None)
    return mac, index


def test_toggle_never_presses_a_control_whose_state_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrong-direction bug: a refused ``AXValue`` read once arrived as
    ``None``, read as "currently off", and ``toggle(False)`` reported
    ``already`` (or ``toggle(True)`` pressed an already-on control off)."""
    agent = _FakeAgent(
        [
            _ok(1, _ping_result()),
            _err(2, "timeout", "Read AXValue from element 11 failed", ax_error=-25204),
        ]
    )
    try:
        mac, _ = _native_mac(monkeypatch, agent)
        with pytest.raises(OperationError) as caught:
            mac.do.toggle(False, app="Demo", text="Enabled")
        receipt = caught.value.receipt
        assert receipt.outcome is Outcome.FAILED
        assert receipt.acted is Acted.NO
        assert receipt.changed is False
        assert receipt.verified is False
        assert receipt.error is not None
        assert receipt.error["code"] == "timeout"
        assert receipt.error["details"]["ax_error"] == -25204
        agent.join(timeout=2.0)
        assert [json.loads(line)["op"] for line in agent.received] == [
            "ping",
            "ax_element_get_value",
        ]
    finally:
        agent.close()


def test_press_with_a_null_expectation_fails_on_a_refused_read_and_replays_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``equals(value=None)`` once verified against the ``None`` a refused
    read produced. The press still happened, so the receipt keeps
    ``acted=yes`` with ``verified=False``, and recalling the token returns
    that failure without pressing again."""
    agent = _FakeAgent(
        [
            _ok(1, _ping_result()),
            _err(2, "timeout", "Read AXValue from element 11 failed", ax_error=-25204),
        ]
    )
    try:
        mac, index = _native_mac(monkeypatch, agent)
        presses: list[object] = []
        pressed = {
            "element_index": index,
            "role": "AXButton",
            "title": "Save",
            "actions": ["AXPress"],
            "app": {"pid": 41, "name": "Demo", "bundle_id": "com.example.demo"},
        }
        monkeypatch.setattr(
            mac, "ax_press", lambda **kwargs: presses.append(kwargs) or dict(pressed)
        )
        with pytest.raises(OperationError) as caught:
            mac.do.press(
                app="Demo",
                text="Save",
                once="null-witness",
                postcondition=equals("Enabled", value=None),
            )
        receipt = caught.value.receipt
        assert receipt.outcome is Outcome.FAILED
        assert receipt.acted is Acted.YES
        assert receipt.verified is False
        assert receipt.changed is None
        assert receipt.error is not None
        assert receipt.error["code"] == "timeout"
        assert len(presses) == 1

        with pytest.raises(OperationError) as replayed:
            mac.do.recall("null-witness")
        assert replayed.value.receipt.replayed is True
        assert replayed.value.receipt.outcome is Outcome.FAILED
        assert replayed.value.receipt.verified is False
        assert len(presses) == 1
    finally:
        agent.close()


# --- endpoint binding: expected_pid --------------------------------------


def test_handshake_accepts_matching_expected_pid() -> None:
    # `client.connect()` (not `.ping()`) makes exactly one dispatch — the
    # handshake ping `expected_pid` is actually checked against — so one
    # scripted response is enough; `.ping()` on a fresh connection would
    # dispatch a *second*, unscripted request right after it.
    agent = _FakeAgent([_ok(1, _ping_result(pid=4242))])
    try:
        client = _client(agent, expected_pid=4242)
        client.connect()
        assert client.connected is True
    finally:
        agent.close()


def test_handshake_rejects_mismatched_expected_pid() -> None:
    agent = _FakeAgent([_ok(1, _ping_result(pid=4242))])
    try:
        client = _client(agent, expected_pid=9999)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_handshake_rejects_missing_pid_field_when_expected_pid_given() -> None:
    payload = {
        "protocol": PROTOCOL_VERSION,
        "agent_version": "0.1.0",
        "trusted": True,
        "uptime_s": 1.0,
    }  # no "pid" key at all
    agent = _FakeAgent([_ok(1, payload)])
    try:
        client = _client(agent, expected_pid=4242)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_handshake_rejects_non_integer_numeric_pid() -> None:
    # JSON's decimal `4242.0` decodes to a Python float, which compares
    # equal to the int 4242 under `==` -- the exact-type check must still
    # reject it rather than trust a naive equality comparison.
    agent = _FakeAgent([_ok(1, _ping_result(pid=4242.0))])
    try:
        client = _client(agent, expected_pid=4242)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
    finally:
        agent.close()


def test_handshake_rejects_boolean_pid_masquerading_as_pid_one() -> None:
    # JSON's `true` decodes to Python's `True`, which compares equal to
    # the int 1 under `==`. A naive `hello["pid"] == expected_pid` check
    # would wrongly accept it when `expected_pid=1`; the type-safe check
    # must not.
    agent = _FakeAgent([_ok(1, _ping_result(pid=True))])
    try:
        client = _client(agent, expected_pid=1)
        with pytest.raises(NativeProtocolError, match="pid"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()


def test_close_before_connect_releases_the_adopted_socket() -> None:
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = NativeClient(sock=client_sock, expected_pid=4242)
    try:
        client.close()
        assert client_sock.fileno() == -1
    finally:
        server_sock.close()


# --- endpoint binding: connect-failure vs. post-dispatch hard failures ----


def test_connect_failure_raises_the_auto_fallback_eligible_subclass() -> None:
    """``backend="auto"`` callers must be able to catch exactly this: a
    pure failure to ever obtain a usable response, before any request --
    even the handshake ping -- was ever answered."""
    server_sock, client_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    server_sock.close()  # nothing will ever answer the handshake ping
    client = NativeClient(
        sock=client_sock, connect_timeout=1.0, request_timeout=1.0, expected_pid=4242
    )
    with pytest.raises(NativeConnectionError):
        client.ping()
    assert client.connected is False


def test_protocol_mismatch_is_not_the_connection_error_subclass() -> None:
    """A protocol-version mismatch happens *after* a real request reached
    the agent; it must stay a plain ``NativeProtocolError`` a
    ``backend="auto"`` caller narrowly catching ``NativeConnectionError``
    will never swallow."""
    agent = _FakeAgent([_ok(1, _ping_result(protocol=2))])
    try:
        client = _client(agent)
        with pytest.raises(NativeProtocolError) as excinfo:
            client.ping()
        assert not isinstance(excinfo.value, NativeConnectionError)
    finally:
        agent.close()


def test_expected_pid_mismatch_is_not_the_connection_error_subclass() -> None:
    agent = _FakeAgent([_ok(1, _ping_result(pid=4242))])
    try:
        client = _client(agent, expected_pid=9999)
        with pytest.raises(NativeProtocolError) as excinfo:
            client.ping()
        assert not isinstance(excinfo.value, NativeConnectionError)
    finally:
        agent.close()


# --- fork / process-identity safety --------------------------------------


def test_client_used_from_a_different_pid_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the one thing a real ``fork()`` would do: hand this
    object to a "process" whose pid no longer matches the one that
    constructed it. No real ``os.fork()`` is used -- the creator pid is
    monkeypatched directly, which exercises exactly the same check
    without forking the test runner itself."""
    agent = _FakeAgent([_ok(1, _ping_result())])
    try:
        client = _client(agent)
        monkeypatch.setattr(client, "_creator_pid", client._creator_pid + 1)
        with pytest.raises(NativeProtocolError, match="fork boundary"):
            client.ping()
        assert client.connected is False
    finally:
        agent.close()
