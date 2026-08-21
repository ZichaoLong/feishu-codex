from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.execution_pages import (
    ExecutionPageLedger,
    TerminalExecutionPageReceipt,
)
from bot.feishu_execution_finalization_controller import (
    FeishuExecutionFinalizationController,
    FeishuExecutionFinalizationPorts,
    FeishuExecutionRuntimeChanged,
)
from bot.runtime_state import FEISHU_RUNTIME_ATTACHED
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state


class _Harness:
    def __init__(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.cleanup = tempdir.cleanup
        self.data_dir = pathlib.Path(tempdir.name)
        self.lock = threading.RLock()
        self.store = ChatBindingStore(self.data_dir)
        self.manager = BindingRuntimeManager(
            lock=self.lock,
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=self.store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(self.data_dir),
            is_group_chat=lambda _chat_id, _message_id: False,
        )
        self.dispatched: list[dict[str, object]] = []
        self.patched: list[dict[str, object]] = []
        self.released: list[tuple[ChatBindingKey, str, str]] = []
        self.drained: list[ChatBindingKey] = []
        self.dispatch_effect = None
        self.dispatch_error: Exception | None = None
        self.events: list[str] = []
        self.presentation_calls = []
        self.runtime_guard_calls = 0
        self.controller = FeishuExecutionFinalizationController(
            binding_runtime=self.manager,
            turn_execution=TurnExecutionCoordinator(),
            execution_output=self,
            runtime_context_guard=self._guard_runtime_context,
            ports=FeishuExecutionFinalizationPorts(
                lock=self.lock,
                release_main_turn=self._release,
                drain_execution_queue=self._drain,
            ),
        )

    def seed_execution(
        self,
        binding: ChatBindingKey = ("sender-a", "chat-a"),
        *,
        thread_id: str = "thread-a",
        card_message_id: str = "card-a",
        turn_id: str = "turn-a",
    ):
        with self.lock:
            state = self.manager._get_or_create_runtime_state_locked(binding)
            state["current_thread_id"] = thread_id
            state["current_thread_title"] = "demo"
            state["feishu_runtime_state"] = FEISHU_RUNTIME_ATTACHED
            set_execution_page_state(
                state,
                current_message_id=card_message_id,
            )
            state["current_turn_id"] = turn_id
            state["current_prompt_message_id"] = "prompt-a"
            state["running"] = True
            state["started_at"] = time.monotonic() - 3
            state["execution_transcript"].append_process_note("work")
            state["execution_transcript"].set_reply_text("done")
            session = self.manager.resident_session_snapshot_locked(binding)
            assert session is not None
        return state, session

    def replace_session(self, binding: ChatBindingKey):
        with self.lock:
            current_state = self.manager.resident_runtime_state_locked(binding)
            assert current_state is not None
            current_session = self.manager.resident_session_snapshot_locked(binding)
            assert current_session is not None
            self.manager._session_authority.retire(
                current_session.handle,
                binding=binding,
                resident_state=current_state,
            )
            replacement = self.manager.build_default_runtime_state()
            self.manager._runtime_state_by_binding[binding] = replacement
            self.manager._session_authority.install(
                binding,
                resident_state=replacement,
            )
            return replacement

    def _guard_runtime_context(self) -> None:
        self.runtime_guard_calls += 1

    def dispatch_execution_card_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> None:
        self.events.append("present")
        self.dispatched.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "transcript": transcript,
                "running": running,
                "elapsed": elapsed,
                "cancelled": cancelled,
            }
        )
        if self.dispatch_effect is not None:
            self.dispatch_effect()
        if self.dispatch_error is not None:
            raise self.dispatch_error

    def present_terminal_execution_card(self, captured, *, background=True):
        del background
        self.presentation_calls.append(captured)
        if not captured.execution.current_message_id:
            self.events.append("present")
            return ()
        page = captured.execution.pages.active_page
        cursor_end = captured.execution.pages.active_projection_end(
            captured.execution.transcript
        )
        assert page is not None and cursor_end is not None
        self.dispatch_execution_card_message(
            captured.binding[1],
            captured.execution.current_message_id,
            transcript=captured.execution.transcript,
            running=False,
            elapsed=3,
            cancelled=captured.execution.cancelled,
        )
        return (
            TerminalExecutionPageReceipt(
                message_id=page.message_id,
                cursor_start=page.cursor_start,
                cursor_end=cursor_end,
            ),
        )

    def patch_execution_card_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ):
        self.patched.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "transcript": transcript,
                "running": running,
                "elapsed": elapsed,
                "cancelled": cancelled,
            }
        )
        return SimpleNamespace(applied=True)

    def _release(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        turn_id: str,
    ) -> bool:
        self.events.append("release")
        self.released.append((binding, thread_id, turn_id))
        return True

    def _drain(self, binding: ChatBindingKey) -> None:
        self.events.append("drain")
        self.drained.append(binding)


class FeishuExecutionFinalizationControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Harness()
        self.addCleanup(self.harness.cleanup)

    def test_exact_finalize_dispatches_retires_persists_and_drains(self) -> None:
        state, captured = self.harness.seed_execution()

        result = self.harness.controller.finalize(captured)

        self.assertTrue(result.had_card)
        self.assertTrue(result.retired)
        self.assertEqual(result.presentation_error, "")
        self.assertEqual(
            [receipt.message_id for receipt in result.terminal_page_receipts],
            ["card-a"],
        )
        self.assertEqual(self.harness.dispatched[0]["message_id"], "card-a")
        self.assertEqual(
            self.harness.dispatched[0]["transcript"].reply_text(),
            "done",
        )
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")
        self.assertFalse(state["running"])
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertEqual(self.harness.events, ["release", "drain", "present"])
        self.assertIsNotNone(self.harness.store.load(captured.binding))

    def test_prepare_and_commit_advances_fifo_before_external_presentation(
        self,
    ) -> None:
        state, captured = self.harness.seed_execution()

        plan = self.harness.controller.prepare_and_commit_if_current(captured)
        assert plan is not None

        self.assertTrue(plan.had_card)
        self.assertTrue(plan.retired)
        self.assertIsNotNone(plan.presentation_snapshot)
        self.assertFalse(state["running"])
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertEqual(self.harness.events, ["release", "drain"])
        self.assertEqual(self.harness.presentation_calls, [])
        self.assertEqual(self.harness.runtime_guard_calls, 1)

        result = self.harness.controller.present(plan)

        self.assertTrue(result.had_card)
        self.assertTrue(result.retired)
        self.assertEqual(
            [receipt.message_id for receipt in result.terminal_page_receipts],
            ["card-a"],
        )
        self.assertEqual(self.harness.events, ["release", "drain", "present"])
        self.assertEqual(self.harness.runtime_guard_calls, 1)

    def test_external_presentation_failure_cannot_rollback_committed_plan(
        self,
    ) -> None:
        state, captured = self.harness.seed_execution()
        plan = self.harness.controller.prepare_and_commit_if_current(captured)
        assert plan is not None
        self.harness.dispatch_error = RuntimeError("card unavailable")

        with self.assertLogs(
            "bot.feishu_execution_finalization_controller",
            level="ERROR",
        ):
            result = self.harness.controller.present(plan)

        self.assertTrue(result.retired)
        self.assertEqual(result.presentation_error, "card unavailable")
        self.assertFalse(state["running"])
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertIsNotNone(self.harness.store.load(captured.binding))

    def test_replacement_during_post_retirement_presentation_cannot_undo_commit(
        self,
    ) -> None:
        state, captured = self.harness.seed_execution()
        replacement = None

        def replace() -> None:
            nonlocal replacement
            replacement = self.harness.replace_session(captured.binding)

        self.harness.dispatch_effect = replace

        result = self.harness.controller.finalize(captured)

        assert replacement is not None
        self.assertTrue(result.retired)
        self.assertFalse(replacement["running"])
        self.assertEqual(
            replacement["execution_pages"].current_message_id,
            "",
        )
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertIsNotNone(self.harness.store.load(captured.binding))
        self.assertFalse(state["running"])
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")

    def test_owner_revision_a_b_a_invalidates_old_finalize_handle(self) -> None:
        _state, captured = self.harness.seed_execution()
        with self.harness.lock:
            self.harness.manager._advance_binding_owner_revision_locked(
                captured.binding
            )
            self.harness.manager._advance_binding_owner_revision_locked(
                captured.binding
            )

        with self.assertRaisesRegex(
            FeishuExecutionRuntimeChanged,
            "stale or replaced",
        ):
            self.harness.controller.finalize(captured)

        self.assertEqual(self.harness.dispatched, [])
        self.assertEqual(self.harness.drained, [])

    def test_staged_prepare_rejects_replaced_execution_without_effects(self) -> None:
        _state, captured = self.harness.seed_execution()
        replacement = self.harness.replace_session(captured.binding)

        plan = self.harness.controller.prepare_and_commit_if_current(captured)

        self.assertIsNone(plan)
        self.assertFalse(replacement["running"])
        self.assertEqual(self.harness.released, [])
        self.assertEqual(self.harness.drained, [])
        self.assertEqual(self.harness.presentation_calls, [])

    def test_presentation_failure_does_not_rollback_retirement(self) -> None:
        state, captured = self.harness.seed_execution()
        self.harness.dispatch_error = RuntimeError("card unavailable")

        result = self.harness.controller.finalize(captured)

        self.assertTrue(result.retired)
        self.assertEqual(result.presentation_error, "card unavailable")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertIsNotNone(self.harness.store.load(captured.binding))

    def test_send_unknown_page_does_not_block_retirement_or_fifo(self) -> None:
        state, captured = self.harness.seed_execution(card_message_id="")
        opening = ExecutionPageLedger.empty().prepare_initial(
            outbound_attempt_id="unknown-page-attempt",
        )
        page = opening.current_page
        assert page is not None
        with self.harness.lock:
            state["execution_pages"] = opening.mark_send_unknown(
                expected_page=page,
            )
            captured = self.harness.manager.session_snapshot_locked(captured.handle)

        result = self.harness.controller.finalize(captured)

        self.assertFalse(result.had_card)
        self.assertTrue(result.retired)
        self.assertEqual(state["execution_pages"].pages, ())
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])
        self.assertEqual(self.harness.dispatched, [])
        self.assertEqual(self.harness.events, ["release", "drain", "present"])
        self.assertEqual(len(self.harness.presentation_calls), 1)
        self.assertTrue(
            self.harness.presentation_calls[0].execution.pages.send_outcome_unknown
        )

    def test_persistence_failure_does_not_block_release_or_drain(self) -> None:
        state, captured = self.harness.seed_execution()

        def fail_save(_binding, _state) -> None:
            raise RuntimeError("binding store unavailable")

        self.harness.store.save = fail_save  # type: ignore[method-assign]

        with self.assertLogs(
            "bot.feishu_execution_finalization_controller",
            level="ERROR",
        ):
            result = self.harness.controller.finalize(captured)

        self.assertTrue(result.retired)
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "card-a")
        self.assertEqual(
            self.harness.released,
            [(captured.binding, "thread-a", "turn-a")],
        )
        self.assertEqual(self.harness.drained, [captured.binding])

    def test_exact_refresh_does_not_follow_same_chat_ingress_remap(self) -> None:
        p2p_binding = ("sender-a", "chat-shared")
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "chat-shared")
        p2p_state, p2p_session = self.harness.seed_execution(
            p2p_binding,
            card_message_id="p2p-card",
        )
        self.harness.seed_execution(
            group_binding,
            card_message_id="group-card",
        )
        with self.harness.lock:
            set_execution_page_state(
                p2p_state,
                last_message_id="p2p-card",
            )
            p2p_state["current_turn_id"] = ""
            p2p_state["running"] = False
            p2p_session = self.harness.manager.session_snapshot_locked(
                p2p_session.handle
            )
        self.assertEqual(
            self.harness.manager.resolve_session(*p2p_binding).binding,
            group_binding,
        )

        refreshed = self.harness.controller.refresh_terminal_card(p2p_session)

        self.assertTrue(refreshed)
        self.assertEqual(
            [effect["message_id"] for effect in self.harness.patched],
            ["p2p-card"],
        )

    def test_stale_exact_refresh_has_no_presentation_effect(self) -> None:
        _state, captured = self.harness.seed_execution()
        self.harness.replace_session(captured.binding)

        refreshed = self.harness.controller.refresh_terminal_card(captured)

        self.assertFalse(refreshed)
        self.assertEqual(self.harness.patched, [])


if __name__ == "__main__":
    unittest.main()
