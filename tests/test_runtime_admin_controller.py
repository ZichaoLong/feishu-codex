import pathlib
import tempfile
import threading
import types
import unittest

from bot.adapters.base import RuntimeConfigSummary, ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.reason_codes import (
    PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST,
    ReasonedCheck,
)
from bot.runtime_admin_controller import RuntimeAdminController
from bot.runtime_state import ThreadStateChanged
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


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
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        unsubscribed: list[str] = []
        released_runtime_leases: list[str] = []
        pending_by_thread: set[str] = set()
        pending_by_binding: set[tuple[str, str]] = set()
        summaries: dict[str, ThreadSummary] = {}
        loaded_thread_ids: list[str] = []
        pending_requests: list[dict[str, object]] = []
        reset_calls: list[bool] = []

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
            app_server_mode=lambda: "managed",
            unsubscribe_thread=lambda thread_id: unsubscribed.append(thread_id),
            release_service_thread_runtime_lease=lambda thread_id: released_runtime_leases.append(thread_id),
            service_control_endpoint=lambda: "tcp://127.0.0.1:32001",
            instance_name=lambda: "corp-a",
            load_thread_runtime_lease=lambda thread_id: None,
            list_pending_interaction_requests=lambda: list(pending_requests),
            reset_current_instance_backend=lambda force: reset_calls.append(bool(force)) or {"force": bool(force)},
            safe_read_runtime_config=lambda: RuntimeConfigSummary(current_model_provider="provider1"),
            current_default_profile_resolution=lambda runtime_config: types.SimpleNamespace(
                effective_profile="default",
                stale_profile="",
            ),
            load_thread_resume_profile=lambda thread_id: None,
            permissions_summary=lambda approval_policy, sandbox: f"{sandbox}/{approval_policy}",
            prompt_write_denial_check=lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.allow(),
            released_runtime_reattach_check=lambda thread_id: ReasonedCheck.allow(),
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
            reprofile_possible_check=lambda thread_id: (thread_id not in loaded_thread_ids, ""),
        )
        return (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            unsubscribed,
            released_runtime_leases,
            pending_by_thread,
            pending_by_binding,
            pending_requests,
            reset_calls,
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

    def test_unsubscribe_availability_locked_blocks_on_pending_request(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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

        allowed, reason = controller.unsubscribe_availability_locked("thread-1")

        self.assertFalse(allowed)
        self.assertIn("审批或输入请求未处理", reason)
        check = controller.unsubscribe_check_locked("thread-1")
        self.assertEqual(check.reason_code, UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST)

    def test_unsubscribe_by_thread_id_marks_binding_released_and_unsubscribes(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            unsubscribed,
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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

        result = controller.unsubscribe_feishu_runtime_by_thread_id("thread-1")

        self.assertTrue(result["changed"])
        self.assertEqual(result["released_binding_ids"], ["p2p:ou_user:c1"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "released")
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
            released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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

        controller._unsubscribe_thread = _fail_unsubscribe

        with self.assertRaisesRegex(RuntimeError, "backend unsubscribe failed"):
            controller.unsubscribe_feishu_runtime_by_thread_id("thread-1")

        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "attached")
        self.assertEqual(binding_runtime.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, [])

        controller._unsubscribe_thread = lambda thread_id: unsubscribed.append(f"retry:{thread_id}")
        result = controller.unsubscribe_feishu_runtime_by_thread_id("thread-1")

        self.assertTrue(result["changed"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "released")
        self.assertEqual(unsubscribed, ["thread-1", "retry:thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_handle_service_control_request_service_status_aggregates_runtime_inventory(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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

        self.assertEqual(status["instance_name"], "corp-a")
        self.assertEqual(status["binding_count"], 1)
        self.assertEqual(status["bound_binding_count"], 1)
        self.assertEqual(status["attached_binding_count"], 1)
        self.assertEqual(status["thread_count"], 1)
        self.assertEqual(status["loaded_thread_ids"], ["thread-1"])
        self.assertEqual(status["running_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(status["app_server_url"], "http://127.0.0.1:1234")
        self.assertEqual(status["backend_reset_status"], "force-only")
        self.assertEqual(status["backend_reset_reason_code"], "backend_reset_force_only_by_running_binding")

    def test_plan_thread_reprofile_allows_direct_write_after_released_and_globally_unloaded(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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
        controller.unsubscribe_feishu_runtime_by_thread_id("thread-1")

        plan = controller.plan_thread_reprofile("thread-1")

        self.assertEqual(plan.status, "direct-write")
        self.assertEqual(plan.backend_thread_status, "notLoaded")
        self.assertEqual(plan.feishu_runtime_state, "released")
        self.assertIn("verifiably globally unloaded", plan.reason_text)

    def test_plan_thread_reprofile_blocks_when_live_runtime_owned_by_other_instance(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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
        controller._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="other-instance",
            owner_service_token="other-token",
            control_endpoint="tcp://127.0.0.1:9393",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(),
        )

        plan = controller.plan_thread_reprofile("thread-1")

        self.assertEqual(plan.status, "blocked")
        self.assertEqual(plan.reason_code, "reprofile_blocked_by_other_instance_owner")
        self.assertIn("other-instance", plan.reason_text)

    def test_handle_service_control_request_reset_backend_forwards_force_flag(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            reset_calls,
        ) = self._make_controller()

        result = controller.handle_service_control_request("service/reset-backend", {"force": True})

        self.assertEqual(reset_calls, [True])
        self.assertTrue(result["force"])

    def test_handle_preflight_command_blocks_released_binding_when_live_runtime_owner_blocks_reattach(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
        ) = self._make_controller()
        binding = ("ou_user", "c1")
        state = self._bind_thread(lock, binding_runtime, binding, thread_id="thread-1")
        state["feishu_runtime_state"] = "released"
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
        controller._released_runtime_reattach_check = lambda thread_id: ReasonedCheck.deny(
            PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            "当前线程正由实例 `default` 的本地 `fcodex` 持有 live runtime；当前不能自动转移。",
        )

        result = controller.handle_preflight_command(binding, "")

        assert result.card is not None
        content = result.card["elements"][0]["content"]
        self.assertIn("下一条普通消息：`blocked` (`prompt_denied_by_live_runtime_owner`)", content)
        self.assertIn("本地 `fcodex` 持有 live runtime", content)

    def test_service_status_reports_runtime_unverified_as_force_only(self) -> None:
        (
            _lock,
            _binding_runtime,
            controller,
            _summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
        ) = self._make_controller()
        controller._list_loaded_thread_ids = lambda: (_ for _ in ()).throw(RuntimeError("backend down"))

        status = controller.handle_service_control_request("service/status", {})

        self.assertEqual(status["backend_reset_status"], "force-only")
        self.assertEqual(
            status["backend_reset_reason_code"],
            "backend_reset_force_only_by_runtime_unverified",
        )

    def test_plan_thread_reprofile_uses_force_only_reason_when_runtime_is_unverified(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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
        controller._list_loaded_thread_ids = lambda: (_ for _ in ()).throw(RuntimeError("backend down"))

        plan = controller.plan_thread_reprofile("thread-1")

        self.assertEqual(plan.status, "reset-force-only")
        self.assertEqual(
            plan.reason_code,
            "reprofile_reset_force_only_by_runtime_unverified",
        )

    def test_clear_all_bindings_for_control_rejects_when_binding_has_pending_request(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            _pending_by_thread,
            pending_by_binding,
            _pending_requests,
            _reset_calls,
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
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
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
                ThreadStateChanged(feishu_runtime_state="released"),
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

    def test_binding_status_snapshot_includes_prompt_and_unsubscribe_reason_codes(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
        ) = self._make_controller()
        controller._prompt_write_denial_check = lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.deny(
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
        pending_by_thread.add("thread-1")

        snapshot = controller.binding_status_snapshot(binding)

        self.assertFalse(snapshot["next_prompt_allowed"])
        self.assertEqual(snapshot["next_prompt_reason_code"], PROMPT_DENIED_BY_INTERACTION_OWNER)
        self.assertFalse(snapshot["unsubscribe_available"])
        self.assertEqual(snapshot["unsubscribe_reason_code"], UNSUBSCRIBE_BLOCKED_BY_PENDING_REQUEST)

    def test_handle_preflight_command_renders_next_prompt_and_unsubscribe_checks(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded_thread_ids,
            _unsubscribed,
            _released_runtime_leases,
            pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
        ) = self._make_controller()
        controller._prompt_write_denial_check = lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.deny(
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
        pending_by_thread.add("thread-1")

        result = controller.handle_preflight_command(binding, "")

        card = result.card
        assert card is not None
        content = card["elements"][0]["content"]
        self.assertIn("作用对象：当前 chat binding；这是 dry-run", content)
        self.assertIn("下一条普通消息：`blocked` (`prompt_denied_by_interaction_owner`)", content)
        self.assertIn("unsubscribe：`blocked` (`unsubscribe_blocked_by_pending_request`)", content)
