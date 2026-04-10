"""
Codex group domain.
"""

from __future__ import annotations

from typing import Any, Protocol

from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTriggerResponse,
)

from bot.cards import build_group_acl_card, build_group_mode_card
from bot.feishu_types import GroupAclSnapshot, MessageContextPayload


class _GroupDomainOwner(Protocol):
    bot: Any

    def _is_group_chat(self, chat_id: str, message_id: str = "") -> bool: ...

    def _reply_text(self, chat_id: str, text: str, *, message_id: str = "") -> None: ...

    def _reply_card(self, chat_id: str, card: dict, *, message_id: str = "") -> None: ...


class CodexGroupDomain:
    def __init__(self, owner: _GroupDomainOwner) -> None:
        self._owner = owner

    def _group_member_label(self, open_id: str) -> str:
        normalized_open_id = str(open_id or "").strip()
        if not normalized_open_id:
            return "unknown"
        display_name = self._owner.bot.get_sender_display_name(
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
        context = self._owner.bot.get_message_context(message_id) if message_id else {}
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
            self._owner.bot.get_group_mode(chat_id),
            can_manage=self._owner.bot.is_group_admin(open_id=open_id),
        )

    def _group_acl_card(self, chat_id: str, *, open_id: str = "") -> dict:
        snapshot: GroupAclSnapshot = self._owner.bot.get_group_acl_snapshot(chat_id)
        return build_group_acl_card(
            snapshot["access_policy"],
            allowlist_members=self._group_member_labels(snapshot["allowlist"]),
            viewer_allowed=self._owner.bot.is_group_user_allowed(chat_id, open_id=open_id),
            can_manage=self._owner.bot.is_group_admin(open_id=open_id),
        )

    def handle_groupmode_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        del sender_id
        context = self._group_command_context(message_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self._owner._reply_card(
                chat_id,
                self._group_mode_card(chat_id, open_id=sender_open_id),
                message_id=message_id,
            )
            return
        mode = self._normalize_group_mode(arg)
        if mode not in {"assistant", "all", "mention_only"}:
            self._owner._reply_text(
                chat_id,
                "群聊工作态仅支持：`assistant`、`all`、`mention-only`",
                message_id=message_id,
            )
            return
        self._owner.bot.set_group_mode(chat_id, mode)
        labels = {
            "assistant": "assistant",
            "all": "all",
            "mention_only": "mention-only",
        }
        self._owner._reply_text(chat_id, f"已切换群聊工作态：`{labels[mode]}`", message_id=message_id)

    def _acl_target_open_ids(self, message_id: str, raw_arg: str) -> list[str]:
        targets = {
            item["open_id"]
            for item in self._owner.bot.extract_non_bot_mentions(message_id)
            if item.get("open_id")
        }
        for token in str(raw_arg or "").replace(",", " ").split():
            token = token.strip()
            if token and not token.startswith("@"):
                targets.add(token)
        return sorted(targets)

    def handle_acl_command(
        self,
        sender_id: str,
        chat_id: str,
        arg: str,
        message_id: str = "",
    ) -> None:
        del sender_id
        context = self._group_command_context(message_id)
        sender_open_id = str(context.get("sender_open_id", "")).strip()
        if not arg:
            self._owner._reply_card(
                chat_id,
                self._group_acl_card(chat_id, open_id=sender_open_id),
                message_id=message_id,
            )
            return

        cmd, _, rest = arg.partition(" ")
        subcommand = cmd.strip().lower()
        payload = rest.strip()
        if subcommand in {"admin-only", "allowlist", "all-members"}:
            payload = subcommand
            subcommand = "policy"

        if subcommand == "policy":
            policy = payload.strip().lower()
            if policy not in {"admin-only", "allowlist", "all-members"}:
                self._owner._reply_text(
                    chat_id,
                    "用法：`/acl policy <admin-only|allowlist|all-members>`",
                    message_id=message_id,
                )
                return
            self._owner.bot.set_group_access_policy(chat_id, policy)
            self._owner._reply_text(chat_id, f"已切换群聊授权策略：`{policy}`", message_id=message_id)
            return

        if subcommand in {"grant", "allow"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self._owner._reply_text(
                    chat_id,
                    "用法：`/acl grant @成员` 或 `/acl grant <open_id>`",
                    message_id=message_id,
                )
                return
            updated = self._owner.bot.grant_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            self._owner._reply_text(
                chat_id,
                f"已授权：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。",
                message_id=message_id,
            )
            return

        if subcommand in {"revoke", "remove"}:
            targets = self._acl_target_open_ids(message_id, payload)
            if not targets:
                self._owner._reply_text(
                    chat_id,
                    "用法：`/acl revoke @成员` 或 `/acl revoke <open_id>`",
                    message_id=message_id,
                )
                return
            updated = self._owner.bot.revoke_group_members(chat_id, targets)
            labels = self._group_member_labels(targets)
            self._owner._reply_text(
                chat_id,
                f"已撤销：{', '.join(labels)}\n当前 allowlist 共 {len(updated)} 人。",
                message_id=message_id,
            )
            return

        self._owner._reply_text(
            chat_id,
            "用法：`/acl`、`/acl policy <admin-only|allowlist|all-members>`、`/acl grant @成员`、`/acl revoke @成员`",
            message_id=message_id,
        )

    def handle_show_group_mode_card_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        if not self._owner._is_group_chat(chat_id, message_id):
            return self._owner.bot.make_card_response(toast="该命令仅支持群聊使用。", toast_type="warning")
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        return self._owner.bot.make_card_response(
            card=self._group_mode_card(chat_id, open_id=operator_open_id)
        )

    def handle_set_group_mode_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del message_id
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        mode = self._normalize_group_mode(str(action_value.get("mode", "")))
        if mode not in {"assistant", "all", "mention_only"}:
            return self._owner.bot.make_card_response(toast="非法群聊工作态", toast_type="warning")
        if not self._owner.bot.is_group_admin(open_id=operator_open_id):
            return self._owner.bot.make_card_response(toast="仅管理员可切换群聊工作态。", toast_type="warning")
        self._owner.bot.set_group_mode(chat_id, mode)
        return self._owner.bot.make_card_response(
            card=self._group_mode_card(chat_id, open_id=operator_open_id),
            toast=f"已切换群聊工作态：{mode}",
            toast_type="success",
        )

    def handle_set_group_acl_policy_action(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        action_value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        del sender_id
        del message_id
        operator_open_id = str(action_value.get("_operator_open_id", "")).strip()
        policy = str(action_value.get("policy", "")).strip().lower()
        if policy not in {"admin-only", "allowlist", "all-members"}:
            return self._owner.bot.make_card_response(toast="非法群聊授权策略", toast_type="warning")
        if not self._owner.bot.is_group_admin(open_id=operator_open_id):
            return self._owner.bot.make_card_response(toast="仅管理员可调整群聊授权策略。", toast_type="warning")
        self._owner.bot.set_group_access_policy(chat_id, policy)
        return self._owner.bot.make_card_response(
            card=self._group_acl_card(chat_id, open_id=operator_open_id),
            toast=f"已切换群聊授权策略：{policy}",
            toast_type="success",
        )
