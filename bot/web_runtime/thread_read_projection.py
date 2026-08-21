"""Detached Web thread-directory and history DTO projection.

This module owns no runtime fact.  Its typed inputs are frozen by the
RuntimeLoop transaction owner, while CPU-heavy turn projection and bounded
attachment-cache materialization run on the external transaction worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.runtime_state import is_confirmed_inactive_backend_thread_status
from bot.stores.interaction_lease_store import InteractionLease
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.stores.web_writer_profile_store import WebWriterProfile
from bot.thread_runtime_coordination import ManagedLoadedThreadInventorySnapshot
from bot.web_runtime.document_registry import WebDocumentOperationReceipt
from bot.web_runtime.projection import (
    project_owner,
    project_thread_snapshot,
    project_thread_summary,
    project_turn_page,
)
from bot.web_runtime.thread_read_model import WebThreadReadObservationReceipt


@dataclass(frozen=True, slots=True)
class WebThreadProjectionReceipt:
    """Small generation-linearized facts used by projection outside the gate."""

    runtime_epoch: str
    revision: int
    cwd: str = ""

    def coordinates(self) -> dict[str, Any]:
        return {
            "runtime_epoch": self.runtime_epoch,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class WebThreadListProjection:
    client_id: str
    document: WebDocumentOperationReceipt | None
    connection_generation: int
    receipt: WebThreadProjectionReceipt
    scope: str
    archived: bool
    thread_limit: int
    instance_name: str
    summaries: tuple[ThreadSummary, ...]
    loaded_thread_ids: frozenset[str]
    managed_inventory: ManagedLoadedThreadInventorySnapshot
    runtime_leases: tuple[ThreadRuntimeLease, ...]
    interaction_leases: tuple[InteractionLease, ...]
    observed_thread_ids: frozenset[str]
    pending_by_thread: tuple[tuple[str, str], ...]
    document_connected: bool


@dataclass(frozen=True, slots=True)
class WebThreadOpenProjection:
    client_id: str
    document: WebDocumentOperationReceipt
    connection_generation: int
    receipt: WebThreadProjectionReceipt
    snapshot: ThreadSnapshot
    owner_lease: InteractionLease | None
    loaded_instance: str
    observed_here: bool
    pending_requests: tuple[dict[str, Any], ...]
    coordinates: dict[str, Any]
    older_turn_cursor: str
    goal: ThreadGoalSummary | None
    token_usage: dict[str, Any] | None
    token_usage_available: bool
    active_turn_context: dict[str, Any] | None
    document_connected: bool
    mutation_unknown: dict[str, Any] | None
    selection_scope: dict[str, Any]
    final_observation: WebThreadReadObservationReceipt


@dataclass(frozen=True, slots=True)
class WebThreadOpenCommit:
    receipt: WebThreadProjectionReceipt
    observation: WebThreadReadObservationReceipt
    goal: ThreadGoalSummary | None


@dataclass(frozen=True, slots=True)
class WebThreadOpenSettlement:
    goal: ThreadGoalSummary | None
    release_autonomous_reason: str = ""


@dataclass(frozen=True, slots=True)
class WebThreadOpenFailureSettlement:
    error: Exception
    select_thread: bool = False
    release_autonomous_reason: str = ""
    publish_autonomous_reason: str = ""
    clear_unusable_reason: str = ""


@dataclass(frozen=True, slots=True)
class WebThreadHistoryProjection:
    client_id: str
    document: WebDocumentOperationReceipt
    connection_generation: int
    observation: WebThreadReadObservationReceipt
    items_view: str
    page: ThreadTurnsPage
    receipt: WebThreadProjectionReceipt


@dataclass(frozen=True, slots=True)
class WebThreadHistoryEffect:
    profile: WebWriterProfile | None
    page: ThreadTurnsPage


def project_thread_action_capabilities(
    client_id: str,
    summary: ThreadSummary,
    *,
    interaction_lease: InteractionLease | None,
    document_connected: bool,
    archived: bool = False,
) -> dict[str, bool]:
    """Project advisory controls without consulting mutable authority owners."""

    thread_id = str(summary.thread_id or "").strip()
    holder = interaction_lease.holder if interaction_lease is not None else None
    owned_by_document = bool(
        holder is not None
        and holder.kind == "web"
        and holder.holder_id == f"web:{str(client_id or '').strip()}"
    )
    direct_target = str(summary.subagent_kind or "").strip() != "threadSpawn"
    mutable = bool(
        thread_id
        and direct_target
        and document_connected
        and (interaction_lease is None or owned_by_document)
    )
    inactive = is_confirmed_inactive_backend_thread_status(summary.status)
    active_writer = bool(
        interaction_lease is not None
        and interaction_lease.turn_id
        and owned_by_document
    )
    return {
        "rename": mutable,
        "archive": mutable and inactive and not archived,
        "unarchive": mutable and inactive and archived,
        "delete": mutable and inactive and archived,
        "compact": mutable and inactive and not archived,
        "fork": False,
        "export": False,
        "review": mutable and inactive and not archived,
        "goal": mutable
        and (
            inactive
            or (str(summary.status or "").strip() == "active" and active_writer)
        ),
    }


def project_thread_list(
    projection: WebThreadListProjection,
) -> dict[str, Any]:
    """Build one claimed directory payload without touching RuntimeLoop owners."""

    summaries = list(projection.summaries)
    truncated = bool(
        projection.archived and len(summaries) > projection.thread_limit
    )
    if truncated:
        summaries = summaries[: projection.thread_limit]
    loaded_ids = set(projection.loaded_thread_ids)
    remote_loaded_instances: dict[str, set[str]] = {}
    unverified_instance_names: list[str] = []
    for inventory in projection.managed_inventory.instances:
        if not inventory.verified:
            unverified_instance_names.append(inventory.instance_name)
            continue
        for thread_id in inventory.loaded_thread_ids:
            remote_loaded_instances.setdefault(thread_id, set()).add(
                inventory.instance_name
            )
    pending_by_thread = dict(projection.pending_by_thread)
    runtime_leases = {
        lease.thread_id: lease for lease in projection.runtime_leases
    }
    interaction_leases = {
        lease.thread_id: lease for lease in projection.interaction_leases
    }
    projected: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.subagent_kind == "threadSpawn":
            continue
        runtime_lease = (
            None
            if projection.archived
            else runtime_leases.get(summary.thread_id)
        )
        known_loaded_instances = set(
            remote_loaded_instances.get(summary.thread_id, set())
        )
        lease_instance = str(
            getattr(runtime_lease, "owner_instance", "") or ""
        ).strip()
        if lease_instance:
            known_loaded_instances.add(lease_instance)
        if summary.thread_id in loaded_ids:
            known_loaded_instances.add(projection.instance_name)
        observed_here = summary.thread_id in projection.observed_thread_ids
        if observed_here:
            known_loaded_instances.add(projection.instance_name)
        if projection.scope == "current" and not (
            projection.instance_name in known_loaded_instances
            or observed_here
            or summary.thread_id in loaded_ids
        ):
            continue
        loaded_instance = (
            projection.instance_name
            if observed_here or projection.instance_name in known_loaded_instances
            else (
                next(iter(known_loaded_instances))
                if len(known_loaded_instances) == 1
                else ""
            )
        )
        loaded_state_verified = (
            projection.scope != "global"
            or projection.archived
            or len(known_loaded_instances) == 1
            or (
                not known_loaded_instances
                and projection.managed_inventory.verified
            )
        )
        selectable = True
        unavailable_reason = ""
        if not projection.archived:
            if len(known_loaded_instances) > 1:
                selectable = False
                unavailable_reason = (
                    "Multiple local Focus instances report this thread as loaded: "
                    f"{', '.join(sorted(known_loaded_instances))}. "
                    "Refresh the directory and inspect those instances before opening it."
                )
            elif (
                not observed_here
                and loaded_instance
                and loaded_instance != projection.instance_name
            ):
                selectable = False
                unavailable_reason = (
                    "Open this thread from the Focus Web instance that currently "
                    f"keeps it loaded: {loaded_instance}."
                )
            elif (
                not known_loaded_instances
                and projection.managed_inventory.registry_error
            ):
                selectable = False
                unavailable_reason = (
                    "Focus could not verify loaded-thread state for other local "
                    "Focus instances. Refresh the directory after the local "
                    "instance registry is available."
                )
            elif not known_loaded_instances and unverified_instance_names:
                selectable = False
                unavailable_reason = (
                    "Focus could not verify loaded-thread state for local Focus "
                    f"instance(s): {', '.join(sorted(unverified_instance_names))}. "
                    "Refresh the directory after those instances are reachable."
                )
        interaction_lease = interaction_leases.get(summary.thread_id)
        projected.append(
            project_thread_summary(
                summary,
                owner=project_owner(
                    interaction_lease,
                    client_id=projection.client_id,
                ),
                pending_interaction=pending_by_thread.get(
                    summary.thread_id,
                    "none",
                ),
                loaded_instance=loaded_instance,
                loaded_state_verified=loaded_state_verified,
                observed_here=observed_here,
                selectable=selectable,
                unavailable_reason=unavailable_reason,
                action_capabilities=project_thread_action_capabilities(
                    projection.client_id,
                    summary,
                    interaction_lease=interaction_lease,
                    document_connected=projection.document_connected,
                    archived=projection.archived,
                ),
            )
        )
    return {
        **projection.receipt.coordinates(),
        "scope": projection.scope,
        "archived": projection.archived,
        "limit": projection.thread_limit,
        "truncated": truncated,
        "threads": projected,
    }


def project_open_thread(
    projection: WebThreadOpenProjection,
    *,
    attachment_url_for_path: Callable[[str], str],
    attachment_url_for_id: Callable[[str], str],
) -> dict[str, Any]:
    """Materialize one claimed thread snapshot on the external worker."""

    snapshot = projection.snapshot
    result = project_thread_snapshot(
        snapshot,
        owner=project_owner(
            projection.owner_lease,
            client_id=projection.client_id,
        ),
        loaded_instance=projection.loaded_instance,
        observed_here=projection.observed_here,
        pending_requests=projection.pending_requests,
        coordinates=projection.coordinates,
        older_turn_cursor=projection.older_turn_cursor,
        goal=projection.goal,
        token_usage=projection.token_usage,
        token_usage_available=projection.token_usage_available,
        active_turn_context=projection.active_turn_context,
        attachment_url_for_path=attachment_url_for_path,
        attachment_url_for_id=attachment_url_for_id,
        action_capabilities=project_thread_action_capabilities(
            projection.client_id,
            snapshot.summary,
            interaction_lease=projection.owner_lease,
            document_connected=projection.document_connected,
        ),
    )
    result["mutation_unknown"] = projection.mutation_unknown
    result["selection_scope"] = projection.selection_scope
    return result


def project_older_turns(
    projection: WebThreadHistoryProjection,
    *,
    attachment_url_for_path: Callable[[str], str],
    attachment_url_for_id: Callable[[str], str],
) -> dict[str, Any]:
    """Project a claimed bounded history page on the external worker."""

    return project_turn_page(
        projection.page.turns,
        items_view=projection.items_view,
        page_cursor=projection.page.backwards_cursor,
        next_cursor=projection.page.next_cursor,
        coordinates=projection.receipt.coordinates(),
        attachment_url_for_path=attachment_url_for_path,
        attachment_url_for_id=attachment_url_for_id,
    )
