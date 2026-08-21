"""Feishu thread create/resume transactions with one local settlement owner.

The upstream thread authority owns create/resume receipts, the binding runtime
owns resident session facts, and the root-operation controller owns the exact
process-local submission.  This coordinator owns the ordering between them;
surfaces receive only a committed snapshot or a typed recovery failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, NoReturn, Protocol

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.binding_runtime_contract import (
    BindingGoalSnapshot,
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.direct_thread_target_policy import (
    DirectThreadTargetPolicyError,
    read_direct_thread_target,
    require_direct_thread_target,
)
from bot.feishu_binding_transition import (
    BindFeishuThreadCommand,
    FeishuBindingTransitionCommit,
    FeishuBindingTransitionOwner,
)
from bot.feishu_active_observer import (
    ActiveObserverExecution,
    ActiveObserverPresentationResult,
    ActiveObserverResumeSnapshot,
)
from bot.feishu_root_operation_contract import FeishuRootOperationToken
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    FEISHU_RUNTIME_ATTACHED,
    FEISHU_RUNTIME_DETACHED,
)
from bot.thread_create_transaction import (
    ThreadCreateLocalCommitFailed,
)
from bot.thread_runtime_authority import (
    PendingThreadResume,
    ThreadResumeLocalCommitFailed,
    ThreadResumeLocalFailurePolicy,
    ThreadResumeSettlementError,
)


logger = logging.getLogger(__name__)


class FeishuThreadSessionAdapter(Protocol):
    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> ThreadSnapshot: ...

    def get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None: ...


class FeishuThreadSessionBindingRuntime(Protocol):
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

    def project_thread_goal_locked(
        self,
        handle: BindingRuntimeHandle,
        goal: BindingGoalSnapshot | None,
        *,
        expected_thread_id: str = "",
    ) -> BindingSessionSnapshot | None: ...


class FeishuThreadSessionRuntimeAuthority(Protocol):
    def create_and_commit_thread(
        self,
        *,
        local_commit: Callable[[ThreadSnapshot], Any],
        **kwargs: Any,
    ) -> Any: ...

    def begin_resume_thread(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        exact_mutation_guard: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> PendingThreadResume[ThreadSnapshot]: ...

    def unsubscribe_thread(self, thread_id: str) -> None: ...


class FeishuThreadSessionRootOperations(Protocol):
    def commit_resume_owner(self, token: FeishuRootOperationToken) -> None: ...


class FeishuThreadSessionWarnings(Protocol):
    def record(
        self,
        *,
        code: str,
        source: str,
        message: str,
        severity: str = "warning",
        details: dict[str, Any] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class FeishuThreadSessionPorts:
    """Small capabilities not already represented by a state owner."""

    acquire_runtime_lease: Callable[[str], bool]
    release_runtime_lease: Callable[[str], None]
    runtime_interest_retained: Callable[[str], bool]
    remember_direct_thread_summary: Callable[[ThreadSummary], None]
    is_thread_not_found_error: Callable[[Exception], bool]
    is_transport_disconnect: Callable[[Exception], bool]
    prepare_active_observer: Callable[
        [ThreadSnapshot],
        ActiveObserverResumeSnapshot | None,
    ]
    prime_active_observer: Callable[
        [BindingSessionSnapshot, ActiveObserverResumeSnapshot],
        ActiveObserverExecution,
    ]
    rollback_active_observer: Callable[
        [BindingSessionSnapshot, ActiveObserverResumeSnapshot],
        None,
    ]
    present_active_observer: Callable[
        [ActiveObserverExecution],
        ActiveObserverPresentationResult,
    ]
    schedule_active_observer_recovery: Callable[
        [BindingSessionSnapshot],
        None,
    ]


@dataclass(frozen=True, slots=True)
class _FeishuBindingCommit:
    summary: ThreadSummary
    transition: FeishuBindingTransitionCommit
    active_observer_execution: ActiveObserverExecution | None = None


def binding_goal_snapshot(
    goal: ThreadGoalSummary | None,
) -> BindingGoalSnapshot | None:
    """Project an adapter goal DTO into the canonical binding snapshot."""

    if goal is None:
        return None
    if not isinstance(goal, ThreadGoalSummary):
        raise TypeError("binding goal projection requires ThreadGoalSummary")
    return BindingGoalSnapshot(
        objective=goal.objective,
        status=goal.status,
        token_budget=goal.token_budget,
        tokens_used=goal.tokens_used,
        time_used_seconds=goal.time_used_seconds,
        created_at=goal.created_at,
        updated_at=goal.updated_at,
    )


class FeishuThreadSessionCoordinator:
    """Own Feishu create/resume ACK through exact local settlement."""

    def __init__(
        self,
        *,
        lock: ContextManager[Any],
        adapter: FeishuThreadSessionAdapter,
        binding_runtime: FeishuThreadSessionBindingRuntime,
        binding_transitions: FeishuBindingTransitionOwner,
        thread_runtime: FeishuThreadSessionRuntimeAuthority,
        root_operations: FeishuThreadSessionRootOperations,
        warnings: FeishuThreadSessionWarnings,
        ports: FeishuThreadSessionPorts,
    ) -> None:
        self._require_methods(
            adapter,
            "adapter",
            ("read_thread", "get_thread_goal"),
        )
        self._require_methods(
            binding_runtime,
            "binding runtime",
            ("resolve_session", "project_thread_goal_locked"),
        )
        if not isinstance(binding_transitions, FeishuBindingTransitionOwner):
            raise TypeError("Feishu thread session requires binding transition owner")
        self._require_methods(
            thread_runtime,
            "thread runtime authority",
            (
                "create_and_commit_thread",
                "begin_resume_thread",
                "unsubscribe_thread",
            ),
        )
        self._require_methods(
            root_operations,
            "Feishu submission owner",
            ("commit_resume_owner",),
        )
        self._require_methods(warnings, "warning sink", ("record",))
        if type(ports) is not FeishuThreadSessionPorts:
            raise TypeError("Feishu thread session requires exact typed ports")
        if any(
            not callable(getattr(ports, name))
            for name in ports.__dataclass_fields__
        ):
            raise TypeError("Feishu thread session ports must all be callable")
        self._lock = lock
        self._adapter = adapter
        self._binding_runtime = binding_runtime
        self._binding_transitions = binding_transitions
        self._thread_runtime = thread_runtime
        self._root_operations = root_operations
        self._warnings = warnings
        self._ports = ports

    def create_and_bind_thread(
        self,
        sender_id: str,
        chat_id: str,
        *,
        cwd: str,
        message_id: str = "",
        config_overrides: dict[str, Any] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> ThreadSnapshot:
        """Commit one thread/start response to its exact Feishu binding."""

        owner_session = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        self._preflight_replaced_binding_owner(
            owner_session,
            replacement_thread_id="",
        )

        def commit_binding(snapshot: ThreadSnapshot) -> _FeishuBindingCommit:
            if not isinstance(snapshot, ThreadSnapshot):
                raise TypeError("thread/start did not return ThreadSnapshot")
            verified = self._require_direct_thread_summary(
                snapshot.summary,
                expected_thread_id=snapshot.summary.thread_id,
                operation="绑定飞书会话",
            )
            return self._commit_resolved_binding_owner(owner_session, verified)

        try:
            created = self._thread_runtime.create_and_commit_thread(
                local_commit=commit_binding,
                cwd=cwd,
                config_overrides=config_overrides,
                model=model,
                model_provider=model_provider,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
            )
        except ThreadCreateLocalCommitFailed as exc:
            self._warnings.record(
                code="thread_create_local_commit_failed",
                source="ThreadRuntimeAuthority",
                message=(
                    "thread/start succeeded but Focus did not finish its local "
                    "Feishu setup; the thread remains discoverable in inventory."
                ),
                details={
                    "attempt_id": exc.attempt_id,
                    "thread_id": exc.thread_id,
                    "stage": exc.stage,
                },
            )
            raise
        commit = created.local_result
        if type(commit) is not _FeishuBindingCommit:
            raise RuntimeError("thread/create returned an invalid Feishu local commit")
        self._finish_binding(commit)
        return created.response

    def resume_and_commit_feishu_binding(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str,
        failure_policy: ThreadResumeLocalFailurePolicy,
        summary: ThreadSummary | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        message_id: str = "",
        exact_mutation_guard: Callable[[], bool] | None = None,
        active_observer: bool = False,
    ) -> ThreadSnapshot:
        """Settle resume ACK, binding commit, and old runtime cleanup."""

        if type(active_observer) is not bool:
            raise TypeError("active_observer must be an exact bool")

        owner_session = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        self._preflight_replaced_binding_owner(
            owner_session,
            replacement_thread_id=thread_id,
        )
        pending = self._begin_resume_snapshot_by_id(
            thread_id,
            original_arg=original_arg,
            summary=summary,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
            exact_mutation_guard=exact_mutation_guard,
        )
        snapshot = pending.response

        def commit_binding() -> _FeishuBindingCommit:
            verified = self._require_direct_thread_summary(
                snapshot.summary,
                expected_thread_id=snapshot.summary.thread_id,
                operation="绑定飞书会话",
            )
            if not active_observer:
                return self._commit_resolved_binding_owner(
                    owner_session,
                    verified,
                )
            prepared = self._ports.prepare_active_observer(snapshot)
            if prepared is None:
                return self._commit_resolved_binding_owner(
                    owner_session,
                    verified,
                )
            with self._lock:
                try:
                    execution = self._ports.prime_active_observer(
                        owner_session,
                        prepared,
                    )
                    self._require_staged_active_observer(
                        owner_session,
                        prepared,
                        execution,
                    )
                    commit = self._commit_resolved_binding_owner(
                        execution.session,
                        verified,
                    )
                except BaseException:
                    self._ports.rollback_active_observer(
                        owner_session,
                        prepared,
                    )
                    raise
                committed_session = commit.transition.session
                self._require_committed_active_observer(
                    committed_session,
                    prepared,
                )
                return _FeishuBindingCommit(
                    summary=commit.summary,
                    transition=commit.transition,
                    active_observer_execution=ActiveObserverExecution(
                        session=committed_session,
                        turn_id=prepared.turn_id,
                    ),
                )

        try:
            commit = pending.commit_local_state(
                commit_binding,
                failure_policy=failure_policy,
            )
        except ThreadResumeLocalCommitFailed as exc:
            if exc.recovery_required:
                self._warnings.record(
                    code="runtime_attach_recovery_required",
                    source="ThreadRuntimeAuthority",
                    message=(
                        "Runtime attach local commit failed and recovery "
                        "remains pending."
                    ),
                    details={"thread_id": pending.lease_receipt.thread_id},
                )
            raise
        except ThreadResumeSettlementError:
            self._warnings.record(
                code="runtime_attach_receipt_commit_failed",
                source="ThreadRuntimeAuthority",
                message=(
                    "Runtime attach binding committed but its lease receipt "
                    "did not."
                ),
                details={"thread_id": pending.lease_receipt.thread_id},
            )
            raise
        if type(commit) is not _FeishuBindingCommit:
            raise RuntimeError("thread/resume returned an invalid Feishu local commit")
        self._finish_binding(commit)
        if commit.active_observer_execution is not None:
            self._present_active_observer(commit.active_observer_execution)
        return snapshot

    @staticmethod
    def _require_staged_active_observer(
        owner_session: BindingSessionSnapshot,
        prepared: ActiveObserverResumeSnapshot,
        execution: object,
    ) -> None:
        if type(execution) is not ActiveObserverExecution:
            raise RuntimeError(
                "active observer prime returned an invalid execution"
            )
        session = execution.session
        if (
            session.handle is not owner_session.handle
            or session.binding != owner_session.binding
            or session.current_thread_id != owner_session.current_thread_id
            or session.thread.feishu_runtime_state != FEISHU_RUNTIME_DETACHED
        ):
            raise RuntimeError(
                "active observer prime returned a mismatched session"
            )
        if (
            execution.turn_id != prepared.turn_id
            or not session.running
            or session.execution.current_turn_id != prepared.turn_id
            or session.execution.current_execution_kind
            != ACTIVE_OBSERVER_EXECUTION_KIND
        ):
            raise RuntimeError(
                "active observer prime returned a mismatched turn"
            )

    @staticmethod
    def _require_committed_active_observer(
        session: BindingSessionSnapshot,
        prepared: ActiveObserverResumeSnapshot,
    ) -> None:
        if (
            session.thread.feishu_runtime_state != FEISHU_RUNTIME_ATTACHED
            or not session.running
            or session.execution.current_turn_id != prepared.turn_id
            or session.execution.current_execution_kind
            != ACTIVE_OBSERVER_EXECUTION_KIND
        ):
            raise RuntimeError(
                "active observer binding committed without its exact anchor"
            )

    def _present_active_observer(
        self,
        execution: ActiveObserverExecution,
    ) -> None:
        try:
            result = self._ports.present_active_observer(execution)
        except Exception as exc:
            logger.exception(
                "active observer execution committed but page presentation failed: "
                "thread=%s",
                execution.session.current_thread_id[:12],
            )
            self._warnings.record(
                code="active_observer_presentation_failed",
                source="FeishuThreadSessionCoordinator",
                message=(
                    "Feishu active observer attached with an exact current-turn "
                    "anchor, but its execution page could not be presented."
                ),
                details={
                    "thread_id": execution.session.current_thread_id,
                    "turn_id": execution.turn_id,
                    "error": str(exc),
                },
            )
            self._schedule_active_observer_recovery(execution)
            return
        if type(result) is not ActiveObserverPresentationResult:
            self._warnings.record(
                code="active_observer_presentation_invalid_result",
                source="FeishuThreadSessionCoordinator",
                message=(
                    "Feishu active observer attached, but its output owner "
                    "returned no typed page result."
                ),
                details={
                    "thread_id": execution.session.current_thread_id,
                    "turn_id": execution.turn_id,
                },
            )
            self._schedule_active_observer_recovery(execution)
            return
        self._schedule_active_observer_recovery(execution)
        if result.status in {"opened", "send_unknown"}:
            return
        self._warnings.record(
            code="active_observer_presentation_incomplete",
            source="FeishuActiveObserverController",
            message=(
                "Feishu active observer attached, but its current-turn "
                "execution page is incomplete."
            ),
            details={
                "thread_id": execution.session.current_thread_id,
                "turn_id": result.turn_id,
                "status": result.status,
            },
        )

    def _schedule_active_observer_recovery(
        self,
        execution: ActiveObserverExecution,
    ) -> None:
        try:
            with self._lock:
                current = self._binding_runtime.session_snapshot_locked(
                    execution.session.handle
                )
        except RuntimeError:
            return
        if (
            current.current_thread_id != execution.session.current_thread_id
            or not current.running
            or current.execution.current_turn_id != execution.turn_id
            or current.execution.current_execution_kind
            != ACTIVE_OBSERVER_EXECUTION_KIND
        ):
            return
        try:
            self._ports.schedule_active_observer_recovery(current)
        except Exception as exc:
            logger.exception(
                "active observer recovery watchdog scheduling failed: "
                "thread=%s",
                current.current_thread_id[:12],
            )
            self._warnings.record(
                code="active_observer_recovery_schedule_failed",
                source="FeishuThreadSessionCoordinator",
                message=(
                    "Feishu active observer attached, but its recovery "
                    "watchdog could not be scheduled."
                ),
                details={
                    "thread_id": current.current_thread_id,
                    "turn_id": execution.turn_id,
                    "error": str(exc),
                },
            )

    def resume_and_commit_feishu_operation_owner(
        self,
        admission: FeishuRootOperationToken,
        thread_id: str,
        *,
        original_arg: str,
        failure_policy: ThreadResumeLocalFailurePolicy,
        summary: ThreadSummary | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
    ) -> ThreadSnapshot:
        """Commit a resume ACK against its exact Feishu submission lease."""

        if not isinstance(admission, FeishuRootOperationToken):
            raise TypeError("Feishu operation resume requires an exact admission")
        pending = self._begin_resume_snapshot_by_id(
            thread_id,
            original_arg=original_arg,
            summary=summary,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        try:
            pending.commit_local_state(
                lambda: self._root_operations.commit_resume_owner(admission),
                failure_policy=failure_policy,
            )
        except ThreadResumeLocalCommitFailed as exc:
            if exc.recovery_required:
                self._warnings.record(
                    code="runtime_operation_resume_recovery_required",
                    source="ThreadRuntimeAuthority",
                    message=(
                        "Runtime resume could not verify its exact Feishu "
                        "submission lease; recovery remains pending."
                    ),
                    details={"thread_id": pending.lease_receipt.thread_id},
                )
            raise
        except ThreadResumeSettlementError:
            self._warnings.record(
                code="runtime_operation_resume_receipt_commit_failed",
                source="ThreadRuntimeAuthority",
                message=(
                    "Runtime resume submission committed but its runtime lease "
                    "receipt did not."
                ),
                details={"thread_id": pending.lease_receipt.thread_id},
            )
            raise
        return pending.response

    def reattach_bound_thread(
        self,
        sender_id: str,
        chat_id: str,
        thread_id: str,
        *,
        original_arg: str,
        summary: ThreadSummary,
        retain_on_local_failure: bool,
        message_id: str = "",
        exact_mutation_guard: Callable[[], bool] | None = None,
    ) -> str:
        """Prompt-facing façade for one complete resume/binding transaction."""

        if type(retain_on_local_failure) is not bool:
            raise TypeError("retain_on_local_failure must be an exact bool")
        snapshot = self.resume_and_commit_feishu_binding(
            sender_id,
            chat_id,
            thread_id,
            original_arg=original_arg,
            summary=summary,
            failure_policy=(
                ThreadResumeLocalFailurePolicy.RETAIN
                if retain_on_local_failure
                else ThreadResumeLocalFailurePolicy.COMPENSATE
            ),
            message_id=message_id,
            exact_mutation_guard=exact_mutation_guard,
        )
        return snapshot.summary.thread_id

    def bind_thread(
        self,
        sender_id: str,
        chat_id: str,
        thread: ThreadSummary,
        *,
        message_id: str = "",
    ) -> None:
        """Bind an already-authorized summary and own lease compensation."""

        owner_session = self._binding_runtime.resolve_session(
            sender_id,
            chat_id,
            message_id,
        )
        self._preflight_replaced_binding_owner(
            owner_session,
            replacement_thread_id=thread.thread_id,
        )
        verified = self._require_direct_thread_summary(
            thread,
            expected_thread_id=thread.thread_id,
            operation="绑定飞书会话",
        )
        lease_was_newly_acquired = self._ports.acquire_runtime_lease(
            verified.thread_id
        )
        try:
            commit = self._commit_resolved_binding_owner(owner_session, verified)
        except Exception:
            if lease_was_newly_acquired:
                self._ports.release_runtime_lease(verified.thread_id)
            raise
        self._finish_binding(commit)

    def _begin_resume_snapshot_by_id(
        self,
        thread_id: str,
        *,
        original_arg: str,
        summary: ThreadSummary | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        exact_mutation_guard: Callable[[], bool] | None = None,
    ) -> PendingThreadResume[ThreadSnapshot]:
        """Begin an upstream resume whose local receipt remains unsettled."""

        normalized_thread_id, thread, resume_kwargs = self._prepare_resume(
            thread_id,
            original_arg=original_arg,
            summary=summary,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        try:
            return self._thread_runtime.begin_resume_thread(
                normalized_thread_id,
                exact_mutation_guard=exact_mutation_guard,
                **resume_kwargs,
            )
        except Exception as exc:
            self._raise_resume_error(
                exc,
                original_arg=original_arg,
                thread=thread,
            )

    def _prepare_resume(
        self,
        thread_id: str,
        *,
        original_arg: str,
        summary: ThreadSummary | None,
        model: str | None,
        reasoning_effort: str | None,
        approval_policy: str | None,
        permissions_profile_id: str | None,
    ) -> tuple[str, ThreadSummary | None, dict[str, Any]]:
        normalized_thread_id = str(thread_id or "").strip()
        del summary
        thread = self.read_direct_thread_summary(
            normalized_thread_id,
            original_arg=original_arg,
            operation="恢复",
        )
        config_overrides = (
            {"model_reasoning_effort": reasoning_effort}
            if reasoning_effort
            else None
        )
        return normalized_thread_id, thread, {
            "config_overrides": config_overrides,
            "model": model or None,
            "approval_policy": approval_policy or None,
            "permissions_profile_id": permissions_profile_id or None,
        }

    def _commit_resolved_binding_owner(
        self,
        owner_session: BindingSessionSnapshot,
        verified_thread: ThreadSummary,
    ) -> _FeishuBindingCommit:
        self._preflight_replaced_binding_owner(
            owner_session,
            replacement_thread_id=verified_thread.thread_id,
        )
        transition = self._binding_transitions.bind_thread(
            BindFeishuThreadCommand(
                session=owner_session,
                thread_id=verified_thread.thread_id,
                thread_title=verified_thread.title,
                working_dir=verified_thread.cwd or None,
            )
        )
        return _FeishuBindingCommit(
            summary=verified_thread,
            transition=transition,
        )

    def _preflight_replaced_binding_owner(
        self,
        owner_session: BindingSessionSnapshot,
        *,
        replacement_thread_id: str,
    ) -> None:
        if type(owner_session) is not BindingSessionSnapshot:
            raise TypeError("Feishu thread session requires an exact binding session")
        del replacement_thread_id

    def _finish_binding(self, commit: _FeishuBindingCommit) -> None:
        summary = commit.summary
        transition = commit.transition
        try:
            self._ports.remember_direct_thread_summary(summary)
        except Exception:
            logger.exception(
                "binding commit 后记录 direct thread 证据失败: thread=%s",
                summary.thread_id[:12],
            )

        old_thread_id = transition.unsubscribe_thread_id.strip()
        if old_thread_id:
            unsubscribe_succeeded = False
            try:
                if not self._ports.runtime_interest_retained(old_thread_id):
                    self._thread_runtime.unsubscribe_thread(old_thread_id)
                unsubscribe_succeeded = True
            except Exception:
                logger.exception(
                    "切换 binding 后回收旧 thread 订阅失败: thread=%s",
                    old_thread_id[:12],
                )
            if unsubscribe_succeeded:
                try:
                    if not self._ports.runtime_interest_retained(old_thread_id):
                        self._ports.release_runtime_lease(old_thread_id)
                except Exception:
                    logger.exception(
                        "切换 binding 后释放旧 runtime lease 失败: thread=%s",
                        old_thread_id[:12],
                    )

        try:
            goal = self._adapter.get_thread_goal(summary.thread_id)
        except Exception:
            logger.debug(
                "读取 thread goal 失败: thread=%s",
                summary.thread_id[:12],
                exc_info=True,
            )
            return
        try:
            with self._lock:
                projected = self._binding_runtime.project_thread_goal_locked(
                    transition.session.handle,
                    binding_goal_snapshot(goal),
                    expected_thread_id=summary.thread_id,
                )
            if projected is None:
                raise RuntimeError("binding changed before goal projection")
        except Exception:
            logger.exception(
                "绑定 commit 后刷新 goal projection 失败: thread=%s",
                summary.thread_id[:12],
            )

    def read_direct_thread_summary(
        self,
        thread_id: str,
        *,
        original_arg: str,
        operation: str,
    ) -> ThreadSummary:
        """Authority-read one directly manageable Feishu root target."""

        try:
            summary = read_direct_thread_target(
                thread_id,
                read_thread=lambda target_id: self._read_thread_snapshot(
                    target_id,
                    original_arg=original_arg,
                ),
                operation=operation,
            )
            self._ports.remember_direct_thread_summary(summary)
            return summary
        except DirectThreadTargetPolicyError as exc:
            raise ValueError(str(exc)) from exc

    def _read_thread_snapshot(
        self,
        thread_id: str,
        *,
        original_arg: str,
    ) -> ThreadSnapshot:
        try:
            return self._adapter.read_thread(thread_id, include_turns=False)
        except Exception as exc:
            if self._ports.is_thread_not_found_error(exc):
                raise ValueError(
                    f"未找到匹配的线程：`{original_arg}`"
                ) from exc
            raise

    @staticmethod
    def _require_direct_thread_summary(
        summary: ThreadSummary,
        *,
        expected_thread_id: str,
        operation: str,
    ) -> ThreadSummary:
        try:
            return require_direct_thread_target(
                summary,
                expected_thread_id=expected_thread_id,
                operation=operation,
            )
        except DirectThreadTargetPolicyError as exc:
            raise ValueError(str(exc)) from exc

    def _raise_resume_error(
        self,
        exc: Exception,
        *,
        original_arg: str,
        thread: ThreadSummary | None,
    ) -> NoReturn:
        if self._ports.is_thread_not_found_error(exc):
            raise ValueError(f"未找到匹配的线程：`{original_arg}`") from exc
        if (
            thread is not None
            and thread.source == "cli"
            and self._ports.is_transport_disconnect(exc)
        ):
            raise RuntimeError(
                "Codex 当前无法通过 app-server 恢复这个 CLI 线程。"
                "这通常意味着该线程正被本地 TUI 使用，或当前版本暂不支持加载它的完整历史。"
            ) from exc
        raise exc

    @staticmethod
    def _require_methods(
        owner: object,
        label: str,
        methods: tuple[str, ...],
    ) -> None:
        if any(not callable(getattr(owner, method, None)) for method in methods):
            raise TypeError(f"Feishu thread session requires {label}")
