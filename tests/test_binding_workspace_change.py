import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.binding_runtime_contract import BindingOwnerLossSettlementReceipt
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.stores.pending_attachment_store import PendingAttachmentRecord, PendingAttachmentStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


class PendingAttachmentWorkspaceCleanupTests(unittest.TestCase):
    @staticmethod
    def _record(
        local_path: pathlib.Path,
        *,
        message_id: str,
        sender_id: str = "ou-user",
        expires_at: float = 200.0,
    ) -> PendingAttachmentRecord:
        return PendingAttachmentRecord(
            sender_id=sender_id,
            chat_id="chat-1",
            thread_id="",
            message_id=message_id,
            attachment_type="file",
            resource_key=message_id,
            display_name=f"{message_id}.txt",
            local_path=str(local_path),
            created_at=100.0,
            expires_at=expires_at,
        )

    def test_cleanup_uses_pending_records_as_durable_workspace_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            store = PendingAttachmentStore(root)
            current_stage = root / "new" / "_feishu_attachments"
            records = (
                self._record(root / "old" / "_feishu_attachments" / "old.txt", message_id="old"),
                self._record(current_stage / "current.txt", message_id="current"),
                self._record(
                    root / "old" / "_feishu_attachments" / "other.txt",
                    message_id="other",
                    sender_id="ou-other",
                ),
                self._record(current_stage / "expired.txt", message_id="expired", expires_at=50.0),
            )
            store.add_many(records)

            invalidated, expired = store.take_workspace_mismatches(
                sender_id="ou-user",
                chat_id="chat-1",
                thread_id="",
                expected_stage_dir=current_stage,
                now=150.0,
            )

            self.assertEqual([item.message_id for item in invalidated], ["old"])
            self.assertEqual([item.message_id for item in expired], ["expired"])
            self.assertEqual(
                [item.message_id for item in store.list_all()],
                ["other", "current"],
            )

    def test_failed_cleanup_write_preserves_records_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = pathlib.Path(tempdir)
            store = PendingAttachmentStore(root)
            record = self._record(
                root / "old" / "_feishu_attachments" / "old.txt",
                message_id="old",
            )
            store.add(record)

            with (
                patch.object(store, "_write_all", side_effect=OSError("cleanup unavailable")),
                self.assertRaisesRegex(OSError, "cleanup unavailable"),
            ):
                store.take_workspace_mismatches(
                    sender_id="ou-user",
                    chat_id="chat-1",
                    thread_id="",
                    expected_stage_dir=root / "new" / "_feishu_attachments",
                    now=150.0,
                )

            self.assertEqual(store.list_all(), (record,))


class BindingWorkspaceChangeTests(unittest.TestCase):
    def _make_manager(self) -> tuple[BindingRuntimeManager, ChatBindingStore]:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        data_dir = pathlib.Path(tempdir.name)
        store = ChatBindingStore(data_dir)
        settlement_nonce = 0

        def settle_owner_loss(command):
            nonlocal settlement_nonce
            settlement_nonce += 1
            return BindingOwnerLossSettlementReceipt(
                command=command,
                _settler_nonce=1,
                _transaction_nonce=settlement_nonce,
            )

        return BindingRuntimeManager(
            lock=threading.RLock(),
            default_working_dir="/workspace/old",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(data_dir),
            is_group_chat=lambda _chat_id, _message_id: False,
            owner_loss_settler=settle_owner_loss,
        ), store

    @staticmethod
    def _bind_thread(
        manager: BindingRuntimeManager,
        binding: tuple[str, str],
    ):
        session = manager.resolve_session(*binding)
        with manager._lock:
            state = manager.resident_runtime_state_locked(binding)
            assert state is not None
            manager.bind_thread_locked(
                session.handle,
                thread_id="thread-old",
                thread_title="Old",
                working_dir="/workspace/old",
            )
        return manager.resolve_session(*binding), state

    def test_clear_and_workspace_change_use_one_durable_save(self) -> None:
        manager, store = self._make_manager()
        binding = ("ou-user", "chat-1")
        session, state = self._bind_thread(manager, binding)

        with patch.object(store, "save", wraps=store.save) as save:
            with manager._lock:
                result = manager.clear_thread_binding_locked(
                    session.handle,
                    working_dir_after_clear="/workspace/new",
                )

        self.assertEqual(result.unsubscribe_thread_id, "thread-old")
        save.assert_called_once()
        committed = save.call_args.args[1]
        self.assertEqual(committed["working_dir"], "/workspace/new")
        self.assertEqual(committed["current_thread_id"], "")
        self.assertEqual(state["working_dir"], "/workspace/new")
        self.assertEqual(state["current_thread_id"], "")
        self.assertEqual(manager.thread_subscribers("thread-old"), ())
        self.assertEqual(store.load(binding), committed)

    def test_save_failure_preserves_resident_store_and_subscription_then_retries(self) -> None:
        manager, store = self._make_manager()
        binding = ("ou-user", "chat-1")
        session, state = self._bind_thread(manager, binding)
        stored_before = store.load(binding)

        with patch.object(store, "save", side_effect=OSError("save unavailable")):
            with manager._lock, self.assertRaisesRegex(OSError, "save unavailable"):
                manager.clear_thread_binding_locked(
                    session.handle,
                    working_dir_after_clear="/workspace/new",
                )

        self.assertEqual(state["working_dir"], "/workspace/old")
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(manager.thread_subscribers("thread-old"), (binding,))
        self.assertEqual(store.load(binding), stored_before)

        with manager._lock:
            manager.clear_thread_binding_locked(
                session.handle,
                working_dir_after_clear="/workspace/new",
            )
        self.assertEqual(state["working_dir"], "/workspace/new")
        self.assertEqual(state["current_thread_id"], "")

    def test_empty_workspace_is_rejected_before_persistence(self) -> None:
        manager, store = self._make_manager()
        binding = ("ou-user", "chat-1")
        session, state = self._bind_thread(manager, binding)
        stored_before = store.load(binding)

        with patch.object(store, "save", wraps=store.save) as save:
            with manager._lock, self.assertRaisesRegex(ValueError, "working_dir"):
                manager.clear_thread_binding_locked(
                    session.handle,
                    working_dir_after_clear="   ",
                )

        save.assert_not_called()
        self.assertEqual(state["working_dir"], "/workspace/old")
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(store.load(binding), stored_before)

    def test_inflight_fence_is_rechecked_at_the_atomic_owner(self) -> None:
        manager, store = self._make_manager()
        binding = ("ou-user", "chat-1")
        session, state = self._bind_thread(manager, binding)
        state["running"] = True
        stored_before = store.load(binding)

        with manager._lock, self.assertRaisesRegex(RuntimeError, "inflight turn"):
            manager.clear_thread_binding_locked(
                session.handle,
                working_dir_after_clear="/workspace/new",
                require_no_inflight_turn=True,
            )

        self.assertEqual(state["working_dir"], "/workspace/old")
        self.assertEqual(state["current_thread_id"], "thread-old")
        self.assertEqual(store.load(binding), stored_before)


if __name__ == "__main__":
    unittest.main()
