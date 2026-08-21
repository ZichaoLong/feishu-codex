"""Composition-level regressions for Feishu execution queue transactions."""

from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from typing import Callable
from unittest.mock import patch

from bot.feishu_compact_execution_service import FeishuCompactStartResult
from bot.feishu_execution_queue import (
    FeishuBindingExecutionSnapshot,
    FeishuExecutionQueueController,
    FeishuQueuedMessageOrigin,
)
from bot.feishu_execution_queue_service import (
    FeishuExecutionQueueService,
    FeishuExecutionQueueServicePorts,
    FeishuQueueIngressSnapshot,
)
from bot.prompt_input_items import replace_text_input_items
from bot.prompt_turn_entry_controller import PromptTurnStartResult
from bot.reason_codes import PROMPT_DENIED_BY_INTERACTION_OWNER, ReasonedCheck


class _QueueServiceEnvironment:
    binding = ("user-a", "chat-a")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.execution_snapshot = FeishuBindingExecutionSnapshot(
            binding=self.binding,
            root_thread_id="root-a",
            active=True,
            attached=True,
            has_inflight_execution=True,
            current_turn_id="turn-a",
        )
        self.ingress = FeishuQueueIngressSnapshot(
            binding=self.binding,
            current_root_thread_id="root-a",
            current_turn_id="turn-a",
            has_execution_anchor=True,
        )
        self.preparations: list[str] = []
        self.prompt_starts: list[str] = []
        self.prompt_input_items: list[list[dict[str, object]]] = []
        self.compact_starts: list[str] = []
        self.replies: list[str] = []
        self.terminal_reconciliations: list[str] = []
        self.reconcile_lock_states: list[bool] = []
        self.preparation_hook: Callable[[str], None] | None = None
        self.prepared_message_ids = {"survivor"}
        self.queue = FeishuExecutionQueueController(
            runtime_context_guard=lambda: None,
        )
        self.service = FeishuExecutionQueueService(
            queue=self.queue,
            ports=FeishuExecutionQueueServicePorts(
                lock=self.lock,
                ingress_snapshot=lambda *_args: self.ingress,
                binding_execution_snapshot_locked=(
                    lambda _binding: self.execution_snapshot
                ),
                binding_execution_active_locked=(
                    lambda _binding: self.execution_snapshot.has_inflight_execution
                ),
                writer_denial_text=lambda *_args: "",
                current_process_local_turn_id=lambda _thread_id: "",
                prompt_queue_admission_check=lambda *_args: ReasonedCheck.allow(),
                start_prompt=self._start_prompt,
                start_compact=self._start_compact,
                load_message_context=lambda _message_id: {},
                remember_message_context=lambda _message_id, _context: None,
                prepare_queued_prompt_text=self._prepare_prompt,
                reply_text=lambda _chat_id, text, **_kwargs: self.replies.append(text),
                reconcile_terminal=self._reconcile_terminal,
            ),
            runtime_context_guard=lambda: None,
        )

    def _prepare_prompt(self, *, message_id: str, text: str, **_kwargs):
        self.preparations.append(message_id)
        if self.preparation_hook is not None:
            self.preparation_hook(message_id)
        if message_id not in self.prepared_message_ids:
            return None
        return text

    def _reconcile_terminal(self, root_thread_id: str) -> None:
        self.reconcile_lock_states.append(self.lock._is_owned())
        self.terminal_reconciliations.append(root_thread_id)

    def _start_prompt(self, *_args, message_id: str = "", **kwargs):
        guard = kwargs.get("exact_admission_guard")
        if guard is not None and not guard():
            return PromptTurnStartResult(
                started=False,
                reason_code="stale_queue_admission",
                disposition="known_no_effect_settled",
            )
        mutation_guard = kwargs.get("exact_mutation_guard")
        if mutation_guard is not None and not mutation_guard():
            return PromptTurnStartResult(
                started=False,
                reason_code="stale_queue_admission",
                disposition="known_no_effect_settled",
            )
        self.prompt_starts.append(message_id)
        self.prompt_input_items.append(list(kwargs.get("input_items") or ()))
        self.execution_snapshot = replace(
            self.execution_snapshot,
            has_inflight_execution=True,
        )
        return PromptTurnStartResult(
            started=True,
            thread_id=self.execution_snapshot.root_thread_id,
            turn_id="turn-survivor",
            disposition="started",
        )

    def _start_compact(self, *_args, message_id: str = "", **kwargs):
        mutation_guard = kwargs.get("exact_mutation_guard")
        if mutation_guard is not None and not mutation_guard():
            return FeishuCompactStartResult(
                accepted=False,
                started=False,
                binding_id="p2p:user-a:chat-a",
                thread_id=self.execution_snapshot.root_thread_id,
                disposition="known_no_effect_settled",
            )
        self.compact_starts.append(message_id)
        return FeishuCompactStartResult(
            accepted=True,
            started=True,
            binding_id="p2p:user-a:chat-a",
            thread_id=self.execution_snapshot.root_thread_id,
            disposition="started",
        )


class FeishuExecutionQueueServiceTests(unittest.TestCase):
    def test_queued_plain_text_preserves_default_input_item(self) -> None:
        env = _QueueServiceEnvironment()

        result = env.service.start_or_enqueue_prompt(
            env.binding[0],
            env.binding[1],
            "plain queued prompt",
            message_id="survivor",
        )
        self.assertTrue(result["queued"])
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )

        env.service.drain(env.binding)

        self.assertEqual(
            env.prompt_input_items,
            [[{"type": "text", "text": "plain queued prompt"}]],
        )

    def test_preclaim_failure_invalidates_generation_and_reconciles_old_root(
        self,
    ) -> None:
        for failure_site in ("initial_snapshot", "begin_drain"):
            with self.subTest(failure_site=failure_site):
                env = _QueueServiceEnvironment()
                for message_id in ("orphaned-head", "orphaned-successor"):
                    env.queue.enqueue_prompt(
                        env.execution_snapshot,
                        sender_id=env.binding[0],
                        chat_id=env.binding[1],
                        message_id=message_id,
                        text="must not survive",
                    )
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    has_inflight_execution=False,
                )
                if failure_site == "initial_snapshot":

                    def fail_snapshot(_binding):
                        raise RuntimeError("snapshot unavailable")

                    env.service._ports = replace(
                        env.service._ports,
                        binding_execution_snapshot_locked=fail_snapshot,
                    )
                    begin_drain = patch.object(
                        env.queue,
                        "begin_terminal_drain",
                        wraps=env.queue.begin_terminal_drain,
                    )
                else:
                    begin_drain = patch.object(
                        env.queue,
                        "begin_terminal_drain",
                        side_effect=RuntimeError("begin drain failed"),
                    )

                with (
                    begin_drain as observed_begin,
                    patch.object(
                        env.queue,
                        "complete_drain",
                        wraps=env.queue.complete_drain,
                    ) as complete_drain,
                ):
                    env.service.drain(env.binding)

                if failure_site == "initial_snapshot":
                    observed_begin.assert_not_called()
                else:
                    observed_begin.assert_called_once()
                complete_drain.assert_not_called()
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )
                self.assertEqual(env.prompt_starts, [])
                self.assertEqual(env.terminal_reconciliations, ["root-a"])
                self.assertEqual(env.reconcile_lock_states, [False])

    def test_enqueue_confirmation_failure_keeps_single_committed_item(self) -> None:
        env = _QueueServiceEnvironment()

        def fail_reply(*_args, **_kwargs):
            raise RuntimeError("presentation unavailable")

        env.service._ports = replace(env.service._ports, reply_text=fail_reply)

        result = env.service.start_or_enqueue_prompt(
            env.binding[0],
            env.binding[1],
            "run later",
            message_id="queued-once",
        )

        self.assertTrue(result["queued"])
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(
            env.queue.snapshot(env.binding).pending_message_ids,
            ("queued-once",),
        )

    def test_denial_reply_failure_preserves_typed_result_and_empty_queue(
        self,
    ) -> None:
        cases = (
            ("writer", PROMPT_DENIED_BY_INTERACTION_OWNER),
            ("stale", "stale_queue_admission"),
            ("admission", "prompt_denied_by_running_turn"),
        )
        for case, reason_code in cases:
            with self.subTest(case=case):
                env = _QueueServiceEnvironment()

                def fail_reply(*_args, **_kwargs):
                    raise RuntimeError("presentation unavailable")

                ports = replace(env.service._ports, reply_text=fail_reply)
                display_mode = "silent"
                if case == "writer":
                    ports = replace(
                        ports,
                        prompt_queue_admission_check=lambda *_args: ReasonedCheck.deny(
                            PROMPT_DENIED_BY_INTERACTION_OWNER,
                            "writer denied",
                        ),
                    )
                elif case == "stale":
                    env.execution_snapshot = replace(
                        env.execution_snapshot,
                        root_thread_id="root-b",
                    )
                else:
                    display_mode = "unsupported"
                env.service._ports = ports

                result = env.service.start_or_enqueue_prompt(
                    env.binding[0],
                    env.binding[1],
                    "must not queue",
                    message_id=f"denied-{case}",
                    display_mode=display_mode,
                )

                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason_code"], reason_code)
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )

    def test_recall_last_unclaimed_head_reconciles_outside_queue_lock(self) -> None:
        env = _QueueServiceEnvironment()
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="last-head",
            text="do not run",
        )

        outcome = env.service.remove_recalled_message(
            chat_id=env.binding[1],
            message_id="last-head",
        )

        self.assertEqual(outcome.removed_count, 1)
        self.assertEqual(outcome.terminal_reconcile_root_thread_ids, ("root-a",))
        self.assertEqual(env.terminal_reconciliations, ["root-a"])
        self.assertEqual(env.reconcile_lock_states, [False])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_recall_claimed_head_leaves_settlement_to_exact_drain(self) -> None:
        env = _QueueServiceEnvironment()
        env.prepared_message_ids.add("claimed-head")
        recall_outcomes = []

        def recall_claimed(message_id: str) -> None:
            recall_outcomes.append(
                env.service.remove_recalled_message(
                    chat_id=env.binding[1],
                    message_id=message_id,
                )
            )

        env.preparation_hook = recall_claimed
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="claimed-head",
            text="must not start",
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )

        env.service.drain(env.binding)

        self.assertEqual(len(recall_outcomes), 1)
        self.assertEqual(recall_outcomes[0].removed_count, 1)
        self.assertEqual(
            recall_outcomes[0].terminal_reconcile_root_thread_ids,
            (),
        )
        self.assertEqual(env.prompt_starts, [])
        self.assertEqual(env.terminal_reconciliations, ["root-a"])
        self.assertEqual(env.reconcile_lock_states, [False])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_one_drain_handles_two_thousand_drops_then_one_survivor(self) -> None:
        env = _QueueServiceEnvironment()
        for index in range(2_000):
            env.queue.enqueue_prompt(
                env.execution_snapshot,
                sender_id=env.binding[0],
                chat_id=env.binding[1],
                message_id=f"drop-{index}",
                text="drop",
            )
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="survivor",
            text="run once",
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )

        env.service.drain(env.binding)

        self.assertEqual(len(env.preparations), 2_001)
        self.assertEqual(env.prompt_starts, ["survivor"])
        self.assertTrue(env.execution_snapshot.has_inflight_execution)
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_prompt_and_compact_reject_locked_root_replacement_at_enqueue(self) -> None:
        for kind in ("prompt", "compact"):
            with self.subTest(kind=kind):
                env = _QueueServiceEnvironment()
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    root_thread_id="root-b",
                )

                if kind == "prompt":
                    result = env.service.start_or_enqueue_prompt(
                        env.binding[0],
                        env.binding[1],
                        "must not follow root-b",
                        message_id="stale-prompt",
                    )
                else:
                    result = env.service.start_or_enqueue_compact(
                        env.binding[0],
                        env.binding[1],
                        message_id="stale-compact",
                    )

                self.assertEqual(result["reason_code"], "stale_queue_admission")
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )
                self.assertEqual(env.prompt_starts, [])
                self.assertEqual(env.compact_starts, [])

    def test_prompt_rejects_locked_turn_or_binding_replacement_before_policy(
        self,
    ) -> None:
        for case in ("turn", "binding"):
            with self.subTest(case=case):
                env = _QueueServiceEnvironment()
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    **(
                        {"current_turn_id": "turn-b"}
                        if case == "turn"
                        else {"binding": ("user-b", "chat-b")}
                    ),
                )
                policy_calls: list[bool] = []
                env.service._ports = replace(
                    env.service._ports,
                    prompt_queue_admission_check=lambda *_args: (
                        policy_calls.append(env.lock._is_owned())
                        or ReasonedCheck.allow()
                    ),
                )

                result = env.service.start_or_enqueue_prompt(
                    env.binding[0],
                    env.binding[1],
                    "must not follow replacement",
                    message_id=f"stale-{case}",
                )

                self.assertEqual(result["reason_code"], "stale_queue_admission")
                self.assertEqual(policy_calls, [])
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )

    def test_prompt_policy_and_enqueue_share_the_runtime_lock(self) -> None:
        env = _QueueServiceEnvironment()
        lock_states: list[tuple[str, bool]] = []

        def policy_check(*_args):
            lock_states.append(("policy", env.lock._is_owned()))
            return ReasonedCheck.allow()

        original_enqueue = env.queue.enqueue_prompt

        def enqueue(*args, **kwargs):
            lock_states.append(("enqueue", env.lock._is_owned()))
            return original_enqueue(*args, **kwargs)

        env.service._ports = replace(
            env.service._ports,
            prompt_queue_admission_check=policy_check,
        )
        with patch.object(env.queue, "enqueue_prompt", side_effect=enqueue):
            result = env.service.start_or_enqueue_prompt(
                env.binding[0],
                env.binding[1],
                "queue atomically",
                message_id="locked-enqueue",
            )

        self.assertTrue(result["queued"])
        self.assertEqual(lock_states, [("policy", True), ("enqueue", True)])

    def test_prompt_rechecks_changed_owner_under_lock_before_enqueue(self) -> None:
        env = _QueueServiceEnvironment()
        policy_lock_states: list[bool] = []

        def changed_owner_check(*_args):
            policy_lock_states.append(env.lock._is_owned())
            return ReasonedCheck.deny(
                PROMPT_DENIED_BY_INTERACTION_OWNER,
                "owner changed before enqueue",
            )

        env.service._ports = replace(
            env.service._ports,
            prompt_queue_admission_check=changed_owner_check,
        )

        result = env.service.start_or_enqueue_prompt(
            env.binding[0],
            env.binding[1],
            "must not follow changed owner",
            message_id="owner-changed",
        )

        self.assertEqual(
            result["reason_code"],
            PROMPT_DENIED_BY_INTERACTION_OWNER,
        )
        self.assertEqual(policy_lock_states, [True])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_compact_keeps_the_ordinary_writer_denial_path(self) -> None:
        env = _QueueServiceEnvironment()
        prompt_policy_calls: list[bool] = []
        env.service._ports = replace(
            env.service._ports,
            writer_denial_text=lambda *_args: "compact writer denied",
            prompt_queue_admission_check=lambda *_args: (
                prompt_policy_calls.append(True) or ReasonedCheck.allow()
            ),
        )

        result = env.service.start_or_enqueue_compact(
            env.binding[0],
            env.binding[1],
            message_id="compact-denied",
        )

        self.assertEqual(result["reason_code"], "compact_denied_by_thread_owner")
        self.assertEqual(result["reason"], "compact writer denied")
        self.assertEqual(prompt_policy_calls, [])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_blocked_prompt_consumes_head_and_does_not_advance_survivor(self) -> None:
        env = _QueueServiceEnvironment()
        for message_id in ("blocked", "survivor"):
            env.queue.enqueue_prompt(
                env.execution_snapshot,
                sender_id=env.binding[0],
                chat_id=env.binding[1],
                message_id=message_id,
                text=message_id,
            )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )
        attempts: list[str] = []

        def blocked_start(*_args, message_id: str = "", **_kwargs):
            attempts.append(message_id)
            return PromptTurnStartResult(
                started=False,
                reason_code="owner_settlement_failed",
                disposition="blocked_unsettled",
            )

        env.service._ports = replace(
            env.service._ports,
            start_prompt=blocked_start,
            prepare_queued_prompt_text=lambda **kwargs: kwargs["text"],
        )

        env.service.drain(env.binding)

        self.assertEqual(attempts, ["blocked"])
        self.assertEqual(
            env.queue.snapshot(env.binding).pending_message_ids,
            ("survivor",),
        )
        self.assertEqual(env.terminal_reconciliations, [])

    def test_exact_local_turn_lease_queues_before_feishu_mirror_projection(
        self,
    ) -> None:
        env = _QueueServiceEnvironment()
        env.ingress = replace(
            env.ingress,
            current_turn_id="",
            has_execution_anchor=False,
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
            current_turn_id="",
        )
        policy_turn_ids: list[str] = []

        def must_not_start(*_args, **_kwargs):
            raise AssertionError("pre-projection exact lease was ignored")

        def policy_check(*args):
            policy_turn_ids.append(args[3])
            return ReasonedCheck.allow()

        env.service._ports = replace(
            env.service._ports,
            current_process_local_turn_id=lambda _root: "turn-local",
            prompt_queue_admission_check=policy_check,
            start_prompt=must_not_start,
        )

        result = env.service.start_or_enqueue_prompt(
            env.binding[0],
            env.binding[1],
            "arrived before mirror",
            message_id="preprojection-local",
            surface_failures=False,
        )

        self.assertTrue(result["queued"])
        self.assertEqual(policy_turn_ids, ["turn-local"])
        self.assertEqual(
            env.queue.snapshot(env.binding).pending_message_ids,
            ("preprojection-local",),
        )

    def test_preprojection_turn_proof_change_fails_closed_without_start(self) -> None:
        for replacement_turn_id in ("", "turn-replaced"):
            with self.subTest(replacement_turn_id=replacement_turn_id):
                env = _QueueServiceEnvironment()
                env.ingress = replace(
                    env.ingress,
                    current_turn_id="",
                    has_execution_anchor=False,
                )
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    has_inflight_execution=False,
                    current_turn_id="",
                )
                observed = iter(("turn-local", replacement_turn_id))

                def must_not_start(*_args, **_kwargs):
                    raise AssertionError("stale proof reached prompt start")

                env.service._ports = replace(
                    env.service._ports,
                    current_process_local_turn_id=lambda _root: next(observed),
                    start_prompt=must_not_start,
                )

                result = env.service.start_or_enqueue_prompt(
                    env.binding[0],
                    env.binding[1],
                    "must not follow a changed lease",
                    message_id="preprojection-stale",
                    surface_failures=False,
                )

                self.assertFalse(result["queued"])
                self.assertEqual(result["reason_code"], "stale_queue_admission")
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )

    def test_immediate_blocked_start_result_never_becomes_a_fifo_head(
        self,
    ) -> None:
        env = _QueueServiceEnvironment()
        env.ingress = replace(
            env.ingress,
            current_turn_id="",
            has_execution_anchor=False,
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
            current_turn_id="",
        )
        env.service._ports = replace(
            env.service._ports,
            start_prompt=lambda *_args, **_kwargs: PromptTurnStartResult(
                started=False,
                thread_id="root-a",
                reason_code="turn_start_failed",
                disposition="blocked_unsettled",
            ),
        )

        result = env.service.start_or_enqueue_prompt(
            env.binding[0],
            env.binding[1],
            "unknown effect",
            message_id="blocked-immediate",
            surface_failures=False,
        )

        self.assertFalse(result["queued"])
        self.assertEqual(result["reason_code"], "turn_start_failed")
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_other_binding_continuity_rejects_before_prompt_start(self) -> None:
        binding_a = ("user-a", "chat-a")
        binding_b = ("user-b", "chat-b")

        env = _QueueServiceEnvironment()
        snapshot_a = replace(
            env.execution_snapshot,
            binding=binding_a,
            has_inflight_execution=True,
            current_turn_id="turn-a",
        )
        snapshot_b = replace(snapshot_a, binding=binding_b)
        env.queue.enqueue_prompt(
            snapshot_a,
            sender_id=binding_a[0],
            chat_id=binding_a[1],
            message_id="a-head",
            text="first binding",
        )
        env.ingress = FeishuQueueIngressSnapshot(
            binding=binding_b,
            current_root_thread_id="root-a",
            current_turn_id="",
            has_execution_anchor=False,
        )
        start_attempts = 0

        def must_not_start(*_args, **_kwargs):
            nonlocal start_attempts
            start_attempts += 1
            raise AssertionError("other binding queue was bypassed")

        env.service._ports = replace(
            env.service._ports,
            binding_execution_snapshot_locked=lambda binding: (
                snapshot_a if binding == binding_a else snapshot_b
            ),
            start_prompt=must_not_start,
        )

        denied = env.service.start_or_enqueue_prompt(
            binding_b[0],
            binding_b[1],
            "must not create a second FIFO",
            message_id="b-denied-before",
            surface_failures=False,
        )

        self.assertFalse(denied["queued"])
        self.assertEqual(
            denied["reason_code"],
            PROMPT_DENIED_BY_INTERACTION_OWNER,
        )
        self.assertEqual(start_attempts, 0)

    def test_untyped_claimed_preflight_failure_blocks_without_retry_scheduler(
        self,
    ) -> None:
        cases = (
            "claimed_snapshot",
            "claimed_receipt",
            "effect_snapshot",
            "effect_receipt",
        )
        for failure_site in cases:
            with self.subTest(failure_site=failure_site):
                env = _QueueServiceEnvironment()
                env.prepared_message_ids.update({"blocked", "survivor"})
                for message_id in ("blocked", "survivor"):
                    env.queue.enqueue_prompt(
                        env.execution_snapshot,
                        sender_id=env.binding[0],
                        chat_id=env.binding[1],
                        message_id=message_id,
                        text=message_id,
                    )
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    has_inflight_execution=False,
                )
                snapshot_calls = 0
                receipt_calls = 0

                def snapshot_port(_binding):
                    nonlocal snapshot_calls
                    snapshot_calls += 1
                    target_call = 2 if failure_site == "claimed_snapshot" else 3
                    if (
                        failure_site.endswith("snapshot")
                        and snapshot_calls == target_call
                    ):
                        raise RuntimeError(f"{failure_site} unavailable")
                    return env.execution_snapshot

                original_receipt_check = env.queue.receipt_may_execute

                def receipt_check(receipt, snapshot):
                    nonlocal receipt_calls
                    receipt_calls += 1
                    target_call = 1 if failure_site == "claimed_receipt" else 2
                    if (
                        failure_site.endswith("receipt")
                        and receipt_calls == target_call
                    ):
                        raise RuntimeError(f"{failure_site} unavailable")
                    return original_receipt_check(receipt, snapshot)

                env.service._ports = replace(
                    env.service._ports,
                    binding_execution_snapshot_locked=snapshot_port,
                )
                with (
                    patch.object(
                        env.queue,
                        "receipt_may_execute",
                        side_effect=receipt_check,
                    ),
                    patch.object(
                        env.queue,
                        "complete_drain",
                        wraps=env.queue.complete_drain,
                    ) as complete_drain,
                ):
                    env.service.drain(env.binding)

                self.assertEqual(complete_drain.call_count, 1)
                self.assertEqual(
                    complete_drain.call_args.kwargs["outcome"],
                    "blocked",
                )
                self.assertEqual(env.prompt_starts, [])
                self.assertEqual(
                    env.queue.snapshot(env.binding).pending_message_ids,
                    ("survivor",),
                )
                self.assertEqual(env.terminal_reconciliations, [])

    def test_successful_prompt_announce_failure_never_replays_head(self) -> None:
        env = _QueueServiceEnvironment()
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="survivor",
            text="run once",
            display_mode="announce",
            synthetic_source="scheduler",
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )

        def fail_reply(*_args, **_kwargs):
            raise RuntimeError("presentation unavailable")

        env.service._ports = replace(env.service._ports, reply_text=fail_reply)

        env.service.drain(env.binding)
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )
        env.service.drain(env.binding)

        self.assertEqual(env.prompt_starts, ["survivor"])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_origin_restore_failure_is_best_effort_and_prompt_starts_once(
        self,
    ) -> None:
        env = _QueueServiceEnvironment()
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="survivor",
            text="run once",
            origin=FeishuQueuedMessageOrigin(sender_open_id="ou-origin"),
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )

        def fail_origin_restore(_message_id: str):
            raise RuntimeError("message context unavailable")

        env.service._ports = replace(
            env.service._ports,
            load_message_context=fail_origin_restore,
        )

        env.service.drain(env.binding)
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )
        env.service.drain(env.binding)

        self.assertEqual(env.prompt_starts, ["survivor"])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_prepare_failure_is_terminal_known_no_effect_even_if_reply_fails(
        self,
    ) -> None:
        env = _QueueServiceEnvironment()
        env.queue.enqueue_prompt(
            env.execution_snapshot,
            sender_id=env.binding[0],
            chat_id=env.binding[1],
            message_id="broken-preparation",
            text="must not run",
        )
        env.execution_snapshot = replace(
            env.execution_snapshot,
            has_inflight_execution=False,
        )
        preparation_attempts = 0

        def fail_preparation(**_kwargs):
            nonlocal preparation_attempts
            preparation_attempts += 1
            raise RuntimeError("assistant history unavailable")

        def fail_reply(*_args, **_kwargs):
            raise RuntimeError("presentation unavailable")

        env.service._ports = replace(
            env.service._ports,
            prepare_queued_prompt_text=fail_preparation,
            reply_text=fail_reply,
        )

        with patch.object(
            env.queue,
            "complete_drain",
            wraps=env.queue.complete_drain,
        ) as complete_drain:
            env.service.drain(env.binding)
            env.service.drain(env.binding)

        self.assertEqual(preparation_attempts, 1)
        self.assertEqual(env.prompt_starts, [])
        self.assertEqual(complete_drain.call_count, 1)
        self.assertEqual(
            complete_drain.call_args.kwargs["outcome"],
            "known_no_effect_settled",
        )
        self.assertEqual(env.terminal_reconciliations, ["root-a"])
        self.assertFalse(env.queue.snapshot(env.binding).has_pending_or_draining)

    def test_prepared_text_normalization_failure_is_known_no_effect(self) -> None:
        class ExplodingText:
            def __str__(self) -> str:
                raise RuntimeError("prepared text cannot be stringified")

        for failure_site in ("stringify", "input_replacement"):
            with self.subTest(failure_site=failure_site):
                env = _QueueServiceEnvironment()
                env.queue.enqueue_prompt(
                    env.execution_snapshot,
                    sender_id=env.binding[0],
                    chat_id=env.binding[1],
                    message_id="broken-normalization",
                    text="original",
                )
                env.execution_snapshot = replace(
                    env.execution_snapshot,
                    has_inflight_execution=False,
                )
                prepared = ExplodingText() if failure_site == "stringify" else "changed"
                env.service._ports = replace(
                    env.service._ports,
                    prepare_queued_prompt_text=lambda **_kwargs: prepared,
                )
                replacement = (
                    patch(
                        "bot.feishu_execution_queue_service.replace_text_input_items",
                        side_effect=RuntimeError("input replacement failed"),
                    )
                    if failure_site == "input_replacement"
                    else patch(
                        "bot.feishu_execution_queue_service.replace_text_input_items",
                        wraps=replace_text_input_items,
                    )
                )

                with (
                    replacement,
                    patch.object(
                        env.queue,
                        "complete_drain",
                        wraps=env.queue.complete_drain,
                    ) as complete_drain,
                ):
                    env.service.drain(env.binding)

                self.assertEqual(
                    complete_drain.call_args.kwargs["outcome"],
                    "known_no_effect_settled",
                )
                self.assertEqual(env.prompt_starts, [])
                self.assertEqual(env.terminal_reconciliations, ["root-a"])
                self.assertFalse(
                    env.queue.snapshot(env.binding).has_pending_or_draining
                )


if __name__ == "__main__":
    unittest.main()
