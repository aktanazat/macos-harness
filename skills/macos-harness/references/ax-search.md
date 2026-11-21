# Accessibility search

`mac.ax` also reaches AutoFill sheets and system popovers outside the target:

```python
mac.ax.press("Not Now", role="button", all_apps=True)
```

## Roles

- `role=` takes an AX name (`AXButton`) or words (`pop up button`); a typo raises naming the nearest. Never pass both `role` and `search_key`.
- `role="window"` lists windows; `mac.windows(app)` gives ids.
- Closed-menu items: `role="menu item"` (press only, no click). Same-named items in two menus: `mac.do.menu("Window > Close", app=)`. Menu bar icons: `mac.do.status_item("Quit", app=, name=)`.

## Scope

Exactly one of `app=`, `all_apps=True`, `apps=` (one selector or an iterable; `apps="Safari"` is one). `mac.do` mutations need one. Cross-process calls need non-empty text or an exact selector.

## Matching

- `text` is a substring (`"Allow"` matches `"Don't Allow"`); a single exact title/description among several wins, then the one enabled match. Otherwise it fails closed listing every match.
- `title=`, `identifier=`, `description=` match whole attributes, case included. `all_apps` sweeps are often incomplete; give exact selectors one app: `mac.ax.press(title="Allow", role="button", app="UserNotificationCenter")`.
- Default attributes exclude `AXValue`: read with `ax.get`. Secure fields never expose values.

## Completeness

Results carry `complete` and `visited`. A limit, failed read or skipped process makes a search incomplete: narrow the scope.

## Waits and presses

- `wait` polls until exactly one match; several fail closed. Timeouts list `elsewhere` and `nearest` labels.
- `wait_gone` needs two complete empty searches an `interval` apart; an exiting app counts as gone.
- `press` needs `AXPress` and never activates.

## Handles

- Element indexes and screenshots last one run; an index only until the next search. Pass matches across runs; twins raise naming each `window_id`: `{**match, "window_id": id}`.
- AXError -25204 (app did not answer) may still have acted: look before retrying. An element gone right after its action counts as done.
- None of this gets past Touch ID, passkeys, CAPTCHA or recovery: `mac.handoff`.
