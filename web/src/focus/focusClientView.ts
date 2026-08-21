import { computed } from 'vue';
import type { Ref } from 'vue';
import type {
  AppGoal,
  AppModel,
  ApprovalBlock,
  ConversationStatus,
  Session,
  SessionActionCapabilities,
  UIQuestion,
  WorkspaceGroup,
  WorkspaceView,
} from '../types';
import { pendingRequestActionToken } from './pendingRequestCapability';
import type {
  FocusGoal,
  FocusMeta,
  FocusPendingRequest,
  FocusThreadScope,
  FocusThreadSnapshot,
  FocusThreadSummary,
} from './types';
import { projectFocusContextUsage } from './contextUsage';

export const AUTO_MODEL_ID = 'focus:auto';

export function projectFocusGoal(goal: FocusGoal | null): AppGoal | null {
  if (!goal) return null;
  return {
    goalId: goal.goal_id,
    objective: goal.objective,
    status: goal.status,
    turnsUsed: 0,
    tokensUsed: goal.tokens_used,
    wallClockMs: goal.wall_clock_ms,
    budget: {
      tokenBudget: goal.budget.token_budget,
      remainingTokens: goal.budget.remaining_tokens,
      turnBudget: goal.budget.turn_budget,
      remainingTurns: goal.budget.remaining_turns,
      wallClockBudgetMs: goal.budget.wall_clock_budget_ms,
      remainingWallClockMs: goal.budget.remaining_wall_clock_ms,
      overBudget: goal.budget.over_budget,
    },
  };
}

export interface FocusClientViewOptions {
  meta: Readonly<Ref<FocusMeta | null>>;
  threads: Readonly<Ref<FocusThreadSummary[]>>;
  searchThreads: Readonly<Ref<FocusThreadSummary[]>>;
  snapshot: Readonly<Ref<FocusThreadSnapshot | null>>;
  snapshotInvalidated: Readonly<Ref<boolean>>;
  connection: Readonly<Ref<string>>;
  threadScope: Readonly<Ref<FocusThreadScope>>;
  activeThreadId: Readonly<Ref<string>>;
  draftWorkspaceId: Readonly<Ref<string>>;
  scopeReady: Readonly<Ref<boolean>>;
  settingsModel: Readonly<Ref<string>>;
}

function formatRelativeTime(timestampSeconds: number): string {
  if (!timestampSeconds) return '';
  const seconds = Math.max(0, Math.floor(Date.now() / 1000) - timestampSeconds);
  if (seconds < 60) return 'now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  if (seconds < 86400 * 30) return `${Math.floor(seconds / 86400)}d`;
  return new Date(timestampSeconds * 1000).toLocaleDateString();
}

function workspaceName(path: string): string {
  const normalized = path.replaceAll('\\', '/').replace(/\/+$/, '');
  return normalized.split('/').filter(Boolean).at(-1) ?? path;
}

function unavailableCapabilities(): SessionActionCapabilities {
  return {
    rename: false,
    archive: false,
    fork: false,
    export: false,
    review: false,
    goal: false,
  };
}

function approvalBlock(request: FocusPendingRequest): ApprovalBlock {
  const params = request.params;
  if (request.method === 'item/commandExecution/requestApproval') {
    const command = String(params.command ?? '');
    const context = [
      params.networkApprovalContext ? `Network: ${JSON.stringify(params.networkApprovalContext, null, 2)}` : '',
      params.additionalPermissions ? `Additional permissions: ${JSON.stringify(params.additionalPermissions, null, 2)}` : '',
      params.commandActions ? `Actions: ${JSON.stringify(params.commandActions, null, 2)}` : '',
      params.proposedExecpolicyAmendment
        ? `Proposed command policy: ${JSON.stringify(params.proposedExecpolicyAmendment, null, 2)}`
        : '',
      params.proposedNetworkPolicyAmendments
        ? `Proposed network policy: ${JSON.stringify(params.proposedNetworkPolicyAmendments, null, 2)}`
        : '',
    ].filter(Boolean).join('\n\n');
    if (!command) {
      return {
        kind: 'generic',
        summary: [String(params.reason ?? request.title), context].filter(Boolean).join('\n\n'),
      };
    }
    return {
      kind: 'shell',
      command,
      cwd: String(params.cwd ?? ''),
      danger: [String(params.reason ?? ''), context].filter(Boolean).join('\n\n'),
    };
  }
  if (request.method === 'item/fileChange/requestApproval') {
    return {
      kind: 'fileop',
      op: 'change',
      path: String(params.grantRoot ?? ''),
      detail: String(params.reason ?? ''),
    };
  }
  if (request.method === 'item/permissions/requestApproval') {
    return {
      kind: 'generic',
      summary: `${String(params.reason ?? request.title)}\n\n${JSON.stringify(params.permissions ?? {}, null, 2)}`,
    };
  }
  return { kind: 'generic', summary: request.title };
}

function questionOptions(raw: unknown): UIQuestion['questions'][number]['options'] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value, index) => {
    if (typeof value === 'string') return [{ id: value, label: value }];
    if (!value || typeof value !== 'object') return [];
    const item = value as Record<string, unknown>;
    const label = String(item.label ?? item.value ?? item.id ?? `Option ${index + 1}`);
    return [{
      id: String(item.value ?? item.id ?? label),
      label,
      description: String(item.description ?? '') || undefined,
      recommended: item.recommended === true,
    }];
  });
}

function uiQuestion(request: FocusPendingRequest): UIQuestion {
  const questionId = pendingRequestActionToken(request);
  if (request.kind === 'elicitation') {
    const schema = request.params.requestedSchema;
    const properties = schema && typeof schema === 'object' && !Array.isArray(schema)
      ? (schema as Record<string, unknown>).properties
      : null;
    const required = new Set(
      schema && typeof schema === 'object' && !Array.isArray(schema)
        && Array.isArray((schema as Record<string, unknown>).required)
        ? ((schema as Record<string, unknown>).required as unknown[]).map(String)
        : [],
    );
    const fields = properties && typeof properties === 'object' && !Array.isArray(properties)
      ? Object.entries(properties as Record<string, unknown>).flatMap(([id, rawField]) => {
          if (!rawField || typeof rawField !== 'object' || Array.isArray(rawField)) return [];
          const field = rawField as Record<string, unknown>;
          const type = String(field.type ?? 'string');
          const enumValues = Array.isArray(field.enum)
            ? field.enum.map((value, index) => ({
                id: String(value),
                label: Array.isArray(field.enumNames)
                  ? String(field.enumNames[index] ?? value)
                  : String(value),
              }))
            : Array.isArray(field.oneOf)
              ? field.oneOf.flatMap((option) => {
                  if (!option || typeof option !== 'object') return [];
                  const value = option as Record<string, unknown>;
                  return [{
                    id: String(value.const ?? ''),
                    label: String(value.title ?? value.const ?? ''),
                  }];
                })
              : [];
          const arrayItems = type === 'array' && field.items && typeof field.items === 'object'
            ? field.items as Record<string, unknown>
            : null;
          const arrayOptions = arrayItems && Array.isArray(arrayItems.enum)
            ? arrayItems.enum.map((value, index) => ({
                id: String(value),
                label: Array.isArray(arrayItems.enumNames)
                  ? String(arrayItems.enumNames[index] ?? value)
                  : String(value),
              }))
            : arrayItems && Array.isArray(arrayItems.anyOf ?? arrayItems.oneOf)
              ? ((arrayItems.anyOf ?? arrayItems.oneOf) as unknown[]).flatMap((option) => {
                  if (!option || typeof option !== 'object') return [];
                  const value = option as Record<string, unknown>;
                  return [{ id: String(value.const ?? ''), label: String(value.title ?? value.const ?? '') }];
                })
              : [];
          const options = type === 'boolean'
            ? [{ id: 'true', label: 'True' }, { id: 'false', label: 'False' }]
            : type === 'array' ? arrayOptions : enumValues;
          return [{
            id,
            question: String(field.title ?? id),
            body: String(field.description ?? '') || undefined,
            options,
            multiSelect: type === 'array',
            allowOther: options.length === 0,
            otherLabel: String(field.description ?? '') || id,
            secret: String(field.format ?? '') === 'password',
            required: required.has(id),
          }];
        })
      : [];
    return { questionId, sessionId: request.thread_id, questions: fields };
  }
  const rawQuestions = Array.isArray(request.params.questions) ? request.params.questions : [];
  const questions = rawQuestions.flatMap((value, index) => {
    if (!value || typeof value !== 'object') return [];
    const item = value as Record<string, unknown>;
    const id = String(item.id ?? `question-${index + 1}`);
    return [{
      id,
      question: String(item.question ?? item.prompt ?? item.header ?? request.title),
      header: String(item.header ?? '') || undefined,
      body: String(item.body ?? '') || undefined,
      options: questionOptions(item.options),
      multiSelect: item.multiSelect === true || item.multi_select === true,
      allowOther: (Array.isArray(item.options) && item.options.length === 0)
        || item.isOther === true
        || item.is_other === true,
      otherLabel: String(item.otherLabel ?? item.other_label ?? '') || undefined,
      secret: item.isSecret === true || item.is_secret === true,
      required: true,
    }];
  });
  return {
    questionId,
    sessionId: request.thread_id,
    autoResolutionMs: typeof request.params.autoResolutionMs === 'number'
      ? request.params.autoResolutionMs
      : undefined,
    autoResolutionVisibleAtMs: typeof request.params.autoResolutionVisibleAtMs === 'number'
      ? request.params.autoResolutionVisibleAtMs
      : undefined,
    autoResolutionDueAtMs: typeof request.params.autoResolutionDueAtMs === 'number'
      ? request.params.autoResolutionDueAtMs
      : undefined,
    questions: questions.length > 0 ? questions : [{
      id: 'answer',
      question: request.title,
      options: [],
      allowOther: true,
    }],
  };
}

export function createFocusClientView(options: FocusClientViewOptions) {
  const reasoningEffortOptions = computed(() => {
    const values: string[] = [];
    for (const model of options.meta.value?.models ?? []) {
      for (const effort of model.supported_reasoning_efforts) {
        if (effort.effort && !values.includes(effort.effort)) values.push(effort.effort);
      }
    }
    const current = options.meta.value?.next_turn_settings.reasoning_effort.trim() ?? '';
    if (current && !values.includes(current)) values.push(current);
    return values;
  });
  const models = computed<AppModel[]>(() => {
    const meta = options.meta.value;
    const runtimeModels: AppModel[] = (meta?.models ?? []).map((model) => ({
      id: model.id,
      provider: 'Codex',
      model: model.model,
      displayName: model.display_name,
      maxContextSize: 0,
      capabilities: model.supported_reasoning_efforts.length ? ['always_thinking'] : [],
      supportEfforts: model.supported_reasoning_efforts.map((effort) => effort.effort),
      defaultEffort: model.default_reasoning_effort || undefined,
    }));
    return [{
      id: AUTO_MODEL_ID,
      provider: 'Codex',
      model: '',
      displayName: 'Auto',
      maxContextSize: 0,
      capabilities: reasoningEffortOptions.value.length ? ['always_thinking'] : [],
      supportEfforts: reasoningEffortOptions.value,
    }, ...runtimeModels];
  });
  const selectedModelId = computed(() => {
    const requested = options.settingsModel.value;
    return requested || AUTO_MODEL_ID;
  });
  const selectedModel = computed(() => (
    models.value.find((model) => model.id === selectedModelId.value)
      ?? {
        id: selectedModelId.value,
        provider: 'Codex',
        model: selectedModelId.value,
        displayName: selectedModelId.value,
        maxContextSize: 0,
        capabilities: [],
        supportEfforts: [],
      }
  ));
  const workspacesView = computed<WorkspaceView[]>(() => {
    const byRoot = new Map<string, FocusThreadSummary[]>();
    for (const thread of options.threads.value) {
      const root = thread.cwd || options.meta.value?.default_working_dir || '';
      if (root) byRoot.set(root, [...(byRoot.get(root) ?? []), thread]);
    }
    const defaultRoot = options.meta.value?.default_working_dir;
    if (byRoot.size === 0 && defaultRoot) byRoot.set(defaultRoot, []);
    return [...byRoot.entries()]
      .sort((left, right) => (
        Math.max(0, ...right[1].map((thread) => thread.updated_at))
        - Math.max(0, ...left[1].map((thread) => thread.updated_at))
      ))
      .map(([root, threads]) => ({
        id: root,
        root,
        name: workspaceName(root),
        shortPath: root,
        sessionCount: threads.length,
      }));
  });

  function threadCapabilities(thread: FocusThreadSummary): SessionActionCapabilities {
    if (options.connection.value !== 'connected' || options.snapshotInvalidated.value) {
      return unavailableCapabilities();
    }
    const source = thread.action_capabilities;
    return {
      rename: source?.rename === true,
      archive: source?.archive === true,
      fork: source?.fork === true,
      export: source?.export === true,
      review: source?.review === true,
      goal: source?.goal === true,
    };
  }

  function projectSession(thread: FocusThreadSummary): Session {
    const meta = options.meta.value;
    const runtimeInstance = thread.loaded_instance
      || (thread.observed_here ? meta?.instance ?? '' : '');
    const runtimeState = options.threadScope.value !== 'global'
      ? undefined
      : !thread.loaded_state_verified
        ? 'unknown'
        : !runtimeInstance
        ? 'unloaded'
        : runtimeInstance === meta?.instance || thread.observed_here
          ? 'current'
          : 'other';
    const workspaceId = thread.cwd || meta?.default_working_dir || '';
    return {
      id: thread.id,
      title: thread.title || thread.preview || thread.id,
      time: formatRelativeTime(thread.updated_at),
      busy: thread.status === 'active',
      pendingInteraction: thread.pending_interaction,
      updatedAt: thread.updated_at
        ? new Date(thread.updated_at * 1000).toISOString()
        : undefined,
      lastPrompt: thread.preview,
      workspaceId,
      workspaceName: workspaceName(workspaceId),
      runtimeState,
      runtimeInstance: thread.loaded_state_verified
        ? runtimeInstance || undefined
        : undefined,
      selectable: thread.selectable !== false,
      unavailableReason: thread.unavailable_reason || undefined,
      actionCapabilities: threadCapabilities(thread),
    };
  }

  const sessions = computed<Session[]>(() => options.threads.value.map(projectSession));
  const searchSessions = computed<Session[]>(() => (
    options.searchThreads.value.length
      ? options.searchThreads.value
      : options.threads.value
  ).map(projectSession));
  const workspaceGroups = computed<WorkspaceGroup[]>(() => (
    workspacesView.value.map((workspace) => ({
      workspace,
      sessions: sessions.value.filter((session) => session.workspaceId === workspace.id),
      hasMore: false,
      loadingMore: false,
      initialCount: Math.max(
        sessions.value.filter((session) => session.workspaceId === workspace.id).length,
        1,
      ),
    }))
  ));
  const activeThread = computed(() => (
    options.snapshot.value?.thread
    ?? options.threads.value.find((thread) => thread.id === options.activeThreadId.value)
    ?? null
  ));
  const activeSessionActionCapabilities = computed(() => {
    if (!options.scopeReady.value) return unavailableCapabilities();
    return activeThread.value
      ? threadCapabilities(activeThread.value)
      : unavailableCapabilities();
  });
  const canCompact = computed(() => (
    options.connection.value === 'connected'
    && options.scopeReady.value
    && !options.snapshotInvalidated.value
    && activeThread.value?.action_capabilities?.compact === true
  ));
  const turns = computed(() => options.snapshot.value?.turns ?? []);
  const tasks = computed(() => options.snapshot.value?.tasks ?? []);
  const goal = computed<AppGoal | null>(() => projectFocusGoal(options.snapshot.value?.goal ?? null));
  const pendingApprovals = computed(() => (options.snapshot.value?.pending_requests ?? [])
    .filter((request) => request.kind === 'approval')
    .map((request) => ({
      approvalId: pendingRequestActionToken(request),
      block: approvalBlock(request),
      agentName: request.agent_name || 'Codex',
      actions: request.actions,
    })));
  const questions = computed<UIQuestion[]>(() => (options.snapshot.value?.pending_requests ?? [])
    .filter((request) => request.kind === 'question' || request.kind === 'elicitation')
    .map(uiQuestion));
  const pendingBySession = computed<Record<string, { approvals: number; questions: number }>>(() => {
    const result: Record<string, { approvals: number; questions: number }> = {};
    for (const thread of options.threads.value) {
      result[thread.id] = {
        approvals: thread.pending_interaction === 'approval' ? 1 : 0,
        questions: thread.pending_interaction === 'question' ? 1 : 0,
      };
    }
    return result;
  });
  const activeWorkspaceId = computed(() => (
    options.draftWorkspaceId.value
    || activeThread.value?.cwd
    || workspacesView.value[0]?.id
    || options.meta.value?.default_working_dir
    || ''
  ));
  const visibleWorkspace = computed(() => (
    workspacesView.value.find((workspace) => workspace.id === activeWorkspaceId.value) ?? null
  ));
  const status = computed<ConversationStatus>(() => {
    const snapshot = options.snapshot.value;
    const contextUsage = projectFocusContextUsage(
      snapshot?.token_usage,
      snapshot?.token_usage_available === true,
    );
    return {
      model: selectedModel.value?.displayName
        ?? selectedModel.value?.model
        ?? 'Codex',
      modelId: selectedModel.value?.id ?? '',
      ctxUsed: contextUsage?.usedTokens ?? 0,
      ctxMax: contextUsage?.windowTokens ?? 0,
      ctxRemainingPct: contextUsage?.remainingPercent ?? null,
      permission: 'manual',
      branch: '',
      cwd: activeThread.value?.cwd
        || (!options.activeThreadId.value && options.scopeReady.value
          ? options.draftWorkspaceId.value
          : '')
        || visibleWorkspace.value?.root
        || options.meta.value?.default_working_dir
        || '',
      isGitRepo: false,
    };
  });

  return {
    reasoningEffortOptions,
    models,
    selectedModelId,
    selectedModel,
    workspacesView,
    sessions,
    searchSessions,
    workspaceGroups,
    activeThread,
    activeSessionActionCapabilities,
    canCompact,
    turns,
    tasks,
    goal,
    pendingApprovals,
    questions,
    pendingBySession,
    activeWorkspaceId,
    visibleWorkspace,
    status,
  };
}
