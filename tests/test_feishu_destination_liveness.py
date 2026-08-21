from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lark_oapi.api.im.v1 import (
    P2ImChatDisbandedV1,
    P2ImChatMemberBotDeletedV1,
)

from bot.feishu_bot import FeishuBot
from bot.feishu_destination_liveness import (
    FeishuDestinationLivenessCoordinator,
    FeishuDestinationLivenessPorts,
)
from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossProofType,
    FeishuDestinationLossState,
)
from bot.runtime_admin.binding_clear import (
    RuntimeBindingBatchDeactivationReceipt,
    RuntimeBindingDeactivationReceipt,
)
from bot.runtime_loop import RuntimeLoop
from bot.stores.feishu_app_connection_lease import (
    FeishuAppConnectionLease,
    FeishuAppConnectionLeaseError,
)
from bot.stores.feishu_destination_loss_store import (
    FeishuDestinationLossStore,
    FeishuDestinationLossStoreUnavailable,
)


def _proof(
    source_id: str = "event-1",
    *,
    chat_id: str = "chat-1",
    proof_type: FeishuDestinationLossProofType = (
        FeishuDestinationLossProofType.CHAT_DISBANDED_EVENT
    ),
) -> FeishuDestinationLossProof:
    return FeishuDestinationLossProof(
        source_id=source_id,
        chat_id=chat_id,
        proof_type=proof_type,
    )


class FeishuDestinationLossStoreTests(unittest.TestCase):
    def _data_dir(self) -> pathlib.Path:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return pathlib.Path(tempdir.name)

    def test_acceptance_is_durable_idempotent_and_identity_checked(self) -> None:
        data_dir = self._data_dir()
        store = FeishuDestinationLossStore(data_dir, clock=lambda: 100.0)
        proof = _proof()

        accepted = store.accept(proof)
        replayed = FeishuDestinationLossStore(
            data_dir,
            clock=lambda: 101.0,
        ).accept(proof)

        self.assertEqual(accepted, replayed)
        self.assertEqual(replayed.state, FeishuDestinationLossState.PENDING)
        self.assertEqual(store.pending(), (accepted,))
        with self.assertRaisesRegex(
            FeishuDestinationLossStoreUnavailable,
            "proof_id was reused",
        ):
            store.accept(_proof(chat_id="chat-other"))

        settled = store.settle(proof)
        self.assertEqual(settled.state, FeishuDestinationLossState.SETTLED)
        self.assertEqual(store.accept(proof), settled)
        self.assertEqual(store.pending(), ())

    def test_pending_records_survive_bounded_settled_tombstone_pruning(self) -> None:
        data_dir = self._data_dir()
        timestamp = iter((10.0, 11.0, 20.0, 21.0, 30.0, 31.0, 40.0))
        store = FeishuDestinationLossStore(
            data_dir,
            settled_limit=2,
            clock=lambda: next(timestamp),
        )
        proofs = [_proof(f"event-{index}") for index in range(4)]
        for proof in proofs[:3]:
            store.accept(proof)
            store.settle(proof)
        pending = store.accept(proofs[3])

        self.assertIsNone(store.load(proofs[0].proof_id))
        self.assertIsNotNone(store.load(proofs[1].proof_id))
        self.assertIsNotNone(store.load(proofs[2].proof_id))
        self.assertEqual(store.pending(), (pending,))

    def test_schema_v1_event_is_read_and_rewritten_as_v2_proof(self) -> None:
        data_dir = self._data_dir()
        path = data_dir / "feishu_destination_loss_events.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "records": {
                        "event-legacy": {
                            "event_id": "event-legacy",
                            "chat_id": "chat-legacy",
                            "event_type": "im.chat.disbanded_v1",
                            "state": "pending",
                            "accepted_at": 100.0,
                            "settled_at": None,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        store = FeishuDestinationLossStore(data_dir, clock=lambda: 101.0)

        pending = store.pending()

        self.assertEqual(len(pending), 1)
        proof = pending[0].proof
        self.assertEqual(proof.source_id, "event-legacy")
        self.assertEqual(
            proof.proof_type,
            FeishuDestinationLossProofType.CHAT_DISBANDED_EVENT,
        )
        store.accept(proof)
        rewritten = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rewritten["schema_version"], 2)
        self.assertEqual(tuple(rewritten["records"]), (proof.proof_id,))

    def test_invalid_json_is_not_interpreted_as_an_empty_inbox(self) -> None:
        data_dir = self._data_dir()
        (data_dir / "feishu_destination_loss_events.json").write_text(
            '{"schema_version": 1, "records":',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            FeishuDestinationLossStoreUnavailable,
            "invalid or unreadable JSON",
        ):
            FeishuDestinationLossStore(data_dir).pending()


class FeishuAppConnectionLeaseTests(unittest.TestCase):
    def test_same_app_is_machine_local_singleton_and_release_is_recoverable(
        self,
    ) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        global_data_dir = pathlib.Path(tempdir.name)
        first = FeishuAppConnectionLease(global_data_dir)
        second = FeishuAppConnectionLease(global_data_dir)
        self.addCleanup(first.release)
        self.addCleanup(second.release)

        first.acquire("cli_app", instance_name="default")
        with self.assertRaisesRegex(
            FeishuAppConnectionLeaseError,
            "多个长连接会导致事件随机投递",
        ):
            second.acquire("cli_app", instance_name="other")

        first.release()
        second.acquire("cli_app", instance_name="other")
        self.assertEqual(second.app_id, "cli_app")

    def test_different_apps_can_hold_independent_connection_authority(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        global_data_dir = pathlib.Path(tempdir.name)
        first = FeishuAppConnectionLease(global_data_dir)
        second = FeishuAppConnectionLease(global_data_dir)
        self.addCleanup(first.release)
        self.addCleanup(second.release)

        first.acquire("cli_app_a", instance_name="a")
        second.acquire("cli_app_b", instance_name="b")

        self.assertEqual((first.app_id, second.app_id), ("cli_app_a", "cli_app_b"))


class _IngressBot(FeishuBot):
    def __init__(self) -> None:
        self.proofs: list[FeishuDestinationLossProof] = []
        self.acceptance_error: Exception | None = None

    def on_message(
        self,
        sender_id: str,
        chat_id: str,
        text: str,
        message_id: str = "",
    ) -> None:
        del sender_id, chat_id, text, message_id

    def on_destination_loss_proof(self, proof: FeishuDestinationLossProof) -> None:
        if self.acceptance_error is not None:
            raise self.acceptance_error
        self.proofs.append(proof)


class FeishuDestinationLossIngressTests(unittest.TestCase):
    def test_registered_callbacks_preserve_exact_event_identity(self) -> None:
        bot = _IngressBot()

        bot._on_raw_chat_disbanded(
            P2ImChatDisbandedV1(
                {
                    "header": {"event_id": "event-disbanded"},
                    "event": {"chat_id": "chat-1"},
                }
            )
        )
        bot._on_raw_chat_member_bot_deleted(
            P2ImChatMemberBotDeletedV1(
                {
                    "header": {"event_id": "event-removed"},
                    "event": {"chat_id": "chat-2"},
                }
            )
        )

        self.assertEqual(
            bot.proofs,
            [
                _proof("event-disbanded", chat_id="chat-1"),
                _proof(
                    "event-removed",
                    chat_id="chat-2",
                    proof_type=FeishuDestinationLossProofType.BOT_REMOVED_EVENT,
                ),
            ],
        )

    def test_malformed_event_and_acceptance_failure_propagate_to_sdk_ack(self) -> None:
        bot = _IngressBot()
        malformed = P2ImChatDisbandedV1({"event": {"chat_id": "chat-1"}})

        with self.assertRaisesRegex(ValueError, "source_id cannot be empty"):
            bot._on_raw_chat_disbanded(malformed)

        bot.acceptance_error = RuntimeError("durable inbox unavailable")
        with self.assertRaisesRegex(RuntimeError, "durable inbox unavailable"):
            bot._on_raw_chat_disbanded(
                P2ImChatDisbandedV1(
                    {
                        "header": {"event_id": "event-1"},
                        "event": {"chat_id": "chat-1"},
                    }
                )
            )

class FeishuDestinationLivenessCoordinatorTests(unittest.TestCase):
    def _coordinator(
        self,
        *,
        data_dir: pathlib.Path,
        deactivate,
        effects: list[tuple[str, object]],
        finalize=None,
        retry_delay_seconds: float = 0.01,
    ) -> tuple[
        FeishuDestinationLivenessCoordinator,
        FeishuDestinationLossStore,
        RuntimeLoop,
    ]:
        store = FeishuDestinationLossStore(data_dir)
        runtime = RuntimeLoop(name="destination-liveness-test-runtime")
        lock = threading.RLock()
        coordinator = FeishuDestinationLivenessCoordinator(
            store=store,
            ports=FeishuDestinationLivenessPorts(
                lock=lock,
                runtime_call=runtime.call,
                runtime_context_guard=runtime.assert_worker_context,
                binding_keys_for_chat_locked=lambda chat_id: (("__group__", chat_id),),
                deactivate_bindings_locked=deactivate,
                finalize_deactivated_thread_runtime=(
                    finalize
                    or (
                        lambda thread_id: effects.append(
                            ("finalize", thread_id)
                        )
                    )
                ),
                fail_close_chat_requests=lambda chat_id: effects.append(
                    ("fail_close", chat_id)
                )
                or 2,
                forget_chat_state=lambda chat_id: effects.append(("forget", chat_id)),
            ),
            retry_delay_seconds=retry_delay_seconds,
        )
        self.addCleanup(runtime.stop)
        self.addCleanup(coordinator.shutdown, timeout=1.0)
        return coordinator, store, runtime

    def _data_dir(self) -> pathlib.Path:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return pathlib.Path(tempdir.name)

    @staticmethod
    def _receipt(chat_id: str) -> RuntimeBindingBatchDeactivationReceipt:
        return RuntimeBindingBatchDeactivationReceipt(
            confirmed_removals=(
                RuntimeBindingDeactivationReceipt(
                    binding=("__group__", chat_id),
                    thread_id="thread-1",
                    unsubscribe_thread_id="thread-1",
                ),
            )
        )

    def test_exact_transition_settles_after_all_local_effects(self) -> None:
        effects: list[tuple[str, object]] = []

        def deactivate(bindings):
            effects.append(("deactivate", bindings))
            return self._receipt(bindings[0][1])

        coordinator, store, runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=deactivate,
            effects=effects,
        )
        proof = _proof()
        coordinator.accept(proof)

        changed = runtime.call(coordinator.reconcile_proof_on_runtime, proof)

        self.assertTrue(changed)
        self.assertEqual(
            store.load(proof.proof_id).state, FeishuDestinationLossState.SETTLED
        )
        self.assertEqual(
            effects,
            [
                ("deactivate", (("__group__", "chat-1"),)),
                ("finalize", "thread-1"),
                ("fail_close", "chat-1"),
                ("forget", "chat-1"),
            ],
        )
        self.assertFalse(runtime.call(coordinator.reconcile_proof_on_runtime, proof))

    def test_outbound_and_event_proofs_use_the_same_cleanup_owner(self) -> None:
        effects: list[tuple[str, object]] = []

        def deactivate(bindings):
            effects.append(("deactivate", bindings))
            return self._receipt(bindings[0][1])

        coordinator, store, runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=deactivate,
            effects=effects,
        )
        proof = _proof(
            "attempt-230002",
            proof_type=(
                FeishuDestinationLossProofType.OUTBOUND_BOT_OUTSIDE_CHAT
            ),
        )
        coordinator.accept(proof)

        changed = runtime.call(coordinator.reconcile_proof_on_runtime, proof)

        self.assertTrue(changed)
        record = store.load(proof.proof_id)
        self.assertIsNotNone(record)
        self.assertEqual(record.state, FeishuDestinationLossState.SETTLED)
        self.assertEqual(
            [name for name, _value in effects],
            ["deactivate", "finalize", "fail_close", "forget"],
        )

    def test_runtime_cleanup_failure_does_not_reopen_destination_cleanup(self) -> None:
        effects: list[tuple[str, object]] = []

        def deactivate(bindings):
            effects.append(("deactivate", bindings))
            return self._receipt(bindings[0][1])

        def fail_runtime_cleanup(thread_id: str) -> None:
            effects.append(("finalize", thread_id))
            raise RuntimeError("direct root cannot be verified")

        coordinator, store, runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=deactivate,
            effects=effects,
            finalize=fail_runtime_cleanup,
        )
        proof = _proof()
        coordinator.accept(proof)

        with self.assertLogs("bot.feishu_destination_liveness", level="ERROR"):
            changed = runtime.call(coordinator.reconcile_proof_on_runtime, proof)

        self.assertTrue(changed)
        self.assertEqual(
            store.load(proof.proof_id).state,
            FeishuDestinationLossState.SETTLED,
        )
        self.assertEqual(
            [name for name, _value in effects],
            ["deactivate", "finalize", "fail_close", "forget"],
        )

    def test_unavailable_inbox_degrades_worker_without_blocking_start(self) -> None:
        effects: list[tuple[str, object]] = []
        coordinator, store, _runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=lambda bindings: self._receipt(bindings[0][1]),
            effects=effects,
        )
        unavailable_path = self._data_dir()

        def unavailable_pending():
            raise FeishuDestinationLossStoreUnavailable(
                unavailable_path,
                "corrupt inbox",
            )

        store.pending = unavailable_pending  # type: ignore[method-assign]

        with patch(
            "bot.feishu_destination_liveness.logger.exception"
        ) as report_error:
            coordinator.start()
            activation_continued = True
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                snapshot = coordinator.snapshot()
                if report_error.called:
                    break
                time.sleep(0.01)

        snapshot = coordinator.snapshot()
        self.assertTrue(activation_continued)
        self.assertTrue(report_error.called)
        self.assertTrue(snapshot.worker_running)
        self.assertIsNone(snapshot.pending_proofs)
        self.assertIn("corrupt inbox", snapshot.last_error)

    def test_worker_retries_failed_transition_and_resumes_pending_inbox(self) -> None:
        effects: list[tuple[str, object]] = []
        attempts = 0

        def deactivate(bindings):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient transition failure")
            effects.append(("deactivate", bindings))
            return self._receipt(bindings[0][1])

        coordinator, store, _runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=deactivate,
            effects=effects,
        )
        proof = _proof()
        coordinator.accept(proof)

        with self.assertLogs("bot.feishu_destination_liveness", level="ERROR"):
            coordinator.start()
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                record = store.load(proof.proof_id)
                if (
                    record is not None
                    and record.state is FeishuDestinationLossState.SETTLED
                ):
                    break
                time.sleep(0.01)

        self.assertEqual(
            store.load(proof.proof_id).state, FeishuDestinationLossState.SETTLED
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(coordinator.snapshot().pending_proofs, 0)

    def test_accept_between_empty_read_and_wait_cannot_lose_worker_wakeup(self) -> None:
        effects: list[tuple[str, object]] = []

        def deactivate(bindings):
            effects.append(("deactivate", bindings))
            return self._receipt(bindings[0][1])

        coordinator, store, _runtime = self._coordinator(
            data_dir=self._data_dir(),
            deactivate=deactivate,
            effects=effects,
        )
        original_pending = store.pending
        empty_read = threading.Event()
        release_empty_read = threading.Event()
        self.addCleanup(release_empty_read.set)
        pending_calls = 0

        def pending_with_controlled_gap():
            nonlocal pending_calls
            pending_calls += 1
            result = original_pending()
            if pending_calls == 1:
                empty_read.set()
                release_empty_read.wait(timeout=1.0)
            return result

        store.pending = pending_with_controlled_gap  # type: ignore[method-assign]
        coordinator.start()
        self.assertTrue(empty_read.wait(timeout=1.0))

        proof = _proof()
        coordinator.accept(proof)
        release_empty_read.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            record = store.load(proof.proof_id)
            if (
                record is not None
                and record.state is FeishuDestinationLossState.SETTLED
            ):
                break
            time.sleep(0.01)

        self.assertEqual(
            store.load(proof.proof_id).state, FeishuDestinationLossState.SETTLED
        )
        self.assertEqual(effects[0][0], "deactivate")


if __name__ == "__main__":
    unittest.main()
