import pathlib
from unittest.mock import patch

from bot.adapters.base import (
    ThreadSummary,
)
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
    _PNG_1X1,
)


class WebRuntimeWorkspaceProfileTests(WebRuntimeControllerHarness):
    def test_meta_projects_navigation_and_next_turn_settings_separately(self):
        meta = self.controller.meta("tab-1")

        self.assertEqual(meta["web_display_name"], "Focus Web")
        self.assertEqual(
            meta["writer_profile"],
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
                "scope_generation": 1,
            },
        )
        self.assertEqual(
            meta["next_turn_settings"],
            {
                "generation": 1,
                "model": "gpt-test",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )

    def test_next_turn_settings_have_an_independent_read_surface(self):
        result = self.controller.next_turn_settings()

        self.assertEqual(
            result["next_turn_settings"],
            {
                "generation": 1,
                "model": "gpt-test",
                "reasoning_effort": "high",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
            },
        )
        self.assertNotIn("writer_profile", result)

    def test_writer_profile_rejects_setting_fields_without_global_mutation(self):
        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.update_profile("tab-1", {"model": "gpt-small"})

        self.assertEqual(caught.exception.code, "invalid_profile")
        self.assertEqual(self.next_turn_settings_store.load().generation, 1)

    def test_settings_update_uses_dedicated_instance_wide_surface(self):
        result = self.controller.update_next_turn_settings(
            "tab-1",
            {"model": "gpt-small", "reasoning_effort": "low"},
        )

        self.assertEqual(result["next_turn_settings"]["generation"], 2)
        self.assertEqual(result["next_turn_settings"]["model"], "gpt-small")
        self.assertEqual(
            self.controller.next_turn_settings()["next_turn_settings"],
            result["next_turn_settings"],
        )
        self.assertEqual(
            set(self.controller.meta("tab-1")["writer_profile"]),
            {"selected_thread_id", "working_dir", "scope_generation"},
        )

    def test_same_host_literal_workspace_becomes_the_next_new_thread_cwd(self):
        literal = pathlib.Path(self.temp_dir.name) / "literal" / "nested"
        literal.mkdir(parents=True)
        spelling_with_parent = literal / ".." / "nested"

        profile = self.controller.update_profile(
            "tab-1",
            {"working_dir": str(spelling_with_parent)},
        )
        result = self.controller.start_thread("tab-1", text="hello")

        self.assertEqual(profile["writer_profile"]["working_dir"], str(literal))
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(self.fake.created[-1]["cwd"], str(literal))

    def test_workspace_switch_invalidates_old_draft_attachments(self):
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        replacement = pathlib.Path(self.temp_dir.name) / "replacement-workspace"
        replacement.mkdir()

        result = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(replacement),
            },
        )

        self.assertEqual(result["attachment_scope_disposition"], "invalidated")
        self.assertEqual(result["current_attachment_scope"], f"draft:{replacement}")
        self.assertEqual(result["invalidated_attachment_count"], 1)
        self.assertEqual(result["rebound_attachment_count"], 0)
        self.assertEqual(
            result["writer_profile"]["working_dir"],
            str(replacement),
        )
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            self.attachment_store.resolve_pending(
                client_id="tab-1",
                scope_key=f"draft:{self.workspace}",
                attachment_ids=[upload["file_id"]],
            )

    def test_selected_thread_to_same_cwd_draft_rebinds_thread_attachments(self):
        self.controller.read_thread("tab-1", "thread-1")
        old_generation = self.controller.meta("tab-1")["writer_profile"][
            "scope_generation"
        ]
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            display_name="thread-draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        result = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
            },
        )

        self.assertTrue(result["scope_changed"])
        self.assertEqual(result["previous_attachment_scope"], "thread:thread-1")
        self.assertEqual(result["current_attachment_scope"], f"draft:{self.workspace}")
        self.assertEqual(result["previous_scope_generation"], old_generation)
        self.assertEqual(
            result["current_scope_generation"],
            result["writer_profile"]["scope_generation"],
        )
        self.assertEqual(result["attachment_scope_disposition"], "rebound")
        self.assertEqual(result["invalidated_attachment_count"], 0)
        self.assertEqual(result["rebound_attachment_count"], 1)
        rebound = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(
            [record.attachment_id for record in rebound], [upload["file_id"]]
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.stage_attachment(
                "tab-1",
                thread_id="thread-1",
                scope_generation=old_generation,
                display_name="late.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(caught.exception.code, "stale_attachment_scope")
        self.assertGreater(result["writer_profile"]["scope_generation"], old_generation)

    def test_thread_selection_advances_scope_generation_and_closes_aba_upload(self):
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-2",
                cwd=str(self.workspace),
                name="Second",
                preview="",
                created_at=2,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        ]

        first = self.controller.read_thread("tab-1", "thread-1")
        first_scope = first["selection_scope"]
        self.assertTrue(first_scope["scope_changed"])
        self.assertEqual(
            first_scope["previous_attachment_scope"], f"draft:{self.workspace}"
        )
        self.assertEqual(first_scope["current_attachment_scope"], "thread:thread-1")
        self.assertEqual(first_scope["attachment_scope_disposition"], "isolated")
        generation_a = first_scope["current_scope_generation"]

        stable = self.controller.read_thread("tab-1", "thread-1")["selection_scope"]
        self.assertFalse(stable["scope_changed"])
        self.assertEqual(stable["previous_attachment_scope"], "")
        self.assertEqual(stable["current_scope_generation"], generation_a)

        completed = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            scope_generation=generation_a,
            display_name="kept.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        selected_b = self.controller.read_thread("tab-1", "thread-2")["selection_scope"]
        selected_a_again = self.controller.read_thread("tab-1", "thread-1")[
            "selection_scope"
        ]

        self.assertEqual(selected_b["previous_attachment_scope"], "thread:thread-1")
        self.assertEqual(selected_b["current_attachment_scope"], "thread:thread-2")
        self.assertEqual(
            selected_b["current_scope_generation"],
            generation_a + 1,
        )
        self.assertEqual(
            selected_a_again["current_scope_generation"],
            generation_a + 2,
        )
        self.assertEqual(
            selected_a_again["writer_profile"]["scope_generation"],
            generation_a + 2,
        )
        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.stage_attachment(
                "tab-1",
                thread_id="thread-1",
                scope_generation=generation_a,
                display_name="late.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(caught.exception.code, "stale_attachment_scope")
        # Selection keeps completed records isolated by thread scope.
        resolved = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[completed["file_id"]],
        )
        self.assertEqual(
            [record.attachment_id for record in resolved], [completed["file_id"]]
        )

    def test_cold_selected_profile_reads_authoritative_cwd_before_rebind(self):
        self.profile_store.update(
            "tab-1",
            selected_thread_id="thread-1",
            working_dir=str(self.workspace),
        )
        self.document_registry.forget_materialized_thread_if_matches(
            "tab-1", "thread-1"
        )
        self.controller._thread_read_model.forget_closed_thread("thread-1")
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            display_name="cold-thread.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        self.controller._thread_read_model.forget_closed_thread("thread-1")
        reads_before = len(self.fake.reads)

        result = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
            },
        )

        self.assertEqual(result["attachment_scope_disposition"], "rebound")
        self.assertGreater(len(self.fake.reads), reads_before)
        rebound = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(
            [record.attachment_id for record in rebound], [upload["file_id"]]
        )

    def test_same_draft_cwd_is_not_a_scope_change_and_preserves_attachments(self):
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        result = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
            },
        )

        self.assertFalse(result["scope_changed"])
        self.assertEqual(result["attachment_scope_disposition"], "unchanged")
        self.assertEqual(result["previous_attachment_scope"], "")
        self.assertEqual(result["current_attachment_scope"], f"draft:{self.workspace}")
        self.assertEqual(result["invalidated_attachment_count"], 0)
        self.assertEqual(result["rebound_attachment_count"], 0)
        resolved = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(
            [record.attachment_id for record in resolved], [upload["file_id"]]
        )

    def test_stale_inflight_upload_cannot_recreate_superseded_draft_scope(self):
        old_generation = self.controller.meta("tab-1")["writer_profile"][
            "scope_generation"
        ]
        replacement = pathlib.Path(self.temp_dir.name) / "replacement-workspace"
        replacement.mkdir()
        switched = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(replacement),
            },
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.stage_attachment(
                "tab-1",
                cwd=str(self.workspace),
                scope_generation=old_generation,
                display_name="late.png",
                media_type="image/png",
                content=_PNG_1X1,
            )

        self.assertEqual(caught.exception.code, "stale_attachment_scope")
        self.assertGreater(
            switched["writer_profile"]["scope_generation"],
            old_generation,
        )

    def test_working_dir_change_cannot_leave_selected_thread_visible(self):
        self.controller.read_thread("tab-1", "thread-1")
        replacement = pathlib.Path(self.temp_dir.name) / "replacement-workspace"
        replacement.mkdir()

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.update_profile(
                "tab-1",
                {"working_dir": str(replacement)},
            )

        self.assertEqual(caught.exception.code, "invalid_profile")
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )

    def test_failed_profile_write_preserves_current_scope_attachments(self):
        upload = self.controller.stage_attachment(
            "tab-1",
            cwd=str(self.workspace),
            display_name="draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        replacement = pathlib.Path(self.temp_dir.name) / "replacement-workspace"
        replacement.mkdir()

        with patch.object(
            self.profile_store,
            "update",
            side_effect=OSError("profile write failed"),
        ):
            with self.assertRaisesRegex(OSError, "profile write failed"):
                self.controller.update_profile(
                    "tab-1",
                    {
                        "selected_thread_id": "",
                        "working_dir": str(replacement),
                    },
                )

        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual([item.attachment_id for item in pending], [upload["file_id"]])

    def test_same_cwd_rebind_rolls_back_when_profile_write_fails(self):
        self.controller.read_thread("tab-1", "thread-1")
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            display_name="thread-draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        with patch.object(
            self.profile_store,
            "update",
            side_effect=OSError("profile write failed"),
        ):
            with self.assertRaisesRegex(OSError, "profile write failed"):
                self.controller.update_profile(
                    "tab-1",
                    {
                        "selected_thread_id": "",
                        "working_dir": str(self.workspace),
                    },
                )

        pending = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual([item.attachment_id for item in pending], [upload["file_id"]])
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )

    def test_rebind_and_profile_double_failure_is_explicitly_fail_closed(self):
        self.controller.read_thread("tab-1", "thread-1")
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            display_name="thread-draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        original_rebind = self.attachment_store.rebind_pending_scope

        def fail_only_rollback(**kwargs):
            if kwargs["source_scope_key"].startswith("draft:"):
                raise OSError("rollback metadata write failed")
            return original_rebind(**kwargs)

        with (
            patch.object(
                self.profile_store,
                "update",
                side_effect=OSError("profile write failed"),
            ),
            patch.object(
                self.attachment_store,
                "rebind_pending_scope",
                side_effect=fail_only_rollback,
            ),
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.controller.update_profile(
                    "tab-1",
                    {
                        "selected_thread_id": "",
                        "working_dir": str(self.workspace),
                    },
                )

        self.assertEqual(caught.exception.code, "attachment_scope_rebind_unknown")
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "thread-1",
        )
        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.attachment_store.resolve_pending(
                client_id="tab-1",
                scope_key="thread:thread-1",
                attachment_ids=[upload["file_id"]],
            )
        stranded = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual(
            [record.attachment_id for record in stranded], [upload["file_id"]]
        )

    def test_workspace_switch_after_new_thread_preserves_no_lease_contract(self):
        self.controller.start_thread(
            "tab-1",
            text="keep this visible",
            cwd=str(self.workspace),
        )
        replacement = pathlib.Path(self.temp_dir.name) / "replacement-workspace"
        replacement.mkdir()

        updated = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(replacement),
            },
        )

        self.assertTrue(updated["scope_changed"])
        profile = self.profile_store.load("tab-1")
        self.assertEqual(profile.selected_thread_id, "")
        self.assertEqual(profile.working_dir, str(replacement))
        self.assertIsNone(self.store.load("thread-1"))

    def test_same_host_literal_workspace_rejects_a_missing_path(self):
        missing = pathlib.Path(self.temp_dir.name) / "missing"

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.update_profile("tab-1", {"working_dir": str(missing)})

        self.assertEqual(caught.exception.code, "invalid_cwd")
        self.assertIn("does not exist", str(caught.exception))

    def test_same_host_literal_workspace_rejects_a_non_directory(self):
        regular_file = pathlib.Path(self.temp_dir.name) / "regular-file"
        regular_file.touch()

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.update_profile("tab-1", {"working_dir": str(regular_file)})

        self.assertEqual(caught.exception.code, "invalid_cwd")
        self.assertIn("not a directory", str(caught.exception))

    def test_same_host_literal_workspace_rejects_resolution_failure(self):
        with patch.object(pathlib.Path, "resolve", side_effect=OSError("unresolvable")):
            with self.assertRaises(WebRuntimeError) as caught:
                self.controller.update_profile(
                    "tab-1", {"working_dir": "/unresolvable"}
                )

        self.assertEqual(caught.exception.code, "invalid_cwd")
        self.assertIn("resolved safely", str(caught.exception))

    def test_known_thread_workspace_can_be_selected_for_the_next_new_thread(self):
        known = pathlib.Path(self.temp_dir.name) / "known-thread-workspace"
        known.mkdir()
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-known",
                cwd=str(known),
                name="Known workspace",
                preview="",
                created_at=2,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        ]

        self.controller.list_threads(client_id="tab-1")
        profile = self.controller.update_profile("tab-1", {"working_dir": str(known)})
        result = self.controller.start_thread("tab-1", text="hello", cwd=str(known))

        self.assertEqual(profile["writer_profile"]["working_dir"], str(known))
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(self.fake.created[-1]["cwd"], str(known))

    def test_known_workspace_must_still_be_a_directory_when_a_new_thread_starts(self):
        known = pathlib.Path(self.temp_dir.name) / "removed-workspace"
        known.mkdir()
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-known",
                cwd=str(known),
                name="Known workspace",
                preview="",
                created_at=2,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        ]
        self.controller.list_threads(client_id="tab-1")
        known.rmdir()

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.start_thread("tab-1", text="hello", cwd=str(known))

        self.assertEqual(caught.exception.code, "invalid_cwd")
        self.assertEqual(self.fake.created, [])

    def test_next_turn_model_change_preserves_and_validates_existing_effort(self):
        self.controller.update_next_turn_settings(
            "tab-1",
            {"model": "gpt-test", "reasoning_effort": "high"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.update_next_turn_settings(
                "tab-1",
                {"model": "gpt-small"},
            )

        self.assertEqual(caught.exception.code, "invalid_effort")
        settings = self.controller.next_turn_settings()["next_turn_settings"]
        self.assertEqual(settings["model"], "gpt-test")
        self.assertEqual(settings["reasoning_effort"], "high")

    def test_next_turn_auto_model_accepts_explicit_effort_override(self):
        result = self.controller.update_next_turn_settings(
            "tab-1",
            {"model": "", "reasoning_effort": "ultra"},
        )

        self.assertEqual(result["next_turn_settings"]["model"], "")
        self.assertEqual(
            result["next_turn_settings"]["reasoning_effort"],
            "ultra",
        )

    def test_updated_next_turn_settings_apply_to_new_thread_and_first_turn(self):
        self.controller.update_next_turn_settings(
            "tab-1",
            {
                "model": "gpt-small",
                "reasoning_effort": "low",
                "approval_policy": "on-request",
                "permissions_profile_id": ":workspace",
            },
        )

        self.controller.start_thread("tab-1", text="hello")

        self.assertEqual(self.fake.created[-1]["model"], "gpt-small")
        self.assertEqual(
            self.fake.created[-1]["config_overrides"],
            {"model_reasoning_effort": "low"},
        )
        self.assertEqual(self.fake.created[-1]["approval_policy"], "on-request")
        self.assertEqual(
            self.fake.created[-1]["permissions_profile_id"],
            ":workspace",
        )
        self.assertEqual(self.fake.started[-1]["model"], "gpt-small")
        self.assertEqual(self.fake.started[-1]["reasoning_effort"], "low")
        self.assertEqual(self.fake.started[-1]["approval_policy"], "on-request")
        self.assertEqual(
            self.fake.started[-1]["permissions_profile_id"],
            ":workspace",
        )
