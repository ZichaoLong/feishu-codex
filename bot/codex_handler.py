"""
Codex 飞书处理器。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import pathlib
import threading
import time
from dataclasses import replace
from typing import Any, Callable, TypeAlias
from uuid import UUID

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.approval_policy import USER_SELECTABLE_APPROVAL_POLICIES
from bot.adapters.codex_app_server import CodexAppServerAdapter, CodexAppServerConfig
from bot.adapters.base import RuntimeConfigSummary, ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.adapter_notification_controller import AdapterNotificationController
from bot.cards import (
    CommandResult,
    build_goal_card,
    build_history_preview_card,
    build_markdown_card,
    make_card_response,
)
from bot.binding_identity import format_binding_id
from bot.binding_runtime_manager import BindingRuntimeManager, ResolvedRuntimeBinding
from bot.config import load_config_file, save_config_file
from bot.constants import (
    DEFAULT_HISTORY_PREVIEW_ROUNDS,
    DEFAULT_THREADS_INITIAL_LIMIT,
    DEFAULT_STREAM_PATCH_INTERVAL_MS,
    DEFAULT_THREAD_LIST_QUERY_LIMIT,
    GROUP_SHARED_BINDING_OWNER_ID,
    KEYWORD,
    display_path,
    resolve_working_dir,
)
from bot.handler import BotHandler
from bot.instance_layout import current_instance_name, global_data_dir
from bot.stores.instance_registry_store import InstanceRegistryStore, build_instance_registry_entry
from bot.codex_protocol.client import CodexRpcError
from bot.codex_goal_domain import CodexGoalDomain, GoalDomainPorts
from bot.codex_group_domain import CodexGroupDomain, GroupDomainPorts
from bot.codex_help_domain import CodexHelpDomain
from bot.codex_threads_ui_domain import CodexThreadsUiDomain, ThreadsUiPorts, ThreadsUiRuntimePorts
from bot.codex_settings_domain import (
    CodexSettingsDomain,
    SettingsDomainPorts,
)
from bot.reason_codes import ReasonedCheck
from bot.execution_transcript import ExecutionTranscript
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_recovery_controller import (
    ExecutionRecoveryController,
    SnapshotReplyProjection,
    TerminalReconcileTarget,
)
from bot.generated_image_delivery import GeneratedImageDeliveryController
from bot.file_message_domain import FileMessageDomain, FileMessagePorts, IncomingAttachmentMessage
from bot.card_text_projection import project_interactive_card_text
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.interaction_request_controller import InteractionRequestController
from bot.interaction_request_controller import PendingRequestStateDict
from bot.permissions_profile import (
    PERMISSION_PROFILE_CHOICES,
    permissions_profile_choice_key,
    permissions_profile_label,
)
from bot.inbound_surface_controller import ActionRoute, CommandRoute, InboundSurfaceController
from bot.owner_binding_queue import OwnerBindingQueue, OwnerBindingQueueItem
from bot.prompt_turn_entry_controller import PromptTurnEntryController, PromptTurnEntryPorts
from bot.runtime_admin_controller import RuntimeAdminController
from bot.runtime_card_publisher import (
    ExecutionCardPatchDispatcher,
    RuntimeCardPublisher,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_IDLE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
    UNSET,
    BindingActivated,
    ExecutionStateChanged,
    RuntimeSettingsChanged,
    RuntimeStateDict,
    RuntimeStateMessage,
    ThreadGoalCleared,
    ThreadGoalStateChanged,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.runtime_view import RuntimeView, build_runtime_view
from bot.service_control_plane import ServiceControlPlane
from bot.thread_resolution import (
    list_current_dir_threads,
    list_global_threads,
    looks_like_thread_id,
    resolve_resume_target_by_name,
)
from bot.stores.pending_attachment_store import PendingAttachmentStore
from bot.stores.app_server_runtime_store import AppServerRuntimeStore, resolve_effective_app_server_url
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.generated_image_delivery_store import GeneratedImageDeliveryStore
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseStore,
)
from bot.stores.terminal_result_store import TerminalResultRecord, TerminalResultStore
from bot.stores.service_instance_lease import (
    ServiceInstanceLease,
    ServiceInstanceLeaseError,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseHolder, ThreadRuntimeLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.thread_runtime_coordination import (
    acquire_thread_runtime_holder_or_raise,
    preview_thread_global_loaded_gate,
    preview_thread_runtime_holder_acquire,
)
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_image_delivery import ThreadImageDeliveryController
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from bot.runtime_loop import RuntimeLoop, RuntimeLoopClosedError
from bot.platform_paths import default_data_root, default_working_dir

logger = logging.getLogger(__name__)

_CARD_REPLY_LIMIT_DEFAULT = 12000
_TERMINAL_RESULT_CARD_LIMIT_DEFAULT = 26000
_CARD_LOG_LIMIT_DEFAULT = 8000
_MIRROR_WATCHDOG_SECONDS_DEFAULT = 8.0
_COMPACT_START_TIMEOUT_SECONDS_DEFAULT = 60.0
_ATTACHMENT_TTL_SECONDS_DEFAULT = 1800.0
_APPROVAL_POLICIES = set(USER_SELECTABLE_APPROVAL_POLICIES)
_LOCAL_THREAD_SAFETY_RULE = (
    "同一线程允许多端订阅观察，但同一 live turn 只有一个交互 owner；非 owner 只能看，不能写或处理审批。"
)
_INIT_COMMAND = feishu_visible_command_syntax("/init <token>")
_DEBUG_CONTACT_COMMAND = feishu_visible_command_syntax("/debug-contact <open_id>")
ChatBindingKey: TypeAlias = tuple[str, str]


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _replace_text_input_items(input_items: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    normalized_text = str(text or "")
    replaced: list[dict[str, Any]] = []
    inserted_text = False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            if not inserted_text:
                replacement = dict(item)
                replacement["text"] = normalized_text
                replaced.append(replacement)
                inserted_text = True
            continue
        replaced.append(dict(item))
    if not inserted_text:
        replaced.insert(0, {"type": "text", "text": normalized_text})
    return replaced


def _permissions_summary(permissions_profile_id: str) -> str:
    choice = permissions_profile_choice_key(permissions_profile_id)
    if choice:
        return PERMISSION_PROFILE_CHOICES[choice]["label"]
    return permissions_profile_label(permissions_profile_id)


class CodexHandler(BotHandler):
    """处理 Feishu -> Codex 的命令与事件。"""

    def __init__(self, data_dir: pathlib.Path | None = None, config_dir: pathlib.Path | None = None):
        super().__init__()
        cfg = load_config_file("codex")

        self._data_dir = data_dir or default_data_root()
        self._config_dir = config_dir
        self._instance_name = current_instance_name(config_dir=self._config_dir, data_dir=self._data_dir)
        self._global_data_dir = global_data_dir()
        self._lock = threading.RLock()
        self._thread_subscription_registry = ThreadSubscriptionRegistry()
        self._interaction_lease_store = InteractionLeaseStore(self._data_dir)
        self._runtime_loop = RuntimeLoop(name="codex-handler-runtime")
        self._service_instance_lease = ServiceInstanceLease(self._data_dir)
        self._instance_registry = InstanceRegistryStore(self._global_data_dir)
        self._thread_runtime_lease_store = ThreadRuntimeLeaseStore(self._global_data_dir)
        self._service_control_plane = ServiceControlPlane(
            data_dir=self._data_dir,
            dispatch=self._handle_service_control_request,
            owns_current_lease=self._service_instance_lease.owns_current_lease,
            auth_token=lambda: self._service_instance_lease.owner_token,
        )
        self._last_runtime_config: RuntimeConfigSummary | None = None

        self._default_working_dir = resolve_working_dir(
            str(cfg.get("default_working_dir", "")),
            fallback=str(default_working_dir()),
        )
        self._threads_initial_limit = int(cfg.get("threads_initial_limit", DEFAULT_THREADS_INITIAL_LIMIT))
        self._thread_list_query_limit = int(cfg.get("thread_list_query_limit", DEFAULT_THREAD_LIST_QUERY_LIMIT))
        self._history_preview_rounds = int(cfg.get("history_preview_rounds", DEFAULT_HISTORY_PREVIEW_ROUNDS))
        self._stream_patch_interval_ms = int(
            cfg.get("stream_patch_interval_ms", DEFAULT_STREAM_PATCH_INTERVAL_MS)
        )
        self._show_history_preview_on_resume = bool(cfg.get("show_history_preview_on_resume", True))
        self._card_reply_limit = int(cfg.get("card_reply_limit", _CARD_REPLY_LIMIT_DEFAULT))
        self._terminal_result_card_limit = int(
            cfg.get("terminal_result_card_limit", _TERMINAL_RESULT_CARD_LIMIT_DEFAULT)
        )
        self._card_log_limit = int(cfg.get("card_log_limit", _CARD_LOG_LIMIT_DEFAULT))
        self._mirror_watchdog_seconds = float(
            cfg.get("mirror_watchdog_seconds", _MIRROR_WATCHDOG_SECONDS_DEFAULT)
        )
        self._compact_start_timeout_seconds = float(
            cfg.get("compact_start_timeout_seconds", _COMPACT_START_TIMEOUT_SECONDS_DEFAULT)
        )
        self._attachment_ttl_seconds = float(
            cfg.get("attachment_ttl_seconds", _ATTACHMENT_TTL_SECONDS_DEFAULT)
        )

        self._adapter_config = CodexAppServerConfig.from_dict(cfg)
        self._app_server_runtime = AppServerRuntimeStore(self._data_dir)
        self._chat_binding_store = ChatBindingStore(self._data_dir)
        self._pending_attachment_store = PendingAttachmentStore(self._data_dir)
        self._terminal_result_store = TerminalResultStore(self._data_dir)
        self._owner_binding_queue = OwnerBindingQueue()
        self._binding_runtime = BindingRuntimeManager(
            lock=self._lock,
            default_working_dir=self._default_working_dir,
            default_approval_policy=self._adapter_config.approval_policy,
            default_permissions_profile_id=self._adapter_config.permissions_profile_id,
            default_model=self._adapter_config.model,
            default_reasoning_effort=self._adapter_config.reasoning_effort,
            chat_binding_store=self._chat_binding_store,
            thread_subscription_registry=self._thread_subscription_registry,
            interaction_lease_store=self._interaction_lease_store,
            is_group_chat=self._is_group_chat,
        )
        self._execution_card_patch_dispatcher = ExecutionCardPatchDispatcher(
            lambda message_id, model: self._runtime_card_publisher().patch_execution_card(message_id, model),
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
            dispatch_execution_card_patch=self._execution_card_patch_dispatcher.submit,
            reply_text=self._reply_text,
            reply_text_get_id=self._reply_text_get_id,
            record_terminal_result_card=self._record_terminal_result_card_with_execution,
            card_reply_limit=lambda: self._card_reply_limit,
            terminal_result_card_limit=lambda: self._terminal_result_card_limit,
            card_log_limit=lambda: self._card_log_limit,
            stream_patch_interval_ms=lambda: self._stream_patch_interval_ms,
        )
        self._generated_image_delivery_store = GeneratedImageDeliveryStore(self._data_dir)
        self._generated_image_delivery = GeneratedImageDeliveryController(
            store=self._generated_image_delivery_store,
            reply_local_image=lambda chat_id, local_path, parent_message_id, reply_in_thread: self.bot.reply_local_image(
                chat_id,
                local_path,
                parent_message_id=parent_message_id,
                reply_in_thread=reply_in_thread,
            ),
        )
        self._thread_image_delivery = ThreadImageDeliveryController(
            upload_image=lambda local_path: self.bot.upload_image(local_path),
            send_image_by_key=lambda chat_id, image_key: self.bot.send_image_by_key(chat_id, image_key),
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
            dispatch_execution_card_message=self._dispatch_execution_card_message,
            remove_execution_card_message=self._remove_execution_card_message,
            publish_terminal_result=self._publish_terminal_result,
            has_recorded_terminal_result=self._has_recorded_terminal_result,
            deliver_generated_images_from_snapshot=self._deliver_generated_images_from_snapshot,
            read_thread=lambda thread_id: self._adapter.read_thread(thread_id, include_turns=True),
            is_thread_not_found_error=self._is_thread_not_found_error,
            is_turn_thread_not_found_error=self._is_turn_thread_not_found_error,
            is_transport_disconnect=self._is_transport_disconnect,
            is_request_timeout_error=self._is_request_timeout_error,
            runtime_recovery_reason=self._runtime_recovery_reason,
            mirror_watchdog_seconds=lambda: self._mirror_watchdog_seconds,
            compact_start_timeout_seconds=lambda: self._compact_start_timeout_seconds,
            terminal_empty_retry_count=lambda: 6,
            terminal_empty_retry_delay_seconds=lambda: 0.5,
        )
        self._interaction_requests = InteractionRequestController(
            lock=self._lock,
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
        self._adapter_notifications = AdapterNotificationController(
            lock=self._lock,
            turn_execution=self._turn_execution,
            thread_subscribers=self._thread_subscribers,
            get_runtime_state=lambda sender_id, chat_id: self._get_runtime_state(sender_id, chat_id),
            on_runtime_event_accepted=lambda sender_id, chat_id: None,
            apply_runtime_state_message_locked=self._apply_runtime_state_message_locked,
            apply_persisted_runtime_state_message_locked=self._apply_persisted_runtime_state_message_locked,
            cancel_mirror_watchdog_locked=self._cancel_mirror_watchdog_locked,
            finalize_execution_from_terminal_signal=self._finalize_execution_from_terminal_signal,
            dispatch_execution_card_message=self._dispatch_execution_card_message,
            send_execution_card=self._send_execution_card,
            schedule_mirror_watchdog=self._schedule_mirror_watchdog,
            schedule_execution_card_update=self._schedule_execution_card_update,
            flush_execution_card=self._flush_execution_card,
            flush_plan_card=self._flush_plan_card,
            interrupt_running_turn=self._interrupt_running_turn,
            on_server_request_resolved=self._interaction_requests.handle_server_request_resolved,
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
        self._adapter_config = replace(
            self._adapter_config,
            app_server_data_dir=str(self._data_dir),
        )
        self._adapter = CodexAppServerAdapter(
            self._adapter_config,
            on_notification=self._handle_adapter_notification,
            on_request=self._handle_adapter_request,
            on_disconnect=self._handle_adapter_disconnect,
            app_server_runtime_store=self._app_server_runtime,
        )
        self._settings_domain = CodexSettingsDomain(
            ports=SettingsDomainPorts(
                get_message_context=lambda message_id: self.bot.get_message_context(message_id),
                get_sender_display_name=lambda **kwargs: self.bot.get_sender_display_name(**kwargs),
                debug_sender_name_resolution=lambda open_id: self.bot.debug_sender_name_resolution(open_id),
                get_bot_identity_snapshot=lambda: self.bot.get_bot_identity_snapshot(),
                add_admin_open_id=lambda open_id: self.bot.add_admin_open_id(open_id),
                set_configured_bot_open_id=lambda open_id: self.bot.set_configured_bot_open_id(open_id),
                get_runtime_view=self._get_runtime_view,
                update_runtime_settings=self._update_runtime_settings,
            ),
            approval_policies=_APPROVAL_POLICIES,
        )
        self._group_domain = CodexGroupDomain(
            ports=GroupDomainPorts(
                get_sender_display_name=lambda **kwargs: self.bot.get_sender_display_name(**kwargs),
                get_message_context=lambda message_id: self.bot.get_message_context(message_id),
                reply_text=self._reply_text,
                get_group_mode=lambda chat_id: self.bot.get_group_mode(chat_id),
                is_group_admin=lambda open_id: self.bot.is_group_admin(open_id=open_id),
                get_group_activation_snapshot=lambda chat_id: self.bot.get_group_activation_snapshot(chat_id),
                set_group_mode=lambda chat_id, mode: self.bot.set_group_mode(chat_id, mode),
                activate_group_chat=lambda chat_id, activated_by: self.bot.activate_group_chat(
                    chat_id,
                    activated_by=activated_by,
                ),
                deactivate_group_chat=lambda chat_id: self.bot.deactivate_group_chat(chat_id),
                is_group_chat=lambda chat_id, message_id="": self._is_group_chat(chat_id, message_id),
                validate_group_mode_change=lambda chat_id, mode, message_id="": self._validate_group_mode_change(
                    chat_id,
                    mode,
                    message_id=message_id,
                ),
            )
        )
        self._goal_domain = CodexGoalDomain(
            ports=GoalDomainPorts(
                get_runtime_view=lambda sender_id, chat_id, message_id="": self._get_runtime_view(
                    sender_id,
                    chat_id,
                    message_id,
                ),
                get_thread_goal=lambda thread_id: self._adapter.get_thread_goal(thread_id),
                set_thread_goal=lambda thread_id, **kwargs: self._adapter.set_thread_goal(thread_id, **kwargs),
                clear_thread_goal=lambda thread_id: self._adapter.clear_thread_goal(thread_id),
                attach_current_binding=lambda sender_id, chat_id, message_id="": self._runtime_admin.attach_binding(
                    self._chat_binding_key(sender_id, chat_id, message_id)
                ),
                update_runtime_goal_projection=self._update_runtime_goal_projection,
                submit_to_runtime=self._runtime_submit,
                resume_goal_on_runtime=self._resume_goal_on_runtime,
            )
        )
        self._help_domain = CodexHelpDomain(
            local_thread_safety_rule=_LOCAL_THREAD_SAFETY_RULE,
            get_runtime_state=lambda sender_id, chat_id, message_id="": self._get_runtime_state(
                sender_id,
                chat_id,
                message_id,
            ),
            is_group_chat=lambda chat_id, message_id="": self._is_group_chat(chat_id, message_id),
            get_group_mode=lambda chat_id: self.bot.get_group_mode(chat_id),
            get_group_activation_snapshot=lambda chat_id: self.bot.get_group_activation_snapshot(chat_id),
        )
        self._threads_ui_domain = CodexThreadsUiDomain(
            ports=ThreadsUiPorts(
                get_runtime_view=self._get_runtime_view,
                is_group_chat=self._is_group_chat,
                is_group_admin_actor=self._is_group_admin_actor,
                rename_bound_thread_title=self._rename_bound_thread_title,
                reply_text=self._reply_text,
                resolve_resume_target=self._resolve_resume_target,
                list_visible_current_dir_threads=self._list_visible_current_dir_threads,
                read_thread_summary_authoritatively=self._read_thread_summary_authoritatively,
                get_thread_goal=lambda thread_id: self._adapter.get_thread_goal(thread_id),
                archive_thread_for_control=self._archive_thread_for_control,
                rename_thread=lambda thread_id, name: self._adapter.rename_thread(thread_id, name),
                patch_message=lambda message_id, content: self.bot.patch_message(message_id, content),
                is_thread_not_loaded_error=self._is_thread_not_loaded_error,
                threads_initial_limit=self._threads_initial_limit,
            ),
            runtime_ports=ThreadsUiRuntimePorts(
                submit_to_runtime=self._runtime_submit,
                resume_thread_on_runtime=self._resume_thread_on_runtime,
            ),
        )
        self._file_message_domain = FileMessageDomain(
            ports=FileMessagePorts(
                get_message_context=lambda message_id: self.bot.get_message_context(message_id),
                download_message_resource=lambda message_id, resource_key, **kwargs: self.bot.download_message_resource(
                    message_id,
                    resource_key,
                    **kwargs,
                ),
                reply_text=self._reply_text,
                get_runtime_view=self._get_runtime_view,
                message_reply_in_thread=self._message_reply_in_thread,
            ),
            store=self._pending_attachment_store,
            ttl_seconds=self._attachment_ttl_seconds,
        )
        self._thread_access_policy = ThreadAccessPolicy(
            lock=self._lock,
            is_group_chat=self._is_group_chat,
            group_mode_for_chat=lambda chat_id: self.bot.get_group_mode(chat_id),
            thread_subscribers_locked=self._binding_runtime.thread_subscribers,
            current_interaction_lease_locked=self._current_interaction_lease_locked,
            feishu_interaction_holder=self._feishu_interaction_holder,
        )
        self._runtime_admin = RuntimeAdminController(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            interaction_requests=self._interaction_requests,
            clear_all_stored_bindings=self._chat_binding_store.clear_all,
            deactivate_binding_locked=self._deactivate_binding_locked,
            read_thread=lambda thread_id: self._adapter.read_thread(thread_id, include_turns=False),
            read_thread_for_stale_cleanup=lambda thread_id: self._adapter.read_thread(
                thread_id,
                include_turns=False,
            ),
            list_loaded_thread_ids=lambda: self._adapter.list_loaded_thread_ids(),
            current_app_server_url=lambda: self._adapter.current_app_server_url(),
            app_server_mode=lambda: self._adapter_config.app_server_mode,
            unsubscribe_thread=lambda thread_id: self._adapter.unsubscribe_thread(thread_id),
            archive_thread=lambda thread_id: self._adapter.archive_thread(thread_id),
            release_service_thread_runtime_lease=self._release_service_thread_runtime_lease,
            service_control_endpoint=lambda: self._service_control_plane.control_endpoint,
            instance_name=lambda: self._instance_name,
            load_thread_runtime_lease=lambda thread_id: self._thread_runtime_lease_store.load(thread_id),
            list_pending_interaction_requests=self._interaction_requests.pending_requests_snapshot,
            reset_current_instance_backend=self._reset_current_instance_backend,
            attach_binding=self._attach_binding_for_control,
            permissions_summary=_permissions_summary,
            thread_image_delivery=self._thread_image_delivery,
            get_thread_goal=lambda thread_id: self._adapter.get_thread_goal(thread_id),
            set_thread_goal=lambda thread_id, **kwargs: self._adapter.set_thread_goal(thread_id, **kwargs),
            clear_thread_goal=lambda thread_id: self._adapter.clear_thread_goal(thread_id),
            submit_to_runtime=self._runtime_submit,
            reply_text=self._reply_text,
            reply_card=self._reply_card,
            submit_prompt_for_control=self._submit_prompt_for_control,
            prompt_write_denial_check=self._thread_access_policy.prompt_write_denial_check,
            detached_runtime_attach_check=self._detached_runtime_attach_check,
            resolve_thread_target_for_control_params=self._resolve_thread_target_for_control_params,
            resolve_binding_chat_display_name=self._resolve_binding_chat_display_name,
            cancel_patch_timer_locked=self._cancel_patch_timer_locked,
            cancel_mirror_watchdog_locked=self._cancel_mirror_watchdog_locked,
            is_thread_not_found_error=self._is_thread_not_found_error,
            is_thread_not_loaded_error=self._is_thread_not_loaded_error,
        )
        self._prompt_turn_entry = PromptTurnEntryController(
            lock=self._lock,
            turn_execution=self._turn_execution,
            ports=PromptTurnEntryPorts(
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
                message_reply_in_thread=self._message_reply_in_thread,
                group_actor_open_id=self._group_actor_open_id,
                access_policy=self._thread_access_policy,
                detached_runtime_attach_check=self._detached_runtime_attach_check,
                acquire_interaction_lease_for_binding=self._acquire_interaction_lease_for_binding,
                release_interaction_lease_for_binding=self._release_interaction_lease_for_binding,
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
            ),
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
            handle_rename_form_fallback=self._threads_ui_domain.handle_rename_form_fallback,
            handle_help_form_fallback=self._handle_help_form_fallback,
            handle_settings_form_fallback=self._handle_settings_form_fallback,
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
        set_terminal_result_text_resolver = getattr(bot, "set_terminal_result_text_resolver", None)
        if callable(set_terminal_result_text_resolver):
            set_terminal_result_text_resolver(self._resolve_terminal_result_text)
        try:
            self._service_instance_lease.acquire()
            self._runtime_loop.start()
            self._adapter.start()
            control_endpoint = self._service_control_plane.start()
            self._service_instance_lease.publish_control_endpoint(control_endpoint)
            self._register_instance_runtime()
            self._restore_service_thread_runtime_leases()
        except ServiceInstanceLeaseError:
            logger.exception("启动 FOCUS service 失败：当前 FOCUS_DATA_DIR 已被其他实例占用")
            raise
        except Exception:
            logger.exception("启动 Codex app-server 失败")
            try:
                self._unregister_instance_runtime()
            except Exception:
                logger.exception("回滚实例注册失败")
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

    def _register_instance_runtime(self) -> None:
        entry = build_instance_registry_entry(
            instance_name=self._instance_name,
            service_token=self._service_instance_lease.owner_token,
            control_endpoint=self._service_control_plane.control_endpoint,
            app_server_url=self._adapter.current_app_server_url(),
            config_dir=self._config_dir or pathlib.Path(""),
            data_dir=self._data_dir,
        )
        self._instance_registry.register(entry)

    def _unregister_instance_runtime(self) -> None:
        self._instance_registry.unregister(
            self._instance_name,
            service_token=self._service_instance_lease.owner_token,
        )

    def _service_thread_runtime_holder(self) -> ThreadRuntimeLeaseHolder:
        return ThreadRuntimeLeaseHolder(
            holder_id=f"service:{self._service_instance_lease.owner_token}",
            holder_type="service",
            instance_name=self._instance_name,
            owner_pid=os.getpid(),
            owner_service_token=self._service_instance_lease.owner_token,
            control_endpoint=self._service_control_plane.control_endpoint,
            backend_url=self._adapter.current_app_server_url(),
            updated_at=time.time(),
        )

    def _cross_instance_loaded_gate_check(self, thread_id: str) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        preview = preview_thread_global_loaded_gate(
            thread_id=normalized_thread_id,
            current_instance_name=self._instance_name,
            registry_store=self._instance_registry,
        )
        if preview.allowed:
            return ReasonedCheck.allow()
        return ReasonedCheck.deny(preview.reason_code, preview.reason_text)

    def _detached_runtime_attach_check(self, thread_id: str) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        loaded_gate = self._cross_instance_loaded_gate_check(normalized_thread_id)
        if not loaded_gate.allowed:
            return loaded_gate
        preview = preview_thread_runtime_holder_acquire(
            thread_id=normalized_thread_id,
            holder=self._service_thread_runtime_holder(),
            lease_store=self._thread_runtime_lease_store,
        )
        if preview.allowed:
            return ReasonedCheck.allow()
        return ReasonedCheck.deny(preview.reason_code, preview.reason_text)

    def _ensure_service_thread_runtime_lease(self, thread_id: str) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False
        loaded_gate = self._cross_instance_loaded_gate_check(normalized_thread_id)
        if not loaded_gate.allowed:
            raise RuntimeError(loaded_gate.reason_text)
        outcome = acquire_thread_runtime_holder_or_raise(
            thread_id=normalized_thread_id,
            holder=self._service_thread_runtime_holder(),
            lease_store=self._thread_runtime_lease_store,
        )
        return outcome.acquired

    def _release_service_thread_runtime_lease(self, thread_id: str) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        self._thread_runtime_lease_store.release(
            normalized_thread_id,
            f"service:{self._service_instance_lease.owner_token}",
        )

    def _restore_service_thread_runtime_leases(self) -> None:
        attached_thread_ids: set[str] = set()
        with self._lock:
            for binding in self._binding_runtime.binding_keys_locked():
                snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
                if snapshot is None:
                    continue
                if snapshot.feishu_runtime_state != FEISHU_RUNTIME_ATTACHED or not snapshot.thread_id:
                    continue
                attached_thread_ids.add(snapshot.thread_id)
        for thread_id in sorted(attached_thread_ids):
            try:
                self._ensure_service_thread_runtime_lease(thread_id)
            except Exception:
                logger.exception("恢复 service thread runtime lease 失败: thread=%s", thread_id[:12])
                try:
                    self._runtime_admin.detach_thread(thread_id)
                except Exception:
                    logger.exception("将冲突线程 fail-closed 为 detached 失败: thread=%s", thread_id[:12])

    def handle_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._runtime_call(self._handle_message_impl, sender_id, chat_id, text, message_id=message_id)

    def _handle_message_impl(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._inbound_surface.handle_message(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
        )

    def handle_message_recalled(self, chat_id: str, message_id: str) -> None:
        self._runtime_submit(self._handle_message_recalled_impl, chat_id, message_id)

    def _handle_message_recalled_impl(self, chat_id: str, message_id: str) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        with self._lock:
            removed = self._owner_binding_queue.remove_by_message_id(normalized_message_id)
        if removed:
            logger.info(
                "已取消撤回消息对应的排队请求: chat=%s message=%s removed=%s",
                chat_id,
                normalized_message_id,
                removed,
            )

    def _activate_binding_if_needed(self, sender_id: str, chat_id: str, message_id: str = "") -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        with self._lock:
            if not state["active"]:
                self._apply_runtime_state_message_locked(state, BindingActivated())

    def handle_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        if self._should_bypass_runtime_for_card_action(action_value):
            return self._handle_card_action_impl(
                sender_id,
                chat_id,
                message_id,
                action_value,
            )
        return self._runtime_call(
            self._handle_card_action_impl,
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    @staticmethod
    def _should_bypass_runtime_for_card_action(action_value: dict[str, Any]) -> bool:
        action = str(action_value.get("action", "") or "").strip()
        if action in {"resume_thread", "attach_runtime", "goal_resume"}:
            return True
        if action == "goal_apply_confirm":
            objective = str(action_value.get("objective", "") or "").strip()
            status = str(action_value.get("status", "") or "").strip()
            return not objective and status == "active"
        return False

    def _handle_card_action_impl(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        return self._inbound_surface.handle_card_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def _seed_help_action_actor_context(self, chat_id: str, message_id: str, action_value: dict) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        operator_open_id = str(action_value.get("_operator_open_id", "") or "").strip()
        operator_user_id = str(action_value.get("_operator_user_id", "") or "").strip()
        if not operator_open_id and not operator_user_id:
            return
        current_context = self.bot.get_message_context(normalized_message_id)
        merged_context = dict(current_context)
        changed = False
        if operator_open_id and not str(merged_context.get("sender_open_id", "") or "").strip():
            merged_context["sender_open_id"] = operator_open_id
            changed = True
        if operator_user_id and not str(merged_context.get("sender_user_id", "") or "").strip():
            merged_context["sender_user_id"] = operator_user_id
            changed = True
        if "sender_type" not in merged_context or not str(merged_context.get("sender_type", "") or "").strip():
            merged_context["sender_type"] = "user"
            changed = True
        if "chat_type" not in merged_context or not str(merged_context.get("chat_type", "") or "").strip():
            merged_context["chat_type"] = self._resolve_chat_type(chat_id, normalized_message_id)
            changed = True
        if not changed:
            return
        remember_message_context = getattr(self.bot, "_remember_message_context", None)
        if callable(remember_message_context):
            remember_message_context(normalized_message_id, merged_context)
            return
        message_contexts = getattr(self.bot, "message_contexts", None)
        if isinstance(message_contexts, dict):
            message_contexts[normalized_message_id] = merged_context

    def _handle_help_execute_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        self._seed_help_action_actor_context(chat_id, message_id, action_value)
        return self._inbound_surface.handle_help_execute_command_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def _handle_help_submit_command_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        self._seed_help_action_actor_context(chat_id, message_id, action_value)
        return self._inbound_surface.handle_help_submit_command_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def _handle_help_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        payload = self._help_domain.resolve_form_submit_payload(action_value)
        if payload is None:
            return None
        merged_action_value = dict(action_value)
        merged_action_value.update(payload)
        return self._handle_help_submit_command_action(
            sender_id,
            chat_id,
            message_id,
            merged_action_value,
        )

    def _handle_settings_form_fallback(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse | None:
        payload = self._settings_domain.resolve_runtime_settings_form_submit_payload(action_value)
        if payload is None:
            return None
        merged_action_value = dict(action_value)
        merged_action_value.update(payload)
        return self._handle_card_action_impl(
            sender_id,
            chat_id,
            message_id,
            merged_action_value,
        )

    def handle_attachment_message(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self._runtime_call(
            self._handle_attachment_message_impl,
            sender_id,
            chat_id,
            message_id,
            attachment_type,
            resource_key,
            file_name,
        )

    def _handle_attachment_message_impl(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self._file_message_domain.handle_message(
            IncomingAttachmentMessage(
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=message_id,
                thread_id=str(self.bot.get_message_context(message_id).get("thread_id", "") or "").strip(),
                attachment_type=attachment_type,
                resource_key=resource_key,
                display_name=file_name,
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

    def is_sender_active(self, sender_id: str, chat_id: str = "", message_id: str = "") -> bool:
        return self._get_runtime_state(sender_id, chat_id, message_id)["active"]

    def _deactivate_binding_locked(self, key: ChatBindingKey) -> str:
        return self._binding_runtime.deactivate_binding_locked(
            key,
            on_deactivate_state=self._deactivate_binding_state_locked,
        )

    def deactivate_sender(self, sender_id: str, chat_id: str = "", message_id: str = "") -> None:
        key = self._chat_binding_key(sender_id, chat_id, message_id)
        unsubscribe_thread_id: str = ""
        with self._lock:
            unsubscribe_thread_id = self._deactivate_binding_locked(key)
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)

    def preflight_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._runtime_call(
            self._preflight_group_prompt_impl,
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def should_route_group_followup_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._runtime_call(
            self._should_route_group_followup_prompt_impl,
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
            binding_keys = list(self._binding_runtime.binding_keys_for_chat_locked(normalized_chat_id))
            for binding in binding_keys:
                unsubscribe_thread_id = self._deactivate_binding_locked(binding)
                if unsubscribe_thread_id:
                    unsubscribe_thread_ids.append(unsubscribe_thread_id)
        for unsubscribe_thread_id in sorted(set(unsubscribe_thread_ids)):
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)
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
            self._binding_runtime.visit_runtime_states_locked(self._cancel_runtime_timers_locked)
        try:
            self._unregister_instance_runtime()
        except Exception:
            logger.exception("注销实例注册失败")
        try:
            self._service_control_plane.stop()
        except Exception:
            logger.exception("停止本地控制面失败")
        try:
            self._adapter.stop()
        except Exception:
            logger.exception("停止 Codex adapter 失败")
        finally:
            self._execution_card_patch_dispatcher.shutdown()
            self._runtime_loop.stop()
            self._service_instance_lease.release()

    def _hydrate_stored_bindings(self) -> None:
        self._binding_runtime.hydrate_stored_bindings()

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

    def _thread_subscribers(self, thread_id: str) -> tuple[ChatBindingKey, ...]:
        with self._lock:
            return self._binding_runtime.thread_subscribers(thread_id)

    def _sync_stored_binding_locked(self, binding: ChatBindingKey, state: RuntimeStateDict) -> None:
        self._binding_runtime.sync_stored_binding_locked(binding, state)

    def _get_runtime_view(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeView:
        return self._binding_runtime.get_runtime_view(sender_id, chat_id, message_id)

    def _runtime_card_publisher(self) -> RuntimeCardPublisher:
        return RuntimeCardPublisher(self.bot)

    @staticmethod
    def _apply_runtime_state_message_locked(state: RuntimeStateDict, message: RuntimeStateMessage) -> None:
        apply_runtime_state_message(state, message)

    def _apply_persisted_runtime_state_message_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
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
        permissions_profile_id: Any = UNSET,
        model: Any = UNSET,
        reasoning_effort: Any = UNSET,
    ) -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        with self._lock:
            self._apply_persisted_runtime_state_message_locked(
                resolved.binding,
                resolved.state,
                RuntimeSettingsChanged(
                    approval_policy=approval_policy,
                    permissions_profile_id=permissions_profile_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
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

    def _cancel_patch_timer_locked(self, state: RuntimeStateDict) -> None:
        self._cancel_timer(state["patch_timer"])
        self._apply_runtime_state_message_locked(state, ExecutionStateChanged(patch_timer=None))

    def _cancel_mirror_watchdog_locked(self, state: RuntimeStateDict) -> None:
        self._execution_recovery.cancel_mirror_watchdog_locked(state)

    def _cancel_runtime_timers_locked(self, state: RuntimeStateDict) -> None:
        self._cancel_patch_timer_locked(state)
        self._cancel_mirror_watchdog_locked(state)

    def _deactivate_binding_state_locked(self, state: RuntimeStateDict) -> None:
        self._cancel_runtime_timers_locked(state)

    def _replace_bound_thread_state_locked(self, state: RuntimeStateDict) -> None:
        self._cancel_runtime_timers_locked(state)
        self._reset_execution_context_locked(state, clear_card_message=True)
        self._clear_thread_goal_state_locked(state)

    def _clear_bound_thread_state_locked(self, state: RuntimeStateDict) -> None:
        self._replace_bound_thread_state_locked(state)
        self._clear_plan_state(state)

    def _apply_thread_goal_projection_locked(
        self,
        state: RuntimeStateDict,
        goal: ThreadGoalSummary | None,
    ) -> None:
        if goal is None:
            self._apply_runtime_state_message_locked(state, ThreadGoalCleared())
            return
        self._apply_runtime_state_message_locked(
            state,
            ThreadGoalStateChanged(
                goal_objective=goal.objective,
                goal_status=goal.status,
                goal_token_budget=goal.token_budget,
                goal_tokens_used=goal.tokens_used,
                goal_time_used_seconds=goal.time_used_seconds,
                goal_created_at=goal.created_at,
                goal_updated_at=goal.updated_at,
            ),
        )

    def _clear_thread_goal_state_locked(self, state: RuntimeStateDict) -> None:
        self._apply_runtime_state_message_locked(state, ThreadGoalCleared())

    def _reset_execution_context_locked(self, state: RuntimeStateDict, *, clear_card_message: bool) -> None:
        self._turn_execution.reset_execution_context_locked(
            state,
            clear_card_message=clear_card_message,
        )

    def _retire_execution_anchor(self, sender_id: str, chat_id: str) -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id)
        state = resolved.state
        with self._lock:
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

    def _deliver_generated_images_from_snapshot(
        self,
        *,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        snapshot: ThreadSnapshot,
        turn_id: str = "",
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
    ) -> int:
        return self._generated_image_delivery.deliver_snapshot_images(
            sender_id=sender_id,
            chat_id=chat_id,
            thread_id=thread_id,
            snapshot=snapshot,
            turn_id=turn_id,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
        )

    def _mark_runtime_degraded(self, sender_id: str, chat_id: str, *, reason: str) -> None:
        self._execution_recovery.mark_runtime_degraded(sender_id, chat_id, reason=reason)

    def _schedule_mirror_watchdog(self, sender_id: str, chat_id: str) -> None:
        self._execution_recovery.schedule_mirror_watchdog(sender_id, chat_id)

    def _run_mirror_watchdog(self, sender_id: str, chat_id: str, generation: int) -> None:
        self._execution_recovery.run_mirror_watchdog(sender_id, chat_id, generation)

    def _existing_chat_binding_key_locked(self, sender_id: str, chat_id: str) -> ChatBindingKey | None:
        return self._binding_runtime.existing_chat_binding_key_locked(sender_id, chat_id)

    def _fresh_chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        return self._binding_runtime.fresh_chat_binding_key(sender_id, chat_id, message_id)

    def _resolve_runtime_binding(self, sender_id: str, chat_id: str, message_id: str = "") -> ResolvedRuntimeBinding:
        return self._binding_runtime.resolve_runtime_binding(sender_id, chat_id, message_id)

    def _get_runtime_state(self, sender_id: str, chat_id: str, message_id: str = "") -> RuntimeStateDict:
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

    def _resolve_binding_chat_display_name(
        self,
        *,
        binding_kind: str,
        sender_id: str,
        chat_id: str,
        refresh_names: bool = False,
    ) -> str:
        if binding_kind == "p2p":
            if not refresh_names:
                return self.bot.lookup_cached_sender_name(sender_id)
            return self.bot.get_sender_display_name(open_id=sender_id, sender_type="user")
        if binding_kind == "group":
            if not refresh_names:
                return self.bot.lookup_chat_display_name(chat_id)
            refresh_chat_display_name = getattr(self.bot, "refresh_chat_display_name", None)
            if callable(refresh_chat_display_name):
                return refresh_chat_display_name(chat_id)
            return self.bot.get_chat_display_name(chat_id)
        return ""

    def _validate_group_mode_change(self, chat_id: str, mode: str, *, message_id: str = "") -> str:
        runtime = self._get_runtime_view(GROUP_SHARED_BINDING_OWNER_ID, chat_id, message_id)
        return self._thread_access_policy.validate_group_mode_change(
            chat_id,
            mode,
            thread_id=runtime.current_thread_id.strip(),
            message_id=message_id,
        )

    def _chat_binding_key(self, sender_id: str, chat_id: str, message_id: str = "") -> ChatBindingKey:
        with self._lock:
            existing = self._existing_chat_binding_key_locked(sender_id, chat_id)
            if existing is not None:
                return existing
        return self._fresh_chat_binding_key(sender_id, chat_id, message_id)

    def _group_actor_open_id(
        self,
        message_id: str = "",
        operator_open_id: str = "",
        sender_open_id: str = "",
    ) -> str:
        normalized_operator_open_id = str(operator_open_id or "").strip()
        if normalized_operator_open_id:
            return normalized_operator_open_id
        if message_id:
            context = self.bot.get_message_context(message_id)
            context_sender_open_id = str(context.get("sender_open_id", "")).strip()
            if context_sender_open_id:
                return context_sender_open_id
        return str(sender_open_id or "").strip()

    def _message_reply_in_thread(self, message_id: str) -> bool:
        if not message_id:
            return False
        context = self.bot.get_message_context(message_id)
        return bool(str(context.get("thread_id", "") or "").strip())

    def _queued_message_origin(self, message_id: str) -> dict[str, str]:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return {}
        context = self.bot.get_message_context(normalized_message_id) or {}
        return {
            "origin_chat_type": str(context.get("chat_type", "") or "").strip(),
            "origin_sender_open_id": str(context.get("sender_open_id", "") or "").strip(),
            "origin_sender_user_id": str(context.get("sender_user_id", "") or "").strip(),
            "origin_sender_type": str(context.get("sender_type", "") or "").strip(),
            "origin_feishu_thread_id": str(context.get("thread_id", "") or "").strip(),
            "assistant_context_mode": str(context.get("assistant_context_mode", "") or "").strip(),
            "assistant_context_created_at": _non_negative_int(context.get("created_at")),
            "assistant_context_seq": _non_negative_int(context.get("assistant_context_seq")),
            "assistant_context_sender_name": str(context.get("sender_name", "") or "").strip(),
        }

    def _restore_queued_message_origin(self, item: OwnerBindingQueueItem) -> None:
        normalized_message_id = str(item.message_id or "").strip()
        if not normalized_message_id:
            return
        restored = {
            "chat_type": str(item.origin_chat_type or "").strip(),
            "sender_open_id": str(item.origin_sender_open_id or "").strip(),
            "sender_user_id": str(item.origin_sender_user_id or "").strip(),
            "sender_type": str(item.origin_sender_type or "").strip(),
            "thread_id": str(item.origin_feishu_thread_id or "").strip(),
            "assistant_context_mode": str(item.assistant_context_mode or "").strip(),
            "created_at": _non_negative_int(item.assistant_context_created_at),
            "assistant_context_seq": _non_negative_int(item.assistant_context_seq),
            "sender_name": str(item.assistant_context_sender_name or "").strip(),
        }
        restored = {key: value for key, value in restored.items() if value}
        if not restored:
            return
        current_context = self.bot.get_message_context(normalized_message_id) or {}
        merged_context = dict(current_context)
        changed = False
        for key, value in restored.items():
            if str(merged_context.get(key, "") or "").strip():
                continue
            merged_context[key] = value
            changed = True
        if not changed:
            return
        remember_message_context = getattr(self.bot, "_remember_message_context", None)
        if callable(remember_message_context):
            remember_message_context(normalized_message_id, merged_context)
            return
        message_contexts = getattr(self.bot, "message_contexts", None)
        if isinstance(message_contexts, dict):
            message_contexts[normalized_message_id] = merged_context

    def _preflight_group_prompt_impl(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._prompt_turn_entry.preflight_group_prompt(
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def _should_route_group_followup_prompt_impl(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        runtime = build_runtime_view(resolved.state)
        if not runtime.running:
            return False
        return self._owner_binding_queue_allowed(
            resolved,
            sender_id,
            chat_id,
            message_id=message_id,
            actor_open_id=self._group_actor_open_id(message_id),
        )

    def _is_group_admin_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
        sender_open_id: str = "",
    ) -> bool:
        if not self._is_group_chat(chat_id, message_id):
            return True
        actor_open_id = self._group_actor_open_id(
            message_id,
            operator_open_id,
            sender_open_id,
        )
        return self.bot.is_group_admin(open_id=actor_open_id)

    def _group_command_admin_denial_text(
        self,
        chat_id: str,
        message_id: str = "",
        sender_open_id: str = "",
    ) -> str:
        if not self._is_group_chat(chat_id, message_id):
            return ""
        if self._is_group_admin_actor(
            chat_id,
            message_id=message_id,
            sender_open_id=sender_open_id,
        ):
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
        pending: PendingRequestStateDict | None = None,
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
                request = self._interaction_requests.pending_request_snapshot_locked(request_key)
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
    ) -> bool:
        if self._is_group_chat(chat_id, message_id) and message_id:
            return bool(
                self.bot.reply(
                    chat_id,
                    text,
                    parent_message_id=message_id,
                    reply_in_thread=reply_in_thread,
                )
            )
        return bool(self.bot.reply(chat_id, text))

    def _reply_text_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        if self._is_group_chat(chat_id, message_id) and message_id:
            return str(
                getattr(self.bot, "reply_get_id", lambda *_args, **_kwargs: "")(
                    chat_id,
                    text,
                    parent_message_id=message_id,
                    reply_in_thread=reply_in_thread,
                )
                or ""
            ).strip()
        return str(getattr(self.bot, "reply_get_id", lambda *_args, **_kwargs: "")(chat_id, text) or "").strip()

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
                    chat_id, arg, sender_id=sender_id, message_id=message_id
                ),
            ),
            "/h": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._help_domain.reply_help(
                    chat_id, arg, sender_id=sender_id, message_id=message_id
                ),
            ),
            "/commands": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: (
                    CommandResult(
                        text="用法：`/commands`\n说明：该命令不接受额外参数；发送 `/help` 查看导航入口。"
                    )
                    if arg.strip()
                    else self._help_domain.reply_commands(chat_id, message_id=message_id)
                ),
            ),
            "/init": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_init_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text=f"请私聊机器人执行 `{_INIT_COMMAND}`。",
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
            "/last": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_last_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/goal": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_goal_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/preflight": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_preflight_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/detach": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_detach_command(
                    sender_id,
                    chat_id,
                    arg,
                    message_id=message_id,
                ),
            ),
            "/attach": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._handle_attach_command(
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
            "/bot-status": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_bot_status_command(
                    chat_id, message_id=message_id
                ),
            ),
            "/debug-contact": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_debug_contact_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
                scope="p2p",
                scope_denied_text=f"请私聊机器人执行 `{_DEBUG_CONTACT_COMMAND}`。",
            ),
            "/reset-backend": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._runtime_admin.handle_reset_backend_command(
                    arg
                ),
            ),
            "/cancel": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: CommandResult(
                    text=self._cancel_current_turn(sender_id, chat_id, message_id=message_id)[1],
                ),
            ),
            "/threads": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: (
                    CommandResult(
                        text="用法：`/threads`\n说明：该命令不接受额外参数；发送 `/help thread` 查看线程相关操作。"
                    )
                    if arg.strip()
                    else self._threads_ui_domain.handle_threads_command(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                    )
                ),
            ),
            "/resume": CommandRoute(
                handler=self._threads_ui_domain.handle_resume_command,
            ),
            "/archive": CommandRoute(
                handler=self._threads_ui_domain.handle_archive_command,
            ),
            "/compact": CommandRoute(
                handler=self._handle_compact_command,
            ),
            "/rename": CommandRoute(
                handler=self._threads_ui_domain.handle_rename_command,
            ),
            "/approval": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_approval_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/permissions": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_permissions_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/model": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_model_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/effort": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._settings_domain.handle_effort_command(
                    sender_id, chat_id, arg, message_id=message_id
                ),
            ),
            "/group-mode": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_group_mode_command(
                    chat_id,
                    arg,
                    sender_id,
                    message_id=message_id,
                ),
                scope="group",
            ),
            "/group": CommandRoute(
                handler=lambda sender_id, chat_id, arg, message_id: self._group_domain.handle_group_command(
                    chat_id,
                    arg,
                    sender_id,
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
                handler=self._threads_ui_domain.handle_resume_thread_action,
                group_guard="group_admin",
            ),
            "resume_thread_confirm": ActionRoute(
                handler=self._threads_ui_domain.handle_resume_thread_confirm_action,
                group_guard="group_admin",
            ),
            "goal_refresh": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_pause": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_resume": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_clear": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "goal_apply_confirm": ActionRoute(
                handler=self._goal_domain.handle_goal_action,
                group_guard="group_admin",
            ),
            "show_more_threads": ActionRoute(
                handler=self._threads_ui_domain.handle_show_more_threads_action,
                group_guard="group_admin",
            ),
            "close_threads_card": ActionRoute(
                handler=self._threads_ui_domain.handle_close_threads_card_action,
                group_guard="group_admin",
            ),
            "reopen_threads_card": ActionRoute(
                handler=self._threads_ui_domain.handle_reopen_threads_card_action,
                group_guard="group_admin",
            ),
            "show_help_page": ActionRoute(
                handler=self._help_domain.handle_show_help_page_action,
            ),
            "help_execute_command": ActionRoute(
                handler=self._handle_help_execute_command_action,
            ),
            "help_submit_command": ActionRoute(
                handler=self._handle_help_submit_command_action,
            ),
            "archive_thread": ActionRoute(
                handler=self._threads_ui_domain.handle_archive_thread_action,
                group_guard="group_admin",
            ),
            "show_rename_form": ActionRoute(
                handler=self._threads_ui_domain.handle_show_rename_action,
                group_guard="group_admin",
            ),
            "rename_thread": ActionRoute(
                handler=self._threads_ui_domain.handle_rename_submit_action,
                group_guard="group_admin",
            ),
            "cancel_rename": ActionRoute(
                handler=self._threads_ui_domain.handle_cancel_rename_action,
                group_guard="group_admin",
            ),
            "set_approval_policy": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_approval_policy(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_permissions_profile": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_permissions_profile(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_model": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_model(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "submit_model_override": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_submit_model_override(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "set_reasoning_effort": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._settings_domain.handle_set_reasoning_effort(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "reset_backend": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_reset_backend_action(
                    sender_id, chat_id, message_id, action_value
                ),
                group_guard="group_admin",
            ),
            "attach_runtime": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_attach_action(
                    sender_id,
                    chat_id,
                    message_id,
                    action_value,
                ),
                group_guard="group_admin",
            ),
            "dismiss_attach": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._runtime_admin.handle_dismiss_attach_action(),
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
            "set_group_activation": ActionRoute(
                handler=lambda sender_id, chat_id, message_id, action_value: self._group_domain.handle_set_group_activation_action(
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
            group_guard="request_actor_or_admin",
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
    def _snapshot_reply(snapshot: ThreadSnapshot, *, turn_id: str = "") -> SnapshotReplyProjection:
        return ExecutionRecoveryController.snapshot_reply(snapshot, turn_id=turn_id)

    def _finalize_execution_card_from_state(self, sender_id: str, chat_id: str) -> bool:
        resolved = self._resolve_runtime_binding(sender_id, chat_id)
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            transition = self._turn_execution.prepare_finalize_locked(state)
            self._cancel_mirror_watchdog_locked(state)
        if not transition.had_card:
            self._retire_execution_anchor(sender_id, chat_id)
            self._drain_owner_binding_queue(resolved.binding)
            return False
        self._flush_execution_card(sender_id, chat_id, immediate=True, background=True)
        self._retire_execution_anchor(sender_id, chat_id)
        self._drain_owner_binding_queue(resolved.binding)
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
        if target is not None:
            self._remember_runtime_terminal_result_text(
                sender_id=sender_id,
                chat_id=chat_id,
                execution_message_id=target.card_message_id,
                final_reply_text=target.transcript.reply_text(),
            )
        finalized = self._finalize_execution_card_from_state(sender_id, chat_id)
        if finalized:
            self._schedule_terminal_execution_reconcile(target)
        return finalized

    def _remember_runtime_terminal_result_text(
        self,
        *,
        sender_id: str,
        chat_id: str,
        execution_message_id: str,
        final_reply_text: str,
    ) -> None:
        normalized_message_id = str(execution_message_id or "").strip()
        raw_text = str(final_reply_text or "")
        if not normalized_message_id or not raw_text:
            return
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            runtime = build_runtime_view(state)
            if runtime.execution.current_message_id.strip() != normalized_message_id:
                return
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(terminal_result_text=raw_text),
            )

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
        prepared = self._file_message_domain.prepare_prompt_input(
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
        )
        if prepared.blocking_text:
            self._reply_text(
                chat_id,
                prepared.blocking_text,
                message_id=message_id,
                reply_in_thread=self._message_reply_in_thread(message_id),
            )
            return
        prompt_admission = self._start_or_enqueue_prompt(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            input_items=list(prepared.input_items),
        )
        if not prompt_admission.get("accepted") and prepared.consumed_attachments:
            self._file_message_domain.restore_consumed_attachments(prepared.consumed_attachments)

    def _start_or_enqueue_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
        surface_failures: bool = True,
    ) -> dict[str, Any]:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        binding_id = format_binding_id(resolved.binding)
        if runtime.running:
            origin = self._queued_message_origin(message_id)
            queued_actor_open_id = str(actor_open_id or "").strip() or str(
                origin.get("origin_sender_open_id", "") or ""
            ).strip()
            if not self._owner_binding_queue_allowed(
                resolved,
                sender_id,
                chat_id,
                message_id=message_id,
                actor_open_id=queued_actor_open_id,
            ):
                if surface_failures:
                    self._reply_text(chat_id, "当前线程仍在执行，请等待结束或先执行 `/cancel`。", message_id=message_id)
                return {
                    "accepted": False,
                    "queued": False,
                    "started": False,
                    "binding_id": binding_id,
                    "thread_id": runtime.current_thread_id,
                    "turn_id": "",
                    "reason_code": "prompt_denied_by_running_turn",
                    "reason": "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
                }
            item = OwnerBindingQueueItem(
                kind="prompt",
                binding=resolved.binding,
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=str(message_id or "").strip(),
                text=str(text or "").strip(),
                actor_open_id=queued_actor_open_id,
                **origin,
                input_items=tuple(dict(item) for item in (input_items or ())),
                synthetic_source=str(synthetic_source or "").strip(),
                display_mode=str(display_mode or "silent").strip().lower() or "silent",
                surface_failures=surface_failures,
            )
            with self._lock:
                depth = self._owner_binding_queue.enqueue(item)
            if surface_failures:
                self._reply_text(chat_id, f"已排队，将在当前执行结束后继续。队列位置：{depth}", message_id=message_id)
            return {
                "accepted": True,
                "queued": True,
                "started": False,
                "binding_id": binding_id,
                "thread_id": runtime.current_thread_id,
                "turn_id": "",
                "reason_code": "",
                "reason": "",
                "queue_position": depth,
            }
        result = self._prompt_turn_entry.start_prompt_turn_result(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            actor_open_id=actor_open_id,
            input_items=input_items,
            surface_failures=surface_failures,
        )
        return {
            "accepted": result.started,
            "queued": False,
            "started": result.started,
            "binding_id": binding_id,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "reason_code": result.reason_code,
            "reason": result.reason_text,
        }

    def _owner_binding_queue_allowed(
        self,
        resolved: ResolvedRuntimeBinding,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
    ) -> bool:
        del chat_id
        del message_id
        del actor_open_id
        if resolved.binding[0] == GROUP_SHARED_BINDING_OWNER_ID:
            return True
        if resolved.binding[0] != GROUP_SHARED_BINDING_OWNER_ID and resolved.binding[0] != sender_id:
            return False
        return True

    def _handle_compact_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        if arg.strip():
            return CommandResult(text="用法：`/compact`")
        result = self._start_or_enqueue_compact(sender_id, chat_id, message_id=message_id)
        if result.get("queued"):
            return CommandResult(text=f"已排队，compact 将在当前执行结束后开始。队列位置：{result['queue_position']}")
        if not result.get("started"):
            return CommandResult(text=str(result.get("reason") or "compact 失败。"))
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        title = runtime.current_thread_title or "（无标题）"
        return CommandResult(
            card=build_markdown_card(
                "Codex Compact 已开始",
                (
                    f"已发起当前 thread 的 compact：`{result['thread_id'][:8]}…` {title}\n"
                    "这是上游 Codex 的 thread 级上下文压缩动作；完成后会继续在同一 thread 内工作。"
                ),
                template="green",
            )
        )

    def _start_or_enqueue_compact(self, sender_id: str, chat_id: str, *, message_id: str = "") -> dict[str, Any]:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        binding_id = format_binding_id(resolved.binding)
        if not runtime.current_thread_id:
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": "",
                "turn_id": "",
                "reason_code": "compact_denied_no_thread",
                "reason": "当前还没有绑定 thread；先执行 `/new`，或直接发送第一条普通消息创建线程。",
            }
        if runtime.running:
            origin = self._queued_message_origin(message_id)
            queued_actor_open_id = str(origin.get("origin_sender_open_id", "") or "").strip()
            if not self._owner_binding_queue_allowed(
                resolved,
                sender_id,
                chat_id,
                message_id=message_id,
                actor_open_id=queued_actor_open_id,
            ):
                return {
                    "accepted": False,
                    "queued": False,
                    "started": False,
                    "binding_id": binding_id,
                    "thread_id": runtime.current_thread_id,
                    "turn_id": "",
                    "reason_code": "compact_denied_by_running_turn",
                    "reason": "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
                }
            item = OwnerBindingQueueItem(
                kind="compact",
                binding=resolved.binding,
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=str(message_id or "").strip(),
                actor_open_id=queued_actor_open_id,
                **origin,
            )
            with self._lock:
                depth = self._owner_binding_queue.enqueue(item)
            return {
                "accepted": True,
                "queued": True,
                "started": False,
                "binding_id": binding_id,
                "thread_id": runtime.current_thread_id,
                "turn_id": "",
                "reason_code": "",
                "reason": "",
                "queue_position": depth,
            }
        return self._start_compact_execution(sender_id, chat_id, message_id=message_id)

    def _start_compact_execution(self, sender_id: str, chat_id: str, *, message_id: str = "") -> dict[str, Any]:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        runtime = build_runtime_view(state)
        binding_id = format_binding_id(resolved.binding)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": "",
                "turn_id": "",
                "reason_code": "compact_denied_no_thread",
                "reason": "当前还没有绑定 thread；先执行 `/new`，或直接发送第一条普通消息创建线程。",
            }
        denial_text = self._thread_access_policy.prompt_write_denial_text(
            resolved.binding,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if denial_text:
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": thread_id,
                "turn_id": "",
                "reason_code": "compact_denied_by_thread_owner",
                "reason": denial_text,
            }
        with self._lock:
            interaction_lease = self._acquire_interaction_lease_for_binding(resolved.binding, thread_id)
        if not interaction_lease.granted:
            denial_text = self._thread_access_policy.interaction_denied_text(interaction_lease.lease)
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": thread_id,
                "turn_id": "",
                "reason_code": "compact_denied_by_interaction_owner",
                "reason": denial_text,
            }
        reply_in_thread = self._message_reply_in_thread(message_id)
        with self._lock:
            started_at = time.monotonic()
            self._turn_execution.prime_prompt_turn_locked(
                state,
                prompt_message_id=str(message_id or "").strip(),
                prompt_reply_in_thread=reply_in_thread,
                actor_open_id=self._group_actor_open_id(message_id),
                started_at=started_at,
                awaiting_attach_status_settle=False,
                execution_kind="compact",
            )
            self._turn_execution.append_process_note_locked(
                state,
                text="正在压缩上下文。",
                marks_work=True,
            )
        card_id = self._send_execution_card(chat_id, message_id, reply_in_thread=reply_in_thread) or ""
        if not card_id:
            error_text = "执行卡片发送失败，未启动 compact；请稍后重试。"
            with self._lock:
                self._turn_execution.record_start_failure_locked(state, error_text=error_text)
            self._retire_execution_anchor(sender_id, chat_id)
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": thread_id,
                "turn_id": "",
                "reason_code": "compact_execution_card_failed",
                "reason": error_text,
            }
        with self._lock:
            self._apply_runtime_state_message_locked(state, ExecutionStateChanged(current_message_id=card_id))
        try:
            self._adapter.compact_thread(thread_id)
        except Exception as exc:
            error_text = self._compact_start_failure_text(thread_id, exc)
            if error_text is None:
                logger.exception("compact 线程失败")
                error_text = f"compact 失败：{exc}"
            with self._lock:
                self._turn_execution.record_start_failure_locked(state, error_text=error_text)
            self._flush_execution_card(sender_id, chat_id, immediate=True)
            self._retire_execution_anchor(sender_id, chat_id)
            return {
                "accepted": False,
                "queued": False,
                "started": False,
                "binding_id": binding_id,
                "thread_id": thread_id,
                "turn_id": "",
                "reason_code": "compact_start_failed",
                "reason": error_text,
            }
        self._schedule_mirror_watchdog(sender_id, chat_id)
        return {
            "accepted": True,
            "queued": False,
            "started": True,
            "binding_id": binding_id,
            "thread_id": thread_id,
            "turn_id": "",
            "reason_code": "",
            "reason": "",
        }

    def _drain_owner_binding_queue(self, binding: tuple[str, str]) -> None:
        with self._lock:
            item = self._owner_binding_queue.begin_drain(binding)
        if item is None:
            return
        consumed = False
        try:
            self._restore_queued_message_origin(item)
            runtime = self._get_runtime_view(item.sender_id, item.chat_id, item.message_id)
            if runtime.running:
                return
            if item.kind == "prompt":
                queued_text = item.text
                queued_input_items: list[dict[str, Any]] | None = [
                    dict(input_item) for input_item in item.input_items
                ]
                prepare_queued_prompt_text = getattr(self.bot, "prepare_queued_prompt_text", None)
                if callable(prepare_queued_prompt_text):
                    prepared_text = prepare_queued_prompt_text(
                        chat_id=item.chat_id,
                        message_id=item.message_id,
                        text=item.text,
                        assistant_context_mode=item.assistant_context_mode,
                        assistant_context_created_at=item.assistant_context_created_at,
                        assistant_context_seq=item.assistant_context_seq,
                        assistant_context_sender_name=item.assistant_context_sender_name,
                        origin_feishu_thread_id=item.origin_feishu_thread_id,
                    )
                    if prepared_text is None:
                        consumed = True
                    else:
                        queued_text = str(prepared_text or "")
                        if queued_text != item.text:
                            queued_input_items = _replace_text_input_items(queued_input_items or [], queued_text)
                if not consumed:
                    result = self._start_or_enqueue_prompt(
                        item.sender_id,
                        item.chat_id,
                        queued_text,
                        message_id=item.message_id,
                        actor_open_id=item.actor_open_id,
                        input_items=queued_input_items,
                        synthetic_source=item.synthetic_source,
                        display_mode=item.display_mode,
                        surface_failures=item.surface_failures,
                    )
                    consumed = not result.get("queued")
                    if result.get("started") and item.display_mode == "announce":
                        label = item.synthetic_source or "系统任务"
                        self._reply_text(item.chat_id, f"{label}触发，开始新一轮执行。", reply_in_thread=False)
            else:
                result = self._start_compact_execution(item.sender_id, item.chat_id, message_id=item.message_id)
                consumed = True
                if not result.get("started"):
                    self._reply_text(item.chat_id, str(result.get("reason") or "compact 失败。"), message_id=item.message_id)
        finally:
            with self._lock:
                self._owner_binding_queue.finish_drain(binding, started=consumed)
        if consumed and not self._get_runtime_view(item.sender_id, item.chat_id, item.message_id).running:
            self._drain_owner_binding_queue(binding)

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

        current_dir = pathlib.Path(str(runtime.working_dir or "")).expanduser().resolve()
        target_dir = pathlib.Path(target).resolve()
        invalidated_attachment_count = 0
        if target_dir != current_dir:
            invalidated_attachment_count = self._file_message_domain.invalidate_pending_attachments_for_scope(
                sender_id=sender_id,
                chat_id=chat_id,
                message_id=message_id,
            )
        self._clear_thread_binding(sender_id, chat_id, message_id=message_id)
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        with self._lock:
            self._apply_persisted_runtime_state_message_locked(
                binding,
                state,
                ThreadStateChanged(working_dir=target),
            )
        message = (
            f"目录：`{display_path(target)}`\n"
            "当前线程绑定已清空。\n"
        )
        if invalidated_attachment_count > 0:
            message += f"已使 {invalidated_attachment_count} 个待消费附件失效。\n"
        message += "直接发送普通文本，会在新目录自动新建线程。"
        return CommandResult(card=build_markdown_card(
            "Codex 目录已切换",
            message,
        ))

    def _handle_new_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        if runtime.running:
            return CommandResult(text="执行中不能新建线程，请等待结束或先执行 `/cancel`。")
        snapshot: ThreadSnapshot | None = None
        try:
            snapshot = self._adapter.create_thread(
                cwd=runtime.working_dir,
                model=runtime.model or None,
                approval_policy=runtime.approval_policy or None,
                permissions_profile_id=runtime.permissions_profile_id or None,
            )
            self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        except Exception as exc:
            logger.exception("新建线程失败")
            created_thread_id = snapshot.summary.thread_id if snapshot is not None else ""
            if created_thread_id:
                try:
                    self._adapter.unsubscribe_thread(created_thread_id)
                except Exception:
                    logger.exception("新建线程失败后回滚 thread 订阅失败: thread=%s", created_thread_id[:12])
                try:
                    self._release_service_thread_runtime_lease(created_thread_id)
                except Exception:
                    logger.exception("新建线程失败后释放 runtime lease 失败: thread=%s", created_thread_id[:12])
            return CommandResult(text=f"新建线程失败：{exc}")
        content = (
            f"线程：`{snapshot.summary.thread_id[:8]}…`\n"
            f"目录：`{display_path(snapshot.summary.cwd)}`\n"
            "直接发送普通文本开始第一轮对话。"
        )
        return CommandResult(card=build_markdown_card(
            "Codex 线程已新建",
            content,
            template="green",
        ))

    def _binding_inventory_locked(self) -> list[dict[str, Any]]:
        return self._runtime_admin.binding_inventory_locked()

    def _clear_all_bindings_for_control(self) -> dict[str, Any]:
        return self._runtime_admin.clear_all_bindings_for_control()

    def _binding_status_snapshot(self, binding: ChatBindingKey) -> dict[str, Any]:
        return self._runtime_admin.binding_status_snapshot(binding)

    def _handle_status_command(self, sender_id: str, chat_id: str, *, message_id: str = "") -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_status_command(binding)

    def _handle_last_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        if str(arg or "").strip().lower() != "text":
            return CommandResult(text="用法：`/last text`")
        text = self._find_last_card_text(sender_id, chat_id, message_id=message_id)
        return CommandResult(text=text)

    def _handle_goal_command(self, sender_id: str, chat_id: str, arg: str, *, message_id: str = "") -> CommandResult:
        return self._goal_domain.handle_goal_command(sender_id, chat_id, arg, message_id=message_id)

    def _find_last_card_text(self, sender_id: str, chat_id: str, *, message_id: str = "") -> str:
        feishu_thread_id = ""
        if message_id and hasattr(self.bot, "get_message_context"):
            context = self.bot.get_message_context(message_id) or {}
            feishu_thread_id = str(context.get("thread_id", "") or "").strip()
        try:
            codex_thread_id = self._get_runtime_view(sender_id, chat_id, message_id).current_thread_id.strip()
        except Exception:
            codex_thread_id = ""

        try:
            items = self.bot.list_recent_messages(
                chat_id=chat_id,
                thread_id=feishu_thread_id,
                limit=20,
            )
        except Exception as exc:
            logger.warning(
                "读取最近卡片失败: chat_id=%s feishu_thread_id=%s codex_thread_id=%s message_id=%s error=%s",
                chat_id,
                feishu_thread_id,
                codex_thread_id,
                message_id,
                exc,
            )
            return "读取最近卡片失败，请稍后重试。"

        app_id = str(getattr(self.bot, "app_id", "") or "").strip()
        fallback_text = ""
        for item in items:
            item_msg_type = str(getattr(item, "msg_type", "") or "").strip()
            sender = getattr(item, "sender", None)
            sender_type = str(getattr(sender, "sender_type", "") or "").strip()
            sender_id = str(getattr(sender, "id", "") or "").strip()
            if app_id and (sender_type != "app" or sender_id != app_id):
                continue

            item_message_id = str(getattr(item, "message_id", "") or "").strip()
            authoritative_text = self._terminal_result_store.get(item_message_id)
            if authoritative_text:
                return authoritative_text
            if item_msg_type != "interactive":
                continue
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "").strip()
            if not raw_content:
                continue
            try:
                content_dict = json.loads(raw_content)
            except Exception:
                continue
            if not isinstance(content_dict, dict):
                continue

            resolved = self.bot.read_interactive_message(
                message_id=item_message_id,
                content_dict=content_dict,
            )
            if resolved.card_kind == "terminal" and resolved.text and resolved.has_authoritative_text:
                return resolved.text
            if not fallback_text and resolved.card_kind == "execution" and resolved.text:
                fallback_text = resolved.text

        if fallback_text:
            return fallback_text
        if codex_thread_id:
            latest_thread_text = self._terminal_result_store.latest_for_thread(codex_thread_id)
            if latest_thread_text:
                return latest_thread_text
        return "最近没有找到可导出的终态卡；也没有可回退的执行卡。"

    def _record_terminal_result_card(self, *, message_id: str, final_reply_text: str) -> None:
        self._record_terminal_result_card_with_execution(
            message_id=message_id,
            execution_message_id="",
            final_reply_text=final_reply_text,
        )

    def _record_terminal_result_card_with_execution(
        self,
        *,
        message_id: str,
        execution_message_id: str,
        final_reply_text: str,
        terminal_result_id: str = "",
        thread_id: str = "",
        checksum: str = "",
    ) -> None:
        normalized_message_id = str(message_id or "").strip()
        normalized_execution_message_id = str(execution_message_id or "").strip()
        raw_text = str(final_reply_text or "")
        if not normalized_message_id or not raw_text:
            return
        self._terminal_result_store.upsert(
            TerminalResultRecord(
                message_id=normalized_message_id,
                execution_message_id=normalized_execution_message_id,
                final_reply_text=raw_text,
                recorded_at=time.time(),
                terminal_result_id=str(terminal_result_id or "").strip().lower(),
                thread_id=str(thread_id or "").strip(),
                checksum=str(checksum or "").strip().lower(),
            )
        )

    def _resolve_terminal_result_text(self, projection) -> str:
        terminal_result_id = str(getattr(projection, "terminal_result_id", "") or "").strip().lower()
        if not terminal_result_id:
            return ""
        return self._terminal_result_store.get_by_terminal_result_id(
            terminal_result_id,
            checksum=str(getattr(projection, "terminal_result_checksum", "") or "").strip().lower(),
        )

    def _has_recorded_terminal_result(self, *, execution_message_id: str, final_reply_text: str) -> bool:
        return self._terminal_result_store.has_execution_result(
            execution_message_id=execution_message_id,
            final_reply_text=final_reply_text,
        )

    def _handle_preflight_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_preflight_command(binding, arg)

    def _handle_detach_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_detach_command(binding, arg)

    def _handle_attach_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        *,
        message_id: str = "",
    ) -> CommandResult:
        binding = self._chat_binding_key(sender_id, chat_id, message_id)
        return self._runtime_admin.handle_attach_command(binding, arg)

    def _detach_thread(self, thread_id: str) -> dict[str, Any]:
        return self._runtime_admin.detach_thread(thread_id)

    def _archive_thread_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        return self._runtime_admin.archive_thread_for_control(thread_id, summary=summary)

    def _handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        return self._runtime_call(self._handle_service_control_request_impl, method, params)

    def _handle_service_control_request_impl(self, method: str, params: dict[str, Any]) -> Any:
        return self._runtime_admin.handle_service_control_request(method, params)

    def _submit_prompt_for_control(
        self,
        binding: ChatBindingKey,
        *,
        text: str,
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
    ) -> dict[str, Any]:
        binding_id = format_binding_id(binding)
        normalized_text = str(text or "").strip()
        normalized_source = str(synthetic_source or "").strip()
        normalized_display_mode = str(display_mode or "silent").strip().lower() or "silent"
        result = self._start_or_enqueue_prompt(
            binding[0],
            binding[1],
            normalized_text,
            actor_open_id=str(actor_open_id or "").strip(),
            input_items=list(input_items) if input_items is not None else None,
            synthetic_source=normalized_source,
            display_mode=normalized_display_mode,
            surface_failures=False,
        )
        if normalized_display_mode == "announce" and result.get("started"):
            label = normalized_source or "系统任务"
            self._reply_text(binding[1], f"{label}触发，开始新一轮执行。", reply_in_thread=False)
        return {
            "binding_id": binding_id,
            "thread_id": str(result.get("thread_id", "") or ""),
            "started": bool(result.get("started")),
            "queued": bool(result.get("queued")),
            "queue_position": int(result.get("queue_position") or 0),
            "turn_id": str(result.get("turn_id", "") or ""),
            "reason_code": str(result.get("reason_code", "") or ""),
            "reason": str(result.get("reason", "") or ""),
            "synthetic_source": normalized_source,
            "display_mode": normalized_display_mode,
        }

    def _attach_binding_for_control(self, binding: ChatBindingKey, thread_id: str) -> ThreadSummary:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        attach_check = self._detached_runtime_attach_check(normalized_thread_id)
        if not attach_check.allowed:
            raise ValueError(attach_check.reason_text)
        # `thread/read` can inspect a loaded thread, but it does not establish
        # the service connection's live thread listener. Control-plane attach
        # must use `thread/resume` so the backend subscription fact matches the
        # local `attached` state.
        fallback_summary = ThreadSummary(
            thread_id=normalized_thread_id,
            cwd="",
            name="",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status=BACKEND_THREAD_STATUS_IDLE,
        )
        summary = self._resume_snapshot_by_id(
            normalized_thread_id,
            original_arg=normalized_thread_id,
            summary=fallback_summary,
        ).summary
        self._bind_thread(binding[0], binding[1], summary)
        return summary

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

    def _refresh_threads_card_message(self, sender_id: str, chat_id: str, message_id: str) -> None:
        self._threads_ui_domain.refresh_threads_card_message(sender_id, chat_id, message_id)

    def _handle_approval_card_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_approval_card_action(action_value)

    def _handle_user_input_action(self, action_value: dict) -> P2CardActionTriggerResponse:
        return self._interaction_requests.handle_user_input_action(action_value)

    def _resume_thread_on_runtime(
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
    ) -> None:
        state = self._get_runtime_state(sender_id, chat_id, message_id)
        all_mode_exclusivity_violation = self._thread_access_policy.all_mode_thread_exclusivity_violation(
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if all_mode_exclusivity_violation:
            self._reply_text(chat_id, all_mode_exclusivity_violation, message_id=message_id)
            if refresh_threads_message_id:
                self._refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
            return
        with self._lock:
            if self._turn_execution.has_active_execution_locked(state):
                self._reply_text(chat_id, "当前线程仍在执行，暂不切换。", message_id=message_id)
                if refresh_threads_message_id:
                    self._refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
                return
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        approval_policy = runtime.approval_policy or None
        permissions_profile_id = runtime.permissions_profile_id or None
        model = runtime.model or None
        reasoning_effort = runtime.reasoning_effort or None
        goal = None
        goal_is_active = False
        loaded_thread_ids = set(self._adapter.list_loaded_thread_ids())
        was_loaded = thread_id in loaded_thread_ids
        if not was_loaded and summary is not None:
            was_loaded = str(summary.status or "").strip() != BACKEND_THREAD_STATUS_NOT_LOADED
        paused_for_cold_sync = False
        try:
            goal = self._get_thread_goal_if_available(thread_id)
            goal_is_active = goal is not None and str(goal.status or "").strip() == "active"
            if not was_loaded and goal_is_active and pause_active_goal_on_resume:
                # Cold-resuming an unloaded thread with an active persisted goal
                # cannot safely guarantee that the first autonomous goal turn
                # will inherit the current binding-wise settings. The safe
                # branch pauses first, restores the thread, syncs settings, and
                # leaves the goal paused for an explicit later resume.
                self._adapter.set_thread_goal(thread_id, status="paused")
                paused_for_cold_sync = True
            carry_cold_binding_settings = not was_loaded and (not goal_is_active or pause_active_goal_on_resume)
            snapshot = self._resume_snapshot_by_id(
                thread_id,
                original_arg=original_arg or thread_id,
                summary=summary,
                model=(model if carry_cold_binding_settings else None),
                reasoning_effort=(reasoning_effort if carry_cold_binding_settings else None),
                approval_policy=(approval_policy if carry_cold_binding_settings else None),
                permissions_profile_id=(permissions_profile_id if carry_cold_binding_settings else None),
            )
        except Exception as exc:
            if paused_for_cold_sync:
                self._restore_paused_goal_after_failed_resume(thread_id)
            logger.exception("恢复线程失败")
            self._reply_text(chat_id, f"恢复线程失败：{exc}", message_id=message_id)
            if refresh_threads_message_id:
                self._refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
            return
        self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
        try:
            self._adapter.update_thread_settings(
                thread_id,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
        except Exception as exc:
            if paused_for_cold_sync:
                self._restore_paused_goal_after_failed_resume(thread_id)
            logger.exception("同步线程设置失败")
            self._reply_text(chat_id, f"恢复线程后同步当前会话设置失败：{exc}", message_id=message_id)
            if refresh_threads_message_id:
                self._refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
            return
        if refresh_threads_message_id:
            self._refresh_threads_card_message(sender_id, chat_id, refresh_threads_message_id)
        summary = (
            f"**已切换到线程**\n"
            f"thread：`{snapshot.summary.thread_id[:8]}…`\n"
            f"标题：{snapshot.summary.title}\n"
            f"目录：`{display_path(snapshot.summary.cwd)}`\n"
            f"{_LOCAL_THREAD_SAFETY_RULE}"
        )
        if paused_for_cold_sync:
            summary += "\n当前按本会话设置恢复了 thread，但 persisted goal 仍保持 `paused`；如需继续，请执行 `/goal resume`。"
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
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> ThreadSnapshot:
        thread = summary or self._lookup_thread_summary_in_bounded_list(thread_id)
        lease_was_newly_acquired = self._ensure_service_thread_runtime_lease(thread_id)
        config_overrides: dict[str, Any] | None = None
        if reasoning_effort:
            config_overrides = {"model_reasoning_effort": reasoning_effort}
        try:
            # Cold thread/resume is the only place where we intentionally carry
            # a narrow slice of binding-wise next-turn settings as one-shot
            # runtime overrides, so the first post-resume autonomous turn does
            # not have to fall back to stale loaded-thread defaults. This does
            # not promote binding-wise settings into a persisted thread-owned
            # truth source; loaded-thread correction still stays on
            # thread/settings/update, and ordinary prompt turns still inject
            # their own turn-scoped overrides.
            return self._adapter.resume_thread(
                thread_id,
                config_overrides=config_overrides,
                model=model or None,
                approval_policy=approval_policy or None,
                permissions_profile_id=permissions_profile_id or None,
            )
        except Exception as exc:
            if lease_was_newly_acquired:
                self._release_service_thread_runtime_lease(thread_id)
            if self._is_thread_not_found_error(exc):
                raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
            if thread and thread.source == "cli" and self._is_transport_disconnect(exc):
                raise RuntimeError(
                    "Codex 当前无法通过 app-server 恢复这个 CLI 线程。"
                    "这通常意味着该线程正被本地 TUI 使用，或当前版本暂不支持加载它的完整历史。"
                ) from exc
            raise

    def _resume_goal_on_runtime(
        self,
        sender_id: str,
        chat_id: str,
        attach_binding: bool,
        message_id: str = "",
    ) -> None:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            self._reply_text(chat_id, "当前没有绑定 thread；请先直接发送消息、执行 `/new`，或 `/resume` 目标线程。", message_id=message_id)
            return
        try:
            goal = self._adapter.get_thread_goal(thread_id)
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                self._reply_card(
                    chat_id,
                    build_markdown_card("Codex Goal 操作失败", "当前 backend 未启用 goal 功能。", template="red"),
                    message_id=message_id,
                )
                return
            raise
        if goal is None:
            self._reply_card(
                chat_id,
                build_markdown_card("Codex Goal 操作失败", "当前 thread 没有可恢复的 goal。", template="red"),
                message_id=message_id,
            )
            return
        approval_policy = runtime.approval_policy or None
        permissions_profile_id = runtime.permissions_profile_id or None
        model = runtime.model or None
        reasoning_effort = runtime.reasoning_effort or None
        loaded_thread_ids = set(self._adapter.list_loaded_thread_ids())
        was_loaded = thread_id in loaded_thread_ids
        snapshot: ThreadSnapshot | None = None
        effective_goal = goal
        paused_for_cold_sync = False
        try:
            if not was_loaded and goal.status == "active":
                effective_goal = self._adapter.set_thread_goal(thread_id, status="paused")
                paused_for_cold_sync = True
                self._update_runtime_goal_projection(sender_id, chat_id, message_id, effective_goal)
            carry_cold_binding_settings = not was_loaded
            if attach_binding or not was_loaded:
                snapshot = self._resume_snapshot_by_id(
                    thread_id,
                    original_arg=thread_id,
                    model=(model if carry_cold_binding_settings else None),
                    reasoning_effort=(reasoning_effort if carry_cold_binding_settings else None),
                    approval_policy=(approval_policy if carry_cold_binding_settings else None),
                    permissions_profile_id=(permissions_profile_id if carry_cold_binding_settings else None),
                )
            if attach_binding and snapshot is not None:
                self._bind_thread(sender_id, chat_id, snapshot.summary, message_id=message_id)
            self._adapter.update_thread_settings(
                thread_id,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            if goal.status != "active" or paused_for_cold_sync:
                effective_goal = self._adapter.set_thread_goal(thread_id, status="active")
            self._update_runtime_goal_projection(sender_id, chat_id, message_id, effective_goal)
        except Exception as exc:
            if paused_for_cold_sync:
                restored_goal = self._restore_paused_goal_after_failed_resume(thread_id)
                if restored_goal is not None:
                    self._update_runtime_goal_projection(sender_id, chat_id, message_id, restored_goal)
            logger.exception("恢复 goal 失败")
            self._reply_card(
                chat_id,
                build_markdown_card("Codex Goal 操作失败", str(exc) or "恢复 goal 失败", template="red"),
                message_id=message_id,
            )
            return
        notice = "已恢复当前 thread goal。"
        if attach_binding:
            notice += "\n当前会话已恢复接收该 thread 的飞书推送。"
        thread_title = self._get_runtime_view(sender_id, chat_id, message_id).current_thread_title.strip()
        self._reply_card(
            chat_id,
            build_goal_card(
                thread_id=thread_id,
                thread_title=thread_title,
                goal=effective_goal,
                notice=notice,
            ),
            message_id=message_id,
        )

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
    def _is_goals_feature_disabled_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        return str(exc.error.get("message", "") or "").strip().lower() == "goals feature is disabled"

    def _get_thread_goal_if_available(self, thread_id: str) -> ThreadGoalSummary | None:
        try:
            return self._adapter.get_thread_goal(thread_id)
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                return None
            raise

    def _restore_paused_goal_after_failed_resume(self, thread_id: str) -> ThreadGoalSummary | None:
        try:
            return self._adapter.set_thread_goal(thread_id, status="active")
        except Exception:
            logger.exception("恢复失败后回滚 paused goal 失败: thread=%s", thread_id[:12])
            return None

    def _compact_start_failure_text(self, thread_id: str, exc: Exception) -> str | None:
        if not self._is_compact_live_runtime_unavailable_error(exc):
            return None
        try:
            self._adapter.read_thread(thread_id, include_turns=False)
        except Exception as read_exc:
            logger.warning(
                "compact 启动失败后无法确认 thread 状态: thread=%s compact_error=%s read_error=%s",
                thread_id[:12],
                exc,
                read_exc,
            )
            return (
                "当前 backend 无法直接 compact 这条 thread，且暂时无法确认它只是未加载，"
                "还是持久化记录已不可读。\n"
                "可稍后重试，或先执行 `/attach`，或直接发送一条普通消息尝试恢复。"
            )
        return (
            "当前 thread 尚未加载到本实例 backend，无法 compact。\n"
            "先执行 `/attach`，或直接发送一条普通消息恢复该 thread。"
        )

    @staticmethod
    def _is_thread_not_loaded_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not loaded:")

    @staticmethod
    def _is_compact_thread_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not found:")

    def _is_compact_live_runtime_unavailable_error(self, exc: Exception) -> bool:
        return self._is_thread_not_loaded_error(exc) or self._is_compact_thread_not_found_error(exc)

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
        lease_was_newly_acquired = self._ensure_service_thread_runtime_lease(thread.thread_id)
        try:
            resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
            state = resolved.state
            chat_binding_key = resolved.binding
            with self._lock:
                unsubscribe_thread_id = self._binding_runtime.bind_thread_locked(
                    chat_binding_key,
                    state,
                    thread_id=thread.thread_id,
                    thread_title=thread.title,
                    working_dir=thread.cwd or state["working_dir"],
                    on_thread_replaced=self._replace_bound_thread_state_locked,
                    on_after_bind=self._clear_plan_state,
                )
        except Exception:
            if lease_was_newly_acquired:
                self._release_service_thread_runtime_lease(thread.thread_id)
            raise
        if unsubscribe_thread_id:
            try:
                self._adapter.unsubscribe_thread(unsubscribe_thread_id)
            except Exception:
                logger.exception("切换 binding 后回收旧 thread 订阅失败: thread=%s", unsubscribe_thread_id[:12])
            try:
                self._release_service_thread_runtime_lease(unsubscribe_thread_id)
            except Exception:
                logger.exception("切换 binding 后释放旧 runtime lease 失败: thread=%s", unsubscribe_thread_id[:12])
        self._refresh_bound_thread_goal_projection(
            sender_id,
            chat_id,
            thread.thread_id,
            message_id=message_id,
        )

    def _clear_thread_binding(self, sender_id: str, chat_id: str, *, message_id: str = "") -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        chat_binding_key = resolved.binding
        unsubscribe_thread_id: str = ""
        with self._lock:
            unsubscribe_thread_id = self._binding_runtime.clear_thread_binding_locked(
                chat_binding_key,
                state,
                on_clear_state=self._clear_bound_thread_state_locked,
            )
        if unsubscribe_thread_id:
            self._adapter.unsubscribe_thread(unsubscribe_thread_id)
            self._release_service_thread_runtime_lease(unsubscribe_thread_id)

    def _update_runtime_goal_projection(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        goal: ThreadGoalSummary | None,
    ) -> None:
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        with self._lock:
            self._apply_thread_goal_projection_locked(state, goal)

    def _refresh_bound_thread_goal_projection(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> None:
        try:
            goal = self._adapter.get_thread_goal(thread_id)
        except Exception:
            logger.debug("读取 thread goal 失败: thread=%s", thread_id[:12], exc_info=True)
            return
        resolved = self._resolve_runtime_binding(sender_id, chat_id, message_id)
        state = resolved.state
        with self._lock:
            if str(state["current_thread_id"] or "").strip() != str(thread_id or "").strip():
                return
            self._apply_thread_goal_projection_locked(state, goal)

    def _list_global_threads(self) -> list[ThreadSummary]:
        return list_global_threads(
            self._adapter,
            limit=self._thread_list_query_limit,
        )

    def _list_visible_current_dir_threads(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> list[ThreadSummary]:
        runtime = self._get_runtime_view(sender_id, chat_id, message_id)
        return list_current_dir_threads(
            self._adapter,
            cwd=runtime.working_dir,
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

    def _interrupt_binding_execution_for_backend_reset(
        self,
        binding: ChatBindingKey,
        *,
        note: str,
    ) -> str:
        sender_id, chat_id = binding
        state = self._get_runtime_state(sender_id, chat_id)
        with self._lock:
            if not self._turn_execution.has_active_execution_locked(state):
                return ""
            self._turn_execution.append_process_note_locked(
                state,
                text=f"\n[中断] {note}\n",
                marks_work=True,
            )
            self._apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(
                    cancelled=True,
                    pending_cancel=False,
                    runtime_channel_state="live",
                ),
            )
        self._finalize_execution_card_from_state(sender_id, chat_id)
        return format_binding_id(binding)

    def _reset_current_instance_backend(self, force: bool) -> dict[str, Any]:
        preview = self._runtime_admin.backend_reset_preview()
        if preview.status == "blocked":
            raise ValueError(preview.reason_text)
        if preview.status == "force-only" and not force:
            raise ValueError(preview.reason_text)

        reset_note = "管理员已重置当前实例 backend，本轮执行已中断。"
        with self._lock:
            binding_keys = list(self._binding_runtime.binding_keys_locked())
            active_bindings = [
                binding
                for binding in binding_keys
                if self._turn_execution.has_active_execution_locked(self._get_runtime_state(binding[0], binding[1]))
            ]
            bound_thread_ids = sorted(
                {
                    str(snapshot.thread_id or "").strip()
                    for binding in binding_keys
                    for snapshot in [self._binding_runtime.binding_runtime_snapshot_locked(binding)]
                    if snapshot is not None and str(snapshot.thread_id or "").strip()
                }
            )

        interrupted_binding_ids = [
            binding_id
            for binding_id in (
                self._interrupt_binding_execution_for_backend_reset(binding, note=reset_note)
                for binding in active_bindings
            )
            if binding_id
        ]

        with self._lock:
            detached_binding_ids: list[str] = []
            for thread_id in bound_thread_ids:
                result = self._binding_runtime.detach_thread_bindings_locked(
                    thread_id,
                    detach_availability=lambda _thread_id: (True, ""),
                    on_release_binding_state=self._cancel_runtime_timers_locked,
                )
                detached_binding_ids.extend(result.detached_binding_ids)
                for binding in self._binding_runtime.bound_bindings_for_thread_locked(thread_id):
                    state = self._get_runtime_state(binding[0], binding[1])
                    self._apply_persisted_runtime_state_message_locked(
                        binding,
                        state,
                        ThreadStateChanged(feishu_runtime_state=FEISHU_RUNTIME_DETACHED),
                    )
                    self._sync_stored_binding_locked(binding, state)

        fail_closed_request_count = self._interaction_requests.fail_close_all_requests()
        self._adapter.stop()
        purged_thread_ids = self._thread_runtime_lease_store.purge_all_for_instance(
            instance_name=self._instance_name,
        )
        self._adapter.start()
        self._register_instance_runtime()
        return {
            "force": bool(force),
            "detached_binding_ids": sorted(set(detached_binding_ids)),
            "interrupted_binding_ids": sorted(set(interrupted_binding_ids)),
            "fail_closed_request_count": fail_closed_request_count,
            "purged_thread_ids": sorted(set(purged_thread_ids)),
            "app_server_url": self._adapter.current_app_server_url(),
        }

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

    def _handle_adapter_disconnect(self) -> None:
        self._runtime_submit(self._handle_adapter_disconnect_impl)

    def _handle_adapter_disconnect_impl(self) -> None:
        affected_bindings: list[tuple[str, str]] = []
        with self._lock:
            for binding in self._binding_runtime.binding_keys_locked():
                snapshot = self._binding_runtime.binding_runtime_snapshot_locked(binding)
                if snapshot is None:
                    continue
                if snapshot.feishu_runtime_state != FEISHU_RUNTIME_ATTACHED or not snapshot.thread_id:
                    continue
                affected_bindings.append(binding)
                state = self._binding_runtime.get_or_create_runtime_state_locked(binding)
                if self._turn_execution.has_active_execution_locked(state):
                    self._turn_execution.apply_terminal_error_locked(
                        state,
                        error_message="Codex websocket disconnected",
                    )
        pending_fail_closed = self._interaction_requests.fail_close_all_requests_without_response(
            note="当前实例与 Codex backend 的 websocket 已断开，已自动结束该请求。",
        )
        if not affected_bindings:
            if pending_fail_closed:
                logger.warning(
                    "Codex websocket disconnected; detached bindings=%s threads=%s pending=%s",
                    [],
                    [],
                    pending_fail_closed,
                )
            return
        result = self._runtime_admin.fail_close_service_attached_runtime()
        for sender_id, chat_id in affected_bindings:
            self._finalize_execution_card_from_state(sender_id, chat_id)
        logger.warning(
            "Codex websocket disconnected; detached bindings=%s threads=%s pending=%s",
            result["detached_binding_ids"],
            result["detached_thread_ids"],
            pending_fail_closed,
        )

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

    def _dispatch_execution_card_message(
        self,
        message_id: str,
        *,
        transcript: ExecutionTranscript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> None:
        self._execution_output.dispatch_execution_card_message(
            message_id,
            transcript=transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
        )

    def _remove_execution_card_message(self, message_id: str) -> bool:
        return self._runtime_card_publisher().delete_card_message(message_id)

    def _schedule_execution_card_update(self, sender_id: str, chat_id: str) -> None:
        self._execution_output.schedule_execution_card_update(sender_id, chat_id)

    def _flush_execution_card(
        self,
        sender_id: str,
        chat_id: str,
        immediate: bool = False,
        *,
        background: bool = False,
    ) -> None:
        self._execution_output.flush_execution_card(
            sender_id,
            chat_id,
            immediate=immediate,
            background=background,
        )

    def _publish_terminal_result(
        self,
        chat_id: str,
        *,
        final_reply_text: str,
        source_execution_message_id: str = "",
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id and prompt_message_id and hasattr(self.bot, "get_message_context"):
            try:
                context = self.bot.get_message_context(prompt_message_id) or {}
                normalized_thread_id = str(context.get("thread_id", "") or "").strip()
            except Exception:
                normalized_thread_id = ""
        return self._execution_output.publish_terminal_result(
            chat_id,
            final_reply_text=final_reply_text,
            source_execution_message_id=source_execution_message_id,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            thread_id=normalized_thread_id,
        )

    def _clear_plan_state(self, state: RuntimeStateDict) -> None:
        self._turn_execution.clear_plan_state_locked(state)

    def _flush_plan_card(self, sender_id: str, chat_id: str) -> None:
        self._execution_output.flush_plan_card(sender_id, chat_id)
