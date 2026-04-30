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
PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER = "prompt_denied_by_live_runtime_owner"

BACKEND_RESET_UNSUPPORTED_REMOTE = "backend_reset_unsupported_remote"
BACKEND_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED = "backend_reset_force_only_by_runtime_unverified"
BACKEND_RESET_FORCE_ONLY_BY_RUNNING_BINDING = "backend_reset_force_only_by_running_binding"
BACKEND_RESET_FORCE_ONLY_BY_PENDING_REQUEST = "backend_reset_force_only_by_pending_request"
BACKEND_RESET_FORCE_ONLY_BY_ACTIVE_LOADED_THREAD = "backend_reset_force_only_by_active_loaded_thread"

REPROFILE_DIRECT_WRITE_AVAILABLE = "reprofile_direct_write_available"
REPROFILE_RESET_AVAILABLE = "reprofile_reset_available"
REPROFILE_RESET_FORCE_ONLY = "reprofile_reset_force_only"
REPROFILE_RESET_FORCE_ONLY_BY_RUNTIME_UNVERIFIED = "reprofile_reset_force_only_by_runtime_unverified"
REPROFILE_BLOCKED_BY_OTHER_INSTANCE_OWNER = "reprofile_blocked_by_other_instance_owner"
REPROFILE_BLOCKED_BY_RESET_UNSUPPORTED = "reprofile_blocked_by_reset_unsupported"
REPROFILE_BLOCKED_BY_UNBOUND_THREAD = "reprofile_blocked_by_unbound_thread"


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
