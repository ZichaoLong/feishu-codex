import { computed, ref, watch } from 'vue';
import type { ComputedRef, Ref } from 'vue';
import type { FocusThreadSnapshot } from '../types';
import { pendingRequestActionToken } from '../pendingRequestCapability';

export type PendingQuestionAction = 'answer' | 'dismiss';

interface PendingApprovalActionRecord {
  generation: number;
  threadId: string;
  phase: 'sending' | 'accepted';
}
interface PendingQuestionActionRecord {
  generation: number;
  threadId: string;
  action: PendingQuestionAction;
  accepted: boolean;
}

interface PendingRequestActionRecord {
  approval: PendingApprovalActionRecord | null;
  question: PendingQuestionActionRecord | null;
}

export interface PendingRequestActionReceipt {
  readonly actionToken: string;
  readonly generation: number;
  readonly kind: 'approval' | 'question';
  readonly threadId: string;
}

export interface PendingRequestActionState {
  readonly approvalActions: ComputedRef<Record<string, true>>;
  readonly questionActions: ComputedRef<Record<string, PendingQuestionAction>>;
  beginApproval(actionToken: string): PendingRequestActionReceipt | null;
  acceptApproval(receipt: PendingRequestActionReceipt): boolean;
  finishApproval(receipt: PendingRequestActionReceipt): boolean;
  beginQuestion(
    actionToken: string,
    action: PendingQuestionAction,
  ): PendingRequestActionReceipt | null;
  acceptQuestion(receipt: PendingRequestActionReceipt): boolean;
  finishQuestion(receipt: PendingRequestActionReceipt): boolean;
  clear(): void;
}

/** Keeps the two per-request action locks under one canonical request record. */
export function createPendingRequestActionState(
  snapshot: Readonly<Ref<FocusThreadSnapshot | null>>,
): PendingRequestActionState {
  const byRequest = ref<Record<string, PendingRequestActionRecord>>({});
  let actionGeneration = 0;

  function currentPendingThreadId(actionToken: string): string {
    const current = snapshot.value;
    if (!current) return '';
    const threadId = current.thread.id.trim();
    if (!threadId) return '';
    return current.pending_requests.some((request) => (
      request.status === 'pending' && pendingRequestActionToken(request) === actionToken
    )) ? threadId : '';
  }

  function authoritativeSnapshotSettles(threadId: string, actionToken: string): boolean {
    const current = snapshot.value;
    if (!current || current.thread.id.trim() !== threadId) return false;
    return !current.pending_requests.some((request) => (
      request.status === 'pending' && pendingRequestActionToken(request) === actionToken
    ));
  }

  function install(requestId: string, record: PendingRequestActionRecord): void {
    const next = { ...byRequest.value };
    if (!record.approval && !record.question) delete next[requestId];
    else next[requestId] = record;
    byRequest.value = next;
  }

  const approvalActions = computed<Record<string, true>>(() => {
    const result: Record<string, true> = {};
    for (const [requestId, record] of Object.entries(byRequest.value)) {
      if (record.approval) result[requestId] = true;
    }
    for (const request of snapshot.value?.pending_requests ?? []) {
      if (request.kind === 'approval' && request.status !== 'pending') {
        result[pendingRequestActionToken(request)] = true;
      }
    }
    return result;
  });

  const questionActions = computed<Record<string, PendingQuestionAction>>(() => {
    const result: Record<string, PendingQuestionAction> = {};
    for (const [requestId, record] of Object.entries(byRequest.value)) {
      if (record.question) result[requestId] = record.question.action;
    }
    for (const request of snapshot.value?.pending_requests ?? []) {
      if (
        (request.kind === 'question' || request.kind === 'elicitation')
        && request.status !== 'pending'
      ) result[pendingRequestActionToken(request)] = 'answer';
    }
    return result;
  });

  const stopSnapshotReconciliation = watch(
    () => snapshot.value,
    () => {
      let changed = false;
      const next: Record<string, PendingRequestActionRecord> = {};
      for (const [actionToken, record] of Object.entries(byRequest.value)) {
        const approval = record.approval?.phase === 'accepted'
          && authoritativeSnapshotSettles(record.approval.threadId, actionToken)
          ? null
          : record.approval;
        const question = record.question?.accepted
          && authoritativeSnapshotSettles(record.question.threadId, actionToken)
          ? null
          : record.question;
        if (approval !== record.approval || question !== record.question) changed = true;
        if (approval || question) next[actionToken] = { approval, question };
      }
      if (changed) byRequest.value = next;
    },
    { flush: 'sync' },
  );

  return {
    approvalActions,
    questionActions,
    beginApproval(actionToken) {
      const threadId = currentPendingThreadId(actionToken);
      if (!threadId) return null;
      const current = byRequest.value[actionToken] ?? {
        approval: null,
        question: null,
      };
      const receipt: PendingRequestActionReceipt = {
        actionToken,
        generation: ++actionGeneration,
        kind: 'approval',
        threadId,
      };
      install(actionToken, {
        ...current,
        approval: { generation: receipt.generation, threadId, phase: 'sending' },
      });
      return receipt;
    },
    acceptApproval(receipt) {
      const current = byRequest.value[receipt.actionToken];
      if (receipt.kind !== 'approval'
        || current?.approval?.generation !== receipt.generation
        || current.approval.threadId !== receipt.threadId
        || current.approval.phase !== 'sending') return false;
      if (authoritativeSnapshotSettles(receipt.threadId, receipt.actionToken)) {
        install(receipt.actionToken, { ...current, approval: null });
        return true;
      }
      install(receipt.actionToken, {
        ...current,
        approval: { ...current.approval, phase: 'accepted' },
      });
      return true;
    },
    finishApproval(receipt) {
      const current = byRequest.value[receipt.actionToken];
      if (receipt.kind !== 'approval'
        || current?.approval?.generation !== receipt.generation
        || current.approval.threadId !== receipt.threadId) return false;
      install(receipt.actionToken, { ...current, approval: null });
      return true;
    },
    beginQuestion(actionToken, action) {
      const threadId = currentPendingThreadId(actionToken);
      if (!threadId) return null;
      const current = byRequest.value[actionToken] ?? {
        approval: null,
        question: null,
      };
      const receipt: PendingRequestActionReceipt = {
        actionToken,
        generation: ++actionGeneration,
        kind: 'question',
        threadId,
      };
      install(actionToken, {
        ...current,
        question: {
          generation: receipt.generation,
          threadId,
          action,
          accepted: false,
        },
      });
      return receipt;
    },
    acceptQuestion(receipt) {
      const current = byRequest.value[receipt.actionToken];
      if (receipt.kind !== 'question'
        || current?.question?.generation !== receipt.generation
        || current.question.threadId !== receipt.threadId) return false;
      if (authoritativeSnapshotSettles(receipt.threadId, receipt.actionToken)) {
        install(receipt.actionToken, { ...current, question: null });
        return true;
      }
      install(receipt.actionToken, {
        ...current,
        question: { ...current.question, accepted: true },
      });
      return true;
    },
    finishQuestion(receipt) {
      const current = byRequest.value[receipt.actionToken];
      if (receipt.kind !== 'question'
        || current?.question?.generation !== receipt.generation
        || current.question.threadId !== receipt.threadId) return false;
      install(receipt.actionToken, { ...current, question: null });
      return true;
    },
    clear() {
      stopSnapshotReconciliation();
      byRequest.value = {};
    },
  };
}
