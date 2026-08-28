import { ref } from 'vue';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { syncFocusDocumentActivityFavicon } from '../src/focus/documentActivityFavicon';

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

afterEach(() => {
  vi.useRealTimers();
});

describe('Focus Web document activity favicon', () => {
  it('uses a fading trail for working frames', () => {
    vi.useFakeTimers();
    const connected = ref(true);
    const working = ref(true);
    const target = faviconTarget();
    const stop = syncFocusDocumentActivityFavicon(
      () => connected.value,
      () => working.value,
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
});
