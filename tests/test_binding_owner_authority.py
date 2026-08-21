import copy
import unittest

from bot.binding_owner_authority import BindingOwnerAuthority
from bot.binding_runtime_contract import (
    BindingDetachOwnerLossReceipt,
    BindingOwnerLossCommand,
    BindingOwnerLossSettlementReceipt,
    BindingOwnerRevisionReceipt,
)


def _owner(
    authority: BindingOwnerAuthority,
    binding: tuple[str, str],
    thread_id: str,
) -> BindingOwnerRevisionReceipt:
    return authority.issue_owner(
        binding,
        expected_thread_id=thread_id,
        current_thread_id=thread_id,
    )


def _command(
    owner: BindingOwnerRevisionReceipt,
    *,
    reason: str = "binding_detached",
    disposition: str = "abandon",
) -> BindingOwnerLossCommand:
    return BindingOwnerLossCommand(
        owner=owner,
        reason=reason,
        disposition=disposition,
    )


def _settlement(
    command: BindingOwnerLossCommand,
    *,
    nonce: int = 1,
) -> BindingOwnerLossSettlementReceipt:
    return BindingOwnerLossSettlementReceipt(
        command=command,
        _settler_nonce=1,
        _transaction_nonce=nonce,
    )


def _detach(
    authority: BindingOwnerAuthority,
    owners: tuple[BindingOwnerRevisionReceipt, ...],
    *,
    disposition: str = "abandon",
) -> BindingDetachOwnerLossReceipt:
    commands = tuple(
        _command(owner, disposition=disposition) for owner in owners
    )
    return authority.issue_detach(
        thread_id=owners[0].expected_thread_id,
        owners=owners,
        settlements=tuple(
            _settlement(command, nonce=index)
            for index, command in enumerate(commands, start=1)
        ),
    )


class BindingOwnerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = BindingOwnerAuthority()
        self.binding = ("ou-user", "chat-1")

    def assert_owner_rejected(
        self,
        receipt: BindingOwnerRevisionReceipt,
        *,
        thread_id: str = "thread-1",
    ) -> None:
        with self.assertRaises(RuntimeError):
            self.authority.require_owner_current(
                receipt,
                current_thread_id=thread_id,
            )

    def test_repeated_issue_for_same_generation_returns_same_receipt(self) -> None:
        first = _owner(self.authority, self.binding, "thread-1")
        second = _owner(self.authority, self.binding, "thread-1")

        self.assertIs(second, first)
        self.authority.require_owner_current(
            first,
            current_thread_id="thread-1",
        )

    def test_owner_receipt_requires_exact_object_from_same_authority(self) -> None:
        receipt = _owner(self.authority, self.binding, "thread-1")
        reconstructed = BindingOwnerRevisionReceipt(
            _issuer_nonce=receipt._issuer_nonce,
            binding=receipt.binding,
            incarnation=receipt.incarnation,
            owner_revision=receipt.owner_revision,
            expected_thread_id=receipt.expected_thread_id,
        )
        foreign = _owner(
            BindingOwnerAuthority(),
            self.binding,
            "thread-1",
        )

        for candidate in (
            copy.copy(receipt),
            copy.deepcopy(receipt),
            reconstructed,
            foreign,
        ):
            with self.subTest(candidate=candidate):
                self.assert_owner_rejected(candidate)

    def test_owner_changes_and_thread_aba_invalidate_old_receipts(self) -> None:
        advanced = _owner(self.authority, self.binding, "thread-1")
        self.authority.advance_owner(self.binding)
        self.assert_owner_rejected(advanced)

        retired = _owner(self.authority, self.binding, "thread-1")
        retired_incarnation = retired.incarnation
        self.authority.retire_owner(self.binding)
        self.assert_owner_rejected(retired)
        recreated = _owner(self.authority, self.binding, "thread-1")
        self.assertGreater(recreated.incarnation, retired_incarnation)

        owner_a = recreated
        owner_b = _owner(self.authority, self.binding, "thread-2")
        owner_a_again = _owner(self.authority, self.binding, "thread-1")
        self.assert_owner_rejected(owner_a)
        self.assert_owner_rejected(owner_b, thread_id="thread-2")
        self.authority.require_owner_current(
            owner_a_again,
            current_thread_id="thread-1",
        )

    def test_owner_loss_reservation_is_exact_retryable_and_must_settle(self) -> None:
        owner = _owner(self.authority, self.binding, "thread-1")
        command = _command(owner)
        self.authority.reserve_owner_loss(command)
        self.authority.reserve_owner_loss(command)

        with self.assertRaises(RuntimeError):
            self.authority.require_owner_loss_not_pending(owner)
        with self.assertRaises(RuntimeError):
            self.authority.advance_owner(self.binding)
        with self.assertRaises(RuntimeError):
            self.authority.retire_owner(self.binding)

        for conflicting in (
            _command(owner, reason="thread_archived"),
            _command(owner, disposition="terminal"),
        ):
            with self.subTest(conflicting=conflicting):
                with self.assertRaises(RuntimeError):
                    self.authority.reserve_owner_loss(conflicting)

        self.authority.advance_owner(
            self.binding,
            settled_command=command,
        )
        self.assert_owner_rejected(owner)

    def test_matching_settlement_can_retire_reserved_generation(self) -> None:
        owner = _owner(self.authority, self.binding, "thread-1")
        command = _command(owner, disposition="terminal")
        self.authority.reserve_owner_loss(command)

        self.authority.retire_owner(
            self.binding,
            settled_command=command,
        )

        self.assert_owner_rejected(owner)
        replacement = _owner(self.authority, self.binding, "thread-1")
        self.assertNotEqual(replacement.incarnation, owner.incarnation)

    def test_detach_receipt_requires_exact_object_from_same_authority(self) -> None:
        owner = _owner(self.authority, self.binding, "thread-1")

        def assert_rejected(candidate_factory) -> None:
            receipt = _detach(self.authority, (owner,))
            candidate = candidate_factory(receipt)
            with self.assertRaises(RuntimeError):
                self.authority.consume_detach(
                    candidate,
                    thread_id="thread-1",
                    bindings=(self.binding,),
                    disposition="abandon",
                )
            self.assertEqual(
                self.authority.consume_detach(
                    receipt,
                    thread_id="thread-1",
                    bindings=(self.binding,),
                    disposition="abandon",
                ),
                (owner,),
            )

        assert_rejected(copy.copy)
        assert_rejected(copy.deepcopy)
        assert_rejected(
            lambda receipt: BindingDetachOwnerLossReceipt(
                _issuer_nonce=receipt._issuer_nonce,
                _receipt_nonce=receipt._receipt_nonce,
                thread_id=receipt.thread_id,
                owners=receipt.owners,
                settlements=receipt.settlements,
            )
        )

        foreign_authority = BindingOwnerAuthority()
        foreign_owner = _owner(foreign_authority, self.binding, "thread-1")
        foreign = _detach(foreign_authority, (foreign_owner,))
        local = _detach(self.authority, (owner,))
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                foreign,
                thread_id="thread-1",
                bindings=(self.binding,),
                disposition="abandon",
            )
        self.assertEqual(
            self.authority.consume_detach(
                local,
                thread_id="thread-1",
                bindings=(self.binding,),
                disposition="abandon",
            ),
            (owner,),
        )

    def test_detach_receipt_fences_arguments_and_is_single_use(self) -> None:
        second_binding = ("ou-other", "chat-2")
        owners = (
            _owner(self.authority, self.binding, "thread-1"),
            _owner(self.authority, second_binding, "thread-1"),
        )

        receipt = _detach(self.authority, owners)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                receipt,
                thread_id="thread-wrong",
                bindings=(self.binding, second_binding),
                disposition="abandon",
            )

        receipt = _detach(self.authority, owners)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                receipt,
                thread_id="thread-1",
                bindings=(second_binding, self.binding),
                disposition="abandon",
            )

        receipt = _detach(self.authority, owners)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                receipt,
                thread_id="thread-1",
                bindings=(self.binding, second_binding),
                disposition="terminal",
            )

        receipt = _detach(self.authority, owners)
        consumed = self.authority.consume_detach(
            receipt,
            thread_id="thread-1",
            bindings=(self.binding, second_binding),
            disposition="abandon",
        )
        self.assertEqual(consumed, owners)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                receipt,
                thread_id="thread-1",
                bindings=(self.binding, second_binding),
                disposition="abandon",
            )

    def test_detach_receipt_is_invalidated_by_discard_or_owner_advance(self) -> None:
        owner = _owner(self.authority, self.binding, "thread-1")
        discarded = _detach(self.authority, (owner,))
        self.authority.discard_detach(discarded)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                discarded,
                thread_id="thread-1",
                bindings=(self.binding,),
                disposition="abandon",
            )

        second_binding = ("ou-other", "chat-2")
        second_owner = _owner(self.authority, second_binding, "thread-1")
        advanced = _detach(self.authority, (owner, second_owner))
        self.authority.advance_owner(second_binding)
        with self.assertRaises(RuntimeError):
            self.authority.consume_detach(
                advanced,
                thread_id="thread-1",
                bindings=(self.binding, second_binding),
                disposition="abandon",
            )


if __name__ == "__main__":
    unittest.main()
