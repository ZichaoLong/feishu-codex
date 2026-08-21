"""Cross-owner regressions for durable Web selection authority."""

from unittest import mock

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import CodexRpcError
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import WebRuntimeControllerHarness


_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cfc0f01f00050001ff89993d1d0000000049454e44ae426082"
)


class WebSelectionAuthorityTests(WebRuntimeControllerHarness):
    def _summary(
        self,
        thread_id: str,
        *,
        source: str = "appServer",
        parent_thread_id: str | None = None,
        can_accept_direct_input: bool = True,
        subagent_kind: str = "",
    ) -> ThreadSummary:
        return ThreadSummary(
            thread_id=thread_id,
            cwd=str(self.workspace),
            name=thread_id,
            preview=thread_id,
            created_at=3,
            updated_at=4,
            source=source,
            status="idle",
            parent_thread_id=parent_thread_id,
            can_accept_direct_input=can_accept_direct_input,
            subagent_kind=subagent_kind,
        )

    def _install_drift(
        self,
        *,
        durable_thread_id: str,
        materialized_thread_id: str,
    ):
        profile = self.profile_store.update(
            "tab-1",
            selected_thread_id=durable_thread_id,
            working_dir=str(self.workspace),
        )
        self.document_registry.materialize_thread("tab-1", materialized_thread_id)
        self.controller._runtime_interest.mark_confirmed(
            materialized_thread_id,
            client_id="tab-1",
        )
        return profile

    def test_durable_profile_wins_attachment_and_cd_scope_while_materialized_drives_cleanup(self):
        self.fake.extra_summaries = [self._summary("thread-2")]
        profile = self._install_drift(
            durable_thread_id="thread-1",
            materialized_thread_id="thread-2",
        )

        meta = self.controller.meta("tab-1")
        self.assertEqual(meta["writer_profile"]["selected_thread_id"], "thread-1")
        accepted = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            scope_generation=profile.scope_generation,
            display_name="durable.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        with self.assertRaises(WebRuntimeError) as stale:
            self.controller.stage_attachment(
                "tab-1",
                thread_id="thread-2",
                scope_generation=profile.scope_generation,
                display_name="materialized.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(stale.exception.code, "stale_attachment_scope")

        switched = self.controller.update_profile(
            "tab-1",
            {
                "selected_thread_id": "",
                "working_dir": str(self.workspace),
            },
        )

        self.assertEqual(switched["previous_attachment_scope"], "thread:thread-1")
        self.assertEqual(switched["attachment_scope_disposition"], "rebound")
        self.assertEqual(self.document_registry.materialized_thread_id("tab-1"), "")
        self.assertFalse(self.controller._runtime_interest.has_desired_clients("thread-2"))
        rebound = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key=f"draft:{self.workspace}",
            attachment_ids=[accepted["file_id"]],
        )
        self.assertEqual([record.attachment_id for record in rebound], [accepted["file_id"]])

    def test_older_history_requires_durable_selection_and_materialization(self):
        self.fake.extra_summaries = [self._summary("thread-2")]
        self._install_drift(
            durable_thread_id="thread-1",
            materialized_thread_id="thread-2",
        )

        for target in ("thread-1", "thread-2"):
            with self.subTest(target=target):
                with self.assertRaises(WebRuntimeError) as caught:
                    self.controller.list_older_turns(
                        "tab-1",
                        target,
                        cursor="older",
                    )
                self.assertEqual(caught.exception.code, "thread_not_selected")
        self.assertEqual(self.fake.turn_pages, [])

        self.document_registry.materialize_thread("tab-1", "thread-1")
        page = self.controller.list_older_turns(
            "tab-1",
            "thread-1",
            cursor="older",
        )

        self.assertEqual(page["turns"], [])
        self.assertEqual(len(self.fake.turn_pages), 1)

    def test_not_found_clears_durable_materialized_and_desired_target(self):
        self.fake.extra_summaries = [self._summary("missing-thread")]
        profile = self._install_drift(
            durable_thread_id="missing-thread",
            materialized_thread_id="missing-thread",
        )
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="missing-thread",
            scope_generation=profile.scope_generation,
            display_name="isolated.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        self.fake.read_error = CodexRpcError(
            "thread/read",
            {"code": -32600, "message": "thread not found"},
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "missing-thread")

        self.assertEqual(caught.exception.code, "thread_not_found")
        cleared = self.profile_store.load("tab-1")
        self.assertEqual(cleared.selected_thread_id, "")
        self.assertEqual(cleared.scope_generation, profile.scope_generation + 1)
        self.assertEqual(self.document_registry.materialized_thread_id("tab-1"), "")
        self.assertFalse(
            self.controller._runtime_interest.has_desired_clients("missing-thread")
        )
        isolated = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:missing-thread",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual([item.attachment_id for item in isolated], [upload["file_id"]])
        with self.assertRaises(WebRuntimeError) as stale:
            self.controller.stage_attachment(
                "tab-1",
                thread_id="missing-thread",
                scope_generation=profile.scope_generation,
                display_name="stale.png",
                media_type="image/png",
                content=_PNG_1X1,
            )
        self.assertEqual(stale.exception.code, "stale_attachment_scope")

    def test_reissued_document_rolls_stale_profile_write_forward(self):
        original_update = self.profile_store.update_if_matches
        replaced = False

        def reissue_before_first_selection_write(*args, **kwargs):
            nonlocal replaced
            if not replaced and kwargs.get("selected_thread_id") == "thread-1":
                replaced = True
                self.controller.client_document_reissued("tab-1")
            return original_update(*args, **kwargs)

        with mock.patch.object(
            self.profile_store,
            "update_if_matches",
            side_effect=reissue_before_first_selection_write,
        ):
            with self.assertRaises(WebRuntimeError) as caught:
                self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(caught.exception.code, "stale_document_read")
        profile = self.profile_store.load("tab-1")
        self.assertIsNotNone(profile)
        self.assertEqual(profile and profile.selected_thread_id, "")
        self.assertEqual(profile and profile.scope_generation, 3)
        self.assertEqual(self.document_registry.materialized_thread_id("tab-1"), "")
        self.assertTrue(
            any(
                event.get("reason") == "web_stale_open_selection_compensated"
                for event in self.events
            )
        )

    def test_stale_profile_compensation_cannot_overwrite_successor(self):
        snapshot = self.controller._workspace.load_profile_snapshot("tab-1")
        stale = self.controller._workspace.persist_thread_selection(
            snapshot,
            "thread-1",
        )
        self.assertIsNotNone(stale)
        assert stale is not None
        successor = self.profile_store.update(
            "tab-1",
            selected_thread_id="thread-2",
            scope_generation=stale.current.scope_generation + 1,
        )

        compensated = (
            self.controller._workspace.compensate_stale_persisted_selection(stale)
        )

        self.assertIsNone(compensated)
        self.assertEqual(self.profile_store.load("tab-1"), successor)

    def test_thread_spawn_rejection_clears_every_direct_selection_fact(self):
        self.fake.extra_summaries = [
            self._summary(
                "child-1",
                source="subAgent",
                parent_thread_id="thread-1",
                can_accept_direct_input=False,
                subagent_kind="threadSpawn",
            )
        ]
        profile = self._install_drift(
            durable_thread_id="child-1",
            materialized_thread_id="child-1",
        )
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="child-1",
            scope_generation=profile.scope_generation,
            display_name="invalid-child.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        with self.assertRaises(WebRuntimeError) as caught:
            self.controller.read_thread("tab-1", "child-1")

        self.assertEqual(caught.exception.code, "subagent_detail_only")
        cleared = self.profile_store.load("tab-1")
        self.assertEqual(cleared.selected_thread_id, "")
        self.assertEqual(cleared.scope_generation, profile.scope_generation + 1)
        self.assertEqual(self.document_registry.materialized_thread_id("tab-1"), "")
        self.assertFalse(self.controller._runtime_interest.has_desired_clients("child-1"))
        with self.assertRaises(KeyError):
            self.attachment_store.download(attachment_id=upload["file_id"])

    def test_stale_thread_spawn_observation_cannot_clear_replacement_document(self):
        self.fake.extra_summaries = [
            self._summary(
                "child-1",
                source="subAgent",
                parent_thread_id="thread-1",
                can_accept_direct_input=False,
                subagent_kind="threadSpawn",
            )
        ]
        self._install_drift(
            durable_thread_id="child-1",
            materialized_thread_id="child-1",
        )
        prepared = self.controller.prepare_read_thread("tab-1", "child-1")
        with self.assertRaises(WebRuntimeError) as observed:
            self.controller._thread_open.execute_read_thread_observation(prepared)
        self.document_registry.begin_operation(
            "tab-1",
            operation="thread_open",
            target_thread_id="child-1",
        )

        with self.assertRaises(WebRuntimeError) as settled:
            self.controller._thread_open.finish_read_thread_observation_failure(
                prepared,
                observed.exception,
            )

        self.assertEqual(settled.exception.code, "subagent_detail_only")
        self.assertEqual(
            self.profile_store.load("tab-1").selected_thread_id,
            "child-1",
        )
        self.assertEqual(
            self.document_registry.materialized_thread_id("tab-1"),
            "child-1",
        )

    def test_confirmed_delete_removes_thread_scoped_attachments(self):
        profile = self._install_drift(
            durable_thread_id="thread-1",
            materialized_thread_id="thread-1",
        )
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            scope_generation=profile.scope_generation,
            display_name="delete-me.png",
            media_type="image/png",
            content=_PNG_1X1,
        )

        result = self.controller.delete_thread(
            "tab-1",
            "thread-1",
            confirmation="thread-1",
        )

        self.assertEqual(result["upstream_outcome"], "success")
        cleared = self.profile_store.load("tab-1")
        self.assertEqual(cleared.selected_thread_id, "")
        self.assertEqual(cleared.scope_generation, profile.scope_generation + 1)
        with self.assertRaises(KeyError):
            self.attachment_store.download(attachment_id=upload["file_id"])

    def test_lifecycle_cleanup_preserves_replacement_materialization_and_removes_stray_edges(self):
        self.fake.extra_summaries = [self._summary("thread-2")]
        profile = self._install_drift(
            durable_thread_id="thread-1",
            materialized_thread_id="thread-2",
        )
        upload = self.controller.stage_attachment(
            "tab-1",
            thread_id="thread-1",
            scope_generation=profile.scope_generation,
            display_name="archived-isolated.png",
            media_type="image/png",
            content=_PNG_1X1,
        )
        self.controller._runtime_interest.add_desired_client("thread-2", "tab-2")
        self.controller._runtime_interest.mark_confirmed(
            "thread-3",
            client_id="tab-1",
        )

        self.controller.handle_notification("thread/archived", {"threadId": "thread-1"})

        cleared = self.profile_store.load("tab-1")
        self.assertEqual(cleared.selected_thread_id, "")
        self.assertEqual(cleared.scope_generation, profile.scope_generation + 1)
        self.assertEqual(
            self.document_registry.materialized_thread_id("tab-1"),
            "thread-2",
        )
        self.assertEqual(
            self.controller._runtime_interest.snapshot("thread-2").desired_client_ids,
            ("tab-2",),
        )
        self.assertEqual(
            self.controller._runtime_interest.desired_thread_ids_for_client("tab-1"),
            (),
        )
        isolated = self.attachment_store.resolve_pending(
            client_id="tab-1",
            scope_key="thread:thread-1",
            attachment_ids=[upload["file_id"]],
        )
        self.assertEqual([item.attachment_id for item in isolated], [upload["file_id"]])
        profile_events = [
            event
            for event in self.events
            if event["type"] == "profile_changed"
            and event.get("reason") == "web_lifecycle_selection_cleared"
        ]
        self.assertEqual(len(profile_events), 1)

        self.controller.handle_notification("thread/archived", {"threadId": "thread-1"})

        replayed = self.profile_store.load("tab-1")
        self.assertEqual(replayed.scope_generation, cleared.scope_generation)
        self.assertEqual(
            len(
                [
                    event
                    for event in self.events
                    if event["type"] == "profile_changed"
                    and event.get("reason") == "web_lifecycle_selection_cleared"
                ]
            ),
            1,
        )

    def test_selection_projection_and_disconnect_remove_every_stray_runtime_edge(self):
        self.fake.extra_summaries = [self._summary("thread-2")]
        self._install_drift(
            durable_thread_id="thread-1",
            materialized_thread_id="thread-2",
        )
        self.controller._runtime_interest.mark_confirmed(
            "thread-3",
            client_id="tab-1",
        )

        self.thread_open.select_thread("tab-1", "thread-1")

        self.assertEqual(
            self.controller._runtime_interest.desired_thread_ids_for_client("tab-1"),
            (),
        )
        self.controller._runtime_interest.mark_confirmed(
            "thread-1",
            client_id="tab-1",
        )
        self.controller._runtime_interest.mark_confirmed(
            "thread-3",
            client_id="tab-1",
        )

        self.controller.client_disconnected("tab-1")

        self.assertEqual(
            self.controller._runtime_interest.desired_thread_ids_for_client("tab-1"),
            (),
        )

    def test_disconnect_cleans_resume_interest_after_selection_projection_failure(self):
        original_update = self.profile_store.update_if_matches

        def fail_selection(client_id, expected, **changes):
            if changes.get("selected_thread_id") == "thread-1":
                raise OSError("profile commit failed")
            return original_update(client_id, expected, **changes)

        with mock.patch.object(
            self.profile_store,
            "update_if_matches",
            side_effect=fail_selection,
        ):
            with self.assertRaisesRegex(OSError, "profile commit failed"):
                self.controller.read_thread("tab-1", "thread-1")

        self.assertEqual(
            self.controller._runtime_interest.desired_thread_ids_for_client("tab-1"),
            ("thread-1",),
        )

        self.controller.client_disconnected("tab-1")

        self.assertEqual(
            self.controller._runtime_interest.desired_thread_ids_for_client("tab-1"),
            (),
        )
