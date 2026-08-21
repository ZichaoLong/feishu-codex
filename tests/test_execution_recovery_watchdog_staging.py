import pathlib
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from typing import Any

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapter_notification_runtime import (
    AdapterNotificationRuntimeTransitions,
    AssistantDeltaNotificationCommand,
    ExecutionRuntimeEventCommand,
)
from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.execution_recovery_controller import ExecutionRecoveryController
from bot.execution_recovery_runtime import (
    ApplyExecutionSnapshotCommand,
    ExecutionRecoveryRuntimeTransitions,
    ExecutionSnapshotTransition,
    MirrorWatchdogInstallPreparation,
    PrepareMirrorWatchdogCommand,
    PrepareSnapshotReconcileCommand,
    SnapshotReconcilePreparation,
)
from bot.feishu_execution_finalization_controller import (
    FeishuExecutionFinalizationResult,
)
from bot.runtime_loop import RuntimeLoop
from bot.runtime_state import (
    RuntimeStateDict,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state


class _ThreadNotFound(RuntimeError):
    pass


class _BindingRuntime:
    def __init__(
        self,
        states: dict[ChatBindingKey, RuntimeStateDict],
    ) -> None:
        self.states = states
        self._next_incarnation = 0
        self._current: dict[
            ChatBindingKey,
            tuple[RuntimeStateDict, BindingRuntimeHandle],
        ] = {}

    def _current_handle(
        self,
        binding: ChatBindingKey,
    ) -> BindingRuntimeHandle | None:
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

    def resolve_session(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot:
        state = self.states[binding]
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        current = self._current_handle(handle.binding)
        if current is not handle:
            raise RuntimeError("stale test binding handle")
        return project_binding_session_snapshot(
            self.states[handle.binding],
            handle=handle,
        )

    def resident_session_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None:
        state = self.states.get(binding)
        if state is None:
            return None
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def resident_runtime_state_locked(
        self,
        binding: ChatBindingKey,
    ) -> RuntimeStateDict | None:
        return self.states.get(binding)

    def update_thread_metadata_locked(
        self,
        handle: BindingRuntimeHandle,
        *,
        expected_thread_id: str,
        current_thread_title: str,
        working_dir: str | None = None,
    ) -> BindingSessionSnapshot | None:
        session = self.session_snapshot_locked(handle)
        if session.current_thread_id != expected_thread_id:
            return None
        state = self.states[handle.binding]
        apply_runtime_state_message(
            state,
            ThreadStateChanged(
                current_thread_title=current_thread_title,
                working_dir=(
                    session.working_dir
                    if working_dir is None
                    else working_dir
                ),
            ),
        )
        return project_binding_session_snapshot(state, handle=handle)


class _RecordingRecoveryRuntime:
    def __init__(
        self,
        delegate: ExecutionRecoveryRuntimeTransitions,
    ) -> None:
        self._delegate = delegate
        self.watchdog_prepare_thread_ids: list[int] = []
        self.snapshot_prepare_thread_ids: list[int] = []
        self.snapshot_settle_thread_ids: list[int] = []
        self.snapshot_settle_attempted = threading.Event()
        self.fallback_settle_thread_ids: list[int] = []
        self.fallback_settle_attempted = threading.Event()
        self.rescheduled = threading.Event()
        self.block_snapshot_prepare = False
        self.snapshot_prepare_entered = threading.Event()
        self.release_snapshot_prepare = threading.Event()
        self.after_snapshot_settle: Callable[[], None] | None = None

    def prepare_mirror_watchdog(
        self,
        command: PrepareMirrorWatchdogCommand,
    ) -> MirrorWatchdogInstallPreparation | None:
        self.watchdog_prepare_thread_ids.append(threading.get_ident())
        prepared = self._delegate.prepare_mirror_watchdog(command)
        if prepared is not None and len(self.watchdog_prepare_thread_ids) > 1:
            self.rescheduled.set()
        return prepared

    def prepare_snapshot_reconcile(
        self,
        command: PrepareSnapshotReconcileCommand,
    ) -> SnapshotReconcilePreparation | None:
        self.snapshot_prepare_thread_ids.append(threading.get_ident())
        prepared = self._delegate.prepare_snapshot_reconcile(command)
        if self.block_snapshot_prepare:
            self.snapshot_prepare_entered.set()
            self.release_snapshot_prepare.wait(timeout=5.0)
        return prepared

    def apply_execution_snapshot(
        self,
        command: ApplyExecutionSnapshotCommand,
    ) -> ExecutionSnapshotTransition | None:
        self.snapshot_settle_thread_ids.append(threading.get_ident())
        self.snapshot_settle_attempted.set()
        transition = self._delegate.apply_execution_snapshot(command)
        if self.after_snapshot_settle is not None:
            self.after_snapshot_settle()
        return transition

    def prepare_terminal_fallback(self, command):
        self.fallback_settle_thread_ids.append(threading.get_ident())
        self.fallback_settle_attempted.set()
        return self._delegate.prepare_terminal_fallback(command)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _WatchdogHarness:
    binding = ("ou_user", "chat-1")

    def __init__(self, test: unittest.TestCase) -> None:
        self._test = test
        self._closed = False
        self._tempdir = tempfile.TemporaryDirectory()
        data_dir = pathlib.Path(self._tempdir.name)
        self._manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda _chat_id, _message_id: False,
        )
        self.state = self.new_running_state()
        self.states = {self.binding: self.state}
        self.binding_runtime = _BindingRuntime(self.states)
        self.lock = threading.RLock()
        self.turn_execution = TurnExecutionCoordinator()
        recovery_delegate = ExecutionRecoveryRuntimeTransitions(
            lock=self.lock,
            binding_runtime=self.binding_runtime,
            turn_execution=self.turn_execution,
        )
        self.recovery_runtime = _RecordingRecoveryRuntime(recovery_delegate)
        self.notification_runtime = AdapterNotificationRuntimeTransitions(
            lock=self.lock,
            binding_runtime=self.binding_runtime,
            turn_execution=self.turn_execution,
        )
        self.runtime_loop = RuntimeLoop(name="watchdog-staging-test-loop")
        self.runtime_loop.start()
        self.runtime_thread_id = self.runtime_loop.call(threading.get_ident)

        self.gate = AdapterIngressGate(
            invalidate_previous_epoch=lambda: None,
            activate_connection_epoch=lambda _generation: None,
        )
        assert self.gate.accept(1)

        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.read_thread_ids: list[int] = []
        self.read_expected_generations: list[int | None] = []
        self.generation_settle_thread_ids: list[int] = []
        self.generation_settle_attempted = threading.Event()
        self.block_generation_settle = False
        self.generation_settle_entered = threading.Event()
        self.release_generation_settle = threading.Event()
        self.finalization_prepare_thread_ids: list[int] = []
        self.finalization_prepare_attempted = threading.Event()
        self.presentation_thread_ids: list[int] = []
        self.presentation_started = threading.Event()
        self.legacy_finalization_thread_ids: list[int] = []
        self.card_presentation_thread_ids: list[int] = []
        self.terminal_result_thread_ids: list[int] = []
        self.read_error: Exception | None = None
        self.snapshot_thread_status = "idle"
        self.snapshot_turn_status = "completed"

        self.controller = ExecutionRecoveryController(
            runtime=self.recovery_runtime,
            runtime_call=self.runtime_loop.call,
            capture_connection_generation=(
                self.gate.capture_existing_connection_generation
            ),
            run_if_connection_generation=self._run_if_connection_generation,
            resolve_session=lambda sender_id, chat_id: (
                self.binding_runtime.resolve_session((sender_id, chat_id))
            ),
            finalize_execution=self._legacy_finalize,
            prepare_execution_finalization=self._prepare_finalization,
            present_execution_finalization=self._present_finalization,
            mark_compact_start_outcome_unknown=lambda _session, _thread_id: None,
            dispatch_execution_card_message=self._dispatch_execution_card,
            publish_terminal_result=self._publish_terminal_result,
            has_recorded_terminal_result=lambda **_kwargs: False,
            deliver_generated_images_from_snapshot=lambda **_kwargs: 0,
            read_thread=self._read_thread,
            is_thread_not_found_error=lambda exc: isinstance(
                exc,
                _ThreadNotFound,
            ),
            is_turn_thread_not_found_error=lambda _exc: False,
            is_pre_send_error=lambda _exc: False,
            is_transport_disconnect=lambda _exc: False,
            is_request_timeout_error=lambda exc: isinstance(exc, TimeoutError),
            runtime_recovery_reason=str,
            mirror_watchdog_seconds=lambda: 60.0,
            compact_start_timeout_seconds=lambda: 60.0,
            terminal_empty_retry_count=lambda: 1,
            terminal_empty_retry_delay_seconds=lambda: 0.0,
        )
        test.addCleanup(self.close)

    def new_running_state(self) -> RuntimeStateDict:
        state = self._manager.build_default_runtime_state()
        state["running"] = True
        state["current_thread_id"] = "thread-a"
        state["current_thread_title"] = "original title"
        state["working_dir"] = "/tmp/original"
        state["current_turn_id"] = "turn-a"
        state["current_execution_kind"] = "prompt"
        state["current_prompt_message_id"] = "prompt-a"
        state["started_at"] = time.monotonic() - 1.0
        set_execution_page_state(state, current_message_id="card-a")
        return state

    def start_watchdog(self) -> None:
        self.runtime_loop.call(
            self.controller.schedule_mirror_watchdog,
            *self.binding,
        )
        registration = self.state["mirror_watchdog_registration"]
        assert registration is not None
        registration.timer.cancel()
        self.controller.submit_mirror_watchdog(registration.ticket)

    def _read_thread(self, _thread_id: str, **kwargs: Any) -> ThreadSnapshot:
        self.read_thread_ids.append(threading.get_ident())
        self.read_expected_generations.append(
            kwargs.get("expected_connection_generation")
        )
        self.read_started.set()
        if not self.release_read.wait(timeout=5.0):
            raise TimeoutError("test did not release generation-pinned thread/read")
        if self.read_error is not None:
            raise self.read_error
        return ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-a",
                cwd="/tmp/from-snapshot",
                name="snapshot title",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status=self.snapshot_thread_status,
            ),
            turns=[
                {
                    "id": "turn-a",
                    "status": self.snapshot_turn_status,
                    "items": [
                        {
                            "type": "agentMessage",
                            "text": "snapshot final reply",
                        }
                    ],
                }
            ],
        )

    def _run_if_connection_generation(
        self,
        generation: int,
        callback: Callable[[], object],
    ) -> object:
        self.generation_settle_thread_ids.append(threading.get_ident())
        self.generation_settle_attempted.set()
        if self.block_generation_settle:
            self.generation_settle_entered.set()
            self.release_generation_settle.wait(timeout=5.0)
        return self.gate.run_if_connection_generation(generation, callback)

    def _legacy_finalize(
        self,
        _session: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationResult:
        self.legacy_finalization_thread_ids.append(threading.get_ident())
        return FeishuExecutionFinalizationResult(had_card=True, retired=True)

    def _prepare_finalization(
        self,
        session: BindingSessionSnapshot,
    ) -> BindingSessionSnapshot | None:
        self.finalization_prepare_thread_ids.append(threading.get_ident())
        self.finalization_prepare_attempted.set()
        try:
            current = self.binding_runtime.session_snapshot_locked(session.handle)
        except RuntimeError:
            return None
        return (
            session
            if BindingExecutionTarget.from_session(session).matches(current)
            else None
        )

    def _present_finalization(
        self,
        _plan: object,
    ) -> FeishuExecutionFinalizationResult:
        self.presentation_thread_ids.append(threading.get_ident())
        self.presentation_started.set()
        return FeishuExecutionFinalizationResult(had_card=True, retired=True)

    def _dispatch_execution_card(self, *_args: Any, **_kwargs: Any) -> bool:
        self.card_presentation_thread_ids.append(threading.get_ident())
        return True

    def _publish_terminal_result(self, *_args: Any, **_kwargs: Any) -> bool:
        self.terminal_result_thread_ids.append(threading.get_ident())
        return True

    def cancel_resident_watchdog_timers(self) -> None:
        for state in tuple(self.states.values()):
            registration = state["mirror_watchdog_registration"]
            if registration is not None:
                registration.timer.cancel()

    def apply_newer_notification(self, *, reply_text: str) -> int:
        def apply_notification() -> int:
            captured = self.binding_runtime.resolve_session(self.binding)
            marked = self.notification_runtime.mark_execution_runtime_event(
                ExecutionRuntimeEventCommand(
                    target=BindingExecutionTarget.from_session(captured),
                    thread_id="thread-a",
                    turn_id="turn-a",
                    occurred_at=time.monotonic(),
                )
            )
            assert marked is not None
            updated = self.notification_runtime.append_assistant_delta(
                AssistantDeltaNotificationCommand(
                    target=BindingExecutionTarget.from_session(marked),
                    delta=reply_text,
                )
            )
            assert updated is not None
            return threading.get_ident()

        thread_id = self.runtime_loop.call(apply_notification)
        assert type(thread_id) is int
        return thread_id

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.release_read.set()
        self.recovery_runtime.release_snapshot_prepare.set()
        self.release_generation_settle.set()
        try:
            self.controller.shutdown(timeout=1.0)
        finally:
            self.cancel_resident_watchdog_timers()
            self.runtime_loop.stop(timeout=1.0)
            self._tempdir.cleanup()


class ExecutionRecoveryWatchdogStagingTests(unittest.TestCase):
    def test_blocked_read_leaves_runtime_loop_free_and_preserves_stage_threads(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        sentinel_done = threading.Event()
        sentinel_thread_ids: list[int] = []

        def sentinel() -> None:
            sentinel_thread_ids.append(threading.get_ident())
            sentinel_done.set()

        harness.runtime_loop.submit(sentinel)

        self.assertTrue(sentinel_done.wait(timeout=1.0))
        self.assertFalse(harness.presentation_started.is_set())
        self.assertEqual(sentinel_thread_ids, [harness.runtime_thread_id])

        harness.release_read.set()
        self.assertTrue(harness.presentation_started.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(
            harness.recovery_runtime.snapshot_prepare_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(
            harness.generation_settle_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(
            harness.recovery_runtime.snapshot_settle_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(
            harness.finalization_prepare_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(len(harness.read_thread_ids), 1)
        self.assertEqual(harness.read_thread_ids, harness.presentation_thread_ids)
        self.assertNotEqual(
            harness.read_thread_ids[0],
            harness.runtime_thread_id,
        )
        self.assertEqual(harness.read_expected_generations, [1])
        self.assertEqual(harness.legacy_finalization_thread_ids, [])
        self.assertEqual(
            harness.card_presentation_thread_ids,
            harness.read_thread_ids,
        )
        self.assertEqual(
            harness.terminal_result_thread_ids,
            harness.read_thread_ids,
        )

    def test_newer_notification_rejects_terminal_snapshot_and_reschedules(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        notification_thread_id = harness.apply_newer_notification(
            reply_text="newer notification reply",
        )
        harness.release_read.set()

        self.assertTrue(
            harness.recovery_runtime.snapshot_settle_attempted.wait(timeout=1.0)
        )
        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(notification_thread_id, harness.runtime_thread_id)
        self.assertTrue(harness.state["running"])
        self.assertEqual(harness.state["runtime_channel_state"], "live")
        self.assertEqual(
            harness.state["execution_transcript"].reply_text(),
            "newer notification reply",
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_newer_notification_rejects_timeout_degraded_settlement(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.read_error = TimeoutError("stale timeout")
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        harness.apply_newer_notification(reply_text="live notification reply")
        harness.release_read.set()

        self.assertTrue(harness.generation_settle_attempted.wait(timeout=1.0))
        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(harness.state["runtime_channel_state"], "live")
        self.assertEqual(
            harness.state["execution_transcript"].reply_text(),
            "live notification reply",
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_newer_notification_rejects_not_found_fallback(self) -> None:
        harness = _WatchdogHarness(self)
        harness.read_error = _ThreadNotFound("stale not-found")
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        harness.apply_newer_notification(reply_text="observed after read began")
        harness.release_read.set()

        self.assertTrue(
            harness.recovery_runtime.fallback_settle_attempted.wait(timeout=1.0)
        )
        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertTrue(harness.state["running"])
        self.assertEqual(harness.state["runtime_channel_state"], "live")
        self.assertEqual(
            harness.state["execution_transcript"].reply_text(),
            "observed after read began",
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_generation_replacement_rejects_old_timeout_degraded_settlement(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.state["runtime_channel_state"] = "live"
        harness.read_error = TimeoutError("old backend timeout")
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        self.assertTrue(harness.gate.accept(2))
        harness.release_read.set()

        self.assertTrue(harness.generation_settle_attempted.wait(timeout=1.0))
        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(harness.state["runtime_channel_state"], "live")
        self.assertEqual(
            harness.recovery_runtime.snapshot_settle_thread_ids,
            [],
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_prepare_without_live_generation_only_reschedules(self) -> None:
        harness = _WatchdogHarness(self)
        harness.state["runtime_channel_state"] = "live"
        self.assertTrue(harness.gate.observe_disconnect(1))

        harness.start_watchdog()

        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertFalse(harness.read_started.is_set())
        self.assertEqual(harness.state["runtime_channel_state"], "live")
        self.assertEqual(
            harness.recovery_runtime.snapshot_settle_thread_ids,
            [],
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_connection_generation_replacement_rejects_old_snapshot_settlement(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        self.assertTrue(harness.gate.accept(2))
        harness.release_read.set()

        self.assertTrue(harness.generation_settle_attempted.wait(timeout=1.0))
        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.runtime_loop.call(lambda: None)
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(harness.read_expected_generations, [1])
        self.assertEqual(
            harness.generation_settle_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(
            harness.recovery_runtime.snapshot_settle_thread_ids,
            [],
        )
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertEqual(harness.state["current_thread_title"], "original title")
        self.assertEqual(harness.state["working_dir"], "/tmp/original")
        self.assertEqual(harness.state["execution_transcript"].reply_text(), "")
        self.assertIsNotNone(harness.state["mirror_watchdog_registration"])

    def test_active_snapshot_reschedules_without_finalization_or_metadata_io(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.snapshot_thread_status = "active"
        harness.snapshot_turn_status = "inProgress"

        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))
        harness.release_read.set()

        self.assertTrue(harness.recovery_runtime.rescheduled.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(
            harness.recovery_runtime.snapshot_settle_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(harness.finalization_prepare_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertEqual(harness.state["current_thread_title"], "original title")
        self.assertEqual(harness.state["working_dir"], "/tmp/original")
        self.assertIsNotNone(harness.state["mirror_watchdog_registration"])

    def test_not_found_fallback_requires_same_generation(self) -> None:
        harness = _WatchdogHarness(self)
        harness.read_error = _ThreadNotFound("missing thread")

        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))
        harness.release_read.set()

        self.assertTrue(harness.presentation_started.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(
            harness.recovery_runtime.fallback_settle_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(harness.recovery_runtime.snapshot_settle_thread_ids, [])
        self.assertEqual(
            harness.finalization_prepare_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(harness.presentation_thread_ids, harness.read_thread_ids)

        stale = _WatchdogHarness(self)
        stale.read_error = _ThreadNotFound("missing old thread")
        stale.start_watchdog()
        self.assertTrue(stale.read_started.wait(timeout=1.0))
        self.assertTrue(stale.gate.accept(2))
        stale.release_read.set()

        self.assertTrue(stale.recovery_runtime.rescheduled.wait(timeout=1.0))
        stale.controller.shutdown(timeout=1.0)

        self.assertEqual(stale.recovery_runtime.fallback_settle_thread_ids, [])
        self.assertEqual(stale.finalization_prepare_thread_ids, [])
        self.assertEqual(stale.presentation_thread_ids, [])

        replaced = _WatchdogHarness(self)
        replaced.read_error = _ThreadNotFound("missing replaced thread")
        replaced.start_watchdog()
        self.assertTrue(replaced.read_started.wait(timeout=1.0))

        def install_successor_target() -> None:
            replaced.state["current_turn_id"] = "turn-b"
            replaced.state["current_prompt_message_id"] = "prompt-b"
            replaced.state["started_at"] = time.monotonic()
            set_execution_page_state(
                replaced.state,
                current_message_id="card-b",
            )

        replaced.runtime_loop.call(install_successor_target)
        replaced.release_read.set()
        self.assertTrue(
            replaced.recovery_runtime.fallback_settle_attempted.wait(
                timeout=1.0
            )
        )
        replaced.controller.shutdown(timeout=1.0)

        self.assertEqual(
            replaced.recovery_runtime.fallback_settle_thread_ids,
            [replaced.runtime_thread_id],
        )
        self.assertEqual(replaced.finalization_prepare_thread_ids, [])
        self.assertEqual(replaced.presentation_thread_ids, [])

    def test_target_replacement_does_not_apply_old_snapshot_to_successor(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))

        def install_successor_target() -> None:
            harness.state["current_thread_id"] = "thread-b"
            harness.state["current_thread_title"] = "successor title"
            harness.state["working_dir"] = "/tmp/successor"
            harness.state["current_turn_id"] = "turn-b"
            harness.state["current_prompt_message_id"] = "prompt-b"
            harness.state["started_at"] = time.monotonic()
            set_execution_page_state(
                harness.state,
                current_message_id="card-b",
            )
            harness.state["execution_transcript"].set_reply_text(
                "successor reply"
            )

        harness.runtime_loop.call(install_successor_target)
        harness.release_read.set()

        self.assertTrue(
            harness.recovery_runtime.snapshot_settle_attempted.wait(timeout=1.0)
        )
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(harness.state["current_thread_id"], "thread-b")
        self.assertEqual(harness.state["current_thread_title"], "successor title")
        self.assertEqual(harness.state["working_dir"], "/tmp/successor")
        self.assertEqual(
            harness.state["execution_transcript"].reply_text(),
            "successor reply",
        )
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertFalse(harness.recovery_runtime.rescheduled.is_set())
        self.assertIsNone(harness.state["mirror_watchdog_registration"])

    def test_binding_replacement_does_not_apply_old_snapshot_to_successor(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))
        successor = harness.new_running_state()
        successor["current_thread_title"] = "replacement binding title"
        successor["working_dir"] = "/tmp/replacement-binding"
        successor["execution_transcript"].set_reply_text(
            "replacement binding reply"
        )

        harness.runtime_loop.call(
            harness.states.__setitem__,
            harness.binding,
            successor,
        )
        harness.release_read.set()

        self.assertTrue(
            harness.recovery_runtime.snapshot_settle_attempted.wait(timeout=1.0)
        )
        harness.controller.shutdown(timeout=1.0)

        self.assertIs(harness.states[harness.binding], successor)
        self.assertEqual(
            successor["current_thread_title"],
            "replacement binding title",
        )
        self.assertEqual(successor["working_dir"], "/tmp/replacement-binding")
        self.assertEqual(
            successor["execution_transcript"].reply_text(),
            "replacement binding reply",
        )
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertFalse(harness.recovery_runtime.rescheduled.is_set())
        self.assertIsNone(successor["mirror_watchdog_registration"])

    def test_successor_installed_after_snapshot_settle_is_not_finalized(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)

        def install_successor() -> None:
            harness.state["current_thread_id"] = "thread-b"
            harness.state["current_thread_title"] = "successor title"
            harness.state["working_dir"] = "/tmp/successor"
            harness.state["current_turn_id"] = "turn-b"
            harness.state["current_prompt_message_id"] = "prompt-b"
            harness.state["started_at"] = time.monotonic()
            harness.state["running"] = True
            set_execution_page_state(
                harness.state,
                current_message_id="card-b",
            )
            harness.state["execution_transcript"].set_reply_text(
                "successor reply"
            )

        harness.recovery_runtime.after_snapshot_settle = install_successor
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))
        harness.release_read.set()

        self.assertTrue(
            harness.recovery_runtime.snapshot_settle_attempted.wait(timeout=1.0)
        )
        self.assertTrue(harness.finalization_prepare_attempted.wait(timeout=1.0))
        harness.controller.shutdown(timeout=1.0)

        self.assertEqual(harness.state["current_thread_id"], "thread-b")
        self.assertEqual(harness.state["current_thread_title"], "successor title")
        self.assertEqual(harness.state["working_dir"], "/tmp/successor")
        self.assertEqual(
            harness.state["execution_transcript"].reply_text(),
            "successor reply",
        )
        self.assertEqual(
            harness.finalization_prepare_thread_ids,
            [harness.runtime_thread_id],
        )
        self.assertEqual(harness.presentation_thread_ids, [])

    def test_stop_observed_after_prepare_prevents_rpc_dispatch(self) -> None:
        harness = _WatchdogHarness(self)
        harness.recovery_runtime.block_snapshot_prepare = True
        harness.start_watchdog()
        self.assertTrue(
            harness.recovery_runtime.snapshot_prepare_entered.wait(timeout=1.0)
        )

        shutdown_done = threading.Event()
        shutdown_thread = threading.Thread(
            target=lambda: (
                harness.controller.shutdown(timeout=2.0),
                shutdown_done.set(),
            ),
            name="watchdog-prepare-shutdown",
        )
        shutdown_thread.start()
        self._wait_for_stop(harness)
        harness.recovery_runtime.release_snapshot_prepare.set()
        shutdown_thread.join(timeout=1.0)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertTrue(shutdown_done.is_set())
        self.assertFalse(harness.read_started.is_set())
        self.assertEqual(harness.recovery_runtime.snapshot_settle_thread_ids, [])
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertFalse(harness.recovery_runtime.rescheduled.is_set())

    def test_stop_observed_inside_generation_guard_blocks_settlement(self) -> None:
        for read_error in (None, _ThreadNotFound("missing thread")):
            with self.subTest(read_error=type(read_error).__name__):
                harness = _WatchdogHarness(self)
                harness.read_error = read_error
                harness.block_generation_settle = True
                harness.start_watchdog()
                self.assertTrue(harness.read_started.wait(timeout=1.0))
                harness.release_read.set()
                self.assertTrue(
                    harness.generation_settle_entered.wait(timeout=1.0)
                )

                shutdown_thread = threading.Thread(
                    target=lambda: harness.controller.shutdown(timeout=2.0),
                    name="watchdog-settle-shutdown",
                )
                shutdown_thread.start()
                self._wait_for_stop(harness)
                harness.release_generation_settle.set()
                shutdown_thread.join(timeout=1.0)

                self.assertFalse(shutdown_thread.is_alive())
                self.assertEqual(
                    harness.recovery_runtime.snapshot_settle_thread_ids,
                    [],
                )
                self.assertEqual(
                    harness.recovery_runtime.fallback_settle_thread_ids,
                    [],
                )
                self.assertEqual(harness.finalization_prepare_thread_ids, [])
                self.assertEqual(harness.presentation_thread_ids, [])
                self.assertFalse(harness.recovery_runtime.rescheduled.is_set())

    def test_shutdown_waits_for_read_and_blocks_late_settle_presentation_reschedule(
        self,
    ) -> None:
        harness = _WatchdogHarness(self)
        harness.start_watchdog()
        self.assertTrue(harness.read_started.wait(timeout=1.0))
        shutdown_done = threading.Event()
        shutdown_errors: list[BaseException] = []

        def shutdown() -> None:
            try:
                harness.controller.shutdown(timeout=2.0)
            except BaseException as exc:
                shutdown_errors.append(exc)
            finally:
                shutdown_done.set()

        shutdown_thread = threading.Thread(
            target=shutdown,
            name="watchdog-staging-shutdown",
        )
        shutdown_thread.start()
        deadline = time.monotonic() + 1.0
        while (
            not harness.controller._workers.stop_requested
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)

        self.assertTrue(harness.controller._workers.stop_requested)
        self.assertFalse(shutdown_done.is_set())

        harness.release_read.set()
        shutdown_thread.join(timeout=1.0)

        self.assertFalse(shutdown_thread.is_alive())
        self.assertTrue(shutdown_done.is_set())
        self.assertEqual(shutdown_errors, [])
        self.assertFalse(harness.generation_settle_attempted.is_set())
        self.assertFalse(
            harness.recovery_runtime.snapshot_settle_attempted.is_set()
        )
        self.assertEqual(harness.presentation_thread_ids, [])
        self.assertFalse(harness.recovery_runtime.rescheduled.is_set())
        self.assertEqual(
            len(harness.recovery_runtime.watchdog_prepare_thread_ids),
            1,
        )
        self.assertIsNone(harness.state["mirror_watchdog_registration"])

    @staticmethod
    def _wait_for_stop(harness: _WatchdogHarness) -> None:
        deadline = time.monotonic() + 1.0
        while (
            not harness.controller._workers.stop_requested
            and time.monotonic() < deadline
        ):
            time.sleep(0.001)
        if not harness.controller._workers.stop_requested:
            raise AssertionError("recovery worker stop was not requested")


if __name__ == "__main__":
    unittest.main()
