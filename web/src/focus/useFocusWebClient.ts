import { computed, ref } from 'vue';
import { i18n } from '../i18n';
import { FocusWebApi, type FocusWebApiPort } from './api';
import { ClientIntentClock } from './clientIntentClock';
import { decodeFocusOperatorStatus } from './projectionEventDecoder';
import { createFocusTransportSession } from './focusTransportSession';
import { createFocusClientView } from './focusClientView';
import {
  createFocusBackendResetTransaction,
  type FocusBackendResetExecutionOutcome,
} from './focusBackendReset';
import {
  createFocusMutationActions,
  type FocusMutationActions,
} from './mutations/actions';
export type {
  UnknownLifecycleMutation,
  UnknownProcessLocalMutation,
  UnknownSubmissionDraft,
  UnknownSubmissionHandoff,
} from './mutations/actions';
import { createFocusProjectionSync } from './focusProjectionSync';
import type { FocusProjectionSync } from './focusProjectionSync';
import { createFocusHistoryNavigation } from './focusHistoryNavigation';
import {
  createFocusNavigationProfile,
} from './focusNavigationProfile';
import { createWebNextTurnSettings } from './client-state/web-next-turn-settings';
import { createBrowserTurnWindow } from './client-state/browser-turn-window';
import { createThreadInspection } from './client-state/thread-inspection';
import { createRuntimeNoticeOwner } from './client-state/runtime-notices';
import {
  FocusApiError,
  isStaleWebReadError,
  type FocusMeta,
  type FocusOperatorStatus,
  type FocusOperatorStatusFreshness,
} from './types';

const THREAD_QUERY_KEY = 'thread';
export { AUTO_MODEL_ID } from './focusClientView';
const DEFAULT_OPERATOR_STATUS_POLL_MS = 15_000;
const MIN_OPERATOR_STATUS_POLL_MS = 1_000;
const MAX_OPERATOR_STATUS_POLL_MS = 60_000;

function updateThreadQuery(threadId: string): void {
  const url = new URL(window.location.href);
  if (threadId) url.searchParams.set(THREAD_QUERY_KEY, threadId);
  else url.searchParams.delete(THREAD_QUERY_KEY);
  history.replaceState(history.state, '', `${url.pathname}${url.search}`);
}

export function useFocusWebClient(api: FocusWebApiPort = new FocusWebApi()) {
  // The Gateway may replace a copied sessionStorage hint with a distinct
  // writer identity. Keep the acknowledged identity reactive so two tabs on
  // the same thread never share one localStorage composer key.
  const operatorStatus = ref<FocusOperatorStatus | null>(null);
  const operatorStatusFreshness = ref<FocusOperatorStatusFreshness>('loading');
  const operatorStatusStale = computed(() => operatorStatusFreshness.value === 'stale');
  const initialized = ref(false);
  const loading = ref(false);
  const authRequired = ref(false);
  // A copied/reloaded document can lose its memory-only document capability
  // while another document keeps the resumable client hint.  Do not keep
  // retrying that stale capability: a browser reload must register a fresh
  // document identity first.
  const documentReloadRequired = ref(false);
  const errorMessage = ref('');
  let operatorStatusTimer: ReturnType<typeof setTimeout> | null = null;
  let operatorStatusRefreshPromise: Promise<void> | null = null;
  const intentClock = new ClientIntentClock();
  const turnWindow = createBrowserTurnWindow();
  let projection!: FocusProjectionSync;
  let mutationActions!: FocusMutationActions;
  let clearHistoryView = (): void => {};
  const settings = createWebNextTurnSettings({
    api,
    modelIsAvailable: (modelId) => (
      modelId === 'focus:auto'
      || projection?.meta.value?.models.some((model) => model.id === modelId) === true
    ),
    supportedReasoningEfforts: (modelId) => (
      projection?.meta.value?.models
        .find((model) => model.id === modelId)
        ?.supported_reasoning_efforts
        .map((option) => option.effort) ?? []
    ),
    runtimeEpochMismatch: () => projection?.invalidateWireProjection(),
    reportError,
  });
  const navigation = createFocusNavigationProfile({
    intentClock,
    api,
    initialClientId: api.clientId,
    defaultWorkspace: () => projection.meta.value?.default_working_dir ?? '',
    clearSnapshot: () => projection.clearSnapshot(),
    clearHistoryView: () => clearHistoryView(),
    updateThreadQuery,
    reportError,
    clearError: () => {
      errorMessage.value = '';
    },
    setNavigationLoading: (value) => {
      loading.value = value;
    },
    threadUnavailableReason: (threadId) => {
      const listed = projection.threads.value.find((thread) => thread.id === threadId);
      return listed?.selectable === false
        ? listed.unavailable_reason
          || `Open this thread from Focus instance ${listed.loaded_instance || 'that owns it'}.`
        : '';
    },
    workspaceNavigationBlockReason: () => (
      activeThreadId.value
      && mutationActions.owner.value.relation === 'self'
      && (
        mutationActions.running.value
        || (snapshot.value?.pending_requests.length ?? 0) > 0
      )
        ? 'Wait for this browser-owned operation to finish before starting a new workspace conversation.'
        : ''
    ),
  });
  const {
    threadScope,
    activeThreadId,
    draftWorkspaceId,
    scopeReady,
    composerReady,
  } = navigation;
  const runtimeNotices = createRuntimeNoticeOwner(activeThreadId);
  projection = createFocusProjectionSync({
    api,
    intentClock,
    turnWindowLimit: turnWindow.limit,
    navigation,
    settings,
    transport: {
      hasOpenedEventSocket: () => transportSession.snapshot.value.hasOpenedEventSocket,
      requestProjectionReload: () => transportSession.requestProjectionReload(),
      scheduleProjectionReloadRetry: () => transportSession.scheduleProjectionReloadRetry(),
      cancelProjectionReloadRetry: () => transportSession.cancelProjectionReloadRetry(),
      resetProjectionReloadBackoff: () => transportSession.resetProjectionReloadBackoff(),
      scheduleProjectionRefresh: () => transportSession.scheduleProjectionRefresh(),
      scheduleThreadListRefresh: () => transportSession.scheduleThreadListRefresh(),
    },
    reportError,
    requireAuthentication,
    projectionAccessIsAvailable: () => !authRequired.value && !documentReloadRequired.value,
    currentErrorMessage: () => errorMessage.value,
    clearErrorMessageIf: (message) => {
      if (message && errorMessage.value === message) errorMessage.value = '';
    },
    activeThreadWasRemoved: () => {
      navigation.clearToRepairDraft(
        workspacesView.value[0]?.id || meta.value?.default_working_dir || '',
      );
    },
    captureUnknownMutationSnapshot: (threadId) => (
      mutationActions.captureUnknownMutationSnapshot(threadId)
    ),
    reconcileUnknownMutation: (receipt, mutation) => {
      return mutationActions.reconcileUnknownMutation(receipt, mutation);
    },
    settleUnknownMutationFromEvent: (threadId, mutationId, operation, disposition) => {
      mutationActions.settleUnknownMutationFromEvent(
        threadId,
        mutationId,
        operation,
        disposition,
      );
    },
  });
  navigation.bindProjection(projection);
  const {
    meta: metaEnvelope,
    threads,
    searchThreads,
    archivedThreads,
    snapshot,
    runtimeEpoch,
    revision,
    snapshotInvalidated,
    archivedLoading,
    archivedTruncated,
    archivedLimit,
  } = projection;
  // Request loading can finish before a covering snapshot is installed when
  // navigation converges through fresh metadata and projection recovery stays
  // in the background. Keep that selected target in an honest loading state;
  // only an authoritative targetless profile may expose the new-chat surface.
  const conversationLoading = computed(() => (
    loading.value
    || (
      activeThreadId.value !== ''
      && snapshot.value?.thread.id !== activeThreadId.value
    )
  ));
  const historyNavigation = createFocusHistoryNavigation({
    api,
    snapshot,
    activeThreadId,
    turnLimit: turnWindow.limit,
    reportError,
    isDisposed: () => navigation.isDisposed,
  });
  clearHistoryView = historyNavigation.clearHistoryWindow;
  const meta = computed<FocusMeta | null>(() => {
    const envelope = metaEnvelope.value;
    const writerProfile = navigation.writerProfile.value;
    const nextTurnSettings = settings.snapshot.value;
    return envelope && writerProfile && nextTurnSettings
      ? {
          ...envelope,
          writer_profile: writerProfile,
          next_turn_settings: nextTurnSettings,
        }
      : null;
  });
  const threadInspection = createThreadInspection({
    api,
    metaCapabilities: computed(() => {
      const capabilities = metaEnvelope.value?.capabilities;
      return capabilities
        ? {
            tool_detail: capabilities.tool_detail,
            history_search: capabilities.history_search,
          }
        : null;
    }),
    accessAvailable: computed(() => (
      !authRequired.value && !documentReloadRequired.value
    )),
    snapshot,
    activeThreadId,
    resolveTurnCursorTarget: historyNavigation.resolveTurnCursorTarget,
    cancelTurnCursorTarget: historyNavigation.cancelDetailIntent,
    reportError,
    isDisposed: () => navigation.isDisposed,
  });
  const thinking = settings.reasoningEffort;
  // Focus's empty wire value is explicit Auto, not an absent Kimi preference.
  // Feed Composer its non-concrete "on" sentinel so shared model defaults do
  // not materialize a fake Low/High label while effort segments stay usable.
  const composerThinking = computed(() => thinking.value ?? 'on');
  const approvalPolicy = settings.approvalPolicy;
  const permissionsProfileId = settings.permissionsProfileId;
  const refreshThreads = projection.refreshThreads;
  const refreshArchivedThreads = projection.refreshArchivedThreads;
  const refreshActiveThread = projection.refreshActiveThread;
  const reloadAll = projection.reloadAll;
  const backendReset = createFocusBackendResetTransaction({ api, reloadAll });
  const transportSession = createFocusTransportSession({
    mayConnect: () => (
      !authRequired.value
      && !documentReloadRequired.value
      && meta.value !== null
    ),
    openEventSocket: (handlers) => api.connectEvents(handlers),
    probeEventAccess: async () => {
      await api.meta();
    },
    onHandshakeProbeError: (error) => {
      if (requiresDocumentReload(error)) {
        requireDocumentReload();
        return 'stop';
      }
      if (error instanceof FocusApiError && error.status === 401) {
        requireAuthentication();
        return 'stop';
      }
      return 'retry';
    },
    onConnectionError: reportError,
    onConnected: (reconnected) => {
      // Runtime health is an independent projection and every event socket
      // boundary is a freshness boundary for it.
      void refreshOperatorStatus();
      if (reconnected) transportSession.requestProjectionReload();
    },
    onEvent: (event) => {
      runtimeNotices.handleEvent(event);
      projection.handleEvent(event);
    },
    onInvalidEvent: () => {
      runtimeNotices.reset();
      projection.invalidateWireProjection();
    },
    refreshProjection: async () => {
      await refreshThreads();
      await refreshActiveThread();
    },
    refreshThreadList: async () => {
      await refreshThreads();
    },
    reloadProjection: reloadAll,
    mayRetryProjectionReload: () => (
      projection.mayRetryProjectionReload()
    ),
    onScheduledTaskError: reportError,
  });
  const connection = computed(() => transportSession.snapshot.value.connection);

  function operatorStatusPollDelayMs(): number {
    const requested = Number(operatorStatus.value?.poll_after_seconds) * 1_000;
    if (!Number.isFinite(requested) || requested <= 0) {
      return DEFAULT_OPERATOR_STATUS_POLL_MS;
    }
    return Math.min(Math.max(requested, MIN_OPERATOR_STATUS_POLL_MS), MAX_OPERATOR_STATUS_POLL_MS);
  }

  function clearOperatorStatusTimer(): void {
    if (operatorStatusTimer === null) return;
    clearTimeout(operatorStatusTimer);
    operatorStatusTimer = null;
  }

  function scheduleOperatorStatusRefresh(): void {
    clearOperatorStatusTimer();
    if (
      transportSession.snapshot.value.disposed
      || authRequired.value
      || documentReloadRequired.value
    ) return;
    operatorStatusTimer = setTimeout(() => {
      operatorStatusTimer = null;
      void refreshOperatorStatus();
    }, operatorStatusPollDelayMs());
  }

  async function refreshOperatorStatus(): Promise<void> {
    clearOperatorStatusTimer();
    if (operatorStatusRefreshPromise) return operatorStatusRefreshPromise;
    if (
      transportSession.snapshot.value.disposed
      || authRequired.value
      || documentReloadRequired.value
    ) return;
    operatorStatusRefreshPromise = (async () => {
      try {
        const nextStatus = decodeFocusOperatorStatus(await api.operatorStatus());
        if (!nextStatus) throw new Error('Focus Web received a malformed operator status projection.');
        if (transportSession.snapshot.value.disposed) return;
        operatorStatus.value = nextStatus;
        operatorStatusFreshness.value = 'fresh';
      } catch (error) {
        if (transportSession.snapshot.value.disposed) return;
        if (requiresDocumentReload(error)) {
          requireDocumentReload();
        } else if (error instanceof FocusApiError && error.status === 401) {
          requireAuthentication();
        } else {
          // Keep the last successful warning projection visible. A failed
          // health read is uncertainty, never proof that old warnings
          // recovered or that no new warning exists.
          operatorStatusFreshness.value = 'stale';
        }
      } finally {
        operatorStatusRefreshPromise = null;
        scheduleOperatorStatusRefresh();
      }
    })();
    return operatorStatusRefreshPromise;
  }

  const clientView = createFocusClientView({
    meta,
    threads,
    searchThreads,
    snapshot,
    snapshotInvalidated,
    connection,
    threadScope,
    activeThreadId,
    draftWorkspaceId,
    scopeReady,
    settingsModel: settings.model,
  });
  const {
    reasoningEffortOptions,
    models,
    selectedModelId,
    workspacesView,
    sessions,
    searchSessions,
    workspaceGroups,
    activeThread,
    activeSessionActionCapabilities,
    canCompact,
    activeWorkspaceId,
    visibleWorkspace,
    status,
    turns: snapshotTurns,
    tasks,
    goal,
    pendingApprovals,
    questions,
    pendingBySession,
  } = clientView;
  // The client view keeps semantic state derived from the authoritative live
  // snapshot. Presentation alone may replace that snapshot's recent tail with
  // one bounded historical detail page.
  void snapshotTurns;
  const turns = historyNavigation.visibleTurns;
  const composerScopeId = navigation.composerScopeId;
  mutationActions = createFocusMutationActions({
    api,
    intentClock,
    navigation,
    projection,
    connection,
    activeThread,
    canCompact,
    attachmentsAreAvailable: () => (
      meta.value?.capabilities.attachments === true
      && !snapshotInvalidated.value
      && !authRequired.value
      && !documentReloadRequired.value
    ),
    defaultDraftWorkspace: () => (
      workspacesView.value[0]?.id || meta.value?.default_working_dir || ''
    ),
    promptMessage: (key, values) => String(i18n.global.t(
      `focus.${key}`,
      values ?? { reason: '' },
    )),
    reportError,
    reportFatalError,
    clearError: () => {
      errorMessage.value = '';
    },
  });
  const {
    starting,
    actionBusy,
    mutationBusyByThread,
    unknownLifecycleMutations,
    unknownProcessLocalMutations,
    unknownSubmissionDraft,
    unknownSubmissionDrafts,
    running,
    owner,
    canSubmit,
    unknownThreadCreateDraftExists,
    canRetryUnknownSubmission,
    canInterrupt,
    pendingApprovalActions,
    pendingQuestionActions,
    reconcilePromptResultsForThread,
    renameThread,
    compact,
    review,
    createGoal,
    controlGoal,
    archiveThread,
    unarchiveThread,
    deleteThread,
    uploadAttachment,
    submit: submitMutation,
    discardUnknownSubmission,
    takeUnknownSubmissionForRetry,
    verifyUnknownLifecycleMutation,
    unlockUnknownLifecycleMutation,
    discardUnknownProcessLocalMutation,
    interrupt,
    respondApproval,
    respondQuestion,
    dismissQuestion,
  } = mutationActions;

  function submit(
    ...parameters: Parameters<FocusMutationActions['submit']>
  ): ReturnType<FocusMutationActions['submit']> {
    historyNavigation.clearHistoryWindow();
    return submitMutation(...parameters);
  }

  function requireAuthentication(): void {
    authRequired.value = true;
    errorMessage.value = '';
    clearOperatorStatusTimer();
    transportSession.suspend();
  }

  function requiresDocumentReload(error: unknown): boolean {
    return error instanceof FocusApiError
      && (error.code === 'document_replaced' || error.code === 'document_unregistered');
  }

  function requireDocumentReload(): void {
    documentReloadRequired.value = true;
    errorMessage.value = '';
    clearOperatorStatusTimer();
    transportSession.suspend();
  }

  function reportFatalError(error: unknown): boolean {
    if (!requiresDocumentReload(error)
      && !(error instanceof FocusApiError && error.status === 401)) return false;
    reportError(error);
    return true;
  }

  function reportError(error: unknown): void {
    // A staged read can legitimately lose its document/observation fence
    // while a newer read is becoming authoritative.  The owning read path
    // drops that result; keep the internal 409 out of the user-facing banner
    // when it reaches this shared presentation boundary.
    if (isStaleWebReadError(error)) return;
    if (requiresDocumentReload(error)) {
      requireDocumentReload();
      return;
    }
    if (error instanceof FocusApiError && error.status === 401) {
      requireAuthentication();
      return;
    }
    errorMessage.value = error instanceof Error ? error.message : String(error);
  }

  async function refreshBackendReset(): Promise<void> {
    errorMessage.value = '';
    const observed = await backendReset.refreshPreview();
    if (observed !== null) return;
    const error = backendReset.previewError.value;
    if (error !== null && !reportFatalError(error)) reportError(error);
  }

  async function executeBackendReset(
    preview: Parameters<typeof backendReset.execute>[0],
  ): Promise<FocusBackendResetExecutionOutcome> {
    errorMessage.value = '';
    const outcome = await backendReset.execute(preview);
    if (outcome.disposition === 'known-no-effect') {
      const presentedError = outcome.refreshError ?? outcome.error;
      if (!reportFatalError(presentedError)) reportError(presentedError);
    } else if (outcome.disposition === 'succeeded' && outcome.reloadError !== null) {
      reportError(outcome.reloadError);
    }
    return outcome;
  }

  function reconcileMaterializedPromptResults(): void {
    const threadId = activeThreadId.value;
    if (!threadId || !scopeReady.value
      || snapshot.value?.thread.id !== threadId) return;
    void reconcilePromptResultsForThread(threadId);
  }

  async function selectThread(threadId: string): Promise<void> {
    await navigation.selectThread(threadId);
    reconcileMaterializedPromptResults();
  }
  const openWorkspaceDraft = navigation.openWorkspaceDraft;

  const setThreadScope = navigation.changeThreadScope;

  async function loadAllSessionsForSearch(): Promise<void> {
    await projection.loadAllSessionsForSearch();
  }

  function downloadAttachment(fileId: string): Promise<Blob> {
    return api.attachmentBlob(fileId);
  }

  async function loadOlderMessages(sessionId: string): Promise<boolean> {
    if (sessionId !== activeThreadId.value) return false;
    return historyNavigation.loadOlderPage();
  }

  function returnToLiveTail(): void {
    historyNavigation.clearHistoryWindow();
  }

  let turnWindowChangeGeneration = 0;
  async function setTurnWindowLimit(value: unknown): Promise<void> {
    if (!turnWindow.setLimit(value)) return;
    const generation = ++turnWindowChangeGeneration;
    historyNavigation.resetForTurnLimitChange();
    projection.applyTurnWindowLimit();
    if (!activeThreadId.value || navigation.isDisposed) {
      await historyNavigation.completeTurnLimitChange(false);
      return;
    }
    let installed = false;
    try {
      installed = await projection.refreshActiveThread();
    } catch (error) {
      if (generation === turnWindowChangeGeneration && !navigation.isDisposed) {
        reportError(error);
      }
    }
    if (generation !== turnWindowChangeGeneration || navigation.isDisposed) return;
    await historyNavigation.completeTurnLimitChange(installed);
  }

  const selectModel = settings.selectModel;
  const setThinking = settings.setThinking;
  const setReasoningEffort = settings.setReasoningEffort;
  const setApprovalPolicy = settings.setApprovalPolicy;
  const setPermissionsProfile = settings.setPermissionsProfile;

  async function load(): Promise<void> {
    if (navigation.isDisposed) return;
    // Capture the deep link before initial durable-profile convergence may
    // replace browser history with that profile's target.
    const requestedThreadId = new URL(window.location.href)
      .searchParams.get(THREAD_QUERY_KEY)?.trim() ?? '';
    loading.value = true;
    authRequired.value = false;
    errorMessage.value = '';
    try {
      const initialMeta = await api.initialize();
      if (navigation.isDisposed) return;
      intentClock.rebase(api.intentGenerationFloor);
      navigation.registerClient(api.clientId);
      projection.installInitialMeta(initialMeta);
      mutationActions.installInitialState(initialMeta);
      // Runtime health is an independent, ephemeral projection. It must not
      // hold the durable thread shell or event connection behind its deadline.
      void refreshOperatorStatus();
      await refreshThreads();
      if (navigation.isDisposed) return;
      await navigation.restoreInitialTarget({
        requestedThreadId,
        recoveryThreadId: unknownSubmissionDraft.value?.threadId.trim() ?? '',
        // Session-local unknown evidence is a retry fence, not selection
        // authority. A concrete recovery target still wins inside the owner;
        // otherwise materialize the durable server selection exactly.
        persistedThreadId: initialMeta.writer_profile.selected_thread_id.trim(),
      });
      if (navigation.isDisposed) return;
      reconcileMaterializedPromptResults();
      transportSession.connect();
    } catch (error) {
      if (!navigation.isDisposed) reportError(error);
    } finally {
      if (!navigation.isDisposed) {
        initialized.value = true;
        loading.value = false;
      }
    }
  }

  function dispose(): void {
    // The document is leaving; pending stream deltas no longer have a visible
    // consumer. Cancel their presentation callbacks instead of doing one last
    // Markdown/DOM pass during teardown.
    projection.dispose();
    backendReset.dispose();
    mutationActions.dispose();
    settings.dispose();
    threadInspection.dispose();
    historyNavigation.dispose();
    runtimeNotices.reset();
    navigation.dispose();
    loading.value = false;
    clearOperatorStatusTimer();
    transportSession.dispose();
  }

  function reloadDocument(): void {
    window.location.reload();
  }

  return {
    api,
    meta,
    operatorStatus,
    operatorStatusFreshness,
    operatorStatusStale,
    runtimeNoticePresentation: runtimeNotices.presentation,
    backendResetPreview: backendReset.preview,
    backendResetResult: backendReset.result,
    backendResetLoading: backendReset.previewPending,
    backendResetBusy: backendReset.executing,
    backendResetOutcomeUnknown: backendReset.outcomeUnknown,
    threads,
    archivedThreads,
    snapshot,
    activeThreadId,
    threadScope,
    initialized,
    loading,
    conversationLoading,
    starting,
    loadingMore: historyNavigation.loading,
    loadingMoreError: historyNavigation.error,
    historyHasMore: historyNavigation.hasMore,
    historyOutline: historyNavigation.outline,
    historyOutlineTruncated: historyNavigation.outlineTruncated,
    historyOutlineLoading: historyNavigation.outlineLoading,
    historyOutlineError: historyNavigation.outlineError,
    historyOutlineHasMore: historyNavigation.outlineHasMore,
    toolDetail: threadInspection.toolDetail,
    toolDetailLocator: threadInspection.toolDetailLocator,
    toolDetailLoading: threadInspection.toolDetailLoading,
    toolDetailError: threadInspection.toolDetailError,
    toolDetailScanStatus: threadInspection.toolDetailScanStatus,
    toolDetailScannedItems: threadInspection.toolDetailScannedItems,
    toolDetailAvailable: threadInspection.toolDetailAvailable,
    toolDetailUnavailableReason: threadInspection.toolDetailUnavailableReason,
    conversationSearchPage: threadInspection.searchPage,
    conversationSearchLoading: threadInspection.searchLoading,
    conversationSearchError: threadInspection.searchError,
    conversationSearchAvailable: threadInspection.historySearchAvailable,
    conversationSearchUnavailableReason: threadInspection.historySearchUnavailableReason,
    archivedLoading,
    archivedTruncated,
    archivedLimit,
    turnWindowLimit: turnWindow.limit,
    actionBusy,
    mutationBusyByThread,
    authRequired,
    documentReloadRequired,
    errorMessage,
    unknownSubmissionDraft,
    unknownSubmissionDrafts,
    unknownLifecycleMutations,
    unknownProcessLocalMutations,
    connection,
    runtimeEpoch,
    revision,
    snapshotInvalidated,
    models,
    reasoningEffortOptions,
    selectedModelId,
    thinking,
    composerThinking,
    approvalPolicy,
    permissionsProfileId,
    nextTurnSettings: settings.snapshot,
    workspacesView,
    workspaceGroups,
    sessions,
    searchSessions,
    activeWorkspaceId,
    composerScopeId,
    scopeReady,
    composerReady,
    visibleWorkspace,
    activeThread,
    activeSessionActionCapabilities,
    canCompact,
    turns,
    tasks,
    goal,
    running,
    owner,
    canSubmit,
    unknownThreadCreateDraftExists,
    canRetryUnknownSubmission,
    canInterrupt,
    status,
    pendingApprovals,
    questions,
    pendingBySession,
    pendingApprovalActions,
    pendingQuestionActions,
    load,
    dispose,
    reloadDocument,
    reloadAll,
    refreshOperatorStatus,
    refreshBackendReset,
    executeBackendReset,
    selectThread,
    setThreadScope,
    loadAllSessionsForSearch,
    openWorkspaceDraft,
    renameThread,
    compact,
    review,
    createGoal,
    controlGoal,
    archiveThread,
    refreshArchivedThreads,
    unarchiveThread,
    deleteThread,
    uploadAttachment,
    downloadAttachment,
    submit,
    discardUnknownSubmission,
    takeUnknownSubmissionForRetry,
    verifyUnknownLifecycleMutation,
    unlockUnknownLifecycleMutation,
    discardUnknownProcessLocalMutation,
    loadOlderMessages,
    returnToLiveTail,
    resolveHistoryPromptTarget: historyNavigation.resolvePromptTarget,
    loadMoreHistoryOutline: historyNavigation.loadMoreOutline,
    readToolDetail: threadInspection.readToolDetail,
    readFullToolDetail: threadInspection.readFullToolDetail,
    clearToolDetail: threadInspection.clearToolDetail,
    cancelToolDetail: threadInspection.cancelToolDetail,
    searchConversation: threadInspection.searchConversation,
    resolveConversationSearchOccurrence: threadInspection.resolveSearchOccurrence,
    clearConversationSearch: threadInspection.clearSearch,
    interrupt,
    respondApproval,
    respondQuestion,
    dismissQuestion,
    selectModel,
    setThinking,
    setReasoningEffort,
    setApprovalPolicy,
    setPermissionsProfile,
    setTurnWindowLimit,
  };
}
