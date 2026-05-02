import unittest

from bot.codex_group_domain import CodexGroupDomain, GroupDomainPorts


class _GroupPortsStub:
    def __init__(self) -> None:
        self.message_contexts = {"m-group": {"sender_open_id": "ou_admin"}}
        self.group_modes = {"chat-group": "assistant"}
        self.group_activation = {
            "activated": False,
            "activated_by": "",
            "activated_at": 0,
        }
        self.group_chat = True
        self.violation = ""
        self.set_mode_calls: list[tuple[str, str]] = []
        self.activation_calls: list[tuple[str, str]] = []
        self.deactivation_calls: list[str] = []
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

    def get_group_activation_snapshot(self, chat_id: str) -> dict:
        del chat_id
        return dict(self.group_activation)

    def set_group_mode(self, chat_id: str, mode: str) -> None:
        self.group_modes[chat_id] = mode
        self.set_mode_calls.append((chat_id, mode))

    def activate_group_chat(self, chat_id: str, activated_by: str) -> dict:
        self.group_activation = {
            "activated": True,
            "activated_by": activated_by,
            "activated_at": 1712476800000,
        }
        self.activation_calls.append((chat_id, activated_by))
        return dict(self.group_activation)

    def deactivate_group_chat(self, chat_id: str) -> dict:
        self.group_activation = {
            "activated": False,
            "activated_by": "",
            "activated_at": 0,
        }
        self.deactivation_calls.append(chat_id)
        return dict(self.group_activation)

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
            get_group_activation_snapshot=stub.get_group_activation_snapshot,
            set_group_mode=stub.set_group_mode,
            activate_group_chat=stub.activate_group_chat,
            deactivate_group_chat=stub.deactivate_group_chat,
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

    def test_group_mode_command_sets_mode_via_ports(self) -> None:
        stub = _GroupPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_group_mode_command("chat-group", "all", message_id="m-group")

        self.assertEqual(stub.validate_calls, [("chat-group", "all", "m-group")])
        self.assertEqual(stub.set_mode_calls, [("chat-group", "all")])
        self.assertEqual(result.text, "已切换群聊工作态：`all`")

    def test_group_command_activates_group_via_ports(self) -> None:
        stub = _GroupPortsStub()
        domain = _make_domain(stub)

        result = domain.handle_group_command("chat-group", "activate", message_id="m-group")

        self.assertEqual(stub.activation_calls, [("chat-group", "ou_admin")])
        self.assertIn("已激活当前群聊", result.text)

    def test_group_activation_card_action_can_deactivate_group(self) -> None:
        stub = _GroupPortsStub()
        stub.group_activation = {
            "activated": True,
            "activated_by": "ou_admin",
            "activated_at": 1712476800000,
        }
        domain = _make_domain(stub)

        response = self._unpack_response(
            domain.handle_set_group_activation_action(
                "chat-group",
                {"activated": False, "_operator_open_id": "ou_admin"},
            )
        )

        self.assertEqual(stub.deactivation_calls, ["chat-group"])
        self.assertEqual(response["toast"], "已停用当前群聊；非管理员后续将不能继续使用机器人。")
        self.assertEqual(response["toast_type"], "success")


if __name__ == "__main__":
    unittest.main()
