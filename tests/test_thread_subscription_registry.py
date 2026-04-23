import unittest

from bot.thread_subscription_registry import ThreadSubscriptionRegistry


class ThreadSubscriptionRegistryTests(unittest.TestCase):
    def test_multiple_subscribers_can_share_thread_without_overwriting_each_other(self) -> None:
        registry = ThreadSubscriptionRegistry()

        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.subscribe(("ou_user", "chat-b"), "thread-1")

        self.assertEqual(
            registry.subscribers("thread-1"),
            (("ou_user", "chat-a"), ("ou_user", "chat-b")),
        )

    def test_unsubscribe_keeps_remaining_subscribers(self) -> None:
        registry = ThreadSubscriptionRegistry()
        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.subscribe(("ou_user", "chat-b"), "thread-1")

        orphaned = registry.unsubscribe(("ou_user", "chat-a"), "thread-1")

        self.assertFalse(orphaned)
        self.assertEqual(registry.subscribers("thread-1"), (("ou_user", "chat-b"),))

    def test_unsubscribe_reports_orphaned_thread(self) -> None:
        registry = ThreadSubscriptionRegistry()
        registry.subscribe(("ou_user", "chat-a"), "thread-1")

        orphaned = registry.unsubscribe(("ou_user", "chat-a"), "thread-1")

        self.assertTrue(orphaned)
        self.assertEqual(registry.subscribers("thread-1"), ())


if __name__ == "__main__":
    unittest.main()
