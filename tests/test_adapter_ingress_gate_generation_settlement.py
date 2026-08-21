from __future__ import annotations

import threading
import unittest

from bot.adapter_ingress_gate import (
    AdapterIngressGate,
    AdapterOutboundRequestBlocked,
    AdapterOutboundRequestEpochLost,
)


class AdapterIngressGateGenerationSettlementTests(unittest.TestCase):
    def _gate(self) -> AdapterIngressGate:
        return AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )

    def test_capture_requires_an_exact_live_admitted_connection(self) -> None:
        gate = self._gate()

        with self.assertRaises(AdapterOutboundRequestBlocked):
            gate.capture_existing_connection_generation()

        self.assertTrue(gate.accept(7))
        self.assertEqual(gate.capture_existing_connection_generation(), 7)

        self.assertTrue(gate.fence_disconnect(7))
        with self.assertRaises(AdapterOutboundRequestBlocked):
            gate.capture_existing_connection_generation()

    def test_settle_rejects_stale_generation_after_replacement(self) -> None:
        gate = self._gate()
        self.assertTrue(gate.accept(7))
        captured = gate.capture_existing_connection_generation()
        gate.begin_backend_reset()
        gate.admit_backend_replacement(8, publish_replacement=lambda: None)
        callback_calls: list[str] = []

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            gate.run_if_connection_generation(
                captured,
                lambda: callback_calls.append("stale"),
            )

        self.assertEqual(callback_calls, [])

    def test_settle_rejects_matching_generation_after_disconnect_fence(self) -> None:
        gate = self._gate()
        self.assertTrue(gate.accept(7))
        captured = gate.capture_existing_connection_generation()
        self.assertTrue(gate.fence_disconnect(7))

        with self.assertRaises(AdapterOutboundRequestEpochLost):
            gate.run_if_connection_generation(
                captured,
                lambda: self.fail("closed generation must not settle"),
            )

    def test_settle_holds_generation_fence_through_local_callback(self) -> None:
        gate = self._gate()
        self.assertTrue(gate.accept(7))
        captured = gate.capture_existing_connection_generation()
        callback_entered = threading.Event()
        release_callback = threading.Event()
        disconnect_finished = threading.Event()
        result: list[str] = []

        def settle() -> None:
            result.append(
                gate.run_if_connection_generation(
                    captured,
                    lambda: self._blocking_local_settle(
                        callback_entered,
                        release_callback,
                    ),
                )
            )

        def disconnect() -> None:
            gate.fence_disconnect(captured)
            disconnect_finished.set()

        settler = threading.Thread(target=settle)
        disconnector = threading.Thread(target=disconnect)
        settler.start()
        self.assertTrue(callback_entered.wait(timeout=1.0))
        disconnector.start()
        self.assertFalse(disconnect_finished.wait(timeout=0.05))

        release_callback.set()
        settler.join(timeout=1.0)
        disconnector.join(timeout=1.0)

        self.assertFalse(settler.is_alive())
        self.assertFalse(disconnector.is_alive())
        self.assertEqual(result, ["settled"])
        self.assertTrue(disconnect_finished.is_set())

    @staticmethod
    def _blocking_local_settle(
        callback_entered: threading.Event,
        release_callback: threading.Event,
    ) -> str:
        callback_entered.set()
        release_callback.wait(timeout=1.0)
        return "settled"


if __name__ == "__main__":
    unittest.main()
