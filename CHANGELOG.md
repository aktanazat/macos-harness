# Changelog

All notable changes to macOS Harness are documented here. This project
follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

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
  shortcut sent to an inactive app -- no longer reads as a success. The
  reading after dispatch is repeated every 10ms for up to 100ms until it
  differs from the one before: the first reading missed 70 of 80 effects
  that showed within 62ms. A reading the app's AX tree refuses leaves
  the receipt without a witness, never without its dispatch or its
  `once` token. The sample reads the focused element without enhanced
  accessibility, so an app that keeps AX off stays that way; a secure
  field reports its role and subrole only, so a password's value,
  length, and selection are never requested. One sample costs about
  0.4ms (p95 0.9ms over 50 readings of TextEdit).

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
