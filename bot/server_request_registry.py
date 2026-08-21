"""Process-local pending Codex server-request identities."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

from bot.server_request_contract import ServerRequestIdentity


ServerRequestRegistrationOutcome = Literal[
    "new",
    "replay",
    "resolved",
    "identity_conflict",
    "epoch_mismatch",
]
ServerRequestSettlementOutcome = Literal[
    "settled",
    "already_resolved",
    "missing",
    "identity_conflict",
    "invalid",
]
ServerRequestResponseAdmissionOutcome = Literal[
    "admitted",
    "not_pending",
    "identity_conflict",
    "processing",
    "submitted",
    "unknown",
    "revoked",
]
ServerRequestResponseFinishOutcome = Literal[
    "not_sent",
    "submitted",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ServerRequestRegistration:
    outcome: ServerRequestRegistrationOutcome
    identity: ServerRequestIdentity | None = None


@dataclass(frozen=True, slots=True)
class ServerRequestSettlement:
    outcome: ServerRequestSettlementOutcome
    identity: ServerRequestIdentity | None = None


@dataclass(frozen=True, slots=True)
class ServerRequestResponseAdmission:
    outcome: ServerRequestResponseAdmissionOutcome
    identity: ServerRequestIdentity | None = None


class ServerRequestRegistry:
    """Own pending identities for the current app-server connection epoch.

    Upstream app-server owns the actual callback lifecycle.  This registry is
    only Focus's process-local multi-surface projection: exact replay reuses
    the same immutable identity, first resolution removes it, and connection
    loss clears the epoch so a later ``thread/resume`` replay can rebuild it.
    """

    def __init__(self, *, resolved_limit: int) -> None:
        if int(resolved_limit) <= 0:
            raise ValueError("server-request resolved limit must be positive")
        self._resolved_limit = int(resolved_limit)
        self._connection_generation = 0
        self._pending: dict[str, ServerRequestIdentity] = {}
        self._dispatch_unknown: set[str] = set()
        self._response_status: dict[
            str,
            Literal["pending", "processing", "submitted", "unknown"],
        ] = {}
        # Response authority and response-effect phase are deliberately
        # separate facts.  A group deactivation can revoke every user-facing
        # capability for one exact callback while an already-started
        # fail-close write still finishes as submitted, unknown, or not-sent.
        self._response_revoked: set[str] = set()
        self._resolved: OrderedDict[str, ServerRequestIdentity] = OrderedDict()

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def activate_connection_epoch(self, connection_generation: int) -> None:
        generation = self._require_generation(connection_generation)
        if generation < self._connection_generation:
            raise RuntimeError("server-request connection generation moved backwards")
        if generation == self._connection_generation:
            return
        self._connection_generation = generation
        self._pending.clear()
        self._dispatch_unknown.clear()
        self._response_status.clear()
        self._response_revoked.clear()
        self._resolved.clear()

    def register(self, identity: ServerRequestIdentity) -> ServerRequestRegistration:
        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("server-request registration requires an identity")
        if identity.connection_generation != self._connection_generation:
            return ServerRequestRegistration("epoch_mismatch")
        request_key = identity.request_key
        if request_key in self._resolved:
            return ServerRequestRegistration(
                "resolved",
                self._resolved[request_key],
            )
        current = self._pending.get(request_key)
        if current is None:
            self._pending[request_key] = identity
            self._response_status[request_key] = "pending"
            return ServerRequestRegistration("new", identity)
        if current.same_identity_as(identity):
            return ServerRequestRegistration("replay", current)
        return ServerRequestRegistration("identity_conflict", current)

    def settle(
        self,
        request_key: str,
        *,
        thread_id: str,
    ) -> ServerRequestSettlement:
        key = str(request_key or "").strip()
        normalized_thread_id = str(thread_id or "").strip()
        if not key or not normalized_thread_id:
            return ServerRequestSettlement("invalid")
        current = self._pending.get(key)
        if current is None:
            resolved = self._resolved.get(key)
            if resolved is None:
                return ServerRequestSettlement("missing")
            if resolved.thread_id != normalized_thread_id:
                return ServerRequestSettlement("identity_conflict", resolved)
            return ServerRequestSettlement("already_resolved", resolved)
        if current.thread_id != normalized_thread_id:
            return ServerRequestSettlement("identity_conflict", current)
        self._pending.pop(key, None)
        self._dispatch_unknown.discard(key)
        self._response_status.pop(key, None)
        self._response_revoked.discard(key)
        self._remember_resolved(key, current)
        return ServerRequestSettlement("settled", current)

    def settle_identity(self, identity: ServerRequestIdentity) -> bool:
        if not isinstance(identity, ServerRequestIdentity):
            return False
        current = self._pending.get(identity.request_key)
        if current is not identity:
            return False
        self._pending.pop(identity.request_key, None)
        self._dispatch_unknown.discard(identity.request_key)
        self._response_status.pop(identity.request_key, None)
        self._response_revoked.discard(identity.request_key)
        self._remember_resolved(identity.request_key, identity)
        return True

    def settle_thread(self, thread_id: str) -> tuple[ServerRequestIdentity, ...]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return ()
        settled = tuple(
            identity
            for identity in self._pending.values()
            if identity.thread_id == normalized_thread_id
        )
        for identity in settled:
            self._pending.pop(identity.request_key, None)
            self._dispatch_unknown.discard(identity.request_key)
            self._response_status.pop(identity.request_key, None)
            self._response_revoked.discard(identity.request_key)
            self._remember_resolved(identity.request_key, identity)
        return settled

    def begin_response(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestResponseAdmission:
        """Atomically admit only the canonical pending identity."""

        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("server-request response requires an identity")
        current = self._pending.get(identity.request_key)
        if current is None:
            return ServerRequestResponseAdmission("not_pending")
        if current is not identity:
            return ServerRequestResponseAdmission("identity_conflict", current)
        if identity.request_key in self._response_revoked:
            return ServerRequestResponseAdmission("revoked", identity)
        status = self._response_status.get(identity.request_key)
        if status == "pending":
            self._response_status[identity.request_key] = "processing"
            return ServerRequestResponseAdmission("admitted", identity)
        if status in {"processing", "submitted", "unknown"}:
            return ServerRequestResponseAdmission(status, identity)
        raise RuntimeError("pending server request has no response status")

    def finish_response(
        self,
        identity: ServerRequestIdentity,
        *,
        outcome: ServerRequestResponseFinishOutcome,
    ) -> bool:
        """Close or release one previously admitted exact response attempt."""

        if not isinstance(identity, ServerRequestIdentity):
            return False
        if outcome not in {"not_sent", "submitted", "unknown"}:
            raise ValueError("invalid server-request response finish outcome")
        if self._pending.get(identity.request_key) is not identity:
            return False
        if self._response_status.get(identity.request_key) != "processing":
            return False
        next_status = "pending" if outcome == "not_sent" else outcome
        self._response_status[identity.request_key] = next_status
        return True

    def active_identity(self, request_key: str) -> ServerRequestIdentity | None:
        """Return the canonical object for a normalized request key."""

        return self._pending.get(str(request_key or "").strip())

    def revoke_response_authority(self, identity: ServerRequestIdentity) -> bool:
        """Revoke user response authority for one exact active capability.

        Revocation is local authority, not a claim that an upstream response
        was submitted.  Keeping it separate from ``_response_status`` also
        lets an already-admitted fail-close attempt record its real outcome.
        """

        if not isinstance(identity, ServerRequestIdentity):
            return False
        if self._pending.get(identity.request_key) is not identity:
            return False
        self._response_revoked.add(identity.request_key)
        return True

    def response_phase(
        self,
        identity: ServerRequestIdentity,
    ) -> Literal["pending", "processing", "submitted", "unknown"] | None:
        """Return the phase only while the exact canonical identity is active."""

        if not isinstance(identity, ServerRequestIdentity):
            return None
        if self._pending.get(identity.request_key) is not identity:
            return None
        status = self._response_status.get(identity.request_key)
        if status is None:
            raise RuntimeError("pending server request has no response status")
        return status

    def response_authority_is_revoked(
        self,
        identity: ServerRequestIdentity,
    ) -> bool:
        """Return exact local revocation without hiding response effect phase."""

        return bool(
            isinstance(identity, ServerRequestIdentity)
            and self._pending.get(identity.request_key) is identity
            and identity.request_key in self._response_revoked
        )

    def response_authority_is_open(
        self,
        identity: ServerRequestIdentity,
    ) -> bool:
        """Return whether one exact canonical response capability is usable."""

        return bool(
            isinstance(identity, ServerRequestIdentity)
            and self._pending.get(identity.request_key) is identity
            and identity.request_key not in self._response_revoked
        )

    def request_response_authority_is_revoked(self, request_key: str) -> bool:
        """Return current-epoch revocation for proxy-side replay suppression."""

        key = str(request_key or "").strip()
        return bool(key and key in self._pending and key in self._response_revoked)

    def resolved_identity(self, request_key: str) -> ServerRequestIdentity | None:
        """Return the exact current-epoch tombstone for a normalized key."""

        return self._resolved.get(str(request_key or "").strip())

    def mark_dispatch_unknown(self, identity: ServerRequestIdentity) -> bool:
        if self._pending.get(identity.request_key) is not identity:
            return False
        self._dispatch_unknown.add(identity.request_key)
        return True

    def dispatch_is_unknown(self, identity: ServerRequestIdentity) -> bool:
        return bool(
            self._pending.get(identity.request_key) is identity
            and identity.request_key in self._dispatch_unknown
        )

    def request_is_resolved(self, request_key: str) -> bool:
        return str(request_key or "").strip() in self._resolved

    def active_matches(self, identity: ServerRequestIdentity) -> bool:
        return bool(
            isinstance(identity, ServerRequestIdentity)
            and self._pending.get(identity.request_key) is identity
        )

    def pending_items(self) -> tuple[tuple[str, ServerRequestIdentity], ...]:
        return tuple(self._pending.items())

    def pending_count(self) -> int:
        return len(self._pending)

    def clear_connection_epoch(self) -> tuple[ServerRequestIdentity, ...]:
        pending = tuple(self._pending.values())
        self._pending.clear()
        self._dispatch_unknown.clear()
        self._response_status.clear()
        self._response_revoked.clear()
        self._resolved.clear()
        self._connection_generation = 0
        return pending

    def _remember_resolved(
        self,
        request_key: str,
        identity: ServerRequestIdentity,
    ) -> None:
        self._resolved[request_key] = identity
        self._resolved.move_to_end(request_key)
        while len(self._resolved) > self._resolved_limit:
            self._resolved.popitem(last=False)

    @staticmethod
    def _require_generation(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("server-request connection generation must be positive")
        return value
