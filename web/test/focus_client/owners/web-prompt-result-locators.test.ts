import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createWebPromptResultLocatorStore } from '../../../src/focus/webPromptResultLocators';

const STORAGE_KEY = 'focus-web.prompt-result-locators';
const MUTATION_ID = '00000000-0000-4000-8000-000000000001';

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

describe('Web prompt result locators', () => {
  it('persists only exact GET coordinates and reloads them without effect authority', () => {
    const first = createWebPromptResultLocatorStore();
    expect(first.remember({ threadId: 'thread-a', mutationId: MUTATION_ID })).toBe('durable');

    const serialized = sessionStorage.getItem(STORAGE_KEY) ?? '';
    expect(JSON.parse(serialized)).toEqual({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-a', mutationId: MUTATION_ID }],
    });
    for (const forbidden of ['text', 'attachment', 'capability', 'payload']) {
      expect(serialized).not.toContain(forbidden);
    }

    const reloaded = createWebPromptResultLocatorStore();
    expect(reloaded.list()).toEqual([{ threadId: 'thread-a', mutationId: MUTATION_ID }]);
    expect(reloaded.forget({ threadId: 'thread-a', mutationId: MUTATION_ID })).toBe('durable');
    expect(sessionStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('fails closed on malformed storage and keeps a memory locator after a failed durable write', () => {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify({
      schemaVersion: 1,
      locators: [{ threadId: 'thread-a', mutationId: 'not-a-uuid' }],
    }));
    expect(createWebPromptResultLocatorStore().list()).toEqual([]);

    const unavailable = memoryStorage();
    unavailable.setItem = () => { throw new Error('storage unavailable'); };
    vi.stubGlobal('sessionStorage', unavailable);
    const store = createWebPromptResultLocatorStore();
    expect(store.remember({ threadId: 'thread-a', mutationId: MUTATION_ID })).toBe('memory_only');
    expect(store.list()).toEqual([{ threadId: 'thread-a', mutationId: MUTATION_ID }]);
  });

  it('deduplicates exact identities and remains bounded', () => {
    const store = createWebPromptResultLocatorStore();
    expect(store.remember({ threadId: 'thread-a', mutationId: MUTATION_ID })).toBe('durable');
    expect(store.remember({ threadId: 'thread-a', mutationId: MUTATION_ID })).toBe('durable');
    for (let index = 2; index <= 40; index += 1) {
      const suffix = index.toString(16).padStart(12, '0');
      expect(store.remember({
        threadId: 'thread-a',
        mutationId: `00000000-0000-4000-8000-${suffix}`,
      })).toBe('durable');
    }
    expect(store.list()).toHaveLength(32);
    expect(store.list().some((item) => item.mutationId === MUTATION_ID)).toBe(false);
  });
});
