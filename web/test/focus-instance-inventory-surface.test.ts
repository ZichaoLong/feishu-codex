import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus managed-instance inventory surface', () => {
  it('shows an explicit unknown loaded state on desktop and mobile', () => {
    const desktop = source('../src/components/SessionRow.vue');
    const mobile = source('../src/components/mobile/MobileSwitcherSheet.vue');
    const en = source('../src/i18n/locales/en/focus.ts');
    const zh = source('../src/i18n/locales/zh/focus.ts');

    expect(desktop).toContain("runtimeState === 'unknown'");
    expect(desktop).toContain("t('focus.loadedStateUnknown')");
    expect(mobile).toContain("runtimeState === 'unknown'");
    expect(mobile).toContain("t('focus.loadedStateUnknown')");
    expect(en).toContain("loadedStateUnknown: 'Loaded state unknown'");
    expect(zh).toContain("loadedStateUnknown: '加载状态未知'");
  });
});
