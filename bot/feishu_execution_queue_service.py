"""Composition service for the Feishu binding execution FIFO.

``FeishuExecutionQueueController`` is the only state owner.  This service owns
the cross-owner transaction around that state: product admission, message
origin recovery, typed effect execution, exact receipt completion, and terminal
settlement ordering. Keeping upstream callbacks here avoids a Queue ->
PromptEntry -> RootOperation -> Queue construction cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from bot.binding_identity import format_binding_id
from bot.feishu_compact_execution_service import FeishuCompactStartResult
from bot.feishu_execution_queue import (
    ChatBindingKey,
    FeishuBindingExecutionSnapshot,
    FeishuExecutionQueueAdmissionError,
    FeishuExecutionQueueController,
    FeishuQueueDrainOutcome,
    FeishuQueueRecallOutcome,
    FeishuQueuedMessageOrigin,
    StartCompactEffect,
    StartPromptEffect,
)
from bot.prompt_input_items import replace_text_input_items
from bot.reason_codes import PROMPT_DENIED_BY_INTERACTION_OWNER, ReasonedCheck
from bot.runtime_loop import RuntimeContextGuard


logger = logging.getLogger(__name__)

_STALE_QUEUE_ADMISSION_TEXT = (
    "queue admission 前 binding/root/turn 已变化；旧输入不能跟随 replacement。"
)
_QUEUED_PROMPT_PREPARATION_FAILURE_TEXT = "排队消息预处理失败，本次输入未执行。"
_OTHER_BINDING_QUEUE_TEXT = (
    "当前 thread 已由另一飞书会话保留后续输入；本次消息未排队，"
    "请等待该队列处理完成后再试。"
)


class _StaleQueueIngress(RuntimeError):
    """The locked binding/root/turn no longer matches ingress."""


class _PromptQueueAdmissionDenied(RuntimeError):
    """The locked typed policy rejected this exact prompt enqueue."""

    def __init__(self, check: ReasonedCheck) -> None:
        self.check = check
        super().__init__(check.reason_text)


@dataclass(frozen=True, slots=True)
class FeishuQueueIngressSnapshot:
    """Immutable input-side view; no mutable binding runtime escapes."""

    binding: ChatBindingKey
    current_root_thread_id: str
    current_turn_id: str
    has_execution_anchor: bool


@dataclass(frozen=True, slots=True)
class FeishuExecutionQueueServicePorts:
    lock: Any
    ingress_snapshot: Callable[[str, str, str], FeishuQueueIngressSnapshot]
    binding_execution_snapshot_locked: Callable[
        [ChatBindingKey], FeishuBindingExecutionSnapshot | None
    ]
    binding_execution_active_locked: Callable[[ChatBindingKey], bool]
    writer_denial_text: Callable[[ChatBindingKey, str, str, str], str]
    current_process_local_turn_id: Callable[[str], str]
    prompt_queue_admission_check: Callable[
        [ChatBindingKey, str, str, str, str, bool], ReasonedCheck
    ]
    start_prompt: Callable[..., Any]
    start_compact: Callable[..., FeishuCompactStartResult]
    load_message_context: Callable[[str], Mapping[str, Any]]
    remember_message_context: Callable[[str, dict[str, Any]], None]
    prepare_queued_prompt_text: Callable[..., str | None]
    reply_text: Callable[..., None]
    reconcile_terminal: Callable[[str], None]


class FeishuExecutionQueueService:
    """Own queue admission/effect transactions without owning queue facts."""

    def __init__(
        self,
        *,
        queue: FeishuExecutionQueueController,
        ports: FeishuExecutionQueueServicePorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not isinstance(queue, FeishuExecutionQueueController):
            raise TypeError("Feishu execution queue service 缺少 state owner。")
        if not callable(runtime_context_guard):
            raise TypeError("Feishu execution queue service 缺少 RuntimeLoop guard。")
        required = tuple(
            getattr(ports, name)
            for name in ports.__dataclass_fields__
            if name != "lock"
        )
        if any(not callable(capability) for capability in required):
            raise TypeError("Feishu execution queue service port 必须可调用。")
        self._queue = queue
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def start_or_enqueue_prompt(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        *,
        message_id: str = "",
        actor_open_id: str = "",
        input_items: list[dict[str, Any]] | None = None,
        synthetic_source: str = "",
        display_mode: str = "silent",
        surface_failures: bool = True,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        ingress = self._ports.ingress_snapshot(sender_id, chat_id, message_id)
        binding_id = format_binding_id(ingress.binding)
        response_root_thread_id = ingress.current_root_thread_id
        preprojection_local_turn_id = ""
        other_binding_continuity = False
        with self._ports.lock:
            initial_snapshot = self._ports.binding_execution_snapshot_locked(
                ingress.binding
            )
            exact_queue_continuity = self._queue.continuity_pending(
                initial_snapshot,
                ingress.current_root_thread_id,
            )
            other_binding_continuity = self._queue.has_other_binding_continuity(
                ingress.binding,
                ingress.current_root_thread_id,
            )
            if (
                not ingress.has_execution_anchor
                and not exact_queue_continuity
                and ingress.current_root_thread_id
            ):
                preprojection_local_turn_id = self._ports.current_process_local_turn_id(
                    ingress.current_root_thread_id
                )
        if other_binding_continuity:
            denial = self._other_binding_queue_denial()
            if surface_failures:
                self._reply_text_best_effort(
                    chat_id,
                    denial.reason_text,
                    message_id=message_id,
                )
            return self._denied(
                binding_id,
                response_root_thread_id,
                reason_code=denial.reason_code,
                reason=denial.reason_text,
            )
        if (
            not ingress.has_execution_anchor
            and not exact_queue_continuity
            and not preprojection_local_turn_id
        ):
            result = self._ports.start_prompt(
                sender_id,
                chat_id,
                text,
                message_id=message_id,
                actor_open_id=actor_open_id,
                input_items=input_items,
                surface_failures=surface_failures,
            )
            return self._prompt_start_result(binding_id, result)

        origin = self._message_origin(message_id)
        queued_actor = str(actor_open_id or "").strip() or origin.sender_open_id
        queued_input_items = (
            input_items if input_items is not None else [{"type": "text", "text": text}]
        )
        try:
            with self._ports.lock:
                snapshot = self._require_fresh_ingress_snapshot(ingress)
                response_root_thread_id = snapshot.root_thread_id
                if self._queue.has_other_binding_continuity(
                    ingress.binding,
                    snapshot.root_thread_id,
                ):
                    raise _PromptQueueAdmissionDenied(
                        self._other_binding_queue_denial()
                    )
                exact_queue_continuity = self._queue.continuity_pending(
                    snapshot,
                    snapshot.root_thread_id,
                )
                if preprojection_local_turn_id:
                    fresh_local_turn_id = self._ports.current_process_local_turn_id(
                        snapshot.root_thread_id
                    )
                    if fresh_local_turn_id != preprojection_local_turn_id:
                        raise _StaleQueueIngress(_STALE_QUEUE_ADMISSION_TEXT)
                policy_check = self._ports.prompt_queue_admission_check(
                    ingress.binding,
                    chat_id,
                    snapshot.root_thread_id,
                    snapshot.current_turn_id or preprojection_local_turn_id,
                    message_id,
                    exact_queue_continuity,
                )
                if not policy_check.allowed:
                    raise _PromptQueueAdmissionDenied(policy_check)
                admission = self._queue.enqueue_prompt(
                    snapshot,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    actor_open_id=queued_actor,
                    origin=origin,
                    input_items=queued_input_items,
                    synthetic_source=synthetic_source,
                    display_mode=display_mode,
                    surface_failures=surface_failures,
                    preprojection_local_turn_id=preprojection_local_turn_id,
                )
        except _PromptQueueAdmissionDenied as exc:
            reason = exc.check.reason_text
            if surface_failures:
                self._reply_text_best_effort(
                    chat_id,
                    reason,
                    message_id=message_id,
                )
            return self._denied(
                binding_id,
                response_root_thread_id,
                reason_code=exc.check.reason_code,
                reason=reason,
            )
        except _StaleQueueIngress:
            if surface_failures:
                self._reply_text_best_effort(
                    chat_id,
                    _STALE_QUEUE_ADMISSION_TEXT,
                    message_id=message_id,
                )
            return self._denied(
                binding_id,
                response_root_thread_id,
                reason_code="stale_queue_admission",
                reason=_STALE_QUEUE_ADMISSION_TEXT,
            )
        except FeishuExecutionQueueAdmissionError:
            reason = "当前线程仍在执行，请等待结束或先执行 `/cancel`。"
            if surface_failures:
                self._reply_text_best_effort(
                    chat_id,
                    reason,
                    message_id=message_id,
                )
            return self._denied(
                binding_id,
                response_root_thread_id,
                reason_code="prompt_denied_by_running_turn",
                reason=reason,
            )

        if surface_failures:
            self._reply_text_best_effort(
                chat_id,
                f"已排队，将在当前执行结束后继续。队列位置：{admission.queue_position}",
                message_id=message_id,
            )
        return self._queued(
            binding_id,
            admission.root_thread_id,
            admission.queue_position,
        )

    def start_or_enqueue_compact(
        self,
        sender_id: str,
        chat_id: str,
        *,
        message_id: str = "",
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        ingress = self._ports.ingress_snapshot(sender_id, chat_id, message_id)
        binding_id = format_binding_id(ingress.binding)
        root_thread_id = ingress.current_root_thread_id
        if not root_thread_id:
            return self._denied(
                binding_id,
                "",
                reason_code="compact_denied_no_thread",
                reason=(
                    "当前还没有绑定 thread；先执行 `/new`，"
                    "或直接发送第一条普通消息创建线程。"
                ),
            )
        if not ingress.has_execution_anchor:
            return self._ports.start_compact(
                sender_id,
                chat_id,
                message_id=message_id,
            ).as_product_result()

        writer_denial = self._ports.writer_denial_text(
            ingress.binding,
            chat_id,
            root_thread_id,
            message_id,
        )
        if writer_denial:
            return self._denied(
                binding_id,
                root_thread_id,
                reason_code="compact_denied_by_thread_owner",
                reason=writer_denial,
            )
        origin = self._message_origin(message_id)
        try:
            with self._ports.lock:
                snapshot = self._require_fresh_ingress_snapshot(ingress)
                admission = self._queue.enqueue_compact(
                    snapshot,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    actor_open_id=origin.sender_open_id,
                    origin=origin,
                )
        except _StaleQueueIngress:
            return self._denied(
                binding_id,
                root_thread_id,
                reason_code="stale_queue_admission",
                reason=_STALE_QUEUE_ADMISSION_TEXT,
            )
        except FeishuExecutionQueueAdmissionError:
            return self._denied(
                binding_id,
                root_thread_id,
                reason_code="compact_denied_by_running_turn",
                reason="当前线程仍在执行，请等待结束或先执行 `/cancel`。",
            )
        return self._queued(binding_id, root_thread_id, admission.queue_position)

    def drain(
        self,
        binding: ChatBindingKey,
    ) -> None:
        """Execute any same-epoch drop/failure run without recursion."""

        self._runtime_context_guard()
        while True:
            preclaim_failure: Exception | None = None
            invalidation = None
            with self._ports.lock:
                try:
                    snapshot = self._ports.binding_execution_snapshot_locked(binding)
                    effect = self._queue.begin_terminal_drain(
                        binding,
                        snapshot,
                    )
                except Exception as exc:
                    # The terminal event is this FIFO's only wake-up. Before a
                    # receipt exists there is no exact head to block and no
                    # retry owner, so revoke this exact binding generation in
                    # the same lock domain instead of leaving orphaned work.
                    preclaim_failure = exc
                    invalidation = self._queue.invalidate_binding(binding)
            if preclaim_failure is not None:
                logger.error(
                    "Feishu queue pre-claim drain 失败；已失效当前 generation: "
                    "binding=%s",
                    format_binding_id(binding),
                    exc_info=(
                        type(preclaim_failure),
                        preclaim_failure,
                        preclaim_failure.__traceback__,
                    ),
                )
                if (
                    invalidation is not None
                    and invalidation.had_continuation
                    and invalidation.invalidated_root_thread_id
                ):
                    try:
                        self._ports.reconcile_terminal(
                            invalidation.invalidated_root_thread_id
                        )
                    except Exception:
                        logger.exception(
                            "Feishu queue pre-claim 失效后 terminal reconcile 失败: "
                            "binding=%s",
                            format_binding_id(binding),
                        )
                return
            if effect is None:
                return

            outcome: FeishuQueueDrainOutcome = "blocked"
            try:
                self._restore_message_origin_best_effort(effect)
                with self._ports.lock:
                    may_execute = self._queue.receipt_may_execute(
                        effect.receipt,
                        self._ports.binding_execution_snapshot_locked(binding),
                    )
                if not may_execute:
                    outcome = "dropped"
                elif isinstance(effect, StartPromptEffect):
                    outcome = self._execute_prompt(effect)
                else:
                    outcome = self._execute_compact(effect)
            except Exception:
                # Once a head is claimed, this service has no independent
                # scheduler that can safely replay it. An untyped
                # snapshot/receipt/effect-preflight
                # failure therefore consumes only this head and blocks the
                # same drain from crossing into its successor.
                logger.exception(
                    "queued Feishu effect preflight 未返回 typed outcome: binding=%s",
                    format_binding_id(binding),
                )
                outcome = "blocked"
            finally:
                with self._ports.lock:
                    completion = self._queue.complete_drain(
                        effect.receipt,
                        outcome=outcome,
                    )

            if completion.terminal_reconcile_root_thread_id:
                try:
                    self._ports.reconcile_terminal(
                        completion.terminal_reconcile_root_thread_id
                    )
                except Exception:
                    logger.exception(
                        "排队 Feishu 后续操作结束后结算 terminal owner 失败: "
                        "binding=%s",
                        format_binding_id(binding),
                    )
            if not completion.continue_same_epoch:
                return

    def remove_recalled_message(
        self,
        *,
        chat_id: str,
        message_id: str,
    ) -> FeishuQueueRecallOutcome:
        """Cancel queued message work and revisit any released terminal root."""

        self._runtime_context_guard()
        with self._ports.lock:
            outcome = self._queue.remove_recalled_message(
                chat_id=chat_id,
                message_id=message_id,
            )
        for root_thread_id in outcome.terminal_reconcile_root_thread_ids:
            try:
                self._ports.reconcile_terminal(root_thread_id)
            except Exception:
                logger.exception(
                    "撤回排队 Feishu 消息后结算 terminal owner 失败: root=%s",
                    root_thread_id[:12],
                )
        return outcome

    def preserves_owner(self, binding: ChatBindingKey, root_thread_id: str) -> bool:
        """Read queue continuity and BindingRuntime as one locked decision."""

        self._runtime_context_guard()
        with self._ports.lock:
            return self.preserves_owner_locked(binding, root_thread_id)

    def preserves_owner_locked(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> bool:
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return False
        snapshot = self._ports.binding_execution_snapshot_locked(binding)
        return self._queue.continuity_pending(snapshot, root_id)

    def binding_execution_active(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> bool:
        """Conservatively project the Feishu root-operation release gate."""

        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        with self._ports.lock:
            snapshot = self._ports.binding_execution_snapshot_locked(binding)
            if snapshot is None or snapshot.root_thread_id != root_id:
                return True
            return bool(
                self._ports.binding_execution_active_locked(binding)
                or self._queue.continuity_pending(snapshot, root_id)
            )

    def invalidate_group_continuity(self, binding: ChatBindingKey) -> bool:
        """Invalidate group authority, then revisit its old terminal root."""

        self._runtime_context_guard()
        with self._ports.lock:
            invalidation = self._queue.invalidate_binding(binding)
        if invalidation.had_continuation and invalidation.invalidated_root_thread_id:
            try:
                self._ports.reconcile_terminal(invalidation.invalidated_root_thread_id)
            except Exception:
                logger.exception(
                    "Unable to settle terminal owner after group FIFO "
                    "cancellation: binding=%s",
                    format_binding_id(binding),
                )
        return invalidation.had_continuation

    def _execute_prompt(
        self,
        effect: StartPromptEffect,
    ) -> FeishuQueueDrainOutcome:
        queued_text = effect.text
        queued_input_items = [dict(item) for item in effect.input_items]
        try:
            prepared_text = self._ports.prepare_queued_prompt_text(
                chat_id=effect.chat_id,
                message_id=effect.message_id,
                text=effect.text,
                assistant_context_mode=effect.origin.assistant_context_mode,
                assistant_context_created_at=(
                    effect.origin.assistant_context_created_at
                ),
                assistant_context_seq=effect.origin.assistant_context_seq,
                assistant_context_sender_name=(
                    effect.origin.assistant_context_sender_name
                ),
                origin_feishu_thread_id=effect.origin.feishu_thread_id,
            )
            if prepared_text is None:
                return "dropped"
            queued_text = str(prepared_text or "")
            if queued_text != effect.text:
                queued_input_items = replace_text_input_items(
                    queued_input_items,
                    queued_text,
                )
        except Exception:
            # Preparation and local input normalization run before any
            # upstream mutation. Retrying this head would require another
            # scheduler wake-up that the terminal drain contract does not
            # provide, and could block the FIFO forever. Consume it as a known
            # no-effect failure instead.
            logger.exception(
                "queued Feishu prompt preparation 失败: message=%s",
                str(effect.message_id or "")[:24],
            )
            if effect.surface_failures:
                self._reply_text_best_effort(
                    effect.chat_id,
                    _QUEUED_PROMPT_PREPARATION_FAILURE_TEXT,
                    message_id=effect.message_id,
                )
            return "known_no_effect_settled"

        if not self._effect_may_execute(effect):
            return "dropped"
        try:
            result = self._ports.start_prompt(
                effect.sender_id,
                effect.chat_id,
                queued_text,
                message_id=effect.message_id,
                actor_open_id=effect.actor_open_id,
                input_items=queued_input_items,
                surface_failures=effect.surface_failures,
                expected_binding=effect.binding,
                expected_root_thread_id=effect.admitted_root_thread_id,
                exact_admission_guard=lambda: self._effect_may_execute(effect),
                exact_mutation_guard=lambda: self._effect_may_mutate(effect),
            )
        except Exception:
            # The entry owner may already have crossed turn/start.  An
            # unclassified exception cannot authorize replay of this head.
            logger.exception("queued Feishu prompt start 未返回 typed outcome")
            return "blocked"
        outcome = self._start_result_outcome(result)
        if outcome != "started":
            return outcome
        if effect.display_mode == "announce":
            try:
                self._ports.reply_text(
                    effect.chat_id,
                    f"{effect.synthetic_source or '系统任务'}触发，开始新一轮执行。",
                    reply_in_thread=False,
                )
            except Exception:
                # turn/start is already committed.  Presentation failure must
                # never turn it back into a retryable queue head.
                logger.exception("queued Feishu prompt start announce 失败")
        return "started"

    def _execute_compact(
        self,
        effect: StartCompactEffect,
    ) -> FeishuQueueDrainOutcome:
        if not self._effect_may_execute(effect):
            return "dropped"
        try:
            result = self._ports.start_compact(
                effect.sender_id,
                effect.chat_id,
                message_id=effect.message_id,
                exact_admission_guard=lambda: self._effect_may_execute(effect),
                exact_mutation_guard=lambda: self._effect_may_mutate(effect),
            )
        except Exception:
            logger.exception("queued Feishu compact start 未返回 typed outcome")
            return "blocked"
        outcome = self._start_result_outcome(result)
        if outcome == "started":
            return outcome
        try:
            self._ports.reply_text(
                effect.chat_id,
                result.reason or "compact 失败。",
                message_id=effect.message_id,
            )
        except Exception:
            logger.exception("queued Feishu compact failure presentation 失败")
        return outcome

    def _message_origin(self, message_id: str) -> FeishuQueuedMessageOrigin:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return FeishuQueuedMessageOrigin()
        return FeishuQueuedMessageOrigin.from_message_context(
            self._ports.load_message_context(normalized_message_id)
        )

    def _effect_may_execute(
        self,
        effect: StartPromptEffect | StartCompactEffect,
    ) -> bool:
        with self._ports.lock:
            return self._queue.receipt_may_execute(
                effect.receipt,
                self._ports.binding_execution_snapshot_locked(effect.binding),
            )

    def _effect_may_mutate(
        self,
        effect: StartPromptEffect | StartCompactEffect,
    ) -> bool:
        with self._ports.lock:
            return self._queue.claimed_receipt_may_mutate(
                effect.receipt,
                self._ports.binding_execution_snapshot_locked(effect.binding),
            )

    @staticmethod
    def _start_result_outcome(result: Any) -> FeishuQueueDrainOutcome:
        if bool(getattr(result, "started", False)):
            return "started"
        disposition = str(getattr(result, "disposition", "") or "")
        if disposition == "known_no_effect_settled":
            return "known_no_effect_settled"
        return "blocked"

    def _require_fresh_ingress_snapshot(
        self,
        ingress: FeishuQueueIngressSnapshot,
    ) -> FeishuBindingExecutionSnapshot:
        snapshot = self._ports.binding_execution_snapshot_locked(ingress.binding)
        if (
            snapshot is None
            or snapshot.binding != ingress.binding
            or str(snapshot.root_thread_id or "").strip()
            != str(ingress.current_root_thread_id or "").strip()
            or str(snapshot.current_turn_id or "").strip()
            != str(ingress.current_turn_id or "").strip()
        ):
            raise _StaleQueueIngress(_STALE_QUEUE_ADMISSION_TEXT)
        return snapshot

    def _restore_message_origin_best_effort(
        self,
        effect: StartPromptEffect | StartCompactEffect,
    ) -> None:
        try:
            message_id = str(effect.message_id or "").strip()
            restored = effect.origin.message_context()
            if not message_id or not restored:
                return
            current = dict(self._ports.load_message_context(message_id) or {})
            changed = False
            for key, value in restored.items():
                if str(current.get(key, "") or "").strip():
                    continue
                current[key] = value
                changed = True
            if changed:
                self._ports.remember_message_context(message_id, current)
        except Exception:
            # The frozen queue effect already carries every origin fact used
            # by prompt preparation.  Repopulating the adapter cache is only
            # a compatibility/presentation aid and must not retain the head.
            logger.exception(
                "queued Feishu message origin restore 失败: message=%s",
                str(effect.message_id or "")[:24],
            )

    def _reply_text_best_effort(self, *args: Any, **kwargs: Any) -> None:
        try:
            self._ports.reply_text(*args, **kwargs)
        except Exception:
            logger.exception("Feishu queue no-mutation presentation 失败")

    @staticmethod
    def _other_binding_queue_denial() -> ReasonedCheck:
        return ReasonedCheck.deny(
            PROMPT_DENIED_BY_INTERACTION_OWNER,
            _OTHER_BINDING_QUEUE_TEXT,
        )

    @staticmethod
    def _prompt_start_result(binding_id: str, result: Any) -> dict[str, Any]:
        return {
            "accepted": bool(result.started),
            "queued": False,
            "started": bool(result.started),
            "binding_id": binding_id,
            "thread_id": str(result.thread_id or ""),
            "turn_id": str(result.turn_id or ""),
            "reason_code": str(result.reason_code or ""),
            "reason": str(result.reason_text or ""),
        }

    @staticmethod
    def _denied(
        binding_id: str,
        thread_id: str,
        *,
        reason_code: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "accepted": False,
            "queued": False,
            "started": False,
            "binding_id": binding_id,
            "thread_id": thread_id,
            "turn_id": "",
            "reason_code": reason_code,
            "reason": reason,
        }

    @staticmethod
    def _queued(
        binding_id: str,
        thread_id: str,
        queue_position: int,
    ) -> dict[str, Any]:
        return {
            "accepted": True,
            "queued": True,
            "started": False,
            "binding_id": binding_id,
            "thread_id": thread_id,
            "turn_id": "",
            "reason_code": "",
            "reason": "",
            "queue_position": int(queue_position),
        }


def remember_message_context(
    bot: Any, message_id: str, context: dict[str, Any]
) -> None:
    """Store recovered message facts through the available Feishu adapter API."""

    remember = getattr(bot, "_remember_message_context", None)
    if callable(remember):
        remember(message_id, context)
        return
    contexts = getattr(bot, "message_contexts", None)
    if isinstance(contexts, dict):
        contexts[message_id] = context


def prepare_queued_prompt_text(bot: Any, **kwargs: Any) -> str | None:
    """Use optional assistant-history recovery without making it mandatory."""

    prepare = getattr(bot, "prepare_queued_prompt_text", None)
    if not callable(prepare):
        return str(kwargs.get("text", "") or "")
    return prepare(**kwargs)
