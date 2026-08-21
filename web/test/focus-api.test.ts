import { describe, expect, it, vi } from 'vitest';
import { FocusWebApi } from '../src/focus/api';
import {
  bootstrap,
  DOCUMENT_RECEIPT,
  installFocusApiTestHooks,
  meta,
  registration,
} from './focus-api-test-support';
import {
  decodeFocusOperatorStatus,
  decodeFocusProjectionEvent,
} from '../src/focus/projectionEventDecoder';
import {
  decodeFocusAttachmentUpload,
  decodeFocusBootstrapResult,
  decodeFocusDocumentRegistration,
  decodeFocusGoalResult,
  decodeFocusLifecycleResult,
  decodeFocusLifecycleVerificationResult,
  decodeFocusMeta,
  decodeFocusMutationResult,
  decodeFocusNextTurnSettingsResult,
  decodeFocusRenameResult,
  decodeFocusRequestResponseResult,
  decodeFocusThreadList,
  decodeFocusThreadConversationSearchPage,
  decodeFocusThreadSnapshot,
  decodeFocusThreadToolDetailScanPage,
  decodeFocusTurnPage,
  decodeFocusWriterProfileResult,
} from '../src/focus/httpResponseDecoder';


const wireThread = {
  id: 'thread-1',
  title: 'Demo',
  name: 'Demo',
  preview: 'hello',
  cwd: '/work',
  created_at: 1,
  updated_at: 2,
  source: 'appServer',
  status: 'idle',
  active_flags: [],
  model_provider: '',
  service_name: 'focus',
  session_id: '',
  parent_thread_id: '',
  can_accept_direct_input: true,
  thread_source: '',
  ephemeral: false,
  agent_nickname: '',
  agent_role: '',
  subagent_kind: '',
  owner: {
    kind: 'none',
    holder_id: '',
    relation: 'none',
    label: 'No active writer',
  },
  pending_interaction: 'none',
  loaded_instance: '',
  loaded_state_verified: true,
  observed_here: false,
  selectable: true,
  unavailable_reason: '',
  history_mode: 'paginated',
  action_capabilities: {
    rename: true,
    archive: true,
    unarchive: false,
    delete: false,
    compact: true,
    fork: false,
    export: false,
    review: true,
    goal: true,
  },
};

const wireSnapshot = {
  runtime_epoch: 'epoch-1',
  revision: 0,
  thread: wireThread,
  turns: [],
  active_turn_id: '',
  active_turn_status: '',
  active_turn_context: null,
  pending_requests: [],
  tasks: [],
  older_turn_cursor: '',
  has_more_turns: false,
  goal: null,
  token_usage: null,
  token_usage_available: false,
  mutation_unknown: null,
  selection_scope: {
    writer_profile: { ...meta.writer_profile, selected_thread_id: 'thread-1' },
    scope_changed: false,
    previous_attachment_scope: '',
    current_attachment_scope: 'thread:thread-1',
    previous_scope_generation: 1,
    current_scope_generation: 1,
    attachment_scope_disposition: 'unchanged',
  },
};

const wireActiveTurnContext = {
  turn_id: 'turn-1',
  initiator: { kind: 'feishu', binding_id: 'chat:group-1' },
  feishu_audience: ['chat:group-1', 'chat:group-2'],
  settings: {
    model: { value: 'gpt-active', source: 'active_reroute' },
    reasoning_effort: { value: 'medium', source: 'inherited' },
    approval_policy: { value: '', source: 'unknown' },
    permissions_profile_id: { value: ':workspace', source: 'inherited' },
  },
};

installFocusApiTestHooks();

describe('FocusWebApi bootstrap', () => {
  it('reads and partially updates instance next-turn settings without navigation intent', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string, options?: RequestInit) => {
      let payload: unknown = meta;
      if (path === '/api/client/register') payload = registration();
      else if (path === '/api/settings/next-turn') {
        payload = {
          runtime_epoch: 'epoch-1',
          revision: 2,
          next_turn_settings: {
            ...meta.next_turn_settings,
            generation: options?.method === 'POST' ? 2 : 1,
            model: options?.method === 'POST' ? 'gpt-test' : '',
          },
        };
      }
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await api.readNextTurnSettings();
    await api.updateNextTurnSettings({ model: 'gpt-test' });

    const settingsCalls = fetchMock.mock.calls.filter(
      ([path]) => path === '/api/settings/next-turn',
    );
    expect(settingsCalls).toHaveLength(2);
    expect(settingsCalls[0]?.[1]).toMatchObject({
      method: 'GET',
      headers: {
        'X-Focus-Web-Client': 'web-1',
        'X-Focus-Web-Document': 'document-token-1',
      },
    });
    expect(settingsCalls[1]?.[1]).toMatchObject({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Focus-Web-Client': 'web-1',
        'X-Focus-Web-Document': 'document-token-1',
        'X-Focus-Web-Csrf': 'csrf-1',
      },
      body: JSON.stringify({ model: 'gpt-test' }),
    });
    expect(settingsCalls[1]?.[1]?.headers).not.toHaveProperty('X-Focus-Web-Intent');
  });

  it('sends only the required exact turn id in the interrupt body', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      const payload = path === '/api/client/register'
        ? registration()
        : path === '/api/meta'
          ? meta
          : {
              accepted: true,
              thread_id: 'thread/one',
              turn_id: 'turn/exact',
            };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await api.interrupt('thread/one', 'turn/exact');

    const interruptCall = fetchMock.mock.calls.find(
      ([path]) => path === '/api/threads/thread%2Fone/interrupt',
    );
    expect(interruptCall?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ turn_id: 'turn/exact' }),
    });
  });

  it('round-trips the exact response generation and service capability', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      const payload = path === '/api/client/register'
        ? registration()
        : path === '/api/meta'
          ? meta
          : { accepted: true };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await api.respondRequest(
      'request-1',
      7,
      'service-capability-7',
      'approve_once',
      { q1: 'yes' },
    );

    const mutation = fetchMock.mock.calls.find(
      ([path]) => path === '/api/requests/request-1/respond',
    );
    expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
      action: 'approve_once',
      answers: { q1: 'yes' },
      connection_generation: 7,
      response_capability: 'service-capability-7',
    });
  });

  it('aborts a stalled operator-status request after its five-second deadline', async () => {
    window.location.hash = '';
    let operatorSignal: AbortSignal | undefined;
    const fetchMock = vi.fn((path: string, options?: RequestInit): Promise<Response> => {
      if (path === '/api/client/register') {
        return Promise.resolve(new Response(JSON.stringify(registration()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }));
      }
      if (path === '/api/meta') {
        return Promise.resolve(new Response(JSON.stringify(meta), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }));
      }
      operatorSignal = options?.signal ?? undefined;
      return new Promise((_resolve, reject) => {
        operatorSignal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        }, { once: true });
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();
    vi.useFakeTimers();

    const statusPromise = api.operatorStatus();
    const rejection = expect(statusPromise).rejects.toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(5_000);

    await rejection;
    expect(operatorSignal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('retains the in-memory bootstrap token across a transient fetch failure', async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('network unavailable'))
      .mockResolvedValueOnce(new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(registration()), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();

    await expect(api.initialize()).rejects.toThrow('network unavailable');
    await expect(api.initialize()).resolves.toMatchObject({ product: 'Focus' });

    const bootstrapCalls = fetchMock.mock.calls.filter(([path]) => path === '/api/auth/bootstrap');
    expect(bootstrapCalls).toHaveLength(2);
    expect(JSON.parse(String(bootstrapCalls[1]?.[1]?.body))).toEqual({ token: 'bootstrap-1' });
    const registrationCalls = fetchMock.mock.calls.filter(([path]) => path === '/api/client/register');
    expect(registrationCalls).toHaveLength(1);
    expect(history.replaceState).toHaveBeenCalledOnce();
  });

  it('uses server registration to split duplicated documents into distinct clients', async () => {
    sessionStorage.setItem('focus-web.client-id', 'copied-tab-id');
    const registrations: Array<Record<string, unknown>> = [];
    const fetchMock = vi.fn(async (path: string, options?: RequestInit) => {
      if (path === '/api/auth/bootstrap') {
        return new Response(JSON.stringify(bootstrap), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (path === '/api/client/register') {
        registrations.push(JSON.parse(String(options?.body)) as Record<string, unknown>);
        const duplicate = registrations.length > 1;
        return new Response(JSON.stringify(registration(
          duplicate ? 'web-clone' : 'copied-tab-id',
          duplicate ? 'document-token-clone' : 'document-token-original',
          duplicate,
        )), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const first = new FocusWebApi();
    const second = new FocusWebApi();
    await first.initialize();
    await second.initialize();

    expect(first.clientId).not.toBe(second.clientId);
    expect(new Set([first.clientId, second.clientId]).has('copied-tab-id')).toBe(true);
    expect(registrations).toHaveLength(2);
    expect(registrations.map((item) => item.resume_client_id)).toEqual([
      'copied-tab-id',
      'copied-tab-id',
    ]);
    expect(registrations[0]?.incarnation_id).not.toBe(registrations[1]?.incarnation_id);
  });

  it('recovers the retained floor when a same-incarnation registration response is lost', async () => {
    window.location.hash = '';
    sessionStorage.setItem('focus-web.client-id', 'reissued-client');
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new TypeError('registration response unavailable'))
      .mockResolvedValueOnce(new Response(JSON.stringify(registration(
        'reissued-client',
        'reissued-document-token',
        false,
        9,
      )), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();

    await expect(api.initialize()).rejects.toThrow('registration response unavailable');
    await expect(api.initialize()).resolves.toMatchObject({ product: 'Focus' });

    const registrationCalls = fetchMock.mock.calls.filter(([path]) => path === '/api/client/register');
    expect(registrationCalls).toHaveLength(2);
    const registrationBodies = registrationCalls.map(([, options]) => (
      JSON.parse(String(options?.body)) as Record<string, unknown>
    ));
    expect(registrationBodies.map((body) => body.resume_client_id)).toEqual([
      'reissued-client',
      'reissued-client',
    ]);
    expect(registrationBodies[0]?.incarnation_id).toBe(registrationBodies[1]?.incarnation_id);
    expect(api.intentGenerationFloor).toBe(9);
  });

  it('binds ordinary requests and event sockets to the registered document token', async () => {
    const fetchMock = vi.fn(async (path: string) => {
      if (path === '/api/auth/bootstrap') {
        return new Response(JSON.stringify(bootstrap), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (path === '/api/client/register') {
        return new Response(JSON.stringify(registration()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (path === '/api/threads?scope=global') {
        return new Response(JSON.stringify({
          runtime_epoch: 'epoch-1',
          revision: 0,
          scope: 'global',
          archived: false,
          limit: 100,
          truncated: false,
          threads: [],
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (path === '/api/threads/thread%2Fone/mutation-unknown') {
        return new Response(JSON.stringify({
          runtime_epoch: 'epoch-1',
          revision: 0,
          accepted: true,
          thread_id: 'thread/one',
          mutation_id: 'mutation-1',
          status: 'already_reconciled',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    class TestWebSocket {
      static lastUrl = '';

      constructor(url: string) {
        TestWebSocket.lastUrl = url;
      }

      addEventListener(): void {
        // Event delivery is outside this request-construction test.
      }
    }
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', TestWebSocket);
    const api = new FocusWebApi();

    await api.initialize();
    await api.listThreads();
    await api.verifyUnknownLifecycleMutation('thread/one', 'mutation-1');
    await api.resolveUnknownMutation(
      'thread/one',
      'discard',
      'mutation-1',
    );
    api.connectEvents({ event: () => {} });

    const threadCall = fetchMock.mock.calls.find(([path]) => path === '/api/threads?scope=global');
    expect(threadCall?.[1]).toMatchObject({
      headers: {
        'X-Focus-Web-Client': 'web-1',
        'X-Focus-Web-Document': 'document-token-1',
      },
    });
    expect(TestWebSocket.lastUrl).toContain('client=web-1');
    expect(TestWebSocket.lastUrl).toContain('document=document-token-1');
    expect(TestWebSocket.lastUrl).toContain('csrf=csrf-1');
    const lifecycleMutationCalls = fetchMock.mock.calls.filter(
      ([path]) => path === '/api/threads/thread%2Fone/mutation-unknown',
    );
    expect(lifecycleMutationCalls[0]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({ action: 'verify_lifecycle', mutation_id: 'mutation-1' }),
    });
    expect(lifecycleMutationCalls[1]?.[1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({
        action: 'discard',
        mutation_id: 'mutation-1',
      }),
    });
  });
});

describe('FocusWebApi attachments', () => {
  it('loads attachment bytes from Focus\'s cookie-authenticated route', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('image bytes', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();

    const blob = await api.attachmentBlob('diagram/1');

    expect(await blob.text()).toBe('image bytes');
    expect(fetchMock).toHaveBeenCalledWith('/api/attachments/diagram%2F1', {
      credentials: 'same-origin',
    });
  });
});

describe('FocusWebApi bounded turn-window requests', () => {
  it('sends one exact turn_limit on recent and history reads', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(bootstrap), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(registration()), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(wireSnapshot), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        runtime_epoch: 'epoch-1', revision: 0, items_view: 'summary', page_cursor: 'p',
        turns: [], older_turn_cursor: '', has_more_turns: false,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await api.readThread('thread-1', 3, 20);
    await api.listOlderTurns('thread-1', 'cursor-1', 'summary', 20);

    expect(fetchMock.mock.calls[3]?.[0]).toBe('/api/threads/thread-1?turn_limit=20');
    expect(fetchMock.mock.calls[4]?.[0]).toBe(
      '/api/threads/thread-1/turns?items_view=summary&turn_limit=20&cursor=cursor-1',
    );
  });
});

describe('FocusWebApi bounded thread inspection', () => {
  it('uses exact locator/search routes without sending kind as query authority', async () => {
    const locator = {
      turn_id: 'turn/one',
      item_id: 'item/one',
      kind: 'fileChange' as const,
      change_index: 3,
    };
    const toolDetail = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread/one',
      ...locator,
      view: 'preview' as const,
      status: 'found' as const,
      cursor: null,
      next_cursor: null,
      scanned_items: 1,
      detail: {
        view: 'preview' as const,
        tool: {
          id: 'item/one:4',
          name: 'Edit',
          arg: '',
          status: 'ok',
          inspectionLocator: locator,
        },
      },
    };
    const searchPage = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread/one',
      query: 'needle',
      cursor: 'cursor 1',
      occurrences: [{
        turn_id: 'turn/one',
        item_id: 'final/one',
        snippet: '😀 needle',
        snippet_match_range: { start: 3, end: 9 },
        turn_cursor: 'turn cursor 1',
      }],
      next_cursor: null,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(bootstrap), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(registration()), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(meta), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(toolDetail), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(searchPage), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await api.readToolDetail('thread/one', locator, 'preview');
    await api.searchConversation('thread/one', '  needle  ', 'cursor 1');

    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      '/api/threads/thread%2Fone/turns/turn%2Fone/tool-items/item%2Fone?view=preview&change_index=3',
    );
    expect(fetchMock.mock.calls[4]?.[0]).toBe(
      '/api/threads/thread%2Fone/conversation-search?query=needle&cursor=cursor+1',
    );
  });
});

describe('Focus HTTP response DTO decoders', () => {
  it('admits only exact cursor-paged tool-detail and conversation-search pages', () => {
    const commandLocator = {
      turn_id: 'turn-1',
      item_id: 'command-1',
      kind: 'commandExecution' as const,
      change_index: null,
    };
    const toolDetail = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread-1',
      ...commandLocator,
      view: 'preview' as const,
      status: 'found' as const,
      cursor: null,
      next_cursor: null,
      scanned_items: 1,
      detail: {
        view: 'preview' as const,
        tool: {
          id: 'command-1',
          name: 'Shell',
          arg: 'printf ok',
          status: 'ok',
          output: ['ok'],
          inspectionLocator: commandLocator,
        },
      },
    };
    expect(decodeFocusThreadToolDetailScanPage(
      toolDetail,
      'thread-1',
      commandLocator,
      null,
      'preview',
    )).not.toBeNull();
    expect(decodeFocusThreadToolDetailScanPage(
      { ...toolDetail, thread_id: 'thread-2' },
      'thread-1',
      commandLocator,
      null,
      'preview',
    )).toBeNull();
    expect(decodeFocusThreadToolDetailScanPage(
      {
        ...toolDetail,
        detail: {
          ...toolDetail.detail,
          tool: { ...toolDetail.detail.tool, id: 'synthetic' },
        },
      },
      'thread-1',
      commandLocator,
      null,
      'preview',
    )).toBeNull();
    expect(decodeFocusThreadToolDetailScanPage(
      {
        ...toolDetail,
        detail: {
          ...toolDetail.detail,
          tool: {
            ...toolDetail.detail.tool,
            inspectionLocator: { ...commandLocator, item_id: 'other' },
          },
        },
      },
      'thread-1',
      commandLocator,
      null,
      'preview',
    )).toBeNull();
    expect(decodeFocusThreadToolDetailScanPage(
      { ...toolDetail, unexpected: true },
      'thread-1',
      commandLocator,
      null,
      'preview',
    )).toBeNull();

    const fileLocator = {
      turn_id: 'turn-1',
      item_id: 'file-1',
      kind: 'fileChange' as const,
      change_index: 1,
    };
    const completeDiff = `@@ -1 +1 @@\n${'+persisted\n'.repeat(2_000)}`;
    const fullDetail = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread-1',
      ...fileLocator,
      view: 'full' as const,
      status: 'found' as const,
      cursor: null,
      next_cursor: null,
      scanned_items: 1,
      detail: {
        view: 'full' as const,
        source: {
          type: 'fileChange' as const,
          id: 'file-1',
          status: 'completed',
          changes: [
            { path: 'first.py', kind: { type: 'add' as const }, diff: '+first' },
            {
              path: 'second.py',
              kind: { type: 'update' as const, movePath: 'old-second.py' },
              diff: completeDiff,
            },
          ],
        },
      },
    };
    const decodedFull = decodeFocusThreadToolDetailScanPage(
      fullDetail,
      'thread-1',
      fileLocator,
      null,
      'full',
    );
    expect(decodedFull).not.toBeNull();
    expect(decodedFull?.detail).toMatchObject({
      view: 'full',
      source: { changes: [{ path: 'first.py' }, { diff: completeDiff }] },
    });
    expect(decodeFocusThreadToolDetailScanPage(
      { ...fullDetail, view: 'preview' },
      'thread-1',
      fileLocator,
      null,
      'full',
    )).toBeNull();
    expect(decodeFocusThreadToolDetailScanPage(
      {
        ...fullDetail,
        detail: {
          ...fullDetail.detail,
          source: {
            ...fullDetail.detail.source,
            changes: [fullDetail.detail.source.changes[1]],
          },
        },
      },
      'thread-1',
      fileLocator,
      null,
      'full',
    )).toBeNull();

    const fullCommandDetail = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread-1',
      ...commandLocator,
      view: 'full' as const,
      status: 'found' as const,
      cursor: null,
      next_cursor: null,
      scanned_items: 1,
      detail: {
        view: 'full' as const,
        source: {
          type: 'commandExecution' as const,
          id: 'command-1',
          pluginId: 'plugin-1',
          scriptPath: 'scripts/check.ts',
          command: 'npm test',
          cwd: '/work',
          processId: 'pty-1',
          source: 'agent',
          status: 'completed',
          commandActions: [
            { type: 'read', command: 'cat a.ts', name: 'cat', path: '/work/a.ts' },
            { type: 'listFiles', command: 'find', path: null },
            { type: 'search', command: 'rg needle', query: 'needle', path: 'src' },
            { type: 'unknown', command: 'opaque-command' },
          ],
          aggregatedOutput: 'head\ncomplete persisted output\ntail',
          exitCode: 0,
          durationMs: 12,
        },
      },
    };
    expect(decodeFocusThreadToolDetailScanPage(
      fullCommandDetail,
      'thread-1',
      commandLocator,
      null,
      'full',
    )).toMatchObject({
      detail: {
        view: 'full',
        source: { aggregatedOutput: 'head\ncomplete persisted output\ntail' },
      },
    });
    expect(decodeFocusThreadToolDetailScanPage(
      {
        ...fullCommandDetail,
        detail: {
          ...fullCommandDetail.detail,
          source: {
            ...fullCommandDetail.detail.source,
            commandActions: [{ type: 'read', command: 'cat a.ts', name: 'cat' }],
          },
        },
      },
      'thread-1',
      commandLocator,
      null,
      'full',
    )).toBeNull();

    const searchPage = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      thread_id: 'thread-1',
      query: 'needle',
      cursor: null,
      occurrences: [{
        turn_id: 'turn-1',
        item_id: 'final-1',
        snippet: '😀 needle',
        snippet_match_range: { start: 3, end: 9 },
        turn_cursor: 'turn-cursor-1',
      }],
      next_cursor: 'next-1',
    };
    expect(decodeFocusThreadConversationSearchPage(
      searchPage,
      'thread-1',
      'needle',
      null,
    )).not.toBeNull();
    expect(decodeFocusThreadConversationSearchPage(
      { ...searchPage, query: 'other' },
      'thread-1',
      'needle',
      null,
    )).toBeNull();
    expect(decodeFocusThreadConversationSearchPage(
      {
        ...searchPage,
        occurrences: [{
          ...searchPage.occurrences[0],
          snippet_match_range: { start: 1, end: 9 },
        }],
      },
      'thread-1',
      'needle',
      null,
    )).toBeNull();
    expect(decodeFocusThreadConversationSearchPage(
      { ...searchPage, occurrences: Array(21).fill(searchPage.occurrences[0]) },
      'thread-1',
      'needle',
      null,
    )).toBeNull();
    expect(decodeFocusThreadConversationSearchPage(
      {
        ...searchPage,
        occurrences: [{ ...searchPage.occurrences[0], snippet: 'x'.repeat(1025) }],
      },
      'thread-1',
      'needle',
      null,
    )).toBeNull();
  });

  it('strictly admits optional live-card inspection locators', () => {
    const locator = {
      turn_id: 'turn-1',
      item_id: 'command-1',
      kind: 'commandExecution' as const,
      change_index: null,
    };
    const tool = {
      id: 'command-1',
      name: 'Shell',
      arg: 'printf ok',
      status: 'ok',
      inspectionLocator: locator,
    };
    const turn = {
      id: 'turn-1:assistant',
      role: 'assistant',
      no: 1,
      text: '',
      tools: [tool],
      blocks: [{ kind: 'tool', tool }],
    };

    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      turns: [turn],
    })?.turns[0]?.tools?.[0]?.inspectionLocator).toEqual(locator);
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      turns: [{
        ...turn,
        tools: [{
          ...tool,
          inspectionLocator: { ...locator, change_index: 0 },
        }],
        blocks: [],
      }],
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      turns: [{
        ...turn,
        tools: [{
          ...tool,
          inspectionLocator: { ...locator, kind: 'futureTool' },
        }],
        blocks: [],
      }],
    })).toBeNull();
  });

  it('accepts the current Focus-owned HTTP contracts', () => {
    const threadList = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      scope: 'global',
      archived: false,
      limit: 100,
      truncated: false,
      threads: [wireThread],
    };
    const turnPage = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      items_view: 'full',
      page_cursor: '',
      turns: [],
      older_turn_cursor: '',
      has_more_turns: false,
    };
    const profileResult = {
      runtime_epoch: 'epoch-1',
      revision: 0,
      writer_profile: meta.writer_profile,
      scope_changed: false,
      previous_attachment_scope: '',
      current_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 1,
      attachment_scope_disposition: 'unchanged',
      invalidated_attachment_count: 0,
      rebound_attachment_count: 0,
    };

    expect(decodeFocusDocumentRegistration(registration())).not.toBeNull();
    expect(decodeFocusDocumentRegistration({
      ...registration(),
      document_receipt: 'A'.repeat(64),
    })).toBeNull();
    expect(decodeFocusDocumentRegistration({
      ...registration(),
      document_receipt: 'a'.repeat(63),
    })).toBeNull();
    expect(decodeFocusDocumentRegistration({
      ...registration(),
      intent_generation_floor: undefined,
    })).toBeNull();
    expect(decodeFocusDocumentRegistration({
      ...registration(),
      intent_generation_floor: -1,
    })).toBeNull();
    expect(decodeFocusBootstrapResult(bootstrap)).not.toBeNull();
    expect(decodeFocusMeta(meta)).not.toBeNull();
    expect(decodeFocusNextTurnSettingsResult({
      runtime_epoch: 'epoch-1',
      revision: 0,
      next_turn_settings: meta.next_turn_settings,
    })).not.toBeNull();
    expect(decodeFocusNextTurnSettingsResult({
      runtime_epoch: 'epoch-1',
      revision: 0,
      next_turn_settings: {
        ...meta.next_turn_settings,
        approval_policy: ' never ',
      },
    })).toBeNull();
    expect(decodeFocusNextTurnSettingsResult({
      runtime_epoch: 'epoch-1',
      revision: 0,
      next_turn_settings: {
        ...meta.next_turn_settings,
        unexpected: 'legacy',
      },
    })).toBeNull();
    expect(decodeFocusMeta({ ...meta, default_model: 'legacy' })).toBeNull();
    expect(decodeFocusWriterProfileResult(profileResult)).not.toBeNull();
    expect(decodeFocusWriterProfileResult({
      ...profileResult,
      applies_to: 'next_turn',
    })).toBeNull();
    expect(decodeFocusWriterProfileResult({
      ...profileResult,
      attachment_scope_disposition: 'future-disposition',
    })).toBeNull();
    expect(decodeFocusWriterProfileResult({
      ...profileResult,
      current_attachment_scope: undefined,
    })).toBeNull();
    expect(decodeFocusWriterProfileResult({
      ...profileResult,
      scope_changed: true,
      previous_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 2,
      writer_profile: { ...profileResult.writer_profile, scope_generation: 2 },
      attachment_scope_disposition: 'unchanged',
    })).toBeNull();
    expect(decodeFocusWriterProfileResult({
      ...profileResult,
      scope_changed: true,
      previous_attachment_scope: 'thread:thread-1',
      previous_scope_generation: 1,
      current_scope_generation: 2,
      writer_profile: { ...profileResult.writer_profile, scope_generation: 2 },
      attachment_scope_disposition: 'rebound',
      invalidated_attachment_count: 1,
    })).toBeNull();
    expect(decodeFocusAttachmentUpload({
      file_id: 'attachment-1',
      name: 'image.png',
      media_type: 'image/png',
      size: 12,
      url: '/api/attachments/attachment-1',
    })).not.toBeNull();
    expect(decodeFocusThreadList(threadList)).not.toBeNull();
    const {
      loaded_state_verified: _loadedStateVerified,
      ...threadWithoutLoadedStateVerification
    } = wireThread;
    expect(decodeFocusThreadList({
      ...threadList,
      threads: [threadWithoutLoadedStateVerification],
    })).toBeNull();
    expect(decodeFocusThreadSnapshot(wireSnapshot)).not.toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      settings_scope: {},
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      selection_scope: undefined,
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      selection_scope: {
        ...wireSnapshot.selection_scope,
        scope_changed: true,
        previous_attachment_scope: 'draft:/work',
        previous_scope_generation: 1,
        current_scope_generation: 1,
        attachment_scope_disposition: 'isolated',
      },
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...wireSnapshot,
      selection_scope: {
        ...wireSnapshot.selection_scope,
        attachment_scope_disposition: 'isolated',
      },
    })).toBeNull();
    expect(decodeFocusTurnPage(turnPage)).not.toBeNull();
    const summaryPage = {
      ...turnPage,
      items_view: 'summary',
      page_cursor: 'stable-page',
      turns: [{
        id: 'turn-1:user', role: 'user', no: 1, text: 'Prompt', title_truncated: false,
      }],
    };
    expect(decodeFocusTurnPage(summaryPage)).not.toBeNull();
    expect(decodeFocusTurnPage({
      ...summaryPage,
      turns: [{ ...summaryPage.turns[0], agent_body: 'must not cross summary wire' }],
    })).toBeNull();
    expect(decodeFocusTurnPage({
      ...summaryPage,
      turns: [{ ...summaryPage.turns[0], title_truncated: 'false' }],
    })).toBeNull();
    expect(decodeFocusMutationResult({
      accepted: true,
      mode: 'started',
      thread_id: 'thread-1',
      turn_id: 'turn-1',
      owner: wireThread.owner,
    })).not.toBeNull();
    expect(decodeFocusMutationResult({
      accepted: true,
      mode: 'steered',
      thread_id: 'thread-1',
      turn_id: 'turn-1',
      owner: wireThread.owner,
    })).toBeNull();
    expect(decodeFocusMutationResult({
      accepted: false,
      thread_id: 'thread-1',
    })).toBeNull();
    expect(decodeFocusLifecycleVerificationResult({
      runtime_epoch: 'epoch-1',
      revision: 1,
      accepted: true,
      thread_id: 'thread-1',
      mutation_id: 'mutation-1',
      operation: 'archive',
      verification: { state: 'archived', verification_id: 'verification-1' },
    })).not.toBeNull();
    expect(decodeFocusLifecycleVerificationResult({
      runtime_epoch: 'epoch-1',
      revision: 1,
      accepted: false,
      thread_id: 'thread-1',
      mutation_id: 'mutation-1',
      status: 'already_reconciled',
    })).toBeNull();
    expect(decodeFocusLifecycleVerificationResult({
      runtime_epoch: 'epoch-1',
      revision: 1,
      accepted: true,
      thread_id: 'thread-1',
      mutation_id: 'mutation-1',
      status: 'already_reconciled',
    })).not.toBeNull();
    expect(decodeFocusLifecycleVerificationResult({
      runtime_epoch: 'epoch-1',
      revision: 1,
      accepted: true,
      thread_id: 'thread-1',
      mutation_id: 'mutation-1',
      status: 'already_reconciled',
      operation: 'archive',
      verification: { state: 'archived', verification_id: 'verification-1' },
    })).toBeNull();
    expect(decodeFocusRenameResult({
      accepted: true,
      thread_id: 'thread-1',
      name: 'Renamed',
    })).not.toBeNull();
    expect(decodeFocusGoalResult({
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      goal: null,
    })).not.toBeNull();
    expect(decodeFocusLifecycleResult({
      thread_id: 'thread-1',
      upstream_outcome: 'success',
      focus_cleanup: 'complete',
      cleanup_errors: [],
    })).not.toBeNull();
    expect(decodeFocusRequestResponseResult({ accepted: true })).not.toBeNull();
    expect(decodeFocusRequestResponseResult({ accepted: false })).toBeNull();
  });

  it('admits only exact active-turn disclosure and per-setting provenance', () => {
    const activeSnapshot = {
      ...wireSnapshot,
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: wireActiveTurnContext,
    };
    const decoded = decodeFocusThreadSnapshot(activeSnapshot);

    expect(decoded?.active_turn_context).toEqual(wireActiveTurnContext);
    expect(decoded?.active_turn_context?.settings.model.value).toBe('gpt-active');
    expect(decodeFocusThreadSnapshot({
      ...activeSnapshot,
      active_turn_context: null,
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...activeSnapshot,
      active_turn_context: {
        ...wireActiveTurnContext,
        turn_id: 'turn-2',
      },
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...activeSnapshot,
      active_turn_context: {
        ...wireActiveTurnContext,
        initiator: { kind: 'feishu', binding_id: '' },
      },
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...activeSnapshot,
      active_turn_context: {
        ...wireActiveTurnContext,
        settings: {
          ...wireActiveTurnContext.settings,
          model: { value: 'gpt-active', source: 'guessed' },
        },
      },
    })).toBeNull();
    expect(decodeFocusThreadSnapshot({
      ...activeSnapshot,
      active_turn_context: {
        ...wireActiveTurnContext,
        settings: {
          ...wireActiveTurnContext.settings,
          approval_policy: { value: 'on-request', source: 'unknown' },
        },
      },
    })).toBeNull();
  });

  it('rejects malformed meta, list, snapshot, and every mutation result family', () => {
    const malformedCases: Array<[
      string,
      (value: unknown) => unknown,
      unknown,
    ]> = [
      ['document registration', decodeFocusDocumentRegistration, {
        ...registration(), document_token: 7,
      }],
      ['bootstrap', decodeFocusBootstrapResult, { ...bootstrap, authenticated: false }],
      ['meta', decodeFocusMeta, { ...meta, writer_profile: null }],
      ['profile mutation', decodeFocusWriterProfileResult, {
        runtime_epoch: 'epoch-1', revision: 0, writer_profile: {},
      }],
      ['next-turn settings mutation', decodeFocusNextTurnSettingsResult, {
        runtime_epoch: 'epoch-1', revision: 0,
        next_turn_settings: { ...meta.next_turn_settings, generation: 0 },
      }],
      ['attachment mutation', decodeFocusAttachmentUpload, {
        file_id: 'attachment-1', name: 'bad', media_type: 'image/png', size: '12', url: '',
      }],
      ['thread list', decodeFocusThreadList, {
        runtime_epoch: 'epoch-1', revision: 0, scope: 'global', archived: false,
        limit: 100, truncated: false, threads: [{ ...wireThread, owner: null }],
      }],
      ['thread snapshot', decodeFocusThreadSnapshot, {
        ...wireSnapshot, pending_requests: [{ id: 'request-1' }],
      }],
      ['thread snapshot unknown durability', decodeFocusThreadSnapshot, {
        ...wireSnapshot,
        mutation_unknown: {
          mutation_id: 'mutation-rename',
          operation: 'rename',
          reconciling: true,
          lifecycle_verification: null,
        },
      }],
      ['turn page', decodeFocusTurnPage, {
        runtime_epoch: 'epoch-1', revision: 0, turns: [{ id: 'turn-1' }],
        older_turn_cursor: '', has_more_turns: false,
      }],
      ['thread/prompt/control mutation', decodeFocusMutationResult, {
        accepted: true, thread_id: 7,
      }],
      ['unknown-settlement disposition', decodeFocusMutationResult, {
        accepted: true, thread_id: 'thread-1', mutation_id: 'mutation-1', disposition: 'wrong',
      }],
      ['lifecycle verification mutation', decodeFocusLifecycleVerificationResult, {
        runtime_epoch: 'epoch-1', revision: 1, accepted: true, thread_id: 'thread-1',
        operation: 'archive', verification: { state: 'maybe', verification_id: 'v1' },
      }],
      ['rename mutation', decodeFocusRenameResult, {
        accepted: true, thread_id: 'thread-1', name: 9,
      }],
      ['goal mutation', decodeFocusGoalResult, {
        runtime_epoch: 'epoch-1', revision: 1, thread_id: 'thread-1', goal: { budget: null },
      }],
      ['archive/unarchive/delete mutation', decodeFocusLifecycleResult, {
        thread_id: 'thread-1', upstream_outcome: 'maybe',
        focus_cleanup: 'complete', cleanup_errors: [],
      }],
      ['interaction response mutation', decodeFocusRequestResponseResult, { accepted: 'yes' }],
    ];

    for (const [contract, decoder, malformed] of malformedCases) {
      expect(decoder(malformed), contract).toBeNull();
    }
  });

  it('fails closed at the API boundary for malformed reads and mutations', async () => {
    window.location.hash = '';
    let metaCalls = 0;
    let nextPayload: unknown = null;
    const fetchMock = vi.fn(async (path: string) => {
      if (path === '/api/client/register') {
        return new Response(JSON.stringify(registration()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (path === '/api/meta' && metaCalls++ === 0) {
        return new Response(JSON.stringify(meta), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify(nextPayload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    const assertInvalid = async (request: () => Promise<unknown>, payload: unknown) => {
      nextPayload = payload;
      await expect(request()).rejects.toMatchObject({
        code: 'invalid_gateway_response',
        status: 502,
      });
    };

    await assertInvalid(() => api.meta(), { ...meta, models: 'not-an-array' });
    await assertInvalid(() => api.operatorStatus(), { status: 'ok', warnings: [] });
    await assertInvalid(() => api.listThreads(), { threads: [] });
    await assertInvalid(() => api.readThread('thread-1'), { ...wireSnapshot, goal: {} });
    await assertInvalid(() => api.listOlderTurns('thread-1', 'cursor-1'), { turns: [] });
    await assertInvalid(() => api.updateProfile({ working_dir: '/next' }), { writer_profile: {} });
    await assertInvalid(() => api.readNextTurnSettings(), {
      runtime_epoch: 'epoch-1', revision: 0,
      next_turn_settings: { ...meta.next_turn_settings, generation: 0 },
    });
    await assertInvalid(() => api.updateNextTurnSettings({ model: 'gpt-test' }), {
      runtime_epoch: 'epoch-1', revision: 0,
      next_turn_settings: { ...meta.next_turn_settings, permissions_profile_id: '  ' },
    });
    await assertInvalid(
      () => api.uploadAttachment(new Blob(['x']), {
        name: 'x.txt',
        cwd: '/work',
        scopeGeneration: 1,
      }),
      { file_id: 'attachment-1', size: 'one' },
    );
    await assertInvalid(() => api.startThread({ text: 'hello', cwd: '/work' }), { accepted: true });
    await assertInvalid(
      () => api.interrupt('thread-1', 'turn-1'),
      { accepted: 'yes', thread_id: 'thread-1' },
    );
    await assertInvalid(
      () => api.verifyUnknownLifecycleMutation('thread-1', 'mutation-1'),
      { accepted: true, thread_id: 'thread-1' },
    );
    await assertInvalid(
      () => api.resolveUnknownMutation('thread-1', 'discard', 'mutation-1'),
      { accepted: true, thread_id: 4 },
    );
    await assertInvalid(() => api.renameThread('thread-1', 'name'), { accepted: true, name: 'name' });
    await assertInvalid(() => api.compactThread('thread-1'), { accepted: true, action: 'compact' });
    await assertInvalid(() => api.startReview('thread-1', {}), { accepted: true, action: 'review' });
    await assertInvalid(() => api.getGoal('thread-1'), { thread_id: 'thread-1', goal: null });
    await assertInvalid(() => api.setGoal('thread-1', { objective: 'goal' }), { goal: null });
    await assertInvalid(() => api.clearGoal('thread-1'), { cleared: true });
    await assertInvalid(() => api.archiveThread('thread-1'), { upstream_outcome: 'success' });
    await assertInvalid(() => api.unarchiveThread('thread-1'), { focus_cleanup: 'complete' });
    await assertInvalid(() => api.deleteThread('thread-1', 'thread-1'), { cleanup_errors: [] });
    await assertInvalid(
      () => api.respondRequest('request-1', 1, 'capability-1', 'approve'),
      { accepted: 'yes' },
    );
  });
});

describe('FocusWebApi projection events', () => {
  it('delivers only strictly decoded event envelopes', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      if (path === '/api/client/register') {
        return new Response(JSON.stringify(registration()), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify(meta), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    type SocketListener = (event: { data?: unknown }) => void;
    class TestWebSocket {
      private readonly listeners = new Map<string, SocketListener[]>();

      addEventListener(type: string, listener: SocketListener): void {
        const current = this.listeners.get(type) ?? [];
        this.listeners.set(type, [...current, listener]);
      }

      message(data: unknown): void {
        for (const listener of this.listeners.get('message') ?? []) listener({ data });
      }
    }
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', TestWebSocket);
    const api = new FocusWebApi();
    await api.initialize();
    const event = vi.fn();
    const invalid = vi.fn();
    const socket = api.connectEvents({ event, invalid }) as unknown as TestWebSocket;

    for (const frame of [
      '{',
      JSON.stringify(null),
      JSON.stringify({ type: 'thread_delta', runtime_epoch: '', revision: 1 }),
      JSON.stringify({ type: 'thread_delta', runtime_epoch: 'epoch-1', revision: '1' }),
      JSON.stringify({ type: 'thread_delta', runtime_epoch: 'epoch-1', revision: -1 }),
      JSON.stringify({ type: 'thread_delta', runtime_epoch: 'epoch-1', revision: 1.5 }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        detail: [],
      }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        detail: { method: 'thread/name/updated', thread_name: 'No target' },
      }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: 'thread-1',
        detail: { thread_name: 'No method' },
      }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: 'thread-1',
        detail: { method: 'thread/goal/updated', goal: { budget: null } },
      }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: 'thread-1',
        detail: { method: 'focus/unsupported/reconciled', tasks: [{ id: 'child-1' }] },
      }),
      JSON.stringify({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: 'thread-1',
        detail: {
          method: 'thread/tokenUsage/updated',
          token_usage: { total: { totalTokens: 'many' } },
        },
      }),
      new Blob(['binary']),
    ]) socket.message(frame);

    socket.message('pong');
    socket.message(JSON.stringify({
      type: 'future_projection_event',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: '',
      reason: '',
      occurred_at: 1.5,
      detail: { future: true },
    }));

    expect(invalid).toHaveBeenCalledTimes(14);
    expect(event).not.toHaveBeenCalled();
  });
});

describe('Focus projection DTO decoders', () => {
  it('requires one exact structured boundary for truncated tool and diff output', () => {
    const marker =
      '[Focus Web omitted 1000 characters of tool output; showing a bounded head and tail.]';
    const tool = {
      id: 'tool-1',
      name: 'Shell',
      arg: '',
      status: 'ok',
      output: [marker, marker, 'tail'],
      outputTruncated: true,
      outputOmittedChars: 1000,
      outputHeadLineCount: 1,
      diff: {
        lines: [
          { type: 'hunk', text: marker },
          { type: 'hunk', text: marker },
          { type: 'context', text: 'tail' },
        ],
        omittedChars: 1000,
        omissionLineIndex: 1,
      },
    };
    const truncatedWithoutOutput: Record<string, unknown> = { ...tool };
    delete truncatedWithoutOutput.output;
    const independentlyValidCountMismatch = {
      ...tool,
      diff: {
        ...tool.diff,
        lines: [
          { type: 'hunk', text: marker },
          {
            type: 'hunk',
            text: '[Focus Web omitted 999 characters of tool output; showing a bounded head and tail.]',
          },
          { type: 'context', text: 'tail' },
        ],
        omittedChars: 999,
      },
    };
    const independentlyValidBoundaryMismatch = {
      ...tool,
      diff: {
        ...tool.diff,
        lines: [
          { type: 'hunk', text: marker },
          { type: 'context', text: 'prefix' },
          { type: 'hunk', text: marker },
          { type: 'context', text: 'tail' },
        ],
        omissionLineIndex: 2,
      },
    };
    const event = {
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'turn/completed',
        turns: [{
          id: 'turn-1:assistant', role: 'assistant', no: 1, text: '',
          blocks: [{ kind: 'tool', tool }],
          tools: [tool],
        }],
      },
    };

    expect(decodeFocusProjectionEvent(event)).not.toBeNull();
    for (const malformedTool of [
      truncatedWithoutOutput,
      { ...tool, outputHeadLineCount: 2 },
      { ...tool, outputHeadLineCount: undefined },
      { ...tool, outputTruncated: false },
      { ...tool, diff: { ...tool.diff, omissionLineIndex: 2 } },
      independentlyValidCountMismatch,
      independentlyValidBoundaryMismatch,
    ]) {
      expect(decodeFocusProjectionEvent({
        ...event,
        detail: {
          ...event.detail,
          turns: [{
            ...event.detail.turns[0],
            blocks: [{ kind: 'tool', tool: malformedTool }],
            tools: [malformedTool],
          }],
        },
      })).toBeNull();
    }
  });

  it('enforces the visible tool-output aggregate on every full-turn decoder', () => {
    const turnsWithOutputs = (outputs: string[]) => {
      const tools = outputs.map((output, index) => ({
        id: `tool-${index}`,
        name: 'Shell',
        arg: '',
        status: 'ok',
        output: [output],
      }));
      return [{
        id: 'turn-aggregate:assistant',
        role: 'assistant',
        no: 1,
        text: '',
        tools,
        blocks: tools.map((tool) => ({ kind: 'tool', tool })),
      }];
    };
    const decoders: Array<[string, (turns: ReturnType<typeof turnsWithOutputs>) => unknown]> = [
      ['thread snapshot', (turns) => decodeFocusThreadSnapshot({ ...wireSnapshot, turns })],
      ['full history page', (turns) => decodeFocusTurnPage({
        runtime_epoch: 'epoch-1',
        revision: 1,
        items_view: 'full',
        page_cursor: 'stable-page',
        turns,
        older_turn_cursor: '',
        has_more_turns: false,
      })],
      ['thread delta', (turns) => decodeFocusProjectionEvent({
        type: 'thread_delta',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: 'thread-1',
        detail: { method: 'turn/completed', turns },
      })],
    ];
    const exactOutputCount = turnsWithOutputs(Array.from({ length: 16 }, () => 'x'));
    const excessiveOutputCount = turnsWithOutputs(Array.from({ length: 17 }, () => 'x'));
    const fullCard = '😀'.repeat(65_536);
    const exactCodePointCount = turnsWithOutputs(Array.from({ length: 4 }, () => fullCard));
    const excessiveCodePointCount = turnsWithOutputs([
      ...Array.from({ length: 4 }, () => fullCard),
      '😀',
    ]);
    const mixedSurfaceTurns = (blockCount: number, directCount: number) => {
      const blockTools = Array.from({ length: blockCount }, (_, index) => ({
        id: `block-tool-${index}`, name: 'Shell', arg: '', status: 'ok', output: ['x'],
      }));
      const directTools = Array.from({ length: directCount }, (_, index) => ({
        id: `direct-tool-${index}`, name: 'Shell', arg: '', status: 'ok', output: ['x'],
      }));
      return [
        {
          id: 'turn-blocks:assistant', role: 'assistant', no: 1, text: '',
          tools: [], blocks: blockTools.map((tool) => ({ kind: 'tool', tool })),
        },
        {
          id: 'turn-tools:assistant', role: 'assistant', no: 1, text: '',
          tools: directTools, blocks: [],
        },
      ] as ReturnType<typeof turnsWithOutputs>;
    };

    for (const [surface, decode] of decoders) {
      expect(decode(exactOutputCount), `${surface}: 16 non-empty outputs`).not.toBeNull();
      expect(decode(excessiveOutputCount), `${surface}: 17 non-empty outputs`).toBeNull();
      expect(decode(exactCodePointCount), `${surface}: 262144 code points`).not.toBeNull();
      expect(decode(excessiveCodePointCount), `${surface}: 262145 code points`).toBeNull();
      expect(decode(mixedSurfaceTurns(8, 8)), `${surface}: mixed surfaces at 16`).not.toBeNull();
      expect(decode(mixedSurfaceTurns(8, 9)), `${surface}: mixed surfaces at 17`).toBeNull();
      const divergentMirrors = turnsWithOutputs(['block value']);
      divergentMirrors[0]!.tools![0] = {
        ...divergentMirrors[0]!.tools![0]!,
        output: ['different direct value'],
      };
      expect(decode(divergentMirrors), `${surface}: divergent mirrors`).toBeNull();
    }
  });

  it('accepts only the empty zero-boundary shape for aggregate omission', () => {
    const tool = {
      id: 'tool-omitted',
      name: 'Shell',
      arg: '',
      status: 'ok',
      output: [],
      outputTruncated: true,
      outputOmittedChars: 100_000,
      outputHeadLineCount: 0,
      diff: {
        lines: [],
        omittedChars: 100_000,
        omissionLineIndex: 0,
      },
    };
    const event = {
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'turn/completed',
        turns: [{
          id: 'turn-1:assistant', role: 'assistant', no: 1, text: '',
          blocks: [{ kind: 'tool', tool }],
          tools: [tool],
        }],
      },
    };

    expect(decodeFocusProjectionEvent(event)).not.toBeNull();
    for (const malformedTool of [
      { ...tool, output: ['not empty'] },
      { ...tool, outputHeadLineCount: 1 },
      { ...tool, diff: { ...tool.diff, lines: [{ type: 'hunk', text: 'not empty' }] } },
      { ...tool, diff: { ...tool.diff, omissionLineIndex: 1 } },
    ]) {
      expect(decodeFocusProjectionEvent({
        ...event,
        detail: {
          ...event.detail,
          turns: [{
            ...event.detail.turns[0],
            blocks: [{ kind: 'tool', tool: malformedTool }],
            tools: [malformedTool],
          }],
        },
      })).toBeNull();
    }
  });

  it('accepts a complete thread delta and rejects malformed nested state', () => {
    const event = {
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 4,
      thread_id: 'thread-1',
      detail: {
        method: 'turn/completed',
        turns: [{
          id: 'turn-1:assistant',
          role: 'assistant',
          no: 1,
          text: 'done',
          blocks: [{ kind: 'text', itemId: 'message-1', text: 'done' }],
          tools: [],
          createdAt: null,
          durationMs: null,
          status: 'completed',
        }],
        tasks: [{
          id: 'child-1',
          name: 'Reviewer',
          kind: 'subagent',
          state: 'done',
          timing: '',
          result: ['complete'],
          executionState: 'completed',
        }],
        goal: {
          goal_id: 'thread-1',
          objective: 'Review contracts',
          status: 'active',
          tokens_used: 120,
          wall_clock_ms: 500,
          budget: {
            token_budget: 1000,
            remaining_tokens: 880,
            turn_budget: null,
            remaining_turns: null,
            wall_clock_budget_ms: null,
            remaining_wall_clock_ms: null,
            over_budget: false,
          },
        },
        token_usage: {
          total: { totalTokens: 120, inputTokens: 100, outputTokens: 20 },
          modelContextWindow: 8192,
        },
      },
    };

    const decoded = decodeFocusProjectionEvent(event);
    expect(decoded).toMatchObject({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 4,
      thread_id: 'thread-1',
    });
    expect(decoded?.detail?.turns).toEqual([{
      id: 'turn-1:assistant',
      role: 'assistant',
      no: 1,
      text: 'done',
      blocks: [{ kind: 'text', itemId: 'message-1', text: 'done' }],
      tools: [],
      status: 'completed',
    }]);
    expect(decoded?.detail?.tasks).toEqual(event.detail.tasks);
    expect(decoded?.detail?.goal).toEqual(event.detail.goal);
    expect(decoded?.detail?.token_usage).toEqual(event.detail.token_usage);
    expect(decodeFocusProjectionEvent({
      ...event,
      detail: {
        ...event.detail,
        turns: [{ ...event.detail.turns[0], blocks: [{ kind: 'tool', tool: null }] }],
      },
    })).toBeNull();
    expect(decodeFocusProjectionEvent({
      ...event,
      detail: {
        ...event.detail,
        tasks: [{ ...event.detail.tasks[0], progress: [1] }],
      },
    })).toBeNull();
    for (const malformedTurn of [
      {
        ...event.detail.turns[0],
        blocks: [{
          kind: 'tool',
          tool: {
            id: 'tool-1', name: 'Diff', arg: '', status: 'ok',
            diff: { path: 'a.txt', lines: 'not-an-array' },
          },
        }],
      },
      { ...event.detail.turns[0], approval: { kind: 'generic', summary: 7 } },
      { ...event.detail.turns[0], compaction: { tokensBefore: 'many' } },
      { ...event.detail.turns[0], skillActivation: { args: '--missing-name' } },
      { ...event.detail.turns[0], pluginCommand: { pluginId: 'p', commandName: 3 } },
      { ...event.detail.turns[0], cron: { recurring: 'yes' } },
    ]) {
      expect(decodeFocusProjectionEvent({
        ...event,
        detail: { ...event.detail, turns: [malformedTurn] },
      })).toBeNull();
    }
  });

  it('rejects an unknown event before it enters client projection state', () => {
    expect(decodeFocusProjectionEvent({
      type: 'future_projection_event',
      runtime_epoch: 'epoch-1',
      revision: 1,
      detail: { future_shape: true },
    })).toBeNull();
  });

  it('applies per-event detail contracts to known non-delta events', () => {
    expect(decodeFocusProjectionEvent({
      type: 'settings_changed',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: '',
      reason: 'web_next_turn_settings_updated',
    })).not.toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'settings_changed',
      runtime_epoch: 'epoch-1',
      revision: 1,
      detail: { next_turn_settings: meta.next_turn_settings },
    })).toBeNull();
    for (const forbidden of [
      { generation: 2 },
      { next_turn_settings: meta.next_turn_settings },
      { mutation_id: 'mutation-1' },
    ]) {
      expect(decodeFocusProjectionEvent({
        type: 'settings_changed',
        runtime_epoch: 'epoch-1',
        revision: 1,
        thread_id: '',
        reason: 'web_next_turn_settings_updated',
        ...forbidden,
      })).toBeNull();
    }
    expect(decodeFocusProjectionEvent({
      type: 'owner_changed',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
    })).not.toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'owner_changed',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: { ignored: true },
    })).toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'projection_invalidated',
      runtime_epoch: 'epoch-1',
      revision: 2,
      reason: 'socket_backpressure',
      detail: { reload: true },
    })).not.toBeNull();
    expect(decodeFocusProjectionEvent({
      type: 'projection_invalidated',
      runtime_epoch: 'epoch-1',
      revision: 2,
      reason: 'socket_backpressure',
      detail: { reload: false },
    })).toBeNull();
    const mutationEvent = {
      type: 'mutation_reconciled',
      runtime_epoch: 'epoch-1',
      revision: 3,
      thread_id: 'thread-1',
      reason: 'prompt',
      detail: {
        mutation_id: 'mutation-1',
        operation: 'prompt',
        disposition: 'effect_observed',
      },
    } as const;
    expect(decodeFocusProjectionEvent(mutationEvent)).not.toBeNull();
    expect(decodeFocusProjectionEvent({
      ...mutationEvent,
      detail: { ...mutationEvent.detail, disposition: 'future_disposition' },
    })).toBeNull();
    const { disposition: _disposition, ...missingDisposition } = mutationEvent.detail;
    expect(decodeFocusProjectionEvent({
      ...mutationEvent,
      detail: missingDisposition,
    })).toBeNull();
  });

  it('accepts the current production operator status shape and rejects malformed DTOs', () => {
    const status = {
      status: 'degraded',
      observed_at: 10,
      poll_after_seconds: 5,
      warnings: [{
        code: 'runtime_queue_delay',
        source: 'RuntimeLoop',
        message: 'RuntimeLoop task queue delay exceeded its threshold.',
        severity: 'warning',
        attention: 'advisory',
        first_seen_at: 9,
        last_seen_at: 10,
        occurrences: 1,
        details: {},
      }],
      adapter_ingress: {
        latest_generation: 2,
        last_disconnected_generation: 1,
        backend_reset_blocked: false,
        cleanup_required: false,
        disconnect_cleanup_pending: false,
        recovery_action: '',
      },
      feishu_destination_liveness: {
        worker_running: true,
        pending_proofs: 0,
        last_error: '',
      },
      runtime_loop: {},
    };

    expect(decodeFocusOperatorStatus(status)).toEqual({
      status: status.status,
      observed_at: status.observed_at,
      poll_after_seconds: status.poll_after_seconds,
      warnings: status.warnings,
      runtime_loop: status.runtime_loop,
    });
    const { runtime_loop: _runtimeLoop, ...missingRuntimeLoop } = status;
    expect(decodeFocusOperatorStatus(missingRuntimeLoop)).toBeNull();
    expect(decodeFocusOperatorStatus({
      ...status,
      warnings: [{ ...status.warnings[0], occurrences: 0 }],
    })).toBeNull();
    const { attention: _attention, ...missingAttention } = status.warnings[0];
    expect(decodeFocusOperatorStatus({
      ...status,
      warnings: [missingAttention],
    })).toBeNull();
    expect(decodeFocusOperatorStatus({
      ...status,
      warnings: [{ ...status.warnings[0], attention: 'future_attention' }],
    })).toBeNull();
  });
});
