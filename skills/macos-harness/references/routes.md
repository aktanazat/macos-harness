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

## Watch

`watch = mac.route.watch(name, app=exact_bundle_id, entry=entry, goal=goal,
timeout=300)` watches the person at the Mac do the task once by hand and
returns at once. The person ends it with Control-Option-Command-Period; it
also ends at `timeout` or `watch.stop()`, and `watch.wait()` blocks until it
does. Both return `status` `saved` with `path` and `steps`, or `unsaved` with
`error`; `skipped` lists ignored clicks. Nothing is saved unless `goal` holds
at the end.

A click becomes a press, a `set` of `AXFocused` on a text field, or a `set`
of `AXSelected` on a row, located by role and the first title, description,
or identifier that finds exactly that control. A click on a menu bar title
and then on an item becomes a `menu` step, `"View > Scientific"`; a pick in a
pop-up button is a press on the button and one on the item. Text typed into
a text field, text area, or combo box becomes a `type` step holding the text
verbatim. It checks that the field has keyboard focus, and so does the step
before it, so replay stops before typing anywhere else. Every other key, and
any held with Command or Control, becomes a `key` step, so an app that reads
keys one at a time, such as Calculator, replays them one at a time. While a
secure text field has focus nothing typed is kept: the route holds a
`secret` step naming the field, and replay stops there with status `handoff`
and a `handoff` to show the person. macOS hides every key from the watch
while that field has focus, the stop hotkey included, so click outside it
first or call `watch.stop()`. Each other step checks the control the next
action went to, or its own control still present; the last checks the goal.

The watch saves nothing on a control it cannot locate, a step it cannot
check, a right click, a click with modifiers, a double click or drag in a
text field, a double-clicked row, a slider or stepper moved with the mouse, a
menu item picked by dragging, a key other than Escape or an arrow while a
menu is open, an unknown key, a key whose text it cannot read, such as an
accent key or a key held down until it repeats or opens the accent menu, a
click still under way when the watch ends, or secure keyboard entry hiding
keys outside a secure field. It also stops when it falls behind: a click or
key that came before its last read of the app may already show in that
read, so it asks for the task again, a little slower. A password typed
right after Tab or a click, before that read, is still kept as the
`secret` step. An app whose accessibility trails its input, such as Chrome,
can show the field from before Tab or a click while keys already go to a
password field, so the watch also stops when keys it kept as text since
the person last moved focus meet a secure field in focus or secure keyboard
entry coming on. Keys after Tab or Return that still read as the field
from before it must show in that field's value by the person's next click,
Tab, Return or key in another field, or the watch stops. The watch also
stops when a key reads as another field after a secure field took focus
but before macOS hid keys. Clicks and keys in other apps are ignored.
Watching needs no permission beyond Accessibility, which lets macOS show
the watch the keys; if macOS still refuses the listener, the error names
Input Monitoring.

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
