import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FocusMeta } from '../../../src/focus/types';
import { createThreadMutationState } from '../../../src/focus/client-state/thread-mutations';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear() {
      values.clear();
    },
    getItem(key: string) {
      return values.get(key) ?? null;
    },
    key(index: number) {
      return [...values.keys()][index] ?? null;
    },
    removeItem(key: string) {
      values.delete(key);
    },
    setItem(key: string, value: string) {
      values.set(key, value);
    },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('createThreadMutationState', () => {
  it('keeps busy and process-local unknown facts in one record without browser persistence', () => {
    const storage = memoryStorage();
    vi.stubGlobal('sessionStorage', storage);
    const state = createThreadMutationState();

    const busyReceipt = state.beginBusy('thread-1')!;
    state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-1',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
    expect(state.busyByThread.value).toEqual({ 'thread-1': true });
    expect(state.unknownMutations.value).toEqual([{
      threadId: 'thread-1',
      mutationId: 'mutation-1',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    }]);

    state.finishBusy(busyReceipt);
    expect(state.busyByThread.value).toEqual({});
    expect(storage.length).toBe(0);

    state.forgetUnknownIfCurrent(state.captureUnknown('thread-1')!);
    expect(state.unknownMutations.value).toEqual([]);
    expect(storage.getItem('focus-web.unknown-lifecycle:client-1')).toBeNull();
  });

  it('tracks process-local recovery separately without persisting process-bound evidence', () => {
    const storage = memoryStorage();
    vi.stubGlobal('sessionStorage', storage);
    const state = createThreadMutationState();

    expect(state.reconcileUnknown(state.captureSnapshot('thread-rename'), {
      mutation_id: 'mutation-rename',
      operation: 'rename',
      durability: 'process_local',
      reconciling: true,
      lifecycle_verification: null,
    })).toBe(true);

    expect(state.unknownProcessLocalMutations.value).toEqual([{
      threadId: 'thread-rename',
      mutationId: 'mutation-rename',
      operation: 'rename',
      durability: 'process_local',
      verification: null,
    }]);
    expect(state.unknownLifecycleMutations.value).toEqual([]);
    expect(storage.getItem('focus-web.unknown-lifecycle:client-1')).toBeNull();
  });

  it('preserves a held verification until the authoritative mutation disappears', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();
    state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-delete',
      operation: 'delete',
      durability: 'process_local',
      verification: { state: 'deleted', verification_id: 'verify-1' },
    });

    state.reconcileUnknown(state.captureSnapshot('thread-1'), {
      mutation_id: 'mutation-delete',
      operation: 'delete',
      durability: 'process_local',
      reconciling: true,
      lifecycle_verification: null,
    });
    expect(state.getUnknown('thread-1')?.verification).toEqual({
      state: 'deleted',
      verification_id: 'verify-1',
    });

    state.reconcileUnknown(state.captureSnapshot('thread-1'), null);
    expect(state.getUnknown('thread-1')).toBeNull();
  });

  it('loads the current server recovery index and ignores stale browser storage', () => {
    const storage = memoryStorage();
    vi.stubGlobal('sessionStorage', storage);
    const state = createThreadMutationState();
    state.installUnknownFromMeta({
      unknown_lifecycle_mutations: [{
        thread_id: 'thread-server',
        mutation_id: 'mutation-server',
        operation: 'unarchive',
        verification: { state: 'present', verification_id: 'verify-server' },
      }],
    } as FocusMeta);
    expect(state.unknownMutations.value.map((mutation) => mutation.threadId)).toEqual([
      'thread-server',
    ]);

    storage.setItem('focus-web.unknown-lifecycle:client-2', JSON.stringify({
      threadId: 'thread-local',
      mutationId: 'mutation-local',
      operation: 'archive',
      verification: null,
    }));
    const fallback = createThreadMutationState();
    fallback.installUnknownFromMeta({} as FocusMeta);
    expect(fallback.unknownMutations.value).toEqual([]);
  });

  it('uses the exact event identity to reject only its own late unknown result', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();

    expect(state.getUnknown('thread-1')).toBeNull();
    expect(state.captureUnknown('thread-1')).toBeNull();

    state.settleUnknownFromEvent(
      'thread-1', 'mutation-1', 'archive', 'effect_observed',
    );

    expect(state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-1',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    })).toBe(false);
    expect(state.getUnknown('thread-1')).toBeNull();

    expect(state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-2',
      operation: 'delete',
      durability: 'process_local',
      verification: null,
    })).toBe(true);
    expect(state.getUnknown('thread-1')?.mutationId).toBe('mutation-2');
  });

  it('does not let an empty snapshot impersonate an explicit settlement event', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();

    state.reconcileUnknown(state.captureSnapshot('thread-1'), null);

    expect(state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-1',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    })).toBe(true);
    expect(state.getUnknown('thread-1')).toEqual({
      threadId: 'thread-1',
      mutationId: 'mutation-1',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
  });

  it('does not let a snapshot captured before a replacement clear the replacement', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();
    const staleSnapshot = state.captureSnapshot('thread-1');

    state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-new',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });

    expect(state.reconcileUnknown(staleSnapshot, null)).toBe(false);
    expect(state.getUnknown('thread-1')?.mutationId).toBe('mutation-new');
  });

  it('does not let a verification receipt update or delete its replacement', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();
    state.rememberUnknownIfUnsettled({
      threadId: 'thread-1',
      mutationId: 'mutation-archive',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
    const verificationReceipt = state.captureUnknown('thread-1')!;

    state.reconcileUnknown(state.captureSnapshot('thread-1'), {
      mutation_id: 'mutation-delete',
      operation: 'delete',
      durability: 'process_local',
      reconciling: true,
      lifecycle_verification: null,
    });

    expect(state.unknownReceiptIsCurrent(verificationReceipt)).toBe(false);
    expect(state.replaceUnknownIfCurrent(verificationReceipt, {
      threadId: 'thread-1',
      mutationId: 'mutation-archive',
      operation: 'archive',
      durability: 'process_local',
      verification: { state: 'archived', verification_id: 'stale-verification' },
    })).toBe(false);
    expect(state.forgetUnknownIfCurrent(verificationReceipt)).toBe(false);
    expect(state.getUnknown('thread-1')).toEqual({
      threadId: 'thread-1',
      mutationId: 'mutation-delete',
      operation: 'delete',
      durability: 'process_local',
      verification: null,
    });
  });

  it('allows only the exact current receipt to replace or forget an unknown', () => {
    vi.stubGlobal('sessionStorage', memoryStorage());
    const state = createThreadMutationState();
    state.rememberUnknownIfUnsettled({
      threadId: 'thread-replace',
      mutationId: 'mutation-replace',
      operation: 'archive',
      durability: 'process_local',
      verification: null,
    });
    const replaceReceipt = state.captureUnknown('thread-replace')!;

    expect(state.replaceUnknownIfCurrent(replaceReceipt, {
      threadId: 'thread-replace',
      mutationId: 'mutation-replace',
      operation: 'archive',
      durability: 'process_local',
      verification: { state: 'archived', verification_id: 'verification-1' },
    })).toBe(true);
    expect(state.getUnknown('thread-replace')?.verification).toEqual({
      state: 'archived',
      verification_id: 'verification-1',
    });
    expect(state.unknownReceiptIsCurrent(replaceReceipt)).toBe(false);

    state.rememberUnknownIfUnsettled({
      threadId: 'thread-forget',
      mutationId: 'mutation-forget',
      operation: 'delete',
      durability: 'process_local',
      verification: null,
    });
    const forgetReceipt = state.captureUnknown('thread-forget')!;

    expect(state.forgetUnknownIfCurrent(forgetReceipt)).toBe(true);
    expect(state.getUnknown('thread-forget')).toBeNull();
    expect(state.unknownReceiptIsCurrent(forgetReceipt)).toBe(false);
  });
});
