import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  FOCUS_DETAIL_PANEL_MIN,
  focusDetailPanelMaxWidth,
  nextFocusDetailSelection,
  resolveFocusThinkingText,
  shouldUseFocusDetailFullscreen,
  type FocusDetailSelection,
} from '../src/focus/focusDetailPanelState';
import { STORAGE_KEYS } from '../src/lib/storage';
import type { ChatTurn } from '../src/types';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus detail panel presentation state', () => {
  it.each<FocusDetailSelection>([
    { kind: 'runtimeDetails' },
    { kind: 'thinking', turnId: 'turn:a', blockIndex: 2, itemId: 'thinking:2' },
    { kind: 'toolDiff', toolId: 'tool:1' },
    { kind: 'conversationSearch' },
    { kind: 'agent', taskId: 'task:1' },
    { kind: 'media', media: { kind: 'image', url: '/media/a', fileId: 'file:1' } },
    { kind: 'media', media: { kind: 'image', url: '/media/no-file' } },
  ])('selecting the same exact $kind target closes it', (selection) => {
    expect(nextFocusDetailSelection(selection, structuredClone(selection))).toBeNull();
  });

  it('replaces a different target and compares media by authoritative identity', () => {
    const current: FocusDetailSelection = {
      kind: 'media',
      media: { kind: 'image', url: '/old-url', fileId: 'file:1' },
    };
    const sameFile: FocusDetailSelection = {
      kind: 'media',
      media: { kind: 'image', url: '/new-url', fileId: 'file:1' },
    };
    const next: FocusDetailSelection = { kind: 'toolDiff', toolId: 'tool:2' };

    expect(nextFocusDetailSelection(current, sameFile)).toBeNull();
    expect(nextFocusDetailSelection(current, next)).toBe(next);
  });

  it('distinguishes the same synthetic tool id by its exact inspection locator', () => {
    const current: FocusDetailSelection = {
      kind: 'toolDiff',
      toolId: 'synthetic-tool',
      inspectionLocator: {
        turn_id: 'turn-1',
        item_id: 'file-change',
        kind: 'fileChange',
        change_index: 0,
      },
    };
    const next: FocusDetailSelection = {
      ...current,
      inspectionLocator: {
        ...current.inspectionLocator!,
        change_index: 1,
      },
    };

    expect(nextFocusDetailSelection(current, structuredClone(current))).toBeNull();
    expect(nextFocusDetailSelection(current, next)).toBe(next);
  });

  it('uses thinking item identity across projection reorder and fails closed if it disappears', () => {
    const selection: FocusDetailSelection = {
      kind: 'thinking',
      turnId: 'turn:a',
      blockIndex: 0,
      itemId: 'thinking:stable',
    };
    const turn = (blocks: ChatTurn['blocks']): ChatTurn => ({
      id: 'turn:a',
      role: 'assistant',
      text: '',
      blocks,
    });

    expect(resolveFocusThinkingText(selection, [turn([
      { kind: 'text', itemId: 'answer', text: 'answer' },
      { kind: 'thinking', itemId: 'thinking:stable', thinking: 'stable plan' },
    ])])).toBe('stable plan');
    expect(resolveFocusThinkingText(selection, [turn([
      { kind: 'thinking', itemId: 'thinking:other', thinking: 'wrong plan' },
    ])])).toBeNull();
    expect(resolveFocusThinkingText(selection, [turn([
      { kind: 'thinking', itemId: 'thinking:stable', thinking: 'ambiguous one' },
      { kind: 'thinking', itemId: 'thinking:stable', thinking: 'ambiguous two' },
    ])])).toBeNull();
    expect(nextFocusDetailSelection(selection, {
      ...selection,
      blockIndex: 4,
    })).toBeNull();
  });

  it('uses the source index only when a thinking item has no identity', () => {
    const selection: FocusDetailSelection = {
      kind: 'thinking',
      turnId: 'turn:a',
      blockIndex: 1,
    };
    const turn: ChatTurn = {
      id: 'turn:a',
      role: 'assistant',
      text: '',
      blocks: [
        { kind: 'text', text: 'answer' },
        { kind: 'thinking', thinking: 'index plan' },
      ],
    };

    expect(resolveFocusThinkingText(selection, [turn])).toBe('index plan');
  });

  it('keeps a usable conversation reserve when clamping desktop width', () => {
    expect(focusDetailPanelMaxWidth(1_200, 270)).toBe(610);
    expect(focusDetailPanelMaxWidth(500, 270)).toBe(FOCUS_DETAIL_PANEL_MIN);
    expect(focusDetailPanelMaxWidth(1_200, -10)).toBe(880);
  });

  it('uses full-screen detail when a narrow desktop cannot keep all three columns usable', () => {
    expect(shouldUseFocusDetailFullscreen(809, 170)).toBe(true);
    expect(shouldUseFocusDetailFullscreen(810, 170)).toBe(false);
    expect(shouldUseFocusDetailFullscreen(640, 0)).toBe(false);
  });

  it('wires one desktop-only reverse handle and fail-closed live targets', () => {
    const app = source('../src/focus/FocusApp.vue');

    expect(STORAGE_KEYS.detailPanelWidth).toBe('focus-web.detail-panel-width');
    expect(app).toContain('grid-template-columns: auto 0 minmax(0, 1fr) 0 auto');
    expect(app).toContain('v-if="detailOpen && !detailPanelFullscreen"');
    expect(app).toContain(':storage-key="STORAGE_KEYS.detailPanelWidth"');
    expect(app).toMatch(/<ResizeHandle[\s\S]*?\breverse\b[\s\S]*?class="detail-handle"|<ResizeHandle[\s\S]*?class="detail-handle"[\s\S]*?\breverse\b/);
    expect(app).toContain("detailPanelFullscreen.value ? undefined : { width: `${visibleDetailPanelWidth.value}px` }");
    expect(app).toContain(':class="{ fullscreen: detailPanelFullscreen }"');
    expect(app).toContain('watch(detailPayloadAvailable');
    expect(app).toContain('resolveFocusThinkingText(');
    expect(app).toContain(`watch(client.activeThreadId, (threadId) => {
  closeDetail();`);
    expect(app).toContain("detailPanelLoadPromise = import('./FocusDetailPanel.vue')");
    expect(app).toContain("selectDetail({ kind: 'runtimeDetails' })");
    expect(app).toContain(':runtime-details-presentation="runtimeDetailsPresentation"');
  });
});
