import threading
import unittest
from unittest.mock import Mock

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.codex_protocol.client import CodexRpcPreSendError
from bot.thread_create_transaction import (
    CommittedThreadCreate,
    ThreadCreateLocalCommitFailed,
    ThreadCreateOutcomeUnknown,
    ThreadCreateSettlementError,
    ThreadCreateTransaction,
)


def _snapshot(thread_id: str = "thread-1") -> ThreadSnapshot:
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id=thread_id,
            cwd="/work",
            name="",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        ),
        effective_model="gpt-effective",
        effective_reasoning_effort="high",
        effective_approval_policy="never",
        effective_permissions_profile_id=":workspace",
    )


class ThreadCreateTransactionTests(unittest.TestCase):
    def _transaction(
        self,
        adapter: Mock,
        *,
        facts: Mock | None = None,
        acquire: Mock | None = None,
    ) -> tuple[ThreadCreateTransaction, Mock, Mock]:
        settings_facts = facts or Mock()
        lease = acquire or Mock(return_value=True)
        ids = iter(f"attempt-{index}" for index in range(1, 20))
        transaction = ThreadCreateTransaction(
            adapter=adapter,
            effective_settings=settings_facts,
            acquire_runtime_lease=lease,
            failure_known_no_effect=lambda exc: isinstance(
                exc,
                CodexRpcPreSendError,
            ),
            new_attempt_id=lambda: next(ids),
        )
        return transaction, settings_facts, lease

    def test_success_applies_typed_response_then_local_commit(self) -> None:
        events: list[str] = []
        adapter = Mock()
        adapter.create_thread.side_effect = lambda **_kwargs: (
            events.append("create") or _snapshot()
        )
        facts = Mock()
        facts.record_start_or_resume.side_effect = (
            lambda *_args, **_kwargs: events.append("settings")
        )
        acquire = Mock(side_effect=lambda _thread_id: events.append("lease") or True)
        transaction, _, _ = self._transaction(
            adapter,
            facts=facts,
            acquire=acquire,
        )

        created = transaction.create_and_commit_thread(
            local_commit=lambda snapshot: (
                events.append("owner") or snapshot.summary.thread_id
            ),
            cwd="/work",
        )

        self.assertIsInstance(created, CommittedThreadCreate)
        self.assertEqual(created.response.summary.thread_id, "thread-1")
        self.assertEqual(created.local_result, "thread-1")
        self.assertEqual(events, ["create", "lease", "settings", "owner"])
        facts.record_start_or_resume.assert_called_once_with(
            "thread-1",
            model="gpt-effective",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_start",
        )

    def test_known_no_effect_failure_is_returned_without_local_state(self) -> None:
        adapter = Mock()
        adapter.create_thread.side_effect = CodexRpcPreSendError(
            "thread/start",
            ValueError("rejected before send"),
        )
        transaction, facts, acquire = self._transaction(adapter)
        local_commit = Mock()

        with self.assertRaises(CodexRpcPreSendError):
            transaction.create_and_commit_thread(local_commit=local_commit)

        acquire.assert_not_called()
        facts.record_start_or_resume.assert_not_called()
        local_commit.assert_not_called()

    def test_unknown_result_is_exact_and_does_not_block_a_later_create(self) -> None:
        adapter = Mock()
        adapter.create_thread.side_effect = [TimeoutError("lost"), _snapshot("thread-2")]
        transaction, _, _ = self._transaction(adapter)

        with self.assertRaises(ThreadCreateOutcomeUnknown) as raised:
            transaction.create_and_commit_thread(local_commit=Mock())

        self.assertEqual(raised.exception.attempt_id, "attempt-1")
        created = transaction.create_and_commit_thread(
            local_commit=lambda snapshot: snapshot.summary.thread_id,
        )
        self.assertEqual(created.local_result, "thread-2")

    def test_invalid_success_shape_is_unknown_without_owner_commit(self) -> None:
        adapter = Mock()
        adapter.create_thread.return_value = object()
        transaction, _, acquire = self._transaction(adapter)
        local_commit = Mock()

        with self.assertRaises(ThreadCreateOutcomeUnknown):
            transaction.create_and_commit_thread(local_commit=local_commit)

        acquire.assert_not_called()
        local_commit.assert_not_called()

    def test_local_stage_failure_reports_known_thread_without_recovery_fence(self) -> None:
        for stage, facts_error, lease_error, owner_error in (
            ("runtime_lease", None, RuntimeError("lease"), None),
            ("effective_settings", RuntimeError("settings"), None, None),
            ("local_owner", None, None, RuntimeError("owner")),
        ):
            with self.subTest(stage=stage):
                adapter = Mock()
                adapter.create_thread.return_value = _snapshot()
                facts = Mock()
                if facts_error is not None:
                    facts.record_start_or_resume.side_effect = facts_error
                acquire = Mock(return_value=True)
                if lease_error is not None:
                    acquire.side_effect = lease_error
                transaction, _, _ = self._transaction(
                    adapter,
                    facts=facts,
                    acquire=acquire,
                )
                local_commit = Mock()
                if owner_error is not None:
                    local_commit.side_effect = owner_error

                with self.assertRaises(ThreadCreateLocalCommitFailed) as raised:
                    transaction.create_and_commit_thread(local_commit=local_commit)

                self.assertEqual(raised.exception.thread_id, "thread-1")
                self.assertEqual(raised.exception.stage, stage)

    def test_external_success_consumes_capability_once(self) -> None:
        transaction, _, _ = self._transaction(Mock())
        attempt = transaction.begin_external_thread_create()
        local_commit = Mock(return_value="registry-receipt")

        created = transaction.commit_external_thread_create(
            attempt,
            thread_id="thread-1",
            local_commit=local_commit,
        )

        self.assertEqual(created.response, "thread-1")
        self.assertEqual(created.local_result, "registry-receipt")
        with self.assertRaises(ThreadCreateSettlementError):
            transaction.commit_external_thread_create(
                attempt,
                thread_id="thread-1",
                local_commit=local_commit,
            )
        local_commit.assert_called_once_with()

    def test_external_unknown_consumes_only_that_attempt(self) -> None:
        transaction, _, _ = self._transaction(Mock())
        first = transaction.begin_external_thread_create()
        transaction.mark_external_thread_create_outcome_unknown(
            first,
            TimeoutError("connection lost"),
        )

        with self.assertRaises(ThreadCreateSettlementError):
            transaction.mark_external_thread_create_outcome_unknown(
                first,
                TimeoutError("again"),
            )
        second = transaction.begin_external_thread_create()
        created = transaction.commit_external_thread_create(
            second,
            thread_id="thread-2",
            local_commit=lambda: "ok",
        )
        self.assertEqual(created.response, "thread-2")

    def test_connection_invalidation_rejects_late_external_response(self) -> None:
        transaction, _, _ = self._transaction(Mock())
        stale = transaction.begin_external_thread_create()

        transaction.invalidate_connection()

        with self.assertRaises(ThreadCreateSettlementError):
            transaction.commit_external_thread_create(
                stale,
                thread_id="thread-1",
                local_commit=Mock(),
            )
        current = transaction.begin_external_thread_create()
        self.assertNotEqual(stale._generation, current._generation)

    def test_concurrent_external_settlement_runs_callback_once(self) -> None:
        transaction, _, _ = self._transaction(Mock())
        attempt = transaction.begin_external_thread_create()
        barrier = threading.Barrier(2)
        calls: list[str] = []
        errors: list[Exception] = []

        def settle() -> None:
            barrier.wait()
            try:
                transaction.commit_external_thread_create(
                    attempt,
                    thread_id="thread-1",
                    local_commit=lambda: calls.append("commit"),
                )
            except Exception as exc:  # one exact loser is expected
                errors.append(exc)

        workers = [threading.Thread(target=settle) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)

        self.assertEqual(calls, ["commit"])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ThreadCreateSettlementError)
