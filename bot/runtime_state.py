"""
Explicit command/event objects for Codex runtime state mutations.

The handler still owns orchestration, but state writes now flow through a small
reducer instead of being scattered as ad-hoc dict assignments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, MutableMapping

UNSET = object()


class RuntimeStateMessage:
    """Base type for explicit runtime state mutations."""


class RuntimeStateCommand(RuntimeStateMessage):
    """Mutation initiated by local command handling."""


class RuntimeStateEvent(RuntimeStateMessage):
    """Mutation initiated by runtime callbacks / external events."""


@dataclass(frozen=True, slots=True)
class BindingActivated(RuntimeStateCommand):
    active: bool = True


@dataclass(frozen=True, slots=True)
class StoredBindingHydrated(RuntimeStateCommand):
    working_dir: str
    current_thread_id: str
    current_thread_title: str
    approval_policy: str
    sandbox: str
    collaboration_mode: str


@dataclass(frozen=True, slots=True)
class RuntimeSettingsChanged(RuntimeStateCommand):
    approval_policy: Any = UNSET
    sandbox: Any = UNSET
    collaboration_mode: Any = UNSET


@dataclass(frozen=True, slots=True)
class ThreadStateChanged(RuntimeStateCommand):
    working_dir: Any = UNSET
    current_thread_id: Any = UNSET
    current_thread_title: Any = UNSET


@dataclass(frozen=True, slots=True)
class ExecutionAnchorCleared(RuntimeStateEvent):
    clear_card_message: bool


@dataclass(frozen=True, slots=True)
class ExecutionRetired(RuntimeStateEvent):
    runtime_channel_state: str = "live"


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat(RuntimeStateEvent):
    occurred_at: float
    channel_state: str = "live"


@dataclass(frozen=True, slots=True)
class ExecutionStateChanged(RuntimeStateEvent):
    running: Any = UNSET
    cancelled: Any = UNSET
    pending_cancel: Any = UNSET
    awaiting_local_turn_started: Any = UNSET
    current_turn_id: Any = UNSET
    current_message_id: Any = UNSET
    last_execution_message_id: Any = UNSET
    current_prompt_message_id: Any = UNSET
    current_actor_open_id: Any = UNSET
    runtime_channel_state: Any = UNSET
    started_at: Any = UNSET
    last_runtime_event_at: Any = UNSET
    last_patch_at: Any = UNSET
    followup_sent: Any = UNSET
    patch_timer: Any = UNSET
    mirror_watchdog_timer: Any = UNSET
    mirror_watchdog_generation: Any = UNSET
    bump_mirror_watchdog_generation: bool = False
    reset_transcript: bool = False
    reply_text: str | None = None


@dataclass(frozen=True, slots=True)
class PlanStateChanged(RuntimeStateEvent):
    clear: bool = False
    plan_message_id: Any = UNSET
    plan_turn_id: Any = UNSET
    plan_explanation: Any = UNSET
    plan_steps: Any = UNSET
    plan_text: Any = UNSET


def apply_runtime_state_message(state: MutableMapping[str, Any], message: RuntimeStateMessage) -> None:
    match message:
        case BindingActivated(active=active):
            state["active"] = active
        case StoredBindingHydrated(
            working_dir=working_dir,
            current_thread_id=current_thread_id,
            current_thread_title=current_thread_title,
            approval_policy=approval_policy,
            sandbox=sandbox,
            collaboration_mode=collaboration_mode,
        ):
            state["working_dir"] = working_dir
            state["current_thread_id"] = current_thread_id
            state["current_thread_title"] = current_thread_title
            state["approval_policy"] = approval_policy
            state["sandbox"] = sandbox
            state["collaboration_mode"] = collaboration_mode
        case RuntimeSettingsChanged(
            approval_policy=approval_policy,
            sandbox=sandbox,
            collaboration_mode=collaboration_mode,
        ):
            if approval_policy is not UNSET:
                state["approval_policy"] = approval_policy
            if sandbox is not UNSET:
                state["sandbox"] = sandbox
            if collaboration_mode is not UNSET:
                state["collaboration_mode"] = collaboration_mode
        case ThreadStateChanged(
            working_dir=working_dir,
            current_thread_id=current_thread_id,
            current_thread_title=current_thread_title,
        ):
            if working_dir is not UNSET:
                state["working_dir"] = working_dir
            if current_thread_id is not UNSET:
                state["current_thread_id"] = current_thread_id
            if current_thread_title is not UNSET:
                state["current_thread_title"] = current_thread_title
        case ExecutionAnchorCleared(clear_card_message=clear_card_message):
            if clear_card_message:
                state["current_message_id"] = ""
            state["current_turn_id"] = ""
            state["current_prompt_message_id"] = ""
            state["current_actor_open_id"] = ""
            state["awaiting_local_turn_started"] = False
        case ExecutionRetired(runtime_channel_state=runtime_channel_state):
            current_message_id = str(state["current_message_id"] or "").strip()
            if current_message_id:
                state["last_execution_message_id"] = current_message_id
            apply_runtime_state_message(state, ExecutionAnchorCleared(clear_card_message=True))
            state["running"] = False
            state["pending_cancel"] = False
            state["runtime_channel_state"] = runtime_channel_state
        case RuntimeHeartbeat(occurred_at=occurred_at, channel_state=channel_state):
            state["last_runtime_event_at"] = occurred_at
            state["runtime_channel_state"] = channel_state
        case ExecutionStateChanged() as change:
            if change.running is not UNSET:
                state["running"] = change.running
            if change.cancelled is not UNSET:
                state["cancelled"] = change.cancelled
            if change.pending_cancel is not UNSET:
                state["pending_cancel"] = change.pending_cancel
            if change.awaiting_local_turn_started is not UNSET:
                state["awaiting_local_turn_started"] = change.awaiting_local_turn_started
            if change.current_turn_id is not UNSET:
                state["current_turn_id"] = change.current_turn_id
            if change.current_message_id is not UNSET:
                state["current_message_id"] = change.current_message_id
            if change.last_execution_message_id is not UNSET:
                state["last_execution_message_id"] = change.last_execution_message_id
            if change.current_prompt_message_id is not UNSET:
                state["current_prompt_message_id"] = change.current_prompt_message_id
            if change.current_actor_open_id is not UNSET:
                state["current_actor_open_id"] = change.current_actor_open_id
            if change.runtime_channel_state is not UNSET:
                state["runtime_channel_state"] = change.runtime_channel_state
            if change.started_at is not UNSET:
                state["started_at"] = change.started_at
            if change.last_runtime_event_at is not UNSET:
                state["last_runtime_event_at"] = change.last_runtime_event_at
            if change.last_patch_at is not UNSET:
                state["last_patch_at"] = change.last_patch_at
            if change.followup_sent is not UNSET:
                state["followup_sent"] = change.followup_sent
            if change.patch_timer is not UNSET:
                state["patch_timer"] = change.patch_timer
            if change.mirror_watchdog_timer is not UNSET:
                state["mirror_watchdog_timer"] = change.mirror_watchdog_timer
            if change.mirror_watchdog_generation is not UNSET:
                state["mirror_watchdog_generation"] = change.mirror_watchdog_generation
            if change.bump_mirror_watchdog_generation:
                state["mirror_watchdog_generation"] += 1
            if change.reset_transcript:
                state["execution_transcript"].reset()
            if change.reply_text is not None:
                state["execution_transcript"].set_reply_text(change.reply_text)
        case PlanStateChanged(clear=True):
            state["plan_message_id"] = ""
            state["plan_turn_id"] = ""
            state["plan_explanation"] = ""
            state["plan_steps"] = []
            state["plan_text"] = ""
        case PlanStateChanged(
            plan_message_id=plan_message_id,
            plan_turn_id=plan_turn_id,
            plan_explanation=plan_explanation,
            plan_steps=plan_steps,
            plan_text=plan_text,
        ):
            if plan_message_id is not UNSET:
                state["plan_message_id"] = plan_message_id
            if plan_turn_id is not UNSET:
                state["plan_turn_id"] = plan_turn_id
            if plan_explanation is not UNSET:
                state["plan_explanation"] = plan_explanation
            if plan_steps is not UNSET:
                state["plan_steps"] = plan_steps
            if plan_text is not UNSET:
                state["plan_text"] = plan_text
        case _:
            raise TypeError(f"Unsupported runtime state message: {type(message)!r}")
