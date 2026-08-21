"""Per-instance discovery state for the Focus Web Gateway."""

from __future__ import annotations

import json
import math
import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from bot.atomic_file import atomic_write_text
from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.network_contract import parse_owned_web_gateway_endpoint
from bot.process_utils import process_exists, process_identity


@dataclass(slots=True, frozen=True)
class WebGatewayRuntime:
    endpoint: str
    bootstrap_token: str
    owner_pid: int
    started_at: float
    owner_process_identity: str


class WebGatewayRuntimeStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "web_gateway_runtime.json"

    def _lock_path(self) -> pathlib.Path:
        return self._data_dir / "web_gateway_runtime.lock"

    def load(self) -> WebGatewayRuntime | None:
        with self._locked():
            raw = self._read_all()
            if raw is None:
                return None
            runtime = self._runtime_from_data(raw)
            if not process_exists(runtime.owner_pid):
                return None
            current_identity = process_identity(runtime.owner_pid)
            if (
                not isinstance(current_identity, str)
                or not current_identity
                or current_identity != current_identity.strip()
            ):
                return None
            if current_identity != runtime.owner_process_identity:
                self._delete_file()
                return None
            return runtime

    def save(
        self,
        *,
        endpoint: str,
        bootstrap_token: str,
        owner_pid: int,
        started_at: float | None = None,
    ) -> None:
        normalized_endpoint = parse_owned_web_gateway_endpoint(endpoint).origin
        normalized_token = str(bootstrap_token or "").strip()
        if not normalized_token:
            raise ValueError("web gateway bootstrap token 不能为空")
        normalized_owner_pid = int(owner_pid)
        if normalized_owner_pid <= 0:
            raise ValueError("web gateway owner_pid 无效")
        normalized_started_at = float(started_at or time.time())
        if not math.isfinite(normalized_started_at) or normalized_started_at <= 0:
            raise ValueError("web gateway started_at 无效")
        owner_process_identity = process_identity(normalized_owner_pid)
        if (
            not isinstance(owner_process_identity, str)
            or not owner_process_identity
            or owner_process_identity != owner_process_identity.strip()
        ):
            raise RuntimeError("无法取得 Web Gateway owner process incarnation")
        payload = {
            "endpoint": normalized_endpoint,
            "bootstrap_token": normalized_token,
            "owner_pid": normalized_owner_pid,
            "owner_process_identity": owner_process_identity,
            "started_at": normalized_started_at,
            "updated_at": time.time(),
        }
        with self._locked():
            self._write_all(payload)

    def clear(self, *, owner_pid: int | None = None) -> None:
        with self._locked():
            raw = self._read_all()
            runtime = self._runtime_from_data(raw) if raw is not None else None
            if owner_pid is not None and runtime is not None and runtime.owner_pid not in {0, int(owner_pid)}:
                return
            self._delete_file()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Linearize stale-owner pruning with cross-process writers."""
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open_lock_file(lock_path) as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    yield
                finally:
                    release_file_lock(lock_file)

    @staticmethod
    def _runtime_from_data(raw: object) -> WebGatewayRuntime:
        if not isinstance(raw, dict):
            raise RuntimeError("invalid web_gateway_runtime.json root")
        try:
            raw_endpoint = raw.get("endpoint")
            raw_bootstrap_token = raw.get("bootstrap_token")
            if not isinstance(raw_endpoint, str) or not isinstance(raw_bootstrap_token, str):
                raise TypeError("endpoint/bootstrap_token")
            endpoint = parse_owned_web_gateway_endpoint(raw_endpoint).origin
            bootstrap_token = raw_bootstrap_token.strip()
            raw_owner_pid = raw.get("owner_pid")
            if type(raw_owner_pid) is not int:
                raise TypeError("owner_pid")
            owner_pid = raw_owner_pid
            started_at = float(raw.get("started_at") or 0.0)
        except (TypeError, ValueError):
            raise RuntimeError("invalid web_gateway_runtime.json fields") from None
        owner_process_identity = raw.get("owner_process_identity")
        if (
            not endpoint
            or not bootstrap_token
            or owner_pid <= 0
            or not math.isfinite(started_at)
            or started_at <= 0
        ):
            raise RuntimeError("invalid web_gateway_runtime.json fields")
        if (
            not isinstance(owner_process_identity, str)
            or not owner_process_identity.strip()
            or owner_process_identity != owner_process_identity.strip()
        ):
            raise RuntimeError("invalid web_gateway_runtime.json owner_process_identity")
        return WebGatewayRuntime(
            endpoint=endpoint,
            bootstrap_token=bootstrap_token,
            owner_pid=owner_pid,
            started_at=started_at,
            owner_process_identity=owner_process_identity,
        )

    def _read_all(self) -> dict | None:
        path = self._file_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid web_gateway_runtime.json: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("invalid web_gateway_runtime.json root")
        return raw

    def _write_all(self, data: dict) -> None:
        path = self._file_path()
        atomic_write_text(
            path,
            json.dumps(data, ensure_ascii=False, indent=2),
            mode=0o600,
        )

    def _delete_file(self) -> None:
        try:
            self._file_path().unlink()
        except FileNotFoundError:
            pass
