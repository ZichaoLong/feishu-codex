import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import lark_oapi.ws.client as lark_ws_client
from lark_oapi.api.im.v1 import P2ImMessageRecalledV1, P2ImMessageReceiveV1

from bot.cards import build_execution_card, build_terminal_result_card
from bot.card_text_projection import terminal_result_checksum
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_bot import FeishuBot
from bot.feishu_destination_liveness_contract import FeishuDestinationLossProof
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.feishu_ws_proxy import (
    configure_feishu_ws_proxy,
    normalize_feishu_ws_proxy_mode,
)
from bot.system_config import SystemConfig


def _confirmed_outbound(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str,
    message_id: str,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id="recording-attempt",
        message_id=message_id,
    )


class _RecordingBot(FeishuBot):
    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        system_config: SystemConfig | dict | None = None,
    ) -> None:
        if isinstance(system_config, SystemConfig):
            config = system_config
        else:
            raw_config = {
                "app_id": "app-id",
                "app_secret": "app-secret",
                "admin_open_ids": ["ou-admin"],
                "bot_open_id": "ou-bot",
            }
            if system_config:
                raw_config.update(system_config)
            config = SystemConfig.from_dict(raw_config)
        super().__init__(
            data_dir=data_dir,
            system_config=config,
        )
        self.received_messages: list[tuple[str, str, str, str]] = []
        self.received_attachments: list[tuple[str, str, str, str, str, str]] = []
        self.replies: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []
        self.reply_refs: list[tuple[str, str, str]] = []
        self.reply_parents: list[tuple[str, str, str]] = []
        self.card_parents: list[tuple[str, dict, str]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.reply_ref_thread_flags: list[bool] = []
        self.history_entries: list[dict] = []
        self.history_fetch_calls: list[dict] = []
        self.history_fetch_error: Exception | None = None
        self.raw_message_items: dict[str, list[object]] = {}
        self.allow_group_prompt_result = True
        self.route_group_followup_prompt_result = False
        self.recalled_messages: list[tuple[str, str]] = []
        self.destination_loss_proofs: list[FeishuDestinationLossProof] = []
        self._default_collect_assistant_context_entries = (
            self._ingress._collect_assistant_context_entries
        )
        self._ingress._collect_assistant_context_entries = (
            self._collect_assistant_context_entries
        )

    def on_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self.received_messages.append((sender_id, chat_id, text, message_id))

    def on_card_action(self, sender_id: str, chat_id: str, message_id: str, action_value: dict):
        return self.make_card_response()

    def on_attachment_message(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        attachment_type: str,
        resource_key: str,
        file_name: str,
    ) -> None:
        self.received_attachments.append(
            (sender_id, chat_id, message_id, attachment_type, resource_key, file_name)
        )

    def reply(self, chat_id: str, text: str, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.replies.append((chat_id, text))
        if parent_message_id:
            self.reply_parents.append((chat_id, text, parent_message_id))

    def reply_card(self, chat_id: str, card: dict, *, parent_message_id: str = "", reply_in_thread: bool = False) -> None:
        self.cards.append((chat_id, card))
        if parent_message_id:
            self.card_parents.append((chat_id, card, parent_message_id))

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
    ) -> FeishuOutboundResult:
        self.sent_messages.append((chat_id, msg_type, content))
        return _confirmed_outbound(
            FeishuOutboundOperation.CREATE_MESSAGE,
            chat_id=chat_id,
            message_id="bootstrap-card-2",
        )

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> FeishuOutboundResult:
        self.reply_refs.append((parent_id, msg_type, content))
        self.reply_ref_thread_flags.append(reply_in_thread)
        return _confirmed_outbound(
            FeishuOutboundOperation.REPLY_MESSAGE,
            chat_id=chat_id,
            message_id="bootstrap-card-1",
        )

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
    ) -> FeishuOutboundResult:
        self.patches.append((message_id, content))
        return _confirmed_outbound(
            FeishuOutboundOperation.PATCH_MESSAGE,
            chat_id=chat_id,
            message_id=message_id,
        )

    def _resolve_sender_name(self, open_id: str) -> str:
        return open_id[:8]

    def allow_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        del sender_id
        del chat_id
        del message_id
        return bool(self.allow_group_prompt_result)

    def should_route_group_followup_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        del sender_id
        del chat_id
        del message_id
        return bool(self.route_group_followup_prompt_result)

    def on_message_recalled(self, chat_id: str, message_id: str) -> None:
        self.recalled_messages.append((chat_id, message_id))

    def on_destination_loss_proof(self, proof: FeishuDestinationLossProof) -> None:
        self.destination_loss_proofs.append(proof)

    def get_message_items(
        self, message_id: str, *, card_msg_content_type: str = ""
    ) -> list[object]:
        del card_msg_content_type
        return list(self.raw_message_items.get(message_id, []))

    def get_message_content_dict(self, message_id: str, *, card_msg_content_type: str = "") -> dict:
        del card_msg_content_type
        items = self.get_message_items(message_id)
        for item in items:
            if str(getattr(item, "message_id", "") or "").strip() != str(message_id or "").strip():
                continue
            body = getattr(item, "body", None)
            raw_content = str(getattr(body, "content", "") or "").strip()
            if not raw_content:
                continue
            return json.loads(raw_content)
        return {}

    def _collect_assistant_context_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time,
        current_seq: int,
        thread_id: str = "",
    ) -> list[dict]:
        history_recovery = self._ingress.history_recovery
        original_fetch = history_recovery.fetch_group_history_entries
        history_recovery.fetch_group_history_entries = (
            self._recorded_group_history_entries
        )
        try:
            return self._default_collect_assistant_context_entries(
                chat_id=chat_id,
                current_message_id=current_message_id,
                current_create_time=current_create_time,
                current_seq=current_seq,
                thread_id=thread_id,
            )
        finally:
            history_recovery.fetch_group_history_entries = original_fetch

    def _recorded_group_history_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time,
        existing_message_ids: set[str],
        after_created_at=None,
        after_message_ids: set[str] | None = None,
        thread_id: str = "",
        limit: int | None = None,
    ) -> list[dict]:
        self.history_fetch_calls.append(
            {
                "chat_id": chat_id,
                "current_message_id": current_message_id,
                "existing_message_ids": set(existing_message_ids),
                "after_created_at": after_created_at,
                "after_message_ids": set(after_message_ids or set()),
                "thread_id": thread_id,
                "limit": limit,
            }
        )
        if self.history_fetch_error is not None:
            raise self.history_fetch_error
        return [dict(item) for item in self.history_entries]


def _history_item(
    *,
    message_id: str,
    created_at: int,
    text: str,
    sender_id: str = "ou-user",
    sender_type: str = "user",
    thread_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        msg_type="text",
        body=SimpleNamespace(content=json.dumps({"text": text}, ensure_ascii=False)),
        mentions=[],
        sender=SimpleNamespace(sender_type=sender_type, id=sender_id),
        create_time=created_at,
        thread_id=thread_id,
    )


class _HistoryResponse:
    def __init__(self, items: list[SimpleNamespace], *, has_more: bool = False, page_token: str = "") -> None:
        self.code = 0
        self.msg = "ok"
        self.data = SimpleNamespace(items=items, has_more=has_more, page_token=page_token)

    def success(self) -> bool:
        return True


class FeishuBotIngressDebugConfigTests(unittest.TestCase):
    def test_raw_card_ingress_logging_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            bot = _RecordingBot(pathlib.Path(raw))

        self.assertFalse(bot._debug_raw_card_ingress)

    def test_raw_card_ingress_logging_requires_yaml_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaisesRegex(ValueError, "debug_raw_card_ingress"):
                _RecordingBot(
                    pathlib.Path(raw),
                    system_config={"debug_raw_card_ingress": "false"},
                )

    def test_feishu_bot_consumes_a_validated_typed_system_config(self) -> None:
        config = SystemConfig.from_dict(
            {
                "app_id": "app-id",
                "app_secret": "app-secret",
                "request_timeout_seconds": 5,
                "feishu_ws_proxy": "disabled",
                "admin_open_ids": ["ou-admin"],
                "bot_open_id": "ou-bot",
                "trigger_open_ids": ["ou-trigger"],
                "group_history_fetch_limit": 0,
                "group_history_fetch_lookback_seconds": 0,
                "debug_raw_card_ingress": True,
            }
        )

        with tempfile.TemporaryDirectory() as raw:
            bot = _RecordingBot(pathlib.Path(raw), system_config=config)

        self.assertEqual(bot._feishu_ws_proxy_mode, "disabled")
        self.assertEqual(bot.list_admin_open_ids(), ["ou-admin"])
        self.assertEqual(
            bot._ingress.configured_group_trigger_open_ids(),
            {"ou-bot", "ou-trigger"},
        )
        self.assertFalse(bot._ingress.history_recovery.history_recovery_enabled())
        self.assertTrue(bot._debug_raw_card_ingress)

    def test_feishu_bot_does_not_coerce_bad_system_config_values(self) -> None:
        invalid_configs = (
            {"admin_open_ids": "ou-admin"},
            {"group_history_fetch_limit": "50"},
            {"request_timeout_seconds": "5"},
        )
        for config in invalid_configs:
            with self.subTest(config=config):
                with tempfile.TemporaryDirectory() as raw:
                    with self.assertRaises(ValueError):
                        _RecordingBot(pathlib.Path(raw), system_config=config)

def _message_event(
    *,
    message_id: str,
    chat_id: str,
    text: str,
    sender_user_id: str,
    sender_open_id: str,
    sender_type: str = "user",
    mentions: list[dict] | None = None,
    create_time: int = 1712476800000,
    thread_id: str = "",
    root_id: str = "",
    parent_id: str = "",
) -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {
                        "user_id": sender_user_id,
                        "open_id": sender_open_id,
                    },
                    "sender_type": sender_type,
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": "group",
                    "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "mentions": mentions or [],
                    "create_time": create_time,
                    "thread_id": thread_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                },
            }
        }
    )


def _p2p_message_event(
    *,
    message_id: str,
    chat_id: str,
    text: str,
    sender_user_id: str,
    sender_open_id: str,
    sender_type: str = "user",
    mentions: list[dict] | None = None,
    create_time: int = 1712476800000,
    thread_id: str = "",
    root_id: str = "",
    parent_id: str = "",
) -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {
                        "user_id": sender_user_id,
                        "open_id": sender_open_id,
                    },
                    "sender_type": sender_type,
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": "p2p",
                    "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "mentions": mentions or [],
                    "create_time": create_time,
                    "thread_id": thread_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                },
            }
        }
    )


def _attachment_message_event(
    *,
    message_id: str,
    chat_id: str,
    msg_type: str,
    sender_user_id: str,
    sender_open_id: str,
    content: dict,
    sender_type: str = "user",
    mentions: list[dict] | None = None,
    create_time: int = 1712476800000,
    thread_id: str = "",
    root_id: str = "",
    parent_id: str = "",
    chat_type: str = "p2p",
) -> P2ImMessageReceiveV1:
    return P2ImMessageReceiveV1(
        {
            "event": {
                "sender": {
                    "sender_id": {
                        "user_id": sender_user_id,
                        "open_id": sender_open_id,
                    },
                    "sender_type": sender_type,
                },
                "message": {
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "chat_type": chat_type,
                    "message_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "mentions": mentions or [],
                    "create_time": create_time,
                    "thread_id": thread_id,
                    "root_id": root_id,
                    "parent_id": parent_id,
                },
            }
        }
    )


class FeishuWebSocketProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._had_ws_connect_kwargs = hasattr(lark_ws_client, "_ws_connect_kwargs")
        self._original_ws_connect_kwargs = getattr(lark_ws_client, "_ws_connect_kwargs", None)
        self._original_websockets = lark_ws_client.websockets
        self.addCleanup(self._restore_ws_proxy_state)

    def _restore_ws_proxy_state(self) -> None:
        lark_ws_client.websockets = self._original_websockets
        if self._had_ws_connect_kwargs:
            lark_ws_client._ws_connect_kwargs = self._original_ws_connect_kwargs
        elif hasattr(lark_ws_client, "_ws_connect_kwargs"):
            delattr(lark_ws_client, "_ws_connect_kwargs")
        if hasattr(lark_ws_client, "_focus_original_websockets"):
            delattr(lark_ws_client, "_focus_original_websockets")

    def _websockets_supports_proxy(self) -> bool:
        return "proxy" in lark_ws_client.inspect.signature(lark_ws_client.websockets.connect).parameters

    def test_feishu_ws_proxy_env_restores_websockets_environment_proxy_discovery(self) -> None:
        mode = configure_feishu_ws_proxy("env")

        self.assertEqual(mode, "env")
        if self._had_ws_connect_kwargs:
            self.assertEqual(lark_ws_client._ws_connect_kwargs(), {})
        else:
            self.assertIs(lark_ws_client.websockets, self._original_websockets)

    def test_feishu_ws_proxy_disabled_preserves_direct_sdk_websocket_behavior(self) -> None:
        mode = configure_feishu_ws_proxy("disabled")

        self.assertEqual(mode, "disabled")
        if self._had_ws_connect_kwargs:
            kwargs = lark_ws_client._ws_connect_kwargs()
            if self._websockets_supports_proxy():
                self.assertEqual(kwargs, {"proxy": None})
            else:
                self.assertEqual(kwargs, {})
        else:
            self.assertIsNot(lark_ws_client.websockets, self._original_websockets)

    def test_feishu_ws_proxy_mode_rejects_unknown_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "feishu_ws_proxy"):
            normalize_feishu_ws_proxy_mode("container")

    def test_feishu_ws_proxy_env_uses_direct_sdk_behavior_when_private_hook_is_missing(self) -> None:
        if hasattr(lark_ws_client, "_ws_connect_kwargs"):
            delattr(lark_ws_client, "_ws_connect_kwargs")

        self.assertEqual(configure_feishu_ws_proxy("env"), "env")
        self.assertIs(lark_ws_client.websockets, self._original_websockets)

    def test_start_applies_configured_feishu_ws_proxy_mode_before_connecting(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        bot = _RecordingBot(pathlib.Path(tempdir.name), system_config={"feishu_ws_proxy": "disabled"})
        ws_client = Mock()

        with patch("bot.feishu_bot.lark.ws.Client", return_value=ws_client):
            bot.start()

        ws_client.start.assert_called_once_with()
        if self._had_ws_connect_kwargs:
            kwargs = lark_ws_client._ws_connect_kwargs()
            if self._websockets_supports_proxy():
                self.assertEqual(kwargs, {"proxy": None})
        else:
            self.assertIsNot(lark_ws_client.websockets, self._original_websockets)


class FeishuBotCardProjectionTests(unittest.TestCase):
    def _make_bot(self) -> _RecordingBot:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return _RecordingBot(pathlib.Path(tempdir.name))

    def test_p2p_terminal_result_card_projects_authoritative_text(self) -> None:
        bot = self._make_bot()
        bot.raw_message_items["card-1"] = [
            SimpleNamespace(
                message_id="card-1",
                body=SimpleNamespace(
                    content=json.dumps(build_terminal_result_card("稳定终态"), ensure_ascii=False)
                ),
            )
        ]

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="card-1",
                chat_id="ou-admin",
                chat_type="p2p",
                msg_type="interactive",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content=build_terminal_result_card("稳定终态"),
            )
        )

        self.assertEqual(
            bot.received_messages,
            [("ou-admin", "ou-admin", "稳定终态", "card-1")],
        )

    def test_p2p_execution_card_projects_visible_text_best_effort(self) -> None:
        bot = self._make_bot()
        bot.raw_message_items["card-2"] = [
            SimpleNamespace(
                message_id="card-2",
                body=SimpleNamespace(
                    content=json.dumps(
                        build_execution_card(
                            "命令输出",
                            [ExecutionReplySegment("assistant", "阶段回复")],
                            running=False,
                        ),
                        ensure_ascii=False,
                    )
                ),
            )
        ]

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="card-2",
                chat_id="ou-admin",
                chat_type="p2p",
                msg_type="interactive",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content=build_execution_card(
                    "命令输出",
                    [ExecutionReplySegment("assistant", "阶段回复")],
                    running=False,
                ),
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        self.assertIn("命令输出", bot.received_messages[0][2])
        self.assertIn("阶段回复", bot.received_messages[0][2])

    def test_read_interactive_message_text_falls_back_to_projection_when_raw_fetch_fails(self) -> None:
        bot = self._make_bot()
        card = build_terminal_result_card("投影终态")

        def _raise(*args, **kwargs):
            raise RuntimeError("boom")

        bot.get_message_content_dict = _raise  # type: ignore[method-assign]

        self.assertEqual(
            bot.read_interactive_message_text("msg-1", content_dict=card),
            "投影终态",
        )

    def test_read_interactive_message_text_uses_authoritative_terminal_text_without_outer_title(self) -> None:
        bot = self._make_bot()
        card = build_terminal_result_card("# 示例标题\n\n正文")
        bot.raw_message_items["msg-terminal"] = [
            SimpleNamespace(
                message_id="msg-terminal",
                body=SimpleNamespace(content=json.dumps(card, ensure_ascii=False)),
            )
        ]

        self.assertEqual(
            bot.read_interactive_message_text("msg-terminal", content_dict={"text": "请升级至最新版本客户端，以查看内容"}),
            "# 示例标题\n\n正文",
        )

    def test_read_interactive_message_restores_terminal_result_from_local_resolver(self) -> None:
        bot = self._make_bot()
        result_id = "0123456789abcdef0123456789abcdef"
        original = "| 证据 |\n|---|\n| `tool_use` 和 [protocol.rs](/tmp/protocol.rs:1) |"
        checksum = terminal_result_checksum(original)
        forwarded = build_terminal_result_card(
            "| 证据 |\n| -------- |\n|  和  |",
            terminal_result_id=result_id,
            checksum=checksum,
        )
        bot.raw_message_items["msg-forwarded"] = [
            SimpleNamespace(
                message_id="msg-forwarded",
                body=SimpleNamespace(content=json.dumps(forwarded, ensure_ascii=False)),
            )
        ]
        bot.set_terminal_result_text_resolver(
            lambda projection: original
            if projection.terminal_result_id == result_id
            and projection.terminal_result_checksum == checksum[:16]
            else ""
        )

        result = bot.read_interactive_message("msg-forwarded", content_dict=forwarded)

        self.assertEqual(result.text, original)
        self.assertTrue(result.has_authoritative_text)
        self.assertEqual(result.terminal_result_id, result_id)
        self.assertEqual(result.text_source, "store")

    def test_history_entry_projects_interactive_terminal_result_from_other_app_sender(self) -> None:
        bot = self._make_bot()

        entry = bot._ingress.history_recovery.history_entry_from_message(
            SimpleNamespace(
                message_id="hist-card",
                msg_type="interactive",
                body=SimpleNamespace(
                    content=json.dumps(build_terminal_result_card("来自其他机器人的终态"), ensure_ascii=False)
                ),
                mentions=[],
                sender=SimpleNamespace(sender_type="app", id="cli_other_bot"),
                create_time=1712476800000,
                thread_id="",
            )
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["text"], "来自其他机器人的终态")
        self.assertEqual(entry["sender_type"], "app")

    def test_p2p_ordinary_card_with_marker_like_text_keeps_best_effort_projection(self) -> None:
        bot = self._make_bot()

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="card-3",
                chat_id="ou-admin",
                chat_type="p2p",
                msg_type="interactive",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content={
                    "header": {
                        "title": {"tag": "plain_text", "content": "示例卡片"},
                    },
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": "普通说明：`<final_reply_text>demo</final_reply_text>`",
                        }
                    ],
                },
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        self.assertIn("示例卡片", bot.received_messages[0][2])
        self.assertIn("<final_reply_text>demo</final_reply_text>", bot.received_messages[0][2])

    def test_merge_forward_projects_interactive_terminal_result_from_other_app(self) -> None:
        bot = self._make_bot()
        bot.raw_message_items["sub-card"] = [
            SimpleNamespace(
                message_id="sub-card",
                body=SimpleNamespace(
                    content=json.dumps(build_terminal_result_card("来自转发终态卡"), ensure_ascii=False)
                ),
            )
        ]

        object.__setattr__(bot._ingress.forward_aggregator._ports, "fetch_merge_forward_items", lambda _message_id: [
            SimpleNamespace(
                message_id="sub-card",
                upper_message_id="merge-root",
                msg_type="interactive",
                sender=SimpleNamespace(sender_type="app", id="cli_other_bot"),
                body=SimpleNamespace(
                    content=json.dumps(build_terminal_result_card("来自转发终态卡"), ensure_ascii=False)
                ),
                create_time=1712476800000,
            )
        ])

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="merge-root",
                chat_id="ou-admin",
                chat_type="p2p",
                msg_type="merge_forward",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content={"text": "Merged and Forwarded Message"},
            )
        )

        pending = bot._ingress.forward_aggregator.peek_pending_forward("ou-admin", "ou-admin")
        assert pending is not None
        pending.timer.cancel()

        bot._handle_raw_message(
            _p2p_message_event(
                message_id="leave-1",
                chat_id="ou-admin",
                text="请读取这个",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        forwarded_text = bot.received_messages[0][2]
        self.assertIn("<forwarded_messages>", forwarded_text)
        self.assertIn("cli_othe[机器人]", forwarded_text)
        self.assertIn("来自转发终态卡", forwarded_text)
        self.assertIn("请读取这个", forwarded_text)

    def test_merge_forward_restores_authoritative_terminal_result_from_local_resolver(self) -> None:
        bot = self._make_bot()
        result_id = "fedcba9876543210fedcba9876543210"
        original = "权威原文：`load()` 和 [protocol.rs](/tmp/protocol.rs:1)"
        checksum = terminal_result_checksum(original)
        forwarded_card = build_terminal_result_card(
            "降级投影： 和 ",
            terminal_result_id=result_id,
            checksum=checksum,
        )
        bot.raw_message_items["sub-card"] = [
            SimpleNamespace(
                message_id="sub-card",
                body=SimpleNamespace(content=json.dumps(forwarded_card, ensure_ascii=False)),
            )
        ]
        bot.set_terminal_result_text_resolver(
            lambda projection: original
            if projection.terminal_result_id == result_id
            and projection.terminal_result_checksum == checksum[:16]
            else ""
        )

        def _fetch_merge_forward_items(_message_id: str) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    message_id="sub-card",
                    upper_message_id="merge-root",
                    msg_type="interactive",
                    sender=SimpleNamespace(sender_type="app", id="cli_other_bot"),
                    body=SimpleNamespace(content=json.dumps(forwarded_card, ensure_ascii=False)),
                    create_time=1712476800000,
                )
            ]

        object.__setattr__(bot._ingress.forward_aggregator._ports, "fetch_merge_forward_items", _fetch_merge_forward_items)

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="merge-root",
                chat_id="ou-admin",
                chat_type="p2p",
                msg_type="merge_forward",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content={"text": "Merged and Forwarded Message"},
            )
        )

        pending = bot._ingress.forward_aggregator.peek_pending_forward("ou-admin", "ou-admin")
        assert pending is not None
        pending.timer.cancel()

        bot._handle_raw_message(
            _p2p_message_event(
                message_id="leave-1",
                chat_id="ou-admin",
                text="请读取这个",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        forwarded_text = bot.received_messages[0][2]
        self.assertIn(original, forwarded_text)
        self.assertNotIn("降级投影", forwarded_text)


class FeishuBotGroupModeTests(unittest.TestCase):
    def _make_bot(self, *, system_config: dict | None = None) -> _RecordingBot:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return _RecordingBot(pathlib.Path(tempdir.name), system_config=system_config)

    def test_activated_group_survives_restart_and_non_admin_can_continue_using_it(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        bot1 = _RecordingBot(data_dir)
        bot1.set_group_mode("chat-1", "all")
        bot1.activate_group_chat("chat-1", activated_by="ou-admin")

        bot2 = _RecordingBot(data_dir)
        self.assertTrue(bot2.get_group_activation_snapshot("chat-1")["activated"])
        self.assertEqual(bot2.get_group_mode("chat-1"), "all")

        bot2._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="管理员离场后继续使用",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(len(bot2.received_messages), 1)
        self.assertEqual(bot2.received_messages[0][2], "管理员离场后继续使用")

    def test_p2p_image_message_routes_to_attachment_handler(self) -> None:
        bot = self._make_bot()

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="img-1",
                chat_id="ou-admin",
                msg_type="image",
                sender_user_id="u-user",
                sender_open_id="ou-admin",
                content={"image_key": "img-key-1"},
            )
        )

        self.assertEqual(
            bot.received_attachments,
            [("ou-admin", "ou-admin", "img-1", "image", "img-key-1", "")],
        )
        self.assertEqual(bot.received_messages, [])

    def test_non_admin_p2p_message_is_rejected(self) -> None:
        bot = self._make_bot()

        bot._handle_raw_message(
            _p2p_message_event(
                message_id="m-p2p",
                chat_id="ou-user",
                text="你好",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertIn("仅支持管理员私聊使用", bot.replies[-1][1])

    def test_non_admin_p2p_bootstrap_commands_are_forwarded(self) -> None:
        for text in ("/whoami", "/bot-status", "/init secret-1"):
            with self.subTest(text=text):
                bot = self._make_bot()

                bot._handle_raw_message(
                    _p2p_message_event(
                        message_id="m-p2p",
                        chat_id="ou-user",
                        text=text,
                        sender_user_id="u-user",
                        sender_open_id="ou-user",
                    )
                )

                self.assertEqual(
                    bot.received_messages,
                    [("ou-user", "ou-user", text, "m-p2p")],
                )
                self.assertEqual(bot.replies, [])

    def test_non_admin_p2p_non_bootstrap_command_is_rejected(self) -> None:
        bot = self._make_bot()

        bot._handle_raw_message(
            _p2p_message_event(
                message_id="m-p2p",
                chat_id="ou-user",
                text="/status",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertIn("仅支持管理员私聊使用", bot.replies[-1][1])

    def test_group_assistant_mode_routes_authorized_attachment_without_logging_text_context(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _attachment_message_event(
                message_id="file-1",
                chat_id="chat-1",
                chat_type="group",
                msg_type="file",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                content={"file_key": "file-key-1", "file_name": "spec.pdf"},
            )
        )

        self.assertEqual(
            bot.received_attachments,
            [("ou-user", "chat-1", "file-1", "file", "file-key-1", "spec.pdf")],
        )
        self.assertEqual(bot.received_messages, [])
        self.assertEqual(bot._ingress.group_store.read_messages_between("chat-1"), [])

    def test_assistant_mode_logs_plain_group_message_without_triggering(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="第一条讨论",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [])
        logged = bot._ingress.group_store.read_messages_between("chat-1")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["text"], "第一条讨论")

    def test_assistant_mode_does_not_route_unmentioned_followup_even_when_runtime_allows_queue(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.route_group_followup_prompt_result = True

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="请提交",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [])
        logged = bot._ingress.group_store.read_messages_between("chat-1")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["text"], "请提交")

    def test_assistant_mode_routes_mentioned_followup_when_runtime_allows_queue(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.allow_group_prompt_result = False
        bot.route_group_followup_prompt_result = True

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请提交",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
                create_time=1712476800000,
            )
        )

        self.assertEqual(bot.received_messages, [("ou-user", "chat-1", "请提交", "m-1")])
        self.assertEqual(bot.history_fetch_calls, [])
        context = bot.get_message_context("m-1")
        self.assertEqual(context["assistant_context_mode"], "deferred_recovery")
        self.assertEqual(context["assistant_context_seq"], 1)
        self.assertEqual(context["created_at"], 1712476800000)
        self.assertEqual(context["sender_name"], "ou-user")

    def test_prepare_queued_assistant_prompt_recovers_to_original_mention_boundary(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        seq_1 = bot._ingress._append_group_log_entry(
            chat_id="chat-1",
            message_id="m-prev",
            created_at=1712476700000,
            sender_user_id="u-user",
            sender_open_id="ou-user",
            sender_type="user",
            msg_type="text",
            text="上一轮触发",
        )
        bot._ingress.group_store.set_last_boundary(
            "chat-1",
            seq=seq_1,
            created_at=1712476700000,
            message_ids=["m-prev"],
        )
        seq_2 = bot._ingress._append_group_log_entry(
            chat_id="chat-1",
            message_id="m-between",
            created_at=1712476750000,
            sender_user_id="u-user",
            sender_open_id="ou-user",
            sender_type="user",
            msg_type="text",
            text="排队前的普通消息",
        )
        seq_3 = bot._ingress._append_group_log_entry(
            chat_id="chat-1",
            message_id="m-queued",
            created_at=1712476800000,
            sender_user_id="u-user2",
            sender_open_id="ou-user2",
            sender_type="user",
            msg_type="text",
            text="请处理",
        )
        bot._ingress._append_group_log_entry(
            chat_id="chat-1",
            message_id="m-after",
            created_at=1712476850000,
            sender_user_id="u-user",
            sender_open_id="ou-user",
            sender_type="user",
            msg_type="text",
            text="排队后的普通消息",
        )

        text = bot.prepare_queued_prompt_text(
            chat_id="chat-1",
            message_id="m-queued",
            text="请处理",
            assistant_context_mode="deferred_recovery",
            assistant_context_created_at=1712476800000,
            assistant_context_seq=seq_3,
            assistant_context_sender_name="Alice",
        )

        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("排队前的普通消息", text)
        self.assertIn("请处理", text)
        self.assertIn("sender_name: Alice", text)
        self.assertNotIn("排队后的普通消息", text)
        self.assertEqual(seq_2, 2)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1"), seq_3)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_created_at("chat-1"), 1712476800000)
        self.assertIn("m-queued", bot._ingress.group_store.get_last_boundary_message_ids("chat-1"))

    def test_assistant_mode_includes_prior_group_messages_on_authorized_mention(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="请大家先看设计稿",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-2",
                chat_id="chat-1",
                text="@_user_1 请总结一下",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        _, _, text, _ = bot.received_messages[0]
        self.assertIn("请大家先看设计稿", text)
        self.assertIn("<group_chat_current_turn>", text)
        self.assertIn("sender_name: ou-user", text)
        self.assertIn("请总结一下", text)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1"), 2)

    def test_group_all_mode_passes_text_through_directly(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "all")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="请直接总结今天讨论",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        _, _, text, _ = bot.received_messages[0]
        self.assertEqual(text, "请直接总结今天讨论")

    def test_group_all_mode_routes_followup_when_runtime_allows_queue_even_if_preflight_blocks(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "all")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.allow_group_prompt_result = False
        bot.route_group_followup_prompt_result = True

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="请提交",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [("ou-user", "chat-1", "请提交", "m-1")])

    def test_group_all_mode_blocks_prompt_when_preflight_blocks_and_runtime_does_not_allow_queue(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "all")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.allow_group_prompt_result = False
        bot.route_group_followup_prompt_result = False

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="插播",
                sender_user_id="u-user",
                sender_open_id="ou-user",
            )
        )

        self.assertEqual(bot.received_messages, [])

    def test_group_mention_only_wraps_current_turn_with_sender_name(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "mention_only")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请直接总结今天讨论",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        _, _, text, _ = bot.received_messages[0]
        self.assertIn("<group_chat_current_turn>", text)
        self.assertIn("sender_name: ou-user", text)
        self.assertIn("请直接总结今天讨论", text)
        self.assertNotIn("优先回复这条消息", text)

    def test_assistant_mode_keeps_history_recovered_bot_messages_in_context(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.history_entries = [
            {
                "message_id": "hist-bot",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_principal_id": "ou-other-bot",
                "sender_type": "app",
                "sender_name": "机器人:ou-other",
                "msg_type": "text",
                "text": "我建议先拆成两个任务。",
            }
        ]

        bot._handle_raw_message(
            _message_event(
                message_id="m-user",
                chat_id="chat-1",
                text="@_user_1 继续",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        self.assertIn("机器人:ou-other", bot.received_messages[0][2])
        self.assertIn("我建议先拆成两个任务。", bot.received_messages[0][2])

    def test_assistant_mode_denies_unauthorized_mention_without_consuming_boundary(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="内部讨论",
                sender_user_id="u-member",
                sender_open_id="ou-member",
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-2",
                chat_id="chat-1",
                text="@_user_1 帮我回复",
                sender_user_id="u-member",
                sender_open_id="ou-member",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertIn("尚未由管理员初始化", bot.replies[-1][1])
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1"), 0)
        self.assertEqual(bot._ingress.group_store.read_messages_between("chat-1"), [])

    def test_assistant_mode_preflight_can_block_history_recovery_before_fetch(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.allow_group_prompt_result = False

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请处理",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertEqual(bot.history_fetch_calls, [])
        self.assertEqual(bot.reply_refs, [])

    def test_assistant_mode_fetches_history_on_every_authorized_mention(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.history_entries = [
            {
                "message_id": "hist-1",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_principal_id": "ou-old-bot",
                "sender_type": "app",
                "sender_name": "机器人:ou-old-b",
                "msg_type": "text",
                "text": "第一次回捞补到的机器人消息",
            }
        ]

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 第一次总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
                create_time=1712476800000,
            )
        )

        self.assertEqual(len(bot.history_fetch_calls), 1)
        self.assertEqual(bot.history_fetch_calls[0]["after_created_at"], 0)
        self.assertEqual(bot.claim_reserved_execution_card("m-1"), "bootstrap-card-1")
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1"), 1)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_created_at("chat-1"), 1712476800000)
        self.assertIn("第一次回捞补到的机器人消息", bot.received_messages[0][2])

        bot.history_entries = [
            {
                "message_id": "hist-2",
                "created_at": 1712476900000,
                "sender_user_id": "",
                "sender_principal_id": "ou-next-bot",
                "sender_type": "app",
                "sender_name": "机器人:ou-next-",
                "msg_type": "text",
                "text": "第二次回捞补到的机器人消息",
            }
        ]
        bot._handle_raw_message(
            _message_event(
                message_id="m-2",
                chat_id="chat-1",
                text="这是两次 @ 之间的人类消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476860000,
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-3",
                chat_id="chat-1",
                text="@_user_1 第二次总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
                create_time=1712476920000,
            )
        )

        self.assertEqual(len(bot.history_fetch_calls), 2)
        self.assertEqual(bot.history_fetch_calls[1]["after_created_at"], 1712476800000)
        self.assertEqual(
            bot.history_fetch_calls[1]["after_message_ids"],
            {"m-1"},
        )
        self.assertEqual(bot.claim_reserved_execution_card("m-3"), "bootstrap-card-1")
        _, _, second_text, _ = bot.received_messages[-1]
        self.assertIn("这是两次 @ 之间的人类消息", second_text)
        self.assertIn("第二次回捞补到的机器人消息", second_text)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1"), 3)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_created_at("chat-1"), 1712476920000)

    def test_message_recalled_event_notifies_subclass(self) -> None:
        bot = self._make_bot()

        bot._on_raw_message_recalled(
            P2ImMessageRecalledV1({"event": {"chat_id": "chat-1", "message_id": "m-queued"}})
        )

        self.assertEqual(bot.recalled_messages, [("chat-1", "m-queued")])

    def test_assistant_mode_persists_boundary_message_ids_for_same_timestamp_entries(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.history_entries = [
            {
                "message_id": "hist-same-ms",
                "created_at": 1712476800000,
                "sender_user_id": "",
                "sender_principal_id": "ou-old-bot",
                "sender_type": "app",
                "sender_name": "机器人:ou-old-b",
                "msg_type": "text",
                "text": "与第一次 @ 同毫秒的机器人消息",
            }
        ]

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 第一次总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
                create_time=1712476800000,
            )
        )

        self.assertEqual(
            bot._ingress.group_store.get_last_boundary_message_ids("chat-1"),
            ["hist-same-ms", "m-1"],
        )

    def test_chat_history_fetch_applies_boundary_slack_to_start_time(self) -> None:
        bot = self._make_bot()
        captured = {}

        def fake_list(request):
            captured["start_time"] = str(getattr(request, "start_time", "") or "")
            captured["end_time"] = str(getattr(request, "end_time", "") or "")
            captured["sort_type"] = str(getattr(request, "sort_type", "") or "")
            return _HistoryResponse([])

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(list=fake_list),
                )
            )
        )

        entries = bot._ingress.history_recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=10000,
            existing_message_ids=set(),
            after_created_at=8000,
            limit=10,
        )

        self.assertEqual(entries, [])
        self.assertEqual(captured["start_time"], "3")
        self.assertEqual(captured["end_time"], "10")
        self.assertEqual(captured["sort_type"], "ByCreateTimeAsc")

    def test_history_fetch_keeps_same_timestamp_unconsumed_messages_after_boundary(self) -> None:
        bot = self._make_bot()
        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(
                        list=lambda request: _HistoryResponse(
                            [
                                _history_item(message_id="m-consumed", created_at=1000, text="已消费"),
                                _history_item(message_id="m-unconsumed", created_at=1000, text="未消费"),
                                _history_item(message_id="m-later", created_at=1001, text="更晚"),
                            ]
                        )
                    ),
                )
            )
        )

        entries = bot._ingress.history_recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=2000,
            existing_message_ids=set(),
            after_created_at=1000,
            after_message_ids={"m-consumed", "m-boundary"},
            limit=10,
        )

        self.assertEqual(
            [item["message_id"] for item in entries],
            ["m-unconsumed", "m-later"],
        )

    def test_assistant_mode_can_disable_history_fetch_by_config(self) -> None:
        bot = self._make_bot(system_config={"group_history_fetch_limit": 0})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.history_entries = [
            {
                "message_id": "hist-1",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_principal_id": "ou-old-user",
                "sender_type": "user",
                "msg_type": "text",
                "text": "不应被回捞",
            }
        ]

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 仅看实时消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.history_fetch_calls, [])
        _, _, text, _ = bot.received_messages[0]
        self.assertNotIn("不应被回捞", text)
        self.assertIn("仅看实时消息", text)

    def test_assistant_mode_reports_history_fetch_failure_and_stops(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot.history_fetch_error = RuntimeError("code=999, msg=permission denied")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertEqual(bot.patches[-1][0], "bootstrap-card-1")
        self.assertIn("群聊上下文准备失败", bot.patches[-1][1])
        self.assertIn("permission denied", bot.patches[-1][1])
        patched_card = json.loads(bot.patches[-1][1])
        self.assertTrue(patched_card["config"]["update_multi"])

    def test_assistant_mode_main_flow_ignores_thread_messages_in_context(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-main-1",
                chat_id="chat-1",
                text="主聊天流消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476800000,
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-thread-1",
                chat_id="chat-1",
                text="话题里的旧消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476810000,
                thread_id="th-1",
                root_id="root-1",
                parent_id="root-1",
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-main-2",
                chat_id="chat-1",
                text="@_user_1 主流里请总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476820000,
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        _, _, text, _ = bot.received_messages[0]
        self.assertIn("当前消息来自群主聊天流", text)
        self.assertIn("主聊天流消息", text)
        self.assertNotIn("话题里的旧消息", text)
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1", scope="main"), 3)

    def test_assistant_mode_thread_context_is_scoped_to_same_thread(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-main-1",
                chat_id="chat-1",
                text="主聊天流消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476800000,
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-thread-1",
                chat_id="chat-1",
                text="话题里的旧消息",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476810000,
                thread_id="th-1",
                root_id="root-1",
                parent_id="root-1",
            )
        )
        bot._handle_raw_message(
            _message_event(
                message_id="m-thread-2",
                chat_id="chat-1",
                text="@_user_1 话题里请总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                create_time=1712476820000,
                thread_id="th-1",
                root_id="root-1",
                parent_id="root-1",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        _, _, text, _ = bot.received_messages[0]
        self.assertIn("当前消息来自群话题内", text)
        self.assertIn("话题里的旧消息", text)
        self.assertNotIn("主聊天流消息", text)
        self.assertEqual(bot.history_fetch_calls[-1]["thread_id"], "th-1")
        self.assertEqual(bot._ingress.group_store.get_last_boundary_seq("chat-1", scope="thread:th-1"), 3)

    def test_group_history_bootstrap_card_is_shared_card(self) -> None:
        bot = self._make_bot()

        bot._ingress._prepare_group_history_execution_card("chat-1", "m-1")

        self.assertEqual(bot.reply_refs[-1][0], "m-1")
        card = json.loads(bot.reply_refs[-1][2])
        self.assertTrue(card["config"]["update_multi"])

    def test_group_reply_to_thread_message_sets_reply_in_thread(self) -> None:
        bot = self._make_bot()
        bot._process_cache.remember_message_context(
            "m-thread",
            {"thread_id": "th-1"},
        )
        bot._outbound = Mock()
        bot._outbound.reply_to_message.return_value = _confirmed_outbound(
            FeishuOutboundOperation.REPLY_MESSAGE,
            chat_id="chat-1",
            message_id="reply-1",
        )

        result = FeishuBot.reply_to_message(
            bot,
            "chat-1",
            "m-thread",
            "text",
            json.dumps({"text": "hi"}),
        )

        self.assertEqual(result.message_id, "reply-1")
        bot._outbound.reply_to_message.assert_called_once_with(
            chat_id="chat-1",
            parent_id="m-thread",
            msg_type="text",
            content=json.dumps({"text": "hi"}),
            reply_in_thread=True,
            attempt_id="",
        )

    def test_reply_local_image_reuses_thread_reply_shape(self) -> None:
        bot = self._make_bot()
        bot._process_cache.remember_message_context(
            "m-thread",
            {"thread_id": "th-1"},
        )
        bot.upload_image = lambda local_path: "img-key-1"

        message_id = FeishuBot.reply_local_image(
            bot,
            "chat-1",
            "/tmp/generated.png",
            parent_message_id="m-thread",
        )

        self.assertEqual(message_id, "bootstrap-card-1")
        self.assertEqual(
            bot.reply_refs[-1],
            (
                "m-thread",
                "image",
                json.dumps({"image_key": "img-key-1"}, ensure_ascii=False),
            ),
        )
        self.assertTrue(bot.reply_ref_thread_flags[-1])

    def test_reply_local_image_without_parent_sends_standalone_image_message(self) -> None:
        bot = self._make_bot()
        bot.upload_image = lambda local_path: "img-key-1"

        message_id = FeishuBot.reply_local_image(
            bot,
            "chat-1",
            "/tmp/generated.png",
        )

        self.assertEqual(message_id, "bootstrap-card-2")
        self.assertEqual(
            bot.sent_messages[-1],
            (
                "chat-1",
                "image",
                json.dumps({"image_key": "img-key-1"}, ensure_ascii=False),
            ),
        )

    def test_raw_handler_defensively_ignores_group_app_sender_before_logging(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": ""})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        # receive_v1 公开协议当前只承诺 sender_type=user；
        # 这里故意注入 app sender，验证 raw handler 在异常输入下仍 fail-close。
        bot._handle_raw_message(
            _message_event(
                message_id="m-app",
                chat_id="chat-1",
                text="@_user_1 机器人自己的消息",
                sender_user_id="",
                sender_open_id="ou-bot",
                sender_type="app",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        self.assertEqual(bot._ingress.group_store.read_messages_between("chat-1"), [])

    def test_forward_timeout_keeps_thread_scope(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")

        bot._ingress._buffer_forward(
            "u-user",
            "chat-1",
            "历史转发",
            "m-forward",
            "group",
            sender_user_id="u-user",
            sender_open_id="ou-user",
            sender_type="user",
            created_at=1712476800000,
            thread_id="th-1",
        )
        pending = bot._ingress.forward_aggregator.peek_pending_forward(
            "u-user", "chat-1", thread_id="th-1"
        )
        assert pending is not None
        pending.timer.cancel()
        bot._ingress.forward_aggregator.on_forward_timeout("u-user", "chat-1", "th-1")

        main_entries = bot._ingress.group_store.read_messages_between("chat-1", scope="main")
        thread_entries = bot._ingress.group_store.read_messages_between("chat-1", scope="thread:th-1")
        self.assertEqual(main_entries, [])
        self.assertEqual(len(thread_entries), 1)
        self.assertIn("历史转发", thread_entries[0]["text"])
        self.assertEqual(thread_entries[0]["created_at"], 1712476800000)

    def test_group_mention_can_use_configured_bot_open_id(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": "ou-configured"})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")
        bot._fetch_bot_open_id = lambda: (_ for _ in ()).throw(AssertionError("should not fetch"))

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-configured"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)

    def test_group_mention_can_use_configured_trigger_open_id(self) -> None:
        bot = self._make_bot(
            system_config={
                "bot_open_id": "ou-bot",
                "trigger_open_ids": ["ou-user-alias"],
            }
        )
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请代我回复",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-alias", "open_id": "ou-user-alias"},
                        "name": "ZLong",
                    }
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        self.assertIn("请代我回复", bot.received_messages[0][2])
        self.assertNotIn("@ZLong", bot.received_messages[0][2])

    def test_extract_non_bot_mentions_excludes_trigger_aliases(self) -> None:
        bot = self._make_bot(
            system_config={
                "bot_open_id": "ou-bot",
                "trigger_open_ids": ["ou-user-alias"],
            }
        )
        bot._process_cache.remember_message_context(
            "m-1",
            {
                "mentions": [
                    {
                        "open_id": "ou-user-alias",
                        "user_id": "u-alias",
                        "name": "ZLong",
                    },
                    {
                        "open_id": "ou-target",
                        "user_id": "u-target",
                        "name": "Alice",
                    },
                ]
            },
        )

        self.assertEqual(
            bot.extract_non_bot_mentions("m-1"),
            [{"open_id": "ou-target", "name": "Alice"}],
        )

    def test_group_normalization_preserves_newlines(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": "ou-bot"})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 第一行\n第二行 @_user_2",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    },
                    {
                        "key": "@_user_2",
                        "id": {"user_id": "u-other", "open_id": "ou-other"},
                        "name": "Alice",
                    },
                ],
            )
        )

        self.assertEqual(len(bot.received_messages), 1)
        self.assertIn("message:\n第一行\n第二行 @Alice", bot.received_messages[0][2])

    def test_group_mention_is_not_matched_without_bot_open_id(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": ""})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请总结",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-bot", "open_id": "ou-bot"},
                        "name": "Codex",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        logged = bot._ingress.group_store.read_messages_between("chat-1")
        self.assertEqual(len(logged), 1)
        self.assertIn("@Codex", logged[0]["text"])
        self.assertIn("请总结", logged[0]["text"])

    def test_group_trigger_alias_requires_bot_open_id(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": "", "trigger_open_ids": ["ou-user-alias"]})
        bot.set_group_mode("chat-1", "assistant")
        bot.activate_group_chat("chat-1", activated_by="ou-admin")

        bot._handle_raw_message(
            _message_event(
                message_id="m-1",
                chat_id="chat-1",
                text="@_user_1 请代答",
                sender_user_id="u-user",
                sender_open_id="ou-user",
                mentions=[
                    {
                        "key": "@_user_1",
                        "id": {"user_id": "u-alias", "open_id": "ou-user-alias"},
                        "name": "ZLong",
                    }
                ],
            )
        )

        self.assertEqual(bot.received_messages, [])
        logged = bot._ingress.group_store.read_messages_between("chat-1")
        self.assertEqual(len(logged), 1)
        self.assertIn("@ZLong", logged[0]["text"])

    def test_fetch_runtime_chat_type_uses_chat_mode_for_group_detection(self) -> None:
        bot = self._make_bot()

        class _Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(chat_mode="group", chat_type="private")

            @staticmethod
            def success() -> bool:
                return True

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    chat=SimpleNamespace(get=lambda request: _Response())
                )
            )
        )

        self.assertEqual(bot.fetch_runtime_chat_type("oc_123"), "group")
        self.assertEqual(bot.lookup_chat_type("oc_123"), "group")

    def test_get_chat_display_name_fetches_and_caches_group_name(self) -> None:
        bot = self._make_bot()
        calls = []

        class _Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(name="Project Group", chat_mode="group")

            @staticmethod
            def success() -> bool:
                return True

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    chat=SimpleNamespace(get=lambda request: calls.append(request) or _Response())
                )
            )
        )

        self.assertEqual(bot.get_chat_display_name("oc_123"), "Project Group")
        self.assertEqual(bot.get_chat_display_name("oc_123"), "Project Group")
        self.assertEqual(len(calls), 1)
        self.assertEqual(bot.lookup_chat_display_name("oc_123"), "Project Group")
        self.assertEqual(bot.lookup_chat_type("oc_123"), "group")

    def test_get_chat_display_name_returns_empty_on_api_failure(self) -> None:
        bot = self._make_bot()

        class _Response:
            code = 999
            msg = "denied"
            data = None

            @staticmethod
            def success() -> bool:
                return False

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    chat=SimpleNamespace(get=lambda request: _Response())
                )
            )
        )

        self.assertEqual(bot.get_chat_display_name("oc_123"), "")
        self.assertEqual(bot.lookup_chat_display_name("oc_123"), "")

    def test_fetch_runtime_chat_type_normalizes_topic_mode_to_group(self) -> None:
        bot = self._make_bot()

        class _Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(chat_mode="topic", chat_type="public")

            @staticmethod
            def success() -> bool:
                return True

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    chat=SimpleNamespace(get=lambda request: _Response())
                )
            )
        )

        self.assertEqual(bot.fetch_runtime_chat_type("oc_topic123"), "group")
        self.assertEqual(bot.lookup_chat_type("oc_topic123"), "group")
