import { afterEach, beforeEach, vi } from 'vitest';
import type { FocusWebApiPort } from '../../src/focus/api';
import type {
  FocusLifecycleVerificationResult,
  FocusMeta,
  FocusNextTurnSettings,
  FocusLifecycleResult,
  FocusProjectionEvent,
  FocusOperatorStatus,
  FocusThreadConversationSearchPage,
  FocusThreadSnapshot,
  FocusThreadSummary,
  FocusThreadToolDetailScanPage,
  FocusToolDetailView,
  FocusToolInspectionLocator,
  FocusTurnPage,
} from '../../src/focus/types';
import type { FocusApiError } from '../../src/focus/types';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear() { values.clear(); },
    getItem(key: string) { return values.get(key) ?? null; },
    key(index: number) { return [...values.keys()].at(index) ?? null; },
    removeItem(key: string) { values.delete(key); },
    setItem(key: string, value: string) { values.set(key, value); },
  };
}

const capabilities = {
  prompt: true,
  new_thread: true,
  interrupt: true,
  approvals: true,
  questions: true,
  markdown: true,
  katex: true,
  mermaid: true,
  file_preview: false,
  terminal: false,
  attachments: true,
  prompt_queue: false,
  durable_event_cursor: false,
  bounded_history: true,
  history_search: false,
  tool_detail: false,
  steer: true,
};

function owner(relation: 'none' | 'self' | 'other' = 'none') {
  return {
    kind: relation === 'none' ? 'none' : 'web',
    holder_id: relation === 'none' ? '' : 'web:tab',
    relation,
    label: relation === 'other' ? 'Another browser' : relation === 'self' ? 'This browser' : 'No active writer',
  } as const;
}

export function thread(relation: 'none' | 'self' | 'other' = 'none'): FocusThreadSummary {
  return {
    id: 'thread-1',
    title: 'Demo',
    name: 'Demo',
    preview: 'hello',
    cwd: '/work',
    created_at: 1,
    updated_at: 2,
    source: 'appServer',
    status: 'idle',
    active_flags: [],
    model_provider: '',
    service_name: 'focus',
    owner: owner(relation),
    pending_interaction: 'none',
    loaded_instance: '',
    loaded_state_verified: true,
    observed_here: false,
    selectable: true,
    unavailable_reason: '',
    history_mode: 'paginated',
    action_capabilities: {
      rename: true,
      archive: true,
      unarchive: false,
      delete: false,
      compact: true,
      fork: false,
      export: false,
      review: true,
      goal: true,
    },
  };
}

export function snapshot(
  relation: 'none' | 'self' | 'other' = 'none',
  selectedThreadId = 'thread-1',
  scopeGeneration = 2,
): FocusThreadSnapshot {
  return {
    runtime_epoch: 'epoch-1',
    revision: 0,
    thread: { ...thread(relation), id: selectedThreadId },
    turns: [],
    active_turn_id: '',
    active_turn_status: '',
    pending_requests: [],
    tasks: [],
    older_turn_cursor: '',
    has_more_turns: false,
    goal: null,
    token_usage: null,
    token_usage_available: false,
    mutation_unknown: null,
    selection_scope: {
      writer_profile: {
        selected_thread_id: selectedThreadId,
        working_dir: '/work',
        scope_generation: scopeGeneration,
      },
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: `thread:${selectedThreadId}`,
      previous_scope_generation: scopeGeneration,
      current_scope_generation: scopeGeneration,
      attachment_scope_disposition: 'unchanged',
    },
  };
}

export class FakeApi implements FocusWebApiPort {
  readonly clientId: string;
  readonly documentReceipt = 'a'.repeat(64);
  intentGenerationFloor = 0;
  handlers: Parameters<FocusWebApiPort['connectEvents']>[0] | null = null;
  metaCalls = 0;
  operatorStatusCalls = 0;
  listCalls = 0;
  listOptions: Array<Record<string, unknown>> = [];
  readCalls = 0;
  olderTurnCalls: Array<{
    threadId: string;
    cursor: string;
    itemsView: 'summary' | 'full';
    turnLimit: number;
  }> = [];
  toolDetailCalls: Array<{
    threadId: string;
    locator: FocusToolInspectionLocator;
    view: FocusToolDetailView;
  }> = [];
  conversationSearchCalls: Array<{
    threadId: string;
    query: string;
    cursor: string | null;
  }> = [];
  startThreadCalls: Array<Record<string, unknown>> = [];
  submitPromptCalls: Array<Record<string, unknown>> = [];
  promptResultCalls: Array<{ threadId: string; mutationId: string }> = [];
  interruptCalls: Array<{ threadId: string; turnId: string }> = [];
  unknownResolutionCalls: Array<{
    threadId: string;
    action: 'discard' | 'retry';
    mutationId: string;
  }> = [];
  lifecycleVerificationCalls: Array<{ threadId: string; mutationId: string }> = [];
  uploadCalls: Array<Record<string, unknown>> = [];
  profileUpdates: Array<Record<string, unknown>> = [];
  settingsReadCalls = 0;
  settingsUpdates: Array<Record<string, string>> = [];
  settingsUpdateError: Error | null = null;
  settingsUpdateGate: Promise<void> | null = null;
  profileUpdateError: Error | null = null;
  profileUpdateGate: Promise<void> | null = null;
  threads: FocusThreadSummary[] = [thread()];
  currentSnapshot = snapshot();
  olderTurns: FocusTurnPage = {
    runtime_epoch: 'epoch-1',
    revision: 0,
    items_view: 'full',
    page_cursor: '',
    turns: [],
    older_turn_cursor: '',
    has_more_turns: false,
  };
  initializeError: Error | null = null;
  metaError: Error | null = null;
  operatorStatusError: Error | null = null;
  operatorStatusGate: Promise<void> | null = null;
  operatorStatusValue: FocusOperatorStatus = {
    status: 'ok',
    observed_at: 1,
    poll_after_seconds: 1,
    warnings: [],
    runtime_loop: {},
  };

  constructor(clientId = 'tab-1') {
    this.clientId = clientId;
  }
  readError: Error | null = null;
  readGate: Promise<void> | null = null;
  startThreadError: FocusApiError | null = null;
  startThreadErrorAfterCreate: FocusApiError | null = null;
  submitPromptError: FocusApiError | null = null;
  archiveResult: FocusLifecycleResult = {
    thread_id: 'thread-1', upstream_outcome: 'success', focus_cleanup: 'complete', cleanup_errors: [],
  };
  unarchiveResult: FocusLifecycleResult = {
    thread_id: 'thread-1', upstream_outcome: 'success', focus_cleanup: 'skipped', cleanup_errors: [],
  };
  deleteResult: FocusLifecycleResult = {
    thread_id: 'thread-1', upstream_outcome: 'success', focus_cleanup: 'complete', cleanup_errors: [],
  };
  lifecycleVerificationState: 'present' | 'archived' | 'deleted' = 'present';
  lifecycleVerificationAlreadyReconciled = false;

  private readonly metaValue: FocusMeta = {
    product: 'Focus',
    instance: 'default',
    web_display_name: 'Focus Web',
    csrf_token: 'csrf-1',
    runtime_epoch: 'epoch-1',
    revision: 0,
    default_working_dir: '/work',
    models: [{
      id: 'gpt-test',
      catalog_id: 'gpt-test',
      model: 'gpt-test',
      display_name: 'GPT Test',
      description: '',
      is_default: true,
      hidden: false,
      default_reasoning_effort: 'high',
      supported_reasoning_efforts: [{ effort: 'medium', description: '' }, { effort: 'high', description: '' }],
      input_modalities: ['text', 'image'],
      supports_personality: true,
      service_tiers: [],
      default_service_tier: '',
      upgrade: '',
      upgrade_info: null,
    }],
    capabilities,
    writer_profile: {
      selected_thread_id: 'thread-1',
      working_dir: '/work',
      scope_generation: 2,
    },
    next_turn_settings: {
      generation: 1,
      model: '',
      reasoning_effort: '',
      approval_policy: 'never',
      permissions_profile_id: ':danger-full-access',
    },
    approval_policies: ['never', 'on-request', 'untrusted'],
    permissions_profiles: [
      { id: ':read-only', label: 'Read Only' },
      { id: ':workspace', label: 'Workspace' },
      { id: ':danger-full-access', label: 'Danger Full Access' },
    ],
  };

  async initialize(): Promise<FocusMeta> {
    if (this.initializeError) {
      const error = this.initializeError;
      this.initializeError = null;
      throw error;
    }
    return structuredClone(this.metaValue);
  }

  async meta(): Promise<FocusMeta> {
    this.metaCalls += 1;
    if (this.metaError) {
      const error = this.metaError;
      this.metaError = null;
      throw error;
    }
    return structuredClone(this.metaValue);
  }

  async operatorStatus(): Promise<FocusOperatorStatus> {
    this.operatorStatusCalls += 1;
    if (this.operatorStatusGate) await this.operatorStatusGate;
    if (this.operatorStatusError) {
      const error = this.operatorStatusError;
      this.operatorStatusError = null;
      throw error;
    }
    return this.operatorStatusValue;
  }

  async updateProfile(changes: Record<string, unknown>) {
    this.profileUpdates.push({ ...changes });
    const responseGate = this.profileUpdateGate;
    if (this.profileUpdateError) {
      const error = this.profileUpdateError;
      this.profileUpdateError = null;
      throw error;
    }
    const previous = { ...this.metaValue.writer_profile };
    const clearsSelection = Object.prototype.hasOwnProperty.call(
      changes,
      'selected_thread_id',
    ) && previous.selected_thread_id !== '';
    const changesWorkspace = typeof changes.working_dir === 'string'
      && changes.working_dir !== previous.working_dir;
    const scopeChanged = clearsSelection || changesWorkspace;
    const previousScope = previous.selected_thread_id
      ? `thread:${previous.selected_thread_id}`
      : `draft:${previous.working_dir}`;
    const previousEffectiveCwd = previous.selected_thread_id
      ? this.threads.find((item) => item.id === previous.selected_thread_id)?.cwd ?? ''
      : previous.working_dir;
    Object.assign(this.metaValue.writer_profile, changes);
    if (scopeChanged) this.metaValue.writer_profile.scope_generation += 1;
    const currentScope = this.metaValue.writer_profile.selected_thread_id
      ? `thread:${this.metaValue.writer_profile.selected_thread_id}`
      : `draft:${this.metaValue.writer_profile.working_dir}`;
    const rebound = clearsSelection
      && previousEffectiveCwd === this.metaValue.writer_profile.working_dir;
    const response = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      writer_profile: { ...this.metaValue.writer_profile },
      scope_changed: scopeChanged,
      previous_attachment_scope: scopeChanged ? previousScope : '',
      current_attachment_scope: currentScope,
      previous_scope_generation: previous.scope_generation,
      current_scope_generation: this.metaValue.writer_profile.scope_generation,
      attachment_scope_disposition: scopeChanged
        ? rebound ? 'rebound' as const : 'invalidated' as const
        : 'unchanged' as const,
      invalidated_attachment_count: 0,
      rebound_attachment_count: 0,
    };
    if (responseGate) await responseGate;
    return response;
  }

  async readNextTurnSettings() {
    this.settingsReadCalls += 1;
    return {
      runtime_epoch: this.metaValue.runtime_epoch,
      revision: this.metaValue.revision,
      next_turn_settings: { ...this.metaValue.next_turn_settings },
    };
  }

  async updateNextTurnSettings(
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
  ) {
    this.settingsUpdates.push({ ...changes });
    if (this.settingsUpdateError) {
      const error = this.settingsUpdateError;
      this.settingsUpdateError = null;
      throw error;
    }
    Object.assign(this.metaValue.next_turn_settings, changes);
    this.metaValue.next_turn_settings.generation += 1;
    const result = {
      runtime_epoch: this.metaValue.runtime_epoch,
      revision: this.metaValue.revision,
      next_turn_settings: { ...this.metaValue.next_turn_settings },
    };
    if (this.settingsUpdateGate) await this.settingsUpdateGate;
    return result;
  }

  setNextTurnSettings(
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
    generation = this.metaValue.next_turn_settings.generation,
  ): void {
    Object.assign(this.metaValue.next_turn_settings, changes, { generation });
  }

  async uploadAttachment(file: Blob, input: Record<string, unknown>) {
    this.uploadCalls.push({ file, ...input });
    return {
      file_id: 'attachment-1',
      name: String(input.name ?? 'attachment'),
      media_type: file.type || 'application/octet-stream',
      size: file.size,
      url: '/api/attachments/attachment-1',
    };
  }

  async attachmentBlob(): Promise<Blob> {
    return new Blob(['attachment']);
  }

  async listThreads(options: Record<string, unknown> = {}) {
    this.listCalls += 1;
    this.listOptions.push({ ...options });
    return {
      runtime_epoch: this.metaValue.runtime_epoch,
      revision: this.metaValue.revision,
      threads: this.threads,
    };
  }

  async readThread(threadId = 'thread-1'): Promise<FocusThreadSnapshot> {
    this.readCalls += 1;
    const result = this.currentSnapshot;
    if (this.readGate) await this.readGate;
    if (this.readError) throw this.readError;
    const previous = { ...this.metaValue.writer_profile };
    const scopeChanged = previous.selected_thread_id !== threadId;
    if (scopeChanged) this.metaValue.writer_profile.scope_generation += 1;
    this.metaValue.writer_profile.selected_thread_id = threadId;
    return {
      ...result,
      selection_scope: {
        writer_profile: { ...this.metaValue.writer_profile },
        scope_changed: scopeChanged,
        previous_attachment_scope: scopeChanged
          ? previous.selected_thread_id
            ? `thread:${previous.selected_thread_id}`
            : `draft:${previous.working_dir}`
          : '',
        current_attachment_scope: `thread:${threadId}`,
        previous_scope_generation: previous.scope_generation,
        current_scope_generation: this.metaValue.writer_profile.scope_generation,
        attachment_scope_disposition: scopeChanged ? 'isolated' : 'unchanged',
      },
    };
  }

  setRuntimeCoordinates(runtimeEpoch: string, revision: number): void {
    this.metaValue.runtime_epoch = runtimeEpoch;
    this.metaValue.revision = revision;
  }

  setSelectedThreadId(
    threadId: string,
    scopeGeneration = this.metaValue.writer_profile.scope_generation,
  ): void {
    this.metaValue.writer_profile.selected_thread_id = threadId;
    this.metaValue.writer_profile.scope_generation = scopeGeneration;
  }

  setDraftProfile(workingDir = '/work', scopeGeneration = 1): void {
    this.metaValue.writer_profile.working_dir = workingDir;
    this.setSelectedThreadId('', scopeGeneration);
  }

  async listOlderTurns(
    threadId: string,
    cursor: string,
    itemsView: 'summary' | 'full' = 'full',
    turnLimit = 10,
  ) {
    this.olderTurnCalls.push({ threadId, cursor, itemsView, turnLimit });
    return this.olderTurns;
  }

  async readToolDetail(
    threadId: string,
    locator: FocusToolInspectionLocator,
    view: FocusToolDetailView,
    _signal?: AbortSignal,
    cursor: string | null = null,
  ): Promise<FocusThreadToolDetailScanPage> {
    this.toolDetailCalls.push({ threadId, locator, view });
    return {
      runtime_epoch: this.metaValue.runtime_epoch,
      revision: this.metaValue.revision,
      thread_id: threadId,
      ...locator,
      view,
      status: 'found',
      cursor,
      next_cursor: null,
      scanned_items: 1,
      detail: view === 'preview'
        ? {
          view: 'preview',
          tool: {
            id: locator.item_id,
            name: 'exec_command',
            arg: 'printf detail',
            status: 'ok',
            output: ['detail'],
            inspectionLocator: locator,
          },
        }
        : {
          view: 'full',
          source: locator.kind === 'commandExecution'
            ? {
              type: 'commandExecution',
              id: locator.item_id,
              pluginId: null,
              scriptPath: null,
              command: 'printf detail',
              cwd: '/work',
              processId: null,
              source: 'agent',
              status: 'completed',
              commandActions: [],
              aggregatedOutput: 'detail',
              exitCode: 0,
              durationMs: 1,
            }
            : {
              type: 'fileChange',
              id: locator.item_id,
              changes: [{
                path: 'file.txt',
                kind: { type: 'update', movePath: null },
                diff: '+detail',
              }],
              status: 'completed',
            },
        },
    };
  }

  async searchConversation(
    threadId: string,
    query: string,
    cursor: string | null = null,
    _signal?: AbortSignal,
  ): Promise<FocusThreadConversationSearchPage> {
    this.conversationSearchCalls.push({ threadId, query, cursor });
    return {
      runtime_epoch: this.metaValue.runtime_epoch,
      revision: this.metaValue.revision,
      thread_id: threadId,
      query,
      cursor,
      occurrences: [],
      next_cursor: null,
    };
  }

  async startThread(input: Record<string, unknown>) {
    this.startThreadCalls.push(input);
    if (this.startThreadError) throw this.startThreadError;
    this.currentSnapshot = {
      ...snapshot(),
      thread: {
        ...thread(),
        id: 'thread-new',
        name: '',
        title: 'New conversation',
      },
    };
    if (this.startThreadErrorAfterCreate) throw this.startThreadErrorAfterCreate;
    return { accepted: true, thread_id: 'thread-new', turn_id: '' };
  }

  async submitPrompt(threadId: string, input: Record<string, unknown>) {
    this.submitPromptCalls.push({ threadId, ...input });
    if (this.submitPromptError) throw this.submitPromptError;
    const mutationId = String(input.mutationId ?? '');
    return {
      thread_id: threadId,
      mutation_id: mutationId,
      client_user_message_id: `focus-web:${mutationId}`,
      status: 'succeeded' as const,
      mode: 'start' as const,
      turn_id: 'turn-new',
      reason_code: '',
    };
  }

  async readPromptResult(threadId: string, mutationId: string) {
    this.promptResultCalls.push({ threadId, mutationId });
    return {
      thread_id: threadId,
      mutation_id: mutationId,
      client_user_message_id: `focus-web:${mutationId}`,
      status: 'succeeded' as const,
      mode: 'start' as const,
      turn_id: 'turn-new',
      reason_code: '',
    };
  }

  async interrupt(threadId: string, turnId: string) {
    this.interruptCalls.push({ threadId, turnId });
    return { accepted: true, thread_id: threadId, turn_id: turnId };
  }

  async verifyUnknownLifecycleMutation(
    threadId: string,
    mutationId: string,
  ): Promise<FocusLifecycleVerificationResult> {
    this.lifecycleVerificationCalls.push({ threadId, mutationId });
    if (this.lifecycleVerificationAlreadyReconciled) {
      if (this.currentSnapshot.thread.id === threadId) {
        this.currentSnapshot = { ...this.currentSnapshot, mutation_unknown: null };
      }
      return {
        accepted: true,
        thread_id: threadId,
        mutation_id: mutationId,
        status: 'already_reconciled',
        runtime_epoch: 'epoch-1',
        revision: 0,
      };
    }
    const verification = {
      state: this.lifecycleVerificationState,
      verification_id: 'verification-1',
    } as const;
    if (this.currentSnapshot.thread.id === threadId) {
      this.currentSnapshot = {
        ...this.currentSnapshot,
        mutation_unknown: {
          mutation_id: mutationId,
          operation: 'unarchive',
          reconciling: true,
          lifecycle_verification: verification,
        },
      };
    }
    return {
      accepted: true,
      thread_id: threadId,
      mutation_id: mutationId,
      operation: 'unarchive',
      verification,
      runtime_epoch: 'epoch-1',
      revision: 0,
    };
  }

  async resolveUnknownMutation(
    threadId: string,
    action: 'discard' | 'retry',
    mutationId: string,
  ) {
    this.unknownResolutionCalls.push({ threadId, action, mutationId });
    if (this.currentSnapshot.thread.id === threadId) {
      this.currentSnapshot = { ...this.currentSnapshot, mutation_unknown: null };
    }
    return {
      accepted: true,
      thread_id: threadId,
      mutation_id: mutationId,
      disposition: action === 'retry' ? 'retry_opened' : 'user_discard',
    };
  }

  async archiveThread(): Promise<FocusLifecycleResult> {
    return this.archiveResult;
  }

  async unarchiveThread(): Promise<FocusLifecycleResult> {
    return this.unarchiveResult;
  }

  async deleteThread(): Promise<FocusLifecycleResult> {
    return this.deleteResult;
  }

  setUnknownLifecycleMutations(mutations: FocusMeta['unknown_lifecycle_mutations']): void {
    this.metaValue.unknown_lifecycle_mutations = mutations;
  }

  async respondRequest() {
    return { accepted: true };
  }

  connectEvents(handlers: Parameters<FocusWebApiPort['connectEvents']>[0]): WebSocket {
    this.handlers = handlers;
    return { close: vi.fn() } as unknown as WebSocket;
  }

  emit(event: FocusProjectionEvent): void {
    this.handlers?.event(event);
  }

  emitInvalid(): void {
    this.handlers?.invalid?.();
  }
}

export function installFocusClientTestHooks(): void {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('localStorage', memoryStorage());
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('history', { state: null, replaceState: vi.fn() });
    vi.stubGlobal('window', {
      location: { href: 'http://127.0.0.1/', pathname: '/', search: '' },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
}
