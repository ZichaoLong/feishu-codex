import { describe, expect, it, vi } from 'vitest';
import { createWebNextTurnSettings } from '../../../src/focus/client-state/web-next-turn-settings';
import type { FocusNextTurnSettings, FocusNextTurnSettingsResult } from '../../../src/focus/types';

function snapshot(
  generation: number,
  changes: Partial<Omit<FocusNextTurnSettings, 'generation'>> = {},
): FocusNextTurnSettings {
  return {
    generation,
    model: '',
    reasoning_effort: '',
    approval_policy: 'never',
    permissions_profile_id: ':workspace',
    ...changes,
  };
}

function result(
  generation: number,
  changes: Partial<Omit<FocusNextTurnSettings, 'generation'>> = {},
  runtimeEpoch = 'epoch-1',
): FocusNextTurnSettingsResult {
  return {
    runtime_epoch: runtimeEpoch,
    revision: generation,
    next_turn_settings: snapshot(generation, changes),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

function harness(input: {
  read?: () => Promise<FocusNextTurnSettingsResult>;
  update?: (
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
  ) => Promise<FocusNextTurnSettingsResult>;
  supportedEfforts?: Record<string, string[]>;
} = {}) {
  const reportError = vi.fn();
  const runtimeEpochMismatch = vi.fn();
  const readNextTurnSettings = vi.fn(input.read ?? (async () => result(1)));
  const updateNextTurnSettings = vi.fn(input.update ?? (async () => result(2)));
  const supported = input.supportedEfforts ?? {};
  const owner = createWebNextTurnSettings({
    api: { readNextTurnSettings, updateNextTurnSettings },
    modelIsAvailable: (modelId) => modelId === 'focus:auto' || modelId in supported,
    supportedReasoningEfforts: (modelId) => supported[modelId] ?? [],
    runtimeEpochMismatch,
    reportError,
  });
  return {
    owner,
    readNextTurnSettings,
    updateNextTurnSettings,
    runtimeEpochMismatch,
    reportError,
  };
}

describe('Web next-turn settings owner', () => {
  it('orders complete snapshots by generation only inside one runtime epoch', () => {
    const { owner } = harness();

    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(2, { model: 'gpt-a' })))
      .toBe('installed');
    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(1, { model: 'stale' })))
      .toBe('ignored');
    expect(owner.snapshot.value).toEqual(snapshot(2, { model: 'gpt-a' }));
    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(2, { model: 'gpt-a' })))
      .toBe('unchanged');
    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(3, { approval_policy: 'on-request' })))
      .toBe('installed');

    // A restarted backend may reuse seed generation 1 with different values.
    expect(owner.installRuntimeSnapshot('epoch-2', snapshot(1, { model: 'gpt-restarted' })))
      .toBe('installed');
    expect(owner.runtimeEpoch.value).toBe('epoch-2');
    expect(owner.snapshot.value).toEqual(snapshot(1, { model: 'gpt-restarted' }));
  });

  it('fails closed on equal-generation disagreement and coalesces its authority reads', async () => {
    const read = deferred<FocusNextTurnSettingsResult>();
    const { owner, readNextTurnSettings, reportError } = harness({
      read: () => read.promise,
    });
    owner.installRuntimeSnapshot('epoch-1', snapshot(4, { model: 'gpt-a' }));

    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(4, { model: 'gpt-b' })))
      .toBe('conflict');
    expect(owner.installRuntimeSnapshot('epoch-1', snapshot(4, { model: 'gpt-c' })))
      .toBe('conflict');
    expect(readNextTurnSettings).toHaveBeenCalledTimes(1);
    expect(owner.snapshot.value?.model).toBe('gpt-a');

    read.resolve(result(4, { model: 'gpt-b' }));
    await vi.waitFor(() => expect(readNextTurnSettings).toHaveBeenCalledTimes(2));
    await vi.waitFor(() => expect(reportError).toHaveBeenCalledTimes(2));
    expect(owner.snapshot.value?.model).toBe('gpt-a');
    expect(String(reportError.mock.calls[0]?.[0])).toContain('conflicting next-turn settings');
  });

  it('coalesces an in-flight invalidation into one follow-up authority read', async () => {
    const first = deferred<FocusNextTurnSettingsResult>();
    const second = deferred<FocusNextTurnSettingsResult>();
    const responses = [first, second];
    const { owner, readNextTurnSettings } = harness({
      read: () => responses.shift()!.promise,
    });
    owner.installRuntimeSnapshot('epoch-1', snapshot(1));

    const firstRefresh = owner.refresh();
    const secondRefresh = owner.refresh();
    expect(readNextTurnSettings).toHaveBeenCalledTimes(1);

    first.resolve(result(2, { model: 'gpt-two' }));
    await vi.waitFor(() => expect(readNextTurnSettings).toHaveBeenCalledTimes(2));
    second.resolve(result(3, { model: 'gpt-three' }));
    await Promise.all([firstRefresh, secondRefresh]);

    expect(owner.snapshot.value).toEqual(snapshot(3, { model: 'gpt-three' }));
  });

  it('uses server generation for different-field and same-field response races', async () => {
    const first = deferred<FocusNextTurnSettingsResult>();
    const second = deferred<FocusNextTurnSettingsResult>();
    const responses = [first, second];
    const { owner, updateNextTurnSettings } = harness({
      update: () => responses.shift()!.promise,
    });
    owner.installRuntimeSnapshot('epoch-1', snapshot(1));

    const modelUpdate = owner.selectModel('focus:auto');
    const approvalUpdate = owner.setApprovalPolicy('on-request');
    second.resolve(result(3, { model: 'gpt-b', approval_policy: 'on-request' }));
    await approvalUpdate;
    first.resolve(result(2, { model: '', approval_policy: 'never' }));
    await modelUpdate;

    expect(updateNextTurnSettings).toHaveBeenCalledTimes(2);
    expect(owner.snapshot.value).toEqual(snapshot(3, {
      model: 'gpt-b',
      approval_policy: 'on-request',
    }));

    const olderIssued = deferred<FocusNextTurnSettingsResult>();
    const newerIssued = deferred<FocusNextTurnSettingsResult>();
    vi.mocked(updateNextTurnSettings)
      .mockImplementationOnce(() => olderIssued.promise)
      .mockImplementationOnce(() => newerIssued.promise);
    const olderCall = owner.setApprovalPolicy('untrusted');
    const newerCall = owner.setApprovalPolicy('never');
    newerIssued.resolve(result(4, { approval_policy: 'never' }));
    await newerCall;
    olderIssued.resolve(result(5, { approval_policy: 'untrusted' }));
    await olderCall;
    expect(owner.snapshot.value?.generation).toBe(5);
    expect(owner.snapshot.value?.approval_policy).toBe('untrusted');
  });

  it('resets an explicitly unsupported effort to Auto in the same model update', async () => {
    const { owner, updateNextTurnSettings } = harness({
      supportedEfforts: { 'gpt-a': ['low'], 'gpt-b': ['high'] },
      update: async (changes) => result(2, {
        model: String(changes.model ?? 'gpt-a'),
        reasoning_effort: String(changes.reasoning_effort ?? 'low'),
      }),
    });
    owner.installRuntimeSnapshot('epoch-1', snapshot(1, {
      model: 'gpt-a',
      reasoning_effort: 'low',
    }));

    await owner.selectModel('gpt-b');

    expect(updateNextTurnSettings).toHaveBeenCalledWith({
      model: 'gpt-b',
      reasoning_effort: '',
    });
    expect(owner.reasoningEffort.value).toBeUndefined();
  });

  it('does not let direct cross-epoch or disposed late results replace the snapshot', async () => {
    const update = deferred<FocusNextTurnSettingsResult>();
    const { owner, runtimeEpochMismatch } = harness({ update: () => update.promise });
    owner.installRuntimeSnapshot('epoch-1', snapshot(3, { model: 'gpt-a' }));
    const pending = owner.setApprovalPolicy('on-request');
    update.resolve(result(1, { model: 'gpt-new-runtime' }, 'epoch-2'));
    await pending;
    expect(runtimeEpochMismatch).toHaveBeenCalledOnce();
    expect(owner.snapshot.value?.model).toBe('gpt-a');

    const late = deferred<FocusNextTurnSettingsResult>();
    const secondHarness = harness({ update: () => late.promise });
    secondHarness.owner.installRuntimeSnapshot('epoch-1', snapshot(1));
    const lateUpdate = secondHarness.owner.setApprovalPolicy('on-request');
    secondHarness.owner.dispose();
    late.resolve(result(2, { approval_policy: 'on-request' }));
    await lateUpdate;
    expect(secondHarness.owner.snapshot.value?.generation).toBe(1);
  });
});
