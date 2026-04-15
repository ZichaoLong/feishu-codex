import unittest

from bot.thread_lease_registry import ThreadLeaseRegistry


class ThreadLeaseRegistryTests(unittest.TestCase):
    def test_multiple_subscribers_can_share_thread_without_overwriting_each_other(self) -> None:
        registry = ThreadLeaseRegistry()

        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.subscribe(("ou_user", "chat-b"), "thread-1")

        self.assertEqual(
            registry.subscribers("thread-1"),
            (("ou_user", "chat-a"), ("ou_user", "chat-b")),
        )
        self.assertIsNone(registry.lease_owner("thread-1"))

    def test_write_lease_is_single_owner_until_released(self) -> None:
        registry = ThreadLeaseRegistry()
        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.subscribe(("ou_user", "chat-b"), "thread-1")

        first = registry.acquire_write_lease(("ou_user", "chat-a"), "thread-1")
        second = registry.acquire_write_lease(("ou_user", "chat-b"), "thread-1")

        self.assertTrue(first.granted)
        self.assertFalse(second.granted)
        self.assertEqual(second.owner, ("ou_user", "chat-a"))

        self.assertTrue(registry.release_write_lease(("ou_user", "chat-a"), "thread-1"))
        self.assertIsNone(registry.lease_owner("thread-1"))

        third = registry.acquire_write_lease(("ou_user", "chat-b"), "thread-1")
        self.assertTrue(third.granted)
        self.assertEqual(registry.lease_owner("thread-1"), ("ou_user", "chat-b"))

    def test_unsubscribe_releases_owner_and_reports_orphaned_thread(self) -> None:
        registry = ThreadLeaseRegistry()
        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.acquire_write_lease(("ou_user", "chat-a"), "thread-1")

        result = registry.unsubscribe(("ou_user", "chat-a"), "thread-1")

        self.assertTrue(result.removed)
        self.assertTrue(result.write_lease_released)
        self.assertTrue(result.thread_orphaned)
        self.assertEqual(registry.subscribers("thread-1"), ())
        self.assertIsNone(registry.lease_owner("thread-1"))


if __name__ == "__main__":
    unittest.main()
