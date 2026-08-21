"""Fcodex exact-turn shared interrupt admission regressions."""

from __future__ import annotations

import os

from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)
from tests.fcodex_operation_harness import FcodexOperationHarness


class FcodexSharedInterruptTests(FcodexOperationHarness):
    @staticmethod
    def _interrupt_params(
        *,
        thread_id: str = "root-1",
        turn_id: str = "turn-1",
    ) -> dict[str, str]:
        return {"threadId": thread_id, "turnId": turn_id}

    def _attach(
        self,
        connection_id: str,
        *,
        request_id: int,
        thread_id: str = "root-1",
        participant_id: str | None = None,
        resume_may_autostart: bool = False,
    ) -> None:
        resumed = self._admit(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=request_id,
            method="thread/resume",
            thread_id=thread_id,
            resume_may_autostart=resume_may_autostart,
        )
        self.assertTrue(resumed["allowed"])
        settled = self._client_response(
            participant_id=participant_id or self.participant_id,
            connection_id=connection_id,
            request_id=request_id,
            outcome="success",
        )
        self.assertTrue(settled["settled"])

    def test_exact_writer_lease_is_attach_proof_without_runtime_source(self) -> None:
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
            method="turn/interrupt",
            request_params=self._interrupt_params(),
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])
        request = next(
            request
            for request in self.operation_service._client_requests.values()
            if request.request_token == admitted["request_token"]
        )
        self.assertEqual(request.active_turn_id, "")
        self.assertIsNone(request.turn_submission_lease)
        self.assertFalse(self.operation_service._main_turns.owns_request(request))

        settled = self._client_response(
            connection_id="connection-a",
            request_id=2,
            outcome="success",
        )
        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)
        self.assertEqual(self.owner_changes[-1], ("root-1", "fcodex_main_turn_started"))

    def test_attached_connection_source_allows_empty_current_interrupt(self) -> None:
        self._connect("connection-a")
        self._attach("connection-a", request_id=1)
        self.assertIsNone(self.interaction_leases.load("root-1"))

        admitted = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id=""),
        )
        settled = self._client_response(
            connection_id="connection-a",
            request_id=2,
            outcome="success",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])
        self.assertTrue(settled["settled"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_empty_current_interrupt_does_not_use_blank_lease_as_attach_proof(
        self,
    ) -> None:
        self._connect("connection-a")
        blank = self.interaction_leases.force_acquire(
            "root-1",
            make_fcodex_interaction_holder(
                self.participant_id,
                connection_id="connection-a",
                owner_pid=os.getpid(),
            ),
        )

        denied = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id=""),
        )

        self.assertFalse(denied["allowed"])
        self.assertIn("未 attach", denied["reason"])
        self.assertEqual(self.interaction_leases.load("root-1"), blank)

    def test_pending_resume_is_not_empty_current_interrupt_attach_proof(self) -> None:
        self._connect("connection-a")
        resume = self._admit(
            connection_id="connection-a",
            request_id=1,
            method="thread/resume",
            resume_may_autostart=True,
        )
        blank = self.interaction_leases.load("root-1")

        denied = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id=""),
        )

        self.assertTrue(resume["allowed"])
        self.assertIsNotNone(blank)
        self.assertFalse(denied["allowed"])
        self.assertIn("未 attach", denied["reason"])
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "root-1",
        )
        self.assertEqual(source.connection_ids, ())
        self.assertEqual(len(source.pending_request_keys), 1)
        self.assertEqual(self.interaction_leases.load("root-1"), blank)

    def test_running_resume_late_attached_nonwriter_interrupts_without_writer_transfer(
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
            method="turn/interrupt",
            request_params=self._interrupt_params(),
        )
        settled = self._client_response(
            connection_id="connection-b",
            request_id=3,
            outcome="error",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)
        self.assertEqual(before and before.holder.connection_id, "connection-a")

    def test_two_attached_endpoints_track_concurrent_exact_interrupts(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)
        before = self.interaction_leases.load("root-1")

        first = self._admit(
            connection_id="connection-a",
            request_id=20,
            method="turn/interrupt",
            request_params=self._interrupt_params(),
        )
        second = self._admit(
            connection_id="connection-b",
            request_id=21,
            method="turn/interrupt",
            request_params=self._interrupt_params(),
        )

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertTrue(first["tracks_response"])
        self.assertTrue(second["tracks_response"])
        self.assertNotEqual(first["request_token"], second["request_token"])
        self.assertEqual(len(self.operation_service._client_requests), 2)
        self.assertEqual(self.interaction_leases.load("root-1"), before)

        first_settled = self._client_response(
            connection_id="connection-a",
            request_id=20,
            outcome="success",
        )
        second_settled = self._client_response(
            connection_id="connection-b",
            request_id=21,
            outcome="success",
        )
        self.assertTrue(first_settled["settled"])
        self.assertTrue(second_settled["settled"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_attached_endpoint_can_interrupt_web_or_feishu_origin_turn(self) -> None:
        self._connect("connection-b")
        holders = (
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
        )
        for offset, holder in enumerate(holders, start=10):
            with self.subTest(holder_kind=holder.kind):
                blank = self.interaction_leases.force_acquire("root-1", holder)
                active = self.interaction_leases.activate_turn(blank, "turn-1")
                self.assertIsNotNone(active)
                self._attach("connection-b", request_id=offset)

                admitted = self._admit(
                    connection_id="connection-b",
                    request_id=offset + 10,
                    method="turn/interrupt",
                    request_params=self._interrupt_params(),
                )
                settled = self._client_response(
                    connection_id="connection-b",
                    request_id=offset + 10,
                    outcome="success",
                )

                self.assertTrue(admitted["allowed"])
                self.assertTrue(settled["settled"])
                self.assertEqual(self.interaction_leases.load("root-1"), active)
                self.coordinator.notification(
                    "turn/completed",
                    {"threadId": "root-1", "turn": {"id": "turn-1"}},
                )
                self._forget_connection_runtime_source()

    def test_attached_endpoint_can_interrupt_autonomous_turn_without_writer_lease(
        self,
    ) -> None:
        self._connect("connection-a")
        self._attach("connection-a", request_id=1)
        self.assertIsNone(self.interaction_leases.load("root-1"))

        admitted = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id="autonomous-turn"),
        )
        settled = self._client_response(
            connection_id="connection-a",
            request_id=2,
            outcome="success",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(settled["settled"])
        self.assertIsNone(self.interaction_leases.load("root-1"))

    def test_other_participant_remains_attached_after_writer_disconnect(self) -> None:
        other_participant = "fcodex:bob:incarnation-2"
        self._connect("connection-a")
        self._connect(
            "connection-b",
            participant_id=other_participant,
        )
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
            method="turn/interrupt",
            request_params=self._interrupt_params(),
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
        self.assertEqual(before and before.holder.participant_id, self.participant_id)

    def test_unattached_or_disconnected_endpoint_cannot_interrupt(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")

        unattached = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(),
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
            method="turn/interrupt",
            request_params=self._interrupt_params(),
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
                method="turn/interrupt",
                request_params=self._interrupt_params(),
            )

    def test_interrupt_rejects_every_noncanonical_raw_param_shape(self) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")
        malformed: tuple[object, ...] = (
            None,
            {},
            {"threadId": "root-1"},
            {"turnId": "turn-1"},
            {"threadId": 1, "turnId": "turn-1"},
            {"threadId": "root-1", "turnId": True},
            {"threadId": "", "turnId": "turn-1"},
            {"threadId": " root-1", "turnId": "turn-1"},
            {"threadId": "root-1", "turnId": " "},
            {"threadId": "root-1", "turnId": "turn-1 "},
            {"threadId": "root-1", "turnId": "turn-1", "extra": True},
        )

        for request_id, request_params in enumerate(malformed, start=10):
            with self.subTest(request_params=request_params):
                denied = self._admit(
                    connection_id="connection-a",
                    request_id=request_id,
                    method="turn/interrupt",
                    request_params=request_params,
                )
                self.assertFalse(denied["allowed"])
                self.assertFalse(denied["tracks_response"])

        mismatched = self._admit(
            connection_id="connection-a",
            request_id=30,
            method="turn/interrupt",
            request_params=self._interrupt_params(thread_id="root-2"),
        )
        self.assertFalse(mismatched["allowed"])
        self.assertIn("direct root 不一致", mismatched["reason"])

    def test_stale_writer_lease_turn_id_is_not_attach_proof(self) -> None:
        self._connect("connection-a")
        self._seed_fcodex_active_lease("connection-a")

        denied = self._admit(
            connection_id="connection-a",
            request_id=2,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id="turn-stale"),
        )

        self.assertFalse(denied["allowed"])
        self.assertIn("未 attach", denied["reason"])
        self.assertEqual(self.interaction_leases.load("root-1").turn_id, "turn-1")

    def test_attached_stale_turn_is_tracked_and_settles_as_typed_error(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)
        before = self.interaction_leases.load("root-1")

        admitted = self._admit(
            connection_id="connection-b",
            request_id=3,
            method="turn/interrupt",
            request_params=self._interrupt_params(turn_id="turn-stale"),
        )
        request = next(
            request
            for request in self.operation_service._client_requests.values()
            if request.request_token == admitted["request_token"]
        )
        settled = self._client_response(
            connection_id="connection-b",
            request_id=3,
            outcome="error",
        )

        self.assertTrue(admitted["allowed"])
        self.assertTrue(admitted["tracks_response"])
        self.assertEqual(request.active_turn_id, "")
        self.assertTrue(settled["known"])
        self.assertTrue(settled["settled"])
        self.assertFalse(settled["outcome_unknown"])
        self.assertEqual(self.interaction_leases.load("root-1"), before)

    def test_exact_attachment_qualifies_both_interrupt_and_shared_steer(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")
        self._attach("connection-b", request_id=2)

        writer = self._admit(
            connection_id="connection-a",
            request_id=3,
            method="turn/steer",
            request_params={
                "threadId": "root-1",
                "input": [{"type": "text", "text": "writer steer"}],
                "expectedTurnId": "turn-1",
            },
        )
        nonwriter = self._admit(
            connection_id="connection-b",
            request_id=4,
            method="turn/steer",
            request_params={
                "threadId": "root-1",
                "input": [{"type": "text", "text": "observer steer"}],
                "expectedTurnId": "turn-1",
            },
        )

        self.assertTrue(writer["allowed"])
        self.assertTrue(nonwriter["allowed"])
        self.assertEqual(
            self.interaction_leases.load("root-1").holder.connection_id,
            "connection-a",
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
