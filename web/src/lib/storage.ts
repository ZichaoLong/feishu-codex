// apps/kimi-web/src/lib/storage.ts
// Thin, safe wrapper over localStorage: raw read/write/remove plus JSON
// helpers, each guarded with try/catch. No validation, clamping, or enum
// checks here — those stay at call sites. Read helpers return null when the
// key is missing or storage is unavailable, so callers decide their own
// fallback. Centralizes the persisted key strings so each key has a single
// source of truth.

export const STORAGE_KEYS = {
  // Deferred A08-SIM-001 façade preferences; remove with their UI contract.
  permission: 'kimi-web.permission',
  planMode: 'kimi-web.plan-mode',
  swarmMode: 'kimi-web.swarm-mode',
  goalMode: 'kimi-web.goal-mode',
  uiFontSize: 'kimi-web.ui-font-size',
  starredModels: 'kimi-web.starred-models',
  accent: 'kimi-web.accent',
  colorScheme: 'kimi-web.color-scheme',
  hiddenWorkspaces: 'kimi-web.hidden-workspaces',
  collapsedWorkspaces: 'kimi-web.collapsed-workspaces',
  workspaceOrder: 'kimi-web.workspace-order',
  workspaceNameOverrides: 'kimi-web.workspace-name-overrides',
  workspaceSort: 'kimi-web.workspace-sort',
  inputHistory: 'kimi-web.input-history',
  // cross-file
  locale: 'kimi-locale',
  debug: 'kimi-web.debug',
  sidebarCollapsed: 'kimi-web.sidebar-collapsed',
  sidebarWidth: 'kimi-web.sidebar-width',
  detailPanelWidth: 'focus-web.detail-panel-width',
  turnWindowLimit: 'focus-web.turn-window-limit',
  activityFaviconEnabled: 'focus-web.activity-favicon-enabled',
  // Active one-time cleanup for a retired conversation preference.
  contentAlign: 'kimi-web.content-align',
} as const;

/** Per-session composer draft key. */
export function draftStorageKey(sid: string | undefined): string {
  return `kimi-web.draft.${sid && sid.length > 0 ? sid : '__new__'}`;
}

export function safeGetString(key: string): string | null {
  try {
    return globalThis.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function safeSetString(key: string, value: string): void {
  try {
    globalThis.localStorage.setItem(key, value);
  } catch {
    // storage unavailable (private mode, quota, etc.) — ignore
  }
}

export function safeRemove(key: string): void {
  try {
    globalThis.localStorage.removeItem(key);
  } catch {
    // ignore
  }
}

export function safeGetJson<T>(key: string): T | null {
  const raw = safeGetString(key);
  if (raw === null) return null;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function safeSetJson(key: string, value: unknown): void {
  try {
    globalThis.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // ignore
  }
}

/**
 * Collapsed workspace ids in the sidebar. Persisted as a JSON array of ids so
 * the fold state of each workspace group survives a page refresh. There is no
 * server-side source of truth for this UI-only state.
 */
export function loadCollapsedWorkspaces(): string[] {
  const parsed = safeGetJson<unknown>(STORAGE_KEYS.collapsedWorkspaces);
  if (!Array.isArray(parsed)) return [];
  return parsed.filter((id): id is string => typeof id === 'string');
}

export function saveCollapsedWorkspaces(ids: Iterable<string>): void {
  safeSetJson(STORAGE_KEYS.collapsedWorkspaces, Array.from(ids));
}

/**
 * Display order of workspace ids in the sidebar. Persisted as a JSON array so
 * the user can drag workspaces into a custom order that survives a page
 * refresh. There is no server-side source of truth for this UI-only ordering;
 * workspaces absent from the list are treated as "not yet placed" and inserted
 * by the caller (newest first).
 */
export function loadWorkspaceOrder(): string[] {
  const parsed = safeGetJson<unknown>(STORAGE_KEYS.workspaceOrder);
  if (!Array.isArray(parsed)) return [];
  return parsed.filter((id): id is string => typeof id === 'string');
}

export function saveWorkspaceOrder(ids: Iterable<string>): void {
  safeSetJson(STORAGE_KEYS.workspaceOrder, Array.from(ids));
}

/**
 * Local display-name overrides for workspaces the daemon cannot rename — today
 * that is derived workspaces (a cwd with sessions that was never explicitly
 * registered), which `PATCH /workspaces/:id` rejects with 404. Keyed by
 * workspace root (stable across the derived → registered transition) and
 * applied on top of the daemon list so the rename survives a refresh. Cleared
 * once the daemon accepts a rename for that root.
 */
export function loadWorkspaceNameOverrides(): Record<string, string> {
  const parsed = safeGetJson<unknown>(STORAGE_KEYS.workspaceNameOverrides);
  if (!parsed || typeof parsed !== 'object') return {};
  const out: Record<string, string> = {};
  for (const [root, name] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof name === 'string') out[root] = name;
  }
  return out;
}

export function saveWorkspaceNameOverrides(overrides: Record<string, string>): void {
  safeSetJson(STORAGE_KEYS.workspaceNameOverrides, overrides);
}

/**
 * Sidebar workspace sort mode preference (`'manual'` or `'recent'`). Stored as
 * a raw string with no enum check here — the call site narrows it to
 * `WorkspaceSortMode`. Returns null when unset or storage is unavailable.
 */
export function loadWorkspaceSort(): string | null {
  return safeGetString(STORAGE_KEYS.workspaceSort);
}

export function saveWorkspaceSort(mode: string): void {
  safeSetString(STORAGE_KEYS.workspaceSort, mode);
}
