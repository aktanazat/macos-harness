# Human handoff at a known boundary

Call `mac.handoff(reason=..., app=...)` the moment a task hits Touch ID, a
passkey, a CAPTCHA, a verification code, a Google or other sign-in approval, a
temporary PIN, or account recovery. Do it immediately: do not burn a
`mac.do`/`ax.wait` timeout first, and do not infer a handoff from a timeout that
happened for an unrelated reason. An absence you cannot identify stays a plain
timeout.

```python
from macos_harness import HandoffReason

handoff = mac.handoff(reason=HandoffReason.AUTHENTICATION_REQUIRED, app="Safari")
print(handoff)
```

Use `HandoffReason.ACCOUNT_RECOVERY_REQUIRED` for a temporary PIN or account
recovery flow and `AUTHENTICATION_REQUIRED` for everything else above. A PID is
the fastest, least ambiguous `app`; a name works when that is what you have.

The call only describes the boundary and returns at once: it resolves the named
app, compares its PID to the frontmost app, and does nothing else. No AX read,
screenshot, clipboard, input, activation, raise, open, notification,
permission request, native agent call, or receipt. `print(handoff)` shows one
of four fixed prompts, chosen only by `reason` and `target_is_frontmost`. Print
it and end the turn; accept only `done` or `cancelled` from the human.

After `done`, rediscover state from scratch through the surface that owns it
(`mac.see`, `mac.ax`, ...). The handoff is an acknowledgement, not proof the
human succeeded, and carries no once token, receipt, or resume method to skip
that rediscovery. After `cancelled`, stop; do not rediscover or retry.
