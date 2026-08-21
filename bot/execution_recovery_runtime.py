"""Exact binding-runtime transitions for execution recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Literal, Protocol, TypeAlias

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
from bot.execution_pages import (
    ExecutionTranscriptCursor,
    TerminalExecutionPageReceipt,
    require_terminal_execution_page_receipts,
)
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.runtime_state import (
    ExecutionStateChanged,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
    RuntimeStateDict,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class ExecutionRecoveryBindingRuntime(Protocol):
    """Exact resident operations consumed by the recovery transition owner."""

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...

    def resident_session_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None: ...

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
        working_dir: str,
    ) -> BindingSessionSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class MirrorWatchdogTarget:
    """Execution fence plus the exact watchdog registration fact."""

    execution: BindingExecutionTarget
    expected_registered: bool

    def __post_init__(self) -> None:
        if type(self.execution) is not BindingExecutionTarget:
            raise TypeError(
                "mirror watchdog target requires an exact execution target"
            )
        if type(self.expected_registered) is not bool:
            raise TypeError("mirror watchdog registered fact must be bool")

    @classmethod
    def from_session(
        cls,
        session: BindingSessionSnapshot,
    ) -> MirrorWatchdogTarget:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError("mirror watchdog target requires an exact session")
        return cls(
            execution=BindingExecutionTarget.from_session(session),
            expected_registered=session.execution.mirror_watchdog_registered,
        )

    @property
    def handle(self) -> BindingRuntimeHandle:
        return self.execution.handle

    @property
    def binding(self) -> ChatBindingKey:
        return self.execution.binding

    def matches(self, session: BindingSessionSnapshot) -> bool:
        return bool(
            self.execution.matches(session)
            and session.execution.mirror_watchdog_registered
            is self.expected_registered
        )


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeObservationFence:
    """Exact online-observation revision frozen before recovery I/O."""

    last_runtime_event_at: float

    def __post_init__(self) -> None:
        if type(self.last_runtime_event_at) is not float:
            raise TypeError(
                "execution observation timestamp must be an exact float"
            )
        if self.last_runtime_event_at < 0:
            raise ValueError(
                "execution observation timestamp must be non-negative"
            )

    @classmethod
    def from_session(
        cls,
        session: BindingSessionSnapshot,
    ) -> ExecutionRuntimeObservationFence:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError(
                "execution observation fence requires an exact session"
            )
        return cls(
            last_runtime_event_at=session.execution.last_runtime_event_at,
        )

    def matches(self, session: BindingSessionSnapshot) -> bool:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError(
                "execution observation fence requires an exact session"
            )
        return (
            session.execution.last_runtime_event_at
            == self.last_runtime_event_at
        )


@dataclass(frozen=True, slots=True)
class PrepareMirrorWatchdogCommand:
    target: MirrorWatchdogTarget
    delay_seconds: float


@dataclass(frozen=True, slots=True)
class MirrorWatchdogInstallPreparation:
    session: BindingSessionSnapshot
    ticket: MirrorWatchdogTicket | None
    delay_seconds: float
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...]


@dataclass(frozen=True, slots=True)
class InstallMirrorWatchdogCommand:
    target: MirrorWatchdogTarget
    registration: MirrorWatchdogRegistration


@dataclass(frozen=True, slots=True)
class RollbackMirrorWatchdogCommand:
    handle: BindingRuntimeHandle
    registration: MirrorWatchdogRegistration


@dataclass(frozen=True, slots=True)
class ConsumeMirrorWatchdogCommand:
    ticket: MirrorWatchdogTicket
    occurred_at: float
    compact_start_timeout_seconds: float


MirrorWatchdogAction: TypeAlias = Literal[
    "reschedule",
    "compact_start_unknown",
    "reconcile",
]


@dataclass(frozen=True, slots=True)
class MirrorWatchdogEffect:
    session: BindingSessionSnapshot
    action: MirrorWatchdogAction
    thread_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class CaptureTerminalReconcileTargetCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class TerminalReconcileTarget:
    """Immutable terminal presentation facts detached from turn ownership."""

    binding: ChatBindingKey
    thread_id: str
    turn_id: str
    card_message_id: str
    prompt_message_id: str
    prompt_reply_in_thread: bool
    transcript: ExecutionTranscriptSnapshot
    cursor_start: ExecutionTranscriptCursor
    cursor_end: ExecutionTranscriptCursor
    cancelled: bool
    elapsed: int
    terminal_page_receipts: tuple[TerminalExecutionPageReceipt, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not tuple
            or len(self.binding) != 2
            or any(type(part) is not str or not part.strip() for part in self.binding)
        ):
            raise TypeError("terminal reconcile target requires an exact binding")
        for name, value in (
            ("thread_id", self.thread_id),
            ("turn_id", self.turn_id),
            ("card_message_id", self.card_message_id),
            ("prompt_message_id", self.prompt_message_id),
        ):
            if type(value) is not str:
                raise TypeError(f"terminal reconcile {name} must be an exact string")
        if type(self.prompt_reply_in_thread) is not bool:
            raise TypeError("terminal reconcile reply-in-thread fact must be bool")
        if type(self.transcript) is not ExecutionTranscriptSnapshot:
            raise TypeError(
                "terminal reconcile transcript must be an exact immutable snapshot"
            )
        if (
            type(self.cursor_start) is not ExecutionTranscriptCursor
            or type(self.cursor_end) is not ExecutionTranscriptCursor
            or not self.cursor_end.follows(self.cursor_start)
        ):
            raise TypeError("terminal reconcile requires an ordered cursor range")
        if type(self.cancelled) is not bool:
            raise TypeError("terminal reconcile cancelled fact must be bool")
        if type(self.elapsed) is not int:
            raise TypeError("terminal reconcile elapsed must be an exact integer")
        if self.elapsed < 0:
            raise ValueError("terminal reconcile elapsed must be non-negative")
        require_terminal_execution_page_receipts(
            self.terminal_page_receipts,
            field="terminal reconcile page receipts",
        )

    @property
    def sender_id(self) -> str:
        return self.binding[0]

    @property
    def chat_id(self) -> str:
        return self.binding[1]


@dataclass(frozen=True, slots=True)
class PrepareSnapshotReconcileCommand:
    target: BindingExecutionTarget
    thread_id: str
    turn_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class SnapshotReconcilePreparation:
    session: BindingSessionSnapshot
    target: BindingExecutionTarget
    observation: ExecutionRuntimeObservationFence
    thread_id: str
    turn_id: str
    local_terminal: TerminalReconcileTarget | None


@dataclass(frozen=True, slots=True)
class PrepareTerminalFallbackCommand:
    target: BindingExecutionTarget
    observation: ExecutionRuntimeObservationFence
    thread_id: str
    turn_id: str
    occurred_at: float


@dataclass(frozen=True, slots=True)
class RecoveryFinalizationPreparation:
    session: BindingSessionSnapshot
    terminal: TerminalReconcileTarget | None


@dataclass(frozen=True, slots=True)
class RecoverySnapshotReplyItem:
    item_type: str
    text: str
    text_available: bool = True

    def __post_init__(self) -> None:
        if type(self.item_type) is not str or type(self.text) is not str:
            raise TypeError("recovery snapshot reply item fields must be exact strings")
        if type(self.text_available) is not bool:
            raise TypeError(
                "recovery snapshot reply item text_available must be exact bool"
            )


@dataclass(frozen=True, slots=True)
class ApplyExecutionSnapshotCommand:
    target: BindingExecutionTarget
    observation: ExecutionRuntimeObservationFence
    thread_id: str
    turn_id: str
    title: str
    working_dir: str
    reply_text: str
    reply_items: tuple[RecoverySnapshotReplyItem, ...]
    turn_status: str
    thread_active: bool
    occurred_at: float
    invalidates_local_agent_evidence: bool = False
    apply_thread_metadata: bool = True


@dataclass(frozen=True, slots=True)
class ExecutionSnapshotTransition:
    session: BindingSessionSnapshot
    should_finalize: bool
    terminal: TerminalReconcileTarget | None


@dataclass(frozen=True, slots=True)
class MarkExecutionRuntimeDegradedCommand:
    target: BindingExecutionTarget
    observation: ExecutionRuntimeObservationFence


@dataclass(frozen=True, slots=True)
class PrepareCompactStartUnknownCommand:
    target: BindingExecutionTarget
    thread_id: str


@dataclass(frozen=True, slots=True)
class CommitCompactStartUnknownCommand:
    target: BindingExecutionTarget
    thread_id: str
    error_text: str


class ExecutionRecoveryRuntimeTransitions:
    """Own recovery runtime mutation while exposing immutable effects only."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: ExecutionRecoveryBindingRuntime,
        turn_execution: TurnExecutionCoordinator,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._turn_execution = turn_execution
        self._lifecycle = BindingRuntimeLifecycleTransitions(
            turn_execution=turn_execution
        )

    def prepare_mirror_watchdog(
        self,
        command: PrepareMirrorWatchdogCommand,
    ) -> MirrorWatchdogInstallPreparation | None:
        self._require_time(command.delay_seconds, field="watchdog delay_seconds")
        with self._lock:
            required = self._require_watchdog_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            timer_cancellations = self._lifecycle.detach_timers_locked(
                command.target.execution.binding,
                state,
                patch=False,
                mirror=True,
            )
            updated = self._updated_session_locked(command.target.handle)
            if not updated.execution.running or not updated.current_thread_id:
                return MirrorWatchdogInstallPreparation(
                    session=updated,
                    ticket=None,
                    delay_seconds=command.delay_seconds,
                    timer_cancellations=timer_cancellations,
                )
            ticket = MirrorWatchdogTicket(
                binding=updated.binding,
                thread_id=updated.current_thread_id.strip(),
                card_message_id=updated.execution.current_message_id.strip(),
                turn_id=updated.execution.current_turn_id.strip(),
            )
            return MirrorWatchdogInstallPreparation(
                session=updated,
                ticket=ticket,
                delay_seconds=command.delay_seconds,
                timer_cancellations=timer_cancellations,
            )

    def install_mirror_watchdog(
        self,
        command: InstallMirrorWatchdogCommand,
    ) -> BindingSessionSnapshot | None:
        if type(command.registration) is not MirrorWatchdogRegistration:
            raise TypeError("mirror watchdog install requires a typed registration")
        with self._lock:
            required = self._require_watchdog_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            registration = command.registration
            ticket = registration.ticket
            if (
                type(ticket) is not MirrorWatchdogTicket
                or session.execution.mirror_watchdog_registered
                or ticket.binding != session.binding
                or ticket.thread_id != session.current_thread_id.strip()
                or ticket.card_message_id
                != session.execution.current_message_id.strip()
                or ticket.turn_id != session.execution.current_turn_id.strip()
            ):
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_registration=registration),
            )
            return self._updated_session_locked(command.target.handle)

    def rollback_mirror_watchdog_start(
        self,
        command: RollbackMirrorWatchdogCommand,
    ) -> bool:
        if type(command.handle) is not BindingRuntimeHandle:
            raise TypeError("mirror watchdog rollback requires an exact handle")
        if type(command.registration) is not MirrorWatchdogRegistration:
            raise TypeError("mirror watchdog rollback requires a typed registration")
        with self._lock:
            try:
                self._binding_runtime.session_snapshot_locked(command.handle)
            except RuntimeError:
                return False
            state = self._binding_runtime.resident_runtime_state_locked(
                command.handle.binding
            )
            if (
                state is None
                or state["mirror_watchdog_registration"] is not command.registration
            ):
                return False
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_registration=None),
            )
            return True

    def consume_mirror_watchdog(
        self,
        command: ConsumeMirrorWatchdogCommand,
    ) -> MirrorWatchdogEffect | None:
        if type(command.ticket) is not MirrorWatchdogTicket:
            raise TypeError("mirror watchdog consume requires a typed ticket")
        self._require_time(command.occurred_at, field="watchdog occurred_at")
        self._require_time(
            command.compact_start_timeout_seconds,
            field="compact start timeout_seconds",
        )
        ticket = command.ticket
        with self._lock:
            state = self._binding_runtime.resident_runtime_state_locked(
                ticket.binding
            )
            if state is None:
                return None
            registration = state["mirror_watchdog_registration"]
            if registration is None or registration.ticket is not ticket:
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_registration=None),
            )
            session = self._binding_runtime.resident_session_snapshot_locked(
                ticket.binding
            )
            if session is None or (
                session.current_thread_id.strip() != ticket.thread_id
                or session.execution.current_message_id.strip()
                != ticket.card_message_id
                or session.execution.current_turn_id.strip() != ticket.turn_id
                or not session.execution.running
            ):
                return None
            execution = session.execution
            awaiting_started = bool(
                execution.current_message_id
                and execution.awaiting_local_turn_started
                and (
                    execution.awaiting_attach_status_settle
                    or not execution.current_turn_id
                )
            )
            action: MirrorWatchdogAction = "reconcile"
            if awaiting_started:
                timed_out = bool(
                    execution.current_execution_kind.strip() == "compact"
                    and command.compact_start_timeout_seconds
                    and execution.started_at
                    and command.occurred_at - execution.started_at
                    >= command.compact_start_timeout_seconds
                )
                action = (
                    "compact_start_unknown" if timed_out else "reschedule"
                )
            return MirrorWatchdogEffect(
                session=session,
                action=action,
                thread_id=session.current_thread_id.strip(),
                turn_id=execution.current_turn_id.strip(),
            )

    def capture_terminal_target(
        self,
        command: CaptureTerminalReconcileTargetCommand,
    ) -> TerminalReconcileTarget | None:
        self._require_time(command.occurred_at, field="terminal capture occurred_at")
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            normalized_thread_id = self._normalize_string(
                command.thread_id,
                field="terminal capture thread_id",
            )
            normalized_turn_id = self._normalize_string(
                command.turn_id,
                field="terminal capture turn_id",
            )
            if session.current_thread_id.strip() != normalized_thread_id:
                return None
            resolved_turn_id = (
                normalized_turn_id
                or session.execution.current_turn_id.strip()
            )
            if not resolved_turn_id:
                return None
            return self._terminal_target(
                session,
                thread_id=normalized_thread_id,
                turn_id=resolved_turn_id,
                occurred_at=command.occurred_at,
            )

    def prepare_snapshot_reconcile(
        self,
        command: PrepareSnapshotReconcileCommand,
    ) -> SnapshotReconcilePreparation | None:
        self._require_time(command.occurred_at, field="snapshot prepare occurred_at")
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            thread_id = self._normalize_string(
                command.thread_id,
                field="snapshot prepare thread_id",
            )
            turn_id = self._normalize_string(
                command.turn_id,
                field="snapshot prepare turn_id",
            )
            if thread_id and session.current_thread_id.strip() != thread_id:
                return None
            if (
                thread_id
                and not turn_id
                and session.execution.current_message_id
                and session.execution.awaiting_local_turn_started
                and (
                    session.execution.awaiting_attach_status_settle
                    or not session.execution.current_turn_id
                )
            ):
                return None
            local_terminal = None
            if not thread_id:
                local_terminal = self._terminal_target(
                    session,
                    thread_id="",
                    turn_id=turn_id,
                    occurred_at=command.occurred_at,
                )
            return SnapshotReconcilePreparation(
                session=session,
                target=command.target,
                observation=ExecutionRuntimeObservationFence.from_session(
                    session
                ),
                thread_id=thread_id,
                turn_id=turn_id,
                local_terminal=local_terminal,
            )

    def prepare_terminal_fallback(
        self,
        command: PrepareTerminalFallbackCommand,
    ) -> RecoveryFinalizationPreparation | None:
        self._require_time(command.occurred_at, field="fallback occurred_at")
        self._require_observation_fence(
            command.observation,
            field="fallback observation",
        )
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            thread_id = self._normalize_string(
                command.thread_id,
                field="fallback thread_id",
            )
            turn_id = self._normalize_string(
                command.turn_id,
                field="fallback turn_id",
            )
            if thread_id and session.current_thread_id.strip() != thread_id:
                return None
            if not command.observation.matches(session):
                return None
            return RecoveryFinalizationPreparation(
                session=session,
                terminal=self._terminal_target(
                    session,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    occurred_at=command.occurred_at,
                ),
            )

    def apply_execution_snapshot(
        self,
        command: ApplyExecutionSnapshotCommand,
    ) -> ExecutionSnapshotTransition | None:
        self._require_snapshot_command(command)
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if session.current_thread_id.strip() != command.thread_id:
                return None
            if not command.observation.matches(session):
                return None
            if command.apply_thread_metadata:
                metadata = self._binding_runtime.update_thread_metadata_locked(
                    command.target.handle,
                    expected_thread_id=command.thread_id,
                    current_thread_title=(
                        command.title or session.current_thread_title
                    ),
                    working_dir=command.working_dir or session.working_dir,
                )
                if metadata is None:
                    return None
            self._turn_execution.apply_snapshot_reply_locked(
                state,
                reply_text=command.reply_text,
                reply_items=[
                    {
                        "type": item.item_type,
                        **({"text": item.text} if item.text_available else {}),
                    }
                    for item in command.reply_items
                ],
            )
            if command.invalidates_local_agent_evidence:
                self._turn_execution.record_unavailable_assistant_completion_locked(
                    state
                )
            if command.turn_status == "interrupted":
                self._turn_execution.apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(cancelled=True),
                )
            turn_terminal = command.turn_status in {
                "completed",
                "interrupted",
                "failed",
            }
            if command.thread_active and not turn_terminal:
                self._turn_execution.acknowledge_running_snapshot_locked(
                    state,
                    occurred_at=command.occurred_at,
                )
                return ExecutionSnapshotTransition(
                    session=self._updated_session_locked(command.target.handle),
                    should_finalize=False,
                    terminal=None,
                )
            updated = self._updated_session_locked(command.target.handle)
            return ExecutionSnapshotTransition(
                session=updated,
                should_finalize=True,
                terminal=self._terminal_target(
                    updated,
                    thread_id=command.thread_id,
                    turn_id=command.turn_id,
                    occurred_at=command.occurred_at,
                ),
            )

    def mark_runtime_degraded(
        self,
        command: MarkExecutionRuntimeDegradedCommand,
    ) -> BindingSessionSnapshot | None:
        self._require_observation_fence(
            command.observation,
            field="degraded observation",
        )
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not command.observation.matches(session):
                return None
            if not self._turn_execution.mark_runtime_degraded_locked(state):
                return None
            return self._updated_session_locked(command.target.handle)

    def prepare_compact_start_unknown(
        self,
        command: PrepareCompactStartUnknownCommand,
    ) -> BindingSessionSnapshot | None:
        thread_id = self._normalize_string(
            command.thread_id,
            field="compact unknown thread_id",
        )
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            return session if self._compact_anchor_matches(session, thread_id) else None

    def commit_compact_start_unknown(
        self,
        command: CommitCompactStartUnknownCommand,
    ) -> BindingSessionSnapshot | None:
        thread_id = self._normalize_string(
            command.thread_id,
            field="compact unknown thread_id",
        )
        if type(command.error_text) is not str:
            raise TypeError("compact unknown error_text must be an exact string")
        with self._lock:
            required = self._require_execution_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if not self._compact_anchor_matches(session, thread_id):
                return None
            self._turn_execution.apply_terminal_error_locked(
                state,
                error_message=command.error_text,
            )
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(
                    runtime_channel_state="degraded",
                ),
            )
            return self._updated_session_locked(command.target.handle)

    def _require_watchdog_target_locked(
        self,
        target: MirrorWatchdogTarget,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict] | None:
        if type(target) is not MirrorWatchdogTarget:
            raise TypeError("recovery transition requires a typed watchdog target")
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

    def _require_execution_target_locked(
        self,
        target: BindingExecutionTarget,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict] | None:
        if type(target) is not BindingExecutionTarget:
            raise TypeError("recovery transition requires a typed execution target")
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
    def _compact_anchor_matches(
        session: BindingSessionSnapshot,
        thread_id: str,
    ) -> bool:
        execution = session.execution
        return bool(
            session.current_thread_id.strip() == thread_id
            and execution.current_message_id.strip()
            and execution.current_execution_kind.strip() == "compact"
            and execution.awaiting_local_turn_started
            and not execution.current_turn_id.strip()
        )

    @staticmethod
    def _terminal_target(
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str,
        occurred_at: float,
    ) -> TerminalReconcileTarget | None:
        card_message_id = session.execution.current_message_id.strip()
        if not card_message_id:
            return None
        page = session.execution.pages.active_page
        cursor_end = session.execution.pages.active_projection_end(
            session.execution.transcript
        )
        if page is None or cursor_end is None:
            return None
        started_at = session.execution.started_at
        return TerminalReconcileTarget(
            binding=session.binding,
            thread_id=thread_id,
            turn_id=turn_id,
            card_message_id=card_message_id,
            prompt_message_id=(
                session.execution.current_prompt_message_id.strip()
            ),
            prompt_reply_in_thread=(
                session.execution.current_prompt_reply_in_thread
            ),
            transcript=session.execution.transcript,
            cursor_start=page.cursor_start,
            cursor_end=cursor_end,
            cancelled=session.execution.cancelled,
            elapsed=(
                int(max(0.0, occurred_at - started_at))
                if started_at
                else 0
            ),
            terminal_page_receipts=(
                TerminalExecutionPageReceipt(
                    message_id=card_message_id,
                    cursor_start=page.cursor_start,
                    cursor_end=cursor_end,
                ),
            ),
        )

    @classmethod
    def _require_snapshot_command(
        cls,
        command: ApplyExecutionSnapshotCommand,
    ) -> None:
        if type(command.target) is not BindingExecutionTarget:
            raise TypeError("snapshot application requires an execution target")
        cls._require_observation_fence(
            command.observation,
            field="snapshot observation",
        )
        for name, value in (
            ("thread_id", command.thread_id),
            ("turn_id", command.turn_id),
            ("title", command.title),
            ("working_dir", command.working_dir),
            ("reply_text", command.reply_text),
            ("turn_status", command.turn_status),
        ):
            if type(value) is not str:
                raise TypeError(f"snapshot {name} must be an exact string")
        if type(command.reply_items) is not tuple or any(
            type(item) is not RecoverySnapshotReplyItem
            for item in command.reply_items
        ):
            raise TypeError(
                "snapshot reply_items must be an exact tuple of typed items"
            )
        if type(command.thread_active) is not bool:
            raise TypeError("snapshot thread_active must be bool")
        if type(command.invalidates_local_agent_evidence) is not bool:
            raise TypeError(
                "snapshot invalidates_local_agent_evidence must be bool"
            )
        if type(command.apply_thread_metadata) is not bool:
            raise TypeError("snapshot apply_thread_metadata must be bool")
        cls._require_time(command.occurred_at, field="snapshot occurred_at")

    @staticmethod
    def _normalize_string(value: str, *, field: str) -> str:
        if type(value) is not str:
            raise TypeError(f"recovery {field} must be an exact string")
        return value.strip()

    @staticmethod
    def _require_time(value: float, *, field: str) -> None:
        if type(value) is not float:
            raise TypeError(f"recovery {field} must be an exact float")
        if value < 0:
            raise ValueError(f"recovery {field} must be non-negative")

    @staticmethod
    def _require_observation_fence(
        value: ExecutionRuntimeObservationFence,
        *,
        field: str,
    ) -> None:
        if type(value) is not ExecutionRuntimeObservationFence:
            raise TypeError(f"recovery {field} must be an exact observation fence")
