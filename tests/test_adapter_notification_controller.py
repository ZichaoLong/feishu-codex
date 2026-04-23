import pathlib
import tempfile
import threading
import unittest

from bot.adapter_notification_controller import AdapterNotificationController
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_state import ExecutionStateChanged, apply_runtime_state_message
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from bot.turn_execution_coordinator import TurnExecutionCoordinator


class AdapterNotificationControllerTests(unittest.TestCase):
    def _make_state(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_sandbox="workspace-write",
            default_collaboration_mode="default",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        return manager.build_default_runtime_state()

    def _make_controller(self, states, subscribers_for_thread):
        lock = threading.RLock()
        note_events: list[tuple[str, str]] = []
        patches: list[dict[str, object]] = []
        sent_cards: list[tuple[str, str, bool]] = []
        watchdogs: list[tuple[str, str]] = []
        updates: list[tuple[str, str]] = []
        flushes: list[tuple[str, str, bool]] = []
        plan_flushes: list[tuple[str, str]] = []
        interrupts: list[tuple[str, str]] = []
        finalizations: list[tuple[str, str, str, str]] = []
        resolved: list[dict[str, object]] = []

        def _cancel_mirror_watchdog_locked(state) -> None:
            timer = state["mirror_watchdog_timer"]
            if timer is not None:
                timer.cancel()
            apply_runtime_state_message(
                state,
                ExecutionStateChanged(
                    mirror_watchdog_timer=None,
                    bump_mirror_watchdog_generation=True,
                ),
            )

        controller = AdapterNotificationController(
            lock=lock,
            turn_execution=TurnExecutionCoordinator(),
            thread_subscribers=lambda thread_id: subscribers_for_thread.get(thread_id, ()),
            get_runtime_state=lambda sender_id, chat_id: states[(sender_id, chat_id)],
            note_runtime_event=lambda sender_id, chat_id: note_events.append((sender_id, chat_id)),
            apply_runtime_state_message_locked=apply_runtime_state_message,
            apply_persisted_runtime_state_message_locked=lambda binding, state, message: apply_runtime_state_message(
                state,
                message,
            ),
            cancel_mirror_watchdog_locked=_cancel_mirror_watchdog_locked,
            finalize_execution_from_terminal_signal=lambda sender_id, chat_id, *, thread_id, turn_id="": (
                finalizations.append((sender_id, chat_id, thread_id, turn_id)) or True
            ),
            patch_execution_card_message=lambda message_id, *, transcript, running, elapsed, cancelled: patches.append(
                {
                    "message_id": message_id,
                    "reply_text": transcript.reply_text(),
                    "running": running,
                    "elapsed": elapsed,
                    "cancelled": cancelled,
                }
            )
            or True,
            send_execution_card=lambda chat_id, parent_message_id, *, reply_in_thread=False: sent_cards.append(
                (chat_id, parent_message_id, reply_in_thread)
            )
            or "new-card",
            schedule_mirror_watchdog=lambda sender_id, chat_id: watchdogs.append((sender_id, chat_id)),
            schedule_execution_card_update=lambda sender_id, chat_id: updates.append((sender_id, chat_id)),
            flush_execution_card=lambda sender_id, chat_id, immediate=False: flushes.append(
                (sender_id, chat_id, immediate)
            ),
            flush_plan_card=lambda sender_id, chat_id: plan_flushes.append((sender_id, chat_id)),
            interrupt_running_turn=lambda *, thread_id, turn_id: interrupts.append((thread_id, turn_id)),
            on_server_request_resolved=lambda params: resolved.append(params),
        )
        return controller, note_events, patches, sent_cards, watchdogs, updates, flushes, plan_flushes, interrupts, finalizations, resolved

    def test_handle_notification_routes_server_request_resolved(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        controller, *_, resolved = self._make_controller(
            {binding: state},
            {},
        )

        controller.handle_notification("serverRequest/resolved", {"requestId": "req-1"})
        controller.handle_notification("unknown", {"noop": True})

        self.assertEqual(resolved, [{"requestId": "req-1"}])

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

    def test_handle_turn_started_patches_previous_card_and_assigns_new_card(self) -> None:
        binding = ("ou_user", "chat-1")
        state = self._make_state()
        state["current_thread_id"] = "thread-1"
        state["current_message_id"] = "old-card"
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
        self.assertEqual(state["current_message_id"], "new-card")
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
        state_a["current_message_id"] = "card-a"
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
        self.assertEqual(state_a["current_message_id"], "card-a")
        self.assertEqual(state_b["current_message_id"], "new-card")
        self.assertEqual(state_a["current_turn_id"], "turn-1")
        self.assertEqual(state_b["current_turn_id"], "turn-1")
        self.assertEqual(watchdogs, [binding_a, binding_b])
        self.assertEqual(updates, [binding_a, binding_b])

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
