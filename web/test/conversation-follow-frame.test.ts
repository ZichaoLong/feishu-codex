import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  createConversationFollowFrame,
  type ConversationFollowMode,
} from '../src/focus/conversationFollowFrame';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

function createFrameHarness(follow: (mode: ConversationFollowMode) => void) {
  let nextId = 1;
  const callbacks = new Map<number, () => void>();
  const cancelled: number[] = [];
  const owner = createConversationFollowFrame(follow, {
    requestFrame(callback) {
      const id = nextId++;
      callbacks.set(id, callback);
      return id;
    },
    cancelFrame(id) {
      cancelled.push(id);
      callbacks.delete(id);
    },
  });
  return {
    owner,
    callbacks,
    cancelled,
    runNext() {
      const entry = callbacks.entries().next().value as [number, () => void] | undefined;
      expect(entry).toBeDefined();
      callbacks.delete(entry![0]);
      entry![1]();
    },
  };
}

describe('conversation ordinary follow frame', () => {
  it('coalesces a burst into one frame and preserves smooth dominance', () => {
    const modes: ConversationFollowMode[] = [];
    const frame = createFrameHarness((mode) => modes.push(mode));

    frame.owner.request('instant');
    frame.owner.request('smooth');
    frame.owner.request('instant');

    expect(frame.callbacks).toHaveLength(1);
    frame.runNext();
    expect(modes).toEqual(['smooth']);
  });

  it('allows a callback to request a distinct following frame', () => {
    const modes: ConversationFollowMode[] = [];
    const frame = createFrameHarness((mode) => {
      modes.push(mode);
      if (modes.length === 1) frame.owner.request('instant');
    });

    frame.owner.request('instant');
    frame.runNext();
    expect(frame.callbacks).toHaveLength(1);
    frame.runNext();
    expect(modes).toEqual(['instant', 'instant']);
  });

  it('suppresses a cancelled late callback without clearing a newer frame', () => {
    const modes: ConversationFollowMode[] = [];
    const frame = createFrameHarness((mode) => modes.push(mode));
    frame.owner.request('instant');
    const [cancelledId, lateCallback] = frame.callbacks.entries().next().value as [number, () => void];

    frame.owner.cancel();
    frame.owner.request('smooth');
    lateCallback();

    expect(frame.cancelled).toEqual([cancelledId]);
    expect(modes).toEqual([]);
    expect(frame.callbacks).toHaveLength(1);
    frame.runNext();
    expect(modes).toEqual(['smooth']);
  });

  it('routes watcher and observer requests through the shared owner', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const watcher = pane.slice(
      pane.indexOf('watch(scrollKey,'),
      pane.indexOf('watch(dockRef,', pane.indexOf('watch(scrollKey,')),
    );

    expect(pane).toContain('createConversationFollowFrame((mode) => {');
    expect(watcher).toContain("scheduleFollow(next.length < prev.length ? 'smooth' : 'instant');");
    expect(watcher).not.toContain('scrollToBottom(');
    expect(pane).toContain('ordinaryFollowFrame.request(mode);');
    expect(pane).toContain('ordinaryFollowFrame.cancel();');
    expect(pane).toContain('function onContentMutated(): void {');
    expect(pane).toContain('function onVisualViewportResize(): void {');
    expect(pane).toContain('let stableFollowRaf = 0;');
    expect(pane).not.toContain('scrollRaf');
  });

  it('keeps no unused root dock-height measurement path', () => {
    const dock = source('../src/components/chat/ChatDock.vue');
    const style = source('../src/style.css');
    const pane = source('../src/components/chat/ConversationPane.vue');

    expect(dock).not.toContain('dockResizeObserver');
    expect(dock).not.toContain('publishDockHeight');
    expect(dock).not.toContain('ref="dockRef"');
    expect(style).not.toContain('--dock-h');
    expect(pane).toContain('dockHeight.value = dockRef.value?.offsetHeight ?? 0;');
  });
});
