---
name: macos-harness
description: "Drive native/Electron macOS apps, dialogs, menus, file prompts, cross-app steps. Web: browser skill."
---

# macOS Harness

In OMP use the `mac_*` tools; otherwise Python. Never put secrets in tool arguments: `credentials`.

One synchronous call per decision. Preloaded: `mac`, `Path`, `subprocess`, `present`, `gone`. `-c CODE` one line; `run flow.py` a file; `--agent-id NAME` for pointer and state diffs. After compaction: `macos-harness skill signatures`.

Start a spare once per bash shell (zsh: `spare`):

```bash
coproc SPARE { macos-harness spare; }; export MACOS_HARNESS_SPARE="${SPARE[0]},${SPARE[1]}"
```

```bash
macos-harness <<'PY'
print(mac.see("Finder"))
PY
```

In omp, `tool.read` the PNG. Bundle reversible steps and verify once at the end; stop only for unclear identity, an irreversible action, or unexpected state.

## Verbs

- `mac.state(app)`: window as text, then only changes; `state[id]` is a match.
- `mac.see(app)`: capture a window; clicks map through the latest or `screenshot=<png>`.
- `mac.key("cmd+k", app=)`, `mac.type(text, app=)`, `mac.click(x, y, app=)` or `element=match`.
- `mac.ax.query/wait/press(text, role=, app=)`: keep matches, not indexes.
- `mac.script(source)`: AppleScript with the harness's Accessibility.
- `mac.windows(all_layers=True)` lists menus and panels for `window_id=`; `see(display=0)` or `region=` replaces `screencapture`.
- `mac.launch(app)` opens unfocused. Never `sleep`: `mac.wait_change(app)` or `see(app, after_change=True)`.

## Receipts

Changes go through `mac.do.press/set/fill/type/menu/...`; `mac.menu_search(text, app=)` finds any menu item. Each returns a `Receipt` or raises `OperationError` (`exc.receipt`). `postcondition=` proves the effect; `once=` guards an action you cannot repeat.

```python
mac.do.menu("Product > Archive", app="Xcode")
mac.ax.wait("Distribute App", role="button", app="Xcode", timeout=600)
```

## Focus

- Only `mac.activate(app)` brings an app forward. A target that comes forward itself raises `FocusChangedError`; never move focus back.
- Never launch a closed app or open a URL scheme when focus is forbidden.
- After one verified failure, switch mode or stop; never repeat keys or clicks.
- Never close or quit what you did not open.

Touch ID, passkey, CAPTCHA, code or sign-in approval: print `mac.handoff(reason="authentication_required", app=...)` and end the turn. `macos-harness doctor` only after a permission or connection error.

## References

`macos-harness skill <name>`:

- `signatures`: every call and its arguments
- `receipts`: outcomes, errors, once tokens, logs
- `ax-search`: roles, scopes, matching, waits
- `invariants`: cleanup, focus, input, windows
- `routes`: saved navigation
- `data-first`: read app data; prove saves
- `diagnostics`: inspect a failure
- `handoff`: the call and what follows
- `credentials`: logins, permissions
- `native-backend`: optional backend
- `vm`: apps off the user's screen
- `spare`: zsh, fallbacks
