"""Contract tests for runtime binding-clear queue cleanup."""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.runtime_admin.harness import (
    RuntimeAdminControllerHarnessMixin,
)


class RuntimeBindingClearServiceTests(
    RuntimeAdminControllerHarnessMixin,
    unittest.TestCase,
):

    @staticmethod
    def _observe_global_invalidation(controller) -> list[bool]:
        calls: list[bool] = []
        service = controller._binding_application._binding_clear
        service._ports = replace(
            service._ports,
            invalidate_all_execution_queues_locked=(
                lambda: calls.append(True) or 0
            ),
        )
        return calls

    def test_empty_inventory_still_invalidates_queue_only_keys(self) -> None:
        _lock, _runtime, controller, *_rest = self._make_controller()
        invalidations = self._observe_global_invalidation(controller)

        result = controller.clear_all_bindings_for_control()

        self.assertTrue(result["already_empty"])
        self.assertEqual(invalidations, [True])

    def test_clear_one_explicitly_invalidates_confirmed_removal_once(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            *_rest,
        ) = self._make_controller()
        binding = ("ou_user", "chat-a")
        self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-a",
        )
        summaries["thread-a"] = self._direct_root_summary("thread-a")

        result = controller._binding_application.clear_binding_for_control(binding)

        self.assertTrue(result["cleared"])
        with lock:
            self.assertFalse(binding_runtime.binding_exists_locked(binding))
        self.assertEqual(controller._invalidated_queues, [binding])

    def test_clear_one_does_not_invalidate_failed_removal(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            *_rest,
        ) = self._make_controller()
        binding = ("ou_user", "chat-a")
        self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-a",
        )
        summaries["thread-a"] = self._direct_root_summary("thread-a")

        with patch.object(
            binding_runtime,
            "deactivate_bindings_with_receipts_locked",
            side_effect=RuntimeError("owner removal failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "owner removal failed"):
                controller._binding_application.clear_binding_for_control(binding)

        with lock:
            self.assertTrue(binding_runtime.binding_exists_locked(binding))
        self.assertEqual(controller._invalidated_queues, [])

    def test_successful_batch_invalidates_inventory_and_orphan_queues(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            *_rest,
        ) = self._make_controller()
        binding = ("ou_user", "chat-a")
        self._bind_thread(
            lock,
            binding_runtime,
            binding,
            thread_id="thread-a",
        )
        summaries["thread-a"] = self._direct_root_summary("thread-a")
        invalidations = self._observe_global_invalidation(controller)

        result = controller.clear_all_bindings_for_control()

        self.assertFalse(result["already_empty"])
        self.assertEqual(
            result["cleared_binding_ids"],
            ["p2p:ou_user:chat-a"],
        )
        self.assertEqual(controller._invalidated_queues, [binding])
        self.assertEqual(invalidations, [True])

    def test_batch_preflight_failure_invalidates_no_local_bindings(self) -> None:
        (
            lock,
            binding_runtime,
            controller,
            summaries,
            _loaded,
            unsubscribed,
            _archived,
            released_runtime_leases,
            *_rest,
        ) = self._make_controller()
        binding_a = ("ou_a", "chat-a")
        binding_b = ("ou_b", "chat-b")
        self._bind_thread(lock, binding_runtime, binding_a, thread_id="thread-a")
        self._bind_thread(lock, binding_runtime, binding_b, thread_id="thread-b")
        with lock:
            binding_runtime.acquire_interaction_lease_for_binding(
                binding_a,
                "thread-a",
            )
            binding_runtime.acquire_interaction_lease_for_binding(
                binding_b,
                "thread-b",
            )
        summaries["thread-a"] = self._direct_root_summary("thread-a")
        summaries["thread-b"] = self._direct_root_summary("thread-b")
        original_release = binding_runtime._interaction_lease_store.release_if_matches
        global_invalidations = self._observe_global_invalidation(controller)

        def fail_second_release(expected):
            if expected.thread_id == "thread-b":
                raise OSError("lease cleanup failed")
            return original_release(expected)

        with patch.object(
            binding_runtime._interaction_lease_store,
            "release_if_matches",
            side_effect=fail_second_release,
        ):
            with self.assertRaisesRegex(RuntimeError, "lease cleanup failed"):
                controller.clear_all_bindings_for_control()

        with lock:
            self.assertIsNotNone(
                binding_runtime.binding_runtime_snapshot_locked(binding_a)
            )
            self.assertIsNotNone(
                binding_runtime.binding_runtime_snapshot_locked(binding_b)
            )
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_a))
        self.assertIsNotNone(binding_runtime._chat_binding_store.load(binding_b))
        self.assertEqual(unsubscribed, [])
        self.assertEqual(released_runtime_leases, [])
        self.assertEqual(controller._invalidated_queues, [])
        self.assertEqual(global_invalidations, [])


if __name__ == "__main__":
    unittest.main()
