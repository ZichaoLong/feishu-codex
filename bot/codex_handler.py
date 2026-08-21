"""Thin Feishu platform adapter for the Focus runtime."""

from __future__ import annotations

import pathlib
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.constants import KEYWORD
from bot.feishu_destination_liveness_contract import FeishuDestinationLossProof
from bot.focus_runtime.runtime import FocusRuntime
from bot.handler import BotHandler


class CodexHandler(BotHandler):
    """Bind Feishu's ``BotHandler`` contract to one ``FocusRuntime``."""

    def __init__(
        self,
        data_dir: pathlib.Path | None = None,
        config_dir: pathlib.Path | None = None,
    ) -> None:
        super().__init__()
        self._runtime = FocusRuntime(data_dir=data_dir, config_dir=config_dir)

    @property
    def runtime(self) -> FocusRuntime:
        return self._runtime

    @property
    def name(self) -> str:
        return "Codex"

    @property
    def keyword(self) -> str:
        return KEYWORD

    @property
    def description(self) -> str:
        return "通过飞书与 Codex 交互"

    def on_register(self, bot) -> None:
        super().on_register(bot)
        self._runtime.start(bot)

    def handle_message(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
    ) -> None:
        self._runtime.handle_message(sender_id, chat_id, text, message_id)

    def handle_message_recalled(self, chat_id: str, message_id: str) -> None:
        self._runtime.handle_message_recalled(chat_id, message_id)

    def handle_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        return self._runtime.handle_card_action(
            sender_id,
            chat_id,
            message_id,
            action_value,
        )

    def handle_attachment_message(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self._runtime.handle_attachment_message(
            sender_id,
            chat_id,
            message_id,
            attachment_type,
            resource_key,
            file_name,
        )

    def is_sender_active(
        self,
        sender_id: str,
        chat_id: str = "",
        message_id: str = "",
    ) -> bool:
        return self._runtime.is_sender_active(sender_id, chat_id, message_id)

    def deactivate_sender(
        self,
        sender_id: str,
        chat_id: str = "",
        message_id: str = "",
    ) -> None:
        self._runtime.deactivate_sender(sender_id, chat_id, message_id)

    def preflight_group_prompt(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        return self._runtime.preflight_group_prompt(
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def should_route_group_followup_prompt(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        return self._runtime.should_route_group_followup_prompt(
            sender_id,
            chat_id,
            message_id=message_id,
        )

    def accept_destination_loss_proof(
        self,
        proof: FeishuDestinationLossProof,
    ) -> None:
        self._runtime.accept_destination_loss_proof(proof)

    def shutdown(self) -> None:
        self._runtime.shutdown()
