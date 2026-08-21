from __future__ import annotations

import unittest

from bot.backend_reset.interaction_coordinator import (
    BackendResetInteractionCoordinator,
)


class BackendResetInteractionCoordinatorTests(unittest.TestCase):
    def test_prepare_all_snapshots_only_the_canonical_pending_count(self) -> None:
        calls: list[str] = []

        def pending_count() -> int:
            calls.append("count")
            return 3

        coordinator = BackendResetInteractionCoordinator(pending_count)

        receipt = coordinator.prepare_all()

        self.assertEqual(receipt.pending_request_count, 3)
        self.assertEqual(receipt.to_dict(), {"pending": 3})
        self.assertEqual(calls, ["count"])

    def test_invalid_pending_count_is_rejected(self) -> None:
        for value in (-1, True, "1"):
            with self.subTest(value=value):
                coordinator = BackendResetInteractionCoordinator(lambda: value)
                with self.assertRaises(RuntimeError):
                    coordinator.prepare_all()


if __name__ == "__main__":
    unittest.main()
