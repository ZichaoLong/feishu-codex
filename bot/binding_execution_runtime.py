"""Closed commands for the remaining binding execution transitions.

The binding manager owns resident state and persistence.  This owner is the
only boundary used by orchestration code for the prompt/compact start path and
the small number of lifecycle projections that still use
``TurnExecutionCoordinator``.  Raw state never leaves a method in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Protocol, cast

from bot.binding_identity import ChatBindingKey, format_binding_id
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    ExecutionStateChanged,
    RuntimeStateDict,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class BindingExecutionRuntimeChanged(RuntimeError):
    """The exact resident execution capability is no longer current."""


class BindingExecutionRuntimeManager(Protocol):
    """Manager-owned operations available inside this closed trust zone."""

    def binding_session_inventory_locked(
        self,
    ) -> tuple[BindingSessionSnapshot, ...]: ...

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...

    def resident_runtime_state_locked(
        self,
        binding: ChatBindingKey,
    ) -> RuntimeStateDict | None: ...

    def persist_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...


@dataclass(frozen=True, slots=True)
class PrimePromptExecutionCommand:
    session: BindingSessionSnapshot
    prompt_message_id: str
    prompt_reply_in_thread: bool
    actor_open_id: str
    started_at: float
    awaiting_attach_status_settle: bool


@dataclass(frozen=True, slots=True)
class PrimeCompactExecutionCommand:
    session: BindingSessionSnapshot
    prompt_message_id: str
    prompt_reply_in_thread: bool
    actor_open_id: str
    started_at: float


@dataclass(frozen=True, slots=True)
class ActiveObserverSnapshotItem:
    item_type: str
    text: str
    text_available: bool


@dataclass(frozen=True, slots=True)
class PrimeActiveObserverExecutionCommand:
    session: BindingSessionSnapshot
    turn_id: str
    reply_items: tuple[ActiveObserverSnapshotItem, ...]
    started_at: float


@dataclass(frozen=True, slots=True)
class RollbackDetachedActiveObserverExecutionCommand:
    session: BindingSessionSnapshot
    turn_id: str


@dataclass(frozen=True, slots=True)
class RecordBindingExecutionStartFailureCommand:
    target: BindingExecutionTarget
    error_text: str


@dataclass(frozen=True, slots=True)
class RetireBindingExecutionCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class RecordBindingStartedTurnCommand:
    target: BindingExecutionTarget
    turn_id: str


@dataclass(frozen=True, slots=True)
class RecordBindingStartedTurnResult:
    committed: bool
    should_interrupt: bool
    session: BindingSessionSnapshot | None = None


@dataclass(frozen=True, slots=True)
class UpdateBindingCancelCommand:
    target: BindingExecutionTarget


@dataclass(frozen=True, slots=True)
class InterruptBindingExecutionCommand:
    session: BindingSessionSnapshot
    process_note: str


@dataclass(frozen=True, slots=True)
class InterruptedBindingExecution:
    binding_id: str
    session: BindingSessionSnapshot


@dataclass(frozen=True, slots=True)
class PrepareBindingDisconnectCommand:
    error_message: str


class BindingExecutionRuntimeTransitions:
    """Own residual execution mutation and return immutable results only."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: BindingExecutionRuntimeManager,
        turn_execution: TurnExecutionCoordinator,
    ) -> None:
        if not isinstance(turn_execution, TurnExecutionCoordinator):
            raise TypeError("binding execution runtime requires the execution reducer")
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._turn_execution = turn_execution

    def persist_session(
        self,
        captured: BindingSessionSnapshot,
    ) -> BindingSessionSnapshot:
        self._require_session(captured)
        with self._lock:
            current, _state = self._require_session_locked(captured)
            if current.current_thread_id != captured.current_thread_id:
                raise BindingExecutionRuntimeChanged(
                    "binding session changed before persistence"
                )
            return self._binding_runtime.persist_session_locked(captured.handle)

    def prime_prompt_execution(
        self,
        command: PrimePromptExecutionCommand,
    ) -> BindingSessionSnapshot:
        self._require_session(command.session)
        self._require_exact_string(
            command.prompt_message_id,
            field="prompt message id",
        )
        self._require_exact_bool(
            command.prompt_reply_in_thread,
            field="prompt reply-in-thread",
        )
        self._require_exact_string(command.actor_open_id, field="prompt actor")
        self._require_time(command.started_at, field="prompt started_at")
        self._require_exact_bool(
            command.awaiting_attach_status_settle,
            field="prompt attach-settlement fact",
        )
        with self._lock:
            current, state = self._require_unchanged_session_locked(
                command.session
            )
            self._require_execution_idle(current)
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.prime_prompt_turn_locked(
                    state,
                    prompt_message_id=command.prompt_message_id,
                    prompt_reply_in_thread=command.prompt_reply_in_thread,
                    actor_open_id=command.actor_open_id,
                    started_at=command.started_at,
                    awaiting_attach_status_settle=(
                        command.awaiting_attach_status_settle
                    ),
                )
                self._turn_execution.clear_plan_state_locked(state)
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise
            return self._updated_session_locked(command.session.handle)

    def prime_compact_execution(
        self,
        command: PrimeCompactExecutionCommand,
    ) -> BindingSessionSnapshot:
        self._require_session(command.session)
        self._require_exact_string(
            command.prompt_message_id,
            field="compact prompt message id",
        )
        self._require_exact_bool(
            command.prompt_reply_in_thread,
            field="compact reply-in-thread",
        )
        self._require_exact_string(command.actor_open_id, field="compact actor")
        self._require_time(command.started_at, field="compact started_at")
        with self._lock:
            current, state = self._require_unchanged_session_locked(
                command.session
            )
            self._require_execution_idle(current)
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.prime_prompt_turn_locked(
                    state,
                    prompt_message_id=command.prompt_message_id,
                    prompt_reply_in_thread=command.prompt_reply_in_thread,
                    actor_open_id=command.actor_open_id,
                    started_at=command.started_at,
                    awaiting_attach_status_settle=False,
                    execution_kind="compact",
                )
                self._turn_execution.clear_plan_state_locked(state)
                self._turn_execution.append_process_note_locked(
                    state,
                    text="正在压缩上下文。",
                    marks_work=True,
                )
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise
            return self._updated_session_locked(command.session.handle)

    def prime_active_observer_execution(
        self,
        command: PrimeActiveObserverExecutionCommand,
    ) -> BindingSessionSnapshot:
        """Stage one observer anchor while its binding remains detached."""

        self._require_session(command.session)
        self._require_exact_string(command.turn_id, field="observer turn id")
        if not command.turn_id.strip():
            raise ValueError("observer turn id must not be empty")
        if type(command.reply_items) is not tuple or any(
            type(item) is not ActiveObserverSnapshotItem
            for item in command.reply_items
        ):
            raise TypeError("observer reply items must be an exact typed tuple")
        for item in command.reply_items:
            self._require_exact_string(item.item_type, field="observer item type")
            self._require_exact_string(item.text, field="observer item text")
            self._require_exact_bool(
                item.text_available,
                field="observer item text availability",
            )
        self._require_time(command.started_at, field="observer started_at")
        with self._lock:
            current, state = self._require_unchanged_session_locked(
                command.session
            )
            self._require_execution_idle(current)
            if (
                current.thread.feishu_runtime_state != FEISHU_RUNTIME_DETACHED
                or not current.current_thread_id.strip()
            ):
                raise BindingExecutionRuntimeChanged(
                    "active observer staging requires a detached direct-root binding"
                )
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.prime_prompt_turn_locked(
                    state,
                    prompt_message_id="",
                    prompt_reply_in_thread=False,
                    actor_open_id="",
                    started_at=command.started_at,
                    awaiting_attach_status_settle=False,
                    execution_kind=ACTIVE_OBSERVER_EXECUTION_KIND,
                )
                self._turn_execution.record_started_turn_id_locked(
                    state,
                    turn_id=command.turn_id,
                )
                self._turn_execution.acknowledge_running_snapshot_locked(
                    state,
                    occurred_at=command.started_at,
                )
                self._turn_execution.append_process_note_locked(
                    state,
                    text=(
                        "已在本轮执行开始后接入；此前的执行过程可能不完整。"
                    ),
                    marks_work=True,
                )
                self._turn_execution.apply_snapshot_reply_locked(
                    state,
                    reply_text="",
                    reply_items=[
                        {
                            "type": item.item_type,
                            **(
                                {"text": item.text}
                                if item.text_available
                                else {}
                            ),
                        }
                        for item in command.reply_items
                    ],
                )
                self._turn_execution.clear_plan_state_locked(state)
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise
            return self._updated_session_locked(command.session.handle)

    def rollback_detached_active_observer_execution(
        self,
        command: RollbackDetachedActiveObserverExecutionCommand,
    ) -> BindingSessionSnapshot:
        """Remove a hidden staged anchor after durable attach failed."""

        self._require_session(command.session)
        self._require_exact_string(command.turn_id, field="observer turn id")
        if not command.turn_id.strip():
            raise ValueError("observer turn id must not be empty")
        with self._lock:
            current, state = self._require_session_locked(command.session)
            if current.current_thread_id != command.session.current_thread_id:
                raise BindingExecutionRuntimeChanged(
                    "active observer rollback found a replaced binding"
                )
            if current.thread.feishu_runtime_state != FEISHU_RUNTIME_DETACHED:
                raise BindingExecutionRuntimeChanged(
                    "active observer rollback requires a detached binding"
                )
            if not current.running and not current.execution.has_execution_anchor:
                return current
            if (
                current.execution.current_execution_kind
                != ACTIVE_OBSERVER_EXECUTION_KIND
                or current.execution.current_turn_id != command.turn_id
            ):
                raise BindingExecutionRuntimeChanged(
                    "active observer rollback found a different execution"
                )
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.reset_execution_context_locked(
                    state,
                    clear_card_message=True,
                )
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise
            return self._updated_session_locked(command.session.handle)

    def record_start_failure(
        self,
        command: RecordBindingExecutionStartFailureCommand,
    ) -> BindingSessionSnapshot | None:
        self._require_exact_string(command.error_text, field="start failure text")
        with self._lock:
            required = self._target_state_locked(command.target)
            if required is None:
                return None
            _current, state = required
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.record_start_failure_locked(
                    state,
                    error_text=command.error_text,
                )
                self._turn_execution.apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(
                        awaiting_local_turn_started=False,
                        current_turn_id="",
                    ),
                )
                return self._binding_runtime.persist_session_locked(
                    command.target.handle
                )
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise

    def retire_execution(
        self,
        command: RetireBindingExecutionCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._target_state_locked(command.target)
            if required is None:
                return None
            _current, state = required
            rollback = self._capture_runtime_state(state)
            try:
                if not self._turn_execution.retire_execution_locked(state):
                    return None
                return self._binding_runtime.persist_session_locked(
                    command.target.handle
                )
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise

    def record_started_turn(
        self,
        command: RecordBindingStartedTurnCommand,
    ) -> RecordBindingStartedTurnResult:
        self._require_exact_string(command.turn_id, field="started turn id")
        with self._lock:
            required = self._target_state_locked(command.target)
            if required is None:
                return RecordBindingStartedTurnResult(
                    committed=False,
                    should_interrupt=bool(command.turn_id.strip()),
                )
            _current, state = required
            should_interrupt = (
                self._turn_execution.record_started_turn_id_locked(
                    state,
                    turn_id=command.turn_id,
                )
            )
            return RecordBindingStartedTurnResult(
                committed=True,
                should_interrupt=should_interrupt,
                session=self._updated_session_locked(command.target.handle),
            )

    def mark_cancel_pending(
        self,
        command: UpdateBindingCancelCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._target_state_locked(command.target)
            if required is None:
                return None
            _current, state = required
            self._turn_execution.mark_cancel_pending_locked(state)
            return self._updated_session_locked(command.target.handle)

    def clear_cancel_pending(
        self,
        command: UpdateBindingCancelCommand,
    ) -> BindingSessionSnapshot | None:
        with self._lock:
            required = self._target_state_locked(command.target)
            if required is None:
                return None
            _current, state = required
            self._turn_execution.clear_cancel_pending_locked(state)
            return self._updated_session_locked(command.target.handle)

    def interrupt_for_backend_reset(
        self,
        command: InterruptBindingExecutionCommand,
    ) -> InterruptedBindingExecution | None:
        self._require_session(command.session)
        self._require_exact_string(command.process_note, field="reset process note")
        target = BindingExecutionTarget.from_session(command.session)
        with self._lock:
            required = self._target_state_locked(target)
            if required is None:
                raise BindingExecutionRuntimeChanged(
                    "backend reset binding session was replaced: "
                    f"{format_binding_id(command.session.binding)}"
                )
            _current, state = required
            if not self._turn_execution.has_active_execution_locked(state):
                return None
            rollback = self._capture_runtime_state(state)
            try:
                self._turn_execution.append_process_note_locked(
                    state,
                    text=command.process_note,
                    marks_work=True,
                )
                self._turn_execution.apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(
                        cancelled=True,
                        pending_cancel=False,
                        runtime_channel_state="live",
                    ),
                )
                updated = self._updated_session_locked(command.session.handle)
            except BaseException:
                self._restore_runtime_state(state, rollback)
                raise
            return InterruptedBindingExecution(
                binding_id=format_binding_id(command.session.binding),
                session=updated,
            )

    def prepare_disconnect(
        self,
        command: PrepareBindingDisconnectCommand,
    ) -> tuple[ChatBindingKey, ...]:
        self._require_exact_string(
            command.error_message,
            field="disconnect error message",
        )
        affected: list[ChatBindingKey] = []
        with self._lock:
            for session in self._binding_runtime.binding_session_inventory_locked():
                if (
                    session.thread.feishu_runtime_state
                    != FEISHU_RUNTIME_ATTACHED
                    or not session.current_thread_id
                ):
                    continue
                state = self._binding_runtime.resident_runtime_state_locked(
                    session.binding
                )
                if state is None:
                    raise BindingExecutionRuntimeChanged(
                        "disconnect projection lost resident binding: "
                        f"{format_binding_id(session.binding)}"
                    )
                affected.append(session.binding)
                if self._turn_execution.has_active_execution_locked(state):
                    self._turn_execution.apply_terminal_error_locked(
                        state,
                        error_message=command.error_message,
                    )
        return tuple(affected)

    @staticmethod
    def _capture_runtime_state(state: RuntimeStateDict) -> RuntimeStateDict:
        captured = dict(state)
        captured["execution_transcript"] = state[
            "execution_transcript"
        ].clone()
        captured["configured_settings"] = list(state["configured_settings"])
        captured["plan_steps"] = list(state["plan_steps"])
        return cast(RuntimeStateDict, captured)

    @staticmethod
    def _restore_runtime_state(
        state: RuntimeStateDict,
        captured: RuntimeStateDict,
    ) -> None:
        state.clear()
        state.update(captured)

    def _target_state_locked(
        self,
        target: BindingExecutionTarget,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict] | None:
        if type(target) is not BindingExecutionTarget:
            raise TypeError("binding execution command requires an exact target")
        try:
            current = self._binding_runtime.session_snapshot_locked(target.handle)
        except RuntimeError:
            return None
        if not target.matches(current):
            return None
        state = self._binding_runtime.resident_runtime_state_locked(target.binding)
        if state is None:
            return None
        return current, state

    def _require_unchanged_session_locked(
        self,
        captured: BindingSessionSnapshot,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict]:
        current, state = self._require_session_locked(captured)
        if current != captured:
            raise BindingExecutionRuntimeChanged(
                "binding session business facts changed"
            )
        return current, state

    def _require_session_locked(
        self,
        captured: BindingSessionSnapshot,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict]:
        try:
            current = self._binding_runtime.session_snapshot_locked(
                captured.handle
            )
        except RuntimeError as exc:
            raise BindingExecutionRuntimeChanged(
                "binding session was retired or replaced"
            ) from exc
        state = self._binding_runtime.resident_runtime_state_locked(
            captured.binding
        )
        if state is None or current.handle is not captured.handle:
            raise BindingExecutionRuntimeChanged(
                "binding session no longer has an exact resident"
            )
        return current, state

    def _updated_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        return self._binding_runtime.session_snapshot_locked(handle)

    @staticmethod
    def _require_session(value: object) -> None:
        if type(value) is not BindingSessionSnapshot:
            raise TypeError("binding execution command requires an exact session")

    @staticmethod
    def _require_execution_idle(session: BindingSessionSnapshot) -> None:
        if session.running or session.execution.has_execution_anchor:
            raise BindingExecutionRuntimeChanged(
                "binding execution is no longer idle"
            )

    @staticmethod
    def _require_exact_string(value: object, *, field: str) -> None:
        if type(value) is not str:
            raise TypeError(f"{field} must be an exact string")

    @staticmethod
    def _require_exact_bool(value: object, *, field: str) -> None:
        if type(value) is not bool:
            raise TypeError(f"{field} must be bool")

    @staticmethod
    def _require_time(value: object, *, field: str) -> None:
        if type(value) is not float:
            raise TypeError(f"{field} must be an exact float")
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
