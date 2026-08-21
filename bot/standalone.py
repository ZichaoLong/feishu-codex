"""
Codex 机器人适配层。
"""

import os
from pathlib import Path

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from bot.codex_handler import CodexHandler
from bot.feishu_destination_liveness_contract import FeishuDestinationLossProof
from bot.feishu_bot import FeishuBot
from bot.system_config import SystemConfig


class CodexBot(FeishuBot):
    """Codex 飞书机器人。"""

    def __init__(
        self,
        *,
        system_config: SystemConfig,
    ):
        config_dir = Path(os.environ["FOCUS_CONFIG_DIR"]) if "FOCUS_CONFIG_DIR" in os.environ else None
        data_dir = Path(os.environ["FOCUS_DATA_DIR"]) if "FOCUS_DATA_DIR" in os.environ else None
        super().__init__(
            data_dir=data_dir,
            system_config=system_config,
        )
        self._handler = CodexHandler(data_dir=data_dir, config_dir=config_dir)
        self._handler.on_register(self)

    def on_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._handler.handle_message(sender_id, chat_id, text, message_id=message_id)

    def on_card_action(
        self, sender_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        return self._handler.handle_card_action(sender_id, chat_id, message_id, action_value)

    def on_attachment_message(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self._handler.handle_attachment_message(
            sender_id,
            chat_id,
            message_id,
            attachment_type,
            resource_key,
            file_name,
        )

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        self._handler.handle_message_recalled(chat_id, message_id)

    def allow_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._handler.preflight_group_prompt(sender_id, chat_id, message_id=message_id)

    def should_route_group_followup_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        return self._handler.should_route_group_followup_prompt(sender_id, chat_id, message_id=message_id)

    def on_destination_loss_proof(self, proof: FeishuDestinationLossProof) -> None:
        self._handler.accept_destination_loss_proof(proof)
