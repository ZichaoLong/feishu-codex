import unittest
from dataclasses import FrozenInstanceError

from bot.execution_transcript import (
    ExecutionReplySegment,
    ExecutionReplySegmentSnapshot,
    ExecutionTranscript,
    ExecutionTranscriptSnapshot,
)


class ExecutionTranscriptSnapshotTests(unittest.TestCase):
    def test_snapshot_copies_owner_values_into_frozen_tuple_vocabulary(self) -> None:
        first = ExecutionReplySegment("assistant", "阶段总结")
        divider = ExecutionReplySegment("divider")
        final = ExecutionReplySegment("assistant", "最终答案")
        transcript = ExecutionTranscript(
            reply_segments=[first, divider, final],
            process_blocks=["执行命令\n", "完成\n"],
            _active_reply_index=2,
            _pending_reply_divider=True,
            _had_assistant_output=True,
        )

        snapshot = transcript.snapshot()

        self.assertIsInstance(snapshot, ExecutionTranscriptSnapshot)
        self.assertIsInstance(snapshot.reply_segments, tuple)
        self.assertIsInstance(snapshot.process_blocks, tuple)
        self.assertEqual(
            snapshot.reply_segments,
            (
                ExecutionReplySegmentSnapshot("assistant", "阶段总结"),
                ExecutionReplySegmentSnapshot("divider"),
                ExecutionReplySegmentSnapshot("assistant", "最终答案"),
            ),
        )
        self.assertIsNot(snapshot.reply_segments[0], first)
        self.assertIsNot(snapshot.reply_segments[1], divider)
        self.assertIsNot(snapshot.reply_segments[2], final)
        self.assertEqual(snapshot.reply_text(), "阶段总结\n\n最终答案")
        self.assertEqual(snapshot.process_text(), "执行命令\n完成\n")
        self.assertTrue(snapshot.has_reply_output())
        self.assertTrue(snapshot.has_process_output())
        self.assertEqual(snapshot.active_reply_index, 2)
        self.assertTrue(snapshot.pending_reply_divider)

    def test_owner_mutation_after_capture_cannot_change_snapshot(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "captured reply")],
            process_blocks=["captured process"],
            _active_reply_index=0,
            _had_assistant_output=True,
        )
        snapshot = transcript.snapshot()

        transcript.reply_segments[0] = ExecutionReplySegment(
            "assistant",
            "mutated reply",
        )
        transcript.reply_segments.append(ExecutionReplySegment("divider"))
        transcript.process_blocks[0] = "mutated process"
        transcript.process_blocks.append("more")
        transcript.reset()

        self.assertEqual(snapshot.reply_text(), "captured reply")
        self.assertEqual(snapshot.process_text(), "captured process")
        self.assertEqual(snapshot.active_reply_index, 0)
        self.assertTrue(snapshot.had_assistant_output)

    def test_snapshot_rejects_mutable_or_untyped_reachable_values(self) -> None:
        snapshot = ExecutionTranscriptSnapshot(
            reply_segments=(ExecutionReplySegmentSnapshot("assistant", "done"),),
            process_blocks=("log",),
        )

        with self.assertRaises(FrozenInstanceError):
            snapshot.process_blocks = ()  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            snapshot.reply_segments[0].text = "changed"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(
                reply_segments=[ExecutionReplySegmentSnapshot("assistant", "x")],  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(
                process_blocks=({"mutable": True},),  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            ExecutionReplySegmentSnapshot("assistant", ["mutable"])  # type: ignore[arg-type]

        class TupleSubclass(tuple):
            pass

        class StringSubclass(str):
            pass

        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(
                reply_segments=TupleSubclass(
                    (ExecutionReplySegmentSnapshot("assistant", "x"),)
                ),
            )
        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(process_blocks=(StringSubclass("x"),))

    def test_snapshot_rejects_invalid_cursor_and_boolean_shapes(self) -> None:
        reply = (ExecutionReplySegmentSnapshot("assistant", "done"),)

        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(
                reply_segments=reply,
                active_reply_index=True,  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            ExecutionTranscriptSnapshot(
                reply_segments=reply,
                active_reply_index=1,
            )
        with self.assertRaises(ValueError):
            ExecutionTranscriptSnapshot(
                reply_segments=(ExecutionReplySegmentSnapshot("divider"),),
                active_reply_index=0,
            )
        with self.assertRaises(ValueError):
            ExecutionTranscriptSnapshot(
                reply_segments=reply,
                process_blocks=("log",),
                active_reply_index=0,
                active_process_index=0,
            )
        with self.assertRaises(TypeError):
            ExecutionTranscriptSnapshot(pending_reply_divider=1)  # type: ignore[arg-type]

    def test_snapshot_rejects_corrupt_owner_without_coercion(self) -> None:
        transcript = ExecutionTranscript()
        transcript.process_blocks = [{"mutable": True}]  # type: ignore[list-item]

        with self.assertRaises(TypeError):
            transcript.snapshot()

    def test_snapshot_to_transcript_returns_a_detached_mutable_owner(self) -> None:
        owner_segment = ExecutionReplySegment("assistant", "hello")
        transcript = ExecutionTranscript(
            reply_segments=[owner_segment],
            process_blocks=["process"],
            _active_reply_index=0,
            _had_assistant_output=True,
        )
        snapshot = transcript.snapshot()

        restored = snapshot.to_transcript()

        self.assertIsInstance(restored.reply_segments, list)
        self.assertIsInstance(restored.process_blocks, list)
        self.assertIsNot(restored.reply_segments, transcript.reply_segments)
        self.assertIsNot(restored.process_blocks, transcript.process_blocks)
        self.assertIsNot(restored.reply_segments[0], owner_segment)
        self.assertIsNot(restored.reply_segments[0], snapshot.reply_segments[0])
        restored.append_assistant_delta(" world")
        restored.append_process_note(" changed")

        self.assertEqual(restored.reply_text(), "hello world")
        self.assertEqual(snapshot.reply_text(), "hello")
        self.assertEqual(snapshot.process_text(), "process")
        self.assertEqual(transcript.reply_text(), "hello")
        self.assertEqual(transcript.process_text(), "process")

    def test_snapshot_page_projection_matches_mutable_owner(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[
                ExecutionReplySegment("assistant", "first"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "second"),
            ],
        )

        self.assertEqual(
            transcript.snapshot().reply_segments_between(2, 8),
            tuple(transcript.reply_segments_between(2, 8)),
        )

    def test_snapshot_round_trip_preserves_empty_completion_evidence(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text("阶段说明")
        transcript.start_process_block("tool", marks_work=True)
        transcript.finish_process_block()
        transcript.reconcile_current_assistant_text("")

        snapshot = transcript.snapshot()
        restored = snapshot.to_transcript()

        self.assertEqual(snapshot.last_completed_assistant_text, "")
        self.assertEqual(restored.reply_text(), "阶段说明")
        self.assertEqual(restored.terminal_reply_evidence(), ("agent", ""))


if __name__ == "__main__":
    unittest.main()
