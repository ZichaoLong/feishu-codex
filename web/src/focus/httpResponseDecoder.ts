import type {
  FocusActiveTurnContext,
  FocusActiveTurnInitiator,
  FocusActiveTurnSetting,
  FocusAttachmentUpload,
  FocusBackendResetPreview,
  FocusBackendResetResult,
  FocusBootstrapResult,
  FocusCapabilityMap,
  FocusDocumentRegistration,
  FocusGoalResult,
  FocusGatewayErrorBody,
  FocusLifecycleResult,
  FocusLifecycleVerification,
  FocusLifecycleVerificationResult,
  FocusMeta,
  FocusModel,
  FocusMutationResult,
  FocusNextTurnSettings,
  FocusNextTurnSettingsResult,
  FocusOwner,
  FocusPendingRequest,
  FocusPromptResultReceipt,
  FocusRenameResult,
  FocusRequestResponseResult,
  FocusThreadList,
  FocusThreadConversationSearchPage,
  FocusThreadSnapshot,
  FocusThreadSummary,
  FocusThreadToolDetailFull,
  FocusThreadToolDetailPayload,
  FocusThreadToolDetailPreview,
  FocusThreadToolDetailScanPage,
  FocusSummaryPrompt,
  FocusToolDetailView,
  FocusToolInspectionLocator,
  FocusTurnPage,
  FocusUnknownLifecycleMutation,
  FocusWriterProfile,
  FocusWriterProfileResult,
} from './types';
import {
  FOCUS_WEB_RECORDS,
  hasFocusWebRequiredFields,
  isFocusWebWireEnum,
  type FocusWebWireRecordName,
} from './focusWire.generated';
import {
  canonicalizeFocusWireChatTurn,
  decodeFocusOperatorStatus,
  isFocusWireChatTurnWindow,
  isFocusWireGoal,
  isFocusWireRecord,
  isFocusWireTaskItem,
  isFocusWireTokenUsage,
  isFocusWireToolCall,
  isFocusWireToolInspectionLocator,
} from './projectionEventDecoder';
import {
  isCanonicalWebMutationId,
  isWebPromptClientUserMessageId,
} from './webPromptMutation';

/**
 * Runtime decoders in this module are the browser's HTTP wire authority.
 * Imported TypeScript interfaces only describe the value returned after a
 * successful decode; view-model casts never admit data from the network.
 */
export type FocusHttpDecoder<T> = (value: unknown) => T | null;

const WEB_DOCUMENT_RECEIPT_PATTERN = new RegExp('^[0-9a-f]{64}$', 'u');

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function hasString(value: Record<string, unknown>, key: string): boolean {
  return hasOwn(value, key) && typeof value[key] === 'string';
}

function hasBoolean(value: Record<string, unknown>, key: string): boolean {
  return hasOwn(value, key) && typeof value[key] === 'boolean';
}

function isNonEmptyTrimmedString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.trim() === value;
}

function isWebDocumentReceipt(value: unknown): value is string {
  return typeof value === 'string'
    && WEB_DOCUMENT_RECEIPT_PATTERN.test(value);
}

function isTrimmedString(value: unknown): value is string {
  return typeof value === 'string' && value.trim() === value;
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function isNonNegativeFiniteNumber(value: unknown): value is number {
  return isFiniteNumber(value) && value >= 0;
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function isPositiveSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function optional(
  value: Record<string, unknown>,
  key: string,
  predicate: (candidate: unknown) => boolean,
): boolean {
  return !hasOwn(value, key) || predicate(value[key]);
}

function isRequiredRecord(
  name: FocusWebWireRecordName,
  value: unknown,
): value is Record<string, unknown> {
  return isFocusWireRecord(value) && hasFocusWebRequiredFields(name, value);
}

function hasExactRequiredFields(
  name: FocusWebWireRecordName,
  value: Record<string, unknown>,
): boolean {
  return Object.keys(value).length === FOCUS_WEB_RECORDS[name].requiredFields.length;
}

function hasCoordinates(value: Record<string, unknown>): boolean {
  return hasFocusWebRequiredFields('coordinates', value)
    && isNonEmptyTrimmedString(value.runtime_epoch)
    && isNonNegativeSafeInteger(value.revision);
}

function isWriterProfile(value: unknown): value is FocusWriterProfile {
  if (
    !isRequiredRecord('writer_profile', value)
    || !hasExactRequiredFields('writer_profile', value)
  ) return false;
  return FOCUS_WEB_RECORDS.writer_profile.requiredFields.every((key) => (
    key === 'scope_generation'
      ? isPositiveSafeInteger(value[key])
      : hasString(value, key)
  ));
}

function isNextTurnSettings(value: unknown): value is FocusNextTurnSettings {
  if (
    !isRequiredRecord('next_turn_settings', value)
    || !hasExactRequiredFields('next_turn_settings', value)
  ) return false;
  return isPositiveSafeInteger(value.generation)
    && isTrimmedString(value.model)
    && isTrimmedString(value.reasoning_effort)
    && isNonEmptyTrimmedString(value.approval_policy)
    && isNonEmptyTrimmedString(value.permissions_profile_id);
}

function isCapabilityMap(value: unknown): value is FocusCapabilityMap {
  return isRequiredRecord('capability_map', value)
    && FOCUS_WEB_RECORDS.capability_map.requiredFields.every(
      (key) => hasBoolean(value, key),
    );
}

function isReasoningEffort(value: unknown): boolean {
  return isRequiredRecord('reasoning_effort', value)
    && FOCUS_WEB_RECORDS.reasoning_effort.requiredFields.every(
      (key) => hasString(value, key),
    );
}

function isServiceTier(value: unknown): boolean {
  return isRequiredRecord('service_tier', value)
    && FOCUS_WEB_RECORDS.service_tier.requiredFields.every(
      (key) => hasString(value, key),
    );
}

function isUpgradeInfo(value: unknown): boolean {
  return isRequiredRecord('model_upgrade_info', value)
    && FOCUS_WEB_RECORDS.model_upgrade_info.requiredFields.every(
      (key) => hasString(value, key),
    );
}

function isModel(value: unknown): value is FocusModel {
  if (!isRequiredRecord('model', value)) return false;
  for (const key of [
    'id',
    'catalog_id',
    'model',
    'display_name',
    'description',
    'default_reasoning_effort',
    'default_service_tier',
    'upgrade',
  ]) {
    if (!hasString(value, key)) return false;
  }
  if (!hasBoolean(value, 'is_default') || !hasBoolean(value, 'hidden')) return false;
  if (
    !Array.isArray(value.supported_reasoning_efforts)
    || !value.supported_reasoning_efforts.every(isReasoningEffort)
  ) return false;
  if (
    value.input_modalities !== null
    && !isStringArray(value.input_modalities)
  ) return false;
  if (
    value.supports_personality !== null
    && typeof value.supports_personality !== 'boolean'
  ) return false;
  if (!Array.isArray(value.service_tiers) || !value.service_tiers.every(isServiceTier)) {
    return false;
  }
  return value.upgrade_info === null || isUpgradeInfo(value.upgrade_info);
}

function isLifecycleVerification(value: unknown): value is FocusLifecycleVerification {
  return isRequiredRecord('lifecycle_verification', value)
    && isFocusWebWireEnum('lifecycle_target_state', value.state)
    && isNonEmptyTrimmedString(value.verification_id);
}

function isUnknownLifecycleMutation(value: unknown): value is FocusUnknownLifecycleMutation {
  if (!isRequiredRecord('unknown_lifecycle_mutation', value)) return false;
  if (!isNonEmptyTrimmedString(value.thread_id)) return false;
  if (!isNonEmptyTrimmedString(value.mutation_id)) return false;
  if (!isFocusWebWireEnum('lifecycle_operation', value.operation)) return false;
  return optional(
    value,
    'verification',
    (candidate) => candidate === null || isLifecycleVerification(candidate),
  );
}

function isOwner(value: unknown): value is FocusOwner {
  if (!isRequiredRecord('owner', value)) return false;
  return hasString(value, 'kind')
    && hasString(value, 'holder_id')
    && isFocusWebWireEnum('owner_relation', value.relation)
    && hasString(value, 'label');
}

function isThreadActionCapabilities(value: unknown): boolean {
  return isRequiredRecord('thread_action_capabilities', value)
    && FOCUS_WEB_RECORDS.thread_action_capabilities.requiredFields.every(
      (key) => hasBoolean(value, key),
    );
}

function isThreadSummary(value: unknown): value is FocusThreadSummary {
  if (!isRequiredRecord('thread_summary', value)) return false;
  if (!isNonEmptyTrimmedString(value.id)) return false;
  for (const key of [
    'title',
    'name',
    'preview',
    'cwd',
    'source',
    'status',
    'model_provider',
    'service_name',
    'loaded_instance',
    'unavailable_reason',
  ]) {
    if (!hasString(value, key)) return false;
  }
  if (!isNonNegativeFiniteNumber(value.created_at)) return false;
  if (!isNonNegativeFiniteNumber(value.updated_at)) return false;
  if (!isStringArray(value.active_flags)) return false;
  if (!isOwner(value.owner)) return false;
  if (!isFocusWebWireEnum('pending_interaction', value.pending_interaction)) return false;
  if (!isFocusWebWireEnum('thread_history_mode', value.history_mode)) return false;
  if (
    !hasBoolean(value, 'loaded_state_verified')
    || !hasBoolean(value, 'observed_here')
    || !hasBoolean(value, 'selectable')
  ) return false;
  if (!isThreadActionCapabilities(value.action_capabilities)) return false;
  for (const key of [
    'session_id',
    'parent_thread_id',
    'thread_source',
    'agent_nickname',
    'agent_role',
    'subagent_kind',
  ]) {
    if (!hasString(value, key)) return false;
  }
  if (!hasBoolean(value, 'ephemeral')) return false;
  return value.can_accept_direct_input === null
    || typeof value.can_accept_direct_input === 'boolean';
}

function isInteractionAction(value: unknown): boolean {
  return isRequiredRecord('interaction_action', value)
    && hasString(value, 'id')
    && hasString(value, 'label')
    && isFocusWebWireEnum('interaction_action_style', value.style);
}

function isPendingRequest(value: unknown): value is FocusPendingRequest {
  if (!isRequiredRecord('pending_request', value)) return false;
  if (!isPositiveSafeInteger(value.connection_generation)) return false;
  if (!isNonEmptyTrimmedString(value.response_capability)) return false;
  for (const key of [
    'id',
    'method',
    'thread_id',
    'turn_id',
    'status',
    'title',
    'owner_thread_id',
    'agent_name',
  ]) {
    if (!hasString(value, key)) return false;
  }
  if (!isFocusWebWireEnum('interaction_kind', value.kind)) return false;
  if (!isFocusWireRecord(value.params)) return false;
  return Array.isArray(value.actions) && value.actions.every(isInteractionAction);
}

function isActiveTurnInitiator(value: unknown): value is FocusActiveTurnInitiator {
  if (!isRequiredRecord('active_turn_initiator', value)) return false;
  if (!isFocusWebWireEnum('active_turn_initiator_kind', value.kind)) return false;
  if (!hasString(value, 'binding_id')) return false;
  return value.kind === 'feishu'
    ? isNonEmptyTrimmedString(value.binding_id)
    : value.binding_id === '';
}

function isActiveTurnSetting(value: unknown): value is FocusActiveTurnSetting {
  if (!isRequiredRecord('active_turn_setting', value)) return false;
  if (!hasString(value, 'value')) return false;
  if (!isFocusWebWireEnum('active_turn_setting_source', value.source)) return false;
  return value.source === 'unknown'
    ? value.value === ''
    : isNonEmptyTrimmedString(value.value);
}

function isActiveTurnSettings(value: unknown): boolean {
  if (!isRequiredRecord('active_turn_settings', value)) return false;
  return FOCUS_WEB_RECORDS.active_turn_settings.requiredFields.every(
    (key) => isActiveTurnSetting(value[key]),
  );
}

function isActiveTurnContext(
  value: unknown,
  activeTurnId: unknown,
): value is FocusActiveTurnContext | null {
  if (typeof activeTurnId !== 'string') return false;
  if (value === null) return activeTurnId === '';
  if (!isRequiredRecord('active_turn_context', value)) return false;
  if (!isNonEmptyTrimmedString(value.turn_id) || value.turn_id !== activeTurnId) return false;
  if (!isActiveTurnInitiator(value.initiator)) return false;
  if (
    !Array.isArray(value.feishu_audience)
    || !value.feishu_audience.every(isNonEmptyTrimmedString)
  ) return false;
  return isActiveTurnSettings(value.settings);
}

function canonicalizeTurns(value: unknown[]): FocusThreadSnapshot['turns'] {
  return value.map((turn) => canonicalizeFocusWireChatTurn(
    turn as Record<string, unknown>,
  ));
}

function isMutationUnknown(value: unknown): boolean {
  if (value === null) return true;
  if (!isRequiredRecord('unknown_mutation', value)) return false;
  if (!isNonEmptyTrimmedString(value.operation)) return false;
  if (!isNonEmptyTrimmedString(value.mutation_id)) return false;
  if (!isFocusWebWireEnum('unknown_mutation_durability', value.durability)) return false;
  if (!hasBoolean(value, 'reconciling')) return false;
  if (!optional(
    value,
    'lifecycle_verification',
    (candidate) => candidate === null || isLifecycleVerification(candidate),
  )) return false;
  return isFocusWebWireEnum('lifecycle_operation', value.operation)
    || value.lifecycle_verification == null;
}

export const decodeFocusDocumentRegistration: FocusHttpDecoder<FocusDocumentRegistration> = (
  value,
) => {
  if (!isRequiredRecord('document_registration', value) || !hasCoordinates(value)) return null;
  if (!isNonEmptyTrimmedString(value.client_id)) return null;
  if (!isNonEmptyTrimmedString(value.document_token)) return null;
  if (!isWebDocumentReceipt(value.document_receipt)) return null;
  if (typeof value.duplicate !== 'boolean') return null;
  if (!isNonNegativeSafeInteger(value.intent_generation_floor)) return null;
  if (!isNonEmptyTrimmedString(value.csrf_token)) return null;
  return value as unknown as FocusDocumentRegistration;
};

export const decodeFocusBootstrapResult: FocusHttpDecoder<FocusBootstrapResult> = (value) => {
  if (!isRequiredRecord('bootstrap_result', value) || !hasCoordinates(value)) return null;
  if (value.authenticated !== true) return null;
  if (!isNonEmptyTrimmedString(value.csrf_token)) return null;
  if (!isNonNegativeFiniteNumber(value.expires_at)) return null;
  return value as unknown as FocusBootstrapResult;
};

export const decodeFocusMeta: FocusHttpDecoder<FocusMeta> = (value) => {
  if (!isRequiredRecord('meta', value) || !hasCoordinates(value)) return null;
  const allowedKeys = new Set([
    ...FOCUS_WEB_RECORDS.meta.requiredFields,
    'unknown_lifecycle_mutations',
  ]);
  if (Object.keys(value).some((key) => !allowedKeys.has(key))) return null;
  for (const key of [
    'product',
    'instance',
    'web_display_name',
    'csrf_token',
    'default_working_dir',
  ]) {
    if (!hasString(value, key)) return null;
  }
  if (
    !isNonEmptyTrimmedString(value.product)
    || !isNonEmptyTrimmedString(value.web_display_name)
    || !isNonEmptyTrimmedString(value.csrf_token)
  ) {
    return null;
  }
  if (!Array.isArray(value.models) || !value.models.every(isModel)) return null;
  if (!isWriterProfile(value.writer_profile)) return null;
  if (!isNextTurnSettings(value.next_turn_settings)) return null;
  if (!isStringArray(value.approval_policies)) return null;
  if (
    !Array.isArray(value.permissions_profiles)
    || !value.permissions_profiles.every((profile) => (
      isRequiredRecord('permissions_profile', profile)
      && isNonEmptyTrimmedString(profile.id)
      && hasString(profile, 'label')
    ))
  ) return null;
  if (!isCapabilityMap(value.capabilities)) return null;
  if (
    !optional(
      value,
      'unknown_lifecycle_mutations',
      (items) => Array.isArray(items) && items.every(isUnknownLifecycleMutation),
    )
  ) return null;
  return value as unknown as FocusMeta;
};

export const decodeFocusOperatorStatusResponse = decodeFocusOperatorStatus;

export const decodeFocusBackendResetPreview: FocusHttpDecoder<FocusBackendResetPreview> = (
  value,
) => {
  if (
    !isRequiredRecord('backend_reset_preview', value)
    || !hasExactRequiredFields('backend_reset_preview', value)
  ) return null;
  if (!isNonEmptyTrimmedString(value.instance)) return null;
  if (!isFocusWebWireEnum('backend_reset_status', value.status)) return null;
  if (!hasString(value, 'reason_code') || !hasString(value, 'reason_text')) return null;
  const generation = value.expected_connection_generation;
  if (
    value.status === 'unavailable'
      ? generation !== 0
      : !isPositiveSafeInteger(generation)
  ) return null;
  for (const field of [
    'pending_request_count',
    'running_binding_count',
    'attached_binding_count',
    'active_loaded_thread_count',
    'loaded_thread_count',
  ]) {
    if (!isNonNegativeSafeInteger(value[field])) return null;
  }
  if (!hasBoolean(value, 'runtime_verification_failed')) return null;
  return value as unknown as FocusBackendResetPreview;
};

export function decodeFocusBackendResetResult(
  value: unknown,
  expectedForce: boolean,
): FocusBackendResetResult | null {
  if (typeof expectedForce !== 'boolean') return null;
  if (
    !isRequiredRecord('backend_reset_result', value)
    || !hasExactRequiredFields('backend_reset_result', value)
  ) return null;
  if (value.force !== expectedForce) return null;
  for (const field of [
    'detached_binding_count',
    'interrupted_binding_count',
    'retired_request_count',
    'purged_thread_count',
  ]) {
    if (!isNonNegativeSafeInteger(value[field])) return null;
  }
  if (
    !Array.isArray(value.projection_warnings)
    || !value.projection_warnings.every(isNonEmptyTrimmedString)
  ) return null;
  return value as unknown as FocusBackendResetResult;
}

export const decodeFocusWriterProfileResult: FocusHttpDecoder<FocusWriterProfileResult> = (
  value,
) => {
  if (
    !isRequiredRecord('writer_profile_result', value)
    || !hasExactRequiredFields('writer_profile_result', value)
    || !hasCoordinates(value)
  ) return null;
  if (!isWriterProfile(value.writer_profile)) return null;
  if (!hasBoolean(value, 'scope_changed')) return null;
  if (!hasString(value, 'previous_attachment_scope')) return null;
  if (!hasString(value, 'current_attachment_scope')) return null;
  if (!isPositiveSafeInteger(value.previous_scope_generation)) return null;
  if (!isPositiveSafeInteger(value.current_scope_generation)) return null;
  if (value.current_scope_generation !== value.writer_profile.scope_generation) return null;
  if (value.scope_changed) {
    if (value.current_scope_generation !== value.previous_scope_generation + 1) return null;
  } else if (value.current_scope_generation !== value.previous_scope_generation) return null;
  if (!isFocusWebWireEnum(
    'profile_attachment_disposition',
    value.attachment_scope_disposition,
  )) return null;
  if (!isNonNegativeSafeInteger(value.invalidated_attachment_count)) return null;
  if (!isNonNegativeSafeInteger(value.rebound_attachment_count)) return null;
  const disposition = String(value.attachment_scope_disposition);
  if (!value.scope_changed) {
    if (
      disposition !== 'unchanged'
      || value.previous_attachment_scope !== ''
      || value.invalidated_attachment_count !== 0
      || value.rebound_attachment_count !== 0
    ) return null;
  } else {
    if (
      disposition === 'unchanged'
      || !isNonEmptyTrimmedString(value.previous_attachment_scope)
      || !isNonEmptyTrimmedString(value.current_attachment_scope)
    ) return null;
    if (disposition === 'invalidated' && value.rebound_attachment_count !== 0) return null;
    if (disposition === 'rebound' && value.invalidated_attachment_count !== 0) return null;
  }
  return value as unknown as FocusWriterProfileResult;
};

export const decodeFocusNextTurnSettingsResult:
FocusHttpDecoder<FocusNextTurnSettingsResult> = (value) => {
  if (!isRequiredRecord('next_turn_settings_result', value) || !hasCoordinates(value)) {
    return null;
  }
  if (!hasExactRequiredFields('next_turn_settings_result', value)) return null;
  if (!isNextTurnSettings(value.next_turn_settings)) return null;
  return value as unknown as FocusNextTurnSettingsResult;
};

export const decodeFocusAttachmentUpload: FocusHttpDecoder<FocusAttachmentUpload> = (value) => {
  if (!isRequiredRecord('attachment_upload', value)) return null;
  if (!isNonEmptyTrimmedString(value.file_id)) return null;
  for (const key of ['name', 'media_type', 'url']) {
    if (!hasString(value, key)) return null;
  }
  if (!isNonNegativeSafeInteger(value.size)) return null;
  return value as unknown as FocusAttachmentUpload;
};

export const decodeFocusThreadList: FocusHttpDecoder<FocusThreadList> = (value) => {
  if (!isRequiredRecord('thread_list', value) || !hasCoordinates(value)) return null;
  if (!isFocusWebWireEnum('thread_scope', value.scope)) return null;
  if (!hasBoolean(value, 'archived')) return null;
  if (!isNonNegativeSafeInteger(value.limit) || !hasBoolean(value, 'truncated')) return null;
  if (!Array.isArray(value.threads) || !value.threads.every(isThreadSummary)) return null;
  return value as unknown as FocusThreadList;
};

function isThreadSelectionScope(
  value: unknown,
  threadId: string,
): boolean {
  if (
    !isRequiredRecord('thread_selection_scope', value)
    || !hasExactRequiredFields('thread_selection_scope', value)
  ) return false;
  if (!isWriterProfile(value.writer_profile)) return false;
  if (!isFocusWebWireEnum(
    'selection_attachment_disposition',
    value.attachment_scope_disposition,
  )) return false;
  if (!hasBoolean(value, 'scope_changed')) return false;
  if (!hasString(value, 'previous_attachment_scope')) return false;
  if (!hasString(value, 'current_attachment_scope')) return false;
  if (!isPositiveSafeInteger(value.previous_scope_generation)) return false;
  if (!isPositiveSafeInteger(value.current_scope_generation)) return false;
  if (value.writer_profile.selected_thread_id !== threadId) return false;
  if (value.current_attachment_scope !== `thread:${threadId}`) return false;
  if (value.current_scope_generation !== value.writer_profile.scope_generation) return false;
  if (value.scope_changed) {
    return value.current_scope_generation === value.previous_scope_generation + 1
      && isNonEmptyTrimmedString(value.previous_attachment_scope)
      && value.previous_attachment_scope !== value.current_attachment_scope
      && value.attachment_scope_disposition === 'isolated';
  }
  return value.current_scope_generation === value.previous_scope_generation
    && value.previous_attachment_scope === ''
    && value.attachment_scope_disposition === 'unchanged';
}

export const decodeFocusThreadSnapshot: FocusHttpDecoder<FocusThreadSnapshot> = (value) => {
  if (
    !isRequiredRecord('thread_snapshot', value)
    || !hasExactRequiredFields('thread_snapshot', value)
    || !hasCoordinates(value)
  ) return null;
  if (!isThreadSummary(value.thread)) return null;
  if (!isFocusWireChatTurnWindow(value.turns)) return null;
  if (!hasString(value, 'active_turn_id') || !hasString(value, 'active_turn_status')) return null;
  if (!isActiveTurnContext(value.active_turn_context, value.active_turn_id)) return null;
  if (
    !Array.isArray(value.pending_requests)
    || !value.pending_requests.every(isPendingRequest)
  ) return null;
  if (!Array.isArray(value.tasks) || !value.tasks.every(isFocusWireTaskItem)) return null;
  if (!hasString(value, 'older_turn_cursor') || !hasBoolean(value, 'has_more_turns')) return null;
  if (value.goal !== null && !isFocusWireGoal(value.goal)) return null;
  if (value.token_usage !== null && !isFocusWireTokenUsage(value.token_usage)) return null;
  if (!hasBoolean(value, 'token_usage_available')) return null;
  if (value.token_usage_available && value.token_usage === null) return null;
  if (!hasOwn(value, 'mutation_unknown') || !isMutationUnknown(value.mutation_unknown)) return null;
  if (!isThreadSelectionScope(value.selection_scope, value.thread.id)) return null;
  return {
    ...(value as unknown as FocusThreadSnapshot),
    turns: canonicalizeTurns(value.turns),
  };
};

export const decodeFocusTurnPage: FocusHttpDecoder<FocusTurnPage> = (value) => {
  if (!isRequiredRecord('turn_page', value) || !hasCoordinates(value)) return null;
  if (!isFocusWebWireEnum('turn_items_view', value.items_view)) return null;
  if (!Array.isArray(value.turns)) return null;
  if (value.items_view === 'summary') {
    if (!value.turns.every((turn) => (
      isFocusWireRecord(turn)
      && Object.keys(turn).length === 5
      && isRequiredRecord('summary_prompt', turn)
      && isNonEmptyTrimmedString(turn.id)
      && turn.role === 'user'
      && isNonNegativeSafeInteger(turn.no)
      && typeof turn.text === 'string'
      && typeof turn.title_truncated === 'boolean'
    ))) return null;
  } else if (!isFocusWireChatTurnWindow(value.turns)) return null;
  if (!hasString(value, 'page_cursor')
    || !hasString(value, 'older_turn_cursor')
    || !hasBoolean(value, 'has_more_turns')) return null;
  return {
    ...(value as unknown as FocusTurnPage),
    turns: value.items_view === 'summary'
      ? value.turns as unknown as FocusSummaryPrompt[]
      : canonicalizeTurns(value.turns),
  };
};

function sameToolInspectionLocator(
  left: FocusToolInspectionLocator,
  right: FocusToolInspectionLocator,
): boolean {
  return left.turn_id === right.turn_id
    && left.item_id === right.item_id
    && left.kind === right.kind
    && left.change_index === right.change_index;
}

function hasExactFields(
  value: unknown,
  fields: readonly string[],
): value is Record<string, unknown> {
  return isFocusWireRecord(value)
    && Object.keys(value).length === fields.length
    && fields.every((field) => hasOwn(value, field));
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

function isSafeIntegerOrNull(value: unknown): value is number | null {
  return value === null || Number.isSafeInteger(value);
}

function isTerminalToolStatus(value: unknown): value is string {
  return value === 'completed' || value === 'failed' || value === 'declined';
}

function isCommandExecutionSourceAction(value: unknown): boolean {
  if (!isFocusWireRecord(value) || !isNonEmptyTrimmedString(value.type)) return false;
  if (!hasString(value, 'command')) return false;
  if (value.type === 'read') {
    return hasExactFields(value, ['type', 'command', 'name', 'path'])
      && hasString(value, 'name')
      && hasString(value, 'path');
  }
  if (value.type === 'listFiles') {
    return hasExactFields(value, ['type', 'command', 'path'])
      && isNullableString(value.path);
  }
  if (value.type === 'search') {
    return hasExactFields(value, ['type', 'command', 'query', 'path'])
      && isNullableString(value.query)
      && isNullableString(value.path);
  }
  return value.type === 'unknown' && hasExactFields(value, ['type', 'command']);
}

function isFileChangeSourceKind(value: unknown): boolean {
  if (!isFocusWireRecord(value) || !isNonEmptyTrimmedString(value.type)) return false;
  if (value.type === 'add' || value.type === 'delete') {
    return hasExactFields(value, ['type']);
  }
  return value.type === 'update'
    && hasExactFields(value, ['type', 'movePath'])
    && isNullableString(value.movePath);
}

function isCommandExecutionSource(
  value: unknown,
  expectedItemId: string,
): boolean {
  if (!hasExactFields(value, [
    'type', 'id', 'pluginId', 'scriptPath', 'command', 'cwd', 'processId',
    'source', 'status', 'commandActions', 'aggregatedOutput', 'exitCode', 'durationMs',
  ])) return false;
  return value.type === 'commandExecution'
    && value.id === expectedItemId
    && hasString(value, 'id')
    && isNullableString(value.pluginId)
    && isNullableString(value.scriptPath)
    && typeof value.command === 'string'
    && typeof value.cwd === 'string'
    && isNullableString(value.processId)
    && isNonEmptyTrimmedString(value.source)
    && isTerminalToolStatus(value.status)
    && Array.isArray(value.commandActions)
    && value.commandActions.every(isCommandExecutionSourceAction)
    && isNullableString(value.aggregatedOutput)
    && isSafeIntegerOrNull(value.exitCode)
    && (value.durationMs === null || isNonNegativeSafeInteger(value.durationMs));
}

function isFileChangeSource(
  value: unknown,
  expectedItemId: string,
  changeIndex: number,
): boolean {
  if (!hasExactFields(value, ['type', 'id', 'changes', 'status'])) return false;
  return value.type === 'fileChange'
    && value.id === expectedItemId
    && hasString(value, 'id')
    && isTerminalToolStatus(value.status)
    && Array.isArray(value.changes)
    && changeIndex < value.changes.length
    && value.changes.every((change) => (
      hasExactFields(change, ['path', 'kind', 'diff'])
      && typeof change.path === 'string'
      && isFileChangeSourceKind(change.kind)
      && typeof change.diff === 'string'
    ));
}

function isToolDetailPreview(
  value: unknown,
  expectedLocator: FocusToolInspectionLocator,
): value is FocusThreadToolDetailPreview {
  if (
    !isRequiredRecord('tool_detail_preview', value)
    || !hasExactRequiredFields('tool_detail_preview', value)
    || value.view !== 'preview'
    || !isFocusWireToolCall(value.tool)
    || !value.tool.inspectionLocator
    || !sameToolInspectionLocator(value.tool.inspectionLocator, expectedLocator)
  ) return false;
  return expectedLocator.kind !== 'commandExecution' || value.tool.id === expectedLocator.item_id;
}

function isToolDetailFull(
  value: unknown,
  expectedLocator: FocusToolInspectionLocator,
): value is FocusThreadToolDetailFull {
  if (
    !isRequiredRecord('tool_detail_full', value)
    || !hasExactRequiredFields('tool_detail_full', value)
    || value.view !== 'full'
  ) return false;
  return expectedLocator.kind === 'commandExecution'
    ? isCommandExecutionSource(value.source, expectedLocator.item_id)
    : expectedLocator.change_index !== null
      && isFileChangeSource(
        value.source,
        expectedLocator.item_id,
        expectedLocator.change_index,
      );
}

function isToolDetailPayload(
  value: unknown,
  expectedLocator: FocusToolInspectionLocator,
  expectedView: FocusToolDetailView,
): value is FocusThreadToolDetailPayload {
  return expectedView === 'preview'
    ? isToolDetailPreview(value, expectedLocator)
    : isToolDetailFull(value, expectedLocator);
}

export function decodeFocusThreadToolDetailScanPage(
  value: unknown,
  expectedThreadId: string,
  expectedLocator: FocusToolInspectionLocator,
  expectedCursor: string | null,
  expectedView: FocusToolDetailView,
): FocusThreadToolDetailScanPage | null {
  if (!isNonEmptyTrimmedString(expectedThreadId)) return null;
  if (!isFocusWireToolInspectionLocator(expectedLocator)) return null;
  if (
    !isRequiredRecord('thread_tool_detail_scan_page', value)
    || !hasExactRequiredFields('thread_tool_detail_scan_page', value)
    || !hasCoordinates(value)
  ) return null;
  if (value.thread_id !== expectedThreadId
    || value.turn_id !== expectedLocator.turn_id
    || value.item_id !== expectedLocator.item_id
    || value.kind !== expectedLocator.kind
    || value.change_index !== expectedLocator.change_index
    || value.cursor !== expectedCursor
    || value.view !== expectedView
    || !isFocusWebWireEnum('tool_detail_view', value.view)
    || !isFocusWebWireEnum('tool_detail_scan_status', value.status)
    || !isNonNegativeSafeInteger(value.scanned_items)
    || value.scanned_items > 100
    || (value.next_cursor !== null && !isExactCursor(value.next_cursor))) {
    return null;
  }
  if (value.status === 'scanning') {
    if (value.next_cursor === null || value.next_cursor === value.cursor || value.detail !== null) {
      return null;
    }
  } else if (value.status === 'found') {
    if (value.next_cursor !== null) return null;
    if (!isToolDetailPayload(value.detail, expectedLocator, expectedView)) return null;
  } else {
    if (value.next_cursor !== null || value.detail !== null) return null;
  }
  return value as unknown as FocusThreadToolDetailScanPage;
}

function isExactCursor(value: unknown): value is string {
  return isNonEmptyTrimmedString(value) && Array.from(value).length <= 4096;
}

function isUtf16Boundary(value: string, offset: number): boolean {
  if (offset === 0 || offset === value.length) return true;
  if (offset < 0 || offset > value.length) return false;
  const previous = value.charCodeAt(offset - 1);
  const next = value.charCodeAt(offset);
  return !(previous >= 0xd800 && previous <= 0xdbff
    && next >= 0xdc00 && next <= 0xdfff);
}

function isConversationSearchOccurrence(value: unknown): boolean {
  if (
    !isRequiredRecord('conversation_search_occurrence', value)
    || !hasExactRequiredFields('conversation_search_occurrence', value)
  ) return false;
  if (!isNonEmptyTrimmedString(value.turn_id)) return false;
  if (!isNonEmptyTrimmedString(value.item_id)) return false;
  if (typeof value.snippet !== 'string' || Array.from(value.snippet).length > 1024) return false;
  if (!isExactCursor(value.turn_cursor)) return false;
  const range = value.snippet_match_range;
  if (
    !isRequiredRecord('conversation_search_match_range', range)
    || !hasExactRequiredFields('conversation_search_match_range', range)
    || !isNonNegativeSafeInteger(range.start)
    || !isNonNegativeSafeInteger(range.end)
    || range.start >= range.end
  ) return false;
  return isUtf16Boundary(value.snippet, range.start)
    && isUtf16Boundary(value.snippet, range.end);
}

export function decodeFocusThreadConversationSearchPage(
  value: unknown,
  expectedThreadId: string,
  expectedQuery: string,
  expectedCursor: string | null,
): FocusThreadConversationSearchPage | null {
  if (!isNonEmptyTrimmedString(expectedThreadId)) return null;
  if (
    !isNonEmptyTrimmedString(expectedQuery)
    || Array.from(expectedQuery).length > 256
  ) return null;
  if (expectedCursor !== null && !isExactCursor(expectedCursor)) return null;
  if (
    !isRequiredRecord('thread_conversation_search_page', value)
    || !hasExactRequiredFields('thread_conversation_search_page', value)
    || !hasCoordinates(value)
  ) return null;
  if (value.thread_id !== expectedThreadId || value.query !== expectedQuery) return null;
  if (value.cursor !== expectedCursor) return null;
  if (value.next_cursor !== null && !isExactCursor(value.next_cursor)) return null;
  if (
    !Array.isArray(value.occurrences)
    || value.occurrences.length > 20
    || !value.occurrences.every(isConversationSearchOccurrence)
  ) return null;
  return value as unknown as FocusThreadConversationSearchPage;
}

export const decodeFocusMutationResult: FocusHttpDecoder<FocusMutationResult> = (value) => {
  if (!isRequiredRecord('mutation_result', value) || value.accepted !== true) return null;
  if (!isNonEmptyTrimmedString(value.thread_id)) return null;
  if (!optional(value, 'mutation_id', isNonEmptyTrimmedString)) return null;
  if (!optional(value, 'mode', (candidate) => isFocusWebWireEnum('mutation_mode', candidate))) {
    return null;
  }
  if (!optional(value, 'action', (candidate) => isFocusWebWireEnum('mutation_action', candidate))) {
    return null;
  }
  if (!optional(value, 'turn_id', (candidate) => typeof candidate === 'string')) return null;
  if (!optional(value, 'owner', isOwner)) return null;
  if (!optional(
    value,
    'disposition',
    (candidate) => isFocusWebWireEnum('mutation_disposition', candidate),
  )) return null;
  return value as unknown as FocusMutationResult;
};

export const decodeFocusPromptResultReceipt:
FocusHttpDecoder<FocusPromptResultReceipt> = (value) => {
  if (!isRequiredRecord('prompt_result_receipt', value)) return null;
  const record = FOCUS_WEB_RECORDS.prompt_result_receipt;
  const expectedKeys = new Set([
    ...record.requiredFields,
    ...Object.keys(record.enumFields),
  ]);
  if (Object.keys(value).length !== expectedKeys.size
    || Object.keys(value).some((key) => !expectedKeys.has(key))) return null;
  if (!isNonEmptyTrimmedString(value.thread_id)
    || !isCanonicalWebMutationId(value.mutation_id)
    || !isWebPromptClientUserMessageId(value.client_user_message_id, value.mutation_id)
    || !isFocusWebWireEnum('prompt_result_status', value.status)
    || !isFocusWebWireEnum('prompt_result_mode', value.mode)
    || !isTrimmedString(value.turn_id)
    || !isTrimmedString(value.reason_code)) return null;
  if (value.mode === 'steer' && !isNonEmptyTrimmedString(value.turn_id)) return null;
  if (value.mode === 'start') {
    if (value.status === 'succeeded') {
      if (!isNonEmptyTrimmedString(value.turn_id)) return null;
    } else if (value.turn_id !== '') return null;
  }
  return value as unknown as FocusPromptResultReceipt;
};

export const decodeFocusLifecycleVerificationResult:
FocusHttpDecoder<FocusLifecycleVerificationResult> = (value) => {
  if (!isRequiredRecord('lifecycle_verification_result', value) || !hasCoordinates(value)) {
    return null;
  }
  if (value.accepted !== true || !isNonEmptyTrimmedString(value.thread_id)) return null;
  if (!isNonEmptyTrimmedString(value.mutation_id)) return null;
  if (!optional(
    value,
    'status',
    (candidate) => isFocusWebWireEnum('lifecycle_verification_status', candidate),
  )) return null;
  if (!optional(
    value,
    'operation',
    (candidate) => isFocusWebWireEnum('lifecycle_operation', candidate),
  )) return null;
  if (!optional(value, 'verification', isLifecycleVerification)) return null;
  if (value.status === 'already_reconciled') {
    if (value.operation !== undefined || value.verification !== undefined) return null;
  } else if (!value.operation || !value.verification) return null;
  return value as unknown as FocusLifecycleVerificationResult;
};

export const decodeFocusRenameResult: FocusHttpDecoder<FocusRenameResult> = (value) => {
  if (!isRequiredRecord('rename_result', value) || !hasBoolean(value, 'accepted')) return null;
  if (!isNonEmptyTrimmedString(value.thread_id) || !hasString(value, 'name')) return null;
  return value as unknown as FocusRenameResult;
};

export const decodeFocusGoalResult: FocusHttpDecoder<FocusGoalResult> = (value) => {
  if (!isRequiredRecord('goal_result', value) || !hasCoordinates(value)) return null;
  if (!isNonEmptyTrimmedString(value.thread_id)) return null;
  if (value.goal !== null && !isFocusWireGoal(value.goal)) return null;
  if (!optional(value, 'cleared', (candidate) => typeof candidate === 'boolean')) return null;
  return value as unknown as FocusGoalResult;
};

export const decodeFocusLifecycleResult: FocusHttpDecoder<FocusLifecycleResult> = (value) => {
  if (!isRequiredRecord('lifecycle_result', value)) return null;
  if (!isNonEmptyTrimmedString(value.thread_id)) return null;
  if (!isFocusWebWireEnum('lifecycle_upstream_outcome', value.upstream_outcome)) return null;
  if (!optional(value, 'mutation_id', isNonEmptyTrimmedString)) return null;
  if (value.upstream_outcome === 'unknown' && !isNonEmptyTrimmedString(value.mutation_id)) {
    return null;
  }
  if (!isFocusWebWireEnum('lifecycle_cleanup', value.focus_cleanup)) return null;
  if (!isStringArray(value.cleanup_errors)) return null;
  for (const key of ['thread_title', 'working_dir', 'upstream_error', 'outcome_detail']) {
    if (!optional(value, key, (candidate) => typeof candidate === 'string')) return null;
  }
  if (!optional(value, 'cleared_binding_ids', isStringArray)) return null;
  return value as unknown as FocusLifecycleResult;
};

export const decodeFocusRequestResponseResult:
FocusHttpDecoder<FocusRequestResponseResult> = (value) => {
  if (!isRequiredRecord('request_response_result', value) || value.accepted !== true) return null;
  return value as unknown as FocusRequestResponseResult;
};

export const decodeFocusGatewayErrorBody: FocusHttpDecoder<FocusGatewayErrorBody> = (value) => {
  if (!isRequiredRecord('gateway_error_body', value)
    || Object.keys(value).length !== 1
    || !isFocusWireRecord(value.error)) return null;
  const errorKeys = new Set(['code', 'message', 'details']);
  if (Object.keys(value.error).some((key) => !errorKeys.has(key))) return null;
  if (!isNonEmptyTrimmedString(value.error.code)
    || !isNonEmptyTrimmedString(value.error.message)
    || !optional(value.error, 'details', isFocusWireRecord)) return null;
  return value as unknown as FocusGatewayErrorBody;
};
