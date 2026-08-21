from dataclasses import FrozenInstanceError
import unittest

from bot.web_runtime.document_registry import (
    InvalidWebDocumentId,
    InvalidWebDocumentIntent,
    InvalidWebDocumentThreadId,
    StaleWebDocumentIntent,
    WebDocumentNotConnected,
    WebDocumentRegistry,
)


class WebDocumentRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.in_runtime = True
        self.guard_calls = 0

        def require_runtime() -> None:
            self.guard_calls += 1
            if not self.in_runtime:
                raise RuntimeError("outside RuntimeLoop")

        self.registry = WebDocumentRegistry(
            runtime_context_guard=require_runtime,
        )

    def test_requires_callable_runtime_context_guard(self):
        with self.assertRaises(TypeError):
            WebDocumentRegistry(runtime_context_guard=None)  # type: ignore[arg-type]

    def test_wrong_runtime_context_rejects_reads_and_mutations_without_state_change(self):
        self.in_runtime = False

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.registry.assert_runtime_context()
        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.registry.mark_connected("document-1")
        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.registry.snapshot("document-1")

        self.in_runtime = True
        self.assertIsNone(self.registry.snapshot("document-1"))
        self.assertEqual(self.guard_calls, 4)

    def test_connection_transitions_preserve_only_the_intended_continuity(self):
        connected = self.registry.mark_connected("document-1")
        self.registry.materialize_thread("document-1", "thread-1")
        self.registry.accept_intent("document-1", 7)

        transport_lost = self.registry.mark_transport_disconnected("document-1")

        self.assertEqual(connected.outcome, "changed")
        self.assertTrue(connected.current.connected)
        self.assertEqual(transport_lost.outcome, "changed")
        self.assertFalse(transport_lost.current.connected)
        self.assertEqual(transport_lost.current.materialized_thread_id, "thread-1")
        self.assertEqual(transport_lost.current.latest_intent_generation, 7)
        with self.assertRaises(WebDocumentNotConnected):
            self.registry.require_connected("document-1")

        reconnected = self.registry.mark_connected("document-1")
        document_lost = self.registry.mark_document_lost("document-1")

        self.assertTrue(reconnected.current.connected)
        self.assertEqual(document_lost.outcome, "changed")
        self.assertFalse(document_lost.current.connected)
        self.assertEqual(document_lost.previous.materialized_thread_id, "thread-1")
        self.assertEqual(document_lost.current.materialized_thread_id, "")
        self.assertEqual(document_lost.current.latest_intent_generation, 7)
        retained_floor = self.registry.snapshot("document-1")
        self.assertIsNotNone(retained_floor)
        self.assertEqual(retained_floor and retained_floor.latest_intent_generation, 7)

    def test_transport_and_document_disconnect_are_idempotent_for_missing_document(self):
        transport = self.registry.mark_transport_disconnected("document-1")
        lost = self.registry.mark_document_lost("document-1")

        self.assertEqual(transport.outcome, "missing")
        self.assertEqual(lost.outcome, "missing")

    def test_document_reissue_revokes_continuity_but_retains_intent_floor(self):
        self.assertEqual(self.registry.intent_generation_floor("missing"), 0)
        self.registry.mark_connected("document-1")
        self.registry.materialize_thread("document-1", "thread-1")
        self.registry.accept_intent("document-1", 7)

        reissued = self.registry.mark_document_reissued("document-1")

        self.assertEqual(reissued.outcome, "changed")
        self.assertTrue(reissued.previous.connected)
        self.assertEqual(reissued.previous.materialized_thread_id, "thread-1")
        self.assertFalse(reissued.current.connected)
        self.assertEqual(reissued.current.materialized_thread_id, "")
        self.assertEqual(reissued.current.latest_intent_generation, 7)
        self.assertEqual(self.registry.intent_generation_floor("document-1"), 7)

        reconnected = self.registry.mark_connected("document-1")
        self.assertTrue(reconnected.current.connected)
        self.assertEqual(reconnected.current.materialized_thread_id, "")
        self.assertEqual(reconnected.current.latest_intent_generation, 7)

    def test_client_inventory_includes_connected_drafts_and_observers(self):
        self.registry.mark_connected("connected-draft")
        self.registry.mark_connected("observer")
        self.registry.materialize_thread("observer", "thread-1")
        self.registry.mark_transport_disconnected("observer")

        inventory = self.registry.client_ids()

        self.assertEqual(inventory, ("connected-draft", "observer"))
        self.assertIsInstance(inventory, tuple)
        self.assertIsNone(self.registry.snapshot("document-1"))

    def test_materialize_and_forget_require_the_exact_thread(self):
        first = self.registry.materialize_thread("document-1", "thread-1")
        replacement = self.registry.materialize_thread("document-1", "thread-2")

        stale_forget = self.registry.forget_materialized_thread_if_matches(
            "document-1",
            "thread-1",
        )

        self.assertEqual(first.previous.materialized_thread_id, "")
        self.assertEqual(first.current.materialized_thread_id, "thread-1")
        self.assertEqual(replacement.previous.materialized_thread_id, "thread-1")
        self.assertEqual(replacement.current.materialized_thread_id, "thread-2")
        self.assertEqual(stale_forget.outcome, "mismatch")
        self.assertEqual(
            self.registry.materialized_thread_id("document-1"),
            "thread-2",
        )

        forgotten = self.registry.forget_materialized_thread_if_matches(
            "document-1",
            "thread-2",
        )
        missing = self.registry.forget_materialized_thread_if_matches(
            "document-1",
            "thread-2",
        )

        self.assertEqual(forgotten.outcome, "changed")
        self.assertEqual(forgotten.previous.materialized_thread_id, "thread-2")
        self.assertEqual(forgotten.current.materialized_thread_id, "")
        self.assertEqual(missing.outcome, "missing")
        self.assertIsNone(self.registry.snapshot("document-1"))

    def test_bulk_forget_changes_only_exact_materializations(self):
        self.registry.mark_connected("document-1")
        self.registry.materialize_thread("document-1", "thread-1")
        self.registry.materialize_thread("document-2", "thread-1")
        self.registry.materialize_thread("document-3", "thread-2")

        changes = self.registry.forget_materialized_thread_for_all("thread-1")

        self.assertEqual(
            tuple(change.previous.client_id for change in changes),
            ("document-1", "document-2"),
        )
        self.assertEqual(self.registry.materialized_thread_id("document-1"), "")
        self.assertEqual(self.registry.materialized_thread_id("document-2"), "")
        self.assertEqual(
            self.registry.materialized_thread_id("document-3"),
            "thread-2",
        )
        self.assertTrue(self.registry.is_connected("document-1"))
        self.assertEqual(self.registry.materialized_client_ids(), ("document-3",))

    def test_intent_generation_is_monotonic_per_document(self):
        zero = self.registry.accept_intent("document-1", 0)
        self.assertFalse(zero.advanced)
        self.assertIsNone(self.registry.snapshot("document-1"))

        first = self.registry.accept_intent("document-1", 4)
        replay = self.registry.accept_intent("document-1", 4)

        self.assertTrue(first.advanced)
        self.assertEqual(first.previous_generation, 0)
        self.assertFalse(replay.advanced)
        self.assertEqual(replay.latest_generation, 4)

        with self.assertRaises(StaleWebDocumentIntent) as caught:
            self.registry.accept_intent("document-1", 3)
        self.assertEqual(caught.exception.client_id, "document-1")
        self.assertEqual(caught.exception.requested_generation, 3)
        self.assertEqual(caught.exception.latest_generation, 4)

        other = self.registry.accept_intent("document-2", 1)
        advanced = self.registry.accept_intent("document-1", 5)
        self.assertEqual(other.latest_generation, 1)
        self.assertEqual(advanced.latest_generation, 5)

    def test_document_loss_keeps_stale_intent_protection_across_reconnect(self):
        self.registry.mark_connected("document-1")
        self.registry.accept_intent("document-1", 9)
        self.registry.mark_document_lost("document-1")
        self.registry.mark_connected("document-1")

        with self.assertRaises(StaleWebDocumentIntent):
            self.registry.accept_intent("document-1", 8)
        snapshot = self.registry.snapshot("document-1")
        self.assertIsNotNone(snapshot)
        self.assertEqual(
            snapshot and snapshot.latest_intent_generation,
            9,
        )

    def test_snapshots_and_mutation_receipts_are_detached_and_immutable(self):
        first_mutation = self.registry.mark_connected("document-1")
        before = self.registry.snapshot("document-1")
        self.assertIsNotNone(before)

        self.registry.materialize_thread("document-1", "thread-1")
        after = self.registry.snapshot("document-1")

        self.assertEqual(before and before.materialized_thread_id, "")
        self.assertEqual(after and after.materialized_thread_id, "thread-1")
        self.assertEqual(first_mutation.current.materialized_thread_id, "")
        with self.assertRaises((FrozenInstanceError, AttributeError)):
            before.connected = False  # type: ignore[misc,union-attr]

        cleared = self.registry.clear()
        self.assertEqual(len(cleared), 1)
        self.assertEqual(cleared[0].materialized_thread_id, "thread-1")
        self.assertIsNone(self.registry.snapshot("document-1"))
        self.assertEqual(after and after.materialized_thread_id, "thread-1")

    def test_invalid_ids_threads_and_intents_fail_before_mutation(self):
        with self.assertRaises(InvalidWebDocumentId):
            self.registry.mark_connected("")
        with self.assertRaises(InvalidWebDocumentThreadId):
            self.registry.materialize_thread("document-1", "")
        with self.assertRaises(InvalidWebDocumentIntent):
            self.registry.accept_intent("document-1", "not-an-int")
        with self.assertRaises(InvalidWebDocumentIntent):
            self.registry.accept_intent("document-1", -1)

        self.assertIsNone(self.registry.snapshot("document-1"))


if __name__ == "__main__":
    unittest.main()
