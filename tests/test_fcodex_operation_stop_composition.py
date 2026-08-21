"""Fcodex active-turn and backend-epoch composition regressions."""

from __future__ import annotations

from tests.fcodex_operation_harness import (
    FcodexOperationHarness,
    _service_server_request,
    jsonrpc_id_key,
)


class FcodexOperationStopCompositionTests(FcodexOperationHarness):
    def test_backend_stop_retires_fcodex_facts_but_not_shared_lease(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()
        routed = _service_server_request(self.coordinator, "reset-approval")
        self.assertTrue(routed["handled"])

        receipt = self.coordinator.settle_backend_epoch_after_stop()

        self.assertIn(
            jsonrpc_id_key("reset-approval"),
            receipt.interaction_request_keys,
        )
        self.assertFalse(hasattr(receipt, "released_interaction_lease_thread_ids"))
        self.assertIsNotNone(self.interaction_leases.load("root-1"))


if __name__ == "__main__":
    import unittest

    unittest.main()
