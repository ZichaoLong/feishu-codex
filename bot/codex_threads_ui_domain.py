"""
Codex threads UI domain.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.cards import (
    CommandResult,
    build_markdown_card,
    build_rename_card,
    build_resume_active_goal_confirm_card,
    build_threads_card,
    build_threads_closed_card,
    build_threads_pending_card,
    make_card_response,
)
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.runtime_state import BACKEND_THREAD_STATUS_NOT_LOADED
from bot.runtime_view import RuntimeView

logger = logging.getLogger(__name__)

_RESUME_USAGE = feishu_visible_command_syntax("/resume <thread_id|thread_name>")
_RENAME_USAGE = feishu_visible_command_syntax("/rename <新标题>")


def _is_goals_feature_disabled_error(exc: Exception) -> bool:
    if not isinstance(exc, CodexRpcError):
        return False
    return str(exc.error.get("message", "") or "").strip().lower() == "goals feature is disabled"


class _SubmitToRuntime(Protocol):
    def __call__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None: ...


class _ResumeThreadOnRuntime(Protocol):
    def __call__(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str | None = None,
        summary: ThreadSummary | None = None,
        pause_active_goal_on_resume: bool = False,
        message_id: str = "",
        refresh_threads_message_id: str = "",
    ) -> None: ...


class _GetRuntimeView(Protocol):
    def __call__(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView: ...


class _IsGroupChat(Protocol):
    def __call__(self, chat_id: str, message_id: str = "") -> bool: ...


class _IsGroupAdminActor(Protocol):
    def __call__(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool: ...


class _RenameBoundThreadTitle(Protocol):
    def __call__(
        self,
        sender_id: str,
        chat_id: str,
        title: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> bool: ...


class _ReplyText(Protocol):
    def __call__(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...


class _ResolveResumeTarget(Protocol):
    def __call__(self, arg: str) -> ThreadSummary: ...


class _ListVisibleCurrentDirThreads(Protocol):
    def __call__(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> list[ThreadSummary]: ...


class _ReadThreadSummaryAuthoritatively(Protocol):
    def __call__(self, thread_id: str, *, original_arg: str) -> ThreadSummary: ...


class _GetThreadGoal(Protocol):
    def __call__(self, thread_id: str) -> ThreadGoalSummary | None: ...


class _ArchiveThreadForControl(Protocol):
    def __call__(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]: ...


class _RenameThread(Protocol):
    def __call__(self, thread_id: str, name: str) -> None: ...


class _PatchMessage(Protocol):
    def __call__(self, message_id: str, content: str) -> bool: ...


class _IsThreadNotLoadedError(Protocol):
    def __call__(self, exc: Exception) -> bool: ...


@dataclass(frozen=True, slots=True)
class ThreadsUiRuntimePorts:
    submit_to_runtime: _SubmitToRuntime
    resume_thread_on_runtime: _ResumeThreadOnRuntime


@dataclass(frozen=True, slots=True)
class ThreadsUiPorts:
    get_runtime_view: _GetRuntimeView
    is_group_chat: _IsGroupChat
    is_group_admin_actor: _IsGroupAdminActor
    rename_bound_thread_title: _RenameBoundThreadTitle
    reply_text: _ReplyText
    resolve_resume_target: _ResolveResumeTarget
    list_visible_current_dir_threads: _ListVisibleCurrentDirThreads
    read_thread_summary_authoritatively: _ReadThreadSummaryAuthoritatively
    get_thread_goal: _GetThreadGoal
    archive_thread_for_control: _ArchiveThreadForControl
    rename_thread: _RenameThread
    patch_message: _PatchMessage
    is_thread_not_loaded_error: _IsThreadNotLoadedError
    threads_initial_limit: int

class CodexThreadsUiDomain:
    def __init__(self, *, ports: ThreadsUiPorts, runtime_ports: ThreadsUiRuntimePorts) -> None:
        self._ports = ports
        self._runtime_ports = runtime_ports
        self._lock = threading.RLock()
        self._expanded_threads_cards: set[str] = set()
        self._pending_rename_forms: dict[str, dict[str, str]] = {}

    def pending_rename_form_snapshot(self, message_id: str) -> dict[str, str] | None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None
        with self._lock:
            pending = self._pending_rename_forms.get(normalized_message_id)
            if pending is None:
                return None
            return dict(pending)

    def register_pending_rename_form(self, message_id: str, *, thread_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_message_id:
            raise ValueError("message_id 不能为空")
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空")
        with self._lock:
            self._pending_rename_forms[normalized_message_id] = {"thread_id": normalized_thread_id}

    def handle_threads_command(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> CommandResult:
        try:
            card = self._render_threads_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("获取线程列表失败")
            return CommandResult(text=f"获取线程列表失败：{exc}")
        return CommandResult(card=card)

    def handle_resume_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult | None:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能切换线程，请等待结束或先执行 `/cancel`。")
        if not arg:
            return CommandResult(
                text=f"用法：`{_RESUME_USAGE}`\n发送 `/help thread` 查看 `/threads` 与 `/resume` 的区别。"
            )
        target = arg.strip()
        try:
            thread = self._ports.resolve_resume_target(target)
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            return CommandResult(text=f"恢复线程失败：{exc}")
        goal = None
        try:
            goal = self._ports.get_thread_goal(thread.thread_id)
        except Exception as exc:
            if not _is_goals_feature_disabled_error(exc):
                logger.exception("读取 thread goal 失败")
                return CommandResult(text=f"恢复线程失败：{exc}")
        if self._resume_requires_active_goal_confirm(thread, goal):
            return CommandResult(
                card=build_resume_active_goal_confirm_card(
                    thread_id=thread.thread_id,
                    thread_title=thread.title,
                    origin="command",
                )
            )
        return CommandResult(
            card=self._build_resume_pending_command_card(target),
            after_dispatch=lambda: self._runtime_ports.submit_to_runtime(
                self._resume_target_on_runtime,
                sender_id,
                chat_id,
                thread.thread_id,
                original_arg=target,
                summary=thread,
                message_id=message_id,
            ),
        )

    def handle_rename_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if not runtime.current_thread_id:
            return CommandResult(text="当前没有绑定线程，无法重命名。")
        if not arg:
            return CommandResult(text=f"用法：`{_RENAME_USAGE}`")
        try:
            self._ports.rename_thread(runtime.current_thread_id, arg)
        except Exception as exc:
            logger.exception("重命名线程失败")
            return CommandResult(text=f"重命名失败：{exc}")
        self._ports.rename_bound_thread_title(
            sender_id,
            chat_id,
            arg,
            message_id=message_id,
            thread_id=runtime.current_thread_id,
        )
        return CommandResult(text=f"已重命名为：{arg}")

    def handle_archive_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能归档线程，请等待结束或先执行 `/cancel`。")
        target = arg.strip() if arg else ""
        if target:
            try:
                thread = self._ports.resolve_resume_target(target)
            except Exception as exc:
                logger.exception("解析归档目标失败")
                return CommandResult(text=f"归档线程失败：{exc}")
        else:
            if not runtime.current_thread_id:
                return CommandResult(text="用法：`/archive [thread_id 或 thread_name]`；省略参数时归档当前线程。")
            try:
                thread = self._ports.read_thread_summary_authoritatively(
                    runtime.current_thread_id,
                    original_arg=runtime.current_thread_id,
                )
            except Exception as exc:
                logger.exception("读取当前线程失败")
                return CommandResult(text=f"归档线程失败：{exc}")

        try:
            result = self._ports.archive_thread_for_control(thread.thread_id, summary=thread)
        except Exception as exc:
            logger.exception("归档线程失败")
            return CommandResult(text=f"归档线程失败：{exc}")
        upstream_outcome = str(result.get("upstream_outcome", "success") or "success")
        if upstream_outcome == "unknown":
            return CommandResult(
                text=(
                    "归档请求结果未知：服务未拿到上游的明确结果。\n"
                    "请先用 `focusctl thread list --archived --scope global` 核对，不要直接重试。\n"
                    f"诊断：{result.get('outcome_detail') or 'transport/timeout'}"
                )
            )
        if upstream_outcome != "success":
            return CommandResult(
                text=(
                    f"归档线程失败：{result.get('upstream_error') or '上游返回错误'}\n"
                    "上游错误仍可能伴随运行态卸载等局部副作用。"
                )
            )
        lines = [
            f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}",
            "说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。",
            "安全范围：本次只协调了本机已知 Focus/fcodex runtime；未覆盖裸 Codex、IDE 或其他机器。",
        ]
        cleared_binding_ids = list(result.get("cleared_binding_ids") or [])
        if cleared_binding_ids:
            lines.append(f"已同步清理当前实例里仍指向该 thread 的 bindings：`{len(cleared_binding_ids)}` 个。")
        if result.get("focus_cleanup") == "incomplete":
            lines.append("本地 Focus 清理不完整；请检查日志，并使用 `thread clear-archived-bindings` 修复残留。")
        return CommandResult(text="\n".join(lines))

    def handle_close_threads_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del action_value
        self._set_threads_card_expanded(message_id, expanded=False)
        return make_card_response(
            card=build_threads_closed_card(),
            toast="已收起。",
            toast_type="success",
        )

    def handle_reopen_threads_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._set_threads_card_expanded(message_id, expanded=True)
        return self._handle_threads_refresh_action(sender_id, chat_id, message_id=message_id, toast="已展开。")

    def handle_resume_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return make_card_response(
                toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                toast_type="warning",
            )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return make_card_response(toast="缺少 thread_id", toast_type="warning")
        thread_title = str(action_value.get("thread_title", "") or action_value.get("title", "")).strip() or thread_id
        try:
            thread = self._ports.read_thread_summary_authoritatively(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取恢复目标失败")
            return make_card_response(toast=f"恢复线程失败：{exc}", toast_type="warning")
        goal = None
        try:
            goal = self._ports.get_thread_goal(thread.thread_id)
        except Exception as exc:
            if not _is_goals_feature_disabled_error(exc):
                logger.exception("读取 thread goal 失败")
                return make_card_response(toast=f"恢复线程失败：{exc}", toast_type="warning")
        if self._resume_requires_active_goal_confirm(thread, goal):
            return make_card_response(
                card=build_resume_active_goal_confirm_card(
                    thread_id=thread.thread_id,
                    thread_title=thread.title,
                    origin="threads_card",
                )
            )
        self._runtime_ports.submit_to_runtime(
            self._resume_target_on_runtime,
            sender_id,
            chat_id,
            thread.thread_id,
            original_arg=thread_id,
            summary=thread,
            message_id=message_id,
            refresh_threads_message_id=message_id,
        )
        return make_card_response(card=build_threads_pending_card(thread.thread_id, title=thread_title))

    def handle_resume_thread_confirm_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return make_card_response(
                toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                toast_type="warning",
            )
        thread_id = str(action_value.get("thread_id", "") or "").strip()
        if not thread_id:
            return make_card_response(toast="缺少 thread_id", toast_type="warning")
        thread_title = str(action_value.get("thread_title", "") or "").strip() or thread_id
        origin = str(action_value.get("origin", "") or "").strip() or "command"
        pause_active_goal_on_resume = (
            str(action_value.get("pause_active_goal_on_resume", "") or "").strip().lower() == "true"
        )
        try:
            thread = self._ports.read_thread_summary_authoritatively(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取恢复目标失败")
            return make_card_response(toast=f"恢复线程失败：{exc}", toast_type="warning")
        self._runtime_ports.submit_to_runtime(
            self._resume_target_on_runtime,
            sender_id,
            chat_id,
            thread.thread_id,
            original_arg=thread_id,
            summary=thread,
            pause_active_goal_on_resume=pause_active_goal_on_resume,
            message_id=message_id,
            refresh_threads_message_id=message_id if origin == "threads_card" else "",
        )
        if origin == "threads_card":
            return make_card_response(card=build_threads_pending_card(thread.thread_id, title=thread_title))
        return make_card_response(card=self._build_resume_pending_command_card(thread_title))

    def _resume_target_on_runtime(
        self,
        sender_id: str,
        chat_id: str,
        target: str,
        *,
        original_arg: str | None = None,
        summary: ThreadSummary | None = None,
        pause_active_goal_on_resume: bool = False,
        message_id: str = "",
        refresh_threads_message_id: str = "",
    ) -> None:
        try:
            thread = summary or self._ports.read_thread_summary_authoritatively(
                target,
                original_arg=original_arg or target,
            )
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            self._ports.reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            if refresh_threads_message_id:
                self.refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
            return
        self._runtime_ports.resume_thread_on_runtime(
            sender_id,
            chat_id,
            thread.thread_id,
            original_arg=original_arg or target,
            summary=thread,
            pause_active_goal_on_resume=pause_active_goal_on_resume,
            message_id=message_id,
            refresh_threads_message_id=refresh_threads_message_id,
        )

    @staticmethod
    def _resume_requires_active_goal_confirm(thread: ThreadSummary, goal: ThreadGoalSummary | None) -> bool:
        return (
            str(thread.status or "").strip() == BACKEND_THREAD_STATUS_NOT_LOADED
            and goal is not None
            and str(goal.status or "").strip() == "active"
        )

    @staticmethod
    def _build_resume_pending_command_card(target: str) -> dict:
        return build_markdown_card(
            "Codex 正在恢复线程",
            f"正在恢复：`{target}`\n完成后会自动回复结果。",
        )

    def handle_show_rename_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        try:
            thread = self._find_thread_row(sender_id, chat_id, thread_id, message_id=message_id)
        except Exception as exc:
            logger.exception("查询重命名目标失败")
            return make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not thread:
            return make_card_response(toast="未找到对应线程", toast_type="warning")
        self.register_pending_rename_form(message_id, thread_id=thread_id)
        return make_card_response(card=build_rename_card(thread))

    def handle_rename_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        form_value = action_value.get("_form_value") or {}
        if not message_id or not isinstance(form_value, dict) or "rename_title" not in form_value:
            return None

        pending = self.pending_rename_form_snapshot(message_id)
        if not pending:
            return make_card_response(
                toast="重命名表单已失效，请重新打开。",
                toast_type="warning",
            )
        if self._ports.is_group_chat(chat_id, message_id) and not self._ports.is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=str(action_value.get("_operator_open_id", "")).strip(),
        ):
            return make_card_response(
                toast="仅管理员可操作群共享会话或群设置。",
                toast_type="warning",
            )

        payload = dict(action_value)
        payload["action"] = "rename_thread"
        payload["thread_id"] = pending["thread_id"]
        return self.handle_rename_submit_action(sender_id, chat_id, message_id, payload)

    def handle_rename_submit_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        form_value = action_value.get("_form_value") or {}
        new_title = str(form_value.get("rename_title", "")).strip()
        if not new_title:
            return make_card_response(toast="标题不能为空", toast_type="warning")
        try:
            self._ports.rename_thread(thread_id, new_title)
        except Exception as exc:
            logger.exception("卡片重命名失败")
            return make_card_response(toast=f"重命名失败：{exc}", toast_type="warning")

        self._clear_pending_rename_form(message_id)
        self._ports.rename_bound_thread_title(
            sender_id,
            chat_id,
            new_title,
            message_id=message_id,
            thread_id=thread_id,
        )
        return self._handle_threads_refresh_action(sender_id, chat_id, message_id=message_id, toast="已重命名。")

    def handle_cancel_rename_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._clear_pending_rename_form(message_id)
        return self._handle_threads_refresh_action(sender_id, chat_id, message_id=message_id, toast="已取消")

    def handle_archive_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return make_card_response(
                toast="执行中不能归档线程，请等待结束或先执行 /cancel。",
                toast_type="warning",
            )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._ports.read_thread_summary_authoritatively(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取归档目标失败")
            return make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        try:
            result = self._ports.archive_thread_for_control(thread.thread_id, summary=thread)
        except Exception as exc:
            logger.exception("归档线程失败")
            return make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        upstream_outcome = str(result.get("upstream_outcome", "success") or "success")
        if upstream_outcome == "unknown":
            return make_card_response(toast="归档结果未知，请先核对 archived 列表。", toast_type="warning")
        if upstream_outcome != "success":
            return make_card_response(
                toast=f"归档线程失败：{result.get('upstream_error') or '上游返回错误'}",
                toast_type="warning",
            )
        if result.get("focus_cleanup") == "incomplete":
            return make_card_response(toast="线程已归档，但本地 binding 清理不完整。", toast_type="warning")
        return self._handle_threads_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast=f"已归档线程：{thread.thread_id[:8]}…",
        )

    def handle_show_more_threads_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._set_threads_card_expanded(message_id, expanded=True)
        try:
            card = self._render_threads_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("展开线程列表失败")
            return make_card_response(toast=f"展开失败：{exc}", toast_type="warning")
        return make_card_response(card=card, toast="已展开全部线程。", toast_type="success")

    def refresh_threads_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        try:
            card = self._render_threads_card(sender_id, chat_id, message_id=normalized_message_id)
        except Exception:
            logger.exception("刷新线程卡片失败")
            return
        self._ports.patch_message(normalized_message_id, json.dumps(card, ensure_ascii=False))

    def _clear_pending_rename_form(self, message_id: str) -> None:
        if not message_id:
            return
        with self._lock:
            self._pending_rename_forms.pop(message_id, None)

    def _handle_threads_refresh_action(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        toast: str,
    ) -> P2CardActionTriggerResponse:
        try:
            card = self._render_threads_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("刷新线程列表失败")
            return make_card_response(toast=f"刷新失败：{exc}", toast_type="warning")
        return make_card_response(card=card, toast=toast, toast_type="success")

    def _set_threads_card_expanded(self, message_id: str, *, expanded: bool) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        with self._lock:
            if expanded:
                self._expanded_threads_cards.add(normalized_message_id)
            else:
                self._expanded_threads_cards.discard(normalized_message_id)

    def _is_threads_card_expanded(self, message_id: str) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        with self._lock:
            return normalized_message_id in self._expanded_threads_cards

    def _render_threads_card(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> dict:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        rows, counts = self._build_thread_rows(sender_id, chat_id, threads, message_id=message_id)
        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        return build_threads_card(
            rows,
            runtime.current_thread_id,
            runtime.working_dir,
            counts["total_all"],
            shown_count=counts["shown"],
            expanded=self._is_threads_card_expanded(message_id),
        )

    def _build_thread_rows(
        self,
        sender_id: str,
        chat_id: str,
        threads: list[ThreadSummary],
        *,
        message_id: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows = [
            {
                "thread_id": thread.thread_id,
                "cwd": thread.cwd,
                "title": thread.title,
                "updated_at": thread.updated_at,
                "model_provider": thread.model_provider or "",
            }
            for thread in threads
        ]
        rows.sort(key=lambda item: item["updated_at"], reverse=True)

        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        current_id = runtime.current_thread_id
        if current_id and all(item["thread_id"] != current_id for item in rows):
            rows.insert(
                0,
                {
                    "thread_id": current_id,
                    "cwd": runtime.working_dir,
                    "title": runtime.current_thread_title or "（无标题）",
                    "updated_at": int(time.time()),
                    "model_provider": "",
                },
            )

        counts = {
            "total_all": len(rows),
            "shown": self._ports.threads_initial_limit,
        }
        return rows, counts

    def _find_thread_row(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> dict[str, Any] | None:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        rows, _ = self._build_thread_rows(sender_id, chat_id, threads, message_id=message_id)
        return next((item for item in rows if item["thread_id"] == thread_id), None)

    def _list_current_dir_threads(self, sender_id: str, chat_id: str, *, message_id: str = "") -> list[ThreadSummary]:
        return self._ports.list_visible_current_dir_threads(
            sender_id,
            chat_id,
            message_id=message_id,
        )
