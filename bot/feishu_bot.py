"""
飞书机器人基类
封装了连接、消息收发等通用逻辑，子类只需实现 on_message / on_card_action 处理业务。
"""

import json
import logging
import pathlib
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    GetChatRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
    P2ImChatDisbandedV1,
    P2ImChatMemberBotDeletedV1,
    P2ImMessageReceiveV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

from bot.card_text_projection import project_interactive_card_text
from bot.feishu_types import (
    BotIdentitySnapshot,
    GroupAclSnapshot,
    GroupMessageEntry,
    MentionMember,
    MentionPayload,
    MessageContextPayload,
)
from bot.forward_aggregator import ForwardAggregator, ForwardAggregatorPorts, PendingForward
from bot.group_history_recovery import (
    GroupHistoryRecovery,
    GroupHistoryRecoveryPorts,
    ListedMessagesPage,
)
from bot.stores.group_chat_store import (
    GroupChatStore,
)
from bot.platform_paths import default_data_root

logger = logging.getLogger(__name__)

# 消息去重缓存最大容量和过期时间
_DEDUP_MAX_SIZE = 500
_DEDUP_TTL = 300  # 5 分钟

# 飞书卡片限制：单张卡片中 markdown 表格数量上限（实测约 5~10，取保守值）
_MAX_CARD_TABLES = 5

# 消息上下文缓存
_MESSAGE_CONTEXT_MAX_SIZE = 1000
_MESSAGE_CONTEXT_TTL = 600

# chat_id -> chat_type 缓存；用于无 message_id 的群命令入口做兜底判断
_CHAT_TYPE_CACHE_MAX_SIZE = 1000
_CHAT_TYPE_CACHE_TTL = 24 * 3600

# 原始消息 -> 预发送执行卡片缓存；用于在耗时预处理前先给用户反馈
_PENDING_EXECUTION_CARD_MAX_SIZE = 1000
_PENDING_EXECUTION_CARD_TTL = 600

# 显示名缓存（秒）
_SENDER_NAME_CACHE_TTL = 6 * 3600
_SENDER_NAME_FAILURE_WARNING_TTL = 300

# assistant 模式按需回捞群历史消息的窗口
_GROUP_HISTORY_FETCH_LIMIT = 50
_GROUP_HISTORY_FETCH_LOOKBACK_SECONDS = 24 * 3600
_DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES = {"image", "file", "audio", "media"}
_UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES = {"folder", "sticker"}
_ATTACHMENT_MESSAGE_TYPES = _DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES | _UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES


def _non_negative_int(value: Any, default: int) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return max(int(default), 0)


def _evict_expired_fifo_entries(
    entries: OrderedDict[str, Any],
    *,
    now: float,
    ttl_seconds: float,
    created_at: Callable[[Any], float],
) -> None:
    while entries:
        oldest_key, oldest_value = next(iter(entries.items()))
        if now - created_at(oldest_value) > ttl_seconds:
            entries.pop(oldest_key, None)
        else:
            break


def _store_fifo_ttl_entry(
    entries: OrderedDict[str, Any],
    *,
    key: str,
    value: Any,
    ttl_seconds: float,
    max_size: int,
    created_at: Callable[[Any], float],
) -> None:
    now = time.time()
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


@dataclass
class _MessageContext:
    payload: MessageContextPayload
    created_at: float


@dataclass
class _CachedChatType:
    chat_type: str
    created_at: float


@dataclass
class _PendingExecutionCard:
    card_message_id: str
    created_at: float


@dataclass(frozen=True, slots=True)
class DownloadedMessageResource:
    content: bytes
    file_name: str
    content_type: str


def _scan_tables(text: str) -> list[tuple[int, int]]:
    """扫描 markdown 文本中 **代码块外** 的表格，返回 (start, end) 行号列表

    会跟踪 ``` 代码块状态，已在代码块内的表格不会被识别。
    """
    lines = text.split("\n")
    tables: list[tuple[int, int]] = []
    in_fence = False
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # 跟踪代码块开关（兼容 ```python 等带语言标记的情况）
        if stripped.startswith("```"):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence and stripped.startswith("|") and stripped.endswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if sep.startswith("|") and "---" in sep:
                start = i
                j = i + 2
                while j < len(lines) and lines[j].strip().startswith("|"):
                    j += 1
                tables.append((start, j))
                i = j
                continue
        i += 1
    return tables


def limit_card_tables(text: str, max_tables: int = _MAX_CARD_TABLES) -> str:
    """限制 markdown 文本中的表格数量，超出部分转为代码块

    飞书卡片对单张卡片中的 markdown 表格数量有上限，超出后
    API 会返回 ErrCode 11310 (card table number over limit)。
    此函数将超出限制的表格用代码块包裹，保留可读性的同时避免触发限制。
    已在代码块内的表格不受影响。
    """
    tables = _scan_tables(text)
    if len(tables) <= max_tables:
        return text

    lines = text.split("\n")
    # 从后往前替换超出的表格为代码块（保持前面的行号不变）
    for start, end in reversed(tables[max_tables:]):
        table_lines = lines[start:end]
        lines[start:end] = ["```", *table_lines, "```"]

    return "\n".join(lines)


def count_card_tables(text: str) -> int:
    """统计 markdown 文本中代码块外的表格数量"""
    return len(_scan_tables(text))


class FeishuBot(ABC):
    """飞书机器人基类

    关键部分：
    1. 连接层: __init__ 中创建 lark.Client 和事件回调，start() 启动 WebSocket
    2. 消息收发层: send_message 泛化发送，reply / reply_card 为便捷方法
    3. 业务逻辑层: 子类实现 on_message 和 on_card_action
    """

    # 群聊工作态常量
    _GROUP_MODE_ALL = "all"
    _GROUP_MODE_MENTION = "mention_only"
    _GROUP_MODE_ASSISTANT = "assistant"

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        request_timeout_seconds: float = 10.0,
        *,
        data_dir: pathlib.Path | None = None,
        system_config: dict[str, Any] | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.request_timeout_seconds = float(request_timeout_seconds)
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()
        self._group_store = GroupChatStore(data_dir or default_data_root())
        self._message_contexts: OrderedDict[str, _MessageContext] = OrderedDict()
        self._message_context_lock = threading.Lock()
        self._chat_type_cache: OrderedDict[str, _CachedChatType] = OrderedDict()
        self._chat_type_cache_lock = threading.Lock()
        self._pending_execution_cards: OrderedDict[str, _PendingExecutionCard] = OrderedDict()
        self._pending_execution_cards_lock = threading.Lock()
        self._sender_name_cache: dict[str, tuple[float, str]] = {}
        self._sender_name_cache_lock = threading.Lock()
        self._sender_name_warning_timestamps: dict[tuple[str, str], float] = {}
        self._sender_name_warning_lock = threading.Lock()
        config = system_config or {}
        self._admin_open_ids = {
            str(item).strip()
            for item in config.get("admin_open_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        self._group_history_fetch_limit = _non_negative_int(
            config.get("group_history_fetch_limit", _GROUP_HISTORY_FETCH_LIMIT),
            _GROUP_HISTORY_FETCH_LIMIT,
        )
        self._group_history_fetch_lookback_seconds = _non_negative_int(
            config.get(
                "group_history_fetch_lookback_seconds",
                _GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
            ),
            _GROUP_HISTORY_FETCH_LOOKBACK_SECONDS,
        )
        self._history_recovery = GroupHistoryRecovery(
            ports=GroupHistoryRecoveryPorts(
                list_messages=self._list_history_messages_page,
                render_message_text=self._render_message_text,
                normalize_mentions=self._normalize_mentions,
                mention_payloads=self._mention_payloads,
                display_name_for_sender_identity=self._display_name_for_sender_identity,
                read_local_messages_between=self._read_group_history_local_messages,
                get_last_boundary_seq=self._get_group_history_boundary_seq,
                get_last_boundary_created_at=self._get_group_history_boundary_created_at,
                get_last_boundary_message_ids=self._get_group_history_boundary_message_ids,
            ),
            app_id=lambda: self.app_id,
            history_fetch_limit=self._group_history_fetch_limit,
            history_fetch_lookback_seconds=self._group_history_fetch_lookback_seconds,
        )
        configured_bot_open_id = str(config.get("bot_open_id", "") or "").strip()
        self._configured_bot_open_id = configured_bot_open_id
        self._configured_trigger_open_ids = {
            str(item).strip()
            for item in config.get("trigger_open_ids", [])
            if isinstance(item, str) and str(item).strip()
        }
        self._bot_open_id_error_logged = False
        self._forward_aggregator = ForwardAggregator(
            ports=ForwardAggregatorPorts(
                get_group_mode=self.get_group_mode,
                append_group_log_entry=self._append_group_log_entry,
                handle_forwarded_text=lambda sender_id, chat_id, text, message_id: self.on_message(
                    sender_id,
                    chat_id,
                    text,
                    message_id=message_id,
                ),
                fetch_merge_forward_items=self._fetch_merge_forward_items,
                batch_resolve_sender_names=self._batch_resolve_sender_names,
                extract_text=self._extract_text,
            ),
            group_mode_all=self._GROUP_MODE_ALL,
            group_mode_assistant=self._GROUP_MODE_ASSISTANT,
        )

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .timeout(self.request_timeout_seconds) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
            .register_p2_im_chat_disbanded_v1(self._on_raw_chat_disbanded) \
            .register_p2_im_chat_member_bot_deleted_v1(self._on_raw_chat_member_bot_deleted) \
            .register_p2_card_action_trigger(self._on_raw_card_action) \
            .register_p2_application_bot_menu_v6(self._on_raw_bot_menu) \
            .build()

    # ---- 消息收发层 ----

    def _is_duplicate(self, message_id: str) -> bool:
        """检查消息是否重复，同时清理过期条目"""
        with self._dedup_lock:
            now = time.time()
            if message_id in self._seen_messages:
                return True
            # 清理过期条目
            while self._seen_messages:
                oldest_id, ts = next(iter(self._seen_messages.items()))
                if now - ts > _DEDUP_TTL:
                    self._seen_messages.pop(oldest_id)
                else:
                    break
            # 容量上限兜底
            if len(self._seen_messages) >= _DEDUP_MAX_SIZE:
                self._seen_messages.popitem(last=False)
            self._seen_messages[message_id] = now
            return False

    def get_group_mode(self, chat_id: str) -> str:
        return self._group_store.get_group_mode(chat_id)

    def set_group_mode(self, chat_id: str, mode: str) -> str:
        return self._group_store.set_group_mode(chat_id, mode)

    def get_group_acl_snapshot(self, chat_id: str) -> GroupAclSnapshot:
        snapshot = self._group_store.group_snapshot(chat_id)
        return {
            "access_policy": snapshot["access_policy"],
            "allowlist": list(snapshot["allowlist"]),
        }

    def set_group_access_policy(self, chat_id: str, policy: str) -> str:
        return self._group_store.set_access_policy(chat_id, policy)

    def grant_group_members(self, chat_id: str, open_ids: list[str] | set[str]) -> list[str]:
        return self._group_store.grant_members(chat_id, open_ids)

    def revoke_group_members(self, chat_id: str, open_ids: list[str] | set[str]) -> list[str]:
        return self._group_store.revoke_members(chat_id, open_ids)

    def is_admin(self, *, open_id: str = "") -> bool:
        return bool(open_id and open_id in self._admin_open_ids)

    def add_admin_open_id(self, open_id: str) -> list[str]:
        normalized_open_id = str(open_id or "").strip()
        if normalized_open_id:
            self._admin_open_ids.add(normalized_open_id)
        return sorted(self._admin_open_ids)

    def list_admin_open_ids(self) -> list[str]:
        return sorted(self._admin_open_ids)

    def set_configured_bot_open_id(self, open_id: str) -> str:
        normalized_open_id = str(open_id or "").strip()
        self._configured_bot_open_id = normalized_open_id
        if normalized_open_id:
            self._bot_open_id_error_logged = False
        return normalized_open_id

    def is_group_admin(self, *, open_id: str = "") -> bool:
        return self.is_admin(open_id=open_id)

    def is_group_user_allowed(self, chat_id: str, *, open_id: str = "") -> bool:
        if self.is_admin(open_id=open_id):
            return True
        snapshot = self._group_store.group_snapshot(chat_id)
        policy = snapshot["access_policy"]
        if policy == "all-members":
            return True
        if policy == "allowlist":
            return bool(open_id and open_id in set(snapshot["allowlist"]))
        return False

    def get_message_context(self, message_id: str) -> MessageContextPayload:
        if not message_id:
            return {}
        with self._message_context_lock:
            self._cleanup_message_contexts()
            ctx = self._message_contexts.get(message_id)
            if not ctx:
                return {}
            return dict(ctx.payload)

    def remember_chat_type(self, chat_id: str, chat_type: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_chat_type = str(chat_type or "").strip()
        if not normalized_chat_id or not normalized_chat_type:
            return
        with self._chat_type_cache_lock:
            _store_fifo_ttl_entry(
                self._chat_type_cache,
                key=normalized_chat_id,
                value=_CachedChatType(
                    chat_type=normalized_chat_type,
                    created_at=time.time(),
                ),
                ttl_seconds=_CHAT_TYPE_CACHE_TTL,
                max_size=_CHAT_TYPE_CACHE_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def lookup_chat_type(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        with self._chat_type_cache_lock:
            self._cleanup_chat_type_cache()
            cached = self._chat_type_cache.get(normalized_chat_id)
            if not cached:
                return ""
            return cached.chat_type

    def fetch_runtime_chat_type(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        try:
            request = GetChatRequest.builder().chat_id(normalized_chat_id).build()
            response = self.client.im.v1.chat.get(request)
        except Exception as exc:
            logger.warning("查询 chat 类型失败(SDK异常): chat=%s, error=%s", normalized_chat_id, exc)
            return ""
        if not response.success():
            logger.warning("查询 chat 类型失败: chat=%s, code=%s, msg=%s", normalized_chat_id, response.code, response.msg)
            return ""
        data = getattr(response, "data", None)
        chat_mode = str(getattr(data, "chat_mode", "") or "").strip()
        if chat_mode == "p2p":
            self.remember_chat_type(normalized_chat_id, "p2p")
            return "p2p"
        if chat_mode in {"group", "topic"}:
            self.remember_chat_type(normalized_chat_id, "group")
            return "group"
        return ""

    def reserve_execution_card(self, trigger_message_id: str, card_message_id: str) -> None:
        normalized_trigger_id = str(trigger_message_id or "").strip()
        normalized_card_id = str(card_message_id or "").strip()
        if not normalized_trigger_id or not normalized_card_id:
            return
        with self._pending_execution_cards_lock:
            _store_fifo_ttl_entry(
                self._pending_execution_cards,
                key=normalized_trigger_id,
                value=_PendingExecutionCard(
                    card_message_id=normalized_card_id,
                    created_at=time.time(),
                ),
                ttl_seconds=_PENDING_EXECUTION_CARD_TTL,
                max_size=_PENDING_EXECUTION_CARD_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def claim_reserved_execution_card(self, trigger_message_id: str) -> str:
        normalized_trigger_id = str(trigger_message_id or "").strip()
        if not normalized_trigger_id:
            return ""
        with self._pending_execution_cards_lock:
            self._cleanup_pending_execution_cards()
            pending = self._pending_execution_cards.pop(normalized_trigger_id, None)
            if not pending:
                return ""
            return pending.card_message_id

    def extract_non_bot_mentions(self, message_id: str) -> list[MentionMember]:
        context = self.get_message_context(message_id)
        mentions = context.get("mentions") or []
        if not isinstance(mentions, list):
            return []
        trigger_open_ids = self._configured_group_trigger_open_ids()
        members: list[MentionMember] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            open_id = str(mention.get("open_id", "")).strip()
            if open_id and open_id in trigger_open_ids:
                continue
            if not open_id:
                continue
            members.append(
                {
                    "open_id": open_id,
                    "name": str(mention.get("name", "")).strip(),
                }
            )
        return members

    def lookup_cached_sender_name(self, sender_id: str) -> str:
        cache_key = str(sender_id or "").strip()
        if not cache_key:
            return ""
        with self._sender_name_cache_lock:
            cached = self._sender_name_cache.get(cache_key)
            if not cached:
                return ""
            ts, value = cached
            if time.time() - ts > _SENDER_NAME_CACHE_TTL:
                self._sender_name_cache.pop(cache_key, None)
                return ""
            return value

    def get_sender_display_name(self, *, user_id: str = "", open_id: str = "", sender_type: str = "user") -> str:
        return self._display_name_for_sender_identity(
            user_id=user_id,
            sender_principal_id=open_id,
            sender_type=sender_type,
        )

    def _remember_message_context(self, message_id: str, payload: MessageContextPayload) -> None:
        if not message_id:
            return
        with self._message_context_lock:
            _store_fifo_ttl_entry(
                self._message_contexts,
                key=message_id,
                value=_MessageContext(payload=payload.copy(), created_at=time.time()),
                ttl_seconds=_MESSAGE_CONTEXT_TTL,
                max_size=_MESSAGE_CONTEXT_MAX_SIZE,
                created_at=lambda item: item.created_at,
            )

    def _cleanup_message_contexts(self) -> None:
        _evict_expired_fifo_entries(
            self._message_contexts,
            now=time.time(),
            ttl_seconds=_MESSAGE_CONTEXT_TTL,
            created_at=lambda item: item.created_at,
        )

    def _cleanup_chat_type_cache(self) -> None:
        _evict_expired_fifo_entries(
            self._chat_type_cache,
            now=time.time(),
            ttl_seconds=_CHAT_TYPE_CACHE_TTL,
            created_at=lambda item: item.created_at,
        )

    def _cleanup_pending_execution_cards(self) -> None:
        _evict_expired_fifo_entries(
            self._pending_execution_cards,
            now=time.time(),
            ttl_seconds=_PENDING_EXECUTION_CARD_TTL,
            created_at=lambda item: item.created_at,
        )

    def _forget_chat_state(self, chat_id: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        self._group_store.clear_chat(normalized_chat_id)
        with self._chat_type_cache_lock:
            self._chat_type_cache.pop(normalized_chat_id, None)
        with self._message_context_lock:
            stale_message_ids = [
                message_id
                for message_id, ctx in self._message_contexts.items()
                if str(ctx.payload.get("chat_id", "") or "").strip() == normalized_chat_id
            ]
            for message_id in stale_message_ids:
                self._message_contexts.pop(message_id, None)
        self._forward_aggregator.forget_chat(normalized_chat_id)

    @staticmethod
    def _extract_text(msg_type: str, content_dict: dict) -> str:
        """从飞书消息中提取纯文本内容

        - text 类型：直接取 text 字段
        - post 富文本：遍历 content 二维数组，提取所有 tag=text 的文本
        - 其他类型（sticker/image/video/audio 等）：返回空字符串
        """
        if msg_type == "text":
            return content_dict.get("text", "").strip()

        if msg_type == "post":
            # 富文本结构: {"title": "...", "content": [[{"tag": "text", "text": "..."}, ...]]}
            # content 可能在顶层或按语言嵌套（如 content.zh_cn）
            paragraphs = content_dict.get("content")
            if isinstance(paragraphs, dict):
                # 按语言嵌套时取第一个语言的内容
                for lang_content in paragraphs.values():
                    if isinstance(lang_content, dict):
                        paragraphs = lang_content.get("content", [])
                    else:
                        paragraphs = lang_content
                    break
            if not isinstance(paragraphs, list):
                return ""
            parts: list[str] = []
            for para in paragraphs:
                if not isinstance(para, list):
                    continue
                for elem in para:
                    if isinstance(elem, dict) and elem.get("tag") == "text":
                        t = elem.get("text", "").strip()
                        if t:
                            parts.append(t)
            return " ".join(parts)

        if msg_type == "interactive":
            projection = project_interactive_card_text(content_dict)
            return projection.text

        # sticker/image/video/audio 等无文本消息
        return ""

    def _render_message_text(self, msg_type: str, content_dict: dict) -> str:
        text = self._extract_text(msg_type, content_dict)
        if text:
            return text

        if msg_type == "share_user":
            # 飞书 `share_user` 消息内容字段名为 `user_id`，但其值实际是 open_id。
            shared_open_id = str(content_dict.get("user_id", "") or "").strip()
            if not shared_open_id:
                return "[个人名片]"
            shared_name = self._resolve_sender_name(shared_open_id)
            self._cache_sender_name(shared_open_id, value=shared_name)
            return f"[个人名片] {shared_name}"

        if msg_type == "share_chat":
            shared_chat_id = str(content_dict.get("chat_id", "") or "").strip()
            return f"[群名片] {shared_chat_id}" if shared_chat_id else "[群名片]"

        if msg_type == "hongbao":
            text = str(content_dict.get("text", "") or "").strip()
            return text or "[红包]"

        if msg_type in {"share_calendar_event", "calendar", "general_calendar"}:
            summary = str(content_dict.get("summary", "") or "").strip()
            return f"[日程] {summary}" if summary else "[日程]"

        if msg_type == "system":
            template = str(content_dict.get("template", "") or "").strip()
            return f"[系统消息] {template}" if template else "[系统消息]"

        return ""

    @staticmethod
    def _attachment_message_name(msg_type: str, content_dict: dict) -> str:
        if msg_type == "image":
            return ""
        if msg_type == "audio":
            return str(content_dict.get("file_name", "") or "").strip() or "语音"
        return str(content_dict.get("file_name", "") or "").strip()

    @staticmethod
    def _attachment_resource_key(msg_type: str, content_dict: dict) -> str:
        if msg_type == "image":
            return str(content_dict.get("image_key", "") or "").strip()
        return str(content_dict.get("file_key", "") or "").strip()

    @staticmethod
    def _mention_payload(mention: Any) -> MentionPayload:
        if isinstance(mention, dict):
            key = str(mention.get("key", "") or "").strip()
            name = str(mention.get("name", "") or "").strip()
            direct_open_id = str(mention.get("open_id", "") or "").strip()
            mention_id = mention.get("id")
        else:
            key = str(getattr(mention, "key", "") or "").strip()
            name = str(getattr(mention, "name", "") or "").strip()
            direct_open_id = str(getattr(mention, "open_id", "") or "").strip()
            mention_id = getattr(mention, "id", None)

        open_id = ""
        if isinstance(mention_id, dict):
            open_id = str(mention_id.get("open_id", "") or mention_id.get("id", "") or "").strip()
        elif isinstance(mention_id, str):
            open_id = mention_id.strip()
        elif mention_id is not None:
            open_id = str(
                getattr(mention_id, "open_id", "") or getattr(mention_id, "id", "") or ""
            ).strip()

        return {
            "key": key,
            "name": name,
            "open_id": direct_open_id or open_id,
        }

    def _configured_group_trigger_open_ids(self) -> set[str]:
        if not self._configured_bot_open_id:
            return set()
        return {self._configured_bot_open_id, *self._configured_trigger_open_ids}

    def _normalize_mentions(self, text: str, mentions: list) -> str:
        """群聊消息中去掉触发 mention，同时保留其他 @成员 的可读文本。"""
        normalized = text
        trigger_open_ids = self._configured_group_trigger_open_ids()
        for mention in mentions:
            payload = self._mention_payload(mention)
            key = payload["key"]
            mention_open_id = payload["open_id"]
            mention_name = str(
                payload["name"]
                or mention_open_id[:8]
            ).strip()
            if not key:
                continue
            if mention_open_id and mention_open_id in trigger_open_ids:
                normalized = normalized.replace(key, "")
            else:
                normalized = normalized.replace(key, f"@{mention_name}")
        return " ".join(normalized.split())

    @staticmethod
    def _sender_ids(sender_id: Any) -> tuple[str, str]:
        if sender_id is None:
            return "", ""
        return (
            str(getattr(sender_id, "user_id", "") or "").strip(),
            str(getattr(sender_id, "open_id", "") or "").strip(),
        )

    def _cache_sender_name(self, *keys: str, value: str) -> None:
        normalized_value = str(value or "").strip()
        if not normalized_value:
            return
        now = time.time()
        with self._sender_name_cache_lock:
            for key in keys:
                cache_key = str(key or "").strip()
                if cache_key:
                    self._sender_name_cache[cache_key] = (now, normalized_value)

    def _display_name_for_sender_identity(
        self,
        *,
        user_id: str = "",
        sender_principal_id: str = "",
        sender_type: str = "user",
    ) -> str:
        if sender_type == "app":
            cache_key = sender_principal_id or user_id
            cached = self.lookup_cached_sender_name(cache_key)
            if cached:
                return cached
            short_id = (sender_principal_id or user_id or "unknown")[:8]
            return f"机器人:{short_id}"
        cached = self.lookup_cached_sender_name(sender_principal_id) or self.lookup_cached_sender_name(user_id)
        if cached:
            return cached
        if sender_principal_id:
            resolution = self._resolve_sender_name_diagnostic(sender_principal_id)
            resolved = str(resolution.get("resolved_name", "") or "").strip()
            if resolved and not bool(resolution.get("used_fallback")):
                self._cache_sender_name(sender_principal_id, user_id, value=resolved)
            return resolved or sender_principal_id[:8]
        if user_id:
            self._cache_sender_name(user_id, value=user_id[:8])
            return user_id[:8]
        return "unknown"

    def _sender_log_fields(
        self,
        *,
        user_id: str = "",
        sender_principal_id: str = "",
        sender_type: str = "user",
    ) -> tuple[str, str, str]:
        return (
            self._display_name_for_sender_identity(
                user_id=user_id,
                sender_principal_id=sender_principal_id,
                sender_type=sender_type,
            ),
            sender_principal_id or "-",
            user_id or "-",
        )

    # ---- 转发消息聚合 ----

    def _pop_pending_forward(self, sender_id: str, chat_id: str) -> Optional[PendingForward]:
        """取出并清除指定用户/会话的待合并转发消息，同时取消其超时定时器

        Returns:
            待合并的转发消息，若不存在则返回 None
        """
        return self._forward_aggregator.pop_pending_forward(sender_id, chat_id)

    def _buffer_forward(
        self, sender_id: str, chat_id: str, forwarded_text: str,
        message_id: str, chat_type: str,
        *,
        sender_user_id: str = "",
        sender_open_id: str = "",
        sender_type: str = "user",
        created_at: int = 0,
        thread_id: str = "",
    ) -> None:
        """暂存合并转发消息，启动超时定时器等待后续留言

        若同一 (sender_id, chat_id) 已有暂存转发，先取消旧定时器再覆盖。
        """
        self._forward_aggregator.buffer_forward(
            sender_id,
            chat_id,
            forwarded_text,
            message_id,
            chat_type,
            sender_user_id=sender_user_id,
            sender_open_id=sender_open_id,
            sender_type=sender_type,
            created_at=created_at,
            thread_id=thread_id,
        )

    def _on_forward_timeout(self, sender_id: str, chat_id: str) -> None:
        """超时未收到留言，单独处理暂存的转发消息

        私聊和 `all` 模式群聊中，转发消息可独立处理。
        `mention_only` 模式群聊中，因无 @mention 上下文，静默丢弃。
        `assistant` 模式群聊中，直接写入群聊日志，供后续有效触发时读取。
        """
        self._forward_aggregator.on_forward_timeout(sender_id, chat_id)

    def _fetch_bot_open_id(self) -> Optional[str]:
        """调用飞书 API 获取机器人自身的 open_id，仅供 `/whoareyou` 之类的显式探测使用。"""
        try:
            req = lark.BaseRequest.builder() \
                .http_method(lark.HttpMethod.GET) \
                .uri("/open-apis/bot/v3/info/") \
                .token_types({lark.AccessTokenType.TENANT}) \
                .build()
            resp = self.client.request(req)
            if not resp.success():
                logger.warning("获取机器人信息失败: code=%s, msg=%s", resp.code, resp.msg)
                return None
            data = json.loads(resp.raw.content)
            open_id = data.get("bot", {}).get("open_id")
            if open_id:
                logger.info("获取机器人 open_id: %s", open_id)
            return open_id
        except Exception as e:
            logger.warning("获取机器人信息异常: %s", e)
            return None

    def get_bot_identity_snapshot(self) -> BotIdentitySnapshot:
        discovered_open_id = self._fetch_bot_open_id() or ""
        return {
            "app_id": self.app_id,
            "configured_open_id": self._configured_bot_open_id,
            "discovered_open_id": discovered_open_id,
            "trigger_open_ids": sorted(self._configured_trigger_open_ids),
        }

    def _is_bot_mentioned(self, mentions: list) -> bool:
        """判断 mentions 列表中是否包含有效触发 open_id。"""
        if not mentions:
            return False
        trigger_open_ids = self._configured_group_trigger_open_ids()
        if not trigger_open_ids:
            if not self._bot_open_id_error_logged:
                logger.error(
                    "未配置 `system.yaml.bot_open_id`，群聊显式 mention 触发已严格失败。"
                    "如需自动写入，可私聊机器人执行 `/init <token>`；"
                    "如需人工诊断，可先执行 `/whoareyou`。"
                )
                self._bot_open_id_error_logged = True
            return False
        for mention in mentions:
            if self._mention_payload(mention)["open_id"] in trigger_open_ids:
                return True
        return False

    def _resolve_sender_name(self, open_id: str) -> str:
        """通过 open_id 查询用户姓名，失败时返回 open_id 前 8 位作为兜底"""
        snapshot = self._resolve_sender_name_diagnostic(open_id)
        return str(snapshot.get("resolved_name", "") or open_id[:8]).strip() or open_id[:8]

    def _log_sender_name_resolution_fallback(self, snapshot: dict[str, Any]) -> None:
        open_id = str(snapshot.get("open_id", "") or "").strip() or "unknown"
        fallback_reason = str(snapshot.get("fallback_reason", "") or "unknown").strip() or "unknown"
        cache_key = (open_id, fallback_reason)
        level = logging.WARNING
        now = time.time()
        with self._sender_name_warning_lock:
            last_at = self._sender_name_warning_timestamps.get(cache_key, 0.0)
            if now - last_at < _SENDER_NAME_FAILURE_WARNING_TTL:
                level = logging.DEBUG
            else:
                self._sender_name_warning_timestamps[cache_key] = now
        extra_parts: list[str] = [f"reason={fallback_reason}"]
        api_code = snapshot.get("api_code")
        api_msg = str(snapshot.get("api_msg", "") or "").strip()
        if api_code not in (None, ""):
            extra_parts.append(f"code={api_code}")
        if api_msg:
            extra_parts.append(f"msg={api_msg}")
        exception_text = str(snapshot.get("exception", "") or "").strip()
        if exception_text:
            extra_parts.append(f"error={exception_text}")
        logger.log(
            level,
            "发送者姓名解析回退: open_id=%s, %s",
            open_id,
            ", ".join(extra_parts),
        )

    def _resolve_sender_name_diagnostic(
        self,
        open_id: str,
        *,
        log_failures: bool = True,
    ) -> dict[str, Any]:
        normalized_open_id = str(open_id or "").strip()
        fallback_name = normalized_open_id[:8] or "unknown"
        snapshot: dict[str, Any] = {
            "open_id": normalized_open_id,
            "resolved_name": fallback_name,
            "used_fallback": False,
            "fallback_reason": "",
            "api_code": "",
            "api_msg": "",
            "exception": "",
            "source": "contact_api",
        }
        if not normalized_open_id:
            snapshot.update(
                resolved_name="unknown",
                used_fallback=True,
                fallback_reason="empty_open_id",
                source="fallback",
            )
            return snapshot
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest as GetContactUserReq
            request = (GetContactUserReq.builder()
                       .user_id(normalized_open_id)
                       .user_id_type("open_id")
                       .build())
            response = self.client.contact.v3.user.get(request)
            if response.success() and response.data and response.data.user:
                name = response.data.user.name or response.data.user.nickname
                if name:
                    snapshot["resolved_name"] = str(name).strip()
                    return snapshot
                snapshot.update(
                    used_fallback=True,
                    fallback_reason="empty_name",
                    source="fallback",
                )
            else:
                snapshot.update(
                    used_fallback=True,
                    fallback_reason="api_non_success" if not response.success() else "empty_user",
                    api_code=getattr(response, "code", ""),
                    api_msg=str(getattr(response, "msg", "") or "").strip(),
                    source="fallback",
                )
        except Exception as e:
            snapshot.update(
                used_fallback=True,
                fallback_reason="exception",
                exception=str(e),
                source="fallback",
            )
        if bool(snapshot.get("used_fallback")) and log_failures:
            self._log_sender_name_resolution_fallback(snapshot)
        return snapshot

    def _batch_resolve_sender_names(self, open_ids: set[str]) -> dict[str, str]:
        """批量解析 open_id → 用户姓名，返回映射表"""
        name_map: dict[str, str] = {}
        for oid in open_ids:
            name_map[oid] = self._display_name_for_sender_identity(
                sender_principal_id=oid,
                sender_type="user",
            )
        return name_map

    def debug_sender_name_resolution(self, open_id: str) -> dict[str, Any]:
        normalized_open_id = str(open_id or "").strip()
        cached_name = self.lookup_cached_sender_name(normalized_open_id)
        live = self._resolve_sender_name_diagnostic(
            normalized_open_id,
            log_failures=False,
        )
        resolved_name = str(live.get("resolved_name", "") or "").strip()
        if resolved_name and not bool(live.get("used_fallback")):
            self._cache_sender_name(normalized_open_id, value=resolved_name)
        return {
            "open_id": normalized_open_id,
            "cache_hit": bool(cached_name),
            "cached_name": cached_name,
            **live,
        }

    @staticmethod
    def _mention_payloads(mentions: list) -> list[MentionPayload]:
        payloads: list[MentionPayload] = []
        for mention in mentions:
            payloads.append(FeishuBot._mention_payload(mention))
        return payloads

    @staticmethod
    def _is_group_control_text(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        return normalized.startswith("/")

    @staticmethod
    def _group_scope_key(thread_id: str = "") -> str:
        return GroupHistoryRecovery.group_scope_key(thread_id)

    @staticmethod
    def _thread_id_for_scope(scope: str) -> str:
        return GroupHistoryRecovery.thread_id_for_scope(scope)

    def _append_group_log_entry(
        self,
        *,
        chat_id: str,
        message_id: str,
        created_at: int | str | None,
        sender_user_id: str,
        sender_open_id: str,
        sender_type: str,
        msg_type: str,
        thread_id: str = "",
        text: str,
    ) -> int:
        sender_name = self._display_name_for_sender_identity(
            user_id=sender_user_id,
            sender_principal_id=sender_open_id,
            sender_type=sender_type,
        )
        entry: GroupMessageEntry = {
            "message_id": str(message_id or ""),
            "created_at": int(created_at or 0),
            "sender_user_id": sender_user_id,
            "sender_principal_id": sender_open_id,
            "sender_type": sender_type,
            "sender_name": sender_name,
            "msg_type": msg_type,
            "thread_id": str(thread_id or "").strip(),
            "text": text,
        }
        return self._group_store.append_message(chat_id, entry)

    def _read_group_history_local_messages(
        self,
        chat_id: str,
        *,
        after_seq: int,
        before_seq: int | None,
        scope: str,
    ) -> list[GroupMessageEntry]:
        return self._group_store.read_messages_between(
            chat_id,
            after_seq=after_seq,
            before_seq=before_seq,
            scope=scope,
        )

    def _get_group_history_boundary_seq(self, chat_id: str, *, scope: str) -> int:
        return self._group_store.get_last_boundary_seq(chat_id, scope=scope)

    def _get_group_history_boundary_created_at(self, chat_id: str, *, scope: str) -> int:
        return self._group_store.get_last_boundary_created_at(chat_id, scope=scope)

    def _get_group_history_boundary_message_ids(self, chat_id: str, *, scope: str) -> list[str]:
        return self._group_store.get_last_boundary_message_ids(chat_id, scope=scope)

    def _list_history_messages_page(
        self,
        *,
        container_id_type: str,
        container_id: str,
        sort_type: str,
        page_size: int = 50,
        page_token: str = "",
        start_time: int | str | None = None,
        end_time: int | str | None = None,
    ) -> ListedMessagesPage:
        builder = (
            ListMessageRequest.builder()
            .container_id_type(container_id_type)
            .container_id(container_id)
            .sort_type(sort_type)
            .page_size(page_size)
        )
        if start_time is not None:
            builder = builder.start_time(str(start_time))
        if end_time is not None:
            builder = builder.end_time(str(end_time))
        if page_token:
            builder = builder.page_token(page_token)
        request = builder.build()
        response = self.client.im.v1.message.list(request)
        if not response.success():
            raise RuntimeError(f"code={response.code}, msg={response.msg}")

        body = response.data
        return ListedMessagesPage(
            items=list(getattr(body, "items", None) or []),
            has_more=bool(getattr(body, "has_more", False)),
            page_token=str(getattr(body, "page_token", "") or "").strip(),
        )

    def _history_recovery_enabled(self) -> bool:
        """Whether assistant mode should perform any history recovery at all.

        `group_history_fetch_limit` and `group_history_fetch_lookback_seconds`
        jointly act as the global recovery switch. For thread containers the
        Feishu API does not support start/end time filters, but setting either
        value to 0 still disables all recovery paths for consistency.
        """
        return self._history_recovery.history_recovery_enabled()

    @staticmethod
    def _group_context_sort_key(item: GroupMessageEntry) -> tuple[int, int, int, str]:
        return GroupHistoryRecovery.group_context_sort_key(item)

    def _collect_assistant_context_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time: int | str | None,
        current_seq: int,
        thread_id: str = "",
    ) -> list[GroupMessageEntry]:
        return self._history_recovery.collect_assistant_context_entries(
            chat_id=chat_id,
            current_message_id=current_message_id,
            current_create_time=current_create_time,
            current_seq=current_seq,
            thread_id=thread_id,
        )

    @staticmethod
    def _collect_boundary_message_ids(
        *,
        current_message_id: str,
        current_created_at: int | str | None,
        context_entries: list[GroupMessageEntry],
    ) -> list[str]:
        return GroupHistoryRecovery.collect_boundary_message_ids(
            current_message_id=current_message_id,
            current_created_at=current_created_at,
            context_entries=context_entries,
        )

    def _prepare_group_history_execution_card(self, chat_id: str, parent_message_id: str) -> None:
        normalized_parent_id = str(parent_message_id or "").strip()
        if not normalized_parent_id:
            return
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Codex（准备群聊上下文）"},
                "template": "turquoise",
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "*正在回捞最近的群聊历史并准备上下文，请稍候。*",
                    }
                ]
            },
        }
        content = json.dumps(card, ensure_ascii=False)
        card_message_id = self.reply_to_message(normalized_parent_id, "interactive", content)
        if not card_message_id:
            card_message_id = self.send_message_get_id(chat_id, "interactive", content)
        if card_message_id:
            self.reserve_execution_card(normalized_parent_id, card_message_id)

    def _notify_group_history_fetch_failed(
        self,
        *,
        chat_id: str,
        parent_message_id: str,
        error: Exception,
    ) -> None:
        reason = str(error).strip() or type(error).__name__
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Codex（群聊上下文准备失败）"},
                "template": "red",
            },
            "body": {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            "*本次 assistant 响应已停止，因为群历史回捞失败。*\n\n"
                            f"错误：`{reason}`\n\n"
                            "建议排查：\n"
                            "- 检查应用是否已开通 `im:message.group_msg`、`im:message:readonly`\n"
                            "- 检查群消息历史是否对机器人可见\n"
                            "- 检查飞书 API / 网络是否异常\n"
                            "- 如需先继续使用群聊，可临时显式 mention 触发对象后执行 `/groupmode mention-only`"
                        ),
                    }
                ]
            },
        }
        content = json.dumps(card, ensure_ascii=False)
        reserved_id = self.claim_reserved_execution_card(parent_message_id)
        if reserved_id and self.patch_message(reserved_id, content):
            return
        if parent_message_id:
            reply_id = self.reply_to_message(parent_message_id, "interactive", content)
            if reply_id:
                return
        self.send_message(chat_id, "interactive", content)

    @staticmethod
    def _format_ts(ts_ms: int | str | None) -> str:
        return GroupHistoryRecovery.format_ts(ts_ms)

    def _format_group_context_entries(self, entries: list[GroupMessageEntry]) -> str:
        return self._history_recovery.format_group_context_entries(entries)

    def _build_assistant_turn_text(
        self,
        context_text: str,
        current_text: str,
        log_path: pathlib.Path,
        *,
        thread_id: str = "",
        current_sender_name: str = "",
    ) -> str:
        return self._history_recovery.build_assistant_turn_text(
            context_text,
            current_text,
            log_path,
            thread_id=thread_id,
            current_sender_name=current_sender_name,
        )

    def _build_group_current_turn_text(self, current_text: str, *, sender_name: str) -> str:
        return self._history_recovery.build_group_current_turn_text(
            current_text,
            sender_name=sender_name,
        )

    @staticmethod
    def _group_acl_denied_text(group_mode: str) -> str:
        normalized_mode = str(group_mode or "").strip().lower()
        if normalized_mode == "all":
            trigger_rule = "当前群工作态是 `all`：已授权成员可直接发消息触发。"
        else:
            trigger_rule = (
                "当前群工作态是 `assistant` / `mention-only`："
                "已授权成员仍需先显式 mention 触发对象。"
            )
        return (
            "当前群仅管理员或已授权成员具备触发资格。\n"
            f"{trigger_rule}\n"
            "管理员可发送 `/acl` 查看或调整当前群的授权策略。"
        )

    def _fetch_merge_forward_items(self, merge_message_id: str) -> list[Any]:
        try:
            request = GetMessageRequest.builder().message_id(merge_message_id).build()
            response = self.client.im.v1.message.get(request)
            if not response.success():
                logger.warning(
                    "获取合并转发消息失败: message_id=%s, code=%s, msg=%s",
                    merge_message_id,
                    response.code,
                    response.msg,
                )
                return []
            return list(response.data.items or [])
        except Exception as exc:
            logger.warning(
                "获取合并转发消息异常: message_id=%s, error=%s",
                merge_message_id,
                exc,
            )
            return []

    def _fetch_merge_forward_text(self, merge_message_id: str) -> str:
        return self._forward_aggregator.fetch_merge_forward_text(merge_message_id)

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """解析原始消息，根据消息类型分发到对应处理方法

        群聊是否触发，取决于当前工作态与有效 mention 判定。
        """
        try:
            self._handle_raw_message(data)
        except Exception as e:
            logger.error("处理消息事件异常: %s", e, exc_info=True)

    def _handle_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """_on_raw_message 的实际逻辑，拆分以便顶层异常捕获

        合并转发消息聚合策略:
        飞书将用户的"转发+留言"拆为两条独立事件（先 merge_forward，后 text）。
        为将它们作为一条指令处理，merge_forward 到达时先暂存到缓冲区，
        等待短时间窗口内同一用户同一会话的后续消息。若后续消息到达则合并处理，
        超时则按当前会话类型处理：私聊直接转发，`assistant` 群聊写入日志，
        `all` 群聊直接转发，`mention_only` 群聊丢弃。
        """
        message = data.event.message
        sender = data.event.sender
        sender_type = getattr(sender, "sender_type", "") or "user"
        sender_user_id, sender_open_id = self._sender_ids(getattr(sender, "sender_id", None))
        sender_id = str(sender_open_id or "").strip()
        chat_id = message.chat_id
        message_id = message.message_id
        msg_type = message.message_type
        chat_type = getattr(message, "chat_type", None) or "p2p"
        thread_id = str(getattr(message, "thread_id", "") or "").strip()
        root_id = str(getattr(message, "root_id", "") or "").strip()
        parent_id = str(getattr(message, "parent_id", "") or "").strip()
        mentions = getattr(message, "mentions", None) or []
        group_mode = self.get_group_mode(chat_id) if chat_type == "group" else ""
        control_text = False
        self.remember_chat_type(chat_id, chat_type)

        # 消息去重，防止飞书重试导致重复处理
        if self._is_duplicate(message_id):
            logger.info("跳过重复消息: message_id=%s", message_id)
            return

        # 精确判断是否命中了有效触发 mention（机器人自身或配置的 alias）
        bot_mentioned = self._is_bot_mentioned(mentions)

        # ---- 合并转发消息：暂存到缓冲区，等待后续留言 ----
        # 合并转发的 content 不是 JSON（是固定字符串 "Merged and Forwarded Message"），
        # 需要在 JSON 解析之前单独处理。
        # 注意：merge_forward 在群聊中不携带 @mention，所以要绕过群聊过滤先暂存。
        if msg_type == "merge_forward":
            logger.info("收到合并转发: user=%s, chat_type=%s, message_id=%s",
                        sender_id, chat_type, message_id)
            text = self._fetch_merge_forward_text(message_id)
            if not text:
                logger.warning("合并转发消息提取文本为空: message_id=%s", message_id)
                # 仅在非群聊或有权响应时回复提示
                if chat_type != "group" or group_mode == self._GROUP_MODE_ALL:
                    self.reply(chat_id, "合并转发的消息中未包含可识别的文本内容。")
                return
            logger.info("合并转发提取完成，暂存等待留言: user=%s, message_id=%s, text=%s",
                        sender_id, message_id, text[:200])
            self._buffer_forward(
                sender_id,
                chat_id,
                text,
                message_id,
                chat_type,
                sender_user_id=sender_user_id,
                sender_open_id=sender_open_id,
                sender_type=sender_type,
                created_at=message.create_time,
                thread_id=thread_id,
            )
            return

        try:
            content_dict = json.loads(message.content)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                "消息内容解析失败: message_id=%s, msg_type=%s, error=%s, raw_content=%r",
                message_id, msg_type, type(e).__name__, message.content,
            )
            return

        sender_name, sender_open_log, sender_user_log = self._sender_log_fields(
            user_id=sender_user_id,
            sender_principal_id=sender_open_id,
            sender_type=sender_type,
        )
        logger.info(
            "收到原始消息: name=%s, open_id=%s, user_id=%s, chat_type=%s, msg_type=%s, message_id=%s, content=%s",
            sender_name, sender_open_log, sender_user_log, chat_type, msg_type, message_id, message.content,
        )

        is_attachment_message = msg_type in _ATTACHMENT_MESSAGE_TYPES
        text = ""
        if is_attachment_message:
            attachment_name = self._attachment_message_name(msg_type, content_dict)
            label = {
                "image": "图片",
                "file": "文件",
                "audio": "音频",
                "media": "媒体",
                "sticker": "表情包",
                "folder": "文件夹",
            }.get(msg_type, "附件")
            text = f"[{label}] {attachment_name}".strip()
            logger.info(
                "收到附件: name=%s, open_id=%s, user_id=%s, chat_type=%s, msg_type=%s, message_id=%s, file=%s",
                sender_name,
                sender_open_log,
                sender_user_log,
                chat_type,
                msg_type,
                message_id,
                attachment_name,
            )
        else:
            text = self._render_message_text(msg_type, content_dict)
        if chat_type == "group" and mentions:
            text = self._normalize_mentions(text, mentions)
        pending = None if is_attachment_message else self._pop_pending_forward(sender_id, chat_id)
        if pending:
            text = (
                f"<forwarded_messages>\n{pending.forwarded_text}\n</forwarded_messages>"
                + (f"\n\n{text}" if text else "")
            ).strip()
            logger.info(
                "转发消息与留言已合并: name=%s, open_id=%s, user_id=%s, chat=%s, forward_msg=%s",
                sender_name,
                sender_open_log,
                sender_user_log,
                chat_id,
                pending.message_id,
            )

        self._remember_message_context(
            message_id,
            {
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_user_id": sender_user_id,
                "sender_open_id": sender_open_id,
                "sender_type": sender_type,
                "bot_mentioned": bot_mentioned,
                "message_type": msg_type,
                "thread_id": thread_id,
                "root_id": root_id,
                "parent_id": parent_id,
                "text": text,
                "mentions": self._mention_payloads(mentions),
            },
        )

        if chat_type == "group" and sender_type == "app":
            logger.debug("忽略群聊机器人消息事件: chat=%s, message_id=%s", chat_id, message_id)
            return

        if chat_type == "group":
            control_text = self._is_group_control_text(text)
            allowed_to_use = self.is_group_user_allowed(chat_id, open_id=sender_open_id)
            if group_mode == self._GROUP_MODE_ASSISTANT:
                if is_attachment_message:
                    if not allowed_to_use:
                        return
                else:
                    log_text = text
                    if bot_mentioned and not log_text and not control_text:
                        log_text = "[@触发]"
                    current_seq = 0
                    if log_text and not control_text:
                        current_seq = self._append_group_log_entry(
                            chat_id=chat_id,
                            message_id=message_id,
                            created_at=message.create_time,
                            sender_user_id=sender_user_id,
                            sender_open_id=sender_open_id,
                            sender_type=sender_type,
                            msg_type=msg_type,
                            thread_id=thread_id,
                            text=log_text,
                        )
                    if not bot_mentioned:
                        return
                    if not allowed_to_use:
                        self.reply(
                            chat_id,
                            self._group_acl_denied_text(group_mode),
                            parent_message_id=message_id,
                        )
                        return
                    if control_text:
                        self.on_message(sender_id, chat_id, text, message_id=message_id)
                        return
                    if not self.allow_group_prompt(sender_id, chat_id, message_id=message_id):
                        return
                    if self._history_recovery_enabled():
                        self._prepare_group_history_execution_card(chat_id, message_id)
                    try:
                        context_entries = self._collect_assistant_context_entries(
                            chat_id=chat_id,
                            current_message_id=message_id,
                            current_create_time=message.create_time,
                            current_seq=current_seq,
                            thread_id=thread_id,
                        )
                    except Exception as exc:
                        logger.warning("群历史回捞失败: chat=%s, error=%s", chat_id, exc)
                        self._notify_group_history_fetch_failed(
                            chat_id=chat_id,
                            parent_message_id=message_id,
                            error=exc,
                        )
                        return
                    assistant_text = self._build_assistant_turn_text(
                        self._format_group_context_entries(context_entries),
                        text,
                        self._group_store.log_path(chat_id),
                        thread_id=thread_id,
                        current_sender_name=sender_name,
                    )
                    if current_seq:
                        boundary_message_ids = self._collect_boundary_message_ids(
                            current_message_id=message_id,
                            current_created_at=message.create_time,
                            context_entries=context_entries,
                        )
                        self._group_store.set_last_boundary(
                            chat_id,
                            seq=current_seq,
                            created_at=message.create_time,
                            message_ids=boundary_message_ids,
                            scope=self._group_scope_key(thread_id),
                        )
                    self.on_message(sender_id, chat_id, assistant_text, message_id=message_id)
                    return

            if group_mode == self._GROUP_MODE_MENTION and not bot_mentioned and not is_attachment_message:
                logger.debug("忽略群聊非触发 mention 消息: chat=%s, user=%s", chat_id, sender_user_id)
                return

            if not allowed_to_use:
                if not is_attachment_message and (bot_mentioned or text.startswith("/")):
                    self.reply(
                        chat_id,
                        self._group_acl_denied_text(group_mode),
                        parent_message_id=message_id,
                    )
                return
            if not is_attachment_message and not control_text and not self.allow_group_prompt(
                sender_id,
                chat_id,
                message_id=message_id,
            ):
                return
        if is_attachment_message:
            resource_key = self._attachment_resource_key(msg_type, content_dict)
            attachment_name = self._attachment_message_name(msg_type, content_dict)
            self.on_attachment_message(
                sender_id,
                chat_id,
                message_id,
                msg_type,
                resource_key,
                attachment_name,
            )
            return

        if not text:
            if chat_type == "group" and bot_mentioned:
                self.on_message(
                    sender_id,
                    chat_id,
                    self._build_group_current_turn_text("", sender_name=sender_name),
                    message_id=message_id,
                )
            elif chat_type != "group":
                logger.info(
                    "忽略空文本消息: name=%s, open_id=%s, user_id=%s, msg_type=%s, message_id=%s",
                    sender_name, sender_open_log, sender_user_log, msg_type, message_id,
                )
                self.reply(chat_id, "当前仅支持文本消息，请直接输入文字。")
            return

        logger.info(
            "收到消息: name=%s, open_id=%s, user_id=%s, chat_type=%s, message_id=%s, text=%s",
            sender_name, sender_open_log, sender_user_log, chat_type, message_id, text,
        )
        outbound_text = text
        if chat_type == "group" and not control_text:
            outbound_text = self._build_group_current_turn_text(
                text,
                sender_name=sender_name,
            )
        self.on_message(sender_id, chat_id, outbound_text, message_id=message_id)

    def _on_raw_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """解析卡片按钮点击事件，交给子类处理"""
        try:
            user_id = data.event.operator.user_id
            operator_open_id = str(getattr(data.event.operator, "open_id", "") or "").strip()
            chat_id = data.event.context.open_chat_id
            message_id = data.event.context.open_message_id
            action_value = data.event.action.value or {}
            if operator_open_id:
                action_value["_operator_open_id"] = operator_open_id
            # 表单提交时携带输入框的值，注入 action_value 供处理器读取
            if data.event.action.form_value:
                action_value["_form_value"] = data.event.action.form_value
            logger.info("卡片点击: user=%s, action=%s", user_id, action_value)
            return self.on_card_action(operator_open_id, chat_id, message_id, action_value)
        except Exception as e:
            logger.error("处理卡片事件异常: %s", e, exc_info=True)
            return P2CardActionTriggerResponse()

    def _on_raw_bot_menu(self, data: P2ApplicationBotMenuV6) -> None:
        """解析机器人菜单点击事件，交给子类处理"""
        try:
            operator = data.event.operator
            user_id = operator.operator_id.user_id
            open_id = operator.operator_id.open_id
            event_key = data.event.event_key
            logger.info("菜单点击: user=%s, event_key=%s", user_id, event_key)
            self.on_bot_menu(open_id, event_key)
        except Exception as e:
            logger.error("处理菜单事件异常: %s", e, exc_info=True)

    def _on_raw_chat_disbanded(self, data: P2ImChatDisbandedV1) -> None:
        try:
            chat_id = str(data.event.chat_id or "").strip()
            if not chat_id:
                return
            logger.info("群聊已解散: chat=%s", chat_id)
            self._forget_chat_state(chat_id)
            self.on_chat_unavailable(chat_id, reason="disbanded")
        except Exception as e:
            logger.error("处理群解散事件异常: %s", e, exc_info=True)

    def _on_raw_chat_member_bot_deleted(self, data: P2ImChatMemberBotDeletedV1) -> None:
        try:
            chat_id = str(data.event.chat_id or "").strip()
            if not chat_id:
                return
            logger.info("机器人已被移出群聊: chat=%s", chat_id)
            self._forget_chat_state(chat_id)
            self.on_chat_unavailable(chat_id, reason="bot_removed")
        except Exception as e:
            logger.error("处理机器人出群事件异常: %s", e, exc_info=True)

    @staticmethod
    def _detect_id_type(receive_id: str) -> str:
        """根据 ID 前缀自动判断 receive_id_type（ou_ → open_id，默认 chat_id）"""
        if receive_id.startswith("ou_"):
            return "open_id"
        return "chat_id"

    def send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """发送任意类型消息"""
        id_type = self._detect_id_type(chat_id)
        request = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        logger.info(
            "发送消息: receive_id=%s, receive_id_type=%s, msg_type=%s, timeout=%.1fs",
            chat_id,
            id_type,
            msg_type,
            self.request_timeout_seconds,
        )
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as e:
            logger.exception("发送消息失败(SDK异常): %s", e)
            return
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)
            return
        try:
            message_id = response.data.message_id
        except AttributeError:
            message_id = ""
        logger.info("发送消息成功: receive_id=%s, message_id=%s, msg_type=%s", chat_id, message_id, msg_type)

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> Optional[str]:
        """发送消息并返回 message_id，失败时返回 None"""
        id_type = self._detect_id_type(chat_id)
        request = CreateMessageRequest.builder() \
            .receive_id_type(id_type) \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()) \
            .build()
        logger.info(
            "发送消息(取ID): receive_id=%s, receive_id_type=%s, msg_type=%s, timeout=%.1fs",
            chat_id,
            id_type,
            msg_type,
            self.request_timeout_seconds,
        )
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as e:
            logger.exception("发送消息失败(SDK异常): %s", e)
            return None
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            message_id = response.data.message_id
        except AttributeError:
            return None
        logger.info("发送消息成功: receive_id=%s, message_id=%s, msg_type=%s", chat_id, message_id, msg_type)
        return message_id

    def patch_message(self, message_id: str, content: str) -> bool:
        """更新已发送消息的文本内容

        Returns:
            更新是否成功
        """
        request = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(PatchMessageRequestBody.builder()
                .content(content)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.patch(request)
        except Exception as e:
            logger.error("消息更新失败(SDK异常): %s", e)
            return False
        if not response.success():
            logger.error(
                "消息更新失败: code=%s, msg=%s, ext=%s",
                response.code, response.msg,
                getattr(response, 'raw', {}).get('ext', '') if isinstance(getattr(response, 'raw', None), dict) else '',
            )
            return False
        return True

    def _should_reply_in_thread(self, parent_message_id: str, explicit_reply_in_thread: bool) -> bool:
        if explicit_reply_in_thread:
            return True
        context = self.get_message_context(parent_message_id)
        return bool(str(context.get("thread_id", "") or "").strip())

    def reply(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        """发送文本消息"""
        content = json.dumps({"text": text})
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            self.reply_to_message(
                normalized_parent_id,
                "text",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
            return
        self.send_message(chat_id, "text", content)

    def reply_card(
        self,
        chat_id: str,
        card: dict,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        """发送交互卡片消息"""
        content = json.dumps(card)
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            self.reply_to_message(
                normalized_parent_id,
                "interactive",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
            return
        self.send_message(chat_id, "interactive", content)

    def reply_to_message(
        self,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> Optional[str]:
        """引用回复指定消息，返回新消息的 message_id，失败时返回 None"""
        effective_reply_in_thread = self._should_reply_in_thread(parent_id, reply_in_thread)
        request = ReplyMessageRequest.builder() \
            .message_id(parent_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
                .reply_in_thread(effective_reply_in_thread)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.reply(request)
        except Exception as e:
            logger.error("引用回复失败(SDK异常): %s", e)
            return None
        if not response.success():
            logger.error("引用回复失败: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            return response.data.message_id
        except AttributeError:
            return None

    def delete_message(self, message_id: str) -> bool:
        """删除指定消息

        Returns:
            是否成功
        """
        request = DeleteMessageRequest.builder() \
            .message_id(message_id) \
            .build()
        try:
            response = self.client.im.v1.message.delete(request)
        except Exception as e:
            logger.error("删除消息失败(SDK异常): %s", e)
            return False
        if not response.success():
            logger.error("删除消息失败: code=%s, msg=%s", response.code, response.msg)
            return False
        return True

    @staticmethod
    def make_card_response(
        card: Optional[dict] = None,
        toast: Optional[str] = None,
        toast_type: str = "info",
    ) -> P2CardActionTriggerResponse:
        """构造卡片动作的响应（可更新卡片 / 弹 toast）。

        委托给 bot.cards.make_card_response，此处保留以兼容现有子类。
        """
        from bot.cards import make_card_response as _make_card_response

        return _make_card_response(card=card, toast=toast, toast_type=toast_type)

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str,
    ) -> DownloadedMessageResource:
        """下载飞书消息资源，返回内容、文件名和内容类型。"""
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type(resource_type) \
            .build()
        try:
            response = self.client.im.v1.message_resource.get(request)
        except Exception as e:
            raise RuntimeError(f"资源下载失败(SDK异常): {e}") from e
        if not response.success():
            raise RuntimeError(f"资源下载失败: code={response.code}, msg={response.msg}")
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", {}) if raw is not None else {}
        content_type = str(headers.get("Content-Type", "") or "").strip()
        return DownloadedMessageResource(
            content=response.file.read(),
            file_name=str(getattr(response, "file_name", "") or "").strip(),
            content_type=content_type,
        )

    def download_file(self, message_id: str, file_key: str) -> bytes:
        """下载飞书消息中的文件，返回文件二进制内容

        Args:
            message_id: 消息 ID
            file_key: 文件的 file_key

        Returns:
            文件的二进制内容

        Raises:
            RuntimeError: 下载失败时抛出
        """
        return self.download_message_resource(
            message_id,
            file_key,
            resource_type="file",
        ).content

    # ---- 业务逻辑层 (子类实现) ----

    @abstractmethod
    def on_message(self, sender_id: str, chat_id: str, text: str,
                   message_id: str = "") -> None:
        """处理收到的文本消息"""
        ...

    def on_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        """处理卡片按钮点击，子类可覆写"""
        return P2CardActionTriggerResponse()

    def on_attachment_message(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        """处理收到的附件消息，子类可覆写"""
        pass

    def on_bot_menu(self, open_id: str, event_key: str) -> None:
        """处理机器人菜单点击事件，子类可覆写"""
        pass

    def allow_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        """在群消息进入业务处理前做一次轻量 preflight，默认允许。"""
        del sender_id
        del chat_id
        del message_id
        return True

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        """群聊解散或机器人出群后的生命周期回调，子类可覆写。"""
        del chat_id
        del reason

    # ---- 启动 ----

    def start(self) -> None:
        """启动 WebSocket 长连接，开始监听消息"""
        ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("机器人启动中，正在连接飞书...")
        ws_client.start()
