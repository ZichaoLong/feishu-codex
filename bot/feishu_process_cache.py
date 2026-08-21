"""Process-local Feishu message and chat caches."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.feishu_types import MessageContextPayload

_DEDUP_MAX_SIZE = 500
_DEDUP_TTL_SECONDS = 300
_MESSAGE_CONTEXT_MAX_SIZE = 1000
_MESSAGE_CONTEXT_TTL_SECONDS = 600
_CHAT_TYPE_MAX_SIZE = 1000
_CHAT_TYPE_TTL_SECONDS = 24 * 3600
_CHAT_DISPLAY_NAME_MAX_SIZE = 1000
_CHAT_DISPLAY_NAME_TTL_SECONDS = 6 * 3600
_PENDING_EXECUTION_CARD_MAX_SIZE = 1000
_PENDING_EXECUTION_CARD_TTL_SECONDS = 600
_SENDER_NAME_TTL_SECONDS = 6 * 3600
_SENDER_NAME_FAILURE_WARNING_TTL_SECONDS = 300


@dataclass(slots=True)
class _MessageContext:
    payload: MessageContextPayload
    created_at: float


@dataclass(slots=True)
class _CachedChatType:
    chat_type: str
    created_at: float


@dataclass(slots=True)
class _CachedChatDisplayName:
    display_name: str
    created_at: float


@dataclass(slots=True)
class _PendingExecutionCard:
    card_message_id: str
    created_at: float


def _evict_expired_fifo_entries(
    entries: OrderedDict[str, Any],
    *,
    now: float,
    ttl_seconds: float,
    created_at: Callable[[Any], float],
) -> None:
    while entries:
        oldest_key, oldest_value = next(iter(entries.items()))
        if now - created_at(oldest_value) <= ttl_seconds:
            break
        entries.pop(oldest_key, None)


def _store_fifo_ttl_entry(
    entries: OrderedDict[str, Any],
    *,
    key: str,
    value: Any,
    now: float,
    ttl_seconds: float,
    max_size: int,
    created_at: Callable[[Any], float],
) -> None:
    _evict_expired_fifo_entries(
        entries,
        now=now,
        ttl_seconds=ttl_seconds,
        created_at=created_at,
    )
    entries.pop(key, None)
    entries[key] = value
    while len(entries) > max_size:
        entries.popitem(last=False)


class FeishuProcessCache:
    """Own transient, process-local Feishu cache facts."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._message_contexts: OrderedDict[str, _MessageContext] = OrderedDict()
        self._message_context_lock = threading.Lock()
        self._chat_types: OrderedDict[str, _CachedChatType] = OrderedDict()
        self._chat_type_lock = threading.Lock()
        self._chat_display_names: OrderedDict[
            str,
            _CachedChatDisplayName,
        ] = OrderedDict()
        self._chat_display_name_lock = threading.Lock()
        self._pending_execution_cards: OrderedDict[
            str,
            _PendingExecutionCard,
        ] = OrderedDict()
        self._pending_execution_card_lock = threading.Lock()
        self._sender_names: dict[str, tuple[float, str]] = {}
        self._sender_name_lock = threading.Lock()
        self._sender_name_warning_timestamps: dict[tuple[str, str], float] = {}
        self._sender_name_warning_lock = threading.Lock()

    def is_duplicate_message(self, message_id: str) -> bool:
        with self._dedup_lock:
            now = self._clock()
            if message_id in self._seen_messages:
                return True
            while self._seen_messages:
                oldest_id, timestamp = next(iter(self._seen_messages.items()))
                if now - timestamp <= _DEDUP_TTL_SECONDS:
                    break
                self._seen_messages.pop(oldest_id, None)
            if len(self._seen_messages) >= _DEDUP_MAX_SIZE:
                self._seen_messages.popitem(last=False)
            self._seen_messages[message_id] = now
            return False

    def get_message_context(self, message_id: str) -> MessageContextPayload:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return {}
        with self._message_context_lock:
            _evict_expired_fifo_entries(
                self._message_contexts,
                now=self._clock(),
                ttl_seconds=_MESSAGE_CONTEXT_TTL_SECONDS,
                created_at=lambda item: item.created_at,
            )
            context = self._message_contexts.get(normalized_message_id)
            return dict(context.payload) if context is not None else {}

    def remember_message_context(
        self,
        message_id: str,
        payload: MessageContextPayload,
    ) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        with self._message_context_lock:
            now = self._clock()
            _store_fifo_ttl_entry(
                self._message_contexts,
                key=normalized_message_id,
                value=_MessageContext(payload=payload.copy(), created_at=now),
                now=now,
                ttl_seconds=_MESSAGE_CONTEXT_TTL_SECONDS,
                max_size=_MESSAGE_CONTEXT_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def remember_chat_type(self, chat_id: str, chat_type: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_chat_type = str(chat_type or "").strip()
        if not normalized_chat_id or not normalized_chat_type:
            return
        with self._chat_type_lock:
            now = self._clock()
            _store_fifo_ttl_entry(
                self._chat_types,
                key=normalized_chat_id,
                value=_CachedChatType(normalized_chat_type, now),
                now=now,
                ttl_seconds=_CHAT_TYPE_TTL_SECONDS,
                max_size=_CHAT_TYPE_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def lookup_chat_type(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        with self._chat_type_lock:
            _evict_expired_fifo_entries(
                self._chat_types,
                now=self._clock(),
                ttl_seconds=_CHAT_TYPE_TTL_SECONDS,
                created_at=lambda item: item.created_at,
            )
            cached = self._chat_types.get(normalized_chat_id)
            return cached.chat_type if cached is not None else ""

    def remember_chat_display_name(self, chat_id: str, display_name: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_display_name = str(display_name or "").strip()
        if not normalized_chat_id or not normalized_display_name:
            return
        with self._chat_display_name_lock:
            now = self._clock()
            _store_fifo_ttl_entry(
                self._chat_display_names,
                key=normalized_chat_id,
                value=_CachedChatDisplayName(normalized_display_name, now),
                now=now,
                ttl_seconds=_CHAT_DISPLAY_NAME_TTL_SECONDS,
                max_size=_CHAT_DISPLAY_NAME_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def lookup_chat_display_name(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        with self._chat_display_name_lock:
            _evict_expired_fifo_entries(
                self._chat_display_names,
                now=self._clock(),
                ttl_seconds=_CHAT_DISPLAY_NAME_TTL_SECONDS,
                created_at=lambda item: item.created_at,
            )
            cached = self._chat_display_names.get(normalized_chat_id)
            return cached.display_name if cached is not None else ""

    def reserve_execution_card(
        self,
        trigger_message_id: str,
        card_message_id: str,
    ) -> None:
        normalized_trigger_id = str(trigger_message_id or "").strip()
        normalized_card_id = str(card_message_id or "").strip()
        if not normalized_trigger_id or not normalized_card_id:
            return
        with self._pending_execution_card_lock:
            now = self._clock()
            _store_fifo_ttl_entry(
                self._pending_execution_cards,
                key=normalized_trigger_id,
                value=_PendingExecutionCard(normalized_card_id, now),
                now=now,
                ttl_seconds=_PENDING_EXECUTION_CARD_TTL_SECONDS,
                max_size=_PENDING_EXECUTION_CARD_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def claim_reserved_execution_card(self, trigger_message_id: str) -> str:
        normalized_trigger_id = str(trigger_message_id or "").strip()
        if not normalized_trigger_id:
            return ""
        with self._pending_execution_card_lock:
            _evict_expired_fifo_entries(
                self._pending_execution_cards,
                now=self._clock(),
                ttl_seconds=_PENDING_EXECUTION_CARD_TTL_SECONDS,
                created_at=lambda item: item.created_at,
            )
            pending = self._pending_execution_cards.pop(
                normalized_trigger_id,
                None,
            )
            return pending.card_message_id if pending is not None else ""

    def remember_sender_name(self, *keys: str, value: str) -> None:
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return
        now = self._clock()
        with self._sender_name_lock:
            for key in keys:
                normalized_key = str(key or "").strip()
                if normalized_key:
                    self._sender_names[normalized_key] = (now, normalized_value)

    def lookup_sender_name(self, sender_id: str) -> str:
        normalized_sender_id = str(sender_id or "").strip()
        if not normalized_sender_id:
            return ""
        with self._sender_name_lock:
            cached = self._sender_names.get(normalized_sender_id)
            if cached is None:
                return ""
            timestamp, value = cached
            if self._clock() - timestamp > _SENDER_NAME_TTL_SECONDS:
                self._sender_names.pop(normalized_sender_id, None)
                return ""
            return value

    def should_emit_sender_name_warning(
        self,
        open_id: str,
        fallback_reason: str,
    ) -> bool:
        key = (
            str(open_id or "").strip() or "unknown",
            str(fallback_reason or "").strip() or "unknown",
        )
        now = self._clock()
        with self._sender_name_warning_lock:
            last_at = self._sender_name_warning_timestamps.get(key, 0.0)
            if now - last_at < _SENDER_NAME_FAILURE_WARNING_TTL_SECONDS:
                return False
            self._sender_name_warning_timestamps[key] = now
            return True

    def forget_chat(self, chat_id: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        with self._chat_type_lock:
            self._chat_types.pop(normalized_chat_id, None)
        with self._chat_display_name_lock:
            self._chat_display_names.pop(normalized_chat_id, None)
        with self._message_context_lock:
            stale_message_ids = [
                message_id
                for message_id, context in self._message_contexts.items()
                if str(
                    context.payload.get("chat_id", "") or ""
                ).strip()
                == normalized_chat_id
            ]
            for message_id in stale_message_ids:
                self._message_contexts.pop(message_id, None)
