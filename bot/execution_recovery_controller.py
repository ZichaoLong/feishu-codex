from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, MutableMapping, TypeAlias

from bot.adapters.base import ThreadSnapshot
from bot.binding_runtime_manager import ResolvedRuntimeBinding
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import ExecutionStateChanged, RuntimeStateMessage, ThreadStateChanged
from bot.runtime_view import build_runtime_view
from bot.turn_execution_coordinator import TurnExecutionCoordinator

logger = logging.getLogger(__name__)

RuntimeState: TypeAlias = MutableMapping[str, Any]


@dataclass(frozen=True)
class TerminalReconcileTarget:
    chat_id: str
    thread_id: str
    turn_id: str
    card_message_id: str
    prompt_message_id: str
    transcript: ExecutionTranscript
    cancelled: bool
    elapsed: int


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
        patch_execution_card_message: Callable[..., bool],
        read_thread: Callable[[str], ThreadSnapshot],
        is_thread_not_found_error: Callable[[Exception], bool],
        is_turn_thread_not_found_error: Callable[[Exception], bool],
        is_transport_disconnect: Callable[[Exception], bool],
        is_request_timeout_error: Callable[[Exception], bool],
        runtime_recovery_reason: Callable[[Exception], str],
        mirror_watchdog_seconds: Callable[[], float],
    ) -> None:
        self._lock = lock
        self._runtime_submit = runtime_submit
        self._turn_execution = turn_execution
        self._get_runtime_state = get_runtime_state
        self._resolve_runtime_binding = resolve_runtime_binding
        self._apply_runtime_state_message_locked = apply_runtime_state_message_locked
        self._apply_persisted_runtime_state_message_locked = apply_persisted_runtime_state_message_locked
        self._finalize_execution_card_from_state = finalize_execution_card_from_state
        self._patch_execution_card_message = patch_execution_card_message
        self._read_thread = read_thread
        self._is_thread_not_found_error = is_thread_not_found_error
        self._is_turn_thread_not_found_error = is_turn_thread_not_found_error
        self._is_transport_disconnect = is_transport_disconnect
        self._is_request_timeout_error = is_request_timeout_error
        self._runtime_recovery_reason = runtime_recovery_reason
        self._mirror_watchdog_seconds = mirror_watchdog_seconds

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
                chat_id=chat_id,
                thread_id=str(thread_id or "").strip(),
                turn_id=resolved_turn_id,
                card_message_id=card_message_id,
                prompt_message_id=runtime.execution.current_prompt_message_id.strip(),
                transcript=runtime.execution.transcript,
                cancelled=runtime.execution.cancelled,
                elapsed=(
                    int(max(0.0, time.monotonic() - runtime.execution.started_at))
                    if runtime.execution.started_at
                    else 0
                ),
            )

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
        try:
            snapshot = self._read_thread(target.thread_id)
        except Exception as exc:
            logger.info(
                "终态补账跳过: chat=%s thread=%s reason=%s",
                target.chat_id,
                target.thread_id[:12],
                self._runtime_recovery_reason(exc),
            )
            return

        reply_text, reply_items = self.snapshot_reply(snapshot, turn_id=target.turn_id)
        if not reply_text:
            return

        transcript = target.transcript.clone()
        if not transcript.rebuild_reply_from_snapshot_items(reply_items, fallback_text=reply_text):
            transcript.set_reply_text(reply_text)
        if transcript.reply_text() == target.transcript.reply_text():
            return

        self._patch_execution_card_message(
            target.card_message_id,
            transcript=transcript,
            running=False,
            elapsed=target.elapsed,
            cancelled=target.cancelled,
        )

    def mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if not self._turn_execution.mark_runtime_degraded_locked(state):
                return
            thread_id = str(state["current_thread_id"] or "").strip()
        logger.warning(
            "执行通道暂时降级，保留当前执行锚点: chat=%s thread=%s reason=%s",
            chat_id,
            thread_id[:12],
            reason,
        )

    def note_runtime_event(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._turn_execution.mark_runtime_event_locked(
                state,
                occurred_at=time.monotonic(),
            )
        self.schedule_mirror_watchdog(sender_id, chat_id)

    def schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._cancel_timer(state["mirror_watchdog_timer"])
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_timer=None),
            )
            if not state["running"] or not state["current_thread_id"]:
                self._apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(bump_mirror_watchdog_generation=True),
                )
                return
            generation = state["mirror_watchdog_generation"] + 1
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
            if state["mirror_watchdog_generation"] != generation:
                return
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(mirror_watchdog_timer=None),
            )
            if not state["running"]:
                return
            thread_id = str(state["current_thread_id"] or "").strip()
            turn_id = str(state["current_turn_id"] or "").strip()
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
        if not normalized_thread_id:
            return self._finalize_execution_card_from_state(sender_id, chat_id)
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
                return self._finalize_execution_card_from_state(sender_id, chat_id)
            if self._is_transport_disconnect(exc) or self._is_request_timeout_error(exc):
                self.mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return False
            logger.exception("读取线程快照失败: thread=%s", normalized_thread_id[:12])
            return False

        reply_text, reply_items = self.snapshot_reply(snapshot, turn_id=turn_id)
        resolved = self._resolve_runtime_binding(sender_id, chat_id)
        state = resolved.state
        should_finalize = snapshot.summary.status != "active"
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
                reply_text=reply_text,
                reply_items=reply_items,
            )
            if not should_finalize:
                self._turn_execution.acknowledge_running_snapshot_locked(
                    state,
                    occurred_at=time.monotonic(),
                )
                return False
        return self._finalize_execution_card_from_state(sender_id, chat_id)

    @staticmethod
    def snapshot_reply(snapshot: ThreadSnapshot, *, turn_id: str = "") -> tuple[str, list[dict[str, Any]]]:
        target_turns = snapshot.turns
        normalized_turn_id = str(turn_id or "").strip()
        if normalized_turn_id:
            matched_turns = [
                turn
                for turn in snapshot.turns
                if str(turn.get("id", "") or "").strip() == normalized_turn_id
            ]
            if matched_turns:
                target_turns = matched_turns[-1:]
        for turn in reversed(target_turns):
            items = turn.get("items") or []
            parts = [
                str(item.get("text", "") or "").strip()
                for item in items
                if item.get("type") == "agentMessage" and str(item.get("text", "") or "").strip()
            ]
            if parts:
                return "\n\n".join(parts), items
        return "", []
