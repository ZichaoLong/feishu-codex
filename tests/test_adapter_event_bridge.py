from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from collections.abc import Callable
from types import SimpleNamespace
from unittest import mock

from bot.adapter_event_bridge import (
    AdapterEventBridge,
    AdapterEventBridgePorts,
)
from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapters.base import ThreadSummary
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.direct_thread_target_policy import DirectThreadTargetRegistry
from bot.feishu_runtime_disconnect_projection import (
    FeishuRuntimeDisconnectReport,
)
from bot.server_request_contract import ServerRequestIdentity
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_web_interaction_holder,
)


class _BridgeHarness:
    def __init__(
        self,
        *,
        thread_subscribers: Callable[
            [str], tuple[tuple[str, str], ...]
        ] | None = None,
        resident_session: Callable[
            [tuple[str, str]], BindingSessionSnapshot | None
        ] | None = None,
        ingress_gate: AdapterIngressGate | None = None,
        interaction_leases: InteractionLeaseStore | None = None,
    ) -> None:
        self.events: list[str] = []
        self.ingress_gate = ingress_gate if ingress_gate is not None else mock.Mock()
        self.notification_pipeline = mock.Mock()
        self.server_requests = mock.Mock()
        self.interaction_requests = mock.Mock()
        self.interaction_auto_resolution = mock.Mock()
        self.direct_thread_targets = DirectThreadTargetRegistry()
        self.operation_owner = mock.Mock()
        self.web_runtime = mock.Mock()
        self.feishu_root_operations = mock.Mock()
        self.thread_runtime_authority = mock.Mock()
        self.interaction_leases = (
            interaction_leases
            if interaction_leases is not None
            else mock.Mock()
        )
        self.runtime_admin = mock.Mock()
        self.feishu_runtime_disconnect = mock.Mock()
        self.bridge = AdapterEventBridge(
            ingress_gate=self.ingress_gate,
            notification_pipeline=self.notification_pipeline,
            server_requests=self.server_requests,
            interaction_requests=self.interaction_requests,
            interaction_auto_resolution=self.interaction_auto_resolution,
            direct_thread_targets=self.direct_thread_targets,
            operation_owner=self.operation_owner,
            web_runtime=self.web_runtime,
            feishu_root_operations=self.feishu_root_operations,
            thread_runtime_authority=self.thread_runtime_authority,
            interaction_leases=self.interaction_leases,
            runtime_admin=self.runtime_admin,
            feishu_runtime_disconnect=self.feishu_runtime_disconnect,
            ports=AdapterEventBridgePorts(
                runtime_submit=lambda fn, *args: fn(*args),
                finalize_execution_card=self.finalize_execution_card,
                thread_subscribers=(
                    thread_subscribers
                    if thread_subscribers is not None
                    else lambda _thread: ()
                ),
                resident_session=(
                    resident_session
                    if resident_session is not None
                    else lambda _binding: None
                ),
            ),
        )

    def finalize_execution_card(self, sender_id: str, chat_id: str) -> bool:
        self.events.append(f"finalize:{sender_id}:{chat_id}")
        return True

    def remember_root(self, thread_id: str = "thread-1") -> None:
        self.direct_thread_targets.remember(
            ThreadSummary(thread_id, "/repo", "", "", 0, 0, "cli", "idle")
        )


class AdapterEventBridgeTest(unittest.TestCase):
    @staticmethod
    def _approval_identity(
        *,
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
    ) -> ServerRequestIdentity:
        return ServerRequestIdentity(
            request_id="approval-1",
            connection_generation=1,
            method="item/commandExecution/requestApproval",
            params={"threadId": thread_id, "turnId": turn_id},
        )

    @staticmethod
    def _compact_session(
        *,
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
    ) -> BindingSessionSnapshot:
        return mock.Mock(
            spec=BindingSessionSnapshot,
            current_thread_id=thread_id,
            execution=SimpleNamespace(
                current_turn_id=turn_id,
                current_execution_kind="compact",
                awaiting_local_turn_started=False,
                settlement_fence="",
            ),
        )

    def test_main_turn_started_binds_only_a_blank_shared_lease(self) -> None:
        harness = _BridgeHarness()
        blank_lease = SimpleNamespace(turn_id="")
        harness.interaction_leases.load.return_value = blank_lease

        harness.bridge.reconcile_active_turn_lease_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "actual-turn-1"}},
        )

        harness.interaction_leases.load.assert_called_once_with("thread-1")
        harness.interaction_leases.activate_turn.assert_called_once_with(
            blank_lease,
            "actual-turn-1",
        )

        harness.interaction_leases.reset_mock()
        harness.interaction_leases.load.return_value = SimpleNamespace(
            turn_id="other-active-turn"
        )

        harness.bridge.reconcile_active_turn_lease_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "actual-turn-1"}},
        )

        harness.interaction_leases.activate_turn.assert_not_called()

    def test_main_turn_completion_releases_only_the_notified_exact_identity(
        self,
    ) -> None:
        harness = _BridgeHarness()

        harness.bridge.reconcile_active_turn_lease_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "actual-turn-1"}},
        )

        harness.interaction_leases.release_turn.assert_called_once_with(
            "thread-1",
            "actual-turn-1",
        )
        harness.interaction_leases.load.assert_not_called()
        harness.interaction_leases.activate_turn.assert_not_called()

    def test_compact_start_reads_exact_p2p_subscriber_when_group_shares_chat(
        self,
    ) -> None:
        group_binding = ("__group__", "chat-1")
        p2p_binding = ("ou-user", "chat-1")
        group_session = self._compact_session()
        p2p_session = self._compact_session()
        sessions = {
            group_binding: group_session,
            p2p_binding: p2p_session,
        }
        resident_reads: list[tuple[str, str]] = []

        def resident_session(
            binding: tuple[str, str],
        ) -> BindingSessionSnapshot | None:
            resident_reads.append(binding)
            return sessions.get(binding)

        harness = _BridgeHarness(
            thread_subscribers=lambda _thread: (p2p_binding,),
            resident_session=resident_session,
        )
        harness.feishu_root_operations.acknowledge_async_start.return_value = True

        harness.bridge.handle_feishu_root_operation_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        self.assertEqual(resident_reads, [p2p_binding])
        harness.feishu_root_operations.acknowledge_async_start.assert_called_once_with(
            p2p_binding,
            "thread-1",
            "turn-1",
        )

    def test_compact_start_missing_exact_resident_has_no_binding_side_effect(
        self,
    ) -> None:
        p2p_binding = ("ou-user", "chat-1")
        resident_session = mock.Mock(return_value=None)
        harness = _BridgeHarness(
            thread_subscribers=lambda _thread: (p2p_binding,),
            resident_session=resident_session,
        )

        harness.bridge.handle_feishu_root_operation_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        resident_session.assert_called_once_with(p2p_binding)
        harness.feishu_root_operations.acknowledge_async_start.assert_not_called()
        harness.feishu_root_operations.reconcile_notification.assert_called_once_with(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

    def test_connection_admission_precedes_request_and_notification_dispatch(
        self,
    ) -> None:
        harness = _BridgeHarness()
        harness.ingress_gate.accept.side_effect = [False, False, True, True]
        params = {"threadId": "thread-1"}

        harness.bridge.handle_request_for_connection(
            1,
            "blocked-request",
            "item/tool/requestUserInput",
            params,
        )
        harness.bridge.handle_notification_for_connection(
            1,
            "turn/started",
            params,
        )
        harness.bridge.handle_request_for_connection(
            2,
            "accepted-request",
            "item/tool/requestUserInput",
            params,
        )
        harness.bridge.handle_notification_for_connection(
            2,
            "turn/started",
            params,
        )

        harness.server_requests.route_request.assert_called_once_with(
            2,
            "accepted-request",
            "item/tool/requestUserInput",
            params,
        )
        harness.notification_pipeline.dispatch.assert_called_once_with(
            "turn/started",
            params,
        )

    def test_backend_reset_ingress_fence_prevents_old_turn_start_aba(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ingress_gate = AdapterIngressGate(
                invalidate_previous_epoch=lambda: None,
                activate_connection_epoch=lambda _generation: None,
            )
            interaction_leases = InteractionLeaseStore(pathlib.Path(temp_dir))
            harness = _BridgeHarness(
                ingress_gate=ingress_gate,
                interaction_leases=interaction_leases,
            )
            harness.notification_pipeline.dispatch.side_effect = (
                harness.bridge.reconcile_active_turn_lease_notification
            )

            self.assertTrue(ingress_gate.accept(1))
            old = interaction_leases.acquire(
                "thread-1",
                make_web_interaction_holder("old-client", owner_pid=os.getpid()),
            ).lease
            self.assertIsNotNone(old)

            ingress_gate.fence_backend_reset()
            ingress_gate.begin_backend_reset()
            capture = interaction_leases.capture_current_process_for_backend_stop()
            retirement = interaction_leases.retire_after_backend_stop(capture)
            self.assertEqual(retirement.retired_thread_ids, ("thread-1",))

            successor = interaction_leases.acquire(
                "thread-1",
                make_web_interaction_holder(
                    "replacement-client",
                    owner_pid=os.getpid(),
                ),
            ).lease
            self.assertIsNotNone(successor)
            self.assertNotEqual(successor, old)
            repeated_retirement = interaction_leases.retire_after_backend_stop(capture)
            self.assertEqual(
                repeated_retirement.preserved_thread_ids,
                ("thread-1",),
            )
            self.assertEqual(interaction_leases.load("thread-1"), successor)

            ingress_gate.admit_backend_replacement(
                2,
                publish_replacement=lambda: None,
            )
            harness.notification_pipeline.dispatch.reset_mock()

            harness.bridge.handle_notification_for_connection(
                1,
                "turn/started",
                {"threadId": "thread-1", "turn": {"id": "old-turn"}},
            )

            harness.notification_pipeline.dispatch.assert_not_called()
            self.assertEqual(interaction_leases.load("thread-1"), successor)

            harness.bridge.handle_notification_for_connection(
                2,
                "turn/started",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "replacement-turn"},
                },
            )

            harness.notification_pipeline.dispatch.assert_called_once_with(
                "turn/started",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "replacement-turn"},
                },
            )
            active = interaction_leases.load("thread-1")
            self.assertIsNotNone(active)
            self.assertEqual(active.lease_id, successor.lease_id)
            self.assertEqual(active.turn_id, "replacement-turn")

    def test_shared_approval_requires_whitelisted_exact_direct_root_turn(self) -> None:
        harness = _BridgeHarness()
        harness.remember_root()

        self.assertTrue(
            harness.bridge.share_server_request_approval(self._approval_identity())
        )

        self.assertFalse(
            harness.bridge.share_server_request_approval(
                self._approval_identity(thread_id="child-1")
            )
        )
        self.assertFalse(
            harness.bridge.share_server_request_approval(
                self._approval_identity(turn_id="")
            )
        )
        self.assertFalse(
            harness.bridge.share_server_request_approval(
                ServerRequestIdentity(
                    request_id="question-1",
                    connection_generation=1,
                    method="item/tool/requestUserInput",
                    params={"threadId": "thread-1", "turnId": "turn-1"},
                )
            )
        )

    def test_shared_approval_does_not_require_a_writer_lease(self) -> None:
        harness = _BridgeHarness()
        harness.remember_root()
        harness.interaction_leases.load.side_effect = AssertionError(
            "shared approval must not consult writer leases"
        )

        self.assertTrue(
            harness.bridge.share_server_request_approval(self._approval_identity())
        )
        harness.interaction_leases.load.assert_not_called()

    def test_shared_desktop_interaction_requires_supported_direct_root_turn(
        self,
    ) -> None:
        harness = _BridgeHarness()
        harness.remember_root()
        harness.interaction_leases.load.side_effect = AssertionError(
            "desktop interaction fanout must not consult writer leases"
        )

        for method in (
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
            "item/tool/call",
        ):
            with self.subTest(method=method):
                self.assertTrue(
                    harness.bridge.share_server_request_desktop_interaction(
                        ServerRequestIdentity(
                            request_id=method,
                            connection_generation=1,
                            method=method,
                            params={"threadId": "thread-1", "turnId": "turn-1"},
                        )
                    )
                )

        for method, thread_id, turn_id in (
            ("item/commandExecution/requestApproval", "thread-1", "turn-1"),
            ("item/tool/requestUserInput", "child-1", "turn-1"),
            ("item/tool/requestUserInput", "thread-1", ""),
            ("experimental/unknown", "thread-1", "turn-1"),
        ):
            with self.subTest(method=method, thread_id=thread_id, turn_id=turn_id):
                self.assertFalse(
                    harness.bridge.share_server_request_desktop_interaction(
                        ServerRequestIdentity(
                            request_id=f"{method}:{thread_id}:{turn_id}",
                            connection_generation=1,
                            method=method,
                            params={"threadId": thread_id, "turnId": turn_id},
                        )
                    )
                )

        harness.interaction_leases.load.assert_not_called()

    def test_resolved_root_reconciles_each_surface_owner_once(self) -> None:
        harness = _BridgeHarness()

        harness.bridge.reconcile_resolved_interaction_root("root-1")

        reconcile_web = (
            harness.web_runtime.reconcile_external_pending_interaction_resolved
        )
        reconcile_web.assert_called_once_with("root-1")
        harness.operation_owner.retry_authoritative_cleanups.assert_called_once_with()
        harness.feishu_root_operations.reconcile_notification.assert_called_once_with(
            "serverRequest/resolved",
            {"threadId": "root-1"},
        )

    def test_thread_started_remembers_only_direct_root_and_terminal_forgets_it(self) -> None:
        harness = _BridgeHarness()
        harness.bridge.reconcile_active_turn_lease_notification(
            "thread/started",
            {
                "thread": {
                    "id": "root-1",
                    "historyMode": "legacy",
                    "source": "cli",
                }
            },
        )
        self.assertTrue(harness.direct_thread_targets.is_known("root-1"))

        harness.bridge.reconcile_active_turn_lease_notification(
            "thread/started",
            {
                "thread": {
                    "id": "child-1",
                    "historyMode": "legacy",
                    "source": {"subAgent": {"thread_spawn": {}}},
                    "parentThreadId": "root-1",
                }
            },
        )
        self.assertFalse(harness.direct_thread_targets.is_known("child-1"))

        harness.bridge.reconcile_active_turn_lease_notification(
            "thread/archived",
            {"threadId": "root-1"},
        )
        self.assertFalse(harness.direct_thread_targets.is_known("root-1"))

    def test_disconnect_orders_authority_cleanup_before_surface_finalize(
        self,
    ) -> None:
        harness = _BridgeHarness()
        harness.remember_root()
        binding = ("sender-1", "chat-1")
        harness.server_requests.backend_disconnected.side_effect = (
            lambda: harness.events.append("server_requests")
        )
        harness.thread_runtime_authority.invalidate_connection.side_effect = (
            lambda: harness.events.append("thread_authority")
        )
        harness.operation_owner.backend_disconnected.side_effect = (
            lambda: harness.events.append("operation_owner")
        )
        harness.web_runtime.backend_disconnected.side_effect = (
            lambda: harness.events.append("web_runtime")
        )
        harness.feishu_runtime_disconnect.prepare.side_effect = lambda: (
            harness.events.append("feishu_prepare")
            or FeishuRuntimeDisconnectReport((binding,))
        )
        fail_close = (
            harness.interaction_requests.fail_close_all_requests_without_response
        )
        fail_close.side_effect = (
            lambda **_kwargs: harness.events.append("interaction_fail_close") or 1
        )
        harness.runtime_admin.fail_close_service_attached_runtime.side_effect = (
            lambda: harness.events.append("runtime_admin")
            or {"detached_binding_ids": (), "detached_thread_ids": ()}
        )

        harness.bridge.handle_disconnect_impl()

        self.assertFalse(harness.direct_thread_targets.is_known("thread-1"))
        self.assertEqual(
            harness.events,
            [
                "server_requests",
                "thread_authority",
                "operation_owner",
                "web_runtime",
                "feishu_prepare",
                "interaction_fail_close",
                "runtime_admin",
                "finalize:sender-1:chat-1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
