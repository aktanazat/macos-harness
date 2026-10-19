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
captures one window and prints the same result as `mac.see`.

## Primitives

```text
mac.see(app=None, *, window_index=0, path=None, max_width=1280, max_height=1280, show_pointer=False)
mac.key(key, *, app=None)
mac.type(text, *, app=None)
mac.click(x, y, *, app=None, button="left", clicks=1, coordinate_space="screenshot")
mac.scroll(delta_y, delta_x=0, *, app=None, unit="pixel", x=None, y=None, coordinate_space="screenshot")
mac.drag(from_x, from_y, to_x, to_y, *, app=None, button="left", coordinate_space="screenshot", duration=0.25, steps=12)
mac.move(x, y, *, app=None, coordinate_space="screenshot", duration=0.16)
mac.script(source, *, language="AppleScript")
mac.activate(app=None, *, timeout=0.5)
mac.launch(app, *, timeout=15.0, window=True)
mac.quit(app, *, timeout=10.0, force=False)
mac.relaunch(app, *, timeout=15.0, window=True)
mac.frontmost()
mac.windows(app=None)
mac.wait_for_window(app=None, *, timeout=2.0)
mac.list_apps()
mac.show_pointer()
mac.hide_pointer()
mac.handoff(*, reason, app)
```

## Accessibility

```text
mac.ax.at(x, y, *, app=None, coordinate_space="screenshot")
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
mac.do.click(x, y, *, app, button="left", clicks=1, coordinate_space="screenshot", timeout=5.0, postcondition=None, once=None, dry_run=False)
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
