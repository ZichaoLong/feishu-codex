from __future__ import annotations

import os

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import ServerRequestIdentity
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
)
from bot.web_runtime.contract import WebRuntimeError
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebConnectedWriterRegressionTests(WebRuntimeControllerHarness):
    def _establish_active_writer(self) -> None:
        self.controller.read_thread("tab-1", "thread-1")
        self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="hello",
        )
        acquired = self.store.acquire(
            "thread-1",
            self.operations.turn_holder("tab-1"),
        )
        self.assertTrue(acquired.granted)
        self.deliver_main_turn_lifecycle(
            "turn/started",
            "thread-1",
            "turn-1",
        )
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "turn-1")
        self.assertTrue(
            lease and lease.holder.same_holder(self.operations.turn_holder("tab-1"))
        )

    def _release_active_turn_lease(self) -> None:
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertTrue(lease and self.store.release_if_matches(lease))

    def _present_approval(self, request_id: str) -> str:
        candidate = ServerRequestIdentity(
            request_id=request_id,
            connection_generation=self.server_request_generation,
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
        )
        claim = self.server_request_registry.register(candidate)
        self.assertIsNotNone(claim.identity)
        identity = claim.identity
        assert identity is not None
        self.assertTrue(self.controller.handle_adapter_request(identity))
        return jsonrpc_id_key(request_id)

    def test_materialized_contributor_steers_without_reacquiring_writer_lease(self):
        self._establish_active_writer()
        self._release_active_turn_lease()

        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="more",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(self.fake.steered[0]["expected_turn_id"], "turn-1")
        self.assertIsNone(self.store.load("thread-1"))

    def test_steer_turn_mismatch_does_not_take_over_and_restores_attachment(self):
        self._establish_active_writer()
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            cwd=str(self.workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        calls: list[dict] = []

        def mismatch(**kwargs):
            calls.append(dict(kwargs))
            raise CodexRpcError(
                "turn/steer",
                {
                    "code": -32602,
                    "message": ("expected active turn id `turn-1` but found `turn-2`"),
                },
            )

        self.fake.steer_turn = mismatch

        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="more",
            attachment_ids=[upload["file_id"]],
        )

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "active_turn_changed")
        self.assertEqual(len(calls), 1)
        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].submitted)

    def test_materialized_observer_interrupts_without_writer_transfer(self):
        self._establish_active_writer()
        self.controller.client_connected("tab-2")
        self.controller.read_thread("tab-2", "thread-1")
        before = self.store.load("thread-1")

        result = self.controller.interrupt(
            "tab-2",
            "thread-1",
            turn_id="turn-1",
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(self.fake.interrupted, [("thread-1", "turn-1")])
        self.assertEqual(self.store.load("thread-1"), before)
        self.assertTrue(
            before and before.holder.same_holder(self.operations.turn_holder("tab-1"))
        )

    def test_opening_thread_b_does_not_interrupt_active_thread_a(self):
        self._establish_active_writer()
        active_a = self.store.load("thread-1")
        self.assertIsNotNone(active_a)
        self.fake.extra_summaries.append(
            ThreadSummary(
                thread_id="thread-b",
                cwd=str(self.workspace),
                name="Thread B",
                preview="idle",
                created_at=2,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        )
        self.controller._runtime_interest.mark_confirmed(  # noqa: SLF001
            "thread-b",
            client_id="tab-1",
        )

        opened_b = self.controller.read_thread("tab-1", "thread-b")

        self.assertEqual(opened_b["thread"]["id"], "thread-b")
        self.assertEqual(self.fake.interrupted, [])
        self.assertEqual(self.store.load("thread-1"), active_a)

    def test_materialized_observer_interrupts_feishu_or_fcodex_origin(self):
        self.controller.client_connected("tab-2")
        self.controller.read_thread("tab-2", "thread-1")
        self.fake.status = "active"
        self.fake.turns = [{"id": "turn-1", "status": "inProgress", "items": []}]
        holders = (
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
            make_fcodex_interaction_holder(
                "fcodex:alice:incarnation-1",
                connection_id="connection-1",
                owner_pid=os.getpid(),
            ),
        )

        for holder in holders:
            with self.subTest(holder_kind=holder.kind):
                blank = self.store.force_acquire("thread-1", holder)
                before = self.store.activate_turn(blank, "turn-1")
                self.assertIsNotNone(before)

                result = self.controller.interrupt(
                    "tab-2",
                    "thread-1",
                    turn_id="turn-1",
                )

                self.assertTrue(result["accepted"])
                self.assertEqual(self.store.load("thread-1"), before)
                self.assertTrue(before and self.store.release_if_matches(before))

    def test_materialized_observer_interrupts_autonomous_turn_without_lease(self):
        self.controller.client_connected("tab-2")
        self.controller.read_thread("tab-2", "thread-1")
        self.fake.status = "active"
        self.fake.turns = [
            {"id": "autonomous-turn", "status": "inProgress", "items": []}
        ]
        self.assertIsNone(self.store.load("thread-1"))

        result = self.controller.interrupt(
            "tab-2",
            "thread-1",
            turn_id="autonomous-turn",
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(
            self.fake.interrupted,
            [("thread-1", "autonomous-turn")],
        )
        self.assertIsNone(self.store.load("thread-1"))

    def test_interrupt_rejects_unmaterialized_or_disconnected_document(self):
        self._establish_active_writer()
        self.controller.client_connected("tab-2")

        with self.assertRaises(WebRuntimeError) as unmaterialized:
            self.controller.interrupt(
                "tab-2",
                "thread-1",
                turn_id="turn-1",
            )
        self.assertEqual(
            unmaterialized.exception.code,
            "thread_not_materialized",
        )

        self.controller.read_thread("tab-2", "thread-1")
        self.controller.client_transport_disconnected("tab-2")
        with self.assertRaises(WebRuntimeError) as disconnected:
            self.controller.interrupt(
                "tab-2",
                "thread-1",
                turn_id="turn-1",
            )
        self.assertEqual(disconnected.exception.code, "web_writer_disconnected")
        self.assertEqual(self.fake.interrupted, [])

    def test_interaction_response_requires_the_exact_active_turn_lease(self):
        self._establish_active_writer()
        request_key = self._present_approval("approval-live-claim")
        self._release_active_turn_lease()

        with self.assertRaises(WebRuntimeError) as caught:
            self.respond_request("tab-1", request_key, action="approve_once")

        self.assertEqual(caught.exception.code, "not_interaction_owner")
        self.assertEqual(self.fake.responses, [])
        pending = self.interaction_inbox.snapshot(request_key)
        self.assertIsNotNone(pending)
        self.assertEqual(pending and pending.status, "pending")


if __name__ == "__main__":
    import unittest

    unittest.main()
