"""RuntimeLoop-owned, process-local Web document continuity.

This registry owns only facts which are meaningful inside the current Focus
process:

* whether a browser document currently has an admitted WebSocket delivery
  path;
* which thread this process has successfully materialized for that document;
* the latest document-scoped HTTP intent generation observed by this process.

It deliberately does not read or write :class:`WebWriterProfileStore`.
In particular, ``materialized_thread_id`` is not a copy of the durable
``selected_thread_id``.  The durable writer profile remains the selection
source of truth; materialization is only bounded-history readiness established
by ``WebSelectionCoordinator`` after the durable selection commit. Runtime
interest remains a separate fact owned by ``WebRuntimeInterestRegistry``.

The Gateway owns browser tokens, sessions, and actual sockets.  Its ordered
lifecycle callbacks are the evidence admitted here.  All access is confined
to RuntimeLoop and therefore uses no internal locks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bot.runtime_loop import RuntimeContextGuard


WebDocumentMutationOutcome = Literal[
    "changed",
    "unchanged",
    "missing",
    "mismatch",
]


class InvalidWebDocumentId(ValueError):
    """Raised when a caller supplies no usable browser document identity."""


class InvalidWebDocumentThreadId(ValueError):
    """Raised when materialization is attempted without an exact thread id."""


class InvalidWebDocumentIntent(ValueError):
    """Raised when an intent generation cannot be represented safely."""


class WebDocumentNotConnected(RuntimeError):
    """Raised when a writer action has no admitted live delivery path."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        super().__init__(f"Web document {client_id!r} is not connected.")


class StaleWebDocumentIntent(RuntimeError):
    """Raised when an older HTTP action arrives after a newer document intent."""

    def __init__(
        self,
        *,
        client_id: str,
        requested_generation: int,
        latest_generation: int,
    ) -> None:
        self.client_id = client_id
        self.requested_generation = requested_generation
        self.latest_generation = latest_generation
        super().__init__(
            "Web document intent is stale: "
            f"client={client_id!r} requested={requested_generation} "
            f"latest={latest_generation}."
        )


@dataclass(frozen=True, slots=True)
class WebDocumentSnapshot:
    """Detached, immutable view of one process-local document record."""

    client_id: str
    connected: bool
    materialized_thread_id: str
    latest_intent_generation: int
    continuity_generation: int = 0


@dataclass(frozen=True, slots=True)
class WebDocumentMutation:
    """Detached before/after receipt for one registry command."""

    outcome: WebDocumentMutationOutcome
    previous: WebDocumentSnapshot
    current: WebDocumentSnapshot


@dataclass(frozen=True, slots=True)
class WebDocumentIntentAdmission:
    """Typed result of admitting a non-stale document intent."""

    client_id: str
    requested_generation: int
    previous_generation: int
    latest_generation: int
    advanced: bool


@dataclass(frozen=True, slots=True)
class WebDocumentOperationReceipt:
    """Exact document-continuity receipt for one staged external operation."""

    client_id: str
    operation: str
    operation_generation: int
    continuity_generation: int
    latest_intent_generation: int
    target_thread_id: str


@dataclass(slots=True)
class _WebDocumentState:
    connected: bool = False
    materialized_thread_id: str = ""
    latest_intent_generation: int = 0
    continuity_generation: int = 0
    operation_generations: dict[str, int] | None = None


class WebDocumentRegistry:
    """Single process-local owner of Web document continuity and intent."""

    def __init__(self, *, runtime_context_guard: RuntimeContextGuard) -> None:
        if not callable(runtime_context_guard):
            raise TypeError("Web document registry requires a RuntimeLoop context guard.")
        self._runtime_context_guard = runtime_context_guard
        self._by_client: dict[str, _WebDocumentState] = {}
        self._next_continuity_generation = 0

    def assert_runtime_context(self) -> None:
        """Assert that the caller is executing on the owning RuntimeLoop."""

        self._runtime_context_guard()

    def snapshot(self, client_id: str) -> WebDocumentSnapshot | None:
        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        return (
            self._snapshot(normalized_client_id, state)
            if state is not None
            else None
        )

    def is_connected(self, client_id: str) -> bool:
        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        return bool(state is not None and state.connected)

    def require_connected(self, client_id: str) -> str:
        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        if state is None or not state.connected:
            raise WebDocumentNotConnected(normalized_client_id)
        return normalized_client_id

    def materialized_thread_id(self, client_id: str) -> str:
        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        return state.materialized_thread_id if state is not None else ""

    def intent_generation_floor(self, client_id: str) -> int:
        """Return the retained monotonic intent floor, with zero for missing."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        return state.latest_intent_generation if state is not None else 0

    def materialized_client_ids(self) -> tuple[str, ...]:
        self._runtime_context_guard()
        return tuple(
            sorted(
                client_id
                for client_id, state in self._by_client.items()
                if state.materialized_thread_id
            )
        )

    def client_ids(self) -> tuple[str, ...]:
        """Return an immutable inventory of every known document record."""

        self._runtime_context_guard()
        return tuple(sorted(self._by_client))

    def mark_connected(self, client_id: str) -> WebDocumentMutation:
        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        previous = self._snapshot_or_empty(normalized_client_id, state)
        if state is None:
            state = self._new_state()
            self._by_client[normalized_client_id] = state
        if state.connected:
            return WebDocumentMutation(
                outcome="unchanged",
                previous=previous,
                current=self._snapshot(normalized_client_id, state),
            )
        state.connected = True
        return WebDocumentMutation(
            outcome="changed",
            previous=previous,
            current=self._snapshot(normalized_client_id, state),
        )

    def mark_transport_disconnected(self, client_id: str) -> WebDocumentMutation:
        """Remove only live delivery; retain materialization and intent.

        Gateway may still reconnect the same document during its bounded
        grace period.  This command must therefore not detach the selected
        runtime edge or reset the monotonic intent floor.
        """

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        previous = self._snapshot_or_empty(normalized_client_id, state)
        if state is None:
            return WebDocumentMutation(
                outcome="missing",
                previous=previous,
                current=previous,
            )
        if not state.connected:
            return WebDocumentMutation(
                outcome="unchanged",
                previous=previous,
                current=previous,
            )
        state.connected = False
        return WebDocumentMutation(
            outcome="changed",
            previous=previous,
            current=self._snapshot(normalized_client_id, state),
        )

    def mark_document_lost(self, client_id: str) -> WebDocumentMutation:
        """Drop live/materialized continuity while retaining the intent floor.

        A Gateway grace-expiry cleanup can finish just as the same document id
        reconnects.  Keeping its latest intent generation prevents an older
        queued HTTP action from becoming current during that race.  Process
        shutdown is the boundary which clears the floor.
        """

        return self._clear_document_continuity(client_id)

    def mark_document_reissued(self, client_id: str) -> WebDocumentMutation:
        """Revoke continuity inherited from an earlier browser document.

        Gateway can deliberately reuse a client id when an F5 navigation
        replaces the browser document.  The replacement must establish its
        own live delivery path and materialize its own thread history, while
        the process-local monotonic intent floor remains in force.
        """

        return self._clear_document_continuity(client_id)

    def _clear_document_continuity(
        self,
        client_id: str,
    ) -> WebDocumentMutation:
        """Clear connected/materialized authority without resetting intent."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        state = self._by_client.get(normalized_client_id)
        previous = self._snapshot_or_empty(normalized_client_id, state)
        if state is None:
            return WebDocumentMutation(
                outcome="missing",
                previous=previous,
                current=previous,
            )
        # Losing/reissuing a document always retires the old continuity epoch,
        # even when its live/materialized flags were already clear.
        changed = True
        state.connected = False
        state.materialized_thread_id = ""
        state.continuity_generation = self._allocate_continuity_generation()
        state.operation_generations = None
        current = self._snapshot(normalized_client_id, state)
        self._prune_if_empty(normalized_client_id, state)
        return WebDocumentMutation(
            outcome="changed" if changed else "unchanged",
            previous=previous,
            current=current,
        )

    def materialize_thread(
        self,
        client_id: str,
        thread_id: str,
    ) -> WebDocumentMutation:
        """Record an already-committed durable selection in this process."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(thread_id)
        state = self._by_client.get(normalized_client_id)
        previous = self._snapshot_or_empty(normalized_client_id, state)
        if state is None:
            state = self._new_state()
            self._by_client[normalized_client_id] = state
        if state.materialized_thread_id == normalized_thread_id:
            return WebDocumentMutation(
                outcome="unchanged",
                previous=previous,
                current=self._snapshot(normalized_client_id, state),
            )
        state.materialized_thread_id = normalized_thread_id
        return WebDocumentMutation(
            outcome="changed",
            previous=previous,
            current=self._snapshot(normalized_client_id, state),
        )

    def forget_materialized_thread_if_matches(
        self,
        client_id: str,
        expected_thread_id: str,
    ) -> WebDocumentMutation:
        """Forget only the exact materialization observed by the caller."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        normalized_thread_id = self._thread_id(expected_thread_id)
        state = self._by_client.get(normalized_client_id)
        previous = self._snapshot_or_empty(normalized_client_id, state)
        if state is None or not state.materialized_thread_id:
            return WebDocumentMutation(
                outcome="missing",
                previous=previous,
                current=previous,
            )
        if state.materialized_thread_id != normalized_thread_id:
            return WebDocumentMutation(
                outcome="mismatch",
                previous=previous,
                current=previous,
            )
        state.materialized_thread_id = ""
        current = self._snapshot(normalized_client_id, state)
        self._prune_if_empty(normalized_client_id, state)
        return WebDocumentMutation(
            outcome="changed",
            previous=previous,
            current=current,
        )

    def forget_materialized_thread_for_all(
        self,
        thread_id: str,
    ) -> tuple[WebDocumentMutation, ...]:
        """Forget one exact thread without disturbing replacement selections."""

        self._runtime_context_guard()
        normalized_thread_id = self._thread_id(thread_id)
        changes: list[WebDocumentMutation] = []
        for client_id, state in tuple(self._by_client.items()):
            if state.materialized_thread_id != normalized_thread_id:
                continue
            previous = self._snapshot(client_id, state)
            state.materialized_thread_id = ""
            current = self._snapshot(client_id, state)
            self._prune_if_empty(client_id, state)
            changes.append(
                WebDocumentMutation(
                    outcome="changed",
                    previous=previous,
                    current=current,
                )
            )
        return tuple(changes)

    def accept_intent(
        self,
        client_id: str,
        generation: object,
    ) -> WebDocumentIntentAdmission:
        """Advance one document's monotonic intent floor, or reject stale work."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        try:
            normalized_generation = int(generation)
        except (TypeError, ValueError) as exc:
            raise InvalidWebDocumentIntent(
                "Browser intent generation must be an integer."
            ) from exc
        if normalized_generation < 0:
            raise InvalidWebDocumentIntent(
                "Browser intent generation must not be negative."
            )
        state = self._by_client.get(normalized_client_id)
        previous_generation = (
            state.latest_intent_generation if state is not None else 0
        )
        if normalized_generation == 0:
            return WebDocumentIntentAdmission(
                client_id=normalized_client_id,
                requested_generation=0,
                previous_generation=previous_generation,
                latest_generation=previous_generation,
                advanced=False,
            )
        if normalized_generation < previous_generation:
            raise StaleWebDocumentIntent(
                client_id=normalized_client_id,
                requested_generation=normalized_generation,
                latest_generation=previous_generation,
            )
        if state is None:
            state = self._new_state()
            self._by_client[normalized_client_id] = state
        advanced = normalized_generation > previous_generation
        state.latest_intent_generation = normalized_generation
        return WebDocumentIntentAdmission(
            client_id=normalized_client_id,
            requested_generation=normalized_generation,
            previous_generation=previous_generation,
            latest_generation=normalized_generation,
            advanced=advanced,
        )

    def begin_operation(
        self,
        client_id: str,
        *,
        operation: str,
        target_thread_id: str = "",
    ) -> WebDocumentOperationReceipt:
        """Issue the only current receipt for one document operation class."""

        self._runtime_context_guard()
        normalized_client_id = self._client_id(client_id)
        normalized_operation = self._operation(operation)
        normalized_thread_id = str(target_thread_id or "").strip()
        state = self._by_client.get(normalized_client_id)
        if state is None:
            state = self._new_state()
            self._by_client[normalized_client_id] = state
        generations = state.operation_generations
        if generations is None:
            generations = {}
            state.operation_generations = generations
        generation = generations.get(normalized_operation, 0) + 1
        generations[normalized_operation] = generation
        return WebDocumentOperationReceipt(
            client_id=normalized_client_id,
            operation=normalized_operation,
            operation_generation=generation,
            continuity_generation=state.continuity_generation,
            latest_intent_generation=state.latest_intent_generation,
            target_thread_id=normalized_thread_id,
        )

    def operation_is_current(
        self,
        receipt: WebDocumentOperationReceipt,
    ) -> bool:
        """Return whether an external result still belongs to this document."""

        self._runtime_context_guard()
        if not isinstance(receipt, WebDocumentOperationReceipt):
            return False
        state = self._by_client.get(receipt.client_id)
        generations = state.operation_generations if state is not None else None
        return bool(
            state is not None
            and state.continuity_generation == receipt.continuity_generation
            and state.latest_intent_generation == receipt.latest_intent_generation
            and generations is not None
            and generations.get(receipt.operation) == receipt.operation_generation
        )

    def clear(self) -> tuple[WebDocumentSnapshot, ...]:
        """Drop all process-local facts at the service lifecycle boundary."""

        self._runtime_context_guard()
        previous = tuple(
            self._snapshot(client_id, state)
            for client_id, state in sorted(self._by_client.items())
        )
        self._by_client.clear()
        return previous

    @staticmethod
    def _snapshot(
        client_id: str,
        state: _WebDocumentState,
    ) -> WebDocumentSnapshot:
        return WebDocumentSnapshot(
            client_id=client_id,
            connected=state.connected,
            materialized_thread_id=state.materialized_thread_id,
            latest_intent_generation=state.latest_intent_generation,
            continuity_generation=state.continuity_generation,
        )

    @classmethod
    def _snapshot_or_empty(
        cls,
        client_id: str,
        state: _WebDocumentState | None,
    ) -> WebDocumentSnapshot:
        if state is None:
            return WebDocumentSnapshot(
                client_id=client_id,
                connected=False,
                materialized_thread_id="",
                latest_intent_generation=0,
                continuity_generation=0,
            )
        return cls._snapshot(client_id, state)

    def _prune_if_empty(self, client_id: str, state: _WebDocumentState) -> None:
        if (
            not state.connected
            and not state.materialized_thread_id
            and state.latest_intent_generation == 0
            and self._by_client.get(client_id) is state
        ):
            self._by_client.pop(client_id, None)

    def _new_state(self) -> _WebDocumentState:
        return _WebDocumentState(
            continuity_generation=self._allocate_continuity_generation()
        )

    def _allocate_continuity_generation(self) -> int:
        self._next_continuity_generation += 1
        return self._next_continuity_generation

    @staticmethod
    def _client_id(client_id: object) -> str:
        normalized = str(client_id or "").strip()
        if not normalized or len(normalized) > 128:
            raise InvalidWebDocumentId("A valid browser client id is required.")
        return normalized

    @staticmethod
    def _thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise InvalidWebDocumentThreadId("A valid thread id is required.")
        return normalized

    @staticmethod
    def _operation(operation: object) -> str:
        normalized = str(operation or "").strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("A valid Web document operation is required.")
        return normalized
