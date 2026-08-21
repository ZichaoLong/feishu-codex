from __future__ import annotations

import unittest

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestRoutingMode,
)
from bot.stores.interaction_lease_store import make_fcodex_interaction_holder
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import WebRuntimeControllerHarness


class WebSharedApprovalTests(WebRuntimeControllerHarness):
    def _claim_server_request(
        self,
        request_id: int | str,
        method: str,
        params: dict,
    ) -> ServerRequestIdentity:
        candidate = ServerRequestIdentity(
            request_id=request_id,
            connection_generation=self.server_request_generation,
            method=method,
            params=params,
        )
        claim = self.server_request_registry.register(candidate)
        self.assertIn(claim.outcome, {"new", "replay"})
        self.assertIsNotNone(claim.identity)
        canonical = claim.identity
        assert canonical is not None
        self.assertTrue(self.server_request_registry.active_matches(canonical))
        return canonical

    def _handle_adapter_request(
        self,
        request_id: int | str,
        method: str,
        params: dict,
        *,
        routing_mode: ServerRequestRoutingMode,
    ) -> bool:
        identity = self._claim_server_request(request_id, method, params)
        return self.controller.handle_adapter_request(
            identity,
            routing_mode=routing_mode,
        )

    def _resolve_server_request(
        self,
        request_id: int | str,
        *,
        thread_id: str,
    ) -> None:
        key = jsonrpc_id_key(request_id)
        resolved = self.server_request_registry.settle(
            key,
            thread_id=thread_id,
        )
        self.assertIn(resolved.outcome, {"settled", "already_resolved"})
        self.controller.remove_resolved_server_request(resolved.identity)

    def test_shared_approval_eligibility_uses_live_exact_document_and_callback_facts(
        self,
    ):
        self.controller.client_connected("materialized-tab")
        self.document_registry.materialize_thread("materialized-tab", "thread-1")
        self.controller._runtime_interest.mark_confirmed("thread-1")
        self.assertTrue(
            self.controller._shared_interaction_eligible(
                "materialized-tab",
                "thread-1",
                "thread-1",
                "turn-1",
            )
        )
        self.assertFalse(
            self.controller._shared_interaction_eligible(
                "materialized-tab",
                "thread-1",
                "child-1",
                "turn-1",
            )
        )
        self.assertFalse(
            self.controller._shared_interaction_eligible(
                "materialized-tab",
                "thread-1",
                "thread-1",
                "",
            )
        )

        self.controller.client_connected("desired-tab")
        self.controller._runtime_interest.mark_confirmed(
            "thread-1",
            client_id="desired-tab",
        )
        self.assertEqual(
            self.document_registry.materialized_thread_id("desired-tab"),
            "",
        )
        self.assertTrue(
            self.controller._shared_interaction_eligible(
                "desired-tab",
                "thread-1",
                "thread-1",
                "turn-1",
            )
        )

        self.controller.client_transport_disconnected("desired-tab")
        self.assertFalse(
            self.controller._shared_interaction_eligible(
                "desired-tab",
                "thread-1",
                "thread-1",
                "turn-1",
            )
        )

        self.controller.client_connected("desired-tab")
        self.controller._runtime_interest.mark_confirmed(
            "thread-1",
            client_id="desired-tab",
        )
        self.controller._runtime_interest.mark_subscription_absent("thread-1")
        self.assertFalse(
            self.controller._shared_interaction_eligible(
                "desired-tab",
                "thread-1",
                "thread-1",
                "turn-1",
            )
        )

    def test_autonomous_no_writer_approval_is_visible_and_answerable(self):
        self.controller.client_connected("observer-tab")
        self.controller.read_thread("observer-tab", "thread-1")
        self.assertIsNone(self.store.load("thread-1"))

        self.assertTrue(
            self._handle_adapter_request(
                "autonomous-approval",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "autonomous-turn",
                    "command": "pwd",
                },
                routing_mode="shared_approval",
            )
        )
        pending = self.controller.read_thread("observer-tab", "thread-1")[
            "pending_requests"
        ]
        self.assertEqual(
            [request["id"] for request in pending],
            [jsonrpc_id_key("autonomous-approval")],
        )

        self.respond_request(
            "observer-tab",
            jsonrpc_id_key("autonomous-approval"),
            action="approve_once",
        )

        self.assertEqual(
            self.fake.responses,
            [("autonomous-approval", {"decision": "accept"}, None)],
        )
        self.assertIsNone(self.store.load("thread-1"))

    def test_shared_approval_is_one_record_for_two_attached_web_documents(self):
        self.controller.client_connected("writer-tab")
        self.controller.client_connected("observer-tab")
        self.submit_web_prompt_with_started_notification("writer-tab", "thread-1", text="hello")
        self.controller.read_thread("writer-tab", "thread-1")
        self.controller.read_thread("observer-tab", "thread-1")

        self.assertTrue(
            self._handle_adapter_request(
                "shared-approval",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "rm file",
                },
                routing_mode="shared_approval",
            )
        )

        writer = self.controller.read_thread("writer-tab", "thread-1")
        observer = self.controller.read_thread("observer-tab", "thread-1")
        writer_pending = writer["pending_requests"][0]
        observer_pending = observer["pending_requests"][0]
        self.assertEqual(writer_pending["id"], observer_pending["id"])
        self.assertEqual(
            writer_pending["response_capability"],
            observer_pending["response_capability"],
        )
        self.assertEqual(
            self.controller.list_threads(client_id="writer-tab")["threads"][0][
                "pending_interaction"
            ],
            "approval",
        )
        self.assertEqual(
            self.controller.list_threads(client_id="observer-tab")["threads"][0][
                "pending_interaction"
            ],
            "approval",
        )

        self.respond_request(
            "observer-tab",
            jsonrpc_id_key("shared-approval"),
            action="approve_once",
        )
        self.assertEqual(
            self.fake.responses,
            [("shared-approval", {"decision": "accept"}, None)],
        )
        with self.assertRaises(WebRuntimeError) as duplicate:
            self.respond_request(
                "writer-tab",
                jsonrpc_id_key("shared-approval"),
                action="reject",
            )
        self.assertEqual(duplicate.exception.code, "request_processing")
        lease = self.store.load("thread-1")
        self.assertIsNone(lease)

        self._resolve_server_request("shared-approval", thread_id="thread-1")
        self.assertEqual(
            self.controller.read_thread("writer-tab", "thread-1")[
                "pending_requests"
            ],
            [],
        )
        self.assertEqual(
            self.controller.read_thread("observer-tab", "thread-1")[
                "pending_requests"
            ],
            [],
        )

    def test_shared_approval_survives_all_web_transport_disconnects(self):
        self.controller.client_connected("writer-tab")
        self.controller.client_connected("observer-tab")
        self.submit_web_prompt_with_started_notification("writer-tab", "thread-1", text="hello")
        self.controller.read_thread("observer-tab", "thread-1")
        self.assertTrue(
            self._handle_adapter_request(
                "shared-disconnect",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                },
                routing_mode="shared_approval",
            )
        )
        before = self.interaction_inbox.snapshot(
            jsonrpc_id_key("shared-disconnect")
        )
        assert before is not None

        self.controller.client_transport_disconnected("writer-tab")
        self.assertEqual(
            len(
                self.controller.read_thread("observer-tab", "thread-1")[
                    "pending_requests"
                ]
            ),
            1,
        )
        self.controller.client_transport_disconnected("observer-tab")
        after = self.interaction_inbox.snapshot(
            jsonrpc_id_key("shared-disconnect")
        )
        self.assertEqual(after, before)
        self.assertEqual(self.fake.responses, [])

        self.controller.client_connected("observer-tab")
        self.respond_request(
            "observer-tab",
            jsonrpc_id_key("shared-disconnect"),
            action="approve_once",
        )
        self.assertEqual(len(self.fake.responses), 1)

    def test_fresh_document_connect_reprojects_current_shared_approval_once(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.assertTrue(
            self._handle_adapter_request(
                "approval-before-f5",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                },
                routing_mode="shared_approval",
            )
        )

        initial = self.controller.read_thread("fresh-tab", "thread-1")
        self.assertFalse(self.document_registry.is_connected("fresh-tab"))
        self.assertEqual(initial["pending_requests"], [])
        revision_before_connect = self.projection.coordinates()["revision"]
        self.events.clear()

        self.controller.client_connected("fresh-tab")

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]["type"], "pending_request_changed")
        self.assertEqual(self.events[0]["thread_id"], "thread-1")
        self.assertEqual(self.events[0]["reason"], "document_connected")
        self.assertGreater(
            self.projection.coordinates()["revision"],
            revision_before_connect,
        )
        pending = self.controller.read_thread("fresh-tab", "thread-1")[
            "pending_requests"
        ]
        self.assertEqual(
            [request["id"] for request in pending],
            [jsonrpc_id_key("approval-before-f5")],
        )
        event_count = len(self.events)

        self.controller.client_connected("fresh-tab")

        self.assertEqual(len(self.events), event_count)

    def test_fresh_document_reprojects_submitted_and_unknown_without_reopening(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        expected_statuses = {
            "approval-submitted": "submitted",
            "approval-unknown": "unknown",
        }
        for request_id, expected_status in expected_statuses.items():
            with self.subTest(status=expected_status):
                self.assertTrue(
                    self._handle_adapter_request(
                        request_id,
                        "item/commandExecution/requestApproval",
                        {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "command": "pwd",
                        },
                        routing_mode="shared_approval",
                    )
                )
                self.fake.respond_error = (
                    RuntimeError("response outcome unknown")
                    if expected_status == "unknown"
                    else None
                )
                if expected_status == "unknown":
                    with self.assertRaises(WebRuntimeError):
                        self.respond_request(
                            "tab-1",
                            jsonrpc_id_key(request_id),
                            action="approve_once",
                        )
                else:
                    self.respond_request(
                        "tab-1",
                        jsonrpc_id_key(request_id),
                        action="approve_once",
                    )
                self.fake.respond_error = None

                fresh_client = f"fresh-{expected_status}"
                self.assertEqual(
                    self.controller.read_thread(fresh_client, "thread-1")[
                        "pending_requests"
                    ],
                    [],
                )
                self.events.clear()
                self.controller.client_connected(fresh_client)
                pending = self.controller.read_thread(fresh_client, "thread-1")[
                    "pending_requests"
                ]

                self.assertEqual(len(self.events), 1)
                self.assertEqual(self.events[0]["reason"], "document_connected")
                projected = next(
                    request
                    for request in pending
                    if request["id"] == jsonrpc_id_key(request_id)
                )
                self.assertEqual(projected["status"], expected_status)
                with self.assertRaises(WebRuntimeError) as duplicate:
                    self.respond_request(
                        fresh_client,
                        jsonrpc_id_key(request_id),
                        action="approve_once",
                    )
                self.assertEqual(
                    duplicate.exception.code,
                    "request_processing"
                    if expected_status == "submitted"
                    else "request_response_unknown",
                )
                self._resolve_server_request(request_id, thread_id="thread-1")

    def test_fresh_document_does_not_revive_approval_resolved_before_connect(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.assertTrue(
            self._handle_adapter_request(
                "resolved-before-connect",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                },
                routing_mode="shared_approval",
            )
        )
        self.assertEqual(
            self.controller.read_thread("fresh-resolved", "thread-1")[
                "pending_requests"
            ],
            [],
        )
        self._resolve_server_request(
            "resolved-before-connect",
            thread_id="thread-1",
        )
        self.events.clear()

        self.controller.client_connected("fresh-resolved")

        self.assertEqual(self.events, [])
        self.assertEqual(
            self.controller.read_thread("fresh-resolved", "thread-1")[
                "pending_requests"
            ],
            [],
        )

    def test_fresh_document_connect_does_not_share_writer_interaction(self):
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        self.seed_web_active_turn_writer("tab-1", "thread-1")
        self.assertTrue(
            self._handle_adapter_request(
                "writer-question",
                "item/tool/requestUserInput",
                {
                    "threadId": "thread-1",
                    "turnId": "",
                    "questions": [{"id": "q1", "question": "Continue?"}],
                },
                routing_mode="single_surface",
            )
        )
        self.assertEqual(
            self.controller.read_thread("fresh-question", "thread-1")[
                "pending_requests"
            ],
            [],
        )
        self.events.clear()

        self.controller.client_connected("fresh-question")

        self.assertEqual(self.events, [])
        self.assertEqual(
            self.controller.read_thread("fresh-question", "thread-1")[
                "pending_requests"
            ],
            [],
        )

    def test_later_web_observer_can_approve_fcodex_turn_for_session(self):
        self.controller.client_connected("observer-one")
        self.controller.client_connected("observer-two")
        self.controller.read_thread("observer-one", "thread-1")
        acquired = self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:remote", owner_pid=0),
        )
        assert acquired.lease is not None
        self.store.activate_turn(acquired.lease, "turn-1")

        self.assertTrue(
            self._handle_adapter_request(
                "shared-file",
                "item/fileChange/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "reason": "apply patch",
                },
                routing_mode="shared_approval",
            )
        )
        self.assertEqual(
            self.controller.read_thread("observer-one", "thread-1")[
                "pending_requests"
            ][0]["id"],
            jsonrpc_id_key("shared-file"),
        )
        self.assertFalse(
            self.controller._shared_interaction_eligible(
                "observer-two",
                "thread-1",
                "thread-1",
                "turn-1",
            )
        )

        observer_two = self.controller.read_thread("observer-two", "thread-1")
        self.assertEqual(
            observer_two["pending_requests"][0]["id"],
            jsonrpc_id_key("shared-file"),
        )
        self.respond_request(
            "observer-two",
            jsonrpc_id_key("shared-file"),
            action="approve_session",
        )
        self.assertEqual(
            self.fake.responses,
            [("shared-file", {"decision": "acceptForSession"}, None)],
        )
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.holder.holder_id, "fcodex:remote")

    def test_shared_pre_send_allows_other_document_retry_but_unknown_does_not(self):
        self.controller.client_connected("writer-tab")
        self.controller.client_connected("observer-tab")
        self.submit_web_prompt_with_started_notification("writer-tab", "thread-1", text="hello")
        self.controller.read_thread("observer-tab", "thread-1")
        self.assertTrue(
            self._handle_adapter_request(
                "shared-retry",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                },
                routing_mode="shared_approval",
            )
        )
        self.fake.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("offline before send"),
        )
        with self.assertRaises(WebRuntimeError) as not_sent:
            self.respond_request(
                "writer-tab",
                jsonrpc_id_key("shared-retry"),
                action="approve_once",
            )
        self.assertEqual(not_sent.exception.code, "request_not_sent")
        self.fake.respond_error = None
        self.respond_request(
            "observer-tab",
            jsonrpc_id_key("shared-retry"),
            action="approve_once",
        )

        self.assertTrue(
            self._handle_adapter_request(
                "shared-unknown",
                "item/commandExecution/requestApproval",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "whoami",
                },
                routing_mode="shared_approval",
            )
        )
        self.fake.respond_error = RuntimeError("possibly sent")
        with self.assertRaises(WebRuntimeError) as unknown:
            self.respond_request(
                "writer-tab",
                jsonrpc_id_key("shared-unknown"),
                action="approve_once",
            )
        self.assertEqual(unknown.exception.code, "request_response_unknown")
        self.fake.respond_error = None
        with self.assertRaises(WebRuntimeError) as fenced:
            self.respond_request(
                "observer-tab",
                jsonrpc_id_key("shared-unknown"),
                action="approve_once",
            )
        self.assertEqual(fenced.exception.code, "request_response_unknown")


if __name__ == "__main__":
    unittest.main()
