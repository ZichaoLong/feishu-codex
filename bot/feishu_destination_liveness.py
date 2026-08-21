"""Owner for durable Feishu destination-loss acceptance and reconciliation."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bot.binding_runtime_lifecycle import cancel_runtime_timer_effects
from bot.feishu_destination_liveness_contract import (
    FeishuDestinationLossProof,
    FeishuDestinationLossRecord,
    FeishuDestinationLossState,
)
from bot.runtime_admin.binding_clear import RuntimeBindingBatchDeactivationReceipt
from bot.stores.feishu_destination_loss_store import FeishuDestinationLossStore


logger = logging.getLogger(__name__)
ChatBindingKey = tuple[str, str]


class FeishuDestinationLivenessShutdownError(RuntimeError):
    """The inbox worker did not prove that it stopped."""


@dataclass(frozen=True, slots=True)
class FeishuDestinationLivenessPorts:
    lock: Any
    runtime_call: Callable[..., Any]
    runtime_context_guard: Callable[[], None]
    binding_keys_for_chat_locked: Callable[[str], tuple[ChatBindingKey, ...]]
    deactivate_bindings_locked: Callable[
        [tuple[ChatBindingKey, ...]],
        RuntimeBindingBatchDeactivationReceipt,
    ]
    finalize_deactivated_thread_runtime: Callable[[str], None]
    fail_close_chat_requests: Callable[[str], int]
    forget_chat_state: Callable[[str], None]


@dataclass(frozen=True, slots=True)
class FeishuDestinationLivenessSnapshot:
    worker_running: bool
    pending_proofs: int | None
    last_error: str

    @property
    def degraded(self) -> bool:
        return self.pending_proofs != 0 or bool(self.last_error)


class FeishuDestinationLivenessCoordinator:
    """Turn accepted event proofs into one retried local owner-loss transition."""

    def __init__(
        self,
        *,
        store: FeishuDestinationLossStore,
        ports: FeishuDestinationLivenessPorts,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        retry_delay = float(retry_delay_seconds)
        if retry_delay <= 0:
            raise ValueError("retry_delay_seconds must be positive")
        self._store = store
        self._ports = ports
        self._retry_delay_seconds = retry_delay
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._closed = False
        self._last_error = ""
        self._wake_generation = 0

    def accept(
        self,
        proof: FeishuDestinationLossProof,
    ) -> FeishuDestinationLossRecord:
        """Durably accept one authoritative loss proof."""

        with self._condition:
            if self._closed:
                raise FeishuDestinationLivenessShutdownError(
                    "destination-liveness inbox is closed"
                )
            record = self._store.accept(proof)
            self._wake_generation += 1
            self._condition.notify_all()
        return record

    def start(self) -> None:
        """Start reconciliation after RuntimeLoop and the Feishu adapter exist."""

        with self._condition:
            if self._closed:
                raise FeishuDestinationLivenessShutdownError(
                    "closed destination-liveness inbox cannot restart"
                )
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping = False
            worker = threading.Thread(
                target=self._run,
                name="feishu-destination-liveness",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def shutdown(self, *, timeout: float | None = None) -> None:
        with self._condition:
            self._stopping = True
            self._closed = True
            worker = self._worker
            self._condition.notify_all()
        if worker is not None and worker is threading.current_thread():
            raise FeishuDestinationLivenessShutdownError(
                "destination-liveness worker cannot join itself"
            )
        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)
            if worker.is_alive():
                raise FeishuDestinationLivenessShutdownError(
                    f"destination-liveness worker did not stop within {timeout} seconds"
                )

    def reconcile_proof_on_runtime(
        self,
        proof: FeishuDestinationLossProof,
    ) -> bool:
        """Apply and settle one exact pending proof from RuntimeLoop."""

        self._ports.runtime_context_guard()
        record = self._store.load(proof.proof_id)
        if record is None:
            raise RuntimeError("destination-loss proof was not durably accepted")
        if record.proof != proof:
            raise RuntimeError(
                "destination-loss proof identity changed after acceptance"
            )
        if record.state is FeishuDestinationLossState.SETTLED:
            return False

        with self._ports.lock:
            bindings = self._ports.binding_keys_for_chat_locked(proof.chat_id)
            receipt = self._ports.deactivate_bindings_locked(bindings)
        cancel_runtime_timer_effects(receipt.timer_cancellations)
        unsubscribe_thread_ids = sorted(
            {
                removal.unsubscribe_thread_id
                for removal in receipt.confirmed_removals
                if removal.unsubscribe_thread_id
            }
        )
        for thread_id in unsubscribe_thread_ids:
            try:
                self._ports.finalize_deactivated_thread_runtime(thread_id)
            except Exception:
                # Binding loss is already committed. Runtime unsubscribe and
                # lease release are an independent conservative consequence;
                # retaining that runtime is safer than rolling the matching
                # destination cleanup back or inventing another durable state.
                logger.exception(
                    "destination loss 后无法收口 thread runtime；"
                    "已保留 thread runtime: proof=%s chat=%s thread=%s",
                    proof.proof_id,
                    proof.chat_id,
                    thread_id[:12],
                )
        pending_fail_closed = self._ports.fail_close_chat_requests(proof.chat_id)
        self._ports.forget_chat_state(proof.chat_id)
        self._store.settle(proof)
        logger.info(
            "destination loss settled: proof=%s type=%s chat=%s bindings=%s "
            "threads=%s pending=%s",
            proof.proof_id,
            proof.proof_type.value,
            proof.chat_id,
            len(receipt.confirmed_removals),
            len(unsubscribe_thread_ids),
            pending_fail_closed,
        )
        return True

    def snapshot(self) -> FeishuDestinationLivenessSnapshot:
        try:
            pending_proofs: int | None = len(self._store.pending())
        except Exception as exc:
            pending_proofs = None
            read_error = str(exc)
        else:
            read_error = ""
        with self._condition:
            worker = self._worker
            return FeishuDestinationLivenessSnapshot(
                worker_running=bool(worker is not None and worker.is_alive()),
                pending_proofs=pending_proofs,
                last_error=self._last_error or read_error,
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                observed_generation = self._wake_generation
            try:
                pending = self._store.pending()
            except Exception as exc:
                self._record_error(exc)
                if self._wait_for_work(
                    self._retry_delay_seconds,
                    observed_generation=observed_generation,
                ):
                    return
                continue
            if not pending:
                if self._wait_for_work(
                    None,
                    observed_generation=observed_generation,
                ):
                    return
                continue
            failed = False
            for record in pending:
                with self._condition:
                    if self._stopping:
                        return
                try:
                    self._ports.runtime_call(
                        self.reconcile_proof_on_runtime,
                        record.proof,
                    )
                except Exception as exc:
                    self._record_error(exc)
                    failed = True
                    break
                else:
                    with self._condition:
                        self._last_error = ""
            if failed and self._wait_for_work(
                self._retry_delay_seconds,
                observed_generation=observed_generation,
            ):
                return

    def _record_error(self, exc: Exception) -> None:
        with self._condition:
            self._last_error = f"{type(exc).__name__}: {exc}"
        logger.exception("destination-loss reconciliation failed; will retry")

    def _wait_for_work(
        self,
        timeout: float | None,
        *,
        observed_generation: int,
    ) -> bool:
        with self._condition:
            if self._stopping:
                return True
            if self._wake_generation == observed_generation:
                self._condition.wait(timeout=timeout)
            return self._stopping
