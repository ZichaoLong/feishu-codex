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
from dataclasses import replace
from typing import Any
from uuid import UUID

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.base import RuntimeConfigSummary, ThreadSnapshot, ThreadSummary
from bot.cards import (
    build_approval_handled_card,
    build_approval_policy_card,
    build_ask_user_answered_card,
    build_ask_user_card,
    build_collaboration_mode_card,
    build_command_approval_card,
    build_execution_card,
    build_group_acl_card,
    build_group_mode_card,
    build_file_change_approval_card,
    build_history_preview_card,
    build_markdown_card,
    build_plan_card,
    build_permissions_preset_card,
    build_permissions_approval_card,
    build_resume_guard_card,
    build_rename_card,
    build_sandbox_policy_card,
    build_sessions_card,
    build_thread_snapshot_card,
)
from bot.config import load_config_file
from bot.constants import (
    DEFAULT_APP_SERVER_MODE,
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
from bot.codex_protocol.client import CodexRpcError
from bot.profile_resolution import DefaultProfileResolution, resolve_local_default_profile
from bot.session_resolution import (
    format_thread_match,
    list_current_dir_threads,
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.stores.favorites_store import FavoritesStore
from bot.stores.profile_state_store import ProfileStateStore

logger = logging.getLogger(__name__)

_CARD_REPLY_LIMIT_DEFAULT = 12000
_CARD_LOG_LIMIT_DEFAULT = 8000
_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_POLICIES = {"read-only", "workspace-write", "danger-full-access"}
_LOCAL_THREAD_SAFETY_RULE = (
    "如需在本地继续同一线程，请使用 `fcodex`，不要与裸 `codex` 同时写同一线程。"
)
_PERMISSIONS_PRESETS: dict[str, dict[str, str]] = {
    "read-only": {
        "label": "Read Only",
        "approval_policy": "on-request",
        "sandbox": "read-only",
    },
    "default": {
        "label": "Default",
        "approval_policy": "on-request",
        "sandbox": "workspace-write",
    },
    "full-access": {
        "label": "Full Access",
        "approval_policy": "never",
        "sandbox": "danger-full-access",
    },
}


def _permissions_preset_key(approval_policy: str, sandbox: str) -> str:
    for preset, config in _PERMISSIONS_PRESETS.items():
        if config["approval_policy"] == approval_policy and config["sandbox"] == sandbox:
            return preset
    return ""


def _permissions_summary(approval_policy: str, sandbox: str) -> str:
    preset = _permissions_preset_key(approval_policy, sandbox)
    if preset:
        return _PERMISSIONS_PRESETS[preset]["label"]
    return f"Custom ({sandbox}, {approval_policy})"


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
        self._pending_rename_forms: dict[str, dict[str, str]] = {}

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
        self._app_server_runtime = AppServerRuntimeStore(self._data_dir)
        if self._adapter_config.app_server_mode == "remote":
            self._adapter_config = replace(
                self._adapter_config,
                app_server_url=resolve_effective_app_server_url(
                    self._adapter_config.app_server_url,
                    data_dir=self._data_dir,
                ),
            )
        self._favorites = FavoritesStore(self._data_dir)
        self._profile_state = ProfileStateStore(self._data_dir)
        self._adapter = CodexAppServerAdapter(
            self._adapter_config,
            on_notification=self._handle_adapter_notification,
            on_request=self._handle_adapter_request,
            app_server_runtime_store=self._app_server_runtime,
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

    def on_register(self, bot) -> None:
        super().on_register(bot)
        try:
            self._adapter.start()
        except Exception:
            logger.exception("启动 Codex app-server 失败")
            raise

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
            rename_fallback = self._handle_rename_form_fallback(user_id, chat_id, message_id, action_value)
            if rename_fallback is not None:
                return rename_fallback
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
            return self._handle_resume_thread_action(user_id, chat_id, action_value)
        if action == "preview_thread_snapshot":
            return self._handle_preview_thread_snapshot_action(user_id, chat_id, action_value)
        if action == "resume_thread_write":
            return self._handle_resume_thread_write_action(user_id, chat_id, action_value)
        if action == "cancel_resume_guard":
            return self.bot.make_card_response(toast="已取消。")
        if action == "toggle_star_thread":
            return self._handle_toggle_star_action(user_id, chat_id, action_value)
        if action == "show_rename_form":
            return self._handle_show_rename_action(user_id, chat_id, message_id, action_value)
        if action == "rename_thread":
            return self._handle_rename_submit_action(user_id, chat_id, message_id, action_value)
        if action == "cancel_rename":
            self._clear_pending_rename_form(message_id)
            return self._handle_sessions_refresh_action(user_id, chat_id, toast="已取消")
        if action == "set_approval_policy":
            return self._handle_set_approval_policy(user_id, chat_id, action_value)
        if action == "set_sandbox_policy":
            return self._handle_set_sandbox_policy(user_id, chat_id, action_value)
        if action == "set_permissions_preset":
            return self._handle_set_permissions_preset(user_id, chat_id, action_value)
        if action == "set_collaboration_mode":
            return self._handle_set_collaboration_mode(user_id, chat_id, action_value)
        if action == "set_group_mode":
            return self._handle_set_group_mode_action(user_id, chat_id, action_value)
        if action == "set_group_acl_policy":
            return self._handle_set_group_acl_policy_action(user_id, chat_id, action_value)
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

    def _handle_rename_form_fallback(
        self,
        user_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse | None:
        form_value = action_value.get("_form_value") or {}
        if not message_id or not isinstance(form_value, dict) or "rename_title" not in form_value:
            return None

        with self._lock:
            pending = self._pending_rename_forms.get(message_id)
        if not pending:
            return self.bot.make_card_response(
                toast="重命名表单已失效，请重新打开。",
                toast_type="warning",
            )

        payload = dict(action_value)
        payload["action"] = "rename_thread"
        payload["thread_id"] = pending["thread_id"]
        return self._handle_rename_submit_action(user_id, chat_id, message_id, payload)

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
                    "pending_local_turn_card": False,
                    "approval_policy": self._adapter_config.approval_policy,
                    "sandbox": self._adapter_config.sandbox,
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
            self._reply_help(chat_id, arg)
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
        if cmd == "/whoami":
            self._handle_whoami_command(user_id, chat_id, message_id=message_id)
            return
        if cmd == "/whoareyou":
            self._handle_botinfo_command(chat_id)
            return
        if cmd == "/profile":
            self._handle_profile_command(user_id, chat_id, arg)
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
        if cmd == "/rm":
            self._handle_rm_command(user_id, chat_id, arg)
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
        if cmd == "/sandbox":
            self._handle_sandbox_command(user_id, chat_id, arg)
            return
        if cmd == "/permissions":
            self._handle_permissions_command(user_id, chat_id, arg)
            return
        if cmd == "/mode":
            self._handle_mode_command(user_id, chat_id, arg)
            return
        if cmd == "/groupmode":
            self._handle_groupmode_command(user_id, chat_id, arg, message_id=message_id)
            return
        if cmd == "/acl":
            self._handle_acl_command(user_id, chat_id, arg, message_id=message_id)
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
            state["pending_local_turn_card"] = True
            self._clear_plan_state(state)

        card_id = ""
        if message_id and hasattr(self.bot, "claim_reserved_execution_card"):
            card_id = str(self.bot.claim_reserved_execution_card(message_id) or "").strip()
            if card_id:
                self.bot.patch_message(
                    card_id,
                    json.dumps(build_execution_card("", running=True), ensure_ascii=False),
                )
        if not card_id:
            card_id = self._send_execution_card(chat_id, message_id)
        with self._lock:
            state["current_message_id"] = card_id or ""

        try:
            self._adapter.start_turn(
                thread_id=thread_id,
                text=text,
                cwd=state["working_dir"],
                model=state["model"] or None,
                profile=self._effective_default_profile() or None,
                approval_policy=state["approval_policy"] or None,
                sandbox=state["sandbox"] or None,
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
                self.bot.reply_card(
                    chat_id,
                    build_markdown_card(
                        "Codex 目录未切换",
                        "执行中不能切换目录，请等待结束或先停止当前执行。",
                        template="orange",
                    ),
                )
                return

        if not arg:
            self.bot.reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 当前目录",
                    f"当前目录：`{display_path(state['working_dir'])}`",
                ),
            )
            return

        target = resolve_working_dir(arg, fallback=state["working_dir"])
        if not pathlib.Path(target).exists():
            self.bot.reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 目录未切换",
                    f"目录不存在：`{display_path(target)}`",
                    template="orange",
                ),
            )
            return
        if not pathlib.Path(target).is_dir():
            self.bot.reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 目录未切换",
                    f"不是目录：`{display_path(target)}`",
                    template="orange",
                ),
            )
            return

        self._clear_thread_binding(user_id, chat_id)
        with self._lock:
            state["working_dir"] = target
        self.bot.reply_card(
            chat_id,
            build_markdown_card(
                "Codex 目录已切换",
                (
                    f"目录：`{display_path(target)}`\n"
                    "当前线程绑定已清空。\n"
                    "直接发送普通文本，会在新目录自动新建线程。"
                ),
            ),
        )

    def _handle_new_command(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "执行中不能新建线程，请等待结束或先执行 `/cancel`。")
                return
        try:
            snapshot = self._adapter.create_thread(
                cwd=state["working_dir"],
                profile=self._effective_default_profile() or None,
                approval_policy=state["approval_policy"] or None,
                sandbox=state["sandbox"] or None,
            )
        except Exception as exc:
            logger.exception("新建线程失败")
            self.bot.reply(chat_id, f"新建线程失败：{exc}")
            return
        self._bind_thread(user_id, chat_id, snapshot.summary)
        self.bot.reply_card(
            chat_id,
            build_markdown_card(
                "Codex 线程已新建",
                (
                    f"线程：`{snapshot.summary.thread_id[:8]}…`\n"
                    f"目录：`{display_path(snapshot.summary.cwd)}`\n"
                    "直接发送普通文本开始第一轮对话。"
                ),
                template="green",
            ),
        )

    def _handle_status_command(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        thread_id = state["current_thread_id"]
        title = state["current_thread_name"] or "（未绑定线程）"
        running = "是" if state["running"] else "否"
        turn_id = state["current_turn_id"][:8] + "…" if state["current_turn_id"] else "-"
        permissions_summary = _permissions_summary(state["approval_policy"], state["sandbox"])
        runtime_config = self._safe_read_runtime_config()
        profile_resolution = self._current_default_profile_resolution(runtime_config)
        local_profile = profile_resolution.effective_profile
        if runtime_config:
            profile_line = f"默认 profile：`{local_profile or '（未设置）'}`"
            provider_line = f"当前 provider：`{runtime_config.current_model_provider or '（未设置）'}`"
        else:
            profile_line = f"默认 profile：`{local_profile or '（未设置）'}`"
            provider_line = "当前 provider：读取失败"
        header = (
            f"目录：`{display_path(state['working_dir'])}`\n当前线程：`{thread_id[:8]}…` {title}"
            if thread_id
            else f"目录：`{display_path(state['working_dir'])}`\n当前线程：-"
        )
        if state["running"]:
            next_step = "如需停止当前执行，可点当前执行卡片上的停止按钮。"
        elif not thread_id:
            next_step = "直接发送普通文本，会在当前目录自动新建线程。"
        else:
            next_step = "发送 `/help` 查看常用命令。"
        content = (
            f"{header}\n"
            f"执行中：{running}\n"
            f"当前 turn：{turn_id}\n"
            f"{profile_line}\n"
            f"{provider_line}\n"
            f"权限预设：`{permissions_summary}`\n"
            f"审批策略：`{state['approval_policy']}`\n"
            f"沙箱策略：`{state['sandbox']}`\n"
            f"协作模式：`{state['collaboration_mode']}`"
            + (
                f"\n\n注意：之前保存的默认 profile `{profile_resolution.stale_profile}` 已不存在，已自动回退到 Codex 原生默认。"
                if profile_resolution.stale_profile
                else ""
            )
            + f"\n\n{next_step}"
        )
        template = "turquoise" if state["running"] else "blue"
        self.bot.reply_card(
            chat_id,
            build_markdown_card("Codex 当前状态", content, template=template),
        )

    def _handle_whoami_command(self, user_id: str, chat_id: str, *, message_id: str = "") -> None:
        context = self.bot.get_message_context(message_id) if message_id else {}
        chat_type = str(context.get("chat_type", "")).strip()
        if chat_type == "group":
            self.bot.reply(chat_id, "请私聊机器人执行 `/whoami`。")
            return
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        name = self.bot.get_sender_display_name(
            user_id=user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        self.bot.reply(
            chat_id,
            "\n".join(
                [
                    "你的身份信息：",
                    f"- name: `{name}`",
                    f"- user_id: `{user_id or '（空）'}`",
                    f"- open_id: `{sender_open_id or '（空）'}`",
                    "",
                    "配置管理员时，把 `open_id` 写进 `system.yaml` 的 `admin_open_ids`。",
                ]
            ),
        )

    def _handle_botinfo_command(self, chat_id: str) -> None:
        identity = self.bot.get_bot_identity()
        source_map = {
            "configured": "`system.yaml.bot_open_id`",
            "auto-discovered": "运行时自动发现",
            "unavailable": "未获取到",
        }
        source = source_map.get(identity["source"], identity["source"] or "未知")
        lines = [
            "机器人身份信息：",
            f"- app_id: `{identity['app_id'] or '（空）'}`",
            f"- open_id: `{identity['open_id'] or '（空）'}`",
            f"- source: {source}",
        ]
        if not identity["open_id"]:
            lines.extend(
                [
                    "",
                    "建议：",
                    "- 直接把 `open_id` 写进 `system.yaml.bot_open_id`",
                    "- 如果依赖自动发现，检查 `application:application:self_manage` 权限",
                ]
            )
        self.bot.reply(chat_id, "\n".join(lines))

    def _handle_session_command(self, user_id: str, chat_id: str) -> None:
        try:
            threads = self._list_current_dir_threads(user_id, chat_id)
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
            self.bot.reply(
                chat_id,
                "用法：`/resume <thread_id 或 thread_name>`\n发送 `/help session` 查看 `/session` 与 `/resume` 的区别。",
            )
            return
        try:
            thread = self._resolve_resume_target(arg)
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            self.bot.reply(chat_id, f"恢复线程失败：{exc}")
            return
        if self._is_loaded_in_current_backend(thread):
            self._resume_thread_in_background(user_id, chat_id, thread.thread_id, original_arg=arg, summary=thread)
            return
        self.bot.reply_card(chat_id, self._build_resume_guard(thread))

    def _handle_profile_command(self, user_id: str, chat_id: str, arg: str) -> None:
        runtime_config = self._safe_read_runtime_config()
        if runtime_config is None:
            self.bot.reply(chat_id, "读取 Codex 运行时配置失败，无法查看或切换 profile。")
            return
        profile_resolution = self._current_default_profile_resolution(runtime_config)
        local_profile = profile_resolution.effective_profile
        profiles = {profile.name: profile for profile in runtime_config.profiles}

        def _profile_provider_text(profile_name: str) -> str:
            if not profile_name:
                return "跟随 Codex 原生默认"
            profile = profiles.get(profile_name)
            if profile and profile.model_provider:
                return f"`{profile.model_provider}`"
            return "未显式设置，实际以新线程解析结果为准"

        if not arg:
            example_profile = runtime_config.profiles[0].name if runtime_config.profiles else "name"
            lines = [
                f"当前默认 profile：`{local_profile or '（未设置）'}`",
                f"默认 profile 对应 provider：{_profile_provider_text(local_profile)}",
                f"切换方式：`/profile <name>`，例如：`/profile {example_profile}`",
            ]
            if runtime_config.profiles:
                lines.extend(["", "**可用 profile**"])
                for profile in runtime_config.profiles:
                    provider = _profile_provider_text(profile.name)
                    marker = " <- 默认" if profile.name == local_profile else ""
                    lines.append(f"- `{profile.name}` -> {provider}{marker}")
            else:
                lines.append("未在当前 Codex 配置中发现可用 profile。")
            lines.extend(
                [
                    "",
                    "**说明**",
                    "作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。",
                    "已打开的 `fcodex` TUI 不会热切换。",
                    "如用 `fcodex -p <profile>`，以显式 profile 为准。",
                ]
            )
            if profile_resolution.stale_profile:
                lines.append(
                    f"注意：之前保存的默认 profile `{profile_resolution.stale_profile}` 已不存在，现已自动清空并回退到 Codex 原生默认。"
                )
            if self._adapter_config.model_provider:
                lines.append(
                    "注意：当前 feishu-codex 配置写死了 "
                    f"`model_provider: {self._adapter_config.model_provider}`，新建线程时可能仍以它为准。"
                )
            self.bot.reply_card(
                chat_id,
                build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
            )
            return

        target_profile = arg.strip()
        if target_profile not in profiles:
            self.bot.reply(
                chat_id,
                f"未找到 profile：`{target_profile}`\n用法：`/profile <name>`\n先发 `/profile` 查看可用 profile。",
            )
            return

        try:
            self._profile_state.save_default_profile(target_profile)
        except Exception as exc:
            logger.exception("保存 feishu-codex 默认 profile 失败")
            self.bot.reply(chat_id, f"切换 profile 失败：{exc}")
            return

        state = self._get_state(user_id, chat_id)
        lines = [
            f"已切换默认 profile：`{target_profile}`",
            f"默认 profile 对应 provider：{_profile_provider_text(target_profile)}",
            "再次切换：`/profile <name>`",
        ]
        lines.extend(
            [
                "",
                "**说明**",
                "作用范围：只影响 feishu-codex 与新的默认 `fcodex` 启动；不改裸 `codex`。",
            ]
        )
        if state["running"]:
            lines.append("如果当前正在执行，新 profile 从下一轮生效。")
        lines.append("已打开的 `fcodex` TUI 不会热切换。")
        lines.append("如用 `fcodex -p` 固定 profile，该会话不受影响。")
        if self._adapter_config.model_provider:
            lines.append(
                "注意：当前 feishu-codex 配置写死了 "
                f"`model_provider: {self._adapter_config.model_provider}`，新建线程时可能仍以它为准。"
            )
        self.bot.reply_card(
            chat_id,
            build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
        )

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

    def _handle_rm_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "执行中不能归档线程，请等待结束或先执行 `/cancel`。")
                return
        target = arg.strip() if arg else ""
        if target:
            try:
                thread = self._resolve_resume_target(target)
            except Exception as exc:
                logger.exception("解析归档目标失败")
                self.bot.reply(chat_id, f"归档线程失败：{exc}")
                return
        else:
            if not state["current_thread_id"]:
                self.bot.reply(chat_id, "用法：`/rm [thread_id 或 thread_name]`；省略参数时归档当前线程。")
                return
            try:
                thread = self._read_thread_summary(state["current_thread_id"], original_arg=state["current_thread_id"])
            except Exception as exc:
                logger.exception("读取当前线程失败")
                self.bot.reply(chat_id, f"归档线程失败：{exc}")
                return

        try:
            self._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            self.bot.reply(chat_id, f"归档线程失败：{exc}")
            return

        self._favorites.remove_thread_globally(thread.thread_id)
        if state["current_thread_id"] == thread.thread_id:
            self._clear_thread_binding(user_id, chat_id)
        self.bot.reply(
            chat_id,
            (
                f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}\n"
                "说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。"
            ),
        )

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
            policy = arg.strip().lower()
            if policy not in _APPROVAL_POLICIES:
                self.bot.reply(chat_id, "审批策略仅支持：`untrusted`、`on-failure`、`on-request`、`never`")
                return
            with self._lock:
                state["approval_policy"] = policy
                running = state["running"]
            message = f"已切换审批策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self.bot.reply(chat_id, message)
            return
        self.bot.reply_card(
            chat_id,
            build_approval_policy_card(state["approval_policy"], running=state["running"]),
        )

    def _handle_sandbox_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in _SANDBOX_POLICIES:
                self.bot.reply(chat_id, "沙箱策略仅支持：`read-only`、`workspace-write`、`danger-full-access`")
                return
            with self._lock:
                state["sandbox"] = policy
                running = state["running"]
            message = f"已切换沙箱策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self.bot.reply(chat_id, message)
            return
        self.bot.reply_card(
            chat_id,
            build_sandbox_policy_card(state["sandbox"], running=state["running"]),
        )

    def _handle_permissions_command(self, user_id: str, chat_id: str, arg: str) -> None:
        state = self._get_state(user_id, chat_id)
        if arg:
            preset = arg.strip().lower()
            config = _PERMISSIONS_PRESETS.get(preset)
            if config is None:
                self.bot.reply(chat_id, "权限预设仅支持：`read-only`、`default`、`full-access`")
                return
            with self._lock:
                state["approval_policy"] = config["approval_policy"]
                state["sandbox"] = config["sandbox"]
                running = state["running"]
            message = (
                f"已切换权限预设：`{config['label']}`\n"
                f"审批：`{config['approval_policy']}`\n"
                f"沙箱：`{config['sandbox']}`\n"
                "作用范围：只影响当前飞书会话的后续 turn。"
            )
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self.bot.reply(chat_id, message)
            return
        self.bot.reply_card(
            chat_id,
            build_permissions_preset_card(
                state["approval_policy"],
                state["sandbox"],
                running=state["running"],
            ),
        )

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
            message = f"已切换协作模式：`{mode}`\n作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self.bot.reply(chat_id, message)
            return
        self.bot.reply_card(
            chat_id,
            build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            ),
        )

    def _require_group_chat(self, chat_id: str, message_id: str = "") -> dict[str, Any] | None:
        context = self.bot.get_message_context(message_id) if message_id else {}
        chat_type = str(context.get("chat_type", "")).strip()
        if not chat_type and hasattr(self.bot, "lookup_chat_type"):
            chat_type = str(self.bot.lookup_chat_type(chat_id) or "").strip()
        if not chat_type and hasattr(self.bot, "fetch_chat_type"):
            chat_type = str(self.bot.fetch_chat_type(chat_id) or "").strip()
        if chat_type == "group":
            if not context:
                return {"chat_type": "group"}
            return context
        self.bot.reply(chat_id, "该命令仅支持群聊使用。")
        return None

    @staticmethod
    def _normalize_group_mode(mode: str) -> str:
        normalized = str(mode or "").strip().lower().replace("-", "_")
        if normalized == "mention":
            return "mention_only"
        return normalized

    def _group_mode_card(self, chat_id: str, *, user_id: str = "", open_id: str = "") -> dict:
        return build_group_mode_card(
            self.bot.get_group_mode(chat_id),
            can_manage=self.bot.is_group_admin(user_id, open_id),
        )

    def _group_acl_card(self, chat_id: str, *, user_id: str = "", open_id: str = "") -> dict:
        snapshot = self.bot.get_group_acl_snapshot(chat_id)
        return build_group_acl_card(
            snapshot["access_policy"],
            allowlist_members=list(snapshot["allowlist"]),
            viewer_allowed=self.bot.is_group_user_allowed(chat_id, user_id, open_id),
            can_manage=self.bot.is_group_admin(user_id, open_id),
        )

    def _handle_groupmode_command(self, user_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        context = self._require_group_chat(chat_id, message_id)
        if context is None:
            return
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self.bot.reply_card(
                chat_id,
                self._group_mode_card(chat_id, user_id=user_id, open_id=sender_open_id),
            )
            return
        if not self.bot.is_group_admin(user_id, sender_open_id):
            self.bot.reply(chat_id, "仅管理员可切换群聊工作态。")
            return
        mode = self._normalize_group_mode(arg)
        if mode not in {"assistant", "all", "mention_only"}:
            self.bot.reply(chat_id, "群聊工作态仅支持：`assistant`、`all`、`mention-only`")
            return
        self.bot.set_group_mode(chat_id, mode)
        labels = {
            "assistant": "assistant",
            "all": "all",
            "mention_only": "mention-only",
        }
        self.bot.reply(chat_id, f"已切换群聊工作态：`{labels[mode]}`")

    def _acl_target_open_ids(self, message_id: str, raw_arg: str) -> list[str]:
        targets = {
            item["open_id"]
            for item in self.bot.extract_non_bot_mentions(message_id)
            if item.get("open_id")
        }
        for token in str(raw_arg or "").replace(",", " ").split():
            token = token.strip()
            if token and not token.startswith("@"):
                targets.add(token)
        return sorted(targets)

    def _handle_acl_command(self, user_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        context = self._require_group_chat(chat_id, message_id)
        if context is None:
            return
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self.bot.reply_card(
                chat_id,
                self._group_acl_card(chat_id, user_id=user_id, open_id=sender_open_id),
            )
            return

        cmd, _, rest = arg.partition(" ")
        subcommand = cmd.strip().lower()
        payload = rest.strip()
        is_admin = self.bot.is_group_admin(user_id, sender_open_id)
        if subcommand in {"admin-only", "allowlist", "all-members"}:
            payload = subcommand
            subcommand = "policy"

        if subcommand == "policy":
            if not is_admin:
                self.bot.reply(chat_id, "仅管理员可调整群聊授权策略。")
                return
            policy = payload.strip().lower()
            if policy not in {"admin-only", "allowlist", "all-members"}:
                self.bot.reply(chat_id, "用法：`/acl policy <admin-only|allowlist|all-members>`")
                return
            self.bot.set_group_access_policy(chat_id, policy)
            self.bot.reply(chat_id, f"已切换群聊授权策略：`{policy}`")
            return

        if subcommand in {"grant", "allow"}:
            if not is_admin:
                self.bot.reply(chat_id, "仅管理员可授权成员。")
                return
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self.bot.reply(chat_id, "用法：`/acl grant @成员` 或 `/acl grant <open_id>`")
                return
            updated = self.bot.grant_group_members(chat_id, targets)
            self.bot.reply(chat_id, f"已授权 {len(targets)} 人，当前 allowlist 共 {len(updated)} 人。")
            return

        if subcommand in {"revoke", "remove"}:
            if not is_admin:
                self.bot.reply(chat_id, "仅管理员可撤销成员授权。")
                return
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self.bot.reply(chat_id, "用法：`/acl revoke @成员` 或 `/acl revoke <open_id>`")
                return
            updated = self.bot.revoke_group_members(chat_id, targets)
            self.bot.reply(chat_id, f"已撤销 {len(targets)} 人，当前 allowlist 共 {len(updated)} 人。")
            return

        self.bot.reply(
            chat_id,
            "用法：`/acl`、`/acl policy <admin-only|allowlist|all-members>`、`/acl grant @成员`、`/acl revoke @成员`",
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

    def _handle_resume_thread_action(
        self,
        user_id: str,
        chat_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                return self.bot.make_card_response(
                    toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._read_thread_summary(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("查询恢复目标失败")
            return self.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if self._is_loaded_in_current_backend(thread):
            threading.Thread(
                target=self._resume_thread_in_background,
                args=(user_id, chat_id, thread_id),
                kwargs={"original_arg": thread_id, "summary": thread},
                daemon=True,
            ).start()
            return self.bot.make_card_response(toast="正在恢复线程…")
        return self.bot.make_card_response(card=self._build_resume_guard(thread))

    def _handle_preview_thread_snapshot_action(
        self,
        user_id: str,
        chat_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        threading.Thread(
            target=self._send_thread_snapshot_in_background,
            args=(chat_id, thread_id),
            daemon=True,
        ).start()
        return self.bot.make_card_response(toast="正在加载快照…")

    def _handle_resume_thread_write_action(
        self,
        user_id: str,
        chat_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            if state["running"]:
                return self.bot.make_card_response(
                    toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        threading.Thread(
            target=self._resume_thread_in_background,
            args=(user_id, chat_id, thread_id),
            kwargs={"original_arg": thread_id},
            daemon=True,
        ).start()
        return self.bot.make_card_response(toast="正在恢复线程并继续写入…")

    def _handle_show_rename_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        try:
            session = self._find_thread_session(user_id, chat_id, thread_id)
        except Exception as exc:
            logger.exception("查询重命名目标失败")
            return self.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not session:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        with self._lock:
            self._pending_rename_forms[message_id] = {"thread_id": thread_id}
        return self.bot.make_card_response(card=build_rename_card(session))

    def _handle_rename_submit_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
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
            self._pending_rename_forms.pop(message_id, None)
            if state["current_thread_id"] == thread_id:
                state["current_thread_name"] = new_title
        return self._handle_sessions_refresh_action(user_id, chat_id, toast="已重命名。")

    def _clear_pending_rename_form(self, message_id: str) -> None:
        if not message_id:
            return
        with self._lock:
            self._pending_rename_forms.pop(message_id, None)

    def _handle_sessions_refresh_action(
        self, user_id: str, chat_id: str, *, toast: str
    ) -> P2CardActionTriggerResponse:
        try:
            threads = self._list_current_dir_threads(user_id, chat_id)
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
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in _APPROVAL_POLICIES:
            return self.bot.make_card_response(toast="非法审批策略", toast_type="warning")
        state = self._get_state(user_id, chat_id)
        with self._lock:
            state["approval_policy"] = policy
            running = state["running"]
        toast = f"已切换审批策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return self.bot.make_card_response(
            card=build_approval_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def _handle_set_sandbox_policy(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in _SANDBOX_POLICIES:
            return self.bot.make_card_response(toast="非法沙箱策略", toast_type="warning")
        state = self._get_state(user_id, chat_id)
        with self._lock:
            state["sandbox"] = policy
            running = state["running"]
        toast = f"已切换沙箱策略：{policy}"
        if running:
            toast += "；下一轮生效"
        return self.bot.make_card_response(
            card=build_sandbox_policy_card(policy, running=running),
            toast=toast,
            toast_type="success",
        )

    def _handle_set_permissions_preset(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        preset = str(action_value.get("preset", "")).strip().lower()
        config = _PERMISSIONS_PRESETS.get(preset)
        if config is None:
            return self.bot.make_card_response(toast="非法权限预设", toast_type="warning")
        state = self._get_state(user_id, chat_id)
        with self._lock:
            state["approval_policy"] = config["approval_policy"]
            state["sandbox"] = config["sandbox"]
            running = state["running"]
        toast = f"已切换权限预设：{config['label']}"
        if running:
            toast += "；下一轮生效"
        return self.bot.make_card_response(
            card=build_permissions_preset_card(
                config["approval_policy"],
                config["sandbox"],
                running=running,
            ),
            toast=toast,
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
        toast = f"已切换协作模式：{mode}"
        if running:
            toast += "；下一轮生效"
        return self.bot.make_card_response(
            card=build_collaboration_mode_card(mode, running=running),
            toast=toast,
            toast_type="success",
        )

    def _handle_set_group_mode_action(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        mode = self._normalize_group_mode(str(action_value.get("mode", "")))
        if mode not in {"assistant", "all", "mention_only"}:
            return self.bot.make_card_response(toast="非法群聊工作态", toast_type="warning")
        if not self.bot.is_group_admin(user_id, operator_open_id):
            return self.bot.make_card_response(toast="仅管理员可切换群聊工作态。", toast_type="warning")
        self.bot.set_group_mode(chat_id, mode)
        return self.bot.make_card_response(
            card=self._group_mode_card(chat_id, user_id=user_id, open_id=operator_open_id),
            toast=f"已切换群聊工作态：{mode}",
            toast_type="success",
        )

    def _handle_set_group_acl_policy_action(
        self, user_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in {"admin-only", "allowlist", "all-members"}:
            return self.bot.make_card_response(toast="非法群聊授权策略", toast_type="warning")
        if not self.bot.is_group_admin(user_id, operator_open_id):
            return self.bot.make_card_response(toast="仅管理员可调整群聊授权策略。", toast_type="warning")
        self.bot.set_group_access_policy(chat_id, policy)
        return self.bot.make_card_response(
            card=self._group_acl_card(chat_id, user_id=user_id, open_id=operator_open_id),
            toast=f"已切换群聊授权策略：{policy}",
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
        snapshot = self._adapter.create_thread(
            cwd=state["working_dir"],
            profile=self._effective_default_profile() or None,
            approval_policy=state["approval_policy"] or None,
            sandbox=state["sandbox"] or None,
        )
        self._bind_thread(user_id, chat_id, snapshot.summary)
        return snapshot.summary.thread_id

    def _resume_thread_in_background(
        self,
        user_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str | None = None,
        summary: ThreadSummary | None = None,
    ) -> None:
        state = self._get_state(user_id, chat_id)
        try:
            snapshot = self._resume_snapshot_by_id(
                thread_id,
                original_arg=original_arg or thread_id,
                summary=summary,
            )
        except Exception as exc:
            logger.exception("恢复线程失败")
            self.bot.reply(chat_id, f"恢复线程失败：{exc}")
            return
        with self._lock:
            if state["running"]:
                self.bot.reply(chat_id, "当前线程仍在执行，暂不切换。")
                return
        self._bind_thread(user_id, chat_id, snapshot.summary)
        summary = (
            f"**已切换到线程**\n"
            f"thread：`{snapshot.summary.thread_id[:8]}…`\n"
            f"标题：{snapshot.summary.title}\n"
            f"目录：`{display_path(snapshot.summary.cwd)}`\n"
            f"{_LOCAL_THREAD_SAFETY_RULE}"
        )
        if self._show_history_preview_on_resume:
            rounds = self._extract_history_rounds(snapshot)
            if rounds:
                self.bot.reply_card(
                    chat_id,
                    build_history_preview_card(
                        snapshot.summary.thread_id,
                        rounds,
                        summary=summary,
                    ),
                )
                return
        self.bot.reply_card(
            chat_id,
            build_markdown_card("Codex 已切换线程", summary, template="green"),
        )

    def _send_thread_snapshot_in_background(self, chat_id: str, thread_id: str) -> None:
        try:
            snapshot = self._read_thread_snapshot(thread_id, original_arg=thread_id, include_turns=True)
        except Exception as exc:
            logger.exception("读取线程快照失败")
            self.bot.reply(chat_id, f"读取线程快照失败：{exc}")
            return
        rounds = self._extract_history_rounds(snapshot)
        self.bot.reply_card(
            chat_id,
            build_thread_snapshot_card(
                snapshot.summary.thread_id,
                title=snapshot.summary.title,
                cwd=snapshot.summary.cwd,
                updated_at=snapshot.summary.updated_at,
                source=snapshot.summary.source,
                service_name=snapshot.summary.service_name,
                rounds=rounds,
            ),
        )

    def _resolve_resume_target(self, arg: str) -> ThreadSummary:
        target = arg.strip()
        if looks_like_thread_id(target):
            return self._read_thread_summary(target, original_arg=target)
        thread = resolve_resume_target_by_name(
            self._adapter,
            name=target,
            limit=self._thread_list_query_limit,
        )
        return self._read_thread_summary(thread.thread_id, original_arg=target)

    def _resume_snapshot(self, arg: str) -> ThreadSnapshot:
        thread = self._resolve_resume_target(arg)
        return self._resume_snapshot_by_id(
            thread.thread_id,
            original_arg=arg.strip(),
            summary=thread,
        )

    def _read_thread_snapshot(
        self,
        thread_id: str,
        *,
        original_arg: str,
        include_turns: bool,
    ) -> ThreadSnapshot:
        try:
            return self._adapter.read_thread(thread_id, include_turns=include_turns)
        except Exception as exc:
            if self._is_thread_not_found_error(exc):
                raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
            raise

    def _read_thread_summary(self, thread_id: str, *, original_arg: str) -> ThreadSummary:
        return self._read_thread_snapshot(
            thread_id,
            original_arg=original_arg,
            include_turns=False,
        ).summary

    def _resume_snapshot_by_id(
        self,
        thread_id: str,
        *,
        original_arg: str,
        summary: ThreadSummary | None = None,
    ) -> ThreadSnapshot:
        thread = summary or self._find_thread_summary(thread_id)
        try:
            return self._adapter.resume_thread(
                thread_id,
                profile=self._effective_default_profile() or None,
            )
        except Exception as exc:
            if self._is_thread_not_found_error(exc):
                raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
            if thread and thread.source == "cli" and self._is_transport_disconnect(exc):
                raise RuntimeError(
                    "Codex 当前无法通过 app-server 恢复这个 CLI 线程。"
                    "这通常意味着该线程正被本地 TUI 使用，或当前版本暂不支持加载它的完整历史。"
                ) from exc
            raise

    @staticmethod
    def _is_loaded_in_current_backend(thread: ThreadSummary) -> bool:
        return thread.status not in {"", "notLoaded"}

    def _build_resume_guard(self, thread: ThreadSummary) -> dict:
        return build_resume_guard_card(
            thread.thread_id,
            title=thread.title,
            cwd=thread.cwd,
            updated_at=thread.updated_at,
            source=thread.source,
            service_name=thread.service_name,
        )

    def _find_thread_summary(self, thread_id: str) -> ThreadSummary | None:
        threads = self._list_global_threads()
        for thread in threads:
            if thread.thread_id == thread_id:
                return thread
        return None

    @staticmethod
    def _is_thread_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "")).lower()
        return message.startswith("no rollout found for thread id ")

    @staticmethod
    def _is_transport_disconnect(exc: Exception) -> bool:
        return isinstance(exc, CodexRpcError) and exc.error.get("message") == "Codex websocket disconnected"

    def _bind_thread(self, user_id: str, chat_id: str, thread: ThreadSummary) -> None:
        state = self._get_state(user_id, chat_id)
        takeover_binding: tuple[str, str] | None = None
        with self._lock:
            old_thread_id = state["current_thread_id"]
            if old_thread_id and self._thread_bindings.get(old_thread_id) == (user_id, chat_id):
                self._thread_bindings.pop(old_thread_id, None)
            existing_binding = self._thread_bindings.get(thread.thread_id)
            if existing_binding and existing_binding != (user_id, chat_id):
                takeover_binding = existing_binding
            state["current_thread_id"] = thread.thread_id
            state["current_thread_name"] = thread.name or thread.preview
            state["working_dir"] = thread.cwd or state["working_dir"]
            state["current_turn_id"] = ""
            state["pending_local_turn_card"] = False
            self._clear_plan_state(state)
            self._thread_bindings[thread.thread_id] = (user_id, chat_id)
        if takeover_binding:
            self.bot.reply(
                takeover_binding[1],
                (
                    f"线程 `{thread.thread_id[:8]}…` 已被另一飞书会话接管。"
                    "当前会话不再接收该线程的实时更新；如需重新接管，请再次执行 "
                    f"`/resume {thread.thread_id}`。"
                ),
            )

    def _clear_thread_binding(self, user_id: str, chat_id: str) -> None:
        state = self._get_state(user_id, chat_id)
        with self._lock:
            thread_id = state["current_thread_id"]
            if thread_id and self._thread_bindings.get(thread_id) == (user_id, chat_id):
                self._thread_bindings.pop(thread_id, None)
            state["current_thread_id"] = ""
            state["current_thread_name"] = ""
            state["current_turn_id"] = ""
            state["pending_local_turn_card"] = False
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
                "model_provider": thread.model_provider or "",
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
                    "model_provider": "",
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
        threads = self._list_current_dir_threads(user_id, chat_id)
        rows, _ = self._build_session_rows(user_id, chat_id, threads)
        return next((item for item in rows if item["thread_id"] == thread_id), None)

    @staticmethod
    def _all_model_providers_filter() -> list[str]:
        return []

    def _list_current_dir_threads(self, user_id: str, chat_id: str) -> list[ThreadSummary]:
        return list_current_dir_threads(
            self._adapter,
            cwd=self._get_state(user_id, chat_id)["working_dir"],
            limit=self._thread_list_query_limit,
        )

    def _list_global_threads(self) -> list[ThreadSummary]:
        return list_global_threads(
            self._adapter,
            limit=self._thread_list_query_limit,
        )

    @staticmethod
    def _format_thread_match(thread: ThreadSummary) -> str:
        return format_thread_match(thread)

    def _safe_read_runtime_config(self) -> RuntimeConfigSummary | None:
        try:
            return self._adapter.read_runtime_config()
        except Exception:
            logger.exception("读取 Codex 运行时配置失败")
            return None

    def _effective_default_profile(self) -> str:
        resolution = self._current_default_profile_resolution(self._safe_read_runtime_config())
        return resolution.effective_profile

    def _current_default_profile_resolution(
        self,
        runtime_config: RuntimeConfigSummary | None,
    ) -> DefaultProfileResolution:
        stored_profile = self._profile_state.load_default_profile().strip()
        resolution = resolve_local_default_profile(stored_profile, runtime_config)
        if resolution.stale_profile:
            self._profile_state.save_default_profile("")
            return DefaultProfileResolution(
                stored_profile=resolution.stale_profile,
                stale_profile=resolution.stale_profile,
                available_profiles=resolution.available_profiles,
            )
        return resolution

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
        turn_id = turn.get("id", "")
        with self._lock:
            external_turn = not state.get("pending_local_turn_card", False)
            state["current_turn_id"] = turn_id
            if external_turn:
                state["cancelled"] = False
                state["current_message_id"] = ""
                state["full_reply_text"] = ""
                state["full_log_text"] = ""
                state["started_at"] = time.monotonic()
                state["last_patch_at"] = 0.0
                state["followup_sent"] = False
            state["running"] = True
            state["pending_local_turn_card"] = False
            self._clear_plan_state(state)
        if external_turn:
            card_id = self._send_execution_card(binding[1], "")
            with self._lock:
                if state["current_turn_id"] == turn_id:
                    state["current_message_id"] = card_id or ""
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
            state["pending_local_turn_card"] = False
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

    def _reply_help(self, chat_id: str, topic: str = "") -> None:
        normalized = (topic or "").strip().lower()
        if normalized in {"", "basic", "basics", "overview"}:
            self.bot.reply_card(chat_id, build_markdown_card("Codex 帮助", self._help_overview_text()))
            return
        if normalized in {"session", "sessions", "resume", "thread", "threads"}:
            self.bot.reply_card(chat_id, build_markdown_card("Codex 帮助：线程", self._help_session_text()))
            return
        if normalized in {
            "settings",
            "permission",
            "permissions",
            "approval",
            "sandbox",
            "mode",
            "advanced",
        }:
            self.bot.reply_card(chat_id, build_markdown_card("Codex 帮助：设置", self._help_settings_text()))
            return
        if normalized in {"group", "groups", "acl"}:
            self.bot.reply_card(chat_id, build_markdown_card("Codex 帮助：群聊", self._help_group_text()))
            return
        if normalized in {"local", "fcodex", "wrapper"}:
            self.bot.reply_card(chat_id, build_markdown_card("Codex 帮助：本地继续", self._help_local_text()))
            return
        self.bot.reply(
            chat_id,
            "帮助主题仅支持：`session`、`settings`、`group`、`local`。\n发送 `/help` 查看概览。",
        )

    def _help_overview_text(self) -> str:
        return (
            "直接发送普通文本即可向当前线程提问；如果当前没有绑定线程，会在当前目录自动新建。\n\n"
            "**命令**\n"
            "- `/new` 立即新建线程\n"
            "- `/session` 查看当前目录线程\n"
            "- `/resume <thread_id|thread_name>` 恢复指定线程\n"
            "- `/cd <path>` 切换目录并清空当前线程绑定\n"
            "- `/status` 查看当前状态\n\n"
            "- `/whoami`：私聊查看自己的 `user_id` / `open_id`\n"
            "- `/whoareyou`：查看机器人自己的 `app_id` / `open_id`\n\n"
            "**更多命令与帮助**\n"
            "- `/help session` 查看线程切换、目录切换与归档\n"
            "- `/help settings` 查看 profile、权限与协作设置\n"
            "- `/help group` 查看群聊工作态、授权策略与上下文规则\n"
            "- `/help local` 查看本地 `fcodex` 的用法\n\n"
            f"{_LOCAL_THREAD_SAFETY_RULE}"
        )

    def _help_session_text(self) -> str:
        return (
            "**线程相关**\n"
            "- `/new` 立即新建并切换到新线程。\n"
            "- `/session` 只列当前目录的线程，结果已跨 provider 汇总。\n"
            "- `/resume <thread_id|thread_name>` 会做全局精确匹配；恢复后会切到线程自己的目录。\n"
            "- 如果匹配到多个同名线程，`/resume` 会报错，不会替你猜。\n"
            "- `/cd <path>` 切换目录并清空当前线程绑定；之后发送普通文本，会在新目录自动新建线程。\n"
            "- `/rename` 改标题，`/star` 收藏当前线程，`/rm` 归档线程而不是硬删除。\n\n"
            "**本地继续同一线程**\n"
            "- 可先用 `fcodex /session` 找线程；需要精确恢复时再用 `fcodex /resume`。\n"
            f"- {_LOCAL_THREAD_SAFETY_RULE}"
        )

    def _help_settings_text(self) -> str:
        return (
            "**设置相关**\n"
            "- `/profile` 查看或切换默认 profile；它影响 feishu-codex 与新的默认 `fcodex` 启动，不热切换已打开的 `fcodex` TUI。\n"
            "- 推荐先用 `/permissions`；它会同时设置审批策略和沙箱，只影响当前飞书会话的后续 turn。\n"
            "- `/approval` 只改审批时机；`/sandbox` 只改文件与网络边界。\n"
            "- `/mode` 切换协作方式；`plan` 更容易先规划或提问，`default` 更接近直接执行；也只影响当前飞书会话的后续 turn。\n"
            "- 如果当前正在执行，新设置从下一轮生效。\n\n"
            "**命令**\n"
            "- `/profile [name]`\n"
            "- `/permissions [read-only|default|full-access]`\n"
            "- `/approval [untrusted|on-failure|on-request|never]`\n"
            "- `/sandbox [read-only|workspace-write|danger-full-access]`\n"
            "- `/mode [default|plan]`"
        )

    def _help_group_text(self) -> str:
        return (
            "**群聊相关**\n"
            "- `/groupmode` 查看当前群聊工作态；管理员可切到 `assistant`、`all`、`mention-only`。\n"
            "- `assistant` 会缓存群聊消息，仅在人类 `@机器人` 时回复；每次有效 `@` 都会回捞最近群历史，把两次 `@` 之间的消息补齐进上下文。\n"
            "- `/acl` 查看当前群授权；管理员可设置 `admin-only`、`allowlist`、`all-members`。\n"
            "- 在群聊 `assistant` 和 `mention-only` 工作态下，群命令本身也需要先 `@机器人`；私聊则不需要。\n"
            "- 群命令不写入 `assistant` 上下文日志，也不会推进上下文边界。\n"
            "- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效 `@` 时额外回捞群历史，用来补齐其他机器人和遗漏消息。\n"
            "- 未获授权成员在 `all` 模式下直接发普通消息会静默忽略；只有显式 `@机器人` 或发群命令时才会收到拒绝提示。\n"
            "- 管理员授权成员时，推荐在群里直接 `@成员` 使用 `/acl grant` 或 `/acl revoke`。\n\n"
            "**命令**\n"
            "- `/groupmode [assistant|all|mention-only]`\n"
            "- `/acl`\n"
            "- `/acl policy <admin-only|allowlist|all-members>`\n"
            "- `/acl grant @成员`\n"
            "- `/acl revoke @成员`"
        )

    def _help_local_text(self) -> str:
        return (
            "**本地继续线程时再用 `fcodex`**\n"
            "- `fcodex` 是 `codex --remote` 的 wrapper，默认连到 feishu-codex 的 shared backend。\n"
            "- `fcodex /session`、`fcodex /resume <thread_id|thread_name>` 会用共享发现逻辑，跨 provider 找线程。\n"
            "- 进入 TUI 后，里面的 `/resume` 是 Codex 原生命令，不等同于 `fcodex /resume`。\n"
            "- 只想开独立的本地会话，直接用裸 `codex`。\n"
            f"- {_LOCAL_THREAD_SAFETY_RULE}"
        )
