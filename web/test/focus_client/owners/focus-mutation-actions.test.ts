import { describe, expect, it, vi } from 'vitest';
import {
  createFirstPromptPossiblySentDraft,
  THREAD_CREATE_FIRST_PROMPT_OPERATION,
  type UnknownSubmissionDraft,
} from '../../../src/focus/webPromptMutationRecovery';
import {
  FocusApiError,
  type FocusAttachmentUpload,
  type FocusMeta,
  type FocusPromptResultReceipt,
} from '../../../src/focus/types';
import {
  PROMPT_MUTATION_ID,
  deferred,
  harness,
  installMutationActionsTestHooks,
  scopeReceipt,
  snapshot,
  thread,
} from './mutation-actions-test-support';

const PROMPT_LOCATOR_KEY = 'focus-web.prompt-result-locators';
const FIRST_PROMPT_KEY = 'focus-web.first-prompt-unknown:document';

installMutationActionsTestHooks();

function promptReceipt(
  status: FocusPromptResultReceipt['status'] = 'succeeded',
  overrides: Partial<FocusPromptResultReceipt> = {},
): FocusPromptResultReceipt {
  return {
    thread_id: 'thread-a',
    mutation_id: PROMPT_MUTATION_ID,
    client_user_message_id: `focus-web:${PROMPT_MUTATION_ID}`,
    status,
    mode: 'steer',
    turn_id: 'turn-1',
    reason_code: status === 'known_no_effect' ? 'turn_replaced' : '',
    ...overrides,
  };
}

function firstPromptDraft(): UnknownSubmissionDraft {
  return createFirstPromptPossiblySentDraft({
    clientId: 'client-1',
    text: 'check the created thread',
    threadId: 'thread-new',
    cwd: '/draft',
    hadAttachments: true,
  });
}

describe('FocusMutationActions shared prompt and interrupt authority', () => {
  it('lets a materialized non-owner interrupt and submit one shared prompt POST', async () => {
    const h = harness();
    h.activeThread.value = {
      ...thread(),
      owner: {
        kind: 'fcodex',
        holder_id: 'fcodex:other',
        relation: 'other',
        label: 'Another trusted local client',
      },
    };

    expect(h.actions.canInterrupt.value).toBe(true);
    expect(h.actions.canSubmit.value).toBe(true);
    await h.actions.interrupt();
    await expect(h.actions.submit('shared follow-up')).resolves.toBe(true);

    expect(h.api.interrupt).toHaveBeenCalledWith('thread-a', 'turn-1');
    expect(h.api.submitPrompt).toHaveBeenCalledOnce();
    expect(h.api.submitPrompt).toHaveBeenCalledWith('thread-a', {
      text: 'shared follow-up',
      attachmentIds: [],
      mutationId: PROMPT_MUTATION_ID,
      sourceScopeGeneration: 1,
      sourceAttachmentScope: 'thread:thread-a',
      sourceComposerScopeId: 'client-1:generation:1:thread:thread-a',
    });
    expect(h.api.readPromptResult).not.toHaveBeenCalled();
  });

  it('interrupts with an empty target when no exact active turn is projected', async () => {
    const h = harness();
    h.snapshot.value = {
      ...snapshot(),
      active_turn_id: '',
      active_turn_status: '',
    };

    await h.actions.interrupt();
    expect(h.api.interrupt).toHaveBeenCalledWith('thread-a', '');
  });

  it('keeps Stop available while the one prompt POST is pending', async () => {
    const h = harness();
    const response = deferred<FocusPromptResultReceipt>();
    h.api.submitPrompt.mockReturnValueOnce(response.promise);
    const submitting = h.actions.submit('pending prompt');
    await vi.waitFor(() => expect(h.api.submitPrompt).toHaveBeenCalledOnce());

    expect(h.actions.starting.value).toBe(true);
    expect(h.actions.canInterrupt.value).toBe(true);
    await h.actions.interrupt();
    expect(h.api.interrupt).toHaveBeenCalledWith('thread-a', 'turn-1');

    response.resolve(promptReceipt());
    await expect(submitting).resolves.toBe(true);
  });

  it('does not expose Stop while disconnected or after a stale scope entry', async () => {
    const disconnected = harness();
    disconnected.connection.value = 'disconnected';
    expect(disconnected.actions.canInterrupt.value).toBe(false);
    await disconnected.actions.interrupt();
    expect(disconnected.api.interrupt).not.toHaveBeenCalled();

    const stale = harness();
    stale.makeScopeStale();
    expect(stale.actions.canInterrupt.value).toBe(false);
    await stale.actions.interrupt();
    expect(stale.api.interrupt).not.toHaveBeenCalled();
  });
});

describe('FocusMutationActions upload receipts', () => {
  it.each([
    {
      receipt: scopeReceipt('thread-a', '/ignored', 3),
      expected: {
        name: 'demo.txt', threadId: 'thread-a', cwd: undefined, scopeGeneration: 3,
      },
    },
    {
      receipt: scopeReceipt('', '/draft-cwd', 4),
      expected: {
        name: 'demo.txt', threadId: undefined, cwd: '/draft-cwd', scopeGeneration: 4,
      },
    },
  ])('sends uploads with only the exact scope receipt', async ({ receipt, expected }) => {
    const h = harness();
    h.setScope(receipt);
    const uploaded = await h.actions.uploadAttachment(
      new Blob(['demo'], { type: 'text/plain' }),
      'demo.txt',
    );

    expect(h.api.uploadAttachment).toHaveBeenCalledWith(expect.any(Blob), expected);
    expect(uploaded?.fileId).toBe('file-1');
  });

  it('drops an upload completion after an ABA replacement', async () => {
    const h = harness();
    const response = deferred<FocusAttachmentUpload>();
    h.api.uploadAttachment.mockReturnValueOnce(response.promise);
    const uploading = h.actions.uploadAttachment(new Blob(['a']), 'a.txt');
    await vi.waitFor(() => expect(h.api.uploadAttachment).toHaveBeenCalledOnce());
    h.setScope(scopeReceipt('thread-b', '/work', 2));
    h.setScope(scopeReceipt('thread-a', '/work', 3));
    response.resolve({
      file_id: 'late-a', name: 'a.txt', media_type: 'text/plain', size: 1, url: '',
    });

    await expect(uploading).resolves.toBeNull();
  });
});

describe('FocusMutationActions single-POST settlement', () => {
  it('settles succeeded before background projection convergence finishes', async () => {
    const h = harness();
    const refresh = deferred<boolean>();
    h.refreshThreads.mockReturnValueOnce(refresh.promise);

    const result = h.actions.submit('one post', [{ fileId: 'file-1', kind: 'file' }]);
    await expect(result).resolves.toBe(true);
    expect(h.api.submitPrompt).toHaveBeenCalledOnce();
    expect(h.refreshThreads).toHaveBeenCalledOnce();
    expect(h.refreshActiveThread).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();

    refresh.resolve(true);
    await vi.waitFor(() => expect(h.refreshActiveThread).toHaveBeenCalledOnce());
  });

  it('retains only an exact known-no-effect or pre-effect 4xx refusal', async () => {
    const exact = harness();
    exact.api.submitPrompt.mockResolvedValueOnce(promptReceipt('known_no_effect'));
    await expect(exact.actions.submit('turn A changed')).resolves.toBe(false);
    expect(exact.reportError).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();

    const refused = harness();
    refused.api.submitPrompt.mockRejectedValueOnce(new FocusApiError('bad scope', {
      status: 409,
      code: 'invalid_submission_scope',
      effectEvidence: 'pre_effect',
    }));
    await expect(refused.actions.submit('retry explicitly')).resolves.toBe(false);
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();
  });

  it('keeps text but retires unsafe chips after attachment rollback fails', async () => {
    const h = harness();
    h.api.submitPrompt.mockResolvedValueOnce(promptReceipt('known_no_effect', {
      reason_code: 'attachment_rollback_failed',
    }));
    const retainTextOnly = vi.fn(() => true);

    await expect(h.actions.submit(
      'keep text',
      [{ fileId: 'unsafe-file', kind: 'file' }],
      retainTextOnly,
    )).resolves.toBe(false);

    expect(retainTextOnly).toHaveBeenCalledOnce();
    expect(h.promptMessage).toHaveBeenCalledWith(
      'promptAttachmentRollbackFailed',
      { reason: '' },
    );
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();

    const noExactOwner = harness();
    noExactOwner.api.submitPrompt.mockResolvedValueOnce(promptReceipt('known_no_effect', {
      reason_code: 'attachment_rollback_failed',
    }));
    await expect(noExactOwner.actions.submit(
      'cannot safely retain chips',
      [{ fileId: 'unsafe-file', kind: 'file' }],
    )).resolves.toBe(true);
  });

  it.each([
    ['pending', promptReceipt('pending')],
    ['outcome_unknown', promptReceipt('outcome_unknown')],
  ] as const)('commits a %s receipt and retains only its GET locator', async (_label, receipt) => {
    const h = harness();
    h.api.submitPrompt.mockResolvedValueOnce(receipt);

    await expect(h.actions.submit('possibly sent')).resolves.toBe(true);
    const stored = sessionStorage.getItem(PROMPT_LOCATOR_KEY) ?? '';
    expect(stored).toContain(PROMPT_MUTATION_ID);
    expect(stored).not.toContain('possibly sent');
    expect(h.api.submitPrompt).toHaveBeenCalledOnce();
  });

  it.each([
    new FocusApiError('gateway failed', { status: 503, code: 'gateway_failure' }),
    new FocusApiError('invalid decoder', { status: 502, code: 'invalid_gateway_response' }),
    new FocusApiError('proxy refusal', { status: 409, code: 'http_409' }),
    new FocusApiError('proxy timeout', { status: 408, code: 'http_408' }),
    new FocusApiError('receipt retired after effect', {
      status: 409,
      code: 'prompt_result_unavailable',
      effectEvidence: 'unknown',
    }),
    new FocusApiError('receipt is already executing', {
      status: 409,
      code: 'prompt_result_pending',
      effectEvidence: 'unknown',
    }),
    new FocusApiError('mutation identity already exists', {
      status: 409,
      code: 'prompt_mutation_conflict',
      effectEvidence: 'unknown',
    }),
    new TypeError('connection lost'),
  ])('commits every failure without pre-effect evidence as possibly-sent', async (failure) => {
    const h = harness();
    h.api.submitPrompt.mockRejectedValueOnce(failure);
    h.api.readPromptResult.mockRejectedValueOnce(new TypeError('still unavailable'));

    await expect(h.actions.submit('do not replay')).resolves.toBe(true);
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toContain(PROMPT_MUTATION_ID);
    expect(h.api.submitPrompt).toHaveBeenCalledOnce();
  });

  it('treats a mismatched receipt as possibly sent and invalidates projection', async () => {
    const h = harness();
    h.api.submitPrompt.mockResolvedValueOnce(promptReceipt('known_no_effect', {
      mutation_id: '00000000-0000-4000-8000-000000000002',
      client_user_message_id: 'focus-web:00000000-0000-4000-8000-000000000002',
    }));

    await expect(h.actions.submit('mismatched response')).resolves.toBe(true);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toContain(PROMPT_MUTATION_ID);
  });

  it('fails before HTTP only when an exact mutation id is unavailable', async () => {
    const noIdentity = harness();
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'invalid') });
    await expect(noIdentity.actions.submit('no identity')).resolves.toBe(false);
    expect(noIdentity.api.submitPrompt).not.toHaveBeenCalled();

    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => PROMPT_MUTATION_ID) });
    const unavailableStorage = {
      ...sessionStorage,
      setItem: () => { throw new Error('storage unavailable'); },
    };
    vi.stubGlobal('sessionStorage', unavailableStorage);
    const memoryLocator = harness();
    await expect(memoryLocator.actions.submit('memory locator')).resolves.toBe(true);
    expect(memoryLocator.api.submitPrompt).toHaveBeenCalledOnce();
  });

  it('reload reconciliation performs only GET and never dispatches or replays', async () => {
    sessionStorage.setItem(PROMPT_LOCATOR_KEY, JSON.stringify({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-a', mutationId: PROMPT_MUTATION_ID }],
    }));
    const h = harness();
    h.api.readPromptResult.mockResolvedValueOnce(promptReceipt('succeeded'));

    h.actions.installInitialState({ unknown_lifecycle_mutations: [] } as unknown as FocusMeta);
    await h.actions.reconcilePromptResultsForThread('thread-a');
    await vi.waitFor(() => expect(h.api.readPromptResult).toHaveBeenCalledWith(
      'thread-a',
      PROMPT_MUTATION_ID,
    ));

    expect(h.api.submitPrompt).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();
  });

  it.each([
    ['pending', 'promptPending', true],
    ['outcome_unknown', 'promptOutcomeUnknown', true],
    ['known_no_effect', 'promptKnownNoEffectAfterReload', false],
  ] as const)(
    'presents a reloaded %s receipt without replay',
    async (status, messageKey, retainsLocator) => {
      sessionStorage.setItem(PROMPT_LOCATOR_KEY, JSON.stringify({
        schemaVersion: 1,
        locators: [{ threadId: 'thread-a', mutationId: PROMPT_MUTATION_ID }],
      }));
      const h = harness();
      h.api.readPromptResult.mockResolvedValueOnce(promptReceipt(status));

      await h.actions.reconcilePromptResultsForThread('thread-a');

      expect(h.api.submitPrompt).not.toHaveBeenCalled();
      expect(h.promptMessage).toHaveBeenCalledWith(
        messageKey,
        expect.objectContaining({ reason: expect.any(String) }),
      );
      expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY) !== null).toBe(retainsLocator);
    },
  );

  it('retires authoritative unavailable and retains transient lookup failures', async () => {
    sessionStorage.setItem(PROMPT_LOCATOR_KEY, JSON.stringify({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-a', mutationId: PROMPT_MUTATION_ID }],
    }));
    const unavailable = harness();
    unavailable.api.readPromptResult.mockRejectedValueOnce(new FocusApiError(
      'receipt evicted',
      {
        status: 404,
        code: 'prompt_result_unavailable',
        effectEvidence: 'pre_effect',
      },
    ));
    await unavailable.actions.reconcilePromptResultsForThread('thread-a');
    expect(unavailable.promptMessage).toHaveBeenCalledWith(
      'promptResultUnavailable',
      { reason: '' },
    );
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toBeNull();

    sessionStorage.setItem(PROMPT_LOCATOR_KEY, JSON.stringify({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-a', mutationId: PROMPT_MUTATION_ID }],
    }));
    const transient = harness();
    transient.api.readPromptResult.mockRejectedValueOnce(new TypeError('offline'));
    await transient.actions.reconcilePromptResultsForThread('thread-a');
    expect(transient.promptMessage).toHaveBeenCalledWith(
      'promptResultLookupFailed',
      { reason: '' },
    );
    expect(sessionStorage.getItem(PROMPT_LOCATOR_KEY)).toContain(PROMPT_MUTATION_ID);
    expect(transient.api.submitPrompt).not.toHaveBeenCalled();
  });
});

describe('FocusMutationActions thread-create first-prompt recovery', () => {
  it('keeps draft thread creation on startThread and records typed text-only unknown', async () => {
    const h = harness();
    h.actions.installInitialState({ unknown_lifecycle_mutations: [] } as unknown as FocusMeta);
    h.setScope(scopeReceipt('', '/draft', 4));
    h.activeThreadId.value = '';
    h.api.startThread.mockRejectedValueOnce(new FocusApiError('unknown first turn', {
      status: 503,
      code: 'turn_submission_unknown',
      details: { operation: 'prompt', thread_id: 'thread-new' },
    }));

    await expect(h.actions.submit(
      'new thread draft',
      [{ fileId: 'draft-file', kind: 'file' }],
    )).resolves.toBe(true);

    expect(h.api.startThread).toHaveBeenCalledWith({
      text: 'new thread draft',
      cwd: '/draft',
      attachmentIds: ['draft-file'],
      intentGeneration: 1,
    });
    expect(h.api.submitPrompt).not.toHaveBeenCalled();
    expect(h.actions.unknownSubmissionDrafts.value[0]).toMatchObject({
      attemptKind: 'thread_create_first_prompt',
      operation: THREAD_CREATE_FIRST_PROMPT_OPERATION,
      text: 'new thread draft',
      attachments: [],
      handoffHadAttachments: true,
    });
    const stored = sessionStorage.getItem(FIRST_PROMPT_KEY) ?? '';
    expect(stored).toContain('new thread draft');
    expect(stored).not.toContain('draft-file');
  });

  it('explicitly hands off a possibly-sent first prompt as a new text-only draft', async () => {
    sessionStorage.setItem(FIRST_PROMPT_KEY, JSON.stringify({
      schemaVersion: 1,
      attempts: [firstPromptDraft()],
    }));
    const h = harness();
    h.actions.installInitialState({ unknown_lifecycle_mutations: [] } as unknown as FocusMeta);
    h.activeThreadId.value = 'thread-a';
    h.setScope(scopeReceipt('thread-new', '/draft', 2));
    const commit = vi.fn(() => true);
    const handoff = vi.fn(async () => commit);

    const result = await h.actions.takeUnknownSubmissionForRetry(
      handoff,
      firstPromptDraft().attemptKey,
    );

    expect(h.confirmUnconfirmedThread).toHaveBeenCalledWith('thread-new');
    expect(handoff).toHaveBeenCalledWith(
      expect.objectContaining({ text: 'check the created thread', attachments: [] }),
      expect.any(String),
    );
    expect(commit).toHaveBeenCalledOnce();
    expect(result).toMatchObject({
      handoffReason: 'possibly_sent',
      hadAttachments: true,
      recoveryRecordState: 'removed',
    });
    expect(sessionStorage.getItem(FIRST_PROMPT_KEY)).toBeNull();
  });

  it('discards the browser-only first-prompt record without a server mutation', async () => {
    sessionStorage.setItem(FIRST_PROMPT_KEY, JSON.stringify({
      schemaVersion: 1,
      attempts: [firstPromptDraft()],
    }));
    const h = harness();
    h.actions.installInitialState({ unknown_lifecycle_mutations: [] } as unknown as FocusMeta);

    await h.actions.discardUnknownSubmission(firstPromptDraft().attemptKey);

    expect(h.api.resolveUnknownMutation).not.toHaveBeenCalled();
    expect(h.api.readPromptResult).not.toHaveBeenCalled();
    expect(sessionStorage.getItem(FIRST_PROMPT_KEY)).toBeNull();
  });

  it('returns a known-no-effect created-thread failure with the Composer payload retained', async () => {
    const h = harness();
    h.setScope(scopeReceipt('', '/draft', 4));
    h.activeThreadId.value = '';
    h.api.startThread.mockRejectedValueOnce(new FocusApiError('turn not started', {
      status: 409,
      code: 'thread_created_turn_not_started',
      details: { thread_id: 'thread-new' },
    }));

    await expect(h.actions.submit('retain me')).resolves.toBe(false);
    expect(h.api.submitPrompt).not.toHaveBeenCalled();
  });
});
