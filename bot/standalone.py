"""
Codex 机器人适配层。
"""

import os
from pathlib import Path

from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

from bot.codex_handler import CodexHandler
from bot.feishu_bot import FeishuBot


class CodexBot(FeishuBot):
    """Codex 飞书机器人。"""

    def __init__(self, app_id: str, app_secret: str):
        super().__init__(app_id, app_secret)
        config_dir = Path(os.environ["FC_CONFIG_DIR"]) if "FC_CONFIG_DIR" in os.environ else None
        data_dir = Path(os.environ["FC_DATA_DIR"]) if "FC_DATA_DIR" in os.environ else None
        self._handler = CodexHandler(data_dir=data_dir, config_dir=config_dir)
        self._handler.on_register(self)

    def on_message(self, user_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self._handler.handle_message(user_id, chat_id, text, message_id=message_id)

    def on_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> P2CardActionTriggerResponse:
        return self._handler.handle_card_action(user_id, chat_id, message_id, action_value)

    def on_file_message(
        self, user_id: str, chat_id: str, message_id: str, file_key: str, file_name: str
    ) -> None:
        self._handler.handle_file_message(user_id, chat_id, message_id, file_key, file_name)
