"""Narrow external epoch lease around Codex transport side effects."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator, TypeAlias


OutboundTransportGuard: TypeAlias = Callable[[], ContextManager[None]]


class OutboundTransportGuardRejectedError(RuntimeError):
    """The external epoch owner rejected work before transport effects."""

    def __init__(self, cause: Exception) -> None:
        self.cause = cause
        super().__init__(str(cause))


@contextmanager
def held_outbound_transport_guard(
    guard: OutboundTransportGuard | None,
) -> Iterator[None]:
    """Hold an epoch lease only around process/socket transport effects."""

    if guard is None:
        yield
        return
    try:
        manager = guard()
        manager.__enter__()
    except Exception as exc:
        raise OutboundTransportGuardRejectedError(exc) from exc
    try:
        yield
    finally:
        # Epoch guards cannot suppress a process-start or websocket error.
        manager.__exit__(*sys.exc_info())
