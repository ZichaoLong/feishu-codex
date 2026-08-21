import type { RuntimeNoticeItem, RuntimeNoticePresentation } from './client-state/runtime-notices';
import type { OperatorStatusPresentation } from './operatorWarningPresentation';
import type { FocusActiveTurnContext, FocusOwner } from './types';

export type RuntimeDetailsTone = 'neutral' | 'advisory' | 'danger';

export interface RuntimeDetailsPresentation {
  instance: string;
  connection: string;
  runtimeEpoch: string;
  revision: number;
  owner: FocusOwner;
  activeTurnContext: FocusActiveTurnContext | null;
  operatorStatus: OperatorStatusPresentation;
  operatorStatusStale: boolean;
  runtimeNotices: RuntimeNoticePresentation;
  primaryRuntimeErrors: readonly RuntimeNoticeItem[];
  primaryAttentionCount: number;
  advisoryAttentionCount: number;
  tone: RuntimeDetailsTone;
}

export interface RuntimeDetailsInput {
  instance: string;
  connection: string;
  runtimeEpoch: string;
  revision: number;
  owner: FocusOwner;
  activeTurnContext: FocusActiveTurnContext | null;
  operatorStatus: OperatorStatusPresentation;
  operatorStatusStale: boolean;
  runtimeNotices: RuntimeNoticePresentation;
}

/**
 * Join already-admitted browser facts for presentation only.
 *
 * This projection owns neither health nor lifecycle. In particular, a missing
 * or unknown warning attention is handled upstream as correctness; this layer
 * never parses warning text or code to decide prominence.
 */
export function projectRuntimeDetailsPresentation(
  input: RuntimeDetailsInput,
): RuntimeDetailsPresentation {
  const primaryRuntimeErrors = input.runtimeNotices.notices.filter(
    (notice) => notice.method === 'error',
  );
  const runtimeWarnings = input.runtimeNotices.notices.length - primaryRuntimeErrors.length;
  const primaryAttentionCount = input.operatorStatus.primaryWarningCount
    + primaryRuntimeErrors.length
    + (input.operatorStatus.degradedWithoutDetails ? 1 : 0);
  const advisoryAttentionCount = input.operatorStatus.advisoryWarningCount
    + runtimeWarnings
    + (input.runtimeNotices.retry ? 1 : 0)
    + (input.operatorStatusStale ? 1 : 0)
    + (input.connection === 'disconnected' ? 1 : 0);

  return {
    ...input,
    primaryRuntimeErrors,
    primaryAttentionCount,
    advisoryAttentionCount,
    tone: primaryAttentionCount > 0
      ? 'danger'
      : advisoryAttentionCount > 0
        ? 'advisory'
        : 'neutral',
  };
}
