from __future__ import annotations

import pathlib
import threading
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from typing import Any

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.runtime_loop import RuntimeLoop
from bot.stores.web_attachment_store import WebAttachmentStore
from bot.stores.web_next_turn_settings_store import WebNextTurnSettings
from bot.stores.web_writer_profile_store import WebWriterProfile, WebWriterProfileStore
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.prompt_submission import (
    WebPromptSubmissionCoordinator,
    WebPromptSubmissionPorts,
)
from bot.web_runtime.selection_coordinator import WebSelectionCoordinator
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.writer_workspace_coordinator import (
    WebComposerScopeReceipt,
    WebWriterWorkspaceCoordinator,
    WebWriterWorkspacePorts,
)


class _Workspace:
    def __init__(self) -> None:
        self.scope_checks = 0
        self.scope_claims = 0
        self.scope_is_current = True
        self.claims: list[tuple[str, ...]] = []
        self.releases = 0
        self.rollbacks = 0
        self.rollback_succeeds = True
        self.remembered_cwds: list[tuple[str, str]] = []

    @staticmethod
    def normalize_attachment_ids(values: list[str] | None) -> list[str]:
        return list(values or [])

    def freeze_composer_scope_receipt(
        self,
        client_id: str,
        *,
        thread_id: str,
        scope_generation: int,
        attachment_scope: str,
        composer_scope_id: str,
    ) -> WebComposerScopeReceipt:
        self.scope_checks += 1
        return WebComposerScopeReceipt(
            client_id=client_id,
            thread_id=thread_id,
            scope_generation=scope_generation,
            attachment_scope=attachment_scope,
            composer_scope_id=composer_scope_id,
        )

    def claim_composer_scope_receipt_external(
        self,
        receipt: WebComposerScopeReceipt,
    ) -> WebComposerScopeReceipt:
        self.scope_claims += 1
        if not self.scope_is_current:
            raise WebRuntimeError(
                "stale scope",
                code="stale_attachment_scope",
                status=409,
            )
        return receipt

    def claim_prompt_attachments_external(
        self,
        _client_id: str,
        *,
        scope_key: str,
        attachment_ids: list[str],
    ) -> tuple[tuple[str, ...], object | None]:
        del scope_key
        normalized = tuple(attachment_ids)
        self.claims.append(normalized)
        return normalized, object() if normalized else None

    @staticmethod
    def prompt_input_items_external(
        text: str,
        attachments: tuple[str, ...],
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        return [{"type": "text", "text": text, "attachments": list(attachments)}]

    def release_prompt_attachment_claim_external(self, _receipt: object) -> None:
        self.releases += 1

    def rollback_prompt_attachment_claim_external(
        self,
        _receipt: object,
    ) -> tuple[str, ...]:
        self.rollbacks += 1
        return self.claims[-1] if self.rollback_succeeds else ()

    def remember_prepared_thread_cwd(self, thread_id: str, cwd: str) -> None:
        self.remembered_cwds.append((thread_id, cwd))


class _Operations:
    def __init__(self) -> None:
        self.admissions: list[tuple[str, str, str, str]] = []

    def admit_explicit_web_effect(
        self,
        client_id: str,
        thread_id: str,
        *,
        operation: str,
        mutation_id: str,
    ) -> None:
        self.admissions.append((client_id, thread_id, operation, mutation_id))

    @staticmethod
    def upstream_outcome_unknown(result: Any) -> bool:
        return bool(
            isinstance(result, dict)
            and result.get("upstream_outcome") == "unknown"
        )


class _GoalPolicy:
    @staticmethod
    def requires_writer_admission(_goal: Any) -> bool:
        return False


class _Projection:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, event_type: str, **kwargs: Any) -> dict[str, Any]:
        event = {"type": event_type, **kwargs}
        self.events.append(event)
        return event


class _Backend:
    def __init__(self) -> None:
        self.status = "idle"
        self.generation = 7
        self.start_response: dict[str, Any] = {"turn": {"id": "turn-started"}}
        self.steer_response: dict[str, Any] = {"turnId": "turn-A"}
        self.start_error: Exception | None = None
        self.steer_error: Exception | None = None
        self.start_calls: list[dict[str, Any]] = []
        self.steer_calls: list[dict[str, Any]] = []
        self.read_calls: list[tuple[str, bool]] = []
        self.start_entered: threading.Event | None = None
        self.start_release: threading.Event | None = None

    def read_thread(self, thread_id: str, _include_turns: bool, **_kwargs: Any):
        self.read_calls.append((thread_id, _include_turns))
        return ThreadSnapshot(
            ThreadSummary(
                thread_id=thread_id,
                cwd="/work",
                name="",
                preview="",
                created_at=1,
                updated_at=1,
                source="cli",
                status=self.status,
            )
        )

    @staticmethod
    def get_thread_goal(_thread_id: str, **_kwargs: Any) -> None:
        return None

    def start_turn(self, **kwargs: Any) -> dict[str, Any]:
        self.start_calls.append(kwargs)
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_release is not None:
            self.start_release.wait(timeout=2)
        if self.start_error is not None:
            raise self.start_error
        return self.start_response

    def steer_turn(self, **kwargs: Any) -> dict[str, Any]:
        self.steer_calls.append(kwargs)
        if self.steer_error is not None:
            raise self.steer_error
        return self.steer_response

    def capture_connection_generation(self) -> int:
        return self.generation

    def run_if_connection_generation(self, generation: int, callback):
        if generation != self.generation:
            raise CodexRpcPreSendError(
                "turn/start",
                RuntimeError("backend generation changed"),
            )
        return callback()


class _SlowProfileStore(WebWriterProfileStore):
    def __init__(self, data_dir: pathlib.Path) -> None:
        super().__init__(data_dir)
        self.block_loads = False
        self.load_calls = 0
        self.load_entered = threading.Event()
        self.load_release = threading.Event()

    def load(self, client_id: str) -> WebWriterProfile | None:
        self.load_calls += 1
        if self.block_loads:
            self.load_entered.set()
            if not self.load_release.wait(timeout=2):
                raise TimeoutError("test profile load was not released")
        return super().load(client_id)


@dataclass
class _Harness:
    coordinator: WebPromptSubmissionCoordinator
    documents: WebDocumentRegistry
    workspace: Any
    operations: _Operations
    read_model: WebThreadReadModel
    projection: _Projection
    backend: _Backend
    settings_calls: list[None]


class WebPromptSubmissionTests(unittest.TestCase):
    client_id = "tab-1"
    thread_id = "thread-1"

    def setUp(self) -> None:
        self.harness = self._build()

    def _build(
        self,
        *,
        runtime_guard=lambda: None,
        runtime_call=lambda callback, *args, **kwargs: callback(*args, **kwargs),
    ) -> _Harness:
        documents = WebDocumentRegistry(runtime_context_guard=runtime_guard)
        documents.mark_connected(self.client_id)
        documents.materialize_thread(self.client_id, self.thread_id)
        workspace = _Workspace()
        operations = _Operations()
        read_model = WebThreadReadModel()
        projection = _Projection()
        backend = _Backend()
        settings_calls: list[None] = []

        def next_turn_settings() -> WebNextTurnSettings:
            settings_calls.append(None)
            return WebNextTurnSettings(
                approval_policy="on-request",
                permissions_profile_id=":workspace",
                model="gpt-test",
                reasoning_effort="high",
            )

        coordinator = WebPromptSubmissionCoordinator(
            documents=documents,
            workspace=workspace,  # type: ignore[arg-type]
            operations=operations,  # type: ignore[arg-type]
            goal_policy=_GoalPolicy(),  # type: ignore[arg-type]
            read_model=read_model,
            projection=projection,  # type: ignore[arg-type]
            next_turn_settings=next_turn_settings,
            ports=WebPromptSubmissionPorts(
                read_thread=backend.read_thread,
                get_thread_goal=backend.get_thread_goal,
                start_turn=backend.start_turn,
                steer_turn=backend.steer_turn,
                capture_connection_generation=backend.capture_connection_generation,
                run_if_connection_generation=backend.run_if_connection_generation,
            ),
            runtime_context_guard=runtime_guard,
            runtime_call=runtime_call,
        )
        return _Harness(
            coordinator,
            documents,
            workspace,
            operations,
            read_model,
            projection,
            backend,
            settings_calls,
        )

    def _prepare(
        self,
        mutation_id: str | None = None,
        *,
        text: str = "hello",
        attachment_ids: list[str] | None = None,
        harness: _Harness | None = None,
        client_id: str | None = None,
        thread_id: str | None = None,
    ):
        owner = harness or self.harness
        source_client_id = client_id or self.client_id
        source_thread_id = thread_id or self.thread_id
        return owner.coordinator.prepare_prompt(
            source_client_id,
            source_thread_id,
            mutation_id=mutation_id or str(uuid.uuid4()),
            text=text,
            attachment_ids=attachment_ids,
            source_scope_generation=1,
            source_attachment_scope=f"thread:{source_thread_id}",
            source_composer_scope_id=(
                f"{source_client_id}:generation:1:thread:{source_thread_id}"
            ),
        )

    def _result(self, prepared, *, harness: _Harness | None = None) -> dict[str, str]:
        owner = harness or self.harness
        return owner.coordinator.run_prepared_prompt(prepared)

    def test_start_success_uses_one_rpc_and_authoritative_turn_id(self) -> None:
        prepared = self._prepare()

        result = self._result(prepared)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "start")
        self.assertEqual(result["turn_id"], "turn-started")
        self.assertEqual(result["client_user_message_id"], f"focus-web:{prepared.mutation_id}")
        self.assertEqual(len(self.harness.backend.start_calls), 1)
        self.assertEqual(self.harness.backend.steer_calls, [])

    def test_start_without_cached_active_id_keeps_the_single_official_start_effect(self) -> None:
        self.harness.backend.status = "active"
        prepared = self._prepare()

        result = self._result(prepared)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(self.harness.backend.start_calls), 1)
        self.assertEqual(self.harness.backend.steer_calls, [])
        self.assertEqual(self.harness.settings_calls, [])

    def test_pre_send_and_decoded_rejection_are_known_no_effect(self) -> None:
        cases = (
            CodexRpcPreSendError("turn/start", RuntimeError("offline")),
            CodexRpcError("turn/start", {"message": "rejected"}),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                harness = self._build()
                harness.backend.start_error = error
                result = self._result(self._prepare(harness=harness), harness=harness)
                self.assertEqual(result["status"], "known_no_effect")
                self.assertEqual(result["turn_id"], "")

    def test_transport_protocol_timeout_and_missing_start_id_are_unknown(self) -> None:
        cases: tuple[Exception | None, dict[str, Any] | None] = (
            (CodexRpcTransportError("turn/start", {"message": "lost"}), None),
            (CodexRpcProtocolError("turn/start", "malformed"), None),
            (TimeoutError("deadline"), None),
            (None, {"turn": {}}),
        )
        for error, response in cases:
            with self.subTest(error=type(error).__name__ if error else "missing_id"):
                harness = self._build()
                harness.backend.start_error = error
                if response is not None:
                    harness.backend.start_response = response
                result = self._result(self._prepare(harness=harness), harness=harness)
                self.assertEqual(result["status"], "outcome_unknown")
                self.assertEqual(result["turn_id"], "")

    def test_exact_active_turn_steers_once_without_start_fallback(self) -> None:
        self.harness.read_model.replace_turns(
            self.thread_id,
            [{"id": "turn-A", "status": "inProgress", "items": []}],
        )
        self.harness.backend.status = "active"
        prepared = self._prepare()

        result = self._result(prepared)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["turn_id"], "turn-A")
        self.assertEqual(len(self.harness.backend.steer_calls), 1)
        self.assertEqual(self.harness.backend.start_calls, [])
        self.assertEqual(self.harness.backend.read_calls, [])
        self.assertEqual(self.harness.settings_calls, [])
        self.assertEqual(self.harness.workspace.remembered_cwds, [])

    def test_steer_successor_or_no_active_is_known_no_effect_without_retarget(self) -> None:
        messages = (
            "expected active turn id `turn-A` but found `turn-B`",
            "no active turn to steer",
        )
        for message in messages:
            with self.subTest(message=message):
                harness = self._build()
                harness.read_model.replace_turns(
                    self.thread_id,
                    [{"id": "turn-A", "status": "inProgress", "items": []}],
                )
                harness.backend.status = "active"
                harness.backend.steer_error = CodexRpcError(
                    "turn/steer",
                    {"message": message},
                )
                result = self._result(self._prepare(harness=harness), harness=harness)
                self.assertEqual(result["status"], "known_no_effect")
                self.assertEqual(result["reason_code"], "active_turn_changed")
                self.assertEqual(len(harness.backend.steer_calls), 1)
                self.assertEqual(harness.backend.start_calls, [])

    def test_steer_ack_turn_mismatch_is_unknown_and_never_retargets(self) -> None:
        self.harness.read_model.replace_turns(
            self.thread_id,
            [{"id": "turn-A", "status": "inProgress", "items": []}],
        )
        self.harness.backend.status = "active"
        self.harness.backend.steer_response = {"turnId": "turn-B"}

        result = self._result(self._prepare())

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["reason_code"], "steer_result_turn_mismatch")
        self.assertEqual(self.harness.backend.start_calls, [])

    def test_duplicate_pending_and_terminal_requests_never_readmit_or_reexecute(self) -> None:
        mutation_id = str(uuid.uuid4())
        original = self._prepare(mutation_id)
        duplicate_pending = self._prepare(mutation_id)

        self.assertTrue(original.should_execute)
        self.assertFalse(duplicate_pending.should_execute)
        self.assertEqual(len(self.harness.operations.admissions), 1)
        self.assertEqual(self.harness.workspace.scope_checks, 2)
        self.assertEqual(self.harness.workspace.scope_claims, 0)

        terminal = self._result(original)
        duplicate_result = self._result(duplicate_pending)
        duplicate_terminal = self._prepare(mutation_id)
        terminal_again = self._result(duplicate_terminal)

        self.assertEqual(duplicate_result, terminal)
        self.assertEqual(terminal_again, terminal)
        self.assertEqual(len(self.harness.operations.admissions), 1)
        self.assertEqual(len(self.harness.backend.start_calls), 1)
        self.assertEqual(self.harness.workspace.scope_claims, 1)

    def test_stale_scope_is_known_no_effect_before_metadata_or_upstream_rpc(self) -> None:
        self.harness.workspace.scope_is_current = False

        result = self._result(self._prepare())

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "stale_attachment_scope")
        self.assertEqual(self.harness.workspace.scope_claims, 1)
        self.assertEqual(self.harness.backend.read_calls, [])
        self.assertEqual(self.harness.backend.start_calls, [])
        self.assertEqual(self.harness.backend.steer_calls, [])

    def test_mutation_identity_conflicts_across_thread_document_and_source_scope(self) -> None:
        mutation_id = str(uuid.uuid4())
        self._prepare(mutation_id)
        self.harness.documents.materialize_thread(self.client_id, "thread-2")

        with self.assertRaises(WebRuntimeError) as cross_thread:
            self._prepare(mutation_id, thread_id="thread-2")
        self.assertEqual(cross_thread.exception.code, "prompt_mutation_conflict")

        self.harness.documents.materialize_thread(self.client_id, self.thread_id)
        self.harness.documents.mark_document_reissued(self.client_id)
        self.harness.documents.mark_connected(self.client_id)
        self.harness.documents.materialize_thread(self.client_id, self.thread_id)
        with self.assertRaises(WebRuntimeError) as replaced_document:
            self._prepare(mutation_id)
        self.assertEqual(replaced_document.exception.code, "prompt_mutation_conflict")
        self.assertEqual(len(self.harness.operations.admissions), 1)

    def test_capacity_refusal_is_a_proven_pre_effect_429(self) -> None:
        for _index in range(256):
            self._prepare()

        with self.assertRaises(WebRuntimeError) as caught:
            self._prepare()

        self.assertEqual(caught.exception.code, "prompt_result_capacity")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.harness.backend.start_calls, [])

    def test_backend_generation_replacement_prevents_any_upstream_effect(self) -> None:
        prepared = self._prepare()
        self.harness.backend.generation += 1

        result = self._result(prepared)

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(self.harness.backend.start_calls, [])

    def test_attachment_claim_settlement_tracks_effect_evidence(self) -> None:
        success = self._result(self._prepare(attachment_ids=["file-1"]))
        self.assertEqual(success["status"], "succeeded")
        self.assertEqual(self.harness.workspace.releases, 1)

        unknown_harness = self._build()
        unknown_harness.backend.start_error = CodexRpcTransportError(
            "turn/start", {"message": "lost"}
        )
        unknown = self._result(
            self._prepare(attachment_ids=["file-2"], harness=unknown_harness),
            harness=unknown_harness,
        )
        self.assertEqual(unknown["status"], "outcome_unknown")
        self.assertEqual(unknown_harness.workspace.releases, 1)
        self.assertEqual(unknown_harness.workspace.rollbacks, 0)

        rejected_harness = self._build()
        rejected_harness.backend.start_error = CodexRpcPreSendError(
            "turn/start", RuntimeError("offline")
        )
        rejected = self._result(
            self._prepare(attachment_ids=["file-3"], harness=rejected_harness),
            harness=rejected_harness,
        )
        self.assertEqual(rejected["status"], "known_no_effect")
        self.assertEqual(rejected_harness.workspace.rollbacks, 1)

    def test_attachment_rollback_failure_is_explicit_and_fail_closed(self) -> None:
        self.harness.workspace.rollback_succeeds = False
        self.harness.backend.start_error = CodexRpcPreSendError(
            "turn/start", RuntimeError("offline")
        )

        result = self._result(self._prepare(attachment_ids=["file-1"]))

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "attachment_rollback_failed")

    def test_transcript_evidence_upgrades_pending_or_unknown_but_not_no_effect(self) -> None:
        pending = self._prepare()
        pending_turn = {
            "id": "turn-observed",
            "items": [
                {
                    "type": "userMessage",
                    "clientId": pending.client_user_message_id,
                }
            ],
        }
        self.assertTrue(
            self.harness.coordinator.reconcile_prompt_results_from_turns(
                self.thread_id,
                [pending_turn],
            )
        )
        observed = self.harness.coordinator.prompt_result(
            self.client_id,
            self.thread_id,
            mutation_id=pending.mutation_id,
        )
        self.assertEqual(observed["status"], "succeeded")
        self.assertEqual(observed["reason_code"], "transcript_observed")

        unknown_harness = self._build()
        unknown_harness.backend.start_error = TimeoutError("lost")
        unknown_prepared = self._prepare(harness=unknown_harness)
        self._result(unknown_prepared, harness=unknown_harness)
        self.assertTrue(
            unknown_harness.coordinator.reconcile_prompt_results_from_turns(
                self.thread_id,
                [
                    {
                        "id": "turn-late",
                        "items": [
                            {
                                "type": "userMessage",
                                "clientId": unknown_prepared.client_user_message_id,
                            }
                        ],
                    }
                ],
            )
        )

        rejected_harness = self._build()
        rejected_harness.backend.start_error = CodexRpcPreSendError(
            "turn/start", RuntimeError("offline")
        )
        rejected_prepared = self._prepare(harness=rejected_harness)
        rejected = self._result(rejected_prepared, harness=rejected_harness)
        self.assertFalse(
            rejected_harness.coordinator.reconcile_prompt_results_from_turns(
                self.thread_id,
                [
                    {
                        "id": "impossible-turn",
                        "items": [
                            {
                                "type": "userMessage",
                                "clientId": rejected_prepared.client_user_message_id,
                            }
                        ],
                    }
                ],
            )
        )
        self.assertEqual(
            rejected_harness.coordinator.prompt_result(
                self.client_id,
                self.thread_id,
                mutation_id=rejected_prepared.mutation_id,
            ),
            rejected,
        )

    def test_steer_transcript_reconciliation_preserves_frozen_turn_id(self) -> None:
        self.harness.read_model.replace_turns(
            self.thread_id,
            [{"id": "turn-A", "status": "inProgress", "items": []}],
        )
        prepared = self._prepare()

        self.assertTrue(
            self.harness.coordinator.reconcile_prompt_results_from_turns(
                self.thread_id,
                [
                    {
                        "id": "turn-B",
                        "items": [
                            {
                                "type": "userMessage",
                                "clientId": prepared.client_user_message_id,
                            }
                        ],
                    }
                ],
            )
        )

        observed = self.harness.coordinator.prompt_result(
            self.client_id,
            self.thread_id,
            mutation_id=prepared.mutation_id,
        )
        self.assertEqual(observed["status"], "succeeded")
        self.assertEqual(observed["mode"], "steer")
        self.assertEqual(observed["turn_id"], "turn-A")

    def test_start_transcript_without_turn_id_is_not_success_evidence(self) -> None:
        prepared = self._prepare()

        reconciled = self.harness.coordinator.reconcile_prompt_results_from_turns(
            self.thread_id,
            [
                {
                    "id": "",
                    "items": [
                        {
                            "type": "userMessage",
                            "clientId": prepared.client_user_message_id,
                        }
                    ],
                }
            ],
        )

        self.assertFalse(reconciled)
        current = self.harness.coordinator.prompt_result(
            self.client_id,
            self.thread_id,
            mutation_id=prepared.mutation_id,
        )
        self.assertEqual(current["status"], "pending")
        self.assertEqual(current["turn_id"], "")

    def test_evicted_duplicate_never_falls_back_to_cached_pending_receipt(self) -> None:
        mutation_id = str(uuid.uuid4())
        original = self._prepare(mutation_id)
        duplicate = self._prepare(mutation_id)
        self._result(original)
        for _index in range(256):
            self._result(self._prepare())

        with self.assertRaises(WebRuntimeError) as caught:
            self._result(duplicate)

        self.assertEqual(caught.exception.code, "prompt_result_unavailable")
        self.assertEqual(len(self.harness.backend.start_calls), 257)

    def test_fresh_prepare_after_terminal_eviction_is_a_new_bounded_slot(self) -> None:
        mutation_id = str(uuid.uuid4())
        self._result(self._prepare(mutation_id))
        for _index in range(256):
            self._result(self._prepare())

        expired_id_reuse = self._prepare(mutation_id)
        result = self._result(expired_id_reuse)

        self.assertTrue(expired_id_reuse.should_execute)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mutation_id"], mutation_id)
        self.assertEqual(len(self.harness.backend.start_calls), 258)

    def test_backend_retirement_invalidates_process_local_result_locator(self) -> None:
        prepared = self._prepare()
        self._result(prepared)

        retirement = self.harness.coordinator.retire_backend_epoch_after_stop()

        self.assertEqual(retirement.count, 1)
        with self.assertRaises(WebRuntimeError) as caught:
            self.harness.coordinator.prompt_result(
                self.client_id,
                self.thread_id,
                mutation_id=prepared.mutation_id,
            )
        self.assertEqual(caught.exception.code, "prompt_result_unavailable")

    def test_post_effect_backend_retirement_unavailable_is_not_no_effect(self) -> None:
        release_entered = threading.Event()
        release_settlement = threading.Event()
        original_release = (
            self.harness.workspace.release_prompt_attachment_claim_external
        )

        def block_after_effect(receipt: object) -> None:
            original_release(receipt)
            release_entered.set()
            if not release_settlement.wait(timeout=2):
                raise TimeoutError("test did not release prompt settlement")

        self.harness.workspace.release_prompt_attachment_claim_external = (
            block_after_effect
        )
        prepared = self._prepare(attachment_ids=["file-1"])
        errors: list[Exception] = []

        def run() -> None:
            try:
                self._result(prepared)
            except Exception as exc:  # exact late-settlement evidence
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(release_entered.wait(timeout=1))
        self.assertEqual(len(self.harness.backend.start_calls), 1)

        retirement = self.harness.coordinator.retire_backend_epoch_after_stop()
        self.assertEqual(retirement.count, 1)
        release_settlement.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], WebRuntimeError)
        assert isinstance(errors[0], WebRuntimeError)
        self.assertEqual(errors[0].code, "prompt_result_unavailable")
        self.assertEqual(errors[0].status, 409)
        # The upstream start already happened. A browser must therefore treat
        # this valid Focus 409 envelope as possibly-sent, never pre-effect.
        self.assertEqual(len(self.harness.backend.start_calls), 1)

    def test_prompt_specific_abandon_is_exact_and_duplicate_has_no_authority(self) -> None:
        original = self._prepare()
        duplicate = self._prepare(original.mutation_id)

        self.assertFalse(self.harness.coordinator.abandon_prompt(duplicate))
        self.assertTrue(self.harness.coordinator.abandon_prompt(original))
        result = self.harness.coordinator.prompt_result(
            self.client_id,
            self.thread_id,
            mutation_id=original.mutation_id,
        )
        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "request_cancelled_before_effect")

    def test_abandon_after_effect_claim_cannot_rewrite_the_result(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        self.harness.backend.start_entered = entered
        self.harness.backend.start_release = release
        prepared = self._prepare()
        results: list[dict[str, str]] = []
        worker = threading.Thread(target=lambda: results.append(self._result(prepared)))
        worker.start()
        self.assertTrue(entered.wait(timeout=1))

        self.assertFalse(self.harness.coordinator.abandon_prompt(prepared))
        release.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "succeeded")

    def test_get_is_read_only_and_never_dispatches_work(self) -> None:
        prepared = self._prepare()

        result = self.harness.coordinator.prompt_result(
            self.client_id,
            self.thread_id,
            mutation_id=prepared.mutation_id,
        )

        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.harness.backend.start_calls, [])
        self.assertEqual(self.harness.backend.steer_calls, [])

    def test_real_slow_profile_claim_runs_off_loop_and_duplicate_does_not_reload(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = pathlib.Path(temporary.name)
        working_dir = data_dir / "workspace"
        working_dir.mkdir()
        profiles = _SlowProfileStore(data_dir)
        profiles.save(
            WebWriterProfile(
                client_id=self.client_id,
                selected_thread_id=self.thread_id,
                working_dir=str(working_dir),
                scope_generation=1,
            )
        )
        runtime = RuntimeLoop(name="web-prompt-scope-store-test")
        self.addCleanup(runtime.stop)

        def build_inside_runtime() -> _Harness:
            documents = WebDocumentRegistry(
                runtime_context_guard=runtime.assert_worker_context
            )
            documents.mark_connected(self.client_id)
            documents.materialize_thread(self.client_id, self.thread_id)
            read_model = WebThreadReadModel()
            projection = _Projection()
            backend = _Backend()
            workspace = WebWriterWorkspaceCoordinator(
                profile_store=profiles,
                attachment_store=WebAttachmentStore(data_dir, ttl_seconds=300),
                documents=documents,
                selection=WebSelectionCoordinator(
                    profile_store=profiles,
                    document_registry=documents,
                    runtime_interest=WebRuntimeInterestRegistry(),
                ),
                read_model=read_model,
                effective_settings=ThreadEffectiveSettingsRegistry(),
                projection=projection,  # type: ignore[arg-type]
                ports=WebWriterWorkspacePorts(
                    list_models=lambda: [],
                    read_thread=backend.read_thread,
                ),
                runtime_context_guard=runtime.assert_worker_context,
                default_working_dir=str(working_dir),
            )
            operations = _Operations()
            settings_calls: list[None] = []

            def next_turn_settings() -> WebNextTurnSettings:
                settings_calls.append(None)
                return WebNextTurnSettings(
                    approval_policy="on-request",
                    permissions_profile_id=":workspace",
                )

            coordinator = WebPromptSubmissionCoordinator(
                documents=documents,
                workspace=workspace,
                operations=operations,  # type: ignore[arg-type]
                goal_policy=_GoalPolicy(),  # type: ignore[arg-type]
                read_model=read_model,
                projection=projection,  # type: ignore[arg-type]
                next_turn_settings=next_turn_settings,
                ports=WebPromptSubmissionPorts(
                    read_thread=backend.read_thread,
                    get_thread_goal=backend.get_thread_goal,
                    start_turn=backend.start_turn,
                    steer_turn=backend.steer_turn,
                    capture_connection_generation=(
                        backend.capture_connection_generation
                    ),
                    run_if_connection_generation=(
                        backend.run_if_connection_generation
                    ),
                ),
                runtime_context_guard=runtime.assert_worker_context,
                runtime_call=runtime.call,
            )
            return _Harness(
                coordinator,
                documents,
                workspace,
                operations,
                read_model,
                projection,
                backend,
                settings_calls,
            )

        harness = runtime.call(build_inside_runtime)
        profiles.block_loads = True
        mutation_id = str(uuid.uuid4())
        original = runtime.call(
            self._prepare,
            mutation_id,
            harness=harness,
        )
        duplicate = runtime.call(
            self._prepare,
            mutation_id,
            harness=harness,
        )
        self.assertFalse(profiles.load_entered.is_set())

        results: list[dict[str, str]] = []
        worker = threading.Thread(
            target=lambda: results.append(self._result(original, harness=harness))
        )
        worker.start()
        self.assertTrue(profiles.load_entered.wait(timeout=1))
        self.assertEqual(runtime.call(lambda: "sentinel"), "sentinel")

        profiles.load_release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "succeeded")
        self.assertEqual(self._result(duplicate, harness=harness), results[0])
        self.assertEqual(profiles.load_calls, 1)
        self.assertEqual(len(harness.operations.admissions), 1)
        self.assertEqual(len(harness.backend.start_calls), 1)

        profiles.block_loads = False
        stale = runtime.call(self._prepare, harness=harness)
        profiles.update(
            self.client_id,
            selected_thread_id=self.thread_id,
            scope_generation=2,
        )
        read_count = len(harness.backend.read_calls)
        start_count = len(harness.backend.start_calls)

        stale_result = self._result(stale, harness=harness)

        self.assertEqual(stale_result["status"], "known_no_effect")
        self.assertEqual(stale_result["reason_code"], "stale_attachment_scope")
        self.assertEqual(len(harness.backend.read_calls), read_count)
        self.assertEqual(len(harness.backend.start_calls), start_count)

    def test_slow_prompt_rpc_does_not_starve_runtime_loop(self) -> None:
        runtime = RuntimeLoop(name="web-prompt-staged-test")
        self.addCleanup(runtime.stop)

        def build_inside_runtime() -> _Harness:
            return self._build(
                runtime_guard=runtime.assert_worker_context,
                runtime_call=runtime.call,
            )

        harness = runtime.call(build_inside_runtime)
        entered = threading.Event()
        release = threading.Event()
        harness.backend.start_entered = entered
        harness.backend.start_release = release
        prepared = runtime.call(self._prepare, harness=harness)
        results: list[dict[str, str]] = []
        worker = threading.Thread(
            target=lambda: results.append(self._result(prepared, harness=harness))
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=1))

        self.assertEqual(runtime.call(lambda: "sentinel"), "sentinel")

        release.set()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
