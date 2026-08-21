"""Single-owner Web runtime-interest records.

The Web controller needs to distinguish three facts which used to be spread
across four containers:

* which browser documents still desire this service subscription;
* whether the latest resume/cleanup outcome is confirmed or unknown;
* which backend connection epoch confirmed the live subscription.

Keeping those values in one per-thread record prevents a thread from being
simultaneously projected as "managed", "uncertain", and current by unrelated
sets.  The registry is RuntimeLoop-owned; it intentionally provides no locks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


WebRuntimeInterestOutcome = Literal["confirmed", "unknown"]


# This is deliberately an allow-list rather than a deny-list.  In upstream
# app-server, metadata and lifecycle notifications such as thread/name/updated,
# thread/started, thread/status/changed, and thread/closed are broadcast to all
# initialized connections.  Receiving one therefore says nothing about this
# connection's thread subscription.  The methods below are emitted through a
# ThreadScopedOutgoingMessageSender whose recipients are the thread's current
# ``subscribed_connection_ids``.  Unknown/future methods must remain
# non-evidence until their upstream routing is reviewed.
_THREAD_SCOPED_NOTIFICATION_EVIDENCE = frozenset(
    {
        "error",
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "item/completed",
        "item/fileChange/outputDelta",
        "item/fileChange/patchUpdated",
        "item/mcpToolCall/progress",
        "item/plan/delta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/textDelta",
        "item/started",
        "model/rerouted",
        "serverRequest/resolved",
        "thread/settings/updated",
        "thread/tokenUsage/updated",
        "turn/completed",
        "turn/diff/updated",
        "turn/plan/updated",
        "turn/started",
    }
)


@dataclass(frozen=True, slots=True)
class WebRuntimeInterestSnapshot:
    thread_id: str
    desired_client_ids: tuple[str, ...]
    ever_confirmed: bool
    subscription_epoch: int
    outcome: WebRuntimeInterestOutcome
    revision: int
    unsubscribe_outcome_unknown: bool


@dataclass(slots=True)
class _WebRuntimeInterest:
    desired_client_ids: set[str] = field(default_factory=set)
    ever_confirmed: bool = False
    subscription_epoch: int = 0
    outcome: WebRuntimeInterestOutcome = "confirmed"
    revision: int = 0
    unsubscribe_outcome_unknown: bool = False


class WebRuntimeInterestRegistry:
    """Own rebuildable Web interest in app-server thread subscriptions."""

    def __init__(self) -> None:
        self._backend_epoch = 1
        self._next_revision = 0
        self._by_thread: dict[str, _WebRuntimeInterest] = {}

    @property
    def backend_epoch(self) -> int:
        return self._backend_epoch

    def subscription_is_current(self, thread_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        return bool(
            record is not None
            and record.outcome == "confirmed"
            and record.subscription_epoch == self._backend_epoch
        )

    def mark_confirmed(self, thread_id: str, *, client_id: str = "") -> None:
        normalized_thread_id = self._thread_id(thread_id)
        record = self._by_thread.setdefault(normalized_thread_id, _WebRuntimeInterest())
        normalized_client_id = self._client_id(client_id)
        if normalized_client_id:
            record.desired_client_ids.add(normalized_client_id)
        record.ever_confirmed = True
        record.subscription_epoch = self._backend_epoch
        record.outcome = "confirmed"
        record.unsubscribe_outcome_unknown = False
        self._touch(record)

    def mark_unknown(self, thread_id: str, *, client_id: str = "") -> None:
        normalized_thread_id = self._thread_id(thread_id)
        record = self._by_thread.setdefault(
            normalized_thread_id,
            _WebRuntimeInterest(outcome="unknown"),
        )
        normalized_client_id = self._client_id(client_id)
        if normalized_client_id:
            record.desired_client_ids.add(normalized_client_id)
        record.outcome = "unknown"
        record.unsubscribe_outcome_unknown = False
        self._touch(record)

    def mark_unsubscribe_unknown(self, thread_id: str) -> None:
        """Retain one transport-unknown unsubscribe without replay authority."""

        normalized_thread_id = self._thread_id(thread_id)
        record = self._by_thread.setdefault(
            normalized_thread_id,
            _WebRuntimeInterest(outcome="unknown"),
        )
        record.outcome = "unknown"
        record.unsubscribe_outcome_unknown = True
        record.subscription_epoch = 0
        self._touch(record)

    def confirm_thread_scoped_notification(
        self,
        thread_id: str,
        *,
        method: str,
    ) -> bool:
        """Refresh a managed subscription only from reviewed routing evidence.

        The exact notification thread is the only subscription this delivery
        attests.  A child notification must not be promoted to its operation
        root: app-server subscriptions are per thread, not per ancestry tree.
        """

        normalized_method = str(method or "").strip()
        if normalized_method not in _THREAD_SCOPED_NOTIFICATION_EVIDENCE:
            return False
        return self._confirm_managed_subscription_in_current_epoch(thread_id)

    def confirm_thread_scoped_server_request(self, thread_id: str) -> bool:
        """Refresh from a server request delivered to this thread subscriber."""

        return self._confirm_managed_subscription_in_current_epoch(thread_id)

    def _confirm_managed_subscription_in_current_epoch(self, thread_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        if record is None or not record.ever_confirmed:
            return False
        record.subscription_epoch = self._backend_epoch
        self._touch(record)
        return True

    def mark_subscription_absent(self, thread_id: str) -> bool:
        """Record authoritative closed/not-loaded lifecycle evidence.

        Browser desire and an unknown mutation outcome are independent facts
        and deliberately survive. Only the claim that this backend epoch has
        a live app-server subscription is invalidated.
        """

        record = self._by_thread.get(self._thread_id(thread_id))
        if record is None:
            return False
        record.subscription_epoch = 0
        self._touch(record)
        return True

    def add_desired_client(self, thread_id: str, client_id: str) -> None:
        """Attach another browser only to an already managed subscription."""

        normalized_thread_id = self._thread_id(thread_id)
        record = self._by_thread.get(normalized_thread_id)
        if record is None or not record.ever_confirmed:
            raise RuntimeError(
                "Web desired-client attachment requires existing managed interest."
            )
        normalized_client_id = self._client_id(client_id)
        if not normalized_client_id:
            return
        record.desired_client_ids.add(normalized_client_id)
        self._touch(record)

    def remove_desired_client(self, thread_id: str, client_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        if record is None:
            return False
        record.desired_client_ids.discard(self._client_id(client_id))
        self._touch(record)
        return bool(record.desired_client_ids)

    def desired_thread_ids_for_client(self, client_id: str) -> tuple[str, ...]:
        """Return every runtime-interest edge owned by one browser document.

        The registry is intentionally indexed by thread because subscription
        outcome and backend epoch are thread facts.  Scanning that one owner is
        preferable to maintaining a second client-to-thread index which could
        drift from ``desired_client_ids``.
        """

        normalized_client_id = self._required_client_id(client_id)
        return tuple(
            sorted(
                thread_id
                for thread_id, record in self._by_thread.items()
                if normalized_client_id in record.desired_client_ids
            )
        )

    def remove_desired_client_from_all(
        self,
        client_id: str,
        *,
        except_thread_id: str | None = None,
    ) -> tuple[str, ...]:
        """Remove all desired edges for one document except an exact target.

        This is a subtractive convergence command.  When ``except_thread_id``
        names a thread without an existing edge, the command does not invent
        one; callers must establish new desire through the normal confirmed or
        managed-interest path.  Thread interest records deliberately survive
        after their client set becomes empty so subscription cleanup remains a
        separate, explicit decision.
        """

        normalized_client_id = self._required_client_id(client_id)
        normalized_exception = (
            self._thread_id(except_thread_id)
            if except_thread_id is not None
            else None
        )
        removed_thread_ids: list[str] = []
        for thread_id, record in self._by_thread.items():
            if thread_id == normalized_exception:
                continue
            if normalized_client_id not in record.desired_client_ids:
                continue
            record.desired_client_ids.remove(normalized_client_id)
            self._touch(record)
            removed_thread_ids.append(thread_id)
        return tuple(sorted(removed_thread_ids))

    def clear_desired_clients(self, thread_id: str) -> None:
        record = self._by_thread.get(self._thread_id(thread_id))
        if record is not None:
            record.desired_client_ids.clear()
            self._touch(record)

    def has_desired_clients(self, thread_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        return bool(record is not None and record.desired_client_ids)

    def has_interest(self, thread_id: str) -> bool:
        return self._thread_id(thread_id) in self._by_thread

    def has_managed_interest(self, thread_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        return bool(record is not None and record.ever_confirmed)

    def is_unknown(self, thread_id: str) -> bool:
        record = self._by_thread.get(self._thread_id(thread_id))
        return bool(record is not None and record.outcome == "unknown")

    def managed_thread_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                thread_id
                for thread_id, record in self._by_thread.items()
                if record.ever_confirmed
            )
        )

    def unknown_thread_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                thread_id
                for thread_id, record in self._by_thread.items()
                if record.outcome == "unknown"
            )
        )

    def forget(self, thread_id: str) -> bool:
        return self._by_thread.pop(self._thread_id(thread_id), None) is not None

    def backend_disconnected(self) -> int:
        self._backend_epoch += 1
        for record in self._by_thread.values():
            self._touch(record)
        return self._backend_epoch

    def clear(self) -> None:
        self._by_thread.clear()

    def snapshot(self, thread_id: str) -> WebRuntimeInterestSnapshot | None:
        normalized_thread_id = self._thread_id(thread_id)
        record = self._by_thread.get(normalized_thread_id)
        if record is None:
            return None
        return WebRuntimeInterestSnapshot(
            thread_id=normalized_thread_id,
            desired_client_ids=tuple(sorted(record.desired_client_ids)),
            ever_confirmed=record.ever_confirmed,
            subscription_epoch=record.subscription_epoch,
            outcome=record.outcome,
            revision=record.revision,
            unsubscribe_outcome_unknown=record.unsubscribe_outcome_unknown,
        )

    def _touch(self, record: _WebRuntimeInterest) -> None:
        self._next_revision += 1
        record.revision = self._next_revision

    @staticmethod
    def _thread_id(thread_id: object) -> str:
        normalized = str(thread_id or "").strip()
        if not normalized:
            raise ValueError("Web runtime interest requires a thread id.")
        return normalized

    @staticmethod
    def _client_id(client_id: object) -> str:
        return str(client_id or "").strip()

    @classmethod
    def _required_client_id(cls, client_id: object) -> str:
        normalized = cls._client_id(client_id)
        if not normalized:
            raise ValueError("Web runtime interest requires a client id.")
        return normalized
