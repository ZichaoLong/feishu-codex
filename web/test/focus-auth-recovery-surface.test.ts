import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import enFocus from '../src/i18n/locales/en/focus';
import zhFocus from '../src/i18n/locales/zh/focus';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const focusApp = source('../src/focus/FocusApp.vue');
const focusClient = source('../src/focus/useFocusWebClient.ts');
const authStart = focusApp.indexOf('<section v-if="client.authRequired.value"');
const loadFailureStart = focusApp.indexOf(
  '<section v-else-if="client.initialized.value && !client.meta.value"',
);
const appStart = focusApp.indexOf('<div\n      v-else\n      class="focus-app"', loadFailureStart);
const authSurface = focusApp.slice(authStart, loadFailureStart);
const loadFailureSurface = focusApp.slice(loadFailureStart, appStart);

describe('Focus authentication recovery surface', () => {
  it('reloads only the stale authenticated document while ordinary load failures retry in place', () => {
    expect(authStart).toBeGreaterThan(-1);
    expect(loadFailureStart).toBeGreaterThan(authStart);
    expect(appStart).toBeGreaterThan(loadFailureStart);
    expect(authSurface).toContain('@click="client.reloadDocument"');
    expect(authSurface).toContain("t('focus.reloadPage')");
    expect(authSurface).not.toContain('@click="client.load"');
    expect(authSurface).not.toContain("t('focus.retry')");
    expect(loadFailureSurface).toContain('@click="client.load"');
    expect(loadFailureSurface).toContain("t('focus.retry')");
    expect(focusClient).toMatch(
      /function reloadDocument\(\): void \{\s*window\.location\.reload\(\);\s*\}/u,
    );
  });

  it('keeps external and local recovery instructions explicit without promising replay', () => {
    expect(enFocus.authTitle).toBe('Focus Web needs to re-authenticate');
    expect(enFocus.authMessage).toContain('trusted proxy');
    expect(enFocus.authMessage).toContain('refresh this page or reopen its bookmark');
    expect(enFocus.authMessage).toContain('focusctl [--instance <name>] web open');
    expect(enFocus.authMessage).toContain('will not replay');

    expect(zhFocus.authTitle).toBe('Focus Web 需要重新认证');
    expect(zhFocus.authMessage).toContain('trusted proxy');
    expect(zhFocus.authMessage).toContain('刷新页面或重新打开书签');
    expect(zhFocus.authMessage).toContain('focusctl [--instance <name>] web open');
    expect(zhFocus.authMessage).toContain('不会自动重放');
  });
});
