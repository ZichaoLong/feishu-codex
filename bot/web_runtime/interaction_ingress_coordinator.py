"""RuntimeLoop-owned Web server-request ingress transaction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.interaction_contract import SHARED_APPROVAL_METHODS, normalize_interaction_request
from bot.runtime_loop import RuntimeContextGuard
from bot.server_request_contract import ServerRequestIdentity, ServerRequestRoutingMode
from bot.server_request_dispatch import ServerRequestSurfaceIdentityConflict
from bot.web_runtime.interaction_inbox import (
    WebInteractionChange,
    WebInteractionIngress,
    WebInteractionInbox,
    WebInteractionInboxError,
)
from bot.web_runtime.contract import (
    WebInteractionDeliveryDecision,
    WebInteractionDeliveryDisposition,
)


logger = logging.getLogger(__name__)


class WebInteractionIngressRuntimeInterestPort(Protocol):
    def confirm_thread_scoped_server_request(self, thread_id: str) -> bool: ...
    def has_managed_interest(self, thread_id: str) -> bool: ...
    def subscription_is_current(self, thread_id: str) -> bool: ...


class WebInteractionIngressOperationPort(Protocol):
    def interaction_delivery_decision(
        self,
        root_thread_id: str,
    ) -> WebInteractionDeliveryDecision: ...


@dataclass(frozen=True, slots=True)
class WebInteractionIngressPorts:
    runtime_interest: WebInteractionIngressRuntimeInterestPort
    operations: WebInteractionIngressOperationPort
    shared_interaction_has_live_recipient: Callable[[str, str, str], bool]
    publish_changes: Callable[[tuple[WebInteractionChange, ...]], None]


class WebInteractionIngressCoordinator:
    """Route one canonical callback without copying writer authority facts."""

    def __init__(
        self,
        *,
        inbox: WebInteractionInbox,
        ports: WebInteractionIngressPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web interaction ingress requires a RuntimeLoop context guard")
        self._inbox = inbox
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard

    def handle_adapter_request(
        self,
        identity: ServerRequestIdentity,
        *,
        auto_resolution_timing: AutoResolutionTiming | None = None,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> bool:
        self._runtime_context_guard()
        if not isinstance(identity, ServerRequestIdentity):
            raise TypeError("Web server requests require a canonical identity")
        request_key = identity.request_key
        thread_id = identity.thread_id
        shared_interaction = routing_mode in {
            "shared_approval",
            "shared_interaction",
        }
        if not thread_id:
            return False
        if routing_mode == "shared_approval" and (
            identity.method not in SHARED_APPROVAL_METHODS or not identity.turn_id
        ):
            return False
        if routing_mode == "shared_interaction" and (
            identity.method in SHARED_APPROVAL_METHODS or not identity.turn_id
        ):
            return False

        ingress = self._inbox.prepare_ingress(identity)
        self._ports.publish_changes(ingress.changes)
        if ingress.disposition == "identity_conflict":
            raise ServerRequestSurfaceIdentityConflict(
                "Web retained a different canonical server-request capability"
            )
        if ingress.disposition == "consumed":
            return True

        owner_thread_id = thread_id

        normalized_request = normalize_interaction_request(identity.method, identity.params)
        presentable = bool(normalized_request.get("presentable"))
        if shared_interaction:
            interest = self._ports.runtime_interest
            if (
                not presentable
                or not interest.has_managed_interest(thread_id)
                or not interest.subscription_is_current(thread_id)
            ):
                return False
            if (
                routing_mode == "shared_interaction"
                and not self._ports.shared_interaction_has_live_recipient(
                    owner_thread_id,
                    thread_id,
                    identity.turn_id,
                )
            ):
                return False
            interest.confirm_thread_scoped_server_request(thread_id)
            try:
                mutation = self._inbox.present(
                    ingress,
                    owner_thread_id=owner_thread_id,
                    client_id="",
                    auto_resolution_timing=auto_resolution_timing,
                    delivery_scope="shared_interaction",
                )
            except WebInteractionInboxError as exc:
                self._ports.publish_changes(exc.changes)
                logger.error(
                    "Unable to retain shared Web interaction: request=%s code=%s",
                    request_key,
                    exc.code,
                )
                raise
            self._ports.publish_changes(mutation.changes)
            return True

        decision = self._ports.operations.interaction_delivery_decision(
            owner_thread_id
        )
        if decision.disposition is WebInteractionDeliveryDisposition.DECLINED:
            return False

        self._ports.runtime_interest.confirm_thread_scoped_server_request(thread_id)

        if not presentable:
            self._fail_close(
                ingress,
                owner_thread_id=owner_thread_id,
                client_id=decision.client_id,
                hidden=True,
                message=str(
                    normalized_request.get("unsupported_reason", "")
                    or "Focus Web cannot reliably present this request"
                ),
                log_label="unsupported",
            )
            return True
        if decision.disposition is WebInteractionDeliveryDisposition.DISCONNECTED:
            self._fail_close(
                ingress,
                owner_thread_id=owner_thread_id,
                client_id=decision.client_id,
                hidden=False,
                message="Focus Web client disconnected",
                log_label="disconnected",
            )
            return True

        try:
            mutation = self._inbox.present(
                ingress,
                owner_thread_id=owner_thread_id,
                client_id=decision.client_id,
                auto_resolution_timing=auto_resolution_timing,
            )
        except WebInteractionInboxError as exc:
            self._ports.publish_changes(exc.changes)
            logger.error(
                "Unable to present Web interaction: request=%s code=%s",
                request_key,
                exc.code,
            )
            raise
        self._ports.publish_changes(mutation.changes)
        return True

    def _fail_close(
        self,
        ingress: WebInteractionIngress,
        *,
        owner_thread_id: str,
        client_id: str,
        hidden: bool,
        message: str,
        log_label: str,
    ) -> None:
        try:
            mutation = self._inbox.fail_close(
                ingress,
                owner_thread_id=owner_thread_id,
                client_id=client_id,
                hidden=hidden,
                message=message,
            )
        except WebInteractionInboxError as exc:
            self._ports.publish_changes(exc.changes)
            logger.error(
                "Unable to fail-close %s Web interaction: request=%s code=%s",
                log_label,
                getattr(getattr(ingress, "identity", None), "request_key", ""),
                exc.code,
            )
            raise
        self._ports.publish_changes(mutation.changes)
