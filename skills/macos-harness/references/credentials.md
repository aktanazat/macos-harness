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

A native app's login field belongs to system AutoFill or a physical handoff.
Typing a secret into whichever native control has focus can send it to the
wrong process, so macOS Harness does not fill native credential fields.

Call `mac.handoff(...)` only at a gate owned by macOS or the provider: Touch ID,
a passkey, the Mac login password, a CAPTCHA, or a sign-in approval. Never
unlock Passwords, get around Touch ID, or act while a physical-presence prompt
is on screen. Tell the user before any call that can raise a system prompt.

## Permissions

Permissions on this Mac are granted and telemetry is off; keep it that way.
Run `macos-harness doctor` only after a call fails with a permission,
Accessibility, or connection error: it inspects permissions without prompting.
Run `macos-harness doctor --request` only with the user's approval.
Accessibility, screen recording, and event posting are global; Apple Events
Automation is per target app.
