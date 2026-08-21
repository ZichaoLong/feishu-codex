"""Machine-visible service and thread-runtime authority coordination."""

from __future__ import annotations

import logging
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.reason_codes import (
    LIFECYCLE_BLOCKED_BY_LOADED_THREAD,
    LIFECYCLE_BLOCKED_BY_RUNTIME_UNVERIFIED,
    ReasonedCheck,
)
from bot.service_control_plane import ServiceControlPlane, control_request
from bot.stores.app_server_runtime_store import AppServerRuntimeStore
from bot.stores.feishu_app_connection_lease import FeishuAppConnectionLease
from bot.stores.instance_registry_store import (
    InstanceRegistryEntry,
    InstanceRegistryStore,
    build_instance_registry_entry,
)
from bot.stores.service_instance_lease import ServiceInstanceLease
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)
from bot.thread_runtime_coordination import (
    MANAGED_LOADED_INVENTORY_TOTAL_TIMEOUT_SECONDS,
    ManagedInstanceLoadedThreadInventory,
    ManagedLoadedThreadInventorySnapshot,
    ThreadGlobalLoadedGatePreview,
    ThreadRuntimeAdmissionError,
    acquire_thread_runtime_holder_or_raise,
    inspect_thread_global_loaded_presence,
    preview_thread_global_loaded_gate,
    preview_thread_runtime_holder_acquire,
)


logger = logging.getLogger("bot.focus_runtime")
_MANAGED_LOADED_INVENTORY_MAX_WORKERS = 8


@dataclass(frozen=True, slots=True)
class ServiceThreadRuntimePreflight:
    """Exact caller-held evidence for one completed cross-instance check."""

    thread_id: str
    _authority_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ServiceThreadRuntimeLeaseRelease:
    """Opaque capability to release one exact machine service holder."""

    thread_id: str
    _expected_holder: ThreadRuntimeLeaseHolder = field(repr=False)
    _authority_token: object = field(repr=False, compare=False)


def _loaded_inventory_error(exc: Exception) -> str:
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _read_managed_instance_loaded_thread_ids(
    entry: InstanceRegistryEntry,
) -> tuple[str, ...]:
    result = control_request(
        pathlib.Path(entry.data_dir),
        "thread/loaded/list",
        {},
        timeout_seconds=MANAGED_LOADED_INVENTORY_TOTAL_TIMEOUT_SECONDS,
    )
    if type(result) is not dict or set(result) != {
        "instance_name",
        "loaded_thread_ids",
    }:
        raise ValueError("loaded-thread inventory response has an invalid shape")
    instance_name = result.get("instance_name")
    if type(instance_name) is not str or instance_name.strip().lower() != entry.instance_name:
        raise ValueError("loaded-thread inventory response has the wrong instance")
    raw_thread_ids = result.get("loaded_thread_ids")
    if type(raw_thread_ids) is not list:
        raise ValueError("loaded-thread inventory response has invalid thread ids")
    loaded_thread_ids: list[str] = []
    seen: set[str] = set()
    for raw_thread_id in raw_thread_ids:
        if type(raw_thread_id) is not str:
            raise ValueError("loaded-thread inventory response has invalid thread ids")
        thread_id = raw_thread_id.strip()
        if not thread_id or thread_id in seen:
            raise ValueError("loaded-thread inventory response has invalid thread ids")
        seen.add(thread_id)
        loaded_thread_ids.append(thread_id)
    return tuple(sorted(loaded_thread_ids))


class ServiceRuntimeAuthority:
    """Coordinate existing machine-visible owners without mirroring their facts."""

    def __init__(
        self,
        *,
        data_dir: pathlib.Path,
        config_dir: pathlib.Path | None,
        instance_name: str,
        adapter: CodexAppServerAdapter,
        adapter_ingress_gate: AdapterIngressGate,
        app_server_runtime: AppServerRuntimeStore,
        service_instance_lease: ServiceInstanceLease,
        feishu_app_connection_lease: FeishuAppConnectionLease,
        instance_registry: InstanceRegistryStore,
        thread_runtime_lease_store: ThreadRuntimeLeaseStore,
        service_control_plane: ServiceControlPlane,
    ) -> None:
        self._data_dir = data_dir
        self._config_dir = config_dir
        self._instance_name = instance_name
        self._adapter = adapter
        self._adapter_ingress_gate = adapter_ingress_gate
        self._app_server_runtime = app_server_runtime
        self._service_instance_lease = service_instance_lease
        self._feishu_app_connection_lease = feishu_app_connection_lease
        self._instance_registry = instance_registry
        self._thread_runtime_lease_store = thread_runtime_lease_store
        self._service_control_plane = service_control_plane
        self._thread_runtime_preflight_token = object()
        self._thread_runtime_release_token = object()

    def prepare_owned_state(self, app_id: str) -> None:
        self._feishu_app_connection_lease.acquire(
            app_id,
            instance_name=self._instance_name,
        )
        # Refuse a second backend generation until the previously owned process
        # is proved stopped, then clear this instance's process-bound runtime
        # projections before admitting a replacement generation.
        self._app_server_runtime.prepare_for_owned_start()
        self._thread_runtime_lease_store.purge_all_for_instance(
            instance_name=self._instance_name
        )

    def register_instance_runtime(self, *, app_server_url: str | None = None) -> None:
        published_endpoint = (
            self._adapter.current_app_server_url()
            if app_server_url is None
            else str(app_server_url or "").strip()
        )
        if not published_endpoint:
            raise RuntimeError("cannot register an empty app-server endpoint")
        entry = build_instance_registry_entry(
            instance_name=self._instance_name,
            service_token=self._service_instance_lease.owner_token,
            control_endpoint=self._service_control_plane.control_endpoint,
            app_server_url=published_endpoint,
            config_dir=self._config_dir or pathlib.Path(""),
            data_dir=self._data_dir,
        )
        self._instance_registry.register(entry)

    def published_app_server_url(self) -> str:
        """Resolve the sole attached-client endpoint through ingress authority."""

        return self._adapter_ingress_gate.resolve_published_backend_endpoint(
            self._adapter.current_app_server_url,
        )

    def unregister_instance_runtime(self) -> None:
        self._instance_registry.unregister(
            self._instance_name,
            service_token=self._service_instance_lease.owner_token,
        )

    def release_service_authority_after_runtime_barrier(
        self,
        *,
        context: str,
    ) -> None:
        """Release machine authority after RuntimeLoop.stop() has returned."""

        service_token = self._service_instance_lease.owner_token
        failures: list[tuple[str, Exception]] = []
        try:
            self.unregister_instance_runtime()
        except Exception as exc:
            failures.append(("instance registry", exc))
            logger.exception("%s 注销实例注册失败", context)
        try:
            self._thread_runtime_lease_store.release_holders_for_service_generation(
                instance_name=self._instance_name,
                owner_service_token=service_token,
            )
        except Exception as exc:
            failures.append(("thread runtime holders", exc))
            logger.exception("%s 清理 thread runtime holder 失败", context)
        try:
            self._feishu_app_connection_lease.release()
        except Exception as exc:
            failures.append(("Feishu app connection lease", exc))
            logger.exception("%s 释放 Feishu app connection lease 失败", context)
        if failures:
            # The stores are machine-visible ownership projections. A stale
            # record normally denies takeover, but that is not proof that the
            # release transaction completed. Keep the authoritative service
            # lease so lifecycle.stop() can retry this exact stage instead of
            # reporting CLOSED with only best-effort cleanup.
            detail = "; ".join(f"{label}: {error}" for label, error in failures)
            raise RuntimeError(
                f"{context} could not release all machine authority ({detail})"
            ) from failures[0][1]
        self._service_instance_lease.release()

    def service_thread_runtime_holder(self) -> ThreadRuntimeLeaseHolder:
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

    def fcodex_runtime_holder(self, participant_id: str) -> ThreadRuntimeLeaseHolder:
        """Return this service process's runtime holder for one participant."""

        normalized_participant_id = str(participant_id or "").strip()
        if not normalized_participant_id.startswith("fcodex:"):
            raise ValueError("fcodex runtime holder 缺少合法 participant id。")
        return ThreadRuntimeLeaseHolder(
            holder_id=normalized_participant_id,
            holder_type="fcodex",
            instance_name=self._instance_name,
            owner_pid=os.getpid(),
            owner_service_token=self._service_instance_lease.owner_token,
            control_endpoint=self._service_control_plane.control_endpoint,
            backend_url=self._adapter.current_app_server_url(),
            updated_at=time.time(),
        )

    def cross_instance_loaded_gate_check(
        self,
        thread_id: str,
    ) -> ThreadGlobalLoadedGatePreview:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ThreadGlobalLoadedGatePreview(allowed=True)
        return preview_thread_global_loaded_gate(
            thread_id=normalized_thread_id,
            current_instance_name=self._instance_name,
            registry_store=self._instance_registry,
        )

    def managed_loaded_thread_inventory(
        self,
    ) -> ManagedLoadedThreadInventorySnapshot:
        """Read each other managed instance once for one advisory Web view."""

        try:
            entries = tuple(
                entry
                for entry in self._instance_registry.list_instances()
                if entry.instance_name != self._instance_name
            )
        except Exception as exc:
            return ManagedLoadedThreadInventorySnapshot(
                registry_error=_loaded_inventory_error(exc),
            )
        if not entries:
            return ManagedLoadedThreadInventorySnapshot()

        inventories: list[ManagedInstanceLoadedThreadInventory] = []
        worker_count = min(
            len(entries),
            _MANAGED_LOADED_INVENTORY_MAX_WORKERS,
        )
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="focus-loaded-inventory",
        )
        try:
            futures = {
                entry.instance_name: executor.submit(
                    _read_managed_instance_loaded_thread_ids,
                    entry,
                )
                for entry in entries
            }
            completed, _pending = wait(
                futures.values(),
                timeout=MANAGED_LOADED_INVENTORY_TOTAL_TIMEOUT_SECONDS,
            )
            for entry in sorted(entries, key=lambda item: item.instance_name):
                future = futures[entry.instance_name]
                if future not in completed:
                    inventories.append(
                        ManagedInstanceLoadedThreadInventory(
                            instance_name=entry.instance_name,
                            error=(
                                "TimeoutError: managed loaded-thread inventory "
                                "exceeded its total timeout"
                            ),
                        )
                    )
                    continue
                try:
                    loaded_thread_ids = future.result()
                except Exception as exc:
                    inventories.append(
                        ManagedInstanceLoadedThreadInventory(
                            instance_name=entry.instance_name,
                            error=_loaded_inventory_error(exc),
                        )
                    )
                else:
                    inventories.append(
                        ManagedInstanceLoadedThreadInventory(
                            instance_name=entry.instance_name,
                            loaded_thread_ids=loaded_thread_ids,
                        )
                    )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return ManagedLoadedThreadInventorySnapshot(instances=tuple(inventories))

    def detached_runtime_attach_check(self, thread_id: str) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        loaded_gate = self.cross_instance_loaded_gate_check(normalized_thread_id)
        if not loaded_gate.allowed:
            return ReasonedCheck.deny(
                loaded_gate.reason_code,
                loaded_gate.reason_text,
            )
        preview = preview_thread_runtime_holder_acquire(
            thread_id=normalized_thread_id,
            holder=self.service_thread_runtime_holder(),
            lease_store=self._thread_runtime_lease_store,
        )
        if preview.allowed:
            return ReasonedCheck.allow()
        return ReasonedCheck.deny(preview.reason_code, preview.reason_text)

    def lifecycle_loaded_gate_check(
        self,
        thread_id: str,
        operation: str,
    ) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_operation = str(operation or "").strip().lower()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        if normalized_operation not in {"archive", "unarchive", "delete"}:
            return ReasonedCheck.deny(
                LIFECYCLE_BLOCKED_BY_RUNTIME_UNVERIFIED,
                f"未知 lifecycle operation：{operation}",
            )
        presence = inspect_thread_global_loaded_presence(
            thread_id=normalized_thread_id,
            registry_store=self._instance_registry,
            excluded_instance_names=(self._instance_name,),
        )
        if presence.verified_clear:
            return ReasonedCheck.allow()
        action_label = {
            "archive": "归档",
            "unarchive": "恢复归档",
            "delete": "永久删除",
        }[normalized_operation]
        blocking_instance = presence.blocking_instance or "unknown"
        if presence.diagnostic:
            return ReasonedCheck.deny(
                LIFECYCLE_BLOCKED_BY_RUNTIME_UNVERIFIED,
                (
                    f"无法确认运行中的实例 `{blocking_instance}` 是否仍将该 thread 保持为 loaded："
                    f"{presence.diagnostic}。当前按 fail-close 拒绝{action_label}。"
                    f"请先检查该实例，或在确认可丢弃其 live runtime 后执行 "
                    f"`focusctl --instance {blocking_instance} service reset-backend`。"
                ),
            )
        status = presence.blocking_status or "unknown"
        if normalized_operation in {"archive", "delete"}:
            return ReasonedCheck.deny(
                LIFECYCLE_BLOCKED_BY_LOADED_THREAD,
                (
                    f"运行中的实例 `{blocking_instance}` 仍将该 thread 保持为 loaded (`{status}`)；"
                    f"拒绝在当前实例{action_label}。请改在该实例执行对应 lifecycle 命令，"
                    "或在确认可丢弃其 live runtime 后先执行 "
                    f"`focusctl --instance {blocking_instance} service reset-backend`。"
                ),
            )
        return ReasonedCheck.deny(
            LIFECYCLE_BLOCKED_BY_LOADED_THREAD,
            (
                f"运行中的实例 `{blocking_instance}` 仍将该 thread 保持为 loaded (`{status}`)；"
                "拒绝 unarchive。请先关闭相关 Focus/fcodex runtime，"
                "或在确认可丢弃后 reset 对应实例 backend。"
            ),
        )

    def prepare_service_thread_runtime_preflight(
        self,
        thread_id: str,
    ) -> ServiceThreadRuntimePreflight:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id must not be empty")
        loaded_gate = self.cross_instance_loaded_gate_check(normalized_thread_id)
        if not loaded_gate.allowed:
            raise ThreadRuntimeAdmissionError(
                loaded_gate.reason_text,
                blocking_instance=loaded_gate.blocking_instance,
                blocking_status=loaded_gate.blocking_status,
                reason_code=loaded_gate.reason_code,
            )
        return ServiceThreadRuntimePreflight(
            thread_id=normalized_thread_id,
            _authority_token=self._thread_runtime_preflight_token,
        )

    def ensure_service_thread_runtime_lease(
        self,
        thread_id: str,
        *,
        runtime_lease_preflight: ServiceThreadRuntimePreflight | None = None,
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False
        if runtime_lease_preflight is None:
            self.prepare_service_thread_runtime_preflight(normalized_thread_id)
        elif (
            not isinstance(runtime_lease_preflight, ServiceThreadRuntimePreflight)
            or runtime_lease_preflight._authority_token
            is not self._thread_runtime_preflight_token
            or runtime_lease_preflight.thread_id != normalized_thread_id
        ):
            raise ValueError(
                "service thread-runtime preflight does not match this authority"
            )
        outcome = acquire_thread_runtime_holder_or_raise(
            thread_id=normalized_thread_id,
            holder=self.service_thread_runtime_holder(),
            lease_store=self._thread_runtime_lease_store,
        )
        return outcome.acquired

    def release_service_thread_runtime_lease(self, thread_id: str) -> None:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        self._thread_runtime_lease_store.release(
            normalized_thread_id,
            f"service:{self._service_instance_lease.owner_token}",
        )

    def prepare_service_thread_runtime_lease_release(
        self,
        thread_id: str,
    ) -> ServiceThreadRuntimeLeaseRelease | None:
        """Capture this service generation's exact holder from machine state."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return None
        service_token = str(self._service_instance_lease.owner_token or "").strip()
        service_holder_id = f"service:{service_token}"
        instance_name = str(self._instance_name or "").strip().lower()
        lease = self._thread_runtime_lease_store.load(normalized_thread_id)
        if lease is None:
            return None
        expected_holder = next(
            (
                holder
                for holder in lease.holders
                if holder.holder_id == service_holder_id
            ),
            None,
        )
        if expected_holder is None:
            return None
        if (
            expected_holder.holder_type != "service"
            or expected_holder.instance_name != instance_name
            or expected_holder.owner_service_token != service_token
        ):
            raise RuntimeError(
                "current service thread-runtime holder metadata does not match "
                "this service generation"
            )
        return ServiceThreadRuntimeLeaseRelease(
            thread_id=normalized_thread_id,
            _expected_holder=expected_holder,
            _authority_token=self._thread_runtime_release_token,
        )

    def release_prepared_service_thread_runtime_lease(
        self,
        receipt: ServiceThreadRuntimeLeaseRelease,
    ) -> bool:
        """CAS-release only the holder captured by this authority's receipt."""

        if (
            not isinstance(receipt, ServiceThreadRuntimeLeaseRelease)
            or receipt._authority_token is not self._thread_runtime_release_token
        ):
            raise ValueError(
                "service thread-runtime release does not match this authority"
            )
        return self._thread_runtime_lease_store.release_if_matches(
            receipt.thread_id,
            receipt._expected_holder,
        )
