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

- `press`, `set`, `fill`, `toggle`, `run`, `key`, `click`, and `type` mutate;
  `expect(condition)` and `expect_any(outcomes)` observe conditions, and
  `recall(once)` looks up a past receipt by its token, without dispatching
  anything. `set`/`toggle` are convergent: they read the current state
  first and report `outcome="already"` instead of touching anything
  already correct.
- Use `fill(value, app=..., identifier=...)` to replace a plain text field.
  It focuses one unique enabled field, selects its full UTF-16 range, types
  through the app's input handler, and verifies the text. Unlike an AX value
  write, this sends the keyboard events a SwiftUI binding needs. Pass
  `value=""` to clear. Secure fields are refused, and no Return key is sent.
  Give `fill` an app-level postcondition when the result matters beyond the
  field itself: `equals(title="Check", role="button", attribute="AXEnabled",
  value=True)` checks that the app accepted the input.
- `expect_any({"saved": present(app=app, identifier="saved"),
  "error": present(app=app, identifier="error")}, timeout=5)` waits for the
  first observation pass with a satisfied condition. Read
  `receipt.observed["matched"]` for every matching name in that pass. Each
  condition needs an explicit app scope. One cooperative deadline covers all
  checks; an AX read already in progress can overrun it. Timeout receipts keep
  each outcome's latest state and reason. No input or activation is sent.
- Run stdin programs with `macos-harness --json-errors` for uncaught harness
  errors as JSON on stderr. Operation failures include their receipt. The
  nonzero exit status and the default text error format are unchanged.
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
The supported steps are `press`, `set`, `fill`, `toggle`, and `key`. Presses and
keys require an explicit postcondition; set and toggle retain their
value-convergence checks. Only boolean and numeric set/equals values are
recordable. Selectors and key combinations are stored verbatim, so keep
secrets out of them.

For forms, record a parameter name with `fill` and supply its text separately:

```python
entry = present(role="text field", identifier="domain")
goal = present(role="static text", identifier="result")
with mac.route.record(
    "check-domain", app=app, entry=entry, goal=goal,
    inputs={"domain": "example.invalid"},
) as rec:
    rec.fill("domain", identifier="domain")
    rec.press(role="button", identifier="check", postcondition=goal)

result = mac.route.run(
    "check-domain", app=app, inputs={"domain": "another.example.invalid"},
)
```

The saved definition contains `domain`, not the supplied text.
`mac.route.list(app=app)` lists the required input names. Missing or unknown
names and invalid text stop replay before any step acts. Input-bearing routes
check the entry and run even if the previous goal still holds: that goal may
describe the previous input. Never put private text in selectors or labels.

Recording checks the entry before yielding and the goal before saving. A failed
step prevents saving even if its exception is caught. The previous file stays
intact. Keep the handle on the thread that entered the block; it closes when the
block exits. A definition has at most 64 steps and a 1 MiB file limit.

Replay validates the whole definition before input. For routes without form
inputs, it returns `already` if the goal holds, otherwise checks the entry and
runs each step once. Only a goal read that came back `Observation.UNMET`
authorizes those steps; any
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

For control-level changes, compare two consecutive inspections of the same app:

```python
before = mac.inspect(app, include_values=True)
# Perform the intended operation.
after = mac.inspect(app, include_values=True)
changes = mac.diff(before, after)
print(changes["changed"], changes["added"], changes["removed"])
```

Stable `ref` values identify controls; `element_index` remains a short-lived
action handle. A partial walk cannot prove a control was added or removed,
so uncertain appearances and disappearances go under `unproven`. Diffing
does not read the desktop. Both observations must come from one `MacOS`
instance and the same app lifetime, with no intervening successful inspection.
Values remain opt-in and may contain private text.

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
