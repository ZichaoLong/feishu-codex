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
from typing import Any, Callable, TypedDict, TypeAlias
from uuid import UUID

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.base import RuntimeConfigSummary, ThreadSnapshot, ThreadSummary
from bot.adapter_notification_controller import AdapterNotificationController
from bot.cards import (
    CommandResult,
    build_history_preview_card,
    build_markdown_card,
    make_card_response,
)
from bot.binding_runtime_manager import BindingRuntimeManager, ResolvedRuntimeBinding
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
from bot.execution_transcript import ExecutionTranscript
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_recovery_controller import ExecutionRecoveryController, TerminalReconcileTarget
from bot.file_message_domain import FileMessageDomain, IncomingFileMessage
from bot.interaction_request_controller import InteractionRequestController
from bot.inbound_surface_controller import ActionRoute, CommandRoute, InboundSurfaceController
from bot.prompt_turn_entry_controller import PromptTurnEntryController
from bot.runtime_admin_controller import RuntimeAdminController
from bot.runtime_card_publisher import (
    RuntimeCardPublisher,
)
from bot.runtime_state import (
    UNSET,
    BindingActivated,
    ExecutionStateChanged,
    RuntimeSettingsChanged,
    RuntimeStateMessage,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.runtime_view import RuntimeView
from bot.service_control_plane import ServiceControlPlane
from bot.session_resolution import (
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseStore,
)
from bot.stores.profile_state_store import ProfileStateStore
from bot.stores.service_instance_lease import (
    ServiceInstanceLease,
    ServiceInstanceLeaseError,
)
from bot.thread_lease_registry import ThreadLeaseRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from bot.runtime_loop import RuntimeLoop, RuntimeLoopClosedError

logger = logging.getLogger(__name__)

_CARD_REPLY_LIMIT_DEFAULT = 12000
_CARD_LOG_LIMIT_DEFAULT = 8000
_MIRROR_WATCHDOG_SECONDS_DEFAULT = 8.0
_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}
_SANDBOX_POLICIES = {"read-only", "workspace-write", "danger-full-access"}
_LOCAL_THREAD_SAFETY_RULE = (
    "同一线程允许多端订阅观察，但同一 live turn 只有一个交互 owner；非 owner 只能看，不能写或处理审批。"
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

class _PlanStepState(TypedDict):
    step: str
    status: str


class _RuntimeState(TypedDict):
    active: bool
    working_dir: str
    current_thread_id: str
    current_thread_title: str
    current_thread_runtime_state: str
    current_turn_id: str
    running: bool
    cancelled: bool
    pending_cancel: bool
    current_message_id: str
    last_execution_message_id: str
    current_prompt_message_id: str
    current_prompt_reply_in_thread: bool
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
    status: str


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
        self._thread_lease_registry = ThreadLeaseRegistry()
        self._interaction_lease_store = InteractionLeaseStore(self._data_dir)
        self._pending_requests: dict[str, _PendingRequestState] = {}
        self._pending_rename_forms: dict[str, _PendingRenameFormState] = {}
        self._runtime_loop = RuntimeLoop(name="codex-handler-runtime")
        self._service_instance_lease = ServiceInstanceLease(self._data_dir)
        self._service_control_plane = ServiceControlPlane(
            data_dir=self._data_dir,
            dispatch=self._handle_service_control_request,
            owns_socket_path=self._service_instance_lease.owns_socket_path,
        )
        self._last_runtime_config: RuntimeConfigSummary | None = None

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
        self._binding_runtime = BindingRuntimeManager(
            lock=self._lock,
            default_working_dir=self._default_working_dir,
            default_approval_policy=self._adapter_config.approval_policy,
            default_sandbox=self._adapter_config.sandbox,
            default_collaboration_mode=self._adapter_config.collaboration_mode,
            default_model=self._adapter_config.model,
            default_reasoning_effort=self._adapter_config.reasoning_effort,
            chat_binding_store=self._chat_binding_store,
            thread_lease_registry=self._thread_lease_registry,
            interaction_lease_store=self._interaction_lease_store,
            is_group_chat=self._is_group_chat,
        )
        self._turn_execution = TurnExecutionCoordinator()
        self._execution_output = ExecutionOutputController(
            lock=self._lock,
            runtime_submit=self._runtime_submit,
            turn_execution=self._turn_execution,
            get_runtime_state=lambda sender_id, chat_id: self._get_runtime_state(sender_id, chat_id),
            get_runtime_view=lambda sender_id, chat_id: self._get_runtime_view(sender_id, chat_id),
            apply_runtime_state_message_locked=self._apply_runtime_state_message_locked,
            cancel_patch_timer_locked=self._cancel_patch_timer_locked,
            card_publisher_factory=self._runtime_card_publisher,
            reply_text=self._reply_text,
            card_reply_limit=lambda: self._card_reply_limit,
            card_log_limit=lambda: self._card_log_limit,
            stream_patch_interval_ms=lambda: self._stream_patch_interval_ms,
        )
        self._execution_recovery = ExecutionRecoveryController(
            lock=self._lock,
            runtime_submit=self._runtime_submit,
            turn_execution=self._turn_execution,
            get_runtime_state=lambda sender_id, chat_id: self._get_runtime_state(sender_id, chat_id),
            resolve_runtime_binding=lambda sender_id, chat_id: self._resolve_runtime_binding(sender_id, chat_id),
            apply_runtime_state_message_locked=self._apply_runtime_state_message_locked,
            apply_persisted_runtime_state_message_locked=self._apply_persisted_runtime_state_message_locked,
            finalize_execution_card_from_state=self._finalize_execution_card_from_state,
            patch_execution_card_message=self._patch_execution_card_message,
            read_thread=lambda thread_id: self._adapter.read_thread(thread_id, include_turns=True),
            is_thread_not_found_error=self._is_thread_not_found_error,
            is_turn_thread_not_found_error=self._is_turn_thread_not_found_error,
            is_transport_disconnect=self._is_transport_disconnect,
            is_request_timeout_error=self._is_request_timeout_error,
            runtime_recovery_reason=self._runtime_recovery_reason,
            mirror_watchdog_seconds=lambda: self._mirror_watchdog_seconds,
        )
        self._interaction_requests = InteractionRequestController(
            lock=self._lock,
            pending_requests=self._pending_requests,
            get_runtime_state=lambda sender_id, chat_id: self._get_runtime_state(sender_id, chat_id),
            interactive_binding_for_thread=lambda thread_id, adopt_sole_subscriber: self._interactive_binding_for_thread(
                thread_id,
                adopt_sole_subscriber=adopt_sole_subscriber,
            ),
            send_interactive_card=lambda chat_id, card, prompt_message_id, prompt_reply_in_thread: (
                self.bot.reply_to_message(
                    prompt_message_id,
                    "interactive",
                    json.dumps(card, ensure_ascii=False),
                    reply_in_thread=prompt_reply_in_thread,
                )
                if prompt_message_id
                else self.bot.send_message_get_id(
                    chat_id,
                    "interactive",
                    json.dumps(card, ensure_ascii=False),
                )
            ),
            reply_text=self._reply_text,
            respond=lambda request_id, result=None, error=None: self._adapter.respond(
                request_id,
                result=result,
                error=error,
            ),
            patch_message=lambda message_id, content: self.bot.patch_message(message_id, content),
        )
        self._runtime_state_by_binding: dict[ChatBindingKey, _RuntimeState] = self._binding_runtime.runtime_state_by_binding
        self._adapter_notifications = AdapterNotificationController(
            lock=self._lock,
            turn_execution=self._turn_execution,
            execution_binding_for_thread=lambda thread_id, adopt_sole_subscriber: self._execution_binding_for_thread(
                thread_id,
                adopt_sole_subscriber=adopt_sole_subscriber,
            ),
            thread_subscribers=self._thread_subscribers,
            thread_write_owner=self._thread_write_owner,
            get_runtime_state=lambda sender_id, chat_id: self._get_runtime_state(sender_id, chat_id),
            note_runtime_event=self._note_runtime_event,
            apply_runtime_state_message_locked=self._apply_runtime_state_message_locked,
            apply_persisted_runtime_state_message_locked=self._apply_persisted_runtime_state_message_locked,
            cancel_mirror_watchdog_locked=self._cancel_mirror_watchdog_locked,
            finalize_execution_from_terminal_signal=self._finalize_execution_from_terminal_signal,
            patch_execution_card_message=self._patch_execution_card_message,
            send_execution_card=self._send_execution_card,
            schedule_mirror_watchdog=self._schedule_mirror_watchdog,
            schedule_execution_card_update=self._schedule_execution_card_update,
            flush_execution_card=self._flush_execution_card,
            flush_plan_card=self._flush_plan_card,
            interrupt_running_turn=self._interrupt_running_turn,
            on_server_request_resolved=self._interaction_requests.handle_server_request_resolved,
        )
        self._runtime_admin = RuntimeAdminController(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            interaction_requests=self._interaction_requests,
            runtime_state_by_binding=self._runtime_state_by_binding,
            clear_all_stored_bindings=self._chat_binding_store.clear_all,
            deactivate_binding_locked=self._deactivate_binding_locked,
            read_thread=lambda thread_id: self._adapter.read_thread(thread_id, include_turns=False),
            list_loaded_thread_ids=lambda: self._adapter.list_loaded_thread_ids(),
            current_app_server_url=lambda: self._adapter.current_app_server_url(),
            unsubscribe_thread=lambda thread_id: self._adapter.unsubscribe_thread(thread_id),
            service_control_socket_path=lambda: str(self._service_control_plane.socket_path),
            safe_read_runtime_config=self._safe_read_runtime_config,
            current_default_profile_resolution=self._current_default_profile_resolution,
            permissions_summary=_permissions_summary,
            resolve_thread_target_for_control_params=self._resolve_thread_target_for_control_params,
            cancel_patch_timer_locked=self._cancel_patch_timer_locked,
            cancel_mirror_watchdog_locked=self._cancel_mirror_watchdog_locked,
            is_thread_not_found_error=self._is_thread_not_found_error,
        )
        self._hydrate_stored_bindings()
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
            local_thread_safety_rule=_LOCAL_THREAD_SAFETY_RULE,
        )
        self._session_ui_domain = CodexSessionUiDomain(self)
        self._file_message_domain = FileMessageDomain(self)
        self._prompt_turn_entry = PromptTurnEntryController(
            lock=self._lock,
            turn_execution=self._turn_execution,
            resolve_runtime_binding=lambda sender_id, chat_id, message_id="": self._resolve_runtime_binding(
                sender_id,
                chat_id,
                message_id,
            ),
            get_runtime_state=lambda sender_id, chat_id, message_id="": self._get_runtime_state(
                sender_id,
                chat_id,
                message_id,
            ),
            get_runtime_view=lambda sender_id, chat_id, message_id="": self._get_runtime_view(
                sender_id,
                chat_id,
                message_id,
            ),
            bind_thread=lambda sender_id, chat_id, thread, message_id="": self._bind_thread(
                sender_id,
                chat_id,
                thread,
                message_id=message_id,
            ),
            clear_thread_binding=lambda sender_id, chat_id, message_id="": self._clear_thread_binding(
                sender_id,
                chat_id,
                message_id=message_id,
            ),
            resume_snapshot_by_id=self._resume_snapshot_by_id,
            create_thread=lambda **kwargs: self._adapter.create_thread(**kwargs),
            effective_default_profile=self._effective_default_profile,
            message_reply_in_thread=self._message_reply_in_thread,
            group_actor_open_id=self._group_actor_open_id,
            prompt_write_denial_text=self._prompt_write_denial_text,
            thread_sharing_policy_violation=self._thread_sharing_policy_violation,
            interaction_denied_text=self._interaction_denied_text,
            acquire_interaction_lease_for_binding=self._acquire_interaction_lease_for_binding,
            release_interaction_lease_for_binding=self._release_interaction_lease_for_binding,
            acquire_thread_write_lease_locked=self._acquire_thread_write_lease_locked,
            sync_stored_binding_locked=self._sync_stored_binding_locked,
            clear_plan_state=self._clear_plan_state,
            apply_runtime_state_message_locked=self._apply_runtime_state_message_locked,
            claim_reserved_execution_card=self._claim_reserved_execution_card,
            patch_message=lambda message_id, content: self.bot.patch_message(message_id, content),
            card_publisher_factory=self._runtime_card_publisher,
            send_execution_card=self._send_execution_card,
            flush_execution_card=self._flush_execution_card,
            retire_execution_anchor=self._retire_execution_anchor,
            schedule_mirror_watchdog=self._schedule_mirror_watchdog,
            reconcile_execution_snapshot=self._reconcile_execution_snapshot,
            refresh_terminal_execution_card_from_state=self._refresh_terminal_execution_card_from_state,
            finalize_execution_card_from_state=self._finalize_execution_card_from_state,
            mark_runtime_degraded=self._mark_runtime_degraded,
            runtime_recovery_reason=self._runtime_recovery_reason,
            is_turn_thread_not_found_error=self._is_turn_thread_not_found_error,
            is_thread_not_found_error=self._is_thread_not_found_error,
            is_transport_disconnect=self._is_transport_disconnect,
            is_request_timeout_error=self._is_request_timeout_error,
            start_turn=lambda **kwargs: self._adapter.start_turn(**kwargs),
            interrupt_running_turn=self._interrupt_running_turn,
            reply_text=self._reply_text,
            mirror_watchdog_seconds=lambda: self._mirror_watchdog_seconds,
            card_reply_limit=lambda: self._card_reply_limit,
            card_log_limit=lambda: self._card_log_limit,
        )
        self._inbound_surface = InboundSurfaceController(
            keyword=KEYWORD,
            activate_binding_if_needed=self._activate_binding_if_needed,
            help_reply=lambda chat_id, message_id: self._help_domain.reply_help(
                chat_id,
                message_id=message_id,
            ),
            handle_prompt=lambda sender_id, chat_id, text, message_id: self._handle_prompt(
                sender_id,
                chat_id,
                text,
                message_id=message_id,
            ),
            reply_text=self._reply_text,
            reply_card=self._reply_card,
            resolve_chat_type=self._resolve_chat_type,
            group_command_admin_denial_text=self._group_command_admin_denial_text,
            is_group_chat=self._is_group_chat,
            is_group_admin_actor=self._is_group_admin_actor,
            is_group_turn_actor=self._is_group_turn_actor,
            is_group_request_actor_or_admin=self._is_group_request_actor_or_admin,
            handle_rename_form_fallback=self._handle_rename_form_fallback,
            handle_user_input_form_fallback=self._handle_user_input_form_fallback,
        )
        self._inbound_surface.install_routes(
            command_routes=self._build_command_routes(),
            action_routes=self._build_action_routes(),
            prefixed_action_routes=self._build_prefixed_action_routes(),
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

    def _runtime_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return self._runtime_loop.call(fn, *args, **kwargs)
        except RuntimeLoopClosedError:
            logger.debug("handler runtime loop already closed; dropping sync call %s", getattr(fn, "__name__", fn))
            raise

    def _runtime_submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            self._runtime_loop.submit(fn, *args, **kwargs)
        except RuntimeLoopClosedError:
            logger.debug(
                "handler runtime loop already closed; dropping async call %s",
                getattr(fn, "__name__", fn),
            )

    def on_register(self, bot) -> None:
        super().on_register(bot)
        try:
            self._service_instance_lease.acquire(socket_path=self._service_control_plane.socket_path)
            self._runtime_loop.start()
            self._adapter.start()
            self._service_control_plane.start()
        except ServiceInstanceLeaseError:
            logger.exception("启动 feishu-codex service 失败：当前 FC_DATA_DIR 已被其他实例占用")
            raise
        except Exception:
            logger.exception("启动 Codex app-server 失败")
            try:
                self._service_control_plane.stop()
            except Exception:
                logger.exception("回滚本地控制面失败")
            try:
                self._adapter.stop()
            except Exception:
                logger.exception("回滚 Codex adapter 失败")
            try:
                self._runtime_loop.stop()
            except Exception:
                logger.exception("回滚 handler runtime loop 失败")
            self._service_instance_lease.release()
            raise

    def handle_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._runtime_call(self._handle_message_impl, sender_id, chat_id, text, message_id=message_id)

    def _handle_message_impl(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._inbound_surface.handle_message(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
        )

    def _activate_binding_if_needed(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        with self._lock:
            if not state["active"]:
                self._apply_runtime_state_message_locked(state, BindingActivated())

    def handle_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        return self._runtime_call(
            self._handle_card_action_impl,
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def _handle_card_action_impl(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        return self._inbound_surface.handle_card_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def handle_file_message(
        self, sender_id: str, chat_id: str, message_id: str, file_key: str, file_name: str
    ) -> None:
        self._runtime_call(
            self._handle_file_message_impl,
            sender_id,
            chat_id,
            message_id,
            file_key,
            file_name,
        )

    def _handle_file_message_impl(
        self, sender_id: str, chat_id: str, message_id: str, file_key: str, file_name: str
    ) -> None:
        self._file_message_domain.handle_message(
            IncomingFileMessage(
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=message_id,
                file_key=file_key,
                file_name=file_name,
            )
        )

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

        with self._lock:
            pending_request = self._interaction_requests.find_user_input_request_by_message_locked(message_id)
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

    def _deactivate_binding_locked(self, key: ChatBindingKey) -> str:
        return self._binding_runtime.deactivate_binding_locked(
            key,
            on_deactivate_state=lambda state: (
                self._cancel_patch_timer_locked(state),
                self._cancel_mirror_watchdog_locked(state),
            ),
        )

    def deactivate_sender(self, sender_id: str, chat_id: str = "", message_id: str = "") -> None:
        key = self._chat_binding_key(sender_id, chat_id, message_id)
        unsubscribe_thread_id: str = ""
        with self._lock:
            unsubscribe_thread_id = self._deactivate_binding_locked(key)
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)

    def preflight_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._runtime_call(
            self._preflight_group_prompt_impl,
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def handle_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        self._runtime_call(self._handle_chat_unavailable_impl, chat_id, reason=reason)

    def _handle_chat_unavailable_impl(self, chat_id: str, *, reason: str = "") -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        unsubscribe_thread_ids: list[str] = []
        with self._lock:
            binding_keys = [
                binding
                for binding in self._runtime_state_by_binding
                if binding[1] == normalized_chat_id
            ]
            for binding in binding_keys:
                unsubscribe_thread_id = self._deactivate_binding_locked(binding)
                if unsubscribe_thread_id:
                    unsubscribe_thread_ids.append(unsubscribe_thread_id)
        for unsubscribe_thread_id in sorted(set(unsubscribe_thread_ids)):
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)
        pending_fail_closed = self._interaction_requests.fail_close_chat_requests(normalized_chat_id)
        logger.info(
            "chat unavailable cleanup finished: chat=%s reason=%s bindings=%s pending=%s",
            normalized_chat_id,
            reason or "-",
            len(unsubscribe_thread_ids),
            pending_fail_closed,
        )

    def shutdown(self) -> None:
        """停止底层 app-server。"""
        with self._lock:
            for state in self._runtime_state_by_binding.values():
                self._cancel_patch_timer_locked(state)
                self._cancel_mirror_watchdog_locked(state)
        try:
            self._service_control_plane.stop()
        except Exception:
            logger.exception("停止本地控制面失败")
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("停止 Codex adapter 失败")
        finally:
            self._runtime_loop.stop()
            self._service_instance_lease.release()

    def _build_default_runtime_state(self) -> _RuntimeState:
        return self._binding_runtime.build_default_runtime_state()  # type: ignore[return-value]

    def _build_default_stored_binding(self) -> dict[str, str]:
        return self._binding_runtime.build_default_stored_binding()

    def _hydrate_stored_bindings(self) -> None:
        self._binding_runtime.hydrate_stored_bindings()

    def _apply_stored_binding(self, state: _RuntimeState, stored_binding: dict[str, str]) -> None:
        self._binding_runtime.apply_stored_binding(state, stored_binding)

    def _subscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._binding_runtime.subscribe_thread_locked(binding, thread_id)

    def _unsubscribe_thread_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._binding_runtime.unsubscribe_thread_locked(binding, thread_id)

    def _acquire_thread_write_lease_locked(self, binding: ChatBindingKey, thread_id: str):
        return self._binding_runtime.acquire_thread_write_lease_locked(binding, thread_id)

    def _release_thread_write_lease_locked(self, binding: ChatBindingKey, thread_id: str) -> bool:
        return self._binding_runtime.release_thread_write_lease_locked(binding, thread_id)

    def _feishu_interaction_holder(self, binding: ChatBindingKey):
        return self._binding_runtime.feishu_interaction_holder(binding)

    def _current_interaction_lease_locked(self, thread_id: str) -> InteractionLease | None:
        return self._binding_runtime.current_interaction_lease_locked(thread_id)

    def _acquire_interaction_lease_for_binding(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> InteractionLeaseAcquireResult:
        return self._binding_runtime.acquire_interaction_lease_for_binding(binding, thread_id)

    def _release_interaction_lease_for_binding(
        self,
        binding: ChatBindingKey,
        thread_id: str,
    ) -> bool:
        return self._binding_runtime.release_interaction_lease_for_binding(binding, thread_id)

    def _interactive_binding_for_thread_locked(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> tuple[ChatBindingKey | None, bool]:
        return self._binding_runtime.interactive_binding_for_thread_locked(
            thread_id,
            adopt_sole_subscriber=adopt_sole_subscriber,
        )

    def _interactive_binding_for_thread(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> tuple[ChatBindingKey | None, bool]:
        with self._lock:
            return self._interactive_binding_for_thread_locked(
                thread_id,
                adopt_sole_subscriber=adopt_sole_subscriber,
            )

    def _execution_binding_for_thread_locked(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> ChatBindingKey | None:
        return self._binding_runtime.execution_binding_for_thread_locked(
            thread_id,
            adopt_sole_subscriber=adopt_sole_subscriber,
        )

    def _execution_binding_for_thread(
        self,
        thread_id: str,
        *,
        adopt_sole_subscriber: bool = False,
    ) -> ChatBindingKey | None:
        with self._lock:
            return self._execution_binding_for_thread_locked(
                thread_id,
                adopt_sole_subscriber=adopt_sole_subscriber,
            )

    def _thread_subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        with self._lock:
            return self._binding_runtime.thread_subscribers(thread_id)

    def _thread_write_owner(self, thread_id: str) -> ChatBindingKey | None:
        with self._lock:
            return self._binding_runtime.thread_write_owner(thread_id)

    def _stored_binding_from_runtime(self, binding: ChatBindingKey, state: _RuntimeState) -> dict[str, str]:
        return self._binding_runtime.stored_binding_from_runtime(binding, state)

    def _sync_stored_binding_locked(self, binding: ChatBindingKey, state: _RuntimeState) -> None:
        self._binding_runtime.sync_stored_binding_locked(binding, state)

    def _save_stored_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        self._binding_runtime.save_stored_binding(sender_id, chat_id, message_id)

    def _get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView:
        return self._binding_runtime.get_runtime_view(sender_id, chat_id, message_id)

    def _runtime_card_publisher(self) -> RuntimeCardPublisher:
        return RuntimeCardPublisher(self.bot)

    @staticmethod
    def _apply_runtime_state_message_locked(state: _RuntimeState, message: RuntimeStateMessage) -> None:
        apply_runtime_state_message(state, message)

    def _apply_persisted_runtime_state_message_locked(
        self,
        binding: ChatBindingKey,
        state: _RuntimeState,
        message: RuntimeStateMessage,
    ) -> None:
        self._binding_runtime.apply_persisted_runtime_state_message_locked(binding, state, message)

    def _update_runtime_settings(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        approval_policy: Any = UNSET,
        sandbox: Any = UNSET,
        collaboration_mode: Any = UNSET,
    ) -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        with self._lock:
            self._apply_persisted_runtime_state_message_locked(
                resolved.binding,
                resolved.state,
                RuntimeSettingsChanged(
                    approval_policy=approval_policy,
                    sandbox=sandbox,
                    collaboration_mode=collaboration_mode,
                ),
            )

    def _rename_bound_thread_title(
        self,
        sender_id: str,
        chat_id: str,
        title: str,
        *,
        message_id: str = "",
        thread_id: str = "",
    ) -> bool:
        normalized_title = str(title or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        with self._lock:
            if normalized_thread_id and state["current_thread_id"] != normalized_thread_id:
                return False
            if not state["current_thread_id"]:
                return False
            self._apply_persisted_runtime_state_message_locked(
                resolved.binding,
                state,
                ThreadStateChanged(current_thread_title=normalized_title),
            )
        return True

    @staticmethod
    def _cancel_timer(timer: threading.Timer | None) -> None:
        if timer is not None:
            timer.cancel()

    def _cancel_patch_timer_locked(self, state: _RuntimeState) -> None:
        self._cancel_timer(state["patch_timer"])
        self._apply_runtime_state_message_locked(state, ExecutionStateChanged(patch_timer=None))

    def _cancel_mirror_watchdog_locked(self, state: _RuntimeState) -> None:
        self._execution_recovery.cancel_mirror_watchdog_locked(state)

    @staticmethod
    def _has_active_execution_locked(state: _RuntimeState) -> bool:
        return TurnExecutionCoordinator.has_active_execution_locked(state)

    def _clear_execution_anchor_locked(self, state: _RuntimeState, *, clear_card_message: bool) -> None:
        self._turn_execution.clear_execution_anchor_locked(
            state,
            clear_card_message=clear_card_message,
        )

    def _reset_execution_context_locked(self, state: _RuntimeState, *, clear_card_message: bool) -> None:
        self._turn_execution.reset_execution_context_locked(
            state,
            clear_card_message=clear_card_message,
        )

    def _retire_execution_anchor(self, sender_id: str, chat_id: str) -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id)
        state = resolved.state
        with self._lock:
            self._release_thread_write_lease_locked(resolved.binding, state["current_thread_id"])
            self._release_interaction_lease_for_binding(resolved.binding, state["current_thread_id"])
            self._turn_execution.retire_execution_locked(state)
            self._sync_stored_binding_locked(resolved.binding, state)

    def _refresh_terminal_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        return self._execution_output.refresh_terminal_execution_card_from_state(sender_id, chat_id)

    def _capture_terminal_reconcile_target(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> TerminalReconcileTarget | None:
        return self._execution_recovery.capture_terminal_reconcile_target(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def _schedule_terminal_execution_reconcile(self, target: TerminalReconcileTarget | None) -> None:
        self._execution_recovery.schedule_terminal_execution_reconcile(target)

    def _run_terminal_execution_reconcile(self, target: TerminalReconcileTarget) -> None:
        self._execution_recovery.run_terminal_execution_reconcile(target)

    def _mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        self._execution_recovery.mark_runtime_degraded(sender_id, chat_id, reason=reason)

    def _note_runtime_event(self, sender_id: str, chat_id: str) -> None:
        self._execution_recovery.note_runtime_event(sender_id, chat_id)

    def _schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        self._execution_recovery.schedule_mirror_watchdog(sender_id, chat_id)

    def _submit_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        self._execution_recovery.submit_mirror_watchdog(sender_id, chat_id, generation)

    def _run_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        self._execution_recovery.run_mirror_watchdog(sender_id, chat_id, generation)

    def _existing_chat_binding_key_locked(self, sender_id: str, chat_id: str) -> ChatBindingKey | None:
        return self._binding_runtime.existing_chat_binding_key_locked(sender_id, chat_id)

    def _fresh_chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        return self._binding_runtime.fresh_chat_binding_key(sender_id, chat_id, message_id)

    def _get_or_create_runtime_state_locked(self, binding: ChatBindingKey) -> _RuntimeState:
        return self._binding_runtime.get_or_create_runtime_state_locked(binding)  # type: ignore[return-value]

    def _resolve_runtime_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> ResolvedRuntimeBinding:
        return self._binding_runtime.resolve_runtime_binding(sender_id, chat_id, message_id)

    def _get_runtime_state(self, sender_id: str, chat_id: str, message_id: str = "") -> _RuntimeState:
        return self._binding_runtime.get_runtime_state(sender_id, chat_id, message_id)  # type: ignore[return-value]

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

    def _thread_sharing_policy_violation(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_thread_id or not normalized_chat_id:
            return ""
        current_mode = str(current_chat_mode or "").strip().lower()
        if not current_mode and self._is_group_chat(normalized_chat_id, message_id):
            current_mode = str(self.bot.get_group_mode(normalized_chat_id) or "").strip().lower()
        with self._lock:
            subscribers = self._thread_lease_registry.subscribers(normalized_thread_id)
        other_chat_ids = sorted({binding[1] for binding in subscribers if binding[1] != normalized_chat_id})
        if current_mode == "all" and other_chat_ids:
            return (
                "当前群聊处于 `all` 模式；该模式下线程不能与其他飞书会话共享。"
                "请先切到 `assistant` 或 `mention-only`，或为本群新建线程。"
            )
        for binding in subscribers:
            if binding[1] == normalized_chat_id:
                continue
            if binding[0] != GROUP_SHARED_BINDING_OWNER_ID:
                continue
            if str(self.bot.get_group_mode(binding[1]) or "").strip().lower() != "all":
                continue
            return (
                "该线程当前已被处于 `all` 模式的其他群聊独占；"
                "请先为本会话新建线程，或让对方切回 `assistant` / `mention-only`。"
            )
        return ""

    def _validate_group_mode_change(self, chat_id: str, mode: str, *, message_id: str = "") -> str:
        normalized_mode = str(mode or "").strip().lower()
        if normalized_mode != "all":
            return ""
        runtime = self._get_runtime_view(GROUP_SHARED_BINDING_OWNER_ID, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            return ""
        return self._thread_sharing_policy_violation(
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode="all",
        )

    @staticmethod
    def _write_denied_text(owner_label: str) -> str:
        return f"当前线程正由{owner_label}执行；本会话可继续查看，但暂时不能写入。待对方执行结束后再试。"

    @classmethod
    def _interaction_denied_text(cls, lease: InteractionLease | None) -> str:
        owner_label = "另一终端"
        if lease is not None and lease.holder.kind == "feishu":
            owner_label = "另一飞书会话"
        return cls._write_denied_text(owner_label)

    def _prompt_write_denial_text(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str:
        sharing_violation = self._thread_sharing_policy_violation(
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode=current_chat_mode,
        )
        if sharing_violation:
            return sharing_violation
        with self._lock:
            interaction_lease = self._current_interaction_lease_locked(thread_id)
            if interaction_lease is not None and not interaction_lease.holder.same_holder(
                self._feishu_interaction_holder(binding)
            ):
                return self._interaction_denied_text(interaction_lease)
            write_owner = self._thread_lease_registry.lease_owner(thread_id)
            if write_owner is not None and write_owner != binding:
                return self._write_denied_text("另一飞书会话")
        return ""

    def _chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        with self._lock:
            existing = self._existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return existing
        return self._fresh_chat_binding_key(sender_id, chat_id, message_id)

    def _group_actor_open_id(self, message_id: str = "", operator_open_id: str = "") -> str:
        normalized_operator_open_id = str(operator_open_id or "").strip()
        if normalized_operator_open_id:
            return normalized_operator_open_id
        if not message_id:
            return ""
        context = self.bot.get_message_context(message_id)
        return str(context.get("sender_open_id", "")).strip()

    def _message_reply_in_thread(self, message_id: str) -> bool:
        if not message_id:
            return False
        context = self.bot.get_message_context(message_id)
        return bool(str(context.get("thread_id", "") or "").strip())

    def _preflight_group_prompt_impl(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._prompt_turn_entry.preflight_group_prompt(
            sender_id,
            chat_id,
            message_id=message_id,
        )

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
        denial_text = self._group_command_admin_denial_text(chat_id, message_id=message_id)
        if not denial_text:
            return True
        self._reply_text(chat_id, denial_text, message_id=message_id)
        return False

    def _group_command_admin_denial_text(self, chat_id: str, message_id: str = "") -> str:
        if not self._is_group_chat(chat_id, message_id):
            return ""
        if self._is_group_admin_actor(chat_id, message_id=message_id):
            return ""
        return "群里的 `/` 命令仅管理员可用；已授权成员请直接提问或显式 mention 触发机器人。"

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

    def _reply_text(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        if self._is_group_chat(chat_id, message_id) and message_id:
            self.bot.reply(
                chat_id,
                text,
                parent_message_id=message_id,
                reply_in_thread=reply_in_thread,
            )
            return
        self.bot.reply(chat_id, text)

    def _reply_card(
        self,
        chat_id: str,
        card: dict,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        if self._is_group_chat(chat_id, message_id) and message_id:
            self.bot.reply_card(
                chat_id,
                card,
                parent_message_id=message_id,
                reply_in_thread=reply_in_thread,
            )
            return
        self.bot.reply_card(chat_id, card)

    def _claim_reserved_execution_card(self, trigger_message_id: str) -> str:
        if not trigger_message_id or not hasattr(self.bot, "claim_reserved_execution_card"):
            return ""
        return str(self.bot.claim_reserved_execution_card(trigger_message_id) or "").strip()

    def _build_command_routes(self) -> dict[str, CommandRoute]:
        return {
            "/help": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/h": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, message_id=message_id
                ),
            ),
            "/init": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_init_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/init <token>`。",
            ),
            "/pwd": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=f"当前目录：`{display_path(self._get_runtime_state(sender_id, chat_id, message_id)['working_dir'])}`",
                ),
            ),
            "/cd": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_cd_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/new": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_new_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/status": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_status_command(
                    sender_id, chat_id, message_id=message_id
                ),
            ),
            "/release-feishu-runtime": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_release_feishu_runtime_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/whoami": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_whoami_command(
                    sender_id, chat_id, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text="请私聊机器人执行 `/whoami`。",
            ),
            "/whoareyou": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_botinfo_command(
                    chat_id, message_id=message_id
                ),
            ),
            "/profile": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_profile_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/cancel": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=self._cancel_current_turn(sender_id, chat_id, message_id=message_id)[1],
                ),
            ),
            "/session": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: (
                    CommandResult(
                        text="用法：`/session`\n说明：该命令不接受额外参数；发送 `/help session` 查看会话相关操作。"
                    )
                    if arg.strip()
                    else self._session_ui_domain.handle_session_command(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                    )
                ),
            ),
            "/resume": CommandRoute(
                handler=self._session_ui_domain.handle_resume_command,
            ),
            "/rm": CommandRoute(
                handler=self._session_ui_domain.handle_rm_command,
            ),
            "/rename": CommandRoute(
                handler=self._session_ui_domain.handle_rename_command,
            ),
            "/approval": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_approval_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/sandbox": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_sandbox_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/permissions": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_permissions_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/mode": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_mode_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/groupmode": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_groupmode_command(
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
                scope="group",
            ),
            "/acl": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_acl_command(
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
                scope="group",
            ),
        }

    def _build_action_routes(self) -> dict[str, ActionRoute]:
        return {
            "cancel_turn": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._handle_cancel_action(
                    sender_id, chat_id
                ),
                group_guard="turn_actor",
            ),
            "resume_thread": ActionRoute(
                handler=self._session_ui_domain.handle_resume_thread_action,
                group_guard="group_admin",
            ),
            "show_more_sessions": ActionRoute(
                handler=self._session_ui_domain.handle_show_more_sessions_action,
                group_guard="group_admin",
            ),
            "close_sessions_card": ActionRoute(
                handler=self._session_ui_domain.handle_close_sessions_card_action,
                group_guard="group_admin",
            ),
            "reopen_sessions_card": ActionRoute(
                handler=self._session_ui_domain.handle_reopen_sessions_card_action,
                group_guard="group_admin",
            ),
            "show_help_page": ActionRoute(
                handler=self._help_domain.handle_show_help_page_action,
            ),
            "help_execute_command": ActionRoute(
                handler=self._inbound_surface.handle_help_execute_command_action,
            ),
            "help_submit_command": ActionRoute(
                handler=self._inbound_surface.handle_help_submit_command_action,
            ),
            "show_permissions_card": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_show_permissions_card_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "show_mode_card": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_show_mode_card_action(
                    sender_id, chat_id, message_id
                ),
                group_guard="group_admin",
            ),
            "show_group_mode_card": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_show_group_mode_card_action(
                    chat_id,
                    message_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
            "archive_thread": ActionRoute(
                handler=self._session_ui_domain.handle_archive_thread_action,
                group_guard="group_admin",
            ),
            "show_rename_form": ActionRoute(
                handler=self._session_ui_domain.handle_show_rename_action,
                group_guard="group_admin",
            ),
            "rename_thread": ActionRoute(
                handler=self._session_ui_domain.handle_rename_submit_action,
                group_guard="group_admin",
            ),
            "cancel_rename": ActionRoute(
                handler=self._session_ui_domain.handle_cancel_rename_action,
                group_guard="group_admin",
            ),
            "set_approval_policy": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_approval_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_sandbox_policy": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_sandbox_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_permissions_preset": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_permissions_preset(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_profile": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_profile(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_collaboration_mode": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_collaboration_mode(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_group_mode": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_set_group_mode_action(
                    chat_id,
                    message_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
            "set_group_acl_policy": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_set_group_acl_policy_action(
                    chat_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
        }

    def _build_prefixed_action_routes(self) -> list[tuple[str, ActionRoute]]:
        approval_route = ActionRoute(
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
                ActionRoute(
                    handler=lambda sender_id, chat_id, message_id, action_value: self._handle_user_input_action(
                        action_value
                    ),
                    group_guard="request_actor_or_admin",
                ),
            ),
        ]

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

    @staticmethod
    def _snapshot_reply(snapshot: ThreadSnapshot, *, turn_id: str = "") -> tuple[str, list[dict[str, Any]]]:
        return ExecutionRecoveryController.snapshot_reply(snapshot, turn_id=turn_id)

    def _finalize_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            transition = self._turn_execution.prepare_finalize_locked(state)
            self._cancel_mirror_watchdog_locked(state)
        if not transition.had_card:
            self._retire_execution_anchor(sender_id, chat_id)
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
            self._notify_non_owner_turn_finished(
                self._chat_binding_key(sender_id, chat_id),
                thread_id=thread_id,
            )
            self._schedule_terminal_execution_reconcile(target)
        return finalized

    def _notify_non_owner_turn_finished(
        self,
        owner_binding: ChatBindingKey,
        *,
        thread_id: str,
    ) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        notifications: list[str] = []
        with self._lock:
            for binding in self._thread_lease_registry.subscribers(normalized_thread_id):
                if binding == owner_binding:
                    continue
                state = self._runtime_state_by_binding.get(binding)
                if state is None or not state["active"]:
                    continue
                if str(state["current_thread_id"] or "").strip() != normalized_thread_id:
                    continue
                notifications.append(binding[1])
        if not notifications:
            return
        message = f"线程 `{normalized_thread_id[:8]}…` 的上一轮执行已结束；本会话现在可继续提问。"
        for chat_id in notifications:
            self.bot.reply(chat_id, message)

    def _reconcile_execution_snapshot(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        return self._execution_recovery.reconcile_execution_snapshot(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def _handle_prompt(self, sender_id: str, chat_id: str, text: str, *, message_id: str = "") -> None:
        self._prompt_turn_entry.handle_prompt(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
        )

    def _handle_cd_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(card=build_markdown_card(
                "Codex 目录未切换",
                "执行中不能切换目录，请等待结束或先停止当前执行。",
                template="orange",
            ))

        if not arg:
            return CommandResult(card=build_markdown_card(
                "Codex 当前目录",
                f"当前目录：`{display_path(runtime.working_dir)}`",
            ))

        target = resolve_working_dir(arg, fallback=runtime.working_dir)
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
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            self._apply_persisted_runtime_state_message_locked(
                binding,
                state,
                ThreadStateChanged(working_dir=target),
            )
        return CommandResult(card=build_markdown_card(
            "Codex 目录已切换",
            (
                f"目录：`{display_path(target)}`\n"
                "当前线程绑定已清空。\n"
                "直接发送普通文本，会在新目录自动新建线程。"
            ),
        ))

    def _handle_new_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能新建线程，请等待结束或先执行 `/cancel`。")
        try:
            snapshot = self._adapter.create_thread(
                cwd=runtime.working_dir,
                profile=self._effective_default_profile() or None,
                approval_policy=runtime.approval_policy or None,
                sandbox=runtime.sandbox or None,
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

    @staticmethod
    def _binding_has_inflight_turn_locked(state: _RuntimeState) -> bool:
        return RuntimeAdminController.binding_has_inflight_turn_locked(state)

    def _binding_inventory_locked(self) -> list[dict[str, Any]]:
        return self._runtime_admin.binding_inventory_locked()

    def _bound_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        return self._runtime_admin.bound_bindings_for_thread_locked(thread_id)

    def _attached_bindings_for_thread_locked(self, thread_id: str) -> list[ChatBindingKey]:
        return self._runtime_admin.attached_bindings_for_thread_locked(thread_id)

    def _read_thread_summary_for_status(self, thread_id: str) -> tuple[ThreadSummary | None, str]:
        return self._runtime_admin.read_thread_summary_for_status(thread_id)

    def _interaction_owner_snapshot_locked(
        self,
        thread_id: str,
        *,
        current_binding: ChatBindingKey | None = None,
    ) -> dict[str, str]:
        return self._runtime_admin.interaction_owner_snapshot_locked(
            thread_id,
            current_binding=current_binding,
        )

    def _release_feishu_runtime_availability_locked(self, thread_id: str) -> tuple[bool, str]:
        return self._runtime_admin.release_feishu_runtime_availability_locked(thread_id)

    def _binding_has_pending_request_locked(self, binding: ChatBindingKey) -> bool:
        return self._runtime_admin.binding_has_pending_request_locked(binding)

    def _binding_clear_availability_locked(self, binding: ChatBindingKey) -> tuple[bool, str]:
        return self._runtime_admin.binding_clear_availability_locked(binding)

    def _clear_binding_for_control(self, binding: ChatBindingKey) -> dict[str, Any]:
        return self._runtime_admin.clear_binding_for_control(binding)

    def _clear_all_bindings_for_control(self) -> dict[str, Any]:
        return self._runtime_admin.clear_all_bindings_for_control()

    def _binding_status_snapshot(self, binding: ChatBindingKey) -> dict[str, Any]:
        return self._runtime_admin.binding_status_snapshot(binding)

    def _render_binding_status_markdown(
        self,
        snapshot: dict[str, Any],
        *,
        include_profile_lines: bool,
    ) -> tuple[str, str]:
        return self._runtime_admin.render_binding_status_markdown(
            snapshot,
            include_profile_lines=include_profile_lines,
        )

    def _handle_status_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_status_command(binding)

    def _handle_release_feishu_runtime_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_release_feishu_runtime_command(binding, arg)

    def _release_feishu_runtime_by_thread_id(self, thread_id: str) -> dict[str, Any]:
        return self._runtime_admin.release_feishu_runtime_by_thread_id(thread_id)

    def _thread_status_snapshot(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        return self._runtime_admin.thread_status_snapshot(thread_id, summary=summary)

    def _handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        return self._runtime_call(self._handle_service_control_request_impl, method, params)

    def _handle_service_control_request_impl(self, method: str, params: dict[str, Any]) -> Any:
        return self._runtime_admin.handle_service_control_request(method, params)

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
        return self._prompt_turn_entry.cancel_current_turn(
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def _interrupt_running_turn(self, *, thread_id: str, turn_id: str) -> None:
        self._adapter.interrupt_turn(thread_id=thread_id, turn_id=turn_id)

    def _refresh_sessions_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        self._session_ui_domain.refresh_sessions_card_message(sender_id, chat_id, message_id)

    @staticmethod
    def _pending_request_status(pending: _PendingRequestState | dict[str, Any]) -> str:
        return InteractionRequestController.pending_request_status(pending)

    def _handle_approval_card_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_approval_card_action(action_value)

    def _handle_user_input_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_user_input_action(action_value)

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
        self._runtime_submit(
            self._resume_thread_in_background_impl,
            sender_id,
            chat_id,
            thread_id,
            original_arg=original_arg,
            summary=summary,
            message_id=message_id,
            refresh_session_message_id=refresh_session_message_id,
        )

    def _resume_thread_in_background_impl(
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
        sharing_violation = self._thread_sharing_policy_violation(
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if sharing_violation:
            self._reply_text(chat_id, sharing_violation, message_id=message_id)
            if refresh_session_message_id:
                self._refresh_sessions_card_message(sender_id, chat_id, refresh_session_message_id)
            return
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
            return self._read_thread_summary_authoritatively(target, original_arg=target)
        thread = resolve_resume_target_by_name(
            self._adapter,
            name=target,
            limit=self._thread_list_query_limit,
        )
        return self._read_thread_summary_authoritatively(thread.thread_id, original_arg=target)

    def _resolve_thread_name_target_for_control(self, thread_name: str) -> ThreadSummary:
        target = str(thread_name or "").strip()
        if not target:
            raise ValueError("thread_name 不能为空。")
        thread = resolve_resume_target_by_name(
            self._adapter,
            name=target,
            limit=self._thread_list_query_limit,
        )
        return self._read_thread_summary_authoritatively(thread.thread_id, original_arg=target)

    def _resolve_thread_target_for_control_params(self, params: dict[str, Any]) -> ThreadSummary:
        thread_id = str(params.get("thread_id", "") or "").strip()
        thread_name = str(params.get("thread_name", "") or "").strip()
        if bool(thread_id) == bool(thread_name):
            raise ValueError("必须且只能提供 `thread_id` 或 `thread_name`。")
        if thread_id:
            return self._read_thread_summary_authoritatively(thread_id, original_arg=thread_id)
        return self._resolve_thread_name_target_for_control(thread_name)

    def _resume_snapshot(self, arg: str) -> ThreadSnapshot:
        thread = self._resolve_resume_target(arg)
        return self._resume_snapshot_by_id(
            thread.thread_id,
            original_arg=arg.strip(),
            summary=thread,
        )

    def _read_thread_snapshot_authoritatively(
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

    def _read_thread_summary_authoritatively(self, thread_id: str, *, original_arg: str) -> ThreadSummary:
        return self._read_thread_snapshot_authoritatively(
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
        thread = summary or self._lookup_thread_summary_in_bounded_list(thread_id)
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

    def _lookup_thread_summary_in_bounded_list(self, thread_id: str) -> ThreadSummary | None:
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
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        chat_binding_key = resolved.binding
        unsubscribe_thread_id: str = ""
        with self._lock:
            unsubscribe_thread_id = self._binding_runtime.bind_thread_locked(
                chat_binding_key,
                state,
                thread_id=thread.thread_id,
                thread_title=thread.title,
                working_dir=thread.cwd or state["working_dir"],
                on_thread_replaced=lambda state: (
                    self._cancel_patch_timer_locked(state),
                    self._cancel_mirror_watchdog_locked(state),
                    self._reset_execution_context_locked(state, clear_card_message=True),
                ),
                on_after_bind=self._clear_plan_state,
            )
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        chat_binding_key = resolved.binding
        unsubscribe_thread_id: str = ""
        with self._lock:
            unsubscribe_thread_id = self._binding_runtime.clear_thread_binding_locked(
                chat_binding_key,
                state,
                on_clear_state=lambda state: (
                    self._cancel_patch_timer_locked(state),
                    self._cancel_mirror_watchdog_locked(state),
                    self._reset_execution_context_locked(state, clear_card_message=True),
                    self._clear_plan_state(state),
                ),
            )
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)

    def _list_global_threads(self) -> list[ThreadSummary]:
        return list_global_threads(
            self._adapter,
            limit=self._thread_list_query_limit,
        )

    def _safe_read_runtime_config(self) -> RuntimeConfigSummary | None:
        try:
            runtime_config = self._adapter.read_runtime_config()
        except Exception:
            logger.exception("读取 Codex 运行时配置失败")
            return self._last_runtime_config
        self._last_runtime_config = runtime_config
        return runtime_config

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
        self._runtime_submit(self._handle_adapter_notification_impl, method, params)

    def _handle_adapter_notification_impl(self, method: str, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_notification(method, params)

    def _handle_adapter_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        self._runtime_submit(self._handle_adapter_request_impl, request_id, method, params)

    def _handle_adapter_request_impl(
        self, request_id: int | str, method: str, params: dict[str, Any]
    ) -> None:
        self._interaction_requests.handle_adapter_request(request_id, method, params)

    def _handle_server_request_resolved(self, params: dict[str, Any]) -> None:
        self._interaction_requests.handle_server_request_resolved(params)

    def _auto_reject_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        self._interaction_requests.auto_reject_request(request_id, method, params)

    def _handle_thread_status_changed(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_thread_status_changed(params)

    def _handle_thread_closed(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_thread_closed(params)

    def _handle_thread_name_updated(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_thread_name_updated(params)

    def _handle_turn_started(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_turn_started(params)

    def _handle_turn_plan_updated(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_turn_plan_updated(params)

    def _handle_item_started(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_item_started(params)

    def _handle_agent_message_delta(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_agent_message_delta(params)

    def _handle_command_delta(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_command_delta(params)

    def _handle_file_change_delta(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_file_change_delta(params)

    def _handle_item_completed(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_item_completed(params)

    def _handle_turn_completed(self, params: dict[str, Any]) -> None:
        self._adapter_notifications.handle_turn_completed(params)

    def _send_execution_card(
        self,
        chat_id: str,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        return self._execution_output.send_execution_card(
            chat_id,
            parent_message_id,
            reply_in_thread=reply_in_thread,
        )

    def _patch_execution_card_message(
        self,
        message_id: str,
        *,
        transcript: ExecutionTranscript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> bool:
        return self._execution_output.patch_execution_card_message(
            message_id,
            transcript=transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
        )

    def _schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        self._execution_output.schedule_execution_card_update(sender_id, chat_id)

    def _submit_flush_execution_card(self, sender_id: str, chat_id: str, immediate: bool = False) -> None:
        self._execution_output.submit_flush_execution_card(sender_id, chat_id, immediate=immediate)

    def _flush_execution_card(self, sender_id: str, chat_id: str, immediate: bool = False) -> None:
        self._execution_output.flush_execution_card(sender_id, chat_id, immediate=immediate)

    def _send_followup_if_needed(self, sender_id: str, chat_id: str) -> None:
        self._execution_output.send_followup_if_needed(sender_id, chat_id)

    def _clear_plan_state(self, state: _RuntimeState) -> None:
        self._turn_execution.clear_plan_state_locked(state)

    def _flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        self._execution_output.flush_plan_card(sender_id, chat_id)
