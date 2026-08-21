"""Surface-neutral thread lifecycle policy and local settlement.

Archive, unarchive, and delete are shared root-thread operations.  Their
admission, upstream-outcome classification, and post-success local cleanup
belong here rather than to a CLI, Feishu, or Web controller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, TypeAlias, TypedDict

from bot.adapters.base import ThreadSummary
from bot.binding_identity import format_binding_id
from bot.binding_runtime_lifecycle import (
    RuntimeTimerCancellationEffect,
    cancel_runtime_timer_effects,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.direct_thread_target_policy import (
    DirectThreadTargetPolicyError,
    read_direct_thread_target,
)
from bot.reason_codes import ReasonedCheck
from bot.runtime_state import (
    BACKEND_THREAD_LOOKUP_ERROR,
    BACKEND_THREAD_LOOKUP_MISSING,
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_IDLE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
    BACKEND_THREAD_STATUS_UNKNOWN,
    FEISHU_RUNTIME_ATTACHED,
    LOADED_BACKEND_THREAD_STATUSES,
)
from bot.stores.interaction_lease_store import InteractionLeaseHolder
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]

UPSTREAM_OUTCOME_SUCCESS = "success"
UPSTREAM_OUTCOME_ERROR = "error"
UPSTREAM_OUTCOME_UNKNOWN = "unknown"
FOCUS_CLEANUP_COMPLETE = "complete"
FOCUS_CLEANUP_INCOMPLETE = "incomplete"
FOCUS_CLEANUP_SKIPPED = "skipped"


class ThreadLifecyclePolicyError(ValueError):
    """An expected, user-correctable lifecycle admission refusal."""


class ThreadLifecycleResult(TypedDict, total=False):
    """Stable result DTO shared by local-control, Feishu, and Web surfaces."""

    operation: str
    thread_id: str
    thread_title: str
    working_dir: str
    bound_binding_ids: list[str]
    attached_binding_ids: list[str]
    detached_binding_ids: list[str]
    cleared_binding_ids: list[str]
    live_runtime_owner: dict[str, str]
    upstream_outcome: str
    upstream_error: str
    outcome_detail: str
    focus_cleanup: str
    cleanup_errors: list[str]


@dataclass(frozen=True, slots=True)
class LocalBindingClearPlan:
    """Immutable view of the local bindings affected by one cleanup."""

    bindings: tuple[ChatBindingKey, ...]
    attached_bindings: tuple[ChatBindingKey, ...]
    running_binding_ids: tuple[str, ...]
    pending_binding_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThreadLifecycleBackendPort:
    read_thread: Callable[[str], Any]
    list_loaded_thread_ids: Callable[[], list[str]]
    archive_thread: Callable[[str], None]
    unarchive_thread: Callable[[str], ThreadSummary]
    delete_thread: Callable[[str], None]
    is_thread_not_found_error: Callable[[Exception], bool]
    is_thread_not_loaded_error: Callable[[Exception], bool]


@dataclass(frozen=True, slots=True)
class ThreadLifecycleAdmissionPort:
    instance_name: Callable[[], str]
    load_runtime_lease: Callable[[str], ThreadRuntimeLease | None]
    external_write_denial_check: Callable[..., ReasonedCheck]
    loaded_gate_check: Callable[[str, str], ReasonedCheck]


@dataclass(frozen=True, slots=True)
class ThreadLifecycleCleanupPort:
    binding_has_pending_request_locked: Callable[[ChatBindingKey], bool]
    invalidate_feishu_execution_queue_locked: Callable[[ChatBindingKey], None]
    unsubscribe_thread: Callable[[str], None]
    release_service_runtime_lease: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ThreadLifecyclePorts:
    backend: ThreadLifecycleBackendPort
    admission: ThreadLifecycleAdmissionPort
    cleanup: ThreadLifecycleCleanupPort


class ThreadLifecycleService:
    """Own root lifecycle admission, upstream mutation, and local settlement."""

    def __init__(
        self,
        *,
        lock: Any,
        binding_runtime: BindingRuntimeManager,
        ports: ThreadLifecyclePorts,
    ) -> None:
        self._lock = lock
        self._binding_runtime = binding_runtime

        backend = ports.backend
        self._read_thread = backend.read_thread
        self._list_loaded_thread_ids = backend.list_loaded_thread_ids
        self._archive_thread = backend.archive_thread
        self._unarchive_thread = backend.unarchive_thread
        self._delete_thread = backend.delete_thread
        self._is_thread_not_found_error = backend.is_thread_not_found_error
        self._is_thread_not_loaded_error = backend.is_thread_not_loaded_error

        admission = ports.admission
        self._instance_name = admission.instance_name
        self._load_runtime_lease = admission.load_runtime_lease
        self._external_write_denial_check = admission.external_write_denial_check
        self._loaded_gate_check = admission.loaded_gate_check

        cleanup = ports.cleanup
        self._binding_has_pending_request_locked = (
            cleanup.binding_has_pending_request_locked
        )
        self._invalidate_feishu_execution_queue_locked = (
            cleanup.invalidate_feishu_execution_queue_locked
        )
        self._unsubscribe_thread = cleanup.unsubscribe_thread
        self._release_service_runtime_lease = cleanup.release_service_runtime_lease

    def read_thread_summary_for_status(
        self, thread_id: str
    ) -> tuple[ThreadSummary | None, str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None, ""
        try:
            summary = self._read_thread(normalized_thread_id).summary
        except Exception as exc:
            if self._is_thread_not_found_error(exc):
                return None, BACKEND_THREAD_LOOKUP_MISSING
            if self._is_thread_not_loaded_error(exc):
                return None, BACKEND_THREAD_STATUS_NOT_LOADED
            logger.exception("读取线程状态失败: thread=%s", normalized_thread_id[:12])
            return None, BACKEND_THREAD_LOOKUP_ERROR
        status = str(summary.status or BACKEND_THREAD_STATUS_UNKNOWN).strip()
        return summary, status or BACKEND_THREAD_STATUS_UNKNOWN

    def loaded_thread_status_for_control(self, thread_id: str) -> dict[str, str]:
        normalized_thread_id = self._require_thread_id(thread_id)
        loaded_thread_ids = {
            str(item or "").strip()
            for item in self._list_loaded_thread_ids()
            if str(item or "").strip()
        }
        if normalized_thread_id not in loaded_thread_ids:
            backend_thread_status = BACKEND_THREAD_STATUS_NOT_LOADED
        else:
            _summary, backend_thread_status = self.read_thread_summary_for_status(
                normalized_thread_id
            )
            if backend_thread_status not in LOADED_BACKEND_THREAD_STATUSES:
                backend_thread_status = BACKEND_THREAD_STATUS_UNKNOWN
        return {
            "thread_id": normalized_thread_id,
            "backend_thread_status": backend_thread_status,
        }

    def archive_thread_for_control(
        self,
        thread_id: str,
        *,
        summary: ThreadSummary | None = None,
        writer_holder: InteractionLeaseHolder | None = None,
    ) -> ThreadLifecycleResult:
        del summary  # Presentation summaries are never lifecycle authority.
        normalized_thread_id = self._require_thread_id(thread_id)
        effective_summary = self._require_direct_thread_target(
            normalized_thread_id,
            operation="archive",
        )
        self._require_external_writer_admission(
            normalized_thread_id,
            operation="archive",
            writer_holder=writer_holder,
        )
        self._require_loaded_gate(normalized_thread_id, operation="archive")
        lease = self._load_runtime_lease(normalized_thread_id)
        self._require_no_non_service_runtime_holder(lease, operation="archive")
        live_runtime_owner = self._live_runtime_owner_snapshot(lease)
        owner_instance = live_runtime_owner["instance_name"]
        if owner_instance and owner_instance != self._instance_name():
            raise ThreadLifecyclePolicyError(
                f"当前 thread 的 live runtime 由实例 `{owner_instance}` 持有；"
                "请改在该实例执行 archive。"
            )
        with self._lock:
            plan = self.local_binding_clear_plan_locked(normalized_thread_id)
            snapshot = self._binding_snapshot_locked(plan)
        self.require_binding_cleanup_allowed(plan, action_text="archive 该 thread")
        try:
            self._archive_thread(normalized_thread_id)
        except (TimeoutError, CodexRpcError, CodexRpcProtocolError) as exc:
            return self._error_result(
                operation="archive",
                thread_id=normalized_thread_id,
                summary=effective_summary,
                snapshot=snapshot,
                live_runtime_owner=live_runtime_owner,
                error=exc,
            )
        cleanup = self._cleanup_local_bindings_after_success(
            normalized_thread_id,
            plan.bindings,
            owner_loss_disposition="terminal",
        )
        return {
            "thread_id": normalized_thread_id,
            "thread_title": effective_summary.title,
            "working_dir": effective_summary.cwd,
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "detached_binding_ids": snapshot["detached_binding_ids"],
            "live_runtime_owner": live_runtime_owner,
            "upstream_outcome": UPSTREAM_OUTCOME_SUCCESS,
            **cleanup,
        }

    def unarchive_thread_for_control(
        self,
        thread_id: str,
        *,
        writer_holder: InteractionLeaseHolder | None = None,
    ) -> ThreadLifecycleResult:
        normalized_thread_id = self._require_thread_id(thread_id)
        self._require_direct_thread_target(normalized_thread_id, operation="unarchive")
        self._require_external_writer_admission(
            normalized_thread_id,
            operation="unarchive",
            writer_holder=writer_holder,
        )
        current_status = self.loaded_thread_status_for_control(normalized_thread_id)[
            "backend_thread_status"
        ]
        if current_status != BACKEND_THREAD_STATUS_NOT_LOADED:
            raise ThreadLifecyclePolicyError(
                "当前目标实例仍将该 thread 保持为 loaded "
                f"(`{current_status or BACKEND_THREAD_STATUS_UNKNOWN}`)；拒绝 unarchive。"
                "请先等待其 unload，或在确认可丢弃 live runtime 后 reset 当前实例 backend。"
            )
        self._require_loaded_gate(normalized_thread_id, operation="unarchive")
        lease = self._load_runtime_lease(normalized_thread_id)
        live_runtime_owner = self._live_runtime_owner_snapshot(lease)
        owner_instance = live_runtime_owner["instance_name"]
        if owner_instance:
            raise ThreadLifecyclePolicyError(
                f"当前 thread 仍有 live runtime owner `{owner_instance}`；不能执行 unarchive。"
            )
        with self._lock:
            plan = self.local_binding_clear_plan_locked(normalized_thread_id)
        if plan.bindings:
            raise ThreadLifecyclePolicyError(
                "当前实例仍有 binding 指向该 archived thread；请先清理 binding 后再执行 unarchive："
                + ", ".join(
                    f"`{format_binding_id(binding)}`" for binding in plan.bindings
                )
            )
        try:
            summary = self._unarchive_thread(normalized_thread_id)
        except (TimeoutError, CodexRpcError, CodexRpcProtocolError) as exc:
            return self._error_result(
                operation="unarchive",
                thread_id=normalized_thread_id,
                summary=None,
                snapshot=None,
                live_runtime_owner=live_runtime_owner,
                error=exc,
            )
        return {
            "thread_id": normalized_thread_id,
            "thread_title": summary.title,
            "working_dir": summary.cwd,
            "bound_binding_ids": [],
            "attached_binding_ids": [],
            "detached_binding_ids": [],
            "cleared_binding_ids": [],
            "live_runtime_owner": live_runtime_owner,
            "upstream_outcome": UPSTREAM_OUTCOME_SUCCESS,
            "focus_cleanup": FOCUS_CLEANUP_SKIPPED,
            "cleanup_errors": [],
        }

    def delete_thread_for_control(
        self,
        thread_id: str,
        *,
        writer_holder: InteractionLeaseHolder | None = None,
    ) -> ThreadLifecycleResult:
        normalized_thread_id = self._require_thread_id(thread_id)
        self._require_direct_thread_target(normalized_thread_id, operation="delete")
        self._require_external_writer_admission(
            normalized_thread_id,
            operation="delete",
            writer_holder=writer_holder,
        )
        self._require_loaded_gate(normalized_thread_id, operation="delete")
        effective_summary, backend_thread_status = self.read_thread_summary_for_status(
            normalized_thread_id
        )
        lease = self._load_runtime_lease(normalized_thread_id)
        self._require_no_non_service_runtime_holder(lease, operation="delete")
        live_runtime_owner = self._live_runtime_owner_snapshot(lease)
        owner_instance = live_runtime_owner["instance_name"]
        if owner_instance and owner_instance != self._instance_name():
            raise ThreadLifecyclePolicyError(
                f"当前 thread 的 live runtime 由实例 `{owner_instance}` 持有；"
                "请改在该实例执行 delete。"
            )
        if backend_thread_status == BACKEND_THREAD_STATUS_ACTIVE:
            raise ThreadLifecyclePolicyError(
                "当前 root thread 的 backend 状态为 `active`；拒绝 delete。"
                "请先结束当前执行并确认状态变为 `idle`。"
            )
        if backend_thread_status not in {
            BACKEND_THREAD_STATUS_IDLE,
            BACKEND_THREAD_STATUS_NOT_LOADED,
            BACKEND_THREAD_LOOKUP_MISSING,
        }:
            raise ThreadLifecyclePolicyError(
                "无法确认 root thread 处于可删除的静止状态；拒绝 delete："
                f"backend status=`{backend_thread_status or BACKEND_THREAD_STATUS_UNKNOWN}`。"
            )
        with self._lock:
            plan = self.local_binding_clear_plan_locked(normalized_thread_id)
            snapshot = self._binding_snapshot_locked(plan)
        self.require_binding_cleanup_allowed(plan, action_text="delete 该 thread")
        try:
            self._delete_thread(normalized_thread_id)
        except (TimeoutError, CodexRpcError, CodexRpcProtocolError) as exc:
            return self._error_result(
                operation="delete",
                thread_id=normalized_thread_id,
                summary=effective_summary,
                snapshot=snapshot,
                live_runtime_owner=live_runtime_owner,
                error=exc,
            )
        cleanup = self._cleanup_local_bindings_after_success(
            normalized_thread_id,
            plan.bindings,
            owner_loss_disposition="terminal",
        )
        return {
            "thread_id": normalized_thread_id,
            "thread_title": effective_summary.title
            if effective_summary is not None
            else "",
            "working_dir": effective_summary.cwd
            if effective_summary is not None
            else "",
            "bound_binding_ids": snapshot["bound_binding_ids"],
            "attached_binding_ids": snapshot["attached_binding_ids"],
            "detached_binding_ids": snapshot["detached_binding_ids"],
            "live_runtime_owner": live_runtime_owner,
            "upstream_outcome": UPSTREAM_OUTCOME_SUCCESS,
            **cleanup,
        }

    def local_thread_bindings_for_control(self, thread_id: str) -> dict[str, Any]:
        normalized_thread_id = self._require_thread_id(thread_id)
        with self._lock:
            plan = self.local_binding_clear_plan_locked(normalized_thread_id)
        return {
            "thread_id": normalized_thread_id,
            "binding_ids": [format_binding_id(binding) for binding in plan.bindings],
            "running_binding_ids": list(plan.running_binding_ids),
            "pending_binding_ids": list(plan.pending_binding_ids),
        }

    def clear_archived_thread_bindings_for_control(
        self,
        thread_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        normalized_thread_id = self._require_thread_id(thread_id)
        self._require_direct_thread_target(
            normalized_thread_id,
            operation="清理 archived thread bindings",
        )
        cleanup_errors: list[str] = []
        timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()
        try:
            with self._lock:
                plan = self.local_binding_clear_plan_locked(normalized_thread_id)
                self.require_binding_cleanup_allowed(
                    plan,
                    action_text="清理该已归档 thread 的本地 bindings",
                )
                existing_bindings = tuple(
                    binding
                    for binding in plan.bindings
                    if self._binding_runtime.binding_exists_locked(binding)
                )
                if dry_run:
                    return {
                        "thread_id": normalized_thread_id,
                        "would_clear_binding_ids": [
                            format_binding_id(binding) for binding in existing_bindings
                        ],
                        "dry_run": True,
                    }
                deactivation_receipts = (
                    self._binding_runtime.deactivate_bindings_with_receipts_locked(
                        existing_bindings,
                        cleanup_errors=cleanup_errors,
                    )
                )
                timer_cancellations = tuple(
                    effect
                    for receipt in deactivation_receipts
                    for effect in receipt.timer_cancellations
                )
                unsubscribe_thread_ids = tuple(
                    receipt.unsubscribe_thread_id
                    for receipt in deactivation_receipts
                    if receipt.unsubscribe_thread_id
                )
                cleared_bindings = tuple(
                    binding
                    for binding in existing_bindings
                    if not self._binding_runtime.binding_exists_locked(binding)
                )
                for binding in cleared_bindings:
                    self._invalidate_feishu_execution_queue_locked(binding)
                cleared_binding_ids = [
                    format_binding_id(binding) for binding in cleared_bindings
                ]
                retained_binding_ids = [
                    format_binding_id(binding)
                    for binding in existing_bindings
                    if self._binding_runtime.binding_exists_locked(binding)
                ]
        finally:
            cancel_runtime_timer_effects(timer_cancellations)
        self._finalize_deactivated_runtime(
            unsubscribe_thread_ids,
            release_only_thread_ids=(
                {normalized_thread_id}
                if cleared_binding_ids and not retained_binding_ids
                else set()
            ),
            cleanup_errors=cleanup_errors,
        )
        if cleanup_errors:
            raise RuntimeError("binding 清理不完整：" + "；".join(cleanup_errors))
        return {
            "thread_id": normalized_thread_id,
            "cleared_binding_ids": cleared_binding_ids,
            "cleared": bool(cleared_binding_ids),
        }

    def local_binding_clear_plan_locked(self, thread_id: str) -> LocalBindingClearPlan:
        """Build a pure runtime+store plan while the shared lock is held.

        Store-only recovery records remain inspection facts here.  Owner loss,
        lease release, durable deletion, and runtime removal happen only in
        the later scoped deactivation commit.
        """

        normalized_thread_id = str(thread_id or "").strip()
        records = tuple(
            record
            for record in self._binding_runtime.binding_record_inventory_locked()
            if record.thread_id == normalized_thread_id
        )
        bindings = tuple(record.binding for record in records)
        attached_bindings = tuple(
            record.binding
            for record in records
            if record.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED
        )
        running_binding_ids = tuple(
            format_binding_id(record.binding)
            for record in records
            if record.has_inflight_turn
        )
        pending_binding_ids = tuple(
            format_binding_id(binding)
            for binding in bindings
            if self._binding_has_pending_request_locked(binding)
        )
        return LocalBindingClearPlan(
            bindings=bindings,
            attached_bindings=attached_bindings,
            running_binding_ids=running_binding_ids,
            pending_binding_ids=pending_binding_ids,
        )

    @staticmethod
    def require_binding_cleanup_allowed(
        plan: LocalBindingClearPlan,
        *,
        action_text: str,
    ) -> None:
        if plan.running_binding_ids:
            raise ThreadLifecyclePolicyError(
                f"当前实例仍有飞书侧 turn 正在运行，不能 {action_text}："
                + ", ".join(
                    f"`{binding_id}`" for binding_id in plan.running_binding_ids
                )
            )
        if plan.pending_binding_ids:
            raise ThreadLifecyclePolicyError(
                f"当前实例仍有待处理审批或补充输入，不能 {action_text}："
                + ", ".join(
                    f"`{binding_id}`" for binding_id in plan.pending_binding_ids
                )
            )

    @staticmethod
    def _require_thread_id(thread_id: str) -> str:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ThreadLifecyclePolicyError("thread_id 不能为空。")
        return normalized_thread_id

    def _require_direct_thread_target(
        self, thread_id: str, *, operation: str
    ) -> ThreadSummary:
        try:
            return read_direct_thread_target(
                thread_id,
                read_thread=self._read_thread,
                operation=operation,
            )
        except DirectThreadTargetPolicyError as exc:
            raise ThreadLifecyclePolicyError(str(exc)) from exc
        except Exception as exc:
            raise ThreadLifecyclePolicyError(
                "无法确认 root thread 是否可直接操作；已按 fail-closed 拒绝"
                f" {operation}。"
            ) from exc

    def _require_external_writer_admission(
        self,
        thread_id: str,
        *,
        operation: str,
        writer_holder: InteractionLeaseHolder | None,
    ) -> None:
        denial = self._external_write_denial_check(thread_id, writer_holder)
        if not denial.allowed:
            raise ThreadLifecyclePolicyError(
                denial.reason_text
                or f"当前 main-turn 状态不允许从控制面 {operation}。"
            )

    def _require_loaded_gate(self, thread_id: str, *, operation: str) -> None:
        check = self._loaded_gate_check(thread_id, operation)
        if not check.allowed:
            raise ThreadLifecyclePolicyError(
                check.reason_text or f"thread {operation} 被 loaded gate 拒绝。"
            )

    @staticmethod
    def _live_runtime_owner_snapshot(
        lease: ThreadRuntimeLease | None,
    ) -> dict[str, str]:
        if lease is None:
            return {"instance_name": "", "label": "none"}
        instance_name = str(lease.owner_instance or "").strip()
        return {
            "instance_name": instance_name,
            "label": instance_name or "unknown",
        }

    @staticmethod
    def _live_runtime_holder_labels(lease: ThreadRuntimeLease | None) -> list[str]:
        if lease is None:
            return []
        labels: list[str] = []
        for holder in lease.holders:
            holder_type = str(holder.holder_type or "").strip() or "unknown"
            instance_name = str(holder.instance_name or "").strip() or "unknown"
            label = f"{holder_type}@{instance_name}"
            if int(holder.owner_pid or 0) > 0:
                label += f"(pid={int(holder.owner_pid)})"
            labels.append(label)
        return labels

    def _require_no_non_service_runtime_holder(
        self,
        lease: ThreadRuntimeLease | None,
        *,
        operation: str,
    ) -> None:
        holder_labels = [
            label
            for holder, label in zip(
                lease.holders if lease is not None else (),
                self._live_runtime_holder_labels(lease),
            )
            if str(holder.holder_type or "").strip() != "service"
        ]
        if holder_labels:
            raise ThreadLifecyclePolicyError(
                f"当前 thread 仍有非 service live runtime holder；拒绝 {operation}："
                + ", ".join(holder_labels)
            )

    def _binding_snapshot_locked(
        self,
        plan: LocalBindingClearPlan,
    ) -> dict[str, list[str]]:
        attached_bindings = set(plan.attached_bindings)
        return {
            "bound_binding_ids": [
                format_binding_id(binding) for binding in plan.bindings
            ],
            "attached_binding_ids": [
                format_binding_id(binding)
                for binding in plan.bindings
                if binding in attached_bindings
            ],
            "detached_binding_ids": [
                format_binding_id(binding)
                for binding in plan.bindings
                if binding not in attached_bindings
            ],
        }

    def _cleanup_local_bindings_after_success(
        self,
        thread_id: str,
        bindings: tuple[ChatBindingKey, ...],
        *,
        owner_loss_disposition: str,
    ) -> dict[str, Any]:
        cleanup_errors: list[str] = []
        cleared_binding_ids: list[str] = []
        retained_binding_ids: list[str] = []
        unsubscribe_thread_ids: tuple[str, ...] = ()
        timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()
        binding_cleanup_verified = False
        try:
            with self._lock:
                existing_bindings: list[ChatBindingKey] = []
                for binding in bindings:
                    record = self._binding_runtime.binding_record_snapshot_locked(
                        binding
                    )
                    if record is None:
                        continue
                    if record.thread_id != thread_id:
                        cleanup_errors.append(
                            "binding 在 upstream 操作期间已指向其他 thread："
                            f"{format_binding_id(binding)}"
                        )
                        continue
                    existing_bindings.append(binding)
                deactivation_receipts = (
                    self._binding_runtime.deactivate_bindings_with_receipts_locked(
                        tuple(existing_bindings),
                        cleanup_errors=cleanup_errors,
                        owner_loss_disposition=owner_loss_disposition,
                    )
                )
                timer_cancellations = tuple(
                    effect
                    for receipt in deactivation_receipts
                    for effect in receipt.timer_cancellations
                )
                unsubscribe_thread_ids = tuple(
                    receipt.unsubscribe_thread_id
                    for receipt in deactivation_receipts
                    if receipt.unsubscribe_thread_id
                )
                cleared_bindings = tuple(
                    binding
                    for binding in existing_bindings
                    if not self._binding_runtime.binding_exists_locked(binding)
                )
                for binding in cleared_bindings:
                    self._invalidate_feishu_execution_queue_locked(binding)
                cleared_binding_ids.extend(
                    format_binding_id(binding) for binding in cleared_bindings
                )
                retained_binding_ids.extend(
                    format_binding_id(binding)
                    for binding in bindings
                    if self._binding_runtime.binding_exists_locked(binding)
                )
                binding_cleanup_verified = True
        except Exception as exc:
            cleanup_errors.append(f"清理本地 binding 失败: {exc}")
            try:
                with self._lock:
                    retained_binding_ids.extend(
                        format_binding_id(binding)
                        for binding in bindings
                        if self._binding_runtime.binding_exists_locked(binding)
                    )
            except Exception as retained_exc:
                cleanup_errors.append(f"复核 retained binding 失败: {retained_exc}")
        finally:
            cancel_runtime_timer_effects(timer_cancellations)

        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        self._finalize_deactivated_runtime(
            unique_unsubscribe_thread_ids,
            release_only_thread_ids=(
                {thread_id}
                if binding_cleanup_verified
                and thread_id not in unique_unsubscribe_thread_ids
                and not retained_binding_ids
                else set()
            ),
            cleanup_errors=cleanup_errors,
        )
        return {
            "cleared_binding_ids": cleared_binding_ids,
            "focus_cleanup": (
                FOCUS_CLEANUP_INCOMPLETE if cleanup_errors else FOCUS_CLEANUP_COMPLETE
            ),
            "cleanup_errors": cleanup_errors,
        }

    def _finalize_deactivated_runtime(
        self,
        unsubscribe_thread_ids: list[str] | tuple[str, ...],
        *,
        release_only_thread_ids: set[str],
        cleanup_errors: list[str],
    ) -> None:
        unique_unsubscribe_thread_ids = sorted(set(unsubscribe_thread_ids))
        for thread_id in unique_unsubscribe_thread_ids:
            try:
                self._unsubscribe_thread(thread_id)
            except Exception as exc:
                cleanup_errors.append(f"取消 thread 订阅失败: {thread_id}: {exc}")
                continue
            try:
                self._release_service_runtime_lease(thread_id)
            except Exception as exc:
                cleanup_errors.append(f"释放 runtime lease 失败: {thread_id}: {exc}")
        for thread_id in sorted(
            set(release_only_thread_ids) - set(unique_unsubscribe_thread_ids)
        ):
            try:
                self._release_service_runtime_lease(thread_id)
            except Exception as exc:
                cleanup_errors.append(f"释放 runtime lease 失败: {thread_id}: {exc}")

    @staticmethod
    def _error_result(
        *,
        operation: str,
        thread_id: str,
        summary: ThreadSummary | None,
        snapshot: dict[str, Any] | None,
        live_runtime_owner: dict[str, str],
        error: Exception,
    ) -> ThreadLifecycleResult:
        legacy_transport_error = isinstance(error, CodexRpcError) and str(
            error.error.get("message", "") or ""
        ) in {"Codex websocket disconnected", "Codex app-server closed"}
        if (
            isinstance(
                error, (TimeoutError, CodexRpcTransportError, CodexRpcProtocolError)
            )
            or legacy_transport_error
        ):
            outcome = UPSTREAM_OUTCOME_UNKNOWN
        elif isinstance(error, CodexRpcError):
            outcome = UPSTREAM_OUTCOME_ERROR
        else:
            raise error
        detail = str(error) or type(error).__name__
        snapshot = snapshot or {}
        return {
            "operation": operation,
            "thread_id": thread_id,
            "thread_title": summary.title if summary is not None else "",
            "working_dir": summary.cwd if summary is not None else "",
            "bound_binding_ids": list(snapshot.get("bound_binding_ids") or []),
            "attached_binding_ids": list(snapshot.get("attached_binding_ids") or []),
            "detached_binding_ids": list(snapshot.get("detached_binding_ids") or []),
            "cleared_binding_ids": [],
            "live_runtime_owner": live_runtime_owner,
            "upstream_outcome": outcome,
            "upstream_error": detail if outcome == UPSTREAM_OUTCOME_ERROR else "",
            "outcome_detail": detail if outcome == UPSTREAM_OUTCOME_UNKNOWN else "",
            "focus_cleanup": FOCUS_CLEANUP_SKIPPED,
            "cleanup_errors": [],
        }
