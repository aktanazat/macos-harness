# Credentials, browsers, and permissions

## Browser pages

Web pages go to the `safari` skill: it reuses the owner's signed-in Safari
session and fills the matching field from Apple Passwords. Never drive a web
page through macOS Accessibility, and never build a second browser credential
path here. The preloaded `browser` object (Browser Harness over Chrome's
DevTools Protocol) is not the route for web work on this Mac.

Treat a login listing as metadata only: never print or store a secret,
one-time code, session cookie, or autofill result. Verify the URL and visible
account state after each submit, because a return value does not prove the page
advanced.

## Native login fields

Native credentials normally use system AutoFill or a physical handoff.
Typing into an unconfirmed control can send a password to the wrong process.

When the owner explicitly authorizes an ordinary password login to a named
local test VM, complete it without another handoff. This includes the VM's
password-only lock screen. Confirm the current VM, guest account, window,
password field, and focus before entering the authorized test password.
Target only that VM's process and window; stop before input if either target
or focus has changed. Never echo or save the password.

This exception does not cover the host Mac's lock screen, Passwords or
Keychain unlocking, production accounts, Touch ID, passkeys, verification
codes, CAPTCHA, account recovery, or approvals requiring the user's presence.
Use `mac.handoff(...)` at those gates; do not bypass them. Tell the user before
any call that can raise a system prompt.

## Permissions

Permissions on this Mac are granted and telemetry is off; keep it that way.
Run `macos-harness doctor` only after a call fails with a permission,
Accessibility, or connection error: it inspects permissions without prompting.
Run `macos-harness doctor --request` only with the user's approval.
Accessibility, screen recording, and event posting are global; Apple Events
Automation is per target app.
