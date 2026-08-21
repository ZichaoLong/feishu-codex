import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import enFocus from '../src/i18n/locales/en/focus';
import zhFocus from '../src/i18n/locales/zh/focus';

const settingsDialog = readFileSync(
  fileURLToPath(new URL('../src/focus/FocusSettingsDialog.vue', import.meta.url)),
  'utf8',
);
const focusApp = readFileSync(
  fileURLToPath(new URL('../src/focus/FocusApp.vue', import.meta.url)),
  'utf8',
);
const primaryNotices = readFileSync(
  fileURLToPath(new URL('../src/focus/FocusPrimaryNotices.vue', import.meta.url)),
  'utf8',
);

describe('Focus backend reset settings surface', () => {
  it('refreshes its non-reserving preview on danger entry and reopen', () => {
    expect(settingsDialog).toContain(
      "const enteringDanger = value === 'danger' && section.value !== 'danger'",
    );
    expect(settingsDialog).toContain(
      "else if (!wasOpen && section.value === 'danger')",
    );
    expect(settingsDialog.match(/emit\('refreshBackendReset'\)/g)).toHaveLength(3);
  });

  it('passes only a captured typed preview into confirmation', () => {
    expect(settingsDialog).toContain(
      'confirmBackendReset: [preview: FocusBackendResetPreview]',
    );
    expect(settingsDialog).toContain("emit('confirmBackendReset', preview)");
    expect(settingsDialog).not.toContain('expected_connection_generation');
    expect(settingsDialog).not.toMatch(/emit\('confirmBackendReset',\s*\{/);
  });

  it('keeps unavailable and outcome-unknown states effect-free', () => {
    expect(settingsDialog).toContain("backendResetPreview.status === 'available'");
    expect(settingsDialog).toContain("backendResetPreview.status === 'force-only'");
    expect(settingsDialog).not.toContain("backendResetPreview.status === 'unavailable'");
    expect(settingsDialog).toContain(
      "v-if=\"!backendResetOutcomeUnknown && backendResetPreview.status === 'available'\"",
    );
    expect(settingsDialog).toContain(
      "v-else-if=\"!backendResetOutcomeUnknown && backendResetPreview.status === 'force-only'\"",
    );
  });

  it('projects only browser-safe reset result counts and warnings', () => {
    for (const field of [
      'detached_binding_count',
      'interrupted_binding_count',
      'retired_request_count',
      'purged_thread_count',
      'projection_warnings',
    ]) {
      expect(settingsDialog).toContain(field);
    }
    expect(settingsDialog).not.toContain('app_server_url');
    expect(settingsDialog).not.toContain('detached_binding_ids');
    expect(settingsDialog).not.toContain('purged_thread_ids');
  });

  it('does not pass backend-authored reason text through the localized surface', () => {
    expect(settingsDialog).not.toContain('reason_text');
  });

  it('keeps English and Chinese Focus keys symmetric', () => {
    expect(Object.keys(enFocus).sort()).toEqual(Object.keys(zhFocus).sort());
  });

  it('keeps confirmation copy complete and symmetric', () => {
    const keys = [
      'backendResetConfirmTitle',
      'backendResetConfirmSafeMessage',
      'backendResetConfirmForceMessage',
      'backendResetExecute',
      'backendResetForceExecute',
      'backendResetConfirmationLabel',
    ] as const;
    for (const key of keys) {
      expect(enFocus[key]).toBeTruthy();
      expect(zhFocus[key]).toBeTruthy();
    }

    const placeholders = [
      '{instance}',
      '{pending}',
      '{running}',
      '{attached}',
      '{activeThreads}',
      '{loadedThreads}',
    ];
    for (const key of [
      'backendResetConfirmSafeMessage',
      'backendResetConfirmForceMessage',
    ] as const) {
      for (const placeholder of placeholders) {
        expect(enFocus[key]).toContain(placeholder);
        expect(zhFocus[key]).toContain(placeholder);
      }
    }
  });

  it('uses the shared exact-instance confirmation and captured transaction action', () => {
    expect(focusApp).toContain('confirmationText: preview.instance');
    expect(focusApp).toContain('confirmationPlaceholder: preview.instance');
    expect(focusApp).toContain('await client.executeBackendReset(preview)');
    expect(focusApp).toContain("outcome.disposition === 'not-started'");
  });

  it('wires a persistent unknown banner without a retry or clear action', () => {
    expect(primaryNotices).toContain(
      '<Banner v-if="backendResetOutcomeUnknown" variant="danger">',
    );
    expect(focusApp).toContain(':backend-reset-outcome-unknown="client.backendResetOutcomeUnknown.value"');
    expect(focusApp).toContain('@refresh-backend-reset="client.refreshBackendReset"');
    expect(focusApp).toContain('@confirm-backend-reset="confirmBackendReset"');
    expect(focusApp).not.toContain('retryBackendReset');
    expect(focusApp).not.toContain('clearBackendReset');
  });
});
