import { describe, expect, it, vi } from 'vitest';
import type { ApprovalResponse, QuestionResponse } from '../../../src/types';
import { pendingRequestActionToken } from '../../../src/focus/pendingRequestCapability';
import { FocusApiError } from '../../../src/focus/types';
import {
  deferred,
  harness,
  installMutationActionsTestHooks,
  pendingRequest,
  snapshot,
  thread,
} from './mutation-actions-test-support';

installMutationActionsTestHooks();

describe('FocusMutationActions pending-response receipts', () => {
  const approvalResponse: ApprovalResponse = { decision: 'approved' };
  const questionResponse: QuestionResponse = {
    answers: { choice: { kind: 'single', optionId: 'yes' } },
  };

  it('rejects response tokens of the wrong interaction kind', async () => {
    const h = harness();
    const approval = pendingRequest('approval');
    const question = pendingRequest('question');
    const approvalToken = pendingRequestActionToken(approval);
    const questionToken = pendingRequestActionToken(question);
    h.snapshot.value = snapshot([approval, question]);

    await h.actions.respondApproval(questionToken, approvalResponse);
    await h.actions.respondQuestion(approvalToken, questionResponse);
    await h.actions.dismissQuestion(approvalToken);

    expect(h.api.respondRequest).not.toHaveBeenCalled();
  });

  it.each(['approval', 'question'] as const)(
    'keeps an accepted %s sticky when refresh fails and suppresses another response',
    async (kind) => {
      const h = harness();
      const pending = pendingRequest(kind);
      const token = pendingRequestActionToken(pending);
      h.snapshot.value = snapshot([pending]);
      h.refreshActiveThread.mockRejectedValue(new Error('refresh unavailable'));

      if (kind === 'approval') {
        await h.actions.respondApproval(token, approvalResponse);
        await h.actions.respondApproval(token, approvalResponse);
        expect(h.actions.pendingApprovalActions.value[token]).toBe(true);
      } else {
        await h.actions.respondQuestion(token, questionResponse);
        await h.actions.respondQuestion(token, questionResponse);
        expect(h.actions.pendingQuestionActions.value[token]).toBe('answer');
      }

      expect(h.api.respondRequest).toHaveBeenCalledOnce();
      expect(h.reportError).toHaveBeenCalledOnce();
    },
  );

  it.each([
    {
      label: 'a definitely-not-sent rejection',
      error: new FocusApiError('not sent', {
        status: 409, code: 'request_not_sent',
      }),
      sticky: false,
    },
    {
      label: 'an unknown response outcome',
      error: new FocusApiError('unknown', {
        status: 409, code: 'request_response_unknown',
      }),
      sticky: true,
    },
    {
      label: 'an already-processing response',
      error: new FocusApiError('processing', {
        status: 409, code: 'request_processing',
      }),
      sticky: true,
    },
    {
      label: 'backend reconciliation',
      error: new FocusApiError('reconciling', {
        status: 409, code: 'backend_reconciling',
      }),
      sticky: true,
    },
    {
      label: 'ambiguous transport failure',
      error: new Error('connection closed after write'),
      sticky: true,
    },
  ])('keeps the exact response lock after $label', async ({ error, sticky }) => {
    const h = harness();
    const approval = pendingRequest('approval');
    const token = pendingRequestActionToken(approval);
    h.snapshot.value = snapshot([approval]);
    h.api.respondRequest.mockRejectedValueOnce(error);

    await h.actions.respondApproval(token, approvalResponse);

    expect(h.actions.pendingApprovalActions.value[token]).toBe(sticky ? true : undefined);
    expect(h.api.respondRequest).toHaveBeenCalledOnce();
  });

  it.each(['answer', 'dismiss'] as const)(
    'keeps an ambiguous question %s locked on the shared response path',
    async (action) => {
      const h = harness();
      const question = pendingRequest('question');
      const token = pendingRequestActionToken(question);
      h.snapshot.value = snapshot([question]);
      h.api.respondRequest.mockRejectedValueOnce(
        new Error('connection closed after write'),
      );

      if (action === 'answer') {
        await h.actions.respondQuestion(token, questionResponse);
        await h.actions.respondQuestion(token, questionResponse);
      } else {
        await h.actions.dismissQuestion(token);
        await h.actions.dismissQuestion(token);
      }

      expect(h.api.respondRequest).toHaveBeenCalledOnce();
      expect(h.actions.pendingQuestionActions.value[token]).toBe(action);
    },
  );

  it.each(['approval', 'question'] as const)(
    'keeps an ambiguous %s response locked while another thread is projected',
    async (kind) => {
      const h = harness();
      const pending = pendingRequest(kind);
      const token = pendingRequestActionToken(pending);
      const responseGate = deferred<{ accepted: boolean }>();
      h.snapshot.value = snapshot([pending]);
      h.api.respondRequest.mockReturnValueOnce(responseGate.promise);

      const responding = kind === 'approval'
        ? h.actions.respondApproval(token, approvalResponse)
        : h.actions.respondQuestion(token, questionResponse);
      await vi.waitFor(() => expect(h.api.respondRequest).toHaveBeenCalledOnce());

      h.snapshot.value = null;
      h.snapshot.value = {
        ...snapshot(),
        thread: thread('thread-b'),
      };
      responseGate.reject(new FocusApiError('unknown', {
        status: 409,
        code: 'request_response_unknown',
      }));
      await responding;

      if (kind === 'approval') {
        expect(h.actions.pendingApprovalActions.value[token]).toBe(true);
      } else {
        expect(h.actions.pendingQuestionActions.value[token]).toBe('answer');
      }

      h.snapshot.value = snapshot([pending]);
      if (kind === 'approval') {
        expect(h.actions.pendingApprovalActions.value[token]).toBe(true);
      } else {
        expect(h.actions.pendingQuestionActions.value[token]).toBe('answer');
      }

      h.snapshot.value = snapshot();
      expect(h.actions.pendingApprovalActions.value[token]).toBeUndefined();
      expect(h.actions.pendingQuestionActions.value[token]).toBeUndefined();
    },
  );

  it('drops a late response continuation after disposal', async () => {
    const responding = harness();
    const approval = pendingRequest('approval');
    const token = pendingRequestActionToken(approval);
    responding.snapshot.value = snapshot([approval]);
    const response = deferred<{ accepted: boolean }>();
    responding.api.respondRequest.mockReturnValueOnce(response.promise);
    const reply = responding.actions.respondApproval(token, approvalResponse);
    await vi.waitFor(() => expect(responding.api.respondRequest).toHaveBeenCalledOnce());
    responding.actions.dispose();
    response.resolve({ accepted: true });
    await reply;
    expect(responding.refreshActiveThread).not.toHaveBeenCalled();
    expect(responding.reportError).not.toHaveBeenCalled();
  });
});
