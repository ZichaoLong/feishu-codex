from __future__ import annotations

import ast
import copy
import pathlib
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from bot.binding_runtime_contract import (
    BindingRuntimeHandle,
    BindingSessionSnapshot,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
    ThreadStateChanged,
)
from bot.stores.chat_binding_store import ChatBindingStore
from tests.runtime_admin_test_support import make_binding_runtime


class BindingRuntimeSessionTests(unittest.TestCase):
    def test_passive_readers_do_not_depend_on_legacy_runtime_view(self) -> None:
        passive_modules = (
            "adapter_event_bridge.py",
            "codex_goal_domain.py",
            "codex_help_domain.py",
            "codex_settings_domain.py",
            "codex_threads_ui_domain.py",
            "file_message_domain.py",
        )
        bot_dir = pathlib.Path(__file__).resolve().parents[1] / "bot"
        forbidden_identifiers = {
            "RuntimeView",
            "build_runtime_view",
            "get_runtime_view",
            "runtime_view",
        }
        violations: list[str] = []

        for module_name in passive_modules:
            path = bot_dir / module_name
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "bot.runtime_view":
                            violations.append(f"{module_name}:{node.lineno}: import")
                elif isinstance(node, ast.ImportFrom):
                    imports_runtime_view = node.module == "bot.runtime_view" or (
                        node.module == "bot"
                        and any(alias.name == "runtime_view" for alias in node.names)
                    )
                    if imports_runtime_view:
                        violations.append(f"{module_name}:{node.lineno}: import")
                elif isinstance(node, ast.Name) and node.id in forbidden_identifiers:
                    violations.append(
                        f"{module_name}:{node.lineno}: identifier {node.id}"
                    )
                elif (
                    isinstance(node, ast.Attribute)
                    and node.attr in forbidden_identifiers
                ):
                    violations.append(
                        f"{module_name}:{node.lineno}: attribute {node.attr}"
                    )

        self.assertEqual(violations, [])

    def _make_manager(
        self,
        *,
        data_dir: pathlib.Path | None = None,
    ) -> BindingRuntimeManager:
        if data_dir is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            data_dir = pathlib.Path(tempdir.name)
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=threading.RLock(),
            chat_binding_store=ChatBindingStore(data_dir),
        )
        return manager

    @staticmethod
    def _legacy_resident_state(
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
    ):
        """Seed state until the typed mutation tranche replaces legacy setup."""

        with manager._lock:
            return manager._get_or_create_runtime_state_locked(binding)

    def _assert_handle_rejected(
        self,
        manager: BindingRuntimeManager,
        handle: BindingRuntimeHandle,
    ) -> None:
        with manager._lock, self.assertRaises(RuntimeError):
            manager.session_snapshot_locked(handle)

    def _bind_thread(
        self,
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
        *,
        thread_id: str,
    ) -> None:
        with manager._lock:
            manager._get_or_create_runtime_state_locked(binding)
            session = manager.resident_session_snapshot_locked(binding)
            assert session is not None
            manager.bind_thread_locked(
                session.handle,
                thread_id=thread_id,
                thread_title=f"Title {thread_id}",
                working_dir=f"/workspace/{thread_id}",
            )

    def test_resolve_session_returns_canonical_snapshot_and_exact_lookup(
        self,
    ) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")

        resolved = manager.resolve_session(*binding)

        self.assertIsInstance(resolved, BindingSessionSnapshot)
        self.assertEqual(resolved.binding, binding)
        self.assertIs(resolved.handle.binding, resolved.binding)
        with manager._lock:
            resident = manager.resident_session_snapshot_locked(binding)
            by_handle = manager.session_snapshot_locked(resolved.handle)
        assert resident is not None
        self.assertIs(resident.handle, resolved.handle)
        self.assertIs(by_handle.handle, resolved.handle)
        self.assertEqual(by_handle, resident)

    def test_resident_snapshot_does_not_hydrate_store_only_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        source = self._make_manager(data_dir=data_dir)
        source.resolve_session(*binding)
        source_state = self._legacy_resident_state(source, binding)
        with source._lock:
            source_state["working_dir"] = "/workspace/persisted"
            source._sync_resident_state_locked(binding, source_state)

        restarted = self._make_manager(data_dir=data_dir)
        with (
            patch.object(
                restarted._chat_binding_store,
                "load",
                side_effect=AssertionError("resident lookup reached durable store"),
            ),
            patch.object(
                restarted._chat_binding_store,
                "load_all",
                side_effect=AssertionError("resident lookup inventoried durable store"),
            ),
            restarted._lock,
        ):
            snapshot = restarted.resident_session_snapshot_locked(binding)
            resident_bindings = restarted.binding_keys_locked()

        self.assertIsNone(snapshot)
        self.assertEqual(resident_bindings, ())
        hydrated = restarted.resolve_session(*binding)
        self.assertEqual(hydrated.working_dir, "/workspace/persisted")

    def test_replace_hydration_retires_old_resident_handle(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        original = manager.resolve_session(*binding)
        state = self._legacy_resident_state(manager, binding)
        with manager._lock:
            state["working_dir"] = "/workspace/replacement"
            manager._sync_resident_state_locked(binding, state)

        manager.hydrate_stored_bindings(replace=True)

        self._assert_handle_rejected(manager, original.handle)
        replacement = manager.resolve_session(*binding)
        self.assertGreater(
            replacement.handle.incarnation,
            original.handle.incarnation,
        )
        self.assertEqual(replacement.working_dir, "/workspace/replacement")

    def test_stale_raw_state_cannot_overwrite_replacement_persistence(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        manager.resolve_session(*binding)
        stale_state = self._legacy_resident_state(manager, binding)
        with manager._lock:
            stale_state["working_dir"] = "/workspace/replacement"
            manager._sync_resident_state_locked(binding, stale_state)

        manager.hydrate_stored_bindings(replace=True)
        replacement = manager.resolve_session(*binding)
        self.assertEqual(replacement.working_dir, "/workspace/replacement")

        stale_state["working_dir"] = "/workspace/stale-sync"
        with manager._lock, self.assertRaisesRegex(RuntimeError, "stale resident state"):
            manager._sync_resident_state_locked(binding, stale_state)
        self.assertEqual(
            manager._chat_binding_store.load(binding)["working_dir"],
            "/workspace/replacement",
        )

        with manager._lock, self.assertRaisesRegex(RuntimeError, "stale resident state"):
            manager._apply_persisted_runtime_state_message_locked(
                binding,
                stale_state,
                ThreadStateChanged(working_dir="/workspace/stale-message"),
            )

        with manager._lock:
            current = manager.resident_session_snapshot_locked(binding)
        assert current is not None
        self.assertEqual(current.working_dir, "/workspace/replacement")
        self.assertEqual(
            manager._chat_binding_store.load(binding)["working_dir"],
            "/workspace/replacement",
        )

    def test_deactivate_recreate_retires_old_handle(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        original = manager.resolve_session(*binding)

        with manager._lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
            self.assertIsNone(
                manager.resident_session_snapshot_locked(binding)
            )

        self._assert_handle_rejected(manager, original.handle)
        recreated = manager.resolve_session(*binding)
        self.assertGreater(
            recreated.handle.incarnation,
            original.handle.incarnation,
        )

    def test_bind_thread_aba_rotates_handle_on_every_owner_revision(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        manager.resolve_session(*binding)

        self._bind_thread(manager, binding, thread_id="thread-a")
        with manager._lock:
            first_a = manager.resident_session_snapshot_locked(binding)
        assert first_a is not None

        self._bind_thread(manager, binding, thread_id="thread-b")
        self._assert_handle_rejected(manager, first_a.handle)
        with manager._lock:
            thread_b = manager.resident_session_snapshot_locked(binding)
        assert thread_b is not None

        self._bind_thread(manager, binding, thread_id="thread-a")
        self._assert_handle_rejected(manager, thread_b.handle)
        self._assert_handle_rejected(manager, first_a.handle)
        with manager._lock:
            second_a = manager.resident_session_snapshot_locked(binding)
        assert second_a is not None
        self.assertEqual(second_a.current_thread_id, "thread-a")
        self.assertGreater(
            second_a.handle.incarnation,
            thread_b.handle.incarnation,
        )

    def test_copied_reconstructed_and_cross_manager_handles_are_rejected(
        self,
    ) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        handle = manager.resolve_session(*binding).handle
        reconstructed = BindingRuntimeHandle(
            _issuer_nonce=handle._issuer_nonce,
            binding=handle.binding,
            incarnation=handle.incarnation,
        )
        other_manager = self._make_manager()
        other_manager.resolve_session(*binding)

        for candidate in (
            copy.copy(handle),
            copy.deepcopy(handle),
            reconstructed,
        ):
            with self.subTest(candidate=candidate):
                self._assert_handle_rejected(manager, candidate)
        self._assert_handle_rejected(other_manager, handle)

    def test_snapshot_is_atomic_and_deeply_immutable(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._legacy_resident_state(manager, binding)
        with manager._lock:
            state["working_dir"] = "/workspace/captured"
            state["configured_settings"] = ["model"]
            state["plan_steps"] = [
                {"step": "capture", "status": "in_progress"}
            ]
            state["execution_transcript"].append_assistant_delta(
                "captured reply"
            )
            state["execution_transcript"].append_process_note(
                "captured process"
            )

        snapshot = manager.resolve_session(*binding)

        with manager._lock:
            state["working_dir"] = "/workspace/mutated"
            state["configured_settings"].append("reasoning_effort")
            state["plan_steps"][0]["status"] = "completed"
            state["plan_steps"].append(
                {"step": "mutate", "status": "in_progress"}
            )
            state["execution_transcript"].reset()
            fresh = manager.session_snapshot_locked(snapshot.handle)

        self.assertEqual(snapshot.working_dir, "/workspace/captured")
        self.assertEqual(snapshot.settings.configured_settings, ("model",))
        self.assertEqual(len(snapshot.plan.steps), 1)
        self.assertEqual(snapshot.plan.steps[0].status, "in_progress")
        self.assertEqual(
            snapshot.execution.transcript.reply_text(),
            "captured reply",
        )
        self.assertEqual(
            snapshot.execution.transcript.process_text(),
            "captured process",
        )
        self.assertEqual(fresh.working_dir, "/workspace/mutated")
        self.assertEqual(
            fresh.settings.configured_settings,
            ("model", "reasoning_effort"),
        )
        self.assertEqual(len(fresh.plan.steps), 2)
        self.assertFalse(fresh.execution.transcript.has_reply_output())
        with self.assertRaises(FrozenInstanceError):
            snapshot.active = False  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            snapshot.plan.steps.append(  # type: ignore[attr-defined]
                ("mutate", "pending")
            )

    def test_snapshot_requires_timer_tickets_for_the_same_binding(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._legacy_resident_state(manager, binding)
        with manager._lock:
            state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
                ticket=ExecutionPatchTimerTicket(
                    binding=binding,
                    thread_id="thread-1",
                    card_message_id="card-1",
                    turn_id="turn-1",
                ),
                timer=Mock(),
            )
            state["mirror_watchdog_registration"] = MirrorWatchdogRegistration(
                ticket=MirrorWatchdogTicket(
                    binding=binding,
                    thread_id="thread-1",
                    card_message_id="card-1",
                    turn_id="turn-1",
                ),
                timer=Mock(),
            )

        snapshot = manager.resolve_session(*binding)

        self.assertEqual(snapshot.handle.binding, binding)
        self.assertTrue(snapshot.execution.patch_timer_registered)
        self.assertTrue(snapshot.execution.mirror_watchdog_registered)

        with manager._lock:
            state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
                ticket=ExecutionPatchTimerTicket(
                    binding=("ou-other", "chat-2"),
                    thread_id="thread-1",
                    card_message_id="card-1",
                    turn_id="turn-1",
                ),
                timer=Mock(),
            )
            with self.assertRaises(TypeError):
                manager.resident_session_snapshot_locked(binding)


if __name__ == "__main__":
    unittest.main()
