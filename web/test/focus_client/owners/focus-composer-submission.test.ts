import { describe, expect, it, vi } from 'vitest';
import { ref } from 'vue';
import {
  createComposerSubmission,
  useComposerSubmissionRevision,
} from '../../../src/components/chat/composerSubmission';
import { dispatchFocusComposerPayload } from '../../../src/focus/focusComposerSubmission';

function submissionHarness() {
  let current = true;
  const clear = vi.fn();
  const clearAttachments = vi.fn();
  const settled = vi.fn();
  const submission = createComposerSubmission(
    {
      text: 'keep me',
      attachments: [{ fileId: 'file-1', kind: 'file' }],
    },
    () => current,
    clear,
    clearAttachments,
    settled,
  );
  return {
    submission,
    clear,
    clearAttachments,
    settled,
    makeStale: () => { current = false; },
  };
}

describe('dispatchFocusComposerPayload', () => {
  it('retains an exact payload in place when the mutation boundary refuses it', async () => {
    const h = submissionHarness();
    await dispatchFocusComposerPayload(h.submission, async () => false);
    expect(h.clear).not.toHaveBeenCalled();
    expect(h.settled).toHaveBeenCalledOnce();
  });

  it('keeps the payload pending until an accepted dispatch settles', async () => {
    const accepted = submissionHarness();
    let settleDispatch!: (accepted: boolean) => void;
    const dispatchResult = new Promise<boolean>((resolve) => { settleDispatch = resolve; });
    const pending = dispatchFocusComposerPayload(
      accepted.submission,
      () => dispatchResult,
    );

    expect(accepted.clear).not.toHaveBeenCalled();
    expect(accepted.settled).not.toHaveBeenCalled();
    settleDispatch(true);
    await pending;
    expect(accepted.clear).toHaveBeenCalledOnce();
    expect(accepted.settled).toHaveBeenCalledOnce();
  });

  it('does not clear newer Composer content after an accepted dispatch', async () => {
    const edited = submissionHarness();
    edited.makeStale();
    await dispatchFocusComposerPayload(edited.submission, async () => true);
    expect(edited.clear).not.toHaveBeenCalled();
    expect(edited.settled).toHaveBeenCalledOnce();
  });

  it('retains the payload when submission throws', async () => {
    const h = submissionHarness();
    await dispatchFocusComposerPayload(h.submission, async () => {
      throw new Error('known request failure');
    });
    expect(h.clear).not.toHaveBeenCalled();
    expect(h.settled).toHaveBeenCalledOnce();
  });

  it('retires only unsafe attachment chips for the exact pending payload', () => {
    const exact = submissionHarness();
    expect(exact.submission.retainTextOnly()).toBe(true);
    expect(exact.clear).not.toHaveBeenCalled();
    expect(exact.clearAttachments).toHaveBeenCalledOnce();
    expect(exact.settled).toHaveBeenCalledOnce();

    const stale = submissionHarness();
    stale.makeStale();
    expect(stale.submission.retainTextOnly()).toBe(false);
    expect(stale.clear).not.toHaveBeenCalled();
    expect(stale.clearAttachments).not.toHaveBeenCalled();
    expect(stale.settled).toHaveBeenCalledOnce();
  });

  it('conservatively commits when exact attachment cleanup throws', () => {
    const clear = vi.fn();
    const clearAttachments = vi.fn(() => { throw new Error('partial cleanup'); });
    const settled = vi.fn();
    const submission = createComposerSubmission(
      { text: 'keep me', attachments: [{ fileId: 'file-1', kind: 'file' }] },
      () => true,
      clear,
      clearAttachments,
      settled,
    );

    expect(submission.retainTextOnly()).toBe(false);
    expect(clearAttachments).toHaveBeenCalledOnce();
    expect(clear).toHaveBeenCalledOnce();
    expect(settled).toHaveBeenCalledOnce();
    expect(submission.commit()).toBe(true);
    submission.retain();
    expect(clear).toHaveBeenCalledOnce();
    expect(settled).toHaveBeenCalledOnce();
  });

  it('fails closed without unlocking if conservative full cleanup also throws', () => {
    const settled = vi.fn();
    const submission = createComposerSubmission(
      { text: 'keep me', attachments: [{ fileId: 'file-1', kind: 'file' }] },
      () => true,
      () => { throw new Error('full cleanup'); },
      () => { throw new Error('partial cleanup'); },
      settled,
    );

    expect(() => submission.retainTextOnly()).toThrow('full cleanup');
    expect(settled).not.toHaveBeenCalled();
    expect(submission.commit()).toBe(true);
    submission.retain();
    expect(settled).not.toHaveBeenCalled();
  });
});

describe('Composer semantic submission revision', () => {
  function semanticRevisionHarness() {
    const text = ref('A');
    const attachments = ref([{
      localId: 'local-1',
      fileId: 'file-1',
      kind: 'file' as const,
      name: 'one.txt',
      mediaType: 'text/plain',
      size: 3,
      uploading: false,
      error: false,
      previewUrl: '/preview/one',
    }]);
    const sessionId = ref<string | undefined>('scope-a');
    const composerReady = ref(true);
    const revision = useComposerSubmissionRevision({
      text,
      attachments,
      sessionId: () => sessionId.value,
      composerReady: () => composerReady.value,
    });
    return { text, attachments, sessionId, composerReady, revision };
  }

  it('never revives a text or scope owner after an A-B-A transition', () => {
    const text = semanticRevisionHarness();
    const textOwner = text.revision.capture();
    text.text.value = 'B';
    text.text.value = 'A';
    expect(text.revision.isCurrent(textOwner)).toBe(false);

    const scope = semanticRevisionHarness();
    const scopeOwner = scope.revision.capture();
    scope.sessionId.value = 'scope-b';
    scope.sessionId.value = 'scope-a';
    expect(scope.revision.isCurrent(scopeOwner)).toBe(false);

    const readiness = semanticRevisionHarness();
    const readinessOwner = readiness.revision.capture();
    readiness.composerReady.value = false;
    readiness.composerReady.value = true;
    expect(readiness.revision.isCurrent(readinessOwner)).toBe(false);
  });

  it('tracks attachment semantics but ignores preview-only root replacement', () => {
    const h = semanticRevisionHarness();
    const previewOwner = h.revision.capture();
    h.attachments.value = [{
      ...h.attachments.value[0]!,
      previewUrl: '/preview/replaced',
    }];
    expect(h.revision.isCurrent(previewOwner)).toBe(true);

    const attachmentOwner = h.revision.capture();
    h.attachments.value = [];
    h.attachments.value = [{
      localId: 'local-1',
      fileId: 'file-1',
      kind: 'file',
      name: 'one.txt',
      mediaType: 'text/plain',
      size: 3,
      uploading: false,
      error: false,
      previewUrl: '/preview/replaced',
    }];
    expect(h.revision.isCurrent(attachmentOwner)).toBe(false);
  });
});
