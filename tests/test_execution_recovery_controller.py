import pathlib
import tempfile
import threading
import time
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager, ResolvedRuntimeBinding
from bot.execution_recovery_controller import ExecutionRecoveryController, TerminalReconcileTarget
from bot.generated_image_delivery import collect_generated_images
from bot.runtime_state import apply_runtime_state_message
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
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
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(self, state, *, compact_start_timeout_seconds: float = 60.0):
        lock = threading.RLock()
        binding = ("ou_user", "c1")
        turn_execution = TurnExecutionCoordinator()
        patches: list[dict[str, object]] = []
        deletes: list[str] = []
        finalized: list[tuple[str, str]] = []
        terminal_results: list[dict[str, object]] = []
        delivered_images: list[dict[str, object]] = []
        snapshots: list[ThreadSnapshot | Exception] = []
        recorded_terminal_results: set[tuple[str, str]] = set()

        def _read_thread(thread_id: str) -> ThreadSnapshot:
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
            dispatch_execution_card_message=lambda message_id, *, transcript, running, elapsed, cancelled: patches.append(
                {
                    "message_id": message_id,
                    "reply_text": transcript.reply_text(),
                    "running": running,
                    "elapsed": elapsed,
                    "cancelled": cancelled,
                }
            )
            or True,
            remove_execution_card_message=lambda message_id: deletes.append(message_id) or True,
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

    def test_capture_terminal_reconcile_target_preserves_execution_anchor(self) -> None:
        state = self._make_state()
        controller, _, _, _, _, _, _ = self._make_controller(state)
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
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
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
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_restores_interrupted_status(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, _, _ = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "card-1"
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

    def test_reconcile_execution_snapshot_waits_for_unbound_turn_id(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "compact-card"
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
        self.assertEqual(state["current_message_id"], "compact-card")
        self.assertTrue(state["running"])
        self.assertEqual(len(snapshots), 1)

    def test_mirror_watchdog_finalizes_unbound_compact_after_start_timeout(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(
            state,
            compact_start_timeout_seconds=0.1,
        )
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "compact-card"
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "compact"
        state["current_turn_id"] = ""
        state["started_at"] = time.monotonic() - 1.0

        controller.run_mirror_watchdog("ou_user", "c1", state["mirror_watchdog_generation"])

        self.assertEqual(finalized, [("ou_user", "c1")])
        self.assertIn("状态不可确认", state["execution_transcript"].reply_text())
        self.assertEqual(snapshots, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_reconcile_execution_snapshot_not_found_finalizes(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
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
        self.assertEqual(deletes, [])
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
                        status="completed",
                    ),
                    turns=[{"id": "turn-1", "items": []}],
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
                            "items": [{"type": "agentMessage", "text": "late final"}],
                        }
                    ],
                ),
            ]
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
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_strips_terminal_final_reply_after_publish(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        raw_final_reply = "  最终答案\n"
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
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
        self.assertEqual(deletes, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "阶段总结")
        self.assertEqual(state["terminal_result_text"], raw_final_reply)
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

    def test_terminal_reconcile_uses_interrupted_snapshot_for_card_status(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, _, _ = self._make_controller(state)
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

        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0]["cancelled"])

    def test_terminal_reconcile_refreshes_interrupted_status_without_reply_text(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
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
            turns=[{"id": "turn-1", "status": "interrupted", "items": []}],
        )
        snapshots.extend([snapshot, snapshot, snapshot])

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
                    "reply_text": "",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": True,
                }
            ],
        )
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_terminal_reconcile_refreshes_interrupted_status_when_retained_card_text_is_unchanged(
        self,
    ) -> None:
        state = self._make_state()
        controller, snapshots, patches, _, _, terminal_results, delivered_images = (
            self._make_controller(state)
        )
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
        state["execution_transcript"].append_process_note("过程内容")
        state["execution_transcript"].set_reply_text("本地回复")
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

        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["reply_text"], "本地回复")
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
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
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

        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["reply_text"], "相同回复")
        self.assertTrue(patches[0]["cancelled"])
        self.assertEqual(terminal_results, [])
        self.assertEqual(delivered_images, [])

    def test_snapshot_reply_does_not_apply_status_from_unmatched_turn(self) -> None:
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

        self.assertEqual(projection.full_reply_text, "other reply")
        self.assertEqual(projection.turn_status, "")

    def test_run_terminal_execution_reconcile_sends_fallback_transcript_when_snapshot_unavailable(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
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
        self.assertEqual(
            terminal_results,
            [
                {
                    "chat_id": "c1",
                    "final_reply_text": "fallback answer",
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
        state["execution_transcript"].set_reply_text("fallback answer")
        snapshots.append(_ThreadNotFound("thread not found"))
        controller._has_recorded_terminal_result = lambda *, execution_message_id, final_reply_text: (
            execution_message_id == "card-1" and final_reply_text == "fallback answer"
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

        state["execution_transcript"].set_reply_text("fallback answer\n")
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

        self.assertEqual(
            [item["final_reply_text"] for item in terminal_results],
            ["fallback answer", "fallback answer\n"],
        )

    def test_run_terminal_execution_reconcile_keeps_final_reply_on_execution_card_when_result_publish_fails(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
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
        self.assertEqual(deletes, [])
        self.assertEqual(terminal_results, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "最终答案")
        self.assertEqual(state["terminal_result_text"], "")
        self.assertEqual(delivered_images, [])

    def test_run_terminal_execution_reconcile_keeps_minimal_execution_card_when_only_final_result_remains(self) -> None:
        state = self._make_state()
        controller, snapshots, patches, deletes, _, terminal_results, delivered_images = self._make_controller(state)
        state["current_message_id"] = "card-1"
        state["last_execution_message_id"] = "card-1"
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
                    "reply_text": "",
                    "running": False,
                    "elapsed": 5,
                    "cancelled": False,
                }
            ],
        )
        self.assertEqual(deletes, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["terminal_result_text"], "最终答案")
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
        state["current_message_id"] = "card-1"
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

    def test_reconcile_execution_snapshot_uses_turn_error_when_failed_turn_has_no_agent_reply(self) -> None:
        state = self._make_state()
        controller, snapshots, _, _, finalized, terminal_results, delivered_images = self._make_controller(state)
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "card-1"
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
                        "items": [],
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
        state["current_message_id"] = "card-1"
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
        state["current_message_id"] = "card-1"
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
