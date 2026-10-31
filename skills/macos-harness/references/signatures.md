# Signatures

Parameters after `*` are keyword-only. When a stdin program passes a wrong
keyword, its `TypeError` ends with the method's real signature; a name
`mac`, `mac.ax`, or `mac.do` lacks lists the nearest calls.

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

`macos-harness run FILE.py [--arg NAME=VALUE ...]` runs a saved program the
way stdin runs one, with each `--arg` as a string in the `args` dict:
`pid = int(args["pid"])`. A malformed `--arg`, a name given twice, or an
unreadable or empty file exits 2 before anything runs.
`macos-harness -c 'print(mac.windows())'` runs one line the same way.
`--json-errors` goes before or after any command. `macos-harness apps
[NAME]` prints one JSON line per running app, only those whose name or
bundle ID contains NAME in any case. `macos-harness state APP` reads 400
nodes unless `--max-nodes` says more, and says on stderr where it stopped.

## Primitives

```text
mac.see(app=None, *, window_index=0, window_id=None, display=None, region=None, path=None, max_width=1280, max_height=1280, crop=None, zoom=1.0, show_pointer=False, after_change=False, timeout=10.0, settle=0.15)
mac.wait_change(app=None, *, window_index=0, window_id=None, region=None, by="pixels", timeout=30.0, settle=0.15)
mac.key(key, *, app=None, window_id=None)
mac.type(text, *, app=None, window_id=None)
mac.click(x=None, y=None, *, element=None, app=None, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot")
mac.scroll(delta_y, delta_x=0, *, app=None, window_id=None, screenshot=None, unit="pixel", x=None, y=None, coordinate_space="screenshot")
mac.drag(from_x, from_y, to_x, to_y, *, app=None, window_id=None, screenshot=None, button="left", coordinate_space="screenshot", duration=0.25, steps=12)
mac.move(x, y, *, app=None, screenshot=None, coordinate_space="screenshot", duration=0.16)
mac.script(source, *, language="AppleScript", timeout=15.0)
mac.activate(app=None, *, timeout=0.5)
mac.launch(app, *, timeout=15.0, window=True)
mac.quit(app, *, timeout=10.0, force=False)
mac.relaunch(app, *, timeout=15.0, window=True)
mac.frontmost()
mac.windows(app=None, *, all_layers=False)
mac.wait_for_window(app=None, *, title=None, timeout=2.0, all_layers=False)
mac.place(app=None, *, window_index=0, window_id=None, x=None, y=None, width=None, height=None, display=None)
mac.menus(app, path=None)
mac.notifications()
mac.list_apps()
mac.show_pointer()
mac.hide_pointer()
mac.handoff(*, reason, app)
```

## AppleScript

`osascript` in a shell borrows that shell's permissions; where the shell's
app lacks Accessibility, a System Events call fails with `osascript is not
allowed assistive access (-25211)`. Run the same source through
`mac.script`, which carries the harness's permission:

```python
mac.activate(4242)  # bring a pid forward; falls back to System Events itself
mac.script('tell application "System Events" to set frontmost of (first process whose unix id is 4242) to true')
mac.script('tell application "System Events" to get name of every window of (first process whose unix id is 4242)')
mac.script('tell application "System Events" to click button "OK" of window 1 of (first process whose unix id is 4242)')
```

Name a process by `unix id`, not by name: one name such as `wine` can
cover several processes.

## Aiming input

`app` takes a name or a pid; `window_id` alone names its owner too. A name
that fits several processes, such as `"wine"`, adds a `warning` naming
the other pids to the result of `see`, `click`, `key`, `type`, and
`activate`; `see`, `key`, and `type` also warn when the app shows several
windows and the call let one be picked. Pass the pid or `window_id`.

A screenshot point outside the picture raises before anything is sent,
and a click read off a picture goes to the window it shows.

`key` and `type` return `pid`, `window_id` (the window that took the keys,
None where accessibility cannot say), `target_is_frontmost`, and a `note`
when the app was in the background, where some apps ignore posted keys.
With `window_id`, that window becomes the app's focused window first,
without bringing the app forward.

`see(app, crop=(x, y, w, h))` captures that part of the window, in window
points; `zoom=2` draws it at twice the screen's pixels per point, still
inside `max_width` and `max_height`. Click its pixels with
`screenshot=` as any picture's.

## Menus, panels, and the screen

`mac.windows()` lists every app's normal windows on screen, front to back;
`all_layers=True` adds menus, popovers, panels, prompts, and the menu bar.
Each entry has `window_id`, `pid`, `owner`, and `layer`.
`mac.wait_for_window(app, title="Settings")` waits for the app to start
and for a window whose title holds `title`; an untitled window off screen,
a helper surface, does not end the wait. `see(window_id=N)`
captures any of them and reports its `layer` and owner `pid`. A window's
picture shows the sheets, popovers, and tips attached to it where they
cover it; what hangs past the window's edge is cut off there, and
`see(window_id=N)` pictures that window whole.
Pointer input picks only a normal window under the point unless
`window_id=N` names the surface, so a click meant for a panel never lands
in the window behind it.

`mac.menus(app, "File")` lists the items of the menu a path names, `"File >
Export"` for a submenu, or with no path the menus in the menu bar: each has
`title`, `enabled`, `checked`, its full `path` for `do.menu`, and a
`shortcut` such as `"shift+cmd+s"` when it has one, for `do.key`.

`mac.notifications()` lists the banners and alerts Notification Center
shows: each has the `app` that sent it, its `title`, `subtitle`, and
`body` (None where it shows none as a text of its own; the `description`
of its `element` joins them all), the `actions` it takes, an alert's
buttons such as `"Snooze"` among them, and its `element`, a match:
`mac.ax.perform(note["element"], "Close")` dismisses it, in this run or a
later one, and brings nothing forward; `"Show"` and `"AXPress"` open the
app that sent it, and a button's name presses that button. Only the
newest banner shows at a time, for about five seconds; a closed one
leaves the list within a second. A read Notification Center cuts short
raises `ax.error` rather than list less: read again.

`see(display=0)` (0 is the main display) or `see(region=(x, y, w, h))` in
screen points captures the screen as it shows, every window composited.
The result has no `app` or `pid`, and its pixels map a later
`coordinate_space="screenshot"` click for whichever app owns the window
there. Its `shows` lists the windows in the picture, the most visible
first, each with `window_id`, `owner`, `pid`, `title`, and the `share` of
the area it covers; another app's window in front means the picture shows
it, not what lies behind.

One process at a time keeps ScreenCaptureKit: macOS treats two live
harness processes on one Python as one client and answers neither. The
first to capture keeps it until it exits; any other renders through
`screencapture`, where a window's sheet that hangs past its edge is cut
off.

`place(app, x=, y=, width=, height=)` moves or resizes window
`window_index`, or the one `window_id=` names, through Accessibility,
without bringing it forward, and returns the `frame` the window reports
afterwards; an app's own limits can make it differ from `requested`. `x`
and `y` count from the top-left of `display` (0 is the main one). With
`display` and no `x` or `y`, a window that does not fit on that display,
such as one at negative coordinates on another, is centered there. A
minimized window, or one its app will not move or resize, raises
`unsupported_op` and nothing changes.

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

A control that ignores `AXPress`, as a SwiftUI row can, takes a click:
`click(element=match)` and `do.click(element=match, app=...)` click the
center of the element's frame once it holds still, in the element's own
process, in place of `x` and `y`. An element of another app, one with no
area on screen, one still moving half a second on, or one whose center
accessibility finds another element over is refused before anything is
posted; the error names what is there.

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

`perform` takes an action an app names itself, which accessibility lists
as `"Name:Close\nTarget:0x0\nSelector:(null)"`, by that name: `"Close"`.

## Receipted operations

```text
mac.do.press(text=None, *, app=None, all_apps=False, apps=None, role=None, title=None, identifier=None, description=None, max_nodes=5000, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.set(value, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.toggle(desired, *, app=None, all_apps=False, apps=None, role=None, text=None, title=None, identifier=None, description=None, attribute="AXValue", timeout=5.0, postcondition=None, dry_run=False)
mac.do.fill(value, *, app, role="text field", text=None, title=None, identifier=None, description=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.key(key, *, app, window_id=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.type(text, *, app, window_id=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.click(x=None, y=None, *, element=None, app, window_id=None, screenshot=None, button="left", clicks=1, coordinate_space="screenshot", timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.menu(path, *, app, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.status_item(path=None, *, app, name=None, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.window(action, *, app, window_id=None, window_index=0, timeout=5.0, postcondition=None, once=None, dry_run=False)
mac.do.reveal(element, *, app, timeout=5.0, once=None, dry_run=False)
mac.do.run(source, *, language="AppleScript", args=(), timeout=5.0, postcondition=None, once=None, dry_run=False, capture_output=False)
mac.do.expect(condition)
mac.do.expect_any(outcomes, *, timeout=5.0, interval=0.1)
mac.do.recall(once)
mac.do.history()
mac.timeline()
```

Conditions come from `macos_harness.receipts`: `present(...)`, `gone(...)`,
and `equals(..., value=, attribute="AXValue")`, all three preloaded in a
program. They take the same selectors as `mac.ax.wait` plus their
own `timeout` and `interval`. A receipt that acted with nothing confirming
its effect prints which postcondition would.

`do.key` and `do.type` send nothing when keyboard focus has moved to
another element since they were prepared (`focus.changed`, reason
`focus_moved_before_dispatch`, `acted="no"`).

`do.window("close" | "minimize" | "zoom", app=)` presses that title bar
button of window `window_index` or `window_id`, then waits until the window
is gone, off screen, or resized. A close a sheet stops fails with `timeout`:
"The window stayed: a save prompt is open (Delete, Cancel, Save)", with the
buttons in `exc.details["buttons"]` for the `do.press` that answers it; any
other window still there leaves the receipt unverified.
`do.reveal(match, app=)` scrolls a search match into view and puts its
`frame` in `observed["input"]`, ready for `click(element=match)`.

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

`inspect` walks at most 5000 nodes and 25 levels; a larger ask walks the
cap, and the result's `capped` says what was asked and used.

## Scrolling

`mac.scroll` moves pixels unless `unit="line"`; a negative `delta_y` scrolls
down. With no `x`/`y` it scrolls the center of the window in the app's last
screenshot, else of the app's frontmost window:

```python
mac.see("Finder")
mac.scroll(-300, app="Finder")
```
