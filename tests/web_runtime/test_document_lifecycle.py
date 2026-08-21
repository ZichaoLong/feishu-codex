import uuid

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.jsonrpc_id import jsonrpc_id_key
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
)


class WebRuntimeDocumentLifecycleTests(WebRuntimeControllerHarness):
    def setUp(self) -> None:
        super().setUp()

        def submit_with_explicit_test_writer(*args, **kwargs):
            receipt = self.submit_web_prompt(*args, **kwargs)
            if receipt["status"] == "succeeded" and receipt["mode"] == "start":
                thread_id = str(receipt["thread_id"])
                client_id = str(args[0])
                acquired = self.store.acquire(
                    thread_id,
                    self.operations.turn_holder(client_id),
                )
                self.assertTrue(acquired.granted)
                self.deliver_main_turn_lifecycle(
                    "turn/started",
                    thread_id,
                    str(receipt["turn_id"]),
                )
            return receipt

        # These tests exercise document lifecycle against an already-owned
        # active turn. Ordinary prompt dispatch no longer creates that lease,
        # so the fixture establishes the subject explicitly.
        self.submit_web_prompt_with_explicit_test_writer = (
            submit_with_explicit_test_writer
        )

    def test_document_reissue_requires_fresh_materialization_and_mutation_identity(
        self,
    ):
        self.assertEqual(
            self.controller.document_intent_generation_floor("missing"),
            0,
        )
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.document_registry.accept_intent("tab-1", 7)
        old_mutation_id = str(uuid.uuid4())
        old_prepared = self.prepare_web_prompt(
            "tab-1",
            "thread-1",
            text="prepared before F5",
            mutation_id=old_mutation_id,
        )
        writer_before = self.store.load("thread-1")
        profile_before = self.controller.meta("tab-1")["writer_profile"]
        interest_before = self.controller._runtime_interest.snapshot(  # noqa: SLF001
            "thread-1"
        )

        self.controller.client_transport_disconnected("tab-1")
        self.controller.client_document_reissued("tab-1")
        self.controller.client_connected("tab-1")
        state_after_reissue = self.document_registry.snapshot("tab-1")
        self.assertIsNotNone(state_after_reissue)
        assert state_after_reissue is not None
        self.assertTrue(state_after_reissue.connected)
        self.assertEqual(state_after_reissue.materialized_thread_id, "")
        self.assertEqual(
            self.controller.document_intent_generation_floor("tab-1"),
            7,
        )
        self.assertEqual(self.store.load("thread-1"), writer_before)
        self.assertEqual(
            self.controller.meta("tab-1")["writer_profile"],
            profile_before,
        )
        self.assertEqual(
            self.controller._runtime_interest.snapshot(  # noqa: SLF001
                "thread-1"
            ),
            interest_before,
        )
        event_count = len(self.events)

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.prepare_prompt(
                "tab-1",
                "thread-1",
                mutation_id=old_mutation_id,
                text="prepared before F5",
                attachment_ids=[],
                source_scope_generation=1,
                source_attachment_scope="thread:thread-1",
                source_composer_scope_id="tab-1:generation:1:thread:thread-1",
            )

        self.assertEqual(caught.exception.code, "thread_not_materialized")
        self.assertEqual(len(self.events), event_count)

        self.controller.read_thread("tab-1", "thread-1")
        with self.assertRaises(WebRuntimeError) as replaced:
            self.prepare_web_prompt(
                "tab-1",
                "thread-1",
                text="prepared before F5",
                mutation_id=old_mutation_id,
            )
        self.assertEqual(replaced.exception.code, "prompt_mutation_conflict")
        self.assertEqual(self.fake.steered, [])

        fresh = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="fresh after F5",
        )
        self.assertEqual(fresh["status"], "succeeded")
        self.assertEqual(fresh["mode"], "steer")
        self.assertEqual(len(self.fake.steered), 1)
        self.assertEqual(self.store.load("thread-1"), writer_before)
        self.assertTrue(self.controller.abandon_prompt(old_prepared))

    def test_document_disconnect_drops_local_request_without_responding(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "approval-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
        )
        self.fake.status = "idle"

        self.controller.client_disconnected("tab-1")

        self.assertEqual(
            self.controller.read_thread("tab-1", "thread-1")["pending_requests"],
            [],
        )
        self.assertIsNone(self.interaction_inbox.snapshot(jsonrpc_id_key("approval-1")))
        self.assertEqual(self.fake.responses, [])
        self.assertIsNotNone(self.store.load("thread-1"))

    def test_document_disconnect_does_not_release_active_turn(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "req-1",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
            },
        )
        self.fake.status = "idle"

        self.controller.client_disconnected("tab-1")

        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertEqual(self.fake.responses, [])
        self._resolve_server_request(
            "req-1",
            thread_id="thread-1",
        )
        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])
        self.assertTrue(self.store.release_turn("thread-1", "turn-1"))
        self.assertIsNone(self.store.load("thread-1"))

    def test_disconnect_retains_active_writer_until_turn_completion(self):
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")

        self.controller.client_disconnected("tab-1")

        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease.holder.holder_id, "web:tab-1")
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])

        self.fake.status = "idle"
        self.controller.handle_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}},
        )
        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertTrue(self.store.release_turn("thread-1", "turn-1"))
        self.assertIsNone(self.store.load("thread-1"))

    def test_disconnected_active_writer_auto_rejects_later_server_request(self):
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.controller.client_disconnected("tab-1")

        handled = self._handle_adapter_request(
            "req-after-disconnect",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Next",
                        "question": "Continue?",
                        "options": [],
                    }
                ],
            },
        )

        self.assertTrue(handled)
        self.assertEqual(self.fake.responses[0][0], "req-after-disconnect")
        self.assertIsNotNone(self.fake.responses[0][2])

    def test_reconnected_active_writer_can_receive_later_server_request(self):
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.controller.client_transport_disconnected("tab-1")
        self.controller.client_connected("tab-1")

        handled = self._handle_adapter_request(
            "req-after-reconnect",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Next",
                        "question": "Continue?",
                        "options": [],
                    }
                ],
            },
        )

        self.assertTrue(handled)
        self.assertEqual(self.fake.responses, [])
        snapshot = self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(
            snapshot["pending_requests"][0]["id"], jsonrpc_id_key("req-after-reconnect")
        )

    def test_late_request_fail_close_pre_send_can_replay_after_browser_reconnect(
        self,
    ):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.controller.client_transport_disconnected("tab-1")
        self.fake.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline"),
        )

        handled = self._handle_adapter_request(
            "req-after-disconnect",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "header": "Next", "question": "Continue?"}],
            },
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(
            jsonrpc_id_key("req-after-disconnect")
        )
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status, "pending")
        self.controller.client_connected("tab-1")
        self.fake.respond_error = None
        # The upstream replay reuses the canonical identity and can rebuild a
        # fresh, answerable local projection once delivery is available.
        self._handle_adapter_request(
            "req-after-disconnect",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "header": "Next", "question": "Continue?"}],
            },
        )
        self.assertEqual(
            [
                item["id"]
                for item in self.controller.read_thread("tab-1", "thread-1")[
                    "pending_requests"
                ]
            ],
            [jsonrpc_id_key("req-after-disconnect")],
        )
        self.respond_request(
            "tab-1",
            jsonrpc_id_key("req-after-disconnect"),
            action="answer",
            answers={"q1": "yes"},
        )
        self.assertEqual(self.fake.responses[-1][0], "req-after-disconnect")

    def test_hard_document_reconnect_restores_delivery_for_still_active_turn(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        # Document loss does not rewrite the exact active-turn lease. The same
        # document can receive later interactions after reconnecting.
        self.controller.client_disconnected("tab-1")
        self.controller.client_connected("tab-1")

        handled = self._handle_adapter_request(
            "req-after-orphan",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [{"id": "q1", "header": "Next", "question": "Continue?"}],
            },
        )

        self.assertTrue(handled)
        self.assertEqual(
            [
                item["id"]
                for item in self.controller.read_thread("tab-1", "thread-1")[
                    "pending_requests"
                ]
            ],
            [jsonrpc_id_key("req-after-orphan")],
        )
        self.assertEqual(self.fake.responses, [])

    def test_backend_disconnect_drops_projection_and_retains_active_turn(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "req-1",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Next",
                        "question": "Continue?",
                        "options": [],
                    }
                ],
            },
        )

        self.controller.backend_disconnected()

        self.assertEqual(self.fake.responses, [])
        self.assertIsNotNone(self.store.load("thread-1"))
        pending = self.controller.read_thread("tab-1", "thread-1")["pending_requests"]
        self.assertEqual(self.fake.resumed, ["thread-1", "thread-1"])
        self.assertEqual(pending, [])

    def test_old_turn_completion_keeps_new_turn_request_and_web_owner(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "req-new",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-2",
                "command": "pwd",
            },
        )

        self.controller.handle_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )

        self.assertEqual(
            [
                item["id"]
                for item in self.controller.read_thread("tab-1", "thread-1")[
                    "pending_requests"
                ]
            ],
            [jsonrpc_id_key("req-new")],
        )
        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertTrue(self.controller.retains_runtime("thread-1"))

    def test_external_deferred_request_keeps_web_owner_until_turn_identity_is_confirmed(
        self,
    ):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.external_pending_roots.add("thread-1")

        self.controller.handle_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )

        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertTrue(self.controller.retains_runtime("thread-1"))

    def test_turn_started_keeps_request_for_its_own_turn(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "req-new",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-2",
                "command": "pwd",
            },
        )

        self.controller.handle_notification(
            "turn/started",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-2", "status": "inProgress"},
            },
        )

        self.assertEqual(
            [
                item["id"]
                for item in self.controller.read_thread("tab-1", "thread-1")[
                    "pending_requests"
                ]
            ],
            [jsonrpc_id_key("req-new")],
        )

    def test_http_activity_does_not_reconnect_disconnected_writer(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.controller.client_disconnected("tab-1")

        self.controller.read_thread("tab-1", "thread-1")
        handled = self._handle_adapter_request(
            "req-after-http",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Next",
                        "question": "Continue?",
                        "options": [],
                    }
                ],
            },
        )

        self.assertTrue(handled)
        self.assertEqual(self.fake.responses[0][0], "req-after-http")
        self.assertIsNotNone(self.fake.responses[0][2])

    def test_disconnected_http_document_cannot_start_writer_actions(self):
        """A cookie alone is not a writer delivery path.

        Every ordinary Web mutation must prove that the same browser document
        still has a live WebSocket before it can claim a lease or call Codex.
        """

        http_only = "http-only"
        actions = {
            "update_profile": lambda: self.controller.update_profile(
                http_only,
                {"working_dir": str(self.workspace)},
            ),
            "start_thread": lambda: self.controller.start_thread(
                http_only, text="hello"
            ),
            "start_prompt": lambda: self.submit_web_prompt_with_explicit_test_writer(
                http_only,
                "thread-1",
                text="hello",
            ),
            "compact": lambda: self.controller.compact_thread(http_only, "thread-1"),
            "review": lambda: self.controller.start_review(
                http_only,
                "thread-1",
                target={"type": "uncommittedChanges"},
            ),
            "rename": lambda: self.controller.rename_thread(
                http_only,
                "thread-1",
                name="Renamed",
            ),
            "set_goal": lambda: self.controller.set_goal(
                http_only,
                "thread-1",
                objective="Ship it",
            ),
            "clear_goal": lambda: self.controller.clear_goal(http_only, "thread-1"),
            "archive": lambda: self.controller.archive_thread(http_only, "thread-1"),
            "unarchive": lambda: self.controller.unarchive_thread(
                http_only, "thread-1"
            ),
            "delete": lambda: self.controller.delete_thread(
                http_only,
                "thread-1",
                confirmation="thread-1",
            ),
        }
        for operation, invoke in actions.items():
            with self.subTest(operation=operation):
                with self.assertRaises(WebRuntimeError) as caught:
                    invoke()
                self.assertEqual(caught.exception.code, "web_writer_disconnected")

        self.assertEqual(self.fake.created, [])
        self.assertEqual(self.fake.started, [])
        self.assertEqual(self.fake.compacted, [])
        self.assertEqual(self.fake.reviews, [])
        self.assertEqual(self.fake.renamed, [])
        self.assertEqual(self.fake.goal_sets, [])
        self.assertEqual(self.fake.goal_clears, [])
        self.assertEqual(self.fake.archived, [])
        self.assertEqual(self.fake.unarchived, [])
        self.assertEqual(self.fake.deleted, [])
        self.assertIsNone(self.store.load("thread-1"))

    def test_transport_disconnected_document_cannot_steer_interrupt_or_respond(self):
        self.submit_web_prompt_with_explicit_test_writer("tab-1", "thread-1", text="hello")
        self.controller.client_transport_disconnected("tab-1")

        # Model a stale HTTP request racing after socket close.  It must not
        # become a second chance to answer an interaction after the service
        # has closed the normal delivery path.
        identity = self._claim_server_request(
            "stale-http-request",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
        )
        ingress = self.interaction_inbox.prepare_ingress(identity)
        self.interaction_inbox.present(
            ingress,
            owner_thread_id="thread-1",
            client_id="tab-1",
        )
        request_id = identity.request_key
        actions = {
            "steer": lambda: self.submit_web_prompt_with_explicit_test_writer(
                "tab-1", "thread-1", text="more"
            ),
            "interrupt": lambda: self.controller.interrupt(
                "tab-1",
                "thread-1",
                turn_id="turn-1",
            ),
            "respond": lambda: self.respond_request(
                "tab-1",
                request_id,
                action="approve_once",
            ),
        }
        for operation, invoke in actions.items():
            with self.subTest(operation=operation):
                with self.assertRaises(WebRuntimeError) as caught:
                    invoke()
                self.assertEqual(caught.exception.code, "web_writer_disconnected")

        self.assertEqual(self.fake.steered, [])
        self.assertEqual(self.fake.interrupted, [])
        self.assertEqual(self.fake.responses, [])
        self.assertEqual(self.interaction_inbox.snapshot(request_id).status, "pending")

    def test_disconnected_document_keeps_read_and_draft_attachment_paths(self):
        staged = self.controller.stage_attachment(
            "http-only",
            cwd=str(self.workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        snapshot = self.controller.read_thread("http-only", "thread-1")

        self.assertTrue(staged["file_id"])
        self.assertEqual(snapshot["thread"]["id"], "thread-1")
        self.assertFalse(any(snapshot["thread"]["action_capabilities"].values()))
        self.assertIsNone(self.store.load("thread-1"))
