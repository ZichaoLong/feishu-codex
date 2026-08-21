import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus Web turn-window settings surface', () => {
  it('offers only 5, 10, and 20 and wires the browser-local owner', () => {
    const app = source('../src/focus/FocusApp.vue');
    const dialog = source('../src/focus/FocusSettingsDialog.vue');
    const en = source('../src/i18n/locales/en/focus.ts');
    const zh = source('../src/i18n/locales/zh/focus.ts');

    expect(dialog).toContain('turnWindowLimit: number;');
    expect(dialog).toContain('setTurnWindowLimit: [value: number]');
    expect(dialog.match(/<option value="(?:5|10|20)">/gu)).toHaveLength(3);
    expect(dialog).toContain('<option value="5">5</option>');
    expect(dialog).toContain('<option value="10">10</option>');
    expect(dialog).toContain('<option value="20">20</option>');
    expect(app).toContain(':turn-window-limit="client.turnWindowLimit.value"');
    expect(app).toContain('@set-turn-window-limit="client.setTurnWindowLimit($event)"');
    expect(en).toContain('turnWindowDescription:');
    expect(zh).toContain('turnWindowDescription:');
  });
});
