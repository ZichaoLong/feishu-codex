"""Compose Feishu resume settlement with active-observer presentation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bot.adapters.base import ThreadSummary
from bot.binding_execution_runtime import BindingExecutionRuntimeTransitions
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.execution_output_controller import ExecutionOutputController
from bot.feishu_active_observer import FeishuActiveObserverController
from bot.feishu_binding_transition import FeishuBindingTransitionOwner
from bot.feishu_thread_session_coordinator import (
    FeishuThreadSessionAdapter,
    FeishuThreadSessionBindingRuntime,
    FeishuThreadSessionCoordinator,
    FeishuThreadSessionPorts,
    FeishuThreadSessionRootOperations,
    FeishuThreadSessionRuntimeAuthority,
    FeishuThreadSessionWarnings,
)


def compose_feishu_thread_sessions(
    *,
    lock: Any,
    adapter: FeishuThreadSessionAdapter,
    binding_runtime: FeishuThreadSessionBindingRuntime,
    binding_transitions: FeishuBindingTransitionOwner,
    thread_runtime: FeishuThreadSessionRuntimeAuthority,
    root_operations: FeishuThreadSessionRootOperations,
    warnings: FeishuThreadSessionWarnings,
    execution_runtime: BindingExecutionRuntimeTransitions,
    execution_output: ExecutionOutputController,
    schedule_active_observer_recovery: Callable[[BindingSessionSnapshot], None],
    acquire_runtime_lease: Callable[[str], bool],
    release_runtime_lease: Callable[[str], None],
    runtime_interest_retained: Callable[[str], bool],
    remember_direct_thread_summary: Callable[[ThreadSummary], None],
    is_thread_not_found_error: Callable[[Exception], bool],
    is_transport_disconnect: Callable[[Exception], bool],
) -> FeishuThreadSessionCoordinator:
    """Build the exact resume owner and its process-local observer helper."""

    observer = FeishuActiveObserverController(
        execution_runtime=execution_runtime,
        execution_output=execution_output,
    )
    return FeishuThreadSessionCoordinator(
        lock=lock,
        adapter=adapter,
        binding_runtime=binding_runtime,
        binding_transitions=binding_transitions,
        thread_runtime=thread_runtime,
        root_operations=root_operations,
        warnings=warnings,
        ports=FeishuThreadSessionPorts(
            acquire_runtime_lease=acquire_runtime_lease,
            release_runtime_lease=release_runtime_lease,
            runtime_interest_retained=runtime_interest_retained,
            remember_direct_thread_summary=remember_direct_thread_summary,
            is_thread_not_found_error=is_thread_not_found_error,
            is_transport_disconnect=is_transport_disconnect,
            prepare_active_observer=observer.prepare_resume_snapshot,
            prime_active_observer=observer.prime_execution,
            rollback_active_observer=observer.rollback_execution,
            present_active_observer=observer.present_execution,
            schedule_active_observer_recovery=(
                schedule_active_observer_recovery
            ),
        ),
    )
