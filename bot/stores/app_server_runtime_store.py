"""Discovery and crash-recovery facts for the Focus-owned app-server."""

from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from bot.atomic_file import atomic_write_text
from bot.constants import DEFAULT_APP_SERVER_URL
from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.process_utils import process_exists, process_identity


OWNED_PROCESS_KIND_DIRECT = "direct"
OWNED_PROCESS_KIND_GUARDIAN = "guardian"
_OWNED_PROCESS_KINDS = frozenset(
    {OWNED_PROCESS_KIND_DIRECT, OWNED_PROCESS_KIND_GUARDIAN}
)


class OrphanedOwnedAppServerError(RuntimeError):
    """A prior generation's backend lifecycle process is still unproved."""


@dataclass(slots=True, frozen=True)
class OwnedAppServerRuntime:
    configured_url: str
    active_url: str
    owner_pid: int
    owner_process_identity: str
    lifecycle_pid: int
    lifecycle_process_identity: str
    lifecycle_kind: str
    cleanup_token: str | None


def uses_default_app_server_url(url: str) -> bool:
    normalized = str(url).strip() or DEFAULT_APP_SERVER_URL
    return normalized == DEFAULT_APP_SERVER_URL


class AppServerRuntimeStore:
    """Single store for endpoint discovery and stale-child startup admission."""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "app_server_runtime.json"

    def _lock_path(self) -> pathlib.Path:
        return self._data_dir / "app_server_runtime.lock"

    def cleanup_receipt_path(self, cleanup_token: str) -> pathlib.Path:
        normalized_token = str(cleanup_token or "").strip()
        if not normalized_token:
            raise ValueError("owned app-server guardian cleanup token 无效")
        token_digest = hashlib.sha256(normalized_token.encode("utf-8")).hexdigest()
        return self._data_dir / "app_server_cleanup_receipts" / f"{token_digest}.json"

    def begin_guardian_generation(self) -> str:
        """Reserve a fresh proof token before launching a dormant guardian.

        The guardian must not activate its child until ``save_owned_runtime``
        has durably published the corresponding token. Requiring an empty
        runtime slot here prevents a caller from overwriting unresolved
        authority from an earlier generation.
        """

        with self._locked():
            if self._read_all() is not None:
                raise OrphanedOwnedAppServerError(
                    "cannot begin an owned app-server generation while prior "
                    "runtime authority is still recorded"
                )
            self._delete_all_cleanup_receipts()
            return secrets.token_urlsafe(32)

    def load_owned_runtime(self) -> OwnedAppServerRuntime | None:
        """Return only a process-identity-proved live service generation."""

        with self._locked():
            data = self._read_all()
            if data is None:
                return None
            runtime = self._runtime_from_data(data)
            owner_status = self._incarnation_status(
                runtime.owner_pid,
                runtime.owner_process_identity,
            )
            lifecycle_status = self._incarnation_status(
                runtime.lifecycle_pid,
                runtime.lifecycle_process_identity,
            )
            if owner_status == "same" and lifecycle_status == "same":
                return runtime
            if lifecycle_status in {"gone", "different"}:
                if (
                    runtime.lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN
                    and self._cleanup_receipt_matches(runtime.cleanup_token)
                ):
                    self._retire_runtime(runtime)
            # A dead service with a live lifecycle process is recovery evidence,
            # not a discoverable endpoint. Preserve it for the next service
            # generation's post-lease startup gate.
            return None

    def prepare_for_owned_start(self, *, guardian_wait_seconds: float = 3.0) -> None:
        """Prove that no prior service generation can still own a backend.

        The caller must already hold the instance service lease. A guardian
        normally observes parent-pipe EOF and exits by itself after a crash, so
        startup may wait briefly for that proof. Legacy direct-child records
        lack this relationship and require explicit operator cleanup instead
        of a guessed takeover.
        """

        deadline = time.monotonic() + max(float(guardian_wait_seconds), 0.0)
        with self._locked():
            data = self._read_all()
            if data is None:
                return
            runtime = self._runtime_from_data(data)
            owner_status = self._incarnation_status(
                runtime.owner_pid,
                runtime.owner_process_identity,
            )
            if owner_status in {"same", "unknown"}:
                raise OrphanedOwnedAppServerError(
                    "app_server_runtime.json may still belong to a live Focus "
                    f"service process: pid={runtime.owner_pid}"
                )

            lifecycle_status = self._incarnation_status(
                runtime.lifecycle_pid,
                runtime.lifecycle_process_identity,
            )
            if runtime.lifecycle_kind == OWNED_PROCESS_KIND_DIRECT:
                raise OrphanedOwnedAppServerError(
                    "a legacy direct app-server record has no process-tree "
                    "cleanup proof; verify that the old tree is absent, then "
                    f"remove only this instance record ({self._file_path()}) and retry: "
                    f"pid={runtime.lifecycle_pid} status={lifecycle_status}"
                )
            while (
                lifecycle_status == "same"
                and runtime.lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                lifecycle_status = self._incarnation_status(
                    runtime.lifecycle_pid,
                    runtime.lifecycle_process_identity,
                )
            if lifecycle_status in {"gone", "different"}:
                if runtime.lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN:
                    self._require_cleanup_receipt(runtime)
                self._retire_runtime(runtime)
                return
            if lifecycle_status == "unknown":
                raise OrphanedOwnedAppServerError(
                    "cannot prove the prior owned app-server lifecycle process "
                    f"incarnation: pid={runtime.lifecycle_pid}"
                )
            raise OrphanedOwnedAppServerError(
                "the prior owned app-server guardian has not finished crash "
                f"cleanup; retry after it exits: "
                f"pid={runtime.lifecycle_pid}"
            )

    def save_owned_runtime(
        self,
        *,
        configured_url: str,
        active_url: str,
        owner_pid: int,
        lifecycle_pid: int,
        lifecycle_kind: str = OWNED_PROCESS_KIND_GUARDIAN,
        cleanup_token: str | None = None,
    ) -> None:
        normalized_configured = str(configured_url).strip() or DEFAULT_APP_SERVER_URL
        normalized_active = str(active_url).strip()
        normalized_owner_pid = int(owner_pid)
        normalized_lifecycle_pid = int(lifecycle_pid)
        normalized_lifecycle_kind = str(lifecycle_kind or "").strip()
        normalized_cleanup_token = str(cleanup_token or "").strip() or None
        if not normalized_active:
            raise ValueError("active_url 不能为空")
        if normalized_owner_pid <= 0 or normalized_lifecycle_pid <= 0:
            raise ValueError("owned app-server runtime pid 无效")
        if normalized_lifecycle_kind not in _OWNED_PROCESS_KINDS:
            raise ValueError("owned app-server lifecycle kind 无效")
        if (
            normalized_lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN
            and normalized_cleanup_token is None
        ):
            raise ValueError("owned app-server guardian cleanup token 无效")
        if (
            normalized_lifecycle_kind == OWNED_PROCESS_KIND_DIRECT
            and normalized_cleanup_token is not None
        ):
            raise ValueError("legacy direct lifecycle must not carry a cleanup token")
        owner_identity = process_identity(normalized_owner_pid)
        lifecycle_identity = process_identity(normalized_lifecycle_pid)
        if not owner_identity or not lifecycle_identity:
            raise RuntimeError(
                "cannot prove owned app-server process incarnations for runtime publication"
            )

        payload = {
            "configured_url": normalized_configured,
            "active_url": normalized_active,
            "owner_pid": normalized_owner_pid,
            "owner_process_identity": owner_identity,
            "lifecycle_pid": normalized_lifecycle_pid,
            "lifecycle_process_identity": lifecycle_identity,
            "lifecycle_kind": normalized_lifecycle_kind,
            "updated_at": int(time.time()),
        }
        if normalized_cleanup_token is not None:
            payload["cleanup_token"] = normalized_cleanup_token
        with self._locked():
            existing_data = self._read_all()
            if existing_data is not None:
                existing = self._runtime_from_data(existing_data)
                same_generation = (
                    existing.owner_pid == normalized_owner_pid
                    and existing.owner_process_identity == owner_identity
                    and existing.lifecycle_pid == normalized_lifecycle_pid
                    and existing.lifecycle_process_identity == lifecycle_identity
                    and existing.lifecycle_kind == normalized_lifecycle_kind
                    and existing.cleanup_token == normalized_cleanup_token
                )
                if not same_generation:
                    raise OrphanedOwnedAppServerError(
                        "refusing to overwrite a different owned app-server "
                        "runtime generation"
                    )
            self._write_all(payload)

    def clear_owned_runtime(
        self,
        *,
        owner_pid: int | None = None,
        cleanup_token: str | None = None,
    ) -> None:
        with self._locked():
            data = self._read_all()
            current = self._runtime_from_data(data) if data is not None else None
            if (
                current is not None
                and current.lifecycle_kind == OWNED_PROCESS_KIND_DIRECT
            ):
                raise OrphanedOwnedAppServerError(
                    "refusing to clear a legacy direct app-server runtime: "
                    "it has no process-tree cleanup proof and requires an "
                    "explicit offline operator recovery path"
                )
            if (
                owner_pid is not None
                and current is not None
                and current.owner_pid != owner_pid
            ):
                raise OrphanedOwnedAppServerError(
                    "refusing to clear an owned app-server runtime belonging "
                    f"to a different service process: owner_pid={current.owner_pid}"
                )
            if current is None:
                self._delete_file()
                self._delete_cleanup_receipt(
                    str(cleanup_token or "").strip() or None
                )
                return
            if (
                current is not None
                and current.lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN
            ):
                normalized_token = str(cleanup_token or "").strip() or None
                if (
                    not normalized_token
                    or not current.cleanup_token
                    or not hmac.compare_digest(
                        normalized_token,
                        current.cleanup_token,
                    )
                ):
                    raise OrphanedOwnedAppServerError(
                        "refusing to clear a different owned app-server runtime generation"
                    )
                self._require_cleanup_receipt(current)
            self._retire_runtime(current)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Linearize discovery, stale admission, and runtime publication."""

        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open_lock_file(lock_path) as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    yield
                finally:
                    release_file_lock(lock_file)

    def _runtime_from_data(self, data: dict) -> OwnedAppServerRuntime:
        configured_url = data.get("configured_url")
        active_url = data.get("active_url")
        owner_pid = data.get("owner_pid")
        owner_process_identity = data.get("owner_process_identity", "")
        # Pre-guardian records used app_server_* names and represented a direct
        # child. Read that shape only to fail closed or prove it has exited.
        lifecycle_pid = data.get("lifecycle_pid", data.get("app_server_pid"))
        lifecycle_process_identity = data.get(
            "lifecycle_process_identity",
            data.get("app_server_process_identity", ""),
        )
        lifecycle_kind = data.get("lifecycle_kind", OWNED_PROCESS_KIND_DIRECT)
        cleanup_token = data.get("cleanup_token")
        if not isinstance(configured_url, str) or not configured_url.strip():
            raise RuntimeError("invalid app_server_runtime.json configured_url")
        if not isinstance(active_url, str) or not active_url.strip():
            raise RuntimeError("invalid app_server_runtime.json active_url")
        if type(owner_pid) is not int or owner_pid <= 0:
            raise RuntimeError("invalid app_server_runtime.json owner_pid")
        if not isinstance(owner_process_identity, str):
            raise RuntimeError("invalid app_server_runtime.json owner_process_identity")
        if type(lifecycle_pid) is not int or lifecycle_pid <= 0:
            raise RuntimeError("invalid app_server_runtime.json lifecycle_pid")
        if not isinstance(lifecycle_process_identity, str):
            raise RuntimeError(
                "invalid app_server_runtime.json lifecycle_process_identity"
            )
        if lifecycle_kind not in _OWNED_PROCESS_KINDS:
            raise RuntimeError("invalid app_server_runtime.json lifecycle_kind")
        if cleanup_token is not None and not isinstance(cleanup_token, str):
            raise RuntimeError("invalid app_server_runtime.json cleanup_token")
        normalized_cleanup_token = str(cleanup_token or "").strip() or None
        if (
            lifecycle_kind == OWNED_PROCESS_KIND_GUARDIAN
            and normalized_cleanup_token is None
        ):
            raise RuntimeError(
                "invalid app_server_runtime.json guardian cleanup_token"
            )
        if (
            lifecycle_kind == OWNED_PROCESS_KIND_DIRECT
            and normalized_cleanup_token is not None
        ):
            raise RuntimeError(
                "invalid app_server_runtime.json direct cleanup_token"
            )
        return OwnedAppServerRuntime(
            configured_url=configured_url.strip(),
            active_url=active_url.strip(),
            owner_pid=owner_pid,
            owner_process_identity=owner_process_identity.strip(),
            lifecycle_pid=lifecycle_pid,
            lifecycle_process_identity=lifecycle_process_identity.strip(),
            lifecycle_kind=lifecycle_kind,
            cleanup_token=normalized_cleanup_token,
        )

    def _require_cleanup_receipt(self, runtime: OwnedAppServerRuntime) -> None:
        if self._cleanup_receipt_matches(runtime.cleanup_token):
            return
        raise OrphanedOwnedAppServerError(
            "the prior owned app-server guardian exited without a matching "
            "durable cleanup receipt; its child process tree may still be "
            f"running: guardian_pid={runtime.lifecycle_pid}. Startup remains "
            "fail-closed; after independently verifying absence within this "
            "platform's documented containment boundary, remove only this "
            f"instance record ({self._file_path()}) and retry. No typed "
            "recovery or force command can turn this unknown state into cleanup proof"
        )

    def _cleanup_receipt_matches(self, expected_token: str | None) -> bool:
        if not expected_token:
            return False
        try:
            raw = json.loads(
                self.cleanup_receipt_path(expected_token).read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError, ValueError):
            return False
        if not isinstance(raw, dict):
            return False
        observed_token = raw.get("cleanup_token")
        if not isinstance(observed_token, str) or not observed_token:
            return False
        return hmac.compare_digest(observed_token, expected_token)

    @staticmethod
    def _incarnation_status(pid: int, expected_identity: str) -> str:
        if pid <= 0 or not process_exists(pid):
            return "gone"
        current_identity = process_identity(pid)
        if not current_identity or not expected_identity:
            return "unknown"
        return "same" if current_identity == expected_identity else "different"

    def _read_all(self) -> dict | None:
        path = self._file_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"invalid app_server_runtime.json: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("invalid app_server_runtime.json root")
        return raw

    def _write_all(self, data: dict) -> None:
        atomic_write_text(
            self._file_path(),
            json.dumps(data, ensure_ascii=False, indent=2),
            mode=0o600,
        )

    def _delete_file(self) -> None:
        try:
            self._file_path().unlink()
        except FileNotFoundError:
            pass

    def _delete_cleanup_receipt(self, cleanup_token: str | None) -> None:
        if not cleanup_token:
            return
        try:
            self.cleanup_receipt_path(cleanup_token).unlink()
        except FileNotFoundError:
            pass

    def _delete_all_cleanup_receipts(self) -> None:
        receipt_dir = self._data_dir / "app_server_cleanup_receipts"
        try:
            candidates = tuple(receipt_dir.iterdir())
        except FileNotFoundError:
            return
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix == ".json":
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def _retire_runtime(self, runtime: OwnedAppServerRuntime | None) -> None:
        # Delete authority first. If the process crashes between these two
        # unlinks, begin_guardian_generation safely discards the stale receipt.
        self._delete_file()
        self._delete_cleanup_receipt(
            runtime.cleanup_token if runtime is not None else None
        )
