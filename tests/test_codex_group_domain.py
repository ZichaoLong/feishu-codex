import unittest

from bot.codex_group_domain import CodexGroupDomain, GroupDomainPorts


class _GroupPortsStub:
    def __init__(self) -> None:
        self.message_contexts = {"m-group": {"sender_open_id": "ou_admin"}}
        self.group_modes = {"chat-group": "assistant"}
        self.group_acl = {
            "access_policy": "admin-only",
            "allowlist": [],
        }
        self.group_chat = True
        self.violation = ""
        self.set_mode_calls: list[tuple[str, str]] = []
        self.validate_calls: list[tuple[str, str, str]] = []

    def get_sender_display_name(self, *, open_id: str, sender_type: str) -> str:
        del sender_type
        return {"ou_admin": "Alice"}.get(open_id, open_id)

    def get_message_context(self, message_id: str) -> dict:
        return dict(self.message_contexts.get(message_id, {}))

    def get_group_mode(self, chat_id: str) -> str:
        return self.group_modes.get(chat_id, "assistant")

    def is_group_admin(self, open_id: str) -> bool:
        return open_id == "ou_admin"

    def get_group_acl_snapshot(self, chat_id: str) -> dict:
        del chat_id
        return dict(self.group_acl)

    def is_group_user_allowed(self, chat_id: str, open_id: str) -> bool:
        del chat_id, open_id
        return True

    def set_group_mode(self, chat_id: str, mode: str) -> None:
        self.group_modes[chat_id] = mode
        self.set_mode_calls.append((chat_id, mode))

    def set_group_access_policy(self, chat_id: str, policy: str) -> None:
        del chat_id
        self.group_acl["access_policy"] = policy

    def grant_group_members(self, chat_id: str, open_ids: list[str]) -> list[str]:
        del chat_id
        return list(open_ids)

    def revoke_group_members(self, chat_id: str, open_ids: list[str]) -> list[str]:
        del chat_id
        return []

    def extract_non_bot_mentions(self, message_id: str) -> list[dict]:
        del message_id
        return []

    def is_group_chat(self, chat_id: str, message_id: str) -> bool:
        del chat_id, message_id
        return self.group_chat

    def validate_group_mode_change(self, chat_id: str, mode: str, message_id: str) -> str:
        self.validate_calls.append((chat_id, mode, message_id))
        return self.violation


def _make_domain(stub: _GroupPortsStub) -> CodexGroupDomain:
    return CodexGroupDomain(
        ports=GroupDomainPorts(
            get_sender_display_name=stub.get_sender_display_name,
            get_message_context=stub.get_message_context,
            get_group_mode=stub.get_group_mode,
            is_group_admin=stub.is_group_admin,
            get_group_acl_snapshot=stub.get_group_acl_snapshot,
            is_group_user_allowed=stub.is_group_user_allowed,
            set_group_mode=stub.set_group_mode,
            set_group_access_policy=stub.set_group_access_policy,
            grant_group_members=stub.grant_group_members,
            revoke_group_members=stub.revoke_group_members,
            extract_non_bot_mentions=stub.extract_non_bot_mentions,
            is_group_chat=stub.is_group_chat,
            validate_group_mode_change=stub.validate_group_mode_change,
        )
    )


class CodexGroupDomainTests(unittest.TestCase):
    @staticmethod
    def _unpack_response(response) -> dict:
        result: dict = {}
        if response.toast is not None:
            result["toast"] = response.toast.content
            result["toast_type"] = response.toast.type
        if response.card is not None:
            result["card"] = response.card.data
        return result

    def test_groupmode_command_sets_mode_via_ports(self) -> None:
        stub = _GroupPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_groupmode_command("chat-group", "all", message_id="m-group")

        self.assertEqual(stub.validate_calls, [("chat-group", "all", "m-group")])
        self.assertEqual(stub.set_mode_calls, [("chat-group", "all")])
        self.assertEqual(result.text, "已切换群聊工作态：`all`")

    def test_show_group_mode_card_action_rejects_non_group_chat(self) -> None:
        stub = _GroupPortsStub()
        stub.group_chat = False
        domain = _make_domain(stub)

        response = self._unpack_response(
            domain.handle_show_group_mode_card_action(
                "chat-p2p",
                "m-group",
                {"_operator_open_id": "ou_admin"},
            )
        )

        self.assertEqual(response["toast"], "该命令仅支持群聊使用。")
        self.assertEqual(response["toast_type"], "warning")


if __name__ == "__main__":
    unittest.main()
