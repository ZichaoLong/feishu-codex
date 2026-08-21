import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.server_request_contract import ServerRequestIdentity
from bot.server_request_dispatch import ServerRequestSurfaceIdentityConflict
from bot.web_runtime.interaction_inbox import (
    WebInteractionChange,
    WebInteractionIngress,
    WebInteractionMutation,
)
from bot.web_runtime.interaction_ingress_coordinator import (
    WebInteractionIngressCoordinator,
    WebInteractionIngressPorts,
)
from bot.web_runtime.contract import (
    WebInteractionDeliveryDecision,
    WebInteractionDeliveryDisposition,
)


class WebInteractionIngressCoordinatorTests(unittest.TestCase):
    def _build(self, *, guard=None):
        inbox = Mock(name="inbox")
        runtime_interest = Mock(name="runtime_interest")
        runtime_interest.has_managed_interest.return_value = True
        runtime_interest.subscription_is_current.return_value = True
        operations = Mock(name="operations")
        operations.interaction_delivery_decision.return_value = (
            WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.CONNECTED,
                client_id="tab-1",
            )
        )
        live_recipient = Mock(
            name="shared_interaction_has_live_recipient",
            return_value=True,
        )
        publish_changes = Mock(name="publish_changes")
        coordinator = WebInteractionIngressCoordinator(
            inbox=inbox,
            ports=WebInteractionIngressPorts(
                runtime_interest=runtime_interest,
                operations=operations,
                shared_interaction_has_live_recipient=live_recipient,
                publish_changes=publish_changes,
            ),
            runtime_context_guard=guard or (lambda: None),
        )
        owners = SimpleNamespace(
            inbox=inbox,
            runtime_interest=runtime_interest,
            operations=operations,
            live_recipient=live_recipient,
        )
        return coordinator, owners, publish_changes

    @staticmethod
    def _identity(
        *,
        method="item/commandExecution/requestApproval",
        thread_id="child-1",
        turn_id="turn-1",
    ):
        return ServerRequestIdentity(
            request_id="request-1",
            connection_generation=7,
            method=method,
            params={
                "threadId": thread_id,
                "turnId": turn_id,
                "command": "pwd",
            },
        )

    @staticmethod
    def _route(owners, identity):
        owners.inbox.prepare_ingress.return_value = WebInteractionIngress(
            identity,
            "route",
        )
        owners.inbox.present.return_value = WebInteractionMutation(
            (WebInteractionChange(identity.thread_id, "created"),)
        )
        owners.inbox.fail_close.return_value = WebInteractionMutation(
            (
                WebInteractionChange(
                    identity.thread_id,
                    "automatic_response_submitted",
                ),
            )
        )

    def test_runtime_guard_fails_before_any_owner_access(self) -> None:
        guard = Mock(side_effect=RuntimeError("outside RuntimeLoop"))
        coordinator, owners, publish_changes = self._build(guard=guard)

        with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
            coordinator.handle_adapter_request(self._identity())

        guard.assert_called_once_with()
        for owner in vars(owners).values():
            self.assertEqual(owner.mock_calls, [])
        self.assertEqual(publish_changes.mock_calls, [])

    def test_connected_exact_thread_routes_to_its_writer_in_causal_order(self) -> None:
        coordinator, owners, publish_changes = self._build()
        identity = self._identity()
        timing = AutoResolutionTiming(3, 5, 100, 200)
        self._route(owners, identity)
        calls: list[str] = []
        owners.inbox.prepare_ingress.side_effect = (
            lambda _identity: calls.append("prepare")
            or WebInteractionIngress(identity, "route")
        )
        publish_changes.side_effect = lambda _changes: calls.append("publish")
        owners.runtime_interest.confirm_thread_scoped_server_request.side_effect = (
            lambda _thread_id: calls.append("interest")
        )
        owners.operations.interaction_delivery_decision.side_effect = (
            lambda _root: calls.append("decision")
            or WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.CONNECTED,
                client_id="tab-1",
            )
        )
        owners.inbox.present.side_effect = (
            lambda *_args, **_kwargs: calls.append("present")
            or WebInteractionMutation(
                (WebInteractionChange("child-1", "created"),)
            )
        )

        self.assertTrue(
            coordinator.handle_adapter_request(
                identity,
                auto_resolution_timing=timing,
            )
        )

        self.assertEqual(
            calls,
            [
                "prepare",
                "publish",
                "decision",
                "interest",
                "present",
                "publish",
            ],
        )
        owners.runtime_interest.confirm_thread_scoped_server_request.assert_called_once_with(
            "child-1"
        )
        owners.operations.interaction_delivery_decision.assert_called_once_with(
            "child-1"
        )
        owners.inbox.present.assert_called_once_with(
            unittest.mock.ANY,
            owner_thread_id="child-1",
            client_id="tab-1",
            auto_resolution_timing=timing,
        )

    def test_delivery_decisions_preserve_decline_and_fail_close_contracts(self) -> None:
        cases = (
            (
                WebInteractionDeliveryDecision(
                    WebInteractionDeliveryDisposition.DECLINED
                ),
                False,
                None,
            ),
            (
                WebInteractionDeliveryDecision(
                    WebInteractionDeliveryDisposition.DISCONNECTED,
                    client_id="tab-1",
                ),
                True,
                "Focus Web client disconnected",
            ),
        )
        for decision, expected_handled, expected_message in cases:
            with self.subTest(disposition=decision.disposition):
                coordinator, owners, _publish_changes = self._build()
                identity = self._identity()
                self._route(owners, identity)
                owners.operations.interaction_delivery_decision.return_value = decision

                self.assertEqual(
                    coordinator.handle_adapter_request(identity),
                    expected_handled,
                )
                if expected_message is None:
                    owners.inbox.fail_close.assert_not_called()
                else:
                    self.assertEqual(
                        owners.inbox.fail_close.call_args.kwargs["message"],
                        expected_message,
                    )
                owners.inbox.present.assert_not_called()

    def test_shared_approval_is_retained_for_current_exact_web_interest(self) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = self._identity(thread_id="root-1")
        self._route(owners, identity)
        owners.operations.interaction_delivery_decision.return_value = (
            WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.DISCONNECTED,
                client_id="tab-1",
            )
        )

        handled = coordinator.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertTrue(handled)
        owners.operations.interaction_delivery_decision.assert_not_called()
        owners.runtime_interest.confirm_thread_scoped_server_request.assert_called_once_with(
            "root-1"
        )
        owners.runtime_interest.has_managed_interest.assert_called_once_with("root-1")
        owners.runtime_interest.subscription_is_current.assert_called_once_with(
            "root-1"
        )
        owners.inbox.fail_close.assert_not_called()
        owners.inbox.present.assert_called_once_with(
            unittest.mock.ANY,
            owner_thread_id="root-1",
            client_id="",
            auto_resolution_timing=None,
            delivery_scope="shared_interaction",
        )
        owners.live_recipient.assert_not_called()

    def test_shared_nonapproval_requires_live_recipient_and_preserves_timer(
        self,
    ) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = ServerRequestIdentity(
            request_id="question-1",
            connection_generation=7,
            method="item/tool/requestUserInput",
            params={
                "threadId": "root-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
                "autoResolutionMs": 1000,
            },
        )
        timing = AutoResolutionTiming(3, 6, 100, 200)
        self._route(owners, identity)

        handled = coordinator.handle_adapter_request(
            identity,
            auto_resolution_timing=timing,
            routing_mode="shared_interaction",
        )

        self.assertTrue(handled)
        owners.operations.interaction_delivery_decision.assert_not_called()
        owners.live_recipient.assert_called_once_with(
            "root-1",
            "root-1",
            "turn-1",
        )
        owners.inbox.present.assert_called_once_with(
            unittest.mock.ANY,
            owner_thread_id="root-1",
            client_id="",
            auto_resolution_timing=timing,
            delivery_scope="shared_interaction",
        )

    def test_shared_nonapproval_declines_without_live_recipient_or_fail_close(
        self,
    ) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = ServerRequestIdentity(
            request_id="question-no-browser",
            connection_generation=7,
            method="item/tool/requestUserInput",
            params={
                "threadId": "root-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
            },
        )
        self._route(owners, identity)
        owners.live_recipient.return_value = False

        handled = coordinator.handle_adapter_request(
            identity,
            routing_mode="shared_interaction",
        )

        self.assertFalse(handled)
        owners.inbox.present.assert_not_called()
        owners.inbox.fail_close.assert_not_called()
        owners.operations.interaction_delivery_decision.assert_not_called()

    def test_unsupported_shared_nonapproval_declines_without_fail_close(self) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = self._identity(
            method="item/tool/call",
            thread_id="root-1",
        )
        self._route(owners, identity)

        handled = coordinator.handle_adapter_request(
            identity,
            routing_mode="shared_interaction",
        )

        self.assertFalse(handled)
        owners.live_recipient.assert_not_called()
        owners.inbox.present.assert_not_called()
        owners.inbox.fail_close.assert_not_called()

    def test_shared_approval_declines_an_unmanaged_exact_child_request(self) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = self._identity()
        self._route(owners, identity)
        owners.runtime_interest.has_managed_interest.return_value = False

        self.assertFalse(
            coordinator.handle_adapter_request(
                identity,
                routing_mode="shared_approval",
            )
        )

        owners.operations.interaction_delivery_decision.assert_not_called()
        owners.runtime_interest.confirm_thread_scoped_server_request.assert_not_called()
        owners.inbox.present.assert_not_called()

    def test_shared_approval_declines_stale_exact_web_interest(self) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = self._identity(thread_id="root-1")
        self._route(owners, identity)
        owners.runtime_interest.subscription_is_current.return_value = False

        self.assertFalse(
            coordinator.handle_adapter_request(
                identity,
                routing_mode="shared_approval",
            )
        )

        owners.runtime_interest.confirm_thread_scoped_server_request.assert_not_called()
        owners.inbox.present.assert_not_called()

    def test_shared_approval_with_empty_turn_is_rejected_before_inbox_ingress(
        self,
    ) -> None:
        coordinator, owners, publish_changes = self._build()
        identity = self._identity(thread_id="root-1", turn_id="")

        self.assertFalse(
            coordinator.handle_adapter_request(
                identity,
                routing_mode="shared_approval",
            )
        )

        owners.inbox.prepare_ingress.assert_not_called()
        owners.runtime_interest.confirm_thread_scoped_server_request.assert_not_called()
        owners.inbox.present.assert_not_called()
        publish_changes.assert_not_called()

    def test_unsupported_request_fails_closed_before_connectivity_policy(self) -> None:
        coordinator, owners, _publish_changes = self._build()
        identity = self._identity(method="experimental/unsupported/request")
        self._route(owners, identity)
        owners.operations.interaction_delivery_decision.return_value = (
            WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.DISCONNECTED,
                client_id="tab-1",
            )
        )

        self.assertTrue(coordinator.handle_adapter_request(identity))

        self.assertTrue(owners.inbox.fail_close.call_args.kwargs["hidden"])

    def test_identity_conflict_publishes_then_blocks_surface_fallback(self) -> None:
        coordinator, owners, publish_changes = self._build()
        identity = self._identity()
        changes = (WebInteractionChange("root-1", "identity_conflict"),)
        owners.inbox.prepare_ingress.return_value = WebInteractionIngress(
            identity,
            "identity_conflict",
            changes=changes,
        )

        with self.assertRaises(ServerRequestSurfaceIdentityConflict):
            coordinator.handle_adapter_request(identity)

        publish_changes.assert_called_once_with(changes)
        owners.operations.interaction_delivery_decision.assert_not_called()

    def test_consumed_replay_never_recreates_a_card(self) -> None:
        coordinator, owners, publish_changes = self._build()
        identity = self._identity()
        changes = (WebInteractionChange("root-1", "replayed"),)
        owners.inbox.prepare_ingress.return_value = WebInteractionIngress(
            identity,
            "consumed",
            changes=changes,
        )

        self.assertTrue(coordinator.handle_adapter_request(identity))

        owners.operations.interaction_delivery_decision.assert_not_called()
        owners.inbox.present.assert_not_called()
        publish_changes.assert_called_once_with(changes)


if __name__ == "__main__":
    unittest.main()
