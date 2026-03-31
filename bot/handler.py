"""
飞书机器人消息处理器抽象基类

FeishuBot 负责连接和消息收发；BotHandler 负责业务逻辑。
子类实现 handle_message 等方法，由 FeishuBot 或其子类调用。
"""

from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from bot.feishu_bot import FeishuBot
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )


class BotHandler(ABC):

    def __init__(self):
        self.bot: Optional["FeishuBot"] = None

    # ---- 元信息（子类必须实现） ----

    @property
    @abstractmethod
    def name(self) -> str:
        """显示名称，如 'Claude Code'"""
        ...

    @property
    @abstractmethod
    def keyword(self) -> str:
        """触发关键词"""
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """一句话描述"""
        ...

    # ---- 生命周期 ----

    def on_register(self, bot: "FeishuBot") -> None:
        """注册时由 FeishuBot 调用，注入 bot 引用"""
        self.bot = bot

    # ---- 消息处理（子类实现） ----

    @abstractmethod
    def handle_message(self, user_id: str, chat_id: str, text: str,
                       message_id: str = "") -> None:
        """处理路由过来的文本消息"""
        ...

    def handle_card_action(
        self, user_id: str, chat_id: str, message_id: str, action_value: dict
    ) -> "P2CardActionTriggerResponse":
        """处理卡片按钮点击，默认无操作，有卡片交互的子类需覆写"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        return P2CardActionTriggerResponse()

    def handle_file_message(
        self, user_id: str, chat_id: str, message_id: str,
        file_key: str, file_name: str
    ) -> None:
        """处理文件消息，默认忽略，需要处理文件的子类覆写此方法"""
        pass

    # ---- 会话状态 ----

    def is_user_active(self, user_id: str, chat_id: str = "") -> bool:
        """用户是否在本处理器的活跃会话中，默认 False（无状态子类）"""
        return False

    def deactivate_user(self, user_id: str, chat_id: str = "") -> None:
        """清理用户会话状态，默认无操作"""
        pass
