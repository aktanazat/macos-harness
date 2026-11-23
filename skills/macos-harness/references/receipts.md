# Receipts: `mac.do` in detail

A `mac.do` verb returns a `Receipt`: `outcome` (`planned`/`done`/`already`/`failed`), `acted` (`no`/`yes`/`unknown`), `changed`, `verified`. `print(receipt)` is one line; `receipt.to_json()` the whole record. Use raw primitives only for reads or actions `mac.do` does not model.

```python
from macos_harness.receipts import equals, gone

not_now = dict(text="Not Now", role="button", all_apps=True)
receipt = mac.do.press(**not_now, postcondition=gone(**not_now), once="dismiss-not-now")
receipt = mac.do.type("hello", app="TextEdit", postcondition=equals(role="text area", value="hello"))
```

## Verbs

- `set`/`toggle` report `already` instead of re-mutating; they take no `once`.
- `fill(value, app=, identifier=)`: focuses one enabled field, selects all, types once, verifies; `""` clears; refuses secure fields; never submits. Add an app-level postcondition (e.g. a button's `AXEnabled`) to prove the app took it.
- `type`, `key`, `fill`, `paste`, text `set` refuse while any app holds secure keyboard entry (`secure_input_in_app`, `secure_input_elsewhere`) or for sign-in prompts and password managers (`secret_keeper_app`): ask the person. `mac.script` is not guarded.
- `paste(text, app=)` (also `html=`, `rtf=`, `tsv=`, `files=`) needs the app in front (`target_in_background` otherwise), restores the clipboard, and reports `observed["input"]["pasteboard"]`.
- `open_files(files, app=)` hands files over without activating and waits for each window's `AXDocument`; use a postcondition for apps that name no document (Preview).
- `steps([...], app=)`: first failure ends the run; nothing is retried.

## Errors

A bad argument raises `MacOSError` before dispatch (nothing attempted). Otherwise `OperationError` carries `exc.receipt` (`.outcome`, `.acted`, `.error["code"]`). An uncaught one ends the program: catch it when later steps should run, then look before acting again.

## What `changed` means

- `True`: a postcondition verified the effect or focus moved. `None`: nothing observable moved (how a silently dropped key shows). `press`/`run` have no readback: `None` without a postcondition.
- `type` without a postcondition reads a plain text field back until the text lands; it never types twice.
- `equals` stores only length/SHA-256 of values.

## Waiting without input

- `mac.do.expect(condition)`: read-only check, 5 s default.
- `mac.do.expect_any({"saved": present(app=app, identifier="saved"), "error": present(app=app, identifier="error")}, timeout=5)`: read `receipt.observed["matched"]`.

## Idempotency and budgets

- `once` on `press`/`run`/`key`/`click`/`type` for an action you cannot repeat: a retry with the same token replays the receipt. The ledger lives only in that `MacOS` instance.
- `dry_run=True` resolves without dispatching.
- `timeout` is cooperative. These sent nothing (`acted="no"`, token stays free): `deadline_exhausted_before_dispatch`, `app_input_busy`, `focus_moved_before_dispatch`, `person_active`, an element click's `still_moving`. Other timeouts may have acted: keep the token and inspect.
- `run` keeps source and output as hashes; `capture_output=True` only when you need the text.

## History

- `mac.timeline()`: latest 256 receipts.
- `MACOS_HARNESS_RECEIPT_LOG=1` appends receipts (no values) to `~/Library/Logs/macos-harness/receipts.jsonl`.
