from __future__ import annotations

from bot.server_request_contract import ServerRequestIdentity
from tests.web_runtime.harness import WebRuntimeControllerHarness


APPROVAL = "item/commandExecution/requestApproval"


class WebServerRequestRemovalTests(WebRuntimeControllerHarness):
    def _present(self, request_id: str) -> ServerRequestIdentity:
        self.controller.client_connected("tab-1")
        self.submit_web_prompt_with_started_notification("tab-1", "thread-1", text="hello")
        candidate = ServerRequestIdentity(
            request_id=request_id,
            connection_generation=self.server_request_generation,
            method=APPROVAL,
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
            },
        )
        claim = self.server_request_registry.register(candidate)
        self.assertIsNotNone(claim.identity)
        assert claim.identity is not None
        self.assertTrue(
            self.controller.handle_adapter_request(
                claim.identity,
                routing_mode="shared_approval",
            )
        )
        return claim.identity

    def _settle(self, identity: ServerRequestIdentity) -> None:
        settlement = self.server_request_registry.settle(
            identity.request_key,
            thread_id=identity.thread_id,
        )
        self.assertEqual(settlement.outcome, "settled")

    def test_canonical_resolution_removes_the_exact_web_projection(self) -> None:
        identity = self._present("approval-resolved")
        self._settle(identity)

        removal = self.controller.remove_resolved_server_request(identity)

        self.assertEqual(removal.outcome, "removed")
        self.assertEqual(removal.request_key, identity.request_key)
        self.assertEqual(removal.thread_id, "thread-1")
        self.assertEqual(removal.root_thread_id, "thread-1")
        self.assertFalse(self.controller.has_pending_request(identity.request_key))
        self.assertEqual(self.events[-1]["reason"], "resolved_elsewhere")

    def test_notification_thread_mismatch_preserves_the_projection(self) -> None:
        identity = self._present("approval-thread-mismatch")
        self._settle(identity)
        mismatch = ServerRequestIdentity(
            request_id=identity.request_id,
            connection_generation=identity.connection_generation,
            method=identity.method,
            params={**identity.params, "threadId": "thread-other"},
        )

        removal = self.controller.remove_resolved_server_request(mismatch)

        self.assertEqual(removal.outcome, "mismatch")
        self.assertEqual(removal.root_thread_id, "")
        self.assertTrue(self.controller.has_pending_request(identity.request_key))

    def test_value_equal_stale_identity_cannot_remove_the_projection(self) -> None:
        identity = self._present("approval-object-mismatch")
        self._settle(identity)
        stale = ServerRequestIdentity(
            request_id=identity.request_id,
            connection_generation=identity.connection_generation,
            method=identity.method,
            params=identity.params,
        )
        self.assertTrue(stale.same_identity_as(identity))
        self.assertIsNot(stale, identity)

        removal = self.controller.remove_resolved_server_request(stale)

        self.assertEqual(removal.outcome, "mismatch")
        self.assertEqual(removal.root_thread_id, "")
        self.assertTrue(self.controller.has_pending_request(identity.request_key))

    def test_unconfirmed_resolution_preserves_the_projection(self) -> None:
        identity = self._present("approval-not-resolved")

        removal = self.controller.remove_resolved_server_request(identity)

        self.assertEqual(removal.outcome, "not_resolved")
        self.assertEqual(removal.root_thread_id, "")
        self.assertTrue(self.controller.has_pending_request(identity.request_key))

    def test_missing_removal_is_idempotent_and_does_not_guess_root(self) -> None:
        identity = ServerRequestIdentity(
            request_id="missing-web-request",
            connection_generation=self.server_request_generation,
            method=APPROVAL,
            params={"threadId": "thread-1"},
        )
        events_before = list(self.events)

        first = self.controller.remove_resolved_server_request(identity)
        second = self.controller.remove_resolved_server_request(identity)

        self.assertEqual(first.outcome, "missing")
        self.assertEqual(second, first)
        self.assertEqual(first.root_thread_id, "")
        self.assertEqual(self.events, events_before)

    def test_generic_notification_does_not_remove_coordinator_owned_projection(
        self,
    ) -> None:
        identity = self._present("approval-generic-skip")
        self._settle(identity)

        self.controller.handle_notification(
            "serverRequest/resolved",
            {"threadId": "thread-1", "requestId": "approval-generic-skip"},
        )

        self.assertTrue(self.controller.has_pending_request(identity.request_key))
