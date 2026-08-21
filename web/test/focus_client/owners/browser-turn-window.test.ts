import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  createBrowserTurnWindow,
  DEFAULT_FOCUS_TURN_WINDOW_LIMIT,
  FOCUS_TURN_WINDOW_LIMITS,
  parseFocusTurnWindowLimit,
} from '../../../src/focus/client-state/browser-turn-window';
import { STORAGE_KEYS } from '../../../src/lib/storage';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => Array.from(values.keys())[index] ?? null,
    removeItem: (key) => { values.delete(key); },
    setItem: (key, value) => { values.set(key, String(value)); },
  };
}

let originalStorage: Storage | undefined;

beforeEach(() => {
  originalStorage = (globalThis as { localStorage?: Storage }).localStorage;
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    writable: true,
    value: memoryStorage(),
  });
});

afterEach(() => {
  if (originalStorage === undefined) {
    delete (globalThis as { localStorage?: Storage }).localStorage;
  } else {
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      writable: true,
      value: originalStorage,
    });
  }
});

describe('browser turn window owner', () => {
  it('admits only the finite 5/10/20 product vocabulary', () => {
    expect(FOCUS_TURN_WINDOW_LIMITS).toEqual([5, 10, 20]);
    expect(parseFocusTurnWindowLimit(null)).toBe(DEFAULT_FOCUS_TURN_WINDOW_LIMIT);
    expect(parseFocusTurnWindowLimit('')).toBe(DEFAULT_FOCUS_TURN_WINDOW_LIMIT);
    expect(parseFocusTurnWindowLimit('05')).toBe(DEFAULT_FOCUS_TURN_WINDOW_LIMIT);
    expect(parseFocusTurnWindowLimit('40')).toBe(DEFAULT_FOCUS_TURN_WINDOW_LIMIT);
    expect(parseFocusTurnWindowLimit('20')).toBe(20);
  });

  it('defaults to ten and persists a valid browser-local choice', () => {
    const owner = createBrowserTurnWindow();
    expect(owner.limit.value).toBe(10);
    expect(owner.setLimit(20)).toBe(true);
    expect(owner.limit.value).toBe(20);
    expect(localStorage.getItem(STORAGE_KEYS.turnWindowLimit)).toBe('20');

    const restored = createBrowserTurnWindow();
    expect(restored.limit.value).toBe(20);
  });

  it('does not mutate or persist unsupported and unchanged values', () => {
    const owner = createBrowserTurnWindow();
    expect(owner.setLimit(40)).toBe(false);
    expect(owner.setLimit('5')).toBe(false);
    expect(owner.setLimit(10)).toBe(false);
    expect(owner.limit.value).toBe(10);
    expect(localStorage.getItem(STORAGE_KEYS.turnWindowLimit)).toBeNull();
  });
});
