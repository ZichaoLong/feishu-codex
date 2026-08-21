from __future__ import annotations

import unittest

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestResponseReport,
    ServerRequestResponseSupersededError,
)
from bot.server_request_registry import ServerRequestRegistry
from bot.web_runtime.interaction_inbox import (
    WebInteractionDeliveryScope,
    WebInteractionInbox,
    WebInteractionInboxError,
    WebInteractionInboxPorts,
)


APPROVAL = "item/commandExecution/requestApproval"
QUESTION = "item/tool/requestUserInput"


class WebInteractionInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ServerRequestRegistry(resolved_limit=32)
        self.registry.activate_connection_epoch(1)
        self.responses: list[tuple[object, dict[str, object]]] = []
        self.respond_error: Exception | None = None

        def respond(request_id, **kwargs) -> None:
            if self.respond_error is not None:
                raise self.respond_error
            self.responses.append((request_id, kwargs))

        self.inbox = WebInteractionInbox(
            ports=WebInteractionInboxPorts(
                respond=respond,
                active_matches=self.registry.active_matches,
            ),
            runtime_context_guard=lambda: None,
            monotonic=lambda: 10.0,
        )

    def _identity(
        self,
        request_id: str = "request-1",
        *,
        generation: int = 1,
        command: str = "pwd",
        method: str = APPROVAL,
        thread_id: str = "thread-1",
        turn_id: str = "turn-1",
    ) -> ServerRequestIdentity:
        candidate = ServerRequestIdentity(
            request_id=request_id,
            connection_generation=generation,
            method=method,
            params={
                "threadId": thread_id,
                "turnId": turn_id,
                "command": command,
            },
        )
        registration = self.registry.register(candidate)
        self.assertIsNotNone(registration.identity)
        assert registration.identity is not None
        return registration.identity

    def _present(
        self,
        identity: ServerRequestIdentity,
        *,
        owner_thread_id: str = "thread-1",
        client_id: str = "tab-1",
        delivery_scope: WebInteractionDeliveryScope = "writer_interaction",
    ):
        ingress = self.inbox.prepare_ingress(identity)
        self.assertEqual(ingress.disposition, "route")
        self.inbox.present(
            ingress,
            owner_thread_id=owner_thread_id,
            client_id=client_id,
            delivery_scope=delivery_scope,
        )
        snapshot = self.inbox.snapshot(identity.request_key)
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        return snapshot

    def test_shared_approval_requires_direct_root_without_client_or_timer(
        self,
    ) -> None:
        shared = self._present(
            self._identity("shared-valid"),
            client_id="",
            delivery_scope="shared_interaction",
        )

        self.assertEqual(shared.delivery_scope, "shared_interaction")
        self.assertEqual(shared.client_id, "")
        self.assertEqual(shared.thread_id, shared.owner_thread_id)
        self.assertEqual(
            shared.projection_dict()["delivery_scope"],
            "shared_interaction",
        )

        invalid_cases = (
            (
                "unsupported-method",
                self._identity("shared-tool", method="item/tool/call"),
                "thread-1",
                "",
                None,
            ),
            (
                "descendant",
                self._identity("shared-child", thread_id="child-1"),
                "thread-1",
                "",
                None,
            ),
            (
                "missing-turn",
                self._identity("shared-missing-turn", turn_id=""),
                "thread-1",
                "",
                None,
            ),
            (
                "client-owner",
                self._identity("shared-client"),
                "thread-1",
                "tab-1",
                None,
            ),
            (
                "timer",
                self._identity("shared-timer"),
                "thread-1",
                "",
                AutoResolutionTiming(1, 1, 10, 20),
            ),
        )
        for label, identity, root_id, client_id, timing in invalid_cases:
            with self.subTest(label=label):
                ingress = self.inbox.prepare_ingress(identity)
                with self.assertRaises(ValueError):
                    self.inbox.present(
                        ingress,
                        owner_thread_id=root_id,
                        client_id=client_id,
                        auto_resolution_timing=timing,
                        delivery_scope="shared_interaction",
                    )
                self.assertFalse(self.inbox.contains(identity.request_key))

    def test_writer_ingress_cannot_rebind_a_child_request_to_a_root(self) -> None:
        identity = self._identity("writer-child", thread_id="child-1")
        ingress = self.inbox.prepare_ingress(identity)

        with self.assertRaisesRegex(ValueError, "direct thread owner"):
            self.inbox.present(
                ingress,
                owner_thread_id="thread-1",
                client_id="tab-1",
            )

        with self.assertRaisesRegex(ValueError, "direct thread owner"):
            self.inbox.fail_close(
                ingress,
                owner_thread_id="thread-1",
                client_id="tab-1",
                hidden=True,
                message="unsupported",
            )
        self.assertFalse(self.inbox.contains(identity.request_key))
        self.assertEqual(self.responses, [])

    def test_shared_user_input_timer_issues_exact_system_response(self) -> None:
        identity = self._identity("shared-question", method=QUESTION)
        timing = AutoResolutionTiming(3, 5, 100, 200)
        ingress = self.inbox.prepare_ingress(identity)
        self.inbox.present(
            ingress,
            owner_thread_id="thread-1",
            client_id="",
            auto_resolution_timing=timing,
            delivery_scope="shared_interaction",
        )

        stale = self.inbox.prepare_auto_resolution(
            identity.request_key,
            timing.backend_epoch,
            timing.generation + 1,
        )
        exact = self.inbox.prepare_auto_resolution(
            identity.request_key,
            timing.backend_epoch,
            timing.generation,
        )

        self.assertEqual(stale.outcome, "recognized")
        self.assertIsNone(stale.response)
        self.assertEqual(exact.outcome, "ready")
        assert exact.response is not None
        self.assertEqual(exact.response.delivery_scope, "shared_interaction")
        self.inbox.submit_response(exact.response, action="auto_resolve")
        self.assertEqual(self.responses[-1][1]["result"], {"answers": {}})

    def test_shared_is_visible_to_two_clients_but_writer_stays_local(self) -> None:
        writer = self._present(self._identity("writer"))
        shared = self._present(
            self._identity("shared"),
            client_id="",
            delivery_scope="shared_interaction",
        )

        tab_one = {
            snapshot.request_key: snapshot
            for snapshot in self.inbox.visible_snapshots("tab-1", "thread-1")
        }
        tab_two = {
            snapshot.request_key: snapshot
            for snapshot in self.inbox.visible_snapshots("tab-2", "thread-1")
        }

        self.assertEqual(set(tab_one), {writer.request_key, shared.request_key})
        self.assertEqual(set(tab_two), {shared.request_key})
        self.assertEqual(
            tab_one[shared.request_key].response_capability,
            tab_two[shared.request_key].response_capability,
        )

    def test_shared_response_accepts_another_client_but_writer_does_not(
        self,
    ) -> None:
        writer = self._present(self._identity("writer-response"))
        shared = self._present(
            self._identity("shared-response"),
            client_id="",
            delivery_scope="shared_interaction",
        )

        preparation = self.inbox.prepare_response(
            "tab-2",
            shared.request_key,
            shared.connection_generation,
            shared.response_capability,
        )
        self.assertEqual(preparation.delivery_scope, "shared_interaction")
        self.assertEqual(preparation.turn_id, "turn-1")

        with self.assertRaises(WebInteractionInboxError) as not_owned:
            self.inbox.prepare_response(
                "tab-2",
                writer.request_key,
                writer.connection_generation,
                writer.response_capability,
            )
        self.assertEqual(not_owned.exception.code, "request_not_owned")

    def test_disconnect_drops_writer_but_retains_same_root_shared_approval(
        self,
    ) -> None:
        writer = self._present(self._identity("writer-disconnect"))
        shared = self._present(
            self._identity("shared-disconnect"),
            client_id="",
            delivery_scope="shared_interaction",
        )

        mutation = self.inbox.fail_close_client("tab-1", "thread-1")

        self.assertIsNone(self.inbox.snapshot(writer.request_key))
        self.assertIsNotNone(self.inbox.snapshot(shared.request_key))
        self.assertEqual(self.responses, [])
        self.assertEqual(mutation.changes[0].reason, "client_disconnected")

    def test_replay_can_upgrade_writer_to_shared_but_cannot_downgrade(
        self,
    ) -> None:
        identity = self._identity("scope-replay")
        writer = self._present(identity)

        replay = self._identity("scope-replay")
        ingress = self.inbox.prepare_ingress(replay)
        self.inbox.present(
            ingress,
            owner_thread_id="thread-1",
            client_id="",
            delivery_scope="shared_interaction",
        )
        shared = self.inbox.snapshot(identity.request_key)
        assert shared is not None
        self.assertEqual(shared.delivery_scope, "shared_interaction")
        self.assertEqual(shared.response_capability, writer.response_capability)

        before_downgrade = shared
        ingress = self.inbox.prepare_ingress(replay)
        with self.assertRaises(WebInteractionInboxError) as rejected:
            self.inbox.present(
                ingress,
                owner_thread_id="thread-1",
                client_id="tab-2",
            )
        self.assertEqual(rejected.exception.code, "request_response_unknown")
        self.assertEqual(self.inbox.snapshot(identity.request_key), before_downgrade)

    def test_writer_replay_cannot_transfer_to_another_client(self) -> None:
        identity = self._identity("writer-transfer")
        before_transfer = self._present(identity)

        ingress = self.inbox.prepare_ingress(self._identity("writer-transfer"))
        with self.assertRaises(WebInteractionInboxError) as rejected:
            self.inbox.present(
                ingress,
                owner_thread_id="thread-1",
                client_id="tab-2",
            )

        self.assertEqual(rejected.exception.code, "request_response_unknown")
        self.assertEqual(self.inbox.snapshot(identity.request_key), before_transfer)

    def test_cross_root_candidates_include_shared_without_other_writer(self) -> None:
        own_writer = self._present(self._identity("own-writer"))
        other_writer = self._present(
            self._identity("other-writer", thread_id="thread-2"),
            owner_thread_id="thread-2",
            client_id="tab-2",
        )
        shared = self._present(
            self._identity("other-shared", thread_id="thread-2"),
            owner_thread_id="thread-2",
            client_id="",
            delivery_scope="shared_interaction",
        )

        candidates = {
            snapshot.request_key
            for snapshot in self.inbox.candidate_snapshots("tab-1")
        }

        self.assertEqual(candidates, {own_writer.request_key, shared.request_key})
        self.assertNotIn(other_writer.request_key, candidates)
        self.assertEqual(self.inbox.root_ids_for_client("tab-1"), {"thread-1"})

    def test_response_requires_generation_and_one_time_surface_capability(self) -> None:
        identity = self._identity()
        snapshot = self._present(identity)

        with self.assertRaises(WebInteractionInboxError) as stale:
            self.inbox.prepare_response(
                "tab-1",
                identity.request_key,
                1,
                "stale-token",
            )
        self.assertEqual(stale.exception.code, "response_capability_mismatch")

        preparation = self.inbox.prepare_response(
            "tab-1",
            identity.request_key,
            snapshot.connection_generation,
            snapshot.response_capability,
        )
        submission = self.inbox.submit_response(
            preparation,
            action="approve_once",
        )

        self.assertEqual(submission.status, "submitted")
        self.assertEqual(len(self.responses), 1)
        with self.assertRaises(WebInteractionInboxError) as repeated:
            self.inbox.prepare_response(
                "tab-1",
                identity.request_key,
                snapshot.connection_generation,
                snapshot.response_capability,
            )
        self.assertEqual(repeated.exception.code, "request_processing")

    def test_pre_send_failure_is_retryable_but_unknown_is_not(self) -> None:
        identity = self._identity("pre-send")
        snapshot = self._present(identity)
        preparation = self.inbox.prepare_response(
            "tab-1",
            identity.request_key,
            1,
            snapshot.response_capability,
        )
        self.respond_error = CodexRpcPreSendError(
            "serverRequest/response",
            RuntimeError("disconnected"),
        )
        with self.assertRaises(WebInteractionInboxError) as not_sent:
            self.inbox.submit_response(preparation, action="reject")
        self.assertEqual(not_sent.exception.code, "request_not_sent")
        self.assertEqual(self.inbox.snapshot(identity.request_key).status, "pending")

        self.respond_error = RuntimeError("write outcome unknown")
        preparation = self.inbox.prepare_response(
            "tab-1",
            identity.request_key,
            1,
            snapshot.response_capability,
        )
        with self.assertRaises(WebInteractionInboxError) as unknown:
            self.inbox.submit_response(preparation, action="reject")
        self.assertEqual(unknown.exception.code, "request_response_unknown")
        self.assertEqual(self.inbox.snapshot(identity.request_key).status, "unknown")

    def test_superseded_response_retires_projection_without_claiming_success(
        self,
    ) -> None:
        identity = self._identity("superseded")
        snapshot = self._present(identity)
        preparation = self.inbox.prepare_response(
            "tab-1",
            identity.request_key,
            1,
            snapshot.response_capability,
        )
        self.respond_error = ServerRequestResponseSupersededError(
            ServerRequestResponseReport(
                "superseded",
                request_key=identity.request_key,
                thread_id=identity.thread_id,
            )
        )

        with self.assertRaises(WebInteractionInboxError) as superseded:
            self.inbox.submit_response(preparation, action="approve_once")

        self.assertEqual(superseded.exception.code, "request_superseded")
        self.assertEqual(superseded.exception.changes[0].reason, "response_superseded")
        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(self.responses, [])

    def test_exact_replay_reuses_identity_and_surface_capability(self) -> None:
        identity = self._identity("replay")
        first = self._present(identity)

        replay = self._identity("replay")
        self.assertIs(replay, identity)
        second = self._present(replay)

        self.assertEqual(first.response_capability, second.response_capability)
        self.assertEqual(self.inbox.pending_count(), 1)

    def test_identity_conflict_closes_only_the_exact_surface_record(self) -> None:
        identity = self._identity("conflict")
        self._present(identity)
        self.registry.clear_connection_epoch()
        self.registry.activate_connection_epoch(2)
        replacement = ServerRequestIdentity(
            request_id="conflict",
            connection_generation=2,
            method=APPROVAL,
            params={
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "different",
            },
        )
        registration = self.registry.register(replacement)
        self.assertIs(registration.identity, replacement)

        ingress = self.inbox.prepare_ingress(replacement)

        self.assertEqual(ingress.disposition, "identity_conflict")
        snapshot = self.inbox.snapshot(identity.request_key)
        self.assertTrue(snapshot.hidden)
        self.assertEqual(snapshot.status, "unknown")

    def test_resolution_requires_the_exact_canonical_object(self) -> None:
        identity = self._identity("resolved")
        self._present(identity)
        stale = ServerRequestIdentity(
            request_id=identity.request_id,
            connection_generation=identity.connection_generation,
            method=identity.method,
            params=identity.params,
        )

        self.assertEqual(self.inbox.resolve_exact(stale).outcome, "mismatch")
        settlement = self.registry.settle(
            identity.request_key,
            thread_id=identity.thread_id,
        )
        self.assertEqual(settlement.outcome, "settled")
        self.assertEqual(self.inbox.resolve_exact(identity).outcome, "resolved")
        self.assertEqual(self.inbox.resolve_exact(identity).outcome, "missing")

    def test_browser_disconnect_drops_projection_without_backend_response(self) -> None:
        identity = self._identity("browser-disconnect")
        self._present(identity)

        mutation = self.inbox.fail_close_client("tab-1", "thread-1")

        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(self.responses, [])
        self.assertEqual(mutation.changes[0].reason, "client_disconnected")
        self.assertTrue(self.registry.active_matches(identity))

    def test_backend_disconnect_clears_all_old_generation_capabilities(self) -> None:
        self._present(self._identity("one"))
        self._present(self._identity("two"))

        mutation = self.inbox.backend_disconnected()

        self.assertEqual(self.inbox.pending_count(), 0)
        self.assertEqual(
            {change.root_thread_id for change in mutation.changes},
            {"thread-1"},
        )


if __name__ == "__main__":
    unittest.main()
