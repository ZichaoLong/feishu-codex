"""Stable admin-facing identifiers for Feishu chat bindings."""

from __future__ import annotations

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID

ChatBindingKey = tuple[str, str]


def binding_kind(binding: ChatBindingKey) -> str:
    return "group" if binding[0] == GROUP_SHARED_BINDING_OWNER_ID else "p2p"


def format_binding_id(binding: ChatBindingKey) -> str:
    sender_id, chat_id = binding
    if binding_kind(binding) == "group":
        return f"group:{chat_id}"
    return f"p2p:{sender_id}:{chat_id}"


def parse_binding_id(binding_id: str) -> ChatBindingKey:
    normalized = str(binding_id or "").strip()
    if normalized.startswith("group:"):
        chat_id = normalized[len("group:") :].strip()
        if chat_id:
            return (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
        raise ValueError("group binding_id 缺少 chat_id")
    if normalized.startswith("p2p:"):
        parts = normalized.split(":", 2)
        if len(parts) == 3 and parts[1].strip() and parts[2].strip():
            return (parts[1].strip(), parts[2].strip())
        raise ValueError("p2p binding_id 必须是 p2p:<sender_id>:<chat_id>")
    raise ValueError("binding_id 必须是 group:<chat_id> 或 p2p:<sender_id>:<chat_id>")
