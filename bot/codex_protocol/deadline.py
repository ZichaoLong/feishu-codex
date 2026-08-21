"""Shared monotonic-deadline synchronization for Codex transport owners."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator


def remaining_before_deadline(
    deadline_monotonic: float | None,
    *,
    operation: str,
) -> float | None:
    """Return the remaining budget or reject an already-expired operation."""

    if deadline_monotonic is None:
        return None
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Codex {operation} exceeded its caller deadline")
    return remaining


@contextmanager
def held_lock_before_deadline(
    lock: Any,
    *,
    deadline_monotonic: float | None,
    operation: str,
) -> Iterator[None]:
    """Acquire a local transport-owner lock within one caller deadline."""

    if deadline_monotonic is None:
        lock.acquire()
    else:
        remaining = remaining_before_deadline(
            deadline_monotonic,
            operation=operation,
        )
        if not lock.acquire(timeout=remaining):
            raise TimeoutError(f"Codex {operation} lock acquisition timed out")
    try:
        yield
    finally:
        lock.release()
