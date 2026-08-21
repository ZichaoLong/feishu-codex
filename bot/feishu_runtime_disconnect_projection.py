"""RuntimeLoop owner for projecting backend loss into Feishu runtime state."""

from __future__ import annotations

from dataclasses import dataclass

from bot.binding_execution_runtime import (
    BindingExecutionRuntimeTransitions,
    PrepareBindingDisconnectCommand,
)
from bot.runtime_loop import RuntimeContextGuard


ChatBindingKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class FeishuRuntimeDisconnectReport:
    """Exact bindings whose attached runtime was affected by backend loss."""

    affected_bindings: tuple[ChatBindingKey, ...] = ()


class FeishuRuntimeDisconnectProjection:
    """Apply the local terminal error without exposing raw runtime state."""

    def __init__(
        self,
        *,
        execution_runtime: BindingExecutionRuntimeTransitions,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError(
                "Feishu runtime disconnect projection requires a RuntimeLoop "
                "context guard."
            )
        self._execution_runtime = execution_runtime
        self._runtime_context_guard = runtime_context_guard

    def prepare(self) -> FeishuRuntimeDisconnectReport:
        """Record terminal errors before the runtime authority is detached."""

        self._runtime_context_guard()
        affected_bindings = self._execution_runtime.prepare_disconnect(
            PrepareBindingDisconnectCommand(
                error_message="Codex websocket disconnected",
            )
        )
        return FeishuRuntimeDisconnectReport(affected_bindings)
