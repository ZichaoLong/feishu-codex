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
        self.assertIn("完整内容已另行发送为文本消息", rendered[0].text)

    def test_reply_segments_for_card_uses_compact_notice_for_tiny_limit(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "x" * 80)]
        )

        rendered = transcript.reply_segments_for_card(6)

        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0].text, "[回复过长]")
        self.assertLessEqual(len(rendered[0].text), 6)


if __name__ == "__main__":
    unittest.main()
