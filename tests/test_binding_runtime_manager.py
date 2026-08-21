import os
import pathlib
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.binding_runtime_contract import (
    BindingOwnerLossSettlementReceipt,
)
from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.runtime_state import (
    ExecutionPatchTimerRegistration,
    ExecutionPatchTimerTicket,
    MirrorWatchdogRegistration,
    MirrorWatchdogTicket,
    RuntimeSettingsChanged,
    ThreadStateChanged,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)
from bot.thread_subscription_registry import ThreadSubscriptionRegistry
from tests.execution_page_test_support import set_execution_page_state


class BindingRuntimeManagerTests(unittest.TestCase):
    def _make_manager(
        self,
        *,
        is_group_chat=None,
        data_dir: pathlib.Path | None = None,
        owner_loss_observer=None,
    ) -> BindingRuntimeManager:
        if data_dir is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            data_dir = pathlib.Path(tempdir.name)
        interaction_leases = InteractionLeaseStore(data_dir)
        transaction_nonce = 0

        def settle_owner_loss(command):
            nonlocal transaction_nonce
            if owner_loss_observer is not None:
                owner_loss_observer(command)
            lease = interaction_leases.load(command.thread_id)
            holder = make_feishu_interaction_holder(
                command.binding[0],
                command.binding[1],
                owner_pid=os.getpid(),
            )
            if lease is not None and lease.holder.same_holder(holder):
                if interaction_leases.release_if_matches(lease) is not True:
                    raise RuntimeError("lease release failed")
            transaction_nonce += 1
            return BindingOwnerLossSettlementReceipt(
                command=command,
                _settler_nonce=1,
                _transaction_nonce=transaction_nonce,
            )

        return BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=ChatBindingStore(data_dir),
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=interaction_leases,
            is_group_chat=is_group_chat or (lambda chat_id, message_id: False),
            owner_loss_settler=settle_owner_loss,
        )

    @staticmethod
    def _resident_state(
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
    ):
        session = manager.resolve_session(*binding)
        with manager._lock:
            state = manager.resident_runtime_state_locked(session.binding)
        assert state is not None
        return state

    @staticmethod
    def _resident_handle_locked(
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
    ):
        session = manager.resident_session_snapshot_locked(binding)
        assert session is not None
        return session.handle

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
        state = self._resident_state(manager, binding)
        with manager._lock:
            state["working_dir"] = working_dir
            state["current_thread_id"] = thread_id
            state["current_thread_title"] = thread_title
            state["feishu_runtime_state"] = "attached"
            manager.subscribe_thread_locked(binding, thread_id)
            if acquire_interaction_owner:
                manager.acquire_interaction_lease_for_binding(binding, thread_id)
            manager._sync_resident_state_locked(binding, state)
        return state

    def _recreate_and_rebind(
        self,
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
    ):
        with manager._lock:
            manager.deactivate_bindings_with_receipts_locked((binding,))
            replacement = manager._get_or_create_runtime_state_locked(binding)
            manager.bind_thread_locked(
                self._resident_handle_locked(manager, binding),
                thread_id="thread-replacement",
                thread_title="Replacement",
                working_dir="/tmp/replacement",
            )
            return replacement

    def test_staging_clone_preserves_timer_ticket_identity_without_real_timer(self) -> None:
        manager = self._make_manager()
        state = manager.build_default_runtime_state()
        binding = ("ou-user", "chat-1")
        patch_timer = Mock()
        watchdog_timer = Mock()
        patch_ticket = ExecutionPatchTimerTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        watchdog_ticket = MirrorWatchdogTicket(
            binding=binding,
            thread_id="thread-1",
            card_message_id="card-1",
            turn_id="turn-1",
        )
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=patch_ticket,
            timer=patch_timer,
        )
        state["mirror_watchdog_registration"] = MirrorWatchdogRegistration(
            ticket=watchdog_ticket,
            timer=watchdog_timer,
        )

        staged = manager._clone_runtime_state_for_staging(state)
        staged_patch = staged["patch_timer_registration"]
        staged_watchdog = staged["mirror_watchdog_registration"]
        assert staged_patch is not None
        assert staged_watchdog is not None

        self.assertIs(staged_patch.ticket, patch_ticket)
        self.assertIs(staged_watchdog.ticket, watchdog_ticket)
        self.assertIsNot(staged_patch.timer, patch_timer)
        self.assertIsNot(staged_watchdog.timer, watchdog_timer)
        staged_patch.timer.cancel()
        staged_watchdog.timer.cancel()
        patch_timer.cancel.assert_not_called()
        watchdog_timer.cancel.assert_not_called()

    def test_resolve_session_reuses_existing_group_binding(self) -> None:
        manager = self._make_manager(is_group_chat=lambda chat_id, message_id: bool(message_id))

        first = manager.resolve_session("ou-user-1", "chat-group", "m-group")
        second = manager.resolve_session("ou-user-2", "chat-group")

        self.assertEqual(first.binding, ("__group__", "chat-group"))
        self.assertEqual(second.binding, ("__group__", "chat-group"))
        self.assertIs(first.handle, second.handle)

    def test_resolve_session_rechecks_deactivate_recreate_rebind_race(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        stale_state = self._resident_state(manager, binding)
        stale_state["working_dir"] = "/tmp/stale"
        original_existing = manager.existing_chat_binding_key_locked
        existing_calls = 0
        raced = False

        def miss_once_then_resolve(sender_id, chat_id):
            nonlocal existing_calls
            existing_calls += 1
            if existing_calls == 1:
                return None
            return original_existing(sender_id, chat_id)

        def replace_during_resolution(sender_id, chat_id, message_id=""):
            nonlocal raced
            if not raced:
                raced = True
                self._recreate_and_rebind(manager, binding)
            return binding

        with (
            patch.object(
                manager,
                "existing_chat_binding_key_locked",
                side_effect=miss_once_then_resolve,
            ),
            patch.object(
                manager,
                "fresh_chat_binding_key",
                side_effect=replace_during_resolution,
            ),
        ):
            runtime = manager.resolve_session(*binding)

        self.assertTrue(raced)
        self.assertEqual(runtime.current_thread_id, "thread-replacement")
        self.assertEqual(runtime.current_thread_title, "Replacement")
        self.assertEqual(runtime.working_dir, "/tmp/replacement")
        self.assertIsNot(manager._runtime_state_by_binding[binding], stale_state)

    def test_save_stored_binding_rechecks_deactivate_recreate_rebind_race(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        stale_state = self._resident_state(manager, binding)
        stale_state["working_dir"] = "/tmp/stale"
        original_resolve = manager.resolve_session
        raced = False

        def resolve_then_replace(sender_id, chat_id, message_id=""):
            nonlocal raced
            resolved = original_resolve(sender_id, chat_id, message_id)
            if not raced:
                raced = True
                self._recreate_and_rebind(manager, binding)
            return resolved

        with patch.object(
            manager,
            "resolve_session",
            side_effect=resolve_then_replace,
        ):
            manager.save_stored_binding(*binding)

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertTrue(raced)
        self.assertEqual(stored["current_thread_id"], "thread-replacement")
        self.assertEqual(stored["current_thread_title"], "Replacement")
        self.assertEqual(stored["working_dir"], "/tmp/replacement")
        self.assertIsNot(manager._runtime_state_by_binding[binding], stale_state)

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

    def test_pure_inventory_does_not_hide_attached_owner_from_startup_reconcile(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        original = self._make_manager(data_dir=data_dir)
        self._attach_binding(original, binding)
        owner_losses = []
        restarted = self._make_manager(
            data_dir=data_dir,
            owner_loss_observer=owner_losses.append,
        )

        with restarted._lock:
            records = restarted.binding_record_inventory_locked()

        self.assertEqual(restarted.binding_keys_locked(), ())
        self.assertEqual(records[0].binding, binding)
        self.assertFalse(records[0].runtime_resident)
        self.assertEqual(records[0].feishu_runtime_state, "detached")
        self.assertEqual(owner_losses, [])
        self.assertEqual(
            restarted.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding,
            )["relation"],
            "current",
        )
        stored_before_reconcile = ChatBindingStore(data_dir).load(binding)
        assert stored_before_reconcile is not None
        self.assertEqual(
            stored_before_reconcile["feishu_runtime_state"],
            "attached",
        )

        restarted.hydrate_stored_bindings(replace=True)

        self.assertEqual(len(owner_losses), 1)
        self.assertEqual(owner_losses[0].binding, binding)
        self.assertEqual(owner_losses[0].thread_id, "thread-1")
        self.assertEqual(owner_losses[0].reason, "binding_hydrated")
        snapshot = restarted.binding_runtime_snapshot_locked(binding)
        assert snapshot is not None
        self.assertEqual(snapshot.feishu_runtime_state, "detached")
        self.assertEqual(
            restarted.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding,
            )["kind"],
            "none",
        )
        stored_after_reconcile = ChatBindingStore(data_dir).load(binding)
        assert stored_after_reconcile is not None
        self.assertEqual(
            stored_after_reconcile["feishu_runtime_state"],
            "detached",
        )

    def test_replace_hydration_settles_current_and_persisted_attachment_once_before_clear(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_owner_loss)
        self._attach_binding(manager, binding)

        with self.assertRaisesRegex(RuntimeError, "owner settlement failed"):
            manager.hydrate_stored_bindings(replace=True)

        # `replace` used to clear the registry before it noticed that the
        # persisted attached binding had lost its owner.  The callback must
        # run exactly once for the identical runtime+store entry, and failure
        # must leave every local delivery/lease fact intact.
        snapshot = manager.binding_runtime_snapshot_locked(binding)
        stored = ChatBindingStore(data_dir).load(binding)
        assert snapshot is not None
        assert stored is not None
        self.assertEqual(len(owner_losses), 1)
        self.assertEqual(owner_losses[0].reason, "binding_hydrated")
        self.assertEqual(snapshot.feishu_runtime_state, "attached")
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"],
            "current",
        )
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_replace_hydration_reentrant_binding_change_rejects_before_local_clear(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding_a = ("ou-a", "chat-a")
        binding_b = ("ou-b", "chat-b")
        manager: BindingRuntimeManager
        reentered = False

        def update_other_binding(command) -> None:
            nonlocal reentered
            if command.binding != binding_a or reentered:
                return
            reentered = True
            manager.bind_thread_locked(
                self._resident_handle_locked(manager, binding_b),
                thread_id="thread-b",
                thread_title="New B",
                working_dir="/tmp/b",
            )

        manager = self._make_manager(
            data_dir=data_dir,
            owner_loss_observer=update_other_binding,
        )
        state_a = self._attach_binding(manager, binding_a, thread_id="thread-a")
        state_b = self._resident_state(manager, binding_b)
        with manager._lock:
            state_b.update(
                current_thread_id="thread-b",
                current_thread_title="Old B",
                feishu_runtime_state="detached",
                working_dir="/tmp/b",
            )
            manager._sync_resident_state_locked(binding_b, state_b)

        with self.assertRaisesRegex(RuntimeError, "过期或被替换"):
            manager.hydrate_stored_bindings(replace=True)

        self.assertIs(manager._runtime_state_by_binding[binding_a], state_a)
        self.assertIs(manager._runtime_state_by_binding[binding_b], state_b)
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["current_thread_title"], "New B")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(manager.thread_subscribers("thread-a"), (binding_a,))
        self.assertEqual(manager.thread_subscribers("thread-b"), (binding_b,))
        stored_b = ChatBindingStore(data_dir).load(binding_b)
        assert stored_b is not None
        self.assertEqual(stored_b["current_thread_title"], "New B")

    def test_replace_hydration_store_failure_preserves_resident_runtime(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = self._attach_binding(manager, binding)

        with patch.object(
            manager._chat_binding_store,
            "save",
            side_effect=RuntimeError("store save failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "store save failed"):
                manager.hydrate_stored_bindings(replace=True)

        self.assertIs(manager._runtime_state_by_binding[binding], state)
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_binding_status_snapshot_uses_manager_owned_state(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._resident_state(manager, binding)
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
        self.assertEqual(
            manager.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding,
            )["relation"],
            "current",
        )
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
            state_b = self._resident_state(manager, binding_b)
            manager._apply_persisted_runtime_state_message_locked(
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
        owner_losses = []
        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=owner_losses.append)
        binding = ("ou-user", "chat-1")
        self._attach_binding(manager, binding)

        with manager._lock:
            receipts = manager.deactivate_bindings_with_receipts_locked(
                (binding,),
                owner_loss_disposition="terminal",
            )
            unsubscribe_thread_id = receipts[0].unsubscribe_thread_id

        store = ChatBindingStore(data_dir)
        self.assertEqual(unsubscribe_thread_id, "thread-1")
        self.assertNotIn(binding, manager.binding_keys_locked())
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        self.assertIsNone(store.load(binding))
        self.assertEqual(len(owner_losses), 1)
        self.assertEqual(owner_losses[0].binding, binding)
        self.assertEqual(owner_losses[0].thread_id, "thread-1")
        self.assertEqual(owner_losses[0].reason, "binding_deactivated")
        self.assertEqual(owner_losses[0].disposition, "terminal")

    def test_deactivate_binding_locked_commits_only_selected_store_record(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        selected = ("ou-selected", "chat-selected")
        retained = ("ou-retained", "chat-retained")
        original = self._make_manager(data_dir=data_dir)
        self._attach_binding(original, selected, thread_id="thread-selected")
        self._attach_binding(original, retained, thread_id="thread-retained")
        owner_losses = []
        restarted = self._make_manager(
            data_dir=data_dir,
            owner_loss_observer=owner_losses.append,
        )

        with restarted._lock:
            receipts = restarted.deactivate_bindings_with_receipts_locked((selected,))
            unsubscribe_thread_id = receipts[0].unsubscribe_thread_id

        self.assertEqual(unsubscribe_thread_id, "")
        self.assertEqual(restarted.binding_keys_locked(), ())
        self.assertIsNone(ChatBindingStore(data_dir).load(selected))
        retained_record = ChatBindingStore(data_dir).load(retained)
        assert retained_record is not None
        self.assertEqual(retained_record["feishu_runtime_state"], "attached")
        self.assertEqual(
            restarted.interaction_owner_snapshot_locked("thread-selected")["kind"],
            "none",
        )
        self.assertEqual(
            restarted.interaction_owner_snapshot_locked(
                "thread-retained",
                current_binding=retained,
            )["relation"],
            "current",
        )
        self.assertEqual([event.binding for event in owner_losses], [selected])

    def test_deactivate_binding_locked_rolls_back_when_store_clear_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        set_execution_page_state(state, current_message_id="card-live")

        with patch.object(manager._chat_binding_store, "clear", side_effect=RuntimeError("store clear failed")):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store clear failed"):
                    manager.deactivate_bindings_with_receipts_locked((binding,))

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "card-live")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(
            manager.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding,
            )["kind"],
            "none",
        )
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
        set_execution_page_state(state_a, current_message_id="card-a")
        set_execution_page_state(state_b, current_message_id="card-b")

        with patch.object(
            manager._chat_binding_store,
            "clear",
            side_effect=[None, RuntimeError("store clear failed")],
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store clear failed"):
                    manager.deactivate_bindings_with_receipts_locked(
                        (binding_a, binding_b)
                    )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertEqual(state_a["current_thread_id"], "thread-a")
        self.assertEqual(state_b["current_thread_id"], "thread-b")
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(state_a["execution_pages"].current_message_id, "card-a")
        self.assertEqual(state_b["execution_pages"].current_message_id, "card-b")
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
            "release_if_matches",
            side_effect=OSError("lease store failed"),
        ):
            with manager._lock:
                with self.assertRaisesRegex(OSError, "lease store failed"):
                    manager.deactivate_bindings_with_receipts_locked(
                        (binding,),
                        cleanup_errors=cleanup_errors,
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertIsNotNone(stored)
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(cleanup_errors, [])

    def test_deactivate_staging_does_not_cancel_real_timer(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        timer = Mock()
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ExecutionPatchTimerTicket(
                binding=binding,
                thread_id="thread-1",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=timer,
        )

        with manager._lock:
            receipts = manager.deactivate_bindings_with_receipts_locked((binding,))
            timer.cancel.assert_not_called()

        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(receipts[0].timer_cancellations), 1)
        cancel_runtime_timer_effects(receipts[0].timer_cancellations)
        timer.cancel.assert_called_once_with()

    def test_deactivate_shared_thread_ghost_suppresses_finalizer(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        removed_binding = ("ou-removed", "chat-removed")
        ghost_binding = ("ou-ghost", "chat-ghost")
        self._attach_binding(manager, removed_binding, thread_id="thread-shared")
        ghost_record = manager.build_default_stored_binding()
        ghost_record["current_thread_id"] = "thread-shared"
        ghost_record["current_thread_title"] = "Ghost"
        ghost_record["feishu_runtime_state"] = "detached"
        manager._chat_binding_store.save(ghost_binding, ghost_record)
        cleanup_errors: list[str] = []
        with manager._lock:
            receipts = manager.deactivate_bindings_with_receipts_locked(
                [removed_binding, ghost_binding],
                cleanup_errors=cleanup_errors,
            )

        self.assertEqual(len(receipts), 2)
        self.assertEqual(receipts[0].binding, removed_binding)
        self.assertEqual(receipts[0].thread_id, "thread-shared")
        self.assertEqual(receipts[0].unsubscribe_thread_id, "thread-shared")
        self.assertFalse(manager.binding_exists_locked(removed_binding))
        self.assertFalse(manager.binding_exists_locked(ghost_binding))
        self.assertEqual(cleanup_errors, [])

    def test_deactivate_proof_read_failure_keeps_exact_removal_receipt(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        self._attach_binding(manager, binding)
        cleanup_errors: list[str] = []

        with patch.object(
            manager,
            "binding_record_inventory_locked",
            side_effect=OSError("inventory unavailable"),
        ):
            with manager._lock:
                receipts = manager.deactivate_bindings_with_receipts_locked(
                    [binding],
                    cleanup_errors=cleanup_errors,
                )

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].binding, binding)
        self.assertEqual(receipts[0].unsubscribe_thread_id, "")
        self.assertFalse(manager.binding_exists_locked(binding))
        self.assertTrue(any("inventory unavailable" in item for item in cleanup_errors))

    def test_deactivate_timer_cleanup_failure_does_not_rollback_owner(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        timer = Mock()
        timer.cancel.side_effect = RuntimeError("timer cleanup failed")
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ExecutionPatchTimerTicket(
                binding=binding,
                thread_id="thread-1",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=timer,
        )

        with manager._lock:
            receipts = manager.deactivate_bindings_with_receipts_locked((binding,))
            timer.cancel.assert_not_called()

        cancel_runtime_timer_effects(receipts[0].timer_cancellations)
        timer.cancel.assert_called_once_with()
        self.assertNotIn(binding, manager._runtime_state_by_binding)
        self.assertEqual(manager.thread_subscribers("thread-1"), ())
        self.assertIsNone(ChatBindingStore(data_dir).load(binding))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1")["kind"],
            "none",
        )

    def test_bind_thread_locked_replaces_old_thread_and_persists_new_attachment(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner_losses = []
        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=owner_losses.append)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")
        set_execution_page_state(state, current_message_id="card-live")
        state["current_turn_id"] = "turn-1"

        with manager._lock:
            result = manager.bind_thread_locked(
                self._resident_handle_locked(manager, binding),
                thread_id="thread-new",
                thread_title="New",
                working_dir="/tmp/project-new",
            )

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(result.unsubscribe_thread_id, "thread-old")
        self.assertEqual(state["current_thread_id"], "thread-new")
        self.assertEqual(state["current_thread_title"], "New")
        self.assertEqual(state["working_dir"], "/tmp/project-new")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["current_turn_id"], "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-new"), [binding])
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-new")
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(stored["working_dir"], "/tmp/project-new")
        self.assertEqual(len(owner_losses), 1)
        self.assertEqual(owner_losses[0].binding, binding)
        self.assertEqual(owner_losses[0].thread_id, "thread-old")
        self.assertEqual(owner_losses[0].reason, "binding_replaced")
        self.assertEqual(owner_losses[0].disposition, "abandon")

    def test_bind_thread_locked_rolls_back_when_lifecycle_projection_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")
        set_execution_page_state(state, current_message_id="card-live")
        state["current_turn_id"] = "turn-1"

        with patch.object(
            manager._lifecycle,
            "project_after_bind_locked",
            side_effect=RuntimeError("after bind failed"),
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "after bind failed"):
                    manager.bind_thread_locked(
                        self._resident_handle_locked(manager, binding),
                        thread_id="thread-new",
                        thread_title="New",
                        working_dir="/tmp/project-new",
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(state["current_thread_title"], "Old")
        self.assertEqual(state["working_dir"], "/tmp/project")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "card-live")
        self.assertEqual(state["current_turn_id"], "turn-1")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-old"), [binding])
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-old"), [binding])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-old", current_binding=binding)["relation"], "current")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-old")
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_owner_loss_callback_blocks_thread_replacement_before_local_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_owner_loss)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old", thread_title="Old")

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "owner settlement failed"):
                manager.bind_thread_locked(
                    self._resident_handle_locked(manager, binding),
                    thread_id="thread-new",
                    thread_title="New",
                    working_dir="/tmp/project-new",
                )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(manager.thread_subscribers("thread-old"), (binding,))
        self.assertEqual(manager.thread_subscribers("thread-new"), ())
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-new"), [])
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-old", current_binding=binding)["relation"],
            "current",
        )
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-old")
        self.assertEqual(owner_losses[0].reason, "binding_replaced")
        self.assertEqual(owner_losses[0].disposition, "abandon")

    def test_clear_thread_binding_locked_clears_attachment_and_keeps_binding_defaults(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        handle = manager.resolve_session(*binding).handle
        set_execution_page_state(
            state,
            current_message_id="card-live",
            last_message_id="card-old",
        )

        with manager._lock:
            result = manager.clear_thread_binding_locked(handle)

        store = ChatBindingStore(data_dir)
        stored = store.load(binding)
        self.assertEqual(result.unsubscribe_thread_id, "thread-1")
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["current_thread_title"], "")
        self.assertEqual(state["feishu_runtime_state"], "")
        self.assertEqual(state["execution_pages"].current_message_id, "")
        self.assertEqual(state["execution_pages"].last_message_id, "")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [])
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1")["kind"], "none")
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "")
        self.assertEqual(stored["feishu_runtime_state"], "")
        self.assertEqual(stored["working_dir"], "/tmp/project")

    def test_clear_thread_binding_locked_rolls_back_when_lifecycle_projection_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        handle = manager.resolve_session(*binding).handle
        set_execution_page_state(state, current_message_id="card-live")

        with patch.object(
            manager._lifecycle,
            "project_thread_cleared_locked",
            side_effect=RuntimeError("clear state failed"),
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "clear state failed"):
                    manager.clear_thread_binding_locked(handle)

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["current_thread_title"], "Demo")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "card-live")
        self.assertEqual(manager.bound_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_owner_loss_callback_blocks_thread_clear_before_local_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_owner_loss)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        handle = manager.resolve_session(*binding).handle
        set_execution_page_state(state, current_message_id="card-live")

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "owner settlement failed"):
                manager.clear_thread_binding_locked(
                    handle,
                )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["current_thread_id"], "thread-1")
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "card-live")
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"],
            "current",
        )
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")
        self.assertEqual(owner_losses[0].reason, "binding_cleared")

    def test_owner_loss_callback_preflights_batch_deactivation_before_any_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        owner_losses = []

        def _fail_second_owner_loss(event) -> None:
            owner_losses.append(event)
            if event.binding == binding_b:
                raise RuntimeError("second owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_second_owner_loss)
        state_a = self._attach_binding(manager, binding_a, thread_id="thread-a")
        state_b = self._attach_binding(manager, binding_b, thread_id="thread-b")

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "second owner settlement failed"):
                manager.deactivate_bindings_with_receipts_locked(
                    (binding_a, binding_b)
                )

        stored_a = ChatBindingStore(data_dir).load(binding_a)
        stored_b = ChatBindingStore(data_dir).load(binding_b)
        self.assertEqual([event.binding for event in owner_losses], [binding_a, binding_b])
        self.assertEqual(state_a["current_thread_id"], "thread-a")
        self.assertEqual(state_b["current_thread_id"], "thread-b")
        self.assertEqual(manager.thread_subscribers("thread-a"), (binding_a,))
        self.assertEqual(manager.thread_subscribers("thread-b"), (binding_b,))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-a", current_binding=binding_a)["kind"],
            "none",
        )
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-b", current_binding=binding_b)["relation"],
            "current",
        )
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["current_thread_id"], "thread-a")
        self.assertEqual(stored_b["current_thread_id"], "thread-b")

    def test_sync_stored_binding_locked_clears_fresh_default_binding(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._sync_resident_state_locked(binding, state)

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
                        self._resident_handle_locked(manager, binding),
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
        set_execution_page_state(state, current_message_id="card-live")

        with patch.object(manager._chat_binding_store, "save", side_effect=RuntimeError("store save failed")):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "store save failed"):
                    manager.detach_binding_locked(
                        binding,
                    )

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(state["execution_pages"].current_message_id, "card-live")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["kind"], "none")
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")

    def test_detach_binding_locked_restores_binding_when_lease_release_fails(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)

        with patch.object(
            manager._interaction_lease_store,
            "release_if_matches",
            side_effect=RuntimeError("lease release failed"),
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "lease release failed"):
                    manager.detach_binding_locked(binding)

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding,
            )["relation"],
            "current",
        )

    def test_thread_detach_treats_nonwriter_binding_owner_loss_as_exact_noop(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        manager = self._make_manager(data_dir=data_dir)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(
            manager,
            binding,
            acquire_interaction_owner=False,
        )
        manager.acquire_interaction_lease_for_binding(
            ("ou-unrelated", "chat-other"),
            "thread-1",
        )

        with manager._lock:
            result = manager.detach_thread_bindings_locked(
                "thread-1",
                detach_availability=lambda _thread_id: (True, ""),
            )

        stored = ChatBindingStore(data_dir).load(binding)
        assert stored is not None
        self.assertEqual(result.detached_binding_ids, ["p2p:ou-user:chat-1"])
        self.assertEqual(state["feishu_runtime_state"], "detached")
        self.assertEqual(stored["feishu_runtime_state"], "detached")
        self.assertEqual(manager.thread_subscribers("thread-1"), ())
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1")["binding_id"],
            "p2p:ou-unrelated:chat-other",
        )

    def test_owner_loss_callback_blocks_single_detach_before_local_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_owner_loss)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "owner settlement failed"):
                manager.detach_binding_locked(binding)

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(state["feishu_runtime_state"], "attached")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding])
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding,))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"],
            "current",
        )
        assert stored is not None
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(owner_losses[0].reason, "binding_detached")

    def test_owner_loss_callback_preflights_batch_detach_before_any_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding_a = ("ou-user-a", "chat-a")
        binding_b = ("ou-user-b", "chat-b")
        owner_losses = []

        def _fail_second_owner_loss(event) -> None:
            owner_losses.append(event)
            if event.binding == binding_b:
                raise RuntimeError("second owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_second_owner_loss)
        state_a = self._attach_binding(manager, binding_a)
        state_b = self._attach_binding(
            manager,
            binding_b,
            acquire_interaction_owner=False,
        )

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "second owner settlement failed"):
                manager.detach_thread_bindings_locked(
                    "thread-1",
                    detach_availability=lambda thread_id: (True, ""),
                )

        stored_a = ChatBindingStore(data_dir).load(binding_a)
        stored_b = ChatBindingStore(data_dir).load(binding_b)
        self.assertEqual([event.binding for event in owner_losses], [binding_a, binding_b])
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(manager.thread_subscribers("thread-1"), (binding_a, binding_b))
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding_a)["kind"],
            "none",
        )
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["feishu_runtime_state"], "attached")
        self.assertEqual(stored_b["feishu_runtime_state"], "attached")

    def test_owner_loss_callback_blocks_hydration_downgrade_before_local_cleanup(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        source_manager = self._make_manager(data_dir=data_dir)
        self._attach_binding(source_manager, binding)
        owner_losses = []

        def _fail_owner_loss(event) -> None:
            owner_losses.append(event)
            raise RuntimeError("owner settlement failed")

        manager = self._make_manager(data_dir=data_dir, owner_loss_observer=_fail_owner_loss)
        with self.assertRaisesRegex(RuntimeError, "owner settlement failed"):
            manager.hydrate_stored_bindings()

        stored = ChatBindingStore(data_dir).load(binding)
        self.assertEqual(manager.binding_keys_locked(), ())
        self.assertEqual(manager.thread_subscribers("thread-1"), ())
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding)["relation"],
            "current",
        )
        assert stored is not None
        self.assertEqual(stored["current_thread_id"], "thread-1")
        self.assertEqual(stored["feishu_runtime_state"], "attached")
        self.assertEqual(owner_losses[0].reason, "binding_hydrated")
        self.assertEqual(owner_losses[0].disposition, "abandon")

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

    def test_hydrate_stored_binding_rejects_unknown_policy_or_profile(self) -> None:
        manager = self._make_manager()
        base_binding = {
            "working_dir": "",
            "current_thread_id": "",
            "current_thread_title": "",
            "feishu_runtime_state": "",
            "approval_policy": "on-request",
            "permissions_profile_id": ":workspace",
            "model": "",
            "reasoning_effort": "",
            "configured_settings": [],
        }
        for field, value in (
            ("approval_policy", "future-policy"),
            ("permissions_profile_id", ":future-profile"),
        ):
            with self.subTest(field=field):
                state = manager.build_default_runtime_state()
                stored_binding = dict(base_binding)
                stored_binding[field] = value
                with manager._lock:
                    with self.assertRaises(ValueError):
                        manager.hydrate_stored_binding_locked(state, stored_binding)

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
        state = self._resident_state(manager, binding)

        self.assertNotIn("collaboration_mode", state)

    def test_unbound_model_auto_persists_across_default_changes(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._apply_persisted_runtime_state_message_locked(
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
        restarted_state = self._resident_state(restarted, binding)
        self.assertEqual(restarted_state["model"], "")
        self.assertEqual(restarted_state["permissions_profile_id"], ":workspace")

    def test_explicit_default_approval_and_permissions_persist_across_default_changes(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._apply_persisted_runtime_state_message_locked(
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
        restarted_state = self._resident_state(restarted, binding)
        self.assertEqual(restarted_state["approval_policy"], "on-request")
        self.assertEqual(restarted_state["permissions_profile_id"], ":workspace")

    def test_configured_unbound_binding_is_visible_in_status_and_inventory(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        binding = ("ou-user", "chat-1")
        manager = self._make_manager(data_dir=data_dir)
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._apply_persisted_runtime_state_message_locked(
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
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._apply_persisted_runtime_state_message_locked(
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
                self._resident_handle_locked(manager, binding),
                thread_id="thread-1",
                thread_title="Demo",
                working_dir="/tmp/default",
            )
            manager.clear_thread_binding_locked(manager.resolve_session(*binding).handle)
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
        state = self._resident_state(manager, binding)

        with manager._lock:
            manager._apply_persisted_runtime_state_message_locked(
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
        restarted_state = self._resident_state(restarted, binding)
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
        set_execution_page_state(state_a, current_message_id="card-a")
        set_execution_page_state(state_b, current_message_id="card-b")

        with manager._lock:
            result = manager.detach_thread_bindings_locked(
                "thread-1",
                detach_availability=lambda thread_id: (True, ""),
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
        self.assertEqual(state_a["execution_pages"].current_message_id, "card-a")
        self.assertEqual(state_b["execution_pages"].current_message_id, "card-b")
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
        set_execution_page_state(state_a, current_message_id="card-a")
        set_execution_page_state(state_b, current_message_id="card-b")

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
                    )

        store = ChatBindingStore(data_dir)
        stored_a = store.load(binding_a)
        stored_b = store.load(binding_b)
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(state_a["execution_pages"].current_message_id, "card-a")
        self.assertEqual(state_b["execution_pages"].current_message_id, "card-b")
        self.assertEqual(manager.attached_bindings_for_thread_locked("thread-1"), [binding_a, binding_b])
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1", current_binding=binding_a)["kind"],
            "none",
        )
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(stored_a["feishu_runtime_state"], "attached")
        self.assertEqual(stored_b["feishu_runtime_state"], "attached")

    def test_detach_thread_bindings_locked_retains_failed_lease_binding_for_retry(self) -> None:
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

        original_release = manager._interaction_lease_store.release_if_matches
        release_attempts = 0

        def fail_owner_release_once(expected):
            nonlocal release_attempts
            release_attempts += 1
            if release_attempts == 1:
                raise RuntimeError("lease release failed")
            return original_release(expected)

        with patch.object(
            manager._interaction_lease_store,
            "release_if_matches",
            side_effect=fail_owner_release_once,
        ):
            with manager._lock:
                with self.assertRaisesRegex(RuntimeError, "lease release failed"):
                    manager.detach_thread_bindings_locked(
                        "thread-1",
                        detach_availability=lambda _thread_id: (True, ""),
                    )

        stored_a = ChatBindingStore(data_dir).load(binding_a)
        stored_b = ChatBindingStore(data_dir).load(binding_b)
        assert stored_a is not None
        assert stored_b is not None
        self.assertEqual(state_a["feishu_runtime_state"], "attached")
        self.assertEqual(stored_a["feishu_runtime_state"], "attached")
        self.assertEqual(state_b["feishu_runtime_state"], "attached")
        self.assertEqual(stored_b["feishu_runtime_state"], "attached")
        self.assertEqual(
            manager.interaction_owner_snapshot_locked(
                "thread-1",
                current_binding=binding_a,
            )["relation"],
            "current",
        )

        with manager._lock:
            retried = manager.detach_thread_bindings_locked(
                "thread-1",
                detach_availability=lambda _thread_id: (True, ""),
            )

        self.assertEqual(
            sorted(retried.detached_binding_ids),
            ["p2p:ou-user-a:chat-a", "p2p:ou-user-b:chat-b"],
        )
        self.assertEqual(state_a["feishu_runtime_state"], "detached")
        self.assertEqual(
            manager.interaction_owner_snapshot_locked("thread-1")["kind"],
            "none",
        )

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

    def test_batch_hydration_rejects_later_settler_reentry_before_overwrite(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = pathlib.Path(temporary.name)
        binding_a = ("ou-a", "chat-a")
        binding_b = ("ou-b", "chat-b")
        source = self._make_manager(data_dir=data_dir)
        self._attach_binding(source, binding_a, thread_id="thread-a")
        self._attach_binding(source, binding_b, thread_id="thread-b")

        manager: BindingRuntimeManager
        reentered = False

        def reenter_on_second(command) -> None:
            nonlocal reentered
            if command.binding != binding_b or reentered:
                return
            reentered = True
            # Finish the earlier binding's exact hydration from inside the
            # later settler.  The outer batch must not overwrite this newer
            # resident generation with its stale plan.
            manager._get_or_create_runtime_state_locked(binding_a)

        manager = self._make_manager(
            data_dir=data_dir,
            owner_loss_observer=reenter_on_second,
        )

        with self.assertRaisesRegex(RuntimeError, "过期或被替换"):
            manager.hydrate_stored_bindings()

        snapshot_a = manager.binding_runtime_snapshot_locked(binding_a)
        self.assertIsNotNone(snapshot_a)
        assert snapshot_a is not None
        self.assertEqual(snapshot_a.thread_id, "thread-a")
        self.assertEqual(snapshot_a.feishu_runtime_state, "detached")
        self.assertIsNone(manager.binding_runtime_snapshot_locked(binding_b))

    def test_detach_preflight_revalidates_every_owner_before_issuing_receipt(self) -> None:
        manager: BindingRuntimeManager
        binding_a = ("ou-a", "chat-a")
        binding_b = ("ou-b", "chat-b")
        first_command = None

        def advance_first_owner_during_second(command) -> None:
            nonlocal first_command
            if command.binding == binding_a:
                first_command = command
                return
            assert first_command is not None
            manager._advance_binding_owner_revision_locked(
                binding_a,
                settled_command=first_command,
            )

        manager = self._make_manager(
            owner_loss_observer=advance_first_owner_during_second,
        )
        self._attach_binding(manager, binding_a, thread_id="thread-shared")
        self._attach_binding(
            manager,
            binding_b,
            thread_id="thread-shared",
            acquire_interaction_owner=False,
        )

        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "过期或被替换"):
                manager.preflight_detach_thread_bindings_locked(
                    "thread-shared",
                    detach_availability=lambda _thread_id: (True, ""),
                )

        self.assertEqual(
            manager.attached_bindings_for_thread_locked("thread-shared"),
            [binding_a, binding_b],
        )

    def test_same_thread_bind_cannot_skip_retryable_owner_loss(self) -> None:
        should_fail = True

        def fail_once(_command) -> None:
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise RuntimeError("owner settlement uncertain")

        manager = self._make_manager(owner_loss_observer=fail_once)
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        handle = manager.resolve_session(*binding).handle
        with manager._lock:
            with self.assertRaisesRegex(RuntimeError, "owner settlement uncertain"):
                manager.clear_thread_binding_locked(handle)
            with self.assertRaisesRegex(RuntimeError, "未完成的 owner-loss"):
                manager.bind_thread_locked(
                    self._resident_handle_locked(manager, binding),
                    thread_id="thread-1",
                    thread_title="must-not-advance",
                    working_dir="/tmp/project",
                )
            manager.clear_thread_binding_locked(handle)

        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(manager.thread_subscribers("thread-1"), ())

    def test_bind_timer_effect_is_deferred_until_after_commit(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old")
        timer = Mock()
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ExecutionPatchTimerTicket(
                binding=binding,
                thread_id="thread-old",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=timer,
        )

        with manager._lock:
            result = manager.bind_thread_locked(
                self._resident_handle_locked(manager, binding),
                thread_id="thread-new",
                thread_title="New",
                working_dir="/tmp/project",
            )
            timer.cancel.assert_not_called()
            self.assertIsNone(state["patch_timer_registration"])

        cancel_runtime_timer_effects(result.timer_cancellations)
        timer.cancel.assert_called_once_with()

        stored = manager._chat_binding_store.load(binding)
        assert stored is not None
        self.assertEqual(state["current_thread_id"], "thread-new")
        self.assertEqual(stored["current_thread_id"], "thread-new")
        self.assertEqual(manager.thread_subscribers("thread-old"), ())
        self.assertEqual(manager.thread_subscribers("thread-new"), (binding,))

    def test_clear_timer_effect_is_deferred_until_after_commit(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding)
        handle = manager.resolve_session(*binding).handle
        timer = Mock()
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=ExecutionPatchTimerTicket(
                binding=binding,
                thread_id="thread-1",
                card_message_id="card-1",
                turn_id="turn-1",
            ),
            timer=timer,
        )

        with manager._lock:
            result = manager.clear_thread_binding_locked(handle)
            timer.cancel.assert_not_called()
            self.assertIsNone(state["patch_timer_registration"])

        cancel_runtime_timer_effects(result.timer_cancellations)
        timer.cancel.assert_called_once_with()

        stored = manager._chat_binding_store.load(binding)
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(state["feishu_runtime_state"], "")
        self.assertEqual(manager.thread_subscribers("thread-1"), ())
        if stored is not None:
            self.assertEqual(stored["current_thread_id"], "")

    def test_deferred_timer_effect_does_not_clear_replacement_registration(self) -> None:
        manager = self._make_manager()
        binding = ("ou-user", "chat-1")
        state = self._attach_binding(manager, binding, thread_id="thread-old")
        old_timer = Mock()
        old_ticket = ExecutionPatchTimerTicket(
            binding=binding,
            thread_id="thread-old",
            card_message_id="card-old",
            turn_id="turn-old",
        )
        state["patch_timer_registration"] = ExecutionPatchTimerRegistration(
            ticket=old_ticket,
            timer=old_timer,
        )
        with manager._lock:
            result = manager.bind_thread_locked(
                self._resident_handle_locked(manager, binding),
                thread_id="thread-new",
                thread_title="New",
                working_dir="/tmp/project",
            )
            new_timer = Mock()
            replacement = ExecutionPatchTimerRegistration(
                ticket=ExecutionPatchTimerTicket(
                    binding=binding,
                    thread_id="thread-new",
                    card_message_id="card-new",
                    turn_id="turn-new",
                ),
                timer=new_timer,
            )
            state["patch_timer_registration"] = replacement

        cancel_runtime_timer_effects(result.timer_cancellations)

        old_timer.cancel.assert_called_once_with()
        new_timer.cancel.assert_not_called()
        self.assertIs(state["patch_timer_registration"], replacement)
