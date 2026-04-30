from __future__ import annotations

from typing import Callable, TypeAlias

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.reason_codes import (
    PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    PROMPT_DENIED_BY_OTHER_GROUP_ALL_OWNER,
    ReasonedCheck,
)
from bot.stores.interaction_lease_store import InteractionLease, InteractionLeaseHolder

ChatBindingKey: TypeAlias = tuple[str, str]


class ThreadAccessPolicy:
    def __init__(
        self,
        *,
        lock,
        is_group_chat: Callable[[str, str], bool],
        group_mode_for_chat: Callable[[str], str],
        thread_subscribers_locked: Callable[[str], tuple[ChatBindingKey, ...]],
        current_interaction_lease_locked: Callable[[str], InteractionLease | None],
        feishu_interaction_holder: Callable[[ChatBindingKey], InteractionLeaseHolder],
    ) -> None:
        self._lock = lock
        self._is_group_chat = is_group_chat
        self._group_mode_for_chat = group_mode_for_chat
        self._thread_subscribers_locked = thread_subscribers_locked
        self._current_interaction_lease_locked = current_interaction_lease_locked
        self._feishu_interaction_holder = feishu_interaction_holder

    @staticmethod
    def write_denied_check(owner_label: str, *, reason_code: str) -> ReasonedCheck:
        return ReasonedCheck.deny(
            reason_code,
            f"当前线程正由{owner_label}执行；本会话可继续查看，但暂时不能写入。待对方执行结束后再试。",
        )

    @classmethod
    def interaction_denied_check(cls, lease: InteractionLease | None) -> ReasonedCheck:
        owner_label = "另一终端"
        if lease is not None and lease.holder.kind == "feishu":
            owner_label = "另一飞书会话"
        return cls.write_denied_check(
            owner_label,
            reason_code=PROMPT_DENIED_BY_INTERACTION_OWNER,
        )

    @classmethod
    def interaction_denied_text(cls, lease: InteractionLease | None) -> str:
        return cls.interaction_denied_check(lease).reason_text

    def all_mode_thread_exclusivity_violation_check(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> ReasonedCheck:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_thread_id or not normalized_chat_id:
            return ReasonedCheck.allow()
        current_mode = str(current_chat_mode or "").strip().lower()
        if not current_mode and self._is_group_chat(normalized_chat_id, message_id):
            current_mode = str(self._group_mode_for_chat(normalized_chat_id) or "").strip().lower()
        with self._lock:
            subscribers = self._thread_subscribers_locked(normalized_thread_id)
        other_chat_ids = sorted({binding[1] for binding in subscribers if binding[1] != normalized_chat_id})
        if current_mode == "all" and other_chat_ids:
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING,
                "当前群聊处于 `all` 模式；该模式下线程不能与其他飞书会话共享。"
                "请先切到 `assistant` 或 `mention-only`，或为本群新建线程。",
            )
        for binding in subscribers:
            if binding[1] == normalized_chat_id:
                continue
            if binding[0] != GROUP_SHARED_BINDING_OWNER_ID:
                continue
            if str(self._group_mode_for_chat(binding[1]) or "").strip().lower() != "all":
                continue
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_OTHER_GROUP_ALL_OWNER,
                "该线程当前已被处于 `all` 模式的其他群聊独占；"
                "请先为本会话新建线程，或让对方切回 `assistant` / `mention-only`。",
            )
        return ReasonedCheck.allow()

    def all_mode_thread_exclusivity_violation(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str:
        return self.all_mode_thread_exclusivity_violation_check(
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode=current_chat_mode,
        ).reason_text

    def validate_group_mode_change(self, chat_id: str, mode: str, *, thread_id: str, message_id: str = "") -> str:
        normalized_mode = str(mode or "").strip().lower()
        normalized_thread_id = str(thread_id or "").strip()
        if normalized_mode != "all" or not normalized_thread_id:
            return ""
        return self.all_mode_thread_exclusivity_violation(
            chat_id,
            normalized_thread_id,
            message_id=message_id,
            current_chat_mode="all",
        )

    def prompt_write_denial_check(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> ReasonedCheck:
        all_mode_exclusivity_violation = self.all_mode_thread_exclusivity_violation_check(
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode=current_chat_mode,
        )
        if not all_mode_exclusivity_violation.allowed:
            return all_mode_exclusivity_violation
        with self._lock:
            interaction_lease = self._current_interaction_lease_locked(thread_id)
            if interaction_lease is not None and not interaction_lease.holder.same_holder(
                self._feishu_interaction_holder(binding)
            ):
                return self.interaction_denied_check(interaction_lease)
        return ReasonedCheck.allow()

    def prompt_write_denial_text(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str:
        return self.prompt_write_denial_check(
            binding,
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode=current_chat_mode,
        ).reason_text
