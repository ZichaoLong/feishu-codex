"""Fail-closed lifecycle transaction for replacing the owned app-server."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from bot.adapter_ingress_gate import (
    AdapterBackendResetGenerationMismatchError,
    AdapterBackendResetUnavailableError,
    AdapterIngressGate,
)
from bot.backend_reset.contract import (
    BackendResetGenerationStaleError,
    BackendResetLocalProjectionReceipt,
    BackendResetUnavailableError,
    require_backend_reset_connection_generation,
)
from bot.codex_protocol.client import (
    CodexRpcConnectionGenerationMismatchError,
    CodexRpcPreSendError,
)
from bot.stores.interaction_lease_store import (
    InteractionLeaseBackendStopCapture,
    InteractionLeaseBackendStopRetirementReceipt,
)


logger = logging.getLogger(__name__)


class BackendResetAdapter(Protocol):
    def require_owned_backend_lifecycle(self) -> None: ...

    def stop(self) -> None: ...

    def rotate_server_request_authority_after_backend_stop(self) -> object: ...

    def start(self) -> None: ...

    def current_app_server_url(self) -> str: ...

    def connection_generation(
        self,
        *,
        timeout: float,
        require_existing_connection: bool,
    ) -> int: ...

    def fence_backend_reset_generation(
        self,
        *,
        expected_connection_generation: int,
        fence_ingress: Callable[[], None],
        timeout: float,
    ) -> None: ...


class BackendResetOperationOwner(Protocol):
    def settle_backend_epoch_after_stop(self) -> object: ...

    def close_backend_epoch_after_machine_replace(self) -> object: ...


class BackendResetInteractionLeaseStore(Protocol):
    def capture_current_process_for_backend_stop(
        self,
    ) -> InteractionLeaseBackendStopCapture: ...

    def retire_after_backend_stop(
        self,
        capture: InteractionLeaseBackendStopCapture,
    ) -> InteractionLeaseBackendStopRetirementReceipt: ...


class BackendResetRuntimeLeaseStore(Protocol):
    def purge_all_for_instance(self, *, instance_name: str) -> list[str]: ...


class BackendResetRuntimeAuthority(Protocol):
    def confirm_backend_reset(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackendResetEpochRetirementReceipt:
    """All authoritative local retirement completed after machine stop."""

    interaction_leases: InteractionLeaseBackendStopRetirementReceipt
    server_request_registry: object
    fcodex: object
    web: object
    local_projection: BackendResetLocalProjectionReceipt
    feishu_root_operations: object
    feishu_requests: object
    transport: object


@dataclass(frozen=True, slots=True)
class BackendResetReceipt:
    """Committed projection of one admitted replacement backend."""

    connection_generation: int
    retirement: BackendResetEpochRetirementReceipt
    machine_cleared_thread_ids: tuple[str, ...]


class BackendResetCoordinator:
    """Own the only legal ordering for an explicit local backend reset.

    This coordinator owns no duplicate backend or operation state.  It orders
    the existing authorities so every partial failure leaves adapter ingress
    closed. The old backend stop is the authority for clearing this instance's
    process-bound runtime holders; no root-operation record survives it.
    """

    def __init__(
        self,
        *,
        ingress_gate: AdapterIngressGate,
        adapter: BackendResetAdapter,
        operation_owner: BackendResetOperationOwner,
        interaction_lease_store: BackendResetInteractionLeaseStore,
        runtime_lease_store: BackendResetRuntimeLeaseStore,
        instance_name: str,
        runtime_authority: BackendResetRuntimeAuthority,
        retire_server_requests_after_stop: Callable[[], object],
        retire_web_after_stop: Callable[[], object],
        retire_feishu_after_stop: Callable[[], object],
        retire_feishu_root_operations_after_stop: Callable[[], object],
        dispatch_feishu_card_projection_best_effort: Callable[[], None],
        connect_timeout_seconds: float,
        publish_replacement: Callable[[str], None],
        runtime_context_guard: Callable[[], None],
    ) -> None:
        capabilities = (
            retire_server_requests_after_stop,
            retire_web_after_stop,
            retire_feishu_after_stop,
            retire_feishu_root_operations_after_stop,
            dispatch_feishu_card_projection_best_effort,
            publish_replacement,
            runtime_context_guard,
        )
        if any(not callable(capability) for capability in capabilities):
            raise TypeError(
                "BackendResetCoordinator 需要 publication port 与 RuntimeLoop guard。"
            )
        # Encode the lifecycle proof in the composition boundary.  An
        # attached client can close only its own websocket; accepting one here
        # would let reset clear machine authority while the old backend keeps
        # running and serving stale proxy connections.
        adapter.require_owned_backend_lifecycle()
        self._ingress_gate = ingress_gate
        self._adapter = adapter
        self._operation_owner = operation_owner
        self._interaction_lease_store = interaction_lease_store
        self._runtime_lease_store = runtime_lease_store
        self._instance_name = str(instance_name or "").strip().lower()
        if not self._instance_name:
            raise ValueError("BackendResetCoordinator requires an instance name")
        self._runtime_authority = runtime_authority
        self._retire_server_requests_after_stop = retire_server_requests_after_stop
        self._retire_web_after_stop = retire_web_after_stop
        self._retire_feishu_after_stop = retire_feishu_after_stop
        self._retire_feishu_root_operations_after_stop = (
            retire_feishu_root_operations_after_stop
        )
        self._dispatch_feishu_card_projection_best_effort = (
            dispatch_feishu_card_projection_best_effort
        )
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._publish_replacement = publish_replacement
        self._runtime_context_guard = runtime_context_guard

    def preview_connection_generation(self) -> int:
        """Return a non-reserving Web reset generation or report unavailable."""

        self._runtime_context_guard()
        try:
            physical_generation = require_backend_reset_connection_generation(
                self._adapter.connection_generation(
                    timeout=self._connect_timeout_seconds,
                    require_existing_connection=False,
                )
            )
        except Exception as exc:
            raise BackendResetUnavailableError(
                "the physical backend connection generation is unavailable"
            ) from exc
        gate = self._ingress_gate.snapshot()
        if (
            gate.latest_generation != physical_generation
            or gate.backend_reset_blocked
            or gate.cleanup_required
            or gate.disconnect_cleanup_pending
        ):
            raise BackendResetUnavailableError(
                "the backend connection and ingress generations are not jointly available"
            )
        return physical_generation

    def fence_ingress(
        self,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        """Enter reset phase one without invalidating the live response path."""

        self._runtime_context_guard()
        if expected_connection_generation is None:
            self._ingress_gate.fence_backend_reset()
            return
        expected = require_backend_reset_connection_generation(
            expected_connection_generation
        )
        try:
            self._adapter.fence_backend_reset_generation(
                expected_connection_generation=expected,
                fence_ingress=lambda: self._ingress_gate.fence_backend_reset(
                    expected_connection_generation=expected
                ),
                timeout=self._connect_timeout_seconds,
            )
        except BackendResetGenerationStaleError:
            raise
        except CodexRpcConnectionGenerationMismatchError as exc:
            raise BackendResetGenerationStaleError(
                expected_generation=expected,
                observed_generation=exc.observed_generation,
                source="physical",
            ) from exc
        except AdapterBackendResetGenerationMismatchError as exc:
            raise BackendResetGenerationStaleError(
                expected_generation=expected,
                observed_generation=exc.observed_generation,
                source="ingress",
            ) from exc
        except (CodexRpcPreSendError, AdapterBackendResetUnavailableError) as exc:
            raise BackendResetUnavailableError(
                "the backend reset generation could not be fenced before its deadline"
            ) from exc

    def replace_owned_backend(
        self,
        *,
        retire_local_projection_after_stop: Callable[
            [], BackendResetLocalProjectionReceipt
        ],
    ) -> BackendResetReceipt:
        """Stop, settle, replace, validate, publish, and finally reopen ingress."""

        self._runtime_context_guard()
        if not callable(retire_local_projection_after_stop):
            raise TypeError(
                "backend reset requires a post-stop local projection capability"
            )
        self._ingress_gate.fence_backend_reset()
        interaction_lease_capture = (
            self._interaction_lease_store.capture_current_process_for_backend_stop()
        )
        self._adapter.stop()

        interaction_lease_retirement = (
            self._interaction_lease_store.retire_after_backend_stop(
                interaction_lease_capture
            )
        )
        registry_retirement = self._retire_server_requests_after_stop()
        fcodex_retirement = self._operation_owner.settle_backend_epoch_after_stop()
        web_retirement = self._retire_web_after_stop()
        local_projection = retire_local_projection_after_stop()
        if not isinstance(local_projection, BackendResetLocalProjectionReceipt):
            raise RuntimeError(
                "backend reset local projection returned an invalid receipt"
            )
        feishu_root_retirement = (
            self._retire_feishu_root_operations_after_stop()
        )
        feishu_retirement = self._retire_feishu_after_stop()
        try:
            self._dispatch_feishu_card_projection_best_effort()
        except Exception:
            # This capability is wired to a detached dispatcher, not to
            # Feishu transport itself. Keep an additional defensive boundary
            # so projection infrastructure can never invalidate the four
            # structural retirement receipts.
            logger.exception(
                "unable to dispatch post-stop Feishu card projection"
            )
        transport_retirement = (
            self._adapter.rotate_server_request_authority_after_backend_stop()
        )
        retirement = BackendResetEpochRetirementReceipt(
            interaction_leases=interaction_lease_retirement,
            server_request_registry=registry_retirement,
            fcodex=fcodex_retirement,
            web=web_retirement,
            local_projection=local_projection,
            feishu_root_operations=feishu_root_retirement,
            feishu_requests=feishu_retirement,
            transport=transport_retirement,
        )
        # Connection-wide invalidation is intentionally after machine-stop
        # retirement. Its broad disconnect projections are then idempotent and
        # can never authorize dropping a still-live response capability.
        self._begin_reset_ingress()
        machine_cleared_thread_ids = tuple(
            sorted(
                self._runtime_lease_store.purge_all_for_instance(
                    instance_name=self._instance_name
                )
            )
        )
        self._operation_owner.close_backend_epoch_after_machine_replace()
        self._runtime_authority.confirm_backend_reset()

        self._adapter.start()
        replacement_generation = self._adapter.connection_generation(
            timeout=self._connect_timeout_seconds,
            require_existing_connection=True,
        )
        if int(replacement_generation) <= 0:
            raise RuntimeError(
                "replacement Codex backend did not establish a valid websocket generation"
            )

        replacement_endpoint = str(
            self._adapter.current_app_server_url() or ""
        ).strip()
        if not replacement_endpoint:
            raise RuntimeError(
                "replacement Codex backend did not publish a usable endpoint"
            )
        self._ingress_gate.admit_backend_replacement(
            replacement_generation,
            # The endpoint was read before entering the gate, preserving the
            # sole RPC -> gate lock order used by actual-send validation.
            publish_replacement=lambda: self._publish_replacement(
                replacement_endpoint
            ),
        )
        return BackendResetReceipt(
            connection_generation=replacement_generation,
            retirement=retirement,
            machine_cleared_thread_ids=machine_cleared_thread_ids,
        )

    def _begin_reset_ingress(self) -> None:
        self._ingress_gate.begin_backend_reset()
