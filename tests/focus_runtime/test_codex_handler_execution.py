import json
import os
import pathlib
import tempfile
import threading
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
    _register_handler as _reg,
)
from tests.focus_runtime.codex_handler_fakes import _capture_reconcile, _run_reconcile, _runtime_state, _store_pending_interaction as _store_pending
from tests.execution_page_test_support import set_execution_page_state as _set_pages
from bot.execution_pages import (
    ExecutionTranscriptCursor,
    TerminalExecutionPageReceipt,
)
from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerExecutionTests(CodexHandlerHarness):
    def test_external_turn_started_opens_new_execution_card(self) -> None:
        handler, bot = self._make_handler()
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
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(state, current_message_id="old-card")
            state["execution_transcript"].set_reply_text("收到")
            state["execution_transcript"].append_process_note("old log")
            state["running"] = False

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-2"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-1", "turnId": "turn-2", "delta": "新的回复"},
        )

        self.assertEqual(len(bot.sent_messages), 1)
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["execution_pages"].current_message_id, "plan-card-2")
        self.assertEqual(
            _runtime_state(handler, "ou_user", "c1")["execution_transcript"].reply_text(),
            "新的回复",
        )

    def test_external_turn_started_finalizes_previous_execution_card(self) -> None:
        handler, bot = self._make_handler()
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
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(state, current_message_id="old-card")
            state["execution_transcript"].set_reply_text("上一轮回复")
            state["execution_transcript"].append_process_note("上一轮日志")
            state["running"] = False

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-2"}},
        )

        self._wait_until(lambda: any(message_id == "old-card" for message_id, _ in bot.patches))
        patched = json.loads(next(content for message_id, content in bot.patches if message_id == "old-card"))
        body_elements = patched["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["execution_pages"].current_message_id, "plan-card-2")

    def test_prompt_start_waits_for_authoritative_turn_started_identity(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])
        lease = handler._interaction_lease_store.load("thread-created")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "")

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-actual"}},
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "turn-actual")
        self.assertFalse(state["awaiting_local_turn_started"])
        lease = handler._interaction_lease_store.load("thread-created")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "turn-actual")

    def test_queued_old_completion_cannot_bind_a_new_prompt_submission(self) -> None:
        handler, _ = self._make_handler()
        original_start_turn = handler._adapter.start_turn

        def start_turn_with_older_completion(**kwargs):
            response = original_start_turn(**kwargs)
            reader = threading.Thread(
                target=handler._adapter_events.handle_notification,
                args=(
                    handler._adapter.connection_generation(),
                    "turn/completed",
                    {
                        "threadId": "thread-created",
                        "turn": {"id": "turn-old", "status": "completed"},
                    },
                ),
            )
            reader.start()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive())
            return response

        handler._adapter.start_turn = start_turn_with_older_completion

        handler.handle_message("ou_user", "c1", "hello")
        handler._runtime_call(lambda: None)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])
        lease = handler._interaction_lease_store.load("thread-created")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "")
        self.assertEqual(
            self._feishu_root_snapshot(
                handler,
                "thread-created",
            ).pending_admission_count,
            1,
        )

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-actual"}},
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["current_turn_id"], "turn-actual")
        self.assertFalse(state["awaiting_local_turn_started"])
        lease = handler._interaction_lease_store.load("thread-created")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "turn-actual")

    def test_cancel_uses_start_candidate_once_before_turn_started(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")

        ok, message = handler._runtime_call(
            handler._feishu_surface.cancel_current_turn,
            "ou_user",
            "c1",
        )

        self.assertTrue(ok)
        self.assertEqual(message, "已请求停止当前执行。")
        self.assertEqual(
            handler._adapter.interrupt_turn_calls,
            [{"thread_id": "thread-created", "turn_id": "turn-1"}],
        )
        self.assertFalse(_runtime_state(handler, "ou_user", "c1")["pending_cancel"])
        self.assertFalse(_runtime_state(handler, "ou_user", "c1")["cancelled"])

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )

        self.assertEqual(
            handler._adapter.interrupt_turn_calls,
            [{"thread_id": "thread-created", "turn_id": "turn-1"}],
        )
        self.assertFalse(_runtime_state(handler, "ou_user", "c1")["pending_cancel"])
        self.assertFalse(_runtime_state(handler, "ou_user", "c1")["cancelled"])

        self._dispatch_adapter_notification(
            handler,
            "turn/completed",
            {
                "threadId": "thread-created",
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        )
        self.assertTrue(_runtime_state(handler, "ou_user", "c1")["cancelled"])

    def test_cancel_recovers_from_missing_thread(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )

        def _raise_missing_thread(*, thread_id: str, turn_id: str):
            del thread_id
            del turn_id
            raise CodexRpcError("turn/interrupt", {"code": -32000, "message": "thread not found: thread-created"})

        handler._adapter.interrupt_turn = _raise_missing_thread

        ok, message = handler._runtime_call(
            handler._feishu_surface.cancel_current_turn,
            "ou_user",
            "c1",
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertTrue(ok)
        self.assertEqual(message, "当前执行已结束，已刷新卡片状态。")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["current_turn_id"], "")

    def test_cancel_pre_send_failure_retains_cancel_intent(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )

        def _raise_pre_send(*, thread_id: str, turn_id: str):
            del thread_id
            del turn_id
            raise CodexRpcPreSendError(
                "turn/interrupt",
                TimeoutError("initialize timed out"),
            )

        handler._adapter.interrupt_turn = _raise_pre_send

        ok, message = handler._runtime_call(
            handler._feishu_surface.cancel_current_turn,
            "ou_user",
            "c1",
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(ok)
        self.assertIn("取消请求未发送", message)
        self.assertFalse(state["cancelled"])
        self.assertTrue(state["pending_cancel"])
        self.assertEqual(state["runtime_channel_state"], "degraded")
        self.assertEqual(state["current_turn_id"], "turn-1")

        self._on_turn_completed(handler,
            {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}}
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["pending_cancel"])
        self.assertFalse(state["cancelled"])

    def test_continue_auto_resumes_bound_thread_when_loaded_thread_is_missing(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "turn/completed",
            {
                "threadId": "thread-created",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )

        original_start_turn = handler._adapter.start_turn
        attempts: list[str] = []

        def _start_turn_with_missing_loaded_thread(**kwargs):
            attempts.append(kwargs["thread_id"])
            if len(attempts) == 1:
                raise CodexRpcError(
                    "turn/start",
                    {"code": -32000, "message": "thread not found: thread-created"},
                )
            return original_start_turn(**kwargs)

        handler._adapter.start_turn = _start_turn_with_missing_loaded_thread

        handler.handle_message("ou_user", "c1", "继续")

        self.assertEqual(attempts, ["thread-created", "thread-created"])
        self.assertEqual(
            handler._adapter.resume_thread_calls,
            [
                {
                    "thread_id": "thread-created",
                    "config_overrides": None,
                    "model": None,
                    "model_provider": None,
                    "approval_policy": None,
                    "permissions_profile_id": None,
                }
            ],
        )
        self.assertEqual(len(handler._adapter.create_thread_calls), 1)
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["current_thread_id"], "thread-created")
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_reconcile_runtime_loss_keeps_thread_binding_for_next_prompt(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler._adapter.thread_snapshots[("thread-created", True)] = CodexRpcError(
            "thread/read",
            {"code": -32000, "message": "thread not found: thread-created"},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")

        original_start_turn = handler._adapter.start_turn
        attempts: list[str] = []

        def _start_turn_with_missing_loaded_thread(**kwargs):
            attempts.append(kwargs["thread_id"])
            if len(attempts) == 1:
                raise CodexRpcError(
                    "turn/start",
                    {"code": -32000, "message": "thread not found: thread-created"},
                )
            return original_start_turn(**kwargs)

        handler._adapter.start_turn = _start_turn_with_missing_loaded_thread

        handler.handle_message("ou_user", "c1", "继续")

        self.assertEqual(attempts, ["thread-created", "thread-created"])
        self.assertEqual(
            handler._adapter.resume_thread_calls[-1],
            {
                "thread_id": "thread-created",
                "config_overrides": None,
                "model": None,
                "model_provider": None,
                "approval_policy": None,
                "permissions_profile_id": None,
            },
        )
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_running_p2p_prompt_queues_and_drains_after_current_turn(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "follow up")

    def test_recalled_queued_prompt_is_removed_before_drain(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello", message_id="m-1")
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        self.assertEqual(bot.replies[-1], ("c1", "已排队，将在当前执行结束后继续。队列位置：1"))

        handler._runtime_call(
            handler._feishu_surface.handle_message_recalled_impl,
            "c1",
            "m-2",
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

    def test_recalled_running_prompt_does_not_cancel_current_turn(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello", message_id="m-1")
        handler._runtime_call(handler._feishu_surface.handle_message_recalled_impl, "c1", "m-1")

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertTrue(state["running"])
        self.assertEqual(state["current_prompt_message_id"], "m-1")
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)

    def test_queued_group_prompt_keeps_origin_context_after_message_context_expires(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler.handle_message("ou_user", "chat-group", "第二轮", message_id="m-2")
        bot.message_contexts.pop("m-2", None)

        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "chat-group", "m-2")
        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "第二轮")
        self.assertEqual(state["current_actor_open_id"], "ou_user")
        self.assertEqual(bot.reply_ref_calls[-1][0], "m-2")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_running_group_turn_routes_same_binding_followup(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")

        self.assertTrue(handler.should_route_group_followup_prompt("ou_user", "chat-group", message_id="m-2"))

    def test_group_followup_route_requires_running_turn_but_not_same_actor(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        self.assertFalse(handler.should_route_group_followup_prompt("ou_user", "chat-group", message_id="m-1"))

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")

        self.assertTrue(handler.should_route_group_followup_prompt("ou_user2", "chat-group", message_id="m-2"))

    def test_running_group_prompt_queues_for_different_actor_on_same_binding(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        handler.handle_message("ou_user2", "chat-group", "插播", message_id="m-2")

        self.assertEqual(bot.reply_parents[-1], ("chat-group", "已排队，将在当前执行结束后继续。队列位置：1", "m-2"))

    def test_queued_prompt_prepares_deferred_assistant_context_before_dequeue_start(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user2",
            "sender_user_id": "u-user2",
            "thread_id": "om-thread",
            "assistant_context_mode": "deferred_recovery",
            "assistant_context_seq": 7,
            "created_at": 1712476800000,
            "sender_name": "Alice",
        }
        bot.queued_prompt_text_overrides["m-2"] = "prepared assistant prompt"

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler.handle_message("ou_user2", "chat-group", "请处理", message_id="m-2")
        bot.message_contexts.pop("m-2", None)

        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "prepared assistant prompt")
        self.assertEqual(
            bot.queued_prompt_preparations[-1],
            {
                "chat_id": "chat-group",
                "message_id": "m-2",
                "text": "请处理",
                "assistant_context_mode": "deferred_recovery",
                "assistant_context_created_at": 1712476800000,
                "assistant_context_seq": 7,
                "assistant_context_sender_name": "Alice",
                "origin_feishu_thread_id": "om-thread",
            },
        )

    def test_queued_prompt_prepare_failure_does_not_block_following_queue_item(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user2",
            "assistant_context_mode": "deferred_recovery",
            "assistant_context_seq": 7,
            "created_at": 1712476800000,
            "sender_name": "Alice",
        }
        bot.message_contexts["m-3"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.queued_prompt_text_overrides["m-2"] = None

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler.handle_message("ou_user2", "chat-group", "会失败的 queued mention", message_id="m-2")
        handler.handle_message("ou_user", "chat-group", "后续 prompt", message_id="m-3")

        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual([call["message_id"] for call in bot.queued_prompt_preparations], ["m-2", "m-3"])
        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["text"], "后续 prompt")

    def test_snapshot_timeout_only_marks_runtime_degraded(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler._adapter.thread_snapshots[("thread-created", True)] = TimeoutError(
            "Codex request timed out: thread/read"
        )

        finalized = handler._reconcile_execution_snapshot(
            "ou_user",
            "c1",
            thread_id="thread-created",
            turn_id="turn-1",
        )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(finalized)
        self.assertTrue(state["running"])
        self.assertEqual(state["runtime_channel_state"], "degraded")
        self.assertEqual(state["current_turn_id"], "turn-1")
        self.assertEqual(state["execution_pages"].current_message_id, "plan-card-2")

    def test_terminal_signal_finalizes_immediately_before_background_reconcile(self) -> None:
        handler, _ = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "plan-card-2")
        schedule_reconcile.assert_called_once()

    def test_live_terminal_receipts_are_scheduled_without_touching_fifo_successor(
        self,
    ) -> None:
        handler, bot = self._make_handler()
        bot.reserved_execution_cards.update(
            {"m-1": "old-card", "m-2": "successor-card"}
        )
        handler.handle_message("ou_user", "c1", "hello", message_id="m-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-created", "turnId": "turn-1", "delta": "完成"},
        )
        handler.handle_message("ou_user", "c1", "follow up", message_id="m-2")

        receipts = []
        scheduled_targets = []
        successor_snapshots = []

        def present_terminal(captured, *, background=True):
            self.assertTrue(background)
            self.assertEqual(captured.execution.current_prompt_message_id, "m-1")
            page = captured.execution.pages.active_page
            assert page is not None
            receipt = TerminalExecutionPageReceipt(
                message_id=page.message_id,
                cursor_start=page.cursor_start,
                cursor_end=ExecutionTranscriptCursor.from_transcript(
                    captured.execution.transcript
                ),
            )
            receipts.append(receipt)
            successor = handler._binding_runtime.resolve_session("ou_user", "c1")
            self.assertEqual(successor.execution.current_prompt_message_id, "m-2")
            self.assertEqual(successor.execution.current_message_id, "successor-card")
            self.assertTrue(successor.execution.running)
            self.assertTrue(successor.execution.awaiting_local_turn_started)
            successor_snapshots.append(successor)
            return (receipt,)

        def schedule(target):
            scheduled_targets.append(target)
            successor_snapshots.append(
                handler._binding_runtime.resolve_session("ou_user", "c1")
            )

        with (
            patch.object(
                handler._execution_output,
                "present_terminal_execution_card",
                side_effect=present_terminal,
            ),
            patch.object(
                handler,
                "_schedule_terminal_execution_reconcile",
                side_effect=schedule,
            ),
        ):
            self._on_turn_completed(
                handler,
                {
                    "threadId": "thread-created",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            )

        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(scheduled_targets), 1)
        target = scheduled_targets[0]
        assert target is not None
        self.assertEqual(target.prompt_message_id, "m-1")
        self.assertEqual(target.card_message_id, "old-card")
        self.assertEqual(target.terminal_page_receipts, tuple(receipts))
        self.assertEqual(len(successor_snapshots), 2)
        first_successor, scheduled_successor = successor_snapshots
        self.assertIs(first_successor.handle, scheduled_successor.handle)
        self.assertIs(
            first_successor.execution.pages,
            scheduled_successor.execution.pages,
        )
        self.assertEqual(first_successor.execution, scheduled_successor.execution)
        self.assertEqual(len(handler._adapter.start_turn_calls), 2)

    def test_background_terminal_reconcile_only_patches_old_card(self) -> None:
        handler, bot = self._make_handler()

        target = _capture_reconcile(handler, "ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        self.assertIsNone(target)

        handler.handle_message("ou_user", "c1", "hello")
        target = _capture_reconcile(handler, "ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None

        handler._runtime_call(
            handler._terminal_execution.finalize_ingress,
            "ou_user",
            "c1",
        )
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(state, current_message_id="new-card")
            state["current_turn_id"] = "turn-2"
            state["running"] = True
            state["awaiting_local_turn_started"] = False

        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [
                        {"type": "agentMessage", "text": "hello final answer"},
                    ],
                }
            ],
        )

        _run_reconcile(handler, target)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["execution_pages"].current_message_id, "new-card")
        self._wait_until(
            lambda: any(
                message_id == target.card_message_id
                for message_id, _ in bot.patches
            )
        )
        self.assertTrue(any(message_id == target.card_message_id for message_id, _ in bot.patches))
        self.assertFalse(any(message_id == "new-card" for message_id, _ in bot.patches))

    def test_group_prompts_share_backend_state_by_chat_id(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-1"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        bot.message_contexts["m-2"] = {"chat_type": "group", "sender_open_id": "ou_user2"}

        handler.handle_message("ou_user", "chat-group", "第一轮", message_id="m-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})
        handler.handle_message("ou_user2", "chat-group", "第二轮", message_id="m-2")

        self.assertEqual(len(handler._adapter.create_thread_calls), 1)
        self.assertEqual(
            [call["thread_id"] for call in handler._adapter.start_turn_calls],
            ["thread-created", "thread-created"],
        )
        self.assertIs(_runtime_state(handler, "ou_user", "chat-group"), _runtime_state(handler, "ou_user2", "chat-group"))

    def test_p2p_stored_binding_survives_handler_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        project_dir = data_dir / "project"
        project_dir.mkdir()

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1.handle_message("ou_user", "c1", f"/cd {project_dir}")
        handler1.handle_message("ou_user", "c1", "/permissions danger-full-access")
        handler1.handle_message("ou_user", "c1", "/model gpt-5.5")
        handler1.handle_message("ou_user", "c1", "/effort high")
        handler1.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler1,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(
            handler1,
            {
                "threadId": "thread-created",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        self._wait_until(
            lambda: handler1._interaction_lease_store.load("thread-created") is None
        )

        handler2, _ = self._make_handler(data_dir=data_dir)
        state = _runtime_state(handler2, "ou_user", "c1")

        self.assertEqual(state["working_dir"], str(project_dir))
        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["current_thread_title"], "（无标题）")
        self.assertEqual(state["approval_policy"], "never")
        self.assertEqual(state["permissions_profile_id"], ":danger-full-access")
        self.assertEqual(state["model"], "gpt-5.5")
        self.assertEqual(state["reasoning_effort"], "high")
        self.assertFalse(state["running"])

        handler2.handle_message("ou_user", "c1", "follow up")

        self.assertEqual(len(handler2._adapter.create_thread_calls), 0)
        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")

    def test_p2p_stored_binding_hydrates_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, _ = self._make_handler(data_dir=data_dir)
        handler1.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler1,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(
            handler1,
            {
                "threadId": "thread-created",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )
        self._wait_until(
            lambda: handler1._interaction_lease_store.load("thread-created") is None
        )

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        state = _runtime_state(handler2, "ou_user", "c1")

        self.assertEqual(state["current_thread_id"], "thread-created")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._binding_runtime_coordinator.thread_subscribers("thread-created"), ())

        handler2.handle_message("ou_user", "c1", "follow up")
        self._dispatch_adapter_notification(
            handler2,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler2,
            "item/agentMessage/delta",
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "delta": "恢复后事件正常路由",
            },
        )
        self._on_turn_completed(handler2, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-created")
        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-created")
        self.assertEqual(_runtime_state(handler2, "ou_user", "c1")["feishu_runtime_state"], "attached")
        self._wait_until(
            lambda: any("恢复后事件正常路由" in payload for _id, payload in bot2.patches)
        )

    def test_group_stored_binding_survives_handler_restart(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, bot1 = self._make_handler(data_dir=data_dir)
        bot1.message_contexts["m-bind"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        _bind_authoritative_thread(handler1,
            "ou_user",
            "chat-group",
            ThreadSummary(
                thread_id="thread-group",
                cwd="/tmp/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            message_id="m-bind",
        )

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        bot2.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        state = _runtime_state(
            handler2, "ou_user2", "chat-group", "m-status"
        )

        self.assertEqual(state["current_thread_id"], "thread-group")
        self.assertIn(("__group__", "chat-group"), self._binding_keys(handler2))

        bot2.message_contexts["m-prompt"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        handler2.handle_message("ou_user2", "chat-group", "第二轮", message_id="m-prompt")

        self.assertEqual(len(handler2._adapter.create_thread_calls), 0)
        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-group")

    def test_group_stored_binding_hydrates_detached_and_next_prompt_attaches(self) -> None:
        tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)

        handler1, bot1 = self._make_handler(data_dir=data_dir)
        bot1.message_contexts["m-bind"] = {"chat_type": "group", "sender_open_id": "ou_user"}
        _bind_authoritative_thread(handler1,
            "ou_user",
            "chat-group",
            ThreadSummary(
                thread_id="thread-group",
                cwd="/tmp/project",
                name="",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            message_id="m-bind",
        )

        handler2, bot2 = self._make_handler(data_dir=data_dir)
        bot2.message_contexts["m-status"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        state = _runtime_state(
            handler2, "ou_user2", "chat-group", "m-status"
        )

        self.assertEqual(state["current_thread_id"], "thread-group")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler2._binding_runtime_coordinator.thread_subscribers("thread-group"), ())

        bot2.message_contexts["m-prompt"] = {"chat_type": "group", "sender_open_id": "ou_user2"}
        handler2.handle_message("ou_user2", "chat-group", "继续", message_id="m-prompt")
        self._dispatch_adapter_notification(
            handler2,
            "turn/started",
            {"threadId": "thread-group", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler2,
            "item/agentMessage/delta",
            {
                "threadId": "thread-group",
                "turnId": "turn-1",
                "delta": "群重启后事件正常路由",
            },
        )
        self._on_turn_completed(handler2, {"threadId": "thread-group", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(handler2._adapter.start_turn_calls[0]["thread_id"], "thread-group")
        self.assertEqual(handler2._adapter.resume_thread_calls[-1]["thread_id"], "thread-group")
        self.assertEqual(_runtime_state(handler2, "ou_user2", "chat-group", "m-status")["feishu_runtime_state"], "attached")
        self._wait_until(
            lambda: any("群重启后事件正常路由" in payload for _id, payload in bot2.patches)
        )

    def test_restart_downgrades_multi_subscriber_feishu_runtime_and_owner(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
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

        handler1, _ = self._make_handler(data_dir=data_dir)
        _bind_authoritative_thread(handler1, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler1, "ou_user", "chat-b", thread)
        handler1.handle_message("ou_user", "chat-a", "first turn")

        # Both handlers run in this test process, so explicitly model the old
        # service PID as dead while the replacement loads the lease store.
        with patch(
            "bot.stores.interaction_lease_store.process_exists",
            return_value=False,
        ):
            handler2, bot2 = self._make_handler(data_dir=data_dir)
            _reg(handler2, bot2)
            self.assertIsNone(
                handler2._interaction_lease_store.load("thread-1")
            )

        self.assertEqual(handler2._binding_runtime_coordinator.thread_subscribers("thread-1"), ())
        interaction_owner = handler2._binding_runtime.interaction_owner_snapshot_locked(
            "thread-1",
            current_binding=("ou_user", "chat-a"),
        )
        self.assertEqual(interaction_owner["kind"], "none")
        self.assertEqual(_runtime_state(handler2, "ou_user", "chat-a")["feishu_runtime_state"], "detached")
        self.assertEqual(_runtime_state(handler2, "ou_user", "chat-b")["feishu_runtime_state"], "detached")

        self._adapter_request(handler2,
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )
        self._dispatch_adapter_notification(
            handler2,
            "item/agentMessage/delta",
            {"threadId": "thread-1", "turnId": "turn-1", "delta": "恢复后继续"},
        )

        self.assertEqual(bot2.sent_messages, [])
        self.assertEqual(_runtime_state(handler2, "ou_user", "chat-a")["execution_transcript"].reply_text(), "")
        self.assertEqual(_runtime_state(handler2, "ou_user", "chat-b")["execution_transcript"].reply_text(), "")

    def test_constructor_hydration_is_read_only_until_service_ownership(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou_user", "chat-a")
        ChatBindingStore(data_dir).save(
            binding,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": "thread-1",
                "current_thread_title": "demo",
                "feishu_runtime_state": "attached",
                "approval_policy": "never",
                "permissions_profile_id": ":danger-full-access",
                "model": "",
                "reasoning_effort": "",
            },
        )
        leases = InteractionLeaseStore(data_dir)
        acquired = leases.acquire(
            "thread-1",
            make_feishu_interaction_holder(binding[0], binding[1], owner_pid=os.getpid()),
        )
        self.assertTrue(acquired.granted)

        handler, bot = self._make_handler(data_dir=data_dir)

        stored_before_register = ChatBindingStore(data_dir).load(binding)
        assert stored_before_register is not None
        self.assertEqual(stored_before_register["feishu_runtime_state"], "attached")
        self.assertIsNotNone(leases.load("thread-1"))
        with handler._lock:
            record = handler._binding_runtime.binding_record_snapshot_locked(binding)
            self.assertEqual(handler._binding_runtime.binding_keys_locked(), ())
        self.assertIsNotNone(record)
        assert record is not None
        self.assertFalse(record.runtime_resident)
        self.assertEqual(record.feishu_runtime_state, "detached")
        stored_after_inspection = ChatBindingStore(data_dir).load(binding)
        assert stored_after_inspection is not None
        self.assertEqual(
            stored_after_inspection["feishu_runtime_state"],
            "attached",
        )
        self.assertIsNotNone(leases.load("thread-1"))

        _reg(handler, bot)

        stored_after_register = ChatBindingStore(data_dir).load(binding)
        assert stored_after_register is not None
        self.assertEqual(stored_after_register["feishu_runtime_state"], "detached")
        self.assertIsNone(leases.load("thread-1"))

    def test_adapter_disconnect_fail_closes_attached_runtime_state(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-created", "turnId": "turn-1", "delta": "partial"},
        )

        handler._runtime_call(handler._adapter_events.handle_disconnect_impl)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-created"), ())
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertIn("Codex websocket disconnected", state["execution_transcript"].process_text())
        self._wait_until(
            lambda: any(
                "Codex websocket disconnected" in payload
                for _message_id, payload in bot.patches
            )
        )
        self.assertTrue(any("Codex websocket disconnected" in payload for _message_id, payload in bot.patches))

    def test_adapter_disconnect_fail_closes_pending_interaction_requests_without_upstream_response(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        _store_pending(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-created"},
            "thread_id": "thread-created",
            "title": "Codex 命令执行审批",
            "message_id": "approval-card-1",
            "chat_id": "c1",
            "sender_id": "ou_user",
            "status": "pending",
        })

        handler._runtime_call(handler._adapter_events.handle_disconnect_impl)

        self.assertEqual(
            handler._interaction_requests.pending_request_snapshot("req-1")["status"],
            "submitted_unknown",
        )
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertFalse(
            any(message_id == "approval-card-1" for message_id, _payload in bot.patches)
        )

    def test_adapter_disconnect_fail_closes_pending_interaction_requests_even_without_attached_binding(self) -> None:
        handler, bot = self._make_handler()

        _store_pending(handler, "req-1", {
            "rpc_request_id": "rpc-1",
            "method": "item/tool/requestUserInput",
            "params": {"threadId": "thread-created"},
            "thread_id": "thread-created",
            "title": "Codex 用户输入",
            "message_id": "approval-card-1",
            "chat_id": "c1",
            "sender_id": "ou_user",
            "status": "pending",
        })

        handler._runtime_call(handler._adapter_events.handle_disconnect_impl)

        self.assertEqual(
            handler._interaction_requests.pending_request_snapshot("req-1")["status"],
            "submitted_unknown",
        )
        self.assertEqual(handler._adapter.respond_calls, [])
        self.assertFalse(
            any(message_id == "approval-card-1" for message_id, _payload in bot.patches)
        )

    def test_status_shows_untitled_instead_of_unbound_when_thread_exists(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        handler.handle_message("ou_user", "c1", "/status")

        _, card = bot.cards[-1]
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("当前线程：`thread-c…` （无标题）", rendered)
        self.assertNotIn("（未绑定线程）", rendered)

    def test_turn_completed_finalizes_immediately_and_schedules_terminal_reconcile(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-created", "turnId": "turn-1", "delta": "完整"},
        )
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "完整")
        schedule_reconcile.assert_called_once()
        self._wait_until(
            lambda: any("完整" in payload for _message_id, payload in bot.patches)
        )

    def test_thread_status_inactive_settles_unidentified_admission_without_reconcile(
        self,
    ) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            self._dispatch_adapter_notification(
                handler,
                "thread/status/changed",
                {"threadId": "thread-created", "status": {"type": "idle"}},
            )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        schedule_reconcile.assert_not_called()
        self._wait_until(lambda: bool(bot.patches))
        patched_card = json.loads(bot.patches[-1][1])
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_thread_closed_finalizes_immediately_without_clearing_binding(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        with patch.object(handler, "_schedule_terminal_execution_reconcile") as schedule_reconcile:
            self._dispatch_adapter_notification(
                handler,
                "thread/closed",
                {"threadId": "thread-created"},
            )

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_thread_id"], "thread-created")
        schedule_reconcile.assert_called_once()
        self._wait_until(
            lambda: any(
                "取消执行" not in payload
                for _message_id, payload in bot.patches
            )
        )
        patched_card = json.loads(
            next(
                payload
                for _message_id, payload in bot.patches
                if "取消执行" not in payload
            )
        )
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_watchdog_reconciles_missed_terminal_notifications(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "c1", "hello")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        handler._adapter.thread_snapshots[("thread-created", True)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="idle",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [
                        {"type": "agentMessage", "text": "watchdog final"},
                    ],
                }
            ],
        )
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            registration = state["mirror_watchdog_registration"]
            self.assertIsNotNone(registration)
            assert registration is not None
            registration.timer.cancel()

        handler._runtime_call(
            handler._adapter_ingress_gate.accept,
            handler._adapter.connection_generation_value,
        )
        handler._execution_recovery.run_mirror_watchdog(registration.ticket)

        state = _runtime_state(handler, "ou_user", "c1")
        self.assertFalse(state["running"])
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "watchdog final",
        )
        self.assertEqual(state["terminal_result_text"], "")
        card = json.loads(bot.sent_messages[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("watchdog final", card["body"]["elements"][-1]["content"])

    def test_cancel_refreshes_stale_execution_card_when_turn_already_finished(self) -> None:
        handler, bot = self._make_handler()

        handler.handle_message("ou_user", "c1", "hello")
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            state["running"] = False
            state["current_turn_id"] = ""
            state["execution_transcript"].set_reply_text("done")

        ok, message = handler._runtime_call(
            handler._feishu_surface.cancel_current_turn,
            "ou_user",
            "c1",
        )

        self.assertTrue(ok)
        self.assertEqual(message, "当前执行已结束，已刷新卡片状态。")
        patched_card = json.loads(bot.patches[-1][1])
        body_elements = patched_card["body"]["elements"]
        self.assertFalse(
            any(
                isinstance(element, dict)
                and element.get("tag") == "button"
                and element.get("text", {}).get("content") == "取消执行"
                for element in body_elements
            )
        )

    def test_local_turn_started_reuses_existing_execution_card(self) -> None:
        handler, bot = self._make_handler()
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
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(state, current_message_id="existing-card")
            state["awaiting_local_turn_started"] = True
            state["running"] = True

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["execution_pages"].current_message_id, "existing-card")

    def test_duplicate_turn_started_does_not_open_second_execution_card(self) -> None:
        handler, bot = self._make_handler()
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
        state = _runtime_state(handler, "ou_user", "c1")
        with handler._lock:
            _set_pages(state, current_message_id="existing-card")
            state["current_turn_id"] = "turn-1"
            state["running"] = True
            state["awaiting_local_turn_started"] = False

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        self.assertEqual(len(bot.sent_messages), 0)
        self.assertEqual(_runtime_state(handler, "ou_user", "c1")["execution_pages"].current_message_id, "existing-card")

    def test_group_thread_binding_is_not_treated_as_takeover_for_same_chat(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
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

        _bind_authoritative_thread(handler, "ou_user", "chat-group", thread)
        _bind_authoritative_thread(handler, "ou_user2", "chat-group", thread)

        self.assertEqual(bot.replies, [])

    def test_rebinding_same_thread_does_not_unsubscribe_current_subscription(self) -> None:
        handler, _ = self._make_handler()
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
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)

        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])

    def test_bind_thread_failure_keeps_existing_service_runtime_lease(self) -> None:
        handler, bot = self._make_handler()
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
        _reg(handler, bot)
        _bind_authoritative_thread(handler, "ou_user", "c1", thread)
        holder_ids_before = self._service_runtime_holder_ids(handler, "thread-1")

        with patch.object(
            handler._binding_runtime,
            "resolve_session",
            side_effect=RuntimeError("bind failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "bind failed"):
                _bind_authoritative_thread(handler, "ou_user2", "c2", thread)

        self.assertEqual(self._service_runtime_holder_ids(handler, "thread-1"), holder_ids_before)
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), (("ou_user", "c1"),))
