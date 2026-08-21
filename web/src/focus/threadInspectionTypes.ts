import type { FocusWebWireEnum } from './focusWire.generated';

export type FocusThreadToolKind = FocusWebWireEnum<'thread_tool_kind'>;

/** Browser-local reason why an inspection surface cannot issue a request. */
export type FocusThreadInspectionUnavailableReason =
  | 'build_unsupported'
  | 'document_unavailable'
  | 'legacy_history'
  | 'no_active_thread'
  | 'runtime_unsupported'
  | 'thread_not_materialized'
  | 'unknown_history';

/** Exact upstream source identity for one inspectable semantic tool card. */
export interface FocusToolInspectionLocator {
  turn_id: string;
  item_id: string;
  kind: FocusThreadToolKind;
  change_index: number | null;
}
