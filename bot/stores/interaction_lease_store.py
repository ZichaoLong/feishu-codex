"""
Shared interaction-lease store for cross-frontend turn ownership.

This store is intentionally separate from Feishu's in-process subscriber and
execution-mirror state. It only answers one question: which frontend currently
owns the interactive control lease for a live thread turn.
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

import fcntl


@dataclass(slots=True, frozen=True)
class InteractionLeaseHolder:
    kind: str
    holder_id: str
    owner_pid: int = 0
    sender_id: str = ""
    chat_id: str = ""

    def same_holder(self, other: "InteractionLeaseHolder") -> bool:
        return self.kind == other.kind and self.holder_id == other.holder_id


@dataclass(slots=True, frozen=True)
class InteractionLease:
    thread_id: str
    holder: InteractionLeaseHolder
    updated_at: float


@dataclass(slots=True, frozen=True)
class InteractionLeaseAcquireResult:
    granted: bool
    lease: InteractionLease | None
    acquired: bool = False


def make_feishu_interaction_holder(
    sender_id: str,
    chat_id: str,
    *,
    owner_pid: int,
) -> InteractionLeaseHolder:
    normalized_sender_id = str(sender_id or "").strip()
    normalized_chat_id = str(chat_id or "").strip()
    return InteractionLeaseHolder(
        kind="feishu",
        holder_id=f"{normalized_sender_id}:{normalized_chat_id}",
        owner_pid=int(owner_pid),
        sender_id=normalized_sender_id,
        chat_id=normalized_chat_id,
    )


def make_fcodex_interaction_holder(
    holder_id: str,
    *,
    owner_pid: int,
) -> InteractionLeaseHolder:
    normalized_holder_id = str(holder_id or "").strip()
    if not normalized_holder_id:
        raise ValueError("fcodex interaction holder_id 不能为空")
    return InteractionLeaseHolder(
        kind="fcodex",
        holder_id=normalized_holder_id,
        owner_pid=int(owner_pid),
    )


def feishu_binding_from_holder(holder: InteractionLeaseHolder) -> tuple[str, str] | None:
    if holder.kind != "feishu":
        return None
    sender_id = str(holder.sender_id or "").strip()
    chat_id = str(holder.chat_id or "").strip()
    if not sender_id or not chat_id:
        return None
    return (sender_id, chat_id)


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


class InteractionLeaseStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "interaction_leases.json"

    def _lock_path(self) -> pathlib.Path:
        return self._data_dir / "interaction_leases.lock"

    @staticmethod
    def _normalize_thread_id(thread_id: str) -> str:
        return str(thread_id or "").strip()

    def load(self, thread_id: str) -> InteractionLease | None:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return None
        with self._locked_data() as data:
            return self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))

    def acquire(self, thread_id: str, holder: InteractionLeaseHolder) -> InteractionLeaseAcquireResult:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return InteractionLeaseAcquireResult(granted=False, lease=None, acquired=False)
        with self._locked_data() as data:
            current = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
            if current is None:
                lease = InteractionLease(
                    thread_id=normalized_thread_id,
                    holder=holder,
                    updated_at=time.time(),
                )
                data[normalized_thread_id] = self._serialize_lease(lease)
                return InteractionLeaseAcquireResult(granted=True, lease=lease, acquired=True)
            if current.holder.same_holder(holder):
                refreshed = InteractionLease(
                    thread_id=current.thread_id,
                    holder=holder,
                    updated_at=current.updated_at,
                )
                if refreshed != current:
                    data[normalized_thread_id] = self._serialize_lease(refreshed)
                return InteractionLeaseAcquireResult(granted=True, lease=refreshed, acquired=False)
            return InteractionLeaseAcquireResult(granted=False, lease=current, acquired=False)

    def force_acquire(self, thread_id: str, holder: InteractionLeaseHolder) -> InteractionLease:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空")
        lease = InteractionLease(
            thread_id=normalized_thread_id,
            holder=holder,
            updated_at=time.time(),
        )
        with self._locked_data() as data:
            data[normalized_thread_id] = self._serialize_lease(lease)
        return lease

    def release(self, thread_id: str, holder: InteractionLeaseHolder) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        with self._locked_data() as data:
            current = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
            if current is None or not current.holder.same_holder(holder):
                return False
            data.pop(normalized_thread_id, None)
            return True

    def clear_thread(self, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        with self._locked_data() as data:
            if normalized_thread_id not in data:
                return False
            data.pop(normalized_thread_id, None)
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
                    self._write_all_unlocked(data)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _prune_stale_leases(self, data: dict[str, dict]) -> bool:
        stale_thread_ids: list[str] = []
        for thread_id, raw in data.items():
            lease = self._lease_from_data(thread_id, raw)
            if lease is None:
                stale_thread_ids.append(thread_id)
                continue
            owner_pid = int(lease.holder.owner_pid or 0)
            if owner_pid > 0 and not _process_exists(owner_pid):
                stale_thread_ids.append(thread_id)
        if not stale_thread_ids:
            return False
        for thread_id in stale_thread_ids:
            data.pop(thread_id, None)
        return True

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
            str(thread_id).strip(): value
            for thread_id, value in raw.items()
            if str(thread_id).strip() and isinstance(value, dict)
        }

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    def _lease_from_data(self, thread_id: str, raw: dict | None) -> InteractionLease | None:
        if not isinstance(raw, dict):
            return None
        holder_raw = raw.get("holder")
        if not isinstance(holder_raw, dict):
            return None
        kind = str(holder_raw.get("kind") or "").strip()
        holder_id = str(holder_raw.get("holder_id") or "").strip()
        if not kind or not holder_id:
            return None
        try:
            updated_at = float(raw.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        return InteractionLease(
            thread_id=self._normalize_thread_id(thread_id),
            holder=InteractionLeaseHolder(
                kind=kind,
                holder_id=holder_id,
                owner_pid=int(holder_raw.get("owner_pid") or 0),
                sender_id=str(holder_raw.get("sender_id") or "").strip(),
                chat_id=str(holder_raw.get("chat_id") or "").strip(),
            ),
            updated_at=updated_at,
        )

    @staticmethod
    def _serialize_lease(lease: InteractionLease) -> dict[str, object]:
        return {
            "thread_id": lease.thread_id,
            "holder": asdict(lease.holder),
            "updated_at": lease.updated_at,
        }
