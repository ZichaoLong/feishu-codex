import pathlib
import tempfile
import threading
import time
import unittest
import json

from bot.binding_runtime_manager import BindingRuntimeManager
from bot.execution_output_controller import ExecutionOutputController
from bot.runtime_card_publisher import RuntimeCardPublisher
from bot.runtime_state import ExecutionStateChanged, apply_runtime_state_message
from bot.runtime_view import build_runtime_view
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_lease_registry import ThreadLeaseRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class _FakeBot:
    def __init__(self) -> None:
        self.reply_refs: list[tuple[str, str, str, bool]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.patch_results: dict[str, bool] = {}

    def reply_to_message(self, parent_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> str:
        self.reply_refs.append((parent_id, msg_type, content, reply_in_thread))
        return "plan-card-1"

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str:
        self.sent_messages.append((chat_id, msg_type, content))
        return "plan-card-2"

    def patch_message(self, message_id: str, content: str) -> bool:
        self.patches.append((message_id, content))
        return self.patch_results.get(message_id, True)


class ExecutionOutputControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_sandbox="workspace-write",
            default_collaboration_mode="default",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_lease_registry=ThreadLeaseRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(self, state, *, card_reply_limit: int = 5):
        bot = _FakeBot()
        replies: list[tuple[str, str, str, bool]] = []
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
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: replies.append(
                (chat_id, text, message_id, reply_in_thread)
            ),
            card_reply_limit=lambda: card_reply_limit,
            card_log_limit=lambda: 100,
            stream_patch_interval_ms=lambda: 1,
        )
        return controller, bot, replies

    def test_flush_execution_card_patch_failure_falls_back_once(self) -> None:
        state = self._make_state()
        controller, bot, replies = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("123456789")
        bot.patch_results["card-1"] = False

        controller.flush_execution_card("ou_user", "c1", immediate=True)
        controller.send_followup_if_needed("ou_user", "c1")

        self.assertEqual(replies, [("c1", "123456789", "msg-1", True)])
        self.assertTrue(state["followup_sent"])

    def test_send_followup_prefers_terminal_result_card_when_reply_fits_card_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies = self._make_controller(state, card_reply_limit=200)
        state["current_message_id"] = "card-1"
        state["current_prompt_message_id"] = "msg-2"
        state["current_prompt_reply_in_thread"] = True
        state["execution_transcript"].set_reply_text("done")

        controller.send_followup_if_needed("ou_user", "c1")

        self.assertEqual(replies, [])
        parent_id, msg_type, content, reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(parent_id, "msg-2")
        self.assertEqual(msg_type, "interactive")
        self.assertTrue(reply_in_thread)
        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "Codex 最终结果")
        self.assertIn("<final_reply_text>", card["elements"][-1]["content"])
        self.assertIn("done", card["elements"][-1]["content"])
        self.assertTrue(state["followup_sent"])

    def test_schedule_execution_card_update_immediate_path_patches_card(self) -> None:
        state = self._make_state()
        controller, bot, _ = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["started_at"] = time.monotonic() - 1
        state["execution_transcript"].set_reply_text("done")
        state["last_patch_at"] = 0.0

        controller.schedule_execution_card_update("ou_user", "c1")

        self.assertEqual(bot.patches[-1][0], "card-1")

    def test_refresh_terminal_execution_card_uses_effective_message_id(self) -> None:
        state = self._make_state()
        controller, bot, _ = self._make_controller(state)
        state["last_execution_message_id"] = "archived-card"
        state["started_at"] = time.monotonic() - 3
        state["execution_transcript"].set_reply_text("complete")

        ok = controller.refresh_terminal_execution_card_from_state("ou_user", "c1")

        self.assertTrue(ok)
        self.assertEqual(bot.patches[-1][0], "archived-card")

    def test_flush_plan_card_reuses_existing_or_updates_message_id(self) -> None:
        state = self._make_state()
        controller, bot, _ = self._make_controller(state)
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
