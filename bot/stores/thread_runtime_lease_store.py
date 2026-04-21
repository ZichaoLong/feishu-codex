"""
Machine-level live thread runtime lease store.

The lease records which instance currently holds live backend residency for a
thread. Multiple holders from the same instance/backend are allowed; holders
from different instances are rejected.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator

from bot.instance_layout import global_data_dir


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLeaseHolder:
    holder_id: str
    holder_type: str
    instance_name: str
    owner_pid: int
    owner_service_token: str
    control_socket_path: str
    backend_url: str
    updated_at: float


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLease:
    thread_id: str
    owner_instance: str
    owner_service_token: str
    control_socket_path: str
    backend_url: str
    attached_at: float
    holders: tuple[ThreadRuntimeLeaseHolder, ...]


@dataclass(frozen=True, slots=True)
class ThreadRuntimeLeaseAcquireResult:
    granted: bool
    acquired: bool
    lease: ThreadRuntimeLease | None


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
            if lease is None:
                if normalized_thread_id in data:
                    data.pop(normalized_thread_id, None)
                    self._write_all_unlocked(data)
                return None
            cleaned = self._serialize_lease(lease)
            if raw != cleaned:
                data[normalized_thread_id] = cleaned
                self._write_all_unlocked(data)
            return lease

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
            current = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
            if current is None:
                lease = ThreadRuntimeLease(
                    thread_id=normalized_thread_id,
                    owner_instance=normalized_holder.instance_name,
                    owner_service_token=normalized_holder.owner_service_token,
                    control_socket_path=normalized_holder.control_socket_path,
                    backend_url=normalized_holder.backend_url,
                    attached_at=normalized_holder.updated_at,
                    holders=(normalized_holder,),
                )
                data[normalized_thread_id] = self._serialize_lease(lease)
                self._write_all_unlocked(data)
                return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=True, lease=lease)
            if current.owner_instance != normalized_holder.instance_name:
                return ThreadRuntimeLeaseAcquireResult(granted=False, acquired=False, lease=current)
            holders = {item.holder_id: item for item in current.holders}
            acquired = normalized_holder.holder_id not in holders
            holders[normalized_holder.holder_id] = normalized_holder
            ordered_holders = tuple(sorted(holders.values(), key=lambda item: item.holder_id))
            lease = ThreadRuntimeLease(
                thread_id=normalized_thread_id,
                owner_instance=current.owner_instance,
                owner_service_token=normalized_holder.owner_service_token or current.owner_service_token,
                control_socket_path=normalized_holder.control_socket_path or current.control_socket_path,
                backend_url=normalized_holder.backend_url or current.backend_url,
                attached_at=current.attached_at or normalized_holder.updated_at,
                holders=ordered_holders,
            )
            data[normalized_thread_id] = self._serialize_lease(lease)
            self._write_all_unlocked(data)
            return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=acquired, lease=lease)

    def release(self, thread_id: str, holder_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_holder_id = str(holder_id or "").strip()
        if not normalized_thread_id or not normalized_holder_id:
            return False
        with self._locked_data() as data:
            lease = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
            if lease is None:
                if normalized_thread_id in data:
                    data.pop(normalized_thread_id, None)
                    self._write_all_unlocked(data)
                return False
            holders = {item.holder_id: item for item in lease.holders}
            if normalized_holder_id not in holders:
                return False
            holders.pop(normalized_holder_id, None)
            if not holders:
                data.pop(normalized_thread_id, None)
            else:
                retained = tuple(sorted(holders.values(), key=lambda item: item.holder_id))
                first = retained[0]
                data[normalized_thread_id] = self._serialize_lease(
                    ThreadRuntimeLease(
                        thread_id=normalized_thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_socket_path=first.control_socket_path,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    )
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
            lease = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
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
                data.pop(normalized_thread_id, None)
            else:
                first = retained[0]
                data[normalized_thread_id] = self._serialize_lease(
                    ThreadRuntimeLease(
                        thread_id=normalized_thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_socket_path=first.control_socket_path,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    )
                )
            self._write_all_unlocked(data)
        return True

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    data = self._read_all_unlocked()
                    if self._prune_stale_leases(data):
                        self._write_all_unlocked(data)
                    yield data
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _prune_stale_leases(self, data: dict[str, dict]) -> bool:
        changed = False
        for thread_id in list(data):
            lease = self._lease_from_data(thread_id, data.get(thread_id))
            if lease is None:
                data.pop(thread_id, None)
                changed = True
                continue
            cleaned = self._serialize_lease(lease)
            if data.get(thread_id) != cleaned:
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
            control_socket_path=str(holder.control_socket_path or "").strip(),
            backend_url=str(holder.backend_url or "").strip(),
            updated_at=float(holder.updated_at or time.time()),
        )

    def _lease_from_data(self, thread_id: str, raw: object) -> ThreadRuntimeLease | None:
        if not isinstance(raw, dict):
            return None
        holders_raw = raw.get("holders")
        if not isinstance(holders_raw, list):
            return None
        holders: list[ThreadRuntimeLeaseHolder] = []
        for item in holders_raw:
            holder = self._holder_from_data(item)
            if holder is None:
                continue
            if not _process_exists(holder.owner_pid):
                continue
            holders.append(holder)
        if not holders:
            return None
        holders.sort(key=lambda item: item.holder_id)
        owner_instance = holders[0].instance_name
        if any(holder.instance_name != owner_instance for holder in holders):
            return None
        attached_at = raw.get("attached_at")
        try:
            normalized_attached_at = float(attached_at or holders[0].updated_at)
        except (TypeError, ValueError):
            normalized_attached_at = holders[0].updated_at
        return ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance=owner_instance,
            owner_service_token=str(raw.get("owner_service_token", "") or "").strip() or holders[0].owner_service_token,
            control_socket_path=str(raw.get("control_socket_path", "") or "").strip() or holders[0].control_socket_path,
            backend_url=str(raw.get("backend_url", "") or "").strip() or holders[0].backend_url,
            attached_at=normalized_attached_at,
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
            control_socket_path = str(raw.get("control_socket_path", "") or "").strip()
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
            control_socket_path=control_socket_path,
            backend_url=backend_url,
            updated_at=updated_at or time.time(),
        )

    @staticmethod
    def _serialize_lease(lease: ThreadRuntimeLease) -> dict[str, object]:
        return {
            "thread_id": lease.thread_id,
            "owner_instance": lease.owner_instance,
            "owner_service_token": lease.owner_service_token,
            "control_socket_path": lease.control_socket_path,
            "backend_url": lease.backend_url,
            "attached_at": lease.attached_at,
            "holders": [asdict(holder) for holder in lease.holders],
        }

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
            self._normalize_thread_id(key): value
            for key, value in raw.items()
            if self._normalize_thread_id(key)
        }

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        rendered = {
            str(key): value
            for key, value in sorted(data.items())
        }
        tmp_path.write_text(json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
