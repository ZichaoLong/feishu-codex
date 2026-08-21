import { ref } from 'vue';
import { describe, expect, it } from 'vitest';
import { createRuntimeNoticeOwner } from '../../../src/focus/client-state/runtime-notices';
import type { FocusProjectionEvent } from '../../../src/focus/types';

function event(
  revision: number,
  detail: Record<string, unknown>,
  threadId = 'thread-a',
  runtimeEpoch = 'epoch-1',
): FocusProjectionEvent {
  return {
    type: 'runtime_notice',
    runtime_epoch: runtimeEpoch,
    revision,
    ...(threadId ? { thread_id: threadId } : {}),
    detail,
  };
}

function warning(
  revision: number,
  message: string,
  threadId = 'thread-a',
  runtimeEpoch = 'epoch-1',
) {
  return event(revision, { method: 'warning', message }, threadId, runtimeEpoch);
}

function error(
  revision: number,
  message: string,
  willRetry: boolean,
  threadId = 'thread-a',
  turnId = 'turn-1',
) {
  return event(revision, {
    method: 'error',
    message,
    additional_details: `${message} details`,
    will_retry: willRetry,
    turn_id: turnId,
  }, threadId);
}

describe('RuntimeNoticeOwner', () => {
  it('replaces retry status for the same target without mixing it into connection state', () => {
    const owner = createRuntimeNoticeOwner(ref('thread-a'));
    owner.handleEvent(error(1, 'first retry', true));
    owner.handleEvent(error(2, 'second retry', true));

    expect(owner.presentation.value).toEqual({
      retry: expect.objectContaining({
        message: 'second retry',
        additionalDetails: 'second retry details',
        willRetry: true,
      }),
      notices: [],
    });
  });

  it('keeps only the latest five warning and non-retry error notices', () => {
    const owner = createRuntimeNoticeOwner(ref('thread-a'));
    for (let revision = 1; revision <= 6; revision += 1) {
      owner.handleEvent(warning(revision, `warning-${revision}`));
    }
    owner.handleEvent(error(7, 'terminal error', false));

    expect(owner.presentation.value.notices.map((item) => item.message)).toEqual([
      'warning-3',
      'warning-4',
      'warning-5',
      'warning-6',
      'terminal error',
    ]);
  });

  it('presents global notices with only the currently selected thread notices', () => {
    const selectedThreadId = ref('thread-a');
    const owner = createRuntimeNoticeOwner(selectedThreadId);
    owner.handleEvent(warning(1, 'global', ''));
    owner.handleEvent(warning(2, 'for-a', 'thread-a'));
    owner.handleEvent(warning(3, 'for-b', 'thread-b'));

    expect(owner.presentation.value.notices.map((item) => item.message)).toEqual([
      'global', 'for-a',
    ]);
    selectedThreadId.value = 'thread-b';
    expect(owner.presentation.value.notices.map((item) => item.message)).toEqual([
      'global', 'for-b',
    ]);
  });

  it('clears matching retry status on later turn progress or an epoch boundary', () => {
    const owner = createRuntimeNoticeOwner(ref('thread-a'));
    owner.handleEvent(error(1, 'retrying', true));
    owner.handleEvent({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-a',
      detail: { method: 'item/agentMessage/delta', turn_id: 'turn-1' },
    });
    expect(owner.presentation.value.retry).toBeNull();

    owner.handleEvent(warning(3, 'old warning'));
    owner.handleEvent(warning(1, 'new epoch warning', 'thread-a', 'epoch-2'));
    expect(owner.presentation.value.notices.map((item) => item.message)).toEqual([
      'new epoch warning',
    ]);
  });

  it('ignores duplicate revisions and malformed details', () => {
    const owner = createRuntimeNoticeOwner(ref('thread-a'));
    owner.handleEvent(warning(1, 'kept'));
    owner.handleEvent(warning(1, 'duplicate'));
    owner.handleEvent(event(2, {
      method: 'error',
      message: 'malformed',
      additional_details: '',
      will_retry: 'yes',
      turn_id: 'turn-1',
    }));

    expect(owner.presentation.value.notices.map((item) => item.message)).toEqual(['kept']);
  });
});
