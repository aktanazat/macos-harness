---
name: macos-harness
description: "Read when a task has to drive a native or Electron macOS app: a window, dialog, menu, file prompt, or a step that crosses apps. Web pages go to the safari skill. Writing Mac-control code is not driving an app: read only the reference you need."
---

# macOS Harness

In OMP, pick the matching `mac_*` tool unasked; Python below covers the
rest. Never put secrets in tool arguments: `credentials`.

One synchronous call per decision. `mac`, `Path`, `subprocess`, `present`
and `gone` are preloaded. `-c CODE` runs one line; `run flow.py` a file.
`--agent-id NAME` names the pointer and turns on state diffs. After compaction,
run `macos-harness skill signatures`.

```bash
macos-harness <<'PY'
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
print(mac.see("Spotify"))
PY
```

In omp, `tool.read` the PNG.

Bundle reversible steps; verify once at the end. Stop only for unclear
identity, an irreversible action, or unexpected state.

## Seven verbs

- `mac.state(app)`: the window as text, then only changes; `state[id]` is a match.
- `mac.see(app)`: capture a window; clicks map through the latest or `screenshot=<png>`.
- `mac.key("cmd+k", app=...)`: a key or combo.
- `mac.type(text, app=...)`: typed text.
- `mac.click(x, y, app=...)`: a click at screenshot points, or `element=match`.
- `mac.ax.query/wait/press(text, role=..., app=...)`: find, await or press a control; keep matches, not indexes.
- `mac.script(source)`: AppleScript with the harness's Accessibility.

`mac.windows(all_layers=True)` lists menus and panels for `window_id=`;
`see(display=0)` or a `region=` replaces `screencapture`.

`mac.launch(app)` opens an app unfocused, then awaits its window.
Never `sleep`: `mac.wait_change(app)` returns once the window changed and
settled; `see(app, after_change=True)` waits first.

## Receipts

For a change, use `mac.do.press/set/fill/type/menu/...`;
`mac.menu_search(text, app=)` finds any menu item. Each returns a
`Receipt` or raises `OperationError` with `exc.receipt`. `postcondition=`
proves the effect; `once=` guards an action you cannot repeat. Await the
next control in one program:

```python
mac.do.menu("Product > Archive", app="Xcode")
mac.ax.wait("Distribute App", role="button", app="Xcode", timeout=600)
```

## Focus invariants

- Input goes to a running app's process; only `mac.activate(app)` brings it forward.
- A target that comes forward itself raises `FocusChangedError`; never move focus back.
- Never launch a closed app or open a URL scheme when focus is forbidden.
- After one verified failure, switch mode or stop; never repeat keys or clicks.
- Never close or quit what you did not open: `invariants`.

## Handoff and credentials

At Touch ID, a passkey, a CAPTCHA, a code, or a sign-in approval,
print `mac.handoff(reason="authentication_required", app=...)` and end the turn.
Passwords, a VM's too, web pages: `credentials`.

`macos-harness doctor` only after a permission or connection error.

## References

`macos-harness skill <name>` prints:

- `signatures`: every signature
- `receipts`: outcomes, errors, once tokens, logs
- `ax-search`: roles, scopes, labels, waits
- `invariants`: cleanup, focus, input, windows
- `routes`: saved navigation
- `data-first`: read data first; prove saves
- `diagnostics`: inspect a failure first
- `handoff`: the call and what follows
- `credentials`: logins, permissions
- `native-backend`: an optional backend
- `vm`: apps off the user's screen
