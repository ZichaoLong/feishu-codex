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
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from bot.binding_runtime_contract import BindingPlanSnapshot
from bot.cards import build_execution_card, build_plan_card, build_terminal_result_card_message_content
from bot.execution_pages import ExecutionTranscriptCursor
from bot.execution_transcript import ExecutionReplySegment
from bot.feishu_outbound import (
    FeishuDestinationLiveness,
    FeishuOutboundEffect,
    FeishuOutboundResult,
)

logger = logging.getLogger(__name__)

EXECUTION_PAGE_PAYLOAD_LIMIT_BYTES = 26_000
EXECUTION_PAGE_COMPONENT_LIMIT = 80


class ExecutionCardPageBudgetError(RuntimeError):
    """The fixed execution-card shell cannot fit its configured page budget."""


class _ExecutionTranscriptProjection(Protocol):
    def process_text(self) -> str: ...

    def reply_content_chars(self) -> int: ...

    def reply_segments_between(
        self,
        start_chars: int,
        end_chars: int,
    ) -> tuple[ExecutionReplySegment, ...] | list[ExecutionReplySegment]: ...


class ExecutionCardPatchDispatcherShutdownTimeoutError(RuntimeError):
    """Raised when patch workers cannot be proven stopped."""


@dataclass(frozen=True, slots=True)
class ExecutionCardModel:
    log_text: str
    reply_segments: tuple[ExecutionReplySegment, ...]
    running: bool
    elapsed: int
    cancelled: bool
    cancelable: bool = True

    @classmethod
    def running_placeholder(
        cls,
        *,
        cancelable: bool = True,
    ) -> ExecutionCardModel:
        return cls(
            log_text="",
            reply_segments=(),
            running=True,
            elapsed=0,
            cancelled=False,
            cancelable=cancelable,
        )


@dataclass(frozen=True, slots=True)
class ExecutionCardPayloadMetrics:
    utf8_bytes: int
    component_count: int


class ExecutionCardPatchStatus(StrEnum):
    FULL_APPLIED = "full_applied"
    MINIMAL_APPLIED = "minimal_applied"
    RETRYABLE = "retryable"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExecutionCardPatchOutcome:
    status: ExecutionCardPatchStatus
    retry_after_seconds: float = 0.0
    retry_model: ExecutionCardModel | None = None
    fallback_safe: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExecutionCardPatchStatus):
            raise TypeError("status must be an ExecutionCardPatchStatus")
        if type(self.fallback_safe) is not bool:
            raise TypeError("fallback_safe must be bool")
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
        return cls(
            status=ExecutionCardPatchStatus.MINIMAL_APPLIED,
            fallback_safe=True,
        )

    @classmethod
    def retry_later(
        cls,
        retry_after_seconds: float,
        *,
        retry_model: ExecutionCardModel,
        fallback_safe: bool = False,
    ) -> ExecutionCardPatchOutcome:
        return cls(
            status=ExecutionCardPatchStatus.RETRYABLE,
            retry_after_seconds=max(float(retry_after_seconds), 0.0),
            retry_model=retry_model,
            fallback_safe=fallback_safe,
        )

    @classmethod
    def failed(
        cls,
        *,
        safe_to_fallback: bool = True,
    ) -> ExecutionCardPatchOutcome:
        return cls(
            status=ExecutionCardPatchStatus.FAILED,
            fallback_safe=safe_to_fallback,
        )

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

    @property
    def safe_to_fallback(self) -> bool:
        return self.fallback_safe


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
    outcome_unknown: bool = False


@dataclass(slots=True)
class _ExecutionCardPatchSlot:
    queued: bool = False
    inflight: bool = False
    retry_scheduled: bool = False


_PATCH_DISPATCHER_STOP = object()


class _CardPublisherBot(Protocol):
    def patch_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult: ...
    def delete_message(self, message_id: str) -> bool: ...

    def reply_to_message(
        self,
        chat_id: str,
        parent_id: str,
        msg_type: str,
        content: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str = "",
    ) -> FeishuOutboundResult: ...

    def send_message(
        self,
        chat_id: str,
        msg_type: str,
        content: str,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult: ...


def build_execution_card_model(
    transcript: _ExecutionTranscriptProjection,
    *,
    running: bool,
    elapsed: int,
    cancelled: bool,
    cancelable: bool = True,
    cursor_start: ExecutionTranscriptCursor | None = None,
    cursor_end: ExecutionTranscriptCursor | None = None,
) -> ExecutionCardModel:
    start = cursor_start or ExecutionTranscriptCursor()
    end = cursor_end or ExecutionTranscriptCursor.from_transcript(transcript)
    if type(start) is not ExecutionTranscriptCursor or type(
        end
    ) is not ExecutionTranscriptCursor:
        raise TypeError("execution card page cursors must be typed")
    transcript_end = ExecutionTranscriptCursor.from_transcript(transcript)
    if not end.follows(start):
        raise ValueError("execution card page cursor range must be ordered")
    if not transcript_end.follows(end):
        raise ValueError("execution card page cursor exceeds the transcript")
    process_text = transcript.process_text()[
        start.process_chars : end.process_chars
    ]
    return ExecutionCardModel(
        log_text=process_text,
        reply_segments=tuple(
            transcript.reply_segments_between(
                start.reply_chars,
                end.reply_chars,
            )
        ),
        running=running,
        elapsed=elapsed,
        cancelled=cancelled and not running,
        cancelable=cancelable,
    )


def render_execution_card(model: ExecutionCardModel) -> dict:
    return build_execution_card(
        model.log_text,
        list(model.reply_segments),
        running=model.running,
        elapsed=model.elapsed,
        cancelled=model.cancelled,
        cancelable=model.cancelable,
    )


def serialize_execution_card(model: ExecutionCardModel) -> str:
    return json.dumps(render_execution_card(model), ensure_ascii=False)


def execution_card_payload_metrics(
    model: ExecutionCardModel,
) -> ExecutionCardPayloadMetrics:
    card = render_execution_card(model)

    def _count_components(value: object) -> int:
        if isinstance(value, dict):
            own = 1 if type(value.get("tag")) is str else 0
            return own + sum(_count_components(item) for item in value.values())
        if isinstance(value, list):
            return sum(_count_components(item) for item in value)
        return 0

    return ExecutionCardPayloadMetrics(
        utf8_bytes=len(
            json.dumps(card, ensure_ascii=False).encode("utf-8")
        ),
        component_count=_count_components(card),
    )


def execution_card_model_fits_page(
    model: ExecutionCardModel,
    *,
    payload_limit_bytes: int = EXECUTION_PAGE_PAYLOAD_LIMIT_BYTES,
    component_limit: int = EXECUTION_PAGE_COMPONENT_LIMIT,
) -> bool:
    if type(payload_limit_bytes) is not int or payload_limit_bytes < 1:
        raise ValueError("execution page payload limit must be a positive int")
    if type(component_limit) is not int or component_limit < 1:
        raise ValueError("execution page component limit must be a positive int")
    metrics = execution_card_payload_metrics(model)
    return bool(
        metrics.utf8_bytes <= payload_limit_bytes
        and metrics.component_count <= component_limit
    )


def fit_execution_card_page_end(
    transcript: _ExecutionTranscriptProjection,
    *,
    cursor_start: ExecutionTranscriptCursor,
    cursor_end: ExecutionTranscriptCursor,
    running: bool,
    elapsed: int,
    cancelled: bool,
    cancelable: bool = True,
    payload_limit_bytes: int = EXECUTION_PAGE_PAYLOAD_LIMIT_BYTES,
    component_limit: int = EXECUTION_PAGE_COMPONENT_LIMIT,
) -> ExecutionTranscriptCursor:
    """Return a deterministic safe prefix endpoint for one execution page."""

    if type(cursor_start) is not ExecutionTranscriptCursor or type(
        cursor_end
    ) is not ExecutionTranscriptCursor:
        raise TypeError("execution page fit requires typed cursors")
    if not cursor_end.follows(cursor_start):
        raise ValueError("execution page fit cursor range must be ordered")

    process_chars = cursor_end.process_chars - cursor_start.process_chars
    reply_chars = cursor_end.reply_chars - cursor_start.reply_chars
    total_chars = process_chars + reply_chars

    def _cursor_at(progress: int) -> ExecutionTranscriptCursor:
        process_progress = min(progress, process_chars)
        reply_progress = max(progress - process_chars, 0)
        return ExecutionTranscriptCursor(
            process_chars=cursor_start.process_chars + process_progress,
            reply_chars=cursor_start.reply_chars + reply_progress,
        )

    def _fits(progress: int) -> bool:
        model = build_execution_card_model(
            transcript,
            running=running,
            elapsed=elapsed,
            cancelled=cancelled,
            cancelable=cancelable,
            cursor_start=cursor_start,
            cursor_end=_cursor_at(progress),
        )
        return execution_card_model_fits_page(
            model,
            payload_limit_bytes=payload_limit_bytes,
            component_limit=component_limit,
        )

    if not _fits(0):
        raise ExecutionCardPageBudgetError(
            "execution card shell exceeds the configured page budget"
        )
    low = 1
    high = total_chars
    best = 0
    while low <= high:
        midpoint = (low + high) // 2
        if _fits(midpoint):
            best = midpoint
            low = midpoint + 1
        else:
            high = midpoint - 1
    if total_chars and best == 0:
        raise ExecutionCardPageBudgetError(
            "execution page budget cannot fit one transcript character"
        )
    return _cursor_at(best)


def build_plan_card_model(plan: BindingPlanSnapshot) -> PlanCardModel:
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

    def publish_interactive_card(
        self,
        chat_id: str,
        card: dict,
        parent_message_id: str,
        reply_in_thread: bool,
        *,
        attempt_id: str = "",
    ) -> FeishuOutboundResult:
        """Publish exactly one card effect, optionally with a caller-owned UUID."""

        content = json.dumps(card, ensure_ascii=False)
        outbound_attempt_id = str(attempt_id or "").strip() or uuid.uuid4().hex
        return self._publish_interactive_card_effect(
            chat_id,
            content,
            parent_message_id,
            reply_in_thread,
            attempt_id=outbound_attempt_id,
        )

    def send_interactive_card(
        self,
        chat_id: str,
        card: dict,
        parent_message_id: str,
        reply_in_thread: bool,
    ) -> str | None:
        """Publish one interaction card without manufacturing a second effect."""

        result = self.publish_interactive_card(
            chat_id,
            card,
            parent_message_id,
            reply_in_thread,
        )
        return result.message_id if result.ok else None

    def _publish_interactive_card_effect(
        self,
        chat_id: str,
        content: str,
        parent_message_id: str,
        reply_in_thread: bool,
        *,
        attempt_id: str,
    ) -> FeishuOutboundResult:
        if parent_message_id:
            return self._bot.reply_to_message(
                chat_id,
                parent_message_id,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
                attempt_id=attempt_id,
            )
        return self._bot.send_message(
            chat_id,
            "interactive",
            content,
            attempt_id=attempt_id,
        )

    def send_execution_card(
        self,
        chat_id: str,
        parent_message_id: str,
        *,
        reply_in_thread: bool = False,
        attempt_id: str,
        cancelable: bool = True,
    ) -> FeishuOutboundResult:
        content = serialize_execution_card(
            ExecutionCardModel.running_placeholder(cancelable=cancelable)
        )
        if parent_message_id:
            return self._bot.reply_to_message(
                chat_id,
                parent_message_id,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
                attempt_id=attempt_id,
            )
        return self._bot.send_message(
            chat_id,
            "interactive",
            content,
            attempt_id=attempt_id,
        )

    def patch_execution_card(
        self,
        chat_id: str,
        message_id: str,
        model: ExecutionCardModel,
        *,
        attempt_id: str = "",
    ) -> ExecutionCardPatchOutcome:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return ExecutionCardPatchOutcome.failed()
        full_result = self._bot.patch_message(
            chat_id,
            normalized_message_id,
            serialize_execution_card(model),
            attempt_id=attempt_id,
        )
        if full_result.ok:
            outcome = ExecutionCardPatchOutcome.full_applied()
        elif full_result.retryable:
            outcome = ExecutionCardPatchOutcome.retry_later(
                full_result.retry_after_seconds,
                retry_model=model,
                fallback_safe=full_result.safe_to_fallback,
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
                cancelable=model.cancelable,
            )
            minimal_result = self._bot.patch_message(
                chat_id,
                normalized_message_id,
                serialize_execution_card(minimal_model),
            )
            if minimal_result.ok:
                outcome = ExecutionCardPatchOutcome.minimal_applied()
            elif minimal_result.retryable:
                outcome = ExecutionCardPatchOutcome.retry_later(
                    minimal_result.retry_after_seconds,
                    retry_model=minimal_model,
                    fallback_safe=minimal_result.safe_to_fallback,
                )
            elif minimal_result.effect is FeishuOutboundEffect.UNKNOWN:
                outcome = ExecutionCardPatchOutcome(
                    status=ExecutionCardPatchStatus.UNKNOWN
                )
            else:
                outcome = ExecutionCardPatchOutcome.failed(
                    safe_to_fallback=minimal_result.safe_to_fallback,
                )
        elif full_result.effect is FeishuOutboundEffect.UNKNOWN:
            outcome = ExecutionCardPatchOutcome(
                status=ExecutionCardPatchStatus.UNKNOWN
            )
        else:
            outcome = ExecutionCardPatchOutcome.failed(
                safe_to_fallback=full_result.safe_to_fallback,
            )
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
    ) -> FeishuOutboundResult:
        content = build_terminal_result_card_message_content(
            final_reply_text,
            terminal_result_id=terminal_result_id,
            checksum=checksum,
        )
        normalized_parent = str(parent_message_id or "").strip()
        if normalized_parent:
            reply_result = self._bot.reply_to_message(
                chat_id,
                normalized_parent,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
            )
            if reply_result.ok or not reply_result.safe_to_fallback:
                return reply_result
        return self._bot.send_message(chat_id, "interactive", content)

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
        if normalized_existing:
            patch_result = self._bot.patch_message(
                chat_id,
                normalized_existing,
                content,
            )
            if patch_result.ok:
                return PlanCardPublishResult(
                    message_id=normalized_existing,
                    attempted_existing=True,
                    reused_existing=True,
                )
            if not patch_result.safe_to_fallback:
                return PlanCardPublishResult(
                    message_id=normalized_existing,
                    attempted_existing=True,
                    reused_existing=False,
                    outcome_unknown=(
                        patch_result.effect is FeishuOutboundEffect.UNKNOWN
                        or patch_result.destination_liveness
                        is FeishuDestinationLiveness.PROVEN_UNREACHABLE
                    ),
                )

        send_result: FeishuOutboundResult | None = None
        if parent_message_id:
            send_result = self._bot.reply_to_message(
                chat_id,
                parent_message_id,
                "interactive",
                content,
                reply_in_thread=reply_in_thread,
            )
        if send_result is None or (
            not send_result.ok and send_result.safe_to_fallback
        ):
            send_result = self._bot.send_message(chat_id, "interactive", content)
        normalized_new_id = send_result.message_id if send_result.ok else None
        return PlanCardPublishResult(
            message_id=normalized_new_id,
            attempted_existing=attempted_existing,
            reused_existing=False,
            outcome_unknown=(
                send_result.effect is FeishuOutboundEffect.UNKNOWN
                or send_result.destination_liveness
                is FeishuDestinationLiveness.PROVEN_UNREACHABLE
            ),
        )


class ExecutionCardPatchDispatcher:
    def __init__(
        self,
        publish_patch: Callable[
            [str, str, ExecutionCardModel],
            ExecutionCardPatchOutcome,
        ],
        *,
        worker_count: int = 2,
    ) -> None:
        self._publish_patch = publish_patch
        self._worker_count = max(int(worker_count), 1)
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[str, ExecutionCardModel]] = {}
        self._slots: dict[str, _ExecutionCardPatchSlot] = {}
        self._retry_timers: dict[str, threading.Timer] = {}
        self._workers: list[threading.Thread] = []
        self._closed = False

    def submit(
        self,
        chat_id: str,
        message_id: str,
        model: ExecutionCardModel,
    ) -> None:
        normalized_chat_id = str(chat_id or "").strip()
        normalized_message_id = str(message_id or "").strip()
        if not normalized_chat_id or not normalized_message_id:
            return
        with self._lock:
            if self._closed:
                return
            self._pending[normalized_message_id] = (normalized_chat_id, model)
            slot = self._slots.setdefault(normalized_message_id, _ExecutionCardPatchSlot())
            if slot.queued or slot.inflight or slot.retry_scheduled:
                return
            slot.queued = True
            self._ensure_workers_locked()
            self._queue.put(normalized_message_id)

    def shutdown(self, *, timeout: float | None = 1.0) -> None:
        with self._lock:
            first_close = not self._closed
            self._closed = True
            workers = list(self._workers)
            timers = list(self._retry_timers.values())
            self._retry_timers.clear()
        if first_close:
            for _ in workers:
                self._queue.put(_PATCH_DISPATCHER_STOP)
            for timer in timers:
                timer.cancel()
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0.0)
        for worker in workers:
            if worker.is_alive() and worker is not threading.current_thread():
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
                worker.join(timeout=remaining)
        live_workers = [
            worker
            for worker in workers
            if worker is not threading.current_thread() and worker.is_alive()
        ]
        if live_workers:
            raise ExecutionCardPatchDispatcherShutdownTimeoutError(
                f"{len(live_workers)} execution card patch worker(s) did not stop"
            )

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
                pending = self._pending.pop(message_id, None)
                if pending is None:
                    if not slot.inflight:
                        self._slots.pop(message_id, None)
                    continue
                chat_id, model = pending
                slot.inflight = True
            result = ExecutionCardPatchOutcome.failed()
            try:
                result = self._publish_patch(chat_id, message_id, model)
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
                                self._pending[message_id] = (chat_id, retry_model)
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
