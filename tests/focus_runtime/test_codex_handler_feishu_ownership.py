import json
import os
import pathlib
import tempfile
from unittest.mock import patch

from tests.focus_runtime.codex_handler_fakes import (
    _bind_authoritative_thread,
    _register_handler as _reg,
)
from tests.focus_runtime.codex_handler_fakes import _capture_reconcile, _run_reconcile, _runtime_state
from tests.focus_runtime.feishu_owner_test_support import (
    apply_destination_loss_event as _lose_chat,
)
from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
)
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.codex_protocol.client import (
    CodexRpcPreSendError,
)
from bot.feishu_root_operation_contract import (
    FeishuRootOperationPoisoned,
    FeishuRootOperationRetentionError,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_fcodex_interaction_holder,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)

from tests.focus_runtime.codex_handler_test_harness import (
    CodexHandlerHarness,
)


class CodexHandlerFeishuOwnershipTests(CodexHandlerHarness):
    def test_group_terminal_result_card_stays_on_trigger_message(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        target = _capture_reconcile(handler,
            "ou_user",
            "chat-group",
            thread_id="thread-created",
            turn_id="turn-1",
        )
        assert target is not None
        handler._runtime_call(
            handler._terminal_execution.finalize_ingress,
            "ou_user",
            "chat-group",
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
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "123456789"}],
                }
            ],
        )

        _run_reconcile(handler, target)

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertEqual(bot.reply_ref_calls[-1][3], True)
        card = json.loads(bot.reply_refs[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("123456789", card["body"]["elements"][-1]["content"])

    def test_group_terminal_result_card_stays_in_topic_after_message_context_is_gone(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        target = _capture_reconcile(handler,
            "ou_user",
            "chat-group",
            thread_id="thread-created",
            turn_id="turn-1",
        )
        assert target is not None
        bot.message_contexts.pop("m-thread", None)
        handler._runtime_call(
            handler._terminal_execution.finalize_ingress,
            "ou_user",
            "chat-group",
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
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "123456789"}],
                }
            ],
        )

        _run_reconcile(handler, target)

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertEqual(bot.reply_ref_calls[-1][3], True)

    def test_multiple_bindings_share_thread_but_only_owner_can_write_until_turn_finishes(self) -> None:
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

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler, "ou_user", "chat-b", thread)
        self.assertEqual(handler._binding_runtime_coordinator.thread_subscribers("thread-1"), (("ou_user", "chat-a"), ("ou_user", "chat-b")))

        handler.handle_message("ou_user", "chat-a", "first turn")

        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-a"),
            )["relation"],
            "current",
        )
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        handler.handle_message("ou_user", "chat-b", "second turn")

        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(bot.replies[-1][0], "chat-b")
        self.assertIn("当前线程正由另一飞书会话执行", bot.replies[-1][1])
        self.assertIn("不能写入", bot.replies[-1][1])
        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-a"),
            )["relation"],
            "current",
        )

        self._on_turn_completed(handler, {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        # Matching main-turn completion releases its exact writer lease.  The
        # test still waits because event dispatch itself is asynchronous.
        self._wait_until(
            lambda: handler._binding_runtime.interaction_owner_snapshot_locked("thread-1")["kind"]
            == "none"
        )

        handler.handle_message("ou_user", "chat-b", "third turn")

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        self.assertEqual(handler._adapter.start_turn_calls[-1]["thread_id"], "thread-1")
        self.assertEqual(
            handler._binding_runtime.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=("ou_user", "chat-b"),
            )["relation"],
            "current",
        )

    def test_child_activity_is_not_projected_and_does_not_hold_main_turn(self) -> None:
        handler, bot = self._make_handler()
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        child = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="child",
            preview="",
            created_at=2,
            updated_at=3,
            source="subAgent",
            status="active",
            parent_thread_id="thread-1",
            can_accept_direct_input=False,
            subagent_kind="threadSpawn",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=root)
        handler._adapter.thread_snapshots[("child-1", None)] = ThreadSnapshot(summary=child)
        _bind_authoritative_thread(handler, "ou_user", "chat-a", root)
        handler.handle_message("ou_user", "chat-a", "first turn")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        handler._runtime_call(
            handler._web_runtime.handle_notification,
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "spawn-1",
                    "type": "collabAgentToolCall",
                    "tool": "spawnAgent",
                    "status": "completed",
                    "receiverThreadIds": ["child-1"],
                    "agentsStates": {"child-1": {"status": "running"}},
                },
            },
        )
        handler._runtime_call(
            handler._web_runtime.handle_notification,
            "turn/started",
            {
                "threadId": "child-1",
                "turn": {"id": "child-turn", "status": "inProgress", "items": []},
            },
        )
        reply_count = len(bot.replies)
        patch_count = len(bot.patches)

        self._on_turn_completed(handler,
            {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}
        )

        state = _runtime_state(handler, "ou_user", "chat-a")
        self.assertFalse(state["running"])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(len(bot.replies), reply_count)
        self.assertFalse(
            any(
                "subagent" in content.lower()
                for _message_id, content in bot.patches[patch_count:]
            )
        )

        handler.handle_message("ou_user", "chat-a", "second turn")

        self.assertEqual(len(handler._adapter.start_turn_calls), 2)
        state = _runtime_state(handler, "ou_user", "chat-a")
        self.assertTrue(state["running"])
        self.assertEqual(state["execution_transcript"].reply_text(), "")

    def test_terminal_feishu_fifo_successor_owns_only_its_exact_submission(self) -> None:

        for kind in ("prompt", "compact"):
            with self.subTest(kind=kind):
                handler, _ = self._make_handler()
                root_thread_id, binding, holder = self._prepare_terminal_feishu_fifo(
                    handler,
                    kind=kind,
                    message_id=f"queued-{kind}",
                )

                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                if kind == "prompt":
                    self.assertEqual(
                        handler._adapter.start_turn_calls[-1]["text"],
                        "queued follow-up",
                    )
                else:
                    self.assertEqual(handler._adapter.compact_thread_calls, [root_thread_id])
                state = _runtime_state(handler, *binding)
                self.assertTrue(handler._turn_execution.has_active_execution_locked(state))
                self.assertFalse(
                    self._queue_snapshot(
                        handler,
                        binding,
                    ).has_pending_or_draining
                )
                lease = handler._interaction_lease_store.load(root_thread_id)
                self.assertIsNotNone(lease)
                assert lease is not None
                self.assertTrue(lease.holder.same_holder(holder))

    def test_terminal_feishu_fifo_drop_or_known_start_failure_settles_owner(self) -> None:
        """Once no queued successor starts, the old terminal owner is released."""

        for outcome in ("drop", "known_start_failure"):
            with self.subTest(outcome=outcome):
                handler, bot = self._make_handler()
                root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
                    handler,
                    kind="prompt",
                    message_id=f"queued-{outcome}",
                )
                if outcome == "drop":
                    bot.queued_prompt_text_overrides[f"queued-{outcome}"] = None
                else:
                    attempts: list[str] = []

                    def _known_start_failure(**kwargs):
                        attempts.append(str(kwargs["thread_id"] or ""))
                        raise CodexRpcPreSendError(
                            "turn/start",
                            RuntimeError("test upstream did not receive request"),
                        )

                    handler._adapter.start_turn = _known_start_failure

                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                if outcome == "known_start_failure":
                    self.assertEqual(attempts, [root_thread_id])
                self.assertFalse(
                    self._queue_snapshot(
                        handler,
                        binding,
                    ).has_pending_or_draining
                )
                state = _runtime_state(handler, *binding)
                self.assertFalse(handler._turn_execution.has_active_execution_locked(state))
                self.assertIsNone(handler._interaction_lease_store.load(root_thread_id))

    def test_resume_rejects_thread_shared_by_all_mode_group(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-a"] = "group"
        bot.chat_types["chat-b"] = "group"
        bot.group_modes["chat-a"] = "all"
        bot.message_contexts["m-b"] = {"chat_type": "group", "sender_open_id": "ou_admin"}
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
        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)

        handler._runtime_call(
            handler._threads_ui_domain._resume_target_on_runtime,
            "ou_user2",
            "chat-b",
            "thread-1",
            message_id="m-b",
        )

        self.assertEqual(_runtime_state(handler, "ou_user2", "chat-b", "m-b")["current_thread_id"], "")
        self.assertIn("`all` 模式", bot.replies[-1][1])
        self.assertIn("其他群聊独占", bot.replies[-1][1])

    def test_turn_completion_finalizes_all_subscribers_without_owner_notice(self) -> None:
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

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler, "ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")
        handler.handle_message("ou_user", "chat-b", "second turn")

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self._dispatch_adapter_notification(
            handler,
            "item/agentMessage/delta",
            {"threadId": "thread-1", "turnId": "turn-1", "delta": "done"},
        )
        self._on_turn_completed(handler, {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertNotIn(
            ("chat-b", "线程 `thread-1…` 的上一轮执行已结束；本会话现在可继续提问。"),
            bot.replies,
        )
        state_b = _runtime_state(handler, "ou_user", "chat-b")
        self.assertEqual(state_b["execution_pages"].current_message_id, "")
        self.assertTrue(state_b["execution_pages"].last_message_id)
        self.assertEqual(state_b["terminal_result_text"], "done")

    def test_destination_loss_clears_binding_and_persistence(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
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

        _lose_chat(handler, "chat-group", reason="disbanded")

        self.assertNotIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, ["thread-1"])
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "thread-1", "include_turns": False},
        )

        handler2, _ = self._make_handler(data_dir=data_dir)
        state = _runtime_state(handler2, "ou_user", "chat-group", "m-group")
        self.assertEqual(state["current_thread_id"], "")

    def test_deactivated_or_unavailable_binding_cannot_run_stale_fifo_after_recreate(self) -> None:
        """A recreated chat binding must never inherit old queued prompts."""

        for cleanup_kind in ("sender", "chat_unavailable"):
            with self.subTest(cleanup_kind=cleanup_kind):
                handler, bot = self._make_handler()
                chat_id = "chat-a"
                if cleanup_kind == "chat_unavailable":
                    bot.chat_types[chat_id] = "group"
                    binding = ("__group__", chat_id)
                else:
                    binding = ("ou_user", chat_id)
                old_thread = ThreadSummary(
                    thread_id="old-thread",
                    cwd="/tmp/project",
                    name="old",
                    preview="",
                    created_at=1,
                    updated_at=2,
                    source="cli",
                    status="idle",
                )
                _bind_authoritative_thread(handler, "ou_user", chat_id, old_thread)
                self._enqueue_feishu_queue_item(
                    handler,
                    kind="prompt",
                    binding=binding,
                    root_thread_id="old-thread",
                    sender_id="ou_user",
                    message_id="stale-queued-message",
                    text="must not run",
                    input_items=({"type": "text", "text": "must not run"},),
                )

                if cleanup_kind == "chat_unavailable":
                    _lose_chat(handler, chat_id, reason="test")
                else:
                    handler.deactivate_sender("ou_user", chat_id)

                self.assertFalse(
                    self._queue_snapshot(handler, binding).has_pending_or_draining
                )
                new_thread = ThreadSummary(
                    thread_id="new-thread",
                    cwd="/tmp/project",
                    name="new",
                    preview="",
                    created_at=3,
                    updated_at=4,
                    source="cli",
                    status="idle",
                )
                _bind_authoritative_thread(handler, "ou_user", chat_id, new_thread)
                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertEqual(
                    _runtime_state(handler, "ou_user", chat_id)["current_thread_id"],
                    "new-thread",
                )

    def test_lifecycle_invalidation_cancels_reentrant_claim_before_start(self) -> None:
        """Each binding-removal owner invalidates the exact queue receipt."""

        for action in (
            "sender_deactivate",
            "binding_detach",
            "thread_detach",
            "service_fail_close",
            "group_deactivate",
        ):
            with self.subTest(action=action):
                handler, bot = self._make_handler()
                chat_id = "chat-group" if action == "group_deactivate" else "chat-a"
                binding = (
                    (GROUP_SHARED_BINDING_OWNER_ID, chat_id)
                    if action == "group_deactivate"
                    else ("ou_user", chat_id)
                )
                if action == "group_deactivate":
                    bot.chat_types[chat_id] = "group"
                    bot.activate_group_chat(chat_id, activated_by="ou_admin")
                thread = ThreadSummary(
                    thread_id="thread-1",
                    cwd="/tmp/project",
                    name="demo",
                    preview="",
                    created_at=1,
                    updated_at=2,
                    source="cli",
                    status="idle",
                )
                _bind_authoritative_thread(handler, "ou_user", chat_id, thread)
                self._enqueue_feishu_queue_item(
                    handler,
                    kind="prompt",
                    binding=binding,
                    root_thread_id=thread.thread_id,
                    sender_id="ou_user",
                    message_id="queued-message",
                    text="must not start",
                )
                actions = {
                    "sender_deactivate": lambda: handler.deactivate_sender(
                        "ou_user", chat_id
                    ),
                    "binding_detach": lambda: handler._runtime_admin.detach_binding(
                        binding
                    ),
                    "thread_detach": lambda: handler._runtime_admin.detach_thread(
                        thread.thread_id
                    ),
                    "service_fail_close": (
                        handler._runtime_admin.fail_close_service_attached_runtime
                    ),
                    "group_deactivate": lambda: handler._feishu_surface.deactivate_group_chat(
                        chat_id
                    ),
                }
                handler._feishu_execution_queue_service._restore_message_origin = (
                    lambda _effect: actions[action]()
                )

                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertFalse(
                    self._queue_snapshot(handler, binding).has_pending_or_draining
                )

    def test_deactivate_sender_cleans_legacy_thread_spawn_binding_without_unsubscribe(self) -> None:
        """A local legacy child bookmark cannot issue `thread/unsubscribe`."""

        handler, _ = self._make_handler()
        local_summary = ThreadSummary(
            thread_id="child-1",
            cwd="/tmp/project",
            name="legacy child bookmark",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-child", local_summary)
        # Simulate an older Focus version having persisted this binding before
        # the authoritative app-server summary exposed its ThreadSpawn kind.
        handler._adapter.thread_snapshots[("child-1", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="child-1",
                cwd="/tmp/project",
                name="actual child",
                preview="",
                created_at=0,
                updated_at=0,
                source="subAgent",
                status="idle",
                # A malformed ThreadSpawn record remains parent-owned too.
                parent_thread_id=None,
                subagent_kind="threadSpawn",
            )
        )

        with self.assertLogs("bot.focus_runtime", level="WARNING") as logged:
            handler.deactivate_sender("ou_user", "chat-child")

        self.assertNotIn(("ou_user", "chat-child"), self._binding_keys(handler))
        self.assertEqual(handler._binding_runtime.thread_subscribers("child-1"), ())
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "child-1", "include_turns": False},
        )
        self.assertIn("ThreadSpawn", logged.output[0])

    def test_prompt_rejects_legacy_thread_spawn_binding_before_writer_lease(self) -> None:
        handler, bot = self._make_handler()
        local_summary = ThreadSummary(
            thread_id="child-prompt",
            cwd="/tmp/project",
            name="legacy child bookmark",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(
            handler,
            "ou_user",
            "chat-child-prompt",
            local_summary,
        )
        handler._adapter.thread_snapshots[("child-prompt", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="child-prompt",
                cwd="/tmp/project",
                name="actual child",
                preview="",
                created_at=0,
                updated_at=0,
                source="subAgent",
                status="idle",
                parent_thread_id="root-1",
                subagent_kind="threadSpawn",
            )
        )

        handler.handle_message(
            "ou_user",
            "chat-child-prompt",
            "must not start",
        )

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertIsNone(handler._interaction_lease_store.load("child-prompt"))
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "child-prompt", "include_turns": False},
        )
        self.assertTrue(
            any("ThreadSpawn" in content for _chat_id, content in bot.replies)
        )

    def test_cancel_authority_reads_before_interrupting_known_thread_id(self) -> None:
        handler, _ = self._make_handler()
        handler.handle_message("ou_user", "chat-cancel", "start root")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        self.assertTrue(handler._direct_thread_targets.is_known("thread-created"))
        handler._adapter.thread_snapshots[("thread-created", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="thread-created",
                cwd="/tmp/project",
                name="actual child",
                preview="",
                created_at=0,
                updated_at=0,
                source="subAgent",
                status="active",
                parent_thread_id="root-1",
                subagent_kind="threadSpawn",
            )
        )

        ok, message = handler._runtime_call(
            handler._feishu_surface.cancel_current_turn,
            "ou_user",
            "chat-cancel",
        )

        self.assertFalse(ok)
        self.assertIn("取消请求未发送", message)
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "thread-created", "include_turns": False},
        )
        self.assertEqual(handler._adapter.interrupt_turn_calls, [])
        lease = handler._interaction_lease_store.load("thread-created")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.turn_id, "turn-1")

    def test_deactivate_sender_keeps_local_cleanup_when_authority_read_fails(self) -> None:
        """An unreadable legacy target retains runtime while local cleanup proceeds."""

        handler, bot = self._make_handler()
        _reg(handler, bot)
        thread = ThreadSummary(
            thread_id="thread-unreadable",
            cwd="/tmp/project",
            name="legacy bookmark",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-unreadable", thread)
        service_holder_id = (
            handler._service_runtime_authority.service_thread_runtime_holder().holder_id
        )
        handler._adapter.thread_snapshots[("thread-unreadable", None)] = RuntimeError(
            "app-server unavailable"
        )

        with self.assertLogs("bot.focus_runtime", level="WARNING") as logged:
            handler.deactivate_sender("ou_user", "chat-unreadable")

        self.assertNotIn(("ou_user", "chat-unreadable"), self._binding_keys(handler))
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "thread-unreadable", "include_turns": False},
        )
        self.assertIn(
            service_holder_id,
            self._service_runtime_holder_ids(handler, "thread-unreadable"),
        )
        self.assertIn("app-server unavailable", logged.output[0])

    def test_chat_unavailable_cleans_legacy_thread_spawn_binding_without_unsubscribe(self) -> None:
        """Chat teardown follows the same direct-root boundary as sender teardown."""

        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        local_summary = ThreadSummary(
            thread_id="child-group-1",
            cwd="/tmp/project",
            name="legacy group child bookmark",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-group", local_summary)
        handler._adapter.thread_snapshots[("child-group-1", None)] = ThreadSnapshot(
            summary=ThreadSummary(
                thread_id="child-group-1",
                cwd="/tmp/project",
                name="actual child",
                preview="",
                created_at=0,
                updated_at=0,
                source="subAgent",
                status="idle",
                parent_thread_id="root-1",
                subagent_kind="threadSpawn",
            )
        )

        with self.assertLogs("bot.focus_runtime", level="WARNING") as logged:
            _lose_chat(handler, "chat-group", reason="disbanded")

        self.assertNotIn(("__group__", "chat-group"), self._binding_keys(handler))
        self.assertEqual(handler._binding_runtime.thread_subscribers("child-group-1"), ())
        self.assertEqual(handler._adapter.unsubscribe_thread_calls, [])
        self.assertEqual(
            handler._adapter.read_thread_calls[-1],
            {"thread_id": "child-group-1", "include_turns": False},
        )
        self.assertIn("ThreadSpawn", logged.output[0])

    def test_turn_completion_skips_inactive_non_owner_subscribers(self) -> None:
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

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler, "ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")

        self._on_turn_completed(handler, {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertNotIn(
            ("chat-b", "线程 `thread-1…` 的上一轮执行已结束；本会话现在可继续提问。"),
            bot.replies,
        )

    def test_prompt_is_denied_when_shared_interaction_lease_is_owned_by_fcodex(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
        fcodex_holder = make_fcodex_interaction_holder(
            "fcodex:other",
            owner_pid=os.getpid(),
        )
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            fcodex_holder,
        )
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

        handler.handle_message("ou_user", "c1", "hello again")

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(bot.replies[-1][0], "c1")
        self.assertIn("另一终端执行", bot.replies[-1][1])

    def test_feishu_thread_management_mutations_do_not_bypass_web_writer(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _bot = self._make_handler(data_dir=data_dir)
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
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(summary=thread)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="existing objective",
            status="paused",
            token_budget=None,
            tokens_used=0,
            time_used_seconds=0,
            created_at=0,
            updated_at=0,
        )
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_web_interaction_holder("web-document", owner_pid=os.getpid()),
        )

        handler.handle_message("ou_user", "c1", "/rename should-not-apply")
        handler.handle_message("ou_user", "c1", "/archive")
        handler.handle_message("ou_user", "c1", "/goal set should-not-apply")
        handler.handle_message("ou_user", "c1", "/goal pause")
        handler.handle_message("ou_user", "c1", "/goal clear")

        self.assertEqual(handler._adapter.rename_thread_calls, [])
        self.assertEqual(handler._adapter.archive_thread_calls, [])
        self.assertEqual(handler._adapter.set_thread_goal_calls, [])
        self.assertIn("thread-1", handler._adapter.thread_goals)

    def test_feishu_resume_does_not_bypass_web_main_turn_writer(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
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
        handler._adapter.thread_snapshots[("thread-1", False)] = ThreadSnapshot(summary=thread)
        holder = make_web_interaction_holder(
            "web-document", owner_pid=os.getpid()
        )
        self._activate_main_turn_lease(handler, "thread-1", holder)

        handler._runtime_call(
            handler._threads_ui_domain._resume_target_on_runtime,
            "ou_other",
            "c2",
            "thread-1",
            summary=thread,
            message_id="resume-message",
        )

        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(handler._adapter.update_thread_settings_calls, [])
        self.assertEqual(
            _runtime_state(handler, "ou_other", "c2", "resume-message")["current_thread_id"],
            "",
        )
        self.assertIn("另一终端执行", bot.replies[-1][1])

    def test_approval_request_is_suppressed_when_shared_interaction_owner_is_fcodex(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, bot = self._make_handler(data_dir=data_dir)
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
        InteractionLeaseStore(data_dir).force_acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:other", owner_pid=os.getpid()),
        )

        self._adapter_request(handler,
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(bot.reply_refs, [])
        self.assertFalse(self._has_pending_request(handler, "req-1"))

    def test_approval_request_reply_stays_in_topic_after_message_context_is_gone(self) -> None:
        handler, bot = self._make_handler()
        bot.chat_types["chat-group"] = "group"
        bot.message_contexts["m-thread"] = {
            "chat_type": "group",
            "sender_open_id": "ou_user",
            "thread_id": "om_thread",
        }

        handler.handle_message("ou_user", "chat-group", "thread prompt", message_id="m-thread")
        bot.message_contexts.pop("m-thread", None)

        self._adapter_request(handler,
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-created",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.reply_refs[-1][0], "m-thread")
        self.assertTrue(bot.reply_ref_calls[-1][3])

    def test_approval_request_routes_to_current_interaction_owner(self) -> None:
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

        _bind_authoritative_thread(handler, "ou_user", "chat-a", thread)
        _bind_authoritative_thread(handler, "ou_user", "chat-b", thread)
        handler.handle_message("ou_user", "chat-a", "first turn")

        self._adapter_request(handler,
            "req-1",
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "command": "ls",
                "cwd": "/tmp/project",
                "reason": "need approval",
            },
        )

        self.assertEqual(bot.sent_messages[-1][0], "chat-a")
        self.assertNotEqual(bot.sent_messages[-1][0], "chat-b")

    def test_exact_server_request_replay_reuses_canonical_identity(self) -> None:
        handler, _ = self._make_handler()
        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "command": "pwd",
        }

        with patch.object(handler._server_request_surface_dispatcher, "_dispatch_once") as dispatch:
            self._adapter_request(handler,
                "request-replay",
                "item/commandExecution/requestApproval",
                params,
            )
            self._adapter_request(handler,
                "request-replay",
                "item/commandExecution/requestApproval",
                dict(params),
            )

        self.assertEqual(dispatch.call_count, 2)
        first = dispatch.call_args_list[0].args[0]
        replay = dispatch.call_args_list[1].args[0]
        self.assertIs(first, replay)
        canonical = dict(handler._server_request_registry.pending_items())[
            jsonrpc_id_key("request-replay")
        ]
        self.assertIs(canonical, first)
        self.assertTrue(handler._server_request_registry.active_matches(canonical))
        self.assertEqual(canonical.request_id, "request-replay")
        self.assertEqual(canonical.method, "item/commandExecution/requestApproval")
        self.assertEqual(canonical.params, params)

    def test_malformed_turn_completion_does_not_clear_newer_pending_request(self) -> None:
        handler, _ = self._make_handler()
        self._store_pending_request(handler, "req-new", {
            "rpc_request_id": "req-new",
            "method": "item/tool/requestUserInput",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-2",
                "questions": [],
            },
            "thread_id": "thread-1",
            "owner_thread_id": "thread-1",
            "turn_id": "turn-2",
            "message_id": "msg-input",
            "title": "Codex 用户输入",
            "status": "not_sent",
            "auto_resolution_backend_epoch": 1,
        })

        self._dispatch_adapter_notification(
            handler,
            "turn/completed",
            {"threadId": "thread-1", "turn": {}},
        )

        self.assertTrue(self._has_pending_request(handler, "req-new"))

    def test_unknown_thread_status_does_not_release_feishu_turn_owner(self) -> None:
        handler, _ = self._make_handler()
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="systemError",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=root)
        handler._direct_thread_targets.remember(root)
        self._store_pending_request(
            handler,
            "req-unknown-status",
            {
                "rpc_request_id": "req-unknown-status",
                "thread_id": "thread-1",
                "owner_thread_id": "thread-1",
                "turn_id": "turn-1",
                "status": "not_sent",
            },
        )
        holder = make_feishu_interaction_holder("ou_user", "chat-1", owner_pid=0)
        self.assertTrue(handler._interaction_lease_store.acquire("thread-1", holder).granted)

        notification = {"threadId": "thread-1", "status": {"type": "systemError"}}
        handler._runtime_call(
            handler._adapter_events.handle_server_request_notification,
            "thread/status/changed",
            notification,
        )
        handler._runtime_call(
            handler._feishu_root_operations.reconcile_terminal,
            "thread-1",
        )

        self.assertTrue(self._has_pending_request(handler, "req-unknown-status"))
        self.assertIsNotNone(handler._interaction_lease_store.load("thread-1"))

    def test_goal_clear_waits_for_an_unknown_submission_to_reconcile(self) -> None:

        handler, _ = self._make_handler()
        binding = ("ou_user", "chat-1")
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, root)
        older_generation = self._arm_accepted_feishu_continuation(
            handler,
            binding,
            "thread-1",
            reason="test_goal_clear_noop",
        )

        with self.assertRaises(FeishuRootOperationPoisoned):
            handler._runtime_call(
                handler._feishu_continuation.clear_goal,
                *binding,
                "thread-1",
            )
        self.assertIn(
            older_generation,
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).continuation_generations,
        )
        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )

    def test_goal_clear_ack_with_failed_status_read_becomes_local_unknown(self) -> None:

        handler, _ = self._make_handler()
        binding = ("ou_user", "chat-1")
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, root)
        handler._adapter.thread_goals["thread-1"] = ThreadGoalSummary(
            thread_id="thread-1",
            objective="will be cleared",
            status="active",
        )
        original_read_thread = handler._adapter.read_thread
        reads = 0

        def fail_settlement_read(*args, **kwargs):
            nonlocal reads
            reads += 1
            if reads >= 3:
                raise RuntimeError("fresh root authority unavailable")
            return original_read_thread(*args, **kwargs)

        handler._adapter.read_thread = fail_settlement_read

        with self.assertRaisesRegex(
            FeishuRootOperationRetentionError,
            "无法确认该 mutation 是否启动了 main turn",
        ):
            handler._runtime_call(
                handler._feishu_continuation.clear_goal,
                *binding,
                "thread-1",
            )

        self.assertNotIn("thread-1", handler._adapter.thread_goals)
        self.assertGreaterEqual(reads, 3)
        snapshot = self._feishu_root_snapshot(handler, "thread-1")
        self.assertEqual(snapshot.continuation_generations, ())
        self.assertTrue(snapshot.submission_outcome_unknown)

    def test_reentrant_goal_mutation_is_rejected_by_submission_single_flight(self) -> None:

        handler, _ = self._make_handler()
        binding = ("ou_user", "chat-1")
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, root)
        calls = 0

        def overlapping_goal_set(thread_id: str, **kwargs) -> ThreadGoalSummary:
            nonlocal calls
            calls += 1
            if calls == 1:
                later = handler._feishu_continuation.mutate_goal(
                    *binding,
                    thread_id,
                    objective="later continuation",
                    status="active",
                )
                self.assertEqual(later.status, "active")
                raise CodexRpcPreSendError("thread/goal/set", RuntimeError("rejected"))
            return ThreadGoalSummary(
                thread_id=thread_id,
                objective=str(kwargs.get("objective") or ""),
                status="active",
            )

        handler._adapter.set_thread_goal = overlapping_goal_set

        with self.assertRaises(FeishuRootOperationRetentionError):
            handler._runtime_call(
                handler._feishu_continuation.mutate_goal,
                *binding,
                "thread-1",
                objective="first continuation",
                status="active",
            )

        self.assertEqual(calls, 1)
        snapshot = self._feishu_root_snapshot(handler, "thread-1")
        self.assertEqual(snapshot.pending_admission_count, 0)
        self.assertEqual(snapshot.continuation_generations, ())
        lease = handler._interaction_lease_store.load("thread-1")
        self.assertIsNone(lease)

    def test_reentrant_pause_cannot_open_a_second_submission(self) -> None:

        handler, _ = self._make_handler()
        binding = ("ou_user", "chat-1")
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, root)
        calls = 0

        def overlapping_goal_set(thread_id: str, **kwargs) -> ThreadGoalSummary:
            nonlocal calls
            calls += 1
            if calls == 1:
                later = handler._feishu_continuation.mutate_goal(
                    *binding,
                    thread_id,
                    objective="later continuation",
                    status="active",
                )
                self.assertEqual(later.status, "active")
                return ThreadGoalSummary(
                    thread_id=thread_id,
                    objective="paused after older fence",
                    status="paused",
                )
            return ThreadGoalSummary(
                thread_id=thread_id,
                objective=str(kwargs.get("objective") or ""),
                status="active",
            )

        handler._adapter.set_thread_goal = overlapping_goal_set

        with self.assertRaises(FeishuRootOperationRetentionError):
            handler._runtime_call(
                handler._feishu_continuation.mutate_goal,
                *binding,
                "thread-1",
                status="paused",
            )

        self.assertEqual(calls, 1)
        generations = self._feishu_root_snapshot(
            handler,
            "thread-1",
        ).continuation_generations
        self.assertEqual(generations, ())
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))

    def test_stale_terminal_lifecycle_cannot_release_new_feishu_turn(self) -> None:
        """A terminal broadcast must not release a newer exact active turn."""

        handler, _ = self._make_handler()
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=3,
            source="cli",
            # The new same-root operation is currently live.  Its earlier
            # turn's delayed terminal broadcast must not unlock it.
            status="active",
        )
        handler._adapter.thread_snapshots[("thread-1", None)] = ThreadSnapshot(summary=root)
        _bind_authoritative_thread(handler, "ou_user", "chat-1", root)
        holder = handler._binding_runtime.feishu_interaction_holder(("ou_user", "chat-1"))
        self._activate_main_turn_lease(handler, "thread-1", holder, "turn-new")

        # Model a delayed terminal broadcast requesting reconciliation.  The
        # fresh direct read below still reports the new operation as active.
        handler._runtime_call(
            handler._feishu_root_operations.reconcile_terminal,
            "thread-1",
        )

        lease = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertTrue(lease.holder.same_holder(holder))
        self.assertEqual(lease.turn_id, "turn-new")

    def test_feishu_unknown_goal_status_keeps_local_submission_uncertainty(self) -> None:
        """A new upstream status is continuation-risk until explicitly reviewed."""

        handler, _ = self._make_handler()
        root = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, "ou_user", "chat-1", root)

        def return_future_goal(_thread_id: str, **_kwargs) -> ThreadGoalSummary:
            return ThreadGoalSummary(
                thread_id="thread-1",
                objective="continue only after a future-status review",
                status="futureStatus",
            )

        handler._adapter.set_thread_goal = return_future_goal

        result = handler._runtime_call(
            handler._feishu_continuation.mutate_goal,
            "ou_user",
            "chat-1",
            "thread-1",
            objective="start an active goal",
            status="active",
        )

        self.assertEqual(result.status, "futureStatus")
        self.assertEqual(
            len(
                self._feishu_root_snapshot(
                    handler,
                    "thread-1",
                ).continuation_generations
            ),
            1,
        )
        holder = handler._binding_runtime.feishu_interaction_holder(("ou_user", "chat-1"))
        lease = handler._interaction_lease_store.load("thread-1")
        self.assertIsNotNone(lease)
        assert lease is not None
        self.assertTrue(lease.holder.same_holder(holder))
        self.assertEqual(lease.turn_id, "")
        self.assertTrue(
            self._feishu_root_snapshot(
                handler,
                "thread-1",
            ).submission_outcome_unknown
        )

    def test_turn_completion_releases_shared_interaction_lease(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        handler, _ = self._make_handler(data_dir=data_dir)
        store = InteractionLeaseStore(data_dir)
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

        handler.handle_message("ou_user", "c1", "first turn")

        self.assertIsNotNone(store.load("thread-1"))

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self._on_turn_completed(handler, {"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self._wait_until(lambda: store.load("thread-1") is None)

    def test_terminal_reconcile_fallback_does_not_duplicate_terminal_result_delivery(self) -> None:
        handler, bot = self._make_handler()
        bot.message_contexts["msg-1"] = {
            "chat_type": "p2p",
            "sender_open_id": "ou_user",
        }

        handler.handle_message("ou_user", "c1", "hello", message_id="msg-1")
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": "thread-created", "turn": {"id": "turn-1"}},
        )
        bot.patch_results["plan-card-1"] = False
        self._dispatch_adapter_notification(
            handler,
            "item/completed",
            {
                "threadId": "thread-created",
                "turnId": "turn-1",
                "item": {
                    "id": "agent-1",
                    "type": "agentMessage",
                    "text": "123456789",
                    "phase": "final_answer",
                },
            },
        )
        target = _capture_reconcile(handler, "ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None
        self._on_turn_completed(handler, {"threadId": "thread-created", "turn": {"id": "turn-1", "status": "completed"}})
        self._wait_until(
            lambda: any(
                parent_id == "msg-1"
                and msg_type == "interactive"
                and json.loads(content)["header"]["title"]["content"] == "Codex"
                for parent_id, msg_type, content in bot.reply_refs
            )
        )
        reply_refs_before_reconcile = list(bot.reply_refs)
        handler._adapter.thread_snapshots[("thread-created", True)] = RuntimeError("snapshot down")
        _run_reconcile(handler, target)

        self.assertEqual(bot.replies, [])
        self.assertEqual(bot.reply_refs, reply_refs_before_reconcile)
        terminal_cards = [
            json.loads(content)
            for parent_id, msg_type, content in bot.reply_refs
            if parent_id == "msg-1" and msg_type == "interactive"
        ]
        self.assertEqual(len(terminal_cards), 2)
        card = next(card for card in terminal_cards if card["header"]["title"]["content"] == "Codex")
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("123456789", card["body"]["elements"][-1]["content"])

    def test_terminal_reconcile_sends_authoritative_result_card_from_snapshot_without_live_reply_delta(self) -> None:
        handler, bot = self._make_handler()
        handler._terminal_result_card_limit = 1000

        handler.handle_message("ou_user", "c1", "hello")
        target = _capture_reconcile(handler, "ou_user", "c1", thread_id="thread-created", turn_id="turn-1")
        assert target is not None
        handler._runtime_call(
            handler._terminal_execution.finalize_ingress,
            "ou_user",
            "c1",
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
                status="completed",
            ),
            turns=[
                {
                    "id": "turn-1",
                    "items": [{"type": "agentMessage", "text": "snapshot final answer"}],
                }
            ],
        )

        _run_reconcile(handler, target)

        self.assertEqual(bot.sent_messages[-1][1], "interactive")
        card = json.loads(bot.sent_messages[-1][2])
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn("snapshot final answer", card["body"]["elements"][-1]["content"])
        self.assertEqual(bot.deletes, [])
