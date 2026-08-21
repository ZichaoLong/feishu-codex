"""
飞书机器人基类
封装了连接、消息收发等通用逻辑，子类只需实现 on_message / on_card_action 处理业务。
"""

import json
import logging
import pathlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateImageRequest,
    CreateImageRequestBody,
    DeleteMessageRequest,
    GetChatRequest,
    GetMessageRequest,
    GetMessageResourceRequest,
    ListMessageRequest,
    P2ImChatDisbandedV1,
    P2ImChatMemberBotDeletedV1,
    P2ImMessageRecalledV1,
    P2ImMessageReceiveV1,
)
from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
    P2ApplicationBotMenuV6,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from bot.card_text_projection import CardTextProjection
from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)
from bot.feishu_ingress_controller import (
    FeishuInboundMessage,
    FeishuIngressController,
    FeishuIngressPorts,
)
from bot.feishu_message_codec import (
    FeishuMessageCodec,
    FeishuMessageCodecPorts,
    InteractiveMessageReadResult,
)
from bot.feishu_outbound import (
    FeishuOutboundGateway,
    FeishuOutboundResult,
)
from bot.feishu_process_cache import FeishuProcessCache
from bot.feishu_types import (
    BotIdentitySnapshot,
    GroupActivationSnapshot,
    MentionMember,
    MessageContextPayload,
)
from bot.feishu_ws_proxy import configure_feishu_ws_proxy
from bot.group_history_recovery import GroupHistoryRecovery, ListedMessagesPage
from bot.platform_paths import default_data_root
from bot.stores.group_chat_store import GroupChatStore
from bot.system_config import SystemConfig

logger = logging.getLogger(__name__)

_CARD_MSG_CONTENT_TYPE_USER_CARD_CONTENT = "user_card_content"


@dataclass(frozen=True, slots=True)
class DownloadedMessageResource:
    content: bytes
    file_name: str
    content_type: str

class FeishuBot(ABC):
    """飞书机器人基类

    关键部分：
    1. 连接层: __init__ 中创建 lark.Client 和事件回调，start() 启动 WebSocket
    2. 消息收发层: send_message 泛化发送，reply / reply_card 为便捷方法
    3. 业务逻辑层: 子类实现 on_message 和 on_card_action
    """

    def __init__(
        self,
        *,
        system_config: SystemConfig,
        data_dir: pathlib.Path | None = None,
    ):
        self.app_id = system_config.app_id
        self.app_secret = system_config.app_secret
        self.request_timeout_seconds = system_config.request_timeout_seconds
        self._process_cache = FeishuProcessCache()
        self._feishu_ws_proxy_mode = system_config.feishu_ws_proxy
        self._debug_raw_card_ingress = system_config.debug_raw_card_ingress
        self._message_codec = FeishuMessageCodec(
            FeishuMessageCodecPorts(
                load_raw_card_content=self._load_raw_card_content_dict,
                resolve_sender_name=self._resolve_sender_name,
                remember_sender_name=lambda key, value: (
                    self._process_cache.remember_sender_name(key, value=value)
                ),
                configured_trigger_open_ids=(
                    lambda: self._ingress.configured_group_trigger_open_ids()
                ),
                log_card_ingress_event=lambda event, fields: (
                    self._log_card_ingress_event(event, **fields)
                ),
            )
        )
        self._ingress = FeishuIngressController(
            ports=FeishuIngressPorts(
                handle_message=lambda sender_id, chat_id, text, message_id: (
                    self.on_message(
                        sender_id,
                        chat_id,
                        text,
                        message_id=message_id,
                    )
                ),
                handle_attachment=self.on_attachment_message,
                allow_group_prompt=lambda sender_id, chat_id, message_id: (
                    self.allow_group_prompt(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                    )
                ),
                should_route_group_followup_prompt=(
                    lambda sender_id, chat_id, message_id: (
                        self.should_route_group_followup_prompt(
                            sender_id,
                            chat_id,
                            message_id=message_id,
                        )
                    )
                ),
                reply_text=self.reply,
                send_message=self.send_message,
                reply_to_message=self.reply_to_message,
                patch_message=self.patch_message,
                list_history_messages_page=self._list_history_messages_page,
                fetch_merge_forward_items=self._fetch_merge_forward_items,
                display_name_for_sender_identity=(
                    self._display_name_for_sender_identity
                ),
                log_card_ingress_event=lambda event, fields: (
                    self._log_card_ingress_event(event, **fields)
                ),
            ),
            process_cache=self._process_cache,
            message_codec=self._message_codec,
            group_store=GroupChatStore(data_dir or default_data_root()),
            app_id=self.app_id,
            admin_open_ids=set(system_config.admin_open_ids),
            configured_bot_open_id=system_config.bot_open_id,
            configured_trigger_open_ids=set(system_config.trigger_open_ids),
            history_fetch_limit=system_config.group_history_fetch_limit,
            history_fetch_lookback_seconds=(
                system_config.group_history_fetch_lookback_seconds
            ),
        )

        self.client = lark.Client.builder() \
            .app_id(self.app_id) \
            .app_secret(self.app_secret) \
            .timeout(self.request_timeout_seconds) \
            .log_level(lark.LogLevel.INFO) \
            .build()
        self._outbound = FeishuOutboundGateway(
            client=lambda: self.client,
            publish_destination_loss=self.on_destination_loss_proof,
            request_timeout_seconds=self.request_timeout_seconds,
        )

        self._event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_raw_message) \
            .register_p2_im_message_recalled_v1(self._on_raw_message_recalled) \
            .register_p2_im_chat_disbanded_v1(self._on_raw_chat_disbanded) \
            .register_p2_im_chat_member_bot_deleted_v1(self._on_raw_chat_member_bot_deleted) \
            .register_p2_card_action_trigger(self._on_raw_card_action) \
            .register_p2_application_bot_menu_v6(self._on_raw_bot_menu) \
            .build()

    def set_terminal_result_text_resolver(
        self,
        resolver: Callable[[CardTextProjection], str] | None,
    ) -> None:
        self._message_codec.set_terminal_result_text_resolver(resolver)

    # ---- 消息收发层 ----

    def get_group_mode(self, chat_id: str) -> str:
        return self._ingress.get_group_mode(chat_id)

    def set_group_mode(self, chat_id: str, mode: str) -> str:
        return self._ingress.set_group_mode(chat_id, mode)

    def get_group_activation_snapshot(self, chat_id: str) -> GroupActivationSnapshot:
        return self._ingress.get_group_activation_snapshot(chat_id)

    def activate_group_chat(self, chat_id: str, *, activated_by: str) -> GroupActivationSnapshot:
        return self._ingress.activate_group_chat(
            chat_id,
            activated_by=activated_by,
        )

    def deactivate_group_chat(self, chat_id: str) -> GroupActivationSnapshot:
        return self._ingress.deactivate_group_chat(chat_id)

    def is_admin(self, *, open_id: str = "") -> bool:
        return self._ingress.is_admin(open_id=open_id)

    def add_admin_open_id(self, open_id: str) -> list[str]:
        return self._ingress.add_admin_open_id(open_id)

    def list_admin_open_ids(self) -> list[str]:
        return self._ingress.list_admin_open_ids()

    def set_configured_bot_open_id(self, open_id: str) -> str:
        return self._ingress.set_configured_bot_open_id(open_id)

    def is_group_admin(self, *, open_id: str = "") -> bool:
        return self._ingress.is_group_admin(open_id=open_id)

    def is_group_user_allowed(self, chat_id: str, *, open_id: str = "") -> bool:
        return self._ingress.is_group_user_allowed(chat_id, open_id=open_id)

    def get_message_context(self, message_id: str) -> MessageContextPayload:
        return self._process_cache.get_message_context(message_id)

    def remember_chat_type(self, chat_id: str, chat_type: str) -> None:
        self._process_cache.remember_chat_type(chat_id, chat_type)

    def lookup_chat_type(self, chat_id: str) -> str:
        return self._process_cache.lookup_chat_type(chat_id)

    def remember_chat_display_name(self, chat_id: str, display_name: str) -> None:
        self._process_cache.remember_chat_display_name(chat_id, display_name)

    def lookup_chat_display_name(self, chat_id: str) -> str:
        return self._process_cache.lookup_chat_display_name(chat_id)

    def get_chat_display_name(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        cached = self._process_cache.lookup_chat_display_name(
            normalized_chat_id
        )
        if cached:
            return cached
        return self.refresh_chat_display_name(normalized_chat_id)

    def refresh_chat_display_name(self, chat_id: str) -> str:
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            return ""
        try:
            request = GetChatRequest.builder().chat_id(normalized_chat_id).build()
            response = self.client.im.v1.chat.get(request)
        except Exception as exc:
            logger.warning("查询 chat 名称失败(SDK异常): chat=%s, error=%s", normalized_chat_id, exc)
            return ""
        if not response.success():
            logger.warning("查询 chat 名称失败: chat=%s, code=%s, msg=%s", normalized_chat_id, response.code, response.msg)
            return ""
        data = getattr(response, "data", None)
        display_name = str(getattr(data, "name", "") or "").strip()
        if display_name:
            self._process_cache.remember_chat_display_name(
                normalized_chat_id,
                display_name,
            )
        chat_mode = str(getattr(data, "chat_mode", "") or "").strip()
        if chat_mode == "p2p":
            self._process_cache.remember_chat_type(normalized_chat_id, "p2p")
        elif chat_mode in {"group", "topic"}:
            self._process_cache.remember_chat_type(normalized_chat_id, "group")
        return display_name

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
        chat_name = str(getattr(data, "name", "") or "").strip()
        if chat_name:
            self._process_cache.remember_chat_display_name(
                normalized_chat_id,
                chat_name,
            )
        chat_mode = str(getattr(data, "chat_mode", "") or "").strip()
        if chat_mode == "p2p":
            self._process_cache.remember_chat_type(normalized_chat_id, "p2p")
            return "p2p"
        if chat_mode in {"group", "topic"}:
            self._process_cache.remember_chat_type(normalized_chat_id, "group")
            return "group"
        return ""

    def reserve_execution_card(self, trigger_message_id: str, card_message_id: str) -> None:
        self._process_cache.reserve_execution_card(
            trigger_message_id,
            card_message_id,
        )

    def claim_reserved_execution_card(self, trigger_message_id: str) -> str:
        return self._process_cache.claim_reserved_execution_card(
            trigger_message_id
        )

    def extract_non_bot_mentions(self, message_id: str) -> list[MentionMember]:
        return self._ingress.extract_non_bot_mentions(message_id)

    def lookup_cached_sender_name(self, sender_id: str) -> str:
        return self._process_cache.lookup_sender_name(sender_id)

    def get_sender_display_name(self, *, user_id: str = "", open_id: str = "", sender_type: str = "user") -> str:
        return self._display_name_for_sender_identity(
            user_id=user_id,
            sender_principal_id=open_id,
            sender_type=sender_type,
        )

    def forget_chat_state_after_destination_loss(self, chat_id: str) -> None:
        self._ingress.forget_chat_state_after_destination_loss(chat_id)

    def read_interactive_message(
        self,
        message_id: str,
        *,
        content_dict: dict[str, Any] | None = None,
    ) -> InteractiveMessageReadResult:
        return self._message_codec.read_interactive_message(
            message_id,
            content_dict=content_dict,
        )

    def read_interactive_message_text(
        self,
        message_id: str,
        *,
        content_dict: dict[str, Any] | None = None,
    ) -> str:
        return self.read_interactive_message(
            message_id,
            content_dict=content_dict,
        ).text

    @staticmethod
    def _sender_ids(sender_id: Any) -> tuple[str, str]:
        if sender_id is None:
            return "", ""
        return (
            str(getattr(sender_id, "user_id", "") or "").strip(),
            str(getattr(sender_id, "open_id", "") or "").strip(),
        )

    def _display_name_for_sender_identity(
        self,
        *,
        user_id: str = "",
        sender_principal_id: str = "",
        sender_type: str = "user",
    ) -> str:
        if sender_type == "app":
            cache_key = sender_principal_id or user_id
            cached = self._process_cache.lookup_sender_name(cache_key)
            if cached:
                return cached
            short_id = (sender_principal_id or user_id or "unknown")[:8]
            return f"机器人:{short_id}"
        cached = self._process_cache.lookup_sender_name(
            sender_principal_id
        ) or self._process_cache.lookup_sender_name(user_id)
        if cached:
            return cached
        if sender_principal_id:
            resolution = self._resolve_sender_name_diagnostic(sender_principal_id)
            resolved = str(resolution.get("resolved_name", "") or "").strip()
            if resolved and not bool(resolution.get("used_fallback")):
                self._process_cache.remember_sender_name(
                    sender_principal_id,
                    user_id,
                    value=resolved,
                )
            return resolved or sender_principal_id[:8]
        if user_id:
            self._process_cache.remember_sender_name(
                user_id,
                value=user_id[:8],
            )
            return user_id[:8]
        return "unknown"

    def _fetch_bot_open_id(self) -> Optional[str]:
        """调用飞书 API 获取机器人自身的 open_id，仅供 `/bot-status` 之类的显式探测使用。"""
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
        return self._ingress.identity_snapshot(self._fetch_bot_open_id() or "")

    def _resolve_sender_name(self, open_id: str) -> str:
        """通过 open_id 查询用户姓名，失败时返回 open_id 前 8 位作为兜底"""
        snapshot = self._resolve_sender_name_diagnostic(open_id)
        return str(snapshot.get("resolved_name", "") or open_id[:8]).strip() or open_id[:8]

    def _log_sender_name_resolution_fallback(self, snapshot: dict[str, Any]) -> None:
        open_id = str(snapshot.get("open_id", "") or "").strip() or "unknown"
        fallback_reason = str(snapshot.get("fallback_reason", "") or "unknown").strip() or "unknown"
        level = (
            logging.WARNING
            if self._process_cache.should_emit_sender_name_warning(
                open_id,
                fallback_reason,
            )
            else logging.DEBUG
        )
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

    def debug_sender_name_resolution(self, open_id: str) -> dict[str, Any]:
        normalized_open_id = str(open_id or "").strip()
        cached_name = self._process_cache.lookup_sender_name(
            normalized_open_id
        )
        live = self._resolve_sender_name_diagnostic(
            normalized_open_id,
            log_failures=False,
        )
        resolved_name = str(live.get("resolved_name", "") or "").strip()
        if resolved_name and not bool(live.get("used_fallback")):
            self._process_cache.remember_sender_name(
                normalized_open_id,
                value=resolved_name,
            )
        return {
            "open_id": normalized_open_id,
            "cache_hit": bool(cached_name),
            "cached_name": cached_name,
            **live,
        }

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
        card_msg_content_type: str = "",
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
        normalized_card_content_type = str(card_msg_content_type or "").strip()
        if normalized_card_content_type:
            request.queries.append(("card_msg_content_type", normalized_card_content_type))
        response = self.client.im.v1.message.list(request)
        if not response.success():
            raise RuntimeError(f"code={response.code}, msg={response.msg}")

        body = response.data
        return ListedMessagesPage(
            items=list(getattr(body, "items", None) or []),
            has_more=bool(getattr(body, "has_more", False)),
            page_token=str(getattr(body, "page_token", "") or "").strip(),
        )

    def list_recent_messages(
        self,
        *,
        chat_id: str,
        thread_id: str = "",
        limit: int = 20,
        card_msg_content_type: str = "",
    ) -> list[Any]:
        normalized_limit = max(int(limit or 0), 0)
        if normalized_limit <= 0:
            return []

        container_id_type = "thread" if str(thread_id or "").strip() else "chat"
        container_id = str(thread_id or "").strip() or str(chat_id or "").strip()
        if not container_id:
            return []

        page_size = min(normalized_limit, 50)
        page_token = ""
        items: list[Any] = []
        sort_type = "ByCreateTimeDesc"

        try:
            while len(items) < normalized_limit:
                page = self._list_history_messages_page(
                    container_id_type=container_id_type,
                    container_id=container_id,
                    sort_type=sort_type,
                    page_size=page_size,
                    page_token=page_token,
                    card_msg_content_type=card_msg_content_type,
                )
                page_items = list(page.items or [])
                if not page_items:
                    break
                items.extend(page_items)
                if not page.has_more or not page.page_token:
                    break
                page_token = page.page_token
        except Exception as exc:
            if container_id_type != "thread" or not GroupHistoryRecovery.should_fallback_thread_history_scan(exc):
                raise
            page_token = ""
            items = []
            while True:
                page = self._list_history_messages_page(
                    container_id_type="thread",
                    container_id=container_id,
                    sort_type="ByCreateTimeAsc",
                    page_size=50,
                    page_token=page_token,
                    card_msg_content_type=card_msg_content_type,
                )
                page_items = list(page.items or [])
                if page_items:
                    items.extend(page_items)
                    if len(items) > normalized_limit:
                        items = items[-normalized_limit:]
                if not page.has_more or not page.page_token:
                    break
                page_token = page.page_token
            items.reverse()
        return items[:normalized_limit]

    def get_message_items(
        self,
        message_id: str,
        *,
        card_msg_content_type: str = "",
    ) -> list[Any]:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return []
        request = GetMessageRequest.builder().message_id(normalized_message_id).build()
        normalized_card_content_type = str(card_msg_content_type or "").strip()
        if normalized_card_content_type:
            request.queries.append(("card_msg_content_type", normalized_card_content_type))
        response = self.client.im.v1.message.get(request)
        if not response.success():
            raise RuntimeError(f"code={response.code}, msg={response.msg}")
        return list(getattr(response.data, "items", None) or [])

    def get_message_content_dict(
        self,
        message_id: str,
        *,
        card_msg_content_type: str = "",
    ) -> dict[str, Any]:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return {}
        items = self.get_message_items(
            normalized_message_id,
            card_msg_content_type=card_msg_content_type,
        )
        for item in items:
            if str(getattr(item, "message_id", "") or "").strip() != normalized_message_id:
                continue
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "").strip()
            if not raw_content:
                continue
            try:
                content_dict = json.loads(raw_content)
            except Exception:
                return {}
            if isinstance(content_dict, dict):
                return content_dict
        return {}

    @staticmethod
    def _normalize_card_ingress_log_value(value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, (list, tuple)):
            return [FeishuBot._normalize_card_ingress_log_value(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): FeishuBot._normalize_card_ingress_log_value(item)
                for key, item in value.items()
            }
        return repr(value)

    def _log_card_ingress_event(self, event: str, **fields: Any) -> None:
        if not self._debug_raw_card_ingress:
            return
        normalized_fields: dict[str, Any] = {}
        for key, value in fields.items():
            normalized_fields[key] = self._normalize_card_ingress_log_value(value)
        logger.info("card_ingress_%s %s", event, json.dumps(normalized_fields, ensure_ascii=False, sort_keys=True))

    def _load_raw_card_content_dict(self, message_id: str) -> dict[str, Any]:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return {}
        try:
            content_dict = self.get_message_content_dict(
                normalized_message_id,
                card_msg_content_type=_CARD_MSG_CONTENT_TYPE_USER_CARD_CONTENT,
            )
        except Exception as exc:
            self._log_card_ingress_event(
                "raw_card_fetch",
                message_id=normalized_message_id,
                ok=False,
                error=str(exc),
            )
            return {}
        if not isinstance(content_dict, dict) or not content_dict:
            self._log_card_ingress_event(
                "raw_card_fetch",
                message_id=normalized_message_id,
                ok=False,
                error="message_not_found_in_items",
            )
            return {}
        self._log_card_ingress_event(
            "raw_card_fetch",
            message_id=normalized_message_id,
            ok=True,
            schema=str(content_dict.get("schema", "") or ""),
            title=str((content_dict.get("header") or {}).get("title", {}).get("content", "") or "")
            if isinstance(content_dict.get("header"), dict)
            else str(content_dict.get("title", "") or ""),
        )
        return content_dict

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
        return self._ingress.prepare_queued_prompt_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            assistant_context_mode=assistant_context_mode,
            assistant_context_created_at=assistant_context_created_at,
            assistant_context_seq=assistant_context_seq,
            assistant_context_sender_name=assistant_context_sender_name,
            origin_feishu_thread_id=origin_feishu_thread_id,
        )

    def _fetch_merge_forward_items(self, merge_message_id: str) -> list[Any]:
        try:
            items = self.get_message_items(merge_message_id)
            for item in items:
                sub_message_id = str(getattr(item, "message_id", "") or "").strip()
                sub_type = str(getattr(item, "msg_type", "") or "").strip()
                if not sub_message_id or sub_message_id == str(merge_message_id or "").strip():
                    continue
                if sub_type != "interactive":
                    continue
                raw_content_dict = self._load_raw_card_content_dict(sub_message_id)
                if not raw_content_dict:
                    continue
                body = getattr(item, "body", None)
                if body is None:
                    continue
                try:
                    setattr(body, "content", json.dumps(raw_content_dict, ensure_ascii=False))
                    self._log_card_ingress_event(
                        "merge_forward_child",
                        parent_message_id=str(merge_message_id or "").strip(),
                        child_message_id=sub_message_id,
                        msg_type=sub_type,
                        path="raw_card_from_merge_forward_child",
                    )
                except Exception as exc:
                    self._log_card_ingress_event(
                        "merge_forward_child",
                        parent_message_id=str(merge_message_id or "").strip(),
                        child_message_id=sub_message_id,
                        msg_type=sub_type,
                        path="raw_card_from_merge_forward_child",
                        ok=False,
                        error=str(exc),
                    )
            self._log_card_ingress_event(
                "merge_forward_expansion",
                message_id=str(merge_message_id or "").strip(),
                ok=True,
                item_count=len(items),
                child_message_ids=[
                    str(getattr(item, "message_id", "") or "").strip()
                    for item in items
                    if str(getattr(item, "message_id", "") or "").strip()
                ],
            )
            return items
        except Exception as exc:
            self._log_card_ingress_event(
                "merge_forward_expansion",
                message_id=str(merge_message_id or "").strip(),
                ok=False,
                error=str(exc),
            )
            logger.warning(
                "获取合并转发消息异常: message_id=%s, error=%s",
                merge_message_id,
                exc,
            )
            return []

    def _on_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """Translate and dispatch one raw SDK message with top-level isolation."""
        try:
            self._handle_raw_message(data)
        except Exception as exc:
            logger.error("处理消息事件异常: %s", exc, exc_info=True)

    def _handle_raw_message(self, data: P2ImMessageReceiveV1) -> None:
        """Translate an SDK event into the surface-neutral ingress envelope."""
        message = data.event.message
        sender = data.event.sender
        sender_user_id, sender_open_id = self._sender_ids(
            getattr(sender, "sender_id", None)
        )
        self._ingress.handle_message(
            FeishuInboundMessage(
                sender_type=str(
                    getattr(sender, "sender_type", "") or "user"
                ),
                sender_user_id=sender_user_id,
                sender_open_id=sender_open_id,
                chat_id=str(getattr(message, "chat_id", "") or ""),
                message_id=str(getattr(message, "message_id", "") or ""),
                message_type=str(
                    getattr(message, "message_type", "") or ""
                ),
                chat_type=str(
                    getattr(message, "chat_type", "") or "p2p"
                ),
                content=str(getattr(message, "content", "") or ""),
                mentions=tuple(getattr(message, "mentions", None) or ()),
                create_time=getattr(message, "create_time", None),
                thread_id=str(getattr(message, "thread_id", "") or ""),
                root_id=str(getattr(message, "root_id", "") or ""),
                parent_id=str(getattr(message, "parent_id", "") or ""),
            )
        )

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
            if user_id:
                action_value["_operator_user_id"] = str(user_id).strip()
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
        proof = FeishuDestinationLossProof(
            source_id=str(getattr(data.header, "event_id", "") or ""),
            chat_id=str(getattr(data.event, "chat_id", "") or ""),
            proof_type=FeishuDestinationLossProofType.CHAT_DISBANDED_EVENT,
        )
        logger.info(
            "群聊已解散: event=%s chat=%s",
            proof.source_id,
            proof.chat_id,
        )
        self.on_destination_loss_proof(proof)

    def _on_raw_chat_member_bot_deleted(self, data: P2ImChatMemberBotDeletedV1) -> None:
        proof = FeishuDestinationLossProof(
            source_id=str(getattr(data.header, "event_id", "") or ""),
            chat_id=str(getattr(data.event, "chat_id", "") or ""),
            proof_type=FeishuDestinationLossProofType.BOT_REMOVED_EVENT,
        )
        logger.info(
            "机器人已被移出群聊: event=%s chat=%s",
            proof.source_id,
            proof.chat_id,
        )
        self.on_destination_loss_proof(proof)

    def _on_raw_message_recalled(self, data: P2ImMessageRecalledV1) -> None:
        try:
            message_id = str(data.event.message_id or "").strip()
            chat_id = str(data.event.chat_id or "").strip()
            if not message_id:
                return
            logger.info("消息已撤回: chat=%s message_id=%s", chat_id, message_id)
            self.on_message_recalled(chat_id, message_id)
        except Exception as e:
            logger.error("处理消息撤回事件异常: %s", e, exc_info=True)

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        return self._outbound.send_message(
            chat_id,
            msg_type,
            content,
            attempt_id=attempt_id,
        )

    def upload_image(self, local_path: str) -> str | None:
        normalized_path = str(local_path or "").strip()
        if not normalized_path:
            return None
        image_path = pathlib.Path(normalized_path).expanduser()
        if not image_path.exists() or not image_path.is_file():
            logger.error("上传图片失败: 路径不存在或不是文件 path=%s", image_path)
            return None
        try:
            with image_path.open("rb") as image_file:
                request = CreateImageRequest.builder().request_body(
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(image_file)
                    .build()
                ).build()
                response = self.client.im.v1.image.create(request)
        except Exception as e:
            logger.exception("上传图片失败(SDK异常): path=%s error=%s", image_path, e)
            return None
        if not response.success():
            logger.error("上传图片失败: path=%s code=%s msg=%s", image_path, response.code, response.msg)
            return None
        image_key = str(getattr(getattr(response, "data", None), "image_key", "") or "").strip()
        if not image_key:
            logger.error("上传图片失败: path=%s image_key 为空", image_path)
            return None
        return image_key

    def reply_local_image(
        self,
        chat_id: str,
        local_path: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str | None:
        image_key = self.upload_image(local_path)
        if not image_key:
            return None
        content = json.dumps({"image_key": image_key}, ensure_ascii=False)
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            result = self.reply_to_message(
                chat_id,
                normalized_parent_id,
                "image",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
            return result.message_id if result.ok else None
        return self.send_image_by_key(chat_id, image_key)

    def send_image_by_key(self, chat_id: str, image_key: str) -> str | None:
        normalized_image_key = str(image_key or "").strip()
        if not normalized_image_key:
            return None
        result = self.send_message(
            chat_id,
            "image",
            json.dumps({"image_key": normalized_image_key}, ensure_ascii=False),
        )
        return result.message_id if result.ok else None

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        """Patch one exact message and return effect plus liveness evidence."""
        return self._outbound.patch_message(
            chat_id=chat_id,
            message_id=message_id,
            content=content,
            attempt_id=attempt_id,
        )

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
    ) -> bool:
        """发送文本消息"""
        content = json.dumps({"text": text})
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            return self.reply_to_message(
                chat_id,
                normalized_parent_id,
                "text",
                content,
                reply_in_thread=self._should_reply_in_thread(
                    normalized_parent_id,
                    reply_in_thread,
                ),
            ).ok
        return self.send_message(chat_id, "text", content).ok

    def reply_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        content = json.dumps({"text": text})
        normalized_parent_id = str(parent_message_id or "").strip()
        if normalized_parent_id:
            result = self.reply_to_message(
                chat_id,
                normalized_parent_id,
                "text",
                content,
                reply_in_thread=self._should_reply_in_thread(
                    normalized_parent_id,
                    reply_in_thread,
                ),
            )
        else:
            result = self.send_message(chat_id, "text", content)
        return result.message_id if result.ok else ""

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
                chat_id,
                normalized_parent_id,
                "interactive",
                content,
                reply_in_thread=self._should_reply_in_thread(normalized_parent_id, reply_in_thread),
            )
            return
        self.send_message(chat_id, "interactive", content)

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        """Reply once with an official stable UUID and a typed outcome."""
        effective_reply_in_thread = self._should_reply_in_thread(
            parent_id, reply_in_thread
        )
        return self._outbound.reply_to_message(
            chat_id=chat_id,
            parent_id=parent_id,
            msg_type=msg_type,
            content=content,
            reply_in_thread=effective_reply_in_thread,
            attempt_id=attempt_id,
        )

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

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        """处理飞书消息撤回事件，子类可覆写"""
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

    def should_route_group_followup_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        """Whether this group message should bypass preflight and enter the binding FIFO."""
        del sender_id
        del chat_id
        del message_id
        return False

    def on_destination_loss_proof(self, proof: FeishuDestinationLossProof) -> None:
        del proof

    # ---- 启动 ----

    def start(self) -> None:
        """启动 WebSocket 长连接，开始监听消息"""
        configure_feishu_ws_proxy(self._feishu_ws_proxy_mode)
        ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=self._event_handler,
            log_level=lark.LogLevel.INFO,
        )
        logger.info("机器人启动中，正在连接飞书...")
        ws_client.start()
