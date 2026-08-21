"""Exact generation authority for binding owner-loss transactions.

The binding runtime owns the mutable binding itself.  This smaller owner keeps
only the capability identities which fence an observed binding/thread
generation across re-entrant callbacks and the lock-free detach RPC gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from bot.binding_identity import ChatBindingKey
from bot.binding_runtime_contract import (
    BindingDetachOwnerLossReceipt,
    BindingOwnerLossCommand,
    BindingOwnerLossSettlementReceipt,
    BindingOwnerRevisionReceipt,
    OwnerLossDisposition,
)


@dataclass(slots=True)
class _BindingOwnerGeneration:
    incarnation: int
    owner_revision: int = 0
    receipt: BindingOwnerRevisionReceipt | None = None
    reserved_owner_loss: BindingOwnerLossCommand | None = None


class BindingOwnerAuthority:
    """Issue and validate opaque authority for exact binding revisions.

    All methods are intentionally lock-agnostic.  The owning
    ``BindingRuntimeManager`` serializes them together with its runtime state,
    and supplies the current thread id when issuing or validating a receipt.
    """

    _issuer_ids = count(1)

    def __init__(self) -> None:
        self._issuer_nonce = next(self._issuer_ids)
        self._next_incarnation = 0
        self._generation_by_binding: dict[
            ChatBindingKey, _BindingOwnerGeneration
        ] = {}
        self._next_detach_receipt_nonce = 0
        self._active_detach_receipts: dict[int, BindingDetachOwnerLossReceipt] = {}

    def issue_owner(
        self,
        binding: ChatBindingKey,
        *,
        expected_thread_id: str,
        current_thread_id: str,
    ) -> BindingOwnerRevisionReceipt:
        expected = str(expected_thread_id or "").strip()
        current = str(current_thread_id or "").strip()
        if expected != current:
            raise RuntimeError("binding owner receipt 的 expected thread 已过期。")
        generation = self._ensure_generation(binding)
        receipt = generation.receipt
        if (
            receipt is not None
            and receipt.expected_thread_id == expected
            and receipt.owner_revision == generation.owner_revision
        ):
            return receipt
        receipt = BindingOwnerRevisionReceipt(
            _issuer_nonce=self._issuer_nonce,
            binding=binding,
            incarnation=generation.incarnation,
            owner_revision=generation.owner_revision,
            expected_thread_id=expected,
        )
        generation.receipt = receipt
        return receipt

    def require_owner_current(
        self,
        receipt: BindingOwnerRevisionReceipt,
        *,
        current_thread_id: str,
    ) -> None:
        if not isinstance(receipt, BindingOwnerRevisionReceipt):
            raise RuntimeError("binding owner receipt 缺少 typed identity。")
        if receipt._issuer_nonce != self._issuer_nonce:
            raise RuntimeError("binding owner receipt 属于另一 authority。")
        generation = self._generation_by_binding.get(receipt.binding)
        if (
            generation is None
            or generation.receipt is not receipt
            or generation.incarnation != receipt.incarnation
            or generation.owner_revision != receipt.owner_revision
            or str(current_thread_id or "").strip()
            != receipt.expected_thread_id
        ):
            raise RuntimeError("binding owner receipt 已过期或被替换。")

    def reserve_owner_loss(self, command: BindingOwnerLossCommand) -> None:
        """Pin a generation to one retryable owner-loss transaction."""

        if not isinstance(command, BindingOwnerLossCommand):
            raise RuntimeError("binding owner-loss reservation 缺少 typed command。")
        generation = self._generation_by_binding.get(command.owner.binding)
        if generation is None or generation.receipt is not command.owner:
            raise RuntimeError("binding owner-loss reservation 已过期。")
        reserved = generation.reserved_owner_loss
        if reserved is not None and reserved != command:
            raise RuntimeError(
                "binding owner generation 已有未完成的 owner-loss transaction。"
            )
        if reserved is None:
            generation.reserved_owner_loss = command

    def require_owner_loss_not_pending(
        self,
        receipt: BindingOwnerRevisionReceipt,
    ) -> None:
        generation = self._generation_by_binding.get(receipt.binding)
        if (
            generation is None
            or generation.receipt is not receipt
            or generation.reserved_owner_loss is not None
        ):
            raise RuntimeError(
                "binding owner generation 仍有未完成的 owner-loss transaction。"
            )

    def advance_owner(
        self,
        binding: ChatBindingKey,
        *,
        settled_command: BindingOwnerLossCommand | None = None,
    ) -> None:
        generation = self._ensure_generation(binding)
        self._consume_owner_loss_reservation(
            generation,
            settled_command=settled_command,
        )
        generation.owner_revision += 1
        generation.receipt = None
        self._discard_detach_receipts_for_binding(binding)

    def retire_owner(
        self,
        binding: ChatBindingKey,
        *,
        settled_command: BindingOwnerLossCommand | None = None,
    ) -> None:
        generation = self._generation_by_binding.get(binding)
        if generation is not None:
            self._consume_owner_loss_reservation(
                generation,
                settled_command=settled_command,
            )
            generation.owner_revision += 1
            generation.receipt = None
            self._generation_by_binding.pop(binding, None)
        self._discard_detach_receipts_for_binding(binding)

    def issue_detach(
        self,
        *,
        thread_id: str,
        owners: tuple[BindingOwnerRevisionReceipt, ...],
        settlements: tuple[BindingOwnerLossSettlementReceipt, ...],
    ) -> BindingDetachOwnerLossReceipt:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id or not owners or len(owners) != len(settlements):
            raise RuntimeError("detach owner-loss preflight 缺少完整 exact settlement。")
        for owner in owners:
            self._discard_detach_receipts_for_binding(owner.binding)
        self._next_detach_receipt_nonce += 1
        receipt = BindingDetachOwnerLossReceipt(
            _issuer_nonce=self._issuer_nonce,
            _receipt_nonce=self._next_detach_receipt_nonce,
            thread_id=normalized_thread_id,
            owners=owners,
            settlements=settlements,
        )
        self._active_detach_receipts[receipt._receipt_nonce] = receipt
        return receipt

    def consume_detach(
        self,
        receipt: BindingDetachOwnerLossReceipt,
        *,
        thread_id: str,
        bindings: tuple[ChatBindingKey, ...],
        disposition: OwnerLossDisposition,
    ) -> tuple[BindingOwnerRevisionReceipt, ...]:
        if not isinstance(receipt, BindingDetachOwnerLossReceipt):
            raise RuntimeError("detach owner-loss preflight 缺少 typed receipt。")
        registered = self._active_detach_receipts.get(receipt._receipt_nonce)
        if (
            receipt._issuer_nonce != self._issuer_nonce
            or registered is not receipt
            or receipt.thread_id != str(thread_id or "").strip()
            or tuple(owner.binding for owner in receipt.owners) != bindings
            or len(receipt.owners) != len(receipt.settlements)
        ):
            raise RuntimeError("detach owner-loss preflight receipt 已伪造或过期。")
        for owner, settlement in zip(receipt.owners, receipt.settlements):
            if (
                settlement.command.owner is not owner
                or settlement.command.disposition != disposition
                or settlement.command.reason != "binding_detached"
            ):
                raise RuntimeError("detach owner-loss settlement receipt 不匹配。")
        self._active_detach_receipts.pop(receipt._receipt_nonce, None)
        return receipt.owners

    def discard_detach(self, receipt: BindingDetachOwnerLossReceipt) -> None:
        if not isinstance(receipt, BindingDetachOwnerLossReceipt):
            raise RuntimeError("detach owner-loss preflight 缺少 typed receipt。")
        registered = self._active_detach_receipts.get(receipt._receipt_nonce)
        if registered is receipt and receipt._issuer_nonce == self._issuer_nonce:
            self._active_detach_receipts.pop(receipt._receipt_nonce, None)

    def _ensure_generation(
        self,
        binding: ChatBindingKey,
    ) -> _BindingOwnerGeneration:
        generation = self._generation_by_binding.get(binding)
        if generation is not None:
            return generation
        self._next_incarnation += 1
        generation = _BindingOwnerGeneration(incarnation=self._next_incarnation)
        self._generation_by_binding[binding] = generation
        return generation

    def _discard_detach_receipts_for_binding(
        self,
        binding: ChatBindingKey,
    ) -> None:
        for nonce, receipt in tuple(self._active_detach_receipts.items()):
            if any(owner.binding == binding for owner in receipt.owners):
                self._active_detach_receipts.pop(nonce, None)

    @staticmethod
    def _consume_owner_loss_reservation(
        generation: _BindingOwnerGeneration,
        *,
        settled_command: BindingOwnerLossCommand | None,
    ) -> None:
        reserved = generation.reserved_owner_loss
        if reserved is None:
            if settled_command is not None:
                raise RuntimeError(
                    "binding owner-loss command 没有对应的 active reservation。"
                )
            return
        if settled_command is None or reserved != settled_command:
            raise RuntimeError(
                "binding owner generation 仍有未完成的 owner-loss transaction。"
            )
        generation.reserved_owner_loss = None
