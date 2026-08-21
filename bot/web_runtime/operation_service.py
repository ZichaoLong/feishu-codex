"""Web active-main-turn ownership and process-local control evidence."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from bot.codex_protocol.client import CodexRpcProtocolError, CodexRpcTransportError
from bot.exception_chain import iter_exception_chain
from bot.runtime_loop import RuntimeContextGuard
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_web_interaction_holder,
)
from bot.thread_create_transaction import ThreadCreateOutcomeUnknown
from bot.thread_runtime_authority import (
    ThreadResumeOutcomeUnknown,
    ThreadResumeSettlementError,
    ThreadResumeSettlementOutcome,
)
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.mutation_recovery import (
    LIFECYCLE_UNKNOWN_OPERATIONS,
    WebBackendRetiredMutation,
    WebLifecycleVerification,
    WebMutationBackendRetirementReceipt,
    WebMutationRecoveryRegistry,
    WebMutationSettlement,
    WebUnknownMutation,
    is_web_mutation_id,
    public_web_mutation_operation,
)
from bot.web_runtime.projection import FocusWebProjection, project_owner
from bot.web_runtime.contract import (
    WebAutonomousTurnReceipt,
    WebConnectedWriterReceipt,
    WebInteractionDeliveryDecision,
    WebInteractionDeliveryDisposition,
    WebRuntimeError,
    WebTurnSubmissionReceipt,
)


@dataclass(slots=True)
class WebOperationPorts:
    require_connected_document: Callable[[str], str]
    require_thread_id: Callable[[str], str]
    read_lifecycle_target_state: Callable[[str], str]
    turn_ids: Callable[[str], Iterable[str]]


class WebOperationService:
    """Own only Web's projection of the shared active-main-turn lease.

    Browser connectivity, selection, runtime interest, presentation, and
    unknown control results are separate facts. None of them extends a main
    turn or creates cross-restart writer authority.
    """

    def __init__(
        self,
        *,
        interaction_lease_store: InteractionLeaseStore,
        document_registry: WebDocumentRegistry,
        projection: FocusWebProjection,
        ports: WebOperationPorts,
        runtime_context_guard: RuntimeContextGuard,
        mutation_recovery: WebMutationRecoveryRegistry | None = None,
    ) -> None:
        if not callable(runtime_context_guard):
            raise TypeError(
                "Web operation service requires a RuntimeLoop context guard"
            )
        self._runtime_context_guard = runtime_context_guard
        self._interaction_leases = interaction_lease_store
        self._documents = document_registry
        self._projection = projection
        self._ports = ports
        self._mutations = mutation_recovery or WebMutationRecoveryRegistry(
            runtime_context_guard=runtime_context_guard,
        )

    @staticmethod
    def holder(client_id: str):
        """Return the current-process Web main-turn holder."""

        return make_web_interaction_holder(client_id, owner_pid=os.getpid())

    turn_holder = holder

    def acquire_autonomous_turn_external(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> WebAutonomousTurnReceipt:
        """Atomically acquire or borrow an autonomous-turn lease off-loop."""

        normalized_client_id = str(client_id or "").strip()
        normalized_root_id = str(root_thread_id or "").strip()
        if not normalized_client_id or not normalized_root_id:
            raise ValueError("autonomous-turn admission requires exact ids")
        try:
            acquired = self._interaction_leases.acquire(
                normalized_root_id,
                self.holder(normalized_client_id),
            )
        except Exception as exc:
            raise self._interaction_state_unavailable(normalized_root_id) from exc
        if not acquired.granted or acquired.lease is None:
            owner = project_owner(acquired.lease, client_id=normalized_client_id)
            raise WebRuntimeError(
                f"This thread is currently controlled by {owner['label']}.",
                code="interaction_owned",
                status=409,
            )
        return WebAutonomousTurnReceipt(
            normalized_client_id,
            normalized_root_id,
            acquired.lease,
            acquired.acquired,
        )

    def release_autonomous_turn_external(
        self,
        receipt: WebAutonomousTurnReceipt,
    ) -> bool:
        """CAS-release only an externally admitted fresh blank."""

        self.require_exact_autonomous_turn_receipt(receipt)
        if not receipt.acquired or receipt.lease.turn_id:
            return False
        try:
            return self._interaction_leases.release_if_matches(receipt.lease)
        except Exception as exc:
            raise self._interaction_state_unavailable(receipt.root_thread_id) from exc

    def require_current_autonomous_turn_external(
        self,
        receipt: WebAutonomousTurnReceipt,
        *,
        client_id: str = "",
        root_thread_id: str = "",
    ) -> None:
        """Require the exact lease generation immediately before an effect."""

        self.require_exact_autonomous_turn_receipt(
            receipt,
            client_id=client_id,
            root_thread_id=root_thread_id,
        )
        try:
            current = self._interaction_leases.load(receipt.root_thread_id)
        except Exception as exc:
            raise self._interaction_state_unavailable(receipt.root_thread_id) from exc
        if current != receipt.lease:
            raise self._interaction_state_unavailable(receipt.root_thread_id)

    def publish_autonomous_turn_change(
        self,
        receipt: WebAutonomousTurnReceipt,
        *,
        reason: str,
        changed: bool,
    ) -> None:
        """Publish a completed external lease transition inside RuntimeLoop."""

        self._runtime_context_guard()
        self.require_exact_autonomous_turn_receipt(receipt)
        if changed:
            self._publish_owner(receipt.root_thread_id, reason)

    def require_exact_autonomous_turn_receipt(
        self,
        receipt: WebAutonomousTurnReceipt,
        *,
        client_id: str = "",
        root_thread_id: str = "",
    ) -> None:
        if not isinstance(receipt, WebAutonomousTurnReceipt):
            raise TypeError("autonomous-turn settlement requires a typed receipt")
        if (
            receipt.client_id != str(client_id or receipt.client_id).strip()
            or receipt.root_thread_id
            != str(root_thread_id or receipt.root_thread_id).strip()
            or receipt.root_thread_id != receipt.lease.thread_id
            or receipt.lease.holder != self.holder(receipt.client_id)
        ):
            raise WebRuntimeError(
                "The autonomous-turn lease receipt is not exact for this Web owner.",
                code="interaction_state_unavailable",
                status=503,
                details={"thread_id": receipt.root_thread_id},
            )

    def owned_main_turn_thread_ids(self, client_id: str) -> tuple[str, ...]:
        self._runtime_context_guard()
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return ()
        holder = self.holder(normalized_client_id)
        return tuple(
            sorted(
                lease.thread_id
                for lease in self._interaction_leases.list()
                if lease.holder.same_holder(holder)
            )
        )

    def acquire_exclusive_turn_submission(
        self,
        client_id: str,
        root_thread_id: str,
    ) -> WebTurnSubmissionReceipt:
        """Acquire one fresh blank lease for review or compact submission."""

        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_root_id = self._ports.require_thread_id(root_thread_id)
        try:
            acquired = self._interaction_leases.acquire(
                normalized_root_id,
                self.holder(normalized_client_id),
            )
        except Exception as exc:
            raise self._interaction_state_unavailable(normalized_root_id) from exc
        if not acquired.granted or not acquired.acquired or acquired.lease is None:
            owner = project_owner(acquired.lease, client_id=normalized_client_id)
            raise WebRuntimeError(
                f"This thread is currently controlled by {owner['label']}.",
                code="interaction_owned",
                status=409,
            )
        self._publish_owner(normalized_root_id, "web_turn_submission_acquired")
        return WebTurnSubmissionReceipt(
            client_id=normalized_client_id,
            root_thread_id=normalized_root_id,
            lease=acquired.lease,
        )

    def activate_turn_submission(
        self,
        receipt: WebTurnSubmissionReceipt,
        turn_id: str,
    ) -> WebConnectedWriterReceipt:
        self._runtime_context_guard()
        if not isinstance(receipt, WebTurnSubmissionReceipt):
            raise TypeError("Web turn activation requires a typed submission receipt")
        try:
            active = self._interaction_leases.activate_turn(receipt.lease, turn_id)
        except Exception as exc:
            raise self._interaction_state_unavailable(receipt.root_thread_id) from exc
        if active is None:
            raise self._interaction_state_unavailable(receipt.root_thread_id)
        return WebConnectedWriterReceipt(
            receipt.client_id,
            receipt.root_thread_id,
            active.holder,
            active,
        )

    def release_exact_blank_turn_submission(
        self,
        receipt: WebTurnSubmissionReceipt,
        *,
        reason: str,
    ) -> bool:
        """Release only the exact still-blank submission generation.

        Callers may use this after a known pre-start failure or when a
        preceding ``thread/resume`` outcome is unknown but the review or
        compact effect was never attempted. An accepted or outcome-unknown
        exclusive action must retain its blank lease. Exact comparison also
        preserves a lease that lifecycle processing already activated or
        replaced.
        """

        self._runtime_context_guard()
        if not isinstance(receipt, WebTurnSubmissionReceipt):
            raise TypeError("Web turn release requires a typed submission receipt")
        if receipt.lease.turn_id:
            return False
        try:
            released = self._interaction_leases.release_if_matches(receipt.lease)
        except Exception as exc:
            raise self._interaction_state_unavailable(receipt.root_thread_id) from exc
        if released:
            self._publish_owner(receipt.root_thread_id, reason)
        return released

    def require_active_turn_writer(
        self,
        client_id: str,
        root_thread_id: str,
        *,
        turn_id: str = "",
    ) -> WebConnectedWriterReceipt:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_root_id = self._ports.require_thread_id(root_thread_id)
        expected_turn_id = str(turn_id or "").strip()
        try:
            lease = self._interaction_leases.load(normalized_root_id)
        except Exception as exc:
            raise self._interaction_state_unavailable(normalized_root_id) from exc
        holder = self.holder(normalized_client_id)
        if (
            lease is not None
            and lease.turn_id
            and lease.holder.same_holder(holder)
            and (not expected_turn_id or lease.turn_id == expected_turn_id)
        ):
            return WebConnectedWriterReceipt(
                normalized_client_id,
                normalized_root_id,
                lease.holder,
                lease,
            )
        if lease is not None:
            owner = project_owner(lease, client_id=normalized_client_id)
            raise WebRuntimeError(
                f"This active turn is currently controlled by {owner['label']}.",
                code="interaction_owned",
                status=409,
                details={"thread_id": normalized_root_id},
            )
        raise WebRuntimeError(
            "Focus has no active-turn writer for this thread.",
            code="not_interaction_owner",
            status=409,
            details={"thread_id": normalized_root_id},
        )

    def admit_autonomous_turn(
        self,
        client_id: str,
        root_thread_id: str,
        *,
        allow_fresh: bool,
    ) -> WebAutonomousTurnReceipt:
        """Admit a goal/resume call that may start a main turn itself."""

        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_root_id = self._ports.require_thread_id(root_thread_id)
        holder = self.holder(normalized_client_id)
        try:
            current = self._interaction_leases.load(normalized_root_id)
            if current is not None:
                if current.holder.same_holder(holder):
                    if not allow_fresh and not current.turn_id:
                        raise WebRuntimeError(
                            "Focus has no active-turn writer for this thread.",
                            code="not_interaction_owner",
                            status=409,
                            details={"thread_id": normalized_root_id},
                        )
                    return WebAutonomousTurnReceipt(
                        normalized_client_id,
                        normalized_root_id,
                        current,
                        False,
                    )
                owner = project_owner(current, client_id=normalized_client_id)
                raise WebRuntimeError(
                    f"This thread is currently controlled by {owner['label']}.",
                    code="interaction_owned",
                    status=409,
                )
            if not allow_fresh:
                raise WebRuntimeError(
                    "This browser is not the active main-turn writer.",
                    code="not_interaction_owner",
                    status=409,
                    details={"thread_id": normalized_root_id},
                )
            acquired = self._interaction_leases.acquire(normalized_root_id, holder)
        except WebRuntimeError:
            raise
        except Exception as exc:
            raise self._interaction_state_unavailable(normalized_root_id) from exc
        if not acquired.granted or not acquired.acquired or acquired.lease is None:
            owner = project_owner(acquired.lease, client_id=normalized_client_id)
            raise WebRuntimeError(
                f"This thread is currently controlled by {owner['label']}.",
                code="interaction_owned",
                status=409,
            )
        self._publish_owner(normalized_root_id, "web_autonomous_turn_admitted")
        return WebAutonomousTurnReceipt(
            normalized_client_id,
            normalized_root_id,
            acquired.lease,
            True,
        )

    def release_fresh_blank_autonomous_turn(
        self,
        receipt: WebAutonomousTurnReceipt,
        *,
        reason: str,
    ) -> bool:
        """Release only a fresh exact blank generation installed by the call.

        A borrowed or lifecycle-activated lease is never removed.  This is
        the narrow settlement authority for both known-no-start results and
        an outcome-unknown ``thread/resume`` whose temporary Web admission
        must not become a persistent writer claim.
        """

        self._runtime_context_guard()
        if not isinstance(receipt, WebAutonomousTurnReceipt):
            raise TypeError("autonomous-turn release requires a typed receipt")
        if not receipt.acquired or receipt.lease.turn_id:
            return False
        try:
            released = self._interaction_leases.release_if_matches(receipt.lease)
        except Exception as exc:
            raise self._interaction_state_unavailable(receipt.root_thread_id) from exc
        if released:
            self._publish_owner(receipt.root_thread_id, reason)
        return released

    def raise_other_writer(self, client_id: str, thread_id: str) -> None:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        try:
            lease = self._interaction_leases.load(normalized_thread_id)
        except Exception as exc:
            raise self._interaction_state_unavailable(normalized_thread_id) from exc
        if lease is None or lease.holder.same_holder(self.holder(normalized_client_id)):
            return
        owner = project_owner(lease, client_id=normalized_client_id)
        raise WebRuntimeError(
            f"This thread is currently controlled by {owner['label']}.",
            code="interaction_owned",
            status=409,
        )

    def interaction_delivery_decision(
        self,
        root_thread_id: str,
    ) -> WebInteractionDeliveryDecision:
        """Route requests only to the connected active-main-turn Web writer."""

        self._runtime_context_guard()
        normalized_root_id = str(root_thread_id or "").strip()
        try:
            lease = self._interaction_leases.load(normalized_root_id)
        except Exception:
            return WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.DECLINED
            )
        if lease is None or lease.holder.kind != "web" or not lease.turn_id:
            return WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.DECLINED
            )
        client_id = self.client_id_from_holder(lease.holder.holder_id)
        if client_id and self._documents.is_connected(client_id):
            return WebInteractionDeliveryDecision(
                WebInteractionDeliveryDisposition.CONNECTED,
                client_id,
            )
        return WebInteractionDeliveryDecision(
            WebInteractionDeliveryDisposition.DISCONNECTED,
            client_id,
        )

    @staticmethod
    def client_id_from_holder(holder_id: str) -> str:
        normalized = str(holder_id or "").strip()
        return normalized.removeprefix("web:") if normalized.startswith("web:") else ""

    def run_writer_scoped_control_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        operation: str,
        call: Callable[[], Any],
    ) -> Any:
        """Run a synchronous control call without inventing a root writer."""

        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        self.admit_explicit_web_effect(
            normalized_client_id,
            normalized_thread_id,
            operation=operation,
        )
        try:
            result = call()
        except Exception as exc:
            if not self.is_unknown_mutation_error(exc):
                raise
            pending = self.record_unknown_mutation(
                normalized_thread_id,
                operation=operation,
                client_id=normalized_client_id,
            )
            raise self.mutation_unknown_error(pending) from exc
        if self.upstream_outcome_unknown(result):
            pending = self.record_unknown_mutation(
                normalized_thread_id,
                operation=operation,
                client_id=normalized_client_id,
            )
            raise self.mutation_unknown_error(pending)
        return result


    def has_unknown_mutation(self, thread_id: str) -> bool:
        self._runtime_context_guard()
        return self._mutations.contains(thread_id)

    def retire_backend_epoch_after_stop(self) -> WebMutationBackendRetirementReceipt:
        self._runtime_context_guard()
        return self._mutations.retire_backend_epoch_after_stop()

    def require_no_unknown_mutation(self, thread_id: str) -> None:
        self._runtime_context_guard()
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        pending = self._mutations.get(normalized_thread_id)
        if pending is not None:
            raise self._mutation_reconciling_error(normalized_thread_id, pending)

    def admit_explicit_web_effect(
        self,
        client_id: str,
        thread_id: str,
        *,
        operation: str,
        mutation_id: str = "",
    ) -> None:
        """Keep one thread's generic unknown controls exact and isolated."""

        self._runtime_context_guard()
        self._ports.require_connected_document(client_id)
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        normalized_operation = str(operation or "").strip()
        if not normalized_operation:
            raise ValueError("Explicit Web effect admission requires an operation")
        if normalized_operation == "start_prompt":
            if not is_web_mutation_id(mutation_id):
                raise WebRuntimeError(
                    "Prompt admission requires one canonical mutation id.",
                    code="invalid_mutation_id",
                    status=400,
                )
            return
        pending = self._mutations.get(normalized_thread_id)
        if pending is not None:
            raise self._mutation_reconciling_error(normalized_thread_id, pending)

    def unknown_mutation_projection(self, thread_id: str) -> dict[str, Any] | None:
        self._runtime_context_guard()
        pending = self._mutations.get(thread_id)
        if pending is None:
            return None
        return {
            "mutation_id": pending.mutation_id,
            "operation": public_web_mutation_operation(pending.operation),
            "durability": pending.durability,
            "reconciling": True,
            "lifecycle_verification": pending.lifecycle_projection(),
        }

    def unknown_lifecycle_mutations_for_client(
        self, client_id: str
    ) -> list[dict[str, Any]]:
        self._runtime_context_guard()
        return self._mutations.lifecycle_projections_for_client(client_id)

    def reconcile_unknown_from_turns(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
    ) -> bool:
        self._runtime_context_guard()
        settlements = self._mutations.reconcile_turns(thread_id, turns)
        for settlement in settlements:
            self._projection.publish(
                "mutation_reconciled",
                thread_id=settlement.thread_id,
                reason=public_web_mutation_operation(settlement.operation),
                detail=settlement.projection_dict(),
            )
        return bool(settlements)

    def record_unknown_mutation(
        self,
        thread_id: str,
        *,
        operation: str,
        client_id: str,
        turn_id: str = "",
        mutation_id: str = "",
    ) -> WebUnknownMutation:
        self._runtime_context_guard()
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        normalized_operation = str(operation or "mutation").strip() or "mutation"
        pending = WebUnknownMutation.create(
            thread_id=normalized_thread_id,
            operation=normalized_operation,
            client_id=client_id,
            durability="process_local",
            turn_id=turn_id,
            baseline_turn_ids=self._ports.turn_ids(normalized_thread_id),
            mutation_id=mutation_id,
        )
        self._mutations.remember(pending)
        self._projection.publish(
            "mutation_unknown",
            thread_id=normalized_thread_id,
            reason=public_web_mutation_operation(pending.operation),
            detail=self._mutation_event_detail(pending),
        )
        return pending

    def resolve_unknown_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        action: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        normalized_action = str(action or "").strip()
        if normalized_action == "verify_lifecycle":
            return self.verify_unknown_lifecycle_mutation(
                normalized_client_id,
                normalized_thread_id,
                mutation_id=str(mutation_id or "").strip(),
            )
        if normalized_action not in {"discard", "retry"}:
            raise WebRuntimeError(
                "Unknown mutation resolution must be discard, retry, or lifecycle verification.",
                code="invalid_unknown_resolution",
            )
        expected_id, pending, settlement, retired = self._expected_unknown_mutation(
            normalized_thread_id, mutation_id
        )
        if retired is not None:
            self._require_recovery_client(retired.client_id, normalized_client_id)
            raise self._mutation_backend_replaced_error(retired)
        if settlement is not None:
            self._require_recovery_client(settlement.client_id, normalized_client_id)
            return self.settlement_result_for_client(
                settlement, client_id=normalized_client_id
            )
        if pending is None:
            raise self._mutation_replaced_error(normalized_thread_id, expected_id)
        self._require_recovery_client(pending.client_id, normalized_client_id)
        if pending.operation in LIFECYCLE_UNKNOWN_OPERATIONS:
            if normalized_action != "discard":
                raise WebRuntimeError(
                    "Lifecycle unknown mutations cannot be retried automatically.",
                    code="invalid_unknown_resolution",
                    status=409,
                )
            if pending.lifecycle_verification is None:
                raise WebRuntimeError(
                    "Read the current lifecycle state from Codex before dismissing this warning.",
                    code="lifecycle_verification_required",
                    status=409,
                    details={"thread_id": normalized_thread_id},
                )
        disposition = (
            "retry_opened" if normalized_action == "retry" else "user_discard"
        )
        reconciled = self._mutations.settle_exact(
            normalized_thread_id, pending.mutation_id, disposition
        )
        if reconciled is None:
            replacement = self._mutations.settlement_exact(
                normalized_thread_id, expected_id
            )
            if replacement is not None:
                self._require_recovery_client(
                    replacement.client_id, normalized_client_id
                )
                return self.settlement_result_for_client(
                    replacement, client_id=normalized_client_id
                )
            raise self._mutation_replaced_error(normalized_thread_id, expected_id)
        self._projection.publish(
            "mutation_reconciled",
            thread_id=normalized_thread_id,
            reason=f"user_{normalized_action}",
            detail=reconciled.projection_dict(),
        )
        return self.settlement_result_for_client(
            reconciled, client_id=normalized_client_id
        )

    def verify_unknown_lifecycle_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        normalized_thread_id = self._ports.require_thread_id(thread_id)
        expected_id, pending, settlement, retired = self._expected_unknown_mutation(
            normalized_thread_id, mutation_id
        )
        if retired is not None:
            self._require_recovery_client(retired.client_id, normalized_client_id)
            raise self._mutation_backend_replaced_error(retired)
        if settlement is not None:
            self._require_recovery_client(settlement.client_id, normalized_client_id)
            return {
                "accepted": True,
                "mutation_id": expected_id,
                "thread_id": normalized_thread_id,
                "status": "already_reconciled",
                **self._projection.coordinates(),
            }
        if pending is None:
            raise self._mutation_replaced_error(normalized_thread_id, expected_id)
        self._require_recovery_client(pending.client_id, normalized_client_id)
        if pending.operation not in LIFECYCLE_UNKNOWN_OPERATIONS:
            raise WebRuntimeError(
                "Only archive, unarchive, and delete outcomes have lifecycle state to verify.",
                code="mutation_not_lifecycle",
                status=409,
            )
        raw_state = self._ports.read_lifecycle_target_state(normalized_thread_id)
        if raw_state not in {"present", "archived", "deleted"}:
            raise WebRuntimeError(
                "Focus received an unsupported lifecycle verification state.",
                code="lifecycle_verification_unavailable",
                status=503,
            )
        verification = WebLifecycleVerification(
            state=raw_state,  # type: ignore[arg-type]
            verification_id=str(uuid.uuid4()),
        )
        verified = self._mutations.install_lifecycle_verification(
            normalized_thread_id, pending.mutation_id, verification
        )
        if verified is None:
            raise self._mutation_replaced_error(normalized_thread_id, expected_id)
        self._projection.publish(
            "mutation_verified",
            thread_id=normalized_thread_id,
            reason=public_web_mutation_operation(pending.operation),
            detail=self._mutation_event_detail(verified),
        )
        return {
            "accepted": True,
            "mutation_id": verified.mutation_id,
            "thread_id": normalized_thread_id,
            "operation": public_web_mutation_operation(verified.operation),
            "verification": verification.projection_dict(),
            **self._projection.coordinates(),
        }

    @staticmethod
    def upstream_outcome_unknown(result: Any) -> bool:
        return bool(
            isinstance(result, dict)
            and str(result.get("upstream_outcome", "") or "").strip() == "unknown"
        )

    @staticmethod
    def is_unknown_mutation_error(exc: Exception) -> bool:
        for current in iter_exception_chain(exc):
            if isinstance(current, ThreadResumeSettlementError):
                return (
                    current.settlement.outcome
                    is ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                    or current.recovery_required
                )
            if isinstance(
                current,
                (
                    ThreadResumeOutcomeUnknown,
                    ThreadCreateOutcomeUnknown,
                    TimeoutError,
                    CodexRpcTransportError,
                    CodexRpcProtocolError,
                ),
            ):
                return True
        return False

    @staticmethod
    def is_resume_outcome_unknown(exc: Exception) -> bool:
        chain = tuple(iter_exception_chain(exc))
        if any(isinstance(current, ThreadResumeSettlementError) for current in chain):
            return False
        return any(isinstance(current, ThreadResumeOutcomeUnknown) for current in chain)

    @staticmethod
    def is_resume_uncertain_error(exc: Exception) -> bool:
        for current in iter_exception_chain(exc):
            if isinstance(current, ThreadResumeSettlementError):
                return bool(
                    current.settlement.outcome
                    is ThreadResumeSettlementOutcome.STALE_OR_INVARIANT_VIOLATION
                    or current.recovery_required
                )
            if isinstance(current, ThreadResumeOutcomeUnknown):
                return True
        return False

    @staticmethod
    def mutation_unknown_error(pending: WebUnknownMutation) -> WebRuntimeError:
        operation = public_web_mutation_operation(pending.operation)
        return WebRuntimeError(
            f"The Codex {operation} request may have executed, but Focus did not receive a reliable result. "
            "Focus will not replay it automatically; refresh before deciding what to do next.",
            code="mutation_unknown",
            status=409,
            details={
                "mutation_id": pending.mutation_id,
                "thread_id": pending.thread_id,
                "operation": operation,
                "durability": pending.durability,
            },
        )

    def settlement_result_for_client(
        self,
        settlement: WebMutationSettlement,
        *,
        client_id: str,
    ) -> dict[str, Any]:
        self._runtime_context_guard()
        normalized_client_id = self._ports.require_connected_document(client_id)
        self._require_recovery_client(settlement.client_id, normalized_client_id)
        return {
            "accepted": True,
            "mutation_id": settlement.mutation_id,
            "thread_id": settlement.thread_id,
            "disposition": settlement.disposition,
        }

    def _expected_unknown_mutation(
        self,
        thread_id: str,
        mutation_id: str,
    ) -> tuple[
        str,
        WebUnknownMutation | None,
        WebMutationSettlement | None,
        WebBackendRetiredMutation | None,
    ]:
        expected_id = str(mutation_id or "").strip()
        if not expected_id:
            raise WebRuntimeError(
                "Unknown mutation settlement requires an exact mutation id.",
                code="invalid_mutation_id",
                status=400,
            )
        settlement = self._mutations.settlement_exact(thread_id, expected_id)
        if settlement is not None:
            return expected_id, None, settlement, None
        retired = self._mutations.backend_retirement_exact(thread_id, expected_id)
        if retired is not None:
            return expected_id, None, None, retired
        return expected_id, self._mutations.get_exact(thread_id, expected_id), None, None

    @staticmethod
    def _require_recovery_client(source_client_id: str, client_id: str) -> None:
        if source_client_id and source_client_id == client_id:
            return
        raise WebRuntimeError(
            "The unknown mutation belongs to another browser document.",
            code="mutation_not_owned",
            status=409,
        )

    @staticmethod
    def _mutation_reconciling_error(
        thread_id: str,
        current: WebUnknownMutation | None,
    ) -> WebRuntimeError:
        details = {"thread_id": str(thread_id or "").strip()}
        if current is not None:
            details.update(
                {
                    "mutation_id": current.mutation_id,
                    "operation": public_web_mutation_operation(current.operation),
                }
            )
        return WebRuntimeError(
            "A Web control mutation on this thread still has an unknown outcome.",
            code="mutation_reconciling",
            status=409,
            details=details,
        )

    @staticmethod
    def _mutation_replaced_error(thread_id: str, mutation_id: str) -> WebRuntimeError:
        return WebRuntimeError(
            "This process-local mutation result is no longer available.",
            code="mutation_replaced",
            status=409,
            details={
                "mutation_id": str(mutation_id or "").strip(),
                "thread_id": str(thread_id or "").strip(),
            },
        )

    @staticmethod
    def _mutation_backend_replaced_error(
        retired: WebBackendRetiredMutation,
    ) -> WebRuntimeError:
        return WebRuntimeError(
            "The backend that owned this control mutation was replaced; its outcome remains unknown.",
            code="mutation_backend_replaced",
            status=409,
            details={
                "thread_id": retired.thread_id,
                "mutation_id": retired.mutation_id,
                "operation": public_web_mutation_operation(retired.operation),
                "reason": retired.reason,
            },
        )

    @staticmethod
    def _mutation_event_detail(pending: WebUnknownMutation) -> dict[str, str]:
        return {
            "mutation_id": pending.mutation_id,
            "operation": public_web_mutation_operation(pending.operation),
            "durability": pending.durability,
        }

    @staticmethod
    def _interaction_state_unavailable(thread_id: str) -> WebRuntimeError:
        return WebRuntimeError(
            "Focus could not read the active-main-turn owner safely.",
            code="interaction_state_unavailable",
            status=503,
            details={"thread_id": str(thread_id or "").strip()},
        )

    def _publish_owner(self, thread_id: str, reason: str) -> None:
        self._projection.publish(
            "owner_changed", thread_id=thread_id, reason=reason
        )
