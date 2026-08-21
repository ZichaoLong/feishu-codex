"""Product-level orchestration for resetting one Focus backend instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeAlias

from bot.backend_reset.contract import (
    BackendResetLocalProjectionReceipt,
    BackendResetPolicyRejectedError,
    BackendResetPreview,
    require_backend_reset_connection_generation,
)
from bot.backend_reset.interaction_coordinator import (
    BackendResetInteractionReceipt,
)
from bot.binding_execution_runtime import (
    BindingExecutionRuntimeTransitions,
    InterruptBindingExecutionCommand,
    InterruptedBindingExecution,
)
from bot.binding_identity import format_binding_id
from bot.binding_runtime_lifecycle import (
    RuntimeTimerCancellationEffect,
    cancel_runtime_timer_effects,
)
from bot.binding_runtime_contract import BindingSessionSnapshot


ChatBindingKey: TypeAlias = tuple[str, str]


class BackendResetDetachResult(Protocol):
    detached_binding_ids: list[str]
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...]


class BackendResetBindingRuntime(Protocol):
    """Binding-state owner operations used by the reset projection."""

    def binding_session_inventory_locked(
        self,
    ) -> tuple[BindingSessionSnapshot, ...]: ...

    def detach_thread_bindings_locked(
        self,
        thread_id: str,
        *,
        detach_availability: Callable[[str], tuple[bool, str]],
    ) -> BackendResetDetachResult: ...

class BackendResetEpochReceipt(Protocol):
    machine_cleared_thread_ids: tuple[str, ...]
    retirement: "BackendResetEpochRetirementReceipt"


class BackendResetEpochRetirementReceipt(Protocol):
    local_projection: BackendResetLocalProjectionReceipt


class BackendResetEpochCoordinator(Protocol):
    """The narrower backend-epoch replacement transaction."""

    def fence_ingress(
        self,
        *,
        expected_connection_generation: int | None = None,
    ) -> None: ...

    def replace_owned_backend(
        self,
        *,
        retire_local_projection_after_stop: Callable[
            [], BackendResetLocalProjectionReceipt
        ],
    ) -> BackendResetEpochReceipt: ...


class BackendResetInteractionPreparation(Protocol):
    """Aggregate owner for the complete pre-stop interaction phase."""

    def prepare_all(self) -> BackendResetInteractionReceipt: ...


class BackendResetExecutionFinalizationResult(Protocol):
    retired: bool
    presentation_error: str


class BackendResetLock(Protocol):
    def __enter__(self) -> Any: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: Any,
    ) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class BackendResetServicePorts:
    """Handler-owned effects required by the product reset workflow."""

    backend_reset_preview: Callable[[], BackendResetPreview]
    invalidate_all_feishu_execution_queues_locked: Callable[[], int]
    finalize_execution: Callable[
        [BindingSessionSnapshot], BackendResetExecutionFinalizationResult
    ]
    interaction_preparation: BackendResetInteractionPreparation
    published_app_server_url: Callable[[], str]
    runtime_context_guard: Callable[[], None]


class BackendResetService:
    """Project and replace one backend without taking ownership of its state.

    Runtime-admin owns reset policy, binding/turn controllers own their state,
    and :class:`BackendResetCoordinator` owns the backend epoch transaction.
    This service owns only their product-level ordering and result projection.
    """

    _RESET_NOTE = "管理员已重置当前实例 backend，本轮执行已中断。"

    def __init__(
        self,
        *,
        lock: BackendResetLock,
        binding_runtime: BackendResetBindingRuntime,
        execution_runtime: BindingExecutionRuntimeTransitions,
        epoch_coordinator: BackendResetEpochCoordinator,
        ports: BackendResetServicePorts,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._execution_runtime = execution_runtime
        self._epoch_coordinator = epoch_coordinator
        self._ports = ports

    def reset_current_instance(
        self,
        *,
        force: bool,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]:
        """Apply reset policy/projections, then replace the backend epoch."""

        if type(force) is not bool:
            raise TypeError("backend reset force must be an exact bool")
        if expected_connection_generation is not None:
            expected_connection_generation = (
                require_backend_reset_connection_generation(
                    expected_connection_generation
                )
            )
        self._ports.runtime_context_guard()
        preview = self._ports.backend_reset_preview()
        if preview.status == "blocked":
            raise BackendResetPolicyRejectedError(preview.reason_text)
        if preview.status == "force-only" and not force:
            raise BackendResetPolicyRejectedError(preview.reason_text)

        # Fence reader/callback ingress before changing local projections.
        self._epoch_coordinator.fence_ingress(
            expected_connection_generation=expected_connection_generation
        )
        active_bindings, bound_thread_ids = self._binding_inventory()

        # This is diagnostic inventory only. Machine stop below invalidates
        # every old-connection request and surface capability atomically.
        interaction_receipt = self._ports.interaction_preparation.prepare_all()
        if not isinstance(interaction_receipt, BackendResetInteractionReceipt):
            raise RuntimeError(
                "backend reset interaction preparation returned an invalid receipt"
            )

        backend_reset = self._epoch_coordinator.replace_owned_backend(
            retire_local_projection_after_stop=lambda: (
                self._retire_local_projection_after_stop(
                    active_bindings,
                    bound_thread_ids,
                )
            )
        )
        local_projection = backend_reset.retirement.local_projection
        return {
            "force": bool(force),
            "detached_binding_ids": list(local_projection.detached_binding_ids),
            "interrupted_binding_ids": list(
                local_projection.interrupted_binding_ids
            ),
            "retired_request_count": interaction_receipt.pending_request_count,
            "purged_thread_ids": list(backend_reset.machine_cleared_thread_ids),
            "projection_warnings": list(local_projection.projection_warnings),
            "app_server_url": self._ports.published_app_server_url(),
        }

    def _binding_inventory(
        self,
    ) -> tuple[list[BindingSessionSnapshot], list[str]]:
        with self._lock:
            # The FIFO owner, not BindingRuntime, is the SSOT for queue keys.
            # Clear it once before taking the canonical session inventory so
            # an orphan key cannot survive reset and replay on replacement.
            self._ports.invalidate_all_feishu_execution_queues_locked()
            sessions = self._binding_runtime.binding_session_inventory_locked()
            active_sessions = [
                snapshot
                for snapshot in sessions
                if snapshot.execution.has_execution_anchor
            ]
            bound_thread_ids = sorted(
                {
                    str(snapshot.current_thread_id or "").strip()
                    for snapshot in sessions
                    if str(snapshot.current_thread_id or "").strip()
                }
            )
        return active_sessions, bound_thread_ids

    def _retire_local_projection_after_stop(
        self,
        active_sessions: list[BindingSessionSnapshot],
        bound_thread_ids: list[str],
    ) -> BackendResetLocalProjectionReceipt:
        """Retire binding/execution projections only after confirmed stop."""

        projection_warnings: list[str] = []
        interrupted_binding_ids = self._interrupt_active_bindings(
            active_sessions,
            projection_warnings,
        )
        detached_binding_ids = self._detach_bound_threads(bound_thread_ids)
        return BackendResetLocalProjectionReceipt(
            detached_binding_ids=tuple(sorted(set(detached_binding_ids))),
            interrupted_binding_ids=tuple(sorted(set(interrupted_binding_ids))),
            projection_warnings=tuple(projection_warnings),
        )

    def _interrupt_active_bindings(
        self,
        active_sessions: list[BindingSessionSnapshot],
        projection_warnings: list[str],
    ) -> list[str]:
        interrupted_binding_ids: list[str] = []
        for snapshot in active_sessions:
            # Process-note and execution-state mutation are one local
            # correctness transition. Propagate either failure so reset cannot
            # reopen around a still-live old execution.
            interrupted = self._interrupt_binding_execution(snapshot)
            if interrupted is None:
                continue
            interrupted_binding_ids.append(interrupted.binding_id)
            finalization = self._ports.finalize_execution(
                interrupted.session
            )
            if finalization.presentation_error:
                projection_warnings.append(
                    "finalize "
                    f"{format_binding_id(interrupted.session.binding)}: "
                    f"{finalization.presentation_error}"
                )
        return interrupted_binding_ids

    def _interrupt_binding_execution(
        self,
        captured: BindingSessionSnapshot,
    ) -> InterruptedBindingExecution | None:
        return self._execution_runtime.interrupt_for_backend_reset(
            InterruptBindingExecutionCommand(
                session=captured,
                process_note=f"\n[中断] {self._RESET_NOTE}\n",
            )
        )

    def _detach_bound_threads(
        self,
        bound_thread_ids: list[str],
    ) -> list[str]:
        detached_binding_ids: list[str] = []
        timer_cancellations: list[RuntimeTimerCancellationEffect] = []
        try:
            with self._lock:
                for thread_id in bound_thread_ids:
                    result = self._binding_runtime.detach_thread_bindings_locked(
                        thread_id,
                        detach_availability=lambda _thread_id: (True, ""),
                    )
                    detached_binding_ids.extend(result.detached_binding_ids)
                    timer_cancellations.extend(result.timer_cancellations)
        finally:
            cancel_runtime_timer_effects(tuple(timer_cancellations))
        return detached_binding_ids
