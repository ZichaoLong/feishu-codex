"""Retryable shutdown ownership for one Codex RPC client."""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from bot.codex_protocol.managed_process import (
    ManagedAppServerProcess,
    ManagedProcessStopResult,
)

logger = logging.getLogger(__name__)

DEFAULT_STOP_TIMEOUT_SECONDS = 5.0


class CodexRpcStopError(RuntimeError):
    """Shutdown is incomplete and the exact resources remain retryable."""

    def __init__(
        self,
        *,
        pending_resources: tuple[str, ...],
        failures: tuple[str, ...] = (),
    ) -> None:
        self.pending_resources = pending_resources
        self.failures = failures
        details = []
        if pending_resources:
            details.append("pending=" + ", ".join(pending_resources))
        if failures:
            details.append("failures=" + "; ".join(failures))
        suffix = ": " + " | ".join(details) if details else ""
        super().__init__(
            "Codex RPC stop is incomplete; retry stop() before start()" + suffix
        )


@dataclass(frozen=True, slots=True)
class RpcStopResourceTransfer:
    """Exact producer capabilities transferred out of connection ownership."""

    websocket: Any | None = None
    reader_threads: tuple[threading.Thread, ...] = ()
    callback_threads: tuple[threading.Thread, ...] = ()
    managed_process: ManagedAppServerProcess | None = None

    @property
    def has_resources(self) -> bool:
        return bool(
            self.websocket is not None
            or self.reader_threads
            or self.callback_threads
            or self.managed_process is not None
        )


@dataclass(frozen=True, slots=True)
class RpcStopAttempt:
    """Opaque receipt for one single-flight drain attempt."""

    number: int


@dataclass
class _WebsocketCloseOperation:
    thread: threading.Thread
    completed: threading.Event
    error: BaseException | None = None


@dataclass
class _StopResources:
    websocket: Any | None
    reader_threads: tuple[threading.Thread, ...]
    callback_threads: tuple[threading.Thread, ...]
    managed_process: ManagedAppServerProcess | None
    websocket_closed: bool = False
    websocket_close_operation: _WebsocketCloseOperation | None = None

    @classmethod
    def receive(cls, transfer: RpcStopResourceTransfer) -> _StopResources:
        return cls(
            websocket=transfer.websocket,
            reader_threads=transfer.reader_threads,
            callback_threads=transfer.callback_threads,
            managed_process=transfer.managed_process,
            websocket_closed=transfer.websocket is None,
        )


@dataclass
class _StopAttemptState:
    receipt: RpcStopAttempt
    completed: bool = False
    error: CodexRpcStopError | None = None


class CodexRpcStopBarrier:
    """Own stop fencing, detached resources, and retryable drain outcomes.

    The connection and stop state machines intentionally share one identity
    lock. The client transfers exact producer capabilities while holding that
    lock; this owner never mirrors connection generation or pending RPC facts.
    """

    def __init__(
        self,
        *,
        identity_lock: Any,
        condition: threading.Condition,
    ) -> None:
        self._identity_lock = identity_lock
        self._condition = condition
        self._requested = threading.Event()
        self._attempt_number = 0
        self._resources: _StopResources | None = None
        self._current_attempt: _StopAttemptState | None = None
        self._last_outcome: tuple[int, CodexRpcStopError | None] | None = None

    @staticmethod
    def deadline(timeout: float) -> float:
        timeout_seconds = float(timeout)
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError(
                "Codex RPC stop timeout must be a finite non-negative number"
            )
        return time.monotonic() + timeout_seconds

    def request_stop(self) -> None:
        self._requested.set()

    @property
    def stop_requested(self) -> bool:
        return self._requested.is_set()

    @property
    def active(self) -> bool:
        return self._current_attempt is not None

    @property
    def has_retained_resources(self) -> bool:
        return self._resources is not None

    @property
    def last_error(self) -> CodexRpcStopError | None:
        outcome = self._last_outcome
        return outcome[1] if outcome is not None else None

    @property
    def is_clear(self) -> bool:
        return (
            not self.stop_requested
            and self._current_attempt is None
            and self._resources is None
        )

    def raise_if_stop_requested(self) -> None:
        if self.stop_requested:
            raise CodexRpcStopError(
                pending_resources=("requested stop cleanup",),
            )

    def wait_until_startable_locked(self) -> None:
        """Wait behind a raced stop; caller must hold the shared condition."""

        while self._current_attempt is not None:
            if self._current_thread_is_owned_locked():
                raise CodexRpcStopError(
                    pending_resources=("active stop barrier",),
                    failures=(
                        "an adapter-owned producer cannot wait for the stop "
                        "barrier that owns it",
                    ),
                )
            self._condition.wait()
        if self._resources is not None:
            if self.last_error is not None:
                raise self.last_error
            raise CodexRpcStopError(
                pending_resources=("retained stop resources",),
            )
        self.raise_if_stop_requested()

    def join_active_locked(self, *, deadline_monotonic: float) -> None:
        """Join the current single-flight attempt under the shared condition."""

        attempt = self._current_attempt
        if attempt is None:
            raise RuntimeError("Codex RPC stop barrier has no active attempt")
        if self._current_thread_is_owned_locked():
            raise CodexRpcStopError(
                pending_resources=("active stop barrier",),
                failures=(
                    "an adapter-owned producer cannot wait for the stop barrier "
                    "that owns it",
                ),
            )
        while not attempt.completed:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                raise CodexRpcStopError(
                    pending_resources=("active stop operation",),
                    failures=(
                        "timed out waiting for the single-flight stop owner",
                    ),
                )
            self._condition.wait(timeout=remaining)
        if attempt.error is not None:
            raise attempt.error

    def begin_locked(
        self,
        transfer: RpcStopResourceTransfer,
    ) -> RpcStopAttempt:
        """Accept exact resources and begin one drain attempt under the lock."""

        if self._current_attempt is not None:
            raise RuntimeError("Codex RPC stop attempt is already active")
        if not self.stop_requested:
            raise RuntimeError("Codex RPC stop resources require a stop request")
        if self._resources is None:
            self._resources = _StopResources.receive(transfer)
        elif transfer.has_resources:
            raise RuntimeError(
                "new Codex RPC resources cannot replace retained stop resources"
            )
        self._attempt_number += 1
        receipt = RpcStopAttempt(number=self._attempt_number)
        self._current_attempt = _StopAttemptState(receipt=receipt)
        return receipt

    def drain_attempt(
        self,
        attempt: RpcStopAttempt,
        *,
        deadline_monotonic: float,
    ) -> None:
        """Drain one attempt and publish a retryable outcome before returning."""

        with self._condition:
            current = self._require_current_attempt_locked(attempt)
            resources = self._resources
        if resources is None:
            raise RuntimeError("Codex RPC stop resources are unavailable")

        stop_error: CodexRpcStopError | None = None
        try:
            stop_error = self._drain_resources(
                resources,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:
            logger.exception("Unexpected Codex RPC stop cleanup failure")
            stop_error = CodexRpcStopError(
                pending_resources=("retained stop resources",),
                failures=(f"unexpected cleanup failure: {exc}",),
            )
        except BaseException as exc:
            stop_error = CodexRpcStopError(
                pending_resources=("retained stop resources",),
                failures=(f"cleanup interrupted by {type(exc).__name__}",),
            )
            raise
        finally:
            with self._condition:
                if stop_error is None and self._resources is resources:
                    self._resources = None
                    self._requested.clear()
                self._last_outcome = (attempt.number, stop_error)
                current.error = stop_error
                current.completed = True
                if self._current_attempt is current:
                    self._current_attempt = None
                self._condition.notify_all()
        if stop_error is not None:
            raise stop_error

    @contextmanager
    def identity_lock(self, *, deadline_monotonic: float):
        remaining = max(0.0, deadline_monotonic - time.monotonic())
        if not self._identity_lock.acquire(timeout=remaining):
            raise CodexRpcStopError(
                pending_resources=("client identity/send lock",),
                failures=("timed out closing transport ingress",),
            )
        try:
            yield
        finally:
            self._identity_lock.release()

    def _require_current_attempt_locked(
        self,
        attempt: RpcStopAttempt,
    ) -> _StopAttemptState:
        current = self._current_attempt
        if current is None or current.receipt is not attempt:
            raise RuntimeError("Codex RPC stop attempt receipt is stale")
        return current

    def _drain_resources(
        self,
        resources: _StopResources,
        *,
        deadline_monotonic: float,
    ) -> CodexRpcStopError | None:
        failures: list[str] = []

        self._drain_websocket_close(
            resources,
            deadline_monotonic=deadline_monotonic,
            failures=failures,
        )
        managed_process = resources.managed_process
        if managed_process is not None:
            failures.extend(managed_process.request_stop())

        if resources.websocket_closed:
            self._join_owned_threads(
                resources.reader_threads,
                kind="reader",
                deadline_monotonic=deadline_monotonic,
                failures=failures,
            )

        managed_result = ManagedProcessStopResult()
        if managed_process is not None:
            managed_result = managed_process.drain_stop(
                deadline_monotonic=deadline_monotonic,
            )
            failures.extend(managed_result.failures)
        self._join_owned_threads(
            resources.callback_threads,
            kind="server callback",
            deadline_monotonic=deadline_monotonic,
            failures=failures,
        )

        pending_resources = (
            *self._pending_resource_names(resources),
            *managed_result.pending_resources,
        )
        if not failures and not pending_resources:
            return None
        return CodexRpcStopError(
            pending_resources=pending_resources,
            failures=tuple(failures),
        )

    def _drain_websocket_close(
        self,
        resources: _StopResources,
        *,
        deadline_monotonic: float,
        failures: list[str],
    ) -> None:
        if resources.websocket_closed or resources.websocket is None:
            resources.websocket_closed = True
            return

        operation = resources.websocket_close_operation
        if operation is not None and operation.completed.is_set():
            if operation.error is None:
                resources.websocket_closed = True
                resources.websocket_close_operation = None
                return
            resources.websocket_close_operation = None
            operation = None

        if operation is None:
            completed = threading.Event()
            websocket = resources.websocket
            operation_holder: list[_WebsocketCloseOperation] = []

            def close_websocket() -> None:
                try:
                    websocket.close()
                except BaseException as exc:
                    operation_holder[0].error = exc
                finally:
                    completed.set()

            close_thread = threading.Thread(
                target=close_websocket,
                name="focus-codex-websocket-close",
                daemon=True,
            )
            operation = _WebsocketCloseOperation(
                thread=close_thread,
                completed=completed,
            )
            operation_holder.append(operation)
            resources.websocket_close_operation = operation
            try:
                close_thread.start()
            except Exception as exc:
                resources.websocket_close_operation = None
                failures.append(f"websocket close thread failed to start: {exc}")
                return

        remaining = max(0.0, deadline_monotonic - time.monotonic())
        try:
            operation.thread.join(timeout=remaining)
        except Exception as exc:
            failures.append(f"websocket close join failed: {exc}")
            return
        if not operation.completed.is_set():
            failures.append("websocket close timed out")
            return
        if operation.error is not None:
            failures.append(f"websocket close failed: {operation.error}")
            resources.websocket_close_operation = None
            return
        resources.websocket_closed = True
        resources.websocket_close_operation = None

    @classmethod
    def _join_owned_threads(
        cls,
        threads: tuple[threading.Thread, ...],
        *,
        kind: str,
        deadline_monotonic: float,
        failures: list[str],
    ) -> None:
        current_thread = threading.current_thread()
        for thread in threads:
            if not cls._thread_is_alive(thread):
                continue
            name = getattr(thread, "name", None) or kind
            if thread is current_thread:
                failures.append(f"cannot join current {kind} thread {name}")
                continue
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            try:
                thread.join(timeout=remaining)
            except Exception as exc:
                failures.append(f"{kind} thread {name} join failed: {exc}")
                continue
            if cls._thread_is_alive(thread):
                failures.append(f"{kind} thread {name} join timed out")

    @staticmethod
    def _thread_is_alive(thread: threading.Thread) -> bool:
        is_alive = getattr(thread, "is_alive", None)
        if not callable(is_alive):
            return False
        return bool(is_alive())

    @classmethod
    def _pending_resource_names(
        cls,
        resources: _StopResources,
    ) -> tuple[str, ...]:
        pending: list[str] = []
        if not resources.websocket_closed:
            pending.append("websocket")
        pending.extend(
            f"reader thread {getattr(thread, 'name', 'unnamed')}"
            for thread in resources.reader_threads
            if cls._thread_is_alive(thread)
        )
        pending.extend(
            f"server callback thread {getattr(thread, 'name', 'unnamed')}"
            for thread in resources.callback_threads
            if cls._thread_is_alive(thread)
        )
        return tuple(pending)

    def _current_thread_is_owned_locked(self) -> bool:
        resources = self._resources
        if resources is None:
            return False
        current_thread = threading.current_thread()
        if current_thread in (
            *resources.reader_threads,
            *resources.callback_threads,
        ):
            return True
        if (
            resources.managed_process is not None
            and resources.managed_process.owns_thread(current_thread)
        ):
            return True
        close_operation = resources.websocket_close_operation
        return bool(
            close_operation is not None
            and current_thread is close_operation.thread
        )
