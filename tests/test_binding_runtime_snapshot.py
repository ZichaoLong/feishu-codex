from __future__ import annotations

import unittest

from bot.binding_runtime_contract import (
    BindingPlanStepSnapshot,
    BindingRuntimeHandle,
)
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
    RuntimeStateDict,
)
from tests.execution_page_test_support import execution_page_ledger


class _Timer:
    def cancel(self) -> None:
        pass


class BindingRuntimeSnapshotProjectorTests(unittest.TestCase):
    binding = ("ou-user", "chat-1")

    @classmethod
    def _handle(cls) -> BindingRuntimeHandle:
        return BindingRuntimeHandle(
            _issuer_nonce=9,
            binding=cls.binding,
            incarnation=4,
        )

    @classmethod
    def _state(cls) -> RuntimeStateDict:
        transcript = ExecutionTranscript(
            reply_segments=[ExecutionReplySegment("assistant", "captured reply")],
            process_blocks=["captured process"],
            _active_reply_index=0,
            _had_assistant_output=True,
        )
        patch_ticket = ExecutionPatchTimerTicket(
            binding=cls.binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        watchdog_ticket = MirrorWatchdogTicket(
            binding=cls.binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        return {
            "active": True,
            "working_dir": " /workspace ",
            "current_thread_id": "thread-1",
            "current_thread_title": " Demo ",
            "feishu_runtime_state": "attached",
            "goal_objective": "finish the projector",
            "goal_status": "active",
            "goal_token_budget": 1000,
            "goal_tokens_used": 120,
            "goal_time_used_seconds": 8,
            "goal_created_at": 10,
            "goal_updated_at": 20,
            "current_turn_id": "turn-1",
            "running": True,
            "cancelled": False,
            "pending_cancel": True,
            "execution_pages": execution_page_ledger(
                current_message_id="card-1",
                last_message_id="card-0",
            ),
            "current_execution_kind": "prompt",
            "current_prompt_message_id": "message-1",
            "current_prompt_reply_in_thread": True,
            "current_actor_open_id": "ou-actor",
            "execution_transcript": transcript,
            "runtime_channel_state": "live",
            "started_at": 1.5,
            "last_runtime_event_at": 2.5,
            "last_patch_at": 3.5,
            "patch_timer_registration": ExecutionPatchTimerRegistration(
                ticket=patch_ticket,
                timer=_Timer(),
            ),
            "mirror_watchdog_registration": MirrorWatchdogRegistration(
                ticket=watchdog_ticket,
                timer=_Timer(),
            ),
            "followup_sent": False,
            "followup_text": "follow-up",
            "terminal_result_text": "terminal",
            "awaiting_local_turn_started": True,
            "awaiting_attach_status_settle": True,
            "approval_policy": "on-request",
            "permissions_profile_id": ":workspace",
            "model": "gpt-5",
            "reasoning_effort": "high",
            "configured_settings": ["model", "reasoning_effort"],
            "plan_message_id": "plan-card",
            "plan_turn_id": "turn-1",
            "plan_explanation": "ordered work",
            "plan_steps": [
                {"step": "inspect", "status": "completed"},
                {"step": "project", "status": "in_progress"},
            ],
            "plan_text": "inspect, then project",
        }

    def test_projector_covers_the_complete_runtime_state_without_coercion(
        self,
    ) -> None:
        state = self._state()
        handle = self._handle()

        snapshot = project_binding_session_snapshot(state, handle=handle)

        self.assertIs(snapshot.handle, handle)
        self.assertEqual(snapshot.binding, self.binding)
        self.assertTrue(snapshot.active)
        self.assertEqual(snapshot.thread.working_dir, " /workspace ")
        self.assertEqual(snapshot.thread.thread_id, "thread-1")
        self.assertEqual(snapshot.thread.title, " Demo ")
        self.assertEqual(snapshot.thread.feishu_runtime_state, "attached")
        self.assertEqual(snapshot.settings.approval_policy, "on-request")
        self.assertEqual(snapshot.settings.permissions_profile_id, ":workspace")
        self.assertEqual(snapshot.settings.model, "gpt-5")
        self.assertEqual(snapshot.settings.reasoning_effort, "high")
        self.assertEqual(
            snapshot.settings.configured_settings,
            ("model", "reasoning_effort"),
        )
        self.assertEqual(snapshot.goal.objective, "finish the projector")
        self.assertEqual(snapshot.goal.status, "active")
        self.assertEqual(snapshot.goal.token_budget, 1000)
        self.assertEqual(snapshot.goal.tokens_used, 120)
        self.assertEqual(snapshot.goal.time_used_seconds, 8)
        self.assertEqual(snapshot.goal.created_at, 10)
        self.assertEqual(snapshot.goal.updated_at, 20)

        execution = snapshot.execution
        self.assertTrue(execution.running)
        self.assertFalse(execution.cancelled)
        self.assertTrue(execution.pending_cancel)
        self.assertEqual(execution.current_turn_id, "turn-1")
        self.assertEqual(execution.current_message_id, "card-1")
        self.assertEqual(execution.last_execution_message_id, "card-0")
        self.assertEqual(execution.current_execution_kind, "prompt")
        self.assertEqual(execution.current_prompt_message_id, "message-1")
        self.assertTrue(execution.current_prompt_reply_in_thread)
        self.assertEqual(execution.current_actor_open_id, "ou-actor")
        self.assertEqual(execution.transcript.reply_text(), "captured reply")
        self.assertEqual(execution.transcript.process_text(), "captured process")
        self.assertEqual(execution.transcript.active_reply_index, 0)
        self.assertIsNone(execution.transcript.active_process_index)
        self.assertFalse(execution.transcript.pending_reply_divider)
        self.assertTrue(execution.transcript.had_assistant_output)
        self.assertEqual(execution.runtime_channel_state, "live")
        self.assertEqual(execution.started_at, 1.5)
        self.assertEqual(execution.last_runtime_event_at, 2.5)
        self.assertEqual(execution.last_patch_at, 3.5)
        self.assertTrue(execution.patch_timer_registered)
        self.assertTrue(execution.mirror_watchdog_registered)
        self.assertFalse(execution.followup_sent)
        self.assertEqual(execution.followup_text, "follow-up")
        self.assertEqual(execution.terminal_result_text, "terminal")
        self.assertTrue(execution.awaiting_local_turn_started)
        self.assertTrue(execution.awaiting_attach_status_settle)

        self.assertEqual(snapshot.plan.message_id, "plan-card")
        self.assertEqual(snapshot.plan.turn_id, "turn-1")
        self.assertEqual(snapshot.plan.explanation, "ordered work")
        self.assertEqual(
            snapshot.plan.steps,
            (
                BindingPlanStepSnapshot("inspect", "completed"),
                BindingPlanStepSnapshot("project", "in_progress"),
            ),
        )
        self.assertEqual(snapshot.plan.text, "inspect, then project")

    def test_projector_deeply_detaches_all_mutable_runtime_containers(self) -> None:
        state = self._state()
        transcript = state["execution_transcript"]
        original_segment = transcript.reply_segments[0]
        original_plan_step = state["plan_steps"][0]
        snapshot = project_binding_session_snapshot(state, handle=self._handle())

        state["configured_settings"].append("approval_policy")
        original_plan_step["step"] = "mutated"
        state["plan_steps"].append({"step": "later", "status": "pending"})
        transcript.set_reply_text("mutated reply")
        transcript.process_blocks[0] = "mutated process"

        self.assertEqual(
            snapshot.settings.configured_settings,
            ("model", "reasoning_effort"),
        )
        self.assertEqual(
            snapshot.plan.steps,
            (
                BindingPlanStepSnapshot("inspect", "completed"),
                BindingPlanStepSnapshot("project", "in_progress"),
            ),
        )
        self.assertNotIsInstance(snapshot.plan.steps[0], dict)
        self.assertEqual(snapshot.execution.transcript.reply_text(), "captured reply")
        self.assertEqual(
            snapshot.execution.transcript.process_text(),
            "captured process",
        )
        self.assertIsNot(
            snapshot.execution.transcript.reply_segments[0],
            original_segment,
        )

    def test_projector_fails_closed_for_corrupt_primitives_and_raw_subclasses(
        self,
    ) -> None:
        class StateSubclass(dict):
            pass

        class StringSubclass(str):
            pass

        class ListSubclass(list):
            pass

        class StepSubclass(dict):
            pass

        corrupt_states: list[tuple[str, object]] = []

        state_subclass = StateSubclass(self._state())
        corrupt_states.append(("state subclass", state_subclass))

        primitive_subclass = self._state()
        primitive_subclass["working_dir"] = StringSubclass("/workspace")
        corrupt_states.append(("primitive subclass", primitive_subclass))

        bool_as_int = self._state()
        bool_as_int["running"] = 1  # type: ignore[typeddict-item]
        corrupt_states.append(("integer bool", bool_as_int))

        int_as_float = self._state()
        int_as_float["started_at"] = 1  # type: ignore[typeddict-item]
        corrupt_states.append(("integer timestamp", int_as_float))

        list_subclass = self._state()
        list_subclass["configured_settings"] = ListSubclass(["model"])
        corrupt_states.append(("list subclass", list_subclass))

        step_subclass = self._state()
        step_subclass["plan_steps"] = [
            StepSubclass(step="inspect", status="pending")
        ]
        corrupt_states.append(("plan-step subclass", step_subclass))

        malformed_step = self._state()
        malformed_step["plan_steps"] = [{"step": "inspect", "extra": "value"}]  # type: ignore[list-item]
        corrupt_states.append(("malformed plan step", malformed_step))

        invalid_registration = self._state()
        invalid_registration["patch_timer_registration"] = object()  # type: ignore[typeddict-item]
        corrupt_states.append(("invalid timer registration", invalid_registration))

        mismatched_ticket = self._state()
        mismatched_ticket["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ExecutionPatchTimerTicket(
                binding=("ou-other", "chat-2"),
                thread_id="thread-1",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=_Timer(),
        )
        corrupt_states.append(("mismatched timer binding", mismatched_ticket))

        for label, corrupt in corrupt_states:
            with self.subTest(label=label), self.assertRaises(TypeError):
                project_binding_session_snapshot(
                    corrupt,  # type: ignore[arg-type]
                    handle=self._handle(),
                )

    def test_projector_requires_complete_schema_and_exact_handle(self) -> None:
        missing = self._state()
        missing.pop("plan_text")
        extra = self._state()
        extra["unexpected"] = "fact"  # type: ignore[typeddict-unknown-key]

        for label, state in (("missing", missing), ("extra", extra)):
            with self.subTest(label=label), self.assertRaises(TypeError):
                project_binding_session_snapshot(state, handle=self._handle())

        with self.assertRaises(TypeError):
            project_binding_session_snapshot(
                self._state(),
                handle=object(),  # type: ignore[arg-type]
            )

        handle = self._handle()
        snapshot = project_binding_session_snapshot(self._state(), handle=handle)
        self.assertIs(snapshot.handle, handle)
        self.assertEqual(snapshot.binding, handle.binding)


if __name__ == "__main__":
    unittest.main()
