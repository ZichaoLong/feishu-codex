"""
Machine-level running instance registry.

This registry is the discovery surface for local CLIs. Each record describes a
running FOCUS service instance and its control/backend endpoints.
"""

from __future__ import annotations

import math
import os
import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
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
class InstanceRegistryEntry:
    instance_name: str
    owner_pid: int
    service_token: str
    control_endpoint: str
    app_server_url: str
    config_dir: str
    data_dir: str
    started_at: float
    updated_at: float
    owner_process_identity: str = ""


class InstanceRegistryStoreUnavailable(RuntimeError):
    """The machine-level instance registry cannot be trusted safely."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        self.path = pathlib.Path(path)
        self.reason = str(reason or "unavailable")
        super().__init__(
            "无法安全读取本机 instance registry；已按 fail-closed 拒绝协调操作"
            f"（{self.path.name}: {self.reason}）。"
        )


class InstanceRegistryStore:
    def __init__(self, root_dir: pathlib.Path | None = None) -> None:
        self._root_dir = pathlib.Path(root_dir) if root_dir is not None else global_data_dir()
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._root_dir / "instance_registry.json"

    def _lock_path(self) -> pathlib.Path:
        return self._root_dir / "instance_registry.lock"

    def list_instances(self) -> list[InstanceRegistryEntry]:
        with self._locked_data() as data:
            entries = [self._entry_from_data(item) for item in data.values()]
        return sorted(entries, key=lambda item: item.instance_name)

    def load(self, instance_name: str) -> InstanceRegistryEntry | None:
        normalized = str(instance_name or "").strip().lower()
        if not normalized:
            return None
        with self._locked_data() as data:
            return self._optional_entry_from_data(data.get(normalized))

    def register(self, entry: InstanceRegistryEntry) -> None:
        normalized_entry = self._normalize_entry_for_write(entry)
        with self._locked_data() as data:
            current = self._optional_entry_from_data(data.get(normalized_entry.instance_name))
            if current is not None and current.service_token != normalized_entry.service_token:
                raise ValueError(
                    f"instance `{normalized_entry.instance_name}` 已由另一个运行中的 service 持有："
                    f"pid={current.owner_pid}"
                )
            data[normalized_entry.instance_name] = asdict(normalized_entry)
            self._write_all_unlocked(data)

    def unregister(self, instance_name: str, *, service_token: str) -> None:
        normalized = str(instance_name or "").strip().lower()
        normalized_token = str(service_token or "").strip()
        if not normalized or not normalized_token:
            return
        with self._locked_data() as data:
            current = self._optional_entry_from_data(data.get(normalized))
            if current is None or current.service_token != normalized_token:
                return
            data.pop(normalized, None)
            self._write_all_unlocked(data)

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open_lock_file(lock_path) as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    data = self._read_all_unlocked()
                    if self._prune_stale_entries(data):
                        self._write_all_unlocked(data)
                    yield data
                finally:
                    release_file_lock(lock_file)

    def _prune_stale_entries(self, data: dict[str, dict]) -> bool:
        changed = False
        for instance_name in list(data):
            entry = self._entry_from_data(data.get(instance_name))
            if not process_exists(entry.owner_pid):
                data.pop(instance_name, None)
                changed = True
                continue
            if entry.owner_process_identity:
                current_identity = process_identity(entry.owner_pid)
                if current_identity and current_identity != entry.owner_process_identity:
                    data.pop(instance_name, None)
                    changed = True
        return changed

    @classmethod
    def _optional_entry_from_data(cls, raw: object) -> InstanceRegistryEntry | None:
        if raw is None:
            return None
        return cls._entry_from_data(raw)

    @staticmethod
    def _entry_from_data(raw: object) -> InstanceRegistryEntry:
        if not isinstance(raw, dict):
            raise ValueError("record is not an object")
        required_fields = {
            "instance_name",
            "owner_pid",
            "service_token",
            "control_endpoint",
            "app_server_url",
            "config_dir",
            "data_dir",
            "started_at",
            "updated_at",
        }
        allowed_fields = required_fields | {"owner_process_identity"}
        if not required_fields.issubset(raw) or not set(raw).issubset(allowed_fields):
            raise ValueError("record fields do not match schema")
        string_fields = (
            "instance_name",
            "service_token",
            "control_endpoint",
            "app_server_url",
            "config_dir",
            "data_dir",
        )
        if any(not isinstance(raw.get(field), str) for field in string_fields):
            raise ValueError("record contains a non-string field")
        if "owner_process_identity" in raw and not isinstance(raw.get("owner_process_identity"), str):
            raise ValueError("record contains an invalid process identity")
        owner_pid_raw = raw.get("owner_pid")
        if isinstance(owner_pid_raw, bool) or not isinstance(owner_pid_raw, int):
            raise ValueError("record contains an invalid owner pid")
        started_at_raw = raw.get("started_at")
        updated_at_raw = raw.get("updated_at")
        if (
            isinstance(started_at_raw, bool)
            or not isinstance(started_at_raw, (int, float))
            or isinstance(updated_at_raw, bool)
            or not isinstance(updated_at_raw, (int, float))
        ):
            raise ValueError("record contains an invalid timestamp")
        instance_name = raw["instance_name"].strip().lower()
        owner_pid = owner_pid_raw
        service_token = raw["service_token"].strip()
        control_endpoint = raw["control_endpoint"].strip()
        app_server_url = raw["app_server_url"].strip()
        config_dir = raw["config_dir"].strip()
        data_dir = raw["data_dir"].strip()
        started_at = float(started_at_raw)
        updated_at = float(updated_at_raw)
        owner_process_identity = str(raw.get("owner_process_identity", "")).strip()
        if (
            not instance_name
            or owner_pid <= 0
            or not service_token
            or not control_endpoint
            or not data_dir
            or not math.isfinite(started_at)
            or started_at < 0
            or not math.isfinite(updated_at)
            or updated_at < 0
        ):
            raise ValueError("record contains an invalid value")
        return InstanceRegistryEntry(
            instance_name=instance_name,
            owner_pid=owner_pid,
            service_token=service_token,
            control_endpoint=control_endpoint,
            app_server_url=app_server_url,
            config_dir=config_dir,
            data_dir=data_dir,
            started_at=started_at,
            updated_at=updated_at,
            owner_process_identity=owner_process_identity,
        )

    @classmethod
    def _normalize_entry_for_write(cls, entry: InstanceRegistryEntry) -> InstanceRegistryEntry:
        normalized = cls._entry_from_data(asdict(entry))
        if normalized.owner_process_identity:
            return normalized
        identity = process_identity(normalized.owner_pid)
        if not identity:
            return normalized
        return replace(normalized, owner_process_identity=identity)

    def _read_all_unlocked(self) -> dict[str, dict]:
        path = self._file_path()
        try:
            raw_records = read_versioned_records(path, schema_version=_SCHEMA_VERSION)
        except VersionedRecordsUnavailable as exc:
            raise InstanceRegistryStoreUnavailable(path, exc.reason) from exc
        records: dict[str, dict] = {}
        for key, value in raw_records.items():
            normalized_key = str(key or "").strip().lower()
            if not normalized_key or normalized_key in records:
                raise InstanceRegistryStoreUnavailable(path, "invalid or duplicate instance key")
            try:
                entry = self._entry_from_data(value)
            except ValueError as exc:
                raise InstanceRegistryStoreUnavailable(path, "invalid instance record") from exc
            if entry.instance_name != normalized_key:
                raise InstanceRegistryStoreUnavailable(path, "instance key does not match record")
            records[normalized_key] = asdict(entry)
        return records

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        try:
            write_versioned_records(path, data, schema_version=_SCHEMA_VERSION)
        except Exception as exc:
            raise InstanceRegistryStoreUnavailable(path, "write failed") from exc


def build_instance_registry_entry(
    *,
    instance_name: str,
    service_token: str,
    control_endpoint: str,
    app_server_url: str,
    config_dir: pathlib.Path,
    data_dir: pathlib.Path,
    owner_pid: int | None = None,
    started_at: float | None = None,
) -> InstanceRegistryEntry:
    now = time.time()
    normalized_owner_pid = int(os.getpid() if owner_pid is None else owner_pid)
    return InstanceRegistryEntry(
        instance_name=str(instance_name or "").strip().lower(),
        owner_pid=normalized_owner_pid,
        service_token=str(service_token or "").strip(),
        control_endpoint=str(control_endpoint or "").strip(),
        app_server_url=str(app_server_url or "").strip(),
        config_dir=str(pathlib.Path(config_dir)),
        data_dir=str(pathlib.Path(data_dir)),
        started_at=float(started_at or now),
        updated_at=now,
        owner_process_identity=process_identity(normalized_owner_pid),
    )
