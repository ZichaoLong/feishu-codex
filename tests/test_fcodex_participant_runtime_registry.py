"""Direct owner tests for fcodex participant/runtime source accounting."""

from __future__ import annotations

import inspect
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from bot.fcodex.participant_runtime_registry import (
    FcodexParticipantRuntimeRegistry,
    FcodexParticipantRuntimeRegistryPorts,
)
from bot.reason_codes import ReasonedCheck
from bot.runtime_loop import RuntimeLoopContextError
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)


class FcodexParticipantRuntimeRegistryTests(unittest.TestCase):
    participant_id = "fcodex:alice:incarnation-1"

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary_directory.name)
        self.runtime_leases = ThreadRuntimeLeaseStore(self.data_dir)
        self.participant_expiries: list[tuple[str, int, float]] = []
        self.connection_expiries: list[tuple[str, str, int, float]] = []
        self.participant_schedule_error: Exception | None = None
        self.connection_schedule_error: Exception | None = None
        self.loaded_gate = ReasonedCheck.allow()
        self.registry = FcodexParticipantRuntimeRegistry(
            ports=FcodexParticipantRuntimeRegistryPorts(
                thread_runtime_lease_store=self.runtime_leases,
                runtime_holder_for_participant=self._runtime_holder,
                global_loaded_gate=lambda _thread_id: self.loaded_gate,
                schedule_participant_expiry=self._schedule_participant_expiry,
                schedule_connection_expiry=self._schedule_connection_expiry,
            ),
            runtime_context_guard=lambda: None,
            disconnect_grace_seconds=15,
            connection_heartbeat_timeout_seconds=12,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _runtime_holder(self, participant_id: str) -> ThreadRuntimeLeaseHolder:
        return ThreadRuntimeLeaseHolder(
            holder_id=participant_id,
            holder_type="fcodex",
            instance_name="test",
            owner_pid=0,
            owner_service_token="service-token",
            control_endpoint="tcp://127.0.0.1:1",
            backend_url="ws://127.0.0.1:2",
            updated_at=time.time(),
        )

    def _schedule_participant_expiry(
        self,
        participant_id: str,
        generation: int,
        delay: float,
    ) -> None:
        if self.participant_schedule_error is not None:
            raise self.participant_schedule_error
        self.participant_expiries.append((participant_id, generation, delay))

    def _schedule_connection_expiry(
        self,
        participant_id: str,
        connection_id: str,
        generation: int,
        delay: float,
    ) -> None:
        if self.connection_schedule_error is not None:
            raise self.connection_schedule_error
        self.connection_expiries.append(
            (participant_id, connection_id, generation, delay)
        )

    def _connect(self, connection_id: str = "connection-a") -> None:
        self.registry.connect(self.participant_id, connection_id)

    def test_live_connection_source_requires_both_subscription_and_endpoint(self) -> None:
        self.assertFalse(self.registry.has_live_connection_source("root-1"))
        self._connect()
        self.assertFalse(self.registry.has_live_connection_source("root-1"))

        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )

        self.assertTrue(self.registry.has_live_connection_source("root-1"))
        self.assertFalse(self.registry.has_live_connection_source("root-2"))
        self.registry.disconnect(self.participant_id, "connection-a")
        self.assertFalse(self.registry.has_live_connection_source("root-1"))

    def test_backend_reset_close_rejects_old_endpoint_until_reconnect_and_reacquires(
        self,
    ) -> None:
        self._connect()
        stale_expiry = self.connection_expiries[-1]
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).holder_presence,
            "confirmed",
        )

        self.assertEqual(
            self.runtime_leases.purge_all_for_instance(instance_name="test"),
            ["root-1"],
        )
        receipt = self.registry.close_backend_epoch_after_machine_replace()

        self.assertEqual(receipt.participant_ids, (self.participant_id,))
        self.assertEqual(
            receipt.endpoint_ids,
            ((self.participant_id, "connection-a"),),
        )
        self.assertEqual(receipt.source_pairs, ((self.participant_id, "root-1"),))
        self.assertEqual(receipt.holder_pairs, ((self.participant_id, "root-1"),))
        self.assertIsNone(self.registry.snapshot(self.participant_id))
        after_reset = self.registry.source_snapshot(
            self.participant_id,
            "root-1",
        )
        self.assertEqual(after_reset.connection_ids, ())
        self.assertEqual(after_reset.holder_presence, "absent")
        self.assertIsNone(self.runtime_leases.load("root-1"))
        self.assertFalse(
            self.registry.connection_expiry_is_current(*stale_expiry[:3])
        )
        with self.assertRaisesRegex(RuntimeError, "participant 未注册"):
            self.registry.heartbeat(self.participant_id, "connection-a")
        with self.assertRaisesRegex(RuntimeError, "participant 未注册"):
            self.registry.retain_connection_source(
                self.participant_id,
                "connection-a",
                "root-1",
            )

        acquire_calls: list[str] = []
        original_acquire = self.runtime_leases.acquire

        def record_acquire(thread_id, holder):
            acquire_calls.append(thread_id)
            return original_acquire(thread_id, holder)

        self.runtime_leases.acquire = record_acquire  # type: ignore[method-assign]
        try:
            self.registry.connect(self.participant_id, "connection-a")
            self.registry.retain_connection_source(
                self.participant_id,
                "connection-a",
                "root-1",
            )
        finally:
            self.runtime_leases.acquire = original_acquire  # type: ignore[method-assign]

        self.assertEqual(acquire_calls, ["root-1"])
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).holder_presence,
            "confirmed",
        )
        self.assertIsNotNone(self.runtime_leases.load("root-1"))


    def test_backend_reset_does_not_reuse_request_source_generation(self) -> None:
        self._connect()
        old_source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.assertEqual(
            self.runtime_leases.purge_all_for_instance(instance_name="test"),
            ["root-1"],
        )
        self.registry.close_backend_epoch_after_machine_replace()
        self._connect()
        replacement = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )

        self.assertGreater(replacement.generation, old_source.generation)
        stale = self.registry.promote_request_to_unknown(old_source)
        self.assertEqual(stale.outcome, "identity_conflict")
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).pending_request_keys,
            ("request-1",),
        )

    def test_every_public_api_checks_runtime_context_before_effects(self) -> None:
        def reject_context() -> None:
            raise RuntimeLoopContextError("outside RuntimeLoop")

        self.registry._runtime_context_guard = reject_context
        for name, method in inspect.getmembers(self.registry, inspect.ismethod):
            if name.startswith("_"):
                continue
            required = {
                parameter.name: None
                for parameter in inspect.signature(method).parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind
                not in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}
            }
            with self.subTest(api=name):
                with self.assertRaises(RuntimeLoopContextError):
                    method(**required)
        self.assertEqual(self.runtime_leases.list(), [])

    def test_connection_and_participant_expiry_use_exact_generations(self) -> None:
        first = self.registry.connect(self.participant_id, "connection-a")
        first_expiry = self.connection_expiries[-1]
        heartbeat = self.registry.heartbeat(self.participant_id, "connection-a")
        second_expiry = self.connection_expiries[-1]

        self.assertTrue(first.is_new_connection)
        self.assertFalse(heartbeat.is_new_connection)
        self.assertNotEqual(first_expiry[2], second_expiry[2])
        self.assertFalse(self.registry.connection_expiry_is_current(*first_expiry[:3]))
        self.assertTrue(self.registry.connection_expiry_is_current(*second_expiry[:3]))

        disconnected = self.registry.disconnect(self.participant_id, "connection-a")
        participant_expiry = self.participant_expiries[-1]
        self.assertTrue(disconnected.connection_removed)
        self.assertEqual(disconnected.state, "grace")
        self.assertFalse(self.registry.expire_participant(self.participant_id, 0))
        self.assertTrue(self.registry.expire_participant(*participant_expiry[:2]))
        self.assertEqual(self.registry.snapshot(self.participant_id).state, "orphaned")

    def test_reconnect_invalidates_participant_grace_without_reusing_old_endpoint(self) -> None:
        self._connect("connection-a")
        self.registry.disconnect(self.participant_id, "connection-a")
        stale_expiry = self.participant_expiries[-1]

        reconnected = self.registry.connect(self.participant_id, "connection-b")

        self.assertTrue(reconnected.is_new_connection)
        self.assertEqual(reconnected.state, "connected")
        self.assertFalse(self.registry.expire_participant(*stale_expiry[:2]))
        self.assertFalse(
            self.registry.has_live_endpoint(self.participant_id, "connection-a")
        )
        self.assertTrue(
            self.registry.has_live_endpoint(self.participant_id, "connection-b")
        )

    def test_reused_connection_id_cannot_be_expired_by_old_generation(self) -> None:
        self._connect("connection-a")
        first_expiry = self.connection_expiries[-1]
        self.registry.disconnect(self.participant_id, "connection-a")
        self.assertNotIn(
            "connection-a",
            self.registry._participants[
                self.participant_id
            ].connection_expiry_generation,
        )
        self.registry.connect(self.participant_id, "connection-a")
        second_expiry = self.connection_expiries[-1]

        self.assertGreater(second_expiry[2], first_expiry[2])
        self.assertFalse(
            self.registry.connection_expiry_is_current(*first_expiry[:3])
        )
        self.assertTrue(
            self.registry.connection_expiry_is_current(*second_expiry[:3])
        )

    def test_connection_timer_failure_does_not_publish_or_refresh_endpoint(self) -> None:
        self.connection_schedule_error = RuntimeError("timer unavailable")
        with self.assertRaisesRegex(RuntimeError, "timer unavailable"):
            self.registry.connect(self.participant_id, "connection-a")
        self.assertIsNone(self.registry.snapshot(self.participant_id))

        self.connection_schedule_error = None
        self._connect("connection-a")
        current_expiry = self.connection_expiries[-1]
        self.connection_schedule_error = RuntimeError("timer unavailable")
        with self.assertRaisesRegex(RuntimeError, "timer unavailable"):
            self.registry.heartbeat(self.participant_id, "connection-a")
        self.assertTrue(
            self.registry.connection_expiry_is_current(*current_expiry[:3])
        )

    def test_participant_timer_failure_commits_disconnect_as_immediate_orphan(self) -> None:
        self._connect()
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        self.participant_schedule_error = RuntimeError("timer unavailable")

        with self.assertLogs(
            "bot.fcodex.participant_runtime_registry",
            level="ERROR",
        ):
            receipt = self.registry.disconnect(
                self.participant_id,
                "connection-a",
            )

        self.assertEqual(receipt.state, "orphaned")
        self.assertFalse(
            self.registry.has_live_endpoint(self.participant_id, "connection-a")
        )
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).connection_ids,
            (),
        )
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_unknown_or_partial_disconnect_is_idempotent(self) -> None:
        unknown = self.registry.disconnect("fcodex:missing:incarnation", "connection-a")
        self._connect()
        stale = self.registry.disconnect(self.participant_id, "connection-missing")

        self.assertFalse(unknown.participant_known)
        self.assertEqual(unknown.state, "unknown")
        self.assertTrue(stale.participant_known)
        self.assertFalse(stale.connection_removed)
        self.assertEqual(stale.state, "connected")

    def test_one_connection_cannot_release_another_connection_source(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self.registry.retain_connection_source(
            self.participant_id, "connection-a", "root-1"
        )
        self.registry.retain_connection_source(
            self.participant_id, "connection-b", "root-1"
        )

        self.assertTrue(
            self.registry.forget_connection_source(
                self.participant_id, "connection-a", "root-1"
            )
        )
        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.connection_ids, ("connection-b",))
        self.assertIsNotNone(self.runtime_leases.load("root-1"))

        self.registry.disconnect(self.participant_id, "connection-b")
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_one_participant_cannot_release_another_participants_source(self) -> None:
        second_participant_id = "fcodex:bob:incarnation-2"
        self._connect("connection-a")
        self.registry.connect(second_participant_id, "connection-b")
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        self.registry.retain_connection_source(
            second_participant_id,
            "connection-b",
            "root-1",
        )

        self.assertTrue(
            self.registry.forget_connection_source(
                self.participant_id,
                "connection-a",
                "root-1",
            )
        )

        lease = self.runtime_leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(
            tuple(holder.holder_id for holder in lease.holders),
            (second_participant_id,),
        )
        self.assertEqual(
            self.registry.source_snapshot(
                second_participant_id,
                "root-1",
            ).connection_ids,
            ("connection-b",),
        )

    def test_live_second_connection_drives_deferred_holder_release_retry(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        original_release = self.runtime_leases.release
        self.runtime_leases.release = lambda *_args: False  # type: ignore[method-assign]
        try:
            with self.assertLogs(
                "bot.fcodex.participant_runtime_registry",
                level="ERROR",
            ):
                self.registry.disconnect(
                    self.participant_id,
                    "connection-a",
                )
        finally:
            self.runtime_leases.release = original_release  # type: ignore[method-assign]

        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.connection_ids, ())
        self.assertEqual(sources.holder_presence, "unknown")
        self.assertIsNotNone(self.runtime_leases.load("root-1"))

        self.registry.heartbeat(self.participant_id, "connection-b")

        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).holder_presence,
            "absent",
        )
        self.assertIsNone(self.runtime_leases.load("root-1"))


    def test_pending_request_survives_thread_gone_until_exact_known_rejection(self) -> None:
        """A lifecycle frame cannot settle a still-in-flight resume result."""

        self._connect("request-connection")
        self._connect("observer-connection")
        self.registry.retain_connection_source(
            self.participant_id,
            "observer-connection",
            "root-1",
        )
        request_source = self.registry.retain_request_source(
            self.participant_id,
            "request-connection",
            "request-1",
            "root-1",
        )

        self.assertTrue(self.registry.clear_thread_sources("root-1"))
        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.connection_ids, ())
        self.assertEqual(sources.pending_request_keys, ("request-1",))
        self.assertIsNotNone(self.runtime_leases.load("root-1"))

        self.assertTrue(
            self.registry.discard_request_source(request_source).exact_settled
        )
        self.assertIsNone(self.runtime_leases.load("root-1"))


    def test_one_known_rejection_cannot_release_second_pending_resume(self) -> None:
        self._connect()
        request_sources = {
            request_key: self.registry.retain_request_source(
                self.participant_id,
                "connection-a",
                request_key,
                "root-1",
            )
            for request_key in ("request-1", "request-2")
        }

        self.assertTrue(
            self.registry.discard_request_source(
                request_sources["request-1"]
            ).exact_settled
        )
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).pending_request_keys,
            ("request-2",),
        )
        self.assertIsNotNone(self.runtime_leases.load("root-1"))

        self.registry.discard_request_source(request_sources["request-2"])
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_other_connection_unsubscribe_cannot_release_pending_request(self) -> None:
        self._connect("request-connection")
        self._connect("observer-connection")
        self.registry.retain_connection_source(
            self.participant_id,
            "observer-connection",
            "root-1",
        )
        self.registry.retain_request_source(
            self.participant_id,
            "request-connection",
            "request-1",
            "root-1",
        )

        self.registry.forget_connection_source(
            self.participant_id,
            "observer-connection",
            "root-1",
        )

        self.assertIsNotNone(self.runtime_leases.load("root-1"))
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id, "root-1"
            ).pending_request_keys,
            ("request-1",),
        )

    def test_disconnect_promotes_pending_request_to_unknown_before_gone_clears_it(self) -> None:
        self._connect()
        request_source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.registry.disconnect(self.participant_id, "connection-a")

        self.assertEqual(
            self.registry.promote_request_to_unknown(request_source).outcome,
            "transitioned",
        )
        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertTrue(sources.unknown)
        self.assertEqual(sources.pending_request_keys, ())
        self.assertIsNotNone(self.runtime_leases.load("root-1"))

        self.assertTrue(self.registry.clear_thread_sources("root-1"))
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_success_promotes_request_source_to_exact_connection(self) -> None:
        self._connect()
        request_source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )

        conflict = self.registry.promote_request_to_connection(
            replace(request_source, thread_id="wrong-root")
        )
        self.assertEqual(conflict.outcome, "identity_conflict")
        self.assertEqual(conflict.conflict, "source_identity")
        self.assertEqual(
            self.registry.promote_request_to_connection(request_source).outcome,
            "transitioned",
        )
        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.pending_request_keys, ())
        self.assertEqual(sources.connection_ids, ("connection-a",))

    def test_request_ref_generation_prevents_reused_rpc_id_aba(self) -> None:
        self._connect()
        first = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.assertIs(
            self.registry.retain_request_source(
                self.participant_id,
                "connection-a",
                "request-1",
                "root-1",
            ),
            first,
        )
        self.assertEqual(
            self.registry.discard_request_source(first).outcome,
            "transitioned",
        )

        second = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.assertGreater(second.generation, first.generation)
        self.assertEqual(
            self.registry.discard_request_source(first).outcome,
            "exact_already_settled",
        )
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).pending_request_keys,
            ("request-1",),
        )
        self.assertEqual(
            self.registry.discard_request_source(second).outcome,
            "transitioned",
        )

    def test_request_transition_distinguishes_idempotent_missing_and_conflict(self) -> None:
        self._connect()
        source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        conflict = self.registry.discard_request_source(
            replace(source, generation=source.generation + 10_000)
        )
        self.assertEqual(conflict.outcome, "identity_conflict")
        self.assertEqual(conflict.conflict, "source_identity")

        transitioned = self.registry.discard_request_source(source)
        self.assertEqual(transitioned.outcome, "transitioned")
        self.assertEqual(
            self.registry.discard_request_source(source).outcome,
            "exact_already_settled",
        )
        self.assertTrue(
            self.registry.acknowledge_request_transition(transitioned)
        )
        self.assertEqual(
            self.registry.discard_request_source(source).outcome,
            "missing",
        )

    def test_request_transition_rejects_a_different_terminal_target(self) -> None:
        self._connect()
        source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.assertEqual(
            self.registry.promote_request_to_unknown(source).outcome,
            "transitioned",
        )

        conflict = self.registry.promote_request_to_connection(source)

        self.assertEqual(conflict.outcome, "identity_conflict")
        self.assertEqual(conflict.conflict, "different_target")
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).connection_ids,
            (),
        )

    def test_connection_promotion_rejects_dead_endpoint_but_unknown_remains_valid(self) -> None:
        self._connect()
        source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        self.registry.disconnect(self.participant_id, "connection-a")

        conflict = self.registry.promote_request_to_connection(source)

        self.assertEqual(conflict.outcome, "identity_conflict")
        self.assertEqual(conflict.conflict, "endpoint_not_live")
        self.assertEqual(
            self.registry.promote_request_to_unknown(source).outcome,
            "transitioned",
        )

    def test_discard_effect_unknown_retains_exact_retry_authority(self) -> None:
        self._connect()
        source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        original_release = self.runtime_leases.release

        def release_then_raise(thread_id: str, holder_id: str) -> bool:
            self.assertTrue(original_release(thread_id, holder_id))
            raise RuntimeError("response lost")

        self.runtime_leases.release = release_then_raise  # type: ignore[method-assign]
        try:
            with self.assertLogs(
                "bot.fcodex.participant_runtime_registry",
                level="ERROR",
            ):
                first = self.registry.discard_request_source(source)
        finally:
            self.runtime_leases.release = original_release  # type: ignore[method-assign]

        self.assertEqual(first.outcome, "effect_unknown")
        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).pending_request_keys,
            (),
        )
        self.assertEqual(
            self.registry.discard_request_source(source).outcome,
            "transitioned",
        )
        self.assertEqual(
            self.registry.discard_request_source(source).outcome,
            "exact_already_settled",
        )

    def test_discard_effect_unknown_can_ratchet_to_connection_loss_unknown(
        self,
    ) -> None:
        self._connect()
        source = self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )
        original_release = self.runtime_leases.release

        def release_then_raise(thread_id: str, holder_id: str) -> bool:
            self.assertTrue(original_release(thread_id, holder_id))
            raise RuntimeError("discard response lost")

        self.runtime_leases.release = release_then_raise  # type: ignore[method-assign]
        try:
            with self.assertLogs(
                "bot.fcodex.participant_runtime_registry",
                level="ERROR",
            ):
                discarded = self.registry.discard_request_source(source)
        finally:
            self.runtime_leases.release = original_release  # type: ignore[method-assign]

        self.assertEqual(discarded.outcome, "effect_unknown")
        promoted = self.registry.promote_request_to_unknown(source)
        self.assertEqual(promoted.outcome, "transitioned")
        snapshot = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertTrue(snapshot.unknown)
        self.assertEqual(snapshot.holder_presence, "confirmed")
        self.assertIsNotNone(self.runtime_leases.load("root-1"))
        self.assertTrue(self.registry.acknowledge_request_transition(promoted))

    def test_request_identity_conflict_preserves_first_source(self) -> None:
        self._connect()
        self.registry.retain_request_source(
            self.participant_id,
            "connection-a",
            "request-1",
            "root-1",
        )

        with self.assertRaises(RuntimeError):
            self.registry.retain_request_source(
                self.participant_id,
                "connection-a",
                "request-1",
                "root-2",
            )

        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id, "root-1"
            ).pending_request_keys,
            ("request-1",),
        )
        self.assertFalse(
            self.registry.source_snapshot(
                self.participant_id, "root-2"
            ).holder_tracked
        )

    def test_authoritative_cleanup_retries_prior_unneeded_release_effect(self) -> None:
        self._connect()
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        original_release = self.runtime_leases.release
        self.runtime_leases.release = lambda *_args: False  # type: ignore[method-assign]
        try:
            with self.assertLogs(
                "bot.fcodex.participant_runtime_registry",
                level="ERROR",
            ):
                self.assertFalse(
                    self.registry.forget_connection_source(
                        self.participant_id,
                        "connection-a",
                        "root-1",
                    )
                )
        finally:
            self.runtime_leases.release = original_release  # type: ignore[method-assign]

        self.assertTrue(self.registry.clear_thread_sources("root-1"))
        self.assertIsNone(self.runtime_leases.load("root-1"))

    def test_authoritative_cleanup_retries_partial_multi_participant_release(self) -> None:
        second_participant_id = "fcodex:bob:incarnation-2"
        self._connect("connection-a")
        self.registry.connect(second_participant_id, "connection-b")
        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )
        self.registry.retain_connection_source(
            second_participant_id,
            "connection-b",
            "root-1",
        )
        original_release = self.runtime_leases.release

        def fail_second_participant(thread_id: str, holder_id: str) -> bool:
            if holder_id == second_participant_id:
                return False
            return original_release(thread_id, holder_id)

        self.runtime_leases.release = fail_second_participant  # type: ignore[method-assign]
        try:
            with self.assertLogs(
                "bot.fcodex.participant_runtime_registry",
                level="ERROR",
            ):
                self.assertFalse(self.registry.clear_thread_sources("root-1"))
        finally:
            self.runtime_leases.release = original_release  # type: ignore[method-assign]

        lease = self.runtime_leases.load("root-1")
        self.assertIsNotNone(lease)
        self.assertEqual(
            tuple(holder.holder_id for holder in lease.holders),
            (second_participant_id,),
        )
        self.assertTrue(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).thread_authoritative_cleanup_pending
        )

        self.assertTrue(self.registry.retry_authoritative_cleanups())
        self.assertIsNone(self.runtime_leases.load("root-1"))
        self.assertFalse(
            self.registry.source_snapshot(
                second_participant_id,
                "root-1",
            ).thread_authoritative_cleanup_pending
        )






    def test_ambiguous_precommit_acquire_reacquires_before_replacement_source(self) -> None:
        self._connect()
        original_acquire = self.runtime_leases.acquire
        original_load = self.runtime_leases.load

        def reject_before_commit(_thread_id, _holder):
            raise RuntimeError("acquire unavailable")

        def load_unavailable(_thread_id):
            raise RuntimeError("point read unavailable")

        self.runtime_leases.acquire = reject_before_commit  # type: ignore[method-assign]
        self.runtime_leases.load = load_unavailable  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "acquire unavailable"):
                self.registry.retain_request_source(
                    self.participant_id,
                    "connection-a",
                    "request-1",
                    "root-1",
                )
        finally:
            self.runtime_leases.acquire = original_acquire  # type: ignore[method-assign]
            self.runtime_leases.load = original_load  # type: ignore[method-assign]

        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.pending_request_keys, ())
        self.assertEqual(sources.holder_presence, "unknown")
        self.assertIsNone(self.runtime_leases.load("root-1"))

        self.registry.retain_connection_source(
            self.participant_id,
            "connection-a",
            "root-1",
        )

        self.assertEqual(
            self.registry.source_snapshot(
                self.participant_id,
                "root-1",
            ).holder_presence,
            "confirmed",
        )
        self.assertIsNotNone(self.runtime_leases.load("root-1"))





    def test_orphaned_empty_participant_is_retired_by_release_reconcile(self) -> None:
        self._connect()
        self.registry.disconnect(self.participant_id, "connection-a")
        participant_expiry = self.participant_expiries[-1]
        self.assertTrue(self.registry.expire_participant(*participant_expiry[:2]))
        self.assertIsNotNone(self.registry.snapshot(self.participant_id))

        self.assertTrue(self.registry.release_unneeded_sources(self.participant_id))

        self.assertIsNone(self.registry.snapshot(self.participant_id))

    def test_retired_participant_timer_capabilities_cannot_hit_same_id_reconnect(self) -> None:
        self._connect("connection-a")
        stale_connection_expiry = self.connection_expiries[-1]
        self.registry.disconnect(self.participant_id, "connection-a")
        stale_participant_expiry = self.participant_expiries[-1]
        self.assertTrue(
            self.registry.expire_participant(*stale_participant_expiry[:2])
        )
        self.assertTrue(self.registry.release_unneeded_sources(self.participant_id))
        self.assertIsNone(self.registry.snapshot(self.participant_id))

        self._connect("connection-a")
        current_connection_expiry = self.connection_expiries[-1]
        self.assertFalse(
            self.registry.connection_expiry_is_current(
                *stale_connection_expiry[:3]
            )
        )
        self.assertTrue(
            self.registry.connection_expiry_is_current(
                *current_connection_expiry[:3]
            )
        )
        self.registry.disconnect(self.participant_id, "connection-a")
        current_participant_expiry = self.participant_expiries[-1]
        self.assertNotEqual(
            stale_participant_expiry[1],
            current_participant_expiry[1],
        )
        self.assertFalse(
            self.registry.expire_participant(*stale_participant_expiry[:2])
        )
        self.assertTrue(
            self.registry.expire_participant(*current_participant_expiry[:2])
        )

    def test_loaded_gate_rejection_installs_no_source_or_holder(self) -> None:
        self._connect()
        self.loaded_gate = ReasonedCheck.deny(
            "blocked",
            "another instance is loaded",
        )

        with self.assertRaisesRegex(RuntimeError, "another instance"):
            self.registry.retain_request_source(
                self.participant_id,
                "connection-a",
                "request-1",
                "root-1",
            )

        sources = self.registry.source_snapshot(self.participant_id, "root-1")
        self.assertEqual(sources.pending_request_keys, ())
        self.assertFalse(sources.holder_tracked)
        self.assertIsNone(self.runtime_leases.load("root-1"))


if __name__ == "__main__":
    unittest.main()
