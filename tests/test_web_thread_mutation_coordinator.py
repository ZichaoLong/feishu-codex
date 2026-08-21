from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.turn_interrupt_audit import record_turn_interrupt_dispatch_attempt
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.event_coordinator import WebRuntimeEventCoordinator
from bot.web_runtime.thread_mutation_coordinator import (
    WebLifecycleTargetReader,
    WebLifecycleTargetReaderPorts,
    WebThreadMutationCoordinator,
    WebThreadMutationPorts,
)
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.writer_workspace_coordinator import WebWriterWorkspaceCoordinator


def _snapshot(
    *,
    status: str = "idle",
    path: str | None = "/work/.codex/sessions/thread-1.jsonl",
    turns: list[dict] | None = None,
) -> ThreadSnapshot:
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id="thread-1",
            cwd="/work",
            name="Demo",
            preview="hello",
            created_at=1,
            updated_at=2,
            source="appServer",
            status=status,
            path=path,
        ),
        turns=list(turns or []),
    )


class WebLifecycleTargetReaderTests(unittest.TestCase):
    def _reader(self, read_thread: Mock) -> WebLifecycleTargetReader:
        return WebLifecycleTargetReader(
            ports=WebLifecycleTargetReaderPorts(read_thread=read_thread),
            runtime_context_guard=Mock(),
        )

    def test_direct_read_distinguishes_present_archived_and_deleted(self) -> None:
        cases = (
            (
                _snapshot(path="/work/.codex/sessions/thread-1.jsonl"),
                "present",
            ),
            (
                _snapshot(path=r"C:\Users\me\.codex\archived_sessions\thread-1.jsonl"),
                "archived",
            ),
            (
                CodexRpcError(
                    "thread/read",
                    {"code": -32000, "message": "thread not found: thread-1"},
                ),
                "deleted",
            ),
            (
                CodexRpcError(
                    "thread/read",
                    {"code": -32000, "message": "thread not loaded: thread-1"},
                ),
                "deleted",
            ),
        )
        for outcome, expected in cases:
            with self.subTest(expected=expected, outcome=outcome):
                read_thread = Mock()
                if isinstance(outcome, Exception):
                    read_thread.side_effect = outcome
                else:
                    read_thread.return_value = outcome

                result = self._reader(read_thread).read("thread-1")

                self.assertEqual(result, expected)
                read_thread.assert_called_once_with("thread-1", False)

    def test_pathless_read_fails_closed(self) -> None:
        read_thread = Mock(return_value=_snapshot(path=None))

        with self.assertRaises(WebRuntimeError) as caught:
            self._reader(read_thread).read("thread-1")

        self.assertEqual(caught.exception.code, "lifecycle_verification_unavailable")
        self.assertEqual(caught.exception.status, 503)


class WebThreadMutationCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = Mock()
        self.documents = Mock(spec=WebDocumentRegistry)
        self.documents.is_connected.return_value = True
        self.documents.materialized_thread_id.return_value = "thread-1"
        self.direct_targets = Mock(spec=WebDirectThreadTargetCoordinator)
        self.operations = Mock(spec=WebOperationService)
        self.goal_policy = Mock(spec=WebGoalResumePolicy)
        self.read_model = Mock(spec=WebThreadReadModel)
        self.workspace = Mock(spec=WebWriterWorkspaceCoordinator)
        self.events = Mock(spec=WebRuntimeEventCoordinator)
        self.projection = Mock(spec=FocusWebProjection)
        self.projection.coordinates.return_value = {
            "instance_id": "instance-1",
            "stream_epoch": "epoch-1",
        }

        self.snapshot = _snapshot(
            turns=[{"id": "turn-1", "status": "inProgress", "items": []}]
        )
        self.direct_targets.read.return_value = self.snapshot
        self.read_model.active_turn_id_from_turns.return_value = "turn-1"
        self.lifecycle_read = Mock(return_value=self.snapshot)
        self.lifecycle_targets = WebLifecycleTargetReader(
            ports=WebLifecycleTargetReaderPorts(read_thread=self.lifecycle_read),
            runtime_context_guard=self.guard,
        )
        self.rename_thread = Mock()
        self.set_thread_goal = Mock()
        self.clear_thread_goal = Mock()
        self.archive_thread = Mock(
            return_value={
                "thread_id": "thread-1",
                "upstream_outcome": "success",
            }
        )
        self.unarchive_thread = Mock(
            return_value={
                "thread_id": "thread-1",
                "upstream_outcome": "success",
            }
        )
        self.delete_thread = Mock(
            return_value={
                "thread_id": "thread-1",
                "upstream_outcome": "success",
            }
        )
        self.interrupt_turn = Mock()
        self.ports = WebThreadMutationPorts(
            read_thread=Mock(return_value=self.snapshot),
            rename_thread=self.rename_thread,
            set_thread_goal=self.set_thread_goal,
            clear_thread_goal=self.clear_thread_goal,
            archive_thread=self.archive_thread,
            unarchive_thread=self.unarchive_thread,
            delete_thread=self.delete_thread,
            interrupt_turn=self.interrupt_turn,
        )

        self.autonomous_receipt = object()
        self.operations.admit_autonomous_turn.return_value = self.autonomous_receipt
        self.operations.holder.return_value = "web:tab-1"
        self.operations.upstream_outcome_unknown.return_value = False
        self.operations.is_unknown_mutation_error.return_value = False
        self.goal_policy.requires_writer_admission.return_value = False
        self.operations.run_writer_scoped_control_mutation.side_effect = (
            lambda *_args, **kwargs: kwargs["call"]()
        )
        self.coordinator = self._make_coordinator()

    def _make_coordinator(self) -> WebThreadMutationCoordinator:
        return WebThreadMutationCoordinator(
            documents=self.documents,
            direct_targets=self.direct_targets,
            operations=self.operations,
            lifecycle_targets=self.lifecycle_targets,
            goal_policy=self.goal_policy,
            workspace=self.workspace,
            events=self.events,
            projection=self.projection,
            ports=self.ports,
            runtime_context_guard=self.guard,
        )

    def test_runtime_guard_fails_before_any_dependency_access(self) -> None:
        self.guard.side_effect = RuntimeError("wrong runtime")

        with self.assertRaisesRegex(RuntimeError, "wrong runtime"):
            self.coordinator.rename_thread("tab-1", "thread-1", name="Renamed")

        self.documents.assert_runtime_context.assert_not_called()
        self.direct_targets.read.assert_not_called()
        self.operations.raise_other_writer.assert_not_called()
        self.rename_thread.assert_not_called()

    def test_pending_unknown_blocks_goal_before_writer_or_upstream_effect(self) -> None:
        self.operations.admit_explicit_web_effect.side_effect = WebRuntimeError(
            "pending mutation",
            code="mutation_reconciling",
            status=409,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.set_goal(
                "tab-1",
                "thread-1",
                objective="Continue",
                status="active",
                intent_generation=1,
            )

        self.assertEqual(caught.exception.code, "mutation_reconciling")
        self.operations.admit_explicit_web_effect.assert_called_once_with(
            "tab-1",
            "thread-1",
            operation="set_goal",
        )
        self.operations.admit_autonomous_turn.assert_called_once()
        self.operations.release_fresh_blank_autonomous_turn.assert_called_once()
        self.set_thread_goal.assert_not_called()

    def test_pending_unknown_blocks_lifecycle_before_upstream_effect(self) -> None:
        self.operations.admit_explicit_web_effect.side_effect = WebRuntimeError(
            "pending mutation",
            code="mutation_reconciling",
            status=409,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.archive_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "mutation_reconciling")
        self.operations.admit_explicit_web_effect.assert_called_once_with(
            "tab-1",
            "thread-1",
            operation="archive",
        )
        self.operations.holder.assert_not_called()
        self.archive_thread.assert_not_called()
        self.events.drop_thread_after_lifecycle.assert_not_called()

    def test_interrupt_orders_live_materialized_authority_before_transport(
        self,
    ) -> None:
        order: list[str] = []
        self.documents.is_connected.side_effect = (
            lambda _client_id: order.append("connected") or True
        )
        self.documents.materialized_thread_id.side_effect = (
            lambda _client_id: order.append("materialized") or "thread-1"
        )
        self.direct_targets.read.side_effect = lambda *_args, **_kwargs: (
            order.append("authority-read") or self.snapshot
        )
        self.interrupt_turn.side_effect = lambda **_kwargs: order.append("transport")

        def _record_audit(**kwargs: object) -> None:
            order.append("audit")
            record_turn_interrupt_dispatch_attempt(**kwargs)  # type: ignore[arg-type]

        with patch(
            "bot.web_runtime.thread_mutation_coordinator."
            "record_turn_interrupt_dispatch_attempt",
            side_effect=_record_audit,
        ):
            with self.assertLogs("bot.turn_interrupt_audit", level="INFO") as audit:
                result = self.coordinator.interrupt(
                    "tab-1",
                    "thread-1",
                    turn_id="turn-1",
                )

        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(
            order,
            [
                "connected",
                "materialized",
                "authority-read",
                "audit",
                "transport",
            ],
        )
        self.direct_targets.read.assert_called_once_with(
            "thread-1",
            operation="中断",
            include_turns=False,
        )
        self.read_model.active_turn_id_from_turns.assert_not_called()
        self.operations.require_active_turn_writer.assert_not_called()
        self.operations.require_no_unknown_mutation.assert_not_called()
        self.interrupt_turn.assert_called_once_with(
            thread_id="thread-1",
            turn_id="turn-1",
        )
        audit_text = "\n".join(audit.output)
        self.assertIn("source=web_document", audit_text)
        self.assertNotIn("thread-1", audit_text)
        self.assertNotIn("turn-1", audit_text)

    def test_interrupt_audit_sink_failure_does_not_block_transport(self) -> None:
        with patch(
            "bot.turn_interrupt_audit.logger.info",
            side_effect=RuntimeError("log sink failed"),
        ):
            result = self.coordinator.interrupt(
                "tab-1",
                "thread-1",
                turn_id="turn-1",
            )

        self.assertEqual(result["turn_id"], "turn-1")
        self.interrupt_turn.assert_called_once_with(
            thread_id="thread-1",
            turn_id="turn-1",
        )

    def test_interrupt_requires_exact_or_empty_unmodified_turn_id(self) -> None:
        for turn_id in (None, 1, True, " ", " turn-1", "turn-1 "):
            with self.subTest(turn_id=turn_id):
                with self.assertRaises(WebRuntimeError) as caught:
                    self.coordinator.interrupt(
                        "tab-1",
                        "thread-1",
                        turn_id=turn_id,  # type: ignore[arg-type]
                    )
                self.assertEqual(caught.exception.code, "invalid_turn_id")
                self.assertEqual(caught.exception.status, 400)
        self.direct_targets.read.assert_not_called()
        self.interrupt_turn.assert_not_called()

    def test_interrupt_forwards_empty_startup_target_unchanged(self) -> None:
        result = self.coordinator.interrupt(
            "tab-1",
            "thread-1",
            turn_id="",
        )

        self.assertEqual(result["turn_id"], "")
        self.interrupt_turn.assert_called_once_with(
            thread_id="thread-1",
            turn_id="",
        )

    def test_interrupt_rejects_disconnected_before_materialization_or_read(
        self,
    ) -> None:
        self.documents.is_connected.return_value = False

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.interrupt(
                "tab-1",
                "thread-1",
                turn_id="turn-1",
            )

        self.assertEqual(caught.exception.code, "web_writer_disconnected")
        self.documents.materialized_thread_id.assert_not_called()
        self.direct_targets.read.assert_not_called()
        self.interrupt_turn.assert_not_called()

    def test_interrupt_requires_exact_materialized_root_before_read(self) -> None:
        self.documents.materialized_thread_id.return_value = "thread-2"

        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.interrupt(
                "tab-1",
                "thread-1",
                turn_id="turn-1",
            )

        self.assertEqual(caught.exception.code, "thread_not_materialized")
        self.assertEqual(caught.exception.status, 409)
        self.direct_targets.read.assert_not_called()
        self.interrupt_turn.assert_not_called()

    def test_interrupt_ignores_stale_or_empty_turns_projection(self) -> None:
        for turns in (
            [],
            [{"id": "turn-2", "status": "inProgress", "items": []}],
        ):
            with self.subTest(turns=turns):
                self.interrupt_turn.reset_mock()
                self.snapshot.turns[:] = turns

                result = self.coordinator.interrupt(
                    "tab-1",
                    "thread-1",
                    turn_id="turn-1",
                )

                self.assertEqual(result["turn_id"], "turn-1")
                self.interrupt_turn.assert_called_once_with(
                    thread_id="thread-1",
                    turn_id="turn-1",
                )
        self.read_model.active_turn_id_from_turns.assert_not_called()
        for call in self.direct_targets.read.call_args_list:
            self.assertEqual(call.kwargs["include_turns"], False)

    def test_interrupt_maps_known_upstream_races_to_typed_conflict(self) -> None:
        for message, code in (
            ("no active turn to interrupt", "no_active_turn"),
            (
                "expected active turn id turn-1 but found turn-2",
                "active_turn_changed",
            ),
        ):
            with self.subTest(message=message):
                self.interrupt_turn.reset_mock()
                self.interrupt_turn.side_effect = CodexRpcError(
                    "turn/interrupt",
                    {"code": -32602, "message": message},
                )
                with self.assertRaises(WebRuntimeError) as caught:
                    self.coordinator.interrupt(
                        "tab-1",
                        "thread-1",
                        turn_id="turn-1",
                    )
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, 409)
                self.interrupt_turn.assert_called_once_with(
                    thread_id="thread-1",
                    turn_id="turn-1",
                )

    def test_interrupt_unknown_transport_protocol_or_timeout_is_not_retried(
        self,
    ) -> None:
        self.operations.is_unknown_mutation_error.side_effect = (
            WebOperationService.is_unknown_mutation_error
        )
        errors = (
            CodexRpcTransportError(
                "turn/interrupt",
                {"code": -32000, "message": "transport lost"},
            ),
            CodexRpcProtocolError("turn/interrupt", "malformed result"),
            TimeoutError("interrupt result unknown"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.interrupt_turn.reset_mock()
                self.interrupt_turn.side_effect = error
                with self.assertRaises(WebRuntimeError) as caught:
                    self.coordinator.interrupt(
                        "tab-1",
                        "thread-1",
                        turn_id="turn-1",
                    )
                self.assertEqual(caught.exception.code, "turn_effect_unknown")
                self.assertEqual(caught.exception.status, 503)
                self.interrupt_turn.assert_called_once_with(
                    thread_id="thread-1",
                    turn_id="turn-1",
                )

    def test_unknown_lifecycle_result_records_process_local_exact_mutation_id(
        self,
    ) -> None:
        self.archive_thread.return_value = {
            "thread_id": "thread-1",
            "upstream_outcome": "unknown",
            "focus_cleanup": "skipped",
        }
        self.operations.upstream_outcome_unknown.return_value = True
        self.operations.record_unknown_mutation.return_value = SimpleNamespace(
            mutation_id="mutation/exact-123"
        )

        result = self.coordinator.archive_thread("tab-1", "thread-1")

        self.assertEqual(result["mutation_id"], "mutation/exact-123")
        self.operations.record_unknown_mutation.assert_called_once_with(
            "thread-1",
            operation="archive",
            client_id="tab-1",
        )
        self.events.drop_thread_after_lifecycle.assert_not_called()
        self.workspace.delete_thread_scope.assert_not_called()

    def test_goal_result_keeps_or_releases_the_exact_autonomous_submission(
        self,
    ) -> None:
        continuing_goal = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Continue",
            status="active",
        )
        self.set_thread_goal.return_value = continuing_goal
        self.goal_policy.requires_writer_admission.return_value = True

        self.coordinator.set_goal(
            "tab-1",
            "thread-1",
            objective="Continue",
            intent_generation=1,
        )

        self.operations.admit_autonomous_turn.assert_called_once_with(
            "tab-1",
            "thread-1",
            allow_fresh=True,
        )
        self.operations.release_fresh_blank_autonomous_turn.assert_not_called()

        self.operations.admit_autonomous_turn.reset_mock()
        self.goal_policy.requires_writer_admission.return_value = False
        self.set_thread_goal.return_value = ThreadGoalSummary(
            thread_id="thread-1",
            objective="Paused",
            status="paused",
        )
        self.coordinator.set_goal(
            "tab-1",
            "thread-1",
            objective="Paused",
            intent_generation=2,
        )

        self.operations.release_fresh_blank_autonomous_turn.assert_called_once_with(
            self.autonomous_receipt,
            reason="web_goal_mutation_known_no_start",
        )

    def test_delete_cleanup_schedule_failure_preserves_upstream_success(
        self,
    ) -> None:
        self.delete_thread.return_value = {
            "thread_id": "thread-1",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleanup_errors": ["existing warning"],
        }
        self.events.schedule_attachment_cleanup.side_effect = OSError(
            "worker closed"
        )

        result = self.coordinator.delete_thread(
            "tab-1",
            "thread-1",
            confirmation="thread-1",
        )

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "incomplete")
        self.assertEqual(
            result["cleanup_errors"],
            [
                "existing warning",
                "Web attachment cleanup could not be scheduled: worker closed",
            ],
        )
        self.events.schedule_attachment_cleanup.assert_called_once_with(
            "thread-1"
        )
        self.workspace.delete_thread_scope.assert_not_called()
        self.events.drop_thread_after_lifecycle.assert_called_once_with("thread-1")

    def test_unknown_recovery_delegates_exact_mutation_id(self) -> None:
        resolve_result = {"resolved": True}
        verify_result = {"verified": True}
        self.operations.resolve_unknown_mutation.return_value = resolve_result
        self.operations.verify_unknown_lifecycle_mutation.return_value = verify_result
        mutation_id = "  exact/mutation#1  "

        self.assertIs(
            self.coordinator.resolve_unknown_mutation(
                "tab-1",
                "thread-1",
                action="abandon",
                mutation_id=mutation_id,
            ),
            resolve_result,
        )
        self.assertIs(
            self.coordinator.verify_unknown_lifecycle_mutation(
                "tab-1",
                "thread-1",
                mutation_id=mutation_id,
            ),
            verify_result,
        )
        self.operations.resolve_unknown_mutation.assert_called_once_with(
            "tab-1",
            "thread-1",
            action="abandon",
            mutation_id=mutation_id,
        )
        self.operations.verify_unknown_lifecycle_mutation.assert_called_once_with(
            "tab-1",
            "thread-1",
            mutation_id=mutation_id,
        )


def test_mutation_coordinator_requires_runtime_loop_guard() -> None:
    try:
        WebThreadMutationCoordinator(
            documents=None,  # type: ignore[arg-type]
            direct_targets=None,  # type: ignore[arg-type]
            operations=Mock(spec=WebOperationService),
            lifecycle_targets=WebLifecycleTargetReader(
                ports=WebLifecycleTargetReaderPorts(
                    read_thread=lambda _thread_id, _include_turns: _snapshot()
                ),
                runtime_context_guard=lambda: None,
            ),
            goal_policy=None,  # type: ignore[arg-type]
            workspace=None,  # type: ignore[arg-type]
            events=None,  # type: ignore[arg-type]
            projection=None,  # type: ignore[arg-type]
            ports=WebThreadMutationPorts(
                read_thread=lambda _thread_id, _include_turns: _snapshot(),
                rename_thread=lambda _thread_id, _name: None,
                set_thread_goal=lambda *_args, **_kwargs: ThreadGoalSummary(
                    thread_id="thread-1",
                    objective="",
                    status="paused",
                ),
                clear_thread_goal=lambda *_args, **_kwargs: True,
                archive_thread=lambda *_args, **_kwargs: {},
                unarchive_thread=lambda *_args, **_kwargs: {},
                delete_thread=lambda *_args, **_kwargs: {},
                interrupt_turn=lambda **_kwargs: None,
            ),
            runtime_context_guard=None,  # type: ignore[arg-type]
        )
    except TypeError as exc:
        assert "RuntimeLoop context guard" in str(exc)
    else:  # pragma: no cover - fail explicitly without pytest dependency
        raise AssertionError("missing RuntimeLoop guard was accepted")
