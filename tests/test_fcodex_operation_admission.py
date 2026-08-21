"""Fcodex operation admission and authoritative direct-target regressions."""

from __future__ import annotations

import os

from bot.stores.interaction_lease_store import (
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)
from tests.fcodex_operation_harness import (
    DirectThreadTargetPolicyError,
    FcodexOperationHarness,
    ThreadSummary,
)


class FcodexOperationAdmissionTests(FcodexOperationHarness):

    def _seed_effective_settings(self, thread_id: str = "root-1") -> None:
        self.effective_settings.record_start_or_resume(
            thread_id,
            model="model-a",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        self.effective_settings.observe_notification(
            "turn/started",
            {"threadId": thread_id, "turn": {"id": "turn-existing"}},
        )

    def test_admitted_turn_start_clears_noncanonical_settings_evidence(self) -> None:
        self._seed_effective_settings()
        self._connect()

        admitted = self._admit(
            method="turn/start",
            request_params={
                "threadId": "root-1",
                "model": "model-b",
                "effort": "ultra",
                "approvalPolicy": "on-request",
                "permissions": ":danger-full-access",
            },
        )

        self.assertTrue(admitted["allowed"])
        self.assertIsNone(self.effective_settings.resolve_model_for_request("root-1"))
        disclosure = self.effective_settings.disclosure_for_active_turn(
            "root-1",
            "turn-existing",
        )
        self.assertEqual(disclosure.model.source, "unknown")
        self.assertEqual(disclosure.reasoning_effort.source, "unknown")
        self.assertEqual(disclosure.approval_policy.source, "unknown")
        self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_turn_start_retires_all_fields_even_when_settings_are_equal(self) -> None:
        self._seed_effective_settings("root-1")
        self._seed_effective_settings("root-2")
        self._connect()

        for request_id, thread_id, params in (
            (
                1,
                "root-1",
                {
                    "threadId": "root-1",
                    "model": "model-a",
                    "effort": "high",
                    "approvalPolicy": "never",
                    "permissions": ":workspace",
                },
            ),
            (2, "root-2", {"threadId": "root-2"}),
        ):
            with self.subTest(thread_id=thread_id):
                admitted = self._admit(
                    request_id=request_id,
                    method="turn/start",
                    thread_id=thread_id,
                    request_params=params,
                )

                self.assertTrue(admitted["allowed"])
                disclosure = self.effective_settings.disclosure_for_active_turn(
                    thread_id,
                    "turn-existing",
                )
                self.assertEqual(disclosure.model.source, "unknown")
                self.assertEqual(disclosure.reasoning_effort.source, "unknown")
                self.assertEqual(disclosure.approval_policy.source, "unknown")
                self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_settings_update_stays_unknown_across_unordered_canonical_ingress(self) -> None:
        self._seed_effective_settings()
        self._connect()

        admitted = self._admit(
            method="thread/settings/update",
            request_params={
                "threadId": "root-1",
                "model": "model-a",
                "effort": "ultra",
                "approvalPolicy": "never",
                "permissions": ":danger-full-access",
            },
        )

        self.assertTrue(admitted["allowed"])
        self.effective_settings.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "root-1",
                "threadSettings": {
                    "model": "event-model",
                    "effort": "ultra",
                    "approvalPolicy": "on-request",
                    "activePermissionProfile": {"id": ":danger-full-access"},
                },
            },
        )
        self.effective_settings.record_start_or_resume(
            "root-1",
            model="response-model",
            reasoning_effort="medium",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        self.assertIsNone(self.effective_settings.resolve_model_for_request("root-1"))

        self.effective_settings.clear_all()
        self.effective_settings.record_start_or_resume(
            "root-1",
            model="after-reset",
            reasoning_effort=None,
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        self.assertEqual(
            self.effective_settings.resolve_model_for_request("root-1"),
            "after-reset",
        )

    def test_autonomous_goal_admission_retires_settings_evidence(self) -> None:
        self._seed_effective_settings()
        self._connect()

        admitted = self._admit(
            method="thread/goal/set",
            request_params={"threadId": "root-1", "goal": {"objective": "continue"}},
            continuation_risk=True,
        )

        self.assertTrue(admitted["allowed"])
        self.assertIsNone(self.effective_settings.resolve_model_for_request("root-1"))

    def test_noncontinuing_goal_admission_keeps_settings_evidence(self) -> None:
        self._seed_effective_settings()
        self._connect()

        admitted = self._admit(
            method="thread/goal/set",
            request_params={"threadId": "root-1", "goal": {"objective": "paused"}},
            continuation_risk=False,
        )

        self.assertTrue(admitted["allowed"])
        self.assertEqual(
            self.effective_settings.resolve_model_for_request("root-1"),
            "model-a",
        )

    def test_review_and_compact_admission_retire_settings_evidence(self) -> None:
        self._connect()

        for request_id, method in enumerate(
            ("review/start", "thread/compact/start"),
            start=1,
        ):
            with self.subTest(method=method):
                self.effective_settings.clear_all()
                self._seed_effective_settings()
                admitted = self._admit(
                    request_id=request_id,
                    method=method,
                    request_params={"threadId": "root-1"},
                )
                self.assertTrue(admitted["allowed"])
                self.assertIsNone(
                    self.effective_settings.resolve_model_for_request("root-1")
                )
                self._client_response(
                    request_id=request_id,
                    outcome="error",
                )

    def test_review_and_compact_reject_every_foreign_lease(self) -> None:
        self._connect()
        holders = (
            make_web_interaction_holder("document-1", owner_pid=os.getpid()),
            make_feishu_interaction_holder(
                "ou_user",
                "chat-1",
                owner_pid=os.getpid(),
            ),
        )
        request_id = 20
        for holder in holders:
            for active_turn_id in ("", "foreign-turn"):
                for method in ("review/start", "thread/compact/start"):
                    request_id += 1
                    with self.subTest(
                        holder_kind=holder.kind,
                        active_turn_id=active_turn_id,
                        method=method,
                    ):
                        lease = self.interaction_leases.force_acquire(
                            "root-1",
                            holder,
                        )
                        if active_turn_id:
                            self.interaction_leases.activate_turn(
                                lease,
                                active_turn_id,
                            )
                        before = self.interaction_leases.load("root-1")

                        denied = self._admit(
                            request_id=request_id,
                            method=method,
                        )

                        self.assertFalse(denied["allowed"])
                        self.assertIn("main turn writer", denied["reason"])
                        self.assertEqual(
                            self.interaction_leases.load("root-1"),
                            before,
                        )
                        self.assertEqual(
                            self.operation_service._client_requests,
                            {},
                        )
                        assert before is not None
                        self.assertTrue(
                            self.interaction_leases.release_if_matches(before)
                        )

    def test_external_thread_lifecycle_admission_retires_settings_evidence(self) -> None:
        self._connect()

        for request_id, method in enumerate(
            ("thread/archive", "thread/delete", "thread/unarchive"),
            start=1,
        ):
            with self.subTest(method=method):
                self.effective_settings.clear_all()
                self._seed_effective_settings()
                admitted = self._admit(request_id=request_id, method=method)
                self.assertTrue(admitted["allowed"])
                self.assertIsNone(
                    self.effective_settings.resolve_model_for_request("root-1")
                )
                self._client_response(request_id=request_id, outcome="error")

    def test_admitted_resume_clears_settings_but_targetless_start_does_not(self) -> None:
        self._seed_effective_settings()
        self._connect()

        created = self._admit(
            request_id=1,
            method="thread/start",
            thread_id="",
            request_params={"cwd": "/repo", "model": "created-model"},
        )
        self.assertTrue(created["allowed"])
        self.assertEqual(
            self.effective_settings.resolve_model_for_request("root-1"),
            "model-a",
        )

        resumed = self._admit(
            request_id=2,
            method="thread/resume",
            request_params={
                "threadId": "root-1",
                "approvalsReviewer": "user",
            },
        )
        self.assertTrue(resumed["allowed"])
        self.assertIsNone(self.effective_settings.resolve_model_for_request("root-1"))

    def test_backend_disconnect_expires_direct_root_routing_proof(self) -> None:
        self.assertEqual(
            self.operation_service.interaction_root_for_thread("root-1"),
            "root-1",
        )

        self.coordinator.backend_disconnected()

        self.assertEqual(
            self.operation_service.interaction_root_for_thread("root-1"),
            "",
        )
        receipt = self.coordinator.settle_backend_epoch_after_stop()
        self.assertEqual(receipt.requests.routed_thread_ids, ())

    def test_authoritative_direct_target_rejects_thread_spawn_and_keeps_other_kinds_direct(self) -> None:
        """Only service-verified non-ThreadSpawn targets become direct roots."""

        for thread_id, parent_thread_id in (
            ("child-with-parent", "root-1"),
            ("child-without-parent", None),
        ):
            with self.subTest(thread_id=thread_id):
                summary = ThreadSummary(
                    thread_id=thread_id,
                    cwd="/tmp/project",
                    name="child",
                    preview="",
                    created_at=1,
                    updated_at=1,
                    source="subAgent",
                    status="active",
                    parent_thread_id=parent_thread_id,
                    subagent_kind="threadSpawn",
                )

                with self.assertRaises(DirectThreadTargetPolicyError):
                    self.coordinator.remember_authoritative_direct_target(
                        summary,
                        expected_thread_id=thread_id,
                        operation="通过 fcodex 直接操作",
                    )

                self.assertEqual(self.operation_service._known_root(thread_id), "")

        for subagent_kind in ("auxiliary", "review", "guardian"):
            with self.subTest(subagent_kind=subagent_kind):
                thread_id = f"{subagent_kind}-1"
                summary = ThreadSummary(
                    thread_id=thread_id,
                    cwd="/tmp/project",
                    name=subagent_kind,
                    preview="",
                    created_at=1,
                    updated_at=1,
                    source="subAgent",
                    status="idle",
                    parent_thread_id="root-1",
                    subagent_kind=subagent_kind,
                )

                self.assertEqual(
                    self.coordinator.remember_authoritative_direct_target(
                        summary,
                        expected_thread_id=thread_id,
                        operation="通过 fcodex 直接操作",
                    ),
                    thread_id,
                )
                self.assertEqual(self.operation_service._known_root(thread_id), thread_id)

    def test_unknown_or_malformed_thread_source_never_becomes_direct_root(self) -> None:
        for source_status, source, subagent_kind in (
            ("unknown", "unknown", None),
            ("malformed", "unknown", None),
            ("malformed", "subAgent", "threadSpawn"),
        ):
            with self.subTest(source_status=source_status, source=source):
                summary = ThreadSummary(
                    thread_id=f"untrusted-{source_status}-{source}",
                    cwd="/tmp/project",
                    name="untrusted",
                    preview="",
                    created_at=1,
                    updated_at=1,
                    source=source,
                    status="idle",
                    subagent_kind=subagent_kind,
                    source_status=source_status,
                )
                with self.assertRaises(DirectThreadTargetPolicyError):
                    self.coordinator.remember_authoritative_direct_target(
                        summary,
                        expected_thread_id=summary.thread_id,
                        operation="通过 fcodex 直接操作",
                    )
                self.assertEqual(
                    self.operation_service._known_root(summary.thread_id), ""
                )

    def test_client_request_keys_include_connection_and_jsonrpc_id_type(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")

        for connection_id, request_id in (
            ("connection-a", 1),
            ("connection-b", 1),
            ("connection-a", "1"),
            ("connection-b", "1"),
        ):
            admitted = self._admit(
                connection_id=connection_id,
                request_id=request_id,
                method="thread/start",
                thread_id="",
            )
            self.assertTrue(admitted["allowed"])
            settled = self._client_response(
                participant_id=self.participant_id,
                connection_id=connection_id,
                request_id=request_id,
                outcome="success",
            )
            self.assertTrue(settled["known"])

    def test_thread_create_and_ordinary_turn_start_create_no_writer(self) -> None:
        """Neither root creation nor ordinary realtime input creates a writer."""

        self._connect()
        created = self._admit(request_id=1, method="thread/start", thread_id="")
        self.assertTrue(created["allowed"])

        settled = self._client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            outcome="success",
            observed_thread_id="root-created",
            observed_root_thread_id="root-created",
        )
        self.assertTrue(settled["known"])
        self.assertTrue(settled["settled"])
        self.assertIsNone(self.interaction_leases.load("root-created"))

        started = self._admit(request_id=2, method="turn/start", thread_id="root-created")
        self.assertTrue(started["allowed"])
        self.assertTrue(started["tracks_response"])
        self.assertIsNone(self.interaction_leases.load("root-created"))

    def test_same_participant_unattached_connection_cannot_steer(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self._seed_fcodex_active_lease("connection-a")

        denied = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="turn/steer",
            request_params={
                "threadId": "root-1",
                "input": [{"type": "text", "text": "unattached steer"}],
                "expectedTurnId": "turn-1",
            },
        )

        self.assertFalse(denied["allowed"])
        self.assertIn("未 attach", denied["reason"])
        self.assertIsNotNone(self.interaction_leases.load("root-1"))

    def test_async_upstream_methods_without_a_terminal_ownership_contract_fail_closed(self) -> None:
        self._connect()

        for request_id, method in enumerate(
            (
                "thread/shellCommand",
                "thread/rollback",
                "thread/realtime/start",
                "thread/backgroundTerminals/clean",
            ),
            start=1,
        ):
            denied = self._admit(request_id=request_id, method=method)
            self.assertFalse(denied["allowed"], method)
            self.assertIn("尚未", denied["reason"])

    def test_fork_is_explicitly_denied_before_it_can_create_an_unowned_thread(self) -> None:
        self._connect()

        denied = self._admit(request_id=2, method="thread/fork")

        self.assertFalse(denied["allowed"])
        self.assertIn("thread/fork", denied["reason"])
        self.assertIsNone(self.interaction_leases.load("root-1"))
