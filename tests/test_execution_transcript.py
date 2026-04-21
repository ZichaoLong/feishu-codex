import unittest

from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript


class ExecutionTranscriptTests(unittest.TestCase):
    def test_reply_segments_for_card_keeps_final_text_within_limit(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "x" * 80)]
        )

        rendered = transcript.reply_segments_for_card(40)

        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0].kind, "assistant")
        self.assertLessEqual(len(rendered[0].text), 40)
        self.assertIn("执行卡仅显示部分内容", rendered[0].text)

    def test_reply_segments_for_card_uses_compact_notice_for_tiny_limit(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "x" * 80)]
        )

        rendered = transcript.reply_segments_for_card(6)

        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0].text, "[回复过长]")
        self.assertLessEqual(len(rendered[0].text), 6)

    def test_rebuild_reply_from_snapshot_items_can_drop_terminal_final_message(self) -> None:
        transcript = ExecutionTranscript()

        rebuilt = transcript.rebuild_reply_from_snapshot_items(
            [
                {"type": "agentMessage", "text": "阶段总结"},
                {"type": "commandExecution"},
                {"type": "agentMessage", "text": "最终答案"},
            ],
            drop_last_text_message=True,
        )

        self.assertTrue(rebuilt)
        self.assertEqual(
            transcript.reply_segments,
            [ExecutionReplySegment("assistant", "阶段总结")],
        )

    def test_rebuild_reply_from_snapshot_items_drop_last_message_can_leave_empty_display(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "stale")]
        )

        rebuilt = transcript.rebuild_reply_from_snapshot_items(
            [{"type": "agentMessage", "text": "最终答案"}],
            drop_last_text_message=True,
        )

        self.assertFalse(rebuilt)
        self.assertEqual(transcript.reply_text(), "stale")


if __name__ == "__main__":
    unittest.main()
