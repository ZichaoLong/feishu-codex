"""
Cross-instance live thread runtime coordination helpers.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

from bot.service_control_plane import ServiceControlError, control_request
from bot.stores.instance_registry_store import InstanceRegistryEntry, InstanceRegistryStore
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLease,
    ThreadRuntimeLeaseAcquireResult,
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)


@dataclass(frozen=True, slots=True)
class ThreadRuntimeAcquireOutcome:
    result: ThreadRuntimeLeaseAcquireResult
    transferred_from: str = ""


def build_runtime_lease_conflict_message(
    lease: ThreadRuntimeLease | None,
    *,
    reason: str = "",
) -> str:
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
    if current is None:
        raise RuntimeError("当前无法获取 thread live runtime。")

    owner_entry = _active_owner_entry(current, registry_store=registry_store, lease_store=lease_store, thread_id=thread_id)
    if owner_entry is None:
        retry = lease_store.acquire(thread_id, holder)
        if retry.granted:
            return ThreadRuntimeAcquireOutcome(result=retry)
        current = retry.lease
        raise RuntimeError(build_runtime_lease_conflict_message(current))

    status = _remote_thread_status(owner_entry, thread_id)
    release_available = bool(status.get("release_feishu_runtime_available"))
    if not release_available:
        raise RuntimeError(
            build_runtime_lease_conflict_message(
                current,
                reason=str(status.get("release_feishu_runtime_reason", "") or "").strip(),
            )
        )

    _remote_release_runtime(owner_entry, thread_id)
    retry = lease_store.acquire(thread_id, holder)
    if retry.granted:
        return ThreadRuntimeAcquireOutcome(result=retry, transferred_from=owner_entry.instance_name)
    raise RuntimeError(build_runtime_lease_conflict_message(
        retry.lease,
        reason="owner 实例仍有其他 live subscriber，当前不能自动转移。",
    ))


def _active_owner_entry(
    lease: ThreadRuntimeLease,
    *,
    registry_store: InstanceRegistryStore,
    lease_store: ThreadRuntimeLeaseStore,
    thread_id: str,
) -> InstanceRegistryEntry | None:
    owner = registry_store.load(lease.owner_instance)
    if owner is None:
        lease_store.purge_instance(thread_id, instance_name=lease.owner_instance)
        return None
    if owner.service_token != lease.owner_service_token:
        lease_store.purge_instance(
            thread_id,
            instance_name=lease.owner_instance,
            owner_service_token=lease.owner_service_token,
        )
        return None
    return owner


def _remote_thread_status(owner: InstanceRegistryEntry, thread_id: str) -> dict:
    try:
        return control_request(pathlib.Path(owner.data_dir), "thread/status", {"thread_id": thread_id})
    except ServiceControlError as exc:
        raise RuntimeError(f"无法查询 owner 实例 `{owner.instance_name}` 的线程状态：{exc}") from exc


def _remote_release_runtime(owner: InstanceRegistryEntry, thread_id: str) -> dict:
    try:
        return control_request(
            pathlib.Path(owner.data_dir),
            "thread/release-feishu-runtime",
            {"thread_id": thread_id},
        )
    except ServiceControlError as exc:
        raise RuntimeError(f"无法释放 owner 实例 `{owner.instance_name}` 的 Feishu runtime：{exc}") from exc
