# Changelog

All notable changes to macOS Harness are documented here. This project
follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `mac.notifications()` lists the banners and alerts on screen, each with
  the `app` that sent it, its `title`, `subtitle`, `body`, `actions`, and
  `element`. `mac.ax.perform(note["element"], "Close")` dismisses one and
  brings nothing forward; an alert's own buttons, such as `"Snooze"`, are
  among its `actions` and press by name the same way. `ax.perform` takes
  any action an app names itself, listed as
  `"Name:Close\nTarget:0x0\nSelector:(null)"`, by that name.
- `see(crop=(x, y, w, h), zoom=)` captures part of a window, in window
  points, and draws it at `zoom` times the screen's pixels per point, so
  small text in a large window reads; clicks map through the cut picture.
- `mac.key` and `mac.type` take `window_id=`, make that window the app's
  focused one without bringing the app forward, and return the `pid`,
  the `window_id` that took the keys, `target_is_frontmost`, and a `note`
  when the app was in the background. They returned `None` before.
- `mac.do.key` and `mac.do.type` take `window_id=` too. The window must
  be the app's and on screen, checked before anything is reserved, and
  it becomes the app's focused window in the same turn that sends the
  keys, so a window another run focused since cannot take them.
- A name that fits several processes, such as `wine`, adds a `warning`
  naming the other pids to `see`, `click`, `key`, `type`, and `activate`;
  `see`, `key`, and `type` also warn when one of several windows was
  picked for the caller.
- `mac.activate` asks once more through Accessibility when macOS declines
  the ordinary request, as it does for Wine apps, and says which request
  worked under `via` (`appkit`, `accessibility`, or `None`).
- `mac.click` reports `target_is_frontmost`; a click on an app in the
  background adds a `note` that a first click there may be dropped, with
  the `wait_change` and `activate` calls that check and cure it.
- `see(region=...)` and `see(display=...)` name the windows they show
  under `shows`, the most visible first, each with its visible `share`,
  so a picture covered by another app's window says so.
- Two live harness processes capture side by side: the first to capture
  keeps ScreenCaptureKit and others render through `screencapture`.
  Before, once both had captured, every capture in either timed out.
- `mac.ax` takes any AX role, by its AX name or in words (`pop up button`,
  `AXRow`); a role Apple's search has no key for is filtered from an
  any-role search, and `role="window"` finds the app's windows. A
  misspelled role names the nearest one.
- A wait that times out names its query and how many searches ran, and in
  one app a match it found off screen or of another role, and the on-screen
  labels nearest its text.
- `mac.wait_for_window(app, title=, all_layers=)` waits for the app to start,
  for a window whose title holds `title`, or for a prompt off the normal
  layer.
- A menu or menu-item search marks a match inside a closed menu with
  `in_closed_menu`. Twins found again from a match are named by window id
  and title, and a `window_id` added to the match picks one.
- A call that takes one element takes a one-match search result.
- `get_app_state` coverage names the read that cut the tree short under
  `read_failure`.
- `mac.menus(app, "File")` lists a menu's items with their state, full
  path and keyboard shortcut, without opening it; a `do.menu` path that
  names a menu, not an item, points at it.
- `mac.do.window("close" | "minimize" | "zoom", app=)` presses a window's
  title bar button and waits until the window is gone, off screen or
  resized; a close a save prompt stops fails with `timeout`, naming the
  prompt's buttons in the message and `details["buttons"]`.
  `mac.do.reveal(match, app=)` scrolls a match into view, a native
  backend's match included, and returns its frame. `mac.place` takes
  `window_id=`; it and `do.window` tell windows sharing one frame apart by
  window number and refuse twins they cannot.
- `macos-harness -c CODE` runs one line with the stdin namespace, served by
  the shell's spare like stdin; `--json-errors` goes after any command;
  `apps [NAME]` prints one JSON line per app and filters by name or bundle
  ID; `state` reads 400 nodes by default and says where it stopped.
  Programs get `present`, `gone` and `equals` preloaded.
- A menu Quit item or `cmd+q` that makes the app exit is a verified
  receipt, not `app.exited`. A printed receipt whose effect nothing
  confirmed names the postcondition that would, and a click on a
  background app names `mac.activate(app)` or `mac.do.press`.
- `mac.script` stops a script after 15 seconds (`timeout=`) and names the
  dialog or permission prompt it is likely waiting on. `see(after_change=True)`
  waits 10 seconds by default, not 30, and adds a `note` when nothing
  changed. `scroll` with no point aims at the app's frontmost window.
- A menu bar app with no window, a `.app` name that is not running, and a
  `fill` that cannot read its text back now say what to call or what was
  read. `frontmost`, `activate` and `launch` flag the harness's own process
  with `harness_owned`.
- `MACOS_HARNESS_RECEIPT_LOG=1` appends each finished `mac.do` receipt as one
  JSON line to `~/Library/Logs/macos-harness/receipts.jsonl`, without values
  or typed text, rotating at 5 MiB. It is off by default.
- `macos-harness skill <name>` prints one skill reference, and
  `scripts/install-skill.sh` copies the card and every reference into an
  agent skills directory.
- `mac.launch(app)` starts an app without bringing it forward and returns
  once its first window is up, instead of `open -a` and a fixed sleep. It
  takes a name, bundle ID, or `.app` path and reuses a running copy.
  `mac.quit(app)` asks an app to quit and returns once it has exited; it
  never forces unless `force=True`, and refuses a partial name match.
  `mac.relaunch(app)` restarts the same bundle, and `mac.frontmost()` names
  the app in front.
- `macos-harness see` takes `--path` and `--window-index`, and `see` and
  `state` read an all-digit app as a pid. The skill card shows reading the
  PNG in the same omp eval cell that captured it, which saves a turn per
  look.
- `mac.wait_change(app)` returns once a window has changed since just before
  the program's latest input to it, or since its last `see` or wait when no
  input came after that, and held still for 0.15 seconds, in place of a
  guessed sleep, with how many looks it took. A blinking caret is no
  change; `region=` watches a smaller one and `by="ax"` compares the app's
  text. Nothing changing raises a timeout whose details say `changed` is
  false. `mac.see(app, after_change=True)` waits the same way, `settle=`
  included, and then captures, reporting `changed` instead of raising.
- `mac.see(window_id=N)` captures any window, panel, menu, prompt, or the
  menu bar, whoever owns it, and reports its `layer` and owner `pid`;
  `macos-harness see --window-id N` does the same. `mac.see(display=0)` and
  `mac.see(region=(x, y, w, h))` capture the screen as it shows, in screen
  points, and their pixels map later clicks like a window's.
  `mac.windows()` with no app lists every app's windows on screen, and
  `all_layers=True` adds the menus and panels, each with its owner and
  layer. `click`, `drag`, `scroll`, `mac.do.click`, and `wait_change` take
  `window_id=` too; without it, pointer input still lands only in a normal
  window. An app showing only a panel now names the panel's window id
  instead of reporting no window.
- A later run can aim through an earlier run's screenshot: `see` keeps a
  record beside each PNG, and `click`, `drag`, `scroll`, `move`, `ax.at`,
  and `mac.do.click` take `screenshot="<png>"`, which maps that image's
  pixels and targets its app without a new `see`. A window that has moved
  or closed since still refuses. The error for screenshot pixels with no
  `see` in the run now names `screenshot=`.
- `mac.ax.get`, `set`, `perform`, and the other element calls take the
  match a search returned in place of its index. An index lasts until the
  next search in one run; a match, which now carries its app's `pid`, is
  found again by role, labels, and frame in any later run, and raises
  `element.unknown` when that finds no element or more than one.
- `macos-harness run FILE.py --arg NAME=VALUE` runs a saved program with
  each `--arg` as a string in its `args` dict, so a rerun with another pid
  or label no longer means editing the file. Tracebacks name the file and
  line, and `--json-errors` works before `run` or after the file. A
  malformed `--arg` exits 2 before anything runs.
- `mac.place(app, x=, y=, width=, height=)` moves or resizes a window
  through Accessibility without bringing it forward, in place of System
  Events, and returns the frame the window reports afterwards. `x` and `y`
  count from the top-left of `display=` (0 is the main one); with `display`
  and no `x` or `y`, a window that does not fit on that display is centered
  there. A minimized window, or one its app will not move or resize, raises
  `unsupported_op` before anything changes.
- `mac.click(element=match)` and `mac.do.click(element=match, app=...)`
  click the center of an element's frame once it holds still, in the
  element's own process, for a control that ignores `AXPress`, as SwiftUI
  rows can. The element may be a match from any run or an index from this
  one. An element of another app, one with no area on screen, one still
  moving half a second on, or one whose center accessibility finds another
  element over in the window the click is routed to, is refused before
  anything is posted. The refreshed second answer decides, even when the
  first matched the target. A hit in another of the app's windows, such as
  a floating panel, does not block input routed here. The receipt names
  the element by its role and labels, never its value.
- `mac.do.key` and `mac.do.type` check the shared deadline again after
  their final field and focus reads. If those reads use up the budget,
  nothing is sent and the `once` token remains available for another try.
- `mac.do.menu("View > Reload Page", app=...)` chooses a menu command by its
  path, each step found only under the one before, so an item two menus
  share, such as Close under File and under Window, is never ambiguous.
  `mac.do.status_item(path, app=...)` does the same in the menu of the app's
  menu bar icon, or with no path opens it; `name=` picks the icon when the
  app has several, since many have no title or description. Both press
  through Accessibility without bringing the app forward, match names
  ignoring case and with "..." for "…", and refuse a disabled item, or a
  path that leads to no item or to more than one, before anything is
  pressed.
- `macos-harness spare`, started once as a shell's coprocess, keeps one
  process with the harness loaded for that shell's next step: a program on
  stdin, or `run` with stdin from `/dev/null`. Each spare runs one step and
  exits while the next one loads. On Calculator an empty step took 58ms
  instead of 133ms, a window picture 200ms instead of 272ms, and a key
  226ms instead of 297ms (15 runs each, alternating). A step runs in its
  own process as before when its output is a terminal, it uses other
  Python flags or load-time settings, another step from the shell is
  running, no spare is ready, or the installed code changed since the
  spare loaded. The spare exits with its shell, even after `kill -9`, once
  a step it is running ends, and with the step it serves; only commands the
  shell starts hold its pipes. `macos-harness skill spare` prints the setup
  lines.

### Fixed

- Two harness runs sending input to one app take turns, which keeps the
  keys of runs that each name their window apart. Keys go to whichever
  window the app has focused, so two runs typing into two of its windows
  at once put both texts, mixed, into one window and nothing into the
  other. A click, drag, scroll, key, or typed text now waits
  while another run sends that app input, up to 60s for the raw verbs or
  the `mac.do` call's `timeout`, then raises `timeout` with `reason:
  "app_input_busy"` having sent nothing; a lock file that will not open
  raises `ax.error` with `reason: "app_input_lock_unavailable"`, also
  having sent nothing. A `mac.do` call keeps its turn only while it
  sends, not while it waits for a postcondition, a quit, a window to
  close, or `fill`'s read back. Input to different apps still runs at
  once. Typing into an app in the background stops with `focus.changed`
  once the app's keys go to another window, instead of sending the rest
  of the text there.
- `wait_change(by="ax")` skips a read of the app's accessibility tree
  that was cut short instead of comparing it: unreadable reads had
  reported `changed` and `settled` with nothing changed. The result, or
  the timeout's message and details, counts them in `failed_reads` with
  the last `read_failure`.
- `see(after_change=True)` reports `settled`, and a change still going
  at `timeout` opens with the note "changed but did not settle in 10s"
  instead of reading as a finished change.
- `see(display=)` and `see(region=)` leave the virtual pointer out in a
  second harness process too, which captures through `screencapture`:
  the pointer's helper closes for the capture and the pointer shows
  again after. Before, it appeared in the picture.
- Text searches with values omitted from their results still match native
  static-text labels stored only in `AXValue`, in both backends. Before,
  a wait could time out and name the exact label as its nearest match.
  Editable values stay unread and app/visibility filters are unchanged.
- A screenshot point outside the picture raises before any input is
  sent, and a click, drag, or scroll read off a window's picture goes to
  that window rather than whichever of the app's windows lies on top, so
  a game window off the normal layer takes a drag without `window_id`.
- The spare shell sizes output buffers as Python 3.14 does, which had
  failed the 3.14 test job since the spare was added.
- One Accessibility value that reads as infinity or NaN no longer fails
  `get_app_state`; only that field is dropped and named under `non_finite`.
- An untitled off-screen helper window no longer ends `launch` and
  `wait_for_window` before the app's real window is up.

### Changed

- `mac.do.key` and `mac.do.type` note the element that has keyboard focus
  when they are prepared, in the window they name if one is focused, and
  send nothing when focus is on another element by the time they would:
  the receipt fails with `focus.changed`, `reason:
  "focus_moved_before_dispatch"`, and `acted="no"`, and its `once` token
  stays free. Background typing stopped by a window change before its
  first character carries that reason too, and its receipt says the same.
- `mac.do.type` without a postcondition reads a plain text field back, as
  `do.fill` does, until it shows the typed text where the selection was;
  only then is the receipt verified, with `observed["input"]["landed"]`.
  Text the field shows differently, as a smart quote or a completion
  rewrites it, leaves the receipt unverified once it holds still for 0.3
  s, other text by the timeout; text typed over a selection of itself is
  never verified; nothing is typed twice.
- A stdin step no longer loads the REPL, the nearest-call hint, the
  cross-app sweep's thread pool, the bundle-version reader, or
  `mac.route` before it needs them; `mac.route` is built on first use.
  An empty step starts in about 124ms instead of 130ms (two sets of 60
  runs each). The rest is the Accessibility, Core Graphics, and
  ScreenCaptureKit bindings every step loads, about 110ms.
- A `mac.do.key`, `click`, `type`, or `status_item` with no postcondition
  watches focus until a reading starts 100ms or more after dispatch, the
  readings' own time included, instead of waiting ten 10ms polls on top of
  eleven readings. On Calculator the watch took 102ms instead of 129ms and
  a receipted key about 180ms instead of 211ms (8 runs each). The latest
  effect the watch was sized for showed at 62ms; an app whose readings take
  50ms or more still gets a reading that starts after it.
- A window picture reuses the window list ScreenCaptureKit gave the same
  session's last picture of that window while the window server shows the
  window, its app's other windows, and the displays where they were. That
  check reads in about 0.3ms; a new list takes 16ms. A key and
  `see(after_change=True)` on Calculator list windows once instead of five
  times and take 474ms instead of 540ms (8 runs each, one fresh `MacOS`
  per run). A window that moved, resized, or gained a tip between two
  pictures is listed again and pictured where it now is.
- The skill is now a short card (about 3 KB, down from 31 KB) plus
  references for signatures, receipts, accessibility search, invariants,
  routes, diagnostics, handoff, credentials, and the native backend. The card
  shows waiting for the next control after a press, and asks for
  `macos-harness doctor` only after a permission or connection error.
- CLI commands and the REPL close their original `MacOS` instance before
  returning, including on errors and interruptions. Retaining or replacing
  the preloaded `mac` variable no longer keeps its private helpers alive.
- `MacOS.close()` and instance finalization also reap the disposable pointer
  helper, including on the default Python backend. Pointer helpers are no
  longer retained until interpreter exit. Explicit pointer use can recreate
  one lazily; cleanup never closes app windows, documents, or human handoffs.
- With the default Python backend, an all-apps search, query or press now
  reaches an app that started after the harness first listed apps, such as
  the process behind a permission dialog. Each listing reads the running
  apps afresh; before, the list stayed as it was at the first read.
- An app given by pid is found even when AppKit's direct pid lookup misses
  it, which happened to 2 of 5 Calculators the same process had just
  launched; a miss now falls back to the running-apps list. Receipts no
  longer report such an app as exited, and `mac.quit` reads the exit from
  the kernel's process table.
- An app given by pid keeps that pid when AppKit hands back a record whose
  pid reads -1, as Device Hub's did every time; every later call used to
  fail on "No running application matches -1". A name whose app has no
  process yet now says it is still launching, and `mac.launch` waits for
  the pid. Of processes with exactly the asked name, the only one with an
  on-screen window is picked instead of raising `app.ambiguous`, unless a
  windowless one is frontmost; two windowed copies, or names that match
  only helpers, still raise.
- The frontmost app is read afresh on both backends. `mac.activate` now sees
  its request take, and the focus guard after a press catches a target that
  brings itself to the front; before, a process kept seeing whichever app was
  in front at its first look. The native agent's app list and app lookup
  likewise see apps launched after its first read.
- A broad all-apps search, query, wait or press first asks every app at once
  whether it can answer Accessibility, then skips the ones that cannot (a
  stopped app, a web content helper) instead of waiting out each one's
  half-second timeout in turn: about 1.8 s instead of 2.7 to 3.4 s per sweep
  on the Python backend, and 1.6 s instead of 3.6 to 4.4 s on the native
  backend, on a Mac running about 90 apps. The native agent asks through a
  new op, `ax_unanswered_apps`; an agent binary that predates it refuses it
  as `unsupported_op`. Skipped apps leave the search incomplete, so a wait
  never confirms a match or its absence across them.
- A wait that times out on an incomplete search names the apps it searched
  only in part and counts the apps that did not answer, in the message and
  in `details["partial"]` and `details["unanswered"]`.
- An all-apps sweep for what is on screen now reaches the whole of big apps
  like Safari and Slack and reports itself complete. A search that walks an
  app's tree for what is on screen leaves out the items of closed menus,
  which were most of Safari's tree; a search for menus or menu items still
  reads them, so `role="menu item"` reaches a command in a closed menu. The
  walk now goes 50 levels deep instead of 25 (Slack's tree reaches 28), and
  `max_nodes` defaults to 5000 instead of 500 for AX queries, waits and
  presses.
- A broad sweep for what is on screen no longer counts an app it cannot
  search against completeness unless that app has a window on screen; a
  stopped background process or web content helper shows nothing to find.
  An all-apps wait for something to disappear can now confirm it.
- A value an app reports it cannot produce (AXError -25200) no longer leaves
  a search or an exact-title match incomplete; a read that failed because the
  element vanished or the app did not answer still does.
- AXError messages say what the code means. An action that fails with -25204
  warns that it may have taken effect.
- `mac.do.press` takes the search text as its first positional argument,
  like `mac.ax.press`.
- The errors for an unknown role, an element index from an earlier search
  or run, and screenshot coordinates with no screenshot in the run say what
  to do instead.
- A stdin program that calls a name `mac`, `mac.ax`, or `mac.do` lacks,
  such as `mac.ax.get_attributes`, now gets the nearest calls across all
  three as they are called. A keyword that abbreviates a parameter, such as
  `dy` for `delta_y`, is pointed at it; Python's own hint said `y`.
  `mac.inspect` past 5000 nodes or 25 levels walks the cap instead of
  failing and reports `capped`, and a `window_index` past an app's windows
  says the index starts at 0 and which indexes the app has.
- `print(receipt)` shows one line, such as
  `press done acted=yes changed=yes verified=yes 0.31s`, with `error=<code>`
  on a failure, instead of the whole record (a median of 1,096 characters).
  `repr(receipt)` and `receipt.to_json()` keep the record.
- An AX wait, and the search inside a Python-backend press, keeps polling
  while the app does not answer in time (AXError -25204), as Secretive and
  Activity Monitor do for a second or more after `open -a`; before, the
  first such poll failed the wait. A wait that times out this way says the
  app did not answer.
- `role=` accepts the AX name a match reports, such as `"AXButton"`, for
  every role it accepts a friendly name for.
- A press, `mac.do.set`, `toggle` or `fill` on the Python backend whose
  search finds several matches takes the only enabled one among the exact
  labels, or among all matches when none is exact; the exact label still
  decides first. A tie that remains lists every match, not the first four,
  with its frame and whether it is enabled.
- An action whose element is gone right afterward counts as done even when
  the app answered it with AXError -25200, -25204 or -25205: Secretive's
  sheet Delete button deleted the key and closed the sheet but answered
  -25205.
- `mac.see` on an app that runs with no window says so and names
  `mac.script('tell application id "…" to reopen')`, which opens the window
  the way a Dock click does.

- A substring AX search that finds several matches now takes the one whose
  whole title or description equals the search text, so
  `mac.ax.press("Allow", all_apps=True)` presses "Allow" beside "Don't Allow".
  Two exact labels, none, a search that filled its limit, or an exact
  selector's incomplete search still fail closed. Raw waits and presses,
  `mac.do`, and the native agent apply the same rule.
- `mac.ax.query`, `query_all`, `wait`, `wait_gone`, and `press`, and
  `mac.do.press`, `set`, and `toggle` take `app=`, `all_apps=True`, or `apps=`
  through one check with one error. `query` gains `all_apps` and `apps`;
  `query_all` gains `app` and `all_apps`. A blank selector is refused like an
  empty one.
- A stdin program that calls a harness method with a wrong keyword ends its
  `TypeError` with the method's real signature. The skill lists each
  primitive's exact signature.
- Exact app-name queries ignore same-named extensions nested inside the
  matching app bundle. Explicit PID, bundle ID, and path selectors still
  reach extensions; multiple matching apps remain ambiguous.
- `mac.diff(before, after)` compares consecutive inspections using stable
  control references. Partial observations separate uncertain appearances
  and disappearances from proven additions and removals.
- `mac.do.expect_any` waits for named outcomes in one read-only call and
  reports every match in the first successful pass. One cooperative timeout
  covers the checks; failures retain each outcome's observation state.
- Saved routes accept named form inputs through `rec.fill`. Definitions keep
  parameter names, not entered text. Replay validates all inputs before action
  and does not mistake the previous input's goal for completion of a new run.
- `mac.do.fill` replaces a plain text field through keyboard input and checks
  the text readback. It handles UTF-16 selection on both backends, refuses
  secure or disabled fields, and stops if focus or text changes before typing.
  Use an app-level postcondition to check that the app processed the input.
- Help and version commands defer loading macOS frameworks. `--json-errors`
  emits structured harness errors and operation receipts on stderr when requested.
- A failed AX attribute batch stays incomplete instead of repeating the
  failed read for every attribute. Bounded searches collect action names
  only for matches. Unsupported batch APIs retain their single-read fallback.
- `mac.explain` retains partial or unavailable inspection coverage and shows
  nearby controls for the `any` role as well as specific roles.
- A failed `mac.do.expect` receipt now reports what the check established.
  `observed` carries `state` — the new public `Observation` enum, `UNMET` or
  `UNOBSERVABLE` — and a `reason` naming the case: a completed search that
  found nothing, a value that differed, a truncated tree walk, an ambiguous
  match, a refused reading, an exhausted deadline, or a disappearance no
  second poll confirmed. Route replay reads that state instead of re-deriving
  it from error details, so a goal read that established nothing no longer
  authorizes replaying presses; previously an unconfirmed disappearance was
  treated as a definite unmet goal.
- App-bound receipts retain process lifetime evidence without replacing the
  original action error or input classification. Otherwise-successful input
  fails with `app.exited` after an observed exit when the effect was unverified.
  A verified expected disappearance can succeed. Replayed receipts retain their
  original process evidence.
- `mac.status` reports process and on-disk build metadata, including command-line
  PIDs. `mac.inspect` composes bounded snapshots, focus, blocking-dialog evidence,
  and current same-role controls after a failed search. Values and screenshots
  are opt-in; secure fields and failed identity reads exclude content attributes.
  `mac.diff_windows` compares supplied observations without reading the desktop.
- `mac.logs`, `mac.crashes`, and `mac.sample` collect bounded diagnostic evidence
  on request. Collection failures and partial results remain explicit.
  `mac.explain` correlates supplied evidence without changing the receipt,
  collecting more data, or retrying input.
- `mac.route` records explicit press/set/toggle/key calls and saves a validated
  navigation definition with entry and terminal conditions. Replay stays on one
  app process, shares one deadline, and stops at the first divergence while
  retaining the failing receipt. Dry runs observe only current conditions and
  targets. Failed recordings preserve the previous file; no route retries,
  resume, rollback, or implicit activation are added.
- Presses keep their original deadline through agent setup, searches, retry
  delays, and focus readings on both backends. A known pre-dispatch timeout
  reports `acted=no` and releases its once-token reservation. Other timeouts
  retain the token because the action may have happened. Raw zero-timeout
  presses still make one attempt.
- Receipts include original ISO-8601 UTC start and completion timestamps.
  `mac.timeline()` returns the latest 256 non-replayed receipts, including
  failures and calls without once tokens, without collecting new observations.
  History eviction leaves the at-most-once token ledger intact.
- AX searches accept exact `title`, `identifier`, and `description` selectors
  alongside substring `text`. Results expose `complete` and `visited`; strict
  waits and presses refuse an unproved unique match, and disappearance requires
  two complete empty searches. Failed reads and traversal limits leave a search
  incomplete. Unsupported fallback predicates now raise instead of broadening
  the search. The native wire protocol is version 2.
- `mac.do.expect` checks an explicitly scoped `present`, `gone`, or `equals`
  condition through the existing verifier and returns a read-only receipt. It
  does not enable accessibility features, sample focus, or reserve a once token.
- `mac.do.set` and `mac.do.toggle` wait for their readback. Both used to
  read the attribute once after the mutation and fail with `acted=yes`
  when that first reading still showed the old state, which is what an
  app that applies a set or a press on its next run-loop turn looks like.
  They now re-read every `interval` until the requested state shows or
  the shared deadline runs out, with the same summary-only receipt, and
  the mutation is never repeated: a stable mismatch still fails with the
  last reading, and a refused read is still an error, not a retry.
- `mac.see()` and `capture_screenshot()` render one window through
  ScreenCaptureKit at the requested output size instead of running
  `screencapture` and shrinking a full Retina PNG with Pillow. A 1280px
  `see` of a 880x448pt window takes 67ms warm (138ms cold) in an 85MB
  process; the old path took 480ms in 186MB. A window behind others or
  on another Space still captures, and the result carries `on_screen`
  (from ScreenCaptureKit) and `captured_at` so a stale render is
  visible. `raw_width`/`raw_height` are gone; `width`/`height` are the
  image size, `bounds` the window's points. Pillow is no longer a
  dependency; `pyobjc-framework-ScreenCaptureKit` is.
- The live pointer overlay draws the system arrow cursor at the user's
  Accessibility pointer size instead of a fixed hand-drawn 44pt arrow,
  sits at status-window level on every Space, honors Reduce Motion, and
  hides itself after three idle seconds. `hide_pointer()` is sticky: no
  helper is spawned again until `show_pointer()`.
- `see(show_pointer=...)` defaults to `False`; the CLI flag is `see
  --pointer`. Sessions passed `show_pointer=False` 78 times for every
  `True`, and the drawn arrow was the "huge pointer" in screenshots.
- Window and screenshot coordinates are checked against the window
  before input: a click, drag, scroll, or move after the window closed,
  moved, resized, or left the screen raises `MacOSError` with the new
  code `window.changed` (`details.reason` is `closed`, `moved`, or
  `off_screen`) instead of posting to the wrong place. The wire
  vocabulary is ten codes.
- Local event suppression is disabled on the harness event source, so a
  posted click no longer pauses the user's own mouse for the system's
  default suppression interval.
- `mac.see()` on an app launched less than five seconds ago waits up to
  two seconds for its first window instead of failing with "No
  capturable windows". `open -a` returns about 50ms after launch, but
  TextEdit's first window appears 335ms later and Notes' 792ms later, so
  a capture straight after a launch used to fail every time.
- An ambiguous app query ranks its matches -- frontmost first, then most
  on-screen windows, then newest -- and reports `frontmost`,
  `on_screen_windows`, and `launched_seconds_ago` for each, so the
  caller can pick the pid. Nothing is picked on its behalf.
- `mac.ax.get(handle, "AXSelectedTextRange")` returns
  `{"location", "length"}`; it raised `AttributeError` before.
- Posted clicks, drags, and scrolls now land. `CGEventPostToPid` skips
  the window server's hit test, so AppKit found no window under the
  event and dropped it: a raw `mac.click` never moved TextEdit's caret,
  and a raw `mac.scroll` never scrolled, active app or not. Each mouse
  and scroll event now carries the app's frontmost on-screen window
  under the point and the point in that window's coordinates, the two
  fields AppKit hit-tests, and `click` returns the `window_id` it
  routed to. Only a window `windows()` would list counts -- on the
  normal layer and at least 40pt on a side -- so a same-app tooltip or
  helper surface over the point does not take the event. A point over
  none of the app's windows raises `bad_request` before anything is
  posted. A drag stays on the window that took its mouse-down. `scroll`
  with no `x`/`y` scrolls at the center of the window in the last
  screenshot of that app instead of posting an event with no target;
  without such a screenshot it raises `bad_request`. The routing uses
  the private `CGEventSetWindowLocation` symbol; a macOS build without
  it raises `unsupported_op` instead of posting an event that cannot
  land.
- `mac.type` into the frontmost app sends text in runs of up to 20
  UTF-16 units per key event with no pause between events: 128
  characters land in about 30ms instead of 1.6s, exact in TextEdit,
  Notes, Safari, and Chrome (280 of 280 trials). An inactive app still
  gets one event pair per character with a 10ms pause, because a
  background Chrome drops every event carrying more than one unit.
  Newline, carriage return, and tab always travel alone as Return and
  Tab key events.
- `clicks` is a count from 1 to 3 (macOS has no gesture past a triple
  click); `mac.click` and `mac.do.click` reject anything else with
  `bad_request` before posting. `drag` and `scroll` accept a pid for
  `app`, as `click` and `type` already did.

### Added

- `mac.activate(app, timeout=0.5)`: one explicit activation request that
  reports `activated`, `previous`, `frontmost`, and `elapsed_ms` from
  what macOS actually did. It never retries: since macOS 14 activation
  is a request the system may decline while the user is busy elsewhere.
  Sessions were forcing this through `osascript` 88 times, some in a
  ten-attempt loop.
- `mac.wait_for_window(app, timeout=2.0)`: `windows(app)` once it is
  non-empty, polled every 50ms; `MacOSError` with code `timeout`
  otherwise.
- `mac.do.click(x, y, app=...)` and `mac.do.type(text, app=...)`: the
  receipted counterparts of `mac.click` and `mac.type`. `click` resolves
  the screen point, the target `window_id`, the button, and the click
  count before anything is reserved or posted, so a stale screenshot, a
  moved window, a point over none of the app's windows, or a fourth
  click fails with `acted=no`, and the events go to the very window the
  receipt names. A `type` receipt carries the text's length and hash,
  never the text; text longer than 4096 characters or holding a lone
  surrogate fails with `acted=no` before a `once` token is reserved.
- `mac.do.key`, `click`, and `type` receipts report what the input did to
  focus. `observed.focus` holds a before/after sample of the frontmost
  pid, the focused window title, and the focused element (role, title,
  value summary, selected range, character count, position, size), and
  `changed` lists which of those moved. A verb whose postcondition is
  not verified is `changed=True` when focus moved and `None` when nothing
  observable moved, so a key that AppKit silently dropped -- a menu
  shortcut sent to an inactive app -- comes back as a done receipt whose
  effect is unconfirmed rather than one that claims it. The sample reads
  the focused element without enhanced accessibility, so an app that
  keeps AX off stays that way. Every read in it is checked: one the app
  refuses -- it did not answer, the element is gone -- fails the whole
  reading rather than passing as a missing field the receipt would count
  as a move, while an attribute AX reports as absent is an ordinary
  absence. A secure field reports its role and subrole only, and only a
  subrole the app actually reported clears a field for its details, so a
  password's value, length, and selection are never requested. One sample
  costs about 0.4ms (p95 0.9ms over 50 readings of TextEdit, measured
  before the reads were checked; the checks add no round trip).
- The focus witness waits for the effect, but never past the deadline.
  Without a postcondition the reading after dispatch is repeated every
  10ms for up to 100ms until it differs from the one before: the first
  reading missed 70 of 80 effects that showed within 62ms. With a
  postcondition the single after-reading is taken and the postcondition
  is verified straight away, so the effect the caller asked about never
  waits behind the witness. A reading the app refuses, before or after,
  leaves the receipt without a witness (`observed.focus.after` is `None`
  when every after-reading was refused), never without its dispatch or
  its `once` token, and the polling goes on to the deadline in case the
  app answers again.
- The focus reading before dispatch counts against the budget: the
  deadline is checked again after it and before the `once` token is
  reserved, so an app whose AX tree answers slowly costs the call its
  dispatch (`acted=no`, the token unspent), never a late mutation.
- `equals(...)`, a third postcondition beside `present`/`gone`: one AX
  match resolved the way `present` resolves it, its `attribute` (default
  `AXValue`) read back every `interval`, satisfied once the reading equals
  `value` under the same canonical comparison `set` uses to judge
  convergence. `key`, `click`, and `type` gain a way to verify what a
  field holds or where a selection landed, not just that focus moved. A
  receipt carries the expected and observed values as length/SHA-256
  summaries; a timeout reports both under `error.details`. `value` must
  be JSON-safe and `attribute` nonempty, checked at construction.

### Fixed

- `mac.ax.press` ran its focus guard in a `finally`, so an `AXPress` that
  raised while the target came frontmost was reported as `focus.changed`
  instead of its own error, and a receipted `mac.do.press(app=...)` then
  read that code as proof the press landed (`acted=yes`). The guard now
  runs only after `AXPress` returns, the order `mac.do.press(apps=...)`,
  `toggle`, and the native agent already used: a failed action keeps its
  error and the receipt says `acted=unknown`.
- On the native backend, `mac.ax.get` read one attribute through the
  agent's best-effort batch op, which answers `null` for a read the app
  refuses -- it did not answer in time, the element is gone, the
  attribute is unsupported. So `mac.do.toggle` read a refused `AXValue`
  as "off" and pressed a control it could not see, `set` judged
  convergence against nothing, and `equals(value=None)` verified a
  postcondition the app never answered. The agent now has a single
  required-value read, `ax_element_get_value`, that returns the error the
  Python backend already raised: `timeout` when the app did not answer
  (`ax_error` -25204), `ax.error` otherwise, with a successful explicit
  `null` still a value. An agent binary that predates the op refuses it
  as `unsupported_op`; nothing downgrades to the batch read.
  `mac.ax.get_attributes` is unchanged: a bulk sample where an unreadable
  attribute is `None`.
- `mac.see` and `mac.wait_change` pictured a window together with every
  sheet, popover, or tip attached to it, and when one hung past the
  window's edge the group was squeezed into the window's own pixel size,
  so the picture no longer matched its `bounds`. Calculator's mode tip,
  hanging over the window's top edge, drew the window at half size, so a
  key press changed too few pixels to count and `wait_change` ran its full
  30 seconds. Such a window is now cut out of the display it sits on: full
  size, with whatever is attached drawn where it covers the window. A
  window partly off screen or across two displays still renders the old
  way.

### Removed

- The credential broker: `macos-harness credential` (`check`,
  `fill-browser`, `fill-native`, `enroll`), `CredentialBroker`,
  `CredentialManifest`, `CredentialEnrollment`, `CredentialReceipt`,
  `CredentialError`, `DEFAULT_CREDENTIAL_MANIFEST`, the worker, and
  `~/.config/macos-harness/credentials.toml`. Its one sink was a field in
  a live ego-browser taskspace, and that route is retired: browser
  credentials belong to the browser's own password manager, and a native
  login field keeps its two owners, system AutoFill or `mac.handoff`.
  Nothing else in the harness read the manifest or the vault.

## [0.5.0] - 2026-08-22

### Added

- A failed credential fill now says *where* it stopped.
  `credential.fill_failed` gained a stage suffix from a closed set --
  `/bad_job`, `/not_authorized`, `/secret_missing`, `/otp_fetch`,
  `/sink`, `/locate_space`, `/origin`, `/locate_field`, `/focus`,
  `/type_verify`, `/timeout_stage`, `/dialog_blocked` -- plus
  `credential.handoff_required/passkey` and
  `credential.unsupported_sink`. A stage name is a keyword chosen at
  author time, never a selector, origin, field value, or provider
  message, and it travels in a 0600 file the broker creates in a 0700
  directory and names in the job: an exit status could not carry it,
  because `mem-secret`, sops, or the shell can exit nonzero before the
  worker runs at all. An unnamed failure stays plain
  `credential.fill_failed`, which is what it is.
- One entry may declare more than one field:
  `field = ["#new", "#confirm"]`. The policy digest covers the whole
  ordered list, so one enrollment authorizes exactly that set in exactly
  that order, and one `fill-browser` call fills them in order inside one
  bounded window on one document proven not to have changed between
  them. A one-field entry keeps the digest it had before lists existed,
  so no live credential needs re-enrolling, and `field = "x"` and
  `field = ["x"]` agree. One value into several fields is the whole
  feature: a form asking for two *different* secrets is two refs, each
  authorized on its own.
- `credential fill-native <ref> --app <app>` and
  `CredentialBroker.fill_native` refuse, always, with
  `credential.unsupported_sink` and a message naming the two paths that
  do work: the system AutoFill sheet, or a handoff to the human. Asking
  now gets a typed answer instead of an `AttributeError`.
- A field asking for conditional passkey mediation (a `webauthn` token
  in `autocomplete`) is refused as
  `credential.handoff_required/passkey` *before* the value is collected,
  and a native dialog blocking the page is refused as
  `credential.fill_failed/dialog_blocked`. Both used to stall until a
  deadline; both are now immediate and typed.
- Every credential CLI failure prints its fixed message beside its code:
  `{"error":"<code>","message":"<fixed sentence>"}`. Both come from
  closed compile-time tables that interpolate nothing.

### Changed

- The credential worker and its browser child now run under a
  reconstructed environment -- `HOME` from the password database, a
  fixed `PATH`, a UTF-8 locale -- rather than an inherited one.
  `mem-secret` takes its vault root from `ENGRAM_ROOT` or `$HOME`, and
  the pinned `ego-browser` wrapper resolves the real browser CLI under
  `$HOME` (measured: `HOME=/tmp/attacker` sends it looking there), so an
  inherited value for either chose which vault was read and which
  program was handed the path the value crosses. An allowlist, because
  the next variable one of those helpers learns to read will not be in
  any denylist written today.
- Every step of the browser side is now bounded and named. There was no
  ceiling below the worker's 40-second one, so a single wedged ego
  helper call was invisible, unattributed, and charged to the whole
  fill's budget; a stalled arm now refuses in about two seconds as
  `/timeout_stage` instead. The handoff wait is excluded from that
  budget, since a Gmail code takes as long as the mailbox takes.
- The two origin rechecks are `Page.getFrameTree` document keys
  (main-frame id plus loaderId) instead of ego `pageInfo()` helper
  calls: one cheap CDP round trip each instead of a vendor helper, and
  strictly stronger -- a reload to the same URL is now caught, which an
  origin comparison alone accepted. `pageInfo()` is still called once,
  for the one thing only it reports: whether a dialog is blocking the
  page.
- A field is focused and cleared immediately *before* it receives the
  value rather than before the handoff wait, so a page that moves focus
  or refills the field while the fill is in flight is recovered from
  rather than refused. The readback now proves identity, focus, and
  exact value in one page turn.
- A TOTP code is generated when the browser child asks for it, after
  every preflight check has passed, rather than before the browser is
  even started -- a six-digit code is valid for a 30-second step, and a
  four-second preflight used to spend an eighth of that window. A Gmail
  mailbox is likewise not read at all for a fill the browser side
  refuses.
- `SECURITY.md` names the one race that is not closed: focus lives in
  the page and the insertion is a browser-level call, so a page that
  moves focus between them receives the keystrokes. The fill is refused
  rather than reported done, but the value reached another element on an
  already-allowed origin.

## [0.4.0] - 2026-08-22

### Added

- `macos-harness credential`: a broker that fills a browser login field
  from a configured ref, backed by the shared mem-secret sops+age vault.
  `check` lists the configured refs; `fill-browser <ref> --space <space>`
  fills a password, TOTP code, or Gmail-delivered one-time code and
  returns a frozen five-field `CredentialReceipt` (`state="filled"`,
  `credential_ref`, `provider`, `sink="browser"`, `acted=True`);
  `enroll <ref>` stores a password or TOTP secret from a hidden TTY prompt
  or from stdin when piped, and `enroll <ref> --clipboard` provisions
  instead from whatever secret is already on the clipboard -- typically the
  agent's own copy of an already-unlocked Passwords entry, revealed and
  copied through ordinary UI automation in the same bounded burst, with no
  separate human step unless macOS itself puts up a physical-presence
  prompt (Touch ID, the account password, a passkey). `--clipboard` pipes
  the clipboard into mem-secret at the OS level, so the value never enters
  the CLI's own process, and clears the clipboard afterward on every path,
  success or failure. Every command prints exactly one compact JSON line on
  success and one fixed, redacted `{"error":"<code>"}` line on failure --
  no secret, secret length, OTP, email body, provider output, clipboard
  value, or derived fingerprint ever crosses into an argument, receipt,
  error, log, or return value.
- `enroll <gmail-ref>` reads nothing at all: no prompt, no stdin, no
  clipboard -- `--clipboard` on a `gmail_otp` ref fails before anything is
  spawned, with `credential.enroll_not_authored`. An emailed
  code is read live and never stored, so that one command stores only the
  nonsecret authorization for that exact policy. A Gmail fill then runs
  under mem-secret with that authorization in its environment and compares
  it against the digest of the policy it was handed before it opens the
  mailbox, so editing the manifest alone cannot repoint a live code at a
  different mailbox, sender, pattern, origin, or field -- the rebound
  policy has no authorization until someone enrolls it again.
- Credentials are declared once in
  `~/.config/macos-harness/credentials.toml` (`version = 1`,
  `[credentials.<ref>]`, `kind = "password" | "totp" | "gmail_otp"`,
  `origins`, `field`); each kind's key set is closed, so an unknown or
  misspelled key is rejected rather than half-honored. That path is fixed
  and is the only policy any command reads. It is also checked for privacy
  before it is parsed (regular non-symlink file, this uid, mode exactly
  0600, in a directory owned by the same uid and not group- or
  world-writable) and otherwise refused with
  `credential.manifest_untrusted`.
- One policy digest owns one ref: a SHA-256 over the entry's canonical
  full policy -- ref, `kind`, sorted `origins`, `field`, and, for
  `gmail_otp`, its `mailbox`, `sender`, `subject_regex`, `body_regex`, and
  `max_age_seconds`. A password or TOTP secret is stored under a vault
  name derived from that whole digest and a Gmail authorization is derived
  from the same digest, so one enrollment authorizes exactly one source
  and one destination and no manifest can name, borrow, or rebind
  another's. Changing any of those fields derives a digest with nothing
  enrolled behind it, so the fill fails instead of sending an old secret to
  a new origin or field, or reading a code from an unauthorized mailbox --
  re-enroll after any edit. Only reordering `origins` is free.
- The manifest, `mem-secret` (at its pinned absolute path
  `~/.local/bin/mem-secret`, never resolved through `PATH`), and the
  browser toolkit are all located from the account's own home directory in
  the passwd record rather than from `$HOME`, so an exported `HOME` cannot
  move the policy, the vault, or the code that types into the page.
- Browser fills run in an existing agent-owned ego-browser taskspace that
  the broker never creates or hands off, require an exact live-origin
  match and exactly one visible, enabled, writable field of the expected
  type, and inject through a single trusted CDP `Input.insertText`. The
  field is resolved, then validated, focused, cleared, and force-armed as
  one DOM object before the secret is read; the origin and that same
  object's connected, focused, enabled, writable, visible, empty,
  expected-type state are rechecked through `Runtime.callFunctionOn`
  between the read and the keystroke; equality is then verified on that
  same object -- the expected value passed as a call argument, never
  spliced into page source -- and the origin once more, without the value
  ever leaving CDP. The only sink is a web field: a CSS selector
  cannot identify a native control, and typing a secret at whatever holds
  first responder is unsafe, so system AutoFill or a `mac.handoff(...)`
  owns app windows.
- Every fill is bounded from the outside -- 45 seconds for a password or
  TOTP, 120 seconds for a Gmail code -- and the broker owns one process
  group for the whole tree it starts (mem-secret, the worker, `gws`,
  ego-browser), so a wedged provider or stalled browser is killed as a
  group and reported as `credential.timeout`. The worker starts no session
  of its own; it keeps only its own step bounds (10 seconds per `gws` call,
  40 seconds on the browser child) and reads its nonsecret job from stdin
  bounded at 64 KiB.
- `gmail_otp` patterns are bounded in length, must compile, and
  `body_regex` must carry a `code` group; matching one message runs under a
  one-second wall-clock alarm in the worker, and a message whose alarm
  cannot be armed is refused rather than matched unbounded. Bodies are
  matched as visible text: a `text/plain` part as it is, `text/html`
  reduced with the standard library only when there is no plain part
  (`script`, `style`, `head`, `title`, `template`, `noscript`, and
  `hidden`, `aria-hidden`, `display:none`, or `visibility:hidden` elements
  dropped, entities resolved, tag boundaries becoming whitespace) -- and a
  message is used only when it yields exactly one distinct code-shaped
  capture.
- Apple Passwords remains the provisioning source, reached only through
  its own UI: the broker never unlocks Passwords, bypasses Touch ID or a
  passkey, or acts while a physical-presence prompt is on screen, but
  provisioning a ref -- including revealing and copying an
  already-unlocked entry -- is otherwise autonomous.
- TOTP is RFC 6238 (SHA-1, 6 digits, 30-second step), implemented with the
  standard library and verified against the RFC test vectors.
- `SECURITY.md` states the credential broker's threat boundary: it defends
  against leaked or model-visible values, a wrong page or mailbox, a stale
  or mistaken policy, and a hung step, and it does not claim to defend
  against a hostile process already running as the same user or against an
  agent deliberately misusing tools the user authorized -- the requested
  setup grants same-user agents that access directly.

## [0.3.0] - 2026-08-22

### Added

- `mac.handoff(reason=, app=)`: an explicit, representation-only handoff for
  a boundary only a human can cross -- Touch ID, a passkey, a CAPTCHA, a
  verification code, a Google or other sign-in approval
  (`HandoffReason.AUTHENTICATION_REQUIRED`), or a temporary PIN or account
  recovery flow (`HandoffReason.ACCOUNT_RECOVERY_REQUIRED`). Agents call it
  the moment they recognize the boundary; a generic timeout is never
  promoted to a handoff -- only an explicit call creates one. `app` is
  required and nonempty; the call resolves that already-running app,
  samples the current frontmost app, and compares PIDs -- nothing else. It
  never activates, raises, opens, or notifies anything; never reads AX
  text, a screenshot, or the clipboard; never sends keyboard or pointer
  input; never requests a permission; never uses the native agent; and
  never touches a `mac.do` receipt or once-token ledger. It returns
  immediately, with no polling and no blocking wait. The return value is a
  frozen, JSON-safe `HumanHandoff` with exactly five machine fields --
  `state`, `reason`, `retry` (`wait_for_user_then_rediscover`),
  `target_is_frontmost`, and `automation_acted` (always `False`) -- and no
  app, window, AX, prompt, timestamp, ID, or secret-derived data.
  `str(handoff)` renders one of four fixed, library-owned prompts chosen
  only by `reason` and `target_is_frontmost`; the prompt never interpolates
  app names, window titles, or AX text the target app controls. Agents
  print the prompt, end the turn, and accept only `done` or `cancelled`
  from the human: `done` authorizes fresh rediscovery through the owning
  surface with no proof of success implied, and `cancelled` stops the
  task.

## [0.2.0] - 2026-08-22

### Added

- `mac.do`: a receipted, verified operations surface for mutations,
  recommended over the raw primitives for anything that changes state.
  `press`, `set`, `toggle`, `run`, and `key` mutate; `recall` looks up a
  past receipt by its idempotency token without dispatching anything. Every
  call returns an immutable, JSON-safe `Receipt` -- `outcome` (`planned`,
  `done`, `already`, `failed`), `acted` (`no`, `yes`, `unknown`), the
  backend and executor that actually ran it, the normalized request and
  target, whether anything changed, whether a postcondition verified the
  effect, duration_s, and a structured error on failure. `present`/`gone`
  postconditions (the same call shape as `ax.wait`/`ax.wait_gone`) confirm
  an operation's real effect, and default to the operation's own scope
  when left unscoped -- except `run`, which has no scope of its own and
  requires an explicitly scoped postcondition. `press`/`run`/`key`
  additionally take a nonempty `once` token for at-most-once dispatch
  within one live `MacOS` instance. Its in-memory ledger is not shared
  with a new instance or process. A repeat call with the same token
  replays the recorded receipt instead of dispatching again, and an
  interrupted or still in-flight attempt fails closed with a failed,
  `acted="unknown"` receipt instead of risking a second dispatch.
  `dry_run=True` validates, resolves, and (for `run`) compiles a script
  without ever dispatching it or touching the idempotency ledger.
- A machine-readable error taxonomy: `errors.ErrorCode` (nine wire codes
  shared by the native agent protocol and `mac.do`) and
  `MacOSError.code`/`.details`/`.to_json()`.
- `macos-harness --version`.
- A `py.typed` marker (PEP 561): the package now ships type information in
  both the wheel and the sdist.
- Python 3.13 and 3.14 support, alongside the existing 3.11/3.12.
- Background-safe cross-process AX controls: `ax.query_all`, `ax.wait`,
  `ax.wait_gone`, and `ax.press` act across every running application (or a
  named subset), not just one, for background AutoFill sheets and system
  popovers outside the app already targeted.
- A persistent Swift Accessibility agent (opt-in native backend) that can
  take over a fixed, narrow set of Accessibility calls for process
  isolation; `python` remains the default and the latency-recommended
  choice. Each `MacOS(backend="native"/"auto")` instance that dispatches a
  routed call launches its own private child process over an inherited,
  validated UNIX-domain socket pair that only that process holds either
  end of, with the handshake bound to that child's own PID -- there is no
  shared daemon, no well-known socket path, and no pidfile. PyPI wheels
  bundle an ad-hoc-signed universal2 (arm64 + x86_64) build of the agent,
  so a cold launch costs about 240 ms on an M4 Pro instead of the ~1,125 ms
  a from-source SwiftPM build takes; most installs never build it
  themselves.

### Changed

- Telemetry is now opt-in. A fresh install sends nothing until
  `macos-harness telemetry enable`; `DO_NOT_TRACK`, `MACOS_HARNESS_TELEMETRY`,
  and `ANONYMIZED_TELEMETRY` remain fail-closed kill switches that always
  win, but no environment variable can turn telemetry on. The documented
  payload, storage path, and endpoint are now complete and accurate,
  including `python_version`, which was previously sent but undocumented.
- The package version is now defined in exactly one place
  (`src/macos_harness/_version.py`) and read directly by
  `macos_harness.__version__`, the CLI, telemetry, and Hatchling's dynamic
  version config -- nothing derives it separately through
  `importlib.metadata`.

### Security

- A telemetry endpoint override (`MACOS_HARNESS_POSTHOG_HOST`) is honored
  only when it is an HTTPS URL; any other scheme is ignored in favor of the
  built-in default, so no environment variable can silently redirect
  telemetry to an unencrypted or unintended endpoint.
- The publish workflow now triggers only on a published GitHub release, no
  longer on arbitrary `workflow_dispatch`; asserts the release tag matches
  the package's own version before doing any build work; runs Ruff, the
  non-native Python test suite, and the Swift test suite as release gates;
  builds the universal2 native agent for real (not just `swift build`); and
  reaches the trusted-publish step only through pinned, SHA-verified
  GitHub Actions.

## [0.1.2] - 2026-08-17

### Fixed

- Render the project banner correctly on PyPI.

## [0.1.1] - 2026-08-17

### Fixed

- Publish the PyPI package from a Linux runner.
- Animate the README hero image.
- Use a repository-relative path for the banner image.

## [0.1.0] - 2026-08-15

### Added

- Initial release: six raw primitives (`see`, `key`, `type`, `click`, `ax`,
  `script`) for controlling a Mac from one persistent Python process, plus
  Browser Harness integration and local filesystem/subprocess access.
