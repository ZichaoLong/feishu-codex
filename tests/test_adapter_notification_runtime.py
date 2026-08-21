from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from bot.adapter_notification_runtime import (
    AdapterNotificationRuntimeTransitions,
    AssistantDeltaNotificationCommand,
    ErrorNotificationCommand,
    ExecutionRuntimeEventCommand,
    ReconcileAssistantTextCommand,
    RememberTerminalResultTextCommand,
    ThreadRuntimeEventCommand,
    ThreadTitleNotificationCommand,
    WorkItemStartedCommand,
)
from bot.binding_runtime_contract import BindingExecutionTarget
from bot.runtime_state import (
    FEISHU_RUNTIME_DETACHED,
    ExecutionStateChanged,
    apply_runtime_state_message,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state
from tests.runtime_admin_test_support import make_binding_runtime


class AdapterNotificationRuntimeTransitionTests(unittest.TestCase):
    def _make_runtime(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        store = ChatBindingStore(data_dir)
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=store,
        )

        runtime = AdapterNotificationRuntimeTransitions(
            lock=lock,
            binding_runtime=manager,
            turn_execution=TurnExecutionCoordinator(),
        )
        return lock, store, manager, runtime

    @staticmethod
    def _seed_execution(manager, lock, binding):
        with lock:
            state = manager._get_or_create_runtime_state_locked(binding)
            state["current_thread_id"] = "thread-1"
            state["current_thread_title"] = "before"
            state["feishu_runtime_state"] = FEISHU_RUNTIME_DETACHED
            state["current_turn_id"] = "turn-1"
            set_execution_page_state(state, current_message_id="card-1")
            state["running"] = True
            session = manager.resident_session_snapshot_locked(binding)
        assert session is not None
        return state, session

    def test_replacement_between_event_and_projection_rejects_old_command(
        self,
    ) -> None:
        lock, _store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _original_state, captured = self._seed_execution(
            manager,
            lock,
            binding,
        )
        marked = runtime.mark_execution_runtime_event(
            ExecutionRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id="thread-1",
                turn_id="turn-1",
                occurred_at=1.0,
            )
        )
        assert marked is not None

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        replacement_state, replacement = self._seed_execution(
            manager,
            lock,
            binding,
        )

        updated = runtime.append_assistant_delta(
            AssistantDeltaNotificationCommand(
                target=BindingExecutionTarget.from_session(marked),
                delta="stale",
            )
        )
        remembered = runtime.remember_terminal_result_text(
            RememberTerminalResultTextCommand(
                target=BindingExecutionTarget.from_session(marked),
                execution_message_id="card-1",
                text="stale terminal result",
            )
        )

        self.assertIsNone(updated)
        self.assertIsNone(remembered)
        self.assertIsNot(replacement.handle, marked.handle)
        self.assertEqual(replacement_state["execution_transcript"].reply_text(), "")
        self.assertEqual(replacement_state["terminal_result_text"], "")

    def test_deactivate_recreate_a_b_a_does_not_restore_old_authority(self) -> None:
        lock, _store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a1, first_a = self._seed_execution(manager, lock, binding)
        marked = runtime.mark_execution_runtime_event(
            ExecutionRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(first_a),
                thread_id="thread-1",
                turn_id="turn-1",
                occurred_at=1.0,
            )
        )
        assert marked is not None

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_a2, second_a = self._seed_execution(manager, lock, binding)

        updated = runtime.append_assistant_delta(
            AssistantDeltaNotificationCommand(
                target=BindingExecutionTarget.from_session(marked),
                delta="stale-a",
            )
        )

        self.assertIsNone(updated)
        self.assertGreater(second_a.handle.incarnation, marked.handle.incarnation)
        self.assertEqual(state_a2["execution_transcript"].reply_text(), "")

    def test_turn_and_card_business_fence_rejects_same_handle_drift(self) -> None:
        lock, _store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        marked = runtime.mark_execution_runtime_event(
            ExecutionRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id="thread-1",
                turn_id="turn-1",
                occurred_at=1.0,
            )
        )
        assert marked is not None
        with lock:
            set_execution_page_state(state, current_message_id="card-2")
            apply_runtime_state_message(
                state,
                ExecutionStateChanged(
                    current_turn_id="turn-2",
                ),
            )

        updated = runtime.append_assistant_delta(
            AssistantDeltaNotificationCommand(
                target=BindingExecutionTarget.from_session(marked),
                delta="stale",
            )
        )

        self.assertIsNone(updated)
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(state["execution_pages"].current_message_id, "card-2")
        self.assertEqual(state["execution_transcript"].reply_text(), "")

    def test_title_persistence_failure_keeps_live_and_durable_title(self) -> None:
        lock, store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        with lock:
            manager._sync_resident_state_locked(binding, state)
        marked = runtime.mark_thread_runtime_event(
            ThreadRuntimeEventCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id="thread-1",
                occurred_at=1.0,
            )
        )
        assert marked is not None

        with mock.patch.object(
            manager._chat_binding_store,
            "save",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                runtime.apply_thread_title(
                    ThreadTitleNotificationCommand(
                        target=BindingExecutionTarget.from_session(marked),
                        title="after",
                    )
                )

        with lock:
            current = manager.resident_session_snapshot_locked(binding)
        assert current is not None
        self.assertEqual(current.current_thread_title, "before")
        stored = store.load(binding)
        assert stored is not None
        self.assertEqual(stored["current_thread_title"], "before")

    def test_image_generation_after_final_preserves_terminal_evidence(self) -> None:
        lock, _store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        completed = runtime.reconcile_assistant_text(
            ReconcileAssistantTextCommand(
                target=BindingExecutionTarget.from_session(captured),
                text="最终答案",
                item_id="agent-final-1",
            )
        )
        assert completed is not None

        transition = runtime.start_work_item(
            WorkItemStartedCommand(
                target=BindingExecutionTarget.from_session(completed),
                thread_id="thread-1",
                turn_id="turn-1",
                item_type="imageGeneration",
                text="\n[图片生成]\n",
                started_at=2.0,
            )
        )

        self.assertIsNotNone(transition)
        self.assertEqual(
            state["execution_transcript"].terminal_reply_evidence(),
            ("agent", "最终答案"),
        )
        coordinate = state["execution_transcript"].terminal_agent_reply_coordinate()
        assert coordinate is not None
        self.assertEqual(coordinate.item_id, "agent-final-1")

    def test_retryable_error_invalidates_prior_agent_candidate(self) -> None:
        lock, _store, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        completed = runtime.reconcile_assistant_text(
            ReconcileAssistantTextCommand(
                target=BindingExecutionTarget.from_session(captured),
                text="阶段说明",
            )
        )
        assert completed is not None

        updated = runtime.apply_error(
            ErrorNotificationCommand(
                target=BindingExecutionTarget.from_session(completed),
                message="temporary failure",
                will_retry=True,
            )
        )

        self.assertIsNotNone(updated)
        self.assertIsNone(state["execution_transcript"].terminal_reply_evidence())


if __name__ == "__main__":
    unittest.main()
