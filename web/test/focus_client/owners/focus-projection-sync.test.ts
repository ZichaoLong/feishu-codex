import { describe, expect, it, vi } from 'vitest';
import { nextTick, ref, watch } from 'vue';
import type { ChatTurn, ToolCall } from '../../../src/types';
import type { FocusWebApiPort } from '../../../src/focus/api';
import {
  createThreadMutationState,
  type ThreadMutationState,
} from '../../../src/focus/client-state/thread-mutations';
import { ClientIntentClock } from '../../../src/focus/clientIntentClock';
import {
  createFocusNavigationProfile,
  type FocusNavigationProfile,
} from '../../../src/focus/focusNavigationProfile';
import {
  createFocusProjectionSync,
  type FocusProjectionSync,
} from '../../../src/focus/focusProjectionSync';
import {
  createWebNextTurnSettings,
  type WebNextTurnSettingsOwner,
} from '../../../src/focus/client-state/web-next-turn-settings';
import type {
  FocusActiveTurnContext,
  FocusCapabilityMap,
  FocusGoal,
  FocusMeta,
  FocusNextTurnSettings,
  FocusProjectionEvent,
  FocusThreadList,
  FocusThreadScope,
  FocusThreadSnapshot,
  FocusThreadSummary,
  FocusTurnPage,
  FocusWriterProfile,
} from '../../../src/focus/types';

const EPOCH = 'epoch-1';

const capabilities: FocusCapabilityMap = {
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
  attachments: false,
  prompt_queue: false,
  durable_event_cursor: false,
  bounded_history: true,
  history_search: false,
  tool_detail: false,
  steer: true,
};

function profile(threadId = 'thread-a', generation = 1): FocusWriterProfile {
  return {
    selected_thread_id: threadId,
    working_dir: '/work',
    scope_generation: generation,
  };
}

function nextTurnSettings(
  generation = 1,
  changes: Partial<Omit<FocusNextTurnSettings, 'generation'>> = {},
): FocusNextTurnSettings {
  return {
    generation,
    model: '',
    reasoning_effort: '',
    approval_policy: 'on-request',
    permissions_profile_id: ':workspace',
    ...changes,
  };
}

function meta(
  revision = 0,
  writerProfile: FocusWriterProfile = profile(),
  settings: FocusNextTurnSettings = nextTurnSettings(),
): FocusMeta {
  return {
    runtime_epoch: EPOCH,
    revision,
    product: 'Focus',
    instance: 'test',
    csrf_token: 'csrf',
    default_working_dir: '/work',
    models: [],
    writer_profile: writerProfile,
    next_turn_settings: settings,
    approval_policies: ['on-request'],
    permissions_profiles: [{ id: ':workspace', label: 'Workspace' }],
    capabilities,
    unknown_lifecycle_mutations: [],
  };
}

function thread(id: string, title = id): FocusThreadSummary {
  return {
    id,
    title,
    name: title,
    preview: title,
    cwd: '/work',
    created_at: 1,
    updated_at: 1,
    source: 'appServer',
    status: 'idle',
    active_flags: [],
    model_provider: '',
    service_name: '',
    session_id: '',
    parent_thread_id: '',
    can_accept_direct_input: null,
    thread_source: '',
    ephemeral: false,
    agent_nickname: '',
    agent_role: '',
    subagent_kind: '',
    owner: { kind: 'none', holder_id: '', relation: 'none', label: 'None' },
    pending_interaction: 'none',
    loaded_instance: 'test',
    loaded_state_verified: true,
    observed_here: true,
    selectable: true,
    unavailable_reason: '',
    history_mode: 'paginated',
    action_capabilities: {
      rename: false,
      archive: false,
      unarchive: false,
      delete: false,
      compact: false,
      fork: false,
      export: false,
      review: false,
      goal: false,
    },
  };
}

function threadList(
  ids: string[],
  options: { revision?: number; scope?: FocusThreadScope; archived?: boolean } = {},
): FocusThreadList {
  return {
    runtime_epoch: EPOCH,
    revision: options.revision ?? 0,
    scope: options.scope ?? 'current',
    archived: options.archived ?? false,
    limit: 200,
    truncated: false,
    threads: ids.map((id) => thread(id)),
  };
}

function snapshot(
  id: string,
  options: {
    revision?: number;
    writerProfile?: FocusWriterProfile;
    cursor?: string;
    hasMore?: boolean;
  } = {},
): FocusThreadSnapshot {
  const writerProfile = options.writerProfile ?? profile(id);
  return {
    runtime_epoch: EPOCH,
    revision: options.revision ?? 0,
    thread: thread(id),
    turns: [],
    active_turn_id: '',
    active_turn_status: '',
    active_turn_context: null,
    pending_requests: [],
    tasks: [],
    older_turn_cursor: options.cursor ?? '',
    has_more_turns: options.hasMore ?? false,
    goal: null,
    token_usage: null,
    token_usage_available: false,
    mutation_unknown: null,
    selection_scope: {
      writer_profile: writerProfile,
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: `thread:${id}`,
      previous_scope_generation: writerProfile.scope_generation,
      current_scope_generation: writerProfile.scope_generation,
      attachment_scope_disposition: 'unchanged',
    },
  };
}

function activeTurnContext(turnId = 'turn-1'): FocusActiveTurnContext {
  return {
    turn_id: turnId,
    initiator: { kind: 'fcodex', binding_id: '' },
    feishu_audience: ['chat:group-1'],
    settings: {
      model: { value: 'gpt-test', source: 'active_reroute' },
      reasoning_effort: { value: '', source: 'unknown' },
      approval_policy: { value: 'on-request', source: 'inherited' },
      permissions_profile_id: { value: '', source: 'unknown' },
    },
  };
}

function goal(objective: string): FocusGoal {
  return {
    goal_id: `goal-${objective}`,
    objective,
    status: 'active',
    tokens_used: 0,
    wall_clock_ms: 0,
    budget: {
      token_budget: null,
      remaining_tokens: null,
      turn_budget: null,
      remaining_turns: null,
      wall_clock_budget_ms: null,
      remaining_wall_clock_ms: null,
      over_budget: false,
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

interface Harness {
  api: FocusWebApiPort;
  navigation: FocusNavigationProfile;
  settings: WebNextTurnSettingsOwner;
  intentClock: ClientIntentClock;
  mutationState: ThreadMutationState;
  projection: FocusProjectionSync;
  reportError: ReturnType<typeof vi.fn>;
  activeThreadWasRemoved: ReturnType<typeof vi.fn>;
  settleUnknownMutationFromEvent: ReturnType<typeof vi.fn>;
  transport: {
    requestProjectionReload: ReturnType<typeof vi.fn>;
    scheduleProjectionReloadRetry: ReturnType<typeof vi.fn>;
    cancelProjectionReloadRetry: ReturnType<typeof vi.fn>;
    resetProjectionReloadBackoff: ReturnType<typeof vi.fn>;
    scheduleProjectionRefresh: ReturnType<typeof vi.fn>;
    scheduleThreadListRefresh: ReturnType<typeof vi.fn>;
  };
}

function harness(initialRevision = 0): Harness {
  const intentClock = new ClientIntentClock();
  const api = {
    clientId: 'client-1',
    meta: vi.fn(async () => meta()),
    readNextTurnSettings: vi.fn(async () => ({
      runtime_epoch: EPOCH,
      revision: 0,
      next_turn_settings: nextTurnSettings(),
    })),
    updateNextTurnSettings: vi.fn(async () => ({
      runtime_epoch: EPOCH,
      revision: 0,
      next_turn_settings: nextTurnSettings(),
    })),
    listThreads: vi.fn(async () => threadList(['thread-a'])),
    readThread: vi.fn(async (threadId: string) => snapshot(threadId)),
    listOlderTurns: vi.fn(async () => ({
      runtime_epoch: EPOCH,
      revision: 0,
      items_view: 'full',
      page_cursor: '',
      turns: [],
      older_turn_cursor: '',
      has_more_turns: false,
    } satisfies FocusTurnPage)),
  } as unknown as FocusWebApiPort;
  const transport = {
    requestProjectionReload: vi.fn(),
    scheduleProjectionReloadRetry: vi.fn(),
    cancelProjectionReloadRetry: vi.fn(),
    resetProjectionReloadBackoff: vi.fn(),
    scheduleProjectionRefresh: vi.fn(),
    scheduleThreadListRefresh: vi.fn(),
  };
  const reportError = vi.fn();
  const mutationState = createThreadMutationState();
  let projection!: FocusProjectionSync;
  const navigation = createFocusNavigationProfile({
    intentClock,
    api,
    initialClientId: 'client-1',
    defaultWorkspace: () => '/work',
    clearSnapshot: () => projection.clearSnapshot(),
    clearHistoryView: vi.fn(),
    updateThreadQuery: vi.fn(),
    reportError,
    clearError: vi.fn(),
    setNavigationLoading: vi.fn(),
    threadUnavailableReason: () => '',
    workspaceNavigationBlockReason: () => '',
  });
  const settings = createWebNextTurnSettings({
    api,
    modelIsAvailable: () => true,
    supportedReasoningEfforts: () => [],
    runtimeEpochMismatch: transport.requestProjectionReload,
    reportError,
  });
  const activeThreadWasRemoved = vi.fn(() => {
    navigation.clearToRepairDraft();
  });
  const settleUnknownMutationFromEvent = vi.fn();
  projection = createFocusProjectionSync({
    api,
    intentClock,
    turnWindowLimit: ref(10),
    navigation,
    settings,
    transport: {
      hasOpenedEventSocket: () => true,
      ...transport,
    },
    reportError: vi.fn(),
    requireAuthentication: vi.fn(),
    projectionAccessIsAvailable: () => true,
    currentErrorMessage: () => '',
    clearErrorMessageIf: vi.fn(),
    activeThreadWasRemoved,
    captureUnknownMutationSnapshot: (threadId) => mutationState.captureSnapshot(threadId),
    reconcileUnknownMutation: (receipt, mutation) => (
      mutationState.reconcileUnknown(receipt, mutation)
    ),
    settleUnknownMutationFromEvent,
  });
  navigation.bindProjection(projection);
  projection.installInitialMeta(meta(initialRevision));
  return {
    api,
    navigation,
    settings,
    intentClock,
    mutationState,
    projection,
    reportError,
    activeThreadWasRemoved,
    settleUnknownMutationFromEvent,
    transport,
  };
}

async function primeActive(h: Harness, id = 'thread-a'): Promise<void> {
  const intent = id === h.navigation.activeThreadId.value
    ? null
    : h.navigation.beginThreadNavigation(id);
  vi.mocked(h.api.listThreads).mockResolvedValueOnce(threadList([id]));
  vi.mocked(h.api.readThread).mockResolvedValueOnce(snapshot(id));
  await h.projection.refreshThreads();
  await h.projection.refreshActiveThread(intent
    ? {
        requestIntentGeneration: intent.requestGeneration,
        navigationGeneration: intent.navigationGeneration,
        navigationAuthorityGeneration: intent.authorityGeneration,
      }
    : {});
}

function threadDelta(
  revision: number,
  threadId: string,
  detail: Record<string, unknown>,
): FocusProjectionEvent {
  return {
    type: 'thread_delta',
    runtime_epoch: EPOCH,
    revision,
    thread_id: threadId,
    detail,
  };
}

describe('FocusProjectionSync', () => {
  it('admits settings_changed through revision ordering and refreshes only settings', async () => {
    const h = harness();
    vi.mocked(h.api.readNextTurnSettings).mockResolvedValueOnce({
      runtime_epoch: EPOCH,
      revision: 1,
      next_turn_settings: nextTurnSettings(2, { model: 'gpt-new' }),
    });

    h.projection.handleEvent({
      type: 'settings_changed',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: '',
      reason: 'web_next_turn_settings_updated',
    });
    await vi.waitFor(() => expect(h.api.readNextTurnSettings).toHaveBeenCalledOnce());

    expect(h.settings.snapshot.value).toEqual(nextTurnSettings(2, { model: 'gpt-new' }));
    expect(h.projection.revision.value).toBe(1);
    expect(h.navigation.currentNavigationStatus).toBe('confirmed');
    expect(h.navigation.navigationRepairIsRequired).toBe(false);
    expect(h.api.meta).not.toHaveBeenCalled();
    expect(h.api.listThreads).not.toHaveBeenCalled();
    expect(h.api.readThread).not.toHaveBeenCalled();
    expect(h.transport.requestProjectionReload).not.toHaveBeenCalled();
  });

  it('keeps profile_changed navigation repair independent from settings', () => {
    const h = harness();
    const installed = h.settings.snapshot.value;

    h.projection.handleEvent({
      type: 'profile_changed',
      runtime_epoch: EPOCH,
      revision: 1,
    });

    expect(h.navigation.navigationRepairIsRequired).toBe(true);
    expect(h.transport.requestProjectionReload).toHaveBeenCalledOnce();
    expect(h.api.readNextTurnSettings).not.toHaveBeenCalled();
    expect(h.settings.snapshot.value).toEqual(installed);
  });

  it('passes the exact terminal mutation disposition to client recovery', () => {
    const h = harness();

    h.projection.handleEvent({
      type: 'mutation_reconciled',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-a',
      reason: 'prompt',
      detail: {
        mutation_id: 'mutation-1',
        operation: 'prompt',
        disposition: 'retry_opened',
      },
    });

    expect(h.settleUnknownMutationFromEvent).toHaveBeenCalledOnce();
    expect(h.settleUnknownMutationFromEvent).toHaveBeenCalledWith(
      'thread-a',
      'mutation-1',
      'prompt',
      'retry_opened',
    );
  });

  it.each(['thread/archived', 'thread/deleted'] as const)(
    'keeps a rev9 active snapshot when a rev8 %s event arrives late',
    async (reason) => {
      const h = harness(7);
      vi.mocked(h.api.readThread).mockResolvedValueOnce(snapshot('thread-a', { revision: 9 }));
      await h.projection.refreshActiveThread();
      vi.mocked(h.api.listThreads).mockClear();

      h.projection.handleEvent({
        type: 'thread_invalidated',
        runtime_epoch: EPOCH,
        revision: 8,
        thread_id: 'thread-a',
        reason,
      });

      expect(h.projection.revision.value).toBe(8);
      expect(h.projection.snapshot.value?.thread.id).toBe('thread-a');
      expect(h.navigation.activeThreadId.value).toBe('thread-a');
      expect(h.activeThreadWasRemoved).not.toHaveBeenCalled();
      expect(h.api.listThreads).toHaveBeenCalledTimes(2);
      expect(h.api.listThreads).toHaveBeenCalledWith({ scope: 'current' });
      expect(h.api.listThreads).toHaveBeenCalledWith({ scope: 'global', archived: true });
    },
  );

  it('does not let a delayed goal command overwrite a newer thread event', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'goal/updated',
      goal: goal('newer event'),
    }));

    h.projection.installGoalResult('thread-a', {
      runtime_epoch: EPOCH,
      revision: 0,
      thread_id: 'thread-a',
      goal: goal('late command'),
    });

    expect(h.projection.snapshot.value?.goal?.objective).toBe('newer event');
  });

  it('does not let an older goal event roll back a newer command result', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.installGoalResult('thread-a', {
      runtime_epoch: EPOCH,
      revision: 2,
      thread_id: 'thread-a',
      goal: goal('newer command'),
    });

    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'goal/updated',
      goal: goal('older event'),
    }));

    expect(h.projection.revision.value).toBe(1);
    expect(h.projection.snapshot.value?.goal?.objective).toBe('newer command');
  });

  it('does not let an older goal command roll back a newer HTTP snapshot', async () => {
    const h = harness();
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 9 }),
      goal: goal('newer snapshot'),
    });
    await h.projection.refreshActiveThread();

    h.projection.installGoalResult('thread-a', {
      runtime_epoch: EPOCH,
      revision: 8,
      thread_id: 'thread-a',
      goal: goal('older command'),
    });

    expect(h.projection.snapshot.value?.goal?.objective).toBe('newer snapshot');
  });

  it('does not let an older HTTP snapshot roll back a newer goal command', async () => {
    const h = harness();
    await primeActive(h);
    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));

    h.projection.installGoalResult('thread-a', {
      runtime_epoch: EPOCH,
      revision: 9,
      thread_id: 'thread-a',
      goal: goal('newer command'),
    });
    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 8 }),
      goal: goal('older snapshot'),
    });

    await expect(refreshing).resolves.toBe(true);
    expect(h.projection.snapshot.value?.goal?.objective).toBe('newer command');
  });

  it('does not let an older name event roll back a newer HTTP snapshot', async () => {
    const h = harness();
    await primeActive(h);
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a'),
      runtime_epoch: EPOCH,
      revision: 2,
      thread: { ...thread('thread-a'), name: 'New name', title: 'New name' },
    });
    await h.projection.refreshActiveThread();

    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'thread/name/updated',
      thread_name: 'Old name',
    }));

    expect(h.projection.revision.value).toBe(1);
    expect(h.projection.snapshot.value?.thread.name).toBe('New name');
  });

  it('does not let an older empty snapshot clear an unknown learned while it was in flight', async () => {
    const h = harness();
    await primeActive(h);
    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);

    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));
    h.mutationState.rememberUnknownIfUnsettled({
      threadId: 'thread-a',
      mutationId: 'mutation-new',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
    delayedSnapshot.resolve(snapshot('thread-a', { revision: 1 }));

    await expect(refreshing).resolves.toBe(true);
    expect(h.mutationState.getUnknown('thread-a')?.mutationId).toBe('mutation-new');
  });

  it('rejects a goal command from another runtime epoch and requests reload', async () => {
    const h = harness();
    await primeActive(h);

    h.projection.installGoalResult('thread-a', {
      runtime_epoch: 'epoch-replaced',
      revision: 1,
      thread_id: 'thread-a',
      goal: goal('wrong runtime'),
    });

    expect(h.projection.snapshot.value?.goal).toBeNull();
    expect(h.transport.requestProjectionReload).toHaveBeenCalledTimes(1);
  });

  it('keeps archived loading owned by the newest request receipt', async () => {
    const h = harness();
    const first = deferred<FocusThreadList>();
    const second = deferred<FocusThreadList>();
    vi.mocked(h.api.listThreads)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);

    const firstRefresh = h.projection.refreshArchivedThreads();
    const secondRefresh = h.projection.refreshArchivedThreads();
    first.resolve(threadList(['archive-old'], { archived: true }));
    await firstRefresh;
    expect(h.projection.archivedLoading.value).toBe(true);
    expect(h.projection.archivedThreads.value).toEqual([]);

    second.resolve(threadList(['archive-new'], { archived: true }));
    await secondRefresh;
    expect(h.projection.archivedLoading.value).toBe(false);
    expect(h.projection.archivedThreads.value.map((item) => item.id)).toEqual(['archive-new']);
  });

  it('commits a new active-turn delta and schedules its disclosure snapshot', async () => {
    const h = harness();
    await primeActive(h);
    h.transport.scheduleProjectionRefresh.mockClear();

    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));

    expect(h.projection.snapshot.value?.active_turn_id).toBe('turn-1');
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();
  });

  it('retains matching disclosure without refreshing on a status-only delta', async () => {
    const h = harness();
    const running = {
      ...snapshot('thread-a'),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    };
    vi.mocked(h.api.readThread).mockResolvedValueOnce(running);
    await h.projection.refreshActiveThread();
    h.transport.scheduleProjectionRefresh.mockClear();

    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/status/changed',
      active_turn_id: 'turn-1',
      active_turn_status: 'waiting',
    }));

    expect(h.projection.snapshot.value?.active_turn_context).toEqual(activeTurnContext());
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('merges only exact disclosure when a newer same-turn event rejects its snapshot', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    h.mutationState.rememberUnknownIfUnsettled({
      threadId: 'thread-a',
      mutationId: 'mutation-newer',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));

    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'item/agentMessage/delta',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      stream_delta: {
        turn_id: 'turn-1',
        item_id: 'message-1',
        kind: 'text',
        delta: 'newer event',
      },
    }));
    h.projection.handleEvent(threadDelta(3, 'thread-a', {
      method: 'thread/status/changed',
      thread_status: { type: 'active', activeFlags: ['waitingOnApproval'] },
    }));
    delayedSnapshot.resolve({
      ...snapshot('thread-a', {
        revision: 1,
        writerProfile: profile('thread-a', 99),
      }),
      thread: thread('thread-a', 'stale response'),
      turns: [{ id: 'turn-stale', role: 'user', no: 1, text: 'stale response' }],
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });

    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_status).toBe('inProgress');
    expect(h.projection.snapshot.value?.active_turn_context).toEqual(activeTurnContext());
    expect(h.projection.snapshot.value?.turns.map((turn) => turn.id)).toEqual([
      'turn-1:assistant',
    ]);
    expect(h.projection.snapshot.value?.turns[0]?.text).toBe('newer event');
    expect(h.projection.snapshot.value?.thread.status).toBe('active');
    expect(h.projection.snapshot.value?.thread.title).toBe('thread-a');
    expect(h.projection.threads.value[0]?.title).toBe('thread-a');
    expect(h.navigation.confirmedWriterProfile.value?.scope_generation).toBe(1);
    expect(h.mutationState.getUnknown('thread-a')?.mutationId).toBe('mutation-newer');
    expect(h.projection.revision.value).toBe(3);
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('publishes consecutive stream deltas once per fixed one-second window without loss', async () => {
    vi.useFakeTimers();
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    const requestFrame = vi.fn(() => 1);
    vi.stubGlobal('requestAnimationFrame', requestFrame);
    const published = vi.fn();
    const stopWatching = watch(h.projection.snapshot, published, { flush: 'post' });
    try {
      h.projection.handleEvent(threadDelta(2, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'hello ',
        },
      }));

      await vi.advanceTimersByTimeAsync(999);
      expect(requestFrame).not.toHaveBeenCalled();
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(h.projection.revision.value).toBe(1);
      expect(published).not.toHaveBeenCalled();

      // A later delta joins the first delta's fixed window without resetting
      // its deadline.
      h.projection.handleEvent(threadDelta(3, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'world',
        },
      }));
      await vi.advanceTimersByTimeAsync(1);
      await nextTick();
      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('hello world');
      expect(h.projection.revision.value).toBe(3);
      expect(published).toHaveBeenCalledOnce();

      h.projection.handleEvent(threadDelta(4, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: ' again',
        },
      }));
      await vi.advanceTimersByTimeAsync(999);
      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('hello world');
      expect(h.projection.revision.value).toBe(3);
      expect(published).toHaveBeenCalledOnce();
      await vi.advanceTimersByTimeAsync(1);
      await nextTick();
      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('hello world again');
      expect(h.projection.revision.value).toBe(4);
      expect(published).toHaveBeenCalledTimes(2);
    } finally {
      stopWatching();
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it('keeps continuous stream-only presentation bounded to one update per second', async () => {
    vi.useFakeTimers();
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    const published = vi.fn();
    const stopWatching = watch(h.projection.snapshot, published, { flush: 'post' });
    try {
      h.projection.handleEvent(threadDelta(2, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'a',
        },
      }));
      await vi.advanceTimersByTimeAsync(500);
      h.projection.handleEvent(threadDelta(3, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'b',
        },
      }));
      await vi.advanceTimersByTimeAsync(499);
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(published).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(1);
      await nextTick();
      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('ab');
      expect(h.projection.revision.value).toBe(3);
      expect(published).toHaveBeenCalledOnce();

      h.projection.handleEvent(threadDelta(4, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'c',
        },
      }));
      await vi.advanceTimersByTimeAsync(500);
      h.projection.handleEvent(threadDelta(5, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'd',
        },
      }));
      await vi.advanceTimersByTimeAsync(500);
      await nextTick();
      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('abcd');
      expect(h.projection.revision.value).toBe(5);
      expect(published).toHaveBeenCalledTimes(2);
    } finally {
      stopWatching();
      vi.useRealTimers();
    }
  });

  it('preserves historical tool turns across non-tool stream deltas', async () => {
    vi.useFakeTimers();
    try {
      const h = harness();
      await primeActive(h);
      const historicalTool: ToolCall = {
        id: 'command-history', name: 'Shell', arg: '', status: 'ok', output: ['kept'],
      };
      vi.mocked(h.api.readThread).mockResolvedValueOnce({
        ...snapshot('thread-a'),
        turns: [{
          id: 'turn-history:assistant', role: 'assistant', no: 1, text: '',
          tools: [historicalTool],
          blocks: [{ kind: 'tool', tool: historicalTool }],
        }],
      });
      await h.projection.refreshActiveThread();
      const historicalTurn = h.projection.snapshot.value?.turns[0];

      for (const [revision, itemId, kind, delta] of [
        [1, 'message-live', 'text', 'answer'],
        [2, 'thinking-live', 'thinking', 'reason'],
      ] as const) {
        h.projection.handleEvent(threadDelta(revision, 'thread-a', {
          method: kind === 'text' ? 'item/agentMessage/delta' : 'item/reasoning/summaryTextDelta',
          active_turn_id: 'turn-live',
          active_turn_status: 'inProgress',
          stream_delta: { turn_id: 'turn-live', item_id: itemId, kind, delta },
        }));
      }
      await vi.advanceTimersByTimeAsync(1_000);

      expect(h.projection.snapshot.value?.turns[0]).toBe(historicalTurn);
      const liveTurn = h.projection.snapshot.value?.turns.at(-1);
      expect(liveTurn?.text).toBe('answer');
      expect(liveTurn?.blocks).toContainEqual({
        kind: 'thinking', itemId: 'thinking-live', thinking: 'reason',
      });
      expect(h.projection.revision.value).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('preserves arbitrary tool-output chunk and blank-line boundaries', async () => {
    vi.useFakeTimers();
    try {
      const h = harness();
      await primeActive(h);
      h.projection.handleEvent(threadDelta(1, 'thread-a', {
        method: 'turn/started',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
      }));
      for (const [revision, delta] of [[2, 'hel'], [3, 'lo'], [4, '\n\n']] as const) {
        h.projection.handleEvent(threadDelta(revision, 'thread-a', {
          method: 'item/commandExecution/outputDelta',
          active_turn_id: 'turn-1',
          active_turn_status: 'inProgress',
          stream_delta: {
            turn_id: 'turn-1', item_id: 'command-1', kind: 'tool_output', delta,
          },
        }));
      }

      await vi.advanceTimersByTimeAsync(1_000);

      expect(h.projection.snapshot.value?.turns[0]?.tools?.[0]?.output).toEqual([
        'hello', '', '',
      ]);
      expect(h.projection.revision.value).toBe(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps live tool outputs inside one page aggregate budget', async () => {
    vi.useFakeTimers();
    try {
      const h = harness();
      await primeActive(h);
      h.projection.handleEvent(threadDelta(1, 'thread-a', {
        method: 'turn/started',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
      }));
      for (let index = 0; index < 17; index += 1) {
        h.projection.handleEvent(threadDelta(index + 2, 'thread-a', {
          method: 'item/commandExecution/outputDelta',
          active_turn_id: 'turn-1',
          active_turn_status: 'inProgress',
          stream_delta: {
            turn_id: 'turn-1',
            item_id: `command-${index}`,
            kind: 'tool_output',
            delta: 'x',
          },
        }));
      }
      h.projection.handleEvent(threadDelta(19, 'thread-a', {
        method: 'item/commandExecution/outputDelta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1',
          item_id: 'command-16',
          kind: 'tool_output',
          delta: '\nmore',
        },
      }));

      await vi.advanceTimersByTimeAsync(1_000);

      const tools = h.projection.snapshot.value?.turns[0]?.tools ?? [];
      expect(tools.filter((tool) => (tool.output?.length ?? 0) > 0)).toHaveLength(16);
      expect(tools[16]).toMatchObject({
        output: [],
        outputTruncated: true,
        outputOmittedChars: 6,
        outputHeadLineCount: 0,
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('lets an aggregate-omitted item completion replace locally streamed output', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'item/commandExecution/outputDelta',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      stream_delta: {
        turn_id: 'turn-1',
        item_id: 'command-1',
        kind: 'tool_output',
        delta: 'locally streamed output',
      },
    }));
    const omittedTool: ToolCall = {
      id: 'command-1',
      name: 'Shell',
      arg: '',
      status: 'ok',
      output: [],
      outputTruncated: true,
      outputOmittedChars: 100_000,
      outputHeadLineCount: 0,
    };
    h.projection.handleEvent(threadDelta(3, 'thread-a', {
      method: 'item/completed',
      turns: [{
        id: 'turn-1:assistant',
        role: 'assistant',
        no: 1,
        text: '',
        status: 'inProgress',
        tools: [omittedTool],
        blocks: [{ kind: 'tool', tool: omittedTool }],
      }],
    }));

    const merged = h.projection.snapshot.value?.turns[0];
    expect(merged?.tools?.[0]).toEqual(omittedTool);
    expect(merged?.blocks?.[0]).toEqual({ kind: 'tool', tool: omittedTool });
  });

  it('installs canonical user-before-assistant order after an early live delta', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'item/agentMessage/delta',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      stream_delta: {
        turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'working',
      },
    }));
    h.projection.handleEvent(threadDelta(3, 'thread-a', {
      method: 'item/started',
      turns: [
        { id: 'turn-1:user', role: 'user', no: 1, text: 'the prompt' },
        {
          id: 'turn-1:assistant',
          role: 'assistant',
          no: 2,
          text: '',
          status: 'inProgress',
          blocks: [],
          tools: [],
        },
      ],
    }));

    expect(h.projection.snapshot.value?.turns.map((turn) => turn.id)).toEqual([
      'turn-1:user',
      'turn-1:assistant',
    ]);
    expect(h.projection.snapshot.value?.turns[0]?.text).toBe('the prompt');
    expect(h.projection.snapshot.value?.turns[1]?.text).toBe('working');
  });

  it('rebudgets consecutive item-completion raw-turn merges across both tool surfaces', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    const output = '😀'.repeat(16_384);
    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'item/commandExecution/outputDelta',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      stream_delta: {
        turn_id: 'turn-1',
        item_id: 'streamed-command',
        kind: 'tool_output',
        delta: output,
      },
    }));
    const expectBoundedMirrors = (turn: ChatTurn | undefined): void => {
      expect(turn).toBeDefined();
      const tools = turn?.tools ?? [];
      const blockTools = (turn?.blocks ?? []).flatMap((block) => (
        block.kind === 'tool' ? [block.tool] : []
      ));
      const visibleTools = tools.filter((tool) => (tool.output?.length ?? 0) > 0);
      const visibleChars = visibleTools.reduce(
        (total, tool) => total + Array.from((tool.output ?? []).join('\n')).length,
        0,
      );
      expect(visibleTools).toHaveLength(16);
      expect(visibleChars).toBe(262_144);
      expect(blockTools.map((tool) => tool.id)).toEqual(tools.map((tool) => tool.id));
      for (const tool of tools) {
        expect(blockTools.find((candidate) => candidate.id === tool.id)).toEqual(tool);
      }
    };

    for (let batch = 0; batch < 3; batch += 1) {
      const completedTools: ToolCall[] = Array.from({ length: 16 }, (_, index) => ({
        id: `completed-${batch}-${index}`,
        name: 'Shell',
        arg: '',
        status: 'ok',
        output: [output],
      }));
      h.projection.handleEvent(threadDelta(batch + 3, 'thread-a', {
        method: 'item/completed',
        turns: [{
          id: 'turn-1:assistant',
          role: 'assistant',
          no: 1,
          text: '',
          status: 'inProgress',
          tools: completedTools,
          blocks: completedTools.map((tool) => ({ kind: 'tool' as const, tool })),
        }],
      }));

      expectBoundedMirrors(h.projection.snapshot.value?.turns[0]);
    }
  });

  it('flushes queued stream deltas before a non-stream event', async () => {
    vi.useFakeTimers();
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    try {
      h.projection.handleEvent(threadDelta(2, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'before',
        },
      }));
      await vi.advanceTimersByTimeAsync(999);
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(h.projection.revision.value).toBe(1);

      h.projection.handleEvent(threadDelta(3, 'thread-a', {
        method: 'thread/status/changed',
        thread_status: { type: 'active', activeFlags: ['waitingOnApproval'] },
      }));

      expect(h.projection.snapshot.value?.turns[0]?.text).toBe('before');
      expect(h.projection.snapshot.value?.thread.active_flags).toEqual(['waitingOnApproval']);
      expect(h.projection.revision.value).toBe(3);
      expect(vi.getTimerCount()).toBe(0);
      await vi.advanceTimersByTimeAsync(1_000);
      expect(h.projection.revision.value).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels pending presentation work on dispose without a final state mutation', async () => {
    vi.useFakeTimers();
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    try {
      h.projection.handleEvent(threadDelta(2, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1', item_id: 'message-1', kind: 'text', delta: 'discarded',
        },
      }));

      await vi.advanceTimersByTimeAsync(999);
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(h.projection.revision.value).toBe(1);
      h.projection.dispose();
      expect(vi.getTimerCount()).toBe(0);
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(h.projection.revision.value).toBe(1);

      await vi.advanceTimersByTimeAsync(1);
      expect(h.projection.snapshot.value?.turns).toEqual([]);
      expect(h.projection.revision.value).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('replaces existing disclosure from a covering read chased by safe stream events', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(3));
    for (const revision of [2, 3]) {
      h.projection.handleEvent(threadDelta(revision, 'thread-a', {
        method: 'item/agentMessage/delta',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        stream_delta: {
          turn_id: 'turn-1',
          item_id: 'message-1',
          kind: 'text',
          delta: String(revision),
        },
      }));
    }
    const refreshedContext = {
      ...activeTurnContext(),
      initiator: { kind: 'web' as const, binding_id: '' },
      feishu_audience: ['chat:group-2'],
    };
    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 2 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: refreshedContext,
    });

    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toEqual(refreshedContext);
    expect(h.projection.snapshot.value?.turns[0]?.text).toBe('23');
    expect(h.projection.revision.value).toBe(3);
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('merges disclosure at the invalidation floor when a later stream event chases it', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(3));
    h.projection.handleEvent({
      type: 'thread_invalidated',
      runtime_epoch: EPOCH,
      revision: 2,
      thread_id: 'thread-a',
      reason: 'feishu_audience_changed',
    });
    h.projection.handleEvent(threadDelta(3, 'thread-a', {
      method: 'item/agentMessage/delta',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      stream_delta: {
        turn_id: 'turn-1',
        item_id: 'message-1',
        kind: 'text',
        delta: 'newer stream',
      },
    }));
    const coveringContext = {
      ...activeTurnContext(),
      feishu_audience: ['chat:group-2'],
    };
    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 2 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: coveringContext,
    });

    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toEqual(coveringContext);
    expect(h.projection.snapshot.value?.turns[0]?.text).toBe('newer stream');
    expect(h.projection.revision.value).toBe(3);
    expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();
  });

  it.each([
    [
      'owner change',
      (revision: number): FocusProjectionEvent => ({
        type: 'owner_changed',
        runtime_epoch: EPOCH,
        revision,
        thread_id: 'thread-a',
        reason: 'fcodex_main_turn_started',
      }),
    ],
    [
      'Feishu audience change',
      (revision: number): FocusProjectionEvent => ({
        type: 'thread_invalidated',
        runtime_epoch: EPOCH,
        revision,
        thread_id: 'thread-a',
        reason: 'feishu_audience_changed',
      }),
    ],
    [
      'model reroute',
      (revision: number): FocusProjectionEvent => ({
        type: 'thread_invalidated',
        runtime_epoch: EPOCH,
        revision,
        thread_id: 'thread-a',
        reason: 'model/rerouted',
      }),
    ],
    [
      'thread settings change',
      (revision: number): FocusProjectionEvent => ({
        type: 'thread_invalidated',
        runtime_epoch: EPOCH,
        revision,
        thread_id: 'thread-a',
        reason: 'thread/settings/updated',
      }),
    ],
  ] as Array<[string, (revision: number) => FocusProjectionEvent]>)(
    'rejects stale disclosure after a newer %s and accepts a covering read',
    async (_label, eventAt) => {
      const h = harness();
      await primeActive(h);
      h.projection.handleEvent(threadDelta(1, 'thread-a', {
        method: 'turn/started',
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
      }));
      vi.mocked(h.api.readThread).mockResolvedValueOnce({
        ...snapshot('thread-a', { revision: 1 }),
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        active_turn_context: activeTurnContext(),
      });
      await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
      h.transport.scheduleProjectionRefresh.mockClear();
      vi.mocked(h.api.readThread).mockClear();

      const delayedSnapshot = deferred<FocusThreadSnapshot>();
      vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
      const refreshing = h.projection.refreshActiveThread();
      await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledOnce());
      h.projection.handleEvent(eventAt(2));
      expect(h.projection.snapshot.value?.active_turn_context).toBeNull();

      delayedSnapshot.resolve({
        ...snapshot('thread-a', { revision: 1 }),
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        active_turn_context: activeTurnContext(),
      });
      await expect(refreshing).resolves.toBe(false);
      expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
      expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();

      const coveringContext = {
        ...activeTurnContext(),
        feishu_audience: ['chat:group-2'],
      };
      vi.mocked(h.api.readThread).mockResolvedValueOnce({
        ...snapshot('thread-a', { revision: 2 }),
        active_turn_id: 'turn-1',
        active_turn_status: 'waiting',
        active_turn_context: coveringContext,
      });
      await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
      expect(h.projection.snapshot.value?.active_turn_context).toEqual(coveringContext);
      expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();
    },
  );

  it('clears stale disclosure on inactive status and installs the covering terminal read', async () => {
    const h = harness(1);
    await primeActive(h);
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(3));
    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'thread/status/changed',
      thread_status: { type: 'idle', activeFlags: [] },
    }));
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();

    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();

    vi.mocked(h.api.readThread).mockResolvedValueOnce(snapshot('thread-a', { revision: 2 }));
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    expect(h.projection.snapshot.value?.active_turn_id).toBe('');
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
  });

  it('rejects pre-disconnect disclosure until a covering backend read arrives', async () => {
    const h = harness();
    await primeActive(h);
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(3));
    h.projection.handleEvent({
      type: 'backend_disconnected',
      runtime_epoch: EPOCH,
      revision: 1,
      reason: 'app_server_disconnected',
    });
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();

    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 0 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.transport.scheduleProjectionRefresh).toHaveBeenCalledOnce();

    const coveringContext = {
      ...activeTurnContext(),
      settings: {
        ...activeTurnContext().settings,
        model: { value: '', source: 'unknown' as const },
      },
    };
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: coveringContext,
    });
    await expect(h.projection.refreshActiveThread()).resolves.toBe(true);
    expect(h.projection.snapshot.value?.active_turn_context).toEqual(coveringContext);
  });

  it.each([8, 9])(
    'keeps revision-9 disclosure when a covered revision-%s event arrives',
    async (eventRevision) => {
      const h = harness(8);
      vi.mocked(h.api.readThread).mockResolvedValueOnce({
        ...snapshot('thread-a', { revision: 9 }),
        active_turn_id: 'turn-1',
        active_turn_status: 'inProgress',
        active_turn_context: activeTurnContext(),
      });
      await expect(h.projection.refreshActiveThread()).resolves.toBe(true);

      h.projection.handleEvent({
        type: 'owner_changed',
        runtime_epoch: EPOCH,
        revision: eventRevision,
        thread_id: 'thread-a',
        reason: 'older_owner_fact',
      });

      expect(h.projection.snapshot.value?.active_turn_context).toEqual(activeTurnContext());
    },
  );

  it('fences projection invalidation until a covering composite reload', async () => {
    const h = harness(1);
    await primeActive(h);
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1', active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await h.projection.refreshActiveThread();
    const delayed = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayed.promise);
    const staleRefresh = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(3));

    h.projection.handleEvent({
      type: 'projection_invalidated', runtime_epoch: EPOCH, revision: 2,
      reason: 'projection_membership_changed',
    });
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.projection.snapshotInvalidated.value).toBe(true);
    delayed.resolve({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1', active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(staleRefresh).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();

    const covering = { ...activeTurnContext(), feishu_audience: ['chat:group-2'] };
    vi.mocked(h.api.meta).mockResolvedValueOnce(meta(2));
    vi.mocked(h.api.listThreads).mockResolvedValueOnce(
      threadList(['thread-a'], { revision: 2 }),
    );
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 2 }),
      active_turn_id: 'turn-1', active_turn_status: 'inProgress',
      active_turn_context: covering,
    });
    await h.projection.reloadAll();
    expect(h.projection.snapshotInvalidated.value).toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toEqual(covering);
  });

  it.each([
    ['revision gap', {
      type: 'owner_changed', runtime_epoch: EPOCH, revision: 3,
      thread_id: 'thread-a', reason: 'gap',
    }],
    ['epoch replacement', {
      type: 'hello', runtime_epoch: 'epoch-2', revision: 1, reason: 'replacement',
    }],
  ] as Array<[string, FocusProjectionEvent]>)('hides disclosure across %s', async (_label, event) => {
    const h = harness(1);
    await primeActive(h);
    vi.mocked(h.api.readThread).mockResolvedValueOnce({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1', active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await h.projection.refreshActiveThread();
    const delayed = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayed.promise);
    const staleRefresh = h.projection.refreshActiveThread();
    h.projection.handleEvent(event);
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.projection.snapshotInvalidated.value).toBe(true);
    delayed.resolve({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1', active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });
    await expect(staleRefresh).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
  });

  it('reports a wrong-thread stale response before considering disclosure merge', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));
    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'turn/status/changed',
      active_turn_id: 'turn-1',
      active_turn_status: 'waiting',
    }));

    delayedSnapshot.resolve({
      ...snapshot('thread-b', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });

    await expect(refreshing).rejects.toThrow(
      'Focus Web received snapshot thread-b for requested thread thread-a.',
    );
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
  });

  it('does not merge disclosure from a stale predecessor turn', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    h.transport.scheduleProjectionRefresh.mockClear();

    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));

    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-2',
      active_turn_status: 'inProgress',
    }));
    h.transport.scheduleProjectionRefresh.mockClear();
    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext('turn-1'),
    });

    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_id).toBe('turn-2');
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('does not revive disclosure after the exact turn completes', async () => {
    const h = harness();
    await primeActive(h);
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    }));
    h.transport.scheduleProjectionRefresh.mockClear();
    const delayedSnapshot = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(delayedSnapshot.promise);
    const refreshing = h.projection.refreshActiveThread();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));

    h.projection.handleEvent(threadDelta(2, 'thread-a', {
      method: 'turn/completed',
      active_turn_id: '',
      active_turn_status: 'completed',
    }));
    delayedSnapshot.resolve({
      ...snapshot('thread-a', { revision: 1 }),
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: activeTurnContext(),
    });

    await expect(refreshing).resolves.toBe(false);
    expect(h.projection.snapshot.value?.active_turn_id).toBe('');
    expect(h.projection.snapshot.value?.active_turn_context).toBeNull();
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('rejects an A read before its echoed writer profile can pollute B', async () => {
    const h = harness();
    const readA = deferred<FocusThreadSnapshot>();
    const readB = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread)
      .mockReturnValueOnce(readA.promise)
      .mockReturnValueOnce(readB.promise);

    const pendingA = h.projection.refreshActiveThread();
    const navigationB = h.navigation.beginThreadNavigation('thread-b');
    const pendingB = h.projection.refreshActiveThread({
      requestIntentGeneration: navigationB.requestGeneration,
      navigationGeneration: navigationB.navigationGeneration,
      navigationAuthorityGeneration: navigationB.authorityGeneration,
    });
    readA.resolve(snapshot('thread-a', { writerProfile: profile('thread-a', 99) }));
    await expect(pendingA).resolves.toBe(false);
    expect(h.navigation.confirmedWriterProfile.value?.scope_generation).toBe(1);

    readB.resolve(snapshot('thread-b', { writerProfile: profile('thread-b', 3) }));
    await expect(pendingB).resolves.toBe(true);
    expect(h.navigation.confirmedWriterProfile.value?.selected_thread_id).toBe('thread-b');
    expect(h.navigation.confirmedWriterProfile.value?.scope_generation).toBe(3);
    expect(h.projection.snapshot.value?.thread.id).toBe('thread-b');
  });

  it('rejects a list response when its captured scope is no longer current', async () => {
    const h = harness();
    const currentList = deferred<FocusThreadList>();
    vi.mocked(h.api.listThreads).mockReturnValueOnce(currentList.promise);

    const pending = h.projection.refreshThreads();
    h.navigation.setThreadScope('global');
    currentList.resolve(threadList(['current-only'], { scope: 'current' }));

    await expect(pending).resolves.toBe(false);
    expect(h.projection.threads.value).toEqual([]);
  });

  it('buffers and replays a typed delta across a composite reload install', async () => {
    const h = harness();
    await primeActive(h);
    const stagedThread = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.meta).mockResolvedValueOnce(meta());
    vi.mocked(h.api.listThreads).mockResolvedValueOnce(threadList(['thread-a']));
    vi.mocked(h.api.readThread).mockReturnValueOnce(stagedThread.promise);

    const reload = h.projection.reloadAll();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(2));
    h.projection.handleEvent(threadDelta(1, 'thread-a', {
      method: 'turn/started',
      active_turn_id: 'turn-buffered',
      active_turn_status: 'inProgress',
    }));
    stagedThread.resolve(snapshot('thread-a'));
    await reload;

    expect(h.projection.snapshotInvalidated.value).toBe(false);
    expect(h.projection.revision.value).toBe(1);
    expect(h.projection.snapshot.value?.active_turn_id).toBe('turn-buffered');
  });

  it('does not replay events already covered by a newer composite thread snapshot', async () => {
    const h = harness(9);
    const stagedThread = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.meta).mockResolvedValueOnce(meta(9));
    vi.mocked(h.api.listThreads).mockResolvedValueOnce(
      threadList(['thread-a'], { revision: 9 }),
    );
    vi.mocked(h.api.readThread).mockReturnValueOnce(stagedThread.promise);

    const reload = h.projection.reloadAll();
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledOnce());
    h.projection.handleEvent(threadDelta(10, 'thread-a', {
      method: 'thread/name/updated',
      thread_name: 'Older event name',
    }));
    h.projection.handleEvent({
      type: 'thread_invalidated',
      runtime_epoch: EPOCH,
      revision: 11,
      thread_id: 'thread-a',
      reason: 'thread/deleted',
    });
    stagedThread.resolve({
      ...snapshot('thread-a', { revision: 12 }),
      thread: {
        ...thread('thread-a'),
        name: 'Snapshot name',
        title: 'Snapshot name',
      },
    });
    await reload;

    expect(h.projection.revision.value).toBe(11);
    expect(h.projection.snapshot.value?.thread.name).toBe('Snapshot name');
    expect(h.navigation.activeThreadId.value).toBe('thread-a');
    expect(h.activeThreadWasRemoved).not.toHaveBeenCalled();
  });

  it('does not select stale meta A while a profile repair races pending B', async () => {
    const h = harness();
    const readB = deferred<FocusThreadSnapshot>();
    vi.mocked(h.api.readThread).mockReturnValueOnce(readB.promise);
    vi.mocked(h.api.meta)
      .mockResolvedValueOnce(meta(1, profile('thread-a', 1)))
      .mockResolvedValueOnce(meta(1, profile('thread-b', 2)));
    vi.mocked(h.api.listThreads).mockResolvedValueOnce(
      threadList(['thread-a', 'thread-b'], { revision: 1 }),
    );

    const selectionB = h.navigation.selectThread('thread-b');
    await vi.waitFor(() => expect(h.api.readThread).toHaveBeenCalledTimes(1));
    h.projection.handleEvent({
      type: 'profile_changed',
      runtime_epoch: EPOCH,
      revision: 1,
    });

    await h.projection.reloadAll();

    // The only read is the user-requested B transaction.  Fresh meta still
    // naming A is projection evidence, not authority to select A over B.
    expect(h.api.readThread).toHaveBeenCalledTimes(1);
    expect(h.api.readThread).toHaveBeenCalledWith('thread-b', 1);
    expect(h.transport.scheduleProjectionReloadRetry).toHaveBeenCalled();

    readB.resolve(snapshot('thread-b', {
      revision: 1,
      writerProfile: profile('thread-b', 2),
    }));
    await selectionB;

    expect(h.navigation.activeThreadId.value).toBe('thread-b');
    expect(h.navigation.confirmedWriterProfile.value).toEqual(profile('thread-b', 2));
    expect(h.navigation.currentNavigationStatus).toBe('confirmed');
    expect(h.navigation.navigationRepairIsRequired).toBe(false);
    expect(h.projection.snapshotInvalidated.value).toBe(true);
  });

  it('stops composite reload at every request boundary after disposal', async () => {
    const afterMeta = harness();
    const metaResult = deferred<FocusMeta>();
    vi.mocked(afterMeta.api.meta).mockReturnValueOnce(metaResult.promise);
    const metaReload = afterMeta.projection.reloadAll();
    await vi.waitFor(() => expect(afterMeta.api.meta).toHaveBeenCalledOnce());
    afterMeta.navigation.dispose();
    metaResult.resolve(meta());
    await metaReload;
    expect(afterMeta.api.listThreads).not.toHaveBeenCalled();
    expect(afterMeta.api.readThread).not.toHaveBeenCalled();

    const afterList = harness();
    const listResult = deferred<FocusThreadList>();
    vi.mocked(afterList.api.listThreads).mockReturnValueOnce(listResult.promise);
    const listReload = afterList.projection.reloadAll();
    await vi.waitFor(() => expect(afterList.api.listThreads).toHaveBeenCalledOnce());
    afterList.navigation.dispose();
    listResult.resolve(threadList(['thread-a']));
    await listReload;
    expect(afterList.api.readThread).not.toHaveBeenCalled();

    const afterRead = harness();
    const readResult = deferred<FocusThreadSnapshot>();
    vi.mocked(afterRead.api.readThread).mockReturnValueOnce(readResult.promise);
    const readReload = afterRead.projection.reloadAll();
    await vi.waitFor(() => expect(afterRead.api.readThread).toHaveBeenCalledOnce());
    const initialMeta = afterRead.projection.meta.value;
    afterRead.navigation.dispose();
    readResult.resolve(snapshot('thread-a', { writerProfile: profile('thread-a', 99) }));
    await readReload;
    expect(afterRead.projection.meta.value).toBe(initialMeta);
    expect(afterRead.projection.threads.value).toEqual([]);
    expect(afterRead.projection.snapshot.value).toBeNull();
    expect(afterRead.transport.scheduleProjectionReloadRetry).not.toHaveBeenCalled();
    expect(afterRead.transport.requestProjectionReload).not.toHaveBeenCalled();
    expect(afterRead.reportError).not.toHaveBeenCalled();
  });

  it('drops late list, active, and older-page results after disposal', async () => {
    const archived = harness();
    const archivedResult = deferred<FocusThreadList>();
    vi.mocked(archived.api.listThreads).mockReturnValueOnce(archivedResult.promise);
    const archivedRefresh = archived.projection.refreshArchivedThreads();
    archived.navigation.dispose();
    archivedResult.resolve(threadList(['late-archived'], { archived: true }));
    await archivedRefresh;
    expect(archived.projection.archivedThreads.value).toEqual([]);

    const search = harness();
    const searchResult = deferred<FocusThreadList>();
    vi.mocked(search.api.listThreads).mockReturnValueOnce(searchResult.promise);
    const searchRefresh = search.projection.loadAllSessionsForSearch();
    search.navigation.dispose();
    searchResult.resolve(threadList(['late-search'], { scope: 'global' }));
    await searchRefresh;
    expect(search.projection.searchThreads.value).toEqual([]);

    const active = harness();
    const activeResult = deferred<FocusThreadSnapshot>();
    vi.mocked(active.api.readThread).mockReturnValueOnce(activeResult.promise);
    const activeRefresh = active.projection.refreshActiveThread();
    active.navigation.dispose();
    activeResult.resolve(snapshot('thread-a', { writerProfile: profile('thread-a', 99) }));
    await expect(activeRefresh).resolves.toBe(false);
    expect(active.projection.snapshot.value).toBeNull();
    expect(active.navigation.confirmedWriterProfile.value?.scope_generation).toBe(1);

  });

  it('ignores synchronous projection commands and events after disposal', async () => {
    const h = harness();
    await primeActive(h);
    const installedMeta = h.projection.meta.value;
    const installedThreads = h.projection.threads.value;
    const installedSnapshot = h.projection.snapshot.value;
    h.navigation.dispose();

    h.projection.installInitialMeta(meta(99, profile('thread-b', 99)));
    h.projection.clearSnapshot();
    h.projection.settleDeletedThread('thread-a', true);
    h.projection.installGoalResult('thread-a', {
      runtime_epoch: EPOCH, revision: 99, thread_id: 'thread-a', goal: null,
    });
    h.projection.handleEvent(threadDelta(99, 'thread-a', { method: 'turn/completed' }));
    h.projection.invalidateWireProjection();
    expect(h.projection.meta.value).toBe(installedMeta);
    expect(h.projection.threads.value).toBe(installedThreads);
    expect(h.projection.snapshot.value).toBe(installedSnapshot);
    expect(h.projection.mayRetryProjectionReload()).toBe(false);
    expect(h.transport.requestProjectionReload).not.toHaveBeenCalled();
    expect(h.transport.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('clears only the exact active projection on delete settlement', async () => {
    const h = harness();
    await primeActive(h);

    h.projection.settleDeletedThread('thread-a', false);
    expect(h.projection.snapshot.value?.thread.id).toBe('thread-a');
    expect(h.projection.threads.value).toEqual([]);

    h.projection.settleDeletedThread('thread-a', true);
    expect(h.projection.snapshot.value).toBeNull();
  });
});
