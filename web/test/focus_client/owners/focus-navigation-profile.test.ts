import { describe, expect, it, vi } from 'vitest';
import { ClientIntentClock } from '../../../src/focus/clientIntentClock';
import { createFocusNavigationProfile } from '../../../src/focus/focusNavigationProfile';
import {
  FocusApiError,
  type FocusThreadScope,
  type FocusWriterProfile,
} from '../../../src/focus/types';

function profile(
  selectedThreadId = 'thread-a',
  scopeGeneration = 1,
  changes: Partial<FocusWriterProfile> = {},
): FocusWriterProfile {
  return {
    selected_thread_id: selectedThreadId,
    working_dir: '/work',
    scope_generation: scopeGeneration,
    ...changes,
  };
}

function harness(options: { defaultWorkspace?: string } = {}) {
  const clearSnapshot = vi.fn();
  const clearHistoryView = vi.fn();
  const updateThreadQuery = vi.fn();
  const api = {
    meta: vi.fn(),
    updateProfile: vi.fn(),
  };
  const reportError = vi.fn();
  const clearError = vi.fn();
  const setNavigationLoading = vi.fn();
  const projection = {
    refreshActiveThread: vi.fn(async () => false),
    refreshThreads: vi.fn(async () => true),
    scheduleProjectionRefresh: vi.fn(),
    invalidateWireProjection: vi.fn(),
  };
  const navigation = createFocusNavigationProfile({
    intentClock: new ClientIntentClock(),
    api,
    initialClientId: 'client-1',
    defaultWorkspace: () => options.defaultWorkspace ?? '/default',
    clearSnapshot,
    clearHistoryView,
    updateThreadQuery,
    reportError,
    clearError,
    setNavigationLoading,
    threadUnavailableReason: () => '',
    workspaceNavigationBlockReason: () => '',
  });
  navigation.bindProjection(projection);
  navigation.installInitialProfile(profile());
  return {
    navigation,
    api,
    projection,
    clearSnapshot,
    clearHistoryView,
    updateThreadQuery,
    reportError,
    clearError,
    setNavigationLoading,
  };
}

describe('FocusNavigationProfile writer scope', () => {
  it('publishes one atomic confirmed writer-scope receipt', () => {
    const { navigation } = harness();

    expect(navigation.scopeReady.value).toBe(true);
    expect(navigation.composerReady.value).toBe(true);
    expect(navigation.activeThreadId.value).toBe('thread-a');
    expect(navigation.scopeReceipt.value).toMatchObject({
      clientId: 'client-1',
      selectedThreadId: 'thread-a',
      scopeGeneration: 1,
      attachmentScope: 'thread:thread-a',
    });
    expect(navigation.composerScopeId.value).toBe(
      'client-1:generation:1:thread:thread-a',
    );
  });

  it('keeps a targetless profile without any cwd non-writable', () => {
    const { navigation } = harness({ defaultWorkspace: '' });

    navigation.installInitialProfile(profile('', 2, { working_dir: '' }));

    expect(navigation.activeThreadId.value).toBe('');
    expect(navigation.draftWorkspaceId.value).toBe('');
    expect(navigation.scopeReceipt.value?.attachmentScope).toBe('');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.composerReady.value).toBe(false);
    expect(navigation.composerScopeId.value).toBe('');
  });

  it('immediately fences a same-target A to A replacement generation', () => {
    const { navigation } = harness();
    const oldReceipt = navigation.scopeReceipt.value!;

    navigation.beginThreadNavigation('thread-a');

    expect(navigation.activeThreadId.value).toBe('thread-a');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.composerScopeId.value).toBe('');
    expect(navigation.scopeReceiptIsCurrent(oldReceipt)).toBe(false);
  });

  it('publishes a new receipt when a same-target navigation is confirmed', () => {
    const { navigation } = harness();
    const oldReceipt = navigation.scopeReceipt.value!;
    const replacement = navigation.beginThreadNavigation('thread-a');

    const newReceipt = navigation.installObservedProfile(profile('thread-a', 1), {
      navigationGeneration: replacement.navigationGeneration,
      navigationAuthorityGeneration: replacement.authorityGeneration,
      expectedThreadId: 'thread-a',
    });

    expect(newReceipt).not.toBeNull();
    expect(newReceipt).not.toBe(oldReceipt);
    expect(navigation.scopeReceiptIsCurrent(oldReceipt)).toBe(false);
    expect(navigation.scopeReceiptIsCurrent(newReceipt!)).toBe(true);
    expect(navigation.scopeReady.value).toBe(true);
  });

  it('keeps an upload receipt current across a confirmed no-op refresh', () => {
    const { navigation } = harness();
    const uploadReceipt = navigation.scopeReceipt.value!;

    const refreshedReceipt = navigation.installObservedProfile(
      profile('thread-a', 1),
      { expectedThreadId: 'thread-a' },
    );

    expect(refreshedReceipt).toBe(uploadReceipt);
    expect(navigation.scopeReceipt.value).toBe(uploadReceipt);
    expect(navigation.scopeReceiptIsCurrent(uploadReceipt)).toBe(true);
  });

  it('fences /cd while the authoritative workspace is pending', () => {
    const { navigation } = harness();

    const pending = navigation.beginWorkspaceNavigation();

    expect(navigation.currentNavigationStatus).toBe('pending');
    expect(navigation.activeThreadId.value).toBe('thread-a');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.installObservedProfile(profile('', 2, { working_dir: '/next' }), {
      navigationGeneration: pending.navigationGeneration,
      navigationAuthorityGeneration: pending.authorityGeneration,
    })).not.toBeNull();
    expect(navigation.activeThreadId.value).toBe('');
    expect(navigation.draftWorkspaceId.value).toBe('/next');
    expect(navigation.scopeReady.value).toBe(true);
  });

  it('never combines pending B with confirmed A generation', () => {
    const { navigation } = harness();

    navigation.beginThreadNavigation('thread-b');

    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.scopeReceipt.value?.selectedThreadId).toBe('thread-a');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.composerScopeId.value).toBe('');
  });

  it('invalidates an old A receipt across A to B to A', () => {
    const { navigation } = harness();
    const firstA = navigation.scopeReceipt.value!;
    const toB = navigation.beginThreadNavigation('thread-b');
    navigation.installObservedProfile(profile('thread-b', 2), {
      navigationGeneration: toB.navigationGeneration,
      navigationAuthorityGeneration: toB.authorityGeneration,
      expectedThreadId: 'thread-b',
    });
    const toA = navigation.beginThreadNavigation('thread-a');
    const replacementA = navigation.installObservedProfile(profile('thread-a', 3), {
      navigationGeneration: toA.navigationGeneration,
      navigationAuthorityGeneration: toA.authorityGeneration,
      expectedThreadId: 'thread-a',
    });

    expect(replacementA).not.toBeNull();
    expect(replacementA?.receiptGeneration).not.toBe(firstA.receiptGeneration);
    expect(navigation.scopeReceiptIsCurrent(firstA)).toBe(false);
    expect(navigation.composerScopeId.value).toBe(
      'client-1:generation:3:thread:thread-a',
    );
  });

  it('rejects a stale observed profile without publishing a replacement scope', () => {
    const { navigation } = harness();
    const pending = navigation.beginThreadNavigation('thread-b');

    const rejected = navigation.installObservedProfile(profile('thread-a', 1), {
      navigationGeneration: pending.navigationGeneration,
      expectedThreadId: 'thread-b',
    });

    expect(rejected).toBeNull();
    expect(navigation.currentNavigationStatus).toBe('pending');
    expect(navigation.scopeReady.value).toBe(false);
  });

  it('reissues the atomic receipt when the registered client is replaced', () => {
    const { navigation } = harness();
    const firstReceipt = navigation.scopeReceipt.value!;

    navigation.registerClient('client-2');

    expect(navigation.registeredClientId.value).toBe('client-2');
    expect(navigation.scopeReceipt.value).toMatchObject({
      clientId: 'client-2',
      selectedThreadId: 'thread-a',
      scopeGeneration: 1,
    });
    expect(navigation.scopeReceipt.value?.receiptGeneration).not.toBe(
      firstReceipt.receiptGeneration,
    );
    expect(navigation.scopeReceiptIsCurrent(firstReceipt)).toBe(false);
    expect(navigation.composerScopeId.value).toBe(
      'client-2:generation:1:thread:thread-a',
    );
  });

  it('does not reissue a receipt when the normalized client is unchanged', () => {
    const { navigation } = harness();
    const firstReceipt = navigation.scopeReceipt.value!;

    navigation.registerClient(' client-1 ');

    expect(navigation.scopeReceipt.value).toBe(firstReceipt);
    expect(navigation.scopeReceiptIsCurrent(firstReceipt)).toBe(true);
  });

  it('rejects lower generations without committing any visible snapshot state', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    const confirmed = navigation.confirmedWriterProfile.value;
    const receipt = navigation.scopeReceipt.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    const rejected = navigation.installObservedProfile(profile('thread-a', 0));

    expect(rejected).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(receipt);
    expect(navigation.writerProfile.value).toEqual(profile());
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
  });

  it.each([
    ['selected thread', { selected_thread_id: 'thread-b' }],
    ['working directory', { working_dir: '/other' }],
  ])(
    'rejects a same-generation conflicting %s without committing snapshot state',
    (_label, changes) => {
      const { navigation, clearSnapshot, updateThreadQuery } = harness();
      const confirmed = navigation.confirmedWriterProfile.value;
      const receipt = navigation.scopeReceipt.value;
      const clearCount = clearSnapshot.mock.calls.length;
      const queryCount = updateThreadQuery.mock.calls.length;

      const rejected = navigation.installObservedProfile(profile('thread-a', 1, changes));

      expect(rejected).toBeNull();
      expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
      expect(navigation.scopeReceipt.value).toBe(receipt);
      expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
      expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
    },
  );

  it('requires an explicit fresh-authority read to settle repair', () => {
    const { navigation } = harness();
    const pending = navigation.beginThreadNavigation('thread-b');
    expect(navigation.markNavigationFailed(pending.navigationGeneration)).toBe(true);

    expect(navigation.installObservedProfile(profile('thread-b', 2), {
      navigationGeneration: pending.navigationGeneration,
      navigationAuthorityGeneration: pending.authorityGeneration,
      expectedThreadId: 'thread-b',
    })).toBeNull();
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);

    const repairFloor = navigation.captureNavigationStateFloor();
    expect(navigation.installObservedProfile(profile('thread-b', 2), {
      expectedThreadId: 'thread-b',
      freshAuthorityFloor: repairFloor,
    })).not.toBeNull();
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.scopeReady.value).toBe(true);
  });

  it('rejects a pending navigation response across a profile repair boundary', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    const pendingB = navigation.beginThreadNavigation('thread-b');
    navigation.requireNavigationRepair();
    const confirmed = navigation.confirmedWriterProfile.value;
    const receipt = navigation.scopeReceipt.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    expect(navigation.installObservedProfile(profile('thread-b', 2), {
      navigationGeneration: pendingB.navigationGeneration,
      navigationAuthorityGeneration: pendingB.authorityGeneration,
      expectedThreadId: 'thread-b',
    })).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(receipt);
    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
  });

  it('converges but does not settle an older response within one repair authority', () => {
    const { navigation } = harness();
    const committedB = navigation.beginThreadNavigation('thread-b');
    const failedC = navigation.beginThreadNavigation('thread-c');
    expect(navigation.markNavigationFailed(failedC.navigationGeneration)).toBe(true);

    const converged = navigation.installObservedProfile(profile('thread-b', 2), {
      navigationGeneration: committedB.navigationGeneration,
      navigationAuthorityGeneration: committedB.authorityGeneration,
      expectedThreadId: 'thread-b',
    });

    expect(converged).not.toBeNull();
    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.composerScopeId.value).toBe('');

    const repairFloor = navigation.captureNavigationStateFloor();
    expect(navigation.installObservedProfile(profile('thread-b', 2), {
      expectedThreadId: 'thread-b',
      freshAuthorityFloor: repairFloor,
    })).not.toBeNull();
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.scopeReady.value).toBe(true);
  });

  it('rejects old navigation responses after an explicit repair boundary', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    const oldB = navigation.beginThreadNavigation('thread-b');
    navigation.beginThreadNavigation('thread-c');
    navigation.showRepairDraft('/repair');
    const confirmed = navigation.confirmedWriterProfile.value;
    const receipt = navigation.scopeReceipt.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    const rejected = navigation.installObservedProfile(profile('thread-b', 2), {
      navigationGeneration: oldB.navigationGeneration,
      navigationAuthorityGeneration: oldB.authorityGeneration,
      expectedThreadId: 'thread-b',
    });

    expect(rejected).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(receipt);
    expect(navigation.activeThreadId.value).toBe('');
    expect(navigation.draftWorkspaceId.value).toBe('/repair');
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
  });

  it('rejects a fresh read captured before the current repair authority', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    const staleReadFloor = navigation.captureNavigationStateFloor();
    navigation.requireNavigationRepair();
    const repairFloor = navigation.captureNavigationStateFloor();
    const confirmed = navigation.confirmedWriterProfile.value;
    const receipt = navigation.scopeReceipt.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    expect(navigation.installObservedProfile(profile('thread-a', 1), {
      freshAuthorityFloor: staleReadFloor,
    })).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(receipt);
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);

    expect(navigation.installObservedProfile(profile('thread-a', 1), {
      freshAuthorityFloor: repairFloor,
    })).not.toBeNull();
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.scopeReady.value).toBe(true);
  });

  it('rejects a stale fresh read after another read wins the exact floor CAS', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    navigation.requireNavigationRepair();
    const sharedFloor = navigation.captureNavigationStateFloor();

    const winningReceipt = navigation.installObservedProfile(profile('thread-a', 2), {
      expectedThreadId: 'thread-a',
      freshAuthorityFloor: sharedFloor,
    });
    expect(winningReceipt).not.toBeNull();
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.scopeReceiptIsCurrent(winningReceipt!)).toBe(true);
    const confirmed = navigation.confirmedWriterProfile.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    expect(navigation.installObservedProfile(profile('thread-a', 3), {
      expectedThreadId: 'thread-a',
      freshAuthorityFloor: sharedFloor,
    })).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(winningReceipt);
    expect(navigation.scopeReceiptIsCurrent(winningReceipt!)).toBe(true);
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
  });

  it('rejects a fresh read started while pending after that navigation fails', () => {
    const { navigation, clearSnapshot, updateThreadQuery } = harness();
    const pending = navigation.beginThreadNavigation('thread-b');
    const staleFreshRead = navigation.captureNavigationStateFloor();
    expect(navigation.markNavigationFailed(pending.navigationGeneration)).toBe(true);
    const confirmed = navigation.confirmedWriterProfile.value;
    const receipt = navigation.scopeReceipt.value;
    const clearCount = clearSnapshot.mock.calls.length;
    const queryCount = updateThreadQuery.mock.calls.length;

    expect(navigation.installObservedProfile(profile('thread-b', 2), {
      expectedThreadId: 'thread-b',
      freshAuthorityFloor: staleFreshRead,
    })).toBeNull();
    expect(navigation.confirmedWriterProfile.value).toBe(confirmed);
    expect(navigation.scopeReceipt.value).toBe(receipt);
    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(clearSnapshot).toHaveBeenCalledTimes(clearCount);
    expect(updateThreadQuery).toHaveBeenCalledTimes(queryCount);
  });

  it('accepts only the two typed thread-scope commands', () => {
    const { navigation } = harness();
    const currentScope: FocusThreadScope = 'current';
    const globalScope: FocusThreadScope = 'global';

    expect(navigation.threadScope.value).toBe(currentScope);
    expect(navigation.setThreadScope(currentScope)).toBe(false);
    expect(navigation.setThreadScope(globalScope)).toBe(true);
    expect(navigation.threadScope.value).toBe('global');
    expect(navigation.setThreadScope(globalScope)).toBe(false);
  });
});

describe('FocusNavigationProfile navigation commands', () => {
  it('retries a stale active read without entering repair or showing an error', async () => {
    const { navigation, projection, reportError } = harness();
    let attempts = 0;
    projection.refreshActiveThread.mockImplementationOnce(async () => {
      attempts += 1;
      throw new FocusApiError('stale read', {
        status: 409,
        code: 'stale_thread_read',
      });
    });
    projection.refreshActiveThread.mockImplementationOnce(async () => {
      attempts += 1;
      const floor = navigation.captureNavigationStateFloor();
      navigation.installObservedProfile(profile('thread-b', 2), {
        navigationGeneration: floor.navigationGeneration,
        navigationAuthorityGeneration: floor.authorityGeneration,
        expectedThreadId: 'thread-b',
      });
      return true;
    });

    await navigation.selectThread('thread-b');

    expect(attempts).toBe(2);
    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.navigationRepairIsRequired).toBe(false);
    expect(reportError).not.toHaveBeenCalled();
    expect(projection.scheduleProjectionRefresh).not.toHaveBeenCalled();
  });

  it('treats a stale document read as a bounded navigation retry', async () => {
    const { navigation, projection, reportError } = harness();
    let attempts = 0;
    projection.refreshActiveThread.mockImplementationOnce(async () => {
      attempts += 1;
      throw new FocusApiError('stale document read', {
        status: 409,
        code: 'stale_document_read',
      });
    });
    projection.refreshActiveThread.mockImplementationOnce(async () => {
      attempts += 1;
      const floor = navigation.captureNavigationStateFloor();
      navigation.installObservedProfile(profile('thread-b', 2), {
        navigationGeneration: floor.navigationGeneration,
        navigationAuthorityGeneration: floor.authorityGeneration,
        expectedThreadId: 'thread-b',
      });
      return true;
    });

    await navigation.selectThread('thread-b');

    expect(attempts).toBe(2);
    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.navigationRepairIsRequired).toBe(false);
    expect(reportError).not.toHaveBeenCalled();
  });

  it('keeps a target pending when bounded stale retries still lose the fence', async () => {
    const { navigation, projection, reportError } = harness();
    const stale = new FocusApiError('stale read', {
      status: 409,
      code: 'stale_thread_read',
    });
    projection.refreshActiveThread
      .mockRejectedValueOnce(stale)
      .mockRejectedValueOnce(stale);

    await navigation.selectThread('thread-b');

    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.currentNavigationStatus).toBe('pending');
    expect(navigation.navigationRepairIsRequired).toBe(false);
    expect(reportError).not.toHaveBeenCalled();
    expect(projection.scheduleProjectionRefresh).toHaveBeenCalledOnce();
  });

  it('settles a failed navigation from a fresh authoritative meta read', async () => {
    const {
      navigation,
      api,
      projection,
      reportError,
      setNavigationLoading,
    } = harness();
    projection.refreshActiveThread.mockResolvedValueOnce(false);
    api.meta.mockResolvedValueOnce({ writer_profile: profile('thread-b', 2) });

    await navigation.selectThread('thread-b');

    expect(navigation.currentNavigationStatus).toBe('confirmed');
    expect(navigation.activeThreadId.value).toBe('thread-b');
    expect(navigation.scopeReady.value).toBe(true);
    expect(navigation.composerReady.value).toBe(true);
    expect(projection.invalidateWireProjection).not.toHaveBeenCalled();
    expect(setNavigationLoading.mock.calls).toEqual([[true], [false]]);
    expect(reportError).not.toHaveBeenCalled();
  });

  it('leaves a failed reconciliation in repair rather than pending forever', async () => {
    const { navigation, api, projection, setNavigationLoading } = harness();
    projection.refreshActiveThread.mockResolvedValueOnce(false);
    api.meta.mockRejectedValueOnce(new Error('meta unavailable'));

    await navigation.selectThread('thread-b');

    expect(navigation.currentNavigationStatus).toBe('repair');
    expect(navigation.scopeReady.value).toBe(false);
    expect(navigation.composerReady.value).toBe(false);
    expect(projection.invalidateWireProjection).toHaveBeenCalledTimes(1);
    expect(setNavigationLoading.mock.calls).toEqual([[true], [false]]);
  });
});
