from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from bot.backend_reset.interaction_coordinator import (
    BackendResetInteractionReceipt,
)
from bot.backend_reset.contract import BackendResetLocalProjectionReceipt
from bot.backend_reset.service import BackendResetService, BackendResetServicePorts
from bot.binding_execution_runtime import InterruptedBindingExecution
from bot.binding_identity import format_binding_id
from bot.binding_runtime_contract import BindingRuntimeHandle
from bot.binding_runtime_lifecycle import RuntimeTimerCancellationEffect


Binding = tuple[str, str]


@dataclass(frozen=True)
class _Preview:
    status: str = "available"
    reason_text: str = ""


@dataclass(frozen=True)
class _ExecutionSnapshot:
    has_execution_anchor: bool


@dataclass(frozen=True)
class _SessionSnapshot:
    handle: BindingRuntimeHandle
    execution: _ExecutionSnapshot
    current_thread_id: str

    @property
    def binding(self) -> Binding:
        return self.handle.binding


@dataclass(frozen=True)
class _DetachResult:
    detached_binding_ids: list[str]
    timer_cancellations: tuple[RuntimeTimerCancellationEffect, ...]


class _BindingRuntime:
    def __init__(
        self,
        events: list[str],
        states: dict[Binding, dict[str, Any]],
    ) -> None:
        self.events = events
        self.states = states
        self.fail_inventory = False
        self.fail_detach: set[str] = set()
        self.detached_ids: dict[str, list[str]] = {}
        self.cancelled_timers: list[str] = []
        self.after_inventory: Callable[[], None] | None = None
        self._next_incarnation = 0
        self._handles: dict[Binding, BindingRuntimeHandle] = {}
        for binding in states:
            self._handles[binding] = self._new_handle(binding)

    def _new_handle(self, binding: Binding) -> BindingRuntimeHandle:
        self._next_incarnation += 1
        return BindingRuntimeHandle(
            _issuer_nonce=1,
            binding=binding,
            incarnation=self._next_incarnation,
        )

    def _snapshot(self, binding: Binding) -> _SessionSnapshot:
        state = self.states[binding]
        return _SessionSnapshot(
            handle=self._handles[binding],
            execution=_ExecutionSnapshot(bool(state.get("active"))),
            current_thread_id=str(state.get("thread_id", "")),
        )

    def binding_session_inventory_locked(self) -> tuple[_SessionSnapshot, ...]:
        self.events.append("bindings.inventory")
        if self.fail_inventory:
            raise RuntimeError("inventory failed")
        snapshots = tuple(self._snapshot(binding) for binding in sorted(self.states))
        if self.after_inventory is not None:
            self.after_inventory()
        return snapshots

    def resident_session_snapshot_locked(
        self,
        binding: Binding,
    ) -> _SessionSnapshot | None:
        if binding not in self.states:
            return None
        return self._snapshot(binding)

    def session_snapshot_locked(
        self,
        handle: BindingRuntimeHandle,
    ) -> _SessionSnapshot:
        current = self._handles.get(handle.binding)
        if current is not handle:
            raise RuntimeError("binding runtime handle is stale or replaced")
        return self._snapshot(handle.binding)

    def resident_runtime_state_locked(
        self,
        binding: Binding,
    ) -> dict[str, Any] | None:
        return self.states.get(binding)

    def replace_session(
        self,
        binding: Binding,
        state: dict[str, Any],
    ) -> None:
        self.states[binding] = state
        self._handles[binding] = self._new_handle(binding)

    def detach_thread_bindings_locked(
        self,
        thread_id: str,
        *,
        detach_availability,
    ) -> _DetachResult:
        self.events.append(f"bindings.detach:{thread_id}")
        if self.fail_detach and thread_id in self.fail_detach:
            raise RuntimeError("detach failed")
        self.assert_detach_available(detach_availability, thread_id)
        timer_cancellations: list[RuntimeTimerCancellationEffect] = []
        for binding, state in self.states.items():
            if state.get("thread_id") == thread_id:
                state["feishu_runtime_state"] = "detached"
                timer_cancellations.append(
                    RuntimeTimerCancellationEffect(
                        binding=binding,
                        _timers=(
                            SimpleNamespace(
                                cancel=lambda sender_id=str(
                                    state.get("sender_id", "")
                                ): self.cancelled_timers.append(sender_id)
                            ),
                        ),
                    )
                )
        return _DetachResult(
            list(self.detached_ids.get(thread_id, [])),
            tuple(timer_cancellations),
        )

    @staticmethod
    def assert_detach_available(detach_availability, thread_id: str) -> None:
        if detach_availability(thread_id) != (True, ""):
            raise AssertionError("backend reset must force the local detach projection")


class _TurnExecution:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fail_note_for: set[str] = set()
        self.fail_state_for: set[str] = set()

    @staticmethod
    def has_active_execution_locked(state: dict[str, Any]) -> bool:
        return bool(state.get("active"))

    def append_process_note_locked(
        self,
        state: dict[str, Any],
        *,
        text: str,
        marks_work: bool = False,
    ) -> None:
        sender_id = str(state.get("sender_id", ""))
        self.events.append(f"turn.note:{sender_id}")
        if sender_id in self.fail_note_for:
            raise RuntimeError("note failed")
        state["note"] = text
        state["marks_work"] = marks_work

    def apply_runtime_state_message_locked(
        self,
        state: dict[str, Any],
        message: object,
    ) -> None:
        sender_id = str(state.get("sender_id", ""))
        self.events.append(f"turn.state:{sender_id}")
        if sender_id in self.fail_state_for:
            raise RuntimeError("state transition failed")
        state["cancelled"] = getattr(message, "cancelled", None)
        state["pending_cancel"] = getattr(message, "pending_cancel", None)
        state["runtime_channel_state"] = getattr(
            message,
            "runtime_channel_state",
            None,
        )


class _ExecutionRuntime:
    def __init__(
        self,
        bindings: _BindingRuntime,
        turns: _TurnExecution,
    ) -> None:
        self.bindings = bindings
        self.turns = turns

    def interrupt_for_backend_reset(self, command):
        session = self.bindings.session_snapshot_locked(command.session.handle)
        state = self.bindings.resident_runtime_state_locked(session.binding)
        if state is None:
            raise RuntimeError("binding runtime is no longer resident")
        if not self.turns.has_active_execution_locked(state):
            return None
        self.turns.append_process_note_locked(
            state,
            text=command.process_note,
            marks_work=True,
        )
        self.turns.apply_runtime_state_message_locked(
            state,
            SimpleNamespace(
                cancelled=True,
                pending_cancel=False,
                runtime_channel_state="live",
            ),
        )
        return InterruptedBindingExecution(
            binding_id=format_binding_id(session.binding),
            session=self.bindings.session_snapshot_locked(session.handle),
        )


class _EpochCoordinator:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.fence_calls = 0
        self.replace_calls = 0
        self.fail_before_local_projection = False
        self.start_calls = 0
        self.cleared_thread_ids = ("thread-purged-b", "thread-purged-a")

    def fence_ingress(
        self,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        self.fence_calls += 1
        self.events.append(f"epoch.fence:{expected_connection_generation}")

    def replace_owned_backend(self, *, retire_local_projection_after_stop):
        self.replace_calls += 1
        self.events.append("epoch.replace")
        if self.fail_before_local_projection:
            raise RuntimeError("owned backend stop failed")
        local_projection = retire_local_projection_after_stop()
        if not isinstance(local_projection, BackendResetLocalProjectionReceipt):
            raise AssertionError("invalid local projection receipt")
        self.events.append("epoch.start")
        self.start_calls += 1
        return SimpleNamespace(
            machine_cleared_thread_ids=self.cleared_thread_ids,
            retirement=SimpleNamespace(local_projection=local_projection),
        )


class _Harness:
    def __init__(
        self,
        *,
        preview: _Preview | None = None,
        states: dict[Binding, dict[str, Any]] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.preview = preview or _Preview()
        self.states: dict[Binding, dict[str, Any]] = states if states is not None else {
            ("user-a", "chat-a"): {
                "sender_id": "user-a",
                "thread_id": "thread-b",
                "active": True,
                "feishu_runtime_state": "attached",
            },
            ("user-b", "chat-b"): {
                "sender_id": "user-b",
                "thread_id": "thread-a",
                "active": False,
                "feishu_runtime_state": "attached",
            },
        }
        self.bindings = _BindingRuntime(self.events, self.states)
        self.bindings.detached_ids = {
            "thread-a": ["p2p:user-b:chat-b"],
            "thread-b": ["p2p:user-a:chat-a", "p2p:user-a:chat-a"],
        }
        self.turns = _TurnExecution(self.events)
        self.execution_runtime = _ExecutionRuntime(self.bindings, self.turns)
        self.epoch = _EpochCoordinator(self.events)
        self.fail_close_error: BaseException | None = None
        self.guard_error: BaseException | None = None
        self.finalized: list[Binding] = []
        self.finalize_error: BaseException | None = None
        self.cancelled_timers = self.bindings.cancelled_timers
        self.queue_clear_calls = 0
        self.queue_clear_error: BaseException | None = None
        self.service = BackendResetService(
            lock=threading.RLock(),
            binding_runtime=self.bindings,
            execution_runtime=self.execution_runtime,
            epoch_coordinator=self.epoch,
            ports=BackendResetServicePorts(
                backend_reset_preview=self._preview,
                invalidate_all_feishu_execution_queues_locked=self._clear_all_queues,
                finalize_execution=self._finalize,
                interaction_preparation=SimpleNamespace(
                    prepare_all=self._prepare_interactions,
                ),
                published_app_server_url=self._published_url,
                runtime_context_guard=self._guard,
            ),
        )

    def _guard(self) -> None:
        self.events.append("guard")
        if self.guard_error is not None:
            raise self.guard_error

    def _preview(self) -> _Preview:
        self.events.append("preview")
        return self.preview

    def _finalize(self, session: _SessionSnapshot):
        binding = session.binding
        self.events.append(f"turn.finalize:{binding[0]}")
        self.finalized.append(binding)
        self.states[binding]["active"] = False
        return SimpleNamespace(
            retired=True,
            presentation_error=(
                str(self.finalize_error) if self.finalize_error is not None else ""
            ),
        )

    def _clear_all_queues(self) -> int:
        self.events.append("queue.clear_all")
        if self.queue_clear_error is not None:
            raise self.queue_clear_error
        self.queue_clear_calls += 1
        return 2

    def _prepare_interactions(self) -> BackendResetInteractionReceipt:
        self.events.append("interactions.prepare_all")
        if self.fail_close_error is not None:
            raise self.fail_close_error
        return BackendResetInteractionReceipt(pending_request_count=3)

    def _published_url(self) -> str:
        self.events.append("endpoint.read")
        return "ws://127.0.0.1:9876"


class BackendResetServiceTest(unittest.TestCase):
    def test_force_must_be_an_exact_bool_before_runtime_access(self) -> None:
        for force in (None, 0, 1, "", "false", [], {}):
            with self.subTest(force=force):
                harness = _Harness()

                with self.assertRaisesRegex(TypeError, "exact bool"):
                    harness.service.reset_current_instance(force=force)  # type: ignore[arg-type]

                self.assertEqual(harness.events, [])
                self.assertEqual(harness.epoch.fence_calls, 0)
                self.assertEqual(harness.epoch.replace_calls, 0)

    def test_success_projects_local_state_after_confirmed_stop_before_start(self) -> None:
        harness = _Harness()

        result = harness.service.reset_current_instance(force=False)

        self.assertEqual(
            result,
            {
                "force": False,
                "detached_binding_ids": [
                    "p2p:user-a:chat-a",
                    "p2p:user-b:chat-b",
                ],
                "interrupted_binding_ids": ["p2p:user-a:chat-a"],
                "retired_request_count": 3,
                "purged_thread_ids": ["thread-purged-b", "thread-purged-a"],
                "projection_warnings": [],
                "app_server_url": "ws://127.0.0.1:9876",
            },
        )
        self.assertEqual(harness.finalized, [("user-a", "chat-a")])
        self.assertIn("[中断] 管理员已重置当前实例 backend", harness.states[("user-a", "chat-a")]["note"])
        self.assertTrue(harness.states[("user-a", "chat-a")]["cancelled"])
        self.assertFalse(harness.states[("user-a", "chat-a")]["pending_cancel"])
        self.assertEqual(
            harness.states[("user-a", "chat-a")]["runtime_channel_state"],
            "live",
        )
        self.assertEqual(
            harness.cancelled_timers,
            ["user-b", "user-a"],
        )
        self.assertEqual(harness.queue_clear_calls, 1)
        self.assertEqual(
            {
                state["feishu_runtime_state"]
                for state in harness.states.values()
            },
            {"detached"},
        )
        self.assertEqual(
            harness.events[:3],
            ["guard", "preview", "epoch.fence:None"],
        )
        self.assertLess(
            harness.events.index("epoch.replace"),
            harness.events.index("turn.note:user-a"),
        )
        self.assertLess(
            harness.events.index("bindings.detach:thread-a"),
            harness.events.index("epoch.start"),
        )
        self.assertEqual(harness.events[-1], "endpoint.read")

    def test_same_chat_group_and_p2p_interrupts_only_exact_active_binding(
        self,
    ) -> None:
        group_binding = ("__group__", "chat-shared")
        p2p_binding = ("p2p-user", "chat-shared")
        group_state: dict[str, Any] = {
            "sender_id": "__group__",
            "thread_id": "",
            "active": False,
            "marker": "must-not-change",
        }
        p2p_state: dict[str, Any] = {
            "sender_id": "p2p-user",
            "thread_id": "",
            "active": True,
        }
        group_before = dict(group_state)
        harness = _Harness(
            states={
                group_binding: group_state,
                p2p_binding: p2p_state,
            }
        )

        result = harness.service.reset_current_instance(force=True)

        self.assertEqual(
            result["interrupted_binding_ids"],
            ["p2p:p2p-user:chat-shared"],
        )
        self.assertEqual(harness.finalized, [p2p_binding])
        self.assertEqual(group_state, group_before)
        self.assertNotIn("note", group_state)
        self.assertNotIn("cancelled", group_state)
        self.assertIn("[中断]", p2p_state["note"])
        self.assertTrue(p2p_state["cancelled"])

    def test_session_replacement_after_inventory_fails_closed(self) -> None:
        harness = _Harness()
        binding = ("user-a", "chat-a")
        captured_state = harness.states[binding]
        replacement_state: dict[str, Any] = {
            "sender_id": "replacement-user",
            "thread_id": "thread-b",
            "active": True,
        }
        harness.bindings.after_inventory = lambda: harness.bindings.replace_session(
            binding,
            replacement_state,
        )

        with self.assertRaisesRegex(RuntimeError, "stale or replaced"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.fence_calls, 1)
        self.assertEqual(harness.epoch.replace_calls, 1)
        self.assertEqual(harness.epoch.start_calls, 0)
        self.assertNotIn("note", captured_state)
        self.assertNotIn("cancelled", captured_state)
        self.assertNotIn("note", replacement_state)
        self.assertNotIn("cancelled", replacement_state)
        self.assertEqual(harness.finalized, [])

    def test_policy_refusal_happens_before_fence_or_projection(self) -> None:
        for preview, force in (
            (_Preview("blocked", "hard blocker"), True),
            (_Preview("force-only", "force required"), False),
        ):
            with self.subTest(status=preview.status):
                harness = _Harness(preview=preview)

                with self.assertRaisesRegex(ValueError, preview.reason_text):
                    harness.service.reset_current_instance(force=force)

                self.assertEqual(harness.events, ["guard", "preview"])
                self.assertEqual(harness.epoch.fence_calls, 0)
                self.assertEqual(harness.epoch.replace_calls, 0)

    def test_expected_generation_is_validated_before_runtime_access(self) -> None:
        for expected in (True, False, 0, -1, 1.0, "1", [], {}):
            with self.subTest(expected=expected):
                harness = _Harness()
                with self.assertRaises((TypeError, ValueError)):
                    harness.service.reset_current_instance(
                        force=False,
                        expected_connection_generation=expected,  # type: ignore[arg-type]
                    )
                self.assertEqual(harness.events, [])
                self.assertEqual(harness.epoch.fence_calls, 0)

    def test_expected_generation_is_forwarded_only_after_policy_recheck(self) -> None:
        harness = _Harness()

        harness.service.reset_current_instance(
            force=False,
            expected_connection_generation=7,
        )

        self.assertEqual(
            harness.events[:3],
            ["guard", "preview", "epoch.fence:7"],
        )

    def test_runtime_guard_fails_before_policy_or_state_access(self) -> None:
        harness = _Harness()
        harness.guard_error = RuntimeError("wrong RuntimeLoop")

        with self.assertRaisesRegex(RuntimeError, "wrong RuntimeLoop"):
            harness.service.reset_current_instance(force=False)

        self.assertEqual(harness.events, ["guard"])
        self.assertEqual(harness.epoch.fence_calls, 0)

    def test_binding_inventory_failure_keeps_fenced_backend_unreplaced(self) -> None:
        harness = _Harness()
        harness.bindings.fail_inventory = True

        with self.assertRaisesRegex(RuntimeError, "inventory failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.fence_calls, 1)
        self.assertEqual(harness.epoch.replace_calls, 0)

    def test_execution_state_failure_keeps_fenced_backend_unreplaced(self) -> None:
        harness = _Harness()
        harness.turns.fail_state_for.add("user-a")

        with self.assertRaisesRegex(RuntimeError, "state transition failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.replace_calls, 1)
        self.assertEqual(harness.epoch.start_calls, 0)

    def test_process_note_failure_keeps_fenced_backend_unreplaced(self) -> None:
        harness = _Harness()
        harness.turns.fail_note_for.add("user-a")

        with self.assertRaisesRegex(RuntimeError, "note failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.replace_calls, 1)
        self.assertEqual(harness.epoch.start_calls, 0)

    def test_card_finalize_failure_is_warning_after_local_transition(self) -> None:
        harness = _Harness()
        harness.finalize_error = RuntimeError("card unavailable")

        result = harness.service.reset_current_instance(force=True)

        self.assertEqual(
            result["projection_warnings"],
            ["finalize p2p:user-a:chat-a: card unavailable"],
        )
        self.assertEqual(harness.epoch.replace_calls, 1)

    def test_detach_failure_keeps_fenced_backend_unreplaced_until_retry(self) -> None:
        harness = _Harness()
        harness.bindings.fail_detach.add("thread-a")

        with self.assertRaisesRegex(RuntimeError, "detach failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.replace_calls, 1)
        self.assertEqual(harness.epoch.start_calls, 0)
        harness.bindings.fail_detach.clear()
        harness.service.reset_current_instance(force=True)
        self.assertEqual(harness.epoch.replace_calls, 2)
        self.assertEqual(harness.epoch.start_calls, 1)
        self.assertEqual(harness.finalized, [("user-a", "chat-a")])
        self.assertEqual(harness.events.count("turn.note:user-a"), 1)

    def test_stop_failure_changes_no_binding_or_execution_projection(self) -> None:
        harness = _Harness()
        before = {binding: dict(state) for binding, state in harness.states.items()}
        harness.epoch.fail_before_local_projection = True

        with self.assertRaisesRegex(RuntimeError, "owned backend stop failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.replace_calls, 1)
        self.assertEqual(harness.epoch.start_calls, 0)
        self.assertEqual(harness.states, before)
        self.assertEqual(harness.finalized, [])
        self.assertFalse(
            any(event.startswith("turn.note:") for event in harness.events)
        )
        self.assertFalse(
            any(event.startswith("bindings.detach:") for event in harness.events)
        )

    def test_interaction_fail_close_failure_keeps_backend_unreplaced(self) -> None:
        harness = _Harness()
        harness.fail_close_error = RuntimeError("interaction ledger unavailable")

        with self.assertRaisesRegex(RuntimeError, "interaction ledger unavailable"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.replace_calls, 0)

    def test_queue_clear_failure_keeps_fenced_backend_unreplaced(self) -> None:
        harness = _Harness()
        harness.queue_clear_error = RuntimeError("queue clear failed")

        with self.assertRaisesRegex(RuntimeError, "queue clear failed"):
            harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.epoch.fence_calls, 1)
        self.assertEqual(harness.epoch.replace_calls, 0)

        harness.queue_clear_error = None
        harness.service.reset_current_instance(force=True)

        self.assertEqual(harness.queue_clear_calls, 1)
        self.assertEqual(harness.epoch.fence_calls, 2)
        self.assertEqual(harness.epoch.replace_calls, 1)


if __name__ == "__main__":
    unittest.main()
