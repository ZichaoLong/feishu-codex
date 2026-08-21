"""Focused contract tests for Feishu submission and main-turn ownership."""

from __future__ import annotations

import inspect
import pathlib
import tempfile
import unittest

from bot.binding_runtime_contract import (
    BindingOwnerLossCommand,
    BindingOwnerRevisionReceipt,
)
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.feishu_root_operation_contract import (
    FeishuRootBackendEpochRetirementReceipt,
    FeishuRootOperationPoisoned,
    FeishuRootOperationPorts,
    FeishuRootOperationRetentionError,
    FeishuRootOperationToken,
    FeishuRootOperationTokenError,
)
from bot.feishu_root_operation_controller import FeishuRootOperationController
from bot.reason_codes import ReasonedCheck
from bot.runtime_loop import RuntimeLoopContextError
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)


class _RuntimeGuard:
    def __init__(self) -> None:
        self.allowed = True

    def __call__(self) -> None:
        if not self.allowed:
            raise RuntimeLoopContextError("outside RuntimeLoop")


class _PortsHarness:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.leases = InteractionLeaseStore(data_dir)
        self.events: list[tuple] = []
        self.access = ReasonedCheck.allow()
        self.direct_target_error: Exception | None = None
        self.root_status: str | Exception = "idle"
        self.lease_release_mode = "real"

    @staticmethod
    def holder(binding: tuple[str, str]):
        return make_feishu_interaction_holder(
            binding[0],
            binding[1],
            owner_pid=0,
        )

    def ports(self) -> FeishuRootOperationPorts:
        return FeishuRootOperationPorts(
            verify_direct_thread_target=self.ensure_direct_target,
            prompt_write_admission=self.prompt_admission,
            holder_for_binding=self.holder_for_binding,
            validate_binding_owner_receipt=self.validate_binding_owner_receipt,
            acquire_interaction_lease=self.acquire_lease,
            release_exact_interaction_lease=self.release_exact_lease,
            activate_interaction_turn=self.leases.activate_turn,
            lookup_interaction_lease=self.lookup_lease,
            read_root_status=self.read_status,
        )

    def ensure_direct_target(self, root_thread_id: str) -> None:
        self.events.append(("direct_target", root_thread_id))
        if self.direct_target_error is not None:
            raise self.direct_target_error

    def controller(
        self,
        guard: _RuntimeGuard | None = None,
    ) -> FeishuRootOperationController:
        return FeishuRootOperationController(
            ports=self.ports(),
            runtime_context_guard=guard or _RuntimeGuard(),
        )

    def prompt_admission(
        self,
        binding: tuple[str, str],
        chat_id: str,
        root_thread_id: str,
        message_id: str,
    ) -> ReasonedCheck:
        self.events.append(
            ("access", binding, chat_id, root_thread_id, message_id)
        )
        return self.access

    def holder_for_binding(self, binding: tuple[str, str]):
        self.events.append(("holder", binding))
        return self.holder(binding)

    @staticmethod
    def validate_binding_owner_receipt(receipt) -> None:
        if not isinstance(receipt, BindingOwnerRevisionReceipt):
            raise RuntimeError("invalid binding owner receipt")

    def acquire_lease(self, binding: tuple[str, str], root_thread_id: str):
        self.events.append(("lease_acquire", binding, root_thread_id))
        return self.leases.acquire(root_thread_id, self.holder(binding))

    def release_exact_lease(self, lease) -> bool:
        self.events.append(("lease_release", lease.thread_id, lease.turn_id))
        if self.lease_release_mode == "raise":
            raise OSError("lease release failed")
        released = self.leases.release_if_matches(lease)
        if self.lease_release_mode == "raise_after_release":
            raise OSError("lease release acknowledgement failed")
        if self.lease_release_mode == "false_after_release":
            return False
        if self.lease_release_mode == "false":
            return False
        return released

    def lookup_lease(self, root_thread_id: str):
        self.events.append(("lease_lookup", root_thread_id))
        return self.leases.load(root_thread_id)

    def read_status(self, root_thread_id: str) -> str:
        self.events.append(("root_status", root_thread_id))
        if isinstance(self.root_status, Exception):
            raise self.root_status
        return self.root_status


class FeishuRootOperationControllerTests(unittest.TestCase):
    binding = ("ou_user", "chat-1")
    root_thread_id = "root-1"

    def _environment(
        self,
    ) -> tuple[_PortsHarness, FeishuRootOperationController]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        harness = _PortsHarness(pathlib.Path(temporary.name))
        return harness, harness.controller()

    def _owner_loss(
        self,
        *,
        reason: str,
        disposition: str,
        binding: tuple[str, str] | None = None,
        thread_id: str | None = None,
        incarnation: int = 1,
        owner_revision: int = 0,
    ) -> BindingOwnerLossCommand:
        return BindingOwnerLossCommand(
            owner=BindingOwnerRevisionReceipt(
                _issuer_nonce=1,
                binding=binding or self.binding,
                incarnation=incarnation,
                owner_revision=owner_revision,
                expected_thread_id=thread_id or self.root_thread_id,
            ),
            reason=reason,
            disposition=disposition,  # type: ignore[arg-type]
        )

    def _admit(
        self,
        controller: FeishuRootOperationController,
        *,
        binding: tuple[str, str] | None = None,
        root_thread_id: str | None = None,
        operation_kind: str = "mutation",
    ) -> FeishuRootOperationToken:
        effective_binding = binding or self.binding
        return controller.admit(
            effective_binding,
            root_thread_id or self.root_thread_id,
            chat_id=effective_binding[1],
            message_id="message-1",
            reason="test_admission",
            operation_kind=operation_kind,
        )

    def test_every_public_api_checks_runtime_context_before_side_effects(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        harness = _PortsHarness(pathlib.Path(temporary.name))
        guard = _RuntimeGuard()
        controller = harness.controller(guard)
        guard.allowed = False

        for name, method in inspect.getmembers(controller, inspect.ismethod):
            if name.startswith("_"):
                continue
            required = {
                parameter.name: None
                for parameter in inspect.signature(method).parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind
                not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
            }
            with self.subTest(api=name):
                with self.assertRaises(RuntimeLoopContextError):
                    method(**required)

        self.assertEqual(harness.events, [])
        self.assertEqual(harness.leases.list(), [])

    def test_admission_creates_only_a_blank_submission_lease(self) -> None:
        harness, controller = self._environment()

        token = self._admit(controller)

        self.assertIsInstance(token, FeishuRootOperationToken)
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)

    def test_admission_authority_reads_direct_target_before_access_or_lease(self) -> None:
        harness, controller = self._environment()
        harness.direct_target_error = ValueError(
            "线程 `child-1` 是 parent-owned ThreadSpawn subagent"
        )

        with self.assertRaisesRegex(ValueError, "ThreadSpawn"):
            self._admit(controller, root_thread_id="child-1")

        self.assertEqual(harness.events, [("direct_target", "child-1")])
        self.assertEqual(harness.leases.list(), [])

    def test_only_one_submission_may_be_in_flight_per_thread(self) -> None:
        _harness, controller = self._environment()
        self._admit(controller)

        with self.assertRaises(FeishuRootOperationRetentionError):
            self._admit(controller)

    def test_another_surface_cannot_take_the_submission_lease(self) -> None:
        harness, controller = self._environment()
        harness.leases.acquire(
            "root-1",
            make_web_interaction_holder("web-1", owner_pid=0),
        )

        with self.assertRaises(PermissionError):
            self._admit(controller)

    def test_an_active_turn_cannot_be_reused_as_a_new_submission(self) -> None:
        harness, controller = self._environment()
        blank = harness.leases.acquire(
            "root-1",
            harness.holder(self.binding),
        ).lease
        assert blank is not None
        harness.leases.activate_turn(blank, "turn-1")

        with self.assertRaises(PermissionError):
            self._admit(controller)

        self.assertEqual(harness.leases.load("root-1").turn_id, "turn-1")

    def test_known_failure_releases_the_exact_blank_submission(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)

        controller.settle_known_failure(token, reason="known rejection")

        self.assertIsNone(harness.leases.load("root-1"))
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)
        with self.assertRaises(FeishuRootOperationTokenError):
            controller.settle_known_failure(token, reason="stale retry")

    def test_known_start_activates_exact_turn_and_matching_completion_releases(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)

        controller.acknowledge_continuing(token, turn_id="turn-1")

        active = harness.leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.turn_id, "turn-1")
        self.assertFalse(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "older-turn"}},
            )
        )
        self.assertIsNotNone(harness.leases.load("root-1"))
        self.assertTrue(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "turn-1"}},
            )
        )
        self.assertIsNone(harness.leases.load("root-1"))

    def test_child_completion_cannot_release_the_root_turn(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.acknowledge_continuing(token, turn_id="turn-1")

        self.assertFalse(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "child-1", "turn": {"id": "turn-1"}},
            )
        )
        self.assertIsNotNone(harness.leases.load("root-1"))

    def test_continuation_receipt_is_process_local_and_not_durable(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)

        receipt = controller.arm_continuation(token)

        self.assertEqual(controller.snapshot("root-1").continuation_generations, (1,))
        controller.settle_continuation_failure(receipt, reason="known failure")
        self.assertEqual(controller.snapshot("root-1").continuation_generations, ())

    def test_unknown_submission_is_local_and_authoritative_idle_releases_it(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)

        controller.mark_outcome_unknown(token, reason="transport timeout")

        snapshot = controller.snapshot("root-1")
        self.assertTrue(snapshot.submission_outcome_unknown)
        with self.assertRaises(FeishuRootOperationPoisoned):
            self._admit(controller)
        harness.root_status = "active"
        self.assertFalse(controller.reconcile_terminal("root-1"))
        harness.root_status = "idle"
        self.assertTrue(controller.reconcile_terminal("root-1"))
        self.assertIsNone(harness.leases.load("root-1"))
        self.assertFalse(
            controller.snapshot("root-1").submission_outcome_unknown
        )

    def test_unknown_submission_adopts_lifecycle_turn_identity(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.mark_outcome_unknown(token, reason="response lost")

        self.assertTrue(
            controller.reconcile_notification(
                "turn/started",
                {"threadId": "root-1", "turn": {"id": "turn-1"}},
            )
        )
        self.assertEqual(harness.leases.load("root-1").turn_id, "turn-1")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)
        self.assertTrue(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turnId": "turn-1"},
            )
        )
        self.assertIsNone(harness.leases.load("root-1"))

    def test_completion_cannot_reconcile_a_missed_started_notification(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.mark_outcome_unknown(token, reason="response lost")

        self.assertFalse(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "turn-1"}},
            )
        )
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "")
        snapshot = controller.snapshot("root-1")
        self.assertTrue(snapshot.submission_outcome_unknown)
        self.assertEqual(snapshot.pending_admission_count, 1)

    def test_completion_cannot_bind_accepted_prompt_with_missed_started(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.await_start_identity(token)

        self.assertFalse(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "turn-1"}},
            )
        )
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)

    def test_compact_completion_cannot_replace_async_start_identity(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="compact")
        controller.await_start_identity(token)

        self.assertFalse(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "turn-1"}},
            )
        )
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)

    def test_compaction_item_started_cannot_bind_an_ordinary_prompt(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.await_start_identity(token)

        self.assertFalse(
            controller.reconcile_notification(
                "item/started",
                {
                    "threadId": "root-1",
                    "turnId": "turn-1",
                    "item": {"type": "contextCompaction"},
                },
            )
        )
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)

    def test_compaction_item_started_binds_an_awaiting_compact(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="compact")
        controller.await_start_identity(token)

        self.assertTrue(
            controller.reconcile_notification(
                "item/started",
                {
                    "threadId": "root-1",
                    "turnId": "turn-1",
                    "item": {"type": "contextCompaction"},
                },
            )
        )
        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.turn_id, "turn-1")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_accepted_continuation_without_turn_id_becomes_local_unknown(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.arm_continuation(token)

        controller.acknowledge_continuing(token)

        snapshot = controller.snapshot("root-1")
        self.assertTrue(snapshot.submission_outcome_unknown)
        self.assertEqual(snapshot.pending_admission_count, 1)
        self.assertIsNotNone(harness.leases.load("root-1"))

    def test_compact_waits_for_and_activates_exact_async_identity(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="compact")
        controller.await_start_identity(token)

        self.assertFalse(
            controller.acknowledge_async_start(
                ("ou_other", "chat-2"),
                "root-1",
                "turn-1",
            )
        )
        self.assertTrue(
            controller.acknowledge_async_start(
                self.binding,
                "root-1",
                "turn-1",
            )
        )
        self.assertEqual(harness.leases.load("root-1").turn_id, "turn-1")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_prompt_waits_for_authoritative_turn_started_identity(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)

        controller.await_start_identity(token)

        self.assertTrue(
            controller.reconcile_notification(
                "turn/started",
                {"threadId": "root-1", "turn": {"id": "turn-actual"}},
            )
        )
        self.assertEqual(harness.leases.load("root-1").turn_id, "turn-actual")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_prompt_start_id_is_a_one_shot_interrupt_candidate_not_a_lease_id(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")

        controller.accept_prompt_start(token, "submission-1")

        lease = harness.leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "")
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(
                ("ou_other", "chat-2"),
                "root-1",
            )
        )
        claim = controller.claim_prompt_interrupt_candidate(
            self.binding,
            "root-1",
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim and claim.turn_id, "submission-1")
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        )

        assert claim is not None
        self.assertTrue(
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=CodexRpcPreSendError(
                    "turn/interrupt",
                    RuntimeError("direct-root read failed"),
                ),
            )
        )
        restored = controller.claim_prompt_interrupt_candidate(
            self.binding,
            "root-1",
        )
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertTrue(controller.consume_prompt_interrupt_candidate(restored))
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        )

    def test_prompt_candidate_is_cleared_by_actual_lifecycle_and_owner_loss(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        controller.accept_prompt_start(token, "submission-1")

        self.assertTrue(
            controller.reconcile_notification(
                "turn/started",
                {"threadId": "root-1", "turn": {"id": "turn-actual"}},
            )
        )
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        )
        self.assertEqual(harness.leases.load("root-1").turn_id, "turn-actual")

        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        controller.accept_prompt_start(token, "submission-2")
        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        )
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)

    def test_prompt_candidate_restore_fails_after_actual_lifecycle_finishes_admission(
        self,
    ) -> None:
        _harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        controller.accept_prompt_start(token, "submission-1")
        claim = controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        assert claim is not None

        self.assertTrue(
            controller.reconcile_notification(
                "turn/started",
                {"threadId": "root-1", "turn": {"id": "turn-actual"}},
            )
        )
        self.assertFalse(
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=CodexRpcPreSendError(
                    "turn/interrupt",
                    RuntimeError("direct-root read failed"),
                ),
            )
        )

    def test_claimed_prompt_candidate_is_cleared_by_exact_root_terminal_settlement(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        controller.accept_prompt_start(token, "submission-1")
        claim = controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        assert claim is not None
        harness.root_status = "idle"

        self.assertTrue(controller.reconcile_terminal("root-1"))
        self.assertIsNone(harness.leases.load("root-1"))
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)
        self.assertFalse(
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=CodexRpcPreSendError(
                    "turn/interrupt",
                    RuntimeError("late direct-root failure"),
                ),
            )
        )

    def test_prompt_candidate_cannot_be_reinstalled_or_restored_without_typed_pre_send(
        self,
    ) -> None:
        _harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        controller.accept_prompt_start(token, "submission-1")
        claim = controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        assert claim is not None

        with self.assertRaisesRegex(
            FeishuRootOperationTokenError,
            "已安装过 interrupt candidate",
        ):
            controller.accept_prompt_start(token, "submission-2")
        with self.assertRaisesRegex(
            FeishuRootOperationTokenError,
            "typed CodexRpcPreSendError",
        ):
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=RuntimeError("not typed"),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(
            FeishuRootOperationTokenError,
            "turn/interrupt pre-send failure",
        ):
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=CodexRpcPreSendError(
                    "thread/resume",
                    RuntimeError("wrong method"),
                ),
            )

        self.assertTrue(controller.consume_prompt_interrupt_candidate(claim))
        self.assertIsNone(
            controller.claim_prompt_interrupt_candidate(self.binding, "root-1")
        )

    def test_authoritative_idle_settles_accepted_prompt_with_missed_identity(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.await_start_identity(token)

        harness.root_status = "active"
        self.assertFalse(controller.reconcile_terminal("root-1"))
        harness.root_status = "idle"
        self.assertTrue(controller.reconcile_terminal("root-1"))

        self.assertIsNone(harness.leases.load("root-1"))
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_authoritative_idle_does_not_settle_compact_before_unknown(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="compact")
        controller.await_start_identity(token)
        harness.root_status = "idle"

        self.assertFalse(controller.reconcile_terminal("root-1"))
        self.assertIsNotNone(harness.leases.load("root-1"))

        controller.mark_awaiting_start_outcome_unknown(
            self.binding,
            "root-1",
            reason="compact identity timeout",
        )
        self.assertTrue(controller.reconcile_terminal("root-1"))
        self.assertIsNone(harness.leases.load("root-1"))

    def test_owner_loss_releases_only_a_blank_submission(self) -> None:
        harness, controller = self._environment()
        self._admit(controller)

        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )

        self.assertIsNone(harness.leases.load("root-1"))
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_owner_loss_releases_an_orphan_blank_without_local_admission(
        self,
    ) -> None:
        harness, controller = self._environment()
        acquired = harness.leases.acquire(
            "root-1",
            harness.holder(self.binding),
        )
        self.assertTrue(acquired.granted)

        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )

        self.assertIsNone(harness.leases.load("root-1"))
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

    def test_owner_loss_preserves_accepted_blank_until_exact_lifecycle(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        accepted_blank = harness.leases.load("root-1")
        assert accepted_blank is not None
        controller.await_start_identity(token)

        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )

        self.assertEqual(harness.leases.load("root-1"), accepted_blank)
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 1)
        successor = harness.leases.acquire(
            "root-1",
            make_web_interaction_holder("web-successor", owner_pid=0),
        )
        self.assertFalse(successor.granted)
        self.assertEqual(successor.lease, accepted_blank)

        self.assertTrue(
            controller.reconcile_notification(
                "turn/started",
                {"threadId": "root-1", "turn": {"id": "turn-actual"}},
            )
        )
        active = harness.leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active and active.lease_id, accepted_blank.lease_id)
        self.assertEqual(active and active.turn_id, "turn-actual")
        self.assertEqual(controller.snapshot("root-1").pending_admission_count, 0)

        self.assertTrue(
            controller.reconcile_notification(
                "turn/completed",
                {"threadId": "root-1", "turn": {"id": "turn-actual"}},
            )
        )
        self.assertIsNone(harness.leases.load("root-1"))

    def test_owner_loss_preserves_unknown_blank_for_lifecycle_reconciliation(
        self,
    ) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        unknown_blank = harness.leases.load("root-1")
        assert unknown_blank is not None
        controller.mark_outcome_unknown(token, reason="transport timeout")

        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )

        self.assertEqual(harness.leases.load("root-1"), unknown_blank)
        snapshot = controller.snapshot("root-1")
        self.assertEqual(snapshot.pending_admission_count, 1)
        self.assertTrue(snapshot.submission_outcome_unknown)
        successor = harness.leases.acquire(
            "root-1",
            make_web_interaction_holder("web-successor", owner_pid=0),
        )
        self.assertFalse(successor.granted)
        self.assertEqual(successor.lease, unknown_blank)

    def test_owner_loss_does_not_release_an_active_main_turn(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.acknowledge_continuing(token, turn_id="turn-1")

        controller.settle_owner_loss(
            self._owner_loss(reason="binding_detached", disposition="abandon")
        )

        active = harness.leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.turn_id, "turn-1")

    def test_backend_epoch_retirement_clears_only_owner_local_facts(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller, operation_kind="prompt")
        continuation = controller.arm_continuation(token)
        controller.accept_prompt_start(token, "submission-1")
        claim = controller.claim_prompt_interrupt_candidate(
            self.binding,
            self.root_thread_id,
        )
        assert claim is not None
        exact_lease = harness.leases.load(self.root_thread_id)
        assert exact_lease is not None
        second_token = self._admit(
            controller,
            root_thread_id="root-2",
            operation_kind="prompt",
        )
        controller.accept_prompt_start(second_token, "submission-2")
        second_lease = harness.leases.load("root-2")
        assert second_lease is not None
        events_before_retirement = list(harness.events)

        retirement = controller.retire_backend_epoch_after_stop()

        self.assertEqual(
            retirement,
            FeishuRootBackendEpochRetirementReceipt(
                root_thread_ids=(self.root_thread_id, "root-2"),
                admission_count=2,
                continuation_count=1,
                interrupt_candidate_count=2,
            ),
        )
        self.assertEqual(harness.events, events_before_retirement)
        self.assertEqual(harness.leases.load(self.root_thread_id), exact_lease)
        self.assertEqual(harness.leases.load("root-2"), second_lease)
        self.assertEqual(
            controller.snapshot(self.root_thread_id).pending_admission_count,
            0,
        )
        self.assertEqual(
            controller.snapshot(self.root_thread_id).continuation_generations,
            (),
        )
        with self.assertRaises(FeishuRootOperationTokenError):
            controller.await_start_identity(token)
        with self.assertRaises(FeishuRootOperationTokenError):
            controller.settle_continuation_failure(
                continuation,
                reason="stale backend epoch",
            )
        self.assertFalse(controller.consume_prompt_interrupt_candidate(claim))
        self.assertFalse(
            controller.restore_prompt_interrupt_candidate_after_pre_send(
                claim,
                error=CodexRpcPreSendError(
                    "turn/interrupt",
                    RuntimeError("stale pre-send result"),
                ),
            )
        )
        self.assertEqual(
            controller.retire_backend_epoch_after_stop(),
            FeishuRootBackendEpochRetirementReceipt(
                root_thread_ids=(),
                admission_count=0,
                continuation_count=0,
                interrupt_candidate_count=0,
            ),
        )

    def test_backend_epoch_retirement_preserves_monotonic_identity_facts(self) -> None:
        harness, controller = self._environment()
        first_token = self._admit(controller, operation_kind="prompt")
        first_continuation = controller.arm_continuation(first_token)
        controller.accept_prompt_start(first_token, "submission-1")
        first_claim = controller.claim_prompt_interrupt_candidate(
            self.binding,
            self.root_thread_id,
        )
        assert first_claim is not None
        owner_loss = self._owner_loss(
            reason="observer_detached",
            disposition="abandon",
            binding=("ou_observer", "chat-observer"),
        )
        first_owner_loss = controller.settle_owner_loss(owner_loss)
        old_lease = harness.leases.load(self.root_thread_id)
        assert old_lease is not None

        controller.retire_backend_epoch_after_stop()
        self.assertTrue(harness.leases.release_if_matches(old_lease))
        second_token = self._admit(controller, operation_kind="prompt")
        second_continuation = controller.arm_continuation(second_token)
        controller.accept_prompt_start(second_token, "submission-2")
        second_claim = controller.claim_prompt_interrupt_candidate(
            self.binding,
            self.root_thread_id,
        )
        assert second_claim is not None
        second_owner_loss = controller.settle_owner_loss(owner_loss)
        next_owner_loss = controller.settle_owner_loss(
            self._owner_loss(
                reason="observer_detached",
                disposition="abandon",
                binding=("ou_other_observer", "chat-other-observer"),
                incarnation=2,
            )
        )

        self.assertEqual(
            controller.snapshot(self.root_thread_id).continuation_generations,
            (2,),
        )
        self.assertEqual(second_token._issuer_nonce, first_token._issuer_nonce)
        self.assertGreater(second_token._token_nonce, first_token._token_nonce)
        self.assertGreater(
            second_continuation._token_nonce,
            first_continuation._token_nonce,
        )
        self.assertGreater(second_claim._claim_nonce, first_claim._claim_nonce)
        self.assertEqual(
            second_owner_loss._transaction_nonce,
            first_owner_loss._transaction_nonce,
        )
        self.assertGreater(
            next_owner_loss._transaction_nonce,
            first_owner_loss._transaction_nonce,
        )
        replacement_lease = harness.leases.load(self.root_thread_id)
        assert replacement_lease is not None
        self.assertNotEqual(replacement_lease.lease_id, old_lease.lease_id)
        self.assertFalse(controller.consume_prompt_interrupt_candidate(first_claim))
        self.assertTrue(controller.consume_prompt_interrupt_candidate(second_claim))

    def test_lost_release_ack_is_resolved_from_the_exact_store_fact(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        harness.lease_release_mode = "raise_after_release"

        controller.settle_known_failure(token, reason="known rejection")

        self.assertIsNone(harness.leases.load("root-1"))

if __name__ == "__main__":
    unittest.main()
