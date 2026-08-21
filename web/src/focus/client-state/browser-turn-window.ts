import { readonly, ref, type Ref } from 'vue';
import { safeGetString, safeSetString, STORAGE_KEYS } from '../../lib/storage';

export const FOCUS_TURN_WINDOW_LIMITS = [5, 10, 20] as const;
export type FocusTurnWindowLimit = (typeof FOCUS_TURN_WINDOW_LIMITS)[number];
export const DEFAULT_FOCUS_TURN_WINDOW_LIMIT: FocusTurnWindowLimit = 10;

export function isFocusTurnWindowLimit(value: unknown): value is FocusTurnWindowLimit {
  return typeof value === 'number'
    && FOCUS_TURN_WINDOW_LIMITS.some((candidate) => candidate === value);
}

export function parseFocusTurnWindowLimit(value: unknown): FocusTurnWindowLimit {
  if (value !== '5' && value !== '10' && value !== '20') {
    return DEFAULT_FOCUS_TURN_WINDOW_LIMIT;
  }
  const parsed = Number(value);
  return isFocusTurnWindowLimit(parsed) ? parsed : DEFAULT_FOCUS_TURN_WINDOW_LIMIT;
}

export interface BrowserTurnWindowOwner {
  readonly limit: Readonly<Ref<FocusTurnWindowLimit>>;
  setLimit(value: unknown): boolean;
}

/** Own one browser document's bounded recent/history full-turn preference. */
export function createBrowserTurnWindow(): BrowserTurnWindowOwner {
  const limit = ref<FocusTurnWindowLimit>(parseFocusTurnWindowLimit(
    safeGetString(STORAGE_KEYS.turnWindowLimit),
  ));

  function setLimit(value: unknown): boolean {
    if (!isFocusTurnWindowLimit(value) || limit.value === value) return false;
    limit.value = value;
    safeSetString(STORAGE_KEYS.turnWindowLimit, String(value));
    return true;
  }

  return {
    limit: readonly(limit),
    setLimit,
  };
}
