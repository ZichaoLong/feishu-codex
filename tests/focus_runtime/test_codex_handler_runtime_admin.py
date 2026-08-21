import pathlib
import tempfile
from types import SimpleNamespace

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
    _register_handler as _reg,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from tests.execution_page_test_support import set_execution_page_state as _set_pages
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
)
from bot.service_control_plane import (
    ServiceControlError, control_request,
)
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLease,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerRuntimeAdminTests(CodexHandlerHarness):
    def test_active_binding_attach_primes_observer_and_matching_terminal_retires_it(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
            history_mode="paginated",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        control_request(
            data_dir,
            "binding/detach",
            {"binding_id": "p2p:ou_user:c1"},
        )
        thread.status = "active"
        active = ThreadSnapshot(
            summary=thread,
            turns=[
                {
                    "id": "turn-live",
                    "status": "inProgress",
                    "items": [
                        {
                            "id": "assistant-1",
                            "type": "agentMessage",
                            "text": "已完成的阶段回复",
                        }
                    ],
                }
            ],
            effective_model="gpt-5.5",
            effective_reasoning_effort="high",
            effective_approval_policy="on-request",
            effective_permissions_profile_id=":workspace",
        )
        handler._adapter.thread_snapshots[(thread.thread_id, None)] = active
        handler._adapter.loaded_thread_ids.add(thread.thread_id)

        def resume_active(thread_id: str, **kwargs):
            handler._adapter.resume_thread_calls.append(
                {"thread_id": thread_id, **kwargs}
            )
            return active

        handler._adapter.resume_thread = resume_active

        attached = control_request(
            data_dir,
            "binding/attach",
            {"binding_id": "p2p:ou_user:c1"},
        )

        self.assertTrue(attached["active_observer"])
        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertTrue(state["running"])
        self.assertEqual(state["current_turn_id"], "turn-live")
        self.assertEqual(state["current_execution_kind"], "active_observer")
        self.assertIsNotNone(state["mirror_watchdog_registration"])
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "已完成的阶段回复",
        )
        initial_payload = bot.sent_messages[-1][2]
        execution_payload = bot.patches[-1][1]
        self.assertNotIn("取消执行", initial_payload)
        self.assertIn("此前的执行过程可能不完整", execution_payload)
        self.assertIn("已完成的阶段回复", execution_payload)
        self.assertNotIn("取消执行", execution_payload)

        self._on_turn_completed(
            handler,
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-live", "status": "completed"},
            },
        )

        retired = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(retired["running"])
        self.assertEqual(retired["current_turn_id"], "")
        self.assertEqual(retired["current_execution_kind"], "")

    def test_service_control_plane_releases_runtime_via_running_service(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        status = control_request(data_dir, "service/status")
        result = control_request(data_dir, "thread/detach", {"thread_id": "thread-1"})

        self.assertEqual(status["binding_count"], 1)
        self.assertTrue(status["control_endpoint"].startswith("tcp://127.0.0.1:"))
        self.assertTrue(result["changed"])
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")

    def test_service_control_plane_thread_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        status = control_request(data_dir, "thread/status", {"thread_name": "demo"})

        self.assertEqual(status["thread_id"], "thread-1")
        self.assertEqual(status["thread_title"], "demo")

    def test_service_control_plane_thread_bindings_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        result = control_request(data_dir, "thread/bindings", {"thread_name": "demo"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["thread_title"], "demo")
        self.assertEqual(
            result["bindings"],
            [{"binding_id": "p2p:ou_user:c1", "feishu_runtime_state": "attached"}],
        )

    def test_service_control_plane_thread_status_thread_id_accepts_not_loaded_thread(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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
        handler._adapter.thread_snapshots[("thread-1", None)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        status = control_request(data_dir, "thread/status", {"thread_id": "thread-1"})

        self.assertEqual(status["thread_id"], "thread-1")
        self.assertEqual(status["backend_thread_status"], "notLoaded")
        self.assertEqual(status["thread_title"], "demo")
        self.assertEqual(status["working_dir"], "/tmp/project")

    def test_service_control_plane_thread_bindings_thread_id_accepts_not_loaded_thread(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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
        handler._adapter.thread_snapshots[("thread-1", None)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        result = control_request(data_dir, "thread/bindings", {"thread_id": "thread-1"})

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["thread_title"], "demo")
        self.assertEqual(result["bindings"], [{"binding_id": "p2p:ou_user:c1", "feishu_runtime_state": "attached"}])

    def test_service_control_plane_binding_list_uses_group_chat_display_name_cache(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        bot.chat_types["oc_group"] = "group"
        bot.chat_display_names["oc_group"] = "Project Group"
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
        _bind_authoritative_thread(handler, "ou_user", "oc_group", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        result = control_request(data_dir, "binding/list")

        self.assertEqual(result["chat_display_name_cache_miss_count"], 0)
        self.assertEqual(result["bindings"][0]["binding_id"], "group:oc_group")
        self.assertEqual(result["bindings"][0]["chat_display_name"], "Project Group")
        self.assertEqual(bot.refreshed_chat_display_names, [])

    def test_service_control_plane_binding_list_refreshes_group_chat_display_name_on_request(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        bot.chat_types["oc_group"] = "group"
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
        _bind_authoritative_thread(handler, "ou_user", "oc_group", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)
        bot.chat_display_names["oc_group"] = "Project Group"

        result = control_request(data_dir, "binding/list", {"refresh_names": True})

        self.assertEqual(result["chat_display_name_cache_miss_count"], 0)
        self.assertEqual(result["bindings"][0]["binding_id"], "group:oc_group")
        self.assertEqual(result["bindings"][0]["chat_display_name"], "Project Group")
        self.assertEqual(bot.refreshed_chat_display_names, ["oc_group"])

    def test_service_control_plane_binding_clear_removes_runtime_state_and_persistence(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        result = control_request(data_dir, "binding/clear", {"binding_id": "p2p:ou_user:c1"})

        self.assertTrue(result["cleared"])
        self.assertEqual(result["binding_id"], "p2p:ou_user:c1")
        self.assertNotIn(("ou_user", "c1"), self._binding_keys(handler))
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), ())
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertIsNone(handler._chat_binding_store.load(("ou_user", "c1")))

    def test_service_control_plane_binding_clear_all_removes_all_bindings(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread_a = ThreadSummary(
            thread_id="thread-a",
            cwd="/tmp/project-a",
            name="demo-a",
            preview="hello",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        thread_b = ThreadSummary(
            thread_id="thread-b",
            cwd="/tmp/project-b",
            name="demo-b",
            preview="world",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread_a)
        bot.message_contexts["m-group"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
        _bind_authoritative_thread(handler, "ou_admin", "chat-group", thread_b, message_id="m-group")

        result = control_request(data_dir, "binding/clear-all")

        self.assertFalse(result["already_empty"])
        self.assertEqual(
            result["cleared_binding_ids"],
            ["group:chat-group", "p2p:ou_user:c1"],
        )
        self.assertEqual(self._binding_keys(handler), ())
        self.assertEqual(sorted(handler._adapter.unsubscribe_thread_calls), ["thread-a", "thread-b"])
        self.assertEqual(handler._chat_binding_store.load_all(), {})

    def test_service_control_plane_binding_submit_prompt_starts_synthetic_turn(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
            },
        )

        self.assertTrue(result["started"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["turn_id"], "")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "继续分析")
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_submit_prompt_rejects_missing_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_typo:chat-typo",
                "text": "继续分析",
            },
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "prompt_denied_binding_not_found")
        self.assertEqual(result["reason"], "未找到 binding：p2p:ou_typo:chat-typo")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])
        self.assertEqual(self._binding_keys(handler), ())

    def test_service_control_plane_binding_submit_prompt_announces_only_after_successful_start(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
                "display_mode": "announce",
            },
        )

        self.assertTrue(result["started"])
        self.assertEqual(bot.replies, [("c1", "schedule触发，开始新一轮执行。")])

    def test_service_control_plane_binding_submit_prompt_queues_without_chat_reply(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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
        state = _runtime_state(handler, "ou_user", "c1")
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        _set_pages(state, current_message_id="execution-card")

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
            },
        )

        self.assertFalse(result["started"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(result["reason_code"], "")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_group_binding_submit_prompt_queues_different_running_actor(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        bot.chat_types["chat-group"] = "group"
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
        _bind_authoritative_thread(handler, "__group__", "chat-group", thread)
        state = _runtime_state(handler, "__group__", "chat-group")
        state["running"] = True
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        _set_pages(state, current_message_id="execution-card")
        state["current_actor_open_id"] = "ou_actor_1"

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "group:chat-group",
                "text": "继续分析",
                "actor_open_id": "ou_actor_2",
            },
        )

        self.assertFalse(result["started"])
        self.assertTrue(result["queued"])
        self.assertEqual(result["queue_position"], 1)
        self.assertEqual(result["reason_code"], "")
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_submit_prompt_announce_does_not_reply_when_start_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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
        handler._prompt_turn_entry.start_prompt_turn_result = lambda *_args, **_kwargs: SimpleNamespace(
            started=False,
            thread_id="thread-1",
            turn_id="",
            reason_code="execution_card_send_failed",
            reason_text="execution card failed",
        )

        result = control_request(
            data_dir,
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou_user:c1",
                "text": "继续分析",
                "synthetic_source": "schedule",
                "display_mode": "announce",
            },
        )

        self.assertFalse(result["started"])
        self.assertEqual(result["reason_code"], "execution_card_send_failed")
        self.assertEqual(bot.replies, [])

    def test_service_control_plane_binding_clear_rejects_when_binding_has_pending_request(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {},
            "thread_id": "thread-1",
            "turn_id": "turn-1",
            "title": "Codex 命令执行审批",
            "message_id": "msg-1",
            "questions": [],
            "answers": {},
            "chat_id": "c1",
            "sender_id": "ou_user",
            "actor_open_id": "ou_user",
            "status": "pending",
        })

        with self.assertRaisesRegex(ServiceControlError, "不能清除 binding"):
            control_request(data_dir, "binding/clear", {"binding_id": "p2p:ou_user:c1"})

        self.assertIn(("ou_user", "c1"), self._binding_keys(handler))

    def test_service_control_plane_detach_name_target_resolves_explicit_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        result = control_request(data_dir, "thread/detach", {"thread_name": "demo"})

        self.assertTrue(result["changed"])
        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(result["backend_thread_status"], "notLoaded")
        self.assertEqual(result["detached_binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["feishu_runtime_state"], "detached")

    def test_service_control_plane_thread_name_target_rejects_ambiguous_exact_name(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread_1 = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project-a",
            name="demo",
            preview="hello",
            created_at=0,
            updated_at=2,
            source="appServer",
            status="idle",
        )
        thread_2 = ThreadSummary(
            thread_id="thread-2",
            cwd="/tmp/project-b",
            name="demo",
            preview="world",
            created_at=0,
            updated_at=1,
            source="appServer",
            status="idle",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread_1)
        handler._adapter.thread_snapshots[("thread-2", None)] = ThreadSnapshot(summary=thread_2)

        with self.assertRaisesRegex(ServiceControlError, "匹配到多个同名线程"):
            control_request(data_dir, "thread/status", {"thread_name": "demo"})

    def test_service_control_plane_thread_target_requires_exactly_one_selector(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
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

        with self.assertRaises(ServiceControlError):
            control_request(data_dir, "thread/status", {})
        with self.assertRaises(ServiceControlError):
            control_request(
                data_dir,
                "thread/status",
                {"thread_id": "thread-1", "thread_name": "demo"},
            )

    def test_service_control_plane_unarchives_by_id_without_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        # Lifecycle control is a direct frontend mutation and must prove the
        # target through app-server `thread/read`, even without a local
        # Feishu binding.
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        result = control_request(data_dir, "thread/unarchive", {"thread_id": "thread-1"})

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "skipped")
        self.assertEqual(handler._adapter.unarchive_thread_calls, ["thread-1"])
        self.assertEqual(self._binding_keys(handler), ())

    def test_service_control_plane_deletes_root_and_clears_local_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=thread)

        result = control_request(data_dir, "thread/delete", {"thread_id": "thread-1"})

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "complete")
        self.assertEqual(handler._adapter.delete_thread_calls, ["thread-1"])
        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:c1"])
        self.assertIsNone(handler._chat_binding_store.load(("ou_user", "c1")))

    def test_service_control_plane_local_binding_inventory_does_not_read_backend(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        _reg(handler, bot)
        thread = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        result = control_request(data_dir, "thread/local-bindings", {"thread_id": "thread-1"})

        self.assertEqual(result["binding_ids"], ["p2p:ou_user:c1"])
        self.assertEqual(handler._adapter.read_thread_calls, [])

    def test_archive_command_archives_current_thread_and_clears_binding(self) -> None:
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
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, ["thread-1"])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "")
        self.assertIn("不是硬删除", bot.replies[-1][1])
        self.assertIn("已同步清理当前实例里仍指向该 thread 的 bindings：`1` 个。", bot.replies[-1][1])

    def test_archive_command_rejects_when_other_binding_has_pending_request(self) -> None:
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
        _bind_authoritative_thread(handler, "ou_other", "c2", thread)
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        self._store_pending_request(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "thread_id": "thread-1",
            "sender_id": "ou_other",
            "chat_id": "c2",
            "status": "pending",
        })

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, [])
        self.assertIn("待处理审批或补充输入", bot.replies[-1][1])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "thread-1")

    def test_archive_command_rejects_when_live_runtime_owner_is_other_instance(self) -> None:
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
        handler._adapter.read_thread = lambda thread_id, include_turns=False: ThreadSnapshot(summary=thread)
        handler._thread_lifecycle._load_runtime_lease = lambda thread_id: ThreadRuntimeLease(
            thread_id=thread_id,
            owner_instance="explorer",
            owner_service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(),
        )

        handler.handle_message("ou_user", "c1", "/archive")

        self.assertEqual(handler._adapter.archive_thread_calls, [])
        self.assertIn("explorer", bot.replies[-1][1])
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "thread-1")
