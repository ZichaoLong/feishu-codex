"""
Codex 飞书处理器。
"""

from __future__ import annotations

import atexit
import json
import logging
import pathlib
from secrets import compare_digest
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, TypeAlias
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
    build_help_dashboard_card,
    build_help_topic_actions_card,
    build_help_topic_card,
    build_file_change_approval_card,
    build_history_preview_card,
    build_markdown_card,
    build_plan_card,
    build_permissions_preset_card,
    build_permissions_approval_card,
    build_resume_guard_card,
    build_resume_guard_handled_card,
    build_rename_card,
    build_sandbox_policy_card,
    build_sessions_card,
    build_sessions_closed_card,
    build_sessions_pending_card,
    build_thread_snapshot_card,
)
from bot.config import (
    ensure_init_token,
    load_config_file,
    load_system_config_raw,
    save_system_config,
)
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
from bot.feishu_types import GroupAclSnapshot, MessageContextPayload
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
_GROUP_SHARED_STATE_KEY = "__group__"
StateBinding: TypeAlias = tuple[str, str]
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


@dataclass(frozen=True)
class _CommandRoute:
    handler: Callable[[str, str, str, str], None]
    scope: str = "any"
    admin_only_in_group: bool = True
    scope_denied_text: str = ""


@dataclass(frozen=True)
class _ActionRoute:
    handler: Callable[[str, str, str, dict[str, Any]], P2CardActionTriggerResponse]
    group_guard: str = "none"


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
        self._states: dict[StateBinding, dict[str, Any]] = {}
        self._thread_bindings: dict[str, StateBinding] = {}
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
        self._command_routes = self._build_command_routes()
        self._action_routes = self._build_action_routes()
        self._prefixed_action_routes = self._build_prefixed_action_routes()
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

    def handle_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        cleaned = (text or "").strip()
        with self._lock:
            if not state["active"]:
                state["active"] = True

        if not cleaned or cleaned.upper() == KEYWORD:
            self._reply_help(chat_id)
            return

        if cleaned.startswith("/"):
            self._handle_command(sender_id, chat_id, cleaned, message_id=message_id)
            return

        self._handle_prompt(sender_id, chat_id, cleaned, message_id=message_id)

    def handle_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        is_group_chat = self._is_group_chat(chat_id, message_id)
        action = action_value.get("action", "")
        if not action:
            rename_fallback = self._handle_rename_form_fallback(sender_id, chat_id, message_id, action_value)
            if rename_fallback is not None:
                return rename_fallback
            fallback = self._handle_user_input_form_fallback(sender_id, chat_id, message_id, action_value)
            if fallback is not None:
                return fallback
            form_value = action_value.get("_form_value") or {}
            if isinstance(form_value, dict) and form_value:
                return self.bot.make_card_response(
                    toast="表单已失效或未找到对应问题，请重新触发该请求。",
                    toast_type="warning",
                )
        route = self._action_routes.get(action)
        if route is None:
            for prefix, prefixed_route in self._prefixed_action_routes:
                if action.startswith(prefix):
                    route = prefixed_route
                    break
        if route is None:
            return P2CardActionTriggerResponse()
        denied = self._check_action_group_guard(
            route,
            is_group_chat=is_group_chat,
            chat_id=chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
            action_value=action_value,
        )
        if denied is not None:
            return denied
        return route.handler(sender_id, chat_id, message_id, action_value)

    def _handle_user_input_form_fallback(
        self,
        sender_id: str,
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
        if self._is_group_chat(chat_id, message_id) and not self._is_group_request_actor_or_admin(
            chat_id,
            request_key=request_key,
            pending=pending,
            message_id=message_id,
            operator_open_id=str(action_value.get("_operator_open_id", "")).strip(),
        ):
            return self.bot.make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
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
        sender_id: str,
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
        if self._is_group_chat(chat_id, message_id) and not self._is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=str(action_value.get("_operator_open_id", "")).strip(),
        ):
            return self.bot.make_card_response(
                toast="仅管理员可操作群共享会话或群设置。",
                toast_type="warning",
            )

        payload = dict(action_value)
        payload["action"] = "rename_thread"
        payload["thread_id"] = pending["thread_id"]
        return self._handle_rename_submit_action(sender_id, chat_id, message_id, payload)

    def is_sender_active(self, sender_id: str, chat_id: str = "", message_id: str = "") -> bool:
        return self._get_state(sender_id, chat_id, message_id).get("active", False)

    def deactivate_sender(self, sender_id: str, chat_id: str = "", message_id: str = "") -> None:
        key = self._state_binding(sender_id, chat_id, message_id)
        with self._lock:
            state = self._states.pop(key, None)
            if not state:
                return
            thread_id = state.get("current_thread_id", "")
            if thread_id and self._thread_bindings.get(thread_id) == key:
                self._thread_bindings.pop(thread_id, None)

    def shutdown(self) -> None:
        """停止底层 app-server。"""
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("停止 Codex adapter 失败")

    def _build_default_state(self) -> dict[str, Any]:
        return {
            "active": False,
            "working_dir": self._default_working_dir,
            "current_thread_id": "",
            "current_thread_name": "",
            "current_turn_id": "",
            "running": False,
            "cancelled": False,
            "pending_cancel": False,
            "current_message_id": "",
            "current_prompt_message_id": "",
            "current_actor_open_id": "",
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

    def _existing_state_binding_locked(self, sender_id: str, chat_id: str) -> StateBinding | None:
        group_binding = (_GROUP_SHARED_STATE_KEY, chat_id)
        if group_binding in self._states:
            return group_binding
        sender_binding = (sender_id, chat_id)
        if sender_binding in self._states:
            return sender_binding
        return None

    def _get_state(self, sender_id: str, chat_id: str, message_id: str = "") -> dict[str, Any]:
        with self._lock:
            existing = self._existing_state_binding_locked(sender_id, chat_id)
            if existing is not None:
                return self._states[existing]
        key = self._state_binding(sender_id, chat_id, message_id)
        with self._lock:
            existing = self._existing_state_binding_locked(sender_id, chat_id)
            if existing is not None:
                return self._states[existing]
            if key not in self._states:
                self._states[key] = self._build_default_state()
            return self._states[key]

    def _resolve_chat_type(self, chat_id: str, message_id: str = "") -> str:
        context = self.bot.get_message_context(message_id) if message_id else {}
        chat_type = str(context.get("chat_type", "")).strip()
        if chat_type:
            return chat_type
        chat_type = str(self.bot.lookup_chat_type(chat_id) or "").strip()
        if chat_type:
            return chat_type
        chat_type = str(self.bot.fetch_runtime_chat_type(chat_id) or "").strip()
        if chat_type:
            return chat_type
        return ""

    def _is_group_chat(self, chat_id: str, message_id: str = "") -> bool:
        return self._resolve_chat_type(chat_id, message_id) == "group"

    def _state_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> StateBinding:
        if sender_id == _GROUP_SHARED_STATE_KEY:
            return (_GROUP_SHARED_STATE_KEY, chat_id)
        with self._lock:
            existing = self._existing_state_binding_locked(sender_id, chat_id)
            if existing is not None:
                return existing
        if self._is_group_chat(chat_id, message_id):
            return (_GROUP_SHARED_STATE_KEY, chat_id)
        return (sender_id, chat_id)

    def _group_actor_open_id(self, message_id: str = "", operator_open_id: str = "") -> str:
        normalized_operator_open_id = str(operator_open_id or "").strip()
        if normalized_operator_open_id:
            return normalized_operator_open_id
        if not message_id:
            return ""
        context = self.bot.get_message_context(message_id)
        return str(context.get("sender_open_id", "")).strip()

    def _is_group_admin_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        if not self._is_group_chat(chat_id, message_id):
            return True
        actor_open_id = self._group_actor_open_id(message_id, operator_open_id)
        return self.bot.is_group_admin(open_id=actor_open_id)

    def _ensure_group_command_admin(self, chat_id: str, message_id: str = "") -> bool:
        if not self._is_group_chat(chat_id, message_id):
            return True
        if self._is_group_admin_actor(chat_id, message_id=message_id):
            return True
        self._reply_text(
            chat_id,
            "群里的 `/` 命令仅管理员可用；已授权成员请直接提问或显式 mention 触发机器人。",
            message_id=message_id,
        )
        return False

    def _ensure_command_scope(self, route: _CommandRoute, chat_id: str, message_id: str = "") -> bool:
        if route.scope == "any":
            return True
        chat_type = self._resolve_chat_type(chat_id, message_id)
        if route.scope == "group" and chat_type == "group":
            return True
        if route.scope == "p2p" and chat_type != "group":
            return True
        denied_text = route.scope_denied_text
        if not denied_text:
            if route.scope == "group":
                denied_text = "该命令仅支持群聊使用。"
            else:
                denied_text = "该命令仅支持私聊使用。"
        self._reply_text(chat_id, denied_text, message_id=message_id)
        return False

    def _is_group_turn_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        if not self._is_group_chat(chat_id, message_id):
            return True
        if self._is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
        ):
            return True
        state = self._get_state(_GROUP_SHARED_STATE_KEY, chat_id, message_id)
        actor_open_id = self._group_actor_open_id(message_id, operator_open_id)
        with self._lock:
            current_actor_open_id = str(state.get("current_actor_open_id", "")).strip()
        return bool(current_actor_open_id and actor_open_id and current_actor_open_id == actor_open_id)

    def _is_group_request_actor_or_admin(
        self,
        chat_id: str,
        *,
        request_key: str,
        pending: dict[str, Any] | None = None,
        message_id: str = "",
        operator_open_id: str = "",
    ) -> bool:
        if not self._is_group_chat(chat_id, message_id):
            return True
        if self._is_group_admin_actor(
            chat_id,
            message_id=message_id,
            operator_open_id=operator_open_id,
        ):
            return True
        request = pending
        if request is None:
            with self._lock:
                request = self._pending_requests.get(request_key)
        if not request:
            return False
        actor_open_id = self._group_actor_open_id(message_id, operator_open_id)
        request_actor_open_id = str(request.get("actor_open_id", "")).strip()
        return bool(request_actor_open_id and actor_open_id and request_actor_open_id == actor_open_id)

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None:
        if self._is_group_chat(chat_id, message_id) and message_id:
            self.bot.reply(chat_id, text, parent_message_id=message_id)
            return
        self.bot.reply(chat_id, text)

    def _reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None:
        if self._is_group_chat(chat_id, message_id) and message_id:
            self.bot.reply_card(chat_id, card, parent_message_id=message_id)
            return
        self.bot.reply_card(chat_id, card)

    def _group_member_label(self, open_id: str) -> str:
        normalized_open_id = str(open_id or "").strip()
        if not normalized_open_id:
            return "unknown"
        display_name = self.bot.get_sender_display_name(open_id=normalized_open_id, sender_type="user")
        normalized_name = str(display_name or "").strip()
        if normalized_name and normalized_name not in {normalized_open_id, normalized_open_id[:8]}:
            return normalized_name
        return normalized_open_id

    def _group_member_labels(self, open_ids: list[str] | set[str]) -> list[str]:
        normalized_open_ids = sorted({str(item).strip() for item in open_ids if str(item).strip()})
        return [self._group_member_label(open_id) for open_id in normalized_open_ids]

    def _build_command_routes(self) -> dict[str, _CommandRoute]:
        return {
            "/help": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/h": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/init": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_init_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/init <token>`。",
            ),
            "/pwd": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._reply_text(
                    chat_id,
                    f"当前目录：`{display_path(self._get_state(sender_id, chat_id, message_id)['working_dir'])}`",
                    message_id=message_id,
                ),
            ),
            "/cd": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_cd_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/new": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_new_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/status": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_status_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/whoami": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_whoami_command(
                    sender_id, chat_id, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/whoami`。",
            ),
            "/whoareyou": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_botinfo_command(
                    chat_id, message_id=message_id
                ),
            ),
            "/profile": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_profile_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/cancel": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._cancel_current_turn(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/session": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_session_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/resume": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_resume_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/rm": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_rm_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/rename": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_rename_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/star": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_star_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/approval": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_approval_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/sandbox": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_sandbox_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/permissions": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_permissions_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/mode": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_mode_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/groupmode": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_groupmode_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="group",
            ),
            "/acl": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_acl_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="group",
            ),
        }

    def _build_action_routes(self) -> dict[str, _ActionRoute]:
        return {
            "cancel_turn": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_cancel_action(
                    sender_id, chat_id
                ),
                group_guard="turn_actor",
            ),
            "resume_thread": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_resume_thread_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "preview_thread_snapshot": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_preview_thread_snapshot_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "resume_thread_write": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_resume_thread_write_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "cancel_resume_guard": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_cancel_resume_guard_action(
                    sender_id, chat_id, message_id, action_value
                ),
            ),
            "close_sessions_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_close_sessions_card_action(),
                group_guard="group_admin",
            ),
            "reopen_sessions_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_reopen_sessions_card_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "show_help_topic": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_help_topic_action(
                    action_value
                ),
            ),
            "show_help_overview": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_help_overview_action(),
            ),
            "show_permissions_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_permissions_card_action(
                    sender_id, chat_id
                ),
                group_guard="group_admin",
            ),
            "show_mode_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_mode_card_action(
                    sender_id, chat_id
                ),
                group_guard="group_admin",
            ),
            "show_group_mode_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_group_mode_card_action(
                    chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "toggle_star_thread": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_toggle_star_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "archive_thread": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_archive_thread_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "show_rename_form": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_show_rename_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "rename_thread": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_rename_submit_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "cancel_rename": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_cancel_rename_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "set_approval_policy": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_approval_policy(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_sandbox_policy": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_sandbox_policy(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_permissions_preset": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_permissions_preset(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_collaboration_mode": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_collaboration_mode(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_group_mode": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_group_mode_action(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_group_acl_policy": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_set_group_acl_policy_action(
                    sender_id, chat_id, action_value
                ),
                group_guard="group_admin",
            ),
        }

    def _build_prefixed_action_routes(self) -> list[tuple[str, _ActionRoute]]:
        approval_route = _ActionRoute(
            handler=lambda sender_id, chat_id, message_id, action_value: self._handle_approval_card_action(
                action_value
            ),
            group_guard="approval_admin",
        )
        return [
            ("command_", approval_route),
            ("file_change_", approval_route),
            ("permissions_", approval_route),
            (
                "answer_user_input_",
                _ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: self._handle_user_input_action(
                        action_value
                    ),
                    group_guard="request_actor_or_admin",
                ),
            ),
        ]

    def _check_action_group_guard(
        self,
        route: _ActionRoute,
        *,
        is_group_chat: bool,
        chat_id: str,
        message_id: str,
        operator_open_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        if not is_group_chat or route.group_guard == "none":
            return None
        if route.group_guard == "group_admin":
            if self._is_group_admin_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return self.bot.make_card_response(
                toast="仅管理员可操作群共享会话或群设置。",
                toast_type="warning",
            )
        if route.group_guard == "turn_actor":
            if self._is_group_turn_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return self.bot.make_card_response(
                toast="仅管理员或当前提问者可停止当前群聊执行。",
                toast_type="warning",
            )
        if route.group_guard == "approval_admin":
            if self._is_group_admin_actor(
                chat_id,
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return self.bot.make_card_response(
                toast="仅管理员可审批群共享会话请求。",
                toast_type="warning",
            )
        if route.group_guard == "request_actor_or_admin":
            if self._is_group_request_actor_or_admin(
                chat_id,
                request_key=str(action_value.get("request_id", "")).strip(),
                message_id=message_id,
                operator_open_id=operator_open_id,
            ):
                return None
            return self.bot.make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
        logger.warning("未知卡片群权限守卫: %s", route.group_guard)
        return self.bot.make_card_response(
            toast="当前卡片动作配置异常。",
            toast_type="warning",
        )

    def _handle_init_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> None:
        context = self.bot.get_message_context(message_id) if message_id else {}
        provided_token = str(arg or "").strip()
        if not provided_token:
            self._reply_text(
                chat_id,
                "用法：`/init <token>`\n`token` 默认保存在本机配置目录的 `init.token` 文件。",
                message_id=message_id,
            )
            return
        expected_token = ensure_init_token()
        if not compare_digest(provided_token, expected_token):
            self._reply_text(
                chat_id,
                "初始化口令错误。请检查本机配置目录中的 `init.token`。",
                message_id=message_id,
            )
            return
        sender_open_id = str(context.get("sender_open_id", "") or "").strip()
        sender_user_id = str(context.get("sender_user_id", "") or "").strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        if not sender_open_id:
            self._reply_text(
                chat_id,
                "初始化失败：当前消息上下文里没有发送者 `open_id`，暂时无法写入管理员配置。",
                message_id=message_id,
            )
            return
        sender_name = self.bot.get_sender_display_name(
            user_id=sender_user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        config = load_system_config_raw()
        admin_open_ids = {
            str(item).strip()
            for item in config.get("admin_open_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        admin_open_ids.update(self.bot.list_admin_open_ids())
        admin_added = sender_open_id not in admin_open_ids
        admin_open_ids.add(sender_open_id)
        configured_bot_open_id = str(config.get("bot_open_id", "") or "").strip()
        identity = self.bot.get_bot_identity_snapshot()
        discovered_bot_open_id = str(identity.get("discovered_open_id", "") or "").strip()
        bot_open_id_written = False
        if discovered_bot_open_id and discovered_bot_open_id != configured_bot_open_id:
            configured_bot_open_id = discovered_bot_open_id
            bot_open_id_written = True

        updated_config = dict(config)
        updated_config["admin_open_ids"] = sorted(admin_open_ids)
        if configured_bot_open_id:
            updated_config["bot_open_id"] = configured_bot_open_id

        try:
            save_system_config(updated_config)
        except Exception as exc:
            logger.exception("保存初始化配置失败")
            self._reply_text(chat_id, f"初始化失败：保存配置时出错：{exc}", message_id=message_id)
            return

        self.bot.add_admin_open_id(sender_open_id)
        if configured_bot_open_id:
            self.bot.set_configured_bot_open_id(configured_bot_open_id)

        lines = [
            "初始化结果：",
            (
                f"- admin_open_ids：已加入 `{sender_name}`"
                if admin_added
                else f"- admin_open_ids：`{sender_name}` 已在管理员列表中"
            ),
        ]
        if configured_bot_open_id:
            lines.append(
                f"- bot_open_id：`{configured_bot_open_id}`"
                + ("（本次已写入）" if bot_open_id_written else "（保持不变）")
            )
        else:
            lines.extend(
                [
                    "- bot_open_id：未写入",
                    "- 请检查 `application:application:self_manage` 权限后重试 `/init <token>`，或手动填写 `system.yaml.bot_open_id`。",
                ]
            )
        lines.append("- 当前命令只会更新管理员和 bot open id，不会改动 `trigger_open_ids`。")
        self._reply_text(chat_id, "\n".join(lines), message_id=message_id)

    def _handle_command(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        command, _, arg = text.partition(" ")
        arg = arg.strip()
        cmd = command.lower()
        route = self._command_routes.get(cmd)
        if route is None:
            self._reply_text(chat_id, f"未知命令：`{command}`\n发送 `/help` 查看可用命令。", message_id=message_id)
            return
        # 先做 scope guard，保证群/私聊专属命令优先返回精确拒绝文本；
        # 只有 scope 允许通过后，才需要进入“群里是否仅管理员可用”的判断。
        if not self._ensure_command_scope(route, chat_id, message_id):
            return
        if route.admin_only_in_group and not self._ensure_group_command_admin(chat_id, message_id):
            return
        route.handler(sender_id, chat_id, arg, message_id)

    def _handle_prompt(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                self._reply_text(chat_id, "当前线程仍在执行，请等待结束或先执行 `/cancel`。", message_id=message_id)
                return

        try:
            thread_id = self._ensure_thread(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("创建线程失败")
            self._reply_text(chat_id, f"创建线程失败：{exc}", message_id=message_id)
            return

        with self._lock:
            state["running"] = True
            state["cancelled"] = False
            state["pending_cancel"] = False
            state["current_turn_id"] = ""
            state["current_prompt_message_id"] = str(message_id or "").strip()
            state["current_actor_open_id"] = self._group_actor_open_id(message_id)
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
            start_response = self._adapter.start_turn(
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
                state["pending_cancel"] = False
                state["full_reply_text"] = f"启动失败：{exc}"
            self._flush_execution_card(sender_id, chat_id, immediate=True)
            if not card_id:
                self._reply_text(chat_id, f"启动失败：{exc}", message_id=message_id)
            return

        turn_id = self._extract_turn_id_from_start_response(start_response)
        should_interrupt_started_turn = False
        with self._lock:
            if turn_id and not state["current_turn_id"]:
                state["current_turn_id"] = turn_id
            if turn_id and state["pending_cancel"]:
                should_interrupt_started_turn = True
        if should_interrupt_started_turn:
            try:
                self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
            except Exception:
                logger.exception("延迟取消 turn 失败")
            else:
                with self._lock:
                    state["pending_cancel"] = False

    def _handle_cd_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                self._reply_card(
                    chat_id,
                    build_markdown_card(
                        "Codex 目录未切换",
                        "执行中不能切换目录，请等待结束或先停止当前执行。",
                        template="orange",
                    ),
                    message_id=message_id,
                )
                return

        if not arg:
            self._reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 当前目录",
                    f"当前目录：`{display_path(state['working_dir'])}`",
                ),
                message_id=message_id,
            )
            return

        target = resolve_working_dir(arg, fallback=state["working_dir"])
        if not pathlib.Path(target).exists():
            self._reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 目录未切换",
                    f"目录不存在：`{display_path(target)}`",
                    template="orange",
                ),
                message_id=message_id,
            )
            return
        if not pathlib.Path(target).is_dir():
            self._reply_card(
                chat_id,
                build_markdown_card(
                    "Codex 目录未切换",
                    f"不是目录：`{display_path(target)}`",
                    template="orange",
                ),
                message_id=message_id,
            )
            return

        self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        with self._lock:
            state["working_dir"] = target
        self._reply_card(
            chat_id,
            build_markdown_card(
                "Codex 目录已切换",
                (
                    f"目录：`{display_path(target)}`\n"
                    "当前线程绑定已清空。\n"
                    "直接发送普通文本，会在新目录自动新建线程。"
                ),
            ),
            message_id=message_id,
        )

    def _handle_new_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                self._reply_text(chat_id, "执行中不能新建线程，请等待结束或先执行 `/cancel`。", message_id=message_id)
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
            self._reply_text(chat_id, f"新建线程失败：{exc}", message_id=message_id)
            return
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        self._reply_card(
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
            message_id=message_id,
        )

    def _handle_status_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
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
        self._reply_card(
            chat_id,
            build_markdown_card("Codex 当前状态", content, template=template),
            message_id=message_id,
        )

    def _handle_whoami_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        context = self.bot.get_message_context(message_id) if message_id else {}
        sender_user_id = str(context.get("sender_user_id", "")).strip()
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        sender_type = str(context.get("sender_type", "user") or "user").strip()
        name = self.bot.get_sender_display_name(
            user_id=sender_user_id,
            open_id=sender_open_id,
            sender_type=sender_type,
        )
        self._reply_text(
            chat_id,
            "\n".join(
                [
                    "你的身份信息：",
                    f"- name: `{name}`",
                    f"- user_id: `{sender_user_id or '（空）'}`",
                    f"- open_id: `{sender_open_id or '（空）'}`",
                    "",
                    "配置管理员时，把 `open_id` 写进 `system.yaml` 的 `admin_open_ids`。",
                    "其中 `user_id` 仅用于排障；若未开 `contact:user.employee_id:readonly`，这里允许为空。",
                ]
            ),
            message_id=message_id,
        )

    def _handle_botinfo_command(self, chat_id: str, *, message_id: str = "") -> None:
        identity = self.bot.get_bot_identity_snapshot()
        configured_open_id = str(identity.get("configured_open_id", "") or "").strip()
        discovered_open_id = str(identity.get("discovered_open_id", "") or "").strip()
        trigger_open_ids = [
            str(item).strip()
            for item in (identity.get("trigger_open_ids") or [])
            if str(item).strip()
        ]
        lines = [
            "机器人身份信息：",
            f"- app_id: `{identity.get('app_id', '') or '（空）'}`",
            f"- configured bot_open_id: `{configured_open_id or '（空）'}`",
            f"- discovered open_id: `{discovered_open_id or '（空）'}`",
            f"- runtime mention matching: `{'enabled' if configured_open_id else 'disabled'}`",
            f"- trigger_open_ids: `{', '.join(trigger_open_ids) or '（空）'}`",
            "- 运行时权威值：`system.yaml.bot_open_id`",
        ]
        if configured_open_id and discovered_open_id and configured_open_id != discovered_open_id:
            lines.extend(
                [
                    "",
                    "警告：",
                    "- 当前运行时仍只按 `system.yaml.bot_open_id` 判定 mention；实时探测值仅用于诊断和初始化。",
                    "- 当前配置值与实时探测值不一致，请优先核对 `system.yaml.bot_open_id` 是否写错。",
                ]
            )
        if not configured_open_id:
            lines.extend(
                [
                    "",
                    "建议：",
                    (
                        f"- 直接执行 `/init <token>` 自动写入，或手动把 `{discovered_open_id}` 写进 `system.yaml.bot_open_id`"
                        if discovered_open_id
                        else "- 先让 `/whoareyou` 能看到 `discovered open_id`，再手动写入 `system.yaml.bot_open_id`；如需自动写入，再执行 `/init <token>`"
                    ),
                    "- 运行时只有 `system.yaml.bot_open_id` 会参与群聊 mention 判定；`/whoareyou` 的实时探测结果不会自动生效。",
                    "- 如需让“别人 @你本人时由机器人代答”，再把对应人的 open_id 写进 `system.yaml.trigger_open_ids`",
                    "- 如果 `discovered open_id` 为空，检查 `application:application:self_manage` 权限",
                ]
            )
        self._reply_text(chat_id, "\n".join(lines), message_id=message_id)

    def _handle_session_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("获取线程列表失败")
            self._reply_text(chat_id, f"获取线程列表失败：{exc}", message_id=message_id)
            return
        self._reply_card(chat_id, card, message_id=message_id)

    def _handle_resume_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                self._reply_text(chat_id, "执行中不能切换线程，请等待结束或先执行 `/cancel`。", message_id=message_id)
                return
        if not arg:
            self._reply_text(
                chat_id,
                "用法：`/resume <thread_id 或 thread_name>`\n发送 `/help session` 查看 `/session` 与 `/resume` 的区别。",
                message_id=message_id,
            )
            return
        try:
            thread = self._resolve_resume_target(arg)
        except Exception as exc:
            logger.exception("解析恢复目标失败")
            self._reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            return
        if self._is_loaded_in_current_backend(thread):
            self._resume_thread_in_background(
                sender_id,
                chat_id,
                thread.thread_id,
                original_arg=arg,
                summary=thread,
                message_id=message_id,
            )
            return
        self._reply_card(chat_id, self._build_resume_guard(thread), message_id=message_id)

    def _handle_profile_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        runtime_config = self._safe_read_runtime_config()
        if runtime_config is None:
            self._reply_text(chat_id, "读取 Codex 运行时配置失败，无法查看或切换 profile。", message_id=message_id)
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
            self._reply_card(
                chat_id,
                build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
                message_id=message_id,
            )
            return

        target_profile = arg.strip()
        if target_profile not in profiles:
            self._reply_text(
                chat_id,
                f"未找到 profile：`{target_profile}`\n用法：`/profile <name>`\n先发 `/profile` 查看可用 profile。",
                message_id=message_id,
            )
            return

        try:
            self._profile_state.save_default_profile(target_profile)
        except Exception as exc:
            logger.exception("保存 feishu-codex 默认 profile 失败")
            self._reply_text(chat_id, f"切换 profile 失败：{exc}", message_id=message_id)
            return

        state = self._get_state(sender_id, chat_id, message_id)
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
        self._reply_card(
            chat_id,
            build_markdown_card("Codex 默认 Profile", "\n".join(lines)),
            message_id=message_id,
        )

    def _handle_rename_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if not state["current_thread_id"]:
            self._reply_text(chat_id, "当前没有绑定线程，无法重命名。", message_id=message_id)
            return
        if not arg:
            self._reply_text(chat_id, "用法：`/rename <新标题>`", message_id=message_id)
            return
        try:
            self._adapter.rename_thread(state["current_thread_id"], arg)
        except Exception as exc:
            logger.exception("重命名线程失败")
            self._reply_text(chat_id, f"重命名失败：{exc}", message_id=message_id)
            return
        with self._lock:
            state["current_thread_name"] = arg
        self._reply_text(chat_id, f"已重命名为：{arg}", message_id=message_id)

    def _handle_rm_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                self._reply_text(chat_id, "执行中不能归档线程，请等待结束或先执行 `/cancel`。", message_id=message_id)
                return
        target = arg.strip() if arg else ""
        if target:
            try:
                thread = self._resolve_resume_target(target)
            except Exception as exc:
                logger.exception("解析归档目标失败")
                self._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
                return
        else:
            if not state["current_thread_id"]:
                self._reply_text(
                    chat_id,
                    "用法：`/rm [thread_id 或 thread_name]`；省略参数时归档当前线程。",
                    message_id=message_id,
                )
                return
            try:
                thread = self._read_thread_summary(state["current_thread_id"], original_arg=state["current_thread_id"])
            except Exception as exc:
                logger.exception("读取当前线程失败")
                self._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
                return

        try:
            self._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            self._reply_text(chat_id, f"归档线程失败：{exc}", message_id=message_id)
            return

        self._favorites.remove_thread_globally(thread.thread_id)
        if state["current_thread_id"] == thread.thread_id:
            self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        self._reply_text(
            chat_id,
            (
                f"已归档线程：`{thread.thread_id[:8]}…` {thread.title}\n"
                "说明：这里调用的是 Codex 的线程归档（archive），会从常规列表中隐藏，不是硬删除。"
            ),
            message_id=message_id,
        )

    def _handle_star_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if not state["current_thread_id"]:
            self._reply_text(chat_id, "当前没有绑定线程，无法收藏。", message_id=message_id)
            return
        starred = self._favorites.toggle(sender_id, state["current_thread_id"])
        self._reply_text(chat_id, "已收藏当前线程。" if starred else "已取消收藏当前线程。", message_id=message_id)

    def _handle_approval_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in _APPROVAL_POLICIES:
                self._reply_text(
                    chat_id,
                    "审批策略仅支持：`untrusted`、`on-failure`、`on-request`、`never`",
                    message_id=message_id,
                )
                return
            with self._lock:
                state["approval_policy"] = policy
                running = state["running"]
            message = f"已切换审批策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self._reply_text(chat_id, message, message_id=message_id)
            return
        self._reply_card(
            chat_id,
            build_approval_policy_card(state["approval_policy"], running=state["running"]),
            message_id=message_id,
        )

    def _handle_sandbox_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if arg:
            policy = arg.strip().lower()
            if policy not in _SANDBOX_POLICIES:
                self._reply_text(
                    chat_id,
                    "沙箱策略仅支持：`read-only`、`workspace-write`、`danger-full-access`",
                    message_id=message_id,
                )
                return
            with self._lock:
                state["sandbox"] = policy
                running = state["running"]
            message = f"已切换沙箱策略：`{policy}`\n作用范围：只影响当前飞书会话的后续 turn。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self._reply_text(chat_id, message, message_id=message_id)
            return
        self._reply_card(
            chat_id,
            build_sandbox_policy_card(state["sandbox"], running=state["running"]),
            message_id=message_id,
        )

    def _handle_permissions_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if arg:
            preset = arg.strip().lower()
            config = _PERMISSIONS_PRESETS.get(preset)
            if config is None:
                self._reply_text(
                    chat_id,
                    "权限预设仅支持：`read-only`、`default`、`full-access`",
                    message_id=message_id,
                )
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
            self._reply_text(chat_id, message, message_id=message_id)
            return
        self._reply_card(
            chat_id,
            build_permissions_preset_card(
                state["approval_policy"],
                state["sandbox"],
                running=state["running"],
            ),
            message_id=message_id,
        )

    def _handle_mode_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        if arg:
            mode = arg.strip().lower()
            if mode not in {"default", "plan"}:
                self._reply_text(chat_id, "协作模式仅支持：`default`、`plan`", message_id=message_id)
                return
            with self._lock:
                state["collaboration_mode"] = mode
                running = state["running"]
            message = f"已切换协作模式：`{mode}`\n作用范围：只影响当前飞书会话的后续 turn，不影响已打开的 `fcodex` TUI。"
            if running:
                message += "\n如果当前正在执行，新设置从下一轮生效。"
            self._reply_text(chat_id, message, message_id=message_id)
            return
        self._reply_card(
            chat_id,
            build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            ),
            message_id=message_id,
        )

    def _group_command_context(self, message_id: str = "") -> MessageContextPayload:
        """Return message context for a command that has already passed group scope checks."""
        context = self.bot.get_message_context(message_id) if message_id else {}
        if context:
            return context
        return {"chat_type": "group"}

    @staticmethod
    def _normalize_group_mode(mode: str) -> str:
        normalized = str(mode or "").strip().lower().replace("-", "_")
        if normalized == "mention":
            return "mention_only"
        return normalized

    def _group_mode_card(self, chat_id: str, *, open_id: str = "") -> dict:
        return build_group_mode_card(
            self.bot.get_group_mode(chat_id),
            can_manage=self.bot.is_group_admin(open_id=open_id),
        )

    def _group_acl_card(self, chat_id: str, *, open_id: str = "") -> dict:
        snapshot: GroupAclSnapshot = self.bot.get_group_acl_snapshot(chat_id)
        return build_group_acl_card(
            snapshot["access_policy"],
            allowlist_members=self._group_member_labels(snapshot["allowlist"]),
            viewer_allowed=self.bot.is_group_user_allowed(chat_id, open_id=open_id),
            can_manage=self.bot.is_group_admin(open_id=open_id),
        )

    def _handle_groupmode_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        context = self._group_command_context(message_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self._reply_card(
                chat_id,
                self._group_mode_card(chat_id, open_id=sender_open_id),
                message_id=message_id,
            )
            return
        mode = self._normalize_group_mode(arg)
        if mode not in {"assistant", "all", "mention_only"}:
            self._reply_text(chat_id, "群聊工作态仅支持：`assistant`、`all`、`mention-only`", message_id=message_id)
            return
        self.bot.set_group_mode(chat_id, mode)
        labels = {
            "assistant": "assistant",
            "all": "all",
            "mention_only": "mention-only",
        }
        self._reply_text(chat_id, f"已切换群聊工作态：`{labels[mode]}`", message_id=message_id)

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

    def _handle_acl_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> None:
        context = self._group_command_context(message_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self._reply_card(
                chat_id,
                self._group_acl_card(chat_id, open_id=sender_open_id),
                message_id=message_id,
            )
            return

        cmd, _, rest = arg.partition(" ")
        subcommand = cmd.strip().lower()
        payload = rest.strip()
        if subcommand in {"admin-only", "allowlist", "all-members"}:
            payload = subcommand
            subcommand = "policy"

        if subcommand == "policy":
            policy = payload.strip().lower()
            if policy not in {"admin-only", "allowlist", "all-members"}:
                self._reply_text(
                    chat_id,
                    "用法：`/acl policy <admin-only|allowlist|all-members>`",
                    message_id=message_id,
                )
                return
            self.bot.set_group_access_policy(chat_id, policy)
            self._reply_text(chat_id, f"已切换群聊授权策略：`{policy}`", message_id=message_id)
            return

        if subcommand in {"grant", "allow"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self._reply_text(chat_id, "用法：`/acl grant @成员` 或 `/acl grant <open_id>`", message_id=message_id)
                return
            updated = self.bot.grant_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            self._reply_text(
                chat_id,
                f"已授权：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。",
                message_id=message_id,
            )
            return

        if subcommand in {"revoke", "remove"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self._reply_text(chat_id, "用法：`/acl revoke @成员` 或 `/acl revoke <open_id>`", message_id=message_id)
                return
            updated = self.bot.revoke_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            self._reply_text(
                chat_id,
                f"已撤销：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。",
                message_id=message_id,
            )
            return

        self._reply_text(
            chat_id,
            "用法：`/acl`、`/acl policy <admin-only|allowlist|all-members>`、`/acl grant @成员`、`/acl revoke @成员`",
            message_id=message_id,
        )

    def _handle_cancel_action(self, sender_id: str, chat_id: str) -> P2CardActionTriggerResponse:
        ok, message = self._cancel_current_turn(sender_id, chat_id, from_card=True)
        return self.bot.make_card_response(toast=message, toast_type="success" if ok else "warning")

    def _cancel_current_turn(
        self,
        sender_id: str,
        chat_id: str,
        *,
        from_card: bool = False,
        message_id: str = "",
    ) -> tuple[bool, str]:
        state = self._get_state(sender_id, chat_id, message_id)
        thread_id = state["current_thread_id"]
        turn_id = state["current_turn_id"]
        if not state["running"] or not thread_id:
            if not from_card:
                self._reply_text(chat_id, "当前没有正在执行的 turn。", message_id=message_id)
            return False, "当前没有正在执行的 turn。"
        if not turn_id:
            with self._lock:
                state["cancelled"] = True
                state["pending_cancel"] = True
            if not from_card:
                self._reply_text(chat_id, "已请求停止当前执行。", message_id=message_id)
            return True, "已请求停止当前执行。"
        try:
            self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            logger.exception("取消 turn 失败")
            if not from_card:
                self._reply_text(chat_id, f"取消失败：{exc}", message_id=message_id)
            return False, f"取消失败：{exc}"
        with self._lock:
            state["cancelled"] = True
            state["pending_cancel"] = False
        if not from_card:
            self._reply_text(chat_id, "已请求停止当前执行。", message_id=message_id)
        return True, "已请求停止当前执行。"

    @staticmethod
    def _extract_turn_id_from_start_response(response: Any) -> str:
        if not isinstance(response, dict):
            return ""
        turn = response.get("turn")
        if isinstance(turn, dict):
            turn_id = str(turn.get("id", "") or "").strip()
            if turn_id:
                return turn_id
        return str(response.get("turnId", "") or "").strip()

    def _interrupt_running_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._adapter.interrupt_turn(thread_id=thread_id, turn_id=turn_id)

    def _handle_toggle_star_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        starred = self._favorites.toggle(sender_id, thread_id)
        return self._handle_sessions_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast="已收藏线程。" if starred else "已取消收藏。",
        )

    def _handle_close_sessions_card_action(self) -> P2CardActionTriggerResponse:
        return self.bot.make_card_response(card=build_sessions_closed_card(), toast="已收起。", toast_type="success")

    def _handle_reopen_sessions_card_action(
        self, sender_id: str, chat_id: str, message_id: str
    ) -> P2CardActionTriggerResponse:
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已展开。")

    def _handle_resume_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(sender_id, chat_id, message_id)
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
                args=(sender_id, chat_id, thread_id),
                kwargs={
                    "original_arg": thread_id,
                    "summary": thread,
                    "message_id": message_id,
                    "refresh_session_message_id": message_id,
                },
                daemon=True,
            ).start()
            return self.bot.make_card_response(
                card=build_sessions_pending_card(thread.thread_id, title=thread.title),
                toast="正在恢复线程…",
                toast_type="success",
            )
        return self.bot.make_card_response(card=self._build_resume_guard(thread, return_to_sessions=True))

    def _handle_preview_thread_snapshot_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        return_to_sessions = bool(action_value.get("return_to_sessions"))
        thread = self._find_thread_summary(thread_id)
        if thread is None:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        threading.Thread(
            target=self._send_thread_snapshot_in_background,
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
        return self.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已选择“查看快照”",
                detail="快照会作为新消息发送；当前确认卡已结束，不会继续写入该线程。",
                template="green",
            ),
            toast="正在加载快照…",
            toast_type="success",
        )

    def _handle_resume_thread_write_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                return self.bot.make_card_response(
                    toast="执行中不能切换线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        return_to_sessions = bool(action_value.get("return_to_sessions"))
        thread = self._find_thread_summary(thread_id)
        if thread is None:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        threading.Thread(
            target=self._resume_thread_in_background,
            args=(sender_id, chat_id, thread_id),
            kwargs={
                "original_arg": thread_id,
                "message_id": message_id,
                "refresh_session_message_id": message_id if return_to_sessions else "",
            },
            daemon=True,
        ).start()
        if return_to_sessions:
            return self.bot.make_card_response(
                card=build_sessions_pending_card(thread.thread_id, title=thread.title),
                toast="正在恢复线程并继续写入…",
                toast_type="success",
            )
        return self.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已选择“恢复并继续写入”",
                detail="恢复请求已提交到 feishu-codex backend；后续结果会通过新的状态消息返回。",
                template="orange",
            ),
            toast="正在恢复线程并继续写入…",
            toast_type="success",
        )

    def _handle_show_rename_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", ""))
        try:
            session = self._find_thread_session(sender_id, chat_id, thread_id)
        except Exception as exc:
            logger.exception("查询重命名目标失败")
            return self.bot.make_card_response(toast=f"查询线程失败：{exc}", toast_type="warning")
        if not session:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        with self._lock:
            self._pending_rename_forms[message_id] = {"thread_id": thread_id}
        return self.bot.make_card_response(card=build_rename_card(session))

    def _handle_rename_submit_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
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

        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            self._pending_rename_forms.pop(message_id, None)
            if state["current_thread_id"] == thread_id:
                state["current_thread_name"] = new_title
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已重命名。")

    def _handle_cancel_rename_action(
        self, sender_id: str, chat_id: str, message_id: str
    ) -> P2CardActionTriggerResponse:
        self._clear_pending_rename_form(message_id)
        return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已取消")

    def _handle_cancel_resume_guard_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        if action_value.get("return_to_sessions"):
            return self._handle_sessions_refresh_action(sender_id, chat_id, message_id=message_id, toast="已取消")
        thread = self._find_thread_summary(thread_id)
        if thread is None:
            return self.bot.make_card_response(toast="未找到对应线程", toast_type="warning")
        return self.bot.make_card_response(
            card=self._build_resume_guard_handled(
                thread,
                decision="已取消本次恢复",
                detail="当前不会查看快照，也不会在 feishu-codex backend 中恢复该线程。",
                template="grey",
            ),
            toast="已取消。",
            toast_type="success",
        )

    def _handle_archive_thread_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                return self.bot.make_card_response(
                    toast="执行中不能归档线程，请等待结束或先执行 /cancel。",
                    toast_type="warning",
                )
        thread_id = str(action_value.get("thread_id", "")).strip()
        if not thread_id:
            return self.bot.make_card_response(toast="缺少 thread_id", toast_type="warning")
        try:
            thread = self._read_thread_summary(thread_id, original_arg=thread_id)
        except Exception as exc:
            logger.exception("读取归档目标失败")
            return self.bot.make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        try:
            self._adapter.archive_thread(thread.thread_id)
        except Exception as exc:
            logger.exception("归档线程失败")
            return self.bot.make_card_response(toast=f"归档线程失败：{exc}", toast_type="warning")
        self._favorites.remove_thread_globally(thread.thread_id)
        if state["current_thread_id"] == thread.thread_id:
            self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        return self._handle_sessions_refresh_action(
            sender_id,
            chat_id,
            message_id=message_id,
            toast=f"已归档线程：{thread.thread_id[:8]}…",
        )

    def _clear_pending_rename_form(self, message_id: str) -> None:
        if not message_id:
            return
        with self._lock:
            self._pending_rename_forms.pop(message_id, None)

    def _handle_sessions_refresh_action(
        self, sender_id: str, chat_id: str, *, message_id: str = "", toast: str
    ) -> P2CardActionTriggerResponse:
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            logger.exception("刷新线程列表失败")
            return self.bot.make_card_response(toast=f"刷新失败：{exc}", toast_type="warning")
        return self.bot.make_card_response(card=card, toast=toast, toast_type="success")

    def _render_sessions_card(self, sender_id: str, chat_id: str, *, message_id: str = "") -> dict:
        threads = self._list_current_dir_threads(sender_id, chat_id, message_id=message_id)
        sessions, counts = self._build_session_rows(sender_id, chat_id, threads, message_id=message_id)
        state = self._get_state(sender_id, chat_id, message_id)
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

    def _refresh_sessions_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        try:
            card = self._render_sessions_card(sender_id, chat_id, message_id=normalized_message_id)
        except Exception:
            logger.exception("刷新会话卡片失败")
            return
        self.bot.patch_message(normalized_message_id, json.dumps(card, ensure_ascii=False))

    def _handle_set_approval_policy(
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in _APPROVAL_POLICIES:
            return self.bot.make_card_response(toast="非法审批策略", toast_type="warning")
        state = self._get_state(sender_id, chat_id)
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
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in _SANDBOX_POLICIES:
            return self.bot.make_card_response(toast="非法沙箱策略", toast_type="warning")
        state = self._get_state(sender_id, chat_id)
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
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        preset = str(action_value.get("preset", "")).strip().lower()
        config = _PERMISSIONS_PRESETS.get(preset)
        if config is None:
            return self.bot.make_card_response(toast="非法权限预设", toast_type="warning")
        state = self._get_state(sender_id, chat_id)
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
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        mode = str(action_value.get("mode", "")).strip().lower()
        if mode not in {"default", "plan"}:
            return self.bot.make_card_response(toast="非法协作模式", toast_type="warning")
        state = self._get_state(sender_id, chat_id)
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
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        mode = self._normalize_group_mode(str(action_value.get("mode", "")))
        if mode not in {"assistant", "all", "mention_only"}:
            return self.bot.make_card_response(toast="非法群聊工作态", toast_type="warning")
        if not self.bot.is_group_admin(open_id=operator_open_id):
            return self.bot.make_card_response(toast="仅管理员可切换群聊工作态。", toast_type="warning")
        self.bot.set_group_mode(chat_id, mode)
        return self.bot.make_card_response(
            card=self._group_mode_card(chat_id, open_id=operator_open_id),
            toast=f"已切换群聊工作态：{mode}",
            toast_type="success",
        )

    def _handle_set_group_acl_policy_action(
        self, sender_id: str, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in {"admin-only", "allowlist", "all-members"}:
            return self.bot.make_card_response(toast="非法群聊授权策略", toast_type="warning")
        if not self.bot.is_group_admin(open_id=operator_open_id):
            return self.bot.make_card_response(toast="仅管理员可调整群聊授权策略。", toast_type="warning")
        self.bot.set_group_access_policy(chat_id, policy)
        return self.bot.make_card_response(
            card=self._group_acl_card(chat_id, open_id=operator_open_id),
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

    def _ensure_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        state = self._get_state(sender_id, chat_id, message_id)
        if state["current_thread_id"]:
            return state["current_thread_id"]
        snapshot = self._adapter.create_thread(
            cwd=state["working_dir"],
            profile=self._effective_default_profile() or None,
            approval_policy=state["approval_policy"] or None,
            sandbox=state["sandbox"] or None,
        )
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        return snapshot.summary.thread_id

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
    ) -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        try:
            snapshot = self._resume_snapshot_by_id(
                thread_id,
                original_arg=original_arg or thread_id,
                summary=summary,
            )
        except Exception as exc:
            logger.exception("恢复线程失败")
            self._reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            if refresh_session_message_id:
                self._refresh_sessions_card_message(sender_id, chat_id, refresh_session_message_id)
            return
        with self._lock:
            if state["running"]:
                self._reply_text(chat_id, "当前线程仍在执行，暂不切换。", message_id=message_id)
                if refresh_session_message_id:
                    self._refresh_sessions_card_message(sender_id, chat_id, refresh_session_message_id)
                return
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        if refresh_session_message_id:
            self._refresh_sessions_card_message(sender_id, chat_id, refresh_session_message_id)
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
                self._reply_card(
                    chat_id,
                    build_history_preview_card(
                        snapshot.summary.thread_id,
                        rounds,
                        summary=summary,
                    ),
                    message_id=message_id,
                )
                return
        self._reply_card(
            chat_id,
            build_markdown_card("Codex 已切换线程", summary, template="green"),
            message_id=message_id,
        )

    def _send_thread_snapshot_in_background(self, chat_id: str, thread_id: str, *, message_id: str = "") -> None:
        try:
            snapshot = self._read_thread_snapshot(thread_id, original_arg=thread_id, include_turns=True)
        except Exception as exc:
            logger.exception("读取线程快照失败")
            self._reply_text(chat_id, f"读取线程快照失败：{exc}", message_id=message_id)
            return
        rounds = self._extract_history_rounds(snapshot)
        self._reply_card(
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
            message_id=message_id,
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

    def _bind_thread(
        self,
        sender_id: str,
        chat_id: str,
        thread: ThreadSummary,
        *,
        message_id: str = "",
    ) -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        state_binding = self._state_binding(sender_id, chat_id, message_id)
        takeover_binding: tuple[str, str] | None = None
        with self._lock:
            old_thread_id = state["current_thread_id"]
            if old_thread_id and self._thread_bindings.get(old_thread_id) == state_binding:
                self._thread_bindings.pop(old_thread_id, None)
            existing_binding = self._thread_bindings.get(thread.thread_id)
            if existing_binding and existing_binding != state_binding:
                takeover_binding = existing_binding
            state["current_thread_id"] = thread.thread_id
            state["current_thread_name"] = thread.name or thread.preview
            state["working_dir"] = thread.cwd or state["working_dir"]
            state["current_turn_id"] = ""
            state["pending_local_turn_card"] = False
            self._clear_plan_state(state)
            self._thread_bindings[thread.thread_id] = state_binding
        if takeover_binding:
            self._reply_text(
                takeover_binding[1],
                (
                    f"线程 `{thread.thread_id[:8]}…` 已被另一飞书会话接管。"
                    "当前会话不再接收该线程的实时更新；如需重新接管，请再次执行 "
                    f"`/resume {thread.thread_id}`。"
                ),
            )

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        state = self._get_state(sender_id, chat_id, message_id)
        state_binding = self._state_binding(sender_id, chat_id, message_id)
        with self._lock:
            thread_id = state["current_thread_id"]
            if thread_id and self._thread_bindings.get(thread_id) == state_binding:
                self._thread_bindings.pop(thread_id, None)
            state["current_thread_id"] = ""
            state["current_thread_name"] = ""
            state["current_turn_id"] = ""
            state["pending_local_turn_card"] = False
            self._clear_plan_state(state)

    def _build_session_rows(
        self,
        sender_id: str,
        chat_id: str,
        threads: list[ThreadSummary],
        *,
        message_id: str = "",
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        starred_ids = self._favorites.load_favorites(sender_id)
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

        state = self._get_state(sender_id, chat_id, message_id)
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

    @staticmethod
    def _all_model_providers_filter() -> list[str]:
        return []

    def _list_current_dir_threads(self, sender_id: str, chat_id: str, *, message_id: str = "") -> list[ThreadSummary]:
        return list_current_dir_threads(
            self._adapter,
            cwd=self._get_state(sender_id, chat_id, message_id)["working_dir"],
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
        sender_id, chat_id = binding
        request_key = str(request_id)
        state = self._get_state(*binding)
        with self._lock:
            prompt_message_id = str(state.get("current_prompt_message_id", "")).strip()
            actor_open_id = str(state.get("current_actor_open_id", "")).strip()

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
            self._reply_text(
                chat_id,
                "收到 MCP elicitation 请求，当前版本暂未支持，已取消该请求。",
                message_id=prompt_message_id,
            )
            self._adapter.respond(request_id, result={"action": "cancel"})
            return
        else:
            logger.warning("未支持的 Codex server request: %s", method)
            self._adapter.respond(
                request_id,
                error={"code": -32001, "message": f"Unsupported request: {method}"},
            )
            return

        content = json.dumps(card, ensure_ascii=False)
        message_id: str | None = None
        if prompt_message_id:
            message_id = self.bot.reply_to_message(prompt_message_id, "interactive", content)
        if not message_id:
            message_id = self.bot.send_message_get_id(chat_id, "interactive", content)
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
                "chat_id": chat_id,
                "sender_id": sender_id,
                "actor_open_id": actor_open_id,
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
        previous_execution_card: dict[str, Any] | None = None
        should_interrupt_started_turn = False
        with self._lock:
            external_turn = not state.get("pending_local_turn_card", False)
            state["current_turn_id"] = turn_id
            if turn_id and state["pending_cancel"]:
                should_interrupt_started_turn = True
            if external_turn:
                previous_message_id = str(state.get("current_message_id", "")).strip()
                if previous_message_id:
                    previous_execution_card = {
                        "message_id": previous_message_id,
                        "reply_text": state["full_reply_text"],
                        "log_text": state["full_log_text"],
                        "cancelled": bool(state["cancelled"]),
                        "elapsed": int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0,
                    }
                state["cancelled"] = False
                state["current_message_id"] = ""
                state["current_prompt_message_id"] = ""
                state["current_actor_open_id"] = ""
                state["full_reply_text"] = ""
                state["full_log_text"] = ""
                state["started_at"] = time.monotonic()
                state["last_patch_at"] = 0.0
                state["followup_sent"] = False
            state["running"] = True
            state["pending_local_turn_card"] = False
            self._clear_plan_state(state)
        if external_turn:
            if previous_execution_card is not None:
                self._patch_execution_card_message(
                    previous_execution_card["message_id"],
                    log_text=previous_execution_card["log_text"],
                    reply_text=previous_execution_card["reply_text"],
                    running=False,
                    elapsed=previous_execution_card["elapsed"],
                    cancelled=previous_execution_card["cancelled"],
                )
            card_id = self._send_execution_card(binding[1], "")
            with self._lock:
                if state["current_turn_id"] == turn_id:
                    state["current_message_id"] = card_id or ""
        if should_interrupt_started_turn:
            try:
                self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
            except Exception:
                logger.exception("turn 启动后自动取消失败")
            else:
                with self._lock:
                    state["pending_cancel"] = False
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
            state["pending_cancel"] = False
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

    def _patch_execution_card_message(
        self,
        message_id: str,
        *,
        log_text: str,
        reply_text: str,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        card = build_execution_card(
            self._card_log_text(log_text),
            self._card_reply_text(reply_text),
            running=running,
            elapsed=elapsed,
            cancelled=cancelled and not running,
        )
        return self.bot.patch_message(normalized_message_id, json.dumps(card, ensure_ascii=False))

    def _schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        state = self._get_state(sender_id, chat_id)
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
                timer = threading.Timer(delay, self._flush_execution_card, args=(sender_id, chat_id))
                timer.daemon = True
                state["patch_timer"] = timer
                timer.start()
                immediate = False
            else:
                immediate = False
        if immediate:
            self._flush_execution_card(sender_id, chat_id)

    def _flush_execution_card(self, sender_id: str, chat_id: str, immediate: bool = False) -> None:
        state = self._get_state(sender_id, chat_id)
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
            prompt_message_id = str(state.get("current_prompt_message_id", "")).strip()
            elapsed = int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0
            state["last_patch_at"] = time.monotonic()

        ok = self._patch_execution_card_message(
            message_id,
            log_text=log_text,
            reply_text=reply_text,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
        )
        if not ok and immediate and reply_text:
            self._reply_text(chat_id, reply_text, message_id=prompt_message_id)

    def _send_followup_if_needed(self, sender_id: str, chat_id: str) -> None:
        state = self._get_state(sender_id, chat_id)
        with self._lock:
            if state["followup_sent"]:
                return
            reply_text = state["full_reply_text"]
            current_message_id = state["current_message_id"]
            prompt_message_id = str(state.get("current_prompt_message_id", "")).strip()
            need_followup = not current_message_id or len(reply_text) > self._card_reply_limit
            if not reply_text or not need_followup:
                return
            state["followup_sent"] = True
        self._reply_text(chat_id, reply_text, message_id=prompt_message_id)

    def _clear_plan_state(self, state: dict[str, Any]) -> None:
        state["plan_message_id"] = ""
        state["plan_turn_id"] = ""
        state["plan_explanation"] = ""
        state["plan_steps"] = []
        state["plan_text"] = ""

    def _flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        state = self._get_state(sender_id, chat_id)
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

    def _normalize_help_topic(self, topic: str) -> str:
        normalized = (topic or "").strip().lower()
        if normalized in {"", "basic", "basics", "overview"}:
            return "overview"
        if normalized in {"session", "sessions", "resume", "thread", "threads"}:
            return "session"
        if normalized in {
            "settings",
            "permission",
            "permissions",
            "approval",
            "sandbox",
            "mode",
            "advanced",
        }:
            return "settings"
        if normalized in {"group", "groups", "acl"}:
            return "group"
        if normalized in {"local", "fcodex", "wrapper"}:
            return "local"
        return ""

    def _build_help_card(self, topic: str) -> dict | None:
        normalized = self._normalize_help_topic(topic)
        if normalized == "overview":
            return build_help_dashboard_card(self._help_overview_text())
        if normalized == "session":
            return build_help_topic_card("Codex 帮助：线程", self._help_session_text())
        if normalized == "settings":
            return build_help_topic_actions_card(
                "Codex 帮助：设置",
                self._help_settings_text(),
                actions=[
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/permissions"},
                        "type": "default",
                        "value": {
                            "action": "show_permissions_card",
                            "plugin": KEYWORD,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/mode"},
                        "type": "default",
                        "value": {
                            "action": "show_mode_card",
                            "plugin": KEYWORD,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "返回帮助"},
                        "type": "default",
                        "value": {
                            "action": "show_help_overview",
                            "plugin": KEYWORD,
                        },
                    },
                ],
                layout="trisection",
            )
        if normalized == "group":
            return build_help_topic_actions_card(
                "Codex 帮助：群聊",
                self._help_group_text(),
                actions=[
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "/groupmode"},
                        "type": "default",
                        "value": {
                            "action": "show_group_mode_card",
                            "plugin": KEYWORD,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "返回帮助"},
                        "type": "default",
                        "value": {
                            "action": "show_help_overview",
                            "plugin": KEYWORD,
                        },
                    },
                ],
            )
        if normalized == "local":
            return build_markdown_card("Codex 帮助：本地继续", self._help_local_text())
        return None

    def _handle_show_help_topic_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        card = self._build_help_card(str(action_value.get("topic", "")))
        if card is None:
            return self.bot.make_card_response(toast="未知帮助主题。", toast_type="warning")
        return self.bot.make_card_response(card=card)

    def _handle_show_help_overview_action(self) -> P2CardActionTriggerResponse:
        return self.bot.make_card_response(card=build_help_dashboard_card(self._help_overview_text()))

    def _handle_show_permissions_card_action(
        self,
        sender_id: str,
        chat_id: str,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(sender_id, chat_id)
        return self.bot.make_card_response(
            card=build_permissions_preset_card(
                state["approval_policy"],
                state["sandbox"],
                running=state["running"],
            )
        )

    def _handle_show_mode_card_action(
        self,
        sender_id: str,
        chat_id: str,
    ) -> P2CardActionTriggerResponse:
        state = self._get_state(sender_id, chat_id)
        return self.bot.make_card_response(
            card=build_collaboration_mode_card(
                state["collaboration_mode"],
                running=state["running"],
            )
        )

    def _handle_show_group_mode_card_action(
        self,
        chat_id: str,
        message_id: str,
        action_value: dict,
    ) -> P2CardActionTriggerResponse:
        if not self._is_group_chat(chat_id, message_id):
            return self.bot.make_card_response(toast="该命令仅支持群聊使用。", toast_type="warning")
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        return self.bot.make_card_response(card=self._group_mode_card(chat_id, open_id=operator_open_id))

    def _reply_help(self, chat_id: str, topic: str = "", *, message_id: str = "") -> None:
        card = self._build_help_card(topic)
        if card is not None:
            self._reply_card(chat_id, card, message_id=message_id)
            return
        self._reply_text(
            chat_id,
            "帮助主题仅支持：`session`、`settings`、`group`、`local`。\n发送 `/help` 查看概览。",
            message_id=message_id,
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
            "- `/init <token>`：私聊初始化管理员和 `bot_open_id`\n"
            "- `/whoami`：私聊查看自己的 `open_id`，以及 best-effort 的 `user_id`（仅用于排障）\n"
            "- `/whoareyou`：查看机器人自己的 `app_id` / `open_id`\n\n"
            "**更多命令与帮助**\n"
            "- 下方按钮可直接切到 `session`、`settings`、`group`\n"
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
            "- `/init <token>` 仅私聊可用；会把当前发送者加入 `admin_open_ids`，并尽量自动写入 `bot_open_id`。\n"
            "- 运行时只有 `system.yaml.bot_open_id` 会参与群聊 mention 判定；`/whoareyou` 的实时探测结果仅用于诊断和初始化。\n"
            "- 推荐先用 `/permissions`；它会同时设置审批策略和沙箱，只影响当前飞书会话的后续 turn。\n"
            "- `/approval` 只改审批时机；`/sandbox` 只改文件与网络边界。\n"
            "- `/mode` 切换协作方式；`plan` 更容易先规划或提问，`default` 更接近直接执行；也只影响当前飞书会话的后续 turn。\n"
            "- 如果当前正在执行，新设置从下一轮生效。\n\n"
            "**命令**\n"
            "- `/init <token>`\n"
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
            "- 私聊底层会话按人隔离；群聊底层会话按 `chat_id` 共享。\n"
            "- `assistant` 会缓存群聊消息，仅在人类有效 mention 时回复；每次有效触发都会回捞最近群历史，把两次触发之间的消息补齐进上下文。\n"
            "- `assistant` 的主聊天流与群话题使用不同上下文边界：主聊天流只看主聊天流，话题只看当前话题；但底层仍是同一个群共享会话。\n"
            "- 主聊天流历史回捞受 `group_history_fetch_lookback_seconds` 和 `group_history_fetch_limit` 共同限制；话题内回捞当前只保证受边界和 `group_history_fetch_limit` 限制。\n"
            "- `group_history_fetch_lookback_seconds` 同时也是历史回捞总开关的一部分；任一项设为 `0`，主聊天流和话题回捞都会一起关闭。\n"
            "- `/acl` 查看当前群授权；管理员可设置 `admin-only`、`allowlist`、`all-members`。\n"
            "- 群里的所有 `/` 命令都只给管理员；在 `assistant` / `mention-only` 下还要先显式 mention 触发对象，在 `all` 下管理员可直接发送。\n"
            "- 有效 mention 默认只认机器人自身 `bot_open_id`；如配置 `trigger_open_ids`，`@这些人` 也会视为触发。\n"
            "- 群命令不写入 `assistant` 上下文日志，也不会推进上下文边界。\n"
            "- 由于飞书不会把其他机器人发言实时推给机器人，`assistant` 会在每次有效触发时额外回捞群历史，用来补齐其他机器人和遗漏消息。\n"
            "- 在话题内触发时，执行卡片、ACL 拒绝和长回复会尽量留在原话题。\n"
            "- 未获授权成员在 `all` 模式下直接发普通消息会静默忽略；只有显式 mention 触发对象或发群命令时才会收到拒绝提示。\n"
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
