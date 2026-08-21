"""Finite request widths for bounded Focus Web full-turn presentation."""

from __future__ import annotations


DEFAULT_TURN_WINDOW_LIMIT = 10
SUPPORTED_TURN_WINDOW_LIMITS = (5, 10, 20)
MAX_TURN_WINDOW_LIMIT = max(SUPPORTED_TURN_WINDOW_LIMITS)


def parse_turn_window_limit(raw_value: object) -> int:
    """Admit only the exact public 5/10/20 query vocabulary."""

    if raw_value is None:
        return DEFAULT_TURN_WINDOW_LIMIT
    if not isinstance(raw_value, str) or raw_value not in {
        str(value) for value in SUPPORTED_TURN_WINDOW_LIMITS
    }:
        raise ValueError("turn_limit must be one of 5, 10, or 20")
    return int(raw_value)


def require_turn_window_limit(value: int) -> int:
    """Defend the coordinator boundary after Gateway admission."""

    if (
        isinstance(value, bool)
        or value not in SUPPORTED_TURN_WINDOW_LIMITS
    ):
        raise ValueError("unsupported Focus Web turn window limit")
    return value
