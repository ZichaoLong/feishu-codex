import { computed, ref, shallowRef } from 'vue';
import type { ComputedRef, Ref } from 'vue';
import type { FocusWebApiPort } from './api';
import type { ClientIntentClock } from './clientIntentClock';
import {
  FocusApiError,
  isStaleWebReadError,
  type FocusThreadScope,
  type FocusWriterProfile,
  type WorkspaceDraftOpenOutcome,
} from './types';

export type NavigationIntentStatus = 'confirmed' | 'pending' | 'repair';

export interface NavigationIntentReceipt {
  requestGeneration: number;
  navigationGeneration: number;
  authorityGeneration: number;
}

export interface NavigationStateFloor {
  navigationGeneration: number;
  status: NavigationIntentStatus;
  scopeReceiptGeneration: number;
  authorityGeneration: number;
  repairRequired: boolean;
}

/**
 * An atomic proof that the visible semantic target and attachment generation
 * came from one accepted writer profile. Consumers must not reconstruct this
 * receipt from independently reactive fields.
 */
export interface ConfirmedWriterScopeReceipt {
  receiptGeneration: number;
  navigationGeneration: number;
  clientId: string;
  selectedThreadId: string;
  workingDir: string;
  scopeGeneration: number;
  attachmentScope: string;
  composerScopeId: string;
}

export interface ObservedProfileOptions {
  /** Exact navigation which caused this response, when one exists. */
  navigationGeneration?: number;
  /** Repair boundary captured with the navigation receipt. */
  navigationAuthorityGeneration?: number;
  /** Thread snapshots must echo the exact requested thread. */
  expectedThreadId?: string;
  /** Exact owner floor captured before a direct meta/composite authority read. */
  freshAuthorityFloor?: NavigationStateFloor;
}

export interface FocusNavigationProfileOptions {
  intentClock: ClientIntentClock;
  api: Pick<FocusWebApiPort, 'meta' | 'updateProfile'>;
  initialClientId: string;
  defaultWorkspace(): string;
  clearSnapshot(): void;
  /** Return presentation to the live-tail history window before an explicit
   *  thread selection. History remains owned outside navigation/projection. */
  clearHistoryView(): void;
  updateThreadQuery(threadId: string): void;
  reportError(error: unknown): void;
  clearError(): void;
  setNavigationLoading(loading: boolean): void;
  threadUnavailableReason(threadId: string): string;
  workspaceNavigationBlockReason(): string;
}

export interface FocusNavigationProjectionPort {
  refreshActiveThread(options?: {
    requestIntentGeneration?: number;
    navigationGeneration?: number;
    navigationAuthorityGeneration?: number;
  }): Promise<boolean>;
  refreshThreads(): Promise<boolean>;
  scheduleProjectionRefresh(): void;
  invalidateWireProjection(): void;
}

export interface FocusNavigationProfile {
  readonly registeredClientId: Readonly<Ref<string>>;
  readonly threadScope: Readonly<Ref<FocusThreadScope>>;
  readonly activeThreadId: Readonly<Ref<string>>;
  readonly draftWorkspaceId: Readonly<Ref<string>>;
  readonly confirmedWriterProfile: Readonly<Ref<FocusWriterProfile | null>>;
  readonly writerProfile: ComputedRef<FocusWriterProfile | null>;
  readonly scopeReceipt: Readonly<Ref<ConfirmedWriterScopeReceipt | null>>;
  readonly scopeReady: ComputedRef<boolean>;
  readonly composerReady: ComputedRef<boolean>;
  readonly composerScopeId: ComputedRef<string>;
  readonly currentNavigationStatus: NavigationIntentStatus;
  readonly navigationRepairIsRequired: boolean;
  readonly isDisposed: boolean;
  bindProjection(port: FocusNavigationProjectionPort): void;
  setThreadScope(scope: FocusThreadScope): boolean;
  registerClient(clientId: string): void;
  installInitialProfile(profile: FocusWriterProfile): ConfirmedWriterScopeReceipt | null;
  beginThreadNavigation(threadId: string): NavigationIntentReceipt;
  beginWorkspaceNavigation(): NavigationIntentReceipt;
  showUnconfirmedThread(threadId: string): NavigationIntentReceipt;
  showRepairDraft(workspace?: string): void;
  navigationIntentIsCurrent(generation: number): boolean;
  navigationIntentMayConverge(generation: number): boolean;
  markNavigationFailed(generation: number): boolean;
  requireNavigationRepair(): void;
  captureNavigationStateFloor(): NavigationStateFloor;
  navigationStateFloorIsCurrent(floor: NavigationStateFloor): boolean;
  navigationRepairIsCurrent(floor: NavigationStateFloor): boolean;
  installObservedProfile(
    profile: FocusWriterProfile,
    options?: ObservedProfileOptions,
  ): ConfirmedWriterScopeReceipt | null;
  scopeReceiptIsCurrent(receipt: ConfirmedWriterScopeReceipt): boolean;
  clearToRepairDraft(workspace?: string): void;
  selectThread(threadId: string): Promise<void>;
  confirmUnconfirmedThread(threadId: string): Promise<boolean>;
  openWorkspaceDraft(workspaceId: string): Promise<WorkspaceDraftOpenOutcome>;
  changeThreadScope(scope: FocusThreadScope): Promise<void>;
  restoreInitialTarget(input: {
    requestedThreadId: string;
    recoveryThreadId: string;
    persistedThreadId: string;
  }): Promise<void>;
  dispose(): void;
}

function copyProfile(profile: FocusWriterProfile): FocusWriterProfile {
  return { ...profile };
}

function navigationCoordinatesMatch(
  left: FocusWriterProfile,
  right: FocusWriterProfile,
): boolean {
  return left.scope_generation === right.scope_generation
    && left.selected_thread_id.trim() === right.selected_thread_id.trim()
    && left.working_dir.trim() === right.working_dir.trim();
}

export function createFocusNavigationProfile(
  options: FocusNavigationProfileOptions,
): FocusNavigationProfile {
  const registeredClientId = ref(options.initialClientId);
  const threadScope = ref<FocusThreadScope>('current');
  const activeThreadId = ref('');
  const draftWorkspaceId = ref('');
  const confirmedWriterProfile = ref<FocusWriterProfile | null>(null);
  const scopeReceipt = shallowRef<ConfirmedWriterScopeReceipt | null>(null);
  const navigationGeneration = ref(0);
  const navigationStatus = ref<NavigationIntentStatus>('confirmed');
  const authorityGeneration = ref(0);
  const repairRequired = ref(false);
  let scopeReceiptGeneration = 0;
  let writerScopeGenerationFloor = 0;
  let projection: FocusNavigationProjectionPort | null = null;
  const disposed = ref(false);

  const writerProfile = computed<FocusWriterProfile | null>(() => (
    confirmedWriterProfile.value
      ? copyProfile(confirmedWriterProfile.value)
      : null
  ));
  const scopeReady = computed(() => {
    if (disposed.value) return false;
    const receipt = scopeReceipt.value;
    if (
      !receipt
      || navigationStatus.value !== 'confirmed'
      || repairRequired.value
    ) return false;
    if (receipt.receiptGeneration !== scopeReceiptGeneration) return false;
    if (receipt.clientId !== registeredClientId.value) return false;
    if (receipt.navigationGeneration !== navigationGeneration.value) return false;
    if (receipt.selectedThreadId !== activeThreadId.value) return false;
    return receipt.selectedThreadId !== ''
      || (
        receipt.workingDir !== ''
        && receipt.workingDir === draftWorkspaceId.value
      );
  });
  const composerReady = computed(() => scopeReady.value);
  const composerScopeId = computed(() => (
    scopeReady.value ? scopeReceipt.value?.composerScopeId ?? '' : ''
  ));
  function convergeVisibleProfile(profile: FocusWriterProfile): void {
    const selectedThreadId = profile.selected_thread_id.trim();
    if (selectedThreadId) {
      if (activeThreadId.value !== selectedThreadId) options.clearSnapshot();
      activeThreadId.value = selectedThreadId;
      draftWorkspaceId.value = '';
      options.updateThreadQuery(selectedThreadId);
      return;
    }
    if (activeThreadId.value) options.clearSnapshot();
    activeThreadId.value = '';
    draftWorkspaceId.value = profile.working_dir.trim() || options.defaultWorkspace();
    options.updateThreadQuery('');
  }

  function publishScopeReceipt(profile: FocusWriterProfile): ConfirmedWriterScopeReceipt {
    scopeReceiptGeneration += 1;
    const clientId = registeredClientId.value || 'unregistered-document';
    const selectedThreadId = profile.selected_thread_id.trim();
    const workingDir = profile.working_dir.trim() || options.defaultWorkspace();
    const attachmentScope = selectedThreadId
      ? `thread:${selectedThreadId}`
      : workingDir ? `draft:${workingDir}` : '';
    const composerScopeId = attachmentScope
      ? `${clientId}:generation:${profile.scope_generation}:${attachmentScope}`
      : '';
    const receipt: ConfirmedWriterScopeReceipt = {
      receiptGeneration: scopeReceiptGeneration,
      navigationGeneration: navigationGeneration.value,
      clientId,
      selectedThreadId,
      workingDir,
      scopeGeneration: profile.scope_generation,
      attachmentScope,
      composerScopeId,
    };
    scopeReceipt.value = receipt;
    return receipt;
  }

  function profileCanBeObserved(
    profile: FocusWriterProfile,
    observedOptions: ObservedProfileOptions,
  ): boolean {
    if (profile.scope_generation < writerScopeGenerationFloor) return false;
    const expectedThreadId = observedOptions.expectedThreadId?.trim();
    if (
      expectedThreadId !== undefined
      && profile.selected_thread_id.trim() !== expectedThreadId
    ) return false;
    const current = confirmedWriterProfile.value;
    if (current) {
      if (profile.scope_generation < current.scope_generation) return false;
      if (
        profile.scope_generation === current.scope_generation
        && !navigationCoordinatesMatch(profile, current)
      ) return false;
    }
    const observedGeneration = observedOptions.navigationGeneration;
    const observedAuthority = observedOptions.navigationAuthorityGeneration;
    const freshAuthorityIsCurrent = observedOptions.freshAuthorityFloor !== undefined
      && navigationStateFloorIsCurrent(observedOptions.freshAuthorityFloor);
    if (observedOptions.freshAuthorityFloor && !freshAuthorityIsCurrent) return false;
    if (navigationStatus.value === 'repair' && !freshAuthorityIsCurrent) {
      return observedGeneration !== undefined
        && observedAuthority === authorityGeneration.value
        && observedGeneration < navigationGeneration.value;
    }
    if (observedGeneration !== undefined) {
      if (observedAuthority !== authorityGeneration.value) return false;
      if (observedGeneration === navigationGeneration.value) return true;
      return observedGeneration < navigationGeneration.value
        && current !== null
        && navigationCoordinatesMatch(profile, current);
    }
    if (navigationStatus.value === 'pending') return false;
    if (navigationStatus.value === 'repair') return freshAuthorityIsCurrent;
    return true;
  }

  function installObservedProfile(
    profile: FocusWriterProfile,
    observedOptions: ObservedProfileOptions = {},
  ): ConfirmedWriterScopeReceipt | null {
    if (disposed.value) return null;
    if (!profileCanBeObserved(profile, observedOptions)) {
      const expectedThreadId = observedOptions.expectedThreadId?.trim();
      if (
        repairRequired.value
        && navigationStatus.value === 'pending'
        && observedOptions.navigationGeneration === navigationGeneration.value
        && expectedThreadId !== undefined
        && profile.selected_thread_id.trim() === expectedThreadId
        && profile.scope_generation >= writerScopeGenerationFloor
      ) {
        // The response proves a newer server scope but crossed an explicit
        // repair boundary. Keep the optimistic target non-writable while
        // preventing an older fresh-meta response from reviving old A.
        writerScopeGenerationFloor = profile.scope_generation;
        navigationStatus.value = 'repair';
      }
      return null;
    }
    const current = confirmedWriterProfile.value;
    const previousReceipt = scopeReceipt.value;
    const preserveScopeReceipt = navigationStatus.value === 'confirmed'
      && !repairRequired.value
      && current !== null
      && previousReceipt !== null
      && navigationCoordinatesMatch(profile, current)
      && previousReceipt.navigationGeneration === navigationGeneration.value
      && previousReceipt.clientId === (registeredClientId.value || 'unregistered-document');
    const nextProfile = copyProfile(profile);
    confirmedWriterProfile.value = nextProfile;
    writerScopeGenerationFloor = Math.max(
      writerScopeGenerationFloor,
      nextProfile.scope_generation,
    );
    const observedGeneration = observedOptions.navigationGeneration;
    const freshAuthorityIsCurrent = observedOptions.freshAuthorityFloor !== undefined
      && navigationStateFloorIsCurrent(observedOptions.freshAuthorityFloor);
    if (observedGeneration !== undefined && observedGeneration > navigationGeneration.value) {
      return null;
    }
    // A current response, or an authoritative repair after the newest failure,
    // replaces the navigation generation and its old A-shaped receipt.
    if (
      observedGeneration !== undefined
      && observedGeneration === navigationGeneration.value
    ) {
      navigationStatus.value = 'confirmed';
      repairRequired.value = false;
    } else if (navigationStatus.value === 'repair' && freshAuthorityIsCurrent) {
      navigationStatus.value = 'confirmed';
      repairRequired.value = false;
    }
    convergeVisibleProfile(nextProfile);
    return preserveScopeReceipt ? previousReceipt : publishScopeReceipt(nextProfile);
  }

  function installInitialProfile(
    profile: FocusWriterProfile,
  ): ConfirmedWriterScopeReceipt | null {
    if (disposed.value) return null;
    navigationStatus.value = 'confirmed';
    repairRequired.value = false;
    confirmedWriterProfile.value = copyProfile(profile);
    writerScopeGenerationFloor = profile.scope_generation;
    convergeVisibleProfile(profile);
    return publishScopeReceipt(profile);
  }

  function beginNavigation(): NavigationIntentReceipt {
    navigationGeneration.value += 1;
    navigationStatus.value = 'pending';
    return {
      requestGeneration: options.intentClock.beginIntent(),
      navigationGeneration: navigationGeneration.value,
      authorityGeneration: authorityGeneration.value,
    };
  }

  function beginThreadNavigation(threadId: string): NavigationIntentReceipt {
    const normalized = threadId.trim();
    const receipt = beginNavigation();
    if (activeThreadId.value !== normalized) options.clearSnapshot();
    activeThreadId.value = normalized;
    draftWorkspaceId.value = '';
    options.updateThreadQuery(normalized);
    return receipt;
  }

  function beginWorkspaceNavigation(): NavigationIntentReceipt {
    return beginNavigation();
  }

  function showUnconfirmedThread(threadId: string): NavigationIntentReceipt {
    return beginThreadNavigation(threadId);
  }

  function showRepairDraft(workspace = ''): void {
    if (disposed.value) return;
    repairRequired.value = true;
    authorityGeneration.value += 1;
    navigationStatus.value = 'repair';
    if (activeThreadId.value) options.clearSnapshot();
    activeThreadId.value = '';
    draftWorkspaceId.value = workspace.trim() || options.defaultWorkspace();
    options.updateThreadQuery('');
  }

  function navigationIntentIsCurrent(generation: number): boolean {
    return generation === navigationGeneration.value;
  }

  function navigationIntentMayConverge(generation: number): boolean {
    return navigationIntentIsCurrent(generation)
      || (generation < navigationGeneration.value && navigationStatus.value === 'repair');
  }

  function markNavigationFailed(generation: number): boolean {
    if (!navigationIntentIsCurrent(generation)) return false;
    navigationStatus.value = 'repair';
    repairRequired.value = true;
    return true;
  }

  function requireNavigationRepair(): void {
    if (disposed.value) return;
    repairRequired.value = true;
    authorityGeneration.value += 1;
    if (navigationStatus.value !== 'pending') navigationStatus.value = 'repair';
  }

  function captureNavigationStateFloor(): NavigationStateFloor {
    return {
      navigationGeneration: navigationGeneration.value,
      status: navigationStatus.value,
      scopeReceiptGeneration,
      authorityGeneration: authorityGeneration.value,
      repairRequired: repairRequired.value,
    };
  }

  function navigationStateFloorIsCurrent(floor: NavigationStateFloor): boolean {
    return floor.navigationGeneration === navigationGeneration.value
      && floor.status === navigationStatus.value
      && floor.scopeReceiptGeneration === scopeReceiptGeneration
      && floor.authorityGeneration === authorityGeneration.value
      && floor.repairRequired === repairRequired.value;
  }

  function navigationRepairIsCurrent(floor: NavigationStateFloor): boolean {
    return floor.status === 'repair'
      && navigationStatus.value === 'repair'
      && navigationStateFloorIsCurrent(floor);
  }

  function scopeReceiptIsCurrent(receipt: ConfirmedWriterScopeReceipt): boolean {
    return scopeReady.value
      && scopeReceipt.value === receipt;
  }

  function registerClient(clientId: string): void {
    if (disposed.value) return;
    const normalized = clientId.trim();
    if (registeredClientId.value === normalized) return;
    registeredClientId.value = normalized;
    // A writer identity is part of the capability. Reissue it atomically from
    // the same confirmed profile rather than editing an old receipt in place.
    const profile = confirmedWriterProfile.value;
    if (profile && navigationStatus.value === 'confirmed') publishScopeReceipt(profile);

  }

  function setThreadScope(scope: FocusThreadScope): boolean {
    if (threadScope.value === scope) return false;
    threadScope.value = scope;
    return true;
  }

  function bindProjection(port: FocusNavigationProjectionPort): void {
    if (projection && projection !== port) {
      throw new Error('Focus navigation projection port is already bound.');
    }
    projection = port;
  }

  function requireProjection(): FocusNavigationProjectionPort {
    if (!projection) throw new Error('Focus navigation projection port is not bound.');
    return projection;
  }

  async function reconcileFailedNavigation(generation: number): Promise<void> {
    if (disposed.value) return;
    const authorityFloor = captureNavigationStateFloor();
    try {
      const nextMeta = await options.api.meta();
      if (disposed.value) return;
      if (!navigationIntentMayConverge(generation)) return;
      const installed = installObservedProfile(nextMeta.writer_profile, {
        freshAuthorityFloor: authorityFloor,
      });
      if (!installed) requireProjection().invalidateWireProjection();
    } catch {
      if (disposed.value) return;
      if (!navigationIntentMayConverge(generation)) return;
      requireNavigationRepair();
      requireProjection().invalidateWireProjection();
    }
  }

  async function settleNavigationRead(
    receipt: NavigationIntentReceipt,
    reportFailure = true,
    staleRetryUsed = false,
  ): Promise<boolean> {
    if (disposed.value) return false;
    try {
      const refreshed = await requireProjection().refreshActiveThread({
        requestIntentGeneration: receipt.requestGeneration,
        navigationGeneration: receipt.navigationGeneration,
        navigationAuthorityGeneration: receipt.authorityGeneration,
      });
      if (disposed.value) return false;
      if (refreshed) return true;
      if (staleRetryUsed) {
        requireProjection().scheduleProjectionRefresh();
        return false;
      }
    } catch (error) {
      if (disposed.value) return false;
      const current = navigationIntentIsCurrent(receipt.navigationGeneration);
      if (isStaleWebReadError(error) && current) {
        if (!staleRetryUsed) {
          return settleNavigationRead(receipt, reportFailure, true);
        }
        requireProjection().scheduleProjectionRefresh();
        return false;
      }
      if (current && reportFailure) {
        options.reportError(error);
      }
    }
    if (markNavigationFailed(receipt.navigationGeneration)) {
      await reconcileFailedNavigation(receipt.navigationGeneration);
    }
    return false;
  }

  async function selectThread(threadId: string): Promise<void> {
    if (disposed.value) return;
    const normalized = threadId.trim();
    if (!normalized) return;
    const unavailableReason = options.threadUnavailableReason(normalized);
    if (unavailableReason) {
      options.reportError(new Error(unavailableReason));
      return;
    }
    const receipt = beginThreadNavigation(normalized);
    options.setNavigationLoading(true);
    options.clearHistoryView();
    options.clearError();
    try {
      await settleNavigationRead(receipt);
    } finally {
      if (navigationIntentIsCurrent(receipt.navigationGeneration)) {
        if (disposed.value) return;
        options.setNavigationLoading(false);
      }
    }
  }

  async function confirmUnconfirmedThread(threadId: string): Promise<boolean> {
    if (disposed.value) return false;
    const normalized = threadId.trim();
    if (!normalized) return false;
    // A confirmed writer scope proves where a mutation may be sent; it does
    // not prove that the matching transcript has been materialized.  Explicit
    // recovery therefore crosses the read boundary even for A -> A and issues
    // a replacement receipt only after the snapshot/profile pair settles.
    const receipt = showUnconfirmedThread(normalized);
    await settleNavigationRead(receipt);
    const confirmed = scopeReceipt.value;
    return confirmed?.selectedThreadId === normalized
      && scopeReceiptIsCurrent(confirmed);
  }

  function failedWorkspaceOutcome(): WorkspaceDraftOpenOutcome {
    return {
      status: 'failed',
      committed: false,
      workspace: '',
      scopeChanged: false,
      previousComposerScopeId: '',
      currentComposerScopeId: '',
      attachmentDisposition: 'unchanged',
      composerScopeEffect: 'none',
      invalidatedAttachmentCount: 0,
      reboundAttachmentCount: 0,
    };
  }

  function supersededWorkspaceOutcome(
    committed: boolean,
  ): Extract<WorkspaceDraftOpenOutcome, { status: 'superseded' }> {
    return {
      status: 'superseded',
      committed,
      workspace: '',
      scopeChanged: false,
      previousComposerScopeId: '',
      currentComposerScopeId: '',
      attachmentDisposition: 'unchanged',
      composerScopeEffect: 'none',
      invalidatedAttachmentCount: 0,
      reboundAttachmentCount: 0,
    };
  }

  async function openWorkspaceDraft(workspaceId: string): Promise<WorkspaceDraftOpenOutcome> {
    if (disposed.value) return failedWorkspaceOutcome();
    const requestedWorkspace = workspaceId.trim() || options.defaultWorkspace();
    if (!requestedWorkspace) {
      options.reportError(new Error('Focus needs a workspace for a new conversation.'));
      return failedWorkspaceOutcome();
    }
    const blockReason = options.workspaceNavigationBlockReason();
    if (blockReason) {
      options.reportError(new Error(blockReason));
      return failedWorkspaceOutcome();
    }
    const intent = beginWorkspaceNavigation();
    options.clearError();
    try {
      const result = await options.api.updateProfile({
        selected_thread_id: '',
        working_dir: requestedWorkspace,
      }, intent.requestGeneration);
      if (disposed.value) return supersededWorkspaceOutcome(false);
      const writerScope = registeredClientId.value || options.initialClientId;
      const composerScopeIdFor = (scope: string, generation: number) => (
        scope ? `${writerScope}:generation:${generation}:${scope}` : ''
      );
      const previousComposerScopeId = composerScopeIdFor(
        result.previous_attachment_scope,
        result.previous_scope_generation,
      );
      const currentComposerScopeId = composerScopeIdFor(
        result.current_attachment_scope,
        result.current_scope_generation,
      );
      const previousScopeGeneration = scopeReceipt.value?.scopeGeneration ?? 0;
      const mayConverge = navigationIntentMayConverge(intent.navigationGeneration);
      const installedScope = installObservedProfile(
        result.writer_profile,
        {
          navigationGeneration: intent.navigationGeneration,
          navigationAuthorityGeneration: intent.authorityGeneration,
        },
      );
      if (
        !installedScope
        || (!mayConverge && !scopeReceiptIsCurrent(installedScope))
      ) {
        if (
          navigationIntentIsCurrent(intent.navigationGeneration)
          && navigationStatus.value === 'pending'
        ) {
          markNavigationFailed(intent.navigationGeneration);
          await reconcileFailedNavigation(intent.navigationGeneration);
        }
        return {
          status: 'superseded',
          committed: true,
          workspace: result.writer_profile.working_dir.trim(),
          scopeChanged: result.scope_changed,
          previousComposerScopeId,
          currentComposerScopeId,
          attachmentDisposition: result.attachment_scope_disposition,
          composerScopeEffect: !result.scope_changed
            ? 'none'
            : result.current_scope_generation > previousScopeGeneration
              ? 'apply'
              : 'clearPrevious',
          invalidatedAttachmentCount: result.invalidated_attachment_count,
          reboundAttachmentCount: result.rebound_attachment_count,
        };
      }
      if (!installedScope.workingDir) {
        options.reportError(new Error(
          'Focus did not confirm a workspace for the new conversation.',
        ));
        return {
          ...supersededWorkspaceOutcome(true),
          scopeChanged: result.scope_changed,
          previousComposerScopeId,
          currentComposerScopeId,
          attachmentDisposition: result.attachment_scope_disposition,
          composerScopeEffect: result.scope_changed ? 'apply' : 'none',
          invalidatedAttachmentCount: result.invalidated_attachment_count,
          reboundAttachmentCount: result.rebound_attachment_count,
        };
      }
      return {
        status: 'committed',
        committed: true,
        workspace: installedScope.workingDir,
        scopeChanged: result.scope_changed,
        previousComposerScopeId,
        currentComposerScopeId,
        attachmentDisposition: result.attachment_scope_disposition,
        composerScopeEffect: result.scope_changed ? 'apply' : 'none',
        invalidatedAttachmentCount: result.invalidated_attachment_count,
        reboundAttachmentCount: result.rebound_attachment_count,
      };
    } catch (error) {
      if (disposed.value) return supersededWorkspaceOutcome(false);
      if (!navigationIntentIsCurrent(intent.navigationGeneration)) {
        return supersededWorkspaceOutcome(false);
      }
      markNavigationFailed(intent.navigationGeneration);
      await reconcileFailedNavigation(intent.navigationGeneration);
      if (disposed.value) return supersededWorkspaceOutcome(false);
      if (error instanceof FocusApiError && error.code === 'stale_intent') {
        return supersededWorkspaceOutcome(false);
      }
      options.reportError(error);
      return failedWorkspaceOutcome();
    }
  }

  async function changeThreadScope(scope: FocusThreadScope): Promise<void> {
    if (disposed.value || !setThreadScope(scope)) return;
    options.clearError();
    try {
      await requireProjection().refreshThreads();
    } catch (error) {
      if (!disposed.value) options.reportError(error);
    }
  }

  async function refreshConfirmedTarget(
    threadId: string,
    reportFailure: boolean,
  ): Promise<boolean> {
    try {
      if (await requireProjection().refreshActiveThread({
        navigationGeneration: navigationGeneration.value,
        navigationAuthorityGeneration: authorityGeneration.value,
      })) return true;
    } catch (error) {
      if (isStaleWebReadError(error)) {
        requireProjection().scheduleProjectionRefresh();
        return false;
      }
      if (!disposed.value && reportFailure) options.reportError(error);
    }
    if (disposed.value) return false;
    requireNavigationRepair();
    await reconcileFailedNavigation(navigationGeneration.value);
    const confirmed = scopeReceipt.value;
    return confirmed?.selectedThreadId === threadId && scopeReceiptIsCurrent(confirmed);
  }

  async function restoreInitialTarget(input: {
    requestedThreadId: string;
    recoveryThreadId: string;
    persistedThreadId: string;
  }): Promise<void> {
    if (disposed.value) return;
    const requested = input.requestedThreadId.trim();
    const recovery = input.recoveryThreadId.trim();
    const persisted = input.persistedThreadId.trim();
    if (requested) {
      const unavailableReason = options.threadUnavailableReason(requested);
      if (!unavailableReason) {
        await settleNavigationRead(beginThreadNavigation(requested));
        return;
      }
      options.reportError(new Error(unavailableReason));
    }
    if (recovery) {
      const current = scopeReceipt.value;
      if (current?.selectedThreadId === recovery && scopeReceiptIsCurrent(current)) {
        await refreshConfirmedTarget(recovery, true);
        return;
      }
      await confirmUnconfirmedThread(recovery);
      return;
    }
    if (persisted) {
      // Even a list-marked unavailable persisted selection crosses the direct
      // read authority. The server may compare-clear stale durable selection,
      // after which the exact fresh-meta reconcile restores a writable draft.
      const current = scopeReceipt.value;
      if (current?.selectedThreadId === persisted && scopeReceiptIsCurrent(current)) {
        // A stale durable selection is background recovery, not an explicit
        // user request.  Its direct-read failure may compare-clear the profile
        // and should converge silently; an explicit URL failure was already
        // reported in the requested-target branch above.
        await refreshConfirmedTarget(persisted, false);
        return;
      }
      await settleNavigationRead(beginThreadNavigation(persisted), requested === '');
    }
  }

  function dispose(): void {
    if (disposed.value) return;
    disposed.value = true;
    authorityGeneration.value += 1;
    navigationGeneration.value += 1;
    navigationStatus.value = 'repair';
    repairRequired.value = true;
    scopeReceipt.value = null;
  }

  return {
    registeredClientId,
    threadScope,
    activeThreadId,
    draftWorkspaceId,
    confirmedWriterProfile,
    writerProfile,
    scopeReceipt,
    scopeReady,
    composerReady,
    composerScopeId,
    get currentNavigationStatus() {
      return navigationStatus.value;
    },
    get navigationRepairIsRequired() {
      return repairRequired.value;
    },
    get isDisposed() {
      return disposed.value;
    },
    bindProjection,
    setThreadScope,
    registerClient,
    installInitialProfile,
    beginThreadNavigation,
    beginWorkspaceNavigation,
    showUnconfirmedThread,
    showRepairDraft,
    navigationIntentIsCurrent,
    navigationIntentMayConverge,
    markNavigationFailed,
    requireNavigationRepair,
    captureNavigationStateFloor,
    navigationStateFloorIsCurrent,
    navigationRepairIsCurrent,
    installObservedProfile,
    scopeReceiptIsCurrent,
    clearToRepairDraft: showRepairDraft,
    selectThread,
    confirmUnconfirmedThread,
    openWorkspaceDraft,
    changeThreadScope,
    restoreInitialTarget,
    dispose,
  };
}
