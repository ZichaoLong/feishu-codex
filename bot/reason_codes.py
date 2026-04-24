from __future__ import annotations

from dataclasses import dataclass


UNSUBSCRIBE_NOT_APPLICABLE_NO_THREAD = "unsubscribe_not_applicable_no_thread"
UNSUBSCRIBE_NOT_APPLICABLE_NO_BINDING = "unsubscribe_not_applicable_no_binding"
UNSUBSCRIBE_NOT_APPLICABLE_ALREADY_RELEASED = "unsubscribe_not_applicable_already_released"
UNSUBSCRIBE_BLOCKED_BY_INFLIGHT_TURN = "unsubscribe_blocked_by_inflight_turn"
UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST = "unsubscribe_blocked_by_pending_request"

BINDING_CLEAR_BLOCKED_BINDING_NOT_FOUND = "binding_clear_blocked_binding_not_found"
BINDING_CLEAR_BLOCKED_BY_INFLIGHT_TURN = "binding_clear_blocked_by_inflight_turn"
BINDING_CLEAR_BLOCKED_BY_PENDING_REQUEST = "binding_clear_blocked_by_pending_request"

PROMPT_DENIED_BY_RUNNING_TURN = "prompt_denied_by_running_turn"
PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING = "prompt_denied_by_group_all_mode_sharing"
PROMPT_DENIED_BY_OTHER_GROUP_ALL_OWNER = "prompt_denied_by_other_group_all_owner"
PROMPT_DENIED_BY_INTERACTION_OWNER = "prompt_denied_by_interaction_owner"


@dataclass(frozen=True, slots=True)
class ReasonedCheck:
    allowed: bool
    reason_code: str = ""
    reason_text: str = ""

    @classmethod
    def allow(cls) -> "ReasonedCheck":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason_code: str, reason_text: str) -> "ReasonedCheck":
        return cls(
            allowed=False,
            reason_code=str(reason_code or "").strip(),
            reason_text=str(reason_text or "").strip(),
        )
