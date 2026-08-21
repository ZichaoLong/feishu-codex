"""Lifecycle owner for bounded execution-recovery workers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ExecutionRecoveryShutdownTimeoutError(RuntimeError):
    """Raised when execution-recovery workers cannot be proven stopped."""


class ExecutionRecoveryWorkerRegistry:
    """Own worker admission, cooperative stop, and the shutdown join barrier."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._stopping = False
        self._stop = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    def wait_for_stop(self, timeout: float) -> bool:
        return self._stop.wait(timeout)

    def start(self, callback: Callable[[], None]) -> bool:
        """Start one tracked worker, or return false after admission closes."""

        callback_admitted = threading.Event()

        def run() -> None:
            callback_admitted.wait()
            try:
                callback()
            finally:
                with self._lock:
                    self._workers.discard(threading.current_thread())

        worker = threading.Thread(target=run, daemon=True)
        with self._lock:
            if self._stopping:
                return False
            self._workers.add(worker)
            try:
                worker.start()
            except BaseException:
                self._workers.discard(worker)
                raise
        callback_admitted.set()
        return True

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Close admission, request cooperative stop, and join every worker."""

        with self._lock:
            self._stopping = True
            self._stop.set()
            workers = tuple(self._workers)
        current = threading.current_thread()
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        for worker in workers:
            if worker is current:
                continue
            remaining = (
                None
                if deadline is None
                else max(deadline - time.monotonic(), 0.0)
            )
            worker.join(timeout=remaining)
        live_workers = [
            worker
            for worker in workers
            if worker is not current and worker.is_alive()
        ]
        if live_workers:
            raise ExecutionRecoveryShutdownTimeoutError(
                f"{len(live_workers)} execution recovery worker(s) did not stop"
            )
