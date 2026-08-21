import { ref } from 'vue';
import type { Ref } from 'vue';
import type { ChatTurn, ToolCall, TurnBlock } from '../types';
import type { FocusWebApiPort } from './api';
import type { ClientIntentClock } from './clientIntentClock';
import type { FocusNavigationProfile } from './focusNavigationProfile';
import type { WebNextTurnSettingsOwner } from './client-state/web-next-turn-settings';
import { ThreadEventRevisionIndex } from './client-state/thread-event-revisions';
import type { UnknownMutationSnapshotReceipt } from './client-state/thread-mutations';
import { decodeFocusThreadDeltaDetail } from './projectionEventDecoder';
import {
  appendBoundedToolOutput,
  boundToolOutputWindow,
} from './toolOutputPresentation';
import {
  boundRecentFullTurns,
  FOCUS_RECENT_FULL_TURN_LIMIT,
  projectedRawTurnKey,
} from './focusHistoryNavigation';
import type {
  FocusCoordinates,
  FocusGoalResult,
  FocusMeta,
  FocusMetaEnvelope,
  FocusMutationDisposition,
  FocusProjectionEvent,
  FocusThreadDeltaDetail,
  FocusThreadSnapshot,
  FocusThreadSummary,
} from './types';
import { isStaleWebReadError } from './types';

const THREAD_LIST_DELTA_METHODS = new Set([
  'thread/name/updated',
  'thread/status/changed',
  'turn/completed',
  'turn/started',
]);
const THREAD_LIST_EVENT_TYPES = new Set([
  'owner_changed',
  'pending_request_changed',
  'thread_invalidated',
]);
const AUTHORITATIVE_LIFECYCLE_REASONS = new Set([
  'thread/archived',
  'thread/deleted',
  'thread/unarchived',
]);
const THREAD_REMOVAL_REASONS = new Set(['thread/archived', 'thread/deleted']);
const ACTIVE_TURN_DISCLOSURE_DELTA_METHODS = new Set([
  'model/rerouted',
  'thread/archived',
  'thread/closed',
  'thread/deleted',
  'thread/settings/updated',
  'turn/completed',
  'turn/started',
]);
const STREAM_PRESENTATION_INTERVAL_MS = 1_000;

type ProjectionRequestChannel =
  | 'activeThread'
  | 'threadList'
  | 'archivedList'
  | 'searchList';

class ProjectionRequestGenerations {
  private readonly values: Record<ProjectionRequestChannel, number> = {
    activeThread: 0,
    threadList: 0,
    archivedList: 0,
    searchList: 0,
  };

  begin(channel: ProjectionRequestChannel): number {
    this.values[channel] += 1;
    return this.values[channel];
  }

  isCurrent(channel: ProjectionRequestChannel, generation: number): boolean {
    return this.values[channel] === generation;
  }
}

export interface FocusProjectionTransportPort {
  hasOpenedEventSocket(): boolean;
  requestProjectionReload(): void;
  scheduleProjectionReloadRetry(): void;
  cancelProjectionReloadRetry(): void;
  resetProjectionReloadBackoff(): void;
  scheduleProjectionRefresh(): void;
  scheduleThreadListRefresh(): void;
}

export interface FocusProjectionSyncOptions {
  api: FocusWebApiPort;
  intentClock: ClientIntentClock;
  turnWindowLimit: Readonly<Ref<number>>;
  navigation: Pick<
    FocusNavigationProfile,
    | 'activeThreadId'
    | 'threadScope'
    | 'isDisposed'
    | 'currentNavigationStatus'
    | 'navigationRepairIsRequired'
    | 'installInitialProfile'
    | 'installObservedProfile'
    | 'captureNavigationStateFloor'
    | 'navigationRepairIsCurrent'
    | 'requireNavigationRepair'
  >;
  settings: Pick<WebNextTurnSettingsOwner, 'installRuntimeSnapshot' | 'refresh'>;
  transport: FocusProjectionTransportPort;
  reportError(error: unknown): void;
  requireAuthentication(): void;
  projectionAccessIsAvailable(): boolean;
  currentErrorMessage(): string;
  clearErrorMessageIf(message: string): void;
  activeThreadWasRemoved(): void;
  captureUnknownMutationSnapshot(threadId: string): UnknownMutationSnapshotReceipt;
  reconcileUnknownMutation(
    receipt: UnknownMutationSnapshotReceipt,
    mutation: FocusThreadSnapshot['mutation_unknown'],
  ): boolean;
  settleUnknownMutationFromEvent(
    threadId: string,
    mutationId: string,
    operation: string,
    disposition: FocusMutationDisposition,
  ): void;
}

export interface RefreshActiveThreadOptions {
  requestIntentGeneration?: number;
  navigationGeneration?: number;
  navigationAuthorityGeneration?: number;
}

export interface FocusProjectionSync {
  readonly meta: Readonly<Ref<FocusMetaEnvelope | null>>;
  readonly threads: Readonly<Ref<FocusThreadSummary[]>>;
  readonly searchThreads: Readonly<Ref<FocusThreadSummary[]>>;
  readonly archivedThreads: Readonly<Ref<FocusThreadSummary[]>>;
  readonly snapshot: Readonly<Ref<FocusThreadSnapshot | null>>;
  readonly runtimeEpoch: Readonly<Ref<string>>;
  readonly revision: Readonly<Ref<number>>;
  readonly snapshotInvalidated: Readonly<Ref<boolean>>;
  readonly archivedLoading: Readonly<Ref<boolean>>;
  readonly archivedTruncated: Readonly<Ref<boolean>>;
  readonly archivedLimit: Readonly<Ref<number>>;
  readonly reloadInFlight: Readonly<Ref<boolean>>;
  installInitialMeta(value: FocusMeta): void;
  clearSnapshot(): void;
  settleDeletedThread(threadId: string, clearActiveProjection: boolean): void;
  installGoalResult(threadId: string, result: FocusGoalResult): void;
  refreshThreads(): Promise<boolean>;
  refreshArchivedThreads(): Promise<void>;
  refreshActiveThread(options?: RefreshActiveThreadOptions): Promise<boolean>;
  scheduleProjectionRefresh(): void;
  loadAllSessionsForSearch(): Promise<void>;
  reloadAll(): Promise<void>;
  handleEvent(event: FocusProjectionEvent): void;
  dispose(): void;
  invalidateWireProjection(): void;
  applyTurnWindowLimit(): void;
  mayRetryProjectionReload(): boolean;
}

function blockStreamItemId(block: TurnBlock): string {
  if (block.kind === 'tool') return block.tool.id;
  return block.itemId ?? '';
}

function mergeLiveStreamBlock(previous: TurnBlock, incoming: TurnBlock): TurnBlock {
  if (previous.kind !== incoming.kind) return incoming;
  if (incoming.kind === 'text' && previous.kind === 'text') {
    if (!incoming.text || previous.text.startsWith(incoming.text)) {
      return { ...incoming, text: previous.text };
    }
    return incoming;
  }
  if (incoming.kind === 'thinking' && previous.kind === 'thinking') {
    if (!incoming.thinking || previous.thinking.startsWith(incoming.thinking)) {
      return { ...incoming, thinking: previous.thinking };
    }
    return incoming;
  }
  if (incoming.kind === 'tool' && previous.kind === 'tool') {
    const incomingOutput = incoming.tool.output ?? [];
    const previousOutput = previous.tool.output ?? [];
    const incomingIsFullyOmitted = incomingOutput.length === 0
      && incoming.tool.outputTruncated === true
      && (incoming.tool.outputOmittedChars ?? 0) > 0
      && incoming.tool.outputHeadLineCount === 0;
    if (incomingIsFullyOmitted) return incoming;
    const incomingIsPrefix = incomingOutput.every((line, index) => previousOutput[index] === line);
    return incomingIsPrefix
      ? {
          ...incoming,
          tool: {
            ...incoming.tool,
            output: previousOutput,
            outputOmittedChars: previous.tool.outputOmittedChars,
            outputHeadLineCount: previous.tool.outputHeadLineCount,
            outputTruncated: previous.tool.outputTruncated,
          },
        }
      : incoming;
  }
  return incoming;
}

function boundProjectionTurns(turns: readonly ChatTurn[], limit: number): ChatTurn[] {
  return boundToolOutputWindow(boundRecentFullTurns(turns, limit));
}

function assistantTextFromBlocks(blocks: TurnBlock[]): string {
  return blocks
    .filter((block): block is Extract<TurnBlock, { kind: 'text' }> => block.kind === 'text')
    .map((block) => block.text)
    .filter(Boolean)
    .join('\n\n');
}

function mergeLiveAssistantTurn(previous: ChatTurn, incoming: ChatTurn): ChatTurn {
  if (
    previous.role !== 'assistant'
    || incoming.role !== 'assistant'
    || incoming.status !== 'inProgress'
  ) return incoming;
  const previousBlocks = previous.blocks ?? [];
  const incomingBlocks = incoming.blocks ?? [];
  if (previousBlocks.length === 0) return incoming;

  const previousByItemId = new Map(
    previousBlocks
      .map((block) => [blockStreamItemId(block), block] as const)
      .filter(([itemId]) => !!itemId),
  );
  const incomingItemIds = new Set<string>();
  const blocks = incomingBlocks.map((block) => {
    const itemId = blockStreamItemId(block);
    if (!itemId) return block;
    incomingItemIds.add(itemId);
    const locallyStreamed = previousByItemId.get(itemId);
    return locallyStreamed ? mergeLiveStreamBlock(locallyStreamed, block) : block;
  });
  for (const block of previousBlocks) {
    const itemId = blockStreamItemId(block);
    if (itemId && !incomingItemIds.has(itemId)) blocks.push(block);
  }
  const tools = blocks
    .filter((block): block is Extract<TurnBlock, { kind: 'tool' }> => block.kind === 'tool')
    .map((block) => block.tool);
  return {
    ...incoming,
    text: assistantTextFromBlocks(blocks),
    blocks,
    tools,
  };
}

function mergeCanonicalRawTurnSegments(
  previousTurns: readonly ChatTurn[], incomingTurns: readonly ChatTurn[],
): ChatTurn[] {
  const previousById = new Map(previousTurns.map((turn) => [turn.id, turn]));
  const canonical = incomingTurns.map((turn) => {
    const previous = previousById.get(turn.id);
    return previous ? mergeLiveAssistantTurn(previous, turn) : turn;
  });
  const canonicalById = new Map(canonical.map((turn) => [turn.id, turn]));
  const canonicalIds = new Set(canonical.map((turn) => turn.id));
  const canonicalByRawTurn = new Map<string, ChatTurn[]>();
  for (const turn of canonical) {
    const rawTurnKey = projectedRawTurnKey(turn);
    if (!rawTurnKey) continue;
    const group = canonicalByRawTurn.get(rawTurnKey) ?? [];
    group.push(turn);
    canonicalByRawTurn.set(rawTurnKey, group);
  }

  const emittedRawTurns = new Set<string>();
  const emittedIds = new Set<string>();
  const merged: ChatTurn[] = [];
  for (const previous of previousTurns) {
    const rawTurnKey = projectedRawTurnKey(previous);
    const canonicalGroup = rawTurnKey ? canonicalByRawTurn.get(rawTurnKey) : undefined;
    if (rawTurnKey && canonicalGroup) {
      if (!emittedRawTurns.has(rawTurnKey)) {
        merged.push(...canonicalGroup);
        merged.push(...previousTurns.filter((turn) => (
          projectedRawTurnKey(turn) === rawTurnKey && !canonicalIds.has(turn.id)
        )));
        emittedRawTurns.add(rawTurnKey);
      }
      continue;
    }
    const replacement = canonicalById.get(previous.id);
    if (replacement) {
      merged.push(replacement);
      emittedIds.add(replacement.id);
    } else {
      merged.push(previous);
    }
  }
  for (const turn of canonical) {
    const rawTurnKey = projectedRawTurnKey(turn);
    if (rawTurnKey) {
      if (emittedRawTurns.has(rawTurnKey)) continue;
      const canonicalGroup = canonicalByRawTurn.get(rawTurnKey) ?? [];
      merged.push(...canonicalGroup);
      emittedRawTurns.add(rawTurnKey);
    } else if (!emittedIds.has(turn.id)) {
      merged.push(turn);
    }
  }
  return merged;
}

function isAssistantSegmentForRawTurn(turn: ChatTurn, turnId: string): boolean {
  if (turn.role !== 'assistant') return false;
  const firstSegmentId = `${turnId}:assistant`;
  return turn.id === firstSegmentId || turn.id.startsWith(`${firstSegmentId}:`);
}

function isUserSegmentForRawTurn(turn: ChatTurn, turnId: string): boolean {
  if (turn.role !== 'user') return false;
  const firstSegmentId = `${turnId}:user`;
  return turn.id === firstSegmentId || turn.id.startsWith(`${firstSegmentId}:`);
}

function nextAssistantSegmentId(turnId: string, turns: ChatTurn[]): string {
  const firstSegmentId = `${turnId}:assistant`;
  let largest = 0;
  for (const turn of turns) {
    if (!isAssistantSegmentForRawTurn(turn, turnId)) continue;
    if (turn.id === firstSegmentId) {
      largest = Math.max(largest, 1);
      continue;
    }
    const suffix = turn.id.slice(firstSegmentId.length + 1);
    const ordinal = Number.parseInt(suffix, 10);
    if (Number.isInteger(ordinal) && ordinal > 0 && String(ordinal) === suffix) {
      largest = Math.max(largest, ordinal);
    }
  }
  return largest === 0 ? firstSegmentId : `${firstSegmentId}:${largest + 1}`;
}

function assistantSegmentWithItem(turn: ChatTurn, itemId: string): boolean {
  return turn.role === 'assistant'
    && (turn.blocks ?? []).some((block) => blockStreamItemId(block) === itemId);
}

function createLiveAssistantSegment(turnId: string, turns: ChatTurn[]): ChatTurn {
  return {
    id: nextAssistantSegmentId(turnId, turns),
    role: 'assistant',
    no: Math.max(0, ...turns.map((turn) => turn.no)) + 1,
    text: '',
    blocks: [],
    tools: [],
    status: 'inProgress',
  };
}

function insertLiveAssistantSegment(turnId: string, turns: ChatTurn[]): number {
  const segment = createLiveAssistantSegment(turnId, turns);
  let lastRelatedIndex = -1;
  let lastRelatedRole: 'user' | 'assistant' | null = null;
  for (let index = 0; index < turns.length; index += 1) {
    const turn = turns[index]!;
    if (isUserSegmentForRawTurn(turn, turnId)) {
      lastRelatedIndex = index;
      lastRelatedRole = 'user';
    } else if (isAssistantSegmentForRawTurn(turn, turnId)) {
      lastRelatedIndex = index;
      lastRelatedRole = 'assistant';
    }
  }
  if (lastRelatedIndex >= 0 && lastRelatedRole === 'assistant') return lastRelatedIndex;
  const insertAt = lastRelatedIndex >= 0 ? lastRelatedIndex + 1 : turns.length;
  turns.splice(insertAt, 0, segment);
  return insertAt;
}

function metaEnvelope(value: FocusMeta): FocusMetaEnvelope {
  const envelope = { ...value };
  delete (envelope as Partial<FocusMeta>).writer_profile;
  delete (envelope as Partial<FocusMeta>).next_turn_settings;
  return envelope;
}

function threadDeltaChangesActiveTurnDisclosure(
  detail: FocusThreadDeltaDetail,
  currentActiveTurnId: string | null,
): boolean {
  if (ACTIVE_TURN_DISCLOSURE_DELTA_METHODS.has(detail.method)) return true;
  if (
    currentActiveTurnId !== null
    && detail.active_turn_id !== undefined
    && detail.active_turn_id !== currentActiveTurnId
  ) return true;
  return detail.method === 'thread/status/changed'
    && detail.thread_status?.type !== 'active';
}

export function createFocusProjectionSync(
  options: FocusProjectionSyncOptions,
): FocusProjectionSync {
  const meta = ref<FocusMetaEnvelope | null>(null);
  const threads = ref<FocusThreadSummary[]>([]);
  const searchThreads = ref<FocusThreadSummary[]>([]);
  const archivedThreads = ref<FocusThreadSummary[]>([]);
  const snapshot = ref<FocusThreadSnapshot | null>(null);
  const runtimeEpoch = ref('');
  const revision = ref(0);
  const snapshotInvalidated = ref(false);
  const archivedLoading = ref(false);
  const archivedTruncated = ref(false);
  const archivedLimit = ref(0);
  const reloadInFlight = ref(false);
  const requests = new ProjectionRequestGenerations();
  const threadEventRevision = new ThreadEventRevisionIndex();
  const activeTurnDisclosureRevision = new ThreadEventRevisionIndex();
  const goalResultFloors = new Map<string, FocusGoalResult>();
  const threadSnapshotFloors = new Map<string, FocusCoordinates>();
  let reloadPromise: Promise<void> | null = null;
  let snapshotReloadErrorMessage = '';
  let installingSnapshot = false;
  let snapshotInstallFloor: FocusCoordinates | null = null;
  let bufferedEvents: FocusProjectionEvent[] = [];
  let reloadAfterInstall = false;
  let globalActiveTurnDisclosureRevision: FocusCoordinates | null = null;
  let pendingStreamEvents: FocusProjectionEvent[] = [];
  let pendingStreamTimer: ReturnType<typeof setTimeout> | null = null;
  const disposedResult = Symbol('disposed projection result');

  function readThreadWindow(threadId: string, intentGeneration: number) {
    const turnLimit = options.turnWindowLimit.value;
    return turnLimit === FOCUS_RECENT_FULL_TURN_LIMIT
      ? options.api.readThread(threadId, intentGeneration)
      : options.api.readThread(threadId, intentGeneration, turnLimit);
  }

  function cancelPendingStreamFlush(): void {
    if (pendingStreamTimer !== null) clearTimeout(pendingStreamTimer);
    pendingStreamTimer = null;
  }

  function flushPendingStreamEvents(): void {
    cancelPendingStreamFlush();
    if (pendingStreamEvents.length === 0) return;
    const queued = pendingStreamEvents;
    pendingStreamEvents = [];
    if (options.navigation.isDisposed) return;
    // Preserve every wire coordinate and event ordering. Vue coalesces these
    // synchronous ref writes into one render, which removes the expensive
    // Markdown/DOM/scroll pass between consecutive stream deltas.
    for (const event of queued) processEvent(event);
  }

  function dispose(): void {
    cancelPendingStreamFlush();
    pendingStreamEvents = [];
  }

  function schedulePendingStreamFlush(): void {
    if (pendingStreamTimer !== null) return;
    pendingStreamTimer = setTimeout(() => {
      pendingStreamTimer = null;
      flushPendingStreamEvents();
    }, STREAM_PRESENTATION_INTERVAL_MS);
  }

  function isStreamPresentationEvent(event: FocusProjectionEvent): boolean {
    if (event.type !== 'thread_delta') return false;
    return decodeFocusThreadDeltaDetail(event.detail)?.stream_delta !== undefined;
  }
  async function awaitProjectionResult<T>(request: Promise<T>): Promise<T | typeof disposedResult> {
    try {
      const result = await request;
      return options.navigation.isDisposed ? disposedResult : result;
    } catch (error) {
      if (options.navigation.isDisposed) return disposedResult;
      throw error;
    }
  }

  function applyCoordinates(
    value: FocusCoordinates,
    coordinateOptions: { advanceRevision?: boolean } = {},
  ): void {
    if (!value.runtime_epoch) return;
    if (runtimeEpoch.value !== value.runtime_epoch) {
      threadEventRevision.clear();
      activeTurnDisclosureRevision.clear();
      globalActiveTurnDisclosureRevision = null;
      goalResultFloors.clear();
      threadSnapshotFloors.clear();
      runtimeEpoch.value = value.runtime_epoch;
      revision.value = value.revision;
      return;
    }
    if (coordinateOptions.advanceRevision !== false) {
      revision.value = Math.max(revision.value, value.revision);
    }
  }

  function applySnapshotCoordinates(value: FocusCoordinates): void {
    if (installingSnapshot) {
      if (!value.runtime_epoch) return;
      if (!snapshotInstallFloor) {
        snapshotInstallFloor = { ...value };
        return;
      }
      if (snapshotInstallFloor.runtime_epoch !== value.runtime_epoch) {
        reloadAfterInstall = true;
        return;
      }
      snapshotInstallFloor = {
        runtime_epoch: value.runtime_epoch,
        revision: Math.min(snapshotInstallFloor.revision, value.revision),
      };
      return;
    }
    applyCoordinates(value, { advanceRevision: !options.transport.hasOpenedEventSocket() });
  }

  function commitSnapshotInstallFloor(replayBase: FocusCoordinates): FocusCoordinates {
    const installedFloor = snapshotInstallFloor;
    snapshotInstallFloor = null;
    if (!installedFloor) return { runtime_epoch: runtimeEpoch.value, revision: revision.value };
    const installedRevision = replayBase.runtime_epoch === installedFloor.runtime_epoch
      ? Math.max(replayBase.revision, installedFloor.revision)
      : installedFloor.revision;
    if (runtimeEpoch.value !== installedFloor.runtime_epoch) {
      threadEventRevision.clear();
      activeTurnDisclosureRevision.clear();
      globalActiveTurnDisclosureRevision = null;
    }
    runtimeEpoch.value = installedFloor.runtime_epoch;
    revision.value = installedRevision;
    return { runtime_epoch: installedFloor.runtime_epoch, revision: installedRevision };
  }

  function discardSnapshotInstallFloor(replayBase: FocusCoordinates): FocusCoordinates {
    snapshotInstallFloor = null;
    runtimeEpoch.value = replayBase.runtime_epoch;
    revision.value = replayBase.revision;
    return replayBase;
  }

  function effectiveSnapshotEpoch(): string {
    return installingSnapshot && snapshotInstallFloor
      ? snapshotInstallFloor.runtime_epoch
      : runtimeEpoch.value;
  }

  function responseEpochWasSuperseded(response: FocusCoordinates, requestEpoch: string): boolean {
    const expectedEpoch = effectiveSnapshotEpoch();
    if (requestEpoch && expectedEpoch && requestEpoch !== expectedEpoch) return true;
    if (expectedEpoch && response.runtime_epoch !== expectedEpoch) {
      if (installingSnapshot) reloadAfterInstall = true;
      else options.transport.requestProjectionReload();
      return true;
    }
    return false;
  }

  function threadResponseWasSuperseded(
    response: FocusCoordinates,
    requestEpoch: string,
    threadId: string,
  ): boolean {
    if (responseEpochWasSuperseded(response, requestEpoch)) return true;
    return response.runtime_epoch === effectiveSnapshotEpoch()
      && (
        threadEventRevision.hasNewerEvent(threadId, response)
        || activeTurnDisclosureWasSuperseded(threadId, response)
      );
  }

  function mergeRefreshedThreadSnapshot(result: FocusThreadSnapshot): FocusThreadSnapshot {
    const goalFloor = goalResultFloors.get(result.thread.id);
    let protectedResult = result;
    if (goalFloor?.runtime_epoch === result.runtime_epoch) {
      if (goalFloor.revision > result.revision) {
        protectedResult = { ...result, goal: goalFloor.goal };
      } else {
        goalResultFloors.delete(result.thread.id);
      }
    }
    const current = snapshot.value;
    if (
      !current
      || current.thread.id !== protectedResult.thread.id
      || current.runtime_epoch !== protectedResult.runtime_epoch
    ) return {
      ...protectedResult,
      turns: boundProjectionTurns(protectedResult.turns, options.turnWindowLimit.value),
    };
    const previousById = new Map(current.turns.map((turn) => [turn.id, turn]));
    const refreshedPage = protectedResult.turns.map((turn) => {
      const previous = previousById.get(turn.id);
      return previous ? mergeLiveAssistantTurn(previous, turn) : turn;
    });
    return {
      ...protectedResult,
      turns: boundProjectionTurns(refreshedPage, options.turnWindowLimit.value),
    };
  }

  function mergeTurns(incoming: FocusThreadSnapshot['turns']): void {
    if (!snapshot.value || incoming.length === 0) return;
    snapshot.value = {
      ...snapshot.value,
      turns: boundProjectionTurns(
        mergeCanonicalRawTurnSegments(snapshot.value.turns, incoming),
        options.turnWindowLimit.value,
      ),
    };
  }

  function appendStreamDelta(detail: FocusThreadDeltaDetail): boolean {
    if (!snapshot.value || !detail.stream_delta) return false;
    const stream = detail.stream_delta;
    const nextTurns = [...snapshot.value.turns];
    let turnIndex = nextTurns.findIndex((turn) => (
      isAssistantSegmentForRawTurn(turn, stream.turn_id)
      && assistantSegmentWithItem(turn, stream.item_id)
    ));
    if (turnIndex < 0) turnIndex = insertLiveAssistantSegment(stream.turn_id, nextTurns);
    const current = nextTurns[turnIndex]!;
    const blocks = [...(current.blocks ?? [])];
    const tools = [...(current.tools ?? [])];

    if (stream.kind === 'text') {
      const index = blocks.findIndex((block) => (
        block.kind === 'text' && block.itemId === stream.item_id
      ));
      if (index < 0) blocks.push({ kind: 'text', itemId: stream.item_id, text: stream.delta });
      else {
        const block = blocks[index];
        if (block?.kind === 'text') blocks[index] = { ...block, text: `${block.text}${stream.delta}` };
      }
    } else if (stream.kind === 'thinking' || stream.kind === 'thinking_separator') {
      const index = blocks.findIndex((block) => (
        block.kind === 'thinking' && block.itemId === stream.item_id
      ));
      const addition = stream.kind === 'thinking_separator' ? '\n\n' : stream.delta;
      if (index < 0) blocks.push({ kind: 'thinking', itemId: stream.item_id, thinking: addition });
      else {
        const block = blocks[index];
        if (block?.kind === 'thinking') {
          blocks[index] = { ...block, thinking: `${block.thinking}${addition}` };
        }
      }
    } else if (stream.kind === 'tool_output' || stream.kind === 'plan') {
      let toolIndex = tools.findIndex((tool) => tool.id === stream.item_id);
      if (toolIndex < 0) {
        const toolName = stream.tool_name
          || (stream.kind === 'plan'
            ? 'Plan'
            : detail.method.includes('commandExecution')
              ? 'Shell'
              : detail.method.includes('fileChange') ? 'File change' : 'Tool');
        const tool: ToolCall = {
          id: stream.item_id,
          name: toolName,
          arg: '',
          status: 'running',
          output: [],
        };
        tools.push(tool);
        blocks.push({ kind: 'tool', tool });
        toolIndex = tools.length - 1;
      }
      const tool = tools[toolIndex]!;
      const boundedOutput = appendBoundedToolOutput(
        tool.output ?? [],
        stream.delta,
        tool.outputOmittedChars ?? 0,
        tool.outputHeadLineCount ?? 0,
      );
      const updated: ToolCall = {
        ...tool,
        output: boundedOutput.lines,
        ...(boundedOutput.omittedChars > 0
          ? {
              outputOmittedChars: boundedOutput.omittedChars,
              outputHeadLineCount: boundedOutput.headLineCount,
              outputTruncated: true,
            }
          : {}),
      };
      if (boundedOutput.omittedChars <= 0) {
        delete updated.outputOmittedChars;
        delete updated.outputHeadLineCount;
        delete updated.outputTruncated;
      }
      tools[toolIndex] = updated;
      const blockIndex = blocks.findIndex((block) => (
        block.kind === 'tool' && block.tool.id === stream.item_id
      ));
      if (blockIndex >= 0) blocks[blockIndex] = { kind: 'tool', tool: updated };
    }

    nextTurns[turnIndex] = {
      ...current,
      text: assistantTextFromBlocks(blocks),
      blocks,
      tools,
      status: 'inProgress',
    };
    const recentTurns = boundRecentFullTurns(nextTurns, options.turnWindowLimit.value);
    snapshot.value = {
      ...snapshot.value,
      turns: stream.kind === 'tool_output' || stream.kind === 'plan'
        ? boundToolOutputWindow(recentTurns)
        : recentTurns,
    };
    return true;
  }

  function threadSnapshotCoversEvent(event: FocusProjectionEvent): boolean {
    if (!event.thread_id) return false;
    const snapshotFloor = threadSnapshotFloors.get(event.thread_id);
    if (snapshotFloor?.runtime_epoch === event.runtime_epoch
      && event.revision < snapshotFloor.revision) {
      return true;
    }
    if (snapshotFloor) threadSnapshotFloors.delete(event.thread_id);
    return false;
  }

  function threadSnapshotCoversDisclosureChange(event: FocusProjectionEvent): boolean {
    if (!event.thread_id) return false;
    const snapshotFloor = threadSnapshotFloors.get(event.thread_id);
    if (snapshotFloor?.runtime_epoch === event.runtime_epoch
      && event.revision <= snapshotFloor.revision) {
      return true;
    }
    if (snapshotFloor) threadSnapshotFloors.delete(event.thread_id);
    return false;
  }

  function observeActiveTurnDisclosureChange(event: FocusProjectionEvent): void {
    const threadId = event.thread_id?.trim() ?? '';
    if (!threadId || threadSnapshotCoversDisclosureChange(event)) return;
    activeTurnDisclosureRevision.observe(threadId, event);
    const current = snapshot.value;
    if (current?.thread.id === threadId && current.active_turn_context !== null) {
      // This event proves that at least one field in the read-side disclosure
      // join may have changed. Hide the old join until a covering snapshot is
      // observed; presentation never becomes lifecycle or mutation authority.
      snapshot.value = { ...current, active_turn_context: null };
    }
  }

  function observeGlobalActiveTurnDisclosureChange(event: FocusProjectionEvent): void {
    if (!event.runtime_epoch) return;
    const current = globalActiveTurnDisclosureRevision;
    if (
      current?.runtime_epoch === event.runtime_epoch
      && current.revision >= event.revision
    ) return;
    globalActiveTurnDisclosureRevision = {
      runtime_epoch: event.runtime_epoch,
      revision: event.revision,
    };
    const currentSnapshot = snapshot.value;
    if (currentSnapshot?.active_turn_context) {
      snapshot.value = { ...currentSnapshot, active_turn_context: null };
    }
  }

  function requireAuthoritativeProjectionReload(event?: FocusProjectionEvent): void {
    if (event) observeGlobalActiveTurnDisclosureChange(event);
    else {
      const current = snapshot.value;
      if (current?.active_turn_context) {
        snapshot.value = { ...current, active_turn_context: null };
      }
    }
    // A read that started before the invalidation cannot become current again
    // merely because its HTTP response arrives before the composite reload.
    requests.begin('activeThread');
    snapshotInvalidated.value = true;
    if (installingSnapshot) reloadAfterInstall = true;
    else options.transport.requestProjectionReload();
  }

  function activeTurnDisclosureWasSuperseded(
    threadId: string,
    response: FocusCoordinates,
  ): boolean {
    return activeTurnDisclosureRevision.hasNewerEvent(threadId, response)
      || (
        globalActiveTurnDisclosureRevision?.runtime_epoch === response.runtime_epoch
        && globalActiveTurnDisclosureRevision.revision > response.revision
      );
  }

  function applyThreadDelta(event: FocusProjectionEvent, detail: FocusThreadDeltaDetail): boolean {
    if (!snapshot.value || event.thread_id !== options.navigation.activeThreadId.value) return false;
    if (threadSnapshotCoversEvent(event)) return true;
    const streamChanged = appendStreamDelta(detail);
    const incomingTurns = detail.turns ?? [];
    if (incomingTurns.length > 0) mergeTurns(incomingTurns);
    const statusChanged = detail.thread_status !== undefined;
    if (detail.thread_status) {
      snapshot.value = {
        ...snapshot.value,
        thread: {
          ...snapshot.value.thread,
          status: detail.thread_status.type,
          active_flags: detail.thread_status.activeFlags ?? snapshot.value.thread.active_flags,
        },
      };
    }
    const nameChanged = detail.thread_name !== undefined;
    if (detail.thread_name !== undefined) {
      snapshot.value = {
        ...snapshot.value,
        thread: {
          ...snapshot.value.thread,
          name: detail.thread_name,
          title: detail.thread_name || snapshot.value.thread.preview || snapshot.value.thread.id,
        },
      };
    }
    const activeTurnIdentityChanged = detail.active_turn_id !== undefined
      && snapshot.value.active_turn_id !== detail.active_turn_id;
    const activeTurnChanged = detail.active_turn_id !== undefined && (
      activeTurnIdentityChanged
      || (
        detail.active_turn_status !== undefined
        && snapshot.value.active_turn_status !== detail.active_turn_status
      )
    );
    if (detail.active_turn_id !== undefined) {
      snapshot.value = {
        ...snapshot.value,
        active_turn_id: detail.active_turn_id,
        active_turn_status: detail.active_turn_status ?? snapshot.value.active_turn_status,
        active_turn_context: snapshot.value.active_turn_context?.turn_id === detail.active_turn_id
          ? snapshot.value.active_turn_context
          : null,
      };
    }
    const goalChanged = Object.prototype.hasOwnProperty.call(detail, 'goal');
    if (goalChanged) {
      const commandFloor = goalResultFloors.get(event.thread_id ?? '');
      const eventPredatesCommand = commandFloor?.runtime_epoch === event.runtime_epoch
        && event.revision < commandFloor.revision;
      if (!eventPredatesCommand) {
        snapshot.value = { ...snapshot.value, goal: detail.goal ?? null };
        if (commandFloor?.runtime_epoch === event.runtime_epoch
          && event.revision >= commandFloor.revision) {
          goalResultFloors.delete(event.thread_id ?? '');
        }
      }
    }
    const tasksChanged = detail.tasks !== undefined;
    if (detail.tasks) snapshot.value = { ...snapshot.value, tasks: detail.tasks };
    const tokenUsageChanged = detail.token_usage !== undefined;
    if (detail.token_usage) {
      snapshot.value = {
        ...snapshot.value,
        token_usage: detail.token_usage,
        token_usage_available: true,
      };
    }
    return streamChanged
      || incomingTurns.length > 0
      || statusChanged
      || nameChanged
      || activeTurnChanged
      || goalChanged
      || tasksChanged
      || tokenUsageChanged;
  }

  async function refreshThreads(): Promise<boolean> {
    if (options.navigation.isDisposed) return false;
    const requestEpoch = effectiveSnapshotEpoch();
    const requestScope = options.navigation.threadScope.value;
    const generation = requests.begin('threadList');
    if (snapshotInvalidated.value) {
      if (installingSnapshot) reloadAfterInstall = true;
      else options.transport.requestProjectionReload();
      return false;
    }
    // A list read may lose its exact projection fence while the external
    // worker is producing the DTO.  Retry that read once under the same
    // request authority; never install the stale DTO or surface its internal
    // 409 to the browser.  A second stale result remains a bounded
    // non-convergence and leaves the last known directory intact.
    let staleRetryUsed = false;
    while (true) {
      try {
        const result = await awaitProjectionResult(
          options.api.listThreads({ scope: requestScope }),
        );
        if (result === disposedResult) return false;
        if (!requests.isCurrent('threadList', generation)) return false;
        if (options.navigation.threadScope.value !== requestScope) return false;
        if (responseEpochWasSuperseded(result, requestEpoch)) return false;
        threads.value = result.threads;
        applySnapshotCoordinates(result);
        return true;
      } catch (error) {
        if (!isStaleWebReadError(error)) throw error;
        if (staleRetryUsed) {
          if (!requests.isCurrent('threadList', generation)) return false;
          if (options.navigation.threadScope.value !== requestScope) return false;
          options.transport.scheduleThreadListRefresh();
          return false;
        }
        staleRetryUsed = true;
        if (!requests.isCurrent('threadList', generation)) return false;
        if (options.navigation.threadScope.value !== requestScope) return false;
      }
    }
  }

  async function refreshArchivedThreads(): Promise<void> {
    if (options.navigation.isDisposed) return;
    const generation = requests.begin('archivedList');
    archivedLoading.value = true;
    try {
      const requestEpoch = runtimeEpoch.value;
      const result = await awaitProjectionResult(
        options.api.listThreads({ scope: 'global', archived: true }));
      if (result === disposedResult) return;
      if (!requests.isCurrent('archivedList', generation)) return;
      if (responseEpochWasSuperseded(result, requestEpoch)) return;
      archivedThreads.value = result.threads;
      archivedTruncated.value = result.truncated;
      archivedLimit.value = result.limit;
      applySnapshotCoordinates(result);
    } catch (error) {
      if (!isStaleWebReadError(error)) throw error;
    } finally {
      if (!options.navigation.isDisposed && requests.isCurrent('archivedList', generation)) archivedLoading.value = false;
    }
  }

  async function refreshActiveThread(
    refreshOptions: RefreshActiveThreadOptions = {},
  ): Promise<boolean> {
    if (options.navigation.isDisposed) return false;
    flushPendingStreamEvents();
    const threadId = options.navigation.activeThreadId.value;
    if (!threadId) {
      snapshot.value = null;
      return true;
    }
    const requestEpoch = effectiveSnapshotEpoch();
    const generation = requests.begin('activeThread');
    if (snapshotInvalidated.value) {
      if (installingSnapshot) reloadAfterInstall = true;
      else options.transport.requestProjectionReload();
      return false;
    }
    const requestIntent = refreshOptions.requestIntentGeneration ?? options.intentClock.currentIntent;
    const unknownMutationReceipt = options.captureUnknownMutationSnapshot(threadId);
    // A thread snapshot may be replaced by a newer notification/read while
    // the external worker is producing it.  Retry one exact target/read
    // receipt after applying any queued deltas.  The stale DTO is never
    // installed; a repeated stale result stays on the current target and
    // lets the existing projection refresh/reload path converge.
    let staleRetryUsed = false;
    let result: FocusThreadSnapshot | typeof disposedResult;
    while (true) {
      try {
        result = await awaitProjectionResult(readThreadWindow(threadId, requestIntent));
        break;
      } catch (error) {
        if (!isStaleWebReadError(error)) throw error;
        if (staleRetryUsed) {
          if (!requests.isCurrent('activeThread', generation)) return false;
          if (options.navigation.activeThreadId.value !== threadId) return false;
          options.transport.scheduleProjectionRefresh();
          if (refreshOptions.navigationGeneration !== undefined) throw error;
          return false;
        }
        staleRetryUsed = true;
        // Deltas received while the stale read was in flight establish their
        // revision floors before the authoritative retry is issued.
        flushPendingStreamEvents();
        if (!requests.isCurrent('activeThread', generation)) return false;
        if (options.navigation.activeThreadId.value !== threadId) return false;
        if (snapshotInvalidated.value) {
          options.transport.requestProjectionReload();
          if (refreshOptions.navigationGeneration !== undefined) throw error;
          return false;
        }
      }
    }
    if (result === disposedResult) return false;
    // Deltas received while this read was in flight must establish their
    // revision floors before the snapshot is judged or installed.
    flushPendingStreamEvents();
    if (responseEpochWasSuperseded(result, requestEpoch)) return false;
    if (!requests.isCurrent('activeThread', generation)) return false;
    if (options.navigation.activeThreadId.value !== threadId) return false;
    if (result.thread.id !== threadId) {
      throw new Error(
        `Focus Web received snapshot ${result.thread.id} for requested thread ${threadId}.`,
      );
    }
    const responseEpochIsCurrent = result.runtime_epoch === effectiveSnapshotEpoch();
    const newerThreadEventExists = responseEpochIsCurrent
      && threadEventRevision.hasNewerEvent(threadId, result);
    const disclosureWasSuperseded = responseEpochIsCurrent
      && activeTurnDisclosureWasSuperseded(threadId, result);
    if (newerThreadEventExists || disclosureWasSuperseded) {
      const current = snapshot.value;
      const context = result.active_turn_context;
      if (
        newerThreadEventExists
        && current?.thread.id === threadId
        && current.active_turn_id
        && result.active_turn_id === current.active_turn_id
        && context?.turn_id === current.active_turn_id
        && !disclosureWasSuperseded
      ) {
        // Newer stream/status state keeps authority over the snapshot. The
        // matching disclosure is a read-only join for the same exact turn, so
        // install only that field and never roll back coordinates or turns.
        snapshot.value = { ...current, active_turn_context: context };
      }
      return false;
    }
    // Snapshot and echoed profile settle through one exact current receipt.
    const scopeReceipt = options.navigation.installObservedProfile(
      result.selection_scope.writer_profile,
      {
        navigationGeneration: refreshOptions.navigationGeneration,
        navigationAuthorityGeneration: refreshOptions.navigationAuthorityGeneration,
        expectedThreadId: threadId,
      },
    );
    if (!scopeReceipt) return false;
    snapshot.value = mergeRefreshedThreadSnapshot(result);
    threadSnapshotFloors.set(threadId, {
      runtime_epoch: result.runtime_epoch,
      revision: result.revision,
    });
    options.reconcileUnknownMutation(unknownMutationReceipt, result.mutation_unknown);
    const currentIndex = threads.value.findIndex((thread) => thread.id === result.thread.id);
    threads.value = currentIndex === -1
      ? [result.thread, ...threads.value]
      : threads.value.map((thread, index) => (index === currentIndex ? result.thread : thread));
    applySnapshotCoordinates(result);
    return true;
  }

  async function loadAllSessionsForSearch(): Promise<void> {
    if (options.navigation.isDisposed) return;
    try {
      const requestEpoch = runtimeEpoch.value;
      const generation = requests.begin('searchList');
      const result = await awaitProjectionResult(
        options.api.listThreads({ scope: 'global', allForSearch: true }));
      if (result === disposedResult) return;
      if (!requests.isCurrent('searchList', generation)) return;
      if (responseEpochWasSuperseded(result, requestEpoch)) return;
      searchThreads.value = result.threads;
      applySnapshotCoordinates(result);
    } catch (error) {
      if (!options.navigation.isDisposed && !isStaleWebReadError(error)) {
        options.reportError(error);
      }
    }
  }

  function replayBufferedEvents(queued: FocusProjectionEvent[], replayBase: FocusCoordinates): void {
    if (queued.length === 0) return;
    const installedEpoch = runtimeEpoch.value;
    let expectedRevision: number | null = replayBase.runtime_epoch === installedEpoch
      ? replayBase.revision
      : null;
    for (const event of queued) {
      if (event.type === 'session_expired') {
        processEvent(event);
        return;
      }
      if (event.runtime_epoch !== installedEpoch) {
        options.transport.requestProjectionReload();
        return;
      }
      if (event.type === 'hello') {
        if (expectedRevision !== null && event.revision > expectedRevision) {
          options.transport.requestProjectionReload();
          return;
        }
        continue;
      }
      if (expectedRevision !== null) {
        if (event.revision <= expectedRevision) continue;
        if (event.revision !== expectedRevision + 1) {
          options.transport.requestProjectionReload();
          return;
        }
      }
      expectedRevision = event.revision;
      processEvent(event, true);
      if (installingSnapshot) return;
    }
  }

  function processEvent(event: FocusProjectionEvent, replayingBufferedEvent = false): void {
    if (event.type === 'session_expired') {
      options.requireAuthentication();
      return;
    }
    const epochChanged = !!runtimeEpoch.value && event.runtime_epoch !== runtimeEpoch.value;
    if (event.type === 'hello') {
      const coordinatesChanged = epochChanged || event.revision !== revision.value;
      if (coordinatesChanged) requireAuthoritativeProjectionReload(event);
      else if (snapshotInvalidated.value) options.transport.requestProjectionReload();
      return;
    }
    if (event.type === 'profile_changed') {
      options.navigation.requireNavigationRepair();
    }
    if (event.type === 'projection_invalidated' || event.type === 'profile_changed') {
      if (event.type === 'projection_invalidated') {
        requireAuthoritativeProjectionReload(event);
      } else {
        options.transport.requestProjectionReload();
      }
      return;
    }
    if (snapshotInvalidated.value && !replayingBufferedEvent) {
      options.transport.requestProjectionReload();
      return;
    }
    if (
      !replayingBufferedEvent
      && !epochChanged
      && !!runtimeEpoch.value
      && event.revision <= revision.value
    ) return;
    const revisionGap = !replayingBufferedEvent
      && !epochChanged
      && !!runtimeEpoch.value
      && event.revision > revision.value + 1;
    if ((!replayingBufferedEvent && epochChanged) || revisionGap) {
      requireAuthoritativeProjectionReload(event);
      return;
    }
    applyCoordinates(event);
    if (event.type === 'runtime_notice') return;
    if (event.type === 'settings_changed') {
      void options.settings.refresh();
      return;
    }
    if (event.type === 'backend_disconnected') {
      observeGlobalActiveTurnDisclosureChange(event);
    }
    if (event.type === 'mutation_reconciled' && event.thread_id) {
      options.settleUnknownMutationFromEvent(
        event.thread_id,
        String(event.detail?.mutation_id ?? ''),
        String(event.detail?.operation ?? ''),
        event.detail?.disposition as FocusMutationDisposition,
      );
    }
    if (event.thread_id && event.runtime_epoch) threadEventRevision.observe(event.thread_id, event);
    const reason = event.reason?.trim() ?? '';
    if (
      event.type === 'owner_changed'
      || event.type === 'thread_invalidated'
    ) observeActiveTurnDisclosureChange(event);
    if (event.type === 'thread_invalidated' && AUTHORITATIVE_LIFECYCLE_REASONS.has(reason)) {
      if (
        event.thread_id === options.navigation.activeThreadId.value
        && THREAD_REMOVAL_REASONS.has(reason)
        && !threadSnapshotCoversEvent(event)
      ) {
        snapshot.value = null;
        options.activeThreadWasRemoved();
      }
      void Promise.all([refreshThreads(), refreshArchivedThreads()]).catch(options.reportError);
      return;
    }
    const targetsAnotherThread = !!event.thread_id
      && event.thread_id !== options.navigation.activeThreadId.value;
    if (event.type === 'thread_delta') {
      const detail = decodeFocusThreadDeltaDetail(event.detail);
      if (!detail) {
        invalidateWireProjection();
        return;
      }
      const currentActiveTurnId = event.thread_id === options.navigation.activeThreadId.value
        ? snapshot.value?.active_turn_id ?? ''
        : null;
      const disclosureChanged = threadDeltaChangesActiveTurnDisclosure(
        detail,
        currentActiveTurnId,
      );
      if (disclosureChanged) observeActiveTurnDisclosureChange(event);
      if (targetsAnotherThread) {
        if (THREAD_LIST_DELTA_METHODS.has(detail.method)) {
          options.transport.scheduleThreadListRefresh();
        }
        return;
      }
      if (!applyThreadDelta(event, detail)) {
        if (!disclosureChanged) observeActiveTurnDisclosureChange(event);
        options.transport.scheduleProjectionRefresh();
        return;
      }
      if (disclosureChanged && snapshot.value?.active_turn_id) {
        // The delta carries lifecycle coordinates but not the initiator,
        // audience, or setting provenance join for the still-active turn.
        options.transport.scheduleProjectionRefresh();
      }
      if (
        detail.method === 'thread/status/changed'
        || detail.method === 'thread/name/updated'
        || detail.method === 'turn/completed'
      ) options.transport.scheduleThreadListRefresh();
      return;
    }
    if (targetsAnotherThread) {
      if (THREAD_LIST_EVENT_TYPES.has(event.type)) options.transport.scheduleThreadListRefresh();
      return;
    }
    options.transport.scheduleProjectionRefresh();
  }

  function handleEvent(event: FocusProjectionEvent): void {
    if (options.navigation.isDisposed) return;
    if (installingSnapshot && event.type !== 'session_expired') {
      flushPendingStreamEvents();
      bufferedEvents.push(event);
      return;
    }
    if (isStreamPresentationEvent(event)) {
      pendingStreamEvents.push(event);
      schedulePendingStreamFlush();
      return;
    }
    flushPendingStreamEvents();
    processEvent(event);
  }

  function invalidateWireProjection(): void {
    if (options.navigation.isDisposed) return;
    flushPendingStreamEvents();
    requireAuthoritativeProjectionReload();
  }

  function scheduleProjectionRefresh(): void {
    if (options.navigation.isDisposed) return;
    options.transport.scheduleProjectionRefresh();
  }

  async function reloadAll(): Promise<void> {
    if (options.navigation.isDisposed) return;
    flushPendingStreamEvents();
    if (reloadPromise) return reloadPromise;
    if (!snapshotInvalidated.value) options.transport.resetProjectionReloadBackoff();
    options.transport.cancelProjectionReloadRetry();
    snapshotInvalidated.value = true;
    const reloadThreadScope = options.navigation.threadScope.value;
    const repairNavigation = options.navigation.navigationRepairIsRequired;
    const repairNavigationFloor = options.navigation.captureNavigationStateFloor();
    const replayBase = { runtime_epoch: runtimeEpoch.value, revision: revision.value };
    reloadInFlight.value = true;
    reloadPromise = (async () => {
      installingSnapshot = true;
      snapshotInstallFloor = null;
      let installSucceeded = false;
      let stagedMeta: FocusMetaEnvelope | null = null;
      let stagedWriterProfile: FocusMeta['writer_profile'] | null = null;
      let stagedThreads: FocusThreadSummary[] | null = null;
      let stagedSnapshot: FocusThreadSnapshot | null = null;
      let stagedActiveThreadId: string | null = null;
      let stagedUnknownMutationReceipt: UnknownMutationSnapshotReceipt | null = null;
      try {
        const nextMeta = await awaitProjectionResult(options.api.meta());
        if (nextMeta === disposedResult) return;
        options.settings.installRuntimeSnapshot(
          nextMeta.runtime_epoch,
          nextMeta.next_turn_settings,
        );
        applySnapshotCoordinates(nextMeta);
        const listEpoch = effectiveSnapshotEpoch();
        const listGeneration = requests.begin('threadList');
        const nextThreadList = await awaitProjectionResult(
          options.api.listThreads({ scope: reloadThreadScope }),
        );
        if (nextThreadList === disposedResult) return;
        if (
          !requests.isCurrent('threadList', listGeneration)
          || options.navigation.threadScope.value !== reloadThreadScope
        ) {
          reloadAfterInstall = true;
          return;
        }
        if (responseEpochWasSuperseded(nextThreadList, listEpoch)) return;
        applySnapshotCoordinates(nextThreadList);

        const targetThreadId = repairNavigation
          ? nextMeta.writer_profile.selected_thread_id.trim()
          : options.navigation.activeThreadId.value;
        let nextThreads = nextThreadList.threads;
        let nextSnapshot: FocusThreadSnapshot | null = null;
        if (targetThreadId) {
          // readThread also selects on the server. Never issue stale-meta A
          // over pending B without the exact repair authority.
          const repairOwnsActiveChannel = !repairNavigation
            || options.navigation.navigationRepairIsCurrent(repairNavigationFloor);
          if (!repairOwnsActiveChannel) {
            reloadAfterInstall = true;
            return;
          }
          const threadEpoch = effectiveSnapshotEpoch();
          const threadGeneration = requests.begin('activeThread');
          const unknownMutationReceipt = options.captureUnknownMutationSnapshot(targetThreadId);
          const nextThread = await awaitProjectionResult(
            readThreadWindow(targetThreadId, options.intentClock.currentIntent),
          );
          if (nextThread === disposedResult) return;
          if (
            !requests.isCurrent('activeThread', threadGeneration)
            || (!repairNavigation && options.navigation.activeThreadId.value !== targetThreadId)
            || options.navigation.threadScope.value !== reloadThreadScope
          ) {
            reloadAfterInstall = true;
            return;
          }
          if (threadResponseWasSuperseded(nextThread, threadEpoch, targetThreadId)) {
            reloadAfterInstall = true;
            return;
          }
          if (nextThread.thread.id !== targetThreadId) {
            throw new Error(
              `Focus Web received snapshot ${nextThread.thread.id} for requested thread ${targetThreadId}.`,
            );
          }
          nextSnapshot = mergeRefreshedThreadSnapshot(nextThread);
          stagedUnknownMutationReceipt = unknownMutationReceipt;
          stagedWriterProfile = nextThread.selection_scope.writer_profile;
          const currentIndex = nextThreads.findIndex((thread) => thread.id === targetThreadId);
          nextThreads = currentIndex === -1
            ? [nextThread.thread, ...nextThreads]
            : nextThreads.map((thread, index) => (
                index === currentIndex ? nextThread.thread : thread
              ));
          applySnapshotCoordinates(nextThread);
        }
        if (
          (!repairNavigation && options.navigation.activeThreadId.value !== targetThreadId)
          || (
            repairNavigation
            && !options.navigation.navigationRepairIsCurrent(repairNavigationFloor)
          )
          || options.navigation.threadScope.value !== reloadThreadScope
        ) {
          reloadAfterInstall = true;
          return;
        }
        stagedMeta = metaEnvelope(nextMeta);
        stagedWriterProfile ??= nextMeta.writer_profile;
        stagedThreads = nextThreads;
        stagedSnapshot = nextSnapshot;
        stagedActiveThreadId = targetThreadId;
        installSucceeded = snapshotInstallFloor !== null && !reloadAfterInstall;
      } catch (error) {
        if (options.navigation.isDisposed) return;
        // A stale thread list/read is an expected freshness miss during a
        // staged composite reload.  Keep the install fail-closed and let the
        // existing bounded reload retry converge; do not turn the internal
        // 409 into the top-level browser error state.
        if (
          !isStaleWebReadError(error)
        ) {
          options.reportError(error);
          if (options.projectionAccessIsAvailable()) {
            snapshotReloadErrorMessage = options.currentErrorMessage();
          }
        }
      } finally {
        installingSnapshot = false;
        reloadPromise = null;
        reloadInFlight.value = false;
        if (options.navigation.isDisposed) {
          bufferedEvents = [];
          reloadAfterInstall = false;
          snapshotInstallFloor = null;
          return;
        }
        const queued = bufferedEvents;
        bufferedEvents = [];
        let installedBase: FocusCoordinates;
        if (
          installSucceeded
          && stagedMeta
          && stagedWriterProfile
          && stagedThreads
          && stagedActiveThreadId !== null
        ) {
          const installedScope = options.navigation.installObservedProfile(
            stagedWriterProfile,
            {
              expectedThreadId: stagedActiveThreadId,
              freshAuthorityFloor: repairNavigationFloor,
            },
          );
          if (!installedScope) installSucceeded = false;
        }
        if (
          installSucceeded
          && stagedMeta
          && stagedThreads
          && stagedActiveThreadId !== null
        ) {
          meta.value = stagedMeta;
          threads.value = stagedThreads;
          snapshot.value = stagedSnapshot;
          if (stagedActiveThreadId && stagedSnapshot) {
            threadSnapshotFloors.set(stagedActiveThreadId, {
              runtime_epoch: stagedSnapshot.runtime_epoch,
              revision: stagedSnapshot.revision,
            });
          }
          if (stagedActiveThreadId && stagedSnapshot && stagedUnknownMutationReceipt) {
            options.reconcileUnknownMutation(
              stagedUnknownMutationReceipt,
              stagedSnapshot.mutation_unknown,
            );
          }
          installedBase = commitSnapshotInstallFloor(replayBase);
          snapshotInvalidated.value = false;
          options.transport.resetProjectionReloadBackoff();
          options.clearErrorMessageIf(snapshotReloadErrorMessage);
          snapshotReloadErrorMessage = '';
        } else {
          installedBase = discardSnapshotInstallFloor(replayBase);
          snapshotInvalidated.value = true;
        }
        replayBufferedEvents(queued, installedBase);
        if (reloadAfterInstall) {
          reloadAfterInstall = false;
          if (installSucceeded) options.transport.requestProjectionReload();
          else options.transport.scheduleProjectionReloadRetry();
        } else if (!installSucceeded) {
          options.transport.scheduleProjectionReloadRetry();
        }
      }
    })();
    return reloadPromise;
  }

  function installInitialMeta(value: FocusMeta): void {
    if (options.navigation.isDisposed) return;
    options.settings.installRuntimeSnapshot(value.runtime_epoch, value.next_turn_settings);
    if (!options.navigation.installInitialProfile(value.writer_profile)) return;
    meta.value = metaEnvelope(value);
    applySnapshotCoordinates(value);
  }

  function settleDeletedThread(threadId: string, clearActiveProjection: boolean): void {
    if (options.navigation.isDisposed) return;
    flushPendingStreamEvents();
    archivedThreads.value = archivedThreads.value.filter((thread) => thread.id !== threadId);
    threads.value = threads.value.filter((thread) => thread.id !== threadId);
    searchThreads.value = searchThreads.value.filter((thread) => thread.id !== threadId);
    if (clearActiveProjection && snapshot.value?.thread.id === threadId) snapshot.value = null;
  }

  function installGoalResult(threadId: string, result: FocusGoalResult): void {
    if (options.navigation.isDisposed) return;
    flushPendingStreamEvents();
    const currentEpoch = effectiveSnapshotEpoch();
    if (result.thread_id !== threadId) return;
    if (currentEpoch && result.runtime_epoch !== currentEpoch) {
      if (installingSnapshot) reloadAfterInstall = true;
      else options.transport.requestProjectionReload();
      return;
    }
    if (
      result.runtime_epoch === currentEpoch
      && threadEventRevision.hasNewerEvent(threadId, result)
    ) return;
    const snapshotFloor = threadSnapshotFloors.get(threadId);
    if (snapshotFloor?.runtime_epoch === result.runtime_epoch
      && snapshotFloor.revision > result.revision) return;
    const currentFloor = goalResultFloors.get(threadId);
    if (currentFloor?.runtime_epoch === result.runtime_epoch
      && currentFloor.revision > result.revision) return;
    if (!currentFloor
      || currentFloor.runtime_epoch !== result.runtime_epoch
      || currentFloor.revision <= result.revision) {
      goalResultFloors.set(threadId, { ...result });
    }
    if (snapshot.value?.thread.id === threadId) {
      snapshot.value = { ...snapshot.value, goal: result.goal };
    }
    applySnapshotCoordinates(result);
  }

  function applyTurnWindowLimit(): void {
    if (options.navigation.isDisposed) return;
    flushPendingStreamEvents();
    // Retire an in-flight snapshot created under the previous request width.
    requests.begin('activeThread');
    if (!snapshot.value) return;
    snapshot.value = {
      ...snapshot.value,
      turns: boundProjectionTurns(
        snapshot.value.turns,
        options.turnWindowLimit.value,
      ),
    };
  }

  return {
    meta,
    threads,
    searchThreads,
    archivedThreads,
    snapshot,
    runtimeEpoch,
    revision,
    snapshotInvalidated,
    archivedLoading,
    archivedTruncated,
    archivedLimit,
    reloadInFlight,
    installInitialMeta,
    clearSnapshot() {
      if (options.navigation.isDisposed) return;
      flushPendingStreamEvents();
      snapshot.value = null;
    },
    settleDeletedThread,
    installGoalResult,
    refreshThreads,
    refreshArchivedThreads,
    refreshActiveThread,
    scheduleProjectionRefresh,
    loadAllSessionsForSearch,
    reloadAll,
    handleEvent,
    dispose,
    invalidateWireProjection,
    applyTurnWindowLimit,
    mayRetryProjectionReload() {
      return !options.navigation.isDisposed
        && reloadPromise === null
        && snapshotInvalidated.value
        && options.projectionAccessIsAvailable();
    },
  };
}
