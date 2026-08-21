from __future__ import annotations

import pathlib
import tempfile
import unittest
import uuid
from unittest.mock import Mock

from bot.codex_protocol.client import CodexRpcTransportError
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)
from bot.web_runtime.contract import (
    WebInteractionDeliveryDisposition,
    WebRuntimeError,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.operation_service import WebOperationPorts, WebOperationService
from bot.web_runtime.projection import FocusWebProjection


_DEFAULT_GUARD = object()


class WebOperationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = pathlib.Path(temporary.name)
        self.in_runtime = True
        self.guard_calls = 0
        self.lifecycle_state = "present"
        self.turns_by_thread: dict[str, tuple[dict, ...]] = {}

        def require_runtime() -> None:
            self.guard_calls += 1
            if not self.in_runtime:
                raise RuntimeError("outside RuntimeLoop")

        self.require_runtime = require_runtime
        self.leases = InteractionLeaseStore(self.data_dir)
        self.documents = WebDocumentRegistry(
            runtime_context_guard=self.require_runtime,
        )
        for client_id in ("tab-1", "tab-2"):
            self.documents.mark_connected(client_id)
        self.documents.materialize_thread("tab-1", "thread-1")
        self.documents.materialize_thread("tab-2", "thread-1")
        self.projection = FocusWebProjection()
        self.events: list[dict] = []
        self.projection.subscribe(self.events.append)
        self.service = self._make_service()

    @staticmethod
    def _require_thread_id(thread_id: str) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("thread id required")
        return normalized

    def _make_service(
        self,
        *,
        leases=None,
        documents=None,
        guard=_DEFAULT_GUARD,
    ) -> WebOperationService:
        document_registry = documents or self.documents
        return WebOperationService(
            interaction_lease_store=leases or self.leases,
            document_registry=document_registry,
            projection=self.projection,
            runtime_context_guard=(
                self.require_runtime if guard is _DEFAULT_GUARD else guard
            ),  # type: ignore[arg-type]
            ports=WebOperationPorts(
                require_connected_document=document_registry.require_connected,
                require_thread_id=self._require_thread_id,
                read_lifecycle_target_state=lambda _thread_id: self.lifecycle_state,
                turn_ids=lambda thread_id: self.turns_by_thread.get(thread_id, ()),
            ),
        )

    def test_requires_guard_and_checks_runtime_before_store_access(self) -> None:
        with self.assertRaises(TypeError):
            self._make_service(guard=None)
        leases = Mock()
        service = self._make_service(leases=leases)
        self.in_runtime = False

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            service.owned_main_turn_thread_ids("tab-1")

        self.assertEqual(leases.mock_calls, [])

    def test_exclusive_submission_activation_and_exact_blank_release(self) -> None:
        submission = self.service.acquire_exclusive_turn_submission(
            "tab-1", "thread-1"
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.acquire_exclusive_turn_submission("tab-2", "thread-1")
        self.assertEqual(caught.exception.code, "interaction_owned")

        active = self.service.activate_turn_submission(submission, "turn-1")
        self.assertEqual(active.lease.turn_id, "turn-1")
        self.assertFalse(
            self.service.release_exact_blank_turn_submission(
                submission,
                reason="must_not_release_successor",
            )
        )
        self.assertEqual(self.leases.load("thread-1"), active.lease)

        blank = self.service.acquire_exclusive_turn_submission("tab-1", "thread-2")
        self.assertTrue(
            self.service.release_exact_blank_turn_submission(
                blank,
                reason="known_no_start",
            )
        )
        self.assertIsNone(self.leases.load("thread-2"))

    def test_active_writer_and_delivery_require_exact_live_web_document(self) -> None:
        submission = self.service.acquire_exclusive_turn_submission(
            "tab-1", "thread-1"
        )
        self.service.activate_turn_submission(submission, "turn-1")

        writer = self.service.require_active_turn_writer(
            "tab-1",
            "thread-1",
            turn_id="turn-1",
        )
        self.assertEqual(writer.lease.turn_id, "turn-1")
        with self.assertRaises(WebRuntimeError):
            self.service.require_active_turn_writer(
                "tab-1",
                "thread-1",
                turn_id="successor",
            )
        self.assertEqual(
            self.service.interaction_delivery_decision("thread-1").disposition,
            WebInteractionDeliveryDisposition.CONNECTED,
        )

        self.documents.mark_transport_disconnected("tab-1")
        self.assertEqual(
            self.service.interaction_delivery_decision("thread-1").disposition,
            WebInteractionDeliveryDisposition.DISCONNECTED,
        )

    def test_autonomous_turn_cannot_take_another_surface_owner(self) -> None:
        acquired = self.service.admit_autonomous_turn(
            "tab-1",
            "thread-1",
            allow_fresh=True,
        )
        borrowed = self.service.admit_autonomous_turn(
            "tab-1",
            "thread-1",
            allow_fresh=True,
        )
        self.assertTrue(acquired.acquired)
        self.assertFalse(borrowed.acquired)
        self.assertEqual(acquired.lease, borrowed.lease)

        self.leases.release_if_matches(acquired.lease)
        feishu = self.leases.acquire(
            "thread-1",
            make_feishu_interaction_holder("user-1", "chat-1", owner_pid=0),
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.service.admit_autonomous_turn(
                "tab-1",
                "thread-1",
                allow_fresh=True,
            )
        self.assertEqual(caught.exception.code, "interaction_owned")
        self.assertEqual(self.leases.load("thread-1"), feishu.lease)

    def test_unknown_control_is_process_local_and_never_replayed(self) -> None:
        calls = 0

        def uncertain_call() -> None:
            nonlocal calls
            calls += 1
            raise CodexRpcTransportError(
                "thread/archive",
                {"message": "connection lost"},
            )

        with self.assertRaises(WebRuntimeError) as caught:
            self.service.run_writer_scoped_control_mutation(
                "tab-1",
                "thread-1",
                operation="archive",
                call=uncertain_call,
            )

        self.assertEqual(caught.exception.code, "mutation_unknown")
        self.assertEqual(calls, 1)
        self.assertTrue(self.service.has_unknown_mutation("thread-1"))
        self.assertIsNone(self.leases.load("thread-1"))

    def test_fresh_prompt_admission_is_exact_but_not_blocked_by_control_unknown(self) -> None:
        pending = self.service.record_unknown_mutation(
            "thread-1",
            operation="archive",
            client_id="tab-1",
        )
        mutation_id = str(uuid.uuid4())

        self.service.admit_explicit_web_effect(
            "tab-1",
            "thread-1",
            operation="start_prompt",
            mutation_id=mutation_id,
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.service.admit_explicit_web_effect(
                "tab-1",
                "thread-1",
                operation="rename",
            )
        self.assertEqual(caught.exception.code, "mutation_reconciling")
        self.assertEqual(caught.exception.details["mutation_id"], pending.mutation_id)

        with self.assertRaises(WebRuntimeError) as invalid:
            self.service.admit_explicit_web_effect(
                "tab-1",
                "thread-1",
                operation="start_prompt",
                mutation_id="not-a-uuid",
            )
        self.assertEqual(invalid.exception.code, "invalid_mutation_id")

    def test_lifecycle_unknown_requires_point_verification_before_discard(self) -> None:
        pending = self.service.record_unknown_mutation(
            "thread-1",
            operation="archive",
            client_id="tab-1",
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.service.resolve_unknown_mutation(
                "tab-1",
                "thread-1",
                action="discard",
                mutation_id=pending.mutation_id,
            )
        self.assertEqual(caught.exception.code, "lifecycle_verification_required")

        self.lifecycle_state = "archived"
        verified = self.service.verify_unknown_lifecycle_mutation(
            "tab-1",
            "thread-1",
            mutation_id=pending.mutation_id,
        )
        settled = self.service.resolve_unknown_mutation(
            "tab-1",
            "thread-1",
            action="discard",
            mutation_id=pending.mutation_id,
        )
        self.assertEqual(verified["verification"]["state"], "archived")
        self.assertEqual(settled["disposition"], "user_discard")

    def test_generic_retry_and_turn_reconciliation_settle_exact_attempts(self) -> None:
        retry = self.service.record_unknown_mutation(
            "thread-1",
            operation="rename",
            client_id="tab-1",
        )
        retry_result = self.service.resolve_unknown_mutation(
            "tab-1",
            "thread-1",
            action="retry",
            mutation_id=retry.mutation_id,
        )
        self.assertEqual(retry_result["disposition"], "retry_opened")

        self.turns_by_thread["thread-2"] = ({"id": "old-turn"},)
        compact = self.service.record_unknown_mutation(
            "thread-2",
            operation="compact",
            client_id="tab-1",
        )
        self.assertTrue(
            self.service.reconcile_unknown_from_turns(
                "thread-2",
                [{"id": "new-turn", "status": "completed", "items": []}],
            )
        )
        self.assertFalse(self.service.has_unknown_mutation("thread-2"))
        self.assertEqual(
            self.service._mutations.settlement_exact(  # noqa: SLF001
                "thread-2",
                compact.mutation_id,
            ).disposition,
            "effect_observed",
        )

    def test_backend_retirement_preserves_only_control_explanation(self) -> None:
        pending = self.service.record_unknown_mutation(
            "thread-1",
            operation="delete",
            client_id="tab-1",
        )

        receipt = self.service.retire_backend_epoch_after_stop()

        self.assertEqual(receipt.retired_count, 1)
        self.assertEqual(receipt.retired_mutation_ids, (pending.mutation_id,))
        self.assertFalse(self.service.has_unknown_mutation("thread-1"))
        with self.assertRaises(WebRuntimeError) as caught:
            self.service.resolve_unknown_mutation(
                "tab-1",
                "thread-1",
                action="discard",
                mutation_id=pending.mutation_id,
            )
        self.assertEqual(caught.exception.code, "mutation_backend_replaced")


if __name__ == "__main__":
    unittest.main()
