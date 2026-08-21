"""
Shared interaction-lease store for cross-frontend turn ownership.

This store is intentionally separate from Feishu's in-process subscriber and
execution-mirror state. It only answers one question: which frontend currently
owns the interactive control lease for a live thread turn.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Iterator

from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.process_utils import process_exists, process_identity
from bot.stores.versioned_records import (
    VersionedRecordsUnavailable,
    read_versioned_records,
    write_versioned_records,
)


_SCHEMA_VERSION = 1


@dataclass(slots=True, frozen=True)
class InteractionLeaseHolder:
    kind: str
    holder_id: str
    owner_pid: int = 0
    sender_id: str = ""
    chat_id: str = ""
    participant_id: str = ""
    connection_id: str = ""
    owner_process_identity: str = ""

    def same_holder(self, other: "InteractionLeaseHolder") -> bool:
        return self.kind == other.kind and self.holder_id == other.holder_id


@dataclass(slots=True, frozen=True)
class InteractionLease:
    thread_id: str
    holder: InteractionLeaseHolder
    lease_id: str
    updated_at: float
    # Empty while one frontend is submitting a new main turn; non-empty after
    # method-specific authoritative evidence identifies the exact active turn.
    # In particular, an ordinary turn/start response carries only a submission
    # id, while an inline review response can identify its review turn.
    turn_id: str = ""


@dataclass(slots=True, frozen=True)
class InteractionLeaseAcquireResult:
    granted: bool
    lease: InteractionLease | None
    acquired: bool = False


@dataclass(slots=True, frozen=True)
class InteractionLeaseBackendStopCapture:
    """Exact current-process lease generations captured before backend stop."""

    owner_pid: int
    owner_process_identity: str
    leases: tuple[InteractionLease, ...]


@dataclass(slots=True, frozen=True)
class InteractionLeaseBackendStopRetirementReceipt:
    """Authoritative outcomes for one idempotent post-stop retirement."""

    retired_thread_ids: tuple[str, ...]
    preserved_thread_ids: tuple[str, ...]


class InteractionLeaseStoreUnavailable(RuntimeError):
    """The cross-frontend conflict ledger cannot be trusted safely."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        self.path = pathlib.Path(path)
        self.reason = str(reason or "unavailable")
        super().__init__(
            "无法安全读取 interaction lease 状态；已按 fail-closed 拒绝操作"
            f"（{self.path.name}: {self.reason}）。"
        )


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
        owner_process_identity=process_identity(int(owner_pid)),
    )


def make_fcodex_interaction_holder(
    participant_id: str,
    *,
    connection_id: str = "",
    owner_pid: int,
) -> InteractionLeaseHolder:
    normalized_participant_id = str(participant_id or "").strip()
    normalized_connection_id = str(connection_id or "").strip()
    if not normalized_participant_id:
        raise ValueError("fcodex interaction participant_id 不能为空")
    holder_id = normalized_participant_id
    if normalized_connection_id:
        holder_id = (
            f"{len(normalized_participant_id)}:"
            f"{normalized_participant_id}{normalized_connection_id}"
        )
    return InteractionLeaseHolder(
        kind="fcodex",
        holder_id=holder_id,
        owner_pid=int(owner_pid),
        participant_id=normalized_participant_id,
        connection_id=normalized_connection_id,
        owner_process_identity=process_identity(int(owner_pid)),
    )


def make_web_interaction_holder(
    client_id: str,
    *,
    owner_pid: int,
) -> InteractionLeaseHolder:
    normalized_client_id = str(client_id or "").strip()
    if not normalized_client_id:
        raise ValueError("web interaction client_id 不能为空")
    return InteractionLeaseHolder(
        kind="web",
        holder_id=f"web:{normalized_client_id}",
        owner_pid=int(owner_pid),
        owner_process_identity=process_identity(int(owner_pid)),
    )


def feishu_binding_from_holder(holder: InteractionLeaseHolder) -> tuple[str, str] | None:
    if holder.kind != "feishu":
        return None
    sender_id = str(holder.sender_id or "").strip()
    chat_id = str(holder.chat_id or "").strip()
    if not sender_id or not chat_id:
        return None
    return (sender_id, chat_id)


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

    def list(self) -> list[InteractionLease]:
        """Return current submission/turn leases after stale-PID pruning.

        ``turn_id`` is the lifecycle discriminator: blank means an outbound
        submission has not produced an exact turn identity; non-blank means
        the holder owns that exact active main turn.
        """

        with self._locked_data() as data:
            leases = [
                self._lease_from_data(thread_id, raw)
                for thread_id, raw in data.items()
            ]
        return sorted(
            (lease for lease in leases if lease is not None),
            key=lambda lease: lease.thread_id,
        )

    def capture_current_process_for_backend_stop(
        self,
    ) -> InteractionLeaseBackendStopCapture:
        """Capture exact current-incarnation leases before stopping the backend.

        This read intentionally performs no stale-owner cleanup. Backend reset
        may retire only the records proven to belong to this process
        incarnation; PID 0, foreign processes, and identity mismatches must be
        preserved byte-for-byte by this transaction.
        """

        owner_pid, owner_process_identity = self._current_process_incarnation()
        with self._locked_data(prune_stale=False) as data:
            current_leases = tuple(
                lease
                for thread_id, raw in data.items()
                if (lease := self._lease_from_data(thread_id, raw)) is not None
            )
            if any(
                lease.holder.owner_pid == owner_pid
                and not lease.holder.owner_process_identity
                for lease in current_leases
            ):
                raise InteractionLeaseStoreUnavailable(
                    self._file_path(),
                    "current-PID lease has no process incarnation",
                )
            leases = tuple(
                sorted(
                    (
                        lease
                        for lease in current_leases
                        if lease.holder.owner_pid == owner_pid
                        and lease.holder.owner_process_identity
                        == owner_process_identity
                    ),
                    key=lambda lease: lease.thread_id,
                )
            )
        return InteractionLeaseBackendStopCapture(
            owner_pid=owner_pid,
            owner_process_identity=owner_process_identity,
            leases=leases,
        )

    def retire_after_backend_stop(
        self,
        capture: InteractionLeaseBackendStopCapture,
    ) -> InteractionLeaseBackendStopRetirementReceipt:
        """Retire a pre-stop capture without touching any later generation.

        Each full record is compare-and-set independently. A failed CAS is
        reconciled by an authoritative non-pruning read: absence is an
        idempotent success and a different record is a successor that must be
        preserved. Seeing the captured record unchanged after a failed CAS is
        an unavailable retirement and therefore fails closed.
        """

        self._validate_backend_stop_capture(capture)
        retired: list[str] = []
        preserved: list[str] = []
        for expected in capture.leases:
            if self._release_if_matches_without_pruning(expected):
                retired.append(expected.thread_id)
                continue
            current = self._load_without_pruning(expected.thread_id)
            if current is None:
                preserved.append(expected.thread_id)
                continue
            if current != expected:
                preserved.append(expected.thread_id)
                continue
            raise InteractionLeaseStoreUnavailable(
                self._file_path(),
                "exact backend-stop lease retirement was not confirmed",
            )
        return InteractionLeaseBackendStopRetirementReceipt(
            retired_thread_ids=tuple(retired),
            preserved_thread_ids=tuple(preserved),
        )

    def acquire(self, thread_id: str, holder: InteractionLeaseHolder) -> InteractionLeaseAcquireResult:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return InteractionLeaseAcquireResult(granted=False, lease=None, acquired=False)
        normalized_holder = self._normalize_holder(holder)
        with self._locked_data() as data:
            current = self._lease_from_data(normalized_thread_id, data.get(normalized_thread_id))
            if current is None:
                lease = InteractionLease(
                    thread_id=normalized_thread_id,
                    holder=normalized_holder,
                    lease_id=str(uuid.uuid4()),
                    updated_at=time.time(),
                    turn_id="",
                )
                data[normalized_thread_id] = self._serialize_lease(lease)
                self._write_all_unlocked(data)
                return InteractionLeaseAcquireResult(granted=True, lease=lease, acquired=True)
            if current.holder.same_holder(normalized_holder):
                refreshed = InteractionLease(
                    thread_id=current.thread_id,
                    holder=normalized_holder,
                    lease_id=current.lease_id,
                    updated_at=current.updated_at,
                    turn_id=current.turn_id,
                )
                if refreshed != current:
                    data[normalized_thread_id] = self._serialize_lease(refreshed)
                    self._write_all_unlocked(data)
                return InteractionLeaseAcquireResult(granted=True, lease=refreshed, acquired=False)
            return InteractionLeaseAcquireResult(granted=False, lease=current, acquired=False)

    def force_acquire(self, thread_id: str, holder: InteractionLeaseHolder) -> InteractionLease:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空")
        normalized_holder = self._normalize_holder(holder)
        lease = InteractionLease(
            thread_id=normalized_thread_id,
            holder=normalized_holder,
            lease_id=str(uuid.uuid4()),
            updated_at=time.time(),
            turn_id="",
        )
        with self._locked_data() as data:
            data[normalized_thread_id] = self._serialize_lease(lease)
            self._write_all_unlocked(data)
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
            self._write_all_unlocked(data)
            return True

    def release_if_matches(self, expected: InteractionLease) -> bool:
        """Release only the exact lease generation observed by the caller.

        ``release`` intentionally answers the older holder-identity contract:
        it is suitable when the current holder itself is the authority.  A
        recovery receipt needs a stronger compare-and-set.  In particular, a
        stale receipt must not release a later lease created for the same
        logical holder after an ABA replacement.
        """

        return self._release_if_matches(expected, prune_stale=True)

    def activate_turn(
        self,
        expected: InteractionLease,
        turn_id: str,
    ) -> InteractionLease | None:
        """Bind one submission lease to its exact authoritative main turn.

        The lease generation, holder and thread must still match.  Repeating
        the same activation is idempotent; a different turn id is an ABA or
        identity conflict and leaves the current owner unchanged.
        """

        if not isinstance(expected, InteractionLease):
            raise TypeError("main-turn activation requires a typed interaction lease")
        normalized_thread_id = self._normalize_thread_id(expected.thread_id)
        normalized_turn_id = str(turn_id or "").strip()
        if (
            not normalized_thread_id
            or normalized_thread_id != expected.thread_id
            or not normalized_turn_id
        ):
            raise ValueError("main-turn activation requires exact thread and turn ids")
        with self._locked_data() as data:
            current = self._lease_from_data(
                normalized_thread_id,
                data.get(normalized_thread_id),
            )
            if (
                current is None
                or current.lease_id != expected.lease_id
                or not current.holder.same_holder(expected.holder)
                or (current.turn_id and current.turn_id != normalized_turn_id)
            ):
                return None
            if current.turn_id == normalized_turn_id:
                return current
            active = InteractionLease(
                thread_id=current.thread_id,
                holder=current.holder,
                lease_id=current.lease_id,
                updated_at=current.updated_at,
                turn_id=normalized_turn_id,
            )
            data[normalized_thread_id] = self._serialize_lease(active)
            self._write_all_unlocked(data)
            return active

    def release_turn(self, thread_id: str, turn_id: str) -> bool:
        """Release only the active owner for one matching main turn."""

        normalized_thread_id = self._normalize_thread_id(thread_id)
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_thread_id or not normalized_turn_id:
            return False
        with self._locked_data() as data:
            current = self._lease_from_data(
                normalized_thread_id,
                data.get(normalized_thread_id),
            )
            if current is None or current.turn_id != normalized_turn_id:
                return False
            data.pop(normalized_thread_id, None)
            self._write_all_unlocked(data)
            return True

    def clear_thread(self, thread_id: str) -> bool:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return False
        with self._locked_data() as data:
            if normalized_thread_id not in data:
                return False
            data.pop(normalized_thread_id, None)
            self._write_all_unlocked(data)
            return True

    @contextmanager
    def _locked_data(
        self,
        *,
        prune_stale: bool = True,
    ) -> Iterator[dict[str, dict]]:
        with self._lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open_lock_file(lock_path) as lock_file:
                acquire_file_lock(lock_file, blocking=True)
                try:
                    data = self._read_all_unlocked()
                    if prune_stale and self._prune_stale_leases(data):
                        self._write_all_unlocked(data)
                    yield data
                finally:
                    release_file_lock(lock_file)

    def _current_process_incarnation(self) -> tuple[int, str]:
        owner_pid = os.getpid()
        owner_process_identity = str(process_identity(owner_pid) or "").strip()
        if owner_pid <= 0 or not owner_process_identity:
            raise InteractionLeaseStoreUnavailable(
                self._file_path(),
                "current process incarnation is unavailable",
            )
        return owner_pid, owner_process_identity

    def _validate_backend_stop_capture(
        self,
        capture: InteractionLeaseBackendStopCapture,
    ) -> None:
        if not isinstance(capture, InteractionLeaseBackendStopCapture):
            raise TypeError("backend-stop retirement requires a typed capture")
        owner_pid, owner_process_identity = self._current_process_incarnation()
        if (
            capture.owner_pid != owner_pid
            or capture.owner_process_identity != owner_process_identity
        ):
            raise ValueError(
                "backend-stop capture does not belong to this process incarnation"
            )
        seen_thread_ids: set[str] = set()
        for lease in capture.leases:
            if not isinstance(lease, InteractionLease):
                raise TypeError("backend-stop capture contains an untyped lease")
            if (
                lease.thread_id in seen_thread_ids
                or lease.holder.owner_pid != owner_pid
                or lease.holder.owner_process_identity != owner_process_identity
            ):
                raise ValueError("backend-stop capture contains an invalid lease")
            seen_thread_ids.add(lease.thread_id)

    def _load_without_pruning(self, thread_id: str) -> InteractionLease | None:
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if not normalized_thread_id:
            return None
        with self._locked_data(prune_stale=False) as data:
            return self._lease_from_data(
                normalized_thread_id,
                data.get(normalized_thread_id),
            )

    def _release_if_matches_without_pruning(
        self,
        expected: InteractionLease,
    ) -> bool:
        return self._release_if_matches(expected, prune_stale=False)

    def _release_if_matches(
        self,
        expected: InteractionLease,
        *,
        prune_stale: bool,
    ) -> bool:
        if not isinstance(expected, InteractionLease):
            raise TypeError("exact interaction lease release requires a typed lease")
        normalized_thread_id = self._normalize_thread_id(expected.thread_id)
        if not normalized_thread_id or normalized_thread_id != expected.thread_id:
            return False
        with self._locked_data(prune_stale=prune_stale) as data:
            current = self._lease_from_data(
                normalized_thread_id,
                data.get(normalized_thread_id),
            )
            if (
                current is None
                or current.lease_id != expected.lease_id
                or current != expected
            ):
                return False
            data.pop(normalized_thread_id, None)
            self._write_all_unlocked(data)
            return True

    def _prune_stale_leases(self, data: dict[str, dict]) -> bool:
        stale_thread_ids: list[str] = []
        for thread_id, raw in data.items():
            lease = self._lease_from_data(thread_id, raw)
            if lease is None:
                stale_thread_ids.append(thread_id)
                continue
            owner_pid = int(lease.holder.owner_pid or 0)
            if owner_pid > 0 and not process_exists(owner_pid):
                stale_thread_ids.append(thread_id)
                continue
            if owner_pid > 0 and lease.holder.owner_process_identity:
                current_identity = process_identity(owner_pid)
                if current_identity and current_identity != lease.holder.owner_process_identity:
                    stale_thread_ids.append(thread_id)
        if not stale_thread_ids:
            return False
        for thread_id in stale_thread_ids:
            data.pop(thread_id, None)
        return True

    def _read_all_unlocked(self) -> dict[str, dict]:
        path = self._file_path()
        try:
            raw_records = read_versioned_records(path, schema_version=_SCHEMA_VERSION)
        except VersionedRecordsUnavailable as exc:
            raise InteractionLeaseStoreUnavailable(path, exc.reason) from exc
        records: dict[str, dict] = {}
        for thread_id, value in raw_records.items():
            normalized_thread_id = self._normalize_thread_id(thread_id)
            if not normalized_thread_id or normalized_thread_id in records:
                raise InteractionLeaseStoreUnavailable(path, "invalid or duplicate thread key")
            if not isinstance(value, dict):
                raise InteractionLeaseStoreUnavailable(path, "invalid lease record")
            try:
                self._lease_from_data(normalized_thread_id, value)
            except ValueError as exc:
                raise InteractionLeaseStoreUnavailable(path, "invalid lease record") from exc
            records[normalized_thread_id] = value
        return records

    def _write_all_unlocked(self, data: dict[str, dict]) -> None:
        path = self._file_path()
        try:
            write_versioned_records(path, data, schema_version=_SCHEMA_VERSION)
        except Exception as exc:
            raise InteractionLeaseStoreUnavailable(path, "write failed") from exc

    def _lease_from_data(self, thread_id: str, raw: dict | None) -> InteractionLease | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("lease is not an object")
        required_fields = {"thread_id", "holder", "updated_at"}
        if not required_fields.issubset(raw) or not set(raw).issubset(
            required_fields | {"lease_id", "turn_id"}
        ):
            raise ValueError("lease fields do not match schema")
        if not isinstance(raw.get("thread_id"), str):
            raise ValueError("thread id is not a string")
        recorded_thread_id = self._normalize_thread_id(raw["thread_id"])
        if not recorded_thread_id or recorded_thread_id != thread_id:
            raise ValueError("thread key does not match lease")
        holder_raw = raw.get("holder")
        if not isinstance(holder_raw, dict):
            raise ValueError("holder is not an object")
        required_holder_fields = {"kind", "holder_id", "owner_pid", "sender_id", "chat_id"}
        allowed_holder_fields = required_holder_fields | {
            "participant_id",
            "connection_id",
            "owner_process_identity",
        }
        if not required_holder_fields.issubset(holder_raw) or not set(holder_raw).issubset(allowed_holder_fields):
            raise ValueError("holder fields do not match schema")
        for field in (
            "kind",
            "holder_id",
            "sender_id",
            "chat_id",
            "participant_id",
            "connection_id",
        ):
            if field not in holder_raw:
                continue
            if not isinstance(holder_raw.get(field), str):
                raise ValueError("holder contains a non-string field")
        if "owner_process_identity" in holder_raw and not isinstance(
            holder_raw.get("owner_process_identity"), str
        ):
            raise ValueError("holder contains an invalid process identity")
        kind = holder_raw["kind"].strip()
        holder_id = holder_raw["holder_id"].strip()
        if not kind or not holder_id:
            raise ValueError("holder identity is empty")
        owner_pid_raw = holder_raw.get("owner_pid")
        if isinstance(owner_pid_raw, bool) or not isinstance(owner_pid_raw, int):
            raise ValueError("lease contains an invalid owner pid")
        updated_at_raw = raw.get("updated_at")
        if isinstance(updated_at_raw, bool) or not isinstance(updated_at_raw, (int, float)):
            raise ValueError("lease contains an invalid timestamp")
        owner_pid = owner_pid_raw
        updated_at = float(updated_at_raw)
        if owner_pid < 0 or not math.isfinite(updated_at) or updated_at < 0:
            raise ValueError("lease contains an invalid numeric value")
        holder = InteractionLeaseHolder(
            kind=kind,
            holder_id=holder_id,
            owner_pid=owner_pid,
            sender_id=holder_raw["sender_id"].strip(),
            chat_id=holder_raw["chat_id"].strip(),
            participant_id=str(holder_raw.get("participant_id", "")).strip(),
            connection_id=str(holder_raw.get("connection_id", "")).strip(),
            owner_process_identity=str(
                holder_raw.get("owner_process_identity", "")
            ).strip(),
        )
        lease_id_raw = raw.get("lease_id")
        if lease_id_raw is None:
            lease_id = self._legacy_lease_id(
                thread_id=recorded_thread_id,
                holder=holder,
                updated_at=updated_at,
            )
        else:
            if not isinstance(lease_id_raw, str) or not lease_id_raw.strip():
                raise ValueError("lease contains an invalid generation id")
            try:
                lease_id = str(uuid.UUID(lease_id_raw.strip()))
            except ValueError as exc:
                raise ValueError("lease contains an invalid generation id") from exc
        turn_id_raw = raw.get("turn_id", "")
        if not isinstance(turn_id_raw, str):
            raise ValueError("lease contains an invalid turn id")
        return InteractionLease(
            thread_id=self._normalize_thread_id(thread_id),
            holder=holder,
            lease_id=lease_id,
            updated_at=updated_at,
            turn_id=turn_id_raw.strip(),
        )

    @staticmethod
    def _legacy_lease_id(
        *,
        thread_id: str,
        holder: InteractionLeaseHolder,
        updated_at: float,
    ) -> str:
        """Derive stable recovery identity for a pre-generation v1 record."""

        payload = json.dumps(
            {
                "thread_id": thread_id,
                "holder": asdict(holder),
                "updated_at": updated_at,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"focus:interaction-lease:v1:{payload}",
            )
        )

    @staticmethod
    def _normalize_holder(holder: InteractionLeaseHolder) -> InteractionLeaseHolder:
        kind = str(holder.kind or "").strip()
        holder_id = str(holder.holder_id or "").strip()
        owner_pid = int(holder.owner_pid or 0)
        if not kind or not holder_id or owner_pid < 0:
            raise ValueError("interaction lease holder 字段无效。")
        owner_process_identity = str(holder.owner_process_identity or "").strip()
        if owner_pid > 0 and not owner_process_identity:
            owner_process_identity = process_identity(owner_pid)
        return InteractionLeaseHolder(
            kind=kind,
            holder_id=holder_id,
            owner_pid=owner_pid,
            sender_id=str(holder.sender_id or "").strip(),
            chat_id=str(holder.chat_id or "").strip(),
            participant_id=str(holder.participant_id or "").strip(),
            connection_id=str(holder.connection_id or "").strip(),
            owner_process_identity=owner_process_identity,
        )

    @staticmethod
    def _serialize_lease(lease: InteractionLease) -> dict[str, object]:
        return {
            "thread_id": lease.thread_id,
            "holder": asdict(lease.holder),
            "lease_id": lease.lease_id,
            "updated_at": lease.updated_at,
            "turn_id": lease.turn_id,
        }
