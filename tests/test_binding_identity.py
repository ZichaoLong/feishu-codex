import unittest

from bot.binding_identity import format_binding_id, parse_binding_id
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID


class BindingIdentityTests(unittest.TestCase):
    def test_format_binding_id_rejects_colon_in_component(self) -> None:
        with self.assertRaisesRegex(ValueError, "sender_id 不能包含"):
            format_binding_id(("ou:user", "chat-1"))
        with self.assertRaisesRegex(ValueError, "chat_id 不能包含"):
            format_binding_id((GROUP_SHARED_BINDING_OWNER_ID, "chat:1"))

    def test_parse_binding_id_rejects_colon_in_component(self) -> None:
        with self.assertRaisesRegex(ValueError, "chat_id 不能包含"):
            parse_binding_id("group:chat:1")
        with self.assertRaisesRegex(ValueError, "chat_id 不能包含"):
            parse_binding_id("p2p:ou_user:chat:1")


if __name__ == "__main__":
    unittest.main()
