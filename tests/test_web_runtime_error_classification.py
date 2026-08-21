import unittest

from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.thread_runtime_authority import (
    ThreadResumeLeaseReceipt,
    ThreadResumeLocalCommitFailed,
    ThreadResumeLocalFailurePolicy,
    ThreadResumeOutcomeUnknown,
    ThreadResumeSettlement,
    ThreadResumeSettlementError,
    ThreadResumeSettlementOutcome,
)
from bot.web_runtime.operation_service import WebOperationService


class WebRuntimeErrorClassificationTests(unittest.TestCase):
    @staticmethod
    def _receipt() -> ThreadResumeLeaseReceipt:
        return ThreadResumeLeaseReceipt(
            thread_id="thread-1",
            lease_was_newly_acquired=True,
            generation=1,
            _authority_token=object(),
            _receipt_token=object(),
        )

    def test_unknown_classifier_follows_typed_resume_cause_chain(self):
        wrapped = RuntimeError("CLI resume unavailable")
        wrapped.__cause__ = ThreadResumeOutcomeUnknown(self._receipt())

        self.assertTrue(WebOperationService.is_unknown_mutation_error(wrapped))

    def test_unknown_classifier_distinguishes_post_send_failures_from_known_rpc_errors(self):
        post_send_failures = (
            TimeoutError("response timed out"),
            CodexRpcTransportError("turn/steer", {"message": "disconnected"}),
            CodexRpcProtocolError("turn/steer", "malformed response"),
        )
        for error in post_send_failures:
            with self.subTest(error=type(error).__name__):
                self.assertTrue(WebOperationService.is_unknown_mutation_error(error))

        self.assertFalse(
            WebOperationService.is_unknown_mutation_error(
                CodexRpcError("turn/steer", {"message": "no active turn to steer"})
            )
        )

    def test_unknown_classifier_trusts_compensated_local_commit(self):
        compensated = ThreadResumeLocalCommitFailed(
            lease_receipt=self._receipt(),
            original_error=TimeoutError("local interest store timed out"),
            failure_policy=ThreadResumeLocalFailurePolicy.COMPENSATE,
            settlement=ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=ThreadResumeSettlementOutcome.COMPENSATED,
                recovery_required=False,
            ),
        )
        compensated.__cause__ = compensated.original_error

        self.assertFalse(WebOperationService.is_unknown_mutation_error(compensated))

    def test_unknown_classifier_trusts_retained_local_commit(self):
        retained = ThreadResumeLocalCommitFailed(
            lease_receipt=self._receipt(),
            original_error=RuntimeError("local interest commit failed"),
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            settlement=ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=ThreadResumeSettlementOutcome.RETAINED,
                recovery_required=True,
            ),
        )

        self.assertTrue(WebOperationService.is_unknown_mutation_error(retained))

    def test_resume_uncertainty_trusts_settled_retained_runtime_lease(self):
        settled = ThreadResumeLocalCommitFailed(
            lease_receipt=self._receipt(),
            original_error=RuntimeError("local interest commit failed"),
            failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            settlement=ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=ThreadResumeSettlementOutcome.RETAINED,
                recovery_required=False,
            ),
        )

        self.assertFalse(WebOperationService.is_resume_uncertain_error(settled))
        self.assertFalse(WebOperationService.is_resume_outcome_unknown(settled))

    def test_unknown_classifier_fails_closed_for_stale_acknowledged_resume(self):
        stale = ThreadResumeSettlementError(
            ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=(
                    ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                ),
                recovery_required=False,
            ),
            "resume receipt was replaced after upstream acknowledgement",
        )

        self.assertTrue(WebOperationService.is_unknown_mutation_error(stale))

    def test_resume_uncertainty_requires_a_typed_resume_boundary(self):
        unknown = ThreadResumeOutcomeUnknown(
            self._receipt(),
            TimeoutError("response lost"),
        )
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = unknown

        self.assertTrue(WebOperationService.is_resume_uncertain_error(unknown))
        self.assertTrue(WebOperationService.is_resume_uncertain_error(wrapped))
        self.assertTrue(WebOperationService.is_resume_outcome_unknown(unknown))
        self.assertTrue(WebOperationService.is_resume_outcome_unknown(wrapped))
        self.assertFalse(
            WebOperationService.is_resume_uncertain_error(
                TimeoutError("local pre-send timeout")
            )
        )
        self.assertFalse(
            WebOperationService.is_resume_outcome_unknown(
                TimeoutError("broad unknown")
            )
        )

    def test_acknowledged_resume_dominates_a_nested_unknown_cause(self):
        acknowledged = ThreadResumeSettlementError(
            ThreadResumeSettlement(
                thread_id="thread-1",
                generation=1,
                outcome=ThreadResumeSettlementOutcome.RETAINED,
                recovery_required=True,
            ),
            "local commit failed",
        )
        acknowledged.__cause__ = ThreadResumeOutcomeUnknown(
            self._receipt(),
            TimeoutError("older nested cause"),
        )

        self.assertTrue(WebOperationService.is_resume_uncertain_error(acknowledged))
        self.assertFalse(WebOperationService.is_resume_outcome_unknown(acknowledged))


if __name__ == "__main__":
    unittest.main()
