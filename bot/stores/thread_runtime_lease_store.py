"""
Machine-level live thread runtime lease store.

The lease records which instance currently holds live backend residency for a
thread. Multiple holders from the same instance/backend are allowed; holders
from different instances are rejected.
"""

from __future__ import annotations

import math
import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator

from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.instance_layout import global_data_dir
from bot.process_utils import process_exists, process_identity
from bot.stores.versioned_records import (
    VersionedRecordsUnavailable,
    read_versioned_records,
    write_versioned_records,
)


_SCHEMA_VERSION = 1


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
    owner_process_identity: str = ""


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
class ThreadRuntimeLeaseAcquireResult:
    granted: bool
    acquired: bool
    lease: ThreadRuntimeLease | None


class ThreadRuntimeLeaseStoreUnavailable(RuntimeError):
    """The machine-level runtime claim ledger cannot be trusted safely."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        self.path = pathlib.Path(path)
        self.reason = str(reason or "unavailable")
        super().__init__(
            "无法安全读取 thread runtime lease 状态；已按 fail-closed 拒绝协调操作"
            f"（{self.path.name}: {self.reason}）。"
        )


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
            cleaned = self._serialize_entry(lease)
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
                data[normalized_thread_id] = self._serialize_entry(lease)
                self._write_all_unlocked(data)
                return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=True, lease=lease)
            if current.owner_instance != normalized_holder.instance_name:
                return ThreadRuntimeLeaseAcquireResult(granted=False, acquired=False, lease=current)
            if current.owner_service_token != normalized_holder.owner_service_token:
                return ThreadRuntimeLeaseAcquireResult(granted=False, acquired=False, lease=current)
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
            data[normalized_thread_id] = self._serialize_entry(lease)
            self._write_all_unlocked(data)
            return ThreadRuntimeLeaseAcquireResult(granted=True, acquired=acquired, lease=lease)

    def release(self, thread_id: str, holder_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_holder_id = str(holder_id or "").strip()
        if not normalized_thread_id or not normalized_holder_id:
            return False
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
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
                data[normalized_thread_id] = self._serialize_entry(
                    ThreadRuntimeLease(
                        thread_id=normalized_thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_endpoint=first.control_endpoint,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=retained,
                    )
                )
            self._write_all_unlocked(data)
        return True

    def release_if_matches(
        self,
        thread_id: str,
        expected_holder: ThreadRuntimeLeaseHolder,
    ) -> bool:
        """Release only the exact holder record captured by the caller.

        Holder ids are stable across refreshes, so they are not a sufficient
        compare-and-swap key.  The complete normalized holder is compared
        while the machine-store lock is held; a mismatch leaves the current
        lease untouched.
        """

        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        normalized_expected_holder = self._normalize_holder(expected_holder)
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            if lease is None:
                if normalized_thread_id in data:
                    data.pop(normalized_thread_id, None)
                    self._write_all_unlocked(data)
                return False
            holders = {item.holder_id: item for item in lease.holders}
            current_holder = holders.get(normalized_expected_holder.holder_id)
            if current_holder != normalized_expected_holder:
                return False
            holders.pop(normalized_expected_holder.holder_id)
            if not holders:
                data.pop(normalized_thread_id, None)
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
                    )
                )
            self._write_all_unlocked(data)
        return True

    def list(self) -> list[ThreadRuntimeLease]:
        """Return every currently valid machine-level runtime lease.

        This is an authority-store read, not discovery cache inspection.  A
        malformed record therefore fails the whole read closed through
        :class:`ThreadRuntimeLeaseStoreUnavailable` instead of being skipped.
        """

        with self._locked_data() as data:
            leases = [
                self._lease_from_data(thread_id, data.get(thread_id))
                for thread_id in sorted(data)
            ]
        return [lease for lease in leases if lease is not None]

    def release_holders_for_service_generation(
        self,
        *,
        instance_name: str,
        owner_service_token: str,
    ) -> list[str]:
        """Release one stopped generation's runtime authority.

        This cleanup is valid only after that generation's RuntimeLoop exit
        barrier. Every holder is scoped to the service generation that created
        it; ``owner_pid == 0`` does not grant authority to a replacement
        generation.
        """

        normalized_instance_name = str(instance_name or "").strip().lower()
        normalized_service_token = str(owner_service_token or "").strip()
        if not normalized_instance_name or not normalized_service_token:
            return []
        changed_thread_ids: list[str] = []
        with self._locked_data() as data:
            changed = False
            for thread_id in list(data):
                lease = self._lease_from_data(thread_id, data.get(thread_id))
                if lease is None or lease.owner_instance != normalized_instance_name:
                    continue
                retained = tuple(
                    holder
                    for holder in lease.holders
                    if holder.owner_service_token != normalized_service_token
                )
                if len(retained) == len(lease.holders):
                    continue
                changed = True
                changed_thread_ids.append(thread_id)
                if not retained:
                    data.pop(thread_id, None)
                    continue
                ordered = tuple(sorted(retained, key=lambda item: item.holder_id))
                first = ordered[0]
                data[thread_id] = self._serialize_entry(
                    ThreadRuntimeLease(
                        thread_id=thread_id,
                        owner_instance=first.instance_name,
                        owner_service_token=first.owner_service_token,
                        control_endpoint=first.control_endpoint,
                        backend_url=first.backend_url,
                        attached_at=lease.attached_at,
                        holders=ordered,
                    )
                )
            if changed:
                self._write_all_unlocked(data)
        return sorted(changed_thread_ids)

    def purge_instance(
        self,
        thread_id: str,
        *,
        instance_name: str,
    ) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_instance_name = str(instance_name or "").strip().lower()
        if not normalized_thread_id or not normalized_instance_name:
            return False
        with self._locked_data() as data:
            raw = data.get(normalized_thread_id)
            lease = self._lease_from_data(normalized_thread_id, raw)
            clears_legacy_transfer = self._legacy_transfer_touches_instance(raw, normalized_instance_name)
            if lease is None:
                if not clears_legacy_transfer:
                    return False
                data.pop(normalized_thread_id, None)
                self._write_all_unlocked(data)
                return True
            # Purge is instance-scoped cleanup, not token-scoped filtering.
            # Once a live generation explicitly purges an instance, any
            # same-instance holder left under another token is stale residue
            # from an older generation and must be removed as well.
            retained = tuple(
                holder
                for holder in lease.holders
                if holder.instance_name != normalized_instance_name
            )
            if len(retained) == len(lease.holders) and not clears_legacy_transfer:
                return False
            if not retained:
                data.pop(normalized_thread_id, None)
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
                    )
                )
            self._write_all_unlocked(data)
        return True

    def purge_all_for_instance(
        self,
        *,
        instance_name: str,
    ) -> list[str]:
        normalized_instance_name = str(instance_name or "").strip().lower()
        if not normalized_instance_name:
            return []
        removed_thread_ids: list[str] = []
        with self._locked_data() as data:
            changed = False
            for thread_id in list(data):
                raw = data.get(thread_id)
                lease = self._lease_from_data(thread_id, raw)
                clears_legacy_transfer = self._legacy_transfer_touches_instance(
                    raw,
                    normalized_instance_name,
                )
                if lease is None and not clears_legacy_transfer:
                    continue
                matched = False
                retained: tuple[ThreadRuntimeLeaseHolder, ...] = ()
                if lease is not None:
                    retained = tuple(
                        holder
                        for holder in lease.holders
                        if holder.instance_name != normalized_instance_name
                    )
                    matched = len(retained) != len(lease.holders)
                if clears_legacy_transfer:
                    matched = True
                if not matched:
                    continue
                removed_thread_ids.append(thread_id)
                changed = True
                if lease is None or not retained:
                    data.pop(thread_id, None)
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
                    )
                )
            if changed:
                self._write_all_unlocked(data)
        return removed_thread_ids

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._locked_raw_data() as data:
            if self._prune_stale_leases(data):
                self._write_all_unlocked(data)
            yield data

    @contextmanager
    def _locked_raw_data(self) -> Iterator[dict[str, dict]]:
        """Lock and read the canonical file without an intermediate write."""

        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open_lock_file(lock_path) as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    data = self._read_all_unlocked()
                    yield data
                finally:
                    release_file_lock(lock_file)

    def _prune_stale_leases(self, data: dict[str, dict]) -> bool:
        changed = False
        for thread_id in list(data):
            raw = data.get(thread_id)
            lease = self._lease_from_data(thread_id, raw)
            cleaned = self._serialize_entry(lease)
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
        holder_id = str(holder.holder_id or "").strip()
        instance_name = str(holder.instance_name or "").strip().lower()
        owner_pid = int(holder.owner_pid or 0)
        owner_service_token = str(holder.owner_service_token or "").strip()
        updated_at = float(holder.updated_at or time.time())
        if (
            not holder_id
            or not instance_name
            or owner_pid < 0
            or not owner_service_token
            or not math.isfinite(updated_at)
            or updated_at < 0
        ):
            raise ValueError("thread runtime lease holder 字段无效。")
        owner_process_identity = str(holder.owner_process_identity or "").strip()
        if owner_pid > 0 and not owner_process_identity:
            owner_process_identity = process_identity(owner_pid)
        return ThreadRuntimeLeaseHolder(
            holder_id=holder_id,
            holder_type=str(holder.holder_type or "").strip() or "unknown",
            instance_name=instance_name,
            owner_pid=owner_pid,
            owner_service_token=owner_service_token,
            control_endpoint=str(holder.control_endpoint or "").strip(),
            backend_url=str(holder.backend_url or "").strip(),
            updated_at=updated_at,
            owner_process_identity=owner_process_identity,
        )

    def _lease_from_data(self, thread_id: str, raw: object) -> ThreadRuntimeLease | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("lease is not an object")
        required_fields = {
            "thread_id",
            "owner_instance",
            "owner_service_token",
            "control_endpoint",
            "backend_url",
            "attached_at",
            "holders",
        }
        # ``transfer`` is a retired, explicitly recognized version-0 field.
        # It is accepted only so a valid old file can be rewritten once into
        # the current shape; arbitrary future fields are not guessed at.
        if not required_fields.issubset(raw) or not set(raw).issubset(required_fields | {"transfer"}):
            raise ValueError("lease fields do not match schema")
        for field in (
            "thread_id",
            "owner_instance",
            "owner_service_token",
            "control_endpoint",
            "backend_url",
        ):
            if not isinstance(raw.get(field), str):
                raise ValueError("lease contains a non-string field")
        recorded_thread_id = self._normalize_thread_id(raw["thread_id"])
        if not recorded_thread_id or recorded_thread_id != thread_id:
            raise ValueError("thread key does not match lease")
        owner_instance = raw["owner_instance"].strip().lower()
        owner_service_token = raw["owner_service_token"].strip()
        control_endpoint = raw["control_endpoint"].strip()
        backend_url = raw["backend_url"].strip()
        attached_at_raw = raw.get("attached_at")
        if isinstance(attached_at_raw, bool) or not isinstance(attached_at_raw, (int, float)):
            raise ValueError("invalid attached_at")
        attached_at = float(attached_at_raw)
        if (
            not owner_instance
            or not owner_service_token
            or not math.isfinite(attached_at)
            or attached_at < 0
        ):
            raise ValueError("invalid lease owner metadata")
        holders_raw = raw.get("holders")
        if not isinstance(holders_raw, list) or not holders_raw:
            raise ValueError("holders must be a non-empty list")
        parsed_holders: list[ThreadRuntimeLeaseHolder] = []
        holder_ids: set[str] = set()
        for item in holders_raw:
            holder = self._holder_from_data(item)
            if holder.holder_id in holder_ids:
                raise ValueError("duplicate holder id")
            # A small number of version-0 files may contain same-instance
            # holders from two service generations.  The top-level owner
            # token remains the admission authority, so this state still
            # fails closed for the newer generation and can be cleared by an
            # explicit instance purge.  Cross-instance mixing is ambiguous
            # and therefore rejected.
            if holder.instance_name != owner_instance:
                raise ValueError("holder owner does not match lease owner")
            holder_ids.add(holder.holder_id)
            parsed_holders.append(holder)
        holders: list[ThreadRuntimeLeaseHolder] = []
        for holder in parsed_holders:
            if holder.owner_pid > 0 and not process_exists(holder.owner_pid):
                continue
            if holder.owner_pid > 0 and holder.owner_process_identity:
                current_identity = process_identity(holder.owner_pid)
                if current_identity and current_identity != holder.owner_process_identity:
                    continue
            holders.append(holder)
        if not holders:
            return None
        holders = sorted(holders, key=lambda item: item.holder_id)
        return ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance=owner_instance,
            owner_service_token=owner_service_token,
            control_endpoint=control_endpoint,
            backend_url=backend_url,
            attached_at=attached_at,
            holders=tuple(holders),
        )

    @staticmethod
    def _holder_from_data(raw: object) -> ThreadRuntimeLeaseHolder:
        if not isinstance(raw, dict):
            raise ValueError("holder is not an object")
        required_fields = {
            "holder_id",
            "holder_type",
            "instance_name",
            "owner_pid",
            "owner_service_token",
            "control_endpoint",
            "backend_url",
            "updated_at",
        }
        allowed_fields = required_fields | {"owner_process_identity"}
        if not required_fields.issubset(raw) or not set(raw).issubset(allowed_fields):
            raise ValueError("holder fields do not match schema")
        for field in (
            "holder_id",
            "holder_type",
            "instance_name",
            "owner_service_token",
            "control_endpoint",
            "backend_url",
        ):
            if not isinstance(raw.get(field), str):
                raise ValueError("holder contains a non-string field")
        if "owner_process_identity" in raw and not isinstance(raw.get("owner_process_identity"), str):
            raise ValueError("holder contains an invalid process identity")
        owner_pid_raw = raw.get("owner_pid")
        if isinstance(owner_pid_raw, bool) or not isinstance(owner_pid_raw, int):
            raise ValueError("holder contains an invalid owner pid")
        updated_at_raw = raw.get("updated_at")
        if isinstance(updated_at_raw, bool) or not isinstance(updated_at_raw, (int, float)):
            raise ValueError("holder contains an invalid timestamp")
        holder_id = raw["holder_id"].strip()
        holder_type = raw["holder_type"].strip() or "unknown"
        instance_name = raw["instance_name"].strip().lower()
        owner_pid = owner_pid_raw
        owner_service_token = raw["owner_service_token"].strip()
        control_endpoint = raw["control_endpoint"].strip()
        backend_url = raw["backend_url"].strip()
        updated_at = float(updated_at_raw)
        owner_process_identity = str(raw.get("owner_process_identity", "")).strip()
        if (
            not holder_id
            or not instance_name
            or owner_pid < 0
            or not owner_service_token
            or not math.isfinite(updated_at)
            or updated_at < 0
        ):
            raise ValueError("holder contains an invalid value")
        return ThreadRuntimeLeaseHolder(
            holder_id=holder_id,
            holder_type=holder_type,
            instance_name=instance_name,
            owner_pid=owner_pid,
            owner_service_token=owner_service_token,
            control_endpoint=control_endpoint,
            backend_url=backend_url,
            updated_at=updated_at,
            owner_process_identity=owner_process_identity,
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
    ) -> dict[str, object] | None:
        if lease is None:
            return None
        return cls._serialize_lease(lease)

    @staticmethod
    def _legacy_transfer_touches_instance(raw: object, instance_name: str) -> bool:
        if not isinstance(raw, dict):
            return False
        payload = raw.get("transfer")
        if not isinstance(payload, dict):
            return False
        owner_instance = str(payload.get("owner_instance", "") or "").strip().lower()
        target_instance = str(payload.get("target_instance", "") or "").strip().lower()
        return owner_instance == instance_name or target_instance == instance_name

    def _read_all_unlocked(self) -> dict[str, dict]:
        path = self._file_path()
        try:
            raw_records = read_versioned_records(path, schema_version=_SCHEMA_VERSION)
        except VersionedRecordsUnavailable as exc:
            raise ThreadRuntimeLeaseStoreUnavailable(path, exc.reason) from exc
        records: dict[str, dict] = {}
        for thread_id, value in raw_records.items():
            normalized_thread_id = self._normalize_thread_id(thread_id)
            if not normalized_thread_id or normalized_thread_id in records:
                raise ThreadRuntimeLeaseStoreUnavailable(path, "invalid or duplicate thread key")
            if not isinstance(value, dict):
                raise ThreadRuntimeLeaseStoreUnavailable(path, "invalid lease record")
            try:
                self._lease_from_data(normalized_thread_id, value)
            except ValueError as exc:
                raise ThreadRuntimeLeaseStoreUnavailable(path, "invalid lease record") from exc
            records[normalized_thread_id] = value
        return records

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        try:
            write_versioned_records(path, data, schema_version=_SCHEMA_VERSION)
        except Exception as exc:
            raise ThreadRuntimeLeaseStoreUnavailable(path, "write failed") from exc
