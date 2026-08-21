import pathlib
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_runtime_contract import BindingRuntimeHandle
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.execution_recovery_controller import ExecutionRecoveryController
from bot.execution_recovery_runtime import (
    ExecutionRecoveryRuntimeTransitions,
    TerminalReconcileTarget,
)
from bot.execution_recovery_worker import ExecutionRecoveryShutdownTimeoutError
from bot.feishu_execution_finalization_controller import FeishuExecutionFinalizationResult
from bot.generated_image_delivery import collect_generated_images
from bot.runtime_state import ThreadStateChanged, apply_runtime_state_message
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state


class _TransportDisconnect(RuntimeError):
    pass


class _ThreadNotFound(RuntimeError):
    pass


class _TestBindingRuntime:
    def __init__(self, states) -> None:
        self.states = states
        self._next_incarnation = 0
        self._current = {}

    def _current_handle(self, binding):
        state = self.states.get(binding)
        if state is None:
            return None
        current = self._current.get(binding)
        if current is not None and current[0] is state:
            return current[1]
        self._next_incarnation += 1
        handle = BindingRuntimeHandle(
            _issuer_nonce=1,
            binding=binding,
            incarnation=self._next_incarnation,
        )
        self._current[binding] = (state, handle)
        return handle

    def resolve_session(self, binding):
        state = self.states[binding]
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def session_snapshot_locked(self, handle):
        current = self._current_handle(handle.binding)
        if current is not handle:
            raise RuntimeError("stale test binding handle")
        return project_binding_session_snapshot(
            self.states[handle.binding],
            handle=handle,
        )

    def resident_session_snapshot_locked(self, binding):
        state = self.states.get(binding)
        if state is None:
            return None
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def resident_runtime_state_locked(self, binding):
        return self.states.get(binding)

    def update_thread_metadata_locked(
        self,
        handle,
        *,
        expected_thread_id,
        current_thread_title,
        working_dir,
    ):
        session = self.session_snapshot_locked(handle)
        if session.current_thread_id != expected_thread_id:
            return None
        state = self.states[handle.binding]
        apply_runtime_state_message(
            state,
            ThreadStateChanged(
                current_thread_title=current_thread_title,
                working_dir=working_dir,
            ),
        )
        return project_binding_session_snapshot(state, handle=handle)


class ExecutionRecoveryControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(
        self,
        state,
        *,
        compact_start_timeout_seconds: float = 60.0,
        compact_unknown: list[tuple[str, str, str]] | None = None,
        resident_states: dict[tuple[str, str], object] | None = None,
        binding_runtime=None,
        finalize_execution=None,
    ):
        lock = threading.RLock()
        binding = ("ou_user", "c1")
        if resident_states is None:
            resident_states = {binding: state}

        turn_execution = TurnExecutionCoordinator()
        binding_runtime = binding_runtime or _TestBindingRuntime(resident_states)
        patches: list[dict[str, object]] = []
        deletes: list[str] = []
        finalized: list[tuple[str, str]] = []
        terminal_results: list[dict[str, object]] = []
        delivered_images: list[dict[str, object]] = []
        snapshots: list[ThreadSnapshot | Exception] = []
        recorded_terminal_results: set[tuple[str, str]] = set()

        def _read_thread(thread_id: str, **_kwargs) -> ThreadSnapshot:
            del thread_id
            current = snapshots.pop(0)
            if isinstance(current, Exception):
                raise current
            return current

        def _deliver_generated_images_from_snapshot(**kwargs) -> int:
            snapshot = kwargs["snapshot"]
            turn_id = str(kwargs.get("turn_id", "") or "")
            if not collect_generated_images(snapshot, turn_id=turn_id):
                return 0
            delivered_images.append(dict(kwargs))
            return 1

        finalize = finalize_execution or (
            lambda session: finalized.append(session.binding)
            or FeishuExecutionFinalizationResult(had_card=True, retired=True)
        )

        controller = ExecutionRecoveryController(
            runtime=ExecutionRecoveryRuntimeTransitions(
                lock=lock,
                binding_runtime=binding_runtime,
                turn_execution=turn_execution,
            ),
            runtime_call=lambda target, *args, **kwargs: target(*args, **kwargs),
            capture_connection_generation=lambda: 1,
            run_if_connection_generation=lambda _generation, callback: callback(),
            resolve_session=lambda sender_id, chat_id: binding_runtime.resolve_session(
                binding
            ),
            finalize_execution=finalize,
            prepare_execution_finalization=lambda session: session,
            present_execution_finalization=finalize,
            mark_compact_start_outcome_unknown=lambda session, thread_id: (
                compact_unknown.append((*session.binding, thread_id))
                if compact_unknown is not None
                else None
            ),
            dispatch_execution_card_message=lambda chat_id, message_id, *, transcript, running, elapsed, cancelled, cursor_start, cursor_end: patches.append(
                {
                    "message_id": message_id,
                    "reply_text": transcript.reply_text(),
                    "running": running,
                    "elapsed": elapsed,
                    "cancelled": cancelled,
                }
            )
            or True,
            publish_terminal_result=lambda chat_id, *, final_reply_text, source_execution_message_id="", prompt_message_id="", prompt_reply_in_thread=False, thread_id="": (
                terminal_results.append(
                    {
                        "chat_id": chat_id,
                        "final_reply_text": final_reply_text,
                        "source_execution_message_id": source_execution_message_id,
                        "prompt_message_id": prompt_message_id,
                        "prompt_reply_in_thread": prompt_reply_in_thread,
                    }
                ),
                recorded_terminal_results.add((str(source_execution_message_id or "").strip(), str(final_reply_text or ""))),
                True,
            )[-1],
            has_recorded_terminal_result=lambda *, execution_message_id, final_reply_text: (
                str(execution_message_id or "").strip(),
                str(final_reply_text or ""),
            ) in recorded_terminal_results,
            deliver_generated_images_from_snapshot=_deliver_generated_images_from_snapshot,
            read_thread=_read_thread,
            is_thread_not_found_error=lambda exc: isinstance(exc, _ThreadNotFound),
            is_turn_thread_not_found_error=lambda exc: False,
            is_pre_send_error=lambda exc: False,
            is_transport_disconnect=lambda exc: isinstance(exc, _TransportDisconnect),
            is_request_timeout_error=lambda exc: isinstance(exc, TimeoutError)
            and str(exc).startswith("Codex request timed out:"),
            runtime_recovery_reason=str,
            mirror_watchdog_seconds=lambda: 60.0,
            compact_start_timeout_seconds=lambda: compact_start_timeout_seconds,
            terminal_empty_retry_count=lambda: 3,
            terminal_empty_retry_delay_seconds=lambda: 0.0,
        )
        return controller, snapshots, patches, deletes, finalized, terminal_results, delivered_images

    def _terminal_target(
        self,
        controller: ExecutionRecoveryController,
        state,
        *,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        turn_id: str,
        card_message_id: str,
        prompt_message_id: str,
        prompt_reply_in_thread: bool,
        transcript,
        cancelled: bool,
        elapsed: int,
    ) -> TerminalReconcileTarget:
        self.assertEqual((sender_id, chat_id), ("ou_user", "c1"))
        state["current_thread_id"] = thread_id
        state["current_turn_id"] = turn_id
        set_execution_page_state(state, current_message_id=card_message_id)
        state["current_prompt_message_id"] = prompt_message_id
        state["current_prompt_reply_in_thread"] = prompt_reply_in_thread
        state["execution_transcript"] = transcript.clone()
        state["cancelled"] = cancelled
        target = controller.capture_terminal_reconcile_target(
            sender_id,
            chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )
        assert target is not None
        return replace(
            target,
            transcript=transcript.snapshot(),
            cancelled=cancelled,
            elapsed=elapsed,
        )

    def test_capture_terminal_reconcile_target_preserves_execution_anchor(self) -> None:
        state = self._make_state()
        controller, _, _, _, _, _, _ = self._make_controller(state)
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
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
        self.assertEqual(target.binding, ("ou_user", "c1"))
        self.assertEqual(target.thread_id, "thread-1")
        self.assertEqual(target.turn_id, "turn-1")
        self.assertEqual(target.card_message_id, "card-1")
        self.assertEqual(target.prompt_message_id, "msg-1")
        self.assertFalse(target.prompt_reply_in_thread)
        self.assertTrue(target.cancelled)
        self.assertEqual(target.cursor_start.reply_chars, 0)
        self.assertEqual(target.cursor_end.reply_chars, len("reply"))
        self.assertIsNot(target.transcript, state["execution_transcript"])
        state["execution_transcript"].set_reply_text("mutated after capture")
        self.assertEqual(target.transcript.reply_text(), "reply")
        self.assertGreaterEqual(target.elapsed, 2)

    def test_exact_session_recovery_effects_reject_replacement_without_side_effects(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["current_thread_id"] = "thread-1"
        state_a["current_turn_id"] = "turn-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_a["running"] = True
        state_a["started_at"] = 1.0
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        captured = binding_runtime.resolve_session(binding)

        state_b = self._make_state()
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        state_b["running"] = True
        state_b["started_at"] = 1.0
        resident_states[binding] = state_b

        controller, *_ = self._make_controller(
            state_a,
            resident_states=resident_states,
            binding_runtime=binding_runtime,
        )

        controller.schedule_mirror_watchdog_for_session(captured)
        target = controller.capture_terminal_reconcile_target_for_session(
            captured,
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertIsNone(target)
        self.assertIsNone(state_b["mirror_watchdog_registration"])

    def test_terminal_reconcile_continues_after_replacement_without_runtime_mutation(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["execution_transcript"].reconcile_current_assistant_text("old reply")
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        controller, snapshots, patches, _, finalized, terminal_results, images = (
            self._make_controller(
                state_a,
                resident_states=resident_states,
                binding_runtime=binding_runtime,
            )
        )
        target = self._terminal_target(
            controller,
            state_a,
            sender_id="ou_user",
            chat_id="c1",
            thread_id="thread-1",
            turn_id="turn-1",
            card_message_id="card-1",
            prompt_message_id="prompt-1",
            prompt_reply_in_thread=False,
            transcript=state_a["execution_transcript"],
            cancelled=False,
            elapsed=1,
        )
        state_b = self._make_state()
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-2"
        set_execution_page_state(state_b, current_message_id="card-2")
        state_b["execution_transcript"].set_reply_text("replacement")
        resident_states[binding] = state_b

        controller.run_terminal_execution_reconcile(target)

        self.assertEqual(snapshots, [])
        self.assertEqual(
            patches,
            [
                {
                    "message_id": "card-1",
                    "reply_text": "",
                    "running": False,
                    "elapsed": 1,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(finalized, [])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "old reply",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "prompt-1",
                    "prompt_reply_in_thread": False,
                }
            ],
        )
        self.assertEqual(images, [])
        self.assertEqual(state_b["execution_transcript"].reply_text(), "replacement")
        self.assertEqual(state_b["execution_pages"].current_message_id, "card-2")
        self.assertEqual(state_b["current_turn_id"], "turn-2")
        self.assertEqual(state_b["terminal_result_text"], "")

    def test_shutdown_waits_for_terminal_reconcile_workers_and_rejects_new_work(self) -> None:
        state = self._make_state()
        controller, _, _, _, _, _, _ = self._make_controller(state)
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()

        def run(_target: TerminalReconcileTarget) -> None:
            started.set()
            release.wait()
            completed.set()

        controller.run_terminal_execution_reconcile = run  # type: ignore[method-assign]
        target = self._terminal_target(controller, state,
            sender_id="ou_user",
            chat_id="c1",
            thread_id="thread-1",
            turn_id="turn-1",
            card_message_id="card-1",
            prompt_message_id="msg-1",
            prompt_reply_in_thread=False,
            transcript=state["execution_transcript"],
            cancelled=False,
            elapsed=1,
        )
        controller.schedule_terminal_execution_reconcile(target)
        self.assertTrue(started.wait(timeout=1.0))

        shutdown_done = threading.Event()
        shutdown_worker = threading.Thread(
            target=lambda: (controller.shutdown(), shutdown_done.set()),
            daemon=True,
        )
        shutdown_worker.start()
        self.assertFalse(shutdown_done.wait(timeout=0.05))

        release.set()
        shutdown_worker.join(timeout=1.0)
        self.assertTrue(completed.is_set())
        self.assertTrue(shutdown_done.is_set())

        started.clear()
        controller.schedule_terminal_execution_reconcile(target)
        self.assertFalse(started.wait(timeout=0.05))

    def test_terminal_reconcile_does_not_publish_after_shutdown_during_read(self) -> None:
        state = self._make_state()
        controller, _, patches, deletes, finalized, terminal_results, delivered_images = self._make_controller(state)
        read_started = threading.Event()
        release_read = threading.Event()

        def blocking_read(_thread_id: str) -> ThreadSnapshot:
            read_started.set()
            release_read.wait(timeout=1.0)
            return ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="cli",
                    status="idle",
                ),
                turns=[{
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": "late result"}],
                }],
            )

        setattr(controller, "_read_thread", blocking_read)
        target = self._terminal_target(controller, state,
            sender_id="ou_user",
            chat_id="c1",
            thread_id="thread-1",
            turn_id="turn-1",
            card_message_id="card-1",
            prompt_message_id="msg-1",
            prompt_reply_in_thread=False,
            transcript=state["execution_transcript"],
            cancelled=False,
            elapsed=1,
        )
        controller.schedule_terminal_execution_reconcile(target)
        self.assertTrue(read_started.wait(timeout=1.0))

        with self.assertRaises(ExecutionRecoveryShutdownTimeoutError):
            controller.shutdown(timeout=0.0)
        release_read.set()
        controller.shutdown(timeout=1.0)

        self.assertEqual(patches, [])
        self.assertEqual(deletes, [])
        self.assertEqual(finalized, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_updates_runtime_state_from_active_snapshot(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "old"
        state["working_dir"] = "/tmp/old"
        set_execution_page_state(state, current_message_id="card-1")

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
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_drops_result_after_binding_retargets(self) -> None:
        state = self._make_state()
        controller, _, _, _, finalized, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        state["running"] = True
        state["current_thread_id"] = "thread-a"
        state["current_thread_title"] = "old A"
        state["working_dir"] = "/tmp/a"
        set_execution_page_state(state, current_message_id="card-a")
        stale_snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-a",
                cwd="/tmp/a-from-snapshot",
                name="A from snapshot",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="active",
            ),
            turns=[
                {
                    "id": "turn-a",
                    "items": [{"type": "agentMessage", "text": "stale A reply"}],
                }
            ],
        )

        def read_then_retarget(_thread_id: str) -> ThreadSnapshot:
            # Deterministically place the A -> B switch after thread/read starts
            # but before the recovery result attempts its local commit.
            state["current_thread_id"] = "thread-b"
            state["current_thread_title"] = "current B"
            state["working_dir"] = "/tmp/b"
            set_execution_page_state(state, current_message_id="card-b")
            state["execution_transcript"].set_reply_text("current B reply")
            return stale_snapshot

        controller._read_thread = read_then_retarget

        reconciled = controller.reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-a",
            turn_id="turn-a",
        )

        self.assertFalse(reconciled)
        self.assertEqual(state["current_thread_id"], "thread-b")
        self.assertEqual(state["current_thread_title"], "current B")
        self.assertEqual(state["working_dir"], "/tmp/b")
        self.assertEqual(state["execution_transcript"].reply_text(), "current B reply")
        self.assertEqual(finalized, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_does_not_follow_replacement_during_finalization(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["running"] = True
        state_a["current_thread_id"] = "thread-1"
        state_a["current_turn_id"] = "turn-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_a["current_prompt_message_id"] = "prompt-1"
        state_a["current_execution_kind"] = "prompt"
        state_a["started_at"] = 1.0
        state_b = self._make_state()
        state_b["running"] = True
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-2"
        set_execution_page_state(state_b, current_message_id="card-2")
        state_b["execution_transcript"].set_reply_text("replacement reply")
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        finalized_sessions = []

        def replace_during_finalization(session):
            finalized_sessions.append(session)
            resident_states[binding] = state_b
            return FeishuExecutionFinalizationResult(
                had_card=True,
                retired=False,
            )

        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(
                state_a,
                resident_states=resident_states,
                binding_runtime=binding_runtime,
                finalize_execution=replace_during_finalization,
            )
        )
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
                    status="idle",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [
                            {"type": "agentMessage", "text": "old final reply"}
                        ],
                    }
                ],
            )
        )

        reconciled = controller.reconcile_execution_snapshot(
            *binding,
            thread_id="thread-1",
            turn_id="turn-1",
        )

        self.assertFalse(reconciled)
        self.assertEqual(len(finalized_sessions), 1)
        self.assertEqual(patches, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])
        self.assertEqual(state_b["execution_pages"].current_message_id, "card-2")
        self.assertEqual(
            state_b["execution_transcript"].reply_text(),
            "replacement reply",
        )

    def test_reconcile_execution_snapshot_restores_interrupted_status(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_turn_id"] = "turn-1"
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
                    status="idle",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "status": "interrupted",
                        "items": [{"type": "agentMessage", "text": "partial reply"}],
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

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertTrue(state["cancelled"])
        self.assertFalse(state["pending_cancel"])

    def test_reconcile_execution_snapshot_timeout_marks_runtime_degraded(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")

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

    def test_reconcile_execution_snapshot_waits_for_unbound_turn_id(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="compact-card")
        state["awaiting_local_turn_started"] = True
        state["current_turn_id"] = ""
        snapshots.append(
            ThreadSnapshot(
                summary=ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=0,
                    updated_at=0,
                    source="appServer",
                    status="idle",
                ),
                turns=[
                    {
                        "id": "old-turn",
                        "items": [{"type": "agentMessage", "text": "old final"}],
                    }
                ],
            )
        )

        finalized_now = controller.reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-1",
            turn_id="",
        )

        self.assertFalse(finalized_now)
        self.assertEqual(finalized, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])
        self.assertEqual(state["execution_pages"].current_message_id, "compact-card")
        self.assertTrue(state["running"])
        self.assertEqual(len(snapshots), 1)

    def test_mirror_watchdog_marks_unbound_compact_unknown_without_finalizing(self) -> None:
        state = self._make_state()
        compact_unknown: list[tuple[str, str, str]] = []
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(
            state,
            compact_start_timeout_seconds=0.1,
            compact_unknown=compact_unknown,
        )
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="compact-card")
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "compact"
        state["current_turn_id"] = ""
        state["started_at"] = time.monotonic() - 1.0

        controller.schedule_mirror_watchdog("ou_user", "c1")
        registration = state["mirror_watchdog_registration"]
        self.assertIsNotNone(registration)
        assert registration is not None
        registration.timer.cancel()
        controller.run_mirror_watchdog(registration.ticket)

        self.assertEqual(compact_unknown, [("ou_user", "c1", "thread-1")])
        self.assertEqual(finalized, [])
        self.assertTrue(state["awaiting_local_turn_started"])
        self.assertEqual(state["execution_pages"].current_message_id, "compact-card")
        self.assertEqual(snapshots, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_watchdog_rejects_same_coordinate_replacement_and_replay(self) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        resident_states = {binding: state_a}
        controller, snapshots, _, _, _, _, _ = self._make_controller(
            state_a,
            resident_states=resident_states,
        )
        state_a["running"] = True
        state_a["current_thread_id"] = "thread-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_a["current_turn_id"] = ""
        state_a["awaiting_local_turn_started"] = True
        controller.schedule_mirror_watchdog(*binding)
        registration_a = state_a["mirror_watchdog_registration"]
        assert registration_a is not None
        registration_a.timer.cancel()

        state_b = self._make_state()
        state_b["running"] = True
        state_b["current_thread_id"] = "thread-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        state_b["current_turn_id"] = ""
        state_b["awaiting_local_turn_started"] = True
        resident_states[binding] = state_b
        controller.schedule_mirror_watchdog(*binding)
        registration_b = state_b["mirror_watchdog_registration"]
        assert registration_b is not None
        registration_b.timer.cancel()

        controller.run_mirror_watchdog(registration_a.ticket)

        self.assertIs(state_b["mirror_watchdog_registration"], registration_b)
        self.assertEqual(snapshots, [])

        controller.run_mirror_watchdog(registration_b.ticket)
        registration_c = state_b["mirror_watchdog_registration"]
        self.assertIsNotNone(registration_c)
        assert registration_c is not None
        registration_c.timer.cancel()
        controller.run_mirror_watchdog(registration_b.ticket)

        self.assertIs(state_b["mirror_watchdog_registration"], registration_c)
        self.assertEqual(snapshots, [])

    def test_watchdog_timer_construction_replacement_blocks_old_install(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["running"] = True
        state_a["current_thread_id"] = "thread-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_b = self._make_state()
        state_b["running"] = True
        state_b["current_thread_id"] = "thread-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        controller, snapshots, _, _, _, _, _ = self._make_controller(
            state_a,
            resident_states=resident_states,
            binding_runtime=binding_runtime,
        )
        timers = []

        class _ReplacementTimer:
            def __init__(self, *args, **kwargs) -> None:
                self.daemon = False
                self.cancelled = False
                self.started = False
                timers.append(self)
                resident_states[binding] = state_b

            def start(self) -> None:
                self.started = True

            def cancel(self) -> None:
                self.cancelled = True

        with patch(
            "bot.execution_recovery_controller.threading.Timer",
            _ReplacementTimer,
        ):
            controller.schedule_mirror_watchdog(*binding)

        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].cancelled)
        self.assertFalse(timers[0].started)
        self.assertIsNone(state_a["mirror_watchdog_registration"])
        self.assertIsNone(state_b["mirror_watchdog_registration"])
        self.assertEqual(snapshots, [])

    def test_watchdog_cancel_reschedule_rejects_old_ticket(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, _, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        controller.schedule_mirror_watchdog("ou_user", "c1")
        registration_a = state["mirror_watchdog_registration"]
        assert registration_a is not None
        registration_a.timer.cancel()

        controller.schedule_mirror_watchdog("ou_user", "c1")
        registration_b = state["mirror_watchdog_registration"]
        assert registration_b is not None
        registration_b.timer.cancel()

        controller.run_mirror_watchdog(registration_a.ticket)

        self.assertIs(state["mirror_watchdog_registration"], registration_b)
        self.assertEqual(snapshots, [])

    def test_watchdog_after_clear_is_side_effect_free(self) -> None:
        binding = ("ou_user", "c1")
        state = self._make_state()
        resident_states = {binding: state}
        controller, snapshots, _, _, _, _, _ = self._make_controller(
            state,
            resident_states=resident_states,
        )
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        controller.schedule_mirror_watchdog(*binding)
        registration = state["mirror_watchdog_registration"]
        assert registration is not None
        registration.timer.cancel()
        del resident_states[binding]

        controller.run_mirror_watchdog(registration.ticket)

        self.assertEqual(resident_states, {})
        self.assertEqual(snapshots, [])

    def test_watchdog_start_failure_clears_registration(self) -> None:
        state = self._make_state()
        controller, _, _, _, _, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")

        class _StartFailureTimer:
            cancelled = False

            def __init__(self, *args, **kwargs) -> None:
                self.daemon = False

            def start(self) -> None:
                raise RuntimeError("start failed")

            def cancel(self) -> None:
                type(self).cancelled = True

        with patch("bot.execution_recovery_controller.threading.Timer", _StartFailureTimer):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                controller.schedule_mirror_watchdog("ou_user", "c1")

        self.assertIsNone(state["mirror_watchdog_registration"])
        self.assertTrue(_StartFailureTimer.cancelled)

    def test_reconcile_execution_snapshot_not_found_finalizes(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"
        state["execution_transcript"].reconcile_current_assistant_text(
            "fallback reply"
        )

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
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": False,
                }
            ],
        )
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_keeps_minimal_execution_card_when_snapshot_only_has_final_reply(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
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
            self._terminal_target(controller, state,
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
        self.assertEqual(deletes, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "updated reply",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_retries_empty_snapshot_until_final_reply_appears(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        snapshots.extend(
            [
                ThreadSnapshot(
                    summary=ThreadSummary(
                        thread_id="thread-1",
                        cwd="/tmp/project",
                        name="demo",
                        preview="",
                        created_at=0,
                        updated_at=0,
                        source="cli",
                        status="active",
                    ),
                    turns=[
                        {
                            "id": "turn-1",
                            "status": "inProgress",
                            "items": [
                                {
                                    "id": "agent-1",
                                    "type": "agentMessage",
                                    "status": "inProgress",
                                    "text": "",
                                },
                                {
                                    "type": "imageGeneration",
                                    "id": "img-1",
                                    "status": "completed",
                                    "savedPath": "/tmp/generated.png",
                                },
                            ],
                        }
                    ],
                ),
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
                            "status": "completed",
                            "items": [
                                {
                                    "type": "agentMessage",
                                    "status": "completed",
                                    "text": "late final",
                                },
                                {
                                    "type": "imageGeneration",
                                    "id": "img-1",
                                    "status": "completed",
                                    "savedPath": "/tmp/generated.png",
                                },
                            ],
                        }
                    ],
                ),
            ]
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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
        self.assertEqual(deletes, [])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "late final",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )
        self.assertEqual(len(delivered_images), 1)

    def test_terminal_reconcile_does_not_guess_interval_from_flattened_reply(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        raw_final_reply = "  最终答案\n"
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].set_reply_text(f"阶段总结\n\n{raw_final_reply}")
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
                            {"type": "agentMessage", "text": raw_final_reply},
                        ],
                    }
                ],
            )
        )

        with self.assertLogs(
            "bot.execution_recovery_controller",
            level="INFO",
        ) as captured:
            controller.run_terminal_execution_reconcile(
                self._terminal_target(controller, state,
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

        self.assertEqual(patches, [])
        self.assertEqual(deletes, [])
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            f"阶段总结\n\n{raw_final_reply}",
        )
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": raw_final_reply,
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )
        self.assertEqual(delivered_images, [])
        self.assertTrue(
            any(
                "reason=snapshot_projection_mismatch" in line
                for line in captured.output
            )
        )

    def test_terminal_reconcile_uses_interrupted_snapshot_for_card_status(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, _, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
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
                    status="idle",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "status": "interrupted",
                        "items": [
                            {"type": "agentMessage", "text": "阶段总结"},
                            {"type": "agentMessage", "text": "最终答案"},
                        ],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0]["cancelled"])

    def test_terminal_reconcile_does_not_promote_commentary_before_empty_final(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        set_execution_page_state(state, current_message_id="card-1")
        transcript = state["execution_transcript"]
        transcript.reconcile_current_assistant_text("阶段说明")
        transcript.start_process_block("tool", marks_work=True)
        transcript.finish_process_block()
        transcript.reconcile_current_assistant_text("")
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "status": "interrupted",
                    "items": [
                        {"type": "agentMessage", "text": "阶段说明"},
                        {"type": "commandExecution"},
                        {"type": "agentMessage", "text": ""},
                    ],
                }
            ],
        )
        snapshots.append(snapshot)

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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
                    "reply_text": "阶段说明\n\n本轮未生成有效终态回复",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": True,
                }
            ],
        )
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])
        self.assertEqual(snapshots, [])

    def test_terminal_reconcile_removes_exact_local_final_and_marks_interrupted(
        self,
    ) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].append_process_note("过程内容")
        state["execution_transcript"].reconcile_current_assistant_text("本地回复")
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
                    status="idle",
                ),
                turns=[{"id": "turn-1", "status": "interrupted", "items": []}],
            )
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["reply_text"], "")
        self.assertTrue(patches[0]["cancelled"])
        self.assertEqual(len(terminal_results), 1)
        self.assertEqual(delivered_images, [])

    def test_terminal_reconcile_refreshes_interrupted_status_when_snapshot_text_is_unchanged(
        self,
    ) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].set_reply_text("相同回复")
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
                    status="idle",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "status": "interrupted",
                        "items": [{"type": "agentMessage", "text": "相同回复"}],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["reply_text"], "相同回复")
        self.assertTrue(patches[0]["cancelled"])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_snapshot_reply_does_not_borrow_text_from_unmatched_turn(self) -> None:
        snapshot = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
            turns=[
                {
                    "id": "other-turn",
                    "status": "interrupted",
                    "items": [{"type": "agentMessage", "text": "other reply"}],
                }
            ],
        )

        projection = ExecutionRecoveryController.snapshot_reply(snapshot, turn_id="missing-turn")

        self.assertEqual(projection.kind, "unavailable")
        self.assertEqual(projection.full_reply_text, "")
        self.assertEqual(projection.turn_status, "")

    def test_run_terminal_execution_reconcile_publishes_short_completed_fallback(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        coordinator = TurnExecutionCoordinator()
        commentary = "这是一段明显长于最终回复的阶段说明"
        coordinator.reconcile_current_assistant_text_locked(
            state,
            text=commentary,
        )
        coordinator.start_process_block_locked(state, text="tool", marks_work=True)
        coordinator.finish_process_block_locked(state)
        coordinator.reconcile_current_assistant_text_locked(state, text="完成")
        snapshots.append(_ThreadNotFound("thread not found"))

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["reply_text"], commentary)
        self.assertEqual(deletes, [])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "完成",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-9",
                    "prompt_reply_in_thread": False,
                }
            ],
        )
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_does_not_duplicate_text_fallback_when_already_recorded(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        state["execution_transcript"].reconcile_current_assistant_text(
            "fallback answer"
        )
        snapshots.append(_ThreadNotFound("thread not found"))
        controller._has_recorded_terminal_result = lambda *, execution_message_id, final_reply_text: (
            execution_message_id == "card-1" and final_reply_text == "fallback answer"
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(terminal_results, [])
        self.assertEqual(
            patches,
            [
                {
                    "message_id": "card-1",
                    "reply_text": "",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(deletes, [])
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_dedupes_terminal_text_by_raw_exact_value(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, _, terminal_results, _ = self._make_controller(state)
        state["execution_transcript"].reconcile_current_assistant_text(
            "fallback answer"
        )
        snapshots.append(_ThreadNotFound("thread not found"))

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        state["execution_transcript"].reconcile_current_assistant_text(
            "fallback answer\n"
        )
        snapshots.append(_ThreadNotFound("thread not found"))
        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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

        self.assertEqual(
            [item["final_reply_text"] for item in terminal_results],
            ["fallback answer", "fallback answer\n"],
        )

    def test_run_terminal_execution_reconcile_keeps_final_reply_on_execution_card_when_result_publish_fails(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
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
                        "items": [
                            {"type": "agentMessage", "text": "最终答案"},
                            {
                                "type": "imageGeneration",
                                "id": "img-1",
                                "status": "completed",
                                "savedPath": "/tmp/generated.png",
                            },
                        ],
                    }
                ],
            )
        )

        controller.run_terminal_execution_reconcile(
            self._terminal_target(controller, state,
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
        self.assertEqual(deletes, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_keeps_minimal_execution_card_when_only_final_result_remains(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].set_reply_text("最终答案")
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
            self._terminal_target(controller, state,
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
                    "reply_text": "",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(deletes, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "最终答案")
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "最终答案",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": True,
                }
            ],
        )
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_delivers_generated_images_after_terminal_text(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"

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
                            {"type": "agentMessage", "text": "最终答案"},
                            {
                                "type": "imageGeneration",
                                "id": "img-1",
                                "status": "completed",
                                "savedPath": "/tmp/generated.png",
                            },
                        ],
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

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "最终答案",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": False,
                }
            ],
        )
        self.assertEqual(len(delivered_images), 1)
        self.assertEqual(delivered_images[0]["thread_id"], "thread-1")
        self.assertEqual(delivered_images[0]["turn_id"], "turn-1")
        self.assertEqual(delivered_images[0]["prompt_message_id"], "msg-1")

    def test_reconcile_execution_snapshot_uses_turn_error_when_agent_reply_is_empty(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"

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
                    status="systemError",
                ),
                turns=[
                    {
                        "id": "turn-1",
                        "items": [{"type": "agentMessage", "text": ""}],
                        "status": "failed",
                        "error": {"message": "Missing environment variable: `CODEX_ZH_API_KEY`."},
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

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "Missing environment variable: `CODEX_ZH_API_KEY`.",
                    "source_execution_message_id": "card-1",
                    "prompt_message_id": "msg-1",
                    "prompt_reply_in_thread": False,
                }
            ],
        )
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_skips_generated_images_when_terminal_text_publish_fails(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"
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
                        "items": [
                            {"type": "agentMessage", "text": "最终答案"},
                            {
                                "type": "imageGeneration",
                                "id": "img-1",
                                "status": "completed",
                                "savedPath": "/tmp/generated.png",
                            },
                        ],
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

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_delivers_generated_images_without_terminal_text(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"

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
                            {
                                "type": "imageGeneration",
                                "id": "img-1",
                                "status": "completed",
                                "savedPath": "/tmp/generated.png",
                            },
                        ],
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

        self.assertTrue(finalized_now)
        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertEqual(terminal_results, [])
        self.assertEqual(len(delivered_images), 1)
