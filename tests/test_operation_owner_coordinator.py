"""Inbox, registry, and operation-service facade composition regressions."""

from __future__ import annotations

from tests.fcodex_operation_harness import (
    FcodexOperationHarness,
    RuntimeLoopContextError,
    _server_request_identity,
    inspect,
    jsonrpc_id_key,
)


class FacadeCompositionTests(FcodexOperationHarness):

    def test_every_public_api_checks_runtime_context_before_side_effects(self) -> None:
        def reject_context() -> None:
            raise RuntimeLoopContextError("outside RuntimeLoop")

        self.coordinator._runtime_context_guard = reject_context
        for name, method in inspect.getmembers(self.coordinator, inspect.ismethod):
            if name.startswith("_"):
                continue
            required = {
                parameter.name: None
                for parameter in inspect.signature(method).parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
            }
            with self.subTest(api=name):
                with self.assertRaises(RuntimeLoopContextError):
                    method(**required)
        self.assertIsNone(self.participant_runtime.snapshot(self.participant_id))

    def test_thread_start_observation_runtime_failure_cannot_create_phantom_source(self) -> None:
        self._connect()
        created = self._admit(request_id=1, method="thread/start", thread_id="")
        self.assertTrue(created["allowed"])
        original_acquire = self.runtime_leases.acquire
        self.runtime_leases.acquire = (  # type: ignore[method-assign]
            lambda *_args: (_ for _ in ()).throw(RuntimeError("runtime unavailable"))
        )
        try:
            settled = self.coordinator.client_response(
                participant_id=self.participant_id,
                connection_id="connection-a",
                request_id=1,
                request_token=created["request_token"],
                outcome="success",
                observed_thread_id="root-created",
                observed_root_thread_id="root-created",
            )
        finally:
            self.runtime_leases.acquire = original_acquire  # type: ignore[method-assign]

        self.assertTrue(settled["known"])
        # The exact proxy response is accounted for even though the local
        # Registry source failed. Keeping this false would quarantine the
        # whole connection after a known create response.
        self.assertTrue(settled["settled"])
        self.assertTrue(settled["retained"])
        sources = self.participant_runtime.source_snapshot(
            self.participant_id,
            "root-created",
        )
        self.assertEqual(sources.pending_request_keys, ())
        self.assertEqual(sources.connection_ids, ())
        self.assertFalse(sources.holder_tracked)
        self.assertIsNone(self.runtime_leases.load("root-created"))

    def test_pending_interaction_does_not_extend_completed_main_turn(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()
        identity = _server_request_identity("pending-main-turn")
        registration = self.server_requests.register(identity)
        self.assertIs(registration.identity, identity)
        self.coordinator.service_server_request(identity)
        self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )
        self.assertTrue(self.coordinator.has_pending_interaction_for_root("root-1"))

        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-1", "status": "completed"}},
        )

        self.assertIsNone(self.interaction_leases.load("root-1"))
        self.assertTrue(self.coordinator.has_pending_interaction_for_root("root-1"))

    def test_known_exclusive_turn_rejection_releases_submission(self) -> None:
        self._connect()
        admitted = self._admit(method="review/start")
        self.assertTrue(admitted["allowed"])
        self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=admitted["request_token"],
            outcome="error",
        )

        self.assertIsNone(self.interaction_leases.load("root-1"))
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_thread_closed_clears_main_turn_lease(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()

        self.coordinator.notification("thread/closed", {"threadId": "root-1"})

        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_child_runtime_is_independent_of_completed_main_turn(
        self,
    ) -> None:
        self._connect()
        self._seed_fcodex_active_lease()
        self.participant_runtime.retain_connection_source(
            self.participant_id,
            "connection-a",
            "child-1",
        )
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))
        self.coordinator.notification("thread/closed", {"threadId": "child-1"})

        self.assertIsNone(self.runtime_leases.load("child-1"))
        self.assertFalse(
            self.participant_runtime.source_snapshot(
                self.participant_id,
                "child-1",
            ).holder_tracked
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_server_request_settlement_is_independent_of_main_turn_release(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()
        identity = _server_request_identity("approval-release")
        registration = self.server_requests.register(identity)
        self.assertIs(registration.identity, identity)
        self.coordinator.service_server_request(identity)
        delivered = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))
        self.assertTrue(self.server_requests.settle_identity(identity))

        removal = self.coordinator.remove_resolved_server_request(identity)

        self.assertEqual(delivered["action"], "deliver")
        self.assertEqual(removal.outcome, "removed")
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_server_request_resolution_does_not_recreate_completed_turn_owner(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()
        identity = _server_request_identity("approval-quarantined-terminal")
        registration = self.server_requests.register(identity)
        self.assertIs(registration.identity, identity)
        self.coordinator.service_server_request(identity)
        route = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id="approval-quarantined-terminal",
            method="item/commandExecution/requestApproval",
            params={"threadId": "root-1", "command": "ls"},
        )
        self.assertEqual(route["action"], "deliver")
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))

        request_key = jsonrpc_id_key("approval-quarantined-terminal")
        self.assertTrue(self.server_requests.settle_identity(identity))
        self.coordinator.remove_resolved_server_request(identity)
        self.assertTrue(self.server_requests.request_is_resolved(request_key))
        self.assertFalse(self.coordinator.has_pending_interaction_for_root("root-1"))
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_proxy_first_unknown_child_never_becomes_a_root_blocker(self) -> None:
        """An unknown child callback remains exact and cannot quarantine root."""

        self._connect()
        self._seed_fcodex_active_lease()

        proxy_route = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id="proxy-first-child",
            method="item/commandExecution/requestApproval",
            params={"threadId": "unproven-child", "command": "ls"},
        )
        identity = _server_request_identity(
            "proxy-first-child", "unproven-child"
        )
        registration = self.server_requests.register(identity)
        self.assertIs(registration.identity, identity)
        service_route = self.coordinator.service_server_request(identity)

        self.assertEqual(proxy_route["action"], "deferred")
        self.assertTrue(service_route["handled"])
        self.assertEqual(service_route["action"], "suppress")
        self.assertFalse(self.coordinator.has_pending_interaction_for_root("root-1"))

        self.assertTrue(self.server_requests.settle_identity(identity))
        self.coordinator.remove_resolved_server_request(identity)
        self.coordinator.retry_authoritative_cleanups()

        self.assertFalse(self.coordinator.has_pending_interaction_for_root("root-1"))
        self.assertEqual(
            self.operation_service.interaction_root_for_thread("unproven-child"),
            "",
        )
