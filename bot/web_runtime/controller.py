"""RuntimeLoop-owned composition façade for the Focus Web surface."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from bot.active_turn_disclosure import ActiveTurnDisclosureComposer
from bot.adapters.base import (
    RuntimeModelSummary,
    ThreadGoalSummary,
    ThreadItemsPage,
    ThreadResumePage,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.approval_policy import USER_SELECTABLE_APPROVAL_POLICIES
from bot.permissions_profile import PERMISSION_PROFILE_CHOICES
from bot.thread_create_transaction import (
    CommittedThreadCreate,
)
from bot.thread_runtime_authority import (
    PendingThreadResume,
    PendingThreadUnsubscribe,
    PreparedThreadResumePage,
    PreparedThreadUnsubscribe,
    ThreadResumeClaimReceipt,
    ThreadResumeLeaseReceipt,
)
from bot.thread_runtime_coordination import ManagedLoadedThreadInventorySnapshot
from bot.interaction_auto_resolution import AutoResolutionTiming
from bot.thread_effective_settings import ThreadEffectiveSettingsRegistry
from bot.server_request_contract import (
    ServerRequestIdentity,
    ServerRequestLocalRemoval,
    ServerRequestRoutingMode,
)
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
)
from bot.stores.web_writer_profile_store import WebWriterProfileStore
from bot.stores.web_next_turn_settings_store import WebNextTurnSettingsStore
from bot.stores.web_attachment_store import (
    WebAttachmentDownload,
    WebAttachmentStore,
)
from bot.web_runtime.selection_coordinator import WebSelectionCoordinator
from bot.web_runtime.next_turn_settings_coordinator import (
    WebNextTurnSettingsCoordinator,
    WebNextTurnSettingsPorts,
)
from bot.web_runtime.projection import (
    FocusWebProjection,
    project_model,
)
from bot.web_runtime.interest import WebRuntimeInterestRegistry
from bot.web_runtime.thread_read_model import WebThreadReadModel
from bot.web_runtime.document_registry import WebDocumentRegistry
from bot.web_runtime.interaction_inbox import (
    WebInteractionBackendEpochRetirement,
    WebInteractionChange,
    WebInteractionInbox,
    WebInteractionInboxError,
)
from bot.web_runtime.interaction_response_controller import (
    WebInteractionResponseController,
    WebInteractionResponsePorts,
)
from bot.web_runtime.interaction_ingress_coordinator import (
    WebInteractionIngressCoordinator,
    WebInteractionIngressPorts,
)
from bot.web_runtime.operation_service import WebOperationPorts, WebOperationService
from bot.web_runtime.mutation_recovery import WebMutationBackendRetirementReceipt
from bot.web_runtime import direct_thread_target_coordinator as web_targets
from bot.web_runtime.goal_resume_policy import WebGoalResumePolicy, WebGoalResumePorts
from bot.web_runtime.contract import WebRuntimeError
from bot.web_runtime.lifecycle_coordinator import (
    WebRuntimeCleanupRelease,
    WebRuntimeLifecycleCoordinator,
    WebRuntimeLifecyclePorts,
)
from bot.web_runtime.event_coordinator import (
    WebRuntimeEventCoordinator,
    WebRuntimeEventPorts,
)
from bot.web_runtime.notification_projection import (
    WebNotificationProjectionReceipt,
)
from bot.web_runtime.thread_create_coordinator import (
    WebThreadCreateCoordinator,
    WebThreadCreatePorts,
)
from bot.web_runtime.thread_open_coordinator import (
    WebThreadHistoryPreparation,
    WebThreadListPreparation,
    WebThreadOpenPreparation,
    WebThreadOpenCoordinator,
    WebThreadOpenPorts,
)
from bot.web_runtime.thread_inspection import (
    WebThreadConversationSearchPreparation,
    WebThreadInspectionPorts,
    WebThreadInspectionPreparation,
    WebThreadInspectionService,
    WebThreadToolDetailPreparation,
)
from bot.web_runtime.thread_mutation_coordinator import (
    WebLifecycleTargetReader,
    WebLifecycleTargetReaderPorts,
    WebThreadMutationCoordinator,
    WebThreadMutationPorts,
)
from bot.web_runtime.turn_command_coordinator import (
    WebTurnCommandCoordinator,
    WebTurnCommandPorts,
)
from bot.web_runtime.prompt_submission import (
    WebPromptBackendEpochRetirementReceipt,
    WebPromptPreparation,
    WebPromptSubmissionCoordinator,
    WebPromptSubmissionPorts,
)
from bot.web_runtime.turn_window import MAX_TURN_WINDOW_LIMIT
from bot.web_runtime.writer_workspace_coordinator import (
    WebWriterWorkspaceCoordinator,
    WebWriterWorkspacePorts,
    require_connected_web_document,
    require_web_client_id,
    require_web_thread_id,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebBackendEpochRetirementReceipt:
    """Aggregate Web authority retired after one confirmed backend stop."""

    interaction_requests: WebInteractionBackendEpochRetirement
    prompt_results: WebPromptBackendEpochRetirementReceipt
    mutations: WebMutationBackendRetirementReceipt


@dataclass(slots=True)
class WebRuntimePorts:
    list_threads: Callable[..., list[ThreadSummary]]
    read_thread: Callable[..., ThreadSnapshot]
    list_models: Callable[[], list[RuntimeModelSummary]]
    list_loaded_thread_ids: Callable[[], list[str]]
    managed_loaded_thread_inventory: Callable[[], ManagedLoadedThreadInventorySnapshot]
    list_thread_runtime_leases: Callable[[], list[Any]]
    create_and_commit_thread: Callable[
        ...,
        CommittedThreadCreate[ThreadSnapshot, str],
    ]
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
    list_thread_items: Callable[..., ThreadItemsPage]
    search_thread_occurrences: Callable[..., ThreadSearchOccurrencesPage]
    start_turn: Callable[..., dict[str, Any]]
    steer_turn: Callable[..., dict[str, Any]]
    connection_generation: Callable[..., int]
    capture_connection_generation: Callable[[], int]
    run_if_connection_generation: Callable[[int, Callable[[], Any]], Any]
    compact_thread: Callable[[str], None]
    start_review: Callable[..., dict[str, Any]]
    rename_thread: Callable[[str, str], None]
    get_thread_goal: Callable[[str], ThreadGoalSummary | None]
    prepare_runtime_lease_preflight: Callable[[str], Any]
    set_thread_goal: Callable[..., ThreadGoalSummary]
    clear_thread_goal: Callable[..., bool]
    archive_thread: Callable[..., dict[str, Any]]
    unarchive_thread: Callable[..., dict[str, Any]]
    delete_thread: Callable[..., dict[str, Any]]
    interrupt_turn: Callable[..., None]
    prepare_unsubscribe_thread: Callable[..., PreparedThreadUnsubscribe]
    execute_prepared_unsubscribe_thread: Callable[[PreparedThreadUnsubscribe], None]
    settle_prepared_unsubscribe_thread: Callable[..., PendingThreadUnsubscribe]
    abandon_prepared_unsubscribe_thread: Callable[[PreparedThreadUnsubscribe], None]
    prepare_service_thread_runtime_lease_release: Callable[[str], object | None]
    release_prepared_service_thread_runtime_lease: Callable[[object], bool]
    schedule_runtime_cleanup: Callable[[str, bool], None]
    schedule_notification_projection: Callable[
        [WebNotificationProjectionReceipt],
        None,
    ]
    schedule_attachment_cleanup: Callable[[str], None]
    thread_subscribers: Callable[[str], tuple[tuple[str, str], ...]]
    # A server request can be fail-closed before child ancestry is proven.
    # Once it is rebound to this root, its unknown delivery outcome must block
    # release even if the request was never presented in the Web controller.
    has_external_pending_interaction_for_root: Callable[[str], bool]


class WebRuntimeController:
    def __init__(
        self,
        *,
        instance_name: str,
        web_display_name: str,
        interaction_lease_store: InteractionLeaseStore,
        profile_store: WebWriterProfileStore,
        next_turn_settings_store: WebNextTurnSettingsStore,
        attachment_store: WebAttachmentStore,
        remember_direct_thread_summary: Callable[[ThreadSummary], None],
        effective_settings: ThreadEffectiveSettingsRegistry,
        projection: FocusWebProjection,
        document_registry: WebDocumentRegistry,
        interaction_inbox: WebInteractionInbox,
        ports: WebRuntimePorts,
        runtime_call: Callable[..., Any],
        default_working_dir: str = "",
        thread_limit: int = 200,
    ) -> None:
        self._instance_name = str(instance_name or "default").strip() or "default"
        self._web_display_name = str(web_display_name or "").strip()
        if not self._web_display_name:
            raise ValueError("web_display_name must not be empty")
        self._projection = projection
        self._document_registry = document_registry
        self._interaction_leases = interaction_lease_store
        self._interaction_inbox = interaction_inbox
        self._ports = ports
        self._runtime_call = runtime_call
        self._active_turn_disclosure = ActiveTurnDisclosureComposer(
            interaction_leases=interaction_lease_store,
            effective_settings=effective_settings,
            thread_subscribers=ports.thread_subscribers,
        )
        self._runtime_interest = WebRuntimeInterestRegistry()
        self._selection = WebSelectionCoordinator(
            profile_store=profile_store,
            document_registry=document_registry,
            runtime_interest=self._runtime_interest,
        )
        self._thread_read_model = WebThreadReadModel(
            recent_turn_limit=MAX_TURN_WINDOW_LIMIT,
        )
        self._workspace = WebWriterWorkspaceCoordinator(
            profile_store=profile_store,
            attachment_store=attachment_store,
            documents=document_registry,
            selection=self._selection,
            read_model=self._thread_read_model,
            effective_settings=effective_settings,
            projection=projection,
            ports=WebWriterWorkspacePorts(
                list_models=ports.list_models,
                read_thread=ports.read_thread,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
            default_working_dir=default_working_dir,
        )
        self._lifecycle_target_reader = WebLifecycleTargetReader(
            ports=WebLifecycleTargetReaderPorts(read_thread=ports.read_thread),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._direct_target_verifier = web_targets.WebDirectThreadTargetVerifier(
            ports=web_targets.WebDirectThreadTargetVerifierPorts(
                read_thread=lambda thread_id,
                include_turns,
                **kwargs: ports.read_thread(
                    thread_id,
                    include_turns,
                    **kwargs,
                ),
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._operations = WebOperationService(
            interaction_lease_store=interaction_lease_store,
            document_registry=document_registry,
            projection=projection,
            runtime_context_guard=document_registry.assert_runtime_context,
            ports=WebOperationPorts(
                require_connected_document=self._require_connected_web_document,
                require_thread_id=self._require_thread_id,
                read_lifecycle_target_state=self._lifecycle_target_reader.read,
                turn_ids=self._thread_read_model.turn_ids,
            ),
        )
        self._next_turn_settings = WebNextTurnSettingsCoordinator(
            settings=next_turn_settings_store,
            documents=document_registry,
            projection=projection,
            ports=WebNextTurnSettingsPorts(list_models=ports.list_models),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._interaction_responses = WebInteractionResponseController(
            inbox=interaction_inbox,
            ports=WebInteractionResponsePorts(
                require_client_id=self._require_client_id,
                require_connected_writer=(self._operations.require_active_turn_writer),
                shared_interaction_eligible=self._shared_interaction_eligible,
                publish_changes=self._publish_interaction_changes,
            ),
        )
        self._interaction_ingress = WebInteractionIngressCoordinator(
            inbox=interaction_inbox,
            ports=WebInteractionIngressPorts(
                runtime_interest=self._runtime_interest,
                operations=self._operations,
                shared_interaction_has_live_recipient=(
                    self._shared_interaction_has_live_recipient
                ),
                publish_changes=self._publish_interaction_changes,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._goal_resume_policy = WebGoalResumePolicy(
            ports=WebGoalResumePorts(
                get_thread_goal=lambda thread_id: ports.get_thread_goal(thread_id)
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._prompt_submissions = WebPromptSubmissionCoordinator(
            documents=document_registry,
            workspace=self._workspace,
            operations=self._operations,
            goal_policy=self._goal_resume_policy,
            read_model=self._thread_read_model,
            projection=projection,
            next_turn_settings=self._next_turn_settings.load_external_snapshot,
            ports=WebPromptSubmissionPorts(
                read_thread=ports.read_thread,
                get_thread_goal=ports.get_thread_goal,
                start_turn=ports.start_turn,
                steer_turn=ports.steer_turn,
                capture_connection_generation=ports.capture_connection_generation,
                run_if_connection_generation=ports.run_if_connection_generation,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
            runtime_call=runtime_call,
        )
        self._lifecycle = WebRuntimeLifecycleCoordinator(
            runtime_context_guard=document_registry.assert_runtime_context,
            ports=WebRuntimeLifecyclePorts(
                operations=self._operations,
                documents=document_registry,
                runtime_interest=self._runtime_interest,
                interaction_inbox=interaction_inbox,
                read_model=self._thread_read_model,
                selection=self._selection,
                interaction_leases=interaction_lease_store,
                require_client_id=self._require_client_id,
                read_thread=ports.read_thread,
                list_loaded_thread_ids=ports.list_loaded_thread_ids,
                capture_connection_generation=ports.capture_connection_generation,
                run_if_connection_generation=ports.run_if_connection_generation,
                prepare_unsubscribe_thread=ports.prepare_unsubscribe_thread,
                execute_prepared_unsubscribe_thread=(
                    ports.execute_prepared_unsubscribe_thread
                ),
                settle_prepared_unsubscribe_thread=(
                    ports.settle_prepared_unsubscribe_thread
                ),
                abandon_prepared_unsubscribe_thread=(
                    ports.abandon_prepared_unsubscribe_thread
                ),
                prepare_service_thread_runtime_lease_release=(
                    ports.prepare_service_thread_runtime_lease_release
                ),
                release_prepared_service_thread_runtime_lease=(
                    ports.release_prepared_service_thread_runtime_lease
                ),
                schedule_runtime_cleanup=ports.schedule_runtime_cleanup,
                thread_subscribers=ports.thread_subscribers,
                has_external_pending_interaction_for_root=(
                    ports.has_external_pending_interaction_for_root
                ),
                shared_interaction_reprojection_roots=(
                    self._shared_interaction_reprojection_roots
                ),
                publish_interaction_changes=self._publish_interaction_changes,
                publish_projection=projection.publish,
            ),
        )
        self._thread_create = WebThreadCreateCoordinator(
            documents=document_registry,
            workspace=self._workspace,
            next_turn_settings=self._next_turn_settings.snapshot,
            operations=self._operations,
            lifecycle=self._lifecycle,
            remember_direct_thread_summary=remember_direct_thread_summary,
            runtime_interest=self._runtime_interest,
            projection=projection,
            ports=WebThreadCreatePorts(
                create_and_commit_thread=ports.create_and_commit_thread,
                start_turn=ports.start_turn,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._direct_targets = web_targets.WebDirectThreadTargetCoordinator(
            verifier=self._direct_target_verifier,
            ports=web_targets.WebDirectThreadTargetPorts(
                remember_direct_thread_summary=remember_direct_thread_summary,
                persist_clear_unusable_thread=(
                    self._workspace.persist_clear_unusable_thread
                ),
                materialize_cleared_unusable_thread=(
                    self._workspace.materialize_cleared_unusable_thread
                ),
                settle_runtime_cleanup_candidates=self._lifecycle.settle_runtime_cleanup_candidates,
                delete_thread_scope=self._workspace.delete_thread_scope_external,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._thread_open = WebThreadOpenCoordinator(
            instance_name=self._instance_name,
            documents=document_registry,
            workspace=self._workspace,
            operations=self._operations,
            lifecycle=self._lifecycle,
            direct_targets=self._direct_targets,
            goal_resume_policy=self._goal_resume_policy,
            read_model=self._thread_read_model,
            runtime_interest=self._runtime_interest,
            selection=self._selection,
            projection=projection,
            interaction_leases=interaction_lease_store,
            interaction_inbox=interaction_inbox,
            active_turn_disclosure=self._active_turn_disclosure,
            next_turn_settings=self._next_turn_settings.load_external_snapshot,
            shared_interaction_eligible=self._shared_interaction_eligible,
            ports=WebThreadOpenPorts(
                list_threads=ports.list_threads,
                read_thread=ports.read_thread,
                list_loaded_thread_ids=ports.list_loaded_thread_ids,
                managed_loaded_thread_inventory=(ports.managed_loaded_thread_inventory),
                list_thread_runtime_leases=ports.list_thread_runtime_leases,
                begin_resume_thread_page=ports.begin_resume_thread_page,
                claim_resume_thread_page=ports.claim_resume_thread_page,
                acquire_claimed_resume_thread_page=(
                    ports.acquire_claimed_resume_thread_page
                ),
                complete_claimed_resume_thread_page=(
                    ports.complete_claimed_resume_thread_page
                ),
                abandon_resume_thread_page_claim=(
                    ports.abandon_resume_thread_page_claim
                ),
                abandon_acquired_resume_thread_page=(
                    ports.abandon_acquired_resume_thread_page
                ),
                execute_prepared_resume_thread_page=(
                    ports.execute_prepared_resume_thread_page
                ),
                settle_prepared_resume_thread_page=(
                    ports.settle_prepared_resume_thread_page
                ),
                list_thread_turns=ports.list_thread_turns,
                get_thread_goal=ports.get_thread_goal,
                prepare_runtime_lease_preflight=(
                    ports.prepare_runtime_lease_preflight
                ),
                capture_connection_generation=ports.capture_connection_generation,
                run_if_connection_generation=ports.run_if_connection_generation,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
            runtime_call=runtime_call,
            thread_limit=thread_limit,
        )
        self._thread_inspection = WebThreadInspectionService(
            documents=document_registry,
            selection=self._selection,
            direct_targets=self._direct_targets,
            ports=WebThreadInspectionPorts(
                read_thread=ports.read_thread,
                list_thread_items=ports.list_thread_items,
                search_thread_occurrences=ports.search_thread_occurrences,
                coordinates=projection.coordinates,
                capture_connection_generation=ports.capture_connection_generation,
                run_if_connection_generation=ports.run_if_connection_generation,
                capture_observation=self._thread_read_model.capture_observation,
                observation_is_current=(
                    self._thread_read_model.observation_is_current
                ),
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._turn_commands = WebTurnCommandCoordinator(
            documents=document_registry,
            operations=self._operations,
            thread_open=self._thread_open,
            direct_targets=self._direct_targets,
            goal_policy=self._goal_resume_policy,
            read_model=self._thread_read_model,
            runtime_interest=self._runtime_interest,
            projection=projection,
            ports=WebTurnCommandPorts(
                compact_thread=ports.compact_thread,
                start_review=ports.start_review,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )
        self._events = WebRuntimeEventCoordinator(
            runtime_context_guard=document_registry.assert_runtime_context,
            ports=WebRuntimeEventPorts(
                runtime_interest=self._runtime_interest,
                interaction_inbox=interaction_inbox,
                read_model=self._thread_read_model,
                operations=self._operations,
                prompt_results=self._prompt_submissions,
                lifecycle=self._lifecycle,
                attachments=attachment_store,
                clear_thread_selection_facts=self._direct_targets.clear_unusable_thread,
                attachment_url_for_path=(
                    self._workspace.materialize_attachment_url_for_path
                ),
                attachment_url_for_id=self._workspace.attachment_url,
                publish_interaction_changes=self._publish_interaction_changes,
                publish_projection=projection.publish,
                projection_coordinates=projection.coordinates,
                schedule_notification_projection=(
                    ports.schedule_notification_projection
                ),
                schedule_attachment_cleanup=ports.schedule_attachment_cleanup,
            ),
        )
        self._thread_mutations = WebThreadMutationCoordinator(
            documents=document_registry,
            direct_targets=self._direct_targets,
            operations=self._operations,
            lifecycle_targets=self._lifecycle_target_reader,
            goal_policy=self._goal_resume_policy,
            workspace=self._workspace,
            events=self._events,
            projection=projection,
            ports=WebThreadMutationPorts(
                read_thread=ports.read_thread,
                rename_thread=ports.rename_thread,
                set_thread_goal=ports.set_thread_goal,
                clear_thread_goal=ports.clear_thread_goal,
                archive_thread=ports.archive_thread,
                unarchive_thread=ports.unarchive_thread,
                delete_thread=ports.delete_thread,
                interrupt_turn=ports.interrupt_turn,
            ),
            runtime_context_guard=document_registry.assert_runtime_context,
        )

    def meta(self, client_id: str) -> dict[str, Any]:
        normalized_client_id = self._require_client_id(client_id)
        runtime_models, profile = self._workspace.read_catalog_profile(
            normalized_client_id
        )
        models = [project_model(model) for model in runtime_models]
        return {
            **self._projection.coordinates(),
            "product": "Focus",
            "instance": self._instance_name,
            "web_display_name": self._web_display_name,
            "default_working_dir": self._workspace.default_working_dir,
            "models": models,
            "writer_profile": self._workspace.profile_payload(profile),
            "next_turn_settings": self._next_turn_settings.payload(),
            "approval_policies": sorted(USER_SELECTABLE_APPROVAL_POLICIES),
            "permissions_profiles": [
                {"id": choice["profile_id"], "label": choice["label"]}
                for choice in PERMISSION_PROFILE_CHOICES.values()
            ],
            "capabilities": {
                "prompt": True,
                "new_thread": True,
                "interrupt": True,
                "approvals": True,
                "questions": True,
                "markdown": True,
                "katex": True,
                "mermaid": True,
                "file_preview": False,
                "terminal": False,
                "attachments": True,
                "prompt_queue": False,
                "durable_event_cursor": False,
                "bounded_history": True,
                "history_search": True,
                "tool_detail": True,
                "steer": True,
            },
            "unknown_lifecycle_mutations": self._thread_mutations.unknown_lifecycle_mutations_for_client(
                normalized_client_id
            ),
        }

    def update_profile(
        self,
        client_id: str,
        changes: dict[str, Any],
        *,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        outcome = self._workspace.update_profile(
            client_id,
            changes,
            intent_generation=intent_generation,
        )
        self._settle_runtime_cleanup_candidates(outcome.runtime_cleanup_thread_ids)
        return outcome.payload

    def next_turn_settings(self) -> dict[str, Any]:
        return {
            **self._projection.coordinates(),
            "next_turn_settings": self._next_turn_settings.payload(),
        }

    def update_next_turn_settings(
        self,
        client_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        return self._next_turn_settings.update(client_id, changes=changes)

    def stage_attachment(
        self,
        client_id: str,
        *,
        thread_id: str = "",
        cwd: str = "",
        scope_generation: int | None = None,
        display_name: str,
        media_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        return self._workspace.stage_attachment(
            client_id,
            thread_id=thread_id,
            cwd=cwd,
            scope_generation=scope_generation,
            display_name=display_name,
            media_type=media_type,
            content=content,
        )

    def attachment_download(self, attachment_id: str) -> WebAttachmentDownload:
        return self._workspace.attachment_download(attachment_id)

    def start_thread(
        self,
        client_id: str,
        *,
        text: str,
        cwd: str = "",
        attachment_ids: list[str] | None = None,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        return self._thread_create.start_thread(
            client_id,
            text=text,
            cwd=cwd,
            attachment_ids=attachment_ids,
            intent_generation=intent_generation,
        )

    def list_threads(
        self,
        *,
        client_id: str = "",
        search: str = "",
        scope: str = "global",
        archived: bool = False,
        all_for_search: bool = False,
    ) -> dict[str, Any]:
        prepared = self.prepare_list_threads(
            client_id=client_id,
            search=search,
            scope=scope,
            archived=archived,
            all_for_search=all_for_search,
        )
        return self.run_prepared_thread_read(prepared)

    def prepare_list_threads(
        self,
        **kwargs: Any,
    ) -> WebThreadListPreparation:
        return self._thread_open.prepare_list_threads(**kwargs)

    def read_thread(
        self,
        client_id: str,
        thread_id: str,
        *,
        turn_limit: int | None = None,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        prepared = self.prepare_read_thread(
            client_id,
            thread_id,
            turn_limit=turn_limit,
            intent_generation=intent_generation,
        )
        return self.run_prepared_thread_read(prepared)

    def prepare_read_thread(
        self,
        client_id: str,
        thread_id: str,
        **kwargs: Any,
    ) -> WebThreadOpenPreparation:
        return self._thread_open.prepare_read_thread(
            client_id,
            thread_id,
            **kwargs,
        )

    def rename_thread(
        self, client_id: str, thread_id: str, *, name: str
    ) -> dict[str, Any]:
        return self._thread_mutations.rename_thread(
            client_id,
            thread_id,
            name=name,
        )

    def compact_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        return self._turn_commands.compact_thread(client_id, thread_id)

    def start_review(
        self,
        client_id: str,
        thread_id: str,
        *,
        target: dict[str, Any],
    ) -> dict[str, Any]:
        return self._turn_commands.start_review(
            client_id,
            thread_id,
            target=target,
        )

    def goal(self, client_id: str, thread_id: str) -> dict[str, Any]:
        return self._thread_mutations.goal(client_id, thread_id)

    def set_goal(
        self,
        client_id: str,
        thread_id: str,
        *,
        objective: str | None = None,
        status: str | None = None,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        return self._thread_mutations.set_goal(
            client_id,
            thread_id,
            objective=objective,
            status=status,
            intent_generation=intent_generation,
        )

    def clear_goal(
        self,
        client_id: str,
        thread_id: str,
        *,
        intent_generation: int = 0,
    ) -> dict[str, Any]:
        return self._thread_mutations.clear_goal(
            client_id,
            thread_id,
            intent_generation=intent_generation,
        )

    def archive_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        return self._thread_mutations.archive_thread(client_id, thread_id)

    def unarchive_thread(self, client_id: str, thread_id: str) -> dict[str, Any]:
        return self._thread_mutations.unarchive_thread(client_id, thread_id)

    def delete_thread(
        self, client_id: str, thread_id: str, *, confirmation: str
    ) -> dict[str, Any]:
        return self._thread_mutations.delete_thread(
            client_id,
            thread_id,
            confirmation=confirmation,
        )

    def list_older_turns(
        self,
        client_id: str,
        thread_id: str,
        *,
        cursor: str,
        items_view: str = "full",
        turn_limit: int | None = None,
    ) -> dict[str, Any]:
        prepared = self.prepare_list_older_turns(
            client_id,
            thread_id,
            cursor=cursor,
            items_view=items_view,
            turn_limit=turn_limit,
        )
        return self.run_prepared_thread_read(prepared)

    def prepare_list_older_turns(
        self,
        client_id: str,
        thread_id: str,
        **kwargs: Any,
    ) -> WebThreadHistoryPreparation:
        return self._thread_open.prepare_list_older_turns(
            client_id,
            thread_id,
            **kwargs,
        )

    def run_prepared_thread_read(
        self,
        prepared: (
            WebThreadListPreparation
            | WebThreadOpenPreparation
            | WebThreadHistoryPreparation
            | WebThreadInspectionPreparation
        ),
    ) -> dict[str, Any]:
        """Run external RPC stages, returning to RuntimeLoop only to settle."""

        if isinstance(prepared, WebThreadListPreparation):
            effect = self._thread_open.execute_list_threads(prepared)
            projection = self._runtime_call(
                self._thread_open.settle_list_threads,
                prepared,
                effect,
            )
            payload = self._thread_open.project_list_threads(projection)
            return self._runtime_call(
                self._thread_open.finalize_list_threads,
                projection,
                payload,
            )
        if isinstance(prepared, WebThreadHistoryPreparation):
            effect = self._thread_open.execute_list_older_turns(prepared)
            projection = self._runtime_call(
                self._thread_open.settle_list_older_turns,
                prepared,
                effect,
            )
            payload = self._thread_open.project_older_turns(projection)
            return self._runtime_call(
                self._thread_open.finalize_older_turns,
                projection,
                payload,
            )
        if isinstance(
            prepared,
            (
                WebThreadToolDetailPreparation,
                WebThreadConversationSearchPreparation,
            ),
        ):
            effect = self._thread_inspection.execute_inspection(prepared)
            return self._runtime_call(
                self._thread_inspection.settle_inspection,
                prepared,
                effect,
            )
        if not isinstance(prepared, WebThreadOpenPreparation):
            raise TypeError("prepared Web thread-read transaction is required")
        try:
            observed = self._thread_open.execute_read_thread_observation(
                prepared
            )
        except Exception as exc:
            self._thread_open.finish_read_thread_observation_failure(
                prepared, exc
            )
            raise AssertionError("thread observation failure did not raise")
        effect_preparation = self._runtime_call(
            self._thread_open.prepare_read_thread_effect,
            prepared,
            observed,
        )
        effect = self._thread_open.execute_read_thread_effect(
            effect_preparation
        )
        result = self._thread_open.finish_read_thread_effect(
            effect_preparation,
            effect,
        )
        self._runtime_call(
            self._reconcile_prompt_results_from_cached_turns,
            prepared.thread_id,
        )
        return result

    def _reconcile_prompt_results_from_cached_turns(self, thread_id: str) -> None:
        self._prompt_submissions.reconcile_prompt_results_from_turns(
            thread_id, self._thread_read_model.turns(thread_id)
        )

    def run_runtime_cleanup_transaction(
        self,
        thread_id: str,
        known_non_active: bool = False,
    ) -> None:
        """Run one lifecycle-fenced cleanup with only exact settles in RuntimeLoop."""

        try:
            self._run_runtime_cleanup_transaction(
                thread_id,
                known_non_active=known_non_active,
            )
        finally:
            try:
                self._runtime_call(
                    self._lifecycle.finish_runtime_cleanup,
                    thread_id,
                )
            except Exception:
                logger.debug(
                    "Web runtime cleanup flight could not finish: thread=%s",
                    str(thread_id or "").strip()[:12],
                    exc_info=True,
                )

    def _run_runtime_cleanup_transaction(
        self,
        thread_id: str,
        *,
        known_non_active: bool,
    ) -> None:
        prepared = self._runtime_call(
            self._lifecycle.prepare_runtime_cleanup,
            thread_id,
            known_non_active=known_non_active,
        )
        if prepared is None:
            return
        try:
            probe = self._lifecycle.execute_runtime_cleanup_probe(prepared)
        except Exception:
            logger.debug(
                "Web runtime cleanup probe failed; retaining runtime: thread=%s",
                prepared.thread_id[:12],
                exc_info=True,
            )
            return
        try:
            claim = self._runtime_call(
                self._lifecycle.settle_runtime_cleanup_probe,
                prepared,
                probe,
            )
        except Exception:
            logger.debug(
                "Web runtime cleanup probe became stale: thread=%s",
                prepared.thread_id[:12],
                exc_info=True,
            )
            return
        if claim is None:
            return

        effect_error: Exception | None = None
        fatal_error: BaseException | None = None
        if claim.execute_unsubscribe:
            try:
                send_allowed = self._runtime_call(
                    self._lifecycle.confirm_runtime_cleanup_unsubscribe_send,
                    claim,
                )
            except Exception:
                self._lifecycle.abandon_runtime_cleanup_claim(claim)
                logger.debug(
                    "Web runtime unsubscribe send fence became stale: thread=%s",
                    prepared.thread_id[:12],
                    exc_info=True,
                )
                return
            if not send_allowed:
                self._lifecycle.abandon_runtime_cleanup_claim(claim)
                return
            try:
                self._lifecycle.execute_runtime_cleanup_unsubscribe(claim)
            except BaseException as exc:
                if isinstance(exc, Exception):
                    effect_error = exc
                else:
                    fatal_error = exc
                    effect_error = RuntimeError(
                        f"thread/unsubscribe aborted by {type(exc).__name__}"
                    )
        try:
            release = self._runtime_call(
                self._lifecycle.settle_runtime_cleanup_unsubscribe,
                claim,
                error=effect_error,
            )
        except Exception:
            self._lifecycle.abandon_runtime_cleanup_claim(claim)
            logger.debug(
                "Web runtime unsubscribe could not settle; retaining runtime: thread=%s",
                prepared.thread_id[:12],
                exc_info=True,
            )
            if fatal_error is not None:
                raise fatal_error
            return
        if release is None:
            if fatal_error is not None:
                raise fatal_error
            return
        try:
            release_allowed = self._runtime_call(
                self._lifecycle.confirm_runtime_cleanup_lease_release,
                release,
            )
        except Exception:
            self._lifecycle.abandon_runtime_cleanup_release(release)
            logger.debug(
                "Web runtime lease-release fence became stale: thread=%s",
                prepared.thread_id[:12],
                exc_info=True,
            )
            return
        if not release_allowed:
            self._finalize_runtime_cleanup_release(release)
            return
        release_fatal: BaseException | None = None
        try:
            lease_released = self._lifecycle.release_runtime_cleanup_lease(
                release
            )
        except BaseException as exc:
            lease_released = False
            if isinstance(exc, Exception):
                logger.exception(
                    "Failed to release inactive Web runtime lease: thread=%s",
                    prepared.thread_id[:12],
                )
            else:
                release_fatal = exc
        if not lease_released:
            try:
                self._runtime_call(
                    self._lifecycle.settle_runtime_cleanup_lease_release_failure,
                    release,
                )
            except Exception:
                self._lifecycle.abandon_runtime_cleanup_release(release)
                logger.debug(
                    "Web runtime lease-release failure could not settle: thread=%s",
                    prepared.thread_id[:12],
                    exc_info=True,
                )
            if release_fatal is not None:
                raise release_fatal
            return
        self._finalize_runtime_cleanup_release(release)

    def _finalize_runtime_cleanup_release(
        self,
        release: WebRuntimeCleanupRelease,
    ) -> None:
        try:
            self._runtime_call(
                self._lifecycle.finalize_runtime_cleanup_release,
                release,
            )
        except Exception:
            self._lifecycle.abandon_runtime_cleanup_release(release)
            logger.debug(
                "Web runtime cleanup final settlement became stale: thread=%s",
                release.claim.preparation.thread_id[:12],
                exc_info=True,
            )

    def prepare_tool_detail(
        self,
        client_id: str,
        thread_id: str,
        turn_id: str,
        item_id: str,
        *,
        view: str,
        change_index: int | None = None,
        cursor: str | None = None,
    ) -> WebThreadToolDetailPreparation:
        return self._thread_inspection.prepare_tool_detail(
            client_id,
            thread_id,
            turn_id,
            item_id,
            view=view,
            change_index=change_index,
            cursor=cursor,
        )

    def prepare_conversation_search(
        self,
        client_id: str,
        thread_id: str,
        *,
        query: str,
        cursor: str | None = None,
    ) -> WebThreadConversationSearchPreparation:
        return self._thread_inspection.prepare_conversation_search(
            client_id,
            thread_id,
            query=query,
            cursor=cursor,
        )

    def prepare_prompt(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
        text: str,
        attachment_ids: list[str] | None = None,
        source_scope_generation: int,
        source_attachment_scope: str,
        source_composer_scope_id: str,
    ) -> WebPromptPreparation:
        return self._prompt_submissions.prepare_prompt(
            client_id,
            thread_id,
            text=text,
            attachment_ids=attachment_ids,
            mutation_id=mutation_id,
            source_scope_generation=source_scope_generation,
            source_attachment_scope=source_attachment_scope,
            source_composer_scope_id=source_composer_scope_id,
        )

    def run_prepared_prompt(
        self,
        prepared: WebPromptPreparation,
    ) -> dict[str, str]:
        return self._prompt_submissions.run_prepared_prompt(prepared)

    def abandon_prompt(self, prepared: WebPromptPreparation) -> bool:
        return self._prompt_submissions.abandon_prompt(prepared)

    def prompt_result(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
    ) -> dict[str, str]:
        return self._prompt_submissions.prompt_result(
            client_id,
            thread_id,
            mutation_id=mutation_id,
        )

    def interrupt(
        self, client_id: str, thread_id: str, *, turn_id: str
    ) -> dict[str, Any]:
        return self._thread_mutations.interrupt(
            client_id,
            thread_id,
            turn_id=turn_id,
        )

    def resolve_unknown_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        action: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        return self._thread_mutations.resolve_unknown_mutation(
            client_id,
            thread_id,
            action=action,
            mutation_id=mutation_id,
        )

    def verify_unknown_lifecycle_mutation(
        self,
        client_id: str,
        thread_id: str,
        *,
        mutation_id: str,
    ) -> dict[str, Any]:
        return self._thread_mutations.verify_unknown_lifecycle_mutation(
            client_id,
            thread_id,
            mutation_id=mutation_id,
        )

    def respond_request(
        self,
        client_id: str,
        request_key: str,
        *,
        connection_generation: int,
        response_capability: str,
        action: str,
        answers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._interaction_responses.respond(
                client_id,
                request_key,
                connection_generation=connection_generation,
                response_capability=response_capability,
                action=action,
                answers=answers,
            )
        except WebInteractionInboxError as exc:
            self._raise_interaction_inbox_error(exc)

    def _publish_interaction_changes(
        self,
        changes: tuple[WebInteractionChange, ...],
    ) -> None:
        for change in changes:
            if not change.root_thread_id:
                continue
            self._projection.publish(
                "pending_request_changed",
                thread_id=change.root_thread_id,
                reason=change.reason,
            )

    def _shared_interaction_eligible(
        self,
        client_id: str,
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool:
        """Compose live document, exact attach, root, and callback authority."""

        normalized_client_id = str(client_id or "").strip()
        root_id = str(root_thread_id or "").strip()
        request_thread_id = str(request_thread_id or "").strip()
        turn_id = str(turn_id or "").strip()
        if not normalized_client_id or not root_id or not turn_id:
            return False
        document = self._document_registry.snapshot(normalized_client_id)
        if document is None or not document.connected:
            return False
        if request_thread_id != root_id:
            return False
        interest = self._runtime_interest.snapshot(root_id)
        if (
            interest is None
            or not interest.ever_confirmed
            or not self._runtime_interest.subscription_is_current(root_id)
        ):
            return False
        attached = document.materialized_thread_id == root_id or bool(
            normalized_client_id in interest.desired_client_ids
        )
        if not attached:
            return False
        # The caller supplies the current-epoch canonical callback identity.
        # A writer lease governs lifecycle mutation, not interaction response
        # authority: autonomous goal turns may legitimately have no Focus
        # writer while the upstream callback remains pending.
        return True

    def _shared_interaction_has_live_recipient(
        self,
        root_thread_id: str,
        request_thread_id: str,
        turn_id: str,
    ) -> bool:
        """Prove that at least one connected document can receive the request."""

        return any(
            self._shared_interaction_eligible(
                client_id,
                root_thread_id,
                request_thread_id,
                turn_id,
            )
            for client_id in self._document_registry.client_ids()
        )

    def _shared_interaction_reprojection_roots(
        self,
        client_id: str,
    ) -> tuple[str, ...]:
        """Return exact current roots whose shared interaction became visible."""

        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return ()
        roots = {
            pending.owner_thread_id
            for pending in self._interaction_inbox.candidate_snapshots(
                normalized_client_id
            )
            if pending.delivery_scope == "shared_interaction"
            and self._shared_interaction_eligible(
                normalized_client_id,
                pending.owner_thread_id,
                pending.thread_id,
                pending.turn_id,
            )
        }
        return tuple(sorted(root_id for root_id in roots if root_id))

    def _raise_interaction_inbox_error(
        self,
        error: WebInteractionInboxError,
    ) -> None:
        self._publish_interaction_changes(error.changes)
        raise WebRuntimeError(
            str(error),
            code=error.code,
            status=error.status,
        ) from error

    def has_pending_request(self, request_key: str) -> bool:
        return self._interaction_inbox.contains(request_key)

    def retire_backend_epoch_after_stop(self) -> WebBackendEpochRetirementReceipt:
        return WebBackendEpochRetirementReceipt(
            interaction_requests=(
                self._interaction_responses.retire_backend_epoch_after_stop()
            ),
            prompt_results=(
                self._prompt_submissions.retire_backend_epoch_after_stop()
            ),
            mutations=self._operations.retire_backend_epoch_after_stop(),
        )

    def auto_resolve_request(
        self, request_key: str, backend_epoch: int, generation: int
    ) -> bool:
        return self._interaction_responses.auto_resolve_request(
            request_key,
            backend_epoch,
            generation,
        )

    def handle_adapter_request(
        self,
        identity: ServerRequestIdentity,
        *,
        auto_resolution_timing: AutoResolutionTiming | None = None,
        routing_mode: ServerRequestRoutingMode = "single_surface",
    ) -> bool:
        return self._interaction_ingress.handle_adapter_request(
            identity,
            auto_resolution_timing=auto_resolution_timing,
            routing_mode=routing_mode,
        )

    def remove_resolved_server_request(
        self,
        identity: ServerRequestIdentity,
    ) -> ServerRequestLocalRemoval:
        return self._events.remove_resolved_server_request(identity)

    def revoke_server_request_response_authority(
        self,
        identity: ServerRequestIdentity,
    ) -> None:
        """Hide one Web capability without asserting upstream settlement."""

        mutation = self._interaction_inbox.revoke_exact_response_authority(identity)
        self._publish_interaction_changes(mutation.changes)

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        self._events.handle_notification(method, params)

    def run_notification_projection_transaction(
        self,
        receipt: WebNotificationProjectionReceipt,
    ) -> None:
        """Project one notification receipt outside RuntimeLoop."""

        detail: dict[str, Any] | None = None
        error: Exception | None = None
        try:
            detail = self._events.project_notification(receipt)
        except Exception as exc:
            error = exc
        self._runtime_call(
            self._events.settle_notification_projection,
            receipt,
            detail,
            error=error,
        )

    def run_notification_attachment_cleanup(self, scope_key: str) -> None:
        """Run rebuildable attachment deletion on the admitted worker."""

        self._events.run_attachment_cleanup(scope_key)

    def client_disconnected(self, client_id: str) -> None:
        self._lifecycle.client_disconnected(client_id)

    def client_transport_disconnected(self, client_id: str) -> None:
        """Fail-close interaction delivery when the last browser socket drops.

        This is deliberately narrower than :meth:`client_disconnected`.
        Gateway keeps the document identity and its writer lease during the
        reconnect grace, but an absent WebSocket cannot receive a new approval
        or question.  Do not release the owner, runtime interest, selection,
        or profile here; a same-identity reconnect may still resume them.
        """

        self._lifecycle.client_transport_disconnected(client_id)

    def client_document_reissued(self, client_id: str) -> None:
        """Revoke continuity inherited across a browser document replacement.

        This keeps the durable writer profile and runtime interest intact, but
        requires the replacement document to reconnect and read its thread
        before it can use document-scoped mutation recovery authority.
        """

        self._lifecycle.client_document_reissued(client_id)

    def document_intent_generation_floor(self, client_id: str) -> int:
        """Read the RuntimeLoop-owned intent floor retained across F5."""

        return self._document_registry.intent_generation_floor(client_id)

    def client_connected(self, client_id: str) -> None:
        self._lifecycle.client_connected(client_id)

    def prepare_shutdown(self) -> None:
        self._lifecycle.prepare_shutdown()

    def finish_shutdown(self) -> None:
        self._lifecycle.finish_shutdown()

    def shutdown(self) -> None:
        self._lifecycle.shutdown()

    def backend_disconnected(self) -> None:
        self._lifecycle.backend_disconnected()

    def has_local_runtime_interest(self, thread_id: str) -> bool:
        return self._lifecycle.has_local_runtime_interest(thread_id)

    def retains_runtime(self, thread_id: str) -> bool:
        return self._lifecycle.retains_runtime(thread_id)

    def reconcile_external_pending_interaction_resolved(
        self, root_thread_id: str
    ) -> None:
        """Recheck a root after the Feishu/service interaction blocker clears.

        The shared handler dispatches a notification to Web before it asks the
        Feishu interaction controller to remove that controller's pending
        record. A terminal Web root must get one post-removal check; otherwise
        its independent runtime interest can remain until an unrelated later
        notification arrives.
        """

        self._lifecycle.reconcile_external_pending_interaction_resolved(root_thread_id)

    def _maybe_release_web_runtime(
        self, thread_id: str, *, known_non_active: bool = False
    ) -> None:
        self._lifecycle.maybe_release_web_runtime(
            thread_id,
            known_non_active=known_non_active,
        )

    def _has_pending_for_thread(self, thread_id: str) -> bool:
        return self._lifecycle.has_pending_for_thread(thread_id)

    @staticmethod
    def _require_client_id(client_id: str) -> str:
        return require_web_client_id(client_id)

    def _require_connected_web_document(self, client_id: str) -> str:
        return require_connected_web_document(
            self._document_registry,
            client_id,
        )

    def _settle_runtime_cleanup_candidates(
        self,
        thread_ids: Iterable[str],
    ) -> None:
        """Retry cleanup after edges or lifecycle events."""

        self._lifecycle.settle_runtime_cleanup_candidates(thread_ids)

    @staticmethod
    def _require_thread_id(thread_id: str) -> str:
        return require_web_thread_id(thread_id)
