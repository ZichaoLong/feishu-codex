import pathlib
import tempfile
import threading
import types
import unittest

from bot.adapters.base import RuntimeConfigSummary, ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_admin_controller import RuntimeAdminController
from bot.runtime_state import ThreadStateChanged
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_lease_registry import ThreadLeaseRegistry


class RuntimeAdminControllerTests(unittest.TestCase):
    def _make_controller(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        chat_binding_store = ChatBindingStore(data_dir)
        binding_runtime = BindingRuntimeManager(
            lock=lock,
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_sandbox="workspace-write",
            default_collaboration_mode="default",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=chat_binding_store,
            thread_lease_registry=ThreadLeaseRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        unsubscribed: list[str] = []
        pending_by_thread: set[str] = set()
        pending_by_binding: set[tuple[str, str]] = set()
        summaries: dict[str, ThreadSummary] = {}
        loaded_thread_ids: list[str] = []

        def _read_thread(thread_id: str):
            return ThreadSnapshot(summary=summaries[thread_id])

        controller = RuntimeAdminController(
            lock=lock,
            binding_runtime=binding_runtime,
            interaction_requests=types.SimpleNamespace(
                thread_has_pending_request_locked=lambda thread_id: thread_id in pending_by_thread,
                binding_has_pending_request_locked=lambda binding: binding in pending_by_binding,
            ),
            clear_all_stored_bindings=chat_binding_store.clear_all,
            deactivate_binding_locked=lambda binding: binding_runtime.deactivate_binding_locked(binding),
            read_thread=_read_thread,
            list_loaded_thread_ids=lambda: list(loaded_thread_ids),
            current_app_server_url=lambda: "http://127.0.0.1:1234",
            unsubscribe_thread=lambda thread_id: unsubscribed.append(thread_id),
            service_control_socket_path=lambda: str(data_dir / "service.sock"),
            safe_read_runtime_config=lambda: RuntimeConfigSummary(current_model_provider="provider1"),
            current_default_profile_resolution=lambda runtime_config: types.SimpleNamespace(
                effective_profile="default",
                stale_profile="",
            ),
            permissions_summary=lambda approval_policy, sandbox: f"{sandbox}/{approval_policy}",
            resolve_thread_target_for_control_params=lambda params: ThreadSummary(
                thread_id=str(params.get("thread_id", "") or "").strip(),
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
            cancel_patch_timer_locked=lambda state: state.update({"patch_timer": None}),
            cancel_mirror_watchdog_locked=lambda state: state.update({"mirror_watchdog_timer": None}),
            is_thread_not_found_error=lambda exc: False,
        )
        return (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            unsubscribed,
            pending_by_thread,
            pending_by_binding,
        )

    def _bind_thread(self, lock, binding_runtime, binding, *, thread_id: str):
        with lock:
            state = binding_runtime.get_or_create_runtime_state_locked(binding)
            binding_runtime.bind_thread_locked(
                binding,
                state,
                thread_id=thread_id,
                thread_title="demo",
                working_dir="/tmp/project",
            )
        return state

    def test_release_feishu_runtime_availability_locked_blocks_on_pending_request(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            *_,
            pending_by_thread,
            _pending_by_binding,
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

        allowed, reason = controller.release_feishu_runtime_availability_locked("thread-1")

        self.assertFalse(allowed)
        self.assertIn("审批或输入请求未处理", reason)

    def test_release_feishu_runtime_by_thread_id_marks_binding_released_and_unsubscribes(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            *_,
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

        result = controller.release_feishu_runtime_by_thread_id("thread-1")

        self.assertTrue(result["changed"])
        self.assertEqual(result["released_binding_ids"], ["p2p:ou_user:c1"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "released")
        self.assertEqual(unsubscribed, ["thread-1"])

    def test_handle_service_control_request_service_status_aggregates_runtime_inventory(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            *_,
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
        loaded_thread_ids.append("thread-1")

        status = controller.handle_service_control_request("service/status", {})

        self.assertEqual(status["binding_count"], 1)
        self.assertEqual(status["bound_binding_count"], 1)
        self.assertEqual(status["attached_binding_count"], 1)
        self.assertEqual(status["thread_count"], 1)
        self.assertEqual(status["loaded_thread_ids"], ["thread-1"])
        self.assertEqual(status["running_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(status["app_server_url"], "http://127.0.0.1:1234")

    def test_clear_all_bindings_for_control_rejects_when_binding_has_pending_request(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _pending_by_thread,
            pending_by_binding,
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

    def test_handle_service_control_request_thread_bindings_reports_attached_and_released(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            *_,
        ) = self._make_controller()
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        state_b = self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-1")
        with lock:
            binding_runtime.unsubscribe_thread_locked(binding_b, "thread-1")
            binding_runtime.apply_persisted_runtime_state_message_locked(
                binding_b,
                state_b,
                ThreadStateChanged(current_thread_runtime_state="released"),
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
                {"binding_id": "p2p:ou_user2:c2", "feishu_runtime_state": "released"},
            ],
        )
