import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createFirstPromptPossiblySentDraft,
  createUnknownSubmissionDraftStore,
  THREAD_CREATE_FIRST_PROMPT_OPERATION,
} from '../../../src/focus/webPromptMutationRecovery';

const STORAGE_KEY = 'focus-web.first-prompt-unknown:document';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear() { values.clear(); },
    getItem(key) { return values.get(key) ?? null; },
    key(index) { return [...values.keys()][index] ?? null; },
    removeItem(key) { values.delete(key); },
    setItem(key, value) { values.set(key, value); },
  };
}

beforeEach(() => {
  vi.stubGlobal('sessionStorage', memoryStorage());
});

describe('thread-create first-prompt recovery store', () => {
  it('persists only a typed text-only possibly-sent record', () => {
    const store = createUnknownSubmissionDraftStore();
    store.install('client-a');
    const draft = createFirstPromptPossiblySentDraft({
      clientId: 'client-a',
      text: 'check the created thread first',
      threadId: 'thread-a',
      cwd: '/work',
      hadAttachments: true,
    });

    expect(store.save(draft, true)).toBe(true);
    expect(store.forThread('thread-a')).toEqual(draft);
    expect(draft).toMatchObject({
      schemaVersion: 1,
      attemptKind: 'thread_create_first_prompt',
      operation: THREAD_CREATE_FIRST_PROMPT_OPERATION,
      attachments: [],
      handoffHadAttachments: true,
      recoveryPhase: 'possibly_sent',
      recoveryBlocked: false,
    });

    const serialized = sessionStorage.getItem(STORAGE_KEY) ?? '';
    expect(serialized).toContain('check the created thread first');
    for (const forbidden of [
      'mutationId',
      'recoveryCapability',
      'reservationGeneration',
      'sourceDocumentReceipt',
      'clientUserMessageId',
    ]) expect(serialized).not.toContain(forbidden);
  });

  it('reloads the exact client record and ignores a copied record for another client', () => {
    const first = createUnknownSubmissionDraftStore();
    first.install('client-a');
    const draft = createFirstPromptPossiblySentDraft({
      clientId: 'client-a',
      text: 'possibly sent',
      threadId: 'thread-a',
      cwd: '/work',
      hadAttachments: false,
    });
    expect(first.save(draft, true)).toBe(true);

    const reloaded = createUnknownSubmissionDraftStore();
    reloaded.install('client-a');
    expect(reloaded.drafts.value).toEqual([draft]);

    const copiedTab = createUnknownSubmissionDraftStore();
    copiedTab.install('client-b');
    expect(copiedTab.drafts.value).toEqual([]);
  });

  it('surfaces malformed same-client state as discardable corrupt state', () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
      schemaVersion: 1,
      attempts: [{
        schemaVersion: 1,
        clientId: 'client-a',
        text: 'payload without an exact thread',
      }],
    }));
    const store = createUnknownSubmissionDraftStore();
    store.install('client-a');

    expect(store.drafts.value).toHaveLength(1);
    expect(store.drafts.value[0]).toMatchObject({
      attemptKind: 'corrupt',
      recoveryPhase: 'corrupt',
      recoveryBlocked: true,
      text: '',
      attachments: [],
    });
    expect(store.remove(store.drafts.value[0]!.attemptKey)).toBe(true);
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('does not publish a required durable save when sessionStorage fails', () => {
    const unavailable = memoryStorage();
    unavailable.setItem = () => { throw new Error('storage unavailable'); };
    vi.stubGlobal('sessionStorage', unavailable);
    const store = createUnknownSubmissionDraftStore();
    store.install('client-a');
    const draft = createFirstPromptPossiblySentDraft({
      clientId: 'client-a',
      text: 'keep in Composer',
      threadId: 'thread-a',
      cwd: '/work',
      hadAttachments: false,
    });

    expect(store.save(draft, true)).toBe(false);
    expect(store.drafts.value).toEqual([]);
  });
});
