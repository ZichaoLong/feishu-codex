"""Owner-level tests for the Feishu binding execution queue."""

from __future__ import annotations

import unittest
from dataclasses import replace

from bot.feishu_execution_queue import (
    FeishuBindingExecutionSnapshot,
    FeishuExecutionQueueAdmissionError,
    FeishuExecutionQueueController,
    FeishuExecutionQueueReceiptError,
    FeishuQueueDrainReceipt,
    StartCompactEffect,
    StartPromptEffect,
)
from bot.runtime_loop import RuntimeLoopContextError


class _RuntimeGuard:
    def __init__(self) -> None:
        self.allowed = True

    def __call__(self) -> None:
        if not self.allowed:
            raise RuntimeLoopContextError("outside RuntimeLoop")


class FeishuExecutionQueueControllerTest(unittest.TestCase):
    binding = ("user-a", "chat-a")

    def setUp(self) -> None:
        self.guard = _RuntimeGuard()
        self.queue = FeishuExecutionQueueController(
            runtime_context_guard=self.guard,
        )

    def snapshot(
        self,
        root_thread_id: str = "root-a",
        *,
        active: bool = True,
        attached: bool = True,
        inflight: bool = True,
        binding: tuple[str, str] | None = None,
    ) -> FeishuBindingExecutionSnapshot:
        return FeishuBindingExecutionSnapshot(
            binding=binding or self.binding,
            root_thread_id=root_thread_id,
            active=active,
            attached=attached,
            has_inflight_execution=inflight,
            current_turn_id="turn-a" if inflight else "",
        )

    def enqueue_prompt(
        self,
        message_id: str,
        *,
        root_thread_id: str = "root-a",
        text: str = "queued prompt",
        binding: tuple[str, str] | None = None,
    ):
        effective_binding = binding or self.binding
        return self.queue.enqueue_prompt(
            self.snapshot(
                root_thread_id,
                binding=effective_binding,
            ),
            sender_id=effective_binding[0],
            chat_id=effective_binding[1],
            message_id=message_id,
            text=text,
            input_items=({"type": "text", "text": text},),
        )

    def test_fifo_emits_typed_effects_and_exact_completion(self) -> None:
        first = self.enqueue_prompt("message-1", text="first")
        compact = self.queue.enqueue_compact(
            self.snapshot(),
            sender_id=self.binding[0],
            chat_id=self.binding[1],
            message_id="message-2",
        )
        self.assertEqual(first.queue_position, 1)
        self.assertEqual(compact.queue_position, 2)
        self.assertEqual(first.binding_epoch, compact.binding_epoch)

        idle = self.snapshot(inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle)
        self.assertIsInstance(effect, StartPromptEffect)
        self.assertEqual(effect.text, "first")
        self.assertTrue(self.queue.receipt_may_execute(effect.receipt, idle))
        completed = self.queue.complete_drain(
            effect.receipt,
            outcome="known_no_effect_settled",
        )
        self.assertTrue(completed.continue_same_epoch)
        self.assertEqual(completed.terminal_reconcile_root_thread_id, "")

        effect = self.queue.begin_terminal_drain(self.binding, idle)
        self.assertIsInstance(effect, StartCompactEffect)
        completed = self.queue.complete_drain(
            effect.receipt,
            outcome="known_no_effect_settled",
        )
        self.assertFalse(completed.continue_same_epoch)
        self.assertEqual(
            completed.terminal_reconcile_root_thread_id,
            "root-a",
        )
        self.assertFalse(self.queue.snapshot(self.binding).has_pending_or_draining)

    def test_started_head_waits_for_a_later_terminal_before_next_head(self) -> None:
        self.enqueue_prompt("message-1", text="first")
        self.enqueue_prompt("message-2", text="second")
        idle = self.snapshot(inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle)
        completed = self.queue.complete_drain(effect.receipt, outcome="started")

        self.assertFalse(completed.continue_same_epoch)
        self.assertIsNone(
            self.queue.begin_terminal_drain(
                self.binding,
                replace(idle, has_inflight_execution=True),
            )
        )
        next_effect = self.queue.begin_terminal_drain(self.binding, idle)
        self.assertIsInstance(next_effect, StartPromptEffect)
        self.assertEqual(next_effect.message_id, "message-2")

    def test_exact_receipt_rejects_forgery_replay_and_other_owner(self) -> None:
        self.enqueue_prompt("message-1")
        idle = self.snapshot(inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle)
        forged = FeishuQueueDrainReceipt(
            effect.receipt._issuer_nonce,
            effect.receipt._token_nonce,
        )
        other = FeishuExecutionQueueController(runtime_context_guard=self.guard)

        with self.assertRaises(FeishuExecutionQueueReceiptError):
            self.queue.complete_drain(forged, outcome="dropped")
        with self.assertRaises(FeishuExecutionQueueReceiptError):
            other.complete_drain(effect.receipt, outcome="dropped")
        self.queue.complete_drain(effect.receipt, outcome="dropped")
        with self.assertRaises(FeishuExecutionQueueReceiptError):
            self.queue.complete_drain(effect.receipt, outcome="dropped")

    def test_binding_epoch_blocks_a_to_b_to_a_receipt_replay(self) -> None:
        old_admission = self.enqueue_prompt("old-a", root_thread_id="root-a")
        idle_a = self.snapshot("root-a", inflight=False)
        old_effect = self.queue.begin_terminal_drain(self.binding, idle_a)

        first_invalidation = self.queue.invalidate_binding(self.binding)
        self.enqueue_prompt("new-b", root_thread_id="root-b")
        second_invalidation = self.queue.invalidate_binding(self.binding)
        new_admission = self.enqueue_prompt("new-a", root_thread_id="root-a")
        fresh_idle_a = self.snapshot("root-a", inflight=False)

        self.assertLess(old_admission.binding_epoch, first_invalidation.binding_epoch)
        self.assertLess(
            first_invalidation.binding_epoch, second_invalidation.binding_epoch
        )
        self.assertEqual(second_invalidation.binding_epoch, new_admission.binding_epoch)
        self.assertFalse(
            self.queue.receipt_may_execute(old_effect.receipt, fresh_idle_a)
        )
        cancelled = self.queue.complete_drain(
            old_effect.receipt,
            outcome="known_no_effect_settled",
        )
        self.assertTrue(cancelled.cancelled_by_invalidation)
        self.assertFalse(cancelled.continue_same_epoch)

        new_effect = self.queue.begin_terminal_drain(self.binding, fresh_idle_a)
        self.assertIsInstance(new_effect, StartPromptEffect)
        self.assertEqual(new_effect.message_id, "new-a")

    def test_missing_or_detached_binding_snapshot_discards_old_fifo(self) -> None:
        for snapshot in (
            None,
            self.snapshot(inflight=False, active=False),
            self.snapshot(inflight=False, attached=False),
        ):
            with self.subTest(snapshot=snapshot):
                queue = FeishuExecutionQueueController(
                    runtime_context_guard=self.guard,
                )
                queue.enqueue_prompt(
                    self.snapshot(),
                    sender_id=self.binding[0],
                    chat_id=self.binding[1],
                    message_id="stale",
                    text="must not run",
                )
                self.assertIsNone(queue.begin_terminal_drain(self.binding, snapshot))
                self.assertFalse(queue.snapshot(self.binding).has_pending_or_draining)

    def test_root_change_discards_fifo_instead_of_following_binding(self) -> None:
        admission = self.enqueue_prompt("old-a", root_thread_id="root-a")
        self.assertIsNone(
            self.queue.begin_terminal_drain(
                self.binding,
                self.snapshot("root-b", inflight=False),
            )
        )
        after = self.queue.snapshot(self.binding)
        self.assertGreater(after.binding_epoch, admission.binding_epoch)
        self.assertEqual(after.root_thread_id, "root-b")
        self.assertFalse(after.has_pending_or_draining)

    def test_recall_of_claimed_head_can_continue_same_epoch(self) -> None:
        self.enqueue_prompt("recalled")
        self.enqueue_prompt("survivor")
        idle = self.snapshot(inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle)

        recall = self.queue.remove_recalled_message(
            chat_id=self.binding[1],
            message_id="recalled",
        )
        self.assertEqual(recall.removed_count, 1)
        self.assertEqual(recall.terminal_reconcile_root_thread_ids, ())
        self.assertFalse(self.queue.receipt_may_execute(effect.receipt, idle))
        completion = self.queue.complete_drain(
            effect.receipt,
            outcome="dropped",
        )
        self.assertFalse(completion.cancelled_by_invalidation)
        self.assertTrue(completion.continue_same_epoch)
        survivor = self.queue.begin_terminal_drain(self.binding, idle)
        self.assertEqual(survivor.message_id, "survivor")

    def test_mutation_guard_allows_local_prime_but_rejects_a_to_b_to_a(self) -> None:
        self.enqueue_prompt("old-a", root_thread_id="root-a")
        idle_a = self.snapshot("root-a", inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle_a)

        self.assertTrue(
            self.queue.claimed_receipt_may_mutate(
                effect.receipt,
                replace(idle_a, has_inflight_execution=True),
            )
        )

        self.queue.invalidate_binding(self.binding)
        self.enqueue_prompt("new-b", root_thread_id="root-b")
        self.queue.invalidate_binding(self.binding)
        self.enqueue_prompt("new-a", root_thread_id="root-a")

        self.assertFalse(
            self.queue.claimed_receipt_may_mutate(
                effect.receipt,
                replace(idle_a, has_inflight_execution=True),
            )
        )
        completion = self.queue.complete_drain(effect.receipt, outcome="blocked")
        self.assertTrue(completion.cancelled_by_invalidation)
        self.assertFalse(completion.continue_same_epoch)

    def test_blocked_head_is_consumed_without_advancing_or_terminal_reconcile(
        self,
    ) -> None:
        self.enqueue_prompt("blocked")
        self.enqueue_prompt("survivor")
        idle = self.snapshot(inflight=False)
        effect = self.queue.begin_terminal_drain(self.binding, idle)

        completion = self.queue.complete_drain(effect.receipt, outcome="blocked")

        self.assertFalse(completion.continue_same_epoch)
        self.assertEqual(completion.terminal_reconcile_root_thread_id, "")
        self.assertEqual(
            self.queue.snapshot(self.binding).pending_message_ids,
            ("survivor",),
        )

    def test_recall_of_last_unclaimed_head_returns_exact_terminal_root(self) -> None:
        self.enqueue_prompt("last-head")

        outcome = self.queue.remove_recalled_message(
            chat_id=self.binding[1],
            message_id="last-head",
        )

        self.assertEqual(outcome.removed_count, 1)
        self.assertEqual(
            outcome.terminal_reconcile_root_thread_ids,
            ("root-a",),
        )
        self.assertFalse(self.queue.snapshot(self.binding).has_pending_or_draining)
        repeated = self.queue.remove_recalled_message(
            chat_id=self.binding[1],
            message_id="last-head",
        )
        self.assertEqual(repeated.removed_count, 0)
        self.assertEqual(repeated.terminal_reconcile_root_thread_ids, ())

    def test_clear_all_owns_orphan_keys_and_cancels_active_receipt(self) -> None:
        orphan_binding = ("orphan-user", "orphan-chat")
        self.enqueue_prompt("draining")
        self.enqueue_prompt(
            "orphan",
            root_thread_id="orphan-root",
            binding=orphan_binding,
        )
        effect = self.queue.begin_terminal_drain(
            self.binding,
            self.snapshot(inflight=False),
        )

        self.assertEqual(self.queue.invalidate_all(), 2)
        self.assertFalse(self.queue.snapshot(orphan_binding).has_pending_or_draining)
        self.assertFalse(
            self.queue.receipt_may_execute(
                effect.receipt,
                self.snapshot(inflight=False),
            )
        )
        completion = self.queue.complete_drain(
            effect.receipt,
            outcome="dropped",
        )
        self.assertTrue(completion.cancelled_by_invalidation)

    def test_iterative_receipt_protocol_handles_two_thousand_dropped_heads(
        self,
    ) -> None:
        item_count = 2_000
        for index in range(item_count):
            self.enqueue_prompt(f"message-{index}")
        idle = self.snapshot(inflight=False)

        completed = 0
        while effect := self.queue.begin_terminal_drain(self.binding, idle):
            result = self.queue.complete_drain(effect.receipt, outcome="dropped")
            completed += 1
            if not result.continue_same_epoch:
                break

        self.assertEqual(completed, item_count)
        self.assertFalse(self.queue.snapshot(self.binding).has_pending_or_draining)

    def test_enqueue_requires_exact_active_attached_root_and_same_p2p_sender(
        self,
    ) -> None:
        invalid_snapshots = (
            self.snapshot(active=False),
            self.snapshot(attached=False),
            self.snapshot(inflight=False),
            self.snapshot(""),
        )
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                with self.assertRaises(FeishuExecutionQueueAdmissionError):
                    self.queue.enqueue_prompt(
                        snapshot,
                        sender_id=self.binding[0],
                        chat_id=self.binding[1],
                        text="rejected",
                    )
        with self.assertRaises(FeishuExecutionQueueAdmissionError):
            self.queue.enqueue_prompt(
                self.snapshot(),
                sender_id="another-user",
                chat_id=self.binding[1],
                text="rejected",
            )

    def test_one_root_cannot_hold_fifo_continuity_for_two_bindings(self) -> None:
        other_binding = ("user-b", "chat-b")
        active_a = self.snapshot(inflight=True)
        active_b = self.snapshot(inflight=True, binding=other_binding)
        first = self.queue.enqueue_prompt(
            active_a,
            sender_id=self.binding[0],
            chat_id=self.binding[1],
            message_id="a-head",
            text="first",
        )

        self.assertTrue(
            self.queue.has_other_binding_continuity(other_binding, "root-a")
        )
        with self.assertRaises(FeishuExecutionQueueAdmissionError):
            self.queue.enqueue_prompt(
                active_b,
                sender_id=other_binding[0],
                chat_id=other_binding[1],
                message_id="b-rejected",
                text="second binding",
            )

        invalidated = self.queue.invalidate_binding(self.binding)
        second = self.queue.enqueue_prompt(
            active_b,
            sender_id=other_binding[0],
            chat_id=other_binding[1],
            message_id="b-head",
            text="after release",
        )

        self.assertLess(first.binding_epoch, invalidated.binding_epoch)
        self.assertEqual(second.queue_position, 1)
        self.assertFalse(
            self.queue.has_other_binding_continuity(other_binding, "root-a")
        )
        self.assertEqual(
            self.queue.snapshot(other_binding).pending_message_ids,
            ("b-head",),
        )

    def test_runtime_context_guard_covers_commands_and_observation(self) -> None:
        self.guard.allowed = False
        with self.assertRaises(RuntimeLoopContextError):
            self.queue.snapshot(self.binding)
        with self.assertRaises(RuntimeLoopContextError):
            self.enqueue_prompt("blocked")


if __name__ == "__main__":
    unittest.main()
