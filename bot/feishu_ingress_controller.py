"""Surface-neutral Feishu message ingress orchestration."""

from __future__ import annotations

import json
import logging
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.feishu_message_codec import FeishuMessageCodec
from bot.feishu_outbound import FeishuOutboundResult
from bot.feishu_process_cache import FeishuProcessCache
from bot.feishu_types import (
    BotIdentitySnapshot,
    GroupActivationSnapshot,
    GroupMessageEntry,
    MentionMember,
)
from bot.forward_aggregator import (
    ForwardAggregator,
    ForwardAggregatorPorts,
    PendingForward,
)
from bot.group_history_recovery import (
    GroupHistoryRecovery,
    GroupHistoryRecoveryPorts,
    ListedMessagesPage,
)
from bot.stores.group_chat_store import GroupChatStore

logger = logging.getLogger(__name__)

_DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES = {"image", "file", "audio", "media"}
_UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES = {"folder", "sticker"}
_ATTACHMENT_MESSAGE_TYPES = (
    _DOWNLOADABLE_ATTACHMENT_MESSAGE_TYPES | _UNSUPPORTED_ATTACHMENT_MESSAGE_TYPES
)
_NON_ADMIN_P2P_BOOTSTRAP_COMMANDS = frozenset({"/whoami", "/bot-status", "/init"})


@dataclass(frozen=True, slots=True)
class FeishuInboundMessage:
    sender_type: str
    sender_user_id: str
    sender_open_id: str
    chat_id: str
    message_id: str
    message_type: str
    chat_type: str
    content: str
    mentions: tuple[Any, ...] = ()
    create_time: int | str | None = None
    thread_id: str = ""
    root_id: str = ""
    parent_id: str = ""


@dataclass(frozen=True, slots=True)
class FeishuIngressPorts:
    handle_message: Callable[[str, str, str, str], None]
    handle_attachment: Callable[[str, str, str, str, str, str], None]
    allow_group_prompt: Callable[[str, str, str], bool]
    should_route_group_followup_prompt: Callable[[str, str, str], bool]
    reply_text: Callable[..., Any]
    send_message: Callable[[str, str, str], FeishuOutboundResult]
    reply_to_message: Callable[..., FeishuOutboundResult]
    patch_message: Callable[[str, str, str], FeishuOutboundResult]
    list_history_messages_page: Callable[..., ListedMessagesPage]
    fetch_merge_forward_items: Callable[[str], list[Any]]
    display_name_for_sender_identity: Callable[..., str]
    log_card_ingress_event: Callable[[str, dict[str, Any]], None]


class FeishuIngressController:
    """Own one message's policy, history, queue, and dispatch sequence."""

    GROUP_MODE_ALL = "all"
    GROUP_MODE_MENTION = "mention_only"
    GROUP_MODE_ASSISTANT = "assistant"

    def __init__(
        self,
        *,
        ports: FeishuIngressPorts,
        process_cache: FeishuProcessCache,
        message_codec: FeishuMessageCodec,
        group_store: GroupChatStore,
        app_id: str,
        admin_open_ids: set[str],
        configured_bot_open_id: str,
        configured_trigger_open_ids: set[str],
        history_fetch_limit: int,
        history_fetch_lookback_seconds: int,
    ) -> None:
        self._ports = ports
        self._process_cache = process_cache
        self._message_codec = message_codec
        self._group_store = group_store
        self._app_id = str(app_id or "").strip()
        self._admin_open_ids = set(admin_open_ids)
        self._configured_bot_open_id = str(configured_bot_open_id or "").strip()
        self._configured_trigger_open_ids = set(configured_trigger_open_ids)
        self._bot_open_id_error_logged = False
        self._history_recovery = GroupHistoryRecovery(
            ports=GroupHistoryRecoveryPorts(
                list_messages=self._ports.list_history_messages_page,
                render_message_text=self._message_codec.render_message_text,
                normalize_mentions=self._message_codec.normalize_mentions,
                mention_payloads=self._message_codec.mention_payloads,
                display_name_for_sender_identity=(
                    self._ports.display_name_for_sender_identity
                ),
                read_local_messages_between=(self._read_group_history_local_messages),
                get_last_boundary_seq=self._get_group_history_boundary_seq,
                get_last_boundary_created_at=(
                    self._get_group_history_boundary_created_at
                ),
                get_last_boundary_message_ids=(
                    self._get_group_history_boundary_message_ids
                ),
            ),
            app_id=self._app_id,
            history_fetch_limit=history_fetch_limit,
            history_fetch_lookback_seconds=history_fetch_lookback_seconds,
        )
        self._forward_aggregator = ForwardAggregator(
            ports=ForwardAggregatorPorts(
                get_group_mode=self.get_group_mode,
                append_group_log_entry=self._append_group_log_entry,
                handle_forwarded_text=self._ports.handle_message,
                fetch_merge_forward_items=self._ports.fetch_merge_forward_items,
                batch_resolve_sender_names=self._batch_resolve_sender_names,
                render_message_text=(
                    lambda msg_type, content, message_id: (
                        self._message_codec.render_message_text(
                            msg_type,
                            content,
                            message_id=message_id,
                        )
                    )
                ),
            ),
            group_mode_all=self.GROUP_MODE_ALL,
            group_mode_assistant=self.GROUP_MODE_ASSISTANT,
        )

    @property
    def group_store(self) -> GroupChatStore:
        return self._group_store

    @property
    def history_recovery(self) -> GroupHistoryRecovery:
        return self._history_recovery

    @property
    def forward_aggregator(self) -> ForwardAggregator:
        return self._forward_aggregator

    def get_group_mode(self, chat_id: str) -> str:
        return self._group_store.get_group_mode(chat_id)

    def set_group_mode(self, chat_id: str, mode: str) -> str:
        return self._group_store.set_group_mode(chat_id, mode)

    def get_group_activation_snapshot(
        self,
        chat_id: str,
    ) -> GroupActivationSnapshot:
        snapshot = self._group_store.activation_snapshot(chat_id)
        return {
            "activated": bool(snapshot["activated"]),
            "activated_by": str(snapshot["activated_by"] or ""),
            "activated_at": int(snapshot["activated_at"]),
        }

    def activate_group_chat(
        self,
        chat_id: str,
        *,
        activated_by: str,
    ) -> GroupActivationSnapshot:
        snapshot = self._group_store.activate_chat(
            chat_id,
            activated_by=activated_by,
        )
        return {
            "activated": bool(snapshot["activated"]),
            "activated_by": str(snapshot["activated_by"] or ""),
            "activated_at": int(snapshot["activated_at"]),
        }

    def deactivate_group_chat(self, chat_id: str) -> GroupActivationSnapshot:
        snapshot = self._group_store.deactivate_chat(chat_id)
        return {
            "activated": bool(snapshot["activated"]),
            "activated_by": str(snapshot["activated_by"] or ""),
            "activated_at": int(snapshot["activated_at"]),
        }

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

    def configured_group_trigger_open_ids(self) -> set[str]:
        if not self._configured_bot_open_id:
            return set()
        return {
            self._configured_bot_open_id,
            *self._configured_trigger_open_ids,
        }

    def is_group_admin(self, *, open_id: str = "") -> bool:
        return self.is_admin(open_id=open_id)

    def is_group_user_allowed(self, chat_id: str, *, open_id: str = "") -> bool:
        if self.is_admin(open_id=open_id):
            return True
        return self._group_store.is_group_activated(chat_id)

    def identity_snapshot(self, discovered_open_id: str) -> BotIdentitySnapshot:
        return {
            "app_id": self._app_id,
            "configured_open_id": self._configured_bot_open_id,
            "discovered_open_id": str(discovered_open_id or "").strip(),
            "trigger_open_ids": sorted(self._configured_trigger_open_ids),
        }

    def extract_non_bot_mentions(self, message_id: str) -> list[MentionMember]:
        context = self._process_cache.get_message_context(message_id)
        mentions = context.get("mentions") or []
        if not isinstance(mentions, list):
            return []
        trigger_open_ids = self.configured_group_trigger_open_ids()
        members: list[MentionMember] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            open_id = str(mention.get("open_id", "")).strip()
            if not open_id or open_id in trigger_open_ids:
                continue
            members.append(
                {
                    "open_id": open_id,
                    "name": str(mention.get("name", "")).strip(),
                }
            )
        return members

    def forget_chat_state_after_destination_loss(self, chat_id: str) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return
        self._group_store.clear_chat(normalized_chat_id)
        self._process_cache.forget_chat(normalized_chat_id)
        self._forward_aggregator.forget_chat(normalized_chat_id)

    def _display_name_for_sender_identity(
        self,
        *,
        user_id: str = "",
        sender_principal_id: str = "",
        sender_type: str = "user",
    ) -> str:
        return self._ports.display_name_for_sender_identity(
            user_id=user_id,
            sender_principal_id=sender_principal_id,
            sender_type=sender_type,
        )

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

    def _batch_resolve_sender_names(
        self,
        open_ids: set[str],
    ) -> dict[str, str]:
        return {
            open_id: self._display_name_for_sender_identity(
                sender_principal_id=open_id,
                sender_type="user",
            )
            for open_id in open_ids
        }

    def _is_bot_mentioned(self, mentions: tuple[Any, ...]) -> bool:
        if not mentions:
            return False
        trigger_open_ids = self.configured_group_trigger_open_ids()
        if not trigger_open_ids:
            if not self._bot_open_id_error_logged:
                logger.error(
                    "未配置 `system.yaml.bot_open_id`，群聊显式 mention 触发已严格失败。"
                    "如需自动写入，可私聊机器人执行 `/init <token>`；"
                    "如需人工诊断，可先执行 `/bot-status`。"
                )
                self._bot_open_id_error_logged = True
            return False
        return any(
            self._message_codec.mention_payload(mention)["open_id"] in trigger_open_ids
            for mention in mentions
        )

    def _pop_pending_forward(
        self,
        sender_id: str,
        chat_id: str,
        *,
        thread_id: str = "",
        root_id: str = "",
    ) -> PendingForward | None:
        return self._forward_aggregator.pop_pending_forward(
            sender_id,
            chat_id,
            thread_id=thread_id,
            root_id=root_id,
        )

    def _buffer_forward(
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
        root_id: str = "",
    ) -> None:
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
            root_id=root_id,
        )

    def _fetch_merge_forward_text(self, merge_message_id: str) -> str:
        return self._forward_aggregator.fetch_merge_forward_text(merge_message_id)

    def _log_card_ingress_event(self, event: str, **fields: Any) -> None:
        self._ports.log_card_ingress_event(event, fields)

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

    def _get_group_history_boundary_created_at(
        self, chat_id: str, *, scope: str
    ) -> int:
        return self._group_store.get_last_boundary_created_at(chat_id, scope=scope)

    def _get_group_history_boundary_message_ids(
        self, chat_id: str, *, scope: str
    ) -> list[str]:
        return self._group_store.get_last_boundary_message_ids(chat_id, scope=scope)

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

    def _prepare_group_history_execution_card(
        self, chat_id: str, parent_message_id: str
    ) -> None:
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
        reply_result = self._ports.reply_to_message(
            chat_id,
            normalized_parent_id,
            "interactive",
            content,
        )
        card_message_id = reply_result.message_id if reply_result.ok else ""
        if not card_message_id and reply_result.safe_to_fallback:
            send_result = self._ports.send_message(
                chat_id,
                "interactive",
                content,
            )
            card_message_id = send_result.message_id if send_result.ok else ""
        if card_message_id:
            self._process_cache.reserve_execution_card(
                normalized_parent_id,
                card_message_id,
            )

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
                "title": {
                    "tag": "plain_text",
                    "content": "Codex（群聊上下文准备失败）",
                },
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
                            "- 如需先继续使用群聊，可临时显式 mention 触发对象后执行 `/group-mode mention-only`"
                        ),
                    }
                ]
            },
        }
        content = json.dumps(card, ensure_ascii=False)
        reserved_id = self._process_cache.claim_reserved_execution_card(
            parent_message_id
        )
        if reserved_id:
            patch_result = self._ports.patch_message(
                chat_id,
                reserved_id,
                content,
            )
            if patch_result.ok:
                return
            if not patch_result.safe_to_fallback:
                return
        if parent_message_id:
            reply_result = self._ports.reply_to_message(
                chat_id,
                parent_message_id,
                "interactive",
                content,
            )
            if reply_result.ok:
                return
            if not reply_result.safe_to_fallback:
                return
        self._ports.send_message(chat_id, "interactive", content)

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

    def _build_group_current_turn_text(
        self, current_text: str, *, sender_name: str
    ) -> str:
        return self._history_recovery.build_group_current_turn_text(
            current_text,
            sender_name=sender_name,
        )

    def _build_group_turn_text(self, current_text: str, *, sender_name: str) -> str:
        return self._history_recovery.build_group_turn_text(
            current_text,
            sender_name=sender_name,
        )

    def prepare_queued_prompt_text(
        self,
        *,
        chat_id: str,
        message_id: str,
        text: str,
        assistant_context_mode: str = "",
        assistant_context_created_at: int = 0,
        assistant_context_seq: int = 0,
        assistant_context_sender_name: str = "",
        origin_feishu_thread_id: str = "",
    ) -> str | None:
        if str(assistant_context_mode or "").strip() != "deferred_recovery":
            return str(text or "")
        current_seq = max(int(assistant_context_seq or 0), 0)
        current_created_at = max(int(assistant_context_created_at or 0), 0)
        thread_id = str(origin_feishu_thread_id or "").strip()
        sender_name = str(assistant_context_sender_name or "").strip()
        if self._history_recovery_enabled():
            self._prepare_group_history_execution_card(chat_id, message_id)
        try:
            context_entries = self._collect_assistant_context_entries(
                chat_id=chat_id,
                current_message_id=message_id,
                current_create_time=current_created_at,
                current_seq=current_seq,
                thread_id=thread_id,
            )
        except Exception as exc:
            logger.warning("queued 群历史回捞失败: chat=%s, error=%s", chat_id, exc)
            self._notify_group_history_fetch_failed(
                chat_id=chat_id,
                parent_message_id=message_id,
                error=exc,
            )
            return None
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
                current_created_at=current_created_at,
                context_entries=context_entries,
            )
            self._group_store.set_last_boundary(
                chat_id,
                seq=current_seq,
                created_at=current_created_at,
                message_ids=boundary_message_ids,
                scope=self._group_scope_key(thread_id),
            )
        return assistant_text

    @staticmethod
    def _group_activation_denied_text(group_mode: str) -> str:
        normalized_mode = str(group_mode or "").strip().lower()
        if normalized_mode == "all":
            trigger_rule = "当前群工作态是 `all`：已授权成员可直接发消息触发。"
        else:
            trigger_rule = (
                "当前群工作态是 `assistant` / `mention-only`："
                "群成员仍需先显式 mention 触发对象。"
            )
        return (
            "当前群聊尚未由管理员初始化，暂时不能使用机器人。\n"
            f"{trigger_rule}\n"
            "请让管理员在群里执行 `/group activate`。"
        )

    @staticmethod
    def _p2p_owner_only_denied_text() -> str:
        return (
            "当前机器人仅支持管理员私聊使用。\n"
            "如需协作使用，请让管理员把机器人拉进群，并先在群里执行 `/group activate`。"
        )

    @staticmethod
    def _is_allowed_non_admin_p2p_bootstrap_text(text: str) -> bool:
        command, _, _ = str(text or "").strip().partition(" ")
        return command.lower() in _NON_ADMIN_P2P_BOOTSTRAP_COMMANDS

    def handle_message(self, message: FeishuInboundMessage) -> None:
        """Handle one SDK-neutral inbound message envelope.

        合并转发消息聚合策略:
        飞书将用户的"转发+留言"拆为两条独立事件（先 merge_forward，后 text）。
        为将它们作为一条指令处理，merge_forward 到达时先暂存到缓冲区，
        等待短时间窗口内同一用户同一会话的后续消息。若后续消息到达则合并处理，
        超时则按当前会话类型处理：私聊直接转发，`assistant` 群聊写入日志，
        `all` 群聊直接转发，`mention_only` 群聊丢弃。暂存与消费严格按
        sender、chat 与 Feishu thread/root scope 配对，不跨主流或其他 topic 合并。
        """
        sender_type = str(message.sender_type or "user").strip() or "user"
        sender_user_id = str(message.sender_user_id or "").strip()
        sender_open_id = str(message.sender_open_id or "").strip()
        sender_id = str(sender_open_id or "").strip()
        chat_id = message.chat_id
        message_id = message.message_id
        msg_type = message.message_type
        chat_type = str(message.chat_type or "p2p").strip() or "p2p"
        thread_id = str(message.thread_id or "").strip()
        root_id = str(message.root_id or "").strip()
        parent_id = str(message.parent_id or "").strip()
        mentions = message.mentions
        group_mode = self.get_group_mode(chat_id) if chat_type == "group" else ""
        control_text = False
        self._process_cache.remember_chat_type(chat_id, chat_type)

        # 消息去重，防止飞书重试导致重复处理
        if self._process_cache.is_duplicate_message(message_id):
            logger.info("跳过重复消息: message_id=%s", message_id)
            return

        # 精确判断是否命中了有效触发 mention（机器人自身或配置的 alias）
        bot_mentioned = self._is_bot_mentioned(mentions)

        # ---- 合并转发消息：暂存到缓冲区，等待后续留言 ----
        # 合并转发的 content 不是 JSON（是固定字符串 "Merged and Forwarded Message"），
        # 需要在 JSON 解析之前单独处理。
        # 注意：merge_forward 在群聊中不携带 @mention，所以要绕过群聊过滤先暂存。
        if msg_type == "merge_forward":
            self._log_card_ingress_event(
                "event",
                message_id=message_id,
                msg_type=msg_type,
                chat_id=chat_id,
                thread_id=thread_id,
                parent_id=parent_id,
                root_id=root_id,
                raw_content="Merged and Forwarded Message",
            )
            logger.info(
                "收到合并转发: user=%s, chat_type=%s, message_id=%s",
                sender_id,
                chat_type,
                message_id,
            )
            text = self._fetch_merge_forward_text(message_id)
            if not text:
                logger.warning("合并转发消息提取文本为空: message_id=%s", message_id)
                # 仅在非群聊或有权响应时回复提示
                if chat_type != "group" or group_mode == self.GROUP_MODE_ALL:
                    self._ports.reply_text(
                        chat_id,
                        "合并转发的消息中未包含可识别的文本内容。",
                    )
                return
            logger.info(
                "合并转发提取完成，暂存等待留言: user=%s, message_id=%s, text=%s",
                sender_id,
                message_id,
                text[:200],
            )
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
                root_id=root_id,
            )
            return

        try:
            content_dict = json.loads(message.content)
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(
                "消息内容解析失败: message_id=%s, msg_type=%s, error=%s, raw_content=%r",
                message_id,
                msg_type,
                type(e).__name__,
                message.content,
            )
            return

        self._log_card_ingress_event(
            "event",
            message_id=message_id,
            msg_type=msg_type,
            chat_id=chat_id,
            thread_id=thread_id,
            parent_id=parent_id,
            root_id=root_id,
            raw_content=str(message.content or "")[:4000],
        )

        sender_name, sender_open_log, sender_user_log = self._sender_log_fields(
            user_id=sender_user_id,
            sender_principal_id=sender_open_id,
            sender_type=sender_type,
        )
        logger.info(
            "收到原始消息: name=%s, open_id=%s, user_id=%s, chat_type=%s, msg_type=%s, message_id=%s, content=%s",
            sender_name,
            sender_open_log,
            sender_user_log,
            chat_type,
            msg_type,
            message_id,
            message.content,
        )

        is_attachment_message = msg_type in _ATTACHMENT_MESSAGE_TYPES
        text = ""
        if is_attachment_message:
            attachment_name = self._message_codec.attachment_message_name(
                msg_type,
                content_dict,
            )
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
            text = self._message_codec.render_message_text(
                msg_type,
                content_dict,
                message_id=message_id,
            )
        if chat_type == "group" and mentions:
            text = self._message_codec.normalize_mentions(text, mentions)
        pending = (
            None
            if is_attachment_message
            else self._pop_pending_forward(
                sender_id,
                chat_id,
                thread_id=thread_id,
                root_id=root_id,
            )
        )
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

        self._process_cache.remember_message_context(
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
                "mentions": self._message_codec.mention_payloads(mentions),
                "created_at": int(message.create_time or 0),
                "sender_name": sender_name,
            },
        )

        if chat_type == "group" and sender_type == "app":
            logger.debug(
                "忽略群聊机器人消息事件: chat=%s, message_id=%s", chat_id, message_id
            )
            return

        if chat_type != "group" and not self.is_admin(open_id=sender_open_id):
            if not self._is_allowed_non_admin_p2p_bootstrap_text(text):
                self._ports.reply_text(
                    chat_id,
                    self._p2p_owner_only_denied_text(),
                    parent_message_id=message_id,
                )
                return

        if chat_type == "group":
            control_text = self._is_group_control_text(text)
            allowed_to_use = self.is_group_user_allowed(chat_id, open_id=sender_open_id)
            if group_mode == self.GROUP_MODE_ASSISTANT:
                if is_attachment_message:
                    if not allowed_to_use:
                        return
                else:
                    if not allowed_to_use:
                        if bot_mentioned or control_text:
                            self._ports.reply_text(
                                chat_id,
                                self._group_activation_denied_text(group_mode),
                                parent_message_id=message_id,
                            )
                        return
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
                    if control_text:
                        self._ports.handle_message(
                            sender_id,
                            chat_id,
                            text,
                            message_id,
                        )
                        return
                    if self._ports.should_route_group_followup_prompt(
                        sender_id,
                        chat_id,
                        message_id,
                    ):
                        self._process_cache.remember_message_context(
                            message_id,
                            {
                                **(
                                    self._process_cache.get_message_context(message_id)
                                    or {}
                                ),
                                "assistant_context_mode": "deferred_recovery",
                                "assistant_context_seq": current_seq,
                                "created_at": int(message.create_time or 0),
                                "sender_name": sender_name,
                            },
                        )
                        self._ports.handle_message(
                            sender_id,
                            chat_id,
                            text,
                            message_id,
                        )
                        return
                    if not self._ports.allow_group_prompt(
                        sender_id,
                        chat_id,
                        message_id,
                    ):
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
                        logger.warning(
                            "群历史回捞失败: chat=%s, error=%s", chat_id, exc
                        )
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
                    self._ports.handle_message(
                        sender_id,
                        chat_id,
                        assistant_text,
                        message_id,
                    )
                    return

            if (
                group_mode == self.GROUP_MODE_MENTION
                and not bot_mentioned
                and not is_attachment_message
            ):
                logger.debug(
                    "忽略群聊非触发 mention 消息: chat=%s, user=%s",
                    chat_id,
                    sender_user_id,
                )
                return

            if not allowed_to_use:
                if not is_attachment_message and (
                    bot_mentioned or text.startswith("/")
                ):
                    self._ports.reply_text(
                        chat_id,
                        self._group_activation_denied_text(group_mode),
                        parent_message_id=message_id,
                    )
                return
            if not is_attachment_message and not control_text:
                route_followup = self._ports.should_route_group_followup_prompt(
                    sender_id,
                    chat_id,
                    message_id,
                )
                if not route_followup and not self._ports.allow_group_prompt(
                    sender_id,
                    chat_id,
                    message_id,
                ):
                    return
        if is_attachment_message:
            resource_key = self._message_codec.attachment_resource_key(
                msg_type,
                content_dict,
            )
            attachment_name = self._message_codec.attachment_message_name(
                msg_type,
                content_dict,
            )
            self._ports.handle_attachment(
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
                if group_mode == self.GROUP_MODE_MENTION:
                    self._ports.handle_message(
                        sender_id,
                        chat_id,
                        self._build_group_turn_text("", sender_name=sender_name),
                        message_id,
                    )
                elif group_mode == self.GROUP_MODE_ASSISTANT:
                    self._ports.handle_message(
                        sender_id,
                        chat_id,
                        self._build_group_current_turn_text(
                            "", sender_name=sender_name
                        ),
                        message_id,
                    )
            elif chat_type != "group":
                logger.info(
                    "忽略空文本消息: name=%s, open_id=%s, user_id=%s, msg_type=%s, message_id=%s",
                    sender_name,
                    sender_open_log,
                    sender_user_log,
                    msg_type,
                    message_id,
                )
                self._ports.reply_text(
                    chat_id,
                    "当前仅支持文本消息，请直接输入文字。",
                )
            return

        logger.info(
            "收到消息: name=%s, open_id=%s, user_id=%s, chat_type=%s, message_id=%s, text=%s",
            sender_name,
            sender_open_log,
            sender_user_log,
            chat_type,
            message_id,
            text,
        )
        outbound_text = text
        if (
            chat_type == "group"
            and not control_text
            and group_mode == self.GROUP_MODE_MENTION
        ):
            outbound_text = self._build_group_turn_text(
                text,
                sender_name=sender_name,
            )
        self._ports.handle_message(
            sender_id,
            chat_id,
            outbound_text,
            message_id,
        )
