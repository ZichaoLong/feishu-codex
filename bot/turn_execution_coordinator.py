from __future__ import annotations

from dataclasses import dataclass
from typing import Any, MutableMapping, TypeAlias

from bot.execution_transcript import ExecutionTranscript
from bot.runtime_state import (
    UNSET,
    ExecutionAnchorCleared,
    ExecutionRetired,
    ExecutionStateChanged,
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
