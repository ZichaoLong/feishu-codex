from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeAlias

from bot.adapters.base import (
    ThreadSnapshot,
    ThreadSummary,
    TurnInputItem,
)
from bot.binding_execution_runtime import (
    BindingExecutionRuntimeChanged,
    BindingExecutionRuntimeTransitions,
    PrimePromptExecutionCommand,
    RecordBindingExecutionStartFailureCommand,
    RetireBindingExecutionCommand,
    UpdateBindingCancelCommand,
)
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingSessionSnapshot,
)
from bot.cards import build_markdown_card
from bot.exception_chain import iter_exception_chain
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.feishu_outbound import FeishuOutboundResult
from bot.feishu_execution_start_contract import (
    FeishuOperationSettlement,
    FeishuStartDisposition,
    PromptTurnStartResult,
)
from bot.feishu_prompt_failure_presentation import (
    FeishuPromptFailurePresentation,
    FeishuPromptFailurePresentationPorts,
)
from bot.feishu_prompt_operation_settlement import (
    FeishuPromptOperationSettlementPorts,
    FeishuPromptOperationSettlementService,
)
from bot.feishu_root_operation_contract import (
    FeishuPromptInterruptCandidateClaim,
    FeishuRootContinuationToken,
    FeishuRootOperationToken,
)
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    BACKEND_THREAD_STATUS_IDLE,
)
from bot.thread_runtime_authority import (
    ThreadResumeLocalCommitFailed,
    ThreadResumePreSendGuardRejected,
    ThreadResumeSettlementError,
)
from bot.reason_codes import ReasonedCheck

logger = logging.getLogger(__name__)

ChatBindingKey: TypeAlias = tuple[str, str]

START_FAILURE_PREPARE_THREAD = "prepare_thread_failed"
START_FAILURE_INTERACTION_DENIED = "interaction_denied"
START_FAILURE_SHARING_DENIED = "sharing_denied"
START_FAILURE_EXECUTION_CARD = "execution_card_send_failed"
START_FAILURE_TURN_START = "turn_start_failed"
START_FAILURE_STALE_QUEUE_ADMISSION = "stale_queue_admission"
class _StaleQueueMutation(RuntimeError):
    """The exact queue capability was revoked before an upstream mutation."""


class _ThreadAccessPolicy(Protocol):
    def prompt_write_denial_text(
        self,
        binding: ChatBindingKey,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str: ...

    def all_mode_thread_exclusivity_violation(
        self,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
        current_chat_mode: str | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ThreadSessionPort:
    resolve_session: Callable[[str, str, str], BindingSessionSnapshot]
    clear_thread_binding: Callable[..., None]
    reattach_bound_thread: Callable[..., str]
    create_and_bind_thread: Callable[..., ThreadSnapshot]
    message_reply_in_thread: Callable[[str], bool]
    group_actor_open_id: Callable[[str], str]
    access_policy: _ThreadAccessPolicy
    detached_runtime_attach_check: Callable[[str], ReasonedCheck]


@dataclass(frozen=True, slots=True)
class PresentationPort:
    claim_reserved_execution_card: Callable[[str], str]
    patch_message: Callable[[str, str, str], FeishuOutboundResult]
    open_initial_execution_page: Callable[..., InitialExecutionPageOpenResult]
    flush_execution_card_for_session: Callable[..., None]
    schedule_mirror_watchdog: Callable[[str, str], None]
    reconcile_execution_snapshot: Callable[..., bool]
    refresh_terminal_card: Callable[[BindingSessionSnapshot], bool]
    finalize_execution: Callable[[BindingSessionSnapshot], Any]
    mark_runtime_degraded: Callable[..., None]
    reply_text: Callable[..., None]
    mirror_watchdog_seconds: Callable[[], float]


@dataclass(frozen=True, slots=True)
class InteractionPort:
    runtime_recovery_reason: Callable[[Exception], str]
    operation_outcome_unknown: Callable[[Exception], bool]
    is_turn_thread_not_found_error: Callable[[Exception], bool]
    is_thread_not_found_error: Callable[[Exception], bool]
    is_pre_send_error: Callable[[Exception], bool]
    is_turn_interrupt_rejected_error: Callable[[Exception], bool]
    start_turn: Callable[..., dict[str, Any]]
    interrupt_running_turn: Callable[..., None]
    finalize_input_items: Callable[
        [str, str, list[dict[str, Any]]],
        list[dict[str, Any]],
    ]


class _AdmitFeishuRootOperation(Protocol):
    def __call__(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
        *,
        chat_id: str,
        message_id: str = "",
        reason: str,
        operation_kind: str = "mutation",
    ) -> FeishuRootOperationToken: ...


@dataclass(frozen=True, slots=True)
class FeishuRootOperationPort:
    """Typed causal owner used by one prompt admission from start to outcome."""

    admit: _AdmitFeishuRootOperation
    arm_continuation: Callable[..., FeishuRootContinuationToken]
    await_start_identity: Callable[[FeishuRootOperationToken], None]
    accept_prompt_start: Callable[[FeishuRootOperationToken, str], None]
    claim_prompt_interrupt_candidate: Callable[
        [ChatBindingKey, str], FeishuPromptInterruptCandidateClaim | None
    ]
    consume_prompt_interrupt_candidate: Callable[
        [FeishuPromptInterruptCandidateClaim], bool
    ]
    restore_prompt_interrupt_candidate_after_pre_send: Callable[..., bool]
    settle_known_failure: Callable[..., None]
    settle_known_mutation: Callable[..., None]
    acknowledge_continuing: Callable[..., None]
    mark_outcome_unknown: Callable[..., None]
    continuation_may_autostart: Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class PromptTurnEntryPorts:
    session: ThreadSessionPort
    root_operation: FeishuRootOperationPort
    interaction: InteractionPort
    presentation: PresentationPort


class PromptTurnEntryController:
    def __init__(
        self,
        *,
        execution_runtime: BindingExecutionRuntimeTransitions,
        ports: PromptTurnEntryPorts,
    ) -> None:
        if not isinstance(execution_runtime, BindingExecutionRuntimeTransitions):
            raise TypeError("prompt turn entry requires typed execution runtime")
        self._execution_runtime = execution_runtime
        session = ports.session
        root_operation = ports.root_operation
        interaction = ports.interaction
        presentation = ports.presentation
        self._resolve_session = session.resolve_session
        self._clear_thread_binding = session.clear_thread_binding
        self._reattach_bound_thread = session.reattach_bound_thread
        self._create_and_bind_thread = session.create_and_bind_thread
        self._message_reply_in_thread = session.message_reply_in_thread
        self._group_actor_open_id = session.group_actor_open_id
        self._access_policy = session.access_policy
        self._detached_runtime_attach_check = session.detached_runtime_attach_check
        self._claim_reserved_execution_card = presentation.claim_reserved_execution_card
        self._patch_message = presentation.patch_message
        self._open_initial_execution_page = presentation.open_initial_execution_page
        self._flush_execution_card_for_session = (
            presentation.flush_execution_card_for_session
        )
        self._schedule_mirror_watchdog = presentation.schedule_mirror_watchdog
        self._reconcile_execution_snapshot = presentation.reconcile_execution_snapshot
        self._refresh_terminal_card = presentation.refresh_terminal_card
        self._finalize_execution = presentation.finalize_execution
        self._mark_runtime_degraded = presentation.mark_runtime_degraded
        self._runtime_recovery_reason = interaction.runtime_recovery_reason
        self._operation_outcome_unknown = interaction.operation_outcome_unknown
        self._is_turn_thread_not_found_error = (
            interaction.is_turn_thread_not_found_error
        )
        self._is_thread_not_found_error = interaction.is_thread_not_found_error
        self._is_pre_send_error = interaction.is_pre_send_error
        self._is_turn_interrupt_rejected_error = (
            interaction.is_turn_interrupt_rejected_error
        )
        self._start_turn = interaction.start_turn
        self._interrupt_running_turn = interaction.interrupt_running_turn
        self._reply_text = presentation.reply_text
        self._failure_presentation = FeishuPromptFailurePresentation(
            FeishuPromptFailurePresentationPorts(
                render_start_failure=self.render_start_failure,
                reply_text=self._reply_text,
                message_reply_in_thread=session.message_reply_in_thread,
            )
        )
        self._mirror_watchdog_seconds = presentation.mirror_watchdog_seconds
        self._admit_root_operation = root_operation.admit
        self._arm_root_continuation = root_operation.arm_continuation
        self._await_root_start_identity = root_operation.await_start_identity
        self._accept_prompt_start = root_operation.accept_prompt_start
        self._claim_prompt_interrupt_candidate = (
            root_operation.claim_prompt_interrupt_candidate
        )
        self._consume_prompt_interrupt_candidate = (
            root_operation.consume_prompt_interrupt_candidate
        )
        self._restore_prompt_interrupt_candidate_after_pre_send = (
            root_operation.restore_prompt_interrupt_candidate_after_pre_send
        )
        self._mark_root_outcome_unknown = root_operation.mark_outcome_unknown
        self._operation_settlement = FeishuPromptOperationSettlementService(
            FeishuPromptOperationSettlementPorts(
                operation_outcome_unknown=interaction.operation_outcome_unknown,
                settle_known_failure=root_operation.settle_known_failure,
                settle_known_mutation=root_operation.settle_known_mutation,
                acknowledge_continuing=root_operation.acknowledge_continuing,
                mark_outcome_unknown=root_operation.mark_outcome_unknown,
            )
        )
        self._continuation_may_autostart = root_operation.continuation_may_autostart
        self._finalize_input_items = interaction.finalize_input_items

    def preflight_group_prompt(
        self, sender_id: str, chat_id: str, *, message_id: str = ""
    ) -> bool:
        if self.handle_running_prompt(sender_id, chat_id, "", message_id=message_id):
            return False
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            return True
        denial_text = self._access_policy.prompt_write_denial_text(
            runtime.binding,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if not denial_text:
            return True
        self._failure_presentation.reply_routed(
            chat_id,
            denial_text,
            message_id=message_id,
        )
        return False

    def render_start_failure(self, *, chat_id: str, message_id: str, text: str) -> None:
        reserved_card_id = self._claim_reserved_execution_card(message_id)
        if reserved_card_id:
            card = build_markdown_card("Codex 启动失败", text, template="red")
            result = self._patch_message(
                chat_id,
                reserved_card_id,
                json.dumps(card, ensure_ascii=False),
            )
            if result.ok:
                return
            if not result.safe_to_fallback:
                return
        self._reply_text(
            chat_id,
            text,
            message_id=message_id,
            reply_in_thread=self._message_reply_in_thread(message_id),
        )

    def ensure_thread(
        self, sender_id: str, chat_id: str, *, message_id: str = ""
    ) -> str:
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        if runtime.current_thread_id:
            return runtime.current_thread_id
        snapshot = self._create_and_bind_thread(
            sender_id,
            chat_id,
            message_id=message_id,
            cwd=runtime.working_dir,
            model=runtime.model or None,
            approval_policy=runtime.approval_policy or None,
            permissions_profile_id=runtime.permissions_profile_id or None,
        )
        return snapshot.summary.thread_id

    def resume_bound_thread(
        self,
        sender_id: str,
        chat_id: str,
        *,
        retain_on_local_failure: bool,
        message_id: str = "",
        exact_mutation_guard: Callable[[], bool] | None = None,
    ) -> str:
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            raise RuntimeError("当前没有可恢复的线程绑定")
        summary = ThreadSummary(
            thread_id=thread_id,
            cwd=runtime.working_dir,
            name=runtime.current_thread_title,
            preview=runtime.current_thread_title,
            created_at=0,
            updated_at=0,
            source="appServer",
            status=BACKEND_THREAD_STATUS_IDLE,
        )
        return self._reattach_bound_thread(
            sender_id,
            chat_id,
            thread_id,
            original_arg=thread_id,
            summary=summary,
            retain_on_local_failure=retain_on_local_failure,
            message_id=message_id,
            exact_mutation_guard=exact_mutation_guard,
        )

    def ensure_binding_runtime_attached(
        self,
        sender_id: str,
        chat_id: str,
        *,
        retain_on_local_failure: bool,
        message_id: str = "",
        exact_mutation_guard: Callable[[], bool] | None = None,
    ) -> str:
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        thread_id = runtime.current_thread_id.strip()
        if not thread_id:
            raise RuntimeError("当前没有可恢复的线程绑定")
        if runtime.thread.feishu_runtime_attached:
            return thread_id
        return self.resume_bound_thread(
            sender_id,
            chat_id,
            retain_on_local_failure=retain_on_local_failure,
            message_id=message_id,
            exact_mutation_guard=exact_mutation_guard,
        )

    def handle_running_prompt(
        self, sender_id: str, chat_id: str, text: str, *, message_id: str = ""
    ) -> bool:
        del text
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        if not runtime.running:
            return False
        thread_id = runtime.current_thread_id.strip()
        turn_id = runtime.execution.current_turn_id.strip()
        last_runtime_event_at = runtime.execution.last_runtime_event_at
        if (
            thread_id
            and last_runtime_event_at
            and (
                time.monotonic() - last_runtime_event_at
                >= self._mirror_watchdog_seconds()
            )
        ):
            self._reconcile_execution_snapshot(
                sender_id,
                chat_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            if not self._resolve_session(sender_id, chat_id, message_id).running:
                return False
        self._failure_presentation.reply(
            chat_id,
            "当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            message_id=message_id,
        )
        return True

    def handle_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        input_items: list[TurnInputItem] | tuple[TurnInputItem, ...] | None = None,
    ) -> bool:
        if self.handle_running_prompt(sender_id, chat_id, text, message_id=message_id):
            return False
        return self.start_prompt_turn(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            input_items=input_items,
        )

    def start_prompt_turn(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[TurnInputItem] | tuple[TurnInputItem, ...] | None = None,
        surface_failures: bool = True,
    ) -> bool:
        return self.start_prompt_turn_result(
            sender_id,
            chat_id,
            text,
            message_id=message_id,
            actor_open_id=actor_open_id,
            input_items=input_items,
            surface_failures=surface_failures,
        ).started

    @staticmethod
    def _expected_queue_target_matches(
        resolved: BindingSessionSnapshot,
        current_root_thread_id: str,
        *,
        expected_binding: ChatBindingKey | None,
        expected_root_thread_id: str,
    ) -> bool:
        expected_root = str(expected_root_thread_id or "").strip()
        if expected_binding is None:
            return not expected_root
        if not expected_root:
            return False
        return bool(
            resolved.binding == expected_binding
            and str(current_root_thread_id or "").strip() == expected_root
        )

    @staticmethod
    def _stale_queue_admission_result(
        expected_root_thread_id: str,
        *,
        disposition: FeishuStartDisposition = "known_no_effect_settled",
    ) -> PromptTurnStartResult:
        return PromptTurnStartResult(
            started=False,
            thread_id=str(expected_root_thread_id or "").strip(),
            reason_code=START_FAILURE_STALE_QUEUE_ADMISSION,
            reason_text=(
                "queued prompt 的 binding/root 已变化；旧输入已按 fail-closed 丢弃。"
            ),
            disposition=disposition,
        )

    @staticmethod
    def _guard_allows(guard: Callable[[], bool] | None) -> bool:
        if guard is None:
            return True
        try:
            return bool(guard())
        except Exception:
            logger.exception("queued prompt exact guard 失败；按 stale 丢弃")
            return False

    def start_prompt_turn_result(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[TurnInputItem] | tuple[TurnInputItem, ...] | None = None,
        surface_failures: bool = True,
        expected_binding: ChatBindingKey | None = None,
        expected_root_thread_id: str = "",
        exact_admission_guard: Callable[[], bool] | None = None,
        exact_mutation_guard: Callable[[], bool] | None = None,
    ) -> PromptTurnStartResult:
        effective_input_items = (
            list(input_items)
            if input_items is not None
            else [{"type": "text", "text": text}]
        )
        expected_root = str(expected_root_thread_id or "").strip()
        queued_target_expected = expected_binding is not None or bool(expected_root)
        failure = self._failure_presentation.scope(
            chat_id=chat_id,
            message_id=message_id,
            surface=surface_failures,
            pre_owner_reason_code=START_FAILURE_PREPARE_THREAD,
        )

        if queued_target_expected and (
            expected_binding is None
            or not expected_root
            or exact_admission_guard is None
            or exact_mutation_guard is None
        ):
            return self._stale_queue_admission_result(expected_root)
        try:
            runtime = self._resolve_session(sender_id, chat_id, message_id)
            chat_binding_key = runtime.binding
        except Exception as exc:
            return failure.pre_owner(exc)
        if not self._expected_queue_target_matches(
            runtime,
            runtime.current_thread_id,
            expected_binding=expected_binding,
            expected_root_thread_id=expected_root_thread_id,
        ) or (not self._guard_allows(exact_admission_guard)):
            return self._stale_queue_admission_result(
                expected_root_thread_id,
            )
        detached_thread_id = runtime.current_thread_id.strip()
        attach_pending = False
        operation_token: FeishuRootOperationToken | None = None
        continuation_receipt: FeishuRootContinuationToken | None = None
        resume_acknowledged = False
        resume_continuation_risk = False
        if detached_thread_id and not runtime.thread.feishu_runtime_attached:
            attach_pending = True
            try:
                denial_text = self._access_policy.prompt_write_denial_text(
                    chat_binding_key,
                    chat_id,
                    detached_thread_id,
                    message_id=message_id,
                )
            except Exception as exc:
                return failure.pre_owner(
                    exc,
                    thread_id=detached_thread_id,
                )
            if denial_text:
                return failure.known_denial(denial_text)
            try:
                attach_check = self._detached_runtime_attach_check(detached_thread_id)
            except Exception as exc:
                return failure.pre_owner(
                    exc,
                    thread_id=detached_thread_id,
                )
            if not attach_check.allowed:
                return failure.known_denial(
                    attach_check.reason_text,
                    thread_id=detached_thread_id,
                    reason_code=attach_check.reason_code,
                )
        try:
            thread_id = self.ensure_thread(sender_id, chat_id, message_id=message_id)
        except Exception as exc:
            return failure.pre_owner(
                exc,
                thread_id=detached_thread_id,
            )

        try:
            all_mode_exclusivity_violation = (
                self._access_policy.all_mode_thread_exclusivity_violation(
                    chat_id,
                    thread_id,
                    message_id=message_id,
                )
            )
        except Exception as exc:
            return failure.pre_owner(
                exc,
                thread_id=thread_id,
            )
        if all_mode_exclusivity_violation:
            return failure.known_denial(
                all_mode_exclusivity_violation,
                thread_id=thread_id,
                reason_code=START_FAILURE_SHARING_DENIED,
            )
        # One typed admission acquires the exact cross-surface submission
        # lease.  The stored binding is synchronized only after that token is
        # returned; no retained root record participates in this boundary.
        try:
            fresh_runtime = self._resolve_session(
                sender_id,
                chat_id,
                message_id,
            )
            fresh_root_thread_id = fresh_runtime.current_thread_id
        except Exception as exc:
            return failure.pre_owner(
                exc,
                thread_id=thread_id,
            )
        if (
            not self._expected_queue_target_matches(
                fresh_runtime,
                fresh_root_thread_id,
                expected_binding=expected_binding,
                expected_root_thread_id=expected_root_thread_id,
            )
            or fresh_runtime.binding != chat_binding_key
            or (fresh_root_thread_id.strip() != thread_id)
            or (not self._guard_allows(exact_admission_guard))
        ):
            return self._stale_queue_admission_result(
                expected_root_thread_id,
            )
        try:
            operation_token = self._admit_root_operation(
                chat_binding_key,
                thread_id,
                chat_id=chat_id,
                message_id=message_id,
                reason="feishu_prompt_claimed",
                operation_kind="prompt",
            )
            fresh_runtime = self._execution_runtime.persist_session(fresh_runtime)
        except Exception as exc:
            settlement = FeishuOperationSettlement(
                owner_settled=False,
                disposition="blocked_unsettled",
            )
            if operation_token is not None:
                settlement = self._operation_settlement.settle_known_failure(
                    operation_token,
                    reason="feishu_prompt_binding_sync_failed",
                )
            error_text = f"无法取得当前 submission；未启动 Codex：{exc}"
            logger.exception("Feishu submission admission 失败")
            return failure.settled_failure(
                error_text,
                thread_id=thread_id,
                reason_code=START_FAILURE_INTERACTION_DENIED,
                disposition=settlement.disposition,
                routed=True,
            )
        if not isinstance(operation_token, FeishuRootOperationToken):
            raise TypeError("Feishu root-operation admission 未返回 typed token。")

        # A detached binding's `thread/resume` is an app-server mutation, not
        # a passive subscription: an active persisted goal may start before
        # its resume ACK.  The submission lease and process-local continuation
        # receipt therefore exist before resume and last only until exact
        # lifecycle evidence identifies a turn or proves the root inactive.
        if attach_pending:
            try:
                resume_continuation_risk = self._continuation_may_autostart(thread_id)
                if resume_continuation_risk:
                    continuation_receipt = self._arm_continuation_exact(
                        operation_token,
                        reason="feishu_prompt_resume_prestart",
                    )
            except Exception as exc:
                # The resume write has not been attempted.  Do not classify a
                # local preparation failure as an unknown app-server mutation;
                # settle only this exact admission before any resume write.
                settlement = self._operation_settlement.settle_known_failure(
                    operation_token,
                    reason="feishu_prompt_prestart_fence_failed",
                )
                logger.exception("无法在 Feishu resume 前记录 continuation receipt")
                error_text = f"准备线程失败：{exc}"
                return failure.settled_failure(
                    error_text,
                    thread_id=thread_id,
                    reason_code=START_FAILURE_PREPARE_THREAD,
                    disposition=settlement.disposition,
                )
        if not self._guard_allows(exact_mutation_guard):
            settlement = self._operation_settlement.settle_known_failure(
                operation_token,
                reason="feishu_prompt_stale_before_resume",
            )
            return self._stale_queue_admission_result(
                expected_root_thread_id,
                disposition=settlement.disposition,
            )
        try:
            thread_id = self.ensure_binding_runtime_attached(
                sender_id,
                chat_id,
                retain_on_local_failure=resume_continuation_risk,
                message_id=message_id,
                exact_mutation_guard=exact_mutation_guard,
            )
            resume_acknowledged = attach_pending
        except Exception as exc:
            settlement = self._settle_resume_operation_after_failure(
                operation_token,
                exc,
                reason="feishu_prompt_resume_failed",
                continuation_receipt=continuation_receipt,
            )
            if isinstance(exc, ThreadResumePreSendGuardRejected):
                return self._stale_queue_admission_result(
                    expected_root_thread_id,
                    disposition=settlement.disposition,
                )
            logger.exception("恢复已声明的线程失败")
            error_text = f"准备线程失败：{exc}"
            if surface_failures:
                self._failure_presentation.render(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=error_text,
                )
            return PromptTurnStartResult(
                started=False,
                thread_id=thread_id,
                reason_code=START_FAILURE_PREPARE_THREAD,
                reason_text=error_text,
                disposition=settlement.disposition,
            )
        try:
            start_session = self._resolve_session(
                sender_id,
                chat_id,
                message_id,
            )
        except Exception as exc:
            settlement = self._operation_settlement.finish_without_turn(
                operation_token,
                reason="feishu_prompt_start_session_capture_failed",
                known_mutation=resume_acknowledged,
                retain_continuing=(
                    resume_acknowledged and continuation_receipt is not None
                ),
            )
            return failure.settled_failure(
                f"准备线程失败：{exc}",
                thread_id=thread_id,
                reason_code=START_FAILURE_PREPARE_THREAD,
                disposition=settlement.disposition,
            )
        if (
            start_session.binding != chat_binding_key
            or start_session.current_thread_id.strip() != thread_id
            or not self._guard_allows(exact_mutation_guard)
        ):
            settlement = self._operation_settlement.finish_without_turn(
                operation_token,
                reason="feishu_prompt_stale_before_local_prime",
                known_mutation=resume_acknowledged,
                retain_continuing=(
                    resume_acknowledged and continuation_receipt is not None
                ),
            )
            return self._stale_queue_admission_result(
                expected_root_thread_id,
                disposition=settlement.disposition,
            )

        try:
            effective_input_items = self._finalize_input_items(
                thread_id,
                start_session.model.strip(),
                [dict(item) for item in effective_input_items],
            )
        except Exception:
            # A media-policy failure is not permission to leak an internal
            # candidate into the upstream protocol.  Keep ordinary typed
            # inputs so the controlled local-path text still reaches Codex.
            logger.exception(
                "Feishu native-media authorization failed; using path-only input: thread=%s",
                thread_id[:12],
            )
            effective_input_items = [
                dict(item)
                for item in effective_input_items
                if str(item.get("type", "") or "")
                in {"text", "image", "localImage", "audio", "localAudio"}
            ]

        prompt_reply_in_thread = False
        card_id = ""
        primed_session: BindingSessionSnapshot | None = None
        execution_session: BindingSessionSnapshot | None = None
        page_result: InitialExecutionPageOpenResult | None = None
        try:
            prompt_reply_in_thread = self._message_reply_in_thread(message_id)
            primed_session = self._execution_runtime.prime_prompt_execution(
                PrimePromptExecutionCommand(
                    session=start_session,
                    prompt_message_id=str(message_id or "").strip(),
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    actor_open_id=(
                        str(actor_open_id or "").strip()
                        or self._group_actor_open_id(message_id)
                    ),
                    started_at=time.monotonic(),
                    awaiting_attach_status_settle=attach_pending,
                )
            )
            page_result = self._open_prompt_execution_page(
                primed_session,
                message_id=message_id,
                reply_in_thread=prompt_reply_in_thread,
            )
            card_id = page_result.message_id
            execution_session = page_result.session
            if (
                page_result.status is InitialExecutionPageOpenStatus.STALE
                or execution_session is None
            ):
                raise BindingExecutionRuntimeChanged(
                    "prompt execution changed during initial page commit"
                )
        except Exception:
            logger.exception("准备 Feishu prompt execution card 失败")
            error_text = "执行卡片发送失败，未启动 Codex；请稍后重试。"
            settlement, _local_cleanup_succeeded = (
                self._complete_known_prompt_start_failure(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    session=(execution_session or primed_session or start_session),
                    token=operation_token,
                    error_text=error_text,
                    reason="feishu_prompt_card_send_failed",
                    known_mutation=resume_acknowledged,
                    retain_continuing=(
                        resume_acknowledged and continuation_receipt is not None
                    ),
                    card_id=card_id,
                    message_id=message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    clear_thread_binding=False,
                    surface_failures=surface_failures,
                )
            )
            return PromptTurnStartResult(
                started=False,
                thread_id=thread_id,
                reason_code=START_FAILURE_EXECUTION_CARD,
                reason_text=error_text,
                disposition=settlement.disposition,
            )

        assert execution_session is not None

        def _stale_mutation_result(reason: str) -> PromptTurnStartResult:
            error_text = "queued prompt 的 exact receipt 已失效；未向 Codex 发送请求。"
            settlement, _local_cleanup_succeeded = (
                self._complete_known_prompt_start_failure(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    session=execution_session,
                    token=operation_token,
                    error_text=error_text,
                    reason=reason,
                    known_mutation=resume_acknowledged,
                    retain_continuing=(
                        resume_acknowledged and continuation_receipt is not None
                    ),
                    card_id=card_id,
                    message_id=message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    clear_thread_binding=False,
                    surface_failures=False,
                )
            )
            return self._stale_queue_admission_result(
                expected_root_thread_id,
                disposition=settlement.disposition,
            )

        def _start_turn_once(bound_thread_id: str) -> dict[str, Any]:
            if not self._guard_allows(exact_mutation_guard):
                raise _StaleQueueMutation
            runtime_model = execution_session.model.strip()
            runtime_reasoning_effort = execution_session.reasoning_effort.strip()
            return self._start_turn(
                thread_id=bound_thread_id,
                input_items=effective_input_items,
                cwd=execution_session.working_dir,
                model=runtime_model or None,
                approval_policy=execution_session.approval_policy or None,
                permissions_profile_id=(
                    execution_session.permissions_profile_id or None
                ),
                reasoning_effort=runtime_reasoning_effort or None,
            )

        def _retain_uncertain_resume_failure(
            start_error: Exception,
        ) -> PromptTurnStartResult:
            """Do not turn post-resume rejection into an implicit unlock."""

            logger.warning(
                "explicit prompt was rejected after a continuation-risk resume; "
                "retaining submission pending lifecycle reconciliation: thread=%s",
                thread_id[:12],
            )
            settlement = self._operation_settlement.settle_after_failure(
                operation_token,
                start_error,
                reason="feishu_prompt_start_after_fenced_resume",
                known_mutation=True,
                retain_continuing=True,
            )
            self._mark_runtime_degraded(
                sender_id,
                chat_id,
                reason=f"uncertain resume followed by turn/start rejection: {start_error}",
            )
            return PromptTurnStartResult(
                started=False,
                thread_id=thread_id,
                reason_code=START_FAILURE_TURN_START,
                reason_text=(
                    "当前 thread 恢复后可能已由 persisted goal 自主开始执行；"
                    "Focus 正在等待后端生命周期确认，未释放操作所有权。"
                ),
                disposition=settlement.disposition,
            )

        try:
            start_response = _start_turn_once(thread_id)
        except _StaleQueueMutation:
            return _stale_mutation_result("feishu_prompt_stale_before_turn_start")
        except Exception as exc:
            if continuation_receipt is not None:
                # A successful `thread/resume` may have just continued an
                # active persisted goal.  Its autonomous turn can make this
                # explicit prompt's `turn/start` receive a perfectly known
                # rejection, but that rejection does *not* prove the resumed
                # root is idle. Keep the process-local submission and
                # execution anchor until lifecycle truth identifies the
                # autonomous turn or proves the root inactive.
                return _retain_uncertain_resume_failure(exc)
            if (
                self._is_turn_thread_not_found_error(exc)
                and execution_session.current_thread_id.strip()
            ):
                logger.info(
                    "检测到线程未加载，自动恢复后重试: thread=%s", thread_id[:12]
                )
                try:
                    resume_continuation_risk = self._continuation_may_autostart(
                        thread_id
                    )
                    if resume_continuation_risk:
                        continuation_receipt = self._arm_continuation_exact(
                            operation_token,
                            reason="feishu_prompt_fallback_resume_prestart",
                        )
                except Exception as prestart_exc:
                    logger.exception(
                        "无法在 Feishu fallback resume 前记录 continuation receipt"
                    )
                    settlement, _local_cleanup_succeeded = (
                        self._complete_known_prompt_start_failure(
                            sender_id=sender_id,
                            chat_id=chat_id,
                            session=execution_session,
                            token=operation_token,
                            error_text=f"启动失败：{prestart_exc}",
                            reason="feishu_prompt_prestart_fence_failed",
                            known_mutation=False,
                            retain_continuing=False,
                            card_id=card_id,
                            message_id=message_id,
                            prompt_reply_in_thread=prompt_reply_in_thread,
                            clear_thread_binding=False,
                            surface_failures=surface_failures,
                        )
                    )
                    return PromptTurnStartResult(
                        started=False,
                        thread_id=thread_id,
                        reason_code=START_FAILURE_TURN_START,
                        reason_text=f"启动失败：{prestart_exc}",
                        disposition=settlement.disposition,
                    )
                if not self._guard_allows(exact_mutation_guard):
                    return _stale_mutation_result(
                        "feishu_prompt_stale_before_fallback_resume"
                    )
                try:
                    thread_id = self.resume_bound_thread(
                        sender_id,
                        chat_id,
                        retain_on_local_failure=resume_continuation_risk,
                        message_id=message_id,
                        exact_mutation_guard=exact_mutation_guard,
                    )
                    resume_acknowledged = True
                    resumed_session = self._resolve_session(
                        sender_id,
                        chat_id,
                        message_id,
                    )
                    if (
                        resumed_session.binding != chat_binding_key
                        or resumed_session.current_thread_id.strip() != thread_id
                        or resumed_session.execution.current_message_id
                        != execution_session.execution.current_message_id
                        or resumed_session.execution.started_at
                        != execution_session.execution.started_at
                    ):
                        return _stale_mutation_result(
                            "feishu_prompt_stale_after_fallback_resume"
                        )
                    execution_session = resumed_session
                except Exception as retry_resume_exc:
                    if isinstance(
                        retry_resume_exc,
                        ThreadResumePreSendGuardRejected,
                    ):
                        return _stale_mutation_result(
                            "feishu_prompt_stale_at_fallback_resume_send"
                        )
                    logger.exception("自动恢复线程后重试 turn 失败")
                    settlement = self._complete_resume_prompt_start_failure(
                        sender_id=sender_id,
                        chat_id=chat_id,
                        session=execution_session,
                        token=operation_token,
                        exc=retry_resume_exc,
                        reason="feishu_prompt_fallback_resume_failed",
                        error_text=f"启动失败：{retry_resume_exc}",
                        continuation_receipt=continuation_receipt,
                        card_id=card_id,
                        message_id=message_id,
                        prompt_reply_in_thread=prompt_reply_in_thread,
                        clear_thread_binding=self._is_thread_not_found_error(
                            retry_resume_exc
                        ),
                        surface_failures=surface_failures,
                    )
                    return PromptTurnStartResult(
                        started=False,
                        thread_id=thread_id,
                        reason_code=START_FAILURE_TURN_START,
                        reason_text=f"启动失败：{retry_resume_exc}",
                        disposition=settlement.disposition,
                    )
                try:
                    start_response = _start_turn_once(thread_id)
                except _StaleQueueMutation:
                    return _stale_mutation_result(
                        "feishu_prompt_stale_before_retry_turn_start"
                    )
                except Exception as retry_start_exc:
                    if continuation_receipt is not None:
                        return _retain_uncertain_resume_failure(retry_start_exc)
                    settlement = self._complete_outbound_prompt_start_failure(
                        sender_id=sender_id,
                        chat_id=chat_id,
                        session=execution_session,
                        token=operation_token,
                        exc=retry_start_exc,
                        reason="feishu_prompt_start_after_resume_failed",
                        known_mutation=resume_acknowledged,
                        retain_continuing=False,
                        error_text=f"启动失败：{retry_start_exc}",
                        card_id=card_id,
                        message_id=message_id,
                        prompt_reply_in_thread=prompt_reply_in_thread,
                        clear_thread_binding=False,
                        surface_failures=surface_failures,
                    )
                    return PromptTurnStartResult(
                        started=False,
                        thread_id=thread_id,
                        reason_code=START_FAILURE_TURN_START,
                        reason_text=f"启动失败：{retry_start_exc}",
                        disposition=settlement.disposition,
                    )
            else:
                logger.exception("启动 turn 失败")
                settlement = self._complete_outbound_prompt_start_failure(
                    sender_id=sender_id,
                    chat_id=chat_id,
                    session=execution_session,
                    token=operation_token,
                    exc=exc,
                    reason="feishu_prompt_start_failed",
                    known_mutation=resume_acknowledged,
                    retain_continuing=False,
                    error_text=f"启动失败：{exc}",
                    card_id=card_id,
                    message_id=message_id,
                    prompt_reply_in_thread=prompt_reply_in_thread,
                    clear_thread_binding=False,
                    surface_failures=surface_failures,
                )
                return PromptTurnStartResult(
                    started=False,
                    thread_id=thread_id,
                    reason_code=START_FAILURE_TURN_START,
                    reason_text=f"启动失败：{exc}",
                    disposition=settlement.disposition,
                )

        try:
            response_turn = start_response.get("turn")
            response_turn_id = (
                str(response_turn.get("id", "") or "").strip()
                if isinstance(response_turn, dict)
                else ""
            )
            if response_turn_id:
                self._accept_prompt_start(operation_token, response_turn_id)
            else:
                self._await_root_start_identity(operation_token)
        except Exception:
            logger.exception(
                "turn/start ACK 后无法等待 authoritative turn identity"
            )
            try:
                self._mark_root_outcome_unknown(
                    operation_token,
                    reason="feishu_prompt_ack_awaiting_turn_identity_failed",
                )
            except Exception:
                logger.exception(
                    "turn/start ACK 后无法保留 Feishu submission gate"
                )
        self._schedule_mirror_watchdog(sender_id, chat_id)
        return PromptTurnStartResult(
            started=True,
            thread_id=thread_id,
            disposition="started",
        )

    def _open_prompt_execution_page(
        self,
        session: BindingSessionSnapshot,
        *,
        message_id: str,
        reply_in_thread: bool,
    ) -> InitialExecutionPageOpenResult:
        """Open and commit one exact initial page before `turn/start`."""

        reserved_card_id = (
            str(self._claim_reserved_execution_card(message_id) or "").strip()
            if message_id
            else ""
        )
        return self._open_initial_execution_page(
            session,
            message_id,
            reply_in_thread=reply_in_thread,
            reserved_message_id=reserved_card_id,
        )

    def _arm_continuation_exact(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> FeishuRootContinuationToken:
        receipt = self._arm_root_continuation(token, reason=reason)
        if not isinstance(receipt, FeishuRootContinuationToken):
            raise TypeError("Feishu continuation admission 未返回 typed receipt。")
        return receipt

    @staticmethod
    def _resume_was_acknowledged(exc: Exception) -> bool:
        """Read the resume authority's typed ACK fact from an exception chain."""

        return any(
            isinstance(
                current,
                (ThreadResumeLocalCommitFailed, ThreadResumeSettlementError),
            )
            for current in iter_exception_chain(exc)
        )

    def _settle_resume_operation_after_failure(
        self,
        token: FeishuRootOperationToken,
        exc: Exception,
        *,
        reason: str,
        continuation_receipt: FeishuRootContinuationToken | None,
    ) -> FeishuOperationSettlement:
        """Settle a resume without mistaking a failed local commit for no send."""

        if self._resume_was_acknowledged(exc):
            return self._operation_settlement.finish_without_turn(
                token,
                reason=reason,
                known_mutation=True,
                retain_continuing=continuation_receipt is not None,
            )
        return self._operation_settlement.settle_after_failure(
            token,
            exc,
            reason=reason,
            known_mutation=False,
            retain_continuing=False,
        )

    def _transition_prompt_start_failure(
        self,
        session: BindingSessionSnapshot,
        *,
        error_text: str,
    ) -> BindingSessionSnapshot | None:
        """Make the local prompt projection inactive before owner reconcile."""

        try:
            return self._execution_runtime.record_start_failure(
                RecordBindingExecutionStartFailureCommand(
                    target=BindingExecutionTarget.from_session(session),
                    error_text=error_text,
                )
            )
        except Exception:
            # A failed local projection cannot authorize retry or owner
            # release.  Settlement below will either retain through its normal
            # activity gate or leave the exact token pending.
            logger.exception("无法在 owner settlement 前记录 prompt start failure")
            return None

    def _cleanup_prompt_start_failure(
        self,
        *,
        sender_id: str,
        chat_id: str,
        session: BindingSessionSnapshot,
        error_text: str,
        card_id: str,
        message_id: str,
        prompt_reply_in_thread: bool,
        clear_thread_binding: bool,
        surface_failures: bool,
        owner_settled: bool,
    ) -> bool:
        """Clean exact local ownership and render presentation best-effort."""

        local_cleanup_succeeded = owner_settled

        try:
            self._flush_execution_card_for_session(session, immediate=True)
        except Exception:
            logger.exception("prompt start failure card cleanup failed")

        if owner_settled:
            if clear_thread_binding:
                try:
                    self._clear_thread_binding(
                        sender_id,
                        chat_id,
                        message_id=message_id,
                        session=session,
                    )
                except Exception:
                    logger.exception("prompt start failure binding cleanup failed")
                    local_cleanup_succeeded = False
            else:
                try:
                    # Retire only the local projection.  The handler-level
                    # presentation callback also releases an interaction lease,
                    # which would bypass this admission's typed provenance and
                    # could consume an older same-writer owner's lease.
                    retired = self._execution_runtime.retire_execution(
                        RetireBindingExecutionCommand(
                            target=BindingExecutionTarget.from_session(session)
                        )
                    )
                except Exception:
                    logger.exception("prompt start failure anchor cleanup failed")
                    local_cleanup_succeeded = False
                else:
                    local_cleanup_succeeded = bool(
                        retired is not None
                        and not retired.execution.has_execution_anchor
                    )
                    if not local_cleanup_succeeded:
                        logger.error(
                            "prompt start failure anchor was not retired exactly"
                        )

        if not card_id and surface_failures:
            try:
                self._reply_text(
                    chat_id,
                    error_text,
                    message_id=message_id,
                    reply_in_thread=prompt_reply_in_thread,
                )
            except Exception:
                logger.exception("prompt start failure text fallback failed")
        return local_cleanup_succeeded

    def _complete_known_prompt_start_failure(
        self,
        *,
        sender_id: str,
        chat_id: str,
        session: BindingSessionSnapshot,
        token: FeishuRootOperationToken,
        error_text: str,
        reason: str,
        known_mutation: bool,
        retain_continuing: bool,
        card_id: str,
        message_id: str,
        prompt_reply_in_thread: bool,
        clear_thread_binding: bool,
        surface_failures: bool,
    ) -> tuple[FeishuOperationSettlement, bool]:
        failed_session = (
            self._transition_prompt_start_failure(
                session,
                error_text=error_text,
            )
            or session
        )
        settlement = self._operation_settlement.finish_without_turn(
            token,
            reason=reason,
            known_mutation=known_mutation,
            retain_continuing=retain_continuing,
        )
        local_cleanup_succeeded = self._cleanup_prompt_start_failure(
            sender_id=sender_id,
            chat_id=chat_id,
            session=failed_session,
            error_text=error_text,
            card_id=card_id,
            message_id=message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            clear_thread_binding=clear_thread_binding,
            surface_failures=surface_failures,
            owner_settled=settlement.owner_settled,
        )
        return settlement, local_cleanup_succeeded

    def _complete_outbound_prompt_start_failure(
        self,
        *,
        sender_id: str,
        chat_id: str,
        session: BindingSessionSnapshot,
        token: FeishuRootOperationToken,
        exc: Exception,
        error_text: str,
        reason: str,
        known_mutation: bool,
        retain_continuing: bool,
        card_id: str,
        message_id: str,
        prompt_reply_in_thread: bool,
        clear_thread_binding: bool,
        surface_failures: bool,
    ) -> FeishuOperationSettlement:
        failed_session = (
            self._transition_prompt_start_failure(
                session,
                error_text=error_text,
            )
            or session
        )
        settlement = self._operation_settlement.settle_after_failure(
            token,
            exc,
            reason=reason,
            known_mutation=known_mutation,
            retain_continuing=retain_continuing,
        )
        self._cleanup_prompt_start_failure(
            sender_id=sender_id,
            chat_id=chat_id,
            session=failed_session,
            error_text=error_text,
            card_id=card_id,
            message_id=message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            clear_thread_binding=clear_thread_binding,
            surface_failures=surface_failures,
            owner_settled=settlement.owner_settled,
        )
        return settlement

    def _complete_resume_prompt_start_failure(
        self,
        *,
        sender_id: str,
        chat_id: str,
        session: BindingSessionSnapshot,
        token: FeishuRootOperationToken,
        exc: Exception,
        error_text: str,
        reason: str,
        continuation_receipt: FeishuRootContinuationToken | None,
        card_id: str,
        message_id: str,
        prompt_reply_in_thread: bool,
        clear_thread_binding: bool,
        surface_failures: bool,
    ) -> FeishuOperationSettlement:
        failed_session = (
            self._transition_prompt_start_failure(
                session,
                error_text=error_text,
            )
            or session
        )
        settlement = self._settle_resume_operation_after_failure(
            token,
            exc,
            reason=reason,
            continuation_receipt=continuation_receipt,
        )
        self._cleanup_prompt_start_failure(
            sender_id=sender_id,
            chat_id=chat_id,
            session=failed_session,
            error_text=error_text,
            card_id=card_id,
            message_id=message_id,
            prompt_reply_in_thread=prompt_reply_in_thread,
            clear_thread_binding=clear_thread_binding,
            surface_failures=surface_failures,
            owner_settled=settlement.owner_settled,
        )
        return settlement

    def cancel_current_turn(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
        action_page_message_id: str = "",
    ) -> tuple[bool, str]:
        runtime = self._resolve_session(sender_id, chat_id, message_id)
        action_origin = str(action_page_message_id or "").strip()
        if (
            action_origin
            and action_origin != runtime.execution.current_message_id.strip()
        ):
            return False, "该执行卡片已归档；请使用当前执行卡片或 `/cancel`。"
        if (
            runtime.execution.current_execution_kind.strip()
            == ACTIVE_OBSERVER_EXECUTION_KIND
        ):
            return (
                False,
                "当前飞书会话是本轮的中途 observer，没有取消该 turn 的权限。",
            )
        target = BindingExecutionTarget.from_session(runtime)
        thread_id = runtime.current_thread_id
        turn_id = runtime.execution.current_turn_id
        if not runtime.running or not thread_id:
            if (
                runtime.execution.current_message_id
                or runtime.execution.last_execution_message_id
            ):
                self._refresh_terminal_card(runtime)
                return True, "当前执行已结束，已刷新卡片状态。"
            return False, "当前没有正在执行的 turn。"
        denial_text = self._access_policy.prompt_write_denial_text(
            runtime.binding,
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if denial_text:
            return False, denial_text
        candidate_claim: FeishuPromptInterruptCandidateClaim | None = None
        if not turn_id:
            candidate_claim = self._claim_prompt_interrupt_candidate(
                runtime.binding,
                thread_id,
            )
            if candidate_claim is not None:
                turn_id = candidate_claim.turn_id
        if not turn_id:
            self._execution_runtime.mark_cancel_pending(
                UpdateBindingCancelCommand(target=target)
            )
            return False, "当前还没有可发送的 exact turn id；本次未取消，已保留取消意图。"
        self._execution_runtime.clear_cancel_pending(
            UpdateBindingCancelCommand(target=target)
        )
        try:
            self._interrupt_running_turn(thread_id=thread_id, turn_id=turn_id)
        except Exception as exc:
            candidate_restored = False
            if candidate_claim is not None:
                if self._is_pre_send_error(exc):
                    candidate_restored = (
                        self._restore_prompt_interrupt_candidate_after_pre_send(
                            candidate_claim,
                            error=exc,
                        )
                    )
                else:
                    self._consume_prompt_interrupt_candidate(candidate_claim)
            if self._is_pre_send_error(exc):
                if candidate_claim is None or candidate_restored:
                    self._execution_runtime.mark_cancel_pending(
                        UpdateBindingCancelCommand(target=target)
                    )
                self._mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                if candidate_claim is not None and not candidate_restored:
                    return (
                        False,
                        "取消请求未发送，但 exact candidate 已失效；本次未取消，请刷新状态后重试。",
                    )
                return False, "取消请求未发送；已保留取消意图，请稍后重试 `/cancel`。"
            if self._is_turn_thread_not_found_error(
                exc
            ) or self._is_thread_not_found_error(exc):
                self._finalize_execution(runtime)
                return True, "当前执行已结束，已刷新卡片状态。"
            if self._is_turn_interrupt_rejected_error(exc):
                return False, "Codex 已拒绝该 exact turn id；当前执行未取消，请刷新状态后重试。"
            if self._operation_outcome_unknown(exc):
                self._mark_runtime_degraded(
                    sender_id,
                    chat_id,
                    reason=self._runtime_recovery_reason(exc),
                )
                return (
                    True,
                    "取消请求可能已发送，但当前后端结果未知；稍后会按 turn lifecycle 对账。",
                )
            logger.exception("取消 turn 失败")
            return False, f"取消失败：{exc}"
        if candidate_claim is not None:
            self._consume_prompt_interrupt_candidate(candidate_claim)
        return True, "已请求停止当前执行。"
