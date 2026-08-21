from __future__ import annotations

import unittest

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.fcodex.interaction_inbox import (
    FcodexInteractionInbox,
    FcodexInteractionInboxPorts,
    FcodexInteractionWriter,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestResponseReport,
    ServerRequestResponseSupersededError,
)
from bot.server_request_coordinator import (
    ServerRequestCoordinator,
    ServerRequestCoordinatorPorts,
)
from bot.server_request_dispatch import ServerRequestDispatchReceipt
from bot.server_request_registry import ServerRequestRegistry
from bot.stores.interaction_lease_store import InteractionLeaseHolder


PARTICIPANT = "fcodex:alice"
CONNECTION = "connection-1"
PARTICIPANT_B = "fcodex:bob"
CONNECTION_B = "connection-2"
ROOT = "root-1"


class _Authority:
    def __init__(self) -> None:
        self.holder = InteractionLeaseHolder(
            kind="fcodex",
            holder_id=PARTICIPANT,
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
        )
        self.connected = True
        self.attached_endpoints = {(PARTICIPANT, CONNECTION)}
        self.live_recipient = True

    def interaction_root_for_thread(self, thread_id: str) -> str:
        return ROOT if thread_id in {ROOT, "child-1"} else ""

    def interaction_writer_for_root(
        self,
        root_thread_id: str,
    ) -> FcodexInteractionWriter | None:
        if root_thread_id != ROOT:
            return None
        return FcodexInteractionWriter(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            holder=self.holder,
            connected=self.connected,
        )

    def interaction_lease_holder_for_root(self, root_thread_id: str):
        return self.holder if root_thread_id == ROOT else None

    def shared_interaction_request_is_eligible(
        self,
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool:
        return (
            root_thread_id == ROOT
            and request_thread_id == ROOT
            and turn_id == "turn-1"
        )

    def shared_interaction_endpoint_is_attached(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> bool:
        return bool(
            root_thread_id == ROOT
            and (participant_id, connection_id) in self.attached_endpoints
        )

    def shared_interaction_has_live_recipient(self, root_thread_id: str) -> bool:
        return bool(root_thread_id == ROOT and self.live_recipient)


class FcodexInteractionInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = _Authority()
        self.resolved: set[str] = set()
        self.responses: list[tuple[object, dict[str, object]]] = []
        self.respond_error: Exception | None = None
        self.expiries: list[tuple[str, int, float]] = []
        self.inbox = FcodexInteractionInbox(
            ports=FcodexInteractionInboxPorts(
                authority=self.authority,
                server_request_is_resolved=lambda key: key in self.resolved,
                server_request_response_authority_is_revoked=lambda _key: False,
                respond=self._respond,
                schedule_proxy_delivery_expiry=lambda key, generation, delay: (
                    self.expiries.append((key, generation, delay))
                ),
            ),
            runtime_context_guard=lambda: None,
        )

    def _respond(self, request_id: object, **kwargs: object) -> None:
        if self.respond_error is not None:
            raise self.respond_error
        self.responses.append((request_id, kwargs))

    @staticmethod
    def _identity(request_id: str, *, command: str = "pwd") -> ServerRequestIdentity:
        return ServerRequestIdentity(
            request_id=request_id,
            connection_generation=1,
            method="item/commandExecution/requestApproval",
            params={
                "threadId": ROOT,
                "turnId": "turn-1",
                "command": command,
            },
        )

    def _proxy(
        self,
        identity: ServerRequestIdentity,
        *,
        participant_id: str = PARTICIPANT,
        connection_id: str = CONNECTION,
    ):
        return self.inbox.proxy_request(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )

    def _submit(
        self,
        identity: ServerRequestIdentity,
        response_token: str,
        *,
        participant_id: str = PARTICIPANT,
        connection_id: str = CONNECTION,
        result: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.inbox.response_submit(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=identity.request_id,
            response_token=response_token,
            result={"decision": "accept"} if result is None else result,
            error=None,
        )

    def test_service_first_waits_for_exact_proxy_delivery(self) -> None:
        identity = self._identity("service-first")

        service = self.inbox.service_request(identity)
        proxy = self._proxy(identity)

        self.assertTrue(service["handled"])
        self.assertEqual(proxy["action"], "deliver")
        self.assertTrue(proxy["response_token"])
        self.assertEqual(len(self.expiries), 1)
        self.assertEqual(self.inbox.pending_count(), 1)

    def test_shared_approval_without_fcodex_endpoint_keeps_canonical_pending(
        self,
    ) -> None:
        self.authority.connected = False
        self.authority.attached_endpoints.clear()
        identity = self._identity("shared-disconnected")

        route = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertTrue(route["handled"])
        self.assertEqual(route["action"], "suppress")
        self.assertEqual(self.inbox.pending_count(), 1)
        self.assertEqual(self.responses, [])

    def test_proxy_first_approval_requires_an_attached_endpoint(
        self,
    ) -> None:
        self.authority.connected = False
        self.authority.attached_endpoints.clear()
        identity = self._identity("proxy-first-disconnected")

        route = self._proxy(identity)

        self.assertEqual(route["action"], "suppress")
        self.assertEqual(route["reason"], "interaction_endpoint_not_attached")
        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(self.responses, [])

    def test_attached_observer_receives_proxy_first_approval_without_fcodex_writer(
        self,
    ) -> None:
        self.authority.interaction_writer_for_root = lambda _root: None
        identity = self._identity("proxy-first-no-writer")

        route = self._proxy(identity)

        self.assertEqual(route["action"], "deliver")
        self.assertTrue(route["response_token"])
        self.assertEqual(self.inbox.pending_count(), 1)
        self.assertEqual(self.responses, [])

    def test_proxy_first_token_cannot_answer_after_group_fail_close_revokes_canonical(
        self,
    ) -> None:
        registry = ServerRequestRegistry(resolved_limit=16)
        attempts: list[tuple[object, dict[str, object]]] = []
        inbox: FcodexInteractionInbox
        coordinator: ServerRequestCoordinator

        def respond(request_id: object, **kwargs: object) -> None:
            attempts.append((request_id, kwargs))
            raise CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before inactive-group cancel"),
            )

        def remove(identity: ServerRequestIdentity) -> ServerRequestLocalRemoval:
            return inbox.remove_resolved(identity)

        def dispatch(identity: ServerRequestIdentity) -> ServerRequestDispatchReceipt:
            route = inbox.service_request(identity, routing_mode="shared_approval")
            self.assertTrue(route["handled"])
            try:
                coordinator.submit_surface_response(
                    identity,
                    result={"decision": "cancel"},
                )
            except CodexRpcPreSendError:
                pass
            finally:
                self.assertTrue(
                    coordinator.revoke_surface_response_authority(identity)
                )
            return ServerRequestDispatchReceipt.committed()

        coordinator = ServerRequestCoordinator(
            registry,
            ServerRequestCoordinatorPorts(
                cancel_auto_resolution=lambda _key: None,
                remove_web_resolved=lambda identity: ServerRequestLocalRemoval(
                    "missing",
                    identity.request_key,
                    identity.thread_id,
                ),
                revoke_web_response_authority=lambda _identity: None,
                remove_fcodex_resolved=remove,
                remove_feishu_resolved=lambda identity: ServerRequestLocalRemoval(
                    "missing",
                    identity.request_key,
                    identity.thread_id,
                ),
                reconcile_resolved_root=lambda _root: None,
                invalidate_auto_resolution_epoch=lambda: None,
                shutdown_auto_resolution=lambda: None,
                dispatch_request=dispatch,
                respond=respond,
            ),
            lambda: None,
        )
        inbox = FcodexInteractionInbox(
            ports=FcodexInteractionInboxPorts(
                authority=self.authority,
                server_request_is_resolved=registry.request_is_resolved,
                server_request_response_authority_is_revoked=(
                    registry.request_response_authority_is_revoked
                ),
                respond=coordinator.submit_surface_response,
                schedule_proxy_delivery_expiry=lambda *_args: None,
            ),
            runtime_context_guard=lambda: None,
        )
        coordinator.activate_connection_epoch(1)
        observed = self._identity("proxy-first-inactive-group")
        proxy = inbox.proxy_request(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=observed.request_id,
            method=observed.method,
            params=observed.params,
        )

        routed = coordinator.route_request(
            observed.connection_generation,
            observed.request_id,
            observed.method,
            observed.params,
        )
        stale = inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=observed.request_id,
            response_token=proxy["response_token"],
            result={"decision": "accept"},
            error=None,
        )
        canonical = registry.active_identity(observed.request_key)

        self.assertEqual(proxy["action"], "deliver")
        self.assertEqual(routed.outcome, "committed")
        self.assertTrue(stale["allowed"])
        self.assertEqual(stale["response_disposition"], "superseded")
        self.assertEqual(len(attempts), 1)
        assert canonical is not None
        self.assertEqual(registry.response_phase(canonical), "pending")
        self.assertTrue(registry.response_authority_is_revoked(canonical))

    def test_shared_route_discards_precanonical_unknown_root_cancel_intent(
        self,
    ) -> None:
        identity = ServerRequestIdentity(
            request_id="late-direct-root",
            connection_generation=1,
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "late-direct-root",
                "turnId": "turn-1",
                "command": "pwd",
            },
        )
        proxy = self._proxy(identity)
        self.authority.interaction_root_for_thread = lambda thread_id: thread_id

        service = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertEqual(proxy["action"], "deferred")
        self.assertFalse(service["handled"])
        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(self.responses, [])

    def test_autonomous_no_writer_approval_still_joins_shared_domain(
        self,
    ) -> None:
        identity = self._identity("autonomous-no-writer")
        self.authority.interaction_writer_for_root = lambda _root: None
        self.authority.interaction_lease_holder_for_root = lambda _root: None
        proxy = self._proxy(identity)

        service = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertEqual(proxy["action"], "deliver")
        self.assertTrue(proxy["response_token"])
        self.assertTrue(service["handled"])
        snapshot = self.inbox.pending_snapshot(identity.request_key)
        assert snapshot is not None
        self.assertTrue(snapshot.shared_interaction)
        self.assertEqual(snapshot.state, "shared_delivered")
        self.assertEqual(self.responses, [])

        receipt = self._submit(identity, proxy["response_token"])

        self.assertEqual(receipt["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)

    def test_shared_approval_has_no_whole_request_delivery_expiry(
        self,
    ) -> None:
        identity = self._identity("shared-delivery-expiry")
        route = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertTrue(route["handled"])
        self.assertEqual(self.expiries, [])
        self.assertEqual(self.inbox.pending_count(), 1)
        self.assertEqual(self.responses, [])

    def test_single_to_shared_replay_cannot_restore_expiry_fail_close(self) -> None:
        identity = self._identity("single-to-shared-expiry")
        self.inbox.service_request(identity)
        request_key, generation, _delay = self.expiries[-1]

        replay = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )
        self.inbox.expire_proxy_delivery(request_key, generation)

        self.assertTrue(replay["handled"])
        self.assertEqual(self.inbox.pending_count(), 1)
        self.assertEqual(self.responses, [])

    def test_shared_approval_can_reach_proxy_before_delivery_expiry(self) -> None:
        identity = self._identity("shared-delivery")
        self.inbox.service_request(identity, routing_mode="shared_approval")

        route = self._proxy(identity)

        self.assertEqual(route["action"], "deliver")
        self.assertTrue(route["response_token"])
        self.assertEqual(self.inbox.pending_count(), 1)

    def test_shared_approval_issues_independent_endpoint_capabilities(self) -> None:
        identity = self._identity("two-endpoints")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_approval")

        first = self._proxy(identity)
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertEqual(first["action"], "deliver")
        self.assertEqual(second["action"], "deliver")
        self.assertNotEqual(first["response_token"], second["response_token"])

    def test_first_shared_endpoint_wins_and_other_receives_superseded(self) -> None:
        identity = self._identity("first-wins")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_approval")
        first = self._proxy(identity)
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        accepted = self._submit(identity, first["response_token"])
        superseded = self._submit(
            identity,
            second["response_token"],
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
            result={"decision": "decline"},
        )

        self.assertEqual(accepted["response_disposition"], "submitted")
        self.assertTrue(superseded["allowed"])
        self.assertEqual(superseded["response_disposition"], "superseded")
        self.assertEqual(len(self.responses), 1)

    def test_pre_send_failure_allows_another_shared_endpoint_to_retry(self) -> None:
        identity = self._identity("pre-send-retry")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_approval")
        first = self._proxy(identity)
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )
        self.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )

        not_sent = self._submit(identity, first["response_token"])
        self.respond_error = None
        retried = self._submit(
            identity,
            second["response_token"],
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertFalse(not_sent["allowed"])
        self.assertEqual(not_sent["response_disposition"], "not_sent")
        self.assertEqual(retried["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)

    def test_unknown_shared_response_fences_only_the_exact_request(self) -> None:
        identity = self._identity("unknown-shared")
        unrelated = self._identity("unrelated-shared")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_approval")
        first = self._proxy(identity)
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )
        self.respond_error = RuntimeError("possibly sent")

        unknown = self._submit(identity, first["response_token"])
        other_endpoint = self._submit(
            identity,
            second["response_token"],
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )
        self.respond_error = None
        self.inbox.service_request(unrelated, routing_mode="shared_approval")
        unrelated_proxy = self._proxy(unrelated)
        unrelated_receipt = self._submit(
            unrelated,
            unrelated_proxy["response_token"],
        )

        self.assertEqual(unknown["response_disposition"], "unknown")
        self.assertEqual(other_endpoint["response_disposition"], "unknown")
        self.assertEqual(unrelated_receipt["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)

    def test_shared_endpoint_disconnect_drops_only_its_capability(self) -> None:
        identity = self._identity("endpoint-disconnect")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_approval")
        first = self._proxy(identity)
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        dropped = self.inbox.drop_delivered(
            PARTICIPANT,
            connection_id=CONNECTION,
        )
        stale = self._submit(identity, first["response_token"])
        remaining = self._submit(
            identity,
            second["response_token"],
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertEqual(dropped, 1)
        self.assertFalse(stale["allowed"])
        self.assertEqual(remaining["response_disposition"], "submitted")

    def test_shared_response_rechecks_attached_endpoint_before_submission(self) -> None:
        identity = self._identity("endpoint-authority-revoked")
        self.inbox.service_request(identity, routing_mode="shared_approval")
        proxy = self._proxy(identity)
        self.authority.attached_endpoints.remove((PARTICIPANT, CONNECTION))

        denied = self._submit(identity, proxy["response_token"])

        self.assertFalse(denied["allowed"])
        self.assertEqual(self.responses, [])

    def test_late_attach_receives_still_pending_shared_approval(self) -> None:
        identity = self._identity("late-attach")
        self.inbox.service_request(identity, routing_mode="shared_approval")
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))

        late = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertEqual(late["action"], "deliver")
        self.assertTrue(late["response_token"])

    def test_late_response_after_resolution_receives_superseded(self) -> None:
        identity = self._identity("resolved-before-response")
        self.inbox.service_request(identity, routing_mode="shared_approval")
        proxy = self._proxy(identity)
        self.resolved.add(identity.request_key)

        removed = self.inbox.remove_resolved(identity)
        late = self._submit(identity, proxy["response_token"])

        self.assertEqual(removed.outcome, "removed")
        self.assertTrue(late["allowed"])
        self.assertEqual(late["response_disposition"], "superseded")
        self.assertEqual(self.responses, [])

    def test_resolved_capability_receipts_are_bounded_and_epoch_scoped(self) -> None:
        inbox = FcodexInteractionInbox(
            ports=FcodexInteractionInboxPorts(
                authority=self.authority,
                server_request_is_resolved=lambda key: key in self.resolved,
                server_request_response_authority_is_revoked=lambda _key: False,
                respond=self._respond,
                schedule_proxy_delivery_expiry=lambda key, generation, delay: (
                    self.expiries.append((key, generation, delay))
                ),
            ),
            runtime_context_guard=lambda: None,
            resolved_capability_limit=1,
        )
        receipts: list[tuple[ServerRequestIdentity, str]] = []
        for request_id in ("old", "new"):
            identity = self._identity(request_id)
            inbox.service_request(identity, routing_mode="shared_approval")
            proxy = inbox.proxy_request(
                participant_id=PARTICIPANT,
                connection_id=CONNECTION,
                request_id=identity.request_id,
                method=identity.method,
                params=identity.params,
            )
            self.resolved.add(identity.request_key)
            self.assertEqual(inbox.remove_resolved(identity).outcome, "removed")
            receipts.append((identity, proxy["response_token"]))

        evicted = inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=receipts[0][0].request_id,
            response_token=receipts[0][1],
            result={"decision": "accept"},
            error=None,
        )
        retained = inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=receipts[1][0].request_id,
            response_token=receipts[1][1],
            result={"decision": "accept"},
            error=None,
        )
        inbox.backend_disconnected()
        after_epoch = inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=receipts[1][0].request_id,
            response_token=receipts[1][1],
            result={"decision": "accept"},
            error=None,
        )

        self.assertFalse(evicted["allowed"])
        self.assertEqual(retained["response_disposition"], "superseded")
        self.assertFalse(after_epoch["allowed"])

    def test_proxy_first_invalid_approval_cannot_cancel_before_canonical_route(
        self,
    ) -> None:
        identity = self._identity("proxy-first-invalid")
        proxy = self._proxy(identity)

        invalid = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
        )
        service = self.inbox.service_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertEqual(invalid["action"], "suppress")
        self.assertTrue(service["handled"])
        self.assertEqual(self.responses, [])

    def test_invalid_shared_approval_retires_only_fcodex_projection(self) -> None:
        identity = self._identity("shared-invalid")
        self.inbox.service_request(identity, routing_mode="shared_approval")
        proxy = self._proxy(identity)
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))

        invalid = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
        )
        other = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertEqual(invalid["action"], "suppress")
        self.assertEqual(other["action"], "deliver")
        self.assertEqual(self.inbox.pending_count(), 1)
        self.assertEqual(self.responses, [])

    def test_invalid_single_surface_approval_still_fail_closes(self) -> None:
        identity = self._identity("single-invalid")
        self.inbox.service_request(identity)
        proxy = self._proxy(identity)

        invalid = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
        )

        self.assertEqual(invalid["action"], "fail_closed")
        self.assertEqual(len(self.responses), 1)

    def test_proxy_first_response_is_represented_until_canonical_generation_arrives(
        self,
    ) -> None:
        identity = self._identity("proxy-first")
        proxy = self._proxy(identity)

        receipt = self._submit(identity, proxy["response_token"])

        self.assertFalse(receipt["allowed"])
        self.assertEqual(receipt["response_disposition"], "not_sent")
        self.assertEqual(self.responses, [])

        service = self.inbox.service_request(identity)
        retried = self._submit(identity, proxy["response_token"])

        self.assertTrue(service["handled"])
        self.assertEqual(retried["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)
        self.assertIs(self.responses[0][0], identity)

    def test_response_token_is_single_use(self) -> None:
        identity = self._identity("one-shot")
        self.inbox.service_request(identity)
        proxy = self._proxy(identity)

        first = self.inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
            result={"decision": "accept"},
            error=None,
        )
        second = self.inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
            result={"decision": "accept"},
            error=None,
        )

        self.assertTrue(first["allowed"])
        self.assertFalse(second["allowed"])
        self.assertEqual(len(self.responses), 1)

    def test_superseded_response_returns_exact_acknowledged_disposition(self) -> None:
        identity = self._identity("superseded")
        self.inbox.service_request(identity)
        proxy = self._proxy(identity)
        self.respond_error = ServerRequestResponseSupersededError(
            ServerRequestResponseReport(
                "superseded",
                request_key=identity.request_key,
                thread_id=identity.thread_id,
            )
        )

        receipt = self.inbox.response_submit(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=proxy["response_token"],
            result={"decision": "accept"},
            error=None,
        )

        self.assertTrue(receipt["allowed"])
        self.assertEqual(receipt["response_disposition"], "superseded")
        self.assertEqual(self.responses, [])

    def test_automatic_fail_close_retries_only_after_exact_upstream_replay(self) -> None:
        identity = self._identity("automatic-retry")
        self.authority.connected = False
        self.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )

        first = self.inbox.service_request(identity)
        self.respond_error = None
        replay = self.inbox.service_request(identity)

        self.assertTrue(first["handled"])
        self.assertEqual(replay["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)
        self.assertIs(self.responses[0][0], identity)

    def test_automatic_fail_close_can_retry_on_same_endpoint_proxy_replay(self) -> None:
        identity = self._identity("automatic-proxy-retry")
        self.authority.connected = False
        self.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )
        self.inbox.service_request(identity)
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))

        other_endpoint = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )
        second_not_sent = self._proxy(identity)
        self.respond_error = None
        retried = self._proxy(identity)

        self.assertEqual(other_endpoint["action"], "suppress")
        self.assertEqual(second_not_sent["action"], "deferred")
        self.assertEqual(
            second_not_sent["response_disposition"],
            "not_sent",
        )
        self.assertEqual(retried["action"], "fail_closed")
        self.assertEqual(retried["response_disposition"], "submitted")
        self.assertEqual(len(self.responses), 1)

    def test_invalid_response_fail_close_pre_send_retries_and_unknown_is_exact(
        self,
    ) -> None:
        retryable = self._identity("invalid-pre-send")
        self.inbox.service_request(retryable)
        retryable_proxy = self._proxy(retryable)
        self.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )

        first = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=retryable.request_id,
            response_token=retryable_proxy["response_token"],
        )
        self.respond_error = None
        replay = self._proxy(retryable)

        uncertain = self._identity("invalid-unknown")
        self.inbox.service_request(uncertain)
        uncertain_proxy = self._proxy(uncertain)
        self.respond_error = RuntimeError("possibly sent")
        unknown = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=uncertain.request_id,
            response_token=uncertain_proxy["response_token"],
        )

        self.assertEqual(first["action"], "deferred")
        self.assertEqual(first["response_disposition"], "not_sent")
        self.assertEqual(replay["action"], "fail_closed")
        self.assertEqual(unknown["action"], "suppress")
        self.assertEqual(unknown["response_disposition"], "unknown")

    def test_automatic_unknown_fail_close_does_not_block_an_unrelated_request(
        self,
    ) -> None:
        uncertain = self._identity("automatic-unknown")
        unrelated = self._identity("automatic-unrelated")
        self.authority.connected = False
        self.respond_error = RuntimeError("possibly sent")

        first = self.inbox.service_request(uncertain)
        replay = self.inbox.service_request(uncertain)
        self.respond_error = None
        other = self.inbox.service_request(unrelated)

        self.assertTrue(first["handled"])
        self.assertNotIn("response_disposition", replay)
        self.assertTrue(other["handled"])
        self.assertEqual(len(self.responses), 1)
        self.assertIs(self.responses[0][0], unrelated)

    def test_proxy_first_nonapproval_joins_shared_domain_without_writer(self) -> None:
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.authority.interaction_writer_for_root = lambda _root: None
        cases = (
            (
                "question",
                "item/tool/requestUserInput",
                {
                    "threadId": ROOT,
                    "turnId": "turn-1",
                    "questions": [{"id": "q1", "question": "Continue?"}],
                },
            ),
            (
                "mcp",
                "mcpServer/elicitation/request",
                {"threadId": ROOT, "turnId": "turn-1", "mode": "form"},
            ),
        )
        for request_id, method, params in cases:
            with self.subTest(request_id=request_id):
                route = self.inbox.proxy_request(
                    participant_id=PARTICIPANT_B,
                    connection_id=CONNECTION_B,
                    request_id=request_id,
                    method=method,
                    params=params,
                )
                self.assertEqual(route["action"], "deliver")
                self.assertTrue(route["response_token"])
                snapshot = self.inbox.pending_snapshot(
                    jsonrpc_id_key(request_id)
                )
                assert snapshot is not None
                self.assertTrue(snapshot.shared_interaction)

    def test_child_approval_does_not_join_shared_domain(self) -> None:
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))

        route = self.inbox.proxy_request(
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
            request_id="child-approval",
            method="item/commandExecution/requestApproval",
            params={"threadId": "child-1", "turnId": "turn-1", "command": "pwd"},
        )

        self.assertEqual(route["action"], "suppress")

    def test_service_shared_nonapproval_requires_a_live_recipient(self) -> None:
        identity = ServerRequestIdentity(
            request_id="question-no-recipient",
            connection_generation=1,
            method="item/tool/requestUserInput",
            params={
                "threadId": ROOT,
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
            },
        )
        self.authority.live_recipient = False

        route = self.inbox.service_request(
            identity,
            routing_mode="shared_interaction",
        )

        self.assertFalse(route["handled"])
        self.assertEqual(self.inbox.pending_count(), 0)

    def test_invalid_shared_nonapproval_retires_only_one_endpoint(self) -> None:
        identity = ServerRequestIdentity(
            request_id="question-invalid",
            connection_generation=1,
            method="item/tool/requestUserInput",
            params={
                "threadId": ROOT,
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
            },
        )
        self.authority.attached_endpoints.add((PARTICIPANT_B, CONNECTION_B))
        self.inbox.service_request(identity, routing_mode="shared_interaction")
        first = self._proxy(identity)

        invalid = self.inbox.response_invalid(
            participant_id=PARTICIPANT,
            connection_id=CONNECTION,
            request_id=identity.request_id,
            response_token=first["response_token"],
        )
        second = self._proxy(
            identity,
            participant_id=PARTICIPANT_B,
            connection_id=CONNECTION_B,
        )

        self.assertEqual(invalid["action"], "suppress")
        self.assertEqual(second["action"], "deliver")
        self.assertEqual(self.responses, [])

    def test_all_shared_approval_response_objects_pass_through_unchanged(self) -> None:
        cases = (
            (
                "command-raw",
                "item/commandExecution/requestApproval",
                {"decision": "acceptForSession"},
            ),
            (
                "file-raw",
                "item/fileChange/requestApproval",
                {"decision": "acceptForSession"},
            ),
            (
                "permissions-raw",
                "item/permissions/requestApproval",
                {
                    "permissions": {"network": {"enabled": True}},
                    "scope": "turn",
                    "strictAutoReview": True,
                },
            ),
        )
        for request_id, method, response in cases:
            with self.subTest(method=method):
                identity = ServerRequestIdentity(
                    request_id=request_id,
                    connection_generation=1,
                    method=method,
                    params={
                        "threadId": ROOT,
                        "turnId": "turn-1",
                        "permissions": {"network": {"enabled": True}},
                    },
                )
                self.inbox.service_request(identity, routing_mode="shared_approval")
                proxy = self._proxy(identity)

                receipt = self._submit(
                    identity,
                    proxy["response_token"],
                    result=response,
                )

                self.assertEqual(receipt["response_disposition"], "submitted")
                _request, kwargs = self.responses[-1]
                self.assertEqual(kwargs["result"], response)
                self.assertIsNone(kwargs["error"])

    def test_identity_conflict_is_exact_and_does_not_create_global_state(self) -> None:
        identity = self._identity("conflict")
        self.inbox.service_request(identity)

        conflict = self.inbox.service_request(
            self._identity("conflict", command="different")
        )

        self.assertFalse(conflict["handled"])
        self.assertEqual(conflict["reason"], "server_request_identity_conflict")
        self.assertEqual(self.inbox.pending_count(), 1)

    def test_resolution_removes_only_the_exact_canonical_projection(self) -> None:
        identity = self._identity("resolved")
        self.inbox.service_request(identity)
        self.resolved.add(jsonrpc_id_key(identity.request_id))
        stale = self._identity("resolved")

        self.assertEqual(self.inbox.remove_resolved(stale).outcome, "mismatch")
        self.assertEqual(self.inbox.remove_resolved(identity).outcome, "removed")
        self.assertEqual(self.inbox.pending_count(), 0)

    def test_proxy_disconnect_and_backend_disconnect_only_retire_local_state(
        self,
    ) -> None:
        first = self._identity("proxy-disconnect")
        self.inbox.service_request(first)
        self._proxy(first)

        self.assertEqual(
            self.inbox.drop_delivered(PARTICIPANT, connection_id=CONNECTION),
            1,
        )
        self.assertEqual(self.responses, [])

        second = self._identity("backend-disconnect")
        self.inbox.service_request(second)
        self.inbox.backend_disconnected()
        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(self.responses, [])


if __name__ == "__main__":
    unittest.main()
