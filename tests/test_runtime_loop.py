from __future__ import annotations

import logging
import threading
import unittest

from bot.runtime_loop import (
    RuntimeLoop,
    RuntimeLoopCallTimeoutError,
    RuntimeLoopContextError,
    RuntimeLoopReentrantOperationError,
    RuntimeLoopShutdownTimeoutError,
)


class RuntimeLoopTests(unittest.TestCase):
    def test_stop_waits_for_accepted_async_task_before_returning(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")
        entered = threading.Event()
        release = threading.Event()
        stop_returned = threading.Event()

        def work() -> None:
            entered.set()
            release.wait()

        loop.submit(work)
        self.assertTrue(entered.wait(timeout=1))

        stopper = threading.Thread(target=lambda: (loop.stop(), stop_returned.set()))
        stopper.start()
        self.assertFalse(stop_returned.wait(timeout=0.05))
        release.set()
        stopper.join(timeout=1)

        self.assertFalse(stopper.is_alive())
        self.assertTrue(stop_returned.is_set())

    def test_bounded_stop_raises_instead_of_returning_with_live_worker(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")
        entered = threading.Event()
        release = threading.Event()
        loop.submit(lambda: (entered.set(), release.wait()))
        self.assertTrue(entered.wait(timeout=1))

        with self.assertRaises(RuntimeLoopShutdownTimeoutError):
            loop.stop(timeout=0.01)

        release.set()
        loop.stop(timeout=1)

    def test_worker_cannot_return_from_an_unproved_self_stop_barrier(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")

        with self.assertRaisesRegex(
            RuntimeLoopReentrantOperationError,
            "cannot prove its own shutdown barrier",
        ):
            loop.call(loop.stop)

        # The rejected call must not half-close the loop.
        self.assertEqual(loop.call(lambda: "still-open"), "still-open")
        loop.stop()

    def test_async_task_exception_is_logged(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")
        completed = threading.Event()

        def fail() -> None:
            try:
                raise ValueError("broken persistence")
            finally:
                completed.set()

        with self.assertLogs("bot.runtime_loop", level=logging.ERROR) as captured:
            loop.submit(fail)
            self.assertTrue(completed.wait(timeout=1))
            loop.stop()

        self.assertIn(
            "unhandled exception in async runtime task", "\n".join(captured.output)
        )
        self.assertIn("broken persistence", "\n".join(captured.output))

    def test_deadline_cancels_task_that_is_still_queued(self) -> None:
        observations = []
        loop = RuntimeLoop(
            name="test-runtime-loop",
            slow_queue_threshold_seconds=60,
            slow_task_threshold_seconds=60,
            task_observer=observations.append,
        )
        entered = threading.Event()
        release = threading.Event()
        called = threading.Event()
        loop.submit(lambda: (entered.set(), release.wait()))
        self.assertTrue(entered.wait(timeout=1))

        with self.assertRaises(RuntimeLoopCallTimeoutError) as captured:
            loop.call_with_deadline(0.02, called.set)
        self.assertFalse(captured.exception.may_still_run)

        release.set()
        loop.call(lambda: None)
        self.assertFalse(called.is_set())
        snapshot = loop.snapshot()
        self.assertEqual(snapshot.cancelled_tasks, 1)
        self.assertGreaterEqual(snapshot.failed_tasks, 1)
        cancelled = next(
            observation
            for observation in observations
            if observation.cancelled_before_start
        )
        self.assertEqual(cancelled.queue_depth_at_enqueue, 0)
        self.assertTrue(cancelled.active_task_at_enqueue)
        self.assertGreaterEqual(
            cancelled.active_task_age_seconds_at_enqueue,
            0.0,
        )
        loop.stop()

    def test_deadline_reports_unknown_outcome_after_task_started(self) -> None:
        loop = RuntimeLoop(
            name="test-runtime-loop",
            slow_queue_threshold_seconds=60,
            slow_task_threshold_seconds=60,
        )
        entered = threading.Event()
        release = threading.Event()

        def work() -> None:
            entered.set()
            release.wait()

        releaser = threading.Timer(0.05, release.set)
        releaser.start()
        with self.assertRaises(RuntimeLoopCallTimeoutError) as captured:
            loop.call_with_deadline(0.01, work)
        self.assertTrue(entered.is_set())
        self.assertTrue(captured.exception.may_still_run)
        release.wait(timeout=1)
        loop.call(lambda: None)
        releaser.join(timeout=1)
        loop.stop()

    def test_worker_cannot_silently_ignore_a_nested_deadline(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")
        called = threading.Event()

        with self.assertRaisesRegex(
            RuntimeLoopReentrantOperationError,
            "cannot enforce a nested task deadline",
        ):
            loop.call(loop.call_with_deadline, 1.0, called.set)

        self.assertFalse(called.is_set())
        loop.stop()

    def test_snapshot_and_observer_expose_queue_and_task_measurements(self) -> None:
        observations = []
        loop = RuntimeLoop(
            name="test-runtime-loop",
            slow_queue_threshold_seconds=60,
            slow_task_threshold_seconds=60,
            task_observer=observations.append,
        )
        entered = threading.Event()
        release = threading.Event()
        loop.submit(lambda: (entered.set(), release.wait()))
        self.assertTrue(entered.wait(timeout=1))
        active = loop.snapshot()
        self.assertTrue(active.active_task_name)
        self.assertGreaterEqual(active.active_task_duration_seconds, 0)

        release.set()
        loop.call(lambda: None)
        completed = loop.snapshot()
        self.assertEqual(completed.accepted_tasks, 2)
        self.assertEqual(completed.completed_tasks, 2)
        self.assertEqual(len(observations), 2)
        self.assertGreaterEqual(completed.max_task_duration_seconds, 0)
        loop.stop()

    def test_observer_preserves_enqueue_backlog_context(self) -> None:
        observations = []
        loop = RuntimeLoop(
            name="test-runtime-loop",
            slow_queue_threshold_seconds=60,
            slow_task_threshold_seconds=60,
            task_observer=observations.append,
        )
        entered = threading.Event()
        release = threading.Event()

        def blocking_task() -> None:
            entered.set()
            release.wait()

        def first_waiting_task() -> None:
            return None

        def second_waiting_task() -> None:
            return None

        loop.submit(blocking_task)
        self.assertTrue(entered.wait(timeout=1))
        loop.submit(first_waiting_task)
        loop.submit(second_waiting_task)
        release.set()
        loop.call(lambda: None)

        by_name = {observation.task_name: observation for observation in observations}
        first = by_name[first_waiting_task.__qualname__]
        second = by_name[second_waiting_task.__qualname__]
        self.assertEqual(first.queue_depth_at_enqueue, 0)
        self.assertEqual(second.queue_depth_at_enqueue, 1)
        self.assertEqual(first.active_task_at_enqueue, blocking_task.__qualname__)
        self.assertEqual(second.active_task_at_enqueue, blocking_task.__qualname__)
        self.assertGreaterEqual(first.active_task_age_seconds_at_enqueue, 0.0)
        self.assertGreaterEqual(second.active_task_age_seconds_at_enqueue, 0.0)
        loop.stop()

    def test_worker_context_assertion_fails_closed(self) -> None:
        loop = RuntimeLoop(name="test-runtime-loop")
        with self.assertRaises(RuntimeLoopContextError):
            loop.assert_worker_context()
        loop.call(loop.assert_worker_context)
        loop.stop()


if __name__ == "__main__":
    unittest.main()
