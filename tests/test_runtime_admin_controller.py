import pathlib
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.reason_codes import (
    PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
    PROMPT_DENIED_BINDING_NOT_FOUND,
    PROMPT_DENIED_BY_INTERACTION_OWNER,
    DETACH_BLOCKED_BY_PENDING_REQUEST,
    ReasonedCheck,
)
from bot.runtime_admin_controller import RuntimeAdminController
from bot.runtime_state import ThreadStateChanged
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease, ThreadRuntimeLeaseHolder
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.thread_image_delivery import ThreadImageDeliveryController


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
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=chat_binding_store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        unsubscribed: list[str] = []
        archived: list[str] = []
        unarchived: list[str] = []
        deleted: list[str] = []
        released_runtime_leases: list[str] = []
        pending_by_thread: set[str] = set()
        pending_by_binding: set[tuple[str, str]] = set()
        summaries: dict[str, ThreadSummary] = {}
        loaded_thread_ids: list[str] = []
        pending_requests: list[dict[str, object]] = []
        reset_calls: list[bool] = []
        sent_images: list[tuple[str, str]] = []
        runtime_submissions: list[tuple[object, tuple[object, ...], dict[str, object]]] = []
        reply_texts: list[tuple[str, str, str]] = []
        reply_cards: list[tuple[str, dict[str, object], str]] = []
        submitted_prompts: list[dict[str, object]] = []
        thread_goals: dict[str, ThreadGoalSummary] = {}
        chat_display_names: dict[tuple[str, str, str], str] = {}
        chat_display_name_calls: list[dict[str, object]] = []

        def _read_thread(thread_id: str):
            return ThreadSnapshot(summary=summaries[thread_id])

        def _set_thread_goal(
            thread_id: str,
            objective: str | None = None,
            status: str | None = None,
        ) -> ThreadGoalSummary:
            existing = thread_goals.get(thread_id)
            goal = ThreadGoalSummary(
                thread_id=thread_id,
                objective=(
                    str(objective or "").strip()
                    if objective is not None
                    else (existing.objective if existing is not None else "")
                ),
                status=(
                    str(status or "").strip()
                    if status is not None
                    else (existing.status if existing is not None else "active")
                )
                or "active",
                token_budget=existing.token_budget if existing is not None else None,
                tokens_used=existing.tokens_used if existing is not None else 0,
                time_used_seconds=existing.time_used_seconds if existing is not None else 0,
                created_at=existing.created_at if existing is not None else 0,
                updated_at=existing.updated_at if existing is not None else 0,
            )
            thread_goals[thread_id] = goal
            return goal

        def _resolve_binding_chat_display_name(
            *,
            binding_kind: str,
            sender_id: str,
            chat_id: str,
            refresh_names: bool = False,
        ) -> str:
            chat_display_name_calls.append(
                {
                    "binding_kind": binding_kind,
                    "sender_id": sender_id,
                    "chat_id": chat_id,
                    "refresh_names": refresh_names,
                }
            )
            return chat_display_names.get((binding_kind, sender_id, chat_id), "")

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
            read_thread_for_stale_cleanup=_read_thread,
            list_loaded_thread_ids=lambda: list(loaded_thread_ids),
            current_app_server_url=lambda: "http://127.0.0.1:1234",
            app_server_mode=lambda: "managed",
            unsubscribe_thread=lambda thread_id: unsubscribed.append(thread_id),
            archive_thread=lambda thread_id: archived.append(thread_id),
            unarchive_thread=lambda thread_id: unarchived.append(thread_id) or summaries[thread_id],
            delete_thread=lambda thread_id: deleted.append(thread_id),
            release_service_thread_runtime_lease=lambda thread_id: released_runtime_leases.append(thread_id),
            service_control_endpoint=lambda: "tcp://127.0.0.1:32001",
            instance_name=lambda: "corp-a",
            load_thread_runtime_lease=lambda thread_id: None,
            list_pending_interaction_requests=lambda: list(pending_requests),
            reset_current_instance_backend=lambda force: reset_calls.append(bool(force)) or {"force": bool(force)},
            attach_binding=lambda binding, thread_id: summaries[thread_id],
            permissions_summary=lambda approval_policy, sandbox: f"{sandbox}/{approval_policy}",
            thread_image_delivery=ThreadImageDeliveryController(
                upload_image=lambda local_path: "img-key-1",
                send_image_by_key=lambda chat_id, image_key: sent_images.append((chat_id, image_key)) or f"msg:{chat_id}",
                path_exists=lambda path: True,
                path_is_file=lambda path: True,
            ),
            get_thread_goal=lambda thread_id: thread_goals.get(thread_id),
            set_thread_goal=_set_thread_goal,
            clear_thread_goal=lambda thread_id: thread_goals.pop(thread_id, None) is not None,
            submit_to_runtime=lambda fn, *args, **kwargs: runtime_submissions.append((fn, args, kwargs)),
            reply_text=lambda chat_id, text, message_id="": reply_texts.append((chat_id, text, message_id)),
            reply_card=lambda chat_id, card, message_id="": reply_cards.append((chat_id, card, message_id)),
            submit_prompt_for_control=lambda binding, **kwargs: submitted_prompts.append(
                {"binding": binding, **kwargs}
            ) or {
                "binding_id": f"p2p:{binding[0]}:{binding[1]}",
                "thread_id": "thread-1",
                "started": True,
                "turn_id": "turn-1",
                "reason_code": "",
                "reason": "",
                "synthetic_source": str(kwargs.get("synthetic_source", "") or ""),
                "display_mode": str(kwargs.get("display_mode", "silent") or "silent"),
            },
            prompt_write_denial_check=lambda binding, chat_id, thread_id, message_id="": ReasonedCheck.allow(),
            detached_runtime_attach_check=lambda thread_id: ReasonedCheck.allow(),
            lifecycle_loaded_gate_check=lambda thread_id, operation: ReasonedCheck.allow(),
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
            resolve_binding_chat_display_name=_resolve_binding_chat_display_name,
            cancel_patch_timer_locked=lambda state: state.update({"patch_timer": None}),
            cancel_mirror_watchdog_locked=lambda state: state.update({"mirror_watchdog_timer": None}),
            is_thread_not_found_error=lambda exc: False,
            is_thread_not_loaded_error=lambda exc: False,
        )
        controller._submitted_prompts = submitted_prompts  # type: ignore[attr-defined]
        controller._thread_goals = thread_goals  # type: ignore[attr-defined]
        controller._unarchived = unarchived  # type: ignore[attr-defined]
        controller._deleted = deleted  # type: ignore[attr-defined]
        controller._chat_display_names = chat_display_names  # type: ignore[attr-defined]
        controller._chat_display_name_calls = chat_display_name_calls  # type: ignore[attr-defined]
        return (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            unsubscribed,
            archived,
            released_runtime_leases,
            pending_by_thread,
            pending_by_binding,
            pending_requests,
            reset_calls,
            sent_images,
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

    def test_binding_list_reports_chat_display_name_cache_misses(self) -> None:
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
        binding = (GROUP_SHARED_BINDING_OWNER_ID, "oc_group")
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

        result = controller.handle_service_control_request("binding/list", {})

        self.assertEqual(result["chat_display_name_cache_miss_count"], 1)
        self.assertEqual(result["bindings"][0]["chat_display_name"], "")

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

        allowed, reason = controller.detach_thread_availability_locked("thread-1")

        self.assertFalse(allowed)
        self.assertIn("审批或输入请求未处理", reason)
        check = controller.detach_thread_check_locked("thread-1")
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

        controller._unsubscribe_thread = _fail_unsubscribe

        with self.assertRaisesRegex(RuntimeError, "backend unsubscribe failed"):
            controller.detach_thread("thread-1")

        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "attached")
        self.assertEqual(binding_runtime.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, [])

        controller._unsubscribe_thread = lambda thread_id: unsubscribed.append(f"retry:{thread_id}")
        result = controller.detach_thread("thread-1")

        self.assertTrue(result["changed"])
        with lock:
            snapshot = binding_runtime.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "detached")
        self.assertEqual(unsubscribed, ["thread-1", "retry:thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

    def test_archive_thread_for_control_archives_and_clears_current_instance_bindings(self) -> None:
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
        self.assertEqual(unsubscribed, ["thread-1"])
        self.assertEqual(released_runtime_leases, ["thread-1"])

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
        controller._lifecycle_loaded_gate_check = lambda thread_id, operation: ReasonedCheck.deny(
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
        controller._archive_thread = lambda thread_id: (_ for _ in ()).throw(TimeoutError(thread_id))

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
            "release",
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
        controller._delete_thread = lambda thread_id: (_ for _ in ()).throw(
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
        controller._delete_thread = lambda thread_id: (_ for _ in ()).throw(
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
        controller._unarchive_thread = lambda thread_id: (_ for _ in ()).throw(
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
        controller._archive_thread = lambda thread_id: (_ for _ in ()).throw(
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
        controller._delete_thread = lambda thread_id: (_ for _ in ()).throw(
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
        controller._read_thread = lambda thread_id: (_ for _ in ()).throw(
            OSError(f"read failed: {thread_id}")
        )

        with patch("bot.runtime_admin_controller.logger.exception"):
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
        controller._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
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
        controller._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
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
        controller._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
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

    def test_clear_archived_thread_bindings_for_control_clears_without_archiving(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
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

    def test_clear_archived_thread_bindings_for_control_dry_run_does_not_clear(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            _summaries,
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
            _summaries,
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
        with lock:
            state["current_turn_id"] = "turn-1"

        with self.assertRaisesRegex(ValueError, "正在运行"):
            controller.clear_archived_thread_bindings_for_control("thread-1")

        self.assertEqual(archived, [])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding))

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
        controller._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller.clear_stale_bindings_for_control(dry_run=True)

        self.assertEqual(result["would_clear_binding_ids"], ["p2p:ou_stale:chat-stale"])
        self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(live_binding))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(stale_binding))

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
        controller._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

        result = controller.clear_stale_bindings_for_control()

        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_stale:chat-stale"])
        self.assertEqual(result["stale_thread_ids"], ["thread-stale"])
        self.assertEqual(result["retained_thread_ids"], ["thread-live"])
        with lock:
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(live_binding))
            self.assertIsNone(binding_runtime.binding_runtime_snapshot_locked(stale_binding))
        self.assertEqual(unsubscribed, ["thread-stale"])
        self.assertEqual(released_runtime_leases, ["thread-stale"])

    def test_clear_stale_bindings_finalizes_successes_before_reporting_partial_failure(self) -> None:
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
        controller._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)
        original_release = binding_runtime._interaction_lease_store.release

        def _release(thread_id, holder):
            if thread_id == "thread-b":
                raise OSError("lease cleanup failed")
            return original_release(thread_id, holder)

        with patch.object(binding_runtime._interaction_lease_store, "release", side_effect=_release):
            with self.assertRaisesRegex(RuntimeError, "lease cleanup failed"):
                controller.clear_stale_bindings_for_control()

        with lock:
            self.assertIsNone(binding_runtime.binding_runtime_snapshot_locked(binding_a))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding_b))
        self.assertIsNone(binding_runtime._chat_binding_store.load(binding_a))
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_b))
        self.assertEqual(unsubscribed, ["thread-a"])
        self.assertEqual(released_runtime_leases, ["thread-a"])

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

    def test_handle_service_control_request_service_status_aggregates_runtime_inventory(self) -> None:
        (
            lock,
            binding_runtime,
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
        controller._load_thread_runtime_lease = lambda thread_id: ThreadRuntimeLease(
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

        snapshot = controller.thread_status_snapshot("thread-1")

        self.assertEqual(snapshot["backend_thread_status"], "notLoaded")
        self.assertEqual(snapshot["live_runtime_owner"]["label"], "explorer")
        self.assertEqual(snapshot["live_runtime_holder_labels"], ["service@explorer(pid=4321)"])

    def test_handle_service_control_request_reset_backend_forwards_force_flag(self) -> None:
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
            reset_calls,
            _sent_images,
        ) = self._make_controller()

        result = controller.handle_service_control_request("service/reset-backend", {"force": True})

        self.assertEqual(reset_calls, [True])
        self.assertTrue(result["force"])

    def test_handle_reset_backend_command_renders_available_preview_card(self) -> None:
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

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        self.assertEqual(result.card["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("作用对象：当前实例 backend", result.card["elements"][0]["content"])
        action = result.card["elements"][2]["actions"][0]
        self.assertEqual(action["text"]["content"], "重置 backend")
        self.assertEqual(action["value"]["force"], False)

    def test_handle_reset_backend_command_renders_force_reset_button_when_force_only(self) -> None:
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
            pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        pending_requests.append({"request_id": "req-1"})

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        self.assertIn("只能显式确认强制重置", result.card["elements"][0]["content"])
        action = result.card["elements"][2]["actions"][0]
        self.assertEqual(action["text"]["content"], "强制重置 backend")
        self.assertEqual(action["value"]["force"], True)

    def test_backend_reset_preview_exposes_blockers_and_collateral_summary(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            pending_requests,
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
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="other",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        loaded_thread_ids.extend(["thread-1", "thread-2"])
        pending_requests.append({"request_id": "req-1"})

        preview = controller.backend_reset_preview()

        self.assertEqual(preview.status, "force-only")
        self.assertEqual(preview.blocking_pending_request_count, 1)
        self.assertEqual(preview.blocking_active_turn_count, 1)
        self.assertEqual(preview.attached_binding_ids, ("p2p:ou_user:c1",))
        self.assertEqual(preview.loaded_thread_preview, ("thread-1", "thread-2"))
        self.assertEqual(preview.collateral_loaded_thread_count, 2)
        self.assertEqual(preview.collateral_active_loaded_thread_count, 1)
        self.assertIn("hard blocker：待处理审批/输入请求：`1`", preview.diagnostics)
        self.assertIn("collateral impact：attached Feishu bindings：`p2p:ou_user:c1`", preview.diagnostics)
        self.assertIn("collateral impact：当前实例 loaded threads：`2`", preview.diagnostics)

    def test_handle_reset_backend_command_renders_hard_blockers_and_collateral_sections(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            loaded_thread_ids,
            _unsubscribed,
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            pending_requests,
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
        loaded_thread_ids.append("thread-1")
        pending_requests.append({"request_id": "req-1"})

        result = controller.handle_reset_backend_command("")

        assert result.card is not None
        content = result.card["elements"][0]["content"]
        self.assertIn("**Hard Blockers**", content)
        self.assertIn("待处理审批/输入请求：`1`", content)
        self.assertIn("**Collateral Impact**", content)
        self.assertIn("attached Feishu bindings：`p2p:ou_user:c1`", content)
        self.assertIn("当前实例 loaded threads：`1`", content)

    def test_handle_reset_backend_action_executes_reset_and_returns_result_card(self) -> None:
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
            reset_calls,
            _sent_images,
        ) = self._make_controller()

        response = controller.handle_reset_backend_action("ou_user", "c1", "m1", {"force": True})

        self.assertEqual(reset_calls, [True])
        self.assertEqual(response.toast.type, "success")
        self.assertEqual(response.toast.content, "已重置当前实例 backend。")
        self.assertIsNotNone(response.card)
        assert response.card is not None
        self.assertEqual(response.card.data["header"]["title"]["content"], "Codex Backend Reset")
        self.assertIn("已重置当前实例 backend。", response.card.data["elements"][0]["content"])
        self.assertIn("如需确认飞书侧继续接收本地", response.card.data["elements"][0]["content"])
        actions = response.card.data["elements"][-1]["actions"]
        self.assertEqual([action["text"]["content"] for action in actions], ["附着当前实例", "保持 detached"])

    def test_handle_reset_backend_action_offers_current_thread_attach_after_reset(self) -> None:
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

        response = controller.handle_reset_backend_action("ou_user", "c1", "m1", {"force": False})

        self.assertIsNotNone(response.card)
        assert response.card is not None
        actions = response.card.data["elements"][-1]["actions"]
        self.assertEqual(
            [action["text"]["content"] for action in actions],
            ["附着当前线程", "附着当前实例", "保持 detached"],
        )
        self.assertEqual(actions[0]["value"]["thread_id"], "thread-1")

    def test_attach_service_is_partial_success_by_thread(self) -> None:
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
        binding_one = ("ou_user", "c1")
        binding_two = ("ou_user", "c2")
        state_one = self._bind_thread(lock, binding_runtime, binding_one, thread_id="thread-1")
        state_two = self._bind_thread(lock, binding_runtime, binding_two, thread_id="thread-2")
        with lock:
            state_one["feishu_runtime_state"] = "detached"
            state_two["feishu_runtime_state"] = "detached"
        summaries["thread-1"] = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo-1",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        summaries["thread-2"] = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project",
            name="demo-2",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="notLoaded",
        )
        controller._detached_runtime_attach_check = lambda thread_id: (
            ReasonedCheck.allow()
            if thread_id == "thread-1"
            else ReasonedCheck.deny(
                PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
                "当前 thread 仍由运行中的实例 `explorer` 保持为 loaded (`idle`)；当前按 fail-close 拒绝跨实例继续。",
            )
        )

        result = controller.attach_service()

        self.assertEqual(result["attached_thread_ids"], ["thread-1"])
        self.assertEqual(result["attached_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(len(result["blocked_threads"]), 1)
        self.assertEqual(result["blocked_threads"][0]["thread_id"], "thread-2")
        self.assertEqual(result["blocked_threads"][0]["binding_ids"], ["p2p:ou_user:c2"])
        self.assertIn("拒绝跨实例继续", result["blocked_threads"][0]["reason"])

    def test_handle_preflight_command_blocks_detached_binding_when_live_runtime_owner_blocks_attach(self) -> None:
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
        state["feishu_runtime_state"] = "detached"
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
        controller._detached_runtime_attach_check = lambda thread_id: ReasonedCheck.deny(
            PROMPT_DENIED_BY_LIVE_RUNTIME_OWNER,
            "当前线程正由实例 `default` 的本地 `fcodex` 持有 live runtime；当前不支持跨实例继续。",
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
            _archived,
            _released_runtime_leases,
            _pending_by_thread,
            _pending_by_binding,
            _pending_requests,
            _reset_calls,
            _sent_images,
        ) = self._make_controller()
        controller._list_loaded_thread_ids = lambda: (_ for _ in ()).throw(RuntimeError("backend down"))

        status = controller.handle_service_control_request("service/status", {})

        self.assertEqual(status["backend_reset_status"], "force-only")
        self.assertEqual(
            status["backend_reset_reason_code"],
            "backend_reset_force_only_by_runtime_unverified",
        )

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

    def test_clear_all_bindings_for_control_rolls_back_when_batch_clear_fails(self) -> None:
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
        binding_a = ("ou_user", "c1")
        binding_b = ("ou_user2", "c2")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-1")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-2")

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

    def test_clear_all_bindings_finalizes_successes_before_reporting_partial_failure(self) -> None:
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
        original_release = binding_runtime._interaction_lease_store.release

        def _release(thread_id, holder):
            if thread_id == "thread-b":
                raise OSError("lease cleanup failed")
            return original_release(thread_id, holder)

        with patch.object(binding_runtime._interaction_lease_store, "release", side_effect=_release):
            with self.assertRaisesRegex(RuntimeError, "lease cleanup failed"):
                controller.clear_all_bindings_for_control()

        with lock:
            self.assertIsNone(binding_runtime.binding_runtime_snapshot_locked(binding_a))
            self.assertIsNotNone(binding_runtime.binding_runtime_snapshot_locked(binding_b))
        self.assertIsNone(binding_runtime._chat_binding_store.load(binding_a))
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_b))
        self.assertEqual(unsubscribed, ["thread-a"])
        self.assertEqual(released_runtime_leases, ["thread-a"])

    def test_clear_all_bindings_for_control_clears_store_only_stale_binding(self) -> None:
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
        live_binding = ("ou_live", "chat-live")
        stale_binding = ("ou_stale", "chat-stale")
        self._bind_thread(lock, binding_runtime, live_binding, thread_id="thread-live")
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
            binding_runtime.apply_persisted_runtime_state_message_locked(
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

    def test_handle_service_control_request_thread_goal_pause_resume_and_clear(self) -> None:
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
        resumed = controller.handle_service_control_request(
            "thread/goal/set",
            {"thread_id": "thread-1", "status": "active"},
        )
        cleared = controller.handle_service_control_request("thread/goal/clear", {"thread_id": "thread-1"})

        self.assertEqual(paused["goal"]["status"], "paused")
        self.assertEqual(resumed["goal"]["status"], "active")
        self.assertIsNone(cleared["goal"])
        self.assertTrue(cleared["cleared"])

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
        controller._read_thread = lambda thread_id: (_ for _ in ()).throw(
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
            _summaries,
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
            _summaries,
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
        controller._is_thread_not_found_error = lambda exc: isinstance(exc, KeyError)

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

        controller._read_thread_for_stale_cleanup = _raise_not_loaded
        controller._is_thread_not_loaded_error = lambda exc: str(exc).startswith("thread not loaded:")

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
        pending_by_binding.add(binding)

        snapshot = controller.binding_status_snapshot(binding)

        self.assertFalse(snapshot["next_prompt_allowed"])
        self.assertEqual(snapshot["next_prompt_reason_code"], PROMPT_DENIED_BY_INTERACTION_OWNER)
        self.assertFalse(snapshot["detach_available"])
        self.assertEqual(snapshot["detach_reason_code"], DETACH_BLOCKED_BY_PENDING_REQUEST)

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
        pending_by_binding.add(binding)

        result = controller.handle_preflight_command(binding, "")

        card = result.card
        assert card is not None
        content = card["elements"][0]["content"]
        self.assertIn("作用对象：当前 chat binding；这是 dry-run", content)
        self.assertIn("下一条普通消息：`blocked` (`prompt_denied_by_interaction_owner`)", content)
        self.assertIn("detach：`blocked` (`detach_blocked_by_pending_request`)", content)
