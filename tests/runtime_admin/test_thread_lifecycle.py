import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.reason_codes import (
    ReasonedCheck,
)
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease, ThreadRuntimeLeaseHolder
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeAdminControllerThreadLifecycleTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_archive_thread_for_control_archives_and_clears_current_instance_bindings(self) -> None:
        owner_losses = []
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
        ) = self._make_controller(owner_loss_observer=owner_losses.append)
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
        result = controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(archived, ["thread-1"])
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])
        self.assertEqual({event.disposition for event in owner_losses}, {"terminal"})
        self.assertEqual({event.reason for event in owner_losses}, {"binding_deactivated"})
        self.assertEqual(
            getattr(controller, "_invalidated_queues"),
            [binding_a, binding_b],
        )

    def test_lifecycle_mutations_reject_cross_instance_loaded_gate_blocker(self) -> None:
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
        controller._thread_lifecycle._loaded_gate_check = lambda thread_id, operation: ReasonedCheck.deny(
            "lifecycle_blocked_by_loaded_thread",
            f"blocked {operation}: {thread_id}",
        )

        for operation, call in (
            ("archive", lambda: controller.archive_thread_for_control("thread-1")),
            ("unarchive", lambda: controller.unarchive_thread_for_control("thread-1")),
            ("delete", lambda: controller.delete_thread_for_control("thread-1")),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, f"blocked {operation}"):
                    call()

        self.assertEqual(archived, [])
        self.assertEqual(controller._unarchived, [])  # type: ignore[attr-defined]
        self.assertEqual(controller._deleted, [])  # type: ignore[attr-defined]

    def test_unarchive_rejects_current_instance_loaded_copy(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        loaded_thread_ids.append("thread-1")
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

        with self.assertRaisesRegex(ValueError, "当前目标实例仍将该 thread 保持为 loaded"):
            controller.unarchive_thread_for_control("thread-1")

        self.assertEqual(controller._unarchived, [])  # type: ignore[attr-defined]

    def test_loaded_thread_status_uses_loaded_inventory_before_read(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()

        self.assertEqual(
            controller.loaded_thread_status_for_control("thread-1")["backend_thread_status"],
            "notLoaded",
        )

        loaded_thread_ids.append("thread-1")
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
        self.assertEqual(
            controller.loaded_thread_status_for_control("thread-1")["backend_thread_status"],
            "active",
        )

    def test_archive_thread_for_control_clears_store_only_binding(self) -> None:
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
        binding = ("ou_stale", "c1")
        binding_runtime._chat_binding_store.save(
            binding,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": "thread-1",
                "current_thread_title": "demo",
                "feishu_runtime_state": "detached",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "",
                "reasoning_effort": "",
            },
        )
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

        result = controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(archived, ["thread-1"])
        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_stale:c1"])
        self.assertEqual(binding_runtime._chat_binding_store.load(binding), None)
        with lock:
            self.assertEqual(binding_runtime.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_archive_thread_timeout_returns_unknown_without_cleanup(self) -> None:
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
        controller._thread_lifecycle._archive_thread = lambda thread_id: (_ for _ in ()).throw(
            TimeoutError(thread_id)
        )

        result = controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(result["upstream_outcome"], "unknown")
        self.assertEqual(result["focus_cleanup"], "skipped")
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding))
        self.assertEqual(released_runtime_leases, [])

    def test_archive_thread_reports_incomplete_when_interaction_lease_cleanup_fails(self) -> None:
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
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        binding_runtime.acquire_interaction_lease_for_binding(binding, "thread-1")
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

        with patch.object(
            binding_runtime._interaction_lease_store,
            "release_if_matches",
            side_effect=OSError("lease cleanup failed"),
        ):
            result = controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "incomplete")
        self.assertIn("lease cleanup failed", result["cleanup_errors"][0])
        self.assertEqual(result["cleared_binding_ids"], [])
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding))
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))
        self.assertEqual(released_runtime_leases, [])

    def test_archive_thread_store_clear_failure_retains_root_runtime_lease(self) -> None:
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

        with patch.object(
            binding_runtime._chat_binding_store,
            "clear",
            side_effect=OSError("store clear failed"),
        ):
            result = controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "incomplete")
        self.assertIn("store clear failed", result["cleanup_errors"][0])
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding))
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))
        self.assertEqual(released_runtime_leases, [])

    def test_unarchive_thread_succeeds_without_creating_binding(self) -> None:
        (
            _lock,
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
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )

        result = controller.unarchive_thread_for_control("thread-1")

        self.assertEqual(controller._unarchived, ["thread-1"])  # type: ignore[attr-defined]
        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "skipped")
        self.assertEqual(binding_runtime.binding_keys_locked(), ())

    def test_unarchive_thread_rejects_residual_local_binding(self) -> None:
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
            status="notLoaded",
        )

        with self.assertRaisesRegex(ValueError, "仍有 binding"):
            controller.unarchive_thread_for_control("thread-1")

        self.assertEqual(controller._unarchived, [])  # type: ignore[attr-defined]

    def test_delete_thread_transport_error_returns_unknown_without_cleanup(self) -> None:
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
        controller._thread_lifecycle._delete_thread = lambda thread_id: (_ for _ in ()).throw(
            CodexRpcTransportError("thread/delete", {"message": f"disconnected: {thread_id}"})
        )

        result = controller.delete_thread_for_control("thread-1")

        self.assertEqual(result["upstream_outcome"], "unknown")
        self.assertEqual(result["focus_cleanup"], "skipped")
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding))

    def test_delete_thread_explicit_rpc_error_is_reported_as_upstream_error(self) -> None:
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
        controller._thread_lifecycle._delete_thread = lambda thread_id: (_ for _ in ()).throw(
            CodexRpcError("thread/delete", {"message": f"refused: {thread_id}"})
        )

        result = controller.delete_thread_for_control("thread-1")

        self.assertEqual(result["upstream_outcome"], "error")
        self.assertIn("refused", result["upstream_error"])

    def test_unarchive_thread_protocol_error_is_unknown(self) -> None:
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
        # `thread/read` is the authoritative direct-target check.  Archived
        # threads remain readable upstream, so model that fact in this focused
        # transport-outcome regression instead of bypassing the guard.
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
        controller._thread_lifecycle._unarchive_thread = lambda thread_id: (_ for _ in ()).throw(
            CodexRpcProtocolError("thread/unarchive", f"invalid response: {thread_id}")
        )

        result = controller.unarchive_thread_for_control("thread-1")

        self.assertEqual(result["upstream_outcome"], "unknown")
        self.assertEqual(result["focus_cleanup"], "skipped")
        self.assertIn("invalid response", result["outcome_detail"])
        self.assertEqual(result["upstream_error"], "")

    def test_archive_thread_pre_send_error_is_not_reported_as_unknown(self) -> None:
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
        controller._thread_lifecycle._archive_thread = lambda thread_id: (_ for _ in ()).throw(
            CodexRpcPreSendError("thread/archive", TimeoutError(f"initialize failed: {thread_id}"))
        )

        with self.assertRaises(CodexRpcPreSendError):
            controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

    def test_delete_thread_local_error_is_not_mislabeled_as_upstream_error(self) -> None:
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
        controller._thread_lifecycle._delete_thread = lambda thread_id: (_ for _ in ()).throw(
            OSError(f"local startup failed: {thread_id}")
        )

        with self.assertRaisesRegex(OSError, "local startup failed"):
            controller.delete_thread_for_control("thread-1")

    def test_delete_thread_rejects_active_root(self) -> None:
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
            status="active",
        )

        with self.assertRaisesRegex(ValueError, "backend 状态为 `active`"):
            controller.delete_thread_for_control("thread-1")

        self.assertEqual(controller._deleted, [])  # type: ignore[attr-defined]

    def test_delete_thread_rejects_backend_status_lookup_error(self) -> None:
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
            OSError(f"read failed: {thread_id}")
        )

        with patch("bot.thread_lifecycle_service.logger.exception"):
            with self.assertRaisesRegex(ValueError, "无法确认 root thread"):
                controller.delete_thread_for_control("thread-1")

        self.assertEqual(controller._deleted, [])  # type: ignore[attr-defined]

    def test_archive_and_delete_reject_same_instance_fcodex_holder(self) -> None:
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
        controller._thread_lifecycle._load_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="corp-a",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(
                ThreadRuntimeLeaseHolder(
                    holder_id="fcodex:123",
                    holder_type="fcodex",
                    instance_name="corp-a",
                    owner_pid=123,
                    owner_service_token="svc-token",
                    control_endpoint="tcp://127.0.0.1:32001",
                    backend_url="ws://127.0.0.1:8765",
                    updated_at=1.0,
                ),
            ),
        )

        for operation, call in (
            ("archive", lambda: controller.archive_thread_for_control("thread-1")),
            ("delete", lambda: controller.delete_thread_for_control("thread-1")),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "fcodex@corp-a"):
                    call()

        self.assertEqual(archived, [])
        self.assertEqual(controller._deleted, [])  # type: ignore[attr-defined]

    def test_delete_thread_success_clears_root_binding(self) -> None:
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
        controller._thread_lifecycle._load_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="corp-a",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(
                ThreadRuntimeLeaseHolder(
                    holder_id="service:svc-token",
                    holder_type="service",
                    instance_name="corp-a",
                    owner_pid=123,
                    owner_service_token="svc-token",
                    control_endpoint="tcp://127.0.0.1:32001",
                    backend_url="ws://127.0.0.1:8765",
                    updated_at=1.0,
                ),
            ),
        )

        result = controller.delete_thread_for_control("thread-1")

        self.assertEqual(controller._deleted, ["thread-1"])  # type: ignore[attr-defined]
        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "complete")
        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:c1"])
        self.assertIsNone(binding_runtime._chat_binding_store.load(binding))
        self.assertEqual(getattr(controller, "_invalidated_queues"), [binding])

    def test_archive_thread_for_control_rejects_other_instance_live_runtime_owner(self) -> None:
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
        controller._thread_lifecycle._load_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(),
        )

        with self.assertRaisesRegex(ValueError, "explorer"):
            controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(archived, [])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))

    def test_archive_thread_for_control_rejects_running_binding(self) -> None:
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
        state["running"] = True
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

        with self.assertRaisesRegex(ValueError, "飞书侧 turn 正在运行"):
            controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(archived, [])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))

    def test_archive_thread_for_control_rejects_pending_binding_request(self) -> None:
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
            pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        pending_by_binding.add(binding)
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

        with self.assertRaisesRegex(ValueError, "待处理审批或补充输入"):
            controller.archive_thread_for_control("thread-1", summary=summaries["thread-1"])

        self.assertEqual(archived, [])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))


if __name__ == "__main__":
    unittest.main()
