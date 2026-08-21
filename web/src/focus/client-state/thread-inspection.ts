import { computed, ref, watch, type Ref } from 'vue';
import type { FocusWebApiPort } from '../api';
import type { FocusThreadInspectionUnavailableReason } from '../threadInspectionTypes';
import { FocusApiError, isStaleWebReadError } from '../types';
import type {
  FocusConversationSearchOccurrence,
  FocusThreadConversationSearchPage,
  FocusThreadSnapshot,
  FocusThreadToolDetailPayload,
  FocusToolDetailView,
  FocusToolInspectionLocator,
} from '../types';

interface InspectionIdentity {
  threadId: string;
  runtimeEpoch: string;
}

export interface ThreadInspectionOptions {
  api: Pick<FocusWebApiPort, 'readToolDetail' | 'searchConversation'>;
  metaCapabilities: Readonly<Ref<{
    tool_detail: boolean;
    history_search: boolean;
  } | null>>;
  accessAvailable: Readonly<Ref<boolean>>;
  snapshot: Readonly<Ref<FocusThreadSnapshot | null>>;
  activeThreadId: Readonly<Ref<string>>;
  resolveTurnCursorTarget(turnCursor: string, rawTurnId: string): Promise<string | null>;
  cancelTurnCursorTarget(): void;
  reportError(error: unknown): void;
  /** Must read the navigation owner's reactive disposal source. */
  isDisposed(): boolean;
}

export interface ConversationSearchInput {
  query: string;
  cursor?: string | null;
}

type InspectionCapability = 'history_search' | 'tool_detail';

function abortError(error: unknown): boolean {
  return typeof error === 'object'
    && error !== null
    && 'name' in error
    && error.name === 'AbortError';
}

function requestUnavailableReason(
  error: unknown,
): FocusThreadInspectionUnavailableReason | null {
  if (!(error instanceof FocusApiError)) return null;
  if (error.code === 'thread_inspection_upstream_unsupported') return 'runtime_unsupported';
  if (error.code === 'thread_not_selected') return 'thread_not_materialized';
  if (error.code === 'thread_inspection_unavailable') return 'unknown_history';
  return null;
}

function sameToolInspectionLocator(
  left: FocusToolInspectionLocator,
  right: FocusToolInspectionLocator,
): boolean {
  return left.turn_id === right.turn_id
    && left.item_id === right.item_id
    && left.kind === right.kind
    && left.change_index === right.change_index;
}

export function createThreadInspection(options: ThreadInspectionOptions) {
  const toolDetail = ref<FocusThreadToolDetailPayload | null>(null);
  // The payload deliberately contains only the wire detail. Keep its exact
  // request locator beside the one browser-local slot so a replaced selection
  // can never render a stale preview/full source while its next request runs.
  const toolDetailLocator = ref<FocusToolInspectionLocator | null>(null);
  const toolDetailLoading = ref(false);
  const toolDetailError = ref(false);
  const toolDetailScanStatus = ref<'idle' | 'scanning' | 'not_found' | 'found' | 'cancelled' | 'error'>('idle');
  const toolDetailScannedItems = ref(0);
  const searchPage = ref<FocusThreadConversationSearchPage | null>(null);
  const searchLoading = ref(false);
  const searchError = ref(false);
  const toolRequestUnavailableReason = ref<FocusThreadInspectionUnavailableReason | null>(null);
  const searchRequestUnavailableReason = ref<FocusThreadInspectionUnavailableReason | null>(null);

  const disposed = ref(false);
  let toolGeneration = 0;
  let searchGeneration = 0;
  let searchNavigationGeneration = 0;
  let toolController: AbortController | null = null;
  let searchController: AbortController | null = null;
  let searchNavigationPending = false;

  function unavailableReason(
    capability: InspectionCapability,
    requestReason: FocusThreadInspectionUnavailableReason | null,
  ): FocusThreadInspectionUnavailableReason | null {
    if (disposed.value || options.isDisposed() || !options.accessAvailable.value) {
      return 'document_unavailable';
    }
    const threadId = options.activeThreadId.value;
    if (!threadId) return 'no_active_thread';
    const snapshot = options.snapshot.value;
    if (!snapshot || snapshot.thread.id !== threadId) return 'thread_not_materialized';
    if (snapshot.thread.history_mode === 'legacy') return 'legacy_history';
    const capabilities = options.metaCapabilities.value;
    if (capabilities === null) return 'document_unavailable';
    if (capabilities[capability] !== true) return 'build_unsupported';
    if (snapshot.thread.history_mode !== 'paginated') return 'unknown_history';
    if (requestReason !== null) return requestReason;
    return null;
  }

  function activeIdentity(): InspectionIdentity | null {
    if (disposed.value || options.isDisposed() || !options.accessAvailable.value) return null;
    const snapshot = options.snapshot.value;
    const threadId = options.activeThreadId.value;
    if (
      !snapshot
      || !threadId
      || snapshot.thread.id !== threadId
      || snapshot.thread.history_mode !== 'paginated'
    ) return null;
    return { threadId, runtimeEpoch: snapshot.runtime_epoch };
  }

  const toolDetailUnavailableReason = computed(() => unavailableReason(
    'tool_detail',
    toolRequestUnavailableReason.value,
  ));
  const historySearchUnavailableReason = computed(() => unavailableReason(
    'history_search',
    searchRequestUnavailableReason.value,
  ));
  const toolDetailAvailable = computed(() => toolDetailUnavailableReason.value === null);
  const historySearchAvailable = computed(() => historySearchUnavailableReason.value === null);

  function identityIsCurrent(identity: InspectionIdentity): boolean {
    const current = activeIdentity();
    return !disposed.value
      && !options.isDisposed()
      && current?.threadId === identity.threadId
      && current.runtimeEpoch === identity.runtimeEpoch;
  }

  function clearToolDetail(): void {
    toolGeneration += 1;
    toolController?.abort();
    toolController = null;
    toolDetail.value = null;
    toolDetailLocator.value = null;
    toolDetailLoading.value = false;
    toolDetailError.value = false;
    toolDetailScanStatus.value = 'idle';
    toolDetailScannedItems.value = 0;
  }

  function cancelToolDetail(): void {
    if (!toolDetailLoading.value) return;
    toolGeneration += 1;
    toolController?.abort();
    toolController = null;
    toolDetailLoading.value = false;
    toolDetailError.value = false;
    toolDetailScanStatus.value = 'cancelled';
  }

  function cancelSearchNavigation(): void {
    searchNavigationGeneration += 1;
    if (!searchNavigationPending) return;
    searchNavigationPending = false;
    options.cancelTurnCursorTarget();
  }

  function clearSearch(): void {
    searchGeneration += 1;
    searchController?.abort();
    searchController = null;
    cancelSearchNavigation();
    searchPage.value = null;
    searchLoading.value = false;
    searchError.value = false;
  }

  function beginToolDetailRead(
    retainPreview: boolean,
  ): { generation: number; controller: AbortController } {
    toolGeneration += 1;
    toolController?.abort();
    const controller = new AbortController();
    toolController = controller;
    if (!retainPreview) {
      toolDetail.value = null;
      toolDetailLocator.value = null;
    }
    toolDetailLoading.value = true;
    toolDetailError.value = false;
    toolDetailScanStatus.value = 'scanning';
    toolDetailScannedItems.value = 0;
    return { generation: toolGeneration, controller };
  }

  async function readToolDetailView(
    locator: FocusToolInspectionLocator,
    view: FocusToolDetailView,
    requestOptions: { retainPreview: boolean },
  ): Promise<boolean> {
    const identity = activeIdentity();
    if (!identity || !toolDetailAvailable.value) return false;
    const { generation, controller } = beginToolDetailRead(requestOptions.retainPreview);
    let cursor: string | null = null;
    const seenCursors = new Set<string>();
    try {
      while (true) {
        const page = await options.api.readToolDetail(
          identity.threadId,
          locator,
          view,
          controller.signal,
          cursor,
        );
        if (
          generation !== toolGeneration
          || controller.signal.aborted
          || !identityIsCurrent(identity)
          || page.thread_id !== identity.threadId
          || page.runtime_epoch !== identity.runtimeEpoch
        ) return false;
        toolDetailScannedItems.value += page.scanned_items;
        if (page.status === 'found' && page.detail !== null) {
          toolDetail.value = page.detail;
          toolDetailLocator.value = { ...locator };
          toolDetailScanStatus.value = 'found';
          toolRequestUnavailableReason.value = null;
          return true;
        }
        if (page.status === 'not_found') {
          toolDetailScanStatus.value = 'not_found';
          return false;
        }
        const nextCursor = page.next_cursor;
        if (nextCursor === null || seenCursors.has(nextCursor)) {
          throw new FocusApiError('Focus returned a non-progressing tool-detail cursor.', {
            status: 502,
            code: 'invalid_gateway_response',
          });
        }
        seenCursors.add(nextCursor);
        cursor = nextCursor;
      }
    } catch (error) {
      if (generation === toolGeneration && !abortError(error)) {
        if (isStaleWebReadError(error)) return false;
        toolRequestUnavailableReason.value = requestUnavailableReason(error);
        toolDetailError.value = true;
        toolDetailScanStatus.value = 'error';
        options.reportError(error);
      }
      return false;
    } finally {
      if (generation === toolGeneration) {
        if (toolController === controller) toolController = null;
        toolDetailLoading.value = false;
      }
    }
  }

  async function readToolDetail(
    locator: FocusToolInspectionLocator,
  ): Promise<boolean> {
    return readToolDetailView(locator, 'preview', { retainPreview: false });
  }

  async function readFullToolDetail(
    locator: FocusToolInspectionLocator,
  ): Promise<boolean> {
    if (
      toolDetail.value?.view !== 'preview'
      || toolDetailLocator.value === null
      || !sameToolInspectionLocator(toolDetailLocator.value, locator)
    ) return false;
    return readToolDetailView(locator, 'full', { retainPreview: true });
  }

  async function searchConversation(input: ConversationSearchInput): Promise<boolean> {
    const identity = activeIdentity();
    const query = input.query.trim();
    if (
      !identity
      || !historySearchAvailable.value
      || !query
      || Array.from(query).length > 256
    ) return false;
    searchGeneration += 1;
    const generation = searchGeneration;
    searchController?.abort();
    const controller = new AbortController();
    searchController = controller;
    cancelSearchNavigation();
    searchPage.value = null;
    searchLoading.value = true;
    searchError.value = false;
    try {
      const page = await options.api.searchConversation(
        identity.threadId,
        query,
        input.cursor ?? null,
        controller.signal,
      );
      if (
        generation !== searchGeneration
        || controller.signal.aborted
        || !identityIsCurrent(identity)
        || page.thread_id !== identity.threadId
        || page.runtime_epoch !== identity.runtimeEpoch
      ) return false;
      searchPage.value = page;
      searchRequestUnavailableReason.value = null;
      return true;
    } catch (error) {
      if (generation === searchGeneration && !abortError(error)) {
        if (isStaleWebReadError(error)) return false;
        searchRequestUnavailableReason.value = requestUnavailableReason(error);
        searchError.value = true;
        options.reportError(error);
      }
      return false;
    } finally {
      if (generation === searchGeneration) {
        if (searchController === controller) searchController = null;
        searchLoading.value = false;
      }
    }
  }

  async function resolveSearchOccurrence(
    occurrence: FocusConversationSearchOccurrence,
  ): Promise<string | null> {
    const identity = activeIdentity();
    if (
      !identity
      || !historySearchAvailable.value
      || !searchPage.value?.occurrences.includes(occurrence)
    ) return null;
    cancelSearchNavigation();
    const generation = ++searchNavigationGeneration;
    searchNavigationPending = true;
    try {
      const anchorId = await options.resolveTurnCursorTarget(
        occurrence.turn_cursor,
        occurrence.turn_id,
      );
      return generation === searchNavigationGeneration
        && identityIsCurrent(identity)
        ? anchorId
        : null;
    } finally {
      if (generation === searchNavigationGeneration) {
        searchNavigationPending = false;
      }
    }
  }

  const stopIdentityWatch = watch(
    () => {
      const snapshot = options.snapshot.value;
      return [
        options.activeThreadId.value,
        options.accessAvailable.value ? 'access' : '',
        snapshot?.thread.id ?? '',
        snapshot?.runtime_epoch ?? '',
        snapshot?.thread.history_mode ?? '',
        options.isDisposed() ? 'disposed' : '',
        options.metaCapabilities.value?.tool_detail === true ? 'tool' : '',
        options.metaCapabilities.value?.history_search === true ? 'search' : '',
      ].join('\u0000');
    },
    (current, previous) => {
      if (previous !== undefined && current !== previous) {
        toolRequestUnavailableReason.value = null;
        searchRequestUnavailableReason.value = null;
        clearToolDetail();
        clearSearch();
      }
    },
    { flush: 'sync' },
  );

  function clearAll(): void {
    clearToolDetail();
    clearSearch();
  }

  function dispose(): void {
    if (disposed.value) return;
    disposed.value = true;
    clearAll();
    stopIdentityWatch();
  }

  return {
    toolDetail,
    toolDetailLocator,
    toolDetailLoading,
    toolDetailError,
    toolDetailScanStatus,
    toolDetailScannedItems,
    searchPage,
    searchLoading,
    searchError,
    toolDetailAvailable,
    toolDetailUnavailableReason,
    historySearchAvailable,
    historySearchUnavailableReason,
    readToolDetail,
    readFullToolDetail,
    searchConversation,
    resolveSearchOccurrence,
    clearToolDetail,
    cancelToolDetail,
    clearSearch,
    clearAll,
    dispose,
  };
}
