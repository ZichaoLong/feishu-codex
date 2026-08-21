from __future__ import annotations

import threading
import unittest

from bot.adapter_ingress_gate import (
    AdapterBackendResetGenerationMismatchError,
    AdapterBackendResetUnavailableError,
    AdapterIngressGate,
    AdapterOutboundRequestBlocked,
    AdapterOutboundRequestEpochLost,
)


def _gate(
    invalidate_previous_epoch,
    activate_connection_epoch=lambda _generation: None,
) -> AdapterIngressGate:
    return AdapterIngressGate(
        invalidate_previous_epoch=invalidate_previous_epoch,
        activate_connection_epoch=activate_connection_epoch,
    )


def _accept(gate: AdapterIngressGate, generation: int) -> bool:
    return gate.accept(generation)


class AdapterIngressGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.invalidations = 0
        self.gate = _gate(self._record_invalidation)

    def _record_invalidation(self) -> None:
        self.invalidations += 1

    def test_new_generation_invalidates_previous_connection_facts_once(self) -> None:
        self.assertTrue(_accept(self.gate, 1))
        self.assertTrue(_accept(self.gate, 1))
        self.assertTrue(_accept(self.gate, 2))
        self.assertEqual(self.invalidations, 1)
        self.assertEqual(self.gate.snapshot().latest_generation, 2)

    def test_accept_activates_after_cleanup_and_before_dispatch(self) -> None:
        events: list[str] = []
        gate = _gate(
            lambda: events.append("cleanup"),
            lambda generation: events.append(f"activate:{generation}"),
        )

        self.assertTrue(gate.accept(1))
        events.append("dispatch:1")
        self.assertTrue(gate.accept(2))
        events.append("dispatch:2")

        self.assertEqual(
            events,
            [
                "activate:1",
                "dispatch:1",
                "cleanup",
                "activate:2",
                "dispatch:2",
            ],
        )

    def test_repeated_generation_reasserts_idempotent_activation(self) -> None:
        activations: list[int] = []
        gate = _gate(
            self._record_invalidation,
            lambda generation: activations.append(generation),
        )

        for _callback in range(2):
            self.assertTrue(gate.accept(7))

        self.assertEqual(activations, [7, 7])
        self.assertEqual(self.invalidations, 0)

    def test_activation_failure_stays_closed_until_explicit_reset_cleanup(self) -> None:
        activation_attempts: list[int] = []

        def activate(generation: int) -> None:
            activation_attempts.append(generation)
            if generation == 1:
                raise RuntimeError("epoch activation failed")

        gate = _gate(self._record_invalidation, activate)

        with self.assertRaisesRegex(RuntimeError, "epoch activation failed"):
            gate.accept(1)

        failed = gate.snapshot()
        self.assertEqual(failed.latest_generation, 1)
        self.assertTrue(failed.backend_reset_blocked)
        self.assertTrue(failed.cleanup_required)
        self.assertFalse(gate.accept(2))
        self.assertFalse(gate.observe_disconnect(1))
        self.assertEqual(activation_attempts, [1])

        gate.begin_backend_reset()
        recovered = gate.snapshot()
        self.assertEqual(self.invalidations, 1)
        self.assertTrue(recovered.backend_reset_blocked)
        self.assertFalse(recovered.cleanup_required)

        gate.admit_backend_replacement(
            2,
            publish_replacement=lambda: None,
        )
        self.assertTrue(gate.accept(2))
        self.assertEqual(activation_attempts, [1, 2])

    def test_first_published_replacement_callback_still_activates_epoch(self) -> None:
        activations: list[int] = []
        gate = _gate(
            self._record_invalidation,
            lambda generation: activations.append(generation),
        )
        self.assertTrue(_accept(gate, 1))
        gate.begin_backend_reset()
        gate.admit_backend_replacement(
            2,
            publish_replacement=lambda: None,
        )

        self.assertEqual(gate.snapshot().latest_generation, 2)
        self.assertTrue(gate.accept(2))

        self.assertEqual(activations, [1, 2])

    def test_disconnected_and_superseded_generations_are_rejected(self) -> None:
        self.assertTrue(_accept(self.gate, 1))
        self.assertTrue(_accept(self.gate, 2))
        self.assertEqual(self.invalidations, 1)
        self.assertFalse(self.gate.observe_disconnect(1))
        self.assertFalse(_accept(self.gate, 1))
        self.assertTrue(_accept(self.gate, 2))

        self.assertTrue(self.gate.observe_disconnect(2))
        self.assertFalse(_accept(self.gate, 2))
        self.assertTrue(_accept(self.gate, 3))
        # The gate committed generation-2 cleanup inside observe_disconnect;
        # generation 3 must not run it a second time.
        self.assertEqual(self.invalidations, 2)

    def test_backend_reset_stays_closed_until_replacement_is_published(self) -> None:
        published: list[int] = []
        resolved: list[str] = []
        self.assertTrue(_accept(self.gate, 3))
        self.gate.begin_backend_reset()
        self.assertFalse(_accept(self.gate, 4))
        self.assertEqual(self.invalidations, 1)
        self.assertEqual(
            self.gate.resolve_published_backend_endpoint(
                lambda: resolved.append("called") or "ws://127.0.0.1:9004"
            ),
            "",
        )
        self.assertEqual(resolved, [])

        self.gate.admit_backend_replacement(
            4,
            publish_replacement=lambda: published.append(4),
        )

        self.assertEqual(published, [4])
        self.assertEqual(
            self.gate.resolve_published_backend_endpoint(
                lambda: "ws://127.0.0.1:9004"
            ),
            "ws://127.0.0.1:9004",
        )
        self.assertTrue(_accept(self.gate, 4))
        self.assertFalse(_accept(self.gate, 3))
        self.assertFalse(self.gate.snapshot().backend_reset_blocked)

    def test_outbound_requests_share_reset_and_cleanup_admission(self) -> None:
        self.gate.require_outbound_request_admitted()

        self.gate.fence_backend_reset()
        with self.assertRaises(AdapterOutboundRequestBlocked):
            self.gate.require_outbound_request_admitted()

        self.gate.begin_backend_reset()
        with self.assertRaises(AdapterOutboundRequestBlocked):
            self.gate.require_outbound_request_admitted()

        self.gate.admit_backend_replacement(
            1,
            publish_replacement=lambda: None,
        )
        self.gate.require_outbound_request_admitted()

    def test_stale_outbound_permit_cannot_commit_after_replacement_reopens(self) -> None:
        permit = self.gate.issue_outbound_request("thread/start")

        self.gate.fence_backend_reset()
        self.gate.begin_backend_reset()
        self.gate.admit_backend_replacement(1, publish_replacement=lambda: None)

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            self.gate.confirm_outbound_request(permit)

    def test_stale_outbound_permit_is_rejected_at_actual_send_boundary(self) -> None:
        permit = self.gate.issue_outbound_request("turn/start")

        self.gate.fence_backend_reset()
        self.gate.begin_backend_reset()
        self.gate.admit_backend_replacement(1, publish_replacement=lambda: None)

        with self.assertRaises(AdapterOutboundRequestBlocked):
            with self.gate.guard_outbound_send(permit):
                self.fail("stale transport body must not run")

    def test_reset_fence_drains_an_already_admitted_transport_stage(self) -> None:
        permit = self.gate.issue_outbound_request("turn/start")
        transport_entered = threading.Event()
        release_transport = threading.Event()
        fence_finished = threading.Event()

        def transport() -> None:
            with self.gate.guard_outbound_send(permit):
                transport_entered.set()
                release_transport.wait(timeout=1.0)

        def fence() -> None:
            self.gate.fence_backend_reset()
            fence_finished.set()

        sender = threading.Thread(target=transport)
        resetter = threading.Thread(target=fence)
        sender.start()
        self.assertTrue(transport_entered.wait(timeout=1.0))
        resetter.start()
        self.assertFalse(fence_finished.wait(timeout=0.05))

        release_transport.set()
        sender.join(timeout=1.0)
        resetter.join(timeout=1.0)

        self.assertFalse(sender.is_alive())
        self.assertFalse(resetter.is_alive())
        self.assertTrue(fence_finished.is_set())
        self.assertTrue(self.gate.snapshot().backend_reset_blocked)

    def test_expected_generation_fence_rejects_stale_without_mutation(self) -> None:
        self.assertTrue(self.gate.accept(7))
        before = self.gate.snapshot()
        outbound_epoch = self.gate._outbound_epoch

        with self.assertRaises(AdapterBackendResetGenerationMismatchError):
            self.gate.fence_backend_reset(expected_connection_generation=6)

        self.assertEqual(self.gate.snapshot(), before)
        self.assertEqual(self.gate._outbound_epoch, outbound_epoch)
        self.assertEqual(self.invalidations, 0)

    def test_expected_generation_fence_rejects_closed_states_without_mutation(self) -> None:
        cases: list[tuple[str, AdapterIngressGate]] = []

        blocked = _gate(self._record_invalidation)
        self.assertTrue(blocked.accept(7))
        blocked.fence_backend_reset()
        cases.append(("backend reset blocked", blocked))

        pending = _gate(self._record_invalidation)
        self.assertTrue(pending.accept(7))
        self.assertTrue(pending.fence_disconnect(7))
        cases.append(("disconnect cleanup pending", pending))

        def fail_cleanup() -> None:
            raise RuntimeError("cleanup failed")

        cleanup = _gate(fail_cleanup)
        self.assertTrue(cleanup.accept(7))
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            cleanup.observe_disconnect(7)
        cases.append(("cleanup required", cleanup))

        for name, gate in cases:
            with self.subTest(name=name):
                before = gate.snapshot()
                outbound_epoch = gate._outbound_epoch
                with self.assertRaises(AdapterBackendResetUnavailableError):
                    gate.fence_backend_reset(expected_connection_generation=7)
                self.assertEqual(gate.snapshot(), before)
                self.assertEqual(gate._outbound_epoch, outbound_epoch)

    def test_expected_generation_fence_closes_only_matching_open_epoch(self) -> None:
        self.assertTrue(self.gate.accept(7))

        self.gate.fence_backend_reset(expected_connection_generation=7)

        snapshot = self.gate.snapshot()
        self.assertEqual(snapshot.latest_generation, 7)
        self.assertTrue(snapshot.backend_reset_blocked)
        self.assertFalse(snapshot.cleanup_required)
        self.assertFalse(snapshot.disconnect_cleanup_pending)

    def test_disconnect_invalidates_in_flight_outbound_permit(self) -> None:
        self.assertTrue(self.gate.accept(1))
        permit = self.gate.issue_outbound_request("thread/read")

        self.assertTrue(self.gate.observe_disconnect(1))

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            self.gate.confirm_outbound_request(permit)

    def test_reader_disconnect_fence_blocks_reconnect_before_runtime_cleanup(self) -> None:
        self.assertTrue(self.gate.accept(1))
        permit = self.gate.issue_outbound_request("thread/read")

        self.assertTrue(self.gate.fence_disconnect(1))
        self.assertTrue(self.gate.snapshot().disconnect_cleanup_pending)
        with self.assertRaises(AdapterOutboundRequestBlocked):
            self.gate.issue_outbound_request("thread/list")
        with self.assertRaises(AdapterOutboundRequestEpochLost):
            self.gate.confirm_outbound_request(permit)

        self.assertTrue(self.gate.observe_disconnect(1))
        self.assertFalse(self.gate.snapshot().disconnect_cleanup_pending)
        self.gate.require_outbound_request_admitted()

    def test_failed_connection_cleanup_keeps_outbound_requests_closed(self) -> None:
        def fail_cleanup() -> None:
            raise RuntimeError("cleanup failed")

        gate = _gate(fail_cleanup)
        self.assertTrue(gate.accept(1))

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            gate.observe_disconnect(1)

        with self.assertRaises(AdapterOutboundRequestBlocked):
            gate.require_outbound_request_admitted()

    def test_replacement_cannot_publish_without_fence_and_invalidation(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "closed reset fence"):
            self.gate.admit_backend_replacement(
                1,
                publish_replacement=lambda: None,
            )

        self.gate.fence_backend_reset()
        with self.assertRaisesRegex(RuntimeError, "fully invalidated"):
            self.gate.admit_backend_replacement(
                1,
                publish_replacement=lambda: None,
            )

        self.gate.begin_backend_reset()
        self.gate.admit_backend_replacement(
            1,
            publish_replacement=lambda: None,
        )

    def test_callback_ingress_is_closed_during_backend_replacement(self) -> None:
        self.assertTrue(_accept(self.gate, 1))
        self.gate.begin_backend_reset()

        self.assertFalse(self.gate.accept(2))

        self.gate.admit_backend_replacement(2, publish_replacement=lambda: None)
        self.assertTrue(self.gate.accept(2))

    def test_failed_or_stale_replacement_keeps_reset_closed(self) -> None:
        self.assertTrue(_accept(self.gate, 3))
        self.gate.begin_backend_reset()

        with self.assertRaisesRegex(RuntimeError, "publish failed"):
            self.gate.admit_backend_replacement(
                4,
                publish_replacement=lambda: (_ for _ in ()).throw(
                    RuntimeError("publish failed")
                ),
            )
        self.assertTrue(self.gate.snapshot().backend_reset_blocked)
        self.assertEqual(
            self.gate.resolve_published_backend_endpoint(
                lambda: "ws://127.0.0.1:9004"
            ),
            "",
        )

        with self.assertRaisesRegex(RuntimeError, "did not advance"):
            self.gate.admit_backend_replacement(
                3,
                publish_replacement=lambda: None,
            )
        self.assertTrue(self.gate.snapshot().backend_reset_blocked)
        self.assertEqual(
            self.gate.resolve_published_backend_endpoint(
                lambda: "ws://127.0.0.1:9003"
            ),
            "",
        )

    def test_disconnect_cleanup_failure_keeps_every_generation_closed(self) -> None:
        def fail_cleanup() -> None:
            raise RuntimeError("cleanup failed")

        gate = _gate(fail_cleanup)
        self.assertTrue(_accept(gate, 1))

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            gate.observe_disconnect(1)

        snapshot = gate.snapshot()
        self.assertTrue(snapshot.backend_reset_blocked)
        self.assertTrue(snapshot.cleanup_required)
        self.assertFalse(_accept(gate, 2))
        with self.assertRaisesRegex(RuntimeError, "cleanup remains incomplete"):
            gate.admit_backend_replacement(2, publish_replacement=lambda: None)

    def test_explicit_backend_reset_retries_incomplete_cleanup_before_replacement(self) -> None:
        attempts = 0

        def invalidate() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("cleanup failed once")

        gate = _gate(invalidate)
        self.assertTrue(_accept(gate, 1))

        with self.assertRaisesRegex(RuntimeError, "cleanup failed once"):
            gate.observe_disconnect(1)

        gate.begin_backend_reset()
        recovered = gate.snapshot()
        self.assertEqual(attempts, 2)
        self.assertTrue(recovered.backend_reset_blocked)
        self.assertFalse(recovered.cleanup_required)
        self.assertFalse(_accept(gate, 2))

        gate.admit_backend_replacement(2, publish_replacement=lambda: None)
        self.assertTrue(_accept(gate, 2))

    def test_failed_explicit_cleanup_retry_keeps_ingress_closed(self) -> None:
        gate = _gate(
            lambda: (_ for _ in ()).throw(
                RuntimeError("cleanup still failed")
            )
        )
        self.assertTrue(_accept(gate, 1))
        with self.assertRaisesRegex(RuntimeError, "cleanup still failed"):
            gate.observe_disconnect(1)

        with self.assertRaisesRegex(RuntimeError, "cleanup still failed"):
            gate.begin_backend_reset()

        snapshot = gate.snapshot()
        self.assertTrue(snapshot.backend_reset_blocked)
        self.assertTrue(snapshot.cleanup_required)
        self.assertFalse(_accept(gate, 2))

    def test_new_generation_cleanup_failure_cannot_fail_open_later_ingress(self) -> None:
        fail = False

        def invalidate() -> None:
            if fail:
                raise RuntimeError("cleanup failed")

        gate = _gate(invalidate)
        self.assertTrue(_accept(gate, 1))
        fail = True

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            _accept(gate, 2)

        self.assertTrue(gate.snapshot().cleanup_required)
        self.assertFalse(_accept(gate, 3))

    def test_epoch_activation_linearizes_before_disconnect_cleanup(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        competitor_started = threading.Event()
        order: list[str] = []

        def activate(_generation: int) -> None:
            order.append("activation-start")
            entered.set()
            self.assertTrue(release.wait(timeout=1))
            order.append("activation-commit")

        gate = _gate(
            lambda: order.append("cleanup"),
            activate,
        )
        ingress = threading.Thread(target=lambda: gate.accept(1))

        def disconnect() -> None:
            order.append("disconnect-attempt")
            competitor_started.set()
            gate.observe_disconnect(1)
            order.append("disconnect-done")

        competing = threading.Thread(target=disconnect)
        ingress.start()
        self.assertTrue(entered.wait(timeout=1))
        competing.start()
        self.assertTrue(competitor_started.wait(timeout=1))
        release.set()
        ingress.join(timeout=1)
        competing.join(timeout=1)

        self.assertFalse(ingress.is_alive())
        self.assertFalse(competing.is_alive())
        self.assertLess(order.index("activation-commit"), order.index("cleanup"))
        self.assertLess(order.index("cleanup"), order.index("disconnect-done"))

if __name__ == "__main__":
    unittest.main()
