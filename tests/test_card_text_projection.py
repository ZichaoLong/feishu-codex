import unittest

from bot.card_text_projection import (
    CardTextProjection,
    can_render_terminal_result_card,
    project_interactive_card_text,
)
from bot.cards import build_execution_card, build_terminal_result_card
from bot.execution_transcript import ExecutionReplySegment


class CardTextProjectionTests(unittest.TestCase):
    def test_terminal_result_card_projects_authoritative_final_reply_text(self) -> None:
        projection = project_interactive_card_text(build_terminal_result_card("最终答复"))

        self.assertIsInstance(projection, CardTextProjection)
        self.assertTrue(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_text, "最终答复")
        self.assertEqual(projection.text, "最终答复")
        self.assertIn("Codex 最终结果", projection.visible_text)

    def test_execution_card_projects_visible_text_best_effort(self) -> None:
        projection = project_interactive_card_text(
            build_execution_card(
                "命令输出",
                [ExecutionReplySegment("assistant", "阶段回复")],
                running=False,
            )
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertIn("Codex", projection.text)
        self.assertIn("执行过程", projection.text)
        self.assertIn("命令输出", projection.text)
        self.assertIn("回复", projection.text)
        self.assertIn("阶段回复", projection.text)

    def test_ordinary_card_ignores_button_labels_but_keeps_visible_text_blocks(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "外部卡片"},
                },
                "elements": [
                    {"tag": "markdown", "content": "这里是正文"},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "不应进入投影"},
                            }
                        ],
                    },
                ],
            }
        )

        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("外部卡片", projection.text)
        self.assertIn("这里是正文", projection.text)
        self.assertNotIn("不应进入投影", projection.text)

    def test_terminal_result_card_budget_is_fail_closed_on_marker_collision(self) -> None:
        self.assertFalse(
            can_render_terminal_result_card(
                "包含 <final_reply_text> 标记",
                char_limit=1000,
            )
        )
