import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { projectOperatorStatusPresentation } from '../src/focus/operatorWarningPresentation';
import { projectRuntimeDetailsPresentation } from '../src/focus/runtimeDetailsPresentation';
import type { FocusOperatorStatus, FocusOwner } from '../src/focus/types';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const owner: FocusOwner = {
  kind: 'none',
  holder_id: '',
  relation: 'none',
  label: 'No active writer',
};

function operatorStatus(
  attention: 'advisory' | 'correctness',
  severity: 'warning' | 'error' = 'warning',
): FocusOperatorStatus {
  return {
    status: 'degraded',
    observed_at: 1,
    poll_after_seconds: 15,
    warnings: [{
      code: `runtime-${attention}`,
      source: 'RuntimeLoop',
      message: 'bounded warning',
      severity,
      attention,
      first_seen_at: 1,
      last_seen_at: 2,
      occurrences: 1,
      details: {},
    }],
    runtime_loop: {},
  };
}

function presentation(
  status: FocusOperatorStatus | null,
  options: { disconnected?: boolean; stale?: boolean; runtimeError?: boolean } = {},
) {
  return projectRuntimeDetailsPresentation({
    instance: 'explorer',
    connection: options.disconnected ? 'disconnected' : 'connected',
    runtimeEpoch: 'epoch-1',
    revision: 7,
    owner,
    activeTurnContext: null,
    operatorStatus: projectOperatorStatusPresentation(status, options.stale ? 'stale' : 'fresh'),
    operatorStatusStale: options.stale ?? false,
    runtimeNotices: {
      retry: null,
      notices: options.runtimeError ? [{
        id: 'epoch-1:8',
        threadId: 'thread-1',
        method: 'error',
        message: 'runtime failed',
        additionalDetails: 'details',
        willRetry: false,
        turnId: 'turn-1',
      }] : [],
    },
  });
}

describe('Focus runtime-details presentation', () => {
  it('keeps an explicit advisory out of the primary conversation bucket', () => {
    const projected = presentation(operatorStatus('advisory'));

    expect(projected).toMatchObject({
      tone: 'advisory',
      primaryAttentionCount: 0,
      advisoryAttentionCount: 1,
    });
  });

  it('keeps correctness, operator errors, and non-retry runtime errors prominent', () => {
    expect(presentation(operatorStatus('correctness'))).toMatchObject({
      tone: 'danger',
      primaryAttentionCount: 1,
      advisoryAttentionCount: 0,
    });
    expect(presentation(operatorStatus('advisory', 'error'))).toMatchObject({
      tone: 'danger',
      primaryAttentionCount: 1,
    });
    expect(presentation(null, { runtimeError: true })).toMatchObject({
      tone: 'danger',
      primaryAttentionCount: 1,
    });
  });

  it('keeps disconnected and stale health discoverable without adding a primary notice', () => {
    expect(presentation(null, { disconnected: true, stale: true })).toMatchObject({
      tone: 'advisory',
      primaryAttentionCount: 0,
      advisoryAttentionCount: 2,
    });
  });

  it('wires one shared detail owner, compact mobile trigger, and a narrowed primary stream', () => {
    const app = source('../src/focus/FocusApp.vue');
    const detailPanel = source('../src/focus/FocusDetailPanel.vue');
    const runtimePanel = source('../src/focus/FocusRuntimeDetailsPanel.vue');
    const primary = source('../src/focus/FocusPrimaryNotices.vue');

    expect(app).toContain("selectDetail({ kind: 'runtimeDetails' })");
    expect(app).toContain('class="runtime-details-mobile-trigger"');
    expect(app).toContain('class="runtime-details-collapsed-trigger"');
    expect(app).toContain('class="runtime-details-entry"');
    expect(detailPanel).toContain("target === 'runtimeDetails'");
    expect(detailPanel).toContain('<FocusRuntimeDetailsPanel');
    expect(runtimePanel).toContain('<FocusRuntimeNotices');
    expect(runtimePanel).toContain('<FocusOperatorWarnings');
    expect(runtimePanel).toContain('presentation.activeTurnContext');
    expect(primary).toContain('primaryRuntimeErrors');
    expect(primary).toContain('primaryOperatorWarningCount');
    expect(primary).toContain('unknownSubmissionDrafts');
    expect(primary).not.toContain('activeTurnContext');
    expect(primary).not.toContain('operatorStatusStale');
    expect(primary).not.toContain('runtimeRetrying');
  });

  it('keeps the Focus shell below its reviewed source-size threshold', () => {
    const app = source('../src/focus/FocusApp.vue');
    expect(app.split('\n').length).toBeLessThan(1_500);
  });
});
