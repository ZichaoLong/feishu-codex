"""
Serialized runtime event loop for handler state mutations.

The Feishu transport layer, app-server callback threads, and timer callbacks
should not mutate CodexHandler runtime state directly. They enqueue commands
onto this loop instead, so the handler can behave like a small event-driven
runtime instead of a pile of cross-thread shared-state callbacks.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

_Thread = threading.Thread
logger = logging.getLogger(__name__)


class RuntimeLoopClosedError(RuntimeError):
    """Raised when work is submitted after the runtime loop has stopped."""


class RuntimeLoopShutdownTimeoutError(RuntimeError):
    """Raised when a bounded shutdown cannot prove that the worker exited."""


class RuntimeLoopCallTimeoutError(TimeoutError):
    """Raised when a synchronous caller's explicit runtime deadline expires.

    ``may_still_run`` distinguishes a task that was cancelled while queued
    from one that had already entered user code and therefore cannot be
    pre-empted safely by Python's threading runtime.
    """

    def __init__(self, task_name: str, *, may_still_run: bool) -> None:
        self.task_name = task_name
        self.may_still_run = may_still_run
        outcome = (
            "may still be running"
            if may_still_run
            else "was cancelled before execution"
        )
        super().__init__(
            f"runtime task {task_name!r} exceeded its deadline and {outcome}"
        )


class RuntimeLoopContextError(RuntimeError):
    """Raised when worker-owned state is accessed outside RuntimeLoop."""


class RuntimeLoopReentrantOperationError(RuntimeError):
    """Raised when the worker asks the loop for an impossible guarantee.

    A worker cannot join itself to prove the shutdown barrier, and it cannot
    enforce a deadline around code that it would have to execute inline.
    Reject those calls instead of silently weakening the public contract.
    """


@dataclass(frozen=True, slots=True)
class RuntimeTaskObservation:
    """One completed or deadline-cancelled task measurement."""

    task_name: str
    queue_age_seconds: float
    task_duration_seconds: float
    failed: bool
    cancelled_before_start: bool = False
    queue_depth_at_enqueue: int = 0
    active_task_at_enqueue: str = ""
    active_task_age_seconds_at_enqueue: float = 0.0


@dataclass(frozen=True, slots=True)
class RuntimeLoopSnapshot:
    """Thread-safe operational snapshot; counters are monotonic per loop."""

    accepted_tasks: int
    completed_tasks: int
    failed_tasks: int
    cancelled_tasks: int
    queued_tasks: int
    active_task_name: str
    active_task_duration_seconds: float
    last_queue_age_seconds: float
    last_task_duration_seconds: float
    max_queue_age_seconds: float
    max_task_duration_seconds: float


class RuntimeTaskObserver(Protocol):
    def __call__(self, observation: RuntimeTaskObservation, /) -> None: ...


class RuntimeContextGuard(Protocol):
    """Required capability for RuntimeLoop-confined aggregate access."""

    def __call__(self) -> None: ...


@dataclass(slots=True)
class _Task:
    fn: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    done: threading.Event | None = None
    result: Any = None
    error: BaseException | None = None
    name: str = ""
    enqueued_at: float = 0.0
    queue_depth_at_enqueue: int = 0
    active_task_at_enqueue: str = ""
    active_task_age_seconds_at_enqueue: float = 0.0
    started_at: float | None = None
    deadline_at: float | None = None
    cancelled_before_start: bool = False
    state_lock: threading.Lock | None = None


_STOP = object()


class RuntimeLoop:
    """A single-threaded command loop for stateful runtime operations."""

    def __init__(
        self,
        *,
        name: str = "runtime-loop",
        slow_queue_threshold_seconds: float = 1.0,
        slow_task_threshold_seconds: float = 5.0,
        task_observer: RuntimeTaskObserver | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._queue: queue.Queue[_Task | object] = queue.Queue()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._closed = False
        self._clock = clock
        self._slow_queue_threshold_seconds = max(
            float(slow_queue_threshold_seconds), 0.0
        )
        self._slow_task_threshold_seconds = max(float(slow_task_threshold_seconds), 0.0)
        self._task_observer = task_observer
        self._accepted_tasks = 0
        self._completed_tasks = 0
        self._failed_tasks = 0
        self._cancelled_tasks = 0
        self._active_task_name = ""
        self._active_task_started_at: float | None = None
        self._last_queue_age_seconds = 0.0
        self._last_task_duration_seconds = 0.0
        self._max_queue_age_seconds = 0.0
        self._max_task_duration_seconds = 0.0

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeLoopClosedError(f"{self._name} is closed")
            if self._worker is not None and self._worker.is_alive():
                return
            worker = _Thread(target=self._run, name=self._name, daemon=True)
            self._worker = worker
            worker.start()

    def stop(self, *, timeout: float | None = None) -> None:
        """Close the loop and wait until its worker has exited.

        Returning from this method is a lifecycle barrier: callers may release
        service/runtime leases only after every task accepted before ``stop``
        has completed.  A caller that deliberately supplies a timeout gets an
        explicit failure instead of a false successful shutdown.
        """
        with self._lock:
            worker = self._worker
            if worker is not None and threading.current_thread() is worker:
                raise RuntimeLoopReentrantOperationError(
                    f"{self._name} worker cannot prove its own shutdown barrier"
                )
            if not self._closed:
                self._closed = True
                self._queue.put(_STOP)
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
            if worker.is_alive():
                raise RuntimeLoopShutdownTimeoutError(
                    f"{self._name} did not stop within {timeout} seconds"
                )

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        if self._is_worker_thread():
            fn(*args, **kwargs)
            return
        self._enqueue(_Task(fn=fn, args=args, kwargs=kwargs))

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._is_worker_thread():
            return fn(*args, **kwargs)
        task = _Task(fn=fn, args=args, kwargs=kwargs, done=threading.Event())
        self._enqueue(task)
        assert task.done is not None
        task.done.wait()
        if task.error is not None:
            raise task.error
        return task.result

    def call_with_deadline(
        self,
        timeout_seconds: float,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run one task with a caller-visible, queue-inclusive deadline.

        A task that has not started when the deadline expires is marked
        cancelled and will never call ``fn``.  Python cannot safely terminate
        code that is already running, so that case raises with
        ``may_still_run=True``.  Callers performing upstream mutations must
        preserve their ordinary unknown-outcome handling for that case.
        """

        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self._is_worker_thread():
            raise RuntimeLoopReentrantOperationError(
                f"{self._name} worker cannot enforce a nested task deadline"
            )
        now = self._clock()
        task = _Task(
            fn=fn,
            args=args,
            kwargs=kwargs,
            done=threading.Event(),
            deadline_at=now + timeout,
        )
        self._enqueue(task, enqueued_at=now)
        assert task.done is not None
        remaining = max((task.deadline_at or now) - self._clock(), 0.0)
        if not task.done.wait(remaining):
            state_lock = task.state_lock
            assert state_lock is not None
            with state_lock:
                may_still_run = task.started_at is not None
                if not may_still_run:
                    task.cancelled_before_start = True
            raise RuntimeLoopCallTimeoutError(
                task.name,
                may_still_run=may_still_run,
            )
        if task.error is not None:
            raise task.error
        return task.result

    def assert_worker_context(self) -> None:
        """Assert that the caller is executing on this loop's worker."""

        if not self._is_worker_thread():
            raise RuntimeLoopContextError(
                f"{self._name} state may only be mutated from its RuntimeLoop worker"
            )

    def snapshot(self) -> RuntimeLoopSnapshot:
        """Return metrics without entering the serialized worker queue."""

        now = self._clock()
        with self._lock:
            active_duration = (
                max(now - self._active_task_started_at, 0.0)
                if self._active_task_started_at is not None
                else 0.0
            )
            return RuntimeLoopSnapshot(
                accepted_tasks=self._accepted_tasks,
                completed_tasks=self._completed_tasks,
                failed_tasks=self._failed_tasks,
                cancelled_tasks=self._cancelled_tasks,
                queued_tasks=self._queue.qsize(),
                active_task_name=self._active_task_name,
                active_task_duration_seconds=active_duration,
                last_queue_age_seconds=self._last_queue_age_seconds,
                last_task_duration_seconds=self._last_task_duration_seconds,
                max_queue_age_seconds=self._max_queue_age_seconds,
                max_task_duration_seconds=self._max_task_duration_seconds,
            )

    def _enqueue(self, task: _Task, *, enqueued_at: float | None = None) -> None:
        self.start()
        with self._lock:
            if self._closed:
                raise RuntimeLoopClosedError(f"{self._name} is closed")
            task.name = self._task_name(task.fn)
            task.enqueued_at = self._clock() if enqueued_at is None else enqueued_at
            task.queue_depth_at_enqueue = self._queue.qsize()
            task.active_task_at_enqueue = self._active_task_name
            task.active_task_age_seconds_at_enqueue = (
                max(task.enqueued_at - self._active_task_started_at, 0.0)
                if self._active_task_name and self._active_task_started_at is not None
                else 0.0
            )
            task.state_lock = threading.Lock()
            self._accepted_tasks += 1
            self._queue.put(task)

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            if task is _STOP:
                return
            assert isinstance(task, _Task)
            started_at = self._clock()
            state_lock = task.state_lock
            assert state_lock is not None
            with state_lock:
                deadline_expired = (
                    task.deadline_at is not None and started_at >= task.deadline_at
                )
                if task.cancelled_before_start or deadline_expired:
                    task.cancelled_before_start = True
                else:
                    task.started_at = started_at
            if task.cancelled_before_start:
                task.error = RuntimeLoopCallTimeoutError(
                    task.name,
                    may_still_run=False,
                )
                self._record_observation(
                    task,
                    RuntimeTaskObservation(
                        task_name=task.name,
                        queue_age_seconds=max(started_at - task.enqueued_at, 0.0),
                        task_duration_seconds=0.0,
                        failed=True,
                        cancelled_before_start=True,
                        queue_depth_at_enqueue=task.queue_depth_at_enqueue,
                        active_task_at_enqueue=task.active_task_at_enqueue,
                        active_task_age_seconds_at_enqueue=(
                            task.active_task_age_seconds_at_enqueue
                        ),
                    ),
                )
                if task.done is not None:
                    task.done.set()
                continue
            with self._lock:
                self._active_task_name = task.name
                self._active_task_started_at = started_at
            try:
                task.result = task.fn(*task.args, **task.kwargs)
            except BaseException as exc:  # pragma: no cover - exercised via call()
                task.error = exc
                if task.done is None:
                    logger.exception(
                        "unhandled exception in async runtime task %s",
                        getattr(task.fn, "__qualname__", repr(task.fn)),
                    )
            finally:
                finished_at = self._clock()
                self._record_observation(
                    task,
                    RuntimeTaskObservation(
                        task_name=task.name,
                        queue_age_seconds=max(started_at - task.enqueued_at, 0.0),
                        task_duration_seconds=max(finished_at - started_at, 0.0),
                        failed=task.error is not None,
                        queue_depth_at_enqueue=task.queue_depth_at_enqueue,
                        active_task_at_enqueue=task.active_task_at_enqueue,
                        active_task_age_seconds_at_enqueue=(
                            task.active_task_age_seconds_at_enqueue
                        ),
                    ),
                )
                if task.done is not None:
                    task.done.set()

    def _is_worker_thread(self) -> bool:
        worker = self._worker
        return worker is not None and threading.current_thread() is worker

    @staticmethod
    def _task_name(fn: Callable[..., Any]) -> str:
        return str(getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn))))

    def _record_observation(
        self,
        task: _Task,
        observation: RuntimeTaskObservation,
    ) -> None:
        with self._lock:
            self._completed_tasks += 1
            if observation.failed:
                self._failed_tasks += 1
            if observation.cancelled_before_start:
                self._cancelled_tasks += 1
            self._last_queue_age_seconds = observation.queue_age_seconds
            self._last_task_duration_seconds = observation.task_duration_seconds
            self._max_queue_age_seconds = max(
                self._max_queue_age_seconds,
                observation.queue_age_seconds,
            )
            self._max_task_duration_seconds = max(
                self._max_task_duration_seconds,
                observation.task_duration_seconds,
            )
            if self._active_task_name == task.name:
                self._active_task_name = ""
                self._active_task_started_at = None
        if observation.queue_age_seconds >= self._slow_queue_threshold_seconds:
            logger.warning(
                "runtime task %s waited %.3fs in %s queue",
                observation.task_name,
                observation.queue_age_seconds,
                self._name,
            )
        if observation.task_duration_seconds >= self._slow_task_threshold_seconds:
            logger.warning(
                "runtime task %s occupied %s for %.3fs",
                observation.task_name,
                self._name,
                observation.task_duration_seconds,
            )
        if self._task_observer is not None:
            try:
                self._task_observer(observation)
            except Exception:
                logger.exception(
                    "runtime task observer failed for %s", observation.task_name
                )
