"""Runtime Admin binding-clear transactions.

This service owns the authority-read/revalidation/removal sequence for local
control-plane binding clears.  BindingRuntimeManager and the Feishu execution
queue remain the state owners; the service only coordinates their exact
cleanup order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot.binding_identity import format_binding_id
from bot.binding_runtime_lifecycle import (
    RuntimeTimerCancellationEffect,
    cancel_runtime_timer_effects,
)
from bot.binding_runtime_manager import BindingRuntimeManager


ChatBindingKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeBindingDeactivationReceipt:
    """One exact owner removal and its optional runtime finalizer target."""

    binding: ChatBindingKey
    thread_id: str
    unsubscribe_thread_id: str = ""
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeBindingBatchDeactivationReceipt:
    """Owner-confirmed effects of one locked batch deactivation."""

    confirmed_removals: tuple[RuntimeBindingDeactivationReceipt, ...]

    @property
    def timer_cancellations(self) -> tuple[RuntimeTimerCancellationEffect, ...]:
        return tuple(
            effect
            for removal in self.confirmed_removals
            for effect in removal.timer_cancellations
        )


class RuntimeBindingBatchDeactivationOwner:
    """Commit batch owner removal, cleanup, and queue invalidation together."""

    def __init__(
        self,
        *,
        binding_runtime: BindingRuntimeManager,
        invalidate_execution_queue_locked: Callable[[ChatBindingKey], None],
    ) -> None:
        self._binding_runtime = binding_runtime
        self._invalidate_execution_queue_locked = invalidate_execution_queue_locked

    def deactivate_locked(
        self,
        bindings: tuple[ChatBindingKey, ...],
        cleanup_errors: list[str] | None = None,
    ) -> RuntimeBindingBatchDeactivationReceipt:
        owner_cleanup_errors = cleanup_errors if cleanup_errors is not None else []
        committed_removals = (
            self._binding_runtime.deactivate_bindings_with_receipts_locked(
                bindings,
                cleanup_errors=owner_cleanup_errors,
            )
        )
        receipt = RuntimeBindingBatchDeactivationReceipt(
            confirmed_removals=tuple(
                RuntimeBindingDeactivationReceipt(
                    binding=item.binding,
                    thread_id=item.thread_id,
                    unsubscribe_thread_id=item.unsubscribe_thread_id,
                    timer_cancellations=item.timer_cancellations,
                )
                for item in committed_removals
            )
        )
        for removal in receipt.confirmed_removals:
            self._invalidate_execution_queue_locked(removal.binding)
        if cleanup_errors is None and owner_cleanup_errors:
            raise RuntimeError("；".join(owner_cleanup_errors))
        return receipt


@dataclass(frozen=True, slots=True)
class RuntimeBindingClearPorts:
    binding_clear_availability_locked: Callable[
        [ChatBindingKey], tuple[bool, str]
    ]
    require_direct_thread_target: Callable[..., Any]
    # Atomic owner boundary: preserve runtime-state cleanup during deactivation,
    # then invalidate this binding's queue exactly once after the commit.
    deactivate_binding_and_invalidate_queue_locked: Callable[
        [ChatBindingKey], RuntimeBindingBatchDeactivationReceipt
    ]
    # Atomic owner boundary: clean every resident runtime state, remove each
    # owner, and invalidate queues only for removals confirmed after commit.
    deactivate_bindings_and_invalidate_queues_locked: Callable[
        [tuple[ChatBindingKey, ...], list[str]],
        RuntimeBindingBatchDeactivationReceipt,
    ]
    clear_all_stored_bindings: Callable[[], None]
    invalidate_all_execution_queues_locked: Callable[[], int]
    finalize_deactivated_thread_runtime: Callable[..., None]
    raise_binding_cleanup_errors: Callable[[list[str]], None]


class RuntimeBindingClearService:
    """Coordinate binding owner removal and execution-queue invalidation."""

    def __init__(
        self,
        *,
        lock: Any,
        binding_runtime: BindingRuntimeManager,
        ports: RuntimeBindingClearPorts,
    ) -> None:
        if not isinstance(binding_runtime, BindingRuntimeManager):
            raise TypeError("runtime binding clear 缺少 binding state owner。")
        self._lock = lock
        self._binding_runtime = binding_runtime
        self._ports = ports

    def clear_one(self, binding: ChatBindingKey) -> dict[str, Any]:
        binding_id = format_binding_id(binding)
        with self._lock:
            allowed, reason = self._ports.binding_clear_availability_locked(binding)
            if not allowed:
                raise ValueError(reason)
            record = self._binding_runtime.binding_record_snapshot_locked(binding)
            assert record is not None
            thread_id = record.thread_id
            thread_title = record.thread_title

        if thread_id:
            self._ports.require_direct_thread_target(
                thread_id,
                operation="清除 binding",
            )

        with self._lock:
            # Authority reads happen outside the runtime lock.  Revalidate the
            # exact target before the owner-removal commit.
            allowed, reason = self._ports.binding_clear_availability_locked(binding)
            if not allowed:
                raise ValueError(reason)
            record = self._binding_runtime.binding_record_snapshot_locked(binding)
            if record is None:
                raise ValueError(f"未找到 binding：{binding_id}")
            if record.thread_id != thread_id:
                raise ValueError("binding 在线程核验期间发生变化；请重试。")
            receipt = self._ports.deactivate_binding_and_invalidate_queue_locked(
                binding
            )
            if self._binding_runtime.binding_exists_locked(binding):
                raise RuntimeError(
                    f"binding owner 移除未确认：{binding_id}"
                )

        cancel_runtime_timer_effects(receipt.timer_cancellations)
        unsubscribe_thread_id = next(
            (
                removal.unsubscribe_thread_id
                for removal in receipt.confirmed_removals
                if removal.unsubscribe_thread_id
            ),
            "",
        )
        if unsubscribe_thread_id:
            cleanup_errors: list[str] = []
            self._ports.finalize_deactivated_thread_runtime(
                [unsubscribe_thread_id],
                cleanup_errors=cleanup_errors,
            )
            self._ports.raise_binding_cleanup_errors(cleanup_errors)
        return {
            "binding_id": binding_id,
            "thread_id": thread_id,
            "thread_title": thread_title,
            "cleared": True,
        }

    def clear_all(self) -> dict[str, Any]:
        cleanup_errors: list[str] = []
        with self._lock:
            records = self._binding_runtime.binding_record_inventory_locked()
            bindings = [record.binding for record in records]
            if not bindings:
                self._ports.clear_all_stored_bindings()
                self._ports.invalidate_all_execution_queues_locked()
                return {
                    "cleared_binding_ids": [],
                    "already_empty": True,
                }
            planned_thread_ids = {
                record.binding: record.thread_id for record in records
            }
            blockers = self._clear_blockers(bindings)
            if blockers:
                raise ValueError(
                    "以下 binding 当前不能清除：\n" + "\n".join(blockers)
                )

        thread_ids = sorted(
            {thread_id for thread_id in planned_thread_ids.values() if thread_id}
        )
        # Complete every authority read before the first local/durable owner
        # mutation so a later target-validation failure cannot produce a partial batch.
        for thread_id in thread_ids:
            self._ports.require_direct_thread_target(
                thread_id,
                operation="批量清除 binding",
            )
        with self._lock:
            current_records = (
                self._binding_runtime.binding_record_inventory_locked()
            )
            if [record.binding for record in current_records] != bindings:
                raise ValueError("binding 在线程核验期间发生变化；请重试。")
            for record in current_records:
                if record.thread_id != planned_thread_ids[record.binding]:
                    raise ValueError("binding 在线程核验期间发生变化；请重试。")
            blockers = self._clear_blockers(bindings)
            if blockers:
                raise ValueError(
                    "以下 binding 当前不能清除：\n" + "\n".join(blockers)
                )

            receipt = (
                self._ports.deactivate_bindings_and_invalidate_queues_locked(
                    tuple(bindings),
                    cleanup_errors,
                )
            )
            cleared_bindings = [item.binding for item in receipt.confirmed_removals]
            if not self._binding_runtime.binding_record_inventory_locked():
                # Queue-only orphan keys are not present in binding inventory.
                self._ports.invalidate_all_execution_queues_locked()

        cancel_runtime_timer_effects(receipt.timer_cancellations)
        unsubscribe_thread_ids = tuple(
            item.unsubscribe_thread_id
            for item in receipt.confirmed_removals
            if item.unsubscribe_thread_id
        )
        if unsubscribe_thread_ids:
            self._ports.finalize_deactivated_thread_runtime(
                unsubscribe_thread_ids,
                cleanup_errors=cleanup_errors,
            )
        self._ports.raise_binding_cleanup_errors(cleanup_errors)
        return {
            "cleared_binding_ids": [
                format_binding_id(binding) for binding in cleared_bindings
            ],
            "already_empty": False,
        }

    def _clear_blockers(
        self,
        bindings: list[ChatBindingKey],
    ) -> list[str]:
        blockers: list[str] = []
        for binding in bindings:
            allowed, reason = self._ports.binding_clear_availability_locked(binding)
            if not allowed:
                blockers.append(f"{format_binding_id(binding)}: {reason}")
        return blockers
