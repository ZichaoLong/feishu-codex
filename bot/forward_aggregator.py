from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

_DEFAULT_FORWARD_AGGREGATE_TIMEOUT = 2.0
_DEFAULT_MERGE_FORWARD_MAX = 50
_DEFAULT_MERGE_FORWARD_MAX_DEPTH = 10


class _Timer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


def _default_timer_factory(
    timeout_seconds: float,
    callback: Callable[..., None],
    args: list[str],
) -> _Timer:
    return threading.Timer(timeout_seconds, callback, args=args)


@dataclass
class PendingForward:
    forwarded_text: str
    message_id: str
    chat_type: str
    sender_user_id: str
    sender_open_id: str
    sender_type: str
    created_at: int
    thread_id: str
    timer: _Timer = field(repr=False)


@dataclass(frozen=True, slots=True)
class ForwardAggregatorPorts:
    get_group_mode: Callable[[str], str]
    append_group_log_entry: Callable[..., int]
    handle_forwarded_text: Callable[[str, str, str, str], None]
    fetch_merge_forward_items: Callable[[str], list[Any]]
    batch_resolve_sender_names: Callable[[set[str]], dict[str, str]]
    extract_text: Callable[[str, dict[str, Any]], str]


class ForwardAggregator:
    def __init__(
        self,
        *,
        ports: ForwardAggregatorPorts,
        group_mode_all: str = "all",
        group_mode_assistant: str = "assistant",
        aggregate_timeout_seconds: float = _DEFAULT_FORWARD_AGGREGATE_TIMEOUT,
        merge_forward_max: int = _DEFAULT_MERGE_FORWARD_MAX,
        merge_forward_max_depth: int = _DEFAULT_MERGE_FORWARD_MAX_DEPTH,
        timer_factory: Callable[[float, Callable[..., None], list[str]], _Timer] = _default_timer_factory,
    ) -> None:
        self._ports = ports
        self._group_mode_all = str(group_mode_all or "").strip() or "all"
        self._group_mode_assistant = str(group_mode_assistant or "").strip() or "assistant"
        self._aggregate_timeout_seconds = float(aggregate_timeout_seconds or 0.0)
        self._merge_forward_max = max(int(merge_forward_max or 0), 1)
        self._merge_forward_max_depth = max(int(merge_forward_max_depth or 0), 1)
        self._timer_factory = timer_factory
        self._pending_forwards: dict[tuple[str, str], PendingForward] = {}
        self._pending_forwards_lock = threading.Lock()

    def peek_pending_forward(self, sender_id: str, chat_id: str) -> PendingForward | None:
        key = (sender_id, chat_id)
        with self._pending_forwards_lock:
            return self._pending_forwards.get(key)

    def forget_chat(self, chat_id: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        with self._pending_forwards_lock:
            stale_forward_keys = [
                key
                for key in self._pending_forwards
                if key[1] == normalized_chat_id
            ]
            for key in stale_forward_keys:
                pending = self._pending_forwards.pop(key, None)
                if pending is not None and pending.timer:
                    pending.timer.cancel()

    def pop_pending_forward(self, sender_id: str, chat_id: str) -> PendingForward | None:
        key = (sender_id, chat_id)
        with self._pending_forwards_lock:
            pending = self._pending_forwards.pop(key, None)
            if pending is not None and pending.timer:
                pending.timer.cancel()
        return pending

    def buffer_forward(
        self,
        sender_id: str,
        chat_id: str,
        forwarded_text: str,
        message_id: str,
        chat_type: str,
        *,
        sender_user_id: str = "",
        sender_open_id: str = "",
        sender_type: str = "user",
        created_at: int = 0,
        thread_id: str = "",
    ) -> None:
        key = (sender_id, chat_id)
        timer = self._timer_factory(
            self._aggregate_timeout_seconds,
            self.on_forward_timeout,
            [sender_id, chat_id],
        )
        with self._pending_forwards_lock:
            old = self._pending_forwards.get(key)
            if old is not None and old.timer:
                old.timer.cancel()
            self._pending_forwards[key] = PendingForward(
                forwarded_text=forwarded_text,
                message_id=message_id,
                chat_type=chat_type,
                sender_user_id=str(sender_user_id or "").strip(),
                sender_open_id=str(sender_open_id or "").strip(),
                sender_type=str(sender_type or "user").strip() or "user",
                created_at=max(int(created_at or 0), 0),
                thread_id=str(thread_id or "").strip(),
                timer=timer,
            )
        timer.start()
        logger.info("转发消息已暂存，等待留言合并: user=%s, chat=%s", sender_id, chat_id)

    def on_forward_timeout(self, sender_id: str, chat_id: str) -> None:
        try:
            pending = self.pop_pending_forward(sender_id, chat_id)
            if pending is None:
                return
            group_mode = self._ports.get_group_mode(chat_id) if pending.chat_type == "group" else ""
            if pending.chat_type == "group" and group_mode == self._group_mode_assistant:
                self._ports.append_group_log_entry(
                    chat_id=chat_id,
                    message_id=pending.message_id,
                    created_at=pending.created_at or int(time.time() * 1000),
                    sender_user_id=pending.sender_user_id,
                    sender_open_id=pending.sender_open_id,
                    sender_type=pending.sender_type,
                    msg_type="merge_forward",
                    thread_id=pending.thread_id,
                    text=f"<forwarded_messages>\n{pending.forwarded_text}\n</forwarded_messages>",
                )
                logger.info(
                    "转发消息聚合超时，已写入助理模式日志: user=%s, chat=%s",
                    sender_id,
                    chat_id,
                )
                return
            if pending.chat_type == "group" and group_mode != self._group_mode_all:
                logger.debug(
                    "转发消息聚合超时，群聊无@唤醒，丢弃: user=%s, chat=%s",
                    sender_id,
                    chat_id,
                )
                return
            text = f"<forwarded_messages>\n{pending.forwarded_text}\n</forwarded_messages>"
            logger.info(
                "转发消息聚合超时，单独处理: user=%s, chat=%s",
                sender_id,
                chat_id,
            )
            self._ports.handle_forwarded_text(
                sender_id,
                chat_id,
                text,
                pending.message_id,
            )
        except Exception as exc:
            logger.error("转发消息超时处理异常: %s", exc, exc_info=True)

    def fetch_merge_forward_text(self, merge_message_id: str) -> str:
        items = list(self._ports.fetch_merge_forward_items(merge_message_id) or [])
        if not items:
            return ""

        children_map: dict[str, list[Any]] = {}
        for item in items[: self._merge_forward_max]:
            sub_id = getattr(item, "message_id", None)
            if sub_id == merge_message_id:
                continue
            parent_id = getattr(item, "upper_message_id", None) or merge_message_id
            children_map.setdefault(parent_id, []).append(item)

        sender_open_ids: set[str] = set()
        for item in items[: self._merge_forward_max]:
            sender = getattr(item, "sender", None)
            if sender and getattr(sender, "sender_type", "") == "user":
                sender_id = getattr(sender, "id", None)
                if sender_id:
                    sender_open_ids.add(sender_id)
        name_map = self._ports.batch_resolve_sender_names(sender_open_ids)

        return self._format_merge_tree(
            merge_message_id,
            children_map,
            name_map,
            depth=0,
        )

    @staticmethod
    def _format_ts(ts_ms: int | str | None) -> str:
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

    def _format_merge_tree(
        self,
        parent_id: str,
        children_map: dict[str, list[Any]],
        name_map: dict[str, str],
        depth: int,
    ) -> str:
        indent = "    " * depth
        if depth >= self._merge_forward_max_depth:
            return f"{indent}[嵌套转发层数过深，已截断]"

        children = children_map.get(parent_id, [])
        parts: list[str] = []
        for item in children:
            try:
                sub_id = getattr(item, "message_id", None)
                sub_type = str(getattr(item, "msg_type", "") or "").strip()

                sender = getattr(item, "sender", None)
                sender_id = str(getattr(sender, "id", "") or "") if sender else ""
                sender_type = str(getattr(sender, "sender_type", "") or "") if sender else ""
                sender_name = name_map.get(sender_id, sender_id[:8])
                if sender_type == "app":
                    sender_name = f"{sender_name}[机器人]"
                ts_str = self._format_ts(getattr(item, "create_time", None))
                header = f"{indent}[{ts_str}] {sender_name}:"
                content_indent = indent + "    "

                if sub_type == "merge_forward":
                    parts.append(f"{header} [forwarded messages]")
                    nested = self._format_merge_tree(
                        str(sub_id or ""),
                        children_map,
                        name_map,
                        depth + 1,
                    )
                    if nested:
                        parts.append(nested)
                    continue

                try:
                    body = getattr(item, "body", None)
                    raw_content = getattr(body, "content", "")
                    content = json.loads(raw_content)
                    text = self._ports.extract_text(sub_type, content)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    text = ""

                if text:
                    indented_lines = "\n".join(
                        f"{content_indent}{line}"
                        for line in text.splitlines()
                    )
                    parts.append(f"{header}\n{indented_lines}")
                    continue

                if sub_type in ("image", "audio", "video", "sticker", "file", "media"):
                    type_labels = {
                        "image": "图片",
                        "audio": "语音",
                        "video": "视频",
                        "sticker": "表情",
                        "file": "文件",
                        "media": "媒体",
                    }
                    parts.append(f"{header} [{type_labels.get(sub_type, sub_type)}]")
                    continue

                parts.append(f"{header} [{sub_type} 消息]")
            except Exception as exc:
                logger.warning(
                    "解析子消息异常: message_id=%s, error=%s",
                    getattr(item, "message_id", "?"),
                    exc,
                )
        return "\n".join(parts)
