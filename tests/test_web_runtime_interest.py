import unittest

from bot.web_runtime.interest import WebRuntimeInterestRegistry


class WebRuntimeInterestRegistryTests(unittest.TestCase):
    def test_confirmed_interest_owns_clients_outcome_and_epoch_together(self) -> None:
        registry = WebRuntimeInterestRegistry()

        registry.mark_confirmed("thread-1", client_id="tab-1")

        self.assertTrue(registry.has_interest("thread-1"))
        self.assertTrue(registry.has_managed_interest("thread-1"))
        self.assertTrue(registry.subscription_is_current("thread-1"))
        self.assertEqual(
            registry.snapshot("thread-1").desired_client_ids,  # type: ignore[union-attr]
            ("tab-1",),
        )

    def test_unknown_outcome_is_not_a_current_subscription(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")

        registry.mark_unknown("thread-1")

        self.assertTrue(registry.has_interest("thread-1"))
        self.assertTrue(registry.is_unknown("thread-1"))
        self.assertTrue(registry.has_managed_interest("thread-1"))
        self.assertFalse(registry.subscription_is_current("thread-1"))
        self.assertEqual(registry.unknown_thread_ids(), ("thread-1",))

        never_confirmed = WebRuntimeInterestRegistry()
        never_confirmed.mark_unknown("thread-2", client_id="tab-2")
        self.assertFalse(never_confirmed.has_managed_interest("thread-2"))

    def test_backend_generation_invalidates_without_dropping_desired_interest(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")

        registry.backend_disconnected()

        self.assertFalse(registry.subscription_is_current("thread-1"))
        self.assertTrue(registry.has_interest("thread-1"))
        self.assertTrue(registry.has_desired_clients("thread-1"))
        registry.confirm_thread_scoped_notification(
            "thread-1",
            method="turn/started",
        )
        self.assertTrue(registry.subscription_is_current("thread-1"))

    def test_broadcast_notification_cannot_confirm_current_subscription(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")
        registry.backend_disconnected()

        for method in (
            "thread/name/updated",
            "thread/started",
            "thread/status/changed",
            "thread/goal/updated",
            "thread/closed",
            "unknown/future",
        ):
            with self.subTest(method=method):
                self.assertFalse(
                    registry.confirm_thread_scoped_notification(
                        "thread-1",
                        method=method,
                    )
                )
                self.assertFalse(registry.subscription_is_current("thread-1"))

    def test_scoped_delivery_confirms_only_the_exact_managed_thread(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("root", client_id="tab-1")
        registry.mark_confirmed("child")
        registry.backend_disconnected()

        self.assertTrue(
            registry.confirm_thread_scoped_notification(
                "child",
                method="item/started",
            )
        )

        self.assertTrue(registry.subscription_is_current("child"))
        self.assertFalse(registry.subscription_is_current("root"))

    def test_closed_subscription_preserves_desire_history_and_unknown_outcome(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")
        registry.mark_unknown("thread-1")

        self.assertTrue(registry.mark_subscription_absent("thread-1"))

        snapshot = registry.snapshot("thread-1")
        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.ever_confirmed)  # type: ignore[union-attr]
        self.assertEqual(snapshot.desired_client_ids, ("tab-1",))  # type: ignore[union-attr]
        self.assertEqual(snapshot.subscription_epoch, 0)  # type: ignore[union-attr]
        self.assertTrue(registry.is_unknown("thread-1"))
        self.assertFalse(registry.subscription_is_current("thread-1"))

    def test_client_and_interest_cleanup_are_explicitly_distinct(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")
        registry.add_desired_client("thread-1", "tab-2")

        self.assertTrue(registry.remove_desired_client("thread-1", "tab-1"))
        self.assertFalse(registry.remove_desired_client("thread-1", "tab-2"))
        self.assertTrue(registry.has_interest("thread-1"))

        self.assertTrue(registry.forget("thread-1"))
        self.assertIsNone(registry.snapshot("thread-1"))

    def test_client_query_exposes_every_desired_edge_from_the_single_owner(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-3", client_id="tab-1")
        registry.mark_unknown("thread-1", client_id="tab-1")
        registry.mark_confirmed("thread-2", client_id="tab-2")
        registry.add_desired_client("thread-2", "tab-1")

        self.assertEqual(
            registry.desired_thread_ids_for_client("tab-1"),
            ("thread-1", "thread-2", "thread-3"),
        )
        self.assertEqual(
            registry.desired_thread_ids_for_client("tab-2"),
            ("thread-2",),
        )

    def test_client_wide_desired_cleanup_is_idempotent_and_preserves_interest(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-2", client_id="tab-1")
        registry.mark_unknown("thread-1", client_id="tab-1")

        first = registry.remove_desired_client_from_all("tab-1")
        second = registry.remove_desired_client_from_all("tab-1")

        self.assertEqual(first, ("thread-1", "thread-2"))
        self.assertEqual(second, ())
        self.assertEqual(registry.desired_thread_ids_for_client("tab-1"), ())
        self.assertTrue(registry.has_interest("thread-1"))
        self.assertTrue(registry.is_unknown("thread-1"))
        self.assertTrue(registry.has_managed_interest("thread-2"))

    def test_client_wide_cleanup_preserves_exact_target_and_other_clients(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")
        registry.add_desired_client("thread-1", "tab-2")
        registry.mark_confirmed("thread-2", client_id="tab-1")
        registry.mark_confirmed("thread-3", client_id="tab-2")

        removed = registry.remove_desired_client_from_all(
            "tab-1",
            except_thread_id="thread-2",
        )

        self.assertEqual(removed, ("thread-1",))
        self.assertEqual(
            registry.desired_thread_ids_for_client("tab-1"),
            ("thread-2",),
        )
        self.assertEqual(
            registry.desired_thread_ids_for_client("tab-2"),
            ("thread-1", "thread-3"),
        )
        self.assertEqual(
            registry.snapshot("thread-1").desired_client_ids,  # type: ignore[union-attr]
            ("tab-2",),
        )

    def test_except_target_is_subtractive_and_does_not_create_missing_desire(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_confirmed("thread-1", client_id="tab-1")
        registry.mark_confirmed("thread-2", client_id="tab-2")

        removed = registry.remove_desired_client_from_all(
            "tab-1",
            except_thread_id="thread-missing",
        )

        self.assertEqual(removed, ("thread-1",))
        self.assertEqual(registry.desired_thread_ids_for_client("tab-1"), ())
        self.assertFalse(registry.has_interest("thread-missing"))

    def test_client_wide_desired_operations_require_an_exact_client(self) -> None:
        registry = WebRuntimeInterestRegistry()

        with self.assertRaisesRegex(ValueError, "client id"):
            registry.desired_thread_ids_for_client("  ")
        with self.assertRaisesRegex(ValueError, "client id"):
            registry.remove_desired_client_from_all("")

    def test_desired_client_requires_existing_managed_interest(self) -> None:
        registry = WebRuntimeInterestRegistry()

        with self.assertRaisesRegex(RuntimeError, "existing managed interest"):
            registry.add_desired_client("thread-1", "tab-1")

        registry.mark_unknown("thread-1")
        with self.assertRaisesRegex(RuntimeError, "existing managed interest"):
            registry.add_desired_client("thread-1", "tab-1")

        snapshot = registry.snapshot("thread-1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.desired_client_ids, ())  # type: ignore[union-attr]

    def test_unknown_interest_can_be_reconfirmed_by_authoritative_resume(self) -> None:
        registry = WebRuntimeInterestRegistry()
        registry.mark_unknown("thread-1", client_id="tab-1")

        registry.mark_confirmed("thread-1", client_id="tab-1")

        self.assertFalse(registry.is_unknown("thread-1"))
        self.assertTrue(registry.subscription_is_current("thread-1"))

    def test_empty_thread_id_is_rejected(self) -> None:
        registry = WebRuntimeInterestRegistry()

        with self.assertRaises(ValueError):
            registry.mark_unknown("  ")


if __name__ == "__main__":
    unittest.main()
