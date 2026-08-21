import { computed, ref } from 'vue';
import type { ComputedRef } from 'vue';

const STORAGE_KEY = 'focus-web.first-prompt-unknown:document';
const STORE_VERSION = 1;
const MAX_ATTEMPTS = 8;

export const THREAD_CREATE_FIRST_PROMPT_OPERATION = 'thread_create_first_prompt';

export type UnknownSubmissionRecoveryPhase = 'possibly_sent' | 'corrupt';

/** A text-only local record for a thread-create whose first turn may have run. */
export interface UnknownSubmissionDraft {
  schemaVersion: 1;
  attemptKey: string;
  attemptKind: 'thread_create_first_prompt' | 'corrupt';
  clientId: string;
  text: string;
  attachments: [];
  handoffHadAttachments: boolean;
  threadId: string;
  cwd: string;
  operation: typeof THREAD_CREATE_FIRST_PROMPT_OPERATION;
  recoveryPhase: UnknownSubmissionRecoveryPhase;
  recoveryBlocked: boolean;
}

export type UnknownSubmissionCommit = () => boolean;

export type UnknownSubmissionHandoff = (
  draft: Readonly<UnknownSubmissionDraft>,
  targetComposerScopeId: string,
) => UnknownSubmissionCommit | null | Promise<UnknownSubmissionCommit | null>;

function attemptKey(clientId: string, threadId: string): string {
  return `${clientId}\u0000${threadId}\u0000${THREAD_CREATE_FIRST_PROMPT_OPERATION}`;
}

export function createFirstPromptPossiblySentDraft(input: {
  clientId: string;
  text: string;
  threadId: string;
  cwd: string;
  hadAttachments: boolean;
}): UnknownSubmissionDraft {
  return {
    schemaVersion: STORE_VERSION,
    attemptKey: attemptKey(input.clientId, input.threadId),
    attemptKind: 'thread_create_first_prompt',
    clientId: input.clientId,
    text: input.text,
    attachments: [],
    handoffHadAttachments: input.hadAttachments,
    threadId: input.threadId,
    cwd: input.cwd,
    operation: THREAD_CREATE_FIRST_PROMPT_OPERATION,
    recoveryPhase: 'possibly_sent',
    recoveryBlocked: false,
  };
}

function exactString(value: unknown): string {
  return typeof value === 'string' && value !== '' && value === value.trim() ? value : '';
}

function corruptDraft(clientId: string): UnknownSubmissionDraft {
  return {
    schemaVersion: STORE_VERSION,
    attemptKey: `corrupt\u0000${clientId}`,
    attemptKind: 'corrupt',
    clientId,
    text: '',
    attachments: [],
    handoffHadAttachments: false,
    threadId: '',
    cwd: '',
    operation: THREAD_CREATE_FIRST_PROMPT_OPERATION,
    recoveryPhase: 'corrupt',
    recoveryBlocked: true,
  };
}

function decodeDraft(value: unknown, clientId: string): UnknownSubmissionDraft | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<UnknownSubmissionDraft>;
  const threadId = exactString(candidate.threadId);
  if (candidate.schemaVersion !== STORE_VERSION
    || candidate.attemptKind !== 'thread_create_first_prompt'
    || exactString(candidate.clientId) !== clientId
    || !threadId
    || candidate.attemptKey !== attemptKey(clientId, threadId)
    || typeof candidate.text !== 'string'
    || !Array.isArray(candidate.attachments)
    || candidate.attachments.length !== 0
    || typeof candidate.handoffHadAttachments !== 'boolean'
    || typeof candidate.cwd !== 'string'
    || candidate.operation !== THREAD_CREATE_FIRST_PROMPT_OPERATION
    || candidate.recoveryPhase !== 'possibly_sent'
    || candidate.recoveryBlocked !== false) return null;
  return createFirstPromptPossiblySentDraft({
    clientId,
    text: candidate.text,
    threadId,
    cwd: candidate.cwd,
    hadAttachments: candidate.handoffHadAttachments,
  });
}

function decodeDrafts(raw: string, clientId: string): UnknownSubmissionDraft[] {
  if (!raw) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw) as unknown;
  } catch {
    return [corruptDraft(clientId)];
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return [corruptDraft(clientId)];
  }
  const envelope = parsed as { schemaVersion?: unknown; attempts?: unknown };
  if (envelope.schemaVersion !== STORE_VERSION
    || !Array.isArray(envelope.attempts)
    || envelope.attempts.length > MAX_ATTEMPTS) return [corruptDraft(clientId)];
  const drafts: UnknownSubmissionDraft[] = [];
  const keys = new Set<string>();
  for (const value of envelope.attempts) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      const recordClientId = exactString((value as Record<string, unknown>).clientId);
      if (recordClientId && recordClientId !== clientId) continue;
    }
    const draft = decodeDraft(value, clientId);
    if (!draft || keys.has(draft.attemptKey)) return [corruptDraft(clientId)];
    keys.add(draft.attemptKey);
    drafts.push(draft);
  }
  return drafts;
}

function readDrafts(clientId: string): UnknownSubmissionDraft[] {
  try {
    return decodeDrafts(sessionStorage.getItem(STORAGE_KEY) ?? '', clientId);
  } catch {
    return [corruptDraft(clientId)];
  }
}

export interface UnknownSubmissionDraftStore {
  readonly drafts: ComputedRef<UnknownSubmissionDraft[]>;
  install(clientId: string): void;
  forThread(threadId: string): UnknownSubmissionDraft | null;
  get(attemptKey: string): UnknownSubmissionDraft | null;
  has(attemptKey: string): boolean;
  save(draft: UnknownSubmissionDraft, requireDurable?: boolean): boolean;
  remove(attemptKey: string): boolean;
}

export function createUnknownSubmissionDraftStore(): UnknownSubmissionDraftStore {
  const records = ref<Record<string, UnknownSubmissionDraft>>({});
  let installedClientId = '';
  const drafts = computed(() => Object.values(records.value).sort(
    (left, right) => left.attemptKey.localeCompare(right.attemptKey),
  ));

  function persist(next: Record<string, UnknownSubmissionDraft>, requireDurable: boolean): boolean {
    const attempts = Object.values(next);
    const serialized = JSON.stringify({ schemaVersion: STORE_VERSION, attempts });
    let persisted = false;
    try {
      if (attempts.length === 0) sessionStorage.removeItem(STORAGE_KEY);
      else sessionStorage.setItem(STORAGE_KEY, serialized);
      const readback = sessionStorage.getItem(STORAGE_KEY);
      persisted = attempts.length === 0 ? readback === null : readback === serialized;
    } catch {
      persisted = false;
    }
    if (requireDurable && !persisted) return false;
    records.value = next;
    return true;
  }

  return {
    drafts,
    install(clientId) {
      installedClientId = clientId;
      records.value = Object.fromEntries(readDrafts(clientId).map((draft) => (
        [draft.attemptKey, draft]
      )));
    },
    forThread(threadId) {
      return drafts.value.find((draft) => (
        draft.threadId === threadId || (draft.attemptKind === 'corrupt' && !draft.threadId)
      )) ?? null;
    },
    get(key) {
      return records.value[key] ?? null;
    },
    has(key) {
      return !!records.value[key];
    },
    save(draft, requireDurable = false) {
      if (draft.clientId !== installedClientId) return false;
      const current = records.value;
      if (!current[draft.attemptKey] && Object.keys(current).length >= MAX_ATTEMPTS) return false;
      return persist({ ...current, [draft.attemptKey]: draft }, requireDurable);
    },
    remove(key) {
      if (!records.value[key]) return false;
      const next = { ...records.value };
      delete next[key];
      return persist(next, true);
    },
  };
}
