"""Minimal local coordination around typed ``thread/start`` responses.

Codex owns thread creation.  Focus adds only the local consequences of a
successful response: a machine runtime lease, the effective-settings fact, and
one surface-specific commit callback.  An unknown transport result is
reported to the initiating surface and is never retried automatically; it
does not create a durable recovery transaction or quarantine other threads.

fcodex transports ``thread/start`` itself, so this module also issues a
process-local, backend-generation capability which can consume one response.
The capability prevents a late response from settling a replacement request,
but intentionally has no cross-restart meaning.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Protocol, TypeVar

from bot.adapters.base import ThreadSnapshot, ThreadSummary
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry


_LocalCommitT = TypeVar("_LocalCommitT")
_CreateResponseT = TypeVar("_CreateResponseT")


class ThreadCreateAdapter(Protocol):
    def create_thread(self, **kwargs: Any) -> ThreadSnapshot: ...


@dataclass(frozen=True, slots=True)
class ExternalThreadCreateAttempt:
    """One process-local capability for an fcodex ``thread/start`` request."""

    attempt_id: str
    _generation: int = field(repr=False, compare=False)
    _authority_token: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CommittedThreadCreate(Generic[_CreateResponseT, _LocalCommitT]):
    """A typed create response and the corresponding local commit result."""

    response: _CreateResponseT
    local_result: _LocalCommitT


class ThreadCreateOutcomeUnknown(RuntimeError):
    """The exact create result is unknown and must not be auto-retried."""

    def __init__(self, attempt_id: str, original_error: Exception) -> None:
        self.attempt_id = str(attempt_id or "").strip()
        self.original_error = original_error
        detail = str(original_error).strip()
        super().__init__(
            "无法确认 thread/start 是否已创建 thread；不会自动重试。"
            "可刷新全局 thread 列表查找可能已创建的 thread。"
            + (f" 原始错误：{detail}" if detail else "")
        )


class ThreadCreateSettlementError(RuntimeError):
    """A process-local external create capability is stale or already used."""

    def __init__(
        self,
        *,
        attempt_id: str,
        message: str,
        thread_id: str = "",
    ) -> None:
        self.attempt_id = str(attempt_id or "").strip()
        self.thread_id = str(thread_id or "").strip()
        super().__init__(message)


class ThreadCreateLocalCommitFailed(ThreadCreateSettlementError):
    """Upstream returned a thread, but a local consequence failed."""

    def __init__(
        self,
        *,
        attempt_id: str,
        thread_id: str,
        stage: str,
        original_error: Exception,
    ) -> None:
        self.stage = str(stage or "local_commit").strip() or "local_commit"
        self.original_error = original_error
        self.upstream_create_succeeded = True
        detail = str(original_error).strip()
        super().__init__(
            attempt_id=attempt_id,
            thread_id=thread_id,
            message=(
                "thread/start 已成功，但 Focus 未完成本地提交；"
                f"stage={self.stage}。该 thread 可从全局列表重新打开，"
                "其他 thread 不受影响。"
                + (f" 原始错误：{detail}" if detail else "")
            ),
        )


class ThreadCreateTransaction:
    """Apply only Focus's local consequences of a typed create response."""

    def __init__(
        self,
        *,
        adapter: ThreadCreateAdapter,
        effective_settings: ThreadEffectiveSettingsRegistry,
        acquire_runtime_lease: Callable[[str], bool],
        failure_known_no_effect: Callable[[Exception], bool],
        new_attempt_id: Callable[[], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._effective_settings = effective_settings
        self._acquire_runtime_lease = acquire_runtime_lease
        self._failure_known_no_effect = failure_known_no_effect
        self._new_attempt_id = new_attempt_id or (lambda: uuid.uuid4().hex)
        self._external_authority_token = object()
        self._external_generation = 0
        self._external_attempts: dict[str, ExternalThreadCreateAttempt] = {}
        self._external_lock = threading.Lock()

    def create_and_commit_thread(
        self,
        *,
        local_commit: Callable[[ThreadSnapshot], _LocalCommitT],
        **kwargs: Any,
    ) -> CommittedThreadCreate[ThreadSnapshot, _LocalCommitT]:
        """Create once, then apply the response to the requesting surface."""

        if not callable(local_commit):
            raise TypeError("local_commit 必须可调用。")
        attempt_id = self._next_attempt_id()
        try:
            snapshot = self._adapter.create_thread(**kwargs)
        except Exception as exc:
            if self._failure_is_known_no_effect(exc):
                raise
            raise ThreadCreateOutcomeUnknown(attempt_id, exc) from exc

        thread_id = self._snapshot_thread_id(snapshot)
        if not thread_id:
            protocol_error = ValueError(
                "thread/start response 缺少有效 thread id；创建结果无法归属。"
            )
            raise ThreadCreateOutcomeUnknown(attempt_id, protocol_error) from protocol_error

        try:
            self._acquire_runtime_lease(thread_id)
        except Exception as exc:
            raise ThreadCreateLocalCommitFailed(
                attempt_id=attempt_id,
                thread_id=thread_id,
                stage="runtime_lease",
                original_error=exc,
            ) from exc
        try:
            self._effective_settings.record_start_or_resume(
                thread_id,
                model=snapshot.effective_model,
                reasoning_effort=snapshot.effective_reasoning_effort,
                approval_policy=snapshot.effective_approval_policy,
                permissions_profile_id=snapshot.effective_permissions_profile_id,
                source="thread_start",
            )
        except Exception as exc:
            raise ThreadCreateLocalCommitFailed(
                attempt_id=attempt_id,
                thread_id=thread_id,
                stage="effective_settings",
                original_error=exc,
            ) from exc
        try:
            local_result = local_commit(snapshot)
        except Exception as exc:
            raise ThreadCreateLocalCommitFailed(
                attempt_id=attempt_id,
                thread_id=thread_id,
                stage="local_owner",
                original_error=exc,
            ) from exc
        return CommittedThreadCreate(response=snapshot, local_result=local_result)

    def begin_external_thread_create(self) -> ExternalThreadCreateAttempt:
        """Issue one current-generation capability before fcodex sends."""

        attempt_id = self._next_attempt_id()
        with self._external_lock:
            while attempt_id in self._external_attempts:
                attempt_id = self._next_attempt_id()
            attempt = ExternalThreadCreateAttempt(
                attempt_id=attempt_id,
                _generation=self._external_generation,
                _authority_token=self._external_authority_token,
            )
            self._external_attempts[attempt_id] = attempt
            return attempt

    def mark_external_thread_create_outcome_unknown(
        self,
        attempt: ExternalThreadCreateAttempt,
        original_error: Exception,
    ) -> None:
        """Consume an exact sent request without creating broader authority."""

        if not isinstance(original_error, Exception):
            raise TypeError("original_error 必须是 Exception。")
        self._consume_external_attempt(attempt)

    def commit_external_thread_create(
        self,
        attempt: ExternalThreadCreateAttempt,
        *,
        thread_id: str,
        local_commit: Callable[[], _LocalCommitT],
    ) -> CommittedThreadCreate[str, _LocalCommitT]:
        """Consume one fcodex success and commit its process-local owner."""

        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id or len(normalized_thread_id) > 256:
            raise ValueError("thread_id 无效。")
        if not callable(local_commit):
            raise TypeError("local_commit 必须可调用。")
        consumed = self._consume_external_attempt(attempt)
        try:
            local_result = local_commit()
        except Exception as exc:
            raise ThreadCreateLocalCommitFailed(
                attempt_id=consumed.attempt_id,
                thread_id=normalized_thread_id,
                stage="fcodex_runtime_source",
                original_error=exc,
            ) from exc
        return CommittedThreadCreate(
            response=normalized_thread_id,
            local_result=local_result,
        )

    def invalidate_connection(self) -> None:
        """Revoke all old-backend external create capabilities."""

        with self._external_lock:
            self._external_generation += 1
            self._external_attempts.clear()

    def _consume_external_attempt(
        self,
        attempt: ExternalThreadCreateAttempt,
    ) -> ExternalThreadCreateAttempt:
        if not isinstance(attempt, ExternalThreadCreateAttempt):
            raise TypeError("需要 ExternalThreadCreateAttempt capability。")
        with self._external_lock:
            active = self._external_attempts.get(attempt.attempt_id)
            if (
                attempt._authority_token is not self._external_authority_token
                or attempt._generation != self._external_generation
                or active is not attempt
            ):
                raise ThreadCreateSettlementError(
                    attempt_id=attempt.attempt_id,
                    message="external thread/create capability 已结算、已失效或不属于当前 backend。",
                )
            self._external_attempts.pop(attempt.attempt_id, None)
        return attempt

    def _next_attempt_id(self) -> str:
        attempt_id = str(self._new_attempt_id() or "").strip()
        if not attempt_id:
            raise ValueError("thread/create attempt_id 不能为空。")
        return attempt_id

    def _failure_is_known_no_effect(self, exc: Exception) -> bool:
        try:
            return bool(self._failure_known_no_effect(exc))
        except Exception:
            return False

    @staticmethod
    def _snapshot_thread_id(snapshot: object) -> str:
        if not isinstance(snapshot, ThreadSnapshot):
            return ""
        summary = snapshot.summary
        if not isinstance(summary, ThreadSummary):
            return ""
        return str(summary.thread_id or "").strip()
