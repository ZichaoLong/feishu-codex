import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FocusWebApiPort } from '../../../src/focus/api';
import type {
  FocusBackendResetPreview,
  FocusCapabilityMap,
  FocusMeta,
  FocusNextTurnSettingsResult,
  FocusProjectionEvent,
  FocusThreadConversationSearchPage,
  FocusThreadList,
  FocusThreadSnapshot,
  FocusThreadToolDetailScanPage,
  FocusThreadSummary,
  FocusToolInspectionLocator,
  FocusWriterProfileResult,
} from '../../../src/focus/types';
import { FocusApiError } from '../../../src/focus/types';
import { useFocusWebClient } from '../../../src/focus/useFocusWebClient';

const EPOCH = 'runtime-epoch';
const DOCUMENT_RECEIPT = 'a'.repeat(64);
const PROMPT_MUTATION_ID = '00000000-0000-4000-8000-000000000001';
const INSPECTION_LOCATOR: FocusToolInspectionLocator = {
  turn_id: 'turn-1',
  item_id: 'command-1',
  kind: 'commandExecution',
  change_index: null,
};

function backendResetPreview(): FocusBackendResetPreview {
  return {
    instance: 'test',
    status: 'available',
    reason_code: '',
    reason_text: 'safe',
    expected_connection_generation: 7,
    pending_request_count: 0,
    running_binding_count: 0,
    attached_binding_count: 0,
    active_loaded_thread_count: 0,
    loaded_thread_count: 0,
    runtime_verification_failed: false,
  };
}

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

function stubBrowser(href = 'http://focus.test/'): void {
  vi.stubGlobal('sessionStorage', memoryStorage());
  vi.stubGlobal('localStorage', memoryStorage());
  vi.stubGlobal('history', { state: null, replaceState: vi.fn() });
  vi.stubGlobal('window', {
    location: {
      href,
      reload: vi.fn(),
    },
  });
}

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
  history_search: true,
  tool_detail: true,
  steer: true,
};

function meta(revision: number): FocusMeta {
  return {
    runtime_epoch: EPOCH,
    revision,
    product: 'Focus',
    instance: 'test',
    web_display_name: 'Focus Web',
    csrf_token: 'csrf',
    default_working_dir: '/work',
    models: [],
    writer_profile: {
      selected_thread_id: 'thread-1',
      working_dir: '/work',
      scope_generation: 1,
    },
    next_turn_settings: {
      generation: 1,
      model: '',
      reasoning_effort: '',
      approval_policy: 'on-request',
      permissions_profile_id: ':workspace',
    },
    approval_policies: ['on-request'],
    permissions_profiles: [{ id: ':workspace', label: 'Workspace' }],
    capabilities,
    unknown_lifecycle_mutations: [],
  };
}

function draftMeta(
  revision: number,
  scopeGeneration = 2,
  workingDir = '/draft',
): FocusMeta {
  return {
    ...meta(revision),
    writer_profile: {
      ...meta(revision).writer_profile,
      selected_thread_id: '',
      working_dir: workingDir,
      scope_generation: scopeGeneration,
    },
  };
}

function thread(title: string): FocusThreadSummary {
  return {
    id: 'thread-1',
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

function threadList(revision: number, title: string): FocusThreadList {
  return {
    runtime_epoch: EPOCH,
    revision,
    scope: 'current',
    archived: false,
    limit: 200,
    truncated: false,
    threads: [thread(title)],
  };
}

function snapshot(revision: number, title: string): FocusThreadSnapshot {
  return {
    runtime_epoch: EPOCH,
    revision,
    thread: thread(title),
    turns: [],
    active_turn_id: '',
    active_turn_status: '',
    active_turn_context: null,
    pending_requests: [],
    tasks: [],
    older_turn_cursor: '',
    has_more_turns: false,
    goal: null,
    token_usage: null,
    token_usage_available: false,
    mutation_unknown: null,
    selection_scope: {
      writer_profile: meta(revision).writer_profile,
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 1,
      attachment_scope_disposition: 'unchanged',
    },
  };
}

function selectedSnapshot(
  threadId: string,
  title: string,
  revision: number,
  scopeGeneration: number,
): FocusThreadSnapshot {
  const selectedThread = { ...thread(title), id: threadId };
  return {
    ...snapshot(revision, title),
    thread: selectedThread,
    selection_scope: {
      ...snapshot(revision, title).selection_scope,
      writer_profile: {
        ...meta(revision).writer_profile,
        selected_thread_id: threadId,
        scope_generation: scopeGeneration,
      },
      current_attachment_scope: `thread:${threadId}`,
      current_scope_generation: scopeGeneration,
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

interface EventHandlers {
  event: (event: FocusProjectionEvent) => void;
  open?: () => void;
  close?: () => void;
}

function testApi(intentGenerationFloor = 0) {
  let handlers: EventHandlers | null = null;
  const socket = { close: vi.fn() } as unknown as WebSocket;
  const api = {
    clientId: 'client-1',
    documentReceipt: DOCUMENT_RECEIPT,
    intentGenerationFloor,
    initialize: vi.fn(async () => meta(0)),
    meta: vi.fn(async () => meta(0)),
    backendResetPreview: vi.fn(async () => backendResetPreview()),
    backendResetExecute: vi.fn(),
    listThreads: vi.fn(async () => threadList(0, 'Initial')),
    readThread: vi.fn(async () => snapshot(0, 'Initial')),
    readToolDetail: vi.fn(async (
      threadId: string,
      locator: FocusToolInspectionLocator,
      _view: 'preview' | 'full',
    ) => ({
      runtime_epoch: EPOCH,
      revision: 0,
      thread_id: threadId,
      ...locator,
      view: 'preview' as const,
      status: 'found' as const,
      cursor: null,
      next_cursor: null,
      scanned_items: 1,
      detail: {
        view: 'preview' as const,
        tool: {
          id: locator.item_id,
          name: 'exec_command',
          arg: 'printf detail',
          status: 'ok' as const,
          output: ['detail'],
          inspectionLocator: locator,
        },
      },
    } satisfies FocusThreadToolDetailScanPage)),
    searchConversation: vi.fn(async (
      threadId: string,
      query: string,
      cursor: string | null = null,
    ) => ({
      runtime_epoch: EPOCH,
      revision: 0,
      thread_id: threadId,
      query,
      cursor,
      occurrences: [],
      next_cursor: null,
    } satisfies FocusThreadConversationSearchPage)),
    archiveThread: vi.fn(async (threadId: string) => ({
      thread_id: threadId,
      upstream_outcome: 'success' as const,
      focus_cleanup: 'complete' as const,
      cleanup_errors: [],
    })),
    deleteThread: vi.fn(async (threadId: string) => ({
      thread_id: threadId,
      upstream_outcome: 'success' as const,
      focus_cleanup: 'complete' as const,
      cleanup_errors: [],
    })),
    startThread: vi.fn(),
    submitPrompt: vi.fn(),
    readPromptResult: vi.fn(async (threadId: string, mutationId: string) => ({
      thread_id: threadId,
      mutation_id: mutationId,
      client_user_message_id: `focus-web:${mutationId}`,
      status: 'succeeded' as const,
      mode: 'steer' as const,
      turn_id: 'turn-1',
      reason_code: '',
    })),
    readNextTurnSettings: vi.fn(async () => ({
      runtime_epoch: EPOCH,
      revision: 0,
      next_turn_settings: meta(0).next_turn_settings,
    })),
    updateNextTurnSettings: vi.fn(async (changes: Record<string, string>) => ({
      runtime_epoch: EPOCH,
      revision: 0,
      next_turn_settings: {
        ...meta(0).next_turn_settings,
        ...changes,
        generation: 2,
      },
    })),
    updateProfile: vi.fn(async (changes: Record<string, string>) => ({
      runtime_epoch: EPOCH,
      revision: 0,
      writer_profile: { ...meta(0).writer_profile, ...changes },
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 1,
      attachment_scope_disposition: 'unchanged',
      invalidated_attachment_count: 0,
      rebound_attachment_count: 0,
    } satisfies FocusWriterProfileResult)),
    connectEvents: vi.fn((nextHandlers: EventHandlers) => {
      handlers = nextHandlers;
      return socket;
    }),
  } as unknown as FocusWebApiPort;
  return {
    api,
    get handlers(): EventHandlers {
      if (handlers === null) throw new Error('event handlers were not registered');
      return handlers;
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useFocusWebClient runtime notice routing', () => {
  it('publishes a typed notice without turning it into a projection reload', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    expect(fake.api.readThread).toHaveBeenCalledOnce();

    fake.handlers.event({
      type: 'runtime_notice',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'error',
        message: 'temporary upstream failure',
        additional_details: 'retry scheduled',
        will_retry: true,
        turn_id: 'turn-1',
      },
    });

    expect(client.runtimeNoticePresentation.value.retry).toMatchObject({
      message: 'temporary upstream failure',
      additionalDetails: 'retry scheduled',
      willRetry: true,
    });
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(fake.api.readThread).toHaveBeenCalledOnce();
    client.dispose();
  });
});

describe('useFocusWebClient turn-window preference', () => {
  function turns(count: number) {
    return Array.from({ length: count }, (_, index) => ({
      id: `raw-${index}:user`,
      role: 'user' as const,
      no: index + 1,
      text: `prompt-${index}`,
    }));
  }

  it('shrinks immediately and lets a newer 20-width generation supersede stale 5', async () => {
    stubBrowser();
    localStorage.setItem('focus-web.turn-window-limit', '20');
    const fake = testApi();
    vi.mocked(fake.api.readThread).mockResolvedValue({
      ...snapshot(0, 'Initial'),
      turns: turns(20),
    });
    const client = useFocusWebClient(fake.api);
    await client.load();
    expect(client.turnWindowLimit.value).toBe(20);
    expect(client.turns.value).toHaveLength(20);

    const shrinkResponse = deferred<FocusThreadSnapshot>();
    const expandResponse = deferred<FocusThreadSnapshot>();
    vi.mocked(fake.api.readThread)
      .mockReturnValueOnce(shrinkResponse.promise)
      .mockReturnValueOnce(expandResponse.promise);
    const shrinking = client.setTurnWindowLimit(5);
    expect(client.turns.value).toHaveLength(5);
    expect(fake.api.readThread).toHaveBeenLastCalledWith('thread-1', expect.any(Number), 5);

    const expanding = client.setTurnWindowLimit(20);
    expect(fake.api.readThread).toHaveBeenLastCalledWith('thread-1', expect.any(Number), 20);
    expandResponse.resolve({
      ...snapshot(2, 'Initial'),
      turns: turns(20),
    });
    await expanding;
    shrinkResponse.resolve({ ...snapshot(1, 'Initial'), turns: turns(5) });
    await shrinking;

    expect(client.turns.value).toHaveLength(20);
    expect(localStorage.getItem('focus-web.turn-window-limit')).toBe('20');
    client.dispose();
  });

  it('settles a failed width refresh with the trimmed view and lightweight outline', async () => {
    stubBrowser();
    localStorage.setItem('focus-web.turn-window-limit', '20');
    const fake = testApi();
    vi.mocked(fake.api.readThread).mockResolvedValueOnce({
      ...snapshot(0, 'Initial'),
      turns: turns(20),
    });
    const client = useFocusWebClient(fake.api);
    await client.load();
    vi.mocked(fake.api.readThread).mockRejectedValueOnce(
      new Error('turn window refresh failed'),
    );

    await expect(client.setTurnWindowLimit(5)).resolves.toBeUndefined();

    expect(client.turns.value).toHaveLength(5);
    expect(client.historyOutline.value.map((prompt) => prompt.id)).toEqual(
      Array.from({ length: 5 }, (_, index) => `raw-${index + 15}:user`),
    );
    expect(client.errorMessage.value).toBe('turn window refresh failed');
    client.dispose();
  });
});

describe('useFocusWebClient backend reset fatal refresh composition', () => {
  it.each([
    {
      name: 'a replaced document',
      refreshError: new FocusApiError('document replaced', {
        status: 409,
        code: 'document_replaced',
      }),
      authRequired: false,
      documentReloadRequired: true,
    },
    {
      name: 'expired authentication',
      refreshError: new FocusApiError('authentication required', {
        status: 401,
        code: 'authentication_required',
      }),
      authRequired: true,
      documentReloadRequired: false,
    },
  ])('honors $name after a known-no-effect stale POST', async ({
    refreshError,
    authRequired,
    documentReloadRequired,
  }) => {
    stubBrowser();
    const fake = testApi();
    vi.mocked(fake.api.backendResetPreview)
      .mockResolvedValueOnce(backendResetPreview())
      .mockRejectedValueOnce(refreshError);
    vi.mocked(fake.api.backendResetExecute).mockRejectedValueOnce(
      new FocusApiError('stale reset preview', {
        status: 409,
        code: 'backend_reset_stale',
      }),
    );
    const client = useFocusWebClient(fake.api);
    await client.load();
    await expect(client.readToolDetail(INSPECTION_LOCATOR)).resolves.toBe(true);
    await expect(client.searchConversation({ query: 'needle' })).resolves.toBe(true);
    expect(client.toolDetail.value).not.toBeNull();
    expect(client.conversationSearchPage.value).not.toBeNull();
    await client.refreshBackendReset();
    const captured = client.backendResetPreview.value;
    if (captured === null) throw new Error('backend reset preview was not installed');

    await expect(client.executeBackendReset(captured)).resolves.toMatchObject({
      disposition: 'known-no-effect',
      refreshedPreview: null,
      refreshError,
    });

    expect(fake.api.backendResetExecute).toHaveBeenCalledOnce();
    expect(fake.api.backendResetPreview).toHaveBeenCalledTimes(2);
    expect(client.authRequired.value).toBe(authRequired);
    expect(client.documentReloadRequired.value).toBe(documentReloadRequired);
    expect(client.errorMessage.value).toBe('');
    expect(client.backendResetOutcomeUnknown.value).toBe(false);
    expect(client.toolDetail.value).toBeNull();
    expect(client.conversationSearchPage.value).toBeNull();
    expect(client.toolDetailAvailable.value).toBe(false);
    expect(client.conversationSearchAvailable.value).toBe(false);
    expect(client.toolDetailUnavailableReason.value).toBe('document_unavailable');
    expect(client.conversationSearchUnavailableReason.value).toBe('document_unavailable');
    await expect(client.readToolDetail(INSPECTION_LOCATOR)).resolves.toBe(false);
    await expect(client.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(fake.api.readToolDetail).toHaveBeenCalledTimes(1);
    expect(fake.api.searchConversation).toHaveBeenCalledTimes(1);
    client.dispose();
  });
});

describe('useFocusWebClient initial writer authority', () => {
  it('rebases an F5 deep-link navigation above the retained server intent floor', async () => {
    stubBrowser('http://focus.test/?thread=thread-1');
    const fake = testApi(12);
    const client = useFocusWebClient(fake.api);

    await client.load();

    expect(fake.api.readThread).toHaveBeenCalledWith('thread-1', 13);
    client.dispose();
  });

  it('captures a URL target before durable-profile convergence rewrites history', async () => {
    stubBrowser('http://focus.test/?thread=thread-b');
    vi.mocked(history.replaceState).mockImplementation((_state, _unused, url) => {
      window.location.href = new URL(String(url), window.location.href).href;
    });
    const fake = testApi();
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-b', 'Deep link', 1, 2),
    );
    const client = useFocusWebClient(fake.api);

    await client.load();

    expect(fake.api.readThread).toHaveBeenCalledWith('thread-b', expect.any(Number));
    expect(client.activeThreadId.value).toBe('thread-b');
    expect(client.snapshot.value?.thread.id).toBe('thread-b');
    expect(window.location.href).toBe('http://focus.test/?thread=thread-b');
    client.dispose();
  });

  it('keeps a selected target loading until its exact snapshot is installed', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    expect(client.conversationLoading.value).toBe(false);

    const selectedMeta = {
      ...meta(1),
      writer_profile: {
        ...meta(1).writer_profile,
        selected_thread_id: 'thread-2',
        scope_generation: 2,
      },
    };
    vi.mocked(fake.api.readThread).mockRejectedValueOnce(
      new Error('selected snapshot is still unavailable'),
    );
    vi.mocked(fake.api.meta).mockResolvedValueOnce(selectedMeta);

    await client.selectThread('thread-2');

    expect(client.loading.value).toBe(false);
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value).toBeNull();
    expect(client.conversationLoading.value).toBe(true);

    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-2', 'Exact second thread', 2, 3),
    );
    await client.selectThread('thread-2');

    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(client.conversationLoading.value).toBe(false);
    client.dispose();
  });

  it('keeps a persisted targetless draft even when inventory is non-empty', async () => {
    stubBrowser();
    const fake = testApi();
    vi.mocked(fake.api.initialize).mockResolvedValueOnce({
      ...meta(1),
      writer_profile: {
        ...meta(1).writer_profile,
        selected_thread_id: '',
        working_dir: '/draft',
        scope_generation: 2,
      },
    });
    const client = useFocusWebClient(fake.api);

    await client.load();

    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/draft');
    expect(client.snapshot.value).toBeNull();
    expect(client.conversationLoading.value).toBe(false);
    expect(client.scopeReady.value).toBe(true);
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:draft:/draft',
    );
    expect(fake.api.readThread).not.toHaveBeenCalled();
    expect(history.replaceState).toHaveBeenLastCalledWith(null, '', '/');
    client.dispose();
  });

  it('recovers an invalid URL selection to the fresh durable draft', async () => {
    stubBrowser('http://focus.test/?thread=missing');
    const fake = testApi();
    const durableDraft = {
      ...meta(2),
      writer_profile: {
        ...meta(2).writer_profile,
        selected_thread_id: '',
        working_dir: '/draft',
        scope_generation: 2,
      },
    };
    vi.mocked(fake.api.initialize).mockResolvedValueOnce(durableDraft);
    vi.mocked(fake.api.readThread).mockRejectedValueOnce(new FocusApiError(
      'Requested thread does not exist.',
      { status: 404, code: 'thread_not_found' },
    ));
    vi.mocked(fake.api.meta).mockResolvedValueOnce(durableDraft);
    const client = useFocusWebClient(fake.api);

    await client.load();

    expect(fake.api.readThread).toHaveBeenCalledWith('missing', expect.any(Number));
    expect(fake.api.meta).toHaveBeenCalled();
    expect(client.errorMessage.value).toContain('does not exist');
    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/draft');
    expect(client.snapshot.value).toBeNull();
    expect(client.scopeReady.value).toBe(true);
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:draft:/draft',
    );
    expect(history.replaceState).toHaveBeenLastCalledWith(null, '', '/');
    client.dispose();
  });

  it('does not skip authority transition for an unselectable persisted target', async () => {
    stubBrowser();
    const fake = testApi();
    const unavailable = {
      ...thread('Archived'),
      selectable: false,
      unavailable_reason: 'This thread is archived.',
    };
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce({
      ...threadList(1, 'Archived'),
      threads: [unavailable],
    });
    vi.mocked(fake.api.readThread).mockRejectedValueOnce(new FocusApiError(
      'This thread is archived.',
      { status: 409, code: 'thread_archived' },
    ));
    vi.mocked(fake.api.meta).mockResolvedValueOnce({
      ...meta(2),
      writer_profile: {
        ...meta(2).writer_profile,
        selected_thread_id: '',
        scope_generation: 2,
      },
    });
    const client = useFocusWebClient(fake.api);

    await client.load();

    expect(fake.api.readThread).toHaveBeenCalledWith('thread-1', expect.any(Number));
    expect(fake.api.meta).toHaveBeenCalled();
    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.scopeReady.value).toBe(true);
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:draft:/work',
    );
    client.dispose();
  });

  it('queries saved prompt results only after the exact target is materialized', async () => {
    stubBrowser();
    sessionStorage.setItem('focus-web.prompt-result-locators', JSON.stringify({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-1', mutationId: PROMPT_MUTATION_ID }],
    }));
    const fake = testApi();
    const client = useFocusWebClient(fake.api);

    await client.load();
    await vi.waitFor(() => expect(fake.api.readPromptResult).toHaveBeenCalledWith(
      'thread-1',
      PROMPT_MUTATION_ID,
    ));

    const readOrder = vi.mocked(fake.api.readThread).mock.invocationCallOrder[0]!;
    const resultOrder = vi.mocked(fake.api.readPromptResult).mock.invocationCallOrder[0]!;
    expect(resultOrder).toBeGreaterThan(readOrder);
    expect(fake.api.submitPrompt).not.toHaveBeenCalled();
    expect(sessionStorage.getItem('focus-web.prompt-result-locators')).toBeNull();
    client.dispose();
  });
  it('silently drops a late initialize success after disposal', async () => {
    stubBrowser();
    const fake = testApi();
    const lateInitialize = deferred<FocusMeta>();
    vi.mocked(fake.api.initialize).mockReturnValueOnce(lateInitialize.promise);
    const client = useFocusWebClient(fake.api);

    const loading = client.load();
    expect(client.loading.value).toBe(true);
    client.dispose();
    lateInitialize.resolve(meta(1));
    await loading;

    expect(client.loading.value).toBe(false);
    expect(client.initialized.value).toBe(false);
    expect(client.meta.value).toBeNull();
    expect(client.errorMessage.value).toBe('');
    expect(fake.api.listThreads).not.toHaveBeenCalled();
    expect(fake.api.readThread).not.toHaveBeenCalled();
    expect(fake.api.connectEvents).not.toHaveBeenCalled();
    expect(history.replaceState).not.toHaveBeenCalled();
  });

  it('silently drops a late initialize error after disposal', async () => {
    stubBrowser();
    const fake = testApi();
    const lateInitialize = deferred<FocusMeta>();
    vi.mocked(fake.api.initialize).mockReturnValueOnce(lateInitialize.promise);
    const client = useFocusWebClient(fake.api);

    const loading = client.load();
    client.dispose();
    lateInitialize.reject(new Error('late initialize failure'));
    await loading;

    expect(client.loading.value).toBe(false);
    expect(client.initialized.value).toBe(false);
    expect(client.errorMessage.value).toBe('');
    expect(fake.api.connectEvents).not.toHaveBeenCalled();
  });

  it('ignores a late thread-selection response after disposal', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const lateSelection = deferred<FocusThreadSnapshot>();
    vi.mocked(fake.api.readThread).mockReturnValueOnce(lateSelection.promise);

    const selecting = client.selectThread('thread-2');
    const snapshotAtDispose = client.snapshot.value;
    const urlWritesAtDispose = vi.mocked(history.replaceState).mock.calls.length;
    client.dispose();

    expect(client.scopeReady.value).toBe(false);
    expect(client.composerReady.value).toBe(false);
    lateSelection.resolve(selectedSnapshot('thread-2', 'Late second', 1, 2));
    await selecting;

    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.meta.value?.writer_profile.selected_thread_id).toBe('thread-1');
    expect(client.snapshot.value).toBe(snapshotAtDispose);
    expect(client.scopeReady.value).toBe(false);
    expect(client.composerScopeId.value).toBe('');
    expect(history.replaceState).toHaveBeenCalledTimes(urlWritesAtDispose);
    expect(fake.api.meta).not.toHaveBeenCalled();
  });

  it('ignores a late settings success after disposal', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const lateSettings = deferred<FocusNextTurnSettingsResult>();
    vi.mocked(fake.api.updateNextTurnSettings).mockReturnValueOnce(lateSettings.promise);

    const updating = client.setPermissionsProfile(':danger-full-access');
    expect(client.permissionsProfileId.value).toBe(':workspace');
    const urlWritesAtDispose = vi.mocked(history.replaceState).mock.calls.length;
    client.dispose();
    expect(client.scopeReady.value).toBe(false);
    expect(client.permissionsProfileId.value).toBe(':workspace');

    lateSettings.resolve({
      runtime_epoch: EPOCH,
      revision: 1,
      next_turn_settings: {
        ...meta(1).next_turn_settings,
        generation: 2,
        permissions_profile_id: ':server-normalized',
      },
    });
    await updating;

    expect(client.permissionsProfileId.value).toBe(':workspace');
    expect(client.meta.value?.next_turn_settings.permissions_profile_id).toBe(
      ':workspace',
    );
    expect(client.scopeReady.value).toBe(false);
    expect(history.replaceState).toHaveBeenCalledTimes(urlWritesAtDispose);
    expect(fake.api.meta).not.toHaveBeenCalled();
  });

  it('does not report or reconcile a late settings failure after disposal', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const lateSettings = deferred<FocusNextTurnSettingsResult>();
    vi.mocked(fake.api.updateNextTurnSettings).mockReturnValueOnce(lateSettings.promise);

    const updating = client.setPermissionsProfile(':danger-full-access');
    client.dispose();
    lateSettings.reject(new Error('late settings failure'));
    await updating;

    expect(fake.api.meta).not.toHaveBeenCalled();
    expect(client.permissionsProfileId.value).toBe(':workspace');
    expect(client.scopeReady.value).toBe(false);
    expect(client.composerReady.value).toBe(false);
  });
});

describe('useFocusWebClient create-outcome navigation', () => {
  it('recovers the original draft when a partial-create thread read returns false', async () => {
    stubBrowser();
    const fake = testApi();
    const originalDraft = draftMeta(1);
    vi.mocked(fake.api.initialize).mockResolvedValueOnce(originalDraft);
    vi.mocked(fake.api.startThread).mockRejectedValueOnce(new FocusApiError(
      'Thread was created but its first turn did not start.',
      {
        status: 500,
        code: 'thread_created_turn_not_started',
        details: { thread_id: 'thread-new' },
      },
    ));
    vi.mocked(fake.api.readThread).mockResolvedValueOnce({
      ...selectedSnapshot('thread-new', 'Partial thread', 2, 3),
      selection_scope: {
        ...selectedSnapshot('thread-new', 'Partial thread', 2, 3).selection_scope,
        writer_profile: {
          ...draftMeta(2, 3).writer_profile,
          selected_thread_id: 'thread-other',
        },
      },
    });
    vi.mocked(fake.api.meta).mockResolvedValueOnce(originalDraft);
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();

    await expect(client.submit('hello')).resolves.toBe(false);

    expect(fake.api.readThread).toHaveBeenCalledWith('thread-new', expect.any(Number));
    expect(fake.api.meta).toHaveBeenCalled();
    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/draft');
    expect(client.scopeReady.value).toBe(true);
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:draft:/draft',
    );
    client.dispose();
  });

  it('reconciles a profile-changed selection clear from durable meta', async () => {
    stubBrowser();

    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:1:thread:thread-1',
    );

    vi.mocked(fake.api.meta).mockResolvedValueOnce({
      ...meta(1),
      writer_profile: {
        ...meta(1).writer_profile,
        selected_thread_id: '',
        scope_generation: 2,
      },
    });
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(
      threadList(1, 'Still listed'),
    );
    fake.handlers.event({
      type: 'profile_changed',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-1',
      reason: 'selection_cleared',
    });

    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    });

    expect(fake.api.meta).toHaveBeenCalledTimes(1);
    expect(fake.api.readThread).toHaveBeenCalledTimes(1);
    expect(client.meta.value?.writer_profile.selected_thread_id).toBe('');
    expect(client.meta.value?.writer_profile.scope_generation).toBe(2);
    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:draft:/work',
    );
    expect(history.replaceState).toHaveBeenLastCalledWith(null, '', '/');
    client.dispose();
  });

  it('keeps writes fenced when profile-changed meta reconciliation fails', async () => {
    stubBrowser();

    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    vi.mocked(fake.api.meta).mockRejectedValueOnce(new Error('meta unavailable'));

    fake.handlers.event({
      type: 'profile_changed',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-1',
      reason: 'selection_cleared',
    });

    await vi.waitFor(() => {
      expect(fake.api.meta).toHaveBeenCalledTimes(1);
    });
    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.meta.value?.writer_profile.selected_thread_id).toBe('thread-1');
    client.dispose();
  });

  it('does not let another document profile event override a pending navigation', async () => {
    stubBrowser();

    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const secondThread = {
      ...thread('Second thread'),
      id: 'thread-2',
    };
    const selectedMeta = {
      ...meta(1),
      writer_profile: {
        ...meta(1).writer_profile,
        selected_thread_id: 'thread-2',
        scope_generation: 2,
      },
    };
    const selectedSnapshot: FocusThreadSnapshot = {
      ...snapshot(1, 'Second thread'),
      thread: secondThread,
      selection_scope: {
        ...snapshot(1, 'Second thread').selection_scope,
        writer_profile: selectedMeta.writer_profile,
        scope_changed: true,
        previous_attachment_scope: 'thread:thread-1',
        current_attachment_scope: 'thread:thread-2',
        previous_scope_generation: 1,
        current_scope_generation: 2,
        attachment_scope_disposition: 'isolated',
      },
    };
    const bothThreads = {
      ...threadList(1, 'First thread'),
      threads: [thread('First thread'), secondThread],
    };
    let resolveSelection!: (value: FocusThreadSnapshot) => void;
    let markSelectionStarted!: () => void;
    const selectionStarted = new Promise<void>((resolve) => {
      markSelectionStarted = resolve;
    });
    vi.mocked(fake.api.readThread).mockClear();
    vi.mocked(fake.api.readThread).mockImplementationOnce(() => new Promise((resolve) => {
      resolveSelection = resolve;
      markSelectionStarted();
    }));

    const selection = client.selectThread('thread-2');
    await selectionStarted;
    vi.mocked(fake.api.meta).mockResolvedValueOnce(meta(1));
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(bothThreads);
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(snapshot(1, 'First thread'));
    fake.handlers.event({
      type: 'profile_changed',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-other',
      reason: 'web_profile_updated',
    });
    await client.reloadAll();

    expect(fake.api.readThread).toHaveBeenCalledTimes(1);
    expect(fake.api.readThread).toHaveBeenLastCalledWith(
      'thread-2',
      expect.any(Number),
    );
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value).toBeNull();
    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);

    resolveSelection(selectedSnapshot);
    await selection;
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshotInvalidated.value).toBe(true);

    vi.mocked(fake.api.meta).mockResolvedValueOnce(selectedMeta);
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(bothThreads);
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(selectedSnapshot);
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    }, { timeout: 2_000 });

    expect(client.meta.value?.writer_profile.selected_thread_id).toBe('thread-2');
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:2:thread:thread-2',
    );
    expect(client.canSubmit.value).toBe(true);
    client.dispose();
  });

  it('keeps the old revision floor and replays buffered events when readThread fails', async () => {
    stubBrowser();

    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    expect(client.revision.value).toBe(0);
    expect(client.canSubmit.value).toBe(true);

    vi.mocked(fake.api.meta).mockResolvedValueOnce(meta(3));
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(threadList(3, 'List at revision 3'));

    let rejectRead!: (error: Error) => void;
    let markReadStarted!: () => void;
    const readStarted = new Promise<void>((resolve) => {
      markReadStarted = resolve;
    });
    vi.mocked(fake.api.readThread).mockImplementationOnce(() => new Promise((_, reject) => {
      rejectRead = reject;
      markReadStarted();
    }));

    // A reconnect hello is only an invalidation hint.  It must not commit its
    // revision before all three HTTP resources have installed successfully.
    fake.handlers.event({ type: 'hello', runtime_epoch: EPOCH, revision: 3 });
    const failedReload = client.reloadAll();
    await readStarted;

    // Delivery may lag the HTTP reads.  This event must still replay from the
    // last committed revision when the active-thread read fails.
    fake.handlers.event({
      type: 'thread_delta',
      runtime_epoch: EPOCH,
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'thread/name/updated',
        thread_name: 'Buffered event applied',
      },
    });
    rejectRead(new Error('readThread failed'));
    await failedReload;

    expect(client.revision.value).toBe(1);
    expect(client.snapshot.value?.thread.title).toBe('Buffered event applied');
    expect(client.meta.value?.revision).toBe(0);
    expect(client.threads.value[0]?.title).toBe('Initial');
    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);

    // The invalidation remains retryable without relying on another WebSocket
    // event or a hidden caller. A complete automatic retry is the only
    // operation allowed to publish the staged resources and newer floor.
    vi.mocked(fake.api.meta).mockResolvedValueOnce(meta(3));
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(threadList(3, 'Authoritative'));
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(snapshot(3, 'Authoritative'));
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    }, { timeout: 2_000 });

    expect(client.revision.value).toBe(3);
    expect(client.snapshot.value?.thread.title).toBe('Authoritative');
    expect(client.snapshotInvalidated.value).toBe(false);
    expect(client.canSubmit.value).toBe(true);
    expect(client.errorMessage.value).toBe('');
    client.dispose();
  });

  it('restarts the composite read when a concurrent scope intent supersedes a member request', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();

    vi.mocked(fake.api.meta).mockResolvedValueOnce(meta(2));
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(threadList(2, 'First list'));
    let resolveRead!: (value: FocusThreadSnapshot) => void;
    let markReadStarted!: () => void;
    const readStarted = new Promise<void>((resolve) => {
      markReadStarted = resolve;
    });
    vi.mocked(fake.api.readThread).mockImplementationOnce(() => new Promise((resolve) => {
      resolveRead = resolve;
      markReadStarted();
    }));

    fake.handlers.event({ type: 'hello', runtime_epoch: EPOCH, revision: 2 });
    const supersededReload = client.reloadAll();
    await readStarted;

    // This increments the list generation while the composite read owns the
    // invalidated state. It must schedule a fresh composite instead of
    // publishing the now-mixed first attempt or leaving the page locked.
    await client.setThreadScope('global');
    vi.mocked(fake.api.meta).mockResolvedValueOnce(meta(2));
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce({
      ...threadList(2, 'Authoritative global list'),
      scope: 'global',
    });
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(snapshot(2, 'Authoritative global'));
    resolveRead(snapshot(2, 'Superseded active snapshot'));
    await supersededReload;

    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    }, { timeout: 2_000 });
    expect(client.revision.value).toBe(2);
    expect(client.threads.value[0]?.title).toBe('Authoritative global');
    expect(client.snapshot.value?.thread.title).toBe('Authoritative global');
    client.dispose();
  });

  it('does not roll back a settings response that completes while older meta is staged', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();

    let resolveSettings!: (value: FocusNextTurnSettingsResult) => void;
    let markSettingsStarted!: () => void;
    const settingsStarted = new Promise<void>((resolve) => {
      markSettingsStarted = resolve;
    });
    vi.mocked(fake.api.updateNextTurnSettings).mockImplementationOnce(() => new Promise((resolve) => {
      resolveSettings = resolve;
      markSettingsStarted();
    }));
    const settingsUpdate = client.setPermissionsProfile(':danger-full-access');
    await settingsStarted;

    vi.mocked(fake.api.meta).mockResolvedValueOnce({
      ...meta(2),
      next_turn_settings: {
        ...meta(2).next_turn_settings,
        generation: 2,
      },
    });
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(threadList(2, 'Reloaded list'));
    let resolveRead!: (value: FocusThreadSnapshot) => void;
    let markReadStarted!: () => void;
    const readStarted = new Promise<void>((resolve) => {
      markReadStarted = resolve;
    });
    vi.mocked(fake.api.readThread).mockImplementationOnce(() => new Promise((resolve) => {
      resolveRead = resolve;
      markReadStarted();
    }));

    fake.handlers.event({ type: 'hello', runtime_epoch: EPOCH, revision: 2 });
    const reload = client.reloadAll();
    await readStarted;

    resolveSettings({
      runtime_epoch: EPOCH,
      revision: 2,
      next_turn_settings: {
        ...meta(0).next_turn_settings,
        generation: 3,
        permissions_profile_id: ':danger-full-access',
      },
    });
    await settingsUpdate;
    resolveRead(snapshot(2, 'Reloaded snapshot'));
    await reload;

    expect(client.revision.value).toBe(2);
    expect(client.snapshotInvalidated.value).toBe(false);
    expect(client.permissionsProfileId.value).toBe(':danger-full-access');
    expect(client.meta.value?.next_turn_settings.permissions_profile_id).toBe(
      ':danger-full-access',
    );
    expect(client.meta.value?.next_turn_settings.generation).toBe(3);
    client.dispose();
  });

  it('settles a successful delete locally when its lifecycle event is lost', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();

    await expect(client.deleteThread('thread-1', 'thread-1')).resolves.toBe(true);

    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.threads.value).toEqual([]);
    expect(history.replaceState).toHaveBeenLastCalledWith(null, '', '/');
    client.dispose();
  });

  it('repairs an A to B to A replacement after delete succeeds without an event', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const deleteResult = deferred<Awaited<ReturnType<FocusWebApiPort['deleteThread']>>>();
    vi.mocked(fake.api.deleteThread).mockReturnValueOnce(deleteResult.promise);
    const deleting = client.deleteThread('thread-1', 'thread-1');

    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-2', 'Second', 1, 2),
    );
    await client.selectThread('thread-2');
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-1', 'Replacement A', 2, 3),
    );
    await client.selectThread('thread-1');

    const authoritativeMeta = deferred<FocusMeta>();
    const remainingThread = { ...thread('Second'), id: 'thread-2' };
    const authoritativeDraft = {
      ...meta(4),
      writer_profile: {
        ...meta(4).writer_profile,
        selected_thread_id: '',
        scope_generation: 4,
      },
    };
    vi.mocked(fake.api.meta)
      .mockReturnValueOnce(authoritativeMeta.promise)
      .mockResolvedValue(authoritativeDraft);
    vi.mocked(fake.api.listThreads).mockResolvedValue({
      ...threadList(4, 'Second'),
      threads: [remainingThread],
    });

    deleteResult.resolve({
      thread_id: 'thread-1',
      upstream_outcome: 'success',
      focus_cleanup: 'complete',
      cleanup_errors: [],
    });
    await expect(deleting).resolves.toBe(true);

    // No lifecycle WebSocket event is delivered. The older delete receipt
    // cannot clear replacement A locally, but it must fence writes until a
    // fresh composite read proves which target survived.
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.scopeReady.value).toBe(false);
    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);

    authoritativeMeta.resolve(authoritativeDraft);
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    });

    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.threads.value.some((item) => item.id === 'thread-1')).toBe(false);
    expect(client.scopeReady.value).toBe(true);
    expect(client.composerScopeId.value).toBe(
      'client-1:generation:4:draft:/work',
    );
    client.dispose();
  });

  it('repairs an A to B to A replacement after archive succeeds without an event', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    const archiveResult = deferred<Awaited<ReturnType<FocusWebApiPort['archiveThread']>>>();
    vi.mocked(fake.api.archiveThread).mockReturnValueOnce(archiveResult.promise);
    const archiving = client.archiveThread('thread-1');

    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-2', 'Second', 1, 2),
    );
    await client.selectThread('thread-2');
    vi.mocked(fake.api.readThread).mockResolvedValueOnce(
      selectedSnapshot('thread-1', 'Replacement A', 2, 3),
    );
    await client.selectThread('thread-1');

    const authoritativeMeta = deferred<FocusMeta>();
    const remainingThread = { ...thread('Second'), id: 'thread-2' };
    const currentThreads = {
      ...threadList(4, 'Second'),
      threads: [remainingThread],
    };
    const authoritativeDraft = {
      ...meta(4),
      writer_profile: {
        ...meta(4).writer_profile,
        selected_thread_id: '',
        scope_generation: 4,
      },
    };
    vi.mocked(fake.api.meta)
      .mockReturnValueOnce(authoritativeMeta.promise)
      .mockResolvedValue(authoritativeDraft);
    const archivedThreads = {
      ...threadList(4, 'Archived A'),
      archived: true,
    };
    vi.mocked(fake.api.listThreads).mockImplementation(async (options) => (
      options?.archived
        ? archivedThreads
        : currentThreads
    ));

    archiveResult.resolve({
      thread_id: 'thread-1',
      upstream_outcome: 'success',
      focus_cleanup: 'complete',
      cleanup_errors: [],
    });
    await expect(archiving).resolves.toBe(true);

    // The lifecycle event is intentionally absent. Directory refresh alone
    // cannot make replacement A a safe writer target.
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.scopeReady.value).toBe(false);
    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);

    authoritativeMeta.resolve(authoritativeDraft);
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    }, { timeout: 2_000 });

    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.threads.value.some((item) => item.id === 'thread-1')).toBe(false);
    expect(client.archivedThreads.value.some((item) => item.id === 'thread-1')).toBe(true);
    expect(client.scopeReady.value).toBe(true);
    client.dispose();
  });

  it('does not clear an A replacement after an A to B to A navigation', async () => {
    stubBrowser();
    const fake = testApi();
    const client = useFocusWebClient(fake.api);
    await client.load();
    fake.handlers.open?.();
    let settleDelete!: (value: Awaited<ReturnType<FocusWebApiPort['deleteThread']>>) => void;
    vi.mocked(fake.api.deleteThread).mockReturnValueOnce(new Promise((resolve) => {
      settleDelete = resolve;
    }));
    const deleting = client.deleteThread('thread-1', 'thread-1');

    const threadB = { ...thread('Second'), id: 'thread-2' };
    vi.mocked(fake.api.readThread).mockResolvedValueOnce({
      ...snapshot(1, 'Second'),
      thread: threadB,
      selection_scope: {
        ...snapshot(1, 'Second').selection_scope,
        writer_profile: {
          ...meta(1).writer_profile,
          selected_thread_id: 'thread-2',
          scope_generation: 2,
        },
      },
    });
    await client.selectThread('thread-2');
    vi.mocked(fake.api.readThread).mockResolvedValueOnce({
      ...snapshot(2, 'Replacement A'),
      selection_scope: {
        ...snapshot(2, 'Replacement A').selection_scope,
        writer_profile: {
          ...meta(2).writer_profile,
          selected_thread_id: 'thread-1',
          scope_generation: 3,
        },
      },
    });
    await client.selectThread('thread-1');

    const authoritativeMeta = deferred<FocusMeta>();
    const replacementProfile = {
      ...meta(3).writer_profile,
      scope_generation: 3,
    };
    vi.mocked(fake.api.meta).mockReturnValueOnce(authoritativeMeta.promise);
    vi.mocked(fake.api.listThreads).mockResolvedValueOnce(
      threadList(3, 'Replacement A'),
    );
    vi.mocked(fake.api.readThread).mockResolvedValueOnce({
      ...snapshot(3, 'Replacement A'),
      selection_scope: {
        ...snapshot(3, 'Replacement A').selection_scope,
        writer_profile: replacementProfile,
      },
    });

    settleDelete({
      thread_id: 'thread-1',
      upstream_outcome: 'success',
      focus_cleanup: 'complete',
      cleanup_errors: [],
    });
    await expect(deleting).resolves.toBe(true);

    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');
    expect(client.snapshot.value?.thread.title).toBe('Replacement A');
    expect(client.threads.value.some((item) => item.id === 'thread-1')).toBe(true);
    expect(history.replaceState).toHaveBeenLastCalledWith(
      null,
      '',
      '/?thread=thread-1',
    );

    authoritativeMeta.resolve({
      ...meta(3),
      writer_profile: replacementProfile,
    });
    await vi.waitFor(() => {
      expect(client.snapshotInvalidated.value).toBe(false);
    });
    expect(client.snapshot.value?.thread.title).toBe('Replacement A');
    expect(client.threads.value.some((item) => item.id === 'thread-1')).toBe(true);
    client.dispose();
  });
});
