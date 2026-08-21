"""Feishu message and interactive-card decoding."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.card_text_projection import (
    TERMINAL_RESULT_SOURCE_CARD_DEGRADED,
    TERMINAL_RESULT_SOURCE_STORE,
    CardTextProjection,
    is_execution_card,
    is_terminal_result_card,
    project_interactive_card_text,
)
from bot.feishu_types import MentionPayload


@dataclass(frozen=True, slots=True)
class InteractiveMessageReadResult:
    text: str
    card_kind: str
    has_authoritative_text: bool = False
    terminal_result_id: str = ""
    text_source: str = ""


@dataclass(frozen=True, slots=True)
class FeishuMessageCodecPorts:
    load_raw_card_content: Callable[[str], dict[str, Any]]
    resolve_sender_name: Callable[[str], str]
    remember_sender_name: Callable[[str, str], None]
    configured_trigger_open_ids: Callable[[], set[str]]
    log_card_ingress_event: Callable[[str, dict[str, Any]], None]


class FeishuMessageCodec:
    """Own Feishu message schemas and card-text projection order."""

    def __init__(self, ports: FeishuMessageCodecPorts) -> None:
        self._ports = ports
        self._terminal_result_text_resolver: (
            Callable[[CardTextProjection], str] | None
        ) = None

    def set_terminal_result_text_resolver(
        self,
        resolver: Callable[[CardTextProjection], str] | None,
    ) -> None:
        self._terminal_result_text_resolver = resolver

    @staticmethod
    def extract_text(msg_type: str, content_dict: dict[str, Any]) -> str:
        if msg_type == "text":
            return str(content_dict.get("text", "") or "").strip()
        if msg_type == "post":
            paragraphs = content_dict.get("content")
            if isinstance(paragraphs, dict):
                for language_content in paragraphs.values():
                    if isinstance(language_content, dict):
                        paragraphs = language_content.get("content", [])
                    else:
                        paragraphs = language_content
                    break
            if not isinstance(paragraphs, list):
                return ""
            parts: list[str] = []
            for paragraph in paragraphs:
                if not isinstance(paragraph, list):
                    continue
                line_parts = [
                    str(element.get("text", "") or "")
                    for element in paragraph
                    if isinstance(element, dict)
                    and element.get("tag") == "text"
                    and str(element.get("text", "") or "")
                ]
                line = "".join(line_parts)
                parts.append(line if line.strip() else "")
            while parts and not parts[0]:
                parts.pop(0)
            while parts and not parts[-1]:
                parts.pop()
            return "\n".join(parts)
        if msg_type == "interactive":
            return project_interactive_card_text(content_dict).text
        return ""

    def render_message_text(
        self,
        msg_type: str,
        content_dict: dict[str, Any],
        *,
        message_id: str = "",
    ) -> str:
        normalized_message_id = str(message_id or "").strip()
        if msg_type == "interactive" and normalized_message_id:
            resolved = self.read_interactive_message(
                normalized_message_id,
                content_dict=content_dict,
            )
            if resolved.text:
                return resolved.text
        text = self.extract_text(msg_type, content_dict)
        if text:
            if normalized_message_id:
                self._ports.log_card_ingress_event(
                    "resolution",
                    {
                        "message_id": normalized_message_id,
                        "msg_type": msg_type,
                        "path": "best_effort_projection",
                        "has_authoritative": False,
                    },
                )
            return text
        if msg_type == "share_user":
            shared_open_id = str(content_dict.get("user_id", "") or "").strip()
            if not shared_open_id:
                return "[个人名片]"
            shared_name = self._ports.resolve_sender_name(shared_open_id)
            self._ports.remember_sender_name(shared_open_id, shared_name)
            return f"[个人名片] {shared_name}"
        if msg_type == "share_chat":
            shared_chat_id = str(content_dict.get("chat_id", "") or "").strip()
            return f"[群名片] {shared_chat_id}" if shared_chat_id else "[群名片]"
        if msg_type == "hongbao":
            return str(content_dict.get("text", "") or "").strip() or "[红包]"
        if msg_type in {
            "share_calendar_event",
            "calendar",
            "general_calendar",
        }:
            summary = str(content_dict.get("summary", "") or "").strip()
            return f"[日程] {summary}" if summary else "[日程]"
        if msg_type == "system":
            template = str(content_dict.get("template", "") or "").strip()
            return f"[系统消息] {template}" if template else "[系统消息]"
        return ""

    def read_interactive_message(
        self,
        message_id: str,
        *,
        content_dict: dict[str, Any] | None = None,
    ) -> InteractiveMessageReadResult:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return InteractiveMessageReadResult(text="", card_kind="")
        raw_content = self._ports.load_raw_card_content(normalized_message_id)
        if raw_content:
            resolved = self._project_interactive_message(
                normalized_message_id,
                raw_content,
                path="raw_card_direct",
            )
            if resolved is not None:
                return resolved
        if isinstance(content_dict, dict):
            resolved = self._project_interactive_message(
                normalized_message_id,
                content_dict,
                path="best_effort_projection",
            )
            if resolved is not None:
                return resolved
        return InteractiveMessageReadResult(text="", card_kind="")

    def _project_interactive_message(
        self,
        message_id: str,
        content: dict[str, Any],
        *,
        path: str,
    ) -> InteractiveMessageReadResult | None:
        projection = project_interactive_card_text(content)
        if not projection.text:
            return None
        resolved_text, source, authoritative = (
            self._resolve_terminal_result_projection(projection)
        )
        self._ports.log_card_ingress_event(
            "resolution",
            {
                "message_id": message_id,
                "msg_type": "interactive",
                "path": path,
                "has_authoritative": authoritative,
                "terminal_result_id": projection.terminal_result_id,
                "text_source": source,
            },
        )
        return InteractiveMessageReadResult(
            text=resolved_text,
            card_kind=self.interactive_card_kind(content),
            has_authoritative_text=authoritative,
            terminal_result_id=projection.terminal_result_id,
            text_source=source,
        )

    def _resolve_terminal_result_projection(
        self,
        projection: CardTextProjection,
    ) -> tuple[str, str, bool]:
        resolver = self._terminal_result_text_resolver
        if projection.terminal_result_id and resolver is not None:
            resolved = str(resolver(projection) or "")
            if resolved:
                return resolved, TERMINAL_RESULT_SOURCE_STORE, True
        source = projection.final_reply_source
        if (
            projection.terminal_result_id
            and source == TERMINAL_RESULT_SOURCE_CARD_DEGRADED
        ):
            return projection.text, TERMINAL_RESULT_SOURCE_CARD_DEGRADED, False
        return projection.text, source, projection.has_authoritative_final_reply

    @staticmethod
    def interactive_card_kind(content_dict: dict[str, Any]) -> str:
        if is_terminal_result_card(content_dict):
            return "terminal"
        if is_execution_card(content_dict):
            return "execution"
        return "other"

    @staticmethod
    def attachment_message_name(
        msg_type: str,
        content_dict: dict[str, Any],
    ) -> str:
        if msg_type == "image":
            return ""
        if msg_type == "audio":
            return str(content_dict.get("file_name", "") or "").strip() or "语音"
        return str(content_dict.get("file_name", "") or "").strip()

    @staticmethod
    def attachment_resource_key(
        msg_type: str,
        content_dict: dict[str, Any],
    ) -> str:
        if msg_type == "image":
            return str(content_dict.get("image_key", "") or "").strip()
        return str(content_dict.get("file_key", "") or "").strip()

    @staticmethod
    def mention_payload(mention: Any) -> MentionPayload:
        if isinstance(mention, dict):
            key = str(mention.get("key", "") or "").strip()
            name = str(mention.get("name", "") or "").strip()
            direct_open_id = str(mention.get("open_id", "") or "").strip()
            mention_id = mention.get("id")
        else:
            key = str(getattr(mention, "key", "") or "").strip()
            name = str(getattr(mention, "name", "") or "").strip()
            direct_open_id = str(
                getattr(mention, "open_id", "") or ""
            ).strip()
            mention_id = getattr(mention, "id", None)
        open_id = ""
        if isinstance(mention_id, dict):
            open_id = str(
                mention_id.get("open_id", "")
                or mention_id.get("id", "")
                or ""
            ).strip()
        elif isinstance(mention_id, str):
            open_id = mention_id.strip()
        elif mention_id is not None:
            open_id = str(
                getattr(mention_id, "open_id", "")
                or getattr(mention_id, "id", "")
                or ""
            ).strip()
        return {
            "key": key,
            "name": name,
            "open_id": direct_open_id or open_id,
        }

    @classmethod
    def mention_payloads(cls, mentions: list[Any]) -> list[MentionPayload]:
        return [cls.mention_payload(mention) for mention in mentions]

    def normalize_mentions(self, text: str, mentions: list[Any]) -> str:
        normalized = text
        trigger_open_ids = self._ports.configured_trigger_open_ids()
        for mention in mentions:
            payload = self.mention_payload(mention)
            key = payload["key"]
            mention_open_id = payload["open_id"]
            mention_name = str(
                payload["name"] or mention_open_id[:8]
            ).strip()
            if not key:
                continue
            if mention_open_id and mention_open_id in trigger_open_ids:
                normalized = normalized.replace(key, "")
            else:
                normalized = normalized.replace(key, f"@{mention_name}")
        return normalized.strip()
