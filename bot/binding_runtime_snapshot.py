"""Pure projection from manager-owned runtime state to its immutable contract."""

from __future__ import annotations

from typing import TypeVar

from bot.binding_runtime_contract import (
    BindingExecutionSnapshot,
    BindingGoalSnapshot,
    BindingPlanSnapshot,
    BindingPlanStepSnapshot,
    BindingRuntimeHandle,
    BindingRuntimeSettingsSnapshot,
    BindingSessionSnapshot,
    BindingThreadSnapshot,
)
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.runtime_state import (
    ExecutionPatchTimerTicket,
    ExecutionPatchTimerRegistration,
    MirrorWatchdogTicket,
    MirrorWatchdogRegistration,
    RuntimeStateDict,
)


_RUNTIME_STATE_FIELDS = frozenset(RuntimeStateDict.__required_keys__)
_PLAN_STEP_FIELDS = frozenset({"step", "status"})
_RegistrationT = TypeVar("_RegistrationT")


def _require_complete_runtime_state(state: RuntimeStateDict) -> None:
    if type(state) is not dict:
        raise TypeError("binding runtime snapshot requires an exact runtime-state dict")
    fields = frozenset(state)
    if fields != _RUNTIME_STATE_FIELDS:
        raise TypeError("binding runtime snapshot requires the complete runtime-state schema")


def _copy_exact_list(value: object, *, field: str) -> tuple[object, ...]:
    if type(value) is not list:
        raise TypeError(f"binding runtime {field} must be an exact list")
    return tuple(value)


def _project_plan_steps(value: object) -> tuple[BindingPlanStepSnapshot, ...]:
    raw_steps = _copy_exact_list(value, field="plan_steps")
    projected: list[BindingPlanStepSnapshot] = []
    for raw_step in raw_steps:
        if type(raw_step) is not dict or frozenset(raw_step) != _PLAN_STEP_FIELDS:
            raise TypeError(
                "binding runtime plan step must be an exact step/status dict"
            )
        projected.append(
            BindingPlanStepSnapshot(
                step=raw_step["step"],
                status=raw_step["status"],
            )
        )
    return tuple(projected)


def _registration_present(
    value: object,
    expected_type: type[_RegistrationT],
    expected_ticket_type: type[object],
    *,
    binding: tuple[str, str],
    field: str,
) -> bool:
    if value is None:
        return False
    if type(value) is not expected_type:
        raise TypeError(f"binding runtime {field} has an invalid registration")
    if (
        type(value.ticket) is not expected_ticket_type
        or value.ticket.binding != binding
        or not callable(getattr(value.timer, "cancel", None))
    ):
        raise TypeError(f"binding runtime {field} has an invalid registration")
    return True


def project_binding_session_snapshot(
    state: RuntimeStateDict,
    *,
    handle: BindingRuntimeHandle,
) -> BindingSessionSnapshot:
    """Copy one exact resident runtime state into the canonical read contract.

    Primitive fields deliberately cross this boundary unchanged.  The strict
    contract constructors reject malformed values instead of letting this
    projector repair, normalize, or guess at manager-owned facts.
    """

    if type(handle) is not BindingRuntimeHandle:
        raise TypeError("binding runtime snapshot requires an exact handle")
    _require_complete_runtime_state(state)
    configured_settings = _copy_exact_list(
        state["configured_settings"],
        field="configured_settings",
    )
    return BindingSessionSnapshot(
        handle=handle,
        active=state["active"],
        thread=BindingThreadSnapshot(
            working_dir=state["working_dir"],
            thread_id=state["current_thread_id"],
            title=state["current_thread_title"],
            feishu_runtime_state=state["feishu_runtime_state"],
        ),
        settings=BindingRuntimeSettingsSnapshot(
            approval_policy=state["approval_policy"],
            permissions_profile_id=state["permissions_profile_id"],
            model=state["model"],
            reasoning_effort=state["reasoning_effort"],
            configured_settings=configured_settings,  # type: ignore[arg-type]
        ),
        goal=BindingGoalSnapshot(
            objective=state["goal_objective"],
            status=state["goal_status"],
            token_budget=state["goal_token_budget"],
            tokens_used=state["goal_tokens_used"],
            time_used_seconds=state["goal_time_used_seconds"],
            created_at=state["goal_created_at"],
            updated_at=state["goal_updated_at"],
        ),
        execution=BindingExecutionSnapshot(
            running=state["running"],
            cancelled=state["cancelled"],
            pending_cancel=state["pending_cancel"],
            current_turn_id=state["current_turn_id"],
            pages=state["execution_pages"],
            current_execution_kind=state["current_execution_kind"],
            current_prompt_message_id=state["current_prompt_message_id"],
            current_prompt_reply_in_thread=state[
                "current_prompt_reply_in_thread"
            ],
            current_actor_open_id=state["current_actor_open_id"],
            transcript=ExecutionTranscriptSnapshot.from_transcript(
                state["execution_transcript"]
            ),
            runtime_channel_state=state["runtime_channel_state"],
            started_at=state["started_at"],
            last_runtime_event_at=state["last_runtime_event_at"],
            last_patch_at=state["last_patch_at"],
            patch_timer_registered=_registration_present(
                state["patch_timer_registration"],
                ExecutionPatchTimerRegistration,
                ExecutionPatchTimerTicket,
                binding=handle.binding,
                field="patch_timer_registration",
            ),
            mirror_watchdog_registered=_registration_present(
                state["mirror_watchdog_registration"],
                MirrorWatchdogRegistration,
                MirrorWatchdogTicket,
                binding=handle.binding,
                field="mirror_watchdog_registration",
            ),
            followup_sent=state["followup_sent"],
            followup_text=state["followup_text"],
            terminal_result_text=state["terminal_result_text"],
            awaiting_local_turn_started=state["awaiting_local_turn_started"],
            awaiting_attach_status_settle=state["awaiting_attach_status_settle"],
        ),
        plan=BindingPlanSnapshot(
            message_id=state["plan_message_id"],
            turn_id=state["plan_turn_id"],
            explanation=state["plan_explanation"],
            steps=_project_plan_steps(state["plan_steps"]),
            text=state["plan_text"],
        ),
    )
