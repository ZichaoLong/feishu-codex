"""
共享 managed app-server 运行时状态。

当默认 shared backend URL 需要从 8765 自动切到其他空闲端口时，
feishu-codex 会把实际监听地址写到这份本地状态里。
未显式传 `--remote` 的 fcodex 等默认入口可据此发现同一 shared backend。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass

from bot.constants import DEFAULT_APP_SERVER_URL


@dataclass(slots=True, frozen=True)
class ManagedAppServerRuntime:
    configured_url: str = ""
    active_url: str = ""
    owner_pid: int = 0
    app_server_pid: int = 0


def uses_default_app_server_url(url: str) -> bool:
    normalized = str(url).strip() or DEFAULT_APP_SERVER_URL
    return normalized == DEFAULT_APP_SERVER_URL


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


class AppServerRuntimeStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "app_server_runtime.json"

    def resolve_url(self, configured_url: str) -> str:
        normalized = str(configured_url).strip() or DEFAULT_APP_SERVER_URL
        if not uses_default_app_server_url(normalized):
            return normalized
        runtime = self.load_managed_runtime()
        if runtime is None:
            return normalized
        if runtime.configured_url != normalized:
            return normalized
        return runtime.active_url or normalized

    def load_managed_runtime(self) -> ManagedAppServerRuntime | None:
        with self._lock:
            data = self._read_all()
            runtime = self._runtime_from_data(data)
            if runtime is None:
                return None
            if runtime.owner_pid > 0 and not _process_exists(runtime.owner_pid):
                self._delete_file()
                return None
            if runtime.app_server_pid > 0 and not _process_exists(runtime.app_server_pid):
                self._delete_file()
                return None
            return runtime

    def save_managed_runtime(
        self,
        *,
        configured_url: str,
        active_url: str,
        owner_pid: int,
        app_server_pid: int = 0,
    ) -> None:
        normalized_configured = str(configured_url).strip() or DEFAULT_APP_SERVER_URL
        normalized_active = str(active_url).strip()
        if not normalized_active:
            raise ValueError("active_url 不能为空")

        payload = {
            "configured_url": normalized_configured,
            "active_url": normalized_active,
            "owner_pid": int(owner_pid),
            "app_server_pid": int(app_server_pid),
            "updated_at": int(time.time()),
        }
        with self._lock:
            self._write_all(payload)

    def clear_managed_runtime(self, *, owner_pid: int | None = None) -> None:
        with self._lock:
            current = self._runtime_from_data(self._read_all())
            if owner_pid is not None and current is not None and current.owner_pid not in {0, owner_pid}:
                return
            self._delete_file()

    def _runtime_from_data(self, data: dict) -> ManagedAppServerRuntime | None:
        configured_url = data.get("configured_url")
        active_url = data.get("active_url")
        owner_pid = data.get("owner_pid")
        app_server_pid = data.get("app_server_pid")
        if not isinstance(configured_url, str) or not configured_url.strip():
            return None
        if not isinstance(active_url, str) or not active_url.strip():
            return None
        return ManagedAppServerRuntime(
            configured_url=configured_url.strip(),
            active_url=active_url.strip(),
            owner_pid=int(owner_pid) if isinstance(owner_pid, int) else 0,
            app_server_pid=int(app_server_pid) if isinstance(app_server_pid, int) else 0,
        )

    def _read_all(self) -> dict:
        path = self._file_path()
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_all(self, data: dict) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    def _delete_file(self) -> None:
        path = self._file_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def resolve_effective_app_server_url(configured_url: str, *, data_dir: pathlib.Path) -> str:
    return AppServerRuntimeStore(data_dir).resolve_url(configured_url)
