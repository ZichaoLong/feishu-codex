import unittest

from bot.interaction_auto_resolution import InteractionAutoResolutionController


class _FakeTimer:
    def __init__(self, delay, callback, args) -> None:
        self.delay = delay
        self.callback = callback
        self.args = args
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        self.callback(*self.args)


class InteractionAutoResolutionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timers: list[_FakeTimer] = []
        self.delivered: list[tuple[str, int, int]] = []
        self.controller = InteractionAutoResolutionController(
            runtime_submit=lambda fn, *args: fn(*args),
            on_due=lambda request_key, epoch, generation: self.delivered.append(
                (request_key, epoch, generation)
            ),
            hidden_grace_seconds=60,
            visible_countdown_seconds=60,
            timer_factory=self._timer_factory,
            clock=lambda: 100.0,
        )
        self.addCleanup(self.controller.shutdown)

    def _timer_factory(self, delay, callback, args):
        timer = _FakeTimer(delay, callback, args)
        self.timers.append(timer)
        return timer

    def test_schedule_exposes_tui_equivalent_timing_and_submits_once(self) -> None:
        timing = self.controller.schedule("req-1", enabled=True)

        self.assertIsNotNone(timing)
        assert timing is not None
        self.assertEqual(timing.backend_epoch, 1)
        self.assertEqual(timing.generation, 1)
        self.assertEqual(timing.visible_at_ms, 160_000)
        self.assertEqual(timing.due_at_ms, 220_000)
        self.assertEqual(self.timers[0].delay, 120.0)
        self.assertTrue(self.timers[0].started)

        self.timers[0].fire()
        self.timers[0].fire()

        self.assertEqual(self.delivered, [("req-1", 1, timing.generation)])

    def test_reschedule_cancels_previous_timer(self) -> None:
        old_timing = self.controller.schedule("req-1", enabled=True)
        current_timing = self.controller.schedule("req-1", enabled=True)

        assert old_timing is not None
        assert current_timing is not None
        self.assertNotEqual(old_timing.generation, current_timing.generation)

        self.assertTrue(self.timers[0].cancelled)
        self.timers[0].fire()
        self.timers[1].fire()

        self.assertEqual(
            self.delivered,
            [("req-1", 1, current_timing.generation)],
        )

    def test_stale_same_epoch_cancel_cannot_cancel_replacement(self) -> None:
        old_timing = self.controller.schedule("req-1", enabled=True)
        current_timing = self.controller.schedule("req-1", enabled=True)

        assert old_timing is not None
        assert current_timing is not None
        self.assertFalse(
            self.controller.cancel_if_matches(
                "req-1",
                old_timing.backend_epoch,
                old_timing.generation,
            )
        )
        self.assertFalse(self.timers[1].cancelled)

        self.timers[1].fire()

        self.assertEqual(
            self.delivered,
            [
                (
                    "req-1",
                    current_timing.backend_epoch,
                    current_timing.generation,
                )
            ],
        )

    def test_matching_cancel_consumes_exact_schedule_capability(self) -> None:
        timing = self.controller.schedule("req-1", enabled=True)

        assert timing is not None
        self.assertTrue(
            self.controller.cancel_if_matches(
                "req-1",
                timing.backend_epoch,
                timing.generation,
            )
        )
        self.assertTrue(self.timers[0].cancelled)
        self.assertFalse(
            self.controller.cancel_if_matches(
                "req-1",
                timing.backend_epoch,
                timing.generation,
            )
        )

        self.timers[0].fire()

        self.assertEqual(self.delivered, [])

    def test_backend_disconnect_invalidates_old_epoch_timer(self) -> None:
        self.controller.schedule("req-1", enabled=True)

        self.controller.backend_disconnected()
        self.timers[0].fire()

        self.assertTrue(self.timers[0].cancelled)
        self.assertEqual(self.delivered, [])

    def test_queued_old_timer_cannot_deliver_after_same_epoch_replay(self) -> None:
        queued: list[tuple[object, tuple[object, ...]]] = []
        controller = InteractionAutoResolutionController(
            runtime_submit=lambda fn, *args: queued.append((fn, args)),
            on_due=lambda request_key, epoch, generation: self.delivered.append(
                (request_key, epoch, generation)
            ),
            hidden_grace_seconds=60,
            visible_countdown_seconds=60,
            timer_factory=self._timer_factory,
            clock=lambda: 100.0,
        )
        self.addCleanup(controller.shutdown)

        old_timing = controller.schedule("req-1", enabled=True)
        self.timers[0].fire()
        current_timing = controller.schedule("req-1", enabled=True)

        assert old_timing is not None
        assert current_timing is not None

        queued_fn, queued_args = queued.pop()
        queued_fn(*queued_args)
        self.timers[1].fire()
        queued_fn, queued_args = queued.pop()
        queued_fn(*queued_args)

        self.assertEqual(
            self.delivered,
            [
                (
                    "req-1",
                    current_timing.backend_epoch,
                    current_timing.generation,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
