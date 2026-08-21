import os
import pathlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_fakes import _detach_thread, _runtime_state
from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
    make_web_interaction_holder,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerThreadAttachmentTests(CodexHandlerHarness):
    def test_feishu_audience_changes_invalidate_the_exact_web_thread(self) -> None:
        handler, _bot = self._make_handler()
        events: list[dict] = []
        unsubscribe = handler._web_projection.subscribe(events.append)
        self.addCleanup(unsubscribe)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _detach_thread(handler, "thread-1")

        audience_events = [
            event
            for event in events
            if event.get("reason") == "feishu_audience_changed"
        ]
        self.assertEqual(
            [
                (event["type"], event["thread_id"])
                for event in audience_events
            ],
            [
                ("thread_invalidated", "thread-1"),
                ("thread_invalidated", "thread-1"),
            ],
        )

    def test_detach_command_detaches_current_binding_and_keeps_other_binding_attached(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler, "ou_user2", "chat-b", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "chat-a", "/detach")

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertEqual(_runtime_state(handler, "ou_user", "chat-a")["current_thread_id"], "thread-1")
        self.assertEqual(_runtime_state(handler, "ou_user2", "chat-b")["current_thread_id"], "thread-1")
        self.assertEqual(_runtime_state(handler, "ou_user", "chat-a")["feishu_runtime_state"], "detached")
        self.assertEqual(_runtime_state(handler, "ou_user2", "chat-b")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), (("ou_user2", "chat-b"),))
        _, card = bot.cards[-1]
        self.assertIn("backend thread status：`idle`", card["elements"][0]["content"])

    def test_detached_binding_hydrates_without_resubscribe_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        unloaded = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="notLoaded",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        def _unsubscribe(thread_id: str) -> None:
            handler._adapter.unsubscribe_thread_calls.append(thread_id)
            handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(summary=unloaded)

        handler._adapter.unsubscribe_thread = _unsubscribe
        _detach_thread(handler, "thread-1")

        handler2, _ = self._make_handler(data_dir=data_dir)
        state2 = _runtime_state(handler2, "ou_user", "c1")
        self.assertEqual(state2["current_thread_id"], "thread-1")
        self.assertEqual(state2["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._binding_runtime_coordinator.thread_subscribers("thread-1"), ())

        handler2.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(_runtime_state(handler2, "ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_attach_command_resumes_loaded_thread_to_restore_service_subscription(self) -> None:
        handler, _ = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        _detach_thread(handler, "thread-1")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), ())

        handler.handle_message("ou_user", "c1", "/attach")

        self.assertEqual(handler._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "attached")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), (("ou_user", "c1"),))

    def test_attach_bind_commit_failure_compensates_fresh_resume_lease(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")
        handler._adapter.unsubscribe_thread_calls.clear()
        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), ())

        with patch.object(
            handler._binding_runtime,
            "bind_thread_locked",
            side_effect=RuntimeError("durable bind failed"),
        ):
            handler.handle_message("ou_user", "c1", "/attach")

        self.assertIn("durable bind failed", bot.replies[-1][1])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), ())
        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"],
            "detached",
        )

    def test_attach_cleanup_failure_retains_only_the_exact_runtime_effect(self) -> None:
        handler, _bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")
        handler._adapter.unsubscribe_thread = lambda _thread_id: (_ for _ in ()).throw(
            RuntimeError("unsubscribe failed")
        )

        with patch.object(
            handler._binding_runtime,
            "bind_thread_locked",
            side_effect=RuntimeError("durable bind failed"),
        ):
            handler.handle_message("ou_user", "c1", "/attach")

        status = handler._operational_status_snapshot()
        self.assertNotIn("thread_resume_recovery", status)
        self.assertNotEqual(self._service_runtime_holder_ids(handler, "thread-1"), ())

    def test_safe_attach_unknown_keeps_only_the_exact_blank_submission(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")
        original_resume = handler._adapter.resume_thread
        handler._adapter.resume_thread = lambda _thread_id, **_kwargs: (_ for _ in ()).throw(
            CodexRpcError(
                "thread/resume",
                {"code": -32603, "message": "response assembly failed"},
            )
        )

        handler.handle_message("ou_user", "c1", "/attach")

        self.assertIn("无法确认 thread/resume", bot.replies[-1][1])
        self.assertNotIn(
            "thread_resume_recovery",
            handler._operational_status_snapshot(),
        )
        lease = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "")
        handler._adapter.resume_thread = original_resume

        handler.handle_message("ou_user", "c1", "/attach")

        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"],
            "detached",
        )
        self.assertIn("submission", bot.replies[-1][1])

    def test_attach_post_commit_projection_failure_does_not_revoke_subscription(self) -> None:
        handler, _bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")
        handler._adapter.unsubscribe_thread_calls.clear()

        with patch.object(
            handler._binding_runtime,
            "project_thread_goal_locked",
            side_effect=RuntimeError("projection failed"),
        ):
            handler.handle_message("ou_user", "c1", "/attach")

        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"],
            "attached",
        )
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertNotEqual(self._service_runtime_holder_ids(handler, "thread-1"), ())

    def test_feishu_explicit_resume_is_not_blocked_by_a_prior_unknown(self) -> None:
        handler, _bot = self._make_handler()
        thread = self._seed_authoritative_thread(handler)
        self._seed_resume_outcome_unknown(handler, thread.thread_id)
        handler._adapter.resume_thread_calls.clear()

        handler._runtime_call(
            handler._threads_ui_domain._resume_target_on_runtime,
            "ou_user",
            "c1",
            thread.thread_id,
            summary=thread,
            message_id="resume-message",
        )

        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.resume_thread_calls],
            [thread.thread_id],
        )
        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["current_thread_id"],
            thread.thread_id,
        )

    def test_detached_prompt_is_not_blocked_by_a_prior_resume_unknown(self) -> None:
        handler, _bot = self._make_handler()
        thread = self._seed_authoritative_thread(handler, status="idle")
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        _detach_thread(handler, thread.thread_id)
        handler._adapter.unsubscribe_thread_calls.clear()
        self._seed_resume_outcome_unknown(handler, thread.thread_id)
        handler._adapter.resume_thread_calls.clear()

        handler.handle_message("ou_user", "c1", "continue after recovery")

        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.resume_thread_calls],
            [thread.thread_id],
        )
        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.start_turn_calls],
            [thread.thread_id],
        )
        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"],
            "attached",
        )

    def test_feishu_goal_resume_is_not_blocked_by_a_prior_resume_unknown(self) -> None:
        handler, _bot = self._make_handler()
        thread = self._seed_authoritative_thread(handler, status="idle")
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals[thread.thread_id] = ThreadGoalSummary(
            thread_id=thread.thread_id,
            objective="continue safely",
            status="paused",
        )
        _detach_thread(handler, thread.thread_id)
        handler._adapter.unsubscribe_thread_calls.clear()
        self._seed_resume_outcome_unknown(handler, thread.thread_id)
        handler._adapter.resume_thread_calls.clear()

        handler._runtime_call(
            handler._goal_domain.resume_goal_on_runtime,
            "ou_user",
            "c1",
            thread.thread_id,
            False,
        )

        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.resume_thread_calls],
            [thread.thread_id],
        )
        self.assertEqual(
            handler._adapter.thread_goals[thread.thread_id].status,
            "active",
        )
        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"],
            "detached",
        )

    def test_loaded_goal_resume_is_not_blocked_by_a_prior_resume_unknown(self) -> None:
        handler, _bot = self._make_handler()
        thread = self._seed_authoritative_thread(handler, status="idle")
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_goals[thread.thread_id] = ThreadGoalSummary(
            thread_id=thread.thread_id,
            objective="remain blocked",
            status="paused",
        )
        _detach_thread(handler, thread.thread_id)
        self._seed_resume_outcome_unknown(handler, thread.thread_id)
        handler._adapter.loaded_thread_ids.add(thread.thread_id)
        handler._adapter.resume_thread_calls.clear()
        handler._adapter.set_thread_goal_calls.clear()
        handler._adapter.update_thread_settings_calls.clear()
        access_policy_calls: list[str] = []
        original_access_check = handler._thread_access_policy.prompt_write_denial_check

        def _record_access_check(*args, **kwargs):
            access_policy_calls.append(str(args[2]))
            return original_access_check(*args, **kwargs)

        handler._thread_access_policy.prompt_write_denial_check = _record_access_check

        handler._runtime_call(
            handler._goal_domain.resume_goal_on_runtime,
            "ou_user",
            "c1",
            thread.thread_id,
            False,
        )

        self.assertTrue(access_policy_calls)
        self.assertEqual(set(access_policy_calls), {thread.thread_id})
        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(
            handler._adapter.set_thread_goal_calls[-1]["status"],
            "active",
        )
        self.assertEqual(
            handler._adapter.thread_goals[thread.thread_id].status,
            "active",
        )

    def test_feishu_attach_does_not_bypass_web_main_turn_writer(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")
        holder = make_web_interaction_holder(
            "web-document", owner_pid=os.getpid()
        )
        self._activate_main_turn_lease(handler, "thread-1", holder)

        handler.handle_message("ou_user", "c1", "/attach")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertIn("另一终端执行", bot.replies[-1][1])

    def test_persisted_attached_binding_hydrates_as_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        handler2, _ = self._make_handler(data_dir=data_dir)
        state2 = _runtime_state(handler2, "ou_user", "c1")
        self.assertEqual(state2["current_thread_id"], "thread-1")
        self.assertEqual(state2["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._binding_runtime_coordinator.thread_subscribers("thread-1"), ())

        handler2.handle_message("ou_user", "c1", "hello")

        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(_runtime_state(handler2, "ou_user", "c1")["feishu_runtime_state"], "attached")

    def test_next_prompt_rejects_when_other_running_instance_still_reports_loaded(self) -> None:
        handler, bot = self._make_handler()
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        _detach_thread(handler, "thread-1")

        with patch(
            "bot.focus_runtime.service_authority.preview_thread_global_loaded_gate",
            return_value=SimpleNamespace(
                allowed=False,
                reason_code="prompt_denied_by_live_runtime_owner",
                reason_text=(
                    "当前 thread 仍由运行中的实例 `explorer` 保持为 loaded (`idle`)；"
                    "当前按 fail-close 拒绝跨实例继续。"
                ),
                blocking_instance="explorer",
                blocking_status="idle",
            ),
        ):
            handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), ())
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertIn("拒绝跨实例继续", bot.replies[-1][1])

    def test_lifecycle_loaded_gate_reports_blocking_focus_instance(self) -> None:
        handler, _ = self._make_handler()

        with patch(
            "bot.focus_runtime.service_authority.inspect_thread_global_loaded_presence",
            return_value=SimpleNamespace(
                verified_clear=False,
                blocking_instance="explorer",
                blocking_status="idle",
                diagnostic="",
            ),
        ) as mock_inspect:
            check = handler._service_runtime_authority.lifecycle_loaded_gate_check(
                "thread-1",
                "archive",
            )

        self.assertFalse(check.allowed)
        self.assertIn("实例 `explorer`", check.reason_text)
        self.assertIn("改在该实例执行", check.reason_text)
        mock_inspect.assert_called_once_with(
            thread_id="thread-1",
            registry_store=handler._instance_registry,
            excluded_instance_names=(handler._instance_name,),
        )

    def test_lifecycle_loaded_gate_fails_closed_when_other_instance_is_unverified(self) -> None:
        handler, _ = self._make_handler()

        with patch(
            "bot.focus_runtime.service_authority.inspect_thread_global_loaded_presence",
            return_value=SimpleNamespace(
                verified_clear=False,
                blocking_instance="explorer",
                blocking_status="unknown",
                diagnostic="control timeout",
            ),
        ):
            check = handler._service_runtime_authority.lifecycle_loaded_gate_check(
                "thread-1",
                "delete",
            )

        self.assertFalse(check.allowed)
        self.assertIn("无法确认", check.reason_text)
        self.assertIn("fail-close", check.reason_text)

    def test_denied_prompt_keeps_detached_binding_detached_when_all_mode_group_owns_thread(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-a"] = "group"
        bot.chat_types["chat-b"] = "group"
        bot.group_modes["chat-b"] = "all"
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _detach_thread(handler, "thread-1")
        _bind_authoritative_thread(handler, "ou_user2", "chat-b", thread)

        handler.handle_message("ou_user", "chat-a", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(
            _runtime_state(handler, "ou_user", "chat-a")["feishu_runtime_state"],
            "detached",
        )
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), (("__group__", "chat-b"),))
        self.assertIn("其他群聊独占", bot.replies[-1][1])

    def test_denied_prompt_keeps_detached_binding_detached_when_interaction_lease_is_external(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )

        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        _detach_thread(handler, "thread-1")
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )

        handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), ())
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")
        self.assertEqual(InteractionLeaseStore(data_dir).load("thread-1").holder.kind, "fcodex")
        self.assertIn("当前线程正由另一终端执行", bot.replies[-1][1])
