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
