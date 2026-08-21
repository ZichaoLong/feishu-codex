import threading
import unittest
from unittest.mock import Mock

from bot.adapters.base import (
    ThreadResumePage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.thread_create_transaction import ThreadCreateLocalCommitFailed
from bot.thread_runtime_authority import (
    ThreadResumeLocalCommitFailed,
    ThreadResumeLocalFailurePolicy,
    ThreadResumeInProgress,
    ThreadResumeOutcomeUnknown,
    ThreadResumePreSendGuardRejected,
    ThreadResumeSettlementError,
    ThreadResumeSettlementOutcome,
    ThreadRuntimeAuthority,
    ThreadStartBlockedByUnsubscribe,
    ThreadUnsubscribeInProgress,
    ThreadUnsubscribeOutcomeUnknown,
    ThreadUnsubscribeSettlementError,
)


def _snapshot(
    thread_id: str = "thread-1",
    model: str | None = "gpt-effective",
) -> ThreadSnapshot:
    return ThreadSnapshot(
        summary=ThreadSummary(
            thread_id=thread_id,
            cwd="/tmp",
            name="",
            preview="",
            created_at=0,
            updated_at=0,
            source="appServer",
            status="idle",
        ),
        effective_model=model,
        effective_reasoning_effort=None,
        effective_approval_policy=None,
        effective_permissions_profile_id=None,
    )


def _resume_page(
    thread_id: str = "thread-1",
    model: str | None = "gpt-effective",
) -> ThreadResumePage:
    return ThreadResumePage(
        snapshot=_snapshot(thread_id, model),
        initial_turns_page=ThreadTurnsPage(),
    )


def _record_settings(
    facts: ThreadEffectiveSettingsRegistry,
    thread_id: str,
    model: str,
    *,
    source: str,
) -> None:
    facts.record_start_or_resume(
        thread_id,
        model=model,
        reasoning_effort=None,
        approval_policy=None,
        permissions_profile_id=None,
        source=source,  # type: ignore[arg-type]
    )


def _settings_notification(model: str) -> dict:
    return {
        "model": model,
        "effort": None,
        "approvalPolicy": "never",
        "activePermissionProfile": None,
    }


class ThreadRuntimeAuthorityTests(unittest.TestCase):
    def _make_authority(
        self,
        adapter: Mock,
        *,
        facts: ThreadEffectiveSettingsRegistry | None = None,
        acquire=None,
        release=None,
        known_no_effect=None,
    ) -> tuple[ThreadRuntimeAuthority, ThreadEffectiveSettingsRegistry]:
        effective_settings = facts or ThreadEffectiveSettingsRegistry()
        authority = ThreadRuntimeAuthority(
            adapter=adapter,
            effective_settings=effective_settings,
            acquire_runtime_lease=acquire or (lambda _thread_id: True),
            release_runtime_lease=release or (lambda _thread_id: None),
            resume_failure_known_no_effect=(
                known_no_effect
                or (lambda exc: isinstance(exc, CodexRpcPreSendError))
            ),
        )
        return authority, effective_settings

    def test_create_local_failure_does_not_quarantine_other_mutations(self) -> None:
        adapter = Mock()
        adapter.create_thread.return_value = _snapshot()
        authority, _facts = self._make_authority(adapter)

        with self.assertRaises(ThreadCreateLocalCommitFailed) as raised:
            authority.create_and_commit_thread(
                local_commit=Mock(side_effect=RuntimeError("owner commit failed")),
                cwd="/tmp",
            )
        self.assertEqual(raised.exception.thread_id, "thread-1")
        self.assertEqual(raised.exception.stage, "local_owner")
        adapter.reset_mock()

        authority.update_thread_settings("thread-1", model="new")
        authority.start_turn(thread_id="thread-1")
        authority.unsubscribe_thread("thread-1")

        adapter.update_thread_settings.assert_called_once()
        adapter.start_turn.assert_called_once()
        adapter.unsubscribe_thread.assert_called_once_with("thread-1")

    def test_lease_rejection_precedes_model_fact_invalidation(self) -> None:
        adapter = Mock()

        def reject_lease(_thread_id: str) -> bool:
            raise RuntimeError("owned elsewhere")

        authority, facts = self._make_authority(adapter, acquire=reject_lease)
        _record_settings(facts, "thread-1", "old", source="thread_start")

        with self.assertRaisesRegex(RuntimeError, "owned elsewhere"):
            authority.begin_resume_thread("thread-1", model="new")

        adapter.resume_thread.assert_not_called()
        self.assertEqual(facts.resolve_model_for_request("thread-1"), "old")

    def test_local_preparation_failure_releases_only_a_new_lease(self) -> None:
        for newly_acquired in (True, False):
            with self.subTest(newly_acquired=newly_acquired):
                adapter = Mock()
                facts = Mock(spec=ThreadEffectiveSettingsRegistry)
                facts.invalidate_requested_settings_if_different.side_effect = RuntimeError(
                    "model registry unavailable"
                )
                released: list[str] = []
                classifier = Mock(return_value=False)
                authority, _ = self._make_authority(
                    adapter,
                    facts=facts,
                    acquire=lambda _thread_id: newly_acquired,
                    release=released.append,
                    known_no_effect=classifier,
                )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "model registry unavailable",
                ):
                    authority.begin_resume_thread("thread-1", model="new")

                self.assertEqual(released, ["thread-1"] if newly_acquired else [])
                classifier.assert_not_called()
                adapter.resume_thread.assert_not_called()

    def test_resume_unknown_invalidates_every_explicit_setting_before_late_start(
        self,
    ) -> None:
        adapter = Mock()
        adapter.resume_thread.side_effect = CodexRpcTransportError(
            "thread/resume",
            {"code": -32000, "message": "connection closed"},
        )
        authority, facts = self._make_authority(adapter)
        facts.record_start_or_resume(
            "thread-1",
            model="base-a",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )

        with self.assertRaises(ThreadResumeOutcomeUnknown):
            authority.begin_resume_thread(
                "thread-1",
                model="base-b",
                config_overrides={"model_reasoning_effort": "ultra"},
                approval_policy="on-request",
                permissions_profile_id=":danger-full-access",
            )

        authority.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-late"}},
        )
        disclosure = facts.disclosure_for_active_turn("thread-1", "turn-late")
        self.assertEqual(disclosure.model.source, "unknown")
        self.assertEqual(disclosure.reasoning_effort.source, "unknown")
        self.assertEqual(disclosure.approval_policy.source, "unknown")
        self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_exact_guard_rejection_is_known_pre_send(self) -> None:
        for guard_error in (None, RuntimeError("guard unavailable")):
            with self.subTest(guard_error=guard_error):
                adapter = Mock()
                released: list[str] = []
                authority, facts = self._make_authority(
                    adapter,
                    release=released.append,
                )
                _record_settings(facts, "thread-1", "old", source="thread_start")

                def reject_after_local_preparation() -> bool:
                    self.assertIsNone(facts.resolve_model_for_request("thread-1"))
                    if guard_error is not None:
                        raise guard_error
                    return False

                with self.assertRaises(ThreadResumePreSendGuardRejected):
                    authority.begin_resume_thread(
                        "thread-1",
                        model="new",
                        exact_mutation_guard=reject_after_local_preparation,
                    )

                self.assertEqual(released, ["thread-1"])
                adapter.resume_thread.assert_not_called()

    def test_known_no_effect_failure_releases_only_a_new_lease(self) -> None:
        for newly_acquired in (True, False):
            with self.subTest(newly_acquired=newly_acquired):
                adapter = Mock()
                adapter.resume_thread.side_effect = CodexRpcPreSendError(
                    "thread/resume",
                    ValueError("known rejection"),
                )
                released: list[str] = []
                authority, _facts = self._make_authority(
                    adapter,
                    acquire=lambda _thread_id: newly_acquired,
                    release=released.append,
                )

                with self.assertRaisesRegex(CodexRpcPreSendError, "known rejection"):
                    authority.begin_resume_thread("thread-1")

                self.assertEqual(released, ["thread-1"] if newly_acquired else [])

    def test_unknown_outcome_retains_lease_without_quarantining_later_calls(
        self,
    ) -> None:
        adapter = Mock()
        adapter.resume_thread.side_effect = (
            CodexRpcError(
                "thread/resume",
                {"code": -32603, "message": "response assembly failed"},
            ),
            _snapshot(),
        )
        acquisitions = iter((True, False))
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            acquire=lambda _thread_id: next(acquisitions),
            release=released.append,
        )

        with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
            authority.begin_resume_thread("thread-1")

        self.assertTrue(raised.exception.lease_receipt.lease_was_newly_acquired)
        authority.update_thread_settings("thread-1", model="new")
        replacement = authority.begin_resume_thread("thread-1")
        self.assertGreater(
            replacement.lease_receipt.generation,
            raised.exception.lease_receipt.generation,
        )
        replacement.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )
        self.assertEqual(released, [])

    def test_paged_resume_unknown_outcome_retains_a_new_lease(self) -> None:
        adapter = Mock()
        adapter.resume_thread_page.side_effect = CodexRpcError(
            "thread/resume",
            {"code": -32603, "message": "response assembly failed"},
        )
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            release=released.append,
        )

        with self.assertRaises(ThreadResumeOutcomeUnknown):
            authority.begin_resume_thread_page("thread-1", limit=25)

        self.assertEqual(released, [])

    def test_unknown_resume_logs_bounded_typed_failure_without_retrying(self) -> None:
        failures = (
            (
                TimeoutError("Codex request timed out: thread/resume"),
                "failure_kind=timeout",
                "Codex request timed out: thread/resume",
            ),
            (
                CodexRpcTransportError(
                    "thread/resume",
                    {"code": -32000, "message": "connection closed"},
                ),
                "failure_kind=transport",
                "code=-32000 detail=connection closed",
            ),
            (
                CodexRpcProtocolError(
                    "thread/resume",
                    "missing initialTurnsPage",
                ),
                "failure_kind=protocol",
                "detail=missing initialTurnsPage",
            ),
            (
                CodexRpcError(
                    "thread/resume",
                    {"code": -32603, "message": "界" * 600},
                ),
                "failure_kind=rpc",
                "method=thread/resume code=-32603",
            ),
        )
        for failure, expected_kind, expected_detail in failures:
            with self.subTest(failure=type(failure).__name__):
                adapter = Mock()
                adapter.resume_thread.side_effect = failure
                released: list[str] = []
                authority, _facts = self._make_authority(
                    adapter,
                    release=released.append,
                )

                with self.assertLogs(
                    "bot.thread_runtime_authority",
                    level="WARNING",
                ) as captured:
                    with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                        authority.begin_resume_thread("thread-1")

                rendered = captured.records[-1].getMessage()
                self.assertIn(expected_kind, rendered)
                self.assertIn(expected_detail, rendered)
                structured_failure = rendered.split(" failure=", 1)[1]
                self.assertLessEqual(len(structured_failure.encode("utf-8")), 512)
                self.assertIs(raised.exception.__cause__, failure)
                self.assertEqual(released, [])
                adapter.resume_thread.assert_called_once()

    def test_unknown_resume_bounds_every_structured_failure_field(self) -> None:
        failures = (
            TimeoutError("timeout\r\n" + "A" * 800),
            CodexRpcTransportError(
                "method-" + "M" * 800,
                {"code": -32000, "message": "transport " + "界" * 800},
            ),
            CodexRpcProtocolError(
                "method-" + "M" * 800,
                "protocol\r\n" + "界" * 800,
            ),
            CodexRpcError(
                "thread/resume",
                {"code": "C" * 800, "message": "rpc " + "界" * 800},
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                adapter = Mock()
                adapter.resume_thread.side_effect = failure
                authority, _facts = self._make_authority(adapter)

                with self.assertLogs(
                    "bot.thread_runtime_authority",
                    level="WARNING",
                ) as captured:
                    with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                        authority.begin_resume_thread("thread-1")

                rendered = captured.records[-1].getMessage()
                structured_failure = rendered.split(" failure=", 1)[1]
                self.assertLessEqual(len(structured_failure.encode("utf-8")), 512)
                self.assertTrue(structured_failure.endswith("…"))
                self.assertNotIn("\r", structured_failure)
                self.assertNotIn("\n", structured_failure)
                self.assertIs(raised.exception.__cause__, failure)
                adapter.resume_thread.assert_called_once()

    def test_unknown_resume_generic_failure_exposes_only_its_type(self) -> None:
        prompt_secret = "PROMPT-SECRET-MUST-NOT-APPEAR"
        token_secret = "TOKEN-SECRET-MUST-NOT-APPEAR"
        failure = RuntimeError(f"prompt={prompt_secret} token={token_secret}")
        adapter = Mock()
        adapter.resume_thread.side_effect = failure
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            release=released.append,
        )

        with self.assertLogs(
            "bot.thread_runtime_authority",
            level="WARNING",
        ) as captured:
            with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                authority.begin_resume_thread("thread-1")

        rendered = captured.records[-1].getMessage()
        public_error = str(raised.exception)
        public_args = repr(raised.exception.args)
        self.assertIn("failure_kind=RuntimeError failure=-", rendered)
        for secret in (prompt_secret, token_secret):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, public_error)
            self.assertNotIn(secret, public_args)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(released, [])
        adapter.resume_thread.assert_called_once()

    def test_unknown_resume_replaces_lone_surrogate_without_changing_classification(self) -> None:
        failure = CodexRpcError(
            "thread/resume",
            {"code": -32603, "message": f"invalid {chr(0xD800)} response"},
        )
        adapter = Mock()
        adapter.resume_thread.side_effect = failure
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            release=released.append,
        )

        with self.assertLogs(
            "bot.thread_runtime_authority",
            level="WARNING",
        ) as captured:
            with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                authority.begin_resume_thread("thread-1")

        rendered = captured.records[-1].getMessage()
        structured_failure = rendered.split(" failure=", 1)[1]
        structured_failure.encode("utf-8")
        self.assertIn("invalid ? response", structured_failure)
        self.assertIs(raised.exception.__cause__, failure)
        self.assertEqual(released, [])
        adapter.resume_thread.assert_called_once()

    def test_classifier_failure_treats_sent_request_as_unknown(self) -> None:
        adapter = Mock()
        adapter.resume_thread.side_effect = RuntimeError("resume failed")
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            release=released.append,
            known_no_effect=Mock(side_effect=RuntimeError("classifier failed")),
        )

        with self.assertLogs("bot.thread_runtime_authority", level="ERROR"):
            with self.assertRaises(ThreadResumeOutcomeUnknown) as raised:
                authority.begin_resume_thread("thread-1")

        self.assertEqual(str(raised.exception.__cause__), "resume failed")
        self.assertEqual(released, [])

    def test_successful_resume_commits_exact_callback_once(self) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        authority, _facts = self._make_authority(adapter)
        pending = authority.begin_resume_thread("thread-1")
        local_commit = Mock(return_value="bound")

        result = pending.commit_local_state(
            local_commit,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

        self.assertEqual(result, "bound")
        with self.assertRaises(ThreadResumeSettlementError) as stale:
            pending.commit_local_state(
                local_commit,
                failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            )
        self.assertEqual(
            stale.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION,
        )
        local_commit.assert_called_once_with()

    def test_retain_reports_local_failure_without_blocking_later_resume(self) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        acquisitions = iter((True, False))
        authority, _facts = self._make_authority(
            adapter,
            acquire=lambda _thread_id: next(acquisitions),
        )
        pending = authority.begin_resume_thread("thread-1")

        with self.assertRaises(ThreadResumeLocalCommitFailed) as raised:
            pending.commit_local_state(
                Mock(side_effect=RuntimeError("bind failed")),
                failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            )

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.RETAINED,
        )
        self.assertTrue(raised.exception.recovery_required)
        replacement = authority.begin_resume_thread("thread-1")
        replacement.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

    def test_compensate_cleans_only_a_newly_acquired_resume_lease(self) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        released: list[str] = []
        authority, facts = self._make_authority(
            adapter,
            release=released.append,
        )
        pending = authority.begin_resume_thread("thread-1")

        with self.assertRaises(ThreadResumeLocalCommitFailed) as raised:
            pending.commit_local_state(
                Mock(side_effect=RuntimeError("bind failed")),
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.COMPENSATED,
        )
        self.assertFalse(raised.exception.recovery_required)
        adapter.unsubscribe_thread.assert_called_once_with("thread-1")
        self.assertIsNone(facts.resolve_model_for_request("thread-1"))
        self.assertEqual(released, ["thread-1"])

    def test_compensate_does_not_clean_a_preexisting_lease(self) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        released: list[str] = []
        authority, facts = self._make_authority(
            adapter,
            acquire=lambda _thread_id: False,
            release=released.append,
        )
        pending = authority.begin_resume_thread("thread-1")

        with self.assertRaises(ThreadResumeLocalCommitFailed) as raised:
            pending.commit_local_state(
                Mock(side_effect=RuntimeError("bind failed")),
                failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            )

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.RETAINED,
        )
        self.assertFalse(raised.exception.recovery_required)
        adapter.unsubscribe_thread.assert_not_called()
        self.assertEqual(
            facts.resolve_model_for_request("thread-1"),
            "gpt-effective",
        )
        self.assertEqual(released, [])

    def test_compensation_failure_is_reported_without_a_recovery_registry(
        self,
    ) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        adapter.unsubscribe_thread.side_effect = RuntimeError("unsubscribe failed")
        acquisitions = iter((True, False))
        authority, _facts = self._make_authority(
            adapter,
            acquire=lambda _thread_id: next(acquisitions),
        )
        pending = authority.begin_resume_thread("thread-1")

        with self.assertLogs("bot.thread_runtime_authority", level="ERROR"):
            with self.assertRaises(ThreadResumeLocalCommitFailed) as raised:
                pending.commit_local_state(
                    Mock(side_effect=RuntimeError("bind failed")),
                    failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
                )

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.CLEANUP_PENDING,
        )
        adapter.unsubscribe_thread.side_effect = None
        replacement = authority.begin_resume_thread("thread-1")
        replacement.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

    def test_response_fact_failure_is_an_exact_retained_failure(self) -> None:
        adapter = Mock()
        adapter.resume_thread.return_value = _snapshot()
        facts = Mock(spec=ThreadEffectiveSettingsRegistry)
        facts.record_start_or_resume.side_effect = RuntimeError(
            "model registry unavailable"
        )
        authority, _ = self._make_authority(adapter, facts=facts)

        with self.assertRaises(ThreadResumeLocalCommitFailed) as raised:
            authority.begin_resume_thread("thread-1")

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.RETAINED,
        )
        self.assertTrue(raised.exception.recovery_required)

    def test_connection_or_reset_invalidates_only_delayed_exact_receipts(self) -> None:
        for invalidator_name in ("invalidate_connection", "confirm_backend_reset"):
            with self.subTest(invalidator=invalidator_name):
                adapter = Mock()
                adapter.resume_thread.return_value = _snapshot()
                acquisitions = iter((True, False))
                authority, facts = self._make_authority(
                    adapter,
                    acquire=lambda _thread_id: next(acquisitions),
                )
                _record_settings(
                    facts,
                    "thread-other",
                    "gpt-before-reset",
                    source="thread_resume",
                )
                delayed = authority.begin_resume_thread("thread-1")
                getattr(authority, invalidator_name)()
                self.assertIsNone(
                    facts.resolve_model_for_request("thread-other")
                )
                local_commit = Mock()

                with self.assertRaises(ThreadResumeSettlementError):
                    delayed.commit_local_state(
                        local_commit,
                        failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
                    )

                local_commit.assert_not_called()
                replacement = authority.begin_resume_thread("thread-1")
                replacement.commit_local_state(
                    lambda: None,
                    failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
                )

    def test_resume_rejects_blank_thread_id_before_acquiring_lease(self) -> None:
        adapter = Mock()
        acquire = Mock(return_value=True)
        authority, _facts = self._make_authority(adapter, acquire=acquire)

        with self.assertRaisesRegex(ValueError, "thread_id 不能为空"):
            authority.begin_resume_thread("  \t  ")

        acquire.assert_not_called()
        adapter.resume_thread.assert_not_called()

    def test_resume_page_records_only_effective_response_model(self) -> None:
        adapter = Mock()
        adapter.resume_thread_page.return_value = ThreadResumePage(
            snapshot=_snapshot(model="effective"),
            initial_turns_page=ThreadTurnsPage(),
        )
        authority, facts = self._make_authority(adapter)

        pending = authority.begin_resume_thread_page(
            "thread-1",
            limit=25,
            model="requested",
        )
        pending.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

        self.assertEqual(facts.resolve_model_for_request("thread-1"), "effective")
        adapter.resume_thread_page.assert_called_once_with(
            "thread-1",
            limit=25,
            model="requested",
            model_provider=None,
            config_overrides=None,
            approval_policy=None,
            permissions_profile_id=None,
        )

    def test_stale_prepared_resume_success_cannot_restore_effective_settings(
        self,
    ) -> None:
        adapter = Mock()
        facts = Mock(spec=ThreadEffectiveSettingsRegistry)
        authority, _facts = self._make_authority(adapter, facts=facts)
        prepared = authority.prepare_resume_thread_page("thread-1", limit=25)

        authority.invalidate_connection()
        facts.reset_mock()

        with self.assertRaises(ThreadResumeSettlementError) as raised:
            authority.settle_prepared_resume_thread_page(
                prepared,
                response=_resume_page(),
            )

        self.assertEqual(
            raised.exception.settlement.outcome,
            ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION,
        )
        facts.record_start_or_resume.assert_not_called()

    def test_paged_resume_prepare_rejects_invalid_inputs_before_lease(self) -> None:
        adapter = Mock()
        acquire = Mock(return_value=True)
        authority, _facts = self._make_authority(adapter, acquire=acquire)

        for kwargs in (
            {"limit": object()},
            {"limit": 25, "config_overrides": {"nested": object()}},
            {"limit": 25, "expected_connection_generation": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises((TypeError, ValueError)):
                    authority.prepare_resume_thread_page("thread-1", **kwargs)

        acquire.assert_not_called()
        adapter.resume_thread_page.assert_not_called()

    def test_paged_resume_prepare_failure_releases_new_lease_and_claim(
        self,
    ) -> None:
        adapter = Mock()
        facts = Mock(spec=ThreadEffectiveSettingsRegistry)
        facts.invalidate_requested_settings_if_different.side_effect = RuntimeError(
            "settings unavailable"
        )
        released: list[str] = []
        authority, _facts = self._make_authority(
            adapter,
            facts=facts,
            release=released.append,
        )

        with self.assertRaisesRegex(RuntimeError, "settings unavailable"):
            authority.prepare_resume_thread_page("thread-1", limit=25)

        self.assertEqual(released, ["thread-1"])
        facts.invalidate_requested_settings_if_different.side_effect = None
        prepared = authority.prepare_resume_thread_page("thread-1", limit=25)
        adapter.resume_thread_page.return_value = _resume_page()
        page = authority.execute_prepared_resume_thread_page(prepared)
        pending = authority.settle_prepared_resume_thread_page(
            prepared,
            response=page,
        )
        pending.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

    def test_prepared_resume_freezes_config_and_forwards_model_provider(self) -> None:
        adapter = Mock()
        adapter.resume_thread_page.return_value = _resume_page()
        authority, _facts = self._make_authority(adapter)
        config = {"nested": {"values": ["prepared"]}}

        prepared = authority.prepare_resume_thread_page(
            "thread-1",
            limit=25,
            model="gpt-requested",
            model_provider="openai",
            config_overrides=config,
            expected_connection_generation=7,
        )
        config["nested"]["values"].append("mutated")
        page = authority.execute_prepared_resume_thread_page(prepared)

        self.assertFalse(hasattr(prepared, "config_overrides"))
        adapter.resume_thread_page.assert_called_once_with(
            "thread-1",
            limit=25,
            model="gpt-requested",
            model_provider="openai",
            config_overrides={"nested": {"values": ["prepared"]}},
            approval_policy=None,
            permissions_profile_id=None,
            expected_connection_generation=7,
        )
        pending = authority.settle_prepared_resume_thread_page(
            prepared,
            response=page,
        )
        pending.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

    def test_prepared_resume_effect_can_be_claimed_only_once(self) -> None:
        adapter = Mock()
        adapter.resume_thread_page.return_value = _resume_page()
        authority, _facts = self._make_authority(adapter)
        prepared = authority.prepare_resume_thread_page("thread-1", limit=25)

        page = authority.execute_prepared_resume_thread_page(prepared)
        with self.assertRaises(ThreadResumeInProgress):
            authority.execute_prepared_resume_thread_page(prepared)

        adapter.resume_thread_page.assert_called_once()
        pending = authority.settle_prepared_resume_thread_page(
            prepared,
            response=page,
        )
        pending.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )

    def test_unacquired_claim_abandon_cannot_retire_acquired_receipt(self) -> None:
        released: list[str] = []
        authority, _facts = self._make_authority(
            Mock(),
            release=released.append,
        )
        claim = authority.claim_resume_thread_page("thread-1")
        receipt = authority.acquire_claimed_resume_thread_page(claim)

        authority.abandon_resume_thread_page_claim(claim)

        with self.assertRaises(ThreadResumeInProgress):
            authority.claim_resume_thread_page("thread-1")
        authority.abandon_acquired_resume_thread_page(receipt)
        successor = authority.claim_resume_thread_page("thread-1")
        authority.abandon_resume_thread_page_claim(successor)
        self.assertEqual(released, ["thread-1"])

    def test_same_thread_resume_rejects_while_lease_acquire_is_in_flight(
        self,
    ) -> None:
        adapter = Mock()
        acquire_entered = threading.Event()
        allow_acquire = threading.Event()

        def acquire(_thread_id: str) -> bool:
            acquire_entered.set()
            if not allow_acquire.wait(2):
                raise TimeoutError("test did not release lease acquisition")
            return True

        authority, _facts = self._make_authority(adapter, acquire=acquire)
        prepared: list[object] = []
        failures: list[BaseException] = []

        def first_prepare() -> None:
            try:
                prepared.append(
                    authority.prepare_resume_thread_page("thread-1", limit=25)
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=first_prepare)
        worker.start()
        self.assertTrue(acquire_entered.wait(1), "lease acquire did not start")

        with self.assertRaises(ThreadResumeInProgress):
            authority.begin_resume_thread_page("thread-1", limit=25)
        adapter.resume_thread_page.assert_not_called()

        allow_acquire.set()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "lease acquire worker did not finish")
        self.assertEqual(failures, [])
        self.assertEqual(len(prepared), 1)
        authority.invalidate_connection()

    def test_invalidated_in_flight_acquire_cleans_before_successor_admission(
        self,
    ) -> None:
        adapter = Mock()
        acquire_entered = threading.Event()
        allow_acquire = threading.Event()
        release_entered = threading.Event()
        allow_release = threading.Event()
        acquisition_count = 0
        released: list[str] = []

        def acquire(_thread_id: str) -> bool:
            nonlocal acquisition_count
            acquisition_count += 1
            if acquisition_count == 1:
                acquire_entered.set()
                if not allow_acquire.wait(2):
                    raise TimeoutError("test did not release old acquisition")
            return True

        def release(thread_id: str) -> None:
            released.append(thread_id)
            release_entered.set()
            if not allow_release.wait(2):
                raise TimeoutError("test did not release old cleanup")

        authority, _facts = self._make_authority(
            adapter,
            acquire=acquire,
            release=release,
        )
        failures: list[BaseException] = []

        def old_prepare() -> None:
            try:
                authority.prepare_resume_thread_page("thread-1", limit=25)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=old_prepare)
        worker.start()
        self.assertTrue(acquire_entered.wait(1), "old acquisition did not start")
        authority.invalidate_connection()
        allow_acquire.set()
        self.assertTrue(release_entered.wait(1), "old cleanup did not start")

        with self.assertRaises(ThreadResumeInProgress):
            authority.prepare_resume_thread_page("thread-1", limit=25)

        allow_release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "old acquisition worker did not finish")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ThreadResumePreSendGuardRejected)
        self.assertEqual(released, ["thread-1"])

        successor = authority.prepare_resume_thread_page("thread-1", limit=25)
        adapter.resume_thread_page.return_value = _resume_page()
        page = authority.execute_prepared_resume_thread_page(successor)
        pending = authority.settle_prepared_resume_thread_page(
            successor,
            response=page,
        )
        pending.commit_local_state(
            lambda: None,
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
        )
        self.assertEqual(released, ["thread-1"])

    def test_different_threads_can_acquire_resume_leases_in_parallel(self) -> None:
        adapter = Mock()
        first_entered = threading.Event()
        allow_first = threading.Event()

        def acquire(thread_id: str) -> bool:
            if thread_id == "thread-a":
                first_entered.set()
                if not allow_first.wait(2):
                    raise TimeoutError("test did not release thread-a")
            return True

        authority, _facts = self._make_authority(adapter, acquire=acquire)
        first_prepared: list[object] = []
        failures: list[BaseException] = []

        def prepare_first() -> None:
            try:
                first_prepared.append(
                    authority.prepare_resume_thread_page("thread-a", limit=25)
                )
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=prepare_first)
        worker.start()
        self.assertTrue(first_entered.wait(1), "thread-a acquisition did not start")

        second = authority.prepare_resume_thread_page("thread-b", limit=25)
        self.assertEqual(second.lease_receipt.thread_id, "thread-b")

        allow_first.set()
        worker.join(2)
        self.assertFalse(worker.is_alive(), "thread-a acquisition did not finish")
        self.assertEqual(failures, [])
        self.assertEqual(len(first_prepared), 1)
        authority.invalidate_connection()

    def test_settings_ack_does_not_install_requested_model(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "old", source="thread_resume")

        authority.update_thread_settings("thread-1", model="requested")

        self.assertIsNone(facts.resolve_model_for_request("thread-1"))
        authority.observe_notification(
            "thread/settings/updated",
            {
                "threadId": "thread-1",
                "threadSettings": _settings_notification("effective"),
            },
        )
        self.assertEqual(facts.resolve_model_for_request("thread-1"), "effective")

    def test_same_model_settings_noop_retains_effective_fact(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_resume")

        authority.update_thread_settings("thread-1", model="vision")

        self.assertEqual(facts.resolve_model_for_request("thread-1"), "vision")
        adapter.update_thread_settings.assert_called_once_with(
            "thread-1",
            approval_policy=None,
            permissions_profile_id=None,
            model="vision",
            reasoning_effort=None,
        )

    def test_turn_start_invalidates_each_differing_explicit_setting(self) -> None:
        adapter = Mock()
        adapter.start_turn.return_value = {"turn": {"id": "turn-2"}}
        authority, facts = self._make_authority(adapter)
        facts.record_start_or_resume(
            "thread-1",
            model="base-a",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        authority.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        authority.start_turn(
            thread_id="thread-1",
            model="base-a",
            reasoning_effort="ultra",
            approval_policy="on-request",
            permissions_profile_id=":danger-full-access",
            input_items=[{"type": "text", "text": "next"}],
        )

        disclosure = facts.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(disclosure.model.source, "inherited")
        self.assertEqual(disclosure.reasoning_effort.source, "unknown")
        self.assertEqual(disclosure.approval_policy.source, "unknown")
        self.assertEqual(disclosure.permissions_profile_id.source, "unknown")

    def test_settings_update_invalidates_each_differing_base_field_only(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        facts.record_start_or_resume(
            "thread-1",
            model="base-a",
            reasoning_effort="high",
            approval_policy="never",
            permissions_profile_id=":workspace",
            source="thread_resume",
        )
        authority.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        authority.update_thread_settings(
            "thread-1",
            model="base-b",
            reasoning_effort="ultra",
            approval_policy="on-request",
            permissions_profile_id=":danger-full-access",
        )

        active = facts.disclosure_for_active_turn("thread-1", "turn-1")
        self.assertEqual(active.model.value, "base-a")
        self.assertEqual(active.reasoning_effort.value, "high")
        authority.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(facts.resolve_model_for_request("thread-1"))

    def test_settings_selector_compares_base_not_active_reroute(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "base-a", source="thread_resume")
        authority.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        authority.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute-b"},
        )

        authority.update_thread_settings("thread-1", model="reroute-b")

        self.assertEqual(facts.resolve_model_for_request("thread-1"), "reroute-b")
        authority.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        self.assertIsNone(facts.resolve_model_for_request("thread-1"))

    def test_same_base_settings_noop_preserves_base_under_reroute(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "base-a", source="thread_resume")
        authority.observe_notification(
            "turn/started",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )
        authority.observe_notification(
            "model/rerouted",
            {"threadId": "thread-1", "turnId": "turn-1", "toModel": "reroute-b"},
        )

        authority.update_thread_settings("thread-1", model="base-a")
        authority.observe_notification(
            "turn/completed",
            {"threadId": "thread-1", "turn": {"id": "turn-1"}},
        )

        self.assertEqual(facts.resolve_model_for_request("thread-1"), "base-a")

    def test_settings_notification_before_ack_is_not_erased(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "old", source="thread_resume")

        def update_settings(_thread_id: str, **_kwargs) -> None:
            authority.observe_notification(
                "thread/settings/updated",
                {
                    "threadId": "thread-1",
                    "threadSettings": _settings_notification("effective"),
                },
            )

        adapter.update_thread_settings.side_effect = update_settings

        authority.update_thread_settings("thread-1", model="requested")

        self.assertEqual(facts.resolve_model_for_request("thread-1"), "effective")

    def test_turn_start_invalidates_mismatch_only_at_write_boundary(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_resume")
        self.assertIsNone(
            facts.resolve_model_for_request("thread-1", requested_model="text")
        )
        self.assertEqual(facts.resolve_model_for_request("thread-1"), "vision")
        observed_at_send: list[str | None] = []

        def start_turn(**_kwargs):
            observed_at_send.append(facts.resolve_model_for_request("thread-1"))
            return {"turn": {"id": "turn-1"}}

        adapter.start_turn.side_effect = start_turn

        authority.start_turn(
            thread_id="thread-1",
            model="text",
            input_items=[{"type": "text", "text": "hello"}],
        )

        self.assertEqual(observed_at_send, [None])
        self.assertIsNone(facts.resolve_model_for_request("thread-1"))

    def test_unsubscribe_claim_blocks_same_thread_start_before_local_mutation(
        self,
    ) -> None:
        adapter = Mock()
        adapter.start_turn.return_value = {"turn": {"id": "turn-2"}}
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_resume")
        prepared = authority.prepare_unsubscribe_thread("thread-1")

        with self.assertRaises(ThreadStartBlockedByUnsubscribe):
            authority.start_turn(thread_id="thread-1", model="text")

        adapter.start_turn.assert_not_called()
        self.assertEqual(facts.resolve_model_for_request("thread-1"), "vision")

        authority.start_turn(thread_id="thread-2", model="text")
        adapter.start_turn.assert_called_once_with(
            thread_id="thread-2",
            model="text",
            reasoning_effort=None,
            approval_policy=None,
            permissions_profile_id=None,
        )

        authority.abandon_prepared_unsubscribe_thread(prepared)
        authority.start_turn(thread_id="thread-1", model="text")
        self.assertEqual(adapter.start_turn.call_count, 2)

    def test_active_start_blocks_only_same_thread_unsubscribe(self) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def start_turn(**_kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release turn/start")
            return {"turn": {"id": "turn-1"}}

        adapter.start_turn.side_effect = start_turn

        def run_start() -> None:
            try:
                authority.start_turn(thread_id="thread-1")
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run_start)
        worker.start()
        try:
            self.assertTrue(entered.wait(timeout=2))
            with self.assertRaises(ThreadUnsubscribeInProgress):
                authority.prepare_unsubscribe_thread("thread-1")
            other = authority.prepare_unsubscribe_thread("thread-2")
            authority.abandon_prepared_unsubscribe_thread(other)
        finally:
            release.set()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        successor = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(successor)

    def test_same_thread_starts_are_not_serialized_and_all_fence_unsubscribe(
        self,
    ) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        call_lock = threading.Lock()
        both_entered = threading.Event()
        releases = (threading.Event(), threading.Event())
        entered_count = 0
        errors: list[BaseException] = []

        def start_turn(**kwargs):
            nonlocal entered_count
            call_index = int(kwargs["client_user_message_id"].removeprefix("start-"))
            with call_lock:
                entered_count += 1
                if entered_count == 2:
                    both_entered.set()
            if not releases[call_index].wait(timeout=2):
                raise TimeoutError("test did not release concurrent turn/start")
            return {"turn": {"id": f"turn-{call_index + 1}"}}

        adapter.start_turn.side_effect = start_turn

        def run_start(call_index: int) -> None:
            try:
                authority.start_turn(
                    thread_id="thread-1",
                    client_user_message_id=f"start-{call_index}",
                )
            except BaseException as exc:
                errors.append(exc)

        workers = (
            threading.Thread(target=run_start, args=(0,)),
            threading.Thread(target=run_start, args=(1,)),
        )
        for worker in workers:
            worker.start()
        try:
            self.assertTrue(both_entered.wait(timeout=2))
            with self.assertRaises(ThreadUnsubscribeInProgress):
                authority.prepare_unsubscribe_thread("thread-1")

            releases[0].set()
            workers[0].join(timeout=2)
            self.assertFalse(workers[0].is_alive())
            with self.assertRaises(ThreadUnsubscribeInProgress):
                authority.prepare_unsubscribe_thread("thread-1")
        finally:
            for release in releases:
                release.set()
            for worker in workers:
                worker.join(timeout=2)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        successor = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(successor)

    def test_failed_start_retires_unsubscribe_fence(self) -> None:
        adapter = Mock()
        adapter.start_turn.side_effect = RuntimeError("wire failed")
        authority, _facts = self._make_authority(adapter)

        with self.assertRaisesRegex(RuntimeError, "wire failed"):
            authority.start_turn(thread_id="thread-1")

        prepared = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(prepared)

    def test_lifecycle_clears_fact_only_after_adapter_success(self) -> None:
        for method_name in ("unsubscribe_thread", "archive_thread", "delete_thread"):
            with self.subTest(method=method_name, outcome="success"):
                adapter = Mock()
                authority, facts = self._make_authority(adapter)
                _record_settings(facts, "thread-1", "vision", source="thread_resume")

                getattr(authority, method_name)("thread-1")

                self.assertIsNone(facts.resolve_model_for_request("thread-1"))
            with self.subTest(method=method_name, outcome="failure"):
                adapter = Mock()
                getattr(adapter, method_name).side_effect = RuntimeError("failed")
                authority, facts = self._make_authority(adapter)
                _record_settings(facts, "thread-1", "vision", source="thread_resume")

                if method_name == "unsubscribe_thread":
                    with self.assertRaises(ThreadUnsubscribeOutcomeUnknown) as raised:
                        getattr(authority, method_name)("thread-1")
                    self.assertIsInstance(raised.exception.__cause__, RuntimeError)
                else:
                    with self.assertRaisesRegex(RuntimeError, "failed"):
                        getattr(authority, method_name)("thread-1")

                self.assertEqual(facts.resolve_model_for_request("thread-1"), "vision")

    def test_staged_unsubscribe_pins_generation_and_holds_claim_until_commit(
        self,
    ) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_resume")
        prepared = authority.prepare_unsubscribe_thread(
            "thread-1",
            expected_connection_generation=7,
        )

        authority.execute_prepared_unsubscribe_thread(prepared)
        adapter.unsubscribe_thread.assert_called_once_with(
            "thread-1",
            expected_connection_generation=7,
        )
        with self.assertRaises(ThreadUnsubscribeInProgress):
            authority.prepare_unsubscribe_thread("thread-1")
        with self.assertRaises(ThreadResumeInProgress):
            authority.prepare_resume_thread_page("thread-1", limit=1)

        pending = authority.settle_prepared_unsubscribe_thread(
            prepared,
            upstream_succeeded=True,
        )
        self.assertIsNone(facts.resolve_model_for_request("thread-1"))
        with self.assertRaises(ThreadResumeInProgress):
            authority.prepare_resume_thread_page("thread-1", limit=1)

        committed: list[str] = []
        pending.commit_local_state(lambda: committed.append("done"))
        self.assertEqual(committed, ["done"])
        successor = authority.prepare_resume_thread_page("thread-1", limit=1)
        self.assertEqual(successor.lease_receipt.thread_id, "thread-1")

    def test_unsubscribe_settlement_failure_retires_its_exact_claim(self) -> None:
        adapter = Mock()
        facts = Mock(spec=ThreadEffectiveSettingsRegistry)
        facts.clear_thread.side_effect = RuntimeError("settings cleanup failed")
        authority, _facts = self._make_authority(adapter, facts=facts)
        prepared = authority.prepare_unsubscribe_thread("thread-1")
        authority.execute_prepared_unsubscribe_thread(prepared)

        with self.assertRaisesRegex(RuntimeError, "settings cleanup failed"):
            authority.settle_prepared_unsubscribe_thread(
                prepared,
                upstream_succeeded=True,
            )

        successor = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(successor)

    def test_late_stale_settlement_cannot_retire_same_thread_successor(self) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        stale = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(stale)
        successor = authority.prepare_unsubscribe_thread("thread-1")

        with self.assertRaises(ThreadUnsubscribeSettlementError):
            authority.settle_prepared_unsubscribe_thread(
                stale,
                subscription_already_absent=True,
            )
        with self.assertRaises(ThreadStartBlockedByUnsubscribe):
            authority.start_turn(thread_id="thread-1")

        authority.abandon_prepared_unsubscribe_thread(successor)
        authority.start_turn(thread_id="thread-1")
        adapter.start_turn.assert_called_once()

    def test_invalidated_pending_unsubscribe_retires_claim_on_commit_failure(
        self,
    ) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        prepared = authority.prepare_unsubscribe_thread("thread-1")
        pending = authority.settle_prepared_unsubscribe_thread(
            prepared,
            subscription_already_absent=True,
        )
        authority.invalidate_connection()

        with self.assertRaises(ThreadUnsubscribeSettlementError):
            pending.commit_local_state(lambda: None)

        successor = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(successor)

    def test_sync_unsubscribe_retires_claim_after_response_invalidation(self) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        adapter.unsubscribe_thread.side_effect = (
            lambda *_args, **_kwargs: authority.invalidate_connection()
        )

        with self.assertRaises(ThreadUnsubscribeSettlementError):
            authority.unsubscribe_thread("thread-1")

        successor = authority.prepare_unsubscribe_thread("thread-1")
        authority.abandon_prepared_unsubscribe_thread(successor)

    def test_staged_unsubscribe_is_one_shot_and_unknown_is_not_replayed(self) -> None:
        adapter = Mock()
        adapter.unsubscribe_thread.side_effect = RuntimeError("wire lost")
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_resume")
        prepared = authority.prepare_unsubscribe_thread("thread-1")

        with self.assertRaisesRegex(RuntimeError, "wire lost"):
            authority.execute_prepared_unsubscribe_thread(prepared)
        with self.assertRaises(ThreadUnsubscribeInProgress):
            authority.execute_prepared_unsubscribe_thread(prepared)
        with self.assertRaises(ThreadUnsubscribeOutcomeUnknown):
            authority.settle_prepared_unsubscribe_thread(
                prepared,
                error=RuntimeError("wire lost"),
            )

        self.assertEqual(adapter.unsubscribe_thread.call_count, 1)
        self.assertEqual(facts.resolve_model_for_request("thread-1"), "vision")
        authority.prepare_resume_thread_page("thread-1", limit=1)

    def test_already_absent_unsubscribe_skips_adapter_and_fences_local_release(
        self,
    ) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        prepared = authority.prepare_unsubscribe_thread("thread-1")

        pending = authority.settle_prepared_unsubscribe_thread(
            prepared,
            subscription_already_absent=True,
        )
        adapter.unsubscribe_thread.assert_not_called()
        with self.assertRaises(ThreadResumeInProgress):
            authority.prepare_resume_thread_page("thread-1", limit=1)

        pending.abandon_local_state()
        authority.prepare_resume_thread_page("thread-1", limit=1)

    def test_backend_invalidation_keeps_unsubscribe_slot_until_worker_abandons(
        self,
    ) -> None:
        adapter = Mock()
        authority, _facts = self._make_authority(adapter)
        prepared = authority.prepare_unsubscribe_thread(
            "thread-1",
            expected_connection_generation=3,
        )

        authority.invalidate_connection()

        with self.assertRaises(ThreadResumeInProgress):
            authority.prepare_resume_thread_page("thread-1", limit=1)
        with self.assertRaises(ThreadUnsubscribeSettlementError):
            authority.execute_prepared_unsubscribe_thread(prepared)

        authority.abandon_prepared_unsubscribe_thread(prepared)
        authority.prepare_resume_thread_page("thread-1", limit=1)

    def test_connection_invalidation_discards_all_model_facts(self) -> None:
        adapter = Mock()
        authority, facts = self._make_authority(adapter)
        _record_settings(facts, "thread-1", "vision", source="thread_start")
        _record_settings(facts, "thread-2", "text", source="thread_resume")

        authority.invalidate_connection()

        self.assertIsNone(facts.resolve_model_for_request("thread-1"))
        self.assertIsNone(facts.resolve_model_for_request("thread-2"))


if __name__ == "__main__":
    unittest.main()
