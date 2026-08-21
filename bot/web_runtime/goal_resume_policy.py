"""Single Web policy for persisted-goal resume safety."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bot.adapters.base import ThreadGoalSummary
from bot.codex_protocol.client import CodexRpcError
from bot.goal_continuation_policy import goal_status_may_continue
from bot.runtime_loop import RuntimeContextGuard
from bot.web_runtime.contract import WebRuntimeError


@dataclass(frozen=True, slots=True)
class WebGoalResumePorts:
    get_thread_goal: Callable[[str], ThreadGoalSummary | None]


class WebGoalResumePolicy:
    """Own goal query normalization and continuation-safe Web decisions."""

    def __init__(
        self,
        *,
        ports: WebGoalResumePorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web goal-resume policy requires a RuntimeLoop guard")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def read(self, thread_id: str) -> ThreadGoalSummary | None:
        self._runtime_context_guard()
        try:
            return self._ports.get_thread_goal(thread_id)
        except CodexRpcError as exc:
            message = str(exc.error.get("message", "") or "").strip().lower()
            if message == "goals feature is disabled":
                return None
            raise

    @staticmethod
    def requires_writer_admission(goal: ThreadGoalSummary | None) -> bool:
        """Fail closed for every unreviewed future goal status."""

        if goal is None:
            return False
        return goal_status_may_continue(goal.status)

    def require_safe_for_new_resume(
        self,
        thread_id: str,
        *,
        operation: str,
    ) -> ThreadGoalSummary | None:
        """Reject an unrelated action before a continuation-capable resume."""

        self._runtime_context_guard()
        try:
            goal = self.read(thread_id)
        except Exception as exc:
            raise WebRuntimeError(
                "Focus could not safely determine whether this thread has a resumable goal. "
                "Refresh and resolve the goal before starting new work.",
                code="goal_state_unconfirmed",
                status=409,
                details={"thread_id": thread_id, "operation": operation},
            ) from exc
        if not self.requires_writer_admission(goal):
            return goal
        goal_status = str((goal.status if goal is not None else "") or "").strip()
        raise WebRuntimeError(
            "This thread has an active or unreviewed persisted goal. Focus will not "
            f"resume it while trying to {operation}; pause or clear that goal explicitly first.",
            code="goal_continuation_requires_resolution",
            status=409,
            details={
                "thread_id": thread_id,
                "goal_status": goal_status,
                "operation": operation,
            },
        )

    def check_post_resume(
        self,
        thread_id: str,
        *,
        operation: str,
    ) -> ThreadGoalSummary | None:
        """Project a resumed goal without treating the read as a barrier."""

        self._runtime_context_guard()
        try:
            goal = self.read(thread_id)
        except Exception as exc:
            raise WebRuntimeError(
                "Codex resumed the thread, but Focus could not confirm whether its goal "
                "started work. Do not submit another action automatically; refresh or "
                "resolve the goal first.",
                code="goal_state_unconfirmed",
                status=409,
                details={
                    "thread_id": thread_id,
                    "operation": operation,
                },
            ) from exc
        if not self.requires_writer_admission(goal):
            # A post-resume point read is not causal evidence that a listener
            # will not subsequently continue the persisted goal.
            return goal
        raise WebRuntimeError(
            "The resumed thread has an active or unreviewed persisted goal. Focus did "
            f"not submit the requested {operation}; pause or clear the goal explicitly first.",
            code="goal_continuation_requires_resolution",
            status=409,
            details={
                "thread_id": thread_id,
                "goal_status": str(
                    (goal.status if goal is not None else "") or ""
                ).strip(),
                "operation": operation,
            },
        )
