"""Process-local typed evidence for unknown Web control mutations.

Ordinary prompt attempts/results are owned separately by ``prompt_submission``.
This registry retains only lifecycle/control mutations such as archive,
delete, compact, review, and interrupt. It never replays an upstream effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal

from bot.runtime_loop import RuntimeContextGuard


WebLifecycleState = Literal["present", "archived", "deleted"]
WebUnknownMutationDurability = Literal["process_local"]
WebMutationDisposition = Literal[
    "effect_observed",
    "user_discard",
    "retry_opened",
]
LIFECYCLE_UNKNOWN_OPERATIONS = frozenset({"archive", "unarchive", "delete"})
WEB_MUTATION_SETTLEMENT_LIMIT = 256
WEB_MUTATION_BACKEND_RETIREMENT_LIMIT = 256
WEB_MUTATION_ACTIVE_LIMIT = 256


def public_web_mutation_operation(operation: str) -> str:
    """Project the exact generic control-operation name."""

    return operation


def is_web_mutation_id(value: object) -> bool:
    """Return whether value is one canonical UUID attempt identity."""

    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True, slots=True)
class WebLifecycleVerification:
    state: WebLifecycleState
    verification_id: str

    def projection_dict(self) -> dict[str, str]:
        return {"state": self.state, "verification_id": self.verification_id}


@dataclass(frozen=True, slots=True)
class WebUnknownMutation:
    thread_id: str
    operation: str
    client_id: str
    durability: WebUnknownMutationDurability
    mutation_id: str
    turn_id: str = ""
    baseline_turn_ids: tuple[str, ...] = ()
    lifecycle_verification: WebLifecycleVerification | None = None

    @property
    def phase(self) -> Literal["unknown"]:
        return "unknown"

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        operation: str,
        client_id: str,
        durability: WebUnknownMutationDurability,
        turn_id: str = "",
        baseline_turn_ids: Iterable[str] = (),
        mutation_id: str = "",
    ) -> WebUnknownMutation:
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            raise ValueError("Web mutation recovery requires a client id")
        return cls(
            thread_id=str(thread_id or "").strip(),
            operation=str(operation or "mutation").strip() or "mutation",
            client_id=normalized_client_id,
            durability=durability,
            mutation_id=str(mutation_id or "").strip() or str(uuid.uuid4()),
            turn_id=str(turn_id or "").strip(),
            baseline_turn_ids=tuple(
                normalized
                for value in baseline_turn_ids
                if (normalized := str(value or "").strip())
            ),
        )

    def lifecycle_projection(self) -> dict[str, str] | None:
        return (
            self.lifecycle_verification.projection_dict()
            if self.lifecycle_verification is not None
            else None
        )


@dataclass(frozen=True, slots=True)
class WebMutationSettlement:
    thread_id: str
    mutation_id: str
    operation: str
    client_id: str
    disposition: WebMutationDisposition
    turn_id: str = ""

    def projection_dict(self) -> dict[str, str]:
        return {
            "mutation_id": self.mutation_id,
            "operation": public_web_mutation_operation(self.operation),
            "disposition": self.disposition,
        }


@dataclass(frozen=True, slots=True)
class WebBackendRetiredMutation:
    thread_id: str
    mutation_id: str
    operation: str
    client_id: str
    reason: Literal["backend_replaced"] = "backend_replaced"


@dataclass(frozen=True, slots=True)
class WebMutationBackendRetirementReceipt:
    retired_count: int
    retired_mutation_ids: tuple[str, ...]


class WebMutationRecoveryRegistry:
    """Sole owner of process-local lifecycle/control mutation evidence."""

    def __init__(self, *, runtime_context_guard: RuntimeContextGuard) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web mutation recovery requires a RuntimeLoop guard")
        self._runtime_context_guard = runtime_context_guard
        self._active: dict[tuple[str, str], WebUnknownMutation] = {}
        self._settled: dict[tuple[str, str], WebMutationSettlement] = {}
        self._backend_retired: dict[tuple[str, str], WebBackendRetiredMutation] = {}

    def remember(self, mutation: WebUnknownMutation) -> None:
        self._runtime_context_guard()
        if not isinstance(mutation, WebUnknownMutation):
            raise TypeError("Web mutation recovery requires typed evidence")
        key = (mutation.thread_id, mutation.mutation_id)
        if not mutation.thread_id or not mutation.mutation_id:
            raise ValueError("Web mutation recovery requires exact ids")
        if key in self._active:
            raise RuntimeError("Web mutation recovery already has this exact attempt")
        if len(self._active) >= WEB_MUTATION_ACTIVE_LIMIT:
            raise RuntimeError("Web mutation recovery active capacity is exhausted")
        self._active[key] = mutation

    def get(self, thread_id: str) -> WebUnknownMutation | None:
        attempts = self.attempts_for_thread(thread_id)
        return attempts[-1] if attempts else None

    def get_exact(self, thread_id: str, mutation_id: str) -> WebUnknownMutation | None:
        self._runtime_context_guard()
        return self._active.get(
            (str(thread_id or "").strip(), str(mutation_id or "").strip())
        )

    def attempts_for_thread(self, thread_id: str) -> tuple[WebUnknownMutation, ...]:
        self._runtime_context_guard()
        normalized = str(thread_id or "").strip()
        return tuple(
            item
            for (candidate_thread, _mutation), item in self._active.items()
            if candidate_thread == normalized
        )

    def contains(self, thread_id: str) -> bool:
        return bool(self.attempts_for_thread(thread_id))

    def settlement_exact(
        self, thread_id: str, mutation_id: str
    ) -> WebMutationSettlement | None:
        self._runtime_context_guard()
        return self._settled.get(
            (str(thread_id or "").strip(), str(mutation_id or "").strip())
        )

    def backend_retirement_exact(
        self, thread_id: str, mutation_id: str
    ) -> WebBackendRetiredMutation | None:
        self._runtime_context_guard()
        return self._backend_retired.get(
            (str(thread_id or "").strip(), str(mutation_id or "").strip())
        )

    def retire_backend_epoch_after_stop(self) -> WebMutationBackendRetirementReceipt:
        self._runtime_context_guard()
        active = tuple(self._active.values())
        for item in active:
            key = (item.thread_id, item.mutation_id)
            self._backend_retired.pop(key, None)
            self._backend_retired[key] = WebBackendRetiredMutation(
                thread_id=item.thread_id,
                mutation_id=item.mutation_id,
                operation=item.operation,
                client_id=item.client_id,
            )
        while len(self._backend_retired) > WEB_MUTATION_BACKEND_RETIREMENT_LIMIT:
            self._backend_retired.pop(next(iter(self._backend_retired)))
        self._active.clear()
        return WebMutationBackendRetirementReceipt(
            retired_count=len(active),
            retired_mutation_ids=tuple(sorted(item.mutation_id for item in active)),
        )

    def settle_exact(
        self,
        thread_id: str,
        mutation_id: str,
        disposition: WebMutationDisposition,
    ) -> WebMutationSettlement | None:
        self._runtime_context_guard()
        key = (str(thread_id or "").strip(), str(mutation_id or "").strip())
        current = self._active.pop(key, None)
        if current is None:
            return None
        if disposition not in {"effect_observed", "user_discard", "retry_opened"}:
            raise ValueError("Web mutation settlement requires a typed disposition")
        settlement = WebMutationSettlement(
            thread_id=current.thread_id,
            mutation_id=current.mutation_id,
            operation=current.operation,
            client_id=current.client_id,
            disposition=disposition,
            turn_id=current.turn_id,
        )
        self._settled.pop(key, None)
        self._settled[key] = settlement
        while len(self._settled) > WEB_MUTATION_SETTLEMENT_LIMIT:
            self._settled.pop(next(iter(self._settled)))
        return settlement

    def install_lifecycle_verification(
        self,
        thread_id: str,
        mutation_id: str,
        verification: WebLifecycleVerification,
    ) -> WebUnknownMutation | None:
        self._runtime_context_guard()
        key = (str(thread_id or "").strip(), str(mutation_id or "").strip())
        current = self._active.get(key)
        if current is None:
            return None
        updated = replace(current, lifecycle_verification=verification)
        self._active[key] = updated
        return updated

    def lifecycle_projections_for_client(self, client_id: str) -> list[dict[str, Any]]:
        self._runtime_context_guard()
        normalized = str(client_id or "").strip()
        return [
            {
                "mutation_id": item.mutation_id,
                "thread_id": item.thread_id,
                "operation": public_web_mutation_operation(item.operation),
                "verification": item.lifecycle_projection(),
            }
            for item in sorted(
                self._active.values(), key=lambda value: (value.thread_id, value.mutation_id)
            )
            if item.client_id == normalized
            and item.operation in LIFECYCLE_UNKNOWN_OPERATIONS
        ]

    def reconcile_turns(
        self,
        thread_id: str,
        turns: Iterable[dict[str, Any]],
    ) -> tuple[WebMutationSettlement, ...]:
        self._runtime_context_guard()
        materialized = tuple(turn for turn in turns if isinstance(turn, dict))
        settlements: list[WebMutationSettlement] = []
        for pending in self.attempts_for_thread(thread_id):
            baseline = set(pending.baseline_turn_ids)
            matched = False
            for turn in materialized:
                turn_id = str(turn.get("id", "") or "").strip()
                status = str(turn.get("status", "") or "").strip()
                if pending.operation in {"compact", "review"}:
                    matched = bool(turn_id and turn_id not in baseline)
                elif pending.operation == "interrupt":
                    matched = bool(
                        turn_id
                        and turn_id == pending.turn_id
                        and status in {"completed", "interrupted", "failed"}
                    )
                if matched:
                    break
            if matched:
                settlement = self.settle_exact(
                    pending.thread_id, pending.mutation_id, "effect_observed"
                )
                if settlement is not None:
                    settlements.append(settlement)
        return tuple(settlements)

    def snapshot(self) -> tuple[WebUnknownMutation, ...]:
        self._runtime_context_guard()
        return tuple(
            sorted(
                self._active.values(), key=lambda value: (value.thread_id, value.mutation_id)
            )
        )
