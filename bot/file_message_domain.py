"""
File message ingress domain.

This keeps file-message behavior out of CodexHandler's core turn lifecycle so
future support can evolve behind a dedicated boundary: validate attachment
metadata, optionally download the file, stage it into the workspace, then turn
it into Codex input. For now the default implementation is explicit rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IncomingFileMessage:
    sender_id: str
    chat_id: str
    message_id: str
    file_key: str
    file_name: str


class _FileMessageOwner(Protocol):
    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...


class FileMessageDomain:
    """Dedicated ingress point for file-message support."""

    def __init__(self, owner: _FileMessageOwner) -> None:
        self._owner = owner

    def handle_message(self, incoming: IncomingFileMessage) -> None:
        file_name = incoming.file_name or "未命名文件"
        self._owner._reply_text(
            incoming.chat_id,
            (
                f"暂不支持直接处理文件消息：`{file_name}`。\n"
                "当前请先发送文字说明；后续文件能力会接到独立文件入口，再支持下载、暂存和交给 Codex 处理。"
            ),
            message_id=incoming.message_id,
        )
