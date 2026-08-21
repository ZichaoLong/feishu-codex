"""End-to-end regressions for targetless fcodex ``thread/start``."""

from __future__ import annotations

from unittest.mock import Mock, patch

from bot.fcodex.interaction_contract import fcodex_client_request_key
from tests.fcodex_operation_harness import FcodexOperationHarness


class FcodexTargetlessThreadCreateTests(FcodexOperationHarness):
    def test_admission_publishes_one_process_local_capability(self) -> None:
        self._connect()

        admitted = self._admit(
            request_id=1,
            method="thread/start",
            thread_id="",
        )

        self.assertTrue(admitted["allowed"])
        request_key = fcodex_client_request_key(
            self.participant_id,
            "connection-a",
            1,
        )
        request = self.operation_service._client_requests[request_key]
        self.assertIsNotNone(request.external_create_attempt)
        self.assertIsNone(request.external_create_resolution)

    def test_create_admission_is_not_globally_serialized_across_connections(self) -> None:
        self._connect("connection-a")
        self._connect("connection-b")

        first = self._admit(
            connection_id="connection-a",
            request_id=1,
            method="thread/start",
            thread_id="",
        )
        second = self._admit(
            connection_id="connection-b",
            request_id=2,
            method="thread/start",
            thread_id="",
        )

        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        attempts = [
            request.external_create_attempt
            for request in self.operation_service._client_requests.values()
        ]
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0].attempt_id, attempts[1].attempt_id)

    def test_post_send_non_success_settles_exact_request_without_proxy_quarantine(
        self,
    ) -> None:
        self._connect()
        for index, outcome in enumerate(("error", "unknown"), start=1):
            with self.subTest(outcome=outcome):
                self.assertTrue(
                    self._admit(
                        request_id=index,
                        method="thread/start",
                        thread_id="",
                    )["allowed"]
                )

                receipt = self._client_response(
                    request_id=index,
                    outcome=outcome,
                )

                self.assertTrue(receipt["known"])
                self.assertTrue(receipt["settled"])
                self.assertTrue(receipt["retained"])
                self.assertEqual(self.operation_service._client_requests, {})

    def test_misdirected_success_is_exact_unknown_without_runtime_source(self) -> None:
        self._connect()
        self._admit(request_id=1, method="thread/start", thread_id="")

        receipt = self._client_response(
            request_id=1,
            outcome="success",
            observed_thread_id="created-root",
            observed_root_thread_id="different-root",
        )

        self.assertTrue(receipt["settled"])
        self.assertTrue(receipt["retained"])
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "created-root",
        )
        self.assertFalse(source.holder_tracked)
        self.assertEqual(source.connection_ids, ())

    def test_success_commits_registry_source_before_acknowledging_response(self) -> None:
        self._connect()
        self._admit(request_id=1, method="thread/start", thread_id="")

        receipt = self._client_response(
            request_id=1,
            outcome="success",
            observed_thread_id="created-root",
            observed_root_thread_id="created-root",
        )

        self.assertTrue(receipt["settled"])
        self.assertFalse(receipt.get("retained", False))
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "created-root",
        )
        self.assertEqual(source.connection_ids, ("connection-a",))
        self.assertEqual(source.pending_request_keys, ())
        self.assertEqual(self.operation_service._client_requests, {})

    def test_registry_ack_retry_does_not_replay_local_create_commit(self) -> None:
        self._connect()
        self._admit(request_id=1, method="thread/start", thread_id="")
        original_retain = self.participant_runtime.retain_request_source
        retain = Mock(wraps=original_retain)
        acknowledgements = iter((False, True))

        with (
            patch.object(
                self.participant_runtime,
                "retain_request_source",
                retain,
            ),
            patch.object(
                self.participant_runtime,
                "acknowledge_request_transition",
                side_effect=lambda _receipt: next(acknowledgements),
            ),
        ):
            first = self._client_response(
                request_id=1,
                outcome="success",
                observed_thread_id="created-root",
                observed_root_thread_id="created-root",
            )
            second = self._client_response(
                request_id=1,
                outcome="success",
                observed_thread_id="created-root",
                observed_root_thread_id="created-root",
            )

        self.assertFalse(first["settled"])
        self.assertTrue(first["retained"])
        self.assertTrue(second["settled"])
        self.assertEqual(retain.call_count, 1)
        self.assertEqual(self.operation_service._client_requests, {})

    def test_backend_epoch_invalidation_rejects_late_create_settlement(self) -> None:
        self._connect()
        admitted = self._admit(
            request_id=1,
            method="thread/start",
            thread_id="",
        )
        self.assertTrue(admitted["allowed"])

        self.thread_runtime_authority.invalidate_connection()
        self.operation_service.backend_disconnected()
        late = self._client_response(
            request_id=1,
            outcome="success",
            observed_thread_id="created-root",
            observed_root_thread_id="created-root",
        )

        self.assertFalse(late["settled"])
        self.assertTrue(late["retained"])
        source = self.participant_runtime.source_snapshot(
            self.participant_id,
            "created-root",
        )
        self.assertFalse(source.holder_tracked)

    def test_old_token_cannot_settle_reused_jsonrpc_id(self) -> None:
        self._connect()
        first = self._admit(request_id=1, method="thread/start", thread_id="")
        first_token = first["request_token"]
        self._client_response(request_id=1, outcome="error")
        second = self._admit(request_id=1, method="thread/start", thread_id="")

        stale = self.operation_service.client_response(
            participant_id=self.participant_id,
            connection_id="connection-a",
            request_id=1,
            request_token=first_token,
            outcome="error",
        )

        self.assertFalse(stale["known"])
        self.assertNotEqual(first_token, second["request_token"])
