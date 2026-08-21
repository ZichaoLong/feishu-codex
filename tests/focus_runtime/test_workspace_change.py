import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSummary
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.focus_runtime.runtime import FocusRuntime as CodexHandler
from bot.service_runtime_lifecycle import ServiceRuntimePhase
from tests.focus_runtime.codex_handler_fakes import (
    _FakeAdapter,
    _FakeBot,
    _bind_authoritative_thread,
)


class CodexHandlerWorkspaceChangeTests(unittest.TestCase):
    def _make_handler(
        self,
        *,
        data_dir: pathlib.Path,
        default_working_dir: str,
    ) -> tuple[CodexHandler, _FakeBot]:
        config_patch = patch(
            "bot.focus_runtime.runtime.load_config_file",
            return_value={
                "default_working_dir": default_working_dir,
                "mirror_watchdog_seconds": 999999,
            },
        )
        adapter_patch = patch(
            "bot.focus_runtime.runtime.CodexAppServerAdapter",
            _FakeAdapter,
        )
        env_patch = patch.dict(
            os.environ,
            {
                "FOCUS_GLOBAL_DATA_DIR": str(data_dir / "_global"),
                "FOCUS_INSTANCE": "default",
            },
            clear=False,
        )
        config_patch.start()
        adapter_patch.start()
        env_patch.start()
        self.addCleanup(config_patch.stop)
        self.addCleanup(adapter_patch.stop)
        self.addCleanup(env_patch.stop)
        handler = CodexHandler(data_dir=data_dir)
        handler._service_runtime_lifecycle._set_phase(ServiceRuntimePhase.ACTIVE)
        handler._service_instance_lease._owner_token = "workspace-change-test"
        self.addCleanup(handler.shutdown)
        bot = _FakeBot(data_dir)
        handler._feishu_platform.attach(bot)
        return handler, bot

    @staticmethod
    def _bind_thread(
        handler: CodexHandler,
        *,
        sender_id: str = "ou-user",
        chat_id: str = "chat-1",
        thread_id: str = "thread-old",
        cwd: str,
    ) -> None:
        thread = ThreadSummary(
            thread_id=thread_id,
            cwd=cwd,
            name="Old",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, sender_id, chat_id, thread)

    @staticmethod
    def _card_content(result) -> str:
        assert result.card is not None
        return "\n".join(
            str(element.get("content", ""))
            for element in result.card["elements"]
            if isinstance(element, dict)
        )

    def test_store_failure_does_not_start_attachment_cleanup_or_partial_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = pathlib.Path(tempdir)
            old_workspace = data_dir / "old"
            new_workspace = data_dir / "new"
            old_workspace.mkdir()
            new_workspace.mkdir()
            handler, _ = self._make_handler(
                data_dir=data_dir,
                default_working_dir=str(old_workspace),
            )
            self._bind_thread(handler, cwd=str(old_workspace))
            binding = ("ou-user", "chat-1")
            stored_before = handler._chat_binding_store.load(binding)

            with (
                patch.object(
                    handler._chat_binding_store,
                    "save",
                    side_effect=OSError("binding save unavailable"),
                ),
                patch.object(
                    handler._file_message_domain,
                    "invalidate_pending_attachments_for_scope",
                ) as invalidate,
                self.assertRaisesRegex(OSError, "binding save unavailable"),
            ):
                handler._runtime_call(
                    handler._feishu_surface.handle_cd_command,
                    "ou-user",
                    "chat-1",
                    str(new_workspace),
                )

            invalidate.assert_not_called()
            session = handler._binding_runtime.resolve_session("ou-user", "chat-1")
            self.assertEqual(session.working_dir, str(old_workspace))
            self.assertEqual(session.current_thread_id, "thread-old")
            self.assertEqual(handler._binding_runtime.thread_subscribers("thread-old"), (binding,))
            self.assertEqual(handler._chat_binding_store.load(binding), stored_before)

    def test_attachment_cleanup_failure_reports_committed_state_and_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = pathlib.Path(tempdir)
            old_workspace = data_dir / "old"
            new_workspace = data_dir / "new"
            old_workspace.mkdir()
            new_workspace.mkdir()
            handler, _ = self._make_handler(
                data_dir=data_dir,
                default_working_dir=str(old_workspace),
            )
            self._bind_thread(handler, cwd=str(old_workspace))
            binding = ("ou-user", "chat-1")

            with (
                patch.object(
                    handler._chat_binding_store,
                    "save",
                    wraps=handler._chat_binding_store.save,
                ) as save,
                patch.object(
                    handler._file_message_domain,
                    "invalidate_pending_attachments_for_scope",
                    side_effect=[OSError("cleanup unavailable"), 2],
                ) as invalidate,
            ):
                first = handler._runtime_call(
                    handler._feishu_surface.handle_cd_command,
                    "ou-user",
                    "chat-1",
                    str(new_workspace),
                )
                committed = handler._chat_binding_store.load(binding)
                second = handler._runtime_call(
                    handler._feishu_surface.handle_cd_command,
                    "ou-user",
                    "chat-1",
                    str(new_workspace),
                )

            assert committed is not None
            self.assertEqual(committed["working_dir"], str(new_workspace))
            self.assertEqual(committed["current_thread_id"], "")
            self.assertGreaterEqual(save.call_count, 1)
            self.assertEqual(invalidate.call_count, 2)
            self.assertIn("目录与 binding 已提交", self._card_content(first))
            self.assertIn("已使 2 个待消费附件失效", self._card_content(second))
            self.assertNotIn("后置清理未完成", self._card_content(second))

    def test_postcommit_queue_cleanup_failure_cannot_masquerade_as_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = pathlib.Path(tempdir)
            old_workspace = data_dir / "old"
            new_workspace = data_dir / "new"
            old_workspace.mkdir()
            new_workspace.mkdir()
            handler, _ = self._make_handler(
                data_dir=data_dir,
                default_working_dir=str(old_workspace),
            )
            self._bind_thread(handler, cwd=str(old_workspace))

            with patch.object(
                handler._feishu_execution_queue,
                "invalidate_binding",
                side_effect=OSError("queue cleanup unavailable"),
            ):
                result = handler._runtime_call(
                    handler._feishu_surface.handle_cd_command,
                    "ou-user",
                    "chat-1",
                    str(new_workspace),
                )

            session = handler._binding_runtime.resolve_session("ou-user", "chat-1")
            self.assertEqual(session.working_dir, str(new_workspace))
            self.assertEqual(session.current_thread_id, "")
            self.assertIn("目录与 binding 已提交", self._card_content(result))
            self.assertEqual(result.card["header"]["title"]["content"], "Codex 目录已切换")

    def test_cd_commits_the_captured_p2p_session_when_group_appears_midflight(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_dir = pathlib.Path(tempdir)
            old_workspace = data_dir / "old"
            new_workspace = data_dir / "new"
            group_workspace = data_dir / "group"
            old_workspace.mkdir()
            new_workspace.mkdir()
            group_workspace.mkdir()
            handler, _ = self._make_handler(
                data_dir=data_dir,
                default_working_dir=str(old_workspace),
            )
            self._bind_thread(handler, cwd=str(old_workspace))
            original_clear = handler._feishu_binding_transitions.clear_thread
            group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "chat-1")

            def install_group_after_p2p_capture(command: object) -> object:
                with handler._lock:
                    handler._binding_runtime._get_or_create_runtime_state_locked(
                        group_binding
                    )
                    group_session = handler._binding_runtime.resident_session_snapshot_locked(
                        group_binding
                    )
                    assert group_session is not None
                    handler._binding_runtime.bind_thread_locked(
                        group_session.handle,
                        thread_id="thread-group",
                        thread_title="Group",
                        working_dir=str(group_workspace),
                    )
                return original_clear(command)

            with patch.object(
                handler._feishu_binding_transitions,
                "clear_thread",
                side_effect=install_group_after_p2p_capture,
            ):
                result = handler._runtime_call(
                    handler._feishu_surface.handle_cd_command,
                    "ou-user",
                    "chat-1",
                    str(new_workspace),
                )

            with handler._lock:
                p2p = handler._binding_runtime.resident_session_snapshot_locked(
                    ("ou-user", "chat-1")
                )
                group = handler._binding_runtime.resident_session_snapshot_locked(
                    group_binding
                )
            assert p2p is not None and group is not None
            self.assertEqual(p2p.current_thread_id, "")
            self.assertEqual(p2p.working_dir, str(new_workspace))
            self.assertEqual(group.current_thread_id, "thread-group")
            self.assertEqual(group.working_dir, str(group_workspace))
            self.assertEqual(result.card["header"]["title"]["content"], "Codex 目录已切换")


if __name__ == "__main__":
    unittest.main()
