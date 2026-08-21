"""Connection-generation authority for Codex adapter ingress."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, TypeVar


logger = logging.getLogger(__name__)
_DEFAULT_TRANSPORT_DRAIN_TIMEOUT_SECONDS = 5.0
_SettleResult = TypeVar("_SettleResult")


class AdapterOutboundRequestBlocked(RuntimeError):
    """An ordinary app-server request cannot enter the current backend epoch."""


class AdapterOutboundRequestEpochLost(RuntimeError):
    """A request returned after its admitted backend epoch was invalidated."""


class AdapterBackendResetGenerationMismatchError(RuntimeError):
    """The requested reset generation is not the gate's current epoch."""

    def __init__(self, *, expected_generation: int, observed_generation: int) -> None:
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        super().__init__(
            "adapter ingress generation changed before reset fencing "
            f"(expected={expected_generation}, observed={observed_generation})"
        )


class AdapterBackendResetUnavailableError(RuntimeError):
    """The gate is already closed or reconciling another backend transition."""


@dataclass(frozen=True, slots=True)
class AdapterOutboundRequestPermit:
    """Opaque capability for one ordinary outbound request."""

    epoch: int
    method: str
    _authority_token: object


@dataclass(frozen=True, slots=True)
class AdapterIngressSnapshot:
    """Stable diagnostics for the current adapter ingress epoch."""

    latest_generation: int
    last_disconnected_generation: int
    backend_reset_blocked: bool
    cleanup_required: bool
    disconnect_cleanup_pending: bool


class AdapterIngressGate:
    """Own websocket-generation admission and backend-reset fencing.

    Adapter callbacks can be queued independently by the transport.  Arrival
    order is therefore not a liveness fact: only this gate decides whether a
    callback still belongs to the current connection epoch.  Backend reset
    uses the same owner so the old epoch stays closed until the replacement
    has been published successfully.
    """

    def __init__(
        self,
        *,
        invalidate_previous_epoch: Callable[[], None],
        activate_connection_epoch: Callable[[int], None],
    ) -> None:
        self._invalidate_previous_epoch = invalidate_previous_epoch
        self._activate_connection_epoch = activate_connection_epoch
        self._lock = threading.RLock()
        self._transport_condition = threading.Condition(self._lock)
        self._latest_generation = 0
        self._last_disconnected_generation = 0
        self._backend_reset_blocked = False
        self._backend_reset_invalidated = False
        self._cleanup_required = False
        # The websocket reader records a physical disconnect synchronously,
        # before its RuntimeLoop callback can be delayed behind other work.
        # Ordinary outbound requests stay closed until that callback finishes
        # the canonical connection-fact invalidation transaction.
        self._disconnect_cleanup_generation = 0
        self._outbound_epoch = 1
        self._outbound_authority_token = object()
        self._active_outbound_transports = 0
        # A successful invalidation commits this bit to false before a newer
        # generation may be admitted. A failed invalidation leaves both this
        # bit and ``cleanup_required`` true while ingress remains closed.
        self._connection_facts_live = False

    def snapshot(self) -> AdapterIngressSnapshot:
        with self._lock:
            return AdapterIngressSnapshot(
                latest_generation=self._latest_generation,
                last_disconnected_generation=self._last_disconnected_generation,
                backend_reset_blocked=self._backend_reset_blocked,
                cleanup_required=self._cleanup_required,
                disconnect_cleanup_pending=(
                    self._disconnect_cleanup_generation > 0
                ),
            )

    def resolve_published_backend_endpoint(
        self,
        ready_endpoint: Callable[[], str],
    ) -> str:
        """Return the READY endpoint only outside a closed reset epoch.

        Transport readiness alone is insufficient during backend replacement:
        the replacement generation must also pass validation, publication,
        and this gate's admission transaction.  Keeping the resolver behind
        the gate lock makes endpoint publication use the same authority as
        adapter callback ingress instead of reconstructing reset state in the
        control-plane layer.
        """

        with self._lock:
            if self._ordinary_ingress_closed_locked():
                return ""
            admitted_epoch = self._outbound_epoch

        # Do not call the adapter while holding the gate lock. Actual-send
        # validation acquires these locks in the opposite (RPC -> gate) order.
        # Recheck the exact epoch afterwards so a complete reset between the
        # two reads can never publish a stale endpoint.
        endpoint = str(ready_endpoint() or "").strip()
        with self._lock:
            if (
                self._ordinary_ingress_closed_locked()
                or admitted_epoch != self._outbound_epoch
            ):
                return ""
            return endpoint

    def issue_outbound_request(
        self,
        method: str,
    ) -> AdapterOutboundRequestPermit:
        """Issue an ordinary-request permit or fail before transport use.

        Callback ingress, endpoint publication, and ordinary outbound requests
        are three views of the same backend epoch.  Once reset or incomplete
        connection cleanup closes that epoch, a normal read is unsafe too:
        ``CodexRpcClient.request`` may otherwise start a backend on demand even
        though every notification from it would be rejected here.

        The adapter calls this immediately before its ordinary RPC boundary and
        confirms the same permit after a successful response. Backend lifecycle
        initialization uses a narrower capability and deliberately bypasses this
        method. A generation-pinned data-plane request still uses this permit
        when it may write or otherwise belongs to ordinary service admission;
        the generation pin and the ordinary epoch fence prove different facts.
        """

        normalized_method = str(method or "").strip()
        if not normalized_method:
            raise ValueError("outbound app-server method must not be empty")
        with self._lock:
            if self._ordinary_ingress_closed_locked():
                raise AdapterOutboundRequestBlocked(
                    "the current app-server epoch is closed; retry backend reset "
                    "or restart the Focus service before ordinary requests"
                )
            return AdapterOutboundRequestPermit(
                epoch=self._outbound_epoch,
                method=normalized_method,
                _authority_token=self._outbound_authority_token,
            )

    @contextmanager
    def guard_outbound_send(
        self,
        permit: AdapterOutboundRequestPermit,
    ) -> Iterator[None]:
        """Linearize one actual websocket write against reset/disconnect.

        A permit is provisional until the transport is about to call
        ``ws.send``.  The RPC client enters this guard only after selecting and
        pinning the concrete websocket. A registered transport lease through
        the synchronous write makes the ordering unambiguous:

        * if reset/disconnect fenced first, the stale call fails pre-send;
        * if this guard entered first, the write belongs to the old epoch and
          the later fence drains it (with a bounded sticky failure) before
          stopping that socket.

        The lock must never cover the subsequent response wait.
        """

        self._validate_outbound_permit(permit)
        with self._lock:
            if (
                self._ordinary_ingress_closed_locked()
                or permit.epoch != self._outbound_epoch
            ):
                raise AdapterOutboundRequestBlocked(
                    f"the admitted app-server epoch for {permit.method} closed before send"
                )
            self._active_outbound_transports += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_outbound_transports -= 1
                if self._active_outbound_transports < 0:
                    raise RuntimeError("outbound transport lease count became negative")
                self._transport_condition.notify_all()

    def confirm_outbound_request(
        self,
        permit: AdapterOutboundRequestPermit,
    ) -> None:
        """Commit a successful response only in its exact admitted epoch."""

        self._validate_outbound_permit(permit)
        with self._lock:
            if (
                self._ordinary_ingress_closed_locked()
                or permit.epoch != self._outbound_epoch
            ):
                raise AdapterOutboundRequestEpochLost(
                    f"app-server epoch changed while {permit.method} was in flight"
                )

    def require_outbound_request_admitted(self) -> None:
        """Compatibility-free probe used by diagnostics and focused tests."""

        self.issue_outbound_request("diagnostic/admission")

    def capture_existing_connection_generation(self) -> int:
        """Capture the exact live connection admitted by this gate.

        This is a prepare-stage read of gate-owned state.  It neither opens a
        websocket nor reserves transport authority.  Callers must still pin
        the same generation at the adapter request boundary and use
        :meth:`run_if_connection_generation` for their short local settle.
        """

        with self._lock:
            if (
                self._ordinary_ingress_closed_locked()
                or not self._connection_facts_live
                or self._latest_generation <= self._last_disconnected_generation
            ):
                raise AdapterOutboundRequestBlocked(
                    "there is no live admitted app-server connection to capture"
                )
            return self._latest_generation

    def run_if_connection_generation(
        self,
        expected_generation: int,
        callback: Callable[[], _SettleResult],
    ) -> _SettleResult:
        """Run one short local settle while the exact generation stays live.

        The callback runs under the gate lock so disconnect, reset, or
        replacement cannot linearize between the generation check and the
        local settlement.  It must not perform transport, filesystem, network,
        or other potentially blocking I/O.
        """

        if type(expected_generation) is not int or expected_generation <= 0:
            raise ValueError("expected connection generation must be a positive integer")
        if not callable(callback):
            raise TypeError("connection-generation settle callback must be callable")
        with self._lock:
            if (
                self._ordinary_ingress_closed_locked()
                or not self._connection_facts_live
                or expected_generation != self._latest_generation
                or expected_generation <= self._last_disconnected_generation
            ):
                raise AdapterOutboundRequestEpochLost(
                    "app-server connection generation changed before local settlement "
                    f"(expected={expected_generation}, observed={self._latest_generation})"
                )
            return callback()

    def _advance_outbound_epoch_locked(self) -> None:
        self._outbound_epoch += 1

    def _drain_outbound_transports_locked(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TRANSPORT_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        while self._active_outbound_transports:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._cleanup_required = True
                self._backend_reset_blocked = True
                raise RuntimeError(
                    "timed out draining app-server transport writes; ingress remains closed"
                )
            self._transport_condition.wait(timeout=remaining)

    def _ordinary_ingress_closed_locked(self) -> bool:
        return bool(
            self._backend_reset_blocked
            or self._cleanup_required
            or self._disconnect_cleanup_generation > 0
        )

    def _validate_outbound_permit(
        self,
        permit: AdapterOutboundRequestPermit,
    ) -> None:
        if not isinstance(permit, AdapterOutboundRequestPermit):
            raise TypeError("expected an AdapterOutboundRequestPermit")
        if permit._authority_token is not self._outbound_authority_token:
            raise ValueError("outbound request permit belongs to another gate")

    def _invalidate_connection_facts_locked(self) -> None:
        """Run and commit the connection-fact invalidation transaction.

        The gate lock deliberately covers the port call. Reader ingress for a
        replacement websocket waits until cleanup commits, so it cannot land
        between a partial cleanup and its failure result. Any exception keeps
        all ingress closed; a process restart or an explicit reviewed recovery
        path must reconstruct the durable facts before service can continue.
        """

        self._advance_outbound_epoch_locked()
        self._cleanup_required = True
        self._drain_outbound_transports_locked()
        try:
            self._invalidate_previous_epoch()
        except Exception:
            self._backend_reset_blocked = True
            raise
        self._connection_facts_live = False
        self._cleanup_required = False

    def accept(
        self,
        connection_generation: int,
    ) -> bool:
        """Activate and admit one callback from the live connection epoch.

        The constructor's activation port is required and runs under the gate
        lock for every admitted RuntimeLoop callback.  It must be idempotent:
        backend replacement publishes the generation before its first
        callback arrives, and repeated callbacks from one generation still
        reassert the canonical registry epoch without copying that fact here.

        An activation exception may represent a partial registry transition.
        Keep both reader and runtime ingress closed until an explicit backend
        reset retries the normal connection-fact invalidation transaction.
        """

        generation = int(connection_generation)
        with self._lock:
            if self._backend_reset_blocked or self._disconnect_cleanup_generation:
                logger.debug(
                    "Dropping adapter ingress while backend replacement is not admitted: "
                    "generation=%s",
                    generation,
                )
                return False
            if generation <= self._last_disconnected_generation:
                logger.debug(
                    "Dropping adapter ingress from disconnected websocket generation: "
                    "generation=%s",
                    generation,
                )
                return False
            if generation < self._latest_generation:
                logger.debug(
                    "Dropping adapter ingress from superseded websocket generation: "
                    "generation=%s latest=%s",
                    generation,
                    self._latest_generation,
                )
                return False
            if generation > self._latest_generation:
                if self._connection_facts_live:
                    # A newer callback can overtake the old websocket's
                    # disconnect callback. Admission therefore owns the full
                    # previous-epoch invalidation barrier, not merely one
                    # controller's cache clear.
                    self._invalidate_connection_facts_locked()
                self._latest_generation = generation
                self._connection_facts_live = True
            try:
                self._activate_connection_epoch(generation)
            except Exception:
                self._advance_outbound_epoch_locked()
                self._cleanup_required = True
                self._backend_reset_blocked = True
                # Activation may have committed only part of its canonical
                # state before raising.  Do not retry it from ordinary ingress;
                # explicit reset cleanup is the sole recovery transaction.
                self._connection_facts_live = True
                raise
            return True

    def fence_disconnect(self, connection_generation: int) -> bool:
        """Synchronously close ordinary ingress for a physical disconnect.

        The websocket reader calls this before queueing RuntimeLoop cleanup.
        It deliberately mutates only gate-owned scalar facts; controller and
        durable cleanup remains serialized by :meth:`observe_disconnect`.
        """

        generation = int(connection_generation)
        with self._lock:
            if self._backend_reset_blocked:
                return False
            if generation <= self._last_disconnected_generation:
                return False
            self._last_disconnected_generation = generation
            if generation < self._latest_generation:
                # A newer connection already reached the runtime.  The old
                # disconnect must not clear facts owned by that live epoch.
                return False
            self._latest_generation = generation
            if generation > self._disconnect_cleanup_generation:
                self._advance_outbound_epoch_locked()
                self._disconnect_cleanup_generation = generation
            return True

    def observe_disconnect(self, connection_generation: int) -> bool:
        """Finish canonical cleanup for a reader-fenced disconnect epoch."""

        generation = int(connection_generation)
        with self._lock:
            if self._backend_reset_blocked:
                return False
            pending_generation = self._disconnect_cleanup_generation
            if pending_generation:
                if generation != pending_generation:
                    return False
            elif not self.fence_disconnect(generation):
                return False
            try:
                self._invalidate_connection_facts_locked()
            finally:
                # A failed invalidation already latched cleanup_required and
                # backend_reset_blocked. Clear only the transient queue fence;
                # the sticky failure facts continue to reject all ingress.
                self._disconnect_cleanup_generation = 0
            return True

    def begin_backend_reset(self) -> None:
        """Close ingress and atomically invalidate the old backend epoch.

        An explicit backend reset is also the only in-process recovery command
        for a previously failed invalidation transaction.  The invalidation
        port must therefore be idempotent: a retry starts from the beginning
        while ingress remains closed, and a second failure keeps
        ``cleanup_required`` sticky.  Merely observing a newer callback never
        performs this retry.
        """

        with self._lock:
            if self._cleanup_required:
                if not self._backend_reset_blocked:
                    raise RuntimeError(
                        "connection epoch cleanup is incomplete without a closed ingress fence"
                    )
                self._invalidate_connection_facts_locked()
                self._disconnect_cleanup_generation = 0
                self._backend_reset_invalidated = True
                # The retried transaction already invalidated every old fact.
                # Keep the gate closed for replacement publication without
                # running the same transaction twice in this reset attempt.
                return
            self._backend_reset_blocked = True
            self._backend_reset_invalidated = False
            self._invalidate_connection_facts_locked()
            self._disconnect_cleanup_generation = 0
            self._backend_reset_invalidated = True

    def fence_backend_reset(
        self,
        *,
        expected_connection_generation: int | None = None,
    ) -> None:
        """Close callback/reader ingress before reset-local response cleanup.

        This first phase intentionally does not invalidate connection facts or
        stop the backend.  The reset caller may still need the live transport
        to fail-close already admitted interactions.  ``begin_backend_reset``
        performs the second phase and is the only path that may reopen through
        replacement admission. A Web caller may additionally pin the exact
        open generation; every failed comparison occurs before any gate fact
        or transport lease is changed. ``None`` preserves the trusted local
        recovery path used by CLI and Feishu.
        """

        if expected_connection_generation is not None and (
            type(expected_connection_generation) is not int
            or expected_connection_generation <= 0
        ):
            raise ValueError(
                "expected backend reset connection generation must be a positive integer"
            )
        with self._lock:
            if expected_connection_generation is not None:
                if self._latest_generation != expected_connection_generation:
                    raise AdapterBackendResetGenerationMismatchError(
                        expected_generation=expected_connection_generation,
                        observed_generation=self._latest_generation,
                    )
                if self._ordinary_ingress_closed_locked():
                    raise AdapterBackendResetUnavailableError(
                        "adapter ingress is already closed or reconciling"
                    )
            self._advance_outbound_epoch_locked()
            self._backend_reset_invalidated = False
            self._backend_reset_blocked = True
            self._drain_outbound_transports_locked()

    def admit_backend_replacement(
        self,
        connection_generation: int,
        *,
        publish_replacement: Callable[[], None],
    ) -> None:
        """Publish and atomically admit a strictly newer backend generation.

        A publication failure deliberately leaves reset admission closed.
        """

        generation = int(connection_generation)
        if generation <= 0:
            raise ValueError("replacement websocket generation must be positive")
        with self._lock:
            if self._cleanup_required:
                raise RuntimeError(
                    "connection epoch cleanup remains incomplete"
                )
            if not self._backend_reset_blocked:
                raise RuntimeError(
                    "backend replacement admission requires a closed reset fence"
                )
            if not self._backend_reset_invalidated:
                raise RuntimeError(
                    "backend replacement admission requires a fully invalidated old epoch"
                )
            previous_generation = max(
                self._latest_generation,
                self._last_disconnected_generation,
            )
            if generation <= previous_generation:
                raise RuntimeError(
                    "replacement websocket generation did not advance beyond the old backend"
                )
            publish_replacement()
            self._last_disconnected_generation = max(
                self._last_disconnected_generation,
                generation - 1,
            )
            self._latest_generation = generation
            self._connection_facts_live = True
            self._disconnect_cleanup_generation = 0
            self._backend_reset_blocked = False
            self._backend_reset_invalidated = False
