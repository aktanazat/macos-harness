# Credentials, browsers, and permissions

- Web pages go to the `browser` skill; never drive a web page through Accessibility or the preloaded `browser` object.
- Never print or store a secret, one-time code, cookie or autofill result. Verify URL and account state after each submit.
- Native logins use system AutoFill or a handoff. Typing into an unconfirmed control can send a password to the wrong process.
- An owner-authorized password login to a named local test VM (its lock screen included): confirm VM, account, window, field and focus, then type the test password; stop if target or focus changed; never echo or save it.
- Not covered: the host lock screen, Passwords or Keychain unlock, production accounts, Touch ID, passkeys, codes, CAPTCHA, recovery: `mac.handoff(...)`. Tell the user before any call that can raise a system prompt.
- Permissions are granted and telemetry is off; keep it so. `macos-harness doctor` only after a permission or connection error; `doctor --request` only with the user's approval.
