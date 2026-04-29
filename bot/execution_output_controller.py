from __future__ import annotations

import threading
import time
from typing import Any, Callable, Protocol, TypeAlias

from bot.card_text_projection import can_render_terminal_result_card
from bot.runtime_card_publisher import (
    ExecutionCardModel,
    RuntimeCardPublisher,
    build_execution_card_model,
    build_plan_card_model,
)
from bot.runtime_state import ExecutionStateChanged, PlanStateChanged, RuntimeStateDict, RuntimeStateMessage
from bot.runtime_view import RuntimeView, build_runtime_view
from bot.turn_execution_coordinator import TurnExecutionCoordinator

RuntimeState: TypeAlias = RuntimeStateDict


class _ReplyText(Protocol):
    def __call__(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None: ...


class ExecutionOutputController:
    def __init__(
        self,
        *,
        lock,
        runtime_submit: Callable[..., None],
        turn_execution: TurnExecutionCoordinator,
        get_runtime_state: Callable[[str, str], RuntimeState],
        get_runtime_view: Callable[[str, str], RuntimeView],
        apply_runtime_state_message_locked: Callable[[RuntimeState, RuntimeStateMessage], None],
        cancel_patch_timer_locked: Callable[[RuntimeState], None],
        card_publisher_factory: Callable[[], RuntimeCardPublisher],
        dispatch_execution_card_patch: Callable[[str, ExecutionCardModel], None],
        reply_text: _ReplyText,
        card_reply_limit: Callable[[], int],
        terminal_result_card_limit: Callable[[], int],
        card_log_limit: Callable[[], int],
        stream_patch_interval_ms: Callable[[], int],
    ) -> None:
        self._lock = lock
        self._runtime_submit = runtime_submit
        self._turn_execution = turn_execution
        self._get_runtime_state = get_runtime_state
        self._get_runtime_view = get_runtime_view
        self._apply_runtime_state_message_locked = apply_runtime_state_message_locked
        self._cancel_patch_timer_locked = cancel_patch_timer_locked
        self._card_publisher_factory = card_publisher_factory
        self._dispatch_execution_card_patch = dispatch_execution_card_patch
        self._reply_text = reply_text
        self._card_reply_limit = card_reply_limit
        self._terminal_result_card_limit = terminal_result_card_limit
        self._card_log_limit = card_log_limit
        self._stream_patch_interval_ms = stream_patch_interval_ms

    def send_execution_card(
        self,
        chat_id: str,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        return self._card_publisher_factory().send_execution_card(
            chat_id,
            parent_message_id,
            reply_in_thread=reply_in_thread,
        )

    def patch_execution_card_message(
        self,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> bool:
        model = build_execution_card_model(
            transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
            log_limit=int(self._card_log_limit()),
            reply_limit=int(self._card_reply_limit()),
        )
        return self._card_publisher_factory().patch_execution_card(message_id, model)

    def dispatch_execution_card_message(
        self,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> None:
        model = build_execution_card_model(
            transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
            log_limit=int(self._card_log_limit()),
            reply_limit=int(self._card_reply_limit()),
        )
        self._dispatch_execution_card_patch(message_id, model)

    def refresh_terminal_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            message_id = runtime.execution.effective_message_id.strip()
            if not message_id:
                return False
            transcript = runtime.execution.transcript
            elapsed = int(max(0.0, time.monotonic() - runtime.execution.started_at)) if runtime.execution.started_at else 0
            cancelled = runtime.execution.cancelled
        return self.patch_execution_card_message(
            message_id,
            transcript=transcript,
            running=False,
            elapsed=elapsed,
            cancelled=cancelled,
        )

    def schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            message_id = runtime.execution.current_message_id.strip()
            if not message_id:
                return
            now = time.monotonic()
            last_patch = float(state["last_patch_at"] or 0.0)
            timer = state["patch_timer"]
            interval_seconds = int(self._stream_patch_interval_ms()) / 1000
            if now - last_patch >= interval_seconds:
                self._apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(last_patch_at=now),
                )
                self._cancel_patch_timer_locked(state)
                immediate = True
            elif timer is None:
                delay = interval_seconds - (now - last_patch)
                timer = threading.Timer(
                    delay,
                    self.submit_flush_execution_card,
                    args=(sender_id, chat_id),
                    kwargs={"background": True},
                )
                timer.daemon = True
                self._apply_runtime_state_message_locked(
                    state,
                    ExecutionStateChanged(patch_timer=timer),
                )
                timer.start()
                immediate = False
            else:
                immediate = False
        if immediate:
            self.flush_execution_card(sender_id, chat_id, background=True)

    def submit_flush_execution_card(
        self,
        sender_id: str,
        chat_id: str,
        immediate: bool = False,
        *,
        background: bool = False,
    ) -> None:
        self._runtime_submit(
            self.flush_execution_card,
            sender_id,
            chat_id,
            immediate=immediate,
            background=background,
        )

    def flush_execution_card(
        self,
        sender_id: str,
        chat_id: str,
        immediate: bool = False,
        *,
        background: bool = False,
    ) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._cancel_patch_timer_locked(state)
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(last_patch_at=time.monotonic()),
            )
            runtime = build_runtime_view(state)
            message_id = runtime.execution.current_message_id
            if not message_id:
                return
            transcript = runtime.execution.transcript
            reply_text = transcript.reply_text()
            running = runtime.execution.running
            cancelled = runtime.execution.cancelled
            elapsed = (
                int(max(0.0, time.monotonic() - runtime.execution.started_at))
                if runtime.execution.started_at
                else 0
            )

        if background:
            self.dispatch_execution_card_message(
                message_id,
                transcript=transcript,
                running=running,
                elapsed=elapsed,
                cancelled=cancelled,
            )
            return

        ok = self.patch_execution_card_message(
            message_id,
            transcript=transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
        )
        if not ok and immediate and reply_text:
            with self._lock:
                followup = self._turn_execution.prepare_patch_failure_followup_locked(state)
            if followup is not None:
                self._reply_text(
                    chat_id,
                    followup.reply_text,
                    message_id=followup.prompt_message_id,
                    reply_in_thread=followup.prompt_reply_in_thread,
                )

    def publish_terminal_result(
        self,
        chat_id: str,
        *,
        final_reply_text: str,
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
    ) -> bool:
        normalized = str(final_reply_text or "").strip()
        if not normalized:
            return False
        if can_render_terminal_result_card(
            normalized,
            char_limit=int(self._terminal_result_card_limit()),
        ):
            published = self._card_publisher_factory().publish_terminal_result_card(
                chat_id=chat_id,
                parent_message_id=prompt_message_id,
                final_reply_text=normalized,
                reply_in_thread=prompt_reply_in_thread,
            )
            if published:
                return True
        self._reply_text(
            chat_id,
            normalized,
            message_id=prompt_message_id,
            reply_in_thread=prompt_reply_in_thread,
        )
        return True

    def flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        runtime = self._get_runtime_view(sender_id, chat_id)
        model = build_plan_card_model(runtime.plan)
        if model.is_empty:
            return
        result = self._card_publisher_factory().publish_plan_card(
            chat_id=chat_id,
            parent_message_id=runtime.execution.current_message_id,
            plan_message_id=runtime.plan.message_id,
            model=model,
            reply_in_thread=runtime.execution.current_prompt_reply_in_thread,
        )
        state = self._get_runtime_state(sender_id, chat_id)
        if result.attempted_existing and not result.reused_existing:
            with self._lock:
                if str(state["plan_message_id"] or "") == runtime.plan.message_id:
                    self._apply_runtime_state_message_locked(
                        state,
                        PlanStateChanged(plan_message_id=""),
                    )
        if result.message_id and result.message_id != runtime.plan.message_id:
            with self._lock:
                self._apply_runtime_state_message_locked(
                    state,
                    PlanStateChanged(plan_message_id=result.message_id),
                )
