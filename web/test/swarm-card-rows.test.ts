import { createSSRApp, h } from 'vue';
import { createI18n } from 'vue-i18n';
import { renderToString } from '@vue/server-renderer';
import { describe, expect, it } from 'vitest';
import type { ToolCall } from '../src/types';
import SwarmTool from '../src/components/chat/tool-calls/SwarmTool.vue';
import type { SwarmResult } from '../src/lib/parseSwarmResult';
import {
  buildSwarmCardRows,
  buildSwarmCardRowWindow,
  SWARM_HEAD_ROW_COUNT,
  SWARM_TAIL_ROW_COUNT,
} from '../src/lib/swarmCardRows';

function result(subagents: SwarmResult['subagents']): SwarmResult {
  return {
    summary: `${subagents.length}`,
    completed: subagents.filter((subagent) => subagent.outcome === 'completed').length,
    failed: subagents.filter((subagent) => subagent.outcome === 'failed').length,
    aborted: subagents.filter((subagent) => subagent.outcome === 'aborted').length,
    total: subagents.length,
    subagents,
  };
}

function structuredOutput(count: number, body: (index: number) => string = () => ''): string[] {
  return [
    '<agent_swarm_result>',
    `<summary>completed: ${count}</summary>`,
    ...Array.from(
      { length: count },
      (_, index) => (
        `<subagent item="row-${index}" agent_id="agent-${index}" outcome="completed">`
        + `${body(index)}</subagent>`
      ),
    ),
    '</agent_swarm_result>',
  ];
}

function swarmApp(output: string[], defaultExpanded: boolean) {
  const tool: ToolCall = {
    id: 'parent-history-swarm',
    name: 'AgentSwarm',
    arg: '',
    status: 'ok',
    output,
    defaultExpanded,
  };
  const app = createSSRApp({ render: () => h(SwarmTool, { tool }) });
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        tools: {
          label: { swarm: 'Swarm' },
          output: { linesOmitted: '{count} lines omitted from this browser view.' },
          swarm: {
            progress: '{done} / {total}',
            runningSub: '{count} in progress',
            doneSub: '{completed} completed · {failed} failed',
            phaseQueued: 'Queued',
            phaseWorking: 'Working',
            phaseSuspended: 'Suspended',
            phaseCompleted: 'Completed',
            phaseFailed: 'Failed',
            waiting: 'Waiting',
            membersOmitted: '{count} members omitted from this browser view.',
          },
        },
      },
    },
  }));
  return app;
}

describe('buildSwarmCardRows', () => {
  it('projects only the parent-history result rows', () => {
    const rows = buildSwarmCardRows(result([
      { outcome: 'completed', item: 'A', agentId: 'a1', body: 'A body' },
      { outcome: 'failed', item: 'B', body: 'B body' },
      { outcome: 'aborted', item: 'C', state: 'not_started', body: 'C never started' },
    ]));

    expect(rows).toEqual([
      { id: 'a1', name: 'A', activity: 'A body', phase: 'completed', body: 'A body' },
      { id: 'B', name: 'B', activity: 'B body', phase: 'failed', body: 'B body' },
      {
        id: 'C',
        name: 'C',
        activity: 'C never started',
        phase: 'failed',
        body: 'C never started',
      },
    ]);
  });

  it('returns no rows without a structured parent-history result', () => {
    expect(buildSwarmCardRows(null)).toEqual([]);
  });
});

describe('buildSwarmCardRowWindow', () => {
  it('keeps only a fixed head and tail of one hundred thousand rows', () => {
    const rows = Array.from({ length: 100_000 }, (_, index) => ({
      id: `row-${index}`,
      name: `row-${index}`,
      activity: '',
      phase: 'completed' as const,
      body: '',
    }));
    const window = buildSwarmCardRowWindow(rows);

    expect(window.head).toHaveLength(SWARM_HEAD_ROW_COUNT);
    expect(window.tail).toHaveLength(SWARM_TAIL_ROW_COUNT);
    expect(window.omittedRowCount).toBe(99_950);
    expect(window.head[0]?.id).toBe('row-0');
    expect(window.head.at(-1)?.id).toBe('row-24');
    expect(window.tail[0]?.id).toBe('row-99975');
    expect(window.tail.at(-1)?.id).toBe('row-99999');
  });

  it('does not split a list at or below the fixed window', () => {
    const rows = Array.from({ length: 50 }, (_, index) => ({
      id: `row-${index}`,
      name: `row-${index}`,
      activity: '',
      phase: 'queued' as const,
      body: '',
    }));

    expect(buildSwarmCardRowWindow(rows)).toEqual({
      head: rows,
      tail: [],
      omittedRowCount: 0,
    });
  });
});

describe('SwarmTool parent-history presentation window', () => {
  it('mounts only 25 head and 25 tail rows while counting the complete result', async () => {
    const html = await renderToString(swarmApp(structuredOutput(100), true));

    expect(html.match(/class="phase-completed member"/g)).toHaveLength(50);
    expect(html.match(/class="member-omission"/g)).toHaveLength(1);
    expect(html).toContain('100 / 100');
    expect(html).toContain('50 members omitted from this browser view.');
    expect(html).toContain('row-0');
    expect(html).toContain('row-24');
    expect(html).not.toContain('row-25');
    expect(html).not.toContain('row-74');
    expect(html).toContain('row-75');
    expect(html).toContain('row-99');
    expect(html).not.toContain('class="member-body');
    expect(html).not.toContain('class="tool-output-block');
  });

  it('does not mount the card body while the settled result is folded', async () => {
    const html = await renderToString(swarmApp(structuredOutput(100), false));

    expect(html).toContain('100 / 100');
    expect(html).not.toContain('class="body"');
    expect(html).not.toContain('class="phase-completed member"');
    expect(html).not.toContain('class="member-omission"');
    expect(html).not.toContain('class="member-body');
  });
});
