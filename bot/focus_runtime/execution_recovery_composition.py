"""Compose execution recovery without giving it a FocusRuntime back-reference."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from bot.adapter_ingress_gate import AdapterIngressGate
from bot.adapters.base import AgentAdapter
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.execution_pages import TerminalExecutionPageReceipt
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_recovery_controller import ExecutionRecoveryController
from bot.execution_recovery_runtime import ExecutionRecoveryRuntimeTransitions
from bot.feishu_execution_finalization_controller import (
    FeishuExecutionFinalizationController,
)
from bot.focus_runtime.thread_targets import CodexThreadTargetService


class _ExecutionFinalizationResult(Protocol):
    had_card: bool
    retired: bool
    terminal_page_receipts: tuple[TerminalExecutionPageReceipt, ...]


def compose_execution_recovery(
    *,
    runtime: ExecutionRecoveryRuntimeTransitions,
    runtime_call: Callable[..., object],
    binding_runtime: BindingRuntimeManager,
    adapter: Callable[[], AgentAdapter],
    adapter_ingress_gate: Callable[[], AdapterIngressGate],
    terminal_execution: Callable[[], FeishuExecutionFinalizationController],
    finalize_execution: Callable[
        [BindingSessionSnapshot],
        _ExecutionFinalizationResult | None,
    ],
    execution_output: ExecutionOutputController,
    mark_compact_start_outcome_unknown: Callable[
        [BindingSessionSnapshot, str], None
    ],
    publish_terminal_result: Callable[..., bool],
    has_recorded_terminal_result: Callable[..., bool],
    deliver_generated_images_from_snapshot: Callable[..., int],
    mirror_watchdog_seconds: Callable[[], float],
    compact_start_timeout_seconds: Callable[[], float],
) -> ExecutionRecoveryController:
    """Build recovery orchestration from explicit owners and late-bound ports."""

    return ExecutionRecoveryController(
        runtime=runtime,
        runtime_call=runtime_call,
        capture_connection_generation=(
            lambda: adapter_ingress_gate().capture_existing_connection_generation()
        ),
        run_if_connection_generation=(
            lambda generation, callback: (
                adapter_ingress_gate().run_if_connection_generation(
                    generation,
                    callback,
                )
            )
        ),
        resolve_session=binding_runtime.resolve_session,
        finalize_execution=finalize_execution,
        prepare_execution_finalization=(
            lambda session: (
                terminal_execution().prepare_and_commit_if_current(session)
            )
        ),
        present_execution_finalization=(
            lambda plan: terminal_execution().present(plan)
        ),
        mark_compact_start_outcome_unknown=mark_compact_start_outcome_unknown,
        dispatch_execution_card_message=(
            execution_output.dispatch_execution_card_message
        ),
        publish_terminal_result=publish_terminal_result,
        has_recorded_terminal_result=has_recorded_terminal_result,
        deliver_generated_images_from_snapshot=(
            deliver_generated_images_from_snapshot
        ),
        read_thread=lambda thread_id, **kwargs: adapter().read_thread(
            thread_id,
            include_turns=True,
            **kwargs,
        ),
        is_thread_not_found_error=CodexThreadTargetService.is_thread_not_found_error,
        is_turn_thread_not_found_error=(
            CodexThreadTargetService.is_turn_thread_not_found_error
        ),
        is_pre_send_error=CodexThreadTargetService.is_pre_send_error,
        is_transport_disconnect=CodexThreadTargetService.is_transport_disconnect,
        is_request_timeout_error=CodexThreadTargetService.is_request_timeout_error,
        runtime_recovery_reason=CodexThreadTargetService.runtime_recovery_reason,
        mirror_watchdog_seconds=mirror_watchdog_seconds,
        compact_start_timeout_seconds=compact_start_timeout_seconds,
        terminal_empty_retry_count=lambda: 6,
        terminal_empty_retry_delay_seconds=lambda: 0.5,
    )
