"""Pure read-side composition for one exact active-turn disclosure.

This module owns no mutable runtime fact.  It joins the current interaction
lease, Feishu subscriber set, and effective-settings provenance only for Web
presentation; the underlying owners remain authoritative for every value.
"""

from __future__ import annotations

from typing import Any, Callable

from bot.binding_identity import ChatBindingKey, format_binding_id
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.stores.interaction_lease_store import (
    InteractionLease,
    InteractionLeaseStore,
    InteractionLeaseStoreUnavailable,
    feishu_binding_from_holder,
)


_UNOBSERVED_LEASE = object()


class ActiveTurnDisclosureComposer:
    """Compose a disposable DTO without retaining a second runtime view."""

    def __init__(
        self,
        *,
        interaction_leases: InteractionLeaseStore,
        effective_settings: ThreadEffectiveSettingsRegistry,
        thread_subscribers: Callable[[str], tuple[ChatBindingKey, ...]],
    ) -> None:
        if not isinstance(interaction_leases, InteractionLeaseStore):
            raise TypeError("active-turn disclosure requires the lease owner")
        if not isinstance(effective_settings, ThreadEffectiveSettingsRegistry):
            raise TypeError("active-turn disclosure requires effective-settings facts")
        if not callable(thread_subscribers):
            raise TypeError("active-turn disclosure requires Feishu subscribers")
        self._interaction_leases = interaction_leases
        self._effective_settings = effective_settings
        self._thread_subscribers = thread_subscribers

    def compose(
        self,
        thread_id: object,
        turn_id: object,
        *,
        observed_lease: InteractionLease | None | object = _UNOBSERVED_LEASE,
    ) -> dict[str, Any] | None:
        normalized_thread_id = str(thread_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_thread_id or not normalized_turn_id:
            return None

        if observed_lease is _UNOBSERVED_LEASE:
            try:
                lease = self._interaction_leases.load(normalized_thread_id)
            except InteractionLeaseStoreUnavailable:
                # This is presentation-only disclosure.  An unreadable authority
                # must not be guessed, but it also must not make a thread
                # impossible to inspect.  Interactive mutations keep their
                # separate fail-closed lease checks.
                lease = None
        else:
            lease = (
                observed_lease
                if isinstance(observed_lease, InteractionLease)
                else None
            )
        settings = self._effective_settings.disclosure_for_active_turn(
            normalized_thread_id,
            normalized_turn_id,
        )
        return {
            "turn_id": normalized_turn_id,
            "initiator": self._initiator(
                lease,
                thread_id=normalized_thread_id,
                turn_id=normalized_turn_id,
            ),
            "feishu_audience": sorted(
                format_binding_id(binding)
                for binding in self._thread_subscribers(normalized_thread_id)
            ),
            "settings": {
                name: {
                    "value": disclosure.value,
                    "source": disclosure.source,
                }
                for name, disclosure in (
                    ("model", settings.model),
                    ("reasoning_effort", settings.reasoning_effort),
                    ("approval_policy", settings.approval_policy),
                    (
                        "permissions_profile_id",
                        settings.permissions_profile_id,
                    ),
                )
            },
        }

    @staticmethod
    def _initiator(
        lease: InteractionLease | None,
        *,
        thread_id: str,
        turn_id: str,
    ) -> dict[str, str]:
        if (
            lease is None
            or lease.thread_id != thread_id
            or lease.turn_id != turn_id
        ):
            return {"kind": "autonomous_or_unknown", "binding_id": ""}
        holder = lease.holder
        if holder.kind == "feishu":
            binding = feishu_binding_from_holder(holder)
            if binding is not None:
                return {
                    "kind": "feishu",
                    "binding_id": format_binding_id(binding),
                }
        if holder.kind in {"web", "fcodex"}:
            return {"kind": holder.kind, "binding_id": ""}
        return {"kind": "autonomous_or_unknown", "binding_id": ""}
