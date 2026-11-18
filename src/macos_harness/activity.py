"""What this harness tells the agent screen companion while it runs.

The companion (``native/agent-screen``) listens on a local datagram socket,
``activity.sock`` in the harness's config directory, and shows each agent's
arrow and a trail of its actions over a live view of the agents' screen. Each
call here sends one small JSON datagram there and returns at once; with no
companion listening the send fails and nothing happens, so the trail is on
exactly while the companion runs.

A datagram names the sending process, its ``--agent-id``, the verb, the
target app's pid and window id, and for a ``mac.do`` receipt the control's
role and labels. Typed text, pasted text, set values, script source and
requested values are never sent; a key is named only when it is a shortcut or
a named key, never a lone character.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from collections.abc import Mapping

from ._paths import config_dir
from .receipts import JSONValue, Receipt

_TARGET_FIELDS = ("role", "title", "description")
_LABEL_LIMIT = 120
_SHORTCUT_MODIFIERS = frozenset({"cmd", "command", "ctrl", "control", "opt", "option", "alt", "fn"})

_sock: socket.socket | None = None
_sock_pid: int | None = None


def socket_path() -> str:
    return str(config_dir() / "activity.sock")


def pointer(agent: str | None, event: str, x: float | None = None, y: float | None = None,
            duration: float = 0.0) -> None:
    """The arrow of ``agent`` moved, showed, clicked, or hid at screen ``x``, ``y``."""
    message: dict[str, JSONValue] = {"kind": "pointer", "agent": agent, "event": event}
    if x is not None and y is not None:
        message.update(x=float(x), y=float(y), duration=float(duration))
    _send(message)


def action(agent: str | None, op: str, *, app_pid: int | None, window_id: int | None = None,
           key: str | None = None) -> None:
    """A raw input verb (``click``, ``type``, ``key``, ``scroll``, ``drag``) went to ``app_pid``."""
    _send({
        "kind": "action",
        "agent": agent,
        "op": op,
        "source": "raw",
        "app_pid": app_pid,
        "window_id": window_id,
        "key": shortcut_name(key),
        "outcome": "succeeded",
    })


def receipt(agent: str | None, finished: Receipt) -> None:
    """A ``mac.do`` operation finished."""
    target = finished.target if isinstance(finished.target, Mapping) else {}
    labels = {
        field: target[field][:_LABEL_LIMIT]
        for field in _TARGET_FIELDS
        if isinstance(target.get(field), str)
    }
    app = target.get("app")
    app_pid = app.get("pid") if isinstance(app, Mapping) else None
    scope = finished.request.get("scope")
    selector = scope.get("app") if isinstance(scope, Mapping) else finished.request.get("app")
    request_key = finished.request.get("key") if finished.op == "key" else None
    _send({
        "kind": "action",
        "agent": agent,
        "op": finished.op,
        "source": "receipt",
        "app_pid": app_pid if isinstance(app_pid, int) else None,
        "app": selector[:_LABEL_LIMIT] if isinstance(selector, str) else None,
        "target": labels or None,
        "outcome": finished.outcome.value,
        "duration_s": round(finished.duration_s, 4),
        "key": shortcut_name(request_key if isinstance(request_key, str) else None),
    })


def shortcut_name(key: str | None) -> str | None:
    """``key`` when it is a shortcut (``cmd+k``) or a named key (``return``),
    else None: a lone character could spell out what was typed."""
    if not key:
        return None
    parts = [part for part in re.split(r"[+-]", key.lower()) if part]
    if not parts:
        return None
    if len(parts[-1]) > 1 or any(part in _SHORTCUT_MODIFIERS for part in parts[:-1]):
        return key[:40]
    return None


def _send(message: dict[str, JSONValue]) -> None:
    global _sock, _sock_pid
    message.update(v=1, pid=os.getpid(), t=time.time())
    # An app's label can hold a lone surrogate; the action already happened.
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    try:
        if _sock is None or _sock_pid != os.getpid():
            _sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            _sock.setblocking(False)
            _sock_pid = os.getpid()
        _sock.sendto(data, socket_path())
    except OSError:
        pass
