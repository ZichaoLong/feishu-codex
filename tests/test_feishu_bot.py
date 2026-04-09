import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

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

    def on_message(self, sender_id: str, chat_id: str, text: str, message_id: str = "") -> None:
        self.received_messages.append((sender_id, chat_id, text, message_id))

    def on_card_action(self, user_id: str, chat_id: str, message_id: str, action_value: dict):
        return self.make_card_response()

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

    def _fetch_group_history_entries(
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


class FeishuBotGroupModeTests(unittest.TestCase):
    def _make_bot(self, *, system_config: dict | None = None) -> _RecordingBot:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return _RecordingBot(pathlib.Path(tempdir.name), system_config=system_config)

    def test_assistant_mode_logs_plain_group_message_without_triggering(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")

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
        bot.set_group_access_policy("chat-1", "all-members")

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
        self.assertIn("请总结一下", text)
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 2)

    def test_assistant_mode_keeps_history_recovered_bot_messages_in_context(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.set_group_access_policy("chat-1", "all-members")
        bot.history_entries = [
            {
                "message_id": "hist-bot",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_open_id": "ou-other-bot",
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
        self.assertIn("仅管理员或已授权成员", bot.replies[-1][1])
        self.assertEqual(bot._group_store.get_last_boundary_seq("chat-1"), 0)

    def test_assistant_mode_fetches_history_on_every_authorized_mention(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.set_group_access_policy("chat-1", "all-members")
        bot.history_entries = [
            {
                "message_id": "hist-1",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_open_id": "ou-old-bot",
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
                "sender_open_id": "ou-next-bot",
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

    def test_assistant_mode_persists_boundary_message_ids_for_same_timestamp_entries(self) -> None:
        bot = self._make_bot()
        bot.set_group_mode("chat-1", "assistant")
        bot.set_group_access_policy("chat-1", "all-members")
        bot.history_entries = [
            {
                "message_id": "hist-same-ms",
                "created_at": 1712476800000,
                "sender_user_id": "",
                "sender_open_id": "ou-old-bot",
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

        entries = FeishuBot._fetch_group_history_entries(
            bot,
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=5000,
            existing_message_ids=set(),
            after_created_at=0,
            limit=2,
        )

        self.assertEqual(calls, ["", "next-1"])
        self.assertEqual([item["message_id"] for item in entries], ["hist-3", "hist-4"])

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

        entries = FeishuBot._fetch_group_history_entries(
            bot,
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
        bot.set_group_access_policy("chat-1", "all-members")
        bot.history_entries = [
            {
                "message_id": "hist-1",
                "created_at": 1712476700000,
                "sender_user_id": "",
                "sender_open_id": "ou-old-user",
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
        bot.set_group_access_policy("chat-1", "all-members")
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
        bot.set_group_access_policy("chat-1", "all-members")

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
        bot.set_group_access_policy("chat-1", "all-members")

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

    def test_assistant_mode_ignores_group_app_events_before_logging(self) -> None:
        bot = self._make_bot(system_config={"bot_open_id": ""})
        bot.set_group_mode("chat-1", "assistant")
        bot.set_group_access_policy("chat-1", "all-members")

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
        with bot._pending_forwards_lock:
            pending = bot._pending_forwards[("u-user", "chat-1")]
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
        bot.set_group_access_policy("chat-1", "all-members")
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
        bot.set_group_access_policy("chat-1", "all-members")

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
            [{"open_id": "ou-target", "user_id": "u-target", "name": "Alice"}],
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
        bot.set_group_access_policy("chat-1", "all-members")

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
        bot.set_group_access_policy("chat-1", "all-members")

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

    def test_fetch_chat_type_prefers_chat_type_over_chat_mode(self) -> None:
        bot = self._make_bot()

        class _Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(chat_mode="thread", chat_type="group")

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

        self.assertEqual(bot.fetch_chat_type("oc_123"), "group")
        self.assertEqual(bot.lookup_chat_type("oc_123"), "group")

    def test_fetch_chat_type_does_not_use_chat_mode_when_chat_type_missing(self) -> None:
        bot = self._make_bot()

        class _Response:
            code = 0
            msg = "ok"
            data = SimpleNamespace(chat_mode="thread", chat_type="")

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

        self.assertEqual(bot.fetch_chat_type("oc_thread123"), "")
        self.assertEqual(bot.lookup_chat_type("oc_thread123"), "")
