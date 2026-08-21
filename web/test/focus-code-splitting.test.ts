import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const focusApp = source('../src/focus/FocusApp.vue');
const focusDetailPanel = source('../src/focus/FocusDetailPanel.vue');
const lazyMarkdown = source('../src/components/chat/LazyMarkdown.vue');

describe('Focus optional-surface code splitting', () => {
  it('keeps the detail-panel family behind one dynamic import', () => {
    expect(focusApp).toContain(
      "detailPanelLoadPromise = import('./FocusDetailPanel.vue')",
    );
    expect(focusApp).not.toMatch(/import\s+FocusDetailPanel\s+from/);
    expect(focusApp).toContain(':is="focusDetailPanel"');
    expect(focusApp).toContain("detailPanelLoadState === 'error'");
    expect(focusApp).toContain('@click="closeDetail"');
    expect(focusApp).toContain('@click="client.reloadDocument"');
    expect(focusApp).toContain("t('focus.reloadPage')");
    expect(focusApp).not.toContain('@click="ensureDetailPanelLoaded"');

    for (const component of [
      'AgentDetailPanel',
      'ThinkingPanel',
      'ToolDiffPanel',
      'FocusMediaPanel',
    ]) {
      expect(focusApp).not.toMatch(new RegExp(`import\\s+${component}\\s+from`));
      expect(focusDetailPanel).toMatch(new RegExp(`import\\s+${component}\\s+from`));
    }
  });

  it('keeps rich Markdown behind a readable async boundary', () => {
    expect(lazyMarkdown).toContain("markdownLoad = import('./Markdown.vue')");
    expect(lazyMarkdown).not.toMatch(/import\s+Markdown\s+from\s+['"]\.\/Markdown\.vue['"]/);
    expect(lazyMarkdown).toContain('<pre>{{ text }}</pre>');
    expect(lazyMarkdown).toContain("loadState === 'error'");
    expect(lazyMarkdown).toContain("t('focus.reloadPage')");

    for (const relativePath of [
      '../src/components/chat/ChatPane.vue',
      '../src/components/chat/QuestionCard.vue',
      '../src/components/chat/ApprovalCard.vue',
    ]) {
      const consumer = source(relativePath);
      expect(consumer).toMatch(/import\s+Markdown\s+from\s+['"].*LazyMarkdown\.vue['"]/);
      expect(consumer).not.toMatch(/import\s+Markdown\s+from\s+['"].*\/Markdown\.vue['"]/);
    }
  });
});
