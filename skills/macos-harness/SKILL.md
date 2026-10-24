---
name: macos-harness
description: "Read when a task has to drive a native or Electron macOS app: a window, dialog, menu, file prompt, or a step that crosses apps. Web pages go to the safari skill. Writing Mac-control code is not driving an app: read only the reference you need."
---

# macOS Harness

One CLI call per decision point, not per primitive. `mac`, `Path`, and
`subprocess` are preloaded; `--json-errors` prints failures as JSON;
`macos-harness run flow.py --arg pid=42` reruns a saved program with `args`.

```bash
macos-harness <<'PY'
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
print(mac.see("Spotify"))
PY
```

In omp, `tool.read` the PNG in the same cell.

Bundle reversible steps; verify once at the end. Stop only for unclear
identity, an irreversible action, or unexpected state.

## Six verbs

- `mac.see(app)`: capture a window; clicks map through the latest or `screenshot=<png>`.
- `mac.key("cmd+k", app=...)`: a key or combo.
- `mac.type(text, app=...)`: typed text.
- `mac.click(x, y, app=...)`: a raw click at screenshot coordinates; `element=match` clicks its center.
- `mac.ax.query/wait/press(text, role=..., app=...)`: find, await, or press a control (`role` is any AX role); a match outlives its run, an index does not.
- `mac.script(source)`: AppleScript for an exact, focus-safe command.

`mac.windows(all_layers=True)` lists menus and panels for `window_id=`;
`see(display=0)` or `see(region=(x, y, w, h))` replaces `screencapture`.
`mac.place(app, x=, y=, width=)` moves a window; `display=0` brings it to
the main one.

`mac.launch(app)` opens an app in the background and waits for a window.
Never `sleep`: `mac.wait_change(app)` returns once the window changed and
settled; `see(app, after_change=True)` waits first.

## Receipts

For a change, use
`mac.do.press/set/fill/toggle/key/click/type/run/menu/status_item`. Each
returns a `Receipt` or raises `OperationError` with `exc.receipt`.
`postcondition=` proves the effect; `once=` guards an action you cannot
repeat. Await the next control in the same program:

```python
from macos_harness import OperationError
try:
    mac.do.menu("Product > Archive", app="Xcode")
except OperationError as exc:
    print(exc.receipt)
mac.ax.wait("Distribute App", role="button", app="Xcode", timeout=600)
```

## Focus invariants

- Input goes to a running app's process; only `mac.activate(app)` brings it forward.
- A target that comes forward itself raises `FocusChangedError`; never move focus back.
- Never launch a closed app or open a URL scheme when focus is forbidden.
- After one verified failure, switch mode or stop; never repeat keys or clicks.
- Never close or quit windows, documents, apps, or handoffs you did not open: `invariants`.

## Handoff and credentials

At Touch ID, a passkey, a CAPTCHA, a code, or a sign-in approval, print
`mac.handoff(reason="authentication_required", app=...)` and end
the turn. Never type secrets into native fields or drive web pages through
Accessibility: `credentials`.

Run `macos-harness doctor` only after a permission or connection error.

## References

`macos-harness skill <name>` prints one:

- `signatures`: every signature
- `receipts`: outcomes, errors, once tokens, logs
- `ax-search`: roles, scopes, labels, waits
- `invariants`: cleanup, focus, input, windows
- `routes`: saved navigation
- `diagnostics`: inspect a failure first
- `handoff`: the call and what follows
- `credentials`: logins, permissions
- `native-backend`: the optional backend
