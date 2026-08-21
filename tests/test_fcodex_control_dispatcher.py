import unittest
from unittest.mock import Mock, patch

from bot.adapter_ingress_gate import AdapterIngressGate, AdapterIngressSnapshot
from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.codex_protocol.client import CodexRpcError
from bot.direct_thread_target_policy import DirectThreadTargetRegistry
from bot.fcodex.control_dispatcher import FcodexControlDispatcher
from bot.operation_owner_coordinator import OperationOwnerCoordinator


_OPEN_INGRESS = AdapterIngressSnapshot(
    latest_generation=1,
    last_disconnected_generation=0,
    backend_reset_blocked=False,
    cleanup_required=False,
    disconnect_cleanup_pending=False,
)


def _root_snapshot(thread_id: str = "root-1") -> ThreadSnapshot:
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="root",
            preview="",
            created_at=1,
            updated_at=1,
            source="appServer",
            status="idle",
        )
    )


class FcodexControlDispatcherTests(unittest.TestCase):
    @staticmethod
    def _make_dispatcher(
        *,
        ingress_snapshot: AdapterIngressSnapshot = _OPEN_INGRESS,
    ) -> tuple[
        FcodexControlDispatcher,
        Mock,
        Mock,
        Mock,
        Mock,
    ]:
        adapter = Mock(spec=CodexAppServerAdapter)
        adapter.read_thread.return_value = _root_snapshot()
        adapter.get_thread_goal.return_value = None
        ingress_gate = Mock(spec=AdapterIngressGate)
        ingress_gate.snapshot.return_value = ingress_snapshot
        operation_owner = Mock(spec=OperationOwnerCoordinator)
        direct_thread_targets = DirectThreadTargetRegistry()
        dispatcher = FcodexControlDispatcher(
            adapter=adapter,
            adapter_ingress_gate=ingress_gate,
            operation_owner=operation_owner,
            direct_thread_targets=direct_thread_targets,
        )
        return (
            dispatcher,
            adapter,
            ingress_gate,
            operation_owner,
            direct_thread_targets,
        )

    def test_observer_era_root_reconcile_control_method_is_rejected(self) -> None:
        dispatcher, _adapter, _gate, owner, _targets = self._make_dispatcher()

        with self.assertRaisesRegex(ValueError, "未知 operation control method"):
            dispatcher.handle(
                "operation/reconcile-root",
                {
                    "participant_id": "fcodex:test",
                    "connection_id": "connection-1",
                    "root_thread_id": "root-1",
                },
            )

        owner.retry_authoritative_cleanups.assert_not_called()

    def test_every_closed_backend_epoch_fact_rejects_before_owner_dispatch(self) -> None:
        closed_states = (
            ("backend_reset_blocked", {"backend_reset_blocked": True}),
            ("cleanup_required", {"cleanup_required": True}),
            (
                "disconnect_cleanup_pending",
                {"disconnect_cleanup_pending": True},
            ),
        )
        for label, override in closed_states:
            with self.subTest(closed_fact=label):
                snapshot = AdapterIngressSnapshot(
                    latest_generation=1,
                    last_disconnected_generation=0,
                    backend_reset_blocked=override.get(
                        "backend_reset_blocked",
                        False,
                    ),
                    cleanup_required=override.get("cleanup_required", False),
                    disconnect_cleanup_pending=override.get(
                        "disconnect_cleanup_pending",
                        False,
                    ),
                )
                dispatcher, _, ingress_gate, owner, _ = self._make_dispatcher(
                    ingress_snapshot=snapshot
                )

                with self.assertRaisesRegex(RuntimeError, "fail-closed"):
                    dispatcher.handle(
                        "operation/participant-connected",
                        {
                            "participant_id": "fcodex:alice",
                            "connection_id": "connection-1",
                        },
                    )

                ingress_gate.snapshot.assert_called_once_with()
                self.assertEqual(owner.mock_calls, [])

    def test_participant_disconnect_bypasses_closed_epoch_for_cleanup(self) -> None:
        dispatcher, _, ingress_gate, owner, _ = self._make_dispatcher(
            ingress_snapshot=AdapterIngressSnapshot(
                latest_generation=1,
                last_disconnected_generation=1,
                backend_reset_blocked=True,
                cleanup_required=True,
                disconnect_cleanup_pending=True,
            )
        )
        owner.participant_disconnected.return_value = {
            "state": "disconnected",
            "retired_requests": 2,
        }

        result = dispatcher.handle(
            "operation/participant-disconnected",
            {
                "participant_id": " fcodex:alice ",
                "connection_id": " connection-1 ",
            },
        )

        self.assertEqual(
            result,
            {"state": "disconnected", "retired_requests": 2},
        )
        ingress_gate.snapshot.assert_not_called()
        owner.participant_disconnected.assert_called_once_with(
            "fcodex:alice",
            "connection-1",
        )

    def test_resume_goal_classification_projects_exact_admission_fact(self) -> None:
        cases = (
            (
                "active",
                ThreadGoalSummary(
                    thread_id="root-1",
                    objective="continue",
                    status="active",
                ),
                None,
                True,
                0,
            ),
            (
                "unreadable",
                None,
                RuntimeError("goal read unavailable"),
                True,
                1,
            ),
            (
                "disabled",
                None,
                CodexRpcError(
                    "thread/goal/get",
                    {"code": -32602, "message": "goals feature is disabled"},
                ),
                False,
                0,
            ),
        )
        for label, goal, error, expected_risk, warning_count in cases:
            with self.subTest(goal_state=label):
                dispatcher, adapter, _, owner, direct_targets = self._make_dispatcher()
                if error is not None:
                    adapter.get_thread_goal.side_effect = error
                else:
                    adapter.get_thread_goal.return_value = goal
                owner.admit.return_value = {"allowed": True}
                request_params = {"threadId": "root-1"}

                with patch("bot.fcodex.control_dispatcher.logger") as logger:
                    result = dispatcher.handle(
                        "operation/admit",
                        {
                            "participant_id": "fcodex:alice",
                            "connection_id": "connection-1",
                            "request_id": "resume-1",
                            "rpc_method": "thread/resume",
                            "thread_id": "root-1",
                            "request_params": request_params,
                        },
                    )

                self.assertEqual(result, {"allowed": True})
                adapter.read_thread.assert_called_once_with(
                    "root-1",
                    include_turns=False,
                )
                owner.remember_authoritative_direct_target.assert_called_once()
                self.assertTrue(direct_targets.is_known("root-1"))
                adapter.get_thread_goal.assert_called_once_with("root-1")
                owner.admit.assert_called_once_with(
                    participant_id="fcodex:alice",
                    connection_id="connection-1",
                    request_id="resume-1",
                    method="thread/resume",
                    thread_id="root-1",
                    request_params=request_params,
                    resume_may_autostart=expected_risk,
                    continuation_risk=False,
                )
                self.assertEqual(logger.warning.call_count, warning_count)

    def test_all_operation_methods_project_exact_owner_parameters(self) -> None:
        dispatcher, _, ingress_gate, owner, _ = self._make_dispatcher()
        participant = "fcodex:alice"
        connection = "connection-1"
        identity = {
            "participant_id": f" {participant} ",
            "connection_id": f" {connection} ",
        }

        owner.participant_connected.return_value = {"connected": True}
        self.assertEqual(
            dispatcher.handle("operation/participant-connected", identity),
            {"connected": True},
        )
        owner.participant_connected.assert_called_once_with(participant, connection)

        owner.participant_heartbeat.return_value = {"ok": True, "mode": "connected"}
        self.assertEqual(
            dispatcher.handle("operation/participant-heartbeat", identity),
            {"ok": True, "mode": "connected"},
        )
        owner.participant_heartbeat.assert_called_once_with(participant, connection)

        owner.has_connected_participant_connection.return_value = True
        self.assertEqual(
            dispatcher.handle("operation/transport-admit", identity),
            {"allowed": True},
        )
        owner.has_connected_participant_connection.assert_called_once_with(
            participant,
            connection,
        )

        owner.participant_disconnected.return_value = {"state": "grace"}
        self.assertEqual(
            dispatcher.handle("operation/participant-disconnected", identity),
            {"state": "grace"},
        )
        owner.participant_disconnected.assert_called_once_with(participant, connection)

        owner.admit.return_value = {"allowed": True, "request_token": 7}
        request_params = {"cwd": "/tmp/project"}
        self.assertEqual(
            dispatcher.handle(
                "operation/admit",
                {
                    **identity,
                    "request_id": "create-1",
                    "rpc_method": "thread/start",
                    "thread_id": "",
                    "request_params": request_params,
                },
            ),
            {"allowed": True, "request_token": 7},
        )
        owner.admit.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="create-1",
            method="thread/start",
            thread_id="",
            request_params=request_params,
            resume_may_autostart=False,
            continuation_risk=False,
        )

        owner.client_response.return_value = {"committed": True}
        response_result = {
            "thread": {
                "id": "root-1",
                "historyMode": "legacy",
                "cwd": "/tmp/project",
                "source": "appServer",
                "status": {"type": "idle"},
            }
        }
        self.assertEqual(
            dispatcher.handle(
                "operation/client-response",
                {
                    **identity,
                    "request_id": "resume-1",
                    "request_token": 9,
                    "outcome": "success",
                    "response_result": response_result,
                },
            ),
            {"committed": True},
        )
        owner.client_response.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="resume-1",
            request_token=9,
            outcome="success",
            response_result=response_result,
            observed_thread_id="root-1",
            observed_root_thread_id="root-1",
        )

        owner.server_request.return_value = {"routed": True}
        server_request_params = {"threadId": "root-1", "command": "pwd"}
        self.assertEqual(
            dispatcher.handle(
                "operation/server-request",
                {
                    **identity,
                    "request_id": "approval-1",
                    "rpc_method": "item/commandExecution/requestApproval",
                    "request_params": server_request_params,
                },
            ),
            {"routed": True},
        )
        owner.server_request.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            method="item/commandExecution/requestApproval",
            params=server_request_params,
        )

        owner.response_admit.return_value = {"allowed": True}
        response_identity = {
            **identity,
            "request_id": "approval-1",
            "response_token": "token-1",
        }
        self.assertEqual(
            dispatcher.handle("operation/request-response-admit", response_identity),
            {"allowed": True},
        )
        owner.response_admit.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            response_token="token-1",
        )

        owner.response_submit.return_value = {"accepted": True}
        self.assertEqual(
            dispatcher.handle(
                "operation/request-response-submit",
                {
                    **response_identity,
                    "response_result": {"decision": "accept"},
                    "response_error": {"code": -1, "message": "unused"},
                },
            ),
            {"accepted": True},
        )
        owner.response_submit.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            response_token="token-1",
            result={"decision": "accept"},
            error={"code": -1, "message": "unused"},
        )

        owner.response_invalid.return_value = {"accepted": False}
        self.assertEqual(
            dispatcher.handle("operation/request-response-invalid", response_identity),
            {"accepted": False},
        )
        owner.response_invalid.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            response_token="token-1",
        )

        self.assertEqual(
            dispatcher.handle("operation/request-response-sent", response_identity),
            {"ok": True},
        )
        owner.response_sent.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            response_token="token-1",
        )

        self.assertEqual(
            dispatcher.handle("operation/request-response-unknown", response_identity),
            {"ok": True},
        )
        owner.response_unknown.assert_called_once_with(
            participant_id=participant,
            connection_id=connection,
            request_id="approval-1",
            response_token="token-1",
        )

        notification_params = {"threadId": "root-1", "turnId": "turn-1"}
        self.assertEqual(
            dispatcher.handle(
                "operation/notification",
                {
                    **identity,
                    "rpc_method": "turn/completed",
                    "notification_params": notification_params,
                },
            ),
            {"ok": True},
        )
        owner.notification.assert_called_once_with(
            "turn/completed",
            notification_params,
        )

        self.assertEqual(ingress_gate.snapshot.call_count, 12)


if __name__ == "__main__":
    unittest.main()
