"""
Presentation and publishing helpers for Codex runtime cards.

These helpers keep Feishu card payload assembly and message IO out of
``CodexHandler`` so the handler can stay focused on orchestration.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from bot.cards import build_execution_card, build_plan_card, build_terminal_result_card_message_content
from bot.execution_transcript import ExecutionReplySegment, ExecutionTranscript
from bot.message_patch_result import MessagePatchResult
from bot.runtime_view import PlanView

logger = logging.getLogger(__name__)

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


class ExecutionCardPatchStatus(StrEnum):
    FULL_APPLIED = "full_applied"
    MINIMAL_APPLIED = "minimal_applied"
    RETRYABLE = "retryable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExecutionCardPatchOutcome:
    status: ExecutionCardPatchStatus
    retry_after_seconds: float = 0.0
    retry_model: ExecutionCardModel | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionCardPatchStatus):
            raise TypeError("status must be an ExecutionCardPatchStatus")
        if self.status == ExecutionCardPatchStatus.RETRYABLE:
            if self.retry_model is None:
                raise ValueError("retryable execution-card patches require retry_model")
            return
        if self.retry_model is not None or self.retry_after_seconds != 0.0:
            raise ValueError("only retryable execution-card patches may carry retry state")

    @classmethod
    def full_applied(cls) -> ExecutionCardPatchOutcome:
        return cls(status=ExecutionCardPatchStatus.FULL_APPLIED)

    @classmethod
    def minimal_applied(cls) -> ExecutionCardPatchOutcome:
        return cls(status=ExecutionCardPatchStatus.MINIMAL_APPLIED)

    @classmethod
    def retry_later(
        cls,
        retry_after_seconds: float,
        *,
        retry_model: ExecutionCardModel,
    ) -> ExecutionCardPatchOutcome:
        return cls(
            status=ExecutionCardPatchStatus.RETRYABLE,
            retry_after_seconds=max(float(retry_after_seconds), 0.0),
            retry_model=retry_model,
        )

    @classmethod
    def failed(cls) -> ExecutionCardPatchOutcome:
        return cls(status=ExecutionCardPatchStatus.FAILED)

    @property
    def applied(self) -> bool:
        return self.status in {
            ExecutionCardPatchStatus.FULL_APPLIED,
            ExecutionCardPatchStatus.MINIMAL_APPLIED,
        }

    @property
    def full_content_applied(self) -> bool:
        return self.status == ExecutionCardPatchStatus.FULL_APPLIED

    @property
    def retryable(self) -> bool:
        return self.status == ExecutionCardPatchStatus.RETRYABLE


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


@dataclass(slots=True)
class _ExecutionCardPatchSlot:
    queued: bool = False
    inflight: bool = False
    retry_scheduled: bool = False


_PATCH_DISPATCHER_STOP = object()


class _CardPublisherBot(Protocol):
    def patch_message(self, message_id: str, content: str) -> bool: ...
    def delete_message(self, message_id: str) -> bool: ...

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

    def _patch_message_result(self, message_id: str, content: str) -> MessagePatchResult:
        patch_message_result = getattr(self._bot, "patch_message_result", None)
        if callable(patch_message_result):
            result = patch_message_result(message_id, content)
            if isinstance(result, MessagePatchResult):
                return result
            if result:
                return MessagePatchResult.success()
            return MessagePatchResult.failure()
        if self._bot.patch_message(message_id, content):
            return MessagePatchResult.success()
        return MessagePatchResult.failure()

    def send_execution_card(
        self,
        chat_id: str,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        content = json.dumps(render_execution_card(ExecutionCardModel.running_placeholder()), ensure_ascii=False)
        if parent_message_id:
            return self._bot.reply_to_message(
                parent_message_id,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
            )
        return self._bot.send_message_get_id(chat_id, "interactive", content)

    def patch_execution_card(
        self,
        message_id: str,
        model: ExecutionCardModel,
    ) -> ExecutionCardPatchOutcome:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return ExecutionCardPatchOutcome.failed()
        full_result = self._patch_message_result(
            normalized_message_id,
            json.dumps(render_execution_card(model), ensure_ascii=False),
        )
        if full_result.ok:
            outcome = ExecutionCardPatchOutcome.full_applied()
        elif full_result.retryable:
            outcome = ExecutionCardPatchOutcome.retry_later(
                full_result.retry_after_seconds,
                retry_model=model,
            )
        elif (
            full_result.content_rejected
            and not model.running
            and (model.log_text or model.reply_segments)
        ):
            logger.warning(
                "执行卡片完整终态内容被飞书拒绝，尝试极简终态卡: message_id=%s",
                normalized_message_id,
            )
            minimal_model = ExecutionCardModel(
                log_text="",
                reply_segments=(),
                running=False,
                elapsed=model.elapsed,
                cancelled=model.cancelled,
            )
            minimal_result = self._patch_message_result(
                normalized_message_id,
                json.dumps(render_execution_card(minimal_model), ensure_ascii=False),
            )
            if minimal_result.ok:
                outcome = ExecutionCardPatchOutcome.minimal_applied()
            elif minimal_result.retryable:
                outcome = ExecutionCardPatchOutcome.retry_later(
                    minimal_result.retry_after_seconds,
                    retry_model=minimal_model,
                )
            else:
                outcome = ExecutionCardPatchOutcome.failed()
        else:
            outcome = ExecutionCardPatchOutcome.failed()
        if outcome.applied and not model.running:
            logger.info(
                "执行卡片终态更新成功: message_id=%s elapsed=%s cancelled=%s log_chars=%s reply_segments=%s outcome=%s",
                normalized_message_id,
                model.elapsed,
                model.cancelled,
                len(model.log_text),
                len(model.reply_segments),
                outcome.status,
            )
        return outcome

    def delete_card_message(self, message_id: str) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        return self._bot.delete_message(normalized_message_id)

    def publish_terminal_result_card(
        self,
        *,
        chat_id: str,
        parent_message_id: str,
        final_reply_text: str,
        terminal_result_id: str = "",
        checksum: str = "",
        reply_in_thread: bool = False,
    ) -> str | None:
        content = build_terminal_result_card_message_content(
            final_reply_text,
            terminal_result_id=terminal_result_id,
            checksum=checksum,
        )
        normalized_parent = str(parent_message_id or "").strip()
        if normalized_parent:
            message_id = self._bot.reply_to_message(
                normalized_parent,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
            )
            if message_id:
                return message_id
        return self._bot.send_message_get_id(chat_id, "interactive", content)

    def publish_plan_card(
        self,
        *,
        chat_id: str,
        parent_message_id: str,
        plan_message_id: str,
        model: PlanCardModel,
        reply_in_thread: bool = False,
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
            new_message_id = self._bot.reply_to_message(
                parent_message_id,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
            )
        if not new_message_id:
            new_message_id = self._bot.send_message_get_id(chat_id, "interactive", content)
        normalized_new_id = str(new_message_id or "").strip() or None
        return PlanCardPublishResult(
            message_id=normalized_new_id,
            attempted_existing=attempted_existing,
            reused_existing=False,
        )


class ExecutionCardPatchDispatcher:
    def __init__(
        self,
        publish_patch: Callable[[str, ExecutionCardModel], ExecutionCardPatchOutcome],
        *,
        worker_count: int = 2,
    ) -> None:
        self._publish_patch = publish_patch
        self._worker_count = max(int(worker_count), 1)
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, ExecutionCardModel] = {}
        self._slots: dict[str, _ExecutionCardPatchSlot] = {}
        self._retry_timers: dict[str, threading.Timer] = {}
        self._workers: list[threading.Thread] = []
        self._closed = False

    def submit(self, message_id: str, model: ExecutionCardModel) -> None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return
        with self._lock:
            if self._closed:
                return
            self._pending[normalized_message_id] = model
            slot = self._slots.setdefault(normalized_message_id, _ExecutionCardPatchSlot())
            if slot.queued or slot.inflight or slot.retry_scheduled:
                return
            slot.queued = True
            self._ensure_workers_locked()
            self._queue.put(normalized_message_id)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            workers = list(self._workers)
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        for _ in workers:
            self._queue.put(_PATCH_DISPATCHER_STOP)
        for timer in timers:
            timer.cancel()
        for worker in workers:
            if worker.is_alive():
                worker.join(timeout=1)

    def _ensure_workers_locked(self) -> None:
        while len(self._workers) < self._worker_count:
            worker = threading.Thread(
                target=self._run_worker,
                name=f"execution-card-patch-{len(self._workers) + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _run_worker(self) -> None:
        while True:
            message_id = self._queue.get()
            if message_id is _PATCH_DISPATCHER_STOP:
                return
            assert isinstance(message_id, str)
            with self._lock:
                slot = self._slots.setdefault(message_id, _ExecutionCardPatchSlot())
                slot.queued = False
                model = self._pending.pop(message_id, None)
                if model is None:
                    if not slot.inflight:
                        self._slots.pop(message_id, None)
                    continue
                slot.inflight = True
            result = ExecutionCardPatchOutcome.failed()
            try:
                result = self._publish_patch(message_id, model)
            finally:
                with self._lock:
                    slot = self._slots.setdefault(message_id, _ExecutionCardPatchSlot())
                    slot.inflight = False
                    if result.retryable and not self._closed:
                        retry_model = result.retry_model
                        if retry_model is None:
                            logger.error(
                                "执行卡片 patch 返回了缺少 retry_model 的可重试结果，已拒绝重试: message_id=%s",
                                message_id,
                            )
                        else:
                            if message_id not in self._pending:
                                self._pending[message_id] = retry_model
                            self._schedule_retry_locked(message_id, result.retry_after_seconds)
                    if (
                        self._pending.get(message_id) is not None
                        and not slot.queued
                        and not slot.retry_scheduled
                        and not self._closed
                    ):
                        slot.queued = True
                        self._queue.put(message_id)
                    elif (
                        message_id not in self._pending
                        and not slot.queued
                        and not slot.retry_scheduled
                    ):
                        self._slots.pop(message_id, None)

    def _schedule_retry_locked(self, message_id: str, delay_seconds: float) -> None:
        slot = self._slots.setdefault(message_id, _ExecutionCardPatchSlot())
        if slot.retry_scheduled or self._closed:
            return
        slot.retry_scheduled = True
        timer = threading.Timer(
            max(float(delay_seconds), 0.0),
            self._retry_ready,
            args=(message_id,),
        )
        timer.daemon = True
        self._retry_timers[message_id] = timer
        timer.start()

    def _retry_ready(self, message_id: str) -> None:
        with self._lock:
            self._retry_timers.pop(message_id, None)
            slot = self._slots.get(message_id)
            if slot is None:
                return
            slot.retry_scheduled = False
            if self._closed:
                if not slot.queued and not slot.inflight and message_id not in self._pending:
                    self._slots.pop(message_id, None)
                return
            if slot.queued or slot.inflight:
                return
            if message_id not in self._pending:
                self._slots.pop(message_id, None)
                return
            slot.queued = True
            self._ensure_workers_locked()
            self._queue.put(message_id)
