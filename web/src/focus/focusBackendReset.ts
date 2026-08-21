import { ref, shallowRef, type Ref } from 'vue';
import type { FocusWebApiPort } from './api';
import {
  FocusApiError,
  type FocusBackendResetPreview,
  type FocusBackendResetResult,
} from './types';

export type FocusBackendResetNotStartedReason =
  | 'disposed'
  | 'already-executing'
  | 'outcome-unknown'
  | 'preview-replaced'
  | 'preview-unavailable';

export type FocusBackendResetExecutionOutcome =
  | {
    disposition: 'not-started';
    reason: FocusBackendResetNotStartedReason;
  }
  | {
    disposition: 'known-no-effect';
    error: FocusApiError;
    refreshedPreview: FocusBackendResetPreview | null;
    refreshError: unknown | null;
  }
  | {
    disposition: 'outcome-unknown';
    error: unknown;
  }
  | {
    disposition: 'succeeded';
    result: FocusBackendResetResult;
    reloadError: unknown | null;
  }
  | { disposition: 'ignored' };

export interface FocusBackendResetTransactionOptions {
  api: Pick<FocusWebApiPort, 'backendResetPreview' | 'backendResetExecute'>;
  reloadAll(): Promise<void>;
}

export interface FocusBackendResetTransaction {
  readonly preview: Readonly<Ref<FocusBackendResetPreview | null>>;
  readonly previewPending: Readonly<Ref<boolean>>;
  readonly previewError: Readonly<Ref<unknown | null>>;
  readonly executing: Readonly<Ref<boolean>>;
  readonly result: Readonly<Ref<FocusBackendResetResult | null>>;
  readonly knownNoEffectError: Readonly<Ref<FocusApiError | null>>;
  readonly outcomeUnknown: Readonly<Ref<boolean>>;
  readonly outcomeUnknownError: Readonly<Ref<unknown | null>>;
  readonly reloadError: Readonly<Ref<unknown | null>>;
  readonly disposed: Readonly<Ref<boolean>>;
  refreshPreview(): Promise<FocusBackendResetPreview | null>;
  execute(
    capturedPreview: FocusBackendResetPreview,
  ): Promise<FocusBackendResetExecutionOutcome>;
  dispose(): void;
}

interface PreviewAuthority {
  readonly value: FocusBackendResetPreview;
  readonly status: FocusBackendResetPreview['status'];
  readonly connectionGeneration: number;
}

function isKnownNoEffect(error: unknown): error is FocusApiError {
  return error instanceof FocusApiError
    && error.status >= 400
    && error.status < 500;
}

function isTypedStaleReset(error: FocusApiError): boolean {
  return error.status === 409 && error.code === 'backend_reset_stale';
}

/**
 * Own one browser document's reset preview and execute transaction.
 *
 * A preview is only an immediate, object-identity capability. The owner
 * revokes it before the sole POST and never infers retry authority from a
 * status read. An unknown POST outcome is sticky until this document is
 * disposed; no method on this owner can clear it.
 */
export function createFocusBackendResetTransaction(
  options: FocusBackendResetTransactionOptions,
): FocusBackendResetTransaction {
  const preview = shallowRef<FocusBackendResetPreview | null>(null);
  const previewPending = ref(false);
  const previewError = shallowRef<unknown | null>(null);
  const executing = ref(false);
  const result = shallowRef<FocusBackendResetResult | null>(null);
  const knownNoEffectError = shallowRef<FocusApiError | null>(null);
  const outcomeUnknown = ref(false);
  const outcomeUnknownError = shallowRef<unknown | null>(null);
  const reloadError = shallowRef<unknown | null>(null);
  const disposed = ref(false);

  let previewAuthority: PreviewAuthority | null = null;
  let previewRequestGeneration = 0;
  let executionGeneration = 0;

  function previewRequestIsCurrent(generation: number): boolean {
    return !disposed.value && generation === previewRequestGeneration;
  }

  async function refreshPreviewInternal({
    clearKnownNoEffect,
  }: {
    clearKnownNoEffect: boolean;
  }): Promise<FocusBackendResetPreview | null> {
    if (disposed.value || executing.value) return null;
    const generation = ++previewRequestGeneration;
    previewAuthority = null;
    preview.value = null;
    previewPending.value = true;
    previewError.value = null;
    if (clearKnownNoEffect) knownNoEffectError.value = null;
    try {
      const observed = await options.api.backendResetPreview();
      if (!previewRequestIsCurrent(generation)) return null;
      preview.value = observed;
      previewAuthority = {
        value: observed,
        status: observed.status,
        connectionGeneration: observed.expected_connection_generation,
      };
      return observed;
    } catch (error) {
      if (previewRequestIsCurrent(generation)) previewError.value = error;
      return null;
    } finally {
      if (previewRequestIsCurrent(generation)) previewPending.value = false;
    }
  }

  function refreshPreview(): Promise<FocusBackendResetPreview | null> {
    return refreshPreviewInternal({ clearKnownNoEffect: true });
  }

  function notStartedReason(
    capturedPreview: FocusBackendResetPreview,
  ): FocusBackendResetNotStartedReason | null {
    if (disposed.value) return 'disposed';
    if (executing.value) return 'already-executing';
    if (outcomeUnknown.value) return 'outcome-unknown';
    const authority = previewAuthority;
    if (
      authority === null
      || preview.value !== capturedPreview
      || authority.value !== capturedPreview
      || authority.status !== capturedPreview.status
      || authority.connectionGeneration
        !== capturedPreview.expected_connection_generation
    ) return 'preview-replaced';
    if (
      authority.status !== 'available'
      && authority.status !== 'force-only'
    ) return 'preview-unavailable';
    return null;
  }

  async function execute(
    capturedPreview: FocusBackendResetPreview,
  ): Promise<FocusBackendResetExecutionOutcome> {
    const refusal = notStartedReason(capturedPreview);
    if (refusal !== null) {
      return { disposition: 'not-started', reason: refusal };
    }

    const authority = previewAuthority;
    if (authority === null) {
      return { disposition: 'not-started', reason: 'preview-replaced' };
    }
    const force = authority.status === 'force-only';
    const expectedConnectionGeneration = authority.connectionGeneration;
    previewAuthority = null;
    preview.value = null;
    executing.value = true;
    knownNoEffectError.value = null;
    result.value = null;
    reloadError.value = null;
    const generation = ++executionGeneration;

    try {
      const resetResult = await options.api.backendResetExecute({
        force,
        expectedConnectionGeneration,
      });
      if (disposed.value || generation !== executionGeneration) {
        return { disposition: 'ignored' };
      }

      // A complete typed result proves reset success before projection reload.
      result.value = resetResult;
      let observedReloadError: unknown | null = null;
      try {
        await options.reloadAll();
      } catch (error) {
        observedReloadError = error;
        if (!disposed.value && generation === executionGeneration) {
          reloadError.value = error;
        }
      }
      if (disposed.value || generation !== executionGeneration) {
        return { disposition: 'ignored' };
      }
      return {
        disposition: 'succeeded',
        result: resetResult,
        reloadError: observedReloadError,
      };
    } catch (error) {
      if (disposed.value || generation !== executionGeneration) {
        return { disposition: 'ignored' };
      }
      if (isKnownNoEffect(error)) {
        executing.value = false;
        previewAuthority = null;
        preview.value = null;
        knownNoEffectError.value = error;
        let refreshedPreview: FocusBackendResetPreview | null = null;
        let refreshError: unknown | null = null;
        if (isTypedStaleReset(error)) {
          refreshedPreview = await refreshPreviewInternal({
            clearKnownNoEffect: false,
          });
          if (refreshedPreview === null) refreshError = previewError.value;
          if (!disposed.value && generation === executionGeneration) {
            knownNoEffectError.value = error;
          }
        }
        if (disposed.value) return { disposition: 'ignored' };
        return {
          disposition: 'known-no-effect',
          error,
          refreshedPreview,
          refreshError,
        };
      }

      outcomeUnknown.value = true;
      outcomeUnknownError.value = error;
      return { disposition: 'outcome-unknown', error };
    } finally {
      if (!disposed.value && generation === executionGeneration) {
        executing.value = false;
      }
    }
  }

  function dispose(): void {
    if (disposed.value) return;
    disposed.value = true;
    previewRequestGeneration += 1;
    executionGeneration += 1;
    previewAuthority = null;
    previewPending.value = false;
    executing.value = false;
  }

  return {
    preview,
    previewPending,
    previewError,
    executing,
    result,
    knownNoEffectError,
    outcomeUnknown,
    outcomeUnknownError,
    reloadError,
    disposed,
    refreshPreview,
    execute,
    dispose,
  };
}
