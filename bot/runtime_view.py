"""
Immutable runtime state projection for read-side consumers.

Domains and renderers should prefer this snapshot over directly reading the
mutable runtime-state dict owned by ``CodexHandler``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import FEISHU_RUNTIME_ATTACHED, RuntimeStateDict


@dataclass(frozen=True, slots=True)
class PlanStepView:
    step: str
    status: str


@dataclass(frozen=True, slots=True)
class RuntimeSettingsView:
    approval_policy: str
    sandbox: str
    collaboration_mode: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True, slots=True)
class ThreadBindingView:
    working_dir: str
    thread_id: str
    title: str
    feishu_runtime_state: str

    @property
    def has_thread(self) -> bool:
        return bool(self.thread_id)

    @property
    def feishu_runtime_attached(self) -> bool:
        return self.feishu_runtime_state == FEISHU_RUNTIME_ATTACHED and self.has_thread


@dataclass(frozen=True, slots=True)
class ExecutionView:
    running: bool
    cancelled: bool
    pending_cancel: bool
    current_turn_id: str
    current_message_id: str
    last_execution_message_id: str
    current_prompt_message_id: str
    current_prompt_reply_in_thread: bool
    current_actor_open_id: str
    transcript: ExecutionTranscript
    runtime_channel_state: str
    started_at: float
    last_runtime_event_at: float
    last_patch_at: float
    mirror_watchdog_generation: int
    followup_sent: bool
    followup_text: str
    terminal_result_text: str
    awaiting_local_turn_started: bool

    @property
    def effective_message_id(self) -> str:
        return self.current_message_id or self.last_execution_message_id

    @property
    def has_execution_anchor(self) -> bool:
        return bool(self.current_message_id) and (
            self.running or self.awaiting_local_turn_started or bool(self.current_turn_id)
        )


@dataclass(frozen=True, slots=True)
class PlanView:
    message_id: str
    turn_id: str
    explanation: str
    steps: tuple[PlanStepView, ...]
    text: str


@dataclass(frozen=True, slots=True)
class RuntimeView:
    active: bool
    binding: ThreadBindingView
    execution: ExecutionView
    settings: RuntimeSettingsView
    plan: PlanView

    @property
    def working_dir(self) -> str:
        return self.binding.working_dir

    @property
    def current_thread_id(self) -> str:
        return self.binding.thread_id

    @property
    def current_thread_title(self) -> str:
        return self.binding.title

    @property
    def running(self) -> bool:
        return self.execution.running

    @property
    def approval_policy(self) -> str:
        return self.settings.approval_policy

    @property
    def sandbox(self) -> str:
        return self.settings.sandbox

    @property
    def collaboration_mode(self) -> str:
        return self.settings.collaboration_mode


def build_runtime_view(state: RuntimeStateDict) -> RuntimeView:
    return RuntimeView(
        active=bool(state["active"]),
        binding=ThreadBindingView(
            working_dir=str(state["working_dir"] or ""),
            thread_id=str(state["current_thread_id"] or ""),
            title=str(state["current_thread_title"] or ""),
            feishu_runtime_state=str(state.get("feishu_runtime_state") or ""),
        ),
        execution=ExecutionView(
            running=bool(state["running"]),
            cancelled=bool(state["cancelled"]),
            pending_cancel=bool(state["pending_cancel"]),
            current_turn_id=str(state["current_turn_id"] or ""),
            current_message_id=str(state["current_message_id"] or ""),
            last_execution_message_id=str(state["last_execution_message_id"] or ""),
            current_prompt_message_id=str(state["current_prompt_message_id"] or ""),
            current_prompt_reply_in_thread=bool(state["current_prompt_reply_in_thread"]),
            current_actor_open_id=str(state["current_actor_open_id"] or ""),
            transcript=state["execution_transcript"].clone(),
            runtime_channel_state=str(state["runtime_channel_state"] or ""),
            started_at=float(state["started_at"] or 0.0),
            last_runtime_event_at=float(state["last_runtime_event_at"] or 0.0),
            last_patch_at=float(state["last_patch_at"] or 0.0),
            mirror_watchdog_generation=int(state["mirror_watchdog_generation"] or 0),
            followup_sent=bool(state["followup_sent"]),
            followup_text=str(state.get("followup_text") or ""),
            terminal_result_text=str(state.get("terminal_result_text") or ""),
            awaiting_local_turn_started=bool(state["awaiting_local_turn_started"]),
        ),
        settings=RuntimeSettingsView(
            approval_policy=str(state["approval_policy"] or ""),
            sandbox=str(state["sandbox"] or ""),
            collaboration_mode=str(state["collaboration_mode"] or ""),
            model=str(state["model"] or ""),
            reasoning_effort=str(state["reasoning_effort"] or ""),
        ),
        plan=PlanView(
            message_id=str(state["plan_message_id"] or ""),
            turn_id=str(state["plan_turn_id"] or ""),
            explanation=str(state["plan_explanation"] or ""),
            steps=tuple(
                PlanStepView(
                    step=str(item.get("step", "") or ""),
                    status=str(item.get("status", "") or ""),
                )
                for item in (state["plan_steps"] or [])
            ),
            text=str(state["plan_text"] or ""),
        ),
    )
