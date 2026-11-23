# Invariants and choosing a mode

## Cleanup and ownership

- Embedded use: `with MacOS() as mac:` or `try/finally: mac.close()`; the CLI cleans up itself.
- Reading or changing a window does not make it yours: never close documents, existing windows or handoffs. `mac.do.window("close", ...)` only for a window the task opened.
- Quit only an app the task launched. `mac.quit(app)` waits; an app still open is usually showing a save prompt, which `force=True` discards.

## Choose the lowest useful mode

0. App data first when it holds the answer (`data-first`).
1. `mac.do` verbs before raw primitives.
2. Identity from local context ("my", prior activity): correlate stable fields, not a loose text hit.
3. `mac.script()` for an exact, focus-safe app command (`mac.sdef(app)`).
4. `mac.see(app)` and vision; a known keyboard route, or a verified coordinate for a low-risk target.
5. Targeted `mac.ax` when semantic identity matters; never dump a full tree first.

After a failed verified burst, switch mode or stop; never repeat keys, clicks or deletes.

## Round trips

- One program per deterministic burst (open search, type, capture), verified once at the end.
- Wait inside the program: `mac.ax.wait`, `mac.do.expect`, or `mac.wait_change(app)` (measures from the program's latest input; `region=` for small changes, `by="ax"` for text; raises `timeout` when nothing changes). For fast feedback before a slow answer, raise `settle=` or wait on the answer's text.
- A `mac.do` verb with a postcondition takes no baseline picture; follow it with a plain `mac.see` to look.

## Focus and input

- Input goes to a running app's pid and never activates it. `mac.activate(app)` is the one request; declined both ways means hand off.
- A background target becoming frontmost raises `FocusChangedError`; never restore focus.
- Clicks go to the app's frontmost window under the point; a point over none raises `bad_request`.
- An inactive app drops menu shortcuts (`cmd+a`) and the first click in text views; the result's `target_is_frontmost` and `note` say so. Fix: `mac.activate(app)` first or `mac.do.press`.
- `mac.type` into an inactive app sends one character per 10 ms; `mac.do.type` caps at 4096 chars; use `mac.do.paste` (front app only) for long text.
- Input to one app is serialized across harness processes (`app_input_busy` after the wait). Pass `window_id` on `key`/`type` when other runs may share the app.

## Screenshots and windows

- Screenshot coordinates hold only while the window stays put; otherwise `window.changed`: take a fresh `see`.
- `mac.see()` renders a window even behind others; `on_screen=False` may be stale.
- `mac.launch(app)` starts without focus and waits for a window (`window=False` for windowless apps); `mac.relaunch(app)` restarts the same bundle.
- A running app with no window: `mac.script('tell application id "<bundle id>" to reopen')`.
- An ambiguous app name raises with ranked pids: pass the pid.
- The agent pointer never moves the real cursor; `mac.move()` cannot hover.
