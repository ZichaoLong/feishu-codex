import { describe, expect, it } from 'vitest';
import { useFocusWebClient } from '../../src/focus/useFocusWebClient';
import { FakeApi, installFocusClientTestHooks, snapshot, thread } from './support';

installFocusClientTestHooks();

describe('Focus Web client', () => {
  it('updates next-turn settings during an active selected turn without changing its disclosure or navigation', async () => {
    const api = new FakeApi();
    api.threads = [{ ...thread('self'), status: 'active' }];
    api.currentSnapshot = {
      ...snapshot('self'),
      thread: { ...thread('self'), status: 'active' },
      active_turn_id: 'turn-1',
      active_turn_status: 'inProgress',
      active_turn_context: {
        turn_id: 'turn-1',
        initiator: { kind: 'fcodex', binding_id: '' },
        feishu_audience: ['chat:group-1'],
        settings: {
          model: { value: 'gpt-active', source: 'active_reroute' },
          reasoning_effort: { value: 'high', source: 'inherited' },
          approval_policy: { value: 'never', source: 'inherited' },
          permissions_profile_id: { value: ':danger-full-access', source: 'inherited' },
        },
      },
    };
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    const writerProfile = JSON.parse(JSON.stringify(client.meta.value?.writer_profile));
    const activeTurnDisclosure = JSON.parse(JSON.stringify(
      client.snapshot.value?.active_turn_context,
    ));
    const owner = JSON.parse(JSON.stringify(client.owner.value));
    const activeThreadId = client.activeThreadId.value;
    const composerScopeId = client.composerScopeId.value;
    expect(client.running.value).toBe(true);
    expect(client.canSubmit.value).toBe(true);

    await client.selectModel('gpt-test');
    await client.setReasoningEffort('medium');
    await client.setApprovalPolicy('on-request');
    await client.setPermissionsProfile(':workspace');

    expect(api.settingsUpdates).toEqual([
      { model: 'gpt-test' },
      { reasoning_effort: 'medium' },
      { approval_policy: 'on-request' },
      { permissions_profile_id: ':workspace' },
    ]);
    expect(api.profileUpdates).toEqual([]);
    expect(client.meta.value?.writer_profile).toEqual(writerProfile);
    expect(client.activeThreadId.value).toBe(activeThreadId);
    expect(client.composerScopeId.value).toBe(composerScopeId);
    expect(client.owner.value).toEqual(owner);
    expect(client.snapshot.value?.active_turn_context).toEqual(activeTurnDisclosure);
    expect(client.running.value).toBe(true);
    expect(client.canSubmit.value).toBe(true);
  });

  it('allows realtime submission across owners but disables it after disconnect', async () => {
    const api = new FakeApi();
    api.threads = [thread('other')];
    api.currentSnapshot = snapshot('other');
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    expect(client.canSubmit.value).toBe(true);
    api.handlers?.close?.();
    expect(client.connection.value).toBe('disconnected');
    expect(client.canSubmit.value).toBe(false);
  });

  it('projects live context usage rather than cumulative token usage into the status bar', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'thread/tokenUsage/updated',
        token_usage: {
          total: { totalTokens: 769_096_416 },
          last: { totalTokens: 143_558 },
          modelContextWindow: 258_400,
        },
      },
    });

    expect(client.status.value.ctxUsed).toBe(143_558);
    expect(client.status.value.ctxMax).toBe(258_400);
    expect(client.status.value.ctxRemainingPct).toBe(47);

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 2,
      thread_id: 'thread-1',
      detail: {
        method: 'thread/tokenUsage/updated',
        token_usage: {
          total: { totalTokens: 773_968_359 },
          last: { totalTokens: 19_303 },
          modelContextWindow: 258_400,
        },
      },
    });

    expect(client.status.value.ctxUsed).toBe(19_303);
    expect(client.status.value.ctxRemainingPct).toBe(97);
  });

  it('keeps context presentation unavailable instead of falling back to cumulative usage', async () => {
    const api = new FakeApi();
    const client = useFocusWebClient(api);
    await client.load();
    api.handlers?.open?.();

    api.emit({
      type: 'thread_delta',
      runtime_epoch: 'epoch-1',
      revision: 1,
      thread_id: 'thread-1',
      detail: {
        method: 'thread/tokenUsage/updated',
        token_usage: {
          total: { totalTokens: 769_096_416 },
          modelContextWindow: 258_400,
        },
      },
    });

    expect(client.status.value.ctxUsed).toBe(0);
    expect(client.status.value.ctxMax).toBe(0);
    expect(client.status.value.ctxRemainingPct).toBeNull();
  });

  it('preserves dynamic approval and structured-question semantics', async () => {
    const api = new FakeApi();
    api.currentSnapshot = {
      ...snapshot('self'),
      pending_requests: [
        {
          id: 'approval-1',
          connection_generation: 1,
          response_capability: 'cap-a',
          kind: 'approval',
          method: 'item/commandExecution/requestApproval',
          thread_id: 'thread-1',
          owner_thread_id: 'thread-1',
          turn_id: 'turn-1',
          status: 'submitted',
          title: 'Command approval',
          params: {
            command: 'git status',
            proposedNetworkPolicyAmendments: [{ host: 'example.com', action: 'allow' }],
          },
          agent_name: 'Explorer',
          actions: [
            { id: 'approve_once', label: 'Allow once', style: 'primary' },
            { id: 'cancel', label: 'Cancel turn', style: 'danger' },
          ],
        },
        {
          id: 'question-1',
          connection_generation: 1,
          response_capability: 'cap-q',
          kind: 'question',
          method: 'item/tool/requestUserInput',
          thread_id: 'thread-1',
          owner_thread_id: 'thread-1',
          turn_id: 'turn-1',
          status: 'pending',
          title: 'User input required',
          params: {
            autoResolutionMs: 5000,
            questions: [{
              id: 'secret',
              question: 'Enter token',
              isSecret: true,
              isOther: true,
            }],
          },
          agent_name: 'Codex',
          actions: [],
        },
      ],
    };
    const client = useFocusWebClient(api);
    await client.load();

    expect(client.pendingApprovals.value[0]?.agentName).toBe('Explorer');
    expect(client.pendingApprovals.value[0]?.actions.map((action) => action.id)).toEqual([
      'approve_once',
      'cancel',
    ]);
    expect(client.pendingApprovals.value[0]?.block).toMatchObject({
      kind: 'shell',
      danger: expect.stringContaining('Proposed network policy'),
    });
    expect(client.pendingApprovalActions.value[client.pendingApprovals.value[0]!.approvalId]).toBe(true);
    expect(client.questions.value[0]?.autoResolutionMs).toBe(5000);
    expect(client.questions.value[0]?.questions[0]).toMatchObject({
      id: 'secret',
      secret: true,
      allowOther: true,
    });
  });

  it('preserves array enumNames labels in elicitation questions', async () => {
    const api = new FakeApi();
    api.currentSnapshot = {
      ...snapshot('self'),
      pending_requests: [{
        id: 'elicitation-1',
        connection_generation: 1,
        response_capability: 'cap-e',
        kind: 'elicitation',
        method: 'mcpServer/elicitation/request',
        thread_id: 'thread-1',
        owner_thread_id: 'thread-1',
        turn_id: 'turn-1',
        status: 'pending',
        title: 'Select scopes',
        params: {
          requestedSchema: {
            properties: {
              scopes: {
                type: 'array',
                title: 'Scopes',
                items: {
                  type: 'string',
                  enum: ['read', 'write'],
                  enumNames: ['Read only', 'Read and write'],
                },
              },
            },
          },
        },
        agent_name: 'MCP server',
        actions: [],
      }],
    };
    const client = useFocusWebClient(api);

    await client.load();

    expect(client.questions.value[0]?.questions[0]).toMatchObject({
      id: 'scopes',
      multiSelect: true,
      options: [
        { id: 'read', label: 'Read only' },
        { id: 'write', label: 'Read and write' },
      ],
    });
  });
});
