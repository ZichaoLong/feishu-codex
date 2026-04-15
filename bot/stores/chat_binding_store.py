"""
Feishu 绑定级持久化状态。

这里只保存“重启后仍应保留”的本地绑定事实：
- 当前工作目录
- 当前绑定 thread
- 当前线程标题
- 当前 thread 的写租约所属绑定（如果有）
- 当前飞书会话的权限 / 协作模式设置

运行中的 turn、执行卡片、审批请求等瞬时状态不落盘。
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.feishu_types import ChatBindingsFileData, StoredChatBinding

CHAT_BINDING_STORE_SCHEMA_VERSION = 2
_CHAT_BINDING_STORE_COMPAT_SCHEMA_VERSIONS = {1, CHAT_BINDING_STORE_SCHEMA_VERSION}


class ChatBindingStore:
    def __init__(self, data_dir: pathlib.Path):
        self._data_dir = data_dir
        self._lock = threading.Lock()

    def load(self, binding: tuple[str, str]) -> StoredChatBinding | None:
        normalized_binding = self._normalize_binding(binding)
        with self._lock:
            data = self._read_all()
            return self._load_from_data(data, normalized_binding)

    def load_all(self) -> dict[tuple[str, str], StoredChatBinding]:
        with self._lock:
            data = self._read_all()
            bindings: dict[tuple[str, str], StoredChatBinding] = {}
            for chat_id, raw_chat_bindings in data["p2p_bindings"].items():
                for sender_open_id, state in raw_chat_bindings.items():
                    bindings[(sender_open_id, chat_id)] = dict(state)
            for chat_id, state in data["group_bindings"].items():
                bindings[(GROUP_SHARED_BINDING_OWNER_ID, chat_id)] = dict(state)
            return bindings

    def save(self, binding: tuple[str, str], state: StoredChatBinding) -> StoredChatBinding:
        normalized_binding = self._normalize_binding(binding)
        normalized_state = self._validate_stored_binding(state)
        with self._lock:
            data = self._read_all()
            self._save_to_data(data, normalized_binding, normalized_state)
            self._write_all(data)
        return dict(normalized_state)

    def clear(self, binding: tuple[str, str]) -> None:
        normalized_binding = self._normalize_binding(binding)
        with self._lock:
            data = self._read_all()
            if not self._clear_from_data(data, normalized_binding):
                return
            self._write_all(data)

    def clear_all(self) -> None:
        with self._lock:
            self._delete_file()

    def _state_path(self) -> pathlib.Path:
        return self._data_dir / "chat_bindings.json"

    @staticmethod
    def _normalize_binding(binding: tuple[str, str]) -> tuple[str, str]:
        binding_owner_id = str(binding[0] or "").strip()
        chat_id = str(binding[1] or "").strip()
        if not chat_id:
            raise ValueError("chat_id 不能为空")
        if not binding_owner_id:
            raise ValueError("binding_owner_id 不能为空")
        return binding_owner_id, chat_id

    @staticmethod
    def _default_data() -> ChatBindingsFileData:
        return {
            "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION,
            "p2p_bindings": {},
            "group_bindings": {},
        }

    def _read_all(self) -> ChatBindingsFileData:
        path = self._state_path()
        if not path.exists():
            return self._default_data()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"invalid chat_bindings.json: {exc}") from exc
        return self._validate_store_data(raw)

    def _write_all(self, data: ChatBindingsFileData) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))

    def _delete_file(self) -> None:
        path = self._state_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _validate_store_data(self, raw: Any) -> ChatBindingsFileData:
        if not isinstance(raw, dict):
            raise ValueError("invalid chat_bindings.json: root must be an object")
        schema_version = raw.get("schema_version")
        if schema_version not in _CHAT_BINDING_STORE_COMPAT_SCHEMA_VERSIONS:
            raise ValueError(
                "invalid chat_bindings.json: "
                f"schema_version must be one of {sorted(_CHAT_BINDING_STORE_COMPAT_SCHEMA_VERSIONS)}"
            )

        raw_p2p = raw.get("p2p_bindings", {})
        raw_groups = raw.get("group_bindings", {})
        if not isinstance(raw_p2p, dict):
            raise ValueError("invalid chat_bindings.json: p2p_bindings must be an object")
        if not isinstance(raw_groups, dict):
            raise ValueError("invalid chat_bindings.json: group_bindings must be an object")

        p2p_bindings: dict[str, dict[str, StoredChatBinding]] = {}
        for chat_id, raw_chat_bindings in raw_p2p.items():
            normalized_chat_id = str(chat_id or "").strip()
            if not normalized_chat_id:
                raise ValueError("invalid chat_bindings.json: p2p chat_id cannot be empty")
            if not isinstance(raw_chat_bindings, dict):
                raise ValueError(
                    f"invalid chat_bindings.json: p2p_bindings[{normalized_chat_id}] must be an object"
                )
            validated_chat_bindings: dict[str, StoredChatBinding] = {}
            for sender_open_id, raw_state in raw_chat_bindings.items():
                normalized_sender_open_id = str(sender_open_id or "").strip()
                if not normalized_sender_open_id:
                    raise ValueError(
                        "invalid chat_bindings.json: p2p sender_open_id cannot be empty"
                    )
                validated_chat_bindings[normalized_sender_open_id] = self._validate_stored_binding(raw_state)
            p2p_bindings[normalized_chat_id] = validated_chat_bindings

        group_bindings: dict[str, StoredChatBinding] = {}
        for chat_id, raw_state in raw_groups.items():
            normalized_chat_id = str(chat_id or "").strip()
            if not normalized_chat_id:
                raise ValueError("invalid chat_bindings.json: group chat_id cannot be empty")
            group_bindings[normalized_chat_id] = self._validate_stored_binding(raw_state)

        return {
            "schema_version": CHAT_BINDING_STORE_SCHEMA_VERSION,
            "p2p_bindings": p2p_bindings,
            "group_bindings": group_bindings,
        }

    @staticmethod
    def _validate_stored_binding(raw_state: Any) -> StoredChatBinding:
        if not isinstance(raw_state, dict):
            raise ValueError("invalid chat_bindings.json: binding state must be an object")
        fields = {
            "working_dir": raw_state.get("working_dir", ""),
            "current_thread_id": raw_state.get("current_thread_id", ""),
            "current_thread_title": raw_state.get("current_thread_title", ""),
            "current_thread_runtime_state": raw_state.get("current_thread_runtime_state", ""),
            "current_thread_write_owner_thread_id": raw_state.get("current_thread_write_owner_thread_id", ""),
            "approval_policy": raw_state.get("approval_policy", ""),
            "sandbox": raw_state.get("sandbox", ""),
            "collaboration_mode": raw_state.get("collaboration_mode", ""),
        }
        normalized: StoredChatBinding = {}
        for key, value in fields.items():
            if not isinstance(value, str):
                raise ValueError(f"invalid chat_bindings.json: {key} must be a string")
            normalized[key] = value.strip()
        current_thread_id = normalized["current_thread_id"]
        runtime_state = normalized["current_thread_runtime_state"]
        if current_thread_id:
            if not runtime_state:
                normalized["current_thread_runtime_state"] = "attached"
            elif runtime_state not in {"attached", "released"}:
                raise ValueError(
                    "invalid chat_bindings.json: current_thread_runtime_state must be attached or released"
                )
        else:
            normalized["current_thread_runtime_state"] = ""
        if (
            normalized["current_thread_write_owner_thread_id"]
            and normalized["current_thread_write_owner_thread_id"] != current_thread_id
        ):
            raise ValueError(
                "invalid chat_bindings.json: current_thread_write_owner_thread_id must match current_thread_id"
            )
        if normalized["current_thread_runtime_state"] != "attached":
            normalized["current_thread_write_owner_thread_id"] = ""
        return normalized

    @staticmethod
    def _load_from_data(
        data: ChatBindingsFileData,
        binding: tuple[str, str],
    ) -> StoredChatBinding | None:
        sender_id, chat_id = binding
        if sender_id == GROUP_SHARED_BINDING_OWNER_ID:
            state = data["group_bindings"].get(chat_id)
            return dict(state) if state is not None else None
        chat_bindings = data["p2p_bindings"].get(chat_id, {})
        state = chat_bindings.get(sender_id)
        return dict(state) if state is not None else None

    @staticmethod
    def _save_to_data(
        data: ChatBindingsFileData,
        binding: tuple[str, str],
        state: StoredChatBinding,
    ) -> None:
        sender_id, chat_id = binding
        if sender_id == GROUP_SHARED_BINDING_OWNER_ID:
            data["group_bindings"][chat_id] = dict(state)
            return
        chat_bindings = data["p2p_bindings"].setdefault(chat_id, {})
        chat_bindings[sender_id] = dict(state)

    @staticmethod
    def _clear_from_data(
        data: ChatBindingsFileData,
        binding: tuple[str, str],
    ) -> bool:
        sender_id, chat_id = binding
        if sender_id == GROUP_SHARED_BINDING_OWNER_ID:
            return data["group_bindings"].pop(chat_id, None) is not None
        chat_bindings = data["p2p_bindings"].get(chat_id)
        if not chat_bindings:
            return False
        removed = chat_bindings.pop(sender_id, None) is not None
        if not chat_bindings:
            data["p2p_bindings"].pop(chat_id, None)
        return removed
