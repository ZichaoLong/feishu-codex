"""Parse app-server notifications and execute exact Feishu projection effects."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from bot.adapter_notification_runtime import (
    AdapterNotificationRuntimeTransitions,
    AssistantDeltaNotificationCommand,
    ErrorNotificationCommand,
    ExecutionRuntimeEventCommand,
    FinishProcessBlockCommand,
    ItemStartedRuntimeEventCommand,
    MarkProcessWorkCommand,
    NotificationPlanStep,
    PlanOutlineNotificationCommand,
    PlanTextNotificationCommand,
    ProcessItemStartedCommand,
    ReconcileAssistantTextCommand,
    RecordUnavailableAssistantCompletionCommand,
    RestoreCancelPendingCommand,
    ThreadClosedNotificationCommand,
    ThreadGoalClearedNotificationCommand,
    ThreadGoalNotificationCommand,
    ThreadRuntimeEventCommand,
    ThreadStatusNotificationCommand,
    ThreadTitleNotificationCommand,
    TurnCompletedNotificationCommand,
    TurnStartedNotificationCommand,
    TurnStartedRuntimeEventCommand,
    WorkItemStartedCommand,
)
from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.execution_pages import ExecutionTranscriptCursor
from bot.feishu_execution_process_projection import (
    FeishuExecutionProcessProjection,
)
from bot.execution_transcript import (
    ExecutionTranscriptSnapshot,
    agent_message_can_be_terminal_candidate,
    is_execution_work_item_type,
    is_terminal_invalidating_work_item_type,
)
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)


logger = logging.getLogger(__name__)


WORK_ITEM_LABELS = {
    "collabAgentToolCall": "协作工具调用",
    "commandExecution": "命令执行",
    "dynamicToolCall": "动态工具调用",
    "enteredReviewMode": "进入代码审查",
    "exitedReviewMode": "结束代码审查",
    "fileChange": "文件修改",
    "imageView": "查看图片",
    "imageGeneration": "图片生成",
    "contextCompaction": "上下文压缩",
    "mcpToolCall": "MCP 工具调用",
    "patchApply": "补丁应用",
    "sleep": "等待",
    "webSearch": "网页搜索",
}

_UPSTREAM_NOTICE_METHODS = frozenset(
    {"warning", "guardianWarning", "deprecationNotice", "configWarning"}
)


class _FinalizeExecution(Protocol):
    def __call__(
        self,
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool: ...


class _DispatchExecutionCard(Protocol):
    def __call__(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript: ExecutionTranscriptSnapshot,
        running: bool,
        elapsed: int,
        cancelled: bool,
        cursor_start: ExecutionTranscriptCursor,
        cursor_end: ExecutionTranscriptCursor,
    ) -> None: ...


class _OpenInitialExecutionPage(Protocol):
    def __call__(
        self,
        session: BindingSessionSnapshot,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
        reserved_message_id: str = "",
    ) -> InitialExecutionPageOpenResult: ...


class _InterruptRunningTurn(Protocol):
    def __call__(self, *, thread_id: str, turn_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class AdapterNotificationEffects:
    """Effects that consume an immutable exact-session capability."""

    finalize_execution_from_terminal_signal: _FinalizeExecution
    dispatch_execution_card_message: _DispatchExecutionCard
    open_initial_execution_page: _OpenInitialExecutionPage
    schedule_mirror_watchdog: Callable[[BindingSessionSnapshot], None]
    schedule_execution_card_update: Callable[[BindingSessionSnapshot], None]
    flush_execution_card: Callable[[BindingSessionSnapshot, bool], None]
    flush_plan_card: Callable[[BindingSessionSnapshot], None]
    interrupt_running_turn: _InterruptRunningTurn
    is_pre_send_error: Callable[[Exception], bool]


class AdapterNotificationController:
    """Own notification parsing and immutable external-effect ordering."""

    def __init__(
        self,
        *,
        runtime: AdapterNotificationRuntimeTransitions,
        thread_subscribers: Callable[[str], tuple[ChatBindingKey, ...]],
        effects: AdapterNotificationEffects,
    ) -> None:
        self._runtime = runtime
        self._thread_subscribers = thread_subscribers
        self._effects = effects

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in _UPSTREAM_NOTICE_METHODS:
            logger.warning(
                "Codex app-server notice: method=%s params=%r",
                method,
                params,
            )
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
            "item/fileChange/patchUpdated": self.handle_file_change_patch_updated,
            "item/completed": self.handle_item_completed,
            "turn/completed": self.handle_turn_completed,
        }
        handler = routes.get(method)
        if handler is not None:
            handler(params)

    def handle_error_notification(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        turn_id = str(params.get("turnId", "") or "").strip()
        error = params.get("error") or {}
        message = str(error.get("message") or "").strip()
        additional_details = str(error.get("additionalDetails") or "").strip()
        if additional_details:
            message = (
                f"{message}\n{additional_details}".strip()
                if message
                else additional_details
            )
        if not bindings or not message:
            return
        will_retry = bool(params.get("willRetry"))
        for binding in bindings:
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            updated = self._runtime.apply_error(
                ErrorNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    message=message,
                    will_retry=will_retry,
                )
            )
            if updated is not None:
                self._effects.schedule_execution_card_update(updated)

    def handle_thread_status_changed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        bindings = self._bindings_for_thread(thread_id)
        status = params.get("status") or {}
        status_type = str(status.get("type") or "").strip()
        for binding in bindings:
            marked = self._mark_thread_event(binding, thread_id)
            if marked is None:
                continue
            transition = self._runtime.apply_thread_status(
                ThreadStatusNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    status_type=status_type,
                )
            )
            if transition is not None:
                cancel_runtime_timer_effects(transition.timer_cancellations)
            if transition is None or transition.action == "none":
                continue
            if transition.action == "finalize":
                self._effects.finalize_execution_from_terminal_signal(
                    transition.session,
                    thread_id=thread_id,
                    turn_id=transition.turn_id,
                )
            elif transition.action == "schedule_execution_card":
                self._effects.schedule_execution_card_update(transition.session)
            elif transition.action == "flush_execution_card":
                self._effects.flush_execution_card(transition.session, True)

    def handle_thread_closed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_thread_event(binding, thread_id)
            if marked is None:
                continue
            transition = self._runtime.apply_thread_closed(
                ThreadClosedNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                )
            )
            if transition is not None:
                cancel_runtime_timer_effects(transition.timer_cancellations)
            if transition is not None and transition.action == "finalize":
                self._effects.finalize_execution_from_terminal_signal(
                    transition.session,
                    thread_id=thread_id,
                    turn_id=transition.turn_id,
                )

    def handle_thread_name_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        new_title = str(params.get("threadName") or "").strip()
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_thread_event(binding, thread_id)
            if marked is None:
                continue
            self._runtime.apply_thread_title(
                ThreadTitleNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    title=new_title or marked.current_thread_title.strip(),
                )
            )

    def handle_thread_goal_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        goal = params.get("goal") or {}
        raw_budget = goal.get("tokenBudget")
        token_budget = None if raw_budget is None else int(raw_budget)
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_thread_event(binding, thread_id)
            if marked is None:
                continue
            self._runtime.apply_thread_goal(
                ThreadGoalNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    objective=str(goal.get("objective", "") or "").strip(),
                    status=str(goal.get("status", "") or "").strip(),
                    token_budget=token_budget,
                    tokens_used=int(goal.get("tokensUsed") or 0),
                    time_used_seconds=int(goal.get("timeUsedSeconds") or 0),
                    created_at=int(goal.get("createdAt") or 0),
                    updated_at=int(goal.get("updatedAt") or 0),
                )
            )

    def handle_thread_goal_cleared(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_thread_event(binding, thread_id)
            if marked is None:
                continue
            self._runtime.clear_thread_goal(
                ThreadGoalClearedNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                )
            )

    def handle_turn_started(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id", "") or "").strip()
        interrupt_sent = False
        interrupt_failure: Exception | None = None
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_turn_started_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            transition = self._runtime.apply_turn_started(
                TurnStartedNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    thread_id=thread_id,
                    turn_id=turn_id,
                    started_at=time.monotonic(),
                )
            )
            if transition is None:
                continue
            current = transition.session
            if not transition.reuse_existing_card:
                previous = transition.previous_execution_card
                if previous is not None:
                    self._effects.dispatch_execution_card_message(
                        current.binding[1],
                        previous.message_id,
                        transcript=previous.transcript,
                        running=False,
                        elapsed=previous.elapsed,
                        cancelled=previous.cancelled,
                        cursor_start=previous.cursor_start,
                        cursor_end=previous.cursor_end,
                    )
                    current = self._runtime.current_session(
                        BindingExecutionTarget.from_session(current)
                    )
                    if current is None:
                        continue
                page_result = self._effects.open_initial_execution_page(
                    current,
                    "",
                )
                if page_result.session is None:
                    continue
                current = page_result.session
                if page_result.status is InitialExecutionPageOpenStatus.REJECTED:
                    logger.error(
                        "adapter-observed turn has no Feishu execution page: "
                        "binding=%s turn=%s",
                        current.binding,
                        turn_id,
                    )
            if transition.should_interrupt_started_turn:
                cancel_target = BindingExecutionTarget.from_session(current)
                if not interrupt_sent:
                    interrupt_sent = True
                    try:
                        self._effects.interrupt_running_turn(
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                    except Exception as exc:
                        interrupt_failure = exc
                        logger.exception("turn 启动后自动取消失败")
                if (
                    interrupt_failure is not None
                    and self._effects.is_pre_send_error(interrupt_failure)
                ):
                    current = self._runtime.restore_cancel_pending(
                        RestoreCancelPendingCommand(target=cancel_target)
                    )
                else:
                    current = self._runtime.current_session(cancel_target)
                if current is None:
                    continue
            # The turn/start response identifies the submission, not the
            # authoritative active turn.  Reinstall the watchdog only after
            # turn/started has bound that identity (and any execution page),
            # otherwise its ticket can retain the blank admission identity.
            self._effects.schedule_mirror_watchdog(current)
            self._effects.schedule_execution_card_update(current)

    def handle_turn_plan_updated(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        plan = params.get("plan") or []
        steps = tuple(
            NotificationPlanStep(
                step=str(item.get("step", "") or "").strip(),
                status=str(item.get("status", "") or "").strip(),
            )
            for item in plan
            if str(item.get("step", "") or "").strip()
        )
        explanation = str(params.get("explanation") or "")
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            updated = self._runtime.apply_plan_outline(
                PlanOutlineNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    turn_id=turn_id,
                    explanation=explanation,
                    steps=steps,
                )
            )
            if updated is not None:
                self._effects.flush_plan_card(updated)

    def handle_item_started(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        interrupt_sent = False
        interrupt_failure: Exception | None = None
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_item_started_event(
                binding,
                thread_id,
                turn_id,
                item_type,
            )
            if marked is None:
                continue
            target = BindingExecutionTarget.from_session(marked)
            if item_type == "commandExecution":
                projection_text = FeishuExecutionProcessProjection.command_started(
                    item,
                    transcript=marked.execution.transcript,
                )
                transition = self._runtime.start_process_item(
                    ProcessItemStartedCommand(
                        target=target,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_type=item_type,
                        text=projection_text,
                        started_at=time.monotonic(),
                    )
                )
            elif item_type == "fileChange":
                projection_text = (
                    FeishuExecutionProcessProjection.file_change_started(
                        transcript=marked.execution.transcript,
                    )
                )
                transition = self._runtime.start_process_item(
                    ProcessItemStartedCommand(
                        target=target,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_type=item_type,
                        text=projection_text,
                        started_at=time.monotonic(),
                    )
                )
            elif is_execution_work_item_type(item_type):
                label = WORK_ITEM_LABELS.get(item_type, "")
                transition = self._runtime.start_work_item(
                    WorkItemStartedCommand(
                        target=target,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        item_type=item_type,
                        text=f"\n[{label}]\n" if label else "",
                        started_at=time.monotonic(),
                    )
                )
            else:
                continue
            if transition is None:
                continue
            current = transition.session
            if transition.should_interrupt_started_turn:
                cancel_target = BindingExecutionTarget.from_session(current)
                if not interrupt_sent:
                    interrupt_sent = True
                    try:
                        self._effects.interrupt_running_turn(
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                    except Exception as exc:
                        interrupt_failure = exc
                        logger.exception("turn 启动后自动取消失败")
                if (
                    interrupt_failure is not None
                    and self._effects.is_pre_send_error(interrupt_failure)
                ):
                    current = self._runtime.restore_cancel_pending(
                        RestoreCancelPendingCommand(target=cancel_target)
                    )
                else:
                    current = self._runtime.current_session(cancel_target)
                if current is None:
                    continue
            self._effects.schedule_execution_card_update(current)

    def handle_agent_message_delta(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        delta = str(params.get("delta", "") or "")
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            updated = self._runtime.append_assistant_delta(
                AssistantDeltaNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    delta=delta,
                )
            )
            if updated is not None:
                self._effects.schedule_execution_card_update(updated)

    def handle_command_delta(self, params: dict[str, Any]) -> None:
        self._observe_process_progress_by_thread(
            str(params.get("threadId", "") or "").strip(),
            turn_id=str(params.get("turnId", "") or "").strip(),
        )

    def handle_file_change_patch_updated(self, params: dict[str, Any]) -> None:
        self._observe_process_progress_by_thread(
            str(params.get("threadId", "") or "").strip(),
            turn_id=str(params.get("turnId", "") or "").strip(),
        )

    def handle_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        thread_id = str(params.get("threadId", "") or "").strip()
        turn_id = str(params.get("turnId", "") or "").strip()
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            target = BindingExecutionTarget.from_session(marked)
            if item_type == "commandExecution":
                updated = self._runtime.finish_process_block(
                    FinishProcessBlockCommand(
                        target=target,
                        suffix=FeishuExecutionProcessProjection.command_completed(
                            item,
                            transcript=marked.execution.transcript,
                        ),
                        marks_work=True,
                    )
                )
                projection = "execution"
            elif item_type == "fileChange":
                updated = self._runtime.finish_process_block(
                    FinishProcessBlockCommand(
                        target=target,
                        suffix=(
                            FeishuExecutionProcessProjection.file_change_completed(
                                item,
                                transcript=marked.execution.transcript,
                            )
                        ),
                        marks_work=True,
                    )
                )
                projection = "execution"
            elif item_type == "agentMessage":
                raw_text = item.get("text")
                if type(raw_text) is not str:
                    updated = self._runtime.record_unavailable_assistant_completion(
                        RecordUnavailableAssistantCompletionCommand(target=target)
                    )
                else:
                    updated = self._runtime.reconcile_assistant_text(
                        ReconcileAssistantTextCommand(
                            target=target,
                            text=raw_text,
                            terminal_candidate=(
                                agent_message_can_be_terminal_candidate(
                                    item.get("phase")
                                )
                            ),
                            item_id=(
                                item.get("id", "").strip()
                                if type(item.get("id")) is str
                                else ""
                            ),
                        )
                    )
                projection = "execution"
            elif item_type == "plan" and item.get("text"):
                updated = self._runtime.apply_plan_text(
                    PlanTextNotificationCommand(
                        target=target,
                        turn_id=turn_id,
                        text=str(item.get("text", "") or ""),
                    )
                )
                projection = "plan"
            elif is_execution_work_item_type(item_type):
                updated = self._runtime.finish_process_block(
                    FinishProcessBlockCommand(
                        target=target,
                        marks_work=is_terminal_invalidating_work_item_type(
                            item_type
                        ),
                    )
                )
                projection = "execution"
            else:
                continue
            if updated is None:
                continue
            if projection == "plan":
                self._effects.flush_plan_card(updated)
            else:
                self._effects.schedule_execution_card_update(updated)

    def handle_turn_completed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", "") or "").strip()
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        status = str(turn.get("status", "") or "").strip()
        turn_id = str(turn.get("id", "") or "").strip()
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            updated = self._runtime.apply_turn_completed(
                TurnCompletedNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    status=status,
                    error_message=(
                        str(error.get("message") or "执行失败").strip()
                        if error
                        else ""
                    ),
                )
            )
            if updated is not None:
                self._effects.finalize_execution_from_terminal_signal(
                    updated,
                    thread_id=thread_id,
                    turn_id=turn_id or updated.execution.current_turn_id.strip(),
                )

    def _observe_process_progress_by_thread(
        self,
        thread_id: str,
        *,
        turn_id: str = "",
    ) -> None:
        for binding in self._bindings_for_thread(thread_id):
            marked = self._mark_execution_event(binding, thread_id, turn_id)
            if marked is None:
                continue
            self._runtime.mark_process_work(
                MarkProcessWorkCommand(
                    target=BindingExecutionTarget.from_session(marked),
                )
            )

    def _bindings_for_thread(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        normalized = str(thread_id or "").strip()
        return self._thread_subscribers(normalized) if normalized else ()

    def _mark_thread_event(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> BindingSessionSnapshot | None:
        captured = self._runtime.resident_session(binding)
        if captured is None:
            return None
        marked = self._runtime.mark_thread_runtime_event(
            ThreadRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=thread_id,
                occurred_at=time.monotonic(),
            )
        )
        return self._after_runtime_event(marked)

    def _mark_execution_event(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        turn_id: str,
    ) -> BindingSessionSnapshot | None:
        captured = self._runtime.resident_session(binding)
        if captured is None:
            return None
        marked = self._runtime.mark_execution_runtime_event(
            ExecutionRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=thread_id,
                turn_id=turn_id,
                occurred_at=time.monotonic(),
            )
        )
        return self._after_runtime_event(marked)

    def _mark_turn_started_event(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        turn_id: str,
    ) -> BindingSessionSnapshot | None:
        captured = self._runtime.resident_session(binding)
        if captured is None:
            return None
        marked = self._runtime.mark_turn_started_runtime_event(
            TurnStartedRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=thread_id,
                turn_id=turn_id,
                occurred_at=time.monotonic(),
            )
        )
        # turn/started binds the authoritative turn identity in the next
        # transition.  Its watchdog is installed once from that post-bind
        # snapshot in handle_turn_started().
        return marked

    def _mark_item_started_event(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        turn_id: str,
        item_type: str,
    ) -> BindingSessionSnapshot | None:
        captured = self._runtime.resident_session(binding)
        if captured is None:
            return None
        marked = self._runtime.mark_item_started_runtime_event(
            ItemStartedRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=thread_id,
                turn_id=turn_id,
                item_type=item_type,
                occurred_at=time.monotonic(),
            )
        )
        return self._after_runtime_event(marked)

    def _after_runtime_event(
        self,
        session: BindingSessionSnapshot | None,
    ) -> BindingSessionSnapshot | None:
        if session is None:
            return None
        self._effects.schedule_mirror_watchdog(session)
        return session
