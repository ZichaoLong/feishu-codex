from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_execution_runtime import BindingExecutionRuntimeTransitions
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.feishu_active_observer import (
    FeishuActiveObserverController,
    ActiveObserverResumeSnapshotRejected,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.runtime_admin_test_support import make_binding_runtime


def _summary(*, status: str = "active") -> ThreadSummary:
    return ThreadSummary(
        thread_id="thread-live",
        cwd="/workspace",
        name="Live thread",
        preview="",
        created_at=1,
        updated_at=2,
        source="appServer",
        status=status,
        history_mode="paginated",
    )


class _RecordingExecutionOutput(ExecutionOutputController):
    def __init__(self) -> None:
        self.opened = []
        self.flushed = []

    def open_initial_execution_page(
        self,
        captured,
        parent_message_id,
        *,
        reply_in_thread=False,
        reserved_message_id="",
    ):
        self.opened.append(
            (
                captured,
                parent_message_id,
                reply_in_thread,
                reserved_message_id,
            )
        )
        return InitialExecutionPageOpenResult(
            status=InitialExecutionPageOpenStatus.ACTIVE,
            session=captured,
            message_id="observer-card",
        )

    def flush_execution_card_for_session(self, session, *, immediate=False):
        self.flushed.append((session, immediate))


class FeishuActiveObserverControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = pathlib.Path(temporary.name)
        self.lock = threading.RLock()
        _leases, self.manager = make_binding_runtime(
            data_dir=data_dir,
            lock=self.lock,
            chat_binding_store=ChatBindingStore(data_dir),
        )
        self.binding = ("ou-user", "chat-1")
        session = self.manager.resolve_session(*self.binding)
        with self.lock:
            self.manager.bind_thread_locked(
                session.handle,
                thread_id="thread-live",
                thread_title="Live thread",
                working_dir="/workspace",
            )
            self.manager.detach_binding_locked(self.binding)
        self.session = self.manager.resolve_session(*self.binding)
        self.execution_runtime = BindingExecutionRuntimeTransitions(
            lock=self.lock,
            binding_runtime=self.manager,
            turn_execution=TurnExecutionCoordinator(),
        )
        self.output = _RecordingExecutionOutput()
        self.controller = FeishuActiveObserverController(
            execution_runtime=self.execution_runtime,
            execution_output=self.output,
        )

    def test_unique_active_turn_primes_page_and_restores_assistant_text(
        self,
    ) -> None:
        snapshot = ThreadSnapshot(
            summary=_summary(),
            turns=[
                {
                    "id": "turn-live",
                    "status": "inProgress",
                    "items": [
                        {
                            "type": "agentMessage",
                            "id": "assistant-1",
                            "text": "已生成的阶段回复",
                        }
                    ],
                }
            ],
        )

        prepared = self.controller.prepare_resume_snapshot(snapshot)
        assert prepared is not None
        execution = self.controller.prime_execution(self.session, prepared)
        result = self.controller.present_execution(execution)

        self.assertEqual(result.status, "opened")
        self.assertEqual(result.turn_id, "turn-live")
        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current.execution.current_turn_id, "turn-live")
        self.assertEqual(
            current.execution.current_execution_kind,
            "active_observer",
        )
        self.assertEqual(
            current.execution.transcript.reply_text(),
            "已生成的阶段回复",
        )
        self.assertEqual(len(self.output.opened), 1)
        self.assertEqual(self.output.flushed, [(current, True)])

    def test_idle_response_does_not_create_an_execution_anchor(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_summary(status="idle"),
            turns=[
                {"id": "turn-done", "status": "completed", "items": []}
            ],
        )

        result = self.controller.prepare_resume_snapshot(snapshot)

        self.assertIsNone(result)
        current = self.manager.resolve_session(*self.binding)
        self.assertFalse(current.execution.running)
        self.assertEqual(current.execution.current_turn_id, "")
        self.assertEqual(self.output.opened, [])
        self.assertEqual(self.output.flushed, [])

    def test_active_response_without_exact_turn_is_rejected(self) -> None:
        snapshot = ThreadSnapshot(summary=_summary(), turns=[])

        with self.assertRaises(ActiveObserverResumeSnapshotRejected):
            self.controller.prepare_resume_snapshot(snapshot)

        self.assertFalse(
            self.manager.resolve_session(*self.binding).execution.running
        )
        self.assertEqual(self.output.opened, [])

    def test_multiple_active_turns_do_not_fabricate_an_anchor(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_summary(),
            turns=[
                {"id": "turn-a", "status": "inProgress", "items": []},
                {"id": "turn-b", "status": "inProgress", "items": []},
            ],
        )

        with self.assertRaises(ActiveObserverResumeSnapshotRejected):
            self.controller.prepare_resume_snapshot(snapshot)
        self.assertFalse(
            self.manager.resolve_session(*self.binding).execution.running
        )
        self.assertEqual(self.output.opened, [])

    def test_active_turn_without_id_does_not_fabricate_an_anchor(self) -> None:
        snapshot = ThreadSnapshot(
            summary=_summary(),
            turns=[{"status": "inProgress", "items": []}],
        )

        with self.assertRaises(ActiveObserverResumeSnapshotRejected):
            self.controller.prepare_resume_snapshot(snapshot)
        self.assertFalse(
            self.manager.resolve_session(*self.binding).execution.running
        )
        self.assertEqual(self.output.opened, [])


if __name__ == "__main__":
    unittest.main()
