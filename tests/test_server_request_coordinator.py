from __future__ import annotations

import unittest

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestResponseSupersededError,
)
from bot.server_request_coordinator import (
    ServerRequestCoordinator,
    ServerRequestCoordinatorPorts,
)
from bot.server_request_dispatch import ServerRequestDispatchReceipt
from bot.server_request_registry import ServerRequestRegistry


REQUEST_METHOD = "item/tool/requestUserInput"


class _CoordinatorHarness:
    def __init__(self, *, resolved_limit: int = 16) -> None:
        self.registry = ServerRequestRegistry(resolved_limit=resolved_limit)
        self.events: list[tuple[str, object]] = []
        self.dispatch_outcomes: dict[str, ServerRequestDispatchReceipt] = {}
        self.dispatched: list[ServerRequestIdentity] = []
        self.cancelled: list[str] = []
        self.reconciled: list[str] = []
        self.revoked_web: list[ServerRequestIdentity] = []
        self.invalidated_epochs = 0
        self.response_attempts: list[tuple[object, dict[str, object]]] = []
        self.response_errors: list[Exception] = []
        self.remove_errors: dict[str, Exception] = {}
        self.reconcile_error: Exception | None = None
        self.coordinator = ServerRequestCoordinator(
            self.registry,
            ServerRequestCoordinatorPorts(
                cancel_auto_resolution=self._cancel,
                remove_web_resolved=lambda identity: self._remove("web", identity),
                revoke_web_response_authority=lambda identity: self.revoked_web.append(
                    identity
                ),
                remove_fcodex_resolved=lambda identity: self._remove(
                    "fcodex", identity
                ),
                remove_feishu_resolved=lambda identity: self._remove(
                    "feishu", identity
                ),
                reconcile_resolved_root=self._reconcile,
                invalidate_auto_resolution_epoch=self._invalidate_epoch,
                shutdown_auto_resolution=lambda: None,
                dispatch_request=self._dispatch,
                respond=self._respond,
            ),
            lambda: None,
        )
        self.coordinator.activate_connection_epoch(1)

    def route(
        self,
        request_id: str,
        *,
        generation: int = 1,
        thread_id: str = "root-1",
        turn_id: str = "turn-1",
        question: str = "Continue?",
    ):
        return self.coordinator.route_request(
            generation,
            request_id,
            REQUEST_METHOD,
            {
                "threadId": thread_id,
                "turnId": turn_id,
                "questions": [{"id": "q1", "question": question}],
            },
        )

    def _dispatch(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestDispatchReceipt:
        self.dispatched.append(identity)
        self.events.append(("dispatch", identity))
        return self.dispatch_outcomes.get(
            str(identity.request_id),
            ServerRequestDispatchReceipt.committed(),
        )

    def _remove(
        self,
        surface: str,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        self.events.append((surface, identity))
        error = self.remove_errors.get(surface)
        if error is not None:
            raise error
        root_id = "root-1" if identity.thread_id == "child-1" else identity.thread_id
        return ServerRequestLocalRemoval(
            "removed",
            identity.request_key,
            identity.thread_id,
            root_id,
        )

    def _cancel(self, request_key: str) -> None:
        self.cancelled.append(request_key)
        self.events.append(("cancel", request_key))

    def _reconcile(self, root_thread_id: str) -> None:
        self.reconciled.append(root_thread_id)
        self.events.append(("reconcile", root_thread_id))
        if self.reconcile_error is not None:
            raise self.reconcile_error

    def _invalidate_epoch(self) -> None:
        self.invalidated_epochs += 1

    def _respond(self, request_id: object, **kwargs: object) -> None:
        self.response_attempts.append((request_id, kwargs))
        if self.response_errors:
            raise self.response_errors.pop(0)


class ServerRequestCoordinatorTest(unittest.TestCase):
    def test_epoch_mismatch_never_reaches_surface_dispatch(self) -> None:
        harness = _CoordinatorHarness()

        report = harness.route("stale-request", generation=2)

        self.assertEqual(report.outcome, "epoch_mismatch")
        self.assertEqual(harness.dispatched, [])
        self.assertEqual(harness.registry.pending_count(), 0)

    def test_exact_replay_reuses_identity_and_reprojects(self) -> None:
        harness = _CoordinatorHarness()

        first = harness.route("request-1")
        replay = harness.route("request-1")

        self.assertEqual(first.outcome, "committed")
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(len(harness.dispatched), 2)
        self.assertIs(harness.dispatched[1], harness.dispatched[0])
        self.assertEqual(harness.registry.pending_count(), 1)

    def test_resolved_removes_all_exact_surface_projections_once(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1", thread_id="child-1")
        identity = harness.dispatched[-1]

        report = harness.coordinator.handle_server_request_resolved(
            {"requestId": "request-1", "threadId": "child-1"}
        )

        self.assertEqual(report.outcome, "settled")
        self.assertEqual(harness.registry.pending_count(), 0)
        self.assertEqual(
            [name for name, value in harness.events if value is identity],
            ["dispatch", "web", "fcodex", "feishu"],
        )
        self.assertEqual(harness.cancelled, [identity.request_key])
        self.assertEqual(harness.reconciled, ["root-1"])

        replay = harness.route("request-1", thread_id="child-1")
        self.assertEqual(replay.outcome, "suppressed_resolved")
        self.assertEqual(len(harness.dispatched), 1)
        self.assertEqual(harness.reconciled, ["root-1"])

    def test_matching_lifecycle_removes_all_exact_surface_projections(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1", thread_id="child-1")
        identity = harness.dispatched[-1]

        harness.coordinator.handle_notification(
            "turn/completed",
            {
                "threadId": "child-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        )

        self.assertEqual(harness.registry.pending_count(), 0)
        self.assertEqual(
            [name for name, value in harness.events if value is identity],
            ["dispatch", "web", "fcodex", "feishu"],
        )
        self.assertEqual(harness.reconciled, ["root-1"])

    def test_one_surface_removal_failure_does_not_skip_other_surfaces(self) -> None:
        for failed_surface in ("web", "fcodex", "feishu"):
            with self.subTest(failed_surface=failed_surface):
                harness = _CoordinatorHarness()
                harness.route("request-1", thread_id="child-1")
                identity = harness.dispatched[-1]
                harness.remove_errors[failed_surface] = RuntimeError("cleanup failed")

                with self.assertLogs("bot.server_request_coordinator", level="ERROR"):
                    report = harness.coordinator.handle_server_request_resolved(
                        {"requestId": "request-1", "threadId": "child-1"}
                    )
                unrelated = harness.route("request-2")

                self.assertEqual(report.outcome, "settled")
                self.assertEqual(len(report.local_removals), 2)
                self.assertEqual(
                    [name for name, value in harness.events if value is identity],
                    ["dispatch", "web", "fcodex", "feishu"],
                )
                self.assertEqual(harness.reconciled, ["root-1"])
                self.assertEqual(unrelated.outcome, "committed")

    def test_root_reconciliation_failure_does_not_reverse_canonical_settlement(
        self,
    ) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1", thread_id="child-1")
        harness.reconcile_error = RuntimeError("projection reconciliation failed")

        with self.assertLogs("bot.server_request_coordinator", level="ERROR"):
            report = harness.coordinator.handle_server_request_resolved(
                {"requestId": "request-1", "threadId": "child-1"}
            )

        self.assertEqual(report.outcome, "settled")
        self.assertEqual(report.reconciled_root_ids, frozenset({"root-1"}))
        self.assertEqual(harness.registry.pending_count(), 0)

    def test_surface_root_prevents_identity_fallback_during_settlement(
        self,
    ) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1", thread_id="child-1")

        report = harness.coordinator.handle_server_request_resolved(
            {"requestId": "request-1", "threadId": "child-1"}
        )

        self.assertEqual(report.outcome, "settled")
        self.assertEqual(report.reconciled_root_ids, frozenset({"root-1"}))
        self.assertEqual(harness.reconciled, ["root-1"])
        self.assertEqual(harness.registry.pending_count(), 0)

    def test_disconnect_clears_epoch_and_replay_rebuilds_identity(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        old_identity = harness.dispatched[-1]

        harness.coordinator.backend_disconnected()

        self.assertEqual(harness.registry.connection_generation, 0)
        self.assertEqual(harness.registry.pending_count(), 0)
        self.assertEqual(harness.invalidated_epochs, 1)

        harness.coordinator.activate_connection_epoch(2)
        replay = harness.route("request-1", generation=2)
        new_identity = harness.dispatched[-1]

        self.assertEqual(replay.outcome, "committed")
        self.assertIsNot(new_identity, old_identity)
        self.assertEqual(new_identity.connection_generation, 2)

    def test_unknown_dispatch_locks_only_the_exact_request(self) -> None:
        harness = _CoordinatorHarness()
        harness.dispatch_outcomes["uncertain"] = (
            ServerRequestDispatchReceipt.outcome_unknown()
        )

        first = harness.route("uncertain")
        replay = harness.route("uncertain")
        unrelated = harness.route("unrelated")

        self.assertEqual(first.outcome, "dispatch_failed")
        self.assertEqual(first.dispatch_outcome, "outcome_unknown")
        self.assertEqual(replay.outcome, "dispatch_failed")
        self.assertEqual(unrelated.outcome, "committed")
        self.assertEqual(
            [identity.request_id for identity in harness.dispatched],
            ["uncertain", "unrelated"],
        )
        self.assertEqual(harness.registry.pending_count(), 2)

    def test_identity_conflict_does_not_fence_the_root(self) -> None:
        harness = _CoordinatorHarness()

        first = harness.route("same-id")
        conflict = harness.route("same-id", question="Different envelope")
        unrelated = harness.route("next-request")

        self.assertEqual(first.outcome, "committed")
        self.assertEqual(conflict.outcome, "identity_conflict")
        self.assertEqual(unrelated.outcome, "committed")
        self.assertEqual(
            [identity.request_id for identity in harness.dispatched],
            ["same-id", "next-request"],
        )
        self.assertEqual(harness.registry.pending_count(), 2)

    def test_unknown_resolution_is_local_missing_not_a_tombstone(self) -> None:
        harness = _CoordinatorHarness()

        report = harness.coordinator.handle_server_request_resolved(
            {"requestId": "unseen", "threadId": "root-1"}
        )
        later = harness.route("unseen")

        self.assertEqual(report.outcome, "missing")
        self.assertEqual(later.outcome, "committed")

    def test_first_successful_response_is_the_only_adapter_submission(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]

        first = harness.coordinator.submit_response(
            identity,
            result={"decision": "accept"},
            error=None,
        )
        second = harness.coordinator.submit_response(
            identity,
            result={"decision": "decline"},
            error=None,
        )

        self.assertEqual(first.outcome, "submitted")
        self.assertEqual(second.outcome, "superseded")
        self.assertEqual(len(harness.response_attempts), 1)

    def test_pre_send_failure_releases_exact_response_for_retry(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]
        harness.response_errors.append(
            CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before send"),
            )
        )

        with self.assertRaises(CodexRpcPreSendError):
            harness.coordinator.submit_response(identity, result={}, error=None)
        retry = harness.coordinator.submit_response(identity, result={}, error=None)

        self.assertEqual(retry.outcome, "submitted")
        self.assertEqual(len(harness.response_attempts), 2)

    def test_unknown_response_fences_only_the_exact_request(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("uncertain")
        uncertain = harness.dispatched[-1]
        harness.route("unrelated")
        unrelated = harness.dispatched[-1]
        harness.response_errors.append(RuntimeError("possibly after send"))

        with self.assertRaisesRegex(RuntimeError, "possibly after send"):
            harness.coordinator.submit_response(uncertain, result={}, error=None)
        duplicate = harness.coordinator.submit_response(
            uncertain,
            result={},
            error=None,
        )
        other = harness.coordinator.submit_response(unrelated, result={}, error=None)

        self.assertEqual(duplicate.outcome, "outcome_unknown")
        self.assertEqual(other.outcome, "submitted")
        self.assertEqual(
            [request_id for request_id, _kwargs in harness.response_attempts],
            ["uncertain", "unrelated"],
        )

    def test_value_equal_identity_cannot_inherit_response_authority(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        canonical = harness.dispatched[-1]
        replacement = ServerRequestIdentity(
            request_id=canonical.request_id,
            connection_generation=canonical.connection_generation,
            method=canonical.method,
            params=canonical.params,
        )

        rejected = harness.coordinator.submit_response(
            replacement,
            result={},
            error=None,
        )
        accepted = harness.coordinator.submit_response(
            canonical,
            result={},
            error=None,
        )

        self.assertEqual(rejected.outcome, "identity_conflict")
        self.assertEqual(accepted.outcome, "submitted")
        self.assertEqual(len(harness.response_attempts), 1)

    def test_settlement_and_epoch_retirement_remove_response_authority(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("resolved")
        resolved = harness.dispatched[-1]
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "resolved", "threadId": "root-1"}
        )

        after_resolution = harness.coordinator.submit_response(
            resolved,
            result={},
            error=None,
        )
        harness.route("retired")
        retired = harness.dispatched[-1]
        harness.coordinator.retire_connection_epoch()
        after_retirement = harness.coordinator.submit_response(
            retired,
            result={},
            error=None,
        )

        self.assertEqual(after_resolution.outcome, "not_pending")
        self.assertEqual(after_retirement.outcome, "not_pending")
        self.assertEqual(harness.response_attempts, [])

    def test_late_surface_response_after_resolution_is_superseded_and_not_resent(
        self,
    ) -> None:
        harness = _CoordinatorHarness()
        harness.route("resolved")
        identity = harness.dispatched[-1]
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "resolved", "threadId": "root-1"}
        )

        with self.assertRaises(ServerRequestResponseSupersededError):
            harness.coordinator.submit_surface_response(
                identity,
                result={"decision": "accept"},
            )

        self.assertEqual(harness.response_attempts, [])

    def test_response_terminal_phase_suppresses_exact_upstream_replay(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]
        harness.coordinator.submit_response(identity, result={}, error=None)

        replay = harness.route("request-1")

        self.assertEqual(replay.outcome, "response_pending_resolution")
        self.assertEqual(replay.response_phase, "submitted")
        self.assertEqual(len(harness.dispatched), 1)

    def test_unknown_response_phase_suppresses_exact_upstream_replay(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]
        harness.response_errors.append(RuntimeError("possibly sent"))
        with self.assertRaisesRegex(RuntimeError, "possibly sent"):
            harness.coordinator.submit_response(identity, result={}, error=None)

        replay = harness.route("request-1")

        self.assertEqual(replay.outcome, "response_pending_resolution")
        self.assertEqual(replay.response_phase, "unknown")
        self.assertEqual(len(harness.dispatched), 1)

    def test_pre_send_response_failure_leaves_exact_replay_dispatchable(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]
        harness.response_errors.append(
            CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before send"),
            )
        )
        with self.assertRaises(CodexRpcPreSendError):
            harness.coordinator.submit_response(identity, result={}, error=None)

        replay = harness.route("request-1")

        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(len(harness.dispatched), 2)

    def test_exact_revocation_blocks_pre_send_retry_and_upstream_replay(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("revoked")
        identity = harness.dispatched[-1]
        harness.response_errors.append(
            CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before fail-close send"),
            )
        )
        with self.assertRaises(CodexRpcPreSendError):
            harness.coordinator.submit_response(identity, result={}, error=None)

        self.assertTrue(
            harness.coordinator.revoke_surface_response_authority(identity)
        )
        with self.assertRaises(ServerRequestResponseSupersededError):
            harness.coordinator.submit_surface_response(
                identity,
                result={"decision": "accept"},
            )
        replay = harness.route("revoked")

        self.assertEqual(replay.outcome, "response_pending_resolution")
        self.assertEqual(replay.response_phase, "pending")
        self.assertTrue(replay.response_authority_revoked)
        self.assertEqual(len(harness.dispatched), 1)
        self.assertEqual(len(harness.response_attempts), 1)

    def test_revocation_is_exact_and_clears_with_resolution_and_epoch(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("revoked")
        revoked = harness.dispatched[-1]
        harness.route("unrelated")
        unrelated = harness.dispatched[-1]
        clone = ServerRequestIdentity(
            request_id=revoked.request_id,
            connection_generation=revoked.connection_generation,
            method=revoked.method,
            params=revoked.params,
        )

        self.assertFalse(
            harness.coordinator.revoke_surface_response_authority(clone)
        )
        self.assertTrue(
            harness.coordinator.revoke_surface_response_authority(revoked)
        )
        self.assertEqual(harness.revoked_web, [revoked])
        self.assertEqual(
            harness.coordinator.submit_response(unrelated, result={}, error=None).outcome,
            "submitted",
        )
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "revoked", "threadId": "root-1"}
        )
        self.assertEqual(
            harness.coordinator.submit_response(revoked, result={}, error=None).outcome,
            "not_pending",
        )

        harness.coordinator.retire_connection_epoch()
        harness.coordinator.activate_connection_epoch(2)
        rebuilt = harness.route("revoked", generation=2)
        fresh = harness.dispatched[-1]
        self.assertEqual(rebuilt.outcome, "committed")
        self.assertEqual(harness.registry.response_phase(fresh), "pending")

    def test_revocation_preserves_unknown_external_effect_phase(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("unknown-then-revoked")
        identity = harness.dispatched[-1]
        harness.response_errors.append(RuntimeError("possibly sent"))
        with self.assertRaisesRegex(RuntimeError, "possibly sent"):
            harness.coordinator.submit_response(identity, result={}, error=None)

        self.assertTrue(
            harness.coordinator.revoke_surface_response_authority(identity)
        )
        replay = harness.route("unknown-then-revoked")

        self.assertEqual(harness.registry.response_phase(identity), "unknown")
        self.assertTrue(harness.registry.response_authority_is_revoked(identity))
        self.assertEqual(replay.response_phase, "unknown")
        self.assertTrue(replay.response_authority_revoked)
        self.assertEqual(len(harness.response_attempts), 1)

    def test_value_equal_surface_identity_cannot_inherit_resolved_tombstone(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("resolved")
        identity = harness.dispatched[-1]
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "resolved", "threadId": "root-1"}
        )
        clone = ServerRequestIdentity(
            request_id=identity.request_id,
            connection_generation=identity.connection_generation,
            method=identity.method,
            params=identity.params,
        )

        with self.assertRaisesRegex(RuntimeError, "identity_conflict"):
            harness.coordinator.submit_surface_response(
                clone,
                result={"decision": "accept"},
            )

        self.assertEqual(harness.response_attempts, [])

    def test_late_surface_identity_cannot_answer_same_id_after_tombstone_eviction(
        self,
    ) -> None:
        harness = _CoordinatorHarness(resolved_limit=1)
        harness.route("same", turn_id="turn-old")
        old_identity = harness.dispatched[-1]
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "same", "threadId": "root-1"}
        )
        harness.route("evict")
        harness.coordinator.handle_server_request_resolved(
            {"requestId": "evict", "threadId": "root-1"}
        )
        harness.route("same", turn_id="turn-new")

        with self.assertRaisesRegex(RuntimeError, "identity_conflict"):
            harness.coordinator.submit_surface_response(
                old_identity,
                result={"decision": "accept"},
            )

        self.assertEqual(harness.response_attempts, [])

    def test_exact_surface_identity_can_retry_after_pre_send_failure(self) -> None:
        harness = _CoordinatorHarness()
        harness.route("request-1")
        identity = harness.dispatched[-1]
        harness.response_errors.append(
            CodexRpcPreSendError(
                "serverRequest/response",
                RuntimeError("offline before send"),
            )
        )

        with self.assertRaises(CodexRpcPreSendError):
            harness.coordinator.submit_surface_response(
                identity,
                result={"decision": "accept"},
            )
        harness.coordinator.submit_surface_response(
            identity,
            result={"decision": "accept"},
        )

        self.assertEqual(len(harness.response_attempts), 2)


if __name__ == "__main__":
    unittest.main()
