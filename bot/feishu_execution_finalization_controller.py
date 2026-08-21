"""Exact Feishu execution-card refresh and terminal retirement transaction."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, ContextManager, Protocol

from bot.binding_identity import ChatBindingKey, format_binding_id
from bot.binding_runtime_contract import (
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.binding_runtime_lifecycle import (
    BindingRuntimeLifecycleTransitions,
    RuntimeTimerCancellationEffect,
    cancel_runtime_timer_effects,
)
from bot.execution_pages import (
    TerminalExecutionPageReceipt,
    require_terminal_execution_page_receipts,
)
from bot.execution_transcript import ExecutionTranscriptSnapshot
from bot.runtime_state import ExecutionStateChanged, RuntimeStateDict
from bot.turn_execution_coordinator import TurnExecutionCoordinator


logger = logging.getLogger(__name__)


class FeishuExecutionRuntimeChanged(RuntimeError):
    """The exact resident execution changed during a terminal transaction."""


@dataclass(frozen=True, slots=True)
class FeishuTerminalExecutionCard:
    """Immutable presentation effect for one exact execution card."""

    binding: ChatBindingKey
    message_id: str
    transcript: ExecutionTranscriptSnapshot
    elapsed: int
    cancelled: bool


@dataclass(frozen=True, slots=True)
class FeishuExecutionFinalizationResult:
    """Typed settlement result; presentation failure never rewrites commit facts."""

    had_card: bool
    retired: bool
    presentation_error: str = ""
    terminal_page_receipts: tuple[TerminalExecutionPageReceipt, ...] = ()

    def __post_init__(self) -> None:
        require_terminal_execution_page_receipts(
            self.terminal_page_receipts,
            field="terminal finalization page receipts",
        )


@dataclass(frozen=True, slots=True)
class FeishuExecutionFinalizationPlan:
    """Single-call-chain plan produced after exact runtime retirement.

    The optional snapshot contains only immutable pre-retirement facts for the
    loop-external presentation effect.  The plan is not durable authority and
    must not be replayed or used to settle another execution.
    """

    had_card: bool
    retired: bool
    presentation_snapshot: BindingSessionSnapshot | None

    def __post_init__(self) -> None:
        if type(self.had_card) is not bool or type(self.retired) is not bool:
            raise TypeError("Feishu finalization plan commit facts must be bool")
        if (
            self.presentation_snapshot is not None
            and type(self.presentation_snapshot) is not BindingSessionSnapshot
        ):
            raise TypeError(
                "Feishu finalization plan presentation must be an exact snapshot"
            )


class FeishuExecutionBindingRuntime(Protocol):
    """Exact resident operations required from the binding-runtime owner."""

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

    def resident_session_snapshot_locked(
        self,
        binding: ChatBindingKey,
    ) -> BindingSessionSnapshot | None: ...

    def resident_runtime_state_locked(
        self,
        binding: ChatBindingKey,
    ) -> RuntimeStateDict | None: ...

    def persist_session_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> BindingSessionSnapshot: ...


class _ExecutionCardPatchOutcome(Protocol):
    applied: bool


class _TerminalExecutionOutput(Protocol):
    def patch_execution_card_message(
        self,
        chat_id: str,
        message_id: str,
        *,
        transcript: ExecutionTranscriptSnapshot,
        running: bool,
        elapsed: int,
        cancelled: bool,
    ) -> _ExecutionCardPatchOutcome: ...

    def present_terminal_execution_card(
        self,
        captured: BindingSessionSnapshot,
        *,
        background: bool = True,
    ) -> tuple[TerminalExecutionPageReceipt, ...]: ...


@dataclass(frozen=True, slots=True)
class FeishuExecutionFinalizationPorts:
    lock: ContextManager[Any]
    release_main_turn: Callable[[ChatBindingKey, str, str], bool]
    drain_execution_queue: Callable[[ChatBindingKey], None]


class FeishuExecutionFinalizationController:
    """Retire one exact execution before detached terminal presentation.

    Ingress callers may select a binding once through ``resolve_session``.
    Consumers that already hold a subscriber, lease, inventory, or receipt must
    call the exact-session methods so their identity is never remapped.
    """

    def __init__(
        self,
        *,
        binding_runtime: FeishuExecutionBindingRuntime,
        turn_execution: TurnExecutionCoordinator,
        execution_output: _TerminalExecutionOutput,
        runtime_context_guard: Callable[[], None],
        ports: FeishuExecutionFinalizationPorts,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError(
                "Feishu execution finalization requires a RuntimeLoop context guard"
            )
        self._binding_runtime = binding_runtime
        self._turn_execution = turn_execution
        self._lifecycle = BindingRuntimeLifecycleTransitions(
            turn_execution=turn_execution
        )
        self._execution_output = execution_output
        self._runtime_context_guard = runtime_context_guard
        self._ports = ports

    def refresh_ingress(
        self,
        sender_id: str,
        chat_id: str,
    ) -> bool:
        self._runtime_context_guard()
        captured = self._binding_runtime.resolve_session(sender_id, chat_id)
        return self._refresh_terminal_card(captured)

    def refresh_terminal_card(
        self,
        captured: BindingSessionSnapshot,
    ) -> bool:
        self._runtime_context_guard()
        return self._refresh_terminal_card(captured)

    def _refresh_terminal_card(
        self,
        captured: BindingSessionSnapshot,
    ) -> bool:
        with self._ports.lock:
            try:
                current, _state = self._require_exact_execution_locked(captured)
            except FeishuExecutionRuntimeChanged:
                return False
            effect = self._terminal_card_effect(current, use_effective_message=True)
        if effect is None:
            return False
        return bool(self._patch_terminal_card(effect))

    def finalize_ingress_result(
        self,
        sender_id: str,
        chat_id: str,
    ) -> FeishuExecutionFinalizationResult:
        self._runtime_context_guard()
        captured = self._binding_runtime.resolve_session(sender_id, chat_id)
        return self._finalize(captured)

    def finalize_ingress(self, sender_id: str, chat_id: str) -> bool:
        result = self.finalize_ingress_result(sender_id, chat_id)
        return bool(result.had_card and result.retired)

    def finalize(
        self,
        captured: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationResult:
        self._runtime_context_guard()
        return self._finalize(captured)

    def prepare_and_commit_if_current(
        self,
        captured: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationPlan | None:
        """Retire the exact current execution, or reject a replaced target."""

        self._runtime_context_guard()
        try:
            return self._prepare_and_commit(captured)
        except FeishuExecutionRuntimeChanged:
            return None

    def _finalize(
        self,
        captured: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationResult:
        return self.present(self._prepare_and_commit(captured))

    def _prepare_and_commit(
        self,
        captured: BindingSessionSnapshot,
    ) -> FeishuExecutionFinalizationPlan:
        timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...] = ()
        try:
            with self._ports.lock:
                current, state = self._require_exact_execution_locked(captured)
                main_thread_id = current.current_thread_id
                main_turn_id = current.execution.current_turn_id
                transition = self._turn_execution.prepare_finalize_locked(state)
                timer_cancellations = self._lifecycle.detach_timers_locked(
                    captured.binding,
                    state,
                    patch=transition.had_card,
                    mirror=True,
                )
                if transition.had_card:
                    self._turn_execution.apply_runtime_state_message_locked(
                        state,
                        ExecutionStateChanged(last_patch_at=time.monotonic()),
                    )
                prepared = self._binding_runtime.session_snapshot_locked(
                    captured.handle
                )
                effect = self._terminal_card_effect(
                    prepared,
                    use_effective_message=False,
                )
        finally:
            cancel_runtime_timer_effects(timer_cancellations)

        retired = self._commit_retirement(
            prepared,
            main_thread_id=main_thread_id,
            main_turn_id=main_turn_id,
        )
        if retired:
            self._ports.drain_execution_queue(prepared.binding)

        return FeishuExecutionFinalizationPlan(
            had_card=transition.had_card,
            retired=retired is not None,
            presentation_snapshot=(
                prepared
                if effect is not None
                or prepared.execution.pages.send_outcome_unknown
                else None
            ),
        )

    def present(
        self,
        plan: FeishuExecutionFinalizationPlan,
    ) -> FeishuExecutionFinalizationResult:
        """Run terminal-card presentation outside RuntimeLoop.

        Runtime retirement and FIFO progress are already committed before this
        method starts.  Presentation failure therefore only changes the typed
        presentation result.
        """

        if type(plan) is not FeishuExecutionFinalizationPlan:
            raise TypeError(
                "Feishu terminal presentation requires an exact finalization plan"
            )
        presentation_error = ""
        terminal_page_receipts: tuple[TerminalExecutionPageReceipt, ...] = ()
        prepared = plan.presentation_snapshot
        if prepared is not None:
            try:
                presented = self._execution_output.present_terminal_execution_card(
                    prepared,
                    background=True,
                )
                terminal_page_receipts = require_terminal_execution_page_receipts(
                    presented,
                    field="terminal execution presentation receipts",
                )
            except Exception as exc:
                presentation_error = str(exc) or type(exc).__name__
                logger.exception(
                    "Feishu terminal execution card presentation failed after "
                    "turn retirement: binding=%s",
                    format_binding_id(prepared.binding),
                )
        return FeishuExecutionFinalizationResult(
            had_card=plan.had_card,
            retired=plan.retired,
            presentation_error=presentation_error,
            terminal_page_receipts=terminal_page_receipts,
        )

    def retire_ingress(self, sender_id: str, chat_id: str) -> bool:
        self._runtime_context_guard()
        captured = self._binding_runtime.resolve_session(sender_id, chat_id)
        return self._retire(captured)

    def retire(self, captured: BindingSessionSnapshot) -> bool:
        self._runtime_context_guard()
        return self._retire(captured)

    def _retire(self, captured: BindingSessionSnapshot) -> bool:
        with self._ports.lock:
            current, _state = self._require_exact_execution_locked(captured)
        return bool(self._commit_retirement(current))

    def _commit_retirement(
        self,
        prepared: BindingSessionSnapshot,
        *,
        main_thread_id: str = "",
        main_turn_id: str = "",
    ) -> BindingSessionSnapshot | None:
        with self._ports.lock:
            _current, state = self._require_exact_execution_locked(prepared)
            thread_id = main_thread_id or prepared.current_thread_id
            turn_id = main_turn_id or prepared.execution.current_turn_id
            if not self._turn_execution.retire_execution_locked(state):
                return None
            try:
                committed = self._binding_runtime.persist_session_locked(
                    prepared.handle
                )
            except Exception:
                # A local durability failure must not resurrect a main turn in
                # the live process or hold its FIFO.  A later binding write can
                # persist the already-committed in-memory retirement.
                logger.exception(
                    "Feishu execution retirement persistence failed; "
                    "continuing live turn release: binding=%s",
                    format_binding_id(prepared.binding),
                )
                committed = self._binding_runtime.session_snapshot_locked(
                    prepared.handle
                )
            if thread_id and turn_id:
                self._ports.release_main_turn(
                    prepared.binding,
                    thread_id,
                    turn_id,
                )
        return committed

    def _patch_terminal_card(
        self,
        effect: FeishuTerminalExecutionCard,
    ) -> bool:
        outcome = self._execution_output.patch_execution_card_message(
            effect.binding[1],
            effect.message_id,
            transcript=effect.transcript,
            running=False,
            elapsed=effect.elapsed,
            cancelled=effect.cancelled,
        )
        return bool(outcome.applied)

    def _require_exact_execution_locked(
        self,
        captured: BindingSessionSnapshot,
    ) -> tuple[BindingSessionSnapshot, RuntimeStateDict]:
        if type(captured) is not BindingSessionSnapshot:
            raise TypeError(
                "Feishu execution transaction requires an exact binding session"
            )
        binding = captured.binding
        try:
            resident = self._binding_runtime.resident_session_snapshot_locked(
                binding
            )
            authorized = self._binding_runtime.session_snapshot_locked(
                captured.handle
            )
        except RuntimeError as exc:
            raise FeishuExecutionRuntimeChanged(
                "captured Feishu execution session is stale or replaced: "
                f"{format_binding_id(binding)}"
            ) from exc
        if (
            resident is None
            or resident.handle is not captured.handle
            or authorized.handle is not captured.handle
            or not self._same_execution_fence(captured, resident)
            or not self._same_execution_fence(captured, authorized)
        ):
            raise FeishuExecutionRuntimeChanged(
                "captured Feishu execution changed before commit: "
                f"{format_binding_id(binding)}"
            )
        state = self._binding_runtime.resident_runtime_state_locked(binding)
        if state is None:
            raise FeishuExecutionRuntimeChanged(
                "captured Feishu execution runtime is no longer resident: "
                f"{format_binding_id(binding)}"
            )
        return authorized, state

    @staticmethod
    def _same_execution_fence(
        expected: BindingSessionSnapshot,
        current: BindingSessionSnapshot,
    ) -> bool:
        expected_execution = expected.execution
        current_execution = current.execution
        return bool(
            current.handle is expected.handle
            and current.binding == expected.binding
            and current.current_thread_id == expected.current_thread_id
            and current_execution.current_turn_id
            == expected_execution.current_turn_id
            and current_execution.pages is expected_execution.pages
            and current_execution.current_prompt_message_id
            == expected_execution.current_prompt_message_id
            and current_execution.started_at == expected_execution.started_at
        )

    @staticmethod
    def _terminal_card_effect(
        session: BindingSessionSnapshot,
        *,
        use_effective_message: bool,
    ) -> FeishuTerminalExecutionCard | None:
        execution = session.execution
        message_id = (
            execution.effective_message_id
            if use_effective_message
            else execution.current_message_id
        ).strip()
        if not message_id:
            return None
        elapsed = (
            int(max(0.0, time.monotonic() - execution.started_at))
            if execution.started_at
            else 0
        )
        return FeishuTerminalExecutionCard(
            binding=session.binding,
            message_id=message_id,
            transcript=execution.transcript,
            elapsed=elapsed,
            cancelled=execution.cancelled,
        )
