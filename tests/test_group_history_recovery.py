import json
import pathlib
import unittest
from types import SimpleNamespace

from bot.group_history_recovery import (
    GroupHistoryRecovery,
    GroupHistoryRecoveryPorts,
    ListedMessagesPage,
)


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


class GroupHistoryRecoveryTests(unittest.TestCase):
    def _make_recovery(
        self,
        *,
        responses: dict[str, ListedMessagesPage] | None = None,
        local_entries: list[dict] | None = None,
        boundary_seq: int = 0,
        boundary_created_at: int = 0,
        boundary_message_ids: list[str] | None = None,
        app_id: str = "app-id",
        history_fetch_limit: int = 50,
    ):
        calls: list[dict] = []
        response_pages = responses or {"": ListedMessagesPage([])}

        def list_messages(**kwargs):
            calls.append(dict(kwargs))
            return response_pages[str(kwargs.get("page_token", "") or "")]

        recovery = GroupHistoryRecovery(
            ports=GroupHistoryRecoveryPorts(
                list_messages=list_messages,
                render_message_text=lambda msg_type, content: str(content.get("text", "") if msg_type == "text" else ""),
                normalize_mentions=lambda text, _mentions: " ".join(text.split()),
                mention_payloads=lambda mentions: list(mentions),
                display_name_for_sender_identity=lambda **kwargs: str(
                    kwargs.get("sender_principal_id") or kwargs.get("user_id") or "unknown"
                ),
                read_local_messages_between=lambda _chat_id, *, after_seq, before_seq, scope: [
                    dict(item) for item in local_entries or []
                ],
                get_last_boundary_seq=lambda _chat_id, *, scope: boundary_seq,
                get_last_boundary_created_at=lambda _chat_id, *, scope: boundary_created_at,
                get_last_boundary_message_ids=lambda _chat_id, *, scope: list(boundary_message_ids or []),
            ),
            app_id=app_id,
            history_fetch_limit=history_fetch_limit,
        )
        return recovery, calls

    def test_chat_fetch_prefers_recent_missing_entries_within_limit(self) -> None:
        recovery, calls = self._make_recovery(
            responses={
                "": ListedMessagesPage(
                    [
                        _history_item(message_id="hist-1", created_at=1000, text="第一条"),
                        _history_item(message_id="hist-2", created_at=2000, text="第二条"),
                    ],
                    has_more=True,
                    page_token="next-1",
                ),
                "next-1": ListedMessagesPage(
                    [
                        _history_item(message_id="hist-3", created_at=3000, text="第三条"),
                        _history_item(message_id="hist-4", created_at=4000, text="第四条"),
                    ],
                ),
            },
            history_fetch_limit=2,
        )

        entries = recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=5000,
            existing_message_ids=set(),
            after_created_at=0,
            limit=2,
        )

        self.assertEqual([call["page_token"] for call in calls], ["", "next-1"])
        self.assertEqual([item["message_id"] for item in entries], ["hist-3", "hist-4"])

    def test_thread_fetch_uses_desc_scan_and_stops_at_boundary(self) -> None:
        recovery, calls = self._make_recovery(
            responses={
                "": ListedMessagesPage(
                    [
                        _history_item(message_id="hist-6", created_at=6000, text="第六条", thread_id="thread-1"),
                        _history_item(message_id="hist-5", created_at=5000, text="第五条", thread_id="thread-1"),
                    ],
                    has_more=True,
                    page_token="next-1",
                ),
                "next-1": ListedMessagesPage(
                    [
                        _history_item(message_id="m-boundary", created_at=3000, text="边界", thread_id="thread-1"),
                        _history_item(message_id="hist-old", created_at=2000, text="过旧", thread_id="thread-1"),
                    ],
                    has_more=True,
                    page_token="next-2",
                ),
            }
        )

        entries = recovery.fetch_group_history_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=7000,
            existing_message_ids=set(),
            after_created_at=3000,
            after_message_ids={"m-boundary"},
            thread_id="thread-1",
            limit=10,
        )

        self.assertEqual(
            [(call["page_token"], call["sort_type"]) for call in calls],
            [("", "ByCreateTimeDesc"), ("next-1", "ByCreateTimeDesc")],
        )
        self.assertEqual([item["message_id"] for item in entries], ["hist-5", "hist-6"])

    def test_collect_context_uses_scoped_boundary_and_fetcher(self) -> None:
        fetch_calls: list[dict] = []

        def fetcher(**kwargs):
            fetch_calls.append(dict(kwargs))
            return [
                {
                    "message_id": "hist-1",
                    "created_at": 2000,
                    "sender_user_id": "",
                    "sender_principal_id": "ou-history",
                    "sender_type": "user",
                    "sender_name": "history",
                    "msg_type": "text",
                    "thread_id": "th-1",
                    "text": "history",
                }
            ]

        recovery, _calls = self._make_recovery(
            local_entries=[
                {
                    "message_id": "local-1",
                    "created_at": 3000,
                    "sender_user_id": "",
                    "sender_principal_id": "ou-local",
                    "sender_type": "user",
                    "sender_name": "local",
                    "msg_type": "text",
                    "thread_id": "th-1",
                    "text": "local",
                    "seq": 2,
                }
            ],
            boundary_seq=1,
            boundary_created_at=1000,
            boundary_message_ids=["m-boundary"],
        )
        recovery.fetch_group_history_entries = fetcher

        entries = recovery.collect_assistant_context_entries(
            chat_id="chat-1",
            current_message_id="m-current",
            current_create_time=4000,
            current_seq=5,
            thread_id="th-1",
        )

        self.assertEqual(fetch_calls[0]["after_created_at"], 1000)
        self.assertEqual(fetch_calls[0]["after_message_ids"], {"m-boundary"})
        self.assertEqual(fetch_calls[0]["existing_message_ids"], {"local-1"})
        self.assertEqual(fetch_calls[0]["thread_id"], "th-1")
        self.assertEqual([item["message_id"] for item in entries], ["hist-1", "local-1"])

    def test_history_entry_skips_self_app_sender_only(self) -> None:
        recovery, _calls = self._make_recovery(app_id="cli_self_bot")

        self.assertIsNone(
            recovery.history_entry_from_message(
                _history_item(
                    message_id="self-app",
                    created_at=1000,
                    text="self",
                    sender_id="cli_self_bot",
                    sender_type="app",
                )
            )
        )
        entry = recovery.history_entry_from_message(
            _history_item(
                message_id="other-app",
                created_at=1001,
                text="other",
                sender_id="cli_other_bot",
                sender_type="app",
            )
        )

        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["sender_principal_id"], "cli_other_bot")
        self.assertEqual(entry["sender_type"], "app")

    def test_build_assistant_turn_text_records_thread_scope(self) -> None:
        recovery, _calls = self._make_recovery()

        text = recovery.build_assistant_turn_text(
            "历史上下文",
            "",
            pathlib.Path("/tmp/group.log"),
            thread_id="th-1",
            current_sender_name="Alice",
        )

        self.assertIn("当前消息来自群话题内", text)
        self.assertIn("当前话题 ID：`th-1`", text)
        self.assertIn("历史上下文", text)
        self.assertIn("<group_chat_current_turn>", text)
        self.assertIn("sender_name: Alice", text)

    def test_build_group_current_turn_text_uses_sender_name_and_placeholder(self) -> None:
        recovery, _calls = self._make_recovery()

        text = recovery.build_group_current_turn_text("", sender_name="Alice")

        self.assertIn("<group_chat_current_turn>", text)
        self.assertIn("sender_name: Alice", text)
        self.assertIn("发送者没有提供额外文本", text)

    def test_build_group_turn_text_is_neutral(self) -> None:
        recovery, _calls = self._make_recovery()

        text = recovery.build_group_turn_text("请总结", sender_name="Alice")

        self.assertIn("<group_chat_current_turn>", text)
        self.assertIn("sender_name: Alice", text)
        self.assertIn("请总结", text)
        self.assertNotIn("优先回复这条消息", text)


if __name__ == "__main__":
    unittest.main()
