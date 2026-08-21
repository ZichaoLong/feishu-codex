from __future__ import annotations

import unittest

from bot.goal_continuation_policy import (
    REVIEWED_NON_CONTINUING_GOAL_STATUSES,
    goal_status_may_continue,
    is_reviewed_non_continuing_goal_status,
    normalize_goal_status,
)


class GoalContinuationPolicyTests(unittest.TestCase):
    def test_reviewed_non_continuing_catalog_is_closed_and_exact(self) -> None:
        self.assertEqual(
            REVIEWED_NON_CONTINUING_GOAL_STATUSES,
            {
                "paused",
                "blocked",
                "usageLimited",
                "budgetLimited",
                "complete",
            },
        )
        for status in REVIEWED_NON_CONTINUING_GOAL_STATUSES:
            self.assertTrue(is_reviewed_non_continuing_goal_status(status))
            self.assertFalse(goal_status_may_continue(status))

    def test_unknown_empty_and_active_statuses_fail_closed(self) -> None:
        for status in (None, "", "active", "futureStatus", object()):
            self.assertFalse(is_reviewed_non_continuing_goal_status(status))
            self.assertTrue(goal_status_may_continue(status))

    def test_normalization_is_shared_by_both_policy_queries(self) -> None:
        self.assertEqual(normalize_goal_status(" paused \n"), "paused")
        self.assertTrue(is_reviewed_non_continuing_goal_status(" paused \n"))
        self.assertFalse(goal_status_may_continue(" paused \n"))


if __name__ == "__main__":
    unittest.main()
