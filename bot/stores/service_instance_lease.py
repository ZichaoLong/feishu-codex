"""
Single-service ownership lease for one FC_DATA_DIR.

This lease is the authoritative ownership guard for the running
``feishu-codex`` service. The published control endpoint is only a service
endpoint; it is not the ownership primitive.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import threading
import time
from dataclasses import dataclass

from bot.file_permissions import ensure_private_file_permissions
from bot.file_lock import FileLockBusyError, acquire_file_lock, release_file_lock
from bot.process_utils import process_exists


@dataclass(slots=True, frozen=True)
class ServiceInstanceMetadata:
    owner_pid: int
    owner_token: str
    control_endpoint: str
    started_at: float


class ServiceInstanceLeaseError(RuntimeError):
    """Raised when FC_DATA_DIR service ownership cannot be acquired."""


class ServiceInstanceLease:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()
        self._lock_file = None
        self._owner_token = ""

    def _lease_path(self) -> pathlib.Path:
        return self._data_dir / "service-instance.lock"

    def _metadata_path(self) -> pathlib.Path:
        return self._data_dir / "service-instance.json"

    @property
    def owner_token(self) -> str:
        return self._owner_token

    def load_metadata(self) -> ServiceInstanceMetadata | None:
        with self._lock:
            return self._read_metadata_unlocked()

    def acquire(self, *, control_endpoint: str = "") -> ServiceInstanceMetadata:
        normalized_control_endpoint = str(control_endpoint or "").strip()
        with self._lock:
            current = self._read_metadata_unlocked()
            if self._lock_file is not None and self._owner_token and current is not None:
                return current

            lease_path = self._lease_path()
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lease_path.open("a+", encoding="utf-8")
            try:
                acquire_file_lock(lock_file, blocking=False)
            except FileLockBusyError as exc:
                metadata = self._read_metadata_unlocked()
                lock_file.close()
                owner_pid = metadata.owner_pid if metadata is not None else 0
                owner_endpoint = metadata.control_endpoint if metadata is not None else normalized_control_endpoint
                raise ServiceInstanceLeaseError(
                    "当前 FC_DATA_DIR 已有运行中的 feishu-codex service 持有所有权。"
                    f" owner_pid={owner_pid or 'unknown'} control={owner_endpoint or 'unknown'}"
                ) from exc

            owner_token = secrets.token_urlsafe(24)
            metadata = ServiceInstanceMetadata(
                owner_pid=os.getpid(),
                owner_token=owner_token,
                control_endpoint=normalized_control_endpoint,
                started_at=time.time(),
            )
            self._write_metadata_unlocked(metadata)
            self._lock_file = lock_file
            self._owner_token = owner_token
            return metadata

    def owns_current_lease(self) -> bool:
        with self._lock:
            metadata = self._read_metadata_unlocked()
            return (
                self._lock_file is not None
                and bool(self._owner_token)
                and metadata is not None
                and metadata.owner_token == self._owner_token
            )

    def publish_control_endpoint(self, control_endpoint: str) -> ServiceInstanceMetadata:
        normalized_control_endpoint = str(control_endpoint or "").strip()
        if not normalized_control_endpoint:
            raise ValueError("control_endpoint 不能为空。")
        with self._lock:
            metadata = self._read_metadata_unlocked()
            if self._lock_file is None or not self._owner_token or metadata is None:
                raise ServiceInstanceLeaseError("当前进程尚未持有 service lease。")
            if metadata.owner_token != self._owner_token:
                raise ServiceInstanceLeaseError("当前进程不是此 service lease 的 owner。")
            updated = ServiceInstanceMetadata(
                owner_pid=metadata.owner_pid,
                owner_token=metadata.owner_token,
                control_endpoint=normalized_control_endpoint,
                started_at=metadata.started_at,
            )
            self._write_metadata_unlocked(updated)
            return updated

    def release(self) -> None:
        with self._lock:
            lock_file = self._lock_file
            owner_token = self._owner_token
            self._lock_file = None
            self._owner_token = ""
            metadata = self._read_metadata_unlocked()
            if metadata is not None and metadata.owner_token == owner_token:
                self._delete_metadata_unlocked()
        if lock_file is None:
            return
        try:
            release_file_lock(lock_file)
        finally:
            lock_file.close()

    def _read_metadata_unlocked(self) -> ServiceInstanceMetadata | None:
        path = self._metadata_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        owner_pid = raw.get("owner_pid")
        owner_token = raw.get("owner_token")
        control_endpoint = raw.get("control_endpoint")
        started_at = raw.get("started_at")
        if not isinstance(owner_token, str) or not owner_token.strip():
            return None
        if not isinstance(control_endpoint, str):
            return None
        try:
            normalized_owner_pid = int(owner_pid)
        except (TypeError, ValueError):
            normalized_owner_pid = 0
        try:
            normalized_started_at = float(started_at)
        except (TypeError, ValueError):
            normalized_started_at = 0.0
        metadata = ServiceInstanceMetadata(
            owner_pid=normalized_owner_pid,
            owner_token=owner_token.strip(),
            control_endpoint=control_endpoint.strip(),
            started_at=normalized_started_at,
        )
        same_owner = self._lock_file is not None and bool(self._owner_token) and metadata.owner_token == self._owner_token
        if metadata.owner_pid > 0 and not process_exists(metadata.owner_pid) and not same_owner:
            return None
        return metadata

    def _write_metadata_unlocked(self, metadata: ServiceInstanceMetadata) -> None:
        path = self._metadata_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        payload = {
            "owner_pid": metadata.owner_pid,
            "owner_token": metadata.owner_token,
            "control_endpoint": metadata.control_endpoint,
            "started_at": metadata.started_at,
        }
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        ensure_private_file_permissions(tmp_path)
        os.replace(tmp_path, path)

    def _delete_metadata_unlocked(self) -> None:
        path = self._metadata_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
