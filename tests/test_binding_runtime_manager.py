import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_state import RuntimeSettingsChanged, ThreadStateChanged
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


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
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
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
        acquire_interaction_owner: bool = True,
    ):
        state = manager.resolve_runtime_binding(*binding).state
        with manager._lock:
            state["working_dir"] = working_dir
            state["current_thread_id"] = thread_id
            state["current_thread_title"] = thread_title
            state["feishu_runtime_state"] = "attached"
            manager.subscribe_thread_locked(binding, thread_id)
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

    def test_hydrate_stored_bindings_downgrades_persisted_attachment(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        (data_dir / "chat_bindings.json").write_text(
            """{
  "schema_version": 4,
  "p2p_bindings": {
    "chat-1": {
      "ou-user": {
        "working_dir": "/tmp/project",
        "current_thread_id": "thread-1",
        "current_thread_title": "Demo",
        "feishu_runtime_state": "attached",
        "current_thread_write_owner_thread_id": "thread-1",
        "approval_policy": "never",
        "sandbox": "danger-full-access",
        "collaboration_mode": "plan"
      }
    }
  },
  "group_bindings": {}
}
""",
            encoding="utf-8",
        )
        manager = self._make_manager(data_dir=data_dir)

        manager.hydrate_stored_bindings()

        state = manager.binding_runtime_snapshot_locked(binding)
        assert state is not None
        self.assertEqual(state.thread_id, "thread-1")
        self.assertEqual(state.thread_title, "Demo")
        self.assertEqual(state.feishu_runtime_state, "detached")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        interaction_owner = manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)
        self.assertEqual(interaction_owner["kind"], "none")
        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "detached")
        self.assertEqual(stored["reasoning_effort"], "")

    def test_binding_status_snapshot_uses_manager_owned_state(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = manager.resolve_runtime_binding(*binding).state
        state["working_dir"] = "/tmp/project"
        state["current_thread_id"] = "thread-1"
        state["current_thread_title"] = "Local title"
        state["feishu_runtime_state"] = "attached"
        state["current_turn_id"] = "turn-1"
        state["running"] = True
        manager.subscribe_thread_locked(binding, "thread-1")
        manager.acquire_interaction_lease_for_binding(binding, "thread-1")

        snapshot = manager.binding_status_snapshot(
            binding,
            read_thread_summary_for_status=lambda thread_id: (
                SimpleNamespace(title="Backend title", cwd="/srv/project", status="notLoaded"),
                "notLoaded",
            ),
            detach_availability=lambda thread_id: (True, ""),
        )

        self.assertEqual(snapshot["binding_id"], "p2p:ou-user:chat-1")
        self.assertEqual(snapshot["thread_title"], "Backend title")
        self.assertEqual(snapshot["working_dir"], "/srv/project")
        self.assertEqual(snapshot["feishu_runtime_state"], "attached")
        self.assertEqual(snapshot["interaction_owner"]["relation"], "current")
        self.assertTrue(snapshot["running_turn"])
        self.assertTrue(snapshot["detach_available"])

    def test_interactive_binding_can_adopt_sole_subscriber(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        self._attach_binding(
            manager,
            binding,
            acquire_interaction_owner=False,
        )

        with manager._lock:
            interactive_binding, handled_elsewhere = manager.interactive_binding_for_thread_locked(
                "thread-1",
                adopt_sole_subscriber=True,
            )

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(interactive_binding, binding)
        self.assertFalse(handled_elsewhere)
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")

    def test_binding_inventory_locked_reports_runtime_state(self) -> None:
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
        self.assertTrue(inventory[0]["running_turn"])
        self.assertEqual(inventory[0]["working_dir"], "/tmp/project")

    def test_thread_binding_snapshot_locked_reports_bound_attached_and_detached_bindings(self) -> None:
        manager = self._make_manager()
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        self._attach_binding(manager, binding_a)
        self._attach_binding(
            manager,
            binding_b,
            acquire_interaction_owner=False,
        )
        with manager._lock:
            state_b = manager.resolve_runtime_binding(*binding_b).state
            manager.apply_persisted_runtime_state_message_locked(
                binding_b,
                state_b,
                ThreadStateChanged(feishu_runtime_state="detached"),
            )
            snapshot = manager.thread_binding_snapshot_locked(
                "thread-1",
                detach_availability=lambda thread_id: (True, ""),
            )

        self.assertEqual(snapshot["thread_id"], "thread-1")
        self.assertEqual(sorted(snapshot["bound_binding_ids"]), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(snapshot["attached_binding_ids"], ["p2p:ou-user-a:chat-a"])
        self.assertEqual(snapshot["detached_binding_ids"], ["p2p:ou-user-b:chat-b"])
        self.assertEqual(snapshot["interaction_owner"]["binding_id"], "p2p:ou-user-a:chat-a")
        self.assertTrue(snapshot["detach_available"])

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
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        self.assertIsNone(store.load(binding))

    def test_deactivate_binding_locked_rolls_back_when_store_clear_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        state["current_message_id"] = "card-live"

        with patch.object(manager._chat_binding_store, "clear", side_effect=RuntimeError("store clear failed")):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store clear failed"):
                    manager.deactivate_binding_locked(
                        binding,
                        on_deactivate_state=lambda current_state: current_state.__setitem__("current_message_id", ""),
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "card-live")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_deactivate_bindings_locked_rolls_back_all_bindings_when_batch_clear_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        state_a = self._attach_binding(manager, binding_a, thread_id="thread-a", thread_title="Demo A")
        state_b = self._attach_binding(manager, binding_b, thread_id="thread-b", thread_title="Demo B")
        state_a["current_message_id"] = "card-a"
        state_b["current_message_id"] = "card-b"

        with patch.object(
            manager._chat_binding_store,
            "clear",
            side_effect=[None, RuntimeError("store clear failed")],
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store clear failed"):
                    manager.deactivate_bindings_locked(
                        [binding_a, binding_b],
                        on_deactivate_state=lambda current_state: current_state.__setitem__("current_message_id", ""),
                    )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertEqual(state_a["current_thread_id"], "thread-a")
        self.assertEqual(state_b["current_thread_id"], "thread-b")
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(state_a["current_message_id"], "card-a")
        self.assertEqual(state_b["current_message_id"], "card-b")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-a"), [binding_a])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-b"), [binding_b])
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["current_thread_id"], "thread-a")
        self.assertEqual(stored_b["current_thread_id"], "thread-b")

    def test_deactivate_binding_preserves_retry_marker_when_interaction_lease_cleanup_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        self._attach_binding(manager, binding)
        cleanup_errors: list[str] = []

        with patch.object(
            manager._interaction_lease_store,
            "release",
            side_effect=OSError("lease store failed"),
        ):
            with manager._lock:
                manager.deactivate_bindings_locked([binding], cleanup_errors=cleanup_errors)

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertIsNotNone(stored)
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(len(cleanup_errors), 1)
        self.assertIn("lease store failed", cleanup_errors[0])

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
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-new"), [binding])
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-new")
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(stored["working_dir"], "/tmp/project-new")

    def test_bind_thread_locked_rolls_back_when_persist_or_after_bind_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")
        state["current_message_id"] = "card-live"
        state["current_turn_id"] = "turn-1"

        def _after_bind_failure(_state) -> None:
            raise RuntimeError("after bind failed")

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "after bind failed"):
                manager.bind_thread_locked(
                    binding,
                    state,
                    thread_id="thread-new",
                    thread_title="New",
                    working_dir="/tmp/project-new",
                    on_thread_replaced=lambda current_state: (
                        current_state.__setitem__("current_message_id", ""),
                        current_state.__setitem__("current_turn_id", ""),
                    ),
                    on_after_bind=_after_bind_failure,
                )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(state["current_thread_title"], "Old")
        self.assertEqual(state["working_dir"], "/tmp/project")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "card-live")
        self.assertEqual(state["current_turn_id"], "turn-1")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [binding])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-old"), [binding])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-old", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-old")
        self.assertEqual(stored["feishu_runtime_state"], "attached")

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
        self.assertEqual(state["feishu_runtime_state"], "")
        self.assertEqual(state["current_message_id"], "")
        self.assertEqual(state["last_execution_message_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "")
        self.assertEqual(stored["feishu_runtime_state"], "")
        self.assertEqual(stored["working_dir"], "/tmp/project")

    def test_clear_thread_binding_locked_rolls_back_when_persist_or_clear_state_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        state["current_message_id"] = "card-live"

        def _clear_state_failure(_state) -> None:
            raise RuntimeError("clear state failed")

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "clear state failed"):
                manager.clear_thread_binding_locked(
                    binding,
                    state,
                    on_clear_state=_clear_state_failure,
                )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["current_thread_title"], "Demo")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "card-live")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_sync_stored_binding_locked_clears_fresh_default_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.sync_stored_binding_locked(binding, state)

        self.assertIsNone(ChatBindingStore(data_dir).load(binding))

    def test_bind_thread_locked_rolls_back_when_store_save_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")

        with patch.object(manager._chat_binding_store, "save", side_effect=RuntimeError("store save failed")):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store save failed"):
                    manager.bind_thread_locked(
                        binding,
                        state,
                        thread_id="thread-new",
                        thread_title="New",
                        working_dir="/tmp/project-new",
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [binding])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [])
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-old")

    def test_detach_binding_locked_rolls_back_when_store_save_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        state["current_message_id"] = "card-live"

        with patch.object(manager._chat_binding_store, "save", side_effect=RuntimeError("store save failed")):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store save failed"):
                    manager.detach_binding_locked(
                        binding,
                        on_detach_binding_state=lambda current_state: current_state.__setitem__("current_message_id", ""),
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["current_message_id"], "card-live")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_hydrate_stored_binding_locked_uses_runtime_defaults_for_empty_overrides(self) -> None:
        manager = self._make_manager()
        state = manager.build_default_runtime_state()

        with manager._lock:
            manager.hydrate_stored_binding_locked(
                state,
                {
                    "working_dir": "",
                    "current_thread_id": "",
                    "current_thread_title": "",
                    "feishu_runtime_state": "",
                    "approval_policy": "",
                    "sandbox": "",
                    "collaboration_mode": "",
                },
            )

        self.assertEqual(state["working_dir"], "/tmp/default")
        self.assertEqual(state["approval_policy"], "on-request")
        self.assertEqual(state["permissions_profile_id"], ":workspace")

    def test_legacy_collaboration_mode_field_is_ignored_on_hydrate(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        ChatBindingStore(data_dir).save(
            binding,
            {
                "working_dir": "",
                "current_thread_id": "",
                "current_thread_title": "",
                "feishu_runtime_state": "",
                "approval_policy": "",
                "permissions_profile_id": "",
                "collaboration_mode": "plan",
                "model": "",
                "reasoning_effort": "",
            },
        )

        manager = self._make_manager(data_dir=data_dir)
        manager.hydrate_stored_bindings()
        state = manager.resolve_runtime_binding(*binding).state

        self.assertNotIn("collaboration_mode", state)

    def test_unbound_model_auto_persists_across_default_changes(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                RuntimeSettingsChanged(model=""),
            )

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["model"], "")
        self.assertEqual(stored["configured_settings"], ["model"])

        restarted = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-6",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        restarted.hydrate_stored_bindings()
        restarted_state = restarted.resolve_runtime_binding(*binding).state
        self.assertEqual(restarted_state["model"], "")
        self.assertEqual(restarted_state["permissions_profile_id"], ":workspace")

    def test_explicit_default_approval_and_permissions_persist_across_default_changes(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                RuntimeSettingsChanged(
                    approval_policy="on-request",
                    permissions_profile_id=":workspace",
                ),
            )

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["approval_policy"], "on-request")
        self.assertEqual(stored["permissions_profile_id"], ":workspace")
        self.assertEqual(
            stored["configured_settings"],
            ["approval_policy", "permissions_profile_id"],
        )

        restarted = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="never",
            default_permissions_profile_id=":danger-full-access",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        restarted.hydrate_stored_bindings()
        restarted_state = restarted.resolve_runtime_binding(*binding).state
        self.assertEqual(restarted_state["approval_policy"], "on-request")
        self.assertEqual(restarted_state["permissions_profile_id"], ":workspace")

    def test_configured_unbound_binding_is_visible_in_status_and_inventory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                RuntimeSettingsChanged(model=""),
            )
            status = manager.binding_status_state_snapshot_locked(binding)
            inventory = manager.binding_inventory_locked()

        self.assertEqual(status["binding_state"], "configured/unbound")
        self.assertEqual(inventory[0]["binding_state"], "configured/unbound")
        self.assertEqual(status["thread_id"], "")

    def test_configured_settings_survive_clearing_thread_bookmark(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                RuntimeSettingsChanged(
                    approval_policy="on-request",
                    permissions_profile_id=":workspace",
                    model="",
                    reasoning_effort="",
                ),
            )
            manager.bind_thread_locked(
                binding,
                state,
                thread_id="thread-1",
                thread_title="Demo",
                working_dir="/tmp/default",
            )
            manager.clear_thread_binding_locked(binding, state)
            status = manager.binding_status_state_snapshot_locked(binding)

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "")
        self.assertEqual(stored["working_dir"], "")
        self.assertEqual(
            stored["configured_settings"],
            ["approval_policy", "model", "permissions_profile_id", "reasoning_effort"],
        )
        self.assertEqual(status["binding_state"], "configured/unbound")

    def test_unbound_effort_auto_persists_across_default_changes(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = manager.resolve_runtime_binding(*binding).state

        with manager._lock:
            manager.apply_persisted_runtime_state_message_locked(
                binding,
                state,
                RuntimeSettingsChanged(reasoning_effort=""),
            )

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["reasoning_effort"], "")
        self.assertEqual(stored["configured_settings"], ["reasoning_effort"])

        restarted = BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="xhigh",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda chat_id, message_id: False,
        )
        restarted.hydrate_stored_bindings()
        restarted_state = restarted.resolve_runtime_binding(*binding).state
        self.assertEqual(restarted_state["reasoning_effort"], "")

    def test_unsubscribe_by_thread_id_locked_marks_bindings_detached(self) -> None:
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
            acquire_interaction_owner=False,
        )
        state_a["current_message_id"] = "card-a"
        state_b["current_message_id"] = "card-b"

        with manager._lock:
            result = manager.detach_thread_bindings_locked(
                "thread-1",
                detach_availability=lambda thread_id: (True, ""),
                on_release_binding_state=lambda current_state: current_state.__setitem__("current_message_id", ""),
            )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertTrue(result.changed)
        self.assertFalse(result.already_detached)
        self.assertEqual(result.thread_id, "thread-1")
        self.assertEqual(result.thread_title, "Demo")
        self.assertEqual(result.working_dir, "/tmp/project")
        self.assertEqual(result.unsubscribe_thread_id, "thread-1")
        self.assertEqual(sorted(result.bound_binding_ids), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(sorted(result.detached_binding_ids), ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"])
        self.assertEqual(state_a["feishu_runtime_state"], "detached")
        self.assertEqual(state_b["feishu_runtime_state"], "detached")
        self.assertEqual(state_a["current_message_id"], "")
        self.assertEqual(state_b["current_message_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding_a, binding_b])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["feishu_runtime_state"], "detached")
        self.assertEqual(stored_b["feishu_runtime_state"], "detached")

    def test_detach_thread_bindings_locked_rolls_back_all_bindings_when_store_save_fails(self) -> None:
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
            acquire_interaction_owner=False,
        )
        state_a["current_message_id"] = "card-a"
        state_b["current_message_id"] = "card-b"

        with patch.object(
            manager._chat_binding_store,
            "save",
            side_effect=[None, RuntimeError("store save failed")],
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store save failed"):
                    manager.detach_thread_bindings_locked(
                        "thread-1",
                        detach_availability=lambda thread_id: (True, ""),
                        on_release_binding_state=lambda current_state: current_state.__setitem__(
                            "current_message_id",
                            "",
                        ),
                    )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(state_a["current_message_id"], "card-a")
        self.assertEqual(state_b["current_message_id"], "card-b")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding_a, binding_b])
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding_a)["relation"],
            "current",
        )
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["feishu_runtime_state"], "attached")
        self.assertEqual(stored_b["feishu_runtime_state"], "attached")

    def test_unsubscribe_by_thread_id_locked_respects_external_availability_gate(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user-a", "chat-a")
        state = self._attach_binding(manager, binding)

        with manager._lock:
            with self.assertRaisesRegex(ValueError, "blocked by controller"):
                manager.detach_thread_bindings_locked(
                    "thread-1",
                    detach_availability=lambda thread_id: (False, "blocked by controller"),
                )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"],
            "current",
        )
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")
