from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.binding_execution_runtime import (
    ActiveObserverSnapshotItem,
    BindingExecutionRuntimeChanged,
    BindingExecutionRuntimeTransitions,
    InterruptBindingExecutionCommand,
    PrimeActiveObserverExecutionCommand,
    PrimePromptExecutionCommand,
    RecordBindingStartedTurnCommand,
    RollbackDetachedActiveObserverExecutionCommand,
)
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.runtime_admin_test_support import make_binding_runtime


class BindingExecutionRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data_dir = pathlib.Path(temporary.name)
        self.lock = threading.RLock()
        _leases, self.manager = make_binding_runtime(
            data_dir=self.data_dir,
            lock=self.lock,
            chat_binding_store=ChatBindingStore(self.data_dir),
        )
        self.turn_execution = TurnExecutionCoordinator()
        self.runtime = BindingExecutionRuntimeTransitions(
            lock=self.lock,
            binding_runtime=self.manager,
            turn_execution=self.turn_execution,
        )
        self.binding = ("ou-user", "chat-1")

    def _bind(self, thread_id: str) -> BindingSessionSnapshot:
        session = self.manager.resolve_session(*self.binding)
        with self.lock:
            self.manager.bind_thread_locked(
                session.handle,
                thread_id=thread_id,
                thread_title=f"Title {thread_id}",
                working_dir=f"/workspace/{thread_id}",
            )
        return self.manager.resolve_session(*self.binding)

    def _prime_prompt(
        self,
        session: BindingSessionSnapshot,
    ) -> BindingSessionSnapshot:
        return self.runtime.prime_prompt_execution(
            PrimePromptExecutionCommand(
                session=session,
                prompt_message_id="prompt-1",
                prompt_reply_in_thread=False,
                actor_open_id="ou-user",
                started_at=1.0,
                awaiting_attach_status_settle=False,
            )
        )

    def test_started_turn_for_deactivated_session_requires_interrupt(self) -> None:
        primed = self._prime_prompt(self._bind("thread-a"))
        target = BindingExecutionTarget.from_session(primed)
        with self.lock:
            self.manager.deactivate_bindings_with_receipts_locked(
                (self.binding,)
            )
        replacement = self.manager.resolve_session(*self.binding)

        result = self.runtime.record_started_turn(
            RecordBindingStartedTurnCommand(
                target=target,
                turn_id="turn-started-after-capability-loss",
            )
        )

        self.assertFalse(result.committed)
        self.assertTrue(result.should_interrupt)
        self.assertIsNone(result.session)
        self.assertIsNot(replacement.handle, primed.handle)
        self.assertEqual(
            self.manager.resolve_session(*self.binding).execution.current_turn_id,
            "",
        )

    def test_backend_reset_rejects_replaced_exact_session(self) -> None:
        primed = self._prime_prompt(self._bind("thread-a"))
        replacement = self._bind("thread-b")

        with self.assertRaises(BindingExecutionRuntimeChanged):
            self.runtime.interrupt_for_backend_reset(
                InterruptBindingExecutionCommand(
                    session=primed,
                    process_note="must not reach replacement",
                )
            )

        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current, replacement)
        self.assertFalse(current.execution.cancelled)
        self.assertNotIn(
            "must not reach replacement",
            current.execution.transcript.process_text(),
        )

    def test_active_observer_primes_exact_turn_with_partial_history_notice(
        self,
    ) -> None:
        session = self._bind("thread-a")
        with self.lock:
            self.manager.detach_binding_locked(self.binding)
        session = self.manager.resolve_session(*self.binding)

        primed = self.runtime.prime_active_observer_execution(
            PrimeActiveObserverExecutionCommand(
                session=session,
                turn_id="turn-live",
                reply_items=(
                    ActiveObserverSnapshotItem(
                        item_type="agentMessage",
                        text="已有阶段回复",
                        text_available=True,
                    ),
                ),
                started_at=2.0,
            )
        )

        self.assertTrue(primed.execution.running)
        self.assertEqual(primed.execution.current_turn_id, "turn-live")
        self.assertEqual(
            primed.execution.current_execution_kind,
            "active_observer",
        )
        self.assertFalse(primed.execution.awaiting_local_turn_started)
        self.assertIn(
            "此前的执行过程可能不完整",
            primed.execution.transcript.process_text(),
        )
        self.assertEqual(
            primed.execution.transcript.reply_text(),
            "已有阶段回复",
        )

        rolled_back = self.runtime.rollback_detached_active_observer_execution(
            RollbackDetachedActiveObserverExecutionCommand(
                session=session,
                turn_id="turn-live",
            )
        )

        self.assertEqual(
            rolled_back.thread.feishu_runtime_state,
            "detached",
        )
        self.assertFalse(rolled_back.execution.running)
        self.assertEqual(rolled_back.execution.current_turn_id, "")

    def test_backend_reset_interrupt_rolls_back_note_when_state_change_fails(self) -> None:
        primed = self._prime_prompt(self._bind("thread-a"))
        before = self.manager.resolve_session(*self.binding)

        with (
            patch.object(
                self.turn_execution,
                "apply_runtime_state_message_locked",
                side_effect=RuntimeError("state change failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "state change failed"),
        ):
            self.runtime.interrupt_for_backend_reset(
                InterruptBindingExecutionCommand(
                    session=primed,
                    process_note="must roll back with the failed transition",
                )
            )

        after = self.manager.resolve_session(*self.binding)
        self.assertEqual(after, before)
        self.assertNotIn(
            "must roll back with the failed transition",
            after.execution.transcript.process_text(),
        )

if __name__ == "__main__":
    unittest.main()
