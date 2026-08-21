import unittest

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    ExecutionRetired,
    ExecutionStateChanged,
    PlanStateChanged,
    RuntimeSettingsChanged,
    ThreadGoalCleared,
    ThreadGoalStateChanged,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from tests.execution_page_test_support import execution_page_ledger


def _build_state() -> dict[str, object]:
    return {
        "active": False,
        "working_dir": "/tmp/project",
        "current_thread_id": "thread-1",
        "current_thread_title": "demo",
        "feishu_runtime_state": "attached",
        "goal_objective": "",
        "goal_status": "",
        "goal_token_budget": None,
        "goal_tokens_used": 0,
        "goal_time_used_seconds": 0,
        "goal_created_at": 0,
        "goal_updated_at": 0,
        "current_turn_id": "turn-1",
        "running": True,
        "cancelled": False,
        "pending_cancel": True,
        "execution_pages": execution_page_ledger(current_message_id="card-1"),
        "current_execution_kind": "prompt",
        "current_prompt_message_id": "prompt-1",
        "current_prompt_reply_in_thread": True,
        "current_actor_open_id": "ou_user",
        "execution_transcript": ExecutionTranscript(),
        "runtime_channel_state": "degraded",
        "started_at": 12.0,
        "last_runtime_event_at": 10.0,
        "last_patch_at": 2.0,
        "patch_timer_registration": None,
        "mirror_watchdog_registration": None,
        "followup_sent": False,
        "followup_text": "",
        "terminal_result_text": "",
        "awaiting_local_turn_started": True,
        "approval_policy": "on-request",
        "permissions_profile_id": ":workspace",
        "model": "",
        "reasoning_effort": "",
        "configured_settings": [],
        "plan_message_id": "plan-1",
        "plan_turn_id": "turn-1",
        "plan_explanation": "old explanation",
        "plan_steps": [{"step": "old", "status": "completed"}],
        "plan_text": "old text",
    }


class RuntimeStateReducerTests(unittest.TestCase):
    def test_execution_retired_moves_card_to_last_execution_and_clears_anchor(self) -> None:
        state = _build_state()

        apply_runtime_state_message(state, ExecutionRetired())

        self.assertFalse(state["running"])
        self.assertFalse(state["pending_cancel"])
        self.assertEqual(state["execution_pages"].last_message_id, "card-1")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["current_execution_kind"], "")
        self.assertEqual(state["current_prompt_message_id"], "")
        self.assertFalse(state["current_prompt_reply_in_thread"])
        self.assertEqual(state["current_actor_open_id"], "")
        self.assertFalse(state["awaiting_local_turn_started"])
        self.assertEqual(state["runtime_channel_state"], "live")

    def test_execution_state_changed_can_reset_transcript_and_replace_reply_text(self) -> None:
        state = _build_state()
        transcript = state["execution_transcript"]
        assert isinstance(transcript, ExecutionTranscript)
        transcript.set_reply_text("stale")
        transcript.start_process_block("$ ls\n", marks_work=True)

        apply_runtime_state_message(
            state,
            ExecutionStateChanged(
                reset_transcript=True,
                reply_text="fresh reply",
                followup_sent=True,
                followup_text="fresh reply",
                terminal_result_text="fresh reply",
            ),
        )

        self.assertEqual(transcript.reply_text(), "fresh reply")
        self.assertEqual(transcript.process_text(), "")
        self.assertTrue(state["followup_sent"])
        self.assertEqual(state["followup_text"], "fresh reply")
        self.assertEqual(state["terminal_result_text"], "fresh reply")

    def test_runtime_settings_and_thread_state_only_change_requested_fields(self) -> None:
        state = _build_state()

        apply_runtime_state_message(
            state,
            RuntimeSettingsChanged(
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
                model="gpt-5.5",
                reasoning_effort="high",
            ),
        )
        apply_runtime_state_message(
            state,
            ThreadStateChanged(current_thread_title="renamed"),
        )

        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(state["model"], "gpt-5.5")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertEqual(
            state["configured_settings"],
            ["approval_policy", "model", "permissions_profile_id", "reasoning_effort"],
        )
        self.assertEqual(state["current_thread_title"], "renamed")
        self.assertEqual(state["working_dir"], "/tmp/project")
        self.assertEqual(state["current_thread_id"], "thread-1")

    def test_plan_state_clear_resets_all_plan_fields(self) -> None:
        state = _build_state()

        apply_runtime_state_message(state, PlanStateChanged(clear=True))

        self.assertEqual(state["plan_message_id"], "")
        self.assertEqual(state["plan_turn_id"], "")
        self.assertEqual(state["plan_explanation"], "")
        self.assertEqual(state["plan_steps"], [])
        self.assertEqual(state["plan_text"], "")

    def test_goal_projection_can_update_and_clear(self) -> None:
        state = _build_state()

        apply_runtime_state_message(
            state,
            ThreadGoalStateChanged(
                goal_objective="ship goal support",
                goal_status="active",
                goal_token_budget=100,
                goal_tokens_used=12,
                goal_time_used_seconds=34,
                goal_created_at=1712476800,
                goal_updated_at=1712476801,
            ),
        )
        apply_runtime_state_message(state, ThreadGoalCleared())

        self.assertEqual(state["goal_objective"], "")
        self.assertEqual(state["goal_status"], "")
        self.assertIsNone(state["goal_token_budget"])
        self.assertEqual(state["goal_tokens_used"], 0)
        self.assertEqual(state["goal_time_used_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
