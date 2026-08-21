import { describe, expect, it, vi } from 'vitest';
import { FocusApiError } from '../../src/focus/types';
import { AUTO_MODEL_ID, useFocusWebClient } from '../../src/focus/useFocusWebClient';
import { FakeApi, installFocusClientTestHooks } from './support';

const FIRST_PROMPT_KEY = 'focus-web.first-prompt-unknown:document';

installFocusClientTestHooks();

describe('Focus Web draft creation and attachments', () => {
  it('uses auto model semantics and sends a new thread first prompt only once', async () => {
    const api = new FakeApi();
    api.threads = [];
    api.setDraftProfile();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    expect(client.selectedModelId.value).toBe(AUTO_MODEL_ID);
    await client.submit('hello');

    expect(api.startThreadCalls).toEqual([{
      text: 'hello',
      cwd: '/work',
      attachmentIds: [],
      intentGeneration: 1,
    }]);
    expect(api.submitPromptCalls).toEqual([]);
  });

  it('keeps a created thread selected when its background directory read is stale', async () => {
    const api = new FakeApi();
    api.threads = [];
    api.setDraftProfile();
    const originalListThreads = api.listThreads.bind(api);
    let listAttempt = 0;
    const listSpy = vi.spyOn(api, 'listThreads').mockImplementation(async (options = {}) => {
      listAttempt += 1;
      if (listAttempt === 2) {
        throw new FocusApiError('stale list', {
          status: 409,
          code: 'stale_thread_list',
        });
      }
      return originalListThreads(options);
    });
    const readSpy = vi.spyOn(api, 'readThread');
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    await expect(client.submit('hello')).resolves.toBe(true);
    await vi.waitFor(() => expect(listAttempt).toBeGreaterThanOrEqual(2));

    expect(api.startThreadCalls).toHaveLength(1);
    expect(api.submitPromptCalls).toEqual([]);
    expect(client.activeThreadId.value).toBe('thread-new');
    expect(client.snapshot.value?.thread.id).toBe('thread-new');
    expect(client.errorMessage.value).toBe('');
    expect(readSpy.mock.invocationCallOrder[0]).toBeLessThan(
      listSpy.mock.invocationCallOrder[1]!,
    );
    client.dispose();
  });

  it('stages attachments without starting a turn and submits their ids atomically', async () => {
    const api = new FakeApi();
    api.threads = [];
    api.setDraftProfile();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    const uploaded = await client.uploadAttachment(
      new Blob(['png'], { type: 'image/png' }),
      'demo.png',
    );
    expect(api.startThreadCalls).toEqual([]);

    await client.submit('', [{
      fileId: uploaded!.fileId,
      kind: 'image',
      name: uploaded!.name,
      mediaType: uploaded!.mediaType,
    }]);

    expect(api.startThreadCalls).toEqual([{
      text: '',
      cwd: '/work',
      attachmentIds: ['attachment-1'],
      intentGeneration: 1,
    }]);
  });

  it('opens a created thread and records only text when its first prompt may be sent', async () => {
    const api = new FakeApi();
    api.threads = [];
    api.setDraftProfile();
    api.startThreadErrorAfterCreate = new FocusApiError('first prompt outcome unknown', {
      status: 503,
      code: 'turn_submission_unknown',
      details: { thread_id: 'thread-new', operation: 'prompt' },
    });
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    await expect(client.submit('inspect before retry', [{
      fileId: 'attachment-1',
      kind: 'file',
      name: 'notes.txt',
      mediaType: 'text/plain',
    }])).resolves.toBe(true);

    expect(client.activeThreadId.value).toBe('thread-new');
    expect(client.snapshot.value?.thread.id).toBe('thread-new');
    expect(client.unknownSubmissionDraft.value).toMatchObject({
      schemaVersion: 1,
      attemptKind: 'thread_create_first_prompt',
      operation: 'thread_create_first_prompt',
      text: 'inspect before retry',
      threadId: 'thread-new',
      attachments: [],
      handoffHadAttachments: true,
      recoveryPhase: 'possibly_sent',
    });
    expect(client.canRetryUnknownSubmission.value).toBe(true);
    expect(api.startThreadCalls).toHaveLength(1);
    expect(api.submitPromptCalls).toEqual([]);
    expect(api.unknownResolutionCalls).toEqual([]);
    const stored = sessionStorage.getItem(FIRST_PROMPT_KEY) ?? '';
    expect(stored).toContain('inspect before retry');
    expect(stored).not.toContain('attachment-1');

    const commit = vi.fn(() => true);
    const recovered = await client.takeUnknownSubmissionForRetry(
      (draft) => () => {
        expect(draft.attachments).toEqual([]);
        return commit();
      },
    );
    expect(commit).toHaveBeenCalledOnce();
    expect(recovered).toMatchObject({
      handoffReason: 'possibly_sent',
      hadAttachments: true,
    });
    expect(sessionStorage.getItem(FIRST_PROMPT_KEY)).toBeNull();
  });

  it('opens a partially created thread while retaining a known-no-effect draft', async () => {
    const api = new FakeApi();
    api.threads = [];
    api.setDraftProfile();
    api.startThreadErrorAfterCreate = new FocusApiError('first turn failed', {
      status: 409,
      code: 'thread_created_turn_not_started',
      details: { thread_id: 'thread-new' },
    });
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    await expect(client.submit('restore me')).resolves.toBe(false);

    expect(client.activeThreadId.value).toBe('thread-new');
    expect(client.snapshot.value?.thread.id).toBe('thread-new');
    expect(client.unknownSubmissionDraft.value).toBeNull();
    expect(api.startThreadCalls).toHaveLength(1);
  });
});
