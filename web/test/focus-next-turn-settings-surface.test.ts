import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus Web next-turn settings surface', () => {
  it('labels active-turn quick and full model controls as next-turn settings', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const picker = source('../src/components/settings/ModelPicker.vue');
    const settingsDialog = source('../src/focus/FocusSettingsDialog.vue');
    const en = source('../src/i18n/locales/en/focus.ts');
    const zh = source('../src/i18n/locales/zh/focus.ts');

    expect(app).toContain("client.running.value ? t('focus.nextTurnSettings') : ''");
    expect(app).toContain(':composer-model-settings-hint="activeNextTurnSettingsHint"');
    expect(app).toContain(':settings-hint="activeNextTurnSettingsHint"');
    expect(pane).toContain(':model-settings-hint="composerModelSettingsHint"');
    expect(pane).toContain(':composer-model-settings-hint="composerModelSettingsHint"');
    expect(dock).toContain(':model-settings-hint="composerModelSettingsHint"');
    expect(composer).toContain('v-if="modelSettingsHint"');
    expect(picker).toContain('v-if="settingsHint"');
    expect(settingsDialog).toContain("t('focus.nextTurnSettings')");
    expect(settingsDialog).not.toContain('running: boolean;');
    expect(en).toContain('nextTurnSettings:');
    expect(zh).toContain('nextTurnSettings:');
  });
});
