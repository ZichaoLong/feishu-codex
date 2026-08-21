"""Web thread mutation and lifecycle transaction owner.

The coordinator owns the ordering of direct-target proof, active-turn admission,
upstream mutation, process-local unknown-outcome evidence, local lifecycle
convergence, and projection publication.  It stores no thread, owner, goal,
attachment, or mutation fact: those remain in their existing state owners.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot
from bot.codex_protocol.client import CodexRpcError
from bot.goal_continuation_policy import goal_status_may_continue
from bot.runtime_loop import RuntimeContextGuard
from bot.runtime_state import is_confirmed_inactive_backend_thread_status
from bot.thread_lifecycle_service import ThreadLifecyclePolicyError
from bot.turn_interrupt_audit import (
    TurnInterruptSource,
    record_turn_interrupt_dispatch_attempt,
)
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.projection import FocusWebProjection, project_goal
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.event_coordinator import WebRuntimeEventCoordinator
from bot.web_runtime.writer_workspace_coordinator import (
    WebWriterWorkspaceCoordinator,
    accept_web_document_intent,
    require_connected_web_document,
    require_web_client_id,
    require_web_thread_id,
)


@dataclass(frozen=True, slots=True)
class WebLifecycleTargetReaderPorts:
    """The one direct app-server read used for lifecycle verification."""

    read_thread: Callable[[str, bool], ThreadSnapshot]


class WebLifecycleTargetReader:
    """Own the wire-to-lifecycle-state interpretation for unknown recovery.

    A direct ``thread/read`` is authoritative for absence after Focus already
    persisted a lifecycle target.  For a present rollout, the upstream-provided
    path is currently the only archive distinction.  A path-less response is
    deliberately insufficient and stays fail-closed.
    """

    def __init__(
        self,
        *,
        ports: WebLifecycleTargetReaderPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(ports, WebLifecycleTargetReaderPorts):
            raise TypeError("Web lifecycle target reader requires typed ports")
        if not callable(runtime_context_guard):
            raise TypeError(
                "Web lifecycle target reader requires a RuntimeLoop context guard"
            )
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def read(self, thread_id: str) -> str:
        self._runtime_context_guard()
        normalized_thread_id = require_web_thread_id(thread_id)
        try:
            snapshot = self._ports.read_thread(normalized_thread_id, False)
        except Exception as exc:
            if self._is_authoritative_absence(exc):
                return "deleted"
            raise WebRuntimeError(
                "Focus could not authoritatively read this lifecycle target. "
                "Keep it locked and try again later.",
                code="lifecycle_verification_unavailable",
                status=503,
                details={"thread_id": normalized_thread_id},
            ) from exc

        path = str(snapshot.summary.path or "").strip()
        if not path:
            raise WebRuntimeError(
                "Codex did not provide enough lifecycle state to distinguish an "
                "archived thread. Keep it locked and wait for an authoritative "
                "notification.",
                code="lifecycle_verification_unavailable",
                status=503,
                details={"thread_id": normalized_thread_id},
            )
        components = tuple(
            component for component in path.replace("\\", "/").split("/") if component
        )
        return "archived" if "archived_sessions" in components else "present"

    @staticmethod
    def _is_authoritative_absence(exc: Exception) -> bool:
        """Interpret direct-read wire absence in exactly one Web owner."""

        if isinstance(exc, CodexRpcError):
            message = str(exc.error.get("message", "") or "").strip().lower()
            if "not found" in message or "does not exist" in message:
                return True
            # Direct thread/read first checks persisted sessions (including
            # archives) and then loaded threads.  In this exact recovery flow,
            # a previously persisted lifecycle target returning not-loaded is
            # therefore authoritative absence rather than an observer-resume
            # failure.
            return exc.method == "thread/read" and "thread not loaded" in message
        if isinstance(exc, ValueError):
            message = str(exc).strip().lower()
            return "未找到匹配的线程" in message or message == "unknown thread"
        return False


@dataclass(frozen=True, slots=True)
class WebThreadMutationPorts:
    """Upstream and lifecycle transports; mutable owners are direct inputs."""

    read_thread: Callable[[str, bool], ThreadSnapshot]
    rename_thread: Callable[[str, str], None]
    set_thread_goal: Callable[..., ThreadGoalSummary]
    clear_thread_goal: Callable[..., bool]
    archive_thread: Callable[..., dict[str, Any]]
    unarchive_thread: Callable[..., dict[str, Any]]
    delete_thread: Callable[..., dict[str, Any]]
    interrupt_turn: Callable[..., None]


def require_confirmed_inactive_web_thread(
    status: object,
    *,
    operation: str,
) -> None:
    """Apply the shared fail-closed status policy for Web commands."""

    normalized_status = str(status or "").strip()
    if normalized_status == "active":
        raise WebRuntimeError(
            f"The thread is active; wait for the current turn to finish before {operation}.",
            code="thread_active",
            status=409,
        )
    if is_confirmed_inactive_backend_thread_status(normalized_status):
        return
    raise WebRuntimeError(
        "Focus could not confirm that the thread is inactive; refresh and retry "
        "after its state settles.",
        code="thread_state_unconfirmed",
        status=409,
    )


class WebThreadMutationCoordinator:
    """Run Web thread mutations on RuntimeLoop without owning their facts."""

    def __init__(
        self,
        *,
        documents: WebDocumentRegistry,
        direct_targets: WebDirectThreadTargetCoordinator,
        operations: WebOperationService,
        lifecycle_targets: WebLifecycleTargetReader,
        goal_policy: WebGoalResumePolicy,
        workspace: WebWriterWorkspaceCoordinator,
        events: WebRuntimeEventCoordinator,
        projection: FocusWebProjection,
        ports: WebThreadMutationPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(operations, WebOperationService):
            raise TypeError("Web thread mutation requires the operation owner")
        if not isinstance(lifecycle_targets, WebLifecycleTargetReader):
            raise TypeError("Web thread mutation requires the lifecycle target reader")
        if not isinstance(ports, WebThreadMutationPorts):
            raise TypeError("Web thread mutation requires typed ports")
        if not callable(runtime_context_guard):
            raise TypeError("Web thread mutation requires a RuntimeLoop context guard")
        self._documents = documents
        self._direct_targets = direct_targets
        self._operations = operations
        self._lifecycle_targets = lifecycle_targets
        self._goal_policy = goal_policy
        self._workspace = workspace
        self._events = events
        self._projection = projection
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def rename_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        name: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise WebRuntimeError(
                "Thread name must not be empty.",
                code="invalid_name",
            )
        self._direct_targets.read(normalized_thread_id, operation="重命名")
        self._operations.raise_other_writer(
            normalized_client_id,
            normalized_thread_id,
        )
        self._operations.run_writer_scoped_control_mutation(
            normalized_client_id,
            normalized_thread_id,
            operation="rename",
            call=lambda: self._ports.rename_thread(
                normalized_thread_id,
                normalized_name,
            ),
        )
        self._projection.publish(
            "thread_delta",
            thread_id=normalized_thread_id,
            reason="web_thread_renamed",
            detail={
                "method": "thread/name/updated",
                "thread_name": normalized_name,
            },
        )
        return {
            "accepted": True,
            "thread_id": normalized_thread_id,
            "name": normalized_name,
        }

    def goal(self, client_id: str, thread_id: str) -> dict[str, Any]:
        self._runtime_context_guard()
        require_web_client_id(client_id)
        normalized_thread_id = require_web_thread_id(thread_id)
        self._direct_targets.read(normalized_thread_id, operation="查看目标")
        return {
            **self._projection.coordinates(),
            "thread_id": normalized_thread_id,
            "goal": project_goal(self._goal_policy.read(normalized_thread_id)),
        }

    def set_goal(
        self,
        client_id: str,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        accept_web_document_intent(
            self._documents,
            normalized_client_id,
            intent_generation,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        summary = self._direct_targets.read(
            normalized_thread_id,
            operation="设置目标",
        ).summary
        normalized_status = None if status is None else str(status or "").strip()
        normalized_objective = (
            None if objective is None else str(objective or "").strip()
        )
        current_status = str(summary.status or "").strip()
        if (
            current_status != "active"
            and not is_confirmed_inactive_backend_thread_status(current_status)
        ):
            require_confirmed_inactive_web_thread(
                current_status,
                operation="set a goal",
            )

        autonomous = None
        if goal_status_may_continue(normalized_status):
            autonomous = self._operations.admit_autonomous_turn(
                normalized_client_id,
                normalized_thread_id,
                allow_fresh=current_status != "active",
            )
        elif current_status == "active":
            self._operations.require_active_turn_writer(
                normalized_client_id,
                normalized_thread_id,
            )
        else:
            self._operations.raise_other_writer(
                normalized_client_id,
                normalized_thread_id,
            )
        try:
            self._operations.admit_explicit_web_effect(
                normalized_client_id,
                normalized_thread_id,
                operation="set_goal",
            )
            result = self._ports.set_thread_goal(
                normalized_thread_id,
                summary=summary,
                objective=normalized_objective,
                status=normalized_status,
            )
            if self._operations.upstream_outcome_unknown(result):
                pending = self._operations.record_unknown_mutation(
                    normalized_thread_id,
                    operation="set_goal",
                    client_id=normalized_client_id,
                )
                raise self._operations.mutation_unknown_error(pending)
        except Exception as exc:
            if isinstance(exc, WebRuntimeError) and exc.code == "mutation_unknown":
                raise
            if self._operations.is_unknown_mutation_error(exc):
                pending = self._operations.record_unknown_mutation(
                    normalized_thread_id,
                    operation="set_goal",
                    client_id=normalized_client_id,
                )
                raise self._operations.mutation_unknown_error(pending) from exc
            if autonomous is not None:
                self._operations.release_fresh_blank_autonomous_turn(
                    autonomous,
                    reason="web_goal_mutation_failed",
                )
            raise

        if (
            not isinstance(result, ThreadGoalSummary)
            or str(result.thread_id or "").strip() != normalized_thread_id
        ):
            self._projection.publish(
                "thread_invalidated",
                thread_id=normalized_thread_id,
                reason="web_goal_mutation_result_unconfirmed",
            )
            raise WebRuntimeError(
                "Codex accepted the goal update, but Focus could not verify its "
                "typed result. Refresh this thread before making another action.",
                code="goal_state_unconfirmed",
                status=409,
                details={"thread_id": normalized_thread_id},
            )

        goal = result
        if autonomous is not None and not self._goal_policy.requires_writer_admission(
            goal
        ):
            self._operations.release_fresh_blank_autonomous_turn(
                autonomous,
                reason="web_goal_mutation_known_no_start",
            )
        projected_goal = project_goal(goal)
        self._projection.publish(
            "thread_delta",
            thread_id=normalized_thread_id,
            reason="web_goal_updated",
            detail={"method": "thread/goal/updated", "goal": projected_goal},
        )
        return {
            **self._projection.coordinates(),
            "thread_id": normalized_thread_id,
            "goal": projected_goal,
        }

    def clear_goal(
        self,
        client_id: str,
        thread_id: str,
        *,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        accept_web_document_intent(
            self._documents,
            normalized_client_id,
            intent_generation,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        summary = self._direct_targets.read(
            normalized_thread_id,
            operation="清除目标",
        ).summary
        current_status = str(summary.status or "").strip()
        if (
            current_status != "active"
            and not is_confirmed_inactive_backend_thread_status(current_status)
        ):
            require_confirmed_inactive_web_thread(
                current_status,
                operation="clear a goal",
            )

        if current_status == "active":
            self._operations.require_active_turn_writer(
                normalized_client_id,
                normalized_thread_id,
            )
        else:
            self._operations.raise_other_writer(
                normalized_client_id,
                normalized_thread_id,
            )
        result = self._operations.run_writer_scoped_control_mutation(
            normalized_client_id,
            normalized_thread_id,
            operation="clear_goal",
            call=lambda: self._ports.clear_thread_goal(
                normalized_thread_id,
                summary=summary,
            ),
        )

        if not isinstance(result, bool):
            self._projection.publish(
                "thread_invalidated",
                thread_id=normalized_thread_id,
                reason="web_goal_clear_result_unconfirmed",
            )
            raise WebRuntimeError(
                "Codex accepted clearing the goal, but Focus could not verify its "
                "typed result. Refresh this thread before making another action.",
                code="goal_state_unconfirmed",
                status=409,
                details={"thread_id": normalized_thread_id},
            )

        self._projection.publish(
            "thread_delta",
            thread_id=normalized_thread_id,
            reason="web_goal_cleared",
            detail={"method": "thread/goal/cleared", "goal": project_goal(None)},
        )
        return {
            **self._projection.coordinates(),
            "thread_id": normalized_thread_id,
            "goal": project_goal(None),
            "cleared": result,
        }

    def archive_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        self._direct_targets.read(normalized_thread_id, operation="归档")
        self._operations.raise_other_writer(
            normalized_client_id,
            normalized_thread_id,
        )
        self._require_inactive_lifecycle_thread(
            normalized_thread_id,
            operation="archive",
        )
        return self._run_lifecycle_mutation(
            normalized_client_id,
            normalized_thread_id,
            operation="archive",
            call=lambda holder: self._ports.archive_thread(
                normalized_thread_id,
                writer_holder=holder,
            ),
            on_success=lambda result: self._archive_success(
                normalized_thread_id,
                result,
            ),
        )

    def unarchive_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        self._direct_targets.read(normalized_thread_id, operation="取消归档")
        self._operations.raise_other_writer(
            normalized_client_id,
            normalized_thread_id,
        )
        return self._run_lifecycle_mutation(
            normalized_client_id,
            normalized_thread_id,
            operation="unarchive",
            call=lambda holder: self._ports.unarchive_thread(
                normalized_thread_id,
                writer_holder=holder,
            ),
            on_success=lambda result: result,
        )

    def delete_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        confirmation: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        self._direct_targets.read(normalized_thread_id, operation="删除")
        if str(confirmation or "").strip() != normalized_thread_id:
            raise WebRuntimeError(
                "Delete confirmation must match the full thread id.",
                code="delete_confirmation_required",
            )
        self._operations.raise_other_writer(
            normalized_client_id,
            normalized_thread_id,
        )
        self._require_inactive_lifecycle_thread(
            normalized_thread_id,
            operation="delete",
        )
        return self._run_lifecycle_mutation(
            normalized_client_id,
            normalized_thread_id,
            operation="delete",
            call=lambda holder: self._ports.delete_thread(
                normalized_thread_id,
                writer_holder=holder,
            ),
            on_success=lambda result: self._delete_success(
                normalized_thread_id,
                result,
            ),
        )

    def interrupt(
        self,
        client_id: str,
        thread_id: str,
        *,
        turn_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        if (
            self._documents.materialized_thread_id(normalized_client_id)
            != normalized_thread_id
        ):
            raise WebRuntimeError(
                "This browser document has not materialized the requested thread. "
                "Open it before interrupting its active turn.",
                code="thread_not_materialized",
                status=409,
            )
        if not isinstance(turn_id, str) or turn_id != turn_id.strip():
            raise WebRuntimeError(
                "Interrupt requires an empty or exact turn id.",
                code="invalid_turn_id",
                status=400,
            )
        exact_turn_id = turn_id
        self._direct_targets.read(
            normalized_thread_id,
            operation="中断",
            include_turns=False,
        )

        try:
            record_turn_interrupt_dispatch_attempt(
                source=TurnInterruptSource.WEB_DOCUMENT,
                thread_id=normalized_thread_id,
                turn_id=exact_turn_id,
            )
            self._ports.interrupt_turn(
                thread_id=normalized_thread_id,
                turn_id=exact_turn_id,
            )
        except Exception as exc:
            if self._operations.is_unknown_mutation_error(exc):
                raise WebRuntimeError(
                    "Codex may have received this interrupt, but Focus did not receive its result. "
                    "Wait for the active turn lifecycle before retrying.",
                    code="turn_effect_unknown",
                    status=503,
                    details={
                        "thread_id": normalized_thread_id,
                        "turn_id": exact_turn_id,
                    },
                ) from exc
            if isinstance(exc, CodexRpcError):
                race = self._interrupt_race(exc)
                if race == "missing":
                    raise WebRuntimeError(
                        "The active turn ended before Codex received this interrupt.",
                        code="no_active_turn",
                        status=409,
                        details={
                            "thread_id": normalized_thread_id,
                            "turn_id": exact_turn_id,
                        },
                    ) from exc
                if race == "mismatch":
                    raise WebRuntimeError(
                        "The active turn changed before Codex received this interrupt. "
                        "Refresh and retry.",
                        code="active_turn_changed",
                        status=409,
                        details={
                            "thread_id": normalized_thread_id,
                            "turn_id": exact_turn_id,
                        },
                    ) from exc
            raise
        return {
            "accepted": True,
            "thread_id": normalized_thread_id,
            "turn_id": exact_turn_id,
        }

    @staticmethod
    def _interrupt_race(error: CodexRpcError) -> str | None:
        if error.method != "turn/interrupt":
            return None
        message = str(error.error.get("message", "") or "")
        if message == "no active turn to interrupt":
            return "missing"
        prefix = "expected active turn id "
        separator = " but found "
        if not message.startswith(prefix) or separator not in message:
            return None
        requested, actual = message[len(prefix) :].split(separator, 1)
        return "mismatch" if requested and actual else None

    def resolve_unknown_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        action: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._operations.resolve_unknown_mutation(
            client_id,
            thread_id,
            action=action,
            mutation_id=mutation_id,
        )

    def verify_unknown_lifecycle_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        return self._operations.verify_unknown_lifecycle_mutation(
            client_id,
            thread_id,
            mutation_id=mutation_id,
        )

    def unknown_lifecycle_mutations_for_client(
        self,
        client_id: str,
    ) -> list[dict[str, Any]]:
        self._runtime_context_guard()
        return self._operations.unknown_lifecycle_mutations_for_client(client_id)

    def read_lifecycle_target_state(self, thread_id: str) -> str:
        """Expose the independent reader for composition and direct tests."""

        self._runtime_context_guard()
        return self._lifecycle_targets.read(thread_id)

    def _run_lifecycle_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        operation: str,
        call: Callable[[Any], dict[str, Any]],
        on_success: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        self._operations.admit_explicit_web_effect(
            client_id,
            thread_id,
            operation=operation,
        )
        try:
            result = call(self._operations.holder(client_id))
        except ThreadLifecyclePolicyError as exc:
            raise WebRuntimeError(
                str(exc),
                code="lifecycle_refused",
                status=409,
            ) from exc
        except Exception as exc:
            if self._operations.is_unknown_mutation_error(exc):
                pending = self._operations.record_unknown_mutation(
                    thread_id,
                    operation=operation,
                    client_id=client_id,
                )
                raise self._operations.mutation_unknown_error(pending) from exc
            raise

        if self._operations.upstream_outcome_unknown(result):
            pending = self._operations.record_unknown_mutation(
                thread_id,
                operation=operation,
                client_id=client_id,
            )
            result = {**result, "mutation_id": pending.mutation_id}
        elif self._lifecycle_succeeded(result):
            result = on_success(result)

        self._projection.publish(
            "thread_invalidated",
            thread_id=thread_id,
            reason=f"web_{operation}",
        )
        return result

    def _require_inactive_lifecycle_thread(
        self,
        thread_id: str,
        *,
        operation: str,
    ) -> None:
        snapshot = self._ports.read_thread(thread_id, False)
        require_confirmed_inactive_web_thread(
            snapshot.summary.status,
            operation=operation,
        )

    def _archive_success(
        self,
        thread_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        self._events.drop_thread_after_lifecycle(thread_id)
        return result

    def _delete_success(
        self,
        thread_id: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            self._events.schedule_attachment_cleanup(thread_id)
        except Exception as exc:
            result = dict(result)
            result["focus_cleanup"] = "incomplete"
            result["cleanup_errors"] = [
                *list(result.get("cleanup_errors") or []),
                f"Web attachment cleanup could not be scheduled: {exc}",
            ]
        self._events.drop_thread_after_lifecycle(thread_id)
        return result

    @staticmethod
    def _lifecycle_succeeded(result: dict[str, Any]) -> bool:
        return (
            isinstance(result, dict)
            and str(result.get("upstream_outcome", "") or "").strip() == "success"
        )


__all__ = [
    "WebLifecycleTargetReader",
    "WebLifecycleTargetReaderPorts",
    "WebThreadMutationCoordinator",
    "WebThreadMutationPorts",
    "require_confirmed_inactive_web_thread",
]
