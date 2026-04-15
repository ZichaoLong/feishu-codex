"""
Single-service ownership lease for one FC_DATA_DIR.

This lease is the authoritative ownership guard for the running
``feishu-codex`` service. The control socket is only a service endpoint; it is
not the ownership primitive.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ServiceInstanceMetadata:
    owner_pid: int
    owner_token: str
    socket_path: str
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

    def acquire(self, *, socket_path: pathlib.Path) -> ServiceInstanceMetadata:
        normalized_socket_path = str(pathlib.Path(socket_path))
        with self._lock:
            current = self._read_metadata_unlocked()
            if self._lock_file is not None and self._owner_token and current is not None:
                return current

            lease_path = self._lease_path()
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lease_path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                metadata = self._read_metadata_unlocked()
                lock_file.close()
                owner_pid = metadata.owner_pid if metadata is not None else 0
                owner_socket_path = metadata.socket_path if metadata is not None else normalized_socket_path
                raise ServiceInstanceLeaseError(
                    "当前 FC_DATA_DIR 已有运行中的 feishu-codex service 持有所有权。"
                    f" owner_pid={owner_pid or 'unknown'} socket={owner_socket_path}"
                ) from exc

            owner_token = secrets.token_urlsafe(24)
            metadata = ServiceInstanceMetadata(
                owner_pid=os.getpid(),
                owner_token=owner_token,
                socket_path=normalized_socket_path,
                started_at=time.time(),
            )
            self._write_metadata_unlocked(metadata)
            self._lock_file = lock_file
            self._owner_token = owner_token
            return metadata

    def owns_socket_path(self, socket_path: pathlib.Path) -> bool:
        normalized_socket_path = str(pathlib.Path(socket_path))
        with self._lock:
            metadata = self._read_metadata_unlocked()
            return (
                self._lock_file is not None
                and bool(self._owner_token)
                and metadata is not None
                and metadata.owner_token == self._owner_token
                and metadata.socket_path == normalized_socket_path
            )

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
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
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
        socket_path = raw.get("socket_path")
        started_at = raw.get("started_at")
        if not isinstance(owner_token, str) or not owner_token.strip():
            return None
        if not isinstance(socket_path, str) or not socket_path.strip():
            return None
        try:
            normalized_owner_pid = int(owner_pid)
        except (TypeError, ValueError):
            normalized_owner_pid = 0
        try:
            normalized_started_at = float(started_at)
        except (TypeError, ValueError):
            normalized_started_at = 0.0
        return ServiceInstanceMetadata(
            owner_pid=normalized_owner_pid,
            owner_token=owner_token.strip(),
            socket_path=socket_path.strip(),
            started_at=normalized_started_at,
        )

    def _write_metadata_unlocked(self, metadata: ServiceInstanceMetadata) -> None:
        path = self._metadata_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        payload = {
            "owner_pid": metadata.owner_pid,
            "owner_token": metadata.owner_token,
            "socket_path": metadata.socket_path,
            "started_at": metadata.started_at,
        }
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)

    def _delete_metadata_unlocked(self) -> None:
        path = self._metadata_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
