"""
Machine-level live thread runtime lease store.

The lease records which instance currently holds live backend residency for a
thread. Multiple holders from the same instance/backend are allowed; holders
from different instances are rejected.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator

from bot.file_lock import acquire_file_lock, release_file_lock
from bot.instance_layout import global_data_dir
from bot.process_utils import process_exists

_TRANSFER_RESERVATION_TTL_SECONDS = 8.0


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLeaseHolder:
    holder_id: str
    holder_type: str
    instance_name: str
    owner_pid: int
    owner_service_token: str
    control_endpoint: str
    backend_url: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLease:
    thread_id: str
    owner_instance: str
    owner_service_token: str
    control_endpoint: str
    backend_url: str
    attached_at: float
    holders: tuple[ThreadRuntimeLeaseHolder, ...]


@dataclass(frozen=True, slots=True)
class ThreadRuntimeTransferReservation:
    thread_id: str
    owner_instance: str
    owner_service_token: str
    target_instance: str
    target_service_token: str
    reserved_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLeaseAcquireResult:
    granted: bool
    acquired: bool
    lease: ThreadRuntimeLease | None
    transfer: ThreadRuntimeTransferReservation | None = None


class ThreadRuntimeLeaseStore:
    def __init__(self, root_dir: pathlib.Path | None = None) -> None:
        self._root_dir = pathlib.Path(root_dir) if root_dir is not None else global_data_dir()
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._root_dir / "thread_runtime_leases.json"

    def _lock_path(self) -> pathlib.Path:
        return self._root_dir / "thread_runtime_leases.lock"

    def load(self, thread_id: str) -> ThreadRuntimeLease | None:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return None
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            cleaned = self._serialize_entry(lease, transfer)
            if cleaned is None:
                if normalized_thread_id in data:
                    data.pop(normalized_thread_id, None)
                    self._write_all_unlocked(data)
                return None
            if raw != cleaned:
                if cleaned is None:
                    data.pop(normalized_thread_id, None)
                else:
                    data[normalized_thread_id] = cleaned
                self._write_all_unlocked(data)
            return lease

    def load_transfer_reservation(self, thread_id: str) -> ThreadRuntimeTransferReservation | None:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return None
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            cleaned = self._serialize_entry(lease, transfer)
            if cleaned is None:
                if normalized_thread_id in data:
                    data.pop(normalized_thread_id, None)
                    self._write_all_unlocked(data)
                return None
            if raw != cleaned:
                data[normalized_thread_id] = cleaned
                self._write_all_unlocked(data)
            return transfer

    def acquire(
        self,
        thread_id: str,
        holder: ThreadRuntimeLeaseHolder,
    ) -> ThreadRuntimeLeaseAcquireResult:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        normalized_holder = self._normalize_holder(holder)
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            current = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            if transfer is not None and not self._transfer_matches_holder(transfer, normalized_holder):
                return ThreadRuntimeLeaseAcquireResult(
                    granted=False,
                    acquired=False,
                    lease=current,
                    transfer=transfer,
                )
            if current is None:
                lease = ThreadRuntimeLease(
                    thread_id=normalized_thread_id,
                    owner_instance=normalized_holder.instance_name,
                    owner_service_token=normalized_holder.owner_service_token,
                    control_endpoint=normalized_holder.control_endpoint,
                    backend_url=normalized_holder.backend_url,
                    attached_at=normalized_holder.updated_at,
                    holders=(normalized_holder,),
                )
                data[normalized_thread_id] = self._serialize_entry(lease, None)
                self._write_all_unlocked(data)
                return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=True, lease=lease, transfer=None)
            if current.owner_instance != normalized_holder.instance_name:
                return ThreadRuntimeLeaseAcquireResult(
                    granted=False,
                    acquired=False,
                    lease=current,
                    transfer=transfer,
                )
            holders = {item.holder_id: item for item in current.holders}
            acquired = normalized_holder.holder_id not in holders
            holders[normalized_holder.holder_id] = normalized_holder
            ordered_holders = tuple(sorted(holders.values(), key=lambda item: item.holder_id))
            lease = ThreadRuntimeLease(
                thread_id=normalized_thread_id,
                owner_instance=current.owner_instance,
                owner_service_token=normalized_holder.owner_service_token or current.owner_service_token,
                control_endpoint=normalized_holder.control_endpoint or current.control_endpoint,
                backend_url=normalized_holder.backend_url or current.backend_url,
                attached_at=current.attached_at or normalized_holder.updated_at,
                holders=ordered_holders,
            )
            data[normalized_thread_id] = self._serialize_entry(lease, None)
            self._write_all_unlocked(data)
            return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=acquired, lease=lease, transfer=None)

    def reserve_transfer(
        self,
        thread_id: str,
        *,
        owner_instance: str,
        owner_service_token: str,
        target_instance: str,
        target_service_token: str,
        ttl_seconds: float = _TRANSFER_RESERVATION_TTL_SECONDS,
    ) -> ThreadRuntimeTransferReservation:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_owner_instance = str(owner_instance or "").strip().lower()
        normalized_owner_token = str(owner_service_token or "").strip()
        normalized_target_instance = str(target_instance or "").strip().lower()
        normalized_target_token = str(target_service_token or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        if not normalized_owner_instance or not normalized_owner_token:
            raise ValueError("owner 信息不能为空。")
        if not normalized_target_instance or not normalized_target_token:
            raise ValueError("target 信息不能为空。")
        now = time.time()
        reservation = ThreadRuntimeTransferReservation(
            thread_id=normalized_thread_id,
            owner_instance=normalized_owner_instance,
            owner_service_token=normalized_owner_token,
            target_instance=normalized_target_instance,
            target_service_token=normalized_target_token,
            reserved_at=now,
            expires_at=now + max(float(ttl_seconds), 0.1),
        )
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            current_transfer = self._transfer_from_data(normalized_thread_id, raw)
            if lease is None:
                raise ValueError("当前没有可转移的 live runtime owner。")
            if (
                lease.owner_instance != normalized_owner_instance
                or lease.owner_service_token != normalized_owner_token
            ):
                raise ValueError("当前线程 owner 已变化，请重试。")
            if current_transfer is not None and not self._same_transfer_target(current_transfer, reservation):
                raise ValueError("当前线程已有其他 live runtime 转移进行中。")
            data[normalized_thread_id] = self._serialize_entry(lease, reservation)
            self._write_all_unlocked(data)
        return reservation

    def clear_transfer_reservation(
        self,
        thread_id: str,
        *,
        target_instance: str = "",
        target_service_token: str = "",
    ) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_target_instance = str(target_instance or "").strip().lower()
        normalized_target_token = str(target_service_token or "").strip()
        if not normalized_thread_id:
            return False
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            if transfer is None:
                return False
            if normalized_target_instance and transfer.target_instance != normalized_target_instance:
                return False
            if normalized_target_token and transfer.target_service_token != normalized_target_token:
                return False
            cleaned = self._serialize_entry(lease, None)
            if cleaned is None:
                data.pop(normalized_thread_id, None)
            else:
                data[normalized_thread_id] = cleaned
            self._write_all_unlocked(data)
            return True

    def release(self, thread_id: str, holder_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_holder_id = str(holder_id or "").strip()
        if not normalized_thread_id or not normalized_holder_id:
            return False
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            if lease is None:
                cleaned = self._serialize_entry(None, transfer)
                if cleaned is None:
                    if normalized_thread_id in data:
                        data.pop(normalized_thread_id, None)
                        self._write_all_unlocked(data)
                else:
                    data[normalized_thread_id] = cleaned
                    self._write_all_unlocked(data)
                return False
            holders = {item.holder_id: item for item in lease.holders}
            if normalized_holder_id not in holders:
                return False
            holders.pop(normalized_holder_id, None)
            if not holders:
                cleaned = self._serialize_entry(None, transfer)
                if cleaned is None:
                    data.pop(normalized_thread_id, None)
                else:
                    data[normalized_thread_id] = cleaned
            else:
                retained = tuple(sorted(holders.values(), key=lambda item: item.holder_id))
                first = retained[0]
                data[normalized_thread_id] = self._serialize_entry(
                    ThreadRuntimeLease(
                        thread_id=normalized_thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_endpoint=first.control_endpoint,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    ),
                    transfer,
                )
            self._write_all_unlocked(data)
        return True

    def purge_instance(
        self,
        thread_id: str,
        *,
        instance_name: str,
        owner_service_token: str | None = None,
    ) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_instance_name = str(instance_name or "").strip().lower()
        normalized_token = str(owner_service_token or "").strip()
        if not normalized_thread_id or not normalized_instance_name:
            return False
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            transfer = self._transfer_from_data(normalized_thread_id, raw)
            if lease is None:
                return False
            retained = tuple(
                holder
                for holder in lease.holders
                if not (
                    holder.instance_name == normalized_instance_name
                    and (not normalized_token or holder.owner_service_token == normalized_token)
                )
            )
            if len(retained) == len(lease.holders):
                return False
            if not retained:
                cleaned = self._serialize_entry(None, transfer)
                if cleaned is None:
                    data.pop(normalized_thread_id, None)
                else:
                    data[normalized_thread_id] = cleaned
            else:
                first = retained[0]
                data[normalized_thread_id] = self._serialize_entry(
                    ThreadRuntimeLease(
                        thread_id=normalized_thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_endpoint=first.control_endpoint,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    ),
                    transfer,
                )
            self._write_all_unlocked(data)
        return True

    def purge_all_for_instance(
        self,
        *,
        instance_name: str,
        owner_service_token: str | None = None,
    ) -> list[str]:
        normalized_instance_name = str(instance_name or "").strip().lower()
        normalized_token = str(owner_service_token or "").strip()
        if not normalized_instance_name:
            return []
        removed_thread_ids: list[str] = []
        with self._locked_data() as data:
            changed = False
            for thread_id in list(data):
                raw = data.get(thread_id)
                lease = self._lease_from_data(thread_id, raw)
                transfer = self._transfer_from_data(thread_id, raw)
                if lease is None and transfer is None:
                    continue
                matched = False
                retained: tuple[ThreadRuntimeLeaseHolder, ...] = ()
                if lease is not None:
                    retained = tuple(
                        holder
                        for holder in lease.holders
                        if not (
                            holder.instance_name == normalized_instance_name
                            and (not normalized_token or holder.owner_service_token == normalized_token)
                        )
                    )
                    matched = len(retained) != len(lease.holders)
                cleared_transfer = transfer
                if transfer is not None and (
                    transfer.owner_instance == normalized_instance_name
                    or transfer.target_instance == normalized_instance_name
                ) and (not normalized_token or transfer.owner_service_token == normalized_token or transfer.target_service_token == normalized_token):
                    cleared_transfer = None
                    matched = True
                if not matched:
                    continue
                removed_thread_ids.append(thread_id)
                changed = True
                if lease is None or not retained:
                    cleaned = self._serialize_entry(None, cleared_transfer)
                    if cleaned is None:
                        data.pop(thread_id, None)
                    else:
                        data[thread_id] = cleaned
                    continue
                first = retained[0]
                data[thread_id] = self._serialize_entry(
                    ThreadRuntimeLease(
                        thread_id=thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_endpoint=first.control_endpoint,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    ),
                    cleared_transfer,
                )
            if changed:
                self._write_all_unlocked(data)
        return removed_thread_ids

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    data = self._read_all_unlocked()
                    if self._prune_stale_leases(data):
                        self._write_all_unlocked(data)
                    yield data
                finally:
                    release_file_lock(lock_file)

    def _prune_stale_leases(self, data: dict[str, dict]) -> bool:
        changed = False
        for thread_id in list(data):
            raw = data.get(thread_id)
            lease = self._lease_from_data(thread_id, raw)
            transfer = self._transfer_from_data(thread_id, raw)
            cleaned = self._serialize_entry(lease, transfer)
            if cleaned is None:
                data.pop(thread_id, None)
                changed = True
                continue
            if raw != cleaned:
                data[thread_id] = cleaned
                changed = True
        return changed

    @staticmethod
    def _normalize_thread_id(thread_id: str) -> str:
        return str(thread_id or "").strip()

    @staticmethod
    def _normalize_holder(holder: ThreadRuntimeLeaseHolder) -> ThreadRuntimeLeaseHolder:
        return ThreadRuntimeLeaseHolder(
            holder_id=str(holder.holder_id or "").strip(),
            holder_type=str(holder.holder_type or "").strip() or "unknown",
            instance_name=str(holder.instance_name or "").strip().lower(),
            owner_pid=int(holder.owner_pid or 0),
            owner_service_token=str(holder.owner_service_token or "").strip(),
            control_endpoint=str(holder.control_endpoint or "").strip(),
            backend_url=str(holder.backend_url or "").strip(),
            updated_at=float(holder.updated_at or time.time()),
        )

    def _lease_from_data(self, thread_id: str, raw: object) -> ThreadRuntimeLease | None:
        if not isinstance(raw, dict):
            return None
        holders_raw = raw.get("holders")
        if not isinstance(holders_raw, list) or not holders_raw:
            return None
        holders: list[ThreadRuntimeLeaseHolder] = []
        for item in holders_raw:
            holder = self._holder_from_data(item)
            if holder is None:
                continue
            if holder.owner_pid > 0 and not process_exists(holder.owner_pid):
                continue
            holders.append(holder)
        if not holders:
            return None
        holders = sorted(holders, key=lambda item: item.holder_id)
        first = holders[0]
        try:
            attached_at = float(raw.get("attached_at") or 0.0)
        except (TypeError, ValueError):
            attached_at = first.updated_at
        return ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance=str(raw.get("owner_instance") or first.instance_name).strip().lower() or first.instance_name,
            owner_service_token=str(raw.get("owner_service_token") or first.owner_service_token).strip()
            or first.owner_service_token,
            control_endpoint=str(raw.get("control_endpoint") or first.control_endpoint).strip() or first.control_endpoint,
            backend_url=str(raw.get("backend_url") or first.backend_url).strip() or first.backend_url,
            attached_at=attached_at or first.updated_at,
            holders=tuple(holders),
        )

    @staticmethod
    def _holder_from_data(raw: object) -> ThreadRuntimeLeaseHolder | None:
        if not isinstance(raw, dict):
            return None
        try:
            holder_id = str(raw.get("holder_id", "") or "").strip()
            holder_type = str(raw.get("holder_type", "") or "").strip() or "unknown"
            instance_name = str(raw.get("instance_name", "") or "").strip().lower()
            owner_pid = int(raw.get("owner_pid") or 0)
            owner_service_token = str(raw.get("owner_service_token", "") or "").strip()
            control_endpoint = str(raw.get("control_endpoint", "") or "").strip()
            backend_url = str(raw.get("backend_url", "") or "").strip()
            updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if not holder_id or not instance_name or not owner_service_token:
            return None
        return ThreadRuntimeLeaseHolder(
            holder_id=holder_id,
            holder_type=holder_type,
            instance_name=instance_name,
            owner_pid=owner_pid,
            owner_service_token=owner_service_token,
            control_endpoint=control_endpoint,
            backend_url=backend_url,
            updated_at=updated_at or time.time(),
        )

    def _transfer_from_data(self, thread_id: str, raw: object) -> ThreadRuntimeTransferReservation | None:
        if not isinstance(raw, dict):
            return None
        payload = raw.get("transfer")
        if not isinstance(payload, dict):
            return None
        try:
            owner_instance = str(payload.get("owner_instance", "") or "").strip().lower()
            owner_service_token = str(payload.get("owner_service_token", "") or "").strip()
            target_instance = str(payload.get("target_instance", "") or "").strip().lower()
            target_service_token = str(payload.get("target_service_token", "") or "").strip()
            reserved_at = float(payload.get("reserved_at") or 0.0)
            expires_at = float(payload.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if (
            not owner_instance
            or not owner_service_token
            or not target_instance
            or not target_service_token
        ):
            return None
        now = time.time()
        if expires_at <= now:
            return None
        return ThreadRuntimeTransferReservation(
            thread_id=thread_id,
            owner_instance=owner_instance,
            owner_service_token=owner_service_token,
            target_instance=target_instance,
            target_service_token=target_service_token,
            reserved_at=reserved_at or now,
            expires_at=expires_at,
        )

    @staticmethod
    def _serialize_lease(lease: ThreadRuntimeLease) -> dict[str, object]:
        return {
            "thread_id": lease.thread_id,
            "owner_instance": lease.owner_instance,
            "owner_service_token": lease.owner_service_token,
            "control_endpoint": lease.control_endpoint,
            "backend_url": lease.backend_url,
            "attached_at": lease.attached_at,
            "holders": [asdict(holder) for holder in lease.holders],
        }

    @classmethod
    def _serialize_entry(
        cls,
        lease: ThreadRuntimeLease | None,
        transfer: ThreadRuntimeTransferReservation | None,
    ) -> dict[str, object] | None:
        if lease is None and transfer is None:
            return None
        payload: dict[str, object] = {}
        if lease is not None:
            payload.update(cls._serialize_lease(lease))
        else:
            payload["thread_id"] = transfer.thread_id
        if transfer is not None:
            payload["transfer"] = asdict(transfer)
        return payload

    @staticmethod
    def _transfer_matches_holder(
        transfer: ThreadRuntimeTransferReservation,
        holder: ThreadRuntimeLeaseHolder,
    ) -> bool:
        return (
            transfer.target_instance == holder.instance_name
            and transfer.target_service_token == holder.owner_service_token
        )

    @staticmethod
    def _same_transfer_target(
        current: ThreadRuntimeTransferReservation,
        expected: ThreadRuntimeTransferReservation,
    ) -> bool:
        return (
            current.target_instance == expected.target_instance
            and current.target_service_token == expected.target_service_token
            and current.owner_instance == expected.owner_instance
            and current.owner_service_token == expected.owner_service_token
        )

    def _read_all_unlocked(self) -> dict[str, dict]:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(thread_id).strip(): value
            for thread_id, value in raw.items()
            if str(thread_id).strip() and isinstance(value, dict)
        }

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
