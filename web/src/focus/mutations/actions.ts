import { computed, ref } from 'vue';
import type { ComputedRef, Ref } from 'vue';
import type { ApprovalResponse, PromptAttachment, QuestionResponse } from '../../types';
import type { UploadedAttachment } from '../../composables/useAttachmentUpload';
import type { FocusWebApiPort } from '../api';
import type { ClientIntentClock } from '../clientIntentClock';
import type { PendingQuestionAction } from '../client-state/pending-request-actions';
import type {
  UnknownLifecycleMutation,
  UnknownMutationSnapshotReceipt,
  UnknownProcessLocalMutation,
} from '../client-state/thread-mutations';
import type {
  ConfirmedWriterScopeReceipt,
  FocusNavigationProfile,
} from '../focusNavigationProfile';
import type { FocusProjectionSync } from '../focusProjectionSync';
import {
  FocusApiError,
  type FocusMeta,
  type FocusMutationDisposition,
  type FocusPromptResultReceipt,
  type FocusThreadSnapshot,
  type FocusThreadSummary,
} from '../types';
import {
  createWebPromptMutationId,
  isWebPromptClientUserMessageId,
} from '../webPromptMutation';
import {
  createFirstPromptPossiblySentDraft,
  createUnknownSubmissionDraftStore,
  THREAD_CREATE_FIRST_PROMPT_OPERATION,
  type UnknownSubmissionDraft,
  type UnknownSubmissionHandoff,
} from '../webPromptMutationRecovery';
import {
  createWebPromptResultLocatorStore,
  type WebPromptResultLocator,
} from '../webPromptResultLocators';
import { createPendingRequestResponses } from './pending-request-responses';
import {
  createThreadMutationActions,
} from './thread-mutation-actions';

export type {
  UnknownSubmissionCommit,
  UnknownSubmissionDraft,
  UnknownSubmissionHandoff,
  UnknownSubmissionRecoveryPhase,
} from '../webPromptMutationRecovery';

type PossiblySentRetryResult = UnknownSubmissionDraft & {
  handoffReason: 'possibly_sent';
  hadAttachments: boolean;
  recoveryRecordState: 'removed' | 'locked_removal_failed';
};

export type UnknownSubmissionRetryResult = PossiblySentRetryResult;

export type {
  UnknownLifecycleMutation,
  UnknownProcessLocalMutation,
} from '../client-state/thread-mutations';

export type FocusPromptMessageKey =
  | 'promptKnownNoEffect'
  | 'promptKnownNoEffectAfterReload'
  | 'promptOutcomeUnknown'
  | 'promptPending'
  | 'promptAttachmentRollbackFailed'
  | 'promptIdentityUnavailable'
  | 'promptResultUnavailable'
  | 'promptResultLookupFailed';

type FocusMutationApiPort = Pick<
  FocusWebApiPort,
  | 'clientId'
  | 'renameThread'
  | 'compactThread'
  | 'startReview'
  | 'setGoal'
  | 'clearGoal'
  | 'archiveThread'
  | 'unarchiveThread'
  | 'deleteThread'
  | 'uploadAttachment'
  | 'startThread'
  | 'submitPrompt'
  | 'readPromptResult'
  | 'resolveUnknownMutation'
  | 'verifyUnknownLifecycleMutation'
  | 'interrupt'
  | 'respondRequest'
>;

export interface FocusMutationActionsOptions {
  api: FocusMutationApiPort;
  intentClock: ClientIntentClock;
  navigation: Pick<
    FocusNavigationProfile,
    | 'activeThreadId'
    | 'scopeReady'
    | 'scopeReceipt'
    | 'scopeReceiptIsCurrent'
    | 'confirmUnconfirmedThread'
    | 'captureNavigationStateFloor'
    | 'navigationStateFloorIsCurrent'
    | 'requireNavigationRepair'
    | 'clearToRepairDraft'
    | 'isDisposed'
  >;
  projection: Pick<
    FocusProjectionSync,
    | 'snapshot'
    | 'snapshotInvalidated'
    | 'refreshThreads'
    | 'refreshArchivedThreads'
    | 'refreshActiveThread'
    | 'settleDeletedThread'
    | 'installGoalResult'
    | 'invalidateWireProjection'
  >;
  connection: Readonly<Ref<string>>;
  activeThread: Readonly<Ref<FocusThreadSummary | null>>;
  canCompact: Readonly<Ref<boolean>>;
  attachmentsAreAvailable(): boolean;
  defaultDraftWorkspace(): string;
  promptMessage(key: FocusPromptMessageKey, values?: { reason: string }): string;
  reportError(error: unknown): void;
  reportFatalError(error: unknown): boolean;
  clearError(): void;
}

export interface FocusMutationActions {
  readonly starting: Readonly<Ref<boolean>>;
  readonly actionBusy: Readonly<Ref<boolean>>;
  readonly mutationBusyByThread: ComputedRef<Record<string, true>>;
  readonly unknownLifecycleMutations: ComputedRef<UnknownLifecycleMutation[]>;
  readonly unknownProcessLocalMutations: ComputedRef<UnknownProcessLocalMutation[]>;
  readonly unknownSubmissionDraft: Readonly<Ref<UnknownSubmissionDraft | null>>;
  readonly unknownSubmissionDrafts: ComputedRef<UnknownSubmissionDraft[]>;
  readonly running: ComputedRef<boolean>;
  readonly owner: ComputedRef<FocusThreadSummary['owner']>;
  readonly canSubmit: ComputedRef<boolean>;
  readonly unknownThreadCreateDraftExists: ComputedRef<boolean>;
  readonly canRetryUnknownSubmission: ComputedRef<boolean>;
  readonly canInterrupt: ComputedRef<boolean>;
  readonly pendingApprovalActions: ComputedRef<Record<string, true>>;
  readonly pendingQuestionActions: ComputedRef<Record<string, PendingQuestionAction>>;
  installInitialState(initialMeta: FocusMeta): void;
  reconcilePromptResultsForThread(threadId: string): Promise<void>;
  captureUnknownMutationSnapshot(threadId: string): UnknownMutationSnapshotReceipt;
  reconcileUnknownMutation(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean;
  settleUnknownMutationFromEvent(
    threadId: string,
    mutationId: string,
    operation: string,
    disposition: FocusMutationDisposition,
  ): void;
  renameThread(threadId: string, name: string): Promise<void>;
  compact(): Promise<void>;
  review(target: Record<string, unknown>): Promise<void>;
  createGoal(objective: string): Promise<void>;
  controlGoal(action: 'pause' | 'resume' | 'cancel'): Promise<void>;
  archiveThread(threadId: string): Promise<boolean>;
  unarchiveThread(threadId: string): Promise<boolean>;
  deleteThread(threadId: string, confirmation: string): Promise<boolean>;
  uploadAttachment(file: Blob, name?: string): Promise<UploadedAttachment | null>;
  submit(
    text: string,
    attachments?: PromptAttachment[],
    retainTextWithoutAttachments?: () => boolean,
  ): Promise<boolean>;
  discardUnknownSubmission(attemptKey?: string): Promise<void>;
  takeUnknownSubmissionForRetry(
    handoff: UnknownSubmissionHandoff,
    attemptKey?: string,
  ): Promise<UnknownSubmissionRetryResult | null>;
  verifyUnknownLifecycleMutation(threadId: string): Promise<void>;
  unlockUnknownLifecycleMutation(threadId: string): Promise<void>;
  discardUnknownProcessLocalMutation(threadId: string): Promise<void>;
  interrupt(): Promise<void>;
  respondApproval(
    actionToken: string,
    response: ApprovalResponse & { actionId?: string },
  ): Promise<void>;
  respondQuestion(actionToken: string, response: QuestionResponse): Promise<void>;
  dismissQuestion(actionToken: string): Promise<void>;
  dispose(): void;
}

function exactNonEmptyString(value: unknown): string {
  return typeof value === 'string' && value !== '' && value === value.trim()
    ? value
    : '';
}

function decodeFirstPromptUnknownThreadId(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
  const details = value as Record<string, unknown>;
  const keys = Object.keys(details).sort();
  if (keys.length !== 2 || keys[0] !== 'operation' || keys[1] !== 'thread_id'
    || details.operation !== 'prompt') return '';
  return exactNonEmptyString(details.thread_id);
}

export function isLocalPossiblySentDraft(
  draft: UnknownSubmissionDraft | null | undefined,
): draft is UnknownSubmissionDraft {
  return !!draft
    && draft.attemptKind === 'thread_create_first_prompt'
    && draft.operation === THREAD_CREATE_FIRST_PROMPT_OPERATION
    && draft.recoveryPhase === 'possibly_sent';
}

export function createFocusMutationActions(
  options: FocusMutationActionsOptions,
): FocusMutationActions {
  const {
    api,
    navigation,
    projection,
  } = options;
  const starting = ref(false);
  const actionBusy = ref(false);
  const unknownSubmissionDraftStore = createUnknownSubmissionDraftStore();
  const promptResultLocators = createWebPromptResultLocatorStore();
  let unknownSubmissionActionKey: string | null = null;
  let disposed = false;
  const {
    threadMutations,
    captureUnknownMutationSnapshot,
    reconcileUnknownMutation,
    rememberProcessLocalUnknownFromError,
    archiveThread,
    unarchiveThread,
    deleteThread,
    verifyUnknownLifecycleMutation,
    unlockUnknownLifecycleMutation,
    discardUnknownProcessLocalMutation,
  } = createThreadMutationActions(options, { isDisposed, reportMutationSettlementError });
  const {
    pendingRequestActions,
    respondApproval,
    respondQuestion,
    dismissQuestion,
  } = createPendingRequestResponses(options, { isDisposed, reportErrorIfCurrent });

  const running = computed(() => (
    options.activeThread.value?.status === 'active' || !!projection.snapshot.value?.active_turn_id
  ));
  const owner = computed<FocusThreadSummary['owner']>(() => options.activeThread.value?.owner ?? {
    kind: 'none', holder_id: '', relation: 'none', label: 'No active writer',
  });
  const unknownSubmissionDrafts = unknownSubmissionDraftStore.drafts;
  const unknownSubmissionDraft = computed<UnknownSubmissionDraft | null>(() => {
    return unknownSubmissionDraftStore.forThread(navigation.activeThreadId.value);
  });
  const unknownThreadCreateDraftExists = computed(() => (
    unknownSubmissionDrafts.value.some((draft) => (
      draft.operation === THREAD_CREATE_FIRST_PROMPT_OPERATION
    ))
  ));
  const canSubmit = computed(() => (
    options.connection.value === 'connected'
    && navigation.scopeReady.value
    && !projection.snapshotInvalidated.value
    && (
      navigation.activeThreadId.value !== ''
      || !unknownThreadCreateDraftExists.value
    )
    && !threadMutations.isBusy(navigation.activeThreadId.value)
  ));
  const canRetryUnknownSubmission = computed(() => {
    const draft = unknownSubmissionDraft.value;
    const localPossiblySent = isLocalPossiblySentDraft(draft);
    return draft?.recoveryBlocked === false
      && (options.connection.value === 'connected' || localPossiblySent)
      && !!draft.threadId.trim()
      && localPossiblySent;
  });
  const canInterrupt = computed(() => (
    options.connection.value === 'connected'
    && navigation.scopeReady.value
    && !!navigation.activeThreadId.value
  ));

  function isDisposed(): boolean {
    return disposed || navigation.isDisposed;
  }

  function reportErrorIfCurrent(error: unknown, current = true): boolean {
    if (isDisposed()) return false;
    if (options.reportFatalError(error)) return true;
    if (!current) return false;
    options.reportError(error);
    return true;
  }

  function reportMutationSettlementError(error: unknown, current: boolean): void {
    if (error instanceof FocusApiError && error.code === 'mutation_replaced') {
      projection.invalidateWireProjection();
    }
    reportErrorIfCurrent(error, current);
  }

  function beginUnknownSubmissionAction(attemptKey: string): string | null {
    if (unknownSubmissionActionKey !== null
      || !unknownSubmissionDraftStore.has(attemptKey)) return null;
    unknownSubmissionActionKey = attemptKey;
    return attemptKey;
  }

  function unknownSubmissionActionIsCurrent(attemptKey: string): boolean {
    return unknownSubmissionActionKey === attemptKey
      && unknownSubmissionDraftStore.has(attemptKey);
  }

  function finishUnknownSubmissionAction(attemptKey: string): void {
    if (unknownSubmissionActionKey === attemptKey) {
      unknownSubmissionActionKey = null;
    }
  }

  function clearUnknownSubmissionDraftIfCurrent(
    attemptKey: string,
  ): boolean {
    if (!unknownSubmissionDraftStore.has(attemptKey)) return true;
    if (!unknownSubmissionActionIsCurrent(attemptKey)) return false;
    return removeUnknownSubmissionDraft(attemptKey);
  }

  function reportRecoveryDraftRemovalFailure(): void {
    options.reportError(new Error(
      'Focus could not durably remove the browser recovery record.',
    ));
  }

  function reportFirstPromptPersistenceFailure(threadId: string): void {
    options.reportError(new Error(
      `Focus could not durably record the possibly-sent first prompt for created thread ${threadId}. `
      + 'The original draft remains in the Composer; inspect that thread before submitting again.',
    ));
  }

  function saveUnknownSubmissionDraft(
    draft: UnknownSubmissionDraft,
    requireDurable = false,
  ): boolean {
    return !isDisposed() && unknownSubmissionDraftStore.save(draft, requireDurable);
  }

  function removeUnknownSubmissionDraft(attemptKey: string): boolean {
    return !isDisposed() && unknownSubmissionDraftStore.remove(attemptKey);
  }

  function installInitialState(initialMeta: FocusMeta): void {
    if (isDisposed()) return;
    unknownSubmissionDraftStore.install(api.clientId);
    threadMutations.installUnknownFromMeta(initialMeta);
  }

  function settleUnknownMutationFromEvent(
    threadId: string,
    mutationId: string,
    operation: string,
    disposition: FocusMutationDisposition,
  ): void {
    if (isDisposed()) return;
    threadMutations.settleUnknownFromEvent(
      threadId,
      mutationId,
      operation,
      disposition,
    );
  }

  async function renameThread(threadId: string, name: string): Promise<void> {
    const normalizedName = name.trim();
    if (isDisposed() || options.connection.value !== 'connected' || !normalizedName || actionBusy.value) return;
    const navigationFloor = navigation.captureNavigationStateFloor();
    actionBusy.value = true;
    options.clearError();
    try {
      const result = await api.renameThread(threadId, normalizedName);
      if (isDisposed()) return;
      if (!result.accepted) throw new Error('Focus rejected the thread rename.');
      await projection.refreshThreads();
      if (isDisposed()) return;
      if (navigation.activeThreadId.value === threadId
        && navigation.navigationStateFloorIsCurrent(navigationFloor)) {
        await projection.refreshActiveThread();
      }
    } catch (error) {
      if (isDisposed()) return;
      rememberProcessLocalUnknownFromError(error, threadId, 'rename');
      reportErrorIfCurrent(error, navigation.navigationStateFloorIsCurrent(navigationFloor));
      throw error;
    } finally {
      if (!isDisposed()) actionBusy.value = false;
    }
  }

  async function compact(): Promise<void> {
    if (isDisposed() || !navigation.scopeReady.value || !navigation.activeThreadId.value
      || !options.canCompact.value || actionBusy.value) return;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt || !navigation.scopeReceiptIsCurrent(receipt)) return;
    actionBusy.value = true;
    options.clearError();
    try {
      await api.compactThread(receipt.selectedThreadId);
      if (isDisposed() || !navigation.scopeReceiptIsCurrent(receipt)) return;
      await projection.refreshActiveThread();
    } catch (error) {
      if (isDisposed()) return;
      reportErrorIfCurrent(error, navigation.scopeReceiptIsCurrent(receipt));
      throw error;
    } finally {
      if (!isDisposed()) actionBusy.value = false;
    }
  }

  async function review(target: Record<string, unknown>): Promise<void> {
    if (isDisposed() || options.connection.value !== 'connected'
      || !navigation.scopeReady.value || actionBusy.value) return;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt?.selectedThreadId || !navigation.scopeReceiptIsCurrent(receipt)) return;
    actionBusy.value = true;
    options.clearError();
    try {
      await api.startReview(receipt.selectedThreadId, target);
      if (isDisposed() || !navigation.scopeReceiptIsCurrent(receipt)) return;
      await projection.refreshActiveThread();
    } catch (error) {
      if (isDisposed()) return;
      reportErrorIfCurrent(error, navigation.scopeReceiptIsCurrent(receipt));
      throw error;
    } finally {
      if (!isDisposed()) actionBusy.value = false;
    }
  }

  async function createGoal(objective: string): Promise<void> {
    const normalizedObjective = objective.trim();
    if (isDisposed() || options.connection.value !== 'connected'
      || !navigation.scopeReady.value || !normalizedObjective || actionBusy.value) return;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt?.selectedThreadId || !navigation.scopeReceiptIsCurrent(receipt)) return;
    const intent = options.intentClock.beginIntent();
    actionBusy.value = true;
    options.clearError();
    try {
      const result = await api.setGoal(receipt.selectedThreadId, {
        objective: normalizedObjective,
      }, intent);
      if (isDisposed() || !options.intentClock.intentIsCurrent(intent)) return;
      projection.installGoalResult(receipt.selectedThreadId, result);
    } catch (error) {
      if (isDisposed()) return;
      reportErrorIfCurrent(error, options.intentClock.intentIsCurrent(intent));
      throw error;
    } finally {
      if (!isDisposed()) actionBusy.value = false;
    }
  }

  async function controlGoal(action: 'pause' | 'resume' | 'cancel'): Promise<void> {
    if (isDisposed() || options.connection.value !== 'connected'
      || !navigation.scopeReady.value || actionBusy.value) return;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt?.selectedThreadId || !navigation.scopeReceiptIsCurrent(receipt)) return;
    const intent = options.intentClock.beginIntent();
    actionBusy.value = true;
    options.clearError();
    try {
      const result = action === 'cancel'
        ? await api.clearGoal(receipt.selectedThreadId, intent)
        : await api.setGoal(receipt.selectedThreadId, {
            status: action === 'pause' ? 'paused' : 'active',
          }, intent);
      if (isDisposed() || !options.intentClock.intentIsCurrent(intent)) return;
      projection.installGoalResult(receipt.selectedThreadId, result);
    } catch (error) {
      reportErrorIfCurrent(error, options.intentClock.intentIsCurrent(intent));
    } finally {
      if (!isDisposed()) actionBusy.value = false;
    }
  }

  async function uploadAttachment(file: Blob, name?: string): Promise<UploadedAttachment | null> {
    if (isDisposed() || !options.attachmentsAreAvailable()) return null;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt || !navigation.scopeReceiptIsCurrent(receipt)) return null;
    try {
      const result = await api.uploadAttachment(file, {
        name,
        threadId: receipt.selectedThreadId || undefined,
        cwd: receipt.selectedThreadId ? undefined : receipt.workingDir,
        scopeGeneration: receipt.scopeGeneration,
      });
      if (isDisposed() || !navigation.scopeReceiptIsCurrent(receipt)) return null;
      return {
        fileId: result.file_id,
        name: result.name,
        mediaType: result.media_type,
        previewUrl: result.url
          ? `/api/attachments/${encodeURIComponent(result.file_id)}`
          : '',
        localImagePreviewAllowed: false,
      };
    } catch (error) {
      reportErrorIfCurrent(error, navigation.scopeReceiptIsCurrent(receipt));
      return null;
    }
  }

  function promptResultIsExact(
    result: FocusPromptResultReceipt,
    locator: WebPromptResultLocator,
  ): boolean {
    return result.thread_id === locator.threadId
      && result.mutation_id === locator.mutationId
      && isWebPromptClientUserMessageId(
        result.client_user_message_id,
        locator.mutationId,
      );
  }

  function settlePromptResultLocator(
    result: FocusPromptResultReceipt,
    locator: WebPromptResultLocator,
  ): boolean {
    if (!promptResultIsExact(result, locator)) {
      projection.invalidateWireProjection();
      return false;
    }
    if (result.status === 'succeeded' || result.status === 'known_no_effect') {
      promptResultLocators.forget(locator);
    }
    return true;
  }

  function reportPromptMessage(
    key: FocusPromptMessageKey,
    reasonCode = '',
  ): void {
    if (isDisposed()) return;
    options.reportError(new Error(options.promptMessage(key, {
      reason: reasonCode ? ` (${reasonCode})` : '',
    })));
  }

  async function reconcilePromptResultsForThread(
    threadId: string,
    presentLookupFailure = true,
  ): Promise<void> {
    const normalizedThreadId = threadId.trim();
    if (!normalizedThreadId || isDisposed()) return;
    const locators = promptResultLocators.list().filter((locator) => (
      locator.threadId === normalizedThreadId
    ));
    await Promise.all(locators.map(async (locator) => {
      try {
        const result = await api.readPromptResult(locator.threadId, locator.mutationId);
        if (isDisposed()) return;
        if (!settlePromptResultLocator(result, locator)) {
          if (presentLookupFailure) reportPromptMessage('promptResultLookupFailed');
          return;
        }
        if (result.status === 'known_no_effect') {
          reportPromptMessage(
            'promptKnownNoEffectAfterReload',
            result.reason_code,
          );
        } else if (result.status === 'outcome_unknown') {
          reportPromptMessage('promptOutcomeUnknown', result.reason_code);
        } else if (result.status === 'pending') {
          reportPromptMessage('promptPending', result.reason_code);
        }
      } catch (error) {
        if (error instanceof FocusApiError
          && error.effectEvidence === 'pre_effect'
          && error.code === 'prompt_result_unavailable') {
          promptResultLocators.forget(locator);
          reportPromptMessage('promptResultUnavailable');
          return;
        }
        if (options.reportFatalError(error)) return;
        // A reload is lookup-only. A missing/temporarily unavailable receipt
        // never authorizes replay and can be queried again by a later reload.
        if (presentLookupFailure) reportPromptMessage('promptResultLookupFailed');
      }
    }));
  }

  function convergePromptProjection(
    receipt: ConfirmedWriterScopeReceipt,
    intent: number,
  ): void {
    void (async () => {
      try {
        await projection.refreshThreads();
        if (isDisposed() || !options.intentClock.intentIsCurrent(intent)) return;
        if (navigation.scopeReceiptIsCurrent(receipt)) {
          await projection.refreshActiveThread();
        }
      } catch (error) {
        reportErrorIfCurrent(error, options.intentClock.intentIsCurrent(intent));
      }
    })();
  }

  function reportPromptReceipt(result: FocusPromptResultReceipt): void {
    if (result.status === 'known_no_effect') {
      reportPromptMessage('promptKnownNoEffect', result.reason_code);
    } else if (result.status === 'outcome_unknown') {
      reportPromptMessage('promptOutcomeUnknown', result.reason_code);
    } else if (result.status === 'pending') {
      reportPromptMessage('promptPending', result.reason_code);
    }
  }

  function settlePromptComposer(
    result: FocusPromptResultReceipt,
    locator: WebPromptResultLocator,
    attachments: readonly PromptAttachment[],
    retainTextWithoutAttachments?: () => boolean,
  ): boolean {
    const exact = settlePromptResultLocator(result, locator);
    if (!exact) {
      // A mismatched success response cannot prove no effect. Treat the exact
      // payload as possibly sent so it cannot be replayed with one click.
      reportPromptMessage('promptOutcomeUnknown', 'invalid_receipt_identity');
      return true;
    }
    if (result.status !== 'known_no_effect') return true;
    if (result.reason_code === 'attachment_rollback_failed'
      && attachments.length > 0) {
      reportPromptMessage('promptAttachmentRollbackFailed');
      // The exact Composer owner can keep the text while retiring unsafe old
      // chips. If it has already been replaced, commit is the fail-closed
      // fallback; ComposerSubmission itself will never clear newer input.
      return retainTextWithoutAttachments?.() !== true;
    }
    reportPromptReceipt(result);
    return false;
  }

  async function submit(
    text: string,
    attachments: PromptAttachment[] = [],
    retainTextWithoutAttachments?: () => boolean,
  ): Promise<boolean> {
    const prompt = text.trim();
    const attachmentIds = attachments.map((attachment) => attachment.fileId).filter(Boolean);
    if (isDisposed() || (!prompt && attachmentIds.length === 0)
      || starting.value || !canSubmit.value) return false;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt || !navigation.scopeReceiptIsCurrent(receipt)) return false;
    const submissionThreadId = receipt.selectedThreadId;
    const submissionCwd = receipt.workingDir;
    const mutationId = submissionThreadId ? createWebPromptMutationId() : null;
    const locator = mutationId
      ? { threadId: submissionThreadId, mutationId }
      : null;
    if (submissionThreadId && !locator) {
      reportPromptMessage('promptIdentityUnavailable');
      return false;
    }
    if (locator) promptResultLocators.remember(locator);

    const intent = options.intentClock.beginIntent();
    starting.value = true;
    options.clearError();
    try {
      if (locator) {
        let result: FocusPromptResultReceipt;
        try {
          result = await api.submitPrompt(submissionThreadId, {
            text: prompt,
            attachmentIds,
            mutationId: locator.mutationId,
            sourceScopeGeneration: receipt.scopeGeneration,
            sourceAttachmentScope: receipt.attachmentScope,
            sourceComposerScopeId: receipt.composerScopeId,
          });
        } catch (error) {
          const knownNoEffect = error instanceof FocusApiError
            && error.effectEvidence === 'pre_effect';
          if (knownNoEffect) {
            promptResultLocators.forget(locator);
          } else {
            void reconcilePromptResultsForThread(submissionThreadId, false);
          }
          convergePromptProjection(receipt, intent);
          if (!options.reportFatalError(error)
            && options.intentClock.intentIsCurrent(intent)) {
            if (knownNoEffect) {
              reportPromptMessage(
                'promptKnownNoEffect',
                error instanceof FocusApiError ? error.code : '',
              );
            } else {
              reportPromptMessage(
                'promptOutcomeUnknown',
                error instanceof FocusApiError ? error.code : '',
              );
            }
          }
          return !knownNoEffect;
        }
        const commitPayload = settlePromptComposer(
          result,
          locator,
          attachments,
          retainTextWithoutAttachments,
        );
        if (result.status === 'succeeded') options.clearError();
        else if (result.status !== 'known_no_effect'
          && promptResultIsExact(result, locator)) reportPromptReceipt(result);
        convergePromptProjection(receipt, intent);
        return commitPayload;
      }

      if (!submissionThreadId) {
        const result = await api.startThread({
          text: prompt,
          cwd: submissionCwd,
          attachmentIds,
          intentGeneration: intent,
        });
        if (isDisposed() || !options.intentClock.intentIsCurrent(intent)) return true;
        // The create response is the authority for the new target.  Confirm
        // it directly before asking the directory projection to converge;
        // a stale/lagging list must not turn a successful create into a draft.
        await navigation.confirmUnconfirmedThread(result.thread_id).catch((error) => {
          reportErrorIfCurrent(error);
        });
        if (!isDisposed()) {
          void projection.refreshThreads().catch((error) => {
            reportErrorIfCurrent(error);
          });
        }
        return true;
      }
      return false;
    } catch (error) {
      if (isDisposed()) return true;
      reportErrorIfCurrent(error, options.intentClock.intentIsCurrent(intent));
      if (!submissionThreadId
        && error instanceof FocusApiError
        && error.status === 503
        && error.code === 'turn_submission_unknown') {
        const createdThreadId = decodeFirstPromptUnknownThreadId(error.details);
        if (createdThreadId) {
          const localDraft = createFirstPromptPossiblySentDraft({
            clientId: api.clientId,
            text: prompt,
            threadId: createdThreadId,
            cwd: submissionCwd,
            hadAttachments: attachments.length > 0,
          });
          if (!saveUnknownSubmissionDraft(localDraft, true)) {
            reportFirstPromptPersistenceFailure(createdThreadId);
            return false;
          }
          if (options.intentClock.intentIsCurrent(intent)) {
            await projection.refreshThreads().catch(() => false);
            if (!isDisposed() && options.intentClock.intentIsCurrent(intent)) {
              await navigation.confirmUnconfirmedThread(createdThreadId).catch(() => false);
            }
          }
          return true;
        }
      }
      if (error instanceof FocusApiError && error.code === 'thread_created_turn_not_started') {
        const createdThreadId = String(error.details.thread_id ?? '').trim();
        if (createdThreadId && options.intentClock.intentIsCurrent(intent)) {
          await projection.refreshThreads().catch((refreshError) => reportErrorIfCurrent(refreshError));
          if (!isDisposed() && options.intentClock.intentIsCurrent(intent)) {
            await navigation.confirmUnconfirmedThread(createdThreadId);
          }
        }
        return false;
      }
      if (!isDisposed()) throw error;
      return true;
    } finally {
      if (!isDisposed()) starting.value = false;
    }
  }

  async function discardUnknownSubmission(attemptKey = ''): Promise<void> {
    const draft = attemptKey
      ? unknownSubmissionDraftStore.get(attemptKey)
      : unknownSubmissionDraft.value;
    if (isDisposed() || !draft) return;
    const actionKey = beginUnknownSubmissionAction(draft.attemptKey);
    if (actionKey === null) return;
    try {
      if (!clearUnknownSubmissionDraftIfCurrent(actionKey)) {
        reportRecoveryDraftRemovalFailure();
      } else options.clearError();
    } finally {
      if (!isDisposed()) finishUnknownSubmissionAction(actionKey);
    }
  }

  async function takeUnknownSubmissionForRetry(
    handoff: UnknownSubmissionHandoff,
    attemptKey = '',
  ): Promise<UnknownSubmissionRetryResult | null> {
    const draft = attemptKey
      ? unknownSubmissionDraftStore.get(attemptKey)
      : unknownSubmissionDraft.value;
    if (isDisposed() || !isLocalPossiblySentDraft(draft)
      || !draft.threadId.trim()) return null;
    const actionKey = beginUnknownSubmissionAction(draft.attemptKey);
    if (actionKey === null) return null;
    try {
      if (navigation.activeThreadId.value !== draft.threadId
        && (options.connection.value !== 'connected'
          || !await navigation.confirmUnconfirmedThread(draft.threadId)
          || isDisposed())) return null;
      if (!unknownSubmissionActionIsCurrent(actionKey)) return null;
      const receipt = navigation.scopeReceipt.value;
      if (!receipt || receipt.selectedThreadId !== draft.threadId
        || !navigation.scopeReceiptIsCurrent(receipt)) return null;
      const commit = await handoff(draft, receipt.composerScopeId);
      if (isDisposed() || !commit
        || !unknownSubmissionActionIsCurrent(actionKey)
        || !navigation.scopeReceiptIsCurrent(receipt)
        || !commit()) return null;
      const removed = clearUnknownSubmissionDraftIfCurrent(actionKey);
      if (!removed) reportRecoveryDraftRemovalFailure();
      else options.clearError();
      if (options.connection.value === 'connected') {
        void projection.refreshActiveThread().catch((error) => {
          if (removed) reportMutationSettlementError(error, true);
        });
      }
      return {
        ...draft,
        handoffReason: 'possibly_sent',
        hadAttachments: draft.handoffHadAttachments,
        recoveryRecordState: removed ? 'removed' : 'locked_removal_failed',
      };
    } catch (error) {
      reportMutationSettlementError(error, unknownSubmissionActionIsCurrent(actionKey));
      return null;
    } finally {
      if (!isDisposed()) finishUnknownSubmissionAction(actionKey);
    }
  }

  async function interrupt(): Promise<void> {
    if (isDisposed() || !canInterrupt.value) return;
    const receipt = navigation.scopeReceipt.value;
    if (!receipt?.selectedThreadId || !navigation.scopeReceiptIsCurrent(receipt)) return;
    const activeTurnId = projection.snapshotInvalidated.value
      ? ''
      : projection.snapshot.value?.active_turn_id ?? '';
    options.clearError();
    try {
      await api.interrupt(receipt.selectedThreadId, activeTurnId);
      if (isDisposed() || !navigation.scopeReceiptIsCurrent(receipt)) return;
      await projection.refreshActiveThread();
    } catch (error) {
      reportErrorIfCurrent(error, navigation.scopeReceiptIsCurrent(receipt));
    }
  }

  function dispose(): void {
    if (disposed) return;
    disposed = true;
    unknownSubmissionActionKey = null;
    starting.value = false;
    actionBusy.value = false;
    pendingRequestActions.clear();
  }

  return {
    starting,
    actionBusy,
    mutationBusyByThread: threadMutations.busyByThread,
    unknownLifecycleMutations: threadMutations.unknownLifecycleMutations,
    unknownProcessLocalMutations: threadMutations.unknownProcessLocalMutations,
    unknownSubmissionDraft,
    unknownSubmissionDrafts,
    running,
    owner,
    canSubmit,
    unknownThreadCreateDraftExists,
    canRetryUnknownSubmission,
    canInterrupt,
    pendingApprovalActions: pendingRequestActions.approvalActions,
    pendingQuestionActions: pendingRequestActions.questionActions,
    installInitialState,
    reconcilePromptResultsForThread,
    captureUnknownMutationSnapshot,
    reconcileUnknownMutation,
    settleUnknownMutationFromEvent,
    renameThread,
    compact,
    review,
    createGoal,
    controlGoal,
    archiveThread,
    unarchiveThread,
    deleteThread,
    uploadAttachment,
    submit,
    discardUnknownSubmission,
    takeUnknownSubmissionForRetry,
    verifyUnknownLifecycleMutation,
    unlockUnknownLifecycleMutation,
    discardUnknownProcessLocalMutation,
    interrupt,
    respondApproval,
    respondQuestion,
    dismissQuestion,
    dispose,
  };
}
