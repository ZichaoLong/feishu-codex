from __future__ import annotations

import unittest
from unittest.mock import Mock

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
    WebDirectThreadTargetPorts,
    WebDirectThreadTargetVerifier,
    WebDirectThreadTargetVerifierPorts,
)
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.writer_workspace_coordinator import WebWorkspaceConvergenceOutcome


def _summary(
    thread_id: str = "thread-1",
    *,
    subagent_kind: str | None = None,
) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        cwd="/workspace",
        name="Demo",
        preview="",
        created_at=1,
        updated_at=2,
        source="appServer",
        status="idle",
        subagent_kind=subagent_kind,
    )


class WebDirectThreadTargetCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = Mock()
        self.read_thread = Mock(return_value=ThreadSnapshot(summary=_summary()))
        self.remember = Mock()
        self.persist_clear = Mock(return_value=())
        self.materialize_clear = Mock(
            return_value=WebWorkspaceConvergenceOutcome(
                runtime_cleanup_thread_ids=("thread-1",),
            )
        )
        self.settle = Mock()
        self.delete_scope = Mock()
        self.verifier = WebDirectThreadTargetVerifier(
            ports=WebDirectThreadTargetVerifierPorts(
                read_thread=self.read_thread,
            ),
            runtime_context_guard=self.guard,
        )
        self.owner = WebDirectThreadTargetCoordinator(
            verifier=self.verifier,
            ports=WebDirectThreadTargetPorts(
                remember_direct_thread_summary=self.remember,
                persist_clear_unusable_thread=self.persist_clear,
                materialize_cleared_unusable_thread=self.materialize_clear,
                settle_runtime_cleanup_candidates=self.settle,
                delete_thread_scope=self.delete_scope,
            ),
            runtime_context_guard=self.guard,
        )

    def test_valid_target_is_remembered_only_after_authoritative_proof(self):
        snapshot = self.owner.read("thread-1", operation="open")

        self.assertIs(snapshot, self.read_thread.return_value)
        self.read_thread.assert_called_once_with("thread-1", False)
        self.remember.assert_called_once_with(snapshot.summary)
        self.persist_clear.assert_not_called()

    def test_mismatched_response_fails_closed_without_local_cleanup(self):
        self.read_thread.return_value = ThreadSnapshot(summary=_summary("thread-2"))

        with self.assertRaises(WebRuntimeError) as caught:
            self.owner.read("thread-1", operation="open")

        self.assertEqual(caught.exception.code, "thread_target_unverified")
        self.assertEqual(caught.exception.status, 503)
        self.remember.assert_not_called()
        self.persist_clear.assert_not_called()
        self.delete_scope.assert_not_called()

    def test_verifier_rejects_child_without_running_convergence_effects(self):
        self.read_thread.return_value = ThreadSnapshot(
            summary=_summary(subagent_kind="threadSpawn")
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.verifier.read("thread-1", operation="settle goal")

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        self.persist_clear.assert_not_called()
        self.settle.assert_not_called()
        self.delete_scope.assert_not_called()
        self.remember.assert_not_called()

    def test_thread_spawn_rejection_converges_selection_and_runtime_cleanup(self):
        self.read_thread.return_value = ThreadSnapshot(
            summary=_summary(subagent_kind="threadSpawn")
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.owner.read("thread-1", operation="open")

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        self.assertEqual(caught.exception.status, 409)
        self.persist_clear.assert_called_once_with("thread-1")
        self.materialize_clear.assert_called_once_with(
            "thread-1",
            (),
            reason="web_direct_target_selection_cleared",
        )
        self.settle.assert_called_once_with(("thread-1",))
        self.delete_scope.assert_called_once_with("thread-1")
        self.remember.assert_not_called()

    def test_physical_attachment_cleanup_failure_does_not_restore_child_target(self):
        self.read_thread.return_value = ThreadSnapshot(
            summary=_summary(subagent_kind="threadSpawn")
        )
        self.delete_scope.side_effect = RuntimeError("metadata unavailable")

        with self.assertLogs(
            "bot.web_runtime.direct_thread_target_coordinator",
            level="ERROR",
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.owner.read("thread-1", operation="open")

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        self.persist_clear.assert_called_once_with("thread-1")
        self.materialize_clear.assert_called_once()
        self.settle.assert_called_once_with(("thread-1",))

    def test_runtime_guard_runs_before_any_dependency(self):
        self.guard.side_effect = RuntimeError("outside RuntimeLoop")

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.owner.read("thread-1", operation="open")

        self.read_thread.assert_not_called()
        self.persist_clear.assert_not_called()

    def test_clear_guard_runs_before_workspace_cleanup(self):
        self.guard.side_effect = RuntimeError("outside RuntimeLoop")

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.owner.clear_unusable_thread("thread-1", reason="unavailable")

        self.persist_clear.assert_not_called()
        self.settle.assert_not_called()

    def test_external_cleanup_preparation_does_not_enter_runtime_context(self):
        self.guard.side_effect = RuntimeError("outside RuntimeLoop")

        receipt = self.owner.prepare_unusable_thread_cleanup(
            "thread-1",
            reason="web_direct_target_selection_cleared",
            delete_attachment_scope=True,
        )

        self.assertEqual(receipt.thread_id, "thread-1")
        self.guard.assert_not_called()
        self.persist_clear.assert_called_once_with("thread-1")
        self.delete_scope.assert_called_once_with("thread-1")


if __name__ == "__main__":
    unittest.main()
