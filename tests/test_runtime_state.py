import unittest

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    ExecutionRetired,
    ExecutionStateChanged,
    PlanStateChanged,
    RuntimeSettingsChanged,
    ThreadStateChanged,
    apply_runtime_state_message,
)


def _build_state() -> dict[str, object]:
    return {
        "active": False,
        "working_dir": "/tmp/project",
        "current_thread_id": "thread-1",
        "current_thread_title": "demo",
        "current_turn_id": "turn-1",
        "running": True,
        "cancelled": False,
        "pending_cancel": True,
        "current_message_id": "card-1",
        "last_execution_message_id": "",
        "current_prompt_message_id": "prompt-1",
        "current_prompt_reply_in_thread": True,
        "current_actor_open_id": "ou_user",
        "execution_transcript": ExecutionTranscript(),
        "runtime_channel_state": "degraded",
        "started_at": 12.0,
        "last_runtime_event_at": 10.0,
        "last_patch_at": 2.0,
        "patch_timer": None,
        "mirror_watchdog_timer": None,
        "mirror_watchdog_generation": 4,
        "followup_sent": False,
        "awaiting_local_turn_started": True,
        "approval_policy": "on-request",
        "sandbox": "workspace-write",
        "collaboration_mode": "default",
        "model": "",
        "reasoning_effort": "",
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
        self.assertEqual(state["last_execution_message_id"], "card-1")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["current_turn_id"], "")
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
            ),
        )

        self.assertEqual(transcript.reply_text(), "fresh reply")
        self.assertEqual(transcript.process_text(), "")
        self.assertTrue(state["followup_sent"])

    def test_runtime_settings_and_thread_state_only_change_requested_fields(self) -> None:
        state = _build_state()

        apply_runtime_state_message(
            state,
            RuntimeSettingsChanged(approval_policy="never"),
        )
        apply_runtime_state_message(
            state,
            ThreadStateChanged(current_thread_title="renamed"),
        )

        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["sandbox"], "workspace-write")
        self.assertEqual(state["collaboration_mode"], "default")
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


if __name__ == "__main__":
    unittest.main()
