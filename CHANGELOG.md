# Changelog

All notable changes to macOS Harness are documented here. This project
follows [Semantic Versioning](https://semver.org/).

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
