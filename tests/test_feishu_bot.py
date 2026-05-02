import json
import pathlib
import tempfile
import time
import unittest
from types import SimpleNamespace

from lark_oapi.api.im.v1 import (
    P2ImChatDisbandedV1,
    P2ImChatMemberBotDeletedV1,
    P2ImMessageReceiveV1,
)

from bot.cards import build_execution_card, build_terminal_result_card
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_bot import FeishuBot


class _RecordingBot(FeishuBot):
    def __init__(self, data_dir: pathlib.Path, *, system_config: dict | None = None) -> None:
        config = {"admin_open_ids": ["ou-admin"], "bot_open_id": "ou-bot"}
        if system_config:
            config.update(system_config)
        super().__init__(
            "app-id",
            "app-secret",
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
        self.allow_group_prompt_result = True
        self.chat_unavailable_events: list[tuple[str, str]] = []

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

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.sent_messages.append((chat_id, msg_type, content))
        return "bootstrap-card-2"

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_refs.append((parent_id, msg_type, content))
        self.reply_ref_thread_flags.append(reply_in_thread)
        return "bootstrap-card-1"

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return True

    def _resolve_sender_name(self, open_id: str) -> str:
        return open_id[:8]

    def allow_group_prompt(self, sender_id: str, chat_id: str, *, message_id: str = "") -> bool:
        del sender_id
        del chat_id
        del message_id
        return bool(self.allow_group_prompt_result)

    def on_chat_unavailable(self, chat_id: str, *, reason: str = "") -> None:
        self.chat_unavailable_events.append((chat_id, reason))

    def _collect_assistant_context_entries(
        self,
        *,
        chat_id: str,
        current_message_id: str,
        current_create_time,
        current_seq: int,
        thread_id: str = "",
    ) -> list[dict]:
        original_fetch = self._history_recovery.fetch_group_history_entries
        self._history_recovery.fetch_group_history_entries = self._recorded_group_history_entries
        try:
            return super()._collect_assistant_context_entries(
                chat_id=chat_id,
                current_message_id=current_message_id,
                current_create_time=current_create_time,
                current_seq=current_seq,
                thread_id=thread_id,
            )
        finally:
            self._history_recovery.fetch_group_history_entries = original_fetch

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


class FeishuBotCardProjectionTests(unittest.TestCase):
    def _make_bot(self) -> _RecordingBot:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return _RecordingBot(pathlib.Path(tempdir.name))

    def test_p2p_terminal_result_card_projects_authoritative_text(self) -> None:
        bot = self._make_bot()

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

    def test_history_entry_projects_interactive_terminal_result_from_other_app_sender(self) -> None:
        bot = self._make_bot()

        entry = bot._history_recovery.history_entry_from_message(
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
        self.assertEqual(bot._group_store.read_messages_between("chat-1"), [])

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
        logged = bot._group_store.read_messages_between("chat-1")
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["text"], "第一条讨论")

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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 2)

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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 0)
        self.assertEqual(bot._group_store.read_messages_between("chat-1"), [])

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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 1)
        self.assertEqual(bot._group_store.get_last_boundary_created_at("chat-1"), 1712476800000)
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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 3)
        self.assertEqual(bot._group_store.get_last_boundary_created_at("chat-1"), 1712476920000)

    def test_chat_disbanded_event_clears_local_group_state_and_notifies_subclass(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "all")
        bot._group_store.append_message(
            "chat-1",
            {
                "message_id": "m-1",
                "created_at": 1,
                "sender_user_id": "u-1",
                "sender_principal_id": "ou-1",
                "sender_type": "user",
                "sender_name": "User",
                "msg_type": "text",
                "thread_id": "",
                "text": "hello",
            },
        )
        bot.remember_chat_type("chat-1", "group")

        bot._on_raw_chat_disbanded(P2ImChatDisbandedV1({"event": {"chat_id": "chat-1"}}))

        self.assertEqual(bot.get_group_mode("chat-1"), "assistant")
        self.assertFalse(bot._group_store.log_path("chat-1").exists())
        self.assertEqual(bot.lookup_chat_type("chat-1"), "")
        self.assertEqual(bot.chat_unavailable_events[-1], ("chat-1", "disbanded"))

    def test_bot_deleted_event_clears_local_group_state_and_notifies_subclass(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "all")

        bot._on_raw_chat_member_bot_deleted(P2ImChatMemberBotDeletedV1({"event": {"chat_id": "chat-1"}}))

        self.assertEqual(bot.get_group_mode("chat-1"), "assistant")
        self.assertEqual(bot.chat_unavailable_events[-1], ("chat-1", "bot_removed"))

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
            bot._group_store.get_last_boundary_message_ids("chat-1"),
            ["hist-same-ms", "m-1"],
        )

    def test_history_fetch_prefers_most_recent_missing_entries_within_limit(self) -> None:
        bot = self._make_bot(system_config={"group_history_fetch_limit": 2})
        responses = {
            "": _HistoryResponse(
                [
                    _history_item(message_id="hist-1", created_at=1000, text="第一条"),
                    _history_item(message_id="hist-2", created_at=2000, text="第二条"),
                ],
                has_more=True,
                page_token="next-1",
            ),
            "next-1": _HistoryResponse(
                [
                    _history_item(message_id="hist-3", created_at=3000, text="第三条"),
                    _history_item(message_id="hist-4", created_at=4000, text="第四条"),
                ],
            ),
        }
        calls: list[str] = []

        def fake_list(request):
            token = str(getattr(request, "page_token", "") or "")
            calls.append(token)
            return responses[token]

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(list=fake_list),
                )
            )
        )

        entries = bot._history_recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=5000,
            existing_message_ids=set(),
            after_created_at=0,
            limit=2,
        )

        self.assertEqual(calls, ["", "next-1"])
        self.assertEqual([item["message_id"] for item in entries], ["hist-3", "hist-4"])

    def test_thread_history_fetch_uses_desc_scan_and_stops_at_boundary(self) -> None:
        bot = self._make_bot()
        responses = {
            "": _HistoryResponse(
                [
                    _history_item(message_id="hist-6", created_at=6000, text="第六条", thread_id="thread-1"),
                    _history_item(message_id="hist-5", created_at=5000, text="第五条", thread_id="thread-1"),
                ],
                has_more=True,
                page_token="next-1",
            ),
            "next-1": _HistoryResponse(
                [
                    _history_item(message_id="m-boundary", created_at=3000, text="边界消息", thread_id="thread-1"),
                    _history_item(message_id="hist-old", created_at=2000, text="过旧消息", thread_id="thread-1"),
                ],
                has_more=True,
                page_token="next-2",
            ),
        }
        calls: list[tuple[str, str]] = []

        def fake_list(request):
            token = str(getattr(request, "page_token", "") or "")
            sort_type = str(getattr(request, "sort_type", "") or "")
            calls.append((token, sort_type))
            return responses[token]

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(list=fake_list),
                )
            )
        )

        entries = bot._history_recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=7000,
            existing_message_ids=set(),
            after_created_at=3000,
            after_message_ids={"m-boundary"},
            thread_id="thread-1",
            limit=10,
        )

        self.assertEqual(calls, [("", "ByCreateTimeDesc"), ("next-1", "ByCreateTimeDesc")])
        self.assertEqual([item["message_id"] for item in entries], ["hist-5", "hist-6"])

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

        entries = bot._history_recovery.fetch_group_history_entries(
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

        entries = bot._history_recovery.fetch_group_history_entries(
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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1", scope="main"), 3)

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
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1", scope="thread:th-1"), 3)

    def test_group_history_bootstrap_card_is_shared_card(self) -> None:
        bot = self._make_bot()

        bot._prepare_group_history_execution_card("chat-1", "m-1")

        self.assertEqual(bot.reply_refs[-1][0], "m-1")
        card = json.loads(bot.reply_refs[-1][2])
        self.assertTrue(card["config"]["update_multi"])

    def test_group_reply_to_thread_message_sets_reply_in_thread(self) -> None:
        bot = self._make_bot()
        bot._remember_message_context("m-thread", {"thread_id": "th-1"})
        captured: list = []

        class _Response:
            @staticmethod
            def success() -> bool:
                return True

            data = SimpleNamespace(message_id="reply-1")

        def fake_reply(request):
            captured.append(request)
            return _Response()

        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(reply=fake_reply),
                )
            )
        )

        reply_id = FeishuBot.reply_to_message(bot, "m-thread", "text", json.dumps({"text": "hi"}))

        self.assertEqual(reply_id, "reply-1")
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].request_body.reply_in_thread)

    def test_get_message_context_returns_empty_after_entry_expires(self) -> None:
        bot = self._make_bot()
        bot._remember_message_context("m-ctx", {"thread_id": "th-1"})
        bot._message_contexts["m-ctx"].created_at = time.time() - 601

        self.assertEqual(bot.get_message_context("m-ctx"), {})

    def test_lookup_chat_type_returns_empty_after_entry_expires(self) -> None:
        bot = self._make_bot()
        bot.remember_chat_type("chat-1", "group")
        bot._chat_type_cache["chat-1"].created_at = time.time() - (24 * 3600 + 1)

        self.assertEqual(bot.lookup_chat_type("chat-1"), "")

    def test_claim_reserved_execution_card_returns_empty_after_entry_expires(self) -> None:
        bot = self._make_bot()
        bot.reserve_execution_card("m-1", "card-1")
        bot._pending_execution_cards["m-1"].created_at = time.time() - 601

        self.assertEqual(bot.claim_reserved_execution_card("m-1"), "")

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
        self.assertEqual(bot._group_store.read_messages_between("chat-1"), [])

    def test_forward_timeout_keeps_thread_scope(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")

        bot._buffer_forward(
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
        pending = bot._forward_aggregator.peek_pending_forward("u-user", "chat-1")
        assert pending is not None
        pending.timer.cancel()
        bot._on_forward_timeout("u-user", "chat-1")

        main_entries = bot._group_store.read_messages_between("chat-1", scope="main")
        thread_entries = bot._group_store.read_messages_between("chat-1", scope="thread:th-1")
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
        bot._remember_message_context(
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

    def test_group_normalization_keeps_non_trigger_mentions(self) -> None:
        bot = self._make_bot(
            system_config={
                "bot_open_id": "ou-bot",
                "trigger_open_ids": ["ou-user-alias"],
            }
        )

        normalized = bot._normalize_mentions(
            "@_user_1 请和 @_user_2 一起看",
            [
                {
                    "key": "@_user_1",
                    "open_id": "ou-user-alias",
                    "name": "ZLong",
                },
                {
                    "key": "@_user_2",
                    "open_id": "ou-other",
                    "name": "Alice",
                },
            ],
        )

        self.assertEqual(normalized, "请和 @Alice 一起看")

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
        logged = bot._group_store.read_messages_between("chat-1")
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
        logged = bot._group_store.read_messages_between("chat-1")
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

    def test_history_entry_uses_sender_principal_id_for_app_sender(self) -> None:
        bot = self._make_bot()

        entry = bot._history_recovery.history_entry_from_message(
            _history_item(
                message_id="hist-app",
                created_at=1712476800000,
                text="来自其他机器人的历史消息",
                sender_id="cli_a1b2c3",
                sender_type="app",
            )
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["sender_principal_id"], "cli_a1b2c3")
        self.assertEqual(entry["sender_type"], "app")

    def test_history_entry_skips_self_app_sender(self) -> None:
        bot = self._make_bot()
        bot.app_id = "cli_self_bot"

        entry = bot._history_recovery.history_entry_from_message(
            _history_item(
                message_id="hist-self-app",
                created_at=1712476800000,
                text="Codex Bot 自己发的卡片",
                sender_id="cli_self_bot",
                sender_type="app",
            )
        )

        self.assertIsNone(entry)

    def test_history_fetch_filters_self_app_messages(self) -> None:
        bot = self._make_bot()
        bot.app_id = "cli_self_bot"
        bot.client = SimpleNamespace(
            im=SimpleNamespace(
                v1=SimpleNamespace(
                    message=SimpleNamespace(
                        list=lambda request: _HistoryResponse(
                            [
                                _history_item(
                                    message_id="hist-self-app",
                                    created_at=1000,
                                    text="自己发的卡片",
                                    sender_id="cli_self_bot",
                                    sender_type="app",
                                ),
                                _history_item(
                                    message_id="hist-other-app",
                                    created_at=1001,
                                    text="其他机器人消息",
                                    sender_id="cli_other_bot",
                                    sender_type="app",
                                ),
                                _history_item(
                                    message_id="hist-user",
                                    created_at=1002,
                                    text="普通用户消息",
                                ),
                            ]
                        )
                    )
                )
            )
        )

        entries = bot._history_recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=2000,
            existing_message_ids=set(),
            after_created_at=0,
            limit=10,
        )

        self.assertEqual(
            [item["message_id"] for item in entries],
            ["hist-other-app", "hist-user"],
        )
