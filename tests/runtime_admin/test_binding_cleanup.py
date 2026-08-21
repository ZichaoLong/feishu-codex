import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSummary
from bot.thread_lifecycle_service import (
    ThreadLifecyclePolicyError,
)
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeAdminControllerBindingCleanupTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_clear_archived_thread_bindings_for_control_clears_without_archiving(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-1")
        summaries["thread-1"] = self._direct_root_summary("thread-1")

        result = controller.clear_archived_thread_bindings_for_control("thread-1")

        self.assertEqual(archived, [])
        self.assertEqual(
            result,
            {
                "thread_id": "thread-1",
                "cleared_binding_ids": ["p2p:ou_user:c1", "p2p:ou_user2:c2"],
                "cleared": True,
            },
        )
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])
        with lock:
            self.assertEqual(binding_runtime.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(
            getattr(controller, "_invalidated_queues"),
            [binding_a, binding_b],
        )

    def test_clear_archived_thread_bindings_for_control_dry_run_does_not_clear(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
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

        result = controller.clear_archived_thread_bindings_for_control("thread-1", dry_run=True)

        self.assertEqual(archived, [])
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])
        self.assertEqual(
            result,
            {
                "thread_id": "thread-1",
                "would_clear_binding_ids": ["p2p:ou_user:c1"],
                "dry_run": True,
            },
        )
        with lock:
            self.assertEqual(binding_runtime.bound_bindings_for_thread_locked("thread-1"), [binding])

    def test_clear_archived_thread_bindings_for_control_rejects_running_binding(self) -> None:
        (
            lock,
            binding_runtime,
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
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        with lock:
            state["current_turn_id"] = "turn-1"

        with self.assertRaisesRegex(ValueError, "正在运行"):
            controller.clear_archived_thread_bindings_for_control("thread-1")

        self.assertEqual(archived, [])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))

    def test_clear_archived_thread_bindings_rejects_thread_spawn_before_local_cleanup(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="child-1")
        summaries["child-1"] = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=0,
            updated_at=0,
            source="subAgent",
            status="idle",
            parent_thread_id="root-1",
            subagent_kind="threadSpawn",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "ThreadSpawn"):
            controller.clear_archived_thread_bindings_for_control("child-1")

        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])

    def test_clear_stale_bindings_for_control_dry_run_keeps_bindings(self) -> None:
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
        live_binding = ("ou_live", "chat-live")
        stale_binding = ("ou_stale", "chat-stale")
        self._bind_thread(lock, binding_runtime, live_binding, thread_id="thread-live")
        self._bind_thread(lock, binding_runtime, stale_binding, thread_id="thread-stale")
        summaries["thread-live"] = ThreadSummary(
            thread_id="thread-live",
            cwd="/tmp/project",
            name="live",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        controller._binding_application._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller._binding_application.clear_stale_bindings_for_control(
            dry_run=True
        )

        self.assertEqual(result["would_clear_binding_ids"], ["p2p:ou_stale:chat-stale"])
        self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(live_binding))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(stale_binding))

    def test_stale_dry_run_inspects_store_only_binding_without_hydration(self) -> None:
        owner_losses = []
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller(owner_loss_observer=owner_losses.append)
        binding = ("ou_recovery", "chat-recovery")
        binding_runtime._chat_binding_store.save(
            binding,
            {
                "working_dir": "/tmp/recovery",
                "current_thread_id": "thread-recovery",
                "current_thread_title": "Recovery",
                "feishu_runtime_state": "attached",
                "approval_policy": "never",
                "permissions_profile_id": "danger-full-access",
                "model": "",
                "reasoning_effort": "",
                "configured_settings": [],
            },
        )
        binding_runtime.acquire_interaction_lease_for_binding(
            binding,
            "thread-recovery",
        )
        controller._binding_application._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller._binding_application.clear_stale_bindings_for_control(
            dry_run=True
        )

        self.assertEqual(
            result["would_clear_binding_ids"],
            ["p2p:ou_recovery:chat-recovery"],
        )
        with lock:
            self.assertIsNone(
                binding_runtime.binding_runtime_snapshot_locked(binding)
            )
        stored = binding_runtime._chat_binding_store.load(binding)
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(owner_losses, [])
        self.assertEqual(
            binding_runtime.interaction_owner_snapshot_locked(
                "thread-recovery",
                current_binding=binding,
            )["relation"],
            "current",
        )
        self.assertEqual(binding_runtime.thread_subscribers("thread-recovery"), ())
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])
        self.assertEqual(getattr(controller, "_invalidated_queues"), [])

    def test_clear_stale_bindings_for_control_clears_missing_thread_bindings(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        live_binding = ("ou_live", "chat-live")
        stale_binding = ("ou_stale", "chat-stale")
        self._bind_thread(lock, binding_runtime, live_binding, thread_id="thread-live")
        self._bind_thread(lock, binding_runtime, stale_binding, thread_id="thread-stale")
        summaries["thread-live"] = ThreadSummary(
            thread_id="thread-live",
            cwd="/tmp/project",
            name="live",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        controller._binding_application._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller._binding_application.clear_stale_bindings_for_control()

        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_stale:chat-stale"])
        self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
        self.assertEqual(result["retained_thread_ids"], ["thread-live"])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(live_binding))
            self.assertIsNone(binding_runtime.binding_runtime_snapshot_locked(stale_binding))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, ["thread-stale"])
        self.assertEqual(
            getattr(controller, "_invalidated_queues"),
            [stale_binding],
        )

    def test_clear_stale_bindings_retains_an_authoritatively_read_thread_spawn(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "chat-child")
        self._bind_thread(lock, binding_runtime, binding, thread_id="child-1")
        summaries["child-1"] = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=0,
            updated_at=0,
            source="subAgent",
            status="idle",
            parent_thread_id="root-1",
            subagent_kind="threadSpawn",
        )

        result = controller._binding_application.clear_stale_bindings_for_control()

        self.assertEqual(result["cleared_binding_ids"], [])
        self.assertEqual(result["retained_thread_ids"], ["child-1"])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])

    def test_clear_stale_bindings_owner_settlement_failure_prevents_all_local_cleanup(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding_a = ("ou_a", "chat-a")
        binding_b = ("ou_b", "chat-b")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-a")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-b")
        binding_runtime.acquire_interaction_lease_for_binding(binding_a, "thread-a")
        binding_runtime.acquire_interaction_lease_for_binding(binding_b, "thread-b")
        controller._binding_application._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)
        original_release = (
            binding_runtime._interaction_lease_store.release_if_matches
        )

        def _release(expected):
            if expected.thread_id == "thread-b":
                raise OSError("lease cleanup failed")
            return original_release(expected)

        with patch.object(
            binding_runtime._interaction_lease_store,
            "release_if_matches",
            side_effect=_release,
        ):
            with self.assertRaisesRegex(RuntimeError, "lease cleanup failed"):
                controller._binding_application.clear_stale_bindings_for_control()

        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding_a))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding_b))
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_a))
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_b))
        self.assertIsNone(binding_runtime._interaction_lease_store.load("thread-a"))
        self.assertIsNotNone(binding_runtime._interaction_lease_store.load("thread-b"))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])
        self.assertEqual(getattr(controller, "_invalidated_queues"), [])

    def test_clear_all_bindings_for_control_rejects_when_binding_has_pending_request(self) -> None:
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
            pending_by_binding,
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
        pending_by_binding.add(binding)

        with self.assertRaises(ValueError) as ctx:
            controller.clear_all_bindings_for_control()

        self.assertIn("p2p:ou_user:c1", str(ctx.exception))
        self.assertIn("不能清除 binding", str(ctx.exception))

    def test_clear_all_bindings_rejects_thread_spawn_before_any_binding_is_cleared(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        root_binding = ("ou_root", "chat-root")
        child_binding = ("ou_child", "chat-child")
        self._bind_thread(lock, binding_runtime, root_binding, thread_id="root-1")
        self._bind_thread(lock, binding_runtime, child_binding, thread_id="child-1")
        summaries["root-1"] = self._direct_root_summary("root-1")
        summaries["child-1"] = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=0,
            updated_at=0,
            source="subAgent",
            status="idle",
            parent_thread_id="root-1",
            subagent_kind="threadSpawn",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "ThreadSpawn"):
            controller.clear_all_bindings_for_control()

        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(root_binding))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(child_binding))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])

    def test_clear_all_bindings_for_control_rolls_back_when_batch_clear_fails(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-2")
        summaries["thread-1"] = self._direct_root_summary("thread-1")
        summaries["thread-2"] = self._direct_root_summary("thread-2")

        with patch.object(
            binding_runtime._chat_binding_store,
            "clear",
            side_effect=[None, RuntimeError("store clear failed")],
        ):
            with self.assertRaisesRegex(RuntimeError, "store clear failed"):
                controller.clear_all_bindings_for_control()

        with lock:
            snapshot_a = binding_runtime.binding_runtime_snapshot_locked(binding_a)
            snapshot_b = binding_runtime.binding_runtime_snapshot_locked(binding_b)
        assert snapshot_a is not None
        assert snapshot_b is not None
        self.assertEqual(snapshot_a.feishu_runtime_state, "attached")
        self.assertEqual(snapshot_b.feishu_runtime_state, "attached")
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])

    def test_clear_all_bindings_for_control_clears_store_only_stale_binding(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            _archived,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        live_binding = ("ou_live", "chat-live")
        stale_binding = ("ou_stale", "chat-stale")
        self._bind_thread(lock, binding_runtime, live_binding, thread_id="thread-live")
        summaries["thread-live"] = self._direct_root_summary("thread-live")
        summaries["thread-stale"] = self._direct_root_summary("thread-stale")
        binding_runtime._chat_binding_store.save(
            stale_binding,
            {
                "working_dir": "/tmp/stale",
                "current_thread_id": "thread-stale",
                "current_thread_title": "Stale",
                "feishu_runtime_state": "detached",
                "approval_policy": "never",
                "sandbox": "danger-full-access",
                "model": "",
            },
        )

        result = controller.clear_all_bindings_for_control()

        self.assertFalse(result["already_empty"])
        self.assertEqual(
            result["cleared_binding_ids"],
            ["p2p:ou_live:chat-live", "p2p:ou_stale:chat-stale"],
        )
        with lock:
            self.assertEqual(binding_runtime.binding_keys_locked(), ())
        self.assertEqual(unsubscribed, ["thread-live"])
        self.assertEqual(released_runtime_leases, ["thread-live"])
        self.assertEqual(binding_runtime._chat_binding_store.load_all(), {})
        self.assertEqual(
            getattr(controller, "_invalidated_queues"),
            [live_binding, stale_binding],
        )


if __name__ == "__main__":
    unittest.main()
