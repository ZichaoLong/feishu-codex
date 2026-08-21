import { afterEach, describe, expect, it, vi } from 'vitest';
import { FocusWebApi } from '../src/focus/api';
import { createFocusBackendResetTransaction } from '../src/focus/focusBackendReset';
import {
  decodeFocusBackendResetPreview,
  decodeFocusBackendResetResult,
} from '../src/focus/httpResponseDecoder';
import {
  FocusApiError,
  type FocusBackendResetPreview,
  type FocusBackendResetResult,
} from '../src/focus/types';

function preview(
  status: FocusBackendResetPreview['status'] = 'available',
  generation = status === 'unavailable' ? 0 : 7,
): FocusBackendResetPreview {
  return {
    instance: 'default',
    status,
    reason_code: '',
    reason_text: 'safe',
    expected_connection_generation: generation,
    pending_request_count: 0,
    running_binding_count: 0,
    attached_binding_count: 1,
    active_loaded_thread_count: 0,
    loaded_thread_count: 1,
    runtime_verification_failed: false,
  };
}

function result(force = false): FocusBackendResetResult {
  return {
    force,
    detached_binding_count: 1,
    interrupted_binding_count: 0,
    retired_request_count: 2,
    purged_thread_count: 1,
    projection_warnings: ['card projection unavailable'],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

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

function transactionHarness(initialPreview = preview()) {
  const api = {
    backendResetPreview: vi.fn(async () => initialPreview),
    backendResetExecute: vi.fn(async (input: {
      force: boolean;
      expectedConnectionGeneration: number;
    }) => result(input.force)),
  };
  const reloadAll = vi.fn(async () => undefined);
  const transaction = createFocusBackendResetTransaction({ api, reloadAll });
  return { api, reloadAll, transaction };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('Focus backend reset wire admission', () => {
  it('admits only complete preview status/generation shapes', () => {
    const valid = preview();
    expect(decodeFocusBackendResetPreview(valid)).toBe(valid);
    expect(decodeFocusBackendResetPreview({
      ...valid,
      status: 'force-only',
    })).not.toBeNull();
    expect(decodeFocusBackendResetPreview({
      ...valid,
      status: 'unavailable',
      expected_connection_generation: 0,
    })).not.toBeNull();

    const invalid = [
      { ...valid, extra: true },
      { ...valid, status: 'blocked' },
      { ...valid, expected_connection_generation: 0 },
      { ...valid, expected_connection_generation: true },
      { ...valid, expected_connection_generation: Number.MAX_SAFE_INTEGER + 1 },
      { ...valid, status: 'unavailable', expected_connection_generation: 7 },
      { ...valid, pending_request_count: -1 },
      { ...valid, running_binding_count: 0.5 },
      Object.fromEntries(Object.entries(valid).filter(([key]) => key !== 'instance')),
    ];
    for (const value of invalid) {
      expect(decodeFocusBackendResetPreview(value)).toBeNull();
    }
  });

  it('admits only the exact requested-force Web result without backend secrets', () => {
    const valid = result(true);
    expect(decodeFocusBackendResetResult(valid, true)).toBe(valid);

    const invalid = [
      { ...valid, app_server_url: 'ws://127.0.0.1:8765' },
      { ...valid, force: false },
      { ...valid, retired_request_count: true },
      { ...valid, purged_thread_count: -1 },
      { ...valid, detached_binding_count: 1.5 },
      { ...valid, projection_warnings: [''] },
      Object.fromEntries(Object.entries(valid).filter(([key]) => key !== 'force')),
    ];
    for (const value of invalid) {
      expect(decodeFocusBackendResetResult(value, true)).toBeNull();
    }
  });
});

describe('Focus backend reset API', () => {
  it('uses the canonical GET and one exact strict-decoded POST', async () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('history', { state: null, replaceState: vi.fn() });
    vi.stubGlobal('window', {
      location: {
        hash: '',
        pathname: '/',
        search: '',
        protocol: 'http:',
        host: '127.0.0.1:8766',
      },
    });
    const fetchMock = vi.fn(async (_path: string, options?: RequestInit) => {
      const force = options?.body
        ? Boolean((JSON.parse(String(options.body)) as { force?: unknown }).force)
        : false;
      return new Response(JSON.stringify(
        options?.method === 'POST' ? result(force) : preview(),
      ), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    Object.assign(api as unknown as Record<string, unknown>, {
      _clientId: 'web-1',
      documentToken: 'document-token-1',
      csrfToken: 'csrf-1',
    });

    await expect(api.backendResetPreview()).resolves.toEqual(preview());
    await expect(api.backendResetExecute({
      force: true,
      expectedConnectionGeneration: 7,
    })).resolves.toEqual(result(true));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0]).toEqual([
      '/api/backend-reset',
      expect.objectContaining({ method: 'GET', body: undefined }),
    ]);
    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/backend-reset');
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: 'POST',
      credentials: 'same-origin',
      body: JSON.stringify({
        force: true,
        expected_connection_generation: 7,
      }),
      headers: {
        'Content-Type': 'application/json',
        'X-Focus-Web-Client': 'web-1',
        'X-Focus-Web-Document': 'document-token-1',
        'X-Focus-Web-Csrf': 'csrf-1',
      },
    });
  });

  it('classifies a malformed successful POST response as invalid gateway data', async () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    vi.stubGlobal('history', { state: null, replaceState: vi.fn() });
    vi.stubGlobal('window', {
      location: {
        hash: '',
        pathname: '/',
        search: '',
        protocol: 'http:',
        host: '127.0.0.1:8766',
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      ...result(true),
      app_server_url: 'ws://must-not-be-admitted',
    }), { status: 200 })));
    const api = new FocusWebApi();
    Object.assign(api as unknown as Record<string, unknown>, {
      _clientId: 'web-1',
      documentToken: 'document-token-1',
      csrfToken: 'csrf-1',
    });

    await expect(api.backendResetExecute({
      force: true,
      expectedConnectionGeneration: 7,
    })).rejects.toMatchObject({
      status: 502,
      code: 'invalid_gateway_response',
    });
  });
});

describe('Focus backend reset document transaction', () => {
  it('installs only the latest preview and ignores a response after dispose', async () => {
    const h = transactionHarness();
    const first = deferred<FocusBackendResetPreview>();
    const second = deferred<FocusBackendResetPreview>();
    h.api.backendResetPreview
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);

    const firstRequest = h.transaction.refreshPreview();
    const secondRequest = h.transaction.refreshPreview();
    first.resolve(preview('available', 7));
    await expect(firstRequest).resolves.toBeNull();
    expect(h.transaction.preview.value).toBeNull();
    const latest = preview('force-only', 8);
    second.resolve(latest);
    await expect(secondRequest).resolves.toBe(latest);
    expect(h.transaction.preview.value).toBe(latest);

    const late = deferred<FocusBackendResetPreview>();
    h.api.backendResetPreview.mockImplementationOnce(() => late.promise);
    const lateRequest = h.transaction.refreshPreview();
    h.transaction.dispose();
    late.resolve(preview('available', 9));
    await expect(lateRequest).resolves.toBeNull();
    expect(h.transaction.disposed.value).toBe(true);
    expect(h.transaction.preview.value).toBeNull();
    expect(h.transaction.previewPending.value).toBe(false);
  });

  it('requires the exact installed object and its unchanged generation/status', async () => {
    const h = transactionHarness();
    const oldPreview = await h.transaction.refreshPreview();
    expect(oldPreview).not.toBeNull();
    if (oldPreview === null) throw new Error('preview was not installed');
    const installed = preview();
    h.api.backendResetPreview.mockResolvedValueOnce(installed);
    await h.transaction.refreshPreview();

    await expect(h.transaction.execute(oldPreview)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'preview-replaced',
    });

    await expect(h.transaction.execute({ ...installed })).resolves.toEqual({
      disposition: 'not-started',
      reason: 'preview-replaced',
    });
    installed.expected_connection_generation = 8;
    await expect(h.transaction.execute(installed)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'preview-replaced',
    });
    installed.expected_connection_generation = 7;
    installed.status = 'force-only';
    await expect(h.transaction.execute(installed)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'preview-replaced',
    });
    expect(h.api.backendResetExecute).not.toHaveBeenCalled();
  });

  it('maps available to safe, force-only to force, and unavailable to zero POST', async () => {
    for (const [status, expectedForce] of [
      ['available', false],
      ['force-only', true],
    ] as const) {
      const h = transactionHarness(preview(status));
      const captured = await h.transaction.refreshPreview();
      if (captured === null) throw new Error('preview was not installed');

      await expect(h.transaction.execute(captured)).resolves.toMatchObject({
        disposition: 'succeeded',
        result: result(expectedForce),
      });
      expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
      expect(h.api.backendResetExecute).toHaveBeenCalledWith({
        force: expectedForce,
        expectedConnectionGeneration: 7,
      });
      expect(h.reloadAll).toHaveBeenCalledOnce();
      expect(h.transaction.preview.value).toBeNull();
      await expect(h.transaction.execute(captured)).resolves.toEqual({
        disposition: 'not-started',
        reason: 'preview-replaced',
      });
      expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
    }

    const unavailable = transactionHarness(preview('unavailable'));
    const captured = await unavailable.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');
    await expect(unavailable.transaction.execute(captured)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'preview-unavailable',
    });
    expect(unavailable.api.backendResetExecute).not.toHaveBeenCalled();
  });

  it('allows one POST at a time and ignores a late result after dispose', async () => {
    const h = transactionHarness();
    const response = deferred<FocusBackendResetResult>();
    h.api.backendResetExecute.mockImplementationOnce(() => response.promise);
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    const execution = h.transaction.execute(captured);
    await expect(h.transaction.execute(captured)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'already-executing',
    });
    h.transaction.dispose();
    response.resolve(result(false));

    await expect(execution).resolves.toEqual({ disposition: 'ignored' });
    expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
    expect(h.reloadAll).not.toHaveBeenCalled();
    expect(h.transaction.result.value).toBeNull();
    expect(h.transaction.outcomeUnknown.value).toBe(false);
  });

  it('refreshes once after typed stale without ever replaying the POST', async () => {
    const h = transactionHarness();
    const fresh = preview('force-only', 8);
    h.api.backendResetPreview
      .mockResolvedValueOnce(preview('available', 7))
      .mockResolvedValueOnce(fresh);
    h.api.backendResetExecute.mockRejectedValueOnce(new FocusApiError('stale', {
      status: 409,
      code: 'backend_reset_stale',
    }));
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    await expect(h.transaction.execute(captured)).resolves.toMatchObject({
      disposition: 'known-no-effect',
      refreshedPreview: fresh,
    });
    expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
    expect(h.api.backendResetPreview).toHaveBeenCalledTimes(2);
    expect(h.transaction.preview.value).toBe(fresh);
    expect(h.transaction.knownNoEffectError.value?.code).toBe('backend_reset_stale');
    expect(h.reloadAll).not.toHaveBeenCalled();
  });

  it('keeps a failed stale refresh distinct from the known-no-effect POST', async () => {
    const h = transactionHarness();
    const refreshError = new FocusApiError('document replaced', {
      status: 409,
      code: 'document_replaced',
    });
    h.api.backendResetPreview
      .mockResolvedValueOnce(preview('available', 7))
      .mockRejectedValueOnce(refreshError);
    h.api.backendResetExecute.mockRejectedValueOnce(new FocusApiError('stale', {
      status: 409,
      code: 'backend_reset_stale',
    }));
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    await expect(h.transaction.execute(captured)).resolves.toMatchObject({
      disposition: 'known-no-effect',
      refreshedPreview: null,
      refreshError,
    });
    expect(h.transaction.previewError.value).toBe(refreshError);
    expect(h.transaction.outcomeUnknown.value).toBe(false);
    expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
    expect(h.api.backendResetPreview).toHaveBeenCalledTimes(2);
  });

  it('treats every other 4xx as known no-effect without an automatic GET', async () => {
    const h = transactionHarness();
    h.api.backendResetExecute.mockRejectedValueOnce(new FocusApiError('forbidden', {
      status: 403,
      code: 'csrf_failed',
    }));
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    await expect(h.transaction.execute(captured)).resolves.toMatchObject({
      disposition: 'known-no-effect',
      refreshedPreview: null,
    });
    expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
    expect(h.api.backendResetPreview).toHaveBeenCalledOnce();
    expect(h.transaction.preview.value).toBeNull();
    expect(h.transaction.outcomeUnknown.value).toBe(false);
    expect(h.reloadAll).not.toHaveBeenCalled();
  });

  it.each([
    ['transport loss', new TypeError('network lost')],
    ['server error', new FocusApiError('server failed', {
      status: 500,
      code: 'internal_error',
    })],
    ['non-JSON or malformed success', new FocusApiError('invalid response', {
      status: 502,
      code: 'invalid_gateway_response',
    })],
  ])('latches %s as document-lifetime outcome unknown', async (_name, error) => {
    const h = transactionHarness();
    h.api.backendResetExecute.mockRejectedValueOnce(error);
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    await expect(h.transaction.execute(captured)).resolves.toMatchObject({
      disposition: 'outcome-unknown',
      error,
    });
    expect(h.transaction.outcomeUnknown.value).toBe(true);
    expect(h.transaction.outcomeUnknownError.value).toBe(error);
    expect(h.reloadAll).not.toHaveBeenCalled();

    const later = await h.transaction.refreshPreview();
    if (later === null) throw new Error('later preview was not installed');
    await expect(h.transaction.execute(later)).resolves.toEqual({
      disposition: 'not-started',
      reason: 'outcome-unknown',
    });
    expect(h.api.backendResetExecute).toHaveBeenCalledOnce();
  });

  it('locks typed success before reload and never downgrades a reload failure', async () => {
    const h = transactionHarness();
    const reload = deferred<void>();
    h.reloadAll.mockImplementationOnce(() => reload.promise);
    const captured = await h.transaction.refreshPreview();
    if (captured === null) throw new Error('preview was not installed');

    const execution = h.transaction.execute(captured);
    await vi.waitFor(() => {
      expect(h.transaction.result.value).toEqual(result(false));
      expect(h.reloadAll).toHaveBeenCalledOnce();
    });
    reload.reject(new Error('reload failed'));
    await expect(execution).resolves.toMatchObject({
      disposition: 'succeeded',
      result: result(false),
      reloadError: expect.any(Error),
    });
    expect(h.transaction.outcomeUnknown.value).toBe(false);
    expect(h.transaction.result.value).toEqual(result(false));
    expect(h.transaction.reloadError.value).toBeInstanceOf(Error);

    const nextPreview = preview('force-only', 8);
    h.api.backendResetPreview.mockResolvedValueOnce(nextPreview);
    await h.transaction.refreshPreview();
    expect(h.transaction.result.value).toEqual(result(false));

    const nextResult = deferred<FocusBackendResetResult>();
    h.api.backendResetExecute.mockImplementationOnce(() => nextResult.promise);
    const nextExecution = h.transaction.execute(nextPreview);
    expect(h.transaction.result.value).toBeNull();
    nextResult.resolve(result(true));
    await expect(nextExecution).resolves.toMatchObject({ disposition: 'succeeded' });
    expect(h.api.backendResetExecute).toHaveBeenCalledTimes(2);
  });

  it('has no clear or retry authority on its public surface', () => {
    const { transaction } = transactionHarness();
    expect(transaction).not.toHaveProperty('clear');
    expect(transaction).not.toHaveProperty('retry');
    expect(transaction).not.toHaveProperty('clearOutcomeUnknown');
  });
});
