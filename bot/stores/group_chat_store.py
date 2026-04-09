"""
群聊工作态、权限与消息日志存储。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

DEFAULT_GROUP_MODE = "assistant"
DEFAULT_ACCESS_POLICY = "admin-only"
GROUP_MODES = {"mention_only", "assistant", "all"}
ACCESS_POLICIES = {"admin-only", "allowlist", "all-members"}
MAIN_SCOPE = "main"


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

    def grant_members(self, chat_id: str, user_ids: list[str] | set[str]) -> list[str]:
        granted = sorted({str(item).strip() for item in user_ids if str(item).strip()})
        if not granted:
            return []
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            allowlist = set(group["allowlist"])
            allowlist.update(granted)
            group["allowlist"] = sorted(allowlist)
            self._write_group_state(data, chat_id, group)
            return group["allowlist"]

    def revoke_members(self, chat_id: str, user_ids: list[str] | set[str]) -> list[str]:
        revoked = {str(item).strip() for item in user_ids if str(item).strip()}
        if not revoked:
            return sorted(self.get_allowlist(chat_id))
        with self._lock:
            data = self._read_all()
            group = self._group_state(chat_id, data=data)
            allowlist = set(group["allowlist"])
            allowlist.difference_update(revoked)
            group["allowlist"] = sorted(allowlist)
            self._write_group_state(data, chat_id, group)
            return group["allowlist"]

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
        normalized_message_ids = sorted(
            {
                str(item).strip()
                for item in message_ids
                if isinstance(item, str) and str(item).strip()
            }
        )
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
        normalized_message_ids = sorted(
            {
                str(item).strip()
                for item in (message_ids or [])
                if isinstance(item, str) and str(item).strip()
            }
        )
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

    def group_snapshot(self, chat_id: str) -> dict[str, Any]:
        with self._lock:
            return self._group_state(chat_id)

    def log_path(self, chat_id: str) -> pathlib.Path:
        return self._log_path(chat_id)

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "group_chat_state.json"

    def _log_path(self, chat_id: str) -> pathlib.Path:
        safe_chat_id = chat_id.replace("/", "_")
        return self._data_dir / "group_chat_logs" / f"{safe_chat_id}.jsonl"

    def _boundary_state(self, group: dict[str, Any], scope: str | None) -> dict[str, Any]:
        normalized_scope = self.normalize_scope(scope)
        boundaries = group.setdefault("boundaries", {})
        boundary = boundaries.get(normalized_scope, {})
        sanitized = {
            "seq": max(int(boundary.get("seq", 0) or 0), 0),
            "created_at": max(int(boundary.get("created_at", 0) or 0), 0),
            "message_ids": sorted(
                {
                    str(item).strip()
                    for item in boundary.get("message_ids", [])
                    if isinstance(item, str) and str(item).strip()
                }
            ),
        }
        boundaries[normalized_scope] = sanitized
        return sanitized

    def _group_state(self, chat_id: str, *, data: dict[str, Any] | None = None) -> dict[str, Any]:
        source = data if data is not None else self._read_all()
        groups = source.get("groups", {})
        raw = groups.get(chat_id, {})
        allowlist = raw.get("allowlist", [])
        mode = raw.get("mode", DEFAULT_GROUP_MODE)
        access_policy = raw.get("access_policy", DEFAULT_ACCESS_POLICY)
        raw_boundaries = raw.get("boundaries", {})
        boundaries: dict[str, dict[str, Any]] = {}
        if isinstance(raw_boundaries, dict):
            for scope, boundary in raw_boundaries.items():
                if not isinstance(boundary, dict):
                    continue
                normalized_scope = self.normalize_scope(scope)
                boundaries[normalized_scope] = {
                    "seq": max(int(boundary.get("seq", 0) or 0), 0),
                    "created_at": max(int(boundary.get("created_at", 0) or 0), 0),
                    "message_ids": sorted(
                        {
                            str(item).strip()
                            for item in boundary.get("message_ids", [])
                            if isinstance(item, str) and str(item).strip()
                        }
                    ),
                }
        if MAIN_SCOPE not in boundaries:
            boundaries[MAIN_SCOPE] = {
                "seq": max(int(raw.get("last_boundary_seq", 0) or 0), 0),
                "created_at": max(int(raw.get("last_boundary_created_at", 0) or 0), 0),
                "message_ids": sorted(
                    {
                        str(item).strip()
                        for item in raw.get("last_boundary_message_ids", [])
                        if isinstance(item, str) and str(item).strip()
                    }
                ),
            }
        return {
            "mode": mode if mode in GROUP_MODES else DEFAULT_GROUP_MODE,
            "access_policy": access_policy if access_policy in ACCESS_POLICIES else DEFAULT_ACCESS_POLICY,
            "allowlist": sorted(
                {
                    str(item).strip()
                    for item in allowlist
                    if isinstance(item, str) and str(item).strip()
                }
            ),
            "boundaries": boundaries,
            "last_log_seq": max(int(raw.get("last_log_seq", 0) or 0), 0),
        }

    def _read_all(self) -> dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return {"groups": {}}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"groups": {}}
        if not isinstance(raw, dict):
            return {"groups": {}}
        groups = raw.get("groups", {})
        if not isinstance(groups, dict):
            groups = {}
        return {"groups": groups}

    def _write_group_state(self, data: dict[str, Any], chat_id: str, group: dict[str, Any]) -> None:
        groups = data.setdefault("groups", {})
        groups[chat_id] = {
            "mode": group["mode"],
            "access_policy": group["access_policy"],
            "allowlist": list(group["allowlist"]),
            "boundaries": {
                self.normalize_scope(scope): {
                    "seq": int(boundary["seq"]),
                    "created_at": int(boundary["created_at"]),
                    "message_ids": list(boundary["message_ids"]),
                }
                for scope, boundary in group.get("boundaries", {}).items()
                if isinstance(boundary, dict)
            },
            "last_log_seq": int(group["last_log_seq"]),
        }
        self._write_all(data)

    def _write_all(self, data: dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
