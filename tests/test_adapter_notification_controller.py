import pathlib
import tempfile
import threading
import unittest

from bot.adapter_notification_controller import (
    AdapterNotificationController,
    AdapterNotificationEffects,
)
from bot.adapter_notification_runtime import AdapterNotificationRuntimeTransitions
from bot.binding_runtime_contract import (
    BindingExecutionTarget,
    BindingRuntimeHandle,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.binding_runtime_snapshot import project_binding_session_snapshot
from bot.constants import GROUP_SHARED_BINDING_OWNER_ID
from bot.execution_page_output_contract import (
    InitialExecutionPageOpenResult,
    InitialExecutionPageOpenStatus,
)
from bot.runtime_state import (
    FEISHU_RUNTIME_DETACHED,
    ThreadStateChanged,
    apply_runtime_state_message,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator
from tests.execution_page_test_support import set_execution_page_state
from tests.runtime_admin_test_support import make_binding_runtime


class _TestBindingRuntime:
    def __init__(self, lock, states) -> None:
        self._lock = lock
        self.states = states
        self._next_incarnation = 1
        self.handles = {
            binding: self._new_handle(binding)
            for binding in states
        }

    def _new_handle(self, binding):
        handle = BindingRuntimeHandle(
            _issuer_nonce=1,
            binding=binding,
            incarnation=self._next_incarnation,
        )
        self._next_incarnation += 1
        return handle

    def resident_session_snapshot_locked(self, binding):
        assert self._lock._is_owned()
        state = self.states.get(binding)
        if state is None:
            return None
        handle = self.handles.get(binding)
        if handle is None:
            handle = self._new_handle(binding)
            self.handles[binding] = handle
        return project_binding_session_snapshot(state, handle=handle)

    def session_snapshot_locked(self, handle):
        assert self._lock._is_owned()
        if self.handles.get(handle.binding) is not handle:
            raise RuntimeError("stale test binding handle")
        state = self.states.get(handle.binding)
        if state is None:
            raise RuntimeError("missing test binding runtime")
        return project_binding_session_snapshot(state, handle=handle)

    def resident_runtime_state_locked(self, binding):
        assert self._lock._is_owned()
        return self.states.get(binding)

    def update_thread_metadata_locked(
        self,
        handle,
        *,
        expected_thread_id,
        current_thread_title,
    ):
        assert self._lock._is_owned()
        session = self.session_snapshot_locked(handle)
        if session.current_thread_id != expected_thread_id:
            return None
        state = self.states[handle.binding]
        apply_runtime_state_message(
            state,
            ThreadStateChanged(current_thread_title=current_thread_title),
        )
        return project_binding_session_snapshot(state, handle=handle)

    def replace(self, binding, state) -> None:
        assert self._lock._is_owned()
        self.states[binding] = state
        self.handles[binding] = self._new_handle(binding)


class AdapterNotificationControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(
        self,
        states,
        subscribers_for_thread,
        *,
        lock=None,
        binding_runtime=None,
        watchdog_effect=None,
        send_card_effect=None,
        dispatch_card_effect=None,
        interrupt_effect=None,
    ):
        lock = lock or threading.RLock()
        patches: list[dict[str, object]] = []
        sent_cards: list[tuple[str, str, bool]] = []
        watchdogs: list[tuple[str, str]] = []
        note_events = watchdogs
        updates: list[tuple[str, str]] = []
        flushes: list[tuple[str, str, bool]] = []
        plan_flushes: list[tuple[str, str]] = []
        interrupts: list[tuple[str, str]] = []
        finalizations: list[tuple[str, str, str, str]] = []
        resolved: list[dict[str, object]] = []

        binding_runtime = binding_runtime or _TestBindingRuntime(lock, states)

        def _schedule_watchdog(session) -> None:
            watchdogs.append(session.binding)
            if watchdog_effect is not None:
                watchdog_effect(session)

        def _open_initial_execution_page(
            session,
            parent_message_id: str,
            *,
            reply_in_thread: bool = False,
            reserved_message_id: str = "",
        ) -> InitialExecutionPageOpenResult:
            chat_id = session.binding[1]
            sent_cards.append((chat_id, parent_message_id, reply_in_thread))
            target = BindingExecutionTarget.from_session(session)
            if send_card_effect is not None:
                message_id = send_card_effect(chat_id, parent_message_id)
            else:
                message_id = reserved_message_id or "new-card"
            with lock:
                try:
                    current = binding_runtime.session_snapshot_locked(session.handle)
                except RuntimeError:
                    return InitialExecutionPageOpenResult(
                        status=InitialExecutionPageOpenStatus.STALE,
                        session=None,
                    )
                if not target.matches(current):
                    return InitialExecutionPageOpenResult(
                        status=InitialExecutionPageOpenStatus.STALE,
                        session=None,
                    )
                state = binding_runtime.resident_runtime_state_locked(
                    session.binding
                )
                assert state is not None
                if not message_id:
                    return InitialExecutionPageOpenResult(
                        status=InitialExecutionPageOpenStatus.REJECTED,
                        session=current,
                    )
                set_execution_page_state(
                    state,
                    current_message_id=message_id,
                )
                updated = binding_runtime.session_snapshot_locked(session.handle)
            return InitialExecutionPageOpenResult(
                status=InitialExecutionPageOpenStatus.ACTIVE,
                session=updated,
                message_id=message_id,
            )

        def _dispatch_card(
            chat_id,
            message_id,
            *,
            transcript,
            running,
            elapsed,
            cancelled,
            cursor_start,
            cursor_end,
        ) -> None:
            patches.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_text": transcript.reply_text(),
                    "running": running,
                    "elapsed": elapsed,
                    "cancelled": cancelled,
                    "cursor_start": cursor_start,
                    "cursor_end": cursor_end,
                }
            )
            if dispatch_card_effect is not None:
                dispatch_card_effect(message_id)

        def _interrupt(*, thread_id: str, turn_id: str) -> None:
            interrupts.append((thread_id, turn_id))
            if interrupt_effect is not None:
                interrupt_effect(thread_id, turn_id)

        runtime = AdapterNotificationRuntimeTransitions(
            lock=lock,
            binding_runtime=binding_runtime,
            turn_execution=TurnExecutionCoordinator(),
        )
        controller = AdapterNotificationController(
            runtime=runtime,
            thread_subscribers=lambda thread_id: subscribers_for_thread.get(thread_id, ()),
            effects=AdapterNotificationEffects(
                finalize_execution_from_terminal_signal=(
                    lambda session, *, thread_id, turn_id="": (
                        finalizations.append(
                            (
                                session.binding[0],
                                session.binding[1],
                                thread_id,
                                turn_id,
                            )
                        )
                        or True
                    )
                ),
                dispatch_execution_card_message=_dispatch_card,
                open_initial_execution_page=_open_initial_execution_page,
                schedule_mirror_watchdog=_schedule_watchdog,
                schedule_execution_card_update=(
                    lambda session: updates.append(session.binding)
                ),
                flush_execution_card=(
                    lambda session, immediate: flushes.append(
                        (*session.binding, immediate)
                    )
                ),
                flush_plan_card=(
                    lambda session: plan_flushes.append(session.binding)
                ),
                interrupt_running_turn=_interrupt,
                is_pre_send_error=lambda exc: str(exc) == "pre-send",
            ),
        )
        return controller, note_events, patches, sent_cards, watchdogs, updates, flushes, plan_flushes, interrupts, finalizations, resolved

    def test_generic_projection_ignores_server_request_resolved(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        controller, *_, resolved = self._make_controller(
            {binding: state},
            {},
        )

        controller.handle_notification("serverRequest/resolved", {"requestId": "req-1"})
        controller.handle_notification("unknown", {"noop": True})

        self.assertEqual(resolved, [])

    def test_upstream_notices_are_preserved_in_central_logs(self) -> None:
        controller, *_ = self._make_controller({}, {})
        notices = {
            "warning": {"threadId": "thread-1", "message": "skills trimmed"},
            "guardianWarning": {"threadId": "thread-1", "message": "review denied"},
            "deprecationNotice": {"summary": "old method", "details": "use the new method"},
            "configWarning": {"summary": "invalid config", "path": "/tmp/config.toml"},
        }

        with self.assertLogs("bot.adapter_notification_controller", level="WARNING") as captured:
            for method, params in notices.items():
                controller.handle_notification(method, params)

        joined = "\n".join(captured.output)
        for method, params in notices.items():
            self.assertIn(f"method={method}", joined)
            for value in params.values():
                self.assertIn(str(value), joined)

    def test_all_subscriber_routes_ignore_missing_exact_resident(self) -> None:
        binding = ("ou_user", "chat-1")
        (
            controller,
            note_events,
            patches,
            sent_cards,
            watchdogs,
            updates,
            flushes,
            plan_flushes,
            interrupts,
            finalizations,
            _,
        ) = self._make_controller(
            {},
            {"thread-1": (binding,)},
        )
        notifications = (
            (
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "error": {"message": "failed"},
                },
            ),
            (
                "thread/status/changed",
                {"threadId": "thread-1", "status": {"type": "idle"}},
            ),
            ("thread/closed", {"threadId": "thread-1"}),
            (
                "thread/name/updated",
                {"threadId": "thread-1", "threadName": "new"},
            ),
            (
                "thread/goal/updated",
                {"threadId": "thread-1", "goal": {"objective": "goal"}},
            ),
            ("thread/goal/cleared", {"threadId": "thread-1"}),
            (
                "turn/started",
                {"threadId": "thread-1", "turn": {"id": "turn-1"}},
            ),
            (
                "turn/plan/updated",
                {"threadId": "thread-1", "turnId": "turn-1", "plan": []},
            ),
            (
                "item/started",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "commandExecution"},
                },
            ),
            (
                "item/agentMessage/delta",
                {"threadId": "thread-1", "turnId": "turn-1", "delta": "x"},
            ),
            (
                "item/commandExecution/outputDelta",
                {"threadId": "thread-1", "turnId": "turn-1", "delta": "x"},
            ),
            (
                "item/fileChange/patchUpdated",
                {"threadId": "thread-1", "turnId": "turn-1", "itemId": "file-1", "changes": []},
            ),
            (
                "item/completed",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": "x"},
                },
            ),
            (
                "turn/completed",
                {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            ),
        )

        for method, params in notifications:
            with self.subTest(method=method):
                controller.handle_notification(method, params)

        self.assertEqual(note_events, [])
        self.assertEqual(patches, [])
        self.assertEqual(sent_cards, [])
        self.assertEqual(watchdogs, [])
        self.assertEqual(updates, [])
        self.assertEqual(flushes, [])
        self.assertEqual(plan_flushes, [])
        self.assertEqual(interrupts, [])
        self.assertEqual(finalizations, [])

    def test_handle_thread_name_updated_updates_all_bound_subscribers(self) -> None:
        binding_a = ("ou_user", "chat-a")
        binding_b = ("ou_user", "chat-b")
        state_a = self._make_state()
        state_b = self._make_state()
        state_a["current_thread_id"] = "thread-1"
        state_b["current_thread_id"] = "thread-1"
        state_a["current_thread_title"] = "old-a"
        state_b["current_thread_title"] = "old-b"

        controller, note_events, *_ = self._make_controller(
            {binding_a: state_a, binding_b: state_b},
            {"thread-1": (binding_a, binding_b)},
        )

        controller.handle_thread_name_updated({"threadId": "thread-1", "threadName": "new-title"})

        self.assertEqual(note_events, [binding_a, binding_b])
        self.assertEqual(state_a["current_thread_title"], "new-title")
        self.assertEqual(state_b["current_thread_title"], "new-title")

    def test_name_update_preserves_exact_p2p_identity_when_group_coexists(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        store = ChatBindingStore(data_dir)
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=store,
        )
        p2p_binding = ("ou_user", "chat-shared")
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "chat-shared")
        with lock:
            p2p_state = manager._get_or_create_runtime_state_locked(p2p_binding)
            p2p_state["current_thread_id"] = "thread-1"
            p2p_state["current_thread_title"] = "p2p-before"
            p2p_state["feishu_runtime_state"] = FEISHU_RUNTIME_DETACHED
            group_state = manager._get_or_create_runtime_state_locked(group_binding)
            group_state["current_thread_id"] = "thread-1"
            group_state["current_thread_title"] = "group-before"
            group_state["feishu_runtime_state"] = FEISHU_RUNTIME_DETACHED
        self.assertEqual(
            manager.resolve_session(*p2p_binding).binding,
            group_binding,
        )

        controller, note_events, *_ = self._make_controller(
            {},
            {"thread-1": (p2p_binding,)},
            lock=lock,
            binding_runtime=manager,
        )

        controller.handle_thread_name_updated(
            {"threadId": "thread-1", "threadName": "p2p-after"}
        )

        with lock:
            p2p_after = manager.resident_runtime_state_locked(p2p_binding)
            group_after = manager.resident_runtime_state_locked(group_binding)
            assert p2p_after is not None
            assert group_after is not None
            self.assertEqual(p2p_after["current_thread_title"], "p2p-after")
            self.assertEqual(group_after["current_thread_title"], "group-before")
        stored_p2p = store.load(p2p_binding)
        assert stored_p2p is not None
        self.assertEqual(stored_p2p["current_thread_title"], "p2p-after")
        self.assertIsNone(store.load(group_binding))
        self.assertEqual(note_events, [p2p_binding])

    def test_same_chat_execution_delta_mutates_only_exact_p2p_subscriber(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        lock = threading.RLock()
        _leases, manager = make_binding_runtime(
            data_dir=data_dir,
            lock=lock,
            chat_binding_store=ChatBindingStore(data_dir),
        )
        p2p_binding = ("ou_user", "chat-shared")
        group_binding = (GROUP_SHARED_BINDING_OWNER_ID, "chat-shared")
        with lock:
            p2p_state = manager._get_or_create_runtime_state_locked(p2p_binding)
            group_state = manager._get_or_create_runtime_state_locked(group_binding)
            for state in (p2p_state, group_state):
                state["current_thread_id"] = "thread-1"
                state["current_turn_id"] = "turn-1"
                set_execution_page_state(state, current_message_id="card-1")
                state["running"] = True
        self.assertEqual(
            manager.resolve_session(*p2p_binding).binding,
            group_binding,
        )
        controller, note_events, _, _, watchdogs, updates, *_ = (
            self._make_controller(
                {},
                {"thread-1": (p2p_binding,)},
                lock=lock,
                binding_runtime=manager,
            )
        )

        controller.handle_agent_message_delta(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "delta": "p2p-only",
            }
        )

        self.assertEqual(
            p2p_state["execution_transcript"].reply_text(),
            "p2p-only",
        )
        self.assertEqual(group_state["execution_transcript"].reply_text(), "")
        self.assertEqual(note_events, [p2p_binding])
        self.assertEqual(watchdogs, [p2p_binding])
        self.assertEqual(updates, [p2p_binding])

    def test_replacement_during_watchdog_effect_blocks_delta_projection(
        self,
    ) -> None:
        binding = ("ou_user", "chat-1")
        lock = threading.RLock()
        original = self._make_state()
        original["current_thread_id"] = "thread-1"
        original["current_turn_id"] = "turn-1"
        set_execution_page_state(original, current_message_id="card-1")
        original["running"] = True
        replacement = self._make_state()
        replacement["current_thread_id"] = "thread-1"
        replacement["current_turn_id"] = "turn-1"
        set_execution_page_state(replacement, current_message_id="card-1")
        replacement["running"] = True
        authority = _TestBindingRuntime(lock, {binding: original})

        def replace_runtime(_session) -> None:
            with lock:
                authority.replace(binding, replacement)

        controller, note_events, _, _, watchdogs, updates, *_ = (
            self._make_controller(
                {},
                {"thread-1": (binding,)},
                lock=lock,
                binding_runtime=authority,
                watchdog_effect=replace_runtime,
            )
        )

        controller.handle_agent_message_delta(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "delta": "stale",
            }
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(watchdogs, [binding])
        self.assertEqual(updates, [])
        self.assertEqual(replacement["execution_transcript"].reply_text(), "")

    def test_replacement_during_card_send_blocks_card_commit(self) -> None:
        binding = ("ou_user", "chat-1")
        lock = threading.RLock()
        original = self._make_state()
        original["current_thread_id"] = "thread-1"
        replacement = self._make_state()
        replacement["current_thread_id"] = "thread-1"
        replacement["current_turn_id"] = "turn-1"
        set_execution_page_state(replacement, current_message_id="replacement-card")
        replacement["running"] = True
        authority = _TestBindingRuntime(lock, {binding: original})

        def replace_runtime(_chat_id: str, _parent_message_id: str) -> str:
            with lock:
                authority.replace(binding, replacement)
            return "orphan-card"

        controller, _, _, sent_cards, _, updates, *_ = self._make_controller(
            {},
            {"thread-1": (binding,)},
            lock=lock,
            binding_runtime=authority,
            send_card_effect=replace_runtime,
        )

        controller.handle_turn_started(
            {"threadId": "thread-1", "turn": {"id": "turn-1"}}
        )

        self.assertEqual(sent_cards, [("chat-1", "", False)])
        self.assertEqual(updates, [])
        self.assertEqual(
            replacement["execution_pages"].current_message_id,
            "replacement-card",
        )
        self.assertEqual(replacement["current_turn_id"], "turn-1")

    def test_replacement_during_previous_card_effect_blocks_new_card_send(
        self,
    ) -> None:
        binding = ("ou_user", "chat-1")
        lock = threading.RLock()
        original = self._make_state()
        original["current_thread_id"] = "thread-1"
        set_execution_page_state(original, current_message_id="old-card")
        replacement = self._make_state()
        replacement["current_thread_id"] = "thread-1"
        replacement["current_turn_id"] = "replacement-turn"
        set_execution_page_state(replacement, current_message_id="replacement-card")
        replacement["running"] = True
        authority = _TestBindingRuntime(lock, {binding: original})

        def replace_runtime(_message_id: str) -> None:
            with lock:
                authority.replace(binding, replacement)

        controller, _, patches, sent_cards, _, updates, *_ = (
            self._make_controller(
                {},
                {"thread-1": (binding,)},
                lock=lock,
                binding_runtime=authority,
                dispatch_card_effect=replace_runtime,
            )
        )

        controller.handle_turn_started(
            {"threadId": "thread-1", "turn": {"id": "turn-1"}}
        )

        self.assertEqual([patch["message_id"] for patch in patches], ["old-card"])
        self.assertEqual(sent_cards, [])
        self.assertEqual(updates, [])
        self.assertEqual(
            replacement["execution_pages"].current_message_id,
            "replacement-card",
        )
        self.assertEqual(replacement["current_turn_id"], "replacement-turn")

    def test_replacement_during_interrupt_blocks_cancel_confirmation(self) -> None:
        binding = ("ou_user", "chat-1")
        lock = threading.RLock()
        original = self._make_state()
        original["current_thread_id"] = "thread-1"
        set_execution_page_state(original, current_message_id="card-1")
        original["running"] = True
        original["awaiting_local_turn_started"] = True
        original["pending_cancel"] = True
        replacement = self._make_state()
        replacement["current_thread_id"] = "thread-1"
        replacement["current_turn_id"] = "turn-1"
        set_execution_page_state(replacement, current_message_id="card-1")
        replacement["running"] = True
        replacement["pending_cancel"] = True
        authority = _TestBindingRuntime(lock, {binding: original})

        def replace_runtime(_thread_id: str, _turn_id: str) -> None:
            with lock:
                authority.replace(binding, replacement)

        controller, _, _, _, _, updates, _, _, interrupts, *_ = (
            self._make_controller(
                {},
                {"thread-1": (binding,)},
                lock=lock,
                binding_runtime=authority,
                interrupt_effect=replace_runtime,
            )
        )

        controller.handle_turn_started(
            {"threadId": "thread-1", "turn": {"id": "turn-1"}}
        )

        self.assertEqual(interrupts, [("thread-1", "turn-1")])
        self.assertEqual(updates, [])
        self.assertTrue(replacement["pending_cancel"])
        self.assertFalse(replacement["cancelled"])

    def test_turn_started_auto_cancel_restores_pending_only_for_pre_send(self) -> None:
        for failure, expected_pending in (
            (RuntimeError("pre-send"), True),
            (RuntimeError("unknown"), False),
            (None, False),
        ):
            with self.subTest(
                failure=str(failure) if failure is not None else "success"
            ):
                binding = ("ou_user", "chat-1")
                state = self._make_state()
                state["current_thread_id"] = "thread-1"
                state["running"] = True
                state["awaiting_local_turn_started"] = True
                state["pending_cancel"] = True
                set_execution_page_state(state, current_message_id="card-1")

                def interrupt_effect(_thread_id: str, _turn_id: str) -> None:
                    if failure is not None:
                        raise failure

                controller, _, _, _, _, _, _, _, interrupts, *_ = (
                    self._make_controller(
                        {binding: state},
                        {"thread-1": (binding,)},
                        interrupt_effect=interrupt_effect,
                    )
                )

                with self.assertLogs(
                    "bot.adapter_notification_controller",
                    level="ERROR",
                ) if failure is not None else self.subTest():
                    controller.handle_turn_started(
                        {"threadId": "thread-1", "turn": {"id": "turn-1"}}
                    )

                self.assertEqual(interrupts, [("thread-1", "turn-1")])
                self.assertEqual(state["current_turn_id"], "turn-1")
                self.assertEqual(state["pending_cancel"], expected_pending)
                self.assertFalse(state["cancelled"])
                if expected_pending:
                    controller.handle_turn_started(
                        {"threadId": "thread-1", "turn": {"id": "turn-1"}}
                    )
                    self.assertEqual(interrupts, [("thread-1", "turn-1")])
                    self.assertTrue(state["pending_cancel"])

    def test_handle_thread_goal_updated_projects_goal_state(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"

        controller, note_events, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_goal_updated(
            {
                "threadId": "thread-1",
                "goal": {
                    "objective": "ship goal support",
                    "status": "active",
                    "tokenBudget": 200,
                    "tokensUsed": 12,
                    "timeUsedSeconds": 34,
                    "createdAt": 1712476800,
                    "updatedAt": 1712476801,
                },
            }
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(state["goal_objective"], "ship goal support")
        self.assertEqual(state["goal_status"], "active")
        self.assertEqual(state["goal_token_budget"], 200)
        self.assertEqual(state["goal_tokens_used"], 12)
        self.assertEqual(state["goal_time_used_seconds"], 34)

    def test_handle_thread_goal_cleared_resets_goal_projection(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["goal_objective"] = "stale goal"
        state["goal_status"] = "paused"
        state["goal_token_budget"] = 200
        state["goal_tokens_used"] = 12
        state["goal_time_used_seconds"] = 34
        state["goal_created_at"] = 1712476800
        state["goal_updated_at"] = 1712476801

        controller, note_events, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_goal_cleared({"threadId": "thread-1"})

        self.assertEqual(note_events, [binding])
        self.assertEqual(state["goal_objective"], "")
        self.assertEqual(state["goal_status"], "")
        self.assertIsNone(state["goal_token_budget"])
        self.assertEqual(state["goal_tokens_used"], 0)
        self.assertEqual(state["goal_time_used_seconds"], 0)

    def test_handle_turn_started_patches_previous_card_and_assigns_new_card(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="old-card")
        state["execution_transcript"].set_reply_text("old reply")
        state["started_at"] = 2.0

        (
            controller,
            note_events,
            patches,
            sent_cards,
            watchdogs,
            updates,
            *_,
        ) = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-2"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(
            patches[0]["message_id"],
            "old-card",
        )
        self.assertEqual(patches[0]["reply_text"], "old reply")
        self.assertEqual(sent_cards, [("chat-1", "", False)])
        self.assertEqual(state["execution_pages"].current_message_id, "new-card")
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(watchdogs, [binding])
        self.assertEqual(updates, [binding])

    def test_handle_turn_started_sends_execution_card_to_each_subscriber(self) -> None:
        binding_a = ("ou_user", "chat-a")
        binding_b = ("ou_user", "chat-b")
        state_a = self._make_state()
        state_b = self._make_state()
        state_a["current_thread_id"] = "thread-1"
        state_b["current_thread_id"] = "thread-1"
        set_execution_page_state(state_a, current_message_id="card-a")
        state_a["running"] = True
        state_a["awaiting_local_turn_started"] = True

        (
            controller,
            note_events,
            _patches,
            sent_cards,
            watchdogs,
            updates,
            *_,
        ) = self._make_controller(
            {binding_a: state_a, binding_b: state_b},
            {"thread-1": (binding_a, binding_b)},
        )

        controller.handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(note_events, [binding_a, binding_b])
        self.assertEqual(sent_cards, [("chat-b", "", False)])
        self.assertEqual(state_a["execution_pages"].current_message_id, "card-a")
        self.assertEqual(state_b["execution_pages"].current_message_id, "new-card")
        self.assertEqual(state_a["current_turn_id"], "turn-1")
        self.assertEqual(state_b["current_turn_id"], "turn-1")
        self.assertEqual(watchdogs, [binding_a, binding_b])
        self.assertEqual(updates, [binding_a, binding_b])

    def test_handle_turn_started_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="queued-card")
        state["current_turn_id"] = "turn-2"
        state["running"] = True
        state["awaiting_local_turn_started"] = True

        (
            controller,
            note_events,
            patches,
            sent_cards,
            watchdogs,
            updates,
            *_,
        ) = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_turn_started({"threadId": "thread-1", "turn": {"id": "turn-1"}})

        self.assertEqual(note_events, [])
        self.assertEqual(patches, [])
        self.assertEqual(sent_cards, [])
        self.assertEqual(watchdogs, [])
        self.assertEqual(updates, [])
        self.assertEqual(
            state["execution_pages"].current_message_id,
            "queued-card",
        )
        self.assertEqual(state["current_turn_id"], "turn-2")

    def test_turn_scoped_notifications_without_turn_id_do_not_touch_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="queued-card")
        state["current_turn_id"] = "turn-2"
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "compact"

        controller, note_events, patches, sent_cards, watchdogs, updates, flushes, plan_flushes, _, finalizations, _ = (
            self._make_controller({binding: state}, {"thread-1": (binding,)})
        )

        cases = [
            lambda: controller.handle_turn_started({"threadId": "thread-1", "turn": {}}),
            lambda: controller.handle_turn_plan_updated({"threadId": "thread-1", "plan": [{"step": "old"}]}),
            lambda: controller.handle_item_started(
                {"threadId": "thread-1", "item": {"type": "contextCompaction", "id": "compact-1"}}
            ),
            lambda: controller.handle_agent_message_delta({"threadId": "thread-1", "delta": "old"}),
            lambda: controller.handle_command_delta({"threadId": "thread-1", "delta": "old stdout"}),
            lambda: controller.handle_file_change_patch_updated({"threadId": "thread-1", "changes": []}),
            lambda: controller.handle_item_completed(
                {"threadId": "thread-1", "item": {"type": "agentMessage", "text": "old final text"}}
            ),
            lambda: controller.handle_turn_completed({"threadId": "thread-1", "turn": {"status": "completed"}}),
        ]
        for index, call in enumerate(cases):
            with self.subTest(index=index):
                call()

        self.assertEqual(note_events, [])
        self.assertEqual(patches, [])
        self.assertEqual(sent_cards, [])
        self.assertEqual(watchdogs, [])
        self.assertEqual(updates, [])
        self.assertEqual(flushes, [])
        self.assertEqual(plan_flushes, [])
        self.assertEqual(finalizations, [])
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(
            state["execution_pages"].current_message_id,
            "queued-card",
        )
        self.assertTrue(state["running"])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["execution_transcript"].process_text(), "")

    def test_thread_level_events_for_stale_subscription_do_not_refresh_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-2"
        state["current_thread_title"] = "current"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="card-2")
        state["running"] = True
        state["goal_objective"] = "keep"
        state["goal_status"] = "active"

        controller, note_events, _, _, _, updates, flushes, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "idle"}})
        controller.handle_thread_closed({"threadId": "thread-1"})
        controller.handle_thread_name_updated({"threadId": "thread-1", "threadName": "stale"})
        controller.handle_thread_goal_updated({"threadId": "thread-1", "goal": {"objective": "stale"}})
        controller.handle_thread_goal_cleared({"threadId": "thread-1"})

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(flushes, [])
        self.assertEqual(finalizations, [])
        self.assertEqual(state["current_thread_id"], "thread-2")
        self.assertEqual(state["current_thread_title"], "current")
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(state["execution_pages"].current_message_id, "card-2")
        self.assertEqual(state["goal_objective"], "keep")
        self.assertEqual(state["goal_status"], "active")

    def test_handle_thread_status_changed_ignores_idle_while_waiting_for_turn_started(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["awaiting_attach_status_settle"] = True
        state["current_turn_id"] = "turn-1"

        controller, note_events, _, _, _, updates, flushes, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "idle"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [])
        self.assertEqual(flushes, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_pages"].current_message_id, "card-1")
        self.assertTrue(state["awaiting_local_turn_started"])

    def test_handle_thread_status_changed_ignores_idle_while_turn_id_unbound(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="compact-card")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["awaiting_attach_status_settle"] = False
        state["current_turn_id"] = ""

        controller, note_events, _, _, _, updates, flushes, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "idle"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [])
        self.assertEqual(flushes, [])
        self.assertEqual(updates, [])
        self.assertEqual(
            state["execution_pages"].current_message_id,
            "compact-card",
        )
        self.assertTrue(state["running"])
        self.assertTrue(state["awaiting_local_turn_started"])

    def test_handle_thread_status_changed_active_does_not_clear_waiting_for_turn_started(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["awaiting_attach_status_settle"] = True
        state["current_turn_id"] = "turn-1"

        controller, note_events, _, _, _, updates, flushes, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "active"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [])
        self.assertEqual(flushes, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_pages"].current_message_id, "card-1")
        self.assertTrue(state["awaiting_local_turn_started"])

    def test_handle_thread_closed_ignores_close_while_waiting_for_turn_started(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["awaiting_attach_status_settle"] = True
        state["current_turn_id"] = "turn-1"

        controller, note_events, _, _, _, _, _, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_closed({"threadId": "thread-1"})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [])
        self.assertEqual(state["execution_pages"].current_message_id, "card-1")
        self.assertTrue(state["awaiting_local_turn_started"])

    def test_item_delta_does_not_bind_unstarted_execution_to_stale_turn(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="compact-card")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "prompt"
        state["current_turn_id"] = ""

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_agent_message_delta({"threadId": "thread-1", "turnId": "old-turn", "delta": "old"})
        controller.handle_item_started(
            {
                "threadId": "thread-1",
                "turnId": "old-turn",
                "item": {"type": "contextCompaction", "id": "compact-1"},
            }
        )

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(state["execution_transcript"].process_text(), "")

    def test_context_compaction_item_started_binds_unstarted_compact_anchor(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="compact-card")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "compact"
        state["current_turn_id"] = ""

        controller, note_events, _, _, watchdogs, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_item_started(
            {
                "threadId": "thread-1",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-1"},
            }
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(state["current_turn_id"], "compact-turn")
        self.assertFalse(state["awaiting_local_turn_started"])
        self.assertIn("上下文压缩", state["execution_transcript"].process_text())
        self.assertEqual(watchdogs, [binding])
        self.assertEqual(updates, [binding])

    def test_context_compaction_item_started_does_not_bind_unstarted_prompt_anchor(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        set_execution_page_state(state, current_message_id="prompt-card")
        state["running"] = True
        state["awaiting_local_turn_started"] = True
        state["current_execution_kind"] = "prompt"
        state["current_turn_id"] = ""

        controller, note_events, _, _, watchdogs, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_item_started(
            {
                "threadId": "thread-1",
                "turnId": "compact-turn",
                "item": {"type": "contextCompaction", "id": "compact-1"},
            }
        )

        self.assertEqual(note_events, [])
        self.assertEqual(state["current_turn_id"], "")
        self.assertTrue(state["awaiting_local_turn_started"])
        self.assertEqual(state["execution_transcript"].process_text(), "")
        self.assertEqual(watchdogs, [])
        self.assertEqual(updates, [])

    def test_handle_turn_completed_delegates_terminal_finalize(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["running"] = True

        controller, note_events, _, _, _, _, _, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [("ou_user", "chat-1", "thread-1", "turn-1")])

    def test_handle_turn_completed_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True
        state["awaiting_local_turn_started"] = True

        controller, note_events, _, _, _, _, _, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(note_events, [])
        self.assertEqual(finalizations, [])
        self.assertEqual(state["current_turn_id"], "turn-2")
        self.assertEqual(
            state["execution_pages"].current_message_id,
            "queued-card",
        )
        self.assertTrue(state["running"])

    def test_handle_agent_message_delta_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_agent_message_delta({"threadId": "thread-1", "turnId": "turn-1", "delta": "old"})

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "")

    def test_handle_command_delta_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_command_delta({"threadId": "thread-1", "turnId": "turn-1", "delta": "old stdout"})

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_transcript"].process_text(), "")

    def test_handle_file_change_patch_updated_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_file_change_patch_updated({"threadId": "thread-1", "turnId": "turn-1", "itemId": "file-1", "changes": []})

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_transcript"].process_text(), "")

    def test_handle_item_started_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_item_started(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "commandExecution", "command": "echo old", "cwd": "/tmp"},
            }
        )

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_transcript"].process_text(), "")

    def test_handle_item_completed_ignores_stale_turn_for_current_execution(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-2"
        set_execution_page_state(state, current_message_id="queued-card")
        state["running"] = True

        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_item_completed(
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {"type": "agentMessage", "text": "old final text"},
            }
        )

        self.assertEqual(note_events, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_transcript"].reply_text(), "")

    def test_agent_completion_phase_and_text_shape_control_terminal_evidence(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state.update(current_thread_id="thread-1", current_turn_id="turn-1", running=True)
        set_execution_page_state(state, current_message_id="card-1")
        controller, note_events, _, _, _, updates, *_ = self._make_controller(
            {binding: state}, {"thread-1": (binding,)}
        )
        items = (
            ({"type": "agentMessage", "phase": "commentary", "text": "阶段说明"}, None),
            ({"type": "agentMessage"}, None),
            ({"type": "agentMessage", "text": ""}, ("agent", "")),
            (
                {
                    "id": "agent-final-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "最终答案",
                },
                ("agent", "最终答案"),
            ),
        )

        for item, expected in items:
            controller.handle_item_completed(
                {"threadId": "thread-1", "turnId": "turn-1", "item": item}
            )
            self.assertEqual(
                state["execution_transcript"].terminal_reply_evidence(), expected
            )

        controller.handle_item_completed(
            {"threadId": "thread-1", "turnId": "turn-1", "item": items[-1][0]}
        )
        transcript = state["execution_transcript"]
        coordinate = transcript.terminal_agent_reply_coordinate()
        assert coordinate is not None
        self.assertEqual(coordinate.item_id, "agent-final-1")
        self.assertEqual(note_events, [binding] * 5)
        self.assertEqual(updates, [binding] * 5)
        self.assertEqual(transcript.reply_text(), "阶段说明\n\n最终答案")

    def test_live_continuation_evidence_survives_missing_started_or_completed(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state.update(current_thread_id="thread-1", current_turn_id="turn-1", running=True)
        set_execution_page_state(state, current_message_id="card-1")
        controller, *_ = self._make_controller({binding: state}, {"thread-1": (binding,)})
        transcript = state["execution_transcript"]
        for item_type in (
            "collabAgentToolCall", "dynamicToolCall", "reasoning", "plan",
            "imageView", "sleep", "enteredReviewMode", "exitedReviewMode",
        ):
            item = {"type": item_type, **({"text": "plan"} if item_type == "plan" else {})}
            for method in (controller.handle_item_started, controller.handle_item_completed):
                transcript.reconcile_current_assistant_text("阶段说明")
                method({"threadId": "thread-1", "turnId": "turn-1", "item": item})
                self.assertIsNone(transcript.terminal_reply_evidence())

        transcript.reconcile_current_assistant_text("阶段说明")
        controller.handle_command_delta(
            {"threadId": "thread-1", "turnId": "turn-1", "delta": "output"}
        )
        self.assertIsNone(transcript.terminal_reply_evidence())

    def test_handle_turn_completed_finalizes_each_subscriber(self) -> None:
        binding_a = ("ou_user", "chat-a")
        binding_b = ("ou_user", "chat-b")
        state_a = self._make_state()
        state_b = self._make_state()
        for state in (state_a, state_b):
            state["current_thread_id"] = "thread-1"
            state["current_turn_id"] = "turn-1"
            state["running"] = True

        controller, note_events, _, _, _, _, _, _, _, finalizations, _ = self._make_controller(
            {binding_a: state_a, binding_b: state_b},
            {"thread-1": (binding_a, binding_b)},
        )

        controller.handle_turn_completed({"threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}})

        self.assertEqual(note_events, [binding_a, binding_b])
        self.assertEqual(
            finalizations,
            [
                ("ou_user", "chat-a", "thread-1", "turn-1"),
                ("ou_user", "chat-b", "thread-1", "turn-1"),
            ],
        )

    def test_handle_thread_status_changed_system_error_waits_for_error_or_turn_completed(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["running"] = True

        controller, note_events, _, _, _, updates, flushes, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "systemError"}})

        self.assertEqual(note_events, [binding])
        self.assertEqual(finalizations, [])
        self.assertEqual(flushes, [])
        self.assertEqual(updates, [])
        self.assertEqual(state["execution_pages"].current_message_id, "card-1")
        self.assertEqual(state["current_turn_id"], "turn-1")

    def test_system_error_followed_by_error_and_turn_completed_preserves_failure_text(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        set_execution_page_state(state, current_message_id="card-1")
        state["running"] = True

        controller, note_events, _, _, _, updates, _, _, _, finalizations, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_thread_status_changed({"threadId": "thread-1", "status": {"type": "systemError"}})
        controller.handle_notification(
            "error",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
                "error": {
                    "message": "Missing environment variable: `CODEX_ZH_API_KEY`.",
                },
            },
        )
        controller.handle_turn_completed(
            {
                "threadId": "thread-1",
                "turn": {
                    "id": "turn-1",
                    "status": "failed",
                    "error": {"message": "Missing environment variable: `CODEX_ZH_API_KEY`."},
                },
            }
        )

        self.assertEqual(
            note_events,
            [binding, binding, binding],
        )
        self.assertEqual(
            updates,
            [binding],
        )
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "Missing environment variable: `CODEX_ZH_API_KEY`.",
        )
        self.assertEqual(finalizations, [("ou_user", "chat-1", "thread-1", "turn-1")])

    def test_handle_error_notification_uses_non_retry_error_as_fallback_reply(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["running"] = True

        controller, note_events, _, _, _, updates, _, _, _, _, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_notification(
            "error",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": False,
                "error": {
                    "message": "provider unavailable",
                    "additionalDetails": "timeout while contacting upstream",
                },
            },
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(updates, [binding])
        self.assertEqual(
            state["execution_transcript"].reply_text(),
            "provider unavailable\ntimeout while contacting upstream",
        )

    def test_handle_error_notification_records_retry_message_in_process_panel(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_turn_id"] = "turn-1"
        state["running"] = True

        controller, note_events, _, _, _, updates, _, _, _, _, _ = self._make_controller(
            {binding: state},
            {"thread-1": (binding,)},
        )

        controller.handle_notification(
            "error",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "willRetry": True,
                "error": {
                    "message": "temporary transport error",
                },
            },
        )

        self.assertEqual(note_events, [binding])
        self.assertEqual(updates, [binding])
        self.assertEqual(state["execution_transcript"].reply_text(), "")
        self.assertEqual(
            state["execution_transcript"].process_text(),
            "\n[重试中] temporary transport error\n",
        )
