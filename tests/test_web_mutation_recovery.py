from __future__ import annotations

import unittest
import uuid

from bot.web_runtime.mutation_recovery import (
    WEB_MUTATION_ACTIVE_LIMIT,
    WEB_MUTATION_BACKEND_RETIREMENT_LIMIT,
    WEB_MUTATION_SETTLEMENT_LIMIT,
    WebLifecycleVerification,
    WebMutationRecoveryRegistry,
    WebUnknownMutation,
    is_web_mutation_id,
)


class WebMutationRecoveryRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.in_runtime = True
        self.guard_calls = 0

        def require_runtime() -> None:
            self.guard_calls += 1
            if not self.in_runtime:
                raise RuntimeError("outside RuntimeLoop")

        self.registry = WebMutationRecoveryRegistry(
            runtime_context_guard=require_runtime,
        )

    @staticmethod
    def _mutation(
        thread_id: str = "thread-1",
        *,
        operation: str = "compact",
        client_id: str = "tab-1",
        mutation_id: str = "",
        turn_id: str = "",
        baseline_turn_ids: tuple[str, ...] = (),
    ) -> WebUnknownMutation:
        return WebUnknownMutation.create(
            thread_id=thread_id,
            operation=operation,
            client_id=client_id,
            durability="process_local",
            mutation_id=mutation_id,
            turn_id=turn_id,
            baseline_turn_ids=baseline_turn_ids,
        )

    def test_requires_callable_guard_and_checks_context_first(self) -> None:
        with self.assertRaises(TypeError):
            WebMutationRecoveryRegistry(runtime_context_guard=None)  # type: ignore[arg-type]

        self.in_runtime = False
        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.registry.remember(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            self.registry.get("")

    def test_compact_and_interrupt_use_only_typed_positive_turn_evidence(self) -> None:
        compact = self._mutation(
            "compact-thread",
            operation="compact",
            baseline_turn_ids=("old-turn",),
        )
        interrupt = self._mutation(
            "interrupt-thread",
            operation="interrupt",
            turn_id="turn-3",
        )
        self.registry.remember(compact)
        self.registry.remember(interrupt)

        compact_result = self.registry.reconcile_turns(
            compact.thread_id,
            [{"id": "new-turn", "status": "completed", "items": []}],
        )
        interrupt_result = self.registry.reconcile_turns(
            interrupt.thread_id,
            [{"id": "turn-3", "status": "interrupted", "items": []}],
        )

        self.assertEqual(compact_result[0].disposition, "effect_observed")
        self.assertEqual(interrupt_result[0].mutation_id, interrupt.mutation_id)
        self.assertFalse(self.registry.contains(compact.thread_id))
        self.assertFalse(self.registry.contains(interrupt.thread_id))

    def test_unmatched_evidence_preserves_the_exact_unknown_attempt(self) -> None:
        pending = self._mutation(
            operation="interrupt",
            turn_id="expected-turn",
        )
        self.registry.remember(pending)

        self.assertEqual(
            self.registry.reconcile_turns(
                pending.thread_id,
                [{"id": "other-turn", "status": "completed", "items": []}],
            ),
            (),
        )
        self.assertEqual(self.registry.get_exact(pending.thread_id, pending.mutation_id), pending)

    def test_settlement_and_lifecycle_verification_are_exact_and_owner_scoped(self) -> None:
        archive = self._mutation(operation="archive")
        other = self._mutation(
            "thread-2",
            operation="delete",
            client_id="tab-2",
        )
        self.registry.remember(archive)
        self.registry.remember(other)
        verification = WebLifecycleVerification(
            state="archived",
            verification_id="verification-1",
        )

        updated = self.registry.install_lifecycle_verification(
            archive.thread_id,
            archive.mutation_id,
            verification,
        )
        settlement = self.registry.settle_exact(
            archive.thread_id,
            archive.mutation_id,
            "user_discard",
        )

        self.assertIsNotNone(updated)
        self.assertEqual(settlement.client_id, "tab-1")  # type: ignore[union-attr]
        self.assertEqual(
            self.registry.lifecycle_projections_for_client("tab-2"),
            [
                {
                    "mutation_id": other.mutation_id,
                    "thread_id": "thread-2",
                    "operation": "delete",
                    "verification": None,
                }
            ],
        )
        self.assertIsNone(
            self.registry.install_lifecycle_verification(
                archive.thread_id,
                archive.mutation_id,
                verification,
            )
        )

    def test_backend_retirement_moves_every_active_control_to_bounded_evidence(self) -> None:
        mutations = [
            self._mutation(f"thread-{index}", operation="archive")
            for index in range(WEB_MUTATION_BACKEND_RETIREMENT_LIMIT + 1)
        ]
        for mutation in mutations:
            self.registry.remember(mutation)
            if len(self.registry.snapshot()) == WEB_MUTATION_ACTIVE_LIMIT:
                self.registry.retire_backend_epoch_after_stop()

        receipt = self.registry.retire_backend_epoch_after_stop()

        self.assertLessEqual(receipt.retired_count, WEB_MUTATION_ACTIVE_LIMIT)
        self.assertEqual(self.registry.snapshot(), ())
        self.assertIsNone(
            self.registry.backend_retirement_exact(
                mutations[0].thread_id,
                mutations[0].mutation_id,
            )
        )
        self.assertIsNotNone(
            self.registry.backend_retirement_exact(
                mutations[-1].thread_id,
                mutations[-1].mutation_id,
            )
        )

    def test_active_and_terminal_histories_are_independently_bounded(self) -> None:
        for index in range(WEB_MUTATION_ACTIVE_LIMIT):
            self.registry.remember(self._mutation(f"active-{index}"))
        with self.assertRaisesRegex(RuntimeError, "capacity"):
            self.registry.remember(self._mutation("overflow"))

        registry = WebMutationRecoveryRegistry(runtime_context_guard=lambda: None)
        mutations = [
            self._mutation(f"settled-{index}")
            for index in range(WEB_MUTATION_SETTLEMENT_LIMIT + 1)
        ]
        for mutation in mutations:
            registry.remember(mutation)
            registry.settle_exact(
                mutation.thread_id,
                mutation.mutation_id,
                "user_discard",
            )
        self.assertIsNone(
            registry.settlement_exact(
                mutations[0].thread_id,
                mutations[0].mutation_id,
            )
        )
        self.assertIsNotNone(
            registry.settlement_exact(
                mutations[-1].thread_id,
                mutations[-1].mutation_id,
            )
        )

    def test_mutation_id_requires_canonical_uuid_text(self) -> None:
        canonical = str(uuid.uuid4())
        self.assertTrue(is_web_mutation_id(canonical))
        self.assertFalse(is_web_mutation_id(canonical.upper()))
        self.assertFalse(is_web_mutation_id("not-a-uuid"))


if __name__ == "__main__":
    unittest.main()
