"""RuntimeLoop-owned FIFO transaction for Feishu binding continuations.

The queue is intentionally narrower than a general scheduler.  Every item is
stamped with both the binding generation and the exact root that admitted it.
It can therefore preserve one Feishu writer across adjacent turns without
letting an old item follow a binding through a detach/rebind lifecycle.

This owner never starts upstream work itself.  It emits typed effects and an
opaque receipt; the handler executes the effect through the normal prompt or
compact entry point and commits the exact receipt afterwards.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import count
from typing import Any, Literal, Mapping, TypeAlias

from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.runtime_loop import RuntimeContextGuard


ChatBindingKey: TypeAlias = tuple[str, str]
FeishuQueueDrainOutcome = Literal[
    "started",
    "known_no_effect_settled",
    "blocked",
    "dropped",
]


class FeishuExecutionQueueError(RuntimeError):
    """Base error for an invalid queue command or transaction."""


class FeishuExecutionQueueAdmissionError(FeishuExecutionQueueError):
    """A queue item lacks a same-binding active root admission."""


class FeishuExecutionQueueReceiptError(FeishuExecutionQueueError):
    """A drain receipt is forged, replayed, stale, or from another owner."""


@dataclass(frozen=True, slots=True)
class FeishuBindingExecutionSnapshot:
    """Minimal binding fact consumed by the queue owner.

    ``BindingRuntimeManager`` remains the binding/runtime source of truth.  A
    caller projects only the fields needed for one queue decision instead of
    exposing its mutable ``RuntimeStateDict``.
    """

    binding: ChatBindingKey
    root_thread_id: str
    active: bool
    attached: bool
    has_inflight_execution: bool
    current_turn_id: str


@dataclass(frozen=True, slots=True)
class FeishuQueuedMessageOrigin:
    """Frozen Feishu message facts needed when a prompt eventually starts."""

    chat_type: str = ""
    sender_open_id: str = ""
    sender_user_id: str = ""
    sender_type: str = ""
    feishu_thread_id: str = ""
    assistant_context_mode: str = ""
    assistant_context_created_at: int = 0
    assistant_context_seq: int = 0
    assistant_context_sender_name: str = ""

    @classmethod
    def from_message_context(
        cls,
        context: Mapping[str, Any] | None,
    ) -> FeishuQueuedMessageOrigin:
        source = context or {}
        return cls(
            chat_type=_text(source.get("chat_type")),
            sender_open_id=_text(source.get("sender_open_id")),
            sender_user_id=_text(source.get("sender_user_id")),
            sender_type=_text(source.get("sender_type")),
            feishu_thread_id=_text(source.get("thread_id")),
            assistant_context_mode=_text(source.get("assistant_context_mode")),
            assistant_context_created_at=_non_negative_int(source.get("created_at")),
            assistant_context_seq=_non_negative_int(
                source.get("assistant_context_seq")
            ),
            assistant_context_sender_name=_text(source.get("sender_name")),
        )

    def message_context(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "chat_type": self.chat_type,
            "sender_open_id": self.sender_open_id,
            "sender_user_id": self.sender_user_id,
            "sender_type": self.sender_type,
            "thread_id": self.feishu_thread_id,
            "assistant_context_mode": self.assistant_context_mode,
            "created_at": self.assistant_context_created_at,
            "assistant_context_seq": self.assistant_context_seq,
            "sender_name": self.assistant_context_sender_name,
        }
        return {key: value for key, value in values.items() if value}


@dataclass(frozen=True, slots=True)
class FeishuQueueDrainReceipt:
    """Opaque capability for completing one exact claimed queue head."""

    _issuer_nonce: int
    _token_nonce: int


@dataclass(frozen=True, slots=True)
class StartPromptEffect:
    """One prompt head that may be executed through normal writer admission."""

    receipt: FeishuQueueDrainReceipt
    binding: ChatBindingKey
    admitted_root_thread_id: str
    sender_id: str
    chat_id: str
    message_id: str
    text: str
    actor_open_id: str
    origin: FeishuQueuedMessageOrigin
    input_items: tuple[dict[str, Any], ...]
    synthetic_source: str
    display_mode: str
    surface_failures: bool


@dataclass(frozen=True, slots=True)
class StartCompactEffect:
    """One compact head that may be executed through normal writer admission."""

    receipt: FeishuQueueDrainReceipt
    binding: ChatBindingKey
    admitted_root_thread_id: str
    sender_id: str
    chat_id: str
    message_id: str
    actor_open_id: str
    origin: FeishuQueuedMessageOrigin


FeishuQueueStartEffect: TypeAlias = StartPromptEffect | StartCompactEffect


@dataclass(frozen=True, slots=True)
class FeishuQueueAdmission:
    binding: ChatBindingKey
    root_thread_id: str
    binding_epoch: int
    queue_position: int


@dataclass(frozen=True, slots=True)
class FeishuQueueInvalidation:
    binding: ChatBindingKey
    invalidated_root_thread_id: str
    binding_epoch: int
    removed_count: int
    cancelled_active_drain: bool

    @property
    def had_continuation(self) -> bool:
        return bool(self.removed_count or self.cancelled_active_drain)


@dataclass(frozen=True, slots=True)
class FeishuQueueRecallOutcome:
    """Queue cancellation facts that may require terminal reconciliation."""

    removed_count: int
    terminal_reconcile_root_thread_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeishuQueueDrainCompletion:
    binding: ChatBindingKey
    root_thread_id: str
    outcome: FeishuQueueDrainOutcome
    cancelled_by_invalidation: bool
    continue_same_epoch: bool
    terminal_reconcile_root_thread_id: str = ""


@dataclass(frozen=True, slots=True)
class FeishuExecutionQueueSnapshot:
    binding: ChatBindingKey
    binding_epoch: int
    root_thread_id: str
    pending_count: int
    draining: bool
    draining_cancelled: bool
    pending_message_ids: tuple[str, ...]

    @property
    def has_pending_or_draining(self) -> bool:
        return bool(
            self.pending_count or (self.draining and not self.draining_cancelled)
        )


@dataclass(frozen=True, slots=True)
class _QueuedItem:
    item_nonce: int
    kind: Literal["prompt", "compact"]
    binding: ChatBindingKey
    binding_epoch: int
    admitted_root_thread_id: str
    sender_id: str
    chat_id: str
    message_id: str
    actor_open_id: str
    origin: FeishuQueuedMessageOrigin
    text: str = ""
    input_items: tuple[dict[str, Any], ...] = ()
    synthetic_source: str = ""
    display_mode: str = "silent"
    surface_failures: bool = True


@dataclass(slots=True)
class _ActiveDrain:
    receipt: FeishuQueueDrainReceipt
    item: _QueuedItem
    cancel_reason: Literal["", "binding_invalidation", "recall"] = ""


class FeishuExecutionQueueController:
    """Own the process-local Feishu binding FIFO and its drain transaction."""

    _issuer_ids = count(1)

    def __init__(self, *, runtime_context_guard: RuntimeContextGuard) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Feishu execution queue 缺少 RuntimeLoop context guard。")
        self._runtime_context_guard = runtime_context_guard
        self._issuer_nonce = next(self._issuer_ids)
        self._next_item_nonce = 0
        self._next_receipt_nonce = 0
        self._binding_epoch: dict[ChatBindingKey, int] = {}
        self._binding_root: dict[ChatBindingKey, str] = {}
        self._items: dict[ChatBindingKey, deque[_QueuedItem]] = defaultdict(deque)
        self._active_by_binding: dict[ChatBindingKey, _ActiveDrain] = {}
        self._active_by_receipt_nonce: dict[int, _ActiveDrain] = {}

    def enqueue_prompt(
        self,
        snapshot: FeishuBindingExecutionSnapshot,
        *,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
        text: str,
        actor_open_id: str = "",
        origin: FeishuQueuedMessageOrigin | None = None,
        input_items: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        synthetic_source: str = "",
        display_mode: str = "silent",
        surface_failures: bool = True,
        preprojection_local_turn_id: str = "",
    ) -> FeishuQueueAdmission:
        """Append behind exact execution, FIFO continuity, or a local turn.

        ``preprojection_local_turn_id`` is the exact current-process Web/fcodex
        lease turn observed under the shared lock before its Feishu mirror.
        """

        self._runtime_context_guard()
        binding, root_thread_id, epoch = self._admit_enqueue_snapshot(
            snapshot,
            sender_id=sender_id,
            preprojection_local_turn_id=preprojection_local_turn_id,
        )
        normalized_display_mode = _text(display_mode).lower() or "silent"
        if normalized_display_mode not in {"silent", "announce"}:
            raise FeishuExecutionQueueAdmissionError(
                "未知的 Feishu queued prompt display mode。"
            )
        item = self._new_item(
            kind="prompt",
            binding=binding,
            binding_epoch=epoch,
            admitted_root_thread_id=root_thread_id,
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            actor_open_id=actor_open_id,
            origin=origin,
            text=text,
            input_items=input_items,
            synthetic_source=synthetic_source,
            display_mode=normalized_display_mode,
            surface_failures=surface_failures,
        )
        return self._append(item)

    def enqueue_compact(
        self,
        snapshot: FeishuBindingExecutionSnapshot,
        *,
        sender_id: str,
        chat_id: str,
        message_id: str = "",
        actor_open_id: str = "",
        origin: FeishuQueuedMessageOrigin | None = None,
    ) -> FeishuQueueAdmission:
        self._runtime_context_guard()
        binding, root_thread_id, epoch = self._admit_enqueue_snapshot(
            snapshot,
            sender_id=sender_id,
        )
        item = self._new_item(
            kind="compact",
            binding=binding,
            binding_epoch=epoch,
            admitted_root_thread_id=root_thread_id,
            sender_id=sender_id,
            chat_id=chat_id,
            message_id=message_id,
            actor_open_id=actor_open_id,
            origin=origin,
        )
        return self._append(item)

    def begin_terminal_drain(
        self,
        binding: ChatBindingKey,
        snapshot: FeishuBindingExecutionSnapshot | None,
    ) -> FeishuQueueStartEffect | None:
        self._runtime_context_guard()
        return self._begin_drain(binding, snapshot)

    def receipt_may_execute(
        self,
        receipt: FeishuQueueDrainReceipt,
        snapshot: FeishuBindingExecutionSnapshot | None,
    ) -> bool:
        """Validate a claimed head while binding execution is still idle."""

        return self._claimed_receipt_matches(
            receipt,
            snapshot,
            require_idle=True,
        )

    def claimed_receipt_may_mutate(
        self,
        receipt: FeishuQueueDrainReceipt,
        snapshot: FeishuBindingExecutionSnapshot | None,
    ) -> bool:
        """Validate the exact capability at the upstream mutation boundary.

        Local prompt/compact preparation deliberately primes an execution
        anchor before the RPC.  Therefore this guard verifies receipt identity,
        cancellation, binding/root/epoch/head, and active attachment without
        requiring the runtime to remain locally idle.
        """

        return self._claimed_receipt_matches(
            receipt,
            snapshot,
            require_idle=False,
        )

    def _claimed_receipt_matches(
        self,
        receipt: FeishuQueueDrainReceipt,
        snapshot: FeishuBindingExecutionSnapshot | None,
        *,
        require_idle: bool,
    ) -> bool:
        self._runtime_context_guard()
        active = self._require_receipt(receipt)
        if active.cancel_reason or snapshot is None:
            return False
        item = active.item
        if _binding(snapshot.binding) != item.binding:
            return False
        return bool(
            snapshot.active
            and snapshot.attached
            and (not require_idle or not snapshot.has_inflight_execution)
            and _text(snapshot.root_thread_id) == item.admitted_root_thread_id
            and self._binding_epoch.get(item.binding, 0) == item.binding_epoch
            and self._binding_root.get(item.binding, "") == item.admitted_root_thread_id
            and self._head_is_item(item)
        )

    def complete_drain(
        self,
        receipt: FeishuQueueDrainReceipt,
        *,
        outcome: FeishuQueueDrainOutcome,
    ) -> FeishuQueueDrainCompletion:
        self._runtime_context_guard()
        if outcome not in {
            "started",
            "known_no_effect_settled",
            "blocked",
            "dropped",
        }:
            raise ValueError("未知的 Feishu queue drain outcome。")
        active = self._require_receipt(receipt)
        item = active.item
        self._active_by_receipt_nonce.pop(receipt._token_nonce, None)
        if self._active_by_binding.get(item.binding) is active:
            self._active_by_binding.pop(item.binding, None)

        cancelled_by_invalidation = bool(
            active.cancel_reason == "binding_invalidation"
            or self._binding_epoch.get(item.binding, 0) != item.binding_epoch
            or self._binding_root.get(item.binding, "") != item.admitted_root_thread_id
        )
        cancelled_by_recall = active.cancel_reason == "recall"
        if not cancelled_by_invalidation and not self._head_is_item(item):
            raise FeishuExecutionQueueReceiptError(
                "Feishu queue receipt 的 exact head 在结算前消失。"
            )
        # The exact claimed head is consumed exactly once. Recall remains an
        # authoritative cancellation if it raced with the effect.
        if not cancelled_by_invalidation:
            queue = self._items.get(item.binding)
            if queue is None or not queue or queue[0] is not item:
                raise FeishuExecutionQueueReceiptError(
                    "Feishu queue receipt 不再对应 exact head。"
                )
            queue.popleft()
            if not queue:
                self._items.pop(item.binding, None)

        same_epoch = bool(
            not cancelled_by_invalidation
            and self._binding_epoch.get(item.binding, 0) == item.binding_epoch
            and self._binding_root.get(item.binding, "") == item.admitted_root_thread_id
        )
        pending = self._has_current_pending(item.binding) if same_epoch else False
        blocks_continuation = bool(outcome == "blocked" and not cancelled_by_recall)
        continue_same_epoch = bool(
            outcome != "started"
            and not blocks_continuation
            and same_epoch
            and pending
        )
        terminal_reconcile_root = ""
        if (
            outcome != "started"
            and not blocks_continuation
            and same_epoch
            and not pending
        ):
            terminal_reconcile_root = item.admitted_root_thread_id
        return FeishuQueueDrainCompletion(
            binding=item.binding,
            root_thread_id=item.admitted_root_thread_id,
            outcome=outcome,
            cancelled_by_invalidation=cancelled_by_invalidation,
            continue_same_epoch=continue_same_epoch,
            terminal_reconcile_root_thread_id=terminal_reconcile_root,
        )

    def invalidate_binding(
        self,
        binding: ChatBindingKey,
    ) -> FeishuQueueInvalidation:
        """End one binding generation and cancel its queued/draining work."""

        self._runtime_context_guard()
        return self._invalidate_binding(_binding(binding), replacement_root="")

    def invalidate_all(self) -> int:
        """Invalidate every queue-owned key, including orphan-only keys."""

        self._runtime_context_guard()
        bindings = set(self._binding_epoch) | set(self._binding_root) | set(self._items)
        bindings.update(self._active_by_binding)
        removed = 0
        for binding in tuple(bindings):
            result = self._invalidate_binding(binding, replacement_root="")
            removed += result.removed_count
        return removed

    def remove_recalled_message(
        self,
        *,
        chat_id: str,
        message_id: str,
    ) -> FeishuQueueRecallOutcome:
        self._runtime_context_guard()
        normalized_chat_id = _text(chat_id)
        normalized_message_id = _text(message_id)
        if not normalized_message_id:
            return FeishuQueueRecallOutcome(0, ())
        removed = 0
        terminal_reconcile_roots: list[str] = []
        for binding, queue in tuple(self._items.items()):
            if normalized_chat_id and binding[1] != normalized_chat_id:
                continue
            active = self._active_by_binding.get(binding)
            active_item = None
            active_already_recalled = False
            if active is not None and active.item.message_id == normalized_message_id:
                active_item = active.item
                active_already_recalled = active.cancel_reason == "recall"
                active.cancel_reason = "recall"
            binding_removed = 0
            removed_current_generation = False
            kept: deque[_QueuedItem] = deque()
            current_epoch = self._binding_epoch.get(binding, 0)
            current_root = self._binding_root.get(binding, "")
            for item in queue:
                if item.message_id != normalized_message_id:
                    kept.append(item)
                    continue
                if item is active_item:
                    # A claimed head is settled only by its exact receipt.
                    kept.append(item)
                    if active_already_recalled:
                        continue
                binding_removed += 1
                removed_current_generation = bool(
                    removed_current_generation
                    or (
                        item.binding_epoch == current_epoch
                        and item.admitted_root_thread_id == current_root
                    )
                )
            removed += binding_removed
            if kept:
                self._items[binding] = kept
            else:
                self._items.pop(binding, None)
                if (
                    binding_removed
                    and removed_current_generation
                    and current_root
                    and current_root not in terminal_reconcile_roots
                ):
                    terminal_reconcile_roots.append(current_root)
        return FeishuQueueRecallOutcome(
            removed_count=removed,
            terminal_reconcile_root_thread_ids=tuple(terminal_reconcile_roots),
        )

    def continuity_pending(
        self,
        snapshot: FeishuBindingExecutionSnapshot | None,
        expected_root_thread_id: str,
    ) -> bool:
        """Whether an exact binding/root generation still bridges a writer."""

        self._runtime_context_guard()
        if snapshot is None:
            return False
        normalized_binding = _binding(snapshot.binding)
        root_id = _text(expected_root_thread_id)
        if (
            not snapshot.active
            or not snapshot.attached
            or _text(snapshot.root_thread_id) != root_id
        ):
            return False
        if not root_id or self._binding_root.get(normalized_binding, "") != root_id:
            return False
        return self._has_current_pending(normalized_binding) or self._has_current_drain(
            normalized_binding
        )

    def has_other_binding_continuity(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> bool:
        """Whether another binding already owns FIFO continuity for this root."""

        self._runtime_context_guard()
        return self._has_other_binding_continuity(
            _binding(binding),
            _text(root_thread_id),
        )

    def snapshot(self, binding: ChatBindingKey) -> FeishuExecutionQueueSnapshot:
        self._runtime_context_guard()
        normalized_binding = _binding(binding)
        epoch = self._binding_epoch.get(normalized_binding, 0)
        root_thread_id = self._binding_root.get(normalized_binding, "")
        queue = self._items.get(normalized_binding, ())
        active = self._active_by_binding.get(normalized_binding)
        return FeishuExecutionQueueSnapshot(
            binding=normalized_binding,
            binding_epoch=epoch,
            root_thread_id=root_thread_id,
            pending_count=len(queue),
            draining=active is not None,
            draining_cancelled=bool(active is not None and active.cancel_reason),
            pending_message_ids=tuple(item.message_id for item in queue),
        )

    def inventory(self) -> tuple[FeishuExecutionQueueSnapshot, ...]:
        self._runtime_context_guard()
        bindings = set(self._binding_epoch) | set(self._binding_root) | set(self._items)
        bindings.update(self._active_by_binding)
        return tuple(self.snapshot(binding) for binding in sorted(bindings))

    def _admit_enqueue_snapshot(
        self,
        snapshot: FeishuBindingExecutionSnapshot,
        *,
        sender_id: str,
        preprojection_local_turn_id: str = "",
    ) -> tuple[ChatBindingKey, str, int]:
        binding = _binding(snapshot.binding)
        root_thread_id = _text(snapshot.root_thread_id)
        normalized_sender = _text(sender_id)
        if not snapshot.active or not snapshot.attached:
            raise FeishuExecutionQueueAdmissionError(
                "Feishu binding 未 active/attached，不能加入 continuation FIFO。"
            )
        if not root_thread_id:
            raise FeishuExecutionQueueAdmissionError(
                "只有已有 exact root execution 的 binding 才能加入 FIFO。"
            )
        if self._has_other_binding_continuity(binding, root_thread_id):
            raise FeishuExecutionQueueAdmissionError(
                "同一 root 已由另一飞书 binding 保留 FIFO continuity。"
            )
        local_turn_id = _text(preprojection_local_turn_id)
        if local_turn_id and (
            snapshot.has_inflight_execution or _text(snapshot.current_turn_id)
        ):
            raise FeishuExecutionQueueAdmissionError(
                "pre-projection turn proof 只能在飞书 mirror 尚未建立时使用。"
            )
        has_exact_continuity = self.continuity_pending(snapshot, root_thread_id)
        if not (
            snapshot.has_inflight_execution
            or has_exact_continuity
            or local_turn_id
        ):
            raise FeishuExecutionQueueAdmissionError(
                "只有 active turn、exact FIFO continuity 或 exact pre-projection "
                "turn proof 才能加入 FIFO。"
            )
        if (
            binding[0] != GROUP_SHARED_BINDING_OWNER_ID
            and binding[0] != normalized_sender
        ):
            raise FeishuExecutionQueueAdmissionError(
                "queued sender 不属于当前 p2p binding。"
            )
        epoch = self._synchronize_root(binding, root_thread_id)
        return binding, root_thread_id, epoch

    def _new_item(
        self,
        *,
        kind: Literal["prompt", "compact"],
        binding: ChatBindingKey,
        binding_epoch: int,
        admitted_root_thread_id: str,
        sender_id: str,
        chat_id: str,
        message_id: str,
        actor_open_id: str,
        origin: FeishuQueuedMessageOrigin | None,
        text: str = "",
        input_items: tuple[dict[str, Any], ...] | list[dict[str, Any]] = (),
        synthetic_source: str = "",
        display_mode: str = "silent",
        surface_failures: bool = True,
    ) -> _QueuedItem:
        self._next_item_nonce += 1
        return _QueuedItem(
            item_nonce=self._next_item_nonce,
            kind=kind,
            binding=binding,
            binding_epoch=binding_epoch,
            admitted_root_thread_id=admitted_root_thread_id,
            sender_id=_text(sender_id),
            chat_id=_text(chat_id),
            message_id=_text(message_id),
            actor_open_id=_text(actor_open_id),
            origin=origin or FeishuQueuedMessageOrigin(),
            text=str(text or "").strip(),
            input_items=tuple(dict(input_item) for input_item in input_items),
            synthetic_source=_text(synthetic_source),
            display_mode=display_mode,
            surface_failures=bool(surface_failures),
        )

    def _append(self, item: _QueuedItem) -> FeishuQueueAdmission:
        queue = self._items[item.binding]
        queue.append(item)
        return FeishuQueueAdmission(
            binding=item.binding,
            root_thread_id=item.admitted_root_thread_id,
            binding_epoch=item.binding_epoch,
            queue_position=len(queue),
        )

    def _begin_drain(
        self,
        binding: ChatBindingKey,
        snapshot: FeishuBindingExecutionSnapshot | None,
    ) -> FeishuQueueStartEffect | None:
        normalized_binding = _binding(binding)
        if snapshot is None:
            self._invalidate_binding(normalized_binding, replacement_root="")
            return None
        if _binding(snapshot.binding) != normalized_binding:
            raise ValueError("binding execution snapshot 与 drain binding 不匹配。")
        root_thread_id = _text(snapshot.root_thread_id)
        if not root_thread_id or not snapshot.active or not snapshot.attached:
            self._invalidate_binding(normalized_binding, replacement_root="")
            return None
        if self._binding_root.get(normalized_binding, root_thread_id) != root_thread_id:
            self._invalidate_binding(
                normalized_binding,
                replacement_root=root_thread_id,
            )
            return None
        self._binding_root.setdefault(normalized_binding, root_thread_id)
        self._binding_epoch.setdefault(normalized_binding, 1)
        if normalized_binding in self._active_by_binding:
            return None
        if snapshot.has_inflight_execution:
            return None
        self._drop_stale_heads(normalized_binding)
        queue = self._items.get(normalized_binding)
        if not queue:
            return None
        item = queue[0]
        self._next_receipt_nonce += 1
        receipt = FeishuQueueDrainReceipt(
            self._issuer_nonce,
            self._next_receipt_nonce,
        )
        active = _ActiveDrain(receipt=receipt, item=item)
        self._active_by_binding[normalized_binding] = active
        self._active_by_receipt_nonce[receipt._token_nonce] = active
        return self._effect(active)

    def _effect(self, active: _ActiveDrain) -> FeishuQueueStartEffect:
        item = active.item
        if item.kind == "prompt":
            return StartPromptEffect(
                receipt=active.receipt,
                binding=item.binding,
                admitted_root_thread_id=item.admitted_root_thread_id,
                sender_id=item.sender_id,
                chat_id=item.chat_id,
                message_id=item.message_id,
                text=item.text,
                actor_open_id=item.actor_open_id,
                origin=item.origin,
                input_items=tuple(dict(value) for value in item.input_items),
                synthetic_source=item.synthetic_source,
                display_mode=item.display_mode,
                surface_failures=item.surface_failures,
            )
        return StartCompactEffect(
            receipt=active.receipt,
            binding=item.binding,
            admitted_root_thread_id=item.admitted_root_thread_id,
            sender_id=item.sender_id,
            chat_id=item.chat_id,
            message_id=item.message_id,
            actor_open_id=item.actor_open_id,
            origin=item.origin,
        )

    def _synchronize_root(self, binding: ChatBindingKey, root_thread_id: str) -> int:
        current_root = self._binding_root.get(binding)
        if current_root is None:
            self._binding_root[binding] = root_thread_id
            self._binding_epoch.setdefault(binding, 1)
        elif current_root != root_thread_id:
            self._invalidate_binding(binding, replacement_root=root_thread_id)
        return self._binding_epoch[binding]

    def _invalidate_binding(
        self,
        binding: ChatBindingKey,
        *,
        replacement_root: str,
    ) -> FeishuQueueInvalidation:
        previous_root = self._binding_root.get(binding, "")
        queue = self._items.pop(binding, None)
        removed = len(queue or ())
        active = self._active_by_binding.get(binding)
        cancelled_active = bool(
            active is not None and active.cancel_reason != "binding_invalidation"
        )
        if active is not None:
            active.cancel_reason = "binding_invalidation"
        next_epoch = self._binding_epoch.get(binding, 0) + 1
        self._binding_epoch[binding] = next_epoch
        normalized_replacement = _text(replacement_root)
        if normalized_replacement:
            self._binding_root[binding] = normalized_replacement
        else:
            self._binding_root.pop(binding, None)
        return FeishuQueueInvalidation(
            binding=binding,
            invalidated_root_thread_id=previous_root,
            binding_epoch=next_epoch,
            removed_count=removed,
            cancelled_active_drain=cancelled_active,
        )

    def _require_receipt(
        self,
        receipt: FeishuQueueDrainReceipt,
    ) -> _ActiveDrain:
        if (
            not isinstance(receipt, FeishuQueueDrainReceipt)
            or receipt._issuer_nonce != self._issuer_nonce
        ):
            raise FeishuExecutionQueueReceiptError(
                "Feishu queue receipt 属于另一 owner 或类型无效。"
            )
        active = self._active_by_receipt_nonce.get(receipt._token_nonce)
        if active is None or active.receipt is not receipt:
            raise FeishuExecutionQueueReceiptError(
                "Feishu queue receipt 已结算、伪造或不是 exact receipt。"
            )
        return active

    def _head_is_item(self, item: _QueuedItem) -> bool:
        queue = self._items.get(item.binding)
        return bool(queue and queue[0] is item)

    def _drop_stale_heads(self, binding: ChatBindingKey) -> int:
        queue = self._items.get(binding)
        if not queue:
            return 0
        epoch = self._binding_epoch.get(binding, 0)
        root_thread_id = self._binding_root.get(binding, "")
        dropped = 0
        while queue and (
            queue[0].binding_epoch != epoch
            or queue[0].admitted_root_thread_id != root_thread_id
        ):
            queue.popleft()
            dropped += 1
        if not queue:
            self._items.pop(binding, None)
        return dropped

    def _has_current_pending(self, binding: ChatBindingKey) -> bool:
        self._drop_stale_heads(binding)
        return bool(self._items.get(binding))

    def _has_current_drain(self, binding: ChatBindingKey) -> bool:
        active = self._active_by_binding.get(binding)
        if active is None or active.cancel_reason:
            return False
        item = active.item
        return bool(
            self._binding_epoch.get(binding, 0) == item.binding_epoch
            and self._binding_root.get(binding, "") == item.admitted_root_thread_id
            and self._head_is_item(item)
        )

    def _has_other_binding_continuity(
        self,
        binding: ChatBindingKey,
        root_thread_id: str,
    ) -> bool:
        if not root_thread_id:
            return False
        candidates = set(self._binding_root) | set(self._items)
        candidates.update(self._active_by_binding)
        for candidate in candidates:
            if candidate == binding:
                continue
            if self._binding_root.get(candidate, "") != root_thread_id:
                continue
            if self._has_current_pending(candidate) or self._has_current_drain(
                candidate
            ):
                return True
        return False


def _binding(value: ChatBindingKey) -> ChatBindingKey:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("Feishu queue binding 必须是二元 tuple。")
    owner_id = _text(value[0])
    chat_id = _text(value[1])
    if not owner_id or not chat_id:
        raise ValueError("Feishu queue binding 缺少 owner/chat identity。")
    return owner_id, chat_id


def _text(value: object) -> str:
    return str(value or "").strip()


def _non_negative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
