import {
  readonly,
  ref,
  watch,
  type Ref,
  type WatchSource,
  type WatchStopHandle,
} from 'vue';
import {
  safeGetString,
  safeRemove,
  safeSetString,
  STORAGE_KEYS,
} from '../lib/storage';

const VISIBLE_FRAME_INTERVAL_MS = 42;
const HIDDEN_FRAME_INTERVAL_MS = 83;
const HIDDEN_FRAME_STEP = 2;
const WORKING_FRAME_COUNT = 24;

interface DocumentActivityFaviconTarget {
  readonly idleHref: string;
  isHidden(): boolean;
  setHref(href: string): void;
}

export interface FocusDocumentActivityFaviconPreference {
  readonly enabled: Readonly<Ref<boolean>>;
  setEnabled(value: unknown): boolean;
}

/** Own one browser document's local activity-favicon preference. */
export function createFocusDocumentActivityFaviconPreference(): FocusDocumentActivityFaviconPreference {
  const enabled = ref(
    safeGetString(STORAGE_KEYS.activityFaviconEnabled) !== '0',
  );

  function setEnabled(value: unknown): boolean {
    if (typeof value !== 'boolean' || enabled.value === value) return false;
    enabled.value = value;
    if (value) safeRemove(STORAGE_KEYS.activityFaviconEnabled);
    else safeSetString(STORAGE_KEYS.activityFaviconEnabled, '0');
    return true;
  }

  return {
    enabled: readonly(enabled),
    setEnabled,
  };
}

function svgDataUrl(svg: string): string {
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

const WORKING_HREFS = Object.freeze(Array.from(
  { length: WORKING_FRAME_COUNT },
  (_unused, index) => svgDataUrl(
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">`
    + '<defs>'
    + '<linearGradient id="focus-working-tail" x1="0%" y1="100%" x2="100%" y2="0%">'
    + '<stop offset="0%" stop-color="#1783ff" stop-opacity="0"/>'
    + '<stop offset="28%" stop-color="#1783ff" stop-opacity=".12"/>'
    + '<stop offset="58%" stop-color="#1783ff" stop-opacity=".4"/>'
    + '<stop offset="82%" stop-color="#1783ff" stop-opacity=".75"/>'
    + '<stop offset="100%" stop-color="#1783ff"/>'
    + '</linearGradient>'
    + '</defs>'
    + '<circle cx="32" cy="32" r="24" fill="none" stroke="#dbeafe" stroke-width="8"/>'
    + `<g transform="rotate(${index * 15} 32 32)">`
    + '<path d="M11.22 44A24 24 0 0 1 32 8" fill="none" stroke="url(#focus-working-tail)" stroke-width="8" stroke-linecap="round"/>'
    + '</g>'
    + '</svg>',
  ),
));

const DISCONNECTED_HREF = svgDataUrl(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
  + '<circle cx="32" cy="32" r="24" fill="none" stroke="#6b7280" stroke-width="8"/>'
  + '<path d="M15 49L49 15" fill="none" stroke="#6b7280" stroke-width="8" stroke-linecap="round"/>'
  + '</svg>',
);

function documentActivityFaviconTarget(
  browserDocument: Document,
): DocumentActivityFaviconTarget {
  let link = browserDocument.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  if (!link) {
    link = browserDocument.createElement('link');
    link.rel = 'icon';
    browserDocument.head.append(link);
  }
  const idleHref = link.getAttribute('href') ?? '';
  return {
    idleHref,
    isHidden: () => browserDocument.visibilityState === 'hidden',
    setHref: (href) => {
      if (href) link.setAttribute('href', href);
      else link.removeAttribute('href');
    },
  };
}

export function syncFocusDocumentActivityFavicon(
  connected: WatchSource<boolean>,
  working: WatchSource<boolean>,
  enabled: WatchSource<boolean>,
  target: DocumentActivityFaviconTarget = documentActivityFaviconTarget(document),
): WatchStopHandle {
  let frameIndex = 0;
  let frameTimer: ReturnType<typeof setTimeout> | null = null;

  function stopAnimation(): void {
    if (frameTimer === null) return;
    clearTimeout(frameTimer);
    frameTimer = null;
  }

  function restoreIdle(): void {
    target.setHref(target.idleHref);
  }

  function showWorkingFrame(): void {
    const hidden = target.isHidden();
    target.setHref(WORKING_HREFS[frameIndex]!);
    frameIndex = (
      frameIndex + (hidden ? HIDDEN_FRAME_STEP : 1)
    ) % WORKING_HREFS.length;
    frameTimer = setTimeout(
      showWorkingFrame,
      hidden ? HIDDEN_FRAME_INTERVAL_MS : VISIBLE_FRAME_INTERVAL_MS,
    );
  }

  return watch(
    [connected, working, enabled],
    ([isConnected, isWorking, isEnabled], _previous, onCleanup) => {
      stopAnimation();
      if (!isEnabled) {
        restoreIdle();
      } else if (!isConnected) {
        target.setHref(DISCONNECTED_HREF);
      } else if (isWorking) {
        frameIndex = 0;
        showWorkingFrame();
      } else {
        restoreIdle();
      }
      onCleanup(() => {
        stopAnimation();
        restoreIdle();
      });
    },
    { flush: 'sync', immediate: true },
  );
}
