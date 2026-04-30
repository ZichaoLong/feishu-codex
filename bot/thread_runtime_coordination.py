"""
Cross-instance live thread runtime coordination helpers.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from bot.reason_codes import PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER
from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.instance_registry_store import InstanceRegistryEntry, InstanceRegistryStore
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLease,
    ThreadRuntimeLeaseAcquireResult,
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
    ThreadRuntimeTransferReservation,
)


@dataclass(frozen=True, slots=True)
class ThreadRuntimeAcquireOutcome:
    result: ThreadRuntimeLeaseAcquireResult
    transferred_from: str = ""


@dataclass(frozen=True, slots=True)
class ThreadRuntimeAcquirePreview:
    allowed: bool
    auto_transfer: bool = False
    reason_code: str = ""
    reason_text: str = ""
    owner_entry: InstanceRegistryEntry | None = None


def build_runtime_lease_conflict_message(
    lease: ThreadRuntimeLease | None,
    *,
    transfer: ThreadRuntimeTransferReservation | None = None,
    reason: str = "",
) -> str:
    if transfer is not None and lease is None:
        base = (
            "当前线程正处于 live runtime 转移窗口："
            f"`{transfer.owner_instance}` -> `{transfer.target_instance}`。"
        )
        if reason:
            return f"{base} {reason}"
        return f"{base} 请稍后重试。"
    if lease is None:
        return "当前无法获取 thread live runtime。"
    base = f"当前线程正由实例 `{lease.owner_instance}` 持有 live runtime。"
    if reason:
        return f"{base} {reason}"
    return base


def acquire_thread_runtime_holder_or_raise(
    *,
    thread_id: str,
    holder: ThreadRuntimeLeaseHolder,
    lease_store: ThreadRuntimeLeaseStore,
    registry_store: InstanceRegistryStore,
) -> ThreadRuntimeAcquireOutcome:
    result = lease_store.acquire(thread_id, holder)
    if result.granted:
        return ThreadRuntimeAcquireOutcome(result=result)

    current = result.lease
    current_transfer = result.transfer
    if current is None and current_transfer is not None:
        raise RuntimeError(build_runtime_lease_conflict_message(None, transfer=current_transfer))
    if current is None:
        raise RuntimeError("当前无法获取 thread live runtime。")

    preview = preview_thread_runtime_holder_acquire_conflict(
        thread_id=thread_id,
        holder=holder,
        current=current,
        current_transfer=current_transfer,
        registry_store=registry_store,
    )
    if not preview.allowed:
        raise RuntimeError(preview.reason_text)
    owner_entry = preview.owner_entry
    if owner_entry is None:
        raise RuntimeError(build_runtime_lease_conflict_message(current))

    lease_store.reserve_transfer(
        thread_id,
        owner_instance=current.owner_instance,
        owner_service_token=current.owner_service_token,
        target_instance=holder.instance_name,
        target_service_token=holder.owner_service_token,
    )
    try:
        _remote_unsubscribe_thread(owner_entry, thread_id)
    except Exception:
        lease_store.clear_transfer_reservation(
            thread_id,
            target_instance=holder.instance_name,
            target_service_token=holder.owner_service_token,
        )
        raise

    retry = lease_store.acquire(thread_id, holder)
    if retry.granted:
        return ThreadRuntimeAcquireOutcome(result=retry, transferred_from=owner_entry.instance_name)
    lease_store.clear_transfer_reservation(
        thread_id,
        target_instance=holder.instance_name,
        target_service_token=holder.owner_service_token,
    )
    raise RuntimeError(build_runtime_lease_conflict_message(
        retry.lease,
        transfer=retry.transfer,
        reason="owner 实例仍有其他 live subscriber，当前不能自动转移。",
    ))


def preview_thread_runtime_holder_acquire(
    *,
    thread_id: str,
    holder: ThreadRuntimeLeaseHolder,
    lease_store: ThreadRuntimeLeaseStore,
    registry_store: InstanceRegistryStore,
) -> ThreadRuntimeAcquirePreview:
    current = lease_store.load(thread_id)
    current_transfer = lease_store.load_transfer_reservation(thread_id)
    if current is None and current_transfer is not None:
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(None, transfer=current_transfer),
        )
    if current is None or current.owner_instance == holder.instance_name:
        return ThreadRuntimeAcquirePreview(allowed=True)
    return preview_thread_runtime_holder_acquire_conflict(
        thread_id=thread_id,
        holder=holder,
        current=current,
        current_transfer=current_transfer,
        registry_store=registry_store,
    )


def preview_thread_runtime_holder_acquire_conflict(
    *,
    thread_id: str,
    holder: ThreadRuntimeLeaseHolder,
    current: ThreadRuntimeLease,
    current_transfer: ThreadRuntimeTransferReservation | None,
    registry_store: InstanceRegistryStore,
) -> ThreadRuntimeAcquirePreview:
    if current_transfer is not None and not _transfer_matches_holder(current_transfer, holder):
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(None, transfer=current_transfer),
        )

    owner_entry, owner_problem = _registered_owner_entry(current, registry_store=registry_store)
    if owner_entry is None:
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(current, reason=owner_problem),
        )

    if _has_non_service_holders(current):
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=_external_holder_conflict_message(current),
        )

    try:
        owner_status = _remote_owner_thread_status(owner_entry, thread_id)
    except Exception as exc:
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(
                current,
                reason=f"无法确认 owner 实例是否可立即释放 runtime：{exc}",
            ),
        )

    if not owner_status["bound_binding_ids"]:
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(
                current,
                reason=(
                    "owner 实例当前没有 Feishu binding 指向该线程，不能自动转移；"
                    "通常是本地 `fcodex` 仍在使用该线程。"
                ),
            ),
        )

    if not owner_status["unsubscribe_available"]:
        return ThreadRuntimeAcquirePreview(
            allowed=False,
            reason_code=PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            reason_text=build_runtime_lease_conflict_message(
                current,
                reason=f"owner 实例当前不能立即释放 runtime：{owner_status['unsubscribe_reason']}",
            ),
        )

    return ThreadRuntimeAcquirePreview(
        allowed=True,
        auto_transfer=True,
        owner_entry=owner_entry,
    )


def _registered_owner_entry(
    lease: ThreadRuntimeLease,
    *,
    registry_store: InstanceRegistryStore,
) -> tuple[InstanceRegistryEntry | None, str]:
    owner = registry_store.load(lease.owner_instance)
    if owner is None:
        return None, (
            "owner 实例当前未注册，不能自动转移；"
            "请先关闭对应 `fcodex` TUI，或等待 owner 进程退出后再试。"
        )
    if owner.service_token != lease.owner_service_token:
        return None, (
            "记录中的 owner service 已变化，当前不能自动转移；"
            "请先关闭对应 `fcodex` TUI，或等待旧 owner 退出后再试。"
        )
    return owner, ""


def _transfer_matches_holder(
    transfer: ThreadRuntimeTransferReservation,
    holder: ThreadRuntimeLeaseHolder,
) -> bool:
    return (
        transfer.target_instance == holder.instance_name
        and transfer.target_service_token == holder.owner_service_token
    )


def _has_non_service_holders(lease: ThreadRuntimeLease) -> bool:
    return any(holder.holder_type != "service" for holder in lease.holders)


def _external_holder_conflict_message(lease: ThreadRuntimeLease) -> str:
    has_fcodex_holder = any(holder.holder_type == "fcodex" for holder in lease.holders)
    if has_fcodex_holder:
        return (
            f"当前线程正由实例 `{lease.owner_instance}` 的本地 `fcodex` 持有 live runtime；"
            "当前不能自动转移。请先关闭对应 `fcodex` TUI 后再试。"
        )
    return build_runtime_lease_conflict_message(
        lease,
        reason="owner 实例仍有非 service subscriber，当前不能自动转移。",
    )


def _remote_owner_thread_status(owner: InstanceRegistryEntry, thread_id: str) -> dict[str, object]:
    payload = control_request(
        pathlib.Path(owner.data_dir),
        "thread/status",
        {"thread_id": thread_id},
    )
    if not isinstance(payload, dict):
        raise RuntimeError("owner 控制面返回了无效 thread 状态。")
    bound_binding_ids = tuple(
        str(item or "").strip()
        for item in payload.get("bound_binding_ids", ())
        if str(item or "").strip()
    )
    return {
        "bound_binding_ids": bound_binding_ids,
        "unsubscribe_available": bool(payload.get("unsubscribe_available")),
        "unsubscribe_reason": str(payload.get("unsubscribe_reason", "") or "").strip(),
    }

def _remote_unsubscribe_thread(owner: InstanceRegistryEntry, thread_id: str) -> dict:
    try:
        return control_request(
            pathlib.Path(owner.data_dir),
            "thread/unsubscribe",
            {"thread_id": thread_id},
        )
    except ServiceControlError as exc:
        raise RuntimeError(f"无法释放 owner 实例 `{owner.instance_name}` 的 Feishu runtime：{exc}") from exc
