import { describe, expect, it } from 'vitest';
import { ref } from 'vue';
import type {
  FocusPendingRequest,
  FocusThreadSnapshot,
} from '../../../src/focus/types';
import { createPendingRequestActionState } from '../../../src/focus/client-state/pending-request-actions';
import { pendingRequestActionToken } from '../../../src/focus/pendingRequestCapability';

function request(
  id: string,
  responseCapability: string,
  kind: FocusPendingRequest['kind'],
  status: FocusPendingRequest['status'],
): FocusPendingRequest {
  return {
    id,
    connection_generation: 7,
    response_capability: responseCapability,
    kind,
    status,
  } as FocusPendingRequest;
}

function requestSnapshot(
  threadId: string,
  pendingRequests: FocusPendingRequest[],
): FocusThreadSnapshot {
  return {
    thread: { id: threadId },
    pending_requests: pendingRequests,
  } as FocusThreadSnapshot;
}

describe('createPendingRequestActionState', () => {
  it('keeps approval and question locks in one request record without conflating them', () => {
    const pending = request(
      'request-1', 'shared-capability', 'approval', 'pending',
    );
    const actionToken = pendingRequestActionToken(pending);
    const snapshot = ref<FocusThreadSnapshot | null>(requestSnapshot('thread-1', [pending]));
    const state = createPendingRequestActionState(snapshot);

    const approvalReceipt = state.beginApproval(actionToken)!;
    const questionReceipt = state.beginQuestion(actionToken, 'dismiss')!;
    expect(state.approvalActions.value).toEqual({ [actionToken]: true });
    expect(state.questionActions.value).toEqual({ [actionToken]: 'dismiss' });

    expect(state.finishApproval(approvalReceipt)).toBe(true);
    expect(state.approvalActions.value).toEqual({});
    expect(state.questionActions.value).toEqual({ [actionToken]: 'dismiss' });
    expect(state.finishQuestion(questionReceipt)).toBe(true);
    expect(state.questionActions.value).toEqual({});
  });

  it('does not let stale receipts finish replacement approval or question actions', () => {
    const approval = request(
      'approval-1', 'approval-capability', 'approval', 'pending',
    );
    const question = request(
      'question-1', 'question-capability', 'question', 'pending',
    );
    const approvalToken = pendingRequestActionToken(approval);
    const questionToken = pendingRequestActionToken(question);
    const state = createPendingRequestActionState(ref(
      requestSnapshot('thread-1', [approval, question]),
    ));
    const firstApproval = state.beginApproval(approvalToken)!;
    const replacementApproval = state.beginApproval(approvalToken)!;
    const firstQuestion = state.beginQuestion(questionToken, 'answer')!;
    const replacementQuestion = state.beginQuestion(questionToken, 'dismiss')!;

    expect(state.finishApproval(firstApproval)).toBe(false);
    expect(state.finishQuestion(firstQuestion)).toBe(false);
    expect(state.approvalActions.value).toEqual({ [approvalToken]: true });
    expect(state.questionActions.value).toEqual({ [questionToken]: 'dismiss' });

    expect(state.finishApproval(replacementApproval)).toBe(true);
    expect(state.finishQuestion(replacementQuestion)).toBe(true);
    expect(state.approvalActions.value).toEqual({});
    expect(state.questionActions.value).toEqual({});
  });

  it('keeps accepted actions sticky only while their exact pending tokens remain', () => {
    const oldApproval = request(
      'shared-request', 'old-approval-capability', 'approval', 'pending',
    );
    const oldQuestion = request(
      'question-1', 'question-capability', 'question', 'pending',
    );
    const snapshot = ref(requestSnapshot('thread-1', [oldApproval, oldQuestion]));
    const state = createPendingRequestActionState(snapshot);
    const oldApprovalToken = pendingRequestActionToken(oldApproval);
    const oldQuestionToken = pendingRequestActionToken(oldQuestion);
    const approvalReceipt = state.beginApproval(oldApprovalToken)!;
    const questionReceipt = state.beginQuestion(oldQuestionToken, 'answer')!;

    expect(state.acceptApproval(approvalReceipt)).toBe(true);
    expect(state.acceptQuestion(questionReceipt)).toBe(true);
    expect(state.approvalActions.value).toEqual({ [oldApprovalToken]: true });
    expect(state.questionActions.value).toEqual({ [oldQuestionToken]: 'answer' });

    const replacementApproval = {
      ...oldApproval,
      response_capability: 'replacement-approval-capability',
    };
    const replacementApprovalToken = pendingRequestActionToken(replacementApproval);
    snapshot.value = requestSnapshot('thread-1', [replacementApproval]);

    expect(state.approvalActions.value).toEqual({});
    expect(state.questionActions.value).toEqual({});
    expect(state.approvalActions.value[replacementApprovalToken]).toBeUndefined();

    const replacementReceipt = state.beginApproval(replacementApprovalToken)!;
    expect(state.approvalActions.value).toEqual({
      [replacementApprovalToken]: true,
    });
    expect(state.finishApproval(replacementReceipt)).toBe(true);
  });

  it.each(['approval', 'question'] as const)(
    'keeps an accepted %s lock across null and other-thread snapshots',
    (kind) => {
      const pending = request(
        `${kind}-1`, `${kind}-capability`, kind, 'pending',
      );
      const actionToken = pendingRequestActionToken(pending);
      const snapshot = ref<FocusThreadSnapshot | null>(
        requestSnapshot('thread-1', [pending]),
      );
      const state = createPendingRequestActionState(snapshot);

      if (kind === 'approval') {
        expect(state.acceptApproval(state.beginApproval(actionToken)!)).toBe(true);
      } else {
        expect(state.acceptQuestion(
          state.beginQuestion(actionToken, 'answer')!,
        )).toBe(true);
      }

      snapshot.value = null;
      snapshot.value = requestSnapshot('thread-2', []);
      if (kind === 'approval') {
        expect(state.approvalActions.value[actionToken]).toBe(true);
      } else {
        expect(state.questionActions.value[actionToken]).toBe('answer');
      }

      snapshot.value = requestSnapshot('thread-1', [pending]);
      if (kind === 'approval') {
        expect(state.approvalActions.value[actionToken]).toBe(true);
      } else {
        expect(state.questionActions.value[actionToken]).toBe('answer');
      }

      snapshot.value = requestSnapshot('thread-1', []);
      expect(state.approvalActions.value[actionToken]).toBeUndefined();
      expect(state.questionActions.value[actionToken]).toBeUndefined();
    },
  );

  it('clear invalidates local receipts without erasing authoritative server locks', () => {
    const pendingApproval = request(
      'approval-pending', 'approval-pending-capability', 'approval', 'pending',
    );
    const pendingQuestion = request(
      'question-pending', 'question-pending-capability', 'question', 'pending',
    );
    const approvalToken = pendingRequestActionToken(pendingApproval);
    const questionToken = pendingRequestActionToken(pendingQuestion);
    const snapshot = ref<FocusThreadSnapshot | null>(requestSnapshot(
      'thread-1', [pendingApproval, pendingQuestion],
    ));
    const state = createPendingRequestActionState(snapshot);
    const approvalReceipt = state.beginApproval(approvalToken)!;
    const questionReceipt = state.beginQuestion(questionToken, 'dismiss')!;

    state.clear();

    expect(state.approvalActions.value).toEqual({});
    expect(state.questionActions.value).toEqual({});
    expect(state.acceptApproval(approvalReceipt)).toBe(false);
    expect(state.finishApproval(approvalReceipt)).toBe(false);
    expect(state.acceptQuestion(questionReceipt)).toBe(false);
    expect(state.finishQuestion(questionReceipt)).toBe(false);

    const submittedApproval = request(
      'approval-1', 'server-approval-capability', 'approval', 'submitted',
    );
    const unknownQuestion = request(
      'question-1', 'server-question-capability', 'question', 'unknown',
    );
    snapshot.value = requestSnapshot('thread-1', [submittedApproval, unknownQuestion]);

    expect(state.approvalActions.value).toEqual({
      [pendingRequestActionToken(submittedApproval)]: true,
    });
    expect(state.questionActions.value).toEqual({
      [pendingRequestActionToken(unknownQuestion)]: 'answer',
    });
  });

  it('projects already-settled server requests as action locks', () => {
    const approval = request(
      'approval-1', 'approval-capability', 'approval', 'submitted',
    );
    const question = request(
      'question-1', 'question-capability', 'question', 'submitted',
    );
    const snapshot = ref(requestSnapshot(
      'thread-1', [
        approval,
        question,
        request('pending-1', 'pending-capability', 'approval', 'pending'),
      ],
    ));
    const state = createPendingRequestActionState(snapshot);

    expect(state.approvalActions.value).toEqual({
      [pendingRequestActionToken(approval)]: true,
    });
    expect(state.questionActions.value).toEqual({
      [pendingRequestActionToken(question)]: 'answer',
    });
  });
});
