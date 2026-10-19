# Accessibility search

`mac.ax` reaches controls in the target app and also background AutoFill
sheets and system popovers outside it:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

## Roles

`query`, `query_all`, `wait`, `wait_gone`, and `press` take the search text as
the first positional argument. Use `role=` for common targets: `any`, `button`,
`checkbox`, `combo box`, `image`, `link`, `list`, `menu`, `menu item`,
`radio button`, `static text`, `table`, `text area`, and `text field`, or the
same role by the AX name a match reports (`AXButton`, `AXTextField`). An
unknown role raises `MacOSError`. For a window, row, group, scroll bar, or any
other kind, pass `role="any"` and read each match's `role`; `mac.windows(app)`
lists an app's windows. Do not pass both `role` and `search_key`. A search for
what is on screen, the default, leaves out the items of closed menus; pass
`role="menu item"` to reach a command in a menu that is not open.

## Scope

Each of them, and `mac.do.press`, `set`, and `toggle`, takes one scope: `app=`
for one app, `all_apps=True` for every running app, or `apps=` for a set. Two
scopes, a blank selector, a pid below 1, or an empty `apps` raise `MacOSError`
with code `bad_request` before anything is searched. With no scope, raw calls
use the last app and `query_all` searches every app; `mac.do` mutations need
one. Pass `apps=` one app name, bundle ID, path, or PID, or an iterable of
them; `apps="Safari"` is one selector, not an iterable of characters.

## Matching

`text` is a substring filter, so `"Allow"` also finds `"Don't Allow"`. When it
finds several matches and exactly one has a title or description equal to
`text`, case included, `wait`, `press`, and `mac.do` take that one. A press,
`mac.do.set`, `toggle`, or `fill` then takes the one enabled match among the
exact labels, or among all matches when none is exact: a sheet that keeps
disabled twins of its "Done" button still gets the working one pressed. The
label decides first, so a disabled "Allow" is never swapped for an enabled
"Don't Allow". The optional native backend's press still treats such a tie as
ambiguous. Anything else, or a search that filled its limit, fails closed and
lists every match with its frame and whether it is enabled.

`title=`, `identifier=`, and `description=` match whole attributes, case
included; all supplied fields must match. Exact waits and presses need a
complete search before accepting a single match. An `all_apps` sweep is often
incomplete because some processes do not answer, so give an exact selector one
app: `mac.ax.press(title="Allow", role="button", app="UserNotificationCenter")`.

Cross-process calls need non-empty text or an exact selector. Default
attributes exclude `AXValue`; read it with `ax.get` or explicit attributes.

## Completeness

Query results are lists with `complete` and `visited` metadata.
`mac.ax.query()` bounds fallback traversal at `max_nodes`; a walk cut short
reports itself incomplete. A cross-app search applies one global `limit`, times
out each process, and returns owner metadata under `app`. A broad search first
asks every app at once whether it can answer and skips the ones that cannot,
which leaves it incomplete; it counts a skipped app against completeness only
while that app has a window on screen. If a limit, failed read, or skipped
process leaves a search incomplete, narrow the scope. A wait that times out on
an incomplete search names the apps it searched only in part, or says the app
did not answer.

## Waits and presses

`wait` returns the one match; zero matches keep polling; several fail closed and
report owner, role, title, frame, and enabled state. An app that does not
answer in time (AXError -25204), as one still launching does for a second or
more after `open -a`, also keeps the wait polling until its timeout.
`wait_gone` needs two consecutive complete, empty searches; a named app that
exits counts as gone. `press` waits for one match, needs `AXPress`, and returns
the match. It never requests activation; if the target makes itself frontmost,
it raises `FocusChangedError` and cannot undo that.

## Handles

Element indexes and `mac.see()` screenshots last for one `macos-harness` run,
and an element handle only until the next AX snapshot or search in it. The next
run must query again, or call `mac.see()` earlier in the same program.

An action that fails with AXError -25204 (the app did not answer in time) may
still have taken effect, because apps often stop answering while the action
opens a menu or dialog. Look before you retry. An action whose element is gone
right afterward, such as a sheet's Delete button that closes the sheet, counts
as done even when the app answered it with an error.

These calls act only on UI macOS already rendered. They cannot make secure UI
appear in an inactive app or get past Touch ID, passkeys, CAPTCHA, account
recovery, or another check that needs the user.
