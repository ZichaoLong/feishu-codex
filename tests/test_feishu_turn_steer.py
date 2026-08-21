from __future__ import annotations

import threading
import unittest
from dataclasses import replace

from bot.adapters.base import ThreadSummary
from bot.binding_runtime_contract import (
    BindingExecutionSnapshot,
    BindingGoalSnapshot,
    BindingPlanSnapshot,
    BindingRuntimeHandle,
    BindingRuntimeSettingsSnapshot,
    BindingSessionSnapshot,
    BindingThreadSnapshot,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.feishu_turn_steer import FeishuTurnSteerController
from bot.reason_codes import ReasonedCheck
from tests.execution_page_test_support import execution_page_ledger


def _session(
    *,
    active: bool = True,
    attached: bool = True,
    running: bool = True,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    execution_kind: str = "prompt",
) -> BindingSessionSnapshot:
    return BindingSessionSnapshot(
        handle=BindingRuntimeHandle(
            _issuer_nonce=1,
            binding=("ou-user", "chat-1"),
            incarnation=1,
        ),
        active=active,
        thread=BindingThreadSnapshot(
            working_dir="/workspace",
            thread_id=thread_id,
            title="Demo",
            feishu_runtime_state="attached" if attached else "detached",
        ),
        settings=BindingRuntimeSettingsSnapshot(
            approval_policy="on-request",
            permissions_profile_id=":workspace",
            model="",
            reasoning_effort="",
            configured_settings=(),
        ),
        goal=BindingGoalSnapshot(
            objective="",
            status="",
            token_budget=None,
            tokens_used=0,
            time_used_seconds=0,
            created_at=0,
            updated_at=0,
        ),
        execution=BindingExecutionSnapshot(
            running=running,
            cancelled=False,
            pending_cancel=False,
            current_turn_id=turn_id,
            pages=execution_page_ledger(current_message_id="card-1"),
            current_execution_kind=execution_kind,
            current_prompt_message_id="message-1",
            current_prompt_reply_in_thread=False,
            current_actor_open_id="ou-user",
            transcript=ExecutionTranscriptSnapshot(),
            runtime_channel_state="live",
            started_at=1.0,
            last_runtime_event_at=1.0,
            last_patch_at=1.0,
            patch_timer_registered=False,
            mirror_watchdog_registered=False,
            followup_sent=False,
            followup_text="",
            terminal_result_text="",
            awaiting_local_turn_started=False,
            awaiting_attach_status_settle=False,
        ),
        plan=BindingPlanSnapshot(
            message_id="",
            turn_id="",
            explanation="",
            steps=(),
            text="",
        ),
    )


def _thread_summary(
    *,
    thread_id: str = "thread-1",
    status: str = "active",
) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        cwd="/workspace",
        name="Demo",
        preview="",
        created_at=0,
        updated_at=0,
        source="appServer",
        status=status,
    )


class _BindingRuntime:
    def __init__(self, session: BindingSessionSnapshot, events: list[str]) -> None:
        self.session = session
        self.events = events
        self.resolve_calls: list[tuple[str, str, str]] = []
        self.snapshot_error: Exception | None = None

    def resolve_session(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> BindingSessionSnapshot:
        self.events.append("resolve")
        self.resolve_calls.append((sender_id, chat_id, message_id))
        return self.session

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot:
        self.events.append("snapshot")
        if self.snapshot_error is not None:
            raise self.snapshot_error
        if handle is not self.session.handle:
            raise RuntimeError("stale handle")
        return self.session


class _AccessPolicy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.result = ReasonedCheck.allow()
        self.calls: list[tuple[str, str, str]] = []

    def all_mode_thread_exclusivity_violation_check(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> ReasonedCheck:
        del current_chat_mode
        self.events.append("exclusivity")
        self.calls.append((chat_id, thread_id, message_id))
        return self.result


class _DirectTargets:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.summary: object = _thread_summary()
        self.error: Exception | None = None
        self.after_read = None
        self.calls: list[tuple[str, str, str]] = []

    def read_direct_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
        operation: str,
    ) -> ThreadSummary:
        self.events.append("direct-read")
        self.calls.append((thread_id, original_arg, operation))
        if self.error is not None:
            raise self.error
        if self.after_read is not None:
            self.after_read()
        return self.summary  # type: ignore[return-value]


class _Adapter:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.connection_generation_value: object = 7
        self.connection_generation_error: Exception | None = None
        self.connection_generation_calls: list[bool] = []
        self.steer_result: object = {"turnId": "turn-1"}
        self.steer_error: Exception | None = None
        self.steer_calls: list[dict] = []

    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int:
        del timeout
        self.events.append("connection")
        self.connection_generation_calls.append(require_existing_connection)
        if self.connection_generation_error is not None:
            raise self.connection_generation_error
        return self.connection_generation_value  # type: ignore[return-value]

    def steer_turn(self, **kwargs):
        self.events.append("steer")
        self.steer_calls.append(dict(kwargs))
        if self.steer_error is not None:
            raise self.steer_error
        return self.steer_result


class FeishuTurnSteerControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[str] = []
        self.lock = threading.RLock()
        self.binding_runtime = _BindingRuntime(_session(), self.events)
        self.access_policy = _AccessPolicy(self.events)
        self.direct_targets = _DirectTargets(self.events)
        self.adapter = _Adapter(self.events)
        self.controller = FeishuTurnSteerController(
            lock=self.lock,
            adapter=self.adapter,
            binding_runtime=self.binding_runtime,
            access_policy=self.access_policy,
            direct_thread_targets=self.direct_targets,
        )

    def test_empty_text_returns_usage_without_reading_runtime(self) -> None:
        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "  ",
            "message-command",
        )

        self.assertIn("/steer 〈文本〉", result.text)
        self.assertEqual(self.events, [])

    def test_ineligible_binding_fails_before_group_or_backend_reads(self) -> None:
        cases = (
            ("inactive", _session(active=False), "未激活"),
            ("detached", _session(attached=False), "未附着"),
            ("missing-thread", _session(thread_id=""), "未附着"),
            ("idle", _session(running=False), "active turn"),
            ("missing-turn", _session(turn_id=""), "active turn"),
            ("compact", _session(execution_kind="compact"), "compact"),
        )
        for label, session, expected_text in cases:
            with self.subTest(label=label):
                self.events.clear()
                self.binding_runtime.session = session

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn(expected_text, result.text)
                self.assertIn("未发送", result.text)
                self.assertIn("未加入队列", result.text)
                self.assertEqual(self.events, ["resolve"])
                self.assertEqual(self.adapter.steer_calls, [])

    def test_group_all_exclusivity_denial_is_authoritative(self) -> None:
        self.access_policy.result = ReasonedCheck.deny(
            "group_all",
            "group all denied",
        )

        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "more context",
            "message-command",
        )

        self.assertEqual(result.text, "group all denied\n未发送，也未加入队列。")
        self.assertEqual(
            self.access_policy.calls,
            [("chat-1", "thread-1", "message-command")],
        )
        self.assertEqual(self.events, ["resolve", "exclusivity"])
        self.assertEqual(self.adapter.steer_calls, [])

    def test_authoritative_direct_root_must_still_be_active(self) -> None:
        cases = (
            ("inactive", _thread_summary(status="idle"), None, "不是 active"),
            (
                "wrong-thread",
                _thread_summary(thread_id="thread-2"),
                None,
                "direct-root",
            ),
            (
                "read-failure",
                _thread_summary(),
                RuntimeError("read failed"),
                "read failed",
            ),
        )
        for label, summary, error, expected_text in cases:
            with self.subTest(label=label):
                self.events.clear()
                self.direct_targets.summary = summary
                self.direct_targets.error = error

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn(expected_text, result.text)
                self.assertIn("未发送", result.text)
                self.assertIn("未加入队列", result.text)
                self.assertEqual(
                    self.events,
                    ["resolve", "exclusivity", "direct-read"],
                )
                self.assertEqual(self.adapter.steer_calls, [])

    def test_connection_generation_is_a_pre_send_fence(self) -> None:
        cases = (
            ("read-error", RuntimeError("disconnected"), 7),
            ("zero", None, 0),
            ("bool", None, True),
        )
        for label, error, value in cases:
            with self.subTest(label=label):
                self.events.clear()
                self.adapter.connection_generation_error = error
                self.adapter.connection_generation_value = value

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn("未发送", result.text)
                self.assertIn("未加入队列", result.text)
                self.assertEqual(
                    self.events,
                    ["resolve", "exclusivity", "direct-read", "connection"],
                )
                self.assertEqual(self.adapter.steer_calls, [])

    def test_exact_session_is_rechecked_after_authoritative_reads(self) -> None:
        original = self.binding_runtime.session

        def replace_turn() -> None:
            self.binding_runtime.session = replace(
                original,
                execution=replace(
                    original.execution,
                    current_turn_id="turn-2",
                ),
            )

        self.direct_targets.after_read = replace_turn

        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "more context",
            "message-command",
        )

        self.assertIn("active turn 已变化", result.text)
        self.assertIn("未发送", result.text)
        self.assertIn("未加入队列", result.text)
        self.assertEqual(
            self.events,
            [
                "resolve",
                "exclusivity",
                "direct-read",
                "connection",
                "snapshot",
            ],
        )
        self.assertEqual(self.adapter.steer_calls, [])

    def test_replaced_binding_handle_is_a_known_pre_send_failure(self) -> None:
        self.binding_runtime.snapshot_error = RuntimeError("retired handle")

        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "more context",
            "message-command",
        )

        self.assertIn("binding 已被替换", result.text)
        self.assertIn("未发送", result.text)
        self.assertEqual(self.adapter.steer_calls, [])

    def test_active_observer_can_send_one_exact_text_only_steer(self) -> None:
        session = _session(execution_kind="active_observer")
        self.binding_runtime.session = session

        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "  add this constraint  ",
            "message-command",
        )

        self.assertIn("已将文本补充", result.text)
        self.assertEqual(
            self.events,
            [
                "resolve",
                "exclusivity",
                "direct-read",
                "connection",
                "snapshot",
                "steer",
            ],
        )
        self.assertEqual(self.adapter.connection_generation_calls, [True])
        self.assertEqual(
            self.direct_targets.calls,
            [
                (
                    "thread-1",
                    "thread-1",
                    "向当前 active turn 补充文本",
                )
            ],
        )
        self.assertEqual(
            self.adapter.steer_calls,
            [
                {
                    "thread_id": "thread-1",
                    "expected_turn_id": "turn-1",
                    "input_items": [{"type": "text", "text": "add this constraint"}],
                    "expected_connection_generation": 7,
                }
            ],
        )
        self.assertIs(self.binding_runtime.session, session)

    def test_adapter_pre_send_and_known_rejection_are_no_effect(self) -> None:
        cases = (
            (
                "pre-send",
                CodexRpcPreSendError("turn/steer", RuntimeError("generation changed")),
                "未发送",
            ),
            (
                "known-reject",
                CodexRpcError(
                    "turn/steer",
                    {"code": -32002, "message": "no active turn to steer"},
                ),
                "明确拒绝",
            ),
        )
        for label, error, expected_text in cases:
            with self.subTest(label=label):
                self.adapter.steer_calls.clear()
                self.adapter.steer_error = error

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn(expected_text, result.text)
                self.assertEqual(len(self.adapter.steer_calls), 1)

    def test_transport_timeout_protocol_and_unclassified_failures_are_unknown(
        self,
    ) -> None:
        cases = (
            CodexRpcTransportError(
                "turn/steer",
                {"message": "Codex websocket disconnected"},
            ),
            TimeoutError("Codex request timed out: turn/steer"),
            CodexRpcProtocolError("turn/steer", "missing turnId"),
            RuntimeError("unexpected adapter failure"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                self.adapter.steer_calls.clear()
                self.adapter.steer_error = error

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn("可能已经发送", result.text)
                self.assertIn("请勿自动重试", result.text)
                self.assertEqual(len(self.adapter.steer_calls), 1)

    def test_malformed_or_mismatched_response_is_unknown(self) -> None:
        for response in (
            None,
            {},
            {"turnId": True},
            {"turnId": "turn-2"},
            {"turnId": " turn-1"},
            {"turnId": "turn-1 "},
            {"turn_id": "turn-1"},
        ):
            with self.subTest(response=response):
                self.adapter.steer_calls.clear()
                self.adapter.steer_error = None
                self.adapter.steer_result = response

                result = self.controller.handle_command(
                    "ou-user",
                    "chat-1",
                    "more context",
                    "message-command",
                )

                self.assertIn("可能已经发送", result.text)
                self.assertEqual(len(self.adapter.steer_calls), 1)

    def test_snake_case_turn_id_response_is_unknown(self) -> None:
        self.adapter.steer_result = {"turn_id": "turn-1"}

        result = self.controller.handle_command(
            "ou-user",
            "chat-1",
            "more context",
            "message-command",
        )

        self.assertIn("结果未知", result.text)
        self.assertEqual(len(self.adapter.steer_calls), 1)


if __name__ == "__main__":
    unittest.main()
