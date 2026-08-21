import { beforeEach, vi } from 'vitest';
import { ref } from 'vue';
import { ClientIntentClock } from '../../../src/focus/clientIntentClock';
import {
  createFocusMutationActions,
  type FocusMutationActions,
  type FocusMutationActionsOptions,
} from '../../../src/focus/mutations/actions';
import type {
  ConfirmedWriterScopeReceipt,
  NavigationStateFloor,
} from '../../../src/focus/focusNavigationProfile';
import {
  type FocusAttachmentUpload,
  type FocusGoalResult,
  type FocusLifecycleResult,
  type FocusLifecycleVerificationResult,
  type FocusMeta,
  type FocusMutationResult,
  type FocusPendingRequest,
  type FocusRenameResult,
  type FocusThreadSnapshot,
  type FocusThreadSummary,
} from '../../../src/focus/types';

export const PROMPT_MUTATION_ID = '00000000-0000-4000-8000-000000000001';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key: string) {
      return values.get(key) ?? null;
    },
    key(index: number) {
      return [...values.keys()][index] ?? null;
    },
    removeItem(key: string) {
      values.delete(key);
    },
    setItem(key: string, value: string) {
      values.set(key, value);
    },
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

export function thread(id = 'thread-a'): FocusThreadSummary {
  return {
    id,
    title: id,
    name: id,
    cwd: '/work',
    status: 'active',
    owner: {
      kind: 'web',
      holder_id: 'client-1',
      relation: 'self',
      label: 'This browser',
    },
  } as FocusThreadSummary;
}

export function snapshot(pendingRequests: FocusPendingRequest[] = []): FocusThreadSnapshot {
  return {
    runtime_epoch: 'epoch-1',
    revision: 1,
    thread: thread('thread-a'),
    active_turn_id: 'turn-1',
    active_turn_status: 'inProgress',
    pending_requests: pendingRequests,
    mutation_unknown: null,
  } as FocusThreadSnapshot;
}

export function scopeReceipt(
  selectedThreadId = 'thread-a',
  workingDir = '/work',
  receiptGeneration = 1,
): ConfirmedWriterScopeReceipt {
  const attachmentScope = selectedThreadId
    ? `thread:${selectedThreadId}`
    : `draft:${workingDir}`;
  return {
    receiptGeneration,
    navigationGeneration: receiptGeneration,
    clientId: 'client-1',
    selectedThreadId,
    workingDir,
    scopeGeneration: receiptGeneration,
    attachmentScope,
    composerScopeId: `client-1:generation:${receiptGeneration}:${attachmentScope}`,
  };
}

function navigationFloor(generation: number): NavigationStateFloor {
  return {
    navigationGeneration: generation,
    status: 'confirmed',
    scopeReceiptGeneration: generation,
    authorityGeneration: generation,
    repairRequired: false,
  };
}

export function lifecycleResult(
  upstreamOutcome: FocusLifecycleResult['upstream_outcome'] = 'success',
): FocusLifecycleResult {
  return {
    thread_id: 'thread-a',
    ...(upstreamOutcome === 'unknown' ? { mutation_id: 'mutation-a' } : {}),
    upstream_outcome: upstreamOutcome,
    focus_cleanup: upstreamOutcome === 'success' ? 'complete' : 'skipped',
    cleanup_errors: [],
  };
}

export function mutationResult(
  threadId = 'thread-a',
  mutationId = '',
  disposition: FocusMutationResult['disposition'] = 'user_discard',
): FocusMutationResult {
  return {
    accepted: true,
    thread_id: threadId,
    turn_id: 'turn-1',
    ...(mutationId ? { mutation_id: mutationId, disposition } : {}),
  };
}

function startResult(threadId = 'thread-a'): FocusMutationResult {
  return {
    accepted: true,
    mode: 'started',
    thread_id: threadId,
  };
}

export function pendingRequest(
  kind: FocusPendingRequest['kind'],
  capability = `${kind}-capability`,
  id = `${kind}-request`,
): FocusPendingRequest {
  return {
    id,
    connection_generation: 7,
    response_capability: capability,
    kind,
    method: kind === 'approval'
      ? 'item/commandExecution/requestApproval'
      : 'item/tool/requestUserInput',
    thread_id: 'thread-a',
    turn_id: 'turn-1',
    status: 'pending',
    title: kind,
    params: {},
    owner_thread_id: 'thread-a',
    agent_name: 'Codex',
    actions: [],
  };
}

export interface Harness {
  actions: FocusMutationActions;
  api: {
    renameThread: ReturnType<typeof vi.fn>;
    compactThread: ReturnType<typeof vi.fn>;
    startReview: ReturnType<typeof vi.fn>;
    setGoal: ReturnType<typeof vi.fn>;
    clearGoal: ReturnType<typeof vi.fn>;
    archiveThread: ReturnType<typeof vi.fn>;
    unarchiveThread: ReturnType<typeof vi.fn>;
    deleteThread: ReturnType<typeof vi.fn>;
    uploadAttachment: ReturnType<typeof vi.fn>;
    startThread: ReturnType<typeof vi.fn>;
    submitPrompt: ReturnType<typeof vi.fn>;
    readPromptResult: ReturnType<typeof vi.fn>;
    resolveUnknownMutation: ReturnType<typeof vi.fn>;
    verifyUnknownLifecycleMutation: ReturnType<typeof vi.fn>;
    interrupt: ReturnType<typeof vi.fn>;
    respondRequest: ReturnType<typeof vi.fn>;
  };
  connection: ReturnType<typeof ref<string>>;
  activeThread: ReturnType<typeof ref<FocusThreadSummary | null>>;
  activeThreadId: ReturnType<typeof ref<string>>;
  scope: ReturnType<typeof ref<ConfirmedWriterScopeReceipt | null>>;
  snapshot: ReturnType<typeof ref<FocusThreadSnapshot | null>>;
  snapshotInvalidated: ReturnType<typeof ref<boolean>>;
  scopeReceiptIsCurrent: ReturnType<typeof vi.fn>;
  confirmUnconfirmedThread: ReturnType<typeof vi.fn>;
  requireNavigationRepair: ReturnType<typeof vi.fn>;
  clearToRepairDraft: ReturnType<typeof vi.fn>;
  refreshThreads: ReturnType<typeof vi.fn>;
  refreshArchivedThreads: ReturnType<typeof vi.fn>;
  refreshActiveThread: ReturnType<typeof vi.fn>;
  settleDeletedThread: ReturnType<typeof vi.fn>;
  invalidateWireProjection: ReturnType<typeof vi.fn>;
  promptMessage: ReturnType<typeof vi.fn>;
  reportError: ReturnType<typeof vi.fn>;
  reportFatalError: ReturnType<typeof vi.fn>;
  advanceIntent(): void;
  setScope(receipt: ConfirmedWriterScopeReceipt | null, current?: boolean): void;
  makeScopeStale(): void;
  advanceNavigationFloor(): void;
}

export function harness(clientId = 'client-1'): Harness {
  const connection = ref('connected');
  const activeThread = ref<FocusThreadSummary | null>(thread());
  const scope = ref<ConfirmedWriterScopeReceipt | null>(scopeReceipt());
  const scopeReady = ref(true);
  const projectionSnapshot = ref<FocusThreadSnapshot | null>(snapshot());
  const activeThreadId = ref('thread-a');
  const snapshotInvalidated = ref(false);
  const canCompact = ref(true);
  let scopeIsCurrent = true;
  let floorGeneration = 1;
  let navigationDisposed = false;

  const api = {
    renameThread: vi.fn(async (): Promise<FocusRenameResult> => ({
      accepted: true, thread_id: 'thread-a', name: 'renamed',
    })),
    compactThread: vi.fn(async (): Promise<FocusMutationResult> => mutationResult()),
    startReview: vi.fn(async (): Promise<FocusMutationResult> => mutationResult()),
    setGoal: vi.fn(async (): Promise<FocusGoalResult> => ({
      runtime_epoch: 'epoch-1', revision: 2, thread_id: 'thread-a', goal: null,
    })),
    clearGoal: vi.fn(async (): Promise<FocusGoalResult> => ({
      runtime_epoch: 'epoch-1', revision: 2, thread_id: 'thread-a', goal: null,
    })),
    archiveThread: vi.fn(async (): Promise<FocusLifecycleResult> => lifecycleResult()),
    unarchiveThread: vi.fn(async (): Promise<FocusLifecycleResult> => lifecycleResult()),
    deleteThread: vi.fn(async (): Promise<FocusLifecycleResult> => lifecycleResult()),
    uploadAttachment: vi.fn(async (): Promise<FocusAttachmentUpload> => ({
      file_id: 'file-1',
      name: 'demo.txt',
      media_type: 'text/plain',
      size: 4,
      url: '/api/attachments/file-1',
    })),
    startThread: vi.fn(async (): Promise<FocusMutationResult> => startResult('thread-new')),
    submitPrompt: vi.fn(async () => ({
      thread_id: 'thread-a',
      mutation_id: PROMPT_MUTATION_ID,
      client_user_message_id: `focus-web:${PROMPT_MUTATION_ID}`,
      status: 'succeeded' as const,
      mode: 'steer' as const,
      turn_id: 'turn-1',
      reason_code: '',
    })),
    readPromptResult: vi.fn(async () => ({
      thread_id: 'thread-a',
      mutation_id: PROMPT_MUTATION_ID,
      client_user_message_id: `focus-web:${PROMPT_MUTATION_ID}`,
      status: 'succeeded' as const,
      mode: 'steer' as const,
      turn_id: 'turn-1',
      reason_code: '',
    })),
    resolveUnknownMutation: vi.fn(async (
      threadId: string,
      action: 'discard' | 'retry',
      mutationId: string,
    ): Promise<FocusMutationResult> => (
      mutationResult(
        threadId,
        mutationId,
        action === 'retry' ? 'retry_opened' : 'user_discard',
      )
    )),
    verifyUnknownLifecycleMutation: vi.fn(async (): Promise<FocusLifecycleVerificationResult> => ({
      accepted: true,
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-a',
      mutation_id: 'mutation-a',
      operation: 'archive',
      verification: { state: 'archived', verification_id: 'verification-1' },
    })),
    interrupt: vi.fn(async (
      _threadId: string,
      _turnId: string,
    ): Promise<FocusMutationResult> => mutationResult()),
    respondRequest: vi.fn(async () => ({ accepted: true })),
  };
  const scopeReceiptIsCurrent = vi.fn((receipt: ConfirmedWriterScopeReceipt) => (
    scopeIsCurrent && scope.value === receipt
  ));
  const confirmUnconfirmedThread = vi.fn(async () => true);
  const requireNavigationRepair = vi.fn();
  const clearToRepairDraft = vi.fn();
  const refreshThreads = vi.fn(async () => true);
  const refreshArchivedThreads = vi.fn(async () => undefined);
  const refreshActiveThread = vi.fn(async () => true);
  const settleDeletedThread = vi.fn();
  const installGoalResult = vi.fn();
  const invalidateWireProjection = vi.fn();
  const promptMessage = vi.fn((key: string, values?: { reason: string }) => (
    `${key}${values?.reason ?? ''}`
  ));
  const reportError = vi.fn();
  const reportFatalError = vi.fn(() => false);
  const intentClock = new ClientIntentClock();
  const navigation = {
    activeThreadId,
    scopeReady,
    scopeReceipt: scope,
    scopeReceiptIsCurrent,
    confirmUnconfirmedThread,
    captureNavigationStateFloor: vi.fn(() => navigationFloor(floorGeneration)),
    navigationStateFloorIsCurrent: vi.fn((floor: NavigationStateFloor) => (
      floor.navigationGeneration === floorGeneration
    )),
    requireNavigationRepair,
    clearToRepairDraft,
    get isDisposed() {
      return navigationDisposed;
    },
  };
  const options: FocusMutationActionsOptions = {
    api: {
      clientId,
      ...api,
    } as unknown as FocusMutationActionsOptions['api'],
    intentClock,
    navigation: navigation as unknown as FocusMutationActionsOptions['navigation'],
    projection: {
      snapshot: projectionSnapshot,
      snapshotInvalidated,
      refreshThreads,
      refreshArchivedThreads,
      refreshActiveThread,
      settleDeletedThread,
      installGoalResult,
      invalidateWireProjection,
    },
    connection,
    activeThread,
    canCompact,
    attachmentsAreAvailable: () => true,
    defaultDraftWorkspace: () => '/default-draft',
    promptMessage,
    reportError,
    reportFatalError,
    clearError: vi.fn(),
  };
  const actions = createFocusMutationActions(options);
  return {
    actions,
    api,
    connection,
    activeThread,
    activeThreadId,
    scope,
    snapshot: projectionSnapshot,
    snapshotInvalidated,
    scopeReceiptIsCurrent,
    confirmUnconfirmedThread,
    requireNavigationRepair,
    clearToRepairDraft,
    refreshThreads,
    refreshArchivedThreads,
    refreshActiveThread,
    settleDeletedThread,
    invalidateWireProjection,
    promptMessage,
    reportError,
    reportFatalError,
    advanceIntent() {
      intentClock.beginIntent();
    },
    setScope(receipt, current = true) {
      scope.value = receipt;
      scopeIsCurrent = current;
    },
    makeScopeStale() {
      scopeIsCurrent = false;
      scopeReady.value = false;
    },
    advanceNavigationFloor() {
      floorGeneration += 1;
    },
  };
}

export function installUnknownLifecycle(
  actions: FocusMutationActions,
  operation: 'archive' | 'unarchive' | 'delete' = 'archive',
  verification: { state: 'present' | 'archived' | 'deleted'; verification_id: string } | null = null,
): void {
  actions.installInitialState({
    unknown_lifecycle_mutations: [{
      thread_id: 'thread-a',
      mutation_id: 'mutation-a',
      operation,
      verification,
    }],
  } as unknown as FocusMeta);
}

export function installMutationActionsTestHooks(): void {
  beforeEach(() => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('crypto', {
      randomUUID: vi.fn(() => PROMPT_MUTATION_ID),
    });
  });
}
