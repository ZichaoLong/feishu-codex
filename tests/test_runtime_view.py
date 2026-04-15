import unittest

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_view import build_runtime_view


def _build_state() -> dict[str, object]:
    transcript = ExecutionTranscript()
    transcript.set_reply_text("hello")
    return {
        "active": True,
        "working_dir": "/tmp/project",
        "current_thread_id": "thread-1",
        "current_thread_title": "demo",
        "current_turn_id": "turn-1",
        "running": True,
        "cancelled": False,
        "pending_cancel": False,
        "current_message_id": "card-1",
        "last_execution_message_id": "card-old",
        "current_prompt_message_id": "prompt-1",
        "current_actor_open_id": "ou_user",
        "execution_transcript": transcript,
        "runtime_channel_state": "live",
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
        "model": "gpt-5.4",
        "reasoning_effort": "high",
        "plan_message_id": "plan-1",
        "plan_turn_id": "turn-1",
        "plan_explanation": "old explanation",
        "plan_steps": [{"step": "old", "status": "completed"}],
        "plan_text": "old text",
    }


class RuntimeViewTests(unittest.TestCase):
    def test_build_runtime_view_captures_read_side_projection(self) -> None:
        view = build_runtime_view(_build_state())

        self.assertTrue(view.active)
        self.assertEqual(view.working_dir, "/tmp/project")
        self.assertEqual(view.current_thread_id, "thread-1")
        self.assertEqual(view.current_thread_title, "demo")
        self.assertTrue(view.running)
        self.assertEqual(view.approval_policy, "on-request")
        self.assertEqual(view.sandbox, "workspace-write")
        self.assertEqual(view.collaboration_mode, "default")
        self.assertEqual(view.execution.effective_message_id, "card-1")
        self.assertTrue(view.execution.has_execution_anchor)
        self.assertEqual(view.plan.steps[0].step, "old")

    def test_view_holds_cloned_transcript(self) -> None:
        state = _build_state()
        original = state["execution_transcript"]
        assert isinstance(original, ExecutionTranscript)

        view = build_runtime_view(state)
        original.set_reply_text("mutated after snapshot")

        self.assertEqual(view.execution.transcript.reply_text(), "hello")
        self.assertEqual(original.reply_text(), "mutated after snapshot")


if __name__ == "__main__":
    unittest.main()
