import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ref } from 'vue';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createFocusDocumentActivityFaviconPreference,
  syncFocusDocumentActivityFavicon,
} from '../src/focus/documentActivityFavicon';
import { STORAGE_KEYS } from '../src/lib/storage';

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

function faviconTarget(hidden = false) {
  let currentHref = 'focus-idle.svg';
  let isHidden = hidden;
  return {
    idleHref: currentHref,
    isHidden: () => isHidden,
    setHidden: (value: boolean) => {
      isHidden = value;
    },
    setHref: (href: string) => {
      currentHref = href;
    },
    href: () => currentHref,
  };
}

function decodeSvgDataUrl(href: string): string {
  const prefix = 'data:image/svg+xml,';
  if (!href.startsWith(prefix)) throw new Error('expected an SVG data URL');
  return decodeURIComponent(href.slice(prefix.length));
}

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

beforeEach(() => {
  vi.stubGlobal('localStorage', memoryStorage());
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('Focus Web document activity favicon', () => {
  it('defaults to enabled and restores only the exact disabled marker', () => {
    expect(createFocusDocumentActivityFaviconPreference().enabled.value).toBe(true);

    localStorage.setItem(STORAGE_KEYS.activityFaviconEnabled, '1');
    expect(createFocusDocumentActivityFaviconPreference().enabled.value).toBe(true);

    localStorage.setItem(STORAGE_KEYS.activityFaviconEnabled, '0');
    expect(createFocusDocumentActivityFaviconPreference().enabled.value).toBe(false);
  });

  it('stores only a disabled marker and removes it when re-enabled', () => {
    const preference = createFocusDocumentActivityFaviconPreference();
    const setItem = vi.spyOn(localStorage, 'setItem');
    const removeItem = vi.spyOn(localStorage, 'removeItem');

    expect(preference.setEnabled(true)).toBe(false);
    expect(preference.setEnabled('disabled')).toBe(false);
    expect(setItem).not.toHaveBeenCalled();
    expect(removeItem).not.toHaveBeenCalled();

    expect(preference.setEnabled(false)).toBe(true);
    expect(preference.enabled.value).toBe(false);
    expect(localStorage.getItem(STORAGE_KEYS.activityFaviconEnabled)).toBe('0');
    expect(preference.setEnabled(false)).toBe(false);

    expect(preference.setEnabled(true)).toBe(true);
    expect(preference.enabled.value).toBe(true);
    expect(localStorage.getItem(STORAGE_KEYS.activityFaviconEnabled)).toBeNull();
  });

  it('uses a fading trail for working frames', () => {
    vi.useFakeTimers();
    const connected = ref(true);
    const working = ref(true);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => true,
      target,
    );

    const svg = decodeSvgDataUrl(target.href());
    expect(svg).toContain('<linearGradient id="focus-working-tail"');
    expect(svg).toContain('stop-opacity="0"');
    expect(svg).toContain('stop-opacity=".75"');
    expect(svg).toContain('stroke="url(#focus-working-tail)"');

    stop();
  });

  it('animates only while the current thread is working and restores idle', () => {
    vi.useFakeTimers();
    const connected = ref(true);
    const working = ref(false);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => true,
      target,
    );

    expect(target.href()).toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    working.value = true;
    const firstFrame = target.href();
    expect(firstFrame).not.toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(1);

    vi.advanceTimersByTime(41);
    expect(target.href()).toBe(firstFrame);
    vi.advanceTimersByTime(1);
    expect(target.href()).not.toBe(firstFrame);

    working.value = false;
    expect(target.href()).toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    stop();
    expect(target.href()).toBe('focus-idle.svg');
  });

  it('shows a stable disconnected icon instead of stale working activity', () => {
    vi.useFakeTimers();
    const connected = ref(false);
    const working = ref(true);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => true,
      target,
    );

    const disconnectedHref = target.href();
    expect(disconnectedHref).not.toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    vi.advanceTimersByTime(2_000);
    expect(target.href()).toBe(disconnectedHref);

    connected.value = true;
    expect(target.href()).not.toBe(disconnectedHref);
    expect(vi.getTimerCount()).toBe(1);

    connected.value = false;
    expect(target.href()).toBe(disconnectedHref);
    expect(vi.getTimerCount()).toBe(0);

    stop();
    expect(target.href()).toBe('focus-idle.svg');
  });

  it('uses a slower cadence while the browser document is hidden', () => {
    vi.useFakeTimers();
    const connected = ref(true);
    const working = ref(true);
    const target = faviconTarget(true);
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => true,
      target,
    );

    const hiddenFrame = target.href();
    vi.advanceTimersByTime(82);
    expect(target.href()).toBe(hiddenFrame);
    vi.advanceTimersByTime(1);
    const secondHiddenFrame = target.href();
    expect(secondHiddenFrame).not.toBe(hiddenFrame);

    working.value = false;
    target.setHidden(false);
    working.value = true;
    const visibleFrame = target.href();
    expect(visibleFrame).toBe(hiddenFrame);
    vi.advanceTimersByTime(41);
    expect(target.href()).toBe(visibleFrame);
    vi.advanceTimersByTime(1);
    expect(target.href()).not.toBe(visibleFrame);
    vi.advanceTimersByTime(42);
    expect(target.href()).toBe(secondHiddenFrame);

    stop();
  });

  it('stops immediately while disabled and resumes from the current state', () => {
    vi.useFakeTimers();
    const connected = ref(true);
    const working = ref(true);
    const enabled = ref(true);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => enabled.value,
      target,
    );

    expect(target.href()).not.toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(1);

    enabled.value = false;
    expect(target.href()).toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    enabled.value = true;
    expect(target.href()).not.toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(1);

    stop();
  });

  it('keeps the normal Focus icon while disabled even when disconnected', () => {
    vi.useFakeTimers();
    const connected = ref(false);
    const working = ref(true);
    const enabled = ref(false);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
      () => enabled.value,
      target,
    );

    expect(target.href()).toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    enabled.value = true;
    const disconnectedHref = target.href();
    expect(disconnectedHref).not.toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    enabled.value = false;
    expect(target.href()).toBe('focus-idle.svg');
    expect(vi.getTimerCount()).toBe(0);

    stop();
  });

  it('wires the browser-local preference through the settings surface', () => {
    const app = source('../src/focus/FocusApp.vue');
    const dialog = source('../src/focus/FocusSettingsDialog.vue');
    const en = source('../src/i18n/locales/en/focus.ts');
    const zh = source('../src/i18n/locales/zh/focus.ts');

    expect(dialog).toContain('activityFaviconEnabled: boolean;');
    expect(dialog).toContain('setActivityFaviconEnabled: [value: boolean]');
    expect(dialog).toContain("emit('setActivityFaviconEnabled', true)");
    expect(dialog).toContain("emit('setActivityFaviconEnabled', false)");
    expect(app).toContain(
      ':activity-favicon-enabled="activityFaviconPreference.enabled.value"',
    );
    expect(app).toContain(
      '@set-activity-favicon-enabled="activityFaviconPreference.setEnabled($event)"',
    );
    expect(en).toContain('activityFaviconDescription:');
    expect(zh).toContain('activityFaviconDescription:');
  });
});
