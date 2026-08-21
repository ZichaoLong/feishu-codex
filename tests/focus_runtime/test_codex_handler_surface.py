from __future__ import annotations

import pathlib
import unittest
from unittest.mock import Mock, patch

import bot.codex_handler as codex_handler_module
from bot.codex_handler import CodexHandler
from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
)


class CodexHandlerSurfaceTests(unittest.TestCase):
    def _handler(self) -> tuple[CodexHandler, Mock]:
        runtime = Mock()
        with patch("bot.codex_handler.FocusRuntime", return_value=runtime) as factory:
            handler = CodexHandler(
                data_dir=pathlib.Path("/tmp/focus-data"),
                config_dir=pathlib.Path("/tmp/focus-config"),
            )
        factory.assert_called_once_with(
            data_dir=pathlib.Path("/tmp/focus-data"),
            config_dir=pathlib.Path("/tmp/focus-config"),
        )
        return handler, runtime

    def test_handler_owns_only_platform_adapter_and_runtime(self) -> None:
        handler, runtime = self._handler()
        bot = object()

        handler.on_register(bot)

        self.assertEqual(set(vars(handler)), {"bot", "_runtime"})
        self.assertIs(handler.bot, bot)
        self.assertIs(handler.runtime, runtime)
        runtime.start.assert_called_once_with(bot)

    def test_public_ingress_delegates_without_rebuilding_a_gate(self) -> None:
        handler, runtime = self._handler()
        runtime.is_sender_active.return_value = True
        runtime.preflight_group_prompt.return_value = True
        runtime.should_route_group_followup_prompt.return_value = False
        card_response = object()
        runtime.handle_card_action.return_value = card_response

        handler.handle_message("sender", "chat", "hello", "message")
        handler.handle_message_recalled("chat", "message")
        returned_card = handler.handle_card_action(
            "sender",
            "chat",
            "message",
            {"action": "attach_runtime"},
        )
        handler.handle_attachment_message(
            "sender",
            "chat",
            "message",
            "file",
            "resource",
            "name.txt",
        )
        self.assertTrue(handler.is_sender_active("sender", "chat", "message"))
        handler.deactivate_sender("sender", "chat", "message")
        self.assertTrue(
            handler.preflight_group_prompt(
                "sender",
                "chat",
                message_id="message",
            )
        )
        self.assertFalse(
            handler.should_route_group_followup_prompt(
                "sender",
                "chat",
                message_id="message",
            )
        )
        destination_loss = FeishuDestinationLossProof(
            source_id="event-1",
            chat_id="chat",
            proof_type=FeishuDestinationLossProofType.BOT_REMOVED_EVENT,
        )
        handler.accept_destination_loss_proof(destination_loss)
        handler.shutdown()

        runtime.handle_message.assert_called_once_with(
            "sender", "chat", "hello", "message"
        )
        runtime.handle_message_recalled.assert_called_once_with("chat", "message")
        runtime.handle_card_action.assert_called_once_with(
            "sender",
            "chat",
            "message",
            {"action": "attach_runtime"},
        )
        self.assertIs(returned_card, card_response)
        runtime.handle_attachment_message.assert_called_once_with(
            "sender",
            "chat",
            "message",
            "file",
            "resource",
            "name.txt",
        )
        runtime.is_sender_active.assert_called_once_with(
            "sender", "chat", "message"
        )
        runtime.deactivate_sender.assert_called_once_with(
            "sender", "chat", "message"
        )
        runtime.preflight_group_prompt.assert_called_once_with(
            "sender", "chat", message_id="message"
        )
        runtime.should_route_group_followup_prompt.assert_called_once_with(
            "sender", "chat", message_id="message"
        )
        runtime.accept_destination_loss_proof.assert_called_once_with(
            destination_loss
        )
        runtime.shutdown.assert_called_once_with()

    def test_handler_source_does_not_own_lifecycle_or_ingress_gate(self) -> None:
        source = pathlib.Path(codex_handler_module.__file__).resolve().read_text(encoding="utf-8")

        self.assertNotIn("ServiceRuntimeLifecycle", source)
        self.assertNotIn("ServiceRuntimeIngressDispatcher", source)
        self.assertNotIn("._ingress", source)


if __name__ == "__main__":
    unittest.main()
