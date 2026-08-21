import { computed, ref, type ComputedRef, type Ref } from 'vue';
import { decodeFocusRuntimeNoticeDetail } from '../projectionEventDecoder';
import type { FocusProjectionEvent } from '../types';

const MAX_RUNTIME_NOTICES = 5;

export interface RuntimeNoticeItem {
  id: string;
  threadId: string;
  method: 'error' | 'warning';
  message: string;
  additionalDetails: string;
  willRetry: boolean;
  turnId: string;
}

export interface RuntimeNoticePresentation {
  retry: RuntimeNoticeItem | null;
  notices: readonly RuntimeNoticeItem[];
}

export interface RuntimeNoticeOwner {
  readonly presentation: ComputedRef<RuntimeNoticePresentation>;
  handleEvent(event: FocusProjectionEvent): void;
  reset(): void;
}

function boundedAppend(
  items: readonly RuntimeNoticeItem[],
  item: RuntimeNoticeItem,
): RuntimeNoticeItem[] {
  return [...items, item].slice(-MAX_RUNTIME_NOTICES);
}

function eventAdvancesRetry(event: FocusProjectionEvent, retry: RuntimeNoticeItem): boolean {
  if (event.type !== 'thread_delta' || !event.thread_id) return false;
  const eventTurnId = typeof event.detail?.turn_id === 'string'
    ? event.detail.turn_id
    : '';
  if (!eventTurnId) return false;
  if (retry.threadId && retry.threadId !== event.thread_id) return false;
  return !retry.turnId || retry.turnId === eventTurnId;
}

/** Owns bounded, presentation-only app-server notices for the current document. */
export function createRuntimeNoticeOwner(
  activeThreadId: Readonly<Ref<string>>,
): RuntimeNoticeOwner {
  const retryNotices = ref<RuntimeNoticeItem[]>([]);
  const notices = ref<RuntimeNoticeItem[]>([]);
  let runtimeEpoch = '';
  let revision = -1;

  const presentation = computed<RuntimeNoticePresentation>(() => {
    const selectedThreadId = activeThreadId.value;
    const applies = (item: RuntimeNoticeItem) => (
      !item.threadId || item.threadId === selectedThreadId
    );
    return {
      retry: [...retryNotices.value].reverse().find(applies) ?? null,
      notices: notices.value.filter(applies),
    };
  });

  function clearItems(): void {
    retryNotices.value = [];
    notices.value = [];
  }

  function reset(): void {
    runtimeEpoch = '';
    revision = -1;
    clearItems();
  }

  function handleEvent(event: FocusProjectionEvent): void {
    if (event.runtime_epoch !== runtimeEpoch) {
      runtimeEpoch = event.runtime_epoch;
      revision = -1;
      clearItems();
    }
    if (event.revision <= revision) return;
    revision = event.revision;

    if (event.type === 'projection_invalidated'
      || event.type === 'runtime_changed'
      || event.type === 'session_expired') {
      clearItems();
      return;
    }
    if (event.type === 'backend_disconnected') {
      retryNotices.value = [];
      return;
    }
    if (event.type !== 'runtime_notice') {
      retryNotices.value = retryNotices.value.filter(
        (retry) => !eventAdvancesRetry(event, retry),
      );
      return;
    }

    const detail = decodeFocusRuntimeNoticeDetail(event.detail);
    if (!detail) return;
    const item: RuntimeNoticeItem = {
      id: `${event.runtime_epoch}:${event.revision}`,
      threadId: event.thread_id ?? '',
      method: detail.method,
      message: detail.message,
      additionalDetails: detail.method === 'error' ? detail.additional_details : '',
      willRetry: detail.method === 'error' && detail.will_retry,
      turnId: detail.method === 'error' ? detail.turn_id : '',
    };
    if (item.willRetry) {
      const sameTarget = (candidate: RuntimeNoticeItem) => (
        candidate.threadId === item.threadId && candidate.turnId === item.turnId
      );
      retryNotices.value = boundedAppend(retryNotices.value.filter(
        (candidate) => !sameTarget(candidate)
      ), item);
      return;
    }
    notices.value = boundedAppend(notices.value, item);
  }

  return { presentation, handleEvent, reset };
}
