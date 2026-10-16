# Invariants and choosing a mode

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
- Poll AX or Apple Events state inside the same program (`mac.ax.wait`,
  `mac.do.expect`) instead of sleeping or asking again in another call.
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
- An app launched under five seconds ago with no window yet makes `mac.see()`
  wait up to two seconds for its first window. Elsewhere, call
  `mac.wait_for_window(app)` before the first capture of a fresh launch.
- An ambiguous app name raises with the matches ranked (frontmost, then
  on-screen windows, then newest) and their pids; pass the pid you mean.
