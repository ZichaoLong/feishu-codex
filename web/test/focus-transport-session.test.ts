import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FocusEventHandlers } from '../src/focus/api';
import {
  createFocusTransportSession,
  type FocusHandshakeProbeDisposition,
  type FocusTransportSessionCallbacks,
} from '../src/focus/focusTransportSession';

interface TestSocket {
  handlers: FocusEventHandlers;
  close: ReturnType<typeof vi.fn>;
}

function transportFixture(overrides: Partial<FocusTransportSessionCallbacks> = {}) {
  const sockets: TestSocket[] = [];
  const connected = vi.fn();
  const handshakeReady = vi.fn();
  const refreshProjection = vi.fn(async () => {});
  const refreshThreadList = vi.fn(async () => {});
  const reloadProjection = vi.fn();
  const scheduledErrors = vi.fn();
  const callbacks: FocusTransportSessionCallbacks = {
    mayConnect: () => true,
    openEventSocket: (handlers) => {
      const socket: TestSocket = { handlers, close: vi.fn() };
      sockets.push(socket);
      return socket as unknown as WebSocket;
    },
    probeEventAccess: async () => {},
    onHandshakeProbeError: () => 'retry',
    onConnectionError: scheduledErrors,
    onSocketOpened: connected,
    onHandshakeReady: handshakeReady,
    onEvent: vi.fn(),
    onInvalidEvent: vi.fn(),
    refreshProjection,
    refreshThreadList,
    reloadProjection,
    mayRetryProjectionReload: () => true,
    onScheduledTaskError: scheduledErrors,
    ...overrides,
  };
  const session = createFocusTransportSession(callbacks);
  return {
    callbacks,
    connected,
    handshakeReady,
    refreshProjection,
    refreshThreadList,
    reloadProjection,
    scheduledErrors,
    session,
    sockets,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Focus transport session', () => {
  it('owns socket replacement, connection state, and bounded reconnect backoff', async () => {
    const fixture = transportFixture();

    fixture.session.connect();
    expect(fixture.sockets).toHaveLength(1);
    expect(fixture.session.snapshot.value.connection).toBe('connecting');

    fixture.sockets[0]?.handlers.open?.();
    expect(fixture.session.snapshot.value.connection).toBe('connected');
    expect(fixture.connected).toHaveBeenCalledOnce();
    expect(fixture.handshakeReady).not.toHaveBeenCalled();
    fixture.sockets[0]?.handlers.event({
      type: 'hello', runtime_epoch: 'epoch-1', revision: 0,
    });
    fixture.sockets[0]?.handlers.event({
      type: 'hello', runtime_epoch: 'epoch-1', revision: 0,
    });
    expect(fixture.handshakeReady).toHaveBeenCalledOnce();
    expect(fixture.handshakeReady).toHaveBeenLastCalledWith(false);

    fixture.sockets[0]?.handlers.close?.();
    expect(fixture.session.snapshot.value.connection).toBe('disconnected');
    expect(fixture.session.snapshot.value.reconnectScheduled).toBe(true);
    await vi.advanceTimersByTimeAsync(999);
    expect(fixture.sockets).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fixture.sockets).toHaveLength(2);
    expect(fixture.session.snapshot.value.reconnectScheduled).toBe(false);

    fixture.sockets[1]?.handlers.open?.();
    expect(fixture.connected).toHaveBeenCalledTimes(2);
    expect(fixture.handshakeReady).toHaveBeenCalledOnce();
    fixture.sockets[1]?.handlers.event({
      type: 'hello', runtime_epoch: 'epoch-1', revision: 0,
    });
    expect(fixture.handshakeReady).toHaveBeenCalledTimes(2);
    expect(fixture.handshakeReady).toHaveBeenLastCalledWith(true);
    expect(fixture.session.snapshot.value.hasOpenedEventSocket).toBe(true);
  });

  it('does not treat a socket open without hello as a completed handshake', async () => {
    const fixture = transportFixture();

    fixture.session.connect();
    fixture.sockets[0]?.handlers.open?.();
    fixture.sockets[0]?.handlers.close?.();
    await vi.advanceTimersByTimeAsync(1_000);
    fixture.sockets[1]?.handlers.open?.();
    fixture.sockets[1]?.handlers.event({
      type: 'hello', runtime_epoch: 'epoch-1', revision: 0,
    });

    expect(fixture.connected).toHaveBeenCalledTimes(2);
    expect(fixture.handshakeReady).toHaveBeenCalledOnce();
    expect(fixture.handshakeReady).toHaveBeenCalledWith(false);
  });

  it('probes an unopened handshake and honors a fail-closed stop disposition', async () => {
    const probeError = new Error('document capability replaced');
    const probeEventAccess = vi.fn(async () => {
      throw probeError;
    });
    const probeErrors: unknown[] = [];
    const fixture = transportFixture({
      probeEventAccess,
      onHandshakeProbeError: (error): FocusHandshakeProbeDisposition => {
        probeErrors.push(error);
        return 'stop';
      },
    });

    fixture.session.connect();
    fixture.sockets[0]?.handlers.close?.();
    await Promise.resolve();
    await Promise.resolve();

    expect(probeEventAccess).toHaveBeenCalledOnce();
    expect(probeErrors).toEqual([probeError]);
    expect(fixture.session.snapshot.value.connection).toBe('disconnected');
    expect(fixture.session.snapshot.value.reconnectScheduled).toBe(false);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fixture.sockets).toHaveLength(1);
  });

  it('rejects open, close, invalid, and event callbacks from a replaced generation', () => {
    const onEvent = vi.fn();
    const onInvalidEvent = vi.fn();
    const fixture = transportFixture({ onEvent, onInvalidEvent });

    fixture.session.connect();
    const staleSocket = fixture.sockets[0];
    fixture.session.connect();
    const currentSocket = fixture.sockets[1];
    currentSocket?.handlers.open?.();

    staleSocket?.handlers.open?.();
    staleSocket?.handlers.invalid?.();
    staleSocket?.handlers.event({
      type: 'hello',
      runtime_epoch: 'stale-epoch',
      revision: 9,
    });
    staleSocket?.handlers.close?.();

    expect(fixture.session.snapshot.value.connection).toBe('connected');
    expect(fixture.session.snapshot.value.reconnectScheduled).toBe(false);
    expect(fixture.connected).toHaveBeenCalledOnce();
    expect(fixture.handshakeReady).not.toHaveBeenCalled();
    expect(onInvalidEvent).not.toHaveBeenCalled();
    expect(onEvent).not.toHaveBeenCalled();
    expect(staleSocket?.close).toHaveBeenCalledOnce();
  });

  it('does not let a stale handshake probe schedule a second replacement socket', async () => {
    let releaseProbe!: () => void;
    const probeEventAccess = vi.fn(() => new Promise<void>((resolve) => {
      releaseProbe = resolve;
    }));
    const fixture = transportFixture({ probeEventAccess });

    fixture.session.connect();
    fixture.sockets[0]?.handlers.close?.();
    expect(probeEventAccess).toHaveBeenCalledOnce();

    fixture.session.connect();
    fixture.sockets[1]?.handlers.open?.();
    releaseProbe();
    await Promise.resolve();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(fixture.sockets).toHaveLength(2);
    expect(fixture.session.snapshot.value.connection).toBe('connected');
    expect(fixture.session.snapshot.value.reconnectScheduled).toBe(false);
  });

  it('coalesces projection refreshes and owns projection retry backoff', async () => {
    const fixture = transportFixture();

    fixture.session.scheduleProjectionRefresh();
    fixture.session.scheduleProjectionRefresh();
    fixture.session.scheduleThreadListRefresh();
    fixture.session.scheduleThreadListRefresh();
    await vi.advanceTimersByTimeAsync(89);
    expect(fixture.refreshProjection).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fixture.refreshProjection).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(160);
    expect(fixture.refreshThreadList).toHaveBeenCalledOnce();

    fixture.session.scheduleProjectionReloadRetry();
    expect(fixture.session.snapshot.value.projectionReloadRetryScheduled).toBe(true);
    await vi.advanceTimersByTimeAsync(249);
    expect(fixture.reloadProjection).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(fixture.reloadProjection).toHaveBeenCalledOnce();

    fixture.session.scheduleProjectionReloadRetry();
    await vi.advanceTimersByTimeAsync(499);
    expect(fixture.reloadProjection).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(fixture.reloadProjection).toHaveBeenCalledTimes(2);

    fixture.session.resetProjectionReloadBackoff();
    fixture.session.scheduleProjectionReloadRetry();
    await vi.advanceTimersByTimeAsync(250);
    expect(fixture.reloadProjection).toHaveBeenCalledTimes(3);
  });

  it('lets an authoritative reload supersede pending lightweight refreshes', async () => {
    const fixture = transportFixture();

    fixture.session.scheduleProjectionRefresh();
    fixture.session.scheduleThreadListRefresh();
    fixture.session.requestProjectionReload();

    expect(fixture.reloadProjection).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(fixture.refreshProjection).not.toHaveBeenCalled();
    expect(fixture.refreshThreadList).not.toHaveBeenCalled();
  });

  it('cancels pending lightweight refreshes when the event connection closes', async () => {
    const fixture = transportFixture();
    fixture.session.connect();
    fixture.sockets[0]?.handlers.open?.();
    fixture.session.scheduleProjectionRefresh();
    fixture.session.scheduleThreadListRefresh();

    fixture.sockets[0]?.handlers.close?.();
    await vi.advanceTimersByTimeAsync(1_000);

    expect(fixture.refreshProjection).not.toHaveBeenCalled();
    expect(fixture.refreshThreadList).not.toHaveBeenCalled();
  });

  it('suspends fail-closed by cancelling every timer and rejecting stale callbacks', async () => {
    const fixture = transportFixture();
    fixture.session.connect();
    const socket = fixture.sockets[0];
    socket?.handlers.open?.();
    socket?.handlers.close?.();
    fixture.session.scheduleProjectionRefresh();
    fixture.session.scheduleThreadListRefresh();
    fixture.session.scheduleProjectionReloadRetry();

    fixture.session.suspend();
    expect(fixture.session.snapshot.value).toMatchObject({
      connection: 'disconnected',
      disposed: false,
      reconnectScheduled: false,
      projectionReloadRetryScheduled: false,
    });

    socket?.handlers.open?.();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fixture.sockets).toHaveLength(1);
    expect(fixture.connected).toHaveBeenCalledOnce();
    expect(fixture.refreshProjection).not.toHaveBeenCalled();
    expect(fixture.refreshThreadList).not.toHaveBeenCalled();
    expect(fixture.reloadProjection).not.toHaveBeenCalled();
  });

  it('closes its current socket once and remains terminal after disposal', async () => {
    const fixture = transportFixture();
    fixture.session.connect();
    const socket = fixture.sockets[0];
    socket?.handlers.open?.();

    fixture.session.dispose();
    fixture.session.dispose();

    expect(socket?.close).toHaveBeenCalledOnce();
    expect(fixture.session.snapshot.value).toMatchObject({
      connection: 'disconnected',
      disposed: true,
    });
    socket?.handlers.close?.();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(fixture.sockets).toHaveLength(1);
  });
});
