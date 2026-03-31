"""
飞书机器人基类
封装了连接、消息收发等通用逻辑，子类只需实现 on_message / on_card_action 处理业务。
"""

import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackCard,
    CallBackToast,
)

logger = logging.getLogger(__name__)

# 消息去重缓存最大容量和过期时间
_DEDUP_MAX_SIZE = 500
_DEDUP_TTL = 300  # 5 分钟

# 飞书卡片限制：单张卡片中 markdown 表格数量上限（实测约 5~10，取保守值）
_MAX_CARD_TABLES = 5

# 合并转发消息：子消息处理上限 & 递归深度上限
_MERGE_FORWARD_MAX = 50
_MERGE_FORWARD_MAX_DEPTH = 10

# 转发消息聚合：等待留言的超时时间（秒）
_FORWARD_AGGREGATE_TIMEOUT = 2.0


@dataclass
class _PendingForward:
    """暂存的合并转发消息，等待后续留言消息合并"""
    forwarded_text: str
    message_id: str
    chat_type: str
    timer: threading.Timer = field(repr=False)


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

    # 唤醒模式关键词和模式常量
    _WAKE_MODE_KEYWORD = "唤醒模式"
    _WAKE_MODE_ALL = "all"
    _WAKE_MODE_MENTION = "mention_only"

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._seen_messages: OrderedDict[str, float] = OrderedDict()
        self._dedup_lock = threading.Lock()
        # 唤醒模式：这些群聊中所有消息都直接处理，无需 @机器人
        self._wake_mode_groups: set[str] = set()
        # 机器人自身的 open_id，用于精确判断群消息是否 @了机器人
        self._bot_open_id: Optional[str] = None
        # 转发消息聚合缓冲区：暂存 merge_forward，等待后续留言合并
        self._pending_forwards: dict[tuple[str, str], _PendingForward] = {}
        self._pending_forwards_lock = threading.Lock()

        self.client = lark.Client.builder() \
            .app_id(app_id) \
            .app_secret(app_secret) \
            .log_level(lark.LogLevel.INFO) \
            .build()

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
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
            # 飞书卡片：GetMessage API 返回降级内容，提取 title 和 text 元素
            title = content_dict.get("title", "")
            texts: list[str] = []
            for para in content_dict.get("elements", []):
                if not isinstance(para, list):
                    continue
                for elem in para:
                    if isinstance(elem, dict) and elem.get("tag") == "text":
                        t = elem.get("text", "").strip()
                        if t:
                            texts.append(t)
            body = " ".join(texts)
            if title and body:
                return f"[卡片: {title}] {body}"
            if title:
                return f"[卡片: {title}]"
            if body:
                return f"[卡片] {body}"
            return ""

        # sticker/image/video/audio 等无文本消息
        return ""

    @staticmethod
    def _strip_mentions(text: str, mentions: list) -> str:
        """从消息文本中剥离所有 @提及 占位符（如 @_user_1）"""
        for mention in mentions:
            key = getattr(mention, "key", "")
            if key:
                text = text.replace(key, "")
        return text.strip()

    # ---- 转发消息聚合 ----

    def _pop_pending_forward(self, sender_id: str, chat_id: str) -> Optional[_PendingForward]:
        """取出并清除指定用户/会话的待合并转发消息，同时取消其超时定时器

        Returns:
            待合并的转发消息，若不存在则返回 None
        """
        key = (sender_id, chat_id)
        with self._pending_forwards_lock:
            pending = self._pending_forwards.pop(key, None)
            if pending and pending.timer:
                pending.timer.cancel()
        return pending

    def _buffer_forward(
        self, sender_id: str, chat_id: str, forwarded_text: str,
        message_id: str, chat_type: str,
    ) -> None:
        """暂存合并转发消息，启动超时定时器等待后续留言

        若同一 (sender_id, chat_id) 已有暂存转发，先取消旧定时器再覆盖。
        """
        key = (sender_id, chat_id)
        timer = threading.Timer(
            _FORWARD_AGGREGATE_TIMEOUT,
            self._on_forward_timeout,
            args=[sender_id, chat_id],
        )
        with self._pending_forwards_lock:
            old = self._pending_forwards.get(key)
            if old and old.timer:
                old.timer.cancel()
            self._pending_forwards[key] = _PendingForward(
                forwarded_text=forwarded_text,
                message_id=message_id,
                chat_type=chat_type,
                timer=timer,
            )
        timer.start()
        logger.info("转发消息已暂存，等待留言合并: user=%s, chat=%s", sender_id, chat_id)

    def _on_forward_timeout(self, sender_id: str, chat_id: str) -> None:
        """超时未收到留言，单独处理暂存的转发消息

        私聊和唤醒模式群聊中，转发消息可独立处理。
        需要 @唤醒的群聊中，因无 @mention 上下文，静默丢弃（与原有行为一致）。
        """
        try:
            pending = self._pop_pending_forward(sender_id, chat_id)
            if not pending:
                return
            # 群聊（非唤醒模式）中无 @mention，丢弃
            if (pending.chat_type == "group"
                    and chat_id not in self._wake_mode_groups):
                logger.debug(
                    "转发消息聚合超时，群聊无@唤醒，丢弃: user=%s, chat=%s",
                    sender_id, chat_id,
                )
                return
            # 私聊或唤醒模式群聊：单独处理转发内容
            text = (f"<forwarded_messages>\n{pending.forwarded_text}"
                    f"\n</forwarded_messages>")
            logger.info(
                "转发消息聚合超时，单独处理: user=%s, chat=%s",
                sender_id, chat_id,
            )
            self.on_message(
                sender_id, chat_id, text, message_id=pending.message_id,
            )
        except Exception as e:
            logger.error("转发消息超时处理异常: %s", e, exc_info=True)

    def _fetch_bot_open_id(self) -> Optional[str]:
        """调用飞书 API 获取机器人自身的 open_id，用于判断群消息是否 @了机器人"""
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

    def _is_bot_mentioned(self, mentions: list) -> bool:
        """判断 mentions 列表中是否包含机器人自身

        首次调用时会通过 API 获取并缓存机器人的 open_id。
        若无法获取 open_id，回退到「有 mention 即视为 @机器人」的宽松策略，
        避免因 API 异常导致群聊功能完全失效。
        """
        if not mentions:
            return False
        # 懒加载机器人 open_id（空字符串表示已尝试但获取失败）
        if self._bot_open_id is None:
            self._bot_open_id = self._fetch_bot_open_id() or ""
        # 无法获取 open_id 时回退：有任何 mention 就视为 @机器人（兼容旧行为）
        if not self._bot_open_id:
            return True
        for mention in mentions:
            mid = getattr(mention, "id", None)
            if mid and getattr(mid, "open_id", None) == self._bot_open_id:
                return True
        return False

    def _resolve_sender_name(self, open_id: str) -> str:
        """通过 open_id 查询用户姓名，失败时返回 open_id 前 8 位作为兜底"""
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest as GetContactUserReq
            request = (GetContactUserReq.builder()
                       .user_id(open_id)
                       .user_id_type("open_id")
                       .build())
            response = self.client.contact.v3.user.get(request)
            if response.success() and response.data and response.data.user:
                name = response.data.user.name or response.data.user.nickname
                if name:
                    return name
        except Exception as e:
            logger.debug("解析用户名失败: open_id=%s, error=%s", open_id, e)
        return open_id[:8]

    def _batch_resolve_sender_names(self, open_ids: set[str]) -> dict[str, str]:
        """批量解析 open_id → 用户姓名，返回映射表"""
        name_map: dict[str, str] = {}
        for oid in open_ids:
            name_map[oid] = self._resolve_sender_name(oid)
        return name_map

    @staticmethod
    def _format_ts(ts_ms: str | None) -> str:
        """将毫秒时间戳转为可读时间字符串"""
        if not ts_ms:
            return "未知时间"
        try:
            from datetime import datetime, timezone, timedelta
            dt = datetime.fromtimestamp(
                int(ts_ms) / 1000,
                tz=timezone(timedelta(hours=8)),
            )
            return dt.strftime("%m-%d %H:%M:%S")
        except (ValueError, OSError):
            return "未知时间"

    def _fetch_merge_forward_text(self, merge_message_id: str) -> str:
        """通过 GetMessage API 获取合并转发中的子消息，还原对话格式返回

        飞书 API 将所有层级的子消息作为扁平列表一次性返回，
        通过 upper_message_id 字段区分父子关系。
        本方法只调用一次 API，用 upper_message_id 构建消息树后递归格式化。
        顶层调用会在外部包裹 <forwarded_messages> 标签。

        输出格式示例:
            [03-17 09:15:55] 张三:
                你好，这是第一条消息
            [03-17 09:16:37] 李四: [forwarded messages]
                [03-17 08:00:00] 王五:
                    嵌套的消息
        """
        try:
            request = GetMessageRequest.builder().message_id(merge_message_id).build()
            response = self.client.im.v1.message.get(request)
            if not response.success():
                logger.warning(
                    "获取合并转发消息失败: message_id=%s, code=%s, msg=%s",
                    merge_message_id, response.code, response.msg,
                )
                return ""
            items = response.data.items or []
        except Exception as e:
            logger.warning("获取合并转发消息异常: message_id=%s, error=%s",
                          merge_message_id, e)
            return ""

        # 用 upper_message_id 构建父子关系树
        children_map: dict[str, list] = {}
        for item in items[:_MERGE_FORWARD_MAX]:
            sub_id = getattr(item, "message_id", None)
            if sub_id == merge_message_id:
                continue  # 跳过合并转发消息本身
            parent_id = getattr(item, "upper_message_id", None) or merge_message_id
            children_map.setdefault(parent_id, []).append(item)

        # 批量解析用户姓名（只对 sender_type=user 调 Contact API）
        user_ids: set[str] = set()
        for item in items[:_MERGE_FORWARD_MAX]:
            sender = getattr(item, "sender", None)
            if sender and getattr(sender, "sender_type", "") == "user":
                sid = getattr(sender, "id", None)
                if sid:
                    user_ids.add(sid)
        name_map = self._batch_resolve_sender_names(user_ids)

        return self._format_merge_tree(
            merge_message_id, children_map, name_map, depth=0,
        )

    def _format_merge_tree(
        self,
        parent_id: str,
        children_map: dict[str, list],
        name_map: dict[str, str],
        depth: int,
    ) -> str:
        """递归格式化消息树的某一层子消息"""
        indent = "    " * depth
        if depth >= _MERGE_FORWARD_MAX_DEPTH:
            return f"{indent}[嵌套转发层数过深，已截断]"

        children = children_map.get(parent_id, [])
        parts: list[str] = []
        for item in children:
            try:
                sub_id = getattr(item, "message_id", None)
                sub_type = item.msg_type

                # 提取发送者和时间
                sender = getattr(item, "sender", None)
                sender_id = getattr(sender, "id", "") if sender else ""
                sender_type = getattr(sender, "sender_type", "") if sender else ""
                sender_name = name_map.get(sender_id, sender_id[:8])
                if sender_type == "app":
                    sender_name = f"{sender_name}[机器人]"
                ts_str = self._format_ts(getattr(item, "create_time", None))
                header = f"{indent}[{ts_str}] {sender_name}:"
                content_indent = indent + "    "

                if sub_type == "merge_forward":
                    # 嵌套合并转发：标记后递归格式化子节点
                    parts.append(f"{header} [forwarded messages]")
                    nested = self._format_merge_tree(
                        sub_id, children_map, name_map, depth + 1,
                    )
                    if nested:
                        parts.append(nested)
                else:
                    # 提取文本内容（text/post/interactive 等）
                    try:
                        sub_content = json.loads(item.body.content)
                        text = self._extract_text(sub_type, sub_content)
                    except (json.JSONDecodeError, AttributeError):
                        text = ""

                    if text:
                        # 多行消息：每行缩进到发送者下方
                        indented_lines = "\n".join(
                            f"{content_indent}{line}"
                            for line in text.splitlines()
                        )
                        parts.append(f"{header}\n{indented_lines}")
                    elif sub_type in ("image", "audio", "video", "sticker",
                                      "file", "media"):
                        # 不支持下载的媒体类型：占位提示
                        type_labels = {
                            "image": "图片", "audio": "语音",
                            "video": "视频", "sticker": "表情",
                            "file": "文件", "media": "媒体",
                        }
                        label = type_labels.get(sub_type, sub_type)
                        parts.append(f"{header} [{label}]")
                    else:
                        # 其他未知类型：占位提示
                        parts.append(f"{header} [{sub_type} 消息]")
            except Exception as e:
                logger.warning("解析子消息异常: message_id=%s, error=%s",
                              getattr(item, "message_id", "?"), e)
                continue
        return "\n".join(parts)

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """解析原始消息，根据消息类型分发到对应处理方法

        群聊消息仅处理 @机器人 的消息，非 @消息直接忽略。
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
        超时则单独处理（私聊/唤醒模式群）或丢弃（需@唤醒的群聊）。
        """
        message = data.event.message
        sender_id = data.event.sender.sender_id.user_id
        chat_id = message.chat_id
        message_id = message.message_id
        msg_type = message.message_type
        chat_type = getattr(message, "chat_type", None) or "p2p"
        mentions = getattr(message, "mentions", None) or []

        # 消息去重，防止飞书重试导致重复处理
        if self._is_duplicate(message_id):
            logger.info("跳过重复消息: message_id=%s", message_id)
            return

        # 精确判断是否 @了机器人（而非 @其他用户）
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
                if chat_type != "group" or chat_id in self._wake_mode_groups:
                    self.reply(chat_id, "合并转发的消息中未包含可识别的文本内容。")
                return
            logger.info("合并转发提取完成，暂存等待留言: user=%s, message_id=%s, text=%s",
                        sender_id, message_id, text[:200])
            self._buffer_forward(sender_id, chat_id, text, message_id, chat_type)
            return

        # ---- 群聊非 @机器人消息过滤（merge_forward 已在上方绕过） ----
        if chat_type == "group" and not bot_mentioned:
            if chat_id not in self._wake_mode_groups:
                logger.debug("忽略群聊非@机器人消息: chat=%s, user=%s", chat_id, sender_id)
                return

        # ---- 检查是否有待合并的转发消息 ----
        pending = self._pop_pending_forward(sender_id, chat_id)

        try:
            content_dict = json.loads(message.content)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                "消息内容解析失败: message_id=%s, msg_type=%s, error=%s, raw_content=%r",
                message_id, msg_type, type(e).__name__, message.content,
            )
            return

        logger.info(
            "收到原始消息: user=%s, chat_type=%s, msg_type=%s, message_id=%s, content=%s",
            sender_id, chat_type, msg_type, message_id, message.content,
        )

        if msg_type == "file":
            file_key = content_dict.get("file_key", "")
            file_name = content_dict.get("file_name", "")
            logger.info("收到文件: user=%s, chat_type=%s, message_id=%s, file=%s",
                       sender_id, chat_type, message_id, file_name)
            self.on_file_message(sender_id, chat_id, message_id, file_key, file_name)
        else:
            text = self._extract_text(msg_type, content_dict)
            # 群聊消息剥离 @提及 占位符
            if chat_type == "group" and bot_mentioned:
                text = self._strip_mentions(text, mentions)
            if not text:
                if chat_type == "group" and bot_mentioned:
                    # 群聊中纯 @机器人无附加文本，视为无指令，触发默认菜单
                    self.on_message(sender_id, chat_id, "", message_id=message_id)
                else:
                    logger.info("忽略空文本消息: user=%s, msg_type=%s, message_id=%s",
                               sender_id, msg_type, message_id)
                    self.reply(chat_id, "当前仅支持文本消息，请直接输入文字。")
                return
            logger.info("收到消息: user=%s, chat_type=%s, message_id=%s, text=%s",
                       sender_id, chat_type, message_id, text)
            # 唤醒模式关键词拦截（仅群聊生效）
            if text == self._WAKE_MODE_KEYWORD and chat_type == "group":
                self._send_wake_mode_card(chat_id)
                return
            # 合并待处理的转发消息（转发内容在前，留言在后）
            if pending:
                text = (f"<forwarded_messages>\n{pending.forwarded_text}"
                        f"\n</forwarded_messages>\n\n{text}")
                logger.info(
                    "转发消息与留言已合并: user=%s, chat=%s, forward_msg=%s",
                    sender_id, chat_id, pending.message_id,
                )
            self.on_message(sender_id, chat_id, text, message_id=message_id)

    def _on_raw_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """解析卡片按钮点击事件，交给子类处理"""
        try:
            user_id = data.event.operator.user_id
            chat_id = data.event.context.open_chat_id
            message_id = data.event.context.open_message_id
            action_value = data.event.action.value or {}
            # 表单提交时携带输入框的值，注入 action_value 供处理器读取
            if data.event.action.form_value:
                action_value["_form_value"] = data.event.action.form_value
            logger.info("卡片点击: user=%s, action=%s", user_id, action_value)
            # 唤醒模式卡片回调在基类拦截，不交给子类
            if action_value.get("action") == "set_wake_mode":
                return self._handle_set_wake_mode(chat_id, action_value)
            return self.on_card_action(user_id, chat_id, message_id, action_value)
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
            self.on_bot_menu(user_id, open_id, event_key)
        except Exception as e:
            logger.error("处理菜单事件异常: %s", e, exc_info=True)

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
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as e:
            logger.error("发送消息失败(SDK异常): %s", e)
            return
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)

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
        try:
            response = self.client.im.v1.message.create(request)
        except Exception as e:
            logger.error("发送消息失败(SDK异常): %s", e)
            return None
        if not response.success():
            logger.error("发送失败: code=%s, msg=%s", response.code, response.msg)
            return None
        try:
            return response.data.message_id
        except AttributeError:
            return None

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

    def urgent_message(self, message_id: str, user_ids: list[str]) -> bool:
        """对已有消息发送应用内加急通知

        Args:
            message_id: 要加急的消息 ID
            user_ids: 接收加急通知的用户 user_id 列表

        Returns:
            是否成功
        """
        request = UrgentAppMessageRequest.builder() \
            .message_id(message_id) \
            .user_id_type("user_id") \
            .request_body(UrgentReceivers.builder()
                .user_id_list(user_ids)
                .build()) \
            .build()
        try:
            response = self.client.im.v1.message.urgent_app(request)
        except Exception as e:
            logger.error("加急通知失败(SDK异常): %s", e)
            return False
        if not response.success():
            logger.error("加急通知失败: code=%s, msg=%s", response.code, response.msg)
            return False
        return True

    def reply(self, chat_id: str, text: str) -> None:
        """发送文本消息"""
        self.send_message(chat_id, "text", json.dumps({"text": text}))

    def reply_card(self, chat_id: str, card: dict) -> None:
        """发送交互卡片消息"""
        self.send_message(chat_id, "interactive", json.dumps(card))

    def reply_to_message(
        self, parent_id: str, msg_type: str, content: str,
    ) -> Optional[str]:
        """引用回复指定消息，返回新消息的 message_id，失败时返回 None"""
        request = ReplyMessageRequest.builder() \
            .message_id(parent_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type(msg_type)
                .content(content)
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
        """构造卡片动作的响应（可更新卡片 / 弹 toast）"""
        resp = P2CardActionTriggerResponse()
        if toast:
            resp.toast = CallBackToast()
            resp.toast.type = toast_type
            resp.toast.content = toast
        if card:
            resp.card = CallBackCard()
            resp.card.type = "raw"
            resp.card.data = card
        return resp

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
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(file_key) \
            .type("file") \
            .build()
        try:
            response = self.client.im.v1.message_resource.get(request)
        except Exception as e:
            raise RuntimeError(f"文件下载失败(SDK异常): {e}") from e
        if not response.success():
            raise RuntimeError(f"文件下载失败: code={response.code}, msg={response.msg}")
        return response.file.read()

    # ---- 唤醒模式 ----

    def _build_wake_mode_card(self, chat_id: str) -> dict:
        """构造唤醒模式选择卡片，高亮当前模式"""
        is_all = chat_id in self._wake_mode_groups
        current = self._WAKE_MODE_ALL if is_all else self._WAKE_MODE_MENTION

        modes = [
            (self._WAKE_MODE_ALL, "全部唤醒",
             "群内所有消息都直接发给机器人，无需 @"),
            (self._WAKE_MODE_MENTION, "仅@唤醒",
             "只有 @机器人 的消息才会触发响应"),
        ]

        elements: list[dict] = []
        for mode, label, desc in modes:
            elements.append({"tag": "markdown", "content": f"**{label}**\n{desc}"})

        buttons = []
        for mode, label, _ in modes:
            is_current = mode == current
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text",
                         "content": f"{'✓ ' if is_current else ''}{label}"},
                "type": "primary" if is_current else "default",
                "value": {"action": "set_wake_mode", "mode": mode},
            })
        elements.append({"tag": "action", "actions": buttons})

        current_label = "全部唤醒" if is_all else "仅@唤醒"
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "唤醒模式"},
                "template": "blue",
            },
            "elements": [
                {"tag": "markdown",
                 "content": f"当前模式：**{current_label}**"},
                {"tag": "hr"},
                *elements,
            ],
        }

    def _send_wake_mode_card(self, chat_id: str) -> None:
        """发送唤醒模式选择卡片"""
        card = self._build_wake_mode_card(chat_id)
        self.reply_card(chat_id, card)

    def _handle_set_wake_mode(
        self, chat_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        """处理唤醒模式卡片按钮点击"""
        mode = action_value.get("mode", self._WAKE_MODE_MENTION)
        if mode == self._WAKE_MODE_ALL:
            self._wake_mode_groups.add(chat_id)
            toast = "已切换为「全部唤醒」模式，群内消息无需 @ 即可触发"
        else:
            self._wake_mode_groups.discard(chat_id)
            toast = "已切换为「仅@唤醒」模式"
        logger.info("唤醒模式切换: chat=%s, mode=%s", chat_id, mode)
        updated_card = self._build_wake_mode_card(chat_id)
        return self.make_card_response(card=updated_card, toast=toast)

    # ---- 业务逻辑层 (子类实现) ----

    @abstractmethod
    def on_message(self, sender_id: str, chat_id: str, text: str,
                   message_id: str = "") -> None:
        """处理收到的文本消息"""
        ...

    def on_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        """处理卡片按钮点击，子类可覆写"""
        return P2CardActionTriggerResponse()

    def on_file_message(
        self, sender_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """处理收到的文件消息，子类可覆写"""
        pass

    def on_bot_menu(self, user_id: str, open_id: str, event_key: str) -> None:
        """处理机器人菜单点击事件，子类可覆写"""
        pass

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
