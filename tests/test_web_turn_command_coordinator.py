from __future__ import annotations

import unittest
from unittest.mock import Mock

from bot.adapters.base import (
    ThreadResumePage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.stores.interaction_lease_store import InteractionLease, InteractionLeaseHolder
from bot.web_runtime.contract import (
    WebConnectedWriterReceipt,
    WebRuntimeError,
    WebTurnSubmissionReceipt,
)
from bot.web_runtime.turn_command_coordinator import (
    WebTurnCommandCoordinator,
    WebTurnCommandPorts,
)


class WebTurnCommandCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = Mock()
        self.documents = Mock()
        self.documents.assert_runtime_context = Mock()
        self.documents.is_connected.return_value = True
        self.operations = Mock()
        self.thread_open = Mock()
        self.direct_targets = Mock()
        self.goal_policy = Mock()
        self.read_model = Mock()
        self.runtime_interest = Mock()
        self.projection = Mock()
        self.ports = WebTurnCommandPorts(
            compact_thread=Mock(return_value=None),
            start_review=Mock(return_value={"turn": {"id": "review-1"}}),
        )
        self.snapshot = ThreadSnapshot(
            ThreadSummary(
                thread_id="thread-1",
                cwd="/work",
                name="",
                preview="",
                created_at=1,
                updated_at=1,
                source="cli",
                status="idle",
            )
        )
        self.direct_targets.read.return_value = self.snapshot
        self.resume_page = ThreadResumePage(
            snapshot=self.snapshot,
            initial_turns_page=ThreadTurnsPage(
                turns=[{"id": "old-turn", "status": "completed", "items": []}]
            ),
        )
        self.thread_open.resume_and_commit_web_interest.return_value = self.resume_page
        holder = InteractionLeaseHolder(kind="web", holder_id="web:tab-1")
        blank = InteractionLease(
            thread_id="thread-1",
            holder=holder,
            lease_id="lease-1",
            updated_at=1.0,
        )
        active = InteractionLease(
            thread_id="thread-1",
            holder=holder,
            lease_id="lease-1",
            updated_at=2.0,
            turn_id="review-1",
        )
        self.submission = WebTurnSubmissionReceipt(
            client_id="tab-1",
            root_thread_id="thread-1",
            lease=blank,
        )
        self.writer = WebConnectedWriterReceipt(
            client_id="tab-1",
            root_thread_id="thread-1",
            holder=holder,
            lease=active,
        )
        self.operations.acquire_exclusive_turn_submission.return_value = self.submission
        self.operations.activate_turn_submission.return_value = self.writer
        self.operations.release_exact_blank_turn_submission.return_value = True
        self.operations.is_unknown_mutation_error.side_effect = lambda exc: isinstance(
            exc, TimeoutError
        )
        self.operations.is_resume_uncertain_error.side_effect = lambda exc: isinstance(
            exc, TimeoutError
        )
        self.operations.is_resume_outcome_unknown.side_effect = lambda exc: isinstance(
            exc, TimeoutError
        )
        self.coordinator = WebTurnCommandCoordinator(
            documents=self.documents,
            operations=self.operations,
            thread_open=self.thread_open,
            direct_targets=self.direct_targets,
            goal_policy=self.goal_policy,
            read_model=self.read_model,
            runtime_interest=self.runtime_interest,
            projection=self.projection,
            ports=self.ports,
            runtime_context_guard=self.guard,
        )

    def test_runtime_guard_runs_before_command_dependencies(self) -> None:
        self.guard.side_effect = RuntimeError("wrong runtime")

        with self.assertRaisesRegex(RuntimeError, "wrong runtime"):
            self.coordinator.compact_thread("tab-1", "thread-1")

        self.documents.assert_runtime_context.assert_not_called()
        self.ports.compact_thread.assert_not_called()

    def test_compact_runs_one_exclusive_resume_and_keeps_blank_for_lifecycle(self) -> None:
        result = self.coordinator.compact_thread("tab-1", "thread-1")

        self.assertEqual(result["action"], "compact")
        self.assertEqual(result["turn_id"], "")
        self.operations.admit_explicit_web_effect.assert_called_once_with(
            "tab-1",
            "thread-1",
            operation="compact",
        )
        self.thread_open.resume_and_commit_web_interest.assert_called_once()
        self.read_model.replace_turns.assert_called_once_with(
            "thread-1",
            self.resume_page.initial_turns_page.turns,
        )
        self.ports.compact_thread.assert_called_once_with("thread-1")
        self.operations.activate_turn_submission.assert_not_called()
        self.operations.release_exact_blank_turn_submission.assert_not_called()

    def test_review_normalizes_target_and_activates_exact_returned_turn(self) -> None:
        result = self.coordinator.start_review(
            "tab-1",
            "thread-1",
            target={"type": "commit", "sha": "abc123", "title": "Fix"},
        )

        self.assertEqual(result["turn_id"], "review-1")
        self.ports.start_review.assert_called_once_with(
            "thread-1",
            target={"type": "commit", "sha": "abc123", "title": "Fix"},
            delivery="inline",
        )
        self.operations.activate_turn_submission.assert_called_once_with(
            self.submission,
            "review-1",
        )

    def test_active_or_incomplete_review_is_rejected_before_admission(self) -> None:
        self.snapshot.summary.status = "active"
        with self.assertRaises(WebRuntimeError) as active:
            self.coordinator.compact_thread("tab-1", "thread-1")
        self.assertEqual(active.exception.code, "thread_active")
        self.operations.acquire_exclusive_turn_submission.assert_not_called()

        self.snapshot.summary.status = "idle"
        with self.assertRaises(WebRuntimeError) as invalid:
            self.coordinator.start_review(
                "tab-1",
                "thread-1",
                target={"type": "commit", "sha": ""},
            )
        self.assertEqual(invalid.exception.code, "invalid_review_target")

    def test_known_start_failure_releases_only_the_blank_submission(self) -> None:
        self.ports.start_review.side_effect = WebRuntimeError(
            "rejected",
            code="review_rejected",
            status=409,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.start_review(
                "tab-1",
                "thread-1",
                target={"type": "uncommittedChanges"},
            )

        self.assertEqual(caught.exception.code, "review_rejected")
        self.operations.release_exact_blank_turn_submission.assert_called_once_with(
            self.submission,
            reason="web_review_submission_failed",
        )

    def test_unknown_start_keeps_submission_and_never_retries(self) -> None:
        self.ports.start_review.side_effect = TimeoutError("lost response")

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.start_review(
                "tab-1",
                "thread-1",
                target={"type": "uncommittedChanges"},
            )

        self.assertEqual(caught.exception.code, "turn_submission_unknown")
        self.assertEqual(self.ports.start_review.call_count, 1)
        self.operations.release_exact_blank_turn_submission.assert_not_called()

    def test_unknown_resume_releases_temporary_writer_before_reporting(self) -> None:
        self.thread_open.resume_and_commit_web_interest.side_effect = TimeoutError(
            "resume response lost"
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.compact_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "runtime_resume_unknown")
        self.runtime_interest.mark_unknown.assert_called_once_with(
            "thread-1",
            client_id="tab-1",
        )
        self.operations.release_exact_blank_turn_submission.assert_called_once_with(
            self.submission,
            reason="web_compact_resume_unknown",
        )
        self.ports.compact_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
