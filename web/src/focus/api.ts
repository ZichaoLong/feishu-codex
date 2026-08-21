import type {
  FocusAttachmentUpload,
  FocusBackendResetPreview,
  FocusBackendResetResult,
  FocusGatewayErrorBody,
  FocusGoalResult,
  FocusLifecycleVerificationResult,
  FocusLifecycleResult,
  FocusMeta,
  FocusMutationResult,
  FocusNextTurnSettings,
  FocusNextTurnSettingsResult,
  FocusOperatorStatus,
  FocusProjectionEvent,
  FocusPromptRequest,
  FocusPromptResultReceipt,
  FocusRenameResult,
  FocusThreadList,
  FocusThreadConversationSearchPage,
  FocusThreadScope,
  FocusThreadSnapshot,
  FocusThreadToolDetailScanPage,
  FocusToolDetailView,
  FocusToolInspectionLocator,
  FocusTurnPage,
  FocusWriterProfile,
  FocusWriterProfileResult,
} from './types';
import { FocusApiError } from './types';
import { decodeFocusProjectionEvent } from './projectionEventDecoder';
import {
  FOCUS_WEB_ENDPOINTS,
  focusWebEndpointPath,
  type FocusWebEndpointName,
} from './focusWire.generated';
import {
  decodeFocusAttachmentUpload,
  decodeFocusBackendResetPreview,
  decodeFocusBackendResetResult,
  decodeFocusBootstrapResult,
  decodeFocusDocumentRegistration,
  decodeFocusGatewayErrorBody,
  decodeFocusGoalResult,
  decodeFocusLifecycleResult,
  decodeFocusLifecycleVerificationResult,
  decodeFocusMeta,
  decodeFocusMutationResult,
  decodeFocusNextTurnSettingsResult,
  decodeFocusOperatorStatusResponse,
  decodeFocusRenameResult,
  decodeFocusRequestResponseResult,
  decodeFocusPromptResultReceipt,
  decodeFocusThreadList,
  decodeFocusThreadConversationSearchPage,
  decodeFocusThreadSnapshot,
  decodeFocusThreadToolDetailScanPage,
  decodeFocusTurnPage,
  decodeFocusWriterProfileResult,
  type FocusHttpDecoder,
} from './httpResponseDecoder';

const CLIENT_STORAGE_KEY = 'focus-web.client-id';
const OPERATOR_STATUS_REQUEST_TIMEOUT_MS = 5_000;
const PROMPT_POST_PRE_EFFECT_ERROR_CODES: ReadonlySet<string> = new Set([
  'unauthorized',
  'csrf_failed',
  'invalid_client',
  'document_unregistered',
  'document_replaced',
  'invalid_json',
  'invalid_prompt',
  'invalid_mutation_id',
  'invalid_attachment',
  'invalid_submission_scope',
  'empty_prompt',
  'invalid_thread',
  'web_writer_disconnected',
  'thread_not_materialized',
  'prompt_result_capacity',
]);
const PROMPT_RESULT_LOOKUP_PRE_EFFECT_ERROR_CODES: ReadonlySet<string> = new Set([
  'prompt_result_unavailable',
]);

function newDocumentIncarnation(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `document-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function loadClientId(): string {
  try {
    return sessionStorage.getItem(CLIENT_STORAGE_KEY)?.trim() ?? '';
  } catch {
    return '';
  }
}

function storeClientId(value: string): void {
  try {
    sessionStorage.setItem(CLIENT_STORAGE_KEY, value);
  } catch {
    // The current document remains usable; a later reload receives a fresh
    // server-issued identity when browser session storage is unavailable.
  }
}

function consumeBootstrapToken(): string {
  const raw = window.location.hash.startsWith('#') ? window.location.hash.slice(1) : '';
  const params = new URLSearchParams(raw);
  const token = params.get('token')?.trim() ?? '';
  if (raw) {
    history.replaceState(history.state, '', `${window.location.pathname}${window.location.search}`);
  }
  return token;
}

async function errorFromResponse(
  response: Response,
  options: { preEffectFocusErrorCodes?: ReadonlySet<string> } = {},
): Promise<FocusApiError> {
  let body: FocusGatewayErrorBody | null = null;
  try {
    body = decodeFocusGatewayErrorBody(await response.json() as unknown);
  } catch {
    // A proxy or browser may replace the JSON body. Preserve the HTTP status.
  }
  const code = body?.error.code ?? `http_${response.status}`;
  const message = body?.error.message || response.statusText || 'Focus Web request failed.';
  const effectEvidence = options.preEffectFocusErrorCodes?.has(code) === true
    && body !== null
    && response.status >= 400
    && response.status < 500
    // A timeout response may be emitted after a proxy has forwarded the body.
    && response.status !== 408
    ? 'pre_effect'
    : 'unknown';
  return new FocusApiError(message, {
    status: response.status,
    code,
    details: body?.error.details,
    effectEvidence,
  });
}

async function decodedJson<T>(
  response: Response,
  decoder: FocusHttpDecoder<T>,
  contract: string,
): Promise<T> {
  let raw: unknown;
  try {
    raw = await response.json() as unknown;
  } catch {
    throw new FocusApiError(`Focus Web returned non-JSON ${contract} data.`, {
      status: 502,
      code: 'invalid_gateway_response',
      details: { contract },
    });
  }
  const decoded = decoder(raw);
  if (decoded === null) {
    throw new FocusApiError(`Focus Web returned an invalid ${contract} response.`, {
      status: 502,
      code: 'invalid_gateway_response',
      details: { contract },
    });
  }
  return decoded;
}

export interface FocusEventHandlers {
  event: (event: FocusProjectionEvent) => void;
  /** The frame was not a valid Focus projection envelope. */
  invalid?: () => void;
  open?: () => void;
  close?: () => void;
}

export interface FocusWebApiPort {
  readonly clientId: string;
  readonly documentReceipt: string;
  readonly intentGenerationFloor: number;
  initialize(): Promise<FocusMeta>;
  meta(): Promise<FocusMeta>;
  operatorStatus(): Promise<FocusOperatorStatus>;
  backendResetPreview(): Promise<FocusBackendResetPreview>;
  backendResetExecute(input: {
    force: boolean;
    expectedConnectionGeneration: number;
  }): Promise<FocusBackendResetResult>;
  updateProfile(changes: Partial<Pick<FocusWriterProfile,
    'selected_thread_id' | 'working_dir'
  >>, intentGeneration?: number): Promise<FocusWriterProfileResult>;
  readNextTurnSettings(): Promise<FocusNextTurnSettingsResult>;
  updateNextTurnSettings(
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
  ): Promise<FocusNextTurnSettingsResult>;
  uploadAttachment(file: Blob, input: {
    name?: string;
    threadId?: string;
    cwd?: string;
    scopeGeneration: number;
  }): Promise<FocusAttachmentUpload>;
  attachmentBlob(fileId: string): Promise<Blob>;
  listThreads(options?: {
    search?: string;
    scope?: FocusThreadScope;
    archived?: boolean;
    allForSearch?: boolean;
  }): Promise<FocusThreadList>;
  readThread(
    threadId: string,
    intentGeneration?: number,
    turnLimit?: number,
  ): Promise<FocusThreadSnapshot>;
  listOlderTurns(
    threadId: string,
    cursor?: string,
    itemsView?: FocusTurnPage['items_view'],
    turnLimit?: number,
  ): Promise<FocusTurnPage>;
  readToolDetail(
    threadId: string,
    locator: FocusToolInspectionLocator,
    view: FocusToolDetailView,
    signal?: AbortSignal,
    cursor?: string | null,
  ): Promise<FocusThreadToolDetailScanPage>;
  searchConversation(
    threadId: string,
    query: string,
    cursor?: string | null,
    signal?: AbortSignal,
  ): Promise<FocusThreadConversationSearchPage>;
  startThread(input: {
    text: string;
    cwd: string;
    attachmentIds?: string[];
    intentGeneration?: number;
  }): Promise<FocusMutationResult>;
  submitPrompt(
    threadId: string,
    input: FocusPromptRequest,
  ): Promise<FocusPromptResultReceipt>;
  readPromptResult(
    threadId: string,
    mutationId: string,
  ): Promise<FocusPromptResultReceipt>;
  interrupt(threadId: string, turnId: string): Promise<FocusMutationResult>;
  verifyUnknownLifecycleMutation(
    threadId: string,
    mutationId: string,
  ): Promise<FocusLifecycleVerificationResult>;
  resolveUnknownMutation(
    threadId: string,
    action: 'discard' | 'retry',
    mutationId: string,
  ): Promise<FocusMutationResult>;
  renameThread(threadId: string, name: string): Promise<FocusRenameResult>;
  compactThread(threadId: string): Promise<FocusMutationResult>;
  startReview(threadId: string, target: Record<string, unknown>): Promise<FocusMutationResult>;
  getGoal(threadId: string): Promise<FocusGoalResult>;
  setGoal(threadId: string, input: { objective?: string; status?: string }, intentGeneration?: number): Promise<FocusGoalResult>;
  clearGoal(threadId: string, intentGeneration?: number): Promise<FocusGoalResult>;
  archiveThread(threadId: string): Promise<FocusLifecycleResult>;
  unarchiveThread(threadId: string): Promise<FocusLifecycleResult>;
  deleteThread(threadId: string, confirmation: string): Promise<FocusLifecycleResult>;
  respondRequest(
    requestId: string,
    connectionGeneration: number,
    responseCapability: string,
    action: string,
    answers?: Record<string, unknown>,
  ): Promise<{ accepted: true }>;
  connectEvents(handlers: FocusEventHandlers): WebSocket;
}

export class FocusWebApi implements FocusWebApiPort {
  private _clientId = loadClientId();
  private readonly documentIncarnation = newDocumentIncarnation();
  private documentToken = '';
  private _documentReceipt = '';
  private _intentGenerationFloor = 0;
  private identityReady: Promise<void> | null = null;
  private csrfToken = '';
  private pendingBootstrapToken = consumeBootstrapToken();

  get clientId(): string {
    return this._clientId;
  }

  get documentReceipt(): string {
    return this._documentReceipt;
  }

  get intentGenerationFloor(): number {
    return this._intentGenerationFloor;
  }

  private ensureDocumentIdentity(): Promise<void> {
    if (this.identityReady) return this.identityReady;
    this.identityReady = this.registerDocument().catch((error: unknown) => {
      this.identityReady = null;
      throw error;
    });
    return this.identityReady;
  }

  private async registerDocument(): Promise<void> {
    const response = await fetch(focusWebEndpointPath('client_register'), {
      method: FOCUS_WEB_ENDPOINTS.client_register.method,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        resume_client_id: this._clientId,
        incarnation_id: this.documentIncarnation,
      }),
    });
    if (!response.ok) throw await errorFromResponse(response);
    const registration = await decodedJson(
      response,
      decodeFocusDocumentRegistration,
      'document registration',
    );
    const clientId = registration.client_id;
    const documentToken = registration.document_token;
    this._clientId = clientId;
    this.documentToken = documentToken;
    this._documentReceipt = registration.document_receipt;
    this._intentGenerationFloor = registration.intent_generation_floor;
    this.csrfToken = registration.csrf_token;
    storeClientId(clientId);
  }

  async initialize(): Promise<FocusMeta> {
    const bootstrapToken = this.pendingBootstrapToken;
    if (bootstrapToken) {
      const response = await fetch(focusWebEndpointPath('auth_bootstrap'), {
        method: FOCUS_WEB_ENDPOINTS.auth_bootstrap.method,
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: bootstrapToken }),
      });
      if (!response.ok) {
        if (response.status < 500) this.pendingBootstrapToken = '';
        throw await errorFromResponse(response);
      }
      const auth = await decodedJson(response, decodeFocusBootstrapResult, 'bootstrap');
      this.csrfToken = auth.csrf_token;
      this.pendingBootstrapToken = '';
    }
    await this.ensureDocumentIdentity();
    return this.meta();
  }

  async meta(): Promise<FocusMeta> {
    const result = await this.request('meta', decodeFocusMeta, 'metadata');
    this.csrfToken = result.csrf_token;
    return result;
  }

  async operatorStatus(): Promise<FocusOperatorStatus> {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      OPERATOR_STATUS_REQUEST_TIMEOUT_MS,
    );
    try {
      return await this.request(
        'operator_status',
        decodeFocusOperatorStatusResponse,
        'operator status',
        {
          signal: controller.signal,
        },
      );
    } finally {
      clearTimeout(timeout);
    }
  }

  backendResetPreview(): Promise<FocusBackendResetPreview> {
    return this.request(
      'backend_reset_preview',
      decodeFocusBackendResetPreview,
      'backend reset preview',
    );
  }

  backendResetExecute(input: {
    force: boolean;
    expectedConnectionGeneration: number;
  }): Promise<FocusBackendResetResult> {
    return this.request(
      'backend_reset_execute',
      (value) => decodeFocusBackendResetResult(value, input.force),
      'backend reset result',
      {
        body: {
          force: input.force,
          expected_connection_generation: input.expectedConnectionGeneration,
        },
      },
    );
  }

  updateProfile(changes: Partial<Pick<FocusWriterProfile,
    'selected_thread_id' | 'working_dir'
  >>, intentGeneration = 0): Promise<FocusWriterProfileResult> {
    return this.request('profile', decodeFocusWriterProfileResult, 'writer profile', {
      body: changes,
      intentGeneration,
    });
  }

  readNextTurnSettings(): Promise<FocusNextTurnSettingsResult> {
    return this.request(
      'next_turn_settings_read',
      decodeFocusNextTurnSettingsResult,
      'next-turn settings',
    );
  }

  updateNextTurnSettings(
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
  ): Promise<FocusNextTurnSettingsResult> {
    return this.request(
      'next_turn_settings_update',
      decodeFocusNextTurnSettingsResult,
      'next-turn settings',
      { body: changes },
    );
  }

  async uploadAttachment(file: Blob, input: {
    name?: string;
    threadId?: string;
    cwd?: string;
    scopeGeneration: number;
  }): Promise<FocusAttachmentUpload> {
    const body = new FormData();
    body.append('thread_id', input.threadId?.trim() ?? '');
    body.append('cwd', input.cwd?.trim() ?? '');
    body.append('scope_generation', String(input.scopeGeneration));
    body.append('file', file, input.name?.trim() || 'attachment');
    const response = await fetch(focusWebEndpointPath('attachment_upload'), {
      method: FOCUS_WEB_ENDPOINTS.attachment_upload.method,
      credentials: 'same-origin',
      headers: {
        'X-Focus-Web-Client': this.clientId,
        'X-Focus-Web-Document': this.requireDocumentToken(),
        'X-Focus-Web-Csrf': this.csrfToken,
      },
      body,
    });
    if (!response.ok) throw await errorFromResponse(response);
    return decodedJson(response, decodeFocusAttachmentUpload, 'attachment upload');
  }

  async attachmentBlob(fileId: string): Promise<Blob> {
    const response = await fetch(focusWebEndpointPath(
      'attachment_download',
      { attachment_id: fileId },
    ), {
      credentials: 'same-origin',
    });
    if (!response.ok) throw await errorFromResponse(response);
    return await response.blob();
  }

  listThreads(options: {
    search?: string;
    scope?: FocusThreadScope;
    archived?: boolean;
    allForSearch?: boolean;
  } = {}): Promise<FocusThreadList> {
    const params = new URLSearchParams();
    const search = options.search?.trim() ?? '';
    if (search) params.set('search', search);
    params.set('scope', options.scope ?? 'global');
    if (options.archived) params.set('archived', 'true');
    if (options.allForSearch) params.set('all_for_search', 'true');
    return this.request(
      'thread_list',
      decodeFocusThreadList,
      'thread list',
      { query: params },
    );
  }

  readThread(
    threadId: string,
    intentGeneration = 0,
    turnLimit = 10,
  ): Promise<FocusThreadSnapshot> {
    const query = new URLSearchParams({ turn_limit: String(turnLimit) });
    return this.request(
      'thread_read',
      decodeFocusThreadSnapshot,
      'thread snapshot',
      { parameters: { thread_id: threadId }, query, intentGeneration },
    );
  }

  listOlderTurns(
    threadId: string,
    cursor = '',
    itemsView: FocusTurnPage['items_view'] = 'full',
    turnLimit = 10,
  ): Promise<FocusTurnPage> {
    const params = new URLSearchParams({
      items_view: itemsView,
      turn_limit: String(turnLimit),
    });
    if (cursor) params.set('cursor', cursor);
    return this.request(
      'thread_turns',
      decodeFocusTurnPage,
      'turn page',
      { parameters: { thread_id: threadId }, query: params },
    );
  }

  readToolDetail(
    threadId: string,
    locator: FocusToolInspectionLocator,
    view: FocusToolDetailView,
    signal?: AbortSignal,
    cursor: string | null = null,
  ): Promise<FocusThreadToolDetailScanPage> {
    const query = new URLSearchParams({ view });
    if (locator.change_index !== null) {
      query.set('change_index', String(locator.change_index));
    }
    if (cursor !== null) query.set('cursor', cursor);
    return this.request(
      'thread_tool_detail',
      (value) => decodeFocusThreadToolDetailScanPage(
        value,
        threadId,
        locator,
        cursor,
        view,
      ),
      'thread tool detail',
      {
        parameters: {
          thread_id: threadId,
          turn_id: locator.turn_id,
          item_id: locator.item_id,
        },
        query,
        signal,
      },
    );
  }

  searchConversation(
    threadId: string,
    query: string,
    cursor: string | null = null,
    signal?: AbortSignal,
  ): Promise<FocusThreadConversationSearchPage> {
    const normalizedQuery = query.trim();
    const params = new URLSearchParams({ query: normalizedQuery });
    if (cursor !== null) params.set('cursor', cursor);
    return this.request(
      'thread_conversation_search',
      (value) => decodeFocusThreadConversationSearchPage(
        value,
        threadId,
        normalizedQuery,
        cursor,
      ),
      'conversation search page',
      { parameters: { thread_id: threadId }, query: params, signal },
    );
  }

  startThread(input: {
    text: string;
    cwd: string;
    attachmentIds?: string[];
    intentGeneration?: number;
  }): Promise<FocusMutationResult> {
    return this.request('thread_start', decodeFocusMutationResult, 'thread mutation', {
      body: {
        text: input.text,
        cwd: input.cwd,
        attachment_ids: input.attachmentIds ?? [],
      },
      intentGeneration: input.intentGeneration ?? 0,
    });
  }

  submitPrompt(
    threadId: string,
    input: FocusPromptRequest,
  ): Promise<FocusPromptResultReceipt> {
    return this.request(
      'thread_prompt',
      decodeFocusPromptResultReceipt,
      'prompt result receipt',
      {
        parameters: { thread_id: threadId },
        body: {
          text: input.text,
          attachment_ids: input.attachmentIds,
          mutation_id: input.mutationId,
          source_scope_generation: input.sourceScopeGeneration,
          source_attachment_scope: input.sourceAttachmentScope,
          source_composer_scope_id: input.sourceComposerScopeId,
        },
        preEffectFocusErrorCodes: PROMPT_POST_PRE_EFFECT_ERROR_CODES,
      },
    );
  }

  readPromptResult(
    threadId: string,
    mutationId: string,
  ): Promise<FocusPromptResultReceipt> {
    return this.request(
      'thread_prompt_result',
      decodeFocusPromptResultReceipt,
      'prompt result receipt',
      {
        parameters: { thread_id: threadId, mutation_id: mutationId },
        // This endpoint is a pure registry GET. A strict Focus 4xx envelope
        // proves only lookup refusal/unavailability and never prompt effect.
        preEffectFocusErrorCodes: PROMPT_RESULT_LOOKUP_PRE_EFFECT_ERROR_CODES,
      },
    );
  }

  interrupt(threadId: string, turnId: string): Promise<FocusMutationResult> {
    return this.request(
      'thread_interrupt',
      decodeFocusMutationResult,
      'interrupt mutation',
      { parameters: { thread_id: threadId }, body: { turn_id: turnId } },
    );
  }

  verifyUnknownLifecycleMutation(
    threadId: string,
    mutationId: string,
  ): Promise<FocusLifecycleVerificationResult> {
    return this.request(
      'thread_unknown_mutation',
      decodeFocusLifecycleVerificationResult,
      'lifecycle verification',
      {
        parameters: { thread_id: threadId },
        body: { action: 'verify_lifecycle', mutation_id: mutationId },
      },
    );
  }

  resolveUnknownMutation(
    threadId: string,
    action: 'discard' | 'retry',
    mutationId: string,
  ): Promise<FocusMutationResult> {
    return this.request(
      'thread_unknown_mutation',
      decodeFocusMutationResult,
      'unknown mutation resolution',
      {
        parameters: { thread_id: threadId },
        body: {
          action,
          mutation_id: mutationId,
        },
      },
    );
  }

  renameThread(threadId: string, name: string): Promise<FocusRenameResult> {
    return this.request(
      'thread_rename',
      decodeFocusRenameResult,
      'thread rename',
      { parameters: { thread_id: threadId }, body: { name } },
    );
  }

  compactThread(threadId: string): Promise<FocusMutationResult> {
    return this.request(
      'thread_compact',
      decodeFocusMutationResult,
      'compact mutation',
      { parameters: { thread_id: threadId } },
    );
  }

  startReview(threadId: string, target: Record<string, unknown>): Promise<FocusMutationResult> {
    return this.request(
      'thread_review',
      decodeFocusMutationResult,
      'review mutation',
      { parameters: { thread_id: threadId }, body: { target } },
    );
  }

  getGoal(threadId: string): Promise<FocusGoalResult> {
    return this.request(
      'thread_goal_read',
      decodeFocusGoalResult,
      'goal',
      { parameters: { thread_id: threadId } },
    );
  }

  setGoal(
    threadId: string,
    input: { objective?: string; status?: string },
    intentGeneration = 0,
  ): Promise<FocusGoalResult> {
    return this.request(
      'thread_goal_set',
      decodeFocusGoalResult,
      'goal mutation',
      { parameters: { thread_id: threadId }, body: input, intentGeneration },
    );
  }

  clearGoal(threadId: string, intentGeneration = 0): Promise<FocusGoalResult> {
    return this.request(
      'thread_goal_clear',
      decodeFocusGoalResult,
      'goal clear mutation',
      { parameters: { thread_id: threadId }, intentGeneration },
    );
  }

  archiveThread(threadId: string): Promise<FocusLifecycleResult> {
    return this.request(
      'thread_archive',
      decodeFocusLifecycleResult,
      'archive mutation',
      { parameters: { thread_id: threadId } },
    );
  }

  unarchiveThread(threadId: string): Promise<FocusLifecycleResult> {
    return this.request(
      'thread_unarchive',
      decodeFocusLifecycleResult,
      'unarchive mutation',
      { parameters: { thread_id: threadId } },
    );
  }

  deleteThread(threadId: string, confirmation: string): Promise<FocusLifecycleResult> {
    return this.request(
      'thread_delete',
      decodeFocusLifecycleResult,
      'delete mutation',
      { parameters: { thread_id: threadId }, body: { confirmation } },
    );
  }

  respondRequest(
    requestId: string,
    connectionGeneration: number,
    responseCapability: string,
    action: string,
    answers: Record<string, unknown> = {},
  ): Promise<{ accepted: true }> {
    return this.request(
      'request_respond',
      decodeFocusRequestResponseResult,
      'request response mutation',
      {
        parameters: { request_id: requestId },
        body: {
          action,
          answers,
          connection_generation: connectionGeneration,
          response_capability: responseCapability,
        },
      },
    );
  }

  connectEvents(handlers: FocusEventHandlers): WebSocket {
    if (!this.csrfToken) throw new FocusApiError('Missing Focus Web CSRF token.', {
      status: 401,
      code: 'missing_csrf',
      effectEvidence: 'pre_effect',
    });
    const documentToken = this.requireDocumentToken();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams({
      client: this.clientId,
      document: documentToken,
      csrf: this.csrfToken,
    });
    const socket = new WebSocket(
      `${protocol}//${window.location.host}${focusWebEndpointPath('events')}?${params.toString()}`,
    );
    socket.addEventListener('open', () => handlers.open?.());
    socket.addEventListener('close', () => handlers.close?.());
    socket.addEventListener('message', (message) => {
      if (message.data === 'pong') return;
      if (typeof message.data !== 'string') {
        handlers.invalid?.();
        return;
      }
      try {
        const event = decodeFocusProjectionEvent(JSON.parse(message.data) as unknown);
        if (event) handlers.event(event);
        else handlers.invalid?.();
      } catch {
        handlers.invalid?.();
      }
    });
    return socket;
  }

  private async request<T>(
    endpointName: FocusWebEndpointName,
    decoder: FocusHttpDecoder<T>,
    contract: string,
    options: {
      parameters?: Readonly<Record<string, string>>;
      query?: URLSearchParams;
      body?: Record<string, unknown>;
      intentGeneration?: number;
      signal?: AbortSignal;
      preEffectFocusErrorCodes?: ReadonlySet<string>;
  } = {},
  ): Promise<T> {
    const endpoint = FOCUS_WEB_ENDPOINTS[endpointName];
    const method = endpoint.method;
    const query = options.query?.toString() ?? '';
    const path = `${focusWebEndpointPath(endpointName, options.parameters)}${query ? `?${query}` : ''}`;
    const headers: Record<string, string> = {
      'X-Focus-Web-Client': this.clientId,
      'X-Focus-Web-Document': this.requireDocumentToken(),
    };
    if ((options.intentGeneration ?? 0) > 0) {
      headers['X-Focus-Web-Intent'] = String(options.intentGeneration);
    }
    if (method !== 'GET') {
      headers['Content-Type'] = 'application/json';
      headers['X-Focus-Web-Csrf'] = this.csrfToken;
    }
    const response = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers,
      body: options.body ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
    if (!response.ok) {
      throw await errorFromResponse(response, {
        preEffectFocusErrorCodes: options.preEffectFocusErrorCodes,
      });
    }
    return decodedJson(response, decoder, contract);
  }

  private requireDocumentToken(): string {
    if (this._clientId && this.documentToken) return this.documentToken;
    throw new FocusApiError('This browser document must be registered before it can use Focus Web.', {
      status: 409,
      code: 'document_unregistered',
      effectEvidence: 'pre_effect',
    });
  }
}
