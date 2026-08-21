import { describe, expect, it, vi } from 'vitest';
import type { FocusThreadSnapshot } from '../../src/focus/types';
import { FocusApiError } from '../../src/focus/types';
import { useFocusWebClient } from '../../src/focus/useFocusWebClient';
import { effectiveThinkingLevel } from '../../src/lib/modelThinking';
import { FakeApi, installFocusClientTestHooks, snapshot, thread } from './support';

installFocusClientTestHooks();

describe('Focus Web client', () => {
  it('waits for server cwd admission before opening a new-workspace draft', async () => {
    const api = new FakeApi();
    const otherWorkspaceThread = {
      ...thread(),
      id: 'thread-2',
      name: 'Other workspace',
      title: 'Other workspace',
      cwd: '/work/other',
    };
    api.threads = [thread(), otherWorkspaceThread];
    const client = useFocusWebClient(api);
    await client.load();

    expect(client.composerScopeId.value).toBe(
      'tab-1:generation:2:thread:thread-1',
    );

    let releaseProfileUpdate = () => {};
    api.profileUpdateGate = new Promise<void>((resolve) => { releaseProfileUpdate = resolve; });
    const opening = client.openWorkspaceDraft('/work/other');
    await Promise.resolve();

    // No optimistic switch: an invalid or failed cwd request must leave the
    // observed thread and its composer scope intact.
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');

    releaseProfileUpdate();
    await expect(opening).resolves.toMatchObject({
      status: 'committed',
      committed: true,
      workspace: '/work/other',
      scopeChanged: true,
    });

    expect(api.profileUpdates).toEqual([{
      selected_thread_id: '',
      working_dir: '/work/other',
    }]);
    expect(client.activeThreadId.value).toBe('');
    expect(client.snapshot.value).toBeNull();
    expect(client.activeWorkspaceId.value).toBe('/work/other');
    expect(client.composerScopeId.value).toBe(
      'tab-1:generation:3:draft:/work/other',
    );
  });

  it('keeps a newer thread selection when an admitted workspace response is superseded', async () => {
    const api = new FakeApi();
    const selectedThread = {
      ...thread(),
      id: 'thread-2',
      title: 'Newer selection',
      name: 'Newer selection',
    };
    api.threads = [thread(), selectedThread];
    const client = useFocusWebClient(api);
    await client.load();

    let releaseProfileUpdate = () => {};
    api.profileUpdateGate = new Promise<void>((resolve) => { releaseProfileUpdate = resolve; });
    const opening = client.openWorkspaceDraft('/work/other');
    await Promise.resolve();
    api.currentSnapshot = { ...snapshot(), thread: selectedThread };
    await client.selectThread('thread-2');
    releaseProfileUpdate();

    await expect(opening).resolves.toMatchObject({
      status: 'superseded',
      committed: true,
    });
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value?.thread.id).toBe('thread-2');
    expect(client.meta.value?.writer_profile.scope_generation).toBe(4);
  });

  it('commits delayed workspace navigation without rolling back independent settings', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();

    let releaseWorkspace = () => {};
    api.profileUpdateGate = new Promise<void>((resolve) => { releaseWorkspace = resolve; });
    const opening = client.openWorkspaceDraft('/work/other');
    await Promise.resolve();
    api.profileUpdateGate = null;

    await client.setApprovalPolicy('on-request');
    expect(client.approvalPolicy.value).toBe('on-request');
    // Settings generation is independent: it must not make the committed
    // navigation response locally stale.
    expect(client.activeThreadId.value).toBe('thread-1');

    releaseWorkspace();
    await expect(opening).resolves.toMatchObject({
      status: 'committed',
      workspace: '/work/other',
    });
    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/work/other');
    expect(client.approvalPolicy.value).toBe('on-request');
    expect(client.meta.value?.next_turn_settings.approval_policy).toBe('on-request');
  });

  it('converges to an older committed workspace when the newer navigation fails', async () => {
    const api = new FakeApi();
    const selectedThread = {
      ...thread(),
      id: 'thread-2',
      title: 'Unavailable target',
      name: 'Unavailable target',
    };
    api.threads = [thread(), selectedThread];
    const client = useFocusWebClient(api);
    await client.load();

    let releaseWorkspace = () => {};
    api.profileUpdateGate = new Promise<void>((resolve) => { releaseWorkspace = resolve; });
    const opening = client.openWorkspaceDraft('/work/other');
    await Promise.resolve();
    api.readError = new FocusApiError('target unavailable', {
      status: 404,
      code: 'thread_not_found',
    });

    await client.selectThread('thread-2');
    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/work/other');

    releaseWorkspace();
    await expect(opening).resolves.toMatchObject({
      status: 'committed',
      workspace: '/work/other',
    });
    expect(client.activeThreadId.value).toBe('');
    expect(client.activeWorkspaceId.value).toBe('/work/other');
  });

  it('invalidates the projection and refuses uploads when failed navigation meta is unreadable', async () => {
    const api = new FakeApi();
    const selectedThread = {
      ...thread(),
      id: 'thread-2',
      title: 'Uncertain target',
      name: 'Uncertain target',
    };
    api.threads = [thread(), selectedThread];
    const client = useFocusWebClient(api);
    await client.load();
    api.readError = new Error('selection transport failed');
    api.metaError = new Error('meta unavailable');

    await client.selectThread('thread-2');

    expect(client.snapshotInvalidated.value).toBe(true);
    expect(client.canSubmit.value).toBe(false);
    const uploadsBefore = api.uploadCalls.length;
    await expect(client.uploadAttachment(
      new Blob(['x'], { type: 'text/plain' }),
      'x.txt',
    )).resolves.toBeNull();
    expect(api.uploadCalls).toHaveLength(uploadsBefore);
  });

  it('settles a current selection whose response epoch is superseded through meta', async () => {
    const api = new FakeApi();
    const selectedThread = {
      ...thread(),
      id: 'thread-2',
      title: 'Epoch target',
      name: 'Epoch target',
    };
    api.threads = [thread(), selectedThread];
    const client = useFocusWebClient(api);
    await client.load();
    api.readThread = vi.fn(async () => ({
      ...snapshot(),
      runtime_epoch: 'epoch-2',
      thread: selectedThread,
      selection_scope: {
        ...snapshot().selection_scope,
        writer_profile: {
          ...snapshot().selection_scope.writer_profile,
          selected_thread_id: 'thread-2',
          scope_generation: 3,
        },
        scope_changed: true,
        previous_attachment_scope: 'thread:thread-1',
        current_attachment_scope: 'thread:thread-2',
        previous_scope_generation: 2,
        current_scope_generation: 3,
        attachment_scope_disposition: 'isolated',
      },
    }));

    await client.selectThread('thread-2');

    // The epoch-mismatched response is never installed. The pending
    // navigation is explicitly failed and reconciled to authoritative meta
    // rather than staying permanently pending on the optimistic target.
    expect(client.activeThreadId.value).toBe('thread-1');
  });

  it('does not replay an older committed rebind after a newer cwd invalidation', async () => {
    const api = new FakeApi();
    api.threads = [thread()];
    const client = useFocusWebClient(api);
    await client.load();

    const pending: Array<{
      resolve: (value: Awaited<ReturnType<FocusWebApiPort['updateProfile']>>) => void;
    }> = [];
    api.updateProfile = vi.fn(() => new Promise((resolve) => {
      pending.push({ resolve });
    }));

    const sameCwd = client.openWorkspaceDraft('/work');
    await Promise.resolve();
    const otherCwd = client.openWorkspaceDraft('/work/other');
    await Promise.resolve();

    pending[1]?.resolve({
      runtime_epoch: 'epoch-1',
      revision: 0,
      writer_profile: {
        ...api.metaValue.writer_profile,
        selected_thread_id: '',
        working_dir: '/work/other',
        scope_generation: 3,
      },
      scope_changed: true,
      previous_attachment_scope: 'draft:/work',
      current_attachment_scope: 'draft:/work/other',
      previous_scope_generation: 2,
      current_scope_generation: 3,
      attachment_scope_disposition: 'invalidated',
      invalidated_attachment_count: 1,
      rebound_attachment_count: 0,
    });
    const newer = await otherCwd;
    expect(newer).toMatchObject({
      status: 'committed',
      composerScopeEffect: 'apply',
      attachmentDisposition: 'invalidated',
      previousComposerScopeId: 'tab-1:generation:2:draft:/work',
    });

    pending[0]?.resolve({
      runtime_epoch: 'epoch-1',
      revision: 0,
      writer_profile: {
        ...api.metaValue.writer_profile,
        selected_thread_id: '',
        working_dir: '/work',
        scope_generation: 2,
      },
      scope_changed: true,
      previous_attachment_scope: 'thread:thread-1',
      current_attachment_scope: 'draft:/work',
      previous_scope_generation: 1,
      current_scope_generation: 2,
      attachment_scope_disposition: 'rebound',
      invalidated_attachment_count: 0,
      rebound_attachment_count: 1,
    });
    const older = await sameCwd;
    expect(older).toMatchObject({
      status: 'superseded',
      committed: true,
      composerScopeEffect: 'clearPrevious',
      attachmentDisposition: 'rebound',
      previousComposerScopeId: 'tab-1:generation:1:thread:thread-1',
      currentComposerScopeId: 'tab-1:generation:2:draft:/work',
    });
    expect(client.activeWorkspaceId.value).toBe('/work/other');
    expect(client.meta.value?.writer_profile.scope_generation).toBe(3);
  });

  it('keeps the current thread visible when server cwd admission rejects an invalid /cd path', async () => {
    const api = new FakeApi();
    api.profileUpdateError = new FocusApiError('workspace does not exist', {
      status: 409,
      code: 'invalid_cwd',
    });
    const client = useFocusWebClient(api);
    await client.load();

    await expect(client.openWorkspaceDraft('/missing')).resolves.toMatchObject({
      status: 'failed',
      committed: false,
    });

    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.snapshot.value?.thread.id).toBe('thread-1');
    expect(client.activeWorkspaceId.value).toBe('/work');
    expect(client.errorMessage.value).toBe('workspace does not exist');
  });

  it('does not hide a browser-owned running operation to change the next workspace', async () => {
    const api = new FakeApi();
    const active = {
      ...thread('self'),
      status: 'active',
    };
    api.threads = [active];
    api.currentSnapshot = {
      ...snapshot('self'),
      thread: active,
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
    };
    const client = useFocusWebClient(api);
    await client.load();

    await expect(client.openWorkspaceDraft('/work')).resolves.toMatchObject({
      status: 'failed',
      committed: false,
    });

    expect(api.profileUpdates).toEqual([]);
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.errorMessage.value).toContain('browser-owned operation');
  });

  it('keeps effort independent from model and exposes auto-model effort choices', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();

    expect(client.models.value[0]?.supportEfforts).toEqual(['medium', 'high']);
    expect(client.thinking.value).toBeUndefined();
    expect(effectiveThinkingLevel(
      client.models.value[0],
      client.composerThinking.value,
    )).toBe('on');
    await client.setReasoningEffort('high');
    await client.selectModel('gpt-test');

    expect(client.thinking.value).toBe('high');
    expect(api.settingsUpdates).toEqual([
      { reasoning_effort: 'high' },
      { model: 'gpt-test' },
    ]);

    await client.setReasoningEffort('');
    expect(client.thinking.value).toBeUndefined();
    const explicitModel = client.models.value.find((model) => model.id === 'gpt-test');
    expect(effectiveThinkingLevel(explicitModel, client.composerThinking.value)).toBe('on');
    expect(api.settingsUpdates.at(-1)).toEqual({ reasoning_effort: '' });
  });

  it('shows a persisted unavailable model exactly without making it selectable', async () => {
    const api = new FakeApi();
    api.setNextTurnSettings({ model: 'gpt-removed' }, 7);
    const client = useFocusWebClient(api);

    await client.load();

    expect(client.selectedModelId.value).toBe('gpt-removed');
    expect(client.status.value.modelId).toBe('gpt-removed');
    expect(client.status.value.model).toBe('gpt-removed');
    expect(client.models.value.some((model) => model.id === 'gpt-removed')).toBe(false);
    await client.selectModel('gpt-removed');
    expect(api.settingsUpdates).toEqual([]);

    await client.selectModel('focus:auto');
    expect(api.settingsUpdates).toEqual([{ model: '' }]);
    expect(client.selectedModelId.value).toBe('focus:auto');
  });

  it('projects explicit residency labels only in the global directory', async () => {
    const api = new FakeApi();
    api.threads[0] = { ...api.threads[0]!, loaded_instance: 'explorer' };
    const client = useFocusWebClient(api);
    await client.load();

    expect(client.sessions.value[0]?.runtimeState).toBeUndefined();
    await client.setThreadScope('global');
    expect(client.sessions.value[0]?.runtimeState).toBe('other');
    expect(client.sessions.value[0]?.runtimeInstance).toBe('explorer');

    api.threads[0] = { ...api.threads[0]!, loaded_instance: '' };
    await client.setThreadScope('current');
    await client.setThreadScope('global');
    expect(client.sessions.value[0]?.runtimeState).toBe('unloaded');

    api.threads[0] = {
      ...api.threads[0]!,
      loaded_instance: 'explorer',
      loaded_state_verified: false,
      selectable: false,
    };
    await client.setThreadScope('current');
    await client.setThreadScope('global');
    expect(client.sessions.value[0]?.runtimeState).toBe('unknown');
    expect(client.sessions.value[0]?.runtimeInstance).toBeUndefined();
  });

  it('keeps a selected thread when an unrelated profile update finishes first', async () => {
    const api = new FakeApi();
    const selectedThread = {
      ...thread(),
      id: 'thread-2',
      name: 'Selected thread',
      title: 'Selected thread',
    };
    api.threads = [thread(), selectedThread];
    const client = useFocusWebClient(api);
    await client.load();

    let resolveSelection = (_value: FocusThreadSnapshot) => {};
    let selectionIntent = 0;
    api.readThread = vi.fn((threadId: string, intent = 0) => new Promise<FocusThreadSnapshot>((resolve) => {
      expect(threadId).toBe('thread-2');
      selectionIntent = intent;
      resolveSelection = resolve;
    }));

    const selection = client.selectThread('thread-2');
    await Promise.resolve();
    await client.setApprovalPolicy('on-request');
    api.setSelectedThreadId('thread-2', 3);
    resolveSelection({
      ...snapshot('none', 'thread-2', 3),
      thread: selectedThread,
    });
    await selection;

    expect(selectionIntent).toBe(1);
    expect(client.approvalPolicy.value).toBe('on-request');
    expect(client.activeThreadId.value).toBe('thread-2');
    expect(client.snapshot.value?.thread.id).toBe('thread-2');
  });

  it('does not open a thread owned by another Focus instance', async () => {
    const api = new FakeApi();
    api.threads = [
      thread(),
      {
        ...thread(),
        id: 'thread-other',
        title: 'Other',
        name: 'Other',
        loaded_instance: 'explorer',
        selectable: false,
        unavailable_reason: 'Open this thread from explorer.',
      },
    ];
    const client = useFocusWebClient(api);
    await client.load();
    const readsBefore = api.readCalls;

    await client.selectThread('thread-other');

    expect(api.readCalls).toBe(readsBefore);
    expect(client.activeThreadId.value).toBe('thread-1');
    expect(client.errorMessage.value).toBe('Open this thread from explorer.');
  });

  it('loads a bounded global inventory for the search dialog on demand', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();

    await client.loadAllSessionsForSearch();

    expect(api.listOptions.at(-1)).toEqual({ scope: 'global', allForSearch: true });
    expect(client.searchSessions.value).toHaveLength(1);
  });
});
