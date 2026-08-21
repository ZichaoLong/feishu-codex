from __future__ import annotations

import unittest
from unittest.mock import Mock

from bot.adapters.base import ThreadGoalSummary
from bot.codex_protocol.client import CodexRpcError
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy, WebGoalResumePorts
from bot.web_runtime.contract import WebRuntimeError


def _goal(status: str) -> ThreadGoalSummary:
    return ThreadGoalSummary(
        thread_id="thread-1",
        objective="Finish safely",
        status=status,
    )


class WebGoalResumePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = Mock()
        self.get_goal = Mock(return_value=None)
        self.policy = WebGoalResumePolicy(
            ports=WebGoalResumePorts(get_thread_goal=self.get_goal),
            runtime_context_guard=self.guard,
        )

    def test_disabled_goal_feature_normalizes_to_no_goal(self):
        self.get_goal.side_effect = CodexRpcError(
            "thread/goal/get",
            {"message": "goals feature is disabled"},
        )

        self.assertIsNone(self.policy.read("thread-1"))

    def test_new_resume_rejects_active_and_future_statuses_before_transport(self):
        for status in ("active", "futureContinuationStatus"):
            with self.subTest(status=status):
                self.get_goal.return_value = _goal(status)
                with self.assertRaises(WebRuntimeError) as caught:
                    self.policy.require_safe_for_new_resume(
                        "thread-1",
                        operation="prompt",
                    )
                self.assertEqual(
                    caught.exception.code,
                    "goal_continuation_requires_resolution",
                )

    def test_post_resume_noncontinuing_projection_is_returned(self):
        self.get_goal.return_value = _goal("paused")

        result = self.policy.check_post_resume("thread-1", operation="prompt")

        self.assertEqual(result and result.status, "paused")

    def test_post_resume_continuing_goal_rejects_without_creating_owner_state(self):
        self.get_goal.return_value = _goal("active")

        with self.assertRaises(WebRuntimeError) as caught:
            self.policy.check_post_resume("thread-1", operation="review")

        self.assertEqual(
            caught.exception.code,
            "goal_continuation_requires_resolution",
        )

    def test_runtime_guard_runs_before_goal_query(self):
        self.guard.side_effect = RuntimeError("outside RuntimeLoop")

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.policy.read("thread-1")

        self.get_goal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
