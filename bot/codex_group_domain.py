"""
Codex group domain.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import CommandResult, build_group_activation_card, build_group_mode_card, make_card_response
from bot.feishu_types import GroupActivationSnapshot, MessageContextPayload
from bot.stores.group_chat_store import GROUP_MODES


@dataclass(frozen=True, slots=True)
class GroupDomainPorts:
    get_sender_display_name: Callable[..., str]
    get_message_context: Callable[[str], MessageContextPayload]
    get_group_mode: Callable[[str], str]
    is_group_admin: Callable[[str], bool]
    get_group_activation_snapshot: Callable[[str], GroupActivationSnapshot]
    set_group_mode: Callable[[str, str], None]
    activate_group_chat: Callable[[str, str], GroupActivationSnapshot]
    deactivate_group_chat: Callable[[str], GroupActivationSnapshot]
    is_group_chat: Callable[[str, str], bool]
    validate_group_mode_change: Callable[[str, str, str], str]


class CodexGroupDomain:
    def __init__(self, *, ports: GroupDomainPorts) -> None:
        self._ports = ports

    def _group_member_label(self, open_id: str) -> str:
        normalized_open_id = str(open_id or "").strip()
        if not normalized_open_id:
            return "unknown"
        display_name = self._ports.get_sender_display_name(
            open_id=normalized_open_id,
            sender_type="user",
        )
        normalized_name = str(display_name or "").strip()
        if normalized_name and normalized_name not in {normalized_open_id, normalized_open_id[:8]}:
            return normalized_name
        return normalized_open_id

    def _group_member_labels(self, open_ids: list[str] | set[str]) -> list[str]:
        normalized_open_ids = sorted({str(item).strip() for item in open_ids if str(item).strip()})
        return [self._group_member_label(open_id) for open_id in normalized_open_ids]

    def _group_command_context(self, message_id: str = "", sender_open_id: str = "") -> MessageContextPayload:
        """Return message context for a command that has already passed group scope checks."""
        context = self._ports.get_message_context(message_id) if message_id else {}
        if context:
            if sender_open_id and not str(context.get("sender_open_id", "")).strip():
                context["sender_open_id"] = str(sender_open_id).strip()
            return context
        fallback_context: MessageContextPayload = {"chat_type": "group"}
        if sender_open_id:
            fallback_context["sender_open_id"] = str(sender_open_id).strip()
        return fallback_context

    @staticmethod
    def _normalize_group_mode(mode: str) -> str:
        normalized = str(mode or "").strip().lower().replace("-", "_")
        if normalized == "mention":
            return "mention_only"
        return normalized

    def _group_mode_card(self, chat_id: str, *, open_id: str = "") -> dict:
        return build_group_mode_card(
            self._ports.get_group_mode(chat_id),
            can_manage=self._ports.is_group_admin(open_id),
        )

    def _group_activation_card(self, chat_id: str, *, open_id: str = "") -> dict:
        snapshot: GroupActivationSnapshot = self._ports.get_group_activation_snapshot(chat_id)
        activated_by_open_id = str(snapshot["activated_by"] or "").strip()
        activated_by_label = self._group_member_label(activated_by_open_id) if activated_by_open_id else ""
        return build_group_activation_card(
            activated=bool(snapshot["activated"]),
            activated_by=activated_by_label,
            activated_at=int(snapshot["activated_at"]),
            can_manage=self._ports.is_group_admin(open_id),
        )

    def handle_groupmode_command(
        self,
        chat_id: str,
        arg: str,
        sender_open_id: str = "",
        message_id: str = "",
    ) -> CommandResult:
        context = self._group_command_context(message_id, sender_open_id=sender_open_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            return CommandResult(card=self._group_mode_card(chat_id, open_id=sender_open_id))
        mode = self._normalize_group_mode(arg)
        if mode not in GROUP_MODES:
            return CommandResult(text="群聊工作态仅支持：`assistant`、`all`、`mention-only`")
        violation = self._ports.validate_group_mode_change(chat_id, mode, message_id)
        if violation:
            return CommandResult(text=violation)
        self._ports.set_group_mode(chat_id, mode)
        labels = {
            "assistant": "assistant",
            "all": "all",
            "mention_only": "mention-only",
        }
        return CommandResult(text=f"已切换群聊工作态：`{labels[mode]}`")

    def handle_group_command(
        self,
        chat_id: str,
        arg: str,
        sender_open_id: str = "",
        message_id: str = "",
    ) -> CommandResult:
        context = self._group_command_context(message_id, sender_open_id=sender_open_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            return CommandResult(card=self._group_activation_card(chat_id, open_id=sender_open_id))

        subcommand = str(arg or "").strip().lower()
        if subcommand == "activate":
            self._ports.activate_group_chat(chat_id, sender_open_id)
            return CommandResult(
                text=(
                    "已激活当前群聊；群成员现在可正常对话，并处理自己发起 turn 的审批或补充输入。"
                    "管理员仍可兜底处理。"
                )
            )
        if subcommand in {"deactivate", "disable"}:
            self._ports.deactivate_group_chat(chat_id)
            return CommandResult(
                text="已停用当前群聊；非管理员后续将不能继续使用机器人。管理员仍可继续初始化与管理。"
            )
        return CommandResult(text="用法：`/group`、`/group activate`、`/group deactivate`")

    def handle_set_group_mode_action(
        self,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        mode = self._normalize_group_mode(str(action_value.get("mode", "")))
        if mode not in GROUP_MODES:
            return make_card_response(toast="非法群聊工作态", toast_type="warning")
        if not self._ports.is_group_admin(operator_open_id):
            return make_card_response(toast="仅管理员可切换群聊工作态。", toast_type="warning")
        violation = self._ports.validate_group_mode_change(chat_id, mode, message_id)
        if violation:
            return make_card_response(toast=violation, toast_type="warning")
        self._ports.set_group_mode(chat_id, mode)
        return make_card_response(
            card=self._group_mode_card(chat_id, open_id=operator_open_id),
            toast=f"已切换群聊工作态：{mode}",
            toast_type="success",
        )

    def handle_set_group_activation_action(
        self,
        chat_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        activated = bool(action_value.get("activated"))
        if not self._ports.is_group_admin(operator_open_id):
            return make_card_response(toast="仅管理员可调整群聊授权状态。", toast_type="warning")
        if activated:
            self._ports.activate_group_chat(chat_id, operator_open_id)
            toast = "已激活当前群聊；成员可处理自己发起 turn 的审批或补充输入。"
        else:
            self._ports.deactivate_group_chat(chat_id)
            toast = "已停用当前群聊；非管理员后续将不能继续使用机器人。"
        return make_card_response(
            card=self._group_activation_card(chat_id, open_id=operator_open_id),
            toast=toast,
            toast_type="success",
        )
