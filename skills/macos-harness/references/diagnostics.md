# Inspecting a failure before more input

- Keep the failed `OperationError.receipt`; preserve `acted=unknown`.
- `mac.status(app_or_receipt)`: process and build metadata without a UI read. `mac.inspect(app_or_receipt)`: controls, focus, windows, sheets; read `process`, `coverage`, `blocked`. A partial walk cannot prove no sheet exists. Values and screenshots only with `include_values=True` / `screenshot=True` when the task permits.
- `mac.diff(before, after)` compares two consecutive `inspect` results by `ref`; read `changed`, `added`, `removed`, `unproven`. `mac.diff_windows` for window snapshots.
- `mac.logs(receipt)`, `mac.crashes(receipt)`, `mac.sample(app, duration=1)` only when they can answer; absent records rule nothing out.
- `mac.explain(receipt, *evidence)` combines them without new observations. A changed executable mtime suggests a stale build.
