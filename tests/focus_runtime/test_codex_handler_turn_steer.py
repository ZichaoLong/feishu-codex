from __future__ import annotations

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import CodexRpcError, CodexRpcTransportError
from bot.stores.pending_attachment_store import PendingAttachmentRecord
from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
    _runtime_state,
)
from tests.focus_runtime.codex_handler_test_harness import CodexHandlerHarness


class CodexHandlerTurnSteerTests(CodexHandlerHarness):
    @staticmethod
    def _active_thread(thread_id: str = "thread-1") -> ThreadSummary:
        return ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="active",
        )

    def _prepare_active_binding(
        self,
        handler,
        *,
        sender_id: str = "ou_user",
        chat_id: str = "c1",
        message_id: str = "",
        execution_kind: str = "prompt",
        thread: ThreadSummary | None = None,
    ) -> dict:
        active_thread = thread or self._active_thread()
        _bind_authoritative_thread(
            handler,
            sender_id,
            chat_id,
            active_thread,
            message_id=message_id,
        )
        state = _runtime_state(handler, sender_id, chat_id, message_id)
        with handler._lock:
            state["running"] = True
            state["current_turn_id"] = "turn-1"
            state["current_execution_kind"] = execution_kind
            state["started_at"] = 1.0
        return state

    def test_explicit_steer_sends_one_exact_text_without_next_turn_side_effects(
        self,
    ) -> None:
        handler, bot = self._make_handler()
        state = self._prepare_active_binding(handler)
        attachment = PendingAttachmentRecord(
            sender_id="ou_user",
            chat_id="c1",
            thread_id="thread-1",
            message_id="attachment-1",
            attachment_type="file",
            resource_key="resource-1",
            display_name="notes.txt",
            local_path="/tmp/project/.focus/attachments/notes.txt",
            created_at=1.0,
            expires_at=9_999_999_999.0,
        )
        handler._pending_attachment_store.add(attachment)
        binding = ("ou_user", "c1")
        pages = state["execution_pages"]
        settings = (
            state["approval_policy"],
            state["permissions_profile_id"],
            state["model"],
            state["reasoning_effort"],
        )
        queue_before = self._queue_snapshot(handler, binding)

        handler.handle_message(
            "ou_user",
            "c1",
            "/steer   add this constraint   ",
            message_id="steer-1",
        )

        self.assertIn("已将文本补充到当前 turn", bot.replies[-1][1])
        self.assertEqual(
            handler._adapter.steer_turn_calls,
            [
                {
                    "thread_id": "thread-1",
                    "expected_turn_id": "turn-1",
                    "input_items": [
                        {"type": "text", "text": "add this constraint"}
                    ],
                    "client_user_message_id": None,
                    "expected_connection_generation": 1,
                }
            ],
        )
        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(self._queue_snapshot(handler, binding), queue_before)
        self.assertEqual(handler._pending_attachment_store.list_all(), (attachment,))
        current = _runtime_state(handler, "ou_user", "c1")
        self.assertIs(current["execution_pages"], pages)
        self.assertEqual(
            (
                current["approval_policy"],
                current["permissions_profile_id"],
                current["model"],
                current["reasoning_effort"],
            ),
            settings,
        )

    def test_active_observer_can_steer_while_ordinary_text_still_queues(
        self,
    ) -> None:
        handler, bot = self._make_handler()
        self._prepare_active_binding(handler, execution_kind="active_observer")

        handler.handle_message(
            "ou_user",
            "c1",
            "/steer observer contribution",
            message_id="steer-observer",
        )
        handler.handle_message(
            "ou_user",
            "c1",
            "ordinary next turn",
            message_id="ordinary-1",
        )

        self.assertEqual(len(handler._adapter.steer_turn_calls), 1)
        self.assertEqual(handler._adapter.start_turn_calls, [])
        queue = self._queue_snapshot(handler, ("ou_user", "c1"))
        self.assertEqual(queue.pending_count, 1)
        self.assertEqual(queue.pending_message_ids, ("ordinary-1",))
        self.assertIn("已排队", bot.replies[-1][1])

    def test_help_form_submits_through_the_same_explicit_steer_route(self) -> None:
        handler, _ = self._make_handler()
        self._prepare_active_binding(handler)
        handler.handle_message(
            "ou_user",
            "c1",
            "/help runtime",
            message_id="msg-help-source",
        )

        response = self._unpack_card_response(
            handler.handle_card_action(
                "ou_user",
                "c1",
                "msg-steer-form",
                {
                    "action": "help_submit_command",
                    "command": "/steer",
                    "field_name": "steer_text",
                    "title": "Codex Steer",
                    "_form_value": {"steer_text": "form contribution"},
                },
            )
        )

        self.assertEqual(
            response["card"]["header"]["title"]["content"],
            "Codex Steer",
        )
        self.assertIn(
            "已将文本补充到当前 turn",
            response["card"]["elements"][0]["content"],
        )
        self.assertEqual(
            handler._adapter.steer_turn_calls[-1]["input_items"],
            [{"type": "text", "text": "form contribution"}],
        )

    def test_group_route_requires_admin_and_all_mode_exclusivity(self) -> None:
        handler, bot = self._make_handler()
        thread = self._active_thread()
        _bind_authoritative_thread(handler, "ou_user", "c-other", thread)
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["group-member"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user2",
        }
        bot.message_contexts["group-admin"] = {
            "chat_type": "group",
            "sender_open_id": "ou_admin",
        }
        self._prepare_active_binding(
            handler,
            sender_id="ou_admin",
            chat_id="chat-group",
            message_id="group-admin",
            thread=thread,
        )

        handler.handle_message(
            "ou_user2",
            "chat-group",
            "/steer denied member",
            message_id="group-member",
        )

        self.assertIn("群里的 `/` 命令仅管理员可用", bot.replies[-1][1])
        self.assertEqual(handler._adapter.steer_turn_calls, [])

        bot.group_modes["chat-group"] = "all"
        handler.handle_message(
            "ou_admin",
            "chat-group",
            "/steer denied shared all",
            message_id="group-admin",
        )

        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("未加入队列", bot.replies[-1][1])
        self.assertEqual(handler._adapter.steer_turn_calls, [])

    def test_known_rejection_and_transport_unknown_are_not_retried(self) -> None:
        cases = (
            (
                CodexRpcError(
                    "turn/steer",
                    {"code": -32002, "message": "no active turn to steer"},
                ),
                "明确拒绝",
            ),
            (
                CodexRpcTransportError(
                    "turn/steer",
                    {"message": "Codex websocket disconnected"},
                ),
                "结果未知",
            ),
        )
        for error, expected_text in cases:
            with self.subTest(error=type(error).__name__):
                handler, bot = self._make_handler()
                self._prepare_active_binding(handler)
                handler._adapter.steer_turn_results.append(error)

                handler.handle_message(
                    "ou_user",
                    "c1",
                    "/steer one attempt",
                    message_id="steer-failure",
                )

                self.assertIn(expected_text, bot.replies[-1][1])
                self.assertEqual(len(handler._adapter.steer_turn_calls), 1)
                self.assertEqual(handler._adapter.start_turn_calls, [])
