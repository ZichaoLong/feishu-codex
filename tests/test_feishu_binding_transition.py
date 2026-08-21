from __future__ import annotations

import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.binding_runtime_contract import BindingOwnerLossSettlementReceipt
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.feishu_binding_transition import (
    BindFeishuThreadCommand,
    ClearFeishuThreadCommand,
    FeishuBindingTransitionChanged,
    FeishuBindingTransitionOwner,
)
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import InteractionLeaseStore
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


class _FakeExecutionQueue:
    def __init__(self) -> None:
        self.invalidated: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def invalidate_binding(self, binding: tuple[str, str]) -> object:
        if self.error is not None:
            raise self.error
        self.invalidated.append(binding)
        return object()


class FeishuBindingTransitionOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.data_dir = pathlib.Path(self._tempdir.name)
        self.lock = threading.RLock()
        self.store = ChatBindingStore(self.data_dir)
        self.queue = _FakeExecutionQueue()
        settlement_nonce = 0

        def settle_owner_loss(command):
            nonlocal settlement_nonce
            settlement_nonce += 1
            return BindingOwnerLossSettlementReceipt(
                command=command,
                _settler_nonce=1,
                _transaction_nonce=settlement_nonce,
            )

        self.manager = BindingRuntimeManager(
            lock=self.lock,
            default_working_dir="/workspace/default",
            default_approval_policy="on-request",
            default_permissions_profile_id=":workspace",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=self.store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=InteractionLeaseStore(self.data_dir),
            is_group_chat=lambda _chat_id, _message_id: False,
            owner_loss_settler=settle_owner_loss,
        )
        self.owner = FeishuBindingTransitionOwner(
            lock=self.lock,
            binding_runtime=self.manager,
            execution_queue=self.queue,
        )
        self.binding = ("ou-user", "chat-1")

    def _bind(
        self,
        thread_id: str,
        *,
        session=None,
        working_dir: str | None = None,
    ):
        captured = session or self.manager.resolve_session(*self.binding)
        return self.owner.bind_thread(
            BindFeishuThreadCommand(
                session=captured,
                thread_id=thread_id,
                thread_title=thread_id,
                working_dir=working_dir,
            )
        )

    def test_replacement_commits_exact_binding_and_invalidates_old_fifo(self) -> None:
        first = self._bind("thread-old", working_dir="/workspace/old")
        self.assertEqual(self.queue.invalidated, [])

        second = self._bind("thread-new", session=first.session)

        self.assertEqual(second.previous_thread_id, "thread-old")
        self.assertEqual(second.session.current_thread_id, "thread-new")
        self.assertEqual(second.session.working_dir, "/workspace/old")
        self.assertEqual(second.unsubscribe_thread_id, "thread-old")
        self.assertEqual(self.queue.invalidated, [self.binding])
        self.assertEqual(self.manager.thread_subscribers("thread-old"), ())
        self.assertEqual(self.manager.thread_subscribers("thread-new"), (self.binding,))
        self.assertEqual(self.store.load(self.binding)["current_thread_id"], "thread-new")

    def test_stale_session_is_rejected_before_mutation(self) -> None:
        first = self._bind("thread-old")
        self._bind("thread-new", session=first.session)

        with self.assertRaisesRegex(
            FeishuBindingTransitionChanged,
            "retired or replaced",
        ):
            self._bind("thread-stale", session=first.session)

        self.assertEqual(
            self.manager.resolve_session(*self.binding).current_thread_id,
            "thread-new",
        )


    def test_persistence_failure_does_not_invalidate_fifo_or_change_resident(self) -> None:
        first = self._bind("thread-old")
        stored_before = self.store.load(self.binding)

        with (
            patch.object(self.store, "save", side_effect=OSError("save unavailable")),
            self.assertRaisesRegex(OSError, "save unavailable"),
        ):
            self._bind("thread-new", session=first.session)

        self.assertEqual(self.queue.invalidated, [])
        self.assertEqual(
            self.manager.resolve_session(*self.binding).current_thread_id,
            "thread-old",
        )
        self.assertEqual(self.store.load(self.binding), stored_before)

    def test_queue_failure_is_postcommit_warning_not_binding_rollback(self) -> None:
        first = self._bind("thread-old")
        self.queue.error = OSError("queue unavailable")

        with self.assertLogs("bot.feishu_binding_transition", level="ERROR"):
            committed = self._bind("thread-new", session=first.session)

        self.assertTrue(committed.queue_cleanup_failed)
        self.assertEqual(committed.session.current_thread_id, "thread-new")
        self.assertEqual(self.store.load(self.binding)["current_thread_id"], "thread-new")

    def test_clear_commits_workspace_and_returns_old_runtime_cleanup(self) -> None:
        first = self._bind("thread-old", working_dir="/workspace/old")

        committed = self.owner.clear_thread(
            ClearFeishuThreadCommand(
                session=first.session,
                working_dir_after_clear="/workspace/new",
            )
        )

        self.assertEqual(committed.previous_thread_id, "thread-old")
        self.assertEqual(committed.unsubscribe_thread_id, "thread-old")
        self.assertEqual(committed.session.current_thread_id, "")
        self.assertEqual(committed.session.working_dir, "/workspace/new")
        self.assertEqual(self.queue.invalidated, [self.binding])
        stored = self.store.load(self.binding)
        self.assertEqual(stored["current_thread_id"], "")
        self.assertEqual(stored["working_dir"], "/workspace/new")

    def test_same_thread_metadata_commit_keeps_fifo_generation(self) -> None:
        first = self._bind("thread-old", working_dir="/workspace/old")

        committed = self.owner.bind_thread(
            BindFeishuThreadCommand(
                session=first.session,
                thread_id="thread-old",
                thread_title="Renamed",
                working_dir="/workspace/new",
            )
        )

        self.assertFalse(committed.queue_cleanup_failed)
        self.assertEqual(self.queue.invalidated, [])
        self.assertEqual(committed.session.current_thread_title, "Renamed")
        self.assertEqual(committed.session.working_dir, "/workspace/new")


if __name__ == "__main__":
    unittest.main()
