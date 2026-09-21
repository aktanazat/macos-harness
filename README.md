<img src="https://raw.githubusercontent.com/browser-use/macos-harness/main/static/banner-ink.svg" alt="macOS Harness" width="100%" />

# macOS Harness ⌘

The simplest, thinnest harness that gives an LLM complete freedom to complete
virtually any task on a Mac.

The agent writes what is missing, mid-task. No framework, no recipes, no rails.
One Python process connected directly to macOS, your real browser, and your files.

```text
● agent: wants to do something no helper exists for
│
● sees the app and uses raw macOS primitives
│
● writes the missing logic in ordinary Python
│
✓ task complete                                  no app-specific tool added
```

**Your agent now has a Mac.**

## Give it to your agent

Paste this into Codex or Claude Code:

```text
Install or upgrade macOS Harness from https://github.com/browser-use/macos-harness with uv using Python 3.12. Register the skill printed by `macos-harness skill`, then run `macos-harness doctor`. Explain any missing macOS permissions and ask before requesting them. Finally, verify the harness by capturing one already-running app without bringing it to the foreground.
```

That is it. The agent installs the package, teaches itself the workflow, checks
permissions, and verifies the connection. [Manual setup](install.md) is available too.

## Machine-first: `mac.do` for mutations, raw primitives underneath

Prefer `mac.do` for anything that changes state. It runs the same primitives
underneath, but returns a `Receipt` instead of a bare value, so you can tell
what actually happened instead of trusting it blindly:

```bash
macos-harness <<'PY'
from macos_harness.receipts import OperationError, gone

not_now = dict(text="Not Now", role="button", all_apps=True)
postcondition = gone(**not_now)

receipt = mac.do.press(**not_now, postcondition=postcondition, once="dismiss-not-now")
print(receipt.outcome, receipt.acted, receipt.verified)  # done yes True

# A repeat call with the same `once` token *and* the same request (reusing
# `not_now`/`postcondition` guarantees that here) replays the recorded
# receipt instead of risking a second press.
replay = mac.do.press(**not_now, postcondition=postcondition, once="dismiss-not-now")
assert replay.replayed

# A dry run validates and resolves without ever dispatching anything or
# touching the once-token ledger.
plan = mac.do.press(**not_now, dry_run=True)
assert plan.outcome == "planned" and plan.acted == "no"

# A failed operation raises OperationError, never a bare exception --
# exc.receipt is the exact same Receipt a success would have returned.
try:
    mac.do.press(text="Does Not Exist", role="button", all_apps=True, timeout=1)
except OperationError as exc:
    print(exc.receipt.outcome, exc.receipt.error["code"])  # failed timeout
PY
```

- `press`, `set`, `toggle`, `run`, `key`, `click`, and `type` mutate;
  `expect(condition)` observes a condition, and `recall(once)` looks up a
  past receipt by its token, without dispatching
  anything. `set`/`toggle` are convergent: they read the current state
  first and report `outcome="already"` instead of touching anything
  already correct.
- Every call returns an immutable, JSON-safe `Receipt` on success, or
  raises `OperationError` on failure — `exc.receipt` is the exact same
  `Receipt` a success would have had, so you never have to choose between
  catching the exception and reading the structured detail: `outcome`
  (`planned`, `done`, `already`, `failed`), `acted` (`no`, `yes`,
  `unknown`), the backend and executor that actually ran it, the
  normalized request and target, whether anything changed, whether a
  postcondition verified the effect, duration_s, and — on failure — a
  structured error. `receipt.to_json()` is ready for `json.dumps`.
- `changed` is only ever an observed fact, never a guess. `set`/`toggle`
  read the target back before and after and know for certain; a readback
  that lags behind the mutation is re-read every `interval` until it
  shows the requested state or the deadline runs out, and the mutation
  itself is never repeated. `key`/`click`/`type` sample focus before and
  after — frontmost pid, focused window, and the focused element's role,
  value summary, selected range, character count, position, and size; a
  secure field gives its role and subrole only — and report the fields
  that moved under `observed.focus`. A reading the app refuses — it did
  not answer, the element is gone — is no reading, not a sample with a
  field missing: it shows as `None` under `observed.focus` and never
  counts as focus having moved, while an attribute the app reports as
  absent is an ordinary absence. Only a subrole the app actually
  reported clears a field for its details to be read.
  Without a `postcondition` the after-reading repeats every 10ms for up
  to 100ms until it differs, never past the deadline, so an app's run
  loop gets time to process the event. With one, the single after-reading
  is taken and the postcondition is verified straight away: it is the
  effect you asked about, so it never waits behind the focus witness.
  `changed` is `True` when focus moved or a postcondition verified the
  effect, `None` when nothing observable moved, which is how a key AppKit
  silently dropped shows up. `press`/`run` have no readback of their own,
  so `changed` is `None` unless you pass a `postcondition` that confirms
  the effect actually took hold.
- A bad argument — an unknown role, a malformed postcondition, reusing a
  `once` token for a genuinely different request — raises `MacOSError`
  directly, before anything is dispatched: there is no receipt, because
  nothing was ever attempted. Once a mutating call actually begins, every
  failure comes back as a `Receipt`, raised inside `OperationError`.
- `present(...)`/`gone(...)` (the same call shape as `mac.ax.wait`/
  `mac.ax.wait_gone`) verify an operation's real effect; left unscoped, a
  postcondition inherits the operation's own `app`/`apps`/`all_apps` scope
  — except for `run`, which has no scope of its own, so its postcondition
  must set `app=`, `all_apps=True`, or `apps=` explicitly. `press`/`set`/
  `toggle` accept an `interval` for their own AX polling — resolution,
  and for `set`/`toggle` the readback after the mutation. `key`/`click`/
  `type` watch focus on a fixed 10ms cadence and `run` polls for nothing,
  so none of those takes one; a postcondition carries its own `interval`.
- `equals(..., value=, attribute="AXValue")` verifies a value, not just a
  presence: it resolves one match the way `present` does, reads
  `attribute` back every `interval`, and is satisfied once the reading
  equals `value` (compared canonically, the way `set` judges
  convergence). Pair it with `key`, `click`, or `type` to confirm a field
  holds the requested text or a selection landed where it should. A
  receipt carries the expected and observed values as length/SHA-256
  summaries, never the values themselves.
- `timeout` is one cooperative budget across resolution, dispatch, and
  verification. Presses keep that budget through agent startup, searches,
  retry delays, and the frontmost-app reading. An expired budget stops the
  next mutating call. A synchronous macOS Accessibility or input call
  already in progress cannot be preempted safely and can return later.
  Timed-out script process groups are terminated.
  A press stopped with `deadline_exhausted_before_dispatch` reports
  `acted="no"` and leaves its `once` token available for another attempt.
  Other timeouts can mean the action happened and retain their token.
  Raw `mac.ax.press(timeout=0)` makes one attempt without retry;
  `mac.do.press(timeout=0)` does not dispatch.
- `press`, `run`, `key`, `click`, and `type` accept a nonempty `once` keyword for
  at-most-once dispatch. The ledger lives only in the memory of the one
  live `MacOS` instance that dispatched the call — a crash, a fresh
  `MacOS()`, or a new process all start with an empty ledger — and it
  keeps every finished receipt only for as long as that instance does,
  never on disk and never shared with another process. A repeat call with
  the same token *and* the same request replays the recorded receipt
  instead of dispatching again; an interrupted or still in-flight attempt
  fails closed with a failed, `acted="unknown"` receipt rather than
  risking a second dispatch. `set`/`toggle` never need one -- reading
  before mutating already makes them safe to repeat.
- A `Receipt` stays safe to keep or log by default. Script source and
  arguments, `set`/`toggle` values, and script output use length and SHA-256
  metadata instead of raw values. Pass `capture_output=True` only when you
  need bounded stdout/stderr text and accept that it can contain sensitive
  data.

Use `mac.do.expect(condition)` to wait without a mutation. Pass an explicit
`app`, `apps`, or `all_apps=True` on `present`, `gone`, or `equals`. The
condition's `timeout` defaults to five seconds; its `interval` controls polling.
This uses the same verifier as postconditions, without enabling accessibility
features, sampling focus, or reserving a `once` token. Success reports
`outcome="done"`, `acted="no"`, `verified=True`, and `changed=None`.
A failed check raises `OperationError` with the original verification error.
Its receipt says which kind of failure it was under `observed`: `state` is
`Observation.UNMET` when a completed search or comparison showed the condition
false, and `Observation.UNOBSERVABLE` when the check established nothing — a
truncated tree walk, an ambiguous match, a refused reading, an exhausted
deadline, or a disappearance no second poll confirmed. `reason` names the
specific case. Only `UNMET` is evidence about the app; treat `UNOBSERVABLE`
as "look again", never as "the condition is false".

```python
from macos_harness import equals

mac.do.expect(equals(app="Demo", identifier="sync-status", value="Saved"))
```

`mac.timeline()` returns the latest 256 non-replayed receipts in completion
order, including tokenless calls and failures. It does not observe the desktop
or write files. `mac.do.history()` returns the same history as immutable
`Receipt` objects. Raw primitives are not recorded.

Receipts carry `started_at` and `finished_at` as ISO-8601 UTC timestamps with
millisecond precision. `duration_s` uses a monotonic clock. Replays keep the
original times and add no history entry. Evicting an old history entry does
not release its once token. Export the JSON view explicitly when needed:

```python
import json
from pathlib import Path

Path("action-history.json").write_text(
    json.dumps(mac.timeline(), indent=2), encoding="utf-8"
)
```

### Saved navigation

Use `mac.route` to record a navigation sequence and replay it later through
`mac.do`. Record only calls made through the yielded handle. Unrelated Python,
raw input, and other `mac.do` calls are outside the recording.

The following example assumes your app exposes these identifiers:

```python
from macos_harness import present

app = "com.example.demo"
entry = present(role="button", identifier="open-settings")
goal = present(role="checkbox", identifier="enable-sync")
with mac.route.record("open-settings", app=app, entry=entry, goal=goal) as rec:
    rec.press(role="button", identifier="open-settings", postcondition=goal)

# On a later visit to the starting screen:
plan = mac.route.run("open-settings", app=app, dry_run=True)
result = mac.route.run("open-settings", app=app, timeout=10)
print(result.status, result.at, result.to_json())
print(mac.route.list(app=app))
```

A route requires an exact bundle identifier, an entry condition, and a terminal
goal. Every target and condition needs a role plus exactly one exact title,
identifier, or description. There is no substring or alternate-field fallback.
The supported steps are `press`, `set`, `toggle`, and `key`. Presses and keys
require an explicit postcondition; set and toggle retain their value-convergence
checks. Only boolean and numeric set/equals values are recordable. Selectors and
key combinations are stored verbatim, so keep secrets out of them.

Recording checks the entry before yielding and the goal before saving. A failed
step prevents saving even if its exception is caught. The previous file stays
intact. Keep the handle on the thread that entered the block; it closes when the
block exits. A definition has at most 64 steps and a 1 MiB file limit.

Replay validates the whole definition before input. It returns `already` if the
goal holds, otherwise checks the entry and runs each step once. Only a goal
read that came back `Observation.UNMET` authorizes the steps; any
`UNOBSERVABLE` goal read stops the run rather than replay input against an app
whose state is unknown. One app process and one cooperative timeout
cover the sequence. A process exit or replacement stops further steps. The
existing operations own target resolution, dispatch, and verification.

Results have status `done`, `already`, `planned`, `diverged`, or `invalid`.
`steps_run` retains the failing step's receipt, including whether it acted.
`check` retains the latest condition check, and `at` names a failed stage.
Dry runs validate the whole file but observe only the goal, or the entry and
first target. Later targets may not exist until earlier steps run. A dry run
does not enable accessibility features or dispatch input.

Each `run` gets fresh once tokens for presses and keys. Calling it again starts
a new run; it does not recover a lost response or resume an interrupted run.
Inspect the result and current state before deciding to run again. Routes do
not retry, roll back, or activate an app.

Definitions are private JSON files under
`~/Library/Application Support/macos-harness/routes/<bundle>/<name>.json`, or
under `MACOS_HARNESS_HOME` when set. Writes replace the file atomically. Listing
reads definitions without observing the app. Results report recorded and current
on-disk bundle versions; a version difference alone does not reject a run.
Run receipts remain in session history and are not saved beside the definition.

### Diagnostics

Use `mac.status(app)` to read process identity and on-disk build metadata without
reading the UI. An app selector or an app-bound receipt is accepted. A numeric
PID can also identify a command-line process. `process.state` is `running`,
`exited`, or `unknown`, relative to the bound PID and launch time.
`build.potentially_stale` compares the executable's modification time with the
process start. It does not identify the build loaded into the running process.

App-bound receipts retain process evidence from completion. Replays keep the
original evidence.

If input returned successfully but its effect was not verified, an observed exit
fails with `app.exited`. An exit that satisfies a verified postcondition can
succeed. An existing action error and its `acted` classification stay intact.
Missing process metadata does not turn a successful action into a failure.

For a failed app-scoped operation, retain `OperationError.receipt` and inspect it:

```python
state = mac.inspect(receipt)
print(state["process"], state["blocked"], state.get("nearby"))

logs = mac.logs(receipt, timeout=3)
crashes = mac.crashes(receipt)
print(mac.explain(receipt, state, logs, crashes))
```

`inspect` composes the existing snapshot exporter with focus and process evidence.
It reads at most 300 nodes to depth 12 by default, without enabling accessibility
features. `coverage` reports node, depth, and read limits. `blocked` describes
observed sheets or modal windows; it is `None` when an incomplete tree cannot
establish their absence. A failed receipt adds up to eight current controls
matching the requested role, including disabled controls.

Values and screenshots require separate `include_values=True` and
`screenshot=True` opt-ins. Secure fields and failed identity reads exclude value,
selection, and character-count reads even when values are requested. Titles and
labels can still contain private text. Snapshots are not atomic; the app can
change between reads. `mac.diff_windows(before, after)` compares supplied
snapshots for opened, closed, and changed windows without another observation.

Logs and crash lookups accept a receipt or a `(start, end)` pair of timezone-aware
ISO-8601 strings with `app=pid`. Receipt intervals include 250 ms on each side.
Logs default to 200 rows, 1 MiB, and a five-second deadline. `subsystem`, `category`,
and `level` narrow the query. Collection rounds outward to whole seconds, then
filters events to the requested interval. Read `status`, `coverage`, and
`truncated`; successful collection does not prove that delayed log events have
arrived.

Crash lookup reads bounded modern `.ips` reports from the user and system
DiagnosticReports directories. It matches PID and capture time, plus launch time
when available. Optional termination fields and empty stacks are retained;
returned stacks contain at most ten frames. `not_found_at_lookup` is not proof
that the app did not crash. File and byte limits can leave the lookup partial.

`mac.sample(app_or_receipt, duration=1)` explicitly collects a call graph with a
10 ms sampling interval. Duration is limited to one through five seconds. A
sample alone does not establish that an app is hung. Log and sample subprocesses
have time and output bounds and are reaped before returning. Their text can
contain private data; these diagnostics do not save it automatically.

`mac.explain(receipt, *evidence)` uses only supplied evidence. It checks available
process and time correlation, keeps the original receipt, and reports observed
conditions separately from collection failures. It does not retry an action or
collect more evidence. When input may have happened, inspect the target before
sending it again.

When no `mac.do` verb fits, drop to the six raw primitives below — an escape
hatch, not a deprecated path: unchanged, fully supported, just without a
receipt or an idempotency token.

## Six primitives. The whole Mac.

```bash
macos-harness <<'PY'
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.script('tell application "Spotify" to play')

print(browser.page_info())
print(list(Path.home().iterdir()))
PY
```

Think in `see`, `key`, `type`, `click`, `ax`, and `script`. `browser`, `Path`, and
`subprocess` are ready in the same Python process.

There are no Spotify tools, Slack tools, or Final Cut tools. The model gets raw
primitives and writes the rest.

## Background AutoFill and system popovers

`mac.ax` can search and act on running processes without requesting activation.
Use one call when the owning process is unknown:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

`query`, `query_all`, `wait`, `wait_gone`, and `press` accept `text` as the
first positional argument. Use `role=` instead of a raw `search_key` for common
targets: `any`, `button`, `checkbox`, `combo box`, `image`, `link`, `list`,
`menu`, `menu item`, `radio button`, `static text`, `table`, `text area`, and
`text field`. An unknown role raises `MacOSError`. Do not pass both `role` and
`search_key`.

`title=`, `identifier=`, and `description=` compare the whole attribute,
including case. Every supplied exact selector must match; `text` remains a
substring filter. Exact filtering happens before the result limit. The ordinary
tree fallback rejects unsupported search keys instead of treating them as `any`.

Query results remain lists and expose `complete` and `visited`. A result or
traversal limit, failed attribute read, or skipped process can leave the search
incomplete. `visited` counts returned candidates for optimized searches and
visited nodes for tree walks. A complete result does not freeze the app's state.

Use `apps=` to limit a cross-process search. Pass one app selector or an
iterable of selectors. Each selector can be an app name, bundle ID, path, or
PID. Duplicate PIDs are removed.

```python
mac.ax.wait("Not Now", role="button", apps="Spotify")
mac.ax.wait_gone("Not Now", role="button", apps=["Spotify"])
```

`query_all` searches every running app, or only the apps named in `apps`. It
uses one positive global `limit`, applies a timeout to each process, and adds
owner metadata (`name`, `bundle_id`, `pid`, `path`) to every match. A broad
search skips inaccessible processes. A scoped `apps` search reports a target
failure. Element handles remain valid until the next AX snapshot or search.

Cross-process calls require non-empty search text or an exact selector. Default
result attributes exclude `AXValue`. Reading a value remains a separate, explicit
`ax.get` call or custom `attributes` choice.

`wait` polls one `app`, every app with `all_apps=True`, or the target set in
`apps`. Pass exactly one scope. Zero matches keep polling until `timeout`.
Multiple matches fail closed with owner, role, and title details. With an exact
selector, a single match is accepted only from a complete search.

`wait_gone` requires two consecutive complete, empty searches. A named app that
exits counts as gone. `press` waits for one match, requires `AXPress`, performs it, and
returns the match. The harness never activates an app on its own;
`mac.activate(app)` is the one explicit request and reports whether macOS
honored it. If the target makes itself frontmost, `press` detects that change
and raises `FocusChangedError`. It cannot undo a focus change initiated by the
target.

These calls act only on accessible UI that macOS already rendered. They cannot
make secure UI appear in an inactive app. They cannot bypass Touch ID, passkeys,
CAPTCHA, account recovery, or another check that requires the user. Declare a
handoff at that boundary instead of waiting on a timeout -- see
[Human handoff for authentication boundaries](#human-handoff-for-authentication-boundaries).

## Human handoff for authentication boundaries

`mac.handoff(reason=..., app=...)` declares that a task has reached a boundary
only a human can cross: Touch ID, a passkey, a CAPTCHA, a verification code, a
Google or other sign-in approval, a temporary PIN, or account recovery. Call
it the moment you recognize the boundary. Do not wait out a `mac.do`/`ax.wait`
timeout first, and do not infer a handoff from a timeout that happened for an
unrelated reason -- a generic timeout stays a timeout and is never promoted.

```python
from macos_harness import HandoffReason

handoff = mac.handoff(reason=HandoffReason.AUTHENTICATION_REQUIRED, app="Google Chrome")
print(handoff)
```

```python
from macos_harness import HandoffReason

handoff = mac.handoff(reason=HandoffReason.ACCOUNT_RECOVERY_REQUIRED, app=1842)
print(handoff)
```

`reason` accepts exactly two values, or their equal string form
(`"authentication_required"`, `"account_recovery_required"`):

- `HandoffReason.AUTHENTICATION_REQUIRED` -- foreground AutoFill, a Google or
  other sign-in approval, Touch ID, a passkey, a CAPTCHA, or a verification
  code.
- `HandoffReason.ACCOUNT_RECOVERY_REQUIRED` -- a temporary PIN or an account
  recovery flow.

`app` is required and cannot be empty. Pass a PID when you already have one --
it is the fastest, least ambiguous way to identify the target. Pass a name
when that is the easy path; a bundle ID or path also works. `mac.handoff`
resolves that already-running app the same way every other primitive does; it
never launches an app and never reuses a previous call's cached app.

The call is representation-only and returns immediately. It resolves the
named app, samples the current frontmost app, and compares their PIDs --
that comparison is the entire call. It never takes a screenshot, reads AX
text, touches the clipboard, sends keyboard or pointer input, activates or
raises anything, opens a URL, sends a notification, requests a permission,
talks to the native agent, or creates a `mac.do` receipt or once-token
ledger entry. There is no polling and no blocking wait for the human.

`mac.handoff` returns a frozen, JSON-safe `HumanHandoff` with exactly five
machine fields:

```python
handoff.state                # "human_action_required"
handoff.reason                # "authentication_required" | "account_recovery_required"
handoff.retry                 # "wait_for_user_then_rediscover"
handoff.target_is_frontmost   # True | False, from the PID comparison above
handoff.automation_acted      # False, always
handoff.to_json()             # the same five fields, ready for json.dumps
```

No app name, window title, AX text, prompt, timestamp, ID, or secret-derived
data enters `to_json()`. `str(handoff)` renders one of four fixed,
library-owned prompts, selected only by `reason` and `target_is_frontmost` --
never by anything the target app controls:

```text
A secure authentication step is waiting in the app currently in front.
Complete it yourself. Do not share passwords, passkeys, verification codes,
temporary PINs, or recovery details with the agent. Return here and reply
only "done" or "cancelled".
```

Print the prompt and end the turn there. Accept only `done` or `cancelled`
back from the human. After `done`, rediscover state from scratch through the
surface that owns it (`mac.see`, `mac.ax`, ...) -- the handoff is an
acknowledgement, not proof the human succeeded, and it carries no once-token,
receipt, or resume method to skip that rediscovery. After `cancelled`, stop;
do not rediscover or retry.

## Credential broker for provisioned logins

`macos-harness credential` fills a browser login field -- a password, a
TOTP code, or a Gmail-delivered one-time code -- from a configured
credential ref. Provisioning that ref, including copying the initial
secret out of an already-unlocked Apple Passwords entry, is itself
autonomous agent work in one bounded burst; it is not a step that waits on
a human. The agent never unlocks Passwords, bypasses Touch ID or a
passkey, or acts while a physical-presence prompt is on screen -- that
boundary belongs to macOS. Once a ref is configured, nobody is asked to
repeat that authorization or reveal the value again.

```bash
macos-harness credential check
macos-harness credential fill-browser acme-login --space my-task
macos-harness credential enroll acme-login              # hidden TTY prompt, or stdin when piped
macos-harness credential enroll acme-login --clipboard  # secret already on the clipboard
macos-harness credential enroll acme-email-otp          # Gmail ref: authorizes the policy, reads nothing
```

Default order for any login step:

1. Reuse an already-authenticated session -- a page that is already signed
   in needs no credential at all.
2. If a ref is configured, run `fill-browser` against the ego-browser
   taskspace already showing the page.
3. If none is, provision one autonomously in the same bounded burst:
   verify the live origin and field, write the nonsecret manifest entry,
   reveal and copy the value from an already-unlocked Passwords entry
   through ordinary UI automation, and run `enroll --clipboard` right
   after. For a one-time or recovery code delivered by email, point the
   entry at the configured Gmail source (`kind = "gmail_otp"`) instead and
   run plain `enroll <ref>` once: the code is read live and never stored,
   so that command stores no secret -- it authorizes exactly this policy.
4. Call `mac.handoff(...)` only when macOS or the provider puts up a gate
   that needs a physical human -- Touch ID, the Mac login password,
   a passkey, a CAPTCHA, a sign-in approval -- or when there is nothing
   safe to draw the value from (no matching Passwords entry and no Gmail
   source). Missing provisioning is never by itself an immediate handoff;
   try step 3 first.

### Only a browser field is filled

The one sink is a web input inside a live ego-browser taskspace. A
manifest `field` is a CSS selector, so it can only ever mean a DOM node.
A login window in a native app keeps one of two owners -- macOS AutoFill,
accepted by the user, or an explicit `mac.handoff(...)` -- because typing
a secret at whatever currently holds first responder can land it in the
wrong control or the wrong app. Provisioning a ref from Passwords is
unaffected: it runs through the ordinary UI primitives against an
already-unlocked Passwords window plus `enroll --clipboard`.

### What a browser fill verifies

A fill is refused unless the taskspace named by `--space` exists, is
unique, is agent-owned, and is active; the broker never creates a space or
hands one off. Inside it, the fill confirms no dialog is open and that the
page's current origin matches `origins` exactly, then resolves exactly one
visible, enabled, writable input of the expected type and -- on that one
resolved DOM object, not on a selector it might re-run -- validates,
focuses, clears, and force-arms it while the page is still known good.

Only then does the secret reach the browser step, through a private FIFO
the worker creates for that fill -- the ego-browser utility does not
inherit an environment, so a pipe is what there is. Between reading the
value and typing it, the fill rechecks the page's current origin and
revalidates that same object -- the object, not a fresh selector match --
as still connected, focused, enabled, writable, visible, empty, and of the
expected type through `Runtime.callFunctionOn`. It then injects the whole
value with a single trusted CDP `Input.insertText`, confirms equality on
that same object inside CDP -- the expected value passed as a call
argument, never spliced into page source -- and confirms the origin once
more. The value is never returned to the broker, printed, or logged. The
broker fills one field -- the credential's own -- and never a username or
any other part of the form.

Every fill is bounded from the outside: 45 seconds for a password or TOTP
fill, 120 seconds for a Gmail fill, and one process group owning the whole
tree the broker starts -- mem-secret, the worker, and the `gws` and
ego-browser commands under it. A wedged provider or a stalled browser is
killed as a group and reported as `credential.timeout` instead of holding
a login open.

`check` lists the configured refs: `{"state":"checked","refs":[...]}`.
`fill-browser` returns a `CredentialReceipt`:
`{"state":"filled","credential_ref":...,"provider":...,"sink":"browser","acted":true}`.
`enroll` prints `{"state":"enrolled","credential_ref":...}`.
For a password or TOTP ref, `enroll` stores the new secret in the shared
mem-secret sops+age vault -- invoked at its pinned absolute path,
`~/.local/bin/mem-secret`, never resolved through `PATH` -- from a hidden
TTY prompt, or from stdin when piped. `enroll --clipboard` takes it from
whatever is already on the clipboard instead: normally the agent's own copy
of an already-unlocked Passwords entry, revealed and copied through the
same UI automation as any other primitive, inside the same bounded
provisioning burst. It pipes `pbpaste` straight into `mem-secret`'s stdin
at the OS level, so the secret bytes never enter the CLI's own Python
process, and it clears the clipboard on every path, success or failure,
including a failure raised before mem-secret runs -- and a clipboard it
cannot prove empty afterward is a failed enrollment, not a successful one.
It cannot make Passwords open, unlock, or reveal an entry: a Touch ID or
account-password prompt there is a physical-user boundary and the agent
stops at it.

For a `gmail_otp` ref, `enroll <ref>` reads no secret at all -- no prompt,
no stdin, no clipboard, and `--clipboard` on a Gmail ref fails before
anything is spawned, with `credential.enroll_not_authored`. There is
nothing to store, because the code is read live; what it stores is the
nonsecret authorization for that one policy -- the policy digest itself --
written straight to the vault under a name derived from that same digest.
That single command is the whole authorization step for an emailed code.
At fill time the Gmail worker runs under mem-secret with that authorization
in its environment and compares it, in constant time, against the digest of
the policy it was handed -- before it compiles a pattern or calls `gws` at
all. Absent or mismatched, the fill is refused with zero provider traffic.
Editing the manifest is therefore not enough to rebind a live Gmail code:
repoint the entry at another mailbox, sender, pattern, origin, or field and
the new policy has no authorization behind it until someone runs `enroll`
again.

Both halves of that path -- the manifest and the vault binary -- are
resolved from the account's own home directory in the passwd record rather
than from `$HOME`, so an exported `HOME` cannot move the policy file, the
vault binary, or the browser toolkit the worker uses.

Every command that fails prints one fixed, redacted `{"error":"<code>"}`
line and nothing else. No secret, secret length, OTP, email body, provider
output, clipboard value, or derived fingerprint ever reaches an argument,
receipt, error, log, or return value.

### The manifest

Credentials are declared once in `~/.config/macos-harness/credentials.toml`.
That path is fixed and is the only policy any command reads, so a fill can
never be pointed at a manifest someone else wrote -- and it must be
private to the current user before it is even parsed: a regular
non-symlink file owned by this uid at mode exactly 0600, in a directory
owned by the same uid that is neither group- nor world-writable. A 0644
manifest, a symlink, or a group-writable parent is refused with
`credential.manifest_untrusted` ("The credential manifest is not private
to this user"). Set it up once:

```bash
mkdir -p ~/.config/macos-harness
chmod 700 ~/.config/macos-harness
chmod 600 ~/.config/macos-harness/credentials.toml
```

The entry itself is nonsecret and safe for the agent to write during
provisioning. What binds it to an authorization is one policy digest: a
SHA-256 over the entry's canonical full policy -- its ref, `kind`, sorted
`origins`, `field`, and, for `gmail_otp`, every Gmail source key
(`mailbox`, `sender`, `subject_regex`, `body_regex`, `max_age_seconds`).
A password or TOTP secret is stored under a vault name derived from that
whole digest; a Gmail entry's authorization is derived from the same digest.
One enrollment therefore authorizes exactly one source and exactly one
destination, and no manifest can name, borrow, or rebind another entry's.

That has a practical consequence: changing any field of an entry --
renaming the ref, changing `kind`, adding, removing, or rewriting an
origin, pointing `field` at a different input, or editing a Gmail
mailbox, sender, pattern, or age window -- derives a different digest with
nothing enrolled behind it. The next fill fails rather than sending an old
secret to a new origin or reading a code from a mailbox nobody authorized,
so re-enroll after any edit. Only reordering `origins` is free; they are
sorted before hashing. Each kind's key set is closed, so an unknown or
misspelled key is rejected rather than half-honored and an outdated
manifest fails loudly instead of half-working. Placeholders only below;
never commit or paste a real value.

```toml
version = 1

[credentials.example-login]
kind = "password"
origins = ["https://example.com"]
field = "#password"

[credentials.example-totp]
kind = "totp"
origins = ["https://example.com"]
field = "#otp"

[credentials.example-gmail-otp]
kind = "gmail_otp"
origins = ["https://example.com"]
field = "#otp"
mailbox = "you@example.com"
sender = "noreply@example.com"
subject_regex = "verification code"
body_regex = "code is (?P<code>\\d{6})"
max_age_seconds = 300
```

The same `gmail_otp` kind covers a one-time verification code or a
temporary account-recovery PIN, as long as it arrives by email to the
configured mailbox -- match it with its own `subject_regex`/`body_regex`
rather than treating recovery as an automatic handoff. The newest matching
message wins, so a resent code supersedes the one before it.

Each pattern is bounded in length, must compile, and `body_regex` must
carry a `code` group. Matching one message then runs under a one-second
wall-clock alarm in the worker, so a pathological pattern is cut off
rather than left to spin; if that alarm cannot be armed, the message is
refused rather than matched unbounded. Nothing here claims a pattern is
linear -- the bound is the clock.

Bodies are matched as visible text. A `text/plain` part is used as it is;
`text/html` is reduced with the standard library only when there is no
plain part -- `script`, `style`, `head`, `title`, `template`, `noscript`,
and any element marked `hidden`, `aria-hidden="true"`, `display:none`, or
`visibility:hidden` dropped, entities resolved, tag boundaries becoming
whitespace. A message is used only when it yields exactly one distinct
code-shaped capture, so a quoted thread or a two-code digest is skipped
instead of guessed at (the same code repeated is still one code).

What all of that does and does not defend against -- it guards against
leaks, a wrong page or mailbox, a stale policy, and a hung step, not
against a hostile process already running as you -- is spelled out in
[SECURITY.md](SECURITY.md#threat-boundary-for-the-credential-broker).

## Native backend

`mac.*` runs entirely in Python by default. A separate, optional Swift agent
process can take over a fixed set of Accessibility calls when you want it;
nothing about the primitives above changes when it does.

Native is a process-isolation option, not a speed mode — default `python`
remains the recommended choice for latency. On an M4 Pro, a 50-query Finder
benchmark measured a Python median of 1.75 ms against a native steady-state
median of 2.07 ms, plus about 240 ms of native cold-launch cost on first use.
Results are workload-specific; measure your own with `bench/ax_smoke.py`.

```bash
export MACOS_HARNESS_BACKEND=native   # python (default) | native | auto
```

`MACOS_HARNESS_BACKEND` selects the backend for every `MacOS()` instance the
CLI creates; construct `MacOS(backend="python" | "native" | "auto")` directly
for the same choice in your own code. The default stays `python`: nothing
launches an agent process unless you opt in.

- **`python`** (default) — every call runs in-process, as it always has. The
  harness never launches or talks to an agent.
- **`native`** — every routed call goes to the agent. If the agent is
  unreachable, the call raises immediately; there is no silent fallback.
- **`auto`** — routed calls prefer the agent and fall back to the Python path
  only when the agent is unreachable *before* a real response arrives: a
  failed spawn or build, or an EOF or timeout ahead of the handshake. Once a
  genuine `ping` response has come back, `auto` never falls back: a protocol
  error, a semantic error (bad request, unknown element, timeout), a
  permission failure, or a mutating call that may have already taken effect
  all surface as errors instead of silently retrying in Python.

Each `MacOS(backend="native" | "auto")` instance that actually dispatches a
routed call launches its own private `macos-harness-agent` child process,
connected over an inherited, validated UNIX-domain socket pair that only
that Python process holds either end of. There is no shared daemon, no
well-known socket path, and no pidfile — nothing for another process on
your machine to discover or connect to. The harness confirms the child's
actual PID in its first `ping` response before routing any call to it.

```python
mac = MacOS(backend="native")
try:
    ...
finally:
    mac.close()

# or, equivalently:
with MacOS(backend="native") as mac:
    ...
```

`close()` stops the child process and closes the socket. It is idempotent
and safe to call even on a `python`-backend instance that never launched an
agent. An instance left open at interpreter exit, or dropped without
`close()`, still cleans up its child process through a finalizer, but do not
rely on that for anything time-sensitive — call `close()` yourself. Once
closed, further native-routed calls on that instance raise `MacOSError`
instead of relaunching or falling back; construct a new `MacOS()` to use
`native`/`auto` again.

The harness resolves which executable to launch, in order: an explicit
`MACOS_HARNESS_AGENT_BIN` path (must already exist and be executable, or the
launch fails immediately with no fallback to the tiers below), then the
binary bundled inside the installed package, then a fresh local SwiftPM
release build from `native/macos-harness-agent/` — rebuilt only when
missing or stale against that package's own sources, and only at this third
tier; the first two never trigger a rebuild. PyPI wheels ship a universal2
(arm64 + x86_64) build of the agent already, so most installs never reach
the third tier; building it yourself requires the Xcode Command Line Tools.

Only a fixed, narrow set of calls ever cross the socket: `list_apps`, the
bounded `ax.query`/`ax.query_all` search, `ax.press`, and the element
primitives `ax.get`/`ax.get_attributes`, `ax.set`, and `ax.perform` once you
already hold an `element_index`. Screenshots, keyboard and pointer input, the
focus sample behind `key`/`click`/`type` receipts, the animated pointer
overlay, AppleScript, full app-state snapshots, and any unrouted or
parameterized AX call stay local to the Python process on every
backend. An `element_index` returned by a native query is not a raw
agent-side number — the client interns it into the same monotonic element
registry local queries use, so `ax.get`/`ax.set`/`ax.perform` accept it
exactly like a Python-minted index, and a stale index still raises instead of
silently aliasing a different element. The two reads keep the contracts the
Python backend gives them: `ax.get` is one checked read, so a read the app
refuses raises (`timeout` when it did not answer, `ax.error` otherwise) and
`set`/`toggle`/`equals` never judge a state from a value that was never
read; `ax.get_attributes` is a bulk sample where an unreadable attribute is
`None`. Query results include completeness metadata. The client and agent use
protocol version 2; an older configured agent is rejected at the handshake
and must be rebuilt or replaced. Each `ax_press` request includes
`action_deadline`, a macOS monotonic timestamp in seconds, or JSON `null`
for the raw zero-timeout single attempt. The agent checks the deadline before
searching and again after reading the frontmost app.

## How it works

```text
                              one persistent Python process
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    │                      │                      │
                 mac.*                  browser.*          Path / subprocess
                    │                      │                      │
        ┌───────────┼───────────┐     Browser Harness        files + shell
        │           │           │            │
    CGWindow     CGEvent      AX + Apple      CDP
   screenshots    to PID       Events          │
        │           │           │          real Chrome
        └───────────┴───────────┘
                    │
              native + Electron apps
```

- Captures one app window through ScreenCaptureKit, behind other windows or
  on another Space, without bringing it forward
- Sends keyboard and coordinate input directly to an app PID
- Draws an animated, click-through pointer at the system cursor size without
  moving your real cursor
- Exposes raw Apple Accessibility and Apple Events when vision is not enough
- Uses Browser Harness for the real, logged-in browser
- Keeps ordinary Python and the local filesystem within reach
- Wraps mutations in `mac.do` for a receipt, optional verification, and
  idempotency — see [Machine-first](#machine-first-macdo-for-mutations-raw-primitives-underneath)
- Can hand a fixed set of Accessibility calls to a supervised native agent
  over a local socket; off by default, see [Native backend](#native-backend)
- Declares an explicit human handoff at a known authentication boundary
  instead of guessing from a timeout — see [Human handoff](#human-handoff-for-authentication-boundaries)
- Fills a provisioned login field in the real browser from one authorized
  policy, and never a native control — see [Credential broker](#credential-broker-for-provisioned-logins)

## Permissions and privacy

`macos-harness doctor` reports the macOS permissions actually needed. The harness
never activates a target app on its own (`mac.activate()` is explicit and reports
the observed result) and never moves the physical pointer.

Telemetry is off by default. Nothing is sent until you run `macos-harness
telemetry enable`, and a kill switch always wins even after that:
`DO_NOT_TRACK=1`, `MACOS_HARNESS_TELEMETRY=0`, or `ANONYMIZED_TELEMETRY=0`
disables it regardless of the stored setting, and no environment variable can
force it back on. Once enabled, each CLI invocation sends one event carrying
a persistent random install ID (generated locally, not derived from any
hardware or user identifier) plus the command category, success, duration,
package version, Python `major.minor`, OS, CPU architecture, and detected
coding-agent client (Codex, Claude Code, Cursor, Gemini CLI, or opencode). It
never carries prompts, app names, screenshots, UI text, scripts, paths, or
window titles. Config lives at `$MACOS_HARNESS_HOME/telemetry.json` (default
`~/Library/Application Support/macos-harness/telemetry.json`, mode `0600`);
events go to PostHog EU (`https://eu.i.posthog.com`), and an endpoint
override is honored only when it is HTTPS.

```bash
macos-harness telemetry enable    # opt in
macos-harness telemetry status    # see exactly what would be sent, and where
macos-harness telemetry disable   # opt back out
```

Experimental. Requires macOS 13+ and Python 3.11 or newer (tested
3.11-3.14). [MIT licensed](LICENSE).
