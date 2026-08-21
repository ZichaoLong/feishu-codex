from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

from bot.feishu_bot import FeishuBot
from bot.interaction_request_controller import InteractionRequestController
from bot.server_request_contract import ServerRequestIdentity
from bot.server_request_registry import ServerRequestRegistry
from bot.system_config import SystemConfig
from bot.web_runtime.interaction_inbox import (
    WebInteractionInbox,
    WebInteractionInboxError,
    WebInteractionInboxPorts,
)


_APPROVAL = "item/commandExecution/requestApproval"
_PARAMS = {
    "threadId": "thread-1",
    "turnId": "turn-1",
    "command": "pwd",
}


def _identity(request_id: str = "same-id") -> ServerRequestIdentity:
    return ServerRequestIdentity(
        request_id=request_id,
        connection_generation=1,
        method=_APPROVAL,
        params=_PARAMS,
    )


def _card_action_values(card: object) -> list[dict]:
    values: list[dict] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            action_value = value.get("value")
            if isinstance(action_value, dict) and "request_id" in action_value:
                values.append(dict(action_value))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(card)
    return values


def _toast_type(response: object) -> str:
    toast = getattr(response, "toast", None)
    return str(getattr(toast, "type", "") or "")


class WebResponseCapabilityTests(unittest.TestCase):
    @staticmethod
    def _inbox() -> tuple[
        WebInteractionInbox,
        ServerRequestRegistry,
        list[tuple[object, dict]],
    ]:
        ledger = ServerRequestRegistry(resolved_limit=16)
        ledger.activate_connection_epoch(1)
        responses: list[tuple[object, dict]] = []

        def respond(identity, *, result=None, error=None):
            responses.append(
                (
                    identity.request_id,
                    {
                        "result": result,
                        "error": error,
                        "connection_generation": identity.connection_generation,
                    },
                )
            )

        inbox = WebInteractionInbox(
            ports=WebInteractionInboxPorts(
                respond=respond,
                active_matches=ledger.active_matches,
            ),
            runtime_context_guard=lambda: None,
        )
        return inbox, ledger, responses

    def _present(
        self,
        inbox: WebInteractionInbox,
        ledger: ServerRequestRegistry,
    ) -> ServerRequestIdentity:
        candidate = _identity()
        claim = ledger.register(candidate)
        assert claim.identity is not None
        identity = claim.identity
        ingress = inbox.prepare_ingress(identity)
        self.assertEqual(ingress.disposition, "route")
        inbox.present(ingress, owner_thread_id="thread-1", client_id="tab-1")
        return identity

    def test_service_restart_rejects_old_browser_capability_before_response(self) -> None:
        old_inbox, old_ledger, old_responses = self._inbox()
        old_identity = self._present(old_inbox, old_ledger)
        old_snapshot = old_inbox.snapshot(old_identity.request_key)
        assert old_snapshot is not None
        old_projection = old_snapshot.projection_dict()
        self.assertEqual(old_projection["connection_generation"], 1)
        self.assertTrue(old_projection["response_capability"])

        retirement = old_inbox.retire_backend_epoch_after_stop()
        self.assertEqual(retirement.count, 1)
        self.assertEqual(old_inbox.retire_backend_epoch_after_stop().count, 0)
        self.assertEqual(old_responses, [])

        # A complete Focus restart can reuse both app-server request id and
        # connection generation. The new surface-issued nonce must not match.
        new_inbox, new_ledger, new_responses = self._inbox()
        new_identity = self._present(new_inbox, new_ledger)
        new_snapshot = new_inbox.snapshot(new_identity.request_key)
        assert new_snapshot is not None
        self.assertNotEqual(
            old_projection["response_capability"],
            new_snapshot.response_capability,
        )

        with self.assertRaises(WebInteractionInboxError) as stale:
            new_inbox.prepare_response(
                "tab-1",
                new_identity.request_key,
                old_projection["connection_generation"],
                old_projection["response_capability"],
            )
        self.assertEqual(stale.exception.code, "response_capability_mismatch")
        self.assertEqual(new_responses, [])

        preparation = new_inbox.prepare_response(
            "tab-1",
            new_identity.request_key,
            new_snapshot.connection_generation,
            new_snapshot.response_capability,
        )
        new_inbox.submit_response(
            preparation,
            action="approve_once",
        )
        self.assertEqual(new_responses[0][1]["connection_generation"], 1)

    def test_missing_or_wrong_web_action_coordinates_fail_closed(self) -> None:
        inbox, ledger, responses = self._inbox()
        identity = self._present(inbox, ledger)
        snapshot = inbox.snapshot(identity.request_key)
        assert snapshot is not None
        cases = (
            (0, snapshot.response_capability, "invalid_request_generation"),
            (1, "", "invalid_response_capability"),
            (1, "old-capability", "response_capability_mismatch"),
        )
        for generation, capability, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(WebInteractionInboxError) as caught:
                    inbox.prepare_response(
                        "tab-1",
                        identity.request_key,
                        generation,
                        capability,
                    )
                self.assertEqual(caught.exception.code, expected_code)
        self.assertEqual(responses, [])


class _FeishuControllerFactory:
    def __init__(self, patch_message=None) -> None:
        self.sent_cards: list[dict] = []
        self.responses: list[dict] = []
        self.patch_message = patch_message or (
            lambda _chat_id, _message_id, _content: True
        )

    def make(self) -> InteractionRequestController:
        def send_card(_chat_id, card, _prompt_id, _reply_in_thread):
            self.sent_cards.append(card)
            return f"message-{len(self.sent_cards)}"

        def respond(identity, *, result=None, error=None):
            self.responses.append(
                {
                    "request_id": identity.request_id,
                    "result": result,
                    "error": error,
                    "connection_generation": identity.connection_generation,
                }
            )

        return InteractionRequestController(
            lock=threading.RLock(),
            resident_session_snapshot_locked=lambda _binding: SimpleNamespace(
                current_thread_id="thread-1",
                execution=SimpleNamespace(
                    current_prompt_message_id="prompt-1",
                    current_prompt_reply_in_thread=True,
                    current_actor_open_id="ou-user",
                ),
            ),
            interactive_binding_for_thread=lambda _thread: (
                ("ou-user", "chat-1"),
                False,
            ),
            interaction_actor_allowed=lambda _sender, _chat, _actor: True,
            send_interactive_card=send_card,
            reply_text=lambda *_args, **_kwargs: None,
            respond=respond,
            revoke_response_authority=lambda _identity: True,
            patch_message=self.patch_message,
        )


class _RawActionBot(FeishuBot):
    def __init__(
        self,
        data_dir: pathlib.Path,
        controller: InteractionRequestController,
    ) -> None:
        super().__init__(
            data_dir=data_dir,
            system_config=SystemConfig.from_dict(
                {
                    "app_id": "app-id",
                    "app_secret": "app-secret",
                    "admin_open_ids": ["ou-admin"],
                    "bot_open_id": "ou-bot",
                }
            ),
        )
        self._controller = controller
        self.ingress_values: list[dict] = []

    def on_message(self, _sender, _chat, _text, message_id="") -> None:
        del message_id

    def on_card_action(self, _sender, _chat, _message, action_value):
        self.ingress_values.append(dict(action_value))
        return self._controller.handle_approval_card_action(action_value)


def _raw_action(value: dict) -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(user_id="user-1", open_id="ou-user"),
            context=SimpleNamespace(
                open_chat_id="chat-1",
                open_message_id="message-old",
            ),
            action=SimpleNamespace(value=dict(value), form_value={}),
        )
    )


class FeishuResponseCapabilityTests(unittest.TestCase):
    def test_raw_card_ingress_rejects_pre_restart_capability(self) -> None:
        old_factory = _FeishuControllerFactory()
        old = old_factory.make()
        old.handle_adapter_request(_identity())
        old_action = _card_action_values(old_factory.sent_cards[0])[0]
        self.assertEqual(old_action["connection_generation"], 1)
        self.assertTrue(old_action["response_capability"])
        self.assertEqual(old.retire_backend_epoch_after_stop().count, 1)

        new_factory = _FeishuControllerFactory()
        new = new_factory.make()
        new.handle_adapter_request(_identity())
        new_action = _card_action_values(new_factory.sent_cards[0])[0]
        self.assertNotEqual(
            old_action["response_capability"],
            new_action["response_capability"],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            bot = _RawActionBot(pathlib.Path(temp_dir), new)
            stale = bot._on_raw_card_action(_raw_action(old_action))
            self.assertEqual(_toast_type(stale), "warning")
            self.assertEqual(new_factory.responses, [])
            current = bot._on_raw_card_action(_raw_action(new_action))

        self.assertEqual(_toast_type(current), "success")
        self.assertEqual(new_factory.responses[0]["connection_generation"], 1)
        self.assertEqual(
            bot.ingress_values[0]["response_capability"],
            old_action["response_capability"],
        )

    def test_retired_card_projection_never_blocks_replacement_owner(self) -> None:
        patch_entered = threading.Event()
        release_patch = threading.Event()

        def slow_failing_patch(
            _chat_id: str,
            _message_id: str,
            _content: str,
        ) -> bool:
            patch_entered.set()
            self.assertTrue(release_patch.wait(timeout=1.0))
            raise RuntimeError("projection transport failed")

        factory = _FeishuControllerFactory(patch_message=slow_failing_patch)
        controller = factory.make()
        original = _identity()
        controller.handle_adapter_request(original)

        retirement = controller.retire_backend_epoch_after_stop()
        self.assertEqual(retirement.count, 1)
        self.assertFalse(patch_entered.is_set())
        self.assertEqual(controller.pending_count(), 0)

        worker = threading.Thread(
            target=controller.project_backend_reset_cards_best_effort,
            daemon=True,
        )
        worker.start()
        self.assertTrue(patch_entered.wait(timeout=1.0))

        # Projection is blocked in Feishu I/O, but it holds no owner lock and
        # cannot prevent the replacement request from obtaining a fresh nonce.
        replacement = _identity()
        controller.handle_adapter_request(replacement)
        replacement_pending = controller.pending_request_snapshot(
            replacement.request_key
        )
        self.assertIsNotNone(replacement_pending)

        release_patch.set()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(
            controller.pending_request_snapshot(replacement.request_key)
        )


if __name__ == "__main__":
    unittest.main()
