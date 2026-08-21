import json
import threading
import time
import unittest

from bot.execution_pages import ExecutionTranscriptCursor
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.runtime_card_publisher import (
    ExecutionCardPatchOutcome,
    ExecutionCardPatchDispatcher,
    ExecutionCardPatchDispatcherShutdownTimeoutError,
    ExecutionCardPatchStatus,
    RuntimeCardPublisher,
    build_execution_card_model,
    build_plan_card_model,
    execution_card_model_fits_page,
    execution_card_payload_metrics,
    fit_execution_card_page_end,
    serialize_execution_card,
)
from bot.binding_runtime_contract import (
    BindingPlanSnapshot,
    BindingPlanStepSnapshot,
)


class _FakeBot:
    def __init__(self) -> None:
        self.patches: list[tuple[str, str]] = []
        self.patch_results: dict[str, bool] = {}
        self.patch_result_overrides: dict[str, FeishuOutboundResult] = {}
        self.patch_result_sequences: dict[str, list[FeishuOutboundResult]] = {}
        self.reply_calls: list[tuple[str, str, str]] = []
        self.send_calls: list[tuple[str, str, str]] = []
        self.reply_attempt_ids: list[str] = []
        self.send_attempt_ids: list[str] = []
        self.deletes: list[str] = []
        self.reply_result_sequences: list[FeishuOutboundResult] = []
        self.send_result_sequences: list[FeishuOutboundResult] = []
        self.reply_result: FeishuOutboundResult | None = None
        self.send_result: FeishuOutboundResult | None = None

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        del attempt_id
        self.patches.append((message_id, content))
        sequence = self.patch_result_sequences.get(message_id)
        if sequence:
            return sequence.pop(0)
        override = self.patch_result_overrides.get(message_id)
        if override is not None:
            return override
        if self.patch_results.get(message_id, True):
            return _confirmed(
                FeishuOutboundOperation.PATCH_MESSAGE,
                chat_id=chat_id,
                message_id=message_id,
            )
        return _rejected(
            FeishuOutboundOperation.PATCH_MESSAGE,
            chat_id=chat_id,
        )

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        del reply_in_thread
        self.reply_calls.append((parent_id, msg_type, content))
        self.reply_attempt_ids.append(attempt_id)
        if self.reply_result_sequences:
            return self.reply_result_sequences.pop(0)
        return self.reply_result or _confirmed(
            FeishuOutboundOperation.REPLY_MESSAGE,
            chat_id=chat_id,
            message_id="reply-card-id",
        )

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        self.send_calls.append((chat_id, msg_type, content))
        self.send_attempt_ids.append(attempt_id)
        if self.send_result_sequences:
            return self.send_result_sequences.pop(0)
        return self.send_result or _confirmed(
            FeishuOutboundOperation.CREATE_MESSAGE,
            chat_id=chat_id,
            message_id="send-card-id",
        )

    def delete_message(self, message_id: str) -> bool:
        self.deletes.append(message_id)
        return True


def _confirmed(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "chat-1",
    message_id: str = "message-1",
    attempt_id: str = "attempt-confirmed",
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id=attempt_id,
        message_id=message_id,
    )


def _rejected(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "chat-1",
    retry_after_seconds: float = 0.0,
    content_rejected: bool = False,
    attempt_id: str = "attempt-rejected",
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.REJECTED,
        destination_liveness=FeishuDestinationLiveness.UNKNOWN,
        chat_id=chat_id,
        attempt_id=attempt_id,
        error_code="230099" if content_rejected else "230013",
        retry_after_seconds=retry_after_seconds,
        content_rejected=content_rejected,
    )


def _unknown(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "chat-1",
    retry_after_seconds: float = 0.0,
    attempt_id: str = "attempt-unknown",
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.UNKNOWN,
        destination_liveness=FeishuDestinationLiveness.UNKNOWN,
        chat_id=chat_id,
        attempt_id=attempt_id,
        error_message="transport timeout",
        retry_after_seconds=retry_after_seconds,
    )


class RuntimeCardPublisherTests(unittest.TestCase):
    def test_running_observer_card_has_no_cancel_action(self) -> None:
        model = build_execution_card_model(
            ExecutionTranscript(process_blocks=["中途接入"]),
            running=True,
            elapsed=1,
            cancelled=False,
            cancelable=False,
        )

        payload = serialize_execution_card(model)

        self.assertIn("执行中", payload)
        self.assertNotIn("取消执行", payload)
        self.assertNotIn("cancel_turn", payload)

    def test_publish_interactive_card_keeps_unknown_to_one_attempt(
        self,
    ) -> None:
        bot = _FakeBot()
        bot.reply_result = _unknown(
            FeishuOutboundOperation.REPLY_MESSAGE,
            attempt_id="single-attempt-uuid",
        )
        publisher = RuntimeCardPublisher(bot)

        result = publisher.publish_interactive_card(
            "chat-1",
            {"schema": "2.0"},
            "prompt-1",
            True,
            attempt_id="single-attempt-uuid",
        )

        self.assertEqual(result.effect, FeishuOutboundEffect.UNKNOWN)
        self.assertEqual(len(bot.reply_calls), 1)
        self.assertEqual(bot.reply_attempt_ids, ["single-attempt-uuid"])

    def test_send_interactive_card_keeps_unknown_reply_single_attempt(self) -> None:
        bot = _FakeBot()
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        publisher = RuntimeCardPublisher(bot)

        message_id = publisher.send_interactive_card(
            "chat-1",
            {"schema": "2.0"},
            "prompt-1",
            True,
        )

        self.assertIsNone(message_id)
        self.assertEqual(len(bot.reply_calls), 1)
        self.assertEqual(bot.send_calls, [])

    def test_execution_card_patch_outcome_rejects_retry_without_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "require retry_model"):
            ExecutionCardPatchOutcome(status=ExecutionCardPatchStatus.RETRYABLE)

    def test_execution_card_patch_outcome_rejects_retry_state_for_terminal_status(self) -> None:
        model = build_execution_card_model(
            ExecutionTranscript(process_blocks=["仍在执行"]),
            running=True,
            elapsed=1,
            cancelled=False,
        )

        with self.assertRaisesRegex(ValueError, "only retryable"):
            ExecutionCardPatchOutcome(
                status=ExecutionCardPatchStatus.FAILED,
                retry_after_seconds=1.0,
                retry_model=model,
            )

    def test_build_execution_card_model_projects_the_exact_cursor_range(self) -> None:
        transcript = ExecutionTranscript(
            reply_segments=[
                ExecutionReplySegment("assistant", "第一段"),
                ExecutionReplySegment("divider"),
                ExecutionReplySegment("assistant", "第二段"),
            ],
            process_blocks=["0123456789"],
        )

        model = build_execution_card_model(
            transcript,
            running=False,
            elapsed=12,
            cancelled=True,
        )

        self.assertEqual(model.log_text, "0123456789")
        self.assertEqual(model.reply_segments[1].kind, "divider")
        self.assertTrue(model.cancelled)

    def test_execution_page_budget_counts_rendered_utf8_bytes(self) -> None:
        transcript = ExecutionTranscript(process_blocks=["界" * 80])
        transcript_end = ExecutionTranscriptCursor.from_transcript(transcript)
        full_model = build_execution_card_model(
            transcript,
            running=True,
            elapsed=1,
            cancelled=False,
        )
        full_metrics = execution_card_payload_metrics(full_model)
        serialized = serialize_execution_card(full_model)
        payload_limit = full_metrics.utf8_bytes - 1

        fitted_end = fit_execution_card_page_end(
            transcript,
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=transcript_end,
            running=True,
            elapsed=1,
            cancelled=False,
            payload_limit_bytes=payload_limit,
        )
        fitted_model = build_execution_card_model(
            transcript,
            running=True,
            elapsed=1,
            cancelled=False,
            cursor_end=fitted_end,
        )

        self.assertEqual(
            full_metrics.utf8_bytes,
            len(serialized.encode("utf-8")),
        )
        self.assertGreater(full_metrics.utf8_bytes, len(serialized))
        self.assertLess(fitted_end.process_chars, transcript_end.process_chars)
        self.assertTrue(
            execution_card_model_fits_page(
                fitted_model,
                payload_limit_bytes=payload_limit,
            )
        )

    def test_execution_page_budget_can_be_bounded_by_component_count(self) -> None:
        segments: list[ExecutionReplySegment] = []
        for index in range(12):
            if index:
                segments.append(ExecutionReplySegment("divider"))
            segments.append(ExecutionReplySegment("assistant", str(index % 10)))
        transcript = ExecutionTranscript(reply_segments=segments)
        first_five = build_execution_card_model(
            transcript,
            running=True,
            elapsed=1,
            cancelled=False,
            cursor_end=ExecutionTranscriptCursor(reply_chars=5),
        )
        component_limit = execution_card_payload_metrics(
            first_five
        ).component_count
        transcript_end = ExecutionTranscriptCursor.from_transcript(transcript)

        fitted_end = fit_execution_card_page_end(
            transcript,
            cursor_start=ExecutionTranscriptCursor(),
            cursor_end=transcript_end,
            running=True,
            elapsed=1,
            cancelled=False,
            payload_limit_bytes=100_000,
            component_limit=component_limit,
        )

        self.assertEqual(fitted_end.reply_chars, 5)
        self.assertTrue(
            execution_card_model_fits_page(
                build_execution_card_model(
                    transcript,
                    running=True,
                    elapsed=1,
                    cancelled=False,
                    cursor_end=fitted_end,
                ),
                payload_limit_bytes=100_000,
                component_limit=component_limit,
            )
        )
        self.assertFalse(
            execution_card_model_fits_page(
                build_execution_card_model(
                    transcript,
                    running=True,
                    elapsed=1,
                    cancelled=False,
                    cursor_end=ExecutionTranscriptCursor(reply_chars=6),
                ),
                payload_limit_bytes=100_000,
                component_limit=component_limit,
            )
        )

    def test_publish_plan_card_reuses_existing_message_when_patch_succeeds(self) -> None:
        bot = _FakeBot()
        publisher = RuntimeCardPublisher(bot)
        model = build_plan_card_model(
            BindingPlanSnapshot(
                message_id="plan-1",
                turn_id="turn-1",
                explanation="exp",
                steps=(
                    BindingPlanStepSnapshot(
                        step="do it",
                        status="pending",
                    ),
                ),
                text="",
            )
        )

        result = publisher.publish_plan_card(
            chat_id="chat-1",
            parent_message_id="parent-1",
            plan_message_id="plan-1",
            model=model,
        )

        self.assertTrue(result.reused_existing)
        self.assertEqual(result.message_id, "plan-1")
        self.assertEqual(len(bot.patches), 1)
        self.assertEqual(bot.reply_calls, [])
        self.assertEqual(bot.send_calls, [])

    def test_publish_plan_card_falls_back_to_reply_when_patch_fails(self) -> None:
        bot = _FakeBot()
        bot.patch_results["plan-1"] = False
        publisher = RuntimeCardPublisher(bot)
        model = build_plan_card_model(
            BindingPlanSnapshot(
                message_id="plan-1",
                turn_id="turn-1",
                explanation="exp",
                steps=(),
                text="body",
            )
        )

        result = publisher.publish_plan_card(
            chat_id="chat-1",
            parent_message_id="parent-1",
            plan_message_id="plan-1",
            model=model,
        )

        self.assertTrue(result.attempted_existing)
        self.assertFalse(result.reused_existing)
        self.assertEqual(result.message_id, "reply-card-id")
        self.assertEqual(len(bot.reply_calls), 1)

    def test_unknown_execution_card_reply_does_not_create_second_message(self) -> None:
        bot = _FakeBot()
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        publisher = RuntimeCardPublisher(bot)

        result = publisher.send_execution_card(
            "chat-1",
            "parent-1",
            attempt_id="stable-attempt",
        )

        self.assertEqual(result.effect, FeishuOutboundEffect.UNKNOWN)
        self.assertEqual(len(bot.reply_calls), 1)
        self.assertEqual(bot.send_calls, [])

    def test_unknown_existing_plan_patch_does_not_send_replacement(self) -> None:
        bot = _FakeBot()
        bot.patch_result_overrides["plan-1"] = _unknown(
            FeishuOutboundOperation.PATCH_MESSAGE
        )
        publisher = RuntimeCardPublisher(bot)
        model = build_plan_card_model(
            BindingPlanSnapshot(
                message_id="plan-1",
                turn_id="turn-1",
                explanation="exp",
                steps=(),
                text="body",
            )
        )

        result = publisher.publish_plan_card(
            chat_id="chat-1",
            parent_message_id="parent-1",
            plan_message_id="plan-1",
            model=model,
        )

        self.assertTrue(result.outcome_unknown)
        self.assertEqual(result.message_id, "plan-1")
        self.assertEqual(bot.reply_calls, [])
        self.assertEqual(bot.send_calls, [])

    def test_patch_execution_card_serializes_rendered_card(self) -> None:
        bot = _FakeBot()
        publisher = RuntimeCardPublisher(bot)
        transcript = ExecutionTranscript()
        transcript.set_reply_text("hello")
        model = build_execution_card_model(
            transcript,
            running=True,
            elapsed=3,
            cancelled=False,
        )

        result = publisher.patch_execution_card("chat-1", "exec-1", model)

        self.assertEqual(result.status, ExecutionCardPatchStatus.FULL_APPLIED)
        self.assertTrue(result.full_content_applied)
        self.assertEqual(len(bot.patches), 1)
        message_id, content = bot.patches[0]
        self.assertEqual(message_id, "exec-1")
        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "Codex 执行过程（执行中 3s）")

    def test_patch_execution_card_logs_only_successful_terminal_update(self) -> None:
        bot = _FakeBot()
        publisher = RuntimeCardPublisher(bot)
        running_model = build_execution_card_model(
            ExecutionTranscript(),
            running=True,
            elapsed=1,
            cancelled=False,
        )
        final_model = build_execution_card_model(
            ExecutionTranscript(),
            running=False,
            elapsed=2,
            cancelled=False,
        )

        with self.assertNoLogs("bot.runtime_card_publisher", level="INFO"):
            self.assertTrue(publisher.patch_execution_card("chat-1", "exec-1", running_model).applied)

        with self.assertLogs("bot.runtime_card_publisher", level="INFO") as logs:
            self.assertTrue(publisher.patch_execution_card("chat-1", "exec-1", final_model).applied)

        self.assertEqual(len(logs.output), 1)
        self.assertIn("执行卡片终态更新成功", logs.output[0])
        self.assertIn("message_id=exec-1", logs.output[0])

    def test_patch_execution_card_does_not_log_failed_terminal_update(self) -> None:
        bot = _FakeBot()
        bot.patch_results["exec-1"] = False
        publisher = RuntimeCardPublisher(bot)
        final_model = build_execution_card_model(
            ExecutionTranscript(),
            running=False,
            elapsed=2,
            cancelled=False,
        )

        with self.assertNoLogs("bot.runtime_card_publisher", level="INFO"):
            self.assertFalse(publisher.patch_execution_card("chat-1", "exec-1", final_model).applied)
        self.assertEqual(len(bot.patches), 1)

    def test_patch_execution_card_falls_back_to_minimal_terminal_card_when_content_is_rejected(self) -> None:
        bot = _FakeBot()
        bot.patch_result_sequences["exec-1"] = [
            _rejected(FeishuOutboundOperation.PATCH_MESSAGE, content_rejected=True),
            _confirmed(FeishuOutboundOperation.PATCH_MESSAGE, message_id="exec-1"),
        ]
        publisher = RuntimeCardPublisher(bot)
        transcript = ExecutionTranscript(
            process_blocks=['<?xml version="1.0"?><rss><item>bad</item></rss>'],
        )
        transcript.set_reply_text("最终回复")
        final_model = build_execution_card_model(
            transcript,
            running=False,
            elapsed=610,
            cancelled=False,
        )

        with self.assertLogs("bot.runtime_card_publisher", level="INFO") as logs:
            result = publisher.patch_execution_card("chat-1", "exec-1", final_model)

        self.assertEqual(result.status, ExecutionCardPatchStatus.MINIMAL_APPLIED)
        self.assertTrue(result.applied)
        self.assertFalse(result.full_content_applied)
        self.assertEqual(len(bot.patches), 2)
        full_card = json.loads(bot.patches[0][1])
        minimal_card = json.loads(bot.patches[1][1])
        self.assertEqual(full_card["header"]["title"]["content"], "Codex 执行过程")
        self.assertEqual(minimal_card["header"]["title"]["content"], "Codex 执行过程")
        self.assertEqual(minimal_card["body"]["elements"], [{"tag": "markdown", "content": "无"}])
        self.assertNotIn("取消执行", bot.patches[1][1])
        self.assertTrue(any("极简终态卡" in line for line in logs.output))
        self.assertTrue(any("outcome=minimal_applied" in line for line in logs.output))

    def test_patch_execution_card_retryable_full_patch_keeps_original_model(self) -> None:
        bot = _FakeBot()
        bot.patch_result_sequences["exec-1"] = [_rejected(FeishuOutboundOperation.PATCH_MESSAGE, retry_after_seconds=0.25)]
        publisher = RuntimeCardPublisher(bot)
        model = build_execution_card_model(
            ExecutionTranscript(process_blocks=["仍在执行"]),
            running=True,
            elapsed=4,
            cancelled=False,
        )

        result = publisher.patch_execution_card("chat-1", "exec-1", model)

        self.assertEqual(result.status, ExecutionCardPatchStatus.RETRYABLE)
        self.assertEqual(result.retry_after_seconds, 0.25)
        self.assertIs(result.retry_model, model)

    def test_delete_card_message_delegates_to_bot(self) -> None:
        bot = _FakeBot()
        publisher = RuntimeCardPublisher(bot)

        ok = publisher.delete_card_message("exec-1")

        self.assertTrue(ok)
        self.assertEqual(bot.deletes, ["exec-1"])

    def test_execution_card_patch_dispatcher_coalesces_stale_updates_for_same_message(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        calls: list[tuple[str, int]] = []

        def publish_patch(
            _chat_id: str,
            message_id: str,
            model,
        ) -> ExecutionCardPatchOutcome:
            calls.append((message_id, model.elapsed))
            if len(calls) == 1:
                first_started.set()
                release_first.wait(timeout=1)
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=2)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False))
        self.assertTrue(first_started.wait(timeout=1))
        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False))
        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=False, elapsed=3, cancelled=False))
        release_first.set()

        deadline = time.time() + 1
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(calls, [("exec-1", 1), ("exec-1", 3)])

    def test_execution_card_patch_dispatcher_does_not_block_other_messages(self) -> None:
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        def publish_patch(
            _chat_id: str,
            message_id: str,
            model,
        ) -> ExecutionCardPatchOutcome:
            del model
            if message_id == "exec-1":
                first_started.set()
                release_first.wait(timeout=1)
            elif message_id == "exec-2":
                second_started.set()
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=2)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False))
        self.assertTrue(first_started.wait(timeout=1))
        dispatcher.submit("chat-1", "exec-2", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False))

        self.assertTrue(second_started.wait(timeout=1))
        release_first.set()

    def test_execution_card_patch_dispatcher_retries_latest_model_after_retryable_failure(self) -> None:
        first_attempt = threading.Event()
        calls: list[tuple[str, int]] = []

        def publish_patch(
            _chat_id: str,
            message_id: str,
            model,
        ) -> ExecutionCardPatchOutcome:
            calls.append((message_id, model.elapsed))
            if len(calls) == 1:
                first_attempt.set()
                return ExecutionCardPatchOutcome.retry_later(0.01, retry_model=model)
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=1)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False))
        self.assertTrue(first_attempt.wait(timeout=1))
        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False))
        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=False, elapsed=3, cancelled=False))

        deadline = time.time() + 1
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(calls, [("exec-1", 1), ("exec-1", 3)])

    def test_execution_card_patch_dispatcher_retries_minimal_model_after_fallback_rate_limit(self) -> None:
        bot = _FakeBot()
        bot.patch_result_sequences["exec-1"] = [
            _rejected(FeishuOutboundOperation.PATCH_MESSAGE, content_rejected=True),
            _rejected(FeishuOutboundOperation.PATCH_MESSAGE, retry_after_seconds=0.01),
            _confirmed(FeishuOutboundOperation.PATCH_MESSAGE, message_id="exec-1"),
        ]
        publisher = RuntimeCardPublisher(bot)
        dispatcher = ExecutionCardPatchDispatcher(publisher.patch_execution_card, worker_count=1)
        self.addCleanup(dispatcher.shutdown)
        transcript = ExecutionTranscript(process_blocks=["<rss>bad</rss>"])
        transcript.set_reply_text("最终回复")

        dispatcher.submit(
            "chat-1",
            "exec-1",
            build_execution_card_model(
                transcript,
                running=False,
                elapsed=3,
                cancelled=False,
            ),
        )

        deadline = time.time() + 1
        while len(bot.patches) < 3 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(bot.patches), 3)
        full_card, first_minimal, retried_minimal = [json.loads(item[1]) for item in bot.patches]
        self.assertIn("最终回复", bot.patches[0][1])
        self.assertEqual(first_minimal["body"]["elements"], [{"tag": "markdown", "content": "无"}])
        self.assertEqual(retried_minimal, first_minimal)
        self.assertNotEqual(full_card, first_minimal)

    def test_execution_card_patch_dispatcher_prefers_new_model_over_minimal_retry_model(self) -> None:
        bot = _FakeBot()
        bot.patch_result_sequences["exec-1"] = [
            _rejected(FeishuOutboundOperation.PATCH_MESSAGE, content_rejected=True),
            _rejected(FeishuOutboundOperation.PATCH_MESSAGE, retry_after_seconds=0.05),
            _confirmed(FeishuOutboundOperation.PATCH_MESSAGE, message_id="exec-1"),
        ]
        publisher = RuntimeCardPublisher(bot)
        dispatcher = ExecutionCardPatchDispatcher(publisher.patch_execution_card, worker_count=1)
        self.addCleanup(dispatcher.shutdown)
        initial = ExecutionTranscript(process_blocks=["<rss>bad</rss>"])
        initial.set_reply_text("旧回复")

        dispatcher.submit(
            "chat-1",
            "exec-1",
            build_execution_card_model(
                initial,
                running=False,
                elapsed=3,
                cancelled=False,
            ),
        )
        deadline = time.time() + 1
        while len(bot.patches) < 2 and time.time() < deadline:
            time.sleep(0.005)

        newer = ExecutionTranscript(process_blocks=["新模型过程"])
        dispatcher.submit(
            "chat-1",
            "exec-1",
            build_execution_card_model(
                newer,
                running=True,
                elapsed=9,
                cancelled=False,
            ),
        )
        deadline = time.time() + 1
        while len(bot.patches) < 3 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(bot.patches), 3)
        retried_card = json.loads(bot.patches[2][1])
        self.assertEqual(retried_card["header"]["title"]["content"], "Codex 执行过程（执行中 9s）")
        self.assertIn("新模型过程", bot.patches[2][1])

    def test_execution_card_patch_dispatcher_retry_backoff_does_not_block_other_messages(self) -> None:
        first_attempt = threading.Event()
        second_started = threading.Event()
        calls: list[str] = []

        def publish_patch(
            _chat_id: str,
            message_id: str,
            model,
        ) -> ExecutionCardPatchOutcome:
            calls.append(message_id)
            if message_id == "exec-1" and len(calls) == 1:
                first_attempt.set()
                return ExecutionCardPatchOutcome.retry_later(0.05, retry_model=model)
            if message_id == "exec-2":
                second_started.set()
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=1)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False))
        self.assertTrue(first_attempt.wait(timeout=1))
        dispatcher.submit("chat-1", "exec-2", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False))

        self.assertTrue(second_started.wait(timeout=1))
        self.assertEqual(calls[:2], ["exec-1", "exec-2"])

    def test_execution_card_patch_dispatcher_keeps_backoff_when_newer_model_arrives_during_retry_wait(self) -> None:
        first_attempt = threading.Event()
        calls: list[tuple[str, int, float]] = []
        started_at = time.monotonic()

        def publish_patch(
            _chat_id: str,
            message_id: str,
            model,
        ) -> ExecutionCardPatchOutcome:
            calls.append((message_id, model.elapsed, time.monotonic() - started_at))
            if len(calls) == 1:
                first_attempt.set()
                return ExecutionCardPatchOutcome.retry_later(0.05, retry_model=model)
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=1)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False))
        self.assertTrue(first_attempt.wait(timeout=1))
        time.sleep(0.01)
        dispatcher.submit("chat-1", "exec-1", build_execution_card_model(ExecutionTranscript(), running=False, elapsed=2, cancelled=False))
        time.sleep(0.02)

        self.assertEqual(len(calls), 1)

        deadline = time.time() + 1
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(calls[1][0:2], ("exec-1", 2))
        self.assertGreaterEqual(calls[1][2], 0.04)

    def test_execution_card_patch_dispatcher_shutdown_fails_when_worker_remains_live(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def publish_patch(
            _chat_id: str,
            _message_id: str,
            _model,
        ) -> ExecutionCardPatchOutcome:
            started.set()
            release.wait()
            return ExecutionCardPatchOutcome.full_applied()

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=1)
        self.addCleanup(dispatcher.shutdown)
        self.addCleanup(release.set)
        dispatcher.submit(
            "chat-1",
            "exec-1",
            build_execution_card_model(
                ExecutionTranscript(),
                running=True,
                elapsed=1,
                cancelled=False,
            ),
        )
        self.assertTrue(started.wait(timeout=1))

        with self.assertRaises(ExecutionCardPatchDispatcherShutdownTimeoutError):
            dispatcher.shutdown(timeout=0)

        release.set()
        dispatcher.shutdown(timeout=1)


if __name__ == "__main__":
    unittest.main()
