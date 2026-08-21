import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const composer = readFileSync(
  fileURLToPath(new URL('../src/components/chat/Composer.vue', import.meta.url)),
  'utf8',
);

describe('Composer context usage surface', () => {
  it('hides an unavailable ring while keeping manual compact reachable', () => {
    expect(composer).toContain('v-if="status && !hideContext && hasContextUsage"');
    expect(composer).toContain(
      'const showCompact = computed(() => !hasContextUsage.value || pct.value >= 80);',
    );
    expect(composer).toContain(
      '<button v-if="capabilities.compact && showCompact" class="compact-chip"',
    );
  });

  it('draws used percent from the projected remaining percent', () => {
    expect(composer).toContain('100 - (props.status?.ctxRemainingPct ?? 100)');
  });
});
