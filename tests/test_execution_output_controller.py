import pathlib
import tempfile
import threading
import time
import unittest
import json
from unittest.mock import patch

from bot.card_text_projection import TERMINAL_RESULT_CARD_MARKER, terminal_result_checksum
from bot.cards import build_terminal_result_card_message_content
from bot.binding_runtime_contract import BindingRuntimeHandle
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenStatus,
)
from bot.execution_pages import (
    ExecutionPageLedger,
    ExecutionPageStatus,
    ExecutionTranscriptCursor,
)
from bot.execution_output_runtime import ExecutionOutputRuntimeTransitions
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundOperation,
    FeishuOutboundResult,
)
from bot.runtime_card_publisher import (
    RuntimeCardPublisher,
    execution_card_model_fits_page,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state


class _TestBindingRuntime:
    def __init__(self, states) -> None:
        self.states = states
        self._next_incarnation = 0
        self._current = {}

    def _current_handle(self, binding):
        state = self.states.get(binding)
        if state is None:
            return None
        current = self._current.get(binding)
        if current is not None and current[0] is state:
            return current[1]
        self._next_incarnation += 1
        handle = BindingRuntimeHandle(
            _issuer_nonce=1,
            binding=binding,
            incarnation=self._next_incarnation,
        )
        self._current[binding] = (state, handle)
        return handle

    def resolve_session(self, binding):
        state = self.states[binding]
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def session_snapshot_locked(self, handle):
        current = self._current_handle(handle.binding)
        if current is not handle:
            raise RuntimeError("stale test binding handle")
        return project_binding_session_snapshot(
            self.states[handle.binding],
            handle=handle,
        )

    def resident_session_snapshot_locked(self, binding):
        state = self.states.get(binding)
        if state is None:
            return None
        handle = self._current_handle(binding)
        assert handle is not None
        return project_binding_session_snapshot(state, handle=handle)

    def resident_runtime_state_locked(self, binding):
        return self.states.get(binding)


class _FakeBot:
    def __init__(self) -> None:
        self.reply_refs: list[tuple[str, str, str, bool]] = []
        self.sent_messages: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str]] = []
        self.patch_results: dict[str, bool] = {}
        self.patch_result_sequences: dict[str, list[FeishuOutboundResult]] = {}
        self.reply_result_sequences: list[FeishuOutboundResult] = []
        self.reply_result: FeishuOutboundResult | None = None
        self.send_result: FeishuOutboundResult | None = None
        self.attempt_ids: list[str] = []

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        self.reply_refs.append((parent_id, msg_type, content, reply_in_thread))
        self.attempt_ids.append(attempt_id)
        if self.reply_result_sequences:
            return self.reply_result_sequences.pop(0)
        return self.reply_result or _confirmed(
            FeishuOutboundOperation.REPLY_MESSAGE,
            chat_id=chat_id,
            message_id=f"plan-card-{len(self.reply_refs)}",
        )

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        self.sent_messages.append((chat_id, msg_type, content))
        self.attempt_ids.append(attempt_id)
        return self.send_result or _confirmed(
            FeishuOutboundOperation.CREATE_MESSAGE,
            chat_id=chat_id,
            message_id="plan-card-2",
        )

    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        self.patches.append((message_id, content))
        self.attempt_ids.append(attempt_id)
        sequence = self.patch_result_sequences.get(message_id)
        if sequence:
            return sequence.pop(0)
        if self.patch_results.get(message_id, True):
            return _confirmed(
                FeishuOutboundOperation.PATCH_MESSAGE,
                chat_id=chat_id,
                message_id=message_id,
            )
        return _rejected(
            FeishuOutboundOperation.PATCH_MESSAGE,
            chat_id=chat_id,
        )


def _confirmed(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "c1",
    message_id: str = "message-1",
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.CONFIRMED,
        destination_liveness=FeishuDestinationLiveness.REACHABLE,
        chat_id=chat_id,
        attempt_id="attempt-confirmed",
        message_id=message_id,
    )


def _rejected(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "c1",
    retry_after_seconds: float = 0.0,
    content_rejected: bool = False,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.REJECTED,
        destination_liveness=FeishuDestinationLiveness.UNKNOWN,
        chat_id=chat_id,
        attempt_id="attempt-rejected",
        error_code="230099" if content_rejected else "230013",
        retry_after_seconds=retry_after_seconds,
        content_rejected=content_rejected,
    )


def _unknown(
    operation: FeishuOutboundOperation,
    *,
    chat_id: str = "c1",
    retry_after_seconds: float = 0.0,
) -> FeishuOutboundResult:
    return FeishuOutboundResult(
        operation=operation,
        effect=FeishuOutboundEffect.UNKNOWN,
        destination_liveness=FeishuDestinationLiveness.UNKNOWN,
        chat_id=chat_id,
        attempt_id="attempt-unknown",
        error_message="transport timeout",
        retry_after_seconds=retry_after_seconds,
    )


class ExecutionOutputControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(
        self,
        state,
        *,
        terminal_result_card_limit: int = 1000,
        resident_states: dict[tuple[str, str], object] | None = None,
        binding_runtime=None,
        stream_patch_interval_ms: int = 1,
        execution_page_payload_limit_bytes: int = 26_000,
        execution_page_component_limit: int = 80,
    ):
        bot = _FakeBot()
        replies: list[tuple[str, str, str, bool]] = []
        dispatched: list[dict[str, object]] = []
        recorded_terminal_results: list[dict[str, str]] = []
        lock = threading.RLock()
        turn_execution = TurnExecutionCoordinator()
        binding = ("ou_user", "c1")
        if resident_states is None:
            resident_states = {binding: state}
        binding_runtime = binding_runtime or _TestBindingRuntime(resident_states)

        controller = ExecutionOutputController(
            runtime=ExecutionOutputRuntimeTransitions(
                lock=lock,
                binding_runtime=binding_runtime,
                turn_execution=turn_execution,
            ),
            runtime_submit=lambda target, *args, **kwargs: target(*args, **kwargs),
            resolve_session=lambda sender_id, chat_id: binding_runtime.resolve_session(
                binding
            ),
            card_publisher_factory=lambda: RuntimeCardPublisher(bot),
            dispatch_execution_card_patch=lambda chat_id, message_id, model: dispatched.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "running": model.running,
                    "elapsed": model.elapsed,
                    "cancelled": model.cancelled,
                    "model": model,
                    "log_text": model.log_text,
                    "reply_text": "".join(
                        segment.text
                        for segment in model.reply_segments
                        if segment.kind == "assistant"
                    ),
                }
            ),
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or True
            ),
            reply_text_get_id=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread))
                or ("text-reply-1" if message_id else "text-message-1")
            ),
            record_terminal_result_card=lambda *, message_id, execution_message_id, final_reply_text, terminal_result_id="", thread_id="", checksum="": recorded_terminal_results.append(
                {
                    "message_id": message_id,
                    "execution_message_id": execution_message_id,
                    "final_reply_text": final_reply_text,
                    "terminal_result_id": terminal_result_id,
                    "thread_id": thread_id,
                    "checksum": checksum,
                }
            ),
            terminal_result_card_limit=lambda: terminal_result_card_limit,
            stream_patch_interval_ms=lambda: stream_patch_interval_ms,
            execution_page_payload_limit_bytes=(
                execution_page_payload_limit_bytes
            ),
            execution_page_component_limit=execution_page_component_limit,
        )
        return controller, bot, replies, dispatched, recorded_terminal_results

    def test_initial_page_records_stable_attempt_before_confirmed_reply(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        captured = controller._resolve_session("ou_user", "c1")

        result = controller.open_initial_execution_page(
            captured,
            "prompt-1",
            reply_in_thread=True,
        )

        self.assertEqual(result.status, InitialExecutionPageOpenStatus.ACTIVE)
        self.assertEqual(result.message_id, "plan-card-1")
        page = state["execution_pages"].active_page
        assert page is not None
        self.assertEqual(page.status, ExecutionPageStatus.ACTIVE)
        self.assertTrue(page.outbound_attempt_id)
        self.assertEqual(bot.attempt_ids, [page.outbound_attempt_id])
        self.assertEqual(len(bot.reply_refs), 1)
        self.assertEqual(bot.sent_messages, [])

    def test_reserved_initial_page_uses_same_attempt_for_patch_and_commit(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        captured = controller._resolve_session("ou_user", "c1")

        result = controller.open_initial_execution_page(
            captured,
            "prompt-1",
            reserved_message_id="reserved-card",
        )

        self.assertEqual(result.status, InitialExecutionPageOpenStatus.ACTIVE)
        self.assertEqual(result.message_id, "reserved-card")
        page = state["execution_pages"].active_page
        assert page is not None
        self.assertEqual(page.message_id, "reserved-card")
        self.assertEqual(bot.attempt_ids, [page.outbound_attempt_id])
        self.assertEqual(bot.patches[-1][0], "reserved-card")
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(bot.sent_messages, [])

    def test_initial_page_unknown_does_not_fallback_or_drop_page_fence(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        captured = controller._resolve_session("ou_user", "c1")

        result = controller.open_initial_execution_page(captured, "prompt-1")

        self.assertEqual(
            result.status,
            InitialExecutionPageOpenStatus.SEND_UNKNOWN,
        )
        page = state["execution_pages"].current_page
        assert page is not None
        self.assertEqual(page.status, ExecutionPageStatus.SEND_UNKNOWN)
        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(len(bot.reply_refs), 1)

    def test_reserved_initial_page_unknown_retains_known_message_identity(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        bot.patch_result_sequences["reserved-card"] = [
            _unknown(FeishuOutboundOperation.PATCH_MESSAGE)
        ]
        captured = controller._resolve_session("ou_user", "c1")

        result = controller.open_initial_execution_page(
            captured,
            "prompt-1",
            reserved_message_id="reserved-card",
        )

        self.assertEqual(
            result.status,
            InitialExecutionPageOpenStatus.SEND_UNKNOWN,
        )
        page = state["execution_pages"].current_page
        assert page is not None
        self.assertEqual(page.message_id, "reserved-card")
        self.assertEqual(bot.patches[-1][0], "reserved-card")
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(bot.sent_messages, [])

    def test_initial_unknown_confirmation_reuses_uuid_and_activates_page(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        opened = controller.open_initial_execution_page(
            controller._resolve_session("ou_user", "c1"),
            "prompt-1",
        )
        assert opened.session is not None
        pending = opened.session.execution.pages.pending_page
        assert pending is not None
        stable_attempt_id = pending.outbound_attempt_id
        bot.reply_result = _confirmed(
            FeishuOutboundOperation.REPLY_MESSAGE,
            message_id="reconciled-card",
        )

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        self.assertEqual(len(bot.reply_refs), 2)
        self.assertEqual(bot.attempt_ids, [stable_attempt_id, stable_attempt_id])
        self.assertEqual(
            state["execution_pages"].current_message_id,
            "reconciled-card",
        )
        self.assertIsNone(state["patch_timer_registration"])

    def test_initial_unknown_rejection_discards_only_the_pending_page(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        opened = controller.open_initial_execution_page(
            controller._resolve_session("ou_user", "c1"),
            "prompt-1",
        )
        assert opened.session is not None
        stable_attempt_id = (
            opened.session.execution.pages.pending_page.outbound_attempt_id
        )
        bot.reply_result = _rejected(FeishuOutboundOperation.REPLY_MESSAGE)

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        self.assertEqual(state["execution_pages"].pages, ())
        self.assertEqual(bot.attempt_ids, [stable_attempt_id, stable_attempt_id])
        self.assertIsNone(state["patch_timer_registration"])

    def test_second_initial_unknown_does_not_install_a_reconciliation_loop(
        self,
    ) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_prompt_message_id"] = "prompt-1"
        state["started_at"] = 1.0
        controller, bot, _, _, _ = self._make_controller(state)
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)
        opened = controller.open_initial_execution_page(
            controller._resolve_session("ou_user", "c1"),
            "prompt-1",
        )
        assert opened.session is not None
        pending = opened.session.execution.pages.pending_page
        assert pending is not None

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        self.assertEqual(len(bot.reply_refs), 2)
        self.assertEqual(
            bot.attempt_ids,
            [pending.outbound_attempt_id, pending.outbound_attempt_id],
        )
        self.assertTrue(state["execution_pages"].send_outcome_unknown)
        self.assertIsNone(state["patch_timer_registration"])

    def test_flush_execution_card_patch_failure_falls_back_once(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("123456789")
        bot.patch_results["card-1"] = False

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "123456789", "msg-1", True)])
        self.assertTrue(state["followup_sent"])

    def test_unknown_execution_card_patch_does_not_send_followup(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("terminal reply")
        bot.patch_result_sequences["card-1"] = [
            _unknown(FeishuOutboundOperation.PATCH_MESSAGE)
        ]

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [])
        self.assertFalse(state["followup_sent"])

    def test_flush_execution_card_minimal_fallback_still_sends_terminal_text_once(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
        state["current_prompt_message_id"] = "msg-1"
        state["current_prompt_reply_in_thread"] = True
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("启动失败：后端不可用")
        bot.patch_result_sequences["card-1"] = [
            _rejected(
                FeishuOutboundOperation.PATCH_MESSAGE,
                content_rejected=True,
            ),
            _confirmed(
                FeishuOutboundOperation.PATCH_MESSAGE,
                message_id="card-1",
            ),
        ]

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "启动失败：后端不可用", "msg-1", True)])
        self.assertTrue(state["followup_sent"])
        self.assertEqual(len(bot.patches), 2)
        minimal_card = json.loads(bot.patches[1][1])
        self.assertEqual(minimal_card["body"]["elements"], [{"tag": "markdown", "content": "无"}])

        controller.flush_execution_card("ou_user", "c1", immediate=True)

        self.assertEqual(replies, [("c1", "启动失败：后端不可用", "msg-1", True)])
        self.assertEqual(len(bot.patches), 3)

    def test_publish_terminal_result_prefers_terminal_result_card_when_reply_fits_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-2",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        parent_id, msg_type, content, reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(parent_id, "msg-2")
        self.assertEqual(msg_type, "interactive")
        self.assertTrue(reply_in_thread)
        card = json.loads(content)
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["title"]["content"], "Codex")
        self.assertIn(TERMINAL_RESULT_CARD_MARKER, card["body"]["elements"][-1]["content"])
        self.assertIn("done", card["body"]["elements"][-1]["content"])
        self.assertRegex(card["body"]["elements"][-1]["element_id"], r"^fc_tr_[0-9a-f]{32}_[0-9a-f]{16}$")
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("plan-card-1", "", "done")],
        )
        self.assertRegex(recorded[0]["terminal_result_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(recorded[0]["thread_id"], "")
        self.assertRegex(recorded[0]["checksum"], r"^[0-9a-f]{64}$")

    def test_publish_terminal_result_records_authoritative_text_without_stripping(self) -> None:
        state = self._make_state()
        controller, _bot, _replies, _, recorded = self._make_controller(state)
        raw_text = "  code output\n"

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-preserve",
        )

        self.assertTrue(ok)
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)
        self.assertEqual(recorded[-1]["checksum"], terminal_result_checksum(raw_text))

    def test_publish_terminal_result_uses_its_payload_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            terminal_result_card_limit=1000,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="long enough",
            prompt_message_id="msg-3",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        self.assertEqual(bot.reply_refs[-1][0], "msg-3")
        self.assertEqual(bot.reply_refs[-1][1], "interactive")
        self.assertEqual(recorded[-1]["message_id"], "plan-card-1")
        self.assertEqual(recorded[-1]["execution_message_id"], "")

    def test_publish_terminal_result_falls_back_to_text_when_authoritative_payload_exceeds_budget(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            terminal_result_card_limit=5,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="# 标题\n\n## 小节\n\n- 条目",
            prompt_message_id="msg-3b",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(
            replies,
            [("c1", "# 标题\n\n## 小节\n\n- 条目", "msg-3b", False)],
        )
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("text-reply-1", "", "# 标题\n\n## 小节\n\n- 条目")],
        )

    def test_publish_terminal_result_budget_uses_terminal_result_card_payload_bytes(self) -> None:
        state = self._make_state()
        raw_text = "![x](a.png)"
        card_content = build_terminal_result_card_message_content(
            raw_text,
            terminal_result_id="0" * 32,
            checksum="1" * 64,
        )
        controller, bot, replies, _, recorded = self._make_controller(
            state,
            terminal_result_card_limit=len(card_content.encode("utf-8")) - 1,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-budget",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(replies, [("c1", raw_text, "msg-budget", True)])
        self.assertEqual(recorded[-1]["message_id"], "text-reply-1")
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)

    def test_publish_terminal_result_with_embedded_image_markdown_uses_sanitized_card(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="![示意图](/tmp/demo.png)\n\nPNG 已生成。",
            prompt_message_id="msg-image",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        parent_id, msg_type, content, reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(parent_id, "msg-image")
        self.assertEqual(msg_type, "interactive")
        self.assertFalse(reply_in_thread)
        card = json.loads(content)
        self.assertIn("【图片】示意图", card["body"]["elements"][-1]["content"])
        self.assertIn("PNG 已生成。", card["body"]["elements"][-1]["content"])

    def test_publish_terminal_result_records_raw_text_while_card_uses_feishu_projection(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)
        raw_text = (
            "- 检查参数\n"
            "  ```python\n"
            "  {\"open_timeout\": ..., \"max_size\": None, \"proxy\": None}\n"
            "  ```"
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text=raw_text,
            prompt_message_id="msg-code",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        card = json.loads(bot.reply_refs[-1][2])
        content = card["body"]["elements"][-1]["content"]
        self.assertIn("- 检查参数\n\n```python\n", content)
        self.assertEqual(recorded[-1]["final_reply_text"], raw_text)
        self.assertEqual(recorded[-1]["checksum"], terminal_result_checksum(raw_text))

    def test_publish_terminal_result_sanitizes_headings_for_feishu_card(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, _ = self._make_controller(state)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="# 标题\n\n## 小节\n\n- 条目",
            prompt_message_id="msg-heading",
            prompt_reply_in_thread=False,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        _parent_id, msg_type, content, _reply_in_thread = bot.reply_refs[-1]
        self.assertEqual(msg_type, "interactive")
        card = json.loads(content)
        self.assertIn("# 标题", card["body"]["elements"][-1]["content"])
        self.assertIn("## 小节", card["body"]["elements"][-1]["content"])

    def test_publish_terminal_result_falls_back_to_top_level_card_before_text(self) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)

        def _reply_fail(
            chat_id: str,
            parent_id: str,
            msg_type: str,
            content: str,
            *,
            reply_in_thread: bool = False,
        ) -> FeishuOutboundResult:
            bot.reply_refs.append((parent_id, msg_type, content, reply_in_thread))
            return _rejected(
                FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
            )

        bot.reply_to_message = _reply_fail  # type: ignore[method-assign]

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-4",
            prompt_reply_in_thread=True,
        )

        self.assertTrue(ok)
        self.assertEqual(replies, [])
        self.assertEqual(bot.reply_refs[-1][0], "msg-4")
        self.assertEqual(bot.sent_messages[-1][0], "c1")
        self.assertEqual(bot.sent_messages[-1][1], "interactive")
        self.assertEqual(
            [(item["message_id"], item["execution_message_id"], item["final_reply_text"]) for item in recorded],
            [("plan-card-2", "", "done")],
        )

    def test_unknown_terminal_card_reply_does_not_send_or_fallback_to_text(
        self,
    ) -> None:
        state = self._make_state()
        controller, bot, replies, _, recorded = self._make_controller(state)
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-unknown",
        )

        self.assertFalse(ok)
        self.assertEqual(len(bot.reply_refs), 1)
        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(replies, [])
        self.assertEqual(recorded, [])

    def test_publish_terminal_result_returns_false_when_text_fallback_fails(self) -> None:
        state = self._make_state()
        bot = _FakeBot()
        replies: list[tuple[str, str, str, bool]] = []
        lock = threading.RLock()
        turn_execution = TurnExecutionCoordinator()
        binding = ("ou_user", "c1")
        resident_states = {binding: state}
        binding_runtime = _TestBindingRuntime(resident_states)

        controller = ExecutionOutputController(
            runtime=ExecutionOutputRuntimeTransitions(
                lock=lock,
                binding_runtime=binding_runtime,
                turn_execution=turn_execution,
            ),
            runtime_submit=lambda target, *args, **kwargs: target(*args, **kwargs),
            resolve_session=lambda sender_id, chat_id: binding_runtime.resolve_session(
                binding
            ),
            card_publisher_factory=lambda: RuntimeCardPublisher(bot),
            dispatch_execution_card_patch=lambda chat_id, message_id, model: None,
            reply_text=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or False
            ),
            reply_text_get_id=lambda chat_id, text, *, message_id="", reply_in_thread=False: (
                replies.append((chat_id, text, message_id, reply_in_thread)) or ""
            ),
            record_terminal_result_card=lambda *, message_id, execution_message_id, final_reply_text, terminal_result_id="", thread_id="", checksum="": None,
            terminal_result_card_limit=lambda: 0,
            stream_patch_interval_ms=lambda: 1,
        )

        ok = controller.publish_terminal_result(
            "c1",
            final_reply_text="done",
            prompt_message_id="msg-5",
            prompt_reply_in_thread=True,
        )

        self.assertFalse(ok)
        self.assertEqual(replies, [("c1", "done", "msg-5", True)])

    def test_schedule_execution_card_update_immediate_path_dispatches_card_patch(self) -> None:
        state = self._make_state()
        controller, bot, _, dispatched, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-1")
        state["started_at"] = time.monotonic() - 1
        state["execution_transcript"].set_reply_text("done")
        state["last_patch_at"] = 0.0

        controller.schedule_execution_card_update("ou_user", "c1")

        self.assertEqual(bot.patches, [])
        self.assertEqual(dispatched[-1]["message_id"], "card-1")

    def test_exact_session_output_effects_reject_replacement_without_side_effects(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["current_thread_id"] = "thread-1"
        state_a["current_turn_id"] = "turn-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_a["started_at"] = 1.0
        state_a["plan_turn_id"] = "turn-1"
        state_a["plan_steps"] = [{"step": "old", "status": "in_progress"}]
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        captured = binding_runtime.resolve_session(binding)

        state_b = self._make_state()
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        state_b["started_at"] = 1.0
        state_b["last_patch_at"] = 17.0
        state_b["plan_turn_id"] = "turn-1"
        state_b["plan_steps"] = [{"step": "old", "status": "in_progress"}]
        resident_states[binding] = state_b

        controller, bot, replies, dispatched, _ = self._make_controller(
            state_a,
            resident_states=resident_states,
            binding_runtime=binding_runtime,
        )

        controller.schedule_execution_card_update_for_session(captured)
        controller.flush_execution_card_for_session(captured, immediate=True)
        controller.flush_plan_card_for_session(captured)

        self.assertEqual(state_b["last_patch_at"], 17.0)
        self.assertIsNone(state_b["patch_timer_registration"])
        self.assertEqual(state_b["plan_message_id"], "")
        self.assertEqual(bot.patches, [])
        self.assertEqual(bot.reply_refs, [])
        self.assertEqual(bot.sent_messages, [])
        self.assertEqual(replies, [])
        self.assertEqual(dispatched, [])

    def test_delayed_patch_rejects_replacement_with_same_coordinates_and_replay(self) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        resident_states = {binding: state_a}
        controller, _, _, dispatched, _ = self._make_controller(
            state_a,
            resident_states=resident_states,
            stream_patch_interval_ms=60_000,
        )
        for state in (state_a,):
            state["current_thread_id"] = "thread-1"
            state["current_turn_id"] = "turn-1"
            set_execution_page_state(state, current_message_id="card-1")
            state["last_patch_at"] = time.monotonic()

        controller.schedule_execution_card_update(*binding)
        registration_a = state_a["patch_timer_registration"]
        self.assertIsNotNone(registration_a)
        assert registration_a is not None
        registration_a.timer.cancel()

        state_b = self._make_state()
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        state_b["last_patch_at"] = time.monotonic()
        resident_states[binding] = state_b
        controller.schedule_execution_card_update(*binding)
        registration_b = state_b["patch_timer_registration"]
        self.assertIsNotNone(registration_b)
        assert registration_b is not None
        registration_b.timer.cancel()
        last_patch_at = state_b["last_patch_at"]

        controller.consume_execution_card_patch_timer(registration_a.ticket)

        self.assertIs(state_b["patch_timer_registration"], registration_b)
        self.assertEqual(state_b["last_patch_at"], last_patch_at)
        self.assertEqual(dispatched, [])

        controller.consume_execution_card_patch_timer(registration_b.ticket)
        controller.consume_execution_card_patch_timer(registration_b.ticket)

        self.assertIsNone(state_b["patch_timer_registration"])
        self.assertEqual([item["message_id"] for item in dispatched], ["card-1"])

    def test_timer_construction_replacement_blocks_old_registration_install(
        self,
    ) -> None:
        binding = ("ou_user", "c1")
        state_a = self._make_state()
        state_a["current_thread_id"] = "thread-1"
        state_a["current_turn_id"] = "turn-1"
        set_execution_page_state(state_a, current_message_id="card-1")
        state_a["last_patch_at"] = time.monotonic()
        state_b = self._make_state()
        state_b["current_thread_id"] = "thread-1"
        state_b["current_turn_id"] = "turn-1"
        set_execution_page_state(state_b, current_message_id="card-1")
        state_b["last_patch_at"] = state_a["last_patch_at"]
        resident_states = {binding: state_a}
        binding_runtime = _TestBindingRuntime(resident_states)
        controller, _, _, dispatched, _ = self._make_controller(
            state_a,
            resident_states=resident_states,
            binding_runtime=binding_runtime,
            stream_patch_interval_ms=60_000,
        )
        timers = []

        class _ReplacementTimer:
            def __init__(self, *args, **kwargs) -> None:
                self.daemon = False
                self.cancelled = False
                self.started = False
                timers.append(self)
                resident_states[binding] = state_b

            def start(self) -> None:
                self.started = True

            def cancel(self) -> None:
                self.cancelled = True

        with patch(
            "bot.execution_output_controller.threading.Timer",
            _ReplacementTimer,
        ):
            controller.schedule_execution_card_update(*binding)

        self.assertEqual(len(timers), 1)
        self.assertTrue(timers[0].cancelled)
        self.assertFalse(timers[0].started)
        self.assertIsNone(state_a["patch_timer_registration"])
        self.assertIsNone(state_b["patch_timer_registration"])
        self.assertEqual(dispatched, [])

    def test_delayed_patch_cancel_reschedule_rejects_old_ticket(self) -> None:
        state = self._make_state()
        controller, _, _, dispatched, _ = self._make_controller(
            state,
            stream_patch_interval_ms=60_000,
        )
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["last_patch_at"] = time.monotonic()
        controller.schedule_execution_card_update("ou_user", "c1")
        registration_a = state["patch_timer_registration"]
        assert registration_a is not None
        registration_a.timer.cancel()

        controller.flush_execution_card("ou_user", "c1", background=True)
        controller.schedule_execution_card_update("ou_user", "c1")
        registration_b = state["patch_timer_registration"]
        assert registration_b is not None
        registration_b.timer.cancel()
        dispatch_count = len(dispatched)

        controller.consume_execution_card_patch_timer(registration_a.ticket)

        self.assertIs(state["patch_timer_registration"], registration_b)
        self.assertEqual(len(dispatched), dispatch_count)

    def test_delayed_patch_after_clear_is_side_effect_free(self) -> None:
        binding = ("ou_user", "c1")
        state = self._make_state()
        resident_states = {binding: state}
        controller, _, _, dispatched, _ = self._make_controller(
            state,
            resident_states=resident_states,
            stream_patch_interval_ms=60_000,
        )
        set_execution_page_state(state, current_message_id="card-1")
        state["last_patch_at"] = time.monotonic()
        controller.schedule_execution_card_update(*binding)
        registration = state["patch_timer_registration"]
        assert registration is not None
        registration.timer.cancel()
        del resident_states[binding]

        controller.consume_execution_card_patch_timer(registration.ticket)

        self.assertEqual(resident_states, {})
        self.assertEqual(dispatched, [])

    def test_delayed_patch_start_failure_clears_registration(self) -> None:
        state = self._make_state()
        controller, _, _, _, _ = self._make_controller(
            state,
            stream_patch_interval_ms=60_000,
        )
        set_execution_page_state(state, current_message_id="card-1")
        state["last_patch_at"] = time.monotonic()

        class _StartFailureTimer:
            cancelled = False

            def __init__(self, *args, **kwargs) -> None:
                self.daemon = False

            def start(self) -> None:
                raise RuntimeError("start failed")

            def cancel(self) -> None:
                type(self).cancelled = True

        with patch("bot.execution_output_controller.threading.Timer", _StartFailureTimer):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                controller.schedule_execution_card_update("ou_user", "c1")

        self.assertIsNone(state["patch_timer_registration"])
        self.assertTrue(_StartFailureTimer.cancelled)

    def test_background_flush_execution_card_dispatches_without_sync_patch(self) -> None:
        state = self._make_state()
        controller, bot, _, dispatched, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="card-2")
        state["started_at"] = time.monotonic() - 2
        state["execution_transcript"].set_reply_text("done")

        controller.flush_execution_card("ou_user", "c1", immediate=True, background=True)

        self.assertEqual(bot.patches, [])
        self.assertEqual(dispatched[-1]["message_id"], "card-2")

    def test_oversized_execution_projection_rolls_to_contiguous_pages(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        state["started_at"] = time.monotonic()
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].append_process_note("x" * 200)
        controller, bot, _, dispatched, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=800,
        )

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        ledger = state["execution_pages"]
        self.assertEqual(
            tuple(page.status for page in ledger.pages),
            (ExecutionPageStatus.SEALED, ExecutionPageStatus.ACTIVE),
        )
        self.assertEqual(ledger.pages[0].cursor_end, ledger.pages[1].cursor_start)
        self.assertEqual(len(bot.reply_refs), 1)
        self.assertTrue(bot.attempt_ids[0])
        self.assertEqual(
            "".join(item["log_text"] for item in dispatched),
            "x" * 200,
        )
        self.assertEqual(
            [item["message_id"] for item in dispatched],
            ["card-1", "plan-card-1"],
        )
        self.assertFalse(dispatched[0]["running"])
        self.assertTrue(dispatched[1]["running"])

    def test_one_flush_can_emit_many_bounded_pages_without_content_loss(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        state["started_at"] = time.monotonic()
        set_execution_page_state(state, current_message_id="card-1")
        process_text = "过程" * 300
        reply_text = "回复" * 300
        state["execution_transcript"].append_process_note(process_text)
        state["execution_transcript"].set_reply_text(reply_text)
        payload_limit = 800
        controller, bot, _, dispatched, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=payload_limit,
        )

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        ledger = state["execution_pages"]
        self.assertGreaterEqual(len(ledger.pages), 4)
        self.assertEqual(len(dispatched), len(ledger.pages))
        self.assertEqual(len(bot.reply_refs), len(ledger.pages) - 1)
        self.assertEqual(
            "".join(item["log_text"] for item in dispatched),
            process_text,
        )
        self.assertEqual(
            "".join(item["reply_text"] for item in dispatched),
            reply_text,
        )
        self.assertTrue(
            all(
                execution_card_model_fits_page(
                    item["model"],
                    payload_limit_bytes=payload_limit,
                )
                for item in dispatched
            )
        )
        for previous, following in zip(
            ledger.pages[:-1],
            ledger.pages[1:],
            strict=True,
        ):
            self.assertEqual(previous.cursor_end, following.cursor_start)
        self.assertEqual(
            ledger.active_projection_end(state["execution_transcript"]),
            ExecutionTranscriptCursor.from_transcript(
                state["execution_transcript"]
            ),
        )

    def test_oversized_terminal_transcript_finishes_as_bounded_sealed_pages(
        self,
    ) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = False
        state["started_at"] = time.monotonic()
        set_execution_page_state(state, current_message_id="card-1")
        process_text = "终态过程" * 300
        reply_text = "终态回复" * 300
        state["execution_transcript"].append_process_note(process_text)
        state["execution_transcript"].set_reply_text(reply_text)
        payload_limit = 800
        controller, bot, _, dispatched, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=payload_limit,
        )

        captured = controller._resolve_session("ou_user", "c1")
        controller.present_terminal_execution_card(
            captured,
            background=True,
        )

        self.assertEqual(len(captured.execution.pages.pages), 1)
        self.assertGreaterEqual(len(dispatched), 4)
        self.assertEqual(
            len(bot.reply_refs),
            len(dispatched) - 1,
        )
        self.assertTrue(all(not item["running"] for item in dispatched))
        self.assertTrue(
            all(
                execution_card_model_fits_page(
                    item["model"],
                    payload_limit_bytes=payload_limit,
                )
                for item in dispatched
            )
        )
        self.assertEqual(
            "".join(item["log_text"] for item in dispatched),
            process_text,
        )
        self.assertEqual(
            "".join(item["reply_text"] for item in dispatched),
            reply_text,
        )

        TurnExecutionCoordinator().retire_execution_locked(state)

        self.assertTrue(
            all(
                page.status is ExecutionPageStatus.SEALED
                for page in state["execution_pages"].pages
            )
        )
        self.assertEqual(len(state["execution_pages"].pages), 1)
        self.assertIsNone(state["execution_pages"].current_page)
        self.assertEqual(
            state["execution_pages"].pages[-1].cursor_end,
            ExecutionTranscriptCursor.from_transcript(
                state["execution_transcript"]
            ),
        )

    def test_terminal_initial_unknown_reconciliation_is_one_exact_effect(
        self,
    ) -> None:
        cases = (
            (
                "confirmed",
                _confirmed(
                    FeishuOutboundOperation.REPLY_MESSAGE,
                    message_id="reconciled-card",
                ),
                False,
                ["reconciled-card"],
            ),
            ("rejected", _rejected(FeishuOutboundOperation.REPLY_MESSAGE), False, []),
            ("unknown", _unknown(FeishuOutboundOperation.REPLY_MESSAGE), False, []),
            ("already_attempted", None, True, []),
        )
        for name, result, already_attempted, expected_messages in cases:
            with self.subTest(outcome=name):
                state = self._make_state()
                state["current_prompt_message_id"] = "prompt-1"
                state["execution_transcript"].set_reply_text("done")
                attempt_id = f"stable-{name}-uuid"
                opening = ExecutionPageLedger.empty().prepare_initial(
                    outbound_attempt_id=attempt_id,
                )
                pending = opening.pending_page
                assert pending is not None
                ledger = opening.mark_send_unknown(expected_page=pending)
                if already_attempted:
                    pending = ledger.pending_page
                    assert pending is not None
                    ledger = ledger.retain_send_unknown(expected_page=pending)
                state["execution_pages"] = ledger
                controller, bot, _, dispatched, _ = self._make_controller(state)
                bot.reply_result = result

                controller.present_terminal_execution_card(
                    controller._resolve_session("ou_user", "c1"),
                    background=True,
                )

                self.assertEqual(bot.attempt_ids, [] if already_attempted else [attempt_id])
                self.assertEqual(len(bot.reply_refs), 0 if already_attempted else 1)
                self.assertEqual(
                    [effect["message_id"] for effect in dispatched],
                    expected_messages,
                )
                self.assertTrue(all(not effect["running"] for effect in dispatched))
                self.assertIs(state["execution_pages"], ledger)

    def test_terminal_rollover_unknown_reconciles_original_uuid_and_ranges(
        self,
    ) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = False
        state["execution_transcript"].append_process_note("x" * 80)
        process_text = state["execution_transcript"].process_text()
        rollover_cursor = ExecutionTranscriptCursor(
            process_chars=len(process_text) // 2,
        )
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="initial-uuid",
            known_message_id="card-1",
        )
        initial_page = opening.pending_page
        assert initial_page is not None
        active = opening.activate_opening(
            expected_page=initial_page,
            message_id="card-1",
        )
        rollover = active.prepare_rollover(
            outbound_attempt_id="stable-rollover-uuid",
            cursor_start=rollover_cursor,
        )
        pending = rollover.pending_page
        assert pending is not None
        unknown = rollover.mark_rollover_send_unknown(
            expected_active=active.active_page,
            expected_opening=pending,
        )
        state["execution_pages"] = unknown
        controller, bot, _, dispatched, _ = self._make_controller(state)
        bot.reply_result = _confirmed(
            FeishuOutboundOperation.REPLY_MESSAGE,
            message_id="card-2",
        )

        controller.present_terminal_execution_card(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        self.assertEqual(bot.attempt_ids, ["stable-rollover-uuid"])
        self.assertEqual(len(bot.reply_refs), 1)
        self.assertEqual(
            [effect["message_id"] for effect in dispatched],
            ["card-1", "card-2"],
        )
        self.assertEqual(
            "".join(effect["log_text"] for effect in dispatched),
            process_text,
        )
        self.assertIs(state["execution_pages"], unknown)

    def test_unknown_rollover_reconciles_same_effect_without_new_generation(
        self,
    ) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].append_process_note("x" * 200)
        controller, bot, _, dispatched, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=800,
        )
        bot.reply_result = _unknown(FeishuOutboundOperation.REPLY_MESSAGE)

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )
        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        ledger = state["execution_pages"]
        self.assertEqual(len(bot.reply_refs), 2)
        self.assertEqual(bot.attempt_ids[0], bot.attempt_ids[1])
        self.assertEqual(len(ledger.pages), 2)
        self.assertEqual(ledger.current_message_id, "card-1")
        assert ledger.pending_page is not None
        self.assertIs(
            ledger.pending_page.status,
            ExecutionPageStatus.SEND_UNKNOWN,
        )
        self.assertEqual(
            dispatched[0]["log_text"],
            dispatched[1]["log_text"],
        )
        self.assertLess(len(dispatched[0]["log_text"]), 200)

    def test_rejected_rollover_can_retry_with_a_new_attempt(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].append_process_note("x" * 200)
        controller, bot, _, _, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=800,
        )
        bot.reply_result = _rejected(FeishuOutboundOperation.REPLY_MESSAGE)

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )
        self.assertEqual(len(state["execution_pages"].pages), 1)
        first_attempt = bot.attempt_ids[-1]
        bot.reply_result = None
        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        self.assertEqual(len(state["execution_pages"].pages), 2)
        self.assertNotEqual(first_attempt, bot.attempt_ids[-1])

    def test_rejected_unknown_reconciliation_uses_a_fresh_rollover_uuid(self) -> None:
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["current_prompt_message_id"] = "prompt-1"
        state["running"] = True
        set_execution_page_state(state, current_message_id="card-1")
        state["execution_transcript"].append_process_note("x" * 200)
        controller, bot, _, _, _ = self._make_controller(
            state,
            execution_page_payload_limit_bytes=800,
        )
        bot.reply_result_sequences = [
            _unknown(FeishuOutboundOperation.REPLY_MESSAGE),
            _rejected(FeishuOutboundOperation.REPLY_MESSAGE),
            _confirmed(
                FeishuOutboundOperation.REPLY_MESSAGE,
                message_id="card-2",
            ),
        ]

        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )
        controller.flush_execution_card_for_session(
            controller._resolve_session("ou_user", "c1"),
            background=True,
        )

        ledger = state["execution_pages"]
        self.assertEqual(len(bot.reply_refs), 3)
        self.assertEqual(bot.attempt_ids[0], bot.attempt_ids[1])
        self.assertNotEqual(bot.attempt_ids[1], bot.attempt_ids[2])
        self.assertEqual(len(ledger.pages), 2)
        self.assertEqual(ledger.current_message_id, "card-2")
        self.assertEqual(
            tuple(page.status for page in ledger.pages),
            (ExecutionPageStatus.SEALED, ExecutionPageStatus.ACTIVE),
        )

    def test_flush_plan_card_reuses_existing_or_updates_message_id(self) -> None:
        state = self._make_state()
        controller, bot, _, _, _ = self._make_controller(state)
        set_execution_page_state(state, current_message_id="exec-1")
        state["plan_message_id"] = "plan-existing"
        state["plan_turn_id"] = "turn-1"
        state["plan_explanation"] = "先分析"
        state["plan_steps"] = [{"step": "确认需求", "status": "completed"}]

        bot.patch_results["plan-existing"] = True
        controller.flush_plan_card("ou_user", "c1")
        self.assertEqual(state["plan_message_id"], "plan-existing")

        bot.patch_results["plan-existing"] = False
        controller.flush_plan_card("ou_user", "c1")

        self.assertEqual(state["plan_message_id"], "plan-card-1")
        self.assertEqual(bot.reply_refs[-1][0], "exec-1")
