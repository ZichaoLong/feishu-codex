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

    def test_membership_listener_observes_only_actual_adds_and_removes(self) -> None:
        changed_thread_ids: list[str] = []
        registry = ThreadSubscriptionRegistry(
            membership_changed=changed_thread_ids.append,
        )
        binding_a = ("ou_user", "chat-a")
        binding_b = ("ou_other", "chat-b")

        registry.subscribe(binding_a, "thread-1")
        registry.subscribe(binding_a, "thread-1")
        registry.subscribe(binding_b, "thread-1")
        registry.unsubscribe(("ou_missing", "chat-missing"), "thread-1")
        registry.unsubscribe(binding_a, "thread-1")
        registry.unsubscribe(binding_a, "thread-1")
        registry.unsubscribe(binding_b, "thread-1")
        registry.unsubscribe(binding_b, "thread-1")

        self.assertEqual(changed_thread_ids, ["thread-1"] * 4)

    def test_membership_listener_failure_does_not_rollback_state(self) -> None:
        def fail(_thread_id: str) -> None:
            raise RuntimeError("presentation unavailable")

        registry = ThreadSubscriptionRegistry(membership_changed=fail)
        binding = ("ou_user", "chat-a")

        with self.assertLogs("bot.thread_subscription_registry", level="ERROR"):
            registry.subscribe(binding, "thread-1")
        self.assertEqual(registry.subscribers("thread-1"), (binding,))

        with self.assertLogs("bot.thread_subscription_registry", level="ERROR"):
            registry.unsubscribe(binding, "thread-1")
        self.assertEqual(registry.subscribers("thread-1"), ())

    def test_clear_notifies_each_thread_once_after_the_registry_is_empty(self) -> None:
        observed: list[tuple[str, tuple, tuple]] = []
        fail = False
        registry: ThreadSubscriptionRegistry

        def observe(thread_id: str) -> None:
            observed.append(
                (
                    thread_id,
                    registry.subscribers("thread-1"),
                    registry.subscribers("thread-2"),
                )
            )
            if fail:
                raise RuntimeError("presentation unavailable")

        registry = ThreadSubscriptionRegistry(membership_changed=observe)
        registry.subscribe(("ou_user", "chat-a"), "thread-1")
        registry.subscribe(("ou_other", "chat-b"), "thread-1")
        registry.subscribe(("ou_third", "chat-c"), "thread-2")
        observed.clear()
        fail = True

        with self.assertLogs("bot.thread_subscription_registry", level="ERROR"):
            registry.clear()

        self.assertEqual(
            observed,
            [
                ("thread-1", (), ()),
                ("thread-2", (), ()),
            ],
        )
        self.assertEqual(registry.subscribers("thread-1"), ())
        self.assertEqual(registry.subscribers("thread-2"), ())


if __name__ == "__main__":
    unittest.main()
