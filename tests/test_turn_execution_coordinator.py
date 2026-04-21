import pathlib
import tempfile
import threading
import unittest

from bot.binding_runtime_manager import BindingRuntimeManager
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_lease_registry import ThreadLeaseRegistry


class TurnExecutionCoordinatorTests(unittest.TestCase):
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

    def test_prime_prompt_turn_locked_sets_local_turn_state(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["execution_transcript"].set_reply_text("stale")

        coordinator.prime_prompt_turn_locked(
            state,
            prompt_message_id="msg-1",
            prompt_reply_in_thread=True,
            actor_open_id="ou-actor",
            started_at=12.5,
        )

        self.assertTrue(state["running"])
        self.assertTrue(state["awaiting_local_turn_started"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["current_prompt_message_id"], "msg-1")
        self.assertTrue(state["current_prompt_reply_in_thread"])
        self.assertEqual(state["current_actor_open_id"], "ou-actor")
        self.assertEqual(state["started_at"], 12.5)
        self.assertEqual(state["last_runtime_event_at"], 12.5)
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["terminal_result_text"], "")

    def test_prepare_turn_started_locked_reuses_existing_execution_card(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_message_id"] = "existing-card"
        state["awaiting_local_turn_started"] = True
        state["running"] = True
        state["pending_cancel"] = True

        transition = coordinator.prepare_turn_started_locked(
            state,
            turn_id="turn-1",
            started_at=20.0,
        )

        self.assertTrue(transition.reuse_existing_card)
        self.assertIsNone(transition.previous_execution_card)
        self.assertTrue(transition.should_interrupt_started_turn)
        self.assertEqual(state["current_message_id"], "existing-card")
        self.assertEqual(state["current_turn_id"], "turn-1")
        self.assertTrue(state["running"])
        self.assertFalse(state["awaiting_local_turn_started"])

    def test_prepare_turn_started_locked_snapshots_previous_card_when_replacing_anchor(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_message_id"] = "old-card"
        state["cancelled"] = True
        state["started_at"] = 2.0
        state["execution_transcript"].set_reply_text("stale reply")

        transition = coordinator.prepare_turn_started_locked(
            state,
            turn_id="turn-2",
            started_at=8.0,
        )

        assert transition.previous_execution_card is not None
        self.assertFalse(transition.reuse_existing_card)
        self.assertEqual(transition.previous_execution_card.message_id, "old-card")
        self.assertEqual(transition.previous_execution_card.transcript.reply_text(), "stale reply")
        self.assertEqual(transition.previous_execution_card.elapsed, 6)
        self.assertTrue(transition.previous_execution_card.cancelled)
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertFalse(state["cancelled"])
        self.assertEqual(state["execution_transcript"].reply_text(), "")

    def test_record_started_turn_id_locked_respects_pending_cancel(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["pending_cancel"] = True

        should_interrupt = coordinator.record_started_turn_id_locked(state, turn_id="turn-3")

        self.assertTrue(should_interrupt)
        self.assertEqual(state["current_turn_id"], "turn-3")

    def test_cancel_and_thread_status_transitions_are_explicit(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_message_id"] = "card-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_turn_id"] = "turn-1"

        self.assertTrue(coordinator.mark_runtime_degraded_locked(state))
        self.assertEqual(state["runtime_channel_state"], "degraded")

        coordinator.request_cancel_without_turn_id_locked(state)
        self.assertTrue(state["cancelled"])
        self.assertTrue(state["pending_cancel"])

        coordinator.acknowledge_active_thread_locked(state)
        self.assertTrue(state["running"])
        self.assertFalse(state["awaiting_local_turn_started"])

        coordinator.confirm_cancel_requested_locked(state)
        self.assertFalse(state["pending_cancel"])
        self.assertTrue(state["cancelled"])

        coordinator.settle_non_active_thread_locked(state)
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["runtime_channel_state"], "live")

        state["running"] = True
        state["pending_cancel"] = True
        coordinator.settle_thread_closed_locked(state)
        self.assertFalse(state["running"])
        self.assertFalse(state["pending_cancel"])

    def test_transcript_mutations_and_snapshot_reconcile_are_coordinator_owned(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()

        coordinator.append_assistant_delta_locked(state, delta="第一段")
        coordinator.start_process_block_locked(
            state,
            text="\n$ (/tmp/project) ls\n",
            marks_work=True,
        )
        coordinator.finish_process_block_locked(
            state,
            suffix="\n[命令结束 status=completed exit=0]\n",
        )
        updated = coordinator.reconcile_current_assistant_text_locked(state, text="第二段")

        self.assertTrue(updated)
        self.assertIn("命令结束", state["execution_transcript"].process_text())
        self.assertEqual(state["execution_transcript"].reply_text(), "第一段\n\n第二段")

        coordinator.apply_snapshot_reply_locked(
            state,
            reply_text="第一段\n\n第二段",
            reply_items=[
                {"type": "agentMessage", "text": "第一段"},
                {"type": "commandExecution"},
                {"type": "agentMessage", "text": "第二段"},
            ],
        )

        self.assertEqual(
            [segment.kind for segment in state["execution_transcript"].reply_segments],
            ["assistant", "divider", "assistant"],
        )

    def test_patch_failure_followup_preparation_is_idempotent_and_uses_prompt_anchor(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["execution_transcript"].set_reply_text("123456789")

        followup = coordinator.prepare_patch_failure_followup_locked(state)

        assert followup is not None
        self.assertEqual(followup.reply_text, "123456789")
        self.assertEqual(followup.prompt_message_id, "msg-1")
        self.assertTrue(followup.prompt_reply_in_thread)
        self.assertTrue(state["followup_sent"])
        self.assertEqual(state["terminal_result_text"], "123456789")
        self.assertIsNone(coordinator.prepare_patch_failure_followup_locked(state))

    def test_plan_state_updates_are_scoped_to_current_turn(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_turn_id"] = "turn-1"

        updated = coordinator.update_plan_outline_locked(
            state,
            turn_id="turn-1",
            explanation="先分析",
            plan=[{"step": "确认需求", "status": "completed"}],
        )

        self.assertTrue(updated)
        self.assertEqual(state["plan_turn_id"], "turn-1")
        self.assertEqual(state["plan_explanation"], "先分析")
        self.assertEqual(state["plan_steps"], [{"step": "确认需求", "status": "completed"}])

        rejected = coordinator.update_plan_outline_locked(
            state,
            turn_id="turn-2",
            explanation="不应覆盖",
            plan=[{"step": "新步骤", "status": "pending"}],
        )

        self.assertFalse(rejected)
        self.assertEqual(state["plan_explanation"], "先分析")

        text_updated = coordinator.update_plan_text_locked(
            state,
            turn_id="turn-1",
            text="1. 确认需求\n2. 实现",
        )
        self.assertTrue(text_updated)
        self.assertEqual(state["plan_text"], "1. 确认需求\n2. 实现")

        shorter = coordinator.update_plan_text_locked(
            state,
            turn_id="turn-1",
            text="1.",
        )
        self.assertFalse(shorter)
        self.assertEqual(state["plan_text"], "1. 确认需求\n2. 实现")

        coordinator.clear_plan_state_locked(state)
        self.assertEqual(state["plan_turn_id"], "")
        self.assertEqual(state["plan_explanation"], "")
        self.assertEqual(state["plan_steps"], [])
        self.assertEqual(state["plan_text"], "")

    def test_apply_turn_completed_locked_sets_reply_or_error_note(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()

        coordinator.apply_turn_completed_locked(
            state,
            status="failed",
            error_message="boom",
        )
        self.assertEqual(state["execution_transcript"].reply_text(), "boom")

        state = self._make_state()
        state["execution_transcript"].set_reply_text("partial")
        coordinator.apply_turn_completed_locked(
            state,
            status="interrupted",
            error_message="boom",
        )

        self.assertTrue(state["cancelled"])
        self.assertIn("[错误] boom", state["execution_transcript"].process_text())
        self.assertEqual(state["execution_transcript"].reply_text(), "partial")

    def test_prepare_finalize_and_retire_execution_locked(self) -> None:
        coordinator = TurnExecutionCoordinator()
        state = self._make_state()
        state["current_message_id"] = "card-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        state["pending_cancel"] = True

        transition = coordinator.prepare_finalize_locked(state)

        self.assertTrue(transition.had_card)
        self.assertFalse(state["running"])
        self.assertFalse(state["pending_cancel"])
        self.assertEqual(state["current_turn_id"], "")

        coordinator.retire_execution_locked(state)

        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "card-1")
        self.assertEqual(state["current_prompt_message_id"], "")
