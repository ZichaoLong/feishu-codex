"""Machine-local singleton authority for one Feishu application's event stream."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
from typing import TextIO

from bot.atomic_file import atomic_write_text
from bot.file_lock import (
    FileLockBusyError,
    acquire_file_lock,
    open_lock_file,
    release_file_lock,
)


logger = logging.getLogger(__name__)


class FeishuAppConnectionLeaseError(RuntimeError):
    """Another local service may receive events for the same Feishu app."""


class FeishuAppConnectionLease:
    """Prevent machine-local random delivery across duplicate app connections."""

    def __init__(self, global_data_dir: pathlib.Path) -> None:
        self._root_dir = pathlib.Path(global_data_dir) / "feishu_app_connections"
        self._lock_file: TextIO | None = None
        self._app_id = ""
        self._metadata_path: pathlib.Path | None = None

    @property
    def app_id(self) -> str:
        return self._app_id

    def acquire(self, app_id: str, *, instance_name: str) -> None:
        normalized_app_id = self._required_text(app_id, field="app_id")
        normalized_instance = self._required_text(
            instance_name,
            field="instance_name",
        )
        if self._lock_file is not None:
            if self._app_id == normalized_app_id:
                return
            raise FeishuAppConnectionLeaseError(
                "one service cannot own two Feishu app connection leases"
            )
        digest = hashlib.sha256(normalized_app_id.encode("utf-8")).hexdigest()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        lease_path = self._root_dir / f"{digest}.lock"
        metadata_path = self._root_dir / f"{digest}.json"
        lock_file = open_lock_file(lease_path)
        try:
            acquire_file_lock(lock_file, blocking=False)
        except FileLockBusyError as exc:
            lock_file.close()
            owner = self._read_owner_hint(metadata_path)
            detail = f"（{owner}）" if owner else ""
            raise FeishuAppConnectionLeaseError(
                "同一台机器只能运行一个使用该 Feishu app_id 的 Focus service"
                f"{detail}；多个长连接会导致事件随机投递。"
            ) from exc
        try:
            atomic_write_text(
                metadata_path,
                json.dumps(
                    {
                        "app_id": normalized_app_id,
                        "instance_name": normalized_instance,
                        "owner_pid": os.getpid(),
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                mode=0o600,
            )
        except Exception:
            release_file_lock(lock_file)
            lock_file.close()
            raise
        self._lock_file = lock_file
        self._app_id = normalized_app_id
        self._metadata_path = metadata_path

    def release(self) -> None:
        lock_file = self._lock_file
        if lock_file is None:
            return
        metadata_path = self._metadata_path
        try:
            if metadata_path is not None:
                try:
                    metadata_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning(
                        "failed to remove stale Feishu app lease metadata: %s",
                        metadata_path,
                        exc_info=True,
                    )
            release_file_lock(lock_file)
        finally:
            lock_file.close()
            self._lock_file = None
            self._app_id = ""
            self._metadata_path = None

    @staticmethod
    def _read_owner_hint(path: pathlib.Path) -> str:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        if not isinstance(raw, dict):
            return ""
        instance_name = str(raw.get("instance_name", "") or "").strip()
        owner_pid = raw.get("owner_pid")
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
            owner_pid = 0
        parts = []
        if instance_name:
            parts.append(f"instance={instance_name}")
        if owner_pid > 0:
            parts.append(f"pid={owner_pid}")
        return ", ".join(parts)

    @staticmethod
    def _required_text(value: object, *, field: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > 1024:
            raise ValueError(f"invalid {field}")
        return normalized
