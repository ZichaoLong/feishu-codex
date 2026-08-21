import pathlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from bot.adapters.base import ThreadSummary
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.stores.web_attachment_store import WebAttachmentStore
from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.selection_coordinator import WebSelectionCoordinator
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.writer_workspace_coordinator import (
    WebWriterWorkspaceCoordinator,
    WebWriterWorkspacePorts,
)
from tests.web_runtime.fakes import _FakeRuntime


_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


class WebWriterWorkspaceCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.data_dir = pathlib.Path(self.temp_dir.name)
        self.workspace = self.data_dir / "workspace"
        self.workspace.mkdir()
        self.fake = _FakeRuntime()
        self.fake.cwd = str(self.workspace)
        self.profiles = WebWriterProfileStore(self.data_dir)
        self.attachments = WebAttachmentStore(self.data_dir, ttl_seconds=300)
        self.documents = WebDocumentRegistry(runtime_context_guard=lambda: None)
        self.documents.mark_connected("tab-1")
        self.runtime_interest = WebRuntimeInterestRegistry()
        self.selection = WebSelectionCoordinator(
            profile_store=self.profiles,
            document_registry=self.documents,
            runtime_interest=self.runtime_interest,
        )
        self.projection = FocusWebProjection()
        self.coordinator = WebWriterWorkspaceCoordinator(
            profile_store=self.profiles,
            attachment_store=self.attachments,
            documents=self.documents,
            selection=self.selection,
            read_model=WebThreadReadModel(),
            effective_settings=ThreadEffectiveSettingsRegistry(),
            projection=self.projection,
            ports=WebWriterWorkspacePorts(
                list_models=self.fake.list_models,
                read_thread=self.fake.read_thread,
            ),
            runtime_context_guard=lambda: None,
            default_working_dir=str(self.workspace),
        )

    def _build_mock_coordinator(self, guard):
        dependencies = {
            "profiles": Mock(name="profiles"),
            "attachments": Mock(name="attachments"),
            "documents": Mock(name="documents"),
            "selection": Mock(name="selection"),
            "read_model": Mock(name="read_model"),
            "effective_settings": Mock(name="effective_settings"),
            "projection": Mock(name="projection"),
            "list_models": Mock(name="list_models"),
            "read_thread": Mock(name="read_thread"),
        }
        coordinator = WebWriterWorkspaceCoordinator(
            profile_store=dependencies["profiles"],
            attachment_store=dependencies["attachments"],
            documents=dependencies["documents"],
            selection=dependencies["selection"],
            read_model=dependencies["read_model"],
            effective_settings=dependencies["effective_settings"],
            projection=dependencies["projection"],
            ports=WebWriterWorkspacePorts(
                list_models=dependencies["list_models"],
                read_thread=dependencies["read_thread"],
            ),
            runtime_context_guard=guard,
            default_working_dir=str(self.workspace),
        )
        return coordinator, dependencies

    def test_requires_callable_runtime_context_guard(self):
        with self.assertRaisesRegex(TypeError, "RuntimeLoop context guard"):
            self._build_mock_coordinator(None)

    def test_runtime_guard_rejects_before_any_owner_dependency_access(self):
        commands = {
            "default cwd": lambda owner: owner.default_working_dir,
            "catalog": lambda owner: owner.read_catalog_profile("tab-1"),
            "profile update": lambda owner: owner.update_profile("tab-1", {}),
            "select": lambda owner: owner.select_thread("tab-1", "thread-1"),
            "clear materialization": lambda owner: owner.materialize_cleared_unusable_thread(
                "thread-1", (), reason="test"
            ),
            "stage": lambda owner: owner.stage_attachment(
                "tab-1",
                cwd=str(self.workspace),
                display_name="draft.txt",
                media_type="text/plain",
                content=b"draft",
            ),
            "download": lambda owner: owner.attachment_download("attachment-1"),
            "scope": lambda owner: owner.attachment_scope(
                "tab-1", cwd=str(self.workspace)
            ),
            "scope CAS": lambda owner: owner.require_current_attachment_scope(
                "tab-1",
                thread_id="",
                cwd=str(self.workspace),
                scope_generation=1,
            ),
            "resolve": lambda owner: owner.resolve_attachments(
                "tab-1",
                scope_key=f"draft:{self.workspace}",
                attachment_ids=["attachment-1"],
            ),
            "input": lambda owner: owner.prompt_input_items(
                "hello", (), thread_id="thread-1"
            ),
            "mark": lambda owner: owner.set_attachments_submitted(
                ["attachment-1"],
                submitted=True,
                scope_key="thread:thread-1",
            ),
            "rollback": lambda owner: owner.rollback_attachments_after_failed_submission(
                ["attachment-1"],
                scope_key="thread:thread-1",
            ),
            "observed media": lambda owner: owner.attachment_url_for_path(
                "/work/project/image.png",
                cwd=str(self.workspace),
            ),
            "renderability": lambda owner: owner.is_web_renderable_image("image/png"),
            "remember cwd": lambda owner: owner.remember_thread_cwd(
                "thread-1", str(self.workspace)
            ),
            "admit cwd": lambda owner: owner.admit_draft_working_dir(
                str(self.workspace)
            ),
            "profile": lambda owner: owner.profile("tab-1"),
            "delete scope": lambda owner: owner.delete_thread_scope("thread-1"),
        }
        for name, invoke in commands.items():
            with self.subTest(command=name):
                guard = Mock(side_effect=RuntimeError("outside RuntimeLoop"))
                coordinator, dependencies = self._build_mock_coordinator(guard)

                with self.assertRaisesRegex(RuntimeError, "outside RuntimeLoop"):
                    invoke(coordinator)

                guard.assert_called_once_with()
                for dependency in dependencies.values():
                    self.assertEqual(dependency.mock_calls, [])

    def _stage_draft(self, *, cwd: pathlib.Path | None = None) -> str:
        working_dir = cwd or self.workspace
        staged = self.coordinator.stage_attachment(
            "tab-1",
            cwd=str(working_dir),
            display_name="draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        return staged["file_id"]

    def _select_thread_and_stage(self, thread_id: str = "thread-1") -> str:
        self.coordinator.select_thread("tab-1", thread_id)
        staged = self.coordinator.stage_attachment(
            "tab-1",
            thread_id=thread_id,
            display_name="thread-draft.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        return staged["file_id"]

    def test_profile_commit_invalidates_pending_attachments_from_old_draft(self):
        attachment_id = self._stage_draft()
        replacement = self.data_dir / "replacement"
        replacement.mkdir()

        outcome = self.coordinator.update_profile(
            "tab-1",
            {"selected_thread_id": "", "working_dir": str(replacement)},
        )

        self.assertEqual(outcome.payload["attachment_scope_disposition"], "invalidated")
        self.assertEqual(outcome.payload["invalidated_attachment_count"], 1)
        self.assertEqual(
            outcome.payload["writer_profile"]["working_dir"],
            str(replacement),
        )
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            self.attachments.resolve_pending(
                client_id="tab-1",
                scope_key=f"draft:{self.workspace}",
                attachment_ids=[attachment_id],
            )

    def test_same_cwd_selected_thread_attachments_rebind_to_draft(self):
        attachment_id = self._select_thread_and_stage()

        outcome = self.coordinator.update_profile(
            "tab-1",
            {"selected_thread_id": "", "working_dir": str(self.workspace)},
        )

        self.assertEqual(outcome.payload["attachment_scope_disposition"], "rebound")
        self.assertEqual(outcome.payload["rebound_attachment_count"], 1)
        rebound = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in rebound), (attachment_id,)
        )

    def test_selection_clear_failure_keeps_commit_rebind_and_publish_then_replays(self):
        attachment_id = self._select_thread_and_stage()
        self.runtime_interest.mark_confirmed("thread-1", client_id="tab-1")
        previous = self.profiles.load("tab-1")
        self.assertIsNotNone(previous)
        assert previous is not None
        original_publish = self.projection.publish

        with (
            patch.object(
                self.selection,
                "clear_document_projection",
                side_effect=RuntimeError("selection cleanup failed"),
            ),
            patch.object(
                self.projection,
                "publish",
                wraps=original_publish,
            ) as publish,
            self.assertLogs(
                "bot.web_runtime.writer_workspace_coordinator",
                level="ERROR",
            ),
        ):
            first = self.coordinator.update_profile(
                "tab-1",
                {"selected_thread_id": "", "working_dir": str(self.workspace)},
            )

        committed = self.profiles.load("tab-1")
        self.assertIsNotNone(committed)
        assert committed is not None
        self.assertEqual(committed.selected_thread_id, "")
        self.assertEqual(committed.scope_generation, previous.scope_generation + 1)
        self.assertEqual(first.payload["attachment_scope_disposition"], "rebound")
        self.assertEqual(first.runtime_cleanup_thread_ids, ())
        publish.assert_called_once_with(
            "profile_changed",
            reason="web_profile_updated",
        )
        self.assertEqual(
            self.documents.materialized_thread_id("tab-1"),
            "thread-1",
        )
        interest = self.runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.desired_client_ids, ("tab-1",))
        rebound = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in rebound),
            (attachment_id,),
        )

        replay = self.coordinator.update_profile(
            "tab-1",
            {"selected_thread_id": "", "working_dir": str(self.workspace)},
        )

        replayed = self.profiles.load("tab-1")
        self.assertIsNotNone(replayed)
        assert replayed is not None
        self.assertEqual(replayed.scope_generation, committed.scope_generation)
        self.assertFalse(replay.payload["scope_changed"])
        self.assertEqual(replay.runtime_cleanup_thread_ids, ("thread-1",))
        self.assertEqual(self.documents.materialized_thread_id("tab-1"), "")
        interest = self.runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest and interest.desired_client_ids, ())

    def test_publish_failure_returns_precommit_coordinates_after_profile_commit(self):
        replacement = self.data_dir / "replacement"
        replacement.mkdir()
        fallback = {"runtime_epoch": "fallback-epoch", "revision": 17}
        calls: list[str] = []
        original_update = self.profiles.update

        def commit_profile(client_id, **changes):
            calls.append("profile_commit")
            return original_update(client_id, **changes)

        def fail_publish(*_args, **_kwargs):
            calls.append("publish")
            raise RuntimeError("projection failed")

        with (
            patch.object(
                self.projection,
                "coordinates",
                side_effect=lambda: calls.append("coordinates") or fallback,
            ) as coordinates,
            patch.object(
                self.profiles,
                "update",
                side_effect=commit_profile,
            ),
            patch.object(
                self.projection,
                "publish",
                side_effect=fail_publish,
            ),
            self.assertLogs(
                "bot.web_runtime.writer_workspace_coordinator",
                level="ERROR",
            ),
        ):
            outcome = self.coordinator.update_profile(
                "tab-1",
                {"selected_thread_id": "", "working_dir": str(replacement)},
            )

        self.assertEqual(calls, ["coordinates", "profile_commit", "publish"])
        coordinates.assert_called_once_with()
        self.assertEqual(outcome.payload["runtime_epoch"], "fallback-epoch")
        self.assertEqual(outcome.payload["revision"], 17)
        committed = self.profiles.load("tab-1")
        self.assertIsNotNone(committed)
        self.assertEqual(committed and committed.working_dir, str(replacement))

    def test_profile_write_failure_rolls_rebound_attachments_back(self):
        attachment_id = self._select_thread_and_stage()

        with (
            patch.object(
                self.selection,
                "clear_document_projection",
                wraps=self.selection.clear_document_projection,
            ) as clear_projection,
            patch.object(
                self.projection,
                "publish",
                wraps=self.projection.publish,
            ) as publish,
            patch.object(
                self.profiles,
                "update",
                side_effect=OSError("profile write failed"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "profile write failed"):
                self.coordinator.update_profile(
                    "tab-1",
                    {
                        "selected_thread_id": "",
                        "working_dir": str(self.workspace),
                    },
                )

        clear_projection.assert_not_called()
        publish.assert_not_called()

        pending = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in pending), (attachment_id,)
        )
        self.assertEqual(self.profiles.load("tab-1").selected_thread_id, "thread-1")

    def test_attachment_cleanup_failure_cannot_readmit_old_scope_generation(self):
        attachment_id = self._stage_draft()
        previous = self.coordinator.profile("tab-1")
        replacement = self.data_dir / "replacement"
        replacement.mkdir()

        with (
            patch.object(
                self.attachments,
                "delete_pending_scope",
                side_effect=OSError("attachment cleanup failed"),
            ),
            self.assertLogs(
                "bot.web_runtime.writer_workspace_coordinator",
                level="ERROR",
            ),
        ):
            outcome = self.coordinator.update_profile(
                "tab-1",
                {"selected_thread_id": "", "working_dir": str(replacement)},
            )

        self.assertEqual(outcome.payload["invalidated_attachment_count"], 0)
        committed = self.profiles.load("tab-1")
        self.assertIsNotNone(committed)
        assert committed is not None
        self.assertEqual(committed.working_dir, str(replacement))
        self.assertEqual(committed.scope_generation, previous.scope_generation + 1)
        preserved_for_cleanup_retry = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in preserved_for_cleanup_retry),
            (attachment_id,),
        )
        with self.assertRaises(WebRuntimeError) as stale:
            self.coordinator.stage_attachment(
                "tab-1",
                cwd=str(self.workspace),
                scope_generation=previous.scope_generation,
                display_name="late.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(stale.exception.code, "stale_attachment_scope")

    def test_profile_and_rebind_rollback_double_failure_is_fail_closed(self):
        attachment_id = self._select_thread_and_stage()
        original_rebind = self.attachments.rebind_pending_scope

        def fail_only_rollback(**kwargs):
            if kwargs["source_scope_key"].startswith("draft:"):
                raise OSError("rollback metadata write failed")
            return original_rebind(**kwargs)

        with (
            patch.object(
                self.profiles,
                "update",
                side_effect=OSError("profile write failed"),
            ),
            patch.object(
                self.attachments,
                "rebind_pending_scope",
                side_effect=fail_only_rollback,
            ),
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.coordinator.update_profile(
                    "tab-1",
                    {
                        "selected_thread_id": "",
                        "working_dir": str(self.workspace),
                    },
                )

        self.assertEqual(caught.exception.code, "attachment_scope_rebind_unknown")
        self.assertEqual(self.profiles.load("tab-1").selected_thread_id, "thread-1")
        with self.assertRaisesRegex(ValueError, "different browser draft"):
            self.attachments.resolve_pending(
                client_id="tab-1",
                scope_key="thread:thread-1",
                attachment_ids=[attachment_id],
            )
        stranded = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in stranded), (attachment_id,)
        )

    def test_scope_generation_rejects_aba_upload(self):
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
        generation_a = self.coordinator.select_thread(
            "tab-1",
            "thread-1",
        ).profile.scope_generation
        self.coordinator.select_thread("tab-1", "thread-2")
        current = self.coordinator.select_thread("tab-1", "thread-1")

        self.assertGreater(current.profile.scope_generation, generation_a)
        with self.assertRaises(WebRuntimeError) as caught:
            self.coordinator.stage_attachment(
                "tab-1",
                thread_id="thread-1",
                scope_generation=generation_a,
                display_name="late.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(caught.exception.code, "stale_attachment_scope")

    def test_catalog_profile_and_payload_are_navigation_only(self):
        self.profiles.update(
            "tab-1",
            working_dir=str(self.workspace),
        )

        models, profile = self.coordinator.read_catalog_profile("tab-1")

        self.assertEqual(models, self.fake.list_models())
        self.assertEqual(
            self.coordinator.profile_payload(profile),
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
                "scope_generation": 1,
            },
        )

    def test_submitted_and_rollback_apis_restore_pending_attachment(self):
        attachment_id = self._stage_draft()
        scope_key = f"draft:{self.workspace}"

        self.coordinator.set_attachments_submitted(
            [attachment_id],
            submitted=True,
            scope_key=scope_key,
        )
        self.assertTrue(
            self.attachments.download(attachment_id=attachment_id).record.submitted
        )

        self.coordinator.rollback_attachments_after_failed_submission(
            [attachment_id],
            scope_key=scope_key,
        )

        pending = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key=scope_key,
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in pending), (attachment_id,)
        )

    def test_selection_convergence_does_not_delete_thread_attachment_scope(self):
        attachment_id = self._select_thread_and_stage()

        receipts = self.coordinator.persist_clear_unusable_thread("thread-1")
        self.coordinator.materialize_cleared_unusable_thread(
            "thread-1",
            receipts,
            reason="test_selection_convergence",
        )

        pending = self.attachments.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[attachment_id],
        )
        self.assertEqual(
            tuple(record.attachment_id for record in pending), (attachment_id,)
        )
        self.coordinator.delete_thread_scope("thread-1")
        with self.assertRaisesRegex(ValueError, "missing or expired"):
            self.attachments.resolve_pending(
                client_id="tab-1",
                scope_key="thread:thread-1",
                attachment_ids=[attachment_id],
            )


if __name__ == "__main__":
    unittest.main()
