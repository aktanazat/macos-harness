# Inspecting a failure before more input

Keep the failed `OperationError.receipt`. Its `process` evidence belongs to the
original completion; a replay does not refresh it. Preserve `acted=unknown`
when input is uncertain. Process exit evidence does not replace an existing
action error, and a verified expected disappearance can still succeed.

## Current state

Use `mac.status(app_or_receipt)` for process and on-disk build metadata without
a UI read. Use `mac.inspect(app_or_receipt)` when current controls, focus,
windows, or blocking sheets matter. Read `process`, `coverage`, and `blocked`
before interpreting the result. A partial walk cannot establish that no sheet
exists. A failed receipt adds current same-role controls under `nearby`.

Inspection excludes values and screenshots by default and does not enable
accessibility features. Request `include_values=True` or `screenshot=True` only
when the task permits that data. Secure fields and failed identity reads still
exclude content attributes. Titles and labels can contain private text.
Snapshots do not freeze the app.

## Comparing

`mac.diff(before, after)` compares two consecutive `mac.inspect(app)` results.
Match nodes by their stable `ref`, not the short-lived `element_index`. Both must
come from the same `MacOS` instance and app lifetime. Read `changed`, `added`,
`removed`, and `unproven`: incomplete walks put uncertain appearances and
disappearances under `unproven`. `mac.diff_windows(before, after)` compares
window snapshots. Neither makes a new observation. Values still need an
explicit opt-in.

## System records

Collect `mac.logs(receipt)` or `mac.crashes(receipt)` only when those records
can answer the failure. An explicit time pair needs `app=pid`. Check collection
status and truncation; delayed or absent records do not rule out a failure.
`mac.sample(app_or_receipt, duration=1)` collects a bounded call graph, which
does not prove a hang. Diagnostic text can contain private data.

Pass collected results to `mac.explain(receipt, *evidence)`. It keeps the
action receipt and makes no new observations or retries. A changed executable
mtime is evidence of a possibly stale build, not the running build's version.
The README's Diagnostics section documents collection limits and result fields.
