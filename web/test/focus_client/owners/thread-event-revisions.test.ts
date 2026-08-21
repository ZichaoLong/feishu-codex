import { describe, expect, it } from 'vitest';
import type { FocusCoordinates } from '../../../src/focus/types';
import { ThreadEventRevisionIndex } from '../../../src/focus/client-state/thread-event-revisions';

describe('ThreadEventRevisionIndex', () => {
  it('is monotonic within an epoch and never compares coordinates across epochs', () => {
    const index = new ThreadEventRevisionIndex();
    index.observe('thread-1', { runtime_epoch: 'epoch-a', revision: 4 });
    index.observe('thread-1', { runtime_epoch: 'epoch-a', revision: 2 });

    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-a', revision: 3 })).toBe(true);
    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-a', revision: 4 })).toBe(false);
    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-b', revision: 0 })).toBe(false);

    index.observe('thread-1', { runtime_epoch: 'epoch-b', revision: 1 });
    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-a', revision: 0 })).toBe(false);
    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-b', revision: 0 })).toBe(true);
    index.clear();
    expect(index.hasNewerEvent('thread-1', { runtime_epoch: 'epoch-b', revision: 0 })).toBe(false);
  });

  it('projects only coordinates instead of retaining an event payload', () => {
    const index = new ThreadEventRevisionIndex();
    const event = {
      runtime_epoch: 'epoch-a',
      revision: 4,
    } as FocusCoordinates & { detail?: unknown };
    Object.defineProperty(event, 'detail', {
      enumerable: true,
      get() {
        throw new Error('event payload must not be read or copied');
      },
    });

    expect(() => index.observe('thread-1', event)).not.toThrow();
    expect(index.hasNewerEvent('thread-1', {
      runtime_epoch: 'epoch-a',
      revision: 3,
    })).toBe(true);
  });
});
