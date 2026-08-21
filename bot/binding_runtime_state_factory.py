"""Stateless construction and persistence projection for binding runtime state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from bot.approval_policy import normalize_approval_policy
from bot.execution_pages import ExecutionPageLedger
from bot.execution_transcript import ExecutionTranscript
from bot.feishu_types import StoredChatBinding
from bot.permissions_profile import (
    BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
    normalize_permissions_profile_id,
)
from bot.runtime_state import (
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    RuntimeStateDict,
    StoredBindingHydrated,
    apply_runtime_state_message,
)


@dataclass(frozen=True, slots=True)
class BindingRuntimeStateFactory:
    """Pure owner of binding runtime-state construction and projection rules."""

    default_working_dir: str
    default_approval_policy: str
    default_model: str
    default_reasoning_effort: str
    default_permissions_profile_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_working_dir",
            str(self.default_working_dir or "").strip(),
        )
        object.__setattr__(
            self,
            "default_approval_policy",
            str(self.default_approval_policy or "").strip(),
        )
        object.__setattr__(
            self,
            "default_permissions_profile_id",
            normalize_permissions_profile_id(
                str(self.default_permissions_profile_id or "").strip(),
                fallback=BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
            ),
        )
        object.__setattr__(
            self,
            "default_model",
            str(self.default_model or "").strip(),
        )
        object.__setattr__(
            self,
            "default_reasoning_effort",
            str(self.default_reasoning_effort or "").strip(),
        )

    @staticmethod
    def build_default_stored_binding() -> StoredChatBinding:
        return {
            "working_dir": "",
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "approval_policy": "",
            "permissions_profile_id": "",
            "model": "",
            "reasoning_effort": "",
            "configured_settings": [],
        }

    def build_default_runtime_state(self) -> RuntimeStateDict:
        return {
            "active": False,
            "working_dir": self.default_working_dir,
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "goal_objective": "",
            "goal_status": "",
            "goal_token_budget": None,
            "goal_tokens_used": 0,
            "goal_time_used_seconds": 0,
            "goal_created_at": 0,
            "goal_updated_at": 0,
            "current_turn_id": "",
            "running": False,
            "cancelled": False,
            "pending_cancel": False,
            "execution_pages": ExecutionPageLedger.empty(),
            "current_execution_kind": "",
            "current_prompt_message_id": "",
            "current_prompt_reply_in_thread": False,
            "current_actor_open_id": "",
            "execution_transcript": ExecutionTranscript(),
            "runtime_channel_state": "live",
            "started_at": 0.0,
            "last_runtime_event_at": 0.0,
            "last_patch_at": 0.0,
            "patch_timer_registration": None,
            "mirror_watchdog_registration": None,
            "followup_sent": False,
            "followup_text": "",
            "terminal_result_text": "",
            "awaiting_local_turn_started": False,
            "awaiting_attach_status_settle": False,
            "approval_policy": self.default_approval_policy,
            "permissions_profile_id": self.default_permissions_profile_id,
            "model": self.default_model,
            "reasoning_effort": self.default_reasoning_effort,
            "configured_settings": [],
            "plan_message_id": "",
            "plan_turn_id": "",
            "plan_explanation": "",
            "plan_steps": [],
            "plan_text": "",
        }

    def hydrate_stored_binding(
        self,
        state: RuntimeStateDict,
        stored_binding: Mapping[str, Any],
    ) -> bool:
        """Apply one durable binding record and report attached-state downgrade."""

        feishu_runtime_state = stored_binding["feishu_runtime_state"]
        downgraded_attached = False
        if feishu_runtime_state == FEISHU_RUNTIME_ATTACHED:
            feishu_runtime_state = FEISHU_RUNTIME_DETACHED
            downgraded_attached = True
        apply_runtime_state_message(
            state,
            StoredBindingHydrated(
                working_dir=(
                    stored_binding["working_dir"] or self.default_working_dir
                ),
                current_thread_id=stored_binding["current_thread_id"],
                current_thread_title=stored_binding["current_thread_title"],
                feishu_runtime_state=feishu_runtime_state,
                approval_policy=normalize_approval_policy(
                    stored_binding["approval_policy"]
                    or self.default_approval_policy,
                ),
                permissions_profile_id=normalize_permissions_profile_id(
                    stored_binding.get("permissions_profile_id", "")
                    or self.default_permissions_profile_id,
                    fallback=self.default_permissions_profile_id,
                ),
                model=str(stored_binding.get("model", "") or "").strip(),
                reasoning_effort=str(
                    stored_binding.get("reasoning_effort", "") or ""
                ).strip(),
                configured_settings=list(
                    stored_binding.get("configured_settings", [])
                ),
            ),
        )
        return downgraded_attached

    def stored_binding_from_runtime(
        self,
        state: RuntimeStateDict,
    ) -> StoredChatBinding:
        """Project the durable subset of one mutable binding runtime."""

        current_thread_id = str(state["current_thread_id"]).strip()
        feishu_runtime_state = str(state["feishu_runtime_state"]).strip()
        if not current_thread_id:
            feishu_runtime_state = ""
        working_dir = str(state["working_dir"]).strip()
        approval_policy = normalize_approval_policy(
            str(state["approval_policy"]).strip()
        )
        permissions_profile_id = normalize_permissions_profile_id(
            str(state["permissions_profile_id"]).strip(),
            fallback=self.default_permissions_profile_id,
        )
        return {
            "working_dir": (
                "" if working_dir == self.default_working_dir else working_dir
            ),
            "current_thread_id": current_thread_id,
            "current_thread_title": str(state["current_thread_title"]).strip(),
            "feishu_runtime_state": feishu_runtime_state,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
            "model": str(state["model"]).strip(),
            "reasoning_effort": str(state["reasoning_effort"]).strip(),
            "configured_settings": list(state.get("configured_settings", [])),
        }

    @staticmethod
    def is_empty_stored_binding(stored_binding: Mapping[str, Any]) -> bool:
        return (
            not str(stored_binding.get("working_dir", "") or "").strip()
            and not str(
                stored_binding.get("current_thread_id", "") or ""
            ).strip()
            and not str(
                stored_binding.get("current_thread_title", "") or ""
            ).strip()
            and not str(
                stored_binding.get("feishu_runtime_state", "") or ""
            ).strip()
            and not list(stored_binding.get("configured_settings", []))
        )
