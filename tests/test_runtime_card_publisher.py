import json
import threading
import time
import unittest

from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.runtime_card_publisher import (
    ExecutionCardPatchDispatcher,
    RuntimeCardPublisher,
    build_execution_card_model,
    build_plan_card_model,
)
from bot.runtime_view import PlanStepView, PlanView


class _FakeBot:
    def __init__(self) -> None:
        self.patches: list[tuple[str, str]] = []
        self.patch_results: dict[str, bool] = {}
        self.reply_calls: list[tuple[str, str, str]] = []
        self.send_calls: list[tuple[str, str, str]] = []

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return self.patch_results.get(message_id, True)

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_calls.append((parent_id, msg_type, content))
        return "reply-card-id"

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.send_calls.append((chat_id, msg_type, content))
        return "send-card-id"


class RuntimeCardPublisherTests(unittest.TestCase):
    def test_build_execution_card_model_truncates_log_and_limits_reply_segments(self) -> None:
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
            log_limit=5,
            reply_limit=100,
        )

        self.assertTrue(model.log_text.endswith("**[日志已截断，仅保留最近部分]**"))
        self.assertEqual(model.reply_segments[1].kind, "divider")
        self.assertTrue(model.cancelled)

    def test_publish_plan_card_reuses_existing_message_when_patch_succeeds(self) -> None:
        bot = _FakeBot()
        publisher = RuntimeCardPublisher(bot)
        model = build_plan_card_model(
            PlanView(
                message_id="plan-1",
                turn_id="turn-1",
                explanation="exp",
                steps=(PlanStepView(step="do it", status="pending"),),
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
            PlanView(
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
            log_limit=100,
            reply_limit=100,
        )

        ok = publisher.patch_execution_card("exec-1", model)

        self.assertTrue(ok)
        self.assertEqual(len(bot.patches), 1)
        message_id, content = bot.patches[0]
        self.assertEqual(message_id, "exec-1")
        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "Codex（执行中 3s）")

    def test_execution_card_patch_dispatcher_coalesces_stale_updates_for_same_message(self) -> None:
        first_started = threading.Event()
        release_first = threading.Event()
        calls: list[tuple[str, int]] = []

        def publish_patch(message_id: str, model) -> bool:
            calls.append((message_id, model.elapsed))
            if len(calls) == 1:
                first_started.set()
                release_first.wait(timeout=1)
            return True

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=2)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False, log_limit=100, reply_limit=100))
        self.assertTrue(first_started.wait(timeout=1))
        dispatcher.submit("exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False, log_limit=100, reply_limit=100))
        dispatcher.submit("exec-1", build_execution_card_model(ExecutionTranscript(), running=False, elapsed=3, cancelled=False, log_limit=100, reply_limit=100))
        release_first.set()

        deadline = time.time() + 1
        while len(calls) < 2 and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(calls, [("exec-1", 1), ("exec-1", 3)])

    def test_execution_card_patch_dispatcher_does_not_block_other_messages(self) -> None:
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()

        def publish_patch(message_id: str, model) -> bool:
            del model
            if message_id == "exec-1":
                first_started.set()
                release_first.wait(timeout=1)
            elif message_id == "exec-2":
                second_started.set()
            return True

        dispatcher = ExecutionCardPatchDispatcher(publish_patch, worker_count=2)
        self.addCleanup(dispatcher.shutdown)

        dispatcher.submit("exec-1", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=1, cancelled=False, log_limit=100, reply_limit=100))
        self.assertTrue(first_started.wait(timeout=1))
        dispatcher.submit("exec-2", build_execution_card_model(ExecutionTranscript(), running=True, elapsed=2, cancelled=False, log_limit=100, reply_limit=100))

        self.assertTrue(second_started.wait(timeout=1))
        release_first.set()


if __name__ == "__main__":
    unittest.main()
