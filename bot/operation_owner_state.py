"""Process-local request records for fcodex proxy coordination.

Participant endpoint and runtime-source state belongs to
:mod:`bot.fcodex.participant_runtime_registry`; interaction state belongs to
:mod:`bot.fcodex.interaction_inbox`.  The records here are not persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.fcodex.operation_contract import (
    fcodex_request_can_have_unknown_root_mutation,
)
from bot.stores.interaction_lease_store import InteractionLease

if TYPE_CHECKING:
    from bot.fcodex.participant_runtime_registry import FcodexRequestSourceRef
    from bot.fcodex.thread_create_owner import FcodexThreadCreateResolution
    from bot.thread_create_transaction import ExternalThreadCreateAttempt


@dataclass(slots=True)
class FcodexClientRequest:
    request_key: str
    participant_id: str
    connection_id: str
    method: str
    thread_id: str
    root_thread_id: str
    # Service-lifetime monotonic capability returned by admission.  A reused
    # JSON-RPC id gets a new token, so a delayed response cannot settle the
    # replacement request once the proxy control protocol carries this field.
    request_token: int = 0
    # Exact Registry-issued capability for a pending thread/start or
    # thread/resume runtime source.  Request-key reconstruction is forbidden:
    # the Registry generation is part of the source identity.
    runtime_request_source: FcodexRequestSourceRef | None = None
    # Targetless thread/start receives a current-backend process-local
    # capability before this record is published. Once consumed, the
    # immutable resolution replaces it so a Registry tombstone retry cannot
    # replay the request or reinterpret a later wire response.
    external_create_attempt: ExternalThreadCreateAttempt | None = None
    external_create_resolution: FcodexThreadCreateResolution | None = None
    external_create_backend_epoch_invalidated: bool = False
    # Whether a persisted active/unknown goal may autonomously start a turn
    # after thread/resume.  This is classified before proxy dispatch.
    resume_may_autostart: bool = False
    # Goal/set and active-goal resume can acknowledge before their autonomous
    # turn starts; both use the same blank submission lease.
    continuation_risk: bool = False
    # Exclusive review/compact and autonomous goal/resume paths may use the
    # shared active-turn lease. Ordinary turn/start is tracked without one.
    turn_submission_lease: InteractionLease | None = None
    active_turn_id: str = ""

    @property
    def can_have_unknown_root_mutation(self) -> bool:
        return fcodex_request_can_have_unknown_root_mutation(
            self.method,
            continuation_risk=self.continuation_risk,
        )
