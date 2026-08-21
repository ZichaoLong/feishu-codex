import pathlib
import tempfile
import threading
import unittest
from dataclasses import dataclass

from bot.adapter_notification_controller import (
    AdapterNotificationController,
    AdapterNotificationEffects,
)
from bot.adapter_notification_runtime import AdapterNotificationRuntimeTransitions
from bot.feishu_execution_process_projection import (
    DIAGNOSTIC_PROCESS_LOG_LIMIT_BYTES,
    NORMAL_PROCESS_LOG_LIMIT_BYTES,
    FeishuExecutionProcessProjection,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state
from tests.runtime_admin_test_support import make_binding_runtime


@dataclass
class _Transcript:
    text: str = ""

    def process_text(self) -> str:
        return self.text

    def append(self, text: str) -> None:
        self.text += text


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


class FeishuExecutionProcessProjectionTests(unittest.TestCase):
    def test_command_start_is_one_line_utf8_safe_and_field_bounded(self) -> None:
        transcript = _Transcript()
        projected = FeishuExecutionProcessProjection.command_started(
            {
                "cwd": f"/tmp/\ud800/{'目录' * 600}\r\nunsafe",
                "command": f"printf \ud800 first\r\n{'输出' * 600}",
            },
            transcript=transcript,
        )

        projected.encode("utf-8")
        self.assertNotIn("\r", projected)
        self.assertEqual(projected.count("\n"), 2)
        self.assertIn("?", projected)
        self.assertLessEqual(_utf8_size(projected), 2 * 1024 + 16)

    def test_command_completion_tail_depends_only_on_terminal_status(self) -> None:
        output = "first\r\nsecond\rthird\nfourth\nlast\ud800\n"
        cases = (
            ("completed", 0, ("last?",), ("fourth", "first")),
            (
                "failed",
                2,
                ("second", "third", "fourth", "last?"),
                ("first",),
            ),
            (
                "declined",
                None,
                ("second", "third", "fourth", "last?"),
                ("first",),
            ),
            (
                "futureStatus",
                None,
                ("second", "third", "fourth", "last?"),
                ("first",),
            ),
        )

        for status, exit_code, present, absent in cases:
            with self.subTest(status=status):
                projected = FeishuExecutionProcessProjection.command_completed(
                    {
                        "status": status,
                        "exitCode": exit_code,
                        "durationMs": 125,
                        "aggregatedOutput": output,
                    },
                    transcript=_Transcript(),
                )
                projected.encode("utf-8")
                self.assertIn(f"status={status}", projected)
                for value in present:
                    self.assertIn(value, projected)
                for value in absent:
                    self.assertNotIn(value, projected)

    def test_long_single_line_output_keeps_a_bounded_tail(self) -> None:
        projected = FeishuExecutionProcessProjection.command_completed(
            {
                "status": "failed",
                "exitCode": 1,
                "aggregatedOutput": f"{'前' * 4096}TAIL",
            },
            transcript=_Transcript(),
        )

        self.assertIn("TAIL", projected)
        self.assertIn("…", projected)
        self.assertLessEqual(_utf8_size(projected), 2200)

    def test_one_mib_of_leading_blank_lines_does_not_enter_completion_tail(
        self,
    ) -> None:
        projected = FeishuExecutionProcessProjection.command_completed(
            {
                "status": "failed",
                "exitCode": 1,
                "aggregatedOutput": "\n" * (1024 * 1024) + "TAIL\n",
            },
            transcript=_Transcript(),
        )

        self.assertIn("[诊断输出]\nTAIL\n", projected)
        self.assertLessEqual(_utf8_size(projected), 2200)

    def test_file_summary_keeps_count_first_three_paths_and_remainder(self) -> None:
        projected = FeishuExecutionProcessProjection.file_change_completed(
            {
                "changes": [
                    {"path": "one.py"},
                    {"path": "二.py"},
                    {"path": f"\ud800-{'长' * 600}.py"},
                    {"path": "four.py"},
                    {"path": "five.py"},
                ]
            },
            transcript=_Transcript(),
        )

        projected.encode("utf-8")
        self.assertIn("count=5", projected)
        self.assertIn("one.py", projected)
        self.assertIn("二.py", projected)
        self.assertIn("?", projected)
        self.assertIn("另有 2 项", projected)
        self.assertNotIn("four.py", projected)
        self.assertNotIn("five.py", projected)

    def test_normal_and_diagnostic_total_budgets_include_existing_process_text(
        self,
    ) -> None:
        normal = _Transcript("n" * (NORMAL_PROCESS_LOG_LIMIT_BYTES - 100))
        normal.append(
            FeishuExecutionProcessProjection.command_started(
                {"cwd": "/tmp", "command": "x" * 4000},
                transcript=normal,
            )
        )
        normal.append(
            FeishuExecutionProcessProjection.command_completed(
                {
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "y" * 10000,
                },
                transcript=normal,
            )
        )
        self.assertLessEqual(
            _utf8_size(normal.process_text()),
            NORMAL_PROCESS_LOG_LIMIT_BYTES,
        )

        diagnostic = _Transcript("d" * (NORMAL_PROCESS_LOG_LIMIT_BYTES - 100))
        diagnostic.append(
            FeishuExecutionProcessProjection.command_started(
                {"cwd": "/tmp", "command": "x" * 4000},
                transcript=diagnostic,
            )
        )
        diagnostic.append(
            FeishuExecutionProcessProjection.command_completed(
                {
                    "status": "failed",
                    "exitCode": 1,
                    "aggregatedOutput": "z" * 10000,
                },
                transcript=diagnostic,
            )
        )
        self.assertGreater(
            _utf8_size(diagnostic.process_text()),
            NORMAL_PROCESS_LOG_LIMIT_BYTES,
        )
        self.assertLessEqual(
            _utf8_size(diagnostic.process_text()),
            DIAGNOSTIC_PROCESS_LOG_LIMIT_BYTES,
        )

        files = _Transcript("f" * (NORMAL_PROCESS_LOG_LIMIT_BYTES - 10))
        files.append(
            FeishuExecutionProcessProjection.file_change_completed(
                {"changes": [{"path": "changed.py"}] * 100},
                transcript=files,
            )
        )
        self.assertLessEqual(
            _utf8_size(files.process_text()),
            NORMAL_PROCESS_LOG_LIMIT_BYTES,
        )


class FeishuExecutionProcessNotificationTests(unittest.TestCase):
    def _make_controller(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=ChatBindingStore(data_dir),
        )
        runtime = AdapterNotificationRuntimeTransitions(
            lock=lock,
            binding_runtime=manager,
            turn_execution=TurnExecutionCoordinator(),
        )
        binding = ("ou_user", "chat-1")
        with lock:
            state = manager._get_or_create_runtime_state_locked(binding)
            state.update(
                current_thread_id="thread-1",
                current_turn_id="turn-1",
                running=True,
            )
            set_execution_page_state(state, current_message_id="card-1")
        note_events: list[tuple[str, str]] = []
        updates: list[tuple[str, str]] = []
        controller = AdapterNotificationController(
            runtime=runtime,
            thread_subscribers=lambda thread_id: (
                (binding,) if thread_id == "thread-1" else ()
            ),
            effects=AdapterNotificationEffects(
                finalize_execution_from_terminal_signal=lambda *args, **kwargs: True,
                dispatch_execution_card_message=lambda *args, **kwargs: None,
                open_initial_execution_page=lambda *args, **kwargs: None,
                schedule_mirror_watchdog=lambda session: note_events.append(
                    session.binding
                ),
                schedule_execution_card_update=lambda session: updates.append(
                    session.binding
                ),
                flush_execution_card=lambda *args, **kwargs: None,
                flush_plan_card=lambda *args, **kwargs: None,
                interrupt_running_turn=lambda **kwargs: None,
                is_pre_send_error=lambda _exc: False,
            ),
        )
        return binding, state, controller, note_events, updates

    def _run_command(self, chunks: tuple[str, ...]):
        _binding, state, controller, note_events, updates = self._make_controller()
        controller.handle_item_started(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "cwd": "/tmp/project",
                    "command": "generate output",
                },
            }
        )
        for delta in chunks:
            controller.handle_command_delta(
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "command-1",
                    "delta": delta,
                }
            )
        controller.handle_item_completed(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "command-1",
                    "type": "commandExecution",
                    "cwd": "/tmp/project",
                    "command": "generate output",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "first line\nlast line\n",
                },
            }
        )
        return state, note_events, updates

    def test_one_mib_delta_is_heartbeat_only_and_chunking_invariant(self) -> None:
        raw = "x" * (1024 * 1024)
        one, one_events, one_updates = self._run_command((raw,))
        chunked, chunked_events, chunked_updates = self._run_command(
            tuple(
                raw[index : index + 256 * 1024]
                for index in range(0, len(raw), 256 * 1024)
            )
        )

        one_text = one["execution_transcript"].process_text()
        chunked_text = chunked["execution_transcript"].process_text()
        self.assertEqual(one_text, chunked_text)
        self.assertNotIn(raw[:128], one_text)
        self.assertIn("last line", one_text)
        self.assertEqual(one_updates, [("ou_user", "chat-1")] * 2)
        self.assertEqual(chunked_updates, [("ou_user", "chat-1")] * 2)
        self.assertEqual(len(one_events), 3)
        self.assertEqual(len(chunked_events), 6)

    def test_patch_updated_refreshes_liveness_without_patching_or_text(self) -> None:
        binding, state, controller, note_events, updates = self._make_controller()
        transcript = state["execution_transcript"]
        transcript.reconcile_current_assistant_text("candidate")
        transcript.start_process_block("active-file-block", marks_work=False)

        controller.handle_file_change_patch_updated(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "file-1",
                "changes": [{"path": "ignored.py"}],
            }
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(updates, [])
        self.assertEqual(transcript.process_text(), "active-file-block")
        self.assertIsNone(transcript.terminal_reply_evidence())


if __name__ == "__main__":
    unittest.main()
