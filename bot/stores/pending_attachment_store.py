from __future__ import annotations

import json
import os
import pathlib
import threading
from dataclasses import asdict, dataclass

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PendingAttachmentRecord:
    sender_id: str
    chat_id: str
    thread_id: str
    message_id: str
    attachment_type: str
    resource_key: str
    display_name: str
    local_path: str
    created_at: float
    expires_at: float


class PendingAttachmentStore:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._lock = threading.Lock()

    def add(self, record: PendingAttachmentRecord) -> None:
        self.add_many((record,))

    def add_many(self, records: tuple[PendingAttachmentRecord, ...] | list[PendingAttachmentRecord]) -> None:
        normalized = [self._normalize_record(record) for record in records]
        if not normalized:
            return
        with self._lock:
            data = self._read_all()
            data.extend(normalized)
            self._write_all(data)

    def take(
        self,
        *,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        now: float,
    ) -> tuple[tuple[PendingAttachmentRecord, ...], tuple[PendingAttachmentRecord, ...]]:
        normalized_sender_id = str(sender_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        active: list[PendingAttachmentRecord] = []
        expired: list[PendingAttachmentRecord] = []
        remaining: list[PendingAttachmentRecord] = []
        with self._lock:
            for record in self._read_all():
                is_target = (
                    record.sender_id == normalized_sender_id
                    and record.chat_id == normalized_chat_id
                    and record.thread_id == normalized_thread_id
                )
                if not is_target:
                    if record.expires_at <= now:
                        expired.append(record)
                    else:
                        remaining.append(record)
                    continue
                if record.expires_at <= now:
                    expired.append(record)
                else:
                    active.append(record)
            self._write_all(remaining)
        active.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        expired.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        return tuple(active), tuple(expired)

    def cleanup_expired(self, *, now: float) -> tuple[PendingAttachmentRecord, ...]:
        expired: list[PendingAttachmentRecord] = []
        kept: list[PendingAttachmentRecord] = []
        with self._lock:
            for record in self._read_all():
                if record.expires_at <= now:
                    expired.append(record)
                else:
                    kept.append(record)
            self._write_all(kept)
        expired.sort(key=lambda item: (item.created_at, item.message_id, item.local_path))
        return tuple(expired)

    def list_all(self) -> tuple[PendingAttachmentRecord, ...]:
        with self._lock:
            items = sorted(
                self._read_all(),
                key=lambda item: (
                    item.sender_id,
                    item.chat_id,
                    item.thread_id,
                    item.created_at,
                    item.message_id,
                    item.local_path,
                ),
            )
        return tuple(items)

    def _file_path(self) -> pathlib.Path:
        return self._data_dir / "pending_attachments.json"

    def _read_all(self) -> list[PendingAttachmentRecord]:
        path = self._file_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RuntimeError("pending_attachments.json 格式损坏：顶层必须是对象。")
        schema_version = int(raw.get("schema_version", 0) or 0)
        if schema_version != _SCHEMA_VERSION:
            raise RuntimeError(
                f"pending_attachments.json schema_version={schema_version}，期望 {_SCHEMA_VERSION}。"
            )
        raw_items = raw.get("attachments")
        if not isinstance(raw_items, list):
            raise RuntimeError("pending_attachments.json 格式损坏：attachments 必须是数组。")
        return [self._record_from_dict(item) for item in raw_items]

    def _write_all(self, records: list[PendingAttachmentRecord]) -> None:
        path = self._file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "attachments": [asdict(record) for record in records],
        }
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)

    @staticmethod
    def _normalize_record(record: PendingAttachmentRecord) -> PendingAttachmentRecord:
        return PendingAttachmentRecord(
            sender_id=str(record.sender_id or "").strip(),
            chat_id=str(record.chat_id or "").strip(),
            thread_id=str(record.thread_id or "").strip(),
            message_id=str(record.message_id or "").strip(),
            attachment_type=str(record.attachment_type or "").strip(),
            resource_key=str(record.resource_key or "").strip(),
            display_name=str(record.display_name or "").strip(),
            local_path=str(record.local_path or "").strip(),
            created_at=float(record.created_at),
            expires_at=float(record.expires_at),
        )

    @classmethod
    def _record_from_dict(cls, raw: object) -> PendingAttachmentRecord:
        if not isinstance(raw, dict):
            raise RuntimeError("pending_attachments.json 格式损坏：attachment 项必须是对象。")
        try:
            return cls._normalize_record(
                PendingAttachmentRecord(
                    sender_id=str(raw["sender_id"]),
                    chat_id=str(raw["chat_id"]),
                    thread_id=str(raw.get("thread_id", "")),
                    message_id=str(raw.get("message_id", "")),
                    attachment_type=str(raw["attachment_type"]),
                    resource_key=str(raw.get("resource_key", "")),
                    display_name=str(raw.get("display_name", "")),
                    local_path=str(raw["local_path"]),
                    created_at=float(raw["created_at"]),
                    expires_at=float(raw["expires_at"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("pending_attachments.json 格式损坏：attachment 项字段非法。") from exc
