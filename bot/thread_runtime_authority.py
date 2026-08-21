"""Typed create/resume local-commit and effective-settings boundaries.

The authority does not own a thread-wide mutation gate.  It keeps only the
exact current-process receipt between a successful ``thread/resume`` response
and the caller's immediate local commit.  Unknown transport outcomes are
reported to the caller and are never replayed or promoted into durable thread
quarantine.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Generic, Protocol, TypeVar

from bot.adapters.base import ThreadResumePage, ThreadSnapshot
from bot.codex_protocol.client import (
    CodexRpcError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.thread_create_transaction import (
    CommittedThreadCreate,
    ExternalThreadCreateAttempt,
    ThreadCreateTransaction,
)


logger = logging.getLogger(__name__)


_ResumeResponseT = TypeVar("_ResumeResponseT")
_LocalCommitT = TypeVar("_LocalCommitT")
_RESUME_FAILURE_DIAGNOSTIC_LIMIT_BYTES = 512
_RESUME_FAILURE_TRUNCATION_SUFFIX = "…"


def _bounded_resume_failure_diagnostic(value: object) -> str:
    normalized = " ".join(str(value or "").split())
    # JSON permits escaped lone surrogates even though they are not Unicode
    # scalar values. Replace them before logging so malformed upstream text
    # cannot turn an outcome-unknown request into a diagnostic encoding error.
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= _RESUME_FAILURE_DIAGNOSTIC_LIMIT_BYTES:
        return encoded.decode("utf-8")
    suffix = _RESUME_FAILURE_TRUNCATION_SUFFIX.encode("utf-8")
    retained = encoded[: _RESUME_FAILURE_DIAGNOSTIC_LIMIT_BYTES - len(suffix)]
    return f"{retained.decode('utf-8', errors='ignore')}{_RESUME_FAILURE_TRUNCATION_SUFFIX}"


def _resume_failure_diagnostic(exc: Exception) -> tuple[str, str]:
    """Return bounded operator evidence without serializing resume request data."""

    if isinstance(exc, TimeoutError):
        return "timeout", _bounded_resume_failure_diagnostic(exc)
    if isinstance(exc, CodexRpcTransportError):
        code = exc.error.get("code")
        detail = exc.error.get("message") or exc
        return "transport", _bounded_resume_failure_diagnostic(
            f"method={exc.method} code={code!r} detail={detail or '-'}"
        )
    if isinstance(exc, CodexRpcProtocolError):
        return (
            "protocol",
            _bounded_resume_failure_diagnostic(
                f"method={exc.method} detail={str(exc) or '-'}"
            ),
        )
    if isinstance(exc, CodexRpcError):
        code = exc.error.get("code")
        detail = exc.error.get("message") or exc
        return "rpc", _bounded_resume_failure_diagnostic(
            f"method={exc.method} code={code!r} detail={detail or '-'}"
        )
    return type(exc).__name__, "-"


@dataclass(frozen=True, slots=True)
class ThreadResumeLeaseReceipt:
    """Exact current-process receipt for one successful resume response."""

    thread_id: str
    lease_was_newly_acquired: bool
    generation: int
    _authority_token: object = field(repr=False, compare=False)
    _receipt_token: object = field(repr=False, compare=False)


class ThreadResumeLocalFailurePolicy(StrEnum):
    """Cleanup choice when the immediate local commit fails."""

    COMPENSATE = "compensate"
    RETAIN = "retain"


class ThreadResumeSettlementOutcome(StrEnum):
    """Typed result attached to an acknowledged resume failure."""

    COMPENSATED = "compensated"
    RETAINED = "retained"
    CLEANUP_PENDING = "cleanup_pending"
    STALE_OR_INVARIANT_VIOLATION = "stale_or_invariant_violation"


@dataclass(frozen=True, slots=True)
class ThreadResumeSettlement:
    thread_id: str
    generation: int
    outcome: ThreadResumeSettlementOutcome
    recovery_required: bool


class ThreadResumeSettlementError(RuntimeError):
    """Upstream resume was acknowledged but local settlement did not finish."""

    def __init__(
        self,
        settlement: ThreadResumeSettlement,
        message: str,
    ) -> None:
        self.thread_id = settlement.thread_id
        self.settlement = settlement
        self.recovery_required = settlement.recovery_required
        super().__init__(message)


class ThreadResumeLocalCommitFailed(ThreadResumeSettlementError):
    """Upstream resume succeeded, but its immediate local commit failed."""

    def __init__(
        self,
        *,
        lease_receipt: ThreadResumeLeaseReceipt,
        original_error: Exception,
        failure_policy: ThreadResumeLocalFailurePolicy,
        settlement: ThreadResumeSettlement,
    ) -> None:
        self.lease_receipt = lease_receipt
        self.original_error = original_error
        self.failure_policy = failure_policy
        self.upstream_resume_succeeded = True
        detail = str(original_error).strip()
        super().__init__(
            settlement,
            "thread/resume 已成功，但本地状态提交失败；"
            f"结算结果为 {settlement.outcome.value}，"
            f"recovery_required={settlement.recovery_required}。"
            + (f" 原始错误：{detail}" if detail else ""),
        )


@dataclass(frozen=True, slots=True)
class PendingThreadResume(Generic[_ResumeResponseT]):
    """Successful resume response awaiting one immediate local commit."""

    response: _ResumeResponseT
    lease_receipt: ThreadResumeLeaseReceipt
    _authority: ThreadRuntimeAuthority = field(repr=False, compare=False)

    def commit_local_state(
        self,
        local_commit: Callable[[], _LocalCommitT],
        *,
        failure_policy: ThreadResumeLocalFailurePolicy,
    ) -> _LocalCommitT:
        return self._authority._commit_pending_resume(
            self.lease_receipt,
            local_commit,
            failure_policy=failure_policy,
        )


@dataclass(frozen=True, slots=True)
class ThreadResumeClaimReceipt:
    """Exact process-local claim held while runtime lease I/O is external."""

    thread_id: str
    generation: int
    _authority_token: object = field(repr=False, compare=False)
    _receipt_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedThreadResumePage:
    """Immutable caller-held plan for one generation-pinned paged resume."""

    lease_receipt: ThreadResumeLeaseReceipt
    limit: int
    model: str | None
    model_provider: str | None
    approval_policy: str | None
    permissions_profile_id: str | None
    expected_connection_generation: int | None
    _config_overrides_json: str | None = field(repr=False, compare=False)
    _authority_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ThreadUnsubscribeReceipt:
    """Exact process-local claim for one subscription-removal transition."""

    thread_id: str
    generation: int
    _authority_token: object = field(repr=False, compare=False)
    _receipt_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedThreadUnsubscribe:
    """Immutable generation-pinned upstream unsubscribe plan."""

    receipt: ThreadUnsubscribeReceipt
    expected_connection_generation: int | None
    _authority_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PendingThreadUnsubscribe:
    """Known-absent subscription awaiting its immediate local cleanup."""

    receipt: ThreadUnsubscribeReceipt
    _authority: ThreadRuntimeAuthority = field(repr=False, compare=False)

    def commit_local_state(
        self,
        local_commit: Callable[[], _LocalCommitT],
    ) -> _LocalCommitT:
        return self._authority._commit_pending_unsubscribe(
            self.receipt,
            local_commit,
        )

    def abandon_local_state(self) -> None:
        self._authority._discard_pending_unsubscribe(self.receipt)


class ThreadResumeOutcomeUnknown(RuntimeError):
    """The exact resume request may have reached upstream."""

    def __init__(
        self,
        lease_receipt: ThreadResumeLeaseReceipt,
        original_error: Exception | None = None,
    ) -> None:
        self.thread_id = lease_receipt.thread_id
        self.lease_receipt = lease_receipt
        # The original exception remains available as ``__cause__`` when this
        # boundary is raised. Never copy its arbitrary message into the public
        # exception args: callers may render those args in Web, Feishu, or logs.
        super().__init__(
            "无法确认 thread/resume 是否已到达上游或产生结果；Focus 不会自动重试。"
            "请通过 thread list/read/resume 重新确认可见状态。"
        )


class ThreadResumePreSendGuardRejected(RuntimeError):
    """An exact caller capability was revoked before ``thread/resume`` send."""

    def __init__(
        self,
        thread_id: str,
        original_error: Exception | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.original_error = original_error
        detail = str(original_error or "").strip()
        super().__init__(
            "thread/resume 的 exact mutation capability 已失效；请求未发送。"
            + (f" 原始错误：{detail}" if detail else "")
        )


class ThreadResumeInProgress(RuntimeError):
    """A second resume was rejected before another exact receipt settled."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            "another thread/resume transaction is already in progress for this thread"
        )


class ThreadUnsubscribeInProgress(RuntimeError):
    """A subscription transition already owns this exact thread."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            "another resume/unsubscribe transition is already in progress "
            "for this thread"
        )


class ThreadStartBlockedByUnsubscribe(RuntimeError):
    """A canonical turn/start was rejected before an unsubscribe settled."""

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        super().__init__(
            "a thread/unsubscribe transition is already in progress for this "
            "thread; turn/start was not sent"
        )


class ThreadUnsubscribeSettlementError(RuntimeError):
    """A prepared unsubscribe receipt is stale or incorrectly settled."""


class ThreadUnsubscribeOutcomeUnknown(RuntimeError):
    """The exact unsubscribe may have reached upstream and is not replayable."""

    def __init__(
        self,
        receipt: ThreadUnsubscribeReceipt,
        original_error: Exception,
    ) -> None:
        self.thread_id = receipt.thread_id
        self.receipt = receipt
        self.original_error = original_error
        super().__init__(
            "无法确认 thread/unsubscribe 是否已到达上游；Focus 不会自动重试。"
        )


class ThreadRuntimeAdapter(Protocol):
    def create_thread(self, **kwargs: Any) -> ThreadSnapshot: ...

    def resume_thread(self, thread_id: str, **kwargs: Any) -> ThreadSnapshot: ...

    def resume_thread_page(self, thread_id: str, **kwargs: Any) -> ThreadResumePage: ...

    def update_thread_settings(self, thread_id: str, **kwargs: Any) -> None: ...

    def start_turn(self, **kwargs: Any) -> dict[str, Any]: ...

    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> None: ...

    def archive_thread(self, thread_id: str) -> None: ...

    def delete_thread(self, thread_id: str) -> None: ...


class ThreadRuntimeAuthority:
    """Order one adapter effect and its immediate local facts."""

    def __init__(
        self,
        *,
        adapter: ThreadRuntimeAdapter,
        effective_settings: ThreadEffectiveSettingsRegistry,
        acquire_runtime_lease: Callable[..., bool],
        release_runtime_lease: Callable[[str], None],
        resume_failure_known_no_effect: Callable[[Exception], bool],
        new_create_attempt_id: Callable[[], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._effective_settings = effective_settings
        self._acquire_runtime_lease = acquire_runtime_lease
        self._release_runtime_lease = release_runtime_lease
        self._resume_failure_known_no_effect = resume_failure_known_no_effect
        self._thread_create_transaction = ThreadCreateTransaction(
            adapter=adapter,
            effective_settings=effective_settings,
            acquire_runtime_lease=acquire_runtime_lease,
            failure_known_no_effect=resume_failure_known_no_effect,
            new_attempt_id=new_create_attempt_id,
        )
        self._receipt_authority_token = object()
        self._receipt_lock = threading.Lock()
        self._next_resume_generation_by_thread: dict[str, int] = {}
        self._resume_claim_by_thread: dict[str, object] = {}
        self._acquiring_resume_claims: set[object] = set()
        self._invalidated_resume_claims: set[object] = set()
        self._pending_resume_receipts: dict[object, ThreadResumeLeaseReceipt] = {}
        self._executing_resume_receipts: set[object] = set()
        self._next_unsubscribe_generation_by_thread: dict[str, int] = {}
        self._unsubscribe_claim_by_thread: dict[str, object] = {}
        self._pending_unsubscribe_receipts: dict[
            object,
            ThreadUnsubscribeReceipt,
        ] = {}
        self._executing_unsubscribe_receipts: set[object] = set()
        self._invalidated_unsubscribe_claims: set[object] = set()
        self._active_start_tokens_by_thread: dict[str, set[object]] = {}

    # Thread create -----------------------------------------------------

    def create_and_commit_thread(
        self,
        *,
        local_commit: Callable[[ThreadSnapshot], _LocalCommitT],
        **kwargs: Any,
    ) -> CommittedThreadCreate[ThreadSnapshot, _LocalCommitT]:
        return self._thread_create_transaction.create_and_commit_thread(
            local_commit=local_commit,
            **kwargs,
        )

    def begin_external_thread_create(self) -> ExternalThreadCreateAttempt:
        return self._thread_create_transaction.begin_external_thread_create()

    def mark_external_thread_create_outcome_unknown(
        self,
        attempt: ExternalThreadCreateAttempt,
        original_error: Exception,
    ) -> None:
        self._thread_create_transaction.mark_external_thread_create_outcome_unknown(
            attempt,
            original_error,
        )

    def commit_external_thread_create(
        self,
        attempt: ExternalThreadCreateAttempt,
        *,
        thread_id: str,
        local_commit: Callable[[], _LocalCommitT],
    ) -> CommittedThreadCreate[str, _LocalCommitT]:
        return self._thread_create_transaction.commit_external_thread_create(
            attempt,
            thread_id=thread_id,
            local_commit=local_commit,
        )

    def invalidate_external_thread_creates(self) -> None:
        self._thread_create_transaction.invalidate_connection()

    # Thread resume -----------------------------------------------------

    def begin_resume_thread(
        self,
        thread_id: str,
        *,
        model: str | None = None,
        exact_mutation_guard: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> PendingThreadResume[ThreadSnapshot]:
        receipt = self._acquire_resume_lease_receipt(thread_id)
        try:
            self._invalidate_resume_setting_intent(
                thread_id,
                model=model,
                kwargs=kwargs,
            )
        except Exception:
            self._release_new_resume_lease(
                receipt,
                reason="failed local resume preparation",
            )
            raise
        self._require_exact_pre_send_guard(
            thread_id,
            receipt,
            exact_mutation_guard,
        )
        try:
            snapshot = self._adapter.resume_thread(
                thread_id,
                model=model,
                **kwargs,
            )
        except Exception as exc:
            self._raise_failed_resume(exc, receipt=receipt)
        self._record_resume_effective_settings_or_retain(
            receipt,
            snapshot,
        )
        return PendingThreadResume(snapshot, receipt, self)

    def begin_resume_thread_page(
        self,
        thread_id: str,
        *,
        limit: int,
        model: str | None = None,
        **kwargs: Any,
    ) -> PendingThreadResume[ThreadResumePage]:
        prepared = self.prepare_resume_thread_page(
            thread_id,
            limit=limit,
            model=model,
            **kwargs,
        )
        try:
            page = self.execute_prepared_resume_thread_page(prepared)
        except Exception as exc:
            return self.settle_prepared_resume_thread_page(
                prepared,
                error=exc,
            )
        return self.settle_prepared_resume_thread_page(
            prepared,
            response=page,
        )

    def prepare_resume_thread_page(
        self,
        thread_id: str,
        *,
        limit: int,
        model: str | None = None,
        model_provider: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
        runtime_lease_preflight: object | None = None,
    ) -> PreparedThreadResumePage:
        """Acquire local admission without entering the upstream transport."""

        normalized_limit, normalized_config = self._normalize_resume_page_request(
            limit,
            config_overrides,
            expected_connection_generation,
        )
        claim = self.claim_resume_thread_page(thread_id)
        receipt = self.acquire_claimed_resume_thread_page(
            claim,
            runtime_lease_preflight=runtime_lease_preflight,
        )
        try:
            return self.complete_claimed_resume_thread_page(
                receipt,
                limit=normalized_limit,
                model=model,
                model_provider=model_provider,
                config_overrides=normalized_config,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                expected_connection_generation=expected_connection_generation,
            )
        except BaseException:
            self.abandon_acquired_resume_thread_page(receipt)
            raise

    def complete_claimed_resume_thread_page(
        self,
        receipt: ThreadResumeLeaseReceipt,
        *,
        limit: int,
        model: str | None = None,
        model_provider: str | None = None,
        config_overrides: dict[str, Any] | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        expected_connection_generation: int | None = None,
    ) -> PreparedThreadResumePage:
        """Complete in-memory settings intent for one acquired exact claim."""

        normalized_limit, normalized_config = self._normalize_resume_page_request(
            limit,
            config_overrides,
            expected_connection_generation,
        )
        normalized_config_json = (
            json.dumps(
                normalized_config,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if normalized_config is not None
            else None
        )
        self._require_pending_resume(receipt)
        thread_id = receipt.thread_id
        kwargs: dict[str, Any] = {
            "config_overrides": normalized_config,
            "model_provider": model_provider,
            "approval_policy": approval_policy,
            "permissions_profile_id": permissions_profile_id,
        }
        self._invalidate_resume_setting_intent(
            thread_id,
            model=model,
            kwargs=kwargs,
        )
        return PreparedThreadResumePage(
            lease_receipt=receipt,
            limit=normalized_limit,
            model=model or None,
            model_provider=model_provider or None,
            approval_policy=approval_policy or None,
            permissions_profile_id=permissions_profile_id or None,
            expected_connection_generation=expected_connection_generation,
            _config_overrides_json=normalized_config_json,
            _authority_token=self._receipt_authority_token,
        )

    @staticmethod
    def _normalize_resume_page_request(
        limit: object,
        config_overrides: object,
        expected_connection_generation: object,
    ) -> tuple[int, dict[str, Any] | None]:
        normalized_limit = max(int(limit), 1)
        encoded = (
            json.dumps(
                config_overrides,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if config_overrides is not None
            else None
        )
        normalized_config = json.loads(encoded) if encoded is not None else None
        if normalized_config is not None and not isinstance(normalized_config, dict):
            raise TypeError("config_overrides must encode one JSON object")
        if expected_connection_generation is not None and (
            type(expected_connection_generation) is not int
            or expected_connection_generation <= 0
        ):
            raise ValueError(
                "expected connection generation must be a positive integer"
            )
        return normalized_limit, normalized_config

    def claim_resume_thread_page(self, thread_id: str) -> ThreadResumeClaimReceipt:
        """Install only the same-thread process claim; perform no store I/O."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        with self._receipt_lock:
            if (
                normalized_thread_id in self._resume_claim_by_thread
                or normalized_thread_id in self._unsubscribe_claim_by_thread
            ):
                raise ThreadResumeInProgress(normalized_thread_id)
            claim_token = object()
            generation = self._next_resume_generation_by_thread.get(
                normalized_thread_id, 0
            ) + 1
            self._next_resume_generation_by_thread[normalized_thread_id] = generation
            self._resume_claim_by_thread[normalized_thread_id] = claim_token
            self._acquiring_resume_claims.add(claim_token)
        return ThreadResumeClaimReceipt(
            thread_id=normalized_thread_id,
            generation=generation,
            _authority_token=self._receipt_authority_token,
            _receipt_token=claim_token,
        )

    def acquire_claimed_resume_thread_page(
        self,
        claim: ThreadResumeClaimReceipt,
        *,
        runtime_lease_preflight: object | None = None,
    ) -> ThreadResumeLeaseReceipt:
        """Acquire the machine lease for one exact claim on an external worker."""

        self._validate_resume_claim_receipt(claim)
        with self._receipt_lock:
            claim_current = (
                self._resume_claim_by_thread.get(claim.thread_id)
                is claim._receipt_token
                and claim._receipt_token in self._acquiring_resume_claims
                and claim._receipt_token not in self._invalidated_resume_claims
            )
        if not claim_current:
            self._discard_resume_claim(claim.thread_id, claim._receipt_token)
            raise ThreadResumePreSendGuardRejected(claim.thread_id)
        try:
            kwargs = (
                {}
                if runtime_lease_preflight is None
                else {"runtime_lease_preflight": runtime_lease_preflight}
            )
            lease_was_newly_acquired = self._acquire_runtime_lease(
                claim.thread_id,
                **kwargs,
            )
        except BaseException:
            self._discard_resume_claim(claim.thread_id, claim._receipt_token)
            raise
        with self._receipt_lock:
            self._acquiring_resume_claims.discard(claim._receipt_token)
            claim_invalidated = (
                self._resume_claim_by_thread.get(claim.thread_id)
                is not claim._receipt_token
                or claim._receipt_token in self._invalidated_resume_claims
            )
            if not claim_invalidated:
                receipt = ThreadResumeLeaseReceipt(
                    thread_id=claim.thread_id,
                    lease_was_newly_acquired=lease_was_newly_acquired,
                    generation=claim.generation,
                    _authority_token=self._receipt_authority_token,
                    _receipt_token=claim._receipt_token,
                )
                self._pending_resume_receipts[claim._receipt_token] = receipt
        if not claim_invalidated:
            return receipt
        try:
            if lease_was_newly_acquired:
                self._release_runtime_lease(claim.thread_id)
        finally:
            self._discard_resume_claim(claim.thread_id, claim._receipt_token)
        raise ThreadResumePreSendGuardRejected(claim.thread_id)

    def abandon_resume_thread_page_claim(
        self,
        claim: ThreadResumeClaimReceipt,
    ) -> None:
        """Retire only an unacquired exact claim."""

        self._validate_resume_claim_receipt(claim)
        with self._receipt_lock:
            if (
                self._resume_claim_by_thread.get(claim.thread_id)
                is not claim._receipt_token
                or claim._receipt_token not in self._acquiring_resume_claims
            ):
                return
            self._acquiring_resume_claims.discard(claim._receipt_token)
            self._invalidated_resume_claims.discard(claim._receipt_token)
            self._resume_claim_by_thread.pop(claim.thread_id, None)

    def abandon_acquired_resume_thread_page(
        self,
        receipt: ThreadResumeLeaseReceipt,
    ) -> None:
        """Release only this exact pre-send claim and its newly acquired lease."""

        self._validate_resume_lease_receipt(receipt)
        self._release_new_resume_lease(
            receipt,
            reason="abandoned prepared paged resume",
        )

    def execute_prepared_resume_thread_page(
        self,
        prepared: PreparedThreadResumePage,
    ) -> ThreadResumePage:
        """Perform only the upstream resume effect on the caller's thread."""

        self._validate_prepared_resume_page(prepared)
        self._claim_prepared_resume_execution(prepared.lease_receipt)
        kwargs: dict[str, Any] = {
            "limit": prepared.limit,
            "model": prepared.model,
            "model_provider": prepared.model_provider,
            "config_overrides": (
                json.loads(prepared._config_overrides_json)
                if prepared._config_overrides_json is not None
                else None
            ),
            "approval_policy": prepared.approval_policy,
            "permissions_profile_id": prepared.permissions_profile_id,
        }
        if prepared.expected_connection_generation is not None:
            kwargs["expected_connection_generation"] = (
                prepared.expected_connection_generation
            )
        return self._adapter.resume_thread_page(
            prepared.lease_receipt.thread_id,
            **kwargs,
        )

    def settle_prepared_resume_thread_page(
        self,
        prepared: PreparedThreadResumePage,
        *,
        response: ThreadResumePage | None = None,
        error: Exception | None = None,
    ) -> PendingThreadResume[ThreadResumePage]:
        """Classify and settle one exact external resume result."""

        self._validate_prepared_resume_page(prepared)
        if (response is None) == (error is None):
            raise ValueError(
                "exactly one prepared resume response or error is required"
            )
        receipt = prepared.lease_receipt
        self._require_pending_resume(receipt)
        self._require_claimed_resume_execution(receipt)
        if error is not None:
            self._raise_failed_resume(error, receipt=receipt)
            raise AssertionError("resume failure settlement must raise")
        if not isinstance(response, ThreadResumePage):
            self._raise_failed_resume(
                TypeError("thread/resume returned an invalid paged response"),
                receipt=receipt,
            )
            raise AssertionError("invalid resume response settlement must raise")
        self._record_resume_effective_settings_or_retain(
            receipt,
            response.snapshot,
        )
        return PendingThreadResume(response, receipt, self)

    def _commit_pending_resume(
        self,
        receipt: ThreadResumeLeaseReceipt,
        local_commit: Callable[[], _LocalCommitT],
        *,
        failure_policy: ThreadResumeLocalFailurePolicy,
    ) -> _LocalCommitT:
        if not callable(local_commit):
            raise TypeError("local_commit 必须可调用。")
        if not isinstance(failure_policy, ThreadResumeLocalFailurePolicy):
            raise TypeError(
                "failure_policy 必须显式使用 ThreadResumeLocalFailurePolicy。"
            )
        self._consume_pending_resume(receipt)
        try:
            return local_commit()
        except Exception as exc:
            settlement = self._settle_failed_local_commit(
                receipt,
                failure_policy=failure_policy,
            )
            raise ThreadResumeLocalCommitFailed(
                lease_receipt=receipt,
                original_error=exc,
                failure_policy=failure_policy,
                settlement=settlement,
            ) from exc

    def _settle_failed_local_commit(
        self,
        receipt: ThreadResumeLeaseReceipt,
        *,
        failure_policy: ThreadResumeLocalFailurePolicy,
    ) -> ThreadResumeSettlement:
        if failure_policy is ThreadResumeLocalFailurePolicy.RETAIN:
            return self._resume_settlement(
                receipt,
                outcome=ThreadResumeSettlementOutcome.RETAINED,
                recovery_required=True,
            )
        if not receipt.lease_was_newly_acquired:
            return self._resume_settlement(
                receipt,
                outcome=ThreadResumeSettlementOutcome.RETAINED,
                recovery_required=False,
            )
        cleanup_steps = (
            (
                "unsubscribe",
                lambda: self._adapter.unsubscribe_thread(receipt.thread_id),
            ),
            (
                "effective-settings clear",
                lambda: self._effective_settings.clear_thread(receipt.thread_id),
            ),
            (
                "runtime-lease release",
                lambda: self._release_runtime_lease(receipt.thread_id),
            ),
        )
        for step, cleanup in cleanup_steps:
            try:
                cleanup()
            except Exception:
                logger.exception(
                    "resume local-commit compensation failed during %s: thread=%s",
                    step,
                    receipt.thread_id[:12],
                )
                return self._resume_settlement(
                    receipt,
                    outcome=ThreadResumeSettlementOutcome.CLEANUP_PENDING,
                    recovery_required=True,
                )
        return self._resume_settlement(
            receipt,
            outcome=ThreadResumeSettlementOutcome.COMPENSATED,
            recovery_required=False,
        )

    def _record_resume_effective_settings_or_retain(
        self,
        receipt: ThreadResumeLeaseReceipt,
        snapshot: ThreadSnapshot,
    ) -> None:
        try:
            self._effective_settings.record_start_or_resume(
                receipt.thread_id,
                model=snapshot.effective_model,
                reasoning_effort=snapshot.effective_reasoning_effort,
                approval_policy=snapshot.effective_approval_policy,
                permissions_profile_id=snapshot.effective_permissions_profile_id,
                source="thread_resume",
            )
        except Exception as exc:
            self._consume_pending_resume(receipt)
            settlement = self._settle_failed_local_commit(
                receipt,
                failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
            )
            raise ThreadResumeLocalCommitFailed(
                lease_receipt=receipt,
                original_error=exc,
                failure_policy=ThreadResumeLocalFailurePolicy.RETAIN,
                settlement=settlement,
            ) from exc

    def _require_exact_pre_send_guard(
        self,
        thread_id: str,
        receipt: ThreadResumeLeaseReceipt,
        guard: Callable[[], bool] | None,
    ) -> None:
        try:
            allowed = guard is None or bool(guard())
        except Exception as exc:
            self._release_new_resume_lease(
                receipt,
                reason="rejected exact pre-send resume guard",
            )
            raise ThreadResumePreSendGuardRejected(thread_id, exc) from exc
        if not allowed:
            self._release_new_resume_lease(
                receipt,
                reason="revoked exact pre-send resume guard",
            )
            raise ThreadResumePreSendGuardRejected(thread_id)

    def _acquire_resume_lease_receipt(
        self,
        thread_id: str,
        *,
        runtime_lease_preflight: object | None = None,
    ) -> ThreadResumeLeaseReceipt:
        claim = self.claim_resume_thread_page(thread_id)
        return self.acquire_claimed_resume_thread_page(
            claim,
            runtime_lease_preflight=runtime_lease_preflight,
        )

    def _consume_pending_resume(self, receipt: ThreadResumeLeaseReceipt) -> None:
        self._validate_resume_lease_receipt(receipt)
        with self._receipt_lock:
            pending = self._pending_resume_receipts.pop(
                receipt._receipt_token,
                None,
            )
            if pending is receipt:
                self._executing_resume_receipts.discard(receipt._receipt_token)
                current_claim = self._resume_claim_by_thread.get(receipt.thread_id)
                if current_claim is receipt._receipt_token:
                    self._resume_claim_by_thread.pop(receipt.thread_id, None)
        if pending is not receipt:
            raise ThreadResumeSettlementError(
                self._resume_settlement(
                    receipt,
                    outcome=(
                        ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                    ),
                    recovery_required=False,
                ),
                "pending resume receipt 已结算、失效或属于旧 backend generation。",
            )

    def _discard_pending_resume(self, receipt: ThreadResumeLeaseReceipt) -> None:
        with self._receipt_lock:
            current = self._pending_resume_receipts.get(receipt._receipt_token)
            if current is receipt:
                self._pending_resume_receipts.pop(receipt._receipt_token, None)
                self._executing_resume_receipts.discard(receipt._receipt_token)
                current_claim = self._resume_claim_by_thread.get(receipt.thread_id)
                if current_claim is receipt._receipt_token:
                    self._resume_claim_by_thread.pop(receipt.thread_id, None)

    def _discard_resume_claim(self, thread_id: str, claim_token: object) -> None:
        with self._receipt_lock:
            self._acquiring_resume_claims.discard(claim_token)
            self._invalidated_resume_claims.discard(claim_token)
            self._executing_resume_receipts.discard(claim_token)
            if self._resume_claim_by_thread.get(thread_id) is claim_token:
                self._resume_claim_by_thread.pop(thread_id, None)

    def _claim_prepared_resume_execution(
        self,
        receipt: ThreadResumeLeaseReceipt,
    ) -> None:
        self._validate_resume_lease_receipt(receipt)
        with self._receipt_lock:
            current = self._pending_resume_receipts.get(receipt._receipt_token)
            if current is receipt and receipt._receipt_token not in (
                self._executing_resume_receipts
            ):
                self._executing_resume_receipts.add(receipt._receipt_token)
                return
            already_claimed = current is receipt
        if already_claimed:
            raise ThreadResumeInProgress(receipt.thread_id)
        raise ThreadResumeSettlementError(
            self._resume_settlement(
                receipt,
                outcome=ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION,
                recovery_required=False,
            ),
            "prepared resume receipt is stale or already settled",
        )

    def _require_claimed_resume_execution(
        self,
        receipt: ThreadResumeLeaseReceipt,
    ) -> None:
        with self._receipt_lock:
            claimed = receipt._receipt_token in self._executing_resume_receipts
        if not claimed:
            raise ThreadResumeSettlementError(
                self._resume_settlement(
                    receipt,
                    outcome=(
                        ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                    ),
                    recovery_required=False,
                ),
                "prepared resume effect was not claimed before settlement",
            )

    def _require_pending_resume(self, receipt: ThreadResumeLeaseReceipt) -> None:
        self._validate_resume_lease_receipt(receipt)
        with self._receipt_lock:
            current = self._pending_resume_receipts.get(receipt._receipt_token)
        if current is not receipt:
            raise ThreadResumeSettlementError(
                self._resume_settlement(
                    receipt,
                    outcome=(
                        ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                    ),
                    recovery_required=False,
                ),
                "prepared resume receipt is stale or already settled",
            )

    def _validate_prepared_resume_page(
        self,
        prepared: PreparedThreadResumePage,
    ) -> None:
        if not isinstance(prepared, PreparedThreadResumePage):
            raise TypeError("prepared paged resume receipt is required")
        if prepared._authority_token is not self._receipt_authority_token:
            raise ValueError("prepared paged resume belongs to another authority")
        self._validate_resume_lease_receipt(prepared.lease_receipt)

    def _raise_failed_resume(
        self,
        exc: Exception,
        *,
        receipt: ThreadResumeLeaseReceipt,
    ) -> None:
        if self._failure_is_known_no_effect(exc):
            self._release_new_resume_lease(
                receipt,
                reason="known no-effect resume failure",
            )
            raise exc
        self._discard_pending_resume(receipt)
        failure_kind, failure_detail = _resume_failure_diagnostic(exc)
        logger.warning(
            "thread/resume may have reached upstream; retaining runtime lease: "
            "thread=%s failure_kind=%s failure=%s",
            receipt.thread_id[:12],
            failure_kind,
            failure_detail,
        )
        raise ThreadResumeOutcomeUnknown(receipt, exc) from exc

    def _release_new_resume_lease(
        self,
        receipt: ThreadResumeLeaseReceipt,
        *,
        reason: str,
    ) -> None:
        with self._receipt_lock:
            receipt_is_current = (
                self._pending_resume_receipts.get(receipt._receipt_token) is receipt
                and self._resume_claim_by_thread.get(receipt.thread_id)
                is receipt._receipt_token
            )
        if not receipt_is_current:
            # Connection invalidation may already have admitted a successor
            # using the same thread-scoped holder. An old attempt must never
            # release that successor's lease by thread id.
            return
        try:
            if receipt.lease_was_newly_acquired:
                self._release_runtime_lease(receipt.thread_id)
        except Exception:
            logger.exception(
                "failed to release runtime lease after %s: thread=%s",
                reason,
                receipt.thread_id[:12],
            )
        finally:
            # Do not expose a same-thread successor until any thread-id-based
            # cleanup from this attempt has completed.
            self._discard_pending_resume(receipt)

    def _validate_resume_lease_receipt(
        self,
        receipt: ThreadResumeLeaseReceipt,
    ) -> None:
        if not isinstance(receipt, ThreadResumeLeaseReceipt):
            raise TypeError("需要 ThreadResumeLeaseReceipt。")
        if receipt._authority_token is not self._receipt_authority_token:
            raise ValueError("resume lease receipt 不属于当前 runtime authority。")
        if not receipt.thread_id:
            raise ValueError("resume lease receipt 缺少 thread_id。")

    def _validate_resume_claim_receipt(
        self,
        claim: ThreadResumeClaimReceipt,
    ) -> None:
        if not isinstance(claim, ThreadResumeClaimReceipt):
            raise TypeError("需要 ThreadResumeClaimReceipt。")
        if claim._authority_token is not self._receipt_authority_token:
            raise ValueError("resume claim receipt 不属于当前 runtime authority。")
        if not claim.thread_id:
            raise ValueError("resume claim receipt 缺少 thread_id。")

    @staticmethod
    def _resume_settlement(
        receipt: ThreadResumeLeaseReceipt,
        *,
        outcome: ThreadResumeSettlementOutcome,
        recovery_required: bool,
    ) -> ThreadResumeSettlement:
        return ThreadResumeSettlement(
            thread_id=receipt.thread_id,
            generation=receipt.generation,
            outcome=outcome,
            recovery_required=recovery_required,
        )

    # Thread unsubscribe ------------------------------------------------

    def prepare_unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> PreparedThreadUnsubscribe:
        """Claim one subscription transition without entering transport."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        if (
            expected_connection_generation is not None
            and (
                type(expected_connection_generation) is not int
                or expected_connection_generation <= 0
            )
        ):
            raise ValueError(
                "expected connection generation must be a positive integer"
            )
        with self._receipt_lock:
            if (
                normalized_thread_id in self._resume_claim_by_thread
                or normalized_thread_id in self._unsubscribe_claim_by_thread
                or self._active_start_tokens_by_thread.get(normalized_thread_id)
            ):
                raise ThreadUnsubscribeInProgress(normalized_thread_id)
            claim_token = object()
            generation = self._next_unsubscribe_generation_by_thread.get(
                normalized_thread_id,
                0,
            ) + 1
            self._next_unsubscribe_generation_by_thread[normalized_thread_id] = (
                generation
            )
            receipt = ThreadUnsubscribeReceipt(
                thread_id=normalized_thread_id,
                generation=generation,
                _authority_token=self._receipt_authority_token,
                _receipt_token=claim_token,
            )
            self._unsubscribe_claim_by_thread[normalized_thread_id] = claim_token
            self._pending_unsubscribe_receipts[claim_token] = receipt
        return PreparedThreadUnsubscribe(
            receipt=receipt,
            expected_connection_generation=expected_connection_generation,
            _authority_token=self._receipt_authority_token,
        )

    def execute_prepared_unsubscribe_thread(
        self,
        prepared: PreparedThreadUnsubscribe,
    ) -> None:
        """Perform only one exact upstream unsubscribe effect."""

        self._validate_prepared_unsubscribe(prepared)
        self._claim_prepared_unsubscribe_execution(prepared.receipt)
        kwargs: dict[str, Any] = {}
        if prepared.expected_connection_generation is not None:
            kwargs["expected_connection_generation"] = (
                prepared.expected_connection_generation
            )
        self._adapter.unsubscribe_thread(prepared.receipt.thread_id, **kwargs)

    def settle_prepared_unsubscribe_thread(
        self,
        prepared: PreparedThreadUnsubscribe,
        *,
        upstream_succeeded: bool = False,
        subscription_already_absent: bool = False,
        error: Exception | None = None,
    ) -> PendingThreadUnsubscribe:
        """Classify an exact effect and retain the claim through local cleanup."""

        self._validate_prepared_unsubscribe(prepared)
        receipt = prepared.receipt
        try:
            outcomes = sum(
                (
                    type(upstream_succeeded) is bool and upstream_succeeded,
                    type(subscription_already_absent) is bool
                    and subscription_already_absent,
                    error is not None,
                )
            )
            if type(upstream_succeeded) is not bool or type(
                subscription_already_absent
            ) is not bool:
                raise TypeError("unsubscribe settlement flags must be exact bools")
            if outcomes != 1:
                raise ValueError(
                    "exactly one unsubscribe settlement outcome is required"
                )
            self._require_pending_unsubscribe(receipt)
            if subscription_already_absent:
                with self._receipt_lock:
                    was_executed = (
                        receipt._receipt_token
                        in self._executing_unsubscribe_receipts
                    )
                if was_executed:
                    raise ThreadUnsubscribeSettlementError(
                        "an executed unsubscribe cannot settle as already absent"
                    )
            else:
                self._require_claimed_unsubscribe_execution(receipt)
            if error is not None:
                if self._failure_is_known_no_effect(error):
                    raise error
                failure_kind, failure_detail = _resume_failure_diagnostic(error)
                logger.warning(
                    "thread/unsubscribe may have reached upstream; not retrying: "
                    "thread=%s failure_kind=%s failure=%s",
                    receipt.thread_id[:12],
                    failure_kind,
                    failure_detail,
                )
                raise ThreadUnsubscribeOutcomeUnknown(receipt, error) from error
            self._effective_settings.clear_thread(receipt.thread_id)
        except BaseException:
            # Settlement either yields one PendingThreadUnsubscribe or retires
            # its own exact token. Identity checks in _discard prevent a stale
            # receipt from touching a same-thread successor.
            self._discard_pending_unsubscribe(receipt)
            raise
        return PendingThreadUnsubscribe(receipt, self)

    def abandon_prepared_unsubscribe_thread(
        self,
        prepared: PreparedThreadUnsubscribe,
    ) -> None:
        """Retire one invalidated/no-longer-needed prepared claim without effect."""

        self._validate_prepared_unsubscribe(prepared)
        self._discard_pending_unsubscribe(prepared.receipt)

    def _commit_pending_unsubscribe(
        self,
        receipt: ThreadUnsubscribeReceipt,
        local_commit: Callable[[], _LocalCommitT],
    ) -> _LocalCommitT:
        if not callable(local_commit):
            raise TypeError("local unsubscribe commit must be callable")
        self._validate_unsubscribe_receipt(receipt)
        try:
            self._require_pending_unsubscribe(receipt)
            return local_commit()
        finally:
            self._discard_pending_unsubscribe(receipt)

    def _claim_prepared_unsubscribe_execution(
        self,
        receipt: ThreadUnsubscribeReceipt,
    ) -> None:
        self._validate_unsubscribe_receipt(receipt)
        with self._receipt_lock:
            current = self._pending_unsubscribe_receipts.get(receipt._receipt_token)
            if (
                current is receipt
                and receipt._receipt_token not in self._invalidated_unsubscribe_claims
                and receipt._receipt_token
                not in self._executing_unsubscribe_receipts
            ):
                self._executing_unsubscribe_receipts.add(receipt._receipt_token)
                return
            already_claimed = (
                current is receipt
                and receipt._receipt_token in self._executing_unsubscribe_receipts
            )
        if already_claimed:
            raise ThreadUnsubscribeInProgress(receipt.thread_id)
        raise ThreadUnsubscribeSettlementError(
            "prepared unsubscribe receipt is stale or invalidated"
        )

    def _require_claimed_unsubscribe_execution(
        self,
        receipt: ThreadUnsubscribeReceipt,
    ) -> None:
        with self._receipt_lock:
            claimed = (
                receipt._receipt_token in self._executing_unsubscribe_receipts
            )
        if not claimed:
            raise ThreadUnsubscribeSettlementError(
                "prepared unsubscribe effect was not claimed before settlement"
            )

    def _require_pending_unsubscribe(
        self,
        receipt: ThreadUnsubscribeReceipt,
    ) -> None:
        self._validate_unsubscribe_receipt(receipt)
        with self._receipt_lock:
            current = self._pending_unsubscribe_receipts.get(receipt._receipt_token)
            claim_is_current = (
                self._unsubscribe_claim_by_thread.get(receipt.thread_id)
                is receipt._receipt_token
            )
            invalidated = (
                receipt._receipt_token in self._invalidated_unsubscribe_claims
            )
        if current is not receipt or not claim_is_current or invalidated:
            raise ThreadUnsubscribeSettlementError(
                "prepared unsubscribe receipt is stale or already settled"
            )

    def _discard_pending_unsubscribe(
        self,
        receipt: ThreadUnsubscribeReceipt,
    ) -> None:
        self._validate_unsubscribe_receipt(receipt)
        with self._receipt_lock:
            current = self._pending_unsubscribe_receipts.get(receipt._receipt_token)
            if current is receipt:
                self._pending_unsubscribe_receipts.pop(receipt._receipt_token, None)
            self._executing_unsubscribe_receipts.discard(receipt._receipt_token)
            self._invalidated_unsubscribe_claims.discard(receipt._receipt_token)
            if (
                self._unsubscribe_claim_by_thread.get(receipt.thread_id)
                is receipt._receipt_token
            ):
                self._unsubscribe_claim_by_thread.pop(receipt.thread_id, None)

    def _validate_prepared_unsubscribe(
        self,
        prepared: PreparedThreadUnsubscribe,
    ) -> None:
        if not isinstance(prepared, PreparedThreadUnsubscribe):
            raise TypeError("prepared unsubscribe receipt is required")
        if prepared._authority_token is not self._receipt_authority_token:
            raise ValueError("prepared unsubscribe belongs to another authority")
        self._validate_unsubscribe_receipt(prepared.receipt)

    def _validate_unsubscribe_receipt(
        self,
        receipt: ThreadUnsubscribeReceipt,
    ) -> None:
        if not isinstance(receipt, ThreadUnsubscribeReceipt):
            raise TypeError("thread unsubscribe receipt is required")
        if receipt._authority_token is not self._receipt_authority_token:
            raise ValueError("unsubscribe receipt belongs to another authority")
        if not receipt.thread_id:
            raise ValueError("unsubscribe receipt is missing thread_id")

    # Other adapter effects --------------------------------------------

    def update_thread_settings(
        self,
        thread_id: str,
        *,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._effective_settings.invalidate_thread_base_if_requested_settings_differ(
            thread_id,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        self._adapter.update_thread_settings(
            thread_id,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )

    def start_turn(
        self,
        *,
        thread_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        approval_policy: str | None = None,
        permissions_profile_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        normalized_thread_id, start_token = self._claim_direct_start(thread_id)
        try:
            self._effective_settings.invalidate_requested_settings_if_different(
                normalized_thread_id,
                model=model,
                reasoning_effort=reasoning_effort,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
            )
            return self._adapter.start_turn(
                thread_id=normalized_thread_id,
                model=model,
                reasoning_effort=reasoning_effort,
                approval_policy=approval_policy,
                permissions_profile_id=permissions_profile_id,
                **kwargs,
            )
        finally:
            self._release_direct_start(normalized_thread_id, start_token)

    def _claim_direct_start(self, thread_id: str) -> tuple[str, object]:
        """Fence canonical start only against same-thread unsubscribe cleanup."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread_id 不能为空。")
        with self._receipt_lock:
            if normalized_thread_id in self._unsubscribe_claim_by_thread:
                raise ThreadStartBlockedByUnsubscribe(normalized_thread_id)
            start_token = object()
            self._active_start_tokens_by_thread.setdefault(
                normalized_thread_id,
                set(),
            ).add(start_token)
        return normalized_thread_id, start_token

    def _release_direct_start(self, thread_id: str, start_token: object) -> None:
        with self._receipt_lock:
            active_tokens = self._active_start_tokens_by_thread.get(thread_id)
            if active_tokens is None:
                return
            active_tokens.discard(start_token)
            if not active_tokens:
                self._active_start_tokens_by_thread.pop(thread_id, None)

    def _invalidate_resume_setting_intent(
        self,
        thread_id: str,
        *,
        model: str | None,
        kwargs: dict[str, Any],
    ) -> None:
        config_overrides = kwargs.get("config_overrides")
        reasoning_effort = (
            config_overrides.get("model_reasoning_effort")
            if isinstance(config_overrides, dict)
            else None
        )
        self._effective_settings.invalidate_requested_settings_if_different(
            thread_id,
            model=model,
            reasoning_effort=reasoning_effort,
            approval_policy=kwargs.get("approval_policy"),
            permissions_profile_id=kwargs.get("permissions_profile_id"),
        )

    def observe_notification(self, method: object, params: object) -> None:
        self._effective_settings.observe_notification(method, params)

    def invalidate_connection(self) -> None:
        """Invalidate connection-local facts and exact pending receipts."""

        try:
            self._effective_settings.clear_all()
        finally:
            self._thread_create_transaction.invalidate_connection()
            with self._receipt_lock:
                self._invalidate_resume_claims_locked()
                self._invalidate_unsubscribe_claims_locked()
                self._pending_resume_receipts.clear()
                self._executing_resume_receipts.clear()

    def confirm_backend_reset(self) -> None:
        try:
            self._effective_settings.clear_all()
        finally:
            self._thread_create_transaction.invalidate_connection()
            with self._receipt_lock:
                self._invalidate_resume_claims_locked()
                self._invalidate_unsubscribe_claims_locked()
                self._pending_resume_receipts.clear()
                self._executing_resume_receipts.clear()

    def _invalidate_resume_claims_locked(self) -> None:
        """Revoke receipts while retaining in-flight acquire cleanup barriers."""

        self._invalidated_resume_claims.update(self._acquiring_resume_claims)
        for thread_id, claim_token in tuple(self._resume_claim_by_thread.items()):
            if claim_token not in self._acquiring_resume_claims:
                self._resume_claim_by_thread.pop(thread_id, None)

    def _invalidate_unsubscribe_claims_locked(self) -> None:
        """Revoke effects but retain their slot until the worker retires it."""

        self._invalidated_unsubscribe_claims.update(
            self._unsubscribe_claim_by_thread.values()
        )

    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        prepared = self.prepare_unsubscribe_thread(
            thread_id,
            expected_connection_generation=expected_connection_generation,
        )
        try:
            try:
                self.execute_prepared_unsubscribe_thread(prepared)
            except Exception as exc:
                self.settle_prepared_unsubscribe_thread(prepared, error=exc)
                raise AssertionError("unsubscribe failure settlement must raise")
            pending = self.settle_prepared_unsubscribe_thread(
                prepared,
                upstream_succeeded=True,
            )
            pending.commit_local_state(lambda: None)
        finally:
            # Idempotent exact-token retirement also covers invalidation between
            # a successful adapter response and local settlement, plus BaseException.
            self.abandon_prepared_unsubscribe_thread(prepared)

    def archive_thread(self, thread_id: str) -> None:
        self._adapter.archive_thread(thread_id)
        self._effective_settings.clear_thread(thread_id)

    def delete_thread(self, thread_id: str) -> None:
        self._adapter.delete_thread(thread_id)
        self._effective_settings.clear_thread(thread_id)

    def _failure_is_known_no_effect(self, exc: Exception) -> bool:
        try:
            return bool(self._resume_failure_known_no_effect(exc))
        except Exception:
            logger.exception(
                "resume no-effect classifier failed; retaining runtime lease"
            )
            return False
