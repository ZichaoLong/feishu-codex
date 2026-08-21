import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { OutputBundle } from 'vite';
import { describe, expect, it } from 'vitest';
import { focusBundleTransportViolations } from '../scripts/focus-bundle-boundary';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus browser transport boundary', () => {
  it('keeps shared media paths dependent on an explicit loader', () => {
    for (const code of [
      source('../src/lib/authenticatedMedia.ts'),
      source('../src/composables/useAttachmentUpload.ts'),
    ]) {
      expect(code).not.toContain('getKimiWebApi');
      expect(code).not.toMatch(/from\s+['"]\.\.\/api['"]/);
    }
  });

  it('rejects daemon modules from any generated Focus chunk', () => {
    const bundle = {
      'index.js': {
        type: 'chunk',
        modules: {
          '/repo/web/src/focus/api.ts': {},
          '/repo/web/src/api/daemon/client.ts': {},
        },
      },
    } as unknown as OutputBundle;

    expect(focusBundleTransportViolations(bundle)).toEqual([
      '/repo/web/src/api/daemon/client.ts',
    ]);
  });
});
