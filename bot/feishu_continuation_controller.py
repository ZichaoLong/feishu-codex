"""Feishu continuation orchestration under exact root authority."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.adapters.codex_app_server import CodexAppServerAdapter
from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.cards import (
    build_goal_card,
    build_history_preview_card,
    build_markdown_card,
)
from bot.codex_protocol.client import CodexRpcError
from bot.constants import display_path
from bot.feishu_resume_settlement import (
    EXPLICIT_RESUME_FAILURE_REASONS,
    EXPLICIT_SETTINGS_FAILURE_REASONS,
    GOAL_RESUME_FAILURE_REASONS,
    RUNTIME_ATTACH_PRESTART_FAILURE_REASONS,
    RUNTIME_ATTACH_RESUME_FAILURE_REASONS,
    FeishuResumeMutationProgress,
    FeishuResumeOwnerDisposition,
    FeishuResumeSettlementService,
    FeishuResumeSuccessKind,
    SettleFeishuResumeFailure,
    SettleFeishuResumeSuccess,
)
from bot.feishu_root_operation_contract import (
    FeishuRootContinuationToken,
    FeishuRootOperationToken,
)
from bot.feishu_root_operation_controller import FeishuRootOperationController
from bot.feishu_thread_session_coordinator import (
    FeishuThreadSessionCoordinator,
    binding_goal_snapshot,
)
from bot.goal_continuation_policy import (
    goal_status_may_continue,
    is_reviewed_non_continuing_goal_status,
)
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_ACTIVE,
    BACKEND_THREAD_STATUS_IDLE,
    BACKEND_THREAD_STATUS_NOT_LOADED,
)
from bot.thread_access_policy import ThreadAccessPolicy
from bot.thread_resolution import looks_like_thread_id, resolve_resume_target_by_name
from bot.thread_runtime_authority import (
    ThreadResumeLocalFailurePolicy,
    ThreadRuntimeAuthority,
)


@dataclass(frozen=True, slots=True)
class FeishuExplicitResumeFailure:
    text: str


@dataclass(frozen=True, slots=True)
class FeishuExplicitResumeSuccess:
    snapshot: ThreadSnapshot
    paused_for_cold_sync: bool


FeishuExplicitResumeResult = (
    FeishuExplicitResumeFailure | FeishuExplicitResumeSuccess
)


class FeishuContinuationController:
    """Own Feishu continuation effects without owning mutable runtime facts."""

    def __init__(
        self,
        *,
        lock: Any,
        adapter: CodexAppServerAdapter,
        binding_runtime: BindingRuntimeManager,
        access_policy: ThreadAccessPolicy,
        root_operations: FeishuRootOperationController,
        resume_settlement: FeishuResumeSettlementService,
        thread_sessions: FeishuThreadSessionCoordinator,
        thread_runtime_authority: ThreadRuntimeAuthority,
        history_preview_rounds: int,
        show_history_preview_on_resume: bool,
        thread_list_query_limit: int,
        local_thread_safety_rule: str,
        logger: logging.Logger,
    ) -> None:
        self._lock = lock
        self._adapter = adapter
        self._binding_runtime = binding_runtime
        self._access_policy = access_policy
        self._root_operations = root_operations
        self._resume_settlement = resume_settlement
        self._thread_sessions = thread_sessions
        self._thread_runtime_authority = thread_runtime_authority
        self._history_preview_rounds = history_preview_rounds
        self._show_history_preview_on_resume = show_history_preview_on_resume
        self._thread_list_query_limit = thread_list_query_limit
        self._local_thread_safety_rule = local_thread_safety_rule
        self._logger = logger

    def get_thread_goal(
        self,
        thread_id: str,
        *,
        operation: str = "查看 goal",
    ) -> ThreadGoalSummary | None:
        return self._get_direct_thread_goal(thread_id, operation=operation)

    def get_thread_goal_for_resume(
        self,
        thread_id: str,
    ) -> ThreadGoalSummary | None:
        return self._get_direct_thread_goal(
            thread_id,
            operation="恢复前读取 goal",
        )

    def set_thread_goal_for_control(
        self,
        thread_id: str,
        **kwargs: Any,
    ) -> ThreadGoalSummary:
        return self._set_direct_thread_goal(
            thread_id,
            operation="通过本地控制面修改 goal",
            **kwargs,
        )

    def clear_thread_goal_for_control(self, thread_id: str) -> bool:
        verified = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation="通过本地控制面清除 goal",
        )
        return self._adapter.clear_thread_goal(verified.thread_id)

    def prompt_write_denial_text(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> str:
        return self._access_policy.prompt_write_denial_text(
            self._chat_binding_key(sender_id, chat_id, message_id),
            chat_id,
            thread_id,
            message_id=message_id,
        )

    def resolve_resume_target(self, arg: str) -> ThreadSummary:
        target = arg.strip()
        if looks_like_thread_id(target):
            return self._thread_sessions.read_direct_thread_summary(
                target,
                original_arg=target,
                operation="直接读取或管理",
            )
        thread = resolve_resume_target_by_name(
            self._adapter,
            name=target,
            limit=self._thread_list_query_limit,
        )
        return self._thread_sessions.read_direct_thread_summary(
            thread.thread_id,
            original_arg=target,
            operation="直接读取或管理",
        )

    def mutate_goal(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        message_id: str = "",
    ) -> ThreadGoalSummary:
        """Apply one Feishu goal mutation under an exact submission lease."""

        verified = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation="修改 goal",
        )
        normalized_objective = (
            None if objective is None else str(objective or "").strip()
        )
        normalized_status = None if status is None else str(status or "").strip()
        may_activate = goal_status_may_continue(normalized_status)
        admission = self._root_operations.admit(
            self._chat_binding_key(sender_id, chat_id, message_id),
            verified.thread_id,
            chat_id=chat_id,
            message_id=message_id,
            reason="feishu_goal_mutation_claimed",
        )
        if may_activate:
            try:
                self._root_operations.arm_continuation(
                    admission,
                    reason="feishu_goal_mutation_prestart",
                )
            except Exception:
                self._root_operations.settle_known_failure(
                    admission,
                    reason="feishu_goal_mutation_prestart_failed",
                )
                raise
        try:
            goal = self._adapter.set_thread_goal(
                verified.thread_id,
                objective=normalized_objective,
                status=normalized_status,
            )
        except Exception as exc:
            if self._resume_settlement.operation_outcome_unknown(exc):
                self._root_operations.mark_outcome_unknown(
                    admission,
                    reason="feishu_goal_mutation_outcome_unknown",
                )
            else:
                self._root_operations.settle_known_failure(
                    admission,
                    reason="feishu_goal_mutation_failed",
                )
            raise
        if is_reviewed_non_continuing_goal_status(goal.status):
            self._root_operations.settle_noncontinuing(
                admission,
                reason="feishu_goal_mutation_noncontinuing",
            )
        else:
            self._root_operations.acknowledge_continuing(admission)
        return goal

    def clear_goal(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        message_id: str = "",
    ) -> bool:
        """Clear a goal without leaving a recordless mutation race."""

        verified = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation="清除 goal",
        )
        admission = self._root_operations.admit(
            self._chat_binding_key(sender_id, chat_id, message_id),
            verified.thread_id,
            chat_id=chat_id,
            message_id=message_id,
            reason="feishu_goal_clear_claimed",
        )
        try:
            cleared = self._adapter.clear_thread_goal(verified.thread_id)
        except Exception as exc:
            if self._resume_settlement.operation_outcome_unknown(exc):
                self._root_operations.mark_outcome_unknown(
                    admission,
                    reason="feishu_goal_clear_outcome_unknown",
                )
            else:
                self._root_operations.settle_known_failure(
                    admission,
                    reason="feishu_goal_clear_failed",
                )
            raise
        if cleared:
            self._root_operations.settle_noncontinuing(
                admission,
                reason="feishu_goal_clear_settled",
            )
        else:
            self._root_operations.settle_known_mutation(
                admission,
                reason="feishu_goal_clear_noop_acknowledged",
            )
        return cleared

    def attach_binding_for_control(
        self,
        binding: ChatBindingKey,
        thread_id: str,
        *,
        active_observer: bool = False,
    ) -> ThreadSummary:
        """Resume one RuntimeAdmin-admitted binding under exact settlement.

        RuntimeAdmin owns both detached-runtime checks immediately before this
        call. This owner starts with continuation classification and never
        re-enters the surface-neutral admin application.
        """

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        if type(active_observer) is not bool:
            raise TypeError("active_observer must be an exact bool")
        fallback_summary = ThreadSummary(
            thread_id=normalized_thread_id,
            cwd="",
            name="",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status=BACKEND_THREAD_STATUS_IDLE,
        )
        if active_observer:
            snapshot = self._thread_sessions.resume_and_commit_feishu_binding(
                binding[0],
                binding[1],
                normalized_thread_id,
                original_arg=normalized_thread_id,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                summary=fallback_summary,
                exact_mutation_guard=lambda: (
                    self._active_observer_resume_guard(normalized_thread_id)
                ),
                active_observer=True,
            )
            return snapshot.summary
        continuation_risk = self.resume_may_autostart(normalized_thread_id)
        admission = self._root_operations.admit(
            binding,
            normalized_thread_id,
            chat_id=binding[1],
            reason="feishu_runtime_admin_attach_claimed",
        )
        resume_continuation: FeishuRootContinuationToken | None = None
        if continuation_risk:
            try:
                resume_continuation = self._resume_settlement.require_continuation(
                    self._root_operations.arm_continuation(
                        admission,
                        reason="feishu_runtime_admin_attach_resume_prestart",
                    ),
                    operation="RuntimeAdmin attach",
                )
            except Exception as exc:
                self._resume_settlement.settle_failure(
                    SettleFeishuResumeFailure(
                        admission=admission,
                        error=exc,
                        progress=FeishuResumeMutationProgress.NONE,
                        reasons=RUNTIME_ATTACH_PRESTART_FAILURE_REASONS,
                    )
                )
                raise
        try:
            snapshot = self._thread_sessions.resume_and_commit_feishu_binding(
                binding[0],
                binding[1],
                normalized_thread_id,
                original_arg=normalized_thread_id,
                failure_policy=(
                    ThreadResumeLocalFailurePolicy.RETAIN
                    if continuation_risk
                    else ThreadResumeLocalFailurePolicy.COMPENSATE
                ),
                summary=fallback_summary,
            )
        except Exception as exc:
            self._resume_settlement.settle_failure(
                SettleFeishuResumeFailure(
                    admission=admission,
                    error=exc,
                    progress=FeishuResumeMutationProgress.ATTEMPTED,
                    continuation=resume_continuation,
                    reasons=RUNTIME_ATTACH_RESUME_FAILURE_REASONS,
                )
            )
            raise
        self._resume_settlement.settle_success(
            SettleFeishuResumeSuccess(
                admission=admission,
                kind=(
                    FeishuResumeSuccessKind.CONTINUING
                    if continuation_risk
                    else FeishuResumeSuccessKind.KNOWN_MUTATION
                ),
                reason="feishu_runtime_admin_attach_acknowledged",
            )
        )
        return snapshot.summary

    def _active_observer_resume_guard(self, thread_id: str) -> bool:
        """Recheck the narrow running-resume exception immediately pre-send."""

        summary = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation="附着 active 飞书 observer",
        )
        return summary.status == BACKEND_THREAD_STATUS_ACTIVE

    def resume_thread(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str | None = None,
        pause_active_goal_on_resume: bool = False,
        message_id: str = "",
    ) -> FeishuExplicitResumeResult:
        """Run one explicit resume and return its settled typed outcome."""

        try:
            verified_target = self._thread_sessions.read_direct_thread_summary(
                thread_id,
                original_arg=original_arg or thread_id,
                operation="恢复",
            )
        except Exception as exc:
            return FeishuExplicitResumeFailure(text=f"恢复线程失败：{exc}")
        thread_id = verified_target.thread_id
        try:
            runtime = self._binding_runtime.resolve_session(
                sender_id,
                chat_id,
                message_id,
            )
        except Exception as exc:
            self._logger.exception("恢复线程前读取当前会话设置失败")
            return FeishuExplicitResumeFailure(text=f"恢复线程失败：{exc}")
        all_mode_exclusivity_violation = (
            self._access_policy.all_mode_thread_exclusivity_violation(
                chat_id,
                thread_id,
                message_id=message_id,
            )
        )
        if all_mode_exclusivity_violation:
            return FeishuExplicitResumeFailure(
                text=all_mode_exclusivity_violation
            )
        if runtime.execution.has_execution_anchor:
            return FeishuExplicitResumeFailure(
                text="当前线程仍在执行，暂不切换。"
            )
        try:
            approval_policy = runtime.approval_policy or None
            permissions_profile_id = runtime.permissions_profile_id or None
            model = runtime.model or None
            reasoning_effort = runtime.reasoning_effort or None
        except Exception as exc:
            self._logger.exception("恢复线程前读取当前会话设置失败")
            return FeishuExplicitResumeFailure(text=f"恢复线程失败：{exc}")
        try:
            admission = self._root_operations.admit(
                self._chat_binding_key(sender_id, chat_id, message_id),
                thread_id,
                chat_id=chat_id,
                message_id=message_id,
                reason="feishu_thread_resume_claimed",
            )
        except Exception as exc:
            return FeishuExplicitResumeFailure(text=f"恢复线程失败：{exc}")
        goal = None
        goal_is_active = False
        goal_may_autostart = False
        paused_for_cold_sync = False
        mutation_attempted = False
        mutation_succeeded = False
        resume_continuation: FeishuRootContinuationToken | None = None
        try:
            loaded_thread_ids = set(self._adapter.list_loaded_thread_ids())
            was_loaded = thread_id in loaded_thread_ids
            if not was_loaded:
                was_loaded = (
                    str(verified_target.status or "").strip()
                    != BACKEND_THREAD_STATUS_NOT_LOADED
                )
            goal = self._get_thread_goal_if_available(thread_id)
            goal_status = str(getattr(goal, "status", "") or "").strip()
            goal_is_active = goal is not None and goal_status == "active"
            goal_may_autostart = goal is not None and goal_status_may_continue(
                goal_status
            )
            if (
                not was_loaded
                and goal_is_active
                and pause_active_goal_on_resume
            ):
                mutation_attempted = True
                self._set_direct_thread_goal(
                    thread_id,
                    operation="暂停 goal 以恢复线程",
                    status="paused",
                )
                mutation_succeeded = True
                paused_for_cold_sync = True
            carry_cold_binding_settings = not was_loaded and (
                not goal_is_active or pause_active_goal_on_resume
            )
            if goal_may_autostart and not paused_for_cold_sync:
                resume_continuation = self._resume_settlement.require_continuation(
                    self._root_operations.arm_continuation(
                        admission,
                        reason="feishu_thread_resume_prestart",
                    ),
                    operation="explicit resume",
                )
            mutation_attempted = True
            snapshot = self._thread_sessions.resume_and_commit_feishu_binding(
                sender_id,
                chat_id,
                thread_id,
                original_arg=original_arg or thread_id,
                failure_policy=(
                    ThreadResumeLocalFailurePolicy.RETAIN
                    if goal_may_autostart and not paused_for_cold_sync
                    else ThreadResumeLocalFailurePolicy.COMPENSATE
                ),
                summary=verified_target,
                model=model if carry_cold_binding_settings else None,
                reasoning_effort=(
                    reasoning_effort if carry_cold_binding_settings else None
                ),
                approval_policy=(
                    approval_policy if carry_cold_binding_settings else None
                ),
                permissions_profile_id=(
                    permissions_profile_id
                    if carry_cold_binding_settings
                    else None
                ),
                message_id=message_id,
            )
            mutation_succeeded = True
        except Exception as exc:
            owner_disposition = FeishuResumeOwnerDisposition.SETTLE
            resume_acknowledged = self._resume_settlement.resume_was_acknowledged(
                exc
            )
            if paused_for_cold_sync and not resume_acknowledged:
                _restored_goal, owner_disposition = (
                    self._restore_paused_goal_after_failed_resume(
                        thread_id,
                        admission=admission,
                    )
                )
            self._resume_settlement.settle_failure(
                SettleFeishuResumeFailure(
                    admission=admission,
                    error=exc,
                    progress=FeishuResumeMutationProgress.from_facts(
                        mutation_attempted=mutation_attempted,
                        mutation_succeeded=mutation_succeeded,
                    ),
                    reasons=EXPLICIT_RESUME_FAILURE_REASONS,
                    continuation=resume_continuation,
                    owner_disposition=owner_disposition,
                )
            )
            self._logger.exception("恢复线程失败")
            return FeishuExplicitResumeFailure(text=f"恢复线程失败：{exc}")
        try:
            mutation_attempted = True
            self._thread_runtime_authority.update_thread_settings(
                thread_id,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            mutation_succeeded = True
        except Exception as exc:
            owner_disposition = FeishuResumeOwnerDisposition.SETTLE
            if paused_for_cold_sync:
                _restored_goal, owner_disposition = (
                    self._restore_paused_goal_after_failed_resume(
                        thread_id,
                        admission=admission,
                    )
                )
            self._resume_settlement.settle_failure(
                SettleFeishuResumeFailure(
                    admission=admission,
                    error=exc,
                    progress=FeishuResumeMutationProgress.from_facts(
                        mutation_attempted=mutation_attempted,
                        mutation_succeeded=mutation_succeeded,
                    ),
                    reasons=EXPLICIT_SETTINGS_FAILURE_REASONS,
                    owner_disposition=owner_disposition,
                )
            )
            self._logger.exception("同步线程设置失败")
            return FeishuExplicitResumeFailure(
                text=f"恢复线程后同步当前会话设置失败：{exc}"
            )
        self._resume_settlement.settle_success(
            SettleFeishuResumeSuccess(
                admission=admission,
                kind=FeishuResumeSuccessKind.KNOWN_MUTATION,
                reason="feishu_thread_resume_acknowledged",
            )
        )
        return FeishuExplicitResumeSuccess(
            snapshot=snapshot,
            paused_for_cold_sync=paused_for_cold_sync,
        )

    def build_explicit_resume_card(
        self,
        result: FeishuExplicitResumeSuccess,
    ) -> dict:
        """Project a settled resume after the caller refreshes thread state."""

        snapshot = result.snapshot
        summary = (
            f"**已切换到线程**\n"
            f"thread：`{snapshot.summary.thread_id[:8]}…`\n"
            f"标题：{snapshot.summary.title}\n"
            f"目录：`{display_path(snapshot.summary.cwd)}`\n"
            f"{self._local_thread_safety_rule}"
        )
        if result.paused_for_cold_sync:
            summary += (
                "\n当前按本会话设置恢复了 thread，但 persisted goal 仍保持 "
                "`paused`；如需继续，请执行 `/goal resume`。"
            )
        if self._show_history_preview_on_resume:
            rounds = self._extract_history_rounds(snapshot)
            if rounds:
                return build_history_preview_card(
                    snapshot.summary.thread_id,
                    rounds,
                    summary=summary,
                )
        return build_markdown_card(
            "Codex 已切换线程",
            summary,
            template="green",
        )

    def resume_goal(
        self,
        sender_id: str,
        chat_id: str,
        expected_thread_id: str,
        attach_binding: bool,
        message_id: str = "",
    ) -> dict:
        normalized_expected_thread_id = str(expected_thread_id or "").strip()
        if not normalized_expected_thread_id:
            return build_markdown_card(
                "Codex Goal 操作失败",
                "确认请求缺少 thread_id；请刷新 goal 卡后重试。",
                template="red",
            )
        runtime = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        thread_id = runtime.current_thread_id.strip()
        if thread_id != normalized_expected_thread_id:
            return build_markdown_card(
                "Codex Goal 操作失败",
                "确认卡已过期：当前会话已切换到另一 thread；"
                "请刷新 goal 卡后重试。",
                template="red",
            )
        thread_id = normalized_expected_thread_id
        try:
            verified_target = self._thread_sessions.read_direct_thread_summary(
                thread_id,
                original_arg=thread_id,
                operation="恢复 goal",
            )
        except Exception as exc:
            return build_markdown_card(
                "Codex Goal 操作失败",
                str(exc) or "无法确认直接目标",
                template="red",
            )
        thread_id = verified_target.thread_id
        try:
            loaded_thread_ids = set(self._adapter.list_loaded_thread_ids())
        except Exception as exc:
            return build_markdown_card(
                "Codex Goal 操作失败",
                str(exc) or "无法确认当前 thread 是否已加载。",
                template="red",
            )
        was_loaded = thread_id in loaded_thread_ids
        denial = self._access_policy.prompt_write_denial_check(
            self._chat_binding_key(sender_id, chat_id, message_id),
            chat_id,
            thread_id,
            message_id=message_id,
        )
        if not denial.allowed:
            return build_markdown_card(
                "Codex Goal 操作失败",
                denial.reason_text,
                template="red",
            )
        try:
            goal = self._get_direct_thread_goal(
                thread_id,
                operation="恢复 goal 前读取",
            )
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                return build_markdown_card(
                    "Codex Goal 操作失败",
                    "当前 backend 未启用 goal 功能。",
                    template="red",
                )
            raise
        if goal is None:
            return build_markdown_card(
                "Codex Goal 操作失败",
                "当前 thread 没有可恢复的 goal。",
                template="red",
            )
        try:
            admission = self._root_operations.admit(
                self._chat_binding_key(sender_id, chat_id, message_id),
                thread_id,
                chat_id=chat_id,
                message_id=message_id,
                reason="feishu_goal_resume_claimed",
            )
        except Exception as exc:
            return build_markdown_card(
                "Codex Goal 操作失败",
                str(exc) or "无法取得 main-turn submission lease",
                template="red",
            )
        approval_policy = runtime.approval_policy or None
        permissions_profile_id = runtime.permissions_profile_id or None
        model = runtime.model or None
        reasoning_effort = runtime.reasoning_effort or None
        effective_goal = goal
        paused_for_cold_sync = False
        mutation_attempted = False
        mutation_succeeded = False
        autonomous_start_accepted = False
        resume_continuation: FeishuRootContinuationToken | None = None
        activation_continuation: FeishuRootContinuationToken | None = None
        try:
            goal_status = str(goal.status or "").strip()
            goal_may_autostart = goal_status_may_continue(goal_status)
            if not was_loaded and goal_status == "active":
                mutation_attempted = True
                effective_goal = self._set_direct_thread_goal(
                    thread_id,
                    operation="暂停 goal 以恢复",
                    status="paused",
                )
                mutation_succeeded = True
                paused_for_cold_sync = True
                self.project_goal(
                    sender_id,
                    chat_id,
                    message_id,
                    effective_goal,
                )
            carry_cold_binding_settings = not was_loaded
            if attach_binding or not was_loaded:
                if goal_may_autostart and not paused_for_cold_sync:
                    resume_continuation = (
                        self._resume_settlement.require_continuation(
                            self._root_operations.arm_continuation(
                                admission,
                                reason="feishu_goal_resume_prestart",
                            ),
                            operation="goal resume",
                        )
                    )
                mutation_attempted = True
                resume_kwargs = {
                    "original_arg": thread_id,
                    "model": model if carry_cold_binding_settings else None,
                    "reasoning_effort": (
                        reasoning_effort if carry_cold_binding_settings else None
                    ),
                    "approval_policy": (
                        approval_policy if carry_cold_binding_settings else None
                    ),
                    "permissions_profile_id": (
                        permissions_profile_id
                        if carry_cold_binding_settings
                        else None
                    ),
                }
                if attach_binding:
                    self._thread_sessions.resume_and_commit_feishu_binding(
                        sender_id,
                        chat_id,
                        thread_id,
                        failure_policy=(
                            ThreadResumeLocalFailurePolicy.RETAIN
                            if goal_may_autostart and not paused_for_cold_sync
                            else ThreadResumeLocalFailurePolicy.COMPENSATE
                        ),
                        message_id=message_id,
                        **resume_kwargs,
                    )
                else:
                    self._thread_sessions.resume_and_commit_feishu_operation_owner(
                        admission,
                        thread_id,
                        failure_policy=(
                            ThreadResumeLocalFailurePolicy.RETAIN
                            if goal_may_autostart and not paused_for_cold_sync
                            else ThreadResumeLocalFailurePolicy.COMPENSATE
                        ),
                        **resume_kwargs,
                    )
                mutation_succeeded = True
                if goal_may_autostart and not paused_for_cold_sync:
                    autonomous_start_accepted = True
            mutation_attempted = True
            self._thread_runtime_authority.update_thread_settings(
                thread_id,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            mutation_succeeded = True
            if goal_status != "active" or paused_for_cold_sync:
                activation_continuation = self._root_operations.arm_continuation(
                    admission,
                    reason="feishu_goal_resume_set_active_prestart",
                )
                mutation_attempted = True
                effective_goal = self._set_direct_thread_goal(
                    thread_id,
                    operation="恢复 goal",
                    status="active",
                )
                mutation_succeeded = True
                autonomous_start_accepted = True
            self.project_goal(
                sender_id,
                chat_id,
                message_id,
                effective_goal,
            )
        except Exception as exc:
            owner_disposition = FeishuResumeOwnerDisposition.SETTLE
            resume_acknowledged = self._resume_settlement.resume_was_acknowledged(
                exc
            )
            if paused_for_cold_sync and not resume_acknowledged:
                restored_goal, owner_disposition = (
                    self._restore_paused_goal_after_failed_resume(
                        thread_id,
                        admission=admission,
                    )
                )
                if restored_goal is not None:
                    autonomous_start_accepted = True
                    try:
                        self.project_goal(
                            sender_id,
                            chat_id,
                            message_id,
                            restored_goal,
                        )
                    except Exception:
                        self._logger.exception(
                            "恢复 paused goal 后刷新本地 projection 失败: thread=%s",
                            thread_id[:12],
                        )
            self._resume_settlement.settle_failure(
                SettleFeishuResumeFailure(
                    admission=admission,
                    error=exc,
                    progress=FeishuResumeMutationProgress.from_facts(
                        mutation_attempted=mutation_attempted,
                        mutation_succeeded=mutation_succeeded,
                    ),
                    reasons=GOAL_RESUME_FAILURE_REASONS,
                    continuation=resume_continuation,
                    known_failure_continuation=(
                        activation_continuation
                        if not autonomous_start_accepted
                        else None
                    ),
                    owner_disposition=owner_disposition,
                )
            )
            self._logger.exception("恢复 goal 失败")
            return build_markdown_card(
                "Codex Goal 操作失败",
                str(exc) or "恢复 goal 失败",
                template="red",
            )
        goal_is_noncontinuing = is_reviewed_non_continuing_goal_status(
            effective_goal.status
        )
        self._resume_settlement.settle_success(
            SettleFeishuResumeSuccess(
                admission=admission,
                kind=(
                    FeishuResumeSuccessKind.NONCONTINUING
                    if goal_is_noncontinuing
                    else FeishuResumeSuccessKind.CONTINUING
                ),
                reason=(
                    "feishu_goal_resume_noncontinuing"
                    if goal_is_noncontinuing
                    else "feishu_goal_resume_continuing"
                ),
            )
        )
        notice = "已恢复当前 thread goal。"
        if attach_binding:
            notice += "\n当前会话已恢复接收该 thread 的飞书推送。"
        thread_title = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        ).current_thread_title.strip()
        return build_goal_card(
            thread_id=thread_id,
            thread_title=thread_title,
            goal=effective_goal,
            notice=notice,
        )

    def project_goal(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str,
        goal: ThreadGoalSummary | None,
    ) -> None:
        session = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        with self._lock:
            self._binding_runtime.project_thread_goal_locked(
                session.handle,
                binding_goal_snapshot(goal),
            )

    def _restore_paused_goal_after_failed_resume(
        self,
        thread_id: str,
        *,
        admission: FeishuRootOperationToken | None = None,
    ) -> tuple[ThreadGoalSummary | None, FeishuResumeOwnerDisposition]:
        """Try the compensating active write without losing admission authority."""

        continuation: FeishuRootContinuationToken | None = None
        if admission is not None:
            try:
                continuation = self._root_operations.arm_continuation(
                    admission,
                    reason="feishu_goal_restore_prestart",
                )
            except Exception:
                self._logger.exception(
                    "恢复 paused goal 前无法记录 continuation receipt: thread=%s",
                    thread_id[:12],
                )
                return None, FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
        try:
            restored = self._set_direct_thread_goal(
                thread_id,
                operation="恢复失败后还原 goal",
                status="active",
            )
        except Exception as exc:
            owner_disposition = FeishuResumeOwnerDisposition.SETTLE
            if admission is not None:
                try:
                    outcome_unknown = (
                        self._resume_settlement.operation_outcome_unknown(exc)
                    )
                except Exception:
                    self._logger.exception(
                        "无法分类 paused goal restore 的上游结果: thread=%s",
                        thread_id[:12],
                    )
                    owner_disposition = (
                        FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
                    )
                else:
                    if outcome_unknown:
                        try:
                            self._root_operations.mark_outcome_unknown(
                                admission,
                                reason="feishu_goal_restore_outcome_unknown",
                            )
                        except Exception:
                            self._logger.exception(
                                "无法记录 paused goal restore 的进程内未知结果: "
                                "thread=%s",
                                thread_id[:12],
                            )
                        owner_disposition = (
                            FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
                        )
                    elif continuation is not None:
                        try:
                            self._root_operations.settle_continuation_failure(
                                continuation,
                                reason="feishu_goal_restore_known_failure",
                            )
                        except Exception:
                            self._logger.exception(
                                "无法结算 paused goal restore 的 exact continuation: "
                                "thread=%s",
                                thread_id[:12],
                            )
                            owner_disposition = (
                                FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
                            )
            self._logger.exception(
                "恢复失败后回滚 paused goal 失败: thread=%s",
                thread_id[:12],
            )
            return None, owner_disposition
        return restored, FeishuResumeOwnerDisposition.SETTLE

    def resume_may_autostart(self, thread_id: str) -> bool:
        """Classify Feishu attach/resume continuation risk conservatively."""

        try:
            goal = self._adapter.get_thread_goal(thread_id)
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                return False
            self._logger.warning(
                "Unable to read persisted goal before Feishu attach/resume; "
                "holding an exact blank submission lease: thread=%s",
                str(thread_id or "")[:12],
                exc_info=True,
            )
            return True
        if goal is None:
            return False
        return goal_status_may_continue(getattr(goal, "status", ""))

    def _get_thread_goal_if_available(
        self,
        thread_id: str,
    ) -> ThreadGoalSummary | None:
        try:
            return self._get_direct_thread_goal(thread_id, operation="查看 goal")
        except Exception as exc:
            if self._is_goals_feature_disabled_error(exc):
                return None
            raise

    def _extract_history_rounds(
        self,
        snapshot: ThreadSnapshot,
    ) -> list[tuple[str, str]]:
        rounds: list[tuple[str, str]] = []
        for turn in snapshot.turns:
            user_parts: list[str] = []
            assistant_parts: list[str] = []
            for item in turn.get("items") or []:
                item_type = item.get("type")
                if item_type == "userMessage":
                    for content in item.get("content") or []:
                        if content.get("type") == "text" and content.get("text"):
                            user_parts.append(content["text"])
                elif item_type == "agentMessage" and item.get("text"):
                    assistant_parts.append(item["text"])
            user_text = "\n".join(
                part.strip() for part in user_parts if part.strip()
            ).strip()
            assistant_text = "\n\n".join(
                part.strip() for part in assistant_parts if part.strip()
            ).strip()
            if user_text or assistant_text:
                rounds.append(
                    (user_text or "（空）", assistant_text or "（无回复）")
                )
        return rounds[-self._history_preview_rounds :]

    def _get_direct_thread_goal(
        self,
        thread_id: str,
        *,
        operation: str,
    ) -> ThreadGoalSummary | None:
        verified = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation=operation,
        )
        return self._adapter.get_thread_goal(verified.thread_id)

    def _set_direct_thread_goal(
        self,
        thread_id: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> ThreadGoalSummary:
        verified = self._thread_sessions.read_direct_thread_summary(
            thread_id,
            original_arg=thread_id,
            operation=operation,
        )
        return self._adapter.set_thread_goal(verified.thread_id, **kwargs)

    def _chat_binding_key(
        self,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
    ) -> ChatBindingKey:
        with self._lock:
            existing = self._binding_runtime.existing_chat_binding_key_locked(
                sender_id,
                chat_id,
            )
            if existing is not None:
                return existing
        return self._binding_runtime.fresh_chat_binding_key(
            sender_id,
            chat_id,
            message_id,
        )

    @staticmethod
    def _is_goals_feature_disabled_error(exc: Exception) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        return (
            str(exc.error.get("message", "") or "").strip().lower()
            == "goals feature is disabled"
        )
