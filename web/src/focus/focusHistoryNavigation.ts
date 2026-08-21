import { computed, ref, watch } from 'vue';
import type { Ref } from 'vue';
import type { ChatTurn } from '../types';
import type { FocusWebApiPort } from './api';
import {
  isStaleWebReadError,
  type FocusSummaryPrompt,
  type FocusThreadSnapshot,
  type FocusTurnPage,
} from './types';

export const FOCUS_HISTORY_PROMPT_LIMIT = 200;
export const FOCUS_HISTORY_TITLE_LIMIT = 160;
export const FOCUS_RECENT_FULL_TURN_LIMIT = 10;
export const FOCUS_HISTORY_OUTLINE_PAGE_LIMIT = 20;

/** Recover the exact upstream raw-turn identity from a projected stable anchor. */
export function projectedRawTurnKey(turn: ChatTurn): string | null {
  if (turn.role !== 'user' && turn.role !== 'assistant' && turn.role !== 'compaction') return null;
  const parts = turn.id.split(':');
  let roleIndex = parts.length - 1;
  const trailingPart = parts[roleIndex] ?? '';
  if (
    trailingPart.length > 0
    && [...trailingPart].every((character) => character >= '0' && character <= '9')
  ) roleIndex -= 1;
  if (parts[roleIndex] !== turn.role) return null;
  return parts.slice(0, roleIndex).join(':') || null;
}

function rawTurnWindowKeys(turns: readonly ChatTurn[]): string[] {
  return turns.map((turn) => projectedRawTurnKey(turn) ?? turn.id);
}

export function boundRecentFullTurns(
  turns: readonly ChatTurn[],
  limit = FOCUS_RECENT_FULL_TURN_LIMIT,
): ChatTurn[] {
  if (turns.length === 0 || limit <= 0) return [];
  const rawTurnKeys = rawTurnWindowKeys(turns);
  const retainedRawTurns = new Set<string>();
  let start = turns.length;
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    if (turn === undefined) continue;
    const key = rawTurnKeys[index] ?? turn.id;
    if (!retainedRawTurns.has(key) && retainedRawTurns.size >= limit) break;
    retainedRawTurns.add(key);
    start = index;
  }
  return turns.slice(start);
}

export interface FocusHistoryPrompt {
  id: string;
  role: 'user';
  no: number;
  title: string;
  titleTruncated: boolean;
  pageCursor: string | null;
  recent: boolean;
}

interface FocusHistoryWindow {
  threadId: string;
  runtimeEpoch: string;
  pageCursor: string;
  turns: ChatTurn[];
  olderTurnCursor: string;
  hasMore: boolean;
}

interface DetailIntent {
  generation: number;
  threadId: string;
  runtimeEpoch: string;
  pageCursor: string;
  targetProjectedTurnId: string;
  targetRawTurnId: string;
  turnLimit: number;
  resolve: (receipt: DetailInstallReceipt) => void;
}

interface DetailInstallReceipt {
  installed: boolean;
  anchorId: string | null;
}

const DETAIL_NOT_INSTALLED: DetailInstallReceipt = {
  installed: false,
  anchorId: null,
};

type OutlineRefreshMode = 'full' | 'first-page' | 'load-more';

export interface FocusHistoryNavigationOptions {
  api: Pick<FocusWebApiPort, 'listOlderTurns'>;
  snapshot: Readonly<Ref<FocusThreadSnapshot | null>>;
  activeThreadId: Readonly<Ref<string>>;
  turnLimit: Readonly<Ref<number>>;
  reportError(error: unknown): void;
  isDisposed(): boolean;
}

function normalizedPromptTitle(text: string): { title: string; truncated: boolean } {
  let title = '';
  let length = 0;
  let pendingSpace = false;
  let truncated = false;
  for (const character of text) {
    if (character.trim() === '') {
      if (length > 0) pendingSpace = true;
      continue;
    }
    const required = 1 + (pendingSpace ? 1 : 0);
    if (length + required > FOCUS_HISTORY_TITLE_LIMIT) {
      truncated = true;
      break;
    }
    if (pendingSpace) {
      title += ' ';
      length += 1;
      pendingSpace = false;
    }
    title += character;
    length += 1;
  }
  return { title: title || 'user', truncated };
}

function userPrompts(
  turns: readonly ChatTurn[],
  pageCursor: string | null,
  recent: boolean,
): Omit<FocusHistoryPrompt, 'no'>[] {
  return turns.flatMap((turn) => {
    if (turn.role !== 'user') return [];
    const normalized = normalizedPromptTitle(turn.text);
    return [{
      id: turn.id,
      role: 'user' as const,
      title: normalized.title,
      titleTruncated: normalized.truncated,
      pageCursor,
      recent,
    }];
  });
}

function summaryPrompts(
  prompts: readonly FocusSummaryPrompt[],
  pageCursor: string,
): Omit<FocusHistoryPrompt, 'no'>[] {
  return prompts.map((prompt) => ({
    id: prompt.id,
    role: 'user',
    title: prompt.text || 'user',
    titleTruncated: prompt.title_truncated,
    pageCursor: pageCursor || null,
    recent: false,
  }));
}

function isHistoricalSummaryPromptId(turnId: string): boolean {
  return turnId.endsWith(':user');
}

function historyIdentity(
  snapshot: FocusThreadSnapshot | null,
  activeThreadId: string,
): { threadId: string; runtimeEpoch: string } | null {
  if (!snapshot || snapshot.thread.id !== activeThreadId) return null;
  return { threadId: snapshot.thread.id, runtimeEpoch: snapshot.runtime_epoch };
}

function historyMismatchError(): Error {
  return new Error('Focus Web history response no longer matches the active thread runtime.');
}

export function createFocusHistoryNavigation(options: FocusHistoryNavigationOptions) {
  const outline = ref<FocusHistoryPrompt[]>([]);
  const outlineTruncated = ref(false);
  const outlineLoading = ref(false);
  const outlineError = ref(false);
  const outlineHasMore = ref(false);
  const historyWindow = ref<FocusHistoryWindow | null>(null);
  const loading = ref(false);
  const error = ref(false);
  const visibleTurns = computed(() => (
    historyWindow.value?.turns ?? options.snapshot.value?.turns ?? []
  ));
  const hasMore = computed(() => (
    historyWindow.value?.hasMore ?? options.snapshot.value?.has_more_turns ?? false
  ));

  let disposed = false;
  let outlineGeneration = 0;
  let indexedThreadId = '';
  let indexedRuntimeEpoch = '';
  let indexedRecentCursor = '';
  let outlineNextCursor = '';
  let outlinePagesLoaded = 0;
  const outlineRequestCursors = new Set<string>();
  const outlinePageCursors = new Set<string>();
  let outlineRunning = false;
  let pendingOutlineMode: OutlineRefreshMode | null = null;
  let outlineWaiters: Array<() => void> = [];
  let latestDetailGeneration = 0;
  let detailRunning = false;
  let activeDetail: DetailIntent | null = null;
  let pendingDetail: DetailIntent | null = null;
  let historyRequestTail: Promise<void> = Promise.resolve();
  let turnLimitChangePending = false;

  async function listHistoryPage(
    threadId: string,
    cursor: string,
    itemsView: FocusTurnPage['items_view'],
    requestIsCurrent: () => boolean,
    turnLimit = options.turnLimit.value,
  ): Promise<FocusTurnPage | null> {
    let release = (): void => {};
    const previous = historyRequestTail;
    historyRequestTail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      if (!requestIsCurrent()) return null;
      try {
        return turnLimit === FOCUS_RECENT_FULL_TURN_LIMIT
          ? await options.api.listOlderTurns(threadId, cursor, itemsView)
          : await options.api.listOlderTurns(threadId, cursor, itemsView, turnLimit);
      } catch (error) {
        // A newer document/observation owns the replacement read.  The old
        // history page cannot be installed; callers retain the live window
        // and may issue a later bounded request.
        if (isStaleWebReadError(error)) return null;
        throw error;
      }
    } finally {
      release();
    }
  }

  function activeIdentity() {
    return historyIdentity(options.snapshot.value, options.activeThreadId.value);
  }

  function detailIntentIsCurrent(intent: DetailIntent, page?: FocusTurnPage): boolean {
    const identity = activeIdentity();
    return !disposed
      && !options.isDisposed()
      && intent.generation === latestDetailGeneration
      && intent.turnLimit === options.turnLimit.value
      && identity?.threadId === intent.threadId
      && identity.runtimeEpoch === intent.runtimeEpoch
      && (page === undefined || page.runtime_epoch === intent.runtimeEpoch);
  }

  function cancelPendingDetail(): void {
    const pending = pendingDetail;
    pendingDetail = null;
    pending?.resolve(DETAIL_NOT_INSTALLED);
  }

  function settleActiveDetailWaiter(): void {
    const active = activeDetail;
    activeDetail = null;
    active?.resolve(DETAIL_NOT_INSTALLED);
  }

  function replaceOutlineLocators(
    prompts: ReadonlyArray<Omit<FocusHistoryPrompt, 'no'> | FocusHistoryPrompt>,
  ): void {
    const byId = new Map(prompts.map((prompt) => [prompt.id, prompt]));
    outline.value = outline.value.map((prompt) => {
      const replacement = byId.get(prompt.id);
      return replacement ? { ...prompt, pageCursor: replacement.pageCursor } : prompt;
    });
  }

  async function resolveMissingPageCursor(intent: DetailIntent): Promise<string> {
    let cursor = '';
    const seenResponseCursors = new Set<string>();
    for (let pageNumber = 0; pageNumber < FOCUS_HISTORY_OUTLINE_PAGE_LIMIT; pageNumber += 1) {
      const page = await listHistoryPage(
        intent.threadId,
        cursor,
        'summary',
        () => detailIntentIsCurrent(intent),
        intent.turnLimit,
      );
      if (!page || !detailIntentIsCurrent(intent)) return '';
      if (page.runtime_epoch !== intent.runtimeEpoch) throw historyMismatchError();
      if (page.items_view !== 'summary') {
        throw new Error('Focus Web history locator request returned a full-detail page.');
      }
      if (!page.page_cursor || seenResponseCursors.has(page.page_cursor)) {
        throw new Error('Focus Web history locator response had no new stable page cursor.');
      }
      seenResponseCursors.add(page.page_cursor);
      const prompts = summaryPrompts(
        page.turns as FocusSummaryPrompt[],
        page.page_cursor,
      );
      replaceOutlineLocators(prompts);
      if (prompts.some((prompt) => prompt.id === intent.targetProjectedTurnId)) {
        return page.page_cursor;
      }
      if (!page.has_more_turns || !page.older_turn_cursor) break;
      cursor = page.older_turn_cursor;
    }
    throw new Error(
      'Focus Web could not locate this Prompt in the bounded upstream history index.',
    );
  }

  function clearHistoryWindow(): void {
    cancelDetailIntent();
    historyWindow.value = null;
    error.value = false;
  }

  /** Retire an in-flight replacement while preserving the installed window. */
  function cancelDetailIntent(): void {
    latestDetailGeneration += 1;
    cancelPendingDetail();
    settleActiveDetailWaiter();
    loading.value = false;
  }

  /**
   * Retire locators and the one full-detail page when page width changes.
   * Opaque page cursors are only reusable with the width that produced them.
   */
  function resetForTurnLimitChange(): void {
    turnLimitChangePending = true;
    outlineGeneration += 1;
    pendingOutlineMode = null;
    clearHistoryWindow();
    clearOutlineForIdentityChange();
    outlineLoading.value = outlineRunning;
  }

  function completeTurnLimitChange(rebuild: boolean): Promise<void> {
    turnLimitChangePending = false;
    const snapshot = options.snapshot.value;
    if (rebuild) {
      const identity = activeIdentity();
      if (snapshot && identity) installRecentOutlineForIdentity(snapshot, identity);
      return queueOutlineRefresh('full');
    }
    installOutline(snapshot ? userPrompts(snapshot.turns, null, true) : [], false);
    return Promise.resolve();
  }

  function installOutline(
    prompts: ReadonlyArray<Omit<FocusHistoryPrompt, 'no'> | FocusHistoryPrompt>,
    alreadyTruncated: boolean,
  ): void {
    const seen = new Set<string>();
    const uniqueReversed: Array<Omit<FocusHistoryPrompt, 'no'> | FocusHistoryPrompt> = [];
    for (let index = prompts.length - 1; index >= 0; index -= 1) {
      const prompt = prompts[index];
      if (prompt === undefined || seen.has(prompt.id)) continue;
      seen.add(prompt.id);
      uniqueReversed.push(prompt);
    }
    const unique = uniqueReversed.reverse();
    const overflow = unique.length > FOCUS_HISTORY_PROMPT_LIMIT;
    outline.value = unique
      .slice(-FOCUS_HISTORY_PROMPT_LIMIT)
      .map((prompt, index) => ({ ...prompt, no: index + 1 }));
    outlineTruncated.value = alreadyTruncated || overflow;
  }

  function resetOutlinePaging(): void {
    outlineNextCursor = '';
    outlinePagesLoaded = 0;
    outlineRequestCursors.clear();
    outlinePageCursors.clear();
    outlineHasMore.value = false;
  }

  function clearOutlineForIdentityChange(): void {
    outline.value = [];
    outlineTruncated.value = false;
    outlineError.value = false;
    indexedThreadId = '';
    indexedRuntimeEpoch = '';
    indexedRecentCursor = '';
    resetOutlinePaging();
  }

  function installRecentOutlineForIdentity(
    snapshot: FocusThreadSnapshot,
    identity: { threadId: string; runtimeEpoch: string },
  ): void {
    installOutline(userPrompts(snapshot.turns, null, true), false);
    indexedThreadId = identity.threadId;
    indexedRuntimeEpoch = identity.runtimeEpoch;
    indexedRecentCursor = snapshot.older_turn_cursor;
  }

  function mergeRecentPrompts(
    recentTurns: readonly ChatTurn[],
    retained: ReadonlyArray<Omit<FocusHistoryPrompt, 'no'> | FocusHistoryPrompt> = outline.value,
  ): Array<Omit<FocusHistoryPrompt, 'no'> | FocusHistoryPrompt> {
    const recent = userPrompts(recentTurns, null, true);
    const previousById = new Map(retained.map((prompt) => [prompt.id, prompt]));
    const recentIds = new Set(recent.map((prompt) => prompt.id));
    const historical = retained.flatMap((prompt) => {
      if (recentIds.has(prompt.id)) return [];
      // Upstream summary exposes only the first user message in each raw turn.
      // A recent steer segment may appear while it is in the live DOM, but it
      // cannot become a durable historical locator once that live page evicts it.
      if (prompt.pageCursor === null && !isHistoricalSummaryPromptId(prompt.id)) return [];
      return [{ ...prompt, recent: false }];
    });
    const current = recent.map((prompt) => ({
      ...prompt,
      pageCursor: previousById.get(prompt.id)?.pageCursor ?? null,
    }));
    return [...historical, ...current];
  }

  async function rebuildOutlineNow(generation: number): Promise<void> {
    const snapshot = options.snapshot.value;
    const identity = activeIdentity();
    if (!snapshot || !identity) {
      outline.value = [];
      outlineTruncated.value = false;
      outlineError.value = false;
      indexedThreadId = '';
      indexedRuntimeEpoch = '';
      indexedRecentCursor = '';
      resetOutlinePaging();
      return;
    }

    outlineError.value = false;
    resetOutlinePaging();
    const recent = userPrompts(snapshot.turns, null, true);
    let collected = recent.slice(-FOCUS_HISTORY_PROMPT_LIMIT);
    let truncated = recent.length > FOCUS_HISTORY_PROMPT_LIMIT;
    // The live window is already authoritative and local. Install it before
    // the serialized history lane can wait on or fail an older summary read.
    installRecentOutlineForIdentity(snapshot, identity);

    try {
      // Legacy turns/list replays the rollout on every request. Keep initial
      // navigation responsive by reading at most one older summary page; the
      // user can explicitly extend the outline one serialized page at a time.
      if (
        snapshot.has_more_turns
        && snapshot.older_turn_cursor
        && collected.length < FOCUS_HISTORY_PROMPT_LIMIT
      ) {
        const requestCursor = snapshot.older_turn_cursor;
        const page = await listHistoryPage(
          identity.threadId,
          requestCursor,
          'summary',
          () => generation === outlineGeneration
            && detailIdentityMatches(identity.threadId, identity.runtimeEpoch),
        );
        if (!page) return;
        if (
          generation !== outlineGeneration
          || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
        ) return;
        if (page.runtime_epoch !== identity.runtimeEpoch) throw historyMismatchError();
        if (page.items_view !== 'summary') {
          throw new Error('Focus Web history summary request returned a full-detail page.');
        }
        if (!page.page_cursor) {
          throw new Error('Focus Web history summary response had no stable page cursor.');
        }
        const prompts = summaryPrompts(
          page.turns as FocusSummaryPrompt[],
          page.page_cursor,
        );
        const remaining = FOCUS_HISTORY_PROMPT_LIMIT - collected.length;
        const retained = prompts.length > remaining ? prompts.slice(-remaining) : prompts;
        collected = [...retained, ...collected];
        truncated ||= prompts.length > retained.length;
        outlinePagesLoaded = 1;
        outlineRequestCursors.add(requestCursor);
        outlinePageCursors.add(page.page_cursor);
        outlineNextCursor = page.older_turn_cursor;
        outlineHasMore.value = page.has_more_turns && Boolean(outlineNextCursor);
      }
      if (
        generation !== outlineGeneration
        || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
      ) return;
      const latestSnapshot = options.snapshot.value;
      const latestRecent = latestSnapshot
        ? userPrompts(latestSnapshot.turns, null, true)
        : [];
      const historical = collected.filter((prompt) => !prompt.recent);
      const remaining = Math.max(FOCUS_HISTORY_PROMPT_LIMIT - latestRecent.length, 0);
      const retainedHistorical = remaining === 0
        ? []
        : historical.length > remaining
          ? historical.slice(-remaining)
          : historical;
      installOutline(
        [...retainedHistorical, ...latestRecent],
        truncated
          || latestRecent.length > FOCUS_HISTORY_PROMPT_LIMIT
          || historical.length > retainedHistorical.length,
      );
      if (outline.value.length >= FOCUS_HISTORY_PROMPT_LIMIT) {
        outlineTruncated.value ||= outlineHasMore.value;
        outlineHasMore.value = false;
      }
      indexedThreadId = identity.threadId;
      indexedRuntimeEpoch = identity.runtimeEpoch;
      indexedRecentCursor = snapshot.older_turn_cursor;
    } catch (caught) {
      if (
        generation !== outlineGeneration
        || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
      ) return;
      outlineError.value = true;
      options.reportError(caught);
    }
  }

  async function refreshFirstOutlinePageNow(generation: number): Promise<void> {
    const snapshot = options.snapshot.value;
    const identity = activeIdentity();
    if (!snapshot || !identity) {
      await rebuildOutlineNow(generation);
      return;
    }
    if (
      indexedThreadId !== identity.threadId
      || indexedRuntimeEpoch !== identity.runtimeEpoch
    ) {
      await rebuildOutlineNow(generation);
      return;
    }

    outlineError.value = false;
    const recent = userPrompts(snapshot.turns, null, true);
    // A changed head cursor commonly arrives with a new live Prompt. Merge
    // that Prompt immediately; refreshing its historical locator is allowed
    // to remain pending or fail without hiding the live navigation surface.
    installOutline(
      mergeRecentPrompts(snapshot.turns),
      outlineTruncated.value,
    );
    indexedRecentCursor = snapshot.older_turn_cursor;
    let firstPage: Omit<FocusHistoryPrompt, 'no'>[] = [];
    let firstPageResponse: FocusTurnPage | null = null;
    if (snapshot.has_more_turns && snapshot.older_turn_cursor) {
      const pageCursor = snapshot.older_turn_cursor;
      try {
        const page = await listHistoryPage(
          identity.threadId,
          pageCursor,
          'summary',
          () => generation === outlineGeneration
            && detailIdentityMatches(identity.threadId, identity.runtimeEpoch),
        );
        if (!page) return;
        if (
          generation !== outlineGeneration
          || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
        ) return;
        if (page.runtime_epoch !== identity.runtimeEpoch) throw historyMismatchError();
        if (page.items_view !== 'summary') {
          throw new Error('Focus Web history summary request returned a full-detail page.');
        }
        if (!page.page_cursor) {
          throw new Error('Focus Web history summary response had no stable page cursor.');
        }
        firstPage = summaryPrompts(
          page.turns as FocusSummaryPrompt[],
          page.page_cursor,
        );
        firstPageResponse = page;
      } catch (caught) {
        if (
          generation !== outlineGeneration
          || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
        ) return;
        outlineError.value = true;
        options.reportError(caught);
        return;
      }
    }

    const latestSnapshot = options.snapshot.value;
    const latestRecent = latestSnapshot
      ? userPrompts(latestSnapshot.turns, null, true)
      : recent;
    const replacedIds = new Set([
      ...latestRecent.map((prompt) => prompt.id),
      ...firstPage.map((prompt) => prompt.id),
    ]);
    const retainedOlder = outline.value
      .filter((prompt) => !replacedIds.has(prompt.id))
      .flatMap((prompt) => (
        prompt.pageCursor !== null || isHistoricalSummaryPromptId(prompt.id)
          ? [{ ...prompt, recent: false }]
          : []
      ));
    installOutline(
      mergeRecentPrompts(
        latestSnapshot?.turns ?? snapshot.turns,
        [...retainedOlder, ...firstPage],
      ),
      outlineTruncated.value,
    );
    outlineRequestCursors.clear();
    outlinePageCursors.clear();
    if (firstPageResponse) {
      outlineRequestCursors.add(snapshot.older_turn_cursor);
      outlinePageCursors.add(firstPageResponse.page_cursor);
    }
    outlinePagesLoaded = firstPageResponse ? 1 : 0;
    outlineNextCursor = firstPageResponse?.older_turn_cursor ?? '';
    outlineHasMore.value = firstPageResponse?.has_more_turns === true
      && Boolean(outlineNextCursor)
      && outline.value.length < FOCUS_HISTORY_PROMPT_LIMIT;
    indexedRecentCursor = snapshot.older_turn_cursor;
  }

  async function loadMoreOutlineNow(generation: number): Promise<void> {
    const identity = activeIdentity();
    if (
      !identity
      || !outlineHasMore.value
      || !outlineNextCursor
      || outlinePagesLoaded >= FOCUS_HISTORY_OUTLINE_PAGE_LIMIT
      || outline.value.length >= FOCUS_HISTORY_PROMPT_LIMIT
    ) return;
    const requestCursor = outlineNextCursor;
    if (outlineRequestCursors.has(requestCursor)) {
      outlineHasMore.value = false;
      throw new Error('Focus Web history request cursor repeated.');
    }
    const page = await listHistoryPage(
      identity.threadId,
      requestCursor,
      'summary',
      () => generation === outlineGeneration
        && detailIdentityMatches(identity.threadId, identity.runtimeEpoch),
    );
    if (!page) return;
    if (
      generation !== outlineGeneration
      || !detailIdentityMatches(identity.threadId, identity.runtimeEpoch)
    ) return;
    if (page.runtime_epoch !== identity.runtimeEpoch) throw historyMismatchError();
    if (page.items_view !== 'summary') {
      throw new Error('Focus Web history summary request returned a full-detail page.');
    }
    if (!page.page_cursor || outlinePageCursors.has(page.page_cursor)) {
      outlineHasMore.value = false;
      throw new Error('Focus Web history response had no new stable page cursor.');
    }
    const knownIds = new Set(outline.value.map((prompt) => prompt.id));
    const prompts = summaryPrompts(
      page.turns as FocusSummaryPrompt[],
      page.page_cursor,
    ).filter((prompt) => !knownIds.has(prompt.id));
    installOutline([...prompts, ...outline.value], outlineTruncated.value);
    outlinePagesLoaded += 1;
    outlineRequestCursors.add(requestCursor);
    outlinePageCursors.add(page.page_cursor);
    outlineNextCursor = page.older_turn_cursor;
    outlineHasMore.value = page.has_more_turns
      && Boolean(outlineNextCursor)
      && outlinePagesLoaded < FOCUS_HISTORY_OUTLINE_PAGE_LIMIT
      && outline.value.length < FOCUS_HISTORY_PROMPT_LIMIT;
    if (!outlineHasMore.value && page.has_more_turns) outlineTruncated.value = true;
  }

  function mergeRecentOutlineWithoutHistoryRead(): void {
    const snapshot = options.snapshot.value;
    const identity = activeIdentity();
    if (!snapshot || !identity) return;
    installOutline(
      mergeRecentPrompts(snapshot.turns),
      outlineTruncated.value,
    );
  }

  async function runOutlineQueue(): Promise<void> {
    if (outlineRunning) return;
    outlineRunning = true;
    outlineLoading.value = true;
    try {
      while (pendingOutlineMode && !disposed && !options.isDisposed()) {
        const mode = pendingOutlineMode;
        pendingOutlineMode = null;
        const generation = outlineGeneration;
        try {
          if (mode === 'full') await rebuildOutlineNow(generation);
          else if (mode === 'first-page') await refreshFirstOutlinePageNow(generation);
          else await loadMoreOutlineNow(generation);
        } catch (caught) {
          if (generation === outlineGeneration) {
            outlineError.value = true;
            options.reportError(caught);
          }
        }
      }
    } finally {
      outlineRunning = false;
      outlineLoading.value = false;
      const waiters = outlineWaiters;
      outlineWaiters = [];
      for (const resolve of waiters) resolve();
      if (pendingOutlineMode && !disposed && !options.isDisposed()) void runOutlineQueue();
    }
  }

  function queueOutlineRefresh(mode: OutlineRefreshMode): Promise<void> {
    outlineGeneration += 1;
    if (
      mode === 'full'
      || pendingOutlineMode === null
      || (mode === 'first-page' && pendingOutlineMode === 'load-more')
    ) pendingOutlineMode = mode;
    const pending = new Promise<void>((resolve) => outlineWaiters.push(resolve));
    void runOutlineQueue();
    return pending;
  }

  function rebuildOutline(): Promise<void> {
    return queueOutlineRefresh('full');
  }

  function loadMoreOutline(): Promise<void> {
    if (!outlineHasMore.value || outlineLoading.value) return Promise.resolve();
    return queueOutlineRefresh('load-more');
  }

  function detailIdentityMatches(threadId: string, runtimeEpoch: string): boolean {
    const identity = activeIdentity();
    return !disposed
      && !options.isDisposed()
      && identity?.threadId === threadId
      && identity.runtimeEpoch === runtimeEpoch;
  }

  async function runDetailQueue(): Promise<void> {
    if (detailRunning) return;
    detailRunning = true;
    try {
      while (pendingDetail && !disposed && !options.isDisposed()) {
        const intent = pendingDetail;
        pendingDetail = null;
        if (intent.generation !== latestDetailGeneration) {
          intent.resolve(DETAIL_NOT_INSTALLED);
          continue;
        }
        activeDetail = intent;
        loading.value = true;
        error.value = false;
        let installed = false;
        let anchorId: string | null = null;
        try {
          const resolvedPageCursor = intent.pageCursor
            || await resolveMissingPageCursor(intent);
          if (!resolvedPageCursor || !detailIntentIsCurrent(intent)) continue;
          const page = await listHistoryPage(
            intent.threadId,
            resolvedPageCursor,
            'full',
            () => detailIntentIsCurrent(intent),
            intent.turnLimit,
          );
          if (!page) continue;
          if (!detailIntentIsCurrent(intent)) {
            if (intent.generation === latestDetailGeneration) {
              error.value = true;
              options.reportError(historyMismatchError());
            }
            continue;
          }
          if (!detailIntentIsCurrent(intent, page)) {
            error.value = true;
            options.reportError(historyMismatchError());
            continue;
          }
          if (page.items_view !== 'full') {
            error.value = true;
            options.reportError(
              new Error('Focus Web history detail request returned a summary page.'),
            );
            continue;
          }
          const turns = boundRecentFullTurns(page.turns as ChatTurn[], intent.turnLimit);
          if (intent.targetProjectedTurnId) {
            anchorId = turns.some((turn) => turn.id === intent.targetProjectedTurnId)
              ? intent.targetProjectedTurnId
              : null;
          } else if (intent.targetRawTurnId) {
            anchorId = turns.find((turn) => (
              projectedRawTurnKey(turn) === intent.targetRawTurnId
            ))?.id ?? null;
          }
          if (
            (intent.targetProjectedTurnId || intent.targetRawTurnId)
            && anchorId === null
          ) {
            error.value = true;
            options.reportError(
              new Error('Focus Web history page did not contain the requested turn.'),
            );
            continue;
          }
          historyWindow.value = {
            threadId: intent.threadId,
            runtimeEpoch: intent.runtimeEpoch,
            pageCursor: page.page_cursor || resolvedPageCursor,
            turns,
            olderTurnCursor: page.older_turn_cursor,
            hasMore: page.has_more_turns,
          };
          installed = true;
        } catch (caught) {
          if (intent.generation === latestDetailGeneration) {
            error.value = true;
            options.reportError(caught);
          }
        } finally {
          if (activeDetail === intent) activeDetail = null;
          if (intent.generation === latestDetailGeneration) loading.value = false;
          intent.resolve({ installed, anchorId });
        }
      }
    } finally {
      detailRunning = false;
      if (pendingDetail && !disposed && !options.isDisposed()) void runDetailQueue();
    }
  }

  function requestFullPage(
    pageCursor: string,
    targetProjectedTurnId = '',
    targetRawTurnId = '',
  ): Promise<DetailInstallReceipt> {
    const identity = activeIdentity();
    if (
      !identity
      || (!pageCursor && !targetProjectedTurnId)
      || (targetProjectedTurnId && targetRawTurnId)
    ) return Promise.resolve(DETAIL_NOT_INSTALLED);
    const generation = ++latestDetailGeneration;
    cancelPendingDetail();
    return new Promise((resolve) => {
      pendingDetail = {
        generation,
        threadId: identity.threadId,
        runtimeEpoch: identity.runtimeEpoch,
        pageCursor,
        targetProjectedTurnId,
        targetRawTurnId,
        turnLimit: options.turnLimit.value,
        resolve,
      };
      void runDetailQueue();
    });
  }

  async function resolvePromptTarget(turnId: string): Promise<boolean> {
    const prompt = outline.value.find((candidate) => candidate.id === turnId);
    if (!prompt) {
      latestDetailGeneration += 1;
      cancelPendingDetail();
      settleActiveDetailWaiter();
      loading.value = false;
      return false;
    }
    const stillInLiveWindow = options.snapshot.value?.turns.some((turn) => turn.id === turnId);
    if (prompt.recent && stillInLiveWindow) {
      clearHistoryWindow();
      return true;
    }
    if (
      historyWindow.value?.pageCursor === prompt.pageCursor
      && historyWindow.value.turns.some((turn) => turn.id === turnId)
    ) {
      latestDetailGeneration += 1;
      cancelPendingDetail();
      settleActiveDetailWaiter();
      loading.value = false;
      return true;
    }
    return (await requestFullPage(prompt.pageCursor ?? '', turnId)).installed;
  }

  /**
   * Replace the sole full-detail window from an upstream search occurrence and
   * return the first real projected DOM anchor for that raw turn.
   */
  async function resolveTurnCursorTarget(
    turnCursor: string,
    rawTurnId: string,
  ): Promise<string | null> {
    if (
      !turnCursor
      || turnCursor !== turnCursor.trim()
      || !rawTurnId
      || rawTurnId !== rawTurnId.trim()
    ) return null;
    const receipt = await requestFullPage(turnCursor, '', rawTurnId);
    return receipt.installed ? receipt.anchorId : null;
  }

  async function loadOlderPage(): Promise<boolean> {
    const cursor = historyWindow.value?.olderTurnCursor
      ?? options.snapshot.value?.older_turn_cursor
      ?? '';
    if (!hasMore.value || !cursor || loading.value) return false;
    return (await requestFullPage(cursor)).installed;
  }

  const stopSnapshotWatch = watch(
    () => {
      const snapshot = options.snapshot.value;
      const recentPrompts = snapshot?.turns
        .filter((turn) => turn.role === 'user')
        .map((turn) => {
          const normalized = normalizedPromptTitle(turn.text);
          return [turn.id, normalized.title, normalized.truncated];
        });
      return JSON.stringify([
        options.activeThreadId.value,
        snapshot?.thread.id ?? '',
        snapshot?.runtime_epoch ?? '',
        snapshot?.older_turn_cursor ?? '',
        snapshot?.has_more_turns ?? false,
        recentPrompts ?? [],
      ]);
    },
    (_identityKey, previousIdentityKey) => {
      if (turnLimitChangePending) return;
      const snapshot = options.snapshot.value;
      const current = activeIdentity();
      if (previousIdentityKey !== undefined) {
        const window = historyWindow.value;
        const detail = activeDetail ?? pendingDetail;
        if (
          (
            window
            && (window.threadId !== current?.threadId
              || window.runtimeEpoch !== current?.runtimeEpoch)
          )
          || (
            detail
            && (detail.threadId !== current?.threadId
              || detail.runtimeEpoch !== current?.runtimeEpoch)
          )
        ) clearHistoryWindow();
      }
      if (
        !snapshot
        || !current
        || indexedThreadId !== current.threadId
        || indexedRuntimeEpoch !== current.runtimeEpoch
      ) {
        clearOutlineForIdentityChange();
        if (snapshot && current) installRecentOutlineForIdentity(snapshot, current);
        void queueOutlineRefresh('full');
      } else if (indexedRecentCursor !== snapshot.older_turn_cursor) {
        void queueOutlineRefresh('first-page');
      } else {
        mergeRecentOutlineWithoutHistoryRead();
      }
    },
    { immediate: true },
  );

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    outlineGeneration += 1;
    pendingOutlineMode = null;
    const waiters = outlineWaiters;
    outlineWaiters = [];
    for (const resolve of waiters) resolve();
    latestDetailGeneration += 1;
    cancelPendingDetail();
    settleActiveDetailWaiter();
    stopSnapshotWatch();
    historyWindow.value = null;
    loading.value = false;
    outlineLoading.value = false;
  }

  return {
    outline,
    outlineTruncated,
    outlineLoading,
    outlineError,
    outlineHasMore,
    historyWindow,
    visibleTurns,
    hasMore,
    loading,
    error,
    clearHistoryWindow,
    cancelDetailIntent,
    resolvePromptTarget,
    resolveTurnCursorTarget,
    loadOlderPage,
    loadMoreOutline,
    rebuildOutline,
    resetForTurnLimitChange,
    completeTurnLimitChange,
    dispose,
  };
}
