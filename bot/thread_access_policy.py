from __future__ import annotations

import os
from typing import Callable, TypeAlias

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.reason_codes import (
    PROMPT_DENIED_BY_GROUP_ALL_MODE_SHARING,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    PROMPT_DENIED_BY_OTHER_GROUP_ALL_OWNER,
    PROMPT_DENIED_BY_RUNNING_TURN,
    ReasonedCheck,
)
from bot.process_utils import process_identity
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
            current_mode = (
                str(self._group_mode_for_chat(normalized_chat_id) or "").strip().lower()
            )
        with self._lock:
            subscribers = self._thread_subscribers_locked(normalized_thread_id)
        other_chat_ids = sorted(
            {binding[1] for binding in subscribers if binding[1] != normalized_chat_id}
        )
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
            if (
                str(self._group_mode_for_chat(binding[1]) or "").strip().lower()
                != "all"
            ):
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

    def validate_group_mode_change(
        self, chat_id: str, mode: str, *, thread_id: str, message_id: str = ""
    ) -> str:
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
        all_mode_exclusivity_violation = (
            self.all_mode_thread_exclusivity_violation_check(
                chat_id,
                thread_id,
                message_id=message_id,
                current_chat_mode=current_chat_mode,
            )
        )
        if not all_mode_exclusivity_violation.allowed:
            return all_mode_exclusivity_violation
        with self._lock:
            interaction_lease = self._current_interaction_lease_locked(thread_id)
            if (
                interaction_lease is not None
                and not interaction_lease.holder.same_holder(
                    self._feishu_interaction_holder(binding)
                )
            ):
                return self.interaction_denied_check(interaction_lease)
        return ReasonedCheck.allow()

    def prompt_queue_admission_check(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        current_turn_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
        has_exact_queue_continuity: bool = False,
    ) -> ReasonedCheck:
        """Authorize one exact same-binding Feishu FIFO append.

        The ordinary writer rule remains authoritative.  Its exceptions are a
        current-process Web/fcodex lease for the exact active turn, an exact
        no-lease autonomous turn already mirrored into this binding, or
        existing exact FIFO continuity. Callers hold the shared runtime lock
        across this typed decision and enqueue.
        """

        exclusivity = self.all_mode_thread_exclusivity_violation_check(
            chat_id,
            thread_id,
            message_id=message_id,
            current_chat_mode=current_chat_mode,
        )
        if not exclusivity.allowed:
            return exclusivity
        if has_exact_queue_continuity:
            return ReasonedCheck.allow()
        with self._lock:
            interaction_lease = self._current_interaction_lease_locked(thread_id)
            if interaction_lease is None:
                if str(current_turn_id or "").strip():
                    return ReasonedCheck.allow()
                return ReasonedCheck.deny(
                    PROMPT_DENIED_BY_RUNNING_TURN,
                    "当前线程正在执行，但尚未取得可核对的 turn 标识；请稍后再试。",
                )
            if interaction_lease.holder.same_holder(
                self._feishu_interaction_holder(binding)
            ):
                return ReasonedCheck.allow()
            if self._is_exact_current_process_local_turn(
                interaction_lease,
                thread_id=thread_id,
                turn_id=current_turn_id,
            ):
                return ReasonedCheck.allow()
            return self.interaction_denied_check(interaction_lease)

    def current_process_local_turn_id(self, thread_id: str) -> str:
        """Return an exact Web/fcodex turn before its Feishu mirror arrives."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ""
        with self._lock:
            lease = self._current_interaction_lease_locked(normalized_thread_id)
            if lease is None or not self._is_exact_current_process_local_turn(
                lease,
                thread_id=normalized_thread_id,
                turn_id=lease.turn_id,
            ):
                return ""
            return str(lease.turn_id or "").strip()

    @staticmethod
    def _is_exact_current_process_local_turn(
        lease: InteractionLease,
        *,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        holder = lease.holder
        current_pid = os.getpid()
        recorded_identity = str(holder.owner_process_identity or "").strip()
        current_identity = process_identity(current_pid)
        return bool(
            normalized_thread_id
            and normalized_turn_id
            and lease.thread_id == normalized_thread_id
            and lease.turn_id == normalized_turn_id
            and holder.kind in {"web", "fcodex"}
            and holder.owner_pid == current_pid
            and recorded_identity
            and current_identity
            and recorded_identity == current_identity
        )

    def external_control_write_denial_check(
        self,
        thread_id: str,
        writer_holder: InteractionLeaseHolder | None = None,
    ) -> ReasonedCheck:
        """Authorize only the active turn's exact writer.

        ``focusctl`` and other local service-control callers share the
        deployment trust domain, but are not a continuation of an existing
        Web, Feishu, or fcodex writer. An active-turn lease therefore blocks
        their mutation instead of becoming an implicit takeover. An internal
        frontend may pass the exact holder it has just admitted.

        Callers establish a direct root target before invoking this method;
        this policy deliberately does not infer parent aliases from a stale
        observation cache.
        """

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ReasonedCheck.allow()
        with self._lock:
            interaction_lease = self._current_interaction_lease_locked(
                normalized_thread_id
            )
        if interaction_lease is not None and (
            writer_holder is None
            or not interaction_lease.holder.same_holder(writer_holder)
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
