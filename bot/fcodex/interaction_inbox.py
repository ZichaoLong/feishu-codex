"""Service-owned fcodex server-request routing and settlement state.

The inbox is the sole process-local owner of pending fcodex interactions.
Operation and participant state stay behind a narrow read port so response
authority cannot be reconstructed from copied mutable maps.
"""

from __future__ import annotations

import copy
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.fcodex.interaction_contract import (
    FcodexFailCloseOutcome,
    fcodex_allow,
    fcodex_deny,
    fcodex_proxy_fail_close_action,
    fcodex_server_request_key,
    fcodex_service_fail_close_action,
    same_fcodex_server_request_identity,
)
from bot.interaction_contract import (
    INTERACTIVE_SERVER_REQUEST_METHODS,
    SHARED_APPROVAL_METHODS,
    fail_closed_interaction_response,
)
from bot.jsonrpc_id import jsonrpc_id_key
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestResponseAdmissionError,
    ServerRequestResponseSupersededError,
    ServerRequestRoutingMode,
)
from bot.stores.interaction_lease_store import InteractionLeaseHolder


@dataclass(frozen=True, slots=True)
class FcodexInteractionWriter:
    """Immutable active-main-turn writer facts for interaction routing."""

    participant_id: str
    connection_id: str
    holder: InteractionLeaseHolder
    connected: bool


class FcodexInteractionAuthority(Protocol):
    """Read port from interactions to exact active-main-turn authority."""

    def interaction_root_for_thread(self, thread_id: str) -> str: ...

    def interaction_writer_for_root(
        self,
        root_thread_id: str,
    ) -> FcodexInteractionWriter | None: ...

    def interaction_lease_holder_for_root(
        self,
        root_thread_id: str,
    ) -> InteractionLeaseHolder | None: ...

    def shared_interaction_request_is_eligible(
        self,
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool: ...

    def shared_interaction_endpoint_is_attached(
        self,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
    ) -> bool: ...

    def shared_interaction_has_live_recipient(
        self,
        root_thread_id: str,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class FcodexInteractionInboxPorts:
    """Explicit external queries and effects used by the inbox owner."""

    authority: FcodexInteractionAuthority
    server_request_is_resolved: Callable[[str], bool]
    server_request_response_authority_is_revoked: Callable[[str], bool]
    respond: Callable[..., None]
    schedule_proxy_delivery_expiry: Callable[[str, int, float], None]


@dataclass(frozen=True, slots=True)
class _DeferredResponseIntent:
    """One token-authorized response waiting for canonical socket authority."""

    result: dict[str, Any] | None
    error: dict[str, Any] | None
    deferred_state: str
    not_sent_state: str
    unknown_state: str
    submitted_state: str
    retry_on_explicit_replay: bool = False


@dataclass(slots=True)
class _ResolvedSharedInteractionCapability:
    """Bounded current-epoch receipt for a response arriving after resolution."""

    root_thread_id: str
    response_tokens: dict[tuple[str, str], str]


@dataclass(slots=True)
class _PendingInteraction:
    request_key: str
    request_id: int | float | str
    participant_id: str
    connection_id: str
    root_thread_id: str
    thread_id: str
    turn_id: str
    method: str
    params: dict[str, Any]
    state: str = "delivered"
    delivery_expiry_generation: int = 0
    canonical_identity: ServerRequestIdentity | None = None
    shared_interaction: bool = False
    backend_epoch: int = 0
    canonical_binding_open: bool = True
    response_authority_open: bool = True
    response_token: str = ""
    shared_response_tokens: dict[tuple[str, str], str] = field(default_factory=dict)
    deferred_response_intent: _DeferredResponseIntent | None = None

    @classmethod
    def from_envelope(
        cls,
        request_id: int | float | str,
        method: str,
        params: dict[str, Any],
        *,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
        backend_epoch: int,
        state: str = "delivered",
        delivery_expiry_generation: int = 0,
        canonical_identity: ServerRequestIdentity | None = None,
        shared_interaction: bool = False,
    ) -> _PendingInteraction:
        snapshot = copy.deepcopy(params or {})
        if canonical_identity is not None:
            request_id = canonical_identity.request_id
            method = canonical_identity.method
            snapshot = canonical_identity.params
        return cls(
            request_key=jsonrpc_id_key(request_id),
            request_id=request_id,
            participant_id=participant_id,
            connection_id=connection_id,
            root_thread_id=root_thread_id,
            thread_id=str(snapshot.get("threadId", "") or "").strip(),
            turn_id=str(snapshot.get("turnId", "") or "").strip(),
            method=str(method or "").strip(),
            params=snapshot,
            state=state,
            delivery_expiry_generation=delivery_expiry_generation,
            canonical_identity=canonical_identity,
            shared_interaction=shared_interaction,
            backend_epoch=backend_epoch,
            canonical_binding_open=canonical_identity is None,
            response_token=("" if shared_interaction else secrets.token_urlsafe(24)),
        )

    @classmethod
    def from_canonical(
        cls,
        identity: ServerRequestIdentity,
        *,
        participant_id: str,
        connection_id: str,
        root_thread_id: str,
        backend_epoch: int,
        state: str,
        delivery_expiry_generation: int = 0,
        shared_interaction: bool = False,
    ) -> _PendingInteraction:
        return cls.from_envelope(
            identity.request_id,
            identity.method,
            identity.params,
            participant_id=participant_id,
            connection_id=connection_id,
            root_thread_id=root_thread_id,
            backend_epoch=backend_epoch,
            state=state,
            delivery_expiry_generation=delivery_expiry_generation,
            canonical_identity=identity,
            shared_interaction=shared_interaction,
        )

    def epoch_is_open(self, backend_epoch: int) -> bool:
        return self.backend_epoch == backend_epoch and self.response_authority_open

    def bind_canonical_identity(
        self,
        identity: ServerRequestIdentity,
        *,
        backend_epoch: int,
        shared_interaction: bool,
    ) -> Literal["bound", "exact_replay", "identity_conflict"]:
        if not self.epoch_is_open(backend_epoch):
            return "identity_conflict"
        if self.canonical_identity is identity:
            if shared_interaction:
                self.promote_to_shared_interaction()
            return "exact_replay"
        params = identity.params
        if (
            self.canonical_identity is not None
            or not self.canonical_binding_open
            or self.request_key != identity.request_key
            or self.method != identity.method
            or self.thread_id != identity.thread_id
            or self.turn_id != identity.turn_id
            or self.params != params
        ):
            return "identity_conflict"
        self.canonical_identity = identity
        if shared_interaction:
            self.promote_to_shared_interaction()
        self.canonical_binding_open = False
        return "bound"

    def promote_to_shared_interaction(self) -> None:
        if self.shared_interaction:
            return
        if self.participant_id and self.connection_id and self.response_token:
            self.shared_response_tokens[
                (self.participant_id, self.connection_id)
            ] = self.response_token
        self.participant_id = ""
        self.connection_id = ""
        self.response_token = ""
        self.shared_interaction = True
        if self.state == "awaiting_proxy_delivery":
            self.state = "awaiting_shared_proxy_delivery"
            self.delivery_expiry_generation += 1
        elif self.state == "delivered":
            self.state = "shared_delivered"

    def issue_shared_response_token(
        self,
        participant_id: str,
        connection_id: str,
    ) -> tuple[str, bool]:
        if not self.shared_interaction:
            raise RuntimeError("shared response token requires a shared interaction")
        endpoint = (participant_id, connection_id)
        existing = self.shared_response_tokens.get(endpoint)
        if existing:
            return existing, False
        response_token = secrets.token_urlsafe(24)
        self.shared_response_tokens[endpoint] = response_token
        return response_token, True

    def shared_response_token_matches(
        self,
        participant_id: str,
        connection_id: str,
        response_token: str,
    ) -> bool:
        expected = self.shared_response_tokens.get(
            (str(participant_id or "").strip(), str(connection_id or "").strip())
        )
        return bool(
            expected
            and response_token
            and secrets.compare_digest(expected, str(response_token))
        )

    def drop_shared_response_token(
        self,
        participant_id: str,
        connection_id: str,
    ) -> bool:
        return (
            self.shared_response_tokens.pop(
                (
                    str(participant_id or "").strip(),
                    str(connection_id or "").strip(),
                ),
                None,
            )
            is not None
        )

    def close_backend_epoch(self) -> None:
        self.canonical_binding_open = False
        self.response_authority_open = False
        self.deferred_response_intent = None
        if self.state not in {
            "response_unknown",
            "response_submitted_unknown",
            "fail_close_unknown",
            "fail_close_submitted_unknown",
        }:
            self.state = "backend_epoch_closed"

    @property
    def connection_generation(self) -> int:
        identity = self.canonical_identity
        return identity.connection_generation if identity is not None else 0


@dataclass(frozen=True, slots=True)
class FcodexPendingInteractionSnapshot:
    """Detached interaction state for diagnostics and owner-focused tests."""

    request_key: str
    request_id: int | float | str
    participant_id: str
    connection_id: str
    root_thread_id: str
    thread_id: str
    turn_id: str
    method: str
    params: dict[str, Any]
    state: str
    delivery_expiry_generation: int
    canonical_bound: bool
    shared_interaction: bool
    backend_epoch: int
    canonical_binding_open: bool
    response_authority_open: bool
    response_intent_deferred: bool


class FcodexInteractionInbox:
    """Own fcodex interaction identity, delivery, response, and settlement."""

    def __init__(
        self,
        *,
        ports: FcodexInteractionInboxPorts,
        runtime_context_guard: Callable[[], None],
        proxy_delivery_timeout_seconds: float = 5.0,
        resolved_capability_limit: int = 256,
    ) -> None:
        required_capabilities = (
            runtime_context_guard,
            ports.authority.interaction_root_for_thread,
            ports.authority.interaction_writer_for_root,
            ports.authority.interaction_lease_holder_for_root,
            ports.authority.shared_interaction_request_is_eligible,
            ports.authority.shared_interaction_endpoint_is_attached,
            ports.authority.shared_interaction_has_live_recipient,
            ports.server_request_is_resolved,
            ports.server_request_response_authority_is_revoked,
            ports.respond,
            ports.schedule_proxy_delivery_expiry,
        )
        if any(not callable(capability) for capability in required_capabilities):
            raise TypeError("FcodexInteractionInbox 的 authority capability 必须全部可调用。")
        self._ports = ports
        self._authority = ports.authority
        self._runtime_context_guard = runtime_context_guard
        self._proxy_delivery_timeout_seconds = max(
            float(proxy_delivery_timeout_seconds),
            0.1,
        )
        self._pending_by_key: dict[str, _PendingInteraction] = {}
        self._resolved_shared_capabilities: dict[
            str, _ResolvedSharedInteractionCapability
        ] = {}
        self._resolved_capability_limit = max(int(resolved_capability_limit), 1)
        # A backend epoch is response authority, not operation lifecycle.
        self._backend_epoch = 0

    def proxy_request(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Route one proxy-observed server request to its exact writer."""

        self._runtime_context_guard()
        normalized_participant_id = str(participant_id or "").strip()
        normalized_connection_id = str(connection_id or "").strip()
        thread_id = str((params or {}).get("threadId", "") or "").strip()
        turn_id = str((params or {}).get("turnId", "") or "").strip()
        root_thread_id = self._authority.interaction_root_for_thread(thread_id)
        request_key = fcodex_server_request_key(request_id)
        if self._ports.server_request_is_resolved(request_key):
            return {"action": "suppress", "root_thread_id": root_thread_id}
        if self._ports.server_request_response_authority_is_revoked(request_key):
            return {
                "action": "suppress",
                "root_thread_id": root_thread_id,
                "reason": "server_request_response_authority_revoked",
            }
        existing = self._pending_by_key.get(request_key)
        approval_candidate = method in SHARED_APPROVAL_METHODS
        shared_interaction_eligible = bool(
            method in INTERACTIVE_SERVER_REQUEST_METHODS
            and root_thread_id
            and self._authority.shared_interaction_request_is_eligible(
                root_thread_id,
                thread_id,
                turn_id,
            )
        )

        # App-server broadcasts callbacks to every subscribed connection. One
        # canonical record owns response phase while each attached endpoint
        # receives an independent one-time projection capability.
        if existing is not None:
            if not same_fcodex_server_request_identity(
                existing,
                method=method,
                params=params,
            ):
                return {
                    "action": "suppress",
                    "root_thread_id": existing.root_thread_id,
                    "reason": "server_request_identity_conflict",
                }
            if not existing.epoch_is_open(self._backend_epoch):
                return {
                    "action": "suppress",
                    "root_thread_id": existing.root_thread_id,
                    "reason": "server_request_identity_conflict",
                }
            if existing.state in {
                "fail_close_deferred",
                "fail_close_not_sent",
                "fail_close_unknown",
                "fail_close_submitted_unknown",
            }:
                same_endpoint = bool(
                    existing.participant_id == normalized_participant_id
                    and existing.connection_id == normalized_connection_id
                )
                disposition = None
                if same_endpoint and existing.state in {
                    "fail_close_deferred",
                    "fail_close_not_sent",
                }:
                    disposition = self._flush_deferred_response(existing)
                route = {
                    "action": (
                        fcodex_proxy_fail_close_action(disposition or "deferred")
                        if same_endpoint and disposition is not None
                        else "deferred"
                        if same_endpoint
                        and existing.state
                        in {"fail_close_deferred", "fail_close_not_sent"}
                        else "suppress"
                    ),
                    "root_thread_id": existing.root_thread_id,
                }
                if disposition is not None:
                    route["response_disposition"] = disposition
                return route
            if existing.shared_interaction or (
                shared_interaction_eligible and existing.canonical_identity is None
            ):
                if (
                    not shared_interaction_eligible
                    or not root_thread_id
                    or existing.root_thread_id != root_thread_id
                    or not self._authority.shared_interaction_endpoint_is_attached(
                        normalized_participant_id,
                        normalized_connection_id,
                        root_thread_id,
                    )
                ):
                    return {"action": "suppress", "root_thread_id": root_thread_id}
                existing.promote_to_shared_interaction()
                if existing.state in {
                    "response_deferred",
                    "response_submitted_unknown",
                    "response_superseded",
                    "response_unknown",
                }:
                    return {"action": "suppress", "root_thread_id": root_thread_id}
                response_token, issued = existing.issue_shared_response_token(
                    normalized_participant_id,
                    normalized_connection_id,
                )
                if not issued:
                    return {"action": "suppress", "root_thread_id": root_thread_id}
                existing.state = "shared_delivered"
                return {
                    "action": "deliver",
                    "root_thread_id": root_thread_id,
                    "response_token": response_token,
                }
            if root_thread_id and existing.root_thread_id == root_thread_id:
                if (
                    existing.participant_id == normalized_participant_id
                    and existing.connection_id == normalized_connection_id
                ):
                    if existing.state in {
                        "awaiting_proxy_delivery",
                        "awaiting_shared_proxy_delivery",
                    }:
                        existing.state = "delivered"
                        existing.delivery_expiry_generation += 1
                        return {
                            "action": "deliver",
                            "root_thread_id": root_thread_id,
                            "response_token": existing.response_token,
                        }
                return {"action": "suppress", "root_thread_id": root_thread_id}
            return {"action": "suppress", "root_thread_id": existing.root_thread_id}

        if shared_interaction_eligible:
            if not self._authority.shared_interaction_endpoint_is_attached(
                normalized_participant_id,
                normalized_connection_id,
                root_thread_id,
            ):
                return {
                    "action": "suppress",
                    "root_thread_id": root_thread_id,
                    "reason": "interaction_endpoint_not_attached",
                }
            pending = _PendingInteraction.from_envelope(
                request_id,
                method,
                params,
                participant_id="",
                connection_id="",
                root_thread_id=root_thread_id,
                backend_epoch=self._backend_epoch,
                state="shared_delivered",
                shared_interaction=True,
            )
            response_token, _issued = pending.issue_shared_response_token(
                normalized_participant_id,
                normalized_connection_id,
            )
            self._pending_by_key[request_key] = pending
            return {
                "action": "deliver",
                "root_thread_id": root_thread_id,
                "response_token": response_token,
            }
        if not root_thread_id:
            outcome = self._fail_close(
                request_id=request_id,
                method=method,
                params=params,
                root_thread_id="",
                thread_id=thread_id,
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                note="Focus 无法确认该请求所属的 root 与可用 surface route，已自动取消。",
            )
            return {
                "action": fcodex_proxy_fail_close_action(outcome),
                "reason": "无法证明 server request 的 root relation。",
            }

        writer = self._authority.interaction_writer_for_root(root_thread_id)
        if writer is None:
            lease_holder = self._authority.interaction_lease_holder_for_root(
                root_thread_id
            )
            if lease_holder is not None and lease_holder.kind in {"web", "feishu"}:
                return {"action": "suppress", "root_thread_id": root_thread_id}
            if approval_candidate and lease_holder is not None:
                return {
                    "action": "suppress",
                    "root_thread_id": root_thread_id,
                    "reason": "awaiting_canonical_approval_route",
                }
            outcome = self._fail_close(
                request_id=request_id,
                method=method,
                params=params,
                root_thread_id=root_thread_id,
                thread_id=thread_id,
                participant_id=normalized_participant_id,
                connection_id=normalized_connection_id,
                note="当前没有可用的 fcodex active main-turn writer，Focus 已自动取消该请求。",
            )
            return {
                "action": fcodex_proxy_fail_close_action(outcome),
                "root_thread_id": root_thread_id,
            }
        if not writer.connected:
            if approval_candidate:
                return {
                    "action": "suppress",
                    "root_thread_id": root_thread_id,
                    "reason": "awaiting_canonical_approval_route",
                }
            outcome = self._fail_close(
                request_id=request_id,
                method=method,
                params=params,
                root_thread_id=root_thread_id,
                thread_id=thread_id,
                participant_id=writer.participant_id,
                connection_id=normalized_connection_id,
                note="fcodex active-turn writer 已断线或当前连接不匹配。",
            )
            return {
                "action": fcodex_proxy_fail_close_action(outcome),
                "root_thread_id": root_thread_id,
            }
        if (
            writer.participant_id != normalized_participant_id
            or writer.connection_id != normalized_connection_id
        ):
            return {"action": "suppress", "root_thread_id": root_thread_id}

        pending = _PendingInteraction.from_envelope(
            request_id,
            method,
            params,
            participant_id=writer.participant_id,
            connection_id=writer.connection_id,
            root_thread_id=root_thread_id,
            backend_epoch=self._backend_epoch,
        )
        self._pending_by_key[request_key] = pending
        return {
            "action": "deliver",
            "root_thread_id": root_thread_id,
            "response_token": pending.response_token,
        }

    def service_request(
        self,
        identity: ServerRequestIdentity,
        *,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> dict[str, Any]:
        """Handle one registry-canonical fcodex request object."""

        self._runtime_context_guard()
        if not isinstance(identity, ServerRequestIdentity):
            return {"handled": False}
        request_id = identity.request_id
        request_key = identity.request_key
        method = identity.method
        params = identity.params
        thread_id = identity.thread_id
        root_thread_id = self._authority.interaction_root_for_thread(thread_id)
        shared_interaction = routing_mode in {
            "shared_approval",
            "shared_interaction",
        }
        if routing_mode == "shared_approval" and method not in SHARED_APPROVAL_METHODS:
            return {"handled": False}
        if (
            routing_mode == "shared_interaction"
            and (
                method in SHARED_APPROVAL_METHODS
                or method not in INTERACTIVE_SERVER_REQUEST_METHODS
            )
        ):
            return {"handled": False}
        existing = self._pending_by_key.get(request_key)
        if (
            shared_interaction
            and existing is not None
            and existing.canonical_identity is None
            and existing.state
            in {
                "fail_close_deferred",
                "fail_close_not_sent",
                "fail_close_unknown",
                "fail_close_submitted_unknown",
            }
        ):
            self._pending_by_key.pop(request_key, None)
            existing.canonical_binding_open = False
            existing.response_authority_open = False
            existing.deferred_response_intent = None
            existing.state = "shared_route_replaced_fail_close"
            existing = None
        if shared_interaction and not self._authority.shared_interaction_request_is_eligible(
            root_thread_id, thread_id, identity.turn_id
        ):
            return {"handled": False}
        if (
            routing_mode == "shared_interaction"
            and not self._authority.shared_interaction_has_live_recipient(
                root_thread_id
            )
        ):
            return {"handled": False}
        if existing is not None:
            binding = existing.bind_canonical_identity(
                identity,
                backend_epoch=self._backend_epoch,
                shared_interaction=shared_interaction,
            )
            if binding == "identity_conflict":
                return {
                    "handled": False,
                    "action": "suppress",
                    "root_thread_id": existing.root_thread_id or root_thread_id,
                    "reason": "server_request_identity_conflict",
                }
            disposition = self._flush_deferred_response(existing)
            route = {
                "handled": True,
                "action": "suppress",
                "root_thread_id": existing.root_thread_id or root_thread_id,
            }
            if disposition is not None:
                route["response_disposition"] = disposition
            return route
        if self._ports.server_request_is_resolved(request_key):
            return {
                "handled": True,
                "action": "suppress",
                "root_thread_id": root_thread_id,
            }
        if not root_thread_id:
            return {"handled": False}

        if shared_interaction:
            pending = _PendingInteraction.from_canonical(
                identity,
                participant_id="",
                connection_id="",
                root_thread_id=root_thread_id,
                state="awaiting_shared_proxy_delivery",
                backend_epoch=self._backend_epoch,
                shared_interaction=True,
            )
            self._pending_by_key[request_key] = pending
            return {
                "handled": True,
                "action": "suppress",
                "root_thread_id": root_thread_id,
            }

        writer = self._authority.interaction_writer_for_root(root_thread_id)
        if writer is not None:
            if writer.connected:
                pending = _PendingInteraction.from_canonical(
                    identity,
                    participant_id=writer.participant_id,
                    connection_id=writer.connection_id,
                    root_thread_id=root_thread_id,
                    state="awaiting_proxy_delivery",
                    delivery_expiry_generation=1,
                    backend_epoch=self._backend_epoch,
                    shared_interaction=False,
                )
                self._pending_by_key[request_key] = pending
                self._ports.schedule_proxy_delivery_expiry(
                    request_key,
                    pending.delivery_expiry_generation,
                    self._proxy_delivery_timeout_seconds,
                )
                return {
                    "handled": True,
                    "action": "suppress",
                    "root_thread_id": root_thread_id,
                }
            outcome = self._fail_close(
                request_id=request_id,
                method=method,
                params=params,
                root_thread_id=root_thread_id,
                thread_id=thread_id,
                participant_id=writer.participant_id,
                connection_id=writer.connection_id,
                note="fcodex writer 已断线，Focus 已自动取消该请求。",
                canonical_identity=identity,
            )
            return {
                "handled": True,
                "action": fcodex_service_fail_close_action(outcome),
                "root_thread_id": root_thread_id,
            }

        lease_holder = self._authority.interaction_lease_holder_for_root(
            root_thread_id
        )
        if lease_holder is None or lease_holder.kind != "fcodex":
            return {"handled": False}
        outcome = self._fail_close(
            request_id=request_id,
            method=method,
            params=params,
            root_thread_id=root_thread_id,
            thread_id=thread_id,
            participant_id=str(lease_holder.holder_id or "fcodex:unknown:stale"),
            connection_id="",
            note="Focus 无法确认 fcodex active main-turn writer，已自动取消该请求。",
            canonical_identity=identity,
        )
        return {
            "handled": True,
            "action": fcodex_service_fail_close_action(outcome),
            "root_thread_id": root_thread_id,
        }

    def has_pending_for_root(self, root_thread_id: str) -> bool:
        self._runtime_context_guard()
        normalized_root_id = str(root_thread_id or "").strip()
        return bool(
            normalized_root_id
            and any(
                pending.root_thread_id == normalized_root_id
                for pending in self._pending_by_key.values()
            )
        )

    def pending_count(self) -> int:
        """Return the number of fcodex-local interaction capabilities."""

        self._runtime_context_guard()
        return len(self._pending_by_key)

    def pending_snapshot(
        self,
        request_key: str,
    ) -> FcodexPendingInteractionSnapshot | None:
        """Return one detached pending record without exposing mutable state."""

        self._runtime_context_guard()
        pending = self._pending_by_key.get(str(request_key or ""))
        if pending is None:
            return None
        return FcodexPendingInteractionSnapshot(
            request_key=pending.request_key,
            request_id=pending.request_id,
            participant_id=pending.participant_id,
            connection_id=pending.connection_id,
            root_thread_id=pending.root_thread_id,
            thread_id=pending.thread_id,
            turn_id=pending.turn_id,
            method=pending.method,
            params=copy.deepcopy(pending.params),
            state=pending.state,
            delivery_expiry_generation=pending.delivery_expiry_generation,
            canonical_bound=pending.canonical_identity is not None,
            shared_interaction=pending.shared_interaction,
            backend_epoch=pending.backend_epoch,
            canonical_binding_open=pending.canonical_binding_open,
            response_authority_open=pending.response_authority_open,
            response_intent_deferred=pending.deferred_response_intent is not None,
        )

    def request_keys_for_root(self, root_thread_id: str) -> set[str]:
        self._runtime_context_guard()
        normalized_root_id = str(root_thread_id or "").strip()
        if not normalized_root_id:
            return set()
        return {
            request_key
            for request_key, pending in self._pending_by_key.items()
            if pending.root_thread_id == normalized_root_id
        }

    def remove_resolved(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        self._runtime_context_guard()
        if not isinstance(identity, ServerRequestIdentity):
            return ServerRequestLocalRemoval("invalid")
        request_key = identity.request_key
        thread_id = identity.thread_id
        if not thread_id:
            return ServerRequestLocalRemoval("invalid", request_key=request_key)
        if not self._ports.server_request_is_resolved(request_key):
            return ServerRequestLocalRemoval(
                "not_resolved",
                request_key=request_key,
                thread_id=thread_id,
            )
        pending = self._pending_by_key.get(request_key)
        if pending is None:
            return ServerRequestLocalRemoval(
                "missing",
                request_key=request_key,
                thread_id=thread_id,
            )
        if pending.canonical_identity is not identity:
            return ServerRequestLocalRemoval(
                "mismatch",
                request_key=request_key,
                thread_id=thread_id,
            )
        self._pending_by_key.pop(request_key, None)
        if pending.shared_interaction and pending.shared_response_tokens:
            self._remember_resolved_shared_capability(pending)
        root_thread_id = pending.root_thread_id
        return ServerRequestLocalRemoval(
            "removed",
            request_key=request_key,
            thread_id=thread_id,
            root_thread_id=root_thread_id,
        )

    def response_admit(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        pending = self._pending_response_for_owner(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=request_id,
            response_token=response_token,
        )
        if pending is None:
            return fcodex_deny("当前 fcodex 不再有权回答该 server request。")
        pending.state = "response_admitted"
        return fcodex_allow(root_thread_id=pending.root_thread_id)

    def response_submit(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Atomically admit and submit through the service adapter."""

        self._runtime_context_guard()
        if (result is None) == (error is None):
            return fcodex_deny("fcodex server request response 必须恰有 result 或 error。")
        if result is not None and not isinstance(result, dict):
            return fcodex_deny("fcodex server request result 必须是 object。")
        if error is not None and not isinstance(error, dict):
            return fcodex_deny("fcodex server request error 必须是 object。")
        request_key = fcodex_server_request_key(request_id)
        pending_candidate = self._pending_by_key.get(request_key)
        if pending_candidate is None:
            resolved = self._resolved_shared_response_receipt(
                request_key,
                participant_id=participant_id,
                connection_id=connection_id,
                response_token=response_token,
            )
            if resolved is not None:
                return resolved
        elif pending_candidate.shared_interaction and pending_candidate.shared_response_token_matches(
            participant_id,
            connection_id,
            response_token,
        ):
            if pending_candidate.state in {
                "response_deferred",
                "response_submitted_unknown",
                "response_superseded",
            }:
                receipt = fcodex_allow(
                    root_thread_id=pending_candidate.root_thread_id
                )
                receipt["response_disposition"] = "superseded"
                return receipt
            if pending_candidate.state == "response_unknown":
                return {
                    "allowed": False,
                    "reason": "Focus 无法确认该回答是否已提交；不会自动重放。",
                    "response_disposition": "unknown",
                }
        pending = self._pending_response_for_owner(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=request_id,
            response_token=response_token,
        )
        if pending is None:
            return fcodex_deny("当前 fcodex 不再有权回答该 server request。")
        if pending.canonical_identity is None:
            return {
                "allowed": False,
                "reason": "Focus 尚未取得 canonical response generation；请求会重新显示。",
                "response_disposition": "not_sent",
            }
        disposition = self._accept_response_intent(
            pending,
            result=result,
            error=error,
            deferred_state="response_deferred",
            not_sent_state="response_not_sent",
            unknown_state="response_unknown",
            submitted_state="response_submitted_unknown",
        )
        if disposition in {"deferred", "submitted", "superseded"}:
            receipt = fcodex_allow(root_thread_id=pending.root_thread_id)
            receipt["response_disposition"] = disposition
            return receipt
        if disposition == "not_sent":
            return {
                "allowed": False,
                "reason": "Focus 确认该回答尚未提交；exact response capability 仍可重试。",
                "response_disposition": disposition,
            }
        return {
            "allowed": False,
            "reason": "Focus 无法确认该回答是否已提交；不会自动重放。",
            "response_disposition": disposition,
        }

    def response_invalid(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        pending = self._pending_response_for_owner(
            participant_id=participant_id,
            connection_id=connection_id,
            request_id=request_id,
            response_token=response_token,
        )
        if pending is None:
            return {"action": "suppress"}
        if pending.shared_interaction or (
            pending.method in SHARED_APPROVAL_METHODS
            and pending.canonical_identity is None
        ):
            if pending.shared_interaction:
                pending.drop_shared_response_token(participant_id, connection_id)
            else:
                self._pending_by_key.pop(pending.request_key, None)
                pending.response_authority_open = False
            return {"action": "suppress"}
        result, error = fail_closed_interaction_response(
            pending.method,
            pending.params,
            message="fcodex 提交了无效的交互响应，Focus 已自动取消该请求。",
        )
        disposition = self._accept_response_intent(
            pending,
            result=result,
            error=error,
            deferred_state="fail_close_deferred",
            not_sent_state="fail_close_not_sent",
            unknown_state="fail_close_unknown",
            submitted_state="fail_close_submitted_unknown",
            retry_on_explicit_replay=True,
        )
        action = {
            "deferred": "deferred",
            "not_sent": "deferred",
            "submitted": "fail_closed",
            "superseded": "suppress",
            "unknown": "suppress",
        }[disposition]
        return {"action": action, "response_disposition": disposition}

    def response_sent(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
    ) -> None:
        self._runtime_context_guard()
        pending = self._pending_by_key.get(fcodex_server_request_key(request_id))
        if (
            pending is None
            or pending.state != "response_admitted"
            or pending.connection_generation <= 0
            or not self._is_same_open_owner(
                pending,
                participant_id=participant_id,
                connection_id=connection_id,
                response_token=response_token,
            )
        ):
            return
        pending.state = "response_submitted_unknown"

    def response_unknown(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
    ) -> None:
        self._runtime_context_guard()
        pending = self._pending_by_key.get(fcodex_server_request_key(request_id))
        if (
            pending is None
            or pending.state != "response_admitted"
            or pending.connection_generation <= 0
            or not self._is_same_open_owner(
                pending,
                participant_id=participant_id,
                connection_id=connection_id,
                response_token=response_token,
            )
        ):
            return
        pending.state = "response_unknown"

    def expire_proxy_delivery(self, request_key: str, expiry_generation: int) -> None:
        self._runtime_context_guard()
        pending = self._pending_by_key.get(str(request_key or ""))
        if pending is None or pending.delivery_expiry_generation != int(
            expiry_generation
        ):
            return
        if pending.state != "awaiting_proxy_delivery":
            return
        self._fail_close(
            request_id=pending.request_id,
            method=pending.method,
            params=pending.params,
            root_thread_id=pending.root_thread_id,
            thread_id=pending.thread_id,
            participant_id=pending.participant_id,
            connection_id=pending.connection_id,
            note="fcodex writer 未收到该交互请求，Focus 已自动取消。",
            existing=pending,
        )

    def drop_delivered(
        self,
        participant_id: str,
        *,
        connection_id: str | None = None,
    ) -> int:
        """Retire one disconnected proxy's process-local action capabilities."""

        self._runtime_context_guard()
        count = 0
        for request_key, pending in tuple(self._pending_by_key.items()):
            if pending.shared_interaction:
                if connection_id is not None and pending.drop_shared_response_token(
                    participant_id,
                    connection_id,
                ):
                    count += 1
                continue
            if (
                pending.participant_id != participant_id
                or (connection_id is not None and pending.connection_id != connection_id)
            ):
                continue
            self._pending_by_key.pop(request_key, None)
            count += 1
        if connection_id is not None:
            endpoint = (str(participant_id or "").strip(), str(connection_id or "").strip())
            for resolved in self._resolved_shared_capabilities.values():
                if resolved.response_tokens.pop(endpoint, None) is not None:
                    count += 1
        return count

    def backend_disconnected(self) -> None:
        """Drop old-connection capabilities; replay rebuilds fresh state."""

        self._runtime_context_guard()
        self._backend_epoch += 1
        self._pending_by_key.clear()
        self._resolved_shared_capabilities.clear()

    def settle_backend_epoch_after_stop(self) -> tuple[str, ...]:
        """Forget response-ineligible local copies after backend termination."""

        self._runtime_context_guard()
        request_keys = tuple(sorted(self._pending_by_key))
        self._pending_by_key.clear()
        self._resolved_shared_capabilities.clear()
        return request_keys

    def _accept_response_intent(
        self,
        pending: _PendingInteraction,
        *,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        deferred_state: str,
        not_sent_state: str,
        unknown_state: str,
        submitted_state: str,
        retry_on_explicit_replay: bool = False,
    ) -> Literal["deferred", "submitted", "superseded", "not_sent", "unknown"]:
        """Consume one public capability before socket generation may exist."""

        pending.deferred_response_intent = _DeferredResponseIntent(
            result=copy.deepcopy(result),
            error=copy.deepcopy(error),
            deferred_state=deferred_state,
            not_sent_state=not_sent_state,
            unknown_state=unknown_state,
            submitted_state=submitted_state,
            retry_on_explicit_replay=retry_on_explicit_replay,
        )
        pending.state = deferred_state
        disposition = self._flush_deferred_response(pending)
        return disposition or "deferred"

    def _flush_deferred_response(
        self,
        pending: _PendingInteraction,
    ) -> Literal[
        "deferred",
        "submitted",
        "superseded",
        "not_sent",
        "unknown",
    ] | None:
        """Submit a token-authorized intent only after canonical generation binds."""

        intent = pending.deferred_response_intent
        if intent is None:
            return None
        if not pending.epoch_is_open(self._backend_epoch):
            pending.deferred_response_intent = None
            pending.state = "backend_epoch_closed"
            return "unknown"
        identity = pending.canonical_identity
        if identity is None:
            pending.state = intent.deferred_state
            return "deferred"
        try:
            self._ports.respond(
                identity,
                result=intent.result,
                error=intent.error,
            )
        except ServerRequestResponseSupersededError:
            pending.state = "response_superseded"
            disposition = "superseded"
        except CodexRpcPreSendError:
            pending.state = intent.not_sent_state
            disposition = "not_sent"
        except ServerRequestResponseAdmissionError:
            pending.state = intent.unknown_state
            disposition = "unknown"
        except Exception:
            pending.state = intent.unknown_state
            disposition = "unknown"
        else:
            pending.state = intent.submitted_state
            disposition = "submitted"
        # An automatic fail-close has no user endpoint which can click again.
        # Keep only that exact intent after a proven pre-send failure so an
        # upstream replay can retry it. User-authored responses are instead
        # re-presented and require another explicit action.
        if not (
            disposition == "not_sent"
            and intent.retry_on_explicit_replay
        ):
            pending.deferred_response_intent = None
        return disposition

    def _pending_response_for_owner(
        self,
        *,
        participant_id: str,
        connection_id: str,
        request_id: Any,
        response_token: str,
    ) -> _PendingInteraction | None:
        pending = self._pending_by_key.get(fcodex_server_request_key(request_id))
        if pending is None:
            return None
        if pending.shared_interaction:
            if pending.state not in {
                "awaiting_shared_proxy_delivery",
                "shared_delivered",
                "response_not_sent",
                "response_admitted",
            }:
                return None
            if (
                not pending.shared_response_token_matches(
                    participant_id,
                    connection_id,
                    response_token,
                )
                or not self._authority.shared_interaction_request_is_eligible(
                    pending.root_thread_id,
                    pending.thread_id,
                    pending.turn_id,
                )
                or not self._authority.shared_interaction_endpoint_is_attached(
                    participant_id,
                    connection_id,
                    pending.root_thread_id,
                )
            ):
                return None
            return pending
        if pending.state not in {
            "delivered",
            "response_admitted",
            "response_not_sent",
        }:
            return None
        writer = self._authority.interaction_writer_for_root(pending.root_thread_id)
        if (
            writer is None
            or not writer.connected
            or writer.participant_id != pending.participant_id
            or writer.connection_id != pending.connection_id
            or not self._is_same_open_owner(
                pending,
                participant_id=participant_id,
                connection_id=connection_id,
                response_token=response_token,
            )
        ):
            return None
        return pending

    def _is_same_open_owner(
        self,
        pending: _PendingInteraction,
        *,
        participant_id: str,
        connection_id: str,
        response_token: str,
    ) -> bool:
        if pending.shared_interaction:
            return bool(
                pending.epoch_is_open(self._backend_epoch)
                and pending.shared_response_token_matches(
                    participant_id,
                    connection_id,
                    response_token,
                )
            )
        return (
            pending.participant_id == str(participant_id or "").strip()
            and pending.connection_id == str(connection_id or "").strip()
            and pending.epoch_is_open(self._backend_epoch)
            and bool(response_token)
            and secrets.compare_digest(pending.response_token, str(response_token))
        )

    def _remember_resolved_shared_capability(
        self,
        pending: _PendingInteraction,
    ) -> None:
        self._resolved_shared_capabilities[pending.request_key] = (
            _ResolvedSharedInteractionCapability(
                root_thread_id=pending.root_thread_id,
                response_tokens=dict(pending.shared_response_tokens),
            )
        )
        while len(self._resolved_shared_capabilities) > self._resolved_capability_limit:
            oldest = next(iter(self._resolved_shared_capabilities))
            self._resolved_shared_capabilities.pop(oldest, None)

    def _resolved_shared_response_receipt(
        self,
        request_key: str,
        *,
        participant_id: str,
        connection_id: str,
        response_token: str,
    ) -> dict[str, Any] | None:
        resolved = self._resolved_shared_capabilities.get(request_key)
        if resolved is None:
            return None
        expected = resolved.response_tokens.get(
            (str(participant_id or "").strip(), str(connection_id or "").strip())
        )
        if not (
            expected
            and response_token
            and secrets.compare_digest(expected, str(response_token))
        ):
            return None
        receipt = fcodex_allow(root_thread_id=resolved.root_thread_id)
        receipt["response_disposition"] = "superseded"
        return receipt

    def _fail_close(
        self,
        *,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
        root_thread_id: str,
        thread_id: str,
        participant_id: str,
        connection_id: str,
        note: str,
        canonical_identity: ServerRequestIdentity | None = None,
        existing: _PendingInteraction | None = None,
    ) -> FcodexFailCloseOutcome:
        request_key = fcodex_server_request_key(request_id)
        pending = existing or self._pending_by_key.get(request_key)
        if (
            pending is not None
            and pending.state == "fail_close_deferred"
            and pending.deferred_response_intent is not None
        ):
            return "deferred"
        if pending is not None and pending.state in {
            "fail_close_unknown",
            "fail_close_submitted_unknown",
            "response_unknown",
            "response_submitted_unknown",
        }:
            return "submitted" if pending.state.endswith("submitted_unknown") else "unknown"
        if pending is not None and pending.state == "response_superseded":
            return "superseded"
        if pending is not None and not pending.epoch_is_open(self._backend_epoch):
            pending.state = "backend_epoch_closed"
            return "unknown"
        if pending is None:
            pending = _PendingInteraction.from_envelope(
                request_id,
                method,
                params,
                participant_id=participant_id,
                connection_id=connection_id,
                root_thread_id=root_thread_id,
                backend_epoch=self._backend_epoch,
                state="fail_close_not_sent",
                canonical_identity=canonical_identity,
            )
            self._pending_by_key[request_key] = pending
        result, error = fail_closed_interaction_response(method, params, message=note)
        return self._accept_response_intent(
            pending,
            result=result,
            error=error,
            deferred_state="fail_close_deferred",
            not_sent_state="fail_close_not_sent",
            unknown_state="fail_close_unknown",
            submitted_state="fail_close_submitted_unknown",
            retry_on_explicit_replay=True,
        )
