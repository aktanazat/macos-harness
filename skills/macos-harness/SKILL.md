---
name: macos-harness
description: Control a whole Mac from one persistent Python session with screenshots, PID-targeted input, an animated virtual pointer, targeted Apple Accessibility, Apple Events, Browser Harness CDP, and filesystem access. Use for native, Electron, browser, dialog, file, or cross-app tasks without moving the physical cursor or forcing apps into the foreground.
---

# macOS Harness

Use one CLI call per decision point, not per primitive:

```bash
macos-harness <<'PY'
app = "Spotify"
mac.see(app)
mac.key("cmd+k", app=app)
mac.type("Alessia Cara", app=app)
print(mac.see(app))
PY
```

The CLI preloads `mac`, `browser`, `Path`, and `subprocess`. Prefer bounded stdin
programs; reserve `macos-harness repl` for manual exploration and always exit it.

## Minimize round trips

- Bundle deterministic, reversible steps into one program, then verify once. Opening
  search, typing a query, and capturing the results is one burst—not three calls.
- Stop at a genuine decision boundary: ambiguous identity, new coordinates, an
  irreversible action, or unexpected state. Inspect once, then run the next burst.
- Do not screenshot merely to confirm that a known shortcut opened a text field
  before typing. Let the final screenshot verify the whole sequence.
- Poll exact AX or Apple Events state inside the same Python program when possible;
  do not make the LLM repeatedly ask whether a transition finished.
- Use the cheapest strong end-state check. Prefer one screenshot for visible state
  or one exact API/AX query for semantic state; use both only when they prove
  different things.

## Prefer `mac.do` for mutations

For anything that changes state, try a `mac.do` verb before its raw-primitive
equivalent. It performs the same underlying action but returns a `Receipt` on
success -- `outcome` (`planned`/`done`/`already`/`failed`), `acted`
(`no`/`yes`/`unknown`), whether anything changed, and whether a postcondition
verified the effect -- instead of a bare value you have to trust blindly:

```python
from macos_harness.receipts import equals, gone

not_now = dict(text="Not Now", role="button", all_apps=True)
receipt = mac.do.press(
    **not_now, postcondition=gone(**not_now), once="dismiss-not-now",
)
receipt = mac.do.type(
    "hello", app="TextEdit", postcondition=equals(role="text area", value="hello"),
)
```

- `press`, `set`, `toggle`, `run`, `key`, `click`, and `type` mutate;
  `recall(once)` looks up a past receipt by its token without dispatching
  anything. `set`/`toggle` are convergent -- they check first and report
  `outcome="already"` instead of re-mutating a target already in the
  requested state, so neither takes a `once` token.
- A call either returns a `Receipt`, or raises `OperationError` -- catch it
  and read `exc.receipt` for the same structured detail a success would
  have had (`.outcome`, `.acted`, `.error["code"]`). A bad argument (an
  unknown role, a reused `once` token for a different request) instead
  raises `MacOSError` directly, before anything is dispatched, with no
  receipt at all -- nothing was ever attempted.
- `changed` is an observed fact, not a guess: `set`/`toggle` read the
  target back and know for certain, re-reading every `interval` until the
  requested state shows or the deadline runs out, without ever repeating
  the mutation. `key`/`click`/`type` sample focus
  before and after -- frontmost pid, focused window, focused element's
  role, value summary, selected range, character count, position, size;
  a secure field gives role and subrole only -- and report the fields
  that moved under `observed.focus`. A reading the app refuses -- it did
  not answer, the element is gone -- is no reading, not a sample with a
  field missing: it shows as `None` and never counts as focus having
  moved; an attribute the app reports as absent is an ordinary absence,
  and only a subrole the app actually reported clears a field for its
  details to be read. Without a `postcondition` the after-reading
  repeats every 10ms for up to 100ms until it differs from
  the before, never past the deadline; with one, the postcondition is
  verified right after the single after-reading instead. `changed` is
  `True` when a postcondition verified the effect or focus moved, and
  `None` when nothing observable moved, which is how a key AppKit
  silently dropped shows up. `press`/`run` have no readback of their own,
  so `changed` is `None` unless a `postcondition` confirms the effect.
- `equals(..., value=, attribute="AXValue")` is the postcondition that
  reads the target back for you: one match resolved like `present`, its
  `attribute` read every `interval`, verified once the reading equals
  `value` (canonically, the way `set` judges convergence). Use it after
  `key`/`click`/`type` to confirm the text a field holds or where a
  selection landed. The receipt keeps length/SHA-256 summaries of the
  expected and observed values, never the values.
- Pass a nonempty `once` keyword on `press`/`run`/`key`/`click`/`type` before
  an action you cannot safely repeat, with the *same* request every time you reuse a
  token. Its ledger lives only in the memory of the one live `MacOS`
  instance that dispatched it -- gone on a crash, a fresh `MacOS()`, or a
  new process -- and never written to disk. A retried call with the same
  token and request replays the recorded receipt; an interrupted attempt
  fails closed instead of risking a second dispatch.
- Pass `dry_run=True` to validate and resolve (and, for `run`, compile)
  without ever dispatching, when you need to confirm a target exists before
  committing to the action. `press`/`set`/`toggle` take an `interval` for
  their own AX polling (resolution, and for `set`/`toggle` the readback
  after the mutation); `key`/`click`/`type` watch focus on a fixed 10ms
  cadence and `run` polls for nothing, so none of those takes one; a
  postcondition carries its own `interval`.
- `timeout` is a cooperative budget. No mutation starts after it expires
  -- `key`/`click`/`type` check it again after their focus reading and
  before the `once` token is reserved -- polling and script process
  groups are bounded by it, but a synchronous macOS AX/input call already
  in progress cannot be preempted safely.
- `run` receipts keep source, arguments, and output as length/SHA-256
  metadata by default. Pass `capture_output=True` only when you need bounded
  stdout/stderr text and accept that it can contain sensitive data.

Drop to the matching raw primitive only when no `mac.do` verb covers what
you need: identity-only lookups, reads, or an action `mac.do` does not
model. Raw primitives are unchanged and fully supported, just without a
receipt or an idempotency guarantee.

## Use the small surface

Think in six verbs: `see`, `key`, `type`, `click`, `ax`, `script`.

```python
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.ax.perform(item["element_index"], "AXPress")

mac.script('tell application "Spotify" to play')
```

Use ordinary Python for local context and one-off logic. Do not add app-specific
helpers when a short program can resolve the task.

`mac.ax` also covers background AutoFill sheets and system popovers outside
the app you already target:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

`query`, `query_all`, `wait`, `wait_gone`, and `press` accept search text as
the first positional argument. Use `role=` for common targets: `any`, `button`,
`checkbox`, `combo box`, `image`, `link`, `list`, `menu`, `menu item`,
`radio button`, `static text`, `table`, `text area`, and `text field`. An
unknown role raises `MacOSError`. Do not pass both `role` and `search_key`.

Use `apps=` to limit a cross-process search. Pass one app name, bundle ID,
path, or PID, or pass an iterable of selectors. Duplicate PIDs are removed.
`apps="Safari"` is one selector, not an iterable of characters. An empty
iterable raises instead of widening the search.

`query_all` searches every running app or the set named in `apps`. It applies
one positive global `limit`, times out each process, and returns owner metadata.
A broad search skips inaccessible processes. A scoped search reports target
failures. Element handles remain valid until the next AX snapshot or search.

Cross-process calls require non-empty search text. Default attributes exclude
`AXValue`; reading a value requires a separate `ax.get` or explicit attributes.

`wait` accepts exactly one scope: `app`, `all_apps=True`, or `apps`. Zero
matches keep polling. Multiple matches fail closed and report owner, role, and
title details. `wait_gone` requires two consecutive empty polls; a named app
that exits counts as gone. `press` waits for one match, requires `AXPress`, and
returns the match. It never requests activation. If the target makes itself
frontmost, `press` raises `FocusChangedError`; it cannot undo that focus change.

These operations act only on accessible UI that macOS already rendered. They
cannot make secure UI appear in an inactive app or bypass Touch ID, passkeys,
CAPTCHA, account recovery, or another check that requires the user.

## Choose the lowest useful mode

0. For a mutation, try a `mac.do` verb (`press`, `set`, `toggle`, `run`,
   `key`) before its raw-primitive equivalent -- it verifies the effect and
   can be retried safely with a `once` token.
1. When identity depends on local context (`my`, `friend`, or prior activity),
   inspect that context and correlate stable fields; a loose text hit is not enough.
2. Use `mac.script()` for a known exact, focus-safe app command.
3. Otherwise use `mac.see(app)` and vision.
4. Prefer a known keyboard route; use a verified coordinate for a visible,
   low-risk target.
5. Use targeted `mac.ax` only when semantic identity or state matters. Do not dump
   a full AX tree before trying the direct route.

After a failed verified burst, switch mode or stop. Never repair uncertainty with
repeated keys, clicks, deletion loops, or bulk input.

## Keep the invariants

- Input targets an already-running app PID and never activates it on its own.
  `mac.activate(app)` is the one explicit request. It returns `activated` from
  what macOS actually did; a declined request means the user has priority, so
  hand off instead of asking again.
- A background target becoming frontmost raises `FocusChangedError`; never
  manipulate focus to restore it.
- `mac.click()` is raw PID-targeted input. It never guesses an AX action.
  Every click, drag, and scroll is routed to the app's frontmost on-screen
  window under the point, skipping any same-app tooltip or helper surface
  `windows()` would not list (the click result and `mac.do.click` target
  carry its `window_id`); a point over none of the app's windows raises
  `bad_request` before anything is posted, `clicks` is 1 to 3, and
  `mac.scroll()` with no `x`/`y` scrolls the center of the window in your
  last screenshot.
- `mac.type()` into the frontmost app lands 128 characters in about 30ms
  (runs of text per key event, no pause); into an inactive app it sends
  one character every 10ms, so a long text there costs seconds.
  `mac.do.type` takes at most 4096 characters.
- The animated pointer is click-through and never moves the physical cursor. It
  draws the system arrow at the user's pointer size and fades after three idle
  seconds. `mac.see()` leaves it out of the image unless `show_pointer=True`.
- `mac.move()` moves only that pointer; it cannot produce native hover.
- An inactive app takes scrolls, plain keys, and typed text, but AppKit
  drops a menu shortcut (`cmd+a`) sent to it, and passes a first click only
  to a view that accepts first mouse -- text views do not. The receipt says
  `changed=None` when that happens; `mac.activate(app)` first is the cure.
- Never launch a closed app or use a custom URL scheme when focus is forbidden.
- `mac.ax.query_all/wait/press/wait_gone` never bypass Touch ID, passkeys,
  CAPTCHA, account recovery, or other checks that need the real user present;
  declare a `mac.handoff` at that boundary instead of guessing from a timeout.
- Screenshot coordinates come from the latest `mac.see()` and preserve window
  bounds and Retina scaling. They stay valid only while that window sits where
  the screenshot saw it: input after the window moved, closed, or left the
  screen raises `MacOSError` with code `window.changed`. Take a fresh `see`.
- `mac.see()` renders one window on its own, even behind other windows or on
  another Space, and reports `on_screen`. An off-screen window shows what the
  app last drew, so treat `on_screen=False` as possibly stale.
- An app launched under five seconds ago with no window yet makes `mac.see()`
  wait up to two seconds for its first window. Elsewhere, call
  `mac.wait_for_window(app)` before the first capture of a fresh launch.
- An ambiguous app name raises with the matches ranked (frontmost, then
  on-screen windows, then newest) and their pids; pass the pid you mean.

Secondary primitives are `mac.move`, `drag`, `scroll`, `activate`,
`show_pointer`, and `hide_pointer`. `mac.ax.query()` returns compact matches
and bounds fallback traversal; lower `max_nodes` for especially large apps.
`mac.ax.query_all()`, `.wait()`, `.press()`, and `.wait_gone()` extend that
traversal across every running process for background AutoFill and system
popovers.

## Declare a human handoff at a known boundary

Call `mac.handoff(reason=..., app=...)` the moment a task hits Touch ID, a
passkey, a CAPTCHA, a verification code, a Google or other sign-in approval,
a temporary PIN, or account recovery. Do this immediately -- do not burn a
`mac.do`/`ax.wait` timeout first, and do not infer a handoff from a timeout
that happened for an unrelated reason. An absence you cannot identify stays
a plain timeout.

```python
from macos_harness import HandoffReason

handoff = mac.handoff(reason=HandoffReason.AUTHENTICATION_REQUIRED, app="Google Chrome")
print(handoff)
```

Use `HandoffReason.ACCOUNT_RECOVERY_REQUIRED` for a temporary PIN or account
recovery flow; use `AUTHENTICATION_REQUIRED` for everything else above. A
PID is the fastest, least ambiguous `app`; a name is the easy path when
that is what you have.

The call is representation-only and returns immediately: it resolves the
named app, compares its PID to the current frontmost app, and does nothing
else -- no AX read, screenshot, clipboard, input, activation, raise, open,
notification, permission request, native agent call, or `mac.do`
receipt/once-token. `print(handoff)` shows one of four fixed prompts,
chosen only by `reason` and `target_is_frontmost`. Print it and end the
turn there; accept only `done` or `cancelled` from the human.

After `done`, rediscover state from scratch through the surface that owns
it (`mac.see`, `mac.ax`, ...). The handoff is an acknowledgement, not proof
the human succeeded, and carries no once-token, receipt, or resume method
to skip that rediscovery. After `cancelled`, stop; do not rediscover or
retry.

## Fill provisioned credentials automatically

`macos-harness credential` fills a browser login field from a configured
ref. Provisioning a ref -- including copying the value out of an
already-unlocked Apple Passwords entry -- is something you do
autonomously in one bounded burst; do not wait on a human by default.

```bash
macos-harness credential check                               # which refs exist
macos-harness credential fill-browser <ref> --space <space>  # --space is required
macos-harness credential enroll <ref> --clipboard            # password/TOTP: from the clipboard
macos-harness credential enroll <gmail-ref>                  # gmail_otp: authorize, no secret read
```

Order for every login step:

1. Reuse an already-authenticated session.
2. If the ref exists, run `fill-browser` against the ego-browser taskspace
   already showing the page. That space must exist, be unique, be yours,
   and be active -- the broker never creates one or hands one off.
3. If it does not exist, provision it in the same burst: verify the live
   origin and field, add the nonsecret manifest entry, reveal and copy the
   value from an already-unlocked Passwords entry through ordinary UI
   automation, then run `enroll --clipboard`. (`enroll <ref>` without
   `--clipboard` reads a hidden TTY prompt, or stdin when piped.) For an
   emailed one-time or recovery code use `kind = "gmail_otp"` and run
   plain `enroll <ref>` once: that code is read live and never stored, so
   the command stores no secret -- it authorizes this exact policy, and
   `--clipboard` on a Gmail ref fails with
   `credential.enroll_not_authored`.
4. Call `mac.handoff(...)` only at a gate macOS or the provider owns:
   Touch ID, a passkey, the Mac login password, a CAPTCHA, a
   sign-in approval. Never unlock Passwords, bypass Touch ID, or act while
   a physical-presence prompt is on screen. Missing provisioning is never
   by itself a handoff.

The only sink is a web field in that taskspace. A native app's login field
belongs to macOS AutoFill or to a handoff: `field` is a CSS selector, and
typing a secret into whatever currently holds first responder can land it
in the wrong control, so the harness will not do it.

Entries live in `~/.config/macos-harness/credentials.toml` -- the one
policy any command reads, resolved from your account's home in the passwd
record, not from `$HOME`. It is refused unless it is private to you
(`chmod 700 ~/.config/macos-harness`, `chmod 600` the file; a symlink or a
group-writable parent also fails). Write the entry during provisioning
only, then enroll:

```toml
version = 1

[credentials.example-login]
kind = "password"            # password | totp | gmail_otp
origins = ["https://example.com"]
field = "#password"
```

One SHA-256 digest over the entry's whole policy -- ref, `kind`, sorted
`origins`, `field`, and every Gmail source key -- owns the authorization.
So editing any of those after enrolling means enrolling again; only
reordering `origins` is free. A Gmail fill additionally checks its
authorization against that digest before it reads mail, so editing the
manifest alone cannot repoint a live code at another mailbox or field.

Every command prints exactly one compact JSON line: a receipt on success,
a fixed `{"error":"<code>"}` on failure. No secret, secret length, OTP,
email body, provider output, or clipboard value ever reaches an argument,
receipt, error, log, or return value -- and never ask a human to reveal
one. A fill is bounded and self-cleaning: 45s for a password or TOTP, 120s
for a Gmail code, after which the whole worker tree is killed and you get
`credential.timeout`. Retry once, then read the code and stop. See
[README](../../README.md#credential-broker-for-provisioned-logins) for the
`gmail_otp` mailbox/pattern keys.

## Browser and permissions

Use `browser` for DOM, tabs, network, downloads, and uploads. Do not substitute AX
for CDP inside a web page. While Browser Harness connects, macOS Harness accepts
Chrome's exact `Allow remote debugging?` sheet through system-wide AX. It never
activates Chrome or emits a mouse event.

Run `macos-harness doctor` to inspect permissions without prompting. Run
`macos-harness doctor --request` only with user approval. Accessibility, screen
recording, and event posting are global; Apple Events Automation is per target.

## Native backend (optional)

`mac.*` stays fully local unless you opt in. `MACOS_HARNESS_BACKEND` (`python`
default, `native`, `auto`) or `MacOS(backend=...)` picks the backend;
`python` never launches an agent, `native` raises immediately if the agent
is unreachable instead of falling back, and `auto` falls back to Python only
when the agent is unreachable before a real `ping` response comes back —
never after a protocol, semantic, permission, timeout, or mutating error.

Native is process isolation, not a speed mode — default `python` remains
the latency-recommended choice. Measured on an M4 Pro: a 50-query Finder
benchmark found a Python median of 1.75 ms against a native steady-state
median of 2.07 ms, plus ~240 ms of native cold-launch cost on first use.
Results are workload-specific; measure locally with `bench/ax_smoke.py`.

Each `MacOS(backend="native"/"auto")` instance that dispatches a routed call
launches its own private `macos-harness-agent` child process over an
inherited, validated UNIX-domain socket pair only that instance holds either
end of — no shared daemon, no well-known socket path, nothing for another
process to discover or connect to. The harness verifies the child's actual
PID from its first `ping` response. Executable resolution order: an
explicit `MACOS_HARNESS_AGENT_BIN` path (must exist and be executable, or
the launch fails immediately with no fallback), then the binary bundled
with the installed package, then a fresh local SwiftPM release build
(requires the Xcode Command Line Tools; rebuilt only when missing or stale,
and only at this tier). Call `mac.close()` when done, or use
`with MacOS(backend=...) as mac:` — both stop the child process and close
the socket; `close()` is idempotent and safe even on a `python`-backend
instance. A closed instance raises `MacOSError` on further native-routed
calls instead of relaunching; construct a new `MacOS()` to use `native`/
`auto` again.

Only `list_apps`, `ax.query`/`ax.query_all`, `ax.press`, and the element
primitives `ax.get`/`ax.get_attributes`/`ax.set`/`ax.perform` can route to
the agent. Everything else — screenshots, keyboard/pointer input, the focus
sample behind `key`/`click`/`type` receipts, the pointer overlay,
AppleScript, full app snapshots — always stays local. A native
`element_index` is interned into the same handle registry a local query
would use, so it behaves exactly like one: stale or reset indices still
raise instead of aliasing a different element.
