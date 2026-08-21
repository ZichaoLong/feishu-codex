"""Binding-loss regressions for the minimal Feishu turn owner."""

import unittest

from bot.binding_runtime_contract import BindingOwnerLossCommand
from bot.feishu_root_operation_contract import FeishuRootOperationRetentionError
from tests import test_feishu_root_operation_controller as root_operation_tests


class FeishuBindingOwnerLossTests(unittest.TestCase):
    binding = root_operation_tests.FeishuRootOperationControllerTests.binding
    root_thread_id = (
        root_operation_tests.FeishuRootOperationControllerTests.root_thread_id
    )
    _environment = (
        root_operation_tests.FeishuRootOperationControllerTests._environment
    )
    _owner_loss = (
        root_operation_tests.FeishuRootOperationControllerTests._owner_loss
    )
    _admit = root_operation_tests.FeishuRootOperationControllerTests._admit

    def test_same_owner_loss_command_is_idempotent(self) -> None:
        _harness, controller = self._environment()
        self._admit(controller)
        command = self._owner_loss(
            reason="binding_detached",
            disposition="abandon",
        )

        first = controller.settle_owner_loss(command)
        second = controller.settle_owner_loss(command)

        self.assertIs(first.command, command)
        self.assertIs(second.command, command)
        self.assertEqual(first._transaction_nonce, second._transaction_nonce)

    def test_same_binding_revision_rejects_a_different_loss_command(self) -> None:
        _harness, controller = self._environment()
        self._admit(controller)
        command = self._owner_loss(
            reason="binding_detached",
            disposition="abandon",
        )
        controller.settle_owner_loss(command)
        competing = BindingOwnerLossCommand(
            owner=command.owner,
            reason="binding_deleted",
            disposition="terminal",
        )

        with self.assertRaises(FeishuRootOperationRetentionError):
            controller.settle_owner_loss(competing)

    def test_observer_binding_loss_does_not_release_another_holder(self) -> None:
        harness, controller = self._environment()
        token = self._admit(controller)
        controller.acknowledge_continuing(token, turn_id="turn-1")

        controller.settle_owner_loss(
            self._owner_loss(
                binding=("ou_observer", "chat-observer"),
                reason="observer_detached",
                disposition="abandon",
            )
        )

        active = harness.leases.load("root-1")
        self.assertIsNotNone(active)
        self.assertEqual(active.turn_id, "turn-1")


if __name__ == "__main__":
    unittest.main()
