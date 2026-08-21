from unittest.mock import Mock, patch

from bot.codex_protocol.client import CodexRpcTransportError
from bot.stores.interaction_lease_store import make_fcodex_interaction_holder
from bot.web_runtime.projection import project_turns
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
    _PNG_1X1,
    _WAV_1X1,
)


class WebRuntimeThreadCreateAttachmentTests(WebRuntimeControllerHarness):
    def test_new_thread_starts_first_prompt_without_main_turn_lease(self):
        with patch.object(
            self.operations,
            "acquire_exclusive_turn_submission",
            side_effect=AssertionError("ordinary prompt must not acquire a lease"),
        ) as acquire_exclusive:
            result = self.controller.start_thread(
                "tab-1", text="hello", cwd=str(self.workspace)
            )

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["turn_id"], "")
        self.assertNotIn("owner", result)
        self.assertEqual(self.fake.created[0]["cwd"], str(self.workspace))
        self.assertEqual(self.fake.created[0]["model"], "gpt-test")
        self.assertEqual(
            self.fake.created[0]["config_overrides"],
            {"model_reasoning_effort": "high"},
        )
        self.assertEqual(self.fake.created[0]["approval_policy"], "never")
        self.assertEqual(
            self.fake.created[0]["permissions_profile_id"],
            ":danger-full-access",
        )
        self.assertEqual(self.fake.started[0]["thread_id"], "thread-1")
        self.assertEqual(self.fake.started[0]["model"], "gpt-test")
        self.assertEqual(self.fake.started[0]["reasoning_effort"], "high")
        self.assertEqual(self.fake.started[0]["approval_policy"], "never")
        self.assertEqual(
            self.fake.started[0]["permissions_profile_id"],
            ":danger-full-access",
        )
        acquire_exclusive.assert_not_called()
        self.assertIsNone(self.store.load("thread-1"))

    def test_new_thread_first_prompt_preserves_foreign_blank_lease(self):
        acquired = self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:foreign", owner_pid=0),
        )
        self.assertIsNotNone(acquired.lease)

        result = self.controller.start_thread("tab-1", text="hello")

        self.assertTrue(result["accepted"])
        self.assertEqual(self.store.load("thread-1"), acquired.lease)
        self.assertEqual(len(self.fake.started), 1)

    def test_new_thread_first_prompt_preserves_foreign_active_lease(self):
        acquired = self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:foreign", owner_pid=0),
        )
        self.assertIsNotNone(acquired.lease)
        assert acquired.lease is not None
        active = self.store.activate_turn(acquired.lease, "turn-foreign")
        self.assertIsNotNone(active)

        result = self.controller.start_thread("tab-1", text="hello")

        self.assertTrue(result["accepted"])
        self.assertEqual(self.store.load("thread-1"), active)
        self.assertEqual(len(self.fake.started), 1)

    def test_create_and_first_turn_share_one_immutable_settings_snapshot(self):
        original_create_thread = self.fake.create_thread
        settings_snapshot = Mock(
            side_effect=self.controller._thread_create._next_turn_settings
        )
        self.controller._thread_create._next_turn_settings = settings_snapshot

        def create_then_update_settings(**kwargs):
            created = original_create_thread(**kwargs)
            self.next_turn_settings_store.update(
                {
                    "model": "gpt-small",
                    "reasoning_effort": "low",
                    "approval_policy": "on-request",
                    "permissions_profile_id": ":workspace",
                }
            )
            return created

        self.fake.create_thread = create_then_update_settings

        self.controller.start_thread("tab-1", text="hello")

        settings_snapshot.assert_called_once_with()
        self.assertEqual(self.next_turn_settings_store.load().model, "gpt-small")
        self.assertEqual(self.fake.created[0]["model"], "gpt-test")
        self.assertEqual(
            self.fake.created[0]["config_overrides"],
            {"model_reasoning_effort": "high"},
        )
        self.assertEqual(self.fake.created[0]["approval_policy"], "never")
        self.assertEqual(
            self.fake.created[0]["permissions_profile_id"],
            ":danger-full-access",
        )
        self.assertEqual(self.fake.started[0]["model"], "gpt-test")
        self.assertEqual(self.fake.started[0]["reasoning_effort"], "high")
        self.assertEqual(self.fake.started[0]["approval_policy"], "never")
        self.assertEqual(
            self.fake.started[0]["permissions_profile_id"],
            ":danger-full-access",
        )

    def test_attachment_upload_is_draft_only_until_prompt_submission(self):
        workspace = self.workspace

        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="diagram.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        self.assertEqual(self.fake.started, [])
        result = self.controller.start_thread(
            "tab-1",
            text="inspect this",
            cwd=str(workspace),
            attachment_ids=[upload["file_id"]],
        )

        self.assertEqual(result["mode"], "started")
        input_items = self.fake.started[0]["input_items"]
        self.assertIn("[[focus.attachments.v1]]", input_items[0]["text"])
        self.assertIn("inspect this", input_items[0]["text"])
        self.assertIn('"delivery":"native_local_image"', input_items[0]["text"])
        self.assertEqual(input_items[1]["type"], "localImage")
        record = self.attachment_store.download(attachment_id=upload["file_id"])
        self.assertEqual(input_items[1]["path"], record.record.local_path)
        self.assertTrue(record.record.submitted)
        self.assertEqual(record.record.scope_key, "thread:thread-1")

    def test_audio_attachment_is_manifest_file_not_native_audio_input(self):
        workspace = self.workspace

        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="note.wav",
            media_type="audio/wav",
            content=_WAV_1X1,
        )
        self.controller.start_thread(
            "tab-1",
            text="Please inspect this audio attachment.",
            cwd=str(workspace),
            attachment_ids=[upload["file_id"]],
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(len(input_items), 1)
        self.assertEqual(input_items[0]["type"], "text")
        self.assertIn('"media_type":"audio/wav"', input_items[0]["text"])
        self.assertIn('"delivery":"same_host_path"', input_items[0]["text"])
        self.assertIn("Focus-managed file", input_items[0]["text"])
        self.assertNotIn('"type": "audio"', input_items[0]["text"])

    def test_text_only_model_keeps_image_as_same_host_manifest_file(self):
        self.fake.effective_model = "gpt-small"
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="diagram.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        self.controller.start_thread(
            "tab-1",
            text="inspect this",
            cwd=str(self.workspace),
            attachment_ids=[upload["file_id"]],
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(len(input_items), 1)
        self.assertIn('"kind":"image"', input_items[0]["text"])
        self.assertIn('"delivery":"same_host_path"', input_items[0]["text"])
        self.assertNotIn('"delivery":"native_local_image"', input_items[0]["text"])

    def test_unknown_model_metadata_disables_native_image_input(self):
        self.fake.effective_model = "future-model-not-in-catalog"
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="diagram.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        self.controller.start_thread(
            "tab-1",
            text="inspect this",
            cwd=str(self.workspace),
            attachment_ids=[upload["file_id"]],
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(len(input_items), 1)
        self.assertIn('"delivery":"same_host_path"', input_items[0]["text"])
        self.assertNotIn('"delivery":"native_local_image"', input_items[0]["text"])

    def test_requested_model_mismatch_cannot_reuse_old_web_image_capability(self):
        self.controller.update_next_turn_settings(
            "tab-1",
            {"model": "gpt-small", "reasoning_effort": "low"},
        )
        self.fake.effective_model = "gpt-test"
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="diagram.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        self.controller.start_thread(
            "tab-1",
            text="inspect this",
            cwd=str(self.workspace),
            attachment_ids=[upload["file_id"]],
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(self.fake.started[0]["model"], "gpt-small")
        self.assertEqual(len(input_items), 1)
        self.assertIn('"delivery":"same_host_path"', input_items[0]["text"])
        self.assertNotIn('"delivery":"native_local_image"', input_items[0]["text"])

    def test_literal_attachment_prefix_in_user_text_round_trips(self):
        literal = "[[focus.attachments.v1]]\nthis is ordinary user text"

        self.controller.start_thread(
            "tab-1",
            text=literal,
            cwd=str(self.workspace),
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(len(input_items), 1)
        self.assertIn("[[focus.user_request]]\n" + literal, input_items[0]["text"])
        projected = project_turns(
            [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {
                            "id": "user-1",
                            "type": "userMessage",
                            "content": input_items,
                        }
                    ],
                }
            ]
        )
        self.assertEqual(projected[0]["text"], literal)

    def test_video_attachment_is_an_opaque_manifest_file_without_web_download(self):
        workspace = self.workspace

        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="clip.mp4",
            media_type="video/mp4",
            content=b"not-a-native-video-input",
        )
        self.assertEqual(upload["url"], "")
        self.controller.start_thread(
            "tab-1",
            text="Inspect this video attachment with your tools.",
            cwd=str(workspace),
            attachment_ids=[upload["file_id"]],
        )

        input_items = self.fake.started[0]["input_items"]
        self.assertEqual(len(input_items), 1)
        self.assertIn('"kind":"file"', input_items[0]["text"])
        self.assertIn('"media_type":"video/mp4"', input_items[0]["text"])
        self.assertIn('"delivery":"same_host_path"', input_items[0]["text"])
        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.attachment_download(upload["file_id"])
        self.assertEqual(caught.exception.code, "attachment_preview_unavailable")

    def test_spoofed_native_attachment_is_rejected_before_any_turn_starts(self):
        workspace = self.workspace

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.stage_attachment(
                "tab-1",
                cwd=str(workspace),
                display_name="spoofed.png",
                media_type="image/png",
                content=b"not an image",
            )

        self.assertEqual(caught.exception.code, "invalid_attachment")
        self.assertEqual(self.fake.started, [])

    def test_failed_prompt_restores_staged_attachment_for_retry(self):
        workspace = self.workspace
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        self.fake.start_error = RuntimeError("turn failed")

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.start_thread(
                "tab-1",
                text="read it",
                cwd=str(workspace),
                attachment_ids=[upload["file_id"]],
            )
        self.assertEqual(caught.exception.code, "thread_created_turn_not_started")
        self.assertEqual(caught.exception.details["thread_id"], "thread-1")
        self.assertEqual(
            caught.exception.details["attachment_disposition"],
            "restored",
        )

        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].submitted)
        self.assertEqual(pending[0].scope_key, "thread:thread-1")
        self.assertIsNone(self.store.load("thread-1"))

    def test_attachment_rollback_failure_does_not_create_main_turn_lease(self):
        workspace = self.workspace
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        original_mark_submitted = self.attachment_store.mark_submitted

        def fail_rollback(attachment_ids, *, submitted, scope_key=None, now=None):
            if not submitted:
                raise RuntimeError("metadata write failed")
            return original_mark_submitted(
                attachment_ids,
                submitted=submitted,
                scope_key=scope_key,
                now=now,
            )

        self.attachment_store.mark_submitted = fail_rollback
        self.fake.start_error = RuntimeError("turn failed")

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.start_thread(
                "tab-1",
                text="read it",
                cwd=str(workspace),
                attachment_ids=[upload["file_id"]],
            )

        self.assertEqual(caught.exception.code, "thread_created_turn_not_started")
        self.assertEqual(
            caught.exception.details["attachment_disposition"],
            "reupload_required",
        )
        self.assertIn("upload them again", str(caught.exception))
        self.assertIsNone(self.store.load("thread-1"))
        record = self.attachment_store.download(attachment_id=upload["file_id"])
        self.assertTrue(record.record.submitted)
        self.controller.client_disconnected("tab-1")
        self.assertEqual(self.fake.released, ["thread-1"])

    def test_unknown_first_prompt_is_not_replayed_or_wrapped_in_a_lease(self):
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        start_turn = Mock(
            side_effect=CodexRpcTransportError(
                "turn/start",
                {"code": -32000, "message": "connection lost"},
            )
        )
        self.fake.start_turn = start_turn

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.start_thread(
                "tab-1",
                text="read it",
                cwd=str(self.workspace),
                attachment_ids=[upload["file_id"]],
            )

        self.assertEqual(caught.exception.code, "turn_submission_unknown")
        self.assertEqual(caught.exception.details["thread_id"], "thread-1")
        self.assertEqual(len(self.fake.created), 1)
        start_turn.assert_called_once()
        self.assertIsNone(self.store.load("thread-1"))
        record = self.attachment_store.download(attachment_id=upload["file_id"])
        self.assertTrue(record.record.submitted)
        self.assertEqual(record.record.scope_key, "thread:thread-1")

    def test_create_model_fact_failure_is_local_error_without_global_fence(self):
        self.effective_settings.record_start_or_resume = Mock(
            side_effect=RuntimeError("effective settings registry failed")
        )

        with self.assertRaises(WebRuntimeError) as raised:
            self.controller.start_thread(
                "tab-1",
                text="hello",
                cwd=str(self.workspace),
            )

        self.assertEqual(
            raised.exception.code,
            "thread_create_local_commit_failed",
        )
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(raised.exception.details["stage"], "effective_settings")
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])

    def test_committed_create_projection_failure_does_not_block_first_turn(self):
        self.remember_direct_thread_summary_hook = lambda _summary: (
            _ for _ in ()
        ).throw(RuntimeError("cache failed"))

        with self.assertLogs(
            "bot.web_runtime.thread_create_coordinator", level="ERROR"
        ):
            result = self.controller.start_thread(
                "tab-1",
                text="hello",
                cwd=str(self.workspace),
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fake.started), 1)
        self.assertIsNone(self.store.load("thread-1"))

    def test_prompt_preparation_failure_leaves_no_main_turn_lease_for_retry(self):
        original_prompt_input_items = self.controller._workspace.prompt_input_items
        first_call = True

        def fail_first_preparation(*args, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                raise RuntimeError("input preparation failed")
            return original_prompt_input_items(*args, **kwargs)

        self.controller._workspace.prompt_input_items = fail_first_preparation

        with self.assertLogs(
            "bot.web_runtime.thread_create_coordinator", level="ERROR"
        ):
            with self.assertRaises(WebRuntimeError) as raised:
                self.controller.start_thread(
                    "tab-1",
                    text="hello",
                    cwd=str(self.workspace),
                )

        self.assertEqual(raised.exception.code, "thread_created_turn_not_started")
        self.assertEqual(raised.exception.details["thread_id"], "thread-1")
        self.assertEqual(
            raised.exception.details["attachment_disposition"],
            "restored",
        )
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(self.fake.started, [])
        self.assertIsNone(self.store.load("thread-1"))

        retried = self.submit_web_prompt_with_started_notification(
            "tab-1",
            "thread-1",
            text="hello",
        )

        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual(retried["mode"], "start")
        self.assertEqual(retried["thread_id"], "thread-1")
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fake.started), 1)

    def test_prompt_preparation_failure_moves_draft_attachments_to_created_thread(self):
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        attachment_id = upload["file_id"]
        original_prompt_input_items = self.controller._workspace.prompt_input_items
        first_call = True

        def fail_first_preparation(*args, **kwargs):
            nonlocal first_call
            if first_call:
                first_call = False
                raise RuntimeError("input preparation failed")
            return original_prompt_input_items(*args, **kwargs)

        self.controller._workspace.prompt_input_items = fail_first_preparation

        with self.assertLogs(
            "bot.web_runtime.thread_create_coordinator", level="ERROR"
        ):
            with self.assertRaises(WebRuntimeError):
                self.controller.start_thread(
                    "tab-1",
                    text="read it",
                    cwd=str(self.workspace),
                    attachment_ids=[attachment_id],
                )

        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].submitted)

        retried = self.submit_web_prompt_with_started_notification(
            "tab-1",
            "thread-1",
            text="read it",
            attachment_ids=[attachment_id],
        )

        self.assertEqual(retried["status"], "succeeded")
        self.assertEqual(retried["mode"], "start")
        self.assertEqual(len(self.fake.created), 1)
        self.assertEqual(len(self.fake.started), 1)
        self.assertIn("notes.txt", self.fake.started[0]["input_items"][0]["text"])

    def test_attachment_cannot_cross_from_workspace_draft_to_thread(self):
        workspace = self.workspace
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(workspace),
            display_name="notes.txt",
            media_type="text/plain",
            content=b"notes",
        )
        self.controller.read_thread("tab-1", "thread-1")

        result = self.submit_web_prompt(
            "tab-1",
            "thread-1",
            text="read it",
            attachment_ids=[upload["file_id"]],
        )

        self.assertEqual(result["status"], "known_no_effect")
        self.assertEqual(result["reason_code"], "invalid_attachment")
        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].submitted)
