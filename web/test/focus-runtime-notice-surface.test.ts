import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus runtime notice surface', () => {
  it('renders retry, warning, and error as their own bounded runtime surface', () => {
    const component = source('../src/focus/FocusRuntimeNotices.vue');
    expect(component).toContain("t('focus.runtimeRetrying')");
    expect(component).toContain("? 'focus.runtimeError'");
    expect(component).toContain(": 'focus.runtimeWarning'");
    expect(component).toContain('presentation.retry.additionalDetails');
    expect(component).toContain("notice.method === 'error' ? 'danger' : 'warning'");
  });

  it('moves retry and warning detail out of the primary conversation flow', () => {
    const app = source('../src/focus/FocusApp.vue');
    const details = source('../src/focus/FocusRuntimeDetailsPanel.vue');
    const primary = source('../src/focus/FocusPrimaryNotices.vue');

    expect(app).not.toContain('<FocusRuntimeNotices');
    expect(details).toContain('<FocusRuntimeNotices');
    expect(details).toContain(':presentation="presentation.runtimeNotices"');
    expect(details).toContain("presentation.connection === 'disconnected'");
    expect(primary).toContain('primaryRuntimeErrors');
    expect(primary).not.toContain('runtimeRetrying');
    expect(primary).not.toContain('runtimeWarning');
  });
});
