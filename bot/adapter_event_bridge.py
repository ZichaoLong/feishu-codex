"""Composition boundary for app-server callbacks entering ``RuntimeLoop``.

Canonical request identity remains owned by ``ServerRequestCoordinator``. This bridge owns only
cross-owner ordering: connection admission, notification fan-out, surface
projection, direct-root classification and disconnect cleanup.  It deliberately
keeps no request, generation, owner or binding mirror.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapter_notification_pipeline import AdapterNotificationPipeline
from bot.adapters.codex_thread_summary import thread_summary_from_app_server_thread
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.direct_thread_target_policy import DirectThreadTargetRegistry
from bot.feishu_root_operation_controller import FeishuRootOperationController
from bot.feishu_runtime_disconnect_projection import (
    FeishuRuntimeDisconnectProjection,
)
from bot.interaction_auto_resolution import InteractionAutoResolutionController
from bot.interaction_contract import (
    INTERACTIVE_SERVER_REQUEST_METHODS,
    SHARED_APPROVAL_METHODS,
)
from bot.interaction_request_controller import InteractionRequestController
from bot.operation_owner_coordinator import OperationOwnerCoordinator
from bot.runtime_admin.controller import RuntimeAdminController
from bot.server_request_contract import ServerRequestIdentity, ServerRequestRoutingMode
from bot.server_request_coordinator import ServerRequestCoordinator
from bot.server_request_dispatch import (
    ServerRequestSurfaceClaim,
    ServerRequestSurfaceIdentityConflict,
)
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_runtime_authority import ThreadRuntimeAuthority
from bot.web_runtime.controller import WebRuntimeController


logger = logging.getLogger(__name__)

ChatBindingKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class AdapterEventBridgePorts:
    """Required Handler/surface effects not owned by callback aggregates."""

    runtime_submit: Callable[..., None]
    finalize_execution_card: Callable[[str, str], bool]
    thread_subscribers: Callable[[str], tuple[ChatBindingKey, ...]]
    resident_session: Callable[[ChatBindingKey], BindingSessionSnapshot | None]


class AdapterEventBridge:
    """Route adapter events without copying any owner's mutable facts."""

    def __init__(
        self,
        *,
        ingress_gate: AdapterIngressGate,
        notification_pipeline: AdapterNotificationPipeline,
        server_requests: ServerRequestCoordinator,
        interaction_requests: InteractionRequestController,
        interaction_auto_resolution: InteractionAutoResolutionController,
        direct_thread_targets: DirectThreadTargetRegistry,
        operation_owner: OperationOwnerCoordinator,
        web_runtime: WebRuntimeController,
        feishu_root_operations: FeishuRootOperationController,
        thread_runtime_authority: ThreadRuntimeAuthority,
        interaction_leases: InteractionLeaseStore,
        runtime_admin: RuntimeAdminController,
        feishu_runtime_disconnect: FeishuRuntimeDisconnectProjection,
        ports: AdapterEventBridgePorts,
    ) -> None:
        self._ingress_gate = ingress_gate
        self._notification_pipeline = notification_pipeline
        self._server_requests = server_requests
        self._interaction_requests = interaction_requests
        self._interaction_auto_resolution = interaction_auto_resolution
        self._direct_thread_targets = direct_thread_targets
        self._operation_owner = operation_owner
        self._web_runtime = web_runtime
        self._feishu_root_operations = feishu_root_operations
        self._thread_runtime_authority = thread_runtime_authority
        self._interaction_leases = interaction_leases
        self._runtime_admin = runtime_admin
        self._feishu_runtime_disconnect = feishu_runtime_disconnect
        self._ports = ports

    # Adapter thread -> RuntimeLoop ingress.

    def handle_notification(
        self,
        connection_generation: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        self._ports.runtime_submit(
            self.handle_notification_for_connection,
            connection_generation,
            method,
            params,
        )

    def handle_notification_for_connection(
        self,
        connection_generation: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if not self.accept_connection_ingress(connection_generation):
            return
        self.dispatch_notification(method, params)

    def dispatch_notification(self, method: str, params: dict[str, Any]) -> None:
        self._notification_pipeline.dispatch(method, params)

    def reconcile_active_turn_lease_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        """Apply exact upstream main-turn lifecycle to the shared writer SSOT."""

        if method == "thread/started":
            raw_thread = params.get("thread")
            if isinstance(raw_thread, dict):
                try:
                    self._direct_thread_targets.remember(
                        thread_summary_from_app_server_thread(raw_thread)
                    )
                except Exception:
                    logger.warning(
                        "Ignoring malformed thread/started direct-target evidence",
                        exc_info=True,
                    )
        thread_id = str(params.get("threadId", "") or "").strip()
        if method in {"thread/closed", "thread/archived", "thread/deleted"}:
            self._direct_thread_targets.forget(thread_id)
        if not thread_id:
            return
        if method == "thread/closed":
            self._interaction_leases.clear_thread(thread_id)
            return
        if method not in {"turn/started", "turn/completed"}:
            return
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        turn_id = str(turn.get("id", "") or "").strip()
        if not turn_id:
            return
        if method == "turn/completed":
            self._interaction_leases.release_turn(thread_id, turn_id)
            return
        lease = self._interaction_leases.load(thread_id)
        if lease is not None and not lease.turn_id:
            self._interaction_leases.activate_turn(lease, turn_id)

    def handle_request(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        self._ports.runtime_submit(
            self.handle_request_for_connection,
            connection_generation,
            request_id,
            method,
            params,
        )

    def handle_request_for_connection(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if not self.accept_connection_ingress(connection_generation):
            return
        self.route_request(
            connection_generation,
            request_id,
            method,
            params,
        )

    def route_request(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        self._server_requests.route_request(
            connection_generation,
            request_id,
            method,
            params,
        )

    def handle_disconnect(self, connection_generation: int) -> None:
        self._ports.runtime_submit(
            self.handle_disconnect_for_connection,
            connection_generation,
        )

    def accept_connection_ingress(self, connection_generation: int) -> bool:
        return self._ingress_gate.accept(connection_generation)

    def handle_disconnect_for_connection(self, connection_generation: int) -> None:
        self._ingress_gate.observe_disconnect(connection_generation)

    # Canonical notification settlement and local projections.

    def handle_server_request_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        self._server_requests.handle_notification(method, params)

    # Surface selection helpers used by the single dispatcher composition.

    def share_server_request_approval(
        self,
        identity: ServerRequestIdentity,
    ) -> bool:
        """Admit one canonical current-epoch approval for a direct-root turn."""

        thread_id = identity.thread_id
        turn_id = identity.turn_id
        if (
            identity.method not in SHARED_APPROVAL_METHODS
            or not thread_id
            or not turn_id
        ):
            return False
        if not self._direct_thread_targets.is_known(thread_id):
            return False
        # ServerRequestCoordinator calls this bridge only with the exact
        # registry-owned identity from the current backend generation.  The
        # callback is therefore approval authority even when an autonomous
        # goal turn has no Focus writer lease; InteractionLeaseStore remains
        # lifecycle writer authority and must not narrow this audience.
        return True

    def share_server_request_desktop_interaction(
        self,
        identity: ServerRequestIdentity,
    ) -> bool:
        """Admit one ordinary direct-root callback to Web/fcodex fanout."""

        return bool(
            identity.method not in SHARED_APPROVAL_METHODS
            and identity.method in INTERACTIVE_SERVER_REQUEST_METHODS
            and identity.thread_id
            and identity.turn_id
            and self._direct_thread_targets.is_known(identity.thread_id)
        )

    def route_fcodex_server_request(
        self,
        identity: ServerRequestIdentity,
        *,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> ServerRequestSurfaceClaim:
        route = self._operation_owner.service_server_request(
            identity,
            routing_mode=routing_mode,
        )
        if route.get("reason") == "server_request_identity_conflict":
            raise ServerRequestSurfaceIdentityConflict(
                "fcodex retained a different canonical server request"
            )
        return ServerRequestSurfaceClaim.from_retained(route.get("handled"))

    def auto_resolve_interaction_request(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> None:
        if self._web_runtime.auto_resolve_request(
            request_key,
            backend_epoch,
            generation,
        ):
            return
        self._interaction_requests.auto_resolve_request(
            request_key,
            backend_epoch,
            generation,
        )

    def has_pending_interaction_outside_fcodex_for_root(
        self,
        root_thread_id: str,
    ) -> bool:
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return True
        return (
            self._interaction_requests.has_pending_request_for_root(root_id)
            or self._server_requests.has_pending_request_for_root(root_id)
        )

    def has_shared_pending_interaction_for_root(
        self,
        root_thread_id: str,
    ) -> bool:
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return True
        return self._operation_owner.has_pending_interaction_for_root(
            root_id
        ) or self.has_pending_interaction_outside_fcodex_for_root(root_id)

    def reconcile_resolved_interaction_root(self, root_thread_id: str) -> None:
        self._web_runtime.reconcile_external_pending_interaction_resolved(
            root_thread_id
        )
        self._operation_owner.retry_authoritative_cleanups()
        self._feishu_root_operations.reconcile_notification(
            "serverRequest/resolved",
            {"threadId": root_thread_id},
        )

    # One disconnect ordering boundary; frontend projection is not authority.

    def handle_disconnect_impl(self) -> None:
        self._direct_thread_targets.clear()
        self._server_requests.backend_disconnected()
        self._thread_runtime_authority.invalidate_connection()
        self._operation_owner.backend_disconnected()
        self._web_runtime.backend_disconnected()
        affected_bindings = (
            self._feishu_runtime_disconnect.prepare().affected_bindings
        )
        pending_fail_closed = (
            self._interaction_requests.fail_close_all_requests_without_response(
                note=(
                    "当前实例与 Codex backend 的 websocket 已断开，"
                    "已自动结束该请求。"
                ),
            )
        )
        if not affected_bindings:
            if pending_fail_closed:
                logger.warning(
                    "Codex websocket disconnected; detached bindings=%s "
                    "threads=%s pending=%s",
                    [],
                    [],
                    pending_fail_closed,
                )
            return
        result = self._runtime_admin.fail_close_service_attached_runtime()
        for sender_id, chat_id in affected_bindings:
            self._ports.finalize_execution_card(sender_id, chat_id)
        logger.warning(
            "Codex websocket disconnected; detached bindings=%s threads=%s "
            "pending=%s",
            result["detached_binding_ids"],
            result["detached_thread_ids"],
            pending_fail_closed,
        )

    def handle_feishu_root_operation_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if method in {"turn/started", "item/started"}:
            thread_id = str(params.get("threadId", "") or "").strip()
            turn_id = ""
            if method == "turn/started":
                turn = (
                    params.get("turn")
                    if isinstance(params.get("turn"), dict)
                    else {}
                )
                turn_id = str(turn.get("id", "") or "").strip()
            else:
                item = (
                    params.get("item")
                    if isinstance(params.get("item"), dict)
                    else {}
                )
                if str(item.get("type", "") or "").strip() == "contextCompaction":
                    turn_id = str(params.get("turnId", "") or "").strip()
            if thread_id and turn_id:
                for binding in self._ports.thread_subscribers(thread_id):
                    runtime = self._ports.resident_session(binding)
                    if (
                        runtime is None
                        or runtime.current_thread_id.strip() != thread_id
                        or runtime.execution.current_turn_id.strip() != turn_id
                        or runtime.execution.current_execution_kind.strip()
                        != "compact"
                        or runtime.execution.awaiting_local_turn_started
                    ):
                        continue
                    if self._feishu_root_operations.acknowledge_async_start(
                        binding,
                        thread_id,
                        turn_id,
                    ):
                        break
        settled_pending_submission = (
            self._feishu_root_operations.reconcile_notification(method, params)
        )
        if method != "thread/status/changed" or not settled_pending_submission:
            return
        thread_id = str(params.get("threadId", "") or "").strip()
        for binding in self._ports.thread_subscribers(thread_id):
            runtime = self._ports.resident_session(binding)
            if (
                runtime is None
                or runtime.current_thread_id.strip() != thread_id
                or runtime.execution.current_turn_id.strip()
                or not runtime.execution.awaiting_local_turn_started
            ):
                continue
            self._ports.finalize_execution_card(binding[0], binding[1])
