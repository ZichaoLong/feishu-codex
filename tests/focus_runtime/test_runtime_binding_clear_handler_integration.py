"""Production-owner integration tests for runtime binding batch clear."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
)
from tests.focus_runtime import test_codex_handler_startup_runtime as startup_tests


class RuntimeBindingClearHandlerIntegrationTests(unittest.TestCase):
    _make_handler = startup_tests.CodexHandlerStartupRuntimeTests._make_handler

    @staticmethod
    def _seed_binding(handler, binding, thread_id: str):
        summary = ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name=thread_id,
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        handler._adapter.thread_snapshots[(thread_id, None)] = ThreadSnapshot(
            summary=summary
        )

        def seed():
            with handler._lock:
                state = handler._binding_runtime._get_or_create_runtime_state_locked(
                    binding
                )
                session = handler._binding_runtime.resident_session_snapshot_locked(
                    binding
                )
                assert session is not None
                handler._binding_runtime.bind_thread_locked(
                    session.handle,
                    thread_id=thread_id,
                    thread_title=thread_id,
                    working_dir="/tmp/project",
                )
                return state

        return handler._runtime_call(seed)

    def _new_handler(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return self._make_handler(pathlib.Path(tempdir.name))[0]

    def test_clear_all_cancels_every_resident_runtime_timer(self) -> None:
        handler = self._new_handler()
        bindings = (("ou-a", "chat-a"), ("ou-b", "chat-b"))
        states = [
            self._seed_binding(handler, binding, f"thread-{index}")
            for index, binding in enumerate(bindings, start=1)
        ]
        patch_timers = [Mock(), Mock()]
        mirror_timers = [Mock(), Mock()]

        def install_timers() -> None:
            with handler._lock:
                for binding, state, patch_timer, mirror_timer in zip(
                    bindings,
                    states,
                    patch_timers,
                    mirror_timers,
                    strict=True,
                ):
                    state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
                        ticket=ExecutionPatchTimerTicket(
                            binding=binding,
                            thread_id=str(state["current_thread_id"]),
                            card_message_id="card",
                            turn_id="turn",
                        ),
                        timer=patch_timer,
                    )
                    state["mirror_watchdog_registration"] = MirrorWatchdogRegistration(
                        ticket=MirrorWatchdogTicket(
                            binding=binding,
                            thread_id=str(state["current_thread_id"]),
                            card_message_id="card",
                            turn_id="turn",
                        ),
                        timer=mirror_timer,
                    )

        handler._runtime_call(install_timers)
        result = handler._runtime_call(
            handler._runtime_admin.clear_all_bindings_for_control
        )

        self.assertEqual(len(result["cleared_binding_ids"]), 2)
        for state, patch_timer, mirror_timer in zip(
            states,
            patch_timers,
            mirror_timers,
            strict=True,
        ):
            patch_timer.cancel.assert_called_once_with()
            mirror_timer.cancel.assert_called_once_with()
            self.assertIsNone(state["patch_timer_registration"])
            self.assertIsNone(state["mirror_watchdog_registration"])

    def test_replacement_and_store_ghost_are_not_invalidated_or_finalized(self) -> None:
        handler = self._new_handler()
        replacement_binding = ("ou-replacement", "chat-replacement")
        ghost_binding = ("ou-ghost", "chat-ghost")
        self._seed_binding(handler, replacement_binding, "thread-old")
        self._seed_binding(handler, ghost_binding, "thread-ghost")
        replacement_state = handler._binding_runtime.build_default_runtime_state()

        def simulate_unconfirmed_removals(_bindings, **_kwargs):
            handler._binding_runtime._runtime_state_by_binding[
                replacement_binding
            ] = replacement_state
            handler._binding_runtime._runtime_state_by_binding.pop(ghost_binding)
            return ()

        finalizer_calls: list[tuple[str, ...]] = []
        service = handler._runtime_admin._binding_application._binding_clear
        service._ports = replace(
            service._ports,
            finalize_deactivated_thread_runtime=(
                lambda thread_ids, **_kwargs: finalizer_calls.append(
                    tuple(thread_ids)
                )
            ),
        )

        with (
            patch.object(
                handler._binding_runtime,
                "deactivate_bindings_with_receipts_locked",
                side_effect=simulate_unconfirmed_removals,
            ),
            patch.object(
                handler._binding_batch_deactivation,
                "_invalidate_execution_queue_locked",
            ) as invalidate,
        ):
            result = handler._runtime_call(
                handler._runtime_admin.clear_all_bindings_for_control
            )

        self.assertEqual(result["cleared_binding_ids"], [])
        invalidate.assert_not_called()
        self.assertEqual(finalizer_calls, [])
        with handler._lock:
            self.assertIs(
                handler._binding_runtime._runtime_state_by_binding[
                    replacement_binding
                ],
                replacement_state,
            )
            self.assertTrue(
                handler._binding_runtime.binding_exists_locked(ghost_binding)
            )

    def test_single_proof_failure_invalidates_removed_queue_before_error(self) -> None:
        handler = self._new_handler()
        binding = ("ou-user", "chat-1")
        self._seed_binding(handler, binding, "thread-1")

        def deactivate():
            with handler._lock:
                return handler._binding_batch_deactivation.deactivate_locked(
                    (binding,)
                )

        with (
            patch.object(
                handler._binding_runtime,
                "binding_record_inventory_locked",
                side_effect=OSError("inventory unavailable"),
            ),
            patch.object(
                handler._binding_batch_deactivation,
                "_invalidate_execution_queue_locked",
            ) as invalidate,
            self.assertRaisesRegex(RuntimeError, "inventory unavailable"),
        ):
            handler._runtime_call(deactivate)

        invalidate.assert_called_once_with(binding)


if __name__ == "__main__":
    unittest.main()
