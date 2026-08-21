"""Typed application contracts shared by Focus Web runtime owners."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseHolder,
)


def new_web_client_user_message_id() -> str:
    """Create the shared idempotency identity for a Web-authored user item."""

    return f"focus-web:{uuid.uuid4()}"


class WebRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class WebConnectedWriterReceipt:
    """Proof that one exact Web document owns the active main turn.

    This is descriptive proof for an immediate RuntimeLoop transaction, not
    a later settlement capability.  The full lease preserves the generation
    observed at admission; holder identity alone is insufficient across an
    A -> B -> A replacement.
    """

    client_id: str
    root_thread_id: str
    holder: InteractionLeaseHolder
    lease: InteractionLease


@dataclass(frozen=True, slots=True)
class WebTurnSubmissionReceipt:
    """Exact blank lease for one outbound exclusive Web turn action.

    The receipt is process-local compare-and-set authority for method-specific
    activation, a known-rejected submission, or cleanup when a preceding
    ``thread/resume`` is outcome-unknown before review or compact starts.
    Inline review may activate from its response identity; compact relies on
    shared lifecycle identity. This is not an ordinary-prompt, root-operation,
    or browser-liveness record.
    """

    client_id: str
    root_thread_id: str
    lease: InteractionLease


@dataclass(frozen=True, slots=True)
class WebAutonomousTurnReceipt:
    """Exact Web lease around a call that may autonomously start a turn.

    ``acquired`` distinguishes a fresh blank submission from an already-active
    turn owned by the same document. Known no-start outcomes and an
    outcome-unknown ``thread/resume`` may release only the fresh exact
    generation; lifecycle activation makes that comparison fail closed.
    """

    client_id: str
    root_thread_id: str
    lease: InteractionLease
    acquired: bool


class WebInteractionDeliveryDisposition(StrEnum):
    """Web routing result derived from the active-turn lease and document."""

    DECLINED = "declined"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class WebInteractionDeliveryDecision:
    """Typed, immediate decision for routing one app-server interaction.

    Raw interaction-lease and browser-document facts do not cross the owner.
    This decision is descriptive for the current RuntimeLoop transaction; it
    is not a takeover or settlement capability and must not be retained.
    """

    disposition: WebInteractionDeliveryDisposition
    client_id: str = ""
