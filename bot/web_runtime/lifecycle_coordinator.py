"""RuntimeLoop-owned Web document and subscription lifecycle coordinator."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Protocol

from bot.adapters.base import ThreadSnapshot
from bot.runtime_loop import RuntimeContextGuard
from bot.runtime_state import is_confirmed_inactive_backend_thread_status
from bot.stores.interaction_lease_store import InteractionLease
from bot.thread_runtime_authority import (
    PendingThreadUnsubscribe,
    PreparedThreadUnsubscribe,
    ThreadUnsubscribeInProgress,
    ThreadUnsubscribeOutcomeUnknown,
)
from bot.web_runtime.document_registry import WebDocumentMutation
from bot.web_runtime.interest import WebRuntimeInterestSnapshot
from bot.web_runtime.interaction_inbox import WebInteractionChange, WebInteractionMutation
from bot.web_runtime.selection_coordinator import WebSelectionConvergence
from bot.web_runtime.contract import WebRuntimeError


logger = logging.getLogger(__name__)
WebRuntimeLifecyclePhase = Literal[
    "running",
    "preparing_shutdown",
    "shutdown_prepared",
    "shutdown_finished",
]
WebRuntimeCleanupProbeDisposition = Literal[
    "blocked",
    "active",
    "already_absent",
    "unsubscribe",
]


@dataclass(frozen=True, slots=True)
class WebRuntimeCleanupPreparation:
    """Immutable Web-interest and backend observation for one passive probe."""

    thread_id: str
    connection_generation: int
    interest: WebRuntimeInterestSnapshot
    subscription_current: bool


@dataclass(frozen=True, slots=True)
class WebRuntimeCleanupProbe:
    """Loop-external point evidence used only by its exact preparation."""

    preparation: WebRuntimeCleanupPreparation
    disposition: WebRuntimeCleanupProbeDisposition
    prepared_service_lease_release: object | None = None


@dataclass(frozen=True, slots=True)
class WebRuntimeCleanupClaim:
    """Exact subscription transition claimed after the passive probe."""

    preparation: WebRuntimeCleanupPreparation
    prepared_unsubscribe: PreparedThreadUnsubscribe
    execute_unsubscribe: bool
    claimed_interest: WebRuntimeInterestSnapshot
    prepared_service_lease_release: object | None


@dataclass(frozen=True, slots=True)
class WebRuntimeCleanupRelease:
    """Known-absent subscription retaining its claim through lease release."""

    claim: WebRuntimeCleanupClaim
    pending_unsubscribe: PendingThreadUnsubscribe


@dataclass(slots=True)
class _WebRuntimeCleanupFlight:
    """RuntimeLoop-owned coalescing state for one thread cleanup worker."""

    rerun_requested: bool = False


class WebLifecycleOperationPort(Protocol):
    def owned_main_turn_thread_ids(self, client_id: str) -> tuple[str, ...]: ...


class WebLifecycleDocumentPort(Protocol):
    def mark_connected(self, client_id: str) -> WebDocumentMutation: ...
    def mark_transport_disconnected(self, client_id: str) -> WebDocumentMutation: ...
    def mark_document_reissued(self, client_id: str) -> WebDocumentMutation: ...
    def client_ids(self) -> tuple[str, ...]: ...
    def clear(self) -> None: ...


class WebLifecycleRuntimeInterestPort(Protocol):
    def has_interest(self, thread_id: str) -> bool: ...
    def is_unknown(self, thread_id: str) -> bool: ...
    def has_managed_interest(self, thread_id: str) -> bool: ...
    def has_desired_clients(self, thread_id: str) -> bool: ...
    def subscription_is_current(self, thread_id: str) -> bool: ...
    def mark_unknown(self, thread_id: str, *, client_id: str = "") -> None: ...
    def mark_unsubscribe_unknown(self, thread_id: str) -> None: ...
    def mark_confirmed(self, thread_id: str, *, client_id: str = "") -> None: ...
    def mark_subscription_absent(self, thread_id: str) -> bool: ...
    def snapshot(self, thread_id: str) -> WebRuntimeInterestSnapshot | None: ...
    def forget(self, thread_id: str) -> None: ...
    def clear(self) -> None: ...
    def backend_disconnected(self) -> int: ...


class WebLifecycleInteractionInboxPort(Protocol):
    def root_ids_for_client(self, client_id: str) -> tuple[str, ...]: ...
    def fail_close_client(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> WebInteractionMutation: ...
    def has_for_root(self, root_thread_id: str) -> bool: ...
    def backend_disconnected(self) -> WebInteractionMutation: ...


class WebLifecycleReadModelPort(Protocol):
    def forget_runtime(self, thread_id: str) -> None: ...
    def backend_disconnected(self) -> None: ...


class WebLifecycleSelectionPort(Protocol):
    def lose_document(self, client_id: str) -> WebSelectionConvergence: ...


class WebLifecycleInteractionLeasePort(Protocol):
    def load(self, thread_id: str) -> InteractionLease | None: ...


@dataclass(frozen=True, slots=True)
class WebRuntimeLifecyclePorts:
    operations: WebLifecycleOperationPort
    documents: WebLifecycleDocumentPort
    runtime_interest: WebLifecycleRuntimeInterestPort
    interaction_inbox: WebLifecycleInteractionInboxPort
    read_model: WebLifecycleReadModelPort
    selection: WebLifecycleSelectionPort
    interaction_leases: WebLifecycleInteractionLeasePort
    require_client_id: Callable[[str], str]
    read_thread: Callable[..., ThreadSnapshot]
    list_loaded_thread_ids: Callable[..., list[str]]
    capture_connection_generation: Callable[[], int]
    run_if_connection_generation: Callable[[int, Callable[[], Any]], Any]
    prepare_unsubscribe_thread: Callable[..., PreparedThreadUnsubscribe]
    execute_prepared_unsubscribe_thread: Callable[[PreparedThreadUnsubscribe], None]
    settle_prepared_unsubscribe_thread: Callable[..., PendingThreadUnsubscribe]
    abandon_prepared_unsubscribe_thread: Callable[[PreparedThreadUnsubscribe], None]
    prepare_service_thread_runtime_lease_release: Callable[[str], object | None]
    release_prepared_service_thread_runtime_lease: Callable[[object], bool]
    schedule_runtime_cleanup: Callable[[str, bool], None]
    thread_subscribers: Callable[[str], tuple[tuple[str, str], ...]]
    has_external_pending_interaction_for_root: Callable[[str], bool]
    shared_interaction_reprojection_roots: Callable[[str], tuple[str, ...]]
    publish_interaction_changes: Callable[[tuple[WebInteractionChange, ...]], None]
    publish_projection: Callable[..., dict[str, Any]]


class WebRuntimeLifecycleCoordinator:
    """Order Web document loss/reconnect and shared-runtime settlement."""

    def __init__(
        self,
        *,
        ports: WebRuntimeLifecyclePorts,
        runtime_context_guard: RuntimeContextGuard,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web runtime lifecycle requires a RuntimeLoop context guard")
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._phase: WebRuntimeLifecyclePhase = "running"
        self._cleanup_flights: dict[str, _WebRuntimeCleanupFlight] = {}

    def has_local_runtime_interest(self, thread_id: str) -> bool:
        self._runtime_context_guard()
        normalized = self._thread_id(thread_id)
        return bool(normalized) and self._ports.runtime_interest.has_interest(normalized)

    def retains_runtime(self, thread_id: str) -> bool:
        self._runtime_context_guard()
        normalized = self._thread_id(thread_id)
        return bool(normalized) and self._ports.runtime_interest.has_interest(normalized)

    def has_pending_for_thread(self, thread_id: str) -> bool:
        self._runtime_context_guard()
        return self._has_pending_for_thread(thread_id)

    def client_disconnected(self, client_id: str) -> None:
        self._runtime_context_guard()
        self._client_disconnected(client_id)

    def _client_disconnected(self, client_id: str) -> None:
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return
        ports = self._ports
        # A document is presentation and delivery state. Losing it does not
        # mutate or extend the active main-turn lease; matching upstream turn
        # completion remains the only normal release authority.
        self._client_transport_disconnected(normalized_client_id)
        convergence = ports.selection.lose_document(normalized_client_id)
        self._settle_runtime_cleanup_candidates(convergence.runtime_cleanup_thread_ids)

    def client_transport_disconnected(self, client_id: str) -> None:
        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return
        self._client_transport_disconnected(normalized_client_id)

    def client_document_reissued(self, client_id: str) -> None:
        """Revoke authority inherited from a replaced browser document."""

        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return
        self._ports.documents.mark_document_reissued(normalized_client_id)
        self._fail_close_client_delivery(normalized_client_id)

    def _client_transport_disconnected(
        self,
        normalized_client_id: str,
    ) -> None:
        self._ports.documents.mark_transport_disconnected(normalized_client_id)
        self._fail_close_client_delivery(normalized_client_id)

    def _fail_close_client_delivery(self, normalized_client_id: str) -> None:
        ports = self._ports
        main_turn_roots = set(
            ports.operations.owned_main_turn_thread_ids(normalized_client_id)
        )
        inbox_roots = set(
            ports.interaction_inbox.root_ids_for_client(normalized_client_id)
        )
        owned_roots = tuple(sorted(main_turn_roots | inbox_roots))
        # A browser document owns only its local projection. The app-server
        # callback remains upstream-owned and can be replayed on resume.
        for thread_id in owned_roots:
            mutation = ports.interaction_inbox.fail_close_client(
                normalized_client_id,
                thread_id,
            )
            ports.publish_interaction_changes(mutation.changes)

    def client_connected(self, client_id: str) -> None:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_client_id(client_id)
        if self._phase != "running":
            raise WebRuntimeError(
                "Focus is shutting down and cannot admit a browser document.",
                code="service_shutting_down",
                status=503,
            )
        ports = self._ports
        connection = ports.documents.mark_connected(normalized_client_id)
        if connection.outcome != "changed":
            return
        roots = ports.shared_interaction_reprojection_roots(normalized_client_id)
        if not roots:
            return
        ports.publish_interaction_changes(
            tuple(
                WebInteractionChange(root_thread_id, "document_connected")
                for root_thread_id in roots
            )
        )

    def prepare_shutdown(self) -> None:
        self._runtime_context_guard()
        self._prepare_shutdown()

    def _prepare_shutdown(self) -> None:
        if self._phase == "shutdown_finished":
            return
        if self._phase == "shutdown_prepared":
            return
        self._phase = "preparing_shutdown"
        ports = self._ports
        client_ids = set(ports.documents.client_ids())
        for client_id in sorted(client_ids):
            self._client_disconnected(client_id)
        self._phase = "shutdown_prepared"

    def finish_shutdown(self) -> None:
        self._runtime_context_guard()
        self._finish_shutdown()

    def _finish_shutdown(self) -> None:
        if self._phase == "shutdown_finished":
            return
        if self._phase != "shutdown_prepared":
            raise RuntimeError(
                "Web runtime shutdown cannot finish before prepare succeeds"
            )
        ports = self._ports
        # Active-turn leases are bound to this service PID and are not
        # reconstructed as durable Web writers after restart.
        ports.runtime_interest.clear()
        ports.documents.clear()
        self._phase = "shutdown_finished"

    def shutdown(self) -> None:
        self._runtime_context_guard()
        self._prepare_shutdown()
        self._finish_shutdown()

    def backend_disconnected(self) -> None:
        self._runtime_context_guard()
        ports = self._ports
        mutation = ports.interaction_inbox.backend_disconnected()
        ports.runtime_interest.backend_disconnected()
        ports.read_model.backend_disconnected()
        ports.publish_interaction_changes(mutation.changes)
        ports.publish_projection(
            "backend_disconnected",
            reason="app_server_disconnected",
        )

    def maybe_release_runtime(
        self,
        root_thread_id: str,
        *,
        known_non_active: bool = False,
    ) -> None:
        self._runtime_context_guard()
        self._maybe_release_web_runtime(
            root_thread_id,
            known_non_active=known_non_active,
        )

    def reconcile_external_pending_interaction_resolved(
        self,
        root_thread_id: str,
    ) -> None:
        self._runtime_context_guard()
        root_id = self._thread_id(root_thread_id)
        if root_id:
            self._maybe_release_web_runtime(root_id)

    def maybe_release_web_runtime(
        self,
        thread_id: str,
        *,
        known_non_active: bool = False,
    ) -> None:
        self._runtime_context_guard()
        self._maybe_release_web_runtime(thread_id, known_non_active=known_non_active)

    def _maybe_release_web_runtime(
        self,
        thread_id: str,
        *,
        known_non_active: bool = False,
    ) -> None:
        self._schedule_runtime_cleanup(
            thread_id,
            known_non_active=known_non_active,
        )

    def reconcile_uncertain_runtime_interest(self, thread_id: str) -> None:
        self._runtime_context_guard()
        self._reconcile_uncertain_runtime_interest(thread_id)

    def _reconcile_uncertain_runtime_interest(self, thread_id: str) -> None:
        self._schedule_runtime_cleanup(thread_id, known_non_active=False)

    def settle_runtime_cleanup_candidates(self, thread_ids: Iterable[str]) -> None:
        self._runtime_context_guard()
        self._settle_runtime_cleanup_candidates(thread_ids)

    def _settle_runtime_cleanup_candidates(self, thread_ids: Iterable[str]) -> None:
        for thread_id in sorted(set(thread_ids)):
            self._schedule_runtime_cleanup(thread_id, known_non_active=False)

    def _schedule_runtime_cleanup(
        self,
        thread_id: str,
        *,
        known_non_active: bool,
    ) -> None:
        _ = known_non_active
        normalized_thread_id = self._thread_id(thread_id)
        if not normalized_thread_id or self._phase != "running":
            return
        current_flight = self._cleanup_flights.get(normalized_thread_id)
        if current_flight is not None:
            current_flight.rerun_requested = True
            return
        flight = _WebRuntimeCleanupFlight()
        self._cleanup_flights[normalized_thread_id] = flight
        try:
            # A lifecycle notification is only a scheduling hint.  Its terminal
            # observation is not an exact receipt and must never let this worker
            # skip its generation-pinned backend probe.
            self._ports.schedule_runtime_cleanup(
                normalized_thread_id,
                False,
            )
        except Exception:
            if self._cleanup_flights.get(normalized_thread_id) is flight:
                self._cleanup_flights.pop(normalized_thread_id, None)
            # Runtime cleanup is subtractive. Shutdown may close external
            # ingress between the local candidate and this dispatch; retain
            # the runtime instead of failing document/notification settlement.
            logger.debug(
                "Unable to schedule Web runtime cleanup; retaining runtime: thread=%s",
                normalized_thread_id[:12],
                exc_info=True,
            )

    def finish_runtime_cleanup(self, thread_id: str) -> None:
        """Retire one worker and admit at most one coalesced successor."""

        self._runtime_context_guard()
        normalized_thread_id = self._thread_id(thread_id)
        if not normalized_thread_id:
            return
        flight = self._cleanup_flights.pop(normalized_thread_id, None)
        if (
            flight is None
            or not flight.rerun_requested
            or self._phase != "running"
        ):
            return
        self._schedule_runtime_cleanup(
            normalized_thread_id,
            known_non_active=False,
        )

    def prepare_runtime_cleanup(
        self,
        thread_id: str,
        *,
        known_non_active: bool = False,
    ) -> WebRuntimeCleanupPreparation | None:
        """Capture one exact passive cleanup observation in RuntimeLoop."""

        _ = known_non_active
        self._runtime_context_guard()
        normalized_thread_id = self._thread_id(thread_id)
        if not normalized_thread_id or self._phase != "running":
            return None
        ports = self._ports
        interest = ports.runtime_interest.snapshot(normalized_thread_id)
        if interest is None:
            return None
        if interest.desired_client_ids or self._has_pending_for_thread(
            normalized_thread_id
        ):
            return None
        if ports.thread_subscribers(normalized_thread_id):
            # Another surface owns the live service subscription. Web can drop
            # only its own interest record; it has no unsubscribe authority.
            ports.runtime_interest.forget(normalized_thread_id)
            return None
        if not interest.ever_confirmed and interest.outcome != "unknown":
            return None
        return WebRuntimeCleanupPreparation(
            thread_id=normalized_thread_id,
            connection_generation=ports.capture_connection_generation(),
            interest=interest,
            subscription_current=ports.runtime_interest.subscription_is_current(
                normalized_thread_id
            ),
        )

    def execute_runtime_cleanup_probe(
        self,
        prepared: WebRuntimeCleanupPreparation,
    ) -> WebRuntimeCleanupProbe:
        """Perform only file/adapter reads on the external transaction thread."""

        self._validate_cleanup_preparation(prepared)
        ports = self._ports
        try:
            interaction_lease = ports.interaction_leases.load(prepared.thread_id)
        except Exception:
            logger.exception(
                "Unable to inspect owner before Web runtime cleanup: thread=%s",
                prepared.thread_id[:12],
            )
            return WebRuntimeCleanupProbe(prepared, "blocked")
        if interaction_lease is not None and interaction_lease.holder.kind == "web":
            return WebRuntimeCleanupProbe(prepared, "blocked")

        disposition: WebRuntimeCleanupProbeDisposition | None = None
        subscription_already_absent = False
        interest = prepared.interest
        if interest.outcome == "unknown":
            loaded = prepared.thread_id in set(
                ports.list_loaded_thread_ids(
                    expected_connection_generation=prepared.connection_generation,
                )
            )
            if not loaded:
                disposition = "already_absent"
            elif interest.unsubscribe_outcome_unknown:
                # The prior unsubscribe may have taken effect. Seeing the
                # thread still loaded is not authority to replay it.
                return WebRuntimeCleanupProbe(prepared, "blocked")
        elif not prepared.subscription_current:
            # The local subscription fact already says no unsubscribe effect is
            # needed, but current backend status still decides whether releasing
            # the service holder is safe.
            subscription_already_absent = True

        if disposition is None:
            snapshot = ports.read_thread(
                prepared.thread_id,
                False,
                expected_connection_generation=prepared.connection_generation,
            )
            if not is_confirmed_inactive_backend_thread_status(
                snapshot.summary.status
            ):
                disposition = "active"
            elif subscription_already_absent:
                disposition = "already_absent"
            else:
                disposition = "unsubscribe"
        assert disposition is not None
        if disposition == "active":
            return WebRuntimeCleanupProbe(prepared, disposition)
        prepared_service_lease_release = (
            ports.prepare_service_thread_runtime_lease_release(
                prepared.thread_id
            )
        )
        return WebRuntimeCleanupProbe(
            prepared,
            disposition,
            prepared_service_lease_release,
        )

    def settle_runtime_cleanup_probe(
        self,
        prepared: WebRuntimeCleanupPreparation,
        probe: WebRuntimeCleanupProbe,
    ) -> WebRuntimeCleanupClaim | None:
        """Claim an exact unsubscribe transition if all local facts still match."""

        self._runtime_context_guard()
        self._validate_cleanup_probe(prepared, probe)
        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._claim_runtime_cleanup(prepared, probe),
        )

    def _claim_runtime_cleanup(
        self,
        prepared: WebRuntimeCleanupPreparation,
        probe: WebRuntimeCleanupProbe,
    ) -> WebRuntimeCleanupClaim | None:
        ports = self._ports
        if self._phase != "running" or probe.disposition in {"blocked", "active"}:
            return None
        if ports.runtime_interest.snapshot(prepared.thread_id) != prepared.interest:
            return None
        if (
            ports.runtime_interest.has_desired_clients(prepared.thread_id)
            or self._has_pending_for_thread(prepared.thread_id)
            or ports.thread_subscribers(prepared.thread_id)
        ):
            return None
        try:
            transition = ports.prepare_unsubscribe_thread(
                prepared.thread_id,
                expected_connection_generation=prepared.connection_generation,
            )
        except ThreadUnsubscribeInProgress:
            return None
        try:
            ports.runtime_interest.mark_subscription_absent(prepared.thread_id)
            claimed_interest = ports.runtime_interest.snapshot(prepared.thread_id)
            if claimed_interest is None:
                raise RuntimeError(
                    "Web runtime cleanup lost its claimed interest record"
                )
        except Exception:
            ports.abandon_prepared_unsubscribe_thread(transition)
            raise
        return WebRuntimeCleanupClaim(
            preparation=prepared,
            prepared_unsubscribe=transition,
            execute_unsubscribe=probe.disposition == "unsubscribe",
            claimed_interest=claimed_interest,
            prepared_service_lease_release=(
                probe.prepared_service_lease_release
            ),
        )

    def confirm_runtime_cleanup_unsubscribe_send(
        self,
        claim: WebRuntimeCleanupClaim,
    ) -> bool:
        """Recheck exact current local interest immediately before send."""

        self._runtime_context_guard()
        self._validate_cleanup_claim(claim)
        prepared = claim.preparation

        def confirm() -> bool:
            if self._runtime_cleanup_facts_match(
                prepared.thread_id,
                claim.claimed_interest,
            ):
                return True
            current_interest = self._ports.runtime_interest.snapshot(
                prepared.thread_id
            )
            if (
                self._phase == "running"
                and prepared.subscription_current
                and current_interest == claim.claimed_interest
            ):
                # No upstream effect was sent. Restore only the subscription
                # fact invalidated by this exact claim. A newer notification or
                # desire owns its successor snapshot and must not be overwritten.
                self._ports.runtime_interest.mark_confirmed(prepared.thread_id)
            return False

        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            confirm,
        )

    def execute_runtime_cleanup_unsubscribe(
        self,
        claim: WebRuntimeCleanupClaim,
    ) -> None:
        """Perform the claimed upstream effect outside RuntimeLoop."""

        self._validate_cleanup_claim(claim)
        if not claim.execute_unsubscribe:
            raise ValueError("already-absent cleanup has no unsubscribe effect")
        self._ports.execute_prepared_unsubscribe_thread(
            claim.prepared_unsubscribe
        )

    def settle_runtime_cleanup_unsubscribe(
        self,
        claim: WebRuntimeCleanupClaim,
        *,
        error: Exception | None = None,
    ) -> WebRuntimeCleanupRelease | None:
        """Settle upstream evidence while the exact generation remains live."""

        self._runtime_context_guard()
        self._validate_cleanup_claim(claim)
        prepared = claim.preparation

        def settle() -> WebRuntimeCleanupRelease | None:
            try:
                pending = self._ports.settle_prepared_unsubscribe_thread(
                    claim.prepared_unsubscribe,
                    upstream_succeeded=claim.execute_unsubscribe and error is None,
                    subscription_already_absent=(
                        not claim.execute_unsubscribe and error is None
                    ),
                    error=error,
                )
            except ThreadUnsubscribeOutcomeUnknown:
                if (
                    self._ports.runtime_interest.snapshot(prepared.thread_id)
                    == claim.claimed_interest
                ):
                    self._ports.runtime_interest.mark_unsubscribe_unknown(
                        prepared.thread_id
                    )
                return None
            except Exception as exc:
                # The authority re-raises the exact adapter exception only for
                # known-no-effect. Settlement/invariant failures are not absence
                # evidence and must not restore a subscription fact.
                if (
                    error is not None
                    and exc is error
                    and prepared.subscription_current
                    and self._ports.runtime_interest.snapshot(prepared.thread_id)
                    == claim.claimed_interest
                ):
                    self._ports.runtime_interest.mark_confirmed(prepared.thread_id)
                raise
            return WebRuntimeCleanupRelease(claim, pending)

        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            settle,
        )

    def confirm_runtime_cleanup_lease_release(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> bool:
        """Recheck exact current local interest before machine-holder CAS."""

        self._runtime_context_guard()
        self._validate_cleanup_release(release)
        claim = release.claim
        prepared = claim.preparation
        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._runtime_cleanup_facts_match(
                prepared.thread_id,
                claim.claimed_interest,
            ),
        )

    def release_runtime_cleanup_lease(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> bool:
        """Release the machine-visible service holder outside RuntimeLoop."""

        self._validate_cleanup_release(release)
        prepared_release = release.claim.prepared_service_lease_release
        if prepared_release is None:
            return True
        return bool(
            self._ports.release_prepared_service_thread_runtime_lease(
                prepared_release
            )
        )

    def settle_runtime_cleanup_lease_release_failure(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> None:
        """Retain exact-thread state when machine-holder release is uncertain."""

        self._runtime_context_guard()
        self._validate_cleanup_release(release)
        claim = release.claim
        prepared = claim.preparation

        def settle() -> None:
            def retain_local_state() -> None:
                if (
                    self._ports.runtime_interest.snapshot(prepared.thread_id)
                    == claim.claimed_interest
                ):
                    self._ports.runtime_interest.mark_unsubscribe_unknown(
                        prepared.thread_id
                    )

            release.pending_unsubscribe.commit_local_state(retain_local_state)

        self._ports.run_if_connection_generation(
            prepared.connection_generation,
            settle,
        )

    def finalize_runtime_cleanup_release(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> None:
        """Commit only in-memory Web cleanup under the exact generation gate."""

        self._runtime_context_guard()
        self._validate_cleanup_release(release)
        prepared = release.claim.preparation

        def commit() -> None:
            def local_commit() -> None:
                ports = self._ports
                if (
                    not self._runtime_cleanup_facts_match(
                        prepared.thread_id,
                        release.claim.claimed_interest,
                    )
                ):
                    return
                ports.runtime_interest.forget(prepared.thread_id)
                ports.read_model.forget_runtime(prepared.thread_id)

            release.pending_unsubscribe.commit_local_state(local_commit)

        self._ports.run_if_connection_generation(
            prepared.connection_generation,
            commit,
        )

    def abandon_runtime_cleanup_claim(
        self,
        claim: WebRuntimeCleanupClaim,
    ) -> None:
        """Retire a stale prepared claim after its generation fence rejects."""

        self._validate_cleanup_claim(claim)
        self._ports.abandon_prepared_unsubscribe_thread(
            claim.prepared_unsubscribe
        )

    def abandon_runtime_cleanup_release(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> None:
        """Retire the transition when lease release/final settlement cannot commit."""

        self._validate_cleanup_release(release)
        release.pending_unsubscribe.abandon_local_state()

    @staticmethod
    def _validate_cleanup_preparation(
        prepared: WebRuntimeCleanupPreparation,
    ) -> None:
        if not isinstance(prepared, WebRuntimeCleanupPreparation):
            raise TypeError("Web runtime cleanup preparation is required")

    @classmethod
    def _validate_cleanup_probe(
        cls,
        prepared: WebRuntimeCleanupPreparation,
        probe: WebRuntimeCleanupProbe,
    ) -> None:
        cls._validate_cleanup_preparation(prepared)
        if not isinstance(probe, WebRuntimeCleanupProbe):
            raise TypeError("Web runtime cleanup probe is required")
        if probe.preparation is not prepared:
            raise ValueError("Web runtime cleanup probe belongs to another preparation")

    @staticmethod
    def _validate_cleanup_claim(claim: WebRuntimeCleanupClaim) -> None:
        if not isinstance(claim, WebRuntimeCleanupClaim):
            raise TypeError("Web runtime cleanup claim is required")

    @staticmethod
    def _validate_cleanup_release(release: WebRuntimeCleanupRelease) -> None:
        if not isinstance(release, WebRuntimeCleanupRelease):
            raise TypeError("Web runtime cleanup release is required")

    def _has_pending_for_thread(self, thread_id: str) -> bool:
        normalized_thread_id = self._thread_id(thread_id)
        if not normalized_thread_id:
            return False
        if self._ports.has_external_pending_interaction_for_root(normalized_thread_id):
            return True
        return self._ports.interaction_inbox.has_for_root(normalized_thread_id)

    def _runtime_cleanup_facts_match(
        self,
        thread_id: str,
        expected_interest: WebRuntimeInterestSnapshot,
    ) -> bool:
        ports = self._ports
        return bool(
            self._phase == "running"
            and ports.runtime_interest.snapshot(thread_id) == expected_interest
            and not ports.runtime_interest.has_desired_clients(thread_id)
            and not self._has_pending_for_thread(thread_id)
            and not ports.thread_subscribers(thread_id)
        )

    @staticmethod
    def _thread_id(thread_id: object) -> str:
        return str(thread_id or "").strip()
