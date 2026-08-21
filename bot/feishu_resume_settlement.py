"""Typed root-owner settlement for Feishu resume/attach transactions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Protocol

from bot.exception_chain import iter_exception_chain
from bot.feishu_root_operation_contract import (
    FeishuRootContinuationToken,
    FeishuRootOperationToken,
)
from bot.thread_runtime_authority import ThreadResumeSettlementError


class FeishuResumeMutationProgress(StrEnum):
    """Strongest upstream mutation fact known before a failed composite flow."""

    NONE = "none"
    ATTEMPTED = "attempted"
    COMMITTED = "committed"

    @classmethod
    def from_facts(
        cls,
        *,
        mutation_attempted: bool,
        mutation_succeeded: bool,
    ) -> FeishuResumeMutationProgress:
        if type(mutation_attempted) is not bool:
            raise TypeError("mutation_attempted must be an exact bool")
        if type(mutation_succeeded) is not bool:
            raise TypeError("mutation_succeeded must be an exact bool")
        if mutation_succeeded and not mutation_attempted:
            raise ValueError("a committed mutation must first be attempted")
        if mutation_succeeded:
            return cls.COMMITTED
        if mutation_attempted:
            return cls.ATTEMPTED
        return cls.NONE


class FeishuResumeOwnerDisposition(StrEnum):
    """Whether this classifier still owns the exact admission settlement."""

    SETTLE = "settle"
    LEAVE_UNCHANGED = "leave_unchanged"


class FeishuResumeSettlementAction(StrEnum):
    """Closed root-owner action selected by the classifier."""

    LEAVE_UNCHANGED = "leave_unchanged"
    ACKNOWLEDGE_CONTINUING = "acknowledge_continuing"
    SETTLE_ACKNOWLEDGED_MUTATION = "settle_acknowledged_mutation"
    SETTLE_KNOWN_MUTATION = "settle_known_mutation"
    SETTLE_KNOWN_FAILURE = "settle_known_failure"
    MARK_OUTCOME_UNKNOWN = "mark_outcome_unknown"
    SETTLE_NONCONTINUING = "settle_noncontinuing"


class FeishuResumeSuccessKind(StrEnum):
    """Exact root-owner consequence of a successful composite operation."""

    KNOWN_MUTATION = "known_mutation"
    CONTINUING = "continuing"
    NONCONTINUING = "noncontinuing"


@dataclass(frozen=True, slots=True)
class FeishuResumeFailureReasons:
    """Stable diagnostics for each possible failure classification."""

    acknowledged_mutation: str
    outcome_unknown: str
    known_failure: str
    partial_mutation: str
    continuation_failure: str = ""

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            if name == "continuation_failure":
                continue
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must be non-empty")


RUNTIME_ATTACH_RESUME_FAILURE_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation=(
        "feishu_runtime_admin_attach_resume_acknowledged_before_local_"
        "settlement_failure"
    ),
    outcome_unknown="feishu_runtime_admin_attach_resume_outcome_unknown",
    known_failure="feishu_runtime_admin_attach_resume_rejected",
    partial_mutation="feishu_runtime_admin_attach_partially_applied",
)
RUNTIME_ATTACH_PRESTART_FAILURE_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation="feishu_runtime_admin_attach_prestart_failed",
    outcome_unknown="feishu_runtime_admin_attach_prestart_failed",
    known_failure="feishu_runtime_admin_attach_prestart_failed",
    partial_mutation="feishu_runtime_admin_attach_prestart_failed",
)
EXPLICIT_RESUME_FAILURE_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation=(
        "feishu_thread_resume_acknowledged_before_local_settlement_failure"
    ),
    outcome_unknown="feishu_thread_resume_outcome_unknown",
    known_failure="feishu_thread_resume_failed",
    partial_mutation="feishu_thread_resume_partially_applied",
)
EXPLICIT_SETTINGS_FAILURE_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation="feishu_thread_settings_acknowledged_failure",
    outcome_unknown="feishu_thread_settings_outcome_unknown",
    known_failure="feishu_thread_settings_failed",
    partial_mutation="feishu_thread_settings_partially_applied",
)
GOAL_RESUME_FAILURE_REASONS = FeishuResumeFailureReasons(
    acknowledged_mutation=(
        "feishu_goal_resume_acknowledged_before_local_settlement_failure"
    ),
    outcome_unknown="feishu_goal_resume_outcome_unknown",
    known_failure="feishu_goal_resume_failed",
    partial_mutation="feishu_goal_resume_partially_applied",
    continuation_failure="feishu_goal_resume_known_noncontinuing_failure",
)


@dataclass(frozen=True, slots=True)
class SettleFeishuResumeFailure:
    """All evidence needed to settle one failed resume/attach owner."""

    admission: FeishuRootOperationToken
    error: Exception
    progress: FeishuResumeMutationProgress
    reasons: FeishuResumeFailureReasons
    continuation: FeishuRootContinuationToken | None = None
    known_failure_continuation: FeishuRootContinuationToken | None = None
    owner_disposition: FeishuResumeOwnerDisposition = (
        FeishuResumeOwnerDisposition.SETTLE
    )

    def __post_init__(self) -> None:
        if not isinstance(self.admission, FeishuRootOperationToken):
            raise TypeError("resume settlement requires an exact admission")
        if not isinstance(self.error, Exception):
            raise TypeError("resume settlement requires an Exception")
        if not isinstance(self.progress, FeishuResumeMutationProgress):
            raise TypeError("resume settlement requires typed mutation progress")
        if not isinstance(self.reasons, FeishuResumeFailureReasons):
            raise TypeError("resume settlement requires typed reasons")
        if self.continuation is not None and not isinstance(
            self.continuation,
            FeishuRootContinuationToken,
        ):
            raise TypeError("resume settlement continuation must be exact")
        if self.known_failure_continuation is not None and not isinstance(
            self.known_failure_continuation,
            FeishuRootContinuationToken,
        ):
            raise TypeError("known-failure continuation must be exact")
        if (
            self.known_failure_continuation is not None
            and not str(self.reasons.continuation_failure or "").strip()
        ):
            raise ValueError("known-failure continuation requires an exact reason")
        if not isinstance(self.owner_disposition, FeishuResumeOwnerDisposition):
            raise TypeError("resume settlement requires typed owner disposition")


@dataclass(frozen=True, slots=True)
class SettleFeishuResumeSuccess:
    admission: FeishuRootOperationToken
    kind: FeishuResumeSuccessKind
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.admission, FeishuRootOperationToken):
            raise TypeError("resume success requires an exact admission")
        if not isinstance(self.kind, FeishuResumeSuccessKind):
            raise TypeError("resume success requires a typed kind")
        if not str(self.reason or "").strip():
            raise ValueError("resume success reason must be non-empty")


@dataclass(frozen=True, slots=True)
class FeishuResumeSettlementReceipt:
    action: FeishuResumeSettlementAction
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FeishuResumeSettlementPorts:
    operation_outcome_unknown: Callable[[Exception], bool]
    settle_known_failure: Callable[..., None]
    settle_known_mutation: Callable[..., None]
    settle_continuation_failure: Callable[..., None]
    settle_noncontinuing: Callable[..., None]
    acknowledge_continuing: Callable[[FeishuRootOperationToken], None]
    mark_outcome_unknown: Callable[..., None]


class FeishuResumeRootOperations(Protocol):
    def settle_known_failure(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None: ...

    def settle_known_mutation(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None: ...

    def settle_continuation_failure(
        self,
        token: FeishuRootContinuationToken,
        *,
        reason: str,
    ) -> None: ...

    def settle_noncontinuing(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None: ...

    def acknowledge_continuing(
        self,
        token: FeishuRootOperationToken,
    ) -> None: ...

    def mark_outcome_unknown(
        self,
        token: FeishuRootOperationToken,
        *,
        reason: str,
    ) -> None: ...


class FeishuResumeSettlementService:
    """Classify once, then settle only the supplied exact operation token."""

    def __init__(self, ports: FeishuResumeSettlementPorts) -> None:
        if type(ports) is not FeishuResumeSettlementPorts:
            raise TypeError("Feishu resume settlement requires exact typed ports")
        if any(
            not callable(getattr(ports, name))
            for name in ports.__dataclass_fields__
        ):
            raise TypeError("Feishu resume settlement ports must all be callable")
        self._ports = ports

    @classmethod
    def from_root_operations(
        cls,
        root_operations: FeishuResumeRootOperations,
        *,
        operation_outcome_unknown: Callable[[Exception], bool],
    ) -> FeishuResumeSettlementService:
        required_methods = (
            "settle_known_failure",
            "settle_known_mutation",
            "settle_continuation_failure",
            "settle_noncontinuing",
            "acknowledge_continuing",
            "mark_outcome_unknown",
        )
        if any(
            not callable(getattr(root_operations, name, None))
            for name in required_methods
        ):
            raise TypeError("Feishu resume settlement requires root operations")
        return cls(
            FeishuResumeSettlementPorts(
                operation_outcome_unknown=operation_outcome_unknown,
                settle_known_failure=root_operations.settle_known_failure,
                settle_known_mutation=root_operations.settle_known_mutation,
                settle_continuation_failure=(
                    root_operations.settle_continuation_failure
                ),
                settle_noncontinuing=root_operations.settle_noncontinuing,
                acknowledge_continuing=root_operations.acknowledge_continuing,
                mark_outcome_unknown=root_operations.mark_outcome_unknown,
            )
        )

    @staticmethod
    def resume_was_acknowledged(exc: BaseException) -> bool:
        """Read the authority-owned post-ACK fact from an exception chain."""

        return any(
            isinstance(current, ThreadResumeSettlementError)
            for current in iter_exception_chain(exc)
        )

    @staticmethod
    def require_continuation(
        value: object,
        *,
        operation: str,
    ) -> FeishuRootContinuationToken:
        if not isinstance(value, FeishuRootContinuationToken):
            raise TypeError(f"{operation} continuation 未返回 typed receipt")
        return value

    def classify_failure(
        self,
        command: SettleFeishuResumeFailure,
    ) -> FeishuResumeSettlementAction:
        if not isinstance(command, SettleFeishuResumeFailure):
            raise TypeError("resume failure classifier requires a typed command")
        if (
            command.owner_disposition
            is FeishuResumeOwnerDisposition.LEAVE_UNCHANGED
        ):
            return FeishuResumeSettlementAction.LEAVE_UNCHANGED
        if self.resume_was_acknowledged(command.error):
            if command.continuation is not None:
                return FeishuResumeSettlementAction.ACKNOWLEDGE_CONTINUING
            return FeishuResumeSettlementAction.SETTLE_ACKNOWLEDGED_MUTATION
        if command.progress is not FeishuResumeMutationProgress.NONE:
            outcome_unknown = self.operation_outcome_unknown(command.error)
            if outcome_unknown:
                return FeishuResumeSettlementAction.MARK_OUTCOME_UNKNOWN
        if command.progress is FeishuResumeMutationProgress.COMMITTED:
            return FeishuResumeSettlementAction.SETTLE_KNOWN_MUTATION
        return FeishuResumeSettlementAction.SETTLE_KNOWN_FAILURE

    def operation_outcome_unknown(self, exc: Exception) -> bool:
        """Return the existing exact upstream-outcome classification."""

        outcome_unknown = self._ports.operation_outcome_unknown(exc)
        if type(outcome_unknown) is not bool:
            raise TypeError("operation outcome classifier must return exact bool")
        return outcome_unknown

    def settle_failure(
        self,
        command: SettleFeishuResumeFailure,
    ) -> FeishuResumeSettlementReceipt:
        action = self.classify_failure(command)
        reasons = command.reasons
        reason = ""
        if action is FeishuResumeSettlementAction.LEAVE_UNCHANGED:
            return FeishuResumeSettlementReceipt(action)
        if action is FeishuResumeSettlementAction.ACKNOWLEDGE_CONTINUING:
            self._ports.acknowledge_continuing(command.admission)
        elif action in {
            FeishuResumeSettlementAction.SETTLE_ACKNOWLEDGED_MUTATION,
            FeishuResumeSettlementAction.SETTLE_KNOWN_MUTATION,
        }:
            reason = (
                reasons.acknowledged_mutation
                if action
                is FeishuResumeSettlementAction.SETTLE_ACKNOWLEDGED_MUTATION
                else reasons.partial_mutation
            )
            if command.known_failure_continuation is not None:
                self._ports.settle_continuation_failure(
                    command.known_failure_continuation,
                    reason=reasons.continuation_failure,
                )
            self._ports.settle_known_mutation(
                command.admission,
                reason=reason,
            )
        elif action is FeishuResumeSettlementAction.MARK_OUTCOME_UNKNOWN:
            reason = reasons.outcome_unknown
            self._ports.mark_outcome_unknown(
                command.admission,
                reason=reason,
            )
        elif action is FeishuResumeSettlementAction.SETTLE_KNOWN_FAILURE:
            reason = reasons.known_failure
            self._ports.settle_known_failure(
                command.admission,
                reason=reason,
            )
        else:  # pragma: no cover - closed enum exhaustiveness guard
            raise AssertionError(f"unsupported resume failure action: {action}")
        return FeishuResumeSettlementReceipt(action, reason)

    def settle_success(
        self,
        command: SettleFeishuResumeSuccess,
    ) -> FeishuResumeSettlementReceipt:
        if not isinstance(command, SettleFeishuResumeSuccess):
            raise TypeError("resume success settlement requires a typed command")
        if command.kind is FeishuResumeSuccessKind.CONTINUING:
            action = FeishuResumeSettlementAction.ACKNOWLEDGE_CONTINUING
            self._ports.acknowledge_continuing(command.admission)
        elif command.kind is FeishuResumeSuccessKind.NONCONTINUING:
            action = FeishuResumeSettlementAction.SETTLE_NONCONTINUING
            self._ports.settle_noncontinuing(
                command.admission,
                reason=command.reason,
            )
        elif command.kind is FeishuResumeSuccessKind.KNOWN_MUTATION:
            action = FeishuResumeSettlementAction.SETTLE_KNOWN_MUTATION
            self._ports.settle_known_mutation(
                command.admission,
                reason=command.reason,
            )
        else:  # pragma: no cover - closed enum exhaustiveness guard
            raise AssertionError(f"unsupported resume success kind: {command.kind}")
        return FeishuResumeSettlementReceipt(action, command.reason)
