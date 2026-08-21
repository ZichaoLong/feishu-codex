from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Literal, Protocol

from bot.adapters.base import ThreadSnapshot
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.execution_pages import (
    ExecutionTranscriptCursor,
    TerminalExecutionPageReceipt,
    TerminalPageCleanupOutcome,
    TerminalPageCleanupReason,
    terminal_reply_interval_coverage,
)
from bot.execution_transcript import (
    ExecutionTranscript,
    ExecutionTranscriptTrailingReplyProjection,
    agent_message_can_be_terminal_candidate,
    is_terminal_invalidating_work_item_type,
)
from bot.execution_recovery_runtime import (
    ApplyExecutionSnapshotCommand,
    CaptureTerminalReconcileTargetCommand,
    ConsumeMirrorWatchdogCommand,
    ExecutionRecoveryRuntimeTransitions,
    ExecutionRuntimeObservationFence,
    ExecutionSnapshotTransition,
    InstallMirrorWatchdogCommand,
    MarkExecutionRuntimeDegradedCommand,
    MirrorWatchdogEffect,
    MirrorWatchdogTarget,
    PrepareMirrorWatchdogCommand,
    PrepareSnapshotReconcileCommand,
    PrepareTerminalFallbackCommand,
    RecoverySnapshotReplyItem,
    RecoveryFinalizationPreparation,
    SnapshotReconcilePreparation,
    RollbackMirrorWatchdogCommand,
    TerminalReconcileTarget,
)
from bot.execution_recovery_worker import ExecutionRecoveryWorkerRegistry
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
)

logger = logging.getLogger(__name__)
_NO_VALID_TERMINAL_REPLY_TEXT = "本轮未生成有效终态回复"


class _ExecutionFinalizationResult(Protocol):
    had_card: bool
    retired: bool
    terminal_page_receipts: tuple[TerminalExecutionPageReceipt, ...]


class _GenerationPinnedThreadRead(Protocol):
    def __call__(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> ThreadSnapshot: ...


@dataclass(frozen=True, slots=True)
class SnapshotReplyProjection:
    kind: Literal["agent_text", "agent_empty", "turn_error", "unavailable"]
    full_reply_text: str
    final_reply_text: str
    reply_items: tuple[RecoverySnapshotReplyItem, ...]
    turn_id: str = ""
    turn_status: str = ""
    invalidates_local_agent_evidence: bool = False
    final_reply_item_id: str = ""


@dataclass(frozen=True, slots=True)
class TerminalReplySelection:
    kind: Literal["agent_text", "agent_empty", "turn_error", "unavailable"]
    source: Literal["snapshot", "local", "none"]
    final_reply_text: str = ""


@dataclass(frozen=True, slots=True)
class MirrorWatchdogSnapshotPreparation:
    """Exact RuntimeLoop receipt for one loop-external watchdog read."""

    snapshot: SnapshotReconcilePreparation
    connection_generation: int

    def __post_init__(self) -> None:
        if type(self.snapshot) is not SnapshotReconcilePreparation:
            raise TypeError("watchdog snapshot preparation requires an exact receipt")
        if (
            type(self.connection_generation) is not int
            or self.connection_generation <= 0
        ):
            raise ValueError(
                "watchdog snapshot preparation requires a positive connection generation"
            )


class ExecutionRecoveryController:
    def __init__(
        self,
        *,
        runtime: ExecutionRecoveryRuntimeTransitions,
        runtime_call: Callable[..., object],
        capture_connection_generation: Callable[[], int],
        run_if_connection_generation: Callable[
            [int, Callable[[], object]], object
        ],
        resolve_session: Callable[[str, str], BindingSessionSnapshot],
        finalize_execution: Callable[
            [BindingSessionSnapshot],
            _ExecutionFinalizationResult | None,
        ],
        prepare_execution_finalization: Callable[
            [BindingSessionSnapshot], object | None
        ],
        present_execution_finalization: Callable[
            [object], _ExecutionFinalizationResult | None
        ],
        mark_compact_start_outcome_unknown: Callable[
            [BindingSessionSnapshot, str], None
        ],
        dispatch_execution_card_message: Callable[..., None],
        publish_terminal_result: Callable[..., bool],
        has_recorded_terminal_result: Callable[..., bool],
        deliver_generated_images_from_snapshot: Callable[..., int],
        read_thread: _GenerationPinnedThreadRead,
        is_thread_not_found_error: Callable[[Exception], bool],
        is_turn_thread_not_found_error: Callable[[Exception], bool],
        is_pre_send_error: Callable[[Exception], bool],
        is_transport_disconnect: Callable[[Exception], bool],
        is_request_timeout_error: Callable[[Exception], bool],
        runtime_recovery_reason: Callable[[Exception], str],
        mirror_watchdog_seconds: Callable[[], float],
        compact_start_timeout_seconds: Callable[[], float],
        terminal_empty_retry_count: Callable[[], int],
        terminal_empty_retry_delay_seconds: Callable[[], float],
    ) -> None:
        self._runtime = runtime
        self._runtime_call = runtime_call
        self._capture_connection_generation = capture_connection_generation
        self._run_if_connection_generation = run_if_connection_generation
        self._resolve_session = resolve_session
        self._finalize_execution = finalize_execution
        self._prepare_execution_finalization = prepare_execution_finalization
        self._present_execution_finalization = present_execution_finalization
        self._mark_compact_start_outcome_unknown = mark_compact_start_outcome_unknown
        self._dispatch_execution_card_message = dispatch_execution_card_message
        self._publish_terminal_result = publish_terminal_result
        self._has_recorded_terminal_result = has_recorded_terminal_result
        self._deliver_generated_images_from_snapshot = deliver_generated_images_from_snapshot
        self._read_thread = read_thread
        self._is_thread_not_found_error = is_thread_not_found_error
        self._is_turn_thread_not_found_error = is_turn_thread_not_found_error
        self._is_pre_send_error = is_pre_send_error
        self._is_transport_disconnect = is_transport_disconnect
        self._is_request_timeout_error = is_request_timeout_error
        self._runtime_recovery_reason = runtime_recovery_reason
        self._mirror_watchdog_seconds = mirror_watchdog_seconds
        self._compact_start_timeout_seconds = compact_start_timeout_seconds
        self._terminal_empty_retry_count = terminal_empty_retry_count
        self._terminal_empty_retry_delay_seconds = terminal_empty_retry_delay_seconds
        self._workers = ExecutionRecoveryWorkerRegistry()

    @staticmethod
    def _finalization_succeeded(
        finalization: _ExecutionFinalizationResult | None,
    ) -> bool:
        return bool(
            finalization is not None
            and finalization.had_card
            and finalization.retired
        )

    @staticmethod
    def _attach_terminal_page_receipts(
        target: TerminalReconcileTarget | None,
        finalization: _ExecutionFinalizationResult | None,
    ) -> TerminalReconcileTarget | None:
        if (
            target is None
            or finalization is None
            or not finalization.terminal_page_receipts
        ):
            return target
        return replace(
            target,
            terminal_page_receipts=finalization.terminal_page_receipts,
        )

    def capture_terminal_reconcile_target(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> TerminalReconcileTarget | None:
        return self.capture_terminal_reconcile_target_for_session(
            self._resolve_session(sender_id, chat_id),
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def capture_terminal_reconcile_target_for_session(
        self,
        captured: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> TerminalReconcileTarget | None:
        return self._runtime.capture_terminal_target(
            CaptureTerminalReconcileTargetCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=str(thread_id or "").strip(),
                turn_id=str(turn_id or "").strip(),
                occurred_at=time.monotonic(),
            )
        )

    @staticmethod
    def _display_changed(previous: ExecutionTranscript, updated: ExecutionTranscript) -> bool:
        return (
            previous.process_blocks != updated.process_blocks
            or previous.reply_segments != updated.reply_segments
        )

    @staticmethod
    def _transcript_has_visible_execution_output(transcript: ExecutionTranscript) -> bool:
        return transcript.has_process_output() or transcript.has_reply_output()

    @staticmethod
    def _terminal_coordinate_interval(
        transcript: ExecutionTranscript,
        *,
        expected_text: str,
    ) -> tuple[tuple[int, int] | None, TerminalPageCleanupReason]:
        interval, reason = transcript.terminal_agent_reply_interval(expected_text)
        cleanup_reasons: dict[str, TerminalPageCleanupReason] = {
            "matched": "cleanup_scheduled",
            "coordinate_unavailable": "local_coordinate_unavailable",
            "raw_text_mismatch": "raw_text_mismatch",
            "not_trailing": "terminal_not_trailing",
        }
        return interval, cleanup_reasons[reason]

    @classmethod
    def _snapshot_terminal_cleanup_interval(
        cls,
        *,
        page_transcript: ExecutionTranscript,
        snapshot_transcript: ExecutionTranscript,
        projection: SnapshotReplyProjection,
    ) -> tuple[tuple[int, int] | None, TerminalPageCleanupReason]:
        coordinate = page_transcript.terminal_agent_reply_coordinate()
        if (
            coordinate is not None
            and coordinate.item_id
            and projection.final_reply_item_id
        ):
            if coordinate.item_id != projection.final_reply_item_id:
                return None, "item_identity_mismatch"
            return cls._terminal_coordinate_interval(
                page_transcript,
                expected_text=projection.final_reply_text,
            )
        if (
            coordinate is not None
            and coordinate.raw_text != projection.final_reply_text
        ):
            return None, "raw_text_mismatch"
        if page_transcript.reply_segments != snapshot_transcript.reply_segments:
            return None, "snapshot_projection_mismatch"
        interval = snapshot_transcript.trailing_reply_interval(
            projection.final_reply_text
        )
        if interval is None:
            return None, "terminal_not_trailing"
        return interval, "cleanup_scheduled"

    @staticmethod
    def _log_terminal_page_cleanup_outcome(
        *,
        target: TerminalReconcileTarget,
        outcome: TerminalPageCleanupOutcome,
        final_reply_chars: int,
    ) -> None:
        logger.info(
            "terminal execution page cleanup: status=%s reason=%s "
            "chat=%s thread=%s turn=%s message_id=%s final_chars=%d "
            "receipts=%d attempted=%d scheduled=%d",
            outcome.status,
            outcome.reason,
            target.chat_id,
            target.thread_id[:12],
            target.turn_id[:12],
            target.card_message_id,
            final_reply_chars,
            len(target.terminal_page_receipts),
            outcome.attempted_receipts,
            outcome.scheduled_patches,
        )

    @classmethod
    def _retain_terminal_page_cleanup(
        cls,
        *,
        target: TerminalReconcileTarget,
        reason: TerminalPageCleanupReason,
        final_reply_chars: int,
    ) -> TerminalPageCleanupOutcome:
        outcome = TerminalPageCleanupOutcome.retained(reason)
        cls._log_terminal_page_cleanup_outcome(
            target=target, outcome=outcome, final_reply_chars=final_reply_chars
        )
        return outcome

    def _remove_terminal_reply_from_confirmed_pages(
        self,
        *,
        target: TerminalReconcileTarget,
        transcript: ExecutionTranscript,
        interval: tuple[int, int],
        cancelled: bool,
        elapsed: int,
        refresh_all_status: bool = False,
    ) -> TerminalPageCleanupOutcome:
        if not target.terminal_page_receipts:
            return self._retain_terminal_page_cleanup(
                target=target,
                reason="confirmed_receipts_missing",
                final_reply_chars=max(interval[1] - interval[0], 0),
            )
        start_chars, end_chars = interval
        if (
            type(start_chars) is not int
            or type(end_chars) is not int
            or start_chars < 0
            or end_chars <= start_chars
            or end_chars != transcript.reply_content_chars()
        ):
            return self._retain_terminal_page_cleanup(
                target=target,
                reason="terminal_not_trailing",
                final_reply_chars=max(end_chars - start_chars, 0),
            )
        coverage = terminal_reply_interval_coverage(
            target.terminal_page_receipts,
            start_reply_chars=start_chars,
            end_reply_chars=end_chars,
        )
        if not coverage.receipts:
            return self._retain_terminal_page_cleanup(
                target=target,
                reason="interval_not_fully_covered",
                final_reply_chars=end_chars - start_chars,
            )
        projected = ExecutionTranscriptTrailingReplyProjection(
            transcript=transcript,
            retained_reply_chars=start_chars,
        )
        intersecting_message_ids = {
            receipt.message_id for receipt in coverage.receipts
        }
        attempted_receipts = 0
        scheduled_patches = 0
        for receipt in target.terminal_page_receipts:
            intersects_final = receipt.message_id in intersecting_message_ids
            if not intersects_final and not refresh_all_status:
                continue
            attempted_receipts += 1
            try:
                self._dispatch_execution_card_message(
                    target.chat_id,
                    receipt.message_id,
                    transcript=projected if intersects_final else transcript,
                    running=False,
                    elapsed=elapsed,
                    cancelled=cancelled,
                    cursor_start=receipt.cursor_start,
                    cursor_end=receipt.cursor_end,
                )
                scheduled_patches += 1
            except Exception:
                logger.exception(
                    "terminal execution page cleanup failed; retaining local "
                    "duplication: message_id=%s",
                    receipt.message_id,
                )
        if scheduled_patches < attempted_receipts:
            outcome = TerminalPageCleanupOutcome(
                status="partial",
                reason="patch_dispatch_partial",
                attempted_receipts=attempted_receipts,
                scheduled_patches=scheduled_patches,
            )
        elif not coverage.fully_covered:
            outcome = TerminalPageCleanupOutcome(
                status="partial",
                reason="interval_not_fully_covered",
                attempted_receipts=attempted_receipts,
                scheduled_patches=scheduled_patches,
            )
        else:
            outcome = TerminalPageCleanupOutcome(
                status="scheduled",
                reason="cleanup_scheduled",
                attempted_receipts=attempted_receipts,
                scheduled_patches=scheduled_patches,
            )
        self._log_terminal_page_cleanup_outcome(
            target=target,
            outcome=outcome,
            final_reply_chars=end_chars - start_chars,
        )
        return outcome

    def _refresh_confirmed_page_status(
        self,
        *,
        target: TerminalReconcileTarget,
        transcript: ExecutionTranscript,
        cancelled: bool,
        elapsed: int,
    ) -> None:
        for receipt in target.terminal_page_receipts:
            try:
                self._dispatch_execution_card_message(
                    target.chat_id,
                    receipt.message_id,
                    transcript=transcript,
                    running=False,
                    elapsed=elapsed,
                    cancelled=cancelled,
                    cursor_start=receipt.cursor_start,
                    cursor_end=receipt.cursor_end,
                )
            except Exception:
                logger.exception(
                    "terminal execution page status refresh failed; retaining "
                    "the prior page: message_id=%s",
                    receipt.message_id,
                )

    @staticmethod
    def _can_remove_terminal_only_execution_card(
        transcript: ExecutionTranscript,
        *,
        final_reply_text: str,
    ) -> bool:
        if transcript.has_process_output():
            return False
        normalized_final = str(final_reply_text or "").strip()
        if not normalized_final:
            return False
        assistant_segments = [
            segment.text.strip()
            for segment in transcript.reply_segments
            if segment.kind == "assistant" and segment.text.strip()
        ]
        return assistant_segments == [normalized_final]

    @staticmethod
    def _transcript_from_snapshot_projection(
        base: ExecutionTranscript,
        *,
        projection: SnapshotReplyProjection,
        drop_last_text_message: bool,
    ) -> ExecutionTranscript:
        transcript = base.clone()
        transcript.set_reply_text("")
        transcript.rebuild_reply_from_snapshot_items(
            [
                {
                    "type": item.item_type,
                    **({"text": item.text} if item.text_available else {}),
                }
                for item in projection.reply_items
            ],
            fallback_text="" if drop_last_text_message else projection.full_reply_text,
            drop_last_text_message=drop_last_text_message,
        )
        return transcript

    def _present_no_valid_terminal_reply(
        self,
        *,
        target: TerminalReconcileTarget,
        transcript: ExecutionTranscript,
        cancelled: bool,
        elapsed: int,
    ) -> None:
        """Show an honest local explanation without recording terminal text."""

        display = transcript.clone()
        display.append_display_only_reply(_NO_VALID_TERMINAL_REPLY_TEXT)
        display_end = ExecutionTranscriptCursor.from_transcript(display)
        process_start = min(
            target.cursor_start.process_chars,
            display_end.process_chars,
        )
        self._dispatch_execution_card_message(
            target.chat_id,
            target.card_message_id,
            transcript=display,
            running=False,
            elapsed=elapsed,
            cancelled=cancelled,
            cursor_start=ExecutionTranscriptCursor(
                process_chars=process_start,
                reply_chars=min(
                    target.cursor_start.reply_chars,
                    display_end.reply_chars,
                ),
            ),
            cursor_end=display_end,
        )

    def _refresh_terminal_execution_card_if_changed(
        self,
        *,
        chat_id: str,
        execution_message_id: str,
        current_transcript: ExecutionTranscript,
        display_transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
        cursor_start: ExecutionTranscriptCursor,
        cursor_end: ExecutionTranscriptCursor,
    ) -> bool:
        transcript_changed = self._display_changed(current_transcript, display_transcript)
        cancelled_changed = current_cancelled != cancelled
        if not (transcript_changed or cancelled_changed):
            return False
        display_end = ExecutionTranscriptCursor.from_transcript(
            display_transcript
        )
        projection_start = ExecutionTranscriptCursor(
            process_chars=min(cursor_start.process_chars, display_end.process_chars),
            reply_chars=min(cursor_start.reply_chars, display_end.reply_chars),
        )
        projection_end = ExecutionTranscriptCursor(
            process_chars=max(
                projection_start.process_chars,
                min(cursor_end.process_chars, display_end.process_chars),
            ),
            reply_chars=max(
                projection_start.reply_chars,
                min(cursor_end.reply_chars, display_end.reply_chars),
            ),
        )
        self._dispatch_execution_card_message(
            chat_id,
            execution_message_id,
            transcript=display_transcript,
            running=False,
            elapsed=elapsed,
            cancelled=cancelled,
            cursor_start=projection_start,
            cursor_end=projection_end,
        )
        return True

    def _publish_terminal_result_if_needed(
        self,
        *,
        target: TerminalReconcileTarget,
        final_reply_text: str,
    ) -> bool:
        raw_text = str(final_reply_text or "")
        if not raw_text.strip():
            return False
        if self._has_recorded_terminal_result(
            execution_message_id=target.card_message_id,
            final_reply_text=raw_text,
        ):
            return True
        published = self._publish_terminal_result(
            target.chat_id,
            final_reply_text=raw_text,
            source_execution_message_id=target.card_message_id,
            prompt_message_id=target.prompt_message_id,
            prompt_reply_in_thread=target.prompt_reply_in_thread,
            thread_id=target.thread_id,
        )
        return published

    def _apply_terminal_snapshot_projection(
        self,
        *,
        target: TerminalReconcileTarget,
        current_transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
        projection: SnapshotReplyProjection,
    ) -> bool:
        full_transcript = self._transcript_from_snapshot_projection(
            current_transcript,
            projection=projection,
            drop_last_text_message=False,
        )
        carrier_available = self._publish_terminal_result_if_needed(
            target=target,
            final_reply_text=projection.final_reply_text,
        )
        display_transcript = full_transcript
        if carrier_available:
            if projection.kind == "agent_text":
                interval, retained_reason = self._snapshot_terminal_cleanup_interval(
                    page_transcript=current_transcript,
                    snapshot_transcript=full_transcript,
                    projection=projection,
                )
                outcome = TerminalPageCleanupOutcome.retained(retained_reason)
                if interval is not None:
                    outcome = self._remove_terminal_reply_from_confirmed_pages(
                        target=target,
                        transcript=current_transcript,
                        interval=interval,
                        cancelled=cancelled,
                        elapsed=elapsed,
                        refresh_all_status=current_cancelled != cancelled,
                    )
                else:
                    outcome = self._retain_terminal_page_cleanup(
                        target=target,
                        reason=retained_reason,
                        final_reply_chars=len(projection.final_reply_text),
                    )
            else:
                outcome = self._retain_terminal_page_cleanup(
                    target=target,
                    reason="non_agent_terminal",
                    final_reply_chars=len(projection.final_reply_text),
                )
            if outcome.status == "retained" and current_cancelled != cancelled:
                self._refresh_confirmed_page_status(
                    target=target,
                    transcript=current_transcript,
                    cancelled=cancelled,
                    elapsed=elapsed,
                )
            return True
        self._retain_terminal_page_cleanup(
            target=target,
            reason="carrier_unavailable",
            final_reply_chars=len(projection.final_reply_text),
        )
        if (
            current_cancelled != cancelled
            and target.terminal_page_receipts
            and not self._display_changed(current_transcript, full_transcript)
        ):
            self._refresh_confirmed_page_status(
                target=target,
                transcript=current_transcript,
                cancelled=cancelled,
                elapsed=elapsed,
            )
            return False
        self._refresh_terminal_execution_card_if_changed(
            chat_id=target.chat_id,
            execution_message_id=target.card_message_id,
            current_transcript=current_transcript,
            display_transcript=display_transcript,
            current_cancelled=current_cancelled,
            cancelled=cancelled,
            elapsed=elapsed,
            cursor_start=target.cursor_start,
            cursor_end=ExecutionTranscriptCursor.from_transcript(
                display_transcript
            ),
        )
        return carrier_available

    def _apply_local_terminal_evidence(
        self,
        *,
        target: TerminalReconcileTarget,
        transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
        evidence: tuple[Literal["agent", "error"], str] | None = None,
    ) -> bool:
        if evidence is None:
            evidence = transcript.terminal_reply_evidence()
        if evidence is None or (evidence[0] == "agent" and not evidence[1].strip()):
            self._present_no_valid_terminal_reply(
                target=target,
                transcript=transcript,
                cancelled=cancelled,
                elapsed=elapsed,
            )
            return False

        final_reply_text = evidence[1]
        published = self._publish_terminal_result_if_needed(
            target=target,
            final_reply_text=final_reply_text,
        )
        receipt_transcript = target.transcript.to_transcript()
        receipt_coordinates_match = bool(
            transcript.reply_segments == receipt_transcript.reply_segments
            and transcript.process_blocks == receipt_transcript.process_blocks
        )
        display_transcript = transcript
        if published and evidence[0] == "agent" and receipt_coordinates_match:
            interval, retained_reason = self._terminal_coordinate_interval(
                transcript,
                expected_text=final_reply_text,
            )
            if interval is not None:
                outcome = self._remove_terminal_reply_from_confirmed_pages(
                    target=target,
                    transcript=transcript,
                    interval=interval,
                    cancelled=cancelled,
                    elapsed=elapsed,
                    refresh_all_status=current_cancelled != cancelled,
                )
                if outcome.status != "retained":
                    return True
            else:
                self._retain_terminal_page_cleanup(
                    target=target,
                    reason=retained_reason,
                    final_reply_chars=len(final_reply_text),
                )
        elif published and evidence[0] == "agent":
            self._retain_terminal_page_cleanup(
                target=target,
                reason="snapshot_projection_mismatch",
                final_reply_chars=len(final_reply_text),
            )
        elif published:
            self._retain_terminal_page_cleanup(
                target=target,
                reason="non_agent_terminal",
                final_reply_chars=len(final_reply_text),
            )
        else:
            self._retain_terminal_page_cleanup(
                target=target,
                reason="carrier_unavailable",
                final_reply_chars=len(final_reply_text),
            )
        if (
            published
            and evidence[0] == "error"
            and receipt_coordinates_match
            and self._can_remove_terminal_only_execution_card(
                transcript,
                final_reply_text=final_reply_text,
            )
        ):
            display_transcript = transcript.clone()
            display_transcript.set_reply_text("")
        if not receipt_coordinates_match:
            if current_cancelled != cancelled:
                self._refresh_confirmed_page_status(
                    target=target,
                    transcript=receipt_transcript,
                    cancelled=cancelled,
                    elapsed=elapsed,
                )
            return published
        if (
            current_cancelled != cancelled
            and target.terminal_page_receipts
            and display_transcript is transcript
        ):
            self._refresh_confirmed_page_status(
                target=target,
                transcript=transcript,
                cancelled=cancelled,
                elapsed=elapsed,
            )
            return published
        self._refresh_terminal_execution_card_if_changed(
            chat_id=target.chat_id,
            execution_message_id=target.card_message_id,
            current_transcript=transcript,
            display_transcript=display_transcript,
            current_cancelled=current_cancelled,
            cancelled=cancelled,
            elapsed=elapsed,
            cursor_start=target.cursor_start,
            cursor_end=target.cursor_end,
        )
        return published

    @staticmethod
    def _select_terminal_reply(
        projection: SnapshotReplyProjection,
        local_evidence: tuple[Literal["agent", "error"], str] | None,
    ) -> TerminalReplySelection:
        local_kind: Literal[
            "agent_text", "agent_empty", "turn_error", "unavailable"
        ] = "unavailable"
        local_text = ""
        if local_evidence is not None:
            local_text = local_evidence[1]
            if local_evidence[0] == "error":
                local_kind = "turn_error"
            elif local_text.strip():
                local_kind = "agent_text"
            else:
                local_kind = "agent_empty"
        if (
            projection.invalidates_local_agent_evidence
            and local_kind in {"agent_text", "agent_empty"}
        ):
            local_kind = "unavailable"
            local_text = ""

        rank = {
            "unavailable": 0,
            "agent_empty": 1,
            "turn_error": 2,
            "agent_text": 3,
        }
        if (
            projection.kind != "unavailable"
            and rank[projection.kind] >= rank[local_kind]
        ):
            return TerminalReplySelection(
                kind=projection.kind,
                source="snapshot",
                final_reply_text=projection.final_reply_text,
            )
        if local_kind != "unavailable":
            return TerminalReplySelection(
                kind=local_kind,
                source="local",
                final_reply_text=local_text,
            )
        return TerminalReplySelection(kind="unavailable", source="none")

    def _apply_terminal_reply_selection(
        self,
        *,
        target: TerminalReconcileTarget,
        transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
        projection: SnapshotReplyProjection,
        local_evidence: tuple[Literal["agent", "error"], str] | None,
        local_transcript: ExecutionTranscript | None = None,
    ) -> tuple[TerminalReplySelection, bool]:
        selection = self._select_terminal_reply(projection, local_evidence)
        if selection.source == "snapshot" and selection.kind in {
            "agent_text",
            "turn_error",
        }:
            return selection, self._apply_terminal_snapshot_projection(
                target=target,
                current_transcript=transcript,
                current_cancelled=current_cancelled,
                cancelled=cancelled,
                elapsed=elapsed,
                projection=projection,
            )
        if selection.source == "local" and selection.kind in {
            "agent_text",
            "turn_error",
        }:
            selected_transcript = local_transcript or transcript
            evidence_kind: Literal["agent", "error"] = (
                "agent" if selection.kind == "agent_text" else "error"
            )
            return selection, self._apply_local_terminal_evidence(
                target=target,
                transcript=selected_transcript,
                current_cancelled=current_cancelled,
                cancelled=cancelled,
                elapsed=elapsed,
                evidence=(evidence_kind, selection.final_reply_text),
            )
        selected_transcript = (
            local_transcript
            if selection.source == "local" and local_transcript is not None
            else transcript
        )
        self._present_no_valid_terminal_reply(
            target=target,
            transcript=selected_transcript,
            cancelled=cancelled,
            elapsed=elapsed,
        )
        return selection, False

    def _deliver_generated_images_if_available(
        self,
        *,
        target: TerminalReconcileTarget,
        snapshot: ThreadSnapshot,
    ) -> int:
        try:
            return int(
                self._deliver_generated_images_from_snapshot(
                    sender_id=target.sender_id,
                    chat_id=target.chat_id,
                    thread_id=target.thread_id,
                    snapshot=snapshot,
                    turn_id=target.turn_id,
                    prompt_message_id=target.prompt_message_id,
                    prompt_reply_in_thread=target.prompt_reply_in_thread,
                )
                or 0
            )
        except Exception:
            logger.exception(
                "终态图片投递失败: chat=%s thread=%s turn=%s",
                target.chat_id,
                target.thread_id[:12],
                target.turn_id[:12],
            )
            return 0

    def schedule_terminal_execution_reconcile(self, target: TerminalReconcileTarget | None) -> None:
        if target is None or not target.thread_id or not target.card_message_id:
            return
        self._workers.start(lambda: self.run_terminal_execution_reconcile(target))

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop accepting reconciliation work and wait for every worker."""
        self._workers.shutdown(timeout=timeout)

    def run_terminal_execution_reconcile(self, target: TerminalReconcileTarget) -> None:
        captured_transcript = target.transcript.to_transcript()
        local_evidence = captured_transcript.terminal_reply_evidence()
        last_snapshot_error: Exception | None = None
        snapshot: ThreadSnapshot | None = None
        projection: SnapshotReplyProjection | None = None
        max_attempts = max(int(self._terminal_empty_retry_count()), 1)
        for attempt in range(max_attempts):
            if self._workers.stop_requested:
                return
            try:
                snapshot = self._read_thread(target.thread_id)
            except Exception as exc:
                last_snapshot_error = exc
                break
            if self._workers.stop_requested:
                return
            projection = self.snapshot_reply(snapshot, turn_id=target.turn_id)
            if self._select_terminal_reply(projection, local_evidence).kind != "unavailable":
                break
            if attempt >= max_attempts - 1:
                break
            delay = max(float(self._terminal_empty_retry_delay_seconds()), 0.0)
            if delay > 0 and self._workers.wait_for_stop(delay):
                return

        if self._workers.stop_requested:
            return

        if snapshot is None or projection is None:
            exc = last_snapshot_error or RuntimeError("terminal reconcile snapshot unavailable")
            logger.info(
                "终态补账跳过: chat=%s thread=%s reason=%s",
                target.chat_id,
                target.thread_id[:12],
                self._runtime_recovery_reason(exc),
            )
            self._apply_local_terminal_evidence(
                target=target,
                transcript=captured_transcript,
                current_cancelled=target.cancelled,
                cancelled=target.cancelled,
                elapsed=target.elapsed,
            )
            return

        snapshot_cancelled = target.cancelled or projection.turn_status == "interrupted"
        selection, carrier_available = self._apply_terminal_reply_selection(
            target=target,
            transcript=captured_transcript,
            current_cancelled=target.cancelled,
            cancelled=snapshot_cancelled,
            elapsed=target.elapsed,
            projection=projection,
            local_evidence=local_evidence,
        )
        if selection.kind in {"agent_text", "turn_error"} and not carrier_available:
            return
        self._deliver_generated_images_if_available(target=target, snapshot=snapshot)

    def mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        self._mark_runtime_degraded_for_session(
            self._resolve_session(sender_id, chat_id),
            reason=reason,
        )

    def _mark_runtime_degraded_for_session(
        self,
        captured: BindingSessionSnapshot,
        *,
        reason: str,
    ) -> None:
        updated = self._runtime.mark_runtime_degraded(
            MarkExecutionRuntimeDegradedCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=ExecutionRuntimeObservationFence.from_session(
                    captured
                ),
            )
        )
        if updated is None:
            return
        logger.warning(
            "执行通道暂时降级，保留当前执行锚点: chat=%s thread=%s reason=%s",
            updated.binding[1],
            updated.current_thread_id[:12],
            reason,
        )

    def schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        self.schedule_mirror_watchdog_for_session(
            self._resolve_session(sender_id, chat_id)
        )

    def schedule_mirror_watchdog_for_session(
        self,
        captured: BindingSessionSnapshot,
    ) -> None:
        preparation = self._runtime.prepare_mirror_watchdog(
            PrepareMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(captured),
                delay_seconds=float(self._mirror_watchdog_seconds()),
            )
        )
        if preparation is None:
            return
        cancel_runtime_timer_effects(preparation.timer_cancellations)
        ticket = preparation.ticket
        if ticket is None:
            return
        timer = threading.Timer(
            preparation.delay_seconds,
            self.submit_mirror_watchdog,
            args=(ticket,),
        )
        timer.daemon = True
        registration = MirrorWatchdogRegistration(
            ticket=ticket,
            timer=timer,
        )
        installed = self._runtime.install_mirror_watchdog(
            InstallMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(preparation.session),
                registration=registration,
            )
        )
        if installed is None:
            timer.cancel()
            return
        try:
            timer.start()
        except BaseException:
            self._runtime.rollback_mirror_watchdog_start(
                RollbackMirrorWatchdogCommand(
                    handle=installed.handle,
                    registration=registration,
                )
            )
            timer.cancel()
            raise

    def submit_mirror_watchdog(self, ticket: MirrorWatchdogTicket) -> None:
        self._workers.start(lambda: self.run_mirror_watchdog(ticket))

    def run_mirror_watchdog(self, ticket: MirrorWatchdogTicket) -> None:
        effect = self._runtime_call(
            self._runtime.consume_mirror_watchdog,
            ConsumeMirrorWatchdogCommand(
                ticket=ticket,
                occurred_at=time.monotonic(),
                compact_start_timeout_seconds=max(
                    float(self._compact_start_timeout_seconds()),
                    0.0,
                ),
            ),
        )
        if effect is None:
            return
        if type(effect) is not MirrorWatchdogEffect:
            raise TypeError("watchdog consume returned an invalid effect")
        if self._workers.stop_requested:
            return
        if effect.action == "compact_start_unknown":
            logger.warning(
                "compact 启动通知超时，按不可确认状态保留本地 gate: chat=%s thread=%s",
                effect.session.binding[1],
                effect.thread_id[:12],
            )
            self._runtime_call(
                self._mark_compact_start_outcome_unknown_if_running,
                effect.session,
                effect.thread_id,
            )
            return
        if effect.action == "reschedule":
            self._reschedule_mirror_watchdog(effect.session)
            return
        if effect.action != "reconcile":
            raise TypeError("unknown mirror watchdog effect")

        prepare_started = time.monotonic()
        prepare_seconds = 0.0
        rpc_seconds = 0.0
        settle_seconds = 0.0
        presentation_seconds = 0.0
        outcome = "prepare_failed"
        try:
            try:
                prepared = self._runtime_call(
                    self._prepare_mirror_watchdog_snapshot,
                    effect,
                )
            except Exception as exc:
                prepare_seconds = time.monotonic() - prepare_started
                if self._workers.stop_requested:
                    outcome = "shutdown_during_prepare"
                    return
                logger.info(
                    "watchdog prepare 失败，仅重排当前 exact execution: "
                    "chat=%s thread=%s reason=%s",
                    effect.session.binding[1],
                    effect.thread_id[:12],
                    self._runtime_recovery_reason(exc),
                )
                self._reschedule_mirror_watchdog(effect.session)
                outcome = "prepare_error"
                return
            prepare_seconds = time.monotonic() - prepare_started
            if prepared is None:
                outcome = "stale_prepare"
                return
            if type(prepared) is not MirrorWatchdogSnapshotPreparation:
                raise TypeError("watchdog prepare returned an invalid receipt")
            if self._workers.stop_requested:
                outcome = "shutdown_after_prepare"
                return

            rpc_started = time.monotonic()
            try:
                snapshot = self._read_thread(
                    prepared.snapshot.thread_id,
                    expected_connection_generation=(
                        prepared.connection_generation
                    ),
                )
            except Exception as exc:
                rpc_seconds = time.monotonic() - rpc_started
                outcome = self._handle_mirror_watchdog_read_failure(
                    prepared,
                    exc,
                )
                if outcome != "finalized":
                    self._reschedule_mirror_watchdog(prepared.snapshot.session)
                return
            rpc_seconds = time.monotonic() - rpc_started
            if self._workers.stop_requested:
                outcome = "shutdown_after_read"
                return

            projection = self.snapshot_reply(
                snapshot,
                turn_id=prepared.snapshot.turn_id,
            )
            local_transcript = (
                prepared.snapshot.session.execution.transcript.to_transcript()
            )
            local_evidence = local_transcript.terminal_reply_evidence()
            if self._workers.stop_requested:
                outcome = "shutdown_before_settle"
                return
            settle_started = time.monotonic()
            try:
                transition = self._runtime_call(
                    self._settle_mirror_watchdog_snapshot,
                    prepared,
                    snapshot,
                    projection,
                )
            except Exception as exc:
                settle_seconds = time.monotonic() - settle_started
                if not self._is_pre_send_error(exc):
                    logger.info(
                        "watchdog 快照结算失败: chat=%s thread=%s reason=%s",
                        prepared.snapshot.session.binding[1],
                        prepared.snapshot.thread_id[:12],
                        self._runtime_recovery_reason(exc),
                    )
                self._reschedule_mirror_watchdog(prepared.snapshot.session)
                outcome = "stale_generation"
                return
            settle_seconds = time.monotonic() - settle_started
            if transition is None:
                self._reschedule_mirror_watchdog(prepared.snapshot.session)
                outcome = "stale_settlement"
                return
            if type(transition) is not ExecutionSnapshotTransition:
                raise TypeError("watchdog settle returned an invalid transition")
            if not transition.should_finalize:
                self._reschedule_mirror_watchdog(transition.session)
                outcome = "active_rescheduled"
                return

            presentation_started = time.monotonic()
            finalized = self._complete_staged_snapshot_finalization(
                transition=transition,
                projection=projection,
                local_transcript=local_transcript,
                local_evidence=local_evidence,
                snapshot=snapshot,
            )
            presentation_seconds = time.monotonic() - presentation_started
            if not finalized:
                self._reschedule_mirror_watchdog(transition.session)
                outcome = "finalization_incomplete"
                return
            outcome = "finalized"
        finally:
            logger.info(
                "mirror watchdog staged transaction: chat=%s thread=%s outcome=%s "
                "prepare_seconds=%.3f rpc_seconds=%.3f settle_seconds=%.3f "
                "presentation_seconds=%.3f total_seconds=%.3f",
                effect.session.binding[1],
                effect.thread_id[:12],
                outcome,
                prepare_seconds,
                rpc_seconds,
                settle_seconds,
                presentation_seconds,
                time.monotonic() - prepare_started,
            )

    def _prepare_mirror_watchdog_snapshot(
        self,
        effect: MirrorWatchdogEffect,
    ) -> MirrorWatchdogSnapshotPreparation | None:
        preparation = self._runtime.prepare_snapshot_reconcile(
            PrepareSnapshotReconcileCommand(
                target=BindingExecutionTarget.from_session(effect.session),
                thread_id=effect.thread_id,
                turn_id=effect.turn_id,
                occurred_at=time.monotonic(),
            )
        )
        if preparation is None:
            return None
        return MirrorWatchdogSnapshotPreparation(
            snapshot=preparation,
            connection_generation=self._capture_connection_generation(),
        )

    def _settle_mirror_watchdog_snapshot(
        self,
        prepared: MirrorWatchdogSnapshotPreparation,
        snapshot: ThreadSnapshot,
        projection: SnapshotReplyProjection,
    ) -> ExecutionSnapshotTransition | None:
        command = ApplyExecutionSnapshotCommand(
            target=prepared.snapshot.target,
            observation=prepared.snapshot.observation,
            thread_id=prepared.snapshot.thread_id,
            turn_id=prepared.snapshot.turn_id,
            title=str(snapshot.summary.title or ""),
            working_dir=str(snapshot.summary.cwd or ""),
            reply_text=projection.full_reply_text,
            reply_items=projection.reply_items,
            turn_status=projection.turn_status,
            thread_active=(
                snapshot.summary.status == BACKEND_THREAD_STATUS_ACTIVE
            ),
            occurred_at=time.monotonic(),
            invalidates_local_agent_evidence=(
                projection.invalidates_local_agent_evidence
            ),
            # Title/cwd are ancillary projection facts backed by local disk.
            # The generation guard covers only the in-memory lifecycle settle;
            # normal notification/read paths converge metadata separately.
            apply_thread_metadata=False,
        )
        if self._workers.stop_requested:
            return None
        transition = self._run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._apply_execution_snapshot_if_running(command),
        )
        if (
            transition is not None
            and type(transition) is not ExecutionSnapshotTransition
        ):
            raise TypeError(
                "watchdog generation settlement returned an invalid transition"
            )
        return transition

    def _apply_execution_snapshot_if_running(
        self,
        command: ApplyExecutionSnapshotCommand,
    ) -> ExecutionSnapshotTransition | None:
        if self._workers.stop_requested:
            return None
        return self._runtime.apply_execution_snapshot(command)

    def _settle_mirror_watchdog_fallback(
        self,
        prepared: MirrorWatchdogSnapshotPreparation,
    ) -> RecoveryFinalizationPreparation | None:
        if self._workers.stop_requested:
            return None
        command = PrepareTerminalFallbackCommand(
            target=prepared.snapshot.target,
            observation=prepared.snapshot.observation,
            thread_id=prepared.snapshot.thread_id,
            turn_id=prepared.snapshot.turn_id,
            occurred_at=time.monotonic(),
        )
        fallback = self._run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._prepare_terminal_fallback_if_running(command),
        )
        if (
            fallback is not None
            and type(fallback) is not RecoveryFinalizationPreparation
        ):
            raise TypeError(
                "watchdog fallback settlement returned an invalid transition"
            )
        return fallback

    def _prepare_terminal_fallback_if_running(
        self,
        command: PrepareTerminalFallbackCommand,
    ) -> RecoveryFinalizationPreparation | None:
        if self._workers.stop_requested:
            return None
        return self._runtime.prepare_terminal_fallback(command)

    def _handle_mirror_watchdog_read_failure(
        self,
        prepared: MirrorWatchdogSnapshotPreparation,
        exc: Exception,
    ) -> str:
        if self._workers.stop_requested:
            return "shutdown_after_read"
        if self._is_thread_not_found_error(
            exc
        ) or self._is_turn_thread_not_found_error(exc):
            try:
                fallback = self._runtime_call(
                    self._settle_mirror_watchdog_fallback,
                    prepared,
                )
            except Exception as settle_error:
                logger.info(
                    "watchdog 缺失快照已过期: chat=%s thread=%s reason=%s",
                    prepared.snapshot.session.binding[1],
                    prepared.snapshot.thread_id[:12],
                    self._runtime_recovery_reason(settle_error),
                )
                return "stale_generation"
            if fallback is None:
                return "stale_settlement"
            return (
                "finalized"
                if self._complete_staged_local_finalization(fallback)
                else "finalization_incomplete"
            )
        if (
            self._is_pre_send_error(exc)
            or self._is_transport_disconnect(exc)
            or self._is_request_timeout_error(exc)
        ):
            try:
                updated = self._runtime_call(
                    self._settle_mirror_watchdog_degraded,
                    prepared,
                )
            except Exception as settle_error:
                logger.info(
                    "watchdog 降级结算已过期: chat=%s thread=%s reason=%s",
                    prepared.snapshot.session.binding[1],
                    prepared.snapshot.thread_id[:12],
                    self._runtime_recovery_reason(settle_error),
                )
                return "stale_generation"
            return (
                "read_unavailable"
                if updated is not None
                else "stale_settlement"
            )
        logger.exception(
            "watchdog 读取线程快照失败: thread=%s",
            prepared.snapshot.thread_id[:12],
        )
        return "read_failed"

    def _settle_mirror_watchdog_degraded(
        self,
        prepared: MirrorWatchdogSnapshotPreparation,
    ) -> BindingSessionSnapshot | None:
        if self._workers.stop_requested:
            return None
        command = MarkExecutionRuntimeDegradedCommand(
            target=prepared.snapshot.target,
            observation=prepared.snapshot.observation,
        )
        updated = self._run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._mark_runtime_degraded_if_running(command),
        )
        if updated is not None and type(updated) is not BindingSessionSnapshot:
            raise TypeError(
                "watchdog degraded settlement returned an invalid session"
            )
        return updated

    def _complete_staged_snapshot_finalization(
        self,
        *,
        transition: ExecutionSnapshotTransition,
        projection: SnapshotReplyProjection,
        local_transcript: ExecutionTranscript,
        local_evidence: tuple[Literal["agent", "error"], str] | None,
        snapshot: ThreadSnapshot,
    ) -> bool:
        finalization = self._run_staged_finalization(transition.session)
        if not self._finalization_succeeded(finalization):
            return False
        if self._workers.stop_requested:
            return True
        target = self._attach_terminal_page_receipts(
            transition.terminal,
            finalization,
        )
        if target is None:
            return True
        current_transcript = target.transcript.to_transcript()
        selection, carrier_available = self._apply_terminal_reply_selection(
            target=target,
            transcript=current_transcript,
            current_cancelled=target.cancelled,
            cancelled=target.cancelled,
            elapsed=target.elapsed,
            projection=projection,
            local_evidence=local_evidence,
            local_transcript=local_transcript,
        )
        if selection.kind in {"agent_text", "turn_error"} and not carrier_available:
            return True
        if self._workers.stop_requested:
            return True
        self._deliver_generated_images_if_available(target=target, snapshot=snapshot)
        return True

    def _complete_staged_local_finalization(
        self,
        fallback: RecoveryFinalizationPreparation,
    ) -> bool:
        finalization = self._run_staged_finalization(fallback.session)
        finalized = self._finalization_succeeded(finalization)
        target = self._attach_terminal_page_receipts(
            fallback.terminal,
            finalization,
        )
        if not finalized or target is None:
            return finalized
        if self._workers.stop_requested:
            return True
        transcript = target.transcript.to_transcript()
        self._apply_local_terminal_evidence(
            target=target,
            transcript=transcript,
            current_cancelled=target.cancelled,
            cancelled=target.cancelled,
            elapsed=target.elapsed,
        )
        return True

    def _run_staged_finalization(
        self,
        session: BindingSessionSnapshot,
    ) -> _ExecutionFinalizationResult | None:
        if self._workers.stop_requested:
            return None
        plan = self._runtime_call(
            self._prepare_execution_finalization_if_running,
            session,
        )
        if plan is None or self._workers.stop_requested:
            return None
        return self._present_execution_finalization(plan)

    def _prepare_execution_finalization_if_running(
        self,
        session: BindingSessionSnapshot,
    ) -> object | None:
        if self._workers.stop_requested:
            return None
        return self._prepare_execution_finalization(session)

    def _mark_compact_start_outcome_unknown_if_running(
        self,
        session: BindingSessionSnapshot,
        thread_id: str,
    ) -> None:
        if self._workers.stop_requested:
            return
        self._mark_compact_start_outcome_unknown(session, thread_id)

    def _mark_runtime_degraded_if_running(
        self,
        command: MarkExecutionRuntimeDegradedCommand,
    ) -> BindingSessionSnapshot | None:
        if self._workers.stop_requested:
            return None
        return self._runtime.mark_runtime_degraded(command)

    def _reschedule_mirror_watchdog(
        self,
        session: BindingSessionSnapshot,
    ) -> None:
        if self._workers.stop_requested:
            return
        self._runtime_call(
            self._schedule_mirror_watchdog_if_running,
            session,
        )

    def _schedule_mirror_watchdog_if_running(
        self,
        session: BindingSessionSnapshot,
    ) -> None:
        if self._workers.stop_requested:
            return
        self.schedule_mirror_watchdog_for_session(session)

    def reconcile_execution_snapshot(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        return self.reconcile_execution_snapshot_for_session(
            self._resolve_session(sender_id, chat_id),
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def reconcile_execution_snapshot_for_session(
        self,
        captured: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        preparation = self._runtime.prepare_snapshot_reconcile(
            PrepareSnapshotReconcileCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=normalized_thread_id,
                turn_id=normalized_turn_id,
                occurred_at=time.monotonic(),
            )
        )
        if preparation is None:
            return False
        if not normalized_thread_id:
            return self._finalize_recovery_fallback(
                preparation.session,
                preparation.local_terminal,
            )

        try:
            snapshot = self._read_thread(normalized_thread_id)
        except Exception as exc:
            if self._is_thread_not_found_error(
                exc
            ) or self._is_turn_thread_not_found_error(exc):
                logger.info(
                    "执行快照缺失，按当前本地 transcript 收口: chat=%s thread=%s reason=%s",
                    preparation.session.binding[1],
                    normalized_thread_id[:12],
                    self._runtime_recovery_reason(exc),
                )
                fallback = self._runtime.prepare_terminal_fallback(
                    PrepareTerminalFallbackCommand(
                        target=preparation.target,
                        observation=preparation.observation,
                        thread_id=normalized_thread_id,
                        turn_id=normalized_turn_id,
                        occurred_at=time.monotonic(),
                    )
                )
                if fallback is None:
                    return False
                return self._finalize_recovery_fallback(
                    fallback.session,
                    fallback.terminal,
                )
            if (
                self._is_pre_send_error(exc)
                or self._is_transport_disconnect(exc)
                or self._is_request_timeout_error(exc)
            ):
                self._mark_runtime_degraded_for_session(
                    preparation.session,
                    reason=self._runtime_recovery_reason(exc),
                )
                return False
            logger.exception(
                "读取线程快照失败: thread=%s",
                normalized_thread_id[:12],
            )
            return False

        projection = self.snapshot_reply(
            snapshot,
            turn_id=normalized_turn_id,
        )
        local_transcript = preparation.session.execution.transcript.to_transcript()
        local_evidence = local_transcript.terminal_reply_evidence()
        transition = self._runtime.apply_execution_snapshot(
            ApplyExecutionSnapshotCommand(
                target=preparation.target,
                observation=preparation.observation,
                thread_id=normalized_thread_id,
                turn_id=normalized_turn_id,
                title=str(snapshot.summary.title or ""),
                working_dir=str(snapshot.summary.cwd or ""),
                reply_text=projection.full_reply_text,
                reply_items=projection.reply_items,
                turn_status=projection.turn_status,
                thread_active=(
                    snapshot.summary.status == BACKEND_THREAD_STATUS_ACTIVE
                ),
                occurred_at=time.monotonic(),
                invalidates_local_agent_evidence=(
                    projection.invalidates_local_agent_evidence
                ),
            )
        )
        if transition is None:
            logger.info(
                "丢弃已过期的执行快照: chat=%s requested=%s",
                preparation.session.binding[1],
                normalized_thread_id[:12],
            )
            return False
        if not transition.should_finalize:
            return False
        finalization = self._finalize_execution(transition.session)
        if not self._finalization_succeeded(finalization):
            return False
        target = self._attach_terminal_page_receipts(
            transition.terminal,
            finalization,
        )
        if target is None:
            return True
        current_transcript = target.transcript.to_transcript()
        selection, carrier_available = self._apply_terminal_reply_selection(
            target=target,
            transcript=current_transcript,
            current_cancelled=target.cancelled,
            cancelled=target.cancelled,
            elapsed=target.elapsed,
            projection=projection,
            local_evidence=local_evidence,
            local_transcript=local_transcript,
        )
        if selection.kind in {"agent_text", "turn_error"} and not carrier_available:
            return True
        self._deliver_generated_images_if_available(target=target, snapshot=snapshot)
        return True

    def _finalize_recovery_fallback(
        self,
        session: BindingSessionSnapshot,
        target: TerminalReconcileTarget | None,
    ) -> bool:
        finalization = self._finalize_execution(session)
        finalized = self._finalization_succeeded(finalization)
        target = self._attach_terminal_page_receipts(target, finalization)
        if not finalized or target is None:
            return finalized
        transcript = target.transcript.to_transcript()
        self._apply_local_terminal_evidence(
            target=target,
            transcript=transcript,
            current_cancelled=target.cancelled,
            cancelled=target.cancelled,
            elapsed=target.elapsed,
        )
        return True

    @staticmethod
    def snapshot_reply(snapshot: ThreadSnapshot, *, turn_id: str = "") -> SnapshotReplyProjection:
        target_turns = snapshot.turns
        normalized_turn_id = str(turn_id or "").strip()
        status_is_authoritative = not normalized_turn_id
        if normalized_turn_id:
            matched_turns = [
                turn
                for turn in snapshot.turns
                if str(turn.get("id", "") or "").strip() == normalized_turn_id
            ]
            if not matched_turns:
                return SnapshotReplyProjection(
                    kind="unavailable",
                    full_reply_text="",
                    final_reply_text="",
                    reply_items=(),
                )
            target_turns = matched_turns[-1:]
            status_is_authoritative = True
        fallback_turn_id = ""
        fallback_turn_status = ""
        fallback_reply_items: tuple[RecoverySnapshotReplyItem, ...] = ()
        fallback_invalidates_local_agent_evidence = False
        fallback_projection_set = False
        for turn in reversed(target_turns):
            projected_turn_id = str(turn.get("id", "") or "").strip()
            projected_turn_status = (
                str(turn.get("status", "") or "").strip().lower()
                if status_is_authoritative
                else ""
            )
            if not fallback_turn_id:
                fallback_turn_id = projected_turn_id if status_is_authoritative else ""
                fallback_turn_status = projected_turn_status
            items = turn.get("items") or []
            reply_items = tuple(
                RecoverySnapshotReplyItem(
                    item_type=str(item.get("type", "") or "").strip(),
                    text=str(item.get("text", "") or ""),
                    text_available=(
                        item.get("type") != "agentMessage"
                        or type(item.get("text")) is str
                    ),
                )
                for item in items
            )
            if not fallback_reply_items:
                fallback_reply_items = reply_items
            agent_message_items = [
                (idx, item)
                for idx, item in enumerate(items)
                if item.get("type") == "agentMessage"
            ]
            agent_messages = [
                item.get("text") if type(item.get("text")) is str else ""
                for _, item in agent_message_items
            ]
            visible_parts = [text for text in agent_messages if text.strip()]
            final_message = agent_messages[-1] if agent_messages else ""
            last_agent_index = (
                agent_message_items[-1][0] if agent_message_items else -1
            )
            last_agent_status = (
                str(agent_message_items[-1][1].get("status", "") or "")
                .strip()
                .lower()
                if agent_message_items
                else ""
            )
            last_agent_item_id_value = (
                agent_message_items[-1][1].get("id")
                if agent_message_items
                else ""
            )
            last_agent_item_id = (
                last_agent_item_id_value.strip()
                if type(last_agent_item_id_value) is str
                else ""
            )
            last_agent_phase_allows_terminal = (
                bool(agent_message_items)
                and agent_message_can_be_terminal_candidate(
                    agent_message_items[-1][1].get("phase")
                )
            )
            work_follows_last_agent = any(
                is_terminal_invalidating_work_item_type(item.get("type"))
                for item in items[last_agent_index + 1 :]
            )
            work_between_last_agent_messages = (
                len(agent_message_items) >= 2
                and any(
                    is_terminal_invalidating_work_item_type(item.get("type"))
                    for item in items[
                        agent_message_items[-2][0] + 1 : last_agent_index
                    ]
                )
            )
            last_agent_text_available = bool(agent_message_items) and type(
                agent_message_items[-1][1].get("text")
            ) is str
            turn_in_progress = projected_turn_status in {
                "active",
                "inprogress",
                "in_progress",
                "pending",
            }
            agent_item_in_progress = last_agent_status in {
                "inprogress",
                "in_progress",
                "pending",
                "started",
            }
            agent_is_terminal_candidate = bool(agent_messages) and not (
                turn_in_progress
                or agent_item_in_progress
                or work_follows_last_agent
                or not last_agent_text_available
                or not last_agent_phase_allows_terminal
            )
            invalidates_local_agent_evidence = (
                work_follows_last_agent
                or (bool(agent_message_items) and not last_agent_text_available)
                or (
                    bool(agent_message_items)
                    and not last_agent_phase_allows_terminal
                )
                or (
                    len(agent_message_items) >= 2
                    and not final_message.strip()
                    and (
                        work_between_last_agent_messages
                        or bool(agent_messages[-2].strip())
                    )
                )
                or (agent_is_terminal_candidate and not final_message.strip())
                or (
                    not agent_message_items
                    and any(
                        is_terminal_invalidating_work_item_type(item.get("type"))
                        for item in items
                    )
                )
            )
            if not fallback_projection_set:
                fallback_invalidates_local_agent_evidence = (
                    invalidates_local_agent_evidence
                )
                fallback_projection_set = True
            if agent_is_terminal_candidate and final_message.strip():
                return SnapshotReplyProjection(
                    kind="agent_text",
                    full_reply_text="\n\n".join(visible_parts),
                    final_reply_text=final_message,
                    reply_items=reply_items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
                    invalidates_local_agent_evidence=(
                        invalidates_local_agent_evidence
                    ),
                    final_reply_item_id=last_agent_item_id,
                )
            error = turn.get("error") or {}
            error_message = str(error.get("message", "") or "").strip()
            additional_details = str(error.get("additionalDetails", "") or "").strip()
            if additional_details:
                error_message = (
                    f"{error_message}\n{additional_details}".strip()
                    if error_message
                    else additional_details
                )
            if error_message and not turn_in_progress:
                return SnapshotReplyProjection(
                    kind="turn_error",
                    full_reply_text=error_message,
                    final_reply_text=error_message,
                    reply_items=reply_items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
                    invalidates_local_agent_evidence=(
                        invalidates_local_agent_evidence
                    ),
                )
            if agent_is_terminal_candidate:
                return SnapshotReplyProjection(
                    kind="agent_empty",
                    full_reply_text="\n\n".join(visible_parts),
                    final_reply_text="",
                    reply_items=reply_items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
                    invalidates_local_agent_evidence=(
                        invalidates_local_agent_evidence
                    ),
                    final_reply_item_id=last_agent_item_id,
                )
            if agent_messages:
                return SnapshotReplyProjection(
                    kind="unavailable",
                    full_reply_text="\n\n".join(visible_parts),
                    final_reply_text="",
                    reply_items=reply_items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
                    invalidates_local_agent_evidence=(
                        invalidates_local_agent_evidence
                    ),
                )
        return SnapshotReplyProjection(
            kind="unavailable",
            full_reply_text="",
            final_reply_text="",
            reply_items=fallback_reply_items,
            turn_id=fallback_turn_id,
            turn_status=fallback_turn_status,
            invalidates_local_agent_evidence=(
                fallback_invalidates_local_agent_evidence
            ),
        )
