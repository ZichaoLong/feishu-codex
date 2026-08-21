"""Handler composition regressions for exact Feishu queue admission."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace

from bot.adapters.base import ThreadSummary
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcTransportError,
)
from bot.feishu_execution_queue import FeishuBindingExecutionSnapshot
from bot.runtime_state import ThreadStateChanged
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
    make_web_interaction_holder,
)
from tests.focus_runtime.codex_handler_fakes import _bind_authoritative_thread
from tests.focus_runtime.codex_handler_test_harness import CodexHandlerHarness
from tests.focus_runtime.feishu_owner_test_support import prepare_terminal_feishu_fifo


class FeishuExecutionQueueHandlerIntegrationTests(CodexHandlerHarness):
    _prepare_terminal_feishu_fifo = prepare_terminal_feishu_fifo

    def test_exact_local_turn_queues_before_feishu_mirror_then_drains_once(
        self,
    ) -> None:
        for kind in ("web", "fcodex"):
            with self.subTest(kind=kind):
                handler, _bot = self._make_handler()
                binding = ("ou_user", "chat-a")
                root_thread_id = "root-a"
                _bind_authoritative_thread(
                    handler,
                    *binding,
                    ThreadSummary(
                        thread_id=root_thread_id,
                        cwd="/tmp/project",
                        name="demo",
                        preview="",
                        created_at=0,
                        updated_at=0,
                        source="appServer",
                        status="active",
                    ),
                )
                holder = (
                    make_web_interaction_holder(
                        "document-1",
                        owner_pid=os.getpid(),
                    )
                    if kind == "web"
                    else make_fcodex_interaction_holder(
                        "fcodex:participant-1",
                        owner_pid=os.getpid(),
                    )
                )
                self._activate_main_turn_lease(
                    handler,
                    root_thread_id,
                    holder,
                    turn_id="turn-local",
                )
                handler.handle_message(
                    binding[0],
                    binding[1],
                    "arrived before local turn mirror",
                    message_id=f"preprojection-{kind}",
                )

                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertEqual(
                    handler._runtime_call(
                        handler._feishu_execution_queue.snapshot,
                        binding,
                    ).pending_message_ids,
                    (f"preprojection-{kind}",),
                )

                self._dispatch_adapter_notification(
                    handler,
                    "turn/started",
                    {
                        "threadId": root_thread_id,
                        "turn": {"id": "turn-local"},
                    },
                )
                self._on_turn_completed(
                    handler,
                    {
                        "threadId": root_thread_id,
                        "turn": {"id": "turn-local", "status": "completed"},
                    },
                )

                self.assertEqual(len(handler._adapter.start_turn_calls), 1)
                self.assertEqual(
                    handler._adapter.start_turn_calls[0]["text"],
                    "arrived before local turn mirror",
                )
                self.assertFalse(
                    handler._runtime_call(
                        handler._feishu_execution_queue.snapshot,
                        binding,
                    ).has_pending_or_draining
                )
                current = handler._runtime_call(
                    handler._binding_runtime.resolve_session,
                    *binding,
                )
                self.assertIsNotNone(current)
                self.assertEqual(current.execution.current_turn_id, "")
                self.assertTrue(current.execution.awaiting_local_turn_started)

                self._dispatch_adapter_notification(
                    handler,
                    "turn/started",
                    {
                        "threadId": root_thread_id,
                        "turn": {"id": "turn-feishu-real"},
                    },
                )
                current = handler._runtime_call(
                    handler._binding_runtime.resolve_session,
                    *binding,
                )
                self.assertIsNotNone(current)
                self.assertEqual(
                    current.execution.current_turn_id,
                    "turn-feishu-real",
                )

    def test_known_active_turn_queues_and_binds_the_next_lifecycle_identity(
        self,
    ) -> None:
        for kind in ("web", "fcodex", "no_writer"):
            with self.subTest(kind=kind):
                handler, _bot = self._make_handler()
                binding = ("ou_user", "chat-a")
                root_thread_id = "root-a"
                _bind_authoritative_thread(
                    handler,
                    *binding,
                    ThreadSummary(
                        thread_id=root_thread_id,
                        cwd="/tmp/project",
                        name="demo",
                        preview="",
                        created_at=0,
                        updated_at=0,
                        source="appServer",
                        status="active",
                    ),
                )
                if kind != "no_writer":
                    holder = (
                        make_web_interaction_holder(
                            "document-1",
                            owner_pid=os.getpid(),
                        )
                        if kind == "web"
                        else make_fcodex_interaction_holder(
                            "fcodex:participant-1",
                            owner_pid=os.getpid(),
                        )
                    )
                    self._activate_main_turn_lease(
                        handler,
                        root_thread_id,
                        holder,
                        turn_id="turn-foreign",
                    )
                self._dispatch_adapter_notification(
                    handler,
                    "turn/started",
                    {
                        "threadId": root_thread_id,
                        "turn": {"id": "turn-foreign"},
                    },
                )

                handler.handle_message(
                    binding[0],
                    binding[1],
                    "queued after known active turn",
                    message_id=f"queued-{kind}",
                )

                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertEqual(
                    handler._runtime_call(
                        handler._feishu_execution_queue.snapshot,
                        binding,
                    ).pending_message_ids,
                    (f"queued-{kind}",),
                )

                self._on_turn_completed(
                    handler,
                    {
                        "threadId": root_thread_id,
                        "turn": {
                            "id": "turn-foreign",
                            "status": "completed",
                        },
                    },
                )

                self.assertEqual(
                    [call["text"] for call in handler._adapter.start_turn_calls],
                    ["queued after known active turn"],
                )
                session = handler._runtime_call(
                    handler._binding_runtime.resolve_session,
                    *binding,
                )
                self.assertIsNotNone(session)
                self.assertEqual(session.execution.current_turn_id, "")
                self.assertTrue(session.execution.awaiting_local_turn_started)
                lease = handler._interaction_lease_store.load(root_thread_id)
                self.assertIsNotNone(lease)
                self.assertEqual(lease and lease.turn_id, "")

                self._dispatch_adapter_notification(
                    handler,
                    "turn/started",
                    {
                        "threadId": root_thread_id,
                        "turn": {"id": "turn-feishu-actual"},
                    },
                )

                session = handler._runtime_call(
                    handler._binding_runtime.resolve_session,
                    *binding,
                )
                self.assertIsNotNone(session)
                self.assertEqual(
                    session.execution.current_turn_id,
                    "turn-feishu-actual",
                )
                lease = handler._interaction_lease_store.load(root_thread_id)
                self.assertIsNotNone(lease)
                self.assertEqual(lease and lease.turn_id, "turn-feishu-actual")

    def test_existing_fifo_continuity_keeps_later_prompt_behind_started_head(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        binding = ("ou_user", "chat-a")
        root_thread_id = "root-a"
        _bind_authoritative_thread(
            handler,
            *binding,
            ThreadSummary(
                thread_id=root_thread_id,
                cwd="/tmp/project",
                name="demo",
                preview="",
                created_at=0,
                updated_at=0,
                source="appServer",
                status="active",
            ),
        )
        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": root_thread_id, "turn": {"id": "turn-a"}},
        )
        handler.handle_message(
            *binding,
            "first queued prompt",
            message_id="queued-first",
        )
        handler.handle_message(
            *binding,
            "second queued prompt",
            message_id="queued-second",
        )

        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("queued-first", "queued-second"),
        )

        self._on_turn_completed(
            handler,
            {
                "threadId": root_thread_id,
                "turn": {"id": "turn-a", "status": "completed"},
            },
        )

        self.assertEqual(
            [call["text"] for call in handler._adapter.start_turn_calls],
            ["first queued prompt"],
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("queued-second",),
        )

        self._dispatch_adapter_notification(
            handler,
            "turn/started",
            {"threadId": root_thread_id, "turn": {"id": "turn-b"}},
        )
        self._on_turn_completed(
            handler,
            {
                "threadId": root_thread_id,
                "turn": {"id": "turn-b", "status": "completed"},
            },
        )

        self.assertEqual(
            [call["text"] for call in handler._adapter.start_turn_calls],
            ["first queued prompt", "second queued prompt"],
        )
        self.assertFalse(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).has_pending_or_draining
        )

    @staticmethod
    def _replace_runtime_root(handler, binding, root_thread_id: str) -> None:
        with handler._lock:
            state = handler._binding_runtime._get_or_create_runtime_state_locked(
                binding
            )
            state["current_thread_id"] = root_thread_id
            state["current_thread_title"] = "replacement"

    @staticmethod
    def _invalidate_queue_a_to_b_to_a(handler, binding, root_thread_id: str) -> None:
        queue = handler._feishu_execution_queue
        queue.invalidate_binding(binding)
        queue.enqueue_prompt(
            FeishuBindingExecutionSnapshot(
                binding=binding,
                root_thread_id="root-b",
                active=True,
                attached=True,
                has_inflight_execution=True,
                current_turn_id="turn-a",
            ),
            sender_id=binding[0],
            chat_id=binding[1],
            message_id="replacement-b",
            text="must be invalidated",
        )
        queue.invalidate_binding(binding)
        queue.enqueue_prompt(
            FeishuBindingExecutionSnapshot(
                binding=binding,
                root_thread_id=root_thread_id,
                active=True,
                attached=True,
                has_inflight_execution=True,
                current_turn_id="turn-b",
            ),
            sender_id=binding[0],
            chat_id=binding[1],
            message_id="replacement-a",
            text="future generation",
        )

    def test_recall_terminal_last_head_leaves_no_writer_authority(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="recalled-last-head",
        )

        handler._runtime_call(
            handler._feishu_surface.handle_message_recalled_impl,
            binding[1],
            "recalled-last-head",
        )

        self.assertFalse(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).has_pending_or_draining
        )
        self.assertIsNone(handler._interaction_lease_store.load(root_thread_id))

    def test_prompt_and_compact_stop_when_root_changes_before_owner_admit(
        self,
    ) -> None:
        for kind in ("prompt", "compact"):
            with self.subTest(kind=kind):
                handler, _bot = self._make_handler()
                _root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
                    handler,
                    kind=kind,
                    message_id=f"queued-{kind}",
                )
                root_admissions: list[str] = []
                original_admit = handler._feishu_root_operations.admit

                def observe_admit(*args, **kwargs):
                    root_admissions.append(str(args[1]))
                    return original_admit(*args, **kwargs)

                handler._feishu_root_operations.admit = observe_admit
                changed = False

                def replace_root_once() -> str:
                    nonlocal changed
                    if not changed:
                        changed = True
                        self._replace_runtime_root(handler, binding, "root-b")
                    return ""

                if kind == "prompt":
                    handler._thread_access_policy.all_mode_thread_exclusivity_violation = (
                        lambda *_args, **_kwargs: replace_root_once()
                    )
                else:
                    handler._thread_access_policy.prompt_write_denial_text = (
                        lambda *_args, **_kwargs: replace_root_once()
                    )

                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                self.assertTrue(changed)
                self.assertEqual(root_admissions, [])
                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertEqual(handler._adapter.compact_thread_calls, [])
                queue_snapshot = handler._runtime_call(
                    handler._feishu_execution_queue.snapshot,
                    binding,
                )
                self.assertFalse(queue_snapshot.has_pending_or_draining)

    def test_handler_drains_two_thousand_preparation_drops_then_one_survivor(
        self,
    ) -> None:
        handler, bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="drop-0",
            text="drop",
        )
        for index in range(1, 2_000):
            self._enqueue_feishu_queue_item(
                handler,
                kind="prompt",
                binding=binding,
                root_thread_id=root_thread_id,
                message_id=f"drop-{index}",
                text="drop",
            )
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="survivor",
            text="run exactly once",
            input_items=({"type": "text", "text": "run exactly once"},),
        )
        preparations: list[str] = []

        def prepare_queued_prompt_text(*, message_id: str, text: str, **_kwargs):
            preparations.append(message_id)
            if message_id == "survivor":
                return text
            return None

        bot.prepare_queued_prompt_text = prepare_queued_prompt_text

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(len(preparations), 2_001)
        self.assertEqual(preparations[0], "drop-0")
        self.assertEqual(preparations[-1], "survivor")
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(
            handler._adapter.start_turn_calls[0]["text"],
            "run exactly once",
        )
        queue_snapshot = handler._runtime_call(
            handler._feishu_execution_queue.snapshot,
            binding,
        )
        self.assertFalse(queue_snapshot.has_pending_or_draining)

    def test_pre_owner_failure_presentation_cannot_block_fifo_successor(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="failed-head",
            text="first",
        )
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="survivor",
            text="second",
            input_items=({"type": "text", "text": "second"},),
        )
        original_resolve = handler._prompt_turn_entry._resolve_session
        resolve_calls = 0

        def fail_first_resolution(*args, **kwargs):
            nonlocal resolve_calls
            resolve_calls += 1
            if resolve_calls == 1:
                raise RuntimeError("pre-owner lookup failed")
            return original_resolve(*args, **kwargs)

        def fail_presentation(**_kwargs):
            raise RuntimeError("presentation unavailable")

        handler._prompt_turn_entry._resolve_session = fail_first_resolution
        handler._prompt_turn_entry._failure_presentation._ports = replace(
            handler._prompt_turn_entry._failure_presentation._ports,
            render_start_failure=fail_presentation,
        )

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertGreaterEqual(resolve_calls, 2)
        self.assertEqual(len(handler._adapter.start_turn_calls), 1)
        self.assertEqual(handler._adapter.start_turn_calls[0]["text"], "second")
        self.assertFalse(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).has_pending_or_draining
        )

    def test_prompt_and_compact_mutation_guard_rejects_card_callback_aba(
        self,
    ) -> None:
        for kind in ("prompt", "compact"):
            with self.subTest(kind=kind):
                handler, _bot = self._make_handler()
                root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
                    handler,
                    kind=kind,
                    message_id=f"queued-{kind}-aba",
                )
                callback_count = 0

                def invalidate_a_to_b_to_a() -> None:
                    nonlocal callback_count
                    callback_count += 1
                    queue = handler._feishu_execution_queue
                    queue.invalidate_binding(binding)
                    queue.enqueue_prompt(
                        FeishuBindingExecutionSnapshot(
                            binding=binding,
                            root_thread_id="root-b",
                            active=True,
                            attached=True,
                            has_inflight_execution=True,
                            current_turn_id="turn-b",
                        ),
                        sender_id=binding[0],
                        chat_id=binding[1],
                        message_id="replacement-b",
                        text="must be invalidated",
                    )
                    queue.invalidate_binding(binding)
                    queue.enqueue_prompt(
                        FeishuBindingExecutionSnapshot(
                            binding=binding,
                            root_thread_id=root_thread_id,
                            active=True,
                            attached=True,
                            has_inflight_execution=True,
                            current_turn_id="turn-a-replacement",
                        ),
                        sender_id=binding[0],
                        chat_id=binding[1],
                        message_id="replacement-a",
                        text="future generation",
                    )

                if kind == "prompt":
                    original_open = (
                        handler._prompt_turn_entry._open_initial_execution_page
                    )

                    def open_prompt_page(*args, **kwargs):
                        result = original_open(*args, **kwargs)
                        invalidate_a_to_b_to_a()
                        return result

                    handler._prompt_turn_entry._open_initial_execution_page = (
                        open_prompt_page
                    )
                else:
                    compact_ports = handler._feishu_compact_execution._ports
                    original_open = (
                        compact_ports.presentation.open_initial_execution_page
                    )

                    def open_compact_page(*args, **kwargs):
                        result = original_open(*args, **kwargs)
                        invalidate_a_to_b_to_a()
                        return result

                    handler._feishu_compact_execution._ports = replace(
                        compact_ports,
                        presentation=replace(
                            compact_ports.presentation,
                            open_initial_execution_page=open_compact_page,
                        ),
                    )

                handler._runtime_call(
                    handler._feishu_execution_queue_service.drain,
                    binding,
                )

                self.assertEqual(callback_count, 1)
                self.assertEqual(handler._adapter.start_turn_calls, [])
                self.assertEqual(handler._adapter.compact_thread_calls, [])
                self.assertEqual(
                    handler._runtime_call(
                        handler._feishu_execution_queue.snapshot,
                        binding,
                    ).pending_message_ids,
                    ("replacement-a",),
                )
                self.assertEqual(
                    handler._runtime_call(
                        handler._feishu_root_operations.snapshot,
                        root_thread_id,
                    ).pending_admission_count,
                    0,
                )

    def test_compact_transport_unknown_settlement_failure_is_not_replayed(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="compact",
            message_id="unknown-head",
        )
        self._enqueue_feishu_queue_item(
            handler,
            kind="compact",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="later-head",
        )
        attempts: list[str] = []

        def disconnect_after_send(thread_id: str) -> None:
            attempts.append(thread_id)
            raise CodexRpcTransportError(
                "thread/compact/start",
                {"message": "Codex websocket disconnected"},
            )

        def fail_unknown_settlement(*_args, **_kwargs) -> None:
            raise RuntimeError("process-local unknown mark failed")

        handler._adapter.compact_thread = disconnect_after_send
        handler._mark_compact_start_outcome_unknown = fail_unknown_settlement

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)
        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(attempts, [root_thread_id])
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("later-head",),
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root_thread_id,
            ).pending_admission_count,
            1,
        )

    def test_prompt_known_failure_settlement_blocker_stops_later_head(self) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="blocked-head",
            text="first",
        )
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="later-head",
            text="second",
            input_items=({"type": "text", "text": "second"},),
        )
        attempts: list[str] = []

        def fail_before_send(**kwargs):
            attempts.append(str(kwargs.get("thread_id") or ""))
            raise CodexRpcPreSendError(
                "turn/start",
                RuntimeError("request was not sent"),
            )

        def fail_owner_settlement(_token, *, reason: str) -> None:
            del reason
            raise RuntimeError("submission owner settlement failed")

        handler._adapter.start_turn = fail_before_send
        settlement = handler._prompt_turn_entry._operation_settlement
        settlement._ports = replace(
            settlement._ports,
            settle_known_failure=fail_owner_settlement,
        )

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(attempts, [root_thread_id])
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("later-head",),
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root_thread_id,
            ).pending_admission_count,
            1,
        )

    def test_tokenless_admission_exception_blocks_fifo_after_submission_claim(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="poisoned-admission",
            text="first",
        )
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="later-head",
            text="second",
            input_items=({"type": "text", "text": "second"},),
        )
        original_admit = handler._prompt_turn_entry._admit_root_operation

        def claim_then_lose_token(*args, **kwargs):
            original_admit(*args, **kwargs)
            raise RuntimeError("admission response lost after submission claim")

        def fail_presentation(*_args, **_kwargs):
            raise RuntimeError("presentation unavailable")

        handler._prompt_turn_entry._admit_root_operation = claim_then_lose_token
        handler._prompt_turn_entry._failure_presentation._ports = replace(
            handler._prompt_turn_entry._failure_presentation._ports,
            reply_text=fail_presentation,
        )

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(handler._adapter.start_turn_calls, [])
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("later-head",),
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root_thread_id,
            ).pending_admission_count,
            1,
        )

    def test_initial_attach_resume_guard_rejects_queue_aba_at_adapter_boundary(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        binding = ("ou_user", "chat-a")
        root_thread_id = "thread-1"
        summary = ThreadSummary(
            thread_id=root_thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=1,
            updated_at=2,
            source="cli",
            status="idle",
        )
        _bind_authoritative_thread(handler, *binding, summary)

        def detach_binding() -> None:
            with handler._lock:
                state = handler._binding_runtime._get_or_create_runtime_state_locked(
                    binding
                )
                handler._binding_runtime.unsubscribe_thread_locked(
                    binding,
                    root_thread_id,
                )
                handler._binding_runtime._apply_persisted_runtime_state_message_locked(
                    binding,
                    state,
                    ThreadStateChanged(feishu_runtime_state="detached"),
                )

        handler._runtime_call(detach_binding)
        self._enqueue_feishu_queue_item(
            handler,
            kind="prompt",
            binding=binding,
            root_thread_id=root_thread_id,
            message_id="claimed-a",
            text="must not resume",
        )
        exact_snapshot = FeishuBindingExecutionSnapshot(
            binding=binding,
            root_thread_id=root_thread_id,
            active=True,
            attached=True,
            has_inflight_execution=False,
            current_turn_id="",
        )
        effect = handler._runtime_call(
            handler._feishu_execution_queue.begin_terminal_drain,
            binding,
            exact_snapshot,
        )
        self.assertIsNotNone(effect)
        callback_count = 0
        original_settings_invalidation = (
            handler._thread_runtime_authority._invalidate_resume_setting_intent
        )

        def invalidate_during_resume_preparation(
            thread_id: str,
            *,
            model: str | None,
            kwargs: dict,
        ):
            nonlocal callback_count
            original_settings_invalidation(
                thread_id,
                model=model,
                kwargs=kwargs,
            )
            callback_count += 1
            self._invalidate_queue_a_to_b_to_a(
                handler,
                binding,
                root_thread_id,
            )

        handler._thread_runtime_authority._invalidate_resume_setting_intent = (
            invalidate_during_resume_preparation
        )

        def run_prompt_and_settle():
            result = handler._prompt_turn_entry.start_prompt_turn_result(
                *binding,
                "must not resume",
                message_id="claimed-a",
                surface_failures=False,
                expected_binding=binding,
                expected_root_thread_id=root_thread_id,
                exact_admission_guard=lambda: (
                    handler._feishu_execution_queue.receipt_may_execute(
                        effect.receipt,
                        exact_snapshot,
                    )
                ),
                exact_mutation_guard=lambda: (
                    handler._feishu_execution_queue.claimed_receipt_may_mutate(
                        effect.receipt,
                        exact_snapshot,
                    )
                ),
            )
            handler._feishu_execution_queue.complete_drain(
                effect.receipt,
                outcome=result.disposition,
            )
            return result

        result = handler._runtime_call(run_prompt_and_settle)

        self.assertEqual(callback_count, 1)
        self.assertFalse(result.started)
        self.assertEqual(result.disposition, "known_no_effect_settled")
        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("replacement-a",),
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root_thread_id,
            ).pending_admission_count,
            0,
        )

    def test_fallback_resume_guard_rejects_queue_aba_at_adapter_boundary(
        self,
    ) -> None:
        handler, _bot = self._make_handler()
        root_thread_id, binding, _holder = self._prepare_terminal_feishu_fifo(
            handler,
            kind="prompt",
            message_id="fallback-a",
            text="must not resume",
        )
        start_attempts: list[str] = []

        callback_count = 0

        def reject_unloaded_turn(**kwargs):
            nonlocal callback_count
            thread_id = str(kwargs.get("thread_id") or "")
            start_attempts.append(thread_id)
            callback_count += 1
            self._invalidate_queue_a_to_b_to_a(
                handler,
                binding,
                root_thread_id,
            )
            raise CodexRpcError(
                "turn/start",
                {"message": f"thread not found: {thread_id}"},
            )

        handler._adapter.start_turn = reject_unloaded_turn

        handler._runtime_call(handler._feishu_execution_queue_service.drain, binding)

        self.assertEqual(callback_count, 1)
        self.assertEqual(start_attempts, [root_thread_id])
        self.assertEqual(handler._adapter.resume_thread_calls, [])
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_execution_queue.snapshot,
                binding,
            ).pending_message_ids,
            ("replacement-a",),
        )
        self.assertEqual(
            handler._runtime_call(
                handler._feishu_root_operations.snapshot,
                root_thread_id,
            ).pending_admission_count,
            0,
        )


if __name__ == "__main__":
    unittest.main()
