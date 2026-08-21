import { describe, expect, it, vi } from 'vitest';
import {
  FocusApiError,
  type FocusLifecycleResult,
  type FocusLifecycleVerificationResult,
  type FocusMutationResult,
} from '../../../src/focus/types';
import {
  deferred,
  harness,
  installMutationActionsTestHooks,
  installUnknownLifecycle,
  lifecycleResult,
  mutationResult,
  type Harness,
} from './mutation-actions-test-support';

installMutationActionsTestHooks();

describe('FocusMutationActions process-local unknown recovery', () => {
  function installRenameUnknown(h: Harness, mutationId = 'mutation-rename'): void {
    expect(h.actions.reconcileUnknownMutation(
      h.actions.captureUnknownMutationSnapshot('thread-a'),
      {
        mutation_id: mutationId,
        operation: 'rename',
        durability: 'process_local',
        reconciling: true,
        lifecycle_verification: null,
      },
    )).toBe(true);
  }

  it('keeps an exact rename transport-unknown visible without blocking an ordinary prompt', async () => {
    const h = harness();
    h.api.renameThread.mockRejectedValueOnce(new FocusApiError('unknown rename', {
      status: 409,
      code: 'mutation_unknown',
      details: {
        thread_id: 'thread-a',
        mutation_id: 'mutation-rename',
        operation: 'rename',
        durability: 'process_local',
      },
    }));

    await expect(h.actions.renameThread('thread-a', 'new name')).rejects.toThrow('unknown rename');

    expect(h.actions.unknownProcessLocalMutations.value).toEqual([{
      threadId: 'thread-a',
      mutationId: 'mutation-rename',
      operation: 'rename',
      durability: 'process_local',
      verification: null,
    }]);
    expect(h.actions.unknownLifecycleMutations.value).toEqual([]);
    expect(h.actions.canSubmit.value).toBe(true);
  });

  it('discards only the exact process-local unknown and refreshes its projection', async () => {
    const h = harness();
    installRenameUnknown(h);

    await h.actions.discardUnknownProcessLocalMutation('thread-a');

    expect(h.api.resolveUnknownMutation).toHaveBeenCalledWith(
      'thread-a', 'discard', 'mutation-rename',
    );
    expect(h.actions.unknownProcessLocalMutations.value).toEqual([]);
    expect(h.refreshThreads).toHaveBeenCalledOnce();
    expect(h.refreshActiveThread).toHaveBeenCalledOnce();
  });

  it('keeps the process-local lock when settlement status is not exact', async () => {
    const h = harness();
    installRenameUnknown(h);
    h.api.resolveUnknownMutation.mockResolvedValueOnce(
      mutationResult('thread-a', 'mutation-rename', 'retry_opened'),
    );

    await h.actions.discardUnknownProcessLocalMutation('thread-a');

    expect(h.actions.unknownProcessLocalMutations.value).toHaveLength(1);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
    expect(h.refreshThreads).not.toHaveBeenCalled();
  });

  it('keeps the current lock and requests authority reload when discard was replaced', async () => {
    const h = harness();
    installRenameUnknown(h);
    h.api.resolveUnknownMutation.mockRejectedValueOnce(new FocusApiError('replaced', {
      status: 409,
      code: 'mutation_replaced',
      details: {
        thread_id: 'thread-a',
        mutation_id: 'mutation-rename',
        current_mutation_id: 'mutation-new',
      },
    }));

    await h.actions.discardUnknownProcessLocalMutation('thread-a');

    expect(h.actions.unknownProcessLocalMutations.value).toHaveLength(1);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
    expect(h.reportError).toHaveBeenCalledOnce();
  });

  it('keeps a replacement when an older process-local discard completes late', async () => {
    const h = harness();
    installRenameUnknown(h, 'mutation-old');
    const settlement = deferred<FocusMutationResult>();
    h.api.resolveUnknownMutation.mockReturnValueOnce(settlement.promise);

    const discarding = h.actions.discardUnknownProcessLocalMutation('thread-a');
    await vi.waitFor(() => expect(h.api.resolveUnknownMutation).toHaveBeenCalledOnce());
    h.actions.reconcileUnknownMutation(
      h.actions.captureUnknownMutationSnapshot('thread-a'),
      {
        mutation_id: 'mutation-new',
        operation: 'rename',
        durability: 'process_local',
        reconciling: true,
        lifecycle_verification: null,
      },
    );
    settlement.resolve(mutationResult('thread-a', 'mutation-old', 'user_discard'));

    await discarding;
    expect(h.actions.unknownProcessLocalMutations.value[0]?.mutationId).toBe('mutation-new');
    expect(h.refreshThreads).not.toHaveBeenCalled();
  });
});

describe('FocusMutationActions lifecycle receipts', () => {
  it('keeps a newer lifecycle unknown without blocking an ordinary prompt', () => {
    const h = harness();
    const staleSnapshot = h.actions.captureUnknownMutationSnapshot('thread-a');
    installUnknownLifecycle(h.actions, 'archive');

    expect(h.actions.reconcileUnknownMutation(staleSnapshot, null)).toBe(false);
    expect(h.actions.canSubmit.value).toBe(true);
    expect(h.actions.unknownLifecycleMutations.value[0]?.mutationId).toBe('mutation-a');
  });

  it('does not send another lifecycle mutation while the target is unknown', async () => {
    const h = harness();
    installUnknownLifecycle(h.actions, 'archive');

    await expect(h.actions.archiveThread('thread-a')).resolves.toBe(false);
    await expect(h.actions.deleteThread('thread-a', 'thread-a')).resolves.toBe(false);

    expect(h.api.archiveThread).not.toHaveBeenCalled();
    expect(h.api.deleteThread).not.toHaveBeenCalled();
    expect(h.actions.unknownLifecycleMutations.value).toHaveLength(1);
  });

  it('does not resurrect an HTTP unknown settled by an earlier event and rejects a double click', async () => {
    const h = harness();
    const response = deferred<FocusLifecycleResult>();
    h.api.archiveThread.mockReturnValueOnce(response.promise);

    const first = h.actions.archiveThread('thread-a');
    await vi.waitFor(() => expect(h.api.archiveThread).toHaveBeenCalledOnce());
    expect(h.actions.canSubmit.value).toBe(false);
    await expect(h.actions.archiveThread('thread-a')).resolves.toBe(false);
    h.actions.settleUnknownMutationFromEvent(
      'thread-a', 'mutation-a', 'archive', 'effect_observed',
    );
    response.resolve(lifecycleResult('unknown'));
    await expect(first).resolves.toBe(false);

    expect(h.api.archiveThread).toHaveBeenCalledOnce();
    expect(h.actions.unknownLifecycleMutations.value).toEqual([]);
    expect(sessionStorage.getItem('focus-web.unknown-lifecycle:client-1')).toBeNull();
  });

  it('does not let an old settlement suppress a replacement HTTP unknown', async () => {
    const h = harness();
    const response = deferred<FocusLifecycleResult>();
    h.api.archiveThread.mockReturnValueOnce(response.promise);

    const archiving = h.actions.archiveThread('thread-a');
    await vi.waitFor(() => expect(h.api.archiveThread).toHaveBeenCalledOnce());
    h.actions.settleUnknownMutationFromEvent(
      'thread-a', 'mutation-old', 'archive', 'effect_observed',
    );
    response.resolve(lifecycleResult('unknown'));
    await expect(archiving).resolves.toBe(false);

    expect(h.actions.unknownLifecycleMutations.value).toEqual([{
      threadId: 'thread-a',
      mutationId: 'mutation-a',
      operation: 'archive',
      verification: null,
    }]);
  });

  it('does not let late verify or unlock results mutate a replacement unknown', async () => {
    const verifying = harness();
    installUnknownLifecycle(verifying.actions, 'archive');
    const verification = deferred<FocusLifecycleVerificationResult>();
    verifying.api.verifyUnknownLifecycleMutation.mockReturnValueOnce(verification.promise);
    const verify = verifying.actions.verifyUnknownLifecycleMutation('thread-a');
    await vi.waitFor(() => expect(
      verifying.api.verifyUnknownLifecycleMutation,
    ).toHaveBeenCalledWith('thread-a', 'mutation-a'));
    await verifying.actions.verifyUnknownLifecycleMutation('thread-a');
    expect(verifying.api.verifyUnknownLifecycleMutation).toHaveBeenCalledOnce();
    verifying.actions.reconcileUnknownMutation(
      verifying.actions.captureUnknownMutationSnapshot('thread-a'),
      {
      mutation_id: 'mutation-b',
      operation: 'delete', durability: 'process_local',
      reconciling: true, lifecycle_verification: null,
      },
    );
    verification.resolve({
      accepted: true,
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-a',
      mutation_id: 'mutation-a',
      operation: 'archive',
      verification: { state: 'archived', verification_id: 'stale-verification' },
    });
    await verify;
    expect(verifying.actions.unknownLifecycleMutations.value).toEqual([{
      threadId: 'thread-a', mutationId: 'mutation-b', operation: 'delete', verification: null,
    }]);
    expect(verifying.refreshThreads).not.toHaveBeenCalled();

    const unlocking = harness();
    installUnknownLifecycle(
      unlocking.actions,
      'archive',
      { state: 'archived', verification_id: 'verification-1' },
    );
    const unlockGate = deferred<FocusMutationResult>();
    unlocking.api.resolveUnknownMutation.mockReturnValueOnce(unlockGate.promise);
    const unlock = unlocking.actions.unlockUnknownLifecycleMutation('thread-a');
    await vi.waitFor(() => expect(unlocking.api.resolveUnknownMutation).toHaveBeenCalledWith(
      'thread-a', 'discard', 'mutation-a',
    ));
    await unlocking.actions.unlockUnknownLifecycleMutation('thread-a');
    expect(unlocking.api.resolveUnknownMutation).toHaveBeenCalledOnce();
    unlocking.actions.reconcileUnknownMutation(
      unlocking.actions.captureUnknownMutationSnapshot('thread-a'),
      {
      mutation_id: 'mutation-b',
      operation: 'delete',
      durability: 'process_local',
      reconciling: true,
      lifecycle_verification: {
        state: 'deleted', verification_id: 'replacement-verification',
      },
      },
    );
    unlockGate.resolve(mutationResult('thread-a', 'mutation-a'));
    await unlock;
    expect(unlocking.actions.unknownLifecycleMutations.value).toEqual([{
      threadId: 'thread-a',
      mutationId: 'mutation-b',
      operation: 'delete',
      verification: { state: 'deleted', verification_id: 'replacement-verification' },
    }]);
    expect(unlocking.refreshThreads).not.toHaveBeenCalled();
  });

  it.each([
    ['another thread', 'thread-b', 'mutation-a'],
    ['another mutation', 'thread-a', 'mutation-b'],
  ])('does not unlock from an already-reconciled result for %s', async (
    _label,
    threadId,
    mutationId,
  ) => {
    const h = harness();
    installUnknownLifecycle(h.actions, 'archive');
    h.api.verifyUnknownLifecycleMutation.mockResolvedValueOnce({
      accepted: true,
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: threadId,
      mutation_id: mutationId,
      status: 'already_reconciled',
    });

    await h.actions.verifyUnknownLifecycleMutation('thread-a');

    expect(h.actions.unknownLifecycleMutations.value).toEqual([{
      threadId: 'thread-a', mutationId: 'mutation-a', operation: 'archive', verification: null,
    }]);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
  });

  it('keeps a lifecycle lock when discard returns the wrong settlement status', async () => {
    const h = harness();
    installUnknownLifecycle(
      h.actions,
      'archive',
      { state: 'archived', verification_id: 'verification-1' },
    );
    h.api.resolveUnknownMutation.mockResolvedValueOnce(
      mutationResult('thread-a', 'mutation-a', 'retry_opened'),
    );

    await h.actions.unlockUnknownLifecycleMutation('thread-a');

    expect(h.actions.unknownLifecycleMutations.value).toHaveLength(1);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
  });

  it('reloads authoritatively when a lifecycle capability was replaced', async () => {
    const h = harness();
    installUnknownLifecycle(h.actions, 'archive');
    h.api.verifyUnknownLifecycleMutation.mockRejectedValueOnce(new FocusApiError(
      'replacement exists',
      { status: 409, code: 'mutation_replaced' },
    ));

    await h.actions.verifyUnknownLifecycleMutation('thread-a');

    expect(h.actions.unknownLifecycleMutations.value).toHaveLength(1);
    expect(h.invalidateWireProjection).toHaveBeenCalledOnce();
    expect(h.reportError).toHaveBeenCalledOnce();
  });

  it('drops a late lifecycle unknown after disposal', async () => {
    const h = harness();
    const response = deferred<FocusLifecycleResult>();
    h.api.deleteThread.mockReturnValueOnce(response.promise);
    const deleting = h.actions.deleteThread('thread-a', 'thread-a');
    await vi.waitFor(() => expect(h.api.deleteThread).toHaveBeenCalledOnce());

    h.actions.dispose();
    response.resolve(lifecycleResult('unknown'));
    await expect(deleting).resolves.toBe(false);

    expect(h.actions.unknownLifecycleMutations.value).toEqual([]);
    expect(sessionStorage.getItem('focus-web.unknown-lifecycle:client-1')).toBeNull();
    expect(h.reportError).not.toHaveBeenCalled();
    expect(h.invalidateWireProjection).not.toHaveBeenCalled();
  });
});
