"""
群聊工作态、权限与消息日志存储。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from typing import Any

from bot.feishu_types import BoundaryState, GroupChatStoreData, GroupMessageEntry, GroupState

DEFAULT_GROUP_MODE = "assistant"
GROUP_MODES = {"mention_only", "assistant", "all"}
MAIN_SCOPE = "main"
GROUP_CHAT_STORE_SCHEMA_VERSION = 2
SUPPORTED_GROUP_CHAT_STORE_SCHEMA_VERSIONS = frozenset({1, GROUP_CHAT_STORE_SCHEMA_VERSION})


class GroupChatStore:
    """管理群聊工作态、激活状态与助理模式消息日志。"""

    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    @staticmethod
    def normalize_scope(scope: str | None) -> str:
        normalized = str(scope or "").strip()
        return normalized or MAIN_SCOPE

    def get_group_mode(self, chat_id: str) -> str:
        with self._lock:
            group = self._group_state(chat_id)
            return group["mode"]

    def set_group_mode(self, chat_id: str, mode: str) -> str:
        normalized = str(mode or "").strip().lower()
        if normalized not in GROUP_MODES:
            raise ValueError(f"invalid group mode: {mode}")
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            group["mode"] = normalized
            self._write_group_state(data, chat_id, group)
        return normalized

    def is_group_activated(self, chat_id: str) -> bool:
        with self._lock:
            group = self._group_state(chat_id)
            return bool(group["activated"])

    def activation_snapshot(self, chat_id: str) -> dict[str, object]:
        with self._lock:
            group = self._group_state(chat_id)
            return {
                "activated": bool(group["activated"]),
                "activated_by": str(group["activated_by"] or ""),
                "activated_at": int(group["activated_at"]),
            }

    def activate_chat(
        self,
        chat_id: str,
        *,
        activated_by: str,
        activated_at: int | None = None,
    ) -> dict[str, object]:
        normalized_activated_by = str(activated_by or "").strip()
        if not normalized_activated_by:
            raise ValueError("activated_by 不能为空")
        normalized_activated_at = max(int(activated_at or int(time.time() * 1000)), 0)
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            updated_group = self._group_with_activation_state(
                group,
                activated=True,
                activated_by=normalized_activated_by,
                activated_at=normalized_activated_at,
            )
            self._write_group_state(data, chat_id, updated_group)
        return {
            "activated": True,
            "activated_by": normalized_activated_by,
            "activated_at": normalized_activated_at,
        }

    def deactivate_chat(self, chat_id: str) -> dict[str, object]:
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            updated_group = self._group_with_activation_state(
                group,
                activated=False,
                activated_by="",
                activated_at=0,
            )
            self._write_group_state(data, chat_id, updated_group)
        return {
            "activated": False,
            "activated_by": "",
            "activated_at": 0,
        }

    def get_last_boundary_seq(self, chat_id: str, *, scope: str = MAIN_SCOPE) -> int:
        with self._lock:
            group = self._group_state(chat_id)
            return int(self._boundary_state(group, scope)["seq"])

    def set_last_boundary_seq(self, chat_id: str, seq: int, *, scope: str = MAIN_SCOPE) -> int:
        normalized = max(int(seq), 0)
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            boundary = self._boundary_state(group, scope)
            boundary["seq"] = normalized
            self._write_group_state(data, chat_id, group)
        return normalized

    def get_last_boundary_created_at(self, chat_id: str, *, scope: str = MAIN_SCOPE) -> int:
        with self._lock:
            group = self._group_state(chat_id)
            return int(self._boundary_state(group, scope)["created_at"])

    def get_last_boundary_message_ids(self, chat_id: str, *, scope: str = MAIN_SCOPE) -> list[str]:
        with self._lock:
            group = self._group_state(chat_id)
            return list(self._boundary_state(group, scope)["message_ids"])

    def set_last_boundary_created_at(
        self,
        chat_id: str,
        created_at: int | str | None,
        *,
        scope: str = MAIN_SCOPE,
    ) -> int:
        normalized = max(int(created_at or 0), 0)
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            boundary = self._boundary_state(group, scope)
            if int(boundary["created_at"]) != normalized:
                boundary["message_ids"] = []
            boundary["created_at"] = normalized
            self._write_group_state(data, chat_id, group)
        return normalized

    def set_last_boundary_message_ids(
        self,
        chat_id: str,
        message_ids: list[str] | set[str],
        *,
        scope: str = MAIN_SCOPE,
    ) -> list[str]:
        normalized_message_ids = self._normalize_string_list(message_ids)
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            boundary = self._boundary_state(group, scope)
            boundary["message_ids"] = normalized_message_ids
            self._write_group_state(data, chat_id, group)
        return normalized_message_ids

    def set_last_boundary(
        self,
        chat_id: str,
        *,
        seq: int,
        created_at: int | str | None,
        message_ids: list[str] | set[str] | None = None,
        scope: str = MAIN_SCOPE,
    ) -> dict[str, Any]:
        normalized_seq = max(int(seq), 0)
        normalized_created_at = max(int(created_at or 0), 0)
        normalized_message_ids = self._normalize_string_list(message_ids or [])
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            boundary = self._boundary_state(group, scope)
            boundary["seq"] = normalized_seq
            boundary["created_at"] = normalized_created_at
            boundary["message_ids"] = normalized_message_ids
            self._write_group_state(data, chat_id, group)
        return {
            "last_boundary_seq": normalized_seq,
            "last_boundary_created_at": normalized_created_at,
            "last_boundary_message_ids": normalized_message_ids,
        }

    def append_message(self, chat_id: str, entry: GroupMessageEntry) -> int:
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            next_seq = int(group["last_log_seq"]) + 1
            group["last_log_seq"] = next_seq
            self._write_group_state(data, chat_id, group)

            payload: GroupMessageEntry = dict(entry)
            payload["seq"] = next_seq
            path = self._log_path(chat_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            return next_seq

    def read_messages_between(
        self,
        chat_id: str,
        *,
        after_seq: int = 0,
        before_seq: int | None = None,
        scope: str | None = None,
    ) -> list[GroupMessageEntry]:
        path = self._log_path(chat_id)
        if not path.exists():
            return []
        lower = max(int(after_seq), 0)
        upper = int(before_seq) if before_seq is not None else None
        normalized_scope = self.normalize_scope(scope) if scope is not None else ""
        messages: list[GroupMessageEntry] = []
        with self._lock:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(item, dict):
                        continue
                    seq = item.get("seq")
                    if not isinstance(seq, int):
                        continue
                    if seq <= lower:
                        continue
                    if upper is not None and seq >= upper:
                        continue
                    if normalized_scope:
                        entry_thread_id = str(item.get("thread_id", "") or "").strip()
                        if normalized_scope == MAIN_SCOPE:
                            if entry_thread_id:
                                continue
                        else:
                            scope_thread_id = normalized_scope.removeprefix("thread:")
                            if entry_thread_id != scope_thread_id:
                                continue
                    messages.append(item)
        return messages

    def group_snapshot(self, chat_id: str) -> GroupState:
        with self._lock:
            return self._clone_group_state(self._group_state(chat_id))

    def log_path(self, chat_id: str) -> pathlib.Path:
        return self._log_path(chat_id)

    def clear_chat(self, chat_id: str) -> bool:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return False
        removed = False
        with self._lock:
            data = self._read_all()
            if data["groups"].pop(normalized_chat_id, None) is not None:
                self._write_all(data)
                removed = True
        log_path = self._log_path(normalized_chat_id)
        if log_path.exists():
            log_path.unlink()
            removed = True
        return removed

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "group_chat_state.json"

    def _log_path(self, chat_id: str) -> pathlib.Path:
        safe_chat_id = chat_id.replace("/", "_")
        return self._data_dir / "group_chat_logs" / f"{safe_chat_id}.jsonl"

    @staticmethod
    def _normalize_string_list(values: Any) -> list[str]:
        if not isinstance(values, (list, set, tuple)):
            raise ValueError("expected string list")
        return sorted(
            {
                str(item).strip()
                for item in values
                if isinstance(item, str) and str(item).strip()
            }
        )

    @staticmethod
    def _default_boundary_state() -> BoundaryState:
        return {
            "seq": 0,
            "created_at": 0,
            "message_ids": [],
        }

    @classmethod
    def _default_group_state(cls) -> GroupState:
        return {
            "mode": DEFAULT_GROUP_MODE,
            "activated": False,
            "activated_by": "",
            "activated_at": 0,
            "boundaries": {MAIN_SCOPE: cls._default_boundary_state()},
            "last_log_seq": 0,
        }

    @classmethod
    def _default_store_data(cls) -> GroupChatStoreData:
        return {
            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
            "groups": {},
        }

    @classmethod
    def _group_with_activation_state(
        cls,
        group: GroupState,
        *,
        activated: bool,
        activated_by: str,
        activated_at: int,
    ) -> GroupState:
        updated = cls._clone_group_state(group)
        updated["activated"] = bool(activated)
        updated["activated_by"] = str(activated_by or "").strip()
        updated["activated_at"] = max(int(activated_at), 0)
        return updated

    def _boundary_state(self, group: GroupState, scope: str | None) -> BoundaryState:
        normalized_scope = self.normalize_scope(scope)
        boundary = group["boundaries"].get(normalized_scope)
        if boundary is None:
            boundary = self._default_boundary_state()
            group["boundaries"][normalized_scope] = boundary
        return boundary

    def _group_state(self, chat_id: str, *, data: GroupChatStoreData | None = None) -> GroupState:
        source = data if data is not None else self._read_all()
        raw = source["groups"].get(chat_id)
        if raw is None:
            return self._default_group_state()
        return self._clone_group_state(raw)

    def _read_all(self) -> GroupChatStoreData:
        path = self._state_path()
        if not path.exists():
            return self._default_store_data()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid {path.name}: failed to parse JSON") from exc
        return self._validate_store_data(raw)

    def _validate_store_data(self, raw: Any) -> GroupChatStoreData:
        if not isinstance(raw, dict):
            raise ValueError("invalid group_chat_state.json: root must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in SUPPORTED_GROUP_CHAT_STORE_SCHEMA_VERSIONS:
            raise ValueError(
                "invalid group_chat_state.json: "
                f"schema_version must be one of {sorted(SUPPORTED_GROUP_CHAT_STORE_SCHEMA_VERSIONS)}"
            )
        raw_groups = raw.get("groups")
        if not isinstance(raw_groups, dict):
            raise ValueError("invalid group_chat_state.json: groups must be an object")
        groups: dict[str, GroupState] = {}
        for chat_id, raw_group in raw_groups.items():
            if not isinstance(chat_id, str) or not chat_id.strip():
                raise ValueError("invalid group_chat_state.json: group chat_id must be a non-empty string")
            groups[chat_id] = self._validate_group_state(
                raw_group,
                chat_id=chat_id,
                schema_version=int(schema_version),
            )
        return {
            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
            "groups": groups,
        }

    def _validate_group_state(self, raw_group: Any, *, chat_id: str, schema_version: int) -> GroupState:
        if not isinstance(raw_group, dict):
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} must be an object")
        mode = str(raw_group.get("mode", "") or "").strip()
        if mode not in GROUP_MODES:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} has invalid mode")
        if schema_version >= GROUP_CHAT_STORE_SCHEMA_VERSION:
            activated = bool(raw_group.get("activated", False))
            activated_by = str(raw_group.get("activated_by", "") or "").strip()
            try:
                activated_at = max(int(raw_group.get("activated_at", 0) or 0), 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"invalid group_chat_state.json: group {chat_id} has invalid activated_at"
                ) from exc
        else:
            activated = False
            activated_by = ""
            activated_at = 0
        raw_boundaries = raw_group.get("boundaries")
        if not isinstance(raw_boundaries, dict):
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} boundaries must be an object")
        boundaries: dict[str, BoundaryState] = {}
        for scope, raw_boundary in raw_boundaries.items():
            normalized_scope = self.normalize_scope(scope)
            boundaries[normalized_scope] = self._validate_boundary_state(
                raw_boundary,
                chat_id=chat_id,
                scope=normalized_scope,
            )
        if MAIN_SCOPE not in boundaries:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} is missing main boundary")
        try:
            last_log_seq = max(int(raw_group.get("last_log_seq", 0) or 0), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} has invalid last_log_seq") from exc
        return {
            "mode": mode,
            "activated": activated,
            "activated_by": activated_by,
            "activated_at": activated_at,
            "boundaries": boundaries,
            "last_log_seq": last_log_seq,
        }

    def _validate_boundary_state(self, raw_boundary: Any, *, chat_id: str, scope: str) -> BoundaryState:
        if not isinstance(raw_boundary, dict):
            raise ValueError(
                f"invalid group_chat_state.json: group {chat_id} boundary {scope} must be an object"
            )
        try:
            seq = max(int(raw_boundary.get("seq", 0) or 0), 0)
            created_at = max(int(raw_boundary.get("created_at", 0) or 0), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid group_chat_state.json: group {chat_id} boundary {scope} has invalid counters"
            ) from exc
        try:
            message_ids = self._normalize_string_list(raw_boundary.get("message_ids", []))
        except ValueError as exc:
            raise ValueError(
                f"invalid group_chat_state.json: group {chat_id} boundary {scope} message_ids must be a string list"
            ) from exc
        return {
            "seq": seq,
            "created_at": created_at,
            "message_ids": message_ids,
        }

    def _write_group_state(self, data: GroupChatStoreData, chat_id: str, group: GroupState) -> None:
        data["groups"][chat_id] = self._clone_group_state(group)
        self._write_all(data)

    def _write_all(self, data: GroupChatStoreData) -> None:
        payload: GroupChatStoreData = {
            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
            "groups": {
                chat_id: self._clone_group_state(group)
                for chat_id, group in data["groups"].items()
            },
        }
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    @staticmethod
    def _clone_group_state(group: GroupState) -> GroupState:
        return {
            "mode": group["mode"],
            "activated": bool(group["activated"]),
            "activated_by": str(group["activated_by"] or ""),
            "activated_at": int(group["activated_at"]),
            "boundaries": {
                scope: {
                    "seq": int(boundary["seq"]),
                    "created_at": int(boundary["created_at"]),
                    "message_ids": list(boundary["message_ids"]),
                }
                for scope, boundary in group["boundaries"].items()
            },
            "last_log_seq": int(group["last_log_seq"]),
        }
