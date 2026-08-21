"""Fcodex exact-turn shared steer admission regressions."""

from __future__ import annotations

import os

from bot.stores.interaction_lease_store import (
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)
from tests.fcodex_operation_harness import FcodexOperationHarness


class FcodexSharedSteerTests(FcodexOperationHarness):
    @staticmethod
    def _steer_params(
        *,
        thread_id: str = "root-1",
        turn_id: str = "turn-1",
        text: str = "consider the shared evidence",
        **extra: object,
    ) -> dict[str, object]:
        return {
            "threadId": thread_id,
            "clientUserMessageId": "client-message-1",
            "input": [{"type": "text", "text": text}],
            "expectedTurnId": turn_id,
            **extra,
        }

    def _attach(
        self,
        connection_id: str,
        *,
        request_id: int,
        participant_id: str | None = None,
        resume_may_autostart: bool = False,
    ) -> None:
        exact_participant_id = participant_id or self.participant_id
        resumed = self._admit(
            participant_id=exact_participant_id,
            connection_id=connection_id,
            request_id=request_id,
            method="thread/resume",
            resume_may_autostart=resume_may_autostart,
        )
        self.assertTrue(resumed["allowed"])
        settled = self._client_response(
            participant_id=exact_participant_id,
            connection_id=connection_id,
            request_id=request_id,
            outcome="success",
        )
        self.assertTrue(settled["settled"])

    def test_exact_initiator_lease_is_attach_proof_without_runtime_source(self) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")
        before = self.interaction_leases.load("root-1")
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "root-1",
        )
        self.assertNotIn("connection-a", source.connection_ids)

        admitted = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/steer",
            request_params=self._steer_params(),
        )

        self.assertTrue(admitted["allowed"])
        request = next(
            request
            for request in self.operation_service._client_requests.values()
            if request.request_token == admitted["request_token"]
        )
        self.assertEqual(request.active_turn_id, "")
        self.assertIsNone(request.turn_submission_lease)
        self.assertFalse(self.operation_service._main_turns.owns_request(request))
        self.assertEqual(self.interaction_leases.load("root-1"), before)

        settled = self._client_response(
            connection_id="connection-a",
            request_id=2,
            outcome="success",
        )
        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_initiator_lease_must_match_expected_turn_without_runtime_source(
        self,
    ) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")
        before = self.interaction_leases.load("root-1")

        denied = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/steer",
            request_params=self._steer_params(turn_id="turn-stale"),
        )

        self.assertFalse(denied["allowed"])
        self.assertFalse(denied["tracks_response"])
        self.assertIn("未 attach", denied["reason"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)
        self.assertEqual(self.operation_service._client_requests, {})

    def test_missing_routed_or_raw_thread_id_is_fail_closed(self) -> None:
        self._connect("connection-a")

        missing_raw = self._admit(
            connection_id="connection-a",
            request_id=1,
            method="turn/steer",
            thread_id="",
            request_params={
                "input": [{"type": "text", "text": "missing raw thread"}],
                "expectedTurnId": "turn-1",
            },
        )
        missing_route = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/steer",
            thread_id="",
            request_params=self._steer_params(),
        )

        self.assertFalse(missing_raw["allowed"])
        self.assertFalse(missing_route["allowed"])
        self.assertIn("缺少 exact root threadId", missing_raw["reason"])
        self.assertIn("缺少 exact root threadId", missing_route["reason"])
        self.assertEqual(self.operation_service._client_requests, {})

    def test_b_plus_running_resume_late_attach_can_steer_without_writer_transfer(
        self,
    ) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach(
            "connection-b",
            request_id=2,
            resume_may_autostart=True,
        )
        before = self.interaction_leases.load("root-1")

        admitted = self._admit(
            connection_id="connection-b",
            request_id=3,
            method="turn/steer",
            request_params=self._steer_params(text="late contributor"),
        )
        settled = self._client_response(
            connection_id="connection-b",
            request_id=3,
            outcome="success",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)
        self.assertEqual(before and before.holder.connection_id, "connection-a")

    def test_two_attached_endpoints_track_concurrent_exact_steers(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)
        before = self.interaction_leases.load("root-1")

        first = self._admit(
            connection_id="connection-a",
            request_id=20,
            method="turn/steer",
            request_params=self._steer_params(text="first"),
        )
        second = self._admit(
            connection_id="connection-b",
            request_id=21,
            method="turn/steer",
            request_params=self._steer_params(text="second"),
        )

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertNotEqual(first["request_token"], second["request_token"])
        self.assertEqual(len(self.operation_service._client_requests), 2)
        self.assertEqual(self.interaction_leases.load("root-1"), before)
        self.assertTrue(
            self._client_response(
                connection_id="connection-a",
                request_id=20,
                outcome="success",
            )["settled"]
        )
        self.assertTrue(
            self._client_response(
                connection_id="connection-b",
                request_id=21,
                outcome="success",
            )["settled"]
        )

    def test_attached_endpoint_can_steer_web_feishu_or_no_writer_turn(self) -> None:
        self._connect("connection-b")
        holders = (
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
            None,
        )
        for offset, holder in enumerate(holders, start=10):
            with self.subTest(holder_kind=holder.kind if holder else "autonomous"):
                turn_id = "turn-1" if holder is not None else "goal-turn"
                if holder is not None:
                    blank = self.interaction_leases.force_acquire("root-1", holder)
                    active = self.interaction_leases.activate_turn(blank, turn_id)
                    self.assertIsNotNone(active)
                self._attach("connection-b", request_id=offset)

                admitted = self._admit(
                    connection_id="connection-b",
                    request_id=offset + 10,
                    method="turn/steer",
                    request_params=self._steer_params(turn_id=turn_id),
                )
                settled = self._client_response(
                    connection_id="connection-b",
                    request_id=offset + 10,
                    outcome="success",
                )

                self.assertTrue(admitted["allowed"])
                self.assertTrue(settled["settled"])
                if holder is not None:
                    self.assertEqual(self.interaction_leases.load("root-1"), active)
                    self.coordinator.notification(
                        "turn/completed",
                        {"threadId": "root-1", "turn": {"id": turn_id}},
                    )
                self._forget_connection_runtime_source(connection_id="connection-b")

    def test_other_participant_stays_qualified_after_writer_disconnect(self) -> None:
        other_participant = "fcodex:bob:incarnation-2"
        self._connect("connection-a")
        self._connect("connection-b", participant_id=other_participant)
        self._seed_fcodex_active_lease("connection-a")
        self._attach(
            "connection-b",
            request_id=2,
            participant_id=other_participant,
        )
        before = self.interaction_leases.load("root-1")
        self.coordinator.participant_disconnected(
            self.participant_id,
            "connection-a",
        )

        admitted = self._admit(
            participant_id=other_participant,
            connection_id="connection-b",
            request_id=3,
            method="turn/steer",
            request_params=self._steer_params(),
        )
        settled = self._client_response(
            participant_id=other_participant,
            connection_id="connection-b",
            request_id=3,
            outcome="success",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_unattached_pending_or_disconnected_endpoint_cannot_steer(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")

        unattached = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="turn/steer",
            request_params=self._steer_params(),
        )
        self.assertFalse(unattached["allowed"])
        self.assertIn("未 attach", unattached["reason"])

        pending_resume = self._admit(
            connection_id="connection-b",
            request_id=3,
            method="thread/resume",
        )
        self.assertTrue(pending_resume["allowed"])
        pending_source = self._admit(
            connection_id="connection-b",
            request_id=4,
            method="turn/steer",
            request_params=self._steer_params(),
        )
        self.assertFalse(pending_source["allowed"])
        self.assertTrue(
            self._client_response(
                connection_id="connection-b",
                request_id=3,
                outcome="success",
            )["settled"]
        )
        self.coordinator.participant_disconnected(
            self.participant_id,
            "connection-b",
        )
        with self.assertRaisesRegex(RuntimeError, "控制连接未注册"):
            self._admit(
                connection_id="connection-b",
                request_id=5,
                method="turn/steer",
                request_params=self._steer_params(),
            )

    def test_steer_rejects_noncanonical_raw_identity_or_parameter_surface(self) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")
        valid_input = [{"type": "text", "text": "keep upstream schema"}]
        malformed: tuple[object, ...] = (
            None,
            {},
            {"threadId": "root-1", "input": valid_input},
            {
                "threadId": "root-1",
                "expectedTurnId": "turn-1",
            },
            {
                "threadId": 1,
                "input": valid_input,
                "expectedTurnId": "turn-1",
            },
            {
                "threadId": "root-1",
                "input": valid_input,
                "expectedTurnId": True,
            },
            self._steer_params(thread_id=" root-1"),
            self._steer_params(turn_id="turn-1 "),
            self._steer_params(extra=True),
            self._steer_params(additionalContext={"hook": {"text": "override"}}),
            self._steer_params(responsesapiClientMetadata={"source": "guest"}),
        )

        for request_id, request_params in enumerate(malformed, start=10):
            with self.subTest(request_params=request_params):
                denied = self._admit(
                    connection_id="connection-a",
                    request_id=request_id,
                    method="turn/steer",
                    request_params=request_params,
                )
                self.assertFalse(denied["allowed"])
                self.assertFalse(denied["tracks_response"])

        mismatched = self._admit(
            connection_id="connection-a",
            request_id=30,
            method="turn/steer",
            request_params=self._steer_params(thread_id="root-2"),
        )
        self.assertFalse(mismatched["allowed"])
        self.assertIn("direct root 不一致", mismatched["reason"])

    def test_null_experimental_fields_preserve_the_stable_upstream_shape(self) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")

        admitted = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/steer",
            request_params=self._steer_params(
                additionalContext=None,
                responsesapiClientMetadata=None,
            ),
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])

    def test_attached_stale_expected_turn_reaches_upstream_without_retargeting(
        self,
    ) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)
        before = self.interaction_leases.load("root-1")

        admitted = self._admit(
            connection_id="connection-b",
            request_id=3,
            method="turn/steer",
            request_params=self._steer_params(turn_id="turn-stale"),
        )
        settled = self._client_response(
            connection_id="connection-b",
            request_id=3,
            outcome="error",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])
        self.assertTrue(settled["settled"])
        self.assertFalse(settled["outcome_unknown"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_shared_steer_does_not_grant_writer_owned_mutation(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)
        before = self.interaction_leases.load("root-1")

        steer = self._admit(
            connection_id="connection-b",
            request_id=3,
            method="turn/steer",
            request_params=self._steer_params(),
        )
        settings = self._admit(
            connection_id="connection-b",
            request_id=4,
            method="thread/settings/update",
        )

        self.assertTrue(steer["allowed"])
        self.assertFalse(settings["allowed"])
        self.assertIn("另一个 frontend", settings["reason"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)


if __name__ == "__main__":
    import unittest

    unittest.main()
