from __future__ import annotations

import unittest

from bot.operational_warnings import (
    FocusRuntimeTaskObserver,
    OperationalWarningRegistry,
)
from bot.runtime_loop import RuntimeTaskObservation


class OperationalWarningRegistryTests(unittest.TestCase):
    def test_repeated_signal_is_coalesced_and_moved_to_front(self) -> None:
        now = [1.0]
        registry = OperationalWarningRegistry(
            limit=3,
            ttl_seconds=60,
            clock=lambda: now[0],
        )
        registry.record(
            code="slow", source="runtime", message="first", details={"seconds": 1}
        )
        now[0] = 2.0
        registry.record(code="other", source="runtime", message="second")
        now[0] = 3.0
        registry.record(
            code="slow", source="runtime", message="first", details={"seconds": 2}
        )

        warnings = registry.snapshot()

        self.assertEqual([item["code"] for item in warnings], ["slow", "other"])
        self.assertEqual(warnings[0]["occurrences"], 2)
        self.assertEqual(warnings[0]["first_seen_at"], 1.0)
        self.assertEqual(warnings[0]["last_seen_at"], 3.0)
        self.assertEqual(warnings[0]["details"], {"seconds": 2})
        self.assertEqual(warnings[0]["severity"], "warning")
        self.assertEqual(warnings[0]["attention"], "correctness")

    def test_coalescing_can_upgrade_but_never_downgrades_attention_or_severity(
        self,
    ) -> None:
        registry = OperationalWarningRegistry()
        registry.record(
            code="shared",
            source="runtime",
            message="same family",
            severity="error",
            attention="correctness",
        )
        registry.record(
            code="shared",
            source="runtime",
            message="same family",
            severity="warning",
            attention="advisory",
        )

        warning = registry.snapshot()[0]
        self.assertEqual(warning["severity"], "error")
        self.assertEqual(warning["attention"], "correctness")

        registry.record(
            code="upgrade",
            source="runtime",
            message="another family",
            severity="warning",
            attention="advisory",
        )
        registry.record(
            code="upgrade",
            source="runtime",
            message="another family",
            severity="error",
            attention="correctness",
        )
        upgraded = registry.snapshot()[0]
        self.assertEqual(upgraded["severity"], "error")
        self.assertEqual(upgraded["attention"], "correctness")

    def test_registry_evicts_oldest_warning_family(self) -> None:
        registry = OperationalWarningRegistry(limit=2)
        registry.record(code="one", source="test", message="one")
        registry.record(code="two", source="test", message="two")
        registry.record(code="three", source="test", message="three")

        self.assertEqual(
            [item["code"] for item in registry.snapshot()],
            ["three", "two"],
        )

    def test_warning_expires_at_ttl_boundary_and_recovery_resets_its_family(
        self,
    ) -> None:
        now = [10.0]
        registry = OperationalWarningRegistry(
            ttl_seconds=5,
            clock=lambda: now[0],
        )
        registry.record(code="slow", source="runtime", message="slow task")

        now[0] = 14.999
        self.assertEqual(len(registry.snapshot()), 1)
        now[0] = 15.0
        self.assertEqual(registry.snapshot(), [])

        now[0] = 20.0
        registry.record(code="slow", source="runtime", message="slow task")
        recovered = registry.snapshot()
        self.assertEqual(recovered[0]["occurrences"], 1)
        self.assertEqual(recovered[0]["first_seen_at"], 20.0)
        self.assertEqual(recovered[0]["last_seen_at"], 20.0)

    def test_record_discards_an_expired_family_before_coalescing(self) -> None:
        now = [1.0]
        registry = OperationalWarningRegistry(
            ttl_seconds=2,
            clock=lambda: now[0],
        )
        registry.record(code="slow", source="runtime", message="slow task")
        now[0] = 3.0
        registry.record(code="slow", source="runtime", message="slow task")

        warning = registry.snapshot()[0]
        self.assertEqual(warning["occurrences"], 1)
        self.assertEqual(warning["first_seen_at"], 3.0)

    def test_invalid_warning_contract_is_rejected(self) -> None:
        registry = OperationalWarningRegistry()
        with self.assertRaises(ValueError):
            registry.record(code="", source="runtime", message="missing code")
        with self.assertRaises(ValueError):
            registry.record(
                code="bad", source="runtime", message="bad", severity="info"
            )
        with self.assertRaises(ValueError):
            registry.record(
                code="bad",
                source="runtime",
                message="bad",
                attention="urgent",
            )
        with self.assertRaises(ValueError):
            OperationalWarningRegistry(ttl_seconds=0)

    def test_runtime_task_observer_projects_only_crossed_thresholds(self) -> None:
        registry = OperationalWarningRegistry()
        observer = FocusRuntimeTaskObserver(registry, 1.0, 5.0)

        observer(RuntimeTaskObservation("fast", 0.9, 4.9, failed=False))
        observer(
            RuntimeTaskObservation(
                "slow",
                1.25,
                5.5,
                failed=False,
                queue_depth_at_enqueue=3,
                active_task_at_enqueue="blocking_task",
                active_task_age_seconds_at_enqueue=2.125,
            )
        )

        warnings = {warning["code"]: warning for warning in registry.snapshot()}
        self.assertEqual(
            set(warnings),
            {"runtime_queue_delay", "runtime_task_slow"},
        )
        self.assertEqual(warnings["runtime_queue_delay"]["attention"], "advisory")
        self.assertEqual(
            warnings["runtime_queue_delay"]["details"],
            {
                "waiting_task": "slow",
                "queue_depth_at_enqueue": 3,
                "active_task_at_enqueue": "blocking_task",
                "active_task_age_seconds_at_enqueue": 2.125,
                "queue_age_seconds": 1.25,
                "threshold_seconds": 1.0,
            },
        )
        self.assertEqual(warnings["runtime_task_slow"]["attention"], "advisory")
        self.assertEqual(
            warnings["runtime_task_slow"]["details"],
            {
                "running_task": "slow",
                "queue_depth_at_enqueue": 3,
                "active_task_at_enqueue": "blocking_task",
                "active_task_age_seconds_at_enqueue": 2.125,
                "queue_age_seconds": 1.25,
                "task_duration_seconds": 5.5,
                "threshold_seconds": 5.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
