"""Closed lifecycle projections for the binding-runtime trust zone."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.binding_identity import ChatBindingKey, format_binding_id
from bot.runtime_state import (
    ExecutionStateChanged,
    RuntimeStateDict,
    ThreadGoalCleared,
)
from bot.turn_execution_coordinator import TurnExecutionCoordinator


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeTimerCancellationEffect:
    """Exact timers detached from one resident binding under its owner lock."""

    binding: ChatBindingKey
    _timers: tuple[Any, ...]

    def __post_init__(self) -> None:
        if type(self.binding) is not tuple or len(self.binding) != 2:
            raise TypeError("timer cancellation effect requires a binding key")
        if type(self._timers) is not tuple:
            raise TypeError("timer cancellation effect requires an exact timer tuple")

    @property
    def timer_count(self) -> int:
        return len(self._timers)

    def cancel(self) -> int:
        """Cancel every detached timer without reopening committed runtime facts."""

        cancelled = 0
        for timer in self._timers:
            try:
                timer.cancel()
            except Exception:
                logger.exception(
                    "binding runtime timer cancellation failed after commit: binding=%s",
                    format_binding_id(self.binding),
                )
            else:
                cancelled += 1
        return cancelled


def cancel_runtime_timer_effects(
    effects: tuple[RuntimeTimerCancellationEffect, ...],
) -> int:
    """Execute immutable timer effects outside the binding mutation lock."""

    if type(effects) is not tuple or any(
        type(effect) is not RuntimeTimerCancellationEffect for effect in effects
    ):
        raise TypeError("runtime timer cancellations require exact typed effects")
    return sum(effect.cancel() for effect in effects)


class BindingRuntimeLifecycleTransitions:
    """Apply the closed lifecycle projection while raw state remains locked."""

    def __init__(self, *, turn_execution: TurnExecutionCoordinator) -> None:
        if not isinstance(turn_execution, TurnExecutionCoordinator):
            raise TypeError("binding lifecycle requires the execution reducer")
        self._turn_execution = turn_execution

    def detach_timers_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
        *,
        patch: bool,
        mirror: bool,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        if type(patch) is not bool or type(mirror) is not bool:
            raise TypeError("timer cancellation selectors must be exact bools")
        timers: list[Any] = []
        if patch:
            registration = state["patch_timer_registration"]
            if registration is not None:
                timers.append(registration.timer)
        if mirror:
            registration = state["mirror_watchdog_registration"]
            if registration is not None and all(
                timer is not registration.timer for timer in timers
            ):
                timers.append(registration.timer)
        if patch or mirror:
            self._turn_execution.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(
                    **(
                        {"patch_timer_registration": None}
                        if patch
                        else {}
                    ),
                    **(
                        {"mirror_watchdog_registration": None}
                        if mirror
                        else {}
                    ),
                ),
            )
        if not timers:
            return ()
        return (
            RuntimeTimerCancellationEffect(
                binding=binding,
                _timers=tuple(timers),
            ),
        )

    def project_deactivated_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        return self.detach_timers_locked(
            binding,
            state,
            patch=True,
            mirror=True,
        )

    def project_detached_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        return self.detach_timers_locked(
            binding,
            state,
            patch=True,
            mirror=True,
        )

    def project_thread_replaced_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        effects = self.detach_timers_locked(
            binding,
            state,
            patch=True,
            mirror=True,
        )
        self._turn_execution.reset_execution_context_locked(
            state,
            clear_card_message=True,
        )
        self._turn_execution.apply_runtime_state_message_locked(
            state,
            ThreadGoalCleared(),
        )
        return effects

    def project_after_bind_locked(self, state: RuntimeStateDict) -> None:
        self._turn_execution.clear_plan_state_locked(state)

    def project_thread_cleared_locked(
        self,
        binding: ChatBindingKey,
        state: RuntimeStateDict,
    ) -> tuple[RuntimeTimerCancellationEffect, ...]:
        effects = self.project_thread_replaced_locked(binding, state)
        self.project_after_bind_locked(state)
        return effects
