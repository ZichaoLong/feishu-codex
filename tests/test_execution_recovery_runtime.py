from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from bot.binding_runtime_contract import BindingExecutionTarget
from bot.execution_recovery_runtime import (
    ApplyExecutionSnapshotCommand,
    CaptureTerminalReconcileTargetCommand,
    CommitCompactStartUnknownCommand,
    ConsumeMirrorWatchdogCommand,
    ExecutionRecoveryRuntimeTransitions,
    ExecutionRuntimeObservationFence,
    InstallMirrorWatchdogCommand,
    MarkExecutionRuntimeDegradedCommand,
    MirrorWatchdogTarget,
    PrepareCompactStartUnknownCommand,
    PrepareMirrorWatchdogCommand,
    RecoverySnapshotReplyItem,
    RollbackMirrorWatchdogCommand,
)
from bot.runtime_state import (
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state
from tests.runtime_admin_test_support import make_binding_runtime


class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ExecutionRecoveryRuntimeTransitionTests(unittest.TestCase):
    def _make_runtime(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=ChatBindingStore(data_dir),
        )
        turn_execution = TurnExecutionCoordinator()
        runtime = ExecutionRecoveryRuntimeTransitions(
            lock=lock,
            binding_runtime=manager,
            turn_execution=turn_execution,
        )
        return lock, manager, turn_execution, runtime

    @staticmethod
    def _seed_execution(
        manager,
        lock,
        binding,
        *,
        started_at: float = 1.0,
        awaiting_started: bool = False,
        execution_kind: str = "prompt",
    ):
        with lock:
            state = manager._get_or_create_runtime_state_locked(binding)
            state["current_thread_id"] = "thread-1"
            state["feishu_runtime_state"] = "attached"
            state["current_turn_id"] = "" if awaiting_started else "turn-1"
            set_execution_page_state(state, current_message_id="card-1")
            state["current_prompt_message_id"] = "prompt-1"
            state["running"] = True
            state["started_at"] = started_at
            state["current_execution_kind"] = execution_kind
            state["awaiting_local_turn_started"] = awaiting_started
            session = manager.resident_session_snapshot_locked(binding)
        assert session is not None
        return state, session

    def test_watchdog_prepare_install_consume_and_replay_use_exact_ticket(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(
            manager,
            lock,
            binding,
            awaiting_started=True,
            execution_kind="compact",
        )

        prepared = runtime.prepare_mirror_watchdog(
            PrepareMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(captured),
                delay_seconds=5.0,
            )
        )
        assert prepared is not None
        registration = MirrorWatchdogRegistration(
            ticket=prepared.ticket,
            timer=_FakeTimer(),
        )
        installed = runtime.install_mirror_watchdog(
            InstallMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(prepared.session),
                registration=registration,
            )
        )
        assert installed is not None
        self.assertIs(state["mirror_watchdog_registration"], registration)

        effect = runtime.consume_mirror_watchdog(
            ConsumeMirrorWatchdogCommand(
                ticket=prepared.ticket,
                occurred_at=3.0,
                compact_start_timeout_seconds=5.0,
            )
        )

        assert effect is not None
        self.assertEqual(effect.action, "reschedule")
        self.assertIs(effect.session.handle, captured.handle)
        self.assertIsNone(state["mirror_watchdog_registration"])
        self.assertIsNone(
            runtime.consume_mirror_watchdog(
                ConsumeMirrorWatchdogCommand(
                    ticket=prepared.ticket,
                    occurred_at=4.0,
                    compact_start_timeout_seconds=5.0,
                )
            )
        )

    def test_watchdog_compact_timeout_returns_exact_unknown_effect(self) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(
            manager,
            lock,
            binding,
            started_at=2.0,
            awaiting_started=True,
            execution_kind="compact",
        )
        ticket = MirrorWatchdogTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="",
        )
        registration = MirrorWatchdogRegistration(
            ticket=ticket,
            timer=_FakeTimer(),
        )
        state["mirror_watchdog_registration"] = registration

        effect = runtime.consume_mirror_watchdog(
            ConsumeMirrorWatchdogCommand(
                ticket=ticket,
                occurred_at=8.0,
                compact_start_timeout_seconds=5.0,
            )
        )

        assert effect is not None
        self.assertEqual(effect.action, "compact_start_unknown")
        self.assertIs(effect.session.handle, captured.handle)

    def test_replacement_after_watchdog_prepare_rejects_install(self) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a, captured = self._seed_execution(manager, lock, binding)
        prepared = runtime.prepare_mirror_watchdog(
            PrepareMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(captured),
                delay_seconds=5.0,
            )
        )
        assert prepared is not None

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, replacement = self._seed_execution(manager, lock, binding)
        registration = MirrorWatchdogRegistration(
            ticket=prepared.ticket,
            timer=_FakeTimer(),
        )

        installed = runtime.install_mirror_watchdog(
            InstallMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(prepared.session),
                registration=registration,
            )
        )

        self.assertIsNone(installed)
        self.assertIsNot(replacement.handle, prepared.session.handle)
        self.assertIsNone(state_b["mirror_watchdog_registration"])

    def test_watchdog_start_rollback_cannot_clear_replacement_registration(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state_a, captured = self._seed_execution(manager, lock, binding)
        ticket_a = MirrorWatchdogTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        registration_a = MirrorWatchdogRegistration(
            ticket=ticket_a,
            timer=_FakeTimer(),
        )
        installed = runtime.install_mirror_watchdog(
            InstallMirrorWatchdogCommand(
                target=MirrorWatchdogTarget.from_session(captured),
                registration=registration_a,
            )
        )
        assert installed is not None
        self.assertIs(state_a["mirror_watchdog_registration"], registration_a)

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, _replacement = self._seed_execution(manager, lock, binding)
        registration_b = MirrorWatchdogRegistration(
            ticket=MirrorWatchdogTicket(
                binding=binding,
                thread_id="thread-1",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=_FakeTimer(),
        )
        state_b["mirror_watchdog_registration"] = registration_b

        rolled_back = runtime.rollback_mirror_watchdog_start(
            RollbackMirrorWatchdogCommand(
                handle=installed.handle,
                registration=registration_a,
            )
        )

        self.assertFalse(rolled_back)
        self.assertIs(state_b["mirror_watchdog_registration"], registration_b)

    def test_active_snapshot_commit_updates_persisted_and_execution_facts(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        state["execution_transcript"].reconcile_current_assistant_text(
            "old commentary"
        )

        transition = runtime.apply_execution_snapshot(
            ApplyExecutionSnapshotCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=ExecutionRuntimeObservationFence.from_session(
                    captured
                ),
                thread_id="thread-1",
                turn_id="turn-1",
                title="new title",
                working_dir="/new/workspace",
                reply_text="snapshot reply",
                reply_items=(
                    RecoverySnapshotReplyItem("agentMessage", "snapshot reply"),
                ),
                turn_status="inProgress",
                thread_active=True,
                occurred_at=7.0,
                invalidates_local_agent_evidence=True,
            )
        )

        assert transition is not None
        self.assertFalse(transition.should_finalize)
        self.assertIsNone(transition.terminal)
        self.assertEqual(state["current_thread_title"], "new title")
        self.assertEqual(state["working_dir"], "/new/workspace")
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "snapshot reply",
        )
        self.assertIsNone(state["execution_transcript"].terminal_reply_evidence())
        self.assertEqual(state["last_runtime_event_at"], 7.0)
        self.assertEqual(state["runtime_channel_state"], "live")

    def test_lifecycle_only_snapshot_commit_skips_metadata_persistence(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        original_title = state["current_thread_title"]
        original_working_dir = state["working_dir"]

        with mock.patch.object(
            manager._chat_binding_store,
            "save",
            side_effect=AssertionError("metadata persistence must be skipped"),
        ):
            transition = runtime.apply_execution_snapshot(
                ApplyExecutionSnapshotCommand(
                    target=BindingExecutionTarget.from_session(captured),
                    observation=ExecutionRuntimeObservationFence.from_session(
                        captured
                    ),
                    thread_id="thread-1",
                    turn_id="turn-1",
                    title="snapshot title",
                    working_dir="/snapshot/workspace",
                    reply_text="snapshot reply",
                    reply_items=(
                        RecoverySnapshotReplyItem(
                            "agentMessage",
                            "snapshot reply",
                        ),
                    ),
                    turn_status="inProgress",
                    thread_active=True,
                    occurred_at=7.0,
                    apply_thread_metadata=False,
                )
            )

        assert transition is not None
        self.assertFalse(transition.should_finalize)
        self.assertEqual(state["current_thread_title"], original_title)
        self.assertEqual(state["working_dir"], original_working_dir)
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "snapshot reply",
        )

    def test_newer_runtime_observation_rejects_snapshot_commit(self) -> None:
        lock, manager, turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        observation = ExecutionRuntimeObservationFence.from_session(captured)

        with lock:
            turn_execution.mark_runtime_event_locked(state, occurred_at=8.0)
            turn_execution.append_assistant_delta_locked(
                state,
                delta="newer notification",
            )

        transition = runtime.apply_execution_snapshot(
            ApplyExecutionSnapshotCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=observation,
                thread_id="thread-1",
                turn_id="turn-1",
                title="stale title",
                working_dir="/stale",
                reply_text="stale terminal reply",
                reply_items=(
                    RecoverySnapshotReplyItem(
                        "agentMessage",
                        "stale terminal reply",
                    ),
                ),
                turn_status="completed",
                thread_active=False,
                occurred_at=9.0,
                apply_thread_metadata=False,
            )
        )

        self.assertIsNone(transition)
        self.assertTrue(state["running"])
        self.assertEqual(state["runtime_channel_state"], "live")
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "newer notification",
        )

    def test_newer_runtime_observation_rejects_degraded_commit(self) -> None:
        lock, manager, turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        observation = ExecutionRuntimeObservationFence.from_session(captured)

        with lock:
            turn_execution.mark_runtime_event_locked(state, occurred_at=8.0)

        updated = runtime.mark_runtime_degraded(
            MarkExecutionRuntimeDegradedCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=observation,
            )
        )

        self.assertIsNone(updated)
        self.assertEqual(state["runtime_channel_state"], "live")

    def test_exact_terminal_turn_finalizes_when_successor_keeps_thread_active(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state, captured = self._seed_execution(manager, lock, binding)

        transition = runtime.apply_execution_snapshot(
            ApplyExecutionSnapshotCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=ExecutionRuntimeObservationFence.from_session(
                    captured
                ),
                thread_id="thread-1",
                turn_id="turn-1",
                title="thread title",
                working_dir="/workspace",
                reply_text="final reply",
                reply_items=(
                    RecoverySnapshotReplyItem("agentMessage", "final reply"),
                ),
                turn_status="completed",
                thread_active=True,
                occurred_at=7.0,
            )
        )

        assert transition is not None
        self.assertTrue(transition.should_finalize)
        self.assertIsNotNone(transition.terminal)
        assert transition.terminal is not None
        self.assertEqual(transition.terminal.turn_id, "turn-1")

    def test_deactivate_recreate_a_b_a_rejects_old_snapshot_commit(self) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a1, first_a = self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_a2, second_a = self._seed_execution(manager, lock, binding)

        transition = runtime.apply_execution_snapshot(
            ApplyExecutionSnapshotCommand(
                target=BindingExecutionTarget.from_session(first_a),
                observation=ExecutionRuntimeObservationFence.from_session(
                    first_a
                ),
                thread_id="thread-1",
                turn_id="turn-1",
                title="stale title",
                working_dir="/stale",
                reply_text="stale reply",
                reply_items=(
                    RecoverySnapshotReplyItem("agentMessage", "stale reply"),
                ),
                turn_status="completed",
                thread_active=False,
                occurred_at=7.0,
            )
        )

        self.assertIsNone(transition)
        self.assertGreater(second_a.handle.incarnation, first_a.handle.incarnation)
        self.assertNotEqual(state_a2["current_thread_title"], "stale title")
        self.assertNotEqual(state_a2["working_dir"], "/stale")
        self.assertEqual(state_a2["execution_transcript"].reply_text(), "")

    def test_snapshot_persistence_failure_leaves_live_execution_unchanged(
        self,
    ) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        original_title = state["current_thread_title"]
        original_working_dir = state["working_dir"]

        with mock.patch.object(
            manager._chat_binding_store,
            "save",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                runtime.apply_execution_snapshot(
                    ApplyExecutionSnapshotCommand(
                        target=BindingExecutionTarget.from_session(captured),
                        observation=ExecutionRuntimeObservationFence.from_session(
                            captured
                        ),
                        thread_id="thread-1",
                        turn_id="turn-1",
                        title="stale title",
                        working_dir="/stale",
                        reply_text="stale reply",
                        reply_items=(
                            RecoverySnapshotReplyItem(
                                "agentMessage",
                                "stale reply",
                            ),
                        ),
                        turn_status="interrupted",
                        thread_active=False,
                        occurred_at=7.0,
                    )
                )

        self.assertEqual(state["current_thread_title"], original_title)
        self.assertEqual(state["working_dir"], original_working_dir)
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertFalse(state["cancelled"])

    def test_degrade_command_rejects_replacement(self) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a, captured = self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, _replacement = self._seed_execution(manager, lock, binding)

        updated = runtime.mark_runtime_degraded(
            MarkExecutionRuntimeDegradedCommand(
                target=BindingExecutionTarget.from_session(captured),
                observation=ExecutionRuntimeObservationFence.from_session(
                    captured
                ),
            )
        )

        self.assertIsNone(updated)
        self.assertEqual(state_b["runtime_channel_state"], "live")

    def test_compact_unknown_commit_rechecks_after_external_effect(self) -> None:
        lock, manager, _turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a, captured = self._seed_execution(
            manager,
            lock,
            binding,
            awaiting_started=True,
            execution_kind="compact",
        )
        prepared = runtime.prepare_compact_start_unknown(
            PrepareCompactStartUnknownCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id="thread-1",
            )
        )
        assert prepared is not None

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, _replacement = self._seed_execution(
            manager,
            lock,
            binding,
            awaiting_started=True,
            execution_kind="compact",
        )

        committed = runtime.commit_compact_start_unknown(
            CommitCompactStartUnknownCommand(
                target=BindingExecutionTarget.from_session(prepared),
                thread_id="thread-1",
                error_text="unknown",
            )
        )

        self.assertIsNone(committed)
        self.assertTrue(state_b["running"])
        self.assertEqual(state_b["execution_transcript"].reply_text(), "")

    def test_terminal_target_is_immutable_and_detached_from_successor_execution(
        self,
    ) -> None:
        lock, manager, turn_execution, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        state["execution_transcript"].set_reply_text("initial")
        with lock:
            captured = manager.session_snapshot_locked(captured.handle)
        target = runtime.capture_terminal_target(
            CaptureTerminalReconcileTargetCommand(
                target=BindingExecutionTarget.from_session(captured),
                thread_id="thread-1",
                turn_id="turn-1",
                occurred_at=5.0,
            )
        )
        assert target is not None

        with lock:
            turn_execution.prepare_finalize_locked(state)
            self.assertTrue(turn_execution.retire_execution_locked(state))

        set_execution_page_state(state, current_message_id="card-2")
        state["current_turn_id"] = "turn-2"
        state["started_at"] = 9.0
        state["running"] = True

        self.assertEqual(target.binding, binding)
        self.assertEqual(target.card_message_id, "card-1")
        self.assertEqual(target.turn_id, "turn-1")
        self.assertEqual(target.transcript.reply_text(), "initial")
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(state["execution_transcript"].reply_text(), "initial")


if __name__ == "__main__":
    unittest.main()
