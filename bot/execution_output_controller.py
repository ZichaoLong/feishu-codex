from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Protocol

from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.card_text_projection import terminal_result_checksum
from bot.cards import build_terminal_result_card_message_content
from bot.execution_output_runtime import (
    CaptureExecutionPlanCardCommand,
    CommitExecutionPageSendUnknownReconciliationCommand,
    CommitExecutionPageRolloverCommand,
    CommitExecutionPlanCardCommand,
    CommitInitialExecutionPageCommand,
    ConsumeExecutionPatchTimerCommand,
    ExecutionCardEffect,
    ExecutionOutputRuntimeTransitions,
    ExecutionOutputTarget,
    ExecutionPatchTimerInstallPreparation,
    ImmediateExecutionCardFlush,
    InstallExecutionPatchTimerCommand,
    PrepareExecutionPageRolloverCommand,
    PrepareExecutionPageSendUnknownReconciliationCommand,
    PrepareInitialExecutionPageCommand,
    PrepareExecutionCardFlushCommand,
    PreparePatchFailureFollowupCommand,
    RollbackExecutionPatchTimerCommand,
    ScheduleExecutionCardCommand,
    SchedulePendingPageReconciliationCommand,
)
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.execution_pages import (
    ExecutionPresentationPage,
    ExecutionPageSendOutcome,
    ExecutionPageStatus,
    ExecutionTranscriptCursor,
    TerminalExecutionPageReceipt,
    require_terminal_execution_page_receipts,
)
from bot.feishu_outbound import FeishuOutboundEffect
from bot.runtime_card_publisher import (
    EXECUTION_PAGE_COMPONENT_LIMIT,
    EXECUTION_PAGE_PAYLOAD_LIMIT_BYTES,
    ExecutionCardPatchOutcome,
    ExecutionCardPatchStatus,
    ExecutionCardModel,
    RuntimeCardPublisher,
    build_execution_card_model,
    build_plan_card_model,
    execution_card_model_fits_page,
    fit_execution_card_page_end,
)
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
)


class _ReplyText(Protocol):
    def __call__(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> bool: ...


class _ReplyTextGetId(Protocol):
    def __call__(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str: ...


class _RecordTerminalResultCard(Protocol):
    def __call__(
        self,
        *,
        message_id: str,
        execution_message_id: str,
        final_reply_text: str,
        terminal_result_id: str = "",
        thread_id: str = "",
        checksum: str = "",
    ) -> None: ...


logger = logging.getLogger(__name__)

_PENDING_PAGE_RECONCILIATION_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ExecutionCardPresentationResult:
    """Resident session and optional patch outcome after presentation."""

    session: BindingSessionSnapshot
    patch_outcome: ExecutionCardPatchOutcome | None = None


class ExecutionOutputController:
    def __init__(
        self,
        *,
        runtime: ExecutionOutputRuntimeTransitions,
        runtime_submit: Callable[..., None],
        resolve_session: Callable[[str, str], BindingSessionSnapshot],
        card_publisher_factory: Callable[[], RuntimeCardPublisher],
        dispatch_execution_card_patch: Callable[
            [str, str, ExecutionCardModel], None
        ],
        reply_text: _ReplyText,
        reply_text_get_id: _ReplyTextGetId,
        record_terminal_result_card: _RecordTerminalResultCard,
        terminal_result_card_limit: Callable[[], int],
        stream_patch_interval_ms: Callable[[], int],
        execution_page_payload_limit_bytes: int = (
            EXECUTION_PAGE_PAYLOAD_LIMIT_BYTES
        ),
        execution_page_component_limit: int = EXECUTION_PAGE_COMPONENT_LIMIT,
    ) -> None:
        self._runtime = runtime
        self._runtime_submit = runtime_submit
        self._resolve_session = resolve_session
        self._card_publisher_factory = card_publisher_factory
        self._dispatch_execution_card_patch = dispatch_execution_card_patch
        self._reply_text = reply_text
        self._reply_text_get_id = reply_text_get_id
        self._record_terminal_result_card = record_terminal_result_card
        self._terminal_result_card_limit = terminal_result_card_limit
        self._stream_patch_interval_ms = stream_patch_interval_ms
        self._execution_page_payload_limit_bytes = int(
            execution_page_payload_limit_bytes
        )
        self._execution_page_component_limit = int(
            execution_page_component_limit
        )
        if self._execution_page_payload_limit_bytes < 1:
            raise ValueError("execution page payload limit must be positive")
        if self._execution_page_component_limit < 1:
            raise ValueError("execution page component limit must be positive")

    def open_initial_execution_page(
        self,
        captured: BindingSessionSnapshot,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
        reserved_message_id: str = "",
    ) -> InitialExecutionPageOpenResult:
        attempt_id = uuid.uuid4().hex
        preparation = self._runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id=attempt_id,
                known_message_id=str(reserved_message_id or "").strip(),
            )
        )
        if preparation is None:
            return InitialExecutionPageOpenResult(
                status=InitialExecutionPageOpenStatus.STALE,
                session=None,
            )

        publisher = self._card_publisher_factory()
        known_message_id = str(reserved_message_id or "").strip()
        cancelable = self._execution_is_cancelable(preparation.session)
        try:
            if known_message_id:
                patch = publisher.patch_execution_card(
                    preparation.session.binding[1],
                    known_message_id,
                    ExecutionCardModel.running_placeholder(
                        cancelable=cancelable
                    ),
                    attempt_id=attempt_id,
                )
                if patch.applied:
                    outcome = ExecutionPageSendOutcome.CONFIRMED
                    message_id = known_message_id
                elif patch.retryable or patch.status is ExecutionCardPatchStatus.UNKNOWN:
                    outcome = ExecutionPageSendOutcome.UNKNOWN
                    message_id = known_message_id
                else:
                    outcome = ExecutionPageSendOutcome.REJECTED
                    message_id = ""
            else:
                sent = publisher.send_execution_card(
                    preparation.session.binding[1],
                    parent_message_id,
                    reply_in_thread=reply_in_thread,
                    attempt_id=attempt_id,
                    cancelable=cancelable,
                )
                message_id = sent.message_id if sent.ok else ""
                outcome = (
                    ExecutionPageSendOutcome.CONFIRMED
                    if sent.ok
                    else (
                        ExecutionPageSendOutcome.UNKNOWN
                        if sent.effect is FeishuOutboundEffect.UNKNOWN
                        else ExecutionPageSendOutcome.REJECTED
                    )
                )
        except Exception:
            logger.exception(
                "initial execution page effect raised; preserving send_unknown"
            )
            outcome = ExecutionPageSendOutcome.UNKNOWN
            message_id = known_message_id

        committed = self._runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=preparation.receipt,
                outcome=outcome,
                message_id=message_id,
            )
        )
        if committed is None:
            return InitialExecutionPageOpenResult(
                status=InitialExecutionPageOpenStatus.STALE,
                session=None,
                message_id=message_id,
            )
        status = {
            ExecutionPageSendOutcome.CONFIRMED: InitialExecutionPageOpenStatus.ACTIVE,
            ExecutionPageSendOutcome.REJECTED: InitialExecutionPageOpenStatus.REJECTED,
            ExecutionPageSendOutcome.UNKNOWN: InitialExecutionPageOpenStatus.SEND_UNKNOWN,
        }[committed.outcome]
        if committed.outcome is ExecutionPageSendOutcome.UNKNOWN:
            self._schedule_pending_page_reconciliation(committed.session)
        return InitialExecutionPageOpenResult(
            status=status,
            session=committed.session,
            message_id=message_id,
        )

    def patch_execution_card_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
        cancelable: bool = True,
        cursor_start: ExecutionTranscriptCursor | None = None,
        cursor_end: ExecutionTranscriptCursor | None = None,
    ) -> ExecutionCardPatchOutcome:
        model = build_execution_card_model(
            transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
            cancelable=cancelable,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
        )
        return self._card_publisher_factory().patch_execution_card(
            chat_id,
            message_id,
            model,
        )

    def dispatch_execution_card_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
        cancelable: bool = True,
        cursor_start: ExecutionTranscriptCursor | None = None,
        cursor_end: ExecutionTranscriptCursor | None = None,
    ) -> None:
        model = build_execution_card_model(
            transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
            cancelable=cancelable,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
        )
        self._dispatch_execution_card_patch(chat_id, message_id, model)

    def _present_execution_card_effect(
        self,
        effect: ExecutionCardEffect,
        *,
        background: bool,
    ) -> ExecutionCardPresentationResult:
        """Patch one page or atomically advance through as many full pages as needed."""

        session = effect.session
        cancelable = self._execution_is_cancelable(session)
        unknown_active_page = None
        if session.execution.pages.send_outcome_unknown:
            unknown_active_page = session.execution.pages.active_page
            reconciled = self._reconcile_send_unknown(session)
            if reconciled is None:
                return ExecutionCardPresentationResult(session=session)
            session, reconciliation_outcome = reconciled
            if (
                reconciliation_outcome is ExecutionPageSendOutcome.CONFIRMED
                and unknown_active_page is not None
            ):
                sealed_page = session.execution.pages.pages[-2]
                assert sealed_page.cursor_end is not None
                sealed_model = build_execution_card_model(
                    session.execution.transcript,
                    running=False,
                    elapsed=(
                        int(
                            max(
                                0.0,
                                time.monotonic()
                                - session.execution.started_at,
                            )
                        )
                        if session.execution.started_at
                        else effect.elapsed
                    ),
                    cancelled=False,
                    cancelable=cancelable,
                    cursor_start=sealed_page.cursor_start,
                    cursor_end=sealed_page.cursor_end,
                )
                self._dispatch_execution_card_patch(
                    session.binding[1],
                    sealed_page.message_id,
                    sealed_model,
                )

        transcript = session.execution.transcript
        running = session.execution.running
        cancelled = session.execution.cancelled
        started_at = session.execution.started_at
        elapsed = (
            int(max(0.0, time.monotonic() - started_at))
            if started_at
            else effect.elapsed
        )

        while True:
            ledger = session.execution.pages
            active_page = ledger.active_page
            cursor_end = ledger.active_projection_end(transcript)
            if active_page is None or cursor_end is None:
                return ExecutionCardPresentationResult(session=session)
            model = build_execution_card_model(
                transcript,
                running=running,
                elapsed=elapsed,
                cancelled=cancelled,
                cancelable=cancelable,
                cursor_start=active_page.cursor_start,
                cursor_end=cursor_end,
            )
            if ledger.pending_page is not None or execution_card_model_fits_page(
                model,
                payload_limit_bytes=self._execution_page_payload_limit_bytes,
                component_limit=self._execution_page_component_limit,
            ):
                return ExecutionCardPresentationResult(
                    session=session,
                    patch_outcome=self._publish_execution_model(
                        session.binding[1],
                        active_page.message_id,
                        model,
                        background=background,
                    ),
                )

            rollover_cursor = fit_execution_card_page_end(
                transcript,
                cursor_start=active_page.cursor_start,
                cursor_end=cursor_end,
                running=running,
                elapsed=elapsed,
                cancelled=cancelled,
                cancelable=cancelable,
                payload_limit_bytes=self._execution_page_payload_limit_bytes,
                component_limit=self._execution_page_component_limit,
            )
            if not rollover_cursor.strictly_follows(active_page.cursor_start):
                raise RuntimeError("execution page rollover failed to advance")
            preparation = self._runtime.prepare_rollover(
                PrepareExecutionPageRolloverCommand(
                    target=ExecutionOutputTarget.from_session(session),
                    outbound_attempt_id=uuid.uuid4().hex,
                    cursor_start=rollover_cursor,
                )
            )
            if preparation is None:
                return ExecutionCardPresentationResult(session=session)

            attempt_id = preparation.receipt.opening_page.outbound_attempt_id
            try:
                sent = self._card_publisher_factory().send_execution_card(
                    preparation.session.binding[1],
                    preparation.session.execution.current_prompt_message_id,
                    reply_in_thread=(
                        preparation.session.execution.current_prompt_reply_in_thread
                    ),
                    attempt_id=attempt_id,
                    cancelable=cancelable,
                )
                message_id = sent.message_id if sent.ok else ""
                outcome = (
                    ExecutionPageSendOutcome.CONFIRMED
                    if sent.ok
                    else (
                        ExecutionPageSendOutcome.UNKNOWN
                        if sent.effect is FeishuOutboundEffect.UNKNOWN
                        else ExecutionPageSendOutcome.REJECTED
                    )
                )
            except Exception:
                logger.exception(
                    "execution page rollover effect raised; preserving send_unknown"
                )
                outcome = ExecutionPageSendOutcome.UNKNOWN
                message_id = ""

            committed = self._runtime.commit_rollover(
                CommitExecutionPageRolloverCommand(
                    receipt=preparation.receipt,
                    outcome=outcome,
                    message_id=message_id,
                )
            )
            if committed is None:
                return ExecutionCardPresentationResult(session=session)
            session = committed.session
            transcript = session.execution.transcript
            running = session.execution.running
            cancelled = session.execution.cancelled
            cancelable = self._execution_is_cancelable(session)
            started_at = session.execution.started_at
            elapsed = (
                int(max(0.0, time.monotonic() - started_at))
                if started_at
                else 0
            )

            if committed.outcome is ExecutionPageSendOutcome.CONFIRMED:
                sealed_page = session.execution.pages.pages[-2]
                assert sealed_page.cursor_end is not None
                sealed_model = build_execution_card_model(
                    transcript,
                    running=False,
                    elapsed=elapsed,
                    cancelled=False,
                    cancelable=cancelable,
                    cursor_start=sealed_page.cursor_start,
                    cursor_end=sealed_page.cursor_end,
                )
                self._dispatch_execution_card_patch(
                    session.binding[1],
                    sealed_page.message_id,
                    sealed_model,
                )
                continue

            if committed.outcome is ExecutionPageSendOutcome.UNKNOWN:
                self._schedule_pending_page_reconciliation(session)

            bounded_model = build_execution_card_model(
                transcript,
                running=running,
                elapsed=elapsed,
                cancelled=cancelled,
                cancelable=cancelable,
                cursor_start=active_page.cursor_start,
                cursor_end=rollover_cursor,
            )
            return ExecutionCardPresentationResult(
                session=session,
                patch_outcome=self._publish_execution_model(
                    session.binding[1],
                    active_page.message_id,
                    bounded_model,
                    background=background,
                ),
            )

    def _reconcile_send_unknown(
        self,
        captured: BindingSessionSnapshot,
    ) -> tuple[BindingSessionSnapshot, ExecutionPageSendOutcome] | None:
        preparation = self._runtime.prepare_send_unknown_reconciliation(
            PrepareExecutionPageSendUnknownReconciliationCommand(
                target=ExecutionOutputTarget.from_session(captured),
            )
        )
        if preparation is None:
            return None
        page = preparation.receipt.page
        outcome, message_id = self._reconcile_send_unknown_page_effect(
            preparation.session,
            page,
        )
        committed = self._runtime.commit_send_unknown_reconciliation(
            CommitExecutionPageSendUnknownReconciliationCommand(
                receipt=preparation.receipt,
                outcome=outcome,
                message_id=message_id,
            )
        )
        if committed is None:
            return None
        return committed.session, committed.outcome

    def _reconcile_send_unknown_page_effect(
        self,
        captured: BindingSessionSnapshot,
        page: ExecutionPresentationPage,
    ) -> tuple[ExecutionPageSendOutcome, str]:
        """Retry one exact page effect with its original external identity."""

        attempt_id = page.outbound_attempt_id
        try:
            publisher = self._card_publisher_factory()
            if page.message_id:
                patched = publisher.patch_execution_card(
                    captured.binding[1],
                    page.message_id,
                    ExecutionCardModel.running_placeholder(
                        cancelable=self._execution_is_cancelable(captured)
                    ),
                    attempt_id=attempt_id,
                )
                if patched.applied:
                    outcome = ExecutionPageSendOutcome.CONFIRMED
                    message_id = page.message_id
                elif (
                    patched.retryable
                    or patched.status is ExecutionCardPatchStatus.UNKNOWN
                ):
                    outcome = ExecutionPageSendOutcome.UNKNOWN
                    message_id = ""
                else:
                    outcome = ExecutionPageSendOutcome.REJECTED
                    message_id = ""
            else:
                sent = publisher.send_execution_card(
                    captured.binding[1],
                    captured.execution.current_prompt_message_id,
                    reply_in_thread=(
                        captured.execution.current_prompt_reply_in_thread
                    ),
                    attempt_id=attempt_id,
                    cancelable=self._execution_is_cancelable(captured),
                )
                outcome = (
                    ExecutionPageSendOutcome.CONFIRMED
                    if sent.ok
                    else (
                        ExecutionPageSendOutcome.UNKNOWN
                        if sent.effect is FeishuOutboundEffect.UNKNOWN
                        else ExecutionPageSendOutcome.REJECTED
                    )
                )
                message_id = sent.message_id if sent.ok else ""
        except Exception:
            logger.exception(
                "execution page reconciliation raised; preserving send_unknown"
            )
            outcome = ExecutionPageSendOutcome.UNKNOWN
            message_id = ""
        return outcome, message_id

    def _publish_execution_model(
        self,
        chat_id: str,
        message_id: str,
        model: ExecutionCardModel,
        *,
        background: bool,
    ) -> ExecutionCardPatchOutcome | None:
        if background:
            self._dispatch_execution_card_patch(chat_id, message_id, model)
            return None
        return self._card_publisher_factory().patch_execution_card(
            chat_id,
            message_id,
            model,
        )

    def present_terminal_execution_card(
        self,
        captured: BindingSessionSnapshot,
        *,
        background: bool = True,
    ) -> tuple[TerminalExecutionPageReceipt, ...]:
        """Project a completed turn from immutable presentation facts.

        This path deliberately performs no binding-runtime transition.  The
        main turn may already be retired and a successor may already be
        active; page delivery therefore cannot delay or reopen that lifecycle.
        A pre-retirement ``send_unknown`` page is reconciled at most once with
        its original UUID. New terminal pages are attempted once with fresh
        outbound UUIDs. An unknown result abandons only that exact page effect.
        """

        if type(captured) is not BindingSessionSnapshot:
            raise TypeError("terminal presentation requires an exact session")
        if captured.execution.running:
            raise RuntimeError(
                "terminal execution presentation requires a settled execution state"
        )
        ledger = captured.execution.pages
        transcript = captured.execution.transcript
        terminal_end = ExecutionTranscriptCursor.from_transcript(transcript)
        receipts: list[TerminalExecutionPageReceipt] = []
        confirmed_message_ids: set[str] = set()

        def confirmed_receipts() -> tuple[TerminalExecutionPageReceipt, ...]:
            return require_terminal_execution_page_receipts(
                tuple(receipts),
                field="terminal execution presentation receipts",
            )

        for page in ledger.pages:
            if page.status is not ExecutionPageStatus.SEALED:
                continue
            assert page.cursor_end is not None
            receipts.append(
                TerminalExecutionPageReceipt(
                    message_id=page.message_id,
                    cursor_start=page.cursor_start,
                    cursor_end=page.cursor_end,
                )
            )
            confirmed_message_ids.add(page.message_id)
        elapsed = (
            int(max(0.0, time.monotonic() - captured.execution.started_at))
            if captured.execution.started_at
            else 0
        )

        pending_page = ledger.pending_page
        if pending_page is not None:
            if not terminal_end.follows(pending_page.cursor_start):
                raise RuntimeError(
                    "terminal transcript regressed behind its pending page"
                )
            previous_active = ledger.active_page
            if previous_active is not None:
                sealed_model = build_execution_card_model(
                    transcript,
                    running=False,
                    elapsed=elapsed,
                    cancelled=False,
                    cursor_start=previous_active.cursor_start,
                    cursor_end=pending_page.cursor_start,
                )
                self._publish_execution_model(
                    captured.binding[1],
                    previous_active.message_id,
                    sealed_model,
                    background=background,
                )
                receipts.append(
                    TerminalExecutionPageReceipt(
                        message_id=previous_active.message_id,
                        cursor_start=previous_active.cursor_start,
                        cursor_end=pending_page.cursor_start,
                    )
                )
                confirmed_message_ids.add(previous_active.message_id)
            if (
                pending_page.status is not ExecutionPageStatus.SEND_UNKNOWN
                or pending_page.reconciliation_attempted
            ):
                return confirmed_receipts()
            outcome, reconciled_message_id = (
                self._reconcile_send_unknown_page_effect(captured, pending_page)
            )
            if outcome is not ExecutionPageSendOutcome.CONFIRMED:
                return confirmed_receipts()
            if reconciled_message_id in confirmed_message_ids:
                logger.error(
                    "terminal execution page reconciliation returned a duplicate "
                    "message id; preserving the confirmed receipt prefix: %s",
                    reconciled_message_id,
                )
                return confirmed_receipts()
            ledger = ledger.confirm_send_unknown(
                expected_page=pending_page,
                message_id=reconciled_message_id,
            )

        active_page = ledger.active_page
        if active_page is None:
            return confirmed_receipts()
        cursor_start = active_page.cursor_start
        if not terminal_end.follows(cursor_start):
            raise RuntimeError("terminal transcript regressed behind its active page")
        message_id = active_page.message_id
        publisher = self._card_publisher_factory()

        while True:
            full_model = build_execution_card_model(
                transcript,
                running=False,
                elapsed=elapsed,
                cancelled=captured.execution.cancelled,
                cursor_start=cursor_start,
                cursor_end=terminal_end,
            )
            if execution_card_model_fits_page(
                full_model,
                payload_limit_bytes=self._execution_page_payload_limit_bytes,
                component_limit=self._execution_page_component_limit,
            ):
                self._publish_execution_model(
                    captured.binding[1],
                    message_id,
                    full_model,
                    background=background,
                )
                receipts.append(
                    TerminalExecutionPageReceipt(
                        message_id=message_id,
                        cursor_start=cursor_start,
                        cursor_end=terminal_end,
                    )
                )
                confirmed_message_ids.add(message_id)
                return confirmed_receipts()

            page_end = fit_execution_card_page_end(
                transcript,
                cursor_start=cursor_start,
                cursor_end=terminal_end,
                running=False,
                elapsed=elapsed,
                cancelled=False,
                payload_limit_bytes=self._execution_page_payload_limit_bytes,
                component_limit=self._execution_page_component_limit,
            )
            if not page_end.strictly_follows(cursor_start):
                raise RuntimeError("terminal execution pagination failed to advance")
            page_model = build_execution_card_model(
                transcript,
                running=False,
                elapsed=elapsed,
                cancelled=False,
                cursor_start=cursor_start,
                cursor_end=page_end,
            )
            self._publish_execution_model(
                captured.binding[1],
                message_id,
                page_model,
                background=background,
            )
            receipts.append(
                TerminalExecutionPageReceipt(
                    message_id=message_id,
                    cursor_start=cursor_start,
                    cursor_end=page_end,
                )
            )
            confirmed_message_ids.add(message_id)

            attempt_id = uuid.uuid4().hex
            try:
                sent = publisher.send_execution_card(
                    captured.binding[1],
                    captured.execution.current_prompt_message_id,
                    reply_in_thread=(
                        captured.execution.current_prompt_reply_in_thread
                    ),
                    attempt_id=attempt_id,
                    cancelable=self._execution_is_cancelable(captured),
                )
            except Exception:
                logger.exception(
                    "terminal execution page effect raised; abandoning exact page"
                )
                return confirmed_receipts()
            if not sent.ok or not sent.message_id:
                return confirmed_receipts()
            if sent.message_id in confirmed_message_ids:
                logger.error(
                    "detached terminal execution page returned a duplicate message "
                    "id; preserving the confirmed receipt prefix: %s",
                    sent.message_id,
                )
                return confirmed_receipts()
            cursor_start = page_end
            message_id = sent.message_id

    def schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        self.schedule_execution_card_update_for_session(
            self._resolve_session(sender_id, chat_id)
        )

    def schedule_execution_card_update_for_session(
        self,
        captured: BindingSessionSnapshot,
    ) -> None:
        transition = self._runtime.prepare_schedule(
            ScheduleExecutionCardCommand(
                target=ExecutionOutputTarget.from_session(captured),
                occurred_at=time.monotonic(),
                interval_seconds=int(self._stream_patch_interval_ms()) / 1000,
            )
        )
        if transition is None:
            return
        cancel_runtime_timer_effects(transition.timer_cancellations)
        if type(transition) is ImmediateExecutionCardFlush:
            self.flush_execution_card_for_session(
                transition.session,
                background=True,
            )
            return
        if type(transition) is not ExecutionPatchTimerInstallPreparation:
            raise TypeError("unknown execution output schedule transition")
        self._install_patch_timer(transition)

    def _schedule_pending_page_reconciliation(
        self,
        captured: BindingSessionSnapshot,
    ) -> None:
        preparation = self._runtime.prepare_pending_page_reconciliation_timer(
            SchedulePendingPageReconciliationCommand(
                target=ExecutionOutputTarget.from_session(captured),
                delay_seconds=_PENDING_PAGE_RECONCILIATION_DELAY_SECONDS,
            )
        )
        if preparation is not None:
            self._install_patch_timer(preparation)

    def _install_patch_timer(
        self,
        transition: ExecutionPatchTimerInstallPreparation,
    ) -> None:
        timer = threading.Timer(
            transition.delay_seconds,
            self.submit_execution_card_patch_timer,
            args=(transition.ticket,),
        )
        timer.daemon = True
        registration = ExecutionPatchTimerRegistration(
            ticket=transition.ticket,
            timer=timer,
        )
        installed = self._runtime.install_patch_timer(
            InstallExecutionPatchTimerCommand(
                target=ExecutionOutputTarget.from_session(transition.session),
                registration=registration,
            )
        )
        if installed is None:
            timer.cancel()
            return
        try:
            timer.start()
        except BaseException:
            self._runtime.rollback_patch_timer_start(
                RollbackExecutionPatchTimerCommand(
                    handle=installed.handle,
                    registration=registration,
                )
            )
            timer.cancel()
            raise

    def submit_execution_card_patch_timer(self, ticket: ExecutionPatchTimerTicket) -> None:
        self._runtime_submit(self.consume_execution_card_patch_timer, ticket)

    def consume_execution_card_patch_timer(self, ticket: ExecutionPatchTimerTicket) -> None:
        """Consume one exact delayed patch without touching a replacement."""

        effect = self._runtime.consume_patch_timer(
            ConsumeExecutionPatchTimerCommand(
                ticket=ticket,
                occurred_at=time.monotonic(),
            )
        )
        if effect is None:
            return
        self._present_execution_card_effect(
            effect,
            background=True,
        )

    def flush_execution_card(
        self,
        sender_id: str,
        chat_id: str,
        immediate: bool = False,
        *,
        background: bool = False,
    ) -> None:
        self.flush_execution_card_for_session(
            self._resolve_session(sender_id, chat_id),
            immediate=immediate,
            background=background,
        )

    def flush_execution_card_for_session(
        self,
        captured: BindingSessionSnapshot,
        immediate: bool = False,
        *,
        background: bool = False,
    ) -> None:
        transition = self._runtime.prepare_flush(
            PrepareExecutionCardFlushCommand(
                target=ExecutionOutputTarget.from_session(captured),
                occurred_at=time.monotonic(),
            )
        )
        if transition is None:
            return
        cancel_runtime_timer_effects(transition.timer_cancellations)
        if transition.effect is None:
            return
        effect = transition.effect
        if background:
            self._present_execution_card_effect(
                effect,
                background=True,
            )
            return

        presentation = self._present_execution_card_effect(
            effect,
            background=False,
        )
        outcome = presentation.patch_outcome
        presented = presentation.session
        if (
            outcome is not None
            and outcome.safe_to_fallback
            and immediate
            and effect.reply_text
        ):
            followup = self._runtime.prepare_patch_failure_followup(
                PreparePatchFailureFollowupCommand(
                    target=ExecutionOutputTarget.from_session(presented),
                )
            )
            if followup is not None:
                self._reply_text(
                    followup.chat_id,
                    followup.reply_text,
                    message_id=followup.prompt_message_id,
                    reply_in_thread=followup.prompt_reply_in_thread,
                )

    def publish_terminal_result(
        self,
        chat_id: str,
        *,
        final_reply_text: str,
        source_execution_message_id: str = "",
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool:
        raw_text = str(final_reply_text or "")
        if not raw_text.strip():
            return False
        terminal_result_id = uuid.uuid4().hex
        checksum = terminal_result_checksum(raw_text)
        budget = int(self._terminal_result_card_limit())
        card_content = build_terminal_result_card_message_content(
            raw_text,
            terminal_result_id=terminal_result_id,
            checksum=checksum,
        )
        if len(card_content.encode("utf-8")) <= budget:
            published = self._card_publisher_factory().publish_terminal_result_card(
                chat_id=chat_id,
                parent_message_id=prompt_message_id,
                final_reply_text=raw_text,
                terminal_result_id=terminal_result_id,
                checksum=checksum,
                reply_in_thread=prompt_reply_in_thread,
            )
            if published.ok:
                self._record_terminal_result_card(
                    message_id=published.message_id,
                    execution_message_id=str(source_execution_message_id or "").strip(),
                    final_reply_text=raw_text,
                    terminal_result_id=terminal_result_id,
                    thread_id=thread_id,
                    checksum=checksum,
                )
                return True
            if not published.safe_to_fallback:
                return False
        text_message_id = self._reply_text_get_id(
            chat_id,
            raw_text,
            message_id=prompt_message_id,
            reply_in_thread=prompt_reply_in_thread,
        )
        if text_message_id:
            self._record_terminal_result_card(
                message_id=text_message_id,
                execution_message_id=str(source_execution_message_id or "").strip(),
                final_reply_text=raw_text,
                terminal_result_id=terminal_result_id,
                thread_id=thread_id,
                checksum=checksum,
            )
            return True
        return False

    @staticmethod
    def _execution_is_cancelable(session: BindingSessionSnapshot) -> bool:
        return (
            session.execution.current_execution_kind.strip()
            != ACTIVE_OBSERVER_EXECUTION_KIND
        )

    def flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        self.flush_plan_card_for_session(self._resolve_session(sender_id, chat_id))

    def flush_plan_card_for_session(
        self,
        captured: BindingSessionSnapshot,
    ) -> None:
        effect = self._runtime.capture_plan_card(
            CaptureExecutionPlanCardCommand(
                target=ExecutionOutputTarget.from_session(
                    captured,
                    include_plan=True,
                )
            )
        )
        if effect is None:
            return
        prepared = effect.session
        model = build_plan_card_model(effect.plan)
        if model.is_empty:
            return
        result = self._card_publisher_factory().publish_plan_card(
            chat_id=prepared.binding[1],
            parent_message_id=prepared.execution.current_message_id,
            plan_message_id=prepared.plan.message_id,
            model=model,
            reply_in_thread=prepared.execution.current_prompt_reply_in_thread,
        )
        if result.outcome_unknown:
            return
        desired_message_id = prepared.plan.message_id
        if result.attempted_existing and not result.reused_existing:
            desired_message_id = ""
        if result.message_id and result.message_id != prepared.plan.message_id:
            desired_message_id = result.message_id
        if desired_message_id == prepared.plan.message_id:
            return
        self._runtime.commit_plan_card(
            CommitExecutionPlanCardCommand(
                target=ExecutionOutputTarget.from_session(
                    prepared,
                    include_plan=True,
                ),
                message_id=desired_message_id,
            )
        )
