"""Closed Focus policy for persisted goal continuation risk.

Upstream can add goal statuses independently.  Focus must never infer safety
from a status merely being different from ``active``: only the explicitly
reviewed statuses below prove that a resume or exact goal mutation cannot
continue autonomous work.
"""

from __future__ import annotations

from typing import Final


REVIEWED_NON_CONTINUING_GOAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "paused",
        "blocked",
        "usageLimited",
        "budgetLimited",
        "complete",
    }
)


def normalize_goal_status(value: object) -> str:
    """Return the exact normalized wire status used by continuation policy."""

    return str(value or "").strip()


def is_reviewed_non_continuing_goal_status(value: object) -> bool:
    """Whether ``value`` is positive evidence of non-continuation."""

    return normalize_goal_status(value) in REVIEWED_NON_CONTINUING_GOAL_STATUSES


def goal_status_may_continue(value: object) -> bool:
    """Fail closed for active, empty, malformed, unknown, or future statuses."""

    return not is_reviewed_non_continuing_goal_status(value)
