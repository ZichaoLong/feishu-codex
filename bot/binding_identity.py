"""Stable admin-facing identifiers for Feishu chat bindings."""

from __future__ import annotations

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID

ChatBindingKey = tuple[str, str]


def _validate_binding_component(name: str, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if ":" in normalized:
        raise ValueError(f"{name} 不能包含 ':'")
    return normalized


def binding_kind(binding: ChatBindingKey) -> str:
    return "group" if binding[0] == GROUP_SHARED_BINDING_OWNER_ID else "p2p"


def format_binding_id(binding: ChatBindingKey) -> str:
    sender_id, chat_id = binding
    normalized_chat_id = _validate_binding_component("chat_id", chat_id)
    if binding_kind(binding) == "group":
        return f"group:{normalized_chat_id}"
    normalized_sender_id = _validate_binding_component("sender_id", sender_id)
    return f"p2p:{normalized_sender_id}:{normalized_chat_id}"


def parse_binding_id(binding_id: str) -> ChatBindingKey:
    normalized = str(binding_id or "").strip()
    if normalized.startswith("group:"):
        chat_id = normalized[len("group:") :].strip()
        return (GROUP_SHARED_BINDING_OWNER_ID, _validate_binding_component("chat_id", chat_id))
    if normalized.startswith("p2p:"):
        parts = normalized.split(":", 2)
        if len(parts) != 3:
            raise ValueError("p2p binding_id 必须是 p2p:<sender_id>:<chat_id>")
        return (
            _validate_binding_component("sender_id", parts[1]),
            _validate_binding_component("chat_id", parts[2]),
        )
    raise ValueError("binding_id 必须是 group:<chat_id> 或 p2p:<sender_id>:<chat_id>")
