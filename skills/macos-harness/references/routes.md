# Saved navigation

## Replay

`mac.route.run(name, app=exact_bundle_id, timeout=30)`: read `status`, `at`, `error`, `check`, `steps_run`. No retry, resume or rollback, and each run may repeat input: inspect state before running again. `dry_run=True` validates without input.

## Record

```python
with mac.route.record(name, app=exact_bundle_id, entry=entry, goal=goal) as rec:
    rec.press(...); rec.set(...); rec.fill(...); rec.toggle(...); rec.key(...)
```

- Only `rec.*` calls are recorded; keep the handle on its thread. A failed step prevents saving.
- Every target needs a role plus one exact identifier, title or description; press and key steps need a postcondition; set values are boolean or numeric. Max 64 steps. Selectors are saved verbatim: no secrets.

## Watch

`watch = mac.route.watch(name, app=, entry=, goal=, timeout=300)` records the person doing the task once; they end it with Control-Option-Command-Period (or `watch.stop()`); `watch.wait()` returns `saved` with `path`, `steps`, or `unsaved` with `error`. Saved only if `goal` holds.

- A secure field becomes a `secret` step; replay stops there with status `handoff`. macOS hides the stop hotkey while that field has focus.
- Right clicks, modifier clicks, drags in text, mouse-moved sliders, accent keys and unlocatable controls make it save nothing; it also stops if it falls behind: ask the person to go a little slower.
- Needs only Accessibility; a refused listener names Input Monitoring.

## Forms

`inputs={"domain": text}` on record and run, `rec.fill("domain", identifier="domain-field")`. Only the parameter name is saved; replay rejects missing or extra names. `mac.route.list(app=)` shows required `inputs`.
