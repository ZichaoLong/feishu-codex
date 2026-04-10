"""
群聊工作态、权限与消息日志存储。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any, TypedDict

DEFAULT_GROUP_MODE = "assistant"
DEFAULT_ACCESS_POLICY = "admin-only"
GROUP_MODES = {"mention_only", "assistant", "all"}
ACCESS_POLICIES = {"admin-only", "allowlist", "all-members"}
MAIN_SCOPE = "main"
GROUP_CHAT_STORE_SCHEMA_VERSION = 1


class BoundaryState(TypedDict):
    seq: int
    created_at: int
    message_ids: list[str]


class GroupState(TypedDict):
    mode: str
    access_policy: str
    allowlist: list[str]
    boundaries: dict[str, BoundaryState]
    last_log_seq: int


class GroupChatStoreData(TypedDict):
    schema_version: int
    groups: dict[str, GroupState]


class GroupChatStore:
    """管理群聊模式、ACL 与助理模式消息日志。"""

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

    def get_access_policy(self, chat_id: str) -> str:
        with self._lock:
            group = self._group_state(chat_id)
            return group["access_policy"]

    def set_access_policy(self, chat_id: str, policy: str) -> str:
        normalized = str(policy or "").strip().lower()
        if normalized not in ACCESS_POLICIES:
            raise ValueError(f"invalid access policy: {policy}")
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            group["access_policy"] = normalized
            self._write_group_state(data, chat_id, group)
        return normalized

    def get_allowlist(self, chat_id: str) -> set[str]:
        with self._lock:
            group = self._group_state(chat_id)
            return set(group["allowlist"])

    def grant_members(self, chat_id: str, open_ids: list[str] | set[str]) -> list[str]:
        granted = sorted({str(item).strip() for item in open_ids if str(item).strip()})
        if not granted:
            return []
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            allowlist = set(group["allowlist"])
            allowlist.update(granted)
            group["allowlist"] = sorted(allowlist)
            self._write_group_state(data, chat_id, group)
            return list(group["allowlist"])

    def revoke_members(self, chat_id: str, open_ids: list[str] | set[str]) -> list[str]:
        revoked = {str(item).strip() for item in open_ids if str(item).strip()}
        if not revoked:
            return sorted(self.get_allowlist(chat_id))
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            allowlist = set(group["allowlist"])
            allowlist.difference_update(revoked)
            group["allowlist"] = sorted(allowlist)
            self._write_group_state(data, chat_id, group)
            return list(group["allowlist"])

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

    def append_message(self, chat_id: str, entry: dict[str, Any]) -> int:
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            next_seq = int(group["last_log_seq"]) + 1
            group["last_log_seq"] = next_seq
            self._write_group_state(data, chat_id, group)

            payload = dict(entry)
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
    ) -> list[dict[str, Any]]:
        path = self._log_path(chat_id)
        if not path.exists():
            return []
        lower = max(int(after_seq), 0)
        upper = int(before_seq) if before_seq is not None else None
        normalized_scope = self.normalize_scope(scope) if scope is not None else ""
        messages: list[dict[str, Any]] = []
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
            "access_policy": DEFAULT_ACCESS_POLICY,
            "allowlist": [],
            "boundaries": {MAIN_SCOPE: cls._default_boundary_state()},
            "last_log_seq": 0,
        }

    @classmethod
    def _default_store_data(cls) -> GroupChatStoreData:
        return {
            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
            "groups": {},
        }

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
        if schema_version != GROUP_CHAT_STORE_SCHEMA_VERSION:
            raise ValueError(
                "invalid group_chat_state.json: "
                f"schema_version must be {GROUP_CHAT_STORE_SCHEMA_VERSION}"
            )
        raw_groups = raw.get("groups")
        if not isinstance(raw_groups, dict):
            raise ValueError("invalid group_chat_state.json: groups must be an object")
        groups: dict[str, GroupState] = {}
        for chat_id, raw_group in raw_groups.items():
            if not isinstance(chat_id, str) or not chat_id.strip():
                raise ValueError("invalid group_chat_state.json: group chat_id must be a non-empty string")
            groups[chat_id] = self._validate_group_state(raw_group, chat_id=chat_id)
        return {
            "schema_version": GROUP_CHAT_STORE_SCHEMA_VERSION,
            "groups": groups,
        }

    def _validate_group_state(self, raw_group: Any, *, chat_id: str) -> GroupState:
        if not isinstance(raw_group, dict):
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} must be an object")
        mode = str(raw_group.get("mode", "") or "").strip()
        if mode not in GROUP_MODES:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} has invalid mode")
        access_policy = str(raw_group.get("access_policy", "") or "").strip()
        if access_policy not in ACCESS_POLICIES:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} has invalid access_policy")
        try:
            allowlist = self._normalize_string_list(raw_group.get("allowlist", []))
        except ValueError as exc:
            raise ValueError(f"invalid group_chat_state.json: group {chat_id} allowlist must be a string list") from exc
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
            "access_policy": access_policy,
            "allowlist": allowlist,
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
            "access_policy": group["access_policy"],
            "allowlist": list(group["allowlist"]),
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
