import { panelMaxWidth } from '../composables/useViewportWidth';
import type { ChatTurn, ToolMedia } from '../types';
import type { FocusToolInspectionLocator } from './threadInspectionTypes';

export const FOCUS_DETAIL_PANEL_DEFAULT = 440;
export const FOCUS_DETAIL_PANEL_MIN = 320;
export const FOCUS_DETAIL_CONVERSATION_RESERVE = 320;

export type FocusDetailSelection =
  | { kind: 'runtimeDetails' }
  | { kind: 'thinking'; turnId: string; blockIndex: number; itemId?: string }
  | {
      kind: 'toolDiff';
      toolId: string;
      inspectionLocator?: FocusToolInspectionLocator;
    }
  | { kind: 'conversationSearch' }
  | { kind: 'media'; media: ToolMedia }
  | { kind: 'agent'; taskId: string };

function isSameDetailSelection(
  current: FocusDetailSelection,
  requested: FocusDetailSelection,
): boolean {
  if (current.kind !== requested.kind) return false;
  if (current.kind === 'runtimeDetails' && requested.kind === 'runtimeDetails') {
    return true;
  }
  if (current.kind === 'thinking' && requested.kind === 'thinking') {
    if (current.turnId !== requested.turnId) return false;
    if (current.itemId || requested.itemId) {
      return current.itemId !== undefined
        && current.itemId === requested.itemId;
    }
    return current.blockIndex === requested.blockIndex;
  }
  if (current.kind === 'toolDiff' && requested.kind === 'toolDiff') {
    if (current.toolId !== requested.toolId) return false;
    const left = current.inspectionLocator;
    const right = requested.inspectionLocator;
    if (!left && !right) return true;
    return Boolean(
      left
      && right
      && left.turn_id === right.turn_id
      && left.item_id === right.item_id
      && left.kind === right.kind
      && left.change_index === right.change_index
    );
  }
  if (current.kind === 'conversationSearch' && requested.kind === 'conversationSearch') {
    return true;
  }
  if (current.kind === 'agent' && requested.kind === 'agent') {
    return current.taskId === requested.taskId;
  }
  if (current.kind === 'media' && requested.kind === 'media') {
    if (current.media.fileId || requested.media.fileId) {
      return current.media.fileId !== undefined
        && current.media.fileId === requested.media.fileId;
    }
    return current.media.kind === requested.media.kind
      && current.media.url === requested.media.url;
  }
  return false;
}

/** Selecting the exact visible target closes it; any other target replaces it. */
export function nextFocusDetailSelection(
  current: FocusDetailSelection | null,
  requested: FocusDetailSelection,
): FocusDetailSelection | null {
  return current && isSameDetailSelection(current, requested)
    ? null
    : requested;
}

/** Resolve an open thinking target without silently following array reorder. */
export function resolveFocusThinkingText(
  selection: FocusDetailSelection | null,
  turns: readonly ChatTurn[],
): string | null {
  if (selection?.kind !== 'thinking') return null;
  const turn = turns.find((candidate) => candidate.id === selection.turnId);
  if (!turn) return null;
  let block: NonNullable<ChatTurn['blocks']>[number] | undefined;
  if (selection.itemId) {
    const matches = turn.blocks?.filter((candidate) => (
      candidate.kind === 'thinking' && candidate.itemId === selection.itemId
    )) ?? [];
    if (matches.length !== 1) return null;
    [block] = matches;
  } else {
    block = turn.blocks?.[selection.blockIndex];
  }
  return block?.kind === 'thinking' ? block.thinking : null;
}

/** Use the full-screen presentation when three usable columns cannot coexist. */
export function shouldUseFocusDetailFullscreen(
  viewportWidth: number,
  sidebarWidth: number,
): boolean {
  return viewportWidth < Math.max(0, sidebarWidth)
    + FOCUS_DETAIL_PANEL_MIN
    + FOCUS_DETAIL_CONVERSATION_RESERVE;
}

/** Cap a desktop panel after the caller proves the three columns can coexist. */
export function focusDetailPanelMaxWidth(
  viewportWidth: number,
  sidebarWidth: number,
): number {
  const available = Math.max(0, viewportWidth - Math.max(0, sidebarWidth));
  return panelMaxWidth(
    available,
    FOCUS_DETAIL_PANEL_MIN,
    FOCUS_DETAIL_CONVERSATION_RESERVE,
  );
}
