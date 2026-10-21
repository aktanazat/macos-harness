# Saved navigation

## Replay

Use `mac.route.run(name, app=exact_bundle_id, timeout=30)` for a previously
recorded navigation sequence. Inspect `status`, `at`, `error`, `check`, and
`steps_run`. A diverged run keeps the failing receipt, including whether it
acted. Stop and inspect the current state before another run; every call has a
fresh run id and may repeat input. There is no retry, resume, rollback, or
implicit activation.

## Record

Record through `with mac.route.record(name, app=exact_bundle_id, entry=entry,
goal=goal) as rec:`. Call `rec.press`, `rec.set`, `rec.fill`, `rec.toggle`, or
`rec.key` only. Unrelated Python and calls outside the handle are not recorded.
Keep the handle on its with-block thread. A failed step prevents saving, even
when caught; the previous file stays intact.

Give every target and condition a role plus one exact identifier, title, or
description. Press and key steps need a postcondition. Set and toggle keep
their convergence checks. Set and equals values must be boolean or numeric.
Keep secrets out of selectors and keys, which are saved verbatim. Supply both an
entry condition and a terminal goal. A definition holds at most 64 steps.

## Forms

Pass `inputs={"domain": text}` to recording and replay, then call
`rec.fill("domain", identifier="domain-field")`. The definition saves the
parameter name, not the text. Replay rejects missing names, extra names, and
invalid text before any step acts. Routes with inputs check the entry and run
even when the old goal holds; that goal may describe the previous input.
`mac.route.list(app=exact_bundle_id)` includes the required names under
`inputs`.

## Dry runs and limits

`dry_run=True` validates the whole definition without input or accessibility
feature changes. It checks the current goal, or the entry and first target;
later controls need not exist yet. A refused or incomplete check stops the run.
Replay stays on one app process and shares one cooperative deadline.
`mac.route.list` reads saved definitions without observing the app. Reported
app versions come from the on-disk bundle; a difference alone does not reject a
run.
