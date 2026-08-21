"""RuntimeLoop owner for process-local Codex server requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.codex_protocol.client import CodexRpcPreSendError
from bot.jsonrpc_id import optional_jsonrpc_id_key
from bot.runtime_loop import RuntimeContextGuard
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestResolutionReport,
    ServerRequestResponseAdmissionError,
    ServerRequestResponseReport,
    ServerRequestResponseSupersededError,
    ServerRequestRoutingReport,
)
from bot.server_request_dispatch import ServerRequestDispatchReceipt
from bot.server_request_registry import ServerRequestRegistry


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServerRequestCoordinatorPorts:
    """Surface effects; none owns the upstream callback lifecycle."""

    cancel_auto_resolution: Callable[[str], None]
    remove_web_resolved: Callable[[ServerRequestIdentity], ServerRequestLocalRemoval]
    revoke_web_response_authority: Callable[[ServerRequestIdentity], None]
    remove_fcodex_resolved: Callable[[ServerRequestIdentity], ServerRequestLocalRemoval]
    remove_feishu_resolved: Callable[[ServerRequestIdentity], ServerRequestLocalRemoval]
    reconcile_resolved_root: Callable[[str], None]
    invalidate_auto_resolution_epoch: Callable[[], None]
    shutdown_auto_resolution: Callable[[], None]
    dispatch_request: Callable[[ServerRequestIdentity], ServerRequestDispatchReceipt]
    respond: Callable[..., None]


class ServerRequestCoordinator:
    """Own canonical callbacks projected into eligible Focus surfaces."""

    def __init__(
        self,
        registry: ServerRequestRegistry,
        ports: ServerRequestCoordinatorPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        self._registry = registry
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def activate_connection_epoch(self, connection_generation: int) -> None:
        self._runtime_context_guard()
        self._registry.activate_connection_epoch(connection_generation)

    def route_request(
        self,
        connection_generation: int,
        request_id: int | str,
        method: str,
        params: dict[str, Any],
    ) -> ServerRequestRoutingReport:
        """Register one frame and idempotently project exact replay."""

        self._runtime_context_guard()
        try:
            observed = ServerRequestIdentity(
                request_id=request_id,
                connection_generation=connection_generation,
                method=method,
                params=params,
            )
        except ValueError:
            logger.exception("Rejecting malformed Codex server request")
            return ServerRequestRoutingReport("identity_conflict")

        registration = self._registry.register(observed)
        identity = registration.identity
        if registration.outcome == "epoch_mismatch":
            return ServerRequestRoutingReport(
                "epoch_mismatch",
                request_key=observed.request_key,
                thread_id=observed.thread_id,
            )
        if registration.outcome == "resolved":
            return ServerRequestRoutingReport(
                "suppressed_resolved",
                request_key=observed.request_key,
                thread_id=observed.thread_id,
            )
        if registration.outcome == "identity_conflict" or identity is None:
            logger.error(
                "Conflicting Codex server request in one connection epoch: request=%s",
                observed.request_key,
            )
            return ServerRequestRoutingReport(
                "identity_conflict",
                request_key=observed.request_key,
                thread_id=observed.thread_id,
            )
        response_phase = self._registry.response_phase(identity)
        response_authority_revoked = self._registry.response_authority_is_revoked(
            identity
        )
        if response_authority_revoked or response_phase in {
            "processing",
            "submitted",
            "unknown",
        }:
            return ServerRequestRoutingReport(
                "response_pending_resolution",
                request_key=identity.request_key,
                thread_id=identity.thread_id,
                response_phase=response_phase or "",
                response_authority_revoked=response_authority_revoked,
            )
        if self._registry.dispatch_is_unknown(identity):
            return ServerRequestRoutingReport(
                "dispatch_failed",
                request_key=identity.request_key,
                thread_id=identity.thread_id,
                dispatch_outcome="outcome_unknown",
            )

        dispatch = self._ports.dispatch_request(identity)
        if dispatch.outcome == "committed":
            return ServerRequestRoutingReport(
                "replayed" if registration.outcome == "replay" else "committed",
                request_key=identity.request_key,
                thread_id=identity.thread_id,
                dispatch_outcome="committed",
            )
        if dispatch.outcome == "outcome_unknown":
            self._registry.mark_dispatch_unknown(identity)
        logger.warning(
            "Codex server request has no committed surface projection: request=%s outcome=%s",
            identity.request_key,
            dispatch.outcome,
        )
        return ServerRequestRoutingReport(
            "dispatch_failed",
            request_key=identity.request_key,
            thread_id=identity.thread_id,
            dispatch_outcome=dispatch.outcome,
        )

    def handle_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> ServerRequestResolutionReport | None:
        """Remove local projections when upstream removes pending callbacks."""

        self._runtime_context_guard()
        normalized_method = str(method or "").strip()
        payload = dict(params or {})
        if normalized_method == "serverRequest/resolved":
            return self.handle_server_request_resolved(payload)
        lifecycle_methods = {
            "turn/started",
            "turn/completed",
            "turn/aborted",
            "thread/closed",
            "thread/archived",
            "thread/deleted",
        }
        if normalized_method == "thread/status/changed":
            status = payload.get("status")
            status_type = (
                str(status.get("type", "") or "").strip()
                if isinstance(status, dict)
                else ""
            )
            if status_type not in {"idle", "notLoaded"}:
                return None
        elif normalized_method not in lifecycle_methods:
            return None
        thread_id = str(payload.get("threadId", "") or "").strip()
        if not thread_id:
            return None
        identities = self._registry.settle_thread(thread_id)
        self._remove_local_projections(identities)
        return None

    def handle_server_request_resolved(
        self,
        params: dict[str, object],
    ) -> ServerRequestResolutionReport:
        self._runtime_context_guard()
        payload = dict(params or {})
        request_key = optional_jsonrpc_id_key(payload.get("requestId"))
        thread_id = str(payload.get("threadId", "") or "").strip()
        settlement = self._registry.settle(request_key, thread_id=thread_id)
        if settlement.outcome in {"identity_conflict", "invalid"}:
            logger.error(
                "Rejecting inconsistent serverRequest/resolved: request=%s thread=%s",
                request_key,
                thread_id,
            )
            self._cancel_auto_resolution(request_key)
            return ServerRequestResolutionReport(
                outcome=settlement.outcome,
                request_key=request_key,
                thread_id=thread_id,
            )
        if settlement.identity is None:
            self._cancel_auto_resolution(request_key)
            return ServerRequestResolutionReport(
                outcome=settlement.outcome,
                request_key=request_key,
                thread_id=thread_id,
            )
        removals, roots = self._remove_local_projections((settlement.identity,))
        return ServerRequestResolutionReport(
            outcome=settlement.outcome,
            request_key=request_key,
            thread_id=thread_id,
            local_removals=removals,
            reconciled_root_ids=roots,
        )

    def request_is_resolved(self, request_key: str) -> bool:
        self._runtime_context_guard()
        return self._registry.request_is_resolved(request_key)

    def submit_response(
        self,
        identity: ServerRequestIdentity,
        *,
        result: dict[str, Any] | None,
        error: dict[str, Any] | None,
        timeout: float | None = None,
    ) -> ServerRequestResponseReport:
        """Submit at most one response for the exact canonical identity.

        A pre-send failure releases the local authority for an exact retry.
        Every other failure has an unknown external outcome and permanently
        fences only this request until upstream settlement or epoch retirement.
        """

        self._runtime_context_guard()
        admission = self._registry.begin_response(identity)
        if admission.outcome != "admitted":
            outcome = {
                "submitted": "superseded",
                "unknown": "outcome_unknown",
                "revoked": "superseded",
            }.get(admission.outcome, admission.outcome)
            return ServerRequestResponseReport(
                outcome=outcome,
                request_key=identity.request_key,
                thread_id=identity.thread_id,
            )

        respond_kwargs: dict[str, Any] = {
            "connection_generation": identity.connection_generation,
            "result": result,
            "error": error,
        }
        if timeout is not None:
            respond_kwargs["timeout"] = timeout
        try:
            self._ports.respond(identity.request_id, **respond_kwargs)
        except CodexRpcPreSendError:
            self._finish_response(identity, outcome="not_sent")
            raise
        except Exception:
            self._finish_response(identity, outcome="unknown")
            raise
        self._finish_response(identity, outcome="submitted")
        return ServerRequestResponseReport(
            "submitted",
            request_key=identity.request_key,
            thread_id=identity.thread_id,
        )

    def submit_surface_response(
        self,
        identity: ServerRequestIdentity,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Submit only the exact canonical object retained by one surface."""

        self._runtime_context_guard()
        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("surface response requires a canonical identity")
        request_key = identity.request_key
        active = self._registry.active_identity(request_key)
        if active is None:
            resolved = self._registry.resolved_identity(request_key)
            if resolved is identity:
                report = ServerRequestResponseReport(
                    "superseded",
                    request_key=request_key,
                    thread_id=resolved.thread_id,
                )
            elif resolved is not None:
                report = ServerRequestResponseReport(
                    "identity_conflict",
                    request_key=request_key,
                    thread_id=resolved.thread_id,
                )
            else:
                report = ServerRequestResponseReport(
                    "not_pending",
                    request_key=request_key,
                )
        elif active is not identity:
            report = ServerRequestResponseReport(
                "identity_conflict",
                request_key=request_key,
                thread_id=active.thread_id,
            )
        else:
            report = self.submit_response(
                identity,
                result=result,
                error=error,
                timeout=timeout,
            )
        if report.outcome == "submitted":
            return
        if report.outcome == "superseded":
            raise ServerRequestResponseSupersededError(report)
        raise ServerRequestResponseAdmissionError(report)

    def revoke_surface_response_authority(
        self,
        identity: ServerRequestIdentity,
    ) -> bool:
        """Revoke one exact current-epoch user response capability.

        This does not assert that a fail-close response reached app-server and
        does not settle the callback.  Canonical resolution, lifecycle cleanup,
        or epoch retirement remains the only way to remove the pending fact.
        """

        self._runtime_context_guard()
        revoked = self._registry.revoke_response_authority(identity)
        if not revoked:
            return False
        try:
            self._ports.revoke_web_response_authority(identity)
        except Exception:
            logger.exception(
                "Unable to hide revoked Web server-request projection: request=%s",
                identity.request_key,
            )
        return True

    def pending_request_keys_for_root(
        self,
        root_thread_id: str,
    ) -> frozenset[str]:
        self._runtime_context_guard()
        root_id = str(root_thread_id or "").strip()
        if not root_id:
            return frozenset()
        return frozenset(
            request_key
            for request_key, identity in self._registry.pending_items()
            if self._root_for_identity(identity) == root_id
        )

    def has_pending_request_for_root(self, root_thread_id: str) -> bool:
        return bool(self.pending_request_keys_for_root(root_thread_id))

    def pending_count(self) -> int:
        self._runtime_context_guard()
        return self._registry.pending_count()

    def backend_disconnected(self) -> None:
        self.retire_connection_epoch()

    def retire_connection_epoch(self) -> tuple[ServerRequestIdentity, ...]:
        self._runtime_context_guard()
        self._ports.invalidate_auto_resolution_epoch()
        return self._registry.clear_connection_epoch()

    def shutdown(self) -> None:
        self._ports.shutdown_auto_resolution()

    def _remove_local_projections(
        self,
        identities: tuple[ServerRequestIdentity, ...],
    ) -> tuple[tuple[ServerRequestLocalRemoval, ...], frozenset[str]]:
        removals: list[ServerRequestLocalRemoval] = []
        affected_roots: set[str] = set()
        for identity in identities:
            self._cancel_auto_resolution(identity.request_key)
            local_root_found = False
            for remove in (
                self._ports.remove_web_resolved,
                self._ports.remove_fcodex_resolved,
                self._ports.remove_feishu_resolved,
            ):
                try:
                    removal = remove(identity)
                except Exception:
                    logger.exception(
                        "Unable to remove one local server-request projection: "
                        "request=%s remover=%s",
                        identity.request_key,
                        getattr(remove, "__name__", type(remove).__name__),
                    )
                    continue
                removals.append(removal)
                root_id = str(removal.root_thread_id or "").strip()
                if removal.outcome == "removed" and root_id:
                    affected_roots.add(root_id)
                    local_root_found = True
            if not local_root_found:
                root_id = self._root_for_identity(identity)
                if root_id:
                    affected_roots.add(root_id)
        for root_id in sorted(affected_roots):
            try:
                self._ports.reconcile_resolved_root(root_id)
            except Exception:
                logger.exception(
                    "Unable to reconcile one resolved server-request root: root=%s",
                    root_id,
                )
        return tuple(removals), frozenset(affected_roots)

    def _root_for_identity(self, identity: ServerRequestIdentity) -> str:
        return str(identity.thread_id or "").strip()

    def _cancel_auto_resolution(self, request_key: str) -> None:
        try:
            self._ports.cancel_auto_resolution(request_key)
        except Exception:
            logger.exception(
                "Unable to cancel server-request auto-resolution: request=%s",
                request_key,
            )

    def _finish_response(
        self,
        identity: ServerRequestIdentity,
        *,
        outcome: str,
    ) -> None:
        if not self._registry.finish_response(identity, outcome=outcome):
            raise RuntimeError(
                "canonical server-request response state changed during submission"
            )
