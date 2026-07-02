import unittest

from bot.card_text_projection import (
    CardTextProjection,
    TERMINAL_RESULT_SOURCE_CARD_DEGRADED,
    TERMINAL_RESULT_SOURCE_CARD_LEGACY,
    TERMINAL_RESULT_CARD_MARKER,
    can_render_terminal_result_card,
    project_interactive_card_text,
    terminal_result_checksum,
)
from bot.cards import build_execution_card, build_terminal_result_card
from bot.execution_transcript import ExecutionReplySegment


class CardTextProjectionTests(unittest.TestCase):
    def test_legacy_terminal_result_card_projects_final_reply_text_as_non_authoritative(self) -> None:
        projection = project_interactive_card_text(build_terminal_result_card("最终答复"))

        self.assertIsInstance(projection, CardTextProjection)
        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_LEGACY)
        self.assertEqual(projection.final_reply_text, "最终答复")
        self.assertEqual(projection.text, "最终答复")
        self.assertIn("Codex", projection.visible_text)
        self.assertNotIn("Codex", projection.text)

    def test_terminal_result_card_normalizes_markdown_links_to_visible_urls(self) -> None:
        projection = project_interactive_card_text(
            build_terminal_result_card(
                "- [示例地图链接](https://maps.example.invalid/shanghai/live)"
            )
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(
            projection.final_reply_text,
            "- [示例地图链接](https://maps.example.invalid/shanghai/live)",
        )
        self.assertIn("[示例地图链接](https://maps.example.invalid/shanghai/live)", projection.visible_text)

    def test_terminal_result_card_keeps_markdown_structure_in_authoritative_text(self) -> None:
        projection = project_interactive_card_text(
            build_terminal_result_card("# 总结\n\n## 下一步\n\n- 事项一\n\n> 注意")
        )

        self.assertIn("# 总结", projection.visible_text)
        self.assertIn("## 下一步", projection.visible_text)
        self.assertEqual(
            projection.final_reply_text,
            "# 总结\n\n## 下一步\n\n- 事项一\n\n> 注意",
        )

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

    def test_minimal_terminal_execution_card_projects_placeholder_text(self) -> None:
        projection = project_interactive_card_text(build_execution_card("", [], running=False))

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertIn("Codex", projection.text)
        self.assertIn("执行过程", projection.text)
        self.assertIn("无", projection.text)

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

    def test_ordinary_card_with_marker_like_text_is_not_promoted_to_authoritative_result(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "示例卡片"},
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "普通说明：`<final_reply_text>demo</final_reply_text>`",
                    }
                ],
            }
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("示例卡片", projection.text)
        self.assertIn("<final_reply_text>demo</final_reply_text>", projection.text)

    def test_terminal_result_card_budget_is_fail_closed_on_marker_collision(self) -> None:
        self.assertFalse(
            can_render_terminal_result_card(
                f"包含{TERMINAL_RESULT_CARD_MARKER}隐藏标记",
                char_limit=1000,
            )
        )

    def test_terminal_result_card_budget_is_fail_closed_on_embedded_image_markdown(self) -> None:
        self.assertFalse(
            can_render_terminal_result_card(
                "![示意图](/tmp/phase1_report_diagram.png)",
                char_limit=1000,
            )
        )

    def test_terminal_result_card_without_authoritative_block_fails_closed(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Codex"},
                    "template": "green",
                },
                "elements": [
                    {"tag": "markdown", "content": "这里只剩普通展示文本"},
                ],
            }
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_text, "")
        self.assertIn("Codex", projection.text)
        self.assertIn("这里只剩普通展示文本", projection.visible_text)

    def test_terminal_result_card_requires_green_template(self) -> None:
        projection = project_interactive_card_text(
            {
                "header": {
                    "title": {"tag": "plain_text", "content": "Codex"},
                    "template": "blue",
                },
                "elements": [
                    {"tag": "markdown", "content": f"foo{TERMINAL_RESULT_CARD_MARKER}"},
                ],
            }
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_text, "")
        self.assertEqual(projection.text, "Codex\n\nfoo")

    def test_terminal_result_card_supports_json2_body_elements_shape(self) -> None:
        card = build_terminal_result_card("# 标题")
        projection = project_interactive_card_text(
            {
                "header": card["header"],
                "body": {"elements": card["body"]["elements"]},
            }
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_text, "# 标题")
        self.assertEqual(projection.text, "# 标题")

    def test_terminal_result_card_with_result_id_projects_as_degraded_until_store_resolution(self) -> None:
        checksum = terminal_result_checksum("最终答复")
        projection = project_interactive_card_text(
            build_terminal_result_card(
                "最终答复",
                terminal_result_id="0123456789abcdef0123456789abcdef",
                checksum=checksum,
            )
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_DEGRADED)
        self.assertEqual(projection.final_reply_text, "最终答复")
        self.assertEqual(projection.terminal_result_id, "0123456789abcdef0123456789abcdef")
        self.assertEqual(projection.terminal_result_checksum, checksum[:16])

    def test_terminal_result_store_miss_projects_feishu_safe_card_body_not_raw_text(self) -> None:
        raw_text = "1. **明确一次性任务**\n   用精确时间："
        projection = project_interactive_card_text(
            build_terminal_result_card(
                raw_text,
                terminal_result_id="0123456789abcdef0123456789abcdef",
                checksum=terminal_result_checksum(raw_text),
            )
        )

        self.assertFalse(projection.has_authoritative_final_reply)
        self.assertEqual(projection.final_reply_source, TERMINAL_RESULT_SOURCE_CARD_DEGRADED)
        self.assertEqual(
            projection.final_reply_text,
            "1. **明确一次性任务**<br>\n   用精确时间：",
        )
        self.assertNotEqual(projection.final_reply_text, raw_text)

    def test_terminal_result_card_normalizes_indented_fenced_code_blocks_for_feishu(self) -> None:
        card = build_terminal_result_card(
            "- 检查参数\n"
            "  ```python\n"
            "  {\"open_timeout\": ..., \"max_size\": None, \"proxy\": None}\n"
            "  ```\n"
            "继续说明"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("- 检查参数\n\n```python\n", content)
        self.assertIn('{"open_timeout": ..., "max_size": None, "proxy": None}\n', content)
        self.assertIn("```\n\n继续说明", content)

    def test_terminal_result_card_hardens_nested_list_order_for_feishu(self) -> None:
        card = build_terminal_result_card(
            "2. xxxx：\n"
            "   - yyyy\n"
            "\n"
            "   zzzz\n"
            "3. next"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("2. xxxx：<br>\n   - yyyy\n\nzzzz\n3. next", content)

    def test_terminal_result_card_keeps_shell_glob_inside_normalized_code_block(self) -> None:
        card = build_terminal_result_card(
            "命令：\n"
            "  ```bash\n"
            "  ssh bot@localhost 'bash -lc \"grep -n \\\"proxy.: None\\\" "
            "/home/bot/.local/share/feishu-codex/.venv/lib/python*/site-packages/bot/codex_protocol/client.py\"'\n"
            "  ```"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("```bash\nssh bot@localhost", content)
        self.assertIn('\\"proxy.: None\\"', content)
        self.assertIn("python*/site-packages", content)
        self.assertNotIn("  ssh bot@localhost", content)

    def test_terminal_result_card_upgrades_markdown_fence_wrapping_nested_fences(self) -> None:
        card = build_terminal_result_card(
            "示例：\n"
            "```markdown\n"
            "- Python 参数检查：\n"
            "\n"
            "  ```python\n"
            "  {\"open_timeout\": ..., \"max_size\": None, \"proxy\": None}\n"
            "  ```\n"
            "```\n"
            "\n"
            "后续命令：\n"
            "```bash\n"
            "echo after\n"
            "```"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("````markdown\n", content)
        self.assertIn("  ```python\n", content)
        self.assertIn("  ```\n", content)
        self.assertIn("````\n\n后续命令", content)
        self.assertIn("```bash\necho after\n```", content)

    def test_terminal_result_card_upgrades_markdown_fence_wrapping_bare_nested_fence(self) -> None:
        card = build_terminal_result_card(
            "```markdown\n"
            "```\n"
            "hello\n"
            "```\n"
            "```\n"
            "after"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("````markdown\n```\nhello\n```\n````\n\nafter", content)

    def test_terminal_result_card_does_not_swallow_following_bare_fence_block(self) -> None:
        card = build_terminal_result_card(
            "```markdown\n"
            "content\n"
            "```\n"
            "after\n"
            "```\n"
            "later\n"
            "```"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("```markdown\ncontent\n```", content)
        self.assertIn("after\n\n```\nlater\n```", content)
        self.assertNotIn("````markdown\ncontent\n```\nafter", content)

    def test_terminal_result_card_treats_spaced_fence_info_as_nested_opener(self) -> None:
        card = build_terminal_result_card(
            "```text\n"
            "``` python\n"
            "print(1)\n"
            "```\n"
            "```\n"
            "\n"
            "after"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("````text\n``` python", content)
        self.assertIn("print(1)\n```\n````\n\nafter", content)

    def test_terminal_result_card_upgrades_text_fence_wrapping_nested_fences(self) -> None:
        card = build_terminal_result_card(
            "示例投影会从容易错配的：\n"
            "\n"
            "```text\n"
            "```markdown\n"
            "...\n"
            "  ```python\n"
            "  ...\n"
            "  ```\n"
            "```\n"
            "```\n"
            "\n"
            "变成：\n"
            "\n"
            "````text\n"
            "````markdown\n"
            "...\n"
            "  ```python\n"
            "  ...\n"
            "  ```\n"
            "````\n"
            "````\n"
            "\n"
            "预期效果是：后续普通段落不应进入代码块。"
        )

        content = card["body"]["elements"][-1]["content"]

        self.assertIn("````text\n```markdown", content)
        self.assertIn("````\n\n变成：", content)
        self.assertIn("`````text\n````markdown", content)
        self.assertIn("`````\n\n预期效果是：", content)

    def test_terminal_result_card_separates_marker_from_closing_code_fence(self) -> None:
        card = build_terminal_result_card("```bash\necho ok\n```")
        content = card["body"]["elements"][-1]["content"]

        self.assertIn(f"```\n", content)
        self.assertIn(TERMINAL_RESULT_CARD_MARKER, content)
        self.assertNotIn(f"```{TERMINAL_RESULT_CARD_MARKER}", content)
