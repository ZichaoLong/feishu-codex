from __future__ import annotations

from typing import NotRequired, TypedDict


class MentionPayload(TypedDict):
    key: str
    name: str
    open_id: str


class MentionMember(TypedDict):
    open_id: str
    name: str


class MessageContextPayload(TypedDict, total=False):
    chat_id: str
    chat_type: str
    sender_user_id: str
    sender_open_id: str
    sender_type: str
    bot_mentioned: bool
    message_type: str
    thread_id: str
    root_id: str
    parent_id: str
    text: str
    mentions: list[MentionPayload]


class GroupMessageEntry(TypedDict):
    message_id: str
    created_at: int
    sender_user_id: str
    sender_principal_id: str
    sender_type: str
    sender_name: str
    msg_type: str
    thread_id: str
    text: str
    seq: NotRequired[int]


class GroupAclSnapshot(TypedDict):
    access_policy: str
    allowlist: list[str]


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


class StoredChatBinding(TypedDict):
    working_dir: str
    current_thread_id: str
    current_thread_title: str
    approval_policy: str
    sandbox: str
    collaboration_mode: str


class ChatBindingsFileData(TypedDict):
    schema_version: int
    p2p_bindings: dict[str, dict[str, StoredChatBinding]]
    group_bindings: dict[str, StoredChatBinding]


class BotIdentitySnapshot(TypedDict):
    app_id: str
    configured_open_id: str
    discovered_open_id: str
    trigger_open_ids: list[str]
