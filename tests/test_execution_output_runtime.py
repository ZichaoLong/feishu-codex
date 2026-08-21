from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest

from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.execution_output_runtime import (
    CaptureExecutionPlanCardCommand,
    CommitExecutionPageRolloverCommand,
    CommitExecutionPageSendUnknownReconciliationCommand,
    CommitExecutionPlanCardCommand,
    CommitInitialExecutionPageCommand,
    ConsumeExecutionPatchTimerCommand,
    ExecutionOutputRuntimeTransitions,
    ExecutionOutputTarget,
    ExecutionPatchTimerInstallPreparation,
    InstallExecutionPatchTimerCommand,
    PrepareExecutionPageRolloverCommand,
    PrepareExecutionPageSendUnknownReconciliationCommand,
    PrepareInitialExecutionPageCommand,
    PrepareExecutionCardFlushCommand,
    PreparePatchFailureFollowupCommand,
    RollbackExecutionPatchTimerCommand,
    ScheduleExecutionCardCommand,
)
from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionPageSendOutcome,
    ExecutionPageStatus,
    ExecutionTranscriptCursor,
)
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.runtime_admin_test_support import make_binding_runtime
from tests.execution_page_test_support import set_execution_page_state


class _FakeTimer:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class ExecutionOutputRuntimeTransitionTests(unittest.TestCase):
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
        runtime = ExecutionOutputRuntimeTransitions(
            lock=lock,
            binding_runtime=manager,
            turn_execution=TurnExecutionCoordinator(),
        )
        return lock, manager, runtime

    @staticmethod
    def _seed_execution(manager, lock, binding, *, last_patch_at=0.0):
        with lock:
            state = manager._get_or_create_runtime_state_locked(binding)
            state["current_thread_id"] = "thread-1"
            state["current_turn_id"] = "turn-1"
            set_execution_page_state(state, current_message_id="card-1")
            state["running"] = True
            state["started_at"] = 1.0
            state["last_patch_at"] = last_patch_at
            session = manager.resident_session_snapshot_locked(binding)
        assert session is not None
        return state, session

    @staticmethod
    def _seed_page_opening(manager, lock, binding):
        with lock:
            state = manager._get_or_create_runtime_state_locked(binding)
            state["current_thread_id"] = "thread-1"
            state["running"] = True
            state["awaiting_local_turn_started"] = True
            state["started_at"] = 1.0
            session = manager.resident_session_snapshot_locked(binding)
        assert session is not None
        return state, session

    def test_initial_page_attempt_is_recorded_before_effect_and_committed_once(
        self,
    ) -> None:
        lock, manager, runtime = self._make_runtime()
        state, captured = self._seed_page_opening(
            manager,
            lock,
            ("ou-user", "chat-1"),
        )

        prepared = runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="stable-attempt-1",
            )
        )

        assert prepared is not None
        opening = state["execution_pages"].current_page
        assert opening is not None
        self.assertIs(opening, prepared.receipt.page)
        self.assertEqual(opening.status, ExecutionPageStatus.OPENING)
        self.assertEqual(opening.outbound_attempt_id, "stable-attempt-1")
        committed = runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.CONFIRMED,
                message_id="card-1",
            )
        )

        assert committed is not None
        self.assertEqual(committed.session.execution.current_message_id, "card-1")
        self.assertEqual(
            committed.session.execution.pages.active_page.status,
            ExecutionPageStatus.ACTIVE,
        )
        self.assertIsNone(
            runtime.commit_initial_page(
                CommitInitialExecutionPageCommand(
                    receipt=prepared.receipt,
                    outcome=ExecutionPageSendOutcome.CONFIRMED,
                    message_id="card-1",
                )
            )
        )

    def test_initial_page_unknown_is_a_runtime_fence_not_a_rejection(self) -> None:
        lock, manager, runtime = self._make_runtime()
        _state, captured = self._seed_page_opening(
            manager,
            lock,
            ("ou-user", "chat-1"),
        )
        prepared = runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="stable-attempt-unknown",
            )
        )
        assert prepared is not None

        committed = runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.UNKNOWN,
            )
        )

        assert committed is not None
        page = committed.session.execution.pages.current_page
        assert page is not None
        self.assertEqual(page.status, ExecutionPageStatus.SEND_UNKNOWN)
        self.assertTrue(committed.session.execution.has_execution_anchor)

    def test_initial_page_commit_rejects_binding_aba(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state, captured = self._seed_page_opening(manager, lock, binding)
        prepared = runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="stable-attempt-stale",
            )
        )
        assert prepared is not None
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        manager.resolve_session(*binding)

        committed = runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.CONFIRMED,
                message_id="orphan-card",
            )
        )

        self.assertIsNone(committed)
        self.assertEqual(
            manager.resolve_session(*binding).execution.current_message_id,
            "",
        )

    def test_initial_page_commit_rejects_same_handle_ledger_aba(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_page_opening(manager, lock, binding)
        prepared = runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="stable-attempt-ledger-aba",
            )
        )
        assert prepared is not None
        with lock:
            state["execution_pages"] = ExecutionPageLedger(
                prepared.receipt.ledger.pages
            )

        committed = runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.CONFIRMED,
                message_id="orphan-card",
            )
        )

        self.assertIsNone(committed)
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(
            state["execution_pages"].current_page.status,
            ExecutionPageStatus.OPENING,
        )

    def test_rollover_prepare_fences_a_second_prepare_and_commits_exact_pages(
        self,
    ) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        state["execution_transcript"].append_process_note("0123456789")
        captured = manager.resolve_session(*binding)

        prepared = runtime.prepare_rollover(
            PrepareExecutionPageRolloverCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="rollover-attempt-1",
                cursor_start=ExecutionTranscriptCursor(process_chars=6),
            )
        )

        assert prepared is not None
        self.assertIs(
            state["execution_pages"],
            prepared.receipt.ledger,
        )
        self.assertIsNone(
            runtime.prepare_rollover(
                PrepareExecutionPageRolloverCommand(
                    target=ExecutionOutputTarget.from_session(
                        prepared.session
                    ),
                    outbound_attempt_id="rollover-attempt-2",
                    cursor_start=ExecutionTranscriptCursor(process_chars=7),
                )
            )
        )
        committed = runtime.commit_rollover(
            CommitExecutionPageRolloverCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.CONFIRMED,
                message_id="card-2",
            )
        )

        assert committed is not None
        pages = committed.session.execution.pages.pages
        self.assertEqual(
            tuple(page.status for page in pages),
            (ExecutionPageStatus.SEALED, ExecutionPageStatus.ACTIVE),
        )
        self.assertEqual(pages[0].cursor_end, pages[1].cursor_start)
        self.assertEqual(pages[1].message_id, "card-2")

    def test_rollover_unknown_keeps_active_page_and_pending_attempt(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(manager, lock, binding)
        state["execution_transcript"].append_assistant_delta("0123456789")
        captured = manager.resolve_session(*binding)
        prepared = runtime.prepare_rollover(
            PrepareExecutionPageRolloverCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="rollover-attempt-unknown",
                cursor_start=ExecutionTranscriptCursor(reply_chars=5),
            )
        )
        assert prepared is not None

        committed = runtime.commit_rollover(
            CommitExecutionPageRolloverCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.UNKNOWN,
            )
        )

        assert committed is not None
        ledger = committed.session.execution.pages
        self.assertEqual(ledger.current_message_id, "card-1")
        self.assertIs(
            ledger.pending_page.status,
            ExecutionPageStatus.SEND_UNKNOWN,
        )
        self.assertEqual(
            ledger.active_projection_end(
                committed.session.execution.transcript
            ),
            ExecutionTranscriptCursor(reply_chars=5),
        )

    def test_initial_send_unknown_reconciliation_outcomes_are_exact_and_replay_safe(
        self,
    ) -> None:
        for outcome in ExecutionPageSendOutcome:
            with self.subTest(outcome=outcome):
                lock, manager, runtime = self._make_runtime()
                _state, captured = self._seed_page_opening(
                    manager,
                    lock,
                    ("ou-user", "chat-1"),
                )
                prepared = runtime.prepare_initial_page(
                    PrepareInitialExecutionPageCommand(
                        target=ExecutionOutputTarget.from_session(captured),
                        outbound_attempt_id="stable-initial-unknown",
                    )
                )
                assert prepared is not None
                unknown = runtime.commit_initial_page(
                    CommitInitialExecutionPageCommand(
                        receipt=prepared.receipt,
                        outcome=ExecutionPageSendOutcome.UNKNOWN,
                    )
                )
                assert unknown is not None
                reconciliation = runtime.prepare_send_unknown_reconciliation(
                    PrepareExecutionPageSendUnknownReconciliationCommand(
                        target=ExecutionOutputTarget.from_session(unknown.session),
                    )
                )
                assert reconciliation is not None

                committed = runtime.commit_send_unknown_reconciliation(
                    CommitExecutionPageSendUnknownReconciliationCommand(
                        receipt=reconciliation.receipt,
                        outcome=outcome,
                        message_id="card-1"
                        if outcome is ExecutionPageSendOutcome.CONFIRMED
                        else "",
                    )
                )

                assert committed is not None
                ledger = committed.session.execution.pages
                if outcome is ExecutionPageSendOutcome.CONFIRMED:
                    self.assertEqual(ledger.current_message_id, "card-1")
                    self.assertIs(
                        ledger.active_page.status,
                        ExecutionPageStatus.ACTIVE,
                    )
                elif outcome is ExecutionPageSendOutcome.REJECTED:
                    self.assertEqual(ledger.pages, ())
                else:
                    assert ledger.pending_page is not None
                    self.assertEqual(
                        ledger.pending_page.outbound_attempt_id,
                        "stable-initial-unknown",
                    )
                    self.assertIs(
                        ledger.pending_page.status,
                        ExecutionPageStatus.SEND_UNKNOWN,
                    )
                    self.assertTrue(
                        ledger.pending_page.reconciliation_attempted
                    )
                self.assertIsNone(
                    runtime.commit_send_unknown_reconciliation(
                        CommitExecutionPageSendUnknownReconciliationCommand(
                            receipt=reconciliation.receipt,
                            outcome=outcome,
                            message_id="card-1"
                            if outcome is ExecutionPageSendOutcome.CONFIRMED
                            else "",
                        )
                    )
                )

    def test_rollover_send_unknown_reconciliation_preserves_page_lineage(
        self,
    ) -> None:
        for outcome in ExecutionPageSendOutcome:
            with self.subTest(outcome=outcome):
                lock, manager, runtime = self._make_runtime()
                binding = ("ou-user", "chat-1")
                state, captured = self._seed_execution(manager, lock, binding)
                state["execution_transcript"].append_process_note("0123456789")
                captured = manager.resolve_session(*binding)
                prepared = runtime.prepare_rollover(
                    PrepareExecutionPageRolloverCommand(
                        target=ExecutionOutputTarget.from_session(captured),
                        outbound_attempt_id="stable-rollover-unknown",
                        cursor_start=ExecutionTranscriptCursor(process_chars=5),
                    )
                )
                assert prepared is not None
                unknown = runtime.commit_rollover(
                    CommitExecutionPageRolloverCommand(
                        receipt=prepared.receipt,
                        outcome=ExecutionPageSendOutcome.UNKNOWN,
                    )
                )
                assert unknown is not None
                reconciliation = runtime.prepare_send_unknown_reconciliation(
                    PrepareExecutionPageSendUnknownReconciliationCommand(
                        target=ExecutionOutputTarget.from_session(unknown.session),
                    )
                )
                assert reconciliation is not None

                committed = runtime.commit_send_unknown_reconciliation(
                    CommitExecutionPageSendUnknownReconciliationCommand(
                        receipt=reconciliation.receipt,
                        outcome=outcome,
                        message_id="card-2"
                        if outcome is ExecutionPageSendOutcome.CONFIRMED
                        else "",
                    )
                )

                assert committed is not None
                ledger = committed.session.execution.pages
                if outcome is ExecutionPageSendOutcome.CONFIRMED:
                    self.assertEqual(
                        tuple(page.status for page in ledger.pages),
                        (ExecutionPageStatus.SEALED, ExecutionPageStatus.ACTIVE),
                    )
                    self.assertEqual(ledger.current_message_id, "card-2")
                    self.assertEqual(
                        ledger.pages[0].cursor_end,
                        ledger.pages[1].cursor_start,
                    )
                elif outcome is ExecutionPageSendOutcome.REJECTED:
                    self.assertEqual(len(ledger.pages), 1)
                    self.assertEqual(ledger.current_message_id, "card-1")
                else:
                    self.assertEqual(len(ledger.pages), 2)
                    assert ledger.pending_page is not None
                    self.assertEqual(
                        ledger.pending_page.outbound_attempt_id,
                        "stable-rollover-unknown",
                    )
                    self.assertTrue(
                        ledger.pending_page.reconciliation_attempted
                    )
                self.assertIsNone(
                    runtime.commit_send_unknown_reconciliation(
                        CommitExecutionPageSendUnknownReconciliationCommand(
                            receipt=reconciliation.receipt,
                            outcome=outcome,
                            message_id="card-2"
                            if outcome is ExecutionPageSendOutcome.CONFIRMED
                            else "",
                        )
                    )
                )

    def test_send_unknown_reconciliation_rejects_same_handle_ledger_aba(
        self,
    ) -> None:
        lock, manager, runtime = self._make_runtime()
        state, captured = self._seed_page_opening(
            manager,
            lock,
            ("ou-user", "chat-1"),
        )
        prepared = runtime.prepare_initial_page(
            PrepareInitialExecutionPageCommand(
                target=ExecutionOutputTarget.from_session(captured),
                outbound_attempt_id="stable-reconcile-ledger-aba",
            )
        )
        assert prepared is not None
        unknown = runtime.commit_initial_page(
            CommitInitialExecutionPageCommand(
                receipt=prepared.receipt,
                outcome=ExecutionPageSendOutcome.UNKNOWN,
            )
        )
        assert unknown is not None
        reconciliation = runtime.prepare_send_unknown_reconciliation(
            PrepareExecutionPageSendUnknownReconciliationCommand(
                target=ExecutionOutputTarget.from_session(unknown.session),
            )
        )
        assert reconciliation is not None
        with lock:
            state["execution_pages"] = ExecutionPageLedger(
                reconciliation.receipt.ledger.pages
            )

        committed = runtime.commit_send_unknown_reconciliation(
            CommitExecutionPageSendUnknownReconciliationCommand(
                receipt=reconciliation.receipt,
                outcome=ExecutionPageSendOutcome.CONFIRMED,
                message_id="orphan-card",
            )
        )

        self.assertIsNone(committed)
        assert state["execution_pages"].pending_page is not None
        self.assertIs(
            state["execution_pages"].pending_page.status,
            ExecutionPageStatus.SEND_UNKNOWN,
        )

    def test_delayed_schedule_install_and_consume_use_exact_ticket(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, captured = self._seed_execution(
            manager,
            lock,
            binding,
            last_patch_at=10.0,
        )

        prepared = runtime.prepare_schedule(
            ScheduleExecutionCardCommand(
                target=ExecutionOutputTarget.from_session(captured),
                occurred_at=11.0,
                interval_seconds=5.0,
            )
        )

        self.assertIsInstance(prepared, ExecutionPatchTimerInstallPreparation)
        assert isinstance(prepared, ExecutionPatchTimerInstallPreparation)
        self.assertEqual(prepared.delay_seconds, 4.0)
        timer = _FakeTimer()
        registration = ExecutionPatchTimerRegistration(
            ticket=prepared.ticket,
            timer=timer,
        )
        installed = runtime.install_patch_timer(
            InstallExecutionPatchTimerCommand(
                target=ExecutionOutputTarget.from_session(prepared.session),
                registration=registration,
            )
        )
        assert installed is not None
        self.assertIs(state["patch_timer_registration"], registration)

        effect = runtime.consume_patch_timer(
            ConsumeExecutionPatchTimerCommand(
                ticket=prepared.ticket,
                occurred_at=15.0,
            )
        )

        assert effect is not None
        self.assertEqual(effect.message_id, "card-1")
        self.assertTrue(effect.running)
        self.assertEqual(effect.elapsed, 14)
        self.assertIsNone(state["patch_timer_registration"])
        self.assertEqual(state["last_patch_at"], 15.0)
        self.assertIsNone(
            runtime.consume_patch_timer(
                ConsumeExecutionPatchTimerCommand(
                    ticket=prepared.ticket,
                    occurred_at=16.0,
                )
            )
        )

    def test_replacement_after_schedule_prepare_rejects_timer_install(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a, captured = self._seed_execution(
            manager,
            lock,
            binding,
            last_patch_at=10.0,
        )
        prepared = runtime.prepare_schedule(
            ScheduleExecutionCardCommand(
                target=ExecutionOutputTarget.from_session(captured),
                occurred_at=11.0,
                interval_seconds=5.0,
            )
        )
        assert isinstance(prepared, ExecutionPatchTimerInstallPreparation)

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, replacement = self._seed_execution(
            manager,
            lock,
            binding,
            last_patch_at=10.0,
        )
        registration = ExecutionPatchTimerRegistration(
            ticket=prepared.ticket,
            timer=_FakeTimer(),
        )

        installed = runtime.install_patch_timer(
            InstallExecutionPatchTimerCommand(
                target=ExecutionOutputTarget.from_session(prepared.session),
                registration=registration,
            )
        )

        self.assertIsNone(installed)
        self.assertIsNot(replacement.handle, prepared.session.handle)
        self.assertIsNone(state_b["patch_timer_registration"])

    def test_deactivate_recreate_a_b_a_rejects_old_flush_target(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        _state_a1, first_a = self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        self._seed_execution(manager, lock, binding)
        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_a2, second_a = self._seed_execution(manager, lock, binding)

        transition = runtime.prepare_flush(
            PrepareExecutionCardFlushCommand(
                target=ExecutionOutputTarget.from_session(first_a),
                occurred_at=20.0,
            )
        )

        self.assertIsNone(transition)
        self.assertGreater(second_a.handle.incarnation, first_a.handle.incarnation)
        self.assertEqual(state_a2["last_patch_at"], 0.0)

    def test_flush_cancels_timer_and_replacement_blocks_failure_followup(
        self,
    ) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state_a, _captured = self._seed_execution(manager, lock, binding)
        state_a["current_prompt_message_id"] = "prompt-1"
        state_a["execution_transcript"].set_reply_text("reply-a")
        timer = _FakeTimer()
        ticket = ExecutionPatchTimerTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        state_a["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ticket,
            timer=timer,
        )
        with lock:
            captured = manager.resident_session_snapshot_locked(binding)
        assert captured is not None
        transition = runtime.prepare_flush(
            PrepareExecutionCardFlushCommand(
                target=ExecutionOutputTarget.from_session(captured),
                occurred_at=20.0,
            )
        )
        assert transition is not None
        assert transition.effect is not None
        self.assertFalse(timer.cancelled)
        cancel_runtime_timer_effects(transition.timer_cancellations)
        self.assertTrue(timer.cancelled)
        self.assertEqual(transition.effect.reply_text, "reply-a")

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, _replacement = self._seed_execution(manager, lock, binding)
        state_b["execution_transcript"].set_reply_text("reply-b")

        followup = runtime.prepare_patch_failure_followup(
            PreparePatchFailureFollowupCommand(
                target=ExecutionOutputTarget.from_session(transition.session),
            )
        )

        self.assertIsNone(followup)
        self.assertFalse(state_b["followup_sent"])
        self.assertEqual(state_b["terminal_result_text"], "")

    def test_plan_drift_after_publish_blocks_message_id_commit(self) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state, _captured = self._seed_execution(manager, lock, binding)
        state["plan_turn_id"] = "turn-1"
        state["plan_steps"] = [{"step": "first", "status": "in_progress"}]
        with lock:
            captured = manager.resident_session_snapshot_locked(binding)
        assert captured is not None
        effect = runtime.capture_plan_card(
            CaptureExecutionPlanCardCommand(
                target=ExecutionOutputTarget.from_session(
                    captured,
                    include_plan=True,
                )
            )
        )
        assert effect is not None
        state["plan_steps"] = [{"step": "new", "status": "in_progress"}]

        committed = runtime.commit_plan_card(
            CommitExecutionPlanCardCommand(
                target=ExecutionOutputTarget.from_session(
                    effect.session,
                    include_plan=True,
                ),
                message_id="plan-card",
            )
        )

        self.assertIsNone(committed)
        self.assertEqual(state["plan_message_id"], "")

    def test_start_failure_rollback_cannot_clear_replacement_registration(
        self,
    ) -> None:
        lock, manager, runtime = self._make_runtime()
        binding = ("ou-user", "chat-1")
        state_a, captured = self._seed_execution(manager, lock, binding)
        timer_a = _FakeTimer()
        ticket_a = ExecutionPatchTimerTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        registration_a = ExecutionPatchTimerRegistration(
            ticket=ticket_a,
            timer=timer_a,
        )
        installed = runtime.install_patch_timer(
            InstallExecutionPatchTimerCommand(
                target=ExecutionOutputTarget.from_session(captured),
                registration=registration_a,
            )
        )
        assert installed is not None
        self.assertIs(state_a["patch_timer_registration"], registration_a)

        with lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
        state_b, _replacement = self._seed_execution(manager, lock, binding)
        timer_b = _FakeTimer()
        ticket_b = ExecutionPatchTimerTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        registration_b = ExecutionPatchTimerRegistration(
            ticket=ticket_b,
            timer=timer_b,
        )
        state_b["patch_timer_registration"] = registration_b

        rolled_back = runtime.rollback_patch_timer_start(
            RollbackExecutionPatchTimerCommand(
                handle=installed.handle,
                registration=registration_a,
            )
        )

        self.assertFalse(rolled_back)
        self.assertIs(state_b["patch_timer_registration"], registration_b)


if __name__ == "__main__":
    unittest.main()
