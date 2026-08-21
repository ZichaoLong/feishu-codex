import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { projectOperatorStatusPresentation } from '../src/focus/operatorWarningPresentation';
import type { FocusOperatorStatus, FocusOperatorWarning } from '../src/focus/types';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

function warning(index: number): FocusOperatorWarning {
  return {
    code: `code-${index}-${'c'.repeat(120)}`,
    source: `source-${index}-${'s'.repeat(120)}`,
    message: `message-${index}-${'m'.repeat(520)}`,
    severity: index % 2 === 0 ? 'warning' : 'error',
    attention: index % 3 === 0 ? 'advisory' : 'correctness',
    first_seen_at: index + 1,
    last_seen_at: index + 2,
    occurrences: index + 1,
    details: {
      alpha: 'a'.repeat(220),
      beta: 2,
      gamma: true,
      delta: null,
      epsilon: 'visible',
      zeta: 6,
      nested: { secret: 'NESTED_SECRET_MUST_NOT_RENDER' },
      list: ['ARRAY_SECRET_MUST_NOT_RENDER'],
      non_finite: Number.POSITIVE_INFINITY,
    },
  };
}

describe('bounded operator warning presentation', () => {
  it('bounds rows and text while admitting only a few scalar detail fields', () => {
    const status: FocusOperatorStatus = {
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 15,
      warnings: Array.from({ length: 25 }, (_, index) => warning(index)),
      runtime_loop: { secret: 'RUNTIME_LOOP_SECRET_MUST_NOT_RENDER' },
    };

    const projected = projectOperatorStatusPresentation(status, 'fresh');

    expect(projected.warningCount).toBe(25);
    expect(projected.primaryWarningCount).toBe(20);
    expect(projected.advisoryWarningCount).toBe(5);
    expect(projected.errorWarningCount).toBe(12);
    expect(projected.warnings).toHaveLength(20);
    expect(projected.omittedWarningCount).toBe(5);
    expect(projected.warnings[0]!.code.length).toBeLessThanOrEqual(96);
    expect(projected.warnings[0]!.source.length).toBeLessThanOrEqual(96);
    expect(projected.warnings[0]!.message.length).toBeLessThanOrEqual(480);
    expect(projected.warnings[0]!.details).toHaveLength(6);
    expect(projected.warnings[0]!.detailsOmitted).toBe(true);
    expect(projected.warnings[0]!.details[0]).toEqual({
      key: 'alpha',
      value: `${'a'.repeat(159)}…`,
    });
    expect(JSON.stringify(projected)).not.toContain('NESTED_SECRET');
    expect(JSON.stringify(projected)).not.toContain('ARRAY_SECRET');
    expect(JSON.stringify(projected)).not.toContain('RUNTIME_LOOP_SECRET');
  });

  it('inspects only the bounded candidate set plus one own-field sentinel', () => {
    const accessed: string[] = [];
    const details: Record<string, unknown> = {};
    for (let index = 0; index < 40; index += 1) {
      const key = `detail_${index}`;
      Object.defineProperty(details, key, {
        enumerable: true,
        get() {
          accessed.push(key);
          return index;
        },
      });
    }
    const projected = projectOperatorStatusPresentation({
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 15,
      warnings: [{ ...warning(0), details }],
      runtime_loop: {},
    }, 'fresh');

    expect(projected.warnings[0]!.details).toHaveLength(6);
    expect(projected.warnings[0]!.detailsOmitted).toBe(true);
    expect(accessed).toEqual(Array.from({ length: 6 }, (_, index) => `detail_${index}`));
  });

  it('marks nested values inside the candidate set without projecting them', () => {
    const projected = projectOperatorStatusPresentation({
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 15,
      warnings: [{
        ...warning(0),
        details: { nested: { secret: 'MUST_NOT_RENDER' }, scalar: 'visible' },
      }],
      runtime_loop: {},
    }, 'fresh');

    expect(projected.warnings[0]!.details).toEqual([{ key: 'scalar', value: 'visible' }]);
    expect(projected.warnings[0]!.detailsOmitted).toBe(true);
    expect(JSON.stringify(projected)).not.toContain('MUST_NOT_RENDER');
  });

  it('keeps stale and degraded-without-details semantics explicit', () => {
    const status: FocusOperatorStatus = {
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 15,
      warnings: [warning(0)],
      runtime_loop: {},
    };

    expect(projectOperatorStatusPresentation(status, 'stale')).toMatchObject({
      warningCount: 1,
      warningsAreLastKnown: true,
      degradedWithoutDetails: false,
    });
    expect(projectOperatorStatusPresentation({ ...status, warnings: [] }, 'fresh')).toMatchObject({
      warningCount: 0,
      warningsAreLastKnown: false,
      degradedWithoutDetails: true,
    });
  });

  it('keeps only explicit non-error advisories out of the primary bucket', () => {
    const status: FocusOperatorStatus = {
      status: 'degraded',
      observed_at: 1,
      poll_after_seconds: 15,
      warnings: [
        { ...warning(0), severity: 'warning', attention: 'advisory' },
        { ...warning(1), severity: 'warning', attention: 'correctness' },
        { ...warning(2), severity: 'error', attention: 'advisory' },
      ],
      runtime_loop: {},
    };

    expect(projectOperatorStatusPresentation(status, 'fresh')).toMatchObject({
      warningCount: 3,
      primaryWarningCount: 2,
      advisoryWarningCount: 1,
      errorWarningCount: 1,
    });
  });

  it('keeps the disclosure text-only and outside the polling owner', () => {
    const component = source('../src/focus/FocusOperatorWarnings.vue');
    const details = source('../src/focus/FocusRuntimeDetailsPanel.vue');
    const projector = source('../src/focus/operatorWarningPresentation.ts');

    expect(component).not.toContain('v-html');
    expect(component).not.toContain('JSON.stringify');
    expect(component).not.toContain('runtime_loop');
    expect(component).not.toMatch(/Markdown|setTimeout|setInterval|operatorStatus\(/);
    expect(projector).not.toContain('runtime_loop');
    expect(projector).not.toMatch(/Object\.(?:entries|keys|values)\(|Reflect\.ownKeys\(/);
    expect(projector).toContain('for (const key in warning.details)');
    expect(details).toContain("import FocusOperatorWarnings from './FocusOperatorWarnings.vue'");
    expect(details).toContain(':presentation="presentation.operatorStatus"');
  });
});
