from __future__ import annotations

from dataclasses import dataclass
from typing import Any, MutableMapping, TypeAlias

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    UNSET,
    ExecutionAnchorCleared,
    ExecutionRetired,
    ExecutionStateChanged,
    PlanStateChanged,
    RuntimeHeartbeat,
    apply_runtime_state_message,
)

RuntimeState: TypeAlias = MutableMapping[str, Any]


@dataclass(frozen=True)
class PreviousExecutionCardSnapshot:
    message_id: str
    transcript: ExecutionTranscript
    cancelled: bool
    elapsed: int


@dataclass(frozen=True)
class TurnStartedTransition:
    reuse_existing_card: bool
    previous_execution_card: PreviousExecutionCardSnapshot | None
    should_interrupt_started_turn: bool


@dataclass(frozen=True)
class FinalizeExecutionTransition:
    had_card: bool


@dataclass(frozen=True)
class ExecutionFollowupMessage:
    reply_text: str
    prompt_message_id: str
    prompt_reply_in_thread: bool


class TurnExecutionCoordinator:
    @staticmethod
    def apply_runtime_state_message_locked(state: RuntimeState, message: Any) -> None:
        apply_runtime_state_message(state, message)

    @staticmethod
    def has_active_execution_locked(state: RuntimeState) -> bool:
        return bool(state["current_message_id"]) and (
            state["running"]
            or state["awaiting_local_turn_started"]
            or bool(state["current_turn_id"])
        )

    def mark_runtime_event_locked(self, state: RuntimeState, *, occurred_at: float) -> None:
        self.apply_runtime_state_message_locked(
            state,
            RuntimeHeartbeat(occurred_at=occurred_at),
        )

    def clear_execution_anchor_locked(self, state: RuntimeState, *, clear_card_message: bool) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionAnchorCleared(clear_card_message=clear_card_message),
        )

    def reset_execution_context_locked(self, state: RuntimeState, *, clear_card_message: bool) -> None:
        self.clear_execution_anchor_locked(state, clear_card_message=clear_card_message)
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=False,
                cancelled=False,
                pending_cancel=False,
                current_message_id="" if clear_card_message else UNSET,
                last_execution_message_id="",
                current_turn_id="",
                current_prompt_message_id="",
                current_prompt_reply_in_thread=False,
                current_actor_open_id="",
                followup_sent=False,
                awaiting_local_turn_started=False,
                runtime_channel_state="live",
                reset_transcript=True,
            ),
        )

    def prime_prompt_turn_locked(
        self,
        state: RuntimeState,
        *,
        prompt_message_id: str,
        prompt_reply_in_thread: bool,
        actor_open_id: str,
        started_at: float,
    ) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=True,
                cancelled=False,
                pending_cancel=False,
                current_turn_id="",
                last_execution_message_id="",
                current_prompt_message_id=prompt_message_id,
                current_prompt_reply_in_thread=prompt_reply_in_thread,
                current_actor_open_id=actor_open_id,
                runtime_channel_state="live",
                started_at=started_at,
                last_runtime_event_at=started_at,
                followup_sent=False,
                last_patch_at=0.0,
                awaiting_local_turn_started=True,
                reset_transcript=True,
            ),
        )

    def record_start_failure_locked(self, state: RuntimeState, *, error_text: str) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=False,
                pending_cancel=False,
                reply_text=error_text,
            ),
        )

    def mark_runtime_degraded_locked(self, state: RuntimeState) -> bool:
        if not self.has_active_execution_locked(state):
            return False
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(runtime_channel_state="degraded"),
        )
        return True

    def record_started_turn_id_locked(self, state: RuntimeState, *, turn_id: str) -> bool:
        normalized_turn_id = str(turn_id or "").strip()
        if normalized_turn_id and not state["current_turn_id"]:
            self.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(current_turn_id=normalized_turn_id),
            )
        return bool(normalized_turn_id and state["pending_cancel"])

    def request_cancel_without_turn_id_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                cancelled=True,
                pending_cancel=True,
            ),
        )

    def confirm_cancel_requested_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                cancelled=True,
                pending_cancel=False,
            ),
        )

    @staticmethod
    def _followup_message_from_state(state: RuntimeState, reply_text: str) -> ExecutionFollowupMessage:
        return ExecutionFollowupMessage(
            reply_text=reply_text,
            prompt_message_id=str(state["current_prompt_message_id"] or "").strip(),
            prompt_reply_in_thread=bool(state["current_prompt_reply_in_thread"]),
        )

    def start_process_block_locked(self, state: RuntimeState, *, text: str, marks_work: bool) -> None:
        state["execution_transcript"].start_process_block(text, marks_work=marks_work)

    def append_process_note_locked(self, state: RuntimeState, *, text: str, marks_work: bool = False) -> None:
        state["execution_transcript"].append_process_note(text, marks_work=marks_work)

    def finish_process_block_locked(self, state: RuntimeState, *, suffix: str = "") -> None:
        state["execution_transcript"].finish_process_block(suffix)

    def append_assistant_delta_locked(self, state: RuntimeState, *, delta: str) -> None:
        state["execution_transcript"].append_assistant_delta(delta)

    def append_process_delta_locked(self, state: RuntimeState, *, text: str) -> None:
        state["execution_transcript"].append_process_delta(text)

    def reconcile_current_assistant_text_locked(self, state: RuntimeState, *, text: str) -> bool:
        transcript = state["execution_transcript"]
        if len(text) < len(transcript.reply_text()):
            return False
        transcript.reconcile_current_assistant_text(text)
        return True

    def apply_snapshot_reply_locked(
        self,
        state: RuntimeState,
        *,
        reply_text: str,
        reply_items: list[dict[str, Any]],
    ) -> None:
        transcript = state["execution_transcript"]
        if reply_text and len(reply_text) >= len(transcript.reply_text()):
            if not transcript.rebuild_reply_from_snapshot_items(
                reply_items,
                fallback_text=reply_text,
            ):
                transcript.set_reply_text(reply_text)

    def acknowledge_running_snapshot_locked(self, state: RuntimeState, *, occurred_at: float) -> None:
        self.acknowledge_active_thread_locked(state)
        self.mark_runtime_event_locked(state, occurred_at=occurred_at)

    def prepare_patch_failure_followup_locked(self, state: RuntimeState) -> ExecutionFollowupMessage | None:
        if state["followup_sent"]:
            return None
        reply_text = state["execution_transcript"].reply_text()
        if not reply_text:
            return None
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(followup_sent=True),
        )
        return self._followup_message_from_state(state, reply_text)

    def prepare_terminal_followup_locked(
        self,
        state: RuntimeState,
    ) -> ExecutionFollowupMessage | None:
        if state["followup_sent"]:
            return None
        reply_text = state["execution_transcript"].reply_text()
        if not reply_text:
            return None
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(followup_sent=True),
        )
        return self._followup_message_from_state(state, reply_text)

    def acknowledge_active_thread_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=True,
                awaiting_local_turn_started=False,
            ),
        )

    def settle_non_active_thread_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                pending_cancel=False,
                awaiting_local_turn_started=False,
                runtime_channel_state="live",
                running=False,
                current_turn_id="",
            ),
        )

    def settle_thread_closed_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=False,
                pending_cancel=False,
            ),
        )

    def clear_plan_state_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(state, PlanStateChanged(clear=True))

    def update_plan_outline_locked(
        self,
        state: RuntimeState,
        *,
        turn_id: str,
        explanation: str,
        plan: list[dict[str, Any]],
    ) -> bool:
        current_turn_id = str(state["current_turn_id"] or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if current_turn_id and normalized_turn_id and current_turn_id != normalized_turn_id:
            return False
        self.apply_runtime_state_message_locked(
            state,
            PlanStateChanged(
                plan_turn_id=normalized_turn_id or state["plan_turn_id"],
                plan_explanation=explanation,
                plan_steps=[
                    {"step": str(item.get("step", "")).strip(), "status": str(item.get("status", "")).strip()}
                    for item in plan
                    if str(item.get("step", "")).strip()
                ],
            ),
        )
        return True

    def update_plan_text_locked(
        self,
        state: RuntimeState,
        *,
        turn_id: str,
        text: str,
    ) -> bool:
        current_turn_id = str(state["current_turn_id"] or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if current_turn_id and normalized_turn_id and current_turn_id != normalized_turn_id:
            return False
        if len(text) < len(str(state["plan_text"] or "")):
            return False
        self.apply_runtime_state_message_locked(
            state,
            PlanStateChanged(
                plan_turn_id=normalized_turn_id or state["plan_turn_id"],
                plan_text=text,
            ),
        )
        return True

    def prepare_turn_started_locked(
        self,
        state: RuntimeState,
        *,
        turn_id: str,
        started_at: float,
    ) -> TurnStartedTransition:
        normalized_turn_id = str(turn_id or "").strip()
        reuse_existing_card = self.has_active_execution_locked(state)
        should_interrupt_started_turn = bool(normalized_turn_id and state["pending_cancel"])
        previous_execution_card: PreviousExecutionCardSnapshot | None = None

        if not reuse_existing_card:
            previous_message_id = str(state["current_message_id"] or "").strip()
            if previous_message_id:
                previous_execution_card = PreviousExecutionCardSnapshot(
                    message_id=previous_message_id,
                    transcript=state["execution_transcript"].clone(),
                    cancelled=bool(state["cancelled"]),
                    elapsed=int(max(0.0, started_at - float(state["started_at"] or 0.0)))
                    if state["started_at"]
                    else 0,
                )
            self.clear_execution_anchor_locked(state, clear_card_message=True)
            self.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(
                    cancelled=False,
                    last_execution_message_id="",
                    started_at=started_at,
                    last_runtime_event_at=started_at,
                    last_patch_at=0.0,
                    followup_sent=False,
                    runtime_channel_state="live",
                    reset_transcript=True,
                ),
            )

        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                current_turn_id=normalized_turn_id,
                running=True,
                awaiting_local_turn_started=False,
            ),
        )
        return TurnStartedTransition(
            reuse_existing_card=reuse_existing_card,
            previous_execution_card=previous_execution_card,
            should_interrupt_started_turn=should_interrupt_started_turn,
        )

    def apply_turn_completed_locked(
        self,
        state: RuntimeState,
        *,
        status: str,
        error_message: str,
    ) -> None:
        if status == "interrupted":
            self.apply_runtime_state_message_locked(
                state,
                ExecutionStateChanged(cancelled=True),
            )
        transcript = state["execution_transcript"]
        if error_message and not transcript.has_reply_output():
            transcript.set_reply_text(error_message)
        elif error_message:
            transcript.append_process_note(f"\n[错误] {error_message}\n")

    def prepare_finalize_locked(self, state: RuntimeState) -> FinalizeExecutionTransition:
        had_card = bool(state["current_message_id"])
        self.apply_runtime_state_message_locked(
            state,
            ExecutionStateChanged(
                running=False,
                pending_cancel=False,
                awaiting_local_turn_started=False,
                current_turn_id="",
            ),
        )
        return FinalizeExecutionTransition(had_card=had_card)

    def retire_execution_locked(self, state: RuntimeState) -> None:
        self.apply_runtime_state_message_locked(state, ExecutionRetired())
