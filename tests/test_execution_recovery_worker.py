import threading
import time
import unittest
from unittest.mock import Mock, patch

from bot.execution_recovery_worker import (
    ExecutionRecoveryShutdownTimeoutError,
    ExecutionRecoveryWorkerRegistry,
)


class ExecutionRecoveryWorkerRegistryTests(unittest.TestCase):
    def test_start_is_atomic_with_shutdown_before_native_thread_start(self) -> None:
        registry = ExecutionRecoveryWorkerRegistry()
        begin_start = threading.Event()
        native_start_entered = threading.Event()
        allow_native_start = threading.Event()
        begin_shutdown = threading.Event()
        shutdown_called = threading.Event()
        start_done = threading.Event()
        shutdown_done = threading.Event()
        callback_done = threading.Event()
        start_results: list[bool] = []
        errors: list[BaseException] = []
        original_start = threading.Thread.start

        def start_registry_worker() -> None:
            begin_start.wait()
            try:
                start_results.append(registry.start(callback_done.set))
            except BaseException as exc:
                errors.append(exc)
            finally:
                start_done.set()

        def shutdown_registry() -> None:
            begin_shutdown.wait()
            shutdown_called.set()
            try:
                registry.shutdown()
            except BaseException as exc:
                errors.append(exc)
            finally:
                shutdown_done.set()

        starter = threading.Thread(target=start_registry_worker, daemon=True)
        shutdown_worker = threading.Thread(target=shutdown_registry, daemon=True)
        starter.start()
        shutdown_worker.start()

        def controlled_native_start(worker: threading.Thread) -> None:
            native_start_entered.set()
            if not allow_native_start.wait(timeout=1.0):
                raise AssertionError("native worker start was not released")
            original_start(worker)

        lock_was_held_during_native_start = False
        with patch.object(threading.Thread, "start", controlled_native_start):
            begin_start.set()
            self.assertTrue(native_start_entered.wait(timeout=1.0))
            lock_was_held_during_native_start = registry._lock.locked()

            begin_shutdown.set()
            self.assertTrue(shutdown_called.wait(timeout=1.0))
            self.assertFalse(shutdown_done.is_set())
            allow_native_start.set()

            self.assertTrue(start_done.wait(timeout=1.0))
            self.assertTrue(shutdown_done.wait(timeout=1.0))

        starter.join(timeout=1.0)
        shutdown_worker.join(timeout=1.0)
        self.assertFalse(starter.is_alive())
        self.assertFalse(shutdown_worker.is_alive())
        self.assertTrue(lock_was_held_during_native_start)
        self.assertEqual(start_results, [True])
        self.assertEqual(errors, [])
        self.assertTrue(callback_done.is_set())

    def test_start_failure_rolls_back_registration(self) -> None:
        registry = ExecutionRecoveryWorkerRegistry()
        failed_callback = Mock()

        with (
            patch.object(threading.Thread, "start", side_effect=OSError("boom")),
            self.assertRaisesRegex(OSError, "boom"),
        ):
            registry.start(failed_callback)

        completed = threading.Event()
        self.assertTrue(registry.start(completed.set))
        self.assertTrue(completed.wait(timeout=1.0))
        registry.shutdown(timeout=1.0)
        failed_callback.assert_not_called()

    def test_shutdown_waits_for_workers_and_closes_admission(self) -> None:
        registry = ExecutionRecoveryWorkerRegistry()
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def run() -> None:
            started.set()
            release.wait()
            completed.set()

        self.assertTrue(registry.start(run))
        self.assertTrue(started.wait(timeout=1.0))

        shutdown_done = threading.Event()
        shutdown_worker = threading.Thread(
            target=lambda: (registry.shutdown(), shutdown_done.set()),
            daemon=True,
        )
        shutdown_worker.start()
        deadline = time.monotonic() + 1.0
        while not registry.stop_requested and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertTrue(registry.stop_requested)
        self.assertFalse(shutdown_done.is_set())

        release.set()
        shutdown_worker.join(timeout=1.0)
        self.assertFalse(shutdown_worker.is_alive())
        self.assertTrue(completed.is_set())
        self.assertTrue(shutdown_done.is_set())
        rejected_callback = Mock()
        self.assertFalse(registry.start(rejected_callback))
        rejected_callback.assert_not_called()

    def test_timeout_does_not_claim_that_a_live_worker_stopped(self) -> None:
        registry = ExecutionRecoveryWorkerRegistry()
        started = threading.Event()
        release = threading.Event()
        self.assertTrue(registry.start(lambda: (started.set(), release.wait())))
        self.assertTrue(started.wait(timeout=1.0))

        with self.assertRaises(ExecutionRecoveryShutdownTimeoutError):
            registry.shutdown(timeout=0.0)

        release.set()
        registry.shutdown(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
