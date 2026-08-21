from __future__ import annotations

import ast
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import bot.focus_runtime.binding_coordinator as binding_coordinator_module
import bot.focus_runtime.runtime as focus_runtime_module
from bot.feishu_binding_transition import FeishuBindingTransitionCommit
from bot.focus_runtime.binding_coordinator import BindingRuntimeCoordinator
from bot.runtime_admin.binding_clear import (
    RuntimeBindingBatchDeactivationReceipt,
    RuntimeBindingDeactivationReceipt,
)
from bot.runtime_state import (
    ACTIVE_OBSERVER_EXECUTION_KIND,
    FEISHU_RUNTIME_ATTACHED,
    UNSET,
)
from bot.stores.interaction_lease_store import (
    InteractionLease,
    make_feishu_interaction_holder,
    make_web_interaction_holder,
)


_ROOT_PATH = pathlib.Path(focus_runtime_module.__file__).resolve()
_OWNER_PATH = pathlib.Path(binding_coordinator_module.__file__).resolve()
_REPO_ROOT = _ROOT_PATH.parents[2]
_CANDIDATE_ROOT_METHODS = {
    "_activate_binding_if_needed",
    "_is_sender_active_on_runtime",
    "_invalidate_feishu_execution_queue_locked",
    "_feishu_binding_execution_snapshot_locked",
    "_feishu_queue_ingress_snapshot",
    "_deactivate_binding_locked",
    "_deactivate_sender_impl",
    "_cancel_frontend_runtime_timers",
    "_hydrate_stored_bindings",
    "_feishu_interaction_holder",
    "_current_interaction_lease_locked",
    "_acquire_interaction_lease_for_binding",
    "_release_main_turn_for_binding",
    "_interactive_binding_for_thread",
    "_thread_subscribers",
    "_unsubscribe_thread_unless_web_runtime_requires_interest",
    "_unsubscribe_thread_and_clear_effective_model",
    "_archive_thread_and_clear_effective_model",
    "_delete_thread_and_clear_effective_model",
    "_release_service_thread_runtime_lease_unless_web_runtime_requires_interest",
    "_finalize_deactivated_feishu_binding_thread_runtime",
    "_resolve_session",
    "_resident_session",
    "_update_runtime_settings",
    "_rename_bound_thread_title",
    "_existing_chat_binding_key_locked",
    "_fresh_chat_binding_key",
    "_chat_binding_key",
    "_clear_thread_binding",
}
_OWNER_METHODS = {
    "activate_binding_if_needed",
    "is_sender_active_on_runtime",
    "feishu_binding_execution_snapshot_locked",
    "feishu_queue_ingress_snapshot",
    "deactivate_sender_impl",
    "cancel_frontend_runtime_timers",
    "release_main_turn_for_binding",
    "interactive_binding_for_thread",
    "thread_subscribers",
    "unsubscribe_thread_unless_web_runtime_requires_interest",
    "release_service_thread_runtime_lease_unless_web_runtime_requires_interest",
    "finalize_deactivated_feishu_binding_thread_runtime",
    "resident_session",
    "update_runtime_settings",
    "rename_bound_thread_title",
    "chat_binding_key",
    "clear_thread_binding",
}
_DELETED_THIN_WRAPPERS = {
    name.removeprefix("_") for name in _CANDIDATE_ROOT_METHODS
} - _OWNER_METHODS
_EXPECTED_DEPENDENCY_ATTRS = {
    "_lock",
    "_binding_runtime",
    "_binding_batch_deactivation",
    "_interaction_lease_store",
    "_thread_runtime_authority",
    "_service_runtime_authority",
    "_runtime_interest_retained",
    "_codex_thread_targets",
    "_feishu_binding_transitions",
}


def _class_node(path: pathlib.Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )


class _RecordingLock:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.depth = 0

    def __enter__(self):
        self.depth += 1
        self.events.append("lock:enter")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.events.append("lock:exit")
        self.depth -= 1

    @property
    def held(self) -> bool:
        return self.depth > 0


def _session(
    *,
    binding: tuple[str, str] = ("ou-user", "chat-1"),
    thread_id: str = "thread-1",
    title: str = "Demo",
    active: bool = True,
    attached: bool = True,
    running: bool = False,
    awaiting_started: bool = False,
    turn_id: str = "",
    has_execution_anchor: bool = False,
    execution_kind: str = "",
):
    return SimpleNamespace(
        binding=binding,
        handle=object(),
        current_thread_id=thread_id,
        current_thread_title=title,
        active=active,
        thread=SimpleNamespace(
            feishu_runtime_state=(FEISHU_RUNTIME_ATTACHED if attached else "detached")
        ),
        execution=SimpleNamespace(
            running=running,
            awaiting_local_turn_started=awaiting_started,
            current_turn_id=turn_id,
            has_execution_anchor=has_execution_anchor,
            current_execution_kind=execution_kind,
        ),
    )


class BindingRuntimeCoordinatorBoundaryTests(unittest.TestCase):
    def test_all_candidates_leave_root_and_only_cross_owner_methods_move(self) -> None:
        root = _class_node(_ROOT_PATH, "FocusRuntime")
        owner = _class_node(_OWNER_PATH, "BindingRuntimeCoordinator")
        root_methods = {
            node.name
            for node in root.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        root_self_references = {
            node.attr
            for node in ast.walk(root)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in _CANDIDATE_ROOT_METHODS
        }
        owner_methods = {
            node.name
            for node in owner.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name != "__init__"
        }

        self.assertEqual(len(_CANDIDATE_ROOT_METHODS), 29)
        self.assertEqual(root_methods & _CANDIDATE_ROOT_METHODS, set())
        self.assertEqual(root_self_references, set())
        self.assertEqual(owner_methods, _OWNER_METHODS)
        self.assertEqual(len(_OWNER_METHODS), 17)
        self.assertEqual(len(_DELETED_THIN_WRAPPERS), 12)
        self.assertTrue(_DELETED_THIN_WRAPPERS.isdisjoint(owner_methods))

        stale_test_references: list[tuple[str, int, str]] = []
        for path in (_REPO_ROOT / "tests").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id
                    in {
                        "codex_handler",
                        "focus_runtime",
                        "handler",
                        "handler2",
                        "runtime",
                    }
                    and node.attr in _CANDIDATE_ROOT_METHODS
                ):
                    stale_test_references.append(
                        (str(path.relative_to(_REPO_ROOT)), node.lineno, node.attr)
                    )
        self.assertEqual(stale_test_references, [])

    def test_owner_holds_only_existing_dependencies_without_root_or_new_state(self) -> None:
        source = _OWNER_PATH.read_text(encoding="utf-8")
        owner = _class_node(_OWNER_PATH, "BindingRuntimeCoordinator")
        initializer = next(
            node
            for node in owner.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        stored_attrs = {
            node.attr
            for node in ast.walk(owner)
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

        self.assertNotIn("FocusRuntime", source)
        self.assertNotIn("bot.focus_runtime.runtime", source)
        self.assertNotIn("WebRuntimeController", source)
        interest_port = next(
            argument
            for argument in initializer.args.kwonlyargs
            if argument.arg == "runtime_interest_retained"
        )
        self.assertEqual(
            ast.unparse(interest_port.annotation),
            "Callable[[str], bool]",
        )
        self.assertEqual(stored_attrs, _EXPECTED_DEPENDENCY_ATTRS)
        self.assertTrue(
            {
                "_focus_runtime",
                "_handler",
                "_runtime",
                "_store",
                "_map",
                "_timer",
                "_ledger",
            }.isdisjoint(stored_attrs)
        )

        root = _class_node(_ROOT_PATH, "FocusRuntime")
        composition_calls = [
            node
            for node in ast.walk(root)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "BindingRuntimeCoordinator"
        ]
        self.assertEqual(len(composition_calls), 1)
        composition_keywords = {
            keyword.arg: keyword.value
            for keyword in composition_calls[0].keywords
            if keyword.arg is not None
        }
        self.assertEqual(
            ast.dump(
                composition_keywords["runtime_interest_retained"],
                include_attributes=False,
            ),
            ast.dump(
                ast.parse(
                    "web_runtime.has_local_runtime_interest",
                    mode="eval",
                ).body,
                include_attributes=False,
            ),
        )


class BindingRuntimeCoordinatorTests(unittest.TestCase):
    def make_coordinator(self):
        events: list[str] = []
        lock = _RecordingLock(events)
        binding_runtime = Mock()
        binding_batch_deactivation = Mock()
        interaction_lease_store = Mock()
        thread_runtime_authority = Mock()
        service_runtime_authority = Mock()
        runtime_interest_retained = Mock(return_value=False)
        codex_thread_targets = Mock()
        binding_transitions = Mock()
        coordinator = BindingRuntimeCoordinator(
            lock=lock,
            binding_runtime=binding_runtime,
            binding_batch_deactivation=binding_batch_deactivation,
            interaction_lease_store=interaction_lease_store,
            thread_runtime_authority=thread_runtime_authority,
            service_runtime_authority=service_runtime_authority,
            runtime_interest_retained=runtime_interest_retained,
            codex_thread_targets=codex_thread_targets,
            feishu_binding_transitions=binding_transitions,
        )
        return SimpleNamespace(
            coordinator=coordinator,
            events=events,
            lock=lock,
            binding_runtime=binding_runtime,
            binding_batch_deactivation=binding_batch_deactivation,
            interaction_lease_store=interaction_lease_store,
            thread_runtime_authority=thread_runtime_authority,
            service_runtime_authority=service_runtime_authority,
            runtime_interest_retained=runtime_interest_retained,
            codex_thread_targets=codex_thread_targets,
            binding_transitions=binding_transitions,
        )

    def test_activation_resolves_before_locked_commit_and_active_reads_snapshot(self) -> None:
        harness = self.make_coordinator()
        captured = _session(active=True)

        def resolve(sender_id, chat_id, message_id):
            self.assertFalse(harness.lock.held)
            harness.events.append("session:resolve")
            self.assertEqual((sender_id, chat_id, message_id), (
                "ou-user",
                "chat-1",
                "message-1",
            ))
            return captured

        def activate(handle):
            self.assertTrue(harness.lock.held)
            self.assertIs(handle, captured.handle)
            harness.events.append("binding:activate")

        harness.binding_runtime.resolve_session.side_effect = resolve
        harness.binding_runtime.activate_session_locked.side_effect = activate

        harness.coordinator.activate_binding_if_needed(
            "ou-user",
            "chat-1",
            "message-1",
        )
        self.assertEqual(
            harness.events,
            [
                "session:resolve",
                "lock:enter",
                "binding:activate",
                "lock:exit",
            ],
        )

        harness.events.clear()
        self.assertTrue(
            harness.coordinator.is_sender_active_on_runtime(
                "ou-user",
                "chat-1",
                "message-1",
            )
        )
        self.assertEqual(harness.events, ["session:resolve"])

    def test_interactive_binding_truth_table_stays_under_shared_lock(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        holder = make_feishu_interaction_holder(*binding, owner_pid=0)
        lease = SimpleNamespace(holder=holder)

        def current_lease(_thread_id):
            self.assertTrue(harness.lock.held)
            return harness.current_lease

        def interactive(_thread_id, *, adopt_sole_subscriber):
            self.assertTrue(harness.lock.held)
            self.assertFalse(adopt_sole_subscriber)
            return harness.interactive_result

        harness.binding_runtime.current_interaction_lease_locked.side_effect = (
            current_lease
        )
        harness.binding_runtime.interactive_binding_for_thread_locked.side_effect = (
            interactive
        )
        harness.binding_runtime.feishu_interaction_holder.return_value = holder

        cases = (
            (None, (binding, False), (None, False)),
            (lease, (binding, True), (None, True)),
            (lease, (None, False), (None, False)),
            (
                SimpleNamespace(
                    holder=make_web_interaction_holder("tab-1", owner_pid=0)
                ),
                (binding, False),
                (None, False),
            ),
            (lease, (binding, False), (binding, False)),
        )
        for current, interactive_result, expected in cases:
            with self.subTest(
                current=current,
                interactive_result=interactive_result,
            ):
                harness.current_lease = current
                harness.interactive_result = interactive_result
                harness.binding_runtime.reset_mock()
                harness.binding_runtime.current_interaction_lease_locked.side_effect = (
                    current_lease
                )
                harness.binding_runtime.interactive_binding_for_thread_locked.side_effect = (
                    interactive
                )
                harness.binding_runtime.feishu_interaction_holder.return_value = holder
                harness.binding_runtime.thread_subscribers.return_value = ()

                self.assertEqual(
                    harness.coordinator.interactive_binding_for_thread("thread-1"),
                    expected,
                )
                harness.binding_runtime.current_interaction_lease_locked.assert_called_once_with(
                    "thread-1"
                )
                if current is None:
                    harness.binding_runtime.interactive_binding_for_thread_locked.assert_not_called()
                    harness.binding_runtime.thread_subscribers.assert_called_once_with(
                        "thread-1"
                    )
                else:
                    harness.binding_runtime.interactive_binding_for_thread_locked.assert_called_once_with(
                        "thread-1",
                        adopt_sole_subscriber=False,
                    )

    def test_active_observer_without_lease_suppresses_interaction_routing(
        self,
    ) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        observer = _session(
            binding=binding,
            running=True,
            turn_id="turn-live",
            execution_kind=ACTIVE_OBSERVER_EXECUTION_KIND,
        )
        harness.binding_runtime.current_interaction_lease_locked.return_value = None
        harness.binding_runtime.thread_subscribers.return_value = (binding,)
        harness.binding_runtime.resident_session_snapshot_locked.return_value = (
            observer
        )

        self.assertEqual(
            harness.coordinator.interactive_binding_for_thread("thread-1"),
            (None, True),
        )
        harness.binding_runtime.interactive_binding_for_thread_locked.assert_not_called()

    def test_active_observer_with_exact_feishu_lease_still_has_no_authority(
        self,
    ) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        holder = make_feishu_interaction_holder(*binding, owner_pid=0)
        observer = _session(
            binding=binding,
            running=True,
            turn_id="turn-live",
            execution_kind=ACTIVE_OBSERVER_EXECUTION_KIND,
        )
        harness.binding_runtime.current_interaction_lease_locked.return_value = (
            SimpleNamespace(holder=holder)
        )
        harness.binding_runtime.interactive_binding_for_thread_locked.return_value = (
            binding,
            False,
        )
        harness.binding_runtime.feishu_interaction_holder.return_value = holder
        harness.binding_runtime.resident_session_snapshot_locked.return_value = (
            observer
        )

        self.assertEqual(
            harness.coordinator.interactive_binding_for_thread("thread-1"),
            (None, True),
        )

    def test_subscriber_and_resident_reads_share_the_binding_lock(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        captured = _session(binding=binding)

        def subscribers(thread_id):
            self.assertTrue(harness.lock.held)
            self.assertEqual(thread_id, "thread-1")
            return (binding,)

        def resident(requested_binding):
            self.assertTrue(harness.lock.held)
            self.assertEqual(requested_binding, binding)
            return captured

        harness.binding_runtime.thread_subscribers.side_effect = subscribers
        harness.binding_runtime.resident_session_snapshot_locked.side_effect = resident

        self.assertEqual(
            harness.coordinator.thread_subscribers("thread-1"),
            (binding,),
        )
        self.assertIs(harness.coordinator.resident_session(binding), captured)
        self.assertEqual(
            harness.events,
            ["lock:enter", "lock:exit", "lock:enter", "lock:exit"],
        )

    def test_snapshots_project_only_immutable_queue_facts(self) -> None:
        harness = self.make_coordinator()
        captured = _session(
            thread_id="thread-1",
            active=True,
            attached=True,
            awaiting_started=True,
            turn_id="turn-1",
            has_execution_anchor=True,
        )
        harness.binding_runtime.resident_session_snapshot_locked.return_value = captured
        harness.binding_runtime.resolve_session.return_value = captured

        execution = harness.coordinator.feishu_binding_execution_snapshot_locked(
            captured.binding
        )
        ingress = harness.coordinator.feishu_queue_ingress_snapshot(
            "ou-user",
            "chat-1",
            "message-1",
        )

        self.assertEqual(execution.binding, captured.binding)
        self.assertEqual(execution.root_thread_id, "thread-1")
        self.assertTrue(execution.attached)
        self.assertTrue(execution.has_inflight_execution)
        self.assertEqual(execution.current_turn_id, "turn-1")
        self.assertEqual(ingress.binding, captured.binding)
        self.assertEqual(ingress.current_root_thread_id, "thread-1")
        self.assertEqual(ingress.current_turn_id, "turn-1")
        self.assertTrue(ingress.has_execution_anchor)

        harness.binding_runtime.resident_session_snapshot_locked.return_value = None
        self.assertIsNone(
            harness.coordinator.feishu_binding_execution_snapshot_locked(
                captured.binding
            )
        )

    def test_frontend_timer_cleanup_prepares_under_lock_then_cancels_outside(self) -> None:
        harness = self.make_coordinator()
        timer_effects = (object(), object())

        def prepare():
            self.assertTrue(harness.lock.held)
            harness.events.append("timers:prepare")
            return timer_effects

        harness.binding_runtime.prepare_all_timer_cancellations_locked.side_effect = (
            prepare
        )
        with patch.object(
            binding_coordinator_module,
            "cancel_runtime_timer_effects",
            side_effect=lambda effects: (
                self.assertFalse(harness.lock.held),
                self.assertIs(effects, timer_effects),
                harness.events.append("timers:cancel"),
            ),
        ):
            harness.coordinator.cancel_frontend_runtime_timers()

        self.assertEqual(
            harness.events,
            ["lock:enter", "timers:prepare", "lock:exit", "timers:cancel"],
        )

    def test_sender_deactivation_rechecks_then_commits_and_runs_effects_outside_lock(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        timer_effect = object()
        receipt = RuntimeBindingBatchDeactivationReceipt(
            confirmed_removals=(
                RuntimeBindingDeactivationReceipt(
                    binding=binding,
                    thread_id="thread-1",
                    unsubscribe_thread_id="thread-1",
                    timer_cancellations=(timer_effect,),
                ),
            )
        )
        harness.binding_runtime.existing_chat_binding_key_locked.return_value = binding

        def read_owner(_binding):
            self.assertTrue(harness.lock.held)
            harness.events.append("owner:read")
            return "thread-1"

        def deactivate(bindings):
            self.assertTrue(harness.lock.held)
            harness.events.append("batch:commit")
            self.assertEqual(bindings, (binding,))
            return receipt

        harness.binding_runtime.binding_owner_thread_id_locked.side_effect = read_owner
        harness.binding_batch_deactivation.deactivate_locked.side_effect = deactivate
        harness.coordinator.finalize_deactivated_feishu_binding_thread_runtime = Mock(
            side_effect=lambda *_args, **_kwargs: harness.events.append("finalize")
        )

        with patch.object(
            binding_coordinator_module,
            "cancel_runtime_timer_effects",
            side_effect=lambda effects: (
                self.assertFalse(harness.lock.held),
                self.assertEqual(effects, (timer_effect,)),
                harness.events.append("timers:cancel"),
            ),
        ):
            harness.coordinator.deactivate_sender_impl(
                "ou-user",
                "chat-1",
                message_id="message-1",
            )

        self.assertEqual(
            harness.events,
            [
                "lock:enter",
                "lock:exit",
                "lock:enter",
                "owner:read",
                "lock:exit",
                "lock:enter",
                "owner:read",
                "batch:commit",
                "lock:exit",
                "timers:cancel",
                "finalize",
            ],
        )
        harness.coordinator.finalize_deactivated_feishu_binding_thread_runtime.assert_called_once_with(
            "thread-1",
            cleanup_reason="sender_deactivated",
        )

    def test_sender_deactivation_rejects_changed_target_before_commit(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        harness.binding_runtime.existing_chat_binding_key_locked.return_value = binding
        harness.binding_runtime.binding_owner_thread_id_locked.side_effect = (
            "thread-old",
            "thread-new",
        )

        with (
            patch.object(binding_coordinator_module, "cancel_runtime_timer_effects") as cancel,
            self.assertRaisesRegex(RuntimeError, "核验期间发生变化"),
        ):
            harness.coordinator.deactivate_sender_impl("ou-user", "chat-1")

        harness.binding_batch_deactivation.deactivate_locked.assert_not_called()
        cancel.assert_not_called()

    def test_sender_finalizes_only_owner_confirmed_unsubscribe_target(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        harness.binding_runtime.existing_chat_binding_key_locked.return_value = binding
        harness.binding_runtime.binding_owner_thread_id_locked.return_value = "thread-1"
        harness.binding_batch_deactivation.deactivate_locked.return_value = (
            RuntimeBindingBatchDeactivationReceipt(confirmed_removals=())
        )
        harness.coordinator.finalize_deactivated_feishu_binding_thread_runtime = Mock()

        with patch.object(binding_coordinator_module, "cancel_runtime_timer_effects"):
            harness.coordinator.deactivate_sender_impl("ou-user", "chat-1")

        harness.coordinator.finalize_deactivated_feishu_binding_thread_runtime.assert_not_called()

    def test_web_interest_blocks_unsubscribe_and_service_release(self) -> None:
        harness = self.make_coordinator()
        harness.runtime_interest_retained.return_value = True

        harness.coordinator.unsubscribe_thread_unless_web_runtime_requires_interest(
            "thread-1"
        )
        harness.coordinator.release_service_thread_runtime_lease_unless_web_runtime_requires_interest(
            "thread-1"
        )

        harness.thread_runtime_authority.unsubscribe_thread.assert_not_called()
        harness.service_runtime_authority.release_service_thread_runtime_lease.assert_not_called()

        harness.runtime_interest_retained.return_value = False
        harness.coordinator.unsubscribe_thread_unless_web_runtime_requires_interest(
            "thread-1"
        )
        harness.coordinator.release_service_thread_runtime_lease_unless_web_runtime_requires_interest(
            "thread-1"
        )
        harness.thread_runtime_authority.unsubscribe_thread.assert_called_once_with(
            "thread-1"
        )
        harness.service_runtime_authority.release_service_thread_runtime_lease.assert_called_once_with(
            "thread-1"
        )

    def test_finalize_proves_direct_root_before_unsubscribe_and_release(self) -> None:
        harness = self.make_coordinator()
        harness.codex_thread_targets.read_direct_thread_summary_authoritatively.side_effect = (
            lambda *_args, **_kwargs: harness.events.append("target:read")
        )
        harness.thread_runtime_authority.unsubscribe_thread.side_effect = (
            lambda _thread_id: harness.events.append("upstream:unsubscribe")
        )
        harness.service_runtime_authority.release_service_thread_runtime_lease.side_effect = (
            lambda _thread_id: harness.events.append("service:release")
        )

        harness.coordinator.finalize_deactivated_feishu_binding_thread_runtime(
            " thread-1 ",
            cleanup_reason="sender_deactivated",
        )

        self.assertEqual(
            harness.events,
            ["target:read", "upstream:unsubscribe", "service:release"],
        )
        harness.codex_thread_targets.read_direct_thread_summary_authoritatively.assert_called_once_with(
            "thread-1",
            original_arg="thread-1",
            operation="取消飞书 thread 订阅",
        )

    def test_finalize_child_or_read_failure_keeps_completed_local_cleanup(self) -> None:
        for error in (ValueError("ThreadSpawn child"), OSError("read failed")):
            with self.subTest(error=error):
                harness = self.make_coordinator()
                binding = ("ou-user", "chat-1")
                harness.binding_runtime.existing_chat_binding_key_locked.return_value = binding
                harness.binding_runtime.binding_owner_thread_id_locked.return_value = "thread-1"
                harness.binding_batch_deactivation.deactivate_locked.return_value = (
                    RuntimeBindingBatchDeactivationReceipt(
                        confirmed_removals=(
                            RuntimeBindingDeactivationReceipt(
                                binding=binding,
                                thread_id="thread-1",
                                unsubscribe_thread_id="thread-1",
                            ),
                        )
                    )
                )
                harness.codex_thread_targets.read_direct_thread_summary_authoritatively.side_effect = error

                with patch.object(
                    binding_coordinator_module,
                    "cancel_runtime_timer_effects",
                ) as cancel:
                    harness.coordinator.deactivate_sender_impl("ou-user", "chat-1")

                harness.binding_batch_deactivation.deactivate_locked.assert_called_once_with(
                    (binding,)
                )
                cancel.assert_called_once_with(())
                harness.thread_runtime_authority.unsubscribe_thread.assert_not_called()
                harness.service_runtime_authority.release_service_thread_runtime_lease.assert_not_called()

    def test_release_main_turn_requires_exact_feishu_holder_thread_and_turn(self) -> None:
        harness = self.make_coordinator()
        binding = ("ou-user", "chat-1")
        holder = make_feishu_interaction_holder(*binding, owner_pid=0)
        harness.binding_runtime.feishu_interaction_holder.return_value = holder
        harness.interaction_lease_store.load.return_value = InteractionLease(
            thread_id="thread-1",
            holder=holder,
            lease_id="lease-1",
            updated_at=1.0,
            turn_id="turn-1",
        )
        harness.interaction_lease_store.release_turn.return_value = True

        self.assertTrue(
            harness.coordinator.release_main_turn_for_binding(
                binding,
                " thread-1 ",
                " turn-1 ",
            )
        )
        harness.interaction_lease_store.load.assert_called_once_with("thread-1")
        harness.interaction_lease_store.release_turn.assert_called_once_with(
            "thread-1",
            "turn-1",
        )

        harness.interaction_lease_store.reset_mock()
        harness.interaction_lease_store.load.return_value = SimpleNamespace(
            holder=make_web_interaction_holder("tab-1", owner_pid=0)
        )
        self.assertFalse(
            harness.coordinator.release_main_turn_for_binding(
                binding,
                "thread-1",
                "turn-1",
            )
        )
        harness.interaction_lease_store.release_turn.assert_not_called()

        for thread_id, turn_id in (("", "turn-1"), ("thread-1", "")):
            with self.subTest(thread_id=thread_id, turn_id=turn_id):
                harness.interaction_lease_store.reset_mock()
                self.assertFalse(
                    harness.coordinator.release_main_turn_for_binding(
                        binding,
                        thread_id,
                        turn_id,
                    )
                )
                harness.interaction_lease_store.load.assert_not_called()
                harness.interaction_lease_store.release_turn.assert_not_called()

    def test_settings_and_title_use_captured_handle_under_lock_and_title_cas(self) -> None:
        harness = self.make_coordinator()
        captured = _session(thread_id="thread-current")
        harness.binding_runtime.resolve_session.return_value = captured

        def update_settings(handle, **kwargs):
            self.assertTrue(harness.lock.held)
            self.assertIs(handle, captured.handle)
            harness.events.append("settings:update")

        def update_title(handle, **kwargs):
            self.assertTrue(harness.lock.held)
            self.assertIs(handle, captured.handle)
            harness.events.append("title:update")
            return captured

        harness.binding_runtime.update_runtime_settings_locked.side_effect = update_settings
        harness.binding_runtime.update_thread_metadata_locked.side_effect = update_title

        harness.coordinator.update_runtime_settings(
            "ou-user",
            "chat-1",
            message_id="message-1",
            approval_policy="never",
            model="gpt-5.4",
        )
        self.assertTrue(
            harness.coordinator.rename_bound_thread_title(
                "ou-user",
                "chat-1",
                " New title ",
                message_id="message-1",
                thread_id=" thread-expected ",
            )
        )

        harness.binding_runtime.update_runtime_settings_locked.assert_called_once_with(
            captured.handle,
            approval_policy="never",
            permissions_profile_id=UNSET,
            model="gpt-5.4",
            reasoning_effort=UNSET,
        )
        harness.binding_runtime.update_thread_metadata_locked.assert_called_once_with(
            captured.handle,
            expected_thread_id="thread-expected",
            current_thread_title="New title",
        )
        self.assertEqual(
            [event for event in harness.events if event.endswith(":update")],
            ["settings:update", "title:update"],
        )

        harness.binding_runtime.update_thread_metadata_locked.return_value = None
        harness.binding_runtime.update_thread_metadata_locked.side_effect = None
        self.assertFalse(
            harness.coordinator.rename_bound_thread_title(
                "ou-user",
                "chat-1",
                "title",
                thread_id="different-thread",
            )
        )

    def test_chat_binding_key_uses_existing_under_lock_and_fresh_outside(self) -> None:
        harness = self.make_coordinator()
        existing = ("ou-existing", "chat-1")

        def existing_lookup(_sender_id, _chat_id):
            self.assertTrue(harness.lock.held)
            return existing

        harness.binding_runtime.existing_chat_binding_key_locked.side_effect = existing_lookup
        self.assertEqual(
            harness.coordinator.chat_binding_key("ou-user", "chat-1", "message-1"),
            existing,
        )
        harness.binding_runtime.fresh_chat_binding_key.assert_not_called()

        harness.binding_runtime.existing_chat_binding_key_locked.side_effect = (
            lambda _sender_id, _chat_id: (
                self.assertTrue(harness.lock.held) or None
            )
        )

        def fresh_lookup(sender_id, chat_id, message_id):
            self.assertFalse(harness.lock.held)
            return (sender_id, chat_id)

        harness.binding_runtime.fresh_chat_binding_key.side_effect = fresh_lookup
        self.assertEqual(
            harness.coordinator.chat_binding_key("ou-fresh", "chat-2", "message-2"),
            ("ou-fresh", "chat-2"),
        )
        harness.binding_runtime.fresh_chat_binding_key.assert_called_once_with(
            "ou-fresh",
            "chat-2",
            "message-2",
        )

    def test_clear_thread_commits_transition_then_reports_cleanup_incomplete(self) -> None:
        harness = self.make_coordinator()
        captured = _session(thread_id="thread-old")
        committed = FeishuBindingTransitionCommit(
            session=_session(thread_id=""),
            previous_thread_id="thread-old",
            unsubscribe_thread_id="thread-old",
            queue_cleanup_failed=False,
        )
        harness.binding_runtime.resolve_session.return_value = captured
        harness.binding_transitions.clear_thread.return_value = committed

        self.assertFalse(
            harness.coordinator.clear_thread_binding(
                "ou-user",
                "chat-1",
                message_id="message-1",
                working_dir_after_clear="/tmp/next",
                require_no_inflight_turn=True,
            )
        )
        command = harness.binding_transitions.clear_thread.call_args.args[0]
        self.assertIs(command.session, captured)
        self.assertEqual(command.working_dir_after_clear, "/tmp/next")
        self.assertTrue(command.require_no_inflight_turn)
        harness.thread_runtime_authority.unsubscribe_thread.assert_called_once_with(
            "thread-old"
        )
        harness.service_runtime_authority.release_service_thread_runtime_lease.assert_called_once_with(
            "thread-old"
        )

        harness.thread_runtime_authority.unsubscribe_thread.reset_mock()
        harness.service_runtime_authority.release_service_thread_runtime_lease.reset_mock()
        harness.binding_transitions.clear_thread.return_value = FeishuBindingTransitionCommit(
            session=_session(thread_id=""),
            previous_thread_id="thread-old",
            unsubscribe_thread_id="",
            queue_cleanup_failed=True,
        )
        self.assertTrue(
            harness.coordinator.clear_thread_binding(
                "ou-user",
                "chat-1",
                session=captured,
            )
        )
        harness.binding_runtime.resolve_session.assert_called_once_with(
            "ou-user",
            "chat-1",
            "message-1",
        )
        harness.thread_runtime_authority.unsubscribe_thread.assert_not_called()
        harness.service_runtime_authority.release_service_thread_runtime_lease.assert_not_called()

    def test_clear_thread_cleanup_failure_does_not_undo_transition(self) -> None:
        harness = self.make_coordinator()
        captured = _session(thread_id="thread-old")
        harness.binding_transitions.clear_thread.return_value = FeishuBindingTransitionCommit(
            session=_session(thread_id=""),
            previous_thread_id="thread-old",
            unsubscribe_thread_id="thread-old",
            queue_cleanup_failed=False,
        )
        harness.thread_runtime_authority.unsubscribe_thread.side_effect = OSError(
            "unsubscribe failed"
        )

        self.assertTrue(
            harness.coordinator.clear_thread_binding(
                "ou-user",
                "chat-1",
                session=captured,
            )
        )
        harness.binding_transitions.clear_thread.assert_called_once()
        harness.service_runtime_authority.release_service_thread_runtime_lease.assert_not_called()


if __name__ == "__main__":
    unittest.main()
