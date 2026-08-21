import { computed, ref } from 'vue';
import type { ComputedRef } from 'vue';
import type {
  FocusLifecycleOperation,
  FocusLifecycleVerification,
  FocusMeta,
  FocusMutationDisposition,
  FocusThreadSnapshot,
  FocusUnknownMutationDurability,
} from '../types';
import { isFocusWebWireEnum } from '../focusWire.generated';

export interface UnknownThreadMutation {
  threadId: string;
  mutationId: string;
  operation: string;
  durability: FocusUnknownMutationDurability;
  verification: FocusLifecycleVerification | null;
}
interface UnknownLifecycleMutationRecord extends UnknownThreadMutation {
  operation: FocusLifecycleOperation;
  durability: 'process_local';
}

export interface UnknownLifecycleMutation {
  threadId: string;
  mutationId: string;
  operation: FocusLifecycleOperation;
  verification: FocusLifecycleVerification | null;
}

export interface UnknownProcessLocalMutation extends UnknownThreadMutation {
  durability: 'process_local';
  verification: null;
}

interface ThreadMutationRecord {
  busy: boolean;
  unknown: UnknownThreadMutation | null;
}

interface TerminalMutationSettlement {
  mutationId: string;
  operation: string;
  disposition: FocusMutationDisposition;
}

export interface UnknownMutationReceipt {
  readonly threadId: string;
  readonly generation: number;
  readonly mutation: UnknownThreadMutation;
}

/** Authority captured before an HTTP thread snapshot is read. */
export interface UnknownMutationSnapshotReceipt {
  readonly threadId: string;
  readonly generation: number;
}

export interface ThreadMutationBusyReceipt {
  readonly threadId: string;
  readonly generation: number;
}

export interface ThreadMutationState {
  readonly busyByThread: ComputedRef<Record<string, true>>;
  readonly unknownMutations: ComputedRef<UnknownThreadMutation[]>;
  readonly unknownLifecycleMutations: ComputedRef<UnknownLifecycleMutation[]>;
  readonly unknownProcessLocalMutations: ComputedRef<UnknownProcessLocalMutation[]>;
  isBusy(threadId: string): boolean;
  beginBusy(threadId: string): ThreadMutationBusyReceipt | null;
  busyReceiptIsCurrent(receipt: ThreadMutationBusyReceipt): boolean;
  finishBusy(receipt: ThreadMutationBusyReceipt): boolean;
  getUnknown(threadId: string): UnknownThreadMutation | null;
  captureSnapshot(threadId: string): UnknownMutationSnapshotReceipt;
  captureUnknown(threadId: string): UnknownMutationReceipt | null;
  unknownReceiptIsCurrent(receipt: UnknownMutationReceipt): boolean;
  rememberUnknownIfUnsettled(mutation: UnknownThreadMutation): boolean;
  settleUnknownFromEvent(
    threadId: string,
    mutationId: string,
    operation: string,
    disposition: FocusMutationDisposition,
  ): boolean;
  settlementDisposition(
    threadId: string,
    mutationId: string,
    operation: string,
  ): FocusMutationDisposition | null;
  replaceUnknownIfCurrent(
    receipt: UnknownMutationReceipt,
    mutation: UnknownThreadMutation,
  ): boolean;
  forgetUnknownIfCurrent(receipt: UnknownMutationReceipt): boolean;
  reconcileUnknown(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean;
  installUnknownFromMeta(initialMeta: FocusMeta): void;
}

function lifecycleVerification(value: unknown): FocusLifecycleVerification | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Partial<FocusLifecycleVerification>;
  const state = candidate.state;
  const verificationId = typeof candidate.verification_id === 'string'
    ? candidate.verification_id.trim()
    : '';
  if (!isFocusWebWireEnum('lifecycle_target_state', state) || !verificationId) return null;
  return {
    state,
    verification_id: verificationId,
  };
}

function isLifecycleOperation(value: string): value is FocusLifecycleOperation {
  return isFocusWebWireEnum('lifecycle_operation', value);
}

export function isUnknownLifecycleMutation(
  mutation: UnknownThreadMutation,
): mutation is UnknownLifecycleMutationRecord {
  return isLifecycleOperation(mutation.operation);
}

export function isUnknownProcessLocalMutation(
  mutation: UnknownThreadMutation,
): mutation is UnknownProcessLocalMutation {
  return mutation.durability === 'process_local'
    && !isLifecycleOperation(mutation.operation);
}

export function normalizeUnknownMutation(value: unknown): UnknownThreadMutation | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Partial<UnknownThreadMutation> & {
    thread_id?: unknown;
    mutation_id?: unknown;
    durability?: unknown;
    verification?: unknown;
  };
  const threadId = typeof candidate.threadId === 'string'
    ? candidate.threadId.trim()
    : typeof candidate.thread_id === 'string'
      ? candidate.thread_id.trim()
      : '';
  const mutationId = typeof candidate.mutationId === 'string'
    ? candidate.mutationId.trim()
    : typeof candidate.mutation_id === 'string'
      ? candidate.mutation_id.trim()
      : '';
  const operation = typeof candidate.operation === 'string' ? candidate.operation.trim() : '';
  if (!threadId || !mutationId || !operation || candidate.durability !== 'process_local') {
    return null;
  }
  return {
    threadId,
    mutationId,
    operation,
    durability: 'process_local',
    verification: isLifecycleOperation(operation)
      ? lifecycleVerification(candidate.verification)
      : null,
  };
}

export function normalizeUnknownLifecycleMutation(value: unknown): UnknownLifecycleMutation | null {
  const mutation = normalizeUnknownMutation(value);
  if (!mutation || !isUnknownLifecycleMutation(mutation)) return null;
  return {
    threadId: mutation.threadId,
    mutationId: mutation.mutationId,
    operation: mutation.operation,
    verification: mutation.verification,
  };
}

function cloneUnknownMutation(mutation: UnknownThreadMutation): UnknownThreadMutation {
  return {
    threadId: mutation.threadId,
    mutationId: mutation.mutationId,
    operation: mutation.operation,
    durability: mutation.durability,
    verification: mutation.verification ? { ...mutation.verification } : null,
  };
}

/**
 * One canonical per-thread record for mutation exclusion and recovery.
 *
 * `busy` and unknown-outcome evidence never survive this browser document.
 * Current server meta is the only bootstrap source. A record disappears when
 * both fields are empty, so independent maps cannot retain contradictory or
 * orphaned thread keys.
 */
export function createThreadMutationState(): ThreadMutationState {
  const byThread = ref<Record<string, ThreadMutationRecord>>({});
  const busyGenerations = new Map<string, number>();
  const unknownRecordGenerations = new Map<string, number>();
  const settledByThread = new Map<string, TerminalMutationSettlement>();

  const busyByThread = computed<Record<string, true>>(() => Object.fromEntries(
    Object.entries(byThread.value)
      .filter(([, record]) => record.busy)
      .map(([threadId]) => [threadId, true]),
  ));
  const unknownMutations = computed<UnknownThreadMutation[]>(() => (
    Object.values(byThread.value)
      .flatMap((record) => record.unknown ? [record.unknown] : [])
      .sort((left, right) => left.threadId.localeCompare(right.threadId))
  ));
  const unknownLifecycleMutations = computed<UnknownLifecycleMutation[]>(() => (
    unknownMutations.value
      .filter(isUnknownLifecycleMutation)
      .map((mutation) => ({
        threadId: mutation.threadId,
        mutationId: mutation.mutationId,
        operation: mutation.operation,
        verification: mutation.verification,
      }))
  ));
  const unknownProcessLocalMutations = computed<UnknownProcessLocalMutation[]>(() => (
    unknownMutations.value.filter(isUnknownProcessLocalMutation)
  ));

  function installRecord(threadId: string, record: ThreadMutationRecord): void {
    const normalizedThreadId = threadId.trim();
    if (!normalizedThreadId) return;
    const next = { ...byThread.value };
    if (!record.busy && !record.unknown) delete next[normalizedThreadId];
    else next[normalizedThreadId] = record;
    byThread.value = next;
  }

  function replaceUnknown(
    mutations: Iterable<UnknownThreadMutation | UnknownLifecycleMutation>,
  ): void {
    const normalizedMutations = [...mutations].flatMap((mutation) => {
      const normalized = normalizeUnknownMutation({
        ...mutation,
        durability: 'process_local',
      });
      return normalized ? [normalized] : [];
    });
    const replacedThreadIds = new Set([
      ...Object.keys(byThread.value),
      ...normalizedMutations.map((mutation) => mutation.threadId),
    ]);
    for (const threadId of replacedThreadIds) {
      unknownRecordGenerations.set(
        threadId,
        (unknownRecordGenerations.get(threadId) ?? 0) + 1,
      );
    }
    const next: Record<string, ThreadMutationRecord> = {};
    for (const [threadId, record] of Object.entries(byThread.value)) {
      if (record.busy) next[threadId] = { busy: true, unknown: null };
    }
    for (const normalized of normalizedMutations) {
      next[normalized.threadId] = {
        busy: next[normalized.threadId]?.busy ?? false,
        unknown: cloneUnknownMutation(normalized),
      };
    }
    byThread.value = next;
  }

  function isBusy(threadId: string): boolean {
    return byThread.value[threadId]?.busy === true;
  }

  function setBusy(threadId: string, busy: boolean): void {
    busyGenerations.set(threadId, (busyGenerations.get(threadId) ?? 0) + 1);
    const current = byThread.value[threadId] ?? { busy: false, unknown: null };
    installRecord(threadId, { ...current, busy });
  }

  function beginBusy(threadId: string): ThreadMutationBusyReceipt | null {
    if (isBusy(threadId)) return null;
    setBusy(threadId, true);
    return { threadId, generation: busyGenerations.get(threadId) ?? 0 };
  }

  function busyReceiptIsCurrent(receipt: ThreadMutationBusyReceipt): boolean {
    return isBusy(receipt.threadId)
      && (busyGenerations.get(receipt.threadId) ?? 0) === receipt.generation;
  }

  function finishBusy(receipt: ThreadMutationBusyReceipt): boolean {
    if (!busyReceiptIsCurrent(receipt)) return false;
    setBusy(receipt.threadId, false);
    return true;
  }

  function getUnknown(threadId: string): UnknownThreadMutation | null {
    return byThread.value[threadId]?.unknown ?? null;
  }

  function captureSnapshot(threadId: string): UnknownMutationSnapshotReceipt {
    return {
      threadId,
      generation: unknownRecordGenerations.get(threadId) ?? 0,
    };
  }

  function captureUnknown(threadId: string): UnknownMutationReceipt | null {
    const mutation = getUnknown(threadId);
    if (!mutation) return null;
    return {
      threadId,
      generation: unknownRecordGenerations.get(threadId) ?? 0,
      mutation: cloneUnknownMutation(mutation),
    };
  }

  function unknownReceiptIsCurrent(receipt: UnknownMutationReceipt): boolean {
    const current = getUnknown(receipt.threadId);
    return current?.mutationId === receipt.mutation.mutationId
      && current.operation === receipt.mutation.operation
      && current.durability === receipt.mutation.durability
      && (unknownRecordGenerations.get(receipt.threadId) ?? 0) === receipt.generation;
  }

  function rememberUnknown(mutation: UnknownThreadMutation): void {
    const normalized = normalizeUnknownMutation(mutation);
    if (!normalized) return;
    const settled = settledByThread.get(normalized.threadId);
    if (settled?.mutationId === normalized.mutationId
      && settled.operation === normalized.operation) return;
    settledByThread.delete(normalized.threadId);
    unknownRecordGenerations.set(
      normalized.threadId,
      (unknownRecordGenerations.get(normalized.threadId) ?? 0) + 1,
    );
    const current = byThread.value[normalized.threadId] ?? { busy: false, unknown: null };
    installRecord(normalized.threadId, {
      busy: current.busy,
      unknown: cloneUnknownMutation(normalized),
    });
  }

  function rememberUnknownIfUnsettled(mutation: UnknownThreadMutation): boolean {
    const current = getUnknown(mutation.threadId);
    if (settlementDisposition(
      mutation.threadId,
      mutation.mutationId,
      mutation.operation,
    ) !== null
      || (current && current.mutationId !== mutation.mutationId)) return false;
    rememberUnknown(mutation);
    return getUnknown(mutation.threadId)?.mutationId === mutation.mutationId;
  }

  function forgetUnknown(threadId: string): void {
    const current = byThread.value[threadId];
    unknownRecordGenerations.set(
      threadId,
      (unknownRecordGenerations.get(threadId) ?? 0) + 1,
    );
    if (!current?.unknown) return;
    installRecord(threadId, { busy: current.busy, unknown: null });
  }

  function settleUnknownFromEvent(
    threadId: string,
    mutationId: string,
    operation: string,
    disposition: FocusMutationDisposition,
  ): boolean {
    const normalizedMutationId = mutationId.trim();
    const normalizedOperation = operation.trim();
    if (!threadId.trim() || !normalizedMutationId || !normalizedOperation
      || !isFocusWebWireEnum('mutation_disposition', disposition)) return false;
    settledByThread.set(threadId, {
      mutationId: normalizedMutationId,
      operation: normalizedOperation,
      disposition,
    });
    const current = getUnknown(threadId);
    if (current?.mutationId !== normalizedMutationId || current.operation !== normalizedOperation) {
      return false;
    }
    forgetUnknown(threadId);
    return true;
  }

  function settlementDisposition(
    threadId: string,
    mutationId: string,
    operation: string,
  ): FocusMutationDisposition | null {
    const settled = settledByThread.get(threadId);
    return settled?.mutationId === mutationId.trim()
      && settled.operation === operation.trim()
      ? settled.disposition
      : null;
  }

  function replaceUnknownIfCurrent(
    receipt: UnknownMutationReceipt,
    mutation: UnknownThreadMutation,
  ): boolean {
    if (!unknownReceiptIsCurrent(receipt) || mutation.threadId !== receipt.threadId) return false;
    rememberUnknown(mutation);
    return true;
  }

  function forgetUnknownIfCurrent(receipt: UnknownMutationReceipt): boolean {
    if (!unknownReceiptIsCurrent(receipt)) return false;
    forgetUnknown(receipt.threadId);
    return true;
  }

  function reconcileUnknown(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean {
    const { threadId } = receipt;
    if (!threadId.trim()
      || (unknownRecordGenerations.get(threadId) ?? 0) !== receipt.generation) return false;
    const normalized = normalizeUnknownMutation({
      threadId,
      mutation_id: mutation?.mutation_id,
      operation: mutation?.operation,
      durability: mutation?.durability,
      verification: mutation?.lifecycle_verification,
    });
    if (mutation !== null && !normalized) return false;
    if (!normalized) {
      forgetUnknown(threadId);
      return true;
    }
    const held = getUnknown(threadId);
    if (!normalized.verification && held?.mutationId === normalized.mutationId) {
      normalized.verification = held.verification;
    }
    rememberUnknown(normalized);
    return true;
  }

  function installUnknownFromMeta(initialMeta: FocusMeta): void {
    const mutations = Array.isArray(initialMeta.unknown_lifecycle_mutations)
      ? initialMeta.unknown_lifecycle_mutations
        .map((mutation) => normalizeUnknownLifecycleMutation({
          ...mutation,
          durability: 'process_local',
        }))
        .filter((mutation): mutation is UnknownLifecycleMutation => mutation !== null)
      : [];
    replaceUnknown(mutations);
  }

  return {
    busyByThread,
    unknownMutations,
    unknownLifecycleMutations,
    unknownProcessLocalMutations,
    isBusy,
    beginBusy,
    busyReceiptIsCurrent,
    finishBusy,
    getUnknown,
    captureSnapshot,
    captureUnknown,
    unknownReceiptIsCurrent,
    rememberUnknownIfUnsettled,
    settleUnknownFromEvent,
    settlementDisposition,
    replaceUnknownIfCurrent,
    forgetUnknownIfCurrent,
    reconcileUnknown,
    installUnknownFromMeta,
  };
}
