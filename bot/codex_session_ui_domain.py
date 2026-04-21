"""
Codex session UI domain.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import ThreadSummary
from bot.cards import (
    CommandResult,
    build_markdown_card,
    build_rename_card,
    build_sessions_card,
    build_sessions_closed_card,
    build_sessions_pending_card,
    make_card_response,
)
from bot.runtime_view import RuntimeView
logger = logging.getLogger(__name__)


class _SessionUiDomainOwner(Protocol):
    bot: Any
    _adapter: Any
    _lock: threading.RLock
    _session_recent_limit: int
    _thread_list_query_limit: int

    def _get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView: ...
    def _is_group_chat(self, chat_id: str, message_id: str = "") -> bool: ...
    def _is_group_admin_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool: ...
    def _rename_bound_thread_title(
        self,
        sender_id: str,
        chat_id: str,
        title: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> bool: ...

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None: ...

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...
    def _runtime_submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None: ...

    def _resolve_resume_target(self, arg: str) -> ThreadSummary: ...

    def _list_visible_current_dir_threads(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> list[ThreadSummary]: ...

    def _read_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
    ) -> ThreadSummary: ...

    def _resume_thread_in_background(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str | None = None,
        summary: ThreadSummary | None = None,
        message_id: str = "",
        refresh_session_message_id: str = "",
    ) -> None: ...


class CodexSessionUiDomain:
    def __init__(self, owner: _SessionUiDomainOwner) -> None:
        self._owner = owner
        self._expanded_session_cards: set[str] = set()
        self._pending_rename_forms: dict[str, dict[str, str]] = {}

    def pending_rename_form_snapshot(self, message_id: str) -> dict[str, str] | None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None
        with self._owner._lock:
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
        with self._owner._lock:
            self._pending_rename_forms[normalized_message_id] = {"thread_id": normalized_thread_id}

    def handle_session_command(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> CommandResult:
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
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
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能切换线程，请等待结束或先执行 `/cancel`。")
        if not arg:
            return CommandResult(
                text="用法：`/resume <thread_id 或 thread_name>`\n发送 `/help session` 查看 `/session` 与 `/resume` 的区别。"
            )
        target = arg.strip()
        return CommandResult(
            card=build_markdown_card(
                "Codex 正在恢复线程",
                f"正在恢复：`{target}`\n完成后会自动回复结果。",
            ),
            after_dispatch=lambda: self._owner._runtime_submit(
                self._resume_target_in_runtime,
                sender_id,
                chat_id,
                target,
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
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        if not runtime.current_thread_id:
            return CommandResult(text="当前没有绑定线程，无法重命名。")
        if not arg:
            return CommandResult(text="用法：`/rename <新标题>`")
        try:
            self._owner._adapter.rename_thread(runtime.current_thread_id, arg)
        except Exception as exc:
            logger.exception("重命名线程失败")
            return CommandResult(text=f"重命名失败：{exc}")
        self._owner._rename_bound_thread_title(
            sender_id,
            chat_id,
            arg,
            message_id=message_id,
            thread_id=runtime.current_thread_id,
        )
        return CommandResult(text=f"已重命名为：{arg}")

    def handle_rm_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能归档线程，请等待结束或先执行 `/cancel`。")
        target = arg.strip() if arg else ""
        if target:
            try:
                thread = self._owner._resolve_resume_target(target)
            except Exception as exc:
                logger.exception("解析归档目标失败")
                return CommandResult(text=f"归档线程失败：{exc}")
        else:
            if not runtime.current_thread_id:
                return CommandResult(text="用法：`/rm [thread_id 或 thread_name]`；省略参数时归档当前线程。")
            try:
                thread = self._owner._read_thread_summary_authoritatively(
                    runtime.current_thread_id,
                    original_arg=runtime.current_thread_id,
                )
            except Exception as exc:
                logger.exception("读取当前线程失败")
                return CommandResult(text=f"归档线程失败：{exc}")

        try:
            self._owner._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            return CommandResult(text=f"归档线程失败：{exc}")

        if runtime.current_thread_id == thread.thread_id:
            self._owner._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        return CommandResult(text=(
            f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}\n"
            "说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。"
        ))

    def handle_close_sessions_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del action_value
        self._set_session_card_expanded(message_id, expanded=False)
        return make_card_response(
            card=build_sessions_closed_card(),
            toast="已收起。",
            toast_type="success",
        )

    def handle_reopen_sessions_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._set_session_card_expanded(message_id, expanded=True)
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已展开。")

    def handle_resume_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return make_card_response(
                toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                toast_type="warning",
            )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return make_card_response(toast="缺少 thread_id", toast_type="warning")
        thread_title = str(action_value.get("thread_title", "") or action_value.get("title", "")).strip() or thread_id
        self._owner._runtime_submit(
            self._resume_target_in_runtime,
            sender_id,
            chat_id,
            thread_id,
            message_id=message_id,
            refresh_session_message_id=message_id,
        )
        return make_card_response(
            card=build_sessions_pending_card(thread_id, title=thread_title),
            toast="正在恢复线程…",
            toast_type="success",
        )

    def _resume_target_in_runtime(
        self,
        sender_id: str,
        chat_id: str,
        target: str,
        *,
        message_id: str = "",
        refresh_session_message_id: str = "",
    ) -> None:
        try:
            thread = self._owner._resolve_resume_target(target)
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            self._owner._reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            if refresh_session_message_id:
                self.refresh_sessions_card_message(sender_id, chat_id, refresh_session_message_id)
            return
        self._owner._resume_thread_in_background(
            sender_id,
            chat_id,
            thread.thread_id,
            original_arg=target,
            summary=thread,
            message_id=message_id,
            refresh_session_message_id=refresh_session_message_id,
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
            session = self._find_thread_session(sender_id, chat_id, thread_id, message_id=message_id)
        except Exception as exc:
            logger.exception("查询重命名目标失败")
            return make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not session:
            return make_card_response(toast="未找到对应线程", toast_type="warning")
        self.register_pending_rename_form(message_id, thread_id=thread_id)
        return make_card_response(card=build_rename_card(session))

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
        if self._owner._is_group_chat(chat_id, message_id) and not self._owner._is_group_admin_actor(
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
            self._owner._adapter.rename_thread(thread_id, new_title)
        except Exception as exc:
            logger.exception("卡片重命名失败")
            return make_card_response(toast=f"重命名失败：{exc}", toast_type="warning")

        self._clear_pending_rename_form(message_id)
        self._owner._rename_bound_thread_title(
            sender_id,
            chat_id,
            new_title,
            message_id=message_id,
            thread_id=thread_id,
        )
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已重命名。")

    def handle_cancel_rename_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._clear_pending_rename_form(message_id)
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已取消")

    def handle_archive_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return make_card_response(
                toast="执行中不能归档线程，请等待结束或先执行 /cancel。",
                toast_type="warning",
            )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._owner._read_thread_summary_authoritatively(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取归档目标失败")
            return make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        try:
            self._owner._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            return make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        if runtime.current_thread_id == thread.thread_id:
            self._owner._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        return self._handle_sessions_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast=f"已归档线程：{thread.thread_id[:8]}…",
        )

    def handle_show_more_sessions_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del action_value
        self._set_session_card_expanded(message_id, expanded=True)
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("展开线程列表失败")
            return make_card_response(toast=f"展开失败：{exc}", toast_type="warning")
        return make_card_response(card=card, toast="已展开全部线程。", toast_type="success")

    def refresh_sessions_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=normalized_message_id)
        except Exception:
            logger.exception("刷新会话卡片失败")
            return
        self._owner.bot.patch_message(normalized_message_id, json.dumps(card, ensure_ascii=False))

    def _clear_pending_rename_form(self, message_id: str) -> None:
        if not message_id:
            return
        with self._owner._lock:
            self._pending_rename_forms.pop(message_id, None)

    def _handle_sessions_refresh_action(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        toast: str,
    ) -> P2CardActionTriggerResponse:
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("刷新线程列表失败")
            return make_card_response(toast=f"刷新失败：{exc}", toast_type="warning")
        return make_card_response(card=card, toast=toast, toast_type="success")

    def _set_session_card_expanded(self, message_id: str, *, expanded: bool) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        with self._owner._lock:
            if expanded:
                self._expanded_session_cards.add(normalized_message_id)
            else:
                self._expanded_session_cards.discard(normalized_message_id)

    def _is_session_card_expanded(self, message_id: str) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        with self._owner._lock:
            return normalized_message_id in self._expanded_session_cards

    def _render_sessions_card(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> dict:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        sessions, counts = self._build_session_rows(sender_id, chat_id, threads, message_id=message_id)
        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
        return build_sessions_card(
            sessions,
            runtime.current_thread_id,
            runtime.working_dir,
            counts["total_all"],
            shown_count=counts["shown"],
            expanded=self._is_session_card_expanded(message_id),
        )

    def _build_session_rows(
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

        runtime = self._owner._get_runtime_view(sender_id, chat_id, message_id)
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
            "shown": self._owner._session_recent_limit,
        }
        return rows, counts

    def _find_thread_session(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> dict[str, Any] | None:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        rows, _ = self._build_session_rows(sender_id, chat_id, threads, message_id=message_id)
        return next((item for item in rows if item["thread_id"] == thread_id), None)

    def _list_current_dir_threads(self, sender_id: str, chat_id: str, *, message_id: str = "") -> list[ThreadSummary]:
        return self._owner._list_visible_current_dir_threads(
            sender_id,
            chat_id,
            message_id=message_id,
        )
