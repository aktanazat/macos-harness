# Signatures

Parameters after `*` are keyword-only. A wrong keyword's `TypeError` ends with the real signature.

CLI: `macos-harness see APP_OR_PID --path out.png [--window-index 0 | --window-id N]`; `macos-harness run FILE.py [--arg NAME=VALUE ...]` (strings in `args`); `-c 'CODE'`; `--json-errors`; `apps [NAME]`; `state APP [--max-nodes N]`; `menus APP "words"`; `sdef APP`; `files`, `file-info PATH`, `shortcuts [NAME --input F --output F]`.

`MacOS(*, backend=None, agent_id=None)`; `--agent-id NAME` or `MACOS_HARNESS_AGENT_ID` names the caller's pointer (1–64 chars, one per parallel agent).

## Primitives

```text
mac.state(app, *, query=None, window_id=None, diff=True, max_nodes=2000)
mac.text(app, *, query=None, window_id=None)
mac.see(app=None, *, window_index=0, window_id=None, display=None, region=None, path=None, max_width=1280, max_height=1280, crop=None, zoom=1.0, show_pointer=False, after_change=False, timeout=10.0, settle=0.15)
mac.wait_change(app=None, *, window_index=0, window_id=None, region=None, by="pixels", timeout=30.0, settle=0.15)
mac.key(key, *, app=None, window_id=None)
mac.type(text, *, app=None, window_id=None)
mac.click(x=None, y=None, *, element=None, app=None, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot")
mac.scroll(delta_y, delta_x=0, *, app=None, window_id=None, screenshot=None, unit="pixel", x=None, y=None, coordinate_space="screenshot")
mac.drag(from_x, from_y, to_x, to_y, *, app=None, window_id=None, screenshot=None, button="left", coordinate_space="screenshot", duration=0.25, steps=12)
mac.move(x, y, *, app=None, screenshot=None, coordinate_space="screenshot", duration=0.16)
mac.script(source, *, language="AppleScript", timeout=15.0)
mac.sdef(app)
mac.activate(app=None, *, timeout=0.5)
mac.launch(app, *, timeout=15.0, window=True)
mac.quit(app, *, timeout=10.0, force=False)
mac.relaunch(app, *, timeout=15.0, window=True)
mac.frontmost()
mac.windows(app=None, *, all_layers=False)
mac.wait_for_window(app=None, *, title=None, timeout=2.0, all_layers=False)
mac.place(app=None, *, window_index=0, window_id=None, x=None, y=None, width=None, height=None, display=None)
mac.menus(app, path=None)
mac.menu_index(app, *, refresh=False)
mac.menu_search(text, *, app, limit=10, refresh=False)
mac.notifications()
mac.list_apps()
mac.show_pointer()
mac.hide_pointer()
mac.handoff(*, reason, app)
```

- `app` takes a name or pid; a name matching several processes warns: pass the pid or `window_id`.
- Input that takes focus or keys from the person waits for a 450 ms pause in their input; after 2.5 s it raises `timeout`, reason `person_active`, having sent nothing.
- `see(crop=(x, y, w, h), zoom=2)` captures part of a window; `see(display=0)` or `region=` captures the screen composited, with `shows` listing visible windows.
- `place` moves/resizes via Accessibility without activating.
- `mac.scroll`: negative `delta_y` scrolls down; pixels unless `unit="line"`.
- `mac.menu_index(app)` caches every menu item per app version; `mac.do.menu_search` picks the single best match and refuses ties, submenus, disabled items.
- `mac.notifications()` lists banners with `actions`; `mac.ax.perform(note["element"], "Close")` dismisses one.
- One process at a time holds ScreenCaptureKit; others fall back to `screencapture`.

## AppleScript

Shell `osascript` lacks the harness's Accessibility (`-25211`); run the source through `mac.script`. Name processes by `unix id`, not name:

A script cannot type into a password field. It runs in a sandboxed process,
and each Apple event it sends is checked first: while a password field has
the keyboard (Secure Input is on) an event for System Events is refused with
`unsupported_op`, and a `keystroke` or `key code` is pressed key by key in
the app in front, each key checked again. `run script` text and a program
started by `do shell script` cannot control an app at all (`-600`), `key
down`/`key up` are refused, and the script's own process shows no dialog:
have an app show it (`tell application "System Events" to display dialog`).

```python
mac.script('tell application "System Events" to click button "OK" of window 1 of (first process whose unix id is 4242)')
```

`mac.sdef(app)` reads the app's dictionary (`commands`, `classes`, `enumerations`) without launching it; none raises `unsupported_op`: use System Events.

## State and text ids

`print(mac.state(app))` lists one control a line (`[12] button "All Clear"`). Ids persist across runs for the same `agent_id` and app launch; `state[12]` refuses an id whose line changed since your last printed look. With an `agent_id`, later looks print only changes; `diff=False` prints all. `query=` lists matching lines.

```python
s = mac.state("Calculator")
for key in (9, 12, 13, 24):
    mac.ax.perform(s[key], "AXPress")
```

`mac.text(app)` OCRs the window (Screen Recording, ~0.3 s) where accessibility names nothing, such as iPhone Mirroring: `text[3]` is a match for `click(element=)` / `do.click(element=)` only.

## Across runs

- `screenshot="<png>"` on `click`, `drag`, `scroll`, `move`, `ax.at`, `do.click` maps an earlier `see`'s pixels; a moved window raises `window.changed`.
- Pass a printed match (not an index) to reuse an element later: `mac.ax.perform({"pid": 4512, "role": "AXButton", "title": "Save"}, "AXPress")`; none or several found raises `element.unknown`.
- A control that ignores `AXPress` (SwiftUI rows) takes `click(element=match)`.

## Accessibility

```text
mac.ax.at(x, y, *, app=None, screenshot=None, coordinate_space="screenshot")
mac.ax.get(element_index, attributes="AXValue")
mac.ax.set(element_index, attribute, value)
mac.ax.perform(element_index, action="AXPress")
mac.ax.query(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, limit=20, max_nodes=5000)
mac.ax.query_all(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, limit=20, max_nodes=5000)
mac.ax.wait(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, interval=0.1)
mac.ax.wait_gone(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, interval=0.1)
mac.ax.press(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, interval=0.1)
```

Rarer keywords: `help(mac.ax.query)`. `perform` takes an app's own action by name (`"Close"`).

## Receipted operations

```text
mac.do.press(text=None, *, app=None, element=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.set(value, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.toggle(desired, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.fill(value, *, app, role="text field", text=None, title=None, identifier=None, description=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.key(key, *, app, window_id=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.type(text, *, app, window_id=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.paste(text=None, *, app, html=None, rtf=None, tsv=None, files=(), window_id=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.open_files(files, *, app, timeout=10.0, postcondition=None, once=None, dry_run=False)
mac.do.click(x=None, y=None, *, element=None, app, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot", timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.menu(path, *, app, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.menu_search(text, *, app, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.status_item(path=None, *, app, name=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.window(action, *, app, window_id=None, window_index=0, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.reveal(element, *, app, timeout=5.0, once=None, dry_run=False)
mac.do.run(source, *, language="AppleScript", args=(), timeout=5.0, postcondition=None, once=None, dry_run=False, capture_output=False)
mac.do.expect(condition)
mac.do.expect_any(outcomes, *, timeout=5.0, interval=0.1)
mac.do.steps(steps, *, app)
mac.do.recall(once)
mac.do.history()
mac.timeline()
```

- Conditions: `present(...)`, `gone(...)`, `equals(..., value=, attribute="AXValue")` from `macos_harness.receipts` (preloaded).
- `do.press(element=state[12], app=)` presses a match directly (~50 ms, works in background windows).
- `do.window("close"|"minimize"|"zoom")` waits for the effect; a save sheet fails with its buttons in `exc.details["buttons"]`.
- `do.reveal(match)` scrolls a match into view for `click(element=)`.
- `do.steps([{"op": "press", "text": "7"}, {"op": "key", "key": "return"}], app=)` runs verbs in order, stops at the first failure, returns a `StepsResult`.

## Routes and diagnostics

```text
mac.route.record(name, *, app, entry, goal, timeout=30.0, inputs=None)
mac.route.watch(name, *, app, entry, goal, timeout=300.0)  # -> RouteWatch: .wait(), .stop() -> WatchResult
mac.route.run(name, *, app, timeout=30.0, dry_run=False, inputs=None)
mac.route.list(*, app)
mac.status(app_or_receipt)
mac.inspect(app_or_receipt, *, max_depth=12, max_nodes=300, include_values=False, screenshot=False)
mac.diff(before, after)
mac.diff_windows(before, after)
mac.logs(receipt_or_time_pair, *, app=None, subsystem=None, category=None, level="info", limit=200, timeout=5.0, max_bytes=1048576)
mac.crashes(receipt_or_time_pair, *, app=None, limit=3, max_files=128)
mac.sample(app_or_receipt, *, duration=1, timeout=5.0, max_bytes=1048576)
mac.explain(receipt, *evidence)
```

## App data

```text
mac.data.find_files(query=None, *, name=None, kind=None, content=None, onlyin=None, limit=1000, timeout=10.0)
mac.data.file_info(path)
mac.data.shortcuts()
mac.data.run_shortcut(name, *, input=None, output=None, output_type=None, timeout=60.0)
mac.data.read_defaults(domain, key=None, *, current_host=False)
mac.data.read_plist(path)
mac.data.query_sqlite(path, sql, params=(), *, immutable=False)
mac.data.wait_file(path, *, contains=None, since=None, timeout=10.0)
mac.data.wait_value(read, expected, *, timeout=10.0)
```
