from __future__ import annotations

import uuid

from bot.codex_protocol.client import (
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.stores.interaction_lease_store import make_fcodex_interaction_holder
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebRuntimePromptSubmissionAuthorityTests(WebRuntimeControllerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.controller.read_thread("tab-1", "thread-1")

    def test_accepted_start_creates_no_web_submission_lease(self):
        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "start")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertIsNone(self.store.load("thread-1"))

        self.deliver_main_turn_lifecycle(
            "turn/completed",
            "thread-1",
            "turn-1",
        )

        self.assertIsNone(self.store.load("thread-1"))

    def test_lifecycle_does_not_invent_prompt_writer_lease(self):
        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "succeeded")
        self.deliver_main_turn_lifecycle("turn/started", "thread-1", "turn-1")
        self.assertIsNone(self.store.load("thread-1"))

        self.deliver_main_turn_lifecycle("turn/completed", "thread-1", "turn-1")

        self.assertIsNone(self.store.load("thread-1"))

    def test_thread_terminal_needs_no_prompt_submission_cleanup(self):
        self.submit_web_prompt("tab-1", "thread-1", text="hello")
        self.assertIsNone(self.store.load("thread-1"))

        self.deliver_main_turn_lifecycle("thread/closed", "thread-1", "")

        self.assertIsNone(self.store.load("thread-1"))

    def test_pre_send_prompt_failure_is_exact_known_no_effect_not_generic_unknown(self):
        self.fake.start_error = CodexRpcPreSendError(
            "turn/start", RuntimeError("offline")
        )

        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "known_no_effect")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_unsubscribe_fence_is_known_no_effect_and_new_mutation_can_retry(self):
        self.fake.status = "active"
        self.fake.turns = []
        self.controller.read_thread("tab-1", "thread-1")
        unsubscribe = self.resume_authority.prepare_unsubscribe_thread("thread-1")

        try:
            refused = self.submit_web_prompt(
                "tab-1",
                "thread-1",
                text="keep this draft",
            )
        finally:
            self.resume_authority.abandon_prepared_unsubscribe_thread(unsubscribe)

        self.assertEqual(refused["status"], "known_no_effect")
        self.assertEqual(self.fake.started, [])
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

        retried = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="keep this draft",
        )
        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual(len(self.fake.started), 1)

    def test_transport_unknown_is_exact_receipt_and_does_not_block_new_attempt(self):
        mutation_id = str(uuid.uuid4())
        self.fake.start_error = CodexRpcTransportError(
            "turn/start",
            {"code": -32000, "message": "connection lost"},
        )

        unknown = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="hello",
            mutation_id=mutation_id,
        )

        self.assertEqual(unknown["status"], "outcome_unknown")
        self.assertEqual(
            self.controller.prompt_result(
                "tab-1",
                "thread-1",
                mutation_id=mutation_id,
            ),
            unknown,
        )
        self.assertIsNone(self.store.load("thread-1"))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

        self.fake.start_error = None
        retry = self.submit_web_prompt("tab-1", "thread-1", text="hello again")
        self.assertEqual(retry["status"], "succeeded")

    def test_nonmatching_terminal_does_not_settle_exact_unknown(self):
        mutation_id = str(uuid.uuid4())
        self.fake.start_error = CodexRpcTransportError(
            "turn/start",
            {"code": -32000, "message": "connection lost"},
        )
        unknown = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="hello",
            mutation_id=mutation_id,
        )

        self.controller.handle_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-unrelated", "status": "completed", "items": []},
            },
        )

        self.assertEqual(unknown["status"], "outcome_unknown")
        current = self.controller.prompt_result(
            "tab-1",
            "thread-1",
            mutation_id=mutation_id,
        )
        self.assertEqual(current["status"], "outcome_unknown")
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_malformed_prompt_response_is_exact_unknown(self):
        self.fake.start_error = CodexRpcProtocolError(
            "turn/start",
            "Codex turn/start response is missing turn.id",
        )

        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIsNone(self.store.load("thread-1"))
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_prompt_does_not_read_or_transfer_another_frontend_lease(self):
        existing = self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:12", owner_pid=0),
        )

        result = self.submit_web_prompt("tab-1", "thread-1", text="hello")

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.store.load("thread-1"), existing.lease)
        self.assertEqual(len(self.fake.started), 1)
