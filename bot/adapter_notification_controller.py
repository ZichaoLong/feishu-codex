from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeAlias

from bot.constants import display_path
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_SYSTEM_ERROR,
    ExecutionStateChanged,
    RuntimeStateDict,
    RuntimeStateMessage,
    ThreadGoalCleared,
    ThreadGoalStateChanged,
    ThreadStateChanged,
)
from bot.runtime_view import build_runtime_view
from bot.turn_execution_coordinator import TurnExecutionCoordinator

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]
RuntimeState: TypeAlias = RuntimeStateDict

WORK_ITEM_LABELS = {
    "commandExecution": "命令执行",
    "fileChange": "文件修改",
    "imageGeneration": "图片生成",
    "contextCompaction": "上下文压缩",
    "mcpToolCall": "MCP 工具调用",
    "patchApply": "补丁应用",
    "viewImageToolCall": "查看图片",
    "webSearch": "网页搜索",
}


class AdapterNotificationController:
    def __init__(
        self,
        *,
        lock,
        turn_execution: TurnExecutionCoordinator,
        thread_subscribers: Callable[[str], tuple[ChatBindingKey, ...]],
        get_runtime_state: Callable[[str, str], RuntimeState],
        on_runtime_event_accepted: Callable[[str, str], None],
        apply_runtime_state_message_locked: Callable[[RuntimeState, RuntimeStateMessage], None],
        apply_persisted_runtime_state_message_locked: Callable[[ChatBindingKey, RuntimeState, RuntimeStateMessage], None],
        cancel_mirror_watchdog_locked: Callable[[RuntimeState], None],
        finalize_execution_from_terminal_signal: Callable[..., bool],
        dispatch_execution_card_message: Callable[..., None],
        send_execution_card: Callable[..., str | None],
        schedule_mirror_watchdog: Callable[[str, str], None],
        schedule_execution_card_update: Callable[[str, str], None],
        flush_execution_card: Callable[[str, str, bool], None],
        flush_plan_card: Callable[[str, str], None],
        interrupt_running_turn: Callable[..., None],
        on_server_request_resolved: Callable[[dict[str, Any]], None],
    ) -> None:
        self._lock = lock
        self._turn_execution = turn_execution
        self._thread_subscribers = thread_subscribers
        self._get_runtime_state = get_runtime_state
        self._on_runtime_event_accepted = on_runtime_event_accepted
        self._apply_runtime_state_message_locked = apply_runtime_state_message_locked
        self._apply_persisted_runtime_state_message_locked = apply_persisted_runtime_state_message_locked
        self._cancel_mirror_watchdog_locked = cancel_mirror_watchdog_locked
        self._finalize_execution_from_terminal_signal = finalize_execution_from_terminal_signal
        self._dispatch_execution_card_message = dispatch_execution_card_message
        self._send_execution_card = send_execution_card
        self._schedule_mirror_watchdog = schedule_mirror_watchdog
        self._schedule_execution_card_update = schedule_execution_card_update
        self._flush_execution_card = flush_execution_card
        self._flush_plan_card = flush_plan_card
        self._interrupt_running_turn = interrupt_running_turn
        self._on_server_request_resolved = on_server_request_resolved

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        routes: dict[str, Callable[[dict[str, Any]], None]] = {
            "error": self.handle_error_notification,
            "thread/status/changed": self.handle_thread_status_changed,
            "thread/closed": self.handle_thread_closed,
            "thread/name/updated": self.handle_thread_name_updated,
            "thread/goal/updated": self.handle_thread_goal_updated,
            "thread/goal/cleared": self.handle_thread_goal_cleared,
            "turn/started": self.handle_turn_started,
            "turn/plan/updated": self.handle_turn_plan_updated,
            "item/started": self.handle_item_started,
            "item/agentMessage/delta": self.handle_agent_message_delta,
            "item/commandExecution/outputDelta": self.handle_command_delta,
            "item/fileChange/outputDelta": self.handle_file_change_delta,
            "item/completed": self.handle_item_completed,
            "turn/completed": self.handle_turn_completed,
            "serverRequest/resolved": self._on_server_request_resolved,
        }
        handler = routes.get(method)
        if handler is None:
            return
        handler(params)

    def handle_error_notification(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        turn_id = str(params.get("turnId", "") or "").strip()
        error = params.get("error") or {}
        message = str(error.get("message") or "").strip()
        additional_details = str(error.get("additionalDetails") or "").strip()
        if additional_details:
            message = f"{message}\n{additional_details}".strip() if message else additional_details
        if not message:
            return
        will_retry = bool(params.get("willRetry"))
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    continue
                if will_retry:
                    self._turn_execution.append_process_note_locked(
                        state,
                        text=f"\n[重试中] {message}\n",
                    )
                else:
                    self._turn_execution.apply_terminal_error_locked(
                        state,
                        error_message=message,
                    )
            self._schedule_execution_card_update(*binding)

    def _bindings_for_thread(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ()
        return self._thread_subscribers(normalized_thread_id)

    def _mark_runtime_event_if_current_execution_target(
        self,
        binding: ChatBindingKey,
        state: RuntimeState,
        *,
        thread_id: str,
        turn_id: str,
        allow_missing_turn_id: bool = False,
        allow_unbound_turn_id: bool = False,
    ) -> bool:
        with self._lock:
            runtime = build_runtime_view(state)
            if not self._resolve_current_execution_target(
                runtime,
                thread_id=thread_id,
                turn_id=turn_id,
                allow_missing_turn_id=allow_missing_turn_id,
                allow_unbound_turn_id=allow_unbound_turn_id,
            ):
                return False
            self._turn_execution.mark_runtime_event_locked(state, occurred_at=time.monotonic())
        self._schedule_mirror_watchdog(*binding)
        self._on_runtime_event_accepted(*binding)
        return True

    def _mark_runtime_event_if_current_thread(self, binding: ChatBindingKey, state: RuntimeState, *, thread_id: str) -> bool:
        with self._lock:
            runtime = build_runtime_view(state)
            if runtime.current_thread_id.strip() != thread_id:
                return False
            self._turn_execution.mark_runtime_event_locked(state, occurred_at=time.monotonic())
        self._schedule_mirror_watchdog(*binding)
        self._on_runtime_event_accepted(*binding)
        return True

    @staticmethod
    def _resolve_current_execution_target(
        runtime,
        *,
        thread_id: str,
        turn_id: str,
        allow_missing_turn_id: bool = False,
        allow_unbound_turn_id: bool = False,
    ) -> bool:
        if runtime.current_thread_id.strip() != thread_id:
            return False
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return allow_missing_turn_id
        current_turn_id = runtime.execution.current_turn_id.strip()
        if current_turn_id and current_turn_id != normalized_turn_id:
            return False
        if not current_turn_id and not allow_unbound_turn_id:
            return False
        return True

    @staticmethod
    def _can_bind_unbound_compact_item_started(runtime, *, thread_id: str, turn_id: str, item_type: str) -> bool:
        return (
            item_type == "contextCompaction"
            and bool(str(turn_id or "").strip())
            and runtime.current_thread_id.strip() == thread_id
            and bool(runtime.execution.current_message_id.strip())
            and bool(runtime.execution.awaiting_local_turn_started)
            and not bool(runtime.execution.current_turn_id.strip())
            and runtime.execution.current_execution_kind.strip() == "compact"
        )

    def handle_thread_status_changed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        status = params.get("status") or {}
        status_type = status.get("type")
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                current_turn_id = runtime.execution.current_turn_id.strip()
                current_message_id = runtime.execution.current_message_id.strip()
                awaiting_started = self._turn_execution.awaiting_remote_turn_started_locked(state)
                if status_type == BACKEND_THREAD_STATUS_ACTIVE and not awaiting_started:
                    self._turn_execution.acknowledge_active_thread_locked(state)
            if not self._mark_runtime_event_if_current_thread(binding, state, thread_id=thread_id):
                continue
            if awaiting_started:
                continue
            if status_type != BACKEND_THREAD_STATUS_ACTIVE and (current_turn_id or current_message_id):
                # Upstream can emit `thread/status=systemError` before the paired
                # `error` and `turn/completed(status=failed)` notifications.
                # Finalizing here would retire the execution anchor too early and
                # drop the real failure text, leaving Feishu with an empty card.
                if status_type == BACKEND_THREAD_STATUS_SYSTEM_ERROR:
                    continue
                self._finalize_execution_from_terminal_signal(
                    binding[0],
                    binding[1],
                    thread_id=thread_id,
                    turn_id=current_turn_id,
                )
                continue
            if status_type == BACKEND_THREAD_STATUS_ACTIVE:
                self._schedule_execution_card_update(*binding)
                continue
            with self._lock:
                self._turn_execution.settle_non_active_thread_locked(state)
                self._cancel_mirror_watchdog_locked(state)
            self._flush_execution_card(binding[0], binding[1], True)

    def handle_thread_closed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                current_turn_id = runtime.execution.current_turn_id.strip()
                current_message_id = runtime.execution.current_message_id.strip()
                is_running = runtime.running
                awaiting_started = self._turn_execution.awaiting_remote_turn_started_locked(state)
            if not self._mark_runtime_event_if_current_thread(binding, state, thread_id=thread_id):
                continue
            if awaiting_started:
                continue
            if is_running or current_turn_id or current_message_id:
                self._finalize_execution_from_terminal_signal(
                    binding[0],
                    binding[1],
                    thread_id=thread_id,
                    turn_id=current_turn_id,
                )
                continue
            with self._lock:
                self._turn_execution.settle_thread_closed_locked(state)
                self._cancel_mirror_watchdog_locked(state)

    def handle_thread_name_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._thread_subscribers(thread_id)
        if not bindings:
            return
        new_title = str(params.get("threadName") or "").strip()
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                resolved_title = new_title or runtime.current_thread_title.strip()
            if not self._mark_runtime_event_if_current_thread(binding, state, thread_id=thread_id):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                self._apply_persisted_runtime_state_message_locked(
                    binding,
                    state,
                    ThreadStateChanged(current_thread_title=resolved_title),
                )

    def handle_thread_goal_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        goal = params.get("goal") or {}
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
            if not self._mark_runtime_event_if_current_thread(binding, state, thread_id=thread_id):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                self._apply_runtime_state_message_locked(
                    state,
                    ThreadGoalStateChanged(
                        goal_objective=str(goal.get("objective", "") or "").strip(),
                        goal_status=str(goal.get("status", "") or "").strip(),
                        goal_token_budget=goal.get("tokenBudget"),
                        goal_tokens_used=int(goal.get("tokensUsed") or 0),
                        goal_time_used_seconds=int(goal.get("timeUsedSeconds") or 0),
                        goal_created_at=int(goal.get("createdAt") or 0),
                        goal_updated_at=int(goal.get("updatedAt") or 0),
                    ),
                )

    def handle_thread_goal_cleared(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
            if not self._mark_runtime_event_if_current_thread(binding, state, thread_id=thread_id):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if runtime.current_thread_id.strip() != thread_id:
                    continue
                self._apply_runtime_state_message_locked(state, ThreadGoalCleared())

    def handle_turn_started(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id", "") or "").strip()
        interrupt_sent = False
        interrupt_succeeded = False
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
                allow_unbound_turn_id=True,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(
                    runtime,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    allow_unbound_turn_id=True,
                ):
                    continue
                transition = self._turn_execution.prepare_turn_started_locked(
                    state,
                    turn_id=turn_id,
                    started_at=time.monotonic(),
                )
                self._turn_execution.clear_plan_state_locked(state)
            if not transition.reuse_existing_card:
                if transition.previous_execution_card is not None:
                    self._dispatch_execution_card_message(
                        transition.previous_execution_card.message_id,
                        transcript=transition.previous_execution_card.transcript,
                        running=False,
                        elapsed=transition.previous_execution_card.elapsed,
                        cancelled=transition.previous_execution_card.cancelled,
                    )
                card_id = self._send_execution_card(binding[1], "")
                with self._lock:
                    runtime = build_runtime_view(state)
                    if (
                        runtime.current_thread_id.strip() == thread_id
                        and runtime.execution.current_turn_id.strip() == turn_id
                    ):
                        self._apply_runtime_state_message_locked(
                            state,
                            ExecutionStateChanged(
                                current_message_id=card_id or "",
                                last_execution_message_id="",
                            ),
                        )
            if transition.should_interrupt_started_turn:
                if not interrupt_sent:
                    interrupt_sent = True
                    try:
                        self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
                    except Exception:
                        logger.exception("turn 启动后自动取消失败")
                    else:
                        interrupt_succeeded = True
                if interrupt_succeeded:
                    with self._lock:
                        self._turn_execution.confirm_cancel_requested_locked(state)
            self._schedule_execution_card_update(*binding)

    def handle_turn_plan_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        turn_id = str(params.get("turnId", "") or "").strip()
        plan = params.get("plan") or []
        explanation = params.get("explanation") or ""
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    continue
                if not self._turn_execution.update_plan_outline_locked(
                    state,
                    turn_id=turn_id,
                    explanation=explanation,
                    plan=plan,
                ):
                    continue
            self._flush_plan_card(*binding)

    def handle_item_started(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        if not bindings:
            return
        interrupt_sent = False
        interrupt_succeeded = False
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            bound_unstarted_compact = False
            should_interrupt_started_turn = False
            with self._lock:
                runtime = build_runtime_view(state)
                can_handle = self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id)
                can_bind_compact = False
                if not can_handle:
                    can_bind_compact = self._can_bind_unbound_compact_item_started(
                        runtime,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_type=item_type,
                    )
                if not can_handle and not can_bind_compact:
                    continue
                self._turn_execution.mark_runtime_event_locked(state, occurred_at=time.monotonic())
            self._schedule_mirror_watchdog(*binding)
            self._on_runtime_event_accepted(*binding)
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    if not self._can_bind_unbound_compact_item_started(
                        runtime,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_type=item_type,
                    ):
                        continue
                    transition = self._turn_execution.prepare_turn_started_locked(
                        state,
                        turn_id=turn_id,
                        started_at=time.monotonic(),
                    )
                    self._turn_execution.clear_plan_state_locked(state)
                    bound_unstarted_compact = True
                    should_interrupt_started_turn = transition.should_interrupt_started_turn
                if item_type == "commandExecution":
                    command = item.get("command") or ""
                    cwd = item.get("cwd") or ""
                    self._turn_execution.start_process_block_locked(
                        state,
                        text=f"\n$ ({display_path(cwd)}) {command}\n",
                        marks_work=True,
                    )
                elif item_type == "fileChange":
                    self._turn_execution.start_process_block_locked(
                        state,
                        text="\n[准备应用文件修改]\n",
                        marks_work=True,
                    )
                elif item_type in WORK_ITEM_LABELS:
                    self._turn_execution.append_process_note_locked(
                        state,
                        text=f"\n[{WORK_ITEM_LABELS[item_type]}]\n",
                        marks_work=True,
                    )
                else:
                    continue
            if should_interrupt_started_turn:
                if not interrupt_sent:
                    interrupt_sent = True
                    try:
                        self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
                    except Exception:
                        logger.exception("turn 启动后自动取消失败")
                    else:
                        interrupt_succeeded = True
                if interrupt_succeeded:
                    with self._lock:
                        self._turn_execution.confirm_cancel_requested_locked(state)
            self._schedule_execution_card_update(*binding)

    def handle_agent_message_delta(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        delta = str(params.get("delta", "") or "")
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    continue
                self._turn_execution.append_assistant_delta_locked(
                    state,
                    delta=delta,
                )
            self._schedule_execution_card_update(*binding)

    def handle_command_delta(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        self._append_log_by_thread(thread_id, str(params.get("delta", "") or ""), turn_id=turn_id)

    def handle_file_change_delta(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        self._append_log_by_thread(thread_id, str(params.get("delta", "") or ""), turn_id=turn_id)

    def handle_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            if item_type == "commandExecution":
                with self._lock:
                    runtime = build_runtime_view(state)
                    if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                        continue
                    self._turn_execution.finish_process_block_locked(
                        state,
                        suffix=f"\n[命令结束 status={item.get('status')} exit={item.get('exitCode')}]\n",
                    )
                self._schedule_execution_card_update(*binding)
            elif item_type == "fileChange":
                changes = item.get("changes") or []
                suffix = ""
                if changes:
                    summary = "\n".join(
                        f"- {change.get('kind', 'update')}: {change.get('path', '')}"
                        for change in changes[:20]
                    )
                    suffix = f"\n[文件变更]\n{summary}\n"
                with self._lock:
                    runtime = build_runtime_view(state)
                    if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                        continue
                    self._turn_execution.finish_process_block_locked(state, suffix=suffix)
                self._schedule_execution_card_update(*binding)
            elif item_type == "agentMessage" and item.get("text"):
                with self._lock:
                    runtime = build_runtime_view(state)
                    if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                        continue
                    self._turn_execution.reconcile_current_assistant_text_locked(
                        state,
                        text=str(item.get("text", "") or ""),
                    )
                self._schedule_execution_card_update(*binding)
            elif item_type in WORK_ITEM_LABELS:
                with self._lock:
                    runtime = build_runtime_view(state)
                    if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                        continue
                    self._turn_execution.finish_process_block_locked(state)
                self._schedule_execution_card_update(*binding)
            elif item_type == "plan" and item.get("text"):
                with self._lock:
                    runtime = build_runtime_view(state)
                    if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                        continue
                    if not self._turn_execution.update_plan_text_locked(
                        state,
                        turn_id=turn_id,
                        text=str(item.get("text", "") or ""),
                    ):
                        continue
                self._flush_plan_card(*binding)

    def handle_turn_completed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        status = str(turn.get("status", "") or "").strip()
        turn_id = str(turn.get("id", "") or "").strip()
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    continue
                self._turn_execution.apply_turn_completed_locked(
                    state,
                    status=status,
                    error_message=str(error.get("message") or "执行失败").strip() if error else "",
                )
                current_turn_id = build_runtime_view(state).execution.current_turn_id.strip()
            self._finalize_execution_from_terminal_signal(
                binding[0],
                binding[1],
                thread_id=thread_id,
                turn_id=turn_id or current_turn_id,
            )

    def _append_log_by_thread(self, thread_id: str, text: str, *, turn_id: str = "") -> None:
        bindings = self._bindings_for_thread(thread_id)
        if not bindings:
            return
        for binding in bindings:
            state = self._get_runtime_state(*binding)
            if not self._mark_runtime_event_if_current_execution_target(
                binding,
                state,
                thread_id=thread_id,
                turn_id=turn_id,
            ):
                continue
            with self._lock:
                runtime = build_runtime_view(state)
                if not self._resolve_current_execution_target(runtime, thread_id=thread_id, turn_id=turn_id):
                    continue
                self._turn_execution.append_process_delta_locked(state, text=text)
            self._schedule_execution_card_update(*binding)
