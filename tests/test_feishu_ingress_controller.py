from __future__ import annotations

import ast
import json
import pathlib
import tempfile
import unittest
from typing import Any

from bot.feishu_ingress_controller import (
    FeishuInboundMessage,
    FeishuIngressController,
    FeishuIngressPorts,
)
from bot.feishu_message_codec import FeishuMessageCodec, FeishuMessageCodecPorts
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.feishu_process_cache import FeishuProcessCache
from bot.group_history_recovery import ListedMessagesPage
from bot.stores.group_chat_store import GroupChatStore


def _confirmed(
    operation: FeishuOutboundOperation,
    chat_id: str,
    message_id: str,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id="test-attempt",
        message_id=message_id,
    )


class _Harness:
    def __init__(self, data_dir: pathlib.Path) -> None:
        self.cache = FeishuProcessCache()
        self.handled: list[tuple[str, str, str, str]] = []
        self.replies: list[tuple[str, str, str]] = []
        self.codec = FeishuMessageCodec(
            FeishuMessageCodecPorts(
                load_raw_card_content=lambda _message_id: {},
                resolve_sender_name=lambda open_id: open_id,
                remember_sender_name=lambda _key, _value: None,
                configured_trigger_open_ids=lambda: {"ou-bot"},
                log_card_ingress_event=lambda _event, _fields: None,
            )
        )
        self.controller = FeishuIngressController(
            ports=FeishuIngressPorts(
                handle_message=lambda sender, chat, text, message: (
                    self.handled.append((sender, chat, text, message))
                ),
                handle_attachment=lambda *_args: None,
                allow_group_prompt=lambda _sender, _chat, _message: True,
                should_route_group_followup_prompt=(
                    lambda _sender, _chat, _message: False
                ),
                reply_text=self._reply_text,
                send_message=lambda chat, _type, _content: _confirmed(
                    FeishuOutboundOperation.CREATE_MESSAGE,
                    chat,
                    "sent-1",
                ),
                reply_to_message=(
                    lambda chat, _parent, _type, _content: _confirmed(
                        FeishuOutboundOperation.REPLY_MESSAGE,
                        chat,
                        "reply-1",
                    )
                ),
                patch_message=lambda chat, message, _content: _confirmed(
                    FeishuOutboundOperation.PATCH_MESSAGE,
                    chat,
                    message,
                ),
                list_history_messages_page=lambda **_kwargs: (
                    ListedMessagesPage(items=[])
                ),
                fetch_merge_forward_items=lambda _message_id: [],
                display_name_for_sender_identity=(
                    lambda **fields: str(
                        fields.get("sender_principal_id")
                        or fields.get("user_id")
                        or "unknown"
                    )
                ),
                log_card_ingress_event=lambda _event, _fields: None,
            ),
            process_cache=self.cache,
            message_codec=self.codec,
            group_store=GroupChatStore(data_dir),
            app_id="app-id",
            admin_open_ids={"ou-admin"},
            configured_bot_open_id="ou-bot",
            configured_trigger_open_ids=set(),
            history_fetch_limit=0,
            history_fetch_lookback_seconds=0,
        )

    def _reply_text(
        self,
        chat_id: str,
        text: str,
        *,
        parent_message_id: str = "",
    ) -> None:
        self.replies.append((chat_id, text, parent_message_id))


class FeishuIngressControllerTests(unittest.TestCase):
    def _harness(self) -> _Harness:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return _Harness(pathlib.Path(tempdir.name))

    def test_assistant_ingress_owns_logging_policy_and_dispatch(self) -> None:
        harness = self._harness()
        controller = harness.controller
        controller.set_group_mode("chat-1", "assistant")
        controller.activate_group_chat("chat-1", activated_by="ou-admin")

        controller.handle_message(self._message("m-1", "先讨论", mentions=()))
        controller.handle_message(
            self._message(
                "m-2",
                "@_user_1 请总结",
                mentions=(
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou-bot"},
                        "name": "Codex",
                    },
                ),
            )
        )

        self.assertEqual(len(harness.handled), 1)
        self.assertIn("先讨论", harness.handled[0][2])
        self.assertIn("请总结", harness.handled[0][2])
        self.assertEqual(
            controller.group_store.get_last_boundary_seq("chat-1"),
            2,
        )

    def test_destination_loss_cleanup_has_one_explicit_owner_and_order(self) -> None:
        harness = self._harness()
        controller = harness.controller
        cache = harness.cache
        controller.set_group_mode("chat-1", "assistant")
        controller.activate_group_chat("chat-1", activated_by="ou-admin")
        cache.remember_chat_type("chat-1", "group")
        cache.remember_chat_type("chat-2", "p2p")
        cache.remember_message_context("message-1", {"chat_id": "chat-1"})
        cache.remember_message_context("message-2", {"chat_id": "chat-2"})
        controller.forward_aggregator.buffer_forward(
            "ou-user",
            "chat-1",
            "forwarded",
            "forward-1",
            "group",
        )

        order: list[str] = []
        clear_chat = controller.group_store.clear_chat
        forget_cache = cache.forget_chat
        forget_forward = controller.forward_aggregator.forget_chat
        controller.group_store.clear_chat = lambda chat_id: (
            order.append("group_store"),
            clear_chat(chat_id),
        )[-1]
        cache.forget_chat = lambda chat_id: (
            order.append("process_cache"),
            forget_cache(chat_id),
        )[-1]
        controller.forward_aggregator.forget_chat = lambda chat_id: (
            order.append("forward_aggregator"),
            forget_forward(chat_id),
        )[-1]

        controller.forget_chat_state_after_destination_loss("chat-1")

        self.assertEqual(
            order,
            ["group_store", "process_cache", "forward_aggregator"],
        )
        self.assertFalse(
            controller.get_group_activation_snapshot("chat-1")["activated"]
        )
        self.assertEqual(cache.lookup_chat_type("chat-1"), "")
        self.assertEqual(cache.lookup_chat_type("chat-2"), "p2p")
        self.assertEqual(cache.get_message_context("message-1"), {})
        self.assertEqual(cache.get_message_context("message-2")["chat_id"], "chat-2")
        self.assertIsNone(
            controller.forward_aggregator.peek_pending_forward(
                "ou-user",
                "chat-1",
            )
        )

    def test_unknown_reserved_card_patch_does_not_fallback(self) -> None:
        harness = self._harness()
        controller = harness.controller
        harness.cache.reserve_execution_card("parent-1", "reserved-1")
        object.__setattr__(
            controller._ports,
            "patch_message",
            lambda chat_id, _message_id, _content: FeishuOutboundResult(
                operation=FeishuOutboundOperation.PATCH_MESSAGE,
                effect=FeishuOutboundEffect.UNKNOWN,
                destination_liveness=FeishuDestinationLiveness.UNKNOWN,
                chat_id=chat_id,
                attempt_id="attempt-unknown",
                error_message="timeout",
            ),
        )
        object.__setattr__(
            controller._ports,
            "reply_to_message",
            lambda *_args: self.fail("unknown patch must not reply"),
        )
        object.__setattr__(
            controller._ports,
            "send_message",
            lambda *_args: self.fail("unknown patch must not send"),
        )

        controller._notify_group_history_fetch_failed(
            chat_id="chat-1",
            parent_message_id="parent-1",
            error=RuntimeError("history unavailable"),
        )

    @staticmethod
    def _message(
        message_id: str,
        text: str,
        *,
        mentions: tuple[Any, ...],
    ) -> FeishuInboundMessage:
        return FeishuInboundMessage(
            sender_type="user",
            sender_user_id="u-user",
            sender_open_id="ou-user",
            chat_id="chat-1",
            message_id=message_id,
            message_type="text",
            chat_type="group",
            content=json.dumps({"text": text}, ensure_ascii=False),
            mentions=mentions,
            create_time=1000 if message_id == "m-1" else 2000,
        )

    def test_feishu_bot_is_only_the_sdk_bridge_and_ingress_facade(self) -> None:
        root = pathlib.Path(__file__).parents[1] / "bot"
        bot_module = ast.parse((root / "feishu_bot.py").read_text(encoding="utf-8"))
        bot = next(
            node
            for node in bot_module.body
            if isinstance(node, ast.ClassDef) and node.name == "FeishuBot"
        )
        methods = {
            node.name: node for node in bot.body if isinstance(node, ast.FunctionDef)
        }
        forbidden = {
            "_append_group_log_entry",
            "_buffer_forward",
            "_collect_assistant_context_entries",
            "_fetch_merge_forward_text",
            "_is_bot_mentioned",
            "_notify_group_history_fetch_failed",
            "_prepare_group_history_execution_card",
        }
        facade_names = {
            "activate_group_chat",
            "add_admin_open_id",
            "deactivate_group_chat",
            "extract_non_bot_mentions",
            "forget_chat_state_after_destination_loss",
            "get_group_activation_snapshot",
            "get_group_mode",
            "is_admin",
            "is_group_admin",
            "is_group_user_allowed",
            "list_admin_open_ids",
            "prepare_queued_prompt_text",
            "set_configured_bot_open_id",
            "set_group_mode",
        }

        self.assertEqual(set(methods) & forbidden, set())
        for name in facade_names:
            with self.subTest(name=name):
                self.assertEqual(len(methods[name].body), 1)
                self.assertIn("_ingress", ast.unparse(methods[name]))
        bridge = ast.unparse(methods["_handle_raw_message"])
        self.assertEqual(bridge.count("self._ingress.handle_message"), 1)
        self.assertEqual(bridge.count("FeishuInboundMessage("), 1)
        self.assertNotIn("json.loads", bridge)
        self.assertNotIn("group_mode", bridge)

    def test_ingress_owner_does_not_import_feishu_sdk(self) -> None:
        path = (
            pathlib.Path(__file__).parents[1] / "bot" / "feishu_ingress_controller.py"
        )
        module = ast.parse(path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in module.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }

        self.assertNotIn("lark_oapi", imported_roots)


if __name__ == "__main__":
    unittest.main()
