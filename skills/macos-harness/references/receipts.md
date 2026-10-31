# Receipts: `mac.do` in detail

For anything that changes state, try a `mac.do` verb before its raw-primitive
equivalent. It performs the same action but returns a `Receipt`: `outcome`
(`planned`/`done`/`already`/`failed`), `acted` (`no`/`yes`/`unknown`), whether
anything changed, and whether a postcondition verified the effect.
`print(receipt)` shows those in one line, such as
`press done acted=yes changed=yes verified=yes 0.31s`, with `error=<code>`
on a failure; `receipt.to_json()` holds the whole record.

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

Drop to the raw primitive only when no `mac.do` verb covers the need:
identity-only lookups, reads, or an action `mac.do` does not model. Raw
primitives carry no receipt and no idempotency guarantee.

## Verbs

- `press`, `set`, `fill`, `toggle`, `run`, `key`, `click`, and `type` mutate;
  `expect(condition)` and `expect_any(outcomes)` observe, and `recall(once)`
  looks up a past receipt without dispatching anything.
- `set`/`toggle` check first and report `outcome="already"` instead of
  re-mutating a target already in the requested state, so neither takes `once`.
- `mac.do.fill(value, app=..., identifier=...)` is for a plain text field,
  especially when `set` changes visible text but leaves an app button disabled.
  It resolves one enabled field, focuses it, selects its full UTF-16 range,
  types once, and verifies the text. It stops if focus or text changes before
  typing. `value=""` clears. Secure fields are refused; it never submits. Add an
  app-level postcondition, such as
  `equals(title="Check", role="button", attribute="AXEnabled", value=True)`,
  to prove the app processed the input rather than just displaying it.

## Errors

A call either returns a `Receipt` or raises `OperationError`; read
`exc.receipt` for the same detail (`.outcome`, `.acted`, `.error["code"]`). A
bad argument (an unknown role, a `once` token reused for a different request)
raises `MacOSError` before anything is dispatched, with no receipt: nothing was
attempted. An uncaught `OperationError` ends a stdin program, so catch it when
later steps should still run, then look before acting again.

## What `changed` means

- `set`/`toggle` read the target back every `interval` until the requested
  state shows or the deadline runs out, without repeating the mutation.
- `key`/`click`/`type` sample focus before and after (frontmost pid, focused
  window, focused element's role, value summary, selected range, character
  count, position, size; a secure field gives role and subrole only) and report
  the fields that moved under `observed.focus`. A reading the app refuses is
  `None` and never counts as focus moving. Without a postcondition the
  after-reading repeats every 10 ms for up to 100 ms until it differs; with
  one, the postcondition is checked right after a single after-reading.
- `type` without a postcondition reads a plain text field back until it
  shows the typed text where the selection was; the receipt is then
  verified, with `observed.input.landed`. Text it never shows leaves the
  receipt unverified after the timeout. It never types twice.
- `changed` is `True` when a postcondition verified the effect or focus moved,
  and `None` when nothing observable moved, which is how a key AppKit silently
  dropped shows up. `press`/`run` have no readback, so `changed` is `None`
  unless a postcondition confirms the effect.
- `equals(..., value=, attribute="AXValue")` reads one match's attribute every
  `interval` until it equals `value`. The receipt keeps length/SHA-256
  summaries of expected and observed values, never the values.

## Waiting without input

- `mac.do.expect(condition)` checks `present`, `gone`, or `equals` read-only.
  Scope the condition explicitly. Its timeout defaults to five seconds. A
  success has `acted="no"`, `verified=True`, `changed=None`; failure raises
  `OperationError`.
- `mac.do.expect_any({"saved": present(app=app, identifier="saved"), "error":
  present(app=app, identifier="error")}, timeout=5)` ends on whichever outcome
  appears. Read `receipt.observed["matched"]`. One cooperative deadline covers
  the checks; a running AX read can overrun it. A timeout keeps each outcome's
  latest state and reason, not a guessed winner.
- `mac.ax.wait(text, role=..., app=..., timeout=...)` is the raw wait for the
  next control after a press: see the card's example.

## Idempotency and budgets

- Pass a nonempty `once` on `press`/`run`/`key`/`click`/`type` before an action
  you cannot safely repeat, with the same request each time. The ledger lives
  only in the one `MacOS` instance that dispatched it: gone on a crash, a fresh
  `MacOS()`, or a new process, and never written to disk. A retry with the same
  token and request replays the recorded receipt; an interrupted attempt fails
  closed instead of risking a second dispatch.
- `dry_run=True` validates and resolves (and, for `run`, compiles) without
  dispatching. `press`/`set`/`toggle` take an `interval` for their own polling;
  `key`/`click`/`type` watch focus on a fixed 10 ms cadence and `run` polls for
  nothing. A postcondition carries its own `interval`.
- `timeout` is a cooperative budget. An expired budget stops the next mutating
  call. Presses share it across agent startup, searches, retry delays, and the
  frontmost-app reading. A press stopped with
  `deadline_exhausted_before_dispatch` reports `acted="no"` and keeps its `once`
  token available, as does an input or menu verb stopped with `app_input_busy`
  while another harness run kept sending the app input, and a `key` or `type`
  stopped by `focus.changed` with reason `focus_moved_before_dispatch` because
  keyboard focus moved before it sent anything. Other timeouts can mean the
  action happened: keep the token and inspect the receipt. A synchronous AX or
  input call already in progress cannot be preempted. Raw
  `mac.ax.press(timeout=0)` makes one attempt; `mac.do.press(timeout=0)` does
  not dispatch.
- `run` receipts keep source, arguments, and output as length/SHA-256 metadata.
  Pass `capture_output=True` only when you need bounded stdout/stderr text and
  accept that it can contain sensitive data.

## History and the receipt log

- `mac.timeline()` returns the latest 256 non-replayed receipts in completion
  order, including failures and tokenless calls, as JSON-safe data. Raw
  primitives are outside it. Replays keep the original `started_at`/
  `finished_at` UTC times; elapsed time and deadlines stay monotonic. Export it
  with `json.dumps` and `Path.write_text` when a file is needed.
- Set `MACOS_HARNESS_RECEIPT_LOG=1` to append every finished receipt as one
  JSON line to `~/Library/Logs/macos-harness/receipts.jsonl`: time, process,
  verb, app, the control's role and labels, outcome, acted, duration, and error.
  Values, typed text, and script source are never written, and `type`/`fill`
  errors keep only their code. The file rotates to `receipts.jsonl.1` at 5 MiB.
  It is off by default.
