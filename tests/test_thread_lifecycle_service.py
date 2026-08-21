import os
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.binding_runtime_contract import BindingOwnerLossSettlementReceipt
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.codex_protocol.client import CodexRpcError
from bot.reason_codes import ReasonedCheck
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLease,
    ThreadRuntimeLeaseHolder,
)
from bot.thread_lifecycle_service import (
    ThreadLifecycleAdmissionPort,
    ThreadLifecycleBackendPort,
    ThreadLifecycleCleanupPort,
    ThreadLifecyclePolicyError,
    ThreadLifecyclePorts,
    ThreadLifecycleService,
)
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


class _LifecycleHarness:
    def __init__(self, root: pathlib.Path) -> None:
        self.lock = threading.RLock()
        self.interaction_leases = InteractionLeaseStore(root)
        self.binding_store = ChatBindingStore(root)
        self.owner_losses = []
        self._owner_loss_transaction_nonce = 0
        self.binding_runtime = BindingRuntimeManager(
            lock=self.lock,
            default_working_dir="/tmp/default",
            default_approval_policy="on-request",
            default_permissions_profile_id="danger-full-access",
            default_model="gpt-5.4",
            default_reasoning_effort="medium",
            chat_binding_store=self.binding_store,
            thread_subscription_registry=ThreadSubscriptionRegistry(),
            interaction_lease_store=self.interaction_leases,
            is_group_chat=lambda _chat_id, _message_id: False,
            owner_loss_settler=self.settle_owner_loss,
        )
        self.summaries: dict[str, ThreadSummary] = {}
        self.loaded_thread_ids: list[str] = []
        self.runtime_leases: dict[str, ThreadRuntimeLease] = {}
        self.pending_bindings: set[tuple[str, str]] = set()
        self.writer_check = ReasonedCheck.allow()
        self.loaded_gate_check = ReasonedCheck.allow()
        self.loaded_gate_calls: list[tuple[str, str]] = []
        self.archive_error: Exception | None = None
        self.unarchive_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.archived: list[str] = []
        self.unarchived: list[str] = []
        self.deleted: list[str] = []
        self.unsubscribed: list[str] = []
        self.released_runtime_leases: list[str] = []
        self.cleared_queues: list[tuple[str, str]] = []
        self.service = ThreadLifecycleService(
            lock=self.lock,
            binding_runtime=self.binding_runtime,
            ports=ThreadLifecyclePorts(
                backend=ThreadLifecycleBackendPort(
                    read_thread=self.read_thread,
                    list_loaded_thread_ids=lambda: list(self.loaded_thread_ids),
                    archive_thread=self.archive_thread,
                    unarchive_thread=self.unarchive_thread,
                    delete_thread=self.delete_thread,
                    is_thread_not_found_error=lambda _exc: False,
                    is_thread_not_loaded_error=lambda _exc: False,
                ),
                admission=ThreadLifecycleAdmissionPort(
                    instance_name=lambda: "focus-a",
                    load_runtime_lease=self.runtime_leases.get,
                    external_write_denial_check=(
                        lambda _thread_id, _writer_holder=None: self.writer_check
                    ),
                    loaded_gate_check=self.check_loaded_gate,
                ),
                cleanup=ThreadLifecycleCleanupPort(
                    binding_has_pending_request_locked=(
                        lambda binding: binding in self.pending_bindings
                    ),
                    invalidate_feishu_execution_queue_locked=self.cleared_queues.append,
                    unsubscribe_thread=self.unsubscribed.append,
                    release_service_runtime_lease=self.released_runtime_leases.append,
                ),
            ),
        )

    def settle_owner_loss(self, command):
        self.owner_losses.append(command)
        lease = self.interaction_leases.load(command.thread_id)
        expected_holder = make_feishu_interaction_holder(
            command.binding[0],
            command.binding[1],
            owner_pid=os.getpid(),
        )
        if lease is not None and lease.holder.same_holder(expected_holder):
            try:
                released = self.interaction_leases.release_if_matches(lease)
            except Exception as exc:
                raise RuntimeError(f"lease release failed: {exc}") from exc
            if released is not True:
                raise RuntimeError("lease release failed")
        self._owner_loss_transaction_nonce += 1
        return BindingOwnerLossSettlementReceipt(
            command=command,
            _settler_nonce=1,
            _transaction_nonce=self._owner_loss_transaction_nonce,
        )

    @staticmethod
    def summary(
        thread_id: str = "thread-1",
        *,
        status: str = "idle",
        subagent_kind: str | None = None,
    ) -> ThreadSummary:
        return ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status=status,
            subagent_kind=subagent_kind,
        )

    def read_thread(self, thread_id: str) -> ThreadSnapshot:
        return ThreadSnapshot(summary=self.summaries[thread_id])

    def check_loaded_gate(self, thread_id: str, operation: str) -> ReasonedCheck:
        self.loaded_gate_calls.append((thread_id, operation))
        return self.loaded_gate_check

    def archive_thread(self, thread_id: str) -> None:
        if self.archive_error is not None:
            raise self.archive_error
        self.archived.append(thread_id)

    def unarchive_thread(self, thread_id: str) -> ThreadSummary:
        if self.unarchive_error is not None:
            raise self.unarchive_error
        self.unarchived.append(thread_id)
        return self.summaries[thread_id]

    def delete_thread(self, thread_id: str) -> None:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(thread_id)

    def bind(self, binding: tuple[str, str], thread_id: str = "thread-1") -> None:
        with self.lock:
            self.binding_runtime._get_or_create_runtime_state_locked(binding)
            session = self.binding_runtime.resident_session_snapshot_locked(binding)
            assert session is not None
            self.binding_runtime.bind_thread_locked(
                session.handle,
                thread_id=thread_id,
                thread_title="demo",
                working_dir="/tmp/project",
            )


class ThreadLifecycleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.harness = _LifecycleHarness(pathlib.Path(temporary_directory.name))
        self.harness.summaries["thread-1"] = self.harness.summary()

    def test_direct_thread_gate_rejects_parent_owned_child_before_mutation(
        self,
    ) -> None:
        self.harness.summaries["child-1"] = self.harness.summary(
            "child-1",
            subagent_kind="threadSpawn",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "ThreadSpawn"):
            self.harness.service.archive_thread_for_control("child-1")

        self.assertEqual(self.harness.archived, [])

    def test_writer_gate_rejects_before_loaded_gate_or_mutation(self) -> None:
        self.harness.writer_check = ReasonedCheck.deny(
            "owned_elsewhere",
            "another frontend owns this root",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "another frontend"):
            self.harness.service.delete_thread_for_control("thread-1")

        self.assertEqual(self.harness.loaded_gate_calls, [])
        self.assertEqual(self.harness.deleted, [])

    def test_loaded_gates_fail_closed_before_upstream_mutation(self) -> None:
        self.harness.loaded_gate_check = ReasonedCheck.deny(
            "loaded_elsewhere",
            "another instance still has this thread loaded",
        )

        with self.assertRaisesRegex(ThreadLifecyclePolicyError, "another instance"):
            self.harness.service.archive_thread_for_control("thread-1")

        self.assertEqual(self.harness.archived, [])

        self.harness.loaded_gate_check = ReasonedCheck.allow()
        self.harness.loaded_thread_ids.append("thread-1")
        with self.assertRaisesRegex(
            ThreadLifecyclePolicyError, "仍将该 thread 保持为 loaded"
        ):
            self.harness.service.unarchive_thread_for_control("thread-1")

        self.assertEqual(self.harness.unarchived, [])

    def test_non_service_runtime_holder_blocks_archive_and_delete(self) -> None:
        self.harness.runtime_leases["thread-1"] = ThreadRuntimeLease(
            thread_id="thread-1",
            owner_instance="focus-a",
            owner_service_token="service-token",
            control_endpoint="tcp://127.0.0.1:32001",
            backend_url="ws://127.0.0.1:8765",
            attached_at=1.0,
            holders=(
                ThreadRuntimeLeaseHolder(
                    holder_id="fcodex:123",
                    holder_type="fcodex",
                    instance_name="focus-a",
                    owner_pid=123,
                    owner_service_token="service-token",
                    control_endpoint="tcp://127.0.0.1:32001",
                    backend_url="ws://127.0.0.1:8765",
                    updated_at=1.0,
                ),
            ),
        )

        for operation, mutate in (
            ("archive", self.harness.service.archive_thread_for_control),
            ("delete", self.harness.service.delete_thread_for_control),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    ThreadLifecyclePolicyError, "fcodex@focus-a"
                ):
                    mutate("thread-1")

        self.assertEqual(self.harness.archived, [])
        self.assertEqual(self.harness.deleted, [])

    def test_archive_success_commits_local_cleanup_after_upstream_success(self) -> None:
        binding = ("ou_user", "chat-1")
        self.harness.bind(binding)

        result = self.harness.service.archive_thread_for_control("thread-1")

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "complete")
        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_user:chat-1"])
        self.assertEqual(self.harness.archived, ["thread-1"])
        self.assertEqual(self.harness.cleared_queues, [binding])
        self.assertEqual(self.harness.unsubscribed, ["thread-1"])
        self.assertEqual(self.harness.released_runtime_leases, ["thread-1"])
        with self.harness.lock:
            self.assertIsNone(
                self.harness.binding_runtime.binding_runtime_snapshot_locked(binding)
            )

    def test_archive_target_does_not_touch_unrelated_store_only_recovery_binding(
        self,
    ) -> None:
        target_binding = ("ou_target", "chat-target")
        recovery_binding = ("ou_recovery", "chat-recovery")
        self.harness.bind(target_binding)
        self.harness.binding_store.save(
            recovery_binding,
            {
                "working_dir": "/tmp/recovery",
                "current_thread_id": "thread-recovery",
                "current_thread_title": "Recovery",
                "feishu_runtime_state": "attached",
                "approval_policy": "never",
                "permissions_profile_id": "danger-full-access",
                "model": "",
                "reasoning_effort": "",
                "configured_settings": [],
            },
        )
        self.harness.binding_runtime.acquire_interaction_lease_for_binding(
            recovery_binding,
            "thread-recovery",
        )

        result = self.harness.service.archive_thread_for_control("thread-1")

        self.assertEqual(result["cleared_binding_ids"], ["p2p:ou_target:chat-target"])
        self.assertIsNone(
            self.harness.binding_runtime.binding_runtime_snapshot_locked(
                recovery_binding
            )
        )
        stored_recovery = self.harness.binding_store.load(recovery_binding)
        assert stored_recovery is not None
        self.assertEqual(stored_recovery["feishu_runtime_state"], "attached")
        self.assertEqual(
            self.harness.binding_runtime.interaction_owner_snapshot_locked(
                "thread-recovery",
                current_binding=recovery_binding,
            )["relation"],
            "current",
        )
        self.assertEqual(
            self.harness.binding_runtime.thread_subscribers("thread-recovery"),
            (),
        )
        self.assertFalse(
            any(
                event.binding == recovery_binding
                for event in self.harness.owner_losses
            )
        )

    def test_transport_unknown_and_explicit_rpc_error_are_classified_without_cleanup(
        self,
    ) -> None:
        binding = ("ou_user", "chat-1")
        self.harness.bind(binding)
        self.harness.archive_error = TimeoutError("archive response timed out")

        unknown = self.harness.service.archive_thread_for_control("thread-1")

        self.assertEqual(unknown["upstream_outcome"], "unknown")
        self.assertEqual(unknown["focus_cleanup"], "skipped")
        self.assertIn("timed out", unknown["outcome_detail"])
        self.assertEqual(self.harness.unsubscribed, [])
        self.assertEqual(self.harness.released_runtime_leases, [])
        with self.harness.lock:
            self.assertIsNotNone(
                self.harness.binding_runtime.binding_runtime_snapshot_locked(binding)
            )

        self.harness.delete_error = CodexRpcError(
            "thread/delete",
            {"message": "delete refused"},
        )
        explicit_error = self.harness.service.delete_thread_for_control("thread-1")

        self.assertEqual(explicit_error["upstream_outcome"], "error")
        self.assertEqual(explicit_error["upstream_error"], "delete refused")
        self.assertEqual(explicit_error["focus_cleanup"], "skipped")

    def test_cleanup_failure_is_reported_incomplete_and_retains_local_authority(
        self,
    ) -> None:
        binding = ("ou_user", "chat-1")
        self.harness.bind(binding)
        self.harness.binding_runtime.acquire_interaction_lease_for_binding(
            binding,
            "thread-1",
        )

        with patch.object(
            self.harness.interaction_leases,
            "release_if_matches",
            side_effect=OSError("lease cleanup failed"),
        ):
            result = self.harness.service.archive_thread_for_control("thread-1")

        self.assertEqual(result["upstream_outcome"], "success")
        self.assertEqual(result["focus_cleanup"], "incomplete")
        self.assertIn("lease cleanup failed", result["cleanup_errors"][0])
        self.assertEqual(result["cleared_binding_ids"], [])
        self.assertEqual(self.harness.unsubscribed, [])
        self.assertEqual(self.harness.released_runtime_leases, [])
        with self.harness.lock:
            self.assertIsNotNone(
                self.harness.binding_runtime.binding_runtime_snapshot_locked(binding)
            )


if __name__ == "__main__":
    unittest.main()
