# Signatures

Parameters after `*` are keyword-only. When a stdin program passes a wrong
keyword, its `TypeError` ends with the method's real signature.

```python
frame = mac.see("Spotify")
mac.key("cmd+k", app="Spotify")
mac.type("Alessia Cara", app="Spotify")
mac.click(640, 420, app="Spotify")

item = mac.ax.at(640, 420, app="Spotify")
mac.ax.perform(item["element_index"], "AXPress")

mac.script('tell application "Spotify" to play')
```

Without a program, `macos-harness see APP_OR_PID --path out.png --window-index 0`
captures one window and prints the same result as `mac.see`;
`macos-harness see --window-id N` captures any window.

## Primitives

```text
mac.see(app=None, *, window_index=0, window_id=None, display=None, region=None, path=None, max_width=1280, max_height=1280, show_pointer=False, after_change=False, timeout=30.0)
mac.wait_change(app=None, *, window_index=0, window_id=None, region=None, by="pixels", timeout=30.0, settle=0.5)
mac.key(key, *, app=None)
mac.type(text, *, app=None)
mac.click(x, y, *, app=None, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot")
mac.scroll(delta_y, delta_x=0, *, app=None, window_id=None, screenshot=None, unit="pixel", x=None, y=None, coordinate_space="screenshot")
mac.drag(from_x, from_y, to_x, to_y, *, app=None, window_id=None, screenshot=None, button="left", coordinate_space="screenshot", duration=0.25, steps=12)
mac.move(x, y, *, app=None, screenshot=None, coordinate_space="screenshot", duration=0.16)
mac.script(source, *, language="AppleScript")
mac.activate(app=None, *, timeout=0.5)
mac.launch(app, *, timeout=15.0, window=True)
mac.quit(app, *, timeout=10.0, force=False)
mac.relaunch(app, *, timeout=15.0, window=True)
mac.frontmost()
mac.windows(app=None, *, all_layers=False)
mac.wait_for_window(app=None, *, timeout=2.0)
mac.list_apps()
mac.show_pointer()
mac.hide_pointer()
mac.handoff(*, reason, app)
```

## Menus, panels, and the screen

`mac.windows()` lists every app's normal windows on screen, front to back;
`all_layers=True` adds menus, popovers, panels, prompts, and the menu bar.
Each entry has `window_id`, `pid`, `owner`, and `layer`. `see(window_id=N)`
captures any of them and reports its `layer` and owner `pid`. Pointer input
picks only a normal window under the point unless `window_id=N` names the
surface, so a click meant for a panel never lands in the window behind it.

`see(display=0)` (0 is the main display) or `see(region=(x, y, w, h))` in
screen points captures the screen as it shows, every window composited.
The result has no `app` or `pid`, and its pixels map a later
`coordinate_space="screenshot"` click for whichever app owns the window
there.

## Across runs

`see` keeps a record beside each PNG (`<png>.json`). In a later run,
`screenshot="<png>"` on `click`, `drag`, `scroll`, `move`, `ax.at`, or
`do.click` maps that PNG's pixels and targets its app, with no new `see`.
If the window has moved or closed since, the call raises `window.changed`
instead.

An element index lasts until the next search in the same run. A match
lasts longer: pass the match itself (a `query`, `query_all`, or `wait`
result) as `element_index`. It is found again in its app by role,
labels, and frame. If it finds no element, or more than one, the call
raises `element.unknown`.

```python
match = {"pid": 4512, "role": "AXButton", "title": "Save"}  # printed by a query
mac.ax.perform(match, "AXPress")
```

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

The AX calls also take rarer keywords: `attributes`, `include_actions`,
`search_key`, `visible_only`, `direction`, `immediate_descendants_only`, and
`element_index` on `query`. `help(mac.ax.query)` lists them.

## Receipted operations

```text
mac.do.press(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.set(value, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.toggle(desired, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.fill(value, *, app, role="text field", text=None, title=None, identifier=None, description=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.key(key, *, app, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.type(text, *, app, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.click(x, y, *, app, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot", timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.run(source, *, language="AppleScript", args=(), timeout=5.0, postcondition=None, once=None, dry_run=False, capture_output=False)
mac.do.expect(condition)
mac.do.expect_any(outcomes, *, timeout=5.0, interval=0.1)
mac.do.recall(once)
mac.do.history()
mac.timeline()
```

Conditions come from `macos_harness.receipts`: `present(...)`, `gone(...)`,
and `equals(..., value=, attribute="AXValue")`. They take the same selectors
as `mac.ax.wait` plus their own `timeout` and `interval`.

## Routes and diagnostics

```text
mac.route.record(name, *, app, entry, goal, timeout=30.0, inputs=None)
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

## Scrolling

`mac.scroll` moves pixels unless `unit="line"`; a negative `delta_y` scrolls
down. With no `x`/`y` it scrolls the center of the window in the app's last
screenshot:

```python
mac.see("Finder")
mac.scroll(-300, app="Finder")
```
