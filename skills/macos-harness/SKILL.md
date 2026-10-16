---
name: macos-harness
description: "Read when a task has to drive a native or Electron macOS app: a window, dialog, menu, file prompt, or a step that crosses apps. Web pages go to the safari skill. Writing Mac-control code is not driving an app: read only the reference you need."
---

# macOS Harness

One CLI call per decision point, not per primitive. `mac`, `Path`, and
`subprocess` are preloaded; `--json-errors` prints failures as JSON.

```bash
macos-harness <<'PY'
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
print(mac.see("Spotify"))
PY
```

Bundle reversible steps; verify once at the end. Stop only for unclear
identity, an irreversible action, or unexpected state.

## Six verbs

- `mac.see(app)`: capture a window; click coordinates come from the latest one.
- `mac.key("cmd+k", app=...)`: a key or combo.
- `mac.type(text, app=...)`: typed text.
- `mac.click(x, y, app=...)`: a raw click at screenshot coordinates.
- `mac.ax.query/wait/press(text, role=..., app=...)`: find, await, or press a control.
- `mac.script(source)`: AppleScript for an exact, focus-safe command.

## Receipts

For a change, use `mac.do.press/set/fill/toggle/key/click/type/run`. Each
returns a `Receipt` (`outcome`, `acted`, `changed`, `verified`) or raises
`OperationError` with `exc.receipt`. `postcondition=` proves the effect;
`once=` guards an action you cannot repeat. Wait for the next control in the
same program, never with `sleep`:

```python
from macos_harness import OperationError
try:
    mac.do.press("Next", role="button", app="Xcode")
except OperationError as exc:  # -25204 may still have pressed
    print(exc.receipt.error["code"])
mac.ax.wait("Upload", role="button", app="Xcode", timeout=15)
print(mac.see("Xcode"))
```

## Focus invariants

- Input goes to a running app's process; only `mac.activate(app)` brings it forward.
- A target that comes forward itself raises `FocusChangedError`; never move focus back.
- Never launch a closed app or open a URL scheme when focus is forbidden.
- AXError -25204 (no answer in time) may still have acted: look before retrying.
- After one verified failure, switch mode or stop; never repeat keys or clicks.
- Never close windows, documents, or handoffs you did not open: `invariants`.

## Handoff and credentials

At Touch ID, a passkey, a CAPTCHA, a code, or a sign-in approval, print
`mac.handoff(reason="authentication_required", app=...)` and end
the turn. Never type secrets into native fields or drive web pages through
Accessibility: `references/credentials.md`.

Permissions are granted. Run `macos-harness doctor` only after a call fails
with a permission or connection error, never as a first step.

## References

After a compaction, read only the reference you need, never the whole skill.
A subagent writing Mac-control code reads one reference, not this card.
`macos-harness skill <name>` prints each one from `references/`:

- `signatures`: every exact signature
- `receipts`: outcomes, errors, once tokens, timeouts, history, the log
- `ax-search`: roles, scopes, exact labels, cross-app search, waits
- `invariants`: cleanup, focus, pointer, screenshot, input; choosing a mode
- `routes`: recording and replaying saved navigation
- `diagnostics`: inspecting a failure before more input
- `handoff`: the handoff call and what follows it
- `credentials`: browser and login boundaries, permissions
- `native-backend`: the optional native backend
