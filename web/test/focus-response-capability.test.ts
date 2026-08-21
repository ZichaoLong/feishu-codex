import { describe, expect, it, vi } from 'vitest';
import type { FocusWebApiPort } from '../src/focus/api';
import {
  pendingRequestActionToken,
  respondPendingRequest,
} from '../src/focus/pendingRequestCapability';
import type { FocusPendingRequest } from '../src/focus/types';

function pending(responseCapability: string): FocusPendingRequest {
  return {
    id: 'same-request-id',
    connection_generation: 1,
    response_capability: responseCapability,
    kind: 'approval',
    method: 'item/commandExecution/requestApproval',
    thread_id: 'thread-1',
    turn_id: 'turn-1',
    status: 'pending',
    title: 'Approve command',
    params: {},
    owner_thread_id: 'thread-1',
    agent_name: 'Codex',
    actions: [],
  };
}

describe('Focus pending-response capability', () => {
  it('keeps an old render callback bound to its old capability after same-id replacement', async () => {
    const respondRequest = vi.fn().mockResolvedValue({ accepted: true });
    const api = { respondRequest } as unknown as FocusWebApiPort;
    let projection = pending('old-capability');

    const renderedActionToken = pendingRequestActionToken(projection);
    const oldRenderCallback = () => respondPendingRequest(
      api,
      renderedActionToken,
      'approve_once',
    );

    projection = pending('replacement-capability');
    expect(pendingRequestActionToken(projection)).not.toBe(renderedActionToken);
    await oldRenderCallback();

    expect(respondRequest).toHaveBeenCalledOnce();
    expect(respondRequest).toHaveBeenCalledWith(
      'same-request-id',
      1,
      'old-capability',
      'approve_once',
      {},
    );
  });

  it('does not fall back from a malformed action token to the current projection', async () => {
    const respondRequest = vi.fn();
    const api = { respondRequest } as unknown as FocusWebApiPort;

    await respondPendingRequest(api, 'same-request-id', 'approve_once');

    expect(respondRequest).not.toHaveBeenCalled();
  });
});
