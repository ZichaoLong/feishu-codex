from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.adapters.base import ThreadGoalSummary, ThreadSnapshot, ThreadSummary
from bot.binding_execution_runtime import (
    BindingExecutionRuntimeTransitions,
    PrimeActiveObserverExecutionCommand,
    RollbackDetachedActiveObserverExecutionCommand,
)
from bot.binding_runtime_contract import BindingOwnerLossSettlementReceipt
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.feishu_active_observer import (
    ActiveObserverExecution,
    ActiveObserverPresentationResult,
    ActiveObserverResumeSnapshot,
    ActiveObserverResumeSnapshotRejected,
)
from bot.feishu_binding_transition import FeishuBindingTransitionOwner
from bot.feishu_root_operation_contract import FeishuRootOperationToken
from bot.feishu_thread_session_coordinator import (
    FeishuThreadSessionCoordinator,
    FeishuThreadSessionPorts,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_runtime_authority import ThreadResumeLocalFailurePolicy
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


def _summary(
    thread_id: str,
    *,
    cwd: str = "/workspace/project",
    source: str = "appServer",
    subagent_kind: str | None = None,
) -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        cwd=cwd,
        name=thread_id,
        preview="",
        created_at=0,
        updated_at=0,
        source=source,
        status="idle",
        subagent_kind=subagent_kind,
    )


class _FakeExecutionQueue:
    def __init__(self) -> None:
        self.invalidated: list[tuple[str, str]] = []

    def invalidate_binding(self, binding: tuple[str, str]) -> object:
        self.invalidated.append(binding)
        return object()


class _FakePendingResume:
    def __init__(
        self,
        runtime: _FakeThreadRuntime,
        snapshot: ThreadSnapshot,
    ) -> None:
        self.response = snapshot
        self.lease_receipt = SimpleNamespace(
            thread_id=snapshot.summary.thread_id
        )
        self._runtime = runtime

    def commit_local_state(self, local_commit, *, failure_policy):
        self._runtime.failure_policies.append(failure_policy)
        self._runtime.events.append("resume_local_commit_begin")
        committed = local_commit()
        self._runtime.events.append("resume_receipt_settled")
        return committed


class _FakeThreadRuntime:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.create_snapshot = ThreadSnapshot(summary=_summary("thread-created"))
        self.resume_snapshots: dict[str, ThreadSnapshot] = {}
        self.failure_policies: list[ThreadResumeLocalFailurePolicy] = []
        self.resume_error: Exception | None = None
        self.create_kwargs: dict = {}
        self.resume_kwargs: dict[str, dict] = {}

    def create_and_commit_thread(self, *, local_commit, **kwargs):
        self.create_kwargs = dict(kwargs)
        self.events.append("create_ack")
        local_result = local_commit(self.create_snapshot)
        self.events.append("create_receipt_settled")
        return SimpleNamespace(
            response=self.create_snapshot,
            local_result=local_result,
        )

    def begin_resume_thread(
        self,
        thread_id: str,
        *,
        exact_mutation_guard=None,
        **kwargs,
    ):
        if exact_mutation_guard is not None and not exact_mutation_guard():
            raise RuntimeError("exact guard rejected")
        if self.resume_error is not None:
            raise self.resume_error
        self.resume_kwargs[thread_id] = dict(kwargs)
        self.events.append("resume_ack")
        return _FakePendingResume(self, self.resume_snapshots[thread_id])

    def unsubscribe_thread(self, thread_id: str) -> None:
        self.events.append(f"unsubscribe:{thread_id}")


class _FakeAdapter:
    def __init__(self) -> None:
        self.snapshots: dict[str, ThreadSnapshot] = {}
        self.goals: dict[str, ThreadGoalSummary] = {}

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> ThreadSnapshot:
        del include_turns
        return self.snapshots[thread_id]

    def list_threads_all(self, **_kwargs) -> list[ThreadSummary]:
        return [snapshot.summary for snapshot in self.snapshots.values()]

    def get_thread_goal(self, thread_id: str) -> ThreadGoalSummary | None:
        return self.goals.get(thread_id)


class _FakeRootOperations:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def commit_resume_owner(self, _token: FeishuRootOperationToken) -> None:
        self.events.append("root_owner_commit")


class _FakeWarnings:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def record(self, **kwargs) -> None:
        self.items.append(dict(kwargs))


class FeishuThreadSessionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = pathlib.Path(temporary.name)
        self.lock = threading.RLock()
        self.binding = ("ou-user", "chat-1")
        settlement_nonce = 0

        def settle_owner_loss(command):
            nonlocal settlement_nonce
            settlement_nonce += 1
            return BindingOwnerLossSettlementReceipt(
                command=command,
                _settler_nonce=1,
                _transaction_nonce=settlement_nonce,
            )

        self.binding_store = ChatBindingStore(data_dir)
        self.manager = BindingRuntimeManager(
            lock=self.lock,
            default_working_dir="/workspace/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=self.binding_store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda _chat_id, _message_id: False,
            owner_loss_settler=settle_owner_loss,
        )
        self.queue = _FakeExecutionQueue()
        self.binding_transitions = FeishuBindingTransitionOwner(
            lock=self.lock,
            binding_runtime=self.manager,
            execution_queue=self.queue,
        )
        self.execution_runtime = BindingExecutionRuntimeTransitions(
            lock=self.lock,
            binding_runtime=self.manager,
            turn_execution=TurnExecutionCoordinator(),
        )
        self.runtime = _FakeThreadRuntime()
        self.adapter = _FakeAdapter()
        self.remembered: list[str] = []
        self.root_operations = _FakeRootOperations(self.runtime.events)
        self.warnings = _FakeWarnings()
        self.retained: set[str] = set()
        self.observer_primes: list[
            tuple[object, ActiveObserverResumeSnapshot]
        ] = []
        self.observer_presentations: list[ActiveObserverExecution] = []
        self.observer_recoveries: list[object] = []
        self.observer_prime_error: Exception | None = None
        self.observer_prime_result: object | None = None
        self.observer_presentation_error: Exception | None = None

        def prepare_active_observer(snapshot: ThreadSnapshot):
            active_turns = [
                turn
                for turn in snapshot.turns
                if turn.get("status") == "inProgress"
            ]
            if not active_turns:
                if snapshot.summary.status == "active":
                    raise ActiveObserverResumeSnapshotRejected(
                        "active test snapshot has no exact active turn"
                    )
                return None
            turn_id = str(active_turns[0].get("id", "") or "").strip()
            if len(active_turns) != 1 or not turn_id:
                raise ActiveObserverResumeSnapshotRejected(
                    "test snapshot has no exact active turn"
                )
            return ActiveObserverResumeSnapshot(
                turn_id=turn_id,
                reply_items=(),
            )

        def prime_active_observer(session, prepared):
            self.observer_primes.append((session, prepared))
            if self.observer_prime_error is not None:
                raise self.observer_prime_error
            primed = self.execution_runtime.prime_active_observer_execution(
                PrimeActiveObserverExecutionCommand(
                    session=session,
                    turn_id=prepared.turn_id,
                    reply_items=prepared.reply_items,
                    started_at=1.0,
                )
            )
            if self.observer_prime_result is not None:
                return self.observer_prime_result
            return ActiveObserverExecution(
                session=primed,
                turn_id=prepared.turn_id,
            )

        def rollback_active_observer(session, prepared):
            self.execution_runtime.rollback_detached_active_observer_execution(
                RollbackDetachedActiveObserverExecutionCommand(
                    session=session,
                    turn_id=prepared.turn_id,
                )
            )

        def present_active_observer(execution):
            self.observer_presentations.append(execution)
            if self.observer_presentation_error is not None:
                raise self.observer_presentation_error
            return ActiveObserverPresentationResult(
                status="opened",
                turn_id=execution.turn_id,
            )

        def acquire_runtime_lease(thread_id: str) -> bool:
            self.runtime.events.append(f"acquire:{thread_id}")
            return True

        def release_runtime_lease(thread_id: str) -> None:
            self.runtime.events.append(f"release:{thread_id}")

        self.coordinator = FeishuThreadSessionCoordinator(
            lock=self.lock,
            adapter=self.adapter,
            binding_runtime=self.manager,
            binding_transitions=self.binding_transitions,
            thread_runtime=self.runtime,
            root_operations=self.root_operations,
            warnings=self.warnings,
            ports=FeishuThreadSessionPorts(
                acquire_runtime_lease=acquire_runtime_lease,
                release_runtime_lease=release_runtime_lease,
                runtime_interest_retained=lambda thread_id: (
                    thread_id in self.retained
                ),
                remember_direct_thread_summary=lambda summary: (
                    self.runtime.events.append(f"remember:{summary.thread_id}"),
                    self.remembered.append(summary.thread_id),
                ),
                is_thread_not_found_error=lambda exc: isinstance(exc, KeyError),
                is_transport_disconnect=lambda exc: "disconnect" in str(exc),
                prepare_active_observer=prepare_active_observer,
                prime_active_observer=prime_active_observer,
                rollback_active_observer=rollback_active_observer,
                present_active_observer=present_active_observer,
                schedule_active_observer_recovery=(
                    self.observer_recoveries.append
                ),
            ),
        )

    def _record_binding_commit(self):
        original = self.binding_transitions.bind_thread

        def commit(command):
            self.runtime.events.append("binding_commit")
            return original(command)

        return patch.object(
            self.binding_transitions,
            "bind_thread",
            side_effect=commit,
        )

    def test_create_ack_binding_commit_and_receipt_are_one_ordered_path(self) -> None:
        self.adapter.snapshots["thread-created"] = self.runtime.create_snapshot

        with self._record_binding_commit():
            snapshot = self.coordinator.create_and_bind_thread(
                *self.binding,
                cwd="/workspace/new",
                model="gpt-5.4",
            )

        self.assertEqual(snapshot.summary.thread_id, "thread-created")
        self.assertEqual(
            self.manager.resolve_session(*self.binding).current_thread_id,
            "thread-created",
        )
        self.assertLess(
            self.runtime.events.index("create_ack"),
            self.runtime.events.index("binding_commit"),
        )
        self.assertLess(
            self.runtime.events.index("binding_commit"),
            self.runtime.events.index("create_receipt_settled"),
        )

    def test_resume_settles_receipt_before_old_runtime_cleanup(self) -> None:
        old = _summary("thread-old")
        new = _summary("thread-new")
        self.adapter.snapshots[old.thread_id] = ThreadSnapshot(summary=old)
        self.adapter.snapshots[new.thread_id] = ThreadSnapshot(summary=new)
        self.coordinator.bind_thread(*self.binding, old)
        self.runtime.events.clear()
        self.runtime.resume_snapshots[new.thread_id] = ThreadSnapshot(summary=new)
        self.adapter.goals[new.thread_id] = ThreadGoalSummary(
            thread_id=new.thread_id,
            objective="ship",
            status="paused",
        )

        with self._record_binding_commit():
            snapshot = self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                new.thread_id,
                original_arg=new.thread_id,
                summary=new,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

        self.assertEqual(snapshot.summary.thread_id, new.thread_id)
        ordered = [
            "resume_ack",
            "resume_local_commit_begin",
            "binding_commit",
            "resume_receipt_settled",
            "unsubscribe:thread-old",
            "release:thread-old",
        ]
        positions = [self.runtime.events.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))
        session = self.manager.resolve_session(*self.binding)
        self.assertEqual(session.current_thread_id, new.thread_id)
        self.assertEqual(session.goal.objective, "ship")
        self.assertEqual(self.queue.invalidated, [self.binding])

    def test_active_observer_bootstraps_from_same_resume_response(self) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(
            summary=thread,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)

        returned = self.coordinator.resume_and_commit_feishu_binding(
            *self.binding,
            thread.thread_id,
            original_arg=thread.thread_id,
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            active_observer=True,
        )

        self.assertIs(returned, snapshot)
        self.assertEqual(len(self.observer_primes), 1)
        session, prepared = self.observer_primes[0]
        self.assertEqual(prepared.turn_id, "turn-live")
        self.assertEqual(session.current_thread_id, thread.thread_id)
        self.assertEqual(len(self.observer_presentations), 1)
        self.assertEqual(
            self.observer_presentations[0].turn_id,
            "turn-live",
        )
        self.assertEqual(
            self.observer_recoveries,
            [self.observer_presentations[0].session],
        )
        self.assertEqual(self.warnings.items, [])

    def test_active_observer_prime_failure_restores_detached_binding(
        self,
    ) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(
            summary=thread,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)
        self.observer_prime_error = RuntimeError("prime failed")

        with self.assertRaisesRegex(RuntimeError, "prime failed"):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                thread.thread_id,
                original_arg=thread.thread_id,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                active_observer=True,
            )

        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current.current_thread_id, thread.thread_id)
        self.assertEqual(current.thread.feishu_runtime_state, "detached")
        self.assertFalse(current.execution.running)
        self.assertEqual(self.observer_presentations, [])

    def test_active_observer_invalid_prime_result_restores_detached_binding(
        self,
    ) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(
            summary=thread,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)
        self.observer_prime_result = object()

        with self.assertRaisesRegex(
            RuntimeError,
            "active observer prime returned an invalid execution",
        ):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                thread.thread_id,
                original_arg=thread.thread_id,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                active_observer=True,
            )

        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current.current_thread_id, thread.thread_id)
        self.assertEqual(current.thread.feishu_runtime_state, "detached")
        self.assertFalse(current.execution.running)
        self.assertEqual(self.observer_presentations, [])

    def test_active_observer_attach_persist_failure_keeps_detached_idle(
        self,
    ) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(
            summary=thread,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)

        with patch.object(
            self.binding_store,
            "save",
            side_effect=OSError("attach save unavailable"),
        ) as save:
            with self.assertRaisesRegex(OSError, "attach save unavailable"):
                self.coordinator.resume_and_commit_feishu_binding(
                    *self.binding,
                    thread.thread_id,
                    original_arg=thread.thread_id,
                    failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                    active_observer=True,
                )

        self.assertEqual(save.call_count, 1)
        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current.current_thread_id, thread.thread_id)
        self.assertEqual(current.thread.feishu_runtime_state, "detached")
        self.assertFalse(current.execution.running)
        self.assertEqual(current.execution.current_turn_id, "")
        self.assertEqual(self.manager.thread_subscribers(thread.thread_id), ())
        stored = self.binding_store.load(self.binding)
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "detached")
        self.assertEqual(self.observer_presentations, [])

    def test_active_observer_presentation_failure_still_schedules_recovery(
        self,
    ) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(
            summary=thread,
            turns=[
                {"id": "turn-live", "status": "inProgress", "items": []}
            ],
        )
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)
        self.observer_presentation_error = RuntimeError("card unavailable")

        returned = self.coordinator.resume_and_commit_feishu_binding(
            *self.binding,
            thread.thread_id,
            original_arg=thread.thread_id,
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            active_observer=True,
        )

        self.assertIs(returned, snapshot)
        self.assertEqual(len(self.observer_recoveries), 1)
        recovery = self.observer_recoveries[0]
        self.assertTrue(recovery.running)
        self.assertEqual(recovery.execution.current_turn_id, "turn-live")
        self.assertEqual(
            [item["code"] for item in self.warnings.items],
            ["active_observer_presentation_failed"],
        )

    def test_active_observer_missing_turn_never_commits_attached_binding(
        self,
    ) -> None:
        thread = _summary("thread-live")
        thread.status = "active"
        snapshot = ThreadSnapshot(summary=thread, turns=[])
        self.adapter.snapshots[thread.thread_id] = snapshot
        self.runtime.resume_snapshots[thread.thread_id] = snapshot
        self.coordinator.bind_thread(*self.binding, thread)
        with self.lock:
            self.manager.detach_binding_locked(self.binding)

        with self.assertRaises(ActiveObserverResumeSnapshotRejected):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                thread.thread_id,
                original_arg=thread.thread_id,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                active_observer=True,
            )

        current = self.manager.resolve_session(*self.binding)
        self.assertEqual(current.thread.feishu_runtime_state, "detached")
        self.assertFalse(current.execution.running)
        self.assertEqual(self.observer_primes, [])

    def test_retained_old_runtime_is_not_unsubscribed_or_released(self) -> None:
        old = _summary("thread-old")
        new = _summary("thread-new")
        self.adapter.snapshots[old.thread_id] = ThreadSnapshot(summary=old)
        self.adapter.snapshots[new.thread_id] = ThreadSnapshot(summary=new)
        self.coordinator.bind_thread(*self.binding, old)
        self.runtime.events.clear()
        self.retained.add(old.thread_id)
        self.runtime.resume_snapshots[new.thread_id] = ThreadSnapshot(summary=new)

        self.coordinator.resume_and_commit_feishu_binding(
            *self.binding,
            new.thread_id,
            original_arg=new.thread_id,
            summary=new,
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
        )

        self.assertNotIn("unsubscribe:thread-old", self.runtime.events)
        self.assertNotIn("release:thread-old", self.runtime.events)
        self.assertEqual(
            self.manager.resolve_session(*self.binding).current_thread_id,
            new.thread_id,
        )

    def test_operation_owner_commit_is_inside_resume_receipt_settlement(self) -> None:
        thread = _summary("thread-operation")
        self.runtime.resume_snapshots[thread.thread_id] = ThreadSnapshot(
            summary=thread
        )
        self.adapter.snapshots[thread.thread_id] = ThreadSnapshot(summary=thread)

        snapshot = self.coordinator.resume_and_commit_feishu_operation_owner(
            FeishuRootOperationToken(1, 1),
            thread.thread_id,
            original_arg=thread.thread_id,
            summary=thread,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

        self.assertEqual(snapshot.summary.thread_id, thread.thread_id)
        ordered = [
            "resume_ack",
            "resume_local_commit_begin",
            "root_owner_commit",
            "resume_receipt_settled",
        ]
        positions = [self.runtime.events.index(item) for item in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_threadspawn_target_is_rejected_before_resume(self) -> None:
        child = _summary("thread-child", subagent_kind="threadSpawn")
        self.adapter.snapshots[child.thread_id] = ThreadSnapshot(summary=child)

        with self.assertRaisesRegex(ValueError, "ThreadSpawn"):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                child.thread_id,
                original_arg=child.thread_id,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

        self.assertNotIn("resume_ack", self.runtime.events)

    def test_cli_disconnect_is_translated_after_authoritative_target_proof(self) -> None:
        thread = _summary("thread-cli", source="cli")
        self.adapter.snapshots[thread.thread_id] = ThreadSnapshot(summary=thread)
        self.runtime.resume_error = RuntimeError("transport disconnect")

        with self.assertRaisesRegex(RuntimeError, "无法通过 app-server"):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                thread.thread_id,
                original_arg=thread.thread_id,
                summary=thread,
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

    def test_missing_direct_target_is_mapped_before_resume(self) -> None:
        with self.assertRaisesRegex(ValueError, "未找到匹配的线程"):
            self.coordinator.resume_and_commit_feishu_binding(
                *self.binding,
                "thread-missing",
                original_arg="thread-missing",
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

        self.assertNotIn("resume_ack", self.runtime.events)


if __name__ == "__main__":
    unittest.main()
