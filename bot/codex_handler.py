"""
Codex 飞书处理器。
"""

from __future__ import annotations

import atexit
import json
import logging
import pathlib
import threading
import time
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.cards import (
    build_approval_handled_card,
    build_approval_policy_card,
    build_ask_user_answered_card,
    build_ask_user_card,
    build_collaboration_mode_card,
    build_command_approval_card,
    build_execution_card,
    build_file_change_approval_card,
    build_history_preview_card,
    build_plan_card,
    build_permissions_approval_card,
    build_rename_card,
    build_sessions_card,
)
from bot.config import load_config_file
from bot.constants import (
    DEFAULT_HISTORY_PREVIEW_ROUNDS,
    DEFAULT_SESSION_RECENT_LIMIT,
    DEFAULT_SESSION_STARRED_LIMIT,
    DEFAULT_STREAM_PATCH_INTERVAL_MS,
    DEFAULT_THREAD_LIST_QUERY_LIMIT,
    FC_DATA_DIR,
    KEYWORD,
    display_path,
    resolve_working_dir,
)
from bot.handler import BotHandler
from bot.stores.favorites_store import FavoritesStore

logger = logging.getLogger(__name__)

_CARD_REPLY_LIMIT_DEFAULT = 12000
_CARD_LOG_LIMIT_DEFAULT = 8000


class CodexHandler(BotHandler):
    """处理 Feishu -> Codex 的命令与事件。"""

    def __init__(self, data_dir: pathlib.Path | None = None, config_dir: pathlib.Path | None = None):
        super().__init__()
        cfg = load_config_file("codex")

        self._data_dir = data_dir or FC_DATA_DIR
        self._config_dir = config_dir
        self._lock = threading.RLock()
        self._states: dict[tuple[str, str], dict[str, Any]] = {}
        self._thread_bindings: dict[str, tuple[str, str]] = {}
        self._pending_requests: dict[str, dict[str, Any]] = {}

        self._default_working_dir = resolve_working_dir(
            str(cfg.get("default_working_dir", "")),
        )
        self._session_recent_limit = int(cfg.get("session_recent_limit", DEFAULT_SESSION_RECENT_LIMIT))
        self._session_starred_limit = int(cfg.get("session_starred_limit", DEFAULT_SESSION_STARRED_LIMIT))
        self._thread_list_query_limit = int(cfg.get("thread_list_query_limit", DEFAULT_THREAD_LIST_QUERY_LIMIT))
        self._history_preview_rounds = int(cfg.get("history_preview_rounds", DEFAULT_HISTORY_PREVIEW_ROUNDS))
        self._stream_patch_interval_ms = int(
            cfg.get("stream_patch_interval_ms", DEFAULT_STREAM_PATCH_INTERVAL_MS)
        )
        self._show_history_preview_on_resume = bool(cfg.get("show_history_preview_on_resume", True))
        self._card_reply_limit = int(cfg.get("card_reply_limit", _CARD_REPLY_LIMIT_DEFAULT))
        self._card_log_limit = int(cfg.get("card_log_limit", _CARD_LOG_LIMIT_DEFAULT))

        self._adapter_config = CodexAppServerConfig.from_dict(cfg)
        self._favorites = FavoritesStore(self._data_dir)
        self._adapter = CodexAppServerAdapter(
            self._adapter_config,
            on_notification=self._handle_adapter_notification,
            on_request=self._handle_adapter_request,
        )
        atexit.register(self.shutdown)

    @property
    def name(self) -> str:
        return "Codex"

    @property
    def keyword(self) -> str:
        return KEYWORD

    @property
    def description(self) -> str:
        return "通过飞书与 Codex 交互"

    def handle_message(self, user_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        state = self._get_state(user_id, chat_id)
        cleaned = (text or "").strip()
        with self._lock:
            if not state["active"]:
                state["active"] = True

        if not cleaned or cleaned.upper() == KEYWORD:
            self._reply_help(chat_id)
            return

        if cleaned.startswith("/"):
            self._handle_command(user_id, chat_id, cleaned, message_id=message_id)
            return

        self._handle_prompt(user_id, chat_id, cleaned, message_id=message_id)

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        action = action_value.get("action", "")
        if not action:
            fallback = self._handle_user_input_form_fallback(user_id, chat_id, message_id, action_value)
            if fallback is not None:
                return fallback
            form_value = action_value.get("_form_value") or {}
            if isinstance(form_value, dict) and form_value:
                return self.bot.make_card_response(
                    toast="表单已失效或未找到对应问题，请重新触发该请求。",
                    toast_type="warning",
                )
        if action == "cancel_turn":
            return self._handle_cancel_action(user_id, chat_id)
        if action == "resume_thread":
            thread_id = str(action_value.get("thread_id", ""))
            threading.Thread(
                target=self._resume_thread_in_background,
                args=(user_id, chat_id, thread_id),
                daemon=True,
            ).start()
            return self.bot.make_card_response(toast="正在恢复线程…")
        if action == "toggle_star_thread":
            return self._handle_toggle_star_action(user_id, chat_id, action_value)
        if action == "show_rename_form":
            return self._handle_show_rename_action(user_id, chat_id, action_value)
        if action == "rename_thread":
            return self._handle_rename_submit_action(user_id, chat_id, action_value)
        if action == "cancel_rename":
            return self._handle_sessions_refresh_action(user_id, chat_id, toast="已取消")
        if action == "set_approval_policy":
            return self._handle_set_approval_policy(user_id, chat_id, action_value)
        if action == "set_collaboration_mode":
            return self._handle_set_collaboration_mode(user_id, chat_id, action_value)
        if action.startswith("command_") or action.startswith("file_change_") or action.startswith("permissions_"):
            return self._handle_approval_card_action(action_value)
        if action.startswith("answer_user_input_"):
            return self._handle_user_input_action(action_value)
        return P2CardActionTriggerResponse()

    def _handle_user_input_form_fallback(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse | None:
        form_value = action_value.get("_form_value") or {}
        if not message_id or not isinstance(form_value, dict) or not form_value:
            return None

        pending_request: tuple[str, dict[str, Any]] | None = None
        with self._lock:
            for request_key, pending in self._pending_requests.items():
                if pending.get("method") != "item/tool/requestUserInput":
                    continue
                if pending.get("message_id") != message_id:
                    continue
                pending_request = (request_key, pending)
                break
        if not pending_request:
            return None

        request_key, pending = pending_request
        matched_question_id = ""
        for question in pending.get("questions") or []:
            qid = str(question.get("id", "")).strip()
            if not qid:
                continue
            options = question.get("options") or []
            allow_custom = bool(question.get("isOther", False)) or not options
            field_name = f"user_input_{qid}"
            if allow_custom and str(form_value.get(field_name, "")).strip():
                matched_question_id = qid
                break
        if not matched_question_id:
            return None

        payload = dict(action_value)
        payload["action"] = "answer_user_input_custom"
        payload["request_id"] = request_key
        payload["question_id"] = matched_question_id
        return self._handle_user_input_action(payload)

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        return self._get_state(user_id, chat_id).get("active", False)

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        with self._lock:
            state = self._states.pop((user_id, chat_id), None)
            if not state:
                return
            thread_id = state.get("current_thread_id", "")
            if thread_id and self._thread_bindings.get(thread_id) == (user_id, chat_id):
                self._thread_bindings.pop(thread_id, None)

    def shutdown(self) -> None:
        """停止底层 app-server。"""
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("停止 Codex adapter 失败")

    def _get_state(self, user_id: str, chat_id: str) -> dict[str, Any]:
        key = (user_id, chat_id)
        with self._lock:
            if key not in self._states:
                self._states[key] = {
                    "active": False,
                    "working_dir": self._default_working_dir,
                    "current_thread_id": "",
                    "current_thread_name": "",
                    "current_turn_id": "",
                    "running": False,
                    "cancelled": False,
                    "current_message_id": "",
                    "full_reply_text": "",
                    "full_log_text": "",
                    "started_at": 0.0,
                    "last_patch_at": 0.0,
                    "patch_timer": None,
                    "followup_sent": False,
                    "approval_policy": self._adapter_config.approval_policy,
                    "collaboration_mode": self._adapter_config.collaboration_mode,
                    "model": self._adapter_config.model,
                    "reasoning_effort": self._adapter_config.reasoning_effort,
                    "plan_message_id": "",
                    "plan_turn_id": "",
                    "plan_explanation": "",
                    "plan_steps": [],
                    "plan_text": "",
                }
            return self._states[key]

    def _handle_command(self, user_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        command, _, arg = text.partition(" ")
        arg = arg.strip()
        cmd = command.lower()

        if cmd in ("/help", "/h"):
            self._reply_help(chat_id)
            return
        if cmd == "/pwd":
            self.bot.reply(chat_id, f"当前目录：`{display_path(self._get_state(user_id, chat_id)['working_dir'])}`")
            return
        if cmd == "/cd":
            self._handle_cd_command(user_id, chat_id, arg)
            return
        if cmd == "/new":
            self._handle_new_command(user_id, chat_id)
            return
        if cmd == "/status":
            self._handle_status_command(user_id, chat_id)
            return
        if cmd == "/cancel":
            self._cancel_current_turn(user_id, chat_id)
            return
        if cmd == "/session":
            self._handle_session_command(user_id, chat_id)
            return
        if cmd == "/resume":
            self._handle_resume_command(user_id, chat_id, arg)
            return
        if cmd == "/rename":
            self._handle_rename_command(user_id, chat_id, arg)
            return
        if cmd == "/star":
            self._handle_star_command(user_id, chat_id)
            return
        if cmd == "/approval":
            self._handle_approval_command(user_id, chat_id, arg)
            return
        if cmd == "/mode":
            self._handle_mode_command(user_id, chat_id, arg)
            return

        self.bot.reply(chat_id, f"未知命令：`{command}`\n发送 `/help` 查看可用命令。")

    def _handle_prompt(self, user_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "当前线程仍在执行，请等待结束或先执行 `/cancel`。")
                return

        try:
            thread_id = self._ensure_thread(user_id, chat_id)
        except Exception as exc:
            logger.exception("创建线程失败")
            self.bot.reply(chat_id, f"创建线程失败：{exc}")
            return

        with self._lock:
            state["running"] = True
            state["cancelled"] = False
            state["current_turn_id"] = ""
            state["full_reply_text"] = ""
            state["full_log_text"] = ""
            state["started_at"] = time.monotonic()
            state["followup_sent"] = False
            state["last_patch_at"] = 0.0
            self._clear_plan_state(state)

        card_id = self._send_execution_card(chat_id, message_id)
        with self._lock:
            state["current_message_id"] = card_id or ""

        try:
            self._adapter.start_turn(
                thread_id=thread_id,
                text=text,
                cwd=state["working_dir"],
                model=state["model"] or None,
                approval_policy=state["approval_policy"] or None,
                reasoning_effort=state["reasoning_effort"] or None,
                collaboration_mode=state["collaboration_mode"] or None,
            )
        except Exception as exc:
            logger.exception("启动 turn 失败")
            with self._lock:
                state["running"] = False
                state["full_reply_text"] = f"启动失败：{exc}"
            self._flush_execution_card(user_id, chat_id, immediate=True)
            if not card_id:
                self.bot.reply(chat_id, f"启动失败：{exc}")

    def _handle_cd_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "执行中不能切换目录，请等待结束或先执行 `/cancel`。")
                return

        if not arg:
            self.bot.reply(chat_id, f"当前目录：`{display_path(state['working_dir'])}`")
            return

        target = resolve_working_dir(arg, fallback=state["working_dir"])
        if not pathlib.Path(target).exists():
            self.bot.reply(chat_id, f"目录不存在：`{display_path(target)}`")
            return
        if not pathlib.Path(target).is_dir():
            self.bot.reply(chat_id, f"不是目录：`{display_path(target)}`")
            return

        self._clear_thread_binding(user_id, chat_id)
        with self._lock:
            state["working_dir"] = target
        self.bot.reply(chat_id, f"已切换目录到 `{display_path(target)}`，当前线程绑定已清空。")

    def _handle_new_command(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "执行中不能新建线程，请等待结束或先执行 `/cancel`。")
                return
        try:
            snapshot = self._adapter.create_thread(cwd=state["working_dir"])
        except Exception as exc:
            logger.exception("新建线程失败")
            self.bot.reply(chat_id, f"新建线程失败：{exc}")
            return
        self._bind_thread(user_id, chat_id, snapshot.summary)
        self.bot.reply(
            chat_id,
            (
                f"已新建线程：`{snapshot.summary.thread_id[:8]}…`\n"
                f"目录：`{display_path(snapshot.summary.cwd)}`"
            ),
        )

    def _handle_status_command(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        thread_id = state["current_thread_id"]
        title = state["current_thread_name"] or "（未绑定线程）"
        running = "是" if state["running"] else "否"
        turn_id = state["current_turn_id"][:8] + "…" if state["current_turn_id"] else "-"
        header = (
            f"目录：`{display_path(state['working_dir'])}`\n当前线程：`{thread_id[:8]}…` {title}"
            if thread_id
            else f"目录：`{display_path(state['working_dir'])}`\n当前线程：-"
        )
        self.bot.reply(
            chat_id,
            (
                f"{header}\n执行中：{running}\n当前 turn：{turn_id}\n"
                f"审批策略：`{state['approval_policy']}`\n"
                f"协作模式：`{state['collaboration_mode']}`"
            ),
        )

    def _handle_session_command(self, user_id: str, chat_id: str) -> None:
        try:
            threads = self._adapter.list_threads_all(
                cwd=self._get_state(user_id, chat_id)["working_dir"],
                limit=self._thread_list_query_limit,
                sort_key="updated_at",
            )
        except Exception as exc:
            logger.exception("获取线程列表失败")
            self.bot.reply(chat_id, f"获取线程列表失败：{exc}")
            return

        sessions, counts = self._build_session_rows(user_id, chat_id, threads)
        card = build_sessions_card(
            sessions,
            self._get_state(user_id, chat_id)["current_thread_id"],
            self._get_state(user_id, chat_id)["working_dir"],
            counts["total_all"],
            shown_starred_count=counts["shown_starred"],
            total_starred_count=counts["total_starred"],
            shown_unstarred_count=counts["shown_unstarred"],
            total_unstarred_count=counts["total_unstarred"],
        )
        self.bot.reply_card(chat_id, card)

    def _handle_resume_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "执行中不能切换线程，请等待结束或先执行 `/cancel`。")
                return
        if not arg:
            self.bot.reply(chat_id, "用法：`/resume <thread_id 或 thread_name>`")
            return
        self._resume_thread_in_background(user_id, chat_id, arg)

    def _handle_rename_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        if not state["current_thread_id"]:
            self.bot.reply(chat_id, "当前没有绑定线程，无法重命名。")
            return
        if not arg:
            self.bot.reply(chat_id, "用法：`/rename <新标题>`")
            return
        try:
            self._adapter.rename_thread(state["current_thread_id"], arg)
        except Exception as exc:
            logger.exception("重命名线程失败")
            self.bot.reply(chat_id, f"重命名失败：{exc}")
            return
        with self._lock:
            state["current_thread_name"] = arg
        self.bot.reply(chat_id, f"已重命名为：{arg}")

    def _handle_star_command(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        if not state["current_thread_id"]:
            self.bot.reply(chat_id, "当前没有绑定线程，无法收藏。")
            return
        starred = self._favorites.toggle(user_id, state["current_thread_id"])
        self.bot.reply(chat_id, "已收藏当前线程。" if starred else "已取消收藏当前线程。")

    def _handle_approval_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        if arg:
            if arg not in {"untrusted", "on-failure", "on-request", "never"}:
                self.bot.reply(chat_id, "审批策略仅支持：`untrusted`、`on-failure`、`on-request`、`never`")
                return
            with self._lock:
                state["approval_policy"] = arg
            self.bot.reply(chat_id, f"审批策略已切换为：`{arg}`")
            return
        self.bot.reply_card(chat_id, build_approval_policy_card(state["approval_policy"]))

    def _handle_mode_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        if arg:
            mode = arg.strip().lower()
            if mode not in {"default", "plan"}:
                self.bot.reply(chat_id, "协作模式仅支持：`default`、`plan`")
                return
            with self._lock:
                state["collaboration_mode"] = mode
                running = state["running"]
            if running:
                self.bot.reply(chat_id, f"协作模式已切换为：`{mode}`，当前执行结束后的下一轮生效。")
            else:
                self.bot.reply(chat_id, f"协作模式已切换为：`{mode}`")
            return
        self.bot.reply_card(
            chat_id,
            build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            ),
        )

    def _handle_cancel_action(self, user_id: str, chat_id: str) -> P2CardActionTriggerResponse:
        ok, message = self._cancel_current_turn(user_id, chat_id, from_card=True)
        return self.bot.make_card_response(toast=message, toast_type="success" if ok else "warning")

    def _cancel_current_turn(self, user_id: str, chat_id: str, *, from_card: bool = False) -> tuple[bool, str]:
        state = self._get_state(user_id, chat_id)
        thread_id = state["current_thread_id"]
        turn_id = state["current_turn_id"]
        if not state["running"] or not thread_id or not turn_id:
            if not from_card:
                self.bot.reply(chat_id, "当前没有正在执行的 turn。")
            return False, "当前没有正在执行的 turn。"
        try:
            self._adapter.interrupt_turn(thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            logger.exception("取消 turn 失败")
            if not from_card:
                self.bot.reply(chat_id, f"取消失败：{exc}")
            return False, f"取消失败：{exc}"
        with self._lock:
            state["cancelled"] = True
        if not from_card:
            self.bot.reply(chat_id, "已请求停止当前执行。")
        return True, "已请求停止当前执行。"

    def _handle_toggle_star_action(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        starred = self._favorites.toggle(user_id, thread_id)
        return self._handle_sessions_refresh_action(
            user_id,
            chat_id,
            toast="已收藏线程。" if starred else "已取消收藏。",
        )

    def _handle_show_rename_action(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        try:
            session = self._find_thread_session(user_id, chat_id, thread_id)
        except Exception as exc:
            logger.exception("查询重命名目标失败")
            return self.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not session:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        return self.bot.make_card_response(card=build_rename_card(session))

    def _handle_rename_submit_action(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        form_value = action_value.get("_form_value") or {}
        new_title = str(form_value.get("rename_title", "")).strip()
        if not new_title:
            return self.bot.make_card_response(toast="标题不能为空", toast_type="warning")
        try:
            self._adapter.rename_thread(thread_id, new_title)
        except Exception as exc:
            logger.exception("卡片重命名失败")
            return self.bot.make_card_response(toast=f"重命名失败：{exc}", toast_type="warning")

        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["current_thread_id"] == thread_id:
                state["current_thread_name"] = new_title
        return self._handle_sessions_refresh_action(user_id, chat_id, toast="已重命名。")

    def _handle_sessions_refresh_action(
        self, user_id: str, chat_id: str, *, toast: str
    ) -> P2CardActionTriggerResponse:
        try:
            threads = self._adapter.list_threads_all(
                cwd=self._get_state(user_id, chat_id)["working_dir"],
                limit=self._thread_list_query_limit,
                sort_key="updated_at",
            )
        except Exception as exc:
            logger.exception("刷新线程列表失败")
            return self.bot.make_card_response(toast=f"刷新失败：{exc}", toast_type="warning")
        sessions, counts = self._build_session_rows(user_id, chat_id, threads)
        card = build_sessions_card(
            sessions,
            self._get_state(user_id, chat_id)["current_thread_id"],
            self._get_state(user_id, chat_id)["working_dir"],
            counts["total_all"],
            shown_starred_count=counts["shown_starred"],
            total_starred_count=counts["total_starred"],
            shown_unstarred_count=counts["shown_unstarred"],
            total_unstarred_count=counts["total_unstarred"],
        )
        return self.bot.make_card_response(card=card, toast=toast, toast_type="success")

    def _handle_set_approval_policy(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", ""))
        if policy not in {"untrusted", "on-failure", "on-request", "never"}:
            return self.bot.make_card_response(toast="非法审批策略", toast_type="warning")
        state = self._get_state(user_id, chat_id)
        with self._lock:
            state["approval_policy"] = policy
        return self.bot.make_card_response(
            card=build_approval_policy_card(policy),
            toast=f"审批策略已切换为 {policy}",
            toast_type="success",
        )

    def _handle_set_collaboration_mode(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        mode = str(action_value.get("mode", "")).strip().lower()
        if mode not in {"default", "plan"}:
            return self.bot.make_card_response(toast="非法协作模式", toast_type="warning")
        state = self._get_state(user_id, chat_id)
        with self._lock:
            state["collaboration_mode"] = mode
            running = state["running"]
        toast = f"协作模式已切换为 {mode}"
        if running:
            toast += "，当前执行结束后的下一轮生效"
        return self.bot.make_card_response(
            card=build_collaboration_mode_card(mode, running=running),
            toast=toast,
            toast_type="success",
        )

    def _handle_approval_card_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", ""))
        with self._lock:
            pending = self._pending_requests.get(request_key)
        if not pending:
            return self.bot.make_card_response(toast="该审批请求已失效或已处理。", toast_type="warning")

        action = action_value.get("action", "")
        title = pending["title"]
        rpc_request_id = pending["rpc_request_id"]

        if action == "command_allow_once":
            result = {"decision": "accept"}
            decision_text = "允许本次"
        elif action == "command_allow_session":
            result = {"decision": "acceptForSession"}
            decision_text = "允许本会话"
        elif action == "command_deny":
            result = {"decision": "decline"}
            decision_text = "拒绝"
        elif action == "command_abort":
            result = {"decision": "cancel"}
            decision_text = "中止本轮"
        elif action == "file_change_accept":
            result = {"decision": "accept"}
            decision_text = "允许本次"
        elif action == "file_change_accept_session":
            result = {"decision": "acceptForSession"}
            decision_text = "允许本会话"
        elif action == "file_change_decline":
            result = {"decision": "decline"}
            decision_text = "拒绝"
        elif action == "file_change_cancel":
            result = {"decision": "cancel"}
            decision_text = "中止本轮"
        elif action == "permissions_allow_once":
            result = {"permissions": pending["params"].get("permissions") or {}, "scope": "turn"}
            decision_text = "允许本次"
        elif action == "permissions_allow_session":
            result = {"permissions": pending["params"].get("permissions") or {}, "scope": "session"}
            decision_text = "允许本会话"
        elif action == "permissions_deny":
            result = {"permissions": {}, "scope": "turn"}
            decision_text = "拒绝"
        else:
            return self.bot.make_card_response(toast="未知审批动作", toast_type="warning")

        logger.info(
            "响应审批请求: request_key=%s, rpc_request_id=%s, action=%s, result=%s",
            request_key,
            rpc_request_id,
            action,
            result,
        )
        self._adapter.respond(rpc_request_id, result=result)
        with self._lock:
            self._pending_requests.pop(request_key, None)
        return self.bot.make_card_response(
            card=build_approval_handled_card(title, decision_text),
            toast=f"已{decision_text}",
            toast_type="success",
        )

    def _handle_user_input_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", ""))
        with self._lock:
            pending = self._pending_requests.get(request_key)
        if not pending:
            return self.bot.make_card_response(toast="该输入请求已失效或已处理。", toast_type="warning")

        question_id = str(action_value.get("question_id", ""))
        if not question_id:
            return self.bot.make_card_response(toast="缺少 question_id", toast_type="warning")

        target_question = next((item for item in pending["questions"] if item.get("id", "") == question_id), None)
        if not target_question:
            return self.bot.make_card_response(toast="未找到对应问题", toast_type="warning")

        if action_value.get("action") == "answer_user_input_option":
            answer = str(action_value.get("answer", "")).strip()
        else:
            options = target_question.get("options") or []
            allow_custom = bool(target_question.get("isOther", False)) or not options
            if not allow_custom:
                return self.bot.make_card_response(toast="该问题仅支持选择预设选项", toast_type="warning")
            form_value = action_value.get("_form_value") or {}
            answer = str(form_value.get(f"user_input_{question_id}", "")).strip()
        if not answer:
            return self.bot.make_card_response(toast="回答不能为空", toast_type="warning")

        pending["answers"][question_id] = answer
        questions = pending["questions"]
        if len(pending["answers"]) < len(questions):
            return self.bot.make_card_response(
                card=build_ask_user_card(request_key, questions, pending["answers"]),
                toast="已记录，继续回答下一题。",
                toast_type="success",
            )

        result = {
            "answers": {
                q.get("id", ""): {"answers": [pending["answers"][q.get("id", "")]]}
                for q in questions
            }
        }
        self._adapter.respond(pending["rpc_request_id"], result=result)
        with self._lock:
            self._pending_requests.pop(request_key, None)
        return self.bot.make_card_response(
            card=build_ask_user_answered_card(questions, pending["answers"]),
            toast="已提交回答。",
            toast_type="success",
        )

    def _ensure_thread(self, user_id: str, chat_id: str) -> str:
        state = self._get_state(user_id, chat_id)
        if state["current_thread_id"]:
            return state["current_thread_id"]
        snapshot = self._adapter.create_thread(cwd=state["working_dir"])
        self._bind_thread(user_id, chat_id, snapshot.summary)
        return snapshot.summary.thread_id

    def _resume_thread_in_background(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        try:
            snapshot = self._resume_snapshot(arg)
        except Exception as exc:
            logger.exception("恢复线程失败")
            self.bot.reply(chat_id, f"恢复线程失败：{exc}")
            return
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "当前线程仍在执行，暂不切换。")
                return
        self._bind_thread(user_id, chat_id, snapshot.summary)
        self.bot.reply(
            chat_id,
            (
                f"已恢复线程：`{snapshot.summary.thread_id[:8]}…`\n"
                f"标题：{snapshot.summary.title}\n"
                f"目录：`{display_path(snapshot.summary.cwd)}`"
            ),
        )
        if self._show_history_preview_on_resume:
            rounds = self._extract_history_rounds(snapshot)
            if rounds:
                self.bot.reply_card(chat_id, build_history_preview_card(snapshot.summary.thread_id, rounds))

    def _resume_snapshot(self, arg: str) -> ThreadSnapshot:
        try:
            return self._adapter.resume_thread(arg)
        except Exception:
            threads = self._adapter.list_threads_all(
                limit=self._thread_list_query_limit,
                sort_key="updated_at",
            )
            exact_name = [thread for thread in threads if thread.name == arg]
            if not exact_name:
                raise ValueError(f"未找到匹配的线程：`{arg}`")
            if len(exact_name) > 1:
                ids = ", ".join(item.thread_id[:8] + "…" for item in exact_name[:5])
                raise ValueError(f"匹配到多个同名线程：{ids}")
            return self._adapter.resume_thread(exact_name[0].thread_id)

    def _bind_thread(self, user_id: str, chat_id: str, thread: ThreadSummary) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            old_thread_id = state["current_thread_id"]
            if old_thread_id and self._thread_bindings.get(old_thread_id) == (user_id, chat_id):
                self._thread_bindings.pop(old_thread_id, None)
            state["current_thread_id"] = thread.thread_id
            state["current_thread_name"] = thread.name or thread.preview
            state["working_dir"] = thread.cwd or state["working_dir"]
            state["current_turn_id"] = ""
            self._clear_plan_state(state)
            self._thread_bindings[thread.thread_id] = (user_id, chat_id)

    def _clear_thread_binding(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            thread_id = state["current_thread_id"]
            if thread_id and self._thread_bindings.get(thread_id) == (user_id, chat_id):
                self._thread_bindings.pop(thread_id, None)
            state["current_thread_id"] = ""
            state["current_thread_name"] = ""
            state["current_turn_id"] = ""
            self._clear_plan_state(state)

    def _build_session_rows(
        self,
        user_id: str,
        chat_id: str,
        threads: list[ThreadSummary],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        starred_ids = self._favorites.load_user_favorites(user_id)
        rows = [
            {
                "thread_id": thread.thread_id,
                "cwd": thread.cwd,
                "title": thread.title,
                "updated_at": thread.updated_at,
                "starred": thread.thread_id in starred_ids,
            }
            for thread in threads
        ]
        rows.sort(key=lambda item: item["updated_at"], reverse=True)

        state = self._get_state(user_id, chat_id)
        current_id = state["current_thread_id"]
        if current_id and all(item["thread_id"] != current_id for item in rows):
            rows.insert(
                0,
                {
                    "thread_id": current_id,
                    "cwd": state["working_dir"],
                    "title": state["current_thread_name"] or "（当前未持久化线程）",
                    "updated_at": int(time.time()),
                    "starred": current_id in starred_ids,
                },
            )

        starred = [item for item in rows if item["starred"]]
        unstarred = [item for item in rows if not item["starred"]]

        display = starred[: self._session_starred_limit] + unstarred[: self._session_recent_limit]
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

    def _find_thread_session(self, user_id: str, chat_id: str, thread_id: str) -> dict[str, Any] | None:
        threads = self._adapter.list_threads_all(
            cwd=self._get_state(user_id, chat_id)["working_dir"],
            limit=self._thread_list_query_limit,
            sort_key="updated_at",
        )
        rows, _ = self._build_session_rows(user_id, chat_id, threads)
        return next((item for item in rows if item["thread_id"] == thread_id), None)

    def _extract_history_rounds(self, snapshot: ThreadSnapshot) -> list[tuple[str, str]]:
        rounds: list[tuple[str, str]] = []
        for turn in snapshot.turns:
            user_parts: list[str] = []
            assistant_parts: list[str] = []
            for item in turn.get("items") or []:
                item_type = item.get("type")
                if item_type == "userMessage":
                    for content in item.get("content") or []:
                        if content.get("type") == "text" and content.get("text"):
                            user_parts.append(content["text"])
                elif item_type == "agentMessage" and item.get("text"):
                    assistant_parts.append(item["text"])
            user_text = "\n".join(part.strip() for part in user_parts if part.strip()).strip()
            assistant_text = "\n\n".join(part.strip() for part in assistant_parts if part.strip()).strip()
            if user_text or assistant_text:
                rounds.append((user_text or "（空）", assistant_text or "（无回复）"))
        return rounds[-self._history_preview_rounds :]

    def _handle_adapter_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "thread/status/changed":
            self._handle_thread_status_changed(params)
            return
        if method == "thread/name/updated":
            self._handle_thread_name_updated(params)
            return
        if method == "turn/started":
            self._handle_turn_started(params)
            return
        if method == "turn/plan/updated":
            self._handle_turn_plan_updated(params)
            return
        if method == "item/started":
            self._handle_item_started(params)
            return
        if method == "item/agentMessage/delta":
            self._handle_agent_message_delta(params)
            return
        if method == "item/commandExecution/outputDelta":
            self._handle_command_delta(params)
            return
        if method == "item/fileChange/outputDelta":
            self._handle_file_change_delta(params)
            return
        if method == "item/completed":
            self._handle_item_completed(params)
            return
        if method == "turn/completed":
            self._handle_turn_completed(params)
            return

    def _handle_adapter_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            logger.warning("未找到线程绑定，自动 fail-close: method=%s thread=%s", method, thread_id)
            self._auto_reject_request(request_id, method, params)
            return
        user_id, chat_id = binding
        request_key = str(request_id)

        if method == "item/commandExecution/requestApproval":
            card = build_command_approval_card(
                request_key,
                command=params.get("command") or "",
                cwd=params.get("cwd") or "",
                reason=params.get("reason") or "",
            )
            title = "Codex 命令执行审批"
        elif method == "item/fileChange/requestApproval":
            card = build_file_change_approval_card(
                request_key,
                grant_root=params.get("grantRoot") or "",
                reason=params.get("reason") or "",
            )
            title = "Codex 文件修改审批"
        elif method == "item/permissions/requestApproval":
            card = build_permissions_approval_card(
                request_key,
                permissions=params.get("permissions") or {},
                reason=params.get("reason") or "",
            )
            title = "Codex 额外权限审批"
        elif method == "item/tool/requestUserInput":
            card = build_ask_user_card(request_key, params.get("questions") or [])
            title = "Codex 用户输入"
        elif method == "mcpServer/elicitation/request":
            self.bot.reply(chat_id, "收到 MCP elicitation 请求，当前版本暂未支持，已取消该请求。")
            self._adapter.respond(request_id, result={"action": "cancel"})
            return
        else:
            logger.warning("未支持的 Codex server request: %s", method)
            self._adapter.respond(
                request_id,
                error={"code": -32001, "message": f"Unsupported request: {method}"},
            )
            return

        message_id = self.bot.send_message_get_id(chat_id, "interactive", json.dumps(card, ensure_ascii=False))
        if not message_id:
            logger.warning("审批/问答卡片发送失败，执行 fail-close: method=%s", method)
            self._auto_reject_request(request_id, method, params)
            return

        with self._lock:
            self._pending_requests[request_key] = {
                "rpc_request_id": request_id,
                "method": method,
                "params": params,
                "thread_id": thread_id,
                "turn_id": params.get("turnId", ""),
                "title": title,
                "message_id": message_id,
                "questions": params.get("questions") or [],
                "answers": {},
                "user_id": user_id,
                "chat_id": chat_id,
            }

    def _auto_reject_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        if method == "item/commandExecution/requestApproval":
            self._adapter.respond(request_id, result={"decision": "abort"})
        elif method == "item/fileChange/requestApproval":
            self._adapter.respond(request_id, result={"decision": "cancel"})
        elif method == "item/permissions/requestApproval":
            self._adapter.respond(request_id, result={"permissions": {}, "scope": "turn"})
        elif method == "item/tool/requestUserInput":
            self._adapter.respond(
                request_id,
                error={"code": -32002, "message": "Unable to deliver user input request to Feishu"},
            )
        elif method == "mcpServer/elicitation/request":
            self._adapter.respond(request_id, result={"action": "cancel"})
        else:
            self._adapter.respond(request_id, error={"code": -32001, "message": f"Unsupported request: {method}"})

    def _handle_thread_status_changed(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        status = params.get("status") or {}
        status_type = status.get("type")
        with self._lock:
            state["running"] = status_type == "active"
        self._schedule_execution_card_update(*binding)

    def _handle_thread_name_updated(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        with self._lock:
            if state["current_thread_id"] == thread_id:
                state["current_thread_name"] = params.get("threadName") or state["current_thread_name"]

    def _handle_turn_started(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        turn = params.get("turn") or {}
        with self._lock:
            state["current_turn_id"] = turn.get("id", "")
            state["running"] = True
            self._clear_plan_state(state)
        self._schedule_execution_card_update(*binding)

    def _handle_turn_plan_updated(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        turn_id = params.get("turnId", "")
        plan = params.get("plan") or []
        explanation = params.get("explanation") or ""
        with self._lock:
            current_turn_id = state["current_turn_id"]
            if current_turn_id and turn_id and current_turn_id != turn_id:
                return
            state["plan_turn_id"] = turn_id or state["plan_turn_id"]
            state["plan_explanation"] = explanation
            state["plan_steps"] = [
                {"step": str(item.get("step", "")).strip(), "status": str(item.get("status", "")).strip()}
                for item in plan
                if str(item.get("step", "")).strip()
            ]
        self._flush_plan_card(*binding)

    def _handle_item_started(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "commandExecution":
            command = item.get("command") or ""
            cwd = item.get("cwd") or ""
            self._append_log_by_thread(
                params.get("threadId", ""),
                f"\n$ ({display_path(cwd)}) {command}\n",
            )
        elif item_type == "fileChange":
            self._append_log_by_thread(params.get("threadId", ""), "\n[准备应用文件修改]\n")

    def _handle_agent_message_delta(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        with self._lock:
            state["full_reply_text"] += params.get("delta", "")
        self._schedule_execution_card_update(*binding)

    def _handle_command_delta(self, params: dict[str, Any]) -> None:
        self._append_log_by_thread(params.get("threadId", ""), params.get("delta", ""))

    def _handle_file_change_delta(self, params: dict[str, Any]) -> None:
        self._append_log_by_thread(params.get("threadId", ""), params.get("delta", ""))

    def _handle_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_type = item.get("type")
        thread_id = params.get("threadId", "")
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            status = item.get("status")
            tail = f"\n[命令结束 status={status} exit={exit_code}]\n"
            self._append_log_by_thread(thread_id, tail)
        elif item_type == "fileChange":
            changes = item.get("changes") or []
            if changes:
                summary = "\n".join(f"- {change.get('kind', 'update')}: {change.get('path', '')}" for change in changes[:20])
                self._append_log_by_thread(thread_id, f"\n[文件变更]\n{summary}\n")
        elif item_type == "agentMessage" and item.get("text"):
            binding = self._thread_bindings.get(thread_id)
            if not binding:
                return
            state = self._get_state(*binding)
            with self._lock:
                if len(item["text"]) > len(state["full_reply_text"]):
                    state["full_reply_text"] = item["text"]
            self._schedule_execution_card_update(*binding)
        elif item_type == "plan" and item.get("text"):
            binding = self._thread_bindings.get(thread_id)
            if not binding:
                return
            state = self._get_state(*binding)
            turn_id = params.get("turnId", "")
            with self._lock:
                current_turn_id = state["current_turn_id"]
                if current_turn_id and turn_id and current_turn_id != turn_id:
                    return
                state["plan_turn_id"] = turn_id or state["plan_turn_id"]
                if len(item["text"]) >= len(state["plan_text"]):
                    state["plan_text"] = item["text"]
            self._flush_plan_card(*binding)

    def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        status = turn.get("status")
        with self._lock:
            state["running"] = False
            state["current_turn_id"] = ""
            if status == "interrupted":
                state["cancelled"] = True
            if error and not state["full_reply_text"]:
                state["full_reply_text"] = error.get("message") or "执行失败"
            elif error:
                state["full_log_text"] += f"\n[错误] {error.get('message', '执行失败')}\n"
        self._flush_execution_card(*binding, immediate=True)
        self._send_followup_if_needed(*binding)

    def _append_log_by_thread(self, thread_id: str, text: str) -> None:
        binding = self._thread_bindings.get(thread_id)
        if not binding:
            return
        state = self._get_state(*binding)
        with self._lock:
            state["full_log_text"] += text
        self._schedule_execution_card_update(*binding)

    def _send_execution_card(self, chat_id: str, parent_message_id: str) -> str | None:
        card = build_execution_card("", running=True)
        content = json.dumps(card, ensure_ascii=False)
        if parent_message_id:
            return self.bot.reply_to_message(parent_message_id, "interactive", content)
        return self.bot.send_message_get_id(chat_id, "interactive", content)

    def _schedule_execution_card_update(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        message_id = state.get("current_message_id", "")
        if not message_id:
            return
        now = time.monotonic()
        with self._lock:
            last_patch = state["last_patch_at"]
            timer = state.get("patch_timer")
            if now - last_patch >= self._stream_patch_interval_ms / 1000:
                state["last_patch_at"] = now
                state["patch_timer"] = None
                immediate = True
            elif timer is None:
                delay = self._stream_patch_interval_ms / 1000 - (now - last_patch)
                timer = threading.Timer(delay, self._flush_execution_card, args=(user_id, chat_id))
                timer.daemon = True
                state["patch_timer"] = timer
                timer.start()
                immediate = False
            else:
                immediate = False
        if immediate:
            self._flush_execution_card(user_id, chat_id)

    def _flush_execution_card(self, user_id: str, chat_id: str, immediate: bool = False) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            timer = state.get("patch_timer")
            if timer is not None:
                timer.cancel()
                state["patch_timer"] = None
            message_id = state.get("current_message_id", "")
            if not message_id:
                return
            reply_text = state["full_reply_text"]
            log_text = state["full_log_text"]
            running = state["running"]
            cancelled = state["cancelled"]
            elapsed = int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0
            state["last_patch_at"] = time.monotonic()

        card = build_execution_card(
            self._card_log_text(log_text),
            self._card_reply_text(reply_text),
            running=running,
            elapsed=elapsed,
            cancelled=cancelled and not running,
        )
        ok = self.bot.patch_message(message_id, json.dumps(card, ensure_ascii=False))
        if not ok and immediate and reply_text:
            self.bot.reply(chat_id, reply_text)

    def _send_followup_if_needed(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["followup_sent"]:
                return
            reply_text = state["full_reply_text"]
            current_message_id = state["current_message_id"]
            need_followup = not current_message_id or len(reply_text) > self._card_reply_limit
            if not reply_text or not need_followup:
                return
            state["followup_sent"] = True
        self.bot.reply(chat_id, reply_text)

    def _clear_plan_state(self, state: dict[str, Any]) -> None:
        state["plan_message_id"] = ""
        state["plan_turn_id"] = ""
        state["plan_explanation"] = ""
        state["plan_steps"] = []
        state["plan_text"] = ""

    def _flush_plan_card(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            plan_message_id = state.get("plan_message_id", "")
            parent_message_id = state.get("current_message_id", "")
            turn_id = state.get("plan_turn_id", "")
            explanation = state.get("plan_explanation", "")
            plan_steps = list(state.get("plan_steps") or [])
            plan_text = state.get("plan_text", "")
        if not explanation and not plan_steps and not plan_text:
            return

        card = build_plan_card(
            turn_id,
            explanation=explanation,
            plan_steps=plan_steps,
            plan_text=plan_text,
        )
        content = json.dumps(card, ensure_ascii=False)

        if plan_message_id:
            if self.bot.patch_message(plan_message_id, content):
                return
            with self._lock:
                if state.get("plan_message_id") == plan_message_id:
                    state["plan_message_id"] = ""

        new_message_id: str | None = None
        if parent_message_id:
            new_message_id = self.bot.reply_to_message(parent_message_id, "interactive", content)
        if not new_message_id:
            new_message_id = self.bot.send_message_get_id(chat_id, "interactive", content)
        if new_message_id:
            with self._lock:
                state["plan_message_id"] = new_message_id

    def _card_reply_text(self, text: str) -> str:
        if len(text) <= self._card_reply_limit:
            return text
        return text[: self._card_reply_limit] + "\n\n**[回复过长，完整内容已另行发送为文本消息]**"

    def _card_log_text(self, text: str) -> str:
        if len(text) <= self._card_log_limit:
            return text
        return text[-self._card_log_limit :] + "\n\n**[日志已截断，仅保留最近部分]**"

    def _reply_help(self, chat_id: str) -> None:
        self.bot.reply(
            chat_id,
            (
                "可用命令：\n"
                "/new 新建线程\n"
                "/session 查看当前目录线程\n"
                "/resume <thread_id|thread_name> 恢复线程并切换目录\n"
                "/cd <path> 切换当前目录并清空当前线程绑定\n"
                "/pwd 查看当前目录\n"
                "/rename <title> 重命名当前线程\n"
                "/star 收藏或取消收藏当前线程\n"
                "/approval 查看或设置审批策略\n"
                "/mode 查看或设置协作模式\n"
                "/status 查看当前状态\n"
                "/cancel 停止当前执行\n"
                "/help 查看帮助\n\n"
                "直接发送普通文本即可向当前线程提问；如果当前没有绑定线程，会在当前目录自动新建。"
            ),
        )
