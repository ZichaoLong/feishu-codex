"""
Canonical runtime-state schema, status vocabulary, and reducer messages.

Top-level orchestration still lives in `CodexHandler`, but the authoritative
shape of the mutable runtime-state dict and its mutation messages live here so
controllers and stores do not redefine partial local variants.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from bot.binding_identity import ChatBindingKey
from bot.execution_pages import ExecutionPageLedger, ExecutionTranscriptCursor
from bot.execution_transcript import ExecutionTranscript

FEISHU_RUNTIME_ATTACHED = "attached"
FEISHU_RUNTIME_DETACHED = "detached"
FEISHU_RUNTIME_NOT_APPLICABLE = "not-applicable"
VALID_FEISHU_RUNTIME_STATES = frozenset(
    {
        FEISHU_RUNTIME_ATTACHED,
        FEISHU_RUNTIME_DETACHED,
    }
)
CONFIGURABLE_RUNTIME_SETTING_KEYS = frozenset(
    {
        "approval_policy",
        "permissions_profile_id",
        "model",
        "reasoning_effort",
    }
)

BACKEND_THREAD_STATUS_IDLE = "idle"
BACKEND_THREAD_STATUS_ACTIVE = "active"
BACKEND_THREAD_STATUS_NOT_LOADED = "notLoaded"
BACKEND_THREAD_STATUS_SYSTEM_ERROR = "systemError"
BACKEND_THREAD_STATUS_UNKNOWN = "unknown"
BACKEND_THREAD_LOOKUP_MISSING = "missing"
BACKEND_THREAD_LOOKUP_ERROR = "error"
ACTIVE_OBSERVER_EXECUTION_KIND = "active_observer"
LOADED_BACKEND_THREAD_STATUSES = frozenset(
    {
        BACKEND_THREAD_STATUS_IDLE,
        BACKEND_THREAD_STATUS_ACTIVE,
        BACKEND_THREAD_STATUS_SYSTEM_ERROR,
    }
)


def is_confirmed_inactive_backend_thread_status(value: object) -> bool:
    """Whether a backend status is positive evidence that a thread is idle.

    This predicate deliberately has a much narrower meaning than "not
    active".  A status notification may be malformed, from a newer
    app-server enum, or ``systemError`` while work/interactions still need
    reconciliation.  Such a value must retain Focus's writer and runtime
    fences rather than silently turn into permission for another frontend.
    """

    return str(value or "").strip() in {
        BACKEND_THREAD_STATUS_IDLE,
        BACKEND_THREAD_STATUS_NOT_LOADED,
    }

UNSET = object()


class PlanStepState(TypedDict):
    step: str
    status: str


class CancelableTimer(Protocol):
    def cancel(self) -> None: ...


@dataclass(frozen=True, slots=True, eq=False)
class ExecutionPatchTimerTicket:
    """Process-local identity for one exact delayed execution-card patch.

    Equality intentionally remains object identity.  Reconstructing the same
    binding/execution coordinates must never authorize an old callback.
    """

    binding: ChatBindingKey
    thread_id: str
    card_message_id: str
    turn_id: str
    page_attempt_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionPatchTimerRegistration:
    ticket: ExecutionPatchTimerTicket
    timer: CancelableTimer


@dataclass(frozen=True, slots=True, eq=False)
class MirrorWatchdogTicket:
    """Process-local identity for one exact execution mirror watchdog."""

    binding: ChatBindingKey
    thread_id: str
    card_message_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class MirrorWatchdogRegistration:
    ticket: MirrorWatchdogTicket
    timer: CancelableTimer


class RuntimeStateDict(TypedDict):
    active: bool
    working_dir: str
    current_thread_id: str
    current_thread_title: str
    feishu_runtime_state: str
    goal_objective: str
    goal_status: str
    goal_token_budget: int | None
    goal_tokens_used: int
    goal_time_used_seconds: int
    goal_created_at: int
    goal_updated_at: int
    current_turn_id: str
    running: bool
    cancelled: bool
    pending_cancel: bool
    execution_pages: ExecutionPageLedger
    current_execution_kind: str
    current_prompt_message_id: str
    current_prompt_reply_in_thread: bool
    current_actor_open_id: str
    execution_transcript: ExecutionTranscript
    runtime_channel_state: str
    started_at: float
    last_runtime_event_at: float
    last_patch_at: float
    patch_timer_registration: ExecutionPatchTimerRegistration | None
    mirror_watchdog_registration: MirrorWatchdogRegistration | None
    followup_sent: bool
    followup_text: str
    terminal_result_text: str
    awaiting_local_turn_started: bool
    awaiting_attach_status_settle: bool
    approval_policy: str
    permissions_profile_id: str
    model: str
    reasoning_effort: str
    configured_settings: list[str]
    plan_message_id: str
    plan_turn_id: str
    plan_explanation: str
    plan_steps: list[PlanStepState]
    plan_text: str


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
    feishu_runtime_state: str
    approval_policy: str
    permissions_profile_id: str
    model: str
    reasoning_effort: str
    configured_settings: list[str]


@dataclass(frozen=True, slots=True)
class RuntimeSettingsChanged(RuntimeStateCommand):
    approval_policy: Any = UNSET
    permissions_profile_id: Any = UNSET
    model: Any = UNSET
    reasoning_effort: Any = UNSET


@dataclass(frozen=True, slots=True)
class ThreadStateChanged(RuntimeStateCommand):
    working_dir: Any = UNSET
    current_thread_id: Any = UNSET
    current_thread_title: Any = UNSET
    feishu_runtime_state: Any = UNSET


@dataclass(frozen=True, slots=True)
class ThreadGoalStateChanged(RuntimeStateEvent):
    goal_objective: Any = UNSET
    goal_status: Any = UNSET
    goal_token_budget: Any = UNSET
    goal_tokens_used: Any = UNSET
    goal_time_used_seconds: Any = UNSET
    goal_created_at: Any = UNSET
    goal_updated_at: Any = UNSET


@dataclass(frozen=True, slots=True)
class ThreadGoalCleared(RuntimeStateEvent):
    pass


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
    awaiting_attach_status_settle: Any = UNSET
    current_turn_id: Any = UNSET
    execution_pages: Any = UNSET
    current_execution_kind: Any = UNSET
    current_prompt_message_id: Any = UNSET
    current_prompt_reply_in_thread: Any = UNSET
    current_actor_open_id: Any = UNSET
    runtime_channel_state: Any = UNSET
    started_at: Any = UNSET
    last_runtime_event_at: Any = UNSET
    last_patch_at: Any = UNSET
    followup_sent: Any = UNSET
    followup_text: Any = UNSET
    terminal_result_text: Any = UNSET
    patch_timer_registration: Any = UNSET
    mirror_watchdog_registration: Any = UNSET
    reset_transcript: bool = False
    transcript: Any = UNSET
    reply_text: str | None = None


@dataclass(frozen=True, slots=True)
class PlanStateChanged(RuntimeStateEvent):
    clear: bool = False
    plan_message_id: Any = UNSET
    plan_turn_id: Any = UNSET
    plan_explanation: Any = UNSET
    plan_steps: Any = UNSET
    plan_text: Any = UNSET


def _mark_configured_runtime_setting(state: RuntimeStateDict, setting: str) -> None:
    if setting not in CONFIGURABLE_RUNTIME_SETTING_KEYS:
        return
    configured = list(state.get("configured_settings", []))
    if setting not in configured:
        configured.append(setting)
        configured.sort()
    state["configured_settings"] = configured


def apply_runtime_state_message(state: RuntimeStateDict, message: RuntimeStateMessage) -> None:
    match message:
        case BindingActivated(active=active):
            state["active"] = active
        case StoredBindingHydrated(
            working_dir=working_dir,
            current_thread_id=current_thread_id,
            current_thread_title=current_thread_title,
            feishu_runtime_state=feishu_runtime_state,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            model=model,
            reasoning_effort=reasoning_effort,
            configured_settings=configured_settings,
        ):
            state["working_dir"] = working_dir
            state["current_thread_id"] = current_thread_id
            state["current_thread_title"] = current_thread_title
            state["feishu_runtime_state"] = feishu_runtime_state
            state["approval_policy"] = approval_policy
            state["permissions_profile_id"] = permissions_profile_id
            state["model"] = model
            state["reasoning_effort"] = reasoning_effort
            state["configured_settings"] = [
                setting
                for setting in configured_settings
                if setting in CONFIGURABLE_RUNTIME_SETTING_KEYS
            ]
        case RuntimeSettingsChanged(
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            model=model,
            reasoning_effort=reasoning_effort,
        ):
            if approval_policy is not UNSET:
                state["approval_policy"] = approval_policy
                _mark_configured_runtime_setting(state, "approval_policy")
            if permissions_profile_id is not UNSET:
                state["permissions_profile_id"] = permissions_profile_id
                _mark_configured_runtime_setting(state, "permissions_profile_id")
            if model is not UNSET:
                state["model"] = model
                _mark_configured_runtime_setting(state, "model")
            if reasoning_effort is not UNSET:
                state["reasoning_effort"] = reasoning_effort
                _mark_configured_runtime_setting(state, "reasoning_effort")
        case ThreadStateChanged(
            working_dir=working_dir,
            current_thread_id=current_thread_id,
            current_thread_title=current_thread_title,
            feishu_runtime_state=feishu_runtime_state,
        ):
            if working_dir is not UNSET:
                state["working_dir"] = working_dir
            if current_thread_id is not UNSET:
                state["current_thread_id"] = current_thread_id
            if current_thread_title is not UNSET:
                state["current_thread_title"] = current_thread_title
            if feishu_runtime_state is not UNSET:
                state["feishu_runtime_state"] = feishu_runtime_state
        case ThreadGoalStateChanged() as change:
            if change.goal_objective is not UNSET:
                state["goal_objective"] = change.goal_objective
            if change.goal_status is not UNSET:
                state["goal_status"] = change.goal_status
            if change.goal_token_budget is not UNSET:
                state["goal_token_budget"] = change.goal_token_budget
            if change.goal_tokens_used is not UNSET:
                state["goal_tokens_used"] = change.goal_tokens_used
            if change.goal_time_used_seconds is not UNSET:
                state["goal_time_used_seconds"] = change.goal_time_used_seconds
            if change.goal_created_at is not UNSET:
                state["goal_created_at"] = change.goal_created_at
            if change.goal_updated_at is not UNSET:
                state["goal_updated_at"] = change.goal_updated_at
        case ThreadGoalCleared():
            state["goal_objective"] = ""
            state["goal_status"] = ""
            state["goal_token_budget"] = None
            state["goal_tokens_used"] = 0
            state["goal_time_used_seconds"] = 0
            state["goal_created_at"] = 0
            state["goal_updated_at"] = 0
        case ExecutionAnchorCleared(clear_card_message=clear_card_message):
            if clear_card_message:
                state["execution_pages"] = state[
                    "execution_pages"
                ].clear_known_pages()
            state["current_turn_id"] = ""
            state["current_execution_kind"] = ""
            state["current_prompt_message_id"] = ""
            state["current_prompt_reply_in_thread"] = False
            state["current_actor_open_id"] = ""
            state["awaiting_local_turn_started"] = False
            state["awaiting_attach_status_settle"] = False
        case ExecutionRetired(runtime_channel_state=runtime_channel_state):
            state["execution_pages"] = state[
                "execution_pages"
            ].retire_for_turn_completion(
                cursor_end=ExecutionTranscriptCursor.from_transcript(
                    state["execution_transcript"]
                )
            )
            apply_runtime_state_message(
                state,
                ExecutionAnchorCleared(clear_card_message=False),
            )
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
            if change.awaiting_attach_status_settle is not UNSET:
                state["awaiting_attach_status_settle"] = change.awaiting_attach_status_settle
            if change.current_turn_id is not UNSET:
                state["current_turn_id"] = change.current_turn_id
            if change.execution_pages is not UNSET:
                if type(change.execution_pages) is not ExecutionPageLedger:
                    raise TypeError("execution_pages must be ExecutionPageLedger")
                state["execution_pages"] = change.execution_pages
            if change.current_execution_kind is not UNSET:
                state["current_execution_kind"] = change.current_execution_kind
            if change.current_prompt_message_id is not UNSET:
                state["current_prompt_message_id"] = change.current_prompt_message_id
            if change.current_prompt_reply_in_thread is not UNSET:
                state["current_prompt_reply_in_thread"] = change.current_prompt_reply_in_thread
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
            if change.followup_text is not UNSET:
                state["followup_text"] = change.followup_text
            if change.terminal_result_text is not UNSET:
                state["terminal_result_text"] = change.terminal_result_text
            if change.patch_timer_registration is not UNSET:
                state["patch_timer_registration"] = change.patch_timer_registration
            if change.mirror_watchdog_registration is not UNSET:
                state["mirror_watchdog_registration"] = change.mirror_watchdog_registration
            if change.reset_transcript:
                state["execution_transcript"].reset()
            if change.transcript is not UNSET:
                state["execution_transcript"] = change.transcript.clone()
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
