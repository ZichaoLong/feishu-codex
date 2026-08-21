import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.execution_recovery_controller import (
    ExecutionRecoveryController,
    SnapshotReplyProjection,
)
from bot.execution_recovery_runtime import (
    RecoverySnapshotReplyItem,
    TerminalReconcileTarget,
)
from bot.execution_pages import ExecutionTranscriptCursor
from bot.execution_transcript import ExecutionTranscript
from bot.runtime_card_publisher import build_execution_card_model


def _snapshot(
    items: list[dict[str, object]],
    *,
    turn_status: str = "completed",
    error: dict[str, object] | None = None,
) -> ThreadSnapshot:
    turn: dict[str, object] = {
        "id": "turn-1",
        "status": turn_status,
        "items": items,
    }
    if error is not None:
        turn["error"] = error
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle" if turn_status != "inProgress" else "active",
        ),
        turns=[turn],
    )


class TerminalReplyEvidenceTests(unittest.TestCase):
    def test_cross_source_selection_uses_contract_precedence(self) -> None:
        cases = (
            ("agent_empty", "", ("error", "failed"), "local", "turn_error", "failed"),
            ("turn_error", "failed", ("agent", "done"), "local", "agent_text", "done"),
            ("agent_empty", "", ("agent", "done"), "local", "agent_text", "done"),
            ("agent_text", "snapshot", ("agent", "local"), "snapshot", "agent_text", "snapshot"),
        )
        for projection_kind, snapshot_text, local, source, kind, text in cases:
            with self.subTest(projection_kind=projection_kind, local=local):
                selection = ExecutionRecoveryController._select_terminal_reply(
                    SnapshotReplyProjection(
                        kind=projection_kind,
                        full_reply_text=snapshot_text,
                        final_reply_text=snapshot_text,
                        reply_items=(),
                    ),
                    local,
                )
                self.assertEqual(
                    (selection.source, selection.kind, selection.final_reply_text),
                    (source, kind, text),
                )

    def test_snapshot_work_invalidates_local_agent_but_not_local_error(self) -> None:
        projection = SnapshotReplyProjection(
            kind="unavailable",
            full_reply_text="阶段说明",
            final_reply_text="",
            reply_items=(),
            invalidates_local_agent_evidence=True,
        )

        agent_selection = ExecutionRecoveryController._select_terminal_reply(
            projection,
            ("agent", "阶段说明"),
        )
        error_selection = ExecutionRecoveryController._select_terminal_reply(
            projection,
            ("error", "provider unavailable"),
        )

        self.assertEqual(agent_selection.kind, "unavailable")
        self.assertEqual(error_selection.kind, "turn_error")
        self.assertEqual(error_selection.final_reply_text, "provider unavailable")

    def test_completed_empty_agent_is_explicit_empty(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot([{"type": "agentMessage", "text": ""}]),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "agent_empty")

    def test_completed_snapshot_empty_invalidates_older_local_agent_text(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot([{"type": "agentMessage", "text": ""}]),
            turn_id="turn-1",
        )

        selection = ExecutionRecoveryController._select_terminal_reply(
            projection,
            ("agent", "旧阶段说明"),
        )

        self.assertTrue(projection.invalidates_local_agent_evidence)
        self.assertEqual(selection.kind, "agent_empty")

    def test_explicit_agent_message_phase_controls_terminal_candidacy(self) -> None:
        cases = (
            (False, None, "agent_text", False),
            (True, None, "agent_text", False),
            (True, "final_answer", "agent_text", False),
            (True, "commentary", "unavailable", True),
            (True, "", "unavailable", True),
            (True, "future_phase", "unavailable", True),
            (True, 1, "unavailable", True),
        )
        for include_phase, phase, kind, invalidates in cases:
            with self.subTest(include_phase=include_phase, phase=phase):
                item: dict[str, object] = {
                    "type": "agentMessage",
                    "text": "最终答案",
                }
                if include_phase:
                    item["phase"] = phase
                projection = ExecutionRecoveryController.snapshot_reply(
                    _snapshot([item]),
                    turn_id="turn-1",
                )

                self.assertEqual(projection.kind, kind)
                self.assertEqual(
                    projection.invalidates_local_agent_evidence,
                    invalidates,
                )

    def test_empty_last_agent_invalidates_earlier_snapshot_commentary(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {"type": "agentMessage", "text": "阶段说明"},
                    {"type": "agentMessage", "text": ""},
                ]
            ),
            turn_id="turn-1",
        )

        selection = ExecutionRecoveryController._select_terminal_reply(
            projection,
            ("agent", "阶段说明"),
        )

        self.assertTrue(projection.invalidates_local_agent_evidence)
        self.assertEqual(selection.kind, "agent_empty")

    def test_root_continuation_work_before_empty_final_invalidates_commentary(
        self,
    ) -> None:
        for item_type in (
            "collabAgentToolCall",
            "commandExecution",
            "contextCompaction",
            "dynamicToolCall",
            "fileChange",
            "imageView",
            "mcpToolCall",
            "plan",
            "reasoning",
            "sleep",
            "enteredReviewMode",
            "exitedReviewMode",
            "webSearch",
        ):
            with self.subTest(item_type=item_type):
                projection = ExecutionRecoveryController.snapshot_reply(
                    _snapshot(
                        [
                            {"type": "agentMessage", "text": "阶段说明"},
                            {"type": item_type},
                            {"type": "agentMessage", "text": ""},
                        ]
                    ),
                    turn_id="turn-1",
                )

                selection = ExecutionRecoveryController._select_terminal_reply(
                    projection,
                    ("agent", "阶段说明"),
                )

                self.assertEqual(projection.kind, "agent_empty")
                self.assertTrue(projection.invalidates_local_agent_evidence)
                self.assertEqual(selection.kind, "agent_empty")

    def test_non_root_or_presentation_items_do_not_invalidate_final(self) -> None:
        for item_type in (
            "subAgentActivity",
            "imageGeneration",
            "turnDiff",
        ):
            with self.subTest(item_type=item_type):
                projection = ExecutionRecoveryController.snapshot_reply(
                    _snapshot(
                        [
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "最终答案",
                            },
                            {"type": item_type},
                        ]
                    ),
                    turn_id="turn-1",
                )

                self.assertEqual(projection.kind, "agent_text")
                self.assertFalse(projection.invalidates_local_agent_evidence)

    def test_continuation_work_preserves_fail_closed_error_evidence(self) -> None:
        for append_work in (
            lambda transcript: transcript.start_process_block(
                "tool",
                marks_work=True,
            ),
            lambda transcript: transcript.append_process_note(
                "tool",
                marks_work=True,
            ),
        ):
            with self.subTest(append_work=append_work):
                transcript = ExecutionTranscript()
                transcript.record_terminal_error("provider unavailable")

                append_work(transcript)
                transcript.reconcile_current_assistant_text("")

                self.assertEqual(
                    transcript.terminal_reply_evidence(),
                    ("error", "provider unavailable"),
                )

    def test_streaming_delta_cannot_erase_fail_closed_error_evidence(self) -> None:
        for completion in ("", None):
            with self.subTest(completion=completion):
                transcript = ExecutionTranscript()
                transcript.record_terminal_error("provider unavailable")

                transcript.append_assistant_delta("late display")
                if completion is None:
                    transcript.record_unavailable_assistant_completion()
                else:
                    transcript.reconcile_current_assistant_text(completion)

                self.assertEqual(
                    transcript.terminal_reply_evidence(),
                    ("error", "provider unavailable"),
                )

    def test_non_empty_completed_agent_final_supersedes_error(self) -> None:
        transcript = ExecutionTranscript()
        transcript.record_terminal_error("provider unavailable")

        transcript.reconcile_current_assistant_text("最终答案")

        self.assertEqual(
            transcript.terminal_reply_evidence(),
            ("agent", "最终答案"),
        )

    def test_live_and_snapshot_choose_same_later_shorter_completion(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text("权威的较长完成文本")
        transcript.reconcile_current_assistant_text("短文本")
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {"type": "agentMessage", "text": "权威的较长完成文本"},
                    {"type": "agentMessage", "text": "短文本"},
                ]
            ),
            turn_id="turn-1",
        )

        self.assertEqual(transcript.reply_text(), projection.full_reply_text)
        self.assertEqual(
            transcript.terminal_reply_evidence(),
            ("agent", projection.final_reply_text),
        )

    def test_short_completion_closes_active_stream_with_exact_text(self) -> None:
        transcript = ExecutionTranscript()
        transcript.append_assistant_delta("流式暂存的较长文本")

        accepted = transcript.reconcile_current_assistant_text("最终短文")

        self.assertTrue(accepted)
        self.assertEqual(transcript.reply_text(), "最终短文")
        self.assertEqual(
            transcript.terminal_reply_evidence(),
            ("agent", "最终短文"),
        )

    def test_terminal_coordinate_preserves_raw_text_and_item_identity(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "阶段说明",
            terminal_candidate=False,
            item_id="commentary-1",
        )
        transcript.start_process_block("compact", marks_work=True)
        transcript.finish_process_block()
        raw_final = "  最终答案\n"

        transcript.reconcile_current_assistant_text(
            raw_final,
            item_id="agent-final-1",
        )

        coordinate = transcript.terminal_agent_reply_coordinate()
        assert coordinate is not None
        self.assertEqual(coordinate.item_id, "agent-final-1")
        self.assertEqual(coordinate.raw_text, raw_final)
        self.assertEqual(
            (coordinate.start_reply_chars, coordinate.end_reply_chars),
            (len("阶段说明"), len("阶段说明") + len(raw_final)),
        )
        restored = transcript.snapshot().to_transcript()
        self.assertEqual(restored.terminal_agent_reply_coordinate(), coordinate)
        self.assertEqual(restored.reply_segments[-1].text, raw_final)

    def test_later_root_work_invalidates_terminal_coordinate(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "最终答案",
            item_id="agent-final-1",
        )

        transcript.append_process_note("继续执行", marks_work=True)

        self.assertIsNone(transcript.terminal_agent_reply_coordinate())
        self.assertIsNone(transcript.terminal_reply_evidence())

    def test_snapshot_display_rebuild_drops_live_terminal_coordinate(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "最终答案",
            item_id="agent-final-1",
        )

        transcript.rebuild_reply_from_snapshot_items(
            [{"type": "agentMessage", "text": "最终答案"}]
        )

        self.assertIsNone(transcript.terminal_agent_reply_coordinate())

    def test_completed_item_replay_does_not_append_duplicate_segment(self) -> None:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "最终答案",
            item_id="agent-final-1",
        )

        transcript.reconcile_current_assistant_text(
            "最终答案",
            item_id="agent-final-1",
        )

        self.assertEqual(transcript.reply_text(), "最终答案")
        self.assertEqual(len(transcript.reply_segments), 1)

    def test_snapshot_projection_exposes_exact_final_item_identity(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {
                        "id": "commentary-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "阶段说明",
                    },
                    {"type": "contextCompaction"},
                    {
                        "id": "agent-final-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "  最终答案\n",
                    },
                ]
            ),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "agent_text")
        self.assertEqual(projection.final_reply_item_id, "agent-final-1")
        self.assertEqual(projection.final_reply_text, "  最终答案\n")

    def test_snapshot_final_expands_old_empty_page_when_carrier_fails(self) -> None:
        controller = object.__new__(ExecutionRecoveryController)
        controller._has_recorded_terminal_result = lambda **kwargs: False
        controller._publish_terminal_result = lambda *args, **kwargs: False
        projected_models = []

        def _dispatch(
            chat_id,
            message_id,
            *,
            transcript,
            running,
            elapsed,
            cancelled,
            cursor_start,
            cursor_end,
        ):
            del chat_id, message_id
            projected_models.append(
                build_execution_card_model(
                    transcript,
                    running=running,
                    elapsed=elapsed,
                    cancelled=cancelled,
                    cursor_start=cursor_start,
                    cursor_end=cursor_end,
                )
            )

        controller._dispatch_execution_card_message = _dispatch
        transcript = ExecutionTranscript()
        target = TerminalReconcileTarget(
            binding=("ou-user", "chat-1"),
            thread_id="thread-1",
            turn_id="turn-1",
            card_message_id="card-1",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=False,
            transcript=transcript.snapshot(),
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=ExecutionTranscriptCursor(),
            cancelled=False,
            elapsed=1,
        )
        projection = SnapshotReplyProjection(
            kind="agent_text",
            full_reply_text="最终答案",
            final_reply_text="最终答案",
            reply_items=(
                RecoverySnapshotReplyItem(
                    item_type="agentMessage",
                    text="最终答案",
                ),
            ),
        )

        carrier_available = controller._apply_terminal_snapshot_projection(
            target=target,
            current_transcript=transcript,
            current_cancelled=False,
            cancelled=False,
            elapsed=1,
            projection=projection,
        )

        self.assertFalse(carrier_available)
        self.assertEqual(len(projected_models), 1)
        self.assertEqual(
            [segment.text for segment in projected_models[0].reply_segments],
            ["最终答案"],
        )

    def test_in_progress_empty_agent_is_unavailable(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {
                        "type": "agentMessage",
                        "status": "inProgress",
                        "text": "",
                    }
                ],
                turn_status="inProgress",
            ),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "unavailable")

    def test_missing_or_null_agent_text_is_unavailable(self) -> None:
        for item in ({"type": "agentMessage"}, {"type": "agentMessage", "text": None}):
            with self.subTest(item=item):
                projection = ExecutionRecoveryController.snapshot_reply(
                    _snapshot([item]),
                    turn_id="turn-1",
                )
                self.assertEqual(projection.kind, "unavailable")

    def test_turn_error_wins_over_empty_agent(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [{"type": "agentMessage", "text": ""}],
                turn_status="failed",
                error={"message": "provider unavailable"},
            ),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "turn_error")
        self.assertEqual(projection.final_reply_text, "provider unavailable")

    def test_terminal_work_after_commentary_is_unavailable(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {"type": "agentMessage", "text": "阶段说明"},
                    {"type": "commandExecution"},
                ]
            ),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "unavailable")

    def test_generated_image_after_final_does_not_invalidate_text(self) -> None:
        projection = ExecutionRecoveryController.snapshot_reply(
            _snapshot(
                [
                    {"type": "agentMessage", "text": "最终答案"},
                    {"type": "imageGeneration", "status": "completed"},
                ]
            ),
            turn_id="turn-1",
        )

        self.assertEqual(projection.kind, "agent_text")
        self.assertEqual(projection.final_reply_text, "最终答案")


if __name__ == "__main__":
    unittest.main()
