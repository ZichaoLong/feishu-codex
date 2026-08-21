
from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
)
from tests.focus_runtime.codex_handler_fakes import _runtime_state
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerCompactTests(CodexHandlerHarness):
    def _make_handler(self, *args, **kwargs):
        handler, bot = super()._make_handler(*args, **kwargs)
        self._seed_authoritative_thread(handler, status="idle")
        return handler, bot

    def test_compact_command_starts_current_thread_compaction(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-1"])
        self.assertEqual(bot.cards[-1][1]["header"]["title"]["content"], "Codex Compact 已开始")
        self.assertIn("`thread-1", bot.cards[-1][1]["elements"][0]["content"])

    def test_compact_rpc_timeout_keeps_process_local_submission_and_anchor(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        def timeout_after_send(thread_id: str) -> None:
            handler._adapter.compact_thread_calls.append(thread_id)
            raise TimeoutError("Codex request timed out: thread/compact/start")

        handler._adapter.compact_thread = timeout_after_send

        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")

        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )
        self.assertTrue(state["running"])
        self.assertTrue(state["awaiting_local_turn_started"])
        self.assertEqual(state["current_execution_kind"], "compact")
        self.assertTrue(state["execution_pages"].current_message_id)
        self.assertIsNotNone(handler._interaction_lease_store.load("thread-1"))

    def test_compact_rpc_transport_loss_keeps_process_local_submission(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        def disconnect_after_send(_thread_id: str) -> None:
            raise CodexRpcTransportError(
                "thread/compact/start",
                {"message": "Codex websocket disconnected"},
            )

        handler._adapter.compact_thread = disconnect_after_send

        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")

        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )
        self.assertTrue(state["running"])
        self.assertTrue(state["awaiting_local_turn_started"])
        self.assertTrue(state["execution_pages"].current_message_id)

    def test_compact_pre_send_failure_retires_anchor_and_allows_next_prompt(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        def fail_before_send(_thread_id: str) -> None:
            raise CodexRpcPreSendError(
                "thread/compact/start",
                RuntimeError("test request was not sent"),
            )

        handler._adapter.compact_thread = fail_before_send

        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")

        self.assertFalse(handler._turn_execution.has_active_execution_locked(state))
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))

        handler.handle_message("ou_user", "c1", "next prompt", message_id="m-next")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.start_turn_calls[0]["text"], "next prompt")

    def test_prompt_compact_prompt_queue_runs_fifo(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first")
        handler.handle_message("ou_user", "c1", "/compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "compact-turn"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_ignores_stale_idle_before_turn_started(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first", message_id="m-1")
        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "c1", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])

        self._dispatch_adapter_notification(
            handler,
            "thread/status/changed",
            {"threadId": "thread-created", "status": {"type": "idle"}},
        )

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertEqual(state["current_turn_id"], "")

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "compact-turn"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/started",
            {
                "threadId": "thread-created",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-item"},
            },
        )
        self.assertIn("上下文压缩", state["execution_transcript"].process_text())

        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_binds_from_context_compaction_item_started_when_turn_started_missing(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "first", message_id="m-1")
        handler.handle_message("ou_user", "c1", "/compact", message_id="m-compact")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertEqual(bot.replies[-2], ("c1", "已排队，compact 将在当前执行结束后开始。队列位置：1"))
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：2"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "c1", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(state["current_execution_kind"], "compact")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])

        self._dispatch_adapter_notification(
            handler,
            "item/started",
            {
                "threadId": "thread-created",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-item"},
            },
        )

        self.assertEqual(state["current_turn_id"], "compact-turn")
        self.assertFalse(state["awaiting_local_turn_started"])
        self.assertIn("上下文压缩", state["execution_transcript"].process_text())

        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "compact-turn", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")

    def test_queued_compact_keeps_origin_context_after_message_context_expires(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "thread_id": "om_thread",
        }
        bot.message_contexts["m-compact"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_admin", "chat-group", "first", message_id="m-1")
        handler.handle_message("ou_admin", "chat-group", "/compact", message_id="m-compact")
        bot.message_contexts.pop("m-compact", None)

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_admin", "chat-group", "m-compact")
        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-created"])
        self.assertEqual(state["current_actor_open_id"], "ou_admin")
        self.assertEqual(bot.reply_ref_calls[-1][0], "m-compact")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_compact_then_prompt_queues_until_compact_completes(self) -> None:
        handler, _ = self._make_handler()
        _bind_authoritative_thread(handler,
            "ou_user",
            "c1",
            ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="cli",
                status="idle",
            ),
        )

        handler.handle_message("ou_user", "c1", "/compact")
        handler.handle_message("ou_user", "c1", "after compact", message_id="m-2")

        self.assertEqual(handler._adapter.compact_thread_calls, ["thread-1"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 0)

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "compact-turn"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/started",
            {
                "threadId": "thread-1",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-item"},
            },
        )
        self._on_turn_completed(
            handler,
            {
                "threadId": "thread-1",
                "turn": {"id": "compact-turn", "status": "completed"},
            },
        )

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "after compact")

    def test_queued_prompt_uses_latest_model_setting_at_dequeue(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "first")
        handler.handle_message("ou_user", "c1", "second", message_id="m-2")
        handler.handle_message("ou_user", "c1", "/model gpt-5.5")

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "second")
        self.assertEqual(handler._adapter.start_turn_calls[-1]["model"], "gpt-5.5")

    def test_compact_command_surfaces_thread_not_loaded_hint(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            )
        )

        def _raise_not_loaded(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not loaded: thread-1"})

        handler._adapter.compact_thread = _raise_not_loaded

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("当前 thread 尚未加载到本实例 backend", bot.replies[-1][1])
        self.assertIn("`/attach`", bot.replies[-1][1])
        self.assertEqual(handler._adapter.read_thread_calls[-1], {"thread_id": "thread-1", "include_turns": False})

    def test_compact_command_thread_not_found_confirms_readable_thread_before_unloaded_hint(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-1",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            )
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("当前 thread 尚未加载到本实例 backend", bot.replies[-1][1])
        self.assertEqual(handler._adapter.read_thread_calls[-1], {"thread_id": "thread-1", "include_turns": False})

    def test_compact_command_rejects_when_direct_target_read_returns_not_loaded(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = CodexRpcError(
            "thread/read",
            {"message": "thread not loaded: thread-1"},
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("无法取得当前 submission；未启动 compact", bot.replies[-1][1])
        self.assertIn("thread not loaded: thread-1", bot.replies[-1][1])
        self.assertEqual(handler._adapter.compact_thread_calls, [])

    def test_compact_command_rejects_when_direct_target_read_is_unavailable(self) -> None:
        handler, bot = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["feishu_runtime_state"] = "attached"
        handler._adapter.thread_snapshots[("thread-1", False)] = TimeoutError(
            "Codex request timed out: thread/read"
        )

        def _raise_not_found(thread_id: str) -> None:
            del thread_id
            raise CodexRpcError("thread/compact/start", {"message": "thread not found: thread-1"})

        handler._adapter.compact_thread = _raise_not_found

        handler.handle_message("ou_user", "c1", "/compact")

        self.assertIn("无法取得当前 submission；未启动 compact", bot.replies[-1][1])
        self.assertIn("Codex request timed out: thread/read", bot.replies[-1][1])
        self.assertEqual(handler._adapter.compact_thread_calls, [])
