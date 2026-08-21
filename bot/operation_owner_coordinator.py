"""Composition facade for fcodex request, runtime-source, and interaction owners."""

from __future__ import annotations

from typing import Any, Callable

from bot.adapters.base import ThreadSummary
from bot.fcodex.interaction_inbox import (
    FcodexInteractionInbox,
    FcodexInteractionInboxPorts,
)
from bot.fcodex.operation_contract import FcodexBackendEpochSettlementReceipt
from bot.fcodex.operation_service import FcodexOperationService
from bot.fcodex.participant_runtime_registry import FcodexParticipantRuntimeRegistry
from bot.fcodex.thread_create_owner import (
    FcodexExternalThreadCreateAuthority,
    FcodexThreadCreateOwner,
)
from bot.runtime_loop import RuntimeContextGuard
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestRoutingMode,
)
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry


class OperationOwnerCoordinator:
    """Order fcodex aggregates without reconstructing their mutable state."""

    def __init__(
        self,
        *,
        interaction_lease_store: InteractionLeaseStore,
        participant_runtime_registry: FcodexParticipantRuntimeRegistry,
        external_thread_create_authority: FcodexExternalThreadCreateAuthority,
        effective_settings: ThreadEffectiveSettingsRegistry,
        server_request_is_resolved: Callable[[str], bool],
        server_request_response_authority_is_revoked: Callable[[str], bool],
        runtime_context_guard: RuntimeContextGuard,
        respond: Callable[..., None],
        schedule_proxy_delivery_expiry: Callable[[str, int, float], None],
        owner_changed: Callable[[str, str], None],
        proxy_delivery_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("OperationOwnerCoordinator 需要 RuntimeLoop context guard。")
        if not isinstance(participant_runtime_registry, FcodexParticipantRuntimeRegistry):
            raise TypeError("OperationOwnerCoordinator 需要 participant Registry owner。")
        self._runtime_context_guard = runtime_context_guard
        self._participant_runtime_registry = participant_runtime_registry
        thread_create_owner = FcodexThreadCreateOwner(
            authority=external_thread_create_authority,
            participant_runtime_registry=participant_runtime_registry,
        )
        self._operation_service = FcodexOperationService(
            interaction_lease_store=interaction_lease_store,
            participant_runtime_registry=participant_runtime_registry,
            thread_create_owner=thread_create_owner,
            effective_settings=effective_settings,
            runtime_context_guard=runtime_context_guard,
            owner_changed=owner_changed,
        )
        self._interaction_inbox = FcodexInteractionInbox(
            ports=FcodexInteractionInboxPorts(
                authority=self._operation_service,
                server_request_is_resolved=server_request_is_resolved,
                server_request_response_authority_is_revoked=(
                    server_request_response_authority_is_revoked
                ),
                respond=respond,
                schedule_proxy_delivery_expiry=schedule_proxy_delivery_expiry,
            ),
            runtime_context_guard=runtime_context_guard,
            proxy_delivery_timeout_seconds=proxy_delivery_timeout_seconds,
        )

    # Participant lifecycle -------------------------------------------------

    def participant_connected(
        self,
        participant_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        receipt = self._participant_runtime_registry.connect(
            participant_id,
            connection_id,
        )
        return {
            "connected": True,
            "state": receipt.state,
        }

    def participant_heartbeat(
        self,
        participant_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        receipt = self._participant_runtime_registry.heartbeat(
            participant_id,
            connection_id,
        )
        return {"ok": True, "state": receipt.state, "mode": "connected"}

    def has_connected_participant_connection(
        self,
        participant_id: str,
        connection_id: str,
    ) -> bool:
        self._runtime_context_guard()
        return self._participant_runtime_registry.has_live_endpoint(
            participant_id,
            connection_id,
        )

    def participant_disconnected(
        self,
        participant_id: str,
        connection_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        receipt = self._participant_runtime_registry.disconnect(
            participant_id,
            connection_id,
        )
        if not receipt.participant_known:
            return {"state": "unknown", "retired_requests": 0}
        retired = self._interaction_inbox.drop_delivered(
            receipt.participant_id,
            connection_id=receipt.connection_id,
        )
        unknown_requests = self._operation_service.connection_lost(
            receipt.participant_id,
            receipt.connection_id,
        )
        if receipt.state == "orphaned":
            self._participant_runtime_registry.release_unneeded_sources(
                receipt.participant_id
            )
        return {
            "state": receipt.state,
            "retired_requests": retired,
            "unknown_client_requests": unknown_requests,
        }

    def expire_connection(
        self,
        participant_id: str,
        connection_id: str,
        expiry_generation: int,
    ) -> None:
        self._runtime_context_guard()
        if self._participant_runtime_registry.connection_expiry_is_current(
            participant_id,
            connection_id,
            expiry_generation,
        ):
            self.participant_disconnected(participant_id, connection_id)

    def expire_participant(self, participant_id: str, expiry_generation: int) -> None:
        self._runtime_context_guard()
        if not self._participant_runtime_registry.expire_participant(
            participant_id,
            expiry_generation,
        ):
            return
        self._participant_runtime_registry.release_unneeded_sources(
            str(participant_id or "").strip()
        )

    def expire_proxy_delivery(self, request_key: str, expiry_generation: int) -> None:
        self._runtime_context_guard()
        self._interaction_inbox.expire_proxy_delivery(
            request_key,
            expiry_generation,
        )

    # Request commands ------------------------------------------------------

    def admit(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        decision = self._operation_service.admit(**kwargs)
        decision.setdefault("tracks_response", False)
        decision.setdefault("request_token", None)
        return decision

    def client_response(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._operation_service.client_response(**kwargs)

    def remember_authoritative_direct_target(
        self,
        summary: ThreadSummary,
        *,
        expected_thread_id: str,
        operation: str,
    ) -> str:
        self._runtime_context_guard()
        return self._operation_service.remember_authoritative_direct_target(
            summary,
            expected_thread_id=expected_thread_id,
            operation=operation,
        )

    # Interaction commands -------------------------------------------------

    def server_request(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._interaction_inbox.proxy_request(**kwargs)

    def service_server_request(
        self,
        identity: ServerRequestIdentity,
        *,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._interaction_inbox.service_request(
            identity,
            routing_mode=routing_mode,
        )

    def has_pending_interaction_for_root(self, root_thread_id: str) -> bool:
        self._runtime_context_guard()
        return self._interaction_inbox.has_pending_for_root(root_thread_id)

    def pending_interaction_count(self) -> int:
        self._runtime_context_guard()
        return self._interaction_inbox.pending_count()

    def remove_resolved_server_request(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        self._runtime_context_guard()
        return self._interaction_inbox.remove_resolved(identity)

    def response_admit(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._interaction_inbox.response_admit(**kwargs)

    def response_submit(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._interaction_inbox.response_submit(**kwargs)

    def response_invalid(self, **kwargs) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._interaction_inbox.response_invalid(**kwargs)

    def response_sent(self, **kwargs) -> None:
        self._runtime_context_guard()
        self._interaction_inbox.response_sent(**kwargs)

    def response_unknown(self, **kwargs) -> None:
        self._runtime_context_guard()
        self._interaction_inbox.response_unknown(**kwargs)

    # Backend lifecycle -----------------------------------------------------

    def notification(self, method: str, params: dict[str, Any]) -> None:
        self._runtime_context_guard()
        self._operation_service.notification(method, params)

    def retry_authoritative_cleanups(self) -> None:
        self._runtime_context_guard()
        self._participant_runtime_registry.retry_authoritative_cleanups()

    def backend_disconnected(self) -> None:
        self._runtime_context_guard()
        self._interaction_inbox.backend_disconnected()
        self._operation_service.backend_disconnected()

    def settle_backend_epoch_after_stop(self) -> FcodexBackendEpochSettlementReceipt:
        """Retire current-instance fcodex facts after the backend stopped."""

        self._runtime_context_guard()
        interaction_keys = self._interaction_inbox.settle_backend_epoch_after_stop()
        requests = self._operation_service.settle_backend_epoch_after_stop()
        return FcodexBackendEpochSettlementReceipt(
            requests=requests,
            interaction_request_keys=interaction_keys,
        )

    def close_backend_epoch_after_machine_replace(self):
        self._runtime_context_guard()
        return self._participant_runtime_registry.close_backend_epoch_after_machine_replace()
