from __future__ import annotations

import uuid

from bot.codex_protocol.client import CodexRpcError
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
)


class WebRuntimeSteerProjectionTests(WebRuntimeControllerHarness):
    def test_active_web_owned_prompt_steers_same_turn(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        read_count = len(self.fake.reads)

        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="more",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(self.fake.steered[0]["expected_turn_id"], "turn-1")
        self.assertEqual(
            self.fake.steered[0]["input_items"], [{"type": "text", "text": "more"}]
        )
        self.assertEqual(len(self.fake.reads), read_count)

    def test_unknown_steer_is_exact_prompt_receipt_not_generic_mutation(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        mutation_id = str(uuid.uuid4())

        def lose_steer_result(**_kwargs):
            raise TimeoutError("steer result lost")

        self.fake.steer_turn = lose_steer_result
        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="possibly accepted",
            mutation_id=mutation_id,
        )

        self.assertEqual(result["status"], "outcome_unknown")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))
        self.assertFalse(self.controller.read_thread("tab-1", "thread-1")["mutation_unknown"])
        self.assertEqual(
            self.controller.prompt_result(
                "tab-1",
                "thread-1",
                mutation_id=mutation_id,
            ),
            result,
        )

    def test_web_steer_backend_aba_settles_before_any_upstream_effect(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        prepared = self.prepare_web_prompt(
            "tab-1",
            "thread-1",
            text="more",
        )
        self.backend_connection_generation = 2

        result = self.controller.run_prepared_prompt(prepared)

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(self.fake.steered, [])
        self.assertFalse(self.operations.has_unknown_mutation("thread-1"))

    def test_duplicate_prompt_borrows_receipt_without_second_effect(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        mutation_id = str(uuid.uuid4())
        original = self.prepare_web_prompt(
            "tab-1",
            "thread-1",
            text="original",
            mutation_id=mutation_id,
        )
        duplicate = self.prepare_web_prompt(
            "tab-1",
            "thread-1",
            text="original",
            mutation_id=mutation_id,
        )

        pending = self.controller.run_prepared_prompt(duplicate)
        self.assertEqual(self.fake.steered, [])
        self.assertEqual(pending["status"], "pending")

        settled = self.controller.run_prepared_prompt(original)
        replay = self.controller.run_prepared_prompt(duplicate)

        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(replay, settled)
        self.assertEqual(len(self.fake.steered), 1)

    def test_no_active_steer_rejection_never_falls_back_to_start(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")

        def no_active(**_kwargs):
            raise CodexRpcError(
                "turn/steer",
                {"code": -32602, "message": "no active turn to steer"},
            )

        self.fake.steer_turn = no_active
        result = self.submit_web_prompt("tab-1", "thread-1", text="late steer")

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "active_turn_changed")
        self.assertEqual(len(self.fake.started), 1)
        self.assertEqual(self.fake.steered, [])

    def test_known_no_effect_steer_restores_attachment_only_to_source(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            cwd=str(self.workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )

        def mismatch(**_kwargs):
            raise CodexRpcError(
                "turn/steer",
                {
                    "code": -32602,
                    "message": "expected active turn id `turn-1` but found `turn-2`",
                },
            )

        self.fake.steer_turn = mismatch
        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="with attachment",
            attachment_ids=[upload["file_id"]],
        )

        self.assertEqual(result["status"], "known_no_effect")
        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].submitted)
        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.attachment_store.resolve_pending(
                client_id="tab-2",
                scope_key="thread:thread-1",
                attachment_ids=[upload["file_id"]],
            )

    def test_distinct_web_client_can_steer_after_materializing_without_writer_transfer(
        self,
    ):
        self.controller.client_connected("web-original")
        self.controller.client_connected("web-clone")
        self.submit_web_prompt_with_started_notification("web-original", "thread-1", text="hello")
        self.seed_web_active_turn_writer("web-original", "thread-1")
        writer_before = self.store.load("thread-1")

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.prepare_prompt(
                "web-clone",
                "thread-1",
                mutation_id=str(uuid.uuid4()),
                text="take over",
                attachment_ids=[],
                source_scope_generation=1,
                source_attachment_scope="thread:thread-1",
                source_composer_scope_id=(
                    "web-clone:generation:1:thread:thread-1"
                ),
            )

        self.assertEqual(caught.exception.code, "thread_not_materialized")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.fake.steered, [])

        self.controller.read_thread("web-clone", "thread-1")
        result = self.submit_web_prompt(
            "web-clone",
            "thread-1",
            text="contribute",
        )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(self.fake.steered[0]["expected_turn_id"], "turn-1")
        self.assertEqual(self.store.load("thread-1"), writer_before)

    def test_live_agent_delta_publishes_compact_stream_delta(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-live", "status": "inProgress", "items": []},
            },
        )
        self.controller.handle_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-live",
                "itemId": "agent-1",
                "delta": "hello",
            },
        )

        event = self.events[-1]
        self.assertEqual(event["type"], "thread_delta")
        self.assertEqual(
            event["detail"]["stream_delta"],
            {
                "turn_id": "turn-live",
                "item_id": "agent-1",
                "kind": "text",
                "delta": "hello",
            },
        )

    def test_live_delta_survives_a_later_steer_item_projection(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-live", "status": "inProgress", "items": []},
            },
        )
        self.controller.handle_notification(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-live",
                "item": {
                    "id": "user-a",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "A"}],
                },
            },
        )
        self.controller.handle_notification(
            "item/agentMessage/delta",
            {
                "threadId": "thread-1",
                "turnId": "turn-live",
                "itemId": "agent-before",
                "delta": "partial answer",
            },
        )
        self.controller.handle_notification(
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "turn-live",
                "item": {
                    "id": "user-steer",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "B / steer"}],
                },
            },
        )

        turns = self.events[-1]["detail"]["turns"]
        self.assertEqual(
            [turn["id"] for turn in turns],
            [
                "turn-live:user",
                "turn-live:assistant",
                "turn-live:user:2",
                "turn-live:assistant:2",
            ],
        )
        self.assertEqual(turns[1]["text"], "partial answer")
        self.assertEqual(turns[2]["text"], "B / steer")
        self.assertEqual(turns[3]["blocks"], [])

    def test_turn_completed_preserves_accumulated_items(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "inProgress", "items": []},
            },
        )
        for item in (
            {
                "id": "reason-1",
                "type": "reasoning",
                "summary": ["Inspecting the repository"],
                "content": [],
            },
            {
                "id": "command-1",
                "type": "commandExecution",
                "command": "pytest -q",
                "aggregatedOutput": "ok",
                "status": "completed",
            },
        ):
            self.controller.handle_notification(
                "item/completed",
                {"threadId": "thread-1", "turnId": "turn-1", "item": item},
            )
        self.controller.handle_notification(
            "turn/diff/updated",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "diff": "@@ -1 +1 @@\n-old\n+new",
            },
        )

        self.controller.handle_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"id": "agent-1", "type": "agentMessage", "text": "Done"}
                    ],
                },
            },
        )

        projected_turn = self.events[-1]["detail"]["turns"][0]
        self.assertEqual(projected_turn["text"], "Done")
        self.assertIn(
            "Inspecting the repository",
            [
                block.get("thinking")
                for block in projected_turn["blocks"]
                if block.get("kind") == "thinking"
            ],
        )
        self.assertEqual(
            [tool["name"] for tool in projected_turn["tools"]],
            ["Shell", "Turn diff"],
        )
