"""Single owner for the Focus service process lifecycle.

The lifecycle owns ordering and proof state.  Concrete runtime components stay
behind required ports so this module never mirrors their mutable state.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar


logger = logging.getLogger(__name__)


_PreparationT = TypeVar("_PreparationT")
_ExternalResultT = TypeVar("_ExternalResultT")


class ServiceRuntimePhase(str, Enum):
    ASSEMBLED = "assembled"
    LEASE_ACQUIRED = "lease_acquired"
    ACTIVE = "active"
    STOPPING = "stopping"
    CLOSED = "closed"


class ServiceRuntimeLifecycleError(RuntimeError):
    """Base error for invalid lifecycle transitions."""


class ServiceRuntimeLifecycleReentryError(ServiceRuntimeLifecycleError):
    """Raised when a lifecycle callback re-enters the same lifecycle."""


class ServiceRuntimeShutdownError(ServiceRuntimeLifecycleError):
    """A shutdown stage could not prove that all of its resources converged."""

    def __init__(
        self,
        stage: str,
        failures: tuple[tuple[str, Exception], ...],
    ) -> None:
        self.stage = str(stage)
        self.failures = failures
        detail = "; ".join(f"{label}: {error}" for label, error in failures)
        super().__init__(
            f"service runtime shutdown did not complete {self.stage}"
            + (f" ({detail})" if detail else "")
        )


class ServiceRuntimeIngressRejected(ServiceRuntimeLifecycleError):
    """A new external callback arrived outside the active service phase."""

    def __init__(self, phase: ServiceRuntimePhase) -> None:
        self.phase = phase
        super().__init__(
            f"external ingress requires an active service runtime; phase={phase.value}"
        )


@dataclass(frozen=True, slots=True)
class ServiceRuntimeIngressReceipt:
    """Opaque proof that one external callback crossed the active-phase gate."""

    sequence: int
    _lifecycle_token: object


@dataclass(frozen=True, slots=True)
class PreparedServiceRuntimeExternalTransaction(Generic[_PreparationT]):
    """One prepared external transaction retaining its exact ingress barrier."""

    preparation: _PreparationT
    _ingress_receipt: ServiceRuntimeIngressReceipt = field(
        repr=False,
        compare=False,
    )
    _dispatcher_token: object = field(repr=False, compare=False)


@dataclass(slots=True)
class _ServiceRuntimeIngressRecord:
    receipt: ServiceRuntimeIngressReceipt
    origin_thread_id: int | None
    execution_thread_id: int | None = None


@dataclass(frozen=True)
class ServiceRuntimeActivationPorts:
    """Required, ordered activation capabilities supplied by the composition root."""

    acquire_service_lease: Callable[[], None]
    prepare_owned_state: Callable[[], None]
    start_runtime_loop: Callable[[], None]
    restore_runtime_state: Callable[[], None]
    start_adapter: Callable[[], None]
    start_destination_liveness_worker: Callable[[], None]
    start_control_plane: Callable[[], str]
    publish_control_endpoint: Callable[[str], None]
    register_instance_runtime: Callable[[], None]
    restore_runtime_leases: Callable[[], None]
    start_web_gateway: Callable[[], None]


@dataclass(frozen=True)
class ServiceRuntimeShutdownPorts:
    """Required cleanup capabilities used to prove safe authority release."""

    cancel_frontend_timers: Callable[[], None]
    web_is_running: Callable[[], bool]
    prepare_web_shutdown: Callable[[], None]
    stop_web_gateway: Callable[[], None]
    stop_control_plane: Callable[[], None]
    stop_server_request_runtime: Callable[[], None]
    stop_execution_recovery_worker: Callable[[], None]
    stop_destination_liveness_worker: Callable[[], None]
    stop_card_dispatcher: Callable[[], None]
    finish_web_shutdown: Callable[[], None]
    stop_runtime_loop: Callable[[], None]
    stop_adapter: Callable[[], None]
    release_machine_authority: Callable[[], None]


class ServiceRuntimeIngressDispatcher:
    """Thin transport adapter around the lifecycle-owned ingress receipts."""

    def __init__(
        self,
        lifecycle: ServiceRuntimeLifecycle,
        call_runtime: Callable[..., Any],
        submit_runtime: Callable[..., None],
    ) -> None:
        self._lifecycle = lifecycle
        self._call_runtime = call_runtime
        self._submit_runtime = submit_runtime
        self._external_transaction_token = object()

    def call(
        self,
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        receipt = self._lifecycle.begin_external_ingress()
        try:
            return self._call_runtime(
                self._lifecycle.run_external_ingress,
                receipt,
                callback,
                *args,
                **kwargs,
            )
        except BaseException:
            # A callback which ran has already settled this exact receipt. A
            # downstream admission failure has not, so abandon only if it is
            # still pending.
            self._lifecycle.abandon_external_ingress(receipt)
            raise

    def submit(
        self,
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        receipt = self._lifecycle.begin_external_ingress()
        try:
            self._submit_runtime(
                self._lifecycle.run_external_ingress,
                receipt,
                callback,
                *args,
                **kwargs,
            )
            self._lifecycle.confirm_external_ingress_dispatch(receipt)
        except BaseException:
            self._lifecycle.abandon_external_ingress(receipt)
            raise

    def run_external_transaction(
        self,
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run one caller-thread transaction under an exact ingress receipt.

        The callback may enter RuntimeLoop briefly for prepare and settle while
        keeping blocking I/O in between on this external thread.  The receipt
        remains the service shutdown barrier until the whole callback returns.
        RuntimeLoop stages are state operations and must not re-enter this
        service lifecycle's start, stop, or maintenance operations.
        """

        receipt = self._lifecycle.begin_external_ingress()
        return self._lifecycle.run_external_ingress(
            receipt,
            callback,
            *args,
            **kwargs,
        )

    def start_background_external_transaction(
        self,
        callback: Callable[..., Any],
        /,
        *args: Any,
        thread_name: str = "focus-external-transaction",
        **kwargs: Any,
    ) -> threading.Thread:
        """Start one lifecycle-fenced external transaction on a daemon thread.

        This is the background counterpart of ``run_external_transaction`` for
        RuntimeLoop-owned state transitions which discover that slow external
        work is needed.  The lifecycle receipt, rather than a second worker
        registry, remains the shutdown barrier from admission until the worker
        returns.  Expected transaction failures should be settled by the
        callback; an unexpected escape is logged because there is no request
        thread to receive it.
        """

        normalized_name = str(thread_name or "").strip()
        if not normalized_name:
            raise ValueError("background external transaction requires a thread name")
        receipt = self._lifecycle.begin_external_ingress()

        def run() -> None:
            try:
                self._lifecycle.run_external_ingress(
                    receipt,
                    callback,
                    *args,
                    **kwargs,
                )
            except Exception:
                logger.exception(
                    "background external transaction failed: thread=%s",
                    normalized_name,
                )

        worker = threading.Thread(
            target=run,
            name=normalized_name,
            daemon=True,
        )
        try:
            worker.start()
            self._lifecycle.confirm_external_ingress_dispatch(receipt)
        except BaseException:
            self._lifecycle.abandon_external_ingress(receipt)
            raise
        return worker

    def prepare_external_transaction(
        self,
        prepare: Callable[..., _PreparationT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> PreparedServiceRuntimeExternalTransaction[_PreparationT]:
        """Prepare under RuntimeLoop while retaining one exact ingress receipt.

        The caller must either run or abandon the returned transaction.  The
        retained ingress receipt remains a service-shutdown barrier across the
        caller's intervening transport/lifecycle lock release.
        """

        receipt = self._lifecycle.begin_external_ingress()
        try:
            preparation = self._call_runtime(prepare, *args, **kwargs)
            self._lifecycle.confirm_external_ingress_dispatch(receipt)
        except BaseException:
            self._lifecycle.abandon_external_ingress(receipt)
            raise
        return PreparedServiceRuntimeExternalTransaction(
            preparation=preparation,
            _ingress_receipt=receipt,
            _dispatcher_token=self._external_transaction_token,
        )

    def run_prepared_external_transaction(
        self,
        prepared: PreparedServiceRuntimeExternalTransaction[_PreparationT],
        callback: Callable[..., _ExternalResultT],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> _ExternalResultT:
        """Claim and run one prepared transaction exactly once."""

        self._require_prepared_external_transaction(prepared)
        return self._lifecycle.run_external_ingress(
            prepared._ingress_receipt,
            callback,
            prepared.preparation,
            *args,
            **kwargs,
        )

    def abandon_prepared_external_transaction(
        self,
        prepared: PreparedServiceRuntimeExternalTransaction[object],
    ) -> bool:
        """Retire a prepared transaction only if execution has not claimed it."""

        self._require_prepared_external_transaction(prepared)
        return self._lifecycle.abandon_external_ingress(
            prepared._ingress_receipt,
        )

    def _require_prepared_external_transaction(
        self,
        prepared: PreparedServiceRuntimeExternalTransaction[object],
    ) -> None:
        if not isinstance(prepared, PreparedServiceRuntimeExternalTransaction):
            raise TypeError("prepared service transaction token is required")
        if prepared._dispatcher_token is not self._external_transaction_token:
            raise ServiceRuntimeLifecycleError(
                "prepared service transaction belongs to another dispatcher"
            )


class ServiceRuntimeLifecycle:
    """One-shot, single-flight owner of service activation and shutdown.

    External callbacks run outside the condition mutex.  The in-flight owner
    token serializes competing threads while also making same-thread callback
    re-entry an explicit error instead of a deadlock.
    """

    def __init__(
        self,
        *,
        activation: ServiceRuntimeActivationPorts,
        shutdown: ServiceRuntimeShutdownPorts,
    ) -> None:
        self._activation = activation
        self._shutdown = shutdown
        self._condition = threading.Condition()
        self._phase = ServiceRuntimePhase.ASSEMBLED
        self._operation_owner: int | None = None
        self._ingress_token = object()
        self._next_ingress_sequence = 0
        self._active_external_ingress: dict[int, _ServiceRuntimeIngressRecord] = {}
        # STOPPING is reachable both from an assembled-only cleanup and after
        # lease acquisition. Keep the release proof explicit across retries.
        self._machine_authority_acquired = False
        # Shutdown is a retryable transaction. Once RuntimeLoop admission has
        # closed, callbacks which require that loop can no longer be replayed;
        # retain exact in-process progress rather than guessing that an
        # earlier cleanup call was idempotently successful.
        self._completed_shutdown_steps: set[str] = set()
        self._web_shutdown_required: bool | None = None
        # Installer maintenance closes ingress before the service manager
        # stops the process.  It is intentionally process-local: a restart is
        # the recovery path, not a second durable lifecycle state machine.
        self._offline_maintenance_prepared = False

    @property
    def phase(self) -> ServiceRuntimePhase:
        with self._condition:
            return self._phase

    @property
    def offline_maintenance_prepared(self) -> bool:
        with self._condition:
            return self._offline_maintenance_prepared

    def start(self) -> None:
        self._enter_operation("start")
        try:
            phase = self.phase
            if phase is ServiceRuntimePhase.ACTIVE:
                return
            if phase is ServiceRuntimePhase.CLOSED:
                raise ServiceRuntimeLifecycleError(
                    "a closed service runtime lifecycle cannot be restarted"
                )
            if phase is ServiceRuntimePhase.STOPPING:
                raise ServiceRuntimeLifecycleError(
                    "a stopping service runtime lifecycle cannot be started"
                )
            if phase is not ServiceRuntimePhase.ASSEMBLED:
                raise ServiceRuntimeLifecycleError(
                    f"unexpected service runtime start phase: {phase.value}"
                )

            # A failed acquire has no inverse: authority was never obtained.
            self._activation.acquire_service_lease()
            self._set_machine_authority_acquired(True)
            self._set_phase(ServiceRuntimePhase.LEASE_ACQUIRED)
            try:
                self._activate_after_lease()
            except Exception:
                self._set_phase(ServiceRuntimePhase.STOPPING)
                try:
                    self._shutdown_after_lease(context="startup rollback")
                except ServiceRuntimeShutdownError:
                    # Preserve the activation error as the caller-visible
                    # cause. The lifecycle remains STOPPING with machine
                    # authority retained, and a later stop() retries the exact
                    # incomplete shutdown stage.
                    logger.exception(
                        "startup rollback did not prove complete shutdown; "
                        "retaining machine authority"
                    )
                raise
            self._set_phase(ServiceRuntimePhase.ACTIVE)
        finally:
            self._leave_operation()

    def stop(self) -> None:
        self._enter_operation("stop")
        try:
            with self._condition:
                phase = self._phase
                if phase is ServiceRuntimePhase.CLOSED:
                    return
                if phase not in {
                    ServiceRuntimePhase.ASSEMBLED,
                    ServiceRuntimePhase.LEASE_ACQUIRED,
                    ServiceRuntimePhase.ACTIVE,
                    ServiceRuntimePhase.STOPPING,
                }:
                    raise ServiceRuntimeLifecycleError(
                        f"unexpected service runtime stop phase: {phase.value}"
                    )
                # Closing admission and observing the last admitted callback
                # are one condition-protected transition. Callback code runs
                # outside this mutex; its exact receipt is the shutdown
                # barrier until it returns (or an accepted async task exits).
                self._offline_maintenance_prepared = False
                self._phase = ServiceRuntimePhase.STOPPING
                while self._active_external_ingress:
                    self._condition.wait()
            self._shutdown_after_lease(context="shutdown")
        finally:
            self._leave_operation()

    def prepare_offline_maintenance(
        self,
        verify_idle: Callable[[], Any],
    ) -> Any:
        """Atomically close ingress, then obtain the runtime's idle proof.

        Verification runs outside the lifecycle mutex but while this method
        owns the lifecycle operation token.  New ingress is already closed,
        so a successful proof cannot be invalidated before ``stop()`` begins.
        """

        self._enter_operation("prepare offline maintenance")
        try:
            with self._condition:
                if self._phase is not ServiceRuntimePhase.ACTIVE:
                    raise ServiceRuntimeLifecycleError(
                        "offline maintenance requires an active service runtime; "
                        f"phase={self._phase.value}"
                    )
                if self._active_external_ingress:
                    raise ServiceRuntimeLifecycleError(
                        "offline maintenance cannot begin while external ingress "
                        f"is active; count={len(self._active_external_ingress)}"
                    )
                self._phase = ServiceRuntimePhase.STOPPING
                self._offline_maintenance_prepared = True
            try:
                return verify_idle()
            except BaseException:
                with self._condition:
                    if (
                        self._offline_maintenance_prepared
                        and self._phase is ServiceRuntimePhase.STOPPING
                        and not self._completed_shutdown_steps
                    ):
                        self._offline_maintenance_prepared = False
                        self._phase = ServiceRuntimePhase.ACTIVE
                raise
        finally:
            self._leave_operation()

    def cancel_offline_maintenance(self) -> None:
        """Reopen ingress only for the exact unconsumed maintenance admission."""

        self._enter_operation("cancel offline maintenance")
        try:
            with self._condition:
                if (
                    not self._offline_maintenance_prepared
                    or self._phase is not ServiceRuntimePhase.STOPPING
                    or self._completed_shutdown_steps
                ):
                    raise ServiceRuntimeLifecycleError(
                        "no cancellable offline maintenance admission exists"
                    )
                self._offline_maintenance_prepared = False
                self._phase = ServiceRuntimePhase.ACTIVE
        finally:
            self._leave_operation()

    def begin_external_ingress(self) -> ServiceRuntimeIngressReceipt:
        """Atomically admit one callback only while the service is active."""

        origin_thread_id = threading.get_ident()
        with self._condition:
            if self._phase is not ServiceRuntimePhase.ACTIVE:
                raise ServiceRuntimeIngressRejected(self._phase)
            self._next_ingress_sequence += 1
            receipt = ServiceRuntimeIngressReceipt(
                sequence=self._next_ingress_sequence,
                _lifecycle_token=self._ingress_token,
            )
            self._active_external_ingress[receipt.sequence] = (
                _ServiceRuntimeIngressRecord(
                    receipt=receipt,
                    origin_thread_id=origin_thread_id,
                )
            )
            return receipt

    def run_external_ingress(
        self,
        receipt: ServiceRuntimeIngressReceipt,
        callback: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run an admitted callback outside the lifecycle mutex and settle it."""

        execution_thread_id = threading.get_ident()
        with self._condition:
            record = self._external_ingress_record_locked(receipt)
            if record is None:
                raise ServiceRuntimeLifecycleError(
                    "external ingress receipt is stale or belongs to another lifecycle"
                )
            if record.execution_thread_id is not None:
                raise ServiceRuntimeLifecycleError(
                    "external ingress receipt is already executing"
                )
            # Once execution has started, only the execution thread can
            # self-deadlock by stopping the lifecycle it is fencing. The
            # origin may already have returned from an async submit.
            record.origin_thread_id = None
            record.execution_thread_id = execution_thread_id
        try:
            return callback(*args, **kwargs)
        finally:
            with self._condition:
                current = self._external_ingress_record_locked(receipt)
                if (
                    current is not None
                    and current.execution_thread_id == execution_thread_id
                ):
                    del self._active_external_ingress[receipt.sequence]
                    self._condition.notify_all()

    def abandon_external_ingress(
        self,
        receipt: ServiceRuntimeIngressReceipt,
    ) -> bool:
        """Cancel an exact receipt when downstream task admission did not occur."""

        with self._condition:
            record = self._external_ingress_record_locked(receipt)
            if record is None or record.execution_thread_id is not None:
                return False
            del self._active_external_ingress[receipt.sequence]
            self._condition.notify_all()
            return True

    def confirm_external_ingress_dispatch(
        self,
        receipt: ServiceRuntimeIngressReceipt,
    ) -> bool:
        """Release origin-thread ownership after an async task was accepted."""

        with self._condition:
            record = self._external_ingress_record_locked(receipt)
            if record is None:
                return False
            record.origin_thread_id = None
            return True

    def _activate_after_lease(self) -> None:
        ports = self._activation
        ports.prepare_owned_state()
        ports.start_runtime_loop()
        ports.restore_runtime_state()
        ports.start_adapter()
        ports.start_destination_liveness_worker()
        control_endpoint = ports.start_control_plane()
        ports.publish_control_endpoint(control_endpoint)
        ports.register_instance_runtime()
        ports.restore_runtime_leases()
        ports.start_web_gateway()

    def _shutdown_after_lease(
        self,
        *,
        context: str,
    ) -> None:
        ports = self._shutdown
        failures: list[tuple[str, Exception]] = []

        def stop_resource(step: str, label: str, action: Callable[[], None]) -> None:
            if step in self._completed_shutdown_steps:
                return
            try:
                action()
            except Exception as exc:
                failures.append((label, exc))
                logger.exception("%s failed to stop %s", context, label)
            else:
                self._completed_shutdown_steps.add(step)

        def require_stage(stage: str) -> None:
            if not failures:
                return
            logger.error(
                "%s did not prove %s; retaining machine authority",
                context,
                stage,
            )
            raise ServiceRuntimeShutdownError(stage, tuple(failures))

        # Close and join execution-recovery admission before cancelling the
        # timers it can register.  A failed join must not let timer
        # cancellation become a completed retry step while a surviving worker
        # can still register a replacement watchdog behind that barrier.
        # RuntimeLoop and the adapter remain alive for the worker's final
        # prepare/settle calls.
        stop_resource(
            "execution_recovery_worker",
            "execution recovery worker",
            ports.stop_execution_recovery_worker,
        )
        require_stage("execution recovery producer barrier")

        # Then close and join every remaining producer other than the adapter.
        # The adapter stays alive until already-admitted RuntimeLoop work has
        # drained: such work may itself perform a backend reset, so stopping
        # the adapter first would allow that task to create a replacement
        # guardian behind the shutdown transaction.
        stop_resource(
            "frontend_timers",
            "frontend runtime timers",
            ports.cancel_frontend_timers,
        )
        if "inspect_web" not in self._completed_shutdown_steps:
            try:
                observed_web_running = bool(ports.web_is_running())
            except Exception as exc:
                # Conservatively run the Web owner cleanup if observation
                # itself is unavailable. Keep the inspection step incomplete
                # so a retry still has to obtain a real answer.
                self._web_shutdown_required = True
                failures.append(("Focus Web Gateway inspection", exc))
                logger.exception("%s failed to inspect Focus Web Gateway", context)
            else:
                self._web_shutdown_required = bool(
                    self._web_shutdown_required or observed_web_running
                )
                self._completed_shutdown_steps.add("inspect_web")
        if self._web_shutdown_required:
            stop_resource(
                "prepare_web",
                "Focus Web runtime admission",
                ports.prepare_web_shutdown,
            )
        stop_resource("web_gateway", "Focus Web Gateway", ports.stop_web_gateway)
        stop_resource("control_plane", "local control plane", ports.stop_control_plane)
        stop_resource(
            "server_request_runtime",
            "server-request runtime",
            ports.stop_server_request_runtime,
        )
        stop_resource(
            "destination_liveness_worker",
            "Feishu destination-liveness worker",
            ports.stop_destination_liveness_worker,
        )
        stop_resource(
            "card_dispatcher",
            "execution card dispatcher",
            ports.stop_card_dispatcher,
        )
        if self._web_shutdown_required:
            stop_resource(
                "finish_web",
                "Focus Web runtime state",
                ports.finish_web_shutdown,
            )
        require_stage("producer cleanup")

        stop_resource(
            "runtime_loop",
            "RuntimeLoop barrier",
            ports.stop_runtime_loop,
        )
        require_stage("RuntimeLoop barrier")

        # No accepted or future RuntimeLoop task can now restart the adapter.
        # Adapter.stop() is itself the callback/process-tree barrier.
        stop_resource("adapter", "Codex adapter", ports.stop_adapter)
        require_stage("adapter cleanup")

        if self._machine_authority_is_acquired():
            stop_resource(
                "machine_authority",
                "machine authority",
                ports.release_machine_authority,
            )
            require_stage("machine-authority release")
            self._set_machine_authority_acquired(False)
        self._set_phase(ServiceRuntimePhase.CLOSED)

    def _enter_operation(self, operation: str) -> None:
        current_thread = threading.get_ident()
        with self._condition:
            if self._thread_holds_external_ingress_locked(current_thread):
                raise ServiceRuntimeLifecycleReentryError(
                    "an admitted external-ingress callback cannot re-enter "
                    f"service runtime lifecycle operation {operation}"
                )
            while self._operation_owner is not None:
                if self._operation_owner == current_thread:
                    raise ServiceRuntimeLifecycleReentryError(
                        f"service runtime lifecycle callback re-entered {operation}"
                    )
                self._condition.wait()
            self._operation_owner = current_thread

    def _leave_operation(self) -> None:
        with self._condition:
            self._operation_owner = None
            self._condition.notify_all()

    def _set_phase(self, phase: ServiceRuntimePhase) -> None:
        with self._condition:
            self._phase = phase

    def _machine_authority_is_acquired(self) -> bool:
        with self._condition:
            return self._machine_authority_acquired

    def _set_machine_authority_acquired(self, acquired: bool) -> None:
        with self._condition:
            self._machine_authority_acquired = bool(acquired)

    def _external_ingress_record_locked(
        self,
        receipt: ServiceRuntimeIngressReceipt,
    ) -> _ServiceRuntimeIngressRecord | None:
        if (
            not isinstance(receipt, ServiceRuntimeIngressReceipt)
            or receipt._lifecycle_token is not self._ingress_token
        ):
            return None
        record = self._active_external_ingress.get(receipt.sequence)
        if record is None or record.receipt is not receipt:
            return None
        return record

    def _thread_holds_external_ingress_locked(self, thread_id: int) -> bool:
        return any(
            record.origin_thread_id == thread_id
            or record.execution_thread_id == thread_id
            for record in self._active_external_ingress.values()
        )
