import pathlib
import tempfile
import threading
import time
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager, ResolvedRuntimeBinding
from bot.execution_recovery_controller import ExecutionRecoveryController, TerminalReconcileTarget
from bot.runtime_state import apply_runtime_state_message
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_lease_registry import ThreadLeaseRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class _TransportDisconnect(RuntimeError):
    pass


class _ThreadNotFound(RuntimeError):
    pass


class ExecutionRecoveryControllerTests(unittest.TestCase):
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

    def _make_controller(self, state):
        lock = threading.RLock()
        binding = ("ou_user", "c1")
        turn_execution = TurnExecutionCoordinator()
        patches: list[dict[str, object]] = []
        finalized: list[tuple[str, str]] = []
        terminal_results: list[dict[str, object]] = []
        snapshots: list[ThreadSnapshot | Exception] = []

        def _read_thread(thread_id: str) -> ThreadSnapshot:
            del thread_id
            current = snapshots.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

        controller = ExecutionRecoveryController(
            lock=lock,
            runtime_submit=lambda target, *args, **kwargs: target(*args, **kwargs),
            turn_execution=turn_execution,
            get_runtime_state=lambda sender_id, chat_id: state,
            resolve_runtime_binding=lambda sender_id, chat_id: ResolvedRuntimeBinding(binding=binding, state=state),
            apply_runtime_state_message_locked=apply_runtime_state_message,
            apply_persisted_runtime_state_message_locked=lambda binding_key, current_state, message: apply_runtime_state_message(
                current_state,
                message,
            ),
            finalize_execution_card_from_state=lambda sender_id, chat_id: finalized.append((sender_id, chat_id)) or True,
            patch_execution_card_message=lambda message_id, *, transcript, running, elapsed, cancelled: patches.append(
                {
                    "message_id": message_id,
                    "reply_text": transcript.reply_text(),
                    "running": running,
                    "elapsed": elapsed,
                    "cancelled": cancelled,
                }
            )
            or True,
            publish_terminal_result=lambda chat_id, *, final_reply_text, prompt_message_id="", prompt_reply_in_thread=False: terminal_results.append(
                {
                    "chat_id": chat_id,
                    "final_reply_text": final_reply_text,
                    "prompt_message_id": prompt_message_id,
                    "prompt_reply_in_thread": prompt_reply_in_thread,
                }
            )
            or True,
            read_thread=_read_thread,
            is_thread_not_found_error=lambda exc: isinstance(exc, _ThreadNotFound),
            is_turn_thread_not_found_error=lambda exc: False,
            is_transport_disconnect=lambda exc: isinstance(exc, _TransportDisconnect),
            is_request_timeout_error=lambda exc: isinstance(exc, TimeoutError)
            and str(exc).startswith("Codex request timed out:"),
            runtime_recovery_reason=str,
            mirror_watchdog_seconds=lambda: 60.0,
        )
        return controller, snapshots, patches, finalized, terminal_results

    def test_capture_terminal_reconcile_target_preserves_execution_anchor(self) -> None:
        state = self._make_state()
        controller, _, _, _, _ = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "msg-1"
        state["cancelled"] = True
        state["started_at"] = time.monotonic() - 3
        state["execution_transcript"].set_reply_text("reply")

        target = controller.capture_terminal_reconcile_target(
            "ou_user",
            "c1",
            thread_id="thread-1",
        )

        assert target is not None
        self.assertEqual(
            target,
            TerminalReconcileTarget(
                sender_id="ou_user",
                chat_id="c1",
                thread_id="thread-1",
                turn_id="turn-1",
                card_message_id="card-1",
                prompt_message_id="msg-1",
                prompt_reply_in_thread=False,
                transcript=state["execution_transcript"],
                cancelled=True,
                elapsed=target.elapsed,
            ),
        )
        self.assertGreaterEqual(target.elapsed, 2)

    def test_reconcile_execution_snapshot_updates_runtime_state_from_active_snapshot(self) -> None:
        state = self._make_state()
        controller, snapshots, _, finalized, terminal_results = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "old"
        state["working_dir"] = "/tmp/old"
        state["current_message_id"] = "card-1"

        snapshots.append(
            ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/new",
                    name="new-title",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="cli",
                    status="active",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [{"type": "agentMessage", "text": "snapshot reply"}],
                    }
                ],
            )
        )

        finalized_now = controller.reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertFalse(finalized_now)
        self.assertEqual(finalized, [])
        self.assertEqual(state["current_thread_title"], "new-title")
        self.assertEqual(state["working_dir"], "/tmp/new")
        self.assertEqual(state["execution_transcript"].reply_text(), "snapshot reply")
        self.assertGreater(state["last_runtime_event_at"], 0.0)
        self.assertEqual(state["runtime_channel_state"], "live")
        self.assertEqual(terminal_results, [])

    def test_reconcile_execution_snapshot_timeout_marks_runtime_degraded(self) -> None:
        state = self._make_state()
        controller, snapshots, _, finalized, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "card-1"

        snapshots.append(TimeoutError("Codex request timed out: read_thread"))

        finalized_now = controller.reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertFalse(finalized_now)
        self.assertEqual(finalized, [])
        self.assertEqual(state["runtime_channel_state"], "degraded")

    def test_reconcile_execution_snapshot_not_found_finalizes(self) -> None:
        state = self._make_state()
        controller, snapshots, _, finalized, terminal_results = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "card-1"
        state["current_prompt_message_id"] = "msg-1"
        state["execution_transcript"].set_reply_text("fallback reply")

        snapshots.append(_ThreadNotFound("thread not found"))

        finalized_now = controller.reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "fallback reply",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": False,
                }
            ],
        )

    def test_note_runtime_event_arms_watchdog_for_running_thread(self) -> None:
        state = self._make_state()
        controller, _, _, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"

        controller.note_runtime_event("ou_user", "c1")

        self.assertGreater(state["last_runtime_event_at"], 0.0)
        self.assertEqual(state["mirror_watchdog_generation"], 1)
        self.assertIsNotNone(state["mirror_watchdog_timer"])
        controller.cancel_mirror_watchdog_locked(state)
        self.assertIsNone(state["mirror_watchdog_timer"])

    def test_run_terminal_execution_reconcile_does_not_reinsert_single_final_reply_into_execution_card(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, terminal_results = self._make_controller(state)
        snapshots.append(
            ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="cli",
                    status="completed",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [{"type": "agentMessage", "text": "updated reply"}],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            TerminalReconcileTarget(
                sender_id="ou_user",
                chat_id="c1",
                thread_id="thread-1",
                turn_id="turn-1",
                card_message_id="card-1",
                prompt_message_id="msg-1",
                prompt_reply_in_thread=True,
                transcript=state["execution_transcript"],
                cancelled=False,
                elapsed=5,
            )
        )

        self.assertEqual(patches, [])
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "updated reply",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )

    def test_run_terminal_execution_reconcile_strips_terminal_final_reply_after_publish(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, terminal_results = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
        state["execution_transcript"].set_reply_text("阶段总结\n\n最终答案")
        snapshots.append(
            ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="cli",
                    status="completed",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [
                            {"type": "agentMessage", "text": "阶段总结"},
                            {"type": "commandExecution"},
                            {"type": "agentMessage", "text": "最终答案"},
                        ],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            TerminalReconcileTarget(
                sender_id="ou_user",
                chat_id="c1",
                thread_id="thread-1",
                turn_id="turn-1",
                card_message_id="card-1",
                prompt_message_id="msg-1",
                prompt_reply_in_thread=True,
                transcript=state["execution_transcript"].clone(),
                cancelled=False,
                elapsed=5,
            )
        )

        self.assertEqual(
            patches,
            [
                {
                    "message_id": "card-1",
                    "reply_text": "阶段总结",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(state["execution_transcript"].reply_text(), "阶段总结")
        self.assertEqual(state["terminal_result_text"], "最终答案")
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "最终答案",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )

    def test_run_terminal_execution_reconcile_sends_fallback_transcript_when_snapshot_unavailable(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, terminal_results = self._make_controller(state)
        state["execution_transcript"].set_reply_text("fallback answer")
        snapshots.append(_ThreadNotFound("thread not found"))

        controller.run_terminal_execution_reconcile(
            TerminalReconcileTarget(
                sender_id="ou_user",
                chat_id="c1",
                thread_id="thread-1",
                turn_id="turn-1",
                card_message_id="card-1",
                prompt_message_id="msg-9",
                prompt_reply_in_thread=False,
                transcript=state["execution_transcript"],
                cancelled=False,
                elapsed=5,
            )
        )

        self.assertEqual(patches, [])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "fallback answer",
                    "prompt_message_id": "msg-9",
                    "prompt_reply_in_thread": False,
                }
            ],
        )

    def test_run_terminal_execution_reconcile_keeps_final_reply_on_execution_card_when_result_publish_fails(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, terminal_results = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
        controller._publish_terminal_result = lambda *args, **kwargs: False
        snapshots.append(
            ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="cli",
                    status="completed",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [{"type": "agentMessage", "text": "最终答案"}],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            TerminalReconcileTarget(
                sender_id="ou_user",
                chat_id="c1",
                thread_id="thread-1",
                turn_id="turn-1",
                card_message_id="card-1",
                prompt_message_id="msg-9",
                prompt_reply_in_thread=False,
                transcript=state["execution_transcript"].clone(),
                cancelled=False,
                elapsed=5,
            )
        )

        self.assertEqual(
            patches,
            [
                {
                    "message_id": "card-1",
                    "reply_text": "最终答案",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(terminal_results, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "最终答案")
        self.assertEqual(state["terminal_result_text"], "")
