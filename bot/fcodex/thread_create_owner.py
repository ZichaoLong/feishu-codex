"""External-transport ``thread/start`` ownership for one fcodex request.

The proxy owns websocket transport, the generic thread-create boundary owns a
current-backend one-shot capability, and the participant Registry owns machine
runtime sources.  This coordinator orders those process-local owners.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from bot.fcodex.operation_contract import (
    fcodex_client_response_receipt,
    fcodex_successful_response_thread_identity,
)
from bot.fcodex.participant_runtime_registry import (
    FcodexParticipantRuntimeRegistry,
    FcodexRequestSourceRef,
    FcodexRequestTransitionReceipt,
)
from bot.thread_create_transaction import (
    CommittedThreadCreate,
    ExternalThreadCreateAttempt,
    ThreadCreateLocalCommitFailed,
    ThreadCreateSettlementError,
)


logger = logging.getLogger(__name__)


class FcodexExternalThreadCreateAuthority(Protocol):
    """Narrow port to the shared process-local thread-create boundary."""

    def begin_external_thread_create(self) -> ExternalThreadCreateAttempt: ...

    def mark_external_thread_create_outcome_unknown(
        self,
        attempt: ExternalThreadCreateAttempt,
        original_error: Exception,
    ) -> None: ...

    def commit_external_thread_create(
        self,
        attempt: ExternalThreadCreateAttempt,
        *,
        thread_id: str,
        local_commit: Callable[[], FcodexRequestTransitionReceipt],
    ) -> CommittedThreadCreate[str, FcodexRequestTransitionReceipt]: ...


class FcodexTargetlessCreateRequest(Protocol):
    """Mutable exact request capability owned by the operation service."""

    request_key: str
    participant_id: str
    connection_id: str
    request_token: int
    thread_id: str
    root_thread_id: str
    runtime_request_source: FcodexRequestSourceRef | None
    external_create_attempt: ExternalThreadCreateAttempt | None
    external_create_resolution: FcodexThreadCreateResolution | None
    external_create_backend_epoch_invalidated: bool


@dataclass(frozen=True, slots=True)
class FcodexThreadCreateResolution:
    """Result of consuming one exact externally transported create attempt."""

    committed: bool
    retained: bool
    thread_id: str = ""
    root_thread_id: str = ""
    runtime_source: FcodexRequestSourceRef | None = None
    runtime_receipt: FcodexRequestTransitionReceipt | None = None


class FcodexThreadCreateOwner:
    """Order one backend-generation create and Registry commit exactly once."""

    def __init__(
        self,
        *,
        authority: FcodexExternalThreadCreateAuthority,
        participant_runtime_registry: FcodexParticipantRuntimeRegistry,
    ) -> None:
        required = (
            authority.begin_external_thread_create,
            authority.mark_external_thread_create_outcome_unknown,
            authority.commit_external_thread_create,
        )
        if any(not callable(capability) for capability in required):
            raise TypeError("fcodex thread-create owner 需要完整 external create authority。")
        if not isinstance(
            participant_runtime_registry,
            FcodexParticipantRuntimeRegistry,
        ):
            raise TypeError("fcodex thread-create owner 需要 participant Registry。")
        self._authority = authority
        self._registry = participant_runtime_registry

    def begin(self, *, participant_id: str) -> ExternalThreadCreateAttempt:
        """Issue a process-local capability before the proxy sends."""

        if not str(participant_id or "").strip():
            raise ValueError("participant_id 不能为空。")
        return self._authority.begin_external_thread_create()

    @staticmethod
    def invalidate_backend_epoch(request: FcodexTargetlessCreateRequest) -> None:
        """Retire an in-flight transport capability after global invalidation.

        ``ThreadRuntimeAuthority.invalidate_connection`` has already revoked
        the exact attempt. Keep the Service request only as stopped-epoch inventory;
        neither a late proxy response nor a later socket disconnect may try
        to consume that now-stale capability.
        """

        if request.external_create_attempt is None:
            return
        request.external_create_attempt = None
        request.external_create_backend_epoch_invalidated = True

    def retain_unknown(
        self,
        attempt: ExternalThreadCreateAttempt,
        *,
        reason: str,
    ) -> FcodexThreadCreateResolution:
        """Consume an already-sent request as exact outcome uncertainty."""

        self._authority.mark_external_thread_create_outcome_unknown(
            attempt,
            RuntimeError(str(reason or "fcodex thread/start outcome unknown")),
        )
        return FcodexThreadCreateResolution(committed=False, retained=True)

    def settle_response(
        self,
        attempt: ExternalThreadCreateAttempt,
        *,
        participant_id: str,
        connection_id: str,
        request_key: str,
        outcome: str,
        observed_thread_id: str,
        observed_root_thread_id: str,
    ) -> FcodexThreadCreateResolution:
        """Consume one exact backend response without inferring no-effect.

        Upstream can create the thread before a later ``thread/start`` step
        reports JSON-RPC error.  Therefore every non-success outcome after the
        proxy send boundary is unknown, not a retryable known rejection.
        """

        normalized_outcome = str(outcome or "").strip().lower()
        identity = (
            fcodex_successful_response_thread_identity(
                "thread/start",
                admitted_thread_id="",
                admitted_root_thread_id="",
                observed_thread_id=observed_thread_id,
                observed_root_thread_id=observed_root_thread_id,
            )
            if normalized_outcome == "success"
            else None
        )
        if identity is None:
            return self.retain_unknown(
                attempt,
                reason=(
                    "fcodex thread/start returned an untrusted success identity"
                    if normalized_outcome == "success"
                    else f"fcodex thread/start outcome={normalized_outcome or 'invalid'}"
                ),
            )

        thread_id, root_thread_id = identity
        source: FcodexRequestSourceRef | None = None
        transition: FcodexRequestTransitionReceipt | None = None

        def commit_registry_source() -> FcodexRequestTransitionReceipt:
            nonlocal source, transition
            source = self._registry.retain_request_source(
                participant_id,
                connection_id,
                request_key,
                thread_id,
            )
            if not self._source_matches(
                source,
                participant_id=participant_id,
                connection_id=connection_id,
                request_key=request_key,
                thread_id=thread_id,
            ):
                raise RuntimeError(
                    "fcodex thread/start Registry returned a mismatched request source"
                )
            transition = self._registry.promote_request_to_connection(source)
            if not self._is_exact_connection_transition(source, transition):
                raise RuntimeError(
                    "fcodex thread/start Registry source promotion was not confirmed"
                )
            return transition

        try:
            self._authority.commit_external_thread_create(
                attempt,
                thread_id=thread_id,
                local_commit=commit_registry_source,
            )
        except (ThreadCreateLocalCommitFailed, ThreadCreateSettlementError) as exc:
            # A local commit failure reports the successful response identity.
            # A stale/reset/replayed capability has no matching identity and
            # remains a structural error instead of becoming a retry path.
            if str(exc.thread_id or "").strip() != thread_id:
                raise
            logger.exception(
                "Unable to commit external fcodex create local owner: thread=%s",
                thread_id[:12],
            )
            exact_source = (
                source
                if self._source_matches(
                    source,
                    participant_id=participant_id,
                    connection_id=connection_id,
                    request_key=request_key,
                    thread_id=thread_id,
                )
                else None
            )
            return FcodexThreadCreateResolution(
                committed=False,
                retained=True,
                thread_id=thread_id,
                root_thread_id=root_thread_id,
                runtime_source=exact_source,
                runtime_receipt=(
                    transition
                    if self._is_exact_connection_transition(
                        exact_source,
                        transition,
                    )
                    else None
                ),
            )
        return FcodexThreadCreateResolution(
            committed=True,
            retained=False,
            thread_id=thread_id,
            root_thread_id=root_thread_id,
            runtime_source=source,
            runtime_receipt=transition,
        )

    def settle_client_response(
        self,
        request: FcodexTargetlessCreateRequest,
        *,
        outcome: str,
        observed_thread_id: str,
        observed_root_thread_id: str,
        remember_direct_root: Callable[[str], object],
        settle_local_request: Callable[
            [FcodexRequestTransitionReceipt | None],
            bool,
        ],
    ) -> dict[str, Any]:
        """Consume create once and retire the process-local request.

        A Registry acknowledgement retry reuses only the immutable resolution;
        it never replays the external create capability or reinterprets a
        different late response.
        """

        pending = fcodex_client_response_receipt(
            request.request_token,
            settled=False,
        )
        if request.external_create_backend_epoch_invalidated:
            return {**pending, "retained": True}
        normalized_outcome = str(outcome or "").strip().lower()
        resolution = request.external_create_resolution
        if resolution is None:
            attempt = request.external_create_attempt
            if attempt is None:
                raise RuntimeError(
                    "targetless fcodex thread/start 缺少 external create capability。"
                )
            resolution = self.settle_response(
                attempt,
                participant_id=request.participant_id,
                connection_id=request.connection_id,
                request_key=request.request_key,
                outcome=normalized_outcome,
                observed_thread_id=observed_thread_id,
                observed_root_thread_id=observed_root_thread_id,
            )
            self._validate_resolution(request, resolution)
            request.external_create_attempt = None
            request.external_create_resolution = resolution
        elif not self._retry_matches(
            resolution,
            outcome=normalized_outcome,
            observed_thread_id=observed_thread_id,
            observed_root_thread_id=observed_root_thread_id,
        ):
            return {**pending, "retained": True}
        self._apply_resolution(
            request,
            resolution,
            remember_direct_root=remember_direct_root,
        )

        if not settle_local_request(resolution.runtime_receipt):
            return {**pending, "retained": True}
        return fcodex_client_response_receipt(
            request.request_token,
            settled=True,
            retained=resolution.retained,
        )

    def settle_connection_lost(
        self,
        request: FcodexTargetlessCreateRequest,
        *,
        remember_direct_root: Callable[[str], object],
        settle_local_request: Callable[
            [FcodexRequestTransitionReceipt | None],
            bool,
        ],
    ) -> tuple[bool, bool]:
        """Ratchet an in-flight create or finish a post-commit ACK retry.

        Returns ``(outcome_unknown, local_request_settled)``.  A resolution
        already committed by the local boundary is never downgraded just
        because its originating websocket disappeared.
        """

        resolution = request.external_create_resolution
        if request.external_create_backend_epoch_invalidated:
            return True, False
        if resolution is None:
            attempt = request.external_create_attempt
            if attempt is None:
                raise RuntimeError(
                    "targetless fcodex thread/start disconnect 缺少 create capability。"
                )
            resolution = self.settle_response(
                attempt,
                participant_id=request.participant_id,
                connection_id=request.connection_id,
                request_key=request.request_key,
                outcome="unknown",
                observed_thread_id="",
                observed_root_thread_id="",
            )
            self._validate_resolution(request, resolution)
            request.external_create_attempt = None
            request.external_create_resolution = resolution
        self._apply_resolution(
            request,
            resolution,
            remember_direct_root=remember_direct_root,
        )
        settled = settle_local_request(resolution.runtime_receipt)
        return not resolution.committed, settled

    @staticmethod
    def _validate_resolution(
        request: FcodexTargetlessCreateRequest,
        resolution: FcodexThreadCreateResolution,
    ) -> None:
        if not isinstance(resolution, FcodexThreadCreateResolution):
            raise TypeError("targetless thread/start 需要 typed create resolution。")
        if resolution.committed == resolution.retained:
            raise RuntimeError("targetless thread/start resolution 未提供唯一终态。")
        if resolution.committed and (
            resolution.runtime_source is None
            or resolution.runtime_receipt is None
        ):
            raise RuntimeError(
                "committed targetless thread/start 缺少 Registry source/receipt。"
            )
        if resolution.runtime_receipt is not None and not (
            FcodexThreadCreateOwner._is_exact_connection_transition(
                resolution.runtime_source,
                resolution.runtime_receipt,
            )
        ):
            raise RuntimeError(
                "targetless thread/start Registry resolution capability 冲突。"
            )
        source = resolution.runtime_source
        if source is not None and (
            source.participant_id != request.participant_id
            or source.connection_id != request.connection_id
            or source.request_key != request.request_key
            or source.thread_id != resolution.thread_id
        ):
            raise RuntimeError(
                "targetless thread/start Registry source 与 exact request 冲突。"
            )
        if resolution.runtime_source is not None:
            if not resolution.thread_id:
                raise RuntimeError(
                    "targetless thread/start runtime source 缺少 durable thread identity。"
                )
        if resolution.thread_id and resolution.root_thread_id != resolution.thread_id:
            raise RuntimeError("targetless thread/start 只能提交 direct root identity。")

    @staticmethod
    def _source_matches(
        source: object,
        *,
        participant_id: str,
        connection_id: str,
        request_key: str,
        thread_id: str,
    ) -> bool:
        return bool(
            isinstance(source, FcodexRequestSourceRef)
            and source.participant_id == participant_id
            and source.connection_id == connection_id
            and source.request_key == request_key
            and source.thread_id == thread_id
        )

    @staticmethod
    def _is_exact_connection_transition(
        source: FcodexRequestSourceRef | None,
        transition: FcodexRequestTransitionReceipt | None,
    ) -> bool:
        return bool(
            source is not None
            and isinstance(transition, FcodexRequestTransitionReceipt)
            and transition.source == source
            and transition.target == "connection"
            and transition.exact_settled
            and transition.holder_presence == "confirmed"
        )

    @staticmethod
    def _apply_resolution(
        request: FcodexTargetlessCreateRequest,
        resolution: FcodexThreadCreateResolution,
        *,
        remember_direct_root: Callable[[str], object],
    ) -> None:
        FcodexThreadCreateOwner._validate_resolution(request, resolution)
        if resolution.runtime_source is not None:
            request.runtime_request_source = resolution.runtime_source
        if not resolution.thread_id:
            return
        request.thread_id = resolution.thread_id
        request.root_thread_id = resolution.root_thread_id
        remember_direct_root(resolution.thread_id)

    @staticmethod
    def _retry_matches(
        resolution: FcodexThreadCreateResolution,
        *,
        outcome: str,
        observed_thread_id: str,
        observed_root_thread_id: str,
    ) -> bool:
        return bool(
            # A Registry acknowledgement retry may reuse only the immutable
            # process-local resolution. Requiring the receipt keeps it from
            # replaying either create or the local commit callback.
            resolution.runtime_receipt is not None
            and outcome == "success"
            and str(observed_thread_id or "").strip() == resolution.thread_id
            and str(observed_root_thread_id or "").strip()
            == resolution.root_thread_id
        )
