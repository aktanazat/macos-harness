"""Representation-only handoff records for human security boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

__all__ = ["HandoffReason", "HumanHandoff"]


class HandoffReason(StrEnum):
    """The two reviewed security boundaries that require a human."""

    AUTHENTICATION_REQUIRED = "authentication_required"
    ACCOUNT_RECOVERY_REQUIRED = "account_recovery_required"


_PROMPTS = {
    (HandoffReason.AUTHENTICATION_REQUIRED, True): (
        "A secure authentication step is waiting in the app currently in front. "
        "Complete it yourself. Do not share passwords, passkeys, verification "
        "codes, temporary PINs, or recovery details with the agent. Return here "
        'and reply only "done" or "cancelled".'
    ),
    (HandoffReason.AUTHENTICATION_REQUIRED, False): (
        "A secure authentication step requires the app to be in front. Switch to "
        "it yourself, then complete the step there. Do not share passwords, "
        "passkeys, verification codes, temporary PINs, or recovery details with "
        'the agent. Return here and reply only "done" or "cancelled".'
    ),
    (HandoffReason.ACCOUNT_RECOVERY_REQUIRED, True): (
        "Account recovery is waiting in the app currently in front. Verify the "
        "service and account, then complete recovery yourself. Do not share any "
        "temporary PIN, verification code, password, passkey, or recovery details "
        'with the agent. Return here and reply only "done" or "cancelled".'
    ),
    (HandoffReason.ACCOUNT_RECOVERY_REQUIRED, False): (
        "Account recovery requires a trusted app or website in front. Switch to it "
        "yourself, verify the service and account, then complete recovery. Do not "
        "share any temporary PIN, verification code, password, passkey, or recovery "
        'details with the agent. Return here and reply only "done" or "cancelled".'
    ),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class HumanHandoff:
    """A side-effect-free instruction to pause for a human security step.

    The record is not evidence that authentication or recovery completed. After
    the human answers, the agent must inspect the owning surface from fresh state.
    """

    reason: HandoffReason
    target_is_frontmost: bool
    state: Literal["human_action_required"] = field(
        default="human_action_required", init=False
    )
    retry: Literal["wait_for_user_then_rediscover"] = field(
        default="wait_for_user_then_rediscover", init=False
    )
    automation_acted: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        try:
            reason = HandoffReason(self.reason)
        except (TypeError, ValueError):
            raise ValueError(
                "Handoff reason must be 'authentication_required' or "
                "'account_recovery_required'"
            ) from None
        if not isinstance(self.target_is_frontmost, bool):
            raise TypeError("target_is_frontmost must be bool")
        object.__setattr__(self, "reason", reason)

    def to_json(self) -> dict[str, str | bool]:
        """Return the fixed, JSON-safe machine contract."""
        return {
            "state": self.state,
            "reason": self.reason.value,
            "retry": self.retry,
            "target_is_frontmost": self.target_is_frontmost,
            "automation_acted": self.automation_acted,
        }

    def to_prompt(self) -> str:
        """Return the fixed human prompt for this reason and focus state."""
        return _PROMPTS[(self.reason, self.target_is_frontmost)]

    def __str__(self) -> str:
        return self.to_prompt()
