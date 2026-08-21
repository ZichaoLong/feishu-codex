import { createSSRApp, defineComponent, h, type Component } from 'vue';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createI18n } from 'vue-i18n';
import { renderToString } from '@vue/server-renderer';
import { describe, expect, it } from 'vitest';
import type { ToolCall } from '../src/types';
import ToolRow from '../src/components/chat/ToolRow.vue';
import ToolDiffPanel from '../src/components/chat/ToolDiffPanel.vue';
import AgentTool from '../src/components/chat/tool-calls/AgentTool.vue';
import AskUserTool from '../src/components/chat/tool-calls/AskUserTool.vue';
import EditTool from '../src/components/chat/tool-calls/EditTool.vue';
import GenericTool from '../src/components/chat/tool-calls/GenericTool.vue';
import SwarmTool from '../src/components/chat/tool-calls/SwarmTool.vue';
import {
  buildDiffLineWindow,
  DIFF_HEAD_LINE_COUNT,
  DIFF_TAIL_LINE_COUNT,
} from '../src/components/chat/DiffLines.vue';
import ToolOutputBlock, {
  buildToolOutputLineWindow,
  TOOL_OUTPUT_HEAD_LINE_COUNT,
  TOOL_OUTPUT_TAIL_LINE_COUNT,
} from '../src/components/chat/tool-calls/ToolOutputBlock.vue';
import FocusToolSourceDetailPanel, {
  formatFullToolDetailSource,
} from '../src/focus/FocusToolSourceDetailPanel.vue';
import FocusCompleteDiff, {
  parseCompleteFileChangeDiff,
  parseCompleteUnifiedDiff,
} from '../src/focus/FocusCompleteDiff.vue';
import type {
  FocusCommandExecutionSourceDetail,
  FocusFileChangeSourceDetail,
} from '../src/focus/types';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

function outputApp(lines: string[], omittedChars = 0, headLineCount = 0) {
  const app = createSSRApp({
    render: () => h(ToolOutputBlock, { lines, omittedChars, headLineCount }),
  });
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        tools: {
          output: {
            linesOmitted: '{count} lines omitted from this browser view.',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'Page budget omitted all {count} characters',
          },
        },
        thinking: { close: 'Close' },
        diff: { noDiff: 'No line changes for this file' },
      },
    },
  }));
  return app;
}

const FULLY_OMITTED_CHARS = 90_000;
const FULLY_OMITTED_MARKER =
  `[Focus Web omitted ${FULLY_OMITTED_CHARS} characters of tool output; showing a bounded head and tail.]`;

function fullyOmittedConsumerApp(component: Component, toolName: string) {
  const tool: ToolCall = {
    id: `fully-omitted-${toolName}`,
    name: toolName,
    arg: '',
    status: 'ok',
    output: [],
    outputTruncated: true,
    outputOmittedChars: FULLY_OMITTED_CHARS,
    outputHeadLineCount: 0,
    defaultExpanded: true,
  };
  const app = createSSRApp({
    render: () => h(component, { tool }),
  });
  app.provide('resolveSwarmMembers', () => []);
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        tasks: { openDetail: 'Open detail' },
        tools: {
          output: {
            linesOmitted: '{count} lines omitted from this browser view.',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'Page budget omitted all {count} characters',
          },
          swarm: {
            progress: '{done} / {total}',
            runningSub: '{count} running',
            doneSub: '{completed} completed, {failed} failed',
            waiting: 'SWARM_WAITING_SENTINEL',
            phaseCompleted: 'Completed',
            phaseWorking: 'Working',
            phaseSuspended: 'Suspended',
            phaseFailed: 'Failed',
            phaseQueued: 'Queued',
          },
        },
        thinking: { close: 'Close' },
        diff: { noDiff: 'NO_DIFF_SENTINEL' },
      },
    },
  }));
  return app;
}

describe('bounded tool detail DOM', () => {
  it('routes every specialized raw-output surface through fixed windows', () => {
    for (const component of [
      '../src/components/chat/tool-calls/AgentTool.vue',
      '../src/components/chat/tool-calls/AskUserTool.vue',
      '../src/components/chat/ToolDiffPanel.vue',
      '../src/components/chat/tool-calls/SwarmTool.vue',
    ]) {
      expect(source(component)).toContain('ToolOutputBlock');
    }
    const swarm = source('../src/components/chat/tool-calls/SwarmTool.vue');
    expect(swarm).toContain('buildSwarmCardRowWindow');
    expect(swarm).toContain('<div v-if="open" class="body">');
    expect(swarm).not.toContain('<div v-show="open" class="body">');
    expect(swarm).not.toContain('v-show="isRowOpen');
  });

  it('does not mount ToolRow default-slot details while folded', async () => {
    let detailSetupCount = 0;
    const DetailProbe = defineComponent({
      setup() {
        detailSetupCount += 1;
        return () => h('span', { class: 'detail-probe' }, 'mounted detail');
      },
    });
    const renderRow = (open: boolean) => renderToString(createSSRApp({
      render: () => h(
        ToolRow,
        { status: 'ok', name: 'Run', open, expandable: true },
        { default: () => h(DetailProbe) },
      ),
    }));

    const folded = await renderRow(false);
    expect(detailSetupCount).toBe(0);
    expect(folded).not.toContain('mounted detail');

    const expanded = await renderRow(true);
    expect(detailSetupCount).toBe(1);
    expect(expanded).toContain('mounted detail');
  });

  it('renders a fixed head and tail for one hundred thousand lines', async () => {
    const lines = Array.from(
      { length: 100_000 },
      (_, index) => `line-${index.toString().padStart(6, '0')}`,
    );
    const window = buildToolOutputLineWindow(lines);

    expect(window.head).toHaveLength(TOOL_OUTPUT_HEAD_LINE_COUNT);
    expect(window.tail).toHaveLength(TOOL_OUTPUT_TAIL_LINE_COUNT);
    expect(window.head.at(0)).toBe('line-000000');
    expect(window.head.at(-1)).toBe('line-000024');
    expect(window.tail.at(0)).toBe('line-099975');
    expect(window.tail.at(-1)).toBe('line-099999');
    expect(window.omittedLineCount).toBe(99_950);

    const html = await renderToString(outputApp(lines));
    expect(html.match(/class="tool-output-line"/g)).toHaveLength(50);
    expect(html.match(/class="tool-output-omission"/g)).toHaveLength(1);
    expect(html).toContain('line-000000');
    expect(html).toContain('line-000024');
    expect(html).not.toContain('line-000025');
    expect(html).not.toContain('line-099974');
    expect(html).toContain('line-099975');
    expect(html).toContain('line-099999');
    expect(html).toContain('99950 lines omitted from this browser view.');
  });

  it('keeps a re-fetched command preview at the same fixed mounted-row window', async () => {
    const lines = Array.from({ length: 100_000 }, (_, index) => `saved-${index}`);
    const app = createSSRApp({
      render: () => h(ToolDiffPanel, {
        tool: {
          id: 'saved-command',
          name: 'exec_command',
          arg: 'long command',
          status: 'ok',
          output: lines,
          commandExecution: {
            cwd: '/work',
            source: 'agent',
            processId: 'process-1',
            exitCode: 0,
          },
        },
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: {
        en: {
          tools: {
            output: {
              linesOmitted: '{count} lines omitted from this browser view.',
              boundedOmitted: '{count} characters omitted from the middle',
              aggregateOmitted: 'Page budget omitted all {count} characters',
            },
            detail: { loading: 'Loading saved detail…' },
          },
          thinking: { close: 'Close' },
          diff: { noDiff: 'No detail' },
        },
      },
    }));

    const html = await renderToString(app);
    expect(html.match(/class="tool-output-line"/g)).toHaveLength(50);
    expect(html).toContain('saved-0');
    expect(html).toContain('saved-99999');
    expect(html).not.toContain('saved-50000');
    expect(html).toContain('cwd: /work');
    expect(html).toContain('exit code: 0');
  });

  it('renders every explicitly requested full source line without the preview window', async () => {
    const completeDiff = Array.from(
      { length: 5_000 },
      (_, index) => `full-${index.toString().padStart(4, '0')}`,
    ).join('\n');
    const sourceDetail: FocusFileChangeSourceDetail = {
      type: 'fileChange',
      id: 'full-file-change',
      status: 'completed',
      changes: [
        { path: 'first.py', kind: { type: 'add' }, diff: 'first' },
        {
          path: 'second.py',
          kind: { type: 'update', movePath: 'old-second.py' },
          diff: completeDiff,
        },
      ],
    };
    const app = createSSRApp({
      render: () => h(FocusToolSourceDetailPanel, {
        source: sourceDetail,
        changeIndex: 1,
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: {
        en: {
          tools: {
            detail: {
              fullFileChangeTitle: 'Full saved file changes',
              copyFull: 'Copy complete content',
              copySucceeded: 'Copied',
              copyFailed: 'Copy failed',
              fullSourceNotice: 'Complete persisted content',
              viewFullDiff: 'Diff view',
              viewFullSourceText: 'Source text',
              fullFileChangeNumber: 'File {current} of {total}',
              fullMovePath: 'Moved from: {path}',
            },
          },
          thinking: { close: 'Close' },
        },
      },
    }));

    const html = await renderToString(app);
    expect(html).toContain('full-file-change');
    expect(html).toContain('completed');
    expect(html).toContain('first.py');
    expect(html).toContain('second.py');
    expect(html).toContain('old-second.py');
    expect(html).toContain('full-0000');
    expect(html).toContain('full-0025');
    expect(html).toContain('full-2500');
    expect(html).toContain('full-4999');
    expect(html).toContain('Diff view');
    expect(html).toContain('focus-tool-source-raw');
    expect(html).not.toContain('focus-complete-diff');
    expect(html).not.toContain('dl-add');
    expect(html).not.toContain('dl-del');
    expect(html).not.toContain('lines omitted from this browser view');
    expect(html).not.toContain('tool-output-line');
    expect(formatFullToolDetailSource(sourceDetail)).toContain('full-2500');
    expect(formatFullToolDetailSource(sourceDetail)).toContain('first.py');
  });

  it('mounts every semantic row only in the explicit complete diff presentation', async () => {
    const additions = Array.from(
      { length: 5_000 },
      (_, index) => `+full-${index.toString().padStart(4, '0')}`,
    );
    additions[2_500] =
      '+[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const completeDiff = [
      'diff --git a/example.txt b/example.txt',
      'index 1111111..2222222 100644',
      '--- a/example.txt',
      '+++ b/example.txt',
      '@@ -7,2 +7,5001 @@',
      '-before',
      ' context',
      ...additions,
    ].join('\n');

    const parsed = parseCompleteUnifiedDiff(completeDiff);
    expect(parsed.slice(0, 5).map((line) => line.type)).toEqual([
      'hunk',
      'hunk',
      'hunk',
      'hunk',
      'hunk',
    ]);
    expect(parsed[5]).toEqual({ type: 'del', text: 'before', oldNo: 7 });
    expect(parsed[6]).toEqual({ type: 'context', text: 'context', oldNo: 8, newNo: 7 });
    expect(parsed[7]).toEqual({ type: 'add', text: 'full-0000', newNo: 8 });

    const app = createSSRApp({
      render: () => h(FocusCompleteDiff, {
        source: completeDiff,
        kind: { type: 'update', movePath: null },
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: { en: { tools: { output: {
        linesOmitted: '{count} lines omitted from this browser view.',
        boundedOmitted: '{count} characters omitted from the middle',
        aggregateOmitted: 'Page budget omitted all {count} characters',
      } } } },
    }));

    const html = await renderToString(app);
    expect(html.match(/\bdl-add\b/g)).toHaveLength(5_000);
    expect(html.match(/\bdl-del\b/g)).toHaveLength(1);
    expect(html.match(/\bdl-context\b/g)).toHaveLength(1);
    expect(html).toContain('full-0000');
    expect(html).toContain('full-2499');
    expect(html).toContain('full-4999');
    expect(html).toContain('Focus Web omitted 12345 characters');
    expect(html).not.toContain('dl-omission');
    expect(html).not.toContain('lines omitted from this browser view');

    const panelSource = source('../src/focus/FocusToolSourceDetailPanel.vue');
    expect(panelSource).toContain(`v-if="fileChangePresentation === 'diff'"`);
    expect(panelSource).not.toContain(`v-show="fileChangePresentation === 'diff'"`);
    expect(panelSource).toContain('@click="toggleFileChangePresentation"');
    expect(panelSource).not.toContain('aria-pressed');
    expect(panelSource).toContain(':kind="change.kind"');
    expect(panelSource).toContain('watch(() => props.source');
    expect(panelSource).toContain("fileChangePresentation.value = 'source'");
  });

  it.each([
    { kind: { type: 'add' } as const, rowClass: 'dl-add', numberKey: 'newNo' as const },
    { kind: { type: 'delete' } as const, rowClass: 'dl-del', numberKey: 'oldNo' as const },
  ])('renders complete $kind.type content as semantic $rowClass rows', async ({
    kind,
    rowClass,
    numberKey,
  }) => {
    const completeContent = 'ordinary line\n+literal plus\n-literal minus\n';
    const parsed = parseCompleteFileChangeDiff(completeContent, kind);
    expect(parsed).toHaveLength(3);
    expect(parsed.map((line) => line.type)).toEqual([
      kind.type === 'add' ? 'add' : 'del',
      kind.type === 'add' ? 'add' : 'del',
      kind.type === 'add' ? 'add' : 'del',
    ]);
    expect(parsed.map((line) => line.text)).toEqual([
      'ordinary line',
      '+literal plus',
      '-literal minus',
    ]);
    expect(parsed.map((line) => line[numberKey])).toEqual([1, 2, 3]);

    const app = createSSRApp({
      render: () => h(FocusCompleteDiff, { source: completeContent, kind }),
    });
    app.use(createI18n({ legacy: false, locale: 'en', messages: { en: {} } }));
    const html = await renderToString(app);
    expect(html.match(new RegExp(`\\b${rowClass}\\b`, 'g'))).toHaveLength(3);
    expect(html).toContain('ordinary line');
    expect(html).toContain('literal plus');
    expect(html).toContain('literal minus');
    expect(html).not.toContain('dl-hunk');
    expect(html).not.toContain('dl-omission');
  });

  it('does not offer a diff presentation for complete command content', async () => {
    const sourceDetail: FocusCommandExecutionSourceDetail = {
      type: 'commandExecution',
      id: 'full-command',
      pluginId: null,
      scriptPath: null,
      command: 'printf complete',
      cwd: '/work',
      processId: null,
      source: 'agent',
      status: 'completed',
      commandActions: [],
      aggregatedOutput: 'complete output',
      exitCode: 0,
      durationMs: 12,
    };
    const app = createSSRApp({
      render: () => h(FocusToolSourceDetailPanel, {
        source: sourceDetail,
        changeIndex: null,
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: { en: {
        tools: { detail: {
          fullCommandTitle: 'Full saved command content',
          copyFull: 'Copy complete content',
          copySucceeded: 'Copied',
          copyFailed: 'Copy failed',
          fullSourceNotice: 'Complete persisted content',
          fullCommandOutput: 'Aggregated output',
          fullNoCommandOutput: 'No output',
        } },
        thinking: { close: 'Close' },
      } },
    }));

    const html = await renderToString(app);
    expect(html).toContain('complete output');
    expect(html).not.toContain('Diff view');
    expect(html).not.toContain('focus-complete-diff');
  });

  it('pins the server character-omission disclosure outside the head and tail', async () => {
    const serverMarker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = Array.from({ length: 100 }, (_, index) => `line-${index}`);
    lines[50] = serverMarker;

    const window = buildToolOutputLineWindow(lines, 12_345, 50);
    expect(window.boundedOmission).toEqual({
      section: 'middle',
      lineIndex: 0,
      omittedChars: 12_345,
    });
    expect(window.omittedLineCount).toBe(49);

    const html = await renderToString(outputApp(lines, 12_345, 50));
    expect(html).not.toContain(serverMarker);
    expect(html).toContain('12345 characters omitted from the middle');
    expect(html).toContain('49 lines omitted from this browser view.');
    expect(html.match(/tool-output-line/g)).toHaveLength(51);
  });

  it.each([
    { boundary: 1, section: 'head', lineIndex: 1 },
    { boundary: 98, section: 'tail', lineIndex: 23 },
  ])('keeps a trusted marker at its visible $section boundary', async ({
    boundary,
    section,
    lineIndex,
  }) => {
    const marker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = Array.from({ length: 100 }, (_, index) => `line-${index}`);
    lines[boundary] = marker;

    const window = buildToolOutputLineWindow(lines, 12_345, boundary);
    expect(window.boundedOmission).toEqual({ section, lineIndex, omittedChars: 12_345 });
    expect(window.omittedLineCount).toBe(50);
    const html = await renderToString(outputApp(lines, 12_345, boundary));
    expect(html).not.toContain(marker);
    expect(html.match(/12345 characters omitted from the middle/g)).toHaveLength(1);
    expect(html).toContain('line-0');
    expect(html).toContain('line-99');
  });

  it('localizes a trusted marker inside a short unwindowed output without reordering rows', async () => {
    const marker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = ['head', marker, 'tail'];
    const window = buildToolOutputLineWindow(lines, 12_345, 1);

    expect(window.boundedOmission).toEqual({
      section: 'head',
      lineIndex: 1,
      omittedChars: 12_345,
    });
    expect(window.head).toEqual(lines);
    const html = await renderToString(outputApp(lines, 12_345, 1));
    expect(html.indexOf('head')).toBeLessThan(html.indexOf('12345 characters omitted'));
    expect(html.indexOf('12345 characters omitted')).toBeLessThan(html.indexOf('tail'));
  });

  it('renders a trusted disclosure when the aggregate omitted the whole output', async () => {
    const window = buildToolOutputLineWindow([], 100_000, 0);

    expect(window.aggregateOmittedChars).toBe(100_000);
    const html = await renderToString(outputApp([], 100_000, 0));
    expect(html).toContain('Page budget omitted all 100000 characters');
    expect(html).not.toContain('showing a bounded head and tail');
    expect(html.match(/tool-output-server-omission/g)).toHaveLength(1);
  });

  it.each([
    { label: 'GenericTool', component: GenericTool, toolName: 'custom' },
    { label: 'AgentTool', component: AgentTool, toolName: 'Agent' },
    { label: 'AskUserTool', component: AskUserTool, toolName: 'AskUserQuestion' },
    { label: 'EditTool', component: EditTool, toolName: 'Edit' },
    { label: 'SwarmTool', component: SwarmTool, toolName: 'AgentSwarm' },
  ])('$label renders a trusted zero-boundary disclosure instead of an empty state', async ({
    component,
    toolName,
  }) => {
    const html = await renderToString(fullyOmittedConsumerApp(component, toolName));

    expect(html).toContain(`Page budget omitted all ${FULLY_OMITTED_CHARS} characters`);
    expect(html).not.toContain(FULLY_OMITTED_MARKER);
    expect(html.match(/tool-output-server-omission/g)).toHaveLength(1);
    expect(html).not.toMatch(/class="[^"]*\bbb-empty\b/);
    expect(html).not.toMatch(/class="[^"]*\bwaiting\b/);
    expect(html).not.toContain('Waiting for output');
    expect(html).not.toContain('SWARM_WAITING_SENTINEL');
  });

  it('does not trust a command-authored omission marker without projection metadata', async () => {
    const marker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = Array.from({ length: 100 }, (_, index) => `line-${index}`);
    lines[50] = marker;

    const window = buildToolOutputLineWindow(lines);
    expect(window.boundedOmission).toBeNull();
    expect(window.omittedLineCount).toBe(50);

    const html = await renderToString(outputApp(lines));
    expect(html).not.toContain(marker);
    expect(html).toContain('50 lines omitted from this browser view.');
    expect(html.match(/tool-output-line/g)).toHaveLength(50);
  });

  it('mounts only a fixed diff head and tail for a giant short-line full-history item', async () => {
    const lines = Array.from(
      { length: 30_000 },
      (_, index) => ({
        type: 'context' as const,
        text: `x-${index.toString().padStart(5, '0')}`,
        oldNo: index + 1,
        newNo: index + 1,
      }),
    );
    const window = buildDiffLineWindow(lines);

    expect(window.head).toHaveLength(DIFF_HEAD_LINE_COUNT);
    expect(window.tail).toHaveLength(DIFF_TAIL_LINE_COUNT);
    expect(window.head[0]?.text).toBe('x-00000');
    expect(window.head.at(-1)?.text).toBe('x-00024');
    expect(window.tail[0]?.text).toBe('x-29975');
    expect(window.tail.at(-1)?.text).toBe('x-29999');
    expect(window.omittedLineCount).toBe(29_950);

    const app = createSSRApp({
      render: () => h(ToolDiffPanel, {
        tool: {
          id: 'history-diff',
          name: 'Edit',
          arg: '',
          status: 'ok',
          diff: { path: 'history.txt', lines },
        },
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: {
        en: {
          tools: { output: {
            linesOmitted: '{count} lines omitted from this browser view.',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'Page budget omitted all {count} characters',
          } },
          thinking: { close: 'Close' },
          diff: { noDiff: 'No line changes for this file' },
        },
      },
    }));

    const html = await renderToString(app);
    expect(html.match(/dl-context/g)).toHaveLength(50);
    expect(html.match(/dl-omission/g)).toHaveLength(1);
    expect(html).toContain('x-00000');
    expect(html).toContain('x-00024');
    expect(html).not.toContain('x-00025');
    expect(html).not.toContain('x-29974');
    expect(html).toContain('x-29975');
    expect(html).toContain('x-29999');
    expect(html).toContain('29950 lines omitted from this browser view.');
  });

  it('pins a structured-diff character omission at its trusted row boundary', async () => {
    const marker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = Array.from(
      { length: 100 },
      (_, index) => ({ type: 'hunk' as const, text: `line-${index}` }),
    );
    lines[50] = { type: 'hunk', text: marker };

    const window = buildDiffLineWindow(lines, 12_345, 50);
    expect(window.boundedOmission).toEqual({
      section: 'middle',
      lineIndex: 0,
      omittedChars: 12_345,
    });
    expect(window.omittedLineCount).toBe(49);

    const app = createSSRApp({
      render: () => h(ToolDiffPanel, {
        tool: {
          id: 'bounded-diff',
          name: 'Edit',
          arg: '',
          status: 'ok',
          diff: {
            lines,
            omittedChars: 12_345,
            omissionLineIndex: 50,
          },
        },
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: {
        en: {
          tools: { output: {
            linesOmitted: '{count} lines omitted from this browser view.',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'Page budget omitted all {count} characters',
          } },
          thinking: { close: 'Close' },
          diff: { noDiff: 'No line changes for this file' },
        },
      },
    }));

    const html = await renderToString(app);
    expect(html).not.toContain(marker);
    expect(html).toContain('12345 characters omitted from the middle');
    expect(html).toContain('49 lines omitted from this browser view.');
    expect(html.match(/dl-server-omission/g)).toHaveLength(1);
  });

  it.each([
    { boundary: 1, section: 'head', lineIndex: 1 },
    { boundary: 98, section: 'tail', lineIndex: 23 },
  ])('keeps a trusted diff marker at its visible $section boundary', ({
    boundary,
    section,
    lineIndex,
  }) => {
    const marker =
      '[Focus Web omitted 12345 characters of tool output; showing a bounded head and tail.]';
    const lines = Array.from(
      { length: 100 },
      (_, index) => ({ type: 'hunk' as const, text: `line-${index}` }),
    );
    lines[boundary] = { type: 'hunk', text: marker };

    const window = buildDiffLineWindow(lines, 12_345, boundary);
    expect(window.boundedOmission).toEqual({ section, lineIndex, omittedChars: 12_345 });
    expect(window.omittedLineCount).toBe(50);
  });

  it('renders a trusted zero-boundary disclosure for a fully omitted diff', () => {
    const window = buildDiffLineWindow([], 90_000, 0);

    expect(window.head).toEqual([]);
    expect(window.tail).toEqual([]);
    expect(window.aggregateOmittedChars).toBe(90_000);
  });

  it('renders the fully omitted diff disclosure through ToolDiffPanel instead of its empty state', async () => {
    const app = createSSRApp({
      render: () => h(ToolDiffPanel, {
        tool: {
          id: 'fully-omitted-diff',
          name: 'Edit',
          arg: '',
          status: 'ok',
          diff: {
            lines: [],
            omittedChars: FULLY_OMITTED_CHARS,
            omissionLineIndex: 0,
          },
        },
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'en',
      messages: {
        en: {
          tools: { output: {
            linesOmitted: '{count} lines omitted from this browser view.',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'Page budget omitted all {count} characters',
          } },
          thinking: { close: 'Close' },
          diff: { noDiff: 'NO_DIFF_SENTINEL' },
        },
      },
    }));

    const html = await renderToString(app);
    expect(html).toContain(`Page budget omitted all ${FULLY_OMITTED_CHARS} characters`);
    expect(html).not.toContain(FULLY_OMITTED_MARKER);
    expect(html.match(/dl-server-omission/g)).toHaveLength(1);
    expect(html).not.toMatch(/class="[^"]*\btdp-empty\b/);
    expect(html).not.toContain('NO_DIFF_SENTINEL');
  });
});
