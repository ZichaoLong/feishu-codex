"""
Presentation and publishing helpers for Codex runtime cards.

These helpers keep Feishu card payload assembly and message IO out of
``CodexHandler`` so the handler can stay focused on orchestration.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from bot.cards import build_execution_card, build_plan_card, build_terminal_result_card
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


@dataclass(slots=True)
class _ExecutionCardPatchSlot:
    queued: bool = False
    inflight: bool = False


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

    def patch_execution_card(self, message_id: str, model: ExecutionCardModel) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return False
        return self._bot.patch_message(
            normalized_message_id,
            json.dumps(render_execution_card(model), ensure_ascii=False),
        )

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
        reply_in_thread: bool = False,
    ) -> str | None:
        content = json.dumps(build_terminal_result_card(final_reply_text), ensure_ascii=False)
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
        publish_patch: Callable[[str, ExecutionCardModel], bool],
        *,
        worker_count: int = 2,
    ) -> None:
        self._publish_patch = publish_patch
        self._worker_count = max(int(worker_count), 1)
        self._queue: queue.Queue[str | object] = queue.Queue()
        self._lock = threading.Lock()
        self._pending: dict[str, ExecutionCardModel] = {}
        self._slots: dict[str, _ExecutionCardPatchSlot] = {}
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
            if slot.queued or slot.inflight:
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
        for _ in workers:
            self._queue.put(_PATCH_DISPATCHER_STOP)
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
            try:
                self._publish_patch(message_id, model)
            finally:
                with self._lock:
                    slot = self._slots.setdefault(message_id, _ExecutionCardPatchSlot())
                    slot.inflight = False
                    if self._pending.get(message_id) is not None and not slot.queued and not self._closed:
                        slot.queued = True
                        self._queue.put(message_id)
                    elif message_id not in self._pending and not slot.queued:
                        self._slots.pop(message_id, None)
