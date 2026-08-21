"""Failure-boundary regressions for Handler-owned Feishu compact starts."""

import unittest
from dataclasses import replace

from bot.execution_pages import ExecutionPageStatus
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.feishu_outbound import FeishuOutboundOperation
from bot.feishu_compact_execution_service import FeishuCompactRuntimeChanged
from tests.focus_runtime.codex_handler_fakes import _outbound_rejected, _runtime_state
from tests.focus_runtime.codex_handler_test_harness import CodexHandlerHarness


class CodexHandlerCompactFailureTests(CodexHandlerHarness):
    """Keep compact failure injection out of the broad Handler suite."""

    def _make_handler(self, *args, **kwargs):
        handler, bot = super()._make_handler(*args, **kwargs)
        self._seed_authoritative_thread(handler, status="idle")
        return handler, bot

    def _make_attached_compact_handler(self):
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"
        return handler, state

    def _observe_known_failure_settlement(self, handler) -> list[str]:
        original_settle = handler._feishu_root_operations.settle_known_failure
        settlement_reasons: list[str] = []

        def observe_settlement(token, *, reason: str) -> None:
            settlement_reasons.append(reason)
            original_settle(token, reason=reason)

        handler._feishu_root_operations.settle_known_failure = observe_settlement
        return settlement_reasons

    def _assert_compact_admission_settled(self, handler) -> None:
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

    def test_reply_routing_failure_settles_before_compact_send(self) -> None:
        handler, state = self._make_attached_compact_handler()
        settlement_reasons = self._observe_known_failure_settlement(handler)

        def fail_reply_routing(_message_id: str) -> bool:
            raise RuntimeError("injected reply routing failure")

        handler._feishu_platform.message_reply_in_thread = fail_reply_routing

        result = handler._runtime_call(
            handler._feishu_compact_execution.start,
            "ou_user",
            "c1",
            message_id="m-compact",
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "compact_local_prepare_failed")
        self.assertEqual(
            settlement_reasons,
            ["feishu_compact_local_prepare_failed"],
        )
        self.assertFalse(state["running"])
        self._assert_compact_admission_settled(handler)

    def test_retired_exact_binding_is_not_recreated_during_prepare(self) -> None:
        handler, _ = self._make_handler()
        gateway = handler._feishu_compact_execution._runtime
        snapshot = gateway.snapshot("ou_user", "c1", "m-compact")

        with handler._lock:
            handler._binding_runtime.deactivate_bindings_with_receipts_locked(
                (snapshot.binding,)
            )
            self.assertIsNone(
                handler._binding_runtime.resident_runtime_state_locked(
                    snapshot.binding
                )
            )

        with self.assertRaises(FeishuCompactRuntimeChanged):
            gateway.prime_execution(snapshot, message_id="m-compact")

        with handler._lock:
            self.assertIsNone(
                handler._binding_runtime.resident_runtime_state_locked(
                    snapshot.binding
                )
            )
        self.assertIsNone(handler._chat_binding_store.load(snapshot.binding))

    def test_execution_prepare_failure_settles_before_compact_send(self) -> None:
        for failure_point in ("prime_prompt", "append_process_note"):
            with self.subTest(failure_point=failure_point):
                handler, state = self._make_attached_compact_handler()
                settlement_reasons = self._observe_known_failure_settlement(handler)

                def fail_local_prepare(*_args, **_kwargs) -> None:
                    raise RuntimeError(f"injected {failure_point} failure")

                if failure_point == "prime_prompt":
                    handler._turn_execution.prime_prompt_turn_locked = (
                        fail_local_prepare
                    )
                else:
                    handler._turn_execution.append_process_note_locked = (
                        fail_local_prepare
                    )

                result = handler._runtime_call(
                    handler._feishu_compact_execution.start,
                    "ou_user",
                    "c1",
                    message_id="m-compact",
                )

                self.assertFalse(result.started)
                self.assertEqual(
                    result.reason_code,
                    "compact_local_prepare_failed",
                )
                self.assertEqual(
                    settlement_reasons,
                    ["feishu_compact_local_prepare_failed"],
                )
                self.assertFalse(state["running"])
                self._assert_compact_admission_settled(handler)

    def test_local_prepare_failure_keeps_submission_when_settlement_is_unconfirmed(
        self,
    ) -> None:
        handler, state = self._make_attached_compact_handler()

        def fail_reply_routing(_message_id: str) -> bool:
            raise RuntimeError("injected reply routing failure")

        def fail_owner_settlement(_token, *, reason: str) -> None:
            del reason
            raise RuntimeError("injected submission settlement failure")

        handler._feishu_platform.message_reply_in_thread = fail_reply_routing
        handler._feishu_root_operations.settle_known_failure = (
            fail_owner_settlement
        )

        result = handler._runtime_call(
            handler._feishu_compact_execution.start,
            "ou_user",
            "c1",
            message_id="m-compact",
        )

        self.assertFalse(result.started)
        self.assertEqual(result.reason_code, "compact_local_prepare_failed")
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertFalse(state["running"])
        self.assertIsNotNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            1,
        )

    def test_initial_page_rejection_settles_exact_admission_without_handler_release(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"
        handler._feishu_platform.bot.reply_to_message = (
            lambda chat_id, *_args, **_kwargs: _outbound_rejected(
                FeishuOutboundOperation.REPLY_MESSAGE,
                chat_id=chat_id,
            )
        )
        handler._terminal_execution.retire_ingress = lambda *_args, **_kwargs: self.fail(
            "compact failure cleanup must not use the lease-releasing handler helper"
        )
        original_settle = handler._feishu_root_operations.settle_known_failure
        settlement_reasons: list[str] = []

        def observe_settlement(token, *, reason: str) -> None:
            settlement_reasons.append(reason)
            original_settle(token, reason=reason)

        handler._feishu_root_operations.settle_known_failure = observe_settlement

        handler.handle_message(
            "ou_user",
            "c1",
            "/compact",
            message_id="m-compact",
        )

        self.assertEqual(settlement_reasons, ["feishu_compact_card_send_failed"])
        self.assertFalse(state["running"])
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

    def test_card_send_exception_retains_send_unknown_page_anchor(self) -> None:
        handler, _ = self._make_handler()
        state = _runtime_state(handler, "ou_user", "c1")
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "demo"
        state["feishu_runtime_state"] = "attached"

        def fail_card_send(*_args, **_kwargs):
            raise RuntimeError("injected card send failure")

        handler._feishu_platform.bot.reply_to_message = fail_card_send
        handler._terminal_execution.retire_ingress = lambda *_args, **_kwargs: self.fail(
            "compact failure cleanup must not use the lease-releasing handler helper"
        )
        original_settle = handler._feishu_root_operations.settle_known_failure
        settlement_observations: list[tuple[bool, bool, bool]] = []

        def observe_settlement(token, *, reason: str) -> None:
            with handler._lock:
                settlement_observations.append(
                    (
                        bool(state["running"]),
                        bool(state["awaiting_local_turn_started"]),
                        handler._turn_execution.has_active_execution_locked(state),
                    )
                )
            original_settle(token, reason=reason)

        handler._feishu_root_operations.settle_known_failure = observe_settlement

        handler.handle_message(
            "ou_user",
            "c1",
            "/compact",
            message_id="m-compact",
        )

        self.assertEqual(settlement_observations, [(False, False, True)])
        self.assertEqual(handler._adapter.compact_thread_calls, [])
        page = state["execution_pages"].current_page
        assert page is not None
        self.assertIs(page.status, ExecutionPageStatus.SEND_UNKNOWN)
        self.assertEqual(state["current_execution_kind"], "compact")
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

    def test_known_adapter_failure_settles_when_card_flush_raises(self) -> None:
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

        flush_calls: list[tuple[str, str]] = []
        owner_was_settled_at_flush: list[bool] = []

        def fail_card_flush(session, **_kwargs) -> None:
            flush_calls.append(session.binding)
            owner_was_settled_at_flush.append(
                handler._interaction_lease_store.load("thread-1") is None
            )
            raise RuntimeError("injected card flush failure")

        handler._adapter.compact_thread = fail_before_send
        compact_ports = handler._feishu_compact_execution._ports
        handler._feishu_compact_execution._ports = replace(
            compact_ports,
            presentation=replace(
                compact_ports.presentation,
                flush_execution_card_for_session=fail_card_flush,
            ),
        )
        handler._terminal_execution.retire_ingress = lambda *_args, **_kwargs: self.fail(
            "compact failure cleanup must not use the lease-releasing handler helper"
        )

        handler.handle_message(
            "ou_user",
            "c1",
            "/compact",
            message_id="m-compact",
        )

        self.assertEqual(flush_calls, [("ou_user", "c1")])
        self.assertEqual(owner_was_settled_at_flush, [True])
        self.assertEqual(state["current_execution_kind"], "")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertIsNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            0,
        )

    def test_unconfirmed_submission_settlement_retains_local_anchor_and_binding(self) -> None:
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

        def fail_owner_settlement(_token, *, reason: str) -> None:
            del reason
            raise RuntimeError("injected submission settlement failure")

        handler._adapter.compact_thread = fail_before_send
        handler._feishu_root_operations.settle_known_failure = fail_owner_settlement
        handler._terminal_execution.retire_ingress = lambda *_args, **_kwargs: self.fail(
            "unconfirmed owner settlement must not retire the local anchor"
        )

        handler.handle_message(
            "ou_user",
            "c1",
            "/compact",
            message_id="m-compact",
        )

        self.assertFalse(state["running"])
        self.assertFalse(state["awaiting_local_turn_started"])
        self.assertEqual(state["current_execution_kind"], "compact")
        self.assertEqual(state["current_prompt_message_id"], "m-compact")
        self.assertTrue(state["execution_pages"].current_message_id)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertIsNotNone(handler._interaction_lease_store.load("thread-1"))
        self.assertEqual(
            self._feishu_root_snapshot(handler, "thread-1").pending_admission_count,
            1,
        )

    def test_ack_local_recovery_failure_becomes_process_local_unknown(self) -> None:
        for failure_point in ("await_identity", "schedule_watchdog"):
            with self.subTest(failure_point=failure_point):
                handler, _ = self._make_handler()
                state = _runtime_state(handler, "ou_user", "c1")
                state["current_thread_id"] = "thread-1"
                state["current_thread_title"] = "demo"
                state["feishu_runtime_state"] = "attached"

                def fail_local_recovery(*_args, **_kwargs) -> None:
                    raise RuntimeError(f"injected {failure_point} failure")

                if failure_point == "await_identity":
                    handler._feishu_root_operations.await_start_identity = (
                        fail_local_recovery
                    )
                else:
                    handler._schedule_mirror_watchdog = fail_local_recovery

                result = handler._runtime_call(
                    handler._feishu_compact_execution.start,
                    "ou_user",
                    "c1",
                    message_id="m-compact",
                )

                self.assertFalse(result.started)
                self.assertEqual(
                    result.reason_code,
                    "compact_start_outcome_unknown",
                )
                self.assertEqual(handler._adapter.compact_thread_calls, ["thread-1"])
                self.assertIsNotNone(
                    handler._interaction_lease_store.load("thread-1")
                )
                self.assertTrue(state["running"])
                self.assertTrue(state["awaiting_local_turn_started"])
                self.assertEqual(
                    self._feishu_root_snapshot(
                        handler,
                        "thread-1",
                    ).pending_admission_count,
                    1,
                )
                self.assertTrue(
                    self._feishu_root_snapshot(
                        handler,
                        "thread-1",
                    ).submission_outcome_unknown
                )


if __name__ == "__main__":
    unittest.main()
