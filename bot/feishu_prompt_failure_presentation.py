"""Best-effort presentation boundary for known Feishu prompt failures."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from bot.feishu_execution_start_contract import PromptTurnStartResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FeishuPromptFailurePresentationPorts:
    render_start_failure: Callable[..., None]
    reply_text: Callable[..., None]
    message_reply_in_thread: Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class FeishuPromptFailureScope:
    """Immutable input scope for one prompt-start admission."""

    owner: FeishuPromptFailurePresentation
    chat_id: str
    message_id: str
    surface: bool
    pre_owner_reason_code: str

    def pre_owner(
        self,
        exc: Exception,
        *,
        thread_id: str = "",
    ) -> PromptTurnStartResult:
        return self.owner.pre_owner_failure(
            exc,
            chat_id=self.chat_id,
            message_id=self.message_id,
            surface=self.surface,
            reason_code=self.pre_owner_reason_code,
            thread_id=thread_id,
        )

    def known_denial(
        self,
        text: str,
        *,
        reason_code: str = "",
        thread_id: str = "",
    ) -> PromptTurnStartResult:
        return self.owner.known_denial(
            text,
            chat_id=self.chat_id,
            message_id=self.message_id,
            surface=self.surface,
            reason_code=reason_code,
            thread_id=thread_id,
        )

    def settled_failure(
        self,
        text: str,
        *,
        reason_code: str,
        disposition: str,
        thread_id: str = "",
        routed: bool = False,
    ) -> PromptTurnStartResult:
        """Project an owner-settled failure without trusting presentation."""

        if self.surface:
            if routed:
                self.owner.reply_routed(
                    self.chat_id,
                    text,
                    message_id=self.message_id,
                )
            else:
                self.owner.render(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                )
        return PromptTurnStartResult(
            started=False,
            thread_id=thread_id,
            reason_code=reason_code,
            reason_text=text,
            disposition=disposition,
        )


class FeishuPromptFailurePresentation:
    """Own known no-effect results and their best-effort presentation."""

    def __init__(self, ports: FeishuPromptFailurePresentationPorts) -> None:
        self._ports = ports

    def scope(
        self,
        *,
        chat_id: str,
        message_id: str,
        surface: bool,
        pre_owner_reason_code: str,
    ) -> FeishuPromptFailureScope:
        return FeishuPromptFailureScope(
            owner=self,
            chat_id=chat_id,
            message_id=message_id,
            surface=surface,
            pre_owner_reason_code=pre_owner_reason_code,
        )

    def render(self, *, chat_id: str, message_id: str, text: str) -> None:
        try:
            self._ports.render_start_failure(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except Exception:
            logger.exception("Feishu prompt failure card/text presentation 失败")

    def reply_routed(self, chat_id: str, text: str, *, message_id: str) -> None:
        try:
            self._ports.reply_text(
                chat_id,
                text,
                message_id=message_id,
                reply_in_thread=self._ports.message_reply_in_thread(message_id),
            )
        except Exception:
            logger.exception("Feishu prompt failure routed reply 失败")

    def reply(self, chat_id: str, text: str, **kwargs: object) -> None:
        try:
            self._ports.reply_text(chat_id, text, **kwargs)
        except Exception:
            logger.exception("Feishu prompt failure reply 失败")

    def pre_owner_failure(
        self,
        exc: Exception,
        *,
        chat_id: str,
        message_id: str,
        surface: bool,
        reason_code: str,
        thread_id: str = "",
    ) -> PromptTurnStartResult:
        """Classify preparation failure before owner admission as no-effect."""

        logger.exception("Feishu prompt pre-owner preparation 失败")
        error_text = f"准备线程失败：{exc}"
        if surface:
            self.render(chat_id=chat_id, message_id=message_id, text=error_text)
        return PromptTurnStartResult(
            started=False,
            thread_id=thread_id,
            reason_code=reason_code,
            reason_text=error_text,
            disposition="known_no_effect_settled",
        )

    def known_denial(
        self,
        text: str,
        *,
        chat_id: str,
        message_id: str,
        surface: bool,
        reason_code: str = "",
        thread_id: str = "",
    ) -> PromptTurnStartResult:
        """Return an admitted-no-mutation denial even if its reply fails."""

        if surface:
            self.reply_routed(chat_id, text, message_id=message_id)
        return PromptTurnStartResult(
            started=False,
            thread_id=thread_id,
            reason_code=reason_code,
            reason_text=text,
            disposition="known_no_effect_settled",
        )
