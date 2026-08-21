import { describe, expect, it, vi } from 'vitest';
import { FocusWebApi } from '../src/focus/api';
import { decodeFocusPromptResultReceipt } from '../src/focus/httpResponseDecoder';
import {
  installFocusApiTestHooks,
  meta,
  registration,
} from './focus-api-test-support';

const MUTATION_ID = '00000000-0000-4000-8000-000000000001';
const RECEIPT = {
  thread_id: 'thread-1',
  mutation_id: MUTATION_ID,
  client_user_message_id: `focus-web:${MUTATION_ID}`,
  status: 'succeeded',
  mode: 'steer',
  turn_id: 'turn-1',
  reason_code: '',
} as const;

installFocusApiTestHooks();

describe('FocusWebApi single-POST prompt', () => {
  it('sends one exact prompt POST and offers a GET-only result lookup', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      const payload = path === '/api/client/register'
        ? registration()
        : path === '/api/meta'
          ? meta
          : RECEIPT;
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await expect(api.submitPrompt('thread-1', {
      text: 'steer exact turn',
      attachmentIds: ['file-1'],
      mutationId: MUTATION_ID,
      sourceScopeGeneration: 4,
      sourceAttachmentScope: 'thread:thread-1',
      sourceComposerScopeId: 'web-1:generation:4:thread:thread-1',
    })).resolves.toEqual(RECEIPT);
    await expect(api.readPromptResult('thread-1', MUTATION_ID)).resolves.toEqual(RECEIPT);

    const promptCalls = fetchMock.mock.calls.filter(([path]) => (
      String(path).includes('/prompt')
    ));
    expect(promptCalls).toHaveLength(2);
    expect(promptCalls[0]?.[0]).toBe('/api/threads/thread-1/prompt');
    expect(promptCalls[0]?.[1]).toMatchObject({ method: 'POST' });
    expect(JSON.parse(String(promptCalls[0]?.[1]?.body))).toEqual({
      text: 'steer exact turn',
      attachment_ids: ['file-1'],
      mutation_id: MUTATION_ID,
      source_scope_generation: 4,
      source_attachment_scope: 'thread:thread-1',
      source_composer_scope_id: 'web-1:generation:4:thread:thread-1',
    });
    expect(promptCalls[1]?.[0]).toBe(
      `/api/threads/thread-1/prompt-result/${MUTATION_ID}`,
    );
    expect(promptCalls[1]?.[1]).toMatchObject({ method: 'GET', body: undefined });
  });

  it('fails closed for a malformed prompt result receipt', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      const payload = path === '/api/client/register'
        ? registration()
        : path === '/api/meta'
          ? meta
          : { ...RECEIPT, client_user_message_id: 'other' };
      return new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await expect(api.submitPrompt('thread-1', {
      text: 'hello',
      attachmentIds: [],
      mutationId: MUTATION_ID,
      sourceScopeGeneration: 4,
      sourceAttachmentScope: 'thread:thread-1',
      sourceComposerScopeId: 'web-1:generation:4:thread:thread-1',
    })).rejects.toMatchObject({
      code: 'invalid_gateway_response',
      status: 502,
    });
  });

  it.each([
    [409, { error: { code: 'invalid_submission_scope', message: 'scope changed' } }, 'pre_effect'],
    [409, { error: { code: 'prompt_result_unavailable', message: 'receipt retired' } }, 'unknown'],
    [409, { error: { code: 'prompt_result_pending', message: 'already executing' } }, 'unknown'],
    [409, { error: { code: 'prompt_mutation_conflict', message: 'identity conflict' } }, 'unknown'],
    [409, { error: { code: 'unrecognized_prompt_refusal', message: 'unknown refusal' } }, 'unknown'],
    [408, { error: { code: 'invalid_submission_scope', message: 'timed out' } }, 'unknown'],
    [409, { error: { code: 'invalid_submission_scope', message: 'scope changed' }, proxy: true }, 'unknown'],
    [409, { proxy_error: 'upstream unavailable' }, 'unknown'],
    [503, { error: { code: 'invalid_submission_scope', message: 'failed' } }, 'unknown'],
  ] as const)(
    'classifies HTTP %s only from the prompt endpoint pre-effect code set',
    async (status, failure, effectEvidence) => {
      window.location.hash = '';
      const fetchMock = vi.fn(async (path: string) => {
        if (path === '/api/client/register') {
          return new Response(JSON.stringify(registration()), { status: 200 });
        }
        if (path === '/api/meta') {
          return new Response(JSON.stringify(meta), { status: 200 });
        }
        return new Response(JSON.stringify(failure), {
          status,
          headers: { 'Content-Type': 'application/json' },
        });
      });
      vi.stubGlobal('fetch', fetchMock);
      const api = new FocusWebApi();
      await api.initialize();

      await expect(api.submitPrompt('thread-1', {
        text: 'one post',
        attachmentIds: [],
        mutationId: MUTATION_ID,
        sourceScopeGeneration: 4,
        sourceAttachmentScope: 'thread:thread-1',
        sourceComposerScopeId: 'web-1:generation:4:thread:thread-1',
      })).rejects.toMatchObject({ status, effectEvidence });
    },
  );

  it('marks a local pre-fetch document refusal explicitly', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();

    await expect(api.submitPrompt('thread-1', {
      text: 'not sent',
      attachmentIds: [],
      mutationId: MUTATION_ID,
      sourceScopeGeneration: 4,
      sourceAttachmentScope: 'thread:thread-1',
      sourceComposerScopeId: 'web-1:generation:4:thread:thread-1',
    })).rejects.toMatchObject({
      code: 'document_unregistered',
      effectEvidence: 'pre_effect',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('marks a strict prompt-result miss as authoritative lookup evidence', async () => {
    window.location.hash = '';
    const fetchMock = vi.fn(async (path: string) => {
      if (path === '/api/client/register') {
        return new Response(JSON.stringify(registration()), { status: 200 });
      }
      if (path === '/api/meta') {
        return new Response(JSON.stringify(meta), { status: 200 });
      }
      return new Response(JSON.stringify({
        error: {
          code: 'prompt_result_unavailable',
          message: 'receipt evicted',
        },
      }), { status: 404 });
    });
    vi.stubGlobal('fetch', fetchMock);
    const api = new FocusWebApi();
    await api.initialize();

    await expect(api.readPromptResult('thread-1', MUTATION_ID)).rejects.toMatchObject({
      code: 'prompt_result_unavailable',
      effectEvidence: 'pre_effect',
    });
  });
});

describe('Focus prompt result receipt decoder', () => {
  it.each([
    ['steer', 'pending', 'turn-1', ''],
    ['steer', 'succeeded', 'turn-1', ''],
    ['steer', 'known_no_effect', 'turn-1', 'turn_replaced'],
    ['steer', 'outcome_unknown', 'turn-1', 'transport_unknown'],
    ['start', 'pending', '', ''],
    ['start', 'succeeded', 'turn-1', ''],
    ['start', 'known_no_effect', '', 'turn_replaced'],
    ['start', 'outcome_unknown', '', 'transport_unknown'],
  ] as const)('admits exact %s/%s receipts', (mode, status, turnId, reasonCode) => {
    const value = { ...RECEIPT, mode, status, turn_id: turnId, reason_code: reasonCode };
    expect(decodeFocusPromptResultReceipt(value)).toEqual(value);
  });

  it('rejects extra fields, mismatched client ids, and unsupported states', () => {
    expect(decodeFocusPromptResultReceipt({ ...RECEIPT, phase: 'execute' })).toBeNull();
    expect(decodeFocusPromptResultReceipt({
      ...RECEIPT,
      client_user_message_id: 'focus-web:00000000-0000-4000-8000-000000000002',
    })).toBeNull();
    expect(decodeFocusPromptResultReceipt({ ...RECEIPT, status: 'reserved' })).toBeNull();
  });

  it.each([
    ['steer', 'pending', ''],
    ['steer', 'outcome_unknown', ''],
    ['start', 'succeeded', ''],
    ['start', 'pending', 'turn-1'],
    ['start', 'known_no_effect', 'turn-1'],
    ['start', 'outcome_unknown', 'turn-1'],
  ] as const)('rejects contradictory %s/%s turn identity', (mode, status, turnId) => {
    expect(decodeFocusPromptResultReceipt({
      ...RECEIPT,
      mode,
      status,
      turn_id: turnId,
    })).toBeNull();
  });
});
