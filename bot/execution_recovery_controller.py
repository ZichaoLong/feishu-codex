from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias

from bot.adapters.base import ThreadSnapshot
from bot.binding_runtime_manager import ResolvedRuntimeBinding
from bot.execution_transcript import ExecutionTranscript
from bot.generated_image_delivery import collect_generated_images
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    ExecutionStateChanged,
    RuntimeStateDict,
    RuntimeStateMessage,
    ThreadStateChanged,
)
from bot.runtime_view import build_runtime_view
from bot.turn_execution_coordinator import TurnExecutionCoordinator

logger = logging.getLogger(__name__)

RuntimeState: TypeAlias = RuntimeStateDict


@dataclass(frozen=True)
class TerminalReconcileTarget:
    sender_id: str
    chat_id: str
    thread_id: str
    turn_id: str
    card_message_id: str
    prompt_message_id: str
    prompt_reply_in_thread: bool
    transcript: ExecutionTranscript
    cancelled: bool
    elapsed: int


@dataclass(frozen=True)
class SnapshotReplyProjection:
    full_reply_text: str
    final_reply_text: str
    reply_items: list[dict[str, Any]]
    turn_id: str = ""
    turn_status: str = ""


class ExecutionRecoveryController:
    def __init__(
        self,
        *,
        lock,
        runtime_submit: Callable[..., None],
        turn_execution: TurnExecutionCoordinator,
        get_runtime_state: Callable[[str, str], RuntimeState],
        resolve_runtime_binding: Callable[[str, str], ResolvedRuntimeBinding],
        apply_runtime_state_message_locked: Callable[[RuntimeState, RuntimeStateMessage], None],
        apply_persisted_runtime_state_message_locked: Callable[[tuple[str, str], RuntimeState, RuntimeStateMessage], None],
        finalize_execution_card_from_state: Callable[[str, str], bool],
        dispatch_execution_card_message: Callable[..., None],
        remove_execution_card_message: Callable[[str], bool],
        publish_terminal_result: Callable[..., bool],
        has_recorded_terminal_result: Callable[..., bool],
        deliver_generated_images_from_snapshot: Callable[..., int],
        read_thread: Callable[[str], ThreadSnapshot],
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
        self._lock = lock
        self._runtime_submit = runtime_submit
        self._turn_execution = turn_execution
        self._get_runtime_state = get_runtime_state
        self._resolve_runtime_binding = resolve_runtime_binding
        self._apply_runtime_state_message_locked = apply_runtime_state_message_locked
        self._apply_persisted_runtime_state_message_locked = apply_persisted_runtime_state_message_locked
        self._finalize_execution_card_from_state = finalize_execution_card_from_state
        self._dispatch_execution_card_message = dispatch_execution_card_message
        self._remove_execution_card_message = remove_execution_card_message
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

    @staticmethod
    def _cancel_timer(timer: threading.Timer | None) -> None:
        if timer is not None:
            timer.cancel()

    def cancel_mirror_watchdog_locked(self, state: RuntimeState) -> None:
        self._cancel_timer(state["mirror_watchdog_timer"])
        self._apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                mirror_watchdog_timer=None,
                bump_mirror_watchdog_generation=True,
            ),
        )

    def capture_terminal_reconcile_target(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> TerminalReconcileTarget | None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            card_message_id = runtime.execution.current_message_id.strip()
            if not card_message_id:
                return None
            resolved_turn_id = str(turn_id or runtime.execution.current_turn_id or "").strip()
            if not resolved_turn_id:
                return None
            return TerminalReconcileTarget(
                sender_id=sender_id,
                chat_id=chat_id,
                thread_id=str(thread_id or "").strip(),
                turn_id=resolved_turn_id,
                card_message_id=card_message_id,
                prompt_message_id=runtime.execution.current_prompt_message_id.strip(),
                prompt_reply_in_thread=runtime.execution.current_prompt_reply_in_thread,
                transcript=runtime.execution.transcript,
                cancelled=runtime.execution.cancelled,
                elapsed=(
                    int(max(0.0, time.monotonic() - runtime.execution.started_at))
                    if runtime.execution.started_at
                    else 0
                ),
            )

    @staticmethod
    def _runtime_matches_execution(runtime, execution_message_id: str) -> bool:
        normalized_message_id = str(execution_message_id or "").strip()
        if not normalized_message_id:
            return False
        return runtime.execution.current_message_id.strip() == normalized_message_id or (
            runtime.execution.last_execution_message_id.strip() == normalized_message_id
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
            projection.reply_items,
            fallback_text="" if drop_last_text_message else projection.full_reply_text,
            drop_last_text_message=drop_last_text_message,
        )
        return transcript

    def _replace_terminal_execution_transcript(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        transcript: ExecutionTranscript,
    ) -> None:
        normalized_message_id = str(execution_message_id or "").strip()
        if not normalized_message_id:
            return
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            if not self._runtime_matches_execution(runtime, normalized_message_id):
                return
            self._turn_execution.replace_execution_transcript_locked(
                state,
                transcript=transcript,
            )

    def _remember_terminal_result_text(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        final_reply_text: str,
    ) -> None:
        normalized_message_id = str(execution_message_id or "").strip()
        raw_text = str(final_reply_text or "")
        if not normalized_message_id or not raw_text:
            return
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            if not self._runtime_matches_execution(runtime, normalized_message_id):
                return
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(terminal_result_text=raw_text),
            )

    def _clear_and_refresh_execution_card(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
    ) -> bool:
        cleared = transcript.clone()
        cleared.set_reply_text("")
        self._replace_terminal_execution_transcript(
            sender_id=sender_id,
            chat_id=chat_id,
            execution_message_id=execution_message_id,
            transcript=cleared,
        )
        return self._refresh_terminal_execution_card_if_changed(
            execution_message_id=execution_message_id,
            current_transcript=transcript,
            display_transcript=cleared,
            current_cancelled=current_cancelled,
            cancelled=cancelled,
            elapsed=elapsed,
        )

    def _refresh_terminal_execution_card_if_changed(
        self,
        *,
        execution_message_id: str,
        current_transcript: ExecutionTranscript,
        display_transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
    ) -> bool:
        transcript_changed = self._display_changed(current_transcript, display_transcript)
        cancelled_changed = current_cancelled != cancelled
        if not (transcript_changed or cancelled_changed):
            return False
        self._dispatch_execution_card_message(
            execution_message_id,
            transcript=display_transcript,
            running=False,
            elapsed=elapsed,
            cancelled=cancelled,
        )
        return True

    def _publish_terminal_result_if_needed(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        final_reply_text: str,
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool:
        raw_text = str(final_reply_text or "")
        if not raw_text.strip():
            return False
        if self._has_recorded_terminal_result(
            execution_message_id=execution_message_id,
            final_reply_text=raw_text,
        ):
            return True
        published = self._publish_terminal_result(
            chat_id,
            final_reply_text=raw_text,
            source_execution_message_id=execution_message_id,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            thread_id=thread_id,
        )
        if published:
            self._remember_terminal_result_text(
                sender_id=sender_id,
                chat_id=chat_id,
                execution_message_id=execution_message_id,
                final_reply_text=raw_text,
            )
        return published

    def _apply_terminal_snapshot_projection(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        prompt_message_id: str,
        prompt_reply_in_thread: bool,
        current_transcript: ExecutionTranscript,
        current_cancelled: bool,
        cancelled: bool,
        elapsed: int,
        projection: SnapshotReplyProjection,
        thread_id: str = "",
    ) -> bool:
        full_transcript = self._transcript_from_snapshot_projection(
            current_transcript,
            projection=projection,
            drop_last_text_message=False,
        )
        carrier_available = self._publish_terminal_result_if_needed(
            sender_id=sender_id,
            chat_id=chat_id,
            execution_message_id=execution_message_id,
            final_reply_text=projection.final_reply_text,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            thread_id=thread_id,
        )
        display_transcript = full_transcript
        if carrier_available:
            display_transcript = self._transcript_from_snapshot_projection(
                current_transcript,
                projection=projection,
                drop_last_text_message=True,
            )
        self._replace_terminal_execution_transcript(
            sender_id=sender_id,
            chat_id=chat_id,
            execution_message_id=execution_message_id,
            transcript=display_transcript,
        )
        self._refresh_terminal_execution_card_if_changed(
            execution_message_id=execution_message_id,
            current_transcript=current_transcript,
            display_transcript=display_transcript,
            current_cancelled=current_cancelled,
            cancelled=cancelled,
            elapsed=elapsed,
        )
        return carrier_available

    def _maybe_publish_terminal_result(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        final_reply_text: str,
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool:
        return self._publish_terminal_result_if_needed(
            sender_id=sender_id,
            chat_id=chat_id,
            execution_message_id=execution_message_id,
            final_reply_text=final_reply_text,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            thread_id=thread_id,
        )

    def _deliver_generated_images_if_available(
        self,
        *,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        snapshot: ThreadSnapshot,
        turn_id: str,
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
    ) -> int:
        try:
            return int(
                self._deliver_generated_images_from_snapshot(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    snapshot=snapshot,
                    turn_id=turn_id,
                    prompt_message_id=prompt_message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                )
                or 0
            )
        except Exception:
            logger.exception(
                "终态图片投递失败: chat=%s thread=%s turn=%s",
                chat_id,
                str(thread_id or "")[:12],
                str(turn_id or "")[:12],
            )
            return 0

    def schedule_terminal_execution_reconcile(self, target: TerminalReconcileTarget | None) -> None:
        if target is None or not target.thread_id or not target.card_message_id:
            return
        worker = threading.Thread(
            target=self.run_terminal_execution_reconcile,
            args=(target,),
            daemon=True,
        )
        worker.start()

    def run_terminal_execution_reconcile(self, target: TerminalReconcileTarget) -> None:
        fallback_reply_text = target.transcript.reply_text()
        last_snapshot_error: Exception | None = None
        snapshot: ThreadSnapshot | None = None
        projection: SnapshotReplyProjection | None = None
        max_attempts = max(int(self._terminal_empty_retry_count()), 1)
        for attempt in range(max_attempts):
            try:
                snapshot = self._read_thread(target.thread_id)
            except Exception as exc:
                last_snapshot_error = exc
                break
            projection = self.snapshot_reply(snapshot, turn_id=target.turn_id)
            if projection.final_reply_text or fallback_reply_text:
                break
            if collect_generated_images(snapshot, turn_id=target.turn_id):
                break
            if attempt >= max_attempts - 1:
                break
            delay = max(float(self._terminal_empty_retry_delay_seconds()), 0.0)
            if delay > 0:
                time.sleep(delay)

        if snapshot is None or projection is None:
            exc = last_snapshot_error or RuntimeError("terminal reconcile snapshot unavailable")
            logger.info(
                "终态补账跳过: chat=%s thread=%s reason=%s",
                target.chat_id,
                target.thread_id[:12],
                self._runtime_recovery_reason(exc),
            )
            if fallback_reply_text:
                published = self._maybe_publish_terminal_result(
                    sender_id=target.sender_id,
                    chat_id=target.chat_id,
                    execution_message_id=target.card_message_id,
                    final_reply_text=fallback_reply_text,
                    prompt_message_id=target.prompt_message_id,
                    prompt_reply_in_thread=target.prompt_reply_in_thread,
                    thread_id=target.thread_id,
                )
                if published and self._can_remove_terminal_only_execution_card(
                    target.transcript,
                    final_reply_text=fallback_reply_text,
                ):
                    self._clear_and_refresh_execution_card(
                        sender_id=target.sender_id,
                        chat_id=target.chat_id,
                        execution_message_id=target.card_message_id,
                        transcript=target.transcript,
                        current_cancelled=target.cancelled,
                        cancelled=target.cancelled,
                        elapsed=target.elapsed,
                    )
            return

        snapshot_cancelled = target.cancelled or projection.turn_status == "interrupted"
        if projection.final_reply_text:
            carrier_available = self._apply_terminal_snapshot_projection(
                sender_id=target.sender_id,
                chat_id=target.chat_id,
                execution_message_id=target.card_message_id,
                prompt_message_id=target.prompt_message_id,
                prompt_reply_in_thread=target.prompt_reply_in_thread,
                current_transcript=target.transcript,
                current_cancelled=target.cancelled,
                cancelled=snapshot_cancelled,
                elapsed=target.elapsed,
                projection=projection,
                thread_id=target.thread_id,
            )
            if carrier_available:
                self._deliver_generated_images_if_available(
                    sender_id=target.sender_id,
                    chat_id=target.chat_id,
                    thread_id=target.thread_id,
                    snapshot=snapshot,
                    turn_id=target.turn_id,
                    prompt_message_id=target.prompt_message_id,
                    prompt_reply_in_thread=target.prompt_reply_in_thread,
                )
            return

        published = False
        display_transcript = target.transcript
        if fallback_reply_text:
            published = self._maybe_publish_terminal_result(
                sender_id=target.sender_id,
                chat_id=target.chat_id,
                execution_message_id=target.card_message_id,
                final_reply_text=fallback_reply_text,
                prompt_message_id=target.prompt_message_id,
                prompt_reply_in_thread=target.prompt_reply_in_thread,
                thread_id=target.thread_id,
            )
            if published and self._can_remove_terminal_only_execution_card(
                target.transcript,
                final_reply_text=fallback_reply_text,
            ):
                display_transcript = target.transcript.clone()
                display_transcript.set_reply_text("")
                self._replace_terminal_execution_transcript(
                    sender_id=target.sender_id,
                    chat_id=target.chat_id,
                    execution_message_id=target.card_message_id,
                    transcript=display_transcript,
                )
        self._refresh_terminal_execution_card_if_changed(
            execution_message_id=target.card_message_id,
            current_transcript=target.transcript,
            display_transcript=display_transcript,
            current_cancelled=target.cancelled,
            cancelled=snapshot_cancelled,
            elapsed=target.elapsed,
        )
        if published or not fallback_reply_text:
            self._deliver_generated_images_if_available(
                sender_id=target.sender_id,
                chat_id=target.chat_id,
                thread_id=target.thread_id,
                snapshot=snapshot,
                turn_id=target.turn_id,
                prompt_message_id=target.prompt_message_id,
                prompt_reply_in_thread=target.prompt_reply_in_thread,
            )

    def mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if not self._turn_execution.mark_runtime_degraded_locked(state):
                return
            thread_id = build_runtime_view(state).current_thread_id.strip()
        logger.warning(
            "执行通道暂时降级，保留当前执行锚点: chat=%s thread=%s reason=%s",
            chat_id,
            thread_id[:12],
            reason,
        )

    def schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._cancel_timer(state["mirror_watchdog_timer"])
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_timer=None),
            )
            runtime = build_runtime_view(state)
            if not runtime.running or not runtime.current_thread_id:
                self._apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(bump_mirror_watchdog_generation=True),
                )
                return
            generation = runtime.execution.mirror_watchdog_generation + 1
            timer = threading.Timer(
                float(self._mirror_watchdog_seconds()),
                self.submit_mirror_watchdog,
                args=(sender_id, chat_id, generation),
            )
            timer.daemon = True
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(
                    mirror_watchdog_timer=timer,
                    mirror_watchdog_generation=generation,
                ),
            )
            timer.start()

    def submit_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        self._runtime_submit(self.run_mirror_watchdog, sender_id, chat_id, generation)

    def run_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            if runtime.execution.mirror_watchdog_generation != generation:
                return
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_timer=None),
            )
            runtime = build_runtime_view(state)
            if not runtime.running:
                return
            awaiting_started = self._turn_execution.awaiting_remote_turn_started_locked(state)
            thread_id = runtime.current_thread_id.strip()
            turn_id = runtime.execution.current_turn_id.strip()
            compact_start_timed_out = False
            if awaiting_started and runtime.execution.current_execution_kind.strip() == "compact":
                timeout_seconds = max(float(self._compact_start_timeout_seconds()), 0.0)
                started_at = float(runtime.execution.started_at or 0.0)
                compact_start_timed_out = bool(
                    timeout_seconds
                    and started_at
                    and time.monotonic() - started_at >= timeout_seconds
                )
                if compact_start_timed_out:
                    self._turn_execution.apply_terminal_error_locked(
                        state,
                        error_message=(
                            "compact 已发起，但未收到上游 turn 启动通知；"
                            "本地状态不可确认，已收口当前执行卡并继续处理队列。"
                        ),
                    )
        if awaiting_started:
            if compact_start_timed_out:
                logger.warning(
                    "compact 启动通知超时，按不可确认状态收口: chat=%s thread=%s",
                    chat_id,
                    thread_id[:12],
                )
                self._finalize_execution_card_from_state(sender_id, chat_id)
                return
            self.schedule_mirror_watchdog(sender_id, chat_id)
            return
        if not thread_id:
            return
        finalized = self.reconcile_execution_snapshot(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        if not finalized:
            self.schedule_mirror_watchdog(sender_id, chat_id)

    def reconcile_execution_snapshot(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if normalized_thread_id and not normalized_turn_id:
            state = self._get_runtime_state(sender_id, chat_id)
            with self._lock:
                if self._turn_execution.awaiting_remote_turn_started_locked(state):
                    return False
        if not normalized_thread_id:
            state = self._get_runtime_state(sender_id, chat_id)
            with self._lock:
                runtime = build_runtime_view(state)
                fallback_reply_text = runtime.execution.transcript.reply_text()
                card_message_id = runtime.execution.current_message_id.strip()
                prompt_message_id = runtime.execution.current_prompt_message_id.strip()
                prompt_reply_in_thread = runtime.execution.current_prompt_reply_in_thread
                cancelled = runtime.execution.cancelled
                elapsed = (
                    int(max(0.0, time.monotonic() - runtime.execution.started_at))
                    if runtime.execution.started_at
                    else 0
                )
            finalized = self._finalize_execution_card_from_state(sender_id, chat_id)
            if finalized and fallback_reply_text:
                published = self._maybe_publish_terminal_result(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    execution_message_id=card_message_id,
                    final_reply_text=fallback_reply_text,
                    prompt_message_id=prompt_message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    thread_id=thread_id,
                )
                if published and self._can_remove_terminal_only_execution_card(
                    runtime.execution.transcript,
                    final_reply_text=fallback_reply_text,
                ):
                    self._clear_and_refresh_execution_card(
                        sender_id=sender_id,
                        chat_id=chat_id,
                        execution_message_id=card_message_id,
                        transcript=runtime.execution.transcript,
                        current_cancelled=cancelled,
                        cancelled=cancelled,
                        elapsed=elapsed,
                    )
            return finalized
        try:
            snapshot = self._read_thread(normalized_thread_id)
        except Exception as exc:
            if self._is_thread_not_found_error(exc) or self._is_turn_thread_not_found_error(exc):
                logger.info(
                    "执行快照缺失，按当前本地 transcript 收口: chat=%s thread=%s reason=%s",
                    chat_id,
                    normalized_thread_id[:12],
                    self._runtime_recovery_reason(exc),
                )
                state = self._get_runtime_state(sender_id, chat_id)
                with self._lock:
                    runtime = build_runtime_view(state)
                    fallback_reply_text = runtime.execution.transcript.reply_text()
                    card_message_id = runtime.execution.current_message_id.strip()
                    prompt_message_id = runtime.execution.current_prompt_message_id.strip()
                    prompt_reply_in_thread = runtime.execution.current_prompt_reply_in_thread
                    cancelled = runtime.execution.cancelled
                    elapsed = (
                        int(max(0.0, time.monotonic() - runtime.execution.started_at))
                        if runtime.execution.started_at
                        else 0
                    )
                finalized = self._finalize_execution_card_from_state(sender_id, chat_id)
                if finalized and fallback_reply_text:
                    published = self._maybe_publish_terminal_result(
                        sender_id=sender_id,
                        chat_id=chat_id,
                        execution_message_id=card_message_id,
                        final_reply_text=fallback_reply_text,
                        prompt_message_id=prompt_message_id,
                        prompt_reply_in_thread=prompt_reply_in_thread,
                        thread_id=thread_id,
                    )
                    if published and self._can_remove_terminal_only_execution_card(
                        runtime.execution.transcript,
                        final_reply_text=fallback_reply_text,
                    ):
                        self._clear_and_refresh_execution_card(
                            sender_id=sender_id,
                            chat_id=chat_id,
                            execution_message_id=card_message_id,
                            transcript=runtime.execution.transcript,
                            current_cancelled=cancelled,
                            cancelled=cancelled,
                            elapsed=elapsed,
                        )
                return finalized
            if (
                self._is_pre_send_error(exc)
                or self._is_transport_disconnect(exc)
                or self._is_request_timeout_error(exc)
            ):
                self.mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return False
            logger.exception("读取线程快照失败: thread=%s", normalized_thread_id[:12])
            return False

        projection = self.snapshot_reply(snapshot, turn_id=normalized_turn_id)
        resolved = self._resolve_runtime_binding(sender_id, chat_id)
        state = resolved.state
        should_finalize = snapshot.summary.status != BACKEND_THREAD_STATUS_ACTIVE
        with self._lock:
            self._apply_persisted_runtime_state_message_locked(
                resolved.binding,
                state,
                ThreadStateChanged(
                    current_thread_title=snapshot.summary.title or state["current_thread_title"],
                    working_dir=snapshot.summary.cwd or state["working_dir"],
                ),
            )
            self._turn_execution.apply_snapshot_reply_locked(
                state,
                reply_text=projection.full_reply_text,
                reply_items=projection.reply_items,
            )
            if projection.turn_status == "interrupted":
                self._apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(cancelled=True),
                )
            if not should_finalize:
                self._turn_execution.acknowledge_running_snapshot_locked(
                    state,
                    occurred_at=time.monotonic(),
                )
                return False
            runtime = build_runtime_view(state)
            card_message_id = runtime.execution.current_message_id.strip()
            current_transcript = runtime.execution.transcript
            prompt_message_id = runtime.execution.current_prompt_message_id.strip()
            prompt_reply_in_thread = runtime.execution.current_prompt_reply_in_thread
            cancelled = runtime.execution.cancelled
            elapsed = (
                int(max(0.0, time.monotonic() - runtime.execution.started_at))
                if runtime.execution.started_at
                else 0
            )
            fallback_reply_text = current_transcript.reply_text()
        finalized = self._finalize_execution_card_from_state(sender_id, chat_id)
        if not finalized:
            return False
        if projection.final_reply_text:
            carrier_available = self._apply_terminal_snapshot_projection(
                sender_id=sender_id,
                chat_id=chat_id,
                execution_message_id=card_message_id,
                prompt_message_id=prompt_message_id,
                prompt_reply_in_thread=prompt_reply_in_thread,
                current_transcript=current_transcript,
                current_cancelled=cancelled,
                cancelled=cancelled,
                elapsed=elapsed,
                projection=projection,
                thread_id=normalized_thread_id,
            )
            if carrier_available:
                self._deliver_generated_images_if_available(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    thread_id=normalized_thread_id,
                    snapshot=snapshot,
                    turn_id=normalized_turn_id,
                    prompt_message_id=prompt_message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                )
            return True
        published = False
        if fallback_reply_text:
            published = self._maybe_publish_terminal_result(
                sender_id=sender_id,
                chat_id=chat_id,
                execution_message_id=card_message_id,
                final_reply_text=fallback_reply_text,
                prompt_message_id=prompt_message_id,
                prompt_reply_in_thread=prompt_reply_in_thread,
                thread_id=normalized_thread_id,
            )
            if published and self._can_remove_terminal_only_execution_card(
                current_transcript,
                final_reply_text=fallback_reply_text,
            ):
                self._clear_and_refresh_execution_card(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    execution_message_id=card_message_id,
                    transcript=current_transcript,
                    current_cancelled=cancelled,
                    cancelled=cancelled,
                    elapsed=elapsed,
                )
        if published or not fallback_reply_text:
            self._deliver_generated_images_if_available(
                sender_id=sender_id,
                chat_id=chat_id,
                thread_id=normalized_thread_id,
                snapshot=snapshot,
                turn_id=normalized_turn_id,
                prompt_message_id=prompt_message_id,
                prompt_reply_in_thread=prompt_reply_in_thread,
            )
        return finalized

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
            if matched_turns:
                target_turns = matched_turns[-1:]
                status_is_authoritative = True
        fallback_turn_id = ""
        fallback_turn_status = ""
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
            parts = [
                str(item.get("text", "") or "")
                for item in items
                if item.get("type") == "agentMessage" and str(item.get("text", "") or "").strip()
            ]
            if parts:
                return SnapshotReplyProjection(
                    full_reply_text="\n\n".join(parts),
                    final_reply_text=parts[-1],
                    reply_items=items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
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
            if error_message:
                return SnapshotReplyProjection(
                    full_reply_text=error_message,
                    final_reply_text=error_message,
                    reply_items=items,
                    turn_id=projected_turn_id if status_is_authoritative else "",
                    turn_status=projected_turn_status,
                )
        return SnapshotReplyProjection(
            full_reply_text="",
            final_reply_text="",
            reply_items=[],
            turn_id=fallback_turn_id,
            turn_status=fallback_turn_status,
        )
