"""Exact binding-runtime transitions for adapter notifications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Literal, Protocol

from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.binding_runtime_lifecycle import (
    BindingRuntimeLifecycleTransitions,
    RuntimeTimerCancellationEffect,
)
from bot.execution_pages import ExecutionTranscriptCursor
from bot.execution_transcript import (
    ExecutionTranscriptSnapshot,
    is_terminal_invalidating_work_item_type,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_SYSTEM_ERROR,
    ExecutionStateChanged,
    RuntimeStateDict,
    ThreadGoalCleared,
    ThreadGoalStateChanged,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class AdapterNotificationBindingRuntime(Protocol):
    """Exact resident operations consumed by the notification owner."""

    def resident_session_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None: ...

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...

    def resident_runtime_state_locked(
        self,
        binding: ChatBindingKey,
    ) -> RuntimeStateDict | None: ...

    def update_thread_metadata_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        expected_thread_id: str,
        current_thread_title: str,
    ) -> BindingSessionSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class ThreadRuntimeEventCommand:
    target: BindingExecutionTarget
    thread_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeEventCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class TurnStartedRuntimeEventCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class ItemStartedRuntimeEventCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    item_type: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class ErrorNotificationCommand:
    target: BindingExecutionTarget
    message: str
    will_retry: bool


@dataclass(frozen=True, slots=True)
class ThreadStatusNotificationCommand:
    target: BindingExecutionTarget
    status_type: str


@dataclass(frozen=True, slots=True)
class ThreadClosedNotificationCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class ThreadTitleNotificationCommand:
    target: BindingExecutionTarget
    title: str


@dataclass(frozen=True, slots=True)
class ThreadGoalNotificationCommand:
    target: BindingExecutionTarget
    objective: str
    status: str
    token_budget: int | None
    tokens_used: int
    time_used_seconds: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ThreadGoalClearedNotificationCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class TurnStartedNotificationCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    started_at: float


@dataclass(frozen=True, slots=True)
class RestoreCancelPendingCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class NotificationPlanStep:
    step: str
    status: str


@dataclass(frozen=True, slots=True)
class PlanOutlineNotificationCommand:
    target: BindingExecutionTarget
    turn_id: str
    explanation: str
    steps: tuple[NotificationPlanStep, ...]


@dataclass(frozen=True, slots=True)
class ProcessItemStartedCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    item_type: str
    text: str
    started_at: float


@dataclass(frozen=True, slots=True)
class WorkItemStartedCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    item_type: str
    text: str
    started_at: float


@dataclass(frozen=True, slots=True)
class AssistantDeltaNotificationCommand:
    target: BindingExecutionTarget
    delta: str


@dataclass(frozen=True, slots=True)
class MarkProcessWorkCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class FinishProcessBlockCommand:
    target: BindingExecutionTarget
    suffix: str = ""
    marks_work: bool = False


@dataclass(frozen=True, slots=True)
class ReconcileAssistantTextCommand:
    target: BindingExecutionTarget
    text: str
    terminal_candidate: bool = True
    item_id: str = ""


@dataclass(frozen=True, slots=True)
class RecordUnavailableAssistantCompletionCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class PlanTextNotificationCommand:
    target: BindingExecutionTarget
    turn_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TurnCompletedNotificationCommand:
    target: BindingExecutionTarget
    status: str
    error_message: str


@dataclass(frozen=True, slots=True)
class RememberTerminalResultTextCommand:
    target: BindingExecutionTarget
    execution_message_id: str
    text: str


@dataclass(frozen=True, slots=True)
class PreviousExecutionCardEffect:
    message_id: str
    transcript: ExecutionTranscriptSnapshot
    cursor_start: ExecutionTranscriptCursor
    cursor_end: ExecutionTranscriptCursor
    elapsed: int
    cancelled: bool


@dataclass(frozen=True, slots=True)
class TurnStartedNotificationTransition:
    session: BindingSessionSnapshot
    reuse_existing_card: bool
    previous_execution_card: PreviousExecutionCardEffect | None
    should_interrupt_started_turn: bool


@dataclass(frozen=True, slots=True)
class ItemStartedNotificationTransition:
    session: BindingSessionSnapshot
    should_interrupt_started_turn: bool


ThreadLifecycleAction = Literal[
    "none",
    "finalize",
    "schedule_execution_card",
    "flush_execution_card",
]


@dataclass(frozen=True, slots=True)
class ThreadLifecycleNotificationTransition:
    session: BindingSessionSnapshot
    action: ThreadLifecycleAction
    turn_id: str = ""
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


class AdapterNotificationRuntimeTransitions:
    """Own exact notification mutations; never retain raw state across locks."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: AdapterNotificationBindingRuntime,
        turn_execution: TurnExecutionCoordinator,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._turn_execution = turn_execution
        self._lifecycle = BindingRuntimeLifecycleTransitions(
            turn_execution=turn_execution
        )

    def resident_session(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            return self._binding_runtime.resident_session_snapshot_locked(binding)

    def current_session(
        self,
        target: BindingExecutionTarget,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(target)
            return required[0] if required is not None else None

    def mark_thread_runtime_event(
        self,
        command: ThreadRuntimeEventCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if session.current_thread_id.strip() != command.thread_id:
                return None
            self._turn_execution.mark_runtime_event_locked(
                state,
                occurred_at=command.occurred_at,
            )
            return self._updated_session_locked(command.target.handle)

    def mark_execution_runtime_event(
        self,
        command: ExecutionRuntimeEventCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not self._current_execution_matches(
                session,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
            ):
                return None
            self._turn_execution.mark_runtime_event_locked(
                state,
                occurred_at=command.occurred_at,
            )
            return self._updated_session_locked(command.target.handle)

    def mark_turn_started_runtime_event(
        self,
        command: TurnStartedRuntimeEventCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not self._turn_started_matches(
                session,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
            ):
                return None
            self._turn_execution.mark_runtime_event_locked(
                state,
                occurred_at=command.occurred_at,
            )
            return self._updated_session_locked(command.target.handle)

    def mark_item_started_runtime_event(
        self,
        command: ItemStartedRuntimeEventCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not self._item_started_matches(
                session,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
                item_type=command.item_type,
            ):
                return None
            self._turn_execution.mark_runtime_event_locked(
                state,
                occurred_at=command.occurred_at,
            )
            return self._updated_session_locked(command.target.handle)

    def apply_error(
        self,
        command: ErrorNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            if command.will_retry:
                self._turn_execution.append_process_note_locked(
                    state,
                    text=f"\n[重试中] {command.message}\n",
                    marks_work=True,
                )
            else:
                self._turn_execution.apply_terminal_error_locked(
                    state,
                    error_message=command.message,
                )
            return self._updated_session_locked(command.target.handle)

    def apply_thread_status(
        self,
        command: ThreadStatusNotificationCommand,
    ) -> ThreadLifecycleNotificationTransition | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            awaiting_started = (
                self._turn_execution.awaiting_upstream_turn_started_locked(state)
            )
            if (
                command.status_type == BACKEND_THREAD_STATUS_ACTIVE
                and not awaiting_started
            ):
                self._turn_execution.acknowledge_active_thread_locked(state)
            if awaiting_started:
                action: ThreadLifecycleAction = "none"
            elif command.status_type != BACKEND_THREAD_STATUS_ACTIVE and (
                session.execution.current_turn_id
                or session.execution.current_message_id
            ):
                action = (
                    "none"
                    if command.status_type == BACKEND_THREAD_STATUS_SYSTEM_ERROR
                    else "finalize"
                )
            elif command.status_type == BACKEND_THREAD_STATUS_ACTIVE:
                action = "schedule_execution_card"
            else:
                self._turn_execution.settle_non_active_thread_locked(state)
                timer_cancellations = self._lifecycle.detach_timers_locked(
                    command.target.binding,
                    state,
                    patch=False,
                    mirror=True,
                )
                action = "flush_execution_card"
            if action != "flush_execution_card":
                timer_cancellations = ()
            updated = self._updated_session_locked(command.target.handle)
            return ThreadLifecycleNotificationTransition(
                session=updated,
                action=action,
                turn_id=session.execution.current_turn_id,
                timer_cancellations=timer_cancellations,
            )

    def apply_thread_closed(
        self,
        command: ThreadClosedNotificationCommand,
    ) -> ThreadLifecycleNotificationTransition | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            awaiting_started = (
                self._turn_execution.awaiting_upstream_turn_started_locked(state)
            )
            if awaiting_started:
                action: ThreadLifecycleAction = "none"
            elif (
                session.running
                or session.execution.current_turn_id
                or session.execution.current_message_id
            ):
                action = "finalize"
            else:
                self._turn_execution.settle_thread_closed_locked(state)
                timer_cancellations = self._lifecycle.detach_timers_locked(
                    command.target.binding,
                    state,
                    patch=False,
                    mirror=True,
                )
                action = "none"
            if action != "none" or awaiting_started:
                timer_cancellations = ()
            updated = self._updated_session_locked(command.target.handle)
            return ThreadLifecycleNotificationTransition(
                session=updated,
                action=action,
                turn_id=session.execution.current_turn_id,
                timer_cancellations=timer_cancellations,
            )

    def apply_thread_title(
        self,
        command: ThreadTitleNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            return self._binding_runtime.update_thread_metadata_locked(
                command.target.handle,
                expected_thread_id=session.current_thread_id,
                current_thread_title=command.title,
            )

    def apply_thread_goal(
        self,
        command: ThreadGoalNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ThreadGoalStateChanged(
                    goal_objective=command.objective,
                    goal_status=command.status,
                    goal_token_budget=command.token_budget,
                    goal_tokens_used=command.tokens_used,
                    goal_time_used_seconds=command.time_used_seconds,
                    goal_created_at=command.created_at,
                    goal_updated_at=command.updated_at,
                ),
            )
            return self._updated_session_locked(command.target.handle)

    def clear_thread_goal(
        self,
        command: ThreadGoalClearedNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ThreadGoalCleared(),
            )
            return self._updated_session_locked(command.target.handle)

    def apply_turn_started(
        self,
        command: TurnStartedNotificationCommand,
    ) -> TurnStartedNotificationTransition | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not self._turn_started_matches(
                session,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
            ):
                return None
            prepared = self._turn_execution.prepare_turn_started_locked(
                state,
                turn_id=command.turn_id,
                started_at=command.started_at,
            )
            self._turn_execution.clear_plan_state_locked(state)
            previous = prepared.previous_execution_card
            previous_effect = (
                PreviousExecutionCardEffect(
                    message_id=previous.message_id,
                    transcript=previous.transcript.snapshot(),
                    cursor_start=previous.cursor_start,
                    cursor_end=previous.cursor_end,
                    elapsed=previous.elapsed,
                    cancelled=previous.cancelled,
                )
                if previous is not None
                else None
            )
            return TurnStartedNotificationTransition(
                session=self._updated_session_locked(command.target.handle),
                reuse_existing_card=prepared.reuse_existing_card,
                previous_execution_card=previous_effect,
                should_interrupt_started_turn=(
                    prepared.should_interrupt_started_turn
                ),
            )

    def restore_cancel_pending(
        self,
        command: RestoreCancelPendingCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.mark_cancel_pending_locked(state)
            return self._updated_session_locked(command.target.handle)

    def apply_plan_outline(
        self,
        command: PlanOutlineNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            changed = self._turn_execution.update_plan_outline_locked(
                state,
                turn_id=command.turn_id,
                explanation=command.explanation,
                plan=[
                    {"step": step.step, "status": step.status}
                    for step in command.steps
                ],
            )
            if not changed:
                return None
            return self._updated_session_locked(command.target.handle)

    def start_process_item(
        self,
        command: ProcessItemStartedCommand,
    ) -> ItemStartedNotificationTransition | None:
        with self._lock:
            prepared = self._prepare_item_started_locked(
                command.target,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
                item_type=command.item_type,
                started_at=command.started_at,
            )
            if prepared is None:
                return None
            state, should_interrupt = prepared
            self._turn_execution.start_process_block_locked(
                state,
                text=command.text,
                marks_work=True,
            )
            return ItemStartedNotificationTransition(
                session=self._updated_session_locked(command.target.handle),
                should_interrupt_started_turn=should_interrupt,
            )

    def start_work_item(
        self,
        command: WorkItemStartedCommand,
    ) -> ItemStartedNotificationTransition | None:
        with self._lock:
            prepared = self._prepare_item_started_locked(
                command.target,
                thread_id=command.thread_id,
                turn_id=command.turn_id,
                item_type=command.item_type,
                started_at=command.started_at,
            )
            if prepared is None:
                return None
            state, should_interrupt = prepared
            self._turn_execution.append_process_note_locked(
                state,
                text=command.text,
                marks_work=is_terminal_invalidating_work_item_type(
                    command.item_type
                ),
            )
            return ItemStartedNotificationTransition(
                session=self._updated_session_locked(command.target.handle),
                should_interrupt_started_turn=should_interrupt,
            )

    def append_assistant_delta(
        self,
        command: AssistantDeltaNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.append_assistant_delta_locked(
                state,
                delta=command.delta,
            )
            return self._updated_session_locked(command.target.handle)

    def mark_process_work(
        self,
        command: MarkProcessWorkCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.mark_process_work_locked(state)
            return self._updated_session_locked(command.target.handle)

    def finish_process_block(
        self,
        command: FinishProcessBlockCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.finish_process_block_locked(
                state,
                suffix=command.suffix,
                marks_work=command.marks_work,
            )
            return self._updated_session_locked(command.target.handle)

    def reconcile_assistant_text(
        self,
        command: ReconcileAssistantTextCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.reconcile_current_assistant_text_locked(
                state,
                text=command.text,
                terminal_candidate=command.terminal_candidate,
                item_id=command.item_id,
            )
            return self._updated_session_locked(command.target.handle)

    def record_unavailable_assistant_completion(
        self,
        command: RecordUnavailableAssistantCompletionCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.record_unavailable_assistant_completion_locked(
                state
            )
            return self._updated_session_locked(command.target.handle)

    def apply_plan_text(
        self,
        command: PlanTextNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            changed = self._turn_execution.update_plan_text_locked(
                state,
                turn_id=command.turn_id,
                text=command.text,
            )
            if not changed:
                return None
            return self._updated_session_locked(command.target.handle)

    def apply_turn_completed(
        self,
        command: TurnCompletedNotificationCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            self._turn_execution.apply_turn_completed_locked(
                state,
                status=command.status,
                error_message=command.error_message,
            )
            return self._updated_session_locked(command.target.handle)

    def remember_terminal_result_text(
        self,
        command: RememberTerminalResultTextCommand,
    ) -> BindingSessionSnapshot | None:
        message_id = str(command.execution_message_id or "").strip()
        text = str(command.text or "")
        if not message_id or not text:
            return None
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if session.execution.current_message_id.strip() != message_id:
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(terminal_result_text=text),
            )
            return self._updated_session_locked(command.target.handle)

    def _prepare_item_started_locked(
        self,
        target: BindingExecutionTarget,
        *,
        thread_id: str,
        turn_id: str,
        item_type: str,
        started_at: float,
    ) -> tuple[RuntimeStateDict, bool] | None:
        required = self._require_target_locked(target)
        if required is None:
            return None
        session, state = required
        if self._current_execution_matches(
            session,
            thread_id=thread_id,
            turn_id=turn_id,
        ):
            return state, False
        if not self._can_bind_unbound_compact_item_started(
            session,
            thread_id=thread_id,
            turn_id=turn_id,
            item_type=item_type,
        ):
            return None
        transition = self._turn_execution.prepare_turn_started_locked(
            state,
            turn_id=turn_id,
            started_at=started_at,
        )
        self._turn_execution.clear_plan_state_locked(state)
        return state, transition.should_interrupt_started_turn

    def _require_target_locked(
        self,
        target: BindingExecutionTarget,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict] | None:
        if type(target) is not BindingExecutionTarget:
            raise TypeError(
                "adapter notification transition requires a typed target"
            )
        try:
            session = self._binding_runtime.session_snapshot_locked(target.handle)
        except RuntimeError:
            return None
        if not target.matches(session):
            return None
        state = self._binding_runtime.resident_runtime_state_locked(target.binding)
        if state is None:
            return None
        return session, state

    def _updated_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        return self._binding_runtime.session_snapshot_locked(handle)

    @staticmethod
    def _current_execution_matches(
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        return bool(
            session.current_thread_id.strip() == thread_id
            and bool(str(turn_id or "").strip())
            and session.execution.current_turn_id.strip()
            == str(turn_id or "").strip()
        )

    @staticmethod
    def _turn_started_matches(
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        normalized_turn_id = str(turn_id or "").strip()
        current_turn_id = session.execution.current_turn_id.strip()
        return bool(
            session.current_thread_id.strip() == thread_id
            and normalized_turn_id
            and (not current_turn_id or current_turn_id == normalized_turn_id)
        )

    @classmethod
    def _item_started_matches(
        cls,
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str,
        item_type: str,
    ) -> bool:
        return cls._current_execution_matches(
            session,
            thread_id=thread_id,
            turn_id=turn_id,
        ) or cls._can_bind_unbound_compact_item_started(
            session,
            thread_id=thread_id,
            turn_id=turn_id,
            item_type=item_type,
        )

    @staticmethod
    def _can_bind_unbound_compact_item_started(
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str,
        item_type: str,
    ) -> bool:
        execution = session.execution
        return bool(
            item_type == "contextCompaction"
            and str(turn_id or "").strip()
            and session.current_thread_id.strip() == thread_id
            and execution.current_message_id.strip()
            and execution.awaiting_local_turn_started
            and not execution.current_turn_id.strip()
            and execution.current_execution_kind.strip() == "compact"
        )
