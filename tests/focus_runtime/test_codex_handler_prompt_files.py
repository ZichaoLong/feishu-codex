import pathlib
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from bot.adapters.base import (
    ThreadSummary,
)
from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
    _PNG_1X1,
)


class CodexHandlerPromptFileTests(CodexHandlerHarness):
    def test_status_hides_removed_new_thread_seed_profile_row(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        self.assertNotIn("新 thread seed profile", card["elements"][0]["content"])

    def test_new_thread_uses_current_runtime_overrides_without_profile_injection(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/new")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])

    def test_new_thread_after_model_auto_does_not_fallback_to_configured_model(self) -> None:
        handler, _ = self._make_handler({"model": "gpt-5.5"})

        handler.handle_message("ou_user", "c1", "/model auto")
        handler.handle_message("ou_user", "c1", "/new")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])
        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model_provider"])

    def test_new_thread_reports_bind_failure_instead_of_silently_dropping_command(self) -> None:
        handler, bot = self._make_handler()

        with patch.object(
            handler._feishu_binding_transitions,
            "bind_thread",
            side_effect=RuntimeError("bind failed"),
        ):
            handler.handle_message("ou_user", "c1", "/new")

        self.assertIn("bind failed", bot.replies[-1][1])
        self.assertNotIn("thread-created", handler._adapter.unsubscribe_thread_calls)
        self.assertNotIn(
            "thread_create_recovery",
            handler._operational_status_snapshot(),
        )
        warning_codes = {
            str(item.get("code") or "")
            for item in handler._operational_warnings.snapshot()
        }
        self.assertIn("thread_create_local_commit_failed", warning_codes)

        _bind_authoritative_thread(handler,
            "ou_other",
            "c2",
            ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="created",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
        )
        self.assertEqual(
            _runtime_state(handler, "ou_other", "c2")["current_thread_id"],
            "thread-created",
        )

    def test_feishu_create_model_fact_failure_is_local_and_reported(self) -> None:
        handler, bot = self._make_handler()

        with patch.object(
            handler._effective_settings,
            "record_start_or_resume",
            side_effect=RuntimeError("effective settings registry failed"),
        ):
            handler.handle_message("ou_user", "c1", "/new")

        self.assertIn("effective settings registry failed", bot.replies[-1][1])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertTrue(
            self._service_runtime_holder_ids(handler, "thread-created")
        )
        warning_codes = {
            str(item.get("code") or "")
            for item in handler._operational_warnings.snapshot()
        }
        self.assertIn("thread_create_local_commit_failed", warning_codes)

    def test_new_thread_failure_rolls_back_existing_binding(self) -> None:
        handler, bot = self._make_handler()
        old_thread = ThreadSummary(
            thread_id="thread-old",
            cwd="/tmp/project",
            name="old",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", old_thread)

        with patch.object(handler._binding_runtime._lifecycle, "project_after_bind_locked", side_effect=RuntimeError("x")):
            handler.handle_message("ou_user", "c1", "/new")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertTrue(bot.replies)
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(state["current_thread_title"], "old")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-old"), (("ou_user", "c1"),))
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-created"), ())
        self.assertTrue(self._service_runtime_holder_ids(handler, "thread-created"))
        self.assertNotIn("thread-created", handler._adapter.unsubscribe_thread_calls)

    def test_new_thread_failure_without_existing_binding_clears_new_thread_binding(self) -> None:
        handler, bot = self._make_handler()

        with patch.object(handler._binding_runtime._lifecycle, "project_after_bind_locked", side_effect=RuntimeError("x")):
            handler.handle_message("ou_user", "c1", "/new")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertTrue(bot.replies)
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["feishu_runtime_state"], "")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-created"), ())
        self.assertTrue(self._service_runtime_holder_ids(handler, "thread-created"))
        self.assertNotIn("thread-created", handler._adapter.unsubscribe_thread_calls)

    def test_prompt_starts_without_project_profile_override(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        self.assertIsNone(handler._adapter.create_thread_calls[-1]["model"])
        self.assertIsNone(handler._adapter.start_turn_calls[-1]["model"])

    def test_prompt_without_configured_default_working_dir_uses_home_directory(self) -> None:
        with patch(
            "bot.focus_runtime.runtime.default_working_dir",
            return_value=pathlib.Path("/home/tester"),
        ):
            handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["cwd"], "/home/tester")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["cwd"], "/home/tester")

    def test_prompt_reuses_reserved_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards["m1"] = "reserved-card"

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["execution_pages"].current_message_id, "reserved-card")
        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(bot.patches[-1][0], "reserved-card")

    def test_prompt_failure_patches_reserved_execution_card(self) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards["m1"] = "reserved-card"
        handler._adapter.create_thread = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))

        handler.handle_message("ou_user", "c1", "hello", message_id="m1")

        self.assertNotIn("m1", bot.reserved_execution_cards)
        self.assertEqual(bot.patches[-1][0], "reserved-card")
        self.assertIn("Codex 启动失败", bot.patches[-1][1])
        self.assertIn("无法确认 thread/start", bot.patches[-1][1])
        self.assertIn("boom", bot.patches[-1][1])

    def test_concurrent_prompts_are_serialized_through_runtime_loop(self) -> None:
        handler, bot = self._make_handler()
        original_create_thread = handler._adapter.create_thread
        started = threading.Event()
        release = threading.Event()
        create_thread_calls = 0

        def blocking_create_thread(**kwargs):
            nonlocal create_thread_calls
            create_thread_calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=1))
            return original_create_thread(**kwargs)

        handler._adapter.create_thread = blocking_create_thread
        first = threading.Thread(target=handler.handle_message, args=("ou_user", "c1", "first"))
        second = threading.Thread(target=handler.handle_message, args=("ou_user", "c1", "second"))

        first.start()
        self.assertTrue(started.wait(timeout=1))
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(create_thread_calls, 1)
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_file_attachment_is_staged_and_consumed_by_next_prompt(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"spec-content",
            file_name="spec.pdf",
            content_type="application/pdf",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "spec.pdf")

        self.assertIn("已保存到本地", bot.replies[-1][1])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        staged_files = sorted((workspace / "_feishu_attachments").iterdir())
        self.assertEqual(len(staged_files), 1)
        self.assertEqual(staged_files[0].read_bytes(), b"spec-content")

        handler.handle_message("ou_user", "c1", "请阅读这个文件", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual(input_items[0]["type"], "text")
        self.assertIn(str(staged_files[0]), input_items[0]["text"])
        self.assertIn("spec.pdf", input_items[0]["text"])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_image_attachment_turn_includes_local_image_input(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=_PNG_1X1,
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请解释这张图", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual([item["type"] for item in input_items], ["text", "localImage"])
        self.assertTrue(input_items[1]["path"].endswith(".png"))
        self.assertIn(input_items[1]["path"], input_items[0]["text"])

    def test_spoofed_feishu_image_keeps_path_text_without_local_image(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=b"not-an-image",
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请检查这个文件", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual([item["type"] for item in input_items], ["text"])
        staged_path = str(next((workspace / "_feishu_attachments").iterdir()))
        self.assertIn(staged_path, input_items[0]["text"])

    def test_unknown_effective_model_keeps_feishu_image_as_path_only(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        original_record = handler._effective_settings.record_start_or_resume

        def _record_then_observe_malformed_settings(thread_id, *args, **kwargs):
            original_record(thread_id, *args, **kwargs)
            handler._effective_settings.observe_notification(
                "thread/settings/updated",
                {
                    "threadId": thread_id,
                    "threadSettings": {"model": "partial-only"},
                },
            )

        handler._effective_settings.record_start_or_resume = (
            _record_then_observe_malformed_settings
        )
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=_PNG_1X1,
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请解释这张图", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual([item["type"] for item in input_items], ["text"])
        self.assertIn("diagram.png", input_items[0]["text"])

    def test_text_only_effective_model_keeps_feishu_image_as_path_only(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        handler.handle_message("ou_user", "c1", "/model gpt-5.4")
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=_PNG_1X1,
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请解释这张图", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual([item["type"] for item in input_items], ["text"])

    def test_requested_model_mismatch_cannot_reuse_old_image_capability(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        handler.handle_message("ou_user", "c1", "/model gpt-5.4")
        original_create_thread = handler._adapter.create_thread

        def _create_with_mismatched_effective_model(**kwargs):
            snapshot = original_create_thread(**kwargs)
            # Simulate app-server normalizing/falling back to the old
            # image-capable model even though turn/start will request 5.4.
            snapshot.effective_model = "gpt-5.5"
            return snapshot

        handler._adapter.create_thread = _create_with_mismatched_effective_model
        bot.message_contexts["m-image"] = {"chat_type": "p2p", "message_type": "image"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-image", "image", "img-key")] = SimpleNamespace(
            content=_PNG_1X1,
            file_name="diagram.png",
            content_type="image/png",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-image", "image", "img-key", "")
        handler.handle_message("ou_user", "c1", "请解释这张图", message_id="m-text")

        input_items = handler._adapter.start_turn_calls[-1]["input_items"]
        self.assertEqual(handler._adapter.start_turn_calls[-1]["model"], "gpt-5.4")
        self.assertEqual([item["type"] for item in input_items], ["text"])

    def test_same_settings_ack_retains_existing_effective_model_evidence(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        stage_dir = workspace / "_feishu_attachments"
        stage_dir.mkdir(parents=True)
        staged_path = stage_dir / "diagram.png"
        staged_path.write_bytes(_PNG_1X1)
        handler, _bot = self._make_handler({"default_working_dir": str(workspace)})
        thread_id = "thread-settings-ack"
        candidate_items = [
            {"type": "text", "text": str(staged_path)},
            {
                "type": "_focusLocalImageCandidate",
                "path": str(staged_path),
                "expectedParent": str(stage_dir.resolve()),
            },
        ]
        handler._effective_settings.record_start_or_resume(
            thread_id,
            model="gpt-5.5",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            source="thread_resume",
        )

        # Current upstream suppresses the notification for an unchanged full
        # settings snapshot.  The ACK adds no evidence, but it also must not
        # erase the matching response-side fact already held by Focus.
        handler._thread_runtime_authority.update_thread_settings(
            thread_id,
            model="gpt-5.5",
        )
        ack_only = handler._file_message_domain.finalize_prompt_input(
            thread_id,
            "gpt-5.5",
            candidate_items,
        )
        self.assertEqual(
            [item["type"] for item in ack_only],
            ["text", "localImage"],
        )

        handler._effective_settings.observe_notification(
            "thread/settings/updated",
            {
                "threadId": thread_id,
                "threadSettings": {
                    "model": "gpt-5.5",
                    "effort": "high",
                    "approvalPolicy": "never",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        confirmed = handler._file_message_domain.finalize_prompt_input(
            thread_id,
            "gpt-5.5",
            candidate_items,
        )
        self.assertEqual(
            [item["type"] for item in confirmed],
            ["text", "localImage"],
        )

    def test_feishu_image_finalizer_rejects_symlink_at_consumption(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        stage_dir = workspace / "_feishu_attachments"
        stage_dir.mkdir(parents=True)
        outside_path = workspace / "outside.png"
        outside_path.write_bytes(_PNG_1X1)
        staged_path = stage_dir / "diagram.png"
        staged_path.symlink_to(outside_path)
        handler, _bot = self._make_handler({"default_working_dir": str(workspace)})
        thread_id = "thread-symlink"
        handler._effective_settings.record_start_or_resume(
            thread_id,
            model="gpt-5.5",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            source="thread_resume",
        )

        finalized = handler._file_message_domain.finalize_prompt_input(
            thread_id,
            "gpt-5.5",
            [
                {"type": "text", "text": str(staged_path)},
                {
                    "type": "_focusLocalImageCandidate",
                    "path": str(staged_path),
                    "expectedParent": str(stage_dir.resolve()),
                },
            ],
        )

        self.assertEqual([item["type"] for item in finalized], ["text"])

    def test_successful_unsubscribe_clears_effective_model_fact(self) -> None:
        handler, _bot = self._make_handler()
        thread_id = "thread-unsubscribe"
        handler._effective_settings.record_start_or_resume(
            thread_id,
            model="gpt-5.5",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":danger-full-access",
            source="thread_resume",
        )

        handler._thread_runtime_authority.unsubscribe_thread(thread_id)

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [thread_id])
        self.assertIsNone(handler._effective_settings.resolve_model_for_request(thread_id))

    def test_missing_staged_attachment_blocks_entire_attachment_batch(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file-1"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-file-2"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file-1", "file", "file-key-1")] = SimpleNamespace(
            content=b"one",
            file_name="one.txt",
            content_type="text/plain",
        )
        bot.downloaded_resources[("m-file-2", "file", "file-key-2")] = SimpleNamespace(
            content=b"two",
            file_name="two.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file-1", "file", "file-key-1", "one.txt")
        handler.handle_attachment_message("ou_user", "c1", "m-file-2", "file", "file-key-2", "two.txt")

        staged_files = sorted((workspace / "_feishu_attachments").iterdir())
        staged_files[0].unlink()

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("重新发送需要处理的全部附件", bot.replies[-1][1])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_workspace_mismatch_blocks_attachment_batch(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace-1"
        workspace_2 = pathlib.Path(tempdir.name) / "workspace-2"
        workspace.mkdir()
        workspace_2.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"one",
            file_name="one.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "one.txt")

        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            state["working_dir"] = str(workspace_2)

        handler.handle_message("ou_user", "c1", "请处理附件", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("属于其他工作目录", bot.replies[-1][1])
        self.assertEqual(handler._pending_attachment_store.list_all(), ())

    def test_group_attachment_pending_is_isolated_by_sender(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler({"default_working_dir": str(workspace)})
        bot.message_contexts["g-file"] = {"chat_type": "group", "message_type": "file", "sender_open_id": "ou_user"}
        bot.message_contexts["g-text-b"] = {"chat_type": "group", "message_type": "text", "sender_open_id": "ou_user2"}
        bot.message_contexts["g-text-a"] = {"chat_type": "group", "message_type": "text", "sender_open_id": "ou_user"}
        bot.downloaded_resources[("g-file", "file", "file-key")] = SimpleNamespace(
            content=b"group-file",
            file_name="group.txt",
            content_type="text/plain",
        )

        handler.handle_attachment_message("ou_user", "chat-group", "g-file", "file", "file-key", "group.txt")
        handler.handle_message("ou_user2", "chat-group", "普通提问", message_id="g-text-b")

        self.assertNotIn("group.txt", handler._adapter.start_turn_calls[-1]["text"])
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "turn/completed",
            {
                "threadId": "thread-created",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )

        handler.handle_message("ou_user", "chat-group", "请一起看附件", message_id="g-text-a")

        self.assertIn("group.txt", handler._adapter.start_turn_calls[-1]["text"])

    def test_expired_attachment_blocks_follow_up_prompt(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace = pathlib.Path(tempdir.name) / "workspace"
        workspace.mkdir()
        handler, bot = self._make_handler(
            {"default_working_dir": str(workspace), "attachment_ttl_seconds": 1}
        )
        bot.message_contexts["m-file"] = {"chat_type": "p2p", "message_type": "file"}
        bot.message_contexts["m-text"] = {"chat_type": "p2p", "message_type": "text"}
        bot.downloaded_resources[("m-file", "file", "file-key")] = SimpleNamespace(
            content=b"ttl",
            file_name="ttl.txt",
            content_type="text/plain",
        )

        with patch("bot.file_message_domain.time.time", return_value=10.0):
            handler.handle_attachment_message("ou_user", "c1", "m-file", "file", "file-key", "ttl.txt")
        with patch("bot.file_message_domain.time.time", return_value=20.0):
            handler.handle_message("ou_user", "c1", "还在吗", message_id="m-text")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIn("附件已过期", bot.replies[-1][1])
        attachment_dir = workspace / "_feishu_attachments"
        self.assertFalse(attachment_dir.exists() and any(attachment_dir.iterdir()))

    def test_unsupported_attachment_type_is_rejected_explicitly(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-folder"] = {"chat_type": "p2p", "message_type": "folder"}

        handler.handle_attachment_message("ou_user", "c1", "m-folder", "folder", "folder-key", "设计资料")

        self.assertIn("文件夹消息当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_merge_forward_attachment_type_is_rejected_with_specific_reason(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-forward"] = {"chat_type": "p2p", "message_type": "merge_forward"}

        handler.handle_attachment_message("ou_user", "c1", "m-forward", "merge_forward", "forward-key", "转发记录")

        self.assertIn("合并转发里的子附件当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_interactive_attachment_type_is_rejected_with_specific_reason(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["m-card"] = {"chat_type": "p2p", "message_type": "interactive"}

        handler.handle_attachment_message("ou_user", "c1", "m-card", "interactive", "card-key", "卡片资源")

        self.assertIn("卡片里的资源当前无法通过飞书 API 下载", bot.replies[-1][1])

    def test_permissions_command_applies_to_thread_creation_and_turn_start(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/permissions danger-full-access")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.create_thread_calls[-1]["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["approval_policy"], "never")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["permissions_profile_id"], ":danger-full-access")

    def test_model_command_applies_to_thread_creation_and_turn_start(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "/model gpt-5.5")
        handler.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler._adapter.create_thread_calls[-1]["model"], "gpt-5.5")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["model"], "gpt-5.5")
