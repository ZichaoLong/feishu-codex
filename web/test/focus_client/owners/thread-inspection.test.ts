import { describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import type { FocusWebApiPort } from '../../../src/focus/api';
import { createThreadInspection } from '../../../src/focus/client-state/thread-inspection';
import { FocusApiError } from '../../../src/focus/types';
import type {
  FocusThreadConversationSearchPage,
  FocusThreadSnapshot,
  FocusThreadToolDetailPayload,
  FocusThreadToolDetailScanPage,
  FocusToolInspectionLocator,
} from '../../../src/focus/types';

const LOCATOR: FocusToolInspectionLocator = {
  turn_id: 'raw-turn',
  item_id: 'command-1',
  kind: 'commandExecution',
  change_index: null,
};

function snapshot(): FocusThreadSnapshot {
  return {
    runtime_epoch: 'epoch-1',
    revision: 1,
    thread: {
      id: 'thread-1',
      title: 'Thread',
      name: '',
      preview: '',
      cwd: '/work',
      created_at: 0,
      updated_at: 0,
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
      loaded_instance: '',
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
    },
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
      writer_profile: {
        selected_thread_id: 'thread-1',
        working_dir: '/work',
        scope_generation: 1,
      },
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 1,
      attachment_scope_disposition: 'unchanged',
    },
  };
}

function previewToolDetail(output = 'detail'): FocusThreadToolDetailPayload {
  return {
    view: 'preview',
    tool: {
      id: 'command-1',
      name: 'exec_command',
      arg: 'printf detail',
      status: 'ok',
      output: [output],
      inspectionLocator: LOCATOR,
    },
  };
}

function fullCommandDetail(output = 'detail'): FocusThreadToolDetailPayload {
  return {
    view: 'full',
    source: {
      type: 'commandExecution',
      id: 'command-1',
      pluginId: null,
      scriptPath: null,
      command: 'printf detail',
      cwd: '/work',
      processId: null,
      source: 'agent',
      status: 'completed',
      commandActions: [],
      aggregatedOutput: output,
      exitCode: 0,
      durationMs: 1,
    },
  };
}

function toolDetailScanPage(
  detail: FocusThreadToolDetailPayload = previewToolDetail(),
  options: {
    status?: 'scanning' | 'found' | 'not_found';
    cursor?: string | null;
    next_cursor?: string | null;
    scanned_items?: number;
    view?: 'preview' | 'full';
  } = {},
): FocusThreadToolDetailScanPage {
  const status = options.status ?? 'found';
  return {
    runtime_epoch: 'epoch-1',
    revision: 2,
    thread_id: 'thread-1',
    ...LOCATOR,
    view: options.view ?? detail.view,
    status,
    cursor: options.cursor ?? null,
    next_cursor: options.next_cursor ?? null,
    scanned_items: options.scanned_items ?? 1,
    detail: status === 'found' ? detail : null,
  };
}

function searchPage(
  query: string,
  cursor: string | null = null,
): FocusThreadConversationSearchPage {
  return {
    runtime_epoch: 'epoch-1',
    revision: 3,
    thread_id: 'thread-1',
    query,
    cursor,
    occurrences: [{
      turn_id: 'raw-turn',
      item_id: 'message-1',
      snippet: `before ${query} after`,
      snippet_match_range: { start: 7, end: 7 + query.length },
      turn_cursor: 'opaque cursor',
    }],
    next_cursor: 'next cursor',
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function harness() {
  const currentSnapshot = ref<FocusThreadSnapshot | null>(snapshot());
  const activeThreadId = ref('thread-1');
  const capabilities = ref({ tool_detail: true, history_search: true });
  const accessAvailable = ref(true);
  const readToolDetail = vi.fn<FocusWebApiPort['readToolDetail']>(async () => toolDetailScanPage());
  const searchConversation = vi.fn<FocusWebApiPort['searchConversation']>(
    async (_threadId, query, cursor = null) => searchPage(query, cursor),
  );
  const resolveTurnCursorTarget = vi.fn(async () => 'raw-turn:user');
  const cancelTurnCursorTarget = vi.fn();
  const reportError = vi.fn();
  const externallyDisposed = ref(false);
  const owner = createThreadInspection({
    api: { readToolDetail, searchConversation },
    metaCapabilities: capabilities,
    accessAvailable,
    snapshot: currentSnapshot,
    activeThreadId,
    resolveTurnCursorTarget,
    cancelTurnCursorTarget,
    reportError,
    isDisposed: () => externallyDisposed.value,
  });
  return {
    owner,
    currentSnapshot,
    activeThreadId,
    capabilities,
    accessAvailable,
    readToolDetail,
    searchConversation,
    resolveTurnCursorTarget,
    cancelTurnCursorTarget,
    reportError,
    externallyDisposed,
  };
}

describe('thread inspection browser owner', () => {
  it('admits only exact paginated materialized threads with enabled build capabilities', async () => {
    const h = harness();
    expect(h.owner.toolDetailAvailable.value).toBe(true);
    expect(h.owner.historySearchAvailable.value).toBe(true);
    expect(h.owner.toolDetailUnavailableReason.value).toBeNull();
    expect(h.owner.historySearchUnavailableReason.value).toBeNull();

    h.currentSnapshot.value = {
      ...snapshot(),
      thread: { ...snapshot().thread, history_mode: 'legacy' },
    };
    expect(h.owner.toolDetailAvailable.value).toBe(false);
    expect(h.owner.historySearchAvailable.value).toBe(false);
    expect(h.owner.toolDetailUnavailableReason.value).toBe('legacy_history');
    expect(h.owner.historySearchUnavailableReason.value).toBe('legacy_history');
    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(h.readToolDetail).not.toHaveBeenCalled();
    expect(h.searchConversation).not.toHaveBeenCalled();

    h.currentSnapshot.value = snapshot();
    h.capabilities.value = { tool_detail: false, history_search: false };
    expect(h.owner.toolDetailAvailable.value).toBe(false);
    expect(h.owner.historySearchAvailable.value).toBe(false);
    expect(h.owner.toolDetailUnavailableReason.value).toBe('build_unsupported');
    expect(h.owner.historySearchUnavailableReason.value).toBe('build_unsupported');
    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(h.readToolDetail).not.toHaveBeenCalled();
    expect(h.searchConversation).not.toHaveBeenCalled();

    h.currentSnapshot.value = {
      ...h.currentSnapshot.value,
      thread: { ...h.currentSnapshot.value.thread, history_mode: 'legacy' },
    };
    expect(h.owner.toolDetailUnavailableReason.value).toBe('legacy_history');
    expect(h.owner.historySearchUnavailableReason.value).toBe('legacy_history');
  });

  it('distinguishes closed local admission reasons without guessing legacy history', () => {
    const h = harness();

    h.activeThreadId.value = '';
    expect(h.owner.historySearchUnavailableReason.value).toBe('no_active_thread');

    h.activeThreadId.value = 'thread-1';
    h.currentSnapshot.value = null;
    expect(h.owner.historySearchUnavailableReason.value).toBe('thread_not_materialized');

    h.currentSnapshot.value = {
      ...snapshot(),
      thread: { ...snapshot().thread, history_mode: 'unknown' },
    };
    expect(h.owner.historySearchUnavailableReason.value).toBe('unknown_history');

    h.accessAvailable.value = false;
    expect(h.owner.historySearchUnavailableReason.value).toBe('document_unavailable');
  });

  it('installs classified request-local unsupported reasons and clears them on identity change', async () => {
    const h = harness();
    h.readToolDetail.mockRejectedValueOnce(new FocusApiError('unsupported', {
      status: 503,
      code: 'thread_inspection_upstream_unsupported',
    }));
    h.searchConversation.mockRejectedValueOnce(new FocusApiError('unsupported', {
      status: 503,
      code: 'thread_inspection_upstream_unsupported',
    }));

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(h.owner.toolDetailUnavailableReason.value).toBe('runtime_unsupported');
    expect(h.owner.historySearchUnavailableReason.value).toBe('runtime_unsupported');

    h.currentSnapshot.value = { ...snapshot(), runtime_epoch: 'epoch-2' };
    expect(h.owner.toolDetailUnavailableReason.value).toBeNull();
    expect(h.owner.historySearchUnavailableReason.value).toBeNull();
  });

  it('maps classified selection and history failures without disabling unclassified retries', async () => {
    const cases = [
      ['thread_not_selected', 'thread_not_materialized'],
      ['thread_inspection_unavailable', 'unknown_history'],
    ] as const;
    for (const [code, reason] of cases) {
      const h = harness();
      h.searchConversation.mockRejectedValueOnce(new FocusApiError(code, { status: 409, code }));
      await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
      expect(h.owner.historySearchUnavailableReason.value).toBe(reason);
    }

    const retryable = harness();
    retryable.searchConversation.mockRejectedValueOnce(new Error('transport failed'));
    await expect(retryable.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(retryable.owner.historySearchUnavailableReason.value).toBeNull();
    expect(retryable.owner.historySearchAvailable.value).toBe(true);
    expect(retryable.owner.searchError.value).toBe(true);
  });

  it('silently drops inspection reads replaced by a newer document', async () => {
    const h = harness();
    const stale = new FocusApiError('stale document read', {
      status: 409,
      code: 'stale_document_read',
    });
    h.readToolDetail.mockRejectedValueOnce(stale);
    h.searchConversation.mockRejectedValueOnce(stale);

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);

    expect(h.owner.toolDetailError.value).toBe(false);
    expect(h.owner.searchError.value).toBe(false);
    expect(h.reportError).not.toHaveBeenCalled();
  });

  it('does not let an obsolete unsupported failure disable a newer successful request', async () => {
    const h = harness();
    const obsolete = deferred<FocusThreadConversationSearchPage>();
    let requestCount = 0;
    h.searchConversation.mockImplementation(async (_thread, query, cursor) => {
      requestCount += 1;
      if (requestCount === 1) return obsolete.promise;
      return searchPage(query, cursor);
    });

    const first = h.owner.searchConversation({ query: 'old' });
    await expect(h.owner.searchConversation({ query: 'new' })).resolves.toBe(true);
    obsolete.reject(new FocusApiError('unsupported', {
      status: 503,
      code: 'thread_inspection_upstream_unsupported',
    }));
    await expect(first).resolves.toBe(false);
    expect(h.owner.historySearchUnavailableReason.value).toBeNull();
    expect(h.owner.historySearchAvailable.value).toBe(true);
    expect(h.owner.searchPage.value?.query).toBe('new');
  });

  it('uses independent replaceable tool and search request slots', async () => {
    const h = harness();
    const firstTool = deferred<FocusThreadToolDetailScanPage>();
    const secondTool = deferred<FocusThreadToolDetailScanPage>();
    const firstSearch = deferred<FocusThreadConversationSearchPage>();
    const toolSignals: AbortSignal[] = [];
    const searchSignals: AbortSignal[] = [];
    h.readToolDetail.mockImplementation(async (_thread, _locator, _view, signal) => {
      toolSignals.push(signal!);
      return toolSignals.length === 1 ? firstTool.promise : secondTool.promise;
    });
    h.searchConversation.mockImplementation(async (_thread, _query, _cursor, signal) => {
      searchSignals.push(signal!);
      return firstSearch.promise;
    });

    const search = h.owner.searchConversation({ query: 'needle' });
    const oldTool = h.owner.readToolDetail(LOCATOR);
    const newTool = h.owner.readToolDetail(LOCATOR);
    expect(toolSignals[0]?.aborted).toBe(true);
    expect(toolSignals[1]?.aborted).toBe(false);
    expect(searchSignals[0]?.aborted).toBe(false);

    firstTool.resolve(toolDetailScanPage(previewToolDetail('old')));
    secondTool.resolve(toolDetailScanPage(previewToolDetail('new')));
    firstSearch.resolve(searchPage('needle'));
    await expect(oldTool).resolves.toBe(false);
    await expect(newTool).resolves.toBe(true);
    await expect(search).resolves.toBe(true);
    expect(h.owner.toolDetail.value).toMatchObject({
      view: 'preview',
      tool: { output: ['new'] },
    });
    expect(h.owner.searchPage.value?.query).toBe('needle');
  });

  it('follows opaque tool cursors and reports page progress separately from display output', async () => {
    const h = harness();
    const pages: FocusThreadToolDetailScanPage[] = [
      toolDetailScanPage(previewToolDetail(), {
        status: 'scanning',
        cursor: null,
        next_cursor: 'next-page',
        scanned_items: 100,
      }),
      toolDetailScanPage(previewToolDetail('complete'), {
        cursor: 'next-page',
        scanned_items: 2,
      }),
    ];
    h.readToolDetail.mockImplementation(async (_thread, _locator, _view, _signal, cursor) => {
      const page = cursor === null ? pages[0] : pages[1];
      if (!page) throw new Error('missing test page');
      return page;
    });

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(true);
    expect(h.readToolDetail.mock.calls.map((call) => call[4])).toEqual([null, 'next-page']);
    expect(h.owner.toolDetailScannedItems.value).toBe(102);
    expect(h.owner.toolDetailScanStatus.value).toBe('found');
    expect(h.owner.toolDetail.value).toMatchObject({
      view: 'preview',
      tool: { output: ['complete'] },
    });
  });

  it('requires the same found preview before a fresh full read and clears the sole slot', async () => {
    const h = harness();
    h.readToolDetail.mockImplementation(async (_thread, _locator, view) => (
      view === 'preview'
        ? toolDetailScanPage(previewToolDetail('bounded preview'))
        : toolDetailScanPage(fullCommandDetail('complete persisted output'))
    ));

    await expect(h.owner.readFullToolDetail(LOCATOR)).resolves.toBe(false);
    expect(h.readToolDetail).not.toHaveBeenCalled();

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(true);
    await expect(h.owner.readFullToolDetail({ ...LOCATOR, item_id: 'other-command' })).resolves.toBe(false);
    expect(h.readToolDetail.mock.calls.map((call) => call[2])).toEqual(['preview']);
    await expect(h.owner.readFullToolDetail(LOCATOR)).resolves.toBe(true);

    expect(h.readToolDetail.mock.calls.map((call) => call[2])).toEqual(['preview', 'full']);
    expect(h.owner.toolDetail.value).toMatchObject({
      view: 'full',
      source: { aggregatedOutput: 'complete persisted output' },
    });
    expect(h.owner.toolDetailLocator.value).toEqual(LOCATOR);

    h.owner.clearToolDetail();
    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.toolDetailLocator.value).toBeNull();
  });

  it('keeps the found preview in the sole slot if its full re-read fails', async () => {
    const h = harness();
    h.readToolDetail.mockImplementation(async (_thread, _locator, view) => {
      if (view === 'preview') return toolDetailScanPage(previewToolDetail('bounded preview'));
      throw new Error('full source read failed');
    });

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(true);
    await expect(h.owner.readFullToolDetail(LOCATOR)).resolves.toBe(false);

    expect(h.owner.toolDetail.value).toMatchObject({
      view: 'preview',
      tool: { output: ['bounded preview'] },
    });
    expect(h.owner.toolDetailLocator.value).toEqual(LOCATOR);
    expect(h.owner.toolDetailError.value).toBe(true);
  });

  it('keeps a complete scan miss distinct from a transport error', async () => {
    const h = harness();
    h.readToolDetail.mockResolvedValue(toolDetailScanPage(previewToolDetail(), {
      status: 'not_found',
      scanned_items: 37,
    }));

    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    expect(h.owner.toolDetailScanStatus.value).toBe('not_found');
    expect(h.owner.toolDetailScannedItems.value).toBe(37);
    expect(h.owner.toolDetailError.value).toBe(false);
    expect(h.reportError).not.toHaveBeenCalled();
  });

  it('cancels a pending scan and aborts the current HTTP request', async () => {
    const h = harness();
    const pending = deferred<FocusThreadToolDetailScanPage>();
    let signal: AbortSignal | undefined;
    h.readToolDetail.mockImplementation(async (_thread, _locator, _view, requestSignal) => {
      signal = requestSignal;
      return pending.promise;
    });

    const request = h.owner.readToolDetail(LOCATOR);
    await Promise.resolve();
    h.owner.cancelToolDetail();

    expect(signal?.aborted).toBe(true);
    expect(h.owner.toolDetailScanStatus.value).toBe('cancelled');
    expect(h.owner.toolDetailLoading.value).toBe(false);
    pending.resolve(toolDetailScanPage());
    await expect(request).resolves.toBe(false);
    expect(h.owner.toolDetail.value).toBeNull();
  });

  it('replaces rather than accumulates search pages without clearing tool detail', async () => {
    const h = harness();
    await h.owner.readToolDetail(LOCATOR);
    await h.owner.searchConversation({ query: 'needle' });
    await h.owner.searchConversation({ query: 'needle', cursor: 'next cursor' });

    expect(h.owner.toolDetailLocator.value?.item_id).toBe('command-1');
    expect(h.owner.searchPage.value).toEqual(searchPage('needle', 'next cursor'));
    expect(h.owner.searchPage.value?.occurrences).toHaveLength(1);
    expect(h.searchConversation).toHaveBeenLastCalledWith(
      'thread-1',
      'needle',
      'next cursor',
      expect.any(AbortSignal),
    );
  });

  it('refuses a stale occurrence after its search page is replaced', async () => {
    const h = harness();
    await h.owner.searchConversation({ query: 'needle' });
    const staleOccurrence = h.owner.searchPage.value!.occurrences[0]!;
    await h.owner.searchConversation({ query: 'needle', cursor: 'next cursor' });

    await expect(h.owner.resolveSearchOccurrence(staleOccurrence)).resolves.toBeNull();
    expect(h.resolveTurnCursorTarget).not.toHaveBeenCalled();
  });

  it('rejects stale runtime responses and clears both slots on identity change', async () => {
    const h = harness();
    h.readToolDetail.mockResolvedValue({
      ...toolDetailScanPage(),
      runtime_epoch: 'epoch-old',
    });
    h.searchConversation.mockResolvedValue({ ...searchPage('needle'), runtime_epoch: 'epoch-old' });
    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.searchPage.value).toBeNull();

    h.readToolDetail.mockResolvedValue(toolDetailScanPage());
    h.searchConversation.mockResolvedValue(searchPage('needle'));
    await h.owner.readToolDetail(LOCATOR);
    await h.owner.searchConversation({ query: 'needle' });
    h.currentSnapshot.value = {
      ...snapshot(),
      runtime_epoch: 'epoch-2',
    };
    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.searchPage.value).toBeNull();
  });

  it('cancels a pending search navigation before its history page can install', async () => {
    const h = harness();
    const navigation = deferred<string>();
    h.resolveTurnCursorTarget.mockReturnValue(navigation.promise);
    await h.owner.searchConversation({ query: 'needle' });
    const result = h.owner.resolveSearchOccurrence(
      h.owner.searchPage.value!.occurrences[0]!,
    );
    await Promise.resolve();

    h.owner.clearSearch();
    expect(h.cancelTurnCursorTarget).toHaveBeenCalledTimes(1);
    navigation.resolve('raw-turn:user');
    await expect(result).resolves.toBeNull();
    expect(h.owner.searchPage.value).toBeNull();
  });

  it('does not cancel an unrelated installed history window when no search jump is pending', async () => {
    const h = harness();
    await h.owner.searchConversation({ query: 'needle' });
    h.owner.clearSearch();
    expect(h.cancelTurnCursorTarget).not.toHaveBeenCalled();
  });

  it('aborts and releases all browser inspection content on dispose', async () => {
    const h = harness();
    const detail = deferred<FocusThreadToolDetailScanPage>();
    const page = deferred<FocusThreadConversationSearchPage>();
    let toolSignal: AbortSignal | undefined;
    let searchSignal: AbortSignal | undefined;
    h.readToolDetail.mockImplementation(async (_thread, _locator, _view, signal) => {
      toolSignal = signal;
      return detail.promise;
    });
    h.searchConversation.mockImplementation(async (_thread, _query, _cursor, signal) => {
      searchSignal = signal;
      return page.promise;
    });
    const detailRequest = h.owner.readToolDetail(LOCATOR);
    const searchRequest = h.owner.searchConversation({ query: 'needle' });
    h.owner.dispose();

    expect(toolSignal?.aborted).toBe(true);
    expect(searchSignal?.aborted).toBe(true);
    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.searchPage.value).toBeNull();
    detail.resolve(toolDetailScanPage());
    page.resolve(searchPage('needle'));
    await expect(detailRequest).resolves.toBe(false);
    await expect(searchRequest).resolves.toBe(false);
  });

  it('revokes in-flight requests and installed payloads with mode or capability admission', async () => {
    const h = harness();
    const detail = deferred<FocusThreadToolDetailScanPage>();
    const page = deferred<FocusThreadConversationSearchPage>();
    let toolSignal: AbortSignal | undefined;
    let searchSignal: AbortSignal | undefined;
    h.readToolDetail.mockImplementation(async (_thread, _locator, _view, signal) => {
      toolSignal = signal;
      return detail.promise;
    });
    h.searchConversation.mockImplementation(async (_thread, _query, _cursor, signal) => {
      searchSignal = signal;
      return page.promise;
    });

    const detailRequest = h.owner.readToolDetail(LOCATOR);
    const searchRequest = h.owner.searchConversation({ query: 'needle' });
    h.capabilities.value = { tool_detail: false, history_search: false };

    expect(toolSignal?.aborted).toBe(true);
    expect(searchSignal?.aborted).toBe(true);
    detail.resolve(toolDetailScanPage());
    page.resolve(searchPage('needle'));
    await expect(detailRequest).resolves.toBe(false);
    await expect(searchRequest).resolves.toBe(false);
    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.searchPage.value).toBeNull();

    h.capabilities.value = { tool_detail: true, history_search: true };
    h.readToolDetail.mockResolvedValue(toolDetailScanPage());
    await h.owner.readToolDetail(LOCATOR);
    expect(h.owner.toolDetail.value).not.toBeNull();
    h.currentSnapshot.value = {
      ...snapshot(),
      thread: { ...snapshot().thread, history_mode: 'legacy' },
    };
    expect(h.owner.toolDetail.value).toBeNull();
  });

  it('does not dispatch new requests after either disposal boundary closes', async () => {
    const locallyDisposed = harness();
    expect(locallyDisposed.owner.toolDetailAvailable.value).toBe(true);
    locallyDisposed.owner.dispose();
    expect(locallyDisposed.owner.toolDetailAvailable.value).toBe(false);
    expect(locallyDisposed.owner.historySearchAvailable.value).toBe(false);
    expect(locallyDisposed.owner.toolDetailUnavailableReason.value).toBe('document_unavailable');
    await expect(locallyDisposed.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(locallyDisposed.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(locallyDisposed.readToolDetail).not.toHaveBeenCalled();
    expect(locallyDisposed.searchConversation).not.toHaveBeenCalled();

    const externallyDisposed = harness();
    expect(externallyDisposed.owner.toolDetailAvailable.value).toBe(true);
    externallyDisposed.externallyDisposed.value = true;
    expect(externallyDisposed.owner.toolDetailAvailable.value).toBe(false);
    expect(externallyDisposed.owner.historySearchAvailable.value).toBe(false);
    expect(externallyDisposed.owner.toolDetailUnavailableReason.value).toBe('document_unavailable');
    await expect(externallyDisposed.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(externallyDisposed.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(externallyDisposed.readToolDetail).not.toHaveBeenCalled();
    expect(externallyDisposed.searchConversation).not.toHaveBeenCalled();
  });

  it('aborts and clears both slots when the external disposal boundary closes', async () => {
    const installed = harness();
    await installed.owner.readToolDetail(LOCATOR);
    await installed.owner.searchConversation({ query: 'needle' });
    installed.externallyDisposed.value = true;
    expect(installed.owner.toolDetail.value).toBeNull();
    expect(installed.owner.searchPage.value).toBeNull();

    const pending = harness();
    const detail = deferred<FocusThreadToolDetailScanPage>();
    const page = deferred<FocusThreadConversationSearchPage>();
    let toolSignal: AbortSignal | undefined;
    let searchSignal: AbortSignal | undefined;
    pending.readToolDetail.mockImplementation(async (_thread, _locator, _view, signal) => {
      toolSignal = signal;
      return detail.promise;
    });
    pending.searchConversation.mockImplementation(async (_thread, _query, _cursor, signal) => {
      searchSignal = signal;
      return page.promise;
    });
    const detailRequest = pending.owner.readToolDetail(LOCATOR);
    const searchRequest = pending.owner.searchConversation({ query: 'needle' });

    pending.externallyDisposed.value = true;
    expect(toolSignal?.aborted).toBe(true);
    expect(searchSignal?.aborted).toBe(true);
    detail.resolve(toolDetailScanPage());
    page.resolve(searchPage('needle'));
    await expect(detailRequest).resolves.toBe(false);
    await expect(searchRequest).resolves.toBe(false);
    expect(pending.owner.toolDetail.value).toBeNull();
    expect(pending.owner.searchPage.value).toBeNull();
  });

  it('revokes content and new requests when current-document access closes', async () => {
    const h = harness();
    await h.owner.readToolDetail(LOCATOR);
    await h.owner.searchConversation({ query: 'needle' });
    expect(h.owner.toolDetail.value).not.toBeNull();
    expect(h.owner.searchPage.value).not.toBeNull();

    h.accessAvailable.value = false;

    expect(h.owner.toolDetail.value).toBeNull();
    expect(h.owner.searchPage.value).toBeNull();
    expect(h.owner.toolDetailAvailable.value).toBe(false);
    expect(h.owner.historySearchAvailable.value).toBe(false);
    await expect(h.owner.readToolDetail(LOCATOR)).resolves.toBe(false);
    await expect(h.owner.searchConversation({ query: 'needle' })).resolves.toBe(false);
    expect(h.readToolDetail).toHaveBeenCalledTimes(1);
    expect(h.searchConversation).toHaveBeenCalledTimes(1);
  });
});
