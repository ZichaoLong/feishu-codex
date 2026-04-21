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

from bot.cards import CommandResult, build_group_acl_card, build_group_mode_card, make_card_response
from bot.feishu_types import GroupAclSnapshot, MessageContextPayload
from bot.stores.group_chat_store import ACCESS_POLICIES, GROUP_MODES


@dataclass(frozen=True, slots=True)
class GroupDomainPorts:
    get_sender_display_name: Callable[..., str]
    get_message_context: Callable[[str], MessageContextPayload]
    get_group_mode: Callable[[str], str]
    is_group_admin: Callable[[str], bool]
    get_group_acl_snapshot: Callable[[str], GroupAclSnapshot]
    is_group_user_allowed: Callable[[str, str], bool]
    set_group_mode: Callable[[str, str], None]
    set_group_access_policy: Callable[[str, str], None]
    grant_group_members: Callable[[str, list[str]], list[str]]
    revoke_group_members: Callable[[str, list[str]], list[str]]
    extract_non_bot_mentions: Callable[[str], list[dict[str, Any]]]
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

    def _group_command_context(self, message_id: str = "") -> MessageContextPayload:
        """Return message context for a command that has already passed group scope checks."""
        context = self._ports.get_message_context(message_id) if message_id else {}
        if context:
            return context
        return {"chat_type": "group"}

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

    def _group_acl_card(self, chat_id: str, *, open_id: str = "") -> dict:
        snapshot: GroupAclSnapshot = self._ports.get_group_acl_snapshot(chat_id)
        return build_group_acl_card(
            snapshot["access_policy"],
            allowlist_members=self._group_member_labels(snapshot["allowlist"]),
            viewer_allowed=self._ports.is_group_user_allowed(chat_id, open_id),
            can_manage=self._ports.is_group_admin(open_id),
        )

    def handle_groupmode_command(
        self,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        context = self._group_command_context(message_id)
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

    def _acl_target_open_ids(self, message_id: str, raw_arg: str) -> list[str]:
        targets = {
            item["open_id"]
            for item in self._ports.extract_non_bot_mentions(message_id)
            if item.get("open_id")
        }
        for token in str(raw_arg or "").replace(",", " ").split():
            token = token.strip()
            if token and not token.startswith("@"):
                targets.add(token)
        return sorted(targets)

    def handle_acl_command(
        self,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> CommandResult:
        context = self._group_command_context(message_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            return CommandResult(card=self._group_acl_card(chat_id, open_id=sender_open_id))

        cmd, _, rest = arg.partition(" ")
        subcommand = cmd.strip().lower()
        payload = rest.strip()
        if subcommand in {"admin-only", "allowlist", "all-members"}:
            payload = subcommand
            subcommand = "policy"

        if subcommand == "policy":
            policy = payload.strip().lower()
            if policy not in ACCESS_POLICIES:
                return CommandResult(text="用法：`/acl policy <admin-only|allowlist|all-members>`")
            self._ports.set_group_access_policy(chat_id, policy)
            return CommandResult(text=f"已切换群聊授权策略：`{policy}`")

        if subcommand in {"grant", "allow"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                return CommandResult(text="用法：`/acl grant @成员` 或 `/acl grant <open_id>`")
            updated = self._ports.grant_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            return CommandResult(text=f"已授权：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。")

        if subcommand in {"revoke", "remove"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                return CommandResult(text="用法：`/acl revoke @成员` 或 `/acl revoke <open_id>`")
            updated = self._ports.revoke_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            return CommandResult(text=f"已撤销：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。")

        return CommandResult(
            text="用法：`/acl`、`/acl policy <admin-only|allowlist|all-members>`、`/acl grant @成员`、`/acl revoke @成员`"
        )

    def handle_show_group_mode_card_action(
        self,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        if not self._ports.is_group_chat(chat_id, message_id):
            return make_card_response(toast="该命令仅支持群聊使用。", toast_type="warning")
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        return make_card_response(
            card=self._group_mode_card(chat_id, open_id=operator_open_id)
        )

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

    def handle_set_group_acl_policy_action(
        self,
        chat_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in ACCESS_POLICIES:
            return make_card_response(toast="非法群聊授权策略", toast_type="warning")
        if not self._ports.is_group_admin(operator_open_id):
            return make_card_response(toast="仅管理员可调整群聊授权策略。", toast_type="warning")
        self._ports.set_group_access_policy(chat_id, policy)
        return make_card_response(
            card=self._group_acl_card(chat_id, open_id=operator_open_id),
            toast=f"已切换群聊授权策略：{policy}",
            toast_type="success",
        )
