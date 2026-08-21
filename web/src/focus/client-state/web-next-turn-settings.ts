import { computed, ref } from 'vue';
import type { ComputedRef, Ref } from 'vue';
import type { ThinkingLevel } from '../../types';
import type { FocusWebApiPort } from '../api';
import type { FocusNextTurnSettings } from '../types';

export type WebNextTurnSettingsInstallResult =
  | 'installed'
  | 'unchanged'
  | 'ignored'
  | 'conflict';

export interface WebNextTurnSettingsOptions {
  api: Pick<FocusWebApiPort, 'readNextTurnSettings' | 'updateNextTurnSettings'>;
  modelIsAvailable(modelId: string): boolean;
  supportedReasoningEfforts(modelId: string): readonly string[];
  runtimeEpochMismatch(): void;
  reportError(error: unknown): void;
}

export interface WebNextTurnSettingsOwner {
  readonly confirmed: Readonly<Ref<FocusNextTurnSettings | null>>;
  readonly runtimeEpoch: Readonly<Ref<string>>;
  readonly snapshot: ComputedRef<FocusNextTurnSettings | null>;
  readonly model: ComputedRef<string>;
  readonly reasoningEffort: ComputedRef<ThinkingLevel | undefined>;
  readonly approvalPolicy: ComputedRef<string>;
  readonly permissionsProfileId: ComputedRef<string>;
  readonly isDisposed: boolean;
  installRuntimeSnapshot(
    runtimeEpoch: string,
    settings: FocusNextTurnSettings,
  ): WebNextTurnSettingsInstallResult;
  refresh(): Promise<WebNextTurnSettingsInstallResult | null>;
  selectModel(modelId: string): Promise<void>;
  setThinking(level: ThinkingLevel): Promise<void>;
  setReasoningEffort(value: string): Promise<void>;
  setApprovalPolicy(value: string): Promise<void>;
  setPermissionsProfile(value: string): Promise<void>;
  dispose(): void;
}

function settingsAreEqual(
  left: FocusNextTurnSettings,
  right: FocusNextTurnSettings,
): boolean {
  return left.generation === right.generation
    && left.model === right.model
    && left.reasoning_effort === right.reasoning_effort
    && left.approval_policy === right.approval_policy
    && left.permissions_profile_id === right.permissions_profile_id;
}

export function createWebNextTurnSettings(
  options: WebNextTurnSettingsOptions,
): WebNextTurnSettingsOwner {
  const confirmed = ref<FocusNextTurnSettings | null>(null);
  const runtimeEpoch = ref('');
  let refreshPromise: Promise<WebNextTurnSettingsInstallResult | null> | null = null;
  let refreshInvalidated = false;
  let disposed = false;

  const snapshot = computed(() => (
    confirmed.value ? { ...confirmed.value } : null
  ));
  const model = computed(() => snapshot.value?.model ?? '');
  const reasoningEffort = computed<ThinkingLevel | undefined>(() => (
    snapshot.value?.reasoning_effort as ThinkingLevel || undefined
  ));
  const approvalPolicy = computed(() => snapshot.value?.approval_policy ?? '');
  const permissionsProfileId = computed(() => (
    snapshot.value?.permissions_profile_id ?? ''
  ));

  function reportGenerationConflict(generation: number): void {
    options.reportError(new Error(
      `Focus Web received conflicting next-turn settings for generation ${generation}.`,
    ));
  }

  function installSameRuntime(
    settings: FocusNextTurnSettings,
    refreshOnConflict: boolean,
  ): WebNextTurnSettingsInstallResult {
    const current = confirmed.value;
    if (!current || settings.generation > current.generation) {
      confirmed.value = { ...settings };
      return 'installed';
    }
    if (settings.generation < current.generation) return 'ignored';
    if (settingsAreEqual(settings, current)) return 'unchanged';
    if (refreshOnConflict) void refresh();
    else reportGenerationConflict(settings.generation);
    return 'conflict';
  }

  /**
   * Meta is runtime-epoch authority. A backend restart may legitimately reuse
   * generation 1 with a different service-start seed, so a new epoch replaces
   * the old snapshot before generation comparison resumes inside that epoch.
   */
  function installRuntimeSnapshot(
    nextRuntimeEpoch: string,
    settings: FocusNextTurnSettings,
  ): WebNextTurnSettingsInstallResult {
    if (disposed) return 'ignored';
    if (!runtimeEpoch.value || runtimeEpoch.value !== nextRuntimeEpoch) {
      runtimeEpoch.value = nextRuntimeEpoch;
      confirmed.value = { ...settings };
      return 'installed';
    }
    return installSameRuntime(settings, true);
  }

  function observeDirectResult(
    nextRuntimeEpoch: string,
    settings: FocusNextTurnSettings,
    refreshOnConflict: boolean,
  ): WebNextTurnSettingsInstallResult {
    if (disposed) return 'ignored';
    if (!runtimeEpoch.value) {
      runtimeEpoch.value = nextRuntimeEpoch;
      confirmed.value = { ...settings };
      return 'installed';
    }
    if (runtimeEpoch.value !== nextRuntimeEpoch) {
      // Only composite meta replacement owns an epoch transition. A direct
      // read/update from another epoch proves the projection must be rebuilt,
      // but cannot order itself against the currently installed epoch.
      options.runtimeEpochMismatch();
      return 'ignored';
    }
    return installSameRuntime(settings, refreshOnConflict);
  }

  async function refresh(): Promise<WebNextTurnSettingsInstallResult | null> {
    if (disposed) return null;
    if (refreshPromise) {
      // A settings_changed event carries no snapshot. If another event arrives
      // while its authority read is in flight, one coalesced follow-up read is
      // required so the later invalidation cannot be lost behind the old GET.
      refreshInvalidated = true;
      return refreshPromise;
    }
    refreshPromise = (async () => {
      let installed: WebNextTurnSettingsInstallResult | null = null;
      do {
        refreshInvalidated = false;
        try {
          const result = await options.api.readNextTurnSettings();
          if (disposed) return null;
          installed = observeDirectResult(
            result.runtime_epoch,
            result.next_turn_settings,
            false,
          );
        } catch (error) {
          if (!disposed) options.reportError(error);
          installed = null;
        }
      } while (!disposed && refreshInvalidated);
      return installed;
    })();
    try {
      return await refreshPromise;
    } finally {
      refreshPromise = null;
    }
  }

  async function update(
    changes: Partial<Omit<FocusNextTurnSettings, 'generation'>>,
  ): Promise<void> {
    if (disposed) return;
    if (!confirmed.value) {
      options.reportError(new Error('Focus Web next-turn settings are not loaded.'));
      return;
    }
    try {
      const result = await options.api.updateNextTurnSettings(changes);
      if (disposed) return;
      observeDirectResult(result.runtime_epoch, result.next_turn_settings, true);
    } catch (error) {
      if (!disposed) options.reportError(error);
    }
  }

  async function selectModel(modelId: string): Promise<void> {
    if (!options.modelIsAvailable(modelId)) return;
    const value = modelId === 'focus:auto' ? '' : modelId.trim();
    const changes: Partial<Omit<FocusNextTurnSettings, 'generation'>> = { model: value };
    const currentEffort = confirmed.value?.reasoning_effort ?? '';
    const supportedEfforts = value ? options.supportedReasoningEfforts(value) : [];
    if (
      currentEffort
      && supportedEfforts.length > 0
      && !supportedEfforts.includes(currentEffort)
    ) {
      changes.reasoning_effort = '';
    }
    await update(changes);
  }

  async function setThinking(level: ThinkingLevel): Promise<void> {
    await update({ reasoning_effort: level.trim().toLowerCase() });
  }

  async function setReasoningEffort(value: string): Promise<void> {
    await update({ reasoning_effort: value.trim().toLowerCase() });
  }

  async function setApprovalPolicy(value: string): Promise<void> {
    await update({ approval_policy: value.trim() });
  }

  async function setPermissionsProfile(value: string): Promise<void> {
    await update({ permissions_profile_id: value.trim() });
  }

  function dispose(): void {
    disposed = true;
    refreshInvalidated = false;
  }

  return {
    confirmed,
    runtimeEpoch,
    snapshot,
    model,
    reasoningEffort,
    approvalPolicy,
    permissionsProfileId,
    get isDisposed() {
      return disposed;
    },
    installRuntimeSnapshot,
    refresh,
    selectModel,
    setThinking,
    setReasoningEffort,
    setApprovalPolicy,
    setPermissionsProfile,
    dispose,
  };
}
