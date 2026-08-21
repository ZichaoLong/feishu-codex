"""Feishu terminal-result persistence, lookup, and publication."""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.focus_runtime.feishu_platform import FeishuPlatform
from bot.stores.terminal_result_store import TerminalResultRecord, TerminalResultStore


logger = logging.getLogger("bot.focus_runtime")


class _ResolveBindingSession(Protocol):
    def __call__(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> BindingSessionSnapshot: ...


class _PublishTerminalResult(Protocol):
    def __call__(
        self,
        chat_id: str,
        *,
        final_reply_text: str,
        source_execution_message_id: str = "",
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool: ...


class TerminalResults:
    """Project authoritative terminal text across persistence and Feishu cards."""

    def __init__(
        self,
        *,
        platform: FeishuPlatform,
        store: TerminalResultStore,
        resolve_session: _ResolveBindingSession,
        publish_terminal_result: _PublishTerminalResult,
    ) -> None:
        self._platform = platform
        self._store = store
        self._resolve_session = resolve_session
        self._publish_terminal_result = publish_terminal_result

    def find_last_card_text(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> str:
        bot = self._platform.bot
        feishu_thread_id = ""
        if message_id and hasattr(bot, "get_message_context"):
            context = bot.get_message_context(message_id) or {}
            feishu_thread_id = str(context.get("thread_id", "") or "").strip()
        try:
            codex_thread_id = self._resolve_session(
                sender_id,
                chat_id,
                message_id,
            ).current_thread_id.strip()
        except Exception:
            codex_thread_id = ""

        try:
            items = bot.list_recent_messages(
                chat_id=chat_id,
                thread_id=feishu_thread_id,
                limit=20,
            )
        except Exception as exc:
            logger.warning(
                "读取最近卡片失败: chat_id=%s feishu_thread_id=%s "
                "codex_thread_id=%s message_id=%s error=%s",
                chat_id,
                feishu_thread_id,
                codex_thread_id,
                message_id,
                exc,
            )
            return "读取最近卡片失败，请稍后重试。"

        app_id = str(getattr(bot, "app_id", "") or "").strip()
        fallback_text = ""
        for item in items:
            item_msg_type = str(getattr(item, "msg_type", "") or "").strip()
            sender = getattr(item, "sender", None)
            sender_type = str(getattr(sender, "sender_type", "") or "").strip()
            sender_id = str(getattr(sender, "id", "") or "").strip()
            if app_id and (sender_type != "app" or sender_id != app_id):
                continue

            item_message_id = str(
                getattr(item, "message_id", "") or ""
            ).strip()
            authoritative_text = self._store.get(item_message_id)
            if authoritative_text:
                return authoritative_text
            if item_msg_type != "interactive":
                continue
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "").strip()
            if not raw_content:
                continue
            try:
                content_dict = json.loads(raw_content)
            except Exception:
                continue
            if not isinstance(content_dict, dict):
                continue

            resolved = bot.read_interactive_message(
                message_id=item_message_id,
                content_dict=content_dict,
            )
            if (
                resolved.card_kind == "terminal"
                and resolved.text
                and resolved.has_authoritative_text
            ):
                return resolved.text
            if (
                not fallback_text
                and resolved.card_kind == "execution"
                and resolved.text
            ):
                fallback_text = resolved.text

        if fallback_text:
            return fallback_text
        if codex_thread_id:
            latest_thread_text = self._store.latest_for_thread(codex_thread_id)
            if latest_thread_text:
                return latest_thread_text
        return "最近没有找到可导出的终态卡；也没有可回退的执行卡。"

    def record_terminal_result_card_with_execution(
        self,
        *,
        message_id: str,
        execution_message_id: str,
        final_reply_text: str,
        terminal_result_id: str = "",
        thread_id: str = "",
        checksum: str = "",
    ) -> None:
        normalized_message_id = str(message_id or "").strip()
        normalized_execution_message_id = str(
            execution_message_id or ""
        ).strip()
        raw_text = str(final_reply_text or "")
        if not normalized_message_id or not raw_text:
            return
        self._store.upsert(
            TerminalResultRecord(
                message_id=normalized_message_id,
                execution_message_id=normalized_execution_message_id,
                final_reply_text=raw_text,
                recorded_at=time.time(),
                terminal_result_id=str(terminal_result_id or "").strip().lower(),
                thread_id=str(thread_id or "").strip(),
                checksum=str(checksum or "").strip().lower(),
            )
        )

    def resolve_terminal_result_text(self, projection) -> str:
        terminal_result_id = str(
            getattr(projection, "terminal_result_id", "") or ""
        ).strip().lower()
        if not terminal_result_id:
            return ""
        return self._store.get_by_terminal_result_id(
            terminal_result_id,
            checksum=str(
                getattr(projection, "terminal_result_checksum", "") or ""
            ).strip().lower(),
        )

    def has_recorded_terminal_result(
        self,
        *,
        execution_message_id: str,
        final_reply_text: str,
    ) -> bool:
        return self._store.has_execution_result(
            execution_message_id=execution_message_id,
            final_reply_text=final_reply_text,
        )

    def publish_terminal_result(
        self,
        chat_id: str,
        *,
        final_reply_text: str,
        source_execution_message_id: str = "",
        prompt_message_id: str = "",
        prompt_reply_in_thread: bool = False,
        thread_id: str = "",
    ) -> bool:
        bot = self._platform.bot
        normalized_thread_id = str(thread_id or "").strip()
        if (
            not normalized_thread_id
            and prompt_message_id
            and hasattr(bot, "get_message_context")
        ):
            try:
                context = bot.get_message_context(prompt_message_id) or {}
                normalized_thread_id = str(
                    context.get("thread_id", "") or ""
                ).strip()
            except Exception:
                normalized_thread_id = ""
        return self._publish_terminal_result(
            chat_id,
            final_reply_text=final_reply_text,
            source_execution_message_id=source_execution_message_id,
            prompt_message_id=prompt_message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            thread_id=normalized_thread_id,
        )
