import unittest

from bot.feishu_command_syntax import feishu_visible_command_syntax


class FeishuCommandSyntaxTests(unittest.TestCase):
    def test_converts_angle_placeholders_to_visible_brackets(self) -> None:
        self.assertEqual(
            feishu_visible_command_syntax("fcodex resume <thread_id|thread_name>"),
            "fcodex resume 〈thread_id|thread_name〉",
        )

    def test_leaves_non_placeholder_text_unchanged(self) -> None:
        self.assertEqual(
            feishu_visible_command_syntax("/archive [thread_id|thread_name]"),
            "/archive [thread_id|thread_name]",
        )
