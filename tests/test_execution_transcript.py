import unittest

from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript


class ExecutionTranscriptTests(unittest.TestCase):
    def test_reply_segments_between_preserves_content_and_divider(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[
                ExecutionReplySegment("assistant", "first"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "second"),
            ]
        )

        rendered = transcript.reply_segments_between(2, 9)

        self.assertEqual(
            rendered,
            [
                ExecutionReplySegment("assistant", "rst"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "seco"),
            ],
        )
        self.assertEqual(transcript.reply_content_chars(), 11)

    def test_reply_segments_between_rejects_a_range_beyond_content(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "short")]
        )

        with self.assertRaisesRegex(ValueError, "exceeds"):
            transcript.reply_segments_between(0, 6)

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

    def test_snapshot_trailing_work_separates_later_live_assistant(self) -> None:
        transcript = ExecutionTranscript()

        rebuilt = transcript.rebuild_reply_from_snapshot_items(
            [
                {"type": "agentMessage", "text": "阶段说明"},
                {"type": "commandExecution"},
            ]
        )
        transcript.append_assistant_delta("最终答案")

        self.assertTrue(rebuilt)
        self.assertEqual(
            transcript.reply_segments,
            [
                ExecutionReplySegment("assistant", "阶段说明"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "最终答案"),
            ],
        )

    def test_empty_completed_assistant_does_not_promote_prior_commentary(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text("阶段说明")
        transcript.start_process_block("tool", marks_work=True)
        transcript.finish_process_block()

        self.assertIsNone(transcript.terminal_reply_evidence())

        transcript.reconcile_current_assistant_text("")

        self.assertEqual(transcript.reply_text(), "阶段说明")
        self.assertEqual(transcript.terminal_reply_evidence(), ("agent", ""))

    def test_terminal_error_is_distinct_from_agent_completion(self) -> None:
        transcript = ExecutionTranscript()

        transcript.record_terminal_error("provider unavailable")

        self.assertEqual(
            transcript.terminal_reply_evidence(),
            ("error", "provider unavailable"),
        )

    def test_terminal_error_wins_over_empty_agent_in_both_event_orders(self) -> None:
        error_then_empty = ExecutionTranscript()
        error_then_empty.record_terminal_error("provider unavailable")
        error_then_empty.reconcile_current_assistant_text("")

        empty_then_error = ExecutionTranscript()
        empty_then_error.reconcile_current_assistant_text("")
        empty_then_error.record_terminal_error("provider unavailable")

        for transcript in (error_then_empty, empty_then_error):
            self.assertEqual(
                transcript.terminal_reply_evidence(),
                ("error", "provider unavailable"),
            )

    def test_snapshot_display_rebuild_does_not_invent_completion_evidence(self) -> None:
        transcript = ExecutionTranscript()

        rebuilt = transcript.rebuild_reply_from_snapshot_items(
            [
                {"type": "agentMessage", "text": "阶段说明"},
                {"type": "commandExecution"},
                {"type": "agentMessage"},
            ]
        )

        self.assertTrue(rebuilt)
        self.assertEqual(transcript.reply_text(), "阶段说明")
        self.assertIsNone(transcript.terminal_reply_evidence())

    def test_later_shorter_completion_replaces_terminal_evidence(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text("权威的较长完成文本")

        accepted = transcript.reconcile_current_assistant_text("短文本")

        self.assertTrue(accepted)
        self.assertEqual(transcript.reply_text(), "权威的较长完成文本\n\n短文本")
        self.assertEqual(
            transcript.terminal_reply_evidence(),
            ("agent", "短文本"),
        )

    def test_short_final_after_explicit_commentary_starts_new_segment(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "这是一段明显长于最终回复的阶段说明",
            terminal_candidate=False,
        )

        accepted = transcript.reconcile_current_assistant_text(
            "完成",
            terminal_candidate=True,
        )

        self.assertTrue(accepted)
        self.assertEqual(
            transcript.reply_text(),
            "这是一段明显长于最终回复的阶段说明\n\n完成",
        )
        self.assertEqual(transcript.terminal_reply_evidence(), ("agent", "完成"))


if __name__ == "__main__":
    unittest.main()
