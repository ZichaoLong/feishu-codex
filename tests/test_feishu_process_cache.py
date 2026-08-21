import ast
import pathlib
import unittest

from bot.feishu_process_cache import FeishuProcessCache


class FeishuProcessCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_000.0
        self.cache = FeishuProcessCache(clock=lambda: self.now)

    def test_message_context_expires_and_returns_a_copy(self) -> None:
        payload = {"thread_id": "thread-1"}
        self.cache.remember_message_context("message-1", payload)
        payload["thread_id"] = "changed"

        self.assertEqual(
            self.cache.get_message_context("message-1"),
            {"thread_id": "thread-1"},
        )

        self.now += 601
        self.assertEqual(self.cache.get_message_context("message-1"), {})

    def test_chat_type_and_display_name_expire_independently(self) -> None:
        self.cache.remember_chat_type("chat-1", "group")
        self.cache.remember_chat_display_name("chat-1", "Project Group")

        self.now += 6 * 3600 + 1
        self.assertEqual(self.cache.lookup_chat_display_name("chat-1"), "")
        self.assertEqual(self.cache.lookup_chat_type("chat-1"), "group")

        self.now += 18 * 3600
        self.assertEqual(self.cache.lookup_chat_type("chat-1"), "")

    def test_reserved_execution_card_is_claimed_once_or_expires(self) -> None:
        self.cache.reserve_execution_card("message-1", "card-1")
        self.assertEqual(
            self.cache.claim_reserved_execution_card("message-1"),
            "card-1",
        )
        self.assertEqual(self.cache.claim_reserved_execution_card("message-1"), "")

        self.cache.reserve_execution_card("message-2", "card-2")
        self.now += 601
        self.assertEqual(self.cache.claim_reserved_execution_card("message-2"), "")

    def test_message_dedup_is_process_local_and_bounded_by_ttl(self) -> None:
        self.assertFalse(self.cache.is_duplicate_message("message-1"))
        self.assertTrue(self.cache.is_duplicate_message("message-1"))

        self.now += 301
        self.assertFalse(self.cache.is_duplicate_message("message-2"))
        self.assertFalse(self.cache.is_duplicate_message("message-1"))

    def test_sender_name_cache_and_warning_throttle_expire(self) -> None:
        self.cache.remember_sender_name(
            "open-1",
            "user-1",
            value="Alice",
        )
        self.assertEqual(self.cache.lookup_sender_name("open-1"), "Alice")
        self.assertEqual(self.cache.lookup_sender_name("user-1"), "Alice")
        self.assertTrue(
            self.cache.should_emit_sender_name_warning("open-1", "timeout")
        )
        self.assertFalse(
            self.cache.should_emit_sender_name_warning("open-1", "timeout")
        )

        self.now += 301
        self.assertTrue(
            self.cache.should_emit_sender_name_warning("open-1", "timeout")
        )
        self.now += 6 * 3600
        self.assertEqual(self.cache.lookup_sender_name("open-1"), "")

    def test_forget_chat_removes_only_chat_scoped_cache_facts(self) -> None:
        for chat_id in ("chat-1", "chat-2"):
            self.cache.remember_chat_type(chat_id, "group")
            self.cache.remember_chat_display_name(chat_id, chat_id)
            self.cache.remember_message_context(
                f"message-{chat_id}",
                {"chat_id": chat_id},
            )

        self.cache.forget_chat("chat-1")

        self.assertEqual(self.cache.lookup_chat_type("chat-1"), "")
        self.assertEqual(self.cache.lookup_chat_display_name("chat-1"), "")
        self.assertEqual(
            self.cache.get_message_context("message-chat-1"),
            {},
        )
        self.assertEqual(self.cache.lookup_chat_type("chat-2"), "group")
        self.assertEqual(
            self.cache.get_message_context("message-chat-2")["chat_id"],
            "chat-2",
        )

    def test_feishu_bot_cache_facades_only_delegate(self) -> None:
        path = pathlib.Path(__file__).parents[1] / "bot" / "feishu_bot.py"
        module = ast.parse(path.read_text(encoding="utf-8"))
        bot = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and node.name == "FeishuBot"
        )
        facade_names = {
            "claim_reserved_execution_card",
            "get_message_context",
            "lookup_cached_sender_name",
            "lookup_chat_display_name",
            "lookup_chat_type",
            "remember_chat_display_name",
            "remember_chat_type",
            "reserve_execution_card",
        }
        methods = {
            node.name: node
            for node in bot.body
            if isinstance(node, ast.FunctionDef) and node.name in facade_names
        }

        self.assertEqual(set(methods), facade_names)
        for name, method in methods.items():
            with self.subTest(name=name):
                self.assertEqual(len(method.body), 1)
                self.assertIn("_process_cache", ast.unparse(method))


if __name__ == "__main__":
    unittest.main()
