import unittest

from bot.adapters.base import ThreadGoalSummary, ThreadSummary
from bot.reason_codes import (
    PROMPT_DENIED_BINDING_NOT_FOUND,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    ReasonedCheck,
)
from bot.thread_lifecycle_service import (
    ThreadLifecyclePolicyError,
)
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeAdminControllerControlSurfaceTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_handle_service_control_request_thread_goal_reads_current_goal(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        self._bind_thread(lock, binding_runtime, ("ou_user", "c1"), thread_id="thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        controller._thread_goals["thread-1"] = ThreadGoalSummary(  # type: ignore[attr-defined]
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
            token_budget=100,
            tokens_used=12,
            time_used_seconds=34,
            created_at=1712476800,
            updated_at=1712476801,
        )

        result = controller.handle_service_control_request("thread/goal", {"thread_id": "thread-1"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["thread_title"], "demo")
        self.assertEqual(result["goal"]["objective"], "ship goal support")
        self.assertEqual(result["goal"]["status"], "active")
        self.assertEqual(result["goal"]["token_budget"], 100)

    def test_handle_service_control_request_thread_goal_set_updates_goal(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        result = controller.handle_service_control_request(
            "thread/goal/set",
            {
                "thread_id": "thread-1",
                "objective": "ship goal support",
                "status": "paused",
            },
        )

        self.assertEqual(result["goal"]["objective"], "ship goal support")
        self.assertEqual(result["goal"]["status"], "paused")

    def test_handle_service_control_request_thread_goal_pause_rejects_active_and_clear(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        controller._thread_goals["thread-1"] = ThreadGoalSummary(  # type: ignore[attr-defined]
            thread_id="thread-1",
            objective="ship goal support",
            status="active",
        )

        paused = controller.handle_service_control_request(
            "thread/goal/set",
            {"thread_id": "thread-1", "status": "paused"},
        )
        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "不能激活"):
            controller.handle_service_control_request(
                "thread/goal/set",
                {"thread_id": "thread-1", "status": "active"},
            )
        cleared = controller.handle_service_control_request("thread/goal/clear", {"thread_id": "thread-1"})

        self.assertEqual(paused["goal"]["status"], "paused")
        self.assertIsNone(cleared["goal"])
        self.assertTrue(cleared["cleared"])

    def test_control_plane_goal_mutations_require_an_unowned_root(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        controller._thread_goals["thread-1"] = ThreadGoalSummary(  # type: ignore[attr-defined]
            thread_id="thread-1",
            objective="do not change",
            status="active",
        )
        controller._external_control_write_denial_check = lambda _thread_id, _holder=None: ReasonedCheck.deny(  # type: ignore[attr-defined]
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            "当前 root operation 正由另一前端执行；本机控制面可继续查看，但不能修改。",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一前端"):
            controller.handle_service_control_request(
                "thread/goal/set",
                {"thread_id": "thread-1", "status": "paused"},
            )
        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一前端"):
            controller.handle_service_control_request(
                "thread/goal/clear",
                {"thread_id": "thread-1"},
            )

        goal = controller._thread_goals["thread-1"]  # type: ignore[attr-defined]
        self.assertEqual(goal.objective, "do not change")
        self.assertEqual(goal.status, "active")

    def test_control_plane_lifecycle_mutations_require_an_unowned_root(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        controller._thread_lifecycle._external_write_denial_check = lambda _thread_id, _holder=None: ReasonedCheck.deny(  # type: ignore[attr-defined]
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            "当前 root operation 正由另一前端执行；本机控制面可继续查看，但不能修改。",
        )

        for method in ("thread/archive", "thread/unarchive", "thread/delete"):
            with self.subTest(method=method):
                with self.assertRaisesRegex(ThreadLifecyclePolicyError, "另一前端"):
                    controller.handle_service_control_request(
                        method,
                        {"thread_id": "thread-1"},
                    )

        self.assertEqual(archived, [])
        self.assertEqual(controller._unarchived, [])  # type: ignore[attr-defined]
        self.assertEqual(controller._deleted, [])  # type: ignore[attr-defined]

    def test_handle_service_control_request_thread_archive_dispatches_control_action(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        result = controller.handle_service_control_request("thread/archive", {"thread_id": "thread-1"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(archived, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_handle_service_control_request_reports_loaded_status_without_reading_archived_thread(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        controller._thread_lifecycle._read_thread = lambda thread_id: (_ for _ in ()).throw(
            AssertionError(f"not-loaded status must not read thread: {thread_id}")
        )

        result = controller.handle_service_control_request(
            "thread/loaded-status",
            {"thread_id": "thread-archived"},
        )

        self.assertEqual(result["thread_id"], "thread-archived")
        self.assertEqual(result["backend_thread_status"], "notLoaded")

    def test_handle_service_control_request_thread_archive_requires_resolved_id(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()

        with self.assertRaisesRegex(ValueError, "thread/archive 缺少 thread_id"):
            controller.handle_service_control_request(
                "thread/archive",
                {"thread_name": "demo"},
            )

    def test_handle_service_control_request_clear_archived_bindings_dispatches_local_cleanup(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = self._direct_root_summary("thread-1")

        result = controller.handle_service_control_request(
            "thread/clear-archived-bindings",
            {"thread_id": "thread-1"},
        )

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(archived, [])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_handle_service_control_request_clear_archived_bindings_supports_dry_run(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = self._direct_root_summary("thread-1")

        result = controller.handle_service_control_request(
            "thread/clear-archived-bindings",
            {"thread_id": "thread-1", "dry_run": True},
        )

        self.assertEqual(
            result,
            {
                "thread_id": "thread-1",
                "would_clear_binding_ids": ["p2p:ou_user:c1"],
                "dry_run": True,
            },
        )
        self.assertEqual(archived, [])
        self.assertEqual(released_runtime_leases, [])
        with lock:
            self.assertEqual(binding_runtime.bound_bindings_for_thread_locked("thread-1"), [binding])

    def test_handle_service_control_request_binding_clear_stale_dispatches_local_cleanup(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_stale", "chat-stale")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-stale")
        controller._binding_application._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller.handle_service_control_request("binding/clear-stale", {"dry_run": False})

        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_stale:chat-stale"])
        self.assertEqual(released_runtime_leases, ["thread-stale"])

    def test_binding_clear_stale_retains_readable_not_loaded_thread(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "chat-live")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-readable")
        summaries["thread-readable"] = ThreadSummary(
            thread_id="thread-readable",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )

        result = controller.handle_service_control_request("binding/clear-stale", {"dry_run": True})

        self.assertEqual(result["would_clear_binding_ids"], [])
        self.assertEqual(result["retained_thread_ids"], ["thread-readable"])
        self.assertEqual(released_runtime_leases, [])
        with lock:
            self.assertEqual(binding_runtime.bound_bindings_for_thread_locked("thread-readable"), [binding])

    def test_binding_clear_stale_clears_thread_not_loaded_lookup_error(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "chat-stale")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-stale")

        def _raise_not_loaded(_thread_id: str):
            raise RuntimeError("thread not loaded: thread-stale")

        controller._binding_application._read_thread_for_stale_cleanup = _raise_not_loaded
        controller._binding_application._is_thread_not_loaded_error = lambda exc: str(exc).startswith("thread not loaded:")

        result = controller.handle_service_control_request("binding/clear-stale", {"dry_run": False})

        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:chat-stale"])
        self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
        self.assertEqual(released_runtime_leases, ["thread-stale"])

    def test_handle_service_control_request_thread_send_image_fanouts_to_attached_bindings(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            sent_images,
        ) = self._make_controller()
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        result = controller.handle_service_control_request(
            "thread/send-image",
            {
                "thread_id": "thread-1",
                "local_path": "/tmp/generated.png",
            },
        )

        self.assertTrue(result["fully_delivered"])
        self.assertEqual(result["delivered_binding_ids"], ["p2p:ou_user:c1", "p2p:ou_user2:c2"])
        self.assertEqual(result["failed_binding_ids"], [])
        self.assertEqual(
            sent_images,
            [("c1", "img-key-1"), ("c2", "img-key-1")],
        )

    def test_handle_service_control_request_binding_submit_prompt_dispatches_callback(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        result = controller.handle_service_control_request(
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续执行",
                "synthetic_source": "schedule",
                "display_mode": "announce",
            },
        )

        self.assertTrue(result["started"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["turn_id"], "turn-1")
        submitted_prompts = getattr(controller, "_submitted_prompts")
        self.assertEqual(len(submitted_prompts), 1)
        self.assertEqual(submitted_prompts[0]["binding"], ("ou_user", "c1"))
        self.assertEqual(submitted_prompts[0]["text"], "继续执行")
        self.assertEqual(submitted_prompts[0]["synthetic_source"], "schedule")
        self.assertEqual(submitted_prompts[0]["display_mode"], "announce")

    def test_handle_service_control_request_binding_submit_prompt_defers_running_check_to_admission(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        state["running"] = True
        state["current_turn_id"] = "turn-1"
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="active",
        )

        result = controller.handle_service_control_request(
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续执行",
            },
        )

        self.assertTrue(result["started"])
        submitted_prompts = getattr(controller, "_submitted_prompts")
        self.assertEqual(len(submitted_prompts), 1)
        self.assertEqual(submitted_prompts[0]["binding"], ("ou_user", "c1"))
        self.assertEqual(submitted_prompts[0]["text"], "继续执行")

    def test_handle_service_control_request_binding_submit_prompt_rejects_missing_binding(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()

        result = controller.handle_service_control_request(
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_typo:chat-typo",
                "text": "继续执行",
            },
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], PROMPT_DENIED_BINDING_NOT_FOUND)
        self.assertEqual(result["reason"], "未找到 binding：p2p:ou_typo:chat-typo")
        self.assertEqual(getattr(controller, "_submitted_prompts"), [])
        with lock:
            self.assertIsNone(binding_runtime.binding_runtime_snapshot_locked(("ou_typo", "chat-typo")))


if __name__ == "__main__":
    unittest.main()
