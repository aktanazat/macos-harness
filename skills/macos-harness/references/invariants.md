# Invariants and choosing a mode

## Cleanup and ownership

Finishing includes cleanup of resources the caller actually owns. The CLI
closes its own accessibility agent and disposable pointer helper before it
returns, including on errors and interruptions. In an embedded process, use
`with MacOS() as mac:` or `try/finally: mac.close()`. The pointer is created
only when needed and can be shown again explicitly after cleanup.

App windows do not become caller-owned when the harness reads or changes
them. Do not automatically close documents, existing windows, or human
handoffs. A receipt does not prove window ownership or saved-state safety.
Reopening a saved document does not restore unsaved edits. Retain only safe
task context and observe the app again when resuming; never save credentials
or unsent form values.

Quit only an app the task launched. `mac.quit(app)` waits until it exits; an
app still open at the timeout is usually showing a save prompt, which
`force=True` would discard.

## Choose the lowest useful mode

0. For a change, try a `mac.do` verb (`press`, `set`, `toggle`, `fill`, `run`,
   `key`) before its raw primitive: it verifies the effect and a `once` token
   makes a retry safe.
1. When identity depends on local context (`my`, `friend`, or prior activity),
   inspect that context and correlate stable fields; a loose text hit is not
   enough.
2. Use `mac.script()` for a known exact, focus-safe app command.
3. Otherwise use `mac.see(app)` and vision.
4. Prefer a known keyboard route; use a verified coordinate for a visible,
   low-risk target.
5. Use targeted `mac.ax` only when semantic identity or state matters. Do not
   dump a full AX tree before trying the direct route.

After a failed verified burst, switch mode or stop. Never repair uncertainty
with repeated keys, clicks, deletion loops, or bulk input.

## Minimize round trips

- Bundle deterministic, reversible steps into one program, then verify once.
  Opening search, typing a query, and capturing the results is one burst.
- Do not screenshot merely to confirm that a known shortcut opened a field
  before typing; the final screenshot verifies the whole sequence.
- Poll inside the same program instead of sleeping or asking again in another
  call: `mac.ax.wait` and `mac.do.expect` for AX or Apple Events state,
  `mac.wait_change(app)` for how a window looks. It measures from just before
  the program's latest input to that window, or from its last `see` or wait
  when no input came after that, so a change the action already made counts
  and an earlier action's does not (with none of these, against the window
  when called), and returns once the new look has held still `settle` seconds.
  About one changed word of text counts; a blinking caret does not.
  `region=(x, y, w, h)` in that `see`'s pixels watches a smaller change, and
  `by="ax"` the app's text. Nothing changing by `timeout` raises `timeout` with
  `details["changed"]` False; `mac.see(app, after_change=True)` reports
  `changed` instead and captures. When quick visible feedback, such as a
  pressed button or a spinner, comes before a slower answer, pass a `settle=`
  longer than the gap, or wait on the answer's text with `by="ax"` or
  `mac.ax.wait`.
- Use the cheapest strong end-state check: one screenshot for visible state or
  one exact AX query for semantic state; both only when they prove different
  things.

## Focus and input

- Input targets an already-running app PID and never activates it on its own.
  `mac.activate(app)` is the one explicit request. It returns `activated` from
  what macOS actually did; a declined request means the user has priority, so
  hand off instead of asking again.
- A background target becoming frontmost raises `FocusChangedError`; never
  manipulate focus to restore it.
- `mac.click()` is raw PID-targeted input and never guesses an AX action. Every
  click, drag, and scroll goes to the app's frontmost on-screen window under the
  point, skipping same-app tooltips and helper surfaces `windows()` would not
  list (the click result and `mac.do.click` target carry its `window_id`). A
  point over none of the app's windows raises `bad_request` before anything is
  posted; `clicks` is 1 to 3.
- `mac.type()` into the frontmost app lands 128 characters in about 30 ms; into
  an inactive app it sends one character every 10 ms, so long text there costs
  seconds. `mac.do.type` takes at most 4096 characters.
- An inactive app takes scrolls, plain keys, and typed text, but AppKit drops a
  menu shortcut (`cmd+a`) sent to it and passes a first click only to a view
  that accepts first mouse; text views do not. The receipt then says
  `changed=None`; `mac.activate(app)` first is the cure.
- Never launch a closed app or use a custom URL scheme when focus is forbidden.
- `mac.ax.query_all/wait/press/wait_gone` never get past Touch ID, passkeys,
  CAPTCHA, account recovery, or other checks that need the user present; call
  `mac.handoff` at that boundary instead of guessing from a timeout.

## Pointer

- The animated pointer is click-through and never moves the physical cursor. It
  draws the system arrow at the user's pointer size and fades after three idle
  seconds. `mac.see()` leaves it out unless `show_pointer=True`.
- `mac.move()` moves only that pointer; it cannot produce native hover.

## Screenshots and windows

- Screenshot coordinates come from the latest `mac.see()` and preserve window
  bounds and Retina scaling. They stay valid only while the window sits where
  the screenshot saw it: input after it moved, closed, or left the screen
  raises `MacOSError` with code `window.changed`. Take a fresh `see`.
- `mac.see()` renders one window on its own, even behind other windows or on
  another Space, and reports `on_screen`. An off-screen window shows what the
  app last drew, so treat `on_screen=False` as possibly stale.
- `mac.launch(app)` starts an app (name, bundle ID, or `.app` path) without
  bringing it forward, reuses a copy already running, and returns once its
  first window is up; `window=False` is for an app that never shows one.
  `mac.relaunch(app)` restarts the same bundle, so a fresh build relaunches
  from its own path. An app launched some other way under five seconds ago
  makes `mac.see()` wait up to two seconds for its first window.
- Some apps keep running with no window until their Dock icon is clicked.
  `mac.see()` then says the app is running with no windows and names
  `mac.script('tell application id "<bundle id>" to reopen')`, which opens the
  window the way a Dock click does; TextEdit answered it without coming to the
  front. `mac.activate()` alone does not open one.
- An ambiguous app name raises with the matches ranked (frontmost, then
  on-screen windows, then newest) and their pids; pass the pid you mean.
  Of processes with exactly that name, the only one with an on-screen
  window is picked for you unless a windowless one is frontmost. An app
  macOS lists without a process yet says it is still launching;
  `mac.launch(app)` waits until it has one.
