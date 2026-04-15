"""
Presentation and publishing helpers for Codex runtime cards.

These helpers keep Feishu card payload assembly and message IO out of
``CodexHandler`` so the handler can stay focused on orchestration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from bot.cards import build_execution_card, build_plan_card
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.runtime_view import PlanView

_LOG_TRUNCATION_NOTICE = "\n\n**[日志已截断，仅保留最近部分]**"


@dataclass(frozen=True, slots=True)
class ExecutionCardModel:
    log_text: str
    reply_segments: tuple[ExecutionReplySegment, ...]
    running: bool
    elapsed: int
    cancelled: bool

    @classmethod
    def running_placeholder(cls) -> ExecutionCardModel:
        return cls(
            log_text="",
            reply_segments=(),
            running=True,
            elapsed=0,
            cancelled=False,
        )


@dataclass(frozen=True, slots=True)
class PlanCardModel:
    turn_id: str
    explanation: str
    plan_steps: tuple[dict[str, str], ...]
    plan_text: str

    @property
    def is_empty(self) -> bool:
        return not self.explanation and not self.plan_steps and not self.plan_text


@dataclass(frozen=True, slots=True)
class PlanCardPublishResult:
    message_id: str | None
    attempted_existing: bool
    reused_existing: bool


class _CardPublisherBot(Protocol):
    def patch_message(self, message_id: str, content: str) -> bool: ...

    def reply_to_message(
        self,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None: ...

    def send_message_get_id(self, chat_id: str, msg_type: str, content: str) -> str | None: ...


def _truncate_log_text(text: str, *, log_limit: int) -> str:
    if len(text) <= log_limit:
        return text
    return text[-log_limit:] + _LOG_TRUNCATION_NOTICE


def build_execution_card_model(
    transcript: ExecutionTranscript,
    *,
    running: bool,
    elapsed: int,
    cancelled: bool,
    log_limit: int,
    reply_limit: int,
) -> ExecutionCardModel:
    return ExecutionCardModel(
        log_text=_truncate_log_text(transcript.process_text(), log_limit=log_limit),
        reply_segments=tuple(transcript.reply_segments_for_card(reply_limit)),
        running=running,
        elapsed=elapsed,
        cancelled=cancelled and not running,
    )


def render_execution_card(model: ExecutionCardModel) -> dict:
    return build_execution_card(
        model.log_text,
        list(model.reply_segments),
        running=model.running,
        elapsed=model.elapsed,
        cancelled=model.cancelled,
    )


def build_plan_card_model(plan: PlanView) -> PlanCardModel:
    return PlanCardModel(
        turn_id=plan.turn_id,
        explanation=plan.explanation,
        plan_steps=tuple(
            {"step": step.step, "status": step.status}
            for step in plan.steps
            if step.step
        ),
        plan_text=plan.text,
    )


def render_plan_card(model: PlanCardModel) -> dict:
    return build_plan_card(
        model.turn_id,
        explanation=model.explanation,
        plan_steps=list(model.plan_steps),
        plan_text=model.plan_text,
    )


class RuntimeCardPublisher:
    def __init__(self, bot: _CardPublisherBot):
        self._bot = bot

    def send_execution_card(self, chat_id: str, parent_message_id: str) -> str | None:
        content = json.dumps(render_execution_card(ExecutionCardModel.running_placeholder()), ensure_ascii=False)
        if parent_message_id:
            return self._bot.reply_to_message(parent_message_id, "interactive", content)
        return self._bot.send_message_get_id(chat_id, "interactive", content)

    def patch_execution_card(self, message_id: str, model: ExecutionCardModel) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        return self._bot.patch_message(
            normalized_message_id,
            json.dumps(render_execution_card(model), ensure_ascii=False),
        )

    def publish_plan_card(
        self,
        *,
        chat_id: str,
        parent_message_id: str,
        plan_message_id: str,
        model: PlanCardModel,
    ) -> PlanCardPublishResult:
        content = json.dumps(render_plan_card(model), ensure_ascii=False)
        normalized_existing = str(plan_message_id or "").strip()
        attempted_existing = bool(normalized_existing)
        if normalized_existing and self._bot.patch_message(normalized_existing, content):
            return PlanCardPublishResult(
                message_id=normalized_existing,
                attempted_existing=True,
                reused_existing=True,
            )

        new_message_id: str | None = None
        if parent_message_id:
            new_message_id = self._bot.reply_to_message(parent_message_id, "interactive", content)
        if not new_message_id:
            new_message_id = self._bot.send_message_get_id(chat_id, "interactive", content)
        normalized_new_id = str(new_message_id or "").strip() or None
        return PlanCardPublishResult(
            message_id=normalized_new_id,
            attempted_existing=attempted_existing,
            reused_existing=False,
        )
