"""
Machine-level running instance registry.

This registry is the discovery surface for local CLIs. Each record describes a
running `feishu-codex` service instance and its control/backend endpoints.
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
        return sorted((entry for entry in entries if entry is not None), key=lambda item: item.instance_name)

    def load(self, instance_name: str) -> InstanceRegistryEntry | None:
        normalized = str(instance_name or "").strip().lower()
        if not normalized:
            return None
        with self._locked_data() as data:
            return self._entry_from_data(data.get(normalized))

    def register(self, entry: InstanceRegistryEntry) -> None:
        with self._locked_data() as data:
            current = self._entry_from_data(data.get(entry.instance_name))
            if current is not None and current.service_token != entry.service_token:
                raise ValueError(
                    f"instance `{entry.instance_name}` 已由另一个运行中的 service 持有：pid={current.owner_pid}"
                )
            data[entry.instance_name] = asdict(entry)
            self._write_all_unlocked(data)

    def unregister(self, instance_name: str, *, service_token: str) -> None:
        normalized = str(instance_name or "").strip().lower()
        normalized_token = str(service_token or "").strip()
        if not normalized or not normalized_token:
            return
        with self._locked_data() as data:
            current = self._entry_from_data(data.get(normalized))
            if current is None or current.service_token != normalized_token:
                return
            data.pop(normalized, None)
            self._write_all_unlocked(data)

    @contextmanager
    def _locked_data(self) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as lock_file:
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
            if entry is None:
                data.pop(instance_name, None)
                changed = True
                continue
            if not process_exists(entry.owner_pid):
                data.pop(instance_name, None)
                changed = True
        return changed

    @staticmethod
    def _entry_from_data(raw: object) -> InstanceRegistryEntry | None:
        if not isinstance(raw, dict):
            return None
        try:
            instance_name = str(raw.get("instance_name", "") or "").strip().lower()
            owner_pid = int(raw.get("owner_pid") or 0)
            service_token = str(raw.get("service_token", "") or "").strip()
            control_endpoint = str(raw.get("control_endpoint", "") or "").strip()
            app_server_url = str(raw.get("app_server_url", "") or "").strip()
            config_dir = str(raw.get("config_dir", "") or "").strip()
            data_dir = str(raw.get("data_dir", "") or "").strip()
            started_at = float(raw.get("started_at") or 0.0)
            updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            return None
        if not instance_name or not service_token or not control_endpoint or not data_dir:
            return None
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
            str(key).strip().lower(): value
            for key, value in raw.items()
            if str(key).strip()
        }

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        rendered = {str(key): value for key, value in sorted(data.items())}
        tmp_path.write_text(json.dumps(rendered, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)


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
    return InstanceRegistryEntry(
        instance_name=str(instance_name or "").strip().lower(),
        owner_pid=int(owner_pid or os.getpid()),
        service_token=str(service_token or "").strip(),
        control_endpoint=str(control_endpoint or "").strip(),
        app_server_url=str(app_server_url or "").strip(),
        config_dir=str(pathlib.Path(config_dir)),
        data_dir=str(pathlib.Path(data_dir)),
        started_at=float(started_at or now),
        updated_at=now,
    )
