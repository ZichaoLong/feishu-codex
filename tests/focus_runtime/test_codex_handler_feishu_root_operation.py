"""Handler integration for the minimal Feishu main-turn owner."""

import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import CodexRpcTransportError
from tests.focus_runtime.codex_handler_fakes import _bind_authoritative_thread
from tests.focus_runtime.codex_handler_test_harness import CodexHandlerHarness


class CodexHandlerFeishuRootOperationTests(CodexHandlerHarness):

    @staticmethod
    def _idle_thread() -> ThreadSummary:
        return ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )

    @staticmethod
    def _cold_thread() -> ThreadSummary:
        return ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="notLoaded",
        )

    def _bind_idle_thread(self, handler) -> ThreadSummary:
        root = self._idle_thread()
        _bind_authoritative_thread(handler, "ou_user", "chat-1", root)
        return root

    def test_active_goal_mutation_uses_local_submission_then_exact_turn(self) -> None:
        handler, _ = self._make_handler()
        self._bind_idle_thread(handler)

        goal = handler._runtime_call(
            handler._feishu_continuation.mutate_goal,
            "ou_user",
            "chat-1",
            "thread-1",
            objective="continue",
            status="active",
        )

        self.assertEqual(goal.status, "active")
        snapshot = self._feishu_root_snapshot(handler, "thread-1")
        self.assertTrue(snapshot.submission_outcome_unknown)
        self.assertEqual(snapshot.pending_admission_count, 1)
        submission = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(submission)
        self.assertEqual(submission.turn_id, "")

        handler._runtime_call(
            handler._feishu_root_operations.reconcile_notification,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        active = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.turn_id, "turn-1")
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

        handler._runtime_call(
            handler._feishu_root_operations.reconcile_notification,
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))

    def test_paused_goal_mutation_does_not_retain_idle_writer(self) -> None:
        handler, _ = self._make_handler()
        self._bind_idle_thread(handler)

        goal = handler._runtime_call(
            handler._feishu_continuation.mutate_goal,
            "ou_user",
            "chat-1",
            "thread-1",
            objective="pause",
            status="paused",
        )

        self.assertEqual(goal.status, "paused")
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

    def test_transport_unknown_is_local_and_does_not_create_durable_owner(self) -> None:
        handler, _ = self._make_handler()
        self._bind_idle_thread(handler)

        def disconnect_after_send(*_args, **_kwargs):
            raise CodexRpcTransportError(
                "thread/goal/set",
                {"message": "transport reset"},
            )

        handler._adapter.set_thread_goal = disconnect_after_send

        with self.assertRaises(CodexRpcTransportError):
            handler._runtime_call(
                handler._feishu_continuation.mutate_goal,
                "ou_user",
                "chat-1",
                "thread-1",
                objective="continue",
                status="active",
            )

        snapshot = self._feishu_root_snapshot(handler, "thread-1")
        self.assertTrue(snapshot.submission_outcome_unknown)
        self.assertIsNotNone(handler._interaction_lease_store.load("thread-1"))

    def test_sole_feishu_subscriber_without_turn_owner_is_not_auto_adopted(self) -> None:
        handler, bot = self._make_handler()
        root = self._idle_thread()
        root = ThreadSummary(
            thread_id=root.thread_id,
            cwd=root.cwd,
            name=root.name,
            preview=root.preview,
            created_at=root.created_at,
            updated_at=root.updated_at,
            source=root.source,
            status="active",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-1", root)

        self._adapter_request(
            handler,
            "req-no-owner",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "command": "pwd",
                "cwd": "/tmp/project",
                "reason": "must not adopt a subscriber",
            },
        )

        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertEqual(bot.sent_messages, [])
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        pending = handler._server_request_registry.pending_items()
        self.assertEqual(len(pending), 1)
        identity = handler._server_request_registry.active_identity(pending[0][0])
        self.assertIsNotNone(identity)
        assert identity is not None
        self.assertEqual(identity.request_id, "req-no-owner")
        self.assertEqual(
            handler._server_request_registry.response_phase(identity),
            "pending",
        )

    def test_explicit_resume_reads_local_settings_before_submission_admission(self) -> None:
        handler, bot = self._make_handler()
        thread = self._cold_thread()
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(
            summary=thread
        )

        def fail_session(*_args, **_kwargs):
            raise ValueError("malformed local settings projection")

        with (
            patch.object(
                handler._binding_runtime,
                "resolve_session",
                side_effect=fail_session,
            ),
            self.assertLogs("bot.focus_runtime", level="ERROR"),
        ):
            handler._runtime_call(
                handler._threads_ui_domain._resume_target_on_runtime,
                "ou_user",
                "chat-1",
                "thread-1",
                summary=thread,
            )

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertIn("malformed local settings projection", bot.replies[-1][1])


if __name__ == "__main__":
    unittest.main()
