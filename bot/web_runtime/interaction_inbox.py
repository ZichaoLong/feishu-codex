"""RuntimeLoop-owned Web interaction delivery and response state."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.interaction_contract import (
    MCP_ELICITATION,
    SHARED_APPROVAL_METHODS,
    USER_INPUT,
    fail_closed_interaction_response,
    interaction_response_payload,
)
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.runtime_loop import RuntimeContextGuard
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestResponseSupersededError,
)


logger = logging.getLogger(__name__)

WebInteractionStatus = Literal[
    "pending",
    "processing",
    "submitted",
    "unknown",
]
WebInteractionDeliveryScope = Literal[
    "writer_interaction",
    "shared_interaction",
]

_SHARED_WEB_INTERACTION_METHODS = SHARED_APPROVAL_METHODS | {
    USER_INPUT,
    MCP_ELICITATION,
}


@dataclass(frozen=True, slots=True)
class WebInteractionInboxPorts:
    respond: Callable[..., None]
    active_matches: Callable[[ServerRequestIdentity], bool]


@dataclass(frozen=True, slots=True)
class WebInteractionChange:
    root_thread_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class WebInteractionMutation:
    changes: tuple[WebInteractionChange, ...] = ()


@dataclass(frozen=True, slots=True)
class WebPendingInteractionSnapshot:
    """Detached state safe for controller composition and projection."""

    request_key: str
    connection_generation: int
    response_capability: str
    method: str
    params: dict[str, Any]
    thread_id: str
    owner_thread_id: str
    turn_id: str
    client_id: str
    delivery_scope: WebInteractionDeliveryScope
    status: WebInteractionStatus
    hidden: bool
    lifecycle_closed: str
    auto_resolution_backend_epoch: int
    auto_resolution_visible_at_ms: int
    auto_resolution_due_at_ms: int

    def projection_dict(self) -> dict[str, Any]:
        return {
            "request_key": self.request_key,
            "connection_generation": self.connection_generation,
            "response_capability": self.response_capability,
            "method": self.method,
            "params": dict(self.params),
            "thread_id": self.thread_id,
            "owner_thread_id": self.owner_thread_id,
            "turn_id": self.turn_id,
            "client_id": self.client_id,
            "delivery_scope": self.delivery_scope,
            "status": self.status,
            "hidden": self.hidden,
            "lifecycle_closed": self.lifecycle_closed,
            "auto_resolution_backend_epoch": self.auto_resolution_backend_epoch,
            "auto_resolution_visible_at_ms": self.auto_resolution_visible_at_ms,
            "auto_resolution_due_at_ms": self.auto_resolution_due_at_ms,
        }


IngressDisposition = Literal[
    "route",
    "consumed",
    "identity_conflict",
]


@dataclass(frozen=True, slots=True)
class WebInteractionIngress:
    identity: ServerRequestIdentity
    disposition: IngressDisposition
    owner_thread_id_hint: str = ""
    changes: tuple[WebInteractionChange, ...] = ()
    _expected: _PendingWebInteraction | None = None
    _issuer: object | None = None


@dataclass(frozen=True, slots=True)
class WebInteractionResponsePreparation:
    request_key: str
    connection_generation: int
    response_capability: str
    root_thread_id: str
    thread_id: str
    turn_id: str
    delivery_scope: WebInteractionDeliveryScope
    _expected: _PendingWebInteraction
    _issuer: object


@dataclass(frozen=True, slots=True)
class WebInteractionSubmission:
    request_key: str
    status: Literal["submitted"] = "submitted"
    changes: tuple[WebInteractionChange, ...] = ()


@dataclass(frozen=True, slots=True)
class WebInteractionResolution:
    outcome: Literal["missing", "mismatch", "not_resolved", "resolved"]
    request_key: str
    owner_thread_id: str = ""
    thread_id: str = ""
    changes: tuple[WebInteractionChange, ...] = ()


@dataclass(frozen=True, slots=True)
class WebInteractionBackendEpochRetirement:
    """Exact Web capabilities retired only after machine stop proof."""

    request_keys: frozenset[str]
    changes: tuple[WebInteractionChange, ...] = ()

    @property
    def count(self) -> int:
        return len(self.request_keys)


@dataclass(frozen=True, slots=True)
class WebAutoResolutionPreparation:
    outcome: Literal["missing", "recognized", "ready"]
    response: WebInteractionResponsePreparation | None = None


class WebInteractionInboxError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 400,
        changes: tuple[WebInteractionChange, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.changes = changes


@dataclass(slots=True)
class _PendingWebInteraction:
    """Surface-local state referencing the canonical registry identity."""

    identity: ServerRequestIdentity
    owner_thread_id: str
    client_id: str
    response_capability: str
    delivery_scope: WebInteractionDeliveryScope
    status: WebInteractionStatus = "pending"
    hidden: bool = False
    lifecycle_closed: str = ""
    auto_resolution_timing: AutoResolutionTiming | None = None


class WebInteractionInbox:
    """Single owner of Web-local pending interaction state."""

    def __init__(
        self,
        *,
        ports: WebInteractionInboxPorts,
        runtime_context_guard: RuntimeContextGuard,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._monotonic = monotonic
        self._pending_by_key: dict[str, _PendingWebInteraction] = {}
        self._issuer = object()

    def contains(self, request_key: str) -> bool:
        self._runtime_context_guard()
        normalized = str(request_key or "").strip()
        return bool(normalized and normalized in self._pending_by_key)

    def pending_count(self) -> int:
        """Return the number of Web-local interaction capabilities."""

        self._runtime_context_guard()
        return len(self._pending_by_key)

    def snapshot(self, request_key: str) -> WebPendingInteractionSnapshot | None:
        self._runtime_context_guard()
        pending = self._pending_by_key.get(str(request_key or "").strip())
        return self._snapshot(pending) if pending is not None else None

    def active_matches(self, request_key: str) -> bool:
        """Check the current record's canonical capability without exposing it."""

        self._runtime_context_guard()
        pending = self._pending_by_key.get(str(request_key or "").strip())
        return pending is not None and self._active_matches(pending.identity)

    def snapshots_for_root(
        self,
        root_thread_id: str,
    ) -> tuple[WebPendingInteractionSnapshot, ...]:
        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return ()
        return tuple(
            self._snapshot(pending)
            for pending in self._pending_by_key.values()
            if pending.owner_thread_id == root_id
        )

    def visible_snapshots(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> tuple[WebPendingInteractionSnapshot, ...]:
        """Return local delivery candidates, never final writer authority.

        The composing controller must still prove scope-specific live-document,
        attach, root, and turn authority.
        """

        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        root_id = str(root_thread_id or "").strip()
        if not normalized_client_id or not root_id:
            return ()
        return self._candidate_snapshots(normalized_client_id, root_id)

    def candidate_snapshots(
        self,
        client_id: str,
        root_thread_id: str = "",
    ) -> tuple[WebPendingInteractionSnapshot, ...]:
        """Return detached candidates before outer document/turn eligibility.

        Writer interactions remain visible only to their assigned client.
        Shared interactions are candidates for every identified local client; the
        composing controller must still prove exact active-turn eligibility.
        Omitting ``root_thread_id`` returns candidates across roots for badges.
        """

        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        root_id = str(root_thread_id or "").strip()
        if not normalized_client_id:
            return ()
        return self._candidate_snapshots(normalized_client_id, root_id)

    def _candidate_snapshots(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> tuple[WebPendingInteractionSnapshot, ...]:
        return tuple(
            self._snapshot(pending)
            for pending in self._pending_by_key.values()
            if (not root_thread_id or pending.owner_thread_id == root_thread_id)
            and (
                pending.delivery_scope == "shared_interaction"
                or pending.client_id == client_id
            )
            and not pending.hidden
            and self._active_matches(pending.identity)
        )

    def request_keys_for_root(self, root_thread_id: str) -> frozenset[str]:
        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return frozenset()
        return frozenset(
            request_key
            for request_key, pending in self._pending_by_key.items()
            if pending.owner_thread_id == root_id
        )

    def has_for_root(self, root_thread_id: str) -> bool:
        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        return bool(
            root_id
            and any(
                pending.owner_thread_id == root_id
                for pending in self._pending_by_key.values()
            )
        )

    def root_ids_for_client(self, client_id: str) -> frozenset[str]:
        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return frozenset()
        return frozenset(
            pending.owner_thread_id
            for pending in self._pending_by_key.values()
            if pending.delivery_scope == "writer_interaction"
            and pending.client_id == normalized_client_id
            and pending.owner_thread_id
        )

    def prepare_auto_resolution(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> WebAutoResolutionPreparation:
        """Issue one exact system-owned response transaction for a due timer."""

        self._runtime_context_guard()
        pending = self._pending_by_key.get(str(request_key or "").strip())
        if pending is None:
            return WebAutoResolutionPreparation("missing")
        timing = pending.auto_resolution_timing
        if (
            timing is None
            or timing.backend_epoch != int(backend_epoch)
            or timing.generation != int(generation)
            or pending.identity.method != USER_INPUT
            or pending.status != "pending"
            or pending.hidden
            or pending.delivery_scope
            not in {"writer_interaction", "shared_interaction"}
            or not self._active_matches(pending.identity)
        ):
            return WebAutoResolutionPreparation("recognized")
        return WebAutoResolutionPreparation(
            "ready",
            WebInteractionResponsePreparation(
                request_key=pending.identity.request_key,
                connection_generation=pending.identity.connection_generation,
                response_capability=pending.response_capability,
                root_thread_id=pending.owner_thread_id,
                thread_id=pending.identity.thread_id,
                turn_id=pending.identity.turn_id,
                delivery_scope=pending.delivery_scope,
                _expected=pending,
                _issuer=self._issuer,
            ),
        )

    def prepare_ingress(
        self,
        identity: ServerRequestIdentity,
    ) -> WebInteractionIngress:
        """Classify one canonical callback without deciding Web writer facts."""

        self._runtime_context_guard()
        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("Web server requests require a canonical identity")
        if not self._active_matches(identity):
            logger.warning(
                "Suppressing inactive Web server-request capability: request=%s",
                identity.request_key,
            )
            return WebInteractionIngress(identity, "consumed", _issuer=self._issuer)

        existing = self._pending_by_key.get(identity.request_key)
        if existing is None:
            return WebInteractionIngress(identity, "route", _issuer=self._issuer)

        if existing.identity is not identity:
            existing.hidden = True
            if existing.status != "submitted":
                existing.status = "unknown"
            changes = (
                (
                    WebInteractionChange(
                        existing.owner_thread_id,
                        "server_request_identity_conflict",
                    ),
                )
                if existing.owner_thread_id
                else ()
            )
            return WebInteractionIngress(
                identity,
                "identity_conflict",
                owner_thread_id_hint=existing.owner_thread_id,
                changes=changes,
                _expected=existing,
                _issuer=self._issuer,
            )
        if existing.status in {"processing", "submitted", "unknown"}:
            return WebInteractionIngress(
                identity,
                "consumed",
                owner_thread_id_hint=existing.owner_thread_id,
                _expected=existing,
                _issuer=self._issuer,
            )
        return WebInteractionIngress(
            identity,
            "route",
            owner_thread_id_hint=existing.owner_thread_id,
            _expected=existing,
            _issuer=self._issuer,
        )

    def present(
        self,
        ingress: WebInteractionIngress,
        *,
        owner_thread_id: str,
        client_id: str,
        auto_resolution_timing: AutoResolutionTiming | None = None,
        delivery_scope: WebInteractionDeliveryScope = "writer_interaction",
    ) -> WebInteractionMutation:
        self._runtime_context_guard()
        self._require_routable_ingress(ingress)
        root_id = str(owner_thread_id or "").strip()
        normalized_client_id = str(client_id or "").strip()
        if delivery_scope not in {"writer_interaction", "shared_interaction"}:
            raise ValueError("presented Web interaction has an invalid delivery scope")
        if not root_id:
            raise ValueError("presented Web interaction requires a root")
        if ingress.identity.thread_id != root_id:
            raise ValueError("presented Web interaction requires its direct thread owner")
        if delivery_scope == "writer_interaction":
            if not normalized_client_id:
                raise ValueError("writer Web interaction requires a client")
        else:
            if ingress.identity.method not in _SHARED_WEB_INTERACTION_METHODS:
                raise ValueError("shared Web interaction has an unsupported method")
            if not ingress.identity.turn_id:
                raise ValueError("shared Web interaction requires a non-empty turn")
            if normalized_client_id:
                raise ValueError("shared Web interaction cannot have a client owner")
            if (
                auto_resolution_timing is not None
                and ingress.identity.method != USER_INPUT
            ):
                raise ValueError("only shared Web user input can auto-resolve")
        if ingress.owner_thread_id_hint and ingress.owner_thread_id_hint != root_id:
            raise WebInteractionInboxError(
                "A replay cannot change its Web root owner.",
                code="request_response_unknown",
                status=409,
            )
        if ingress._expected is not None:
            previous_scope = ingress._expected.delivery_scope
            if (
                previous_scope == "shared_interaction"
                and delivery_scope != "shared_interaction"
            ):
                raise WebInteractionInboxError(
                    "A shared interaction replay cannot become writer-owned.",
                    code="request_response_unknown",
                    status=409,
                )
            if (
                previous_scope == "writer_interaction"
                and delivery_scope == "writer_interaction"
                and ingress._expected.client_id != normalized_client_id
            ):
                raise WebInteractionInboxError(
                    "A replay cannot transfer its browser writer.",
                    code="request_response_unknown",
                    status=409,
                )
        previous_root = ingress._expected.owner_thread_id if ingress._expected else ""
        response_capability = (
            ingress._expected.response_capability
            if ingress._expected is not None
            and ingress._expected.identity is ingress.identity
            else secrets.token_urlsafe(32)
        )
        self._pending_by_key[ingress.identity.request_key] = _PendingWebInteraction(
            identity=ingress.identity,
            owner_thread_id=root_id,
            client_id=normalized_client_id,
            response_capability=response_capability,
            delivery_scope=delivery_scope,
            auto_resolution_timing=auto_resolution_timing,
        )
        changes: list[WebInteractionChange] = []
        if previous_root and previous_root != root_id:
            changes.append(
                WebInteractionChange(previous_root, "backend_request_id_reused")
            )
        changes.append(WebInteractionChange(root_id, "created"))
        return WebInteractionMutation(tuple(changes))

    def _require_routable_ingress(self, ingress: WebInteractionIngress) -> None:
        if (
            not isinstance(ingress, WebInteractionIngress)
            or ingress._issuer is not self._issuer
        ):
            raise ValueError("Web interaction ingress was not issued by this inbox")
        if ingress.disposition != "route":
            raise ValueError("Web interaction ingress is not routable")
        current = self._pending_by_key.get(ingress.identity.request_key)
        if current is not ingress._expected:
            raise WebInteractionInboxError(
                "The Web interaction changed while it was being routed.",
                code="request_response_unknown",
                status=409,
            )
        if not self._active_matches(ingress.identity):
            raise WebInteractionInboxError(
                "This interaction belongs to an earlier Codex connection and cannot be routed.",
                code="request_identity_inactive",
                status=409,
            )

    def _require_current(
        self,
        ingress: WebInteractionIngress,
    ) -> _PendingWebInteraction:
        current = self._pending_by_key.get(ingress.identity.request_key)
        if current is None or current is not ingress._expected:
            raise WebInteractionInboxError(
                "This request is no longer pending.",
                code="request_not_found",
                status=404,
            )
        return current

    def prepare_response(
        self,
        client_id: str,
        request_key: str,
        connection_generation: int,
        response_capability: str,
    ) -> WebInteractionResponsePreparation:
        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        normalized_request_key = str(request_key or "").strip()
        normalized_response_capability = str(response_capability or "").strip()
        if (
            isinstance(connection_generation, bool)
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise WebInteractionInboxError(
                "A positive Codex connection generation is required.",
                code="invalid_request_generation",
            )
        if (
            not isinstance(response_capability, str)
            or not normalized_response_capability
            or normalized_response_capability != response_capability
            or len(response_capability) > 256
        ):
            raise WebInteractionInboxError(
                "An exact response capability is required.",
                code="invalid_response_capability",
            )
        pending = self._pending_by_key.get(normalized_request_key)
        if pending is None or pending.hidden:
            raise WebInteractionInboxError(
                "This request is no longer pending.",
                code="request_not_found",
                status=404,
            )
        if not normalized_client_id or (
            pending.delivery_scope == "writer_interaction"
            and pending.client_id != normalized_client_id
        ):
            raise WebInteractionInboxError(
                "This request belongs to another browser writer.",
                code="request_not_owned",
                status=409,
            )
        if pending.identity.connection_generation != connection_generation:
            raise WebInteractionInboxError(
                "This action belongs to an earlier Codex connection.",
                code="request_generation_mismatch",
                status=409,
            )
        if pending.response_capability != response_capability:
            raise WebInteractionInboxError(
                "This action belongs to an expired response capability.",
                code="response_capability_mismatch",
                status=409,
            )
        if pending.status in {"processing", "submitted"}:
            raise WebInteractionInboxError(
                "This request is already being submitted.",
                code="request_processing",
                status=409,
            )
        if pending.status == "unknown":
            raise WebInteractionInboxError(
                "The request response may already have been delivered; wait for Codex to resolve it.",
                code="request_response_unknown",
                status=409,
            )
        return WebInteractionResponsePreparation(
            request_key=normalized_request_key,
            connection_generation=connection_generation,
            response_capability=response_capability,
            root_thread_id=pending.owner_thread_id,
            thread_id=pending.identity.thread_id,
            turn_id=pending.identity.turn_id,
            delivery_scope=pending.delivery_scope,
            _expected=pending,
            _issuer=self._issuer,
        )

    def submit_response(
        self,
        preparation: WebInteractionResponsePreparation,
        *,
        action: str,
        answers: dict[str, Any] | None = None,
    ) -> WebInteractionSubmission:
        self._runtime_context_guard()
        if preparation._issuer is not self._issuer:
            raise ValueError("Web response preparation was not issued by this inbox")
        pending = self._pending_by_key.get(preparation.request_key)
        if pending is None or pending is not preparation._expected:
            raise WebInteractionInboxError(
                "This request is no longer pending.",
                code="request_not_found",
                status=404,
            )
        if pending.identity.connection_generation != preparation.connection_generation:
            raise WebInteractionInboxError(
                "This action belongs to an earlier Codex connection.",
                code="request_generation_mismatch",
                status=409,
            )
        if pending.response_capability != preparation.response_capability:
            raise WebInteractionInboxError(
                "This action belongs to an expired response capability.",
                code="response_capability_mismatch",
                status=409,
            )
        if pending.hidden:
            raise WebInteractionInboxError(
                "This request is no longer pending.",
                code="request_not_found",
                status=404,
            )
        if pending.status in {"processing", "submitted"}:
            raise WebInteractionInboxError(
                "This request is already being submitted.",
                code="request_processing",
                status=409,
            )
        if pending.status == "unknown":
            raise WebInteractionInboxError(
                "The request response may already have been delivered; wait for Codex to resolve it.",
                code="request_response_unknown",
                status=409,
            )
        if not self._active_matches(pending.identity):
            pending.hidden = True
            pending.status = "unknown"
            change = WebInteractionChange(
                pending.owner_thread_id,
                "server_request_identity_inactive",
            )
            raise WebInteractionInboxError(
                "This interaction belongs to an earlier Codex connection and cannot be answered.",
                code="request_identity_inactive",
                status=409,
                changes=(change,),
            )

        try:
            result, error = interaction_response_payload(
                pending.identity.method,
                pending.identity.params,
                action=str(action or "").strip(),
                answers=answers or {},
            )
        except ValueError as exc:
            raise WebInteractionInboxError(
                str(exc),
                code="invalid_action",
            ) from exc

        pending.status = "processing"
        try:
            self._ports.respond(
                pending.identity,
                result=result,
                error=error,
            )
        except ServerRequestResponseSupersededError as exc:
            self._pending_by_key.pop(preparation.request_key, None)
            change = WebInteractionChange(
                pending.owner_thread_id,
                "response_superseded",
            )
            raise WebInteractionInboxError(
                "This interaction was already handled elsewhere or cleared by Codex.",
                code="request_superseded",
                status=409,
                changes=(change,),
            ) from exc
        except CodexRpcPreSendError as exc:
            pending.status = "pending"
            raise WebInteractionInboxError(
                "The interaction response was not sent because the Codex connection was unavailable. "
                "Reconnect before retrying this decision.",
                code="request_not_sent",
                status=503,
            ) from exc
        except Exception as exc:
            pending.status = "unknown"
            change = WebInteractionChange(
                pending.owner_thread_id,
                "response_outcome_unknown",
            )
            raise WebInteractionInboxError(
                "The interaction response may have been delivered, but Focus could not confirm it. "
                "Do not submit a second decision.",
                code="request_response_unknown",
                status=409,
                changes=(change,),
            ) from exc
        pending.status = "submitted"
        change = WebInteractionChange(pending.owner_thread_id, "submitted")
        return WebInteractionSubmission(
            request_key=preparation.request_key,
            changes=(change,),
        )

    def mark_response_outcome_unknown(
        self,
        preparation: WebInteractionResponsePreparation,
        *,
        reason: str,
    ) -> WebInteractionMutation:
        """Fail closed a prepared response after a prerequisite became uncertain."""

        self._runtime_context_guard()
        if preparation._issuer is not self._issuer:
            raise ValueError("Web response preparation was not issued by this inbox")
        pending = self._pending_by_key.get(preparation.request_key)
        if pending is None or pending is not preparation._expected:
            raise WebInteractionInboxError(
                "This request is no longer pending.",
                code="request_not_found",
                status=404,
            )
        if pending.status == "submitted":
            raise WebInteractionInboxError(
                "This request was already submitted.",
                code="request_processing",
                status=409,
            )
        if pending.status == "unknown":
            return WebInteractionMutation()
        pending.status = "unknown"
        return WebInteractionMutation(
            (
                WebInteractionChange(
                    pending.owner_thread_id,
                    str(reason or "response_outcome_unknown").strip()
                    or "response_outcome_unknown",
                ),
            )
        )

    def fail_close(
        self,
        ingress: WebInteractionIngress,
        *,
        owner_thread_id: str,
        client_id: str,
        hidden: bool,
        message: str,
    ) -> WebInteractionMutation:
        self._runtime_context_guard()
        self._require_routable_ingress(ingress)
        root_id = str(owner_thread_id or "").strip()
        normalized_client_id = str(client_id or "").strip()
        if not root_id or not normalized_client_id:
            raise ValueError("fail-closed Web interaction requires root and client")
        if ingress.identity.thread_id != root_id:
            raise ValueError(
                "fail-closed Web interaction requires its direct thread owner"
            )
        if ingress.owner_thread_id_hint and ingress.owner_thread_id_hint != root_id:
            raise WebInteractionInboxError(
                "A replay cannot change its Web root owner.",
                code="request_response_unknown",
                status=409,
            )
        if ingress._expected is not None:
            if ingress._expected.delivery_scope != "writer_interaction":
                raise WebInteractionInboxError(
                    "A shared interaction replay cannot become writer-owned.",
                    code="request_response_unknown",
                    status=409,
                )
            if ingress._expected.client_id != normalized_client_id:
                raise WebInteractionInboxError(
                    "A replay cannot transfer its browser writer.",
                    code="request_response_unknown",
                    status=409,
                )
        status = self._send_fail_close(
            ingress.identity,
            message=message,
            log_context="automatic",
        )
        self._pending_by_key[ingress.identity.request_key] = _PendingWebInteraction(
            identity=ingress.identity,
            owner_thread_id=root_id,
            client_id=normalized_client_id,
            response_capability=(
                ingress._expected.response_capability
                if ingress._expected is not None
                and ingress._expected.identity is ingress.identity
                else secrets.token_urlsafe(32)
            ),
            delivery_scope="writer_interaction",
            status=status,
            hidden=bool(hidden),
        )
        reason = {
            "pending": "automatic_response_not_sent",
            "submitted": "automatic_response_submitted",
            "unknown": "automatic_response_unknown",
        }[status]
        return WebInteractionMutation((WebInteractionChange(root_id, reason),))

    def _send_fail_close(
        self,
        identity: ServerRequestIdentity,
        *,
        message: str,
        log_context: str,
    ) -> Literal["pending", "submitted", "unknown"]:
        result, error = fail_closed_interaction_response(
            identity.method,
            identity.params,
            message=message,
        )
        try:
            self._ports.respond(
                identity,
                result=result,
                error=error,
            )
        except CodexRpcPreSendError:
            logger.info(
                "Web %s fail-close response was not sent: request=%s",
                log_context,
                identity.request_key,
                exc_info=True,
            )
            return "pending"
        except Exception:
            logger.exception(
                "Web %s fail-close response has unknown outcome: request=%s",
                log_context,
                identity.request_key,
            )
            return "unknown"
        return "submitted"

    def resolve_exact(
        self,
        expected_identity: ServerRequestIdentity,
    ) -> WebInteractionResolution:
        """Remove only an exact identity already settled by the registry owner."""

        self._runtime_context_guard()
        if not isinstance(expected_identity, ServerRequestIdentity):
            raise TypeError(
                "Web resolution requires an exact server-request identity"
            )
        normalized_request_key = expected_identity.request_key
        pending = self._pending_by_key.get(normalized_request_key)
        if pending is None:
            return WebInteractionResolution("missing", normalized_request_key)
        if pending.identity is not expected_identity:
            return WebInteractionResolution(
                "mismatch",
                normalized_request_key,
                owner_thread_id=pending.owner_thread_id,
                thread_id=pending.identity.thread_id,
            )
        if self._active_matches(expected_identity):
            return WebInteractionResolution(
                "not_resolved",
                normalized_request_key,
                owner_thread_id=pending.owner_thread_id,
                thread_id=pending.identity.thread_id,
            )
        if self._pending_by_key.get(normalized_request_key) is not pending:
            return WebInteractionResolution("mismatch", normalized_request_key)
        self._pending_by_key.pop(normalized_request_key, None)
        change = WebInteractionChange(pending.owner_thread_id, "resolved_elsewhere")
        return WebInteractionResolution(
            "resolved",
            normalized_request_key,
            owner_thread_id=pending.owner_thread_id,
            thread_id=pending.identity.thread_id,
            changes=(change,),
        )

    def revoke_exact_response_authority(
        self,
        expected_identity: ServerRequestIdentity,
    ) -> WebInteractionMutation:
        """Hide one exact projection while its upstream callback stays pending."""

        self._runtime_context_guard()
        if not isinstance(expected_identity, ServerRequestIdentity):
            raise TypeError(
                "Web response-authority revocation requires an exact server-request identity"
            )
        pending = self._pending_by_key.get(expected_identity.request_key)
        if pending is None or pending.identity is not expected_identity or pending.hidden:
            return WebInteractionMutation()
        pending.hidden = True
        return WebInteractionMutation(
            (
                WebInteractionChange(
                    pending.owner_thread_id,
                    "response_authority_revoked",
                ),
            )
        )

    def hide_for_lifecycle(
        self,
        thread_id: str,
        *,
        reason: str,
        turn_id: str | None = None,
        preserve_turn_id: bool = False,
    ) -> WebInteractionMutation:
        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return WebInteractionMutation()
        normalized_turn_id = str(turn_id or "").strip() if turn_id is not None else None
        affected_roots: set[str] = set()
        for pending in self._pending_by_key.values():
            if pending.identity.thread_id != normalized_thread_id:
                continue
            if normalized_turn_id is not None:
                if not normalized_turn_id:
                    continue
                if preserve_turn_id and pending.identity.turn_id == normalized_turn_id:
                    continue
                if not preserve_turn_id and pending.identity.turn_id != normalized_turn_id:
                    continue
            pending.hidden = True
            pending.lifecycle_closed = str(reason or "").strip()
            if pending.owner_thread_id:
                affected_roots.add(pending.owner_thread_id)
        return WebInteractionMutation(
            tuple(
                WebInteractionChange(root_id, f"upstream_{reason}")
                for root_id in sorted(affected_roots)
            )
        )

    def backend_disconnected(self) -> WebInteractionMutation:
        """Drop old-connection capabilities; replay rebuilds fresh projections."""

        self._runtime_context_guard()
        retired = tuple(self._pending_by_key.values())
        self._pending_by_key.clear()
        affected_roots = {
            pending.owner_thread_id for pending in retired if pending.owner_thread_id
        }
        return WebInteractionMutation(
            tuple(
                WebInteractionChange(root_id, "backend_disconnected")
                for root_id in sorted(affected_roots)
            )
        )

    def retire_backend_epoch_after_stop(
        self,
    ) -> WebInteractionBackendEpochRetirement:
        """Retire all response capabilities after the old machine stopped.

        This is local capability retirement, not upstream resolution. Clearing
        the map before returning makes every already-issued response
        preparation inert against a same-id replacement from the next backend
        generation.
        """

        self._runtime_context_guard()
        retired = tuple(self._pending_by_key.items())
        self._pending_by_key.clear()
        roots = {
            pending.owner_thread_id
            for _request_key, pending in retired
            if pending.owner_thread_id
        }
        return WebInteractionBackendEpochRetirement(
            request_keys=frozenset(request_key for request_key, _pending in retired),
            changes=tuple(
                WebInteractionChange(root_id, "backend_epoch_retired_after_stop")
                for root_id in sorted(roots)
            ),
        )

    def fail_close_client(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> WebInteractionMutation:
        """Drop one browser projection and let upstream replay on resume."""

        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        root_id = str(root_thread_id or "").strip()
        removed = False
        for request_key, pending in tuple(self._pending_by_key.items()):
            if (
                pending.delivery_scope == "writer_interaction"
                and pending.client_id == normalized_client_id
                and pending.owner_thread_id == root_id
            ):
                self._pending_by_key.pop(request_key, None)
                removed = True
        return WebInteractionMutation(
            (WebInteractionChange(root_id, "client_disconnected"),)
            if removed and root_id
            else ()
        )

    def _active_matches(self, identity: ServerRequestIdentity) -> bool:
        try:
            return bool(self._ports.active_matches(identity))
        except Exception:
            logger.exception(
                "Unable to verify active Web server-request capability: request=%s",
                identity.request_key,
            )
            return False

    @staticmethod
    def _snapshot(pending: _PendingWebInteraction) -> WebPendingInteractionSnapshot:
        timing = pending.auto_resolution_timing
        return WebPendingInteractionSnapshot(
            request_key=pending.identity.request_key,
            connection_generation=pending.identity.connection_generation,
            response_capability=pending.response_capability,
            method=pending.identity.method,
            params=pending.identity.params,
            thread_id=pending.identity.thread_id,
            owner_thread_id=pending.owner_thread_id,
            turn_id=pending.identity.turn_id,
            client_id=pending.client_id,
            delivery_scope=pending.delivery_scope,
            status=pending.status,
            hidden=pending.hidden,
            lifecycle_closed=pending.lifecycle_closed,
            auto_resolution_backend_epoch=timing.backend_epoch if timing else 0,
            auto_resolution_visible_at_ms=timing.visible_at_ms if timing else 0,
            auto_resolution_due_at_ms=timing.due_at_ms if timing else 0,
        )
