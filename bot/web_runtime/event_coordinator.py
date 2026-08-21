"""RuntimeLoop-owned Web notification and reconciliation transaction.

This coordinator owns event ordering plus the process-local single-flight handoff
for detached notification projection. Runtime interest, pending interactions,
thread read state, operation recovery, and projection revisions remain in their
existing owners. Child lifecycle is not remapped into a root; Tasks are derived
only from collaboration items recorded in the parent thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from bot.runtime_loop import RuntimeContextGuard
from bot.runtime_state import (
    BACKEND_THREAD_STATUS_NOT_LOADED,
    is_confirmed_inactive_backend_thread_status,
)
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
)
from bot.web_runtime.interaction_inbox import (
    WebInteractionChange,
    WebInteractionMutation,
    WebInteractionResolution,
)
from bot.web_runtime.notification_projection import (
    WebNotificationProjectionReceipt,
    project_notification,
)
from bot.web_runtime.projection import project_goal_payload
from bot.web_runtime.runtime_notice import project_runtime_notice
from bot.web_runtime.thread_read_model import (
    WebThreadNotificationUpdate,
    WebThreadReadObservationReceipt,
)


logger = logging.getLogger(__name__)
_RECONCILE_NOTIFICATION_METHODS = frozenset(
    {
        "error",
        "thread/archived",
        "thread/closed",
        "thread/compacted",
        "thread/deleted",
        "thread/settings/updated",
        "thread/started",
        "thread/unarchived",
        "model/rerouted",
    }
)


class WebEventRuntimeInterestPort(Protocol):
    def mark_subscription_absent(self, thread_id: str) -> None: ...
    def has_managed_interest(self, thread_id: str) -> bool: ...
    def confirm_thread_scoped_notification(
        self,
        thread_id: str,
        *,
        method: str,
    ) -> bool: ...


class WebEventInteractionInboxPort(Protocol):
    def resolve_exact(
        self,
        identity: ServerRequestIdentity,
    ) -> WebInteractionResolution: ...
    def hide_for_lifecycle(
        self,
        thread_id: str,
        *,
        reason: str,
        turn_id: str = "",
        preserve_turn_id: bool = False,
    ) -> WebInteractionMutation: ...


class WebEventReadModelPort(Protocol):
    def observe_notification(self, thread_id: str) -> int: ...
    def capture_observation(
        self,
        thread_id: str,
    ) -> WebThreadReadObservationReceipt: ...
    def observation_is_current(
        self,
        receipt: WebThreadReadObservationReceipt,
    ) -> bool: ...
    def apply_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> WebThreadNotificationUpdate | None: ...
    def turns(self, thread_id: str) -> tuple[dict[str, Any], ...]: ...
    def forget_closed_thread(self, thread_id: str) -> None: ...
    def forget_thread(self, thread_id: str) -> None: ...
    def collaboration_turns(
        self,
        thread_id: str,
    ) -> tuple[dict[str, Any], ...]: ...
    def cwd(self, thread_id: str) -> str: ...


class WebEventOperationPort(Protocol):
    def has_unknown_mutation(self, thread_id: str) -> bool: ...
    def reconcile_unknown_from_turns(
        self,
        thread_id: str,
        turns: list[dict[str, Any]],
    ) -> bool: ...


class WebEventPromptResultPort(Protocol):
    def reconcile_prompt_results_from_turns(
        self,
        thread_id: str,
        turns: list[dict[str, Any]],
    ) -> bool: ...


class WebEventLifecyclePort(Protocol):
    def maybe_release_runtime(
        self,
        thread_id: str,
        *,
        known_non_active: bool = False,
    ) -> None: ...


class WebEventAttachmentPort(Protocol):
    def delete_scope(self, scope_key: str) -> object: ...


class WebEventSelectionCleanupPort(Protocol):
    def __call__(self, thread_id: str, *, reason: str) -> None: ...


class WebEventAttachmentUrlPort(Protocol):
    def __call__(self, path: str, *, cwd: str) -> str: ...


@dataclass(frozen=True, slots=True)
class WebRuntimeEventPorts:
    runtime_interest: WebEventRuntimeInterestPort
    interaction_inbox: WebEventInteractionInboxPort
    read_model: WebEventReadModelPort
    operations: WebEventOperationPort
    prompt_results: WebEventPromptResultPort
    lifecycle: WebEventLifecyclePort
    attachments: WebEventAttachmentPort
    clear_thread_selection_facts: WebEventSelectionCleanupPort
    attachment_url_for_path: WebEventAttachmentUrlPort
    attachment_url_for_id: Callable[[str], str]
    publish_interaction_changes: Callable[
        [tuple[WebInteractionChange, ...]],
        None,
    ]
    publish_projection: Callable[..., dict[str, Any]]
    projection_coordinates: Callable[[], dict[str, Any]]
    schedule_notification_projection: Callable[
        [WebNotificationProjectionReceipt],
        None,
    ]
    schedule_attachment_cleanup: Callable[[str], None]


class WebRuntimeEventCoordinator:
    """Apply events in order and coordinate detached projection receipts."""

    def __init__(
        self,
        *,
        ports: WebRuntimeEventPorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web runtime events require a RuntimeLoop context guard")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._next_projection_sequence = 0
        self._projection_flights: dict[
            str,
            WebNotificationProjectionReceipt,
        ] = {}
        self._projection_successors: dict[
            str,
            WebNotificationProjectionReceipt,
        ] = {}

    def remove_resolved_server_request(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        self._runtime_context_guard()
        key = identity.request_key
        target_thread_id = identity.thread_id
        if not key or not target_thread_id:
            return ServerRequestLocalRemoval("invalid", key, target_thread_id)
        ports = self._ports
        resolution = ports.interaction_inbox.resolve_exact(identity)
        if resolution.outcome == "missing":
            return ServerRequestLocalRemoval("missing", key, target_thread_id)
        if resolution.outcome == "mismatch":
            return ServerRequestLocalRemoval("mismatch", key, target_thread_id)
        if resolution.outcome == "not_resolved":
            return ServerRequestLocalRemoval("not_resolved", key, target_thread_id)
        root_thread_id = str(resolution.owner_thread_id or "").strip()
        ports.publish_interaction_changes(resolution.changes)
        if root_thread_id:
            ports.lifecycle.maybe_release_runtime(root_thread_id)
        return ServerRequestLocalRemoval(
            "removed",
            key,
            target_thread_id,
            root_thread_id,
        )

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        self._runtime_context_guard()
        if method == "serverRequest/resolved":
            # The ordered server-request coordinator owns canonical settlement.
            return
        ports = self._ports
        runtime_notice = project_runtime_notice(method, params)
        if method == "warning":
            if runtime_notice is not None:
                ports.publish_projection(
                    "runtime_notice",
                    thread_id=runtime_notice.thread_id,
                    reason=method,
                    detail=dict(runtime_notice.detail),
                )
            return
        thread_id = self._notification_thread_id(method, params)
        status = (
            params.get("status")
            if method == "thread/status/changed"
            and isinstance(params.get("status"), dict)
            else {}
        )
        subscription_became_absent = bool(
            method == "thread/closed"
            or (
                method == "thread/status/changed"
                and str(status.get("type", "") or "").strip()
                == BACKEND_THREAD_STATUS_NOT_LOADED
            )
        )
        if thread_id:
            # Advance the cache fence before any notification-owned mutation
            # or projection. A read prepared before this point can no longer
            # replace the newer observation when its RPC response arrives.
            ports.read_model.observe_notification(thread_id)
            if subscription_became_absent:
                ports.runtime_interest.mark_subscription_absent(thread_id)
            else:
                ports.runtime_interest.confirm_thread_scoped_notification(
                    thread_id,
                    method=method,
                )
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
            mutation = ports.interaction_inbox.hide_for_lifecycle(
                thread_id,
                reason=method,
                turn_id=str(turn.get("id", "") or "").strip(),
                preserve_turn_id=(method == "turn/started"),
            )
            ports.publish_interaction_changes(mutation.changes)
        elif method in {"thread/closed", "thread/archived", "thread/deleted"}:
            mutation = ports.interaction_inbox.hide_for_lifecycle(
                thread_id,
                reason=method,
            )
            ports.publish_interaction_changes(mutation.changes)
        update = (
            self._apply_live_notification(method, params)
            if thread_id and ports.runtime_interest.has_managed_interest(thread_id)
            else None
        )
        if thread_id and ports.operations.has_unknown_mutation(thread_id):
            ports.operations.reconcile_unknown_from_turns(
                thread_id,
                list(ports.read_model.turns(thread_id)),
            )
        if thread_id:
            ports.prompt_results.reconcile_prompt_results_from_turns(
                thread_id,
                list(ports.read_model.turns(thread_id)),
            )
        if method == "turn/completed" and thread_id:
            ports.lifecycle.maybe_release_runtime(
                thread_id,
                known_non_active=True,
            )
        elif method == "thread/status/changed" and thread_id:
            status_type = str(status.get("type", "") or "").strip()
            if is_confirmed_inactive_backend_thread_status(status_type):
                ports.lifecycle.maybe_release_runtime(
                    thread_id,
                    known_non_active=True,
                )
        elif method == "thread/closed" and thread_id:
            ports.read_model.forget_closed_thread(thread_id)
            ports.lifecycle.maybe_release_runtime(
                thread_id,
                known_non_active=True,
            )
        elif method in {"thread/archived", "thread/deleted"} and thread_id:
            if method == "thread/deleted":
                try:
                    self.schedule_attachment_cleanup(thread_id)
                except Exception:
                    logger.exception(
                        "Web attachment cleanup could not be scheduled after "
                        "authoritative thread deletion: %s",
                        thread_id,
                    )
            # These notifications prove only a lifecycle observation.  They
            # carry no Focus mutation id, so they cannot settle an unknown
            # archive/delete attempt without risking same-operation ABA.
            self._drop_thread_after_lifecycle(thread_id)
        if thread_id and method in {
            "thread/closed",
            "thread/archived",
            "thread/deleted",
        }:
            self._projection_successors.pop(thread_id, None)

        detail: dict[str, Any] | None = None
        if update is not None:
            if update.raw_turn is None:
                detail = self._project_immediate_update(update)
                self._refresh_pending_projection(thread_id)
            else:
                self._enqueue_projection(update)
        elif (
            thread_id
            and method
            not in {"thread/closed", "thread/archived", "thread/deleted"}
        ):
            # Every thread notification advances the read-observation fence.
            # If a detached projection is already in flight, retain its latest
            # complete cache view under the new observation rather than
            # letting an otherwise ignored notification strand that update.
            self._refresh_pending_projection(thread_id)
        if detail is not None:
            ports.publish_projection(
                "thread_delta",
                thread_id=thread_id,
                reason=method,
                detail=detail,
            )
        elif method in _RECONCILE_NOTIFICATION_METHODS:
            ports.publish_projection(
                "thread_invalidated",
                thread_id=thread_id,
                reason=method,
            )
        if runtime_notice is not None:
            ports.publish_projection(
                "runtime_notice",
                thread_id=runtime_notice.thread_id,
                reason=method,
                detail=dict(runtime_notice.detail),
            )

    def drop_thread_after_lifecycle(self, thread_id: str) -> None:
        self._runtime_context_guard()
        self._drop_thread_after_lifecycle(thread_id)

    def _drop_thread_after_lifecycle(self, thread_id: str) -> None:
        ports = self._ports
        ports.clear_thread_selection_facts(
            thread_id,
            reason="web_lifecycle_selection_cleared",
        )
        ports.read_model.forget_thread(thread_id)
        ports.lifecycle.maybe_release_runtime(
            thread_id,
            known_non_active=True,
        )

    def _apply_live_notification(
        self,
        method: str,
        params: dict[str, Any],
    ) -> WebThreadNotificationUpdate | None:
        thread_id = str(params.get("threadId", "") or "").strip()
        if not thread_id:
            return None
        ports = self._ports
        update = ports.read_model.apply_notification(method, params)
        return update

    @staticmethod
    def _project_immediate_update(
        update: WebThreadNotificationUpdate,
    ) -> dict[str, Any]:
        detail = dict(update.detail)
        if update.goal_changed:
            detail["goal"] = project_goal_payload(update.goal)
        return detail

    def project_notification(
        self,
        receipt: WebNotificationProjectionReceipt,
    ) -> dict[str, Any]:
        """Run detached CPU/attachment projection outside RuntimeLoop."""

        ports = self._ports
        return project_notification(
            receipt,
            attachment_url_for_path=lambda path: ports.attachment_url_for_path(
                path,
                cwd=receipt.cwd,
            ),
            attachment_url_for_id=ports.attachment_url_for_id,
        )

    def settle_notification_projection(
        self,
        receipt: WebNotificationProjectionReceipt,
        detail: dict[str, Any] | None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Publish one exact receipt and admit its successor independently.

        A successor must obtain a new service-ingress receipt.  Reusing the
        current worker's receipt would let notifications arriving after
        shutdown admission closes extend presentation work indefinitely.
        """

        self._runtime_context_guard()
        thread_id = receipt.thread_id
        current = self._projection_flights.get(thread_id)
        if current is not receipt:
            return None
        successor = self._projection_successors.pop(thread_id, None)
        if successor is None:
            self._projection_flights.pop(thread_id, None)
        else:
            self._projection_flights[thread_id] = successor

        ports = self._ports
        coordinates = ports.projection_coordinates()
        current_epoch = str(coordinates.get("runtime_epoch", "") or "")
        receipt_is_current = bool(
            error is None
            and detail is not None
            and current_epoch == receipt.runtime_epoch
            and ports.read_model.observation_is_current(receipt.observation)
        )
        if receipt_is_current:
            ports.publish_projection(
                "thread_delta",
                thread_id=thread_id,
                reason=receipt.method,
                detail=detail,
            )
        elif error is not None and successor is None:
            logger.error(
                "Web notification projection failed: thread=%s method=%s: %s",
                thread_id,
                receipt.method,
                error,
            )
            ports.publish_projection(
                "thread_invalidated",
                thread_id=thread_id,
                reason=receipt.method,
            )
        if successor is None:
            return
        try:
            ports.schedule_notification_projection(successor)
        except Exception:
            if self._projection_flights.get(thread_id) is successor:
                self._projection_flights.pop(thread_id, None)
                self._projection_successors.pop(thread_id, None)
            logger.exception(
                "Web notification successor could not be admitted: "
                "thread=%s method=%s",
                thread_id,
                successor.method,
            )
            ports.publish_projection(
                "thread_invalidated",
                thread_id=thread_id,
                reason=successor.method,
            )

    def run_attachment_cleanup(self, scope_key: str) -> None:
        """Delete one rebuildable attachment scope outside RuntimeLoop."""

        try:
            self._ports.attachments.delete_scope(scope_key)
        except Exception:
            logger.exception(
                "Web attachment cleanup failed after authoritative thread deletion: %s",
                str(scope_key or "").removeprefix("thread:"),
            )

    def schedule_attachment_cleanup(self, thread_id: str) -> None:
        """Admit physical cleanup without making it lifecycle authority."""

        self._runtime_context_guard()
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return
        self._ports.schedule_attachment_cleanup(
            f"thread:{normalized_thread_id}"
        )

    def _enqueue_projection(self, update: WebThreadNotificationUpdate) -> None:
        receipt = self._freeze_projection(update)
        thread_id = receipt.thread_id
        if thread_id in self._projection_flights:
            self._projection_successors[thread_id] = receipt
            return
        self._projection_flights[thread_id] = receipt
        try:
            self._ports.schedule_notification_projection(receipt)
        except Exception:
            if self._projection_flights.get(thread_id) is receipt:
                self._projection_flights.pop(thread_id, None)
                self._projection_successors.pop(thread_id, None)
            logger.exception(
                "Web notification projection could not be scheduled: "
                "thread=%s method=%s",
                thread_id,
                update.method,
            )
            self._ports.publish_projection(
                "thread_invalidated",
                thread_id=thread_id,
                reason=update.method,
            )

    def _refresh_pending_projection(self, thread_id: str) -> None:
        basis = self._projection_successors.get(thread_id)
        if basis is None:
            basis = self._projection_flights.get(thread_id)
        if basis is None:
            return
        self._projection_successors[thread_id] = self._freeze_projection(
            basis.update
        )

    def _freeze_projection(
        self,
        update: WebThreadNotificationUpdate,
    ) -> WebNotificationProjectionReceipt:
        ports = self._ports
        coordinates = ports.projection_coordinates()
        self._next_projection_sequence += 1
        return WebNotificationProjectionReceipt(
            sequence=self._next_projection_sequence,
            method=update.method,
            thread_id=update.thread_id,
            runtime_epoch=str(coordinates.get("runtime_epoch", "") or ""),
            observation=ports.read_model.capture_observation(update.thread_id),
            update=update,
            cwd=ports.read_model.cwd(update.thread_id),
            collaboration_turns=(
                ports.read_model.collaboration_turns(update.thread_id)
            ),
        )

    @staticmethod
    def _notification_thread_id(method: str, params: dict[str, Any]) -> str:
        thread_id = str(params.get("threadId", "") or "").strip()
        if thread_id or method != "thread/started":
            return thread_id
        thread = params.get("thread")
        if not isinstance(thread, dict):
            return ""
        return str(thread.get("id", "") or "").strip()
