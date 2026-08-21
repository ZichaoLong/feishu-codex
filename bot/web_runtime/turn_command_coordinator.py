"""RuntimeLoop-owned Web turn-command transaction coordinator.

This module owns the exclusive compact and review commands. Ordinary browser
prompts use the staged single-request owner in ``prompt_submission``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.runtime_loop import RuntimeContextGuard
from bot.thread_runtime_authority import ThreadResumeLocalFailurePolicy
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.projection import FocusWebProjection, project_owner
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.thread_mutation_coordinator import (
    require_confirmed_inactive_web_thread,
)
from bot.web_runtime.thread_open_coordinator import WebThreadOpenCoordinator
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.turn_window import DEFAULT_TURN_WINDOW_LIMIT
from bot.web_runtime.writer_workspace_coordinator import (
    require_connected_web_document,
    require_web_thread_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebTurnCommandPorts:
    """Only app-server and runtime-authority effects used by turn commands."""

    compact_thread: Callable[[str], None]
    start_review: Callable[..., dict[str, Any]]


class WebTurnCommandCoordinator:
    """Run one complete existing-thread Web command on RuntimeLoop."""

    def __init__(
        self,
        *,
        documents: WebDocumentRegistry,
        operations: WebOperationService,
        thread_open: WebThreadOpenCoordinator,
        direct_targets: WebDirectThreadTargetCoordinator,
        goal_policy: WebGoalResumePolicy,
        read_model: WebThreadReadModel,
        runtime_interest: WebRuntimeInterestRegistry,
        projection: FocusWebProjection,
        ports: WebTurnCommandPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(ports, WebTurnCommandPorts):
            raise TypeError("Web turn commands require typed ports")
        if not callable(runtime_context_guard):
            raise TypeError("Web turn commands require a RuntimeLoop context guard")
        self._documents = documents
        self._operations = operations
        self._thread_open = thread_open
        self._direct_targets = direct_targets
        self._goal_policy = goal_policy
        self._read_model = read_model
        self._runtime_interest = runtime_interest
        self._projection = projection
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def compact_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        """Resume an inactive direct root and start one compact turn."""

        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        return self._start_exclusive_thread_action(
            normalized_client_id,
            normalized_thread_id,
            action="compact",
            start=lambda: self._ports.compact_thread(normalized_thread_id),
        )

    def start_review(
        self,
        client_id: str,
        thread_id: str,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        """Resume an inactive direct root and start one inline review turn."""

        self._runtime_context_guard()
        normalized_client_id = require_connected_web_document(
            self._documents,
            client_id,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        normalized_target = self._normalize_review_target(target)
        return self._start_exclusive_thread_action(
            normalized_client_id,
            normalized_thread_id,
            action="review",
            start=lambda: self._ports.start_review(
                normalized_thread_id,
                target=normalized_target,
                delivery="inline",
            ),
        )

    def _start_exclusive_thread_action(
        self,
        client_id: str,
        thread_id: str,
        *,
        action: str,
        start: Callable[[], Any],
    ) -> dict[str, Any]:
        direct_operation = {
            "compact": "压缩",
            "review": "发起审查",
        }.get(action, action)
        current = self._direct_targets.read(
            thread_id,
            operation=direct_operation,
        )
        require_confirmed_inactive_web_thread(
            current.summary.status,
            operation=f"start {action}",
        )
        self._goal_policy.require_safe_for_new_resume(
            thread_id,
            operation=f"start {action}",
        )
        self._operations.raise_other_writer(client_id, thread_id)
        self._operations.admit_explicit_web_effect(
            client_id,
            thread_id,
            operation=action,
        )
        submission = self._operations.acquire_exclusive_turn_submission(
            client_id,
            thread_id,
        )
        resume_transaction_pending = False
        resume_committed = False
        resume_writer_released = False
        start_called = False
        try:
            resume_transaction_pending = True
            resumed = self._thread_open.resume_and_commit_web_interest(
                client_id,
                thread_id,
                turn_limit=DEFAULT_TURN_WINDOW_LIMIT,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                model=None,
                config_overrides=None,
                approval_policy=None,
                permissions_profile_id=None,
            )
            resume_transaction_pending = False
            resume_committed = True
            self._read_model.replace_turns(
                thread_id,
                resumed.initial_turns_page.turns,
            )
            self._goal_policy.check_post_resume(
                thread_id,
                operation=action,
            )
            start_called = True
            response = start()
        except Exception as exc:
            unknown_start = bool(
                start_called and self._operations.is_unknown_mutation_error(exc)
            )
            unknown_resume = bool(
                resume_transaction_pending
                and self._operations.is_resume_uncertain_error(exc)
            )
            resume_outcome_unknown = bool(
                unknown_resume and self._operations.is_resume_outcome_unknown(exc)
            )
            if unknown_resume:
                self._runtime_interest.mark_unknown(
                    thread_id,
                    client_id=client_id,
                )
                if resume_outcome_unknown:
                    resume_writer_released = (
                        self._operations.release_exact_blank_turn_submission(
                            submission,
                            reason=f"web_{action}_resume_unknown",
                        )
                    )
                try:
                    self._projection.publish(
                        "thread_invalidated",
                        thread_id=thread_id,
                        reason="web_resume_unknown",
                    )
                except Exception:
                    logger.exception(
                        "Failed to publish Web resume-unknown invalidation: thread=%s action=%s",
                        thread_id[:12],
                        action,
                    )
            post_resume_may_have_started = bool(
                resume_committed
                and isinstance(exc, WebRuntimeError)
                and exc.code
                in {
                    "goal_continuation_requires_resolution",
                    "goal_state_unconfirmed",
                }
            )
            if (
                not unknown_start
                and not unknown_resume
                and not post_resume_may_have_started
            ):
                self._operations.release_exact_blank_turn_submission(
                    submission,
                    reason=f"web_{action}_submission_failed",
                )
            if unknown_start:
                raise self._turn_submission_unknown_error(
                    thread_id,
                    operation=action,
                ) from exc
            if unknown_resume:
                message = (
                    f"Codex resumed this thread, but Focus could not finish its local commit. "
                    f"The {action} action was not submitted; keep this browser session open while Focus reconciles the runtime."
                )
                if resume_outcome_unknown:
                    if resume_writer_released:
                        message = (
                            f"Codex may have resumed this thread, but {action} was not submitted. "
                            "Focus kept read interest but released this action's temporary writer claim. "
                            "Inspect the thread and retry explicitly; Focus will not retry automatically."
                        )
                    else:
                        message = (
                            f"Codex may have resumed this thread, but {action} was not submitted. "
                            "Focus kept read interest but did not remove owner state because this call did not "
                            "still own a fresh exact blank. Refresh before another action."
                        )
                raise WebRuntimeError(
                    message,
                    code="runtime_resume_unknown",
                    status=503,
                    details={"thread_id": thread_id, "operation": action},
                ) from exc
            raise
        payload = response if isinstance(response, dict) else {}
        turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
        turn_id = str(turn.get("id", "") or "").strip()
        writer_lease = submission.lease
        if turn_id:
            writer_lease = self._operations.activate_turn_submission(
                submission,
                turn_id,
            ).lease
        self._projection.publish(
            "thread_invalidated",
            thread_id=thread_id,
            reason=f"web_{action}_started",
        )
        return {
            "accepted": True,
            "action": action,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "owner": project_owner(writer_lease, client_id=client_id),
        }

    @staticmethod
    def _turn_submission_unknown_error(
        thread_id: str,
        *,
        operation: str,
    ) -> WebRuntimeError:
        return WebRuntimeError(
            f"Codex may have accepted this {operation}, but Focus did not receive its result. "
            "Focus will reconcile the exact submission from turn lifecycle events; do not retry it automatically.",
            code="turn_submission_unknown",
            status=503,
            details={"thread_id": thread_id, "operation": operation},
        )

    @staticmethod
    def _normalize_review_target(target: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(target, dict):
            raise WebRuntimeError(
                "Review target must be an object.",
                code="invalid_review_target",
            )
        kind = str(target.get("type", "") or "").strip()
        if kind == "uncommittedChanges":
            return {"type": kind}
        if kind == "baseBranch":
            branch = str(target.get("branch", "") or "").strip()
            if branch:
                return {"type": kind, "branch": branch}
        elif kind == "commit":
            sha = str(target.get("sha", "") or "").strip()
            if sha:
                title = str(target.get("title", "") or "").strip()
                return {"type": kind, "sha": sha, "title": title or None}
        elif kind == "custom":
            instructions = str(target.get("instructions", "") or "").strip()
            if instructions:
                return {"type": kind, "instructions": instructions}
        raise WebRuntimeError(
            "Review target is incomplete or unsupported.",
            code="invalid_review_target",
        )
