# Human handoff

Call `mac.handoff` the moment a task hits Touch ID, a passkey, a CAPTCHA, a verification code, a sign-in approval, a temporary PIN or account recovery; do not wait out a timeout first.

```python
from macos_harness import HandoffReason

handoff = mac.handoff(reason=HandoffReason.AUTHENTICATION_REQUIRED, app="Safari")
print(handoff)
```

- `ACCOUNT_RECOVERY_REQUIRED` for a temporary PIN or recovery; `AUTHENTICATION_REQUIRED` otherwise. A pid is the best `app`.
- It only describes the boundary: no input, capture or activation. Print it and end the turn; accept only `done` or `cancelled`.
- After `done`, rediscover state from scratch (it proves nothing). After `cancelled`, stop.
- A password-only login or lock screen in an explicitly approved local test VM is not a gate: follow `credentials`.
