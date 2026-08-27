"""Focus service runtime composition and ingress façade."""

from __future__ import annotations

import atexit
import logging
import pathlib
import threading
import time
from dataclasses import replace
from typing import Any, Callable

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.approval_policy import USER_SELECTABLE_APPROVAL_POLICIES
from bot.adapters.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerConfig,
)
from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapter_event_bridge import (
    AdapterEventBridge,
    AdapterEventBridgePorts,
)
from bot.adapter_notification_controller import (
    AdapterNotificationController,
    AdapterNotificationEffects,
)
from bot.adapter_notification_pipeline import AdapterNotificationPipeline
from bot.adapter_notification_runtime import (
    AdapterNotificationRuntimeTransitions,
    RememberTerminalResultTextCommand,
)
from bot.backend_reset.contract import BACKEND_RESET_STATUS_AVAILABLE
from bot.backend_reset.coordinator import BackendResetCoordinator
from bot.backend_reset.interaction_coordinator import (
    BackendResetInteractionCoordinator,
)
from bot.backend_reset.service import BackendResetService, BackendResetServicePorts
from bot.binding_execution_runtime import (
    BindingExecutionRuntimeTransitions,
)
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.feishu_binding_transition import FeishuBindingTransitionOwner
from bot.codex_config import CodexConfig
from bot.config import load_config_file
from bot.constants import resolve_working_dir
from bot.instance_layout import current_instance_name, global_data_dir
from bot.stores.instance_registry_store import InstanceRegistryStore
from bot.codex_protocol.client import (
    CodexRpcProtocolError,
)
from bot.codex_goal_domain import CodexGoalDomain, GoalDomainPorts
from bot.codex_group_domain import CodexGroupDomain, GroupDomainPorts
from bot.codex_help_domain import CodexHelpDomain
from bot.codex_threads_ui_domain import CodexThreadsUiDomain, ThreadsUiPorts
from bot.codex_settings_domain import (
    CodexSettingsDomain,
    SettingsDomainPorts,
)
from bot.focus_runtime.binding_coordinator import BindingRuntimeCoordinator
from bot.focus_runtime.feishu_platform import FeishuPlatform
from bot.focus_runtime.feishu_surface import FeishuSurface
from bot.focus_runtime.feishu_thread_session_composition import (
    compose_feishu_thread_sessions,
)
from bot.focus_runtime.execution_recovery_composition import (
    compose_execution_recovery,
)
from bot.focus_runtime.service_authority import ServiceRuntimeAuthority
from bot.focus_runtime.terminal_results import TerminalResults
from bot.focus_runtime.thread_targets import CodexThreadTargetService
from bot.focus_runtime.web_gateway_composition import compose_web_gateway
from bot.service_runtime_lifecycle import (
    ServiceRuntimeActivationPorts,
    ServiceRuntimeIngressDispatcher,
    ServiceRuntimeLifecycle,
    ServiceRuntimePhase,
    ServiceRuntimeShutdownPorts,
)
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_output_runtime import ExecutionOutputRuntimeTransitions
from bot.execution_recovery_runtime import (
    CommitCompactStartUnknownCommand,
    ExecutionRecoveryRuntimeTransitions,
    PrepareCompactStartUnknownCommand,
    TerminalReconcileTarget,
)
from bot.exception_chain import iter_exception_chain
from bot.generated_image_delivery import GeneratedImageDeliveryController
from bot.file_message_domain import FileMessageDomain, FileMessagePorts
from bot.operational_warnings import (
    FocusRuntimeTaskObserver,
    OperationalWarningRegistry,
)
from bot.feishu_root_operation_contract import (
    FeishuRootOperationPorts,
    FeishuRootOperationToken,
)
from bot.feishu_root_operation_controller import FeishuRootOperationController
from bot.feishu_continuation_controller import FeishuContinuationController
from bot.feishu_resume_settlement import FeishuResumeSettlementService
from bot.feishu_runtime_disconnect_projection import (
    FeishuRuntimeDisconnectProjection,
)
from bot.feishu_compact_execution_service import (
    COMPACT_START_OUTCOME_UNKNOWN_TEXT,
    FeishuCompactAdapterPort,
    FeishuCompactExecutionPorts,
    FeishuCompactExecutionService,
    FeishuCompactPresentationPort,
    FeishuCompactRootOperationPort,
    FeishuCompactRuntimePort,
)
from bot.feishu_execution_queue import FeishuExecutionQueueController
from bot.feishu_execution_queue_service import (
    FeishuExecutionQueueService,
    FeishuExecutionQueueServicePorts,
    prepare_queued_prompt_text,
    remember_message_context,
)
from bot.feishu_execution_finalization_controller import (
    FeishuExecutionFinalizationController,
    FeishuExecutionFinalizationResult,
    FeishuExecutionFinalizationPorts,
    FeishuExecutionRuntimeChanged,
)
from bot.feishu_destination_liveness import (
    FeishuDestinationLivenessCoordinator,
    FeishuDestinationLivenessPorts,
)
from bot.feishu_destination_liveness_contract import FeishuDestinationLossProof
from bot.interaction_request_controller import InteractionRequestController
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.permissions_profile import (
    PERMISSION_PROFILE_CHOICES,
    permissions_profile_choice_key,
    permissions_profile_label,
)
from bot.operation_owner_coordinator import (
    OperationOwnerCoordinator,
)
from bot.fcodex.control_dispatcher import FcodexControlDispatcher
from bot.fcodex.participant_runtime_registry import (
    FcodexParticipantRuntimeRegistry,
    FcodexParticipantRuntimeRegistryPorts,
)
from bot.prompt_turn_entry_controller import (
    FeishuRootOperationPort,
    InteractionPort,
    PresentationPort,
    PromptTurnEntryController,
    PromptTurnEntryPorts,
    ThreadSessionPort,
)
from bot.runtime_admin.controller import (
    RuntimeAdminController,
    RuntimeAdminCoordinationPort,
    RuntimeAdminPolicyPort,
    RuntimeAdminPorts,
    RuntimeAdminPresentationPort,
    RuntimeAdminThreadPort,
)
from bot.runtime_admin.control_router import (
    loaded_thread_inventory_control_response,
)
from bot.runtime_admin.binding_clear import RuntimeBindingBatchDeactivationOwner
from bot.runtime_card_publisher import ExecutionCardPatchDispatcher
from bot.thread_lifecycle_service import (
    ThreadLifecycleAdmissionPort,
    ThreadLifecycleBackendPort,
    ThreadLifecycleCleanupPort,
    ThreadLifecyclePorts,
    ThreadLifecycleService,
)
from bot.runtime_state import FEISHU_RUNTIME_ATTACHED
from bot.service_control_plane import ServiceControlPlane
from bot.server_request_registry import ServerRequestRegistry
from bot.server_request_coordinator import ServerRequestCoordinator, ServerRequestCoordinatorPorts
from bot.server_request_surface_dispatcher import (
    ServerRequestSurfaceDispatcher,
    ServerRequestSurfaceDispatcherPorts,
)
from bot.server_request_dispatch import ServerRequestSurfaceClaim
from bot.interaction_auto_resolution import InteractionAutoResolutionController
from bot.direct_thread_target_policy import DirectThreadTargetRegistry
from bot.stores.pending_attachment_store import PendingAttachmentStore
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.generated_image_delivery_store import GeneratedImageDeliveryStore
from bot.stores.feishu_app_connection_lease import FeishuAppConnectionLease
from bot.stores.feishu_destination_loss_store import FeishuDestinationLossStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.terminal_result_store import TerminalResultStore
from bot.stores.service_instance_lease import (
    ServiceInstanceLease,
    ServiceInstanceLeaseError,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLeaseStore
from bot.stores.web_attachment_store import WebAttachmentStore
from bot.stores.web_next_turn_settings_store import (
    WebNextTurnSettings,
    WebNextTurnSettingsStore,
)
from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.thread_runtime_coordination import (
    MANAGED_LOADED_INVENTORY_RPC_TIMEOUT_SECONDS,
)
from bot.thread_runtime_authority import (
    ThreadResumeLocalCommitFailed,
    ThreadResumeOutcomeUnknown,
    ThreadResumeSettlementError,
    ThreadRuntimeAuthority,
)
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_image_delivery import ThreadImageDeliveryController
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from bot.runtime_loop import RuntimeLoop, RuntimeLoopClosedError
from bot.platform_paths import default_data_root, default_working_dir
from bot.web_runtime.gateway import WebGatewayConfig
from bot.web_runtime.notification_projection import (
    WebNotificationProjectionReceipt,
)
from bot.web_runtime.backend_reset_controller import (
    WebBackendResetController,
    WebBackendResetControllerPorts,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interaction_inbox import WebInteractionInbox, WebInteractionInboxPorts
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.controller import WebRuntimeController, WebRuntimePorts

logger = logging.getLogger("bot.focus_runtime")

_SERVER_REQUEST_TOMBSTONE_LIMIT = 512
_RUNTIME_SLOW_QUEUE_SECONDS = 1.0
_RUNTIME_SLOW_TASK_SECONDS = 5.0
_OPERATIONAL_STATUS_POLL_SECONDS = 15.0
_APPROVAL_POLICIES = set(USER_SELECTABLE_APPROVAL_POLICIES)
_LOCAL_THREAD_SAFETY_RULE = (
    "同一线程允许多端订阅观察，但同一 live turn 只有一个交互 owner；非 owner 只能看，不能写或处理审批。"
)
def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _permissions_summary(permissions_profile_id: str) -> str:
    choice = permissions_profile_choice_key(permissions_profile_id)
    if choice:
        return PERMISSION_PROFILE_CHOICES[choice]["label"]
    return permissions_profile_label(permissions_profile_id)


class FocusRuntime:
    """Compose and expose the Focus service runtime owner graph."""

    def __init__(self, data_dir: pathlib.Path | None = None, config_dir: pathlib.Path | None = None):
        feishu_platform = FeishuPlatform()
        self._feishu_platform = feishu_platform
        cfg = CodexConfig.from_dict(load_config_file("codex", directory=config_dir))

        self._data_dir = data_dir or default_data_root()
        self._config_dir = config_dir
        self._instance_name = current_instance_name(config_dir=self._config_dir, data_dir=self._data_dir)
        self._global_data_dir = global_data_dir()
        self._lock = threading.RLock()
        self._web_projection = FocusWebProjection()
        self._thread_subscription_registry = ThreadSubscriptionRegistry(
            membership_changed=lambda thread_id: self._web_projection.publish(
                "thread_invalidated",
                thread_id=thread_id,
                reason="feishu_audience_changed",
            )
        )
        self._interaction_lease_store = InteractionLeaseStore(self._data_dir)
        self._web_writer_profile_store = WebWriterProfileStore(self._data_dir)
        self._operational_warnings = OperationalWarningRegistry()
        self._runtime_loop = RuntimeLoop(
            name="codex-handler-runtime",
            slow_queue_threshold_seconds=_RUNTIME_SLOW_QUEUE_SECONDS,
            slow_task_threshold_seconds=_RUNTIME_SLOW_TASK_SECONDS,
            task_observer=FocusRuntimeTaskObserver(
                self._operational_warnings, _RUNTIME_SLOW_QUEUE_SECONDS, _RUNTIME_SLOW_TASK_SECONDS
            ),
        )
        self._service_instance_lease = ServiceInstanceLease(self._data_dir)
        self._feishu_app_connection_lease = FeishuAppConnectionLease(self._global_data_dir)
        self._instance_registry = InstanceRegistryStore(self._global_data_dir)
        self._thread_runtime_lease_store = ThreadRuntimeLeaseStore(self._global_data_dir)
        self._service_control_plane = ServiceControlPlane(
            data_dir=self._data_dir,
            dispatch=self._handle_service_control_request,
            owns_current_lease=self._service_instance_lease.owns_current_lease,
            auth_token=lambda: self._service_instance_lease.owner_token,
        )
        self._default_working_dir = resolve_working_dir(
            cfg.default_working_dir,
            fallback=str(default_working_dir()),
        )
        self._threads_initial_limit = cfg.threads_initial_limit
        self._thread_list_query_limit = cfg.thread_list_query_limit
        self._stream_patch_interval_ms = cfg.stream_patch_interval_ms
        self._terminal_result_card_limit = cfg.terminal_result_card_limit
        self._mirror_watchdog_seconds = cfg.mirror_watchdog_seconds
        self._compact_start_timeout_seconds = cfg.compact_start_timeout_seconds
        self._attachment_ttl_seconds = cfg.attachment_ttl_seconds
        self._web_attachment_store = WebAttachmentStore(
            self._data_dir,
            ttl_seconds=self._attachment_ttl_seconds,
        )
        web_static_dir_raw = cfg.web_static_dir.strip()
        self._web_config = WebGatewayConfig(
            enabled=cfg.web_enabled,
            instance_name=self._instance_name,
            host=cfg.web_host,
            port=cfg.web_port,
            trusted_proxy_origin=cfg.web_trusted_proxy_origin,
            trusted_proxy_proof_sha256=cfg.web_trusted_proxy_proof_sha256,
            session_ttl_seconds=cfg.web_session_ttl_seconds,
            disconnect_grace_seconds=cfg.web_disconnect_grace_seconds,
            static_dir=pathlib.Path(web_static_dir_raw).expanduser() if web_static_dir_raw else None,
        )
        self._adapter_config = CodexAppServerConfig.from_config(cfg)
        self._web_next_turn_settings_store = WebNextTurnSettingsStore(
            self._data_dir,
            initial=WebNextTurnSettings(
                approval_policy=self._adapter_config.approval_policy,
                permissions_profile_id=self._adapter_config.permissions_profile_id,
                model=self._adapter_config.model,
                reasoning_effort=self._adapter_config.reasoning_effort,
            ),
        )
        self._effective_settings = ThreadEffectiveSettingsRegistry()
        self._app_server_runtime = AppServerRuntimeStore(self._data_dir)
        self._chat_binding_store = ChatBindingStore(self._data_dir)
        self._destination_loss_store = FeishuDestinationLossStore(self._data_dir)
        self._pending_attachment_store = PendingAttachmentStore(self._data_dir)
        terminal_result_store = TerminalResultStore(self._data_dir)
        self._feishu_execution_queue = FeishuExecutionQueueController(
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
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
            is_group_chat=feishu_platform.is_group_chat,
            owner_loss_settler=lambda c: self._feishu_root_operations.settle_owner_loss(c),
        )
        self._feishu_binding_transitions = FeishuBindingTransitionOwner(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            execution_queue=self._feishu_execution_queue,
        )
        self._binding_batch_deactivation = RuntimeBindingBatchDeactivationOwner(
            binding_runtime=self._binding_runtime,
            invalidate_execution_queue_locked=self._feishu_execution_queue.invalidate_binding,
        )
        self._execution_card_patch_dispatcher = ExecutionCardPatchDispatcher(
            lambda chat_id, message_id, model: (
                feishu_platform.runtime_card_publisher().patch_execution_card(
                    chat_id,
                    message_id,
                    model,
                )
            ),
        )
        self._turn_execution = TurnExecutionCoordinator()
        self._binding_execution_runtime = BindingExecutionRuntimeTransitions(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            turn_execution=self._turn_execution,
        )
        execution_output: ExecutionOutputController
        terminal_results = TerminalResults(
            platform=feishu_platform,
            store=terminal_result_store,
            resolve_session=self._binding_runtime.resolve_session,
            publish_terminal_result=lambda chat_id, **kwargs: execution_output.publish_terminal_result(
                chat_id, **kwargs
            ),
        )
        self._terminal_results = terminal_results
        execution_output = ExecutionOutputController(
            runtime=ExecutionOutputRuntimeTransitions(
                lock=self._lock,
                binding_runtime=self._binding_runtime,
                turn_execution=self._turn_execution,
            ),
            runtime_submit=self._runtime_submit,
            resolve_session=self._binding_runtime.resolve_session,
            card_publisher_factory=feishu_platform.runtime_card_publisher,
            dispatch_execution_card_patch=self._execution_card_patch_dispatcher.submit,
            reply_text=feishu_platform.reply_text,
            reply_text_get_id=feishu_platform.reply_text_get_id,
            record_terminal_result_card=terminal_results.record_terminal_result_card_with_execution,
            terminal_result_card_limit=lambda: self._terminal_result_card_limit,
            stream_patch_interval_ms=lambda: self._stream_patch_interval_ms,
        )
        self._execution_output = execution_output
        self._generated_image_delivery_store = GeneratedImageDeliveryStore(self._data_dir)
        self._generated_image_delivery = GeneratedImageDeliveryController(
            store=self._generated_image_delivery_store,
            reply_local_image=lambda chat_id, local_path, parent_message_id, reply_in_thread: feishu_platform.bot.reply_local_image(
                chat_id,
                local_path,
                parent_message_id=parent_message_id,
                reply_in_thread=reply_in_thread,
            ),
        )
        self._thread_image_delivery = ThreadImageDeliveryController(
            upload_image=lambda local_path: feishu_platform.bot.upload_image(local_path),
            send_image_by_key=lambda chat_id, image_key: feishu_platform.bot.send_image_by_key(chat_id, image_key),
        )
        self._execution_recovery_runtime = ExecutionRecoveryRuntimeTransitions(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            turn_execution=self._turn_execution,
        )
        self._execution_recovery = compose_execution_recovery(
            runtime=self._execution_recovery_runtime,
            runtime_call=self._runtime_call,
            binding_runtime=self._binding_runtime,
            adapter=lambda: self._adapter,
            adapter_ingress_gate=lambda: self._adapter_ingress_gate,
            terminal_execution=lambda: self._terminal_execution,
            finalize_execution=self._finalize_execution_for_recovery,
            execution_output=self._execution_output,
            mark_compact_start_outcome_unknown=(
                self._mark_compact_start_outcome_unknown_for_session
            ),
            publish_terminal_result=terminal_results.publish_terminal_result,
            has_recorded_terminal_result=terminal_results.has_recorded_terminal_result,
            deliver_generated_images_from_snapshot=self._deliver_generated_images_from_snapshot,
            mirror_watchdog_seconds=lambda: self._mirror_watchdog_seconds,
            compact_start_timeout_seconds=lambda: self._compact_start_timeout_seconds,
        )
        self._adapter_notification_runtime = AdapterNotificationRuntimeTransitions(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            turn_execution=self._turn_execution,
        )
        self._adapter_config = replace(
            self._adapter_config,
            app_server_data_dir=str(self._data_dir),
        )
        self._adapter = CodexAppServerAdapter(
            self._adapter_config,
            on_notification=lambda generation, method, params: self._adapter_events.handle_notification(
                generation,
                method,
                params,
            ),
            on_request=lambda generation, request_id, method, params: self._adapter_events.handle_request(
                generation,
                request_id,
                method,
                params,
            ),
            on_disconnect_ingress=(
                lambda generation: self._adapter_ingress_gate.fence_disconnect(
                    generation
                )
            ),
            on_disconnect=lambda generation: self._adapter_events.handle_disconnect(
                generation
            ),
            app_server_runtime_store=self._app_server_runtime,
            issue_outbound_request=(
                lambda method: self._adapter_ingress_gate.issue_outbound_request(method)
            ),
            guard_outbound_send=(
                lambda permit: self._adapter_ingress_gate.guard_outbound_send(permit)
            ),
            confirm_outbound_request=(
                lambda permit: self._adapter_ingress_gate.confirm_outbound_request(permit)
            ),
        )
        # Adapter callbacks become serialized only after they enter
        # RuntimeLoop. The gate is the sole owner of websocket-generation
        # admission, including the closed interval during backend reset.
        self._adapter_ingress_gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: self._adapter_events.handle_disconnect_impl(),
            activate_connection_epoch=lambda generation: self._server_request_coordinator.activate_connection_epoch(
                generation
            ),
        )
        service_authority = ServiceRuntimeAuthority(
            data_dir=self._data_dir,
            config_dir=self._config_dir,
            instance_name=self._instance_name,
            adapter=self._adapter,
            adapter_ingress_gate=self._adapter_ingress_gate,
            app_server_runtime=self._app_server_runtime,
            service_instance_lease=self._service_instance_lease,
            feishu_app_connection_lease=self._feishu_app_connection_lease,
            instance_registry=self._instance_registry,
            thread_runtime_lease_store=self._thread_runtime_lease_store,
            service_control_plane=self._service_control_plane,
        )
        self._service_runtime_authority = service_authority
        self._thread_runtime_authority = ThreadRuntimeAuthority(
            adapter=self._adapter,
            effective_settings=self._effective_settings,
            acquire_runtime_lease=service_authority.ensure_service_thread_runtime_lease,
            release_runtime_lease=service_authority.release_service_thread_runtime_lease,
            resume_failure_known_no_effect=CodexThreadTargetService.is_pre_send_error,
        )
        self._direct_thread_targets = DirectThreadTargetRegistry()
        self._codex_thread_targets = CodexThreadTargetService(
            adapter=self._adapter,
            binding_runtime=self._binding_runtime,
            thread_runtime_authority=self._thread_runtime_authority,
            direct_thread_targets=self._direct_thread_targets,
            thread_list_query_limit=self._thread_list_query_limit,
        )
        thread_targets = self._codex_thread_targets
        self._server_request_registry = ServerRequestRegistry(
            resolved_limit=_SERVER_REQUEST_TOMBSTONE_LIMIT,
        )
        self._fcodex_participant_runtime = FcodexParticipantRuntimeRegistry(
            ports=FcodexParticipantRuntimeRegistryPorts(
                thread_runtime_lease_store=self._thread_runtime_lease_store,
                runtime_holder_for_participant=service_authority.fcodex_runtime_holder,
                global_loaded_gate=service_authority.cross_instance_loaded_gate_check,
                schedule_participant_expiry=self._schedule_fcodex_participant_expiry,
                schedule_connection_expiry=self._schedule_fcodex_connection_expiry,
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
            disconnect_grace_seconds=self._web_config.disconnect_grace_seconds,
        )
        self._operation_owner = OperationOwnerCoordinator(
            interaction_lease_store=self._interaction_lease_store,
            participant_runtime_registry=self._fcodex_participant_runtime,
            external_thread_create_authority=self._thread_runtime_authority,
            effective_settings=self._effective_settings,
            server_request_is_resolved=(
                self._server_request_registry.request_is_resolved
            ),
            server_request_response_authority_is_revoked=(
                self._server_request_registry.request_response_authority_is_revoked
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
            respond=lambda identity, **kwargs: self._server_request_coordinator.submit_surface_response(
                identity, **kwargs
            ),
            schedule_proxy_delivery_expiry=self._schedule_fcodex_proxy_delivery_expiry,
            owner_changed=lambda thread_id, reason: self._web_projection.publish(
                "owner_changed",
                thread_id=thread_id,
                reason=reason,
            ),
        )
        self._fcodex_control_dispatcher = FcodexControlDispatcher(
            adapter=self._adapter,
            adapter_ingress_gate=self._adapter_ingress_gate,
            operation_owner=self._operation_owner,
            direct_thread_targets=self._direct_thread_targets,
        )
        self._web_interaction_inbox = WebInteractionInbox(
            ports=WebInteractionInboxPorts(
                respond=lambda identity, **kwargs: self._server_request_coordinator.submit_surface_response(
                    identity, **kwargs
                ),
                active_matches=self._server_request_registry.response_authority_is_open,
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        binding_coordinator: BindingRuntimeCoordinator
        web_runtime = WebRuntimeController(
            instance_name=self._instance_name,
            web_display_name=cfg.web_display_name,
            interaction_lease_store=self._interaction_lease_store,
            profile_store=self._web_writer_profile_store,
            next_turn_settings_store=self._web_next_turn_settings_store,
            attachment_store=self._web_attachment_store,
            remember_direct_thread_summary=self._direct_thread_targets.remember,
            effective_settings=self._effective_settings,
            projection=self._web_projection,
            document_registry=WebDocumentRegistry(runtime_context_guard=self._runtime_loop.assert_worker_context),
            interaction_inbox=self._web_interaction_inbox,
            ports=WebRuntimePorts(
                list_threads=lambda **kwargs: self._adapter.list_threads_all(**kwargs),
                read_thread=lambda thread_id, include_turns, **kwargs: self._adapter.read_thread(
                    thread_id,
                    include_turns=include_turns,
                    **kwargs,
                ),
                list_models=lambda: self._adapter.list_models(include_hidden=False),
                list_loaded_thread_ids=self._adapter.list_loaded_thread_ids,
                managed_loaded_thread_inventory=(
                    service_authority.managed_loaded_thread_inventory
                ),
                list_thread_runtime_leases=self._thread_runtime_lease_store.list,
                create_and_commit_thread=(
                    self._thread_runtime_authority.create_and_commit_thread
                ),
                begin_resume_thread_page=lambda thread_id, **kwargs: thread_targets.begin_web_thread_page(
                    thread_id,
                    original_arg=thread_id,
                    **kwargs,
                ),
                claim_resume_thread_page=(
                    self._thread_runtime_authority.claim_resume_thread_page
                ),
                acquire_claimed_resume_thread_page=(
                    self._thread_runtime_authority.acquire_claimed_resume_thread_page
                ),
                complete_claimed_resume_thread_page=(
                    self._thread_runtime_authority.complete_claimed_resume_thread_page
                ),
                abandon_resume_thread_page_claim=(
                    self._thread_runtime_authority.abandon_resume_thread_page_claim
                ),
                abandon_acquired_resume_thread_page=(
                    self._thread_runtime_authority.abandon_acquired_resume_thread_page
                ),
                execute_prepared_resume_thread_page=(
                    self._thread_runtime_authority.execute_prepared_resume_thread_page
                ),
                settle_prepared_resume_thread_page=(
                    self._thread_runtime_authority.settle_prepared_resume_thread_page
                ),
                list_thread_turns=lambda thread_id, **kwargs: self._adapter.list_thread_turns(
                    thread_id,
                    **kwargs,
                ),
                list_thread_items=lambda thread_id, **kwargs: self._adapter.list_thread_items(
                    thread_id,
                    **kwargs,
                ),
                search_thread_occurrences=lambda thread_id, **kwargs: self._adapter.search_thread_occurrences(
                    thread_id,
                    **kwargs,
                ),
                start_turn=lambda **kwargs: self._thread_runtime_authority.start_turn(**kwargs),
                steer_turn=lambda **kwargs: self._adapter.steer_turn(**kwargs),
                connection_generation=self._adapter.connection_generation,
                capture_connection_generation=(
                    self._adapter_ingress_gate.capture_existing_connection_generation
                ),
                run_if_connection_generation=(
                    self._adapter_ingress_gate.run_if_connection_generation
                ),
                compact_thread=self._adapter.compact_thread,
                start_review=lambda thread_id, **kwargs: self._adapter.start_review(
                    thread_id,
                    **kwargs,
                ),
                rename_thread=self._adapter.rename_thread,
                get_thread_goal=self._adapter.get_thread_goal,
                prepare_runtime_lease_preflight=(
                    service_authority.prepare_service_thread_runtime_preflight
                ),
                # WebRuntimeController has just point-read the direct target
                # and admitted the exact Web writer before these callbacks
                # run.  Do not route that already-proven writer through the
                # identity-less local control-plane gate: the Web document has
                # already supplied the exact frontend identity for this call.
                # `focusctl` continues to use RuntimeAdmin's separately
                # gated control-plane methods below.
                set_thread_goal=(
                    lambda thread_id, objective=None, status=None, **_kwargs: self._adapter.set_thread_goal(
                        thread_id,
                        objective=objective,
                        status=status,
                    )
                ),
                clear_thread_goal=(
                    lambda thread_id, **_kwargs: self._adapter.clear_thread_goal(thread_id)
                ),
                # WebRuntimeController carries the exact holder it just
                # admitted. Lifecycle policy is surface-neutral, so Web does
                # not route through the local-control/Feishu admin facade.
                archive_thread=lambda thread_id, *, writer_holder=None: self._thread_lifecycle.archive_thread_for_control(
                    thread_id,
                    writer_holder=writer_holder,
                ),
                unarchive_thread=lambda thread_id, *, writer_holder=None: self._thread_lifecycle.unarchive_thread_for_control(
                    thread_id,
                    writer_holder=writer_holder,
                ),
                delete_thread=lambda thread_id, *, writer_holder=None: self._thread_lifecycle.delete_thread_for_control(
                    thread_id,
                    writer_holder=writer_holder,
                ),
                interrupt_turn=lambda **kwargs: self._adapter.interrupt_turn(**kwargs),
                prepare_unsubscribe_thread=(
                    self._thread_runtime_authority.prepare_unsubscribe_thread
                ),
                execute_prepared_unsubscribe_thread=(
                    self._thread_runtime_authority.execute_prepared_unsubscribe_thread
                ),
                settle_prepared_unsubscribe_thread=(
                    self._thread_runtime_authority.settle_prepared_unsubscribe_thread
                ),
                abandon_prepared_unsubscribe_thread=(
                    self._thread_runtime_authority.abandon_prepared_unsubscribe_thread
                ),
                prepare_service_thread_runtime_lease_release=(
                    service_authority.prepare_service_thread_runtime_lease_release
                ),
                release_prepared_service_thread_runtime_lease=(
                    service_authority.release_prepared_service_thread_runtime_lease
                ),
                schedule_runtime_cleanup=self._schedule_web_runtime_cleanup,
                schedule_notification_projection=(
                    self._schedule_web_notification_projection
                ),
                schedule_attachment_cleanup=(
                    self._schedule_web_attachment_cleanup
                ),
                thread_subscribers=lambda thread_id: binding_coordinator.thread_subscribers(thread_id),
                has_external_pending_interaction_for_root=(
                    lambda root_thread_id: self._adapter_events.has_shared_pending_interaction_for_root(
                        root_thread_id
                    )
                ),
            ),
            runtime_call=self._runtime_call,
            default_working_dir=self._default_working_dir,
            thread_limit=max(self._thread_list_query_limit, 100),
        )
        self._web_runtime = web_runtime
        binding_coordinator = BindingRuntimeCoordinator(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            binding_batch_deactivation=self._binding_batch_deactivation,
            interaction_lease_store=self._interaction_lease_store,
            thread_runtime_authority=self._thread_runtime_authority,
            service_runtime_authority=service_authority,
            runtime_interest_retained=web_runtime.has_local_runtime_interest,
            codex_thread_targets=thread_targets,
            feishu_binding_transitions=self._feishu_binding_transitions,
        )
        self._binding_runtime_coordinator = binding_coordinator
        self._interaction_requests = InteractionRequestController(
            lock=self._lock,
            resident_session_snapshot_locked=self._binding_runtime.resident_session_snapshot_locked,
            interactive_binding_for_thread=binding_coordinator.interactive_binding_for_thread,
            interaction_actor_allowed=feishu_platform.interaction_actor_allowed,
            send_interactive_card=lambda *args: feishu_platform.runtime_card_publisher().send_interactive_card(*args),
            publish_interactive_card=lambda *args, **kwargs: feishu_platform.runtime_card_publisher().publish_interactive_card(*args, **kwargs),
            reply_text=feishu_platform.reply_text,
            respond=lambda identity, **kwargs: self._server_request_coordinator.submit_surface_response(identity, **kwargs),
            revoke_response_authority=lambda identity: self._server_request_coordinator.revoke_surface_response_authority(identity),
            patch_message=lambda chat_id, message_id, content: feishu_platform.bot.patch_message(chat_id, message_id, content).ok,
        )
        self._backend_reset_coordinator = BackendResetCoordinator(
            ingress_gate=self._adapter_ingress_gate,
            adapter=self._adapter,
            operation_owner=self._operation_owner,
            interaction_lease_store=self._interaction_lease_store,
            runtime_lease_store=self._thread_runtime_lease_store,
            instance_name=self._instance_name,
            runtime_authority=self._thread_runtime_authority,
            retire_server_requests_after_stop=lambda: self._server_request_coordinator.retire_connection_epoch(),
            retire_web_after_stop=lambda: self._web_runtime.retire_backend_epoch_after_stop(),
            retire_feishu_after_stop=lambda: self._interaction_requests.retire_backend_epoch_after_stop(),
            retire_feishu_root_operations_after_stop=lambda: self._feishu_root_operations.retire_backend_epoch_after_stop(),
            dispatch_feishu_card_projection_best_effort=self._interaction_requests.project_backend_reset_cards_best_effort,
            connect_timeout_seconds=self._adapter_config.connect_timeout_seconds,
            publish_replacement=lambda app_server_url: service_authority.register_instance_runtime(app_server_url=app_server_url),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        self._adapter_notifications = AdapterNotificationController(
            runtime=self._adapter_notification_runtime,
            thread_subscribers=binding_coordinator.thread_subscribers,
            effects=AdapterNotificationEffects(
                finalize_execution_from_terminal_signal=self._finalize_execution_from_terminal_signal,
                dispatch_execution_card_message=self._execution_output.dispatch_execution_card_message,
                open_initial_execution_page=self._execution_output.open_initial_execution_page,
                schedule_mirror_watchdog=self._execution_recovery.schedule_mirror_watchdog_for_session,
                schedule_execution_card_update=self._execution_output.schedule_execution_card_update_for_session,
                flush_execution_card=self._execution_output.flush_execution_card_for_session,
                flush_plan_card=self._execution_output.flush_plan_card_for_session,
                interrupt_running_turn=thread_targets.interrupt_running_turn,
                is_pre_send_error=thread_targets.is_pre_send_error,
            ),
        )
        self._adapter_notification_pipeline = AdapterNotificationPipeline(
            stages={
                "effective_settings_facts": (
                    lambda method, params: self._thread_runtime_authority.observe_notification(
                        method,
                        params,
                    )
                ),
                "active_turn_owner": lambda *args: self._adapter_events.reconcile_active_turn_lease_notification(*args),
                "server_requests": (
                    lambda method, params: self._adapter_events.handle_server_request_notification(
                        method,
                        params,
                    )
                ),
                "web_runtime": (
                    lambda method, params: self._web_runtime.handle_notification(method, params)
                ),
                "operation_owner": (
                    lambda method, params: self._operation_owner.notification(method, params)
                ),
                "feishu_projection": (
                    lambda method, params: self._adapter_notifications.handle_notification(
                        method,
                        params,
                    )
                ),
                "feishu_root_operation": (
                    lambda method, params: self._adapter_events.handle_feishu_root_operation_notification(
                        method,
                        params,
                    )
                ),
            },
            assert_runtime_context=self._runtime_loop.assert_worker_context,
        )
        self._interaction_auto_resolution = InteractionAutoResolutionController(
            runtime_submit=self._runtime_submit,
            on_due=lambda request_key, backend_epoch, generation: self._adapter_events.auto_resolve_interaction_request(
                request_key,
                backend_epoch,
                generation,
            ),
        )
        self._server_request_surface_dispatcher = ServerRequestSurfaceDispatcher(
            ServerRequestSurfaceDispatcherPorts(
                share_approval=lambda identity: self._adapter_events.share_server_request_approval(identity),
                share_desktop_interaction=lambda identity: self._adapter_events.share_server_request_desktop_interaction(
                    identity
                ),
                route_fcodex=lambda offer: self._adapter_events.route_fcodex_server_request(
                    offer.identity, routing_mode=offer.mode,
                ),
                schedule_auto_resolution=lambda request_key, enabled: (
                    self._interaction_auto_resolution.schedule(
                        request_key,
                        enabled=enabled,
                    )
                ),
                route_web=lambda offer: ServerRequestSurfaceClaim.from_retained(
                    self._web_runtime.handle_adapter_request(
                        offer.identity,
                        auto_resolution_timing=offer.auto_resolution_timing,
                        routing_mode=offer.mode,
                    )
                ),
                route_feishu=lambda offer: ServerRequestSurfaceClaim.from_retained(
                    self._interaction_requests.handle_adapter_request(
                        offer.identity,
                        auto_resolution_timing=offer.auto_resolution_timing,
                        routing_mode=offer.mode,
                    )
                ),
                web_has_pending=self._web_runtime.has_pending_request,
                feishu_has_pending=self._interaction_requests.has_pending_request,
                cancel_auto_resolution=(
                    lambda request_key, timing: (
                        self._interaction_auto_resolution.cancel_if_matches(
                            request_key,
                            timing.backend_epoch,
                            timing.generation,
                        )
                    )
                ),
            )
        )
        self._server_request_coordinator = ServerRequestCoordinator(
            self._server_request_registry,
            ServerRequestCoordinatorPorts(
                cancel_auto_resolution=self._interaction_auto_resolution.cancel,
                remove_web_resolved=(
                    self._web_runtime.remove_resolved_server_request
                ),
                revoke_web_response_authority=(
                    self._web_runtime.revoke_server_request_response_authority
                ),
                remove_fcodex_resolved=(
                    self._operation_owner.remove_resolved_server_request
                ),
                remove_feishu_resolved=(
                    self._interaction_requests.remove_resolved_server_request
                ),
                reconcile_resolved_root=(
                    lambda root_thread_id: self._adapter_events.reconcile_resolved_interaction_root(
                        root_thread_id
                    )
                ),
                invalidate_auto_resolution_epoch=self._interaction_auto_resolution.backend_disconnected,
                shutdown_auto_resolution=lambda: self._interaction_auto_resolution.shutdown(),
                dispatch_request=self._server_request_surface_dispatcher.dispatch,
                respond=lambda request_id, **kwargs: self._adapter.respond(
                    request_id,
                    **kwargs,
                ),
            ),
            self._runtime_loop.assert_worker_context,
        )
        self._backend_reset_interactions = BackendResetInteractionCoordinator.from_owner(
            self._server_request_coordinator
        )
        self._web_gateway = compose_web_gateway(
            config=self._web_config,
            data_dir=self._data_dir,
            projection=self._web_projection,
            web_runtime=self._web_runtime,
            backend_reset=lambda: self._web_backend_reset,
            ingress=lambda: self._ingress,
            runtime_call=self._runtime_call,
            operator_status=self._operational_status_snapshot,
        )
        feishu_surface: FeishuSurface
        self._settings_domain = CodexSettingsDomain(
            ports=SettingsDomainPorts(
                get_message_context=lambda message_id: feishu_platform.bot.get_message_context(message_id),
                get_sender_display_name=lambda **kwargs: feishu_platform.bot.get_sender_display_name(**kwargs),
                debug_sender_name_resolution=lambda open_id: feishu_platform.bot.debug_sender_name_resolution(open_id),
                get_bot_identity_snapshot=lambda: feishu_platform.bot.get_bot_identity_snapshot(),
                add_admin_open_id=lambda open_id: feishu_platform.bot.add_admin_open_id(open_id),
                set_configured_bot_open_id=lambda open_id: feishu_platform.bot.set_configured_bot_open_id(open_id),
                resolve_session=self._binding_runtime.resolve_session,
                list_models=lambda: self._adapter.list_models(include_hidden=True),
                update_runtime_settings=binding_coordinator.update_runtime_settings,
            ),
            approval_policies=_APPROVAL_POLICIES,
        )
        self._group_domain = CodexGroupDomain(
            ports=GroupDomainPorts(
                get_sender_display_name=lambda **kwargs: feishu_platform.bot.get_sender_display_name(**kwargs),
                get_message_context=lambda message_id: feishu_platform.bot.get_message_context(message_id),
                reply_text=feishu_platform.reply_text,
                get_group_mode=lambda chat_id: feishu_platform.bot.get_group_mode(chat_id),
                is_group_admin=lambda open_id: feishu_platform.bot.is_group_admin(open_id=open_id),
                get_group_activation_snapshot=lambda chat_id: feishu_platform.bot.get_group_activation_snapshot(chat_id),
                set_group_mode=lambda chat_id, mode: feishu_platform.bot.set_group_mode(chat_id, mode),
                activate_group_chat=lambda chat_id, activated_by: feishu_platform.bot.activate_group_chat(
                    chat_id,
                    activated_by=activated_by,
                ),
                deactivate_group_chat=lambda chat_id: feishu_surface.deactivate_group_chat(chat_id),
                is_group_chat=feishu_platform.is_group_chat,
                validate_group_mode_change=lambda chat_id, mode, message_id="": feishu_surface.validate_group_mode_change(
                    chat_id,
                    mode,
                    message_id=message_id,
                ),
            )
        )
        self._help_domain = CodexHelpDomain(
            local_thread_safety_rule=_LOCAL_THREAD_SAFETY_RULE,
            resolve_session=self._binding_runtime.resolve_session,
            is_group_chat=feishu_platform.is_group_chat,
            get_group_mode=lambda chat_id: feishu_platform.bot.get_group_mode(chat_id),
            get_group_activation_snapshot=lambda chat_id: feishu_platform.bot.get_group_activation_snapshot(chat_id),
        )
        self._file_message_domain = FileMessageDomain(
            ports=FileMessagePorts(
                get_message_context=lambda message_id: feishu_platform.bot.get_message_context(message_id),
                download_message_resource=lambda message_id, resource_key, **kwargs: feishu_platform.bot.download_message_resource(
                    message_id,
                    resource_key,
                    **kwargs,
                ),
                reply_text=feishu_platform.reply_text,
                resolve_session=self._binding_runtime.resolve_session,
                list_models=lambda: self._adapter.list_models(include_hidden=False),
                message_reply_in_thread=feishu_platform.message_reply_in_thread,
            ),
            store=self._pending_attachment_store,
            ttl_seconds=self._attachment_ttl_seconds,
            effective_settings=self._effective_settings,
        )
        self._thread_access_policy = ThreadAccessPolicy(
            lock=self._lock,
            is_group_chat=feishu_platform.is_group_chat,
            group_mode_for_chat=lambda chat_id: feishu_platform.bot.get_group_mode(chat_id),
            thread_subscribers_locked=self._binding_runtime.thread_subscribers,
            current_interaction_lease_locked=self._binding_runtime.current_interaction_lease_locked,
            feishu_interaction_holder=self._binding_runtime.feishu_interaction_holder,
        )
        self._feishu_root_operations = FeishuRootOperationController(
            ports=FeishuRootOperationPorts(
                verify_direct_thread_target=(
                    lambda root_thread_id: thread_targets.read_direct_thread_summary_authoritatively(
                        root_thread_id,
                        original_arg=root_thread_id,
                        operation="执行飞书主 turn 操作",
                    )
                ),
                prompt_write_admission=(
                    lambda binding, chat_id, thread_id, message_id: (
                        self._thread_access_policy.prompt_write_denial_check(
                            binding,
                            chat_id,
                            thread_id,
                            message_id=message_id,
                        )
                    )
                ),
                holder_for_binding=self._binding_runtime.feishu_interaction_holder,
                validate_binding_owner_receipt=(
                    self._binding_runtime.require_binding_owner_receipt_current
                ),
                acquire_interaction_lease=(
                    self._binding_runtime.acquire_interaction_lease_for_binding
                ),
                release_exact_interaction_lease=(
                    self._interaction_lease_store.release_if_matches
                ),
                activate_interaction_turn=(
                    self._interaction_lease_store.activate_turn
                ),
                lookup_interaction_lease=self._interaction_lease_store.load,
                read_root_status=(
                    lambda root_thread_id: thread_targets.read_direct_thread_summary_authoritatively(
                        root_thread_id,
                        original_arg=root_thread_id,
                        operation="对账飞书主 turn 状态",
                    ).status
                ),
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        self._feishu_resume_settlement = FeishuResumeSettlementService.from_root_operations(
            self._feishu_root_operations,
            operation_outcome_unknown=self._operation_start_outcome_unknown,
        )
        self._feishu_thread_sessions = compose_feishu_thread_sessions(
            lock=self._lock,
            adapter=self._adapter,
            binding_runtime=self._binding_runtime,
            binding_transitions=self._feishu_binding_transitions,
            thread_runtime=self._thread_runtime_authority,
            root_operations=self._feishu_root_operations,
            warnings=self._operational_warnings,
            execution_runtime=self._binding_execution_runtime,
            execution_output=self._execution_output,
            schedule_active_observer_recovery=self._execution_recovery.schedule_mirror_watchdog_for_session,
            acquire_runtime_lease=service_authority.ensure_service_thread_runtime_lease,
            release_runtime_lease=service_authority.release_service_thread_runtime_lease,
            runtime_interest_retained=self._web_runtime.has_local_runtime_interest,
            remember_direct_thread_summary=self._direct_thread_targets.remember,
            is_thread_not_found_error=thread_targets.is_thread_not_found_error,
            is_transport_disconnect=thread_targets.is_transport_disconnect,
        )
        self._feishu_continuation = FeishuContinuationController(
            lock=self._lock,
            adapter=self._adapter,
            binding_runtime=self._binding_runtime,
            access_policy=self._thread_access_policy,
            root_operations=self._feishu_root_operations,
            resume_settlement=self._feishu_resume_settlement,
            thread_sessions=self._feishu_thread_sessions,
            thread_runtime_authority=self._thread_runtime_authority,
            history_preview_rounds=cfg.history_preview_rounds,
            show_history_preview_on_resume=cfg.show_history_preview_on_resume,
            thread_list_query_limit=self._thread_list_query_limit,
            local_thread_safety_rule=_LOCAL_THREAD_SAFETY_RULE,
            logger=logger,
        )
        self._goal_domain = CodexGoalDomain(
            ports=GoalDomainPorts(
                resolve_session=self._binding_runtime.resolve_session,
                get_thread_goal=self._feishu_continuation.get_thread_goal,
                mutate_goal=self._feishu_continuation.mutate_goal,
                clear_goal=self._feishu_continuation.clear_goal,
                thread_mutation_denial_text=(
                    self._feishu_continuation.prompt_write_denial_text
                ),
                attach_current_binding=lambda sender_id, chat_id, message_id="": self._runtime_admin.attach_binding(
                    binding_coordinator.chat_binding_key(sender_id, chat_id, message_id),
                    writer_binding=binding_coordinator.chat_binding_key(sender_id, chat_id, message_id),
                ),
                update_runtime_goal_projection=self._feishu_continuation.project_goal,
                submit_to_runtime=self._runtime_submit,
                resume_goal=self._feishu_continuation.resume_goal,
                reply_card=feishu_platform.reply_card,
            )
        )
        self._threads_ui_domain = CodexThreadsUiDomain(
            continuation=self._feishu_continuation,
            ports=ThreadsUiPorts(
                submit_to_runtime=self._runtime_submit,
                resolve_session=self._binding_runtime.resolve_session,
                is_group_chat=feishu_platform.is_group_chat,
                is_group_admin_actor=feishu_platform.is_group_admin_actor,
                rename_bound_thread_title=binding_coordinator.rename_bound_thread_title,
                reply_text=feishu_platform.reply_text,
                reply_card=feishu_platform.reply_card,
                list_visible_current_dir_threads=thread_targets.list_visible_current_dir_threads,
                read_thread_summary_authoritatively=thread_targets.read_thread_summary_authoritatively,
                archive_thread_for_control=self._archive_thread_for_control,
                rename_thread=thread_targets.rename_direct_thread,
                patch_message=lambda chat_id, message_id, content: feishu_platform.bot.patch_message(
                    chat_id,
                    message_id,
                    content,
                ).ok,
                is_thread_not_loaded_error=thread_targets.is_thread_not_loaded_error,
                threads_initial_limit=self._threads_initial_limit,
            ),
        )
        self._thread_lifecycle = ThreadLifecycleService(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            ports=ThreadLifecyclePorts(
                backend=ThreadLifecycleBackendPort(
                    read_thread=lambda thread_id: self._adapter.read_thread(
                        thread_id,
                        include_turns=False,
                    ),
                    list_loaded_thread_ids=self._adapter.list_loaded_thread_ids,
                    archive_thread=self._thread_runtime_authority.archive_thread,
                    unarchive_thread=self._adapter.unarchive_thread,
                    delete_thread=self._thread_runtime_authority.delete_thread,
                    is_thread_not_found_error=thread_targets.is_thread_not_found_error,
                    is_thread_not_loaded_error=thread_targets.is_thread_not_loaded_error,
                ),
                admission=ThreadLifecycleAdmissionPort(
                    instance_name=lambda: self._instance_name,
                    load_runtime_lease=self._thread_runtime_lease_store.load,
                    external_write_denial_check=(
                        self._thread_access_policy.external_control_write_denial_check
                    ),
                    loaded_gate_check=service_authority.lifecycle_loaded_gate_check,
                ),
                cleanup=ThreadLifecycleCleanupPort(
                    binding_has_pending_request_locked=(
                        self._interaction_requests.binding_has_pending_request_locked
                    ),
                    invalidate_feishu_execution_queue_locked=(
                        self._feishu_execution_queue.invalidate_binding
                    ),
                    unsubscribe_thread=(
                        binding_coordinator.unsubscribe_thread_unless_web_runtime_requires_interest
                    ),
                    release_service_runtime_lease=(
                        binding_coordinator.release_service_thread_runtime_lease_unless_web_runtime_requires_interest
                    ),
                ),
            ),
        )
        self._destination_liveness = FeishuDestinationLivenessCoordinator(
            store=self._destination_loss_store,
            ports=FeishuDestinationLivenessPorts(
                lock=self._lock,
                runtime_call=self._runtime_call,
                runtime_context_guard=self._runtime_loop.assert_worker_context,
                binding_keys_for_chat_locked=(
                    self._binding_runtime.binding_keys_for_chat_locked
                ),
                deactivate_bindings_locked=(
                    self._binding_batch_deactivation.deactivate_locked
                ),
                finalize_deactivated_thread_runtime=lambda thread_id: (
                    binding_coordinator.finalize_deactivated_feishu_binding_thread_runtime(
                        thread_id,
                        cleanup_reason="chat_unavailable",
                    )
                ),
                fail_close_chat_requests=(
                    self._interaction_requests.fail_close_chat_requests
                ),
                forget_chat_state=lambda chat_id: (
                    feishu_platform.bot.forget_chat_state_after_destination_loss(chat_id)
                ),
            ),
        )
        self._runtime_admin = RuntimeAdminController(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            interaction_requests=self._interaction_requests,
            thread_lifecycle=self._thread_lifecycle,
            ports=RuntimeAdminPorts(
                thread=RuntimeAdminThreadPort(
                    read_thread=lambda thread_id: self._adapter.read_thread(
                        thread_id,
                        include_turns=False,
                    ),
                    read_thread_for_stale_cleanup=lambda thread_id: self._adapter.read_thread(
                        thread_id,
                        include_turns=False,
                    ),
                    list_loaded_thread_ids=lambda: self._adapter.list_loaded_thread_ids(),
                    current_app_server_url=service_authority.published_app_server_url,
                    unsubscribe_thread=(
                        binding_coordinator.unsubscribe_thread_unless_web_runtime_requires_interest
                    ),
                    attach_binding=(
                        self._feishu_continuation.attach_binding_for_control
                    ),
                    get_thread_goal=lambda thread_id: self._adapter.get_thread_goal(thread_id),
                    set_thread_goal=(
                        self._feishu_continuation.set_thread_goal_for_control
                    ),
                    clear_thread_goal=(
                        self._feishu_continuation.clear_thread_goal_for_control
                    ),
                    resolve_thread_target_for_control_params=(
                        thread_targets.resolve_thread_target_for_control_params
                    ),
                ),
                coordination=RuntimeAdminCoordinationPort(
                    clear_all_stored_bindings=self._chat_binding_store.clear_all,
                    deactivate_binding_and_invalidate_queue_locked=(
                        lambda key: self._binding_batch_deactivation.deactivate_locked(
                            (key,)
                        )
                    ),
                    deactivate_bindings_and_invalidate_queues_locked=(
                        self._binding_batch_deactivation.deactivate_locked
                    ),
                    release_service_thread_runtime_lease=(
                        binding_coordinator.release_service_thread_runtime_lease_unless_web_runtime_requires_interest
                    ),
                    service_control_endpoint=lambda: self._service_control_plane.control_endpoint,
                    web_gateway_enabled=lambda: self._web_config.enabled,
                    current_web_gateway_url=lambda: self._web_gateway.endpoint,
                    instance_name=lambda: self._instance_name,
                    load_thread_runtime_lease=lambda thread_id: self._thread_runtime_lease_store.load(thread_id),
                    pending_interaction_request_count=self._backend_reset_interactions.pending_count,
                    reset_current_instance_backend=self._reset_current_instance_backend,
                    submit_to_runtime=self._runtime_submit,
                    invalidate_feishu_execution_queue_locked=(
                        self._feishu_execution_queue.invalidate_binding
                    ),
                    invalidate_all_feishu_execution_queues_locked=self._feishu_execution_queue.invalidate_all,
                    operational_status=self._operational_status_snapshot,
                ),
                policy=RuntimeAdminPolicyPort(
                    prompt_write_denial_check=(
                        self._thread_access_policy.prompt_write_denial_check
                    ),
                    external_control_write_denial_check=self._thread_access_policy.external_control_write_denial_check,
                    all_mode_thread_exclusivity_check=self._thread_access_policy.all_mode_thread_exclusivity_violation_check,
                    detached_runtime_attach_check=service_authority.detached_runtime_attach_check,
                    is_thread_not_found_error=thread_targets.is_thread_not_found_error,
                    is_thread_not_loaded_error=thread_targets.is_thread_not_loaded_error,
                ),
                presentation=RuntimeAdminPresentationPort(
                    permissions_summary=_permissions_summary,
                    thread_image_delivery=self._thread_image_delivery,
                    reply_text=feishu_platform.reply_text,
                    reply_card=feishu_platform.reply_card,
                    submit_prompt_for_control=lambda binding, **kwargs: feishu_surface.submit_prompt_for_control(
                        binding, **kwargs
                    ),
                    resolve_binding_chat_display_name=feishu_platform.resolve_binding_chat_display_name,
                ),
            ),
        )
        self._feishu_runtime_disconnect = FeishuRuntimeDisconnectProjection(
            execution_runtime=self._binding_execution_runtime,
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        self._adapter_events = AdapterEventBridge(
            ingress_gate=self._adapter_ingress_gate,
            notification_pipeline=self._adapter_notification_pipeline,
            server_requests=self._server_request_coordinator,
            interaction_requests=self._interaction_requests,
            interaction_auto_resolution=self._interaction_auto_resolution,
            direct_thread_targets=self._direct_thread_targets,
            operation_owner=self._operation_owner,
            web_runtime=self._web_runtime,
            feishu_root_operations=self._feishu_root_operations,
            thread_runtime_authority=self._thread_runtime_authority,
            interaction_leases=self._interaction_lease_store,
            runtime_admin=self._runtime_admin,
            feishu_runtime_disconnect=self._feishu_runtime_disconnect,
            ports=AdapterEventBridgePorts(
                runtime_submit=self._runtime_submit,
                finalize_execution_card=(
                    lambda sender_id, chat_id: self._terminal_execution.finalize_ingress(
                        sender_id,
                        chat_id,
                    )
                ),
                thread_subscribers=binding_coordinator.thread_subscribers,
                resident_session=binding_coordinator.resident_session,
            ),
        )
        self._backend_reset_service = BackendResetService(
            lock=self._lock,
            binding_runtime=self._binding_runtime,
            execution_runtime=self._binding_execution_runtime,
            epoch_coordinator=self._backend_reset_coordinator,
            ports=BackendResetServicePorts(
                backend_reset_preview=self._runtime_admin.backend_reset_preview,
                invalidate_all_feishu_execution_queues_locked=(
                    self._feishu_execution_queue.invalidate_all
                ),
                finalize_execution=(
                    lambda session: (
                        self._terminal_execution.finalize(session)
                    )
                ),
                interaction_preparation=self._backend_reset_interactions,
                published_app_server_url=lambda: service_authority.published_app_server_url(),
                runtime_context_guard=self._runtime_loop.assert_worker_context,
            ),
        )
        self._web_backend_reset = WebBackendResetController(
            instance_name=self._instance_name,
            ports=WebBackendResetControllerPorts(
                backend_reset_preview=self._runtime_admin.backend_reset_preview,
                preview_connection_generation=(
                    self._backend_reset_coordinator.preview_connection_generation
                ),
                reset_current_instance=(
                    self._backend_reset_service.reset_current_instance
                ),
            ),
        )
        self._prompt_turn_entry = PromptTurnEntryController(
            execution_runtime=self._binding_execution_runtime,
            ports=PromptTurnEntryPorts(
                session=ThreadSessionPort(
                    resolve_session=self._binding_runtime.resolve_session,
                    clear_thread_binding=lambda sender_id, chat_id, message_id="": binding_coordinator.clear_thread_binding(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                    ),
                    reattach_bound_thread=(
                        self._feishu_thread_sessions.reattach_bound_thread
                    ),
                    create_and_bind_thread=(
                        self._feishu_thread_sessions.create_and_bind_thread
                    ),
                    message_reply_in_thread=feishu_platform.message_reply_in_thread,
                    group_actor_open_id=feishu_platform.group_actor_open_id,
                    access_policy=self._thread_access_policy,
                    detached_runtime_attach_check=service_authority.detached_runtime_attach_check,
                ),
                root_operation=FeishuRootOperationPort(
                    admit=self._feishu_root_operations.admit,
                    arm_continuation=(
                        self._feishu_root_operations.arm_continuation
                    ),
                    await_start_identity=(
                        self._feishu_root_operations.await_start_identity
                    ),
                    accept_prompt_start=(
                        self._feishu_root_operations.accept_prompt_start
                    ),
                    claim_prompt_interrupt_candidate=(
                        self._feishu_root_operations.claim_prompt_interrupt_candidate
                    ),
                    consume_prompt_interrupt_candidate=(
                        self._feishu_root_operations.consume_prompt_interrupt_candidate
                    ),
                    restore_prompt_interrupt_candidate_after_pre_send=(
                        self._feishu_root_operations.restore_prompt_interrupt_candidate_after_pre_send
                    ),
                    settle_known_failure=(
                        self._feishu_root_operations.settle_known_failure
                    ),
                    settle_known_mutation=(
                        self._feishu_root_operations.settle_known_mutation
                    ),
                    acknowledge_continuing=(
                        self._feishu_root_operations.acknowledge_continuing
                    ),
                    mark_outcome_unknown=(
                        self._feishu_root_operations.mark_outcome_unknown
                    ),
                    continuation_may_autostart=(
                        self._feishu_continuation.resume_may_autostart
                    ),
                ),
                interaction=InteractionPort(
                    runtime_recovery_reason=thread_targets.runtime_recovery_reason,
                    operation_outcome_unknown=self._operation_start_outcome_unknown,
                    is_turn_thread_not_found_error=thread_targets.is_turn_thread_not_found_error,
                    is_thread_not_found_error=thread_targets.is_thread_not_found_error,
                    is_pre_send_error=thread_targets.is_pre_send_error,
                    is_turn_interrupt_rejected_error=(
                        thread_targets.is_turn_interrupt_rejected_error
                    ),
                    start_turn=lambda **kwargs: (
                        self._thread_runtime_authority.start_turn(**kwargs)
                    ),
                    interrupt_running_turn=thread_targets.interrupt_running_turn,
                    finalize_input_items=self._file_message_domain.finalize_prompt_input,
                ),
                presentation=PresentationPort(
                    claim_reserved_execution_card=feishu_platform.claim_reserved_execution_card,
                    patch_message=lambda chat_id, message_id, content: feishu_platform.bot.patch_message(
                        chat_id,
                        message_id,
                        content,
                    ),
                    open_initial_execution_page=(
                        self._execution_output.open_initial_execution_page
                    ),
                    flush_execution_card_for_session=(
                        self._execution_output.flush_execution_card_for_session
                    ),
                    schedule_mirror_watchdog=self._schedule_mirror_watchdog,
                    reconcile_execution_snapshot=self._reconcile_execution_snapshot,
                    refresh_terminal_card=(
                        lambda session: self._terminal_execution.refresh_terminal_card(
                            session
                        )
                    ),
                    finalize_execution=(
                        lambda session: self._terminal_execution.finalize(session)
                    ),
                    mark_runtime_degraded=self._mark_runtime_degraded,
                    reply_text=feishu_platform.reply_text,
                    mirror_watchdog_seconds=lambda: self._mirror_watchdog_seconds,
                ),
            ),
        )
        self._feishu_compact_execution = FeishuCompactExecutionService(
            execution_runtime=self._binding_execution_runtime,
            ports=FeishuCompactExecutionPorts(
                runtime=FeishuCompactRuntimePort(
                    resolve_session=self._binding_runtime.resolve_session,
                    writer_denial_text=(
                        lambda binding, chat_id, thread_id, message_id: (
                            self._thread_access_policy.prompt_write_denial_text(
                                binding,
                                chat_id,
                                thread_id,
                                message_id=message_id,
                            )
                        )
                    ),
                    message_reply_in_thread=(
                        lambda message_id: feishu_platform.message_reply_in_thread(message_id)
                    ),
                    group_actor_open_id=feishu_platform.group_actor_open_id,
                ),
                root_operation=FeishuCompactRootOperationPort(
                    admit=lambda *args, **kwargs: self._feishu_root_operations.admit(
                        *args,
                        **kwargs,
                    ),
                    settle_known_failure=(
                        lambda *args, **kwargs: self._feishu_root_operations.settle_known_failure(
                            *args,
                            **kwargs,
                        )
                    ),
                    await_start_identity=(
                        lambda token: self._feishu_root_operations.await_start_identity(
                            token
                        )
                    ),
                    mark_start_outcome_unknown=(
                        lambda *args, **kwargs: self._mark_compact_start_outcome_unknown(
                            *args,
                            **kwargs,
                        )
                    ),
                ),
                adapter=FeishuCompactAdapterPort(
                    compact_thread=lambda thread_id: self._adapter.compact_thread(
                        thread_id
                    ),
                    read_thread=lambda *args, **kwargs: self._adapter.read_thread(
                        *args,
                        **kwargs,
                    ),
                    operation_start_outcome_unknown=self._operation_start_outcome_unknown,
                    is_thread_not_loaded_error=thread_targets.is_thread_not_loaded_error,
                ),
                presentation=FeishuCompactPresentationPort(
                    open_initial_execution_page=self._execution_output.open_initial_execution_page,
                    flush_execution_card_for_session=self._execution_output.flush_execution_card_for_session,
                    schedule_mirror_watchdog=(
                        lambda sender_id, chat_id: self._schedule_mirror_watchdog(
                            sender_id,
                            chat_id,
                        )
                    ),
                ),
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        self._feishu_execution_queue_service = FeishuExecutionQueueService(
            queue=self._feishu_execution_queue,
            ports=FeishuExecutionQueueServicePorts(
                lock=self._lock,
                ingress_snapshot=binding_coordinator.feishu_queue_ingress_snapshot,
                binding_execution_snapshot_locked=(
                    binding_coordinator.feishu_binding_execution_snapshot_locked
                ),
                binding_execution_active_locked=(
                    lambda binding: bool(
                        (
                            session := self._binding_runtime.resident_session_snapshot_locked(
                                binding
                            )
                        )
                        and session.execution.has_execution_anchor
                    )
                ),
                writer_denial_text=(
                    lambda binding, chat_id, root_thread_id, message_id: (
                        self._thread_access_policy.prompt_write_denial_text(
                            binding,
                            chat_id,
                            root_thread_id,
                            message_id=message_id,
                        )
                    )
                ),
                current_process_local_turn_id=(
                    self._thread_access_policy.current_process_local_turn_id
                ),
                prompt_queue_admission_check=(
                    lambda binding,
                    chat_id,
                    root_thread_id,
                    turn_id,
                    message_id,
                    has_exact_queue_continuity: (
                        self._thread_access_policy.prompt_queue_admission_check(
                            binding,
                            chat_id,
                            root_thread_id,
                            turn_id,
                            message_id=message_id,
                            has_exact_queue_continuity=(has_exact_queue_continuity),
                        )
                    )
                ),
                start_prompt=lambda *args, **kwargs: (
                    self._prompt_turn_entry.start_prompt_turn_result(*args, **kwargs)
                ),
                start_compact=self._feishu_compact_execution.start,
                load_message_context=(
                    lambda message_id: feishu_platform.bot.get_message_context(message_id) or {}
                ),
                remember_message_context=(
                    lambda message_id, context: remember_message_context(
                        feishu_platform.bot,
                        message_id,
                        context,
                    )
                ),
                prepare_queued_prompt_text=(
                    lambda **kwargs: prepare_queued_prompt_text(
                        feishu_platform.bot,
                        **kwargs,
                    )
                ),
                reply_text=feishu_platform.reply_text,
                reconcile_terminal=self._feishu_root_operations.reconcile_terminal,
            ),
            runtime_context_guard=self._runtime_loop.assert_worker_context,
        )
        self._terminal_execution = FeishuExecutionFinalizationController(
            binding_runtime=self._binding_runtime,
            turn_execution=self._turn_execution,
            execution_output=self._execution_output,
            runtime_context_guard=self._runtime_loop.assert_worker_context,
            ports=FeishuExecutionFinalizationPorts(
                lock=self._lock,
                release_main_turn=binding_coordinator.release_main_turn_for_binding,
                drain_execution_queue=self._feishu_execution_queue_service.drain,
            ),
        )
        feishu_surface = FeishuSurface(
            lock=self._lock,
            adapter=self._adapter,
            platform=feishu_platform,
            binding_runtime=self._binding_runtime,
            binding_runtime_coordinator=binding_coordinator,
            thread_access_policy=self._thread_access_policy,
            direct_thread_targets=self._codex_thread_targets,
            interaction_requests=self._interaction_requests,
            feishu_execution_queue_service=self._feishu_execution_queue_service,
            feishu_thread_sessions=self._feishu_thread_sessions,
            file_message_domain=self._file_message_domain,
            prompt_turn_entry=self._prompt_turn_entry,
            runtime_admin=self._runtime_admin,
            help_domain=self._help_domain,
            settings_domain=self._settings_domain,
            group_domain=self._group_domain,
            threads_ui_domain=self._threads_ui_domain,
            goal_domain=self._goal_domain,
            terminal_results=terminal_results,
        )
        self._feishu_surface = feishu_surface
        self._service_runtime_lifecycle = ServiceRuntimeLifecycle(
            activation=ServiceRuntimeActivationPorts(
                acquire_service_lease=lambda: self._service_instance_lease.acquire(),
                prepare_owned_state=lambda: service_authority.prepare_owned_state(feishu_platform.bot.app_id),
                start_runtime_loop=lambda: self._runtime_loop.start(),
                restore_runtime_state=self._restore_service_runtime_state,
                start_adapter=lambda: self._adapter.start(),
                start_destination_liveness_worker=self._destination_liveness.start,
                start_control_plane=lambda: self._service_control_plane.start(),
                publish_control_endpoint=lambda endpoint: (
                    self._service_instance_lease.publish_control_endpoint(endpoint)
                ),
                register_instance_runtime=service_authority.register_instance_runtime,
                restore_runtime_leases=lambda: self._runtime_call(
                    self._restore_service_thread_runtime_leases
                ),
                start_web_gateway=lambda: self._web_gateway.start(),
            ),
            shutdown=ServiceRuntimeShutdownPorts(
                cancel_frontend_timers=binding_coordinator.cancel_frontend_runtime_timers,
                web_is_running=lambda: bool(self._web_gateway.endpoint),
                prepare_web_shutdown=lambda: self._runtime_call(
                    self._web_runtime.prepare_shutdown
                ),
                stop_web_gateway=lambda: self._web_gateway.stop(),
                stop_control_plane=lambda: self._service_control_plane.stop(),
                stop_server_request_runtime=self._server_request_coordinator.shutdown,
                stop_execution_recovery_worker=lambda: self._execution_recovery.shutdown(
                    timeout=1.0
                ),
                stop_destination_liveness_worker=lambda: self._destination_liveness.shutdown(
                    timeout=1.0
                ),
                stop_card_dispatcher=lambda: self._execution_card_patch_dispatcher.shutdown(
                    timeout=1.0
                ),
                finish_web_shutdown=lambda: self._runtime_call(
                    self._web_runtime.finish_shutdown
                ),
                stop_runtime_loop=lambda: self._runtime_loop.stop(),
                stop_adapter=lambda: self._adapter.stop(),
                release_machine_authority=lambda: (
                    service_authority.release_service_authority_after_runtime_barrier(
                        context="service lifecycle"
                    )
                ),
            ),
        )
        self._ingress = ServiceRuntimeIngressDispatcher(
            self._service_runtime_lifecycle,
            self._runtime_call,
            self._runtime_loop.submit,
        )
        atexit.register(self.shutdown)

    @property
    def phase(self) -> ServiceRuntimePhase:
        return self._service_runtime_lifecycle.phase

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return self._runtime_loop.call(fn, *args, **kwargs)
        except RuntimeLoopClosedError:
            logger.debug("handler runtime loop already closed; dropping sync call %s", getattr(fn, "__name__", fn))
            raise

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        try:
            self._runtime_loop.submit(fn, *args, **kwargs)
        except RuntimeLoopClosedError:
            logger.debug(
                "handler runtime loop already closed; dropping async call %s",
                getattr(fn, "__name__", fn),
            )

    _runtime_call = call
    _runtime_submit = submit

    def _schedule_web_runtime_cleanup(
        self,
        thread_id: str,
        known_non_active: bool,
    ) -> None:
        """Admit one background cleanup into the existing service barrier."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        self._ingress.start_background_external_transaction(
            self._web_runtime.run_runtime_cleanup_transaction,
            normalized_thread_id,
            bool(known_non_active),
            thread_name=(
                "focus-web-runtime-cleanup-"
                f"{normalized_thread_id[:12]}"
            ),
        )

    def _schedule_web_notification_projection(
        self,
        receipt: WebNotificationProjectionReceipt,
    ) -> None:
        """Admit one coalesced Web projection into the shutdown barrier."""

        self._ingress.start_background_external_transaction(
            self._web_runtime.run_notification_projection_transaction,
            receipt,
            thread_name=(
                "focus-web-notification-projection-"
                f"{receipt.thread_id[:12]}"
            ),
        )

    def _schedule_web_attachment_cleanup(self, scope_key: str) -> None:
        """Admit rebuildable attachment cleanup into the shutdown barrier."""

        thread_label = str(scope_key or "").removeprefix("thread:")[:12]
        self._ingress.start_background_external_transaction(
            self._web_runtime.run_notification_attachment_cleanup,
            scope_key,
            thread_name=f"focus-web-attachment-cleanup-{thread_label}",
        )

    def status(self) -> dict[str, Any]:
        return self._operational_status_snapshot()

    def _operational_status_snapshot(self) -> dict[str, Any]:
        runtime = self._runtime_loop.snapshot()
        adapter_ingress = self._adapter_ingress_gate.snapshot()
        destination_liveness = self._destination_liveness.snapshot()
        warnings = self._operational_warnings.snapshot(limit=20)
        adapter_ingress_degraded = bool(
            adapter_ingress.backend_reset_blocked
            or adapter_ingress.cleanup_required
            or adapter_ingress.disconnect_cleanup_pending
        )
        active_task_over_threshold = bool(
            runtime.active_task_name
            and runtime.active_task_duration_seconds >= _RUNTIME_SLOW_TASK_SECONDS
        )
        return {
            "status": (
                "degraded"
                if (
                    warnings
                    or active_task_over_threshold
                    or adapter_ingress_degraded
                    or destination_liveness.degraded
                )
                else "ok"
            ),
            "observed_at": time.time(),
            "poll_after_seconds": _OPERATIONAL_STATUS_POLL_SECONDS,
            "warnings": warnings,
            "adapter_ingress": {
                "latest_generation": adapter_ingress.latest_generation,
                "last_disconnected_generation": adapter_ingress.last_disconnected_generation,
                "backend_reset_blocked": adapter_ingress.backend_reset_blocked,
                "cleanup_required": adapter_ingress.cleanup_required,
                "disconnect_cleanup_pending": (
                    adapter_ingress.disconnect_cleanup_pending
                ),
                "recovery_action": (
                    "retry_backend_reset_or_restart_service"
                    if adapter_ingress.cleanup_required
                    else (
                        "complete_backend_replacement_or_restart_service"
                        if adapter_ingress.backend_reset_blocked
                        else ""
                    )
                ),
            },
            "feishu_destination_liveness": {
                "worker_running": destination_liveness.worker_running,
                "pending_proofs": destination_liveness.pending_proofs,
                "last_error": destination_liveness.last_error,
            },
            "runtime_loop": {
                "accepted_tasks": runtime.accepted_tasks,
                "completed_tasks": runtime.completed_tasks,
                "failed_tasks": runtime.failed_tasks,
                "cancelled_tasks": runtime.cancelled_tasks,
                "queued_tasks": runtime.queued_tasks,
                "active_task_name": runtime.active_task_name,
                "active_task_duration_seconds": round(
                    runtime.active_task_duration_seconds,
                    3,
                ),
                "active_task_over_threshold": active_task_over_threshold,
                "last_queue_age_seconds": round(runtime.last_queue_age_seconds, 3),
                "last_task_duration_seconds": round(runtime.last_task_duration_seconds, 3),
                "max_queue_age_seconds": round(runtime.max_queue_age_seconds, 3),
                "max_task_duration_seconds": round(runtime.max_task_duration_seconds, 3),
            },
        }

    def start(self, bot) -> None:
        self._feishu_platform.attach(bot)
        set_terminal_result_text_resolver = getattr(bot, "set_terminal_result_text_resolver", None)
        if callable(set_terminal_result_text_resolver):
            set_terminal_result_text_resolver(
                self._terminal_results.resolve_terminal_result_text
            )
        try:
            self._service_runtime_lifecycle.start()
        except ServiceInstanceLeaseError:
            logger.exception("启动 FOCUS service 失败：当前 FOCUS_DATA_DIR 已被其他实例占用")
            raise
        except Exception:
            logger.exception("启动 Codex app-server 失败")
            raise

    def _restore_service_runtime_state(self) -> None:
        """Restore runtime-owned projections only after RuntimeLoop activation."""
        self._runtime_call(self._restore_service_runtime_state_on_runtime)

    def _restore_service_runtime_state_on_runtime(self) -> None:
        self._binding_runtime.hydrate_stored_bindings(replace=True)

    def _schedule_fcodex_participant_expiry(
        self,
        participant_id: str,
        expiry_generation: int,
        delay_seconds: float,
    ) -> None:
        def _expire() -> None:
            self._runtime_submit(
                self._operation_owner.expire_participant,
                participant_id,
                expiry_generation,
            )

        timer = threading.Timer(max(float(delay_seconds), 0.0), _expire)
        timer.daemon = True
        timer.start()

    def _schedule_fcodex_connection_expiry(
        self,
        participant_id: str,
        connection_id: str,
        expiry_generation: int,
        delay_seconds: float,
    ) -> None:
        """Enqueue fcodex heartbeat expiry; never mutate owner state on a timer."""

        def _expire() -> None:
            self._runtime_submit(
                self._operation_owner.expire_connection,
                participant_id,
                connection_id,
                expiry_generation,
            )

        timer = threading.Timer(max(float(delay_seconds), 0.0), _expire)
        timer.daemon = True
        timer.start()

    def _schedule_fcodex_proxy_delivery_expiry(
        self,
        request_key: str,
        expiry_generation: int,
        delay_seconds: float,
    ) -> None:
        """Bound the service-copy wait for an otherwise live fcodex writer."""

        def _expire() -> None:
            self._runtime_submit(
                self._operation_owner.expire_proxy_delivery,
                request_key,
                expiry_generation,
            )

        timer = threading.Timer(max(float(delay_seconds), 0.0), _expire)
        timer.daemon = True
        timer.start()

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
                self._service_runtime_authority.ensure_service_thread_runtime_lease(
                    thread_id
                )
            except Exception:
                logger.exception("恢复 service thread runtime lease 失败: thread=%s", thread_id[:12])
                try:
                    self._runtime_admin.detach_thread(thread_id)
                except Exception:
                    logger.exception("将冲突线程 fail-closed 为 detached 失败: thread=%s", thread_id[:12])

    def handle_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._ingress.call(
            self._feishu_surface.handle_message_impl,
            sender_id,
            chat_id,
            text,
            message_id=message_id,
        )

    def handle_message_recalled(self, chat_id: str, message_id: str) -> None:
        self._ingress.submit(
            self._feishu_surface.handle_message_recalled_impl,
            chat_id,
            message_id,
        )

    def handle_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        dispatch = (
            self._ingress.run_external_transaction
            if self._feishu_surface.should_bypass_runtime_for_card_action(
                action_value
            )
            else self._ingress.call
        )
        return dispatch(
            self._feishu_surface.handle_card_action_impl,
            sender_id,
            chat_id,
            message_id,
            action_value,
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
        self._ingress.call(
            self._feishu_surface.handle_attachment_message_impl,
            sender_id,
            chat_id,
            message_id,
            attachment_type,
            resource_key,
            file_name,
        )

    def is_sender_active(self, sender_id: str, chat_id: str = "", message_id: str = "") -> bool:
        return bool(
            self._ingress.call(
                self._binding_runtime_coordinator.is_sender_active_on_runtime,
                sender_id,
                chat_id,
                message_id,
            )
        )

    def deactivate_sender(self, sender_id: str, chat_id: str = "", message_id: str = "") -> None:
        self._ingress.call(
            self._binding_runtime_coordinator.deactivate_sender_impl,
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def preflight_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._ingress.call(
            self._feishu_surface.preflight_group_prompt_impl,
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def should_route_group_followup_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._ingress.call(
            self._feishu_surface.should_route_group_followup_prompt_impl,
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def accept_destination_loss_proof(
        self,
        proof: FeishuDestinationLossProof,
    ) -> None:
        self._ingress.run_external_transaction(self._destination_liveness.accept, proof)

    def shutdown(self) -> None:
        self.stop()

    def stop(self) -> None:
        self._service_runtime_lifecycle.stop()

    def _mark_compact_start_outcome_unknown_for_session(
        self,
        captured: BindingSessionSnapshot,
        thread_id: str,
    ) -> None:
        self._mark_compact_start_outcome_unknown(
            captured.binding[0],
            captured.binding[1],
            thread_id,
            captured_session=captured,
        )

    def _mark_compact_start_outcome_unknown(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        reason: str = "feishu_compact_start_notification_timeout",
        admission: FeishuRootOperationToken | None = None,
        captured_session: BindingSessionSnapshot | None = None,
    ) -> None:
        """Retain one process-local unknown compact submission for reconciliation."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("compact 启动结果未知时缺少 thread id。")
        captured = captured_session or self._binding_runtime.resolve_session(sender_id, chat_id)
        prepared = self._execution_recovery_runtime.prepare_compact_start_unknown(
            PrepareCompactStartUnknownCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id=normalized_thread_id,
            )
        )
        if prepared is None:
            return

        if admission is not None:
            self._feishu_root_operations.mark_outcome_unknown(
                admission,
                reason=reason,
            )
        else:
            self._feishu_root_operations.mark_awaiting_start_outcome_unknown(
                prepared.binding,
                normalized_thread_id,
                reason=reason,
            )

        updated = self._execution_recovery_runtime.commit_compact_start_unknown(
            CommitCompactStartUnknownCommand(
                target=BindingExecutionTarget.from_session(prepared),
                thread_id=normalized_thread_id,
                error_text=COMPACT_START_OUTCOME_UNKNOWN_TEXT,
            )
        )
        if updated is None:
            logger.warning(
                "compact 未知结果已记录，但本地执行 anchor 已变更: chat=%s thread=%s",
                prepared.binding[1],
                normalized_thread_id[:12],
            )
            return
        self._execution_output.flush_execution_card_for_session(
            updated,
            immediate=True,
        )


    def _operation_start_outcome_unknown(self, exc: Exception) -> bool:
        # Some higher-level resume helpers add a user-facing RuntimeError
        # while preserving the transport/protocol failure as ``__cause__``.
        # Do not let that presentation layer turn an already-sent continuation
        # into a falsely known failure.
        for current in iter_exception_chain(exc):
            if isinstance(current, ThreadResumeOutcomeUnknown):
                return True
            if isinstance(
                current,
                (ThreadResumeLocalCommitFailed, ThreadResumeSettlementError),
            ):
                return current.recovery_required
            if isinstance(current, (TimeoutError, CodexRpcProtocolError)):
                return True
            if isinstance(current, Exception) and (
                CodexThreadTargetService.is_transport_disconnect(current)
                or CodexThreadTargetService.is_request_timeout_error(current)
            ):
                return True
        return False

    def _schedule_terminal_execution_reconcile(self, target: TerminalReconcileTarget | None) -> None:
        self._execution_recovery.schedule_terminal_execution_reconcile(target)

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

    def _finalize_execution_for_recovery(
        self,
        session: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationResult | None:
        try:
            return self._terminal_execution.finalize(session)
        except FeishuExecutionRuntimeChanged:
            return None

    def _finalize_execution_from_terminal_signal(
        self,
        session: BindingSessionSnapshot,
        *,
        thread_id: str,
        turn_id: str = "",
    ) -> bool:
        target = self._execution_recovery.capture_terminal_reconcile_target_for_session(
            session,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        if target is not None:
            self._adapter_notification_runtime.remember_terminal_result_text(
                RememberTerminalResultTextCommand(
                    target=BindingExecutionTarget.from_session(session),
                    execution_message_id=target.card_message_id,
                    text=target.transcript.reply_text(),
                )
            )
        finalization = self._terminal_execution.finalize(session)
        finalized = bool(finalization.had_card and finalization.retired)
        if target is not None and finalization.terminal_page_receipts:
            target = replace(
                target,
                terminal_page_receipts=finalization.terminal_page_receipts,
            )
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
        return self._execution_recovery.reconcile_execution_snapshot(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    def _archive_thread_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
    ) -> dict[str, Any]:
        return self._runtime_admin.archive_thread_for_control(thread_id, summary=summary)

    def _handle_service_control_request(self, method: str, params: dict[str, Any]) -> Any:
        normalized_method = str(method or "").strip()
        if normalized_method == "thread/loaded/list":
            if self._service_runtime_lifecycle.offline_maintenance_prepared:
                raise RuntimeError(
                    "当前实例已关闭新 ingress，正在等待离线 maintenance 停服；"
                    "除 status/cancel 外不再接受控制面操作。"
                )
            # A global Web directory request already owns RuntimeLoop while it
            # fans out to peer instances. Re-entering this instance's loop from
            # its control request would form a two-instance wait cycle. The
            # adapter/RPC connection is the thread-safe owner of this read; use
            # only its existing websocket and let control-plane shutdown drain
            # this request before adapter shutdown.
            return loaded_thread_inventory_control_response(
                params,
                instance_name=self._instance_name,
                list_loaded_thread_ids=lambda: self._adapter.list_loaded_thread_ids_for_control(
                    timeout=MANAGED_LOADED_INVENTORY_RPC_TIMEOUT_SECONDS,
                ),
            )
        return self._runtime_call(self._handle_service_control_request_impl, method, params)

    def _handle_service_control_request_impl(self, method: str, params: dict[str, Any]) -> Any:
        normalized_method = str(method or "").strip()
        if normalized_method == "service/cancel-offline-maintenance":
            self._service_runtime_lifecycle.cancel_offline_maintenance()
            return {
                "instance_name": self._instance_name,
                "status": "cancelled",
            }
        if self._service_runtime_lifecycle.offline_maintenance_prepared:
            if normalized_method == "service/status":
                return self._runtime_admin.handle_service_control_request(
                    normalized_method,
                    params,
                )
            raise RuntimeError(
                "当前实例已关闭新 ingress，正在等待离线 maintenance 停服；"
                "除 status/cancel 外不再接受控制面操作。"
            )
        if normalized_method == "service/prepare-offline-maintenance":
            return self._service_runtime_lifecycle.prepare_offline_maintenance(
                self._verify_offline_maintenance_idle
            )
        if str(method or "").strip().startswith("operation/"):
            return self._fcodex_control_dispatcher.handle(method, params)
        return self._runtime_admin.handle_service_control_request(method, params)

    def _verify_offline_maintenance_idle(self) -> dict[str, Any]:
        preview = self._runtime_admin.backend_reset_preview()
        if preview.status != BACKEND_RESET_STATUS_AVAILABLE:
            reason = str(preview.reason_text or "").strip() or str(
                preview.reason_code or "runtime is not idle"
            )
            raise RuntimeError(f"实例 {self._instance_name} 当前不能离线 maintenance：{reason}")
        return {
            "instance_name": self._instance_name,
            "status": "prepared",
            "loaded_thread_count": len(preview.loaded_thread_ids),
            "active_loaded_thread_count": len(preview.active_loaded_thread_ids),
            "running_binding_count": len(preview.running_binding_ids),
            "pending_request_count": preview.pending_request_count,
            "runtime_verification_failed": preview.runtime_verification_failed,
        }

    def _reset_current_instance_backend(self, force: bool) -> dict[str, Any]:
        return self._backend_reset_service.reset_current_instance(force=force)
