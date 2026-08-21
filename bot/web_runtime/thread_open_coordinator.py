"""Web thread inventory, open, and bounded-history transaction owner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from bot.active_turn_disclosure import ActiveTurnDisclosureComposer
from bot.adapter_ingress_gate import AdapterOutboundRequestEpochLost
from bot.adapters.base import (
    ThreadGoalSummary,
    ThreadResumePage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.codex_protocol.client import CodexRpcError
from bot.interaction_contract import MCP_ELICITATION, USER_INPUT
from bot.runtime_loop import RuntimeContextGuard
from bot.runtime_state import LOADED_BACKEND_THREAD_STATUSES
from bot.stores.interaction_lease_store import InteractionLease, InteractionLeaseStore
from bot.stores.thread_runtime_lease_store import ThreadRuntimeLease
from bot.stores.web_next_turn_settings_store import WebNextTurnSettings
from bot.stores.web_writer_profile_store import WebWriterProfile
from bot.thread_runtime_authority import (
    PendingThreadResume,
    PreparedThreadResumePage,
    ThreadResumeClaimReceipt,
    ThreadResumeInProgress,
    ThreadResumeLeaseReceipt,
    ThreadResumeLocalCommitFailed,
    ThreadResumeLocalFailurePolicy,
)
from bot.thread_runtime_coordination import (
    ManagedLoadedThreadInventorySnapshot,
    ThreadRuntimeAdmissionError,
)
from bot.web_runtime.direct_thread_target_coordinator import (
    WebDirectThreadTargetCoordinator,
    require_web_direct_thread_snapshot,
)
from bot.web_runtime.document_registry import (
    WebDocumentOperationReceipt,
    WebDocumentRegistry,
)
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy
from bot.web_runtime.gateway_external_transaction import capture_external_failure
from bot.web_runtime.interaction_inbox import (
    WebInteractionInbox,
    WebPendingInteractionSnapshot,
)
from bot.web_runtime.operation_service import WebOperationService
from bot.web_runtime.projection import FocusWebProjection
from bot.web_runtime.thread_read_projection import (
    WebThreadHistoryEffect,
    WebThreadHistoryProjection,
    WebThreadListProjection,
    WebThreadOpenCommit,
    WebThreadOpenFailureSettlement,
    WebThreadOpenProjection,
    WebThreadOpenSettlement,
    WebThreadProjectionReceipt,
    project_older_turns,
    project_open_thread,
    project_thread_list,
)
from bot.web_runtime.contract import WebAutonomousTurnReceipt, WebRuntimeError
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.lifecycle_coordinator import WebRuntimeLifecycleCoordinator
from bot.web_runtime.selection_coordinator import (
    WebSelectionCoordinator,
    WebSelectionNotReady,
    WebThreadSelection,
)
from bot.web_runtime.thread_read_model import (
    PreparedWebThreadTurns,
    WebThreadReadModel,
    WebThreadReadObservationReceipt,
)
from bot.web_runtime.turn_window import DEFAULT_TURN_WINDOW_LIMIT, require_turn_window_limit
from bot.web_runtime.writer_workspace_coordinator import (
    WebWorkspaceProfileSnapshot,
    WebWriterWorkspaceCoordinator,
    accept_web_document_intent,
    require_web_client_id,
    require_web_thread_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebThreadOpenPorts:
    """Upstream reads and resume transport; no port re-enters the façade."""

    list_threads: Callable[..., list[ThreadSummary]]
    read_thread: Callable[..., ThreadSnapshot]
    list_loaded_thread_ids: Callable[..., list[str]]
    managed_loaded_thread_inventory: Callable[
        [], ManagedLoadedThreadInventorySnapshot
    ]
    list_thread_runtime_leases: Callable[[], list[ThreadRuntimeLease]]
    begin_resume_thread_page: Callable[..., PendingThreadResume[ThreadResumePage]]
    claim_resume_thread_page: Callable[[str], ThreadResumeClaimReceipt]
    acquire_claimed_resume_thread_page: Callable[..., ThreadResumeLeaseReceipt]
    complete_claimed_resume_thread_page: Callable[..., PreparedThreadResumePage]
    abandon_resume_thread_page_claim: Callable[[ThreadResumeClaimReceipt], None]
    abandon_acquired_resume_thread_page: Callable[[ThreadResumeLeaseReceipt], None]
    execute_prepared_resume_thread_page: Callable[
        [PreparedThreadResumePage],
        ThreadResumePage,
    ]
    settle_prepared_resume_thread_page: Callable[..., PendingThreadResume[ThreadResumePage]]
    list_thread_turns: Callable[..., ThreadTurnsPage]
    get_thread_goal: Callable[..., ThreadGoalSummary | None]
    prepare_runtime_lease_preflight: Callable[[str], Any]
    capture_connection_generation: Callable[[], int]
    run_if_connection_generation: Callable[[int, Callable[[], Any]], Any]


@dataclass(frozen=True, slots=True)
class WebThreadListPreparation:
    client_id: str
    document: WebDocumentOperationReceipt | None
    connection_generation: int
    search: str
    scope: str
    archived: bool
    all_for_search: bool
    query_limit: int
    managed_thread_ids: tuple[str, ...]
    runtime_epoch: str
    projection_revision: int


@dataclass(frozen=True, slots=True)
class WebThreadListEffect:
    summaries: tuple[ThreadSummary, ...]
    loaded_thread_ids: frozenset[str]
    managed_inventory: ManagedLoadedThreadInventorySnapshot
    runtime_leases: tuple[ThreadRuntimeLease, ...]
    interaction_leases: tuple[InteractionLease, ...]


@dataclass(frozen=True, slots=True)
class WebThreadOpenPreparation:
    client_id: str
    thread_id: str
    turn_limit: int
    document: WebDocumentOperationReceipt
    observation: WebThreadReadObservationReceipt
    connection_generation: int
    subscription_current: bool


@dataclass(frozen=True, slots=True)
class WebThreadOpenObservation:
    metadata: ThreadSnapshot
    profile: WebWorkspaceProfileSnapshot
    goal: ThreadGoalSummary | None
    goal_known: bool
    goal_requires_writer_admission: bool
    runtime_lease_preflight: object | None


@dataclass(frozen=True, slots=True)
class WebThreadOpenEffectPreparation:
    initial: WebThreadOpenPreparation
    observed: WebThreadOpenObservation
    kind: str
    live_subscription: bool
    resume_claim: ThreadResumeClaimReceipt | None = None


@dataclass(frozen=True, slots=True)
class WebThreadOpenEffect:
    page: ThreadTurnsPage | ThreadResumePage | None = None
    error: Exception | None = None
    post_resume_goal: ThreadGoalSummary | None = None
    post_resume_goal_known: bool = False
    post_resume_goal_error: Exception | None = None
    prepared_turns: PreparedWebThreadTurns | None = None
    interaction_lease: InteractionLease | None = None
    interaction_lease_error: Exception | None = None
    normalized_cwd: str = ""
    resume: PreparedThreadResumePage | None = None
    profile: WebWorkspaceProfileSnapshot | None = None
    autonomous_admission: WebAutonomousTurnReceipt | None = None
    fatal_error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class WebThreadHistoryPreparation:
    client_id: str
    thread_id: str
    cursor: str
    items_view: str
    turn_limit: int
    document: WebDocumentOperationReceipt
    observation: WebThreadReadObservationReceipt
    connection_generation: int


def classify_web_thread_unavailable_error(
    exc: Exception,
) -> tuple[str, int, str, bool] | None:
    """Map reviewed upstream read failures to the public Web open contract."""

    if isinstance(exc, ThreadResumeInProgress):
        return (
            "runtime_resume_in_progress",
            409,
            "Another request is already resuming this thread. Refresh after that "
            "request settles; Focus did not send a second resume.",
            False,
        )

    if isinstance(exc, ThreadRuntimeAdmissionError):
        instance = exc.blocking_instance
        if instance and exc.blocking_status in LOADED_BACKEND_THREAD_STATUSES:
            return (
                "thread_loaded_elsewhere",
                409,
                f"This thread is still loaded by Focus instance {instance}. "
                f"Open it with `focusctl --instance {instance} web open`, or wait for that runtime to unload.",
                True,
            )
        target = f" Focus instance {instance}" if instance else " local Focus runtimes"
        return (
            "thread_runtime_unverified",
            503,
            f"Focus could not verify loaded-thread state for{target}, so it did not open this thread. "
            "Retry after local runtime coordination is available.",
            False,
        )
    if isinstance(exc, CodexRpcError):
        message = str(exc.error.get("message", "") or "").strip()
        lowered = message.lower()
        if " is archived" in lowered:
            return (
                "thread_archived",
                409,
                "This thread is archived. Unarchive it before opening it in Focus Web.",
                True,
            )
        if "not found" in lowered or "does not exist" in lowered:
            return ("thread_not_found", 404, "This thread no longer exists.", True)
    if isinstance(exc, ValueError):
        message = str(exc).strip().lower()
        if "未找到匹配的线程" in message or message == "unknown thread":
            return ("thread_not_found", 404, "This thread no longer exists.", True)
    return None


class WebThreadOpenCoordinator:
    """Run Web inventory, thread-open, and older-history transactions."""

    def __init__(
        self,
        *,
        instance_name: str,
        documents: WebDocumentRegistry,
        workspace: WebWriterWorkspaceCoordinator,
        operations: WebOperationService,
        lifecycle: WebRuntimeLifecycleCoordinator,
        direct_targets: WebDirectThreadTargetCoordinator,
        goal_resume_policy: WebGoalResumePolicy,
        read_model: WebThreadReadModel,
        runtime_interest: WebRuntimeInterestRegistry,
        selection: WebSelectionCoordinator,
        projection: FocusWebProjection,
        interaction_leases: InteractionLeaseStore,
        interaction_inbox: WebInteractionInbox,
        active_turn_disclosure: ActiveTurnDisclosureComposer,
        next_turn_settings: Callable[[], WebNextTurnSettings],
        shared_interaction_eligible: Callable[[str, str, str, str], bool],
        ports: WebThreadOpenPorts,
        runtime_context_guard: RuntimeContextGuard,
        runtime_call: Callable[..., Any],
        thread_limit: int = 200,
    ) -> None:
        if not isinstance(ports, WebThreadOpenPorts):
            raise TypeError("Web thread open requires typed ports")
        if not callable(next_turn_settings):
            raise TypeError("Web thread open requires next-turn settings")
        if not callable(runtime_context_guard):
            raise TypeError("Web thread open requires a RuntimeLoop context guard")
        if not callable(runtime_call):
            raise TypeError("Web thread open requires a RuntimeLoop call boundary")
        self._instance_name = str(instance_name or "default").strip() or "default"
        self._documents = documents
        self._workspace = workspace
        self._operations = operations
        self._lifecycle = lifecycle
        self._direct_targets = direct_targets
        self._goal_resume_policy = goal_resume_policy
        self._read_model = read_model
        self._runtime_interest = runtime_interest
        self._selection = selection
        self._projection = projection
        self._interaction_leases = interaction_leases
        self._interaction_inbox = interaction_inbox
        self._active_turn_disclosure = active_turn_disclosure
        self._next_turn_settings = next_turn_settings
        self._shared_interaction_eligible = shared_interaction_eligible
        self._ports = ports
        self._runtime_context_guard = runtime_context_guard
        self._runtime_call = runtime_call
        self._thread_limit = max(int(thread_limit), 1)

    def prepare_list_threads(
        self,
        *,
        client_id: str = "",
        search: str = "",
        scope: str = "global",
        archived: bool = False,
        all_for_search: bool = False,
    ) -> WebThreadListPreparation:
        """Freeze one bounded inventory request without entering app-server I/O."""

        self._runtime_context_guard()
        normalized_scope = str(scope or "global").strip().lower()
        if normalized_scope not in {"current", "global"}:
            raise WebRuntimeError(
                "Thread scope must be current or global.",
                code="invalid_scope",
            )
        query_limit = (
            min(self._thread_limit * 10, 2000)
            if all_for_search
            else self._thread_limit + (1 if archived else 0)
        )
        normalized_client_id = str(client_id or "").strip()
        document = (
            self._documents.begin_operation(
                normalized_client_id,
                operation="thread_list",
            )
            if normalized_client_id
            else None
        )
        coordinates = self._projection.coordinates()
        return WebThreadListPreparation(
            client_id=normalized_client_id,
            document=document,
            connection_generation=self._ports.capture_connection_generation(),
            search=str(search or "").strip(),
            scope=normalized_scope,
            archived=bool(archived),
            all_for_search=bool(all_for_search),
            query_limit=query_limit,
            managed_thread_ids=self._runtime_interest.managed_thread_ids(),
            runtime_epoch=str(coordinates["runtime_epoch"]),
            projection_revision=int(coordinates["revision"]),
        )

    def execute_list_threads(
        self,
        prepared: WebThreadListPreparation,
    ) -> WebThreadListEffect:
        """Perform only generation-pinned inventory reads on the caller thread."""

        generation = prepared.connection_generation
        summaries = self._ports.list_threads(
            limit=prepared.query_limit,
            search_term=prepared.search or None,
            archived=prepared.archived,
            expected_connection_generation=generation,
        )
        loaded_ids = (
            set(
                self._ports.list_loaded_thread_ids(
                    expected_connection_generation=generation,
                )
            )
            if not prepared.archived
            else set()
        )
        managed_inventory = ManagedLoadedThreadInventorySnapshot()
        remote_loaded_instances: dict[str, set[str]] = {}
        if prepared.scope == "global" and not prepared.archived:
            managed_inventory = self._ports.managed_loaded_thread_inventory()
            for inventory in managed_inventory.instances:
                if not inventory.verified:
                    continue
                for thread_id in inventory.loaded_thread_ids:
                    remote_loaded_instances.setdefault(thread_id, set()).add(
                        inventory.instance_name
                    )
            # The ordinary persisted directory is intentionally bounded, so an
            # older thread may be absent even though another managed instance
            # still keeps it loaded. Materialize only those missing advisory
            # rows, with the same owner-level limit bounding extra reads. An
            # explicit search keeps the upstream search result authoritative.
            if not prepared.search:
                known_ids = {summary.thread_id for summary in summaries}
                missing_loaded_ids = sorted(
                    set(remote_loaded_instances).difference(known_ids)
                )[: self._thread_limit]
                for thread_id in missing_loaded_ids:
                    try:
                        summary = self._ports.read_thread(
                            thread_id,
                            False,
                            expected_connection_generation=generation,
                        ).summary
                    except Exception:
                        continue
                    if summary.thread_id != thread_id:
                        continue
                    summaries.append(summary)
        if prepared.scope == "current" and not prepared.archived:
            known_ids = {summary.thread_id for summary in summaries}
            current_ids = loaded_ids | set(prepared.managed_thread_ids)
            for thread_id in sorted(current_ids - known_ids):
                try:
                    summaries.append(
                        self._ports.read_thread(
                            thread_id,
                            False,
                            expected_connection_generation=generation,
                        ).summary
                    )
                except Exception:
                    continue
        runtime_leases = (
            tuple(self._ports.list_thread_runtime_leases())
            if not prepared.archived
            else ()
        )
        interaction_leases = tuple(self._interaction_leases.list())
        return WebThreadListEffect(
            summaries=tuple(summaries),
            loaded_thread_ids=frozenset(loaded_ids),
            managed_inventory=managed_inventory,
            runtime_leases=runtime_leases,
            interaction_leases=interaction_leases,
        )

    def settle_list_threads(
        self,
        prepared: WebThreadListPreparation,
        effect: WebThreadListEffect,
    ) -> WebThreadListProjection:
        self._runtime_context_guard()
        receipt = self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._claim_list_threads_projection(prepared),
        )
        observed_thread_ids = frozenset(
            summary.thread_id
            for summary in effect.summaries
            if self._runtime_interest.subscription_is_current(summary.thread_id)
        )
        interaction_leases = {
            lease.thread_id: lease for lease in effect.interaction_leases
        }
        return WebThreadListProjection(
            client_id=prepared.client_id,
            document=prepared.document,
            connection_generation=prepared.connection_generation,
            receipt=receipt,
            scope=prepared.scope,
            archived=prepared.archived,
            thread_limit=self._thread_limit,
            instance_name=self._instance_name,
            summaries=effect.summaries,
            loaded_thread_ids=effect.loaded_thread_ids,
            managed_inventory=effect.managed_inventory,
            runtime_leases=effect.runtime_leases,
            interaction_leases=effect.interaction_leases,
            observed_thread_ids=observed_thread_ids,
            pending_by_thread=tuple(
                self._pending_kind_by_thread(
                    prepared.client_id,
                    interaction_leases=interaction_leases,
                ).items()
            ),
            document_connected=bool(
                prepared.client_id
                and self._documents.is_connected(prepared.client_id)
            ),
        )

    def finalize_list_threads(
        self,
        projection: WebThreadListProjection,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Install only a list payload that remained exact during projection."""

        self._runtime_context_guard()
        if not isinstance(projection, WebThreadListProjection):
            raise TypeError("Web thread-list projection is required")
        self._ports.run_if_connection_generation(
            projection.connection_generation,
            lambda: self._require_projection_current(
                projection.document,
                projection.receipt,
                stale_code="stale_thread_list",
            ),
        )
        return payload

    def _claim_list_threads_projection(
        self,
        prepared: WebThreadListPreparation,
    ) -> WebThreadProjectionReceipt:
        """Linearize only O(1) local facts against connection replacement."""

        self._require_document_operation(prepared.document)
        coordinates = self._projection.coordinates()
        if (
            coordinates.get("runtime_epoch") != prepared.runtime_epoch
            or coordinates.get("revision") != prepared.projection_revision
        ):
            raise WebRuntimeError(
                "A newer runtime event replaced this thread directory read.",
                code="stale_thread_list",
                status=409,
            )
        return self._projection_receipt(coordinates)

    def project_list_threads(
        self,
        projection: WebThreadListProjection,
    ) -> dict[str, Any]:
        """Project one claimed inventory result on the external worker."""

        if not isinstance(projection, WebThreadListProjection):
            raise TypeError("Web thread-list projection is required")
        return project_thread_list(projection)

    def prepare_read_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        turn_limit: int | None = None,
        intent_generation: int = 0,
    ) -> WebThreadOpenPreparation:
        """Freeze exact document, target, cache, and backend coordinates."""

        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        accept_web_document_intent(
            self._documents,
            normalized_client_id,
            intent_generation,
        )
        normalized_thread_id = require_web_thread_id(thread_id)
        requested_turn_limit = self._requested_turn_limit(turn_limit)
        return WebThreadOpenPreparation(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            turn_limit=requested_turn_limit,
            document=self._documents.begin_operation(
                normalized_client_id,
                operation="thread_open",
                target_thread_id=normalized_thread_id,
            ),
            observation=self._read_model.capture_observation(
                normalized_thread_id
            ),
            connection_generation=self._ports.capture_connection_generation(),
            subscription_current=self._runtime_interest.subscription_is_current(
                normalized_thread_id
            ),
        )

    def execute_read_thread_observation(
        self,
        prepared: WebThreadOpenPreparation,
    ) -> WebThreadOpenObservation:
        """Read direct metadata and goal facts on the external caller thread."""

        generation = prepared.connection_generation
        metadata = self._ports.read_thread(
            prepared.thread_id,
            False,
            expected_connection_generation=generation,
        )
        require_web_direct_thread_snapshot(
            metadata,
            thread_id=prepared.thread_id,
            operation="作为独立会话打开",
        )
        goal: ThreadGoalSummary | None = None
        goal_known = False
        goal_requires_writer_admission = False
        runtime_lease_preflight: object | None = None
        try:
            goal = self._ports.get_thread_goal(
                prepared.thread_id,
                expected_connection_generation=generation,
            )
            goal_known = True
            goal_requires_writer_admission = (
                self._goal_resume_policy.requires_writer_admission(goal)
            )
        except CodexRpcError as exc:
            message = str(exc.error.get("message", "") or "").strip().lower()
            if message == "goals feature is disabled":
                goal_known = True
            elif not prepared.subscription_current:
                goal_requires_writer_admission = True
                logger.warning(
                    "Web passive open will not resume thread with unreadable "
                    "goal state: %s",
                    prepared.thread_id[:12],
                    exc_info=True,
                )
        except Exception:
            if not prepared.subscription_current:
                goal_requires_writer_admission = True
                logger.warning(
                    "Web passive open will not resume thread with unreadable goal "
                    "state: %s",
                    prepared.thread_id[:12],
                    exc_info=True,
                )
        if not prepared.subscription_current:
            runtime_lease_preflight = self._ports.prepare_runtime_lease_preflight(
                prepared.thread_id
            )
        return WebThreadOpenObservation(
            metadata=metadata,
            profile=self._workspace.load_profile_snapshot(prepared.client_id),
            goal=goal,
            goal_known=goal_known,
            goal_requires_writer_admission=goal_requires_writer_admission,
            runtime_lease_preflight=runtime_lease_preflight,
        )

    def prepare_read_thread_effect(
        self,
        initial: WebThreadOpenPreparation,
        observed: WebThreadOpenObservation,
    ) -> WebThreadOpenEffectPreparation:
        self._runtime_context_guard()
        self._ports.run_if_connection_generation(
            initial.connection_generation,
            lambda: self._remember_read_thread_effect_current(initial, observed),
        )
        return self._prepare_read_thread_effect_after_generation_check(
            initial,
            observed,
        )

    def _remember_read_thread_effect_current(
        self,
        initial: WebThreadOpenPreparation,
        observed: WebThreadOpenObservation,
    ) -> None:
        """Install the verified direct target only in its exact backend epoch."""

        self._require_document_operation(initial.document)
        self._require_observation(initial.observation)
        self._direct_targets.remember_verified_snapshot(observed.metadata)

    def _prepare_read_thread_effect_after_generation_check(
        self,
        initial: WebThreadOpenPreparation,
        observed: WebThreadOpenObservation,
    ) -> WebThreadOpenEffectPreparation:
        """Admit either one passive page read or one explicit resume effect."""

        subscription_current = self._runtime_interest.subscription_is_current(
            initial.thread_id
        )
        resume_admitted = False
        if not subscription_current:
            try:
                self._operations.require_no_unknown_mutation(initial.thread_id)
            except WebRuntimeError as exc:
                if exc.code != "mutation_reconciling":
                    raise
            else:
                resume_admitted = True
        if not resume_admitted:
            return WebThreadOpenEffectPreparation(
                initial=initial,
                observed=observed,
                kind="passive",
                live_subscription=subscription_current,
            )
        try:
            resume_claim = self._ports.claim_resume_thread_page(initial.thread_id)
        except Exception as exc:
            unavailable = classify_web_thread_unavailable_error(exc)
            if unavailable is None:
                raise
            code, status, message, _clear_target = unavailable
            raise WebRuntimeError(message, code=code, status=status) from exc
        return WebThreadOpenEffectPreparation(
            initial=initial,
            observed=observed,
            kind="resume",
            live_subscription=True,
            resume_claim=resume_claim,
        )

    def execute_read_thread_effect(
        self,
        prepared: WebThreadOpenEffectPreparation,
    ) -> WebThreadOpenEffect:
        """Perform the prepared page read or resume outside RuntimeLoop."""

        initial = prepared.initial
        generation = initial.connection_generation
        claim = prepared.resume_claim
        autonomous_admission: WebAutonomousTurnReceipt | None = None
        resume_as_passive = False
        if prepared.kind == "resume" and prepared.observed.goal_requires_writer_admission:
            if claim is None:
                raise RuntimeError("prepared Web resume is missing its exact claim")
            try:
                autonomous_admission = self._operations.acquire_autonomous_turn_external(
                    initial.client_id,
                    initial.thread_id,
                )
            except WebRuntimeError as exc:
                self._ports.abandon_resume_thread_page_claim(claim)
                if exc.code != "interaction_owned":
                    return WebThreadOpenEffect(error=exc)
                resume_as_passive = True
        if prepared.kind == "passive" or resume_as_passive:
            try:
                page = self._ports.list_thread_turns(
                    initial.thread_id,
                    limit=initial.turn_limit,
                    sort_direction="desc",
                    items_view="full",
                    expected_connection_generation=generation,
                )
                return self._prepare_read_thread_projection_effect(
                    prepared,
                    page=page,
                )
            except Exception as exc:
                return WebThreadOpenEffect(error=exc)
        if claim is None:
            raise RuntimeError("prepared Web resume is missing its exact claim")
        lease_receipt: ThreadResumeLeaseReceipt | None = None
        try:
            resume_settings = (
                self._next_turn_settings()
                if autonomous_admission is not None
                else None
            )
            lease_receipt = self._ports.acquire_claimed_resume_thread_page(
                claim,
                runtime_lease_preflight=prepared.observed.runtime_lease_preflight,
            )
            if autonomous_admission is not None:
                self._operations.require_current_autonomous_turn_external(
                    autonomous_admission,
                    client_id=initial.client_id,
                    root_thread_id=initial.thread_id,
                )
            resume = self._runtime_call(
                self._complete_read_thread_resume,
                prepared,
                lease_receipt,
                resume_settings,
                autonomous_admission,
            )
        except BaseException as exc:
            if lease_receipt is None:
                self._ports.abandon_resume_thread_page_claim(claim)
            else:
                self._ports.abandon_acquired_resume_thread_page(lease_receipt)
            if not isinstance(exc, Exception):
                if autonomous_admission is not None:
                    self._operations.release_autonomous_turn_external(
                        autonomous_admission
                    )
                raise
            return WebThreadOpenEffect(
                error=exc,
                autonomous_admission=autonomous_admission,
            )
        try:
            page = self._ports.execute_prepared_resume_thread_page(resume)
        except BaseException as exc:
            error, fatal_error = capture_external_failure(exc, "thread/resume")
            return WebThreadOpenEffect(
                error=error,
                resume=resume,
                autonomous_admission=autonomous_admission,
                fatal_error=fatal_error,
            )
        goal: ThreadGoalSummary | None = None
        goal_known = False
        goal_error: Exception | None = None
        fatal_error: BaseException | None = None
        if autonomous_admission is not None:
            try:
                goal = self._ports.get_thread_goal(
                    initial.thread_id,
                    expected_connection_generation=generation,
                )
                goal_known = True
            except BaseException as exc:
                goal_error, fatal_error = capture_external_failure(
                    exc, "post-resume goal read"
                )
        try:
            return self._prepare_read_thread_projection_effect(
                prepared,
                page=page,
                resume=resume,
                autonomous_admission=autonomous_admission,
                post_resume_goal=goal,
                post_resume_goal_known=goal_known,
                post_resume_goal_error=goal_error,
            )
        except BaseException as exc:
            projection_error, projection_fatal = capture_external_failure(exc, "thread projection")
            return WebThreadOpenEffect(
                page=page,
                error=projection_error,
                resume=resume,
                autonomous_admission=autonomous_admission,
                fatal_error=fatal_error or projection_fatal,
            )

    def _prepare_read_thread_projection_effect(
        self,
        prepared: WebThreadOpenEffectPreparation,
        *,
        page: ThreadTurnsPage | ThreadResumePage,
        resume: PreparedThreadResumePage | None = None,
        post_resume_goal: ThreadGoalSummary | None = None,
        post_resume_goal_known: bool = False,
        post_resume_goal_error: Exception | None = None,
        autonomous_admission: WebAutonomousTurnReceipt | None = None,
    ) -> WebThreadOpenEffect:
        """Prepare bounded cache and detached projection inputs off-loop."""

        if isinstance(page, ThreadResumePage):
            turns_page = page.initial_turns_page
            summary = page.snapshot.summary
        else:
            turns_page = page
            summary = prepared.observed.metadata.summary
        interaction_lease: InteractionLease | None = None
        interaction_lease_error: Exception | None = None
        try:
            interaction_lease = self._interaction_leases.load(
                prepared.initial.thread_id
            )
        except Exception as exc:
            interaction_lease_error = exc
        return WebThreadOpenEffect(
            page=page,
            post_resume_goal=post_resume_goal,
            post_resume_goal_known=post_resume_goal_known,
            post_resume_goal_error=post_resume_goal_error,
            prepared_turns=self._read_model.prepare_turn_replacement(
                prepared.initial.thread_id,
                turns_page.turns,
            ),
            interaction_lease=interaction_lease,
            interaction_lease_error=interaction_lease_error,
            normalized_cwd=self._workspace.working_dir_key(summary.cwd),
            resume=resume,
            profile=prepared.observed.profile,
            autonomous_admission=autonomous_admission,
        )

    def _complete_read_thread_resume(
        self,
        prepared: WebThreadOpenEffectPreparation,
        lease_receipt: ThreadResumeLeaseReceipt,
        settings: WebNextTurnSettings | None,
        autonomous_admission: WebAutonomousTurnReceipt | None,
    ) -> PreparedThreadResumePage:
        self._runtime_context_guard()
        initial = prepared.initial
        if autonomous_admission is not None:
            self._operations.require_exact_autonomous_turn_receipt(
                autonomous_admission,
                client_id=initial.client_id,
                root_thread_id=initial.thread_id,
            )
        self._ports.run_if_connection_generation(
            initial.connection_generation,
            lambda: (
                self._require_document_operation(initial.document),
                self._require_observation(initial.observation),
            ),
        )
        return self._ports.complete_claimed_resume_thread_page(
            lease_receipt,
            limit=initial.turn_limit,
            model=(settings.model or None) if settings else None,
            config_overrides=(
                {"model_reasoning_effort": settings.reasoning_effort}
                if settings and settings.reasoning_effort
                else None
            ),
            approval_policy=settings.approval_policy if settings else None,
            permissions_profile_id=(
                settings.permissions_profile_id if settings else None
            ),
            expected_connection_generation=initial.connection_generation,
        )

    def settle_read_thread_observation_failure(
        self,
        prepared: WebThreadOpenPreparation,
        exc: Exception,
    ) -> WebThreadOpenFailureSettlement:
        self._runtime_context_guard()
        return self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._settle_read_thread_observation_failure_current(
                prepared,
                exc,
            ),
        )

    def _settle_read_thread_observation_failure_current(
        self,
        prepared: WebThreadOpenPreparation,
        exc: Exception,
    ) -> WebThreadOpenFailureSettlement:
        """Apply only exact-current cleanup after an authoritative read failure."""

        read_current = self._open_read_is_current(prepared)
        if isinstance(exc, WebRuntimeError) and exc.code == "subagent_detail_only":
            return WebThreadOpenFailureSettlement(
                exc,
                clear_unusable_reason=(
                    "web_direct_target_selection_cleared" if read_current else ""
                ),
            )
        return self._read_failure_settlement(prepared, exc)

    def finish_read_thread_observation_failure(
        self,
        prepared: WebThreadOpenPreparation,
        exc: Exception,
    ) -> None:
        settlement = self._runtime_call(
            self.settle_read_thread_observation_failure, prepared, exc
        )
        self._finish_read_thread_failure(prepared, None, settlement)
        raise settlement.error

    def settle_read_thread(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
    ) -> WebThreadOpenSettlement | WebThreadOpenFailureSettlement:
        self._runtime_context_guard()
        return self._settle_read_thread_effect(prepared, effect)

    def _settle_read_thread_effect(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
    ) -> WebThreadOpenSettlement | WebThreadOpenFailureSettlement:
        """Settle one exact read, preserving any known resume effect."""

        initial = prepared.initial
        pending_resume: PendingThreadResume[ThreadResumePage] | None = None
        if effect.resume is not None:
            resume_succeeded = isinstance(effect.page, ThreadResumePage)
            try:
                pending_resume = self._ports.settle_prepared_resume_thread_page(
                    effect.resume,
                    response=effect.page if resume_succeeded else None,
                    error=None if resume_succeeded else effect.error,
                )
            except Exception as exc:
                return self._settle_resume_failure(prepared, effect, exc)
        elif prepared.kind == "resume" and effect.error is not None:
            return self._settle_resume_failure(
                prepared,
                effect,
                effect.error,
                pre_send=True,
            )
        elif effect.error is not None:
            return self._ports.run_if_connection_generation(
                initial.connection_generation,
                lambda: self._read_failure_settlement(initial, effect.error),
            )

        goal = prepared.observed.goal
        goal_known = prepared.observed.goal_known
        if pending_resume is not None:
            try:
                self._commit_known_resume_interest(prepared, effect, pending_resume)
            except ThreadResumeLocalCommitFailed as exc:
                return self._settle_resume_failure(prepared, effect, exc)
        if effect.error is not None:
            return self._read_failure_settlement(initial, effect.error)

        return self._settle_read_thread_current_generation(
            prepared,
            effect,
            goal=goal,
            goal_known=goal_known,
        )

    def _commit_known_resume_interest(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        pending_resume: PendingThreadResume[ThreadResumePage],
    ) -> None:
        """Consume known success while skipping facts for a replaced backend."""

        initial = prepared.initial
        generation_failure: AdapterOutboundRequestEpochLost | None = None

        def commit_if_current_generation() -> bool:
            nonlocal generation_failure
            try:
                self._ports.run_if_connection_generation(
                    initial.connection_generation,
                    lambda: self._runtime_interest.mark_confirmed(
                        initial.thread_id,
                        client_id=(
                            initial.client_id
                            if self._open_read_is_current(initial)
                            else ""
                        ),
                    ),
                )
            except AdapterOutboundRequestEpochLost as exc:
                generation_failure = exc
                return False
            return True

        committed = pending_resume.commit_local_state(
            commit_if_current_generation,
            failure_policy=(
                ThreadResumeLocalFailurePolicy.RETAIN
                if effect.autonomous_admission is not None
                else ThreadResumeLocalFailurePolicy.COMPENSATE
            ),
        )
        if committed:
            return
        if generation_failure is None:
            raise RuntimeError(
                "known resume skipped its Web interest without a generation failure"
            )
        raise generation_failure

    def _read_failure_settlement(
        self,
        initial: WebThreadOpenPreparation,
        exc: Exception,
    ) -> WebThreadOpenFailureSettlement:
        unavailable = classify_web_thread_unavailable_error(exc)
        if unavailable is None:
            return WebThreadOpenFailureSettlement(exc)
        code, status, message, clear_target = unavailable
        return WebThreadOpenFailureSettlement(
            WebRuntimeError(message, code=code, status=status),
            clear_unusable_reason=(
                f"web_{code}_selection_cleared"
                if clear_target and self._open_read_is_current(initial)
                else ""
            ),
        )

    def _settle_read_thread_current_generation(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        *,
        goal: ThreadGoalSummary | None,
        goal_known: bool,
    ) -> WebThreadOpenSettlement | WebThreadOpenFailureSettlement:
        initial = prepared.initial
        release_reason = ""
        if effect.autonomous_admission is not None:
            if effect.post_resume_goal_error is not None:
                return WebThreadOpenFailureSettlement(
                    WebRuntimeError(
                        "Codex resumed this goal-owning thread, but Focus could not "
                        "confirm whether it started work. Refresh before another action.",
                        code="goal_state_unconfirmed",
                        status=409,
                        details={"thread_id": initial.thread_id},
                    ),
                    publish_autonomous_reason="web_autonomous_turn_admitted",
                )
            if effect.post_resume_goal_known:
                goal = effect.post_resume_goal
                goal_known = True
                if not self._goal_resume_policy.requires_writer_admission(goal):
                    release_reason = "web_goal_resume_known_no_start"
        return WebThreadOpenSettlement(
            goal=goal if goal_known else None,
            release_autonomous_reason=release_reason,
        )

    def _finish_read_thread_failure(
        self,
        prepared: WebThreadOpenEffectPreparation | WebThreadOpenPreparation,
        effect: WebThreadOpenEffect | None,
        settlement: WebThreadOpenFailureSettlement,
    ) -> None:
        initial = prepared.initial if isinstance(prepared, WebThreadOpenEffectPreparation) else prepared
        admission = effect.autonomous_admission if effect is not None else None
        released = False
        if admission is not None and settlement.release_autonomous_reason:
            try:
                released = self._operations.release_autonomous_turn_external(admission)
            except Exception:
                logger.exception("Unable to release failed Web resume admission")
        if admission is not None:
            try:
                self._runtime_call(
                    self._operations.publish_autonomous_turn_change,
                    admission,
                    reason=(settlement.release_autonomous_reason or settlement.publish_autonomous_reason),
                    changed=released or bool(settlement.publish_autonomous_reason and admission.acquired),
                )
            except Exception:
                logger.exception("Unable to publish failed Web resume admission")
        def require_current() -> None:
            self._runtime_call(
                self._ports.run_if_connection_generation,
                initial.connection_generation,
                lambda: (self._require_document_operation(initial.document), self._require_observation(initial.observation)),
            )
        try:
            selection = (
                self._workspace.persist_current_thread_selection(
                    prepared.observed.profile, initial.thread_id, still_current=require_current
                )
                if settlement.select_thread and isinstance(prepared, WebThreadOpenEffectPreparation)
                else None
            )
            cleanup = None
            if settlement.clear_unusable_reason:
                require_current()
                cleanup = self._direct_targets.prepare_unusable_thread_cleanup(
                    initial.thread_id,
                    reason=settlement.clear_unusable_reason,
                    delete_attachment_scope=(settlement.clear_unusable_reason == "web_direct_target_selection_cleared"),
                )
            if selection is not None or cleanup is not None:
                def materialize() -> None:
                    self._require_document_operation(initial.document)
                    self._require_observation(initial.observation)
                    if selection is not None:
                        selected = self._workspace.materialize_persisted_selection(selection)
                        self._lifecycle.settle_runtime_cleanup_candidates(selected.runtime_cleanup_thread_ids)
                    if cleanup is not None:
                        self._direct_targets.settle_unusable_thread_cleanup(cleanup)
                self._runtime_call(
                    self._ports.run_if_connection_generation,
                    initial.connection_generation,
                    materialize,
                )
        except (AdapterOutboundRequestEpochLost, WebRuntimeError):
            pass
        except Exception:
            logger.exception("Web thread-open failure cleanup is incomplete")

    def finish_read_thread_effect(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
    ) -> dict[str, Any]:
        settlement = self._runtime_call(self.settle_read_thread, prepared, effect)
        if isinstance(settlement, WebThreadOpenFailureSettlement):
            self._finish_read_thread_failure(prepared, effect, settlement)
            if effect.fatal_error is not None:
                raise effect.fatal_error
            raise settlement.error
        admission = effect.autonomous_admission
        released = bool(
            admission
            and settlement.release_autonomous_reason
            and self._operations.release_autonomous_turn_external(admission)
        )
        commit = self._runtime_call(
            self._claim_read_thread_settlement,
            prepared,
            effect,
            settlement,
            released,
        )
        selection = self.persist_read_thread_selection(prepared, effect, commit)
        try:
            projection = self._runtime_call(
                self.commit_read_thread, prepared, effect, commit, selection
            )
        except (AdapterOutboundRequestEpochLost, WebRuntimeError) as exc:
            if not isinstance(exc, WebRuntimeError) or exc.code in {
                "stale_document_read",
                "stale_thread_read",
            }:
                compensated = self._workspace.compensate_stale_persisted_selection(
                    selection
                )
                if compensated is not None:
                    self._runtime_call(
                        self._workspace.publish_stale_selection_compensation,
                        compensated,
                    )
            raise
        payload = self.project_read_thread(projection)
        return self._runtime_call(
            self.finalize_read_thread_projection,
            projection,
            payload,
        )

    def _claim_read_thread_settlement(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        settlement: WebThreadOpenSettlement,
        released: bool,
    ) -> WebThreadOpenCommit:
        self._runtime_context_guard()
        initial = prepared.initial

        def claim() -> WebThreadOpenCommit:
            self._claim_open_observation(initial)
            return WebThreadOpenCommit(
                receipt=self._projection_receipt(self._projection.coordinates()),
                observation=self._read_model.capture_observation(initial.thread_id),
                goal=settlement.goal,
            )

        admission = effect.autonomous_admission
        if admission is not None:
            self._operations.require_exact_autonomous_turn_receipt(
                admission,
                client_id=initial.client_id,
                root_thread_id=initial.thread_id,
            )
            reason = settlement.release_autonomous_reason
            self._operations.publish_autonomous_turn_change(
                admission,
                reason=reason or "web_autonomous_turn_admitted",
                changed=released or (not reason and admission.acquired),
            )
        return self._ports.run_if_connection_generation(
            initial.connection_generation,
            claim,
        )

    def persist_read_thread_selection(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        commit: WebThreadOpenCommit,
    ) -> WebThreadSelection:
        if effect.profile is None:
            raise RuntimeError("Web thread read is missing its profile snapshot")
        return self._workspace.persist_current_thread_selection(
            effect.profile,
            prepared.initial.thread_id,
            still_current=lambda: self._runtime_call(
                self._require_open_commit_current, prepared, commit
            ),
        )

    def _require_open_commit_current(
        self,
        prepared: WebThreadOpenEffectPreparation,
        commit: WebThreadOpenCommit,
    ) -> None:
        self._runtime_context_guard()
        self._ports.run_if_connection_generation(
            prepared.initial.connection_generation,
            lambda: (
                self._require_projection_current(
                    prepared.initial.document,
                    commit.receipt,
                    stale_code="stale_thread_read",
                ),
                self._require_observation(commit.observation),
            ),
        )

    def commit_read_thread(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        commit: WebThreadOpenCommit,
        selection: WebThreadSelection,
    ) -> WebThreadOpenProjection:
        self._runtime_context_guard()
        page = effect.page
        if isinstance(page, ThreadResumePage):
            snapshot, turns_page = page.snapshot, page.initial_turns_page
        elif isinstance(page, ThreadTurnsPage):
            snapshot, turns_page = prepared.observed.metadata, page
        else:
            raise RuntimeError("Web thread read did not return a bounded turn page")
        return self._ports.run_if_connection_generation(
            prepared.initial.connection_generation,
            lambda: self._settle_open_snapshot(
                prepared.initial,
                snapshot=snapshot,
                page=turns_page,
                prepared_turns=effect.prepared_turns,
                interaction_lease=effect.interaction_lease,
                interaction_lease_error=effect.interaction_lease_error,
                normalized_cwd=effect.normalized_cwd,
                goal=commit.goal,
                live_subscription=prepared.live_subscription,
                commit=commit,
                selection=selection,
            ),
        )

    def _claim_open_observation(
        self,
        initial: WebThreadOpenPreparation,
    ) -> None:
        """CAS one exact read before any Web-local projection commit."""

        self._require_document_operation(initial.document)
        self._require_observation(initial.observation)
        if not self._read_model.claim_observation(initial.observation):
            raise WebRuntimeError(
                "This thread read was replaced by a newer observation.",
                code="stale_thread_read",
                status=409,
                details={"thread_id": initial.thread_id},
            )

    def _settle_resume_failure(
        self,
        prepared: WebThreadOpenEffectPreparation,
        effect: WebThreadOpenEffect,
        exc: Exception,
        *,
        pre_send: bool = False,
    ) -> WebThreadOpenFailureSettlement:
        initial = prepared.initial
        admission = effect.autonomous_admission
        if not pre_send and self._operations.is_resume_uncertain_error(exc):
            outcome_unknown = self._operations.is_resume_outcome_unknown(exc)
            try:
                select_thread = self._ports.run_if_connection_generation(
                    initial.connection_generation,
                    lambda: self._commit_uncertain_resume_current_generation(
                        prepared,
                        outcome_unknown=outcome_unknown,
                        autonomous=admission is not None,
                    ),
                )
            except AdapterOutboundRequestEpochLost:
                select_thread = False
            if outcome_unknown:
                message = (
                    "Codex may have resumed this thread, but Focus could not confirm "
                    "the result. Focus kept read interest, will not retry, and can "
                    "release only this attempt's fresh exact blank writer claim."
                )
            else:
                message = (
                    "Codex resumed this thread, but Focus could not finish its local "
                    "runtime-interest commit. Keep this browser session open while "
                    "Focus reconciles the runtime."
                )
            return WebThreadOpenFailureSettlement(
                WebRuntimeError(
                    message,
                    code="runtime_resume_unknown",
                    status=503,
                    details={"thread_id": initial.thread_id},
                ),
                select_thread=select_thread,
                release_autonomous_reason=(
                    "web_goal_resume_unknown" if outcome_unknown and admission else ""
                ),
                publish_autonomous_reason=(
                    "web_autonomous_turn_admitted"
                    if not outcome_unknown and admission
                    else ""
                ),
            )
        unavailable = classify_web_thread_unavailable_error(exc)
        if unavailable is None:
            error = exc
            clear_reason = ""
        else:
            code, status, message, clear_target = unavailable
            error = WebRuntimeError(message, code=code, status=status)
            clear_reason = (
                f"web_{code}_selection_cleared"
                if clear_target and self._open_read_is_current(initial)
                else ""
            )
        return WebThreadOpenFailureSettlement(
            error,
            release_autonomous_reason=(
                "web_goal_writer_resume_failed" if admission else ""
            ),
            clear_unusable_reason=clear_reason,
        )

    def _commit_uncertain_resume_current_generation(
        self,
        prepared: WebThreadOpenEffectPreparation,
        *,
        outcome_unknown: bool,
        autonomous: bool,
    ) -> bool:
        initial = prepared.initial
        read_current = self._open_read_is_current(initial)
        if autonomous and not outcome_unknown:
            self._operations.record_unknown_mutation(
                initial.thread_id,
                operation="resume",
                client_id=initial.client_id,
            )
        self._runtime_interest.mark_unknown(
            initial.thread_id,
            client_id=initial.client_id if read_current else "",
        )
        if read_current:
            self._projection.publish(
                "thread_invalidated",
                thread_id=initial.thread_id,
                reason="web_resume_unknown",
            )
        return read_current

    def _settle_open_snapshot(
        self,
        prepared: WebThreadOpenPreparation,
        *,
        snapshot: ThreadSnapshot,
        page: ThreadTurnsPage,
        prepared_turns: PreparedWebThreadTurns | None,
        interaction_lease: InteractionLease | None,
        interaction_lease_error: Exception | None,
        normalized_cwd: str,
        goal: ThreadGoalSummary | None,
        live_subscription: bool,
        commit: WebThreadOpenCommit,
        selection: WebThreadSelection,
    ) -> WebThreadOpenProjection:
        if prepared_turns is None:
            raise RuntimeError("Web thread read is missing its prepared turns")
        self._require_projection_current(
            prepared.document,
            commit.receipt,
            stale_code="stale_thread_read",
        )
        self._require_observation(commit.observation)
        selected = self._workspace.materialize_persisted_selection(selection)
        if interaction_lease_error is not None:
            raise interaction_lease_error
        snapshot = ThreadSnapshot(
            summary=snapshot.summary,
            turns=list(prepared_turns.projection_turns),
            effective_model=snapshot.effective_model,
            effective_reasoning_effort=snapshot.effective_reasoning_effort,
            effective_approval_policy=snapshot.effective_approval_policy,
            effective_permissions_profile_id=(
                snapshot.effective_permissions_profile_id
            ),
        )
        self._workspace.remember_prepared_thread_cwd(
            prepared.thread_id,
            normalized_cwd,
        )
        self._read_model.install_prepared_turns(prepared_turns)
        self._operations.reconcile_unknown_from_turns(
            prepared.thread_id,
            snapshot.turns,
        )
        if live_subscription:
            interest = self._runtime_interest.snapshot(prepared.thread_id)
            if interest is None or not interest.ever_confirmed:
                self._runtime_interest.mark_confirmed(
                    prepared.thread_id,
                    client_id=prepared.client_id,
                )
            else:
                self._runtime_interest.add_desired_client(
                    prepared.thread_id,
                    prepared.client_id,
                )
        token_usage, token_usage_available = self._read_model.token_usage(
            prepared.thread_id
        )
        active_turn_id = self._read_model.active_turn_id_from_turns(snapshot.turns)
        projection_coordinates = {
            "runtime_epoch": commit.receipt.runtime_epoch,
            "revision": commit.receipt.revision,
        }
        pending_requests = tuple(
            self._pending_for_thread(
                prepared.client_id,
                prepared.thread_id,
                interaction_lease=interaction_lease,
            )
        )
        return WebThreadOpenProjection(
            client_id=prepared.client_id,
            document=prepared.document,
            connection_generation=prepared.connection_generation,
            receipt=self._projection_receipt(projection_coordinates),
            snapshot=snapshot,
            owner_lease=interaction_lease,
            loaded_instance=(
                self._instance_name if live_subscription else ""
            ),
            observed_here=live_subscription,
            pending_requests=pending_requests,
            coordinates=projection_coordinates,
            older_turn_cursor=page.next_cursor or "",
            goal=goal,
            token_usage=token_usage,
            token_usage_available=token_usage_available,
            active_turn_context=self._active_turn_disclosure.compose(
                prepared.thread_id,
                active_turn_id,
                observed_lease=interaction_lease,
            ),
            document_connected=self._documents.is_connected(prepared.client_id),
            mutation_unknown=self._operations.unknown_mutation_projection(
                prepared.thread_id
            ),
            selection_scope=selected.selection_scope,
            final_observation=self._read_model.capture_observation(
                prepared.thread_id
            ),
        )

    def project_read_thread(
        self,
        projection: WebThreadOpenProjection,
    ) -> dict[str, Any]:
        """Materialize one claimed thread DTO on the external worker."""

        if not isinstance(projection, WebThreadOpenProjection):
            raise TypeError("Web thread-open projection is required")
        return project_open_thread(
            projection,
            attachment_url_for_path=lambda path: (
                self._workspace.materialize_attachment_url_for_path(
                    path,
                    cwd=projection.snapshot.summary.cwd,
                )
            ),
            attachment_url_for_id=self._workspace.attachment_url,
        )

    def finalize_read_thread_projection(
        self,
        projection: WebThreadOpenProjection,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Reject a DTO superseded after its authoritative local commit."""

        self._runtime_context_guard()
        if not isinstance(projection, WebThreadOpenProjection):
            raise TypeError("Web thread-open projection is required")
        return self._ports.run_if_connection_generation(
            projection.connection_generation,
            lambda: self._finalize_read_thread_projection_current_generation(
                projection,
                payload,
            ),
        )

    def _finalize_read_thread_projection_current_generation(
        self,
        projection: WebThreadOpenProjection,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._require_projection_current(
            projection.document,
            projection.receipt,
            stale_code="stale_thread_read",
        )
        self._require_observation(projection.final_observation)
        return payload

    def prepare_list_older_turns(
        self,
        client_id: str,
        thread_id: str,
        *,
        cursor: str,
        items_view: str = "full",
        turn_limit: int | None = None,
    ) -> WebThreadHistoryPreparation:
        """Validate history admission and freeze exact external-read coordinates."""

        self._runtime_context_guard()
        normalized_client_id = require_web_client_id(client_id)
        normalized_thread_id = require_web_thread_id(thread_id)
        self._selection.require_materialized_thread(
            normalized_client_id,
            normalized_thread_id,
        )
        normalized_cursor = str(cursor or "").strip()
        normalized_items_view = str(items_view or "").strip()
        requested_turn_limit = self._requested_turn_limit(turn_limit)
        if normalized_items_view not in {"summary", "full"}:
            raise WebRuntimeError(
                "History items view must be 'summary' or 'full'.",
                code="invalid_items_view",
            )
        if not normalized_cursor and normalized_items_view != "summary":
            raise WebRuntimeError(
                "History cursor is required.",
                code="invalid_cursor",
            )
        return WebThreadHistoryPreparation(
            client_id=normalized_client_id,
            thread_id=normalized_thread_id,
            cursor=normalized_cursor,
            items_view=normalized_items_view,
            turn_limit=requested_turn_limit,
            document=self._documents.begin_operation(
                normalized_client_id,
                operation="thread_history",
                target_thread_id=normalized_thread_id,
            ),
            observation=self._read_model.capture_observation(
                normalized_thread_id
            ),
            connection_generation=self._ports.capture_connection_generation(),
        )

    def execute_list_older_turns(
        self,
        prepared: WebThreadHistoryPreparation,
    ) -> WebThreadHistoryEffect:
        """Perform direct proof and one bounded page read outside RuntimeLoop."""

        profile = self._selection.load_profile_snapshot(prepared.client_id)
        if profile is None or profile.selected_thread_id != prepared.thread_id:
            raise WebRuntimeError(
                "Select this thread before loading its history.",
                code="thread_not_selected",
                status=409,
            )
        generation = prepared.connection_generation
        snapshot = self._ports.read_thread(
            prepared.thread_id,
            False,
            expected_connection_generation=generation,
        )
        require_web_direct_thread_snapshot(
            snapshot,
            thread_id=prepared.thread_id,
            operation="读取历史",
        )
        page = self._ports.list_thread_turns(
            prepared.thread_id,
            cursor=prepared.cursor or None,
            limit=prepared.turn_limit,
            sort_direction="desc",
            items_view=prepared.items_view,
            expected_connection_generation=generation,
        )
        return WebThreadHistoryEffect(profile=profile, page=page)

    def settle_list_older_turns(
        self,
        prepared: WebThreadHistoryPreparation,
        effect: WebThreadHistoryEffect,
    ) -> WebThreadHistoryProjection:
        self._runtime_context_guard()
        if not isinstance(effect, WebThreadHistoryEffect):
            raise TypeError("Web thread-history effect is required")
        receipt = self._ports.run_if_connection_generation(
            prepared.connection_generation,
            lambda: self._claim_history_projection(prepared, effect.profile),
        )
        return WebThreadHistoryProjection(
            client_id=prepared.client_id,
            document=prepared.document,
            connection_generation=prepared.connection_generation,
            observation=prepared.observation,
            items_view=prepared.items_view,
            page=effect.page,
            receipt=receipt,
        )

    def _claim_history_projection(
        self,
        prepared: WebThreadHistoryPreparation,
        profile: WebWriterProfile | None,
    ) -> WebThreadProjectionReceipt:
        """Capture the exact history coordinates without projecting its page."""

        self._require_document_operation(prepared.document)
        try:
            self._selection.require_history_ready_snapshot(
                profile,
                prepared.client_id,
                prepared.thread_id,
            )
        except WebSelectionNotReady as exc:
            raise WebRuntimeError(
                "Select this thread before loading its history.",
                code="thread_not_selected",
                status=409,
            ) from exc
        self._require_observation(prepared.observation)
        return self._projection_receipt(
            self._projection.coordinates(),
            cwd=self._read_model.cwd(prepared.thread_id),
        )

    def project_older_turns(
        self,
        projection: WebThreadHistoryProjection,
    ) -> dict[str, Any]:
        """Project a claimed history page on the external worker."""

        if not isinstance(projection, WebThreadHistoryProjection):
            raise TypeError("Web thread-history projection is required")
        return project_older_turns(
            projection,
            attachment_url_for_path=lambda path: (
                self._workspace.materialize_attachment_url_for_path(
                    path,
                    cwd=projection.receipt.cwd,
                )
            ),
            attachment_url_for_id=self._workspace.attachment_url,
        )

    def finalize_older_turns(
        self,
        projection: WebThreadHistoryProjection,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Return only a history payload whose exact read stayed current."""

        self._runtime_context_guard()
        if not isinstance(projection, WebThreadHistoryProjection):
            raise TypeError("Web thread-history projection is required")
        self._ports.run_if_connection_generation(
            projection.connection_generation,
            lambda: (
                self._require_projection_current(
                    projection.document,
                    projection.receipt,
                    stale_code="stale_thread_read",
                ),
                self._require_observation(projection.observation),
                self._selection.require_materialized_thread(
                    projection.client_id,
                    projection.document.target_thread_id,
                ),
            ),
        )
        return payload

    def select_thread(
        self,
        client_id: str,
        thread_id: str,
    ) -> tuple[WebWriterProfile, dict[str, Any]]:
        """Commit selection/materialization, then retry old runtime cleanup."""

        self._runtime_context_guard()
        selection = self._workspace.select_thread(client_id, thread_id)
        self._lifecycle.settle_runtime_cleanup_candidates(
            selection.runtime_cleanup_thread_ids
        )
        return selection.profile, selection.selection_scope

    def _require_document_operation(
        self,
        receipt: WebDocumentOperationReceipt | None,
    ) -> None:
        if receipt is None:
            return
        if not self._documents.operation_is_current(receipt):
            raise WebRuntimeError(
                "This browser document request was replaced before its external "
                "read completed.",
                code="stale_document_read",
                status=409,
                details={"thread_id": receipt.target_thread_id},
            )

    def _open_read_is_current(self, prepared: WebThreadOpenPreparation) -> bool:
        return self._documents.operation_is_current(
            prepared.document
        ) and self._read_model.observation_is_current(prepared.observation)

    def _require_observation(
        self,
        receipt: WebThreadReadObservationReceipt,
    ) -> None:
        if not self._read_model.observation_is_current(receipt):
            raise WebRuntimeError(
                "A newer thread notification or read replaced this external "
                "result.",
                code="stale_thread_read",
                status=409,
                details={"thread_id": receipt.thread_id},
            )

    def _require_projection_current(
        self,
        document: WebDocumentOperationReceipt | None,
        receipt: WebThreadProjectionReceipt,
        *,
        stale_code: str,
    ) -> None:
        """Recheck the exact document and revision after external projection."""

        self._require_document_operation(document)
        coordinates = self._projection.coordinates()
        if (
            coordinates.get("runtime_epoch") != receipt.runtime_epoch
            or coordinates.get("revision") != receipt.revision
        ):
            raise WebRuntimeError(
                "A newer Web runtime event replaced this projected read.",
                code=stale_code,
                status=409,
            )

    @staticmethod
    def _projection_receipt(
        coordinates: dict[str, Any],
        *,
        cwd: str = "",
    ) -> WebThreadProjectionReceipt:
        return WebThreadProjectionReceipt(
            runtime_epoch=str(coordinates.get("runtime_epoch", "") or ""),
            revision=int(coordinates.get("revision", 0) or 0),
            cwd=str(cwd or ""),
        )

    def resume_and_commit_web_interest(
        self,
        client_id: str,
        thread_id: str,
        *,
        turn_limit: int,
        failure_policy: ThreadResumeLocalFailurePolicy,
        model: str | None,
        config_overrides: dict[str, Any] | None,
        approval_policy: str | None,
        permissions_profile_id: str | None,
    ) -> ThreadResumePage:
        """Commit one paged resume to the Web runtime-interest owner."""

        self._runtime_context_guard()
        pending = self._ports.begin_resume_thread_page(
            thread_id,
            limit=turn_limit,
            model=model,
            config_overrides=config_overrides,
            approval_policy=approval_policy,
            permissions_profile_id=permissions_profile_id,
        )
        resumed = pending.response
        pending.commit_local_state(
            lambda: self._runtime_interest.mark_confirmed(
                thread_id,
                client_id=client_id,
            ),
            failure_policy=failure_policy,
        )
        return resumed

    @staticmethod
    def _requested_turn_limit(requested: int | None) -> int:
        if requested is None:
            return DEFAULT_TURN_WINDOW_LIMIT
        try:
            return require_turn_window_limit(requested)
        except ValueError as exc:
            raise WebRuntimeError(
                "Turn window must be exactly 5, 10, or 20.",
                code="invalid_turn_limit",
            ) from exc

    def _pending_for_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        interaction_lease: InteractionLease | None,
    ) -> list[dict[str, Any]]:
        """Expose only records for which this document is currently eligible."""

        normalized_client_id = str(client_id or "").strip()
        normalized_root_id = str(thread_id or "").strip()
        if not normalized_client_id or not normalized_root_id:
            return []
        return [
            pending.projection_dict()
            for pending in self._interaction_inbox.visible_snapshots(
                normalized_client_id,
                normalized_root_id,
            )
            if self._pending_candidate_eligible(
                normalized_client_id,
                pending,
                interaction_lease=interaction_lease,
            )
        ]

    def _pending_kind_by_thread(
        self,
        client_id: str,
        *,
        interaction_leases: dict[str, InteractionLease],
    ) -> dict[str, str]:
        """Expose pending attention only to the document allowed to act."""

        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return {}
        result: dict[str, str] = {}
        for pending in self._interaction_inbox.candidate_snapshots(
            normalized_client_id
        ):
            if not self._pending_candidate_eligible(
                normalized_client_id,
                pending,
                interaction_lease=interaction_leases.get(
                    pending.owner_thread_id
                ),
            ):
                continue
            root_thread_id = pending.owner_thread_id
            if pending.method in {USER_INPUT, MCP_ELICITATION}:
                result[root_thread_id] = "question"
            else:
                result.setdefault(root_thread_id, "approval")
        return result

    def _pending_candidate_eligible(
        self,
        client_id: str,
        pending: WebPendingInteractionSnapshot,
        *,
        interaction_lease: InteractionLease | None,
    ) -> bool:
        if pending.delivery_scope == "shared_interaction":
            return self._shared_interaction_eligible(
                client_id,
                pending.owner_thread_id,
                pending.thread_id,
                pending.turn_id,
            )
        if not self._documents.is_connected(client_id):
            return False
        return bool(
            interaction_lease is not None
            and interaction_lease.turn_id
            and interaction_lease.holder.kind == "web"
            and interaction_lease.holder.holder_id
            == f"web:{str(client_id or '').strip()}"
        )
