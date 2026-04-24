"""
Attachment ingress domain.

This domain owns the Feishu-side attachment lifecycle:
- validate attachment messages
- download Feishu resources
- stage them into the current workspace
- persist pending attachments until the next prompt consumes them
"""

from __future__ import annotations

import mimetypes
import os
import pathlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.adapters.base import LocalImageTurnInputItem, TextTurnInputItem, TurnInputItem
from bot.constants import display_path
from bot.runtime_view import RuntimeView
from bot.stores.pending_attachment_store import PendingAttachmentRecord, PendingAttachmentStore

_ATTACHMENT_STAGE_DIRNAME = "_feishu_attachments"
_DOWNLOADABLE_ATTACHMENT_TYPES = {"image", "file", "audio", "media"}
_UNSUPPORTED_ATTACHMENT_TYPES = {
    "folder": "文件夹消息当前无法通过飞书 API 下载，暂不支持接入当前工作区。",
    "sticker": "表情包消息当前无法通过飞书 API 下载，暂不支持作为附件输入。",
    "merge_forward": "合并转发里的子附件当前无法通过飞书 API 下载，暂不支持作为附件输入。",
    "interactive": "卡片里的资源当前无法通过飞书 API 下载，暂不支持作为附件输入。",
}
_ATTACHMENT_TYPE_LABELS = {
    "image": "图片",
    "file": "文件",
    "audio": "音频",
    "media": "媒体",
}


@dataclass(frozen=True, slots=True)
class IncomingAttachmentMessage:
    sender_id: str
    chat_id: str
    message_id: str
    thread_id: str
    attachment_type: str
    resource_key: str
    display_name: str


@dataclass(frozen=True, slots=True)
class PreparedPromptInput:
    input_items: tuple[TurnInputItem, ...]
    consumed_attachments: tuple[PendingAttachmentRecord, ...] = ()
    blocking_text: str = ""


@dataclass(frozen=True, slots=True)
class FileMessagePorts:
    get_message_context: Callable[[str], dict[str, Any]]
    download_message_resource: Callable[..., Any]
    reply_text: Callable[..., None]
    get_runtime_view: Callable[[str, str, str], RuntimeView]
    message_reply_in_thread: Callable[[str], bool]


class FileMessageDomain:
    """Dedicated ingress point for Feishu attachment messages."""

    def __init__(
        self,
        *,
        ports: FileMessagePorts,
        store: PendingAttachmentStore,
        ttl_seconds: float,
    ) -> None:
        self._ports = ports
        self._store = store
        self._ttl_seconds = max(float(ttl_seconds), 1.0)

    def handle_message(self, incoming: IncomingAttachmentMessage) -> None:
        self._cleanup_expired_attachments()

        attachment_type = str(incoming.attachment_type or "").strip().lower()
        if attachment_type in _UNSUPPORTED_ATTACHMENT_TYPES:
            self._reply_attachment_rejected(
                incoming,
                _UNSUPPORTED_ATTACHMENT_TYPES[attachment_type],
            )
            return
        if attachment_type not in _DOWNLOADABLE_ATTACHMENT_TYPES:
            self._reply_attachment_rejected(
                incoming,
                f"暂不支持接入 `{attachment_type or 'unknown'}` 类型附件。",
            )
            return
        if not str(incoming.resource_key or "").strip():
            self._reply_attachment_rejected(
                incoming,
                f"{self._attachment_label(attachment_type)}消息缺少资源 key，无法下载，请重新发送。",
            )
            return

        runtime = self._ports.get_runtime_view(
            incoming.sender_id,
            incoming.chat_id,
            incoming.message_id,
        )
        working_dir = pathlib.Path(str(runtime.working_dir or "")).expanduser()
        if not working_dir.exists():
            self._reply_attachment_rejected(
                incoming,
                f"当前工作目录不存在：`{working_dir}`",
            )
            return
        if not working_dir.is_dir():
            self._reply_attachment_rejected(
                incoming,
                f"当前工作目录不是目录：`{working_dir}`",
            )
            return

        try:
            downloaded = self._ports.download_message_resource(
                incoming.message_id,
                incoming.resource_key,
                resource_type=self._download_resource_type(attachment_type),
            )
        except Exception as exc:
            self._reply_attachment_rejected(
                incoming,
                f"下载{self._attachment_label(attachment_type)}失败：{exc}",
            )
            return

        try:
            staged_path = self._stage_downloaded_attachment(
                working_dir=working_dir,
                attachment_type=attachment_type,
                message_id=incoming.message_id,
                display_name=incoming.display_name,
                downloaded_name=downloaded.file_name,
                content_type=downloaded.content_type,
                content=downloaded.content,
            )
        except Exception as exc:
            self._reply_attachment_rejected(
                incoming,
                f"保存{self._attachment_label(attachment_type)}到本地失败：{exc}",
            )
            return

        now = time.time()
        display_name = self._resolve_display_name(
            attachment_type=attachment_type,
            display_name=incoming.display_name,
            downloaded_name=downloaded.file_name,
            content_type=downloaded.content_type,
        )
        record = PendingAttachmentRecord(
            sender_id=incoming.sender_id,
            chat_id=incoming.chat_id,
            thread_id=incoming.thread_id,
            message_id=incoming.message_id,
            attachment_type=attachment_type,
            resource_key=incoming.resource_key,
            display_name=display_name,
            local_path=str(staged_path),
            created_at=now,
            expires_at=now + self._ttl_seconds,
        )
        self._store.add(record)
        pending_count = self._pending_count_for_key(
            sender_id=incoming.sender_id,
            chat_id=incoming.chat_id,
            thread_id=incoming.thread_id,
            now=now,
        )
        saved_path = display_path(str(staged_path), str(working_dir))
        suffix = f"\n当前待消费附件：{pending_count} 个。" if pending_count > 1 else ""
        self._ports.reply_text(
            incoming.chat_id,
            (
                f"{self._attachment_label(attachment_type)}已保存到本地：`{saved_path}`\n"
                "继续发送一条文字说明即可；我会把这些本地路径交给 Codex 处理。"
                f"{suffix}"
            ),
            message_id=incoming.message_id,
            reply_in_thread=self._ports.message_reply_in_thread(incoming.message_id),
        )

    def prepare_prompt_input(
        self,
        *,
        sender_id: str,
        chat_id: str,
        message_id: str,
        text: str,
    ) -> PreparedPromptInput:
        now = time.time()
        attachments, expired = self._take_pending_attachments_for_message(
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            now=now,
        )
        self._delete_local_files(expired)
        if not attachments:
            if expired:
                return PreparedPromptInput(
                    input_items=(),
                    blocking_text="附件已过期，请重新发送后再补充说明。",
                )
            return PreparedPromptInput(input_items=(self._text_input_item(text),))

        runtime = self._ports.get_runtime_view(sender_id, chat_id, message_id)
        working_dir = pathlib.Path(str(runtime.working_dir or "")).expanduser()
        blocking_text = self._validate_pending_attachments(
            attachments,
            working_dir=working_dir,
        )
        if blocking_text:
            return PreparedPromptInput(
                input_items=(),
                blocking_text=blocking_text,
            )

        prompt_text = self._compose_prompt_text(text, attachments)
        input_items: list[TurnInputItem] = [self._text_input_item(prompt_text)]
        for record in attachments:
            if record.attachment_type == "image":
                input_items.append(self._local_image_input_item(record.local_path))
        return PreparedPromptInput(
            input_items=tuple(input_items),
            consumed_attachments=attachments,
        )

    def restore_consumed_attachments(self, records: tuple[PendingAttachmentRecord, ...]) -> None:
        if records:
            self._store.add_many(records)

    def invalidate_pending_attachments_for_scope(
        self,
        *,
        sender_id: str,
        chat_id: str,
        message_id: str,
    ) -> int:
        attachments, expired = self._take_pending_attachments_for_message(
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            now=time.time(),
        )
        self._delete_local_files(expired)
        return len(attachments)

    def _cleanup_expired_attachments(self) -> None:
        expired = self._store.cleanup_expired(now=time.time())
        self._delete_local_files(expired)

    @staticmethod
    def _compose_prompt_text(text: str, attachments: tuple[PendingAttachmentRecord, ...]) -> str:
        lines = ["已接收来自飞书的本地附件："]
        for record in attachments:
            label = _ATTACHMENT_TYPE_LABELS.get(record.attachment_type, record.attachment_type or "附件")
            lines.append(f"- [{record.attachment_type}] {record.display_name or label} -> {record.local_path}")
        lines.append("")
        lines.append("请结合这些本地附件完成下面的用户请求。")
        lines.append("用户请求：")
        lines.append(text)
        return "\n".join(lines)

    def _take_pending_attachments_for_message(
        self,
        *,
        sender_id: str,
        chat_id: str,
        message_id: str,
        now: float,
    ) -> tuple[tuple[PendingAttachmentRecord, ...], tuple[PendingAttachmentRecord, ...]]:
        context = self._ports.get_message_context(message_id) if message_id else {}
        thread_id = str(context.get("thread_id", "") or "").strip()
        return self._store.take(
            sender_id=sender_id,
            chat_id=chat_id,
            thread_id=thread_id,
            now=now,
        )

    def _validate_pending_attachments(
        self,
        attachments: tuple[PendingAttachmentRecord, ...],
        *,
        working_dir: pathlib.Path,
    ) -> str:
        expected_stage_dir = (working_dir / _ATTACHMENT_STAGE_DIRNAME).resolve()
        missing_local_file = False
        workspace_mismatch = False
        for record in attachments:
            local_path = pathlib.Path(record.local_path)
            if not local_path.exists():
                missing_local_file = True
                continue
            try:
                stage_dir = local_path.parent.resolve()
            except OSError:
                missing_local_file = True
                continue
            if stage_dir != expected_stage_dir:
                workspace_mismatch = True
        if missing_local_file and workspace_mismatch:
            return "待消费附件已失效，请重新发送需要处理的全部附件后再试。"
        if workspace_mismatch:
            return "待消费附件属于其他工作目录，切换目录后已失效，请在当前目录重新发送需要处理的全部附件后再试。"
        if missing_local_file:
            return "附件暂存集合已不完整，请重新发送需要处理的全部附件后再试。"
        return ""

    def _pending_count_for_key(
        self,
        *,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        now: float,
    ) -> int:
        return sum(
            1
            for record in self._store.list_all()
            if record.sender_id == sender_id
            and record.chat_id == chat_id
            and record.thread_id == thread_id
            and record.expires_at > now
        )

    def _reply_attachment_rejected(self, incoming: IncomingAttachmentMessage, reason: str) -> None:
        display_name = incoming.display_name or self._attachment_label(incoming.attachment_type)
        self._ports.reply_text(
            incoming.chat_id,
            f"无法接入附件：`{display_name}`\n{reason}",
            message_id=incoming.message_id,
            reply_in_thread=self._ports.message_reply_in_thread(incoming.message_id),
        )

    def _stage_downloaded_attachment(
        self,
        *,
        working_dir: pathlib.Path,
        attachment_type: str,
        message_id: str,
        display_name: str,
        downloaded_name: str,
        content_type: str,
        content: bytes,
    ) -> pathlib.Path:
        stage_dir = working_dir / _ATTACHMENT_STAGE_DIRNAME
        stage_dir.mkdir(parents=True, exist_ok=True)
        file_name = self._build_staged_file_name(
            attachment_type=attachment_type,
            message_id=message_id,
            display_name=display_name,
            downloaded_name=downloaded_name,
            content_type=content_type,
        )
        path = stage_dir / file_name
        original_path = pathlib.Path(file_name)
        base_stem = original_path.name[: -len("".join(original_path.suffixes))] if original_path.suffixes else original_path.name
        suffix_text = "".join(original_path.suffixes)
        suffix = 1
        while path.exists():
            path = stage_dir / f"{base_stem}-{suffix}{suffix_text}"
            suffix += 1
        path.write_bytes(content)
        return path.resolve()

    def _build_staged_file_name(
        self,
        *,
        attachment_type: str,
        message_id: str,
        display_name: str,
        downloaded_name: str,
        content_type: str,
    ) -> str:
        resolved_name = self._resolve_display_name(
            attachment_type=attachment_type,
            display_name=display_name,
            downloaded_name=downloaded_name,
            content_type=content_type,
        )
        base_name = pathlib.Path(resolved_name).name
        stem = pathlib.Path(base_name).stem
        safe_stem = self._sanitize_file_stem(stem) or attachment_type
        safe_stem = safe_stem[:80]
        safe_suffixes = "".join(self._sanitize_suffix(part) for part in pathlib.Path(base_name).suffixes)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        message_suffix = re.sub(r"[^A-Za-z0-9]+", "", str(message_id or ""))[-10:] or "msg"
        return f"{timestamp}-{message_suffix}-{safe_stem}{safe_suffixes}"

    def _resolve_display_name(
        self,
        *,
        attachment_type: str,
        display_name: str,
        downloaded_name: str,
        content_type: str,
    ) -> str:
        for candidate in (display_name, downloaded_name):
            normalized = pathlib.Path(str(candidate or "").strip()).name
            if normalized:
                return normalized
        extension = ""
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if normalized_content_type:
            extension = mimetypes.guess_extension(normalized_content_type) or ""
        if attachment_type == "image" and extension == ".jpe":
            extension = ".jpg"
        return f"{attachment_type or 'attachment'}{extension}"

    @staticmethod
    def _sanitize_file_stem(stem: str) -> str:
        normalized = str(stem or "").strip().replace("\x00", "")
        normalized = re.sub(r"[\\/]+", "_", normalized)
        normalized = "".join(ch if ch.isprintable() else "_" for ch in normalized)
        normalized = re.sub(r"\s+", "_", normalized)
        normalized = normalized.strip("._")
        return normalized

    @staticmethod
    def _sanitize_suffix(suffix: str) -> str:
        normalized = str(suffix or "").strip().replace("\x00", "")
        normalized = re.sub(r"[^A-Za-z0-9.]+", "", normalized)
        if normalized and not normalized.startswith("."):
            normalized = "." + normalized
        return normalized

    @staticmethod
    def _delete_local_files(records: tuple[PendingAttachmentRecord, ...]) -> None:
        for record in records:
            try:
                os.remove(record.local_path)
            except FileNotFoundError:
                continue
            except OSError:
                continue

    @staticmethod
    def _download_resource_type(attachment_type: str) -> str:
        return "image" if attachment_type == "image" else "file"

    @staticmethod
    def _attachment_label(attachment_type: str) -> str:
        return _ATTACHMENT_TYPE_LABELS.get(str(attachment_type or "").strip().lower(), "附件")

    @staticmethod
    def _text_input_item(text: str) -> TextTurnInputItem:
        return {"type": "text", "text": text}

    @staticmethod
    def _local_image_input_item(path: str) -> LocalImageTurnInputItem:
        return {"type": "localImage", "path": path}
