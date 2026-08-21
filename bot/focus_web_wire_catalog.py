"""Versioned vocabulary for the Focus-owned browser wire boundary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, TypeVar


FOCUS_WEB_WIRE_VERSION: Final = 13
FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES: Final = 16 * 1024
_NAME_RE = re.compile(r"\A[a-z][a-z0-9_]*\Z")
_PATH_PARAMETER_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_SUPPORTED_METHODS = frozenset({"GET", "POST", "DELETE"})


@dataclass(frozen=True, slots=True)
class FocusWebEndpointSpec:
    """One exact Gateway route and its production handler."""

    name: str
    method: str
    path: str
    handler: str

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid Focus Web endpoint name: {self.name!r}")
        if self.method not in _SUPPORTED_METHODS:
            raise ValueError(f"invalid Focus Web endpoint method: {self.method!r}")
        if not self.path.startswith("/api/"):
            raise ValueError(f"invalid Focus Web endpoint path: {self.path!r}")
        if not self.handler.startswith("_handle_"):
            raise ValueError(f"invalid Focus Web endpoint handler: {self.handler!r}")

    @property
    def path_parameters(self) -> tuple[str, ...]:
        return tuple(_PATH_PARAMETER_RE.findall(self.path))


@dataclass(frozen=True, slots=True)
class FocusWebEventSpec:
    """One browser projection event and its routing scope."""

    name: str
    thread_scoped: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid Focus Web event name: {self.name!r}")
        if type(self.thread_scoped) is not bool:
            raise TypeError("Focus Web event thread_scoped must be bool")


@dataclass(frozen=True, slots=True)
class FocusWebEnumSpec:
    """One closed string vocabulary used by a Focus-owned DTO field."""

    name: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid Focus Web enum name: {self.name!r}")
        if not self.values or len(set(self.values)) != len(self.values):
            raise ValueError(f"invalid Focus Web enum values: {self.name!r}")
        if any(type(value) is not str or not value for value in self.values):
            raise ValueError(f"invalid Focus Web enum value: {self.name!r}")


@dataclass(frozen=True, slots=True)
class FocusWebRecordSpec:
    """Required top-level fields and enum fields for one wire record."""

    name: str
    typescript_type: str
    required_fields: tuple[str, ...]
    enum_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.name):
            raise ValueError(f"invalid Focus Web record name: {self.name!r}")
        if self.typescript_type and not self.typescript_type.isidentifier():
            raise ValueError(
                f"invalid Focus Web TypeScript type: {self.typescript_type!r}"
            )
        fields = self.required_fields + tuple(
            field for field, _enum in self.enum_fields
        )
        if any(not _NAME_RE.fullmatch(field) for field in fields):
            raise ValueError(f"invalid Focus Web record field: {self.name!r}")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError(f"duplicate Focus Web required field: {self.name!r}")
        if len({field for field, _enum in self.enum_fields}) != len(self.enum_fields):
            raise ValueError(f"duplicate Focus Web enum field: {self.name!r}")


def _words(value: str) -> tuple[str, ...]:
    return tuple(value.split())


def _enum(name: str, values: str) -> FocusWebEnumSpec:
    return FocusWebEnumSpec(name=name, values=_words(values))


def _record(
    name: str,
    typescript_type: str,
    required_fields: str,
    enum_fields: str = "",
) -> FocusWebRecordSpec:
    parsed_enum_fields: list[tuple[str, str]] = []
    for item in _words(enum_fields):
        field, separator, enum_name = item.partition(":")
        if separator != ":" or not field or not enum_name:
            raise ValueError(f"invalid Focus Web enum field mapping: {item!r}")
        parsed_enum_fields.append((field, enum_name))
    return FocusWebRecordSpec(
        name=name,
        typescript_type=typescript_type,
        required_fields=_words(required_fields),
        enum_fields=tuple(parsed_enum_fields),
    )


FOCUS_WEB_ENDPOINTS: Final = (
    FocusWebEndpointSpec("health", "GET", "/api/health", "_handle_health"),
    FocusWebEndpointSpec(
        "auth_bootstrap",
        "POST",
        "/api/auth/bootstrap",
        "_handle_bootstrap",
    ),
    FocusWebEndpointSpec("auth_logout", "POST", "/api/auth/logout", "_handle_logout"),
    FocusWebEndpointSpec(
        "client_register",
        "POST",
        "/api/client/register",
        "_handle_client_register",
    ),
    FocusWebEndpointSpec("meta", "GET", "/api/meta", "_handle_meta"),
    FocusWebEndpointSpec(
        "operator_status",
        "GET",
        "/api/operator-status",
        "_handle_operator_status",
    ),
    FocusWebEndpointSpec(
        "backend_reset_preview",
        "GET",
        "/api/backend-reset",
        "_handle_backend_reset_preview",
    ),
    FocusWebEndpointSpec(
        "backend_reset_execute",
        "POST",
        "/api/backend-reset",
        "_handle_backend_reset_execute",
    ),
    FocusWebEndpointSpec("profile", "POST", "/api/profile", "_handle_profile_update"),
    FocusWebEndpointSpec(
        "next_turn_settings_read",
        "GET",
        "/api/settings/next-turn",
        "_handle_next_turn_settings_read",
    ),
    FocusWebEndpointSpec(
        "next_turn_settings_update",
        "POST",
        "/api/settings/next-turn",
        "_handle_next_turn_settings_update",
    ),
    FocusWebEndpointSpec(
        "attachment_upload",
        "POST",
        "/api/attachments",
        "_handle_attachment_upload",
    ),
    FocusWebEndpointSpec(
        "attachment_download",
        "GET",
        "/api/attachments/{attachment_id}",
        "_handle_attachment_download",
    ),
    FocusWebEndpointSpec("thread_list", "GET", "/api/threads", "_handle_threads"),
    FocusWebEndpointSpec(
        "thread_start", "POST", "/api/threads", "_handle_start_thread"
    ),
    FocusWebEndpointSpec(
        "thread_read",
        "GET",
        "/api/threads/{thread_id}",
        "_handle_thread",
    ),
    FocusWebEndpointSpec(
        "thread_turns",
        "GET",
        "/api/threads/{thread_id}/turns",
        "_handle_thread_turns",
    ),
    FocusWebEndpointSpec(
        "thread_tool_detail",
        "GET",
        "/api/threads/{thread_id}/turns/{turn_id}/tool-items/{item_id}",
        "_handle_thread_tool_detail",
    ),
    FocusWebEndpointSpec(
        "thread_conversation_search",
        "GET",
        "/api/threads/{thread_id}/conversation-search",
        "_handle_thread_conversation_search",
    ),
    FocusWebEndpointSpec(
        "thread_prompt",
        "POST",
        "/api/threads/{thread_id}/prompt",
        "_handle_prompt",
    ),
    FocusWebEndpointSpec(
        "thread_prompt_result",
        "GET",
        "/api/threads/{thread_id}/prompt-result/{mutation_id}",
        "_handle_prompt_result",
    ),
    FocusWebEndpointSpec(
        "thread_interrupt",
        "POST",
        "/api/threads/{thread_id}/interrupt",
        "_handle_interrupt",
    ),
    FocusWebEndpointSpec(
        "thread_unknown_mutation",
        "POST",
        "/api/threads/{thread_id}/mutation-unknown",
        "_handle_unknown_mutation",
    ),
    FocusWebEndpointSpec(
        "thread_rename",
        "POST",
        "/api/threads/{thread_id}/rename",
        "_handle_rename_thread",
    ),
    FocusWebEndpointSpec(
        "thread_compact",
        "POST",
        "/api/threads/{thread_id}/compact",
        "_handle_compact_thread",
    ),
    FocusWebEndpointSpec(
        "thread_review",
        "POST",
        "/api/threads/{thread_id}/review",
        "_handle_review",
    ),
    FocusWebEndpointSpec(
        "thread_goal_read",
        "GET",
        "/api/threads/{thread_id}/goal",
        "_handle_goal",
    ),
    FocusWebEndpointSpec(
        "thread_goal_set",
        "POST",
        "/api/threads/{thread_id}/goal",
        "_handle_set_goal",
    ),
    FocusWebEndpointSpec(
        "thread_goal_clear",
        "DELETE",
        "/api/threads/{thread_id}/goal",
        "_handle_clear_goal",
    ),
    FocusWebEndpointSpec(
        "thread_archive",
        "POST",
        "/api/threads/{thread_id}/archive",
        "_handle_archive_thread",
    ),
    FocusWebEndpointSpec(
        "thread_unarchive",
        "POST",
        "/api/threads/{thread_id}/unarchive",
        "_handle_unarchive_thread",
    ),
    FocusWebEndpointSpec(
        "thread_delete",
        "DELETE",
        "/api/threads/{thread_id}",
        "_handle_delete_thread",
    ),
    FocusWebEndpointSpec(
        "request_respond",
        "POST",
        "/api/requests/{request_id}/respond",
        "_handle_request_response",
    ),
    FocusWebEndpointSpec("events", "GET", "/api/events", "_handle_events"),
)

FOCUS_WEB_EVENTS: Final = (
    FocusWebEventSpec("backend_disconnected"),
    FocusWebEventSpec("hello"),
    FocusWebEventSpec("mutation_reconciled", thread_scoped=True),
    FocusWebEventSpec("mutation_unknown", thread_scoped=True),
    FocusWebEventSpec("mutation_verified", thread_scoped=True),
    FocusWebEventSpec("owner_changed", thread_scoped=True),
    FocusWebEventSpec("pending_request_changed", thread_scoped=True),
    FocusWebEventSpec("profile_changed"),
    FocusWebEventSpec("projection_invalidated"),
    FocusWebEventSpec("runtime_changed"),
    FocusWebEventSpec("runtime_notice"),
    FocusWebEventSpec("settings_changed"),
    FocusWebEventSpec("session_expired"),
    FocusWebEventSpec("thread_delta", thread_scoped=True),
    FocusWebEventSpec("thread_invalidated", thread_scoped=True),
)

FOCUS_WEB_ENUMS: Final = (
    _enum("thread_scope", "current global"),
    _enum("thread_history_mode", "legacy paginated unknown"),
    _enum("turn_items_view", "summary full"),
    _enum("thread_tool_kind", "commandExecution fileChange"),
    _enum("tool_detail_view", "preview full"),
    _enum("tool_detail_scan_status", "scanning found not_found"),
    _enum("warning_severity", "warning error"),
    _enum("warning_attention", "advisory correctness"),
    _enum("runtime_notice_method", "error warning"),
    _enum("backend_reset_status", "available force-only unavailable"),
    _enum(
        "active_turn_initiator_kind",
        "feishu web fcodex autonomous_or_unknown",
    ),
    _enum(
        "active_turn_setting_source",
        "active_reroute inherited unknown",
    ),
    _enum("lifecycle_operation", "archive unarchive delete"),
    _enum("lifecycle_target_state", "present archived deleted"),
    _enum("lifecycle_verification_status", "already_reconciled"),
    _enum(
        "profile_attachment_disposition",
        "unchanged invalidated rebound",
    ),
    _enum("selection_attachment_disposition", "unchanged isolated"),
    _enum("owner_relation", "none self other"),
    _enum("pending_interaction", "none approval question"),
    _enum("goal_status", "active paused blocked complete usageLimited budgetLimited"),
    _enum("interaction_kind", "approval question elicitation"),
    _enum("interaction_action_style", "primary secondary danger"),
    _enum("unknown_mutation_durability", "process_local"),
    _enum("mutation_mode", "started"),
    _enum(
        "prompt_result_status",
        "pending succeeded known_no_effect outcome_unknown",
    ),
    _enum("prompt_result_mode", "start steer"),
    _enum("mutation_action", "compact review"),
    _enum(
        "mutation_disposition",
        "effect_observed user_discard retry_opened",
    ),
    _enum("lifecycle_upstream_outcome", "success error unknown"),
    _enum("lifecycle_cleanup", "complete incomplete skipped"),
    _enum("stream_delta_kind", "text thinking thinking_separator tool_output plan"),
    _enum("task_state", "run done fail pending"),
    _enum(
        "task_execution_state",
        "active waiting_on_approval waiting_on_user_input idle not_loaded "
        "system_error completed interrupted failed unknown",
    ),
)

FOCUS_WEB_RECORDS: Final = (
    _record("coordinates", "FocusCoordinates", "runtime_epoch revision"),
    _record(
        "capability_map",
        "FocusCapabilityMap",
        "prompt new_thread interrupt approvals questions markdown katex mermaid "
        "file_preview terminal attachments prompt_queue durable_event_cursor "
        "bounded_history history_search tool_detail steer",
    ),
    _record("reasoning_effort", "FocusReasoningEffort", "effort description"),
    _record("service_tier", "", "id name description"),
    _record(
        "model_upgrade_info", "", "model upgrade_copy model_link migration_markdown"
    ),
    _record(
        "model",
        "FocusModel",
        "id catalog_id model display_name description is_default hidden "
        "default_reasoning_effort supported_reasoning_efforts input_modalities "
        "supports_personality service_tiers default_service_tier upgrade upgrade_info",
    ),
    _record("permissions_profile", "", "id label"),
    _record(
        "next_turn_settings",
        "FocusNextTurnSettings",
        "generation model reasoning_effort approval_policy permissions_profile_id",
    ),
    _record(
        "next_turn_settings_result",
        "FocusNextTurnSettingsResult",
        "runtime_epoch revision next_turn_settings",
    ),
    _record(
        "meta",
        "FocusMeta",
        "runtime_epoch revision product instance csrf_token default_working_dir "
        "models writer_profile next_turn_settings approval_policies "
        "permissions_profiles capabilities",
    ),
    _record(
        "operator_warning",
        "FocusOperatorWarning",
        "code source message severity attention first_seen_at last_seen_at occurrences details",
        "severity:warning_severity attention:warning_attention",
    ),
    _record(
        "operator_status",
        "FocusOperatorStatus",
        "status observed_at poll_after_seconds warnings runtime_loop",
    ),
    _record(
        "runtime_error_notice_detail",
        "FocusRuntimeErrorNoticeDetail",
        "method message additional_details will_retry turn_id",
        "method:runtime_notice_method",
    ),
    _record(
        "runtime_warning_notice_detail",
        "FocusRuntimeWarningNoticeDetail",
        "method message",
        "method:runtime_notice_method",
    ),
    _record(
        "backend_reset_preview",
        "FocusBackendResetPreview",
        "instance status reason_code reason_text expected_connection_generation "
        "pending_request_count running_binding_count attached_binding_count "
        "active_loaded_thread_count loaded_thread_count runtime_verification_failed",
        "status:backend_reset_status",
    ),
    _record(
        "backend_reset_result",
        "FocusBackendResetResult",
        "force detached_binding_count interrupted_binding_count retired_request_count "
        "purged_thread_count projection_warnings",
    ),
    _record(
        "lifecycle_verification",
        "FocusLifecycleVerification",
        "state verification_id",
        "state:lifecycle_target_state",
    ),
    _record(
        "unknown_lifecycle_mutation",
        "FocusUnknownLifecycleMutation",
        "thread_id mutation_id operation",
        "operation:lifecycle_operation",
    ),
    _record(
        "lifecycle_verification_result",
        "FocusLifecycleVerificationResult",
        "runtime_epoch revision accepted thread_id mutation_id",
        "status:lifecycle_verification_status operation:lifecycle_operation",
    ),
    _record(
        "writer_profile",
        "FocusWriterProfile",
        "selected_thread_id working_dir scope_generation",
    ),
    _record(
        "writer_profile_result",
        "FocusWriterProfileResult",
        "runtime_epoch revision writer_profile scope_changed "
        "previous_attachment_scope current_attachment_scope previous_scope_generation "
        "current_scope_generation attachment_scope_disposition "
        "invalidated_attachment_count rebound_attachment_count",
        "attachment_scope_disposition:profile_attachment_disposition",
    ),
    _record(
        "thread_selection_scope",
        "FocusThreadSelectionScope",
        "writer_profile scope_changed previous_attachment_scope current_attachment_scope "
        "previous_scope_generation current_scope_generation attachment_scope_disposition",
        "attachment_scope_disposition:selection_attachment_disposition",
    ),
    _record(
        "attachment_upload",
        "FocusAttachmentUpload",
        "file_id name media_type size url",
    ),
    _record(
        "owner",
        "FocusOwner",
        "kind holder_id relation label",
        "relation:owner_relation",
    ),
    _record(
        "thread_action_capabilities",
        "FocusThreadActionCapabilities",
        "rename archive unarchive delete compact fork export review goal",
    ),
    _record(
        "thread_summary",
        "FocusThreadSummary",
        "id title name preview cwd created_at updated_at source status active_flags "
        "model_provider service_name session_id parent_thread_id can_accept_direct_input "
        "thread_source ephemeral agent_nickname agent_role subagent_kind owner "
        "pending_interaction loaded_instance loaded_state_verified observed_here selectable unavailable_reason "
        "action_capabilities history_mode",
        "pending_interaction:pending_interaction history_mode:thread_history_mode",
    ),
    _record(
        "goal_budget",
        "",
        "token_budget remaining_tokens turn_budget remaining_turns wall_clock_budget_ms "
        "remaining_wall_clock_ms over_budget",
    ),
    _record(
        "goal",
        "FocusGoal",
        "goal_id objective status tokens_used wall_clock_ms budget",
        "status:goal_status",
    ),
    _record(
        "pending_request",
        "FocusPendingRequest",
        "id connection_generation response_capability kind method thread_id turn_id status "
        "title params owner_thread_id agent_name actions",
        "kind:interaction_kind",
    ),
    _record(
        "interaction_action",
        "FocusInteractionAction",
        "id label style",
        "style:interaction_action_style",
    ),
    _record(
        "thread_list",
        "FocusThreadList",
        "runtime_epoch revision scope archived limit truncated threads",
        "scope:thread_scope",
    ),
    _record(
        "unknown_mutation",
        "",
        "mutation_id operation durability reconciling",
        "durability:unknown_mutation_durability",
    ),
    _record(
        "active_turn_initiator",
        "FocusActiveTurnInitiator",
        "kind binding_id",
        "kind:active_turn_initiator_kind",
    ),
    _record(
        "active_turn_setting",
        "FocusActiveTurnSetting",
        "value source",
        "source:active_turn_setting_source",
    ),
    _record(
        "active_turn_settings",
        "FocusActiveTurnSettings",
        "model reasoning_effort approval_policy permissions_profile_id",
    ),
    _record(
        "active_turn_context",
        "FocusActiveTurnContext",
        "turn_id initiator feishu_audience settings",
    ),
    _record(
        "thread_snapshot",
        "FocusThreadSnapshot",
        "runtime_epoch revision thread turns active_turn_id active_turn_status "
        "active_turn_context "
        "pending_requests tasks older_turn_cursor has_more_turns goal "
        "token_usage token_usage_available mutation_unknown selection_scope",
    ),
    _record("token_usage", "FocusTokenUsage", ""),
    _record("token_breakdown", "FocusTokenBreakdown", ""),
    _record(
        "summary_prompt",
        "FocusSummaryPrompt",
        "id role no text title_truncated",
    ),
    _record(
        "turn_page",
        "FocusTurnPage",
        "runtime_epoch revision items_view page_cursor turns older_turn_cursor has_more_turns",
        "items_view:turn_items_view",
    ),
    _record(
        "tool_inspection_locator",
        "FocusToolInspectionLocator",
        "turn_id item_id kind change_index",
        "kind:thread_tool_kind",
    ),
    _record(
        "tool_detail_preview",
        "FocusThreadToolDetailPreview",
        "view tool",
        "view:tool_detail_view",
    ),
    _record(
        "tool_detail_full",
        "FocusThreadToolDetailFull",
        "view source",
        "view:tool_detail_view",
    ),
    _record(
        "thread_tool_detail_scan_page",
        "FocusThreadToolDetailScanPage",
        "runtime_epoch revision thread_id turn_id item_id kind change_index "
        "status cursor next_cursor scanned_items view detail",
        "kind:thread_tool_kind status:tool_detail_scan_status view:tool_detail_view",
    ),
    _record(
        "conversation_search_match_range",
        "FocusConversationSearchMatchRange",
        "start end",
    ),
    _record(
        "conversation_search_occurrence",
        "FocusConversationSearchOccurrence",
        "turn_id item_id snippet snippet_match_range turn_cursor",
    ),
    _record(
        "thread_conversation_search_page",
        "FocusThreadConversationSearchPage",
        "runtime_epoch revision thread_id query cursor occurrences next_cursor",
    ),
    _record(
        "mutation_result",
        "FocusMutationResult",
        "accepted thread_id",
        "mode:mutation_mode action:mutation_action disposition:mutation_disposition",
    ),
    _record(
        "prompt_result_receipt",
        "FocusPromptResultReceipt",
        "thread_id mutation_id client_user_message_id status mode turn_id reason_code",
        "status:prompt_result_status mode:prompt_result_mode",
    ),
    _record("rename_result", "FocusRenameResult", "accepted thread_id name"),
    _record(
        "goal_result",
        "FocusGoalResult",
        "runtime_epoch revision thread_id goal",
    ),
    _record(
        "lifecycle_result",
        "FocusLifecycleResult",
        "thread_id upstream_outcome focus_cleanup cleanup_errors",
        "upstream_outcome:lifecycle_upstream_outcome focus_cleanup:lifecycle_cleanup",
    ),
    _record(
        "projection_event",
        "FocusProjectionEvent",
        "type runtime_epoch revision",
    ),
    _record("gateway_error_body", "FocusGatewayErrorBody", "error"),
    _record(
        "document_registration",
        "FocusDocumentRegistration",
        "runtime_epoch revision client_id document_token document_receipt duplicate intent_generation_floor csrf_token",
    ),
    _record(
        "bootstrap_result",
        "FocusBootstrapResult",
        "runtime_epoch revision authenticated csrf_token expires_at",
    ),
    _record(
        "request_response_result",
        "FocusRequestResponseResult",
        "accepted",
    ),
    _record(
        "stream_delta",
        "FocusStreamDelta",
        "turn_id item_id kind delta",
        "kind:stream_delta_kind",
    ),
    _record(
        "task_item",
        "",
        "id name kind state timing",
        "state:task_state",
    ),
    _record("thread_delta_detail", "FocusThreadDeltaDetail", "method"),
)


_WireSpec = TypeVar("_WireSpec")


def _index_unique(
    items: tuple[_WireSpec, ...],
    *,
    attribute: str,
) -> Mapping[str, _WireSpec]:
    indexed: dict[str, _WireSpec] = {}
    for item in items:
        key = getattr(item, attribute)
        if key in indexed:
            raise ValueError(f"duplicate Focus Web wire {attribute}: {key}")
        indexed[key] = item
    return MappingProxyType(indexed)


FOCUS_WEB_ENDPOINT_BY_NAME: Final[Mapping[str, FocusWebEndpointSpec]] = _index_unique(
    FOCUS_WEB_ENDPOINTS,
    attribute="name",
)
FOCUS_WEB_EVENT_BY_NAME: Final[Mapping[str, FocusWebEventSpec]] = _index_unique(
    FOCUS_WEB_EVENTS,
    attribute="name",
)
FOCUS_WEB_ENUM_BY_NAME: Final[Mapping[str, FocusWebEnumSpec]] = _index_unique(
    FOCUS_WEB_ENUMS,
    attribute="name",
)
FOCUS_WEB_RECORD_BY_NAME: Final[Mapping[str, FocusWebRecordSpec]] = _index_unique(
    FOCUS_WEB_RECORDS,
    attribute="name",
)

_typescript_types = {
    record.typescript_type for record in FOCUS_WEB_RECORDS if record.typescript_type
}
if len(_typescript_types) != sum(
    bool(record.typescript_type) for record in FOCUS_WEB_RECORDS
):
    raise ValueError("duplicate Focus Web record TypeScript type")
for record in FOCUS_WEB_RECORDS:
    for _field, enum_name in record.enum_fields:
        if enum_name not in FOCUS_WEB_ENUM_BY_NAME:
            raise ValueError(
                f"Focus Web record {record.name!r} references unknown enum "
                f"{enum_name!r}"
            )

_route_keys = {(endpoint.method, endpoint.path) for endpoint in FOCUS_WEB_ENDPOINTS}
if len(_route_keys) != len(FOCUS_WEB_ENDPOINTS):
    raise ValueError("duplicate Focus Web endpoint method/path")


def require_focus_web_event_type(value: object) -> str:
    """Return one exact catalog event name or reject the producer typo."""

    if type(value) is not str or value not in FOCUS_WEB_EVENT_BY_NAME:
        raise ValueError(f"unknown Focus Web projection event type: {value!r}")
    return value
