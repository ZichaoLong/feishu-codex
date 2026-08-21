"""Web public-action composition around the interaction inbox owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.web_runtime.interaction_inbox import (
    WebInteractionBackendEpochRetirement,
    WebInteractionChange,
    WebInteractionInbox,
    WebInteractionInboxError,
)
from bot.web_runtime.contract import WebConnectedWriterReceipt, WebRuntimeError


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebInteractionResponsePorts:
    require_client_id: Callable[[str], str]
    require_connected_writer: Callable[..., WebConnectedWriterReceipt]
    shared_interaction_eligible: Callable[[str, str, str, str], bool]
    publish_changes: Callable[[tuple[WebInteractionChange, ...]], None]


class WebInteractionResponseController:
    """Compose document, callback, nonce, and RPC-generation capabilities."""

    def __init__(
        self,
        *,
        inbox: WebInteractionInbox,
        ports: WebInteractionResponsePorts,
    ) -> None:
        self._inbox = inbox
        self._ports = ports

    def respond(
        self,
        client_id: str,
        request_key: str,
        *,
        connection_generation: int,
        response_capability: str,
        action: str,
        answers: dict[str, Any] | None,
    ) -> dict[str, Any]:
        client_id = self._ports.require_client_id(client_id)
        preparation = self._inbox.prepare_response(
            client_id,
            str(request_key or "").strip(),
            connection_generation,
            response_capability,
        )
        if preparation.delivery_scope == "shared_interaction":
            if not self._ports.shared_interaction_eligible(
                client_id,
                preparation.root_thread_id,
                preparation.thread_id,
                preparation.turn_id,
            ):
                raise WebRuntimeError(
                    "This browser is not attached to the exact turn for this interaction.",
                    code="request_not_owned",
                    status=409,
                )
        else:
            self._ports.require_connected_writer(
                client_id,
                preparation.root_thread_id,
            )
        submission = self._inbox.submit_response(
            preparation,
            action=action,
            answers=answers,
        )
        self._ports.publish_changes(submission.changes)
        return {
            "accepted": True,
            "request_id": submission.request_key,
            "status": submission.status,
        }

    def retire_backend_epoch_after_stop(
        self,
    ) -> WebInteractionBackendEpochRetirement:
        retirement = self._inbox.retire_backend_epoch_after_stop()
        self._ports.publish_changes(retirement.changes)
        return retirement

    def auto_resolve_request(
        self,
        request_key: str,
        backend_epoch: int,
        generation: int,
    ) -> bool:
        """Submit one exact timer-owned user-input auto-resolution."""

        normalized_request_key = str(request_key or "").strip()
        transaction = self._inbox.prepare_auto_resolution(
            normalized_request_key,
            backend_epoch,
            generation,
        )
        if transaction.outcome == "missing":
            return False
        if transaction.response is None:
            return True
        try:
            submission = self._inbox.submit_response(
                transaction.response,
                action="auto_resolve",
                answers=None,
            )
            self._ports.publish_changes(submission.changes)
        except WebInteractionInboxError as exc:
            self._ports.publish_changes(exc.changes)
            logger.warning(
                "Web user-input auto-resolution was not confirmed: request=%s",
                normalized_request_key,
                exc_info=True,
            )
        return True
