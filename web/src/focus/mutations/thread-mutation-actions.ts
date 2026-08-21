import type { Ref } from 'vue';
import type { FocusWebApiPort } from '../api';
import {
  createThreadMutationState,
  isUnknownLifecycleMutation,
  type ThreadMutationState,
  type UnknownMutationSnapshotReceipt,
} from '../client-state/thread-mutations';
import type { FocusNavigationProfile } from '../focusNavigationProfile';
import type { FocusProjectionSync } from '../focusProjectionSync';
import {
  FocusApiError,
  type FocusLifecycleOperation,
  type FocusLifecycleResult,
  type FocusMutationDisposition,
  type FocusMutationResult,
  type FocusThreadSnapshot,
} from '../types';

type ThreadMutationApiPort = Pick<
  FocusWebApiPort,
  | 'archiveThread'
  | 'unarchiveThread'
  | 'deleteThread'
  | 'resolveUnknownMutation'
  | 'verifyUnknownLifecycleMutation'
>;

interface ThreadMutationActionsOptions {
  api: ThreadMutationApiPort;
  navigation: Pick<
    FocusNavigationProfile,
    | 'activeThreadId'
    | 'captureNavigationStateFloor'
    | 'navigationStateFloorIsCurrent'
    | 'requireNavigationRepair'
    | 'clearToRepairDraft'
  >;
  projection: Pick<
    FocusProjectionSync,
    | 'snapshotInvalidated'
    | 'refreshThreads'
    | 'refreshArchivedThreads'
    | 'refreshActiveThread'
    | 'settleDeletedThread'
    | 'invalidateWireProjection'
  >;
  connection: Readonly<Ref<string>>;
  defaultDraftWorkspace(): string;
  reportError(error: unknown): void;
  clearError(): void;
}

interface ThreadMutationRuntimePort {
  isDisposed(): boolean;
  reportMutationSettlementError(error: unknown, current: boolean): void;
}

type ThreadMutationCompositionPort = Pick<
  ThreadMutationState,
  | 'busyByThread'
  | 'unknownLifecycleMutations'
  | 'unknownProcessLocalMutations'
  | 'isBusy'
  | 'getUnknown'
  | 'installUnknownFromMeta'
  | 'settleUnknownFromEvent'
>;

interface ThreadMutationActions {
  readonly threadMutations: ThreadMutationCompositionPort;
  captureUnknownMutationSnapshot(threadId: string): UnknownMutationSnapshotReceipt;
  reconcileUnknownMutation(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean;
  rememberProcessLocalUnknownFromError(
    error: unknown,
    threadId: string,
    operation: string,
  ): boolean;
  archiveThread(threadId: string): Promise<boolean>;
  unarchiveThread(threadId: string): Promise<boolean>;
  deleteThread(threadId: string, confirmation: string): Promise<boolean>;
  verifyUnknownLifecycleMutation(threadId: string): Promise<void>;
  unlockUnknownLifecycleMutation(threadId: string): Promise<void>;
  discardUnknownProcessLocalMutation(threadId: string): Promise<void>;
}

export function lifecycleMutationError(
  result: FocusLifecycleResult,
  action: string,
): Error | null {
  if (result.upstream_outcome === 'unknown') {
    return new Error(
      `${action} result is unknown. Do not retry automatically. ${result.outcome_detail ?? ''}`.trim(),
    );
  }
  if (result.upstream_outcome === 'error') {
    return new Error(result.upstream_error?.trim() || `${action} was rejected by Codex.`);
  }
  if (result.focus_cleanup === 'incomplete') {
    const detail = result.cleanup_errors.filter(Boolean).join('; ');
    return new Error(
      `${action} succeeded in Codex, but Focus cleanup is incomplete. ${detail}`.trim(),
    );
  }
  return null;
}

function unknownSettlementIsExact(
  result: FocusMutationResult,
  threadId: string,
  mutationId: string,
  disposition: FocusMutationDisposition,
): boolean {
  return result.accepted === true
    && result.thread_id === threadId
    && result.mutation_id === mutationId
    && result.disposition === disposition;
}

export function createThreadMutationActions(
  options: ThreadMutationActionsOptions,
  runtime: ThreadMutationRuntimePort,
): ThreadMutationActions {
  const {
    api,
    navigation,
    projection,
  } = options;
  const { isDisposed, reportMutationSettlementError } = runtime;
  const threadMutations = createThreadMutationState();

  function captureUnknownMutationSnapshot(threadId: string): UnknownMutationSnapshotReceipt {
    return threadMutations.captureSnapshot(threadId);
  }

  function reconcileUnknownMutation(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean {
    return !isDisposed() && threadMutations.reconcileUnknown(receipt, mutation);
  }

  function rememberLifecycleUnknownFromError(
    error: unknown,
    threadId: string,
    operation: FocusLifecycleOperation,
  ): void {
    if (!(error instanceof FocusApiError)) return;
    if (error.code === 'mutation_reconciling') {
      projection.invalidateWireProjection();
      return;
    }
    if (error.code !== 'mutation_unknown') return;
    const mutationId = String(error.details.mutation_id ?? '').trim();
    const targetThreadId = String(error.details.thread_id ?? '').trim();
    const targetOperation = String(error.details.operation ?? '').trim();
    if (!mutationId || targetThreadId !== threadId || targetOperation !== operation) {
      projection.invalidateWireProjection();
      return;
    }
    threadMutations.rememberUnknownIfUnsettled({
      threadId,
      mutationId,
      operation,
      durability: 'process_local',
      verification: null,
    });
  }

  function rememberProcessLocalUnknownFromError(
    error: unknown,
    threadId: string,
    operation: string,
  ): boolean {
    if (!(error instanceof FocusApiError)) return false;
    if (error.code === 'mutation_reconciling') {
      projection.invalidateWireProjection();
      return false;
    }
    if (error.code !== 'mutation_unknown') return false;
    const mutationId = String(error.details.mutation_id ?? '').trim();
    const targetThreadId = String(error.details.thread_id ?? '').trim();
    const targetOperation = String(error.details.operation ?? '').trim();
    const durability = String(error.details.durability ?? '').trim();
    if (!mutationId || targetThreadId !== threadId || targetOperation !== operation
      || durability !== 'process_local') {
      projection.invalidateWireProjection();
      return false;
    }
    return threadMutations.rememberUnknownIfUnsettled({
      threadId,
      mutationId,
      operation,
      durability: 'process_local',
      verification: null,
    });
  }

  async function archiveThread(threadId: string): Promise<boolean> {
    if (isDisposed() || options.connection.value !== 'connected'
      || projection.snapshotInvalidated.value
      || !threadId || threadMutations.isBusy(threadId)
      || threadMutations.getUnknown(threadId)) return false;
    const navigationFloor = navigation.captureNavigationStateFloor();
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return false;
    options.clearError();
    try {
      const result = await api.archiveThread(threadId);
      if (isDisposed()) return false;
      if (result.thread_id !== threadId) {
        throw new Error('Focus returned an archive result for another thread.');
      }
      if (result.upstream_outcome === 'unknown') {
        if (threadMutations.busyReceiptIsCurrent(busyReceipt)) {
          threadMutations.rememberUnknownIfUnsettled({
            threadId,
            mutationId: result.mutation_id ?? '',
            operation: 'archive',
            durability: 'process_local',
            verification: null,
          });
        }
      }
      if (result.upstream_outcome === 'success') {
        const targetStillMatches = navigation.activeThreadId.value === threadId;
        const targetIsExact = navigation.navigationStateFloorIsCurrent(navigationFloor);
        if (targetStillMatches && !targetIsExact) {
          navigation.requireNavigationRepair();
          projection.invalidateWireProjection();
        } else if (targetStillMatches) navigation.clearToRepairDraft();
        await projection.refreshThreads();
        if (isDisposed()) return false;
        await projection.refreshArchivedThreads();
      }
      const failure = lifecycleMutationError(result, 'Archive');
      if (failure) throw failure;
      return true;
    } catch (error) {
      if (isDisposed()) return false;
      rememberLifecycleUnknownFromError(error, threadId, 'archive');
      options.reportError(error);
      return false;
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  async function unarchiveThread(threadId: string): Promise<boolean> {
    if (isDisposed() || options.connection.value !== 'connected'
      || projection.snapshotInvalidated.value
      || !threadId || threadMutations.isBusy(threadId)
      || threadMutations.getUnknown(threadId)) return false;
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return false;
    options.clearError();
    try {
      const result = await api.unarchiveThread(threadId);
      if (isDisposed()) return false;
      if (result.thread_id !== threadId) {
        throw new Error('Focus returned an unarchive result for another thread.');
      }
      if (result.upstream_outcome === 'unknown') {
        if (threadMutations.busyReceiptIsCurrent(busyReceipt)) {
          threadMutations.rememberUnknownIfUnsettled({
            threadId,
            mutationId: result.mutation_id ?? '',
            operation: 'unarchive',
            durability: 'process_local',
            verification: null,
          });
        }
      }
      if (result.upstream_outcome === 'success') {
        await projection.refreshArchivedThreads();
        if (isDisposed()) return false;
        await projection.refreshThreads();
      }
      const failure = lifecycleMutationError(result, 'Unarchive');
      if (failure) throw failure;
      return true;
    } catch (error) {
      if (isDisposed()) return false;
      rememberLifecycleUnknownFromError(error, threadId, 'unarchive');
      options.reportError(error);
      return false;
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  async function deleteThread(threadId: string, confirmation: string): Promise<boolean> {
    if (isDisposed() || options.connection.value !== 'connected'
      || projection.snapshotInvalidated.value
      || !threadId || threadMutations.isBusy(threadId)
      || threadMutations.getUnknown(threadId)) return false;
    const navigationFloor = navigation.captureNavigationStateFloor();
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return false;
    options.clearError();
    try {
      const result = await api.deleteThread(threadId, confirmation);
      if (isDisposed()) return false;
      if (result.thread_id !== threadId) {
        throw new Error('Focus returned a delete result for another thread.');
      }
      if (result.upstream_outcome === 'unknown') {
        if (threadMutations.busyReceiptIsCurrent(busyReceipt)) {
          threadMutations.rememberUnknownIfUnsettled({
            threadId,
            mutationId: result.mutation_id ?? '',
            operation: 'delete',
            durability: 'process_local',
            verification: null,
          });
        }
      }
      if (result.upstream_outcome === 'success') {
        const targetStillMatches = navigation.activeThreadId.value === threadId;
        const targetIsExact = navigation.navigationStateFloorIsCurrent(navigationFloor);
        if (targetStillMatches && !targetIsExact) {
          navigation.requireNavigationRepair();
          projection.invalidateWireProjection();
        } else {
          projection.settleDeletedThread(threadId, targetStillMatches);
        }
        if (targetStillMatches && targetIsExact) {
          navigation.clearToRepairDraft(options.defaultDraftWorkspace());
        }
      }
      const failure = lifecycleMutationError(result, 'Delete');
      if (failure) throw failure;
      return true;
    } catch (error) {
      if (isDisposed()) return false;
      rememberLifecycleUnknownFromError(error, threadId, 'delete');
      options.reportError(error);
      return false;
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  async function verifyUnknownLifecycleMutation(threadId: string): Promise<void> {
    const receipt = threadMutations.captureUnknown(threadId);
    if (isDisposed() || options.connection.value !== 'connected' || !receipt
      || !isUnknownLifecycleMutation(receipt.mutation)) return;
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return;
    const { mutation } = receipt;
    try {
      const result = await api.verifyUnknownLifecycleMutation(
        mutation.threadId,
        mutation.mutationId,
      );
      if (isDisposed() || !threadMutations.unknownReceiptIsCurrent(receipt)) return;
      if (result.thread_id !== mutation.threadId
        || result.mutation_id !== mutation.mutationId) {
        projection.invalidateWireProjection();
        return;
      }
      if (!result.verification) {
        threadMutations.forgetUnknownIfCurrent(receipt);
        return;
      }
      if (result.operation !== mutation.operation) {
        projection.invalidateWireProjection();
        return;
      }
      if (!threadMutations.replaceUnknownIfCurrent(
        receipt,
        { ...mutation, verification: result.verification },
      )) return;
      options.clearError();
      await Promise.all([projection.refreshThreads(), projection.refreshArchivedThreads()]);
      if (isDisposed()) return;
      if (navigation.activeThreadId.value === mutation.threadId) {
        await projection.refreshActiveThread();
      }
    } catch (error) {
      reportMutationSettlementError(
        error,
        threadMutations.unknownReceiptIsCurrent(receipt),
      );
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  async function unlockUnknownLifecycleMutation(threadId: string): Promise<void> {
    const receipt = threadMutations.captureUnknown(threadId);
    const mutation = receipt?.mutation;
    if (isDisposed() || options.connection.value !== 'connected'
      || !receipt || !mutation || !isUnknownLifecycleMutation(mutation)
      || !mutation.verification) return;
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return;
    try {
      const result = await api.resolveUnknownMutation(
        mutation.threadId,
        'discard',
        mutation.mutationId,
      );
      if (isDisposed() || !threadMutations.unknownReceiptIsCurrent(receipt)) return;
      if (!unknownSettlementIsExact(
        result,
        mutation.threadId,
        mutation.mutationId,
        'user_discard',
      )) {
        projection.invalidateWireProjection();
        return;
      }
      if (!threadMutations.forgetUnknownIfCurrent(receipt)) return;
      options.clearError();
      await Promise.all([projection.refreshThreads(), projection.refreshArchivedThreads()]);
      if (isDisposed()) return;
      if (navigation.activeThreadId.value === mutation.threadId) {
        await projection.refreshActiveThread();
      }
    } catch (error) {
      reportMutationSettlementError(
        error,
        threadMutations.unknownReceiptIsCurrent(receipt),
      );
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  async function discardUnknownProcessLocalMutation(threadId: string): Promise<void> {
    const receipt = threadMutations.captureUnknown(threadId);
    const mutation = receipt?.mutation;
    if (isDisposed() || options.connection.value !== 'connected'
      || !receipt || !mutation || isUnknownLifecycleMutation(mutation)) return;
    const busyReceipt = threadMutations.beginBusy(threadId);
    if (!busyReceipt) return;
    try {
      const result = await api.resolveUnknownMutation(
        mutation.threadId,
        'discard',
        mutation.mutationId,
      );
      if (isDisposed() || !threadMutations.unknownReceiptIsCurrent(receipt)) return;
      if (!unknownSettlementIsExact(
        result,
        mutation.threadId,
        mutation.mutationId,
        'user_discard',
      )) {
        projection.invalidateWireProjection();
        return;
      }
      if (!threadMutations.forgetUnknownIfCurrent(receipt)) return;
      options.clearError();
      await projection.refreshThreads();
      if (isDisposed()) return;
      if (navigation.activeThreadId.value === mutation.threadId) {
        await projection.refreshActiveThread();
      }
    } catch (error) {
      reportMutationSettlementError(
        error,
        threadMutations.unknownReceiptIsCurrent(receipt),
      );
    } finally {
      if (!isDisposed()) threadMutations.finishBusy(busyReceipt);
    }
  }

  return {
    threadMutations: {
      busyByThread: threadMutations.busyByThread,
      unknownLifecycleMutations: threadMutations.unknownLifecycleMutations,
      unknownProcessLocalMutations: threadMutations.unknownProcessLocalMutations,
      isBusy: threadMutations.isBusy,
      getUnknown: threadMutations.getUnknown,
      installUnknownFromMeta: threadMutations.installUnknownFromMeta,
      settleUnknownFromEvent: threadMutations.settleUnknownFromEvent,
    },
    captureUnknownMutationSnapshot,
    reconcileUnknownMutation,
    rememberProcessLocalUnknownFromError,
    archiveThread,
    unarchiveThread,
    deleteThread,
    verifyUnknownLifecycleMutation,
    unlockUnknownLifecycleMutation,
    discardUnknownProcessLocalMutation,
  };
}

export { unknownSettlementIsExact };
