import unittest

from bot.adapters.base import ThreadSummary
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.reason_codes import (
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    DETACH_BLOCKED_BY_PENDING_REQUEST,
    ReasonedCheck,
)
from bot.runtime_state import ThreadStateChanged
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease, ThreadRuntimeLeaseHolder
from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeAdminControllerBindingStateTests(
    RuntimeAdminControllerHarnessMixin, unittest.TestCase
):
    def test_binding_list_uses_cached_chat_and_authoritative_thread_name_only(self) -> None:
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
        p2p_binding = ("ou_user", "oc_p2p")
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "oc_group")
        self._bind_thread(lock, binding_runtime, p2p_binding, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, group_binding, thread_id="thread-2")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="Renamed in Codex",
            preview="first prompt must not matter",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="",
            preview="fallback preview must not be displayed",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        getattr(controller, "_chat_display_names").update(
            {
                ("p2p", "ou_user", "oc_p2p"): "Alice",
                ("group", GROUP_SHARED_BINDING_OWNER_ID, "oc_group"): "Project Group",
            }
        )

        result = controller.handle_service_control_request("binding/list", {})

        bindings = {item["binding_id"]: item for item in result["bindings"]}
        self.assertEqual(bindings["p2p:ou_user:oc_p2p"]["chat_display_name"], "Alice")
        self.assertEqual(bindings["p2p:ou_user:oc_p2p"]["thread_name"], "Renamed in Codex")
        self.assertEqual(bindings["group:oc_group"]["chat_display_name"], "Project Group")
        self.assertEqual(bindings["group:oc_group"]["thread_name"], "")
        self.assertEqual(result["chat_display_name_cache_miss_count"], 0)
        self.assertNotIn("fallback preview", str(result))

    def test_binding_list_deduplicates_p2p_display_name_refresh_by_sender(self) -> None:
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
        p2p_binding_a = ("ou_user", "oc_direct_a")
        p2p_binding_b = ("ou_user", "oc_direct_b")
        self._bind_thread(lock, binding_runtime, p2p_binding_a, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, p2p_binding_b, thread_id="thread-2")
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo 1",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="demo 2",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        getattr(controller, "_chat_display_names").update(
            {
                ("p2p", "ou_user", "oc_direct_a"): "Alice",
                ("p2p", "ou_user", "oc_direct_b"): "Alice",
            }
        )

        result = controller.handle_service_control_request("binding/list", {"refresh_names": True})

        bindings = {item["binding_id"]: item for item in result["bindings"]}
        self.assertEqual(bindings["p2p:ou_user:oc_direct_a"]["chat_display_name"], "Alice")
        self.assertEqual(bindings["p2p:ou_user:oc_direct_b"]["chat_display_name"], "Alice")
        display_name_calls = getattr(controller, "_chat_display_name_calls")
        self.assertEqual(
            [
                (call["binding_kind"], call["sender_id"], call["chat_id"], call["refresh_names"])
                for call in display_name_calls
            ],
            [("p2p", "ou_user", "oc_direct_a", True)],
        )

    def test_detach_thread_availability_locked_blocks_on_pending_request(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            pending_by_thread,
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
            status="notLoaded",
        )
        pending_by_thread.add("thread-1")

        application = controller._binding_application
        allowed, reason = application.detach_thread_availability_locked("thread-1")

        self.assertFalse(allowed)
        self.assertIn("审批或输入请求未处理", reason)
        check = application.detach_thread_check_locked("thread-1")
        self.assertEqual(check.reason_code, DETACH_BLOCKED_BY_PENDING_REQUEST)

    def test_unsubscribe_by_thread_id_marks_binding_detached_and_unsubscribes(self) -> None:
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
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
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

        result = controller.detach_thread("thread-1")

        self.assertTrue(result["changed"])
        self.assertEqual(result["detached_binding_ids"], ["p2p:ou_user:c1"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "detached")
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])
        self.assertEqual(getattr(controller, "_invalidated_queues"), [binding])

    def test_detach_binding_clears_its_owner_fifo_after_successful_detach(self) -> None:
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
        self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        summaries["thread-1"] = self._direct_root_summary("thread-1")

        result = controller.detach_binding(binding)

        self.assertTrue(result["changed"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "detached")
        self.assertEqual(getattr(controller, "_invalidated_queues"), [binding])
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_unsubscribe_by_thread_id_keeps_binding_attached_when_backend_unsubscribe_fails(self) -> None:
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

        def _fail_unsubscribe(thread_id: str) -> None:
            unsubscribed.append(thread_id)
            raise RuntimeError("backend unsubscribe failed")

        controller._binding_application._unsubscribe_thread = _fail_unsubscribe

        with self.assertRaisesRegex(RuntimeError, "backend unsubscribe failed"):
            controller.detach_thread("thread-1")

        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "attached")
        self.assertEqual(binding_runtime.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, [])

        controller._binding_application._unsubscribe_thread = lambda thread_id: unsubscribed.append(f"retry:{thread_id}")
        result = controller.detach_thread("thread-1")

        self.assertTrue(result["changed"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "detached")
        self.assertEqual(unsubscribed, ["thread-1", "retry:thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_detach_thread_settlement_failure_prevents_backend_unsubscribe(self) -> None:
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("retained store unavailable")

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
        ) = self._make_controller(owner_loss_observer=_fail_owner_loss)
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
        with self.assertRaisesRegex(RuntimeError, "retained store unavailable"):
            controller.detach_thread("thread-1")

        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(len(owner_losses), 1)
        self.assertEqual(owner_losses[0].reason, "binding_detached")
        self.assertEqual(snapshot.feishu_runtime_state, "attached")
        self.assertEqual(binding_runtime.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])

    def test_fail_close_service_attached_runtime_downgrades_attached_without_backend_unsubscribe(self) -> None:
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
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo-1",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="demo-2",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )

        result = controller.fail_close_service_attached_runtime()

        self.assertCountEqual(
            result["detached_binding_ids"],
            ["p2p:ou_user:c1", "p2p:ou_user2:c2"],
        )
        self.assertEqual(result["detached_thread_ids"], ["thread-1", "thread-2"])
        self.assertEqual(result["released_thread_ids"], ["thread-1", "thread-2"])
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, ["thread-1", "thread-2"])
        with lock:
            snapshot_a = binding_runtime.binding_runtime_snapshot_locked(binding_a)
            snapshot_b = binding_runtime.binding_runtime_snapshot_locked(binding_b)
        assert snapshot_a is not None
        assert snapshot_b is not None
        self.assertEqual(snapshot_a.feishu_runtime_state, "detached")
        self.assertEqual(snapshot_b.feishu_runtime_state, "detached")
        self.assertCountEqual(
            getattr(controller, "_invalidated_queues"),
            [binding_a, binding_b],
        )

    def test_thread_status_snapshot_exposes_machine_global_live_runtime_owner(self) -> None:
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
            status="notLoaded",
        )
        controller._binding_application._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(
                ThreadRuntimeLeaseHolder(
                    holder_id="service:svc-token",
                    holder_type="service",
                    instance_name="explorer",
                    owner_pid=4321,
                    owner_service_token="svc-token",
                    control_endpoint="tcp://127.0.0.1:32001",
                    backend_url="ws://127.0.0.1:8765",
                    updated_at=1.0,
                ),
            ),
        )

        snapshot = controller._binding_application.thread_status_snapshot(
            "thread-1"
        )

        self.assertEqual(snapshot["backend_thread_status"], "notLoaded")
        self.assertEqual(snapshot["live_runtime_owner"]["label"], "explorer")
        self.assertEqual(snapshot["live_runtime_holder_labels"], ["service@explorer(pid=4321)"])

    def test_handle_service_control_request_thread_bindings_reports_attached_and_detached(self) -> None:
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
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        state_b = self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-1")
        with lock:
            binding_runtime.unsubscribe_thread_locked(binding_b, "thread-1")
            binding_runtime._apply_persisted_runtime_state_message_locked(
                binding_b,
                state_b,
                ThreadStateChanged(feishu_runtime_state="detached"),
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

        result = controller.handle_service_control_request("thread/bindings", {"thread_id": "thread-1"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(
            result["bindings"],
            [
                {"binding_id": "p2p:ou_user:c1", "feishu_runtime_state": "attached"},
                {"binding_id": "p2p:ou_user2:c2", "feishu_runtime_state": "detached"},
            ],
        )

    def test_binding_status_snapshot_includes_prompt_and_detach_reason_codes(self) -> None:
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
        controller._binding_application._prompt_write_denial_check = lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.deny(
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            "当前线程正由另一飞书会话执行；本会话可继续查看，但暂时不能写入。待对方执行结束后再试。",
        )
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

        snapshot = controller.binding_status_snapshot(binding)

        self.assertFalse(snapshot["next_prompt_allowed"])
        self.assertEqual(snapshot["next_prompt_reason_code"], PROMPT_DENIED_BY_INTERACTION_OWNER)
        self.assertFalse(snapshot["detach_available"])
        self.assertEqual(snapshot["detach_reason_code"], DETACH_BLOCKED_BY_PENDING_REQUEST)
        self.assertEqual(snapshot["operator_status"]["status"], "ok")
        markdown, _template = controller.render_binding_status_markdown(
            snapshot,
            include_profile_lines=True,
        )
        self.assertIn("运行健康：`ok`；当前进程告警：`0`", markdown)

    def test_handle_preflight_command_renders_next_prompt_and_unsubscribe_checks(self) -> None:
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
        controller._binding_application._prompt_write_denial_check = lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.deny(
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            "当前线程正由另一飞书会话执行；本会话可继续查看，但暂时不能写入。待对方执行结束后再试。",
        )
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

        result = controller.handle_preflight_command(binding, "")

        card = result.card
        assert card is not None
        content = card["elements"][0]["content"]
        self.assertIn("作用对象：当前 chat binding；这是 dry-run", content)
        self.assertIn("下一条普通消息：`blocked` (`prompt_denied_by_interaction_owner`)", content)
        self.assertIn("detach：`blocked` (`detach_blocked_by_pending_request`)", content)

    def test_unsubscribe_failure_keeps_runtime_lease_fail_closed(self) -> None:
        (
            _lock,
            _binding_runtime,
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
        controller._binding_application._unsubscribe_thread = lambda _thread_id: (_ for _ in ()).throw(
            RuntimeError("unload failed")
        )
        cleanup_errors: list[str] = []

        controller._binding_application._finalize_deactivated_thread_runtime(
            ["thread-1"],
            cleanup_errors=cleanup_errors,
        )

        self.assertEqual(released_runtime_leases, [])
        self.assertEqual(len(cleanup_errors), 1)
        self.assertIn("unload failed", cleanup_errors[0])


if __name__ == "__main__":
    unittest.main()
