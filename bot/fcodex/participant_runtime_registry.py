"""RuntimeLoop-owned fcodex participant and runtime-source registry.

This owner is deliberately independent from main-turn and interaction state.
It owns participant incarnations, live websocket endpoints, their liveness
generations, and the three process-local reasons why
the service's machine-level fcodex runtime holder must remain installed:

* a confirmed connection subscription;
* an in-flight resume/start request;
* an unresolved resume/start outcome.

Only this registry acquires or releases the corresponding machine holder.
Callers exchange immutable receipts and source snapshots, never mutable
participant records.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from bot.fcodex.interaction_contract import fcodex_connection_id
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.thread_runtime_lease_store import (
    ThreadRuntimeLeaseHolder,
    ThreadRuntimeLeaseStore,
)
from bot.thread_runtime_coordination import acquire_thread_runtime_holder_or_raise


logger = logging.getLogger(__name__)


FcodexParticipantState = Literal["connected", "grace", "orphaned"]
FcodexRuntimeHolderPresence = Literal["absent", "confirmed", "unknown"]
FcodexRequestTransitionTarget = Literal["connection", "unknown", "discard"]
FcodexRequestTransitionOutcome = Literal[
    "transitioned",
    "exact_already_settled",
    "missing",
    "identity_conflict",
    "effect_unknown",
]
FcodexRequestTransitionConflict = Literal[
    "source_identity",
    "different_target",
    "endpoint_not_live",
]


@dataclass(frozen=True, slots=True)
class FcodexParticipantRuntimeRegistryPorts:
    """Required effect ports for the participant/runtime owner."""

    thread_runtime_lease_store: ThreadRuntimeLeaseStore
    runtime_holder_for_participant: Callable[[str], ThreadRuntimeLeaseHolder]
    global_loaded_gate: Callable[[str], Any]
    schedule_participant_expiry: Callable[[str, int, float], None]
    schedule_connection_expiry: Callable[[str, str, int, float], None]


@dataclass(frozen=True, slots=True)
class FcodexParticipantConnectionReceipt:
    participant_id: str
    connection_id: str
    state: FcodexParticipantState
    is_new_connection: bool


@dataclass(frozen=True, slots=True)
class FcodexParticipantDisconnectReceipt:
    participant_id: str
    connection_id: str
    state: FcodexParticipantState | Literal["unknown"]
    participant_known: bool
    connection_removed: bool


@dataclass(frozen=True, slots=True)
class FcodexParticipantRuntimeSnapshot:
    participant_id: str
    state: FcodexParticipantState
    connection_ids: tuple[str, ...]
    tracked_thread_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FcodexThreadRuntimeSourceSnapshot:
    """Immutable source inventory for one participant/thread pair."""

    participant_id: str
    thread_id: str
    connection_ids: tuple[str, ...]
    pending_request_keys: tuple[str, ...]
    unknown: bool
    holder_presence: FcodexRuntimeHolderPresence
    thread_authoritative_cleanup_pending: bool

    @property
    def holder_tracked(self) -> bool:
        return self.holder_presence != "absent"


@dataclass(frozen=True, slots=True)
class FcodexRequestSourceRef:
    """Registry-issued exact capability for one pending start/resume source.

    A JSON-RPC id may be reused after its earlier request finishes.  The
    Registry-lifetime generation therefore participates in identity and must
    accompany every transition; ``request_key`` alone is never exact.
    """

    request_key: str
    generation: int
    participant_id: str
    connection_id: str
    thread_id: str


@dataclass(frozen=True, slots=True)
class FcodexRequestTransitionReceipt:
    """Typed proof of one exact request-source transition attempt."""

    source: FcodexRequestSourceRef
    target: FcodexRequestTransitionTarget
    outcome: FcodexRequestTransitionOutcome
    conflict: FcodexRequestTransitionConflict | None = None
    holder_presence: FcodexRuntimeHolderPresence = "absent"

    @property
    def exact_settled(self) -> bool:
        return self.outcome in {"transitioned", "exact_already_settled"}


@dataclass(frozen=True, slots=True)
class FcodexBackendEpochCloseReceipt:
    """Diagnostics for one destructive, purge-proven backend epoch close."""

    participant_ids: tuple[str, ...]
    endpoint_ids: tuple[tuple[str, str], ...]
    source_pairs: tuple[tuple[str, str], ...]
    holder_pairs: tuple[tuple[str, str], ...]
    authoritative_cleanup_thread_ids: tuple[str, ...]


@dataclass(slots=True)
class _ParticipantRuntime:
    participant_id: str
    state: FcodexParticipantState = "connected"
    connection_ids: set[str] = field(default_factory=set)
    expiry_generation: int = 0
    connection_expiry_generation: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _RequestTransition:
    source: FcodexRequestSourceRef
    target: FcodexRequestTransitionTarget
    phase: Literal["effect_pending", "settled"]


@dataclass(slots=True)
class _AuthoritativeCleanup:
    connection_generations: dict[tuple[str, str, str], int] = field(
        default_factory=dict
    )
    unknown_generations: dict[tuple[str, str], int] = field(default_factory=dict)
    tracked_participant_ids: set[str] = field(default_factory=set)


class FcodexParticipantRuntimeRegistry:
    """Own fcodex endpoint liveness and machine runtime-holder sources."""

    def __init__(
        self,
        *,
        ports: FcodexParticipantRuntimeRegistryPorts,
        runtime_context_guard: RuntimeContextGuard,
        disconnect_grace_seconds: float = 15.0,
        connection_heartbeat_timeout_seconds: float = 12.0,
    ) -> None:
        required = (
            ports.runtime_holder_for_participant,
            ports.global_loaded_gate,
            ports.schedule_participant_expiry,
            ports.schedule_connection_expiry,
        )
        if any(not callable(capability) for capability in required):
            raise TypeError("fcodex participant/runtime registry 的 effect ports 必须全部可调用。")
        if not callable(runtime_context_guard):
            raise TypeError("fcodex participant/runtime registry 需要 RuntimeLoop context guard。")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._disconnect_grace_seconds = max(float(disconnect_grace_seconds), 0.0)
        self._connection_heartbeat_timeout_seconds = max(
            float(connection_heartbeat_timeout_seconds),
            1.0,
        )

        self._participants: dict[str, _ParticipantRuntime] = {}
        self._connection_sources: dict[tuple[str, str, str], int] = {}
        self._pending_request_sources: dict[str, FcodexRequestSourceRef] = {}
        # Exact tombstones make same-stack retries reliable without inferring
        # provenance from pair-level connection/unknown sources.  Callers may
        # acknowledge a settled receipt only after deleting their own request
        # record; backend epoch close is the final leak-safe cleanup boundary.
        self._request_transitions: dict[
            tuple[str, int],
            _RequestTransition,
        ] = {}
        self._unknown_sources: dict[tuple[str, str], int] = {}
        self._source_generation_sequence = 0
        # Timer capabilities must never repeat while this Registry lives, even
        # after an empty participant record is retired and the same incarnation
        # and connection ids reconnect.
        self._liveness_generation_sequence = 0
        # Retry evidence for an authoritative gone event whose effect could
        # not be committed under the shared mutation gate. It is not a runtime
        # source and never keeps a holder by itself.
        self._authoritative_cleanups: dict[str, _AuthoritativeCleanup] = {}
        # This is effect/retry bookkeeping, not a fifth logical source.  The
        # four maps above alone decide whether a holder is still needed.
        # `unknown` is materially different from `confirmed`: a replacement
        # source must idempotently acquire again before it may be published.
        self._holder_presence_by_participant: dict[
            str,
            dict[str, Literal["confirmed", "unknown"]],
        ] = {}

    # ------------------------------------------------------------------
    # Participant and endpoint lifecycle

    def connect(
        self,
        participant_id: str,
        connection_id: str,
    ) -> FcodexParticipantConnectionReceipt:
        self._runtime_context_guard()
        normalized_participant_id = self._normalize_participant_id(participant_id)
        normalized_connection_id = fcodex_connection_id(connection_id)
        participant = self._participants.get(normalized_participant_id)
        if participant is None:
            participant = _ParticipantRuntime(participant_id=normalized_participant_id)
        is_new_connection = normalized_connection_id not in participant.connection_ids
        # Schedule first. A failed timer installation must not publish a new
        # live endpoint, and a failed heartbeat refresh must preserve the old
        # generation/timer as the current liveness authority.
        self._touch_connection_liveness(participant, normalized_connection_id)
        self._participants.setdefault(normalized_participant_id, participant)
        participant.connection_ids.add(normalized_connection_id)
        participant.state = "connected"
        # Invalidate a participant-grace timer even for an idempotent connect.
        participant.expiry_generation = self._next_liveness_generation()
        self._retry_unneeded_holder_effects(participant.participant_id)
        return FcodexParticipantConnectionReceipt(
            participant_id=normalized_participant_id,
            connection_id=normalized_connection_id,
            state=participant.state,
            is_new_connection=is_new_connection,
        )

    def heartbeat(
        self,
        participant_id: str,
        connection_id: str,
    ) -> FcodexParticipantConnectionReceipt:
        self._runtime_context_guard()
        participant, normalized_connection_id = self._require_live_endpoint(
            participant_id,
            connection_id,
        )
        self._touch_connection_liveness(participant, normalized_connection_id)
        self._retry_unneeded_holder_effects(participant.participant_id)
        return FcodexParticipantConnectionReceipt(
            participant_id=participant.participant_id,
            connection_id=normalized_connection_id,
            state=participant.state,
            is_new_connection=False,
        )

    def require_live_endpoint(self, participant_id: str, connection_id: str) -> None:
        self._runtime_context_guard()
        self._require_live_endpoint(participant_id, connection_id)

    def has_live_endpoint(self, participant_id: str, connection_id: str) -> bool:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        participant = self._participants.get(normalized_participant_id)
        return bool(
            participant
            and normalized_connection_id
            and normalized_connection_id in participant.connection_ids
        )

    def has_live_connection_source(self, thread_id: str) -> bool:
        """Return whether any live endpoint is subscribed to one exact thread."""

        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return False
        for participant_id, connection_id, source_thread_id in self._connection_sources:
            if source_thread_id != normalized_thread_id:
                continue
            participant = self._participants.get(participant_id)
            if participant is not None and connection_id in participant.connection_ids:
                return True
        return False

    def disconnect(
        self,
        participant_id: str,
        connection_id: str,
    ) -> FcodexParticipantDisconnectReceipt:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        participant = self._participants.get(normalized_participant_id)
        if participant is None:
            return FcodexParticipantDisconnectReceipt(
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                state="unknown",
                participant_known=False,
                connection_removed=False,
            )
        if (
            not normalized_connection_id
            or normalized_connection_id not in participant.connection_ids
        ):
            return FcodexParticipantDisconnectReceipt(
                participant_id=participant.participant_id,
                connection_id=normalized_connection_id,
                state=participant.state,
                participant_known=True,
                connection_removed=False,
            )

        enter_grace = (
            len(participant.connection_ids) == 1
            and participant.state != "orphaned"
        )
        next_participant_generation = self._next_liveness_generation()
        grace_timer_scheduled = False
        if enter_grace:
            try:
                self._ports.schedule_participant_expiry(
                    participant.participant_id,
                    next_participant_generation,
                    self._disconnect_grace_seconds,
                )
                grace_timer_scheduled = True
            except Exception:
                # This disconnect may itself be the one-shot heartbeat expiry
                # callback. Rolling it back would leave no future callback and
                # could advertise a dead endpoint forever. Lose reconnect grace
                # instead: commit the disconnect and fail closed as orphaned.
                logger.exception(
                    "Unable to schedule fcodex participant grace; orphaning immediately: participant=%s",
                    participant.participant_id,
                )
        participant.connection_ids.remove(normalized_connection_id)
        # The participant-wide sequence, rather than a stored per-connection
        # entry, prevents timer ABA when a connection id is reused.  Forget the
        # dead endpoint's current generation so a long-lived participant does
        # not accumulate every historical websocket id.
        participant.connection_expiry_generation.pop(normalized_connection_id, None)
        self._drop_connection_sources(participant.participant_id, normalized_connection_id)
        if enter_grace:
            participant.state = "grace" if grace_timer_scheduled else "orphaned"
            participant.expiry_generation = next_participant_generation
        return FcodexParticipantDisconnectReceipt(
            participant_id=participant.participant_id,
            connection_id=normalized_connection_id,
            state=participant.state,
            participant_known=True,
            connection_removed=True,
        )

    def connection_expiry_is_current(
        self,
        participant_id: str,
        connection_id: str,
        expiry_generation: int,
    ) -> bool:
        self._runtime_context_guard()
        participant = self._participants.get(str(participant_id or "").strip())
        normalized_connection_id = str(connection_id or "").strip()
        return bool(
            participant
            and normalized_connection_id in participant.connection_ids
            and participant.connection_expiry_generation.get(normalized_connection_id)
            == int(expiry_generation)
        )

    def expire_participant(self, participant_id: str, expiry_generation: int) -> bool:
        self._runtime_context_guard()
        participant = self._participants.get(str(participant_id or "").strip())
        if participant is None:
            return False
        if (
            participant.expiry_generation != int(expiry_generation)
            or participant.connection_ids
            or participant.state != "grace"
        ):
            return False
        participant.state = "orphaned"
        return True

    def snapshot(self, participant_id: str) -> FcodexParticipantRuntimeSnapshot | None:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        participant = self._participants.get(normalized_participant_id)
        if participant is None:
            return None
        return FcodexParticipantRuntimeSnapshot(
            participant_id=participant.participant_id,
            state=participant.state,
            connection_ids=tuple(sorted(participant.connection_ids)),
            tracked_thread_ids=tuple(
                sorted(
                    self._holder_presence_by_participant.get(
                        participant.participant_id,
                        (),
                    )
                )
            ),
        )

    # ------------------------------------------------------------------
    # Runtime sources

    def retain_connection_source(
        self,
        participant_id: str,
        connection_id: str,
        thread_id: str,
    ) -> None:
        self._runtime_context_guard()
        participant, normalized_connection_id = self._require_live_endpoint(
            participant_id,
            connection_id,
        )
        normalized_thread_id = self._normalize_thread_id(thread_id)
        if (
            self._holder_presence(participant.participant_id, normalized_thread_id)
            != "confirmed"
        ):
            self._ensure_machine_holder(participant.participant_id, normalized_thread_id)
        self._connection_sources[
            (
                participant.participant_id,
                normalized_connection_id,
                normalized_thread_id,
            )
        ] = self._next_source_generation()

    def forget_connection_source(
        self,
        participant_id: str,
        connection_id: str,
        thread_id: str,
    ) -> bool:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_participant_id or not normalized_connection_id or not normalized_thread_id:
            return True
        self._connection_sources.pop(
            (
                normalized_participant_id,
                normalized_connection_id,
                normalized_thread_id,
            ),
            None,
        )
        return self._release_machine_holder_if_unneeded(
            normalized_participant_id,
            normalized_thread_id,
        )

    def retain_request_source(
        self,
        participant_id: str,
        connection_id: str,
        request_key: str,
        thread_id: str,
    ) -> FcodexRequestSourceRef:
        self._runtime_context_guard()
        participant, normalized_connection_id = self._require_live_endpoint(
            participant_id,
            connection_id,
        )
        normalized_request_key = str(request_key or "")
        if not normalized_request_key:
            raise ValueError("fcodex runtime request key 不能为空。")
        normalized_thread_id = self._normalize_thread_id(thread_id)
        existing = self._pending_request_sources.get(normalized_request_key)
        if existing is not None:
            if (
                existing.participant_id != participant.participant_id
                or existing.connection_id != normalized_connection_id
                or existing.thread_id != normalized_thread_id
            ):
                raise RuntimeError("fcodex pending runtime request identity 冲突。")
            if (
                self._holder_presence(
                    existing.participant_id,
                    existing.thread_id,
                )
                != "confirmed"
            ):
                self._ensure_machine_holder(
                    existing.participant_id,
                    existing.thread_id,
                )
            return existing
        if (
            self._holder_presence(participant.participant_id, normalized_thread_id)
            != "confirmed"
        ):
            self._ensure_machine_holder(participant.participant_id, normalized_thread_id)
        source = FcodexRequestSourceRef(
            request_key=normalized_request_key,
            generation=self._next_source_generation(),
            participant_id=participant.participant_id,
            connection_id=normalized_connection_id,
            thread_id=normalized_thread_id,
        )
        self._pending_request_sources[normalized_request_key] = source
        return source

    def promote_request_to_connection(
        self,
        source: FcodexRequestSourceRef,
    ) -> FcodexRequestTransitionReceipt:
        self._runtime_context_guard()
        preflight = self._preflight_request_transition(
            source,
            target="connection",
            require_live_endpoint=True,
        )
        if preflight is not None:
            return preflight
        if (
            self._holder_presence(source.participant_id, source.thread_id)
            != "confirmed"
        ):
            try:
                self._ensure_machine_holder(source.participant_id, source.thread_id)
            except Exception:
                return self._request_transition_receipt(
                    source,
                    target="connection",
                    outcome="effect_unknown",
                )
        self._connection_sources[
            (source.participant_id, source.connection_id, source.thread_id)
        ] = self._next_source_generation()
        self._pending_request_sources.pop(source.request_key, None)
        self._request_transitions[self._request_transition_key(source)] = (
            _RequestTransition(
                source=source,
                target="connection",
                phase="settled",
            )
        )
        return self._request_transition_receipt(
            source,
            target="connection",
            outcome="transitioned",
        )

    def promote_request_to_unknown(
        self,
        source: FcodexRequestSourceRef,
    ) -> FcodexRequestTransitionReceipt:
        self._runtime_context_guard()
        preflight = self._preflight_request_transition(
            source,
            target="unknown",
        )
        if preflight is not None:
            return preflight
        if (
            self._holder_presence(source.participant_id, source.thread_id)
            != "confirmed"
        ):
            try:
                self._ensure_machine_holder(source.participant_id, source.thread_id)
            except Exception:
                return self._request_transition_receipt(
                    source,
                    target="unknown",
                    outcome="effect_unknown",
                )
        self._unknown_sources[
            (source.participant_id, source.thread_id)
        ] = self._next_source_generation()
        self._pending_request_sources.pop(source.request_key, None)
        self._request_transitions[self._request_transition_key(source)] = (
            _RequestTransition(
                source=source,
                target="unknown",
                phase="settled",
            )
        )
        return self._request_transition_receipt(
            source,
            target="unknown",
            outcome="transitioned",
        )

    def discard_request_source(
        self,
        source: FcodexRequestSourceRef,
    ) -> FcodexRequestTransitionReceipt:
        self._runtime_context_guard()
        preflight = self._preflight_request_transition(
            source,
            target="discard",
        )
        if preflight is not None:
            return preflight
        transition_key = self._request_transition_key(source)
        transition = self._request_transitions.get(transition_key)
        if transition is None:
            # The staged transition, rather than a now-absent pending source,
            # is the exact retry authority for an unconfirmed release effect.
            transition = _RequestTransition(
                source=source,
                target="discard",
                phase="effect_pending",
            )
            self._request_transitions[transition_key] = transition
            self._pending_request_sources.pop(source.request_key, None)
        if not self._release_machine_holder_if_unneeded(
            source.participant_id,
            source.thread_id,
        ):
            return self._request_transition_receipt(
                source,
                target="discard",
                outcome="effect_unknown",
            )
        transition.phase = "settled"
        return self._request_transition_receipt(
            source,
            target="discard",
            outcome="transitioned",
        )

    def acknowledge_request_transition(
        self,
        receipt: FcodexRequestTransitionReceipt,
    ) -> bool:
        """Forget an exact tombstone after its caller removed local state."""

        self._runtime_context_guard()
        if not isinstance(receipt, FcodexRequestTransitionReceipt):
            raise TypeError("fcodex request transition acknowledgement 需要 typed receipt。")
        if not receipt.exact_settled:
            return False
        transition_key = self._request_transition_key(receipt.source)
        transition = self._request_transitions.get(transition_key)
        if (
            transition is None
            or transition.source != receipt.source
            or transition.target != receipt.target
            or transition.phase != "settled"
        ):
            return False
        self._request_transitions.pop(transition_key, None)
        return True

    def clear_thread_sources(
        self,
        thread_id: str,
    ) -> bool:
        """Clear connection and resolved-unknown sources for authoritative gone.

        An in-flight resume/start remains a source: it can still complete after
        the lifecycle frame and load the thread again.  Operation-root sources
        likewise require the operation owner's exact settlement transaction.
        """

        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return True
        cleanup = _AuthoritativeCleanup(
            connection_generations={
                key: generation
                for key, generation in self._connection_sources.items()
                if key[2] == normalized_thread_id
            },
            unknown_generations={
                key: generation
                for key, generation in self._unknown_sources.items()
                if key[1] == normalized_thread_id
            },
            tracked_participant_ids={
                participant_id
                for participant_id, holder_presence in (
                    self._holder_presence_by_participant.items()
                )
                if normalized_thread_id in holder_presence
            },
        )
        self._authoritative_cleanups[normalized_thread_id] = cleanup
        return self._apply_authoritative_cleanup(normalized_thread_id, cleanup)

    def retry_authoritative_cleanups(self) -> bool:
        """Retry quarantined authoritative-gone effects without ABA clearing."""

        self._runtime_context_guard()
        settled = True
        for thread_id, cleanup in tuple(self._authoritative_cleanups.items()):
            settled = self._apply_authoritative_cleanup(
                thread_id,
                cleanup,
            ) and settled
        return settled

    def release_unneeded_sources(self, participant_id: str) -> bool:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        settled = True
        for thread_id in tuple(
            self._holder_presence_by_participant.get(
                normalized_participant_id,
                (),
            )
        ):
            settled = self._release_machine_holder_if_unneeded(
                normalized_participant_id,
                thread_id,
            ) and settled
        # An orphan can legitimately have no holder at all.  Retirement must
        # therefore not depend on entering the loop above.
        self._retire_orphaned_participant_if_empty(normalized_participant_id)
        return settled

    def close_backend_epoch_after_machine_replace(
        self,
    ) -> FcodexBackendEpochCloseReceipt:
        """Destructively close every fcodex fact from the stopped backend.

        The atomic machine-holder replacement proves that all transient holders
        from this service instance's stopped epoch are gone. Endpoint liveness
        is epoch-local: an existing proxy control socket must perform
        ``participant-connected`` again before asserting a replacement source.
        """

        self._runtime_context_guard()
        receipt = FcodexBackendEpochCloseReceipt(
            participant_ids=tuple(sorted(self._participants)),
            endpoint_ids=tuple(
                sorted(
                    (participant_id, connection_id)
                    for participant_id, participant in self._participants.items()
                    for connection_id in participant.connection_ids
                )
            ),
            source_pairs=self._runtime_source_pairs(),
            holder_pairs=tuple(
                sorted(
                    (participant_id, thread_id)
                    for participant_id, thread_presence in (
                        self._holder_presence_by_participant.items()
                    )
                    for thread_id in thread_presence
                )
            ),
            authoritative_cleanup_thread_ids=tuple(
                sorted(self._authoritative_cleanups)
            ),
        )
        self._participants.clear()
        self._connection_sources.clear()
        self._pending_request_sources.clear()
        self._request_transitions.clear()
        self._unknown_sources.clear()
        self._authoritative_cleanups.clear()
        self._holder_presence_by_participant.clear()
        return receipt

    def source_snapshot(
        self,
        participant_id: str,
        thread_id: str,
    ) -> FcodexThreadRuntimeSourceSnapshot:
        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        connection_ids = tuple(
            sorted(
                connection_id
                for (
                    candidate_participant_id,
                    connection_id,
                    candidate_thread_id,
                ) in self._connection_sources
                if candidate_participant_id == normalized_participant_id
                and candidate_thread_id == normalized_thread_id
            )
        )
        pending_request_keys = tuple(
            sorted(
                request_key
                for request_key, source in self._pending_request_sources.items()
                if source.participant_id == normalized_participant_id
                and source.thread_id == normalized_thread_id
            )
        )
        return FcodexThreadRuntimeSourceSnapshot(
            participant_id=normalized_participant_id,
            thread_id=normalized_thread_id,
            connection_ids=connection_ids,
            pending_request_keys=pending_request_keys,
            unknown=(normalized_participant_id, normalized_thread_id)
            in self._unknown_sources,
            holder_presence=self._holder_presence(
                normalized_participant_id,
                normalized_thread_id,
            ),
            thread_authoritative_cleanup_pending=(
                normalized_thread_id in self._authoritative_cleanups
            ),
        )

    # ------------------------------------------------------------------
    # Internal effect and invariant helpers

    @staticmethod
    def _normalize_participant_id(participant_id: str) -> str:
        normalized = str(participant_id or "").strip()
        if not normalized.startswith("fcodex:") or normalized.count(":") < 2:
            raise ValueError("operation participant 必须是带 incarnation 的 fcodex id。")
        return normalized

    @staticmethod
    def _normalize_thread_id(thread_id: str) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("fcodex runtime thread_id 不能为空。")
        return normalized

    def _participant_or_error(self, participant_id: str) -> _ParticipantRuntime:
        participant = self._participants.get(participant_id)
        if participant is None:
            raise RuntimeError("fcodex participant 未注册；当前按 fail-closed 拒绝。")
        return participant

    def _require_live_endpoint(
        self,
        participant_id: str,
        connection_id: str,
    ) -> tuple[_ParticipantRuntime, str]:
        normalized_participant_id = self._normalize_participant_id(participant_id)
        normalized_connection_id = fcodex_connection_id(connection_id)
        participant = self._participant_or_error(normalized_participant_id)
        if normalized_connection_id not in participant.connection_ids:
            raise RuntimeError("fcodex 控制连接未注册；当前按 fail-closed 拒绝。")
        return participant, normalized_connection_id

    @staticmethod
    def _require_request_source_ref(source: FcodexRequestSourceRef) -> None:
        if not isinstance(source, FcodexRequestSourceRef):
            raise TypeError("fcodex request source transition 需要 Registry-issued ref。")

    @staticmethod
    def _request_transition_key(
        source: FcodexRequestSourceRef,
    ) -> tuple[str, int]:
        return source.request_key, source.generation

    def _request_transition_receipt(
        self,
        source: FcodexRequestSourceRef,
        *,
        target: FcodexRequestTransitionTarget,
        outcome: FcodexRequestTransitionOutcome,
        conflict: FcodexRequestTransitionConflict | None = None,
    ) -> FcodexRequestTransitionReceipt:
        return FcodexRequestTransitionReceipt(
            source=source,
            target=target,
            outcome=outcome,
            conflict=conflict,
            holder_presence=self._holder_presence(
                source.participant_id,
                source.thread_id,
            ),
        )

    def _preflight_request_transition(
        self,
        source: FcodexRequestSourceRef,
        *,
        target: FcodexRequestTransitionTarget,
        require_live_endpoint: bool = False,
    ) -> FcodexRequestTransitionReceipt | None:
        """Return a terminal receipt, or ``None`` when an effect may proceed."""

        self._require_request_source_ref(source)
        transition = self._request_transitions.get(
            self._request_transition_key(source)
        )
        if transition is not None:
            if transition.source != source:
                return self._request_transition_receipt(
                    source,
                    target=target,
                    outcome="identity_conflict",
                    conflict="source_identity",
                )
            if transition.target != target:
                if (
                    transition.target == "discard"
                    and transition.phase == "effect_pending"
                    and target == "unknown"
                ):
                    # A release whose effect is unknown cannot remain a
                    # known-no-effect discard once the connection or operator
                    # stop proves the upstream request outcome is unknown.
                    # Reacquiring the exact holder before promotion makes this
                    # a safe monotonic ratchet, including when the ambiguous
                    # release actually succeeded.
                    return None
                return self._request_transition_receipt(
                    source,
                    target=target,
                    outcome="identity_conflict",
                    conflict="different_target",
                )
            if transition.phase == "settled":
                return self._request_transition_receipt(
                    source,
                    target=target,
                    outcome="exact_already_settled",
                )
            # Only discard stages an effect before it can become settled.
            return None

        pending = self._pending_request_sources.get(source.request_key)
        if pending is None:
            conflicting_tombstone = any(
                candidate.source.request_key == source.request_key
                for candidate in self._request_transitions.values()
            )
            return self._request_transition_receipt(
                source,
                target=target,
                outcome=(
                    "identity_conflict" if conflicting_tombstone else "missing"
                ),
                conflict=("source_identity" if conflicting_tombstone else None),
            )
        if pending != source:
            return self._request_transition_receipt(
                source,
                target=target,
                outcome="identity_conflict",
                conflict="source_identity",
            )
        if require_live_endpoint and not self.has_live_endpoint(
            source.participant_id,
            source.connection_id,
        ):
            return self._request_transition_receipt(
                source,
                target=target,
                outcome="identity_conflict",
                conflict="endpoint_not_live",
            )
        return None

    def _touch_connection_liveness(
        self,
        participant: _ParticipantRuntime,
        connection_id: str,
    ) -> None:
        generation = self._next_liveness_generation()
        self._ports.schedule_connection_expiry(
            participant.participant_id,
            connection_id,
            generation,
            self._connection_heartbeat_timeout_seconds,
        )
        participant.connection_expiry_generation[connection_id] = generation

    def _drop_connection_sources(self, participant_id: str, connection_id: str) -> None:
        thread_ids = {
            thread_id
            for candidate_participant_id, candidate_connection_id, thread_id in self._connection_sources
            if candidate_participant_id == participant_id
            and candidate_connection_id == connection_id
        }
        for thread_id in thread_ids:
            self._connection_sources.pop(
                (participant_id, connection_id, thread_id),
                None,
            )
        for thread_id in thread_ids:
            self._release_machine_holder_if_unneeded(participant_id, thread_id)

    def _retry_unneeded_holder_effects(self, participant_id: str) -> None:
        """Use live endpoint traffic to drive otherwise ownerless release retry."""

        if not self.release_unneeded_sources(participant_id):
            logger.warning(
                "Fcodex participant still has deferred runtime-holder cleanup: participant=%s",
                participant_id,
            )

    def _apply_authoritative_cleanup(
        self,
        thread_id: str,
        cleanup: _AuthoritativeCleanup,
    ) -> bool:
        affected_participants = set(cleanup.tracked_participant_ids)
        for key, expected_generation in cleanup.connection_generations.items():
            if self._connection_sources.get(key) == expected_generation:
                self._connection_sources.pop(key, None)
                affected_participants.add(key[0])
        for key, expected_generation in cleanup.unknown_generations.items():
            if self._unknown_sources.get(key) == expected_generation:
                self._unknown_sources.pop(key, None)
                affected_participants.add(key[0])
        settled = True
        for participant_id in sorted(affected_participants):
            settled = self._release_machine_holder_if_unneeded(
                participant_id,
                thread_id,
            ) and settled
        if settled and self._authoritative_cleanups.get(thread_id) is cleanup:
            self._authoritative_cleanups.pop(thread_id, None)
        return settled

    def _next_source_generation(self) -> int:
        self._source_generation_sequence += 1
        return self._source_generation_sequence

    def _next_liveness_generation(self) -> int:
        self._liveness_generation_sequence += 1
        return self._liveness_generation_sequence

    def _ensure_machine_holder(self, participant_id: str, thread_id: str) -> None:
        loaded_gate = self._ports.global_loaded_gate(thread_id)
        if not bool(getattr(loaded_gate, "allowed", False)):
            raise RuntimeError(
                str(
                    getattr(
                        loaded_gate,
                        "reason_text",
                        "当前无法获取 thread live runtime。",
                    )
                )
            )
        holder = self._ports.runtime_holder_for_participant(participant_id)
        try:
            acquire_thread_runtime_holder_or_raise(
                thread_id=thread_id,
                holder=holder,
                lease_store=self._ports.thread_runtime_lease_store,
            )
        except Exception as acquire_error:
            # A store call can commit and lose its return.  An exact point read
            # converts that case into confirmed success; otherwise preserve the
            # original fail-closed exception and install no logical source.
            try:
                lease = self._ports.thread_runtime_lease_store.load(thread_id)
            except Exception:
                # The effect is now ambiguous. Track retry authority even
                # though no logical source may be published to the caller.
                self._set_holder_presence(participant_id, thread_id, "unknown")
                raise acquire_error
            if lease is None or not any(
                self._same_runtime_holder_acquire_effect(candidate, holder)
                for candidate in lease.holders
            ):
                self._clear_holder_presence(participant_id, thread_id)
                raise acquire_error
            logger.warning(
                "Recovered committed fcodex runtime-holder acquire after lost response: thread=%s",
                thread_id[:12],
            )
        self._set_holder_presence(participant_id, thread_id, "confirmed")

    def _thread_has_source(self, participant_id: str, thread_id: str) -> bool:
        if (participant_id, thread_id) in self._unknown_sources:
            return True
        if any(
            candidate_participant_id == participant_id
            and candidate_thread_id == thread_id
            for (
                candidate_participant_id,
                _connection_id,
                candidate_thread_id,
            ) in self._connection_sources
        ):
            return True
        return any(
            source.participant_id == participant_id and source.thread_id == thread_id
            for source in self._pending_request_sources.values()
        )

    def _runtime_source_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                {
                    (participant_id, thread_id)
                    for participant_id, _connection_id, thread_id in (
                        self._connection_sources
                    )
                }
                | {
                    (source.participant_id, source.thread_id)
                    for source in self._pending_request_sources.values()
                }
                | set(self._unknown_sources)
            )
        )

    def _release_machine_holder_if_unneeded(
        self,
        participant_id: str,
        thread_id: str,
    ) -> bool:
        holder_presence = self._holder_presence(participant_id, thread_id)
        if holder_presence == "absent":
            self._retire_orphaned_participant_if_empty(participant_id)
            return True
        if self._thread_has_source(participant_id, thread_id):
            return True
        try:
            holder = self._ports.runtime_holder_for_participant(participant_id)
            runtime_lease = self._ports.thread_runtime_lease_store.load(thread_id)
        except Exception:
            logger.exception(
                "Unable to inspect fcodex runtime lease; retaining retry authority: thread=%s",
                thread_id[:12],
            )
            return False
        if runtime_lease is None or not any(
            self._same_runtime_holder_identity(candidate, holder)
            for candidate in runtime_lease.holders
        ):
            self._clear_holder_presence(participant_id, thread_id)
            self._retire_orphaned_participant_if_empty(participant_id)
            return True
        try:
            released = self._ports.thread_runtime_lease_store.release(
                thread_id,
                holder.holder_id,
            )
        except Exception:
            self._set_holder_presence(participant_id, thread_id, "unknown")
            logger.exception(
                "Failed to release fcodex runtime lease; retaining retry authority: thread=%s",
                thread_id[:12],
            )
            return False
        if not released:
            # The store did not confirm the effect. Keep retry authority, but
            # require a future replacement source to reacquire before commit.
            self._set_holder_presence(participant_id, thread_id, "unknown")
            logger.error(
                "Fcodex runtime lease release was not confirmed; retaining retry authority: thread=%s",
                thread_id[:12],
            )
            return False
        self._clear_holder_presence(participant_id, thread_id)
        self._retire_orphaned_participant_if_empty(participant_id)
        return True

    def _retire_orphaned_participant_if_empty(self, participant_id: str) -> None:
        participant = self._participants.get(participant_id)
        if (
            participant is None
            or participant.state != "orphaned"
            or participant.connection_ids
            or self._holder_presence_by_participant.get(participant_id)
        ):
            return
        if any(key[0] == participant_id for key in self._connection_sources):
            return
        if any(
            source.participant_id == participant_id
            for source in self._pending_request_sources.values()
        ):
            return
        if any(key[0] == participant_id for key in self._unknown_sources):
            return
        self._participants.pop(participant_id, None)
        self._holder_presence_by_participant.pop(participant_id, None)

    def _holder_presence(
        self,
        participant_id: str,
        thread_id: str,
    ) -> FcodexRuntimeHolderPresence:
        return self._holder_presence_by_participant.get(participant_id, {}).get(
            thread_id,
            "absent",
        )

    def _set_holder_presence(
        self,
        participant_id: str,
        thread_id: str,
        presence: Literal["confirmed", "unknown"],
    ) -> None:
        self._holder_presence_by_participant.setdefault(participant_id, {})[
            thread_id
        ] = presence

    def _clear_holder_presence(self, participant_id: str, thread_id: str) -> None:
        holder_presence = self._holder_presence_by_participant.get(participant_id)
        if holder_presence is None:
            return
        holder_presence.pop(thread_id, None)
        if not holder_presence:
            self._holder_presence_by_participant.pop(participant_id, None)

    @staticmethod
    def _same_runtime_holder_identity(
        candidate: ThreadRuntimeLeaseHolder,
        expected: ThreadRuntimeLeaseHolder,
    ) -> bool:
        """Compare immutable release authority, not only the holder label."""

        return bool(
            candidate.holder_id == expected.holder_id
            and candidate.holder_type == expected.holder_type
            and candidate.instance_name == expected.instance_name
            and candidate.owner_service_token == expected.owner_service_token
            and candidate.owner_pid == expected.owner_pid
            and candidate.owner_process_identity == expected.owner_process_identity
        )

    @classmethod
    def _same_runtime_holder_acquire_effect(
        cls,
        candidate: ThreadRuntimeLeaseHolder,
        expected: ThreadRuntimeLeaseHolder,
    ) -> bool:
        """Prove both immutable authority and this acquire's projection update."""

        return bool(
            cls._same_runtime_holder_identity(candidate, expected)
            and candidate.control_endpoint == expected.control_endpoint
            and candidate.backend_url == expected.backend_url
        )
