import {
  isCanonicalWebMutationId,
} from './webPromptMutation';

const STORAGE_KEY = 'focus-web.prompt-result-locators';
const STORE_VERSION = 1;
const MAX_LOCATORS = 32;

export interface WebPromptResultLocator {
  threadId: string;
  mutationId: string;
}

export type WebPromptResultLocatorDurability = 'durable' | 'memory_only';

function isExactString(value: unknown): value is string {
  return typeof value === 'string' && value !== '' && value === value.trim();
}

function locatorKey(locator: WebPromptResultLocator): string {
  return `${locator.threadId}\u0000${locator.mutationId}`;
}

function decodeLocators(raw: string): WebPromptResultLocator[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return [];
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return [];
  const envelope = parsed as { schemaVersion?: unknown; locators?: unknown };
  if (envelope.schemaVersion !== STORE_VERSION
    || !Array.isArray(envelope.locators)
    || envelope.locators.length > MAX_LOCATORS) return [];
  const decoded: WebPromptResultLocator[] = [];
  const keys = new Set<string>();
  for (const value of envelope.locators) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
    const candidate = value as Record<string, unknown>;
    if (Object.keys(candidate).length !== 2
      || !isExactString(candidate.threadId)
      || !isCanonicalWebMutationId(candidate.mutationId)) return [];
    const locator = {
      threadId: candidate.threadId,
      mutationId: candidate.mutationId,
    };
    const key = locatorKey(locator);
    if (keys.has(key)) return [];
    keys.add(key);
    decoded.push(locator);
  }
  return decoded;
}

function readLocators(): WebPromptResultLocator[] {
  try {
    return decodeLocators(sessionStorage.getItem(STORAGE_KEY) ?? '');
  } catch {
    return [];
  }
}

function persistLocators(locators: readonly WebPromptResultLocator[]): boolean {
  const serialized = JSON.stringify({ schemaVersion: STORE_VERSION, locators });
  try {
    if (locators.length === 0) sessionStorage.removeItem(STORAGE_KEY);
    else sessionStorage.setItem(STORAGE_KEY, serialized);
    const stored = sessionStorage.getItem(STORAGE_KEY);
    return locators.length === 0
      ? stored === null
      : stored === serialized;
  } catch {
    return false;
  }
}

/**
 * Own only GET locators. No prompt text, attachments, capability, or replay
 * authority crosses a document reload through this store.
 */
export function createWebPromptResultLocatorStore() {
  let locators = readLocators();

  return {
    list(): readonly WebPromptResultLocator[] {
      return locators.map((locator) => ({ ...locator }));
    },
    remember(locator: WebPromptResultLocator): WebPromptResultLocatorDurability {
      const key = locatorKey(locator);
      const next = [
        ...locators.filter((candidate) => locatorKey(candidate) !== key),
        { ...locator },
      ].slice(-MAX_LOCATORS);
      locators = next;
      return persistLocators(next) ? 'durable' : 'memory_only';
    },
    forget(locator: WebPromptResultLocator): WebPromptResultLocatorDurability {
      const key = locatorKey(locator);
      const next = locators.filter((candidate) => locatorKey(candidate) !== key);
      if (next.length === locators.length) return 'durable';
      locators = next;
      return persistLocators(next) ? 'durable' : 'memory_only';
    },
  };
}
