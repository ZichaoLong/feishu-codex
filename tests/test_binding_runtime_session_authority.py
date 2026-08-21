from __future__ import annotations

import copy
import unittest

from bot.binding_runtime_contract import BindingRuntimeHandle
from bot.binding_runtime_session_authority import BindingRuntimeSessionAuthority


class BindingRuntimeSessionAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.authority = BindingRuntimeSessionAuthority()
        self.binding = ("ou-user", "chat-1")
        self.state: object = {"mutable": "owner-only"}

    def assert_rejected(
        self,
        handle: BindingRuntimeHandle,
        *,
        binding: tuple[str, str] | None = None,
        resident_state: object | None = None,
    ) -> None:
        with self.assertRaises(RuntimeError):
            self.authority.require(
                handle,
                binding=binding or self.binding,
                resident_state=(
                    self.state if resident_state is None else resident_state
                ),
            )

    def test_install_is_idempotent_for_one_exact_resident_object(self) -> None:
        handle = self.authority.install(
            self.binding,
            resident_state=self.state,
        )

        self.assertIs(
            self.authority.install(
                self.binding,
                resident_state=self.state,
            ),
            handle,
        )
        self.assertIs(
            self.authority.current(
                self.binding,
                resident_state=self.state,
            ),
            handle,
        )
        self.authority.require(
            handle,
            binding=self.binding,
            resident_state=self.state,
        )
        self.assertFalse(hasattr(handle, "resident_state"))

    def test_copied_reconstructed_and_cross_authority_handles_are_rejected(self) -> None:
        handle = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        reconstructed = BindingRuntimeHandle(
            _issuer_nonce=handle._issuer_nonce,
            binding=handle.binding,
            incarnation=handle.incarnation,
        )
        foreign = BindingRuntimeSessionAuthority().install(
            self.binding,
            resident_state=self.state,
        )

        for candidate in (
            copy.copy(handle),
            copy.deepcopy(handle),
            reconstructed,
            foreign,
        ):
            with self.subTest(candidate=candidate):
                self.assert_rejected(candidate)

    def test_replacement_rejects_old_handle_and_value_equal_state(self) -> None:
        first = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        equal_replacement: object = {"mutable": "owner-only"}

        self.assertIsNone(
            self.authority.current(
                self.binding,
                resident_state=equal_replacement,
            )
        )
        second = self.authority.install(
            self.binding,
            resident_state=equal_replacement,
        )

        self.assertGreater(second.incarnation, first.incarnation)
        self.assert_rejected(first)
        self.assert_rejected(second, resident_state=self.state)
        self.authority.require(
            second,
            binding=self.binding,
            resident_state=equal_replacement,
        )

    def test_retire_requires_handle_and_object_from_same_incarnation(self) -> None:
        old_state: object = {"generation": "old"}
        old = self.authority.install(
            self.binding,
            resident_state=old_state,
        )
        replacement: object = {"generation": "replacement"}
        current = self.authority.install(
            self.binding,
            resident_state=replacement,
        )

        with self.assertRaises(RuntimeError):
            self.authority.retire(
                old,
                binding=self.binding,
                resident_state=old_state,
            )
        self.authority.require(
            current,
            binding=self.binding,
            resident_state=replacement,
        )

        self.authority.retire(
            current,
            binding=self.binding,
            resident_state=replacement,
        )
        self.assertIsNone(
            self.authority.current(
                self.binding,
                resident_state=replacement,
            )
        )
        self.assert_rejected(current, resident_state=replacement)

    def test_deactivate_recreate_and_state_object_aba_issue_new_handles(self) -> None:
        first_a = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        self.authority.retire(
            first_a,
            binding=self.binding,
            resident_state=self.state,
        )
        recreated_a = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        self.assertGreater(recreated_a.incarnation, first_a.incarnation)
        self.assert_rejected(first_a)

        state_b: object = {"generation": "B"}
        handle_b = self.authority.install(
            self.binding,
            resident_state=state_b,
        )
        second_a = self.authority.install(
            self.binding,
            resident_state=self.state,
        )

        self.assertGreater(second_a.incarnation, handle_b.incarnation)
        self.assert_rejected(recreated_a)
        self.assert_rejected(handle_b, resident_state=state_b)
        self.authority.require(
            second_a,
            binding=self.binding,
            resident_state=self.state,
        )

    def test_in_place_binding_revision_aba_rotates_the_handle(self) -> None:
        first_a = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        handle_b = self.authority.advance(
            first_a,
            binding=self.binding,
            resident_state=self.state,
        )
        second_a = self.authority.advance(
            handle_b,
            binding=self.binding,
            resident_state=self.state,
        )

        self.assertGreater(handle_b.incarnation, first_a.incarnation)
        self.assertGreater(second_a.incarnation, handle_b.incarnation)
        self.assert_rejected(first_a)
        self.assert_rejected(handle_b)
        self.authority.require(
            second_a,
            binding=self.binding,
            resident_state=self.state,
        )

    def test_one_resident_object_cannot_be_shared_across_bindings(self) -> None:
        self.authority.install(
            self.binding,
            resident_state=self.state,
        )

        with self.assertRaises(RuntimeError):
            self.authority.install(
                ("ou-other", "chat-2"),
                resident_state=self.state,
            )

    def test_retire_all_invalidates_every_handle_without_reusing_incarnation(self) -> None:
        first = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        other_binding = ("ou-other", "chat-2")
        other_state: object = {"generation": "other"}
        other = self.authority.install(
            other_binding,
            resident_state=other_state,
        )

        self.authority.retire_all()

        self.assert_rejected(first)
        self.assert_rejected(
            other,
            binding=other_binding,
            resident_state=other_state,
        )
        recreated = self.authority.install(
            self.binding,
            resident_state=self.state,
        )
        self.assertGreater(recreated.incarnation, other.incarnation)

    def test_none_is_not_a_resident_runtime_identity(self) -> None:
        with self.assertRaises(TypeError):
            self.authority.install(
                self.binding,
                resident_state=None,
            )


if __name__ == "__main__":
    unittest.main()
