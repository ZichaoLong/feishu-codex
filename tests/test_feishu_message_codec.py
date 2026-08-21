import ast
import pathlib
import unittest

from bot.card_text_projection import terminal_result_checksum
from bot.cards import build_terminal_result_card
from bot.feishu_message_codec import (
    FeishuMessageCodec,
    FeishuMessageCodecPorts,
)


class FeishuMessageCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_cards: dict[str, dict] = {}
        self.remembered_names: list[tuple[str, str]] = []
        self.events: list[tuple[str, dict]] = []
        self.trigger_open_ids = {"ou-bot", "ou-trigger"}
        self.codec = FeishuMessageCodec(
            FeishuMessageCodecPorts(
                load_raw_card_content=lambda message_id: dict(
                    self.raw_cards.get(message_id, {})
                ),
                resolve_sender_name=lambda open_id: f"name:{open_id}",
                remember_sender_name=lambda key, value: (
                    self.remembered_names.append((key, value))
                ),
                configured_trigger_open_ids=lambda: set(
                    self.trigger_open_ids
                ),
                log_card_ingress_event=lambda event, fields: (
                    self.events.append((event, dict(fields)))
                ),
            )
        )

    def test_extract_post_text_preserves_paragraph_breaks(self) -> None:
        text = self.codec.extract_text(
            "post",
            {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "第一段"}],
                    [],
                    [
                        {"tag": "text", "text": "- "},
                        {"tag": "text", "text": "第二段"},
                    ],
                ],
            },
        )

        self.assertEqual(text, "第一段\n\n- 第二段")

    def test_normalize_mentions_removes_triggers_and_keeps_members(self) -> None:
        normalized = self.codec.normalize_mentions(
            "@_user_1 请和 @_user_2 一起看",
            [
                {
                    "key": "@_user_1",
                    "open_id": "ou-trigger",
                    "name": "Trigger",
                },
                {
                    "key": "@_user_2",
                    "open_id": "ou-other",
                    "name": "Alice",
                },
            ],
        )

        self.assertEqual(normalized, "请和 @Alice 一起看")

    def test_raw_card_precedes_best_effort_event_projection(self) -> None:
        self.raw_cards["message-1"] = build_terminal_result_card("权威终态")

        result = self.codec.read_interactive_message(
            "message-1",
            content_dict=build_terminal_result_card("事件投影"),
        )

        self.assertEqual(result.text, "权威终态")
        self.assertEqual(result.card_kind, "terminal")
        self.assertFalse(result.has_authoritative_text)
        self.assertEqual(self.events[-1][1]["path"], "raw_card_direct")

    def test_best_effort_projection_is_used_when_raw_card_is_missing(self) -> None:
        result = self.codec.read_interactive_message(
            "message-1",
            content_dict=build_terminal_result_card("事件投影"),
        )

        self.assertEqual(result.text, "事件投影")
        self.assertEqual(self.events[-1][1]["path"], "best_effort_projection")

    def test_terminal_result_resolver_restores_authoritative_text(self) -> None:
        result_id = "0123456789abcdef0123456789abcdef"
        original = "| 证据 |\n|---|\n| `tool_use` |"
        checksum = terminal_result_checksum(original)
        forwarded = build_terminal_result_card(
            "| 证据 |\n| -------- |\n|  |",
            terminal_result_id=result_id,
            checksum=checksum,
        )
        self.codec.set_terminal_result_text_resolver(
            lambda projection: original
            if projection.terminal_result_id == result_id
            and projection.terminal_result_checksum == checksum[:16]
            else ""
        )

        result = self.codec.read_interactive_message(
            "message-1",
            content_dict=forwarded,
        )

        self.assertEqual(result.text, original)
        self.assertTrue(result.has_authoritative_text)
        self.assertEqual(result.text_source, "store")

    def test_share_user_uses_ports_without_owning_sdk_client(self) -> None:
        text = self.codec.render_message_text(
            "share_user",
            {"user_id": "ou-user"},
        )

        self.assertEqual(text, "[个人名片] name:ou-user")
        self.assertEqual(
            self.remembered_names,
            [("ou-user", "name:ou-user")],
        )

    def test_feishu_bot_does_not_reown_codec_implementation(self) -> None:
        path = pathlib.Path(__file__).parents[1] / "bot" / "feishu_bot.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        bot = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "FeishuBot"
        )
        methods = {
            node.name: node
            for node in bot.body
            if isinstance(node, ast.FunctionDef)
        }
        forbidden = {
            "_attachment_message_name",
            "_attachment_resource_key",
            "_extract_text",
            "_interactive_card_kind",
            "_mention_payload",
            "_mention_payloads",
            "_normalize_mentions",
            "_render_message_text",
            "_resolve_terminal_result_projection",
        }

        self.assertEqual(set(methods) & forbidden, set())
        self.assertEqual(len(methods["read_interactive_message"].body), 1)
        self.assertIn(
            "_message_codec",
            ast.unparse(methods["read_interactive_message"]),
        )

    def test_codec_and_cache_do_not_import_feishu_sdk(self) -> None:
        root = pathlib.Path(__file__).parents[1] / "bot"
        for filename in ("feishu_message_codec.py", "feishu_process_cache.py"):
            module = ast.parse((root / filename).read_text(encoding="utf-8"))
            imported_roots = {
                alias.name.split(".", 1)[0]
                for node in module.body
                if isinstance(node, (ast.Import, ast.ImportFrom))
                for alias in node.names
            }
            with self.subTest(filename=filename):
                self.assertNotIn("lark_oapi", imported_roots)


if __name__ == "__main__":
    unittest.main()
