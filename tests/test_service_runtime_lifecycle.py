from __future__ import annotations

import threading
import time
import unittest

from bot.runtime_loop import RuntimeLoop
from bot.service_runtime_lifecycle import (
    ServiceRuntimeActivationPorts,
    ServiceRuntimeIngressDispatcher,
    ServiceRuntimeIngressRejected,
    ServiceRuntimeLifecycle,
    ServiceRuntimeLifecycleError,
    ServiceRuntimeLifecycleReentryError,
    ServiceRuntimeShutdownError,
    ServiceRuntimePhase,
    ServiceRuntimeShutdownPorts,
)


_ACTIVATION_ORDER = (
    "acquire_service_lease",
    "prepare_owned_state",
    "start_runtime_loop",
    "restore_runtime_state",
    "start_adapter",
    "start_destination_liveness_worker",
    "start_control_plane",
    "publish_control_endpoint",
    "register_instance_runtime",
    "restore_runtime_leases",
    "start_web_gateway",
)

_SHUTDOWN_ORDER = (
    "stop_execution_recovery_worker",
    "cancel_frontend_timers",
    "web_is_running",
    "prepare_web_shutdown",
    "stop_web_gateway",
    "stop_control_plane",
    "stop_server_request_runtime",
    "stop_destination_liveness_worker",
    "stop_card_dispatcher",
    "finish_web_shutdown",
    "stop_runtime_loop",
    "stop_adapter",
    "release_machine_authority",
)


class _LifecycleFixture:
    def __init__(self, *, fail_on: str = "", web_running: bool = True) -> None:
        self.events: list[str] = []
        self.fail_on = fail_on
        self.web_running = web_running
        self.lifecycle = ServiceRuntimeLifecycle(
            activation=ServiceRuntimeActivationPorts(
                acquire_service_lease=self.callback("acquire_service_lease"),
                prepare_owned_state=self.callback("prepare_owned_state"),
                start_runtime_loop=self.callback("start_runtime_loop"),
                restore_runtime_state=self.callback("restore_runtime_state"),
                start_adapter=self.callback("start_adapter"),
                start_destination_liveness_worker=self.callback(
                    "start_destination_liveness_worker"
                ),
                start_control_plane=self.start_control_plane,
                publish_control_endpoint=self.publish_control_endpoint,
                register_instance_runtime=self.callback(
                    "register_instance_runtime"
                ),
                restore_runtime_leases=self.callback("restore_runtime_leases"),
                start_web_gateway=self.callback("start_web_gateway"),
            ),
            shutdown=ServiceRuntimeShutdownPorts(
                cancel_frontend_timers=self.callback("cancel_frontend_timers"),
                web_is_running=self.is_web_running,
                prepare_web_shutdown=self.callback("prepare_web_shutdown"),
                stop_web_gateway=self.callback("stop_web_gateway"),
                stop_control_plane=self.callback("stop_control_plane"),
                stop_server_request_runtime=self.callback(
                    "stop_server_request_runtime"
                ),
                stop_execution_recovery_worker=self.callback(
                    "stop_execution_recovery_worker"
                ),
                stop_destination_liveness_worker=self.callback(
                    "stop_destination_liveness_worker"
                ),
                stop_card_dispatcher=self.callback("stop_card_dispatcher"),
                finish_web_shutdown=self.callback("finish_web_shutdown"),
                stop_runtime_loop=self.callback("stop_runtime_loop"),
                stop_adapter=self.callback("stop_adapter"),
                release_machine_authority=self.callback(
                    "release_machine_authority"
                ),
            ),
        )

    def callback(self, name: str):
        def invoke(*_args) -> None:
            self.events.append(name)
            if self.fail_on == name:
                raise RuntimeError(f"failed: {name}")

        return invoke

    def start_control_plane(self) -> str:
        self.callback("start_control_plane")()
        return "tcp://control"

    def publish_control_endpoint(self, endpoint: str) -> None:
        if endpoint != "tcp://control":
            raise AssertionError(endpoint)
        self.callback("publish_control_endpoint")()

    def is_web_running(self) -> bool:
        self.events.append("web_is_running")
        if self.fail_on == "web_is_running":
            raise RuntimeError("failed: web_is_running")
        return self.web_running


class ServiceRuntimeLifecycleTests(unittest.TestCase):
    def test_successful_start_and_stop_are_ordered_and_idempotent(self) -> None:
        fixture = _LifecycleFixture()

        fixture.lifecycle.start()
        fixture.lifecycle.start()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        self.assertEqual(tuple(fixture.events), _ACTIVATION_ORDER)

        fixture.lifecycle.stop()
        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(
            tuple(fixture.events),
            _ACTIVATION_ORDER + _SHUTDOWN_ORDER,
        )
        with self.assertRaisesRegex(ServiceRuntimeLifecycleError, "cannot be restarted"):
            fixture.lifecycle.start()

    def test_assembled_stop_closes_resources_without_releasing_authority(self) -> None:
        fixture = _LifecycleFixture(web_running=False)

        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(
            tuple(fixture.events),
            tuple(
                event
                for event in _SHUTDOWN_ORDER
                if event
                not in {
                    "prepare_web_shutdown",
                    "finish_web_shutdown",
                    "release_machine_authority",
                }
            ),
        )

    def test_lease_acquire_failure_stays_assembled_without_cleanup(self) -> None:
        fixture = _LifecycleFixture(fail_on="acquire_service_lease")

        with self.assertRaisesRegex(RuntimeError, "failed: acquire_service_lease"):
            fixture.lifecycle.start()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ASSEMBLED)
        self.assertEqual(fixture.events, ["acquire_service_lease"])

    def test_every_post_lease_activation_failure_runs_full_rollback(self) -> None:
        for failed_step in _ACTIVATION_ORDER[1:]:
            with self.subTest(failed_step=failed_step):
                fixture = _LifecycleFixture(fail_on=failed_step)
                with self.assertLogs(
                    "bot.service_runtime_lifecycle", level="ERROR"
                ) if failed_step in _SHUTDOWN_ORDER else _NullLogContext():
                    with self.assertRaisesRegex(RuntimeError, f"failed: {failed_step}"):
                        fixture.lifecycle.start()

                self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
                failure_index = _ACTIVATION_ORDER.index(failed_step)
                self.assertEqual(
                    tuple(fixture.events[: failure_index + 1]),
                    _ACTIVATION_ORDER[: failure_index + 1],
                )
                self.assertEqual(
                    tuple(fixture.events[failure_index + 1 :]),
                    _SHUTDOWN_ORDER,
                )
                fixture.lifecycle.stop()

    def test_cleanup_failure_retains_authority_and_retry_can_close(self) -> None:
        fixture = _LifecycleFixture(fail_on="stop_control_plane")
        fixture.lifecycle.start()

        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaises(ServiceRuntimeShutdownError) as raised:
                fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertEqual(raised.exception.stage, "producer cleanup")
        self.assertNotIn("stop_runtime_loop", fixture.events)
        self.assertNotIn("stop_adapter", fixture.events)
        self.assertNotIn("release_machine_authority", fixture.events)

        fixture.fail_on = ""
        fixture.lifecycle.stop()
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(fixture.events.count("stop_control_plane"), 2)
        self.assertEqual(fixture.events.count("stop_web_gateway"), 1)
        self.assertLess(
            fixture.events.index("stop_runtime_loop"),
            fixture.events.index("stop_adapter"),
        )

    def test_execution_recovery_join_precedes_timer_cancellation(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()

        fixture.lifecycle.stop()

        self.assertLess(
            fixture.events.index("stop_execution_recovery_worker"),
            fixture.events.index("cancel_frontend_timers"),
        )
        self.assertLess(
            fixture.events.index("stop_execution_recovery_worker"),
            fixture.events.index("stop_runtime_loop"),
        )
        self.assertLess(
            fixture.events.index("stop_execution_recovery_worker"),
            fixture.events.index("stop_adapter"),
        )

    def test_execution_recovery_join_failure_does_not_cross_timer_barrier(
        self,
    ) -> None:
        fixture = _LifecycleFixture(fail_on="stop_execution_recovery_worker")
        fixture.lifecycle.start()

        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaises(ServiceRuntimeShutdownError) as raised:
                fixture.lifecycle.stop()

        self.assertEqual(
            raised.exception.stage,
            "execution recovery producer barrier",
        )
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertNotIn("cancel_frontend_timers", fixture.events)
        self.assertNotIn("stop_runtime_loop", fixture.events)
        self.assertNotIn("stop_adapter", fixture.events)

        fixture.fail_on = ""
        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(
            fixture.events.count("stop_execution_recovery_worker"),
            2,
        )
        self.assertEqual(fixture.events.count("cancel_frontend_timers"), 1)
        self.assertLess(
            fixture.events.index("cancel_frontend_timers"),
            fixture.events.index("stop_runtime_loop"),
        )

    def test_runtime_barrier_failure_is_raised_and_retains_authority(self) -> None:
        fixture = _LifecycleFixture(fail_on="stop_runtime_loop")
        fixture.lifecycle.start()

        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaisesRegex(
                ServiceRuntimeShutdownError,
                "failed: stop_runtime_loop",
            ) as raised:
                fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertEqual(raised.exception.stage, "RuntimeLoop barrier")
        self.assertNotIn("stop_adapter", fixture.events)
        self.assertNotIn("release_machine_authority", fixture.events)
        prepared_count = fixture.events.count("prepare_web_shutdown")
        fixture.fail_on = ""
        fixture.lifecycle.stop()
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(
            fixture.events.count("prepare_web_shutdown"),
            prepared_count,
        )
        self.assertEqual(fixture.events.count("stop_runtime_loop"), 2)

    def test_release_failure_is_raised_and_can_be_retried(self) -> None:
        fixture = _LifecycleFixture(fail_on="release_machine_authority")
        fixture.lifecycle.start()

        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaisesRegex(
                ServiceRuntimeShutdownError,
                "failed: release_machine_authority",
            ):
                fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        fixture.fail_on = ""
        fixture.lifecycle.stop()
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)

    def test_adapter_failure_happens_only_after_runtime_barrier_and_is_retryable(self) -> None:
        fixture = _LifecycleFixture(fail_on="stop_adapter")
        fixture.lifecycle.start()

        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaises(ServiceRuntimeShutdownError) as raised:
                fixture.lifecycle.stop()

        self.assertEqual(raised.exception.stage, "adapter cleanup")
        self.assertLess(
            fixture.events.index("stop_runtime_loop"),
            fixture.events.index("stop_adapter"),
        )
        self.assertNotIn("release_machine_authority", fixture.events)
        pre_barrier_events = tuple(fixture.events)

        fixture.fail_on = ""
        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertEqual(fixture.events.count("stop_runtime_loop"), 1)
        self.assertEqual(fixture.events.count("stop_adapter"), 2)
        self.assertEqual(
            tuple(fixture.events[: len(pre_barrier_events)]),
            pre_barrier_events,
        )

    def test_concurrent_starts_serialize_and_activate_once(self) -> None:
        fixture = _LifecycleFixture()
        entered = threading.Event()
        release = threading.Event()
        original_prepare = fixture.lifecycle._activation.prepare_owned_state

        def blocking_prepare() -> None:
            entered.set()
            release.wait(timeout=2.0)
            original_prepare()

        object.__setattr__(
            fixture.lifecycle._activation,
            "prepare_owned_state",
            blocking_prepare,
        )
        errors: list[Exception] = []
        first = threading.Thread(target=lambda: self._capture(fixture.lifecycle.start, errors))
        second = threading.Thread(target=lambda: self._capture(fixture.lifecycle.start, errors))

        first.start()
        self.assertTrue(entered.wait(timeout=1.0))
        second.start()
        time.sleep(0.02)
        self.assertTrue(second.is_alive())
        release.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

        self.assertEqual(errors, [])
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        self.assertEqual(fixture.events.count("acquire_service_lease"), 1)

    def test_callback_reentry_is_rejected_without_deadlock(self) -> None:
        fixture = _LifecycleFixture()

        def reenter_stop() -> None:
            fixture.events.append("prepare_owned_state")
            fixture.lifecycle.stop()

        object.__setattr__(
            fixture.lifecycle._activation,
            "prepare_owned_state",
            reenter_stop,
        )

        with self.assertRaises(ServiceRuntimeLifecycleReentryError):
            fixture.lifecycle.start()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertIn("release_machine_authority", fixture.events)

    def test_external_ingress_requires_the_exact_active_phase(self) -> None:
        fixture = _LifecycleFixture()

        with self.assertRaises(ServiceRuntimeIngressRejected) as assembled:
            fixture.lifecycle.begin_external_ingress()
        self.assertEqual(assembled.exception.phase, ServiceRuntimePhase.ASSEMBLED)

        fixture.lifecycle.start()
        receipt = fixture.lifecycle.begin_external_ingress()
        observed = fixture.lifecycle.run_external_ingress(
            receipt,
            lambda: fixture.lifecycle.phase,
        )
        self.assertEqual(observed, ServiceRuntimePhase.ACTIVE)
        with self.assertRaisesRegex(
            ServiceRuntimeLifecycleError,
            "stale or belongs to another lifecycle",
        ):
            fixture.lifecycle.run_external_ingress(receipt, lambda: None)

        fixture.lifecycle.stop()
        with self.assertRaises(ServiceRuntimeIngressRejected) as closed:
            fixture.lifecycle.begin_external_ingress()
        self.assertEqual(closed.exception.phase, ServiceRuntimePhase.CLOSED)

    def test_offline_maintenance_closes_ingress_before_idle_verification(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        observed_phases: list[ServiceRuntimePhase] = []

        result = fixture.lifecycle.prepare_offline_maintenance(
            lambda: observed_phases.append(fixture.lifecycle.phase) or {"idle": True}
        )

        self.assertEqual(result, {"idle": True})
        self.assertEqual(observed_phases, [ServiceRuntimePhase.STOPPING])
        self.assertTrue(fixture.lifecycle.offline_maintenance_prepared)
        with self.assertRaises(ServiceRuntimeIngressRejected) as rejected:
            fixture.lifecycle.begin_external_ingress()
        self.assertEqual(rejected.exception.phase, ServiceRuntimePhase.STOPPING)

    def test_failed_idle_verification_reopens_ingress(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()

        with self.assertRaisesRegex(RuntimeError, "active turn"):
            fixture.lifecycle.prepare_offline_maintenance(
                lambda: (_ for _ in ()).throw(RuntimeError("active turn"))
            )

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        self.assertFalse(fixture.lifecycle.offline_maintenance_prepared)
        receipt = fixture.lifecycle.begin_external_ingress()
        fixture.lifecycle.abandon_external_ingress(receipt)

    def test_active_ingress_rejects_maintenance_without_changing_phase(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        receipt = fixture.lifecycle.begin_external_ingress()
        fixture.lifecycle.confirm_external_ingress_dispatch(receipt)

        with self.assertRaisesRegex(
            ServiceRuntimeLifecycleError,
            "external ingress is active",
        ):
            fixture.lifecycle.prepare_offline_maintenance(lambda: None)

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        self.assertFalse(fixture.lifecycle.offline_maintenance_prepared)
        fixture.lifecycle.abandon_external_ingress(receipt)

    def test_cancel_reopens_only_an_exact_maintenance_preparation(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()

        with self.assertRaisesRegex(
            ServiceRuntimeLifecycleError,
            "no cancellable",
        ):
            fixture.lifecycle.cancel_offline_maintenance()

        fixture.lifecycle.prepare_offline_maintenance(lambda: None)
        fixture.lifecycle.cancel_offline_maintenance()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.ACTIVE)
        self.assertFalse(fixture.lifecycle.offline_maintenance_prepared)
        with self.assertRaisesRegex(
            ServiceRuntimeLifecycleError,
            "no cancellable",
        ):
            fixture.lifecycle.cancel_offline_maintenance()

    def test_prepared_maintenance_is_consumed_by_normal_shutdown(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        fixture.lifecycle.prepare_offline_maintenance(lambda: None)

        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        self.assertFalse(fixture.lifecycle.offline_maintenance_prepared)
        self.assertEqual(tuple(fixture.events), _ACTIVATION_ORDER + _SHUTDOWN_ORDER)

    def test_ordinary_stopping_state_cannot_be_cancelled_as_maintenance(self) -> None:
        fixture = _LifecycleFixture(fail_on="stop_control_plane")
        fixture.lifecycle.start()
        with self.assertLogs("bot.service_runtime_lifecycle", level="ERROR"):
            with self.assertRaises(ServiceRuntimeShutdownError):
                fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertFalse(fixture.lifecycle.offline_maintenance_prepared)
        with self.assertRaisesRegex(
            ServiceRuntimeLifecycleError,
            "no cancellable",
        ):
            fixture.lifecycle.cancel_offline_maintenance()

    def test_stop_closes_ingress_then_waits_for_an_admitted_callback(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        callback_entered = threading.Event()
        allow_reentrant_stop = threading.Event()
        callback_errors: list[Exception] = []
        stop_errors: list[Exception] = []
        receipt = fixture.lifecycle.begin_external_ingress()

        def admitted_callback() -> None:
            callback_entered.set()
            allow_reentrant_stop.wait(timeout=2.0)
            self._capture(fixture.lifecycle.stop, callback_errors)

        callback_thread = threading.Thread(
            target=lambda: fixture.lifecycle.run_external_ingress(
                receipt,
                admitted_callback,
            )
        )
        callback_thread.start()
        self.assertTrue(callback_entered.wait(timeout=1.0))

        stop_thread = threading.Thread(
            target=lambda: self._capture(fixture.lifecycle.stop, stop_errors)
        )
        stop_thread.start()
        deadline = time.monotonic() + 1.0
        while (
            fixture.lifecycle.phase is not ServiceRuntimePhase.STOPPING
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        self.assertNotIn("cancel_frontend_timers", fixture.events)
        with self.assertRaises(ServiceRuntimeIngressRejected) as stopping:
            fixture.lifecycle.begin_external_ingress()
        self.assertEqual(stopping.exception.phase, ServiceRuntimePhase.STOPPING)

        allow_reentrant_stop.set()
        callback_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)
        self.assertFalse(callback_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertEqual(len(callback_errors), 1)
        self.assertIsInstance(
            callback_errors[0],
            ServiceRuntimeLifecycleReentryError,
        )
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)

    def test_external_transaction_keeps_runtime_loop_free_and_fences_shutdown(
        self,
    ) -> None:
        fixture = _LifecycleFixture()
        runtime_loop = RuntimeLoop(name="staged-ingress-test-loop")
        original_start_runtime_loop = (
            fixture.lifecycle._activation.start_runtime_loop
        )
        original_stop_runtime_loop = fixture.lifecycle._shutdown.stop_runtime_loop

        def start_runtime_loop() -> None:
            original_start_runtime_loop()
            runtime_loop.start()

        def stop_runtime_loop() -> None:
            original_stop_runtime_loop()
            runtime_loop.stop(timeout=1.0)

        object.__setattr__(
            fixture.lifecycle._activation,
            "start_runtime_loop",
            start_runtime_loop,
        )
        object.__setattr__(
            fixture.lifecycle._shutdown,
            "stop_runtime_loop",
            stop_runtime_loop,
        )
        dispatcher = ServiceRuntimeIngressDispatcher(
            fixture.lifecycle,
            runtime_loop.call,
            runtime_loop.submit,
        )
        effect_entered = threading.Event()
        release_effect = threading.Event()
        settle_finished = threading.Event()
        sentinel_finished = threading.Event()
        prepare_thread_ids: list[int] = []
        effect_thread_ids: list[int] = []
        settle_thread_ids: list[int] = []
        sentinel_thread_ids: list[int] = []
        transaction_errors: list[Exception] = []
        stop_errors: list[Exception] = []

        def prepare() -> None:
            prepare_thread_ids.append(threading.get_ident())

        def settle() -> None:
            settle_thread_ids.append(threading.get_ident())
            settle_finished.set()

        def staged_transaction() -> None:
            runtime_loop.call(prepare)
            effect_thread_ids.append(threading.get_ident())
            effect_entered.set()
            if not release_effect.wait(timeout=5.0):
                raise TimeoutError("test did not release the staged effect")
            runtime_loop.call(settle)

        fixture.lifecycle.start()
        transaction_thread = threading.Thread(
            target=lambda: self._capture(
                lambda: dispatcher.run_external_transaction(staged_transaction),
                transaction_errors,
            ),
            name="staged-external-transaction",
        )
        stop_thread: threading.Thread | None = None
        transaction_thread.start()
        try:
            self.assertTrue(effect_entered.wait(timeout=1.0))

            def sentinel() -> None:
                sentinel_thread_ids.append(threading.get_ident())
                sentinel_finished.set()

            runtime_loop.submit(sentinel)
            self.assertTrue(sentinel_finished.wait(timeout=1.0))
            self.assertFalse(settle_finished.is_set())

            stop_thread = threading.Thread(
                target=lambda: self._capture(fixture.lifecycle.stop, stop_errors),
                name="staged-external-transaction-stop",
            )
            stop_thread.start()
            deadline = time.monotonic() + 1.0
            while (
                fixture.lifecycle.phase is not ServiceRuntimePhase.STOPPING
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)

            self.assertEqual(
                fixture.lifecycle.phase,
                ServiceRuntimePhase.STOPPING,
            )
            self.assertTrue(stop_thread.is_alive())
            self.assertNotIn("cancel_frontend_timers", fixture.events)
            with self.assertRaises(ServiceRuntimeIngressRejected) as stopping:
                fixture.lifecycle.begin_external_ingress()
            self.assertEqual(
                stopping.exception.phase,
                ServiceRuntimePhase.STOPPING,
            )

            release_effect.set()
            transaction_thread.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

            self.assertFalse(transaction_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(transaction_errors, [])
            self.assertEqual(stop_errors, [])
            self.assertTrue(settle_finished.is_set())
            self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
            self.assertEqual(len(prepare_thread_ids), 1)
            self.assertEqual(prepare_thread_ids, settle_thread_ids)
            self.assertEqual(prepare_thread_ids, sentinel_thread_ids)
            self.assertEqual(len(effect_thread_ids), 1)
            self.assertEqual(effect_thread_ids[0], transaction_thread.ident)
            self.assertNotEqual(effect_thread_ids, prepare_thread_ids)
        finally:
            release_effect.set()
            transaction_thread.join(timeout=1.0)
            if stop_thread is not None:
                stop_thread.join(timeout=1.0)
            runtime_loop.stop(timeout=1.0)

    def test_background_external_transaction_uses_the_same_shutdown_barrier(
        self,
    ) -> None:
        fixture = _LifecycleFixture()
        dispatcher = ServiceRuntimeIngressDispatcher(
            fixture.lifecycle,
            lambda callback, *args, **kwargs: callback(*args, **kwargs),
            lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        effect_entered = threading.Event()
        release_effect = threading.Event()
        stop_finished = threading.Event()

        def transaction() -> None:
            effect_entered.set()
            if not release_effect.wait(timeout=5.0):
                raise TimeoutError("test did not release background transaction")

        fixture.lifecycle.start()
        worker = dispatcher.start_background_external_transaction(
            transaction,
            thread_name="background-external-transaction-test",
        )
        self.assertTrue(effect_entered.wait(timeout=1.0))

        stop_thread = threading.Thread(
            target=lambda: (fixture.lifecycle.stop(), stop_finished.set()),
            name="background-external-transaction-stop",
        )
        stop_thread.start()
        try:
            deadline = time.monotonic() + 1.0
            while (
                fixture.lifecycle.phase is not ServiceRuntimePhase.STOPPING
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
            self.assertFalse(stop_finished.is_set())

            release_effect.set()
            worker.join(timeout=1.0)
            stop_thread.join(timeout=1.0)
            self.assertFalse(worker.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(stop_finished.is_set())
            self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        finally:
            release_effect.set()
            worker.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

    def test_stopping_rejects_successor_from_admitted_background_transaction(
        self,
    ) -> None:
        fixture = _LifecycleFixture()
        dispatcher = ServiceRuntimeIngressDispatcher(
            fixture.lifecycle,
            lambda callback, *args, **kwargs: callback(*args, **kwargs),
            lambda callback, *args, **kwargs: callback(*args, **kwargs),
        )
        effect_entered = threading.Event()
        release_effect = threading.Event()
        successor_started = threading.Event()
        successor_rejected = threading.Event()
        stop_errors: list[Exception] = []

        def transaction() -> None:
            effect_entered.set()
            if not release_effect.wait(timeout=5.0):
                raise TimeoutError("test did not release background transaction")
            try:
                dispatcher.start_background_external_transaction(
                    successor_started.set,
                    thread_name="background-successor-must-not-start",
                )
            except ServiceRuntimeIngressRejected as exc:
                self.assertEqual(exc.phase, ServiceRuntimePhase.STOPPING)
                successor_rejected.set()

        fixture.lifecycle.start()
        worker = dispatcher.start_background_external_transaction(
            transaction,
            thread_name="background-predecessor-during-stop",
        )
        self.assertTrue(effect_entered.wait(timeout=1.0))
        stop_thread = threading.Thread(
            target=lambda: self._capture(fixture.lifecycle.stop, stop_errors),
            name="background-successor-admission-stop",
        )
        stop_thread.start()
        try:
            deadline = time.monotonic() + 1.0
            while (
                fixture.lifecycle.phase is not ServiceRuntimePhase.STOPPING
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)

            release_effect.set()
            worker.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

            self.assertFalse(worker.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertEqual(stop_errors, [])
            self.assertTrue(successor_rejected.is_set())
            self.assertFalse(successor_started.is_set())
            self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)
        finally:
            release_effect.set()
            worker.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

    def test_abandoned_ingress_receipt_does_not_block_shutdown(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        receipt = fixture.lifecycle.begin_external_ingress()

        self.assertTrue(fixture.lifecycle.abandon_external_ingress(receipt))
        self.assertFalse(fixture.lifecycle.abandon_external_ingress(receipt))
        fixture.lifecycle.stop()

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)

    def test_ingress_callback_start_cannot_deadlock_with_concurrent_stop(self) -> None:
        fixture = _LifecycleFixture()
        fixture.lifecycle.start()
        callback_entered = threading.Event()
        try_reentrant_start = threading.Event()
        callback_errors: list[Exception] = []
        stop_errors: list[Exception] = []
        receipt = fixture.lifecycle.begin_external_ingress()

        def admitted_callback() -> None:
            callback_entered.set()
            try_reentrant_start.wait(timeout=2.0)
            self._capture(fixture.lifecycle.start, callback_errors)

        callback_thread = threading.Thread(
            target=lambda: fixture.lifecycle.run_external_ingress(
                receipt,
                admitted_callback,
            )
        )
        callback_thread.start()
        self.assertTrue(callback_entered.wait(timeout=1.0))

        stop_thread = threading.Thread(
            target=lambda: self._capture(fixture.lifecycle.stop, stop_errors)
        )
        stop_thread.start()
        deadline = time.monotonic() + 1.0
        while (
            fixture.lifecycle.phase is not ServiceRuntimePhase.STOPPING
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)

        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.STOPPING)
        try_reentrant_start.set()
        callback_thread.join(timeout=1.0)
        stop_thread.join(timeout=1.0)

        self.assertFalse(callback_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertEqual(stop_errors, [])
        self.assertEqual(len(callback_errors), 1)
        self.assertIsInstance(
            callback_errors[0],
            ServiceRuntimeLifecycleReentryError,
        )
        self.assertEqual(fixture.lifecycle.phase, ServiceRuntimePhase.CLOSED)

    @staticmethod
    def _capture(action, errors: list[Exception]) -> None:
        try:
            action()
        except Exception as exc:  # pragma: no cover - asserted by the caller
            errors.append(exc)


class _NullLogContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
