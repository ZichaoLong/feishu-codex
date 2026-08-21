import { describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import type { ChatTurn, ToolCall } from '../../../src/types';
import { buildToolOutputLineWindow } from '../../../src/components/chat/tool-calls/ToolOutputBlock.vue';
import type { FocusWebApiPort } from '../../../src/focus/api';
import { FocusApiError } from '../../../src/focus/types';
import {
  boundRecentFullTurns,
  createFocusHistoryNavigation,
  FOCUS_HISTORY_OUTLINE_PAGE_LIMIT,
  FOCUS_HISTORY_PROMPT_LIMIT,
  FOCUS_RECENT_FULL_TURN_LIMIT,
} from '../../../src/focus/focusHistoryNavigation';
import {
  TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS,
  TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS,
  toolOutputCodePointLength,
} from '../../../src/focus/toolOutputPresentation';
import type {
  FocusSummaryPrompt,
  FocusThreadSnapshot,
  FocusTurnPage,
} from '../../../src/focus/types';

const EPOCH = 'epoch-1';

function user(rawTurnId: string, text = rawTurnId): ChatTurn {
  return { id: `${rawTurnId}:user`, role: 'user', no: 1, text };
}

function snapshot(turns: ChatTurn[], cursor = 'older-1'): FocusThreadSnapshot {
  return {
    runtime_epoch: EPOCH,
    revision: 0,
    thread: {
      id: 'thread-1', title: 'Thread', name: '', preview: '', cwd: '/work',
      created_at: 0, updated_at: 0, source: 'appServer', status: 'idle',
      active_flags: [], model_provider: '', service_name: '',
      session_id: '', parent_thread_id: '', can_accept_direct_input: null,
      thread_source: '', ephemeral: false, agent_nickname: '', agent_role: '',
      subagent_kind: '',
      owner: { kind: 'none', holder_id: '', relation: 'none', label: 'None' },
      pending_interaction: 'none', loaded_instance: '', loaded_state_verified: true,
      observed_here: true,
      selectable: true, unavailable_reason: '',
      history_mode: 'paginated',
      action_capabilities: {
        rename: false, archive: false, unarchive: false, delete: false,
        compact: false, fork: false, export: false, review: false, goal: false,
      },
    },
    turns,
    active_turn_id: '', active_turn_status: '', active_turn_context: null,
    pending_requests: [], tasks: [], older_turn_cursor: cursor,
    has_more_turns: Boolean(cursor), goal: null, token_usage: null,
    token_usage_available: false, mutation_unknown: null,
    selection_scope: {
      writer_profile: {
        selected_thread_id: 'thread-1', working_dir: '/work', scope_generation: 1,
      },
      scope_changed: false, previous_attachment_scope: '',
      current_attachment_scope: 'thread:thread-1', previous_scope_generation: 1,
      current_scope_generation: 1, attachment_scope_disposition: 'unchanged',
    },
  };
}

function summaryPage(
  rawTurnIds: string[],
  pageCursor: string,
  olderCursor = '',
): FocusTurnPage {
  const turns: FocusSummaryPrompt[] = rawTurnIds.map((id, index) => ({
    id: `${id}:user`, role: 'user', no: index + 1, text: id, title_truncated: false,
  }));
  return {
    runtime_epoch: EPOCH, revision: 0, items_view: 'summary',
    page_cursor: pageCursor, turns, older_turn_cursor: olderCursor,
    has_more_turns: Boolean(olderCursor),
  };
}

function fullPage(rawTurnIds: string[], pageCursor: string): FocusTurnPage {
  return {
    runtime_epoch: EPOCH, revision: 0, items_view: 'full', page_cursor: pageCursor,
    turns: rawTurnIds.map((id) => user(id)), older_turn_cursor: '',
    has_more_turns: false,
  };
}

function fullPageWithBoundedTools(target: string, pageCursor: string): FocusTurnPage {
  const visibleOutput = '😀'.repeat(
    TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS / TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS,
  );
  const visibleTools: ToolCall[] = Array.from(
    { length: TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS },
    (_, index) => ({
      id: `${target}:visible-${index}`,
      name: 'exec_command',
      arg: '',
      status: 'ok',
      output: [visibleOutput],
    }),
  );
  const fullyOmittedTools: ToolCall[] = Array.from({ length: 4 }, (_, index) => ({
    id: `${target}:omitted-${index}`,
    name: 'exec_command',
    arg: '',
    status: 'ok',
    output: [],
    outputTruncated: true,
    outputOmittedChars: 100_000 + index,
    outputHeadLineCount: 0,
  }));
  const tools = [...visibleTools, ...fullyOmittedTools];
  return {
    runtime_epoch: EPOCH,
    revision: 0,
    items_view: 'full',
    page_cursor: pageCursor,
    turns: [
      user(target),
      {
        id: `${target}:assistant`,
        role: 'assistant',
        no: 1,
        text: '',
        tools,
        blocks: tools.map((tool) => ({ kind: 'tool', tool })),
      },
    ],
    older_turn_cursor: '',
    has_more_turns: false,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function harness(initialTurns: ChatTurn[], responder: (
  threadId: string,
  cursor: string,
  itemsView: 'summary' | 'full',
) => Promise<FocusTurnPage>, initialCursor = 'older-1') {
  const currentSnapshot = ref<FocusThreadSnapshot | null>(snapshot(initialTurns, initialCursor));
  const activeThreadId = ref('thread-1');
  const turnLimit = ref(FOCUS_RECENT_FULL_TURN_LIMIT);
  const listOlderTurns = vi.fn(responder);
  const reportError = vi.fn();
  const owner = createFocusHistoryNavigation({
    api: { listOlderTurns } as Pick<FocusWebApiPort, 'listOlderTurns'>,
    snapshot: currentSnapshot,
    activeThreadId,
    turnLimit,
    reportError,
    isDisposed: () => false,
  });
  return { owner, currentSnapshot, activeThreadId, turnLimit, listOlderTurns, reportError };
}

async function settleOwner(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

describe('Focus history navigation owner', () => {
  it('bounds complete user/assistant segments by raw turn rather than message count', () => {
    const turns = Array.from({ length: 12 }, (_, index) => [
      user(`raw-${index}`),
      {
        id: `raw-${index}:assistant`,
        role: 'assistant' as const,
        no: 1,
        text: `answer-${index}`,
      },
    ]).flat();

    const bounded = boundRecentFullTurns(turns);

    expect(bounded).toHaveLength(20);
    expect(bounded.at(0)?.id).toBe('raw-2:user');
    expect(bounded.at(-1)?.id).toBe('raw-11:assistant');
  });

  it('groups a canonical compaction-only raw turn without adjacent-role guesses', () => {
    const turns = [
      ...Array.from({ length: 10 }, (_, index) => user(`raw-${index}`)),
      {
        id: 'raw-10:compaction',
        role: 'compaction' as const,
        no: 1,
        text: '',
      },
    ];

    const bounded = boundRecentFullTurns(turns);

    expect(bounded).toHaveLength(FOCUS_RECENT_FULL_TURN_LIMIT);
    expect(bounded.at(0)?.id).toBe('raw-1:user');
    expect(bounded.at(-1)?.id).toBe('raw-10:compaction');
  });

  it('keeps compaction then user segments in their canonical enclosing raw turn', () => {
    const turns = [
      ...Array.from({ length: 9 }, (_, index) => user(`raw-${index}`)),
      {
        id: 'raw-9:compaction',
        role: 'compaction' as const,
        no: 1,
        text: '',
      },
      user('raw-9', 'after compaction'),
    ];

    expect(boundRecentFullTurns(turns).map((turn) => turn.id)).toEqual(
      turns.map((turn) => turn.id),
    );
  });

  it('does not attach a previous raw turn compaction to the following assistant', () => {
    const turns: ChatTurn[] = [
      ...Array.from({ length: 9 }, (_, index) => user(`raw-${index}`)),
      { id: 'raw-9:compaction', role: 'compaction', no: 1, text: '' },
      { id: 'raw-10:assistant', role: 'assistant', no: 1, text: 'next raw turn' },
    ];

    const bounded = boundRecentFullTurns(turns);

    expect(bounded).toHaveLength(FOCUS_RECENT_FULL_TURN_LIMIT);
    expect(bounded.map((turn) => turn.id)).not.toContain('raw-0:user');
    expect(bounded.map((turn) => turn.id)).toContain('raw-9:compaction');
    expect(bounded.at(-1)?.id).toBe('raw-10:assistant');
  });

  it('loads only one summary page initially and extends the outline explicitly', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      expect(view).toBe('summary');
      return cursor === 'older-1'
        ? summaryPage(['old-1'], 'page-1', 'older-2')
        : summaryPage(['old-2'], 'page-2');
    });
    await settleOwner();

    expect(h.listOlderTurns).toHaveBeenCalledTimes(1);
    expect(h.owner.outline.value.map((item) => item.id)).toEqual([
      'old-1:user', 'recent:user',
    ]);
    expect(h.owner.outlineHasMore.value).toBe(true);

    await h.owner.loadMoreOutline();
    expect(h.listOlderTurns).toHaveBeenCalledTimes(2);
    expect(h.owner.outline.value.map((item) => item.id)).toEqual([
      'old-2:user', 'old-1:user', 'recent:user',
    ]);
    expect(h.owner.outlineHasMore.value).toBe(false);
  });

  it('installs recent Prompts immediately while the initial summary is pending', async () => {
    const pendingSummary = deferred<FocusTurnPage>();
    const h = harness([user('recent')], async (_thread, _cursor, view) => {
      expect(view).toBe('summary');
      return pendingSummary.promise;
    });

    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual(['recent:user']);
    expect(h.owner.outlineLoading.value).toBe(true);

    pendingSummary.resolve(summaryPage(['older'], 'older-page'));
    await vi.waitFor(() => {
      expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual([
        'older:user', 'recent:user',
      ]);
    });
  });

  it('does not let a pending summary overwrite a newer live Prompt', async () => {
    const pendingSummary = deferred<FocusTurnPage>();
    const h = harness([user('recent-a')], async () => pendingSummary.promise);
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual(['recent-a:user']);

    h.currentSnapshot.value = snapshot([user('recent-a'), user('recent-b')], 'older-1');
    await settleOwner();
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual([
      'recent-a:user', 'recent-b:user',
    ]);

    pendingSummary.resolve(summaryPage(['older'], 'older-page'));
    await vi.waitFor(() => expect(h.owner.outlineLoading.value).toBe(false));
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual([
      'older:user', 'recent-a:user', 'recent-b:user',
    ]);
  });

  it('keeps an initial Prompt after eleven later turns and lazy-resolves it from head', async () => {
    const initial = Array.from({ length: 10 }, (_, index) => user(`initial-${index}`));
    const h = harness(initial, async (_thread, cursor, view) => {
      if (view === 'summary' && cursor === 'older-1') {
        return summaryPage(['older'], 'older-page');
      }
      if (view === 'summary' && cursor === '') {
        return summaryPage(['initial-0'], 'initial-page');
      }
      if (view === 'full' && cursor === 'initial-page') {
        return fullPage(['initial-0'], 'initial-page');
      }
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();

    // Eleven later raw turns evict the entire initial detail tail. The outline
    // keeps its bounded Prompt entries without retaining those full turns.
    const accumulated = [...initial];
    for (let index = 0; index < 11; index += 1) {
      accumulated.push(user(`live-${index}`));
      h.currentSnapshot.value = snapshot(accumulated.slice(-10));
      await settleOwner();
    }
    expect(h.currentSnapshot.value?.turns).toHaveLength(10);
    expect(h.owner.outline.value.some((item) => item.id === 'initial-0:user')).toBe(true);

    await h.owner.resolvePromptTarget('initial-0:user');
    expect(h.listOlderTurns).toHaveBeenCalledWith('thread-1', '', 'summary');
    expect(h.listOlderTurns).toHaveBeenCalledWith('thread-1', 'initial-page', 'full');
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toContain('initial-0:user');
  });

  it('retains only the latest bounded detail window across twenty page visits', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      const target = cursor.slice('detail-'.length);
      const turns = [
        ...Array.from({ length: 12 }, (_, index) => `extra-${target}-${index}`),
        target,
      ];
      return fullPage(turns, cursor);
    });
    await settleOwner();
    h.owner.outline.value = Array.from({ length: 20 }, (_, index) => ({
      id: `target-${index}:user`,
      role: 'user' as const,
      no: index + 1,
      title: `target-${index}`,
      titleTruncated: false,
      pageCursor: `detail-target-${index}`,
      recent: false,
    }));

    for (let index = 0; index < 20; index += 1) {
      await h.owner.resolvePromptTarget(`target-${index}:user`);
      expect(h.owner.historyWindow.value?.pageCursor).toBe(`detail-target-${index}`);
      expect(h.owner.visibleTurns.value).toHaveLength(FOCUS_RECENT_FULL_TURN_LIMIT);
      expect(h.owner.visibleTurns.value.at(-1)?.id).toBe(`target-${index}:user`);
    }

    expect(h.owner.historyWindow.value?.turns).toEqual(h.owner.visibleTurns.value);
  });

  it('retires old-width locators and uses one exact new width for summary and detail', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['target'], 'page-20');
      return fullPage(['target'], cursor);
    });
    await settleOwner();
    h.turnLimit.value = 20;
    h.owner.resetForTurnLimitChange();
    await h.owner.completeTurnLimitChange(true);

    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.listOlderTurns).toHaveBeenLastCalledWith(
      'thread-1', 'older-1', 'summary', 20,
    );
    await h.owner.resolvePromptTarget('target:user');
    expect(h.listOlderTurns).toHaveBeenLastCalledWith(
      'thread-1', 'page-20', 'full', 20,
    );
    expect(h.owner.visibleTurns.value).toHaveLength(1);
  });

  it('restores recent Prompts before a new-width summary finishes', async () => {
    const pendingNewWidth = deferred<FocusTurnPage>();
    let summaryReads = 0;
    const h = harness([user('recent')], async (_thread, _cursor, view) => {
      expect(view).toBe('summary');
      summaryReads += 1;
      if (summaryReads === 1) return summaryPage(['older'], 'page-10');
      return pendingNewWidth.promise;
    });
    await vi.waitFor(() => expect(h.owner.outlineLoading.value).toBe(false));

    h.turnLimit.value = 20;
    h.owner.resetForTurnLimitChange();
    expect(h.owner.outline.value).toEqual([]);
    const rebuilding = h.owner.completeTurnLimitChange(true);

    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual(['recent:user']);
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenLastCalledWith(
      'thread-1', 'older-1', 'summary', 20,
    ));
    pendingNewWidth.resolve(summaryPage(['older'], 'page-20'));
    await rebuilding;
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual([
      'older:user', 'recent:user',
    ]);
  });

  it('keeps aggregate output and disclosure rows fixed across twenty rich detail pages', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      const target = cursor.slice('detail-'.length);
      return fullPageWithBoundedTools(target, cursor);
    });
    await settleOwner();
    h.owner.outline.value = Array.from({ length: 20 }, (_, index) => ({
      id: `rich-${index}:user`,
      role: 'user' as const,
      no: index + 1,
      title: `rich-${index}`,
      titleTruncated: false,
      pageCursor: `detail-rich-${index}`,
      recent: false,
    }));

    for (let index = 0; index < 20; index += 1) {
      const target = `rich-${index}`;
      await expect(h.owner.resolvePromptTarget(`${target}:user`)).resolves.toBe(true);
      const tools = h.owner.visibleTurns.value.flatMap((turn) => turn.tools ?? []);
      const nonEmpty = tools.filter((tool) => (tool.output?.length ?? 0) > 0);
      const visibleChars = nonEmpty.reduce(
        (total, tool) => total + toolOutputCodePointLength((tool.output ?? []).join('\n')),
        0,
      );
      const windows = tools.map((tool) => buildToolOutputLineWindow(
        tool.output ?? [],
        tool.outputOmittedChars ?? 0,
        tool.outputHeadLineCount ?? 0,
      ));

      expect(h.owner.historyWindow.value?.pageCursor).toBe(`detail-${target}`);
      expect(tools).toHaveLength(TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS + 4);
      expect(tools.every((tool) => tool.id.startsWith(`${target}:`))).toBe(true);
      expect(nonEmpty).toHaveLength(TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS);
      expect(visibleChars).toBe(TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS);
      expect(windows.reduce((count, window) => count + window.head.length + window.tail.length, 0))
        .toBe(TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS);
      expect(windows.filter((window) => window.aggregateOmittedChars > 0))
        .toHaveLength(4);
    }
  });

  it('preserves the installed detail window on epoch mismatch, request failure, and target miss', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      if (cursor === 'good-page') return fullPage(['good'], cursor);
      if (cursor === 'wrong-epoch') {
        return { ...fullPage(['wrong-epoch'], cursor), runtime_epoch: 'epoch-2' };
      }
      if (cursor === 'failed-page') throw new Error('history read failed');
      if (cursor === 'missing-target') return fullPage(['different'], cursor);
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();
    h.owner.outline.value = [
      { id: 'good:user', role: 'user', no: 1, title: 'good', titleTruncated: false, pageCursor: 'good-page', recent: false },
      { id: 'wrong-epoch:user', role: 'user', no: 2, title: 'epoch', titleTruncated: false, pageCursor: 'wrong-epoch', recent: false },
      { id: 'failed:user', role: 'user', no: 3, title: 'failed', titleTruncated: false, pageCursor: 'failed-page', recent: false },
      { id: 'missing:user', role: 'user', no: 4, title: 'missing', titleTruncated: false, pageCursor: 'missing-target', recent: false },
    ];
    await expect(h.owner.resolvePromptTarget('good:user')).resolves.toBe(true);
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['good:user']);

    for (const target of ['wrong-epoch:user', 'failed:user', 'missing:user']) {
      await expect(h.owner.resolvePromptTarget(target)).resolves.toBe(false);
      expect(h.owner.historyWindow.value?.pageCursor).toBe('good-page');
      expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['good:user']);
    }
    expect(h.reportError).toHaveBeenCalledTimes(3);
  });

  it('keeps live updates behind a history window and reveals the latest tail when it closes', async () => {
    const h = harness([user('live-1')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['old'], 'outline-page');
      return fullPage(['old'], cursor);
    });
    await settleOwner();
    h.owner.outline.value = [{
      id: 'old:user', role: 'user', no: 2, title: 'old',
      titleTruncated: false, pageCursor: 'old-page', recent: false,
    }];
    await h.owner.resolvePromptTarget('old:user');
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['old:user']);

    h.currentSnapshot.value = snapshot([user('live-2')]);
    await settleOwner();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['old:user']);

    h.owner.clearHistoryWindow();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['live-2:user']);
  });

  it('replaces an old outline with new recent Prompts when a runtime summary fails', async () => {
    let summaryReads = 0;
    const h = harness([user('recent')], async (_thread, _cursor, view) => {
      expect(view).toBe('summary');
      summaryReads += 1;
      if (summaryReads === 1) return summaryPage(['old'], 'old-page');
      throw new Error('new runtime history unavailable');
    });
    await settleOwner();
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toContain('old:user');

    h.currentSnapshot.value = {
      ...snapshot([user('new-runtime')]),
      runtime_epoch: 'epoch-2',
    };
    await settleOwner();

    expect(h.owner.outline.value.map((prompt) => prompt.id)).toEqual([
      'new-runtime:user',
    ]);
    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.reportError).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'new runtime history unavailable' }),
    );
    await h.owner.resolvePromptTarget('old:user');
    expect(h.listOlderTurns.mock.calls.filter((call) => call[2] === 'full')).toEqual([]);
  });

  it('settles an active detail waiter when the owner is disposed', async () => {
    const detail = deferred<FocusTurnPage>();
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['old'], 'outline-page');
      if (cursor === 'old-page') return detail.promise;
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();
    h.owner.outline.value = [{
      id: 'old:user', role: 'user', no: 2, title: 'old',
      titleTruncated: false, pageCursor: 'old-page', recent: false,
    }];

    const navigation = h.owner.resolvePromptTarget('old:user');
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledWith(
      'thread-1', 'old-page', 'full',
    ));
    h.owner.dispose();

    await expect(navigation).resolves.toBe(false);
    detail.resolve(fullPage(['old'], 'old-page'));
    await settleOwner();
  });

  it.each([
    ['thread', 'thread-2', EPOCH],
    ['runtime epoch', 'thread-1', 'epoch-2'],
  ])('settles active detail and rejects its stale page when the %s changes', async (
    _identityPart,
    nextThreadId,
    nextRuntimeEpoch,
  ) => {
    const detail = deferred<FocusTurnPage>();
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['old'], 'outline-page');
      if (cursor === 'old-page') return detail.promise;
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();
    h.owner.outline.value = [{
      id: 'old:user', role: 'user', no: 2, title: 'old',
      titleTruncated: false, pageCursor: 'old-page', recent: false,
    }];

    let navigationSettled = false;
    const navigation = h.owner.resolvePromptTarget('old:user');
    void navigation.then(() => { navigationSettled = true; });
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledWith(
      'thread-1', 'old-page', 'full',
    ));
    const nextSnapshot = snapshot([user('next')], '');
    nextSnapshot.thread.id = nextThreadId;
    nextSnapshot.runtime_epoch = nextRuntimeEpoch;
    h.activeThreadId.value = nextThreadId;
    h.currentSnapshot.value = nextSnapshot;

    await vi.waitFor(() => expect(navigationSettled).toBe(true));
    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['next:user']);

    detail.resolve(fullPage(['old'], 'old-page'));
    await settleOwner();
    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['next:user']);
  });

  it('runs one detail request and then only the latest queued intent', async () => {
    const first = deferred<FocusTurnPage>();
    const calls: string[] = [];
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      calls.push(`${view}:${cursor}`);
      if (view === 'summary') {
        return summaryPage(['a', 'b', 'c'], 'page-index');
      }
      if (cursor === 'page-a') return first.promise;
      return fullPage([cursor === 'page-c' ? 'c' : 'b'], cursor);
    });
    await settleOwner();
    h.owner.outline.value = [
      { id: 'a:user', role: 'user', no: 1, title: 'a', titleTruncated: false, pageCursor: 'page-a', recent: false },
      { id: 'b:user', role: 'user', no: 2, title: 'b', titleTruncated: false, pageCursor: 'page-b', recent: false },
      { id: 'c:user', role: 'user', no: 3, title: 'c', titleTruncated: false, pageCursor: 'page-c', recent: false },
    ];

    const a = h.owner.resolvePromptTarget('a:user');
    await Promise.resolve();
    const b = h.owner.resolvePromptTarget('b:user');
    const c = h.owner.resolvePromptTarget('c:user');
    first.resolve(fullPage(['a'], 'page-a'));
    await Promise.all([a, b, c]);

    expect(calls.filter((call) => call.startsWith('full:'))).toEqual([
      'full:page-a', 'full:page-c',
    ]);
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['c:user']);
  });

  it('cancels an active request when the next target is in the installed detail', async () => {
    const first = deferred<FocusTurnPage>();
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['a', 'b'], 'page-index');
      if (cursor === 'page-a') return first.promise;
      if (cursor === 'page-b') return fullPage(['b'], cursor);
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();
    h.owner.outline.value = [
      { id: 'a:user', role: 'user', no: 1, title: 'a', titleTruncated: false, pageCursor: 'page-a', recent: false },
      { id: 'b:user', role: 'user', no: 2, title: 'b', titleTruncated: false, pageCursor: 'page-b', recent: false },
    ];

    await h.owner.resolvePromptTarget('b:user');
    const a = h.owner.resolvePromptTarget('a:user');
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledWith(
      'thread-1', 'page-a', 'full',
    ));

    await h.owner.resolvePromptTarget('b:user');
    await expect(a).resolves.toBe(false);
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['b:user']);

    first.resolve(fullPage(['a'], 'page-a'));
    await settleOwner();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['b:user']);
  });

  it('returns a zero-write receipt when a pending top load is superseded by live B', async () => {
    const topPage = deferred<FocusTurnPage>();
    const h = harness([user('live-a')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      if (view === 'full' && cursor === 'older-1') return topPage.promise;
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();

    const topLoad = h.owner.loadOlderPage();
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledWith(
      'thread-1', 'older-1', 'full',
    ));
    h.currentSnapshot.value = snapshot([user('live-b')], 'older-1');
    await settleOwner();

    await expect(h.owner.resolvePromptTarget('live-b:user')).resolves.toBe(true);
    await expect(topLoad).resolves.toBe(false);
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['live-b:user']);

    topPage.resolve(fullPage(['older'], 'older-1'));
    await settleOwner();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['live-b:user']);
  });

  it('returns a zero-write receipt when a replacement page fails', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      if (view === 'full' && cursor === 'older-1') throw new Error('page failed');
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();

    await expect(h.owner.loadOlderPage()).resolves.toBe(false);
    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['recent:user']);
  });

  it('silently drops a history page replaced by a newer document read', async () => {
    const h = harness([user('recent')], async (_thread, _cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      throw new FocusApiError('stale document read', {
        status: 409,
        code: 'stale_document_read',
      });
    });
    await settleOwner();
    h.owner.outline.value.push({
      id: 'older:user', role: 'user', no: 2, title: 'older',
      titleTruncated: false, pageCursor: 'detail-page', recent: false,
    });

    await expect(h.owner.resolvePromptTarget('older:user')).resolves.toBe(false);

    expect(h.owner.historyWindow.value).toBeNull();
    expect(h.owner.error.value).toBe(false);
    expect(h.reportError).not.toHaveBeenCalled();
  });

  it('caps a lazy locator scan at the explicit page limit', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'full') throw new Error('full detail must not run');
      const page = cursor ? Number(cursor.slice(2)) : 0;
      return summaryPage([`other-${page}`], `page-${page}`, `c-${page + 1}`);
    });
    await settleOwner();
    h.owner.outline.value.push({
      id: 'missing:user', role: 'user', no: 2, title: 'missing',
      titleTruncated: false, pageCursor: null, recent: false,
    });

    await h.owner.resolvePromptTarget('missing:user');
    const headScan = h.listOlderTurns.mock.calls.filter((call) => call[2] === 'summary');
    expect(headScan.length).toBeLessThanOrEqual(FOCUS_HISTORY_OUTLINE_PAGE_LIMIT + 1);
    expect(h.reportError).toHaveBeenCalled();
    expect(h.owner.historyWindow.value).toBeNull();
  });

  it('caps the explicit outline at two hundred Prompts and twenty pages', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      expect(view).toBe('summary');
      const pageNumber = Number(cursor.slice('older-'.length));
      return summaryPage(
        Array.from({ length: 10 }, (_, index) => `page-${pageNumber}-${index}`),
        `stable-${pageNumber}`,
        `older-${pageNumber + 1}`,
      );
    });
    await settleOwner();

    while (h.owner.outlineHasMore.value) await h.owner.loadMoreOutline();

    expect(h.listOlderTurns).toHaveBeenCalledTimes(FOCUS_HISTORY_OUTLINE_PAGE_LIMIT);
    expect(h.owner.outline.value).toHaveLength(FOCUS_HISTORY_PROMPT_LIMIT);
    expect(h.owner.outlineTruncated.value).toBe(true);
    expect(h.owner.outlineHasMore.value).toBe(false);
  });

  it('tracks request cursors separately from stable response page cursors', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      expect(view).toBe('summary');
      return summaryPage(['older'], `stable-for-${cursor}`, cursor);
    });
    await settleOwner();
    expect(h.owner.outlineHasMore.value).toBe(true);

    await h.owner.loadMoreOutline();

    expect(h.listOlderTurns).toHaveBeenCalledTimes(1);
    expect(h.owner.outlineHasMore.value).toBe(false);
    expect(h.reportError).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'Focus Web history request cursor repeated.' }),
    );
  });

  it('serializes summary and full-detail reads through one history lane', async () => {
    const secondSummary = deferred<FocusTurnPage>();
    let activeReads = 0;
    let maximumActiveReads = 0;
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      activeReads += 1;
      maximumActiveReads = Math.max(maximumActiveReads, activeReads);
      try {
        if (view === 'summary' && cursor === 'older-1') {
          return summaryPage(['older'], 'stable-1', 'older-2');
        }
        if (view === 'summary' && cursor === 'older-2') return await secondSummary.promise;
        if (view === 'full' && cursor === 'target-page') {
          return fullPage(['target'], cursor);
        }
        throw new Error(`unexpected ${view}:${cursor}`);
      } finally {
        activeReads -= 1;
      }
    });
    await settleOwner();
    h.owner.outline.value.push({
      id: 'target:user', role: 'user', no: 3, title: 'target',
      titleTruncated: false, pageCursor: 'target-page', recent: false,
    });

    const outlineRead = h.owner.loadMoreOutline();
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledTimes(2));
    const detailRead = h.owner.resolvePromptTarget('target:user');
    await Promise.resolve();
    expect(activeReads).toBe(1);
    secondSummary.resolve(summaryPage(['older-2'], 'stable-2'));
    await Promise.all([outlineRead, detailRead]);

    expect(maximumActiveReads).toBe(1);
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['target:user']);
  });

  it('honestly drops an evicted steer segment that upstream summary cannot locate', async () => {
    const steer: ChatTurn = {
      id: 'initial:user:1', role: 'user', no: 2, text: 'steered follow-up',
    };
    const h = harness([user('initial'), steer], async () => {
      throw new Error('no history read expected');
    }, '');
    await settleOwner();
    expect(h.owner.outline.value.map((prompt) => prompt.id)).toContain('initial:user:1');

    h.currentSnapshot.value = snapshot(
      Array.from({ length: 10 }, (_, index) => user(`later-${index}`)),
      '',
    );
    await settleOwner();

    expect(h.owner.outline.value.map((prompt) => prompt.id)).toContain('initial:user');
    expect(h.owner.outline.value.map((prompt) => prompt.id)).not.toContain('initial:user:1');
    expect(h.listOlderTurns).not.toHaveBeenCalled();
  });

  it('resolves a search turn cursor to the first stable projected anchor for the raw turn', async () => {
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      if (cursor !== 'search-page') throw new Error(`unexpected ${view}:${cursor}`);
      return {
        ...fullPage([], cursor),
        turns: [
          user('raw-target', 'first prompt'),
          { id: 'raw-target:user:1', role: 'user', no: 2, text: 'steer' },
          { id: 'raw-target:assistant', role: 'assistant', no: 1, text: 'final' },
        ],
      };
    });
    await settleOwner();

    await expect(
      h.owner.resolveTurnCursorTarget('search-page', 'raw-target'),
    ).resolves.toBe('raw-target:user');
    expect(h.owner.historyWindow.value?.pageCursor).toBe('search-page');
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual([
      'raw-target:user',
      'raw-target:user:1',
      'raw-target:assistant',
    ]);
  });

  it('cancels a late search cursor intent without deleting the installed window', async () => {
    const slowPage = deferred<FocusTurnPage>();
    const h = harness([user('recent')], async (_thread, cursor, view) => {
      if (view === 'summary') return summaryPage(['older'], 'outline-page');
      if (cursor === 'installed-page') return fullPage(['installed'], cursor);
      if (cursor === 'slow-page') return slowPage.promise;
      throw new Error(`unexpected ${view}:${cursor}`);
    });
    await settleOwner();
    await expect(
      h.owner.resolveTurnCursorTarget('installed-page', 'installed'),
    ).resolves.toBe('installed:user');

    const late = h.owner.resolveTurnCursorTarget('slow-page', 'slow');
    await vi.waitFor(() => expect(h.listOlderTurns).toHaveBeenCalledWith(
      'thread-1', 'slow-page', 'full',
    ));
    h.owner.cancelDetailIntent();
    await expect(late).resolves.toBeNull();
    expect(h.owner.historyWindow.value?.pageCursor).toBe('installed-page');

    slowPage.resolve(fullPage(['slow'], 'slow-page'));
    await settleOwner();
    expect(h.owner.historyWindow.value?.pageCursor).toBe('installed-page');
    expect(h.owner.visibleTurns.value.map((turn) => turn.id)).toEqual(['installed:user']);
  });
});
