"""Exact binding-runtime transitions for execution-card output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Protocol, TypeAlias

from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingPlanSnapshot,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.binding_runtime_lifecycle import (
    BindingRuntimeLifecycleTransitions,
    RuntimeTimerCancellationEffect,
)
from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionPageSendOutcome,
    ExecutionPageStatus,
    ExecutionPresentationPage,
    ExecutionTranscriptCursor,
)
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
    ExecutionStateChanged,
    PlanStateChanged,
    RuntimeStateDict,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class ExecutionOutputBindingRuntime(Protocol):
    """Exact resident operations consumed by the output transition owner."""

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


@dataclass(frozen=True, slots=True)
class ExecutionOutputTarget:
    """Execution fence plus output-control facts captured by one command."""

    execution: BindingExecutionTarget
    expected_last_patch_at: float
    expected_patch_timer_registered: bool
    expected_plan: BindingPlanSnapshot | None = None

    @classmethod
    def from_session(
        cls,
        session: BindingSessionSnapshot,
        *,
        include_plan: bool = False,
    ) -> ExecutionOutputTarget:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError("execution output target requires an exact session")
        return cls(
            execution=BindingExecutionTarget.from_session(session),
            expected_last_patch_at=session.execution.last_patch_at,
            expected_patch_timer_registered=(
                session.execution.patch_timer_registered
            ),
            expected_plan=session.plan if include_plan else None,
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
            and session.execution.last_patch_at == self.expected_last_patch_at
            and session.execution.patch_timer_registered
            is self.expected_patch_timer_registered
            and (
                self.expected_plan is None
                or session.plan == self.expected_plan
            )
        )


@dataclass(frozen=True, slots=True)
class ScheduleExecutionCardCommand:
    target: ExecutionOutputTarget
    occurred_at: float
    interval_seconds: float


@dataclass(frozen=True, slots=True)
class SchedulePendingPageReconciliationCommand:
    target: ExecutionOutputTarget
    delay_seconds: float


@dataclass(frozen=True, slots=True)
class PrepareInitialExecutionPageCommand:
    target: ExecutionOutputTarget
    outbound_attempt_id: str
    known_message_id: str = ""


@dataclass(frozen=True, slots=True, eq=False)
class InitialExecutionPageReceipt:
    """Single-use authority for one exact opening page object."""

    handle: BindingRuntimeHandle
    ledger: ExecutionPageLedger
    page: ExecutionPresentationPage


@dataclass(frozen=True, slots=True)
class InitialExecutionPagePreparation:
    session: BindingSessionSnapshot
    receipt: InitialExecutionPageReceipt


@dataclass(frozen=True, slots=True)
class CommitInitialExecutionPageCommand:
    receipt: InitialExecutionPageReceipt
    outcome: ExecutionPageSendOutcome
    message_id: str = ""


@dataclass(frozen=True, slots=True)
class InitialExecutionPageCommit:
    session: BindingSessionSnapshot
    outcome: ExecutionPageSendOutcome


@dataclass(frozen=True, slots=True)
class PrepareExecutionPageRolloverCommand:
    target: ExecutionOutputTarget
    outbound_attempt_id: str
    cursor_start: ExecutionTranscriptCursor


@dataclass(frozen=True, slots=True, eq=False)
class ExecutionPageRolloverReceipt:
    """Single-use authority for one active-page rollover attempt."""

    handle: BindingRuntimeHandle
    ledger: ExecutionPageLedger
    active_page: ExecutionPresentationPage
    opening_page: ExecutionPresentationPage


@dataclass(frozen=True, slots=True)
class ExecutionPageRolloverPreparation:
    session: BindingSessionSnapshot
    receipt: ExecutionPageRolloverReceipt


@dataclass(frozen=True, slots=True)
class CommitExecutionPageRolloverCommand:
    receipt: ExecutionPageRolloverReceipt
    outcome: ExecutionPageSendOutcome
    message_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionPageRolloverCommit:
    session: BindingSessionSnapshot
    outcome: ExecutionPageSendOutcome


@dataclass(frozen=True, slots=True)
class PrepareExecutionPageSendUnknownReconciliationCommand:
    target: ExecutionOutputTarget


@dataclass(frozen=True, slots=True, eq=False)
class ExecutionPageSendUnknownReconciliationReceipt:
    """Single-use authority for one retry of the same outbound UUID."""

    handle: BindingRuntimeHandle
    ledger: ExecutionPageLedger
    page: ExecutionPresentationPage


@dataclass(frozen=True, slots=True)
class ExecutionPageSendUnknownReconciliationPreparation:
    session: BindingSessionSnapshot
    receipt: ExecutionPageSendUnknownReconciliationReceipt


@dataclass(frozen=True, slots=True)
class CommitExecutionPageSendUnknownReconciliationCommand:
    receipt: ExecutionPageSendUnknownReconciliationReceipt
    outcome: ExecutionPageSendOutcome
    message_id: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionPageSendUnknownReconciliationCommit:
    session: BindingSessionSnapshot
    outcome: ExecutionPageSendOutcome


@dataclass(frozen=True, slots=True)
class ImmediateExecutionCardFlush:
    session: BindingSessionSnapshot
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionPatchTimerInstallPreparation:
    session: BindingSessionSnapshot
    ticket: ExecutionPatchTimerTicket
    delay_seconds: float
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


ScheduleExecutionCardTransition: TypeAlias = (
    ImmediateExecutionCardFlush | ExecutionPatchTimerInstallPreparation
)


@dataclass(frozen=True, slots=True)
class InstallExecutionPatchTimerCommand:
    target: ExecutionOutputTarget
    registration: ExecutionPatchTimerRegistration


@dataclass(frozen=True, slots=True)
class RollbackExecutionPatchTimerCommand:
    handle: BindingRuntimeHandle
    registration: ExecutionPatchTimerRegistration


@dataclass(frozen=True, slots=True)
class ConsumeExecutionPatchTimerCommand:
    ticket: ExecutionPatchTimerTicket
    occurred_at: float


@dataclass(frozen=True, slots=True)
class PrepareExecutionCardFlushCommand:
    target: ExecutionOutputTarget
    occurred_at: float


@dataclass(frozen=True, slots=True)
class PreparePatchFailureFollowupCommand:
    target: ExecutionOutputTarget


@dataclass(frozen=True, slots=True)
class CaptureExecutionPlanCardCommand:
    target: ExecutionOutputTarget


@dataclass(frozen=True, slots=True)
class CommitExecutionPlanCardCommand:
    target: ExecutionOutputTarget
    message_id: str


@dataclass(frozen=True, slots=True)
class ExecutionCardEffect:
    session: BindingSessionSnapshot
    message_id: str
    transcript: ExecutionTranscriptSnapshot
    running: bool
    elapsed: int
    cancelled: bool

    @property
    def reply_text(self) -> str:
        return self.transcript.reply_text()


@dataclass(frozen=True, slots=True)
class ExecutionCardFlushTransition:
    session: BindingSessionSnapshot
    effect: ExecutionCardEffect | None
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionPatchFailureFollowupEffect:
    chat_id: str
    reply_text: str
    prompt_message_id: str
    prompt_reply_in_thread: bool


@dataclass(frozen=True, slots=True)
class ExecutionPlanCardEffect:
    session: BindingSessionSnapshot
    plan: BindingPlanSnapshot


class ExecutionOutputRuntimeTransitions:
    """Own output runtime mutation while exposing immutable effects only."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        binding_runtime: ExecutionOutputBindingRuntime,
        turn_execution: TurnExecutionCoordinator,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._turn_execution = turn_execution
        self._lifecycle = BindingRuntimeLifecycleTransitions(
            turn_execution=turn_execution
        )

    def prepare_initial_page(
        self,
        command: PrepareInitialExecutionPageCommand,
    ) -> InitialExecutionPagePreparation | None:
        attempt_id = self._require_text(
            command.outbound_attempt_id,
            field="initial page outbound_attempt_id",
        )
        known_message_id = self._require_text(
            command.known_message_id,
            field="initial page known_message_id",
            allow_empty=True,
        )
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            ledger = session.execution.pages.prepare_initial(
                outbound_attempt_id=attempt_id,
                known_message_id=known_message_id,
            )
            page = ledger.current_page
            if page is None:
                raise RuntimeError("initial execution page preparation lost its page")
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(execution_pages=ledger),
            )
            updated = self._updated_session_locked(command.target.handle)
            return InitialExecutionPagePreparation(
                session=updated,
                receipt=InitialExecutionPageReceipt(
                    handle=command.target.handle,
                    ledger=ledger,
                    page=page,
                ),
            )

    def commit_initial_page(
        self,
        command: CommitInitialExecutionPageCommand,
    ) -> InitialExecutionPageCommit | None:
        if type(command.receipt) is not InitialExecutionPageReceipt:
            raise TypeError("initial page commit requires an exact receipt")
        if type(command.outcome) is not ExecutionPageSendOutcome:
            raise TypeError("initial page commit requires a typed outcome")
        message_id = self._require_text(
            command.message_id,
            field="initial page result message_id",
            allow_empty=True,
        )
        receipt = command.receipt
        with self._lock:
            try:
                session = self._binding_runtime.session_snapshot_locked(
                    receipt.handle
                )
            except RuntimeError:
                return None
            if session.execution.pages is not receipt.ledger:
                return None
            state = self._binding_runtime.resident_runtime_state_locked(
                receipt.handle.binding
            )
            if state is None:
                return None
            if command.outcome is ExecutionPageSendOutcome.CONFIRMED:
                ledger = receipt.ledger.activate_opening(
                    expected_page=receipt.page,
                    message_id=message_id,
                )
            elif command.outcome is ExecutionPageSendOutcome.UNKNOWN:
                if message_id and message_id != receipt.page.message_id:
                    raise ValueError(
                        "unknown initial page cannot invent a new message id"
                    )
                ledger = receipt.ledger.mark_send_unknown(
                    expected_page=receipt.page,
                )
            else:
                if message_id:
                    raise ValueError("rejected initial page cannot have message_id")
                ledger = receipt.ledger.discard_opening(
                    expected_page=receipt.page,
                )
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(execution_pages=ledger),
            )
            return InitialExecutionPageCommit(
                session=self._updated_session_locked(receipt.handle),
                outcome=command.outcome,
            )

    def prepare_rollover(
        self,
        command: PrepareExecutionPageRolloverCommand,
    ) -> ExecutionPageRolloverPreparation | None:
        attempt_id = self._require_text(
            command.outbound_attempt_id,
            field="rollover outbound_attempt_id",
        )
        if type(command.cursor_start) is not ExecutionTranscriptCursor:
            raise TypeError("execution rollover requires a typed cursor")
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            transcript_end = ExecutionTranscriptCursor.from_transcript(
                session.execution.transcript
            )
            if not transcript_end.follows(command.cursor_start):
                raise ValueError("execution rollover cursor exceeds the transcript")
            if (
                session.execution.pages.active_page is None
                or session.execution.pages.pending_page is not None
            ):
                return None
            ledger = session.execution.pages.prepare_rollover(
                outbound_attempt_id=attempt_id,
                cursor_start=command.cursor_start,
            )
            active_page = ledger.active_page
            opening_page = ledger.pending_page
            if active_page is None or opening_page is None:
                raise RuntimeError("execution rollover preparation lost its pages")
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(execution_pages=ledger),
            )
            updated = self._updated_session_locked(command.target.handle)
            return ExecutionPageRolloverPreparation(
                session=updated,
                receipt=ExecutionPageRolloverReceipt(
                    handle=command.target.handle,
                    ledger=ledger,
                    active_page=active_page,
                    opening_page=opening_page,
                ),
            )

    def commit_rollover(
        self,
        command: CommitExecutionPageRolloverCommand,
    ) -> ExecutionPageRolloverCommit | None:
        if type(command.receipt) is not ExecutionPageRolloverReceipt:
            raise TypeError("execution rollover commit requires an exact receipt")
        if type(command.outcome) is not ExecutionPageSendOutcome:
            raise TypeError("execution rollover commit requires a typed outcome")
        message_id = self._require_text(
            command.message_id,
            field="rollover result message_id",
            allow_empty=True,
        )
        receipt = command.receipt
        with self._lock:
            try:
                session = self._binding_runtime.session_snapshot_locked(
                    receipt.handle
                )
            except RuntimeError:
                return None
            if session.execution.pages is not receipt.ledger:
                return None
            state = self._binding_runtime.resident_runtime_state_locked(
                receipt.handle.binding
            )
            if state is None:
                return None
            if command.outcome is ExecutionPageSendOutcome.CONFIRMED:
                ledger = receipt.ledger.activate_rollover(
                    expected_active=receipt.active_page,
                    expected_opening=receipt.opening_page,
                    message_id=message_id,
                )
            elif command.outcome is ExecutionPageSendOutcome.UNKNOWN:
                if message_id:
                    raise ValueError(
                        "unknown execution rollover cannot claim a message id"
                    )
                ledger = receipt.ledger.mark_rollover_send_unknown(
                    expected_active=receipt.active_page,
                    expected_opening=receipt.opening_page,
                )
            else:
                if message_id:
                    raise ValueError(
                        "rejected execution rollover cannot have message_id"
                    )
                ledger = receipt.ledger.discard_rollover_opening(
                    expected_active=receipt.active_page,
                    expected_opening=receipt.opening_page,
                )
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(execution_pages=ledger),
            )
            return ExecutionPageRolloverCommit(
                session=self._updated_session_locked(receipt.handle),
                outcome=command.outcome,
            )

    def prepare_send_unknown_reconciliation(
        self,
        command: PrepareExecutionPageSendUnknownReconciliationCommand,
    ) -> ExecutionPageSendUnknownReconciliationPreparation | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            pending = session.execution.pages.pending_page
            if (
                pending is None
                or pending.status is not ExecutionPageStatus.SEND_UNKNOWN
                or pending.reconciliation_attempted
            ):
                return None
            return ExecutionPageSendUnknownReconciliationPreparation(
                session=session,
                receipt=ExecutionPageSendUnknownReconciliationReceipt(
                    handle=command.target.handle,
                    ledger=session.execution.pages,
                    page=pending,
                ),
            )

    def commit_send_unknown_reconciliation(
        self,
        command: CommitExecutionPageSendUnknownReconciliationCommand,
    ) -> ExecutionPageSendUnknownReconciliationCommit | None:
        if type(command.receipt) is not (
            ExecutionPageSendUnknownReconciliationReceipt
        ):
            raise TypeError(
                "execution page reconciliation commit requires an exact receipt"
            )
        if type(command.outcome) is not ExecutionPageSendOutcome:
            raise TypeError(
                "execution page reconciliation commit requires a typed outcome"
            )
        message_id = self._require_text(
            command.message_id,
            field="reconciled execution page message_id",
            allow_empty=True,
        )
        receipt = command.receipt
        with self._lock:
            try:
                session = self._binding_runtime.session_snapshot_locked(
                    receipt.handle
                )
            except RuntimeError:
                return None
            if session.execution.pages is not receipt.ledger:
                return None
            state = self._binding_runtime.resident_runtime_state_locked(
                receipt.handle.binding
            )
            if state is None:
                return None
            if command.outcome is ExecutionPageSendOutcome.CONFIRMED:
                ledger = receipt.ledger.confirm_send_unknown(
                    expected_page=receipt.page,
                    message_id=message_id,
                )
            elif command.outcome is ExecutionPageSendOutcome.REJECTED:
                if message_id:
                    raise ValueError(
                        "rejected execution page reconciliation cannot have message_id"
                    )
                ledger = receipt.ledger.reject_send_unknown(
                    expected_page=receipt.page,
                )
            else:
                if message_id:
                    raise ValueError(
                        "unknown execution page reconciliation cannot claim a message id"
                    )
                ledger = receipt.ledger.retain_send_unknown(
                    expected_page=receipt.page,
                )
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(execution_pages=ledger),
            )
            return ExecutionPageSendUnknownReconciliationCommit(
                session=self._updated_session_locked(receipt.handle),
                outcome=command.outcome,
            )

    def prepare_pending_page_reconciliation_timer(
        self,
        command: SchedulePendingPageReconciliationCommand,
    ) -> ExecutionPatchTimerInstallPreparation | None:
        self._require_time(
            command.delay_seconds,
            field="page reconciliation delay_seconds",
        )
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            pending = session.execution.pages.pending_page
            if (
                pending is None
                or not session.execution.pages.send_outcome_unknown
                or pending.reconciliation_attempted
                or session.execution.patch_timer_registered
            ):
                return None
            return ExecutionPatchTimerInstallPreparation(
                session=session,
                ticket=ExecutionPatchTimerTicket(
                    binding=session.binding,
                    thread_id=session.current_thread_id.strip(),
                    card_message_id=(
                        session.execution.current_message_id.strip()
                        or pending.message_id.strip()
                    ),
                    turn_id=session.execution.current_turn_id.strip(),
                    page_attempt_id=pending.outbound_attempt_id,
                ),
                delay_seconds=command.delay_seconds,
            )

    def prepare_schedule(
        self,
        command: ScheduleExecutionCardCommand,
    ) -> ScheduleExecutionCardTransition | None:
        self._require_time(command.occurred_at, field="schedule occurred_at")
        self._require_time(
            command.interval_seconds,
            field="schedule interval_seconds",
        )
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            presentation_key = self._timer_presentation_key(session)
            if not presentation_key:
                return None
            last_patch_at = session.execution.last_patch_at
            if command.occurred_at - last_patch_at >= command.interval_seconds:
                self._turn_execution.apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(last_patch_at=command.occurred_at),
                )
                timer_cancellations = self._prepare_patch_timer_cancellation_locked(
                    command.target.binding,
                    state,
                )
                return ImmediateExecutionCardFlush(
                    session=self._updated_session_locked(command.target.handle),
                    timer_cancellations=timer_cancellations,
                )
            if session.execution.patch_timer_registered:
                return None
            ticket = ExecutionPatchTimerTicket(
                binding=session.binding,
                thread_id=session.current_thread_id.strip(),
                card_message_id=session.execution.current_message_id.strip(),
                turn_id=session.execution.current_turn_id.strip(),
                page_attempt_id=(
                    session.execution.pages.pending_page.outbound_attempt_id
                    if session.execution.pages.send_outcome_unknown
                    and session.execution.pages.pending_page is not None
                    else ""
                ),
            )
            return ExecutionPatchTimerInstallPreparation(
                session=session,
                ticket=ticket,
                delay_seconds=(
                    command.interval_seconds
                    - (command.occurred_at - last_patch_at)
                ),
            )

    def install_patch_timer(
        self,
        command: InstallExecutionPatchTimerCommand,
    ) -> BindingSessionSnapshot | None:
        if type(command.registration) is not ExecutionPatchTimerRegistration:
            raise TypeError("patch timer install requires a typed registration")
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            registration = command.registration
            ticket = registration.ticket
            if (
                type(ticket) is not ExecutionPatchTimerTicket
                or session.execution.patch_timer_registered
                or not self._timer_ticket_matches(session, ticket)
            ):
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(patch_timer_registration=registration),
            )
            return self._updated_session_locked(command.target.handle)

    def rollback_patch_timer_start(
        self,
        command: RollbackExecutionPatchTimerCommand,
    ) -> bool:
        if type(command.handle) is not BindingRuntimeHandle:
            raise TypeError("patch timer rollback requires an exact handle")
        if type(command.registration) is not ExecutionPatchTimerRegistration:
            raise TypeError("patch timer rollback requires a typed registration")
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
                or state["patch_timer_registration"] is not command.registration
            ):
                return False
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(patch_timer_registration=None),
            )
            return True

    def consume_patch_timer(
        self,
        command: ConsumeExecutionPatchTimerCommand,
    ) -> ExecutionCardEffect | None:
        if type(command.ticket) is not ExecutionPatchTimerTicket:
            raise TypeError("patch timer consume requires a typed ticket")
        self._require_time(command.occurred_at, field="timer occurred_at")
        ticket = command.ticket
        with self._lock:
            state = self._binding_runtime.resident_runtime_state_locked(
                ticket.binding
            )
            if state is None:
                return None
            registration = state["patch_timer_registration"]
            if registration is None or registration.ticket is not ticket:
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(patch_timer_registration=None),
            )
            session = self._binding_runtime.resident_session_snapshot_locked(
                ticket.binding
            )
            if session is None or not self._timer_ticket_matches(
                session,
                ticket,
            ):
                return None
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(last_patch_at=command.occurred_at),
            )
            updated = self._binding_runtime.session_snapshot_locked(
                session.handle
            )
            return self._card_effect(updated, occurred_at=command.occurred_at)

    def prepare_flush(
        self,
        command: PrepareExecutionCardFlushCommand,
    ) -> ExecutionCardFlushTransition | None:
        self._require_time(command.occurred_at, field="flush occurred_at")
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            timer_cancellations = self._prepare_patch_timer_cancellation_locked(
                command.target.binding,
                state,
            )
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(last_patch_at=command.occurred_at),
            )
            updated = self._updated_session_locked(command.target.handle)
            return ExecutionCardFlushTransition(
                session=updated,
                effect=self._card_effect(
                    updated,
                    occurred_at=command.occurred_at,
                ),
                timer_cancellations=timer_cancellations,
            )

    def prepare_patch_failure_followup(
        self,
        command: PreparePatchFailureFollowupCommand,
    ) -> ExecutionPatchFailureFollowupEffect | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            _session, state = required
            followup = (
                self._turn_execution.prepare_patch_failure_followup_locked(
                    state
                )
            )
            if followup is None:
                return None
            return ExecutionPatchFailureFollowupEffect(
                chat_id=command.target.binding[1],
                reply_text=followup.reply_text,
                prompt_message_id=followup.prompt_message_id,
                prompt_reply_in_thread=followup.prompt_reply_in_thread,
            )

    def capture_plan_card(
        self,
        command: CaptureExecutionPlanCardCommand,
    ) -> ExecutionPlanCardEffect | None:
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, _state = required
            return ExecutionPlanCardEffect(
                session=session,
                plan=session.plan,
            )

    def commit_plan_card(
        self,
        command: CommitExecutionPlanCardCommand,
    ) -> BindingSessionSnapshot | None:
        message_id = str(command.message_id or "")
        with self._lock:
            required = self._require_target_locked(command.target)
            if required is None:
                return None
            session, state = required
            if session.plan.message_id == message_id:
                return session
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                PlanStateChanged(plan_message_id=message_id),
            )
            return self._updated_session_locked(command.target.handle)

    def _require_target_locked(
        self,
        target: ExecutionOutputTarget,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict] | None:
        if type(target) is not ExecutionOutputTarget:
            raise TypeError("execution output transition requires a typed target")
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

    def _prepare_patch_timer_cancellation_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        return self._lifecycle.detach_timers_locked(
            binding,
            state,
            patch=True,
            mirror=False,
        )

    @staticmethod
    def _card_effect(
        session: BindingSessionSnapshot,
        *,
        occurred_at: float,
    ) -> ExecutionCardEffect | None:
        message_id = session.execution.current_message_id.strip()
        if not message_id and not session.execution.pages.send_outcome_unknown:
            return None
        started_at = session.execution.started_at
        return ExecutionCardEffect(
            session=session,
            message_id=message_id,
            transcript=session.execution.transcript,
            running=session.execution.running,
            elapsed=(
                int(max(0.0, occurred_at - started_at))
                if started_at
                else 0
            ),
            cancelled=session.execution.cancelled,
        )

    @staticmethod
    def _timer_presentation_key(session: BindingSessionSnapshot) -> str:
        message_id = session.execution.current_message_id.strip()
        if message_id:
            return f"message:{message_id}"
        pending = session.execution.pages.pending_page
        if pending is not None and session.execution.pages.send_outcome_unknown:
            return f"attempt:{pending.outbound_attempt_id}"
        return ""

    @staticmethod
    def _timer_ticket_matches(
        session: BindingSessionSnapshot,
        ticket: ExecutionPatchTimerTicket,
    ) -> bool:
        if (
            ticket.binding != session.binding
            or ticket.thread_id != session.current_thread_id.strip()
            or ticket.turn_id != session.execution.current_turn_id.strip()
        ):
            return False
        if ticket.page_attempt_id:
            pending = session.execution.pages.pending_page
            return bool(
                pending is not None
                and session.execution.pages.send_outcome_unknown
                and pending.outbound_attempt_id == ticket.page_attempt_id
                and ticket.card_message_id
                in {
                    session.execution.current_message_id.strip(),
                    pending.message_id.strip(),
                }
            )
        return bool(
            ticket.card_message_id
            and ticket.card_message_id
            == session.execution.current_message_id.strip()
        )

    @staticmethod
    def _require_time(value: float, *, field: str) -> None:
        if type(value) is not float:
            raise TypeError(f"execution output {field} must be an exact float")
        if value < 0:
            raise ValueError(f"execution output {field} must be non-negative")

    @staticmethod
    def _require_text(
        value: object,
        *,
        field: str,
        allow_empty: bool = False,
    ) -> str:
        if type(value) is not str:
            raise TypeError(f"execution output {field} must be an exact string")
        normalized = value.strip()
        if not normalized and not allow_empty:
            raise ValueError(f"execution output {field} cannot be empty")
        return normalized
