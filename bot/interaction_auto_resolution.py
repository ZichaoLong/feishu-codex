"""Shared request-user-input auto-resolution timers."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol


class _Timer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[..., None], tuple[object, ...]], _Timer]


def _timer_factory(
    delay: float,
    callback: Callable[..., None],
    args: tuple[object, ...],
) -> _Timer:
    return threading.Timer(delay, callback, args=args)


@dataclass(frozen=True, slots=True)
class AutoResolutionTiming:
    backend_epoch: int
    generation: int
    visible_at_ms: int
    due_at_ms: int


@dataclass(frozen=True, slots=True)
class _ScheduledTimer:
    """One concrete scheduling attempt for a backend request."""

    backend_epoch: int
    generation: int
    timer: _Timer


class InteractionAutoResolutionController:
    """Schedule one bounded empty-answer submission per backend request epoch."""

    def __init__(
        self,
        *,
        runtime_submit: Callable[..., None],
        on_due: Callable[[str, int, int], None],
        hidden_grace_seconds: float = 60.0,
        visible_countdown_seconds: float = 60.0,
        timer_factory: TimerFactory = _timer_factory,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._runtime_submit = runtime_submit
        self._on_due = on_due
        self._hidden_grace_seconds = max(float(hidden_grace_seconds), 0.0)
        self._visible_countdown_seconds = max(float(visible_countdown_seconds), 0.0)
        self._timer_factory = timer_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._backend_epoch = 1
        self._next_generation = 0
        self._timers: dict[str, _ScheduledTimer] = {}
        self._callbacks_submitting = 0
        self._closed = False

    def schedule(self, request_key: str, *, enabled: bool) -> AutoResolutionTiming | None:
        normalized_key = str(request_key or "").strip()
        if not normalized_key or not enabled:
            return None
        now = self._clock()
        visible_at = now + self._hidden_grace_seconds
        due_at = visible_at + self._visible_countdown_seconds
        with self._lock:
            if self._closed:
                return None
            self._cancel_locked(normalized_key)
            epoch = self._backend_epoch
            self._next_generation += 1
            generation = self._next_generation
            timer = self._timer_factory(
                max(due_at - now, 0.0),
                self._timer_fired,
                (normalized_key, epoch, generation),
            )
            timer.daemon = True
            self._timers[normalized_key] = _ScheduledTimer(
                backend_epoch=epoch,
                generation=generation,
                timer=timer,
            )
            timer.start()
        return AutoResolutionTiming(
            backend_epoch=epoch,
            generation=generation,
            visible_at_ms=round(visible_at * 1000),
            due_at_ms=round(due_at * 1000),
        )

    def cancel(self, request_key: str) -> None:
        normalized_key = str(request_key or "").strip()
        if not normalized_key:
            return
        with self._lock:
            self._cancel_locked(normalized_key)

    def cancel_if_matches(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> bool:
        """Cancel only the exact schedule represented by this capability."""

        normalized_key = str(request_key or "").strip()
        if not normalized_key:
            return False
        with self._lock:
            current = self._timers.get(normalized_key)
            if (
                current is None
                or current.backend_epoch != backend_epoch
                or current.generation != generation
            ):
                return False
            self._timers.pop(normalized_key)
            current.timer.cancel()
            return True

    def backend_disconnected(self) -> None:
        with self._lock:
            self._backend_epoch += 1
            for scheduled in self._timers.values():
                scheduled.timer.cancel()
            self._timers.clear()

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            for scheduled in self._timers.values():
                scheduled.timer.cancel()
            self._timers.clear()
            while self._callbacks_submitting:
                self._condition.wait()

    def _cancel_locked(self, request_key: str) -> None:
        current = self._timers.pop(request_key, None)
        if current is not None:
            current.timer.cancel()

    def _timer_fired(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> None:
        # Register the submission under the lifecycle lock, then release it
        # before calling the injected port (tests may execute the task
        # synchronously).  ``shutdown`` waits for this short submission
        # interval, so once it returns no timer can race a fresh command into
        # the RuntimeLoop after its authority-release barrier has started.
        with self._condition:
            current = self._timers.get(request_key)
            if (
                self._closed
                or current is None
                or current.backend_epoch != backend_epoch
                or current.generation != generation
                or backend_epoch != self._backend_epoch
            ):
                return
            self._callbacks_submitting += 1
        try:
            self._runtime_submit(
                self._deliver_due,
                request_key,
                backend_epoch,
                generation,
            )
        finally:
            with self._condition:
                self._callbacks_submitting -= 1
                self._condition.notify_all()

    def _deliver_due(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> None:
        with self._lock:
            current = self._timers.get(request_key)
            if (
                self._closed
                or current is None
                or current.backend_epoch != backend_epoch
                or current.generation != generation
                or backend_epoch != self._backend_epoch
            ):
                return
            self._timers.pop(request_key, None)
        self._on_due(request_key, backend_epoch, generation)
