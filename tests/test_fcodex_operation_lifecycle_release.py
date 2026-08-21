"""Fcodex active-main-turn ownership regressions."""

from __future__ import annotations

import os

from tests.fcodex_operation_harness import (
    FcodexOperationHarness,
    _service_server_request,
)
from bot.server_request_contract import ServerRequestIdentity
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)


class FcodexOperationLifecycleReleaseTests(FcodexOperationHarness):
    def test_ordinary_turn_start_tracks_without_main_turn_lease(self) -> None:
        self._connect()

        admitted = self._admit()

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])
        request = next(iter(self.operation_service._client_requests.values()))
        self.assertIsNone(request.turn_submission_lease)
        self.assertEqual(request.active_turn_id, "")
        self.assertIsNone(self.interaction_leases.load("root-1"))
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_exclusive_owner_rejects_ordinary_start_method(self) -> None:
        with self.assertRaisesRegex(ValueError, "只准入"):
            self.operation_service._main_turns.admit_exclusive_start(
                participant_id=self.participant_id,
                connection_id="connection-a",
                request_key="ordinary-must-not-enter-exclusive-owner",
                method="turn/start",
                root_thread_id="root-1",
                exact_root=True,
            )

        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_ordinary_turn_start_preserves_every_existing_lease_outcome(self) -> None:
        self._connect()
        holders = (
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
            make_fcodex_interaction_holder(
                "fcodex:bob:incarnation-2",
                connection_id="connection-b",
                owner_pid=os.getpid(),
            ),
        )
        request_id = 0
        for holder in holders:
            for turn_id in ("", "foreign-turn"):
                for outcome in ("success", "error", "unknown"):
                    request_id += 1
                    with self.subTest(
                        holder_kind=holder.kind,
                        turn_id=turn_id,
                        outcome=outcome,
                    ):
                        blank = self.interaction_leases.force_acquire(
                            "root-1",
                            holder,
                        )
                        if turn_id:
                            self.interaction_leases.activate_turn(blank, turn_id)
                        before = self.interaction_leases.load("root-1")
                        admitted = self._admit(
                            request_id=request_id,
                            method="turn/start",
                        )
                        self.assertTrue(admitted["allowed"])
                        self.assertEqual(
                            self.interaction_leases.load("root-1"),
                            before,
                        )
                        settled = self._client_response(
                            request_id=request_id,
                            outcome=outcome,
                        )
                        self.assertTrue(settled["settled"])
                        self.assertEqual(
                            self.interaction_leases.load("root-1"),
                            before,
                        )
                        assert before is not None
                        self.assertTrue(
                            self.interaction_leases.release_if_matches(before)
                        )

    def test_ordinary_turn_start_connection_loss_preserves_existing_lease(self) -> None:
        self._connect()
        before = self.interaction_leases.force_acquire(
            "root-1",
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
        )
        admitted = self._admit()
        self.assertTrue(admitted["allowed"])
        before = self.interaction_leases.load("root-1")

        disconnected = self.coordinator.participant_disconnected(
            self.participant_id,
            "connection-a",
        )
        self.assertEqual(disconnected["unknown_client_requests"], 1)
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_concurrent_ordinary_start_lifecycle_may_bind_existing_fcodex_blank(
        self,
    ) -> None:
        """Upstream exposes no effect identity for this accepted narrow race."""

        self._connect()
        exclusive = self._admit(method="thread/compact/start")
        blank = self.interaction_leases.load("root-1")
        ordinary = self._admit(request_id=2, method="turn/start")
        ordinary_settled = self._client_response(
            request_id=2,
            outcome="success",
        )

        self.assertTrue(exclusive["allowed"])
        self.assertTrue(ordinary["allowed"])
        self.assertTrue(ordinary_settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), blank)

        self.coordinator.notification(
            "turn/started",
            {"threadId": "root-1", "turn": {"id": "raced-turn"}},
        )

        active = self.interaction_leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active and active.lease_id, blank and blank.lease_id)
        self.assertEqual(active and active.turn_id, "raced-turn")

    def test_ordinary_start_response_and_lifecycle_create_no_fcodex_lease(self) -> None:
        self._connect()
        settled = self._settle_ordinary_turn_start()
        self.coordinator.notification(
            "turn/started",
            {"threadId": "root-1", "turn": {"id": "turn-actual"}},
        )
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-actual"}},
        )

        self.assertTrue(settled["settled"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_inline_review_response_still_activates_its_exact_turn(self) -> None:
        self._connect()
        admitted = self._admit(method="review/start")

        settled = self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=admitted["request_token"],
            outcome="success",
            response_result={"turn": {"id": "review-turn-1"}},
        )

        self.assertTrue(settled["settled"])
        self.assertEqual(
            self.interaction_leases.load("root-1").turn_id,
            "review-turn-1",
        )

    def test_compact_response_stays_blank_until_lifecycle_identity(self) -> None:
        self._connect()
        admitted = self._admit(method="thread/compact/start")

        settled = self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=admitted["request_token"],
            outcome="success",
            response_result={"turn": {"id": "not-a-compact-response-contract"}},
        )

        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1").turn_id, "")

        self.coordinator.notification(
            "turn/started",
            {"threadId": "root-1", "turn": {"id": "compact-turn-1"}},
        )
        self.assertEqual(
            self.interaction_leases.load("root-1").turn_id,
            "compact-turn-1",
        )

    def test_stale_completion_cannot_release_blank_aba_replacement(self) -> None:
        self._connect()
        self._admit(method="thread/compact/start")
        self._client_response(request_id=1, outcome="success")
        generation_a = self.interaction_leases.load("root-1")
        assert generation_a is not None
        self.coordinator.notification("thread/closed", {"threadId": "root-1"})

        replacement = self._admit(
            request_id=2,
            method="thread/compact/start",
        )
        generation_b = self.interaction_leases.load("root-1")
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-a"}},
        )

        self.assertTrue(replacement["allowed"])
        self.assertIsNotNone(generation_b)
        self.assertNotEqual(
            generation_b and generation_b.lease_id,
            generation_a.lease_id,
        )
        self.assertEqual(self.interaction_leases.load("root-1"), generation_b)

    def test_matching_completion_releases_without_descendant_or_pending_gate(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))
        successor = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="turn/start",
        )

        self.assertTrue(successor["allowed"])

    def test_stale_completion_cannot_release_newer_active_turn(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()

        self.coordinator.notification(
            "turn/completed",
            {"threadId": "root-1", "turn": {"id": "old-turn"}},
        )

        lease = self.interaction_leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "turn-1")

    def test_connection_loss_does_not_grace_or_orphan_active_turn(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")

        disconnected = self.coordinator.participant_disconnected(
            self.participant_id,
            "connection-a",
        )
        denied = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="turn/interrupt",
        )

        self.assertEqual(disconnected["unknown_client_requests"], 0)
        self.assertFalse(denied["allowed"])
        self.assertEqual(self.interaction_leases.load("root-1").turn_id, "turn-1")

    def test_blank_resume_lease_without_exact_active_turn_cannot_steer(self) -> None:
        self._connect()
        resumed = self._admit(
            request_id=1,
            method="thread/resume",
            resume_may_autostart=True,
        )
        self.assertTrue(resumed["allowed"])
        lease = self.interaction_leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertTrue(self.interaction_leases.release_if_matches(lease))

        denied = self._admit(
            request_id=2,
            method="turn/steer",
            request_params={
                "threadId": "root-1",
                "input": [{"type": "text", "text": "not attached"}],
                "expectedTurnId": "turn-1",
            },
        )

        self.assertFalse(denied["allowed"])
        self.assertIn("未 attach", denied["reason"])

    def test_active_turn_observer_resume_does_not_transfer_fcodex_writer(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        before = self.interaction_leases.load("root-1")

        resumed = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="thread/resume",
            resume_may_autostart=True,
        )
        settled = self._client_response(
            connection_id="connection-b",
            request_id=2,
            outcome="success",
        )
        after = self.interaction_leases.load("root-1")

        self.assertTrue(resumed["allowed"])
        self.assertTrue(settled["settled"])
        self.assertEqual(after, before)
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "root-1",
        )
        self.assertIn("connection-b", source.connection_ids)

    def test_active_web_or_feishu_turn_allows_local_observer_resume(self) -> None:
        self._connect("connection-b")
        holders = (
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
        )
        for request_id, holder in enumerate(holders, start=10):
            with self.subTest(holder_kind=holder.kind):
                blank = self.interaction_leases.force_acquire("root-1", holder)
                active = self.interaction_leases.activate_turn(
                    blank,
                    "turn-1",
                )
                self.assertIsNotNone(active)

                resumed = self._admit(
                    connection_id="connection-b",
                    request_id=request_id,
                    method="thread/resume",
                    resume_may_autostart=True,
                )
                settled = self._client_response(
                    connection_id="connection-b",
                    request_id=request_id,
                    outcome="success",
                )

                self.assertTrue(resumed["allowed"])
                self.assertTrue(settled["settled"])
                self.assertEqual(self.interaction_leases.load("root-1"), active)

    def test_known_exclusive_start_rejection_releases_exact_blank(self) -> None:
        self._connect()
        admitted = self._admit(method="review/start")

        settled = self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=admitted["request_token"],
            outcome="error",
        )

        self.assertTrue(settled["settled"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_stale_exclusive_rejection_cannot_release_aba_replacement(self) -> None:
        self._connect()
        admitted = self._admit(method="review/start")
        generation_a = self.interaction_leases.load("root-1")
        assert generation_a is not None
        self.coordinator.notification("thread/closed", {"threadId": "root-1"})
        replacement = self._admit(request_id=2, method="review/start")
        generation_b = self.interaction_leases.load("root-1")

        stale = self.coordinator.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=admitted["request_token"],
            outcome="error",
        )

        self.assertTrue(stale["settled"])
        self.assertTrue(replacement["allowed"])
        self.assertIsNotNone(generation_b)
        self.assertNotEqual(generation_a.lease_id, generation_b and generation_b.lease_id)
        self.assertEqual(self.interaction_leases.load("root-1"), generation_b)
        current = self._client_response(
            request_id=2,
            outcome="error",
        )
        self.assertTrue(current["settled"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_server_request_routes_from_active_lease_not_retained_record(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")

        service_route = _service_server_request(
            self.coordinator,
            "approval-1",
        )
        observer_route = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-b",
            request_id="approval-1",
            method="item/commandExecution/requestApproval",
            params={"threadId": "root-1", "command": "ls"},
        )
        writer_route = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id="approval-1",
            method="item/commandExecution/requestApproval",
            params={"threadId": "root-1", "command": "ls"},
        )

        self.assertTrue(service_route["handled"])
        self.assertEqual(service_route["action"], "suppress")
        self.assertEqual(observer_route["action"], "suppress")
        self.assertEqual(writer_route["action"], "deliver")

    def test_attached_fcodex_endpoints_share_exact_active_turn_approval(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        for request_id, connection_id in ((10, "connection-a"), (11, "connection-b")):
            resumed = self._admit(
                connection_id=connection_id,
                request_id=request_id,
                method="thread/resume",
            )
            self.assertTrue(resumed["allowed"])
            self.assertTrue(
                self._client_response(
                    connection_id=connection_id,
                    request_id=request_id,
                    outcome="success",
                )["settled"]
            )
        self._seed_fcodex_active_lease("connection-a")
        identity = ServerRequestIdentity(
            request_id="shared-approval",
            connection_generation=1,
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "root-1",
                "turnId": "turn-1",
                "command": "ls",
            },
        )

        service = self.coordinator.service_server_request(
            identity,
            routing_mode="shared_approval",
        )
        first = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )
        second = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-b",
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )

        self.assertTrue(service["handled"])
        self.assertEqual(first["action"], "deliver")
        self.assertEqual(second["action"], "deliver")
        self.assertNotEqual(first["response_token"], second["response_token"])
        self.assertEqual(
            self.interaction_leases.load("root-1").holder.connection_id,
            "connection-a",
        )

    def test_attached_fcodex_endpoint_receives_autonomous_no_writer_approval(
        self,
    ) -> None:
        self._connect("connection-a")
        resumed = self._admit(
            connection_id="connection-a",
            request_id=10,
            method="thread/resume",
        )
        self.assertTrue(resumed["allowed"])
        self.assertTrue(
            self._client_response(
                connection_id="connection-a",
                request_id=10,
                outcome="success",
            )["settled"]
        )
        self.assertIsNone(self.interaction_leases.load("root-1"))
        identity = ServerRequestIdentity(
            request_id="autonomous-approval",
            connection_generation=1,
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "root-1",
                "turnId": "autonomous-turn",
                "command": "pwd",
            },
        )

        service = self.coordinator.service_server_request(
            identity,
            routing_mode="shared_approval",
        )
        proxy = self.coordinator.server_request(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )
        self.assertTrue(service["handled"])
        self.assertEqual(proxy["action"], "deliver")
        self.assertTrue(proxy["response_token"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_thread_closed_clears_active_turn_owner(self) -> None:
        self._connect()
        self._seed_fcodex_active_lease()

        self.coordinator.notification("thread/closed", {"threadId": "root-1"})

        self.assertIsNone(self.interaction_leases.load("root-1"))


if __name__ == "__main__":
    import unittest

    unittest.main()
