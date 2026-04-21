import json
import unittest
from types import SimpleNamespace

from bot.forward_aggregator import ForwardAggregator, ForwardAggregatorPorts


class _FakeTimer:
    def __init__(self, interval, callback, args) -> None:
        self.interval = interval
        self.callback = callback
        self.args = list(args)
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class ForwardAggregatorTests(unittest.TestCase):
    def _make_aggregator(self):
        appended_logs: list[dict] = []
        handled_messages: list[tuple[str, str, str, str]] = []
        fetched_items: list[object] = []
        timers: list[_FakeTimer] = []

        def _timer_factory(interval, callback, args):
            timer = _FakeTimer(interval, callback, args)
            timers.append(timer)
            return timer

        aggregator = ForwardAggregator(
            ports=ForwardAggregatorPorts(
                get_group_mode=lambda chat_id: "assistant" if chat_id == "chat-assistant" else "all",
                append_group_log_entry=lambda **kwargs: appended_logs.append(kwargs) or 1,
                handle_forwarded_text=lambda sender_id, chat_id, text, message_id: handled_messages.append(
                    (sender_id, chat_id, text, message_id)
                ),
                fetch_merge_forward_items=lambda merge_message_id: list(fetched_items),
                batch_resolve_sender_names=lambda open_ids: {open_id: f"name:{open_id}" for open_id in open_ids},
                extract_text=lambda msg_type, content: str(content.get("text", "") if msg_type == "text" else ""),
            ),
            timer_factory=_timer_factory,
        )
        return aggregator, appended_logs, handled_messages, fetched_items, timers

    def test_buffer_forward_replaces_existing_timer_for_same_sender_and_chat(self) -> None:
        aggregator, _appended_logs, _handled_messages, _fetched_items, timers = self._make_aggregator()

        aggregator.buffer_forward("ou-1", "chat-1", "first", "m-1", "p2p")
        aggregator.buffer_forward("ou-1", "chat-1", "second", "m-2", "p2p")

        self.assertEqual(len(timers), 2)
        self.assertTrue(timers[0].started)
        self.assertTrue(timers[0].cancelled)
        self.assertTrue(timers[1].started)
        self.assertFalse(timers[1].cancelled)
        self.assertEqual(aggregator.pending_forwards[("ou-1", "chat-1")].forwarded_text, "second")

    def test_timeout_in_assistant_mode_appends_thread_scoped_log(self) -> None:
        aggregator, appended_logs, handled_messages, _fetched_items, _timers = self._make_aggregator()

        aggregator.buffer_forward(
            "ou-1",
            "chat-assistant",
            "history",
            "m-forward",
            "group",
            sender_user_id="u-1",
            sender_open_id="ou-1",
            sender_type="user",
            created_at=1712476800000,
            thread_id="th-1",
        )
        aggregator.on_forward_timeout("ou-1", "chat-assistant")

        self.assertEqual(handled_messages, [])
        self.assertEqual(len(appended_logs), 1)
        self.assertEqual(appended_logs[0]["thread_id"], "th-1")
        self.assertIn("history", appended_logs[0]["text"])

    def test_fetch_merge_forward_text_formats_nested_tree(self) -> None:
        aggregator, _appended_logs, _handled_messages, fetched_items, _timers = self._make_aggregator()
        fetched_items.extend(
            [
                SimpleNamespace(
                    message_id="sub-1",
                    upper_message_id="merge-root",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="user", id="ou-a"),
                    body=SimpleNamespace(content=json.dumps({"text": "第一条"}, ensure_ascii=False)),
                    create_time=1712476800000,
                ),
                SimpleNamespace(
                    message_id="sub-2",
                    upper_message_id="merge-root",
                    msg_type="merge_forward",
                    sender=SimpleNamespace(sender_type="user", id="ou-b"),
                    body=SimpleNamespace(content=""),
                    create_time=1712476801000,
                ),
                SimpleNamespace(
                    message_id="sub-3",
                    upper_message_id="sub-2",
                    msg_type="text",
                    sender=SimpleNamespace(sender_type="app", id="bot-1"),
                    body=SimpleNamespace(content=json.dumps({"text": "嵌套消息"}, ensure_ascii=False)),
                    create_time=1712476802000,
                ),
            ]
        )

        text = aggregator.fetch_merge_forward_text("merge-root")

        self.assertIn("name:ou-a", text)
        self.assertIn("第一条", text)
        self.assertIn("name:ou-b", text)
        self.assertIn("[forwarded messages]", text)
        self.assertIn("bot-1[机器人]", text)
        self.assertIn("嵌套消息", text)


if __name__ == "__main__":
    unittest.main()
