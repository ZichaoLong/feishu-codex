from bot.codex_protocol.client import (
    CodexRpcPreSendError,
    CodexRpcTransportError,
)
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.jsonrpc_id import jsonrpc_id_key
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
)


class WebRuntimeServerRequestAuthorityTests(WebRuntimeControllerHarness):
    def test_child_request_is_not_rebound_to_the_root_web_writer(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        identity = self._claim_server_request(
            "child-approval",
            "item/commandExecution/requestApproval",
            {
                "threadId": "child-1",
                "turnId": "child-turn",
                "command": "pwd",
            },
        )

        handled = self.controller.handle_adapter_request(identity)

        self.assertFalse(handled)
        self.assertIsNone(
            self.interaction_inbox.snapshot(jsonrpc_id_key("child-approval"))
        )

    def test_shared_approval_requires_current_exact_web_interest(self):
        self.controller.read_thread("tab-1", "thread-1")
        identity = self._claim_server_request(
            "shared-approval",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "autonomous-turn",
                "command": "pwd",
            },
        )

        handled = self.controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(jsonrpc_id_key("shared-approval"))
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.owner_thread_id, "thread-1")
        self.assertEqual(pending.thread_id, "thread-1")
        self.assertEqual(pending.delivery_scope, "shared_interaction")

    def test_shared_question_is_visible_and_auto_resolves_without_web_writer(self):
        self.controller.client_connected("tab-1")
        self.controller.read_thread("tab-1", "thread-1")
        self.assertIsNone(self.store.load("thread-1"))
        identity = self._claim_server_request(
            "autonomous-question",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "autonomous-turn",
                "questions": [
                    {"id": "q1", "question": "Continue?", "options": []}
                ],
            },
        )
        timing = AutoResolutionTiming(7, 9, 1000, 2000)

        handled = self.controller.handle_adapter_request(
            identity,
            auto_resolution_timing=timing,
            routing_mode="shared_interaction",
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(pending.delivery_scope, "shared_interaction")
        self.assertEqual(pending.client_id, "")
        self.assertEqual(
            [item["id"] for item in self.controller.read_thread(
                "tab-1", "thread-1"
            )["pending_requests"]],
            [identity.request_key],
        )
        self.assertTrue(
            self.controller.auto_resolve_request(identity.request_key, 7, 9)
        )
        self.assertEqual(
            self.fake.responses,
            [("autonomous-question", {"answers": {}}, None)],
        )

    def test_shared_question_requires_a_connected_document_then_reprojects(self):
        self.controller.client_connected("tab-1")
        self.controller.read_thread("tab-1", "thread-1")
        identity = self._claim_server_request(
            "reconnect-question",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "autonomous-turn",
                "questions": [
                    {"id": "q1", "question": "Continue?", "options": []}
                ],
            },
        )
        self.assertTrue(
            self.controller.handle_adapter_request(
                identity,
                routing_mode="shared_interaction",
            )
        )

        self.controller.client_transport_disconnected("tab-1")

        self.assertIsNotNone(self.interaction_inbox.snapshot(identity.request_key))
        self.assertEqual(
            self.controller.read_thread("tab-1", "thread-1")["pending_requests"],
            [],
        )
        self.controller.client_connected("tab-1")
        self.assertEqual(
            [item["id"] for item in self.controller.read_thread(
                "tab-1", "thread-1"
            )["pending_requests"]],
            [identity.request_key],
        )

    def test_shared_approval_declines_after_exact_subscription_is_stale(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.controller.backend_disconnected()
        identity = self._claim_server_request(
            "stale-shared-approval",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "autonomous-turn",
                "command": "pwd",
            },
        )

        handled = self.controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertFalse(handled)
        self.assertIsNone(
            self.interaction_inbox.snapshot(
                jsonrpc_id_key("stale-shared-approval")
            )
        )

    def test_web_owned_approval_is_projected_and_answered_once(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        handled = self._handle_adapter_request(
            7,
            "item/commandExecution/requestApproval",
            {"threadId": "thread-1", "turnId": "turn-1", "command": "rm file"},
            routing_mode="shared_approval",
        )
        self.assertTrue(handled)

        snapshot = self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(snapshot["pending_requests"][0]["kind"], "approval")
        self.assertNotIn(
            "_server_request_identity",
            snapshot["pending_requests"][0],
        )
        self.respond_request("tab-1", jsonrpc_id_key(7), action="approve_once")
        self.assertEqual(self.fake.responses, [(7, {"decision": "accept"}, None)])

        with self.assertRaises(WebRuntimeError) as caught:
            self.respond_request("tab-1", jsonrpc_id_key(7), action="approve_once")
        self.assertEqual(caught.exception.code, "request_processing")

        self._resolve_server_request(
            7,
            thread_id="thread-1",
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.respond_request("tab-1", jsonrpc_id_key(7), action="approve_once")
        self.assertEqual(caught.exception.code, "request_not_found")

    def test_observer_cannot_read_another_web_writers_actionable_pending_request(self):
        """Pending interaction DTOs belong only to the connected writer."""

        self.controller.client_connected("writer-tab")
        self.controller.client_connected("observer-tab")
        self.submit_web_prompt_with_started_notification("writer-tab", "thread-1", text="hello")
        self.seed_web_active_turn_writer("writer-tab", "thread-1")
        self.assertTrue(
            self._handle_adapter_request(
                "approval-1",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "",
                    "command": "rm file",
                },
            )
        )

        writer_snapshot = self.controller.read_thread("writer-tab", "thread-1")
        observer_snapshot = self.controller.read_thread("observer-tab", "thread-1")
        writer_listing = self.controller.list_threads(client_id="writer-tab")
        observer_listing = self.controller.list_threads(client_id="observer-tab")

        self.assertEqual(
            [item["id"] for item in writer_snapshot["pending_requests"]],
            [jsonrpc_id_key("approval-1")],
        )
        self.assertEqual(writer_snapshot["thread"]["pending_interaction"], "approval")
        self.assertEqual(observer_snapshot["pending_requests"], [])
        self.assertEqual(observer_snapshot["thread"]["pending_interaction"], "none")
        self.assertEqual(
            writer_listing["threads"][0]["pending_interaction"], "approval"
        )
        self.assertEqual(observer_listing["threads"][0]["pending_interaction"], "none")

        with self.assertRaises(WebRuntimeError) as observer_response:
            self.respond_request(
                "observer-tab",
                jsonrpc_id_key("approval-1"),
                action="approve_once",
            )
        self.assertEqual(observer_response.exception.code, "request_not_owned")

        # Visibility is client-scoped only. Pending delivery state does not
        # extend the upstream main turn's ownership lifetime.
        self.assertTrue(self.controller._has_pending_for_thread("thread-1"))
        self.assertTrue(self.store.release_turn("thread-1", "turn-1"))
        self.assertIsNone(self.store.load("thread-1"))

    def test_external_pending_interaction_does_not_extend_main_turn_owner(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self.external_pending_roots.add("thread-1")

        self.assertTrue(self.store.release_turn("thread-1", "turn-1"))
        self.assertIsNone(self.store.load("thread-1"))
        self.external_pending_roots.clear()
        self.assertIsNone(self.store.load("thread-1"))

    def test_numeric_and_string_server_request_ids_have_distinct_web_tokens_and_resolve_independently(
        self,
    ):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        params = {"threadId": "thread-1", "turnId": "turn-1", "command": "pwd"}

        self.assertTrue(
            self._handle_adapter_request(
                1,
                "item/commandExecution/requestApproval",
                params,
                routing_mode="shared_approval",
            )
        )
        self.assertTrue(
            self._handle_adapter_request(
                "1",
                "item/commandExecution/requestApproval",
                params,
                routing_mode="shared_approval",
            )
        )
        numeric_key = jsonrpc_id_key(1)
        textual_key = jsonrpc_id_key("1")
        snapshot = self.controller.read_thread("tab-1", "thread-1")
        self.assertEqual(
            {pending["id"] for pending in snapshot["pending_requests"]},
            {numeric_key, textual_key},
        )

        # The HTTP route receives the projected opaque token, not a coerced
        # JSON number/string.  A raw `"1"` cannot select either request.
        with self.assertRaises(WebRuntimeError) as raw_token:
            self.respond_request("tab-1", "1", action="approve_once")
        self.assertEqual(raw_token.exception.code, "request_not_found")

        self.respond_request("tab-1", numeric_key, action="approve_once")
        self.assertEqual(self.fake.responses, [(1, {"decision": "accept"}, None)])
        self._resolve_server_request(
            1,
            thread_id="thread-1",
        )
        remaining = self.controller.read_thread("tab-1", "thread-1")["pending_requests"]
        self.assertEqual([pending["id"] for pending in remaining], [textual_key])

        self.respond_request("tab-1", textual_key, action="approve_once")
        self.assertEqual(
            self.fake.responses,
            [(1, {"decision": "accept"}, None), ("1", {"decision": "accept"}, None)],
        )

    def test_invalid_interaction_action_remains_editable_and_retryable(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        request_key = jsonrpc_id_key("approval-invalid-action")
        self._handle_adapter_request(
            "approval-invalid-action",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
            routing_mode="shared_approval",
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.respond_request(
                "tab-1",
                request_key,
                action="not-a-decision",
            )

        self.assertEqual(caught.exception.code, "invalid_action")
        self.assertEqual(
            self.interaction_inbox.snapshot(request_key).status,
            "pending",
        )
        self.assertEqual(self.fake.responses, [])
        self.respond_request(
            "tab-1",
            request_key,
            action="approve_once",
        )
        self.assertEqual(
            self.fake.responses,
            [("approval-invalid-action", {"decision": "accept"}, None)],
        )

    def test_transport_unknown_interaction_response_cannot_be_retried(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "approval-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
            routing_mode="shared_approval",
        )
        self.fake.respond_error = CodexRpcTransportError(
            "serverRequest/response",
            {"code": -32000, "message": "connection lost"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.respond_request(
                "tab-1", jsonrpc_id_key("approval-1"), action="approve_once"
            )
        self.assertEqual(caught.exception.code, "request_response_unknown")

        with self.assertRaises(WebRuntimeError) as retry:
            self.respond_request(
                "tab-1", jsonrpc_id_key("approval-1"), action="approve_once"
            )
        self.assertEqual(retry.exception.code, "request_response_unknown")

        self.fake.respond_error = None
        self.controller.client_transport_disconnected("tab-1")
        self.assertEqual(self.fake.responses, [])
        self.assertIsNone(self.store.load("thread-1"))

    def test_web_user_input_auto_resolution_submits_empty_answers(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        handled = self._handle_adapter_request(
            "question-1",
            "item/tool/requestUserInput",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "questions": [
                    {
                        "id": "q1",
                        "header": "Optional",
                        "question": "Add context?",
                        "options": [],
                    }
                ],
            },
            auto_resolution_timing=AutoResolutionTiming(7, 3, 1000, 2000),
            routing_mode="shared_interaction",
        )

        self.assertTrue(handled)
        self.assertTrue(
            self.controller.auto_resolve_request(jsonrpc_id_key("question-1"), 7, 3)
        )
        self.assertEqual(
            self.fake.responses,
            [("question-1", {"answers": {}}, None)],
        )
        pending = self.controller.read_thread(
            "tab-1",
            "thread-1",
        )["pending_requests"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], "submitted")

    def test_disconnect_does_not_cancel_an_already_submitted_response(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self._handle_adapter_request(
            "approval-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
            routing_mode="shared_approval",
        )
        self.respond_request(
            "tab-1", jsonrpc_id_key("approval-1"), action="approve_once"
        )

        self.controller.client_disconnected("tab-1")

        self.assertEqual(
            self.fake.responses, [("approval-1", {"decision": "accept"}, None)]
        )
        self.assertIsNone(self.store.load("thread-1"))

    def test_transport_disconnect_drops_local_request_without_responding(self):
        self.controller.client_connected("tab-1")
        self.controller.read_thread("tab-1", "thread-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self._handle_adapter_request(
            "approval-before-disconnect",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "",
                "command": "pwd",
            },
        )

        self.controller.client_transport_disconnected("tab-1")

        self.assertFalse(self.document_registry.is_connected("tab-1"))
        self.assertEqual(self.fake.responses, [])
        self.assertEqual(
            self.controller.read_thread("tab-1", "thread-1")["pending_requests"],
            [],
        )
        self.assertEqual(
            self.store.load("thread-1").holder.holder_id,
            "web:tab-1",
        )
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )
        self.assertTrue(self.controller.retains_runtime("thread-1"))

    def test_transport_disconnect_fail_closes_late_request_during_grace(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self.controller.client_transport_disconnected("tab-1")

        handled = self._handle_adapter_request(
            "approval-during-grace",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "",
                "command": "pwd",
            },
        )

        self.assertTrue(handled)
        self.assertEqual(self.fake.responses[0][0], "approval-during-grace")
        self.assertEqual(self.fake.responses[0][1], {"decision": "cancel"})
        self.assertIsNone(self.fake.responses[0][2])
        self.assertEqual(
            self.controller.read_thread("tab-1", "thread-1")["pending_requests"],
            [],
        )
        self.assertEqual(self.store.load("thread-1").holder.holder_id, "web:tab-1")

    def test_automatic_fail_close_unclassified_response_is_unknown(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self.fake.respond_error = RuntimeError("unclassified responder failure")

        handled = self._handle_adapter_request(
            "unsupported-unknown",
            "experimental/unsupported/request",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(jsonrpc_id_key("unsupported-unknown"))
        self.assertTrue(pending.hidden)
        self.assertEqual(pending.status, "unknown")
        self.assertEqual(self.events[-1]["reason"], "automatic_response_unknown")

    def test_automatic_fail_close_typed_pre_send_remains_known_not_sent(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self.fake.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )

        handled = self._handle_adapter_request(
            "unsupported-not-sent",
            "experimental/unsupported/request",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(
            jsonrpc_id_key("unsupported-not-sent")
        )
        self.assertTrue(pending.hidden)
        self.assertEqual(pending.status, "pending")
        self.assertEqual(self.events[-1]["reason"], "automatic_response_not_sent")

    def test_automatic_fail_close_success_projects_submitted_reason(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")

        handled = self._handle_adapter_request(
            "unsupported-submitted",
            "experimental/unsupported/request",
            {"threadId": "thread-1", "turnId": "turn-1"},
        )

        self.assertTrue(handled)
        pending = self.interaction_inbox.snapshot(
            jsonrpc_id_key("unsupported-submitted")
        )
        self.assertTrue(pending.hidden)
        self.assertEqual(pending.status, "submitted")
        self.assertEqual(self.events[-1]["reason"], "automatic_response_submitted")

    def test_transport_reconnect_delivers_new_request_but_not_cancelled_request(self):
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self._handle_adapter_request(
            "approval-cancelled",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "",
                "command": "pwd",
            },
        )
        self.controller.client_transport_disconnected("tab-1")
        self.controller.client_connected("tab-1")

        with self.assertRaises(WebRuntimeError) as cancelled:
            self.respond_request(
                "tab-1", jsonrpc_id_key("approval-cancelled"), action="approve_once"
            )
        self.assertEqual(cancelled.exception.code, "request_not_found")

        handled = self._handle_adapter_request(
            "approval-after-reconnect",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "",
                "command": "pwd",
            },
        )

        self.assertTrue(handled)
        pending = self.controller.read_thread("tab-1", "thread-1")["pending_requests"]
        self.assertEqual(
            [request["id"] for request in pending],
            [jsonrpc_id_key("approval-after-reconnect")],
        )
