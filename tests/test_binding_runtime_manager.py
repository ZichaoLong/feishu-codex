import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace

from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_state import ThreadStateChanged
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_lease_registry import ThreadLeaseRegistry


class BindingRuntimeManagerTests(unittest.TestCase):
    def _make_manager(
        self,
        *,
        is_group_chat=None,
        data_dir: pathlib.Path | None = None,
    ) -> BindingRuntimeManager:
        if data_dir is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            data_dir = pathlib.Path(tempdir.name)
        return BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_sandbox="workspace-write",
            default_collaboration_mode="default",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_lease_registry=ThreadLeaseRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=is_group_chat or (lambda chat_id, message_id: False),
        )

    def _attach_binding(
        self,
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
        *,
        thread_id: str = "thread-1",
        thread_title: str = "Demo",
        working_dir: str = "/tmp/project",
        acquire_write_owner: bool = True,
        acquire_interaction_owner: bool = True,
    ):
        state = manager.resolve_runtime_binding(*binding).state
        with manager._lock:
            state["working_dir"] = working_dir
            state["current_thread_id"] = thread_id
            state["current_thread_title"] = thread_title
            state["current_thread_runtime_state"] = "attached"
            manager.subscribe_thread_locked(binding, thread_id)
            if acquire_write_owner:
                manager.acquire_thread_write_lease_locked(binding, thread_id)
            if acquire_interaction_owner:
                manager.acquire_interaction_lease_for_binding(binding, thread_id)
            manager.sync_stored_binding_locked(binding, state)
        return state

    def test_resolve_runtime_binding_reuses_existing_group_binding(self) -> None:
        manager = self._make_manager(is_group_chat=lambda chat_id, message_id: bool(message_id))

        first = manager.resolve_runtime_binding("ou-user-1", "chat-group", "m-group")
        second = manager.resolve_runtime_binding("ou-user-2", "chat-group")

        self.assertEqual(first.binding, ("__group__", "chat-group"))
        self.assertEqual(second.binding, ("__group__", "chat-group"))
        self.assertIs(first.state, second.state)

    def test_hydrate_stored_bindings_recovers_subscription_and_owners(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        store = ChatBindingStore(data_dir)
        binding = ("ou-user", "chat-1")
        store.save(
            binding,
            {
                "working_dir": "/tmp/project",
                "current_thread_id": "thread-1",
                "current_thread_title": "Demo",
                "current_thread_runtime_state": "attached",
                "current_thread_write_owner_thread_id": "thread-1",
                "approval_policy": "never",
                "sandbox": "danger-full-access",
                "collaboration_mode": "plan",
            },
        )
        manager = self._make_manager(data_dir=data_dir)

        manager.hydrate_stored_bindings()

        state = manager.binding_runtime_snapshot_locked(binding)
        assert state is not None
        self.assertEqual(state.thread_id, "thread-1")
        self.assertEqual(state.thread_title, "Demo")
        self.assertEqual(state.feishu_runtime_state, "attached")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager._thread_lease_registry.lease_owner("thread-1"), binding)
        interaction_owner = manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)
        self.assertEqual(interaction_owner["kind"], "feishu")
        self.assertEqual(interaction_owner["relation"], "current")
        self.assertEqual(interaction_owner["binding_id"], "p2p:ou-user:chat-1")

    def test_binding_status_snapshot_uses_manager_owned_state(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = manager.resolve_runtime_binding(*binding).state
        state["working_dir"] = "/tmp/project"
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "Local title"
        state["current_thread_runtime_state"] = "attached"
        state["current_turn_id"] = "turn-1"
        state["running"] = True
        manager.subscribe_thread_locked(binding, "thread-1")
        manager.acquire_thread_write_lease_locked(binding, "thread-1")
        manager.acquire_interaction_lease_for_binding(binding, "thread-1")

        snapshot = manager.binding_status_snapshot(
            binding,
            read_thread_summary_for_status=lambda thread_id: (
                SimpleNamespace(title="Backend title", cwd="/srv/project", status="notLoaded"),
                "notLoaded",
            ),
            release_feishu_runtime_availability=lambda thread_id: (True, ""),
        )

        self.assertEqual(snapshot["binding_id"], "p2p:ou-user:chat-1")
        self.assertEqual(snapshot["thread_title"], "Backend title")
        self.assertEqual(snapshot["working_dir"], "/srv/project")
        self.assertEqual(snapshot["feishu_runtime_state"], "attached")
        self.assertEqual(snapshot["feishu_write_owner_relation"], "current")
        self.assertEqual(snapshot["interaction_owner"]["relation"], "current")
        self.assertTrue(snapshot["running_turn"])
        self.assertTrue(snapshot["release_feishu_runtime_available"])
        self.assertTrue(snapshot["reprofile_possible"])

    def test_execution_and_interaction_binding_can_adopt_sole_subscriber(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        self._attach_binding(
            manager,
            binding,
            acquire_write_owner=False,
            acquire_interaction_owner=False,
        )

        with manager._lock:
            interactive_binding, handled_elsewhere = manager.interactive_binding_for_thread_locked(
                "thread-1",
                adopt_sole_subscriber=True,
            )
            execution_binding = manager.execution_binding_for_thread_locked(
                "thread-1",
                adopt_sole_subscriber=True,
            )

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(interactive_binding, binding)
        self.assertFalse(handled_elsewhere)
        self.assertEqual(execution_binding, binding)
        self.assertEqual(manager.thread_write_owner("thread-1"), binding)
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["current_thread_write_owner_thread_id"], "thread-1")

    def test_binding_inventory_locked_reports_runtime_state_and_owner(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        state["running"] = True

        with manager._lock:
            inventory = manager.binding_inventory_locked()

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0]["binding_id"], "p2p:ou-user:chat-1")
        self.assertEqual(inventory[0]["binding_kind"], "p2p")
        self.assertEqual(inventory[0]["binding_state"], "bound")
        self.assertEqual(inventory[0]["feishu_runtime_state"], "attached")
        self.assertTrue(inventory[0]["feishu_write_owner"])
        self.assertTrue(inventory[0]["running_turn"])
        self.assertEqual(inventory[0]["working_dir"], "/tmp/project")

    def test_thread_binding_snapshot_locked_reports_bound_attached_and_released_bindings(self) -> None:
        manager = self._make_manager()
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        self._attach_binding(manager, binding_a)
        self._attach_binding(
            manager,
            binding_b,
            acquire_write_owner=False,
            acquire_interaction_owner=False,
        )
        with manager._lock:
            state_b = manager.resolve_runtime_binding(*binding_b).state
            manager.apply_persisted_runtime_state_message_locked(
                binding_b,
                state_b,
                ThreadStateChanged(current_thread_runtime_state="released"),
            )
            snapshot = manager.thread_binding_snapshot_locked(
                "thread-1",
                release_feishu_runtime_availability=lambda thread_id: (True, ""),
            )

        self.assertEqual(snapshot["thread_id"], "thread-1")
        self.assertEqual(sorted(snapshot["bound_binding_ids"]), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(snapshot["attached_binding_ids"], ["p2p:ou-user-a:chat-a"])
        self.assertEqual(snapshot["released_binding_ids"], ["p2p:ou-user-b:chat-b"])
        self.assertEqual(snapshot["feishu_write_owner_binding_id"], "p2p:ou-user-a:chat-a")
        self.assertEqual(snapshot["interaction_owner"]["binding_id"], "p2p:ou-user-a:chat-a")
        self.assertTrue(snapshot["release_feishu_runtime_available"])

    def test_deactivate_binding_locked_clears_runtime_store_and_leases(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        self._attach_binding(manager, binding)

        with manager._lock:
            unsubscribe_thread_id = manager.deactivate_binding_locked(binding)

        store = ChatBindingStore(data_dir)
        self.assertEqual(unsubscribe_thread_id, "thread-1")
        self.assertNotIn(binding, manager.binding_keys_locked())
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertIsNone(manager._thread_lease_registry.lease_owner("thread-1"))
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        self.assertIsNone(store.load(binding))

    def test_bind_thread_locked_replaces_old_thread_and_persists_new_attachment(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")
        state["current_message_id"] = "card-live"
        state["current_turn_id"] = "turn-1"

        with manager._lock:
            unsubscribe_thread_id = manager.bind_thread_locked(
                binding,
                state,
                thread_id="thread-new",
                thread_title="New",
                working_dir="/tmp/project-new",
                on_thread_replaced=lambda current_state: (
                    current_state.__setitem__("current_message_id", ""),
                    current_state.__setitem__("current_turn_id", ""),
                ),
            )

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(unsubscribe_thread_id, "thread-old")
        self.assertEqual(state["current_thread_id"], "thread-new")
        self.assertEqual(state["current_thread_title"], "New")
        self.assertEqual(state["working_dir"], "/tmp/project-new")
        self.assertEqual(state["current_thread_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-new"), [binding])
        self.assertIsNone(manager._thread_lease_registry.lease_owner("thread-old"))
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-new")
        self.assertEqual(stored["current_thread_runtime_state"], "attached")
        self.assertEqual(stored["working_dir"], "/tmp/project-new")

    def test_clear_thread_binding_locked_clears_attachment_and_keeps_binding_defaults(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        state["current_message_id"] = "card-live"
        state["last_execution_message_id"] = "card-old"

        with manager._lock:
            unsubscribe_thread_id = manager.clear_thread_binding_locked(
                binding,
                state,
                on_clear_state=lambda current_state: (
                    current_state.__setitem__("current_message_id", ""),
                    current_state.__setitem__("last_execution_message_id", ""),
                ),
            )

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(unsubscribe_thread_id, "thread-1")
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["current_thread_title"], "")
        self.assertEqual(state["current_thread_runtime_state"], "")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertIsNone(manager._thread_lease_registry.lease_owner("thread-1"))
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "")
        self.assertEqual(stored["current_thread_runtime_state"], "")
        self.assertEqual(stored["working_dir"], "/tmp/project")

    def test_release_feishu_runtime_by_thread_id_locked_marks_bindings_released(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        state_a = self._attach_binding(manager, binding_a)
        state_b = self._attach_binding(
            manager,
            binding_b,
            acquire_write_owner=False,
            acquire_interaction_owner=False,
        )
        state_a["current_message_id"] = "card-a"
        state_b["current_message_id"] = "card-b"

        with manager._lock:
            result = manager.release_feishu_runtime_by_thread_id_locked(
                "thread-1",
                release_feishu_runtime_availability=lambda thread_id: (True, ""),
                on_release_binding_state=lambda current_state: current_state.__setitem__("current_message_id", ""),
            )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertTrue(result.changed)
        self.assertFalse(result.already_released)
        self.assertEqual(result.thread_id, "thread-1")
        self.assertEqual(result.thread_title, "Demo")
        self.assertEqual(result.working_dir, "/tmp/project")
        self.assertEqual(result.unsubscribe_thread_id, "thread-1")
        self.assertEqual(sorted(result.bound_binding_ids), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(sorted(result.released_binding_ids), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(state_a["current_thread_runtime_state"], "released")
        self.assertEqual(state_b["current_thread_runtime_state"], "released")
        self.assertEqual(state_a["current_message_id"], "")
        self.assertEqual(state_b["current_message_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding_a, binding_b])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertIsNone(manager._thread_lease_registry.lease_owner("thread-1"))
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["current_thread_runtime_state"], "released")
        self.assertEqual(stored_b["current_thread_runtime_state"], "released")
        self.assertEqual(stored_a["current_thread_write_owner_thread_id"], "")
        self.assertEqual(stored_b["current_thread_write_owner_thread_id"], "")
