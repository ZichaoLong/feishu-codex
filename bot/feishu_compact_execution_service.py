"""Feishu compact-start transaction boundary.

The service owns the complete local-prepare -> root-owner admission -> card
identity -> upstream compact mutation transaction.  It deliberately owns no
runtime state: ``BindingRuntimeManager``, ``TurnExecutionCoordinator``, and
``FeishuRootOperationController`` remain the facts owners behind narrow ports.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from bot.binding_execution_runtime import (
    BindingExecutionRuntimeChanged,
    BindingExecutionRuntimeTransitions,
    PrimeCompactExecutionCommand,
    RecordBindingExecutionStartFailureCommand,
    RetireBindingExecutionCommand,
)
from bot.binding_identity import format_binding_id
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.codex_protocol.client import CodexRpcError
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.feishu_execution_start_contract import FeishuStartDisposition
from bot.feishu_root_operation_contract import FeishuRootOperationToken
from bot.runtime_loop import RuntimeContextGuard


logger = logging.getLogger(__name__)

COMPACT_START_OUTCOME_UNKNOWN_TEXT = (
    "compact 请求已离开 Focus，但上游是否已接受、以及对应 turn 是否已启动不可确认。"
    "Focus 不会自动重试；当前进程会等待 exact turn 或 thread lifecycle 证据后继续。"
)

ChatBindingKey = tuple[str, str]
ExactAdmissionGuard = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class FeishuCompactStartResult:
    """Typed compact-start result before product-level queue projection."""

    accepted: bool
    started: bool
    binding_id: str
    thread_id: str
    reason_code: str = ""
    reason: str = ""
    disposition: FeishuStartDisposition = "blocked_unsettled"

    def as_product_result(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "queued": False,
            "started": self.started,
            "binding_id": self.binding_id,
            "thread_id": self.thread_id,
            "turn_id": "",
            "reason_code": self.reason_code,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class FeishuCompactRuntimePort:
    resolve_session: Callable[[str, str, str], BindingSessionSnapshot]
    writer_denial_text: Callable[[ChatBindingKey, str, str, str], str]
    message_reply_in_thread: Callable[[str], bool]
    group_actor_open_id: Callable[[str], str]


@dataclass(frozen=True, slots=True)
class FeishuCompactRuntimeSnapshot:
    session: BindingSessionSnapshot

    @property
    def binding(self) -> ChatBindingKey:
        return self.session.binding

    @property
    def root_thread_id(self) -> str:
        return self.session.current_thread_id.strip()


class FeishuCompactRuntimeChanged(RuntimeError):
    """The exact binding/root anchor changed during compact preparation."""


class FeishuCompactRuntimeGateway:
    """Bind compact orchestration to exact immutable execution commands."""

    def __init__(
        self,
        *,
        execution_runtime: BindingExecutionRuntimeTransitions,
        ports: FeishuCompactRuntimePort,
    ) -> None:
        if not isinstance(execution_runtime, BindingExecutionRuntimeTransitions):
            raise TypeError("Feishu compact runtime 缺少 typed execution owner。")
        self._execution_runtime = execution_runtime
        self._ports = ports

    def snapshot(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
    ) -> FeishuCompactRuntimeSnapshot:
        return FeishuCompactRuntimeSnapshot(
            session=self._ports.resolve_session(sender_id, chat_id, message_id),
        )

    def prime_execution(
        self,
        snapshot: FeishuCompactRuntimeSnapshot,
        *,
        message_id: str,
    ) -> tuple[FeishuCompactRuntimeSnapshot, bool]:
        reply_in_thread = self._ports.message_reply_in_thread(message_id)
        actor_open_id = self._ports.group_actor_open_id(message_id)
        try:
            primed = self._execution_runtime.prime_compact_execution(
                PrimeCompactExecutionCommand(
                    session=snapshot.session,
                    prompt_message_id=str(message_id or "").strip(),
                    prompt_reply_in_thread=reply_in_thread,
                    actor_open_id=actor_open_id,
                    started_at=time.monotonic(),
                )
            )
        except BindingExecutionRuntimeChanged as exc:
            raise FeishuCompactRuntimeChanged(
                "compact transaction 的 binding/session 已发生变化。"
            ) from exc
        return FeishuCompactRuntimeSnapshot(session=primed), reply_in_thread

    def record_known_failure(
        self,
        snapshot: FeishuCompactRuntimeSnapshot,
        *,
        error_text: str,
    ) -> FeishuCompactRuntimeSnapshot:
        updated = self._execution_runtime.record_start_failure(
            RecordBindingExecutionStartFailureCommand(
                target=BindingExecutionTarget.from_session(snapshot.session),
                error_text=error_text,
            )
        )
        if updated is None:
            raise FeishuCompactRuntimeChanged(
                "compact transaction changed before failure commit"
            )
        return FeishuCompactRuntimeSnapshot(session=updated)

    def retire_execution(
        self,
        snapshot: FeishuCompactRuntimeSnapshot,
    ) -> FeishuCompactRuntimeSnapshot:
        updated = self._execution_runtime.retire_execution(
            RetireBindingExecutionCommand(
                target=BindingExecutionTarget.from_session(snapshot.session),
            )
        )
        if updated is None:
            raise FeishuCompactRuntimeChanged(
                "compact transaction changed before retirement"
            )
        return FeishuCompactRuntimeSnapshot(session=updated)


@dataclass(frozen=True, slots=True)
class FeishuCompactRootOperationPort:
    admit: Callable[..., FeishuRootOperationToken]
    settle_known_failure: Callable[..., None]
    await_start_identity: Callable[[FeishuRootOperationToken], None]
    mark_start_outcome_unknown: Callable[..., None]


@dataclass(frozen=True, slots=True)
class FeishuCompactAdapterPort:
    compact_thread: Callable[[str], Any]
    read_thread: Callable[..., Any]
    operation_start_outcome_unknown: Callable[[Exception], bool]
    is_thread_not_loaded_error: Callable[[Exception], bool]


@dataclass(frozen=True, slots=True)
class FeishuCompactPresentationPort:
    open_initial_execution_page: Callable[..., InitialExecutionPageOpenResult]
    flush_execution_card_for_session: Callable[..., None]
    schedule_mirror_watchdog: Callable[[str, str], None]


@dataclass(frozen=True, slots=True)
class FeishuCompactExecutionPorts:
    runtime: FeishuCompactRuntimePort
    root_operation: FeishuCompactRootOperationPort
    adapter: FeishuCompactAdapterPort
    presentation: FeishuCompactPresentationPort


class FeishuCompactExecutionService:
    """Run one exact Feishu compact start without becoming a state owner."""

    def __init__(
        self,
        *,
        execution_runtime: BindingExecutionRuntimeTransitions,
        ports: FeishuCompactExecutionPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Feishu compact execution 缺少 RuntimeLoop guard。")
        self._ports = ports
        self._runtime = FeishuCompactRuntimeGateway(
            execution_runtime=execution_runtime,
            ports=ports.runtime,
        )
        self._runtime_context_guard = runtime_context_guard

    def start(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        exact_admission_guard: ExactAdmissionGuard | None = None,
        exact_mutation_guard: ExactAdmissionGuard | None = None,
    ) -> FeishuCompactStartResult:
        self._runtime_context_guard()
        runtime = self._runtime.snapshot(
            sender_id,
            chat_id,
            message_id,
        )
        binding_id = format_binding_id(runtime.binding)
        thread_id = runtime.root_thread_id

        if (exact_admission_guard is None) != (exact_mutation_guard is None):
            return self._denied(
                binding_id,
                thread_id,
                "stale_queue_admission",
                "queued compact 缺少完整的 exact queue capability；旧操作已丢弃。",
            )

        if not self._guard_allows(exact_admission_guard):
            return self._denied(
                binding_id,
                thread_id,
                "stale_queue_admission",
                "queued compact 的 binding/root 已变化；旧操作已丢弃。",
            )
        if not thread_id:
            return self._denied(
                binding_id,
                "",
                "compact_denied_no_thread",
                "当前还没有绑定 thread；先执行 `/new`，或直接发送第一条普通消息创建线程。",
            )

        writer_denial = self._ports.runtime.writer_denial_text(
            runtime.binding,
            chat_id,
            thread_id,
            message_id,
        )
        if writer_denial:
            return self._denied(
                binding_id,
                thread_id,
                "compact_denied_by_thread_owner",
                writer_denial,
            )

        # The exact queue receipt is checked again at the outbound root-owner
        # boundary.  A preflight alone cannot prevent an A -> B -> A epoch from
        # allowing stale work to follow the replacement binding.
        if not self._guard_allows(exact_admission_guard):
            return self._denied(
                binding_id,
                thread_id,
                "stale_queue_admission",
                "queued compact 的 binding/root 已变化；旧操作已丢弃。",
            )
        try:
            admission = self._ports.root_operation.admit(
                runtime.binding,
                thread_id,
                chat_id=chat_id,
                message_id=message_id,
                reason="feishu_compact_claimed",
                operation_kind="compact",
            )
        except Exception as exc:
            error_text = f"无法取得当前 submission；未启动 compact：{exc}"
            logger.exception("Feishu compact submission admission 失败")
            return self._denied(
                binding_id,
                thread_id,
                "compact_submission_denied",
                error_text,
                disposition="blocked_unsettled",
            )

        try:
            runtime, reply_in_thread = self._runtime.prime_execution(
                runtime,
                message_id=message_id,
            )
        except Exception as exc:
            logger.exception("准备 compact 本地 execution 失败")
            error_text = f"准备 compact 失败，未向 Codex 发送请求：{exc}"
            owner_settled = self._complete_known_failure(
                sender_id=sender_id,
                chat_id=chat_id,
                runtime=runtime,
                admission=admission,
                error_text=error_text,
                reason="feishu_compact_local_prepare_failed",
                flush_card=False,
            )
            return self._denied(
                binding_id,
                thread_id,
                "compact_local_prepare_failed",
                error_text,
                disposition=self._settled_disposition(owner_settled),
            )

        try:
            page_result = self._ports.presentation.open_initial_execution_page(
                runtime.session,
                message_id,
                reply_in_thread=reply_in_thread,
            )
        except Exception:
            logger.exception("发送 compact execution card 失败")
            page_result = InitialExecutionPageOpenResult(
                status=InitialExecutionPageOpenStatus.REJECTED,
                session=runtime.session,
            )
        if page_result.session is not None:
            runtime = FeishuCompactRuntimeSnapshot(session=page_result.session)
        if page_result.send_unknown and page_result.session is not None:
            error_text = (
                "执行卡片发送结果暂时无法确认；未启动 compact。"
                "为避免重复卡片，当前会话会保留该投递状态等待对账。"
            )
            owner_settled = self._complete_page_send_unknown(
                runtime=runtime,
                admission=admission,
                error_text=error_text,
            )
            return self._denied(
                binding_id,
                thread_id,
                "compact_execution_page_send_unknown",
                error_text,
                disposition=self._settled_disposition(owner_settled),
            )
        if not page_result.active or page_result.session is None:
            error_text = "执行卡片发送失败，未启动 compact；请稍后重试。"
            owner_settled = self._complete_known_failure(
                sender_id=sender_id,
                chat_id=chat_id,
                runtime=runtime,
                admission=admission,
                error_text=error_text,
                reason="feishu_compact_card_send_failed",
                flush_card=False,
            )
            return self._denied(
                binding_id,
                thread_id,
                "compact_execution_card_failed",
                error_text,
                disposition=self._settled_disposition(owner_settled),
            )

        if not self._guard_allows(exact_mutation_guard):
            error_text = (
                "queued compact 的 exact receipt 已失效；未向 Codex 发送请求。"
            )
            owner_settled = self._complete_known_failure(
                sender_id=sender_id,
                chat_id=chat_id,
                runtime=runtime,
                admission=admission,
                error_text=error_text,
                reason="feishu_compact_stale_before_rpc",
                flush_card=True,
            )
            return self._denied(
                binding_id,
                thread_id,
                "stale_queue_admission",
                error_text,
                disposition=self._settled_disposition(owner_settled),
            )

        try:
            self._ports.adapter.compact_thread(thread_id)
        except Exception as exc:
            try:
                outcome_unknown = (
                    self._ports.adapter.operation_start_outcome_unknown(exc)
                )
            except Exception:
                logger.exception(
                    "compact RPC 后无法分类上游结果；按不可确认状态保留"
                )
                self._mark_start_outcome_unknown_safely(
                    sender_id,
                    chat_id,
                    thread_id,
                    admission=admission,
                    reason="feishu_compact_rpc_classification_failed",
                )
                return self._unknown(binding_id, thread_id)
            if outcome_unknown:
                logger.warning(
                    "compact RPC 结果不可确认，保留本地 gate: thread=%s",
                    thread_id[:12],
                    exc_info=True,
                )
                self._mark_start_outcome_unknown_safely(
                    sender_id,
                    chat_id,
                    thread_id,
                    reason="feishu_compact_rpc_outcome_unknown",
                    admission=admission,
                )
                return self._unknown(binding_id, thread_id)

            error_text = self._start_failure_text(thread_id, exc)
            if error_text is None:
                logger.exception("compact 线程失败")
                error_text = f"compact 失败：{exc}"
            owner_settled = self._complete_known_failure(
                sender_id=sender_id,
                chat_id=chat_id,
                runtime=runtime,
                admission=admission,
                error_text=error_text,
                reason="feishu_compact_start_failed",
                flush_card=True,
            )
            return self._denied(
                binding_id,
                thread_id,
                "compact_start_failed",
                error_text,
                disposition=self._settled_disposition(owner_settled),
            )

        try:
            self._ports.root_operation.await_start_identity(admission)
            self._ports.presentation.schedule_mirror_watchdog(sender_id, chat_id)
        except Exception:
            # The RPC was acknowledged, so local recovery failure is an unknown
            # mutation outcome, never a known no-send failure.
            logger.exception(
                "compact ACK 后无法建立 turn identity recovery；按不可确认状态保留"
            )
            self._mark_start_outcome_unknown_safely(
                sender_id,
                chat_id,
                thread_id,
                reason="feishu_compact_ack_local_recovery_failed",
                admission=admission,
            )
            return self._unknown(binding_id, thread_id)

        return FeishuCompactStartResult(
            accepted=True,
            started=True,
            binding_id=binding_id,
            thread_id=thread_id,
            disposition="started",
        )

    def _complete_known_failure(
        self,
        *,
        sender_id: str,
        chat_id: str,
        runtime: FeishuCompactRuntimeSnapshot,
        admission: FeishuRootOperationToken,
        error_text: str,
        reason: str,
        flush_card: bool,
    ) -> bool:
        """Fail one exact compact start without bypassing owner provenance."""

        failed_runtime: FeishuCompactRuntimeSnapshot | None = None
        try:
            failed_runtime = self._runtime.record_known_failure(
                runtime,
                error_text=error_text,
            )
        except Exception:
            logger.exception("无法在 owner settlement 前记录 compact start failure")

        owner_settled = False
        try:
            self._ports.root_operation.settle_known_failure(
                admission,
                reason=reason,
            )
            owner_settled = True
        except Exception:
            logger.exception(
                "无法结算 exact Feishu compact operation token: reason=%s",
                reason,
            )

        if flush_card:
            try:
                self._ports.presentation.flush_execution_card_for_session(
                    (failed_runtime or runtime).session,
                    immediate=True,
                )
            except Exception:
                logger.exception("compact start failure card cleanup failed")

        if not owner_settled:
            return False
        if failed_runtime is None:
            return True
        try:
            # Generic Handler cleanup also releases the interaction lease; the
            # exact root token above is the sole settlement authority.
            self._runtime.retire_execution(failed_runtime)
        except Exception:
            logger.exception("compact start failure anchor cleanup failed")
        return True

    def _complete_page_send_unknown(
        self,
        *,
        runtime: FeishuCompactRuntimeSnapshot,
        admission: FeishuRootOperationToken,
        error_text: str,
    ) -> bool:
        """Settle the unsent compact root while retaining page uncertainty."""

        try:
            self._runtime.record_known_failure(
                runtime,
                error_text=error_text,
            )
        except Exception:
            logger.exception("无法记录 compact initial-page unknown")
        try:
            self._ports.root_operation.settle_known_failure(
                admission,
                reason="feishu_compact_initial_page_send_unknown",
            )
        except Exception:
            logger.exception("无法结算 compact initial-page unknown root token")
            return False
        return True

    def _mark_start_outcome_unknown_safely(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        admission: FeishuRootOperationToken,
        reason: str,
    ) -> bool:
        try:
            self._ports.root_operation.mark_start_outcome_unknown(
                sender_id,
                chat_id,
                thread_id,
                reason=reason,
                admission=admission,
            )
        except Exception:
            logger.exception(
                "compact submission unknown settlement 失败；禁止自动重试: reason=%s",
                reason,
            )
            return False
        return True

    def _start_failure_text(self, thread_id: str, exc: Exception) -> str | None:
        if not (
            self._ports.adapter.is_thread_not_loaded_error(exc)
            or self._is_compact_thread_not_found_error(exc)
        ):
            return None
        try:
            self._ports.adapter.read_thread(thread_id, include_turns=False)
        except Exception as read_exc:
            logger.warning(
                "compact 启动失败后无法确认 thread 状态: "
                "thread=%s compact_error=%s read_error=%s",
                thread_id[:12],
                exc,
                read_exc,
            )
            return (
                "当前 backend 无法直接 compact 这条 thread，且暂时无法确认它只是未加载，"
                "还是持久化记录已不可读。\n"
                "可稍后重试，或先执行 `/attach`，或直接发送一条普通消息尝试恢复。"
            )
        return (
            "当前 thread 尚未加载到本实例 backend，无法 compact。\n"
            "先执行 `/attach`，或直接发送一条普通消息恢复该 thread。"
        )

    @staticmethod
    def _guard_allows(guard: ExactAdmissionGuard | None) -> bool:
        if guard is None:
            return True
        try:
            return bool(guard())
        except Exception:
            logger.exception("queued compact exact admission guard 失败；按 stale 丢弃")
            return False

    @staticmethod
    def _is_compact_thread_not_found_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").lower()
        return message.startswith("thread not found:")

    @staticmethod
    def _denied(
        binding_id: str,
        thread_id: str,
        reason_code: str,
        reason: str,
        *,
        disposition: FeishuStartDisposition = "known_no_effect_settled",
    ) -> FeishuCompactStartResult:
        return FeishuCompactStartResult(
            accepted=False,
            started=False,
            binding_id=binding_id,
            thread_id=thread_id,
            reason_code=reason_code,
            reason=reason,
            disposition=disposition,
        )

    @staticmethod
    def _settled_disposition(owner_settled: bool) -> FeishuStartDisposition:
        if owner_settled:
            return "known_no_effect_settled"
        return "blocked_unsettled"

    @staticmethod
    def _unknown(binding_id: str, thread_id: str) -> FeishuCompactStartResult:
        return FeishuCompactStartResult(
            accepted=False,
            started=False,
            binding_id=binding_id,
            thread_id=thread_id,
            reason_code="compact_start_outcome_unknown",
            reason=COMPACT_START_OUTCOME_UNKNOWN_TEXT,
            disposition="blocked_unsettled",
        )
