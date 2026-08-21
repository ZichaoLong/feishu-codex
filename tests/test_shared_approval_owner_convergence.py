from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace

from bot.fcodex.interaction_inbox import (
    FcodexInteractionInbox,
    FcodexInteractionInboxPorts,
    FcodexInteractionWriter,
)
from bot.interaction_request_controller import InteractionRequestController
from bot.server_request_contract import ServerRequestIdentity, ServerRequestLocalRemoval
from bot.server_request_coordinator import (
    ServerRequestCoordinator,
    ServerRequestCoordinatorPorts,
)
from bot.server_request_dispatch import ServerRequestDispatchReceipt
from bot.server_request_registry import ServerRequestRegistry
from bot.stores.interaction_lease_store import InteractionLeaseHolder
from bot.web_runtime.interaction_inbox import (
    WebInteractionInbox,
    WebInteractionInboxError,
    WebInteractionInboxPorts,
)


ROOT = "root-1"
PARTICIPANT = "fcodex:local"
ENDPOINT_A = "connection-a"
ENDPOINT_B = "connection-b"
APPROVAL = "item/commandExecution/requestApproval"
QUESTION = "item/tool/requestUserInput"


class _LocalAuthority:
    def __init__(self) -> None:
        self.endpoints = {
            (PARTICIPANT, ENDPOINT_A),
            (PARTICIPANT, ENDPOINT_B),
        }
        self.holder = InteractionLeaseHolder(
            kind="fcodex",
            holder_id=PARTICIPANT,
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
        )

    @staticmethod
    def interaction_root_for_thread(thread_id: str) -> str:
        return ROOT if thread_id == ROOT else ""

    def interaction_writer_for_root(
        self,
        root_thread_id: str,
    ) -> FcodexInteractionWriter | None:
        if root_thread_id != ROOT:
            return None
        return FcodexInteractionWriter(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            holder=self.holder,
            connected=True,
        )

    def interaction_lease_holder_for_root(
        self,
        root_thread_id: str,
    ) -> InteractionLeaseHolder | None:
        return self.holder if root_thread_id == ROOT else None

    @staticmethod
    def shared_interaction_request_is_eligible(
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool:
        return bool(
            root_thread_id == ROOT
            and request_thread_id == ROOT
            and turn_id == "turn-1"
        )

    def shared_interaction_endpoint_is_attached(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> bool:
        return bool(
            root_thread_id == ROOT
            and (participant_id, connection_id) in self.endpoints
        )

    def shared_interaction_has_live_recipient(self, root_thread_id: str) -> bool:
        return bool(root_thread_id == ROOT and self.endpoints)


class SharedApprovalOwnerConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ServerRequestRegistry(resolved_limit=32)
        self.authority = _LocalAuthority()
        self.wire_responses: list[tuple[object, dict[str, object]]] = []
        self.card_patches: list[tuple[str, str, str]] = []
        self.web_revocation_changes = []
        self.coordinator: ServerRequestCoordinator

        self.fcodex = FcodexInteractionInbox(
            ports=FcodexInteractionInboxPorts(
                authority=self.authority,
                server_request_is_resolved=self.registry.request_is_resolved,
                server_request_response_authority_is_revoked=(
                    self.registry.request_response_authority_is_revoked
                ),
                respond=lambda identity, **kwargs: self.coordinator.submit_surface_response(
                    identity, **kwargs
                ),
                schedule_proxy_delivery_expiry=lambda *_args: None,
            ),
            runtime_context_guard=lambda: None,
        )
        self.web = WebInteractionInbox(
            ports=WebInteractionInboxPorts(
                respond=lambda identity, **kwargs: self.coordinator.submit_surface_response(
                    identity, **kwargs
                ),
                active_matches=self.registry.response_authority_is_open,
            ),
            runtime_context_guard=lambda: None,
            monotonic=lambda: 10.0,
        )
        self.feishu = InteractionRequestController(
            lock=threading.RLock(),
            resident_session_snapshot_locked=lambda _binding: SimpleNamespace(
                current_thread_id=ROOT,
                execution=SimpleNamespace(
                    current_prompt_message_id="prompt-1",
                    current_prompt_reply_in_thread=True,
                    current_actor_open_id="ou-member",
                ),
            ),
            interactive_binding_for_thread=lambda _thread_id: (
                ("ou-member", "chat-1"),
                False,
            ),
            interaction_actor_allowed=lambda *_args: True,
            send_interactive_card=lambda *_args: "feishu-card-1",
            reply_text=lambda *_args, **_kwargs: None,
            respond=lambda identity, **kwargs: self.coordinator.submit_surface_response(
                identity, **kwargs
            ),
            revoke_response_authority=lambda identity: self.coordinator.revoke_surface_response_authority(
                identity
            ),
            patch_message=lambda chat_id, message_id, content: self.card_patches.append(
                (chat_id, message_id, content)
            )
            or True,
        )
        self.coordinator = ServerRequestCoordinator(
            self.registry,
            ServerRequestCoordinatorPorts(
                cancel_auto_resolution=lambda _request_key: None,
                remove_web_resolved=self._remove_web,
                revoke_web_response_authority=(
                    self._revoke_web_response_authority
                ),
                remove_fcodex_resolved=self.fcodex.remove_resolved,
                remove_feishu_resolved=self.feishu.remove_resolved_server_request,
                reconcile_resolved_root=lambda _root: None,
                invalidate_auto_resolution_epoch=lambda: None,
                shutdown_auto_resolution=lambda: None,
                dispatch_request=self._dispatch_all,
                respond=lambda request_id, **kwargs: self.wire_responses.append(
                    (request_id, kwargs)
                ),
            ),
            lambda: None,
        )
        self.coordinator.activate_connection_epoch(1)

    def _dispatch_all(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestDispatchReceipt:
        routing_mode = (
            "shared_approval" if identity.method == APPROVAL else "shared_interaction"
        )
        fcodex = self.fcodex.service_request(
            identity, routing_mode=routing_mode
        )
        self.assertTrue(fcodex["handled"])
        ingress = self.web.prepare_ingress(identity)
        self.web.present(
            ingress,
            owner_thread_id=ROOT,
            client_id="",
            delivery_scope="shared_interaction",
        )
        if routing_mode == "shared_approval":
            self.assertTrue(
                self.feishu.handle_adapter_request(
                    identity, routing_mode="shared_approval"
                )
            )
        return ServerRequestDispatchReceipt.committed()

    def _remove_web(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        resolution = self.web.resolve_exact(identity)
        outcome = "removed" if resolution.outcome == "resolved" else resolution.outcome
        return ServerRequestLocalRemoval(
            outcome,
            request_key=identity.request_key,
            thread_id=identity.thread_id,
            root_thread_id=resolution.owner_thread_id,
        )

    def _revoke_web_response_authority(
        self,
        identity: ServerRequestIdentity,
    ) -> None:
        mutation = self.web.revoke_exact_response_authority(identity)
        self.web_revocation_changes.extend(mutation.changes)

    def _route(self, request_id: str) -> ServerRequestIdentity:
        report = self.coordinator.route_request(
            1,
            request_id,
            APPROVAL,
            {"threadId": ROOT, "turnId": "turn-1", "command": "pwd"},
        )
        self.assertEqual(report.outcome, "committed")
        identity = self.registry.active_identity(report.request_key)
        assert identity is not None
        return identity

    def _route_question(self, request_id: str) -> ServerRequestIdentity:
        report = self.coordinator.route_request(
            1,
            request_id,
            QUESTION,
            {
                "threadId": ROOT,
                "turnId": "turn-1",
                "questions": [{"id": "q1", "question": "Continue?"}],
            },
        )
        self.assertEqual(report.outcome, "committed")
        identity = self.registry.active_identity(report.request_key)
        assert identity is not None
        return identity

    def _fcodex_projection(
        self,
        identity: ServerRequestIdentity,
        connection_id: str,
    ) -> dict[str, object]:
        return self.fcodex.proxy_request(
            participant_id=PARTICIPANT,
            connection_id=connection_id,
            request_id=identity.request_id,
            method=identity.method,
            params=identity.params,
        )

    def _resolve(self, identity: ServerRequestIdentity) -> None:
        report = self.coordinator.handle_server_request_resolved(
            {"requestId": identity.request_id, "threadId": ROOT}
        )
        self.assertEqual(report.outcome, "settled")

    def test_web_first_retires_real_fcodex_and_feishu_projections(self) -> None:
        identity = self._route("web-first")
        fcodex = self._fcodex_projection(identity, ENDPOINT_A)
        web = self.web.snapshot(identity.request_key)
        assert web is not None

        preparation = self.web.prepare_response(
            "tab-1",
            identity.request_key,
            identity.connection_generation,
            web.response_capability,
        )
        submitted = self.web.submit_response(preparation, action="approve_once")
        stale = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            request_id=identity.request_id,
            response_token=str(fcodex["response_token"]),
            result={"decision": "accept"},
            error=None,
        )

        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(stale["response_disposition"], "superseded")
        self.assertEqual(len(self.wire_responses), 1)
        self._resolve(identity)
        self.assertEqual(self.web.pending_count(), 0)
        self.assertEqual(self.fcodex.pending_count(), 0)
        self.assertFalse(self.feishu.has_pending_request(identity.request_key))
        self.assertEqual(self.card_patches[0][1], "feishu-card-1")

    def test_fcodex_first_converges_other_fcodex_web_and_feishu(self) -> None:
        identity = self._route("fcodex-first")
        first = self._fcodex_projection(identity, ENDPOINT_A)
        second = self._fcodex_projection(identity, ENDPOINT_B)
        web = self.web.snapshot(identity.request_key)
        assert web is not None

        accepted = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            request_id=identity.request_id,
            response_token=str(first["response_token"]),
            result={"decision": "accept"},
            error=None,
        )
        duplicate = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_B,
            request_id=identity.request_id,
            response_token=str(second["response_token"]),
            result={"decision": "decline"},
            error=None,
        )
        preparation = self.web.prepare_response(
            "tab-2",
            identity.request_key,
            identity.connection_generation,
            web.response_capability,
        )
        with self.assertRaises(WebInteractionInboxError) as stale_web:
            self.web.submit_response(preparation, action="reject")

        self.assertEqual(accepted["response_disposition"], "submitted")
        self.assertEqual(duplicate["response_disposition"], "superseded")
        self.assertEqual(stale_web.exception.code, "request_superseded")
        self.assertEqual(len(self.wire_responses), 1)
        self._resolve(identity)
        self.assertEqual(self.web.pending_count(), 0)
        self.assertEqual(self.fcodex.pending_count(), 0)
        self.assertFalse(self.feishu.has_pending_request(identity.request_key))
        self.assertEqual(self.card_patches[0][1], "feishu-card-1")

    def test_exact_revocation_hides_web_and_blocks_fcodex_late_attach(self) -> None:
        identity = self._route("revoked-shared-approval")
        first = self._fcodex_projection(identity, ENDPOINT_A)

        self.assertEqual(first["action"], "deliver")
        self.assertEqual(len(self.web.visible_snapshots("tab-1", ROOT)), 1)
        self.assertTrue(
            self.coordinator.revoke_surface_response_authority(identity)
        )

        late = self._fcodex_projection(identity, ENDPOINT_B)
        stale = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            request_id=identity.request_id,
            response_token=str(first["response_token"]),
            result={"decision": "accept"},
            error=None,
        )
        replay = self.coordinator.route_request(
            identity.connection_generation,
            identity.request_id,
            identity.method,
            identity.params,
        )

        self.assertEqual(self.web.visible_snapshots("tab-1", ROOT), ())
        self.assertEqual(
            [change.reason for change in self.web_revocation_changes],
            ["response_authority_revoked"],
        )
        self.assertEqual(late["action"], "suppress")
        self.assertEqual(
            late["reason"],
            "server_request_response_authority_revoked",
        )
        self.assertEqual(stale["response_disposition"], "superseded")
        self.assertEqual(self.wire_responses, [])
        self.assertEqual(replay.outcome, "response_pending_resolution")
        self.assertTrue(replay.response_authority_revoked)

    def test_shared_question_web_first_converges_fcodex_without_feishu(self) -> None:
        identity = self._route_question("question-web-first")
        fcodex = self._fcodex_projection(identity, ENDPOINT_A)
        web = self.web.snapshot(identity.request_key)
        assert web is not None
        preparation = self.web.prepare_response(
            "tab-1",
            identity.request_key,
            identity.connection_generation,
            web.response_capability,
        )

        submitted = self.web.submit_response(
            preparation,
            action="answer",
            answers={"q1": "yes"},
        )
        stale = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            request_id=identity.request_id,
            response_token=str(fcodex["response_token"]),
            result={"answers": {"q1": {"answers": ["yes"]}}},
            error=None,
        )

        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(stale["response_disposition"], "superseded")
        self.assertEqual(len(self.wire_responses), 1)
        self.assertFalse(self.feishu.has_pending_request(identity.request_key))

    def test_shared_question_fcodex_first_converges_web_without_feishu(self) -> None:
        identity = self._route_question("question-fcodex-first")
        fcodex = self._fcodex_projection(identity, ENDPOINT_A)
        web = self.web.snapshot(identity.request_key)
        assert web is not None

        accepted = self.fcodex.response_submit(
            participant_id=PARTICIPANT,
            connection_id=ENDPOINT_A,
            request_id=identity.request_id,
            response_token=str(fcodex["response_token"]),
            result={"answers": {"q1": {"answers": ["yes"]}}},
            error=None,
        )
        preparation = self.web.prepare_response(
            "tab-1",
            identity.request_key,
            identity.connection_generation,
            web.response_capability,
        )
        with self.assertRaises(WebInteractionInboxError) as stale_web:
            self.web.submit_response(
                preparation,
                action="answer",
                answers={"q1": "no"},
            )

        self.assertEqual(accepted["response_disposition"], "submitted")
        self.assertEqual(stale_web.exception.code, "request_superseded")
        self.assertEqual(len(self.wire_responses), 1)
        self.assertFalse(self.feishu.has_pending_request(identity.request_key))


if __name__ == "__main__":
    unittest.main()
