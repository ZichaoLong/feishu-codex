import json
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import CodexRpcPreSendError, CodexRpcTransportError
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.interaction_request_controller import (
    InteractionRequestController,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestResponseReport,
    ServerRequestResponseSupersededError,
)
from bot.server_request_dispatch import ServerRequestSurfaceIdentityConflict
from bot.stores.chat_binding_store import ChatBindingStore
from tests.runtime_admin_test_support import make_binding_runtime


class InteractionRequestControllerTests(unittest.TestCase):
    @staticmethod
    def _identity(
        request_id: int | str,
        *,
        connection_generation: int = 1,
        method: str = "item/commandExecution/requestApproval",
        params: dict | None = None,
    ) -> ServerRequestIdentity:
        return ServerRequestIdentity(
            request_id=request_id,
            connection_generation=connection_generation,
            method=method,
            params=(
                params
                if params is not None
                else {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "command": "pwd",
                }
            ),
        )

    @staticmethod
    def _unpack_card_response(response) -> dict:
        result: dict = {}
        if getattr(response, "card", None):
            result["card"] = response.card.data
        if getattr(response, "toast", None):
            result["toast"] = response.toast.content
            result["toast_type"] = response.toast.type
        return result

    def _make_controller(
        self,
        *,
        respond=None,
        interaction_actor_allowed=None,
        patch_message=None,
        resident_session_snapshot_locked=None,
        interactive_binding_for_thread=None,
        send_interactive_card=None,
        publish_interactive_card=None,
        revoke_response_authority=None,
        wall_clock=None,
        monotonic_clock=None,
        lock=None,
        resident_thread_id="thread-1",
    ):
        lock = lock or threading.RLock()
        session = SimpleNamespace(
            current_thread_id=resident_thread_id,
            execution=SimpleNamespace(
                current_prompt_message_id="prompt-1",
                current_prompt_reply_in_thread=True,
                current_actor_open_id="ou_actor",
            )
        )
        sent_cards: list[tuple[str, dict, str, bool]] = []
        replies: list[tuple[str, str, str, bool]] = []
        responses: list[tuple[object, dict | None, dict | None]] = []
        patches: list[tuple[str, dict]] = []
        normal_respond = respond or (
            lambda identity, *, result=None, error=None: responses.append(
                (identity.request_id, result, error)
            )
        )
        controller = InteractionRequestController(
            lock=lock,
            resident_session_snapshot_locked=(
                resident_session_snapshot_locked
                or (lambda _binding: session)
            ),
            interactive_binding_for_thread=(
                interactive_binding_for_thread
                or (
                    lambda thread_id: (
                        ("ou_user", "chat-1"),
                        False,
                    )
                )
            ),
            interaction_actor_allowed=interaction_actor_allowed or (lambda sender_id, chat_id, actor_open_id: True),
            send_interactive_card=(
                send_interactive_card
                or (
                    lambda chat_id, card, prompt_message_id, prompt_reply_in_thread: sent_cards.append(
                        (chat_id, card, prompt_message_id, prompt_reply_in_thread)
                    )
                    or "msg-card-1"
                )
            ),
            publish_interactive_card=publish_interactive_card,
            wall_clock=wall_clock or (lambda: 1_000.0),
            monotonic_clock=monotonic_clock or (lambda: 1_000.0),
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: replies.append(
                (chat_id, text, message_id, reply_in_thread)
            ),
            respond=normal_respond,
            revoke_response_authority=(
                revoke_response_authority or (lambda _identity: True)
            ),
            patch_message=patch_message
            or (
                lambda chat_id, message_id, content: patches.append(
                    (message_id, json.loads(content))
                )
                or True
            ),
        )
        return controller, sent_cards, replies, responses, patches

    def test_handle_adapter_request_registers_pending_request_and_routes_to_prompt_anchor(self) -> None:
        controller, sent_cards, _, _, _ = self._make_controller()
        identity = self._identity(
            "req-1",
            params={
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        controller.handle_adapter_request(identity)

        self.assertEqual(len(sent_cards), 1)
        self.assertEqual(sent_cards[0][0], "chat-1")
        self.assertEqual(sent_cards[0][2], "prompt-1")
        self.assertTrue(sent_cards[0][3])
        pending = controller.pending_request_snapshot(jsonrpc_id_key("req-1"))
        assert pending is not None
        self.assertIs(pending["identity"], identity)
        self.assertEqual(pending["thread_id"], "thread-1")
        self.assertEqual(pending["owner_thread_id"], "thread-1")
        self.assertEqual(pending["actor_open_id"], "ou_actor")
        self.assertEqual(pending["message_id"], "msg-card-1")

    def test_missing_exact_resident_session_fails_closed_without_card(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            resident_session_snapshot_locked=lambda _binding: None,
        )
        identity = self._identity(
            "req-missing-session",
            params={"threadId": "thread-1", "command": "pwd"},
        )

        self.assertTrue(controller.handle_adapter_request(identity))

        self.assertEqual(sent_cards, [])
        self.assertEqual(
            responses,
            [("req-missing-session", {"decision": "cancel"}, None)],
        )
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(pending["status"], "submitted")

    def test_active_observer_declines_replayed_request_without_answering(
        self,
    ) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            interactive_binding_for_thread=lambda _thread_id: (None, True),
        )
        identity = self._identity("observer-replay")

        handled = controller.handle_adapter_request(identity)

        self.assertFalse(handled)
        self.assertEqual(sent_cards, [])
        self.assertEqual(responses, [])
        self.assertIsNone(
            controller.pending_request_snapshot(identity.request_key)
        )

    def test_shared_approval_missing_feishu_session_declines_without_answering(
        self,
    ) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            resident_session_snapshot_locked=lambda _binding: None,
        )
        identity = self._identity("shared-no-session")

        handled = controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertFalse(handled)
        self.assertEqual(sent_cards, [])
        self.assertEqual(responses, [])
        self.assertIsNone(controller.pending_request_snapshot(identity.request_key))

    def test_shared_approval_requires_nonempty_turn(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller()
        identity = self._identity(
            "shared-missing-turn",
            params={"threadId": "thread-1", "command": "pwd"},
        )

        self.assertFalse(
            controller.handle_adapter_request(
                identity,
                routing_mode="shared_approval",
            )
        )

        self.assertEqual(sent_cards, [])
        self.assertEqual(responses, [])

    def test_shared_approval_card_send_failure_never_answers_for_other_surfaces(
        self,
    ) -> None:
        controller, _, _, responses, _ = self._make_controller(
            send_interactive_card=lambda *_args: None,
        )
        identity = self._identity("shared-card-send-failed")

        handled = controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        self.assertTrue(handled)
        self.assertEqual(responses, [])
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertIs(pending["identity"], identity)
        self.assertEqual(pending["status"], "not_sent")

    def test_shared_approval_reconciles_initial_unknown_only_after_resolution(
        self,
    ) -> None:
        publish_calls: list[tuple[tuple, dict]] = []
        outcomes = [
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="stable-approval-uuid",
                error_message="initial timeout",
            ),
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.CONFIRMED,
                destination_liveness=FeishuDestinationLiveness.REACHABLE,
                chat_id="chat-1",
                attempt_id="stable-approval-uuid",
                message_id="reconciled-card",
            ),
        ]

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return outcomes.pop(0)

        controller, _, _, responses, patches = self._make_controller(
            publish_interactive_card=publish,
        )
        identity = self._identity("shared-reconciled-card")

        handled = controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(len(publish_calls), 1)
        self.assertEqual(publish_calls[0][1], {})
        self.assertEqual(
            pending["shared_card_unknown_intent"].attempt_id,
            "stable-approval-uuid",
        )

        removed = controller.remove_resolved_server_request(identity)
        duplicate = controller.remove_resolved_server_request(identity)

        self.assertTrue(handled)
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(
            publish_calls[1][1],
            {"attempt_id": "stable-approval-uuid"},
        )
        self.assertEqual(publish_calls[0][0], publish_calls[1][0])
        self.assertEqual(removed.outcome, "removed")
        self.assertEqual(duplicate.outcome, "missing")
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(responses, [])
        self.assertEqual(patches[0][0], "reconciled-card")

    def test_shared_approval_unknown_create_reconciles_with_same_uuid(self) -> None:
        publish_calls: list[tuple[tuple, dict]] = []
        outcomes = [
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.CREATE_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="stable-create-uuid",
                error_message="initial timeout",
            ),
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.CREATE_MESSAGE,
                effect=FeishuOutboundEffect.CONFIRMED,
                destination_liveness=FeishuDestinationLiveness.REACHABLE,
                chat_id="chat-1",
                attempt_id="stable-create-uuid",
                message_id="reconciled-create-card",
            ),
        ]

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return outcomes.pop(0)

        controller, _, _, _, patches = self._make_controller(
            resident_session_snapshot_locked=lambda _binding: SimpleNamespace(
                current_thread_id="thread-1",
                execution=SimpleNamespace(
                    current_prompt_message_id="",
                    current_prompt_reply_in_thread=False,
                    current_actor_open_id="ou_actor",
                ),
            ),
            publish_interactive_card=publish,
        )
        identity = self._identity("shared-create-card")

        controller.handle_adapter_request(identity, routing_mode="shared_approval")
        controller.remove_resolved_server_request(identity)

        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(publish_calls[0][0][2:], ("", False))
        self.assertEqual(
            publish_calls[1][1],
            {"attempt_id": "stable-create-uuid"},
        )
        self.assertEqual(patches[0][0], "reconciled-create-card")

    def test_shared_approval_expired_unknown_card_is_never_replayed(self) -> None:
        now = [1_000.0]
        publish_calls: list[tuple[tuple, dict]] = []

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="expired-card-uuid",
                error_message="initial timeout",
            )

        controller, _, _, _, patches = self._make_controller(
            publish_interactive_card=publish,
            wall_clock=lambda: now[0],
            monotonic_clock=lambda: now[0],
        )
        identity = self._identity("shared-expired-card")
        controller.handle_adapter_request(identity, routing_mode="shared_approval")
        now[0] += 3_000.0

        controller.remove_resolved_server_request(identity)

        self.assertEqual(len(publish_calls), 1)
        self.assertEqual(patches, [])

    def test_shared_approval_reconciliation_rejects_effect_identity_drift(self) -> None:
        drift_cases = (
            (FeishuOutboundOperation.CREATE_MESSAGE, "chat-1", "stable-drift-uuid"),
            (FeishuOutboundOperation.REPLY_MESSAGE, "chat-other", "stable-drift-uuid"),
            (FeishuOutboundOperation.REPLY_MESSAGE, "chat-1", "different-uuid"),
        )
        for operation, chat_id, attempt_id in drift_cases:
            with self.subTest(
                operation=operation.value,
                chat_id=chat_id,
                attempt_id=attempt_id,
            ):
                publish_calls: list[tuple[tuple, dict]] = []

                def publish(*args, **kwargs):
                    publish_calls.append((args, kwargs))
                    if len(publish_calls) == 1:
                        return FeishuOutboundResult(
                            operation=FeishuOutboundOperation.REPLY_MESSAGE,
                            effect=FeishuOutboundEffect.UNKNOWN,
                            destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                            chat_id="chat-1",
                            attempt_id="stable-drift-uuid",
                            error_message="initial timeout",
                        )
                    return FeishuOutboundResult(
                        operation=operation,
                        effect=FeishuOutboundEffect.CONFIRMED,
                        destination_liveness=FeishuDestinationLiveness.REACHABLE,
                        chat_id=chat_id,
                        attempt_id=attempt_id,
                        message_id="drifted-card",
                    )

                controller, _, _, _, patches = self._make_controller(
                    publish_interactive_card=publish,
                )
                identity = self._identity(
                    f"shared-drift-{operation.value}-{chat_id}-{attempt_id}"
                )
                controller.handle_adapter_request(
                    identity,
                    routing_mode="shared_approval",
                )

                controller.remove_resolved_server_request(identity)
                controller.remove_resolved_server_request(identity)

                self.assertEqual(len(publish_calls), 2)
                self.assertEqual(patches, [])

    def test_shared_approval_reconciliation_rejected_stops_once(self) -> None:
        publish_calls: list[tuple[tuple, dict]] = []

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=(
                    FeishuOutboundEffect.UNKNOWN
                    if len(publish_calls) == 1
                    else FeishuOutboundEffect.REJECTED
                ),
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="stable-rejected-uuid",
                error_message="rejected" if len(publish_calls) == 2 else "timeout",
            )

        controller, _, _, _, patches = self._make_controller(
            publish_interactive_card=publish,
        )
        identity = self._identity("shared-rejected-card")
        controller.handle_adapter_request(identity, routing_mode="shared_approval")

        controller.remove_resolved_server_request(identity)
        controller.remove_resolved_server_request(identity)

        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(patches, [])

    def test_shared_approval_second_unknown_stops_after_resolution_without_answering(
        self,
    ) -> None:
        publish_calls: list[tuple[tuple, dict]] = []

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="stable-unknown-uuid",
                error_message="timeout after exact reconciliation",
            )

        controller, _, _, responses, patches = self._make_controller(
            publish_interactive_card=publish,
        )
        identity = self._identity("shared-still-unknown")

        handled = controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertTrue(handled)
        self.assertEqual(len(publish_calls), 1)
        self.assertEqual(pending["message_id"], "")
        self.assertEqual(pending["status"], "not_sent")

        removed = controller.remove_resolved_server_request(identity)

        self.assertEqual(removed.outcome, "removed")
        self.assertIsNone(controller.pending_request_snapshot(identity.request_key))
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(
            publish_calls[1][1],
            {"attempt_id": "stable-unknown-uuid"},
        )
        self.assertEqual(patches, [])
        self.assertEqual(responses, [])

    def test_mismatched_exact_resident_thread_fails_closed_without_card(
        self,
    ) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            resident_thread_id="thread-replaced",
        )
        identity = self._identity(
            "req-stale-session",
            params={"threadId": "thread-1", "command": "pwd"},
        )

        self.assertTrue(controller.handle_adapter_request(identity))

        self.assertEqual(sent_cards, [])
        self.assertEqual(
            responses,
            [("req-stale-session", {"decision": "cancel"}, None)],
        )

    def test_adapter_request_uses_exact_p2p_session_when_group_coexists(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=ChatBindingStore(data_dir),
        )
        self.assertIsInstance(manager, BindingRuntimeManager)
        p2p_binding = ("ou_user", "chat-shared")
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "chat-shared")
        with lock:
            p2p_state = manager._get_or_create_runtime_state_locked(p2p_binding)
            p2p_state["current_prompt_message_id"] = "p2p-prompt"
            p2p_state["current_prompt_reply_in_thread"] = True
            p2p_state["current_actor_open_id"] = "p2p-actor"
            p2p_state["current_thread_id"] = "thread-1"
            group_state = manager._get_or_create_runtime_state_locked(group_binding)
            group_state["current_prompt_message_id"] = "group-prompt"
            group_state["current_prompt_reply_in_thread"] = False
            group_state["current_actor_open_id"] = "group-actor"
            group_state["current_thread_id"] = "thread-1"

        self.assertEqual(
            manager.resolve_session(*p2p_binding).binding,
            group_binding,
        )
        controller, sent_cards, _, _, _ = self._make_controller(
            lock=lock,
            resident_session_snapshot_locked=(
                manager.resident_session_snapshot_locked
            ),
            interactive_binding_for_thread=(
                lambda _thread_id: (p2p_binding, False)
            ),
        )
        identity = self._identity(
            "req-exact-p2p",
            params={"threadId": "thread-1", "command": "pwd"},
        )

        self.assertTrue(controller.handle_adapter_request(identity))

        self.assertEqual(len(sent_cards), 1)
        self.assertEqual(sent_cards[0][2], "p2p-prompt")
        self.assertTrue(sent_cards[0][3])
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(pending["sender_id"], p2p_binding[0])
        self.assertEqual(pending["actor_open_id"], "p2p-actor")

    def test_exact_identity_replay_does_not_present_a_second_card(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller()
        identity = self._identity("req-replay")

        first = controller.handle_adapter_request(identity)
        replay = controller.handle_adapter_request(identity)

        self.assertTrue(first)
        self.assertTrue(replay)
        self.assertEqual(len(sent_cards), 1)
        self.assertEqual(responses, [])
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertIs(pending["identity"], identity)

    def test_distinct_capability_cannot_replace_unknown_feishu_pending(self) -> None:
        response_attempts: list[int | str] = []

        def lose_response(identity, *, result=None, error=None):
            del result
            del error
            self.assertEqual(identity.connection_generation, 1)
            response_attempts.append(identity.request_id)
            raise CodexRpcTransportError(
                "server-response",
                {"message": "disconnected"},
            )

        controller, sent_cards, _, _, _ = self._make_controller(
            respond=lose_response
        )
        original = self._identity("req-aba")
        value_equal_replacement = self._identity("req-aba")
        changed_replacement = self._identity(
            "req-aba",
            method="item/fileChange/requestApproval",
            params={
                "threadId": "thread-2",
                "turnId": "turn-2",
                "reason": "different envelope",
            },
        )

        self.assertTrue(controller.handle_adapter_request(original))
        original_pending = controller.pending_request_snapshot(original.request_key)
        assert original_pending is not None
        response_capability = str(original_pending["response_capability"])
        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": original.request_key,
                    "connection_generation": original.connection_generation,
                    "response_capability": response_capability,
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("结果未知", response["toast"])

        with self.assertRaises(ServerRequestSurfaceIdentityConflict):
            controller.handle_adapter_request(value_equal_replacement)
        with self.assertRaises(ServerRequestSurfaceIdentityConflict):
            controller.handle_adapter_request(changed_replacement)
        self.assertTrue(controller.handle_adapter_request(original))

        pending = controller.pending_request_snapshot(original.request_key)
        assert pending is not None
        self.assertIs(pending["identity"], original)
        self.assertEqual(pending["method"], original.method)
        self.assertEqual(pending["params"], original.params)
        self.assertEqual(pending["status"], "submitted_unknown")
        self.assertEqual(len(sent_cards), 1)
        self.assertEqual(response_attempts, [original.request_id])

        replayed_action = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": original.request_key,
                    "connection_generation": original.connection_generation,
                    "response_capability": response_capability,
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )
        self.assertEqual(replayed_action["toast_type"], "warning")
        self.assertEqual(response_attempts, [original.request_id])

    def test_canonical_auto_reject_preserves_unknown_identity_across_aba(self) -> None:
        response_attempts: list[int | str] = []

        def lose_response(identity, *, result=None, error=None):
            del result
            del error
            self.assertEqual(identity.connection_generation, 1)
            response_attempts.append(identity.request_id)
            raise CodexRpcTransportError(
                "server-response",
                {"message": "disconnected"},
            )

        controller, sent_cards, _, _, _ = self._make_controller(
            respond=lose_response
        )
        original = self._identity("req-auto-aba")
        replacement = self._identity("req-auto-aba")

        self.assertTrue(controller.auto_reject_server_request(original))
        with self.assertRaises(ServerRequestSurfaceIdentityConflict):
            controller.auto_reject_server_request(replacement)
        self.assertTrue(controller.auto_reject_server_request(original))

        pending = controller.pending_request_snapshot(original.request_key)
        assert pending is not None
        self.assertIs(pending["identity"], original)
        self.assertEqual(pending["status"], "submitted_unknown")
        self.assertEqual(response_attempts, [original.request_id])
        self.assertEqual(sent_cards, [])

    def test_legacy_approval_action_has_no_response_authority(self) -> None:
        controller, _, _, responses, _ = self._make_controller()
        identity = self._identity("req-legacy")
        controller.store_pending_request(
            identity.request_key,
            {
                "identity": identity,
                "response_capability": "capability-legacy",
                "rpc_request_id": "req-legacy",
                "method": "item/commandExecution/requestApproval",
                "params": {},
                "title": "Codex 命令执行审批",
                "status": "not_sent",
            },
        )

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": identity.request_key,
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-legacy",
                    "action": "command_allow_once",
                }
            )
        )

        self.assertEqual(responses, [])
        self.assertEqual(response["toast_type"], "warning")
        self.assertEqual(response["toast"], "未知审批动作")
        self.assertEqual(
            controller.pending_request_snapshot(identity.request_key)["status"],
            "not_sent",
        )

    def test_handle_approval_card_action_retains_request_until_exact_resolution(self) -> None:
        controller, _, _, responses, _ = self._make_controller()
        identity = self._identity("req-1")
        controller.store_pending_request(jsonrpc_id_key("req-1"), {
            "identity": identity,
            "response_capability": "capability-1",
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "title": "Codex 命令执行审批",
            "questions": [],
            "answers": {},
            "thread_id": "thread-1",
            "status": "pending",
        })

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": jsonrpc_id_key("req-1"),
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )

        self.assertEqual(responses, [("req-1", {"decision": "accept"}, None)])
        pending = controller.pending_request_snapshot(jsonrpc_id_key("req-1"))
        assert pending is not None
        self.assertEqual(pending["status"], "submitted")
        self.assertEqual(response["toast_type"], "success")
        self.assertEqual(response["toast"], "已允许本次")
        controller.remove_resolved_server_request(identity)
        self.assertFalse(controller.has_pending_request(jsonrpc_id_key("req-1")))

    def test_handle_user_input_action_updates_card_then_submits_final_answers(self) -> None:
        controller, _, _, responses, _ = self._make_controller()
        identity = self._identity("req-1", method="item/tool/requestUserInput")
        controller.store_pending_request(jsonrpc_id_key("req-1"), {
            "identity": identity,
            "response_capability": "capability-1",
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "questions": [
                {
                    "id": "q1",
                    "header": "第一题",
                    "question": "Q1",
                    "options": [{"label": "A", "description": ""}],
                    "isOther": False,
                },
                {
                    "id": "q2",
                    "header": "第二题",
                    "question": "Q2",
                    "options": [],
                    "isOther": True,
                },
            ],
            "answers": {},
            "thread_id": "thread-1",
            "status": "pending",
        })

        first = self._unpack_card_response(
            controller.handle_user_input_action(
                {
                    "request_id": jsonrpc_id_key("req-1"),
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "answer_user_input_option",
                    "question_id": "q1",
                    "answer": "A",
                }
            )
        )
        self.assertEqual(first["toast"], "已记录，继续回答下一题。")
        pending_after_first = controller.pending_request_snapshot(jsonrpc_id_key("req-1"))
        assert pending_after_first is not None
        self.assertEqual(pending_after_first["answers"], {"q1": "A"})

        second = self._unpack_card_response(
            controller.handle_user_input_action(
                {
                    "request_id": jsonrpc_id_key("req-1"),
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "answer_user_input_custom",
                    "question_id": "q2",
                    "_form_value": {"user_input_q2": "custom"},
                }
            )
        )
        self.assertEqual(
            responses,
            [("req-1", {"answers": {"q1": {"answers": ["A"]}, "q2": {"answers": ["custom"]}}}, None)],
        )
        pending = controller.pending_request_snapshot(jsonrpc_id_key("req-1"))
        assert pending is not None
        self.assertEqual(pending["status"], "submitted")
        self.assertEqual(second["toast"], "已提交回答。")
        controller.remove_resolved_server_request(identity)
        self.assertFalse(controller.has_pending_request(jsonrpc_id_key("req-1")))

    def test_user_input_auto_resolution_submits_empty_answers(self) -> None:
        controller, _, _, responses, patches = self._make_controller()
        identity = self._identity("req-1", method="item/tool/requestUserInput")
        controller.store_pending_request(jsonrpc_id_key("req-1"), {
            "identity": identity,
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "params": {
                "questions": [
                    {
                        "id": "q1",
                        "header": "Optional",
                        "question": "Add context?",
                        "options": [],
                    }
                ],
            },
            "title": "Codex 用户输入",
            "message_id": "msg-card-1",
            "thread_id": "thread-1",
            "status": "not_sent",
            "auto_resolution_backend_epoch": 3,
            "auto_resolution_generation": 9,
        })

        handled = controller.auto_resolve_request(jsonrpc_id_key("req-1"), 3, 9)

        self.assertTrue(handled)
        self.assertEqual(responses, [("req-1", {"answers": {}}, None)])
        pending = controller.pending_request_snapshot(jsonrpc_id_key("req-1"))
        assert pending is not None
        self.assertEqual(pending["status"], "submitted")
        self.assertEqual(patches[0][0], "msg-card-1")
        self.assertIn("提交空答案", patches[0][1]["elements"][0]["content"])
        controller.remove_resolved_server_request(identity)
        self.assertFalse(controller.has_pending_request(jsonrpc_id_key("req-1")))

    def test_remove_resolved_server_request_patches_handled_elsewhere_card(self) -> None:
        identity = self._identity(
            "req-1",
            method="item/tool/requestUserInput",
            params={"threadId": "thread-child"},
        )
        request_key = identity.request_key
        controller, _, _, _, patches = self._make_controller()
        controller.store_pending_request(request_key, {
            "identity": identity,
            "method": "item/tool/requestUserInput",
            "title": "Codex 用户输入",
            "message_id": "msg-card-1",
            "thread_id": "thread-child",
            "owner_thread_id": "thread-child",
        })

        resolution = controller.remove_resolved_server_request(identity)

        self.assertFalse(controller.has_pending_request(request_key))
        self.assertEqual(resolution.outcome, "removed")
        self.assertEqual(resolution.request_key, request_key)
        self.assertEqual(resolution.thread_id, "thread-child")
        self.assertEqual(resolution.root_thread_id, "thread-child")
        self.assertEqual(patches[0][0], "msg-card-1")
        self.assertIn("其他终端处理", patches[0][1]["elements"][0]["content"])

    def test_remove_resolved_server_request_preserves_mismatched_thread_projection(self) -> None:
        current = self._identity("req-1", params={"threadId": "thread-current"})
        request_key = current.request_key
        controller, _, _, _, patches = self._make_controller()
        controller.store_pending_request(
            request_key,
            {
                "identity": current,
                "method": "item/tool/requestUserInput",
                "message_id": "msg-card-1",
                "thread_id": "thread-current",
                "owner_thread_id": "root-current",
            },
        )

        stale = self._identity(
            "req-1",
            params={"threadId": "thread-stale-notification"},
        )
        resolution = controller.remove_resolved_server_request(stale)

        self.assertEqual(resolution.outcome, "mismatch")
        self.assertEqual(resolution.request_key, request_key)
        self.assertEqual(resolution.thread_id, "thread-stale-notification")
        self.assertEqual(resolution.root_thread_id, "")
        self.assertTrue(controller.has_pending_request(request_key))
        self.assertEqual(patches, [])

    def test_remove_resolved_server_request_reports_invalid_missing_and_submitted(self) -> None:
        controller, _, _, _, patches = self._make_controller()
        malformed_identity = self._identity(
            "malformed", params={"threadId": " "}
        )
        malformed_key = malformed_identity.request_key
        controller.store_pending_request(
            malformed_key,
            {"identity": malformed_identity, "thread_id": "thread-1"},
        )

        invalid = controller.remove_resolved_server_request(None)
        malformed_thread = controller.remove_resolved_server_request(
            malformed_identity
        )
        missing_identity = self._identity("missing")
        missing = controller.remove_resolved_server_request(missing_identity)
        submitted_identity = self._identity(
            "submitted", params={"threadId": "thread-fallback"}
        )
        controller.store_pending_request(
            submitted_identity.request_key,
            {
                "identity": submitted_identity,
                "status": "submitted",
                "message_id": "must-not-be-patched",
                "owner_thread_id": "",
                "thread_id": "thread-fallback",
            },
        )
        submitted = controller.remove_resolved_server_request(submitted_identity)

        self.assertEqual(invalid.outcome, "invalid")
        self.assertEqual(invalid.request_key, "")
        self.assertEqual(malformed_thread.outcome, "invalid")
        self.assertTrue(controller.has_pending_request(malformed_key))
        self.assertEqual(missing.outcome, "missing")
        self.assertEqual(missing.request_key, jsonrpc_id_key("missing"))
        self.assertEqual(submitted.outcome, "removed")
        self.assertEqual(submitted.root_thread_id, "")
        self.assertEqual(patches, [])

    def test_fail_close_chat_requests_auto_rejects_matching_chat_only(self) -> None:
        controller, _, _, responses, _ = self._make_controller()
        controller.store_pending_request("req-1", {
            "identity": self._identity("rpc-1"),
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
            "chat_id": "chat-1",
        })
        controller.store_pending_request("req-2", {
            "identity": self._identity("rpc-2"),
            "rpc_request_id": "rpc-2",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-2"},
            "chat_id": "chat-2",
        })

        closed = controller.fail_close_chat_requests("chat-1")

        self.assertEqual(closed, 1)
        self.assertEqual(controller.pending_request_snapshot("req-1")["status"], "submitted")
        self.assertTrue(controller.has_pending_request("req-2"))
        self.assertEqual(responses, [("rpc-1", {"decision": "cancel"}, None)])

    def test_destination_loss_retires_shared_projection_without_answering(self) -> None:
        controller, _, _, responses, patches = self._make_controller()
        identity = self._identity("shared-destination-loss")
        controller.store_pending_request(
            identity.request_key,
            {
                "identity": identity,
                "rpc_request_id": identity.request_id,
                "method": identity.method,
                "params": identity.params,
                "chat_id": "chat-1",
                "message_id": "shared-card",
                "shared_approval": True,
            },
        )

        closed = controller.fail_close_chat_requests("chat-1")

        self.assertEqual(closed, 1)
        self.assertIsNone(controller.pending_request_snapshot(identity.request_key))
        self.assertEqual(responses, [])
        self.assertEqual(patches[0][0], "shared-card")
        self.assertIn("其他可信终端", patches[0][1]["elements"][0]["content"])

    def test_fail_close_non_admin_chat_requests_preserves_admin_requests(self) -> None:
        controller, _, _, responses, patches = self._make_controller()
        controller.store_pending_request("member", {
            "identity": self._identity("rpc-member"),
            "rpc_request_id": "rpc-member",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-member"},
            "chat_id": "chat-1",
            "actor_open_id": "ou_member",
            "message_id": "card-member",
        })
        controller.store_pending_request("admin", {
            "identity": self._identity("rpc-admin"),
            "rpc_request_id": "rpc-admin",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-admin"},
            "chat_id": "chat-1",
            "actor_open_id": "ou_admin",
            "message_id": "card-admin",
        })

        closed = controller.fail_close_non_admin_chat_requests(
            "chat-1",
            is_admin_actor=lambda open_id: open_id == "ou_admin",
        )

        self.assertEqual(closed, 1)
        self.assertEqual(controller.pending_request_snapshot("member")["status"], "submitted")
        self.assertTrue(controller.has_pending_request("admin"))
        self.assertEqual(responses, [("rpc-member", {"decision": "cancel"}, None)])
        self.assertEqual(patches[0][0], "card-member")
        self.assertIn("群聊已停用", patches[0][1]["elements"][0]["content"])

    def test_group_deactivation_fail_closes_member_shared_approval(self) -> None:
        revoked: list[ServerRequestIdentity] = []
        controller, _, _, responses, patches = self._make_controller(
            revoke_response_authority=lambda identity: revoked.append(identity) or True,
        )
        identity = self._identity("member-shared")
        controller.store_pending_request(
            identity.request_key,
            {
                "identity": identity,
                "rpc_request_id": identity.request_id,
                "method": identity.method,
                "params": identity.params,
                "chat_id": "chat-1",
                "actor_open_id": "ou_member",
                "message_id": "member-shared-card",
                "shared_approval": True,
            },
        )

        closed = controller.fail_close_non_admin_chat_requests(
            "chat-1",
            is_admin_actor=lambda _open_id: False,
        )

        self.assertEqual(closed, 1)
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(pending["status"], "submitted")
        self.assertTrue(pending["group_authority_revoked"])
        self.assertEqual(
            responses,
            [("member-shared", {"decision": "cancel"}, None)],
        )
        self.assertEqual(patches[0][0], "member-shared-card")
        self.assertIn("群聊已停用", patches[0][1]["elements"][0]["content"])
        self.assertEqual(revoked, [identity])

    def test_group_deactivation_revokes_shared_approval_after_pre_send_failure(
        self,
    ) -> None:
        revoked: list[ServerRequestIdentity] = []

        def respond(_identity, **_kwargs):
            raise CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before cancel send"),
            )

        controller, _, _, _, _ = self._make_controller(
            respond=respond,
            revoke_response_authority=lambda identity: revoked.append(identity) or True,
        )
        identity = self._identity("member-shared-not-sent")
        controller.store_pending_request(
            identity.request_key,
            {
                "identity": identity,
                "rpc_request_id": identity.request_id,
                "method": identity.method,
                "params": identity.params,
                "chat_id": "chat-1",
                "actor_open_id": "ou_member",
                "message_id": "member-shared-card",
                "shared_approval": True,
            },
        )

        controller.fail_close_non_admin_chat_requests(
            "chat-1",
            is_admin_actor=lambda _open_id: False,
        )

        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertEqual(pending["status"], "not_sent")
        self.assertTrue(pending["group_authority_revoked"])
        self.assertEqual(revoked, [identity])

    def test_inactive_group_shared_approval_is_cancelled_and_revoked_on_arrival(
        self,
    ) -> None:
        revoked: list[ServerRequestIdentity] = []
        controller, sent_cards, _, responses, _ = self._make_controller(
            interaction_actor_allowed=lambda *_args: False,
            revoke_response_authority=lambda identity: revoked.append(identity) or True,
        )
        identity = self._identity("inactive-shared-arrival")

        handled = controller.handle_adapter_request(
            identity,
            routing_mode="shared_approval",
        )

        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertTrue(handled)
        self.assertEqual(sent_cards, [])
        self.assertEqual(
            responses,
            [("inactive-shared-arrival", {"decision": "cancel"}, None)],
        )
        self.assertEqual(pending["status"], "submitted")
        self.assertTrue(pending["group_authority_revoked"])
        self.assertEqual(revoked, [identity])

    def test_unknown_shared_card_group_cancel_reconciles_and_patches_once(self) -> None:
        publish_calls: list[tuple[tuple, dict]] = []
        revoked: list[ServerRequestIdentity] = []
        outcomes = [
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="group-cancel-card-uuid",
                error_message="initial timeout",
            ),
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.CONFIRMED,
                destination_liveness=FeishuDestinationLiveness.REACHABLE,
                chat_id="chat-1",
                attempt_id="group-cancel-card-uuid",
                message_id="group-cancel-card",
            ),
        ]

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return outcomes.pop(0)

        controller, _, _, responses, patches = self._make_controller(
            publish_interactive_card=publish,
            revoke_response_authority=lambda identity: revoked.append(identity) or True,
        )
        identity = self._identity("group-cancel-unknown-card")
        controller.handle_adapter_request(identity, routing_mode="shared_approval")
        controller.fail_close_non_admin_chat_requests(
            "chat-1",
            is_admin_actor=lambda _open_id: False,
        )

        removed = controller.remove_resolved_server_request(identity)
        duplicate = controller.remove_resolved_server_request(identity)

        self.assertEqual(removed.outcome, "removed")
        self.assertEqual(duplicate.outcome, "missing")
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(
            publish_calls[1][1],
            {"attempt_id": "group-cancel-card-uuid"},
        )
        self.assertEqual(
            responses,
            [("group-cancel-unknown-card", {"decision": "cancel"}, None)],
        )
        self.assertEqual(revoked, [identity])
        self.assertEqual(patches[0][0], "group-cancel-card")
        self.assertIn("群聊已停用", patches[0][1]["elements"][0]["content"])

    def test_unknown_shared_card_survives_superseded_cancel_until_resolution(
        self,
    ) -> None:
        publish_calls: list[tuple[tuple, dict]] = []
        revoked: list[ServerRequestIdentity] = []
        outcomes = [
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id="chat-1",
                attempt_id="superseded-card-uuid",
                error_message="initial timeout",
            ),
            FeishuOutboundResult(
                operation=FeishuOutboundOperation.REPLY_MESSAGE,
                effect=FeishuOutboundEffect.CONFIRMED,
                destination_liveness=FeishuDestinationLiveness.REACHABLE,
                chat_id="chat-1",
                attempt_id="superseded-card-uuid",
                message_id="superseded-card",
            ),
        ]

        def publish(*args, **kwargs):
            publish_calls.append((args, kwargs))
            return outcomes.pop(0)

        identity = self._identity("group-cancel-superseded-card")

        def respond(_identity, **_kwargs):
            raise ServerRequestResponseSupersededError(
                ServerRequestResponseReport(
                    "superseded",
                    request_key=identity.request_key,
                    thread_id=identity.thread_id,
                )
            )

        controller, _, _, _, patches = self._make_controller(
            respond=respond,
            publish_interactive_card=publish,
            revoke_response_authority=lambda revoked_identity: revoked.append(
                revoked_identity
            )
            or True,
        )
        controller.handle_adapter_request(identity, routing_mode="shared_approval")

        pending_before = controller.pending_request_snapshot(identity.request_key)
        assert pending_before is not None
        stale_capability = pending_before["response_capability"]
        controller.fail_close_non_admin_chat_requests(
            "chat-1",
            is_admin_actor=lambda _open_id: False,
        )

        retained = controller.pending_request_snapshot(identity.request_key)
        assert retained is not None
        self.assertEqual(retained["status"], "superseded")
        self.assertEqual(retained["response_capability"], "")
        self.assertTrue(retained["group_authority_revoked"])
        self.assertEqual(revoked, [identity])
        self.assertEqual(len(publish_calls), 1)
        self.assertEqual(patches, [])
        stale_action = controller.handle_approval_card_action(
            {
                "request_id": identity.request_key,
                "connection_generation": identity.connection_generation,
                "response_capability": stale_capability,
                "action": "interaction_approval",
                "response_action": "approve_once",
            }
        )
        self.assertIn("失效", self._unpack_card_response(stale_action)["toast"])

        removed = controller.remove_resolved_server_request(identity)
        duplicate = controller.remove_resolved_server_request(identity)

        self.assertEqual(removed.outcome, "removed")
        self.assertEqual(duplicate.outcome, "missing")
        self.assertIsNone(controller.pending_request_snapshot(identity.request_key))
        self.assertEqual(len(publish_calls), 2)
        self.assertEqual(
            publish_calls[1][1],
            {"attempt_id": "superseded-card-uuid"},
        )
        self.assertEqual(patches[0][0], "superseded-card")
        self.assertEqual(len(patches), 1)
        self.assertIn("其他端处理", patches[0][1]["elements"][0]["content"])

    def test_deactivated_group_actor_request_is_rejected_without_sending_a_card(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            interaction_actor_allowed=lambda sender_id, chat_id, actor_open_id: False,
        )

        identity = self._identity(
            "req-1",
            params={"threadId": "thread-1", "command": "pwd"},
        )
        controller.handle_adapter_request(identity)

        self.assertEqual(sent_cards, [])
        self.assertEqual(responses, [("req-1", {"decision": "cancel"}, None)])
        pending = controller.pending_request_snapshot(identity.request_key)
        assert pending is not None
        self.assertIs(pending["identity"], identity)
        self.assertEqual(pending["status"], "submitted")

    def test_unknown_child_request_fail_closes_exact_identity_without_root_binding(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller(
            interactive_binding_for_thread=lambda _thread_id: (None, False),
        )

        identity = self._identity(
            "req-child",
            params={
                "threadId": "thread-child",
                "turnId": "turn-child",
                "command": "pwd",
                "availableDecisions": ["accept", "cancel"],
            },
        )
        handled = controller.handle_adapter_request(identity)

        self.assertTrue(handled)
        self.assertEqual(sent_cards, [])
        self.assertEqual(responses, [("req-child", {"decision": "cancel"}, None)])
        pending = controller.pending_request_snapshot(jsonrpc_id_key("req-child"))
        assert pending is not None
        self.assertEqual(pending["thread_id"], "thread-child")
        self.assertEqual(pending["owner_thread_id"], "thread-child")
        self.assertEqual(pending["status"], "submitted")

    def test_numeric_and_string_server_request_ids_use_distinct_card_tokens_and_resolve_independently(self) -> None:
        controller, sent_cards, _, responses, _ = self._make_controller()
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "command": "pwd",
        }

        numeric_identity = self._identity(1, params=params)
        textual_identity = self._identity("1", params=params)
        controller.handle_adapter_request(numeric_identity)
        controller.handle_adapter_request(textual_identity)

        numeric_key = jsonrpc_id_key(1)
        textual_key = jsonrpc_id_key("1")
        self.assertEqual(len(sent_cards), 2)
        self.assertNotEqual(numeric_key, textual_key)
        self.assertIsNotNone(controller.pending_request_snapshot(numeric_key))
        self.assertIsNotNone(controller.pending_request_snapshot(textual_key))
        self.assertIs(
            controller.pending_request_snapshot(numeric_key)["identity"],
            numeric_identity,
        )
        self.assertIs(
            controller.pending_request_snapshot(textual_key)["identity"],
            textual_identity,
        )

        controller.remove_resolved_server_request(numeric_identity)

        self.assertIsNone(controller.pending_request_snapshot(numeric_key))
        self.assertIsNotNone(controller.pending_request_snapshot(textual_key))
        pending = controller.pending_request_snapshot(textual_key)
        assert pending is not None
        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": textual_key,
                    "connection_generation": textual_identity.connection_generation,
                    "response_capability": pending["response_capability"],
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )
        self.assertEqual(responses, [("1", {"decision": "accept"}, None)])
        self.assertEqual(response["toast_type"], "success")

    def test_fail_close_without_response_retains_unknown_requests(self) -> None:
        controller, _, _, responses, patches = self._make_controller()
        controller.store_pending_request("req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1"},
            "title": "Codex 命令执行审批",
            "message_id": "msg-card-1",
            "chat_id": "chat-1",
            "sender_id": "ou_user",
            "thread_id": "thread-1",
        })
        controller.store_pending_request("req-2", {
            "rpc_request_id": "rpc-2",
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-2"},
            "title": "Codex 用户输入",
            "message_id": "msg-card-2",
            "chat_id": "chat-2",
            "sender_id": "ou_other",
            "thread_id": "thread-2",
        })

        closed = controller.fail_close_all_requests_without_response(
            note="当前实例与 Codex backend 的 websocket 已断开，已自动结束该请求。",
        )

        self.assertEqual(closed, 2)
        self.assertEqual(
            controller.pending_request_snapshot("req-1")["status"],
            "submitted_unknown",
        )
        self.assertEqual(
            controller.pending_request_snapshot("req-2")["status"],
            "submitted_unknown",
        )
        self.assertEqual(responses, [])
        self.assertEqual(patches, [])

    def test_approval_pre_send_failure_keeps_retryable_request(self) -> None:
        def fail_before_send(_identity, *, result=None, error=None):
            del result, error
            raise CodexRpcPreSendError("server-response", RuntimeError("offline"))

        controller, _, _, _, _ = self._make_controller(respond=fail_before_send)
        identity = self._identity("rpc-1")
        controller.store_pending_request(
            "req-1",
            {
                "identity": identity,
                "response_capability": "capability-1",
                "rpc_request_id": "rpc-1",
                "method": "item/commandExecution/requestApproval",
                "params": {},
                "title": "Codex 命令执行审批",
                "status": "not_sent",
            },
        )

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": "req-1",
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )

        self.assertEqual(controller.pending_request_snapshot("req-1")["status"], "not_sent")
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("未发送", response["toast"])

    def test_approval_transport_failure_keeps_unknown_request(self) -> None:
        def lose_response(_identity, *, result=None, error=None):
            del result, error
            raise CodexRpcTransportError("server-response", {"message": "disconnected"})

        controller, _, _, _, _ = self._make_controller(respond=lose_response)
        identity = self._identity("rpc-1")
        controller.store_pending_request(
            "req-1",
            {
                "identity": identity,
                "response_capability": "capability-1",
                "rpc_request_id": "rpc-1",
                "method": "item/commandExecution/requestApproval",
                "params": {},
                "title": "Codex 命令执行审批",
                "status": "not_sent",
            },
        )

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": "req-1",
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )

        self.assertEqual(
            controller.pending_request_snapshot("req-1")["status"],
            "submitted_unknown",
        )
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("结果未知", response["toast"])

    def test_approval_superseded_by_another_surface_retires_card_capability(
        self,
    ) -> None:
        def superseded(_identity, *, result=None, error=None):
            del result, error
            raise ServerRequestResponseSupersededError(
                ServerRequestResponseReport(
                    "superseded",
                    request_key="req-1",
                    thread_id="thread-1",
                )
            )

        controller, _, _, _, _ = self._make_controller(respond=superseded)
        identity = self._identity("rpc-1")
        controller.store_pending_request(
            "req-1",
            {
                "identity": identity,
                "response_capability": "capability-1",
                "rpc_request_id": "rpc-1",
                "method": "item/commandExecution/requestApproval",
                "params": {},
                "title": "Codex 命令执行审批",
                "status": "not_sent",
            },
        )

        response = self._unpack_card_response(
            controller.handle_approval_card_action(
                {
                    "request_id": "req-1",
                    "connection_generation": identity.connection_generation,
                    "response_capability": "capability-1",
                    "action": "interaction_approval",
                    "response_action": "approve_once",
                }
            )
        )

        self.assertIsNone(controller.pending_request_snapshot("req-1"))
        self.assertEqual(response["toast_type"], "warning")
        self.assertIn("其他端处理或失效", response["toast"])

    def test_batch_fail_close_keeps_submitted_items_until_exact_resolution(self) -> None:
        responses: list[str] = []

        def respond(identity, *, result=None, error=None):
            del result, error
            self.assertEqual(identity.connection_generation, 1)
            responses.append(str(identity.request_id))
            if identity.request_id == "rpc-2":
                raise CodexRpcPreSendError("server-response", RuntimeError("offline"))

        controller, _, _, _, patches = self._make_controller(respond=respond)
        for index in (1, 2):
            controller.store_pending_request(
                f"req-{index}",
                {
                    "identity": self._identity(f"rpc-{index}"),
                    "rpc_request_id": f"rpc-{index}",
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": f"thread-{index}"},
                    "title": "Codex 命令执行审批",
                    "message_id": f"msg-{index}",
                    "chat_id": "chat-1",
                    "sender_id": "ou_user",
                    "thread_id": f"thread-{index}",
                },
            )

        self.assertEqual(controller.fail_close_chat_requests("chat-1"), 2)

        self.assertEqual(responses, ["rpc-1", "rpc-2"])
        self.assertEqual(controller.pending_request_snapshot("req-1")["status"], "submitted")
        self.assertEqual(controller.pending_request_snapshot("req-2")["status"], "not_sent")
        self.assertEqual([message_id for message_id, _card in patches], ["msg-1"])
