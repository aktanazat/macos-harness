# Security

Please report vulnerabilities privately through GitHub Security Advisories for
`browser-use/macos-harness`.

macOS Harness can control applications and read visible UI content with the
permissions the user grants it. Treat agent instructions and third-party UI as
untrusted input, review irreversible actions, and grant the minimum permissions
needed for the task.

## Credential broker boundary

The credential broker limits accidental disclosure and wrong-destination fills.
It keeps secret values out of command arguments, receipts, errors, logs, and
model-visible output. It accepts only the owner-only manifest at
`~/.config/macos-harness/credentials.toml`, binds enrollment to the entry's full
source and destination policy, verifies the live browser origin and field, and
bounds provider and browser work. Gmail messages must match the authorized
mailbox, sender, age, subject, visible body, and code shape.

The broker does not protect against a malicious process running as the same
macOS user. It also cannot protect against an agent that intentionally invokes
local tools the user authorized, a compromised trusted executable, or a
compromised allowed website. These actors already have the same account-level
access as the broker. Run untrusted agents under another macOS account.

The broker cannot bypass Touch ID, passkeys, the Mac login password, CAPTCHA,
provider approval, or a locked Passwords app. These checks require physical
user presence. Native app fields use system AutoFill or `mac.handoff`; the
broker fills browser fields only.

The environment a fill runs in is reconstructed, not inherited: `HOME` from
the password database, a fixed `PATH`, and a UTF-8 locale. Nothing else is
passed through. `mem-secret` resolves its vault through `ENGRAM_ROOT` or
`$HOME`, and the pinned `ego-browser` wrapper resolves the real browser CLI
through `$HOME`, so an inherited value for either would choose which vault is
read and which program is handed the value. A Gmail entry whose `gws`
credentials are reachable only through an environment pointer must be
configured on disk instead.

A failed fill reports which stage refused -- `credential.fill_failed/<stage>`
from a closed, author-time set of stage names. A stage name is never a
selector, an origin, a field value, or provider output, and the worker reports
it through a file the broker created for that purpose rather than through
stdout, stderr, or an exit status any other process could also produce.

One residual race is known and not closed. Focus lives in the page and the
insertion is a browser-level call, so the two cannot be one turn: a page that
moves focus between them receives the keystrokes instead. The readback then
refuses the fill -- it is never reported as done -- but the value did reach
another element on an already-allowed origin. Closing it would mean giving up
trusted input for a scripted value assignment, which real login forms treat
differently.
