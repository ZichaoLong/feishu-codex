from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, replace

from bot.binding_runtime_contract import (
    BindingExecutionSnapshot,
    BindingExecutionTarget,
    BindingGoalSnapshot,
    BindingPlanSnapshot,
    BindingPlanStepSnapshot,
    BindingRuntimeHandle,
    BindingRuntimeSettingsSnapshot,
    BindingSessionSnapshot,
    BindingThreadSnapshot,
)
from bot.execution_transcript import (
    ExecutionReplySegmentSnapshot,
    ExecutionTranscriptSnapshot,
)
from tests.execution_page_test_support import execution_page_ledger


class BindingRuntimeContractTests(unittest.TestCase):
    @staticmethod
    def _session() -> BindingSessionSnapshot:
        handle = BindingRuntimeHandle(
            _issuer_nonce=17,
            binding=("ou-user", "chat-1"),
            incarnation=3,
        )
        return BindingSessionSnapshot(
            handle=handle,
            active=True,
            thread=BindingThreadSnapshot(
                working_dir="/workspace",
                thread_id="thread-1",
                title="Demo",
                feishu_runtime_state="attached",
            ),
            settings=BindingRuntimeSettingsSnapshot(
                approval_policy="on-request",
                permissions_profile_id="workspace-write",
                model="gpt-5",
                reasoning_effort="high",
                configured_settings=("model", "reasoning_effort"),
            ),
            goal=BindingGoalSnapshot(
                objective="finish the migration",
                status="active",
                token_budget=1000,
                tokens_used=120,
                time_used_seconds=8,
                created_at=10,
                updated_at=20,
            ),
            execution=BindingExecutionSnapshot(
                running=True,
                cancelled=False,
                pending_cancel=False,
                current_turn_id="turn-1",
                pages=execution_page_ledger(
                    current_message_id="card-1",
                    last_message_id="card-0",
                ),
                current_execution_kind="prompt",
                current_prompt_message_id="message-1",
                current_prompt_reply_in_thread=True,
                current_actor_open_id="ou-actor",
                transcript=ExecutionTranscriptSnapshot(
                    reply_segments=(
                        ExecutionReplySegmentSnapshot("assistant", "hello"),
                    ),
                    process_blocks=("running",),
                    active_reply_index=0,
                    active_process_index=None,
                    pending_reply_divider=True,
                    had_assistant_output=True,
                ),
                runtime_channel_state="live",
                started_at=1.5,
                last_runtime_event_at=2.5,
                last_patch_at=3.5,
                patch_timer_registered=True,
                mirror_watchdog_registered=True,
                followup_sent=False,
                followup_text="",
                terminal_result_text="",
                awaiting_local_turn_started=True,
                awaiting_attach_status_settle=True,
            ),
            plan=BindingPlanSnapshot(
                message_id="plan-card",
                turn_id="turn-1",
                explanation="ordered work",
                steps=(
                    BindingPlanStepSnapshot("inspect", "completed"),
                    BindingPlanStepSnapshot("migrate", "in_progress"),
                ),
                text="inspect, then migrate",
            ),
        )

    def test_runtime_handle_is_identity_authority_not_a_value_token(self) -> None:
        handle = self._session().handle
        copied = copy.copy(handle)
        reconstructed = BindingRuntimeHandle(
            _issuer_nonce=handle._issuer_nonce,
            binding=handle.binding,
            incarnation=handle.incarnation,
        )
        replaced = replace(handle)

        self.assertIsNot(copied, handle)
        self.assertIsNot(reconstructed, handle)
        self.assertIsNot(replaced, handle)
        self.assertNotEqual(copied, handle)
        self.assertNotEqual(reconstructed, handle)
        self.assertNotEqual(replaced, handle)
        self.assertEqual(handle, handle)

    def test_session_shape_is_frozen_slotted_and_deeply_immutable(self) -> None:
        session = self._session()

        for value in (
            session,
            session.handle,
            session.thread,
            session.settings,
            session.goal,
            session.execution,
            session.execution.transcript,
            session.execution.transcript.reply_segments[0],
            session.plan,
        ):
            self.assertFalse(hasattr(value, "__dict__"), type(value).__name__)

        self.assertIsInstance(session.settings.configured_settings, tuple)
        self.assertIsInstance(session.execution.transcript.reply_segments, tuple)
        self.assertIsInstance(session.execution.transcript.process_blocks, tuple)
        self.assertIsInstance(session.plan.steps, tuple)
        self.assertTrue(
            all(type(step) is BindingPlanStepSnapshot for step in session.plan.steps)
        )

        with self.assertRaises(FrozenInstanceError):
            session.active = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            session.execution.running = False  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            session.execution.transcript.reply_segments = ()  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            session.plan.steps.append(("ship", "pending"))  # type: ignore[attr-defined]

    def test_session_vocabulary_covers_every_runtime_state_fact(self) -> None:
        session = self._session()

        self.assertEqual(session.binding, session.handle.binding)
        self.assertEqual(session.thread.thread_id, "thread-1")
        self.assertEqual(
            session.settings.configured_settings, ("model", "reasoning_effort")
        )
        self.assertEqual(session.goal.token_budget, 1000)
        self.assertEqual(session.execution.current_turn_id, "turn-1")
        self.assertEqual(session.execution.current_message_id, "card-1")
        self.assertEqual(session.execution.last_execution_message_id, "card-0")
        self.assertEqual(session.execution.current_execution_kind, "prompt")
        self.assertEqual(session.execution.transcript.reply_text(), "hello")
        self.assertTrue(session.execution.patch_timer_registered)
        self.assertTrue(session.execution.mirror_watchdog_registered)
        self.assertTrue(session.execution.awaiting_local_turn_started)
        self.assertTrue(session.execution.awaiting_attach_status_settle)
        self.assertEqual(
            session.plan.steps[1],
            BindingPlanStepSnapshot("migrate", "in_progress"),
        )

    def test_session_derives_binding_and_exposes_canonical_read_helpers(self) -> None:
        session = self._session()

        self.assertEqual(session.binding, session.handle.binding)
        self.assertTrue(session.thread.has_thread)
        self.assertTrue(session.thread.feishu_runtime_attached)
        self.assertTrue(session.goal.exists)
        self.assertEqual(session.execution.effective_message_id, "card-1")
        self.assertTrue(session.execution.has_execution_anchor)
        self.assertEqual(session.working_dir, "/workspace")
        self.assertEqual(session.current_thread_id, "thread-1")
        self.assertEqual(session.current_thread_title, "Demo")
        self.assertTrue(session.running)
        self.assertEqual(session.approval_policy, "on-request")
        self.assertEqual(session.permissions_profile_id, "workspace-write")
        self.assertEqual(session.model, "gpt-5")
        self.assertEqual(session.reasoning_effort, "high")

    def test_session_rejects_mutable_nested_values_and_type_subclasses(self) -> None:
        session = self._session()

        with self.assertRaises(TypeError):
            BindingRuntimeSettingsSnapshot(
                approval_policy="on-request",
                permissions_profile_id="workspace-write",
                model="gpt-5",
                reasoning_effort="high",
                configured_settings=["model"],  # type: ignore[arg-type]
            )
        with self.assertRaises(TypeError):
            BindingPlanSnapshot(
                message_id="plan-card",
                turn_id="turn-1",
                explanation="ordered work",
                steps=[["inspect", "completed"]],  # type: ignore[arg-type,list-item]
                text="inspect",
            )
        with self.assertRaises(TypeError):
            BindingSessionSnapshot(
                handle=session.handle,
                active=1,  # type: ignore[arg-type]
                thread=session.thread,
                settings=session.settings,
                goal=session.goal,
                execution=session.execution,
                plan=session.plan,
            )

        class StringSubclass(str):
            pass

        with self.assertRaises(TypeError):
            BindingPlanStepSnapshot(StringSubclass("inspect"), "pending")

    def test_execution_target_is_strict_and_matches_only_its_business_fence(
        self,
    ) -> None:
        session = self._session()
        target = BindingExecutionTarget.from_session(session)

        self.assertIs(target.handle, session.handle)
        self.assertEqual(target.binding, session.binding)
        self.assertTrue(target.matches(session))
        self.assertFalse(target.matches(object()))  # type: ignore[arg-type]

        with self.assertRaises(TypeError):
            BindingExecutionTarget.from_session(object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            replace(target, handle=object())  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            replace(target, expected_turn_id=type("Text", (str,), {})("turn-1"))
        with self.assertRaises(TypeError):
            replace(target, expected_started_at=1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            replace(target, expected_started_at=-1.0)

        execution = session.execution
        drifted_sessions = (
            replace(session, handle=copy.copy(session.handle)),
            replace(
                session,
                thread=replace(session.thread, thread_id="thread-2"),
            ),
            replace(
                session,
                execution=replace(execution, current_turn_id="turn-2"),
            ),
            replace(
                session,
                execution=replace(
                    execution,
                    pages=execution_page_ledger(
                        current_message_id="card-2",
                        last_message_id="card-0",
                    ),
                ),
            ),
            replace(
                session,
                execution=replace(
                    execution,
                    pages=execution_page_ledger(
                        current_message_id="card-1",
                        last_message_id="card-other",
                    ),
                ),
            ),
            replace(
                session,
                execution=replace(
                    execution,
                    current_prompt_message_id="message-2",
                ),
            ),
            replace(
                session,
                execution=replace(execution, current_execution_kind="resume"),
            ),
            replace(
                session,
                execution=replace(execution, started_at=2.0),
            ),
        )
        for drifted in drifted_sessions:
            with self.subTest(drifted=drifted):
                self.assertFalse(target.matches(drifted))

        volatile_drift = replace(
            session,
            execution=replace(
                execution,
                running=False,
                last_runtime_event_at=7.0,
                last_patch_at=8.0,
            ),
        )
        self.assertTrue(target.matches(volatile_drift))


if __name__ == "__main__":
    unittest.main()
