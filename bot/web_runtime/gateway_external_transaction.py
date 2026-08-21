"""Cancellation-safe handoff for Gateway staged external transactions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar


logger = logging.getLogger(__name__)

_PreparedT = TypeVar("_PreparedT")
_ResultT = TypeVar("_ResultT")


def capture_external_failure(
    exc: BaseException,
    operation: str,
) -> tuple[Exception, BaseException | None]:
    """Preserve fatal control flow while giving domain settlement an error."""

    if isinstance(exc, Exception):
        return exc, None
    return RuntimeError(f"{operation} aborted by {type(exc).__name__}"), exc


class WebGatewayExternalTransactionRunner:
    """Bridge aiohttp cancellation to one exact service-ingress receipt.

    ``asyncio.to_thread`` cannot cancel an already-running worker.  A cancelled
    handler therefore either abandons an unclaimed prepared receipt or lets the
    exact claimed effect/settle callback finish while consuming its result.
    """

    def __init__(self, abandon: Callable[[Any], bool]) -> None:
        if not callable(abandon):
            raise TypeError("Gateway external transaction abandon port is required")
        self._abandon = abandon

    async def prepare(
        self,
        callback: Callable[..., _PreparedT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _PreparedT:
        task = asyncio.create_task(asyncio.to_thread(callback, *args, **kwargs))
        return await self._await_prepare_task(task)

    async def prepare_async(
        self,
        callback: Callable[..., Coroutine[Any, Any, _PreparedT]],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _PreparedT:
        """Run an async prepare owner independently of its request handler.

        The callback may own an event-loop guard such as a document lifecycle
        lock while it awaits worker completion.  Shielding that whole owner,
        rather than only its ``to_thread`` call, prevents handler cancellation
        from releasing the guard while the worker can still mutate prepare
        state.
        """

        task = asyncio.create_task(callback(*args, **kwargs))
        return await self._await_prepare_task(task)

    async def _await_prepare_task(
        self,
        task: asyncio.Task[_PreparedT],
    ) -> _PreparedT:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._abandon_finished_prepare)
            raise

    async def execute(
        self,
        prepared: _PreparedT,
        callback: Callable[[_PreparedT], _ResultT],
    ) -> _ResultT:
        task = asyncio.create_task(asyncio.to_thread(callback, prepared))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._best_effort_abandon(
                prepared,
                "Unable to abandon a cancelled Web external transaction",
            )
            task.add_done_callback(self._consume_finished_execute)
            raise
        except BaseException:
            # ``to_thread`` may fail before its callback reaches the executor,
            # and the callback may reject this token before claiming it.  In
            # both cases the exact prepared ingress receipt is still the
            # service-shutdown barrier.  Once execution has claimed or settled
            # it, the abandon port returns false and leaves that settlement
            # untouched.
            self._best_effort_abandon(
                prepared,
                "Unable to abandon a failed Web external transaction",
            )
            raise

    def _abandon_finished_prepare(
        self,
        task: asyncio.Task[_PreparedT],
    ) -> None:
        try:
            prepared = task.result()
        except BaseException:
            return
        self._best_effort_abandon(
            prepared,
            "Unable to abandon a Web transaction whose handler was cancelled "
            "during prepare",
        )

    def _best_effort_abandon(
        self,
        prepared: Any,
        failure_message: str,
    ) -> None:
        try:
            self._abandon(prepared)
        except BaseException:
            # Cleanup must never replace the request's original failure or
            # cancellation.  The exact receipt owner remains responsible for
            # rejecting forged, stale, or already-claimed tokens.
            logger.exception(failure_message)

    @staticmethod
    def _consume_finished_execute(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            return
