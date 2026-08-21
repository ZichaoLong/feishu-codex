import pathlib
import tempfile
import unittest

from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.selection_coordinator import (
    WebSelectionAuthorityMismatch,
    WebSelectionCoordinator,
    WebSelectionNotReady,
)


class WebSelectionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.profiles = WebWriterProfileStore(pathlib.Path(self.temp_dir.name))
        self.documents = WebDocumentRegistry(runtime_context_guard=lambda: None)
        self.interest = WebRuntimeInterestRegistry()
        self.coordinator = WebSelectionCoordinator(
            profile_store=self.profiles,
            document_registry=self.documents,
            runtime_interest=self.interest,
        )

    def test_history_requires_durable_and_materialized_same_target(self) -> None:
        self.profiles.update("tab-1", selected_thread_id="thread-1")
        self.documents.materialize_thread("tab-1", "thread-2")

        with self.assertRaises(WebSelectionNotReady):
            self.coordinator.require_history_ready("tab-1", "thread-1")
        with self.assertRaises(WebSelectionNotReady):
            self.coordinator.require_history_ready("tab-1", "thread-2")

        self.documents.materialize_thread("tab-1", "thread-1")
        self.coordinator.require_history_ready("tab-1", "thread-1")

    def test_materialization_cannot_override_durable_selection(self) -> None:
        self.profiles.update("tab-1", selected_thread_id="thread-1")

        with self.assertRaises(WebSelectionAuthorityMismatch):
            self.coordinator.materialize_selected_thread("tab-1", "thread-2")

        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "")

    def test_materialization_removes_all_stray_edges_except_exact_target(self) -> None:
        self.profiles.update("tab-1", selected_thread_id="thread-2")
        self.interest.mark_confirmed("thread-1", client_id="tab-1")
        self.interest.mark_confirmed("thread-2", client_id="tab-1")
        self.interest.mark_unknown("thread-3", client_id="tab-1")

        result = self.coordinator.materialize_selected_thread("tab-1", "thread-2")

        self.assertEqual(result.runtime_cleanup_thread_ids, ("thread-1", "thread-3"))
        self.assertEqual(
            self.interest.desired_thread_ids_for_client("tab-1"),
            ("thread-2",),
        )

    def test_select_thread_persists_before_process_facts_and_projects_scope(self) -> None:
        current = self.profiles.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir="/work/one",
            scope_generation=4,
        )
        self.documents.materialize_thread("tab-1", "thread-3")
        self.interest.mark_confirmed("thread-3", client_id="tab-1")

        selected = self.coordinator.select_thread(
            current,
            "thread-2",
            draft_scope_key="/work/one",
        )

        self.assertTrue(selected.scope_changed)
        self.assertEqual(selected.current.selected_thread_id, "thread-2")
        self.assertEqual(selected.current.scope_generation, 5)
        self.assertEqual(selected.runtime_cleanup_thread_ids, ("thread-3",))
        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "thread-2")
        self.assertEqual(
            selected.project(writer_profile={"selected_thread_id": "thread-2"}),
            {
                "writer_profile": {"selected_thread_id": "thread-2"},
                "scope_changed": True,
                "previous_attachment_scope": "thread:thread-1",
                "current_attachment_scope": "thread:thread-2",
                "previous_scope_generation": 4,
                "current_scope_generation": 5,
                "attachment_scope_disposition": "isolated",
            },
        )

    def test_explicit_draft_and_document_loss_clear_all_document_edges(self) -> None:
        self.profiles.update("tab-1", selected_thread_id="")
        self.documents.materialize_thread("tab-1", "thread-2")
        self.interest.mark_confirmed("thread-1", client_id="tab-1")
        self.interest.mark_confirmed("thread-2", client_id="tab-1")

        cleared = self.coordinator.clear_document_projection("tab-1")

        self.assertEqual(cleared.runtime_cleanup_thread_ids, ("thread-1", "thread-2"))
        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "")
        self.profiles.update("tab-1", selected_thread_id="thread-3")
        self.documents.materialize_thread("tab-1", "thread-3")
        self.interest.mark_confirmed("thread-3", client_id="tab-1")

        lost = self.coordinator.lose_document("tab-1")

        self.assertEqual(lost.runtime_cleanup_thread_ids, ("thread-3",))
        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "")
        self.assertEqual(self.profiles.load("tab-1").selected_thread_id, "thread-3")

    def test_unusable_target_advances_exact_durable_profiles_and_converges_r(self) -> None:
        self.profiles.update(
            "tab-1",
            selected_thread_id="thread-1",
            scope_generation=4,
        )
        self.profiles.update(
            "tab-2",
            selected_thread_id="thread-2",
            scope_generation=8,
        )
        self.documents.materialize_thread("tab-1", "thread-2")
        self.documents.materialize_thread("tab-2", "thread-1")
        self.interest.mark_confirmed("thread-1", client_id="tab-2")
        self.interest.mark_confirmed("thread-2", client_id="tab-1")
        self.interest.mark_unknown("thread-3", client_id="tab-1")

        first = self.coordinator.materialize_cleared_unusable_thread(
            "thread-1",
            self.coordinator.persist_clear_unusable_thread("thread-1"),
        )
        replay = self.coordinator.materialize_cleared_unusable_thread(
            "thread-1",
            self.coordinator.persist_clear_unusable_thread("thread-1"),
        )

        self.assertEqual(len(first.cleared_profiles), 1)
        self.assertEqual(first.cleared_profiles[0].current.client_id, "tab-1")
        self.assertEqual(first.cleared_profiles[0].current.scope_generation, 5)
        self.assertEqual(first.runtime_cleanup_thread_ids, (
            "thread-1",
            "thread-2",
            "thread-3",
        ))
        self.assertEqual(replay.cleared_profiles, ())
        self.assertEqual(replay.runtime_cleanup_thread_ids, ("thread-1",))
        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "thread-2")
        self.assertEqual(self.documents.materialized_thread_id("tab-2"), "")
        self.assertEqual(self.profiles.load("tab-2").selected_thread_id, "thread-2")

    def test_unusable_target_retries_cleanup_after_last_document_edge_was_lost(self) -> None:
        self.interest.mark_unknown("thread-1", client_id="tab-1")
        self.interest.remove_desired_client_from_all("tab-1")

        result = self.coordinator.materialize_cleared_unusable_thread(
            "thread-1",
            self.coordinator.persist_clear_unusable_thread("thread-1"),
        )

        self.assertEqual(result.cleared_profiles, ())
        self.assertEqual(result.runtime_cleanup_thread_ids, ("thread-1",))


if __name__ == "__main__":
    unittest.main()
