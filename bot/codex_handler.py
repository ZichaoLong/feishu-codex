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
from dataclasses import dataclass, replace
from typing import Any, Callable, TypedDict, TypeAlias
from uuid import UUID

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.base import RuntimeConfigSummary, ThreadSnapshot, ThreadSummary
from bot.cards import (
    CommandResult,
    build_approval_handled_card,
    build_ask_user_answered_card,
    build_ask_user_card,
    build_command_approval_card,
    build_execution_card,
    build_file_change_approval_card,
    build_history_preview_card,
    build_markdown_card,
    build_plan_card,
    build_permissions_approval_card,
    make_card_response,
)
from bot.config import load_config_file
from bot.constants import (
    DEFAULT_APP_SERVER_MODE,
    DEFAULT_HISTORY_PREVIEW_ROUNDS,
    DEFAULT_SESSION_RECENT_LIMIT,
    DEFAULT_STREAM_PATCH_INTERVAL_MS,
    DEFAULT_THREAD_LIST_QUERY_LIMIT,
    FC_DATA_DIR,
    GROUP_SHARED_BINDING_OWNER_ID,
    KEYWORD,
    display_path,
    resolve_working_dir,
)
from bot.handler import BotHandler
from bot.codex_config_reader import ResolvedProfileConfig, resolve_profile_from_codex_config
from bot.codex_protocol.client import CodexRpcError
from bot.codex_group_domain import CodexGroupDomain
from bot.codex_help_domain import CodexHelpDomain
from bot.codex_session_ui_domain import CodexSessionUiDomain
from bot.codex_settings_domain import CodexSettingsDomain
from bot.profile_resolution import DefaultProfileResolution, resolve_local_default_profile
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.session_resolution import (
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.profile_state_store import ProfileStateStore

logger = logging.getLogger(__name__)

_CARD_REPLY_LIMIT_DEFAULT = 12000
_CARD_LOG_LIMIT_DEFAULT = 8000
_MIRROR_WATCHDOG_SECONDS_DEFAULT = 8.0
_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_POLICIES = {"read-only", "workspace-write", "danger-full-access"}
_LOCAL_THREAD_SAFETY_RULE = (
    "fcodex 和飞书可以同时读写同一线程；裸 codex 不要与 fcodex 或飞书同时写同一线程。"
)
_EMPTY_RESOLVED_PROFILE = ResolvedProfileConfig()
ChatBindingKey: TypeAlias = tuple[str, str]
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
_WORK_ITEM_LABELS = {
    "commandExecution": "命令执行",
    "fileChange": "文件修改",
    "imageGeneration": "图片生成",
    "mcpToolCall": "MCP 工具调用",
    "patchApply": "补丁应用",
    "viewImageToolCall": "查看图片",
    "webSearch": "网页搜索",
}


@dataclass(frozen=True)
class _CommandRoute:
    handler: Callable[[str, str, str, str], CommandResult | None]
    scope: str = "any"
    admin_only_in_group: bool = True
    scope_denied_text: str = ""


@dataclass(frozen=True)
class _ActionRoute:
    handler: Callable[[str, str, str, dict[str, Any]], P2CardActionTriggerResponse]
    group_guard: str = "none"


class _PlanStepState(TypedDict):
    step: str
    status: str


class _RuntimeState(TypedDict):
    active: bool
    working_dir: str
    current_thread_id: str
    current_thread_title: str
    current_turn_id: str
    running: bool
    cancelled: bool
    pending_cancel: bool
    current_message_id: str
    last_execution_message_id: str
    current_prompt_message_id: str
    current_actor_open_id: str
    execution_transcript: ExecutionTranscript
    runtime_channel_state: str
    started_at: float
    last_runtime_event_at: float
    last_patch_at: float
    patch_timer: threading.Timer | None
    mirror_watchdog_timer: threading.Timer | None
    mirror_watchdog_generation: int
    followup_sent: bool
    awaiting_local_turn_started: bool
    approval_policy: str
    sandbox: str
    collaboration_mode: str
    model: str
    reasoning_effort: str
    plan_message_id: str
    plan_turn_id: str
    plan_explanation: str
    plan_steps: list[_PlanStepState]
    plan_text: str


class _PendingRenameFormState(TypedDict):
    thread_id: str


class _PendingRequestState(TypedDict):
    rpc_request_id: int | str
    method: str
    params: dict[str, Any]
    thread_id: str
    turn_id: str
    title: str
    message_id: str
    questions: list[dict[str, Any]]
    answers: dict[str, str]
    chat_id: str
    sender_id: str
    actor_open_id: str


@dataclass(frozen=True)
class _TerminalReconcileTarget:
    chat_id: str
    thread_id: str
    turn_id: str
    card_message_id: str
    prompt_message_id: str
    transcript: ExecutionTranscript
    cancelled: bool
    elapsed: int


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
        self._runtime_state_by_binding: dict[ChatBindingKey, _RuntimeState] = {}
        self._chat_binding_key_by_thread_id: dict[str, ChatBindingKey] = {}
        self._pending_requests: dict[str, _PendingRequestState] = {}
        self._pending_rename_forms: dict[str, _PendingRenameFormState] = {}

        self._default_working_dir = resolve_working_dir(
            str(cfg.get("default_working_dir", "")),
        )
        self._session_recent_limit = int(cfg.get("session_recent_limit", DEFAULT_SESSION_RECENT_LIMIT))
        self._thread_list_query_limit = int(cfg.get("thread_list_query_limit", DEFAULT_THREAD_LIST_QUERY_LIMIT))
        self._history_preview_rounds = int(cfg.get("history_preview_rounds", DEFAULT_HISTORY_PREVIEW_ROUNDS))
        self._stream_patch_interval_ms = int(
            cfg.get("stream_patch_interval_ms", DEFAULT_STREAM_PATCH_INTERVAL_MS)
        )
        self._show_history_preview_on_resume = bool(cfg.get("show_history_preview_on_resume", True))
        self._card_reply_limit = int(cfg.get("card_reply_limit", _CARD_REPLY_LIMIT_DEFAULT))
        self._card_log_limit = int(cfg.get("card_log_limit", _CARD_LOG_LIMIT_DEFAULT))
        self._mirror_watchdog_seconds = float(
            cfg.get("mirror_watchdog_seconds", _MIRROR_WATCHDOG_SECONDS_DEFAULT)
        )

        self._adapter_config = CodexAppServerConfig.from_dict(cfg)
        self._app_server_runtime = AppServerRuntimeStore(self._data_dir)
        self._chat_binding_store = ChatBindingStore(self._data_dir)
        if self._adapter_config.app_server_mode == "remote":
            self._adapter_config = replace(
                self._adapter_config,
                app_server_url=resolve_effective_app_server_url(
                    self._adapter_config.app_server_url,
                    data_dir=self._data_dir,
                ),
            )
        self._profile_state = ProfileStateStore(self._data_dir)
        self._adapter = CodexAppServerAdapter(
            self._adapter_config,
            on_notification=self._handle_adapter_notification,
            on_request=self._handle_adapter_request,
            app_server_runtime_store=self._app_server_runtime,
        )
        self._settings_domain = CodexSettingsDomain(
            self,
            approval_policies=_APPROVAL_POLICIES,
            sandbox_policies=_SANDBOX_POLICIES,
            permissions_presets=_PERMISSIONS_PRESETS,
        )
        self._group_domain = CodexGroupDomain(self)
        self._help_domain = CodexHelpDomain(
            plugin_keyword=KEYWORD,
            local_thread_safety_rule=_LOCAL_THREAD_SAFETY_RULE,
        )
        self._session_ui_domain = CodexSessionUiDomain(self)
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
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        cleaned = (text or "").strip()
        with self._lock:
            if not state["active"]:
                state["active"] = True

        if not cleaned or cleaned.upper() == KEYWORD:
            self._dispatch_command_result(chat_id, self._help_domain.reply_help(chat_id))
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
                return make_card_response(
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

        pending_request: tuple[str, _PendingRequestState] | None = None
        with self._lock:
            for request_key, pending in self._pending_requests.items():
                if pending["method"] != "item/tool/requestUserInput":
                    continue
                if pending["message_id"] != message_id:
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
            return make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
        matched_question_id = ""
        for question in pending["questions"]:
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
            return make_card_response(
                toast="重命名表单已失效，请重新打开。",
                toast_type="warning",
            )
        if self._is_group_chat(chat_id, message_id) and not self._is_group_admin_actor(
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
        return self._session_ui_domain.handle_rename_submit_action(sender_id, chat_id, message_id, payload)

    def is_sender_active(self, sender_id: str, chat_id: str = "", message_id: str = "") -> bool:
        return self._get_runtime_state(sender_id, chat_id, message_id)["active"]

    def deactivate_sender(self, sender_id: str, chat_id: str = "", message_id: str = "") -> None:
        key = self._chat_binding_key(sender_id, chat_id, message_id)
        unsubscribe_thread_id: str = ""
        with self._lock:
            state = self._runtime_state_by_binding.pop(key, None)
            if not state:
                return
            self._cancel_patch_timer_locked(state)
            self._cancel_mirror_watchdog_locked(state)
            thread_id = state["current_thread_id"]
            if thread_id and self._chat_binding_key_by_thread_id.get(thread_id) == key:
                self._chat_binding_key_by_thread_id.pop(thread_id, None)
                unsubscribe_thread_id = thread_id
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)

    def shutdown(self) -> None:
        """停止底层 app-server。"""
        with self._lock:
            for state in self._runtime_state_by_binding.values():
                self._cancel_patch_timer_locked(state)
                self._cancel_mirror_watchdog_locked(state)
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("停止 Codex adapter 失败")

    def _build_default_runtime_state(self) -> _RuntimeState:
        stored_binding = self._build_default_stored_binding()
        return {
            "active": False,
            "working_dir": stored_binding["working_dir"],
            "current_thread_id": stored_binding["current_thread_id"],
            "current_thread_title": stored_binding["current_thread_title"],
            "current_turn_id": "",
            "running": False,
            "cancelled": False,
            "pending_cancel": False,
            "current_message_id": "",
            "last_execution_message_id": "",
            "current_prompt_message_id": "",
            "current_actor_open_id": "",
            "execution_transcript": ExecutionTranscript(),
            "runtime_channel_state": "live",
            "started_at": 0.0,
            "last_runtime_event_at": 0.0,
            "last_patch_at": 0.0,
            "patch_timer": None,
            "mirror_watchdog_timer": None,
            "mirror_watchdog_generation": 0,
            "followup_sent": False,
            "awaiting_local_turn_started": False,
            "approval_policy": stored_binding["approval_policy"],
            "sandbox": stored_binding["sandbox"],
            "collaboration_mode": stored_binding["collaboration_mode"],
            "model": self._adapter_config.model,
            "reasoning_effort": self._adapter_config.reasoning_effort,
            "plan_message_id": "",
            "plan_turn_id": "",
            "plan_explanation": "",
            "plan_steps": [],
            "plan_text": "",
        }

    def _build_default_stored_binding(self) -> dict[str, str]:
        return {
            "working_dir": self._default_working_dir,
            "current_thread_id": "",
            "current_thread_title": "",
            "approval_policy": self._adapter_config.approval_policy,
            "sandbox": self._adapter_config.sandbox,
            "collaboration_mode": self._adapter_config.collaboration_mode,
        }

    def _apply_stored_binding(self, state: _RuntimeState, stored_binding: dict[str, str]) -> None:
        state["working_dir"] = stored_binding["working_dir"]
        state["current_thread_id"] = stored_binding["current_thread_id"]
        state["current_thread_title"] = stored_binding["current_thread_title"]
        state["approval_policy"] = stored_binding["approval_policy"]
        state["sandbox"] = stored_binding["sandbox"]
        state["collaboration_mode"] = stored_binding["collaboration_mode"]

    def _stored_binding_from_runtime(self, state: _RuntimeState) -> dict[str, str]:
        return {
            "working_dir": str(state["working_dir"]).strip(),
            "current_thread_id": str(state["current_thread_id"]).strip(),
            "current_thread_title": str(state["current_thread_title"]).strip(),
            "approval_policy": str(state["approval_policy"]).strip(),
            "sandbox": str(state["sandbox"]).strip(),
            "collaboration_mode": str(state["collaboration_mode"]).strip(),
        }

    def _sync_stored_binding_locked(self, binding: ChatBindingKey, state: _RuntimeState) -> None:
        stored_binding = self._stored_binding_from_runtime(state)
        if stored_binding == self._build_default_stored_binding():
            self._chat_binding_store.clear(binding)
            return
        self._chat_binding_store.save(binding, stored_binding)

    def _save_stored_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            self._sync_stored_binding_locked(binding, state)

    @staticmethod
    def _cancel_timer(timer: threading.Timer | None) -> None:
        if timer is not None:
            timer.cancel()

    def _cancel_patch_timer_locked(self, state: _RuntimeState) -> None:
        self._cancel_timer(state["patch_timer"])
        state["patch_timer"] = None

    def _cancel_mirror_watchdog_locked(self, state: _RuntimeState) -> None:
        self._cancel_timer(state["mirror_watchdog_timer"])
        state["mirror_watchdog_timer"] = None
        state["mirror_watchdog_generation"] += 1

    @staticmethod
    def _mark_runtime_event_locked(state: _RuntimeState) -> None:
        state["last_runtime_event_at"] = time.monotonic()
        state["runtime_channel_state"] = "live"

    @staticmethod
    def _has_active_execution_locked(state: _RuntimeState) -> bool:
        return bool(state["current_message_id"]) and (
            state["running"]
            or state["awaiting_local_turn_started"]
            or bool(state["current_turn_id"])
        )

    @staticmethod
    def _clear_execution_anchor_locked(state: _RuntimeState, *, clear_card_message: bool) -> None:
        if clear_card_message:
            state["current_message_id"] = ""
        state["current_turn_id"] = ""
        state["current_prompt_message_id"] = ""
        state["current_actor_open_id"] = ""
        state["awaiting_local_turn_started"] = False

    def _retire_execution_anchor(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            current_message_id = state["current_message_id"].strip()
            if current_message_id:
                state["last_execution_message_id"] = current_message_id
            self._clear_execution_anchor_locked(state, clear_card_message=True)
            state["running"] = False
            state["pending_cancel"] = False
            state["runtime_channel_state"] = "live"

    def _refresh_terminal_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            message_id = state["current_message_id"].strip() or state["last_execution_message_id"].strip()
            if not message_id:
                return False
            transcript = state["execution_transcript"].clone()
            elapsed = int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0
            cancelled = state["cancelled"]
        return self._patch_execution_card_message(
            message_id,
            transcript=transcript,
            running=False,
            elapsed=elapsed,
            cancelled=cancelled,
        )

    def _capture_terminal_reconcile_target(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> _TerminalReconcileTarget | None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            card_message_id = state["current_message_id"].strip()
            if not card_message_id:
                return None
            resolved_turn_id = str(turn_id or state["current_turn_id"] or "").strip()
            if not resolved_turn_id:
                return None
            return _TerminalReconcileTarget(
                chat_id=chat_id,
                thread_id=str(thread_id or "").strip(),
                turn_id=resolved_turn_id,
                card_message_id=card_message_id,
                prompt_message_id=state["current_prompt_message_id"].strip(),
                transcript=state["execution_transcript"].clone(),
                cancelled=state["cancelled"],
                elapsed=int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0,
            )

    def _schedule_terminal_execution_reconcile(self, target: _TerminalReconcileTarget | None) -> None:
        if target is None or not target.thread_id or not target.card_message_id:
            return
        worker = threading.Thread(
            target=self._run_terminal_execution_reconcile,
            args=(target,),
            daemon=True,
        )
        worker.start()

    def _run_terminal_execution_reconcile(self, target: _TerminalReconcileTarget) -> None:
        try:
            snapshot = self._adapter.read_thread(target.thread_id, include_turns=True)
        except Exception as exc:
            logger.info(
                "终态补账跳过: chat=%s thread=%s reason=%s",
                target.chat_id,
                target.thread_id[:12],
                self._runtime_recovery_reason(exc),
            )
            return

        reply_text, reply_items = self._snapshot_reply(snapshot, turn_id=target.turn_id)
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

    def _mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if not self._has_active_execution_locked(state):
                return
            state["runtime_channel_state"] = "degraded"
            thread_id = state["current_thread_id"].strip()
        logger.warning(
            "执行通道暂时降级，保留当前执行锚点: chat=%s thread=%s reason=%s",
            chat_id,
            thread_id[:12],
            reason,
        )

    def _note_runtime_event(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._mark_runtime_event_locked(state)
        self._schedule_mirror_watchdog(sender_id, chat_id)

    def _schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._cancel_timer(state["mirror_watchdog_timer"])
            state["mirror_watchdog_timer"] = None
            if not state["running"] or not state["current_thread_id"]:
                state["mirror_watchdog_generation"] += 1
                return
            generation = state["mirror_watchdog_generation"] + 1
            state["mirror_watchdog_generation"] = generation
            timer = threading.Timer(
                self._mirror_watchdog_seconds,
                self._run_mirror_watchdog,
                args=(sender_id, chat_id, generation),
            )
            timer.daemon = True
            state["mirror_watchdog_timer"] = timer
            timer.start()

    def _run_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if state["mirror_watchdog_generation"] != generation:
                return
            state["mirror_watchdog_timer"] = None
            if not state["running"]:
                return
            thread_id = state["current_thread_id"].strip()
            turn_id = state["current_turn_id"].strip()
        if not thread_id:
            return
        finalized = self._reconcile_execution_snapshot(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        if not finalized:
            self._schedule_mirror_watchdog(sender_id, chat_id)

    def _existing_chat_binding_key_locked(self, sender_id: str, chat_id: str) -> ChatBindingKey | None:
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        if group_binding in self._runtime_state_by_binding:
            return group_binding
        sender_binding = (sender_id, chat_id)
        if sender_binding in self._runtime_state_by_binding:
            return sender_binding
        return None

    def _get_runtime_state(self, sender_id: str, chat_id: str, message_id: str = "") -> _RuntimeState:
        with self._lock:
            existing = self._existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return self._runtime_state_by_binding[existing]
        key = self._chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            existing = self._existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return self._runtime_state_by_binding[existing]
            if key not in self._runtime_state_by_binding:
                state = self._build_default_runtime_state()
                stored_binding = self._chat_binding_store.load(key)
                if stored_binding is not None:
                    self._apply_stored_binding(state, stored_binding)
                self._runtime_state_by_binding[key] = state
            return self._runtime_state_by_binding[key]

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

    def _chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        if sender_id == GROUP_SHARED_BINDING_OWNER_ID:
            return (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        with self._lock:
            existing = self._existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return existing
        if self._is_group_chat(chat_id, message_id):
            return (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
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
        state = self._get_runtime_state(GROUP_SHARED_BINDING_OWNER_ID, chat_id, message_id)
        actor_open_id = self._group_actor_open_id(message_id, operator_open_id)
        with self._lock:
            current_actor_open_id = state["current_actor_open_id"].strip()
        return bool(current_actor_open_id and actor_open_id and current_actor_open_id == actor_open_id)

    def _is_group_request_actor_or_admin(
        self,
        chat_id: str,
        *,
        request_key: str,
        pending: _PendingRequestState | None = None,
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
        request_actor_open_id = request["actor_open_id"].strip()
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

    def _build_command_routes(self) -> dict[str, _CommandRoute]:
        return {
            "/help": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/h": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/init": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_init_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/init <token>`。",
            ),
            "/pwd": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=f"当前目录：`{display_path(self._get_runtime_state(sender_id, chat_id, message_id)['working_dir'])}`",
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
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_whoami_command(
                    sender_id, chat_id, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/whoami`。",
            ),
            "/whoareyou": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_botinfo_command(
                    chat_id, message_id=message_id
                ),
            ),
            "/profile": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_profile_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/cancel": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=self._cancel_current_turn(sender_id, chat_id, message_id=message_id)[1],
                ),
            ),
            "/session": _CommandRoute(
                handler=self._session_ui_domain.handle_session_command,
            ),
            "/resume": _CommandRoute(
                handler=self._session_ui_domain.handle_resume_command,
            ),
            "/rm": _CommandRoute(
                handler=self._session_ui_domain.handle_rm_command,
            ),
            "/rename": _CommandRoute(
                handler=self._session_ui_domain.handle_rename_command,
            ),
            "/approval": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_approval_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/sandbox": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_sandbox_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/permissions": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_permissions_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/mode": _CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_mode_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/groupmode": _CommandRoute(
                handler=self._group_domain.handle_groupmode_command,
                scope="group",
            ),
            "/acl": _CommandRoute(
                handler=self._group_domain.handle_acl_command,
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
                handler=self._session_ui_domain.handle_resume_thread_action,
                group_guard="group_admin",
            ),
            "show_more_sessions": _ActionRoute(
                handler=self._session_ui_domain.handle_show_more_sessions_action,
                group_guard="group_admin",
            ),
            "close_sessions_card": _ActionRoute(
                handler=self._session_ui_domain.handle_close_sessions_card_action,
                group_guard="group_admin",
            ),
            "reopen_sessions_card": _ActionRoute(
                handler=self._session_ui_domain.handle_reopen_sessions_card_action,
                group_guard="group_admin",
            ),
            "show_help_topic": _ActionRoute(
                handler=self._help_domain.handle_show_help_topic_action,
            ),
            "show_help_overview": _ActionRoute(
                handler=self._help_domain.handle_show_help_overview_action,
            ),
            "show_permissions_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_show_permissions_card_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "show_mode_card": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_show_mode_card_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "show_group_mode_card": _ActionRoute(
                handler=self._group_domain.handle_show_group_mode_card_action,
                group_guard="group_admin",
            ),
            "archive_thread": _ActionRoute(
                handler=self._session_ui_domain.handle_archive_thread_action,
                group_guard="group_admin",
            ),
            "show_rename_form": _ActionRoute(
                handler=self._session_ui_domain.handle_show_rename_action,
                group_guard="group_admin",
            ),
            "rename_thread": _ActionRoute(
                handler=self._session_ui_domain.handle_rename_submit_action,
                group_guard="group_admin",
            ),
            "cancel_rename": _ActionRoute(
                handler=self._session_ui_domain.handle_cancel_rename_action,
                group_guard="group_admin",
            ),
            "set_approval_policy": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_approval_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_sandbox_policy": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_sandbox_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_permissions_preset": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_permissions_preset(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_profile": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_profile(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_collaboration_mode": _ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_collaboration_mode(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_group_mode": _ActionRoute(
                handler=self._group_domain.handle_set_group_mode_action,
                group_guard="group_admin",
            ),
            "set_group_acl_policy": _ActionRoute(
                handler=self._group_domain.handle_set_group_acl_policy_action,
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
            return make_card_response(
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
            return make_card_response(
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
            return make_card_response(
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
            return make_card_response(
                toast="仅管理员或当前提问者可提交群里的补充输入。",
                toast_type="warning",
            )
        logger.warning("未知卡片群权限守卫: %s", route.group_guard)
        return make_card_response(
            toast="当前卡片动作配置异常。",
            toast_type="warning",
        )

    def _handle_command(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        command, _, arg = text.partition(" ")
        arg = arg.strip()
        cmd = command.lower()
        route = self._command_routes.get(cmd)
        if route is None:
            self._reply_text(chat_id, f"未知命令：`{command}`\n发送 `/help` 查看可用命令。", message_id=message_id)
            return
        # 先做 scope guard，保证群/私聊专属命令优先返回精确拒绝文本；
        # 只有 scope 允许通过后，才需要进入"群里是否仅管理员可用"的判断。
        if not self._ensure_command_scope(route, chat_id, message_id):
            return
        if route.admin_only_in_group and not self._ensure_group_command_admin(chat_id, message_id):
            return
        result = route.handler(sender_id, chat_id, arg, message_id)
        if result is not None:
            self._dispatch_command_result(chat_id, result, message_id=message_id)

    def _dispatch_command_result(self, chat_id: str, result: CommandResult, *, message_id: str = "") -> None:
        if result.card is not None:
            self._reply_card(chat_id, result.card, message_id=message_id)
        elif result.text:
            self._reply_text(chat_id, result.text, message_id=message_id)

    @staticmethod
    def _is_turn_thread_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not found:")

    @staticmethod
    def _is_request_timeout_error(exc: Exception) -> bool:
        return isinstance(exc, TimeoutError) and str(exc).startswith("Codex request timed out:")

    @staticmethod
    def _runtime_recovery_reason(exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return str(exc)
        if isinstance(exc, CodexRpcError):
            return str(exc.error.get("message", "") or exc)
        return str(exc)

    def _resume_bound_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        thread_id = str(state["current_thread_id"] or "").strip()
        if not thread_id:
            raise RuntimeError("当前没有可恢复的线程绑定")
        summary = ThreadSummary(
            thread_id=thread_id,
            cwd=state["working_dir"],
            name=state["current_thread_title"],
            preview=state["current_thread_title"],
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        snapshot = self._resume_snapshot_by_id(
            thread_id,
            original_arg=thread_id,
            summary=summary,
        )
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        return snapshot.summary.thread_id

    @staticmethod
    def _snapshot_reply(snapshot: ThreadSnapshot, *, turn_id: str = "") -> tuple[str, list[dict[str, Any]]]:
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

    def _card_reply_segments(
        self,
        transcript: ExecutionTranscript,
    ) -> list[ExecutionReplySegment]:
        return transcript.reply_segments_for_card(self._card_reply_limit)

    def _finalize_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            has_card = bool(state["current_message_id"])
            state["running"] = False
            state["pending_cancel"] = False
            state["awaiting_local_turn_started"] = False
            state["current_turn_id"] = ""
            self._cancel_mirror_watchdog_locked(state)
        if not has_card:
            with self._lock:
                self._clear_execution_anchor_locked(state, clear_card_message=False)
                state["runtime_channel_state"] = "live"
            return False
        self._flush_execution_card(sender_id, chat_id, immediate=True)
        self._send_followup_if_needed(sender_id, chat_id)
        self._retire_execution_anchor(sender_id, chat_id)
        return True

    def _finalize_execution_from_terminal_signal(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        target = self._capture_terminal_reconcile_target(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        finalized = self._finalize_execution_card_from_state(sender_id, chat_id)
        if finalized:
            self._schedule_terminal_execution_reconcile(target)
        return finalized

    def _reconcile_execution_snapshot(
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
            snapshot = self._adapter.read_thread(normalized_thread_id, include_turns=True)
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
                self._mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return False
            logger.exception("读取线程快照失败: thread=%s", normalized_thread_id[:12])
            return False

        reply_text, reply_items = self._snapshot_reply(snapshot, turn_id=turn_id)
        state = self._get_runtime_state(sender_id, chat_id)
        should_finalize = snapshot.summary.status != "active"
        with self._lock:
            state["current_thread_title"] = snapshot.summary.title or state["current_thread_title"]
            state["working_dir"] = snapshot.summary.cwd or state["working_dir"]
            self._sync_stored_binding_locked(self._chat_binding_key(sender_id, chat_id), state)
            transcript = state["execution_transcript"]
            if reply_text and len(reply_text) >= len(transcript.reply_text()):
                if not transcript.rebuild_reply_from_snapshot_items(
                    reply_items,
                    fallback_text=reply_text,
                ):
                    transcript.set_reply_text(reply_text)
            if not should_finalize:
                state["running"] = True
                state["awaiting_local_turn_started"] = False
                self._mark_runtime_event_locked(state)
                return False
        return self._finalize_execution_card_from_state(sender_id, chat_id)

    def _start_prompt_turn(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
    ) -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
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
            state["last_execution_message_id"] = ""
            state["current_prompt_message_id"] = str(message_id or "").strip()
            state["current_actor_open_id"] = str(actor_open_id or "").strip() or self._group_actor_open_id(message_id)
            state["execution_transcript"].reset()
            state["runtime_channel_state"] = "live"
            state["started_at"] = time.monotonic()
            state["last_runtime_event_at"] = state["started_at"]
            state["followup_sent"] = False
            state["last_patch_at"] = 0.0
            state["awaiting_local_turn_started"] = True
            self._clear_plan_state(state)

        card_id = ""
        if message_id and hasattr(self.bot, "claim_reserved_execution_card"):
            card_id = str(self.bot.claim_reserved_execution_card(message_id) or "").strip()
            if card_id:
                self.bot.patch_message(
                    card_id,
                    json.dumps(build_execution_card("", [], running=True), ensure_ascii=False),
                )
        if not card_id:
            card_id = self._send_execution_card(chat_id, message_id)
        with self._lock:
            state["current_message_id"] = card_id or ""

        def _start_turn_once(bound_thread_id: str) -> dict[str, Any]:
            return self._adapter.start_turn(
                thread_id=bound_thread_id,
                text=text,
                cwd=state["working_dir"],
                model=state["model"] or None,
                profile=self._effective_default_profile() or None,
                approval_policy=state["approval_policy"] or None,
                sandbox=state["sandbox"] or None,
                reasoning_effort=state["reasoning_effort"] or None,
                collaboration_mode=state["collaboration_mode"] or None,
            )

        try:
            start_response = _start_turn_once(thread_id)
        except Exception as exc:
            if self._is_turn_thread_not_found_error(exc) and str(state["current_thread_id"] or "").strip():
                logger.info("检测到线程未加载，自动恢复后重试: thread=%s", thread_id[:12])
                try:
                    thread_id = self._resume_bound_thread(sender_id, chat_id, message_id=message_id)
                    start_response = _start_turn_once(thread_id)
                except Exception as retry_exc:
                    logger.exception("自动恢复线程后重试 turn 失败")
                    with self._lock:
                        state["running"] = False
                        state["pending_cancel"] = False
                        state["execution_transcript"].set_reply_text(f"启动失败：{retry_exc}")
                    if self._is_thread_not_found_error(retry_exc):
                        with self._lock:
                            state["current_thread_id"] = ""
                            state["current_thread_title"] = ""
                            self._sync_stored_binding_locked(self._chat_binding_key(sender_id, chat_id, message_id), state)
                    self._flush_execution_card(sender_id, chat_id, immediate=True)
                    self._retire_execution_anchor(sender_id, chat_id)
                    if not card_id:
                        self._reply_text(chat_id, f"启动失败：{retry_exc}", message_id=message_id)
                    return
            else:
                logger.exception("启动 turn 失败")
                with self._lock:
                    state["running"] = False
                    state["pending_cancel"] = False
                    state["execution_transcript"].set_reply_text(f"启动失败：{exc}")
                self._flush_execution_card(sender_id, chat_id, immediate=True)
                self._retire_execution_anchor(sender_id, chat_id)
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
        self._schedule_mirror_watchdog(sender_id, chat_id)

    def _handle_running_prompt(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> bool:
        del text
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        with self._lock:
            if not state["running"]:
                return False
            thread_id = state["current_thread_id"].strip()
            turn_id = state["current_turn_id"].strip()
            last_runtime_event_at = state["last_runtime_event_at"]
        if thread_id and last_runtime_event_at and (
            time.monotonic() - last_runtime_event_at >= self._mirror_watchdog_seconds
        ):
            self._reconcile_execution_snapshot(
                sender_id,
                chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            with self._lock:
                if not state["running"]:
                    return False
        self._reply_text(chat_id, "当前线程仍在执行，请等待结束或先执行 `/cancel`。", message_id=message_id)
        return True

    def _handle_prompt(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        if self._handle_running_prompt(sender_id, chat_id, text, message_id=message_id):
            return
        self._start_prompt_turn(sender_id, chat_id, text, message_id=message_id)

    def _handle_cd_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                return CommandResult(card=build_markdown_card(
                    "Codex 目录未切换",
                    "执行中不能切换目录，请等待结束或先停止当前执行。",
                    template="orange",
                ))

        if not arg:
            return CommandResult(card=build_markdown_card(
                "Codex 当前目录",
                f"当前目录：`{display_path(state['working_dir'])}`",
            ))

        target = resolve_working_dir(arg, fallback=state["working_dir"])
        if not pathlib.Path(target).exists():
            return CommandResult(card=build_markdown_card(
                "Codex 目录未切换",
                f"目录不存在：`{display_path(target)}`",
                template="orange",
            ))
        if not pathlib.Path(target).is_dir():
            return CommandResult(card=build_markdown_card(
                "Codex 目录未切换",
                f"不是目录：`{display_path(target)}`",
                template="orange",
            ))

        self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        with self._lock:
            state["working_dir"] = target
            self._sync_stored_binding_locked(self._chat_binding_key(sender_id, chat_id, message_id), state)
        return CommandResult(card=build_markdown_card(
            "Codex 目录已切换",
            (
                f"目录：`{display_path(target)}`\n"
                "当前线程绑定已清空。\n"
                "直接发送普通文本，会在新目录自动新建线程。"
            ),
        ))

    def _handle_new_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        with self._lock:
            if state["running"]:
                return CommandResult(text="执行中不能新建线程，请等待结束或先执行 `/cancel`。")
        try:
            snapshot = self._adapter.create_thread(
                cwd=state["working_dir"],
                profile=self._effective_default_profile() or None,
                approval_policy=state["approval_policy"] or None,
                sandbox=state["sandbox"] or None,
            )
        except Exception as exc:
            logger.exception("新建线程失败")
            return CommandResult(text=f"新建线程失败：{exc}")
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        return CommandResult(card=build_markdown_card(
            "Codex 线程已新建",
            (
                f"线程：`{snapshot.summary.thread_id[:8]}…`\n"
                f"目录：`{display_path(snapshot.summary.cwd)}`\n"
                "直接发送普通文本开始第一轮对话。"
            ),
            template="green",
        ))

    def _handle_status_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        thread_id = state["current_thread_id"]
        title = state["current_thread_title"] or "（无标题）"
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
        return CommandResult(card=build_markdown_card("Codex 当前状态", content, template=template))

    def _handle_cancel_action(self, sender_id: str, chat_id: str) -> P2CardActionTriggerResponse:
        ok, message = self._cancel_current_turn(sender_id, chat_id)
        return make_card_response(toast=message, toast_type="success" if ok else "warning")

    def _cancel_current_turn(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> tuple[bool, str]:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        thread_id = state["current_thread_id"]
        turn_id = state["current_turn_id"]
        if not state["running"] or not thread_id:
            if state["current_message_id"] or state["last_execution_message_id"]:
                self._refresh_terminal_execution_card_from_state(sender_id, chat_id)
                return True, "当前执行已结束，已刷新卡片状态。"
            return False, "当前没有正在执行的 turn。"
        if not turn_id:
            with self._lock:
                state["cancelled"] = True
                state["pending_cancel"] = True
            return True, "已请求停止当前执行。"
        try:
            self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            if self._is_turn_thread_not_found_error(exc) or self._is_thread_not_found_error(exc):
                self._finalize_execution_card_from_state(sender_id, chat_id)
                return True, "当前执行已结束，已刷新卡片状态。"
            if self._is_transport_disconnect(exc) or self._is_request_timeout_error(exc):
                self._mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return True, "取消请求已发送，但当前后端状态暂不可确认；稍后会自动对账。"
            logger.exception("取消 turn 失败")
            return False, f"取消失败：{exc}"
        with self._lock:
            state["cancelled"] = True
            state["pending_cancel"] = False
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

    def _refresh_sessions_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        self._session_ui_domain.refresh_sessions_card_message(sender_id, chat_id, message_id)

    def _handle_approval_card_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", ""))
        with self._lock:
            pending = self._pending_requests.get(request_key)
        if not pending:
            return make_card_response(toast="该审批请求已失效或已处理。", toast_type="warning")

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
            return make_card_response(toast="未知审批动作", toast_type="warning")

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
        return make_card_response(
            card=build_approval_handled_card(title, decision_text),
            toast=f"已{decision_text}",
            toast_type="success",
        )

    def _handle_user_input_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        request_key = str(action_value.get("request_id", ""))
        with self._lock:
            pending = self._pending_requests.get(request_key)
        if not pending:
            return make_card_response(toast="该输入请求已失效或已处理。", toast_type="warning")

        question_id = str(action_value.get("question_id", ""))
        if not question_id:
            return make_card_response(toast="缺少 question_id", toast_type="warning")

        target_question = next((item for item in pending["questions"] if item.get("id", "") == question_id), None)
        if not target_question:
            return make_card_response(toast="未找到对应问题", toast_type="warning")

        if action_value.get("action") == "answer_user_input_option":
            answer = str(action_value.get("answer", "")).strip()
        else:
            options = target_question.get("options") or []
            allow_custom = bool(target_question.get("isOther", False)) or not options
            if not allow_custom:
                return make_card_response(toast="该问题仅支持选择预设选项", toast_type="warning")
            form_value = action_value.get("_form_value") or {}
            answer = str(form_value.get(f"user_input_{question_id}", "")).strip()
        if not answer:
            return make_card_response(toast="回答不能为空", toast_type="warning")

        pending["answers"][question_id] = answer
        questions = pending["questions"]
        if len(pending["answers"]) < len(questions):
            return make_card_response(
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
        return make_card_response(
            card=build_ask_user_answered_card(questions, pending["answers"]),
            toast="已提交回答。",
            toast_type="success",
        )

    def _ensure_thread(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
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
        state = self._get_runtime_state(sender_id, chat_id, message_id)
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
        profile = self._effective_default_profile()
        resolved = resolve_profile_from_codex_config(profile) if profile else _EMPTY_RESOLVED_PROFILE
        try:
            return self._adapter.resume_thread(
                thread_id,
                profile=profile or None,
                model=resolved.model or None,
                model_provider=resolved.model_provider or None,
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
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        chat_binding_key = self._chat_binding_key(sender_id, chat_id, message_id)
        takeover_chat_binding_key: tuple[str, str] | None = None
        unsubscribe_thread_id: str = ""
        with self._lock:
            old_thread_id = state["current_thread_id"]
            if (
                old_thread_id
                and old_thread_id != thread.thread_id
                and self._chat_binding_key_by_thread_id.get(old_thread_id) == chat_binding_key
            ):
                self._chat_binding_key_by_thread_id.pop(old_thread_id, None)
                unsubscribe_thread_id = old_thread_id
            existing_chat_binding_key = self._chat_binding_key_by_thread_id.get(thread.thread_id)
            if existing_chat_binding_key and existing_chat_binding_key != chat_binding_key:
                takeover_chat_binding_key = existing_chat_binding_key
            state["current_thread_id"] = thread.thread_id
            state["current_thread_title"] = thread.title
            state["working_dir"] = thread.cwd or state["working_dir"]
            state["current_turn_id"] = ""
            state["awaiting_local_turn_started"] = False
            self._cancel_mirror_watchdog_locked(state)
            self._clear_plan_state(state)
            self._chat_binding_key_by_thread_id[thread.thread_id] = chat_binding_key
            self._sync_stored_binding_locked(chat_binding_key, state)
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)
        if takeover_chat_binding_key:
            self._reply_text(
                takeover_chat_binding_key[1],
                (
                    f"线程 `{thread.thread_id[:8]}…` 已被另一飞书会话接管。"
                    "当前会话不再接收该线程的实时更新；如需重新接管，请再次执行 "
                    f"`/resume {thread.thread_id}`。"
                ),
            )

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        chat_binding_key = self._chat_binding_key(sender_id, chat_id, message_id)
        unsubscribe_thread_id: str = ""
        with self._lock:
            thread_id = state["current_thread_id"]
            if thread_id and self._chat_binding_key_by_thread_id.get(thread_id) == chat_binding_key:
                self._chat_binding_key_by_thread_id.pop(thread_id, None)
                unsubscribe_thread_id = thread_id
            state["current_thread_id"] = ""
            state["current_thread_title"] = ""
            state["current_turn_id"] = ""
            state["awaiting_local_turn_started"] = False
            self._cancel_mirror_watchdog_locked(state)
            self._clear_plan_state(state)
            self._sync_stored_binding_locked(chat_binding_key, state)
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)

    def _list_global_threads(self) -> list[ThreadSummary]:
        return list_global_threads(
            self._adapter,
            limit=self._thread_list_query_limit,
        )

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
        if method == "thread/closed":
            self._handle_thread_closed(params)
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
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            logger.warning("未找到线程绑定，自动 fail-close: method=%s thread=%s", method, thread_id)
            self._auto_reject_request(request_id, method, params)
            return
        sender_id, chat_id = binding
        request_key = str(request_id)
        state = self._get_runtime_state(*binding)
        with self._lock:
            prompt_message_id = state["current_prompt_message_id"].strip()
            actor_open_id = state["current_actor_open_id"].strip()

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
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        state = self._get_runtime_state(*binding)
        status = params.get("status") or {}
        status_type = status.get("type")
        self._note_runtime_event(*binding)
        with self._lock:
            current_turn_id = state["current_turn_id"]
            current_message_id = state["current_message_id"]
            if status_type == "active":
                state["running"] = True
                state["awaiting_local_turn_started"] = False
        if status_type != "active" and (current_turn_id or current_message_id):
            self._finalize_execution_from_terminal_signal(
                binding[0],
                binding[1],
                thread_id=thread_id,
                turn_id=current_turn_id,
            )
            return
        if status_type == "active":
            self._schedule_execution_card_update(*binding)
            return
        with self._lock:
            state["pending_cancel"] = False
            state["awaiting_local_turn_started"] = False
            state["runtime_channel_state"] = "live"
            state["running"] = False
            state["current_turn_id"] = ""
            self._cancel_mirror_watchdog_locked(state)
        self._flush_execution_card(*binding, immediate=True)

    def _handle_thread_closed(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
        with self._lock:
            current_turn_id = state["current_turn_id"]
            current_message_id = state["current_message_id"]
            is_running = state["running"]
        if is_running or current_turn_id or current_message_id:
            self._finalize_execution_from_terminal_signal(
                binding[0],
                binding[1],
                thread_id=thread_id,
                turn_id=current_turn_id,
            )
            return
        with self._lock:
            state["running"] = False
            state["pending_cancel"] = False
            self._cancel_mirror_watchdog_locked(state)

    def _handle_thread_name_updated(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
        with self._lock:
            if state["current_thread_id"] == thread_id:
                state["current_thread_title"] = str(params.get("threadName") or "").strip() or state["current_thread_title"]
                self._sync_stored_binding_locked(binding, state)

    def _handle_turn_started(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
        turn = params.get("turn") or {}
        turn_id = turn.get("id", "")
        previous_execution_card: dict[str, Any] | None = None
        should_interrupt_started_turn = False
        with self._lock:
            reuse_existing_card = self._has_active_execution_locked(state)
            if turn_id and state["pending_cancel"]:
                should_interrupt_started_turn = True
            if not reuse_existing_card:
                previous_message_id = state["current_message_id"].strip()
                if previous_message_id:
                    previous_execution_card = {
                        "message_id": previous_message_id,
                        "transcript": state["execution_transcript"].clone(),
                        "cancelled": bool(state["cancelled"]),
                        "elapsed": int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0,
                    }
                state["cancelled"] = False
                state["last_execution_message_id"] = ""
                self._clear_execution_anchor_locked(state, clear_card_message=True)
                state["execution_transcript"].reset()
                state["started_at"] = time.monotonic()
                state["last_runtime_event_at"] = state["started_at"]
                state["last_patch_at"] = 0.0
                state["followup_sent"] = False
                state["runtime_channel_state"] = "live"
            state["current_turn_id"] = turn_id
            state["running"] = True
            state["awaiting_local_turn_started"] = False
            self._clear_plan_state(state)
        if not reuse_existing_card:
            if previous_execution_card is not None:
                self._patch_execution_card_message(
                    previous_execution_card["message_id"],
                    transcript=previous_execution_card["transcript"],
                    running=False,
                    elapsed=previous_execution_card["elapsed"],
                    cancelled=previous_execution_card["cancelled"],
                )
            card_id = self._send_execution_card(binding[1], "")
            with self._lock:
                if state["current_turn_id"] == turn_id:
                    state["current_message_id"] = card_id or ""
                    state["last_execution_message_id"] = ""
        if should_interrupt_started_turn:
            try:
                self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
            except Exception:
                logger.exception("turn 启动后自动取消失败")
            else:
                with self._lock:
                    state["pending_cancel"] = False
        self._schedule_mirror_watchdog(*binding)
        self._schedule_execution_card_update(*binding)

    def _handle_turn_plan_updated(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
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
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if binding:
            self._note_runtime_event(*binding)
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        if not binding:
            return
        state = self._get_runtime_state(*binding)
        if item_type == "commandExecution":
            command = item.get("command") or ""
            cwd = item.get("cwd") or ""
            with self._lock:
                state["execution_transcript"].start_process_block(
                    f"\n$ ({display_path(cwd)}) {command}\n",
                    marks_work=True,
                )
            self._schedule_execution_card_update(*binding)
        elif item_type == "fileChange":
            with self._lock:
                state["execution_transcript"].start_process_block("\n[准备应用文件修改]\n", marks_work=True)
            self._schedule_execution_card_update(*binding)
        elif item_type in _WORK_ITEM_LABELS:
            with self._lock:
                state["execution_transcript"].append_process_note(
                    f"\n[{_WORK_ITEM_LABELS[item_type]}]\n",
                    marks_work=True,
                )
            self._schedule_execution_card_update(*binding)

    def _handle_agent_message_delta(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
        with self._lock:
            state["execution_transcript"].append_assistant_delta(str(params.get("delta", "") or ""))
        self._schedule_execution_card_update(*binding)

    def _handle_command_delta(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if binding:
            self._note_runtime_event(*binding)
        self._append_log_by_thread(thread_id, str(params.get("delta", "") or ""))

    def _handle_file_change_delta(self, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if binding:
            self._note_runtime_event(*binding)
        self._append_log_by_thread(thread_id, str(params.get("delta", "") or ""))

    def _handle_item_completed(self, params: dict[str, Any]) -> None:
        item = params.get("item") or {}
        item_type = str(item.get("type", "") or "").strip()
        thread_id = params.get("threadId", "")
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if binding:
            self._note_runtime_event(*binding)
        if item_type == "commandExecution":
            exit_code = item.get("exitCode")
            status = item.get("status")
            state = self._get_runtime_state(*binding) if binding else None
            if state is not None:
                with self._lock:
                    state["execution_transcript"].finish_process_block(
                        f"\n[命令结束 status={status} exit={exit_code}]\n"
                    )
                self._schedule_execution_card_update(*binding)
        elif item_type == "fileChange":
            changes = item.get("changes") or []
            state = self._get_runtime_state(*binding) if binding else None
            if state is not None:
                suffix = ""
                if changes:
                    summary = "\n".join(
                        f"- {change.get('kind', 'update')}: {change.get('path', '')}"
                        for change in changes[:20]
                    )
                    suffix = f"\n[文件变更]\n{summary}\n"
                with self._lock:
                    state["execution_transcript"].finish_process_block(suffix)
                self._schedule_execution_card_update(*binding)
        elif item_type == "agentMessage" and item.get("text"):
            binding = self._chat_binding_key_by_thread_id.get(thread_id)
            if not binding:
                return
            state = self._get_runtime_state(*binding)
            with self._lock:
                transcript = state["execution_transcript"]
                if len(item["text"]) >= len(transcript.reply_text()):
                    transcript.reconcile_current_assistant_text(str(item["text"] or ""))
            self._schedule_execution_card_update(*binding)
        elif item_type in _WORK_ITEM_LABELS:
            state = self._get_runtime_state(*binding) if binding else None
            if state is not None:
                with self._lock:
                    state["execution_transcript"].finish_process_block()
                self._schedule_execution_card_update(*binding)
        elif item_type == "plan" and item.get("text"):
            binding = self._chat_binding_key_by_thread_id.get(thread_id)
            if not binding:
                return
            state = self._get_runtime_state(*binding)
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
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        self._note_runtime_event(*binding)
        state = self._get_runtime_state(*binding)
        turn = params.get("turn") or {}
        error = turn.get("error") or {}
        status = turn.get("status")
        turn_id = str(turn.get("id", "") or "").strip()
        with self._lock:
            if status == "interrupted":
                state["cancelled"] = True
            transcript = state["execution_transcript"]
            if error and not transcript.has_reply_output():
                transcript.set_reply_text(error.get("message") or "执行失败")
            elif error:
                transcript.append_process_note(f"\n[错误] {error.get('message', '执行失败')}\n")
        self._finalize_execution_from_terminal_signal(
            binding[0],
            binding[1],
            thread_id=thread_id,
            turn_id=turn_id or state["current_turn_id"],
        )

    def _append_log_by_thread(self, thread_id: str, text: str) -> None:
        binding = self._chat_binding_key_by_thread_id.get(thread_id)
        if not binding:
            return
        state = self._get_runtime_state(*binding)
        with self._lock:
            state["execution_transcript"].append_process_delta(text)
        self._schedule_execution_card_update(*binding)

    def _send_execution_card(self, chat_id: str, parent_message_id: str) -> str | None:
        card = build_execution_card("", [], running=True)
        content = json.dumps(card, ensure_ascii=False)
        if parent_message_id:
            return self.bot.reply_to_message(parent_message_id, "interactive", content)
        return self.bot.send_message_get_id(chat_id, "interactive", content)

    def _patch_execution_card_message(
        self,
        message_id: str,
        *,
        transcript: ExecutionTranscript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        card = build_execution_card(
            self._card_log_text(transcript.process_text()),
            self._card_reply_segments(transcript),
            running=running,
            elapsed=elapsed,
            cancelled=cancelled and not running,
        )
        return self.bot.patch_message(normalized_message_id, json.dumps(card, ensure_ascii=False))

    def _schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        message_id = state["current_message_id"]
        if not message_id:
            return
        now = time.monotonic()
        with self._lock:
            last_patch = state["last_patch_at"]
            timer = state["patch_timer"]
            if now - last_patch >= self._stream_patch_interval_ms / 1000:
                state["last_patch_at"] = now
                self._cancel_patch_timer_locked(state)
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
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            self._cancel_patch_timer_locked(state)
            message_id = state["current_message_id"]
            if not message_id:
                return
            transcript = state["execution_transcript"].clone()
            reply_text = transcript.reply_text()
            running = state["running"]
            cancelled = state["cancelled"]
            prompt_message_id = state["current_prompt_message_id"].strip()
            elapsed = int(max(0.0, time.monotonic() - state["started_at"])) if state["started_at"] else 0
            state["last_patch_at"] = time.monotonic()

        ok = self._patch_execution_card_message(
            message_id,
            transcript=transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
        )
        if not ok and immediate and reply_text:
            self._reply_text(chat_id, reply_text, message_id=prompt_message_id)

    def _send_followup_if_needed(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if state["followup_sent"]:
                return
            reply_text = state["execution_transcript"].reply_text()
            current_message_id = state["current_message_id"]
            prompt_message_id = state["current_prompt_message_id"].strip()
            need_followup = not current_message_id or len(reply_text) > self._card_reply_limit
            if not reply_text or not need_followup:
                return
            state["followup_sent"] = True
        self._reply_text(chat_id, reply_text, message_id=prompt_message_id)

    def _clear_plan_state(self, state: _RuntimeState) -> None:
        state["plan_message_id"] = ""
        state["plan_turn_id"] = ""
        state["plan_explanation"] = ""
        state["plan_steps"] = []
        state["plan_text"] = ""

    def _flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            plan_message_id = state["plan_message_id"]
            parent_message_id = state["current_message_id"]
            turn_id = state["plan_turn_id"]
            explanation = state["plan_explanation"]
            plan_steps = list(state["plan_steps"])
            plan_text = state["plan_text"]
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
                if state["plan_message_id"] == plan_message_id:
                    state["plan_message_id"] = ""

        new_message_id: str | None = None
        if parent_message_id:
            new_message_id = self.bot.reply_to_message(parent_message_id, "interactive", content)
        if not new_message_id:
            new_message_id = self.bot.send_message_get_id(chat_id, "interactive", content)
        if new_message_id:
            with self._lock:
                state["plan_message_id"] = new_message_id

    def _card_log_text(self, text: str) -> str:
        if len(text) <= self._card_log_limit:
            return text
        return text[-self._card_log_limit :] + "\n\n**[日志已截断，仅保留最近部分]**"
