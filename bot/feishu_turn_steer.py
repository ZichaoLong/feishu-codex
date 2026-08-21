"""Send one explicit Feishu text contribution to an exact active turn."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from bot.adapters.base import ThreadSummary
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.cards import CommandResult
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.feishu_command_syntax import feishu_visible_command_syntax
from bot.reason_codes import ReasonedCheck
from bot.runtime_state import BACKEND_THREAD_STATUS_ACTIVE


logger = logging.getLogger(__name__)

_USAGE_TEXT = (
    f"用法：`{feishu_visible_command_syntax('/steer <文本>')}`\n"
    "只把这段纯文本补充到当前已镜像的 active turn；不会排队到下一轮。"
)
_NO_SEND_TEXT = "未发送，也未加入队列。"
_UNKNOWN_TEXT = (
    "本次 steer 可能已经发送到 Codex，但 Focus 没有收到可核对的结果，结果未知；"
    "请勿自动重试，以免重复发送。"
)


class _BindingRuntime(Protocol):
    def resolve_session(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> BindingSessionSnapshot: ...

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...


class _ThreadAccessPolicy(Protocol):
    def all_mode_thread_exclusivity_violation_check(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> ReasonedCheck: ...


class _DirectThreadTargets(Protocol):
    def read_direct_thread_summary_authoritatively(
        self,
        thread_id: str,
        *,
        original_arg: str,
        operation: str,
    ) -> ThreadSummary: ...


class _TurnSteerAdapter(Protocol):
    def connection_generation(
        self,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
    ) -> int: ...

    def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[dict[str, str]],
        client_user_message_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> dict[str, Any]: ...


class FeishuTurnSteerController:
    """Own the bounded pre-send checks for explicit Feishu ``turn/steer``."""

    def __init__(
        self,
        *,
        lock: Any,
        adapter: _TurnSteerAdapter,
        binding_runtime: _BindingRuntime,
        access_policy: _ThreadAccessPolicy,
        direct_thread_targets: _DirectThreadTargets,
    ) -> None:
        self._lock = lock
        self._adapter = adapter
        self._binding_runtime = binding_runtime
        self._access_policy = access_policy
        self._direct_thread_targets = direct_thread_targets

    def handle_command(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
    ) -> CommandResult:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return CommandResult(text=_USAGE_TEXT)

        session = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        eligibility_error = self._eligibility_error(session)
        if eligibility_error:
            return CommandResult(text=self._pre_send_failure(eligibility_error))
        target = BindingExecutionTarget.from_session(session)

        exclusivity = self._access_policy.all_mode_thread_exclusivity_violation_check(
            chat_id,
            target.expected_thread_id,
            message_id=message_id,
        )
        if not exclusivity.allowed:
            reason = exclusivity.reason_text or "当前 thread 不允许从本会话 steer。"
            return CommandResult(text=self._pre_send_failure(reason))

        pre_send_error = self._authoritative_active_error(target)
        if pre_send_error:
            return CommandResult(text=self._pre_send_failure(pre_send_error))

        try:
            connection_generation = self._adapter.connection_generation(
                require_existing_connection=True,
            )
        except Exception as exc:
            return CommandResult(
                text=self._pre_send_failure(f"无法核对当前 Codex 连接：{exc}")
            )
        if type(connection_generation) is not int or connection_generation <= 0:
            return CommandResult(
                text=self._pre_send_failure("无法核对当前 Codex 连接 generation。")
            )

        try:
            with self._lock:
                current = self._binding_runtime.session_snapshot_locked(target.handle)
                target_still_eligible = self._target_still_eligible(
                    target,
                    current,
                )
        except Exception as exc:
            return CommandResult(
                text=self._pre_send_failure(f"当前 binding 已被替换：{exc}")
            )
        if not target_still_eligible:
            return CommandResult(
                text=self._pre_send_failure(
                    "当前 binding 或 active turn 已变化，请重试。"
                )
            )

        try:
            response = self._adapter.steer_turn(
                thread_id=target.expected_thread_id,
                expected_turn_id=target.expected_turn_id,
                input_items=[{"type": "text", "text": normalized_text}],
                expected_connection_generation=connection_generation,
            )
        except CodexRpcPreSendError as exc:
            return CommandResult(
                text=self._pre_send_failure(f"本次 steer 在 dispatch 前失败：{exc}")
            )
        except (CodexRpcTransportError, CodexRpcProtocolError, TimeoutError):
            return CommandResult(text=_UNKNOWN_TEXT)
        except CodexRpcError as exc:
            return CommandResult(
                text=f"Codex 明确拒绝了本次 steer；文本未加入当前 turn：{exc}"
            )
        except Exception:
            logger.exception(
                "Feishu turn/steer failed with an unclassified outcome: "
                "thread=%s turn=%s",
                target.expected_thread_id,
                target.expected_turn_id,
            )
            return CommandResult(text=_UNKNOWN_TEXT)

        response_turn_id = self._response_turn_id(response)
        if response_turn_id != target.expected_turn_id:
            logger.warning(
                "Feishu turn/steer returned an unverified turn id: "
                "thread=%s expected=%s actual=%s",
                target.expected_thread_id,
                target.expected_turn_id,
                response_turn_id or "<missing>",
            )
            return CommandResult(text=_UNKNOWN_TEXT)
        return CommandResult(
            text=f"已将文本补充到当前 turn `{target.expected_turn_id}`。"
        )

    @staticmethod
    def _eligibility_error(session: BindingSessionSnapshot) -> str:
        if not session.active:
            return "当前 binding 未激活。"
        if not session.thread.feishu_runtime_attached:
            return "当前飞书会话未附着到线程。"
        thread_id = session.current_thread_id
        turn_id = session.execution.current_turn_id
        if not thread_id.strip() or thread_id != thread_id.strip():
            return "当前会话没有可核对的 thread。"
        if not session.running or not turn_id.strip() or turn_id != turn_id.strip():
            return "当前会话没有可核对的 active turn。"
        if session.execution.current_execution_kind.strip() == "compact":
            return "当前 active turn 正在 compact，不能 steer。"
        return ""

    def _authoritative_active_error(
        self,
        target: BindingExecutionTarget,
    ) -> str:
        try:
            summary = (
                self._direct_thread_targets.read_direct_thread_summary_authoritatively(
                    target.expected_thread_id,
                    original_arg=target.expected_thread_id,
                    operation="向当前 active turn 补充文本",
                )
            )
        except Exception as exc:
            return f"无法确认当前 thread 可直接 steer：{exc}"
        if (
            not isinstance(summary, ThreadSummary)
            or summary.thread_id != target.expected_thread_id
        ):
            return "无法确认当前 direct-root thread。"
        if summary.status != BACKEND_THREAD_STATUS_ACTIVE:
            return "权威读取显示当前 thread 已不是 active。"
        return ""

    @staticmethod
    def _pre_send_failure(reason: str) -> str:
        return f"{reason}\n{_NO_SEND_TEXT}"

    @classmethod
    def _target_still_eligible(
        cls,
        target: BindingExecutionTarget,
        current: BindingSessionSnapshot,
    ) -> bool:
        return bool(target.matches(current) and not cls._eligibility_error(current))

    @staticmethod
    def _response_turn_id(response: object) -> str:
        if not isinstance(response, dict):
            return ""
        value = response.get("turnId")
        if type(value) is not str or not value or value != value.strip():
            return ""
        return value
