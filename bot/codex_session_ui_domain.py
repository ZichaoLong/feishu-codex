"""
Codex session UI domain.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Protocol

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.base import ThreadSummary
from bot.cards import (
    build_rename_card,
    build_resume_guard_card,
    build_resume_guard_handled_card,
    build_sessions_card,
    build_sessions_closed_card,
    build_sessions_pending_card,
)
from bot.session_resolution import list_current_dir_threads

logger = logging.getLogger(__name__)


class _SessionUiDomainOwner(Protocol):
    bot: Any
    _adapter: Any
    _favorites: Any
    _lock: threading.RLock
    _pending_rename_forms: dict[str, dict[str, str]]
    _session_recent_limit: int
    _session_starred_limit: int
    _thread_list_query_limit: int

    def _get_state(self, sender_id: str, chat_id: str, message_id: str = "") -> Any: ...

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...

    def _reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None: ...

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None: ...

    def _resolve_resume_target(self, arg: str) -> ThreadSummary: ...

    def _read_thread_summary(self, thread_id: str, *, original_arg: str) -> ThreadSummary: ...

    def _find_thread_summary(self, thread_id: str) -> ThreadSummary | None: ...

    def _is_loaded_in_current_backend(self, thread: ThreadSummary) -> bool: ...

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

    def _send_thread_snapshot_in_background(self, chat_id: str, thread_id: str, *, message_id: str = "") -> None: ...


class CodexSessionUiDomain:
    def __init__(self, owner: _SessionUiDomainOwner) -> None:
        self._owner = owner

    def handle_session_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        del arg
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("获取线程列表失败")
            self._owner._reply_text(chat_id, f"获取线程列表失败：{exc}", message_id=message_id)
            return
        self._owner._reply_card(chat_id, card, message_id=message_id)

    def handle_resume_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            if state["running"]:
                self._owner._reply_text(
                    chat_id,
                    "执行中不能切换线程，请等待结束或先执行 `/cancel`。",
                    message_id=message_id,
                )
                return
        if not arg:
            self._owner._reply_text(
                chat_id,
                "用法：`/resume <thread_id 或 thread_name>`\n发送 `/help session` 查看 `/session` 与 `/resume` 的区别。",
                message_id=message_id,
            )
            return
        try:
            thread = self._owner._resolve_resume_target(arg)
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            self._owner._reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            return
        if self._owner._is_loaded_in_current_backend(thread):
            self._owner._resume_thread_in_background(
                sender_id,
                chat_id,
                thread.thread_id,
                original_arg=arg,
                summary=thread,
                message_id=message_id,
            )
            return
        self._owner._reply_card(chat_id, self._build_resume_guard(thread), message_id=message_id)

    def handle_rename_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        if not state["current_thread_id"]:
            self._owner._reply_text(chat_id, "当前没有绑定线程，无法重命名。", message_id=message_id)
            return
        if not arg:
            self._owner._reply_text(chat_id, "用法：`/rename <新标题>`", message_id=message_id)
            return
        try:
            self._owner._adapter.rename_thread(state["current_thread_id"], arg)
        except Exception as exc:
            logger.exception("重命名线程失败")
            self._owner._reply_text(chat_id, f"重命名失败：{exc}", message_id=message_id)
            return
        with self._owner._lock:
            state["current_thread_name"] = arg
        self._owner._reply_text(chat_id, f"已重命名为：{arg}", message_id=message_id)

    def handle_rm_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            if state["running"]:
                self._owner._reply_text(
                    chat_id,
                    "执行中不能归档线程，请等待结束或先执行 `/cancel`。",
                    message_id=message_id,
                )
                return
        target = arg.strip() if arg else ""
        if target:
            try:
                thread = self._owner._resolve_resume_target(target)
            except Exception as exc:
                logger.exception("解析归档目标失败")
                self._owner._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
                return
        else:
            if not state["current_thread_id"]:
                self._owner._reply_text(
                    chat_id,
                    "用法：`/rm [thread_id 或 thread_name]`；省略参数时归档当前线程。",
                    message_id=message_id,
                )
                return
            try:
                thread = self._owner._read_thread_summary(
                    state["current_thread_id"],
                    original_arg=state["current_thread_id"],
                )
            except Exception as exc:
                logger.exception("读取当前线程失败")
                self._owner._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
                return

        try:
            self._owner._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            self._owner._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
            return

        self._owner._favorites.remove_thread_globally(thread.thread_id)
        if state["current_thread_id"] == thread.thread_id:
            self._owner._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        self._owner._reply_text(
            chat_id,
            (
                f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}\n"
                "说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。"
            ),
            message_id=message_id,
        )

    def handle_star_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        del arg
        state = self._owner._get_state(sender_id, chat_id, message_id)
        if not state["current_thread_id"]:
            self._owner._reply_text(chat_id, "当前没有绑定线程，无法收藏。", message_id=message_id)
            return
        starred = self._owner._favorites.toggle(sender_id, state["current_thread_id"])
        self._owner._reply_text(chat_id, "已收藏当前线程。" if starred else "已取消收藏当前线程。", message_id=message_id)

    def handle_toggle_star_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        starred = self._owner._favorites.toggle(sender_id, thread_id)
        return self._handle_sessions_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast="已收藏线程。" if starred else "已取消收藏。",
        )

    def handle_close_sessions_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del chat_id
        del message_id
        del action_value
        return self._owner.bot.make_card_response(
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
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已展开。")

    def handle_resume_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            if state["running"]:
                return self._owner.bot.make_card_response(
                    toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._owner._read_thread_summary(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("查询恢复目标失败")
            return self._owner.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if self._owner._is_loaded_in_current_backend(thread):
            threading.Thread(
                target=self._owner._resume_thread_in_background,
                args=(sender_id, chat_id, thread_id),
                kwargs={
                    "original_arg": thread_id,
                    "summary": thread,
                    "message_id": message_id,
                    "refresh_session_message_id": message_id,
                },
                daemon=True,
            ).start()
            return self._owner.bot.make_card_response(
                card=build_sessions_pending_card(thread.thread_id, title=thread.title),
                toast="正在恢复线程…",
                toast_type="success",
            )
        return self._owner.bot.make_card_response(
            card=self._build_resume_guard(thread, return_to_sessions=True)
        )

    def handle_preview_thread_snapshot_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        return_to_sessions = bool(action_value.get("return_to_sessions"))
        thread = self._owner._find_thread_summary(thread_id)
        if thread is None:
            return self._owner.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        threading.Thread(
            target=self._owner._send_thread_snapshot_in_background,
            args=(chat_id, thread_id),
            kwargs={"message_id": message_id},
            daemon=True,
        ).start()
        if return_to_sessions:
            return self._handle_sessions_refresh_action(
                sender_id,
                chat_id,
                message_id=message_id,
                toast="正在加载快照…",
            )
        return self._owner.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已选择“查看快照”",
                detail="快照会作为新消息发送；当前确认卡已结束，不会继续写入该线程。",
                template="green",
            ),
            toast="正在加载快照…",
            toast_type="success",
        )

    def handle_resume_thread_write_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            if state["running"]:
                return self._owner.bot.make_card_response(
                    toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        return_to_sessions = bool(action_value.get("return_to_sessions"))
        thread = self._owner._find_thread_summary(thread_id)
        if thread is None:
            return self._owner.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        threading.Thread(
            target=self._owner._resume_thread_in_background,
            args=(sender_id, chat_id, thread_id),
            kwargs={
                "original_arg": thread_id,
                "message_id": message_id,
                "refresh_session_message_id": message_id if return_to_sessions else "",
            },
            daemon=True,
        ).start()
        if return_to_sessions:
            return self._owner.bot.make_card_response(
                card=build_sessions_pending_card(thread.thread_id, title=thread.title),
                toast="正在恢复线程并继续写入…",
                toast_type="success",
            )
        return self._owner.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已选择“恢复并继续写入”",
                detail="恢复请求已提交到 feishu-codex backend；后续结果会通过新的状态消息返回。",
                template="orange",
            ),
            toast="正在恢复线程并继续写入…",
            toast_type="success",
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
            return self._owner.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not session:
            return self._owner.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        with self._owner._lock:
            self._owner._pending_rename_forms[message_id] = {"thread_id": thread_id}
        return self._owner.bot.make_card_response(card=build_rename_card(session))

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
            return self._owner.bot.make_card_response(toast="标题不能为空", toast_type="warning")
        try:
            self._owner._adapter.rename_thread(thread_id, new_title)
        except Exception as exc:
            logger.exception("卡片重命名失败")
            return self._owner.bot.make_card_response(toast=f"重命名失败：{exc}", toast_type="warning")

        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            self._owner._pending_rename_forms.pop(message_id, None)
            if state["current_thread_id"] == thread_id:
                state["current_thread_name"] = new_title
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

    def handle_cancel_resume_guard_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        if action_value.get("return_to_sessions"):
            return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已取消")
        thread = self._owner._find_thread_summary(thread_id)
        if thread is None:
            return self._owner.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        return self._owner.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已取消本次恢复",
                detail="当前不会查看快照，也不会在 feishu-codex backend 中恢复该线程。",
                template="grey",
            ),
            toast="已取消。",
            toast_type="success",
        )

    def handle_archive_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        state = self._owner._get_state(sender_id, chat_id, message_id)
        with self._owner._lock:
            if state["running"]:
                return self._owner.bot.make_card_response(
                    toast="执行中不能归档线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self._owner.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._owner._read_thread_summary(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取归档目标失败")
            return self._owner.bot.make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        try:
            self._owner._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            return self._owner.bot.make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        self._owner._favorites.remove_thread_globally(thread.thread_id)
        if state["current_thread_id"] == thread.thread_id:
            self._owner._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        return self._handle_sessions_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast=f"已归档线程：{thread.thread_id[:8]}…",
        )

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
            self._owner._pending_rename_forms.pop(message_id, None)

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
            return self._owner.bot.make_card_response(toast=f"刷新失败：{exc}", toast_type="warning")
        return self._owner.bot.make_card_response(card=card, toast=toast, toast_type="success")

    def _render_sessions_card(self, sender_id: str, chat_id: str, *, message_id: str = "") -> dict:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        sessions, counts = self._build_session_rows(sender_id, chat_id, threads, message_id=message_id)
        state = self._owner._get_state(sender_id, chat_id, message_id)
        return build_sessions_card(
            sessions,
            state["current_thread_id"],
            state["working_dir"],
            counts["total_all"],
            shown_starred_count=counts["shown_starred"],
            total_starred_count=counts["total_starred"],
            shown_unstarred_count=counts["shown_unstarred"],
            total_unstarred_count=counts["total_unstarred"],
        )

    def _build_resume_guard(self, thread: ThreadSummary, *, return_to_sessions: bool = False) -> dict:
        return build_resume_guard_card(
            thread.thread_id,
            title=thread.title,
            cwd=thread.cwd,
            updated_at=thread.updated_at,
            source=thread.source,
            service_name=thread.service_name,
            return_to_sessions=return_to_sessions,
        )

    def _build_resume_guard_handled(
        self,
        thread: ThreadSummary,
        *,
        decision: str,
        detail: str,
        template: str,
    ) -> dict:
        return build_resume_guard_handled_card(
            thread.thread_id,
            title=thread.title,
            cwd=thread.cwd,
            updated_at=thread.updated_at,
            source=thread.source,
            service_name=thread.service_name,
            decision=decision,
            detail=detail,
            template=template,
        )

    def _build_session_rows(
        self,
        sender_id: str,
        chat_id: str,
        threads: list[ThreadSummary],
        *,
        message_id: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        starred_ids = self._owner._favorites.load_favorites(sender_id)
        rows = [
            {
                "thread_id": thread.thread_id,
                "cwd": thread.cwd,
                "title": thread.title,
                "updated_at": thread.updated_at,
                "model_provider": thread.model_provider or "",
                "starred": thread.thread_id in starred_ids,
            }
            for thread in threads
        ]
        rows.sort(key=lambda item: item["updated_at"], reverse=True)

        state = self._owner._get_state(sender_id, chat_id, message_id)
        current_id = state["current_thread_id"]
        if current_id and all(item["thread_id"] != current_id for item in rows):
            rows.insert(
                0,
                {
                    "thread_id": current_id,
                    "cwd": state["working_dir"],
                    "title": state["current_thread_name"] or "（当前未持久化线程）",
                    "updated_at": int(time.time()),
                    "model_provider": "",
                    "starred": current_id in starred_ids,
                },
            )

        starred = [item for item in rows if item["starred"]]
        unstarred = [item for item in rows if not item["starred"]]

        display = starred[: self._owner._session_starred_limit] + unstarred[: self._owner._session_recent_limit]
        if current_id:
            current = next((item for item in rows if item["thread_id"] == current_id), None)
            if current and all(item["thread_id"] != current_id for item in display):
                display.insert(0, current)

        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in display:
            if item["thread_id"] in seen:
                continue
            deduped.append(item)
            seen.add(item["thread_id"])

        counts = {
            "shown_starred": sum(1 for item in deduped if item["starred"]),
            "total_starred": len(starred),
            "shown_unstarred": sum(1 for item in deduped if not item["starred"]),
            "total_unstarred": len(unstarred),
            "total_all": len(rows),
        }
        return deduped, counts

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
        return list_current_dir_threads(
            self._owner._adapter,
            cwd=self._owner._get_state(sender_id, chat_id, message_id)["working_dir"],
            limit=self._owner._thread_list_query_limit,
        )
