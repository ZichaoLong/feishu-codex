import { afterEach, beforeEach, vi } from 'vitest';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear() { values.clear(); },
    getItem(key: string) { return values.get(key) ?? null; },
    key(index: number) { return [...values.keys()].at(index) ?? null; },
    removeItem(key: string) { values.delete(key); },
    setItem(key: string, value: string) { values.set(key, value); },
  };
}

export const DOCUMENT_RECEIPT = 'a'.repeat(64);

export const meta = {
  product: 'Focus',
  instance: 'default',
  web_display_name: 'Focus Web',
  csrf_token: 'csrf-1',
  runtime_epoch: 'epoch-1',
  revision: 0,
  default_working_dir: '/work',
  models: [],
  writer_profile: {
    selected_thread_id: '',
    working_dir: '/work',
    scope_generation: 1,
  },
  next_turn_settings: {
    generation: 1,
    model: '',
    reasoning_effort: '',
    approval_policy: 'never',
    permissions_profile_id: ':danger-full-access',
  },
  approval_policies: ['never'],
  permissions_profiles: [{ id: ':danger-full-access', label: 'Full Access' }],
  capabilities: {
    prompt: true,
    new_thread: true,
    interrupt: true,
    approvals: true,
    questions: true,
    markdown: true,
    katex: true,
    mermaid: true,
    file_preview: false,
    terminal: false,
    attachments: false,
    prompt_queue: false,
    durable_event_cursor: false,
    bounded_history: true,
    history_search: false,
    tool_detail: true,
    steer: true,
  },
  unknown_lifecycle_mutations: [],
};

export function registration(
  clientId = 'web-1',
  documentToken = 'document-token-1',
  duplicate = false,
  intentGenerationFloor = 0,
) {
  return {
    client_id: clientId,
    document_token: documentToken,
    document_receipt: DOCUMENT_RECEIPT,
    duplicate,
    intent_generation_floor: intentGenerationFloor,
    csrf_token: 'csrf-1',
    runtime_epoch: 'epoch-1',
    revision: 0,
  };
}

export const bootstrap = {
  authenticated: true,
  csrf_token: 'csrf-1',
  expires_at: 1000,
  runtime_epoch: 'epoch-1',
  revision: 0,
};

export function installFocusApiTestHooks(): void {
  beforeEach(() => {
    const replaceState = vi.fn();
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('history', { state: null, replaceState });
    vi.stubGlobal('window', {
      location: {
        hash: '#token=bootstrap-1',
        pathname: '/',
        search: '',
        protocol: 'http:',
        host: '127.0.0.1:8766',
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });
}
