"""Typed contract for RuntimeLoop-owned Feishu submission facts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeAlias

from bot.binding_runtime_contract import BindingOwnerRevisionReceipt
from bot.reason_codes import ReasonedCheck
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseAcquireResult,
    InteractionLeaseHolder,
)


ChatBindingKey: TypeAlias = tuple[str, str]


class FeishuRootOperationError(RuntimeError):
    """Base failure for Feishu root-operation ownership commands."""


class FeishuRootOperationPoisoned(FeishuRootOperationError):
    """The process cannot yet identify one submitted upstream outcome."""


class FeishuRootOperationTokenError(FeishuRootOperationError):
    """An operation token is forged, stale, or belongs to another owner."""


class FeishuRootOperationRetentionError(FeishuRootOperationError):
    """An exact submission transition was not confirmed."""


@dataclass(frozen=True, slots=True)
class FeishuRootOperationToken:
    """Opaque capability for one admitted outbound mutation.

    A controller accepts the token only while its registry contains this exact
    object. Copying or reconstructing its private values cannot forge authority.
    """

    _issuer_nonce: int
    _token_nonce: int


@dataclass(frozen=True, slots=True)
class FeishuRootContinuationToken:
    """Opaque receipt for one exact continuation-capable upstream send."""

    _issuer_nonce: int
    _token_nonce: int


@dataclass(frozen=True, slots=True)
class FeishuRootBackendEpochRetirementReceipt:
    """Process-local Feishu facts retired after the owned backend stopped."""

    root_thread_ids: tuple[str, ...]
    admission_count: int
    continuation_count: int
    interrupt_candidate_count: int


@dataclass(frozen=True, slots=True)
class FeishuPromptInterruptCandidateClaim:
    """One exact, process-local claim on an accepted prompt submission id."""

    turn_id: str
    _issuer_nonce: int
    _token: FeishuRootOperationToken
    _claim_nonce: int


@dataclass(frozen=True, slots=True)
class FeishuRootOperationSnapshot:
    """Immutable diagnostic projection of one root's submission facts."""

    root_thread_id: str
    pending_admission_count: int
    continuation_generations: tuple[int, ...]
    submission_outcome_unknown: bool = False
    submission_unknown_reason: str = ""
    local_holder_kind: str = ""
    local_holder_id: str = ""


@dataclass(frozen=True, slots=True)
class FeishuRootOperationPorts:
    """Required capabilities consumed by the Feishu submission owner."""

    verify_direct_thread_target: Callable[[str], object]
    prompt_write_admission: Callable[
        [ChatBindingKey, str, str, str], ReasonedCheck
    ]
    holder_for_binding: Callable[[ChatBindingKey], InteractionLeaseHolder]
    validate_binding_owner_receipt: Callable[[BindingOwnerRevisionReceipt], None]
    acquire_interaction_lease: Callable[
        [ChatBindingKey, str], InteractionLeaseAcquireResult
    ]
    release_exact_interaction_lease: Callable[[InteractionLease], bool]
    activate_interaction_turn: Callable[
        [InteractionLease, str], InteractionLease | None
    ]
    lookup_interaction_lease: Callable[[str], InteractionLease | None]
    read_root_status: Callable[[str], str]
