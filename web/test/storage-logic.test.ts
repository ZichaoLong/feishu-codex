import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  loadCollapsedWorkspaces,
  loadWorkspaceOrder,
  saveCollapsedWorkspaces,
  saveWorkspaceOrder,
  STORAGE_KEYS,
  draftStorageKey,
  safeGetJson,
  safeGetString,
  safeRemove,
  safeSetJson,
  safeSetString,
} from '../src/lib/storage';

function createMemoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear() {
      data.clear();
    },
    getItem(key: string) {
      return data.get(key) ?? null;
    },
    key(index: number) {
      return Array.from(data.keys()).at(index) ?? null;
    },
    removeItem(key: string) {
      data.delete(key);
    },
    setItem(key: string, value: string) {
      data.set(key, String(value));
    },
  };
}

function installStorage(storage: Storage): void {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: storage,
  });
}

let backing: Storage;

beforeEach(() => {
  backing = createMemoryStorage();
  installStorage(backing);
});

afterEach(() => {
  installStorage(createMemoryStorage());
});

describe('safeGetString / safeSetString', () => {
  it('round-trips a value', () => {
    safeSetString('k', 'hello');
    expect(safeGetString('k')).toBe('hello');
  });

  it('returns null for a missing key', () => {
    expect(safeGetString('missing')).toBeNull();
  });

  it('overwrites an existing value', () => {
    safeSetString('k', 'a');
    safeSetString('k', 'b');
    expect(safeGetString('k')).toBe('b');
  });
});

describe('safeRemove', () => {
  it('removes an existing key', () => {
    safeSetString('k', 'v');
    safeRemove('k');
    expect(safeGetString('k')).toBeNull();
  });

  it('is a no-op for a missing key', () => {
    expect(() => safeRemove('missing')).not.toThrow();
  });
});

describe('safeGetJson / safeSetJson', () => {
  it('round-trips a JSON value', () => {
    safeSetJson('k', { a: 1, b: [2, 3] });
    expect(safeGetJson('k')).toEqual({ a: 1, b: [2, 3] });
  });

  it('returns null for a missing key', () => {
    expect(safeGetJson('missing')).toBeNull();
  });

  it('returns null when the stored value is not valid JSON', () => {
    safeSetString('k', '{not json');
    expect(safeGetJson('k')).toBeNull();
  });
});

describe('error swallowing', () => {
  it('safeGetString returns null when storage throws', () => {
    const throwing = createMemoryStorage();
    throwing.getItem = () => {
      throw new Error('denied');
    };
    installStorage(throwing);
    expect(safeGetString('k')).toBeNull();
  });

  it('safeSetString does not throw when storage throws', () => {
    const throwing = createMemoryStorage();
    throwing.setItem = () => {
      throw new Error('quota');
    };
    installStorage(throwing);
    expect(() => safeSetString('k', 'v')).not.toThrow();
  });
});

describe('draftStorageKey', () => {
  it('uses the session id when present', () => {
    expect(draftStorageKey('abc')).toBe('kimi-web.draft.abc');
  });

  it('falls back to __new__ when sid is empty/undefined', () => {
    expect(draftStorageKey(undefined)).toBe('kimi-web.draft.__new__');
    expect(draftStorageKey('')).toBe('kimi-web.draft.__new__');
  });
});

describe('STORAGE_KEYS', () => {
  it('keeps active and explicitly deferred key strings unchanged', () => {
    expect(STORAGE_KEYS.locale).toBe('kimi-locale');
    expect(STORAGE_KEYS.collapsedWorkspaces).toBe('kimi-web.collapsed-workspaces');
    expect(STORAGE_KEYS.workspaceOrder).toBe('kimi-web.workspace-order');
  });
});

describe('loadCollapsedWorkspaces / saveCollapsedWorkspaces', () => {
  it('returns an empty array when the key is missing', () => {
    expect(loadCollapsedWorkspaces()).toEqual([]);
  });

  it('round-trips the collapsed ids', () => {
    saveCollapsedWorkspaces(['ws-1', 'ws-2']);
    expect(loadCollapsedWorkspaces()).toEqual(['ws-1', 'ws-2']);
  });

  it('accepts any iterable of ids', () => {
    saveCollapsedWorkspaces(new Set(['ws-1', 'ws-3']));
    expect(loadCollapsedWorkspaces()).toEqual(['ws-1', 'ws-3']);
  });

  it('drops non-string entries and returns [] for malformed values', () => {
    safeSetString(STORAGE_KEYS.collapsedWorkspaces, JSON.stringify(['ws-1', 2, null, 'ws-2']));
    expect(loadCollapsedWorkspaces()).toEqual(['ws-1', 'ws-2']);

    safeSetString(STORAGE_KEYS.collapsedWorkspaces, JSON.stringify({ ws: true }));
    expect(loadCollapsedWorkspaces()).toEqual([]);
  });
});

describe('loadWorkspaceOrder / saveWorkspaceOrder', () => {
  it('returns an empty array when the key is missing', () => {
    expect(loadWorkspaceOrder()).toEqual([]);
  });

  it('round-trips the ordered ids', () => {
    saveWorkspaceOrder(['ws-2', 'ws-1']);
    expect(loadWorkspaceOrder()).toEqual(['ws-2', 'ws-1']);
  });

  it('accepts any iterable of ids', () => {
    saveWorkspaceOrder(new Set(['ws-3', 'ws-1']));
    expect(loadWorkspaceOrder()).toEqual(['ws-3', 'ws-1']);
  });

  it('drops non-string entries and returns [] for malformed values', () => {
    safeSetString(STORAGE_KEYS.workspaceOrder, JSON.stringify(['ws-1', 2, null]));
    expect(loadWorkspaceOrder()).toEqual(['ws-1']);

    safeSetString(STORAGE_KEYS.workspaceOrder, JSON.stringify({ ws: true }));
    expect(loadWorkspaceOrder()).toEqual([]);
  });
});
