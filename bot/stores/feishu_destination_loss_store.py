"""Strict durable inbox for authoritative Feishu destination-loss proofs."""

from __future__ import annotations

import pathlib
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Iterator

from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
    FeishuDestinationLossRecord,
    FeishuDestinationLossState,
)
from bot.file_lock import acquire_file_lock, open_lock_file, release_file_lock
from bot.stores.versioned_records import (
    VersionedRecordsUnavailable,
    read_versioned_records,
    write_versioned_records,
)


_SCHEMA_VERSION = 2
_PROOF_ID_MAX_LENGTH = 2048
_RECORD_FIELDS = frozenset(
    {
        "proof_id",
        "source_id",
        "chat_id",
        "proof_type",
        "state",
        "accepted_at",
        "settled_at",
    }
)
_LEGACY_RECORD_FIELDS = frozenset(
    {"event_id", "chat_id", "event_type", "state", "accepted_at", "settled_at"}
)


class FeishuDestinationLossStoreUnavailable(RuntimeError):
    """The inbox cannot be read or changed without guessing."""

    def __init__(self, path: pathlib.Path, reason: str) -> None:
        self.path = pathlib.Path(path)
        self.reason = str(reason or "unavailable")
        super().__init__(
            "无法安全读写飞书 destination-loss inbox；proof 未确认接受"
            f"（{self.path.name}: {self.reason}）。"
        )


class FeishuDestinationLossStore:
    """Cross-thread/process-safe proof ledger with bounded tombstones."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        settled_limit: int = 4096,
        clock=time.time,
    ) -> None:
        if type(settled_limit) is not int or settled_limit < 1:
            raise ValueError("settled_limit must be a positive integer")
        self._data_dir = pathlib.Path(data_dir)
        self._settled_limit = settled_limit
        self._clock = clock
        self._lock = threading.Lock()

    def accept(
        self,
        proof: FeishuDestinationLossProof,
    ) -> FeishuDestinationLossRecord:
        if type(proof) is not FeishuDestinationLossProof:
            raise TypeError("proof must be FeishuDestinationLossProof")
        with self._locked_records(write=True) as records:
            existing = records.get(proof.proof_id)
            if existing is not None:
                if existing.proof != proof:
                    raise FeishuDestinationLossStoreUnavailable(
                        self._path(),
                        "proof_id was reused for a different destination-loss fact",
                    )
                return existing
            record = FeishuDestinationLossRecord(
                proof=proof,
                state=FeishuDestinationLossState.PENDING,
                accepted_at=self._now(),
            )
            records[proof.proof_id] = record
            self._prune_settled(records)
            return record

    def pending(self) -> tuple[FeishuDestinationLossRecord, ...]:
        with self._locked_records(write=False) as records:
            return tuple(
                sorted(
                    (
                        record
                        for record in records.values()
                        if record.state is FeishuDestinationLossState.PENDING
                    ),
                    key=lambda record: (record.accepted_at, record.proof.proof_id),
                )
            )

    def load(self, proof_id: str) -> FeishuDestinationLossRecord | None:
        normalized_proof_id = self._required_text(
            proof_id,
            field="proof_id",
            maximum=_PROOF_ID_MAX_LENGTH,
        )
        with self._locked_records(write=False) as records:
            return records.get(normalized_proof_id)

    def settle(
        self,
        proof: FeishuDestinationLossProof,
    ) -> FeishuDestinationLossRecord:
        if type(proof) is not FeishuDestinationLossProof:
            raise TypeError("proof must be FeishuDestinationLossProof")
        with self._locked_records(write=True) as records:
            existing = records.get(proof.proof_id)
            if existing is None:
                raise FeishuDestinationLossStoreUnavailable(
                    self._path(),
                    "cannot settle a proof that was not durably accepted",
                )
            if existing.proof != proof:
                raise FeishuDestinationLossStoreUnavailable(
                    self._path(),
                    "settlement identity does not match the accepted proof",
                )
            if existing.state is FeishuDestinationLossState.SETTLED:
                return existing
            settled = replace(
                existing,
                state=FeishuDestinationLossState.SETTLED,
                settled_at=max(self._now(), existing.accepted_at),
            )
            records[proof.proof_id] = settled
            self._prune_settled(records)
            return settled

    def _prune_settled(
        self,
        records: dict[str, FeishuDestinationLossRecord],
    ) -> None:
        settled = sorted(
            (
                record
                for record in records.values()
                if record.state is FeishuDestinationLossState.SETTLED
            ),
            key=lambda record: (
                record.settled_at or record.accepted_at,
                record.proof.proof_id,
            ),
        )
        for record in settled[: max(len(settled) - self._settled_limit, 0)]:
            records.pop(record.proof.proof_id, None)

    def _now(self) -> float:
        value = float(self._clock())
        if value <= 0:
            raise FeishuDestinationLossStoreUnavailable(
                self._path(),
                "clock returned a non-positive timestamp",
            )
        return value

    @contextmanager
    def _locked_records(
        self,
        *,
        write: bool,
    ) -> Iterator[dict[str, FeishuDestinationLossRecord]]:
        try:
            with self._lock:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                with open_lock_file(self._lock_path()) as lock_file:
                    acquire_file_lock(lock_file, blocking=True)
                    try:
                        records = self._read_all_unlocked()
                        yield records
                        if write:
                            self._write_all_unlocked(records)
                    finally:
                        release_file_lock(lock_file)
        except FeishuDestinationLossStoreUnavailable:
            raise
        except Exception as exc:
            raise FeishuDestinationLossStoreUnavailable(
                self._path(),
                "storage unavailable",
            ) from exc

    def _read_all_unlocked(self) -> dict[str, FeishuDestinationLossRecord]:
        legacy = False
        try:
            raw_records = read_versioned_records(
                self._path(),
                schema_version=_SCHEMA_VERSION,
                accept_unversioned=False,
            )
        except VersionedRecordsUnavailable as current_error:
            try:
                raw_records = read_versioned_records(
                    self._path(),
                    schema_version=1,
                    accept_unversioned=False,
                )
            except VersionedRecordsUnavailable as legacy_error:
                reason = (
                    current_error.reason
                    if current_error.reason != "unsupported or invalid schema"
                    else legacy_error.reason
                )
                raise FeishuDestinationLossStoreUnavailable(
                    self._path(),
                    reason,
                ) from legacy_error
            legacy = True
        records: dict[str, FeishuDestinationLossRecord] = {}
        for record_id, raw in raw_records.items():
            try:
                record = (
                    self._legacy_record_from_data(record_id, raw)
                    if legacy
                    else self._record_from_data(record_id, raw)
                )
            except (TypeError, ValueError) as exc:
                raise FeishuDestinationLossStoreUnavailable(
                    self._path(),
                    "invalid record",
                ) from exc
            proof_id = record.proof.proof_id
            if proof_id in records:
                raise FeishuDestinationLossStoreUnavailable(
                    self._path(),
                    "duplicate normalized proof_id",
                )
            records[proof_id] = record
        return records

    def _write_all_unlocked(
        self,
        records: dict[str, FeishuDestinationLossRecord],
    ) -> None:
        try:
            payload = {
                proof_id: {
                    "proof_id": record.proof.proof_id,
                    "source_id": record.proof.source_id,
                    "chat_id": record.proof.chat_id,
                    "proof_type": record.proof.proof_type.value,
                    "state": record.state.value,
                    "accepted_at": record.accepted_at,
                    "settled_at": record.settled_at,
                }
                for proof_id, record in records.items()
            }
            write_versioned_records(
                self._path(),
                payload,
                schema_version=_SCHEMA_VERSION,
            )
        except Exception as exc:
            raise FeishuDestinationLossStoreUnavailable(
                self._path(),
                "write failed",
            ) from exc

    @classmethod
    def _record_from_data(
        cls,
        proof_id: object,
        raw: object,
    ) -> FeishuDestinationLossRecord:
        normalized_proof_id = cls._required_text(
            proof_id,
            field="proof_id",
            maximum=_PROOF_ID_MAX_LENGTH,
        )
        if not isinstance(raw, dict) or set(raw) != _RECORD_FIELDS:
            raise ValueError("record fields do not match schema")
        proof = FeishuDestinationLossProof(
            source_id=cls._required_text(raw.get("source_id"), field="source_id"),
            chat_id=cls._required_text(raw.get("chat_id"), field="chat_id"),
            proof_type=FeishuDestinationLossProofType(
                cls._required_text(raw.get("proof_type"), field="proof_type")
            ),
        )
        stored_proof_id = cls._required_text(
            raw.get("proof_id"),
            field="proof_id",
            maximum=_PROOF_ID_MAX_LENGTH,
        )
        if stored_proof_id != normalized_proof_id or stored_proof_id != proof.proof_id:
            raise ValueError("record key does not match proof_id")
        return cls._record_from_common(proof, raw)

    @classmethod
    def _legacy_record_from_data(
        cls,
        event_id: object,
        raw: object,
    ) -> FeishuDestinationLossRecord:
        normalized_event_id = cls._required_text(event_id, field="event_id")
        if not isinstance(raw, dict) or set(raw) != _LEGACY_RECORD_FIELDS:
            raise ValueError("legacy record fields do not match schema")
        stored_event_id = cls._required_text(raw.get("event_id"), field="event_id")
        if stored_event_id != normalized_event_id:
            raise ValueError("legacy record key does not match event_id")
        proof = FeishuDestinationLossProof(
            source_id=stored_event_id,
            chat_id=cls._required_text(raw.get("chat_id"), field="chat_id"),
            proof_type=FeishuDestinationLossProofType(
                cls._required_text(raw.get("event_type"), field="event_type")
            ),
        )
        return cls._record_from_common(proof, raw)

    @classmethod
    def _record_from_common(
        cls,
        proof: FeishuDestinationLossProof,
        raw: dict,
    ) -> FeishuDestinationLossRecord:
        state = FeishuDestinationLossState(
            cls._required_text(raw.get("state"), field="state")
        )
        accepted_at = raw.get("accepted_at")
        settled_at = raw.get("settled_at")
        if isinstance(accepted_at, bool) or not isinstance(accepted_at, (int, float)):
            raise TypeError("accepted_at must be a number")
        if settled_at is not None and (
            isinstance(settled_at, bool) or not isinstance(settled_at, (int, float))
        ):
            raise TypeError("settled_at must be a number or null")
        return FeishuDestinationLossRecord(
            proof=proof,
            state=state,
            accepted_at=float(accepted_at),
            settled_at=float(settled_at) if settled_at is not None else None,
        )

    @staticmethod
    def _required_text(
        value: object,
        *,
        field: str,
        maximum: int = 1024,
    ) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"invalid {field}")
        return normalized

    def _path(self) -> pathlib.Path:
        return self._data_dir / "feishu_destination_loss_events.json"

    def _lock_path(self) -> pathlib.Path:
        return self._data_dir / "feishu_destination_loss_events.lock"
