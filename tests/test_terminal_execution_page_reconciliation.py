import unittest
from dataclasses import replace
from types import SimpleNamespace

from bot.binding_runtime_contract import BindingRuntimeHandle
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.binding_runtime_state_factory import BindingRuntimeStateFactory
from bot.cards import build_execution_card
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionTranscriptCursor,
    TerminalExecutionPageReceipt,
    terminal_reply_interval_coverage,
)
from bot.execution_recovery_controller import (
    ExecutionRecoveryController,
    SnapshotReplyProjection,
)
from bot.execution_recovery_runtime import (
    RecoverySnapshotReplyItem,
    TerminalReconcileTarget,
)
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.feishu_execution_finalization_controller import (
    FeishuExecutionFinalizationResult,
)
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.runtime_card_publisher import (
    build_execution_card_model,
    render_execution_card,
)


def _reply_text(model) -> str:
    return "".join(
        segment.text
        for segment in model.reply_segments
        if segment.kind == "assistant"
    )


def _reply_panel_expanded(*, running: bool) -> bool:
    card = build_execution_card(
        "",
        [ExecutionReplySegment("assistant", "reply")],
        running=running,
    )
    panel = next(
        element
        for element in card["body"]["elements"]
        if element.get("tag") == "collapsible_panel"
    )
    return bool(panel["expanded"])


class _ConfirmedPagePublisher:
    def __init__(self) -> None:
        self.sent = 1

    def send_execution_card(self, *args, **kwargs):
        del args, kwargs
        self.sent += 1
        return SimpleNamespace(ok=True, message_id=f"card-{self.sent}")


class _OutcomePagePublisher:
    def __init__(self, result: FeishuOutboundResult) -> None:
        self.result = result
        self.sent = 0

    def send_execution_card(self, *args, **kwargs) -> FeishuOutboundResult:
        del args, kwargs
        self.sent += 1
        return self.result


def _page_send_result(
    effect: FeishuOutboundEffect,
    *,
    message_id: str = "",
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=FeishuOutboundOperation.REPLY_MESSAGE,
        effect=effect,
        destination_liveness=(
            FeishuDestinationLiveness.REACHABLE
            if effect is FeishuOutboundEffect.CONFIRMED
            else FeishuDestinationLiveness.UNKNOWN
        ),
        chat_id="chat-1",
        attempt_id=f"page-{effect.value}",
        message_id=message_id,
        error_code="230013" if effect is FeishuOutboundEffect.REJECTED else "",
        error_message=(
            "transport timeout" if effect is FeishuOutboundEffect.UNKNOWN else ""
        ),
    )


class TerminalExecutionPageReconciliationTests(unittest.TestCase):
    @staticmethod
    def _transcript() -> ExecutionTranscript:
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "重复文本",
            terminal_candidate=False,
        )
        transcript.start_process_block("", marks_work=True)
        transcript.finish_process_block()
        transcript.reconcile_current_assistant_text("重复文本")
        return transcript

    @classmethod
    def _target(cls) -> TerminalReconcileTarget:
        transcript = cls._transcript()
        receipts = (
            TerminalExecutionPageReceipt(
                message_id="card-1",
                cursor_start=ExecutionTranscriptCursor(reply_chars=0),
                cursor_end=ExecutionTranscriptCursor(reply_chars=2),
            ),
            TerminalExecutionPageReceipt(
                message_id="card-2",
                cursor_start=ExecutionTranscriptCursor(reply_chars=2),
                cursor_end=ExecutionTranscriptCursor(reply_chars=6),
            ),
            TerminalExecutionPageReceipt(
                message_id="card-3",
                cursor_start=ExecutionTranscriptCursor(reply_chars=6),
                cursor_end=ExecutionTranscriptCursor(reply_chars=8),
            ),
        )
        return TerminalReconcileTarget(
            binding=("ou-user", "chat-1"),
            thread_id="thread-1",
            turn_id="turn-1",
            card_message_id="card-1",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=False,
            transcript=transcript.snapshot(),
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=ExecutionTranscriptCursor(reply_chars=8),
            cancelled=False,
            elapsed=3,
            terminal_page_receipts=receipts,
        )

    @staticmethod
    def _recovery_controller(patches, *, failing_message_id: str = ""):
        controller = object.__new__(ExecutionRecoveryController)

        def dispatch(
            chat_id,
            message_id,
            *,
            transcript,
            running,
            elapsed,
            cancelled,
            cursor_start,
            cursor_end,
        ) -> None:
            del chat_id
            if message_id == failing_message_id:
                raise RuntimeError("page unavailable")
            model = build_execution_card_model(
                transcript,
                running=running,
                elapsed=elapsed,
                cancelled=cancelled,
                cursor_start=cursor_start,
                cursor_end=cursor_end,
            )
            patches.append((message_id, model))

        controller._dispatch_execution_card_message = dispatch
        return controller

    @staticmethod
    def _terminal_presentation_fixture(publisher):
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text("终态回复" * 300)
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="page-1-attempt",
            known_message_id="card-1",
        )
        page = opening.pending_page
        assert page is not None
        ledger = opening.activate_opening(
            expected_page=page,
            message_id="card-1",
        )
        state = BindingRuntimeStateFactory(
            default_working_dir="/tmp/project",
            default_approval_policy="on-request",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
        ).build_default_runtime_state()
        state["execution_pages"] = ledger
        state["execution_transcript"] = transcript
        state["current_prompt_message_id"] = "prompt-1"
        captured = project_binding_session_snapshot(
            state,
            handle=BindingRuntimeHandle(
                _issuer_nonce=1,
                binding=("ou-user", "chat-1"),
                incarnation=1,
            ),
        )
        published = []
        controller = object.__new__(ExecutionOutputController)
        controller._execution_page_payload_limit_bytes = 800
        controller._execution_page_component_limit = 80
        controller._card_publisher_factory = lambda: publisher
        controller._publish_execution_model = (
            lambda chat_id, message_id, model, *, background: published.append(
                (chat_id, message_id, model, background)
            )
        )
        return controller, captured, published, ledger, transcript

    def test_terminal_pagination_returns_all_confirmed_detached_receipts(self) -> None:
        publisher = _ConfirmedPagePublisher()
        controller, captured, published, ledger, transcript = (
            self._terminal_presentation_fixture(publisher)
        )

        receipts = controller.present_terminal_execution_card(captured)

        self.assertGreaterEqual(len(receipts), 2)
        self.assertEqual(
            [receipt.message_id for receipt in receipts],
            [item[1] for item in published],
        )
        for previous, following in zip(receipts, receipts[1:]):
            self.assertEqual(previous.cursor_end, following.cursor_start)
        self.assertEqual(
            receipts[-1].cursor_end,
            ExecutionTranscriptCursor.from_transcript(transcript),
        )
        self.assertIs(captured.execution.pages, ledger)

    def test_rejected_or_unknown_detached_page_returns_confirmed_prefix(self) -> None:
        for effect in (
            FeishuOutboundEffect.REJECTED,
            FeishuOutboundEffect.UNKNOWN,
        ):
            with self.subTest(effect=effect.value):
                publisher = _OutcomePagePublisher(_page_send_result(effect))
                controller, captured, published, _, _ = (
                    self._terminal_presentation_fixture(publisher)
                )

                receipts = controller.present_terminal_execution_card(captured)

                self.assertEqual(publisher.sent, 1)
                self.assertEqual(
                    [receipt.message_id for receipt in receipts],
                    ["card-1"],
                )
                self.assertEqual([item[1] for item in published], ["card-1"])

    def test_duplicate_detached_page_id_stops_before_overwriting_prior_page(self) -> None:
        publisher = _OutcomePagePublisher(
            _page_send_result(
                FeishuOutboundEffect.CONFIRMED,
                message_id="card-1",
            )
        )
        controller, captured, published, _, _ = self._terminal_presentation_fixture(
            publisher
        )

        with self.assertLogs("bot.execution_output_controller", level="ERROR"):
            receipts = controller.present_terminal_execution_card(captured)

        self.assertEqual(publisher.sent, 1)
        self.assertEqual([receipt.message_id for receipt in receipts], ["card-1"])
        self.assertEqual([item[1] for item in published], ["card-1"])

    def test_finalization_rejects_invalid_receipt_sequences(self) -> None:
        first = TerminalExecutionPageReceipt(
            message_id="card-1",
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=ExecutionTranscriptCursor(reply_chars=2),
        )
        cases = (
            (
                "duplicate",
                TerminalExecutionPageReceipt(
                    message_id="card-1",
                    cursor_start=ExecutionTranscriptCursor(reply_chars=2),
                    cursor_end=ExecutionTranscriptCursor(reply_chars=4),
                ),
            ),
            (
                "regression",
                TerminalExecutionPageReceipt(
                    message_id="card-2",
                    cursor_start=ExecutionTranscriptCursor(reply_chars=1),
                    cursor_end=ExecutionTranscriptCursor(reply_chars=3),
                ),
            ),
            (
                "gap",
                TerminalExecutionPageReceipt(
                    message_id="card-2",
                    cursor_start=ExecutionTranscriptCursor(reply_chars=3),
                    cursor_end=ExecutionTranscriptCursor(reply_chars=4),
                ),
            ),
        )
        for name, invalid in cases:
            with self.subTest(case=name), self.assertRaises(ValueError):
                FeishuExecutionFinalizationResult(
                    had_card=True,
                    retired=True,
                    terminal_page_receipts=(first, invalid),
                )

    def test_exact_final_interval_is_removed_without_reflow_or_text_search(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        target = self._target()
        transcript = target.transcript.to_transcript()
        interval = transcript.trailing_reply_interval("重复文本")
        assert interval is not None

        outcome = controller._remove_terminal_reply_from_confirmed_pages(
            target=target,
            transcript=transcript,
            interval=interval,
            cancelled=False,
            elapsed=3,
        )

        self.assertEqual((outcome.status, outcome.reason), ("scheduled", "cleanup_scheduled"))
        self.assertEqual([message_id for message_id, _ in patches], ["card-2", "card-3"])
        self.assertEqual([_reply_text(model) for _, model in patches], ["文本", ""])
        self.assertEqual(
            render_execution_card(patches[-1][1])["body"]["elements"],
            [{"tag": "markdown", "content": "无"}],
        )

    def test_one_page_cleanup_failure_does_not_stop_later_pages(self) -> None:
        patches = []
        controller = self._recovery_controller(
            patches,
            failing_message_id="card-2",
        )
        target = self._target()
        transcript = target.transcript.to_transcript()

        with self.assertLogs("bot.execution_recovery_controller", level="ERROR"):
            outcome = controller._remove_terminal_reply_from_confirmed_pages(
                target=target,
                transcript=transcript,
                interval=(4, 8),
                cancelled=False,
                elapsed=3,
            )

        self.assertEqual(
            (outcome.status, outcome.reason),
            ("partial", "patch_dispatch_partial"),
        )
        self.assertEqual([message_id for message_id, _ in patches], ["card-3"])

    def test_reply_interval_coverage_honors_half_open_page_boundaries(self) -> None:
        receipts = self._target().terminal_page_receipts

        middle = terminal_reply_interval_coverage(
            receipts,
            start_reply_chars=2,
            end_reply_chars=6,
        )
        last = terminal_reply_interval_coverage(
            receipts,
            start_reply_chars=6,
            end_reply_chars=8,
        )

        self.assertTrue(middle.fully_covered)
        self.assertEqual([item.message_id for item in middle.receipts], ["card-2"])
        self.assertTrue(last.fully_covered)
        self.assertEqual([item.message_id for item in last.receipts], ["card-3"])

    def test_confirmed_receipt_prefix_returns_partial_cleanup(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        target = self._target()
        target = replace(
            target,
            terminal_page_receipts=target.terminal_page_receipts[:2],
        )

        outcome = controller._remove_terminal_reply_from_confirmed_pages(
            target=target,
            transcript=target.transcript.to_transcript(),
            interval=(4, 8),
            cancelled=False,
            elapsed=3,
        )

        self.assertEqual(
            (outcome.status, outcome.reason),
            ("partial", "interval_not_fully_covered"),
        )
        self.assertEqual([message_id for message_id, _ in patches], ["card-2"])

    def test_terminal_cleanup_refreshes_nonintersecting_page_status(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: True
        target = self._target()

        published = controller._apply_local_terminal_evidence(
            target=target,
            transcript=target.transcript.to_transcript(),
            current_cancelled=False,
            cancelled=True,
            elapsed=3,
            evidence=("agent", "重复文本"),
        )

        self.assertTrue(published)
        self.assertEqual(
            [message_id for message_id, _ in patches],
            ["card-1", "card-2", "card-3"],
        )
        self.assertEqual([_reply_text(model) for _, model in patches], ["重复", "文本", ""])
        self.assertTrue(all(model.cancelled for _, model in patches))

    def test_failed_terminal_carrier_keeps_every_execution_page_unchanged(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: False
        controller._publish_terminal_result = lambda *args, **kwargs: False
        target = self._target()

        with self.assertLogs(
            "bot.execution_recovery_controller",
            level="INFO",
        ) as captured:
            published = controller._apply_local_terminal_evidence(
                target=target,
                transcript=target.transcript.to_transcript(),
                current_cancelled=False,
                cancelled=False,
                elapsed=3,
            )

        self.assertFalse(published)
        self.assertEqual(patches, [])
        self.assertTrue(any("reason=carrier_unavailable" in line for line in captured.output))

    def test_failed_carrier_status_refresh_keeps_final_on_every_page(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: False
        controller._publish_terminal_result = lambda *args, **kwargs: False
        target = self._target()

        published = controller._apply_local_terminal_evidence(
            target=target,
            transcript=target.transcript.to_transcript(),
            current_cancelled=False,
            cancelled=True,
            elapsed=3,
        )

        self.assertFalse(published)
        self.assertEqual(
            [message_id for message_id, _ in patches],
            ["card-1", "card-2", "card-3"],
        )
        self.assertEqual(
            "".join(_reply_text(model) for _, model in patches),
            "重复文本重复文本",
        )
        self.assertTrue(all(model.cancelled for _, model in patches))

    def test_local_evidence_cannot_clean_different_snapshot_coordinates(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: True
        target = self._target()
        local_transcript = target.transcript.to_transcript()
        snapshot_transcript = ExecutionTranscript()
        snapshot_transcript.reconcile_current_assistant_text(
            "其他文本",
            terminal_candidate=False,
        )
        snapshot_transcript.start_process_block("", marks_work=True)
        snapshot_transcript.finish_process_block()
        snapshot_transcript.reconcile_current_assistant_text("重复文本")
        self.assertEqual(
            local_transcript.reply_content_chars(),
            snapshot_transcript.reply_content_chars(),
        )
        target = replace(target, transcript=snapshot_transcript.snapshot())

        published = controller._apply_local_terminal_evidence(
            target=target,
            transcript=local_transcript,
            current_cancelled=False,
            cancelled=False,
            elapsed=3,
            evidence=("agent", "重复文本"),
        )

        self.assertTrue(published)
        self.assertEqual(patches, [])

    def test_snapshot_item_identity_uses_page_source_coordinate_across_shape_change(
        self,
    ) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: True
        target = self._target()
        transcript = ExecutionTranscript()
        commentary = "这是一段明显长于最终回复的阶段说明"
        transcript.reconcile_current_assistant_text(
            commentary,
            terminal_candidate=False,
            item_id="commentary-1",
        )
        transcript.start_process_block("", marks_work=True)
        transcript.finish_process_block()
        transcript.reconcile_current_assistant_text(
            "  终\n",
            item_id="agent-final-1",
        )
        first_end = len(commentary) // 2
        second_end = len(commentary) + 2
        total_end = transcript.reply_content_chars()
        target = replace(
            target,
            transcript=transcript.snapshot(),
            cursor_end=ExecutionTranscriptCursor(reply_chars=total_end),
            terminal_page_receipts=(
                TerminalExecutionPageReceipt(
                    "card-1",
                    ExecutionTranscriptCursor(reply_chars=0),
                    ExecutionTranscriptCursor(reply_chars=first_end),
                ),
                TerminalExecutionPageReceipt(
                    "card-2",
                    ExecutionTranscriptCursor(reply_chars=first_end),
                    ExecutionTranscriptCursor(reply_chars=second_end),
                ),
                TerminalExecutionPageReceipt(
                    "card-3",
                    ExecutionTranscriptCursor(reply_chars=second_end),
                    ExecutionTranscriptCursor(reply_chars=total_end),
                ),
            ),
        )
        snapshot_commentary = "另" * len(commentary)
        projection = SnapshotReplyProjection(
            kind="agent_text",
            full_reply_text=f"{snapshot_commentary}\n\n  终\n",
            final_reply_text="  终\n",
            reply_items=(
                RecoverySnapshotReplyItem("agentMessage", snapshot_commentary),
                RecoverySnapshotReplyItem("commandExecution", ""),
                RecoverySnapshotReplyItem("agentMessage", "  终\n"),
            ),
            final_reply_item_id="agent-final-1",
        )

        carrier_available = controller._apply_terminal_snapshot_projection(
            target=target,
            current_transcript=transcript,
            current_cancelled=False,
            cancelled=False,
            elapsed=3,
            projection=projection,
        )

        self.assertTrue(carrier_available)
        self.assertEqual([message_id for message_id, _ in patches], ["card-2", "card-3"])
        self.assertEqual(
            [_reply_text(model) for _, model in patches],
            [commentary[first_end:], ""],
        )

    def test_snapshot_same_text_with_different_item_identity_retains_pages(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: True
        target = self._target()
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "重复文本",
            terminal_candidate=False,
        )
        transcript.start_process_block("tool", marks_work=True)
        transcript.finish_process_block()
        transcript.reconcile_current_assistant_text(
            "重复文本",
            item_id="agent-live",
        )
        target = replace(target, transcript=transcript.snapshot())
        projection = SnapshotReplyProjection(
            kind="agent_text",
            full_reply_text="重复文本\n\n重复文本",
            final_reply_text="重复文本",
            reply_items=(
                RecoverySnapshotReplyItem("agentMessage", "重复文本"),
                RecoverySnapshotReplyItem("commandExecution", ""),
                RecoverySnapshotReplyItem("agentMessage", "重复文本"),
            ),
            final_reply_item_id="agent-snapshot",
        )

        with self.assertLogs(
            "bot.execution_recovery_controller",
            level="INFO",
        ) as captured:
            carrier_available = controller._apply_terminal_snapshot_projection(
                target=target,
                current_transcript=transcript,
                current_cancelled=False,
                cancelled=False,
                elapsed=3,
                projection=projection,
            )

        self.assertTrue(carrier_available)
        self.assertEqual(patches, [])
        self.assertTrue(any("reason=item_identity_mismatch" in line for line in captured.output))

    def test_snapshot_same_item_with_different_raw_text_retains_pages(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: True
        target = self._target()
        transcript = ExecutionTranscript()
        transcript.reconcile_current_assistant_text(
            "本地最终文本",
            item_id="agent-final-1",
        )
        target = replace(target, transcript=transcript.snapshot())
        projection = SnapshotReplyProjection(
            kind="agent_text",
            full_reply_text="快照最终文本",
            final_reply_text="快照最终文本",
            reply_items=(
                RecoverySnapshotReplyItem("agentMessage", "快照最终文本"),
            ),
            final_reply_item_id="agent-final-1",
        )

        with self.assertLogs(
            "bot.execution_recovery_controller",
            level="INFO",
        ) as captured:
            carrier_available = controller._apply_terminal_snapshot_projection(
                target=target,
                current_transcript=transcript,
                current_cancelled=False,
                cancelled=False,
                elapsed=3,
                projection=projection,
            )

        self.assertTrue(carrier_available)
        self.assertEqual(patches, [])
        self.assertTrue(any("reason=raw_text_mismatch" in line for line in captured.output))

    def test_coordinate_mismatch_refreshes_status_from_receipt_transcript(self) -> None:
        patches = []
        controller = self._recovery_controller(patches)
        controller._has_recorded_terminal_result = lambda **kwargs: False
        controller._publish_terminal_result = lambda *args, **kwargs: False
        target = self._target()
        local_transcript = target.transcript.to_transcript()
        snapshot_transcript = ExecutionTranscript()
        snapshot_transcript.reconcile_current_assistant_text(
            "其他文本",
            terminal_candidate=False,
        )
        snapshot_transcript.start_process_block("", marks_work=True)
        snapshot_transcript.finish_process_block()
        snapshot_transcript.reconcile_current_assistant_text("重复文本")
        target = replace(target, transcript=snapshot_transcript.snapshot())

        published = controller._apply_local_terminal_evidence(
            target=target,
            transcript=local_transcript,
            current_cancelled=False,
            cancelled=True,
            elapsed=3,
            evidence=("agent", "重复文本"),
        )

        self.assertFalse(published)
        self.assertEqual(
            [message_id for message_id, _ in patches],
            ["card-1", "card-2", "card-3"],
        )
        self.assertEqual(
            "".join(_reply_text(model) for _, model in patches),
            "其他文本重复文本",
        )
        self.assertTrue(all(model.cancelled for _, model in patches))

    def test_running_reply_is_expanded_and_completed_reply_is_collapsed(self) -> None:
        self.assertTrue(_reply_panel_expanded(running=True))
        self.assertFalse(_reply_panel_expanded(running=False))


if __name__ == "__main__":
    unittest.main()
