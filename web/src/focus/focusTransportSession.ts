import { computed, shallowRef, type ComputedRef } from 'vue';
import type { FocusEventHandlers } from './api';
import type { FocusProjectionEvent } from './types';

const PROJECTION_REFRESH_DELAY_MS = 90;
const THREAD_LIST_REFRESH_DELAY_MS = 250;
const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 10_000;
const INITIAL_PROJECTION_RETRY_DELAY_MS = 250;
const MAX_PROJECTION_RETRY_DELAY_MS = 10_000;

export type FocusTransportConnectionState = 'connecting' | 'connected' | 'disconnected';
export type FocusHandshakeProbeDisposition = 'retry' | 'stop';

export interface FocusTransportSessionSnapshot {
  connection: FocusTransportConnectionState;
  hasOpenedEventSocket: boolean;
  reconnectScheduled: boolean;
  projectionReloadRetryScheduled: boolean;
  disposed: boolean;
}

export interface FocusTransportSessionCallbacks {
  mayConnect(): boolean;
  openEventSocket(handlers: FocusEventHandlers): WebSocket;
  probeEventAccess(): Promise<void>;
  onHandshakeProbeError(error: unknown): FocusHandshakeProbeDisposition;
  onConnectionError(error: unknown): void;
  onConnected(reconnected: boolean): void;
  onEvent(event: FocusProjectionEvent): void;
  onInvalidEvent(): void;
  refreshProjection(): Promise<void>;
  refreshThreadList(): Promise<void>;
  reloadProjection(): void | Promise<void>;
  mayRetryProjectionReload(): boolean;
  onScheduledTaskError(error: unknown): void;
}

export interface FocusTransportSession {
  readonly snapshot: ComputedRef<Readonly<FocusTransportSessionSnapshot>>;
  connect(): void;
  suspend(): void;
  requestProjectionReload(): void;
  scheduleProjectionRefresh(): void;
  scheduleThreadListRefresh(): void;
  scheduleProjectionReloadRetry(): void;
  cancelProjectionReloadRetry(): void;
  resetProjectionReloadBackoff(): void;
  dispose(): void;
}

/**
 * Owns the browser event transport and every timer created from that
 * transport. Domain projection installation remains with the caller and is
 * reached only through the command callbacks above.
 */
export function createFocusTransportSession(
  callbacks: FocusTransportSessionCallbacks,
): FocusTransportSession {
  const mutableSnapshot = shallowRef<FocusTransportSessionSnapshot>({
    connection: 'connecting',
    hasOpenedEventSocket: false,
    reconnectScheduled: false,
    projectionReloadRetryScheduled: false,
    disposed: false,
  });
  const snapshot = computed<Readonly<FocusTransportSessionSnapshot>>(
    () => mutableSnapshot.value,
  );

  let socket: WebSocket | null = null;
  let socketGeneration = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let projectionRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  let threadListRefreshTimer: ReturnType<typeof setTimeout> | null = null;
  let projectionReloadRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;
  let projectionReloadRetryAttempt = 0;

  function updateSnapshot(changes: Partial<FocusTransportSessionSnapshot>): void {
    mutableSnapshot.value = { ...mutableSnapshot.value, ...changes };
  }

  function cancelReconnect(): void {
    if (reconnectTimer === null) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
    updateSnapshot({ reconnectScheduled: false });
  }

  function cancelProjectionReloadRetry(): void {
    if (projectionReloadRetryTimer === null) return;
    clearTimeout(projectionReloadRetryTimer);
    projectionReloadRetryTimer = null;
    updateSnapshot({ projectionReloadRetryScheduled: false });
  }

  function scheduleReconnect(expectedGeneration: number): void {
    if (
      mutableSnapshot.value.disposed
      || !callbacks.mayConnect()
      || socket !== null
      || expectedGeneration !== socketGeneration
    ) return;
    cancelReconnect();
    const delay = Math.min(
      INITIAL_RECONNECT_DELAY_MS * 2 ** reconnectAttempt,
      MAX_RECONNECT_DELAY_MS,
    );
    reconnectAttempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      updateSnapshot({ reconnectScheduled: false });
      if (expectedGeneration !== socketGeneration) return;
      connect();
    }, delay);
    updateSnapshot({ reconnectScheduled: true });
  }

  function handleUnopenedSocketClose(expectedGeneration: number): void {
    void (async () => {
      try {
        await callbacks.probeEventAccess();
      } catch (error) {
        if (callbacks.onHandshakeProbeError(error) === 'stop') return;
      }
      if (
        mutableSnapshot.value.disposed
        || !callbacks.mayConnect()
        || socket !== null
        || expectedGeneration !== socketGeneration
      ) return;
      scheduleReconnect(expectedGeneration);
    })();
  }

  function connect(): void {
    if (mutableSnapshot.value.disposed || !callbacks.mayConnect()) return;
    cancelReconnect();
    socketGeneration += 1;
    const generation = socketGeneration;
    const previousSocket = socket;
    socket = null;
    previousSocket?.close();
    updateSnapshot({ connection: 'connecting' });
    try {
      let nextSocket: WebSocket;
      let socketOpened = false;
      nextSocket = callbacks.openEventSocket({
        open: () => {
          if (socket !== nextSocket || generation !== socketGeneration) return;
          socketOpened = true;
          const reconnected = mutableSnapshot.value.hasOpenedEventSocket;
          reconnectAttempt = 0;
          updateSnapshot({
            connection: 'connected',
            hasOpenedEventSocket: true,
          });
          callbacks.onConnected(reconnected);
        },
        close: () => {
          if (socket !== nextSocket || generation !== socketGeneration) return;
          socket = null;
          updateSnapshot({ connection: 'disconnected' });
          if (mutableSnapshot.value.disposed || !callbacks.mayConnect()) return;
          if (socketOpened) scheduleReconnect(generation);
          else handleUnopenedSocketClose(generation);
        },
        invalid: () => {
          if (socket === nextSocket && generation === socketGeneration) {
            callbacks.onInvalidEvent();
          }
        },
        event: (event) => {
          if (socket === nextSocket && generation === socketGeneration) {
            callbacks.onEvent(event);
          }
        },
      });
      socket = nextSocket;
    } catch (error) {
      callbacks.onConnectionError(error);
      updateSnapshot({ connection: 'disconnected' });
    }
  }

  function suspend(): void {
    socketGeneration += 1;
    cancelReconnect();
    cancelProjectionReloadRetry();
    const currentSocket = socket;
    socket = null;
    currentSocket?.close();
    if (projectionRefreshTimer !== null) {
      clearTimeout(projectionRefreshTimer);
      projectionRefreshTimer = null;
    }
    if (threadListRefreshTimer !== null) {
      clearTimeout(threadListRefreshTimer);
      threadListRefreshTimer = null;
    }
    updateSnapshot({ connection: 'disconnected' });
  }

  function runScheduledTask(task: () => Promise<void>): void {
    if (mutableSnapshot.value.disposed) return;
    void task().catch(callbacks.onScheduledTaskError);
  }

  function scheduleProjectionRefresh(): void {
    if (mutableSnapshot.value.disposed) return;
    if (projectionRefreshTimer !== null) clearTimeout(projectionRefreshTimer);
    projectionRefreshTimer = setTimeout(() => {
      projectionRefreshTimer = null;
      runScheduledTask(callbacks.refreshProjection);
    }, PROJECTION_REFRESH_DELAY_MS);
  }

  function scheduleThreadListRefresh(): void {
    if (mutableSnapshot.value.disposed) return;
    if (threadListRefreshTimer !== null) clearTimeout(threadListRefreshTimer);
    threadListRefreshTimer = setTimeout(() => {
      threadListRefreshTimer = null;
      runScheduledTask(callbacks.refreshThreadList);
    }, THREAD_LIST_REFRESH_DELAY_MS);
  }

  function requestProjectionReload(): void {
    if (mutableSnapshot.value.disposed) return;
    try {
      void Promise.resolve(callbacks.reloadProjection()).catch(callbacks.onScheduledTaskError);
    } catch (error) {
      callbacks.onScheduledTaskError(error);
    }
  }

  function scheduleProjectionReloadRetry(): void {
    if (
      projectionReloadRetryTimer !== null
      || mutableSnapshot.value.disposed
      || !callbacks.mayRetryProjectionReload()
    ) return;
    const delay = Math.min(
      INITIAL_PROJECTION_RETRY_DELAY_MS * 2 ** projectionReloadRetryAttempt,
      MAX_PROJECTION_RETRY_DELAY_MS,
    );
    projectionReloadRetryAttempt += 1;
    projectionReloadRetryTimer = setTimeout(() => {
      projectionReloadRetryTimer = null;
      updateSnapshot({ projectionReloadRetryScheduled: false });
      if (mutableSnapshot.value.disposed || !callbacks.mayRetryProjectionReload()) return;
      requestProjectionReload();
    }, delay);
    updateSnapshot({ projectionReloadRetryScheduled: true });
  }

  function resetProjectionReloadBackoff(): void {
    projectionReloadRetryAttempt = 0;
  }

  function dispose(): void {
    if (mutableSnapshot.value.disposed) return;
    updateSnapshot({ disposed: true });
    suspend();
  }

  return {
    snapshot,
    connect,
    suspend,
    requestProjectionReload,
    scheduleProjectionRefresh,
    scheduleThreadListRefresh,
    scheduleProjectionReloadRetry,
    cancelProjectionReloadRetry,
    resetProjectionReloadBackoff,
    dispose,
  };
}
