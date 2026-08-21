from __future__ import annotations

import unittest

from bot.feishu_resume_settlement import (
    FeishuResumeFailureReasons,
    FeishuResumeMutationProgress,
    FeishuResumeOwnerDisposition,
    FeishuResumeSettlementAction,
    FeishuResumeSettlementPorts,
    FeishuResumeSettlementService,
    FeishuResumeSuccessKind,
    SettleFeishuResumeFailure,
    SettleFeishuResumeSuccess,
)
from bot.feishu_root_operation_contract import (
    FeishuRootContinuationToken,
    FeishuRootOperationToken,
)
from bot.thread_runtime_authority import (
    ThreadResumeSettlement,
    ThreadResumeSettlementError,
    ThreadResumeSettlementOutcome,
)


_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation="acknowledged",
    outcome_unknown="unknown",
    known_failure="rejected",
    partial_mutation="partial",
    continuation_failure="continuation_failed",
)


class FeishuResumeSettlementServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.unknown_result: object = False

        def record(name: str):
            def call(_token, *, reason: str) -> None:
                self.calls.append((name, reason))

            return call

        self.service = FeishuResumeSettlementService(
            FeishuResumeSettlementPorts(
                operation_outcome_unknown=lambda _exc: self.unknown_result,
                settle_known_failure=record("known_failure"),
                settle_known_mutation=record("known_mutation"),
                settle_continuation_failure=record("continuation_failure"),
                settle_noncontinuing=record("noncontinuing"),
                acknowledge_continuing=lambda _token: self.calls.append(
                    ("continuing", "")
                ),
                mark_outcome_unknown=record("unknown"),
            )
        )
        self.admission = FeishuRootOperationToken(1, 1)
        self.continuation = FeishuRootContinuationToken(1, 2)

    @staticmethod
    def _acknowledged_error() -> Exception:
        acknowledged = ThreadResumeSettlementError(
            ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=(
                    ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                ),
                recovery_required=False,
            ),
            "local settlement failed",
        )
        acknowledged.__cause__ = TimeoutError("nested local timeout")
        wrapped = RuntimeError("surface wrapper")
        wrapped.__cause__ = acknowledged
        return wrapped

    def _failure(self, **overrides) -> SettleFeishuResumeFailure:
        values = {
            "admission": self.admission,
            "error": RuntimeError("resume failed"),
            "progress": FeishuResumeMutationProgress.ATTEMPTED,
            "reasons": _REASONS,
        }
        values.update(overrides)
        return SettleFeishuResumeFailure(**values)

    def test_acknowledged_resume_with_continuation_stays_continuing(self) -> None:
        receipt = self.service.settle_failure(
            self._failure(
                error=self._acknowledged_error(),
                continuation=self.continuation,
            )
        )

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.ACKNOWLEDGE_CONTINUING,
        )
        self.assertEqual(self.calls, [("continuing", "")])

    def test_acknowledged_resume_without_continuation_is_known_mutation(self) -> None:
        receipt = self.service.settle_failure(
            self._failure(error=self._acknowledged_error())
        )

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.SETTLE_ACKNOWLEDGED_MUTATION,
        )
        self.assertEqual(self.calls, [("known_mutation", "acknowledged")])

    def test_attempted_unknown_is_durably_marked_unknown(self) -> None:
        self.unknown_result = True

        receipt = self.service.settle_failure(self._failure())

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.MARK_OUTCOME_UNKNOWN,
        )
        self.assertEqual(self.calls, [("unknown", "unknown")])

    def test_known_rejection_settles_fresh_owner_as_failure(self) -> None:
        receipt = self.service.settle_failure(self._failure())

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.SETTLE_KNOWN_FAILURE,
        )
        self.assertEqual(self.calls, [("known_failure", "rejected")])

    def test_pre_send_failure_cannot_be_promoted_to_unknown(self) -> None:
        self.unknown_result = True

        receipt = self.service.settle_failure(
            self._failure(progress=FeishuResumeMutationProgress.NONE)
        )

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.SETTLE_KNOWN_FAILURE,
        )
        self.assertEqual(self.calls, [("known_failure", "rejected")])

    def test_partial_mutation_settles_child_before_root_owner(self) -> None:
        receipt = self.service.settle_failure(
            self._failure(
                progress=FeishuResumeMutationProgress.COMMITTED,
                known_failure_continuation=self.continuation,
            )
        )

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.SETTLE_KNOWN_MUTATION,
        )
        self.assertEqual(
            self.calls,
            [
                ("continuation_failure", "continuation_failed"),
                ("known_mutation", "partial"),
            ],
        )

    def test_owner_already_resolved_or_retained_is_never_double_settled(self) -> None:
        self.unknown_result = object()

        receipt = self.service.settle_failure(
            self._failure(
                owner_disposition=(
                    FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
                )
            )
        )

        self.assertEqual(
            receipt.action,
            FeishuResumeSettlementAction.LEAVE_UNCHANGED,
        )
        self.assertEqual(self.calls, [])

    def test_success_uses_the_same_closed_settlement_actions(self) -> None:
        expected = {
            FeishuResumeSuccessKind.KNOWN_MUTATION: (
                FeishuResumeSettlementAction.SETTLE_KNOWN_MUTATION,
                "known_mutation",
            ),
            FeishuResumeSuccessKind.CONTINUING: (
                FeishuResumeSettlementAction.ACKNOWLEDGE_CONTINUING,
                "continuing",
            ),
            FeishuResumeSuccessKind.NONCONTINUING: (
                FeishuResumeSettlementAction.SETTLE_NONCONTINUING,
                "noncontinuing",
            ),
        }
        for kind, (expected_action, expected_call) in expected.items():
            with self.subTest(kind=kind):
                self.calls.clear()
                receipt = self.service.settle_success(
                    SettleFeishuResumeSuccess(
                        admission=self.admission,
                        kind=kind,
                        reason="success",
                    )
                )
                self.assertEqual(receipt.action, expected_action)
                self.assertEqual(self.calls[0][0], expected_call)

    def test_progress_rejects_impossible_committed_without_attempt(self) -> None:
        with self.assertRaisesRegex(ValueError, "must first be attempted"):
            FeishuResumeMutationProgress.from_facts(
                mutation_attempted=False,
                mutation_succeeded=True,
            )

    def test_unknown_classifier_must_return_exact_bool(self) -> None:
        self.unknown_result = 1

        with self.assertRaisesRegex(TypeError, "exact bool"):
            self.service.settle_failure(self._failure())

        self.assertEqual(self.calls, [])

    def test_continuation_receipt_is_exactly_typed(self) -> None:
        with self.assertRaisesRegex(TypeError, "typed receipt"):
            self.service.require_continuation(None, operation="test resume")


if __name__ == "__main__":
    unittest.main()
