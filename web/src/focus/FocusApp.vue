<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  provide,
  ref,
  shallowRef,
  watch,
} from 'vue';
import { useI18n } from 'vue-i18n';
import Sidebar from '../components/Sidebar.vue';
import ResizeHandle from '../components/ResizeHandle.vue';
import ConversationPane from '../components/chat/ConversationPane.vue';
import MobileTopBar from '../components/mobile/MobileTopBar.vue';
import MobileSwitcherSheet from '../components/mobile/MobileSwitcherSheet.vue';
import ModelPicker from '../components/settings/ModelPicker.vue';
import GlobalLoading from '../components/GlobalLoading.vue';
import Button from '../components/ui/Button.vue';
import Icon from '../components/ui/Icon.vue';
import IconButton from '../components/ui/IconButton.vue';
import SegmentedControl from '../components/ui/SegmentedControl.vue';
import ConfirmDialogHost from '../components/dialogs/ConfirmDialogHost.vue';
import type { AgentMember, ComposerCapabilities, PromptAttachment, ToolCall, ToolMedia, TurnAttachment } from '../types';
import type { ComposerSubmission } from '../components/chat/composerSubmission';
import { useAppearance } from '../composables/client/useAppearance';
import { useIsMobile } from '../composables/useIsMobile';
import { useSidebarLayout } from '../composables/useSidebarLayout';
import { useConfirmDialog } from '../composables/useConfirmDialog';
import { clampPanelWidth, useViewportWidth } from '../composables/useViewportWidth';
import { STORAGE_KEYS } from '../lib/storage';
import FocusGoalDialog from './FocusGoalDialog.vue';
import FocusPrimaryNotices from './FocusPrimaryNotices.vue';
import FocusReviewDialog from './FocusReviewDialog.vue';
import FocusSettingsDialog from './FocusSettingsDialog.vue';
import { executeCdCommand, parseCdCommand } from './cdCommand';
import {
  createFocusDocumentActivityFaviconPreference,
  syncFocusDocumentActivityFavicon,
} from './documentActivityFavicon';
import { DEFAULT_WEB_DISPLAY_NAME, syncFocusDocumentTitle } from './documentTitle';
import {
  FOCUS_DETAIL_PANEL_DEFAULT,
  FOCUS_DETAIL_PANEL_MIN,
  focusDetailPanelMaxWidth,
  nextFocusDetailSelection,
  resolveFocusThinkingText,
  shouldUseFocusDetailFullscreen,
  type FocusDetailSelection,
} from './focusDetailPanelState';
import {
  isLocalPossiblySentDraft,
  type UnknownSubmissionDraft,
} from './mutations/actions';
import {
  dispatchFocusComposerPayload,
} from './focusComposerSubmission';
import { projectOperatorStatusPresentation } from './operatorWarningPresentation';
import { projectRuntimeDetailsPresentation } from './runtimeDetailsPresentation';
import { useFocusWebClient } from './useFocusWebClient';
import type {
  FocusBackendResetPreview,
  FocusConversationSearchOccurrence,
  FocusThreadScope,
  FocusThreadToolDetailPayload,
  FocusToolInspectionLocator,
} from './types';

const { t } = useI18n();
const client = useFocusWebClient();
const activityFaviconPreference = createFocusDocumentActivityFaviconPreference();
const isMobile = useIsMobile();
const showMobileSwitcher = ref(false);
const showSettings = ref(false);
const showModelPicker = ref(false);
const showGoalDialog = ref(false);
const showReviewDialog = ref(false);
const detailSelection = ref<FocusDetailSelection | null>(null);
const detailTarget = computed(() => detailSelection.value?.kind ?? null);
const focusDetailPanel = shallowRef<
  (typeof import('./FocusDetailPanel.vue'))['default'] | null
>(null);
const detailPanelLoadState = ref<'idle' | 'loading' | 'ready' | 'error'>('idle');
let detailPanelLoadPromise: Promise<void> | null = null;
const detailOpen = computed(() => detailSelection.value !== null);
const conversationPaneRef = ref<InstanceType<typeof ConversationPane> | null>(null);
const unsupportedNotice = ref('');
let unsupportedNoticeTimer: ReturnType<typeof window.setTimeout> | null = null;
const { colorScheme, setColorScheme } = useAppearance();
const { confirm } = useConfirmDialog();
const { viewportWidth } = useViewportWidth();
const {
  SIDEBAR_WIDTH_KEY,
  SIDEBAR_DEFAULT,
  SIDEBAR_MIN,
  sidebarMax,
  sessionColWidth,
  sidebarCollapsed,
  sidebarDragging,
  sideWidth,
  loadSidebarCollapsed,
  toggleSidebarCollapse,
} = useSidebarLayout({ previewOpen: detailOpen });
const detailPanelWidth = ref(FOCUS_DETAIL_PANEL_DEFAULT);
const detailPanelMax = computed(() => focusDetailPanelMaxWidth(
  viewportWidth.value,
  sidebarCollapsed.value ? 0 : sideWidth.value,
));
const detailPanelFullscreen = computed(() => isMobile.value || shouldUseFocusDetailFullscreen(
  viewportWidth.value,
  sidebarCollapsed.value ? 0 : sideWidth.value,
));
const visibleDetailPanelWidth = computed(() => clampPanelWidth(
  detailPanelWidth.value,
  FOCUS_DETAIL_PANEL_MIN,
  detailPanelMax.value,
));
const detailPanelStyle = computed(() => (
  detailPanelFullscreen.value ? undefined : { width: `${visibleDetailPanelWidth.value}px` }
));

async function ensureDetailPanelLoaded(): Promise<void> {
  if (focusDetailPanel.value) {
    detailPanelLoadState.value = 'ready';
    return;
  }
  if (detailPanelLoadPromise) return detailPanelLoadPromise;
  detailPanelLoadState.value = 'loading';
  detailPanelLoadPromise = import('./FocusDetailPanel.vue')
    .then((module) => {
      focusDetailPanel.value = module.default;
      detailPanelLoadState.value = 'ready';
    })
    .catch(() => {
      // Keep failure local to this optional surface. The parent-side close
      // and reload controls remain in the eagerly loaded shell, so a
      // missing/stale hashed chunk can never trap a mobile document. Browsers
      // retain a failed module-map entry for this document, so an in-document
      // import retry would not be a reliable recovery contract.
      detailPanelLoadState.value = 'error';
    })
    .finally(() => {
      detailPanelLoadPromise = null;
    });
  return detailPanelLoadPromise;
}

// Focus deliberately has no workspace-file read API.  Returning null is an
// explicit refusal understood by Markdown.vue: local image syntax becomes an
// honest unavailable notice instead of a same-origin request for a server path.
provide('resolveImage', async () => null);

const activeSessionTitle = computed(() => {
  if (!client.activeThreadId.value) return t('focus.newConversation');
  return client.activeThread.value?.title ?? '';
});
syncFocusDocumentTitle(
  () => client.meta.value?.web_display_name ?? DEFAULT_WEB_DISPLAY_NAME,
  () => (
    client.activeThreadId.value
      ? client.activeThread.value?.title ?? ''
      : ''
  ),
);
syncFocusDocumentActivityFavicon(
  () => client.connection.value === 'connected',
  () => Boolean(client.activeThreadId.value) && client.running.value,
  () => activityFaviconPreference.enabled.value,
);
const workspaceSessionCount = computed(() => {
  const id = client.activeWorkspaceId.value;
  return client.workspaceGroups.value.find((group) => group.workspace.id === id)?.sessions.length ?? 0;
});
const attentionByWorkspace = computed<Record<string, number>>(() => {
  const result: Record<string, number> = {};
  for (const group of client.workspaceGroups.value) {
    result[group.workspace.id] = group.sessions.reduce((count, session) => (
      count + (session.pendingInteraction && session.pendingInteraction !== 'none' ? 1 : 0)
    ), 0);
  }
  return result;
});
const composerCapabilities = computed<Partial<ComposerCapabilities>>(() => ({
  commands: false,
  permissions: false,
  modes: false,
  compact: client.canCompact.value,
  model: true,
  effort: client.reasoningEffortOptions.value.length > 0,
  interrupt: client.canInterrupt.value,
  submit: client.canSubmit.value,
  submitWhileRunning: client.canSubmit.value,
}));
const activeNextTurnSettingsHint = computed(() => (
  client.running.value ? t('focus.nextTurnSettings') : ''
));
const sessionActionCapabilities = computed(() => client.activeSessionActionCapabilities.value);
const activeTurnContext = computed(() => {
  const snapshot = client.snapshot.value;
  const context = snapshot?.active_turn_context;
  return context && context.turn_id === snapshot.active_turn_id ? context : null;
});
const operatorStatusPresentation = computed(() => projectOperatorStatusPresentation(
  client.operatorStatus.value,
  client.operatorStatusFreshness.value,
));
const runtimeDetailsPresentation = computed(() => projectRuntimeDetailsPresentation({
  instance: client.meta.value?.instance ?? '',
  connection: client.connection.value,
  runtimeEpoch: client.runtimeEpoch.value,
  revision: client.revision.value,
  owner: client.owner.value,
  activeTurnContext: activeTurnContext.value,
  operatorStatus: operatorStatusPresentation.value,
  operatorStatusStale: client.operatorStatusStale.value,
  runtimeNotices: client.runtimeNoticePresentation.value,
}));
const runtimeDetailsAttentionCount = computed(() => (
  runtimeDetailsPresentation.value.primaryAttentionCount
  + runtimeDetailsPresentation.value.advisoryAttentionCount
));
const currentDocumentAccessAvailable = computed(() => (
  !client.authRequired.value && !client.documentReloadRequired.value
));
const conversationSearchVisible = computed(() => (
  Boolean(client.activeThreadId.value)
  && currentDocumentAccessAvailable.value
));
const resolvedThinkingText = computed(() => resolveFocusThinkingText(
  detailSelection.value,
  client.turns.value,
));
const thinkingText = computed(() => resolvedThinkingText.value ?? '');
function sameToolInspectionLocator(
  left: FocusToolInspectionLocator,
  right: FocusToolInspectionLocator,
): boolean {
  return left.turn_id === right.turn_id
    && left.item_id === right.item_id
    && left.kind === right.kind
    && left.change_index === right.change_index;
}

function visibleTool(
  toolId: string,
  locator?: FocusToolInspectionLocator,
): ToolCall | null {
  const tools = client.turns.value.flatMap((turn) => turn.tools ?? []);
  if (locator) {
    return tools.find((tool) => (
      tool.inspectionLocator
      && sameToolInspectionLocator(tool.inspectionLocator, locator)
    )) ?? null;
  }
  return tools.find((tool) => tool.id === toolId) ?? null;
}

function inspectableToolDetailLocator(tool: ToolCall): FocusToolInspectionLocator | null {
  const locator = tool.inspectionLocator;
  if (!locator || tool.status === 'running') return null;
  return locator;
}

const selectedToolDetail = computed<FocusThreadToolDetailPayload | null>(() => {
  const selection = detailSelection.value;
  const locator = client.toolDetailLocator.value;
  if (
    selection?.kind !== 'toolDiff'
    || !selection.inspectionLocator
    || !locator
    || !sameToolInspectionLocator(selection.inspectionLocator, locator)
  ) return null;
  return client.toolDetail.value;
});
const selectedToolDetailChangeIndex = computed(() => {
  const selection = detailSelection.value;
  return selection?.kind === 'toolDiff'
    ? selection.inspectionLocator?.change_index ?? null
    : null;
});
const toolDetailTool = computed<ToolCall | null>(() => {
  const selection = detailSelection.value;
  if (selection?.kind !== 'toolDiff') return null;
  const detail = selectedToolDetail.value;
  if (detail?.view === 'preview') {
    return detail.tool;
  }
  return visibleTool(selection.toolId, selection.inspectionLocator);
});
const mediaTarget = computed<ToolMedia | null>(() => (
  detailSelection.value?.kind === 'media' ? detailSelection.value.media : null
));
const agentMember = computed<AgentMember | null>(() => {
  const selection = detailSelection.value;
  if (selection?.kind !== 'agent') return null;
  const task = client.tasks.value.find((candidate) => candidate.id === selection.taskId);
  if (!task) return null;
  const failed = task.state === 'fail';
  const completed = task.state === 'done';
  const pending = task.state === 'pending';
  return {
    id: task.id,
    toolCallId: task.parentToolCallId,
    name: task.name,
    phase: failed ? 'failed' : completed ? 'completed' : pending ? 'suspended' : 'working',
    status: failed ? 'failed' : completed ? 'completed' : 'running',
    prompt: task.prompt,
    subagentType: task.metadata?.join(' · ') || task.meta,
    outputLines: task.progress,
    text: task.result?.join('\n'),
  };
});
const detailPayloadAvailable = computed(() => {
  const selection = detailSelection.value;
  if (!selection) return true;
  if (selection.kind === 'runtimeDetails') return true;
  if (selection.kind === 'thinking') return resolvedThinkingText.value !== null;
  if (selection.kind === 'toolDiff') {
    return selectedToolDetail.value?.view === 'full' || toolDetailTool.value !== null;
  }
  if (selection.kind === 'conversationSearch') return true;
  if (selection.kind === 'agent') return agentMember.value !== null;
  return selection.media.kind === 'image' && selection.media.url.length > 0;
});

async function openWorkspaceDraft(workspace: string) {
  const outcome = await client.openWorkspaceDraft(workspace);
  if (
    outcome.committed
    && outcome.previousComposerScopeId
  ) {
    if (
      outcome.composerScopeEffect === 'clearPrevious'
      || (
        outcome.composerScopeEffect === 'apply'
        && outcome.attachmentDisposition === 'invalidated'
      )
    ) {
      conversationPaneRef.value?.clearComposerAttachmentsForSession(
        outcome.previousComposerScopeId,
      );
    } else if (
      outcome.composerScopeEffect === 'apply'
      &&
      outcome.attachmentDisposition === 'rebound'
      && outcome.currentComposerScopeId
    ) {
      conversationPaneRef.value?.rebindComposerAttachmentsForSession(
        outcome.previousComposerScopeId,
        outcome.currentComposerScopeId,
      );
    }
  }
  return outcome;
}

async function handleSubmit(payload: ComposerSubmission): Promise<void> {
  // Navigation/profile confirmation owns writer-scope readiness.  The leaf
  // Composer also gates every input path, but keep this shell boundary
  // fail-closed so a stale/synthetic component event cannot cross into /cd or
  // a prompt mutation while the visible target is still optimistic.
  if (!client.scopeReady.value) {
    payload.retain();
    return;
  }
  // Avoid even one microtask yield before a real prompt reaches the mutation
  // boundary. `/cd` is the sole shell-owned Composer command on this path.
  if (parseCdCommand(payload.text)) {
    if (!payload.commit()) return;
    await executeCdCommand(payload.text, payload.attachments, {
      currentDirectory: () => (
        client.activeThread.value?.cwd
        || client.activeWorkspaceId.value
        || client.meta.value?.default_working_dir
        || '-'
      ),
      openWorkspaceDraft,
      restoreDraft: loadComposerDraft,
      showCurrentDirectory: (directory) => {
        showTransientNotice(t('focus.cdCurrent', { directory }));
      },
      showAttachmentsInvalidated: () => {
        showTransientNotice(t('focus.cdAttachmentsInvalidated'));
      },
    });
    return;
  }
  await dispatchFocusComposerPayload(
    payload,
    (text, attachments) => client.submit(
      text,
      attachments,
      () => payload.retainTextOnly(),
    ),
  );
}

async function loadComposerDraft(
  text: string,
  attachments: ReadonlyArray<PromptAttachment>,
): Promise<boolean> {
  await nextTick();
  return commitComposerDraft(text, attachments);
}

function commitComposerDraft(
  text: string,
  attachments: ReadonlyArray<PromptAttachment>,
): boolean {
  const accepted = conversationPaneRef.value?.loadComposerForEdit(
    text,
    attachments.map((attachment) => ({
      kind: attachment.kind,
      fileId: attachment.fileId,
      url: `/api/attachments/${encodeURIComponent(attachment.fileId)}`,
      name: attachment.name,
      mediaType: attachment.mediaType,
      size: attachment.size,
    })),
  ) === true;
  if (accepted) conversationPaneRef.value?.focusComposer();
  return accepted;
}

async function prepareComposerRecovery(
  targetComposerScopeId: string,
  text: string,
  attachments: ReadonlyArray<PromptAttachment>,
): Promise<() => boolean> {
  await nextTick();
  return () => conversationPaneRef.value?.loadComposerRecovery(
    targetComposerScopeId,
    text,
    attachments.map((attachment) => ({
      kind: attachment.kind,
      fileId: attachment.fileId,
      url: `/api/attachments/${encodeURIComponent(attachment.fileId)}`,
      name: attachment.name,
      mediaType: attachment.mediaType,
      size: attachment.size,
    })),
  ) === true;
}

async function retryUnknownSubmission(draft: UnknownSubmissionDraft): Promise<void> {
  const recovered = await client.takeUnknownSubmissionForRetry(
    (recovered, targetComposerScopeId) => prepareComposerRecovery(
      targetComposerScopeId,
      recovered.text,
      recovered.attachments,
    ),
    draft.attemptKey,
  );
  if (recovered?.handoffReason === 'possibly_sent') {
    showTransientNotice(t(recovered.hadAttachments
      ? 'focus.possiblySentDraftRestoredWithAttachments'
      : 'focus.possiblySentDraftRestoredTextOnly'));
  }
}

function canRecoverUnknownSubmission(draft: UnknownSubmissionDraft): boolean {
  const localPossiblySentHandoff = isLocalPossiblySentDraft(draft);
  return !draft.recoveryBlocked
    && (localPossiblySentHandoff
      ? client.activeThreadId.value === draft.threadId
        || client.connection.value === 'connected'
      : client.connection.value === 'connected')
    && !!draft.threadId
    && localPossiblySentHandoff;
}

async function confirmProcessLocalMutationDiscard(
  threadId: string,
  operation: string,
): Promise<void> {
  const approved = await confirm({
    title: t('focus.unknownControlUnlockTitle'),
    message: t('focus.unknownControlUnlockConfirm', { threadId, operation }),
    confirmLabel: t('focus.unlockUnknownControl'),
    cancelLabel: t('focus.cancel'),
    variant: 'danger',
  });
  if (approved) await client.discardUnknownProcessLocalMutation(threadId);
}

function handleCopyMessageToComposer(payload: { text: string; attachments?: TurnAttachment[] }): void {
  conversationPaneRef.value?.loadComposerForEdit(payload.text, payload.attachments);
  conversationPaneRef.value?.focusComposer();
}

function selectDetail(requested: FocusDetailSelection): boolean {
  const next = nextFocusDetailSelection(detailSelection.value, requested);
  if (!next) {
    closeDetail();
    return false;
  }
  // A detail selection owns the sole browser-local tool slot. Replacing any
  // selection must release a prior full source before the next preview starts.
  client.clearToolDetail();
  if (next.kind !== 'conversationSearch') client.clearConversationSearch();
  detailSelection.value = next;
  return true;
}

function openRuntimeDetails(): void {
  selectDetail({ kind: 'runtimeDetails' });
}

function openThinking(target: { turnId: string; blockIndex: number }): void {
  const turn = client.turns.value.find((item) => item.id === target.turnId);
  const block = turn?.blocks?.[target.blockIndex];
  if (block?.kind !== 'thinking') return;
  selectDetail({
    kind: 'thinking',
    turnId: target.turnId,
    blockIndex: target.blockIndex,
    ...(block.itemId ? { itemId: block.itemId } : {}),
  });
}

function openToolDiff(tool: ToolCall): void {
  const current = visibleTool(tool.id, tool.inspectionLocator);
  if (!current) {
    closeDetail();
    return;
  }
  const inspectionLocator = inspectableToolDetailLocator(current);
  const opened = selectDetail({
    kind: 'toolDiff',
    toolId: current.id,
    ...(inspectionLocator ? { inspectionLocator } : {}),
  });
  if (!opened) return;
  if (inspectionLocator && client.toolDetailAvailable.value) {
    void client.readToolDetail(inspectionLocator);
  }
  else client.clearToolDetail();
}

function loadFullToolDetail(): void {
  const selection = detailSelection.value;
  if (
    selection?.kind !== 'toolDiff'
    || !selection.inspectionLocator
    || selectedToolDetail.value?.view !== 'preview'
  ) return;
  void client.readFullToolDetail(selection.inspectionLocator);
}

function openConversationSearch(): void {
  if (!conversationSearchVisible.value) return;
  selectDetail({ kind: 'conversationSearch' });
}

function resolveConversationTocTarget(turnId: string): Promise<boolean> {
  // Retire a search-owned cursor intent before Prompt navigation creates its
  // newer history intent; a later panel close must not cancel that Prompt.
  if (detailSelection.value?.kind === 'conversationSearch') closeDetail();
  return client.resolveHistoryPromptTarget(turnId);
}

function searchConversation(query: string): void {
  void client.searchConversation({ query });
}

function loadNextConversationSearchPage(): void {
  const page = client.conversationSearchPage.value;
  if (!page?.next_cursor) return;
  void client.searchConversation({
    query: page.query,
    cursor: page.next_cursor,
  });
}

async function selectConversationSearchOccurrence(
  occurrence: FocusConversationSearchOccurrence,
): Promise<void> {
  const threadId = client.activeThreadId.value;
  const runtimeEpoch = client.snapshot.value?.runtime_epoch ?? '';
  const anchorId = await client.resolveConversationSearchOccurrence(occurrence);
  if (
    !anchorId
    || client.activeThreadId.value !== threadId
    || client.snapshot.value?.runtime_epoch !== runtimeEpoch
  ) return;
  closeDetail();
  await nextTick();
  if (
    client.activeThreadId.value !== threadId
    || client.snapshot.value?.runtime_epoch !== runtimeEpoch
  ) return;
  conversationPaneRef.value?.scrollToRenderedTurn(anchorId);
}

function openMedia(media: ToolMedia): void {
  // Focus deliberately projects a browser media surface only for controlled
  // images. A future upstream media shape must not turn into a video/audio
  // player merely because the generic Kimi renderer knows how to display one.
  if (media.kind !== 'image' || !media.url) {
    showUnsupported();
    return;
  }
  selectDetail({ kind: 'media', media });
}

function openAgent(target: string): void {
  const task = client.tasks.value.find((candidate) => (
    candidate.id === target || candidate.parentToolCallId === target
  ));
  if (!task) {
    showUnsupported();
    return;
  }
  selectDetail({ kind: 'agent', taskId: task.id });
}

function closeDetail(): void {
  detailSelection.value = null;
  client.clearToolDetail();
  client.clearConversationSearch();
}

function showTransientNotice(message: string): void {
  unsupportedNotice.value = message;
  if (unsupportedNoticeTimer !== null) window.clearTimeout(unsupportedNoticeTimer);
  unsupportedNoticeTimer = window.setTimeout(() => {
    unsupportedNotice.value = '';
    unsupportedNoticeTimer = null;
  }, 4000);
}

function showUnsupported(): void {
  showTransientNotice(t('focus.fileUnavailable'));
}

async function confirmArchiveThread(threadId: string): Promise<void> {
  const approved = await confirm({
    title: t('sidebar.archive'),
    message: t('sidebar.archiveConfirm'),
    confirmLabel: t('sidebar.archive'),
    cancelLabel: t('focus.cancel'),
    variant: 'danger',
  });
  if (approved) await client.archiveThread(threadId);
}

async function confirmBackendReset(preview: FocusBackendResetPreview): Promise<void> {
  const force = preview.status === 'force-only';
  let refusedBeforePost = false;
  try {
    await confirm({
      title: t('focus.backendResetConfirmTitle', { instance: preview.instance }),
      message: t(
        force
          ? 'focus.backendResetConfirmForceMessage'
          : 'focus.backendResetConfirmSafeMessage',
        {
          instance: preview.instance,
          pending: preview.pending_request_count,
          running: preview.running_binding_count,
          attached: preview.attached_binding_count,
          activeThreads: preview.active_loaded_thread_count,
          loadedThreads: preview.loaded_thread_count,
        },
      ),
      confirmLabel: t(
        force ? 'focus.backendResetForceExecute' : 'focus.backendResetExecute',
      ),
      cancelLabel: t('focus.cancel'),
      confirmationText: preview.instance,
      confirmationLabel: t('focus.backendResetConfirmationLabel', {
        instance: preview.instance,
      }),
      confirmationPlaceholder: preview.instance,
      variant: 'danger',
      action: async () => {
        const outcome = await client.executeBackendReset(preview);
        if (outcome.disposition === 'not-started') {
          refusedBeforePost = true;
          throw new Error('backend reset preview changed before execution');
        }
      },
    });
  } catch {
    if (refusedBeforePost) showTransientNotice(t('focus.backendResetPreviewChanged'));
    // Known HTTP failures use the shared error banner; an unknown POST result
    // uses the document-lifetime reset banner and must not offer retry.
  }
}

async function submitGoal(objective: string): Promise<void> {
  try {
    await client.createGoal(objective);
    showGoalDialog.value = false;
  } catch {
    // The shared error banner already carries the operation failure.
  }
}

async function submitReview(target: Record<string, unknown>): Promise<void> {
  try {
    await client.review(target);
    showReviewDialog.value = false;
  } catch {
    // Keep the dialog open so the target can be corrected and retried.
  }
}

let appHeightRaf = 0;
function setAppHeight(): void {
  const viewport = window.visualViewport;
  document.documentElement.style.setProperty('--app-height', `${viewport?.height ?? window.innerHeight}px`);
  document.documentElement.style.setProperty('--app-top', `${viewport?.offsetTop ?? 0}px`);
}
function syncAppHeight(): void {
  if (appHeightRaf) return;
  appHeightRaf = requestAnimationFrame(() => {
    appHeightRaf = 0;
    setAppHeight();
  });
}

onMounted(() => {
  loadSidebarCollapsed();
  setAppHeight();
  window.visualViewport?.addEventListener('resize', syncAppHeight);
  window.visualViewport?.addEventListener('scroll', syncAppHeight);
  window.addEventListener('resize', syncAppHeight);
  void client.load();
});

watch(client.activeThreadId, closeDetail);
watch(currentDocumentAccessAvailable, (available) => {
  if (!available) closeDetail();
}, { flush: 'sync' });
watch(conversationSearchVisible, (visible) => {
  if (!visible && detailSelection.value?.kind === 'conversationSearch') closeDetail();
}, { flush: 'sync' });
watch(detailPayloadAvailable, (available) => {
  if (!available) closeDetail();
}, { flush: 'sync' });
watch(detailOpen, (open) => {
  if (open) void ensureDetailPanelLoaded();
}, { flush: 'sync' });

onUnmounted(() => {
  client.dispose();
  if (unsupportedNoticeTimer !== null) window.clearTimeout(unsupportedNoticeTimer);
  window.visualViewport?.removeEventListener('resize', syncAppHeight);
  window.visualViewport?.removeEventListener('scroll', syncAppHeight);
  window.removeEventListener('resize', syncAppHeight);
  if (appHeightRaf) cancelAnimationFrame(appHeightRaf);
  document.documentElement.style.removeProperty('--app-height');
  document.documentElement.style.removeProperty('--app-top');
});
</script>

<template>
  <div class="focus-shell">
    <section v-if="client.authRequired.value" class="auth-page">
      <div class="auth-page-inner">
        <div class="focus-lockup"><span>F</span><strong>Focus Web</strong></div>
        <div class="auth-copy">
          <h1>{{ t('focus.authTitle') }}</h1>
          <p>{{ t('focus.authMessage') }}</p>
        </div>
        <Button variant="primary" @click="client.reloadDocument">
          <Icon name="refresh" size="md" />
          {{ t('focus.reloadPage') }}
        </Button>
      </div>
    </section>

    <section v-else-if="client.initialized.value && !client.meta.value" class="auth-page">
      <div class="auth-page-inner">
        <div class="focus-lockup"><span>F</span><strong>Focus Web</strong></div>
        <div class="auth-copy">
          <h1>{{ t('focus.loadFailed') }}</h1>
          <p>{{ client.errorMessage.value }}</p>
        </div>
        <Button variant="primary" :loading="client.loading.value" @click="client.load">
          <Icon name="refresh" size="md" />
          {{ t('focus.retry') }}
        </Button>
      </div>
    </section>

    <div
      v-else
      class="focus-app"
      :class="{ mobile: isMobile, 'sidebar-collapsed': sidebarCollapsed && !isMobile }"
    >
      <template v-if="!isMobile">
        <Sidebar
          :collapsed="sidebarCollapsed"
          :dragging="sidebarDragging"
          :col-width="sideWidth"
          :active-workspace="client.visibleWorkspace.value"
          :active-workspace-id="client.activeWorkspaceId.value"
          :sessions="client.sessions.value"
          :search-sessions="client.searchSessions.value"
          :groups="client.workspaceGroups.value"
          :active-id="client.activeThreadId.value"
          :pending-by-session="client.pendingBySession.value"
          workspace-sort-mode="recent"
          backend="v2"
          allow-create
          :allow-workspace-create="false"
          :allow-session-actions="true"
          :session-action-capabilities="sessionActionCapabilities"
          :allow-workspace-actions="false"
          :allow-workspace-reorder="false"
          @select="client.selectThread($event)"
          @rename="(id, title) => client.renameThread(id, title)"
          @archive="confirmArchiveThread($event)"
          @create="openWorkspaceDraft(client.activeWorkspaceId.value)"
          @create-in-workspace="openWorkspaceDraft($event)"
          @open-settings="showSettings = true"
          @collapse="toggleSidebarCollapse"
          @load-all-sessions="client.loadAllSessionsForSearch()"
        >
          <template #directory-controls>
            <SegmentedControl
              class="directory-scope"
              :model-value="client.threadScope.value"
              :options="[
                { value: 'current', label: t('focus.currentInstance') },
                { value: 'global', label: t('focus.globalThreads') },
              ]"
              size="sm"
              @update:model-value="client.setThreadScope($event as FocusThreadScope)"
            />
            <button
              type="button"
              class="runtime-details-entry"
              @click="openRuntimeDetails"
            >
              <span
                class="runtime-details-dot"
                :class="runtimeDetailsPresentation.tone"
                aria-hidden="true"
              />
              <span>{{ t('focus.runtimeDetailsTitle') }}</span>
              <span
                v-if="runtimeDetailsAttentionCount > 0"
                class="runtime-details-count"
              >{{ runtimeDetailsAttentionCount }}</span>
            </button>
          </template>
        </Sidebar>
        <ResizeHandle
          v-show="!sidebarCollapsed"
          class="side-handle"
          :storage-key="SIDEBAR_WIDTH_KEY"
          :default-width="SIDEBAR_DEFAULT"
          :min="SIDEBAR_MIN"
          :max="sidebarMax"
          @update:width="sessionColWidth = $event"
          @update:dragging="sidebarDragging = $event"
        />
      </template>

      <MobileTopBar
        v-else
        :workspace="client.visibleWorkspace.value"
        :session-title="activeSessionTitle"
        :running="client.running.value"
        :session-count="workspaceSessionCount"
        @open-switcher="showMobileSwitcher = true"
        @open-settings="showSettings = true"
      />
      <IconButton
        v-if="isMobile"
        class="runtime-details-mobile-trigger"
        size="sm"
        :label="t('focus.runtimeDetailsOpen')"
        @click="openRuntimeDetails"
      >
        <Icon name="info" size="sm" />
        <span
          class="runtime-details-dot"
          :class="runtimeDetailsPresentation.tone"
          aria-hidden="true"
        />
      </IconButton>

      <main class="focus-main">
        <FocusPrimaryNotices
          :document-reload-required="client.documentReloadRequired.value"
          :backend-reset-outcome-unknown="client.backendResetOutcomeUnknown.value"
          :error-message="client.errorMessage.value"
          :primary-runtime-errors="runtimeDetailsPresentation.primaryRuntimeErrors"
          :primary-operator-warning-count="operatorStatusPresentation.primaryWarningCount"
          :operator-error-count="operatorStatusPresentation.errorWarningCount"
          :operator-degraded-without-details="operatorStatusPresentation.degradedWithoutDetails"
          :unknown-submission-drafts="client.unknownSubmissionDrafts.value"
          :unknown-process-local-mutations="client.unknownProcessLocalMutations.value"
          :unknown-lifecycle-mutations="client.unknownLifecycleMutations.value"
          :mutation-busy-by-thread="client.mutationBusyByThread.value"
          :connection="client.connection.value"
          :can-recover-unknown-submission="canRecoverUnknownSubmission"
          @reload="client.reloadDocument"
          @open-runtime-details="openRuntimeDetails"
          @retry-unknown-submission="retryUnknownSubmission"
          @discard-unknown-submission="client.discardUnknownSubmission($event)"
          @unlock-process-local-mutation="confirmProcessLocalMutationDiscard($event.threadId, $event.operation)"
          @verify-lifecycle-mutation="client.verifyUnknownLifecycleMutation($event)"
          @unlock-lifecycle-mutation="client.unlockUnknownLifecycleMutation($event)"
        />

        <div v-if="unsupportedNotice" class="transient-notice" role="status">
          {{ unsupportedNotice }}
        </div>

        <ConversationPane
          ref="conversationPaneRef"
          :mobile="isMobile"
          :turns="client.turns.value"
          :session-id="client.activeThreadId.value"
          :composer-session-id="client.composerScopeId.value"
          :composer-ready="client.scopeReady.value"
          :approvals="client.pendingApprovals.value"
          :tasks="client.tasks.value"
          :status="client.status.value"
          :thinking="client.composerThinking.value"
          :models="client.models.value"
          :composer-model-settings-hint="activeNextTurnSettingsHint"
          :questions="client.questions.value"
          :pending-question-actions="client.pendingQuestionActions.value"
          :pending-approval-actions="client.pendingApprovalActions.value"
          :interaction-enabled="client.connection.value === 'connected'"
          :running="client.running.value"
          :interrupt-enabled="client.canInterrupt.value"
          :turn-active="client.running.value"
          :working="client.running.value"
          :starting="client.starting.value"
          :draft-create-outcome-unknown="client.unknownThreadCreateDraftExists.value"
          :file-reload-key="client.activeThreadId.value || 'draft'"
          :session-loading="client.conversationLoading.value"
          :has-more-messages="client.historyHasMore.value"
          :loading-more="client.loadingMore.value"
          :loading-more-error="client.loadingMoreError.value"
          :load-older-messages="client.loadOlderMessages"
          :return-to-live-tail="client.returnToLiveTail"
          :workspace-name="client.visibleWorkspace.value?.name"
          :workspace-root="client.visibleWorkspace.value?.root ?? client.status.value.cwd"
          :workspaces="client.workspacesView.value"
          :active-workspace-id="client.activeWorkspaceId.value"
          :session-title="activeSessionTitle"
          :conversation-toc="true"
          :conversation-toc-items="client.historyOutline.value"
          :conversation-toc-truncated="client.historyOutlineTruncated.value"
          :conversation-toc-has-more="client.historyOutlineHasMore.value"
          :conversation-toc-loading-more="client.historyOutlineLoading.value"
          :load-more-conversation-toc="client.loadMoreHistoryOutline"
          :resolve-conversation-toc-target="resolveConversationTocTarget"
          :conversation-search-visible="conversationSearchVisible"
          :composer-capabilities="composerCapabilities"
          :defer-submit-clear="true"
          :upload-image="client.meta.value?.capabilities.attachments ? client.uploadAttachment : undefined"
          :download-file="client.downloadAttachment"
          :session-actions="true"
          :session-action-capabilities="sessionActionCapabilities"
          :goal="client.goal.value"
          :allow-workspace-create="false"
          :tool-diff-panel="true"
          :tool-detail-available="client.toolDetailAvailable.value"
          @select-workspace="openWorkspaceDraft($event)"
          @submit="handleSubmit"
          @approval="(requestId, response) => client.respondApproval(requestId, response)"
          @answer="(requestId, response) => client.respondQuestion(requestId, response)"
          @dismiss="client.dismissQuestion($event)"
          @interrupt="client.interrupt"
          @compact="client.compact"
          @control-goal="client.controlGoal($event)"
          @set-thinking="client.setThinking($event)"
          @pick-model="showModelPicker = true"
          @select-model="client.selectModel($event)"
          @open-thinking="openThinking"
          @open-file="showUnsupported"
          @open-media="openMedia"
          @open-tool-diff="openToolDiff"
          @open-agent="openAgent"
          @search-conversation="openConversationSearch"
          @copy-message-to-composer="handleCopyMessageToComposer"
          @rename-session="(id, title) => client.renameThread(id, title)"
          @archive-session="confirmArchiveThread($event)"
          @review-session="showReviewDialog = true"
          @goal-session="showGoalDialog = true"
        />
      </main>

      <IconButton
        v-if="!isMobile && sidebarCollapsed"
        class="sidebar-toggle-btn"
        size="sm"
        :label="t('sidebar.expandSidebar')"
        @click="toggleSidebarCollapse"
      >
        <Icon name="panel-expand" />
      </IconButton>

      <IconButton
        v-if="!isMobile && sidebarCollapsed"
        class="runtime-details-collapsed-trigger"
        size="sm"
        :label="t('focus.runtimeDetailsOpen')"
        @click="openRuntimeDetails"
      >
        <Icon name="info" size="sm" />
        <span
          class="runtime-details-dot"
          :class="runtimeDetailsPresentation.tone"
          aria-hidden="true"
        />
      </IconButton>

      <ResizeHandle
        v-if="detailOpen && !detailPanelFullscreen"
        class="detail-handle"
        :storage-key="STORAGE_KEYS.detailPanelWidth"
        :default-width="FOCUS_DETAIL_PANEL_DEFAULT"
        :min="FOCUS_DETAIL_PANEL_MIN"
        :max="detailPanelMax"
        reverse
        :aria-label="t('focus.resizeDetailPanelAria')"
        @update:width="detailPanelWidth = $event"
      />

      <aside
        v-if="detailOpen"
        class="focus-detail"
        :class="{ fullscreen: detailPanelFullscreen }"
        :style="detailPanelStyle"
        role="complementary"
        :aria-label="t('layout.detailPanelAria')"
      >
        <div
          v-if="detailPanelLoadState !== 'ready' || !focusDetailPanel"
          class="focus-detail-load-state"
          :role="detailPanelLoadState === 'error' ? 'alert' : 'status'"
        >
          <div class="focus-detail-load-header">
            <strong>{{ t('layout.detailPanelAria') }}</strong>
            <IconButton
              size="sm"
              :label="t('tasks.closePanel')"
              @click="closeDetail"
            >
              <Icon name="close" size="sm" />
            </IconButton>
          </div>
          <p>
            {{ t(detailPanelLoadState === 'error' ? 'focus.detailLoadFailed' : 'focus.detailLoading') }}
          </p>
          <Button
            v-if="detailPanelLoadState === 'error'"
            size="sm"
            variant="secondary"
            @click="client.reloadDocument"
          >
            {{ t('focus.reloadPage') }}
          </Button>
        </div>
        <component
          :is="focusDetailPanel"
          v-else-if="detailTarget"
          :target="detailTarget"
          :runtime-details-presentation="runtimeDetailsPresentation"
          :thinking-text="thinkingText"
          :tool="toolDetailTool"
          :tool-detail="selectedToolDetail"
          :tool-detail-change-index="selectedToolDetailChangeIndex"
          :tool-detail-loading="client.toolDetailLoading.value"
          :tool-detail-error="client.toolDetailError.value"
          :tool-detail-scan-status="client.toolDetailScanStatus.value"
          :tool-detail-scanned-items="client.toolDetailScannedItems.value"
          :tool-detail-unavailable-reason="client.toolDetailUnavailableReason.value"
          :conversation-search-unavailable-reason="client.conversationSearchUnavailableReason.value"
          :conversation-search-loading="client.conversationSearchLoading.value"
          :conversation-search-error="client.conversationSearchError.value"
          :conversation-search-page="client.conversationSearchPage.value"
          :media-target="mediaTarget"
          :agent-member="agentMember"
          @close="closeDetail"
          @cancel-tool-detail="client.cancelToolDetail"
          @load-full-tool-detail="loadFullToolDetail"
          @search-conversation="searchConversation"
          @next-conversation-search-page="loadNextConversationSearchPage"
          @select-conversation-search-occurrence="selectConversationSearchOccurrence"
        />
      </aside>

      <ModelPicker
        v-if="showModelPicker"
        :models="client.models.value"
        :current="client.selectedModelId.value"
        :starred-ids="[]"
        :loading="false"
        :unavailable="false"
        :settings-hint="activeNextTurnSettingsHint"
        @select="client.selectModel($event); showModelPicker = false"
        @toggle-star="() => undefined"
        @close="showModelPicker = false"
      />

      <FocusSettingsDialog
        v-model:open="showSettings"
        :color-scheme="colorScheme"
        :turn-window-limit="client.turnWindowLimit.value"
        :activity-favicon-enabled="activityFaviconPreference.enabled.value"
        :connection="client.connection.value"
        :approval-policy="client.approvalPolicy.value"
        :approval-policies="client.meta.value?.approval_policies ?? []"
        :reasoning-effort="client.thinking.value ?? ''"
        :reasoning-effort-options="client.reasoningEffortOptions.value"
        :permissions-profile-id="client.permissionsProfileId.value"
        :permissions-profiles="client.meta.value?.permissions_profiles ?? []"
        :archived-threads="client.archivedThreads.value"
        :archived-loading="client.archivedLoading.value"
        :archived-truncated="client.archivedTruncated.value"
        :archived-limit="client.archivedLimit.value"
        :lifecycle-busy-by-thread="client.mutationBusyByThread.value"
        :backend-reset-preview="client.backendResetPreview.value"
        :backend-reset-result="client.backendResetResult.value"
        :backend-reset-loading="client.backendResetLoading.value"
        :backend-reset-busy="client.backendResetBusy.value"
        :backend-reset-outcome-unknown="client.backendResetOutcomeUnknown.value"
        @set-color-scheme="setColorScheme"
        @set-turn-window-limit="client.setTurnWindowLimit($event)"
        @set-activity-favicon-enabled="activityFaviconPreference.setEnabled($event)"
        @set-approval-policy="client.setApprovalPolicy($event)"
        @set-reasoning-effort="client.setReasoningEffort($event)"
        @set-permissions-profile="client.setPermissionsProfile($event)"
        @refresh-archived="client.refreshArchivedThreads"
        @unarchive="client.unarchiveThread($event)"
        @delete-thread="(threadId, confirmation) => client.deleteThread(threadId, confirmation)"
        @refresh-backend-reset="client.refreshBackendReset"
        @confirm-backend-reset="confirmBackendReset"
      />

      <FocusGoalDialog
        v-model:open="showGoalDialog"
        :objective="client.goal.value?.objective"
        :loading="client.actionBusy.value"
        :enabled="client.connection.value === 'connected'"
        @submit="submitGoal"
      />

      <FocusReviewDialog
        v-model:open="showReviewDialog"
        :loading="client.actionBusy.value"
        :enabled="client.connection.value === 'connected'"
        @submit="submitReview"
      />

      <MobileSwitcherSheet
        v-if="isMobile"
        v-model="showMobileSwitcher"
        :groups="client.workspaceGroups.value"
        :active-workspace-id="client.activeWorkspaceId.value"
        :active-id="client.activeThreadId.value"
        :attention-by-workspace="attentionByWorkspace"
        allow-create
        :allow-workspace-create="false"
        :allow-session-actions="true"
        :allow-workspace-actions="false"
        @select="client.selectThread($event)"
        @create="openWorkspaceDraft(client.activeWorkspaceId.value)"
        @create-in-workspace="openWorkspaceDraft($event)"
        @rename="(id, title) => client.renameThread(id, title)"
        @archive="confirmArchiveThread($event)"
      >
        <template #controls>
          <SegmentedControl
            class="directory-scope mobile-scope"
            :model-value="client.threadScope.value"
            :options="[
              { value: 'current', label: t('focus.currentInstance') },
              { value: 'global', label: t('focus.globalThreads') },
            ]"
            size="sm"
            @update:model-value="client.setThreadScope($event as FocusThreadScope)"
          />
        </template>
      </MobileSwitcherSheet>
    </div>

    <ConfirmDialogHost />

    <Transition name="gload-fade">
      <GlobalLoading v-if="!client.initialized.value" :issue="client.errorMessage.value" />
    </Transition>
  </div>
</template>

<style scoped>
.focus-shell {
  position: fixed;
  top: var(--app-top, 0px);
  left: 0;
  right: 0;
  height: var(--app-height, 100dvh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--color-bg);
}
.focus-app {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: auto 0 minmax(0, 1fr) 0 auto;
  overflow: hidden;
  background: var(--color-bg);
  color: var(--color-text);
}
.focus-app > * {
  min-width: 0;
  min-height: 0;
}
.focus-app > .side { grid-column: 1; }
.side-handle { grid-column: 2; }
.focus-main {
  grid-column: 3;
  display: flex;
  flex-direction: column;
  min-height: 0;
  position: relative;
}
.detail-handle { grid-column: 4; }
.focus-main > :deep(.con) {
  flex: 1;
  min-height: 0;
}
.focus-detail {
  grid-column: 5;
  width: min(440px, 42vw);
  min-width: 320px;
  height: 100%;
  border-left: 1px solid var(--color-line);
  background: var(--color-surface);
}
.focus-detail.fullscreen {
  position: fixed;
  inset: 0;
  z-index: var(--z-modal);
  width: auto;
  min-width: 0;
  border-left: 0;
}
.focus-detail-load-state {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-3);
}
.focus-detail-load-header {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}
.focus-detail-load-state p {
  margin: 0;
  color: var(--color-text-secondary);
}
.runtime-details-entry {
  width: 100%;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-muted);
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.runtime-details-entry:hover { color: var(--color-text); background: var(--color-surface-sunken); }
.runtime-details-entry:focus-visible { outline: none; box-shadow: var(--p-focus-ring); }
.runtime-details-dot {
  width: 7px;
  height: 7px;
  flex: none;
  border-radius: var(--radius-full);
  background: var(--color-text-faint);
}
.runtime-details-dot.advisory { background: var(--color-warning); }
.runtime-details-dot.danger { background: var(--color-danger); }
.runtime-details-count {
  min-width: 18px;
  margin-left: auto;
  padding: 1px 5px;
  border-radius: var(--radius-full);
  background: var(--color-surface-sunken);
  color: var(--color-text);
  font-size: var(--text-xs);
  text-align: center;
}
.runtime-details-mobile-trigger,
.runtime-details-collapsed-trigger {
  position: absolute;
  z-index: var(--z-sticky);
  background: var(--color-surface-raised);
  border-color: var(--color-line);
  box-shadow: var(--shadow-xs);
}
.runtime-details-mobile-trigger {
  top: calc(var(--safe-top) + 12px);
  right: calc(max(12px, var(--safe-right)) + 46px);
}
.runtime-details-collapsed-trigger {
  top: calc(var(--space-3) + 36px);
  left: var(--space-3);
}
.runtime-details-mobile-trigger .runtime-details-dot,
.runtime-details-collapsed-trigger .runtime-details-dot {
  position: absolute;
  top: 3px;
  right: 3px;
}
.transient-notice {
  position: absolute;
  z-index: var(--z-sticky);
  top: var(--space-3);
  left: 50%;
  max-width: min(520px, calc(100% - var(--space-6)));
  transform: translateX(-50%);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--color-warning-bd);
  border-radius: var(--radius-md);
  background: var(--color-warning-soft);
  color: var(--color-text);
  box-shadow: var(--shadow-md);
  overflow-wrap: anywhere;
}
.directory-scope {
  width: 100%;
  margin: 0 0 var(--space-2);
}
.mobile-scope { padding-inline: var(--space-2); }
.auth-page {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-6);
}
.auth-page-inner {
  width: min(440px, 100%);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-5);
}
.focus-lockup {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  color: var(--color-text);
  font-size: var(--text-xl);
}
.focus-lockup span {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 32px;
  border-radius: var(--radius-md);
  background: var(--color-accent);
  color: var(--color-text-on-accent);
  font-size: var(--text-base);
  font-weight: 500;
}
.focus-lockup strong { font-weight: 500; }
.auth-copy { display: flex; flex-direction: column; gap: var(--space-2); }
.auth-copy h1 {
  margin: 0;
  color: var(--color-text);
  font-size: var(--text-2xl);
  font-weight: 500;
  letter-spacing: 0;
}
.auth-copy p {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--text-base);
  line-height: var(--leading-relaxed);
}
.sidebar-toggle-btn {
  position: absolute;
  top: var(--space-3);
  left: var(--space-3);
  z-index: var(--z-sticky);
}
.gload-fade-leave-active { transition: opacity var(--duration-slow) var(--ease-out); }
.gload-fade-leave-to { opacity: 0; }
.focus-app.mobile {
  display: flex;
  flex-direction: column;
}
.focus-app.mobile .focus-main {
  flex: 1;
  min-height: 0;
}
@media (max-width: 640px) {
  .transient-notice { top: var(--space-2); }
}
</style>
