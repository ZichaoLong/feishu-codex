import type { ChatTurn, TaskItem, ToolCall, TurnAttachment, TurnBlock } from '../types';
import type {
  FocusGoal,
  FocusOperatorStatus,
  FocusOperatorWarning,
  FocusProjectionEvent,
  FocusRuntimeNoticeDetail,
  FocusStreamDelta,
  FocusToolInspectionLocator,
  FocusThreadDeltaDetail,
  FocusTokenBreakdown,
  FocusTokenUsage,
} from './types';
import {
  FOCUS_WEB_RECORDS,
  FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES,
  FOCUS_WEB_THREAD_SCOPED_EVENT_TYPE_SET as THREAD_SCOPED_EVENT_TYPES,
  hasFocusWebRequiredFields,
  isFocusWebEventType,
  isFocusWebWireEnum,
  type FocusWebWireRecordName,
} from './focusWire.generated';
import {
  TOOL_OUTPUT_MAX_VISIBLE_CHARS,
  toolOutputCodePointLength,
  toolOutputWindowFitsAggregate,
} from './toolOutputPresentation';

export function isFocusWireRecord(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function isRequiredRecord(
  name: FocusWebWireRecordName,
  value: unknown,
): value is Record<string, unknown> {
  return isFocusWireRecord(value) && hasFocusWebRequiredFields(name, value);
}

function isTrimmedString(value: unknown, allowEmpty: boolean): value is string {
  if (typeof value !== 'string' || value.trim() !== value) return false;
  return allowEmpty || value.length > 0;
}

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isFiniteNumber(value: unknown, minimum = 0): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= minimum;
}

function isNonNegativeSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}

function isSafeInteger(value: unknown): value is number {
  return Number.isSafeInteger(value);
}

function isNullableNonNegativeSafeInteger(value: unknown): value is number | null {
  return value === null || isNonNegativeSafeInteger(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function optionalProperty(
  value: Record<string, unknown>,
  key: string,
  predicate: (candidate: unknown) => boolean,
): boolean {
  return !hasOwn(value, key) || predicate(value[key]);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string';
}

function isToolMedia(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  if (!['image', 'video', 'audio'].includes(String(value.kind))) return false;
  if (typeof value.url !== 'string') return false;
  for (const key of ['path', 'mimeType', 'dimensions', 'fileId']) {
    if (!optionalProperty(value, key, (item) => typeof item === 'string')) return false;
  }
  return optionalProperty(value, 'bytes', isNonNegativeSafeInteger);
}

function isDiffViewLine(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  if (!['add', 'del', 'context', 'hunk'].includes(String(value.type))) return false;
  if (typeof value.text !== 'string') return false;
  if (!optionalProperty(value, 'oldNo', isNonNegativeSafeInteger)) return false;
  return optionalProperty(value, 'newNo', isNonNegativeSafeInteger);
}

function toolOutputOmissionMarker(omittedChars: number): string {
  return `[Focus Web omitted ${omittedChars} characters of tool output; showing a bounded head and tail.]`;
}

function hasTrustedOmissionBoundary(
  lines: readonly string[],
  omittedChars: unknown,
  headLineCount: unknown,
  truncated: unknown,
): boolean {
  if (truncated !== true) {
    return omittedChars === undefined && headLineCount === undefined;
  }
  if (!isNonNegativeSafeInteger(omittedChars) || omittedChars <= 0) return false;
  if (!isNonNegativeSafeInteger(headLineCount)) return false;
  if (headLineCount === 0) return lines.length === 0;
  return headLineCount < lines.length
    && lines[headLineCount] === toolOutputOmissionMarker(omittedChars);
}

function isToolDiff(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  if (!optionalProperty(value, 'path', (item) => typeof item === 'string')) return false;
  if (!Array.isArray(value.lines) || !value.lines.every(isDiffViewLine)) return false;
  const omitted = value.omittedChars;
  const boundary = value.omissionLineIndex;
  if (omitted === undefined && boundary === undefined) return true;
  if (!isNonNegativeSafeInteger(omitted) || omitted <= 0) return false;
  if (!isNonNegativeSafeInteger(boundary)) return false;
  if (boundary === 0) return value.lines.length === 0;
  const boundaryLine = value.lines[boundary];
  return isFocusWireRecord(boundaryLine)
    && boundaryLine.text === toolOutputOmissionMarker(omitted);
}

function isCommandExecutionAction(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  for (const key of ['type', 'command', 'name', 'path', 'query']) {
    if (!optionalProperty(value, key, isNullableString)) return false;
  }
  return true;
}

function isCommandExecutionFacts(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  for (const key of ['cwd', 'processId', 'source']) {
    if (!optionalProperty(value, key, isNullableString)) return false;
  }
  if (!optionalProperty(value, 'exitCode', (item) => item === null || isSafeInteger(item))) {
    return false;
  }
  return optionalProperty(
    value,
    'commandActions',
    (items) => Array.isArray(items) && items.every(isCommandExecutionAction),
  );
}

export function isFocusWireToolInspectionLocator(
  value: unknown,
): value is FocusToolInspectionLocator {
  if (!isRequiredRecord('tool_inspection_locator', value)) return false;
  if (Object.keys(value).length !== 4) return false;
  if (!isTrimmedString(value.turn_id, false) || !isTrimmedString(value.item_id, false)) {
    return false;
  }
  if (!isFocusWebWireEnum('thread_tool_kind', value.kind)) return false;
  if (value.kind === 'commandExecution') return value.change_index === null;
  return isNonNegativeSafeInteger(value.change_index)
    && value.change_index <= 0xffff_ffff;
}

function isToolCall(value: unknown): value is ToolCall {
  if (!isFocusWireRecord(value)) return false;
  if (!isTrimmedString(value.id, false)) return false;
  if (typeof value.name !== 'string' || typeof value.arg !== 'string') return false;
  if (!['ok', 'running', 'error'].includes(String(value.status))) return false;
  if (!optionalProperty(value, 'timing', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'output', isStringArray)) return false;
  if (!optionalProperty(value, 'outputOmittedChars', isNonNegativeSafeInteger)) return false;
  if (!optionalProperty(value, 'outputHeadLineCount', isNonNegativeSafeInteger)) return false;
  if (!optionalProperty(value, 'outputTruncated', (item) => typeof item === 'boolean')) return false;
  if (value.outputTruncated === true && !hasOwn(value, 'output')) return false;
  const outputLines = Array.isArray(value.output) ? value.output as string[] : [];
  if (!hasTrustedOmissionBoundary(
    outputLines,
    value.outputOmittedChars,
    value.outputHeadLineCount,
    value.outputTruncated,
  )) return false;
  const markerAllowance = value.outputTruncated === true
    ? toolOutputCodePointLength(toolOutputOmissionMarker(Number(value.outputOmittedChars))) + 2
    : 0;
  if (
    toolOutputCodePointLength(outputLines.join('\n'))
    > TOOL_OUTPUT_MAX_VISIBLE_CHARS + markerAllowance
  ) return false;
  if (!optionalProperty(value, 'defaultExpanded', (item) => typeof item === 'boolean')) return false;
  if (!optionalProperty(value, 'planPath', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'media', isToolMedia)) return false;
  if (!optionalProperty(value, 'diff', isToolDiff)) return false;
  if (isFocusWireRecord(value.diff)) {
    const outputHasOmission = value.outputOmittedChars !== undefined
      || value.outputHeadLineCount !== undefined;
    const diffHasOmission = value.diff.omittedChars !== undefined
      || value.diff.omissionLineIndex !== undefined;
    if (outputHasOmission !== diffHasOmission) return false;
    if (
      outputHasOmission
      && (
        value.diff.omittedChars !== value.outputOmittedChars
        || value.diff.omissionLineIndex !== value.outputHeadLineCount
      )
    ) return false;
  }
  if (!optionalProperty(value, 'commandExecution', isCommandExecutionFacts)) return false;
  if (!optionalProperty(
    value,
    'inspectionLocator',
    isFocusWireToolInspectionLocator,
  )) return false;
  return true;
}

export function isFocusWireToolCall(value: unknown): value is ToolCall {
  return isToolCall(value);
}

function isTurnBlock(value: unknown): value is TurnBlock {
  if (!isFocusWireRecord(value)) return false;
  if (value.kind === 'text') {
    return typeof value.text === 'string'
      && optionalProperty(value, 'itemId', (item) => typeof item === 'string');
  }
  if (value.kind === 'thinking') {
    return typeof value.thinking === 'string'
      && optionalProperty(value, 'itemId', (item) => typeof item === 'string');
  }
  return value.kind === 'tool' && isToolCall(value.tool);
}

function isTurnAttachment(value: unknown): value is TurnAttachment {
  if (!isFocusWireRecord(value)) return false;
  if (!['image', 'video', 'file'].includes(String(value.kind))) return false;
  if (typeof value.url !== 'string') return false;
  if (!optionalProperty(value, 'fileId', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'name', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'mediaType', (item) => typeof item === 'string')) return false;
  return optionalProperty(value, 'size', isNonNegativeSafeInteger);
}

function isApprovalDiffLine(value: unknown): boolean {
  return isFocusWireRecord(value)
    && ['ctx', 'add', 'rem'].includes(String(value.kind))
    && typeof value.gutter === 'string'
    && typeof value.text === 'string';
}

function isApprovalBlock(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  switch (value.kind) {
    case 'diff':
      return typeof value.path === 'string'
        && Array.isArray(value.diff)
        && value.diff.every(isApprovalDiffLine);
    case 'shell':
      return typeof value.command === 'string'
        && optionalProperty(value, 'cwd', (item) => typeof item === 'string')
        && optionalProperty(value, 'danger', (item) => typeof item === 'string');
    case 'file':
      return typeof value.path === 'string'
        && typeof value.content === 'string'
        && optionalProperty(value, 'language', (item) => typeof item === 'string');
    case 'fileop':
      return typeof value.op === 'string'
        && typeof value.path === 'string'
        && optionalProperty(value, 'detail', (item) => typeof item === 'string');
    case 'url':
      return typeof value.url === 'string'
        && optionalProperty(value, 'method', (item) => typeof item === 'string');
    case 'search':
      return typeof value.query === 'string'
        && optionalProperty(value, 'scope', (item) => typeof item === 'string');
    case 'invocation':
      return typeof value.kind2 === 'string'
        && typeof value.name === 'string'
        && optionalProperty(value, 'description', (item) => typeof item === 'string');
    case 'todo':
      return Array.isArray(value.items)
        && value.items.every((item) => (
          isFocusWireRecord(item)
          && typeof item.title === 'string'
          && typeof item.status === 'string'
        ));
    case 'plan_review':
      return typeof value.plan === 'string'
        && optionalProperty(value, 'path', (item) => typeof item === 'string')
        && optionalProperty(
          value,
          'options',
          (items) => Array.isArray(items) && items.every((item) => (
            isFocusWireRecord(item)
            && typeof item.label === 'string'
            && optionalProperty(item, 'description', (entry) => typeof entry === 'string')
          )),
        );
    case 'generic':
      return typeof value.summary === 'string';
    default:
      return false;
  }
}

function isCompaction(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  if (!optionalProperty(
    value,
    'trigger',
    (item) => item === 'manual' || item === 'auto',
  )) return false;
  if (!optionalProperty(value, 'tokensBefore', isNonNegativeSafeInteger)) return false;
  return optionalProperty(value, 'tokensAfter', isNonNegativeSafeInteger);
}

function isSkillActivation(value: unknown): boolean {
  return isFocusWireRecord(value)
    && isTrimmedString(value.name, false)
    && optionalProperty(value, 'args', (item) => typeof item === 'string');
}

function isPluginCommand(value: unknown): boolean {
  return isFocusWireRecord(value)
    && isTrimmedString(value.pluginId, false)
    && isTrimmedString(value.commandName, false)
    && optionalProperty(value, 'args', (item) => typeof item === 'string');
}

function isCronTurnData(value: unknown): boolean {
  if (!isFocusWireRecord(value)) return false;
  for (const key of ['jobId', 'cron']) {
    if (!optionalProperty(value, key, (item) => typeof item === 'string')) return false;
  }
  for (const key of ['recurring', 'stale']) {
    if (!optionalProperty(value, key, (item) => typeof item === 'boolean')) return false;
  }
  if (!optionalProperty(value, 'coalescedCount', isNonNegativeSafeInteger)) return false;
  return optionalProperty(value, 'missedCount', isNonNegativeSafeInteger);
}

export function isFocusWireChatTurn(value: unknown): value is Record<string, unknown> {
  if (!isFocusWireRecord(value)) return false;
  if (!isTrimmedString(value.id, false)) return false;
  if (!['user', 'assistant', 'compaction', 'cron'].includes(String(value.role))) return false;
  if (!isNonNegativeSafeInteger(value.no) || typeof value.text !== 'string') return false;
  if (!optionalProperty(value, 'thinking', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'status', (item) => typeof item === 'string')) return false;
  // The current Focus projector emits explicit nulls while these timestamps
  // are not available; both values are display-only and are treated as absent.
  if (!optionalProperty(
    value,
    'createdAt',
    (item) => item === null || typeof item === 'string',
  )) return false;
  if (!optionalProperty(
    value,
    'durationMs',
    (item) => item === null || isFiniteNumber(item),
  )) return false;
  if (!optionalProperty(value, 'approvalId', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(
    value,
    'tools',
    (items) => Array.isArray(items) && items.every(isToolCall),
  )) return false;
  if (!optionalProperty(
    value,
    'blocks',
    (items) => Array.isArray(items) && items.every(isTurnBlock),
  )) return false;
  if (!optionalProperty(
    value,
    'attachments',
    (items) => Array.isArray(items) && items.every(isTurnAttachment),
  )) return false;
  if (!optionalProperty(value, 'approval', isApprovalBlock)) return false;
  if (!optionalProperty(value, 'compaction', isCompaction)) return false;
  if (!optionalProperty(value, 'skillActivation', isSkillActivation)) return false;
  if (!optionalProperty(value, 'pluginCommand', isPluginCommand)) return false;
  if (!optionalProperty(value, 'cron', isCronTurnData)) return false;
  return true;
}

export function isFocusWireChatTurnWindow(value: unknown): value is Record<string, unknown>[] {
  if (!Array.isArray(value) || !value.every(isFocusWireChatTurn)) return false;
  return toolOutputWindowFitsAggregate(value as unknown as ChatTurn[]);
}

export function isFocusWireTaskItem(value: unknown): value is TaskItem {
  if (!isRequiredRecord('task_item', value)) return false;
  if (!isTrimmedString(value.id, false)) return false;
  if (typeof value.name !== 'string' || typeof value.kind !== 'string') return false;
  if (!isFocusWebWireEnum('task_state', value.state) || typeof value.timing !== 'string') {
    return false;
  }
  for (const key of ['meta', 'prompt', 'parentToolCallId', 'threadId', 'parentThreadId']) {
    if (!optionalProperty(value, key, (item) => typeof item === 'string')) return false;
  }
  for (const key of ['output', 'progress', 'result', 'metadata']) {
    if (!optionalProperty(value, key, isStringArray)) return false;
  }
  for (const key of ['runInBackground', 'ephemeral', 'resultAvailable']) {
    if (!optionalProperty(value, key, (item) => typeof item === 'boolean')) return false;
  }
  if (!optionalProperty(
    value,
    'canAcceptDirectInput',
    (item) => item === null || typeof item === 'boolean',
  )) return false;
  return optionalProperty(
    value,
    'executionState',
    (item) => isFocusWebWireEnum('task_execution_state', item),
  );
}

export function isFocusWireGoal(value: unknown): value is FocusGoal {
  if (!isRequiredRecord('goal', value) || !isRequiredRecord('goal_budget', value.budget)) {
    return false;
  }
  if (!isTrimmedString(value.goal_id, false) || typeof value.objective !== 'string') return false;
  if (!isFocusWebWireEnum('goal_status', value.status)) return false;
  if (!isNonNegativeSafeInteger(value.tokens_used)) return false;
  if (!isNonNegativeSafeInteger(value.wall_clock_ms)) return false;
  const budget = value.budget;
  for (const key of [
    'token_budget',
    'remaining_tokens',
    'turn_budget',
    'remaining_turns',
    'wall_clock_budget_ms',
    'remaining_wall_clock_ms',
  ]) {
    if (!hasOwn(budget, key) || !isNullableNonNegativeSafeInteger(budget[key])) return false;
  }
  return typeof budget.over_budget === 'boolean';
}

function isTokenBreakdown(value: unknown): value is FocusTokenBreakdown {
  if (!isRequiredRecord('token_breakdown', value)) return false;
  for (const key of [
    'totalTokens',
    'inputTokens',
    'cachedInputTokens',
    'outputTokens',
    'reasoningOutputTokens',
  ]) {
    if (!optionalProperty(value, key, isNonNegativeSafeInteger)) return false;
  }
  return true;
}

export function isFocusWireTokenUsage(value: unknown): value is FocusTokenUsage {
  if (!isRequiredRecord('token_usage', value)) return false;
  if (!optionalProperty(value, 'total', isTokenBreakdown)) return false;
  if (!optionalProperty(value, 'last', isTokenBreakdown)) return false;
  return optionalProperty(
    value,
    'modelContextWindow',
    (item) => item === null || isNonNegativeSafeInteger(item),
  );
}

function isStreamDelta(value: unknown): value is FocusStreamDelta {
  if (!isRequiredRecord('stream_delta', value)) return false;
  if (!isTrimmedString(value.turn_id, false) || !isTrimmedString(value.item_id, false)) return false;
  if (!isFocusWebWireEnum('stream_delta_kind', value.kind)) return false;
  if (typeof value.delta !== 'string') return false;
  return optionalProperty(value, 'tool_name', (item) => typeof item === 'string');
}

function isThreadStatus(value: unknown): boolean {
  if (!isFocusWireRecord(value) || !isTrimmedString(value.type, false)) return false;
  return optionalProperty(
    value,
    'activeFlags',
    (items) => Array.isArray(items) && items.every((item) => isTrimmedString(item, false)),
  );
}

function isThreadDeltaDetail(value: unknown): value is Record<string, unknown> {
  if (!isRequiredRecord('thread_delta_detail', value)) return false;
  if (!isTrimmedString(value.method, false)) return false;
  if (!optionalProperty(value, 'thread_name', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'active_turn_id', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'active_turn_status', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'turn_id', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'token_usage_durable', (item) => typeof item === 'boolean')) return false;
  if (!optionalProperty(value, 'plan_replay', (item) => typeof item === 'string')) return false;
  if (!optionalProperty(value, 'stream_delta', isStreamDelta)) return false;
  if (!optionalProperty(value, 'thread_status', isThreadStatus)) return false;
  if (!optionalProperty(
    value,
    'turns',
    isFocusWireChatTurnWindow,
  )) return false;
  if (!optionalProperty(
    value,
    'tasks',
    (items) => Array.isArray(items) && items.every(isFocusWireTaskItem),
  )) return false;
  if (hasOwn(value, 'goal') && value.goal !== null && !isFocusWireGoal(value.goal)) return false;
  return optionalProperty(value, 'token_usage', isFocusWireTokenUsage);
}

export function canonicalizeFocusWireChatTurn(value: Record<string, unknown>): ChatTurn {
  const turn = { ...value };
  if (turn.createdAt === null) delete turn.createdAt;
  if (turn.durationMs === null) delete turn.durationMs;
  return turn as unknown as ChatTurn;
}

function canonicalizeThreadDeltaDetail(
  value: Record<string, unknown>,
): Record<string, unknown> {
  const detail = { ...value };
  if (Array.isArray(value.turns)) {
    detail.turns = value.turns.map((turn) => (
      canonicalizeFocusWireChatTurn(turn as Record<string, unknown>)
    ));
  }
  return detail;
}

const SETTINGS_CHANGED_EVENT_KEYS = new Set([
  'type',
  'runtime_epoch',
  'revision',
  'thread_id',
  'reason',
  'occurred_at',
]);

function isExactRequiredRecord(
  name: FocusWebWireRecordName,
  value: unknown,
): value is Record<string, unknown> {
  if (!isRequiredRecord(name, value)) return false;
  const record = FOCUS_WEB_RECORDS[name];
  const expected = new Set<string>([
    ...record.requiredFields,
    ...Object.keys(record.enumFields),
  ]);
  const actual = Object.keys(value);
  return actual.length === expected.size && actual.every((key) => expected.has(key));
}

function isBoundedRuntimeNoticeText(value: unknown): value is string {
  if (typeof value !== 'string') return false;
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xD800 && unit <= 0xDBFF) {
      if (index + 1 >= value.length) return false;
      const next = value.charCodeAt(index + 1);
      if (next < 0xDC00 || next > 0xDFFF) return false;
      index += 1;
    } else if (unit >= 0xDC00 && unit <= 0xDFFF) return false;
  }
  return new TextEncoder().encode(value).byteLength
    <= FOCUS_WEB_RUNTIME_NOTICE_FIELD_LIMIT_BYTES;
}

function isBoundedRuntimeNoticeIdentifier(value: unknown, allowEmpty: boolean): value is string {
  return isBoundedRuntimeNoticeText(value) && isTrimmedString(value, allowEmpty);
}

export function decodeFocusRuntimeNoticeDetail(
  value: unknown,
): FocusRuntimeNoticeDetail | null {
  if (!isFocusWireRecord(value)
    || !isFocusWebWireEnum('runtime_notice_method', value.method)) return null;
  if (value.method === 'warning') {
    return isExactRequiredRecord('runtime_warning_notice_detail', value)
      && isBoundedRuntimeNoticeText(value.message)
      ? { method: 'warning', message: value.message }
      : null;
  }
  if (!isExactRequiredRecord('runtime_error_notice_detail', value)
    || !isBoundedRuntimeNoticeText(value.message)
    || !isBoundedRuntimeNoticeText(value.additional_details)
    || typeof value.will_retry !== 'boolean'
    || !isBoundedRuntimeNoticeIdentifier(value.turn_id, false)) return null;
  return {
    method: 'error',
    message: value.message,
    additional_details: value.additional_details,
    will_retry: value.will_retry,
    turn_id: value.turn_id,
  };
}

/**
 * Produce the typed command consumed by the projection owner. The wire
 * validator above remains the sole authority for every nested field.
 */
export function decodeFocusThreadDeltaDetail(
  value: unknown,
): FocusThreadDeltaDetail | null {
  if (!isThreadDeltaDetail(value)) return null;
  return canonicalizeThreadDeltaDetail(value) as unknown as FocusThreadDeltaDetail;
}

function isKnownEventPayload(value: Record<string, unknown>): boolean {
  if (THREAD_SCOPED_EVENT_TYPES.has(String(value.type))) {
    if (!isTrimmedString(value.thread_id, false)) return false;
  }
  switch (value.type) {
    case 'thread_delta':
      return isThreadDeltaDetail(value.detail);
    case 'projection_invalidated':
      return isTrimmedString(value.reason, false)
        && isFocusWireRecord(value.detail)
        && value.detail.reload === true;
    case 'mutation_reconciled':
      return isFocusWireRecord(value.detail)
        && isTrimmedString(value.detail.mutation_id, false)
        && isTrimmedString(value.detail.operation, false)
        && isFocusWebWireEnum('mutation_disposition', value.detail.disposition);
    case 'mutation_unknown':
    case 'mutation_verified':
      return isFocusWireRecord(value.detail)
        && isTrimmedString(value.detail.mutation_id, false)
        && isTrimmedString(value.detail.operation, false);
    case 'settings_changed':
      return !hasOwn(value, 'detail')
        && Object.keys(value).every((key) => SETTINGS_CHANGED_EVENT_KEYS.has(key));
    case 'runtime_notice': {
      const detail = decodeFocusRuntimeNoticeDetail(value.detail);
      if (!detail) return false;
      if (detail.method === 'error') {
        return isBoundedRuntimeNoticeIdentifier(value.thread_id, false);
      }
      return !hasOwn(value, 'thread_id')
        || isBoundedRuntimeNoticeIdentifier(value.thread_id, true);
    }
    case 'backend_disconnected':
    case 'hello':
    case 'owner_changed':
    case 'pending_request_changed':
    case 'profile_changed':
    case 'runtime_changed':
    case 'session_expired':
    case 'thread_invalidated':
      return !hasOwn(value, 'detail');
    default:
      return false;
  }
}

/**
 * Decode the untrusted WebSocket envelope before it reaches projection state.
 * Unknown or malformed events fail closed and trigger the transport's
 * authoritative projection reload path.
 */
export function decodeFocusProjectionEvent(value: unknown): FocusProjectionEvent | null {
  if (!isRequiredRecord('projection_event', value)) return null;
  const eventType = value.type;
  if (!isFocusWebEventType(eventType)) return null;
  if (!isTrimmedString(value.runtime_epoch, false)) return null;
  if (!Number.isSafeInteger(value.revision) || Number(value.revision) < 0) return null;
  if ('thread_id' in value && !isTrimmedString(value.thread_id, true)) return null;
  if ('reason' in value && !isTrimmedString(value.reason, true)) return null;
  if (
    'occurred_at' in value
    && (typeof value.occurred_at !== 'number'
      || !Number.isFinite(value.occurred_at)
      || value.occurred_at < 0)
  ) return null;
  if ('detail' in value && !isFocusWireRecord(value.detail)) return null;
  if (!isKnownEventPayload(value)) return null;

  const event: FocusProjectionEvent = {
    type: eventType,
    runtime_epoch: value.runtime_epoch,
    revision: Number(value.revision),
  };
  if ('thread_id' in value) event.thread_id = value.thread_id as string;
  if ('reason' in value) event.reason = value.reason as string;
  if ('occurred_at' in value) event.occurred_at = value.occurred_at as number;
  if ('detail' in value) {
    const detail = value.detail as Record<string, unknown>;
    event.detail = eventType === 'thread_delta'
      ? canonicalizeThreadDeltaDetail(detail)
      : { ...detail };
  }
  return event;
}

function isOperatorWarning(value: unknown): value is FocusOperatorWarning {
  if (!isRequiredRecord('operator_warning', value)) return false;
  if (!isTrimmedString(value.code, false) || !isTrimmedString(value.source, false)) return false;
  if (
    typeof value.message !== 'string'
    || !isFocusWebWireEnum('warning_severity', value.severity)
    || !isFocusWebWireEnum('warning_attention', value.attention)
  ) {
    return false;
  }
  if (!isFiniteNumber(value.first_seen_at) || !isFiniteNumber(value.last_seen_at)) return false;
  if (!isNonNegativeSafeInteger(value.occurrences) || value.occurrences < 1) return false;
  return isFocusWireRecord(value.details);
}

/** Decode the independent, untrusted operator projection before publishing it. */
export function decodeFocusOperatorStatus(value: unknown): FocusOperatorStatus | null {
  if (!isRequiredRecord('operator_status', value)) return null;
  if (!isTrimmedString(value.status, false)) return null;
  if (!isFiniteNumber(value.observed_at) || !isFiniteNumber(value.poll_after_seconds)) return null;
  if (!Array.isArray(value.warnings) || !value.warnings.every(isOperatorWarning)) return null;
  if (!isFocusWireRecord(value.runtime_loop)) return null;
  return {
    status: value.status,
    observed_at: value.observed_at,
    poll_after_seconds: value.poll_after_seconds,
    warnings: value.warnings,
    runtime_loop: { ...value.runtime_loop },
  };
}
