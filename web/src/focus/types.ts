import type { ChatTurn, TaskItem, ToolCall } from '../types';
import type {
  FocusWebEventType,
  FocusWebWireEnum,
} from './focusWire.generated';
import type { FocusThreadToolKind } from './threadInspectionTypes';

export type {
  FocusThreadInspectionUnavailableReason,
  FocusThreadToolKind,
  FocusToolInspectionLocator,
} from './threadInspectionTypes';

export type FocusThreadScope = FocusWebWireEnum<'thread_scope'>;
export type FocusThreadHistoryMode = FocusWebWireEnum<'thread_history_mode'>;

export interface FocusCoordinates {
  runtime_epoch: string;
  revision: number;
}

export interface FocusDocumentRegistration extends FocusCoordinates {
  client_id: string;
  document_token: string;
  /** Non-authorizing identity for this exact server-issued document token. */
  document_receipt: string;
  duplicate: boolean;
  intent_generation_floor: number;
  csrf_token: string;
}

export interface FocusBootstrapResult extends FocusCoordinates {
  authenticated: true;
  csrf_token: string;
  expires_at: number;
}

export interface FocusRequestResponseResult {
  accepted: true;
}

export interface FocusCapabilityMap {
  prompt: boolean;
  new_thread: boolean;
  interrupt: boolean;
  approvals: boolean;
  questions: boolean;
  markdown: boolean;
  katex: boolean;
  mermaid: boolean;
  file_preview: boolean;
  terminal: boolean;
  attachments: boolean;
  prompt_queue: boolean;
  durable_event_cursor: boolean;
  bounded_history: boolean;
  history_search: boolean;
  tool_detail: boolean;
  steer: boolean;
}

export interface FocusReasoningEffort {
  effort: string;
  description: string;
}

export interface FocusModel {
  id: string;
  catalog_id: string;
  model: string;
  display_name: string;
  description: string;
  is_default: boolean;
  hidden: boolean;
  default_reasoning_effort: string;
  supported_reasoning_efforts: FocusReasoningEffort[];
  /** null means the connected app-server did not provide capability metadata. */
  input_modalities: string[] | null;
  supports_personality: boolean | null;
  service_tiers: Array<{ id: string; name: string; description: string }>;
  default_service_tier: string;
  upgrade: string;
  upgrade_info: {
    model: string;
    upgrade_copy: string;
    model_link: string;
    migration_markdown: string;
  } | null;
}

export interface FocusMeta extends FocusCoordinates {
  product: string;
  instance: string;
  web_display_name: string;
  csrf_token: string;
  default_working_dir: string;
  models: FocusModel[];
  writer_profile: FocusWriterProfile;
  next_turn_settings: FocusNextTurnSettings;
  approval_policies: string[];
  permissions_profiles: { id: string; label: string }[];
  capabilities: FocusCapabilityMap;
  /**
   * Lifecycle mutations whose result is still unknown and whose writer is this
   * browser document.  This is a server-side recovery index, not a history of
   * completed lifecycle actions.
   */
  unknown_lifecycle_mutations?: FocusUnknownLifecycleMutation[];
}

/** Rebuildable runtime metadata without document-owned writer state. */
export type FocusMetaEnvelope = Omit<FocusMeta, 'writer_profile' | 'next_turn_settings'>;

export interface FocusOperatorWarning {
  code: string;
  source: string;
  message: string;
  severity: FocusWebWireEnum<'warning_severity'>;
  attention: FocusWebWireEnum<'warning_attention'>;
  first_seen_at: number;
  last_seen_at: number;
  occurrences: number;
  details: Record<string, unknown>;
}

export interface FocusOperatorStatus {
  status: 'ok' | 'degraded' | string;
  observed_at: number;
  /** Server-owned refresh cadence for this ephemeral projection. */
  poll_after_seconds: number;
  warnings: FocusOperatorWarning[];
  runtime_loop: Record<string, unknown>;
}

export type FocusOperatorStatusFreshness = 'loading' | 'fresh' | 'stale';

export interface FocusBackendResetPreview {
  instance: string;
  status: FocusWebWireEnum<'backend_reset_status'>;
  reason_code: string;
  reason_text: string;
  expected_connection_generation: number;
  pending_request_count: number;
  running_binding_count: number;
  attached_binding_count: number;
  active_loaded_thread_count: number;
  loaded_thread_count: number;
  runtime_verification_failed: boolean;
}

export interface FocusBackendResetResult {
  force: boolean;
  detached_binding_count: number;
  interrupted_binding_count: number;
  retired_request_count: number;
  purged_thread_count: number;
  projection_warnings: string[];
}

export type FocusLifecycleOperation = FocusWebWireEnum<'lifecycle_operation'>;
export type FocusUnknownMutationDurability =
  FocusWebWireEnum<'unknown_mutation_durability'>;
export type FocusMutationDisposition = FocusWebWireEnum<'mutation_disposition'>;

/** Browser-authored identity and payload for one server-routed prompt effect. */
export interface FocusPromptRequest {
  text: string;
  attachmentIds: string[];
  mutationId: string;
  sourceScopeGeneration: number;
  sourceAttachmentScope: string;
  sourceComposerScopeId: string;
}

export type FocusPromptResultStatus = FocusWebWireEnum<'prompt_result_status'>;

export type FocusPromptResultMode = FocusWebWireEnum<'prompt_result_mode'>;

/** Process-local receipt for the one browser POST and its pure GET lookup. */
export interface FocusPromptResultReceipt {
  thread_id: string;
  mutation_id: string;
  client_user_message_id: string;
  status: FocusPromptResultStatus;
  mode: FocusPromptResultMode;
  turn_id: string;
  reason_code: string;
}

/** The three lifecycle states that app-server can authoritatively distinguish. */
export type FocusLifecycleTargetState =
  FocusWebWireEnum<'lifecycle_target_state'>;

export interface FocusLifecycleVerification {
  state: FocusLifecycleTargetState;
  verification_id: string;
}

export interface FocusUnknownLifecycleMutation {
  thread_id: string;
  mutation_id: string;
  operation: FocusLifecycleOperation;
  verification?: FocusLifecycleVerification | null;
}

export interface FocusLifecycleVerificationResult extends FocusCoordinates {
  accepted: true;
  thread_id: string;
  mutation_id: string;
  /**
   * A concurrent authoritative notification may reconcile the lock between
   * the browser click and the direct read.  In that case the server returns
   * `already_reconciled` without fabricating stale verification data.
   */
  status?: FocusWebWireEnum<'lifecycle_verification_status'>;
  operation?: FocusLifecycleOperation;
  verification?: FocusLifecycleVerification;
}

export interface FocusWriterProfile {
  selected_thread_id: string;
  working_dir: string;
  scope_generation: number;
}

export interface FocusNextTurnSettings {
  generation: number;
  model: string;
  reasoning_effort: string;
  approval_policy: string;
  permissions_profile_id: string;
}

export interface FocusNextTurnSettingsResult extends FocusCoordinates {
  next_turn_settings: FocusNextTurnSettings;
}

export interface FocusWriterProfileResult extends FocusCoordinates {
  writer_profile: FocusWriterProfile;
  scope_changed: boolean;
  previous_attachment_scope: string;
  current_attachment_scope: string;
  previous_scope_generation: number;
  current_scope_generation: number;
  attachment_scope_disposition: FocusWebWireEnum<'profile_attachment_disposition'>;
  invalidated_attachment_count: number;
  rebound_attachment_count: number;
}

/**
 * Authoritative attachment-scope consequence of opening a thread.
 *
 * Selection keeps completed pending attachments isolated under their semantic
 * thread/draft scope; `isolated` means the browser moved between two such
 * scopes without rebinding either scope's records. The generation still
 * advances so uploads which crossed the navigation boundary fail closed.
 */
export interface FocusThreadSelectionScope {
  writer_profile: FocusWriterProfile;
  scope_changed: boolean;
  previous_attachment_scope: string;
  current_attachment_scope: string;
  previous_scope_generation: number;
  current_scope_generation: number;
  attachment_scope_disposition: FocusWebWireEnum<'selection_attachment_disposition'>;
}

export type WorkspaceDraftOpenOutcome =
  | {
      status: 'committed';
      committed: true;
      workspace: string;
      scopeChanged: boolean;
      previousComposerScopeId: string;
      currentComposerScopeId: string;
      attachmentDisposition: FocusWriterProfileResult['attachment_scope_disposition'];
      composerScopeEffect: 'none' | 'apply' | 'clearPrevious';
      invalidatedAttachmentCount: number;
      reboundAttachmentCount: number;
    }
  | {
      status: 'superseded';
      committed: boolean;
      workspace: string;
      scopeChanged: boolean;
      previousComposerScopeId: string;
      currentComposerScopeId: string;
      attachmentDisposition: FocusWriterProfileResult['attachment_scope_disposition'];
      composerScopeEffect: 'none' | 'apply' | 'clearPrevious';
      invalidatedAttachmentCount: number;
      reboundAttachmentCount: number;
    }
  | {
      status: 'failed';
      committed: false;
      workspace: '';
      scopeChanged: false;
      previousComposerScopeId: '';
      currentComposerScopeId: '';
      attachmentDisposition: 'unchanged';
      composerScopeEffect: 'none';
      invalidatedAttachmentCount: 0;
      reboundAttachmentCount: 0;
    };

export interface FocusAttachmentUpload {
  file_id: string;
  name: string;
  media_type: string;
  size: number;
  url: string;
}

export interface FocusOwner {
  kind: string;
  holder_id: string;
  relation: FocusWebWireEnum<'owner_relation'>;
  label: string;
}

/**
 * Server-projected availability for thread-scoped browser actions. It is a
 * fail-closed presentation contract: endpoints still re-check live ownership
 * because a writer can change after this snapshot was produced.
 */
export interface FocusThreadActionCapabilities {
  rename: boolean;
  archive: boolean;
  unarchive: boolean;
  delete: boolean;
  compact: boolean;
  fork: boolean;
  export: boolean;
  review: boolean;
  goal: boolean;
}

export interface FocusThreadSummary {
  id: string;
  title: string;
  name: string;
  preview: string;
  cwd: string;
  created_at: number;
  updated_at: number;
  source: string;
  status: string;
  active_flags: string[];
  model_provider: string;
  service_name: string;
  session_id: string;
  parent_thread_id: string;
  can_accept_direct_input: boolean | null;
  thread_source: string;
  ephemeral: boolean;
  agent_nickname: string;
  agent_role: string;
  subagent_kind: string;
  owner: FocusOwner;
  pending_interaction: FocusWebWireEnum<'pending_interaction'>;
  loaded_instance: string;
  loaded_state_verified: boolean;
  observed_here: boolean;
  selectable: boolean;
  unavailable_reason: string;
  action_capabilities: FocusThreadActionCapabilities;
  history_mode: FocusThreadHistoryMode;
}

export interface FocusGoal {
  goal_id: string;
  objective: string;
  status: FocusWebWireEnum<'goal_status'>;
  tokens_used: number;
  wall_clock_ms: number;
  budget: {
    token_budget: number | null;
    remaining_tokens: number | null;
    turn_budget: number | null;
    remaining_turns: number | null;
    wall_clock_budget_ms: number | null;
    remaining_wall_clock_ms: number | null;
    over_budget: boolean;
  };
}

export interface FocusPendingRequest {
  id: string;
  connection_generation: number;
  response_capability: string;
  kind: FocusWebWireEnum<'interaction_kind'>;
  method: string;
  thread_id: string;
  turn_id: string;
  status: string;
  title: string;
  params: Record<string, unknown>;
  owner_thread_id: string;
  agent_name: string;
  actions: FocusInteractionAction[];
}

export interface FocusInteractionAction {
  id: string;
  label: string;
  style: FocusWebWireEnum<'interaction_action_style'>;
}

export interface FocusThreadList extends FocusCoordinates {
  scope: FocusThreadScope;
  archived: boolean;
  limit: number;
  truncated: boolean;
  threads: FocusThreadSummary[];
}

export interface FocusActiveTurnInitiator {
  kind: FocusWebWireEnum<'active_turn_initiator_kind'>;
  binding_id: string;
}

export interface FocusActiveTurnSetting {
  value: string;
  source: FocusWebWireEnum<'active_turn_setting_source'>;
}

export interface FocusActiveTurnSettings {
  model: FocusActiveTurnSetting;
  reasoning_effort: FocusActiveTurnSetting;
  approval_policy: FocusActiveTurnSetting;
  permissions_profile_id: FocusActiveTurnSetting;
}

export interface FocusActiveTurnContext {
  turn_id: string;
  initiator: FocusActiveTurnInitiator;
  feishu_audience: string[];
  settings: FocusActiveTurnSettings;
}

export interface FocusThreadSnapshot extends FocusCoordinates {
  thread: FocusThreadSummary;
  turns: ChatTurn[];
  active_turn_id: string;
  active_turn_status: string;
  active_turn_context: FocusActiveTurnContext | null;
  pending_requests: FocusPendingRequest[];
  tasks: TaskItem[];
  older_turn_cursor: string;
  has_more_turns: boolean;
  goal: FocusGoal | null;
  token_usage: FocusTokenUsage | null;
  token_usage_available: boolean;
  mutation_unknown: {
    mutation_id: string;
    operation: string;
    durability: FocusUnknownMutationDurability;
    reconciling: boolean;
    lifecycle_verification?: FocusLifecycleVerification | null;
  } | null;
  selection_scope: FocusThreadSelectionScope;
}

export interface FocusTokenUsage {
  total?: FocusTokenBreakdown;
  last?: FocusTokenBreakdown;
  modelContextWindow?: number | null;
}

export interface FocusTokenBreakdown {
  totalTokens?: number;
  inputTokens?: number;
  cachedInputTokens?: number;
  outputTokens?: number;
  reasoningOutputTokens?: number;
}

export interface FocusSummaryPrompt {
  id: string;
  role: 'user';
  no: number;
  text: string;
  title_truncated: boolean;
}

export interface FocusTurnPage extends FocusCoordinates {
  items_view: FocusWebWireEnum<'turn_items_view'>;
  page_cursor: string;
  turns: ChatTurn[] | FocusSummaryPrompt[];
  older_turn_cursor: string;
  has_more_turns: boolean;
}

export type FocusThreadToolDetailScanStatus = FocusWebWireEnum<'tool_detail_scan_status'>;
export type FocusToolDetailView = FocusWebWireEnum<'tool_detail_view'>;

/** Bounded semantic presentation returned by an explicit preview request. */
export interface FocusThreadToolDetailPreview {
  view: 'preview';
  tool: ToolCall;
}

export type FocusCommandExecutionSourceAction =
  | { type: 'read'; command: string; name: string; path: string }
  | { type: 'listFiles'; command: string; path: string | null }
  | { type: 'search'; command: string; query: string | null; path: string | null }
  | { type: 'unknown'; command: string };

export interface FocusCommandExecutionSourceDetail {
  type: 'commandExecution';
  id: string;
  pluginId: string | null;
  scriptPath: string | null;
  command: string;
  cwd: string;
  processId: string | null;
  source: string;
  status: string;
  commandActions: FocusCommandExecutionSourceAction[];
  aggregatedOutput: string | null;
  exitCode: number | null;
  durationMs: number | null;
}

export type FocusFileChangeSourceKind =
  | { type: 'add' }
  | { type: 'delete' }
  | { type: 'update'; movePath: string | null };

export interface FocusFileChangeSourceChange {
  path: string;
  kind: FocusFileChangeSourceKind;
  diff: string;
}

export interface FocusFileChangeSourceDetail {
  type: 'fileChange';
  id: string;
  changes: FocusFileChangeSourceChange[];
  status: string;
}

export type FocusThreadToolDetailSource =
  | FocusCommandExecutionSourceDetail
  | FocusFileChangeSourceDetail;

/** Complete persisted source returned only by an explicit full request. */
export interface FocusThreadToolDetailFull {
  view: 'full';
  source: FocusThreadToolDetailSource;
}

export type FocusThreadToolDetailPayload =
  | FocusThreadToolDetailPreview
  | FocusThreadToolDetailFull;

export interface FocusThreadToolDetailScanPage extends FocusCoordinates {
  thread_id: string;
  turn_id: string;
  item_id: string;
  kind: FocusThreadToolKind;
  change_index: number | null;
  status: FocusThreadToolDetailScanStatus;
  cursor: string | null;
  next_cursor: string | null;
  scanned_items: number;
  view: FocusToolDetailView;
  detail: FocusThreadToolDetailPayload | null;
}

export interface FocusConversationSearchMatchRange {
  start: number;
  end: number;
}

export interface FocusConversationSearchOccurrence {
  turn_id: string;
  item_id: string;
  snippet: string;
  snippet_match_range: FocusConversationSearchMatchRange;
  turn_cursor: string;
}

export interface FocusThreadConversationSearchPage extends FocusCoordinates {
  thread_id: string;
  query: string;
  cursor: string | null;
  occurrences: FocusConversationSearchOccurrence[];
  next_cursor: string | null;
}

export interface FocusMutationResult {
  accepted: true;
  mutation_id?: string;
  mode?: FocusWebWireEnum<'mutation_mode'>;
  action?: FocusWebWireEnum<'mutation_action'>;
  thread_id: string;
  turn_id?: string;
  owner?: FocusOwner;
  disposition?: FocusMutationDisposition;
}

export interface FocusRenameResult {
  accepted: boolean;
  thread_id: string;
  name: string;
}

export interface FocusGoalResult extends FocusCoordinates {
  thread_id: string;
  goal: FocusGoal | null;
  cleared?: boolean;
}

export interface FocusLifecycleResult {
  thread_id: string;
  mutation_id?: string;
  thread_title?: string;
  working_dir?: string;
  upstream_outcome: FocusWebWireEnum<'lifecycle_upstream_outcome'>;
  upstream_error?: string;
  outcome_detail?: string;
  focus_cleanup: FocusWebWireEnum<'lifecycle_cleanup'>;
  cleanup_errors: string[];
  cleared_binding_ids?: string[];
}

export interface FocusProjectionEvent extends FocusCoordinates {
  type: FocusWebEventType;
  thread_id?: string;
  reason?: string;
  occurred_at?: number;
  detail?: Record<string, unknown>;
}

export interface FocusRuntimeErrorNoticeDetail {
  method: FocusWebWireEnum<'runtime_notice_method'> & 'error';
  message: string;
  additional_details: string;
  will_retry: boolean;
  turn_id: string;
}

export interface FocusRuntimeWarningNoticeDetail {
  method: FocusWebWireEnum<'runtime_notice_method'> & 'warning';
  message: string;
}

export type FocusRuntimeNoticeDetail =
  | FocusRuntimeErrorNoticeDetail
  | FocusRuntimeWarningNoticeDetail;

export interface FocusStreamDelta {
  turn_id: string;
  item_id: string;
  kind: FocusWebWireEnum<'stream_delta_kind'>;
  delta: string;
  tool_name?: string;
}

export interface FocusThreadDeltaDetail {
  method: string;
  thread_name?: string;
  active_turn_id?: string;
  active_turn_status?: string;
  turn_id?: string;
  token_usage_durable?: boolean;
  plan_replay?: string;
  stream_delta?: FocusStreamDelta;
  thread_status?: { type: string; activeFlags?: string[] };
  turns?: ChatTurn[];
  tasks?: TaskItem[];
  goal?: FocusGoal | null;
  token_usage?: FocusTokenUsage;
}

export interface FocusGatewayErrorBody {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
}

export type FocusEffectEvidence = 'pre_effect' | 'unknown';

export class FocusApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;
  readonly effectEvidence: FocusEffectEvidence;

  constructor(message: string, options: {
    status: number;
    code: string;
    details?: Record<string, unknown>;
    effectEvidence?: FocusEffectEvidence;
  }) {
    super(message);
    this.name = 'FocusApiError';
    this.status = options.status;
    this.code = options.code;
    this.details = { ...(options.details ?? {}) };
    this.effectEvidence = options.effectEvidence ?? 'unknown';
  }
}

export type FocusStaleWebReadCode =
  | 'stale_document_read'
  | 'stale_thread_list'
  | 'stale_thread_read';

/** A staged Web read was superseded while its external result was materialized. */
export function isStaleWebReadError(
  error: unknown,
  expectedCode?: FocusStaleWebReadCode,
): error is FocusApiError {
  return error instanceof FocusApiError
    && error.status === 409
    && (
      error.code === 'stale_document_read'
      || error.code === 'stale_thread_list'
      || error.code === 'stale_thread_read'
    )
    && (expectedCode === undefined || error.code === expectedCode);
}

/** A staged thread projection was superseded while its external read ran. */
export function isStaleThreadProjectionError(
  error: unknown,
  expectedCode?: 'stale_thread_list' | 'stale_thread_read',
): error is FocusApiError {
  return isStaleWebReadError(error, expectedCode)
    && error.code !== 'stale_document_read';
}
