"""Feishu platform access and presentation routing for Focus runtime."""

from __future__ import annotations

from typing import Any

from bot.runtime_card_publisher import RuntimeCardPublisher


class FeishuPlatform:
    """Own the runtime's attached Feishu bot reference and platform rules."""

    def __init__(self) -> None:
        self._bot: Any = None

    @property
    def bot(self) -> Any:
        return self._bot

    def attach(self, bot: Any) -> None:
        if self._bot is not None and self._bot is not bot:
            raise RuntimeError(
                "Focus runtime is already attached to another platform adapter"
            )
        self._bot = bot

    def runtime_card_publisher(self) -> RuntimeCardPublisher:
        return RuntimeCardPublisher(self.bot)

    def resolve_chat_type(self, chat_id: str, message_id: str = "") -> str:
        context = self.bot.get_message_context(message_id) if message_id else {}
        chat_type = str(context.get("chat_type", "")).strip()
        if chat_type:
            return chat_type
        chat_type = str(self.bot.lookup_chat_type(chat_id) or "").strip()
        if chat_type:
            return chat_type
        chat_type = str(self.bot.fetch_runtime_chat_type(chat_id) or "").strip()
        if chat_type:
            return chat_type
        return ""

    def is_group_chat(self, chat_id: str, message_id: str = "") -> bool:
        return self.resolve_chat_type(chat_id, message_id) == "group"

    def resolve_binding_chat_display_name(
        self,
        *,
        binding_kind: str,
        sender_id: str,
        chat_id: str,
        refresh_names: bool = False,
    ) -> str:
        if binding_kind == "p2p":
            if not refresh_names:
                return self.bot.lookup_cached_sender_name(sender_id)
            return self.bot.get_sender_display_name(
                open_id=sender_id,
                sender_type="user",
            )
        if binding_kind == "group":
            if not refresh_names:
                return self.bot.lookup_chat_display_name(chat_id)
            refresh_chat_display_name = getattr(
                self.bot,
                "refresh_chat_display_name",
                None,
            )
            if callable(refresh_chat_display_name):
                return refresh_chat_display_name(chat_id)
            return self.bot.get_chat_display_name(chat_id)
        return ""

    def group_actor_open_id(
        self,
        message_id: str = "",
        operator_open_id: str = "",
        sender_open_id: str = "",
    ) -> str:
        normalized_operator_open_id = str(operator_open_id or "").strip()
        if normalized_operator_open_id:
            return normalized_operator_open_id
        if message_id:
            context = self.bot.get_message_context(message_id)
            context_sender_open_id = str(
                context.get("sender_open_id", "")
            ).strip()
            if context_sender_open_id:
                return context_sender_open_id
        return str(sender_open_id or "").strip()

    def message_reply_in_thread(self, message_id: str) -> bool:
        if not message_id:
            return False
        context = self.bot.get_message_context(message_id)
        return bool(str(context.get("thread_id", "") or "").strip())

    def is_group_admin_actor(
        self,
        chat_id: str,
        *,
        message_id: str = "",
        operator_open_id: str = "",
        sender_open_id: str = "",
    ) -> bool:
        if not self.is_group_chat(chat_id, message_id):
            return True
        actor_open_id = self.group_actor_open_id(
            message_id,
            operator_open_id,
            sender_open_id,
        )
        return self.bot.is_group_admin(open_id=actor_open_id)

    def group_command_admin_denial_text(
        self,
        chat_id: str,
        message_id: str = "",
        sender_open_id: str = "",
    ) -> str:
        if not self.is_group_chat(chat_id, message_id):
            return ""
        if self.is_group_admin_actor(
            chat_id,
            message_id=message_id,
            sender_open_id=sender_open_id,
        ):
            return ""
        return "群里的 `/` 命令仅管理员可用；已授权成员请直接提问或显式 mention 触发机器人。"

    def interaction_actor_allowed(
        self,
        sender_id: str,
        chat_id: str,
        actor_open_id: str,
    ) -> bool:
        del sender_id
        if not self.is_group_chat(chat_id):
            return True
        return self.bot.is_group_user_allowed(chat_id, open_id=actor_open_id)

    def reply_text(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> bool:
        if self.is_group_chat(chat_id, message_id) and message_id:
            return bool(
                self.bot.reply(
                    chat_id,
                    text,
                    parent_message_id=message_id,
                    reply_in_thread=reply_in_thread,
                )
            )
        return bool(self.bot.reply(chat_id, text))

    def reply_text_get_id(
        self,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> str:
        if self.is_group_chat(chat_id, message_id) and message_id:
            return str(
                getattr(self.bot, "reply_get_id", lambda *_args, **_kwargs: "")(
                    chat_id,
                    text,
                    parent_message_id=message_id,
                    reply_in_thread=reply_in_thread,
                )
                or ""
            ).strip()
        return str(
            getattr(self.bot, "reply_get_id", lambda *_args, **_kwargs: "")(
                chat_id,
                text,
            )
            or ""
        ).strip()

    def reply_card(
        self,
        chat_id: str,
        card: dict,
        *,
        message_id: str = "",
        reply_in_thread: bool = False,
    ) -> None:
        if self.is_group_chat(chat_id, message_id) and message_id:
            self.bot.reply_card(
                chat_id,
                card,
                parent_message_id=message_id,
                reply_in_thread=reply_in_thread,
            )
            return
        self.bot.reply_card(chat_id, card)

    def claim_reserved_execution_card(self, trigger_message_id: str) -> str:
        if not trigger_message_id or not hasattr(
            self.bot,
            "claim_reserved_execution_card",
        ):
            return ""
        return str(
            self.bot.claim_reserved_execution_card(trigger_message_id) or ""
        ).strip()
