import { describe, expect, it, vi } from 'vitest';
import type { ChatTurn } from '../../src/types';
import type { FocusThreadSnapshot } from '../../src/focus/types';
import { FocusApiError } from '../../src/focus/types';
import { projectOperatorStatusPresentation } from '../../src/focus/operatorWarningPresentation';
import { useFocusWebClient } from '../../src/focus/useFocusWebClient';
import { FakeApi, installFocusClientTestHooks, snapshot, thread } from './support';

installFocusClientTestHooks();

describe('Focus Web client', () => {
  it('continues initial target recovery after a stale thread directory read', async () => {
    const api = new FakeApi();
    const originalListThreads = api.listThreads.bind(api);
    let listAttempt = 0;
    vi.spyOn(api, 'listThreads').mockImplementation(async (options = {}) => {
      listAttempt += 1;
      if (listAttempt === 1) {
        throw new FocusApiError('stale list', {
          status: 409,
          code: 'stale_thread_list',
        });
      }
      return originalListThreads(options);
    });
    const client = useFocusWebClient(api);

    await client.load();

    expect(listAttempt).toBe(2);
    expect(client.errorMessage.value).toBe('');
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');
    expect(api.handlers).not.toBeNull();
    client.dispose();
  });

  it('continues initial target recovery after a stale document read', async () => {
    const api = new FakeApi();
    const originalListThreads = api.listThreads.bind(api);
    let listAttempt = 0;
    vi.spyOn(api, 'listThreads').mockImplementation(async (options = {}) => {
      listAttempt += 1;
      if (listAttempt === 1) {
        throw new FocusApiError('stale document read', {
          status: 409,
          code: 'stale_document_read',
        });
      }
      return originalListThreads(options);
    });
    const client = useFocusWebClient(api);

    await client.load();

    expect(listAttempt).toBe(2);
    expect(client.errorMessage.value).toBe('');
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');
    client.dispose();
  });

  it('keeps initial recovery alive when a bounded directory retry is also stale', async () => {
    const api = new FakeApi();
    const originalListThreads = api.listThreads.bind(api);
    let listAttempt = 0;
    vi.spyOn(api, 'listThreads').mockImplementation(async (options = {}) => {
      listAttempt += 1;
      if (listAttempt <= 2) {
        throw new FocusApiError('stale list', {
          status: 409,
          code: 'stale_thread_list',
        });
      }
      return originalListThreads(options);
    });
    const client = useFocusWebClient(api);

    await client.load();

    expect(listAttempt).toBe(2);
    expect(client.errorMessage.value).toBe('');
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');
    await vi.advanceTimersByTimeAsync(250);
    expect(listAttempt).toBeGreaterThanOrEqual(3);
    client.dispose();
  });

  it('retries a stale active snapshot without clearing the selected thread', async () => {
    const api = new FakeApi();
    const selected = { ...thread(), id: 'thread-2', title: 'Thread 2', name: 'Thread 2' };
    api.threads = [thread(), selected];
    const client = useFocusWebClient(api);
    await client.load();
    api.currentSnapshot = { ...snapshot('none', 'thread-2'), thread: selected };
    const originalReadThread = api.readThread.bind(api);
    let selectReadAttempt = 0;
    vi.spyOn(api, 'readThread').mockImplementation(async (threadId = 'thread-1') => {
      if (threadId === 'thread-2') {
        selectReadAttempt += 1;
        if (selectReadAttempt === 1) {
          throw new FocusApiError('stale read', {
            status: 409,
            code: 'stale_thread_read',
          });
        }
      }
      return originalReadThread(threadId);
    });

    await client.selectThread('thread-2');

    expect(selectReadAttempt).toBe(2);
    expect(client.errorMessage.value).toBe('');
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(client.scopeReady.value).toBe(true);
    client.dispose();
  });

  it('keeps a composite reload fail-closed without surfacing a stale directory error', async () => {
    const api = new FakeApi();
    const originalListThreads = api.listThreads.bind(api);
    let listAttempt = 0;
    vi.spyOn(api, 'listThreads').mockImplementation(async (options = {}) => {
      listAttempt += 1;
      if (listAttempt === 2) {
        throw new FocusApiError('stale list', {
          status: 409,
          code: 'stale_thread_list',
        });
      }
      return originalListThreads(options);
    });
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    await client.reloadAll();

    expect(listAttempt).toBe(2);
    expect(client.errorMessage.value).toBe('');
    expect(client.snapshotInvalidated.value).toBe(true);
    client.dispose();
  });

  it('refreshes only next-turn settings for a contiguous settings_changed event', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();
    const counts = {
      meta: api.metaCalls,
      list: api.listCalls,
      read: api.readCalls,
    };
    api.setRuntimeCoordinates('epoch-1', 1);
    api.setNextTurnSettings({ model: 'gpt-test' }, 2);

    api.emit({
      type: 'settings_changed',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: '',
      reason: 'web_next_turn_settings_updated',
    });
    await vi.waitFor(() => expect(api.settingsReadCalls).toBe(1));

    expect(client.nextTurnSettings.value?.model).toBe('gpt-test');
    expect(client.nextTurnSettings.value?.generation).toBe(2);
    expect(api.metaCalls).toBe(counts.meta);
    expect(api.listCalls).toBe(counts.list);
    expect(api.readCalls).toBe(counts.read);
    expect(client.snapshotInvalidated.value).toBe(false);
    expect(client.activeThreadId.value).toBe('thread-1');
    client.dispose();
  });

  it('does not block initial load on the independent operator projection', async () => {
    const api = new FakeApi();
    let releaseOperatorStatus!: () => void;
    api.operatorStatusGate = new Promise((resolve) => {
      releaseOperatorStatus = resolve;
    });
    const client = useFocusWebClient(api);

    await client.load();

    expect(client.initialized.value).toBe(true);
    expect(api.operatorStatusCalls).toBe(1);
    expect(client.operatorStatusFreshness.value).toBe('loading');
    expect(client.operatorStatusStale.value).toBe(false);
    api.handlers?.open?.();
    await Promise.resolve();
    expect(api.operatorStatusCalls).toBe(1);

    releaseOperatorStatus();
    await client.refreshOperatorStatus();
    expect(client.operatorStatusFreshness.value).toBe('fresh');
    expect(client.operatorStatusStale.value).toBe(false);
    client.dispose();
  });

  it('marks operator health stale only after the first probe actually fails', async () => {
    const api = new FakeApi();
    let releaseOperatorStatus!: () => void;
    api.operatorStatusGate = new Promise((resolve) => {
      releaseOperatorStatus = resolve;
    });
    api.operatorStatusError = new Error('health transport failed');
    const client = useFocusWebClient(api);

    await client.load();

    expect(client.operatorStatusFreshness.value).toBe('loading');
    expect(client.operatorStatusStale.value).toBe(false);
    releaseOperatorStatus();
    await client.refreshOperatorStatus();

    expect(client.operatorStatusFreshness.value).toBe('stale');
    expect(client.operatorStatusStale.value).toBe(true);
    client.dispose();
  });

  it('keeps the last valid operator status when a later DTO is malformed', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    await client.refreshOperatorStatus();

    expect(client.operatorStatus.value?.status).toBe('ok');
    api.operatorStatusValue = {
      ...api.operatorStatusValue,
      warnings: [{
        code: 'broken-warning',
        source: 'test',
        message: 'must not install',
        severity: 'warning',
        attention: 'correctness',
        first_seen_at: 1,
        last_seen_at: 1,
        occurrences: 0,
        details: {},
      }],
    };

    await client.refreshOperatorStatus();

    expect(client.operatorStatus.value?.warnings).toEqual([]);
    expect(client.operatorStatusFreshness.value).toBe('stale');
    client.dispose();
  });

  it.each([
    {
      label: 'an initial health probe still in flight',
      status: null,
      freshness: 'loading' as const,
      expected: { warningCount: 0, degradedWithoutDetails: false, warningsAreLastKnown: false },
    },
    {
      label: 'fresh degraded health without warning details',
      status: { ...new FakeApi().operatorStatusValue, status: 'degraded', warnings: [] },
      freshness: 'fresh' as const,
      expected: { warningCount: 0, degradedWithoutDetails: true, warningsAreLastKnown: false },
    },
    {
      label: 'stale last-known warning details',
      status: {
        ...new FakeApi().operatorStatusValue,
        status: 'degraded',
        warnings: [{
          code: 'runtime_queue_delay',
          source: 'RuntimeLoop',
          message: 'slow queue',
          severity: 'warning' as const,
          attention: 'advisory' as const,
          first_seen_at: 1,
          last_seen_at: 1,
          occurrences: 1,
          details: {},
        }],
      },
      freshness: 'stale' as const,
      expected: { warningCount: 1, degradedWithoutDetails: false, warningsAreLastKnown: true },
    },
    {
      label: 'fresh warning details',
      status: {
        ...new FakeApi().operatorStatusValue,
        status: 'degraded',
        warnings: [{
          code: 'runtime_queue_delay',
          source: 'RuntimeLoop',
          message: 'slow queue',
          severity: 'warning' as const,
          attention: 'advisory' as const,
          first_seen_at: 1,
          last_seen_at: 1,
          occurrences: 1,
          details: {},
        }],
      },
      freshness: 'fresh' as const,
      expected: { warningCount: 1, degradedWithoutDetails: false, warningsAreLastKnown: false },
    },
  ])('projects $label honestly', ({ status, freshness, expected }) => {
    expect(projectOperatorStatusPresentation(status, freshness)).toMatchObject(expected);
  });

  it('polls the independent operator projection so warnings appear and expire', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();

    expect(api.operatorStatusCalls).toBe(1);
    expect(client.operatorStatus.value?.status).toBe('ok');
    expect(client.operatorStatusStale.value).toBe(false);

    api.operatorStatusValue = {
      status: 'degraded',
      observed_at: 2,
      poll_after_seconds: 1,
      warnings: [{
        code: 'runtime_task_slow',
        source: 'RuntimeLoop',
        message: 'slow task',
        severity: 'warning',
        attention: 'advisory',
        first_seen_at: 1,
        last_seen_at: 2,
        occurrences: 1,
        details: {},
      }],
      runtime_loop: {},
    };
    await vi.advanceTimersByTimeAsync(1_000);

    expect(client.operatorStatus.value?.warnings).toHaveLength(1);
    expect(client.operatorStatus.value?.status).toBe('degraded');

    api.operatorStatusValue = {
      status: 'ok',
      observed_at: 302,
      poll_after_seconds: 1,
      warnings: [],
      runtime_loop: {},
    };
    await vi.advanceTimersByTimeAsync(1_000);

    expect(client.operatorStatus.value?.warnings).toEqual([]);
    expect(client.operatorStatus.value?.status).toBe('ok');
  });

  it('retains the last warning and marks freshness unknown when a poll fails', async () => {
    const api = new FakeApi();
    api.operatorStatusValue = {
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 1,
      warnings: [{
        code: 'runtime_queue_delay',
        source: 'RuntimeLoop',
        message: 'slow queue',
        severity: 'warning',
        attention: 'advisory',
        first_seen_at: 1,
        last_seen_at: 1,
        occurrences: 1,
        details: {},
      }],
      runtime_loop: {},
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.operatorStatusError = new Error('health transport failed');
    api.operatorStatusValue = {
      status: 'ok',
      observed_at: 2,
      poll_after_seconds: 1,
      warnings: [],
      runtime_loop: {},
    };

    await vi.advanceTimersByTimeAsync(1_000);

    expect(client.operatorStatusStale.value).toBe(true);
    expect(client.operatorStatus.value?.warnings).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(1_000);

    expect(client.operatorStatusStale.value).toBe(false);
    expect(client.operatorStatus.value?.warnings).toEqual([]);
  });

  it('refreshes operator health at every websocket connection boundary', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    expect(api.operatorStatusCalls).toBe(1);

    api.handlers?.open?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(api.operatorStatusCalls).toBe(2);

    api.handlers?.open?.();
    await Promise.resolve();
    await Promise.resolve();
    expect(api.operatorStatusCalls).toBe(3);
  });

  it('stops operator polling after disposal', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    client.dispose();

    await vi.advanceTimersByTimeAsync(10_000);

    expect(api.operatorStatusCalls).toBe(1);
  });

  it('does not refetch a bounded snapshot for an in-sync initial websocket hello', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({ type: 'hello', runtime_epoch: 'epoch-1', revision: 0 });
    await vi.advanceTimersByTimeAsync(100);

    expect(api.metaCalls).toBe(0);
    expect(api.listCalls).toBe(1);
    expect(api.readCalls).toBe(1);
  });

  it('reconciles when websocket hello reports newer projection coordinates', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({ type: 'hello', runtime_epoch: 'epoch-1', revision: 1 });
    await client.reloadAll();

    expect(api.metaCalls).toBe(1);
    expect(api.listCalls).toBe(2);
    expect(api.readCalls).toBe(2);
  });

  it('reloads an invalid wire frame without letting it corrupt coordinates', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.setRuntimeCoordinates('epoch-1', 2);
    api.currentSnapshot = { ...snapshot(), revision: 2 };
    let releaseRead = () => {};
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });

    api.emitInvalid();
    await Promise.resolve();
    await Promise.resolve();

    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.revision.value).toBe(0);
    expect(Number.isNaN(client.revision.value)).toBe(false);
    expect(api.metaCalls).toBe(1);

    releaseRead();
    await client.reloadAll();

    expect(client.snapshotInvalidated.value).toBe(false);
    expect(client.revision.value).toBe(2);
    client.dispose();
  });

  it('reconciles bounded state after a websocket reconnect without duplicating initial load', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    const firstConnection = api.handlers;
    firstConnection?.open?.();

    firstConnection?.close?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(api.handlers).not.toBe(firstConnection);
    api.handlers?.open?.();
    await client.reloadAll();

    expect(api.metaCalls).toBe(1);
    expect(api.listCalls).toBe(2);
    expect(api.readCalls).toBe(2);
  });

  it('stops reconnecting a document token that another tab replaced', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    const firstConnection = api.handlers;
    firstConnection?.open?.();

    // The first closed socket has no way to expose a rejected future
    // handshake.  It schedules one reconnect; the unopened replacement
    // socket then confirms through HTTP that this document capability was
    // rotated by another tab.
    firstConnection?.close?.();
    await vi.advanceTimersByTimeAsync(1000);
    const staleReconnect = api.handlers;
    expect(staleReconnect).not.toBe(firstConnection);
    api.metaError = new FocusApiError('document replaced', {
      status: 409,
      code: 'document_replaced',
    });
    staleReconnect?.close?.();
    await Promise.resolve();
    await Promise.resolve();

    expect(client.documentReloadRequired.value).toBe(true);
    expect(client.connection.value).toBe('disconnected');
    expect(client.canSubmit.value).toBe(false);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(api.handlers).toBe(staleReconnect);
  });

  it('refreshes the active snapshot when its thread is invalidated', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();
    expect(api.readCalls).toBe(1);

    api.emit({ type: 'thread_invalidated', runtime_epoch: 'epoch-1', revision: 1, thread_id: 'thread-1' });
    await vi.advanceTimersByTimeAsync(100);

    expect(api.listCalls).toBe(2);
    expect(api.readCalls).toBe(2);
  });

  it('drops an active selection after an authoritative archive notification', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_invalidated',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      reason: 'thread/archived',
    });
    await Promise.resolve();
    await Promise.resolve();

    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(api.listCalls).toBe(3);
    expect(api.readCalls).toBe(1);
    expect(history.replaceState).toHaveBeenCalled();
  });

  it('does not reload the active snapshot for another thread live delta', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-other',
      detail: { method: 'item/agentMessage/delta', turns: [] },
    });
    await vi.advanceTimersByTimeAsync(300);

    expect(api.listCalls).toBe(1);
    expect(api.readCalls).toBe(1);
  });

  it('writes stream deltas to the assistant segment on the correct side of a steer', async () => {
    const api = new FakeApi();
    const liveTurns: ChatTurn[] = [
      { id: 'turn-live:user', role: 'user', no: 1, text: 'A' },
      {
        id: 'turn-live:assistant',
        role: 'assistant',
        no: 2,
        text: 'partial',
        blocks: [{ kind: 'text', itemId: 'agent-before', text: 'partial' }],
        tools: [],
        status: 'inProgress',
      },
      { id: 'turn-live:user:2', role: 'user', no: 3, text: 'B / steer' },
    ];
    api.currentSnapshot = {
      ...snapshot('self'),
      turns: liveTurns,
      active_turn_id: 'turn-live',
      active_turn_status: 'inProgress',
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'item/agentMessage/delta',
        stream_delta: {
          turn_id: 'turn-live', item_id: 'agent-after', kind: 'text', delta: 'continued',
        },
      },
    });
    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-1',
      detail: {
        method: 'item/agentMessage/delta',
        stream_delta: {
          turn_id: 'turn-live', item_id: 'agent-before', kind: 'text', delta: ' (late)',
        },
      },
    });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(client.snapshot.value?.turns[1]?.text).toBe('partial (late)');
    expect(client.snapshot.value?.turns[3]?.id).toBe('turn-live:assistant:2');
    expect(client.snapshot.value?.turns[3]?.text).toBe('continued');

    // A still-live item projection may lag the deltas already rendered in
    // this document.  Its empty skeleton must not erase either side of the
    // steer; completed item/turn snapshots remain authoritative instead.
    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 3,
      thread_id: 'thread-1',
      detail: {
        method: 'item/started',
        turns: [
          { ...liveTurns[0]! },
          {
            ...liveTurns[1]!, text: '',
            blocks: [{ kind: 'text', itemId: 'agent-before', text: '' }],
          },
          { ...liveTurns[2]! },
          {
            id: 'turn-live:assistant:2',
            role: 'assistant',
            no: 4,
            text: '',
            blocks: [{ kind: 'text', itemId: 'agent-after', text: '' }],
            tools: [],
            status: 'inProgress',
          },
        ],
      },
    });
    expect(client.snapshot.value?.turns[1]?.text).toBe('partial (late)');
    expect(client.snapshot.value?.turns[3]?.text).toBe('continued');
  });

  it('refreshes only the directory when another thread lifecycle changes', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_invalidated',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-other',
    });
    await vi.advanceTimersByTimeAsync(300);

    expect(api.listCalls).toBe(2);
    expect(api.readCalls).toBe(1);
  });

  it('restores current-process lifecycle warnings and reconciles only the matching target', async () => {
    const api = new FakeApi();
    api.threads = [
      thread(),
      { ...thread(), id: 'thread-2', title: 'Second', name: 'Second' },
    ];
    api.setUnknownLifecycleMutations([
      {
        thread_id: 'thread-1', mutation_id: 'mutation-1', operation: 'unarchive', verification: null,
      },
      {
        thread_id: 'thread-2',
        mutation_id: 'mutation-2',
        operation: 'delete',
        verification: { state: 'deleted', verification_id: 'verification-2' },
      },
    ]);
    api.currentSnapshot = {
      ...snapshot(),
      mutation_unknown: {
        mutation_id: 'mutation-1', operation: 'unarchive', reconciling: true,
      },
    };
    const client = useFocusWebClient(api);
    await client.load();
    expect(client.unknownLifecycleMutations.value).toEqual([
      {
        threadId: 'thread-1', mutationId: 'mutation-1', operation: 'unarchive', verification: null,
      },
      {
        threadId: 'thread-2',
        mutationId: 'mutation-2',
        operation: 'delete',
        verification: { state: 'deleted', verification_id: 'verification-2' },
      },
    ]);
    expect(sessionStorage.getItem('focus-web.unknown-lifecycle:tab-1')).toBeNull();

    api.emit({
      type: 'mutation_reconciled',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      reason: 'unarchive',
      detail: {
        mutation_id: 'mutation-1', operation: 'unarchive', disposition: 'effect_observed',
      },
    });

    expect(client.unknownLifecycleMutations.value).toEqual([{
      threadId: 'thread-2',
      mutationId: 'mutation-2',
      operation: 'delete',
      verification: { state: 'deleted', verification_id: 'verification-2' },
    }]);
    expect(sessionStorage.getItem('focus-web.unknown-lifecycle:tab-1')).toBeNull();
  });

  it('reloads snapshots on a revision gap and stops reconnecting after session expiry', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({ type: 'thread_invalidated', runtime_epoch: 'epoch-1', revision: 3, thread_id: 'thread-1' });
    await Promise.resolve();
    await Promise.resolve();
    expect(api.metaCalls).toBe(1);

    api.emit({ type: 'session_expired', runtime_epoch: 'epoch-1', revision: 4 });
    expect(client.authRequired.value).toBe(true);
    expect(client.connection.value).toBe('disconnected');
  });

  it('converges to a newer snapshot revision when a gap reload buffers no events', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.setRuntimeCoordinates('epoch-1', 3);
    api.currentSnapshot = {
      ...snapshot(),
      revision: 3,
      thread: { ...thread(), name: 'Reconciled name', title: 'Reconciled name' },
    };
    api.emit({ type: 'thread_invalidated', runtime_epoch: 'epoch-1', revision: 3, thread_id: 'thread-1' });
    await client.reloadAll();

    expect(client.revision.value).toBe(3);
    expect(client.snapshot.value?.thread.name).toBe('Reconciled name');
    const reloadCount = api.metaCalls;

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 4,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Next name' },
    });

    expect(client.revision.value).toBe(4);
    expect(client.snapshot.value?.thread.name).toBe('Next name');
    expect(api.metaCalls).toBe(reloadCount);
  });

  it.each(['metadata', 'thread list', 'thread snapshot'] as const)(
    'keeps navigation projection atomic while settings stay independent when %s validation fails',
    async (failedContract) => {
      const api = new FakeApi();
      const client = useFocusWebClient(api);
      await client.load();
      api.handlers?.open?.();

      const committedMeta = client.meta.value;
      const committedThreads = client.threads.value;
      const committedSnapshot = client.snapshot.value;
      const committedProfile = {
        thinking: client.thinking.value,
        approvalPolicy: client.approvalPolicy.value,
        permissionsProfileId: client.permissionsProfileId.value,
      };
      const committedCoordinates = {
        runtimeEpoch: client.runtimeEpoch.value,
        revision: client.revision.value,
      };

      api.meta = vi.fn(async () => {
        if (failedContract === 'metadata') {
          throw new FocusApiError('malformed metadata', {
            status: 502,
            code: 'invalid_gateway_response',
          });
        }
        return {
          ...committedMeta!,
          instance: 'must-not-install',
          revision: 9,
          writer_profile: {
            ...committedMeta!.writer_profile,
            working_dir: '/must-not-install',
          },
          next_turn_settings: {
            ...committedMeta!.next_turn_settings,
            generation: committedMeta!.next_turn_settings.generation + 1,
            reasoning_effort: 'ultra',
            approval_policy: 'untrusted',
            permissions_profile_id: ':read-only',
          },
        };
      });
      api.listThreads = vi.fn(async () => {
        if (failedContract === 'thread list') {
          throw new FocusApiError('malformed thread list', {
            status: 502,
            code: 'invalid_gateway_response',
          });
        }
        return {
          runtime_epoch: 'epoch-1',
          revision: 9,
          scope: 'global' as const,
          archived: false,
          limit: 100,
          truncated: false,
          threads: [{
            ...thread(),
            name: 'must-not-install',
            title: 'must-not-install',
          }],
        };
      });
      api.readThread = vi.fn(async () => {
        throw new FocusApiError('malformed thread snapshot', {
          status: 502,
          code: 'invalid_gateway_response',
        });
      });

      await client.reloadAll();

      if (failedContract === 'metadata') expect(client.meta.value).toBe(committedMeta);
      expect(client.threads.value).toBe(committedThreads);
      expect(client.snapshot.value).toBe(committedSnapshot);
      expect(client.meta.value?.instance).toBe('default');
      expect(client.threads.value[0]?.name).toBe('Demo');
      expect(client.snapshot.value?.thread.name).toBe('Demo');
      const resultingSettings = {
        thinking: client.thinking.value,
        approvalPolicy: client.approvalPolicy.value,
        permissionsProfileId: client.permissionsProfileId.value,
      };
      if (failedContract === 'metadata') expect(resultingSettings).toEqual(committedProfile);
      else expect(resultingSettings).toEqual({
        thinking: 'ultra',
        approvalPolicy: 'untrusted',
        permissionsProfileId: ':read-only',
      });
      expect({
        runtimeEpoch: client.runtimeEpoch.value,
        revision: client.revision.value,
      }).toEqual(committedCoordinates);
      expect(client.snapshotInvalidated.value).toBe(true);
      client.dispose();
    },
  );

  it('replays an event buffered while a same-revision HTTP snapshot is installed', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    let releaseRead = () => {};
    api.currentSnapshot = {
      ...snapshot(),
      revision: 1,
      thread: { ...thread(), name: 'Stale name', title: 'Stale name' },
    };
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    const reload = client.reloadAll();
    await vi.waitFor(() => expect(api.readCalls).toBe(2));

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Live name' },
    });
    releaseRead();
    await reload;

    expect(client.snapshot.value?.thread.name).toBe('Live name');
    expect(client.revision.value).toBe(1);
  });

  it('does not let snapshot coordinates skip a delayed websocket event', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    let releaseRead = () => {};
    api.currentSnapshot = {
      ...snapshot(),
      revision: 2,
      thread: { ...thread(), name: 'Snapshot name', title: 'Snapshot name' },
    };
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    const reload = client.reloadAll();
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Buffered name' },
    });
    releaseRead();
    await reload;
    expect(client.revision.value).toBe(1);

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Latest name' },
    });

    expect(client.snapshot.value?.thread.name).toBe('Latest name');
    expect(client.revision.value).toBe(2);
  });

  it('discards a stale active-thread refresh superseded by a websocket event', async () => {
    const api = new FakeApi();
    api.currentSnapshot = {
      ...snapshot('self'),
      thread: { ...thread('self'), status: 'active' },
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    let releaseRead = () => {};
    api.currentSnapshot = {
      ...api.currentSnapshot,
      revision: 0,
      thread: { ...api.currentSnapshot.thread, name: 'Stale name', title: 'Stale name' },
    };
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    const interrupt = client.interrupt();
    await Promise.resolve();
    expect(api.readCalls).toBe(2);

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Live name' },
    });
    releaseRead();
    await interrupt;

    expect(client.snapshot.value?.thread.name).toBe('Live name');
    expect(client.revision.value).toBe(1);
  });

  it('installs a newly selected thread when its load event arrives before the HTTP snapshot', async () => {
    const api = new FakeApi();
    const otherThread = {
      ...thread(),
      id: 'thread-2',
      name: 'Other thread',
      title: 'Other thread',
    };
    api.threads = [thread(), otherThread];
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    let releaseRead = () => {};
    api.currentSnapshot = {
      ...snapshot(),
      revision: 1,
      thread: otherThread,
    };
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    const selection = client.selectThread('thread-2');
    await Promise.resolve();

    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value).toBeNull();

    api.emit({
      type: 'thread_invalidated',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-2',
      reason: 'web_thread_loaded',
    });
    releaseRead();
    await selection;

    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(client.snapshot.value?.thread.name).toBe('Other thread');
    expect(client.revision.value).toBe(1);
  });

  it('switches from an active thread A to thread B without dispatching interrupt', async () => {
    const api = new FakeApi();
    const activeThread = {
      ...thread(),
      status: 'active' as const,
    };
    const otherThread = {
      ...thread(),
      id: 'thread-2',
      name: 'Other thread',
      title: 'Other thread',
    };
    api.threads = [activeThread, otherThread];
    api.currentSnapshot = {
      ...snapshot(),
      thread: activeThread,
      active_turn_id: 'turn-a',
      active_turn_status: 'inProgress',
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();
    api.currentSnapshot = {
      ...snapshot('none', 'thread-2', 2),
      thread: otherThread,
    };

    await client.selectThread('thread-2');

    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(api.readCalls).toBe(2);
    expect(api.interruptCalls).toEqual([]);
  });

  it('keeps the newest rapid thread selection when older HTTP reads finish later', async () => {
    const api = new FakeApi();
    const secondThread = {
      ...thread(),
      id: 'thread-2',
      name: 'Second',
      title: 'Second',
    };
    const thirdThread = {
      ...thread(),
      id: 'thread-3',
      name: 'Third',
      title: 'Third',
    };
    api.threads = [thread(), secondThread, thirdThread];
    const client = useFocusWebClient(api);
    await client.load();

    const pending = new Map<string, {
      intent: number;
      resolve: (value: FocusThreadSnapshot) => void;
    }>();
    api.readThread = vi.fn((threadId: string, intent = 0) => new Promise<FocusThreadSnapshot>((resolve) => {
      pending.set(threadId, { intent, resolve });
    }));

    const selectSecond = client.selectThread('thread-2');
    await Promise.resolve();
    const selectThird = client.selectThread('thread-3');
    await Promise.resolve();
    api.setSelectedThreadId('thread-3', 4);
    pending.get('thread-3')?.resolve({
      ...snapshot('none', 'thread-3', 4),
      thread: thirdThread,
    });
    await selectThird;
    pending.get('thread-2')?.resolve({
      ...snapshot('none', 'thread-2', 3),
      thread: secondThread,
    });
    await selectSecond;

    expect(pending.get('thread-2')?.intent).toBe(1);
    expect(pending.get('thread-3')?.intent).toBe(2);
    expect(client.activeThreadId.value).toBe('thread-3');
    expect(client.snapshot.value?.thread.id).toBe('thread-3');
  });

  it('projects server action capabilities per session instead of guessing observer writes', async () => {
    const api = new FakeApi();
    const observerThread = {
      ...thread('other'),
      id: 'thread-2',
      name: 'Observed elsewhere',
      title: 'Observed elsewhere',
      action_capabilities: {
        rename: false,
        archive: false,
        unarchive: false,
        delete: false,
        compact: false,
        fork: false,
        export: false,
        review: false,
        goal: false,
      },
    };
    api.threads = [thread(), observerThread];
    const client = useFocusWebClient(api);
    await client.load();

    expect(client.sessions.value.find((session) => session.id === 'thread-2')?.actionCapabilities).toEqual({
      rename: false,
      archive: false,
      fork: false,
      export: false,
      review: false,
      goal: false,
    });

    api.currentSnapshot = {
      ...snapshot('other'),
      thread: observerThread,
    };
    await client.selectThread('thread-2');

    expect(client.activeSessionActionCapabilities.value).toEqual({
      rename: false,
      archive: false,
      fork: false,
      export: false,
      review: false,
      goal: false,
    });
    expect(client.canCompact.value).toBe(false);
  });

  it('does not carry a prior epoch thread revision watermark into a new epoch snapshot', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.setRuntimeCoordinates('epoch-1', 99);
    api.currentSnapshot = {
      ...snapshot(),
      revision: 99,
      thread: { ...thread(), name: 'Epoch A snapshot', title: 'Epoch A snapshot' },
    };
    api.emit({ type: 'hello', runtime_epoch: 'epoch-1', revision: 99 });
    await client.reloadAll();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 100,
      thread_id: 'thread-1',
      detail: { method: 'thread/name/updated', thread_name: 'Epoch A event' },
    });
    expect(client.snapshot.value?.thread.name).toBe('Epoch A event');

    api.setRuntimeCoordinates('epoch-2', 1);
    api.currentSnapshot = {
      ...snapshot(),
      runtime_epoch: 'epoch-2',
      revision: 1,
      thread: { ...thread(), name: 'Epoch B snapshot', title: 'Epoch B snapshot' },
    };
    api.emit({ type: 'hello', runtime_epoch: 'epoch-2', revision: 1 });
    await client.reloadAll();

    expect(client.runtimeEpoch.value).toBe('epoch-2');
    expect(client.snapshot.value?.thread.name).toBe('Epoch B snapshot');
  });

  it('does not let an old-epoch HTTP response restore a replaced runtime epoch', async () => {
    const api = new FakeApi();
    api.currentSnapshot = {
      ...snapshot('self'),
      thread: { ...thread('self'), status: 'active' },
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    let releaseRead = () => {};
    api.readGate = new Promise<void>((resolve) => { releaseRead = resolve; });
    const interrupt = client.interrupt();
    await Promise.resolve();
    expect(api.readCalls).toBe(2);

    api.setRuntimeCoordinates('epoch-2', 0);
    api.currentSnapshot = {
      ...snapshot('self'),
      runtime_epoch: 'epoch-2',
      thread: { ...thread('self'), name: 'New runtime', title: 'New runtime' },
    };
    api.emit({ type: 'hello', runtime_epoch: 'epoch-2', revision: 0 });
    await Promise.resolve();
    await Promise.resolve();
    releaseRead();
    await interrupt;
    await client.reloadAll();

    expect(client.runtimeEpoch.value).toBe('epoch-2');
    expect(client.snapshot.value?.thread.name).toBe('New runtime');
  });
});
