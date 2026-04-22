from __future__ import annotations

import json
import logging
import pathlib
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from bot.feishu_types import GroupMessageEntry, MentionPayload

logger = logging.getLogger(__name__)

_DEFAULT_GROUP_HISTORY_FETCH_LIMIT = 50
_DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS = 24 * 3600
_DEFAULT_GROUP_HISTORY_BOUNDARY_SLACK_SECONDS = 5


@dataclass(frozen=True, slots=True)
class ListedMessagesPage:
    items: list[Any]
    has_more: bool = False
    page_token: str = ""


@dataclass(frozen=True, slots=True)
class GroupHistoryRecoveryPorts:
    list_messages: Callable[..., ListedMessagesPage]
    render_message_text: Callable[[str, dict[str, Any]], str]
    normalize_mentions: Callable[[str, list[MentionPayload]], str]
    mention_payloads: Callable[[list[Any]], list[MentionPayload]]
    display_name_for_sender_identity: Callable[..., str]
    read_local_messages_between: Callable[..., list[GroupMessageEntry]]
    get_last_boundary_seq: Callable[..., int]
    get_last_boundary_created_at: Callable[..., int]
    get_last_boundary_message_ids: Callable[..., list[str]]


class GroupHistoryRecovery:
    def __init__(
        self,
        *,
        ports: GroupHistoryRecoveryPorts,
        app_id: str | Callable[[], str] = "",
        history_fetch_limit: int = _DEFAULT_GROUP_HISTORY_FETCH_LIMIT,
        history_fetch_lookback_seconds: int = _DEFAULT_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
        boundary_slack_seconds: int = _DEFAULT_GROUP_HISTORY_BOUNDARY_SLACK_SECONDS,
    ) -> None:
        self._ports = ports
        if callable(app_id):
            self._app_id_getter = app_id
        else:
            self._app_id_getter = lambda: str(app_id or "").strip()
        self._history_fetch_limit = max(int(history_fetch_limit or 0), 0)
        self._history_fetch_lookback_seconds = max(int(history_fetch_lookback_seconds or 0), 0)
        self._boundary_slack_seconds = max(int(boundary_slack_seconds or 0), 0)

    @staticmethod
    def group_scope_key(thread_id: str = "") -> str:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return "main"
        return f"thread:{normalized_thread_id}"

    @staticmethod
    def thread_id_for_scope(scope: str) -> str:
        normalized_scope = str(scope or "").strip()
        if normalized_scope.startswith("thread:"):
            return normalized_scope.removeprefix("thread:")
        return ""

    def history_recovery_enabled(self) -> bool:
        return (
            self._history_fetch_limit > 0
            and self._history_fetch_lookback_seconds > 0
        )

    def history_entry_from_message(self, item: Any) -> GroupMessageEntry | None:
        message_id = str(getattr(item, "message_id", "") or "").strip()
        if not message_id:
            return None

        msg_type = str(getattr(item, "msg_type", "") or "text").strip()
        body = getattr(item, "body", None)
        raw_content = str(getattr(body, "content", "") or "").strip()
        try:
            content_dict = json.loads(raw_content) if raw_content else {}
        except Exception:
            content_dict = {}

        text = self._ports.render_message_text(msg_type, content_dict)
        mentions = list(getattr(item, "mentions", None) or [])
        if text and mentions:
            text = self._ports.normalize_mentions(
                text,
                self._ports.mention_payloads(mentions),
            )
        if not text:
            return None

        sender = getattr(item, "sender", None)
        sender_type = str(getattr(sender, "sender_type", "") or "user").strip()
        sender_id = str(getattr(sender, "id", "") or "").strip()
        if self._is_self_history_app_sender(sender_type=sender_type, sender_id=sender_id):
            return None
        sender_principal_id = sender_id if sender_type in {"user", "app"} else ""
        sender_name = self._ports.display_name_for_sender_identity(
            user_id="",
            sender_principal_id=sender_principal_id,
            sender_type=sender_type,
        )
        return {
            "message_id": message_id,
            "created_at": int(getattr(item, "create_time", 0) or 0),
            "sender_user_id": "",
            "sender_principal_id": sender_principal_id,
            "sender_type": sender_type,
            "sender_name": sender_name,
            "msg_type": msg_type,
            "thread_id": str(getattr(item, "thread_id", "") or "").strip(),
            "text": text,
        }

    def _is_self_history_app_sender(self, *, sender_type: str, sender_id: str) -> bool:
        normalized_sender_type = str(sender_type or "").strip()
        normalized_sender_id = str(sender_id or "").strip()
        normalized_app_id = str(self._app_id_getter() or "").strip()
        return bool(normalized_app_id) and normalized_sender_type == "app" and normalized_sender_id == normalized_app_id

    def fetch_group_history_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        existing_message_ids: set[str],
        after_created_at: int | str | None = None,
        after_message_ids: set[str] | None = None,
        thread_id: str = "",
        limit: int | None = None,
    ) -> list[GroupMessageEntry]:
        effective_limit = self._history_fetch_limit if limit is None else max(int(limit), 0)
        if effective_limit <= 0 or self._history_fetch_lookback_seconds <= 0:
            return []
        min_created_at = max(int(after_created_at or 0), 0)
        normalized_thread_id = str(thread_id or "").strip()
        if normalized_thread_id:
            try:
                return self.fetch_thread_history_entries(
                    thread_id=normalized_thread_id,
                    current_message_id=current_message_id,
                    existing_message_ids=existing_message_ids,
                    min_created_at=min_created_at,
                    boundary_message_ids=after_message_ids or set(),
                    limit=effective_limit,
                    descending=True,
                )
            except Exception as exc:
                if not self.should_fallback_thread_history_scan(exc):
                    raise
                logger.warning("话题倒序历史回捞失败，回退到升序扫描: thread_id=%s error=%s", normalized_thread_id, exc)
                return self.fetch_thread_history_entries(
                    thread_id=normalized_thread_id,
                    current_message_id=current_message_id,
                    existing_message_ids=existing_message_ids,
                    min_created_at=min_created_at,
                    boundary_message_ids=after_message_ids or set(),
                    limit=effective_limit,
                    descending=False,
                )
        return self.fetch_chat_history_entries(
            chat_id=chat_id,
            current_message_id=current_message_id,
            current_create_time=current_create_time,
            existing_message_ids=existing_message_ids,
            min_created_at=min_created_at,
            boundary_message_ids=after_message_ids or set(),
            limit=effective_limit,
        )

    @staticmethod
    def should_fallback_thread_history_scan(exc: Exception) -> bool:
        message = str(exc).lower()
        return "invalid request parameter" in message or "sort_type" in message

    def fetch_thread_history_entries(
        self,
        *,
        thread_id: str,
        current_message_id: str,
        existing_message_ids: set[str],
        min_created_at: int,
        boundary_message_ids: set[str],
        limit: int,
        descending: bool,
    ) -> list[GroupMessageEntry]:
        page_token = ""
        seen_message_ids = set(existing_message_ids)
        seen_message_ids.add(str(current_message_id or "").strip())
        normalized_boundary_ids = {
            str(item).strip()
            for item in boundary_message_ids
            if str(item).strip()
        }
        descending_entries: list[GroupMessageEntry] = []
        ascending_entries: deque[GroupMessageEntry] = deque(maxlen=limit)

        while True:
            page = self._ports.list_messages(
                container_id_type="thread",
                container_id=thread_id,
                sort_type="ByCreateTimeDesc" if descending else "ByCreateTimeAsc",
                page_size=50,
                page_token=page_token,
            )
            stop_fetch = False
            for item in list(page.items or []):
                entry = self.history_entry_from_message(item)
                if not entry:
                    continue
                if str(entry.get("thread_id", "") or "").strip() != thread_id:
                    continue
                entry_created_at = max(int(entry.get("created_at", 0) or 0), 0)
                message_id = str(entry.get("message_id", "") or "").strip()
                if min_created_at > 0 and entry_created_at < min_created_at:
                    if descending:
                        stop_fetch = True
                        break
                    continue
                if (
                    min_created_at > 0
                    and entry_created_at == min_created_at
                    and message_id in normalized_boundary_ids
                ):
                    continue
                if not message_id or message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                if descending:
                    descending_entries.append(entry)
                    if len(descending_entries) >= limit:
                        stop_fetch = True
                        break
                else:
                    ascending_entries.append(entry)

            if stop_fetch or not page.has_more:
                break
            page_token = str(page.page_token or "").strip()
            if not page_token:
                break

        if descending:
            descending_entries.reverse()
            return descending_entries
        return list(ascending_entries)

    def fetch_chat_history_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        existing_message_ids: set[str],
        min_created_at: int,
        boundary_message_ids: set[str],
        limit: int,
    ) -> list[GroupMessageEntry]:
        end_time = int(int(current_create_time or 0) / 1000) if current_create_time else int(time.time())
        if end_time <= 0:
            end_time = int(time.time())
        start_time = max(0, end_time - self._history_fetch_lookback_seconds)
        if min_created_at > 0:
            start_time = max(
                start_time,
                max(0, int(min_created_at / 1000) - self._boundary_slack_seconds),
            )
        page_token = ""
        entries: deque[GroupMessageEntry] = deque(maxlen=limit)
        seen_message_ids = set(existing_message_ids)
        seen_message_ids.add(str(current_message_id or "").strip())
        boundary_message_ids = {
            str(item).strip()
            for item in boundary_message_ids
            if str(item).strip()
        }

        while True:
            page = self._ports.list_messages(
                container_id_type="chat",
                container_id=chat_id,
                start_time=str(start_time),
                end_time=str(end_time),
                sort_type="ByCreateTimeAsc",
                page_size=50,
                page_token=page_token,
            )
            for item in list(page.items or []):
                entry = self.history_entry_from_message(item)
                if not entry:
                    continue
                if str(entry.get("thread_id", "") or "").strip():
                    continue
                entry_created_at = max(int(entry.get("created_at", 0) or 0), 0)
                message_id = str(entry.get("message_id", "") or "").strip()
                if min_created_at > 0 and entry_created_at < min_created_at:
                    continue
                if (
                    min_created_at > 0
                    and entry_created_at == min_created_at
                    and message_id in boundary_message_ids
                ):
                    continue
                if not message_id or message_id in seen_message_ids:
                    continue
                entries.append(entry)
                seen_message_ids.add(message_id)

            if not page.has_more:
                break
            page_token = str(page.page_token or "").strip()
            if not page_token:
                break

        return list(entries)

    @staticmethod
    def group_context_sort_key(item: GroupMessageEntry) -> tuple[int, int, int, str]:
        created_at = max(int(item.get("created_at", 0) or 0), 0)
        seq = item.get("seq")
        if isinstance(seq, int):
            return (created_at, 0, seq, str(item.get("message_id", "") or ""))
        return (created_at, 1, 0, str(item.get("message_id", "") or ""))

    def collect_assistant_context_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        current_seq: int,
        thread_id: str = "",
    ) -> list[GroupMessageEntry]:
        scope = self.group_scope_key(thread_id)
        boundary_seq = self._ports.get_last_boundary_seq(chat_id, scope=scope)
        boundary_created_at = self._ports.get_last_boundary_created_at(chat_id, scope=scope)
        boundary_message_ids = set(self._ports.get_last_boundary_message_ids(chat_id, scope=scope))
        local_entries = self._ports.read_local_messages_between(
            chat_id,
            after_seq=boundary_seq,
            before_seq=current_seq or None,
            scope=scope,
        )
        if not self.history_recovery_enabled():
            return local_entries

        existing_message_ids = {
            str(item.get("message_id", "") or "").strip()
            for item in local_entries
            if isinstance(item, dict) and str(item.get("message_id", "") or "").strip()
        }
        history_entries = self.fetch_group_history_entries(
            chat_id=chat_id,
            current_message_id=current_message_id,
            current_create_time=current_create_time,
            existing_message_ids=existing_message_ids,
            after_created_at=boundary_created_at,
            after_message_ids=boundary_message_ids,
            thread_id=thread_id,
        )
        if not history_entries:
            return local_entries
        merged_entries = [*local_entries, *history_entries]
        return sorted(merged_entries, key=self.group_context_sort_key)

    @staticmethod
    def collect_boundary_message_ids(
        *,
        current_message_id: str,
        current_created_at: int | str | None,
        context_entries: list[GroupMessageEntry],
    ) -> list[str]:
        normalized_created_at = max(int(current_created_at or 0), 0)
        if normalized_created_at <= 0:
            return []
        message_ids = {
            str(current_message_id or "").strip(),
        }
        for item in context_entries:
            if max(int(item.get("created_at", 0) or 0), 0) != normalized_created_at:
                continue
            message_id = str(item.get("message_id", "") or "").strip()
            if message_id:
                message_ids.add(message_id)
        message_ids.discard("")
        return sorted(message_ids)

    @staticmethod
    def format_ts(ts_ms: int | str | None) -> str:
        if not ts_ms:
            return "未知时间"
        try:
            from datetime import datetime, timedelta, timezone

            dt = datetime.fromtimestamp(
                int(ts_ms) / 1000,
                tz=timezone(timedelta(hours=8)),
            )
            return dt.strftime("%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return "未知时间"

    @staticmethod
    def normalize_sender_name(sender_name: str) -> str:
        normalized = " ".join(str(sender_name or "").split())
        return normalized or "unknown"

    def build_group_turn_text(self, current_text: str, *, sender_name: str) -> str:
        message_text = str(current_text or "").strip()
        if not message_text:
            message_text = "（发送者没有提供额外文本，请基于上下文回复最近这段讨论。）"
        return (
            "<group_chat_current_turn>\n"
            f"sender_name: {self.normalize_sender_name(sender_name)}\n"
            "message:\n"
            f"{message_text}\n"
            "</group_chat_current_turn>"
        )

    def build_group_current_turn_text(self, current_text: str, *, sender_name: str) -> str:
        message_text = str(current_text or "").strip()
        if not message_text:
            message_text = "（发送者没有提供额外文本，请基于上下文回复最近这段讨论。）"
        return (
            "<group_chat_current_turn>\n"
            "以下是当前需要你直接响应的群消息。优先回复这条消息，而不是复述整段历史。\n"
            f"sender_name: {self.normalize_sender_name(sender_name)}\n"
            "message:\n"
            f"{message_text}\n"
            "</group_chat_current_turn>"
        )

    def format_group_context_entries(self, entries: list[GroupMessageEntry]) -> str:
        parts: list[str] = []
        for item in entries:
            seq = item.get("seq")
            ts = self.format_ts(item.get("created_at"))
            sender_name = str(item.get("sender_name", "") or "unknown").strip()
            sender_type = str(item.get("sender_type", "") or "user").strip()
            msg_type = str(item.get("msg_type", "") or "text").strip()
            text = str(item.get("text", "") or "").strip()
            if sender_type == "app" and not sender_name.startswith("机器人:"):
                sender_name = f"{sender_name}[机器人]"
            if isinstance(seq, int) and seq > 0:
                header = f"[#{seq} {ts}] {sender_name}"
            else:
                header = f"[{ts}] {sender_name}"
            if msg_type and msg_type != "text":
                header += f" ({msg_type})"
            if text:
                parts.append(f"{header}\n{text}")
            else:
                parts.append(header)
        return "\n\n".join(parts).strip()

    def build_assistant_turn_text(
        self,
        context_text: str,
        current_text: str,
        log_path: pathlib.Path,
        *,
        thread_id: str = "",
        current_sender_name: str = "",
    ) -> str:
        context_block = context_text.strip() or "（上次有效触发之后暂无可用群聊消息）"
        normalized_thread_id = str(thread_id or "").strip()
        if normalized_thread_id:
            scope_block = (
                "<group_chat_scope>\n"
                "当前消息来自群话题内。你仍是本群共享的同一个助手/数字分身。\n"
                "默认优先依据当前话题上下文回复；如需引用主聊天流或其他话题中已明确的信息，应明确说明那是本群其他讨论中的结论，并只保留与当前话题直接相关的部分。\n"
                f"当前话题 ID：`{normalized_thread_id}`\n"
                "</group_chat_scope>\n\n"
            )
        else:
            scope_block = (
                "<group_chat_scope>\n"
                "当前消息来自群主聊天流，不是群话题。你仍是本群共享的同一个助手/数字分身。\n"
                "默认优先依据当前主聊天流上下文回复；如需引用其他话题中已明确的信息，应明确说明那是本群其他讨论中的结论，并避免无关展开。\n"
                "</group_chat_scope>\n\n"
            )
        return (
            scope_block
            + "<group_chat_context>\n"
            "以下是本群自上次有效触发到本次触发之前的消息。\n"
            f"群聊日志文件：`{log_path}`\n\n"
            f"{context_block}\n"
            "</group_chat_context>\n\n"
            + self.build_group_current_turn_text(
                current_text,
                sender_name=current_sender_name,
            )
        )
