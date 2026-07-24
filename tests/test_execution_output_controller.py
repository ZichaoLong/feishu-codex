import pathlib
import tempfile
import threading
import time
import unittest
import json

from bot.card_text_projection import TERMINAL_RESULT_CARD_MARKER, terminal_result_checksum
from bot.cards import build_terminal_result_card_message_content
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.execution_output_controller import ExecutionOutputController
from bot.message_patch_result import MessagePatchResult
from bot.runtime_card_publisher import RuntimeCardPublisher
from bot.runtime_state import ExecutionStateChanged, apply_runtime_state_message
from bot.runtime_view import build_runtime_view
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class _FakeBot:
    def __init__(self) -> None:
        self.reply_refs: list[tuple[str, str, str, bool]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.patch_results: dict[str, bool] = {}
        self.patch_result_sequences: dict[str, list[MessagePatchResult]] = {}

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_refs.append((parent_id, msg_type, content, reply_in_thread))
        return "plan-card-1"

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.sent_messages.append((chat_id, msg_type, content))
        return "plan-card-2"

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return self.patch_results.get(message_id, True)

    def patch_message_result(self, message_id: str, content: str) -> MessagePatchResult:
        self.patches.append((message_id, content))
        sequence = self.patch_result_sequences.get(message_id)
        if sequence:
            return sequence.pop(0)
        if self.patch_results.get(message_id, True):
            return MessagePatchResult.success()
        return MessagePatchResult.failure()


class ExecutionOutputControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(
        self,
        state,
        *,
        card_reply_limit: int = 5,
        terminal_result_card_limit: int = 1000,
    ):
        bot = _FakeBot()
        replies: list[tuple[str, str, str, bool]] = []
        dispatched: list[dict[str, object]] = []
        recorded_terminal_results: list[dict[str, str]] = []
        lock = threading.RLock()
        turn_execution = TurnExecutionCoordinator()

        def _cancel_patch_timer_locked(current_state) -> None:
            timer = current_state["patch_timer"]
            if timer is not None:
                timer.cancel()
            apply_runtime_state_message(current_state, ExecutionStateChanged(patch_timer=None))

        controller = ExecutionOutputController(
            lock=lock,
            runtime_submit=lambda target, *args, **kwargs: target(*args, **kwargs),
            turn_execution=turn_execution,
            get_runtime_state=lambda sender_id, chat_id: state,
            get_runtime_view=lambda sender_id, chat_id: build_runtime_view(state),
            apply_runtime_state_message_locked=apply_runtime_state_message,
            cancel_patch_timer_locked=_cancel_patch_timer_locked,
            card_publisher_factory=lambda: RuntimeCardPublisher(bot),
            dispatch_execution_card_patch=lambda message_id, model: dispatched.append(
                {
                    "message_id": message_id,
                    "running": model.running,
                    "elapsed": model.elapsed,
                    "cancelled": model.cancelled,
                }
            ),
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or True
            ),
            reply_text_get_id=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread))
                or ("text-reply-1" if message_id else "text-message-1")
            ),
            record_terminal_result_card=lambda *, message_id, execution_message_id, final_reply_text, terminal_result_id="", thread_id="", checksum="": recorded_terminal_results.append(
                {
                    "message_id": message_id,
                    "execution_message_id": execution_message_id,
                    "final_reply_text": final_reply_text,
                    "terminal_result_id": terminal_result_id,
                    "thread_id": thread_id,
                    "checksum": checksum,
                }
            ),
            card_reply_limit=lambda: card_reply_limit,
            terminal_result_card_limit=lambda: terminal_result_card_limit,
            card_log_limit=lambda: 100,
            stream_patch_interval_ms=lambda: 1,
        )
        return controller, bot, replies, dispatched, recorded_terminal_results

    def test_flush_execution_card_patch_failure_falls_back_once(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("123456789")
        bot.patch_results["card-1"] = False

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "123456789", "msg-1", True)])
        self.assertTrue(state["followup_sent"])

    def test_flush_execution_card_minimal_fallback_still_sends_terminal_text_once(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("启动失败：后端不可用")
        bot.patch_result_sequences["card-1"] = [
            MessagePatchResult.invalid_content(),
            MessagePatchResult.success(),
        ]

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "启动失败：后端不可用", "msg-1", True)])
        self.assertTrue(state["followup_sent"])
        self.assertEqual(len(bot.patches), 2)
        minimal_card = json.loads(bot.patches[1][1])
        self.assertEqual(minimal_card["body"]["elements"], [{"tag": "markdown", "content": "无"}])

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "启动失败：后端不可用", "msg-1", True)])
        self.assertEqual(len(bot.patches), 3)

    def test_publish_terminal_result_prefers_terminal_result_card_when_reply_fits_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-2",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        parent_id, msg_type, content, reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(parent_id, "msg-2")
        self.assertEqual(msg_type, "interactive")
        self.assertTrue(reply_in_thread)
        card = json.loads(content)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn(TERMINAL_RESULT_CARD_MARKER, card["body"]["elements"][-1]["content"])
        self.assertIn("done", card["body"]["elements"][-1]["content"])
        self.assertRegex(card["body"]["elements"][-1]["element_id"], r"^fc_tr_[0-9a-f]{32}_[0-9a-f]{16}$")
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("plan-card-1", "", "done")],
        )
        self.assertRegex(recorded[0]["terminal_result_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(recorded[0]["thread_id"], "")
        self.assertRegex(recorded[0]["checksum"], r"^[0-9a-f]{64}$")

    def test_publish_terminal_result_records_authoritative_text_without_stripping(self) -> None:
        state = self._make_state()
        controller, _bot, _replies, _, recorded = self._make_controller(state)
        raw_text = "  code output\n"

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-preserve",
        )

        self.assertTrue(ok)
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)
        self.assertEqual(recorded[-1]["checksum"], terminal_result_checksum(raw_text))

    def test_publish_terminal_result_uses_independent_budget_from_execution_card_reply_limit(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            card_reply_limit=3,
            terminal_result_card_limit=1000,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="long enough",
            prompt_message_id="msg-3",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        self.assertEqual(bot.reply_refs[-1][0], "msg-3")
        self.assertEqual(bot.reply_refs[-1][1], "interactive")
        self.assertEqual(recorded[-1]["message_id"], "plan-card-1")
        self.assertEqual(recorded[-1]["execution_message_id"], "")

    def test_publish_terminal_result_falls_back_to_text_when_authoritative_payload_exceeds_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            terminal_result_card_limit=5,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="# 标题\n\n## 小节\n\n- 条目",
            prompt_message_id="msg-3b",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(
            replies,
            [("c1", "# 标题\n\n## 小节\n\n- 条目", "msg-3b", False)],
        )
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("text-reply-1", "", "# 标题\n\n## 小节\n\n- 条目")],
        )

    def test_publish_terminal_result_budget_uses_terminal_result_card_payload_bytes(self) -> None:
        state = self._make_state()
        raw_text = "![x](a.png)"
        card_content = build_terminal_result_card_message_content(
            raw_text,
            terminal_result_id="0" * 32,
            checksum="1" * 64,
        )
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            terminal_result_card_limit=len(card_content.encode("utf-8")) - 1,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-budget",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(replies, [("c1", raw_text, "msg-budget", True)])
        self.assertEqual(recorded[-1]["message_id"], "text-reply-1")
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)

    def test_publish_terminal_result_with_embedded_image_markdown_uses_sanitized_card(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="![示意图](/tmp/demo.png)\n\nPNG 已生成。",
            prompt_message_id="msg-image",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        parent_id, msg_type, content, reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(parent_id, "msg-image")
        self.assertEqual(msg_type, "interactive")
        self.assertFalse(reply_in_thread)
        card = json.loads(content)
        self.assertIn("【图片】示意图", card["body"]["elements"][-1]["content"])
        self.assertIn("PNG 已生成。", card["body"]["elements"][-1]["content"])

    def test_publish_terminal_result_records_raw_text_while_card_uses_feishu_projection(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)
        raw_text = (
            "- 检查参数\n"
            "  ```python\n"
            "  {\"open_timeout\": ..., \"max_size\": None, \"proxy\": None}\n"
            "  ```"
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-code",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        card = json.loads(bot.reply_refs[-1][2])
        content = card["body"]["elements"][-1]["content"]
        self.assertIn("- 检查参数\n\n```python\n", content)
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)
        self.assertEqual(recorded[-1]["checksum"], terminal_result_checksum(raw_text))

    def test_publish_terminal_result_sanitizes_headings_for_feishu_card(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="# 标题\n\n## 小节\n\n- 条目",
            prompt_message_id="msg-heading",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        _parent_id, msg_type, content, _reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(msg_type, "interactive")
        card = json.loads(content)
        self.assertIn("# 标题", card["body"]["elements"][-1]["content"])
        self.assertIn("## 小节", card["body"]["elements"][-1]["content"])

    def test_publish_terminal_result_falls_back_to_top_level_card_before_text(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)

        def _reply_fail(parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str | None:
            bot.reply_refs.append((parent_id, msg_type, content, reply_in_thread))
            return None

        bot.reply_to_message = _reply_fail  # type: ignore[method-assign]

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-4",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        self.assertEqual(bot.reply_refs[-1][0], "msg-4")
        self.assertEqual(bot.sent_messages[-1][0], "c1")
        self.assertEqual(bot.sent_messages[-1][1], "interactive")
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("plan-card-2", "", "done")],
        )

    def test_publish_terminal_result_returns_false_when_text_fallback_fails(self) -> None:
        state = self._make_state()
        bot = _FakeBot()
        replies: list[tuple[str, str, str, bool]] = []
        lock = threading.RLock()
        turn_execution = TurnExecutionCoordinator()

        controller = ExecutionOutputController(
            lock=lock,
            runtime_submit=lambda target, *args, **kwargs: target(*args, **kwargs),
            turn_execution=turn_execution,
            get_runtime_state=lambda sender_id, chat_id: state,
            get_runtime_view=lambda sender_id, chat_id: build_runtime_view(state),
            apply_runtime_state_message_locked=apply_runtime_state_message,
            cancel_patch_timer_locked=lambda current_state: None,
            card_publisher_factory=lambda: RuntimeCardPublisher(bot),
            dispatch_execution_card_patch=lambda message_id, model: None,
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or False
            ),
            reply_text_get_id=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or ""
            ),
            record_terminal_result_card=lambda *, message_id, execution_message_id, final_reply_text, terminal_result_id="", thread_id="", checksum="": None,
            card_reply_limit=lambda: 5,
            terminal_result_card_limit=lambda: 0,
            card_log_limit=lambda: 100,
            stream_patch_interval_ms=lambda: 1,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-5",
            prompt_reply_in_thread=True,
        )

        self.assertFalse(ok)
        self.assertEqual(replies, [("c1", "done", "msg-5", True)])

    def test_schedule_execution_card_update_immediate_path_dispatches_card_patch(self) -> None:
        state = self._make_state()
        controller, bot, _, dispatched, _ = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["started_at"] = time.monotonic() - 1
        state["execution_transcript"].set_reply_text("done")
        state["last_patch_at"] = 0.0

        controller.schedule_execution_card_update("ou_user", "c1")

        self.assertEqual(bot.patches, [])
        self.assertEqual(dispatched[-1]["message_id"], "card-1")

    def test_background_flush_execution_card_dispatches_without_sync_patch(self) -> None:
        state = self._make_state()
        controller, bot, _, dispatched, _ = self._make_controller(state)
        state["current_message_id"] = "card-2"
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("done")

        controller.flush_execution_card("ou_user", "c1", immediate=True, background=True)

        self.assertEqual(bot.patches, [])
        self.assertEqual(dispatched[-1]["message_id"], "card-2")

    def test_refresh_terminal_execution_card_uses_effective_message_id(self) -> None:
        state = self._make_state()
        controller, bot, _, _, _ = self._make_controller(state)
        state["last_execution_message_id"] = "archived-card"
        state["started_at"] = time.monotonic() - 3
        state["execution_transcript"].set_reply_text("complete")

        ok = controller.refresh_terminal_execution_card_from_state("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(bot.patches[-1][0], "archived-card")

    def test_flush_plan_card_reuses_existing_or_updates_message_id(self) -> None:
        state = self._make_state()
        controller, bot, _, _, _ = self._make_controller(state)
        state["current_message_id"] = "exec-1"
        state["plan_message_id"] = "plan-existing"
        state["plan_turn_id"] = "turn-1"
        state["plan_explanation"] = "先分析"
        state["plan_steps"] = [{"step": "确认需求", "status": "completed"}]

        bot.patch_results["plan-existing"] = True
        controller.flush_plan_card("ou_user", "c1")
        self.assertEqual(state["plan_message_id"], "plan-existing")

        bot.patch_results["plan-existing"] = False
        controller.flush_plan_card("ou_user", "c1")

        self.assertEqual(state["plan_message_id"], "plan-card-1")
        self.assertEqual(bot.reply_refs[-1][0], "exec-1")
