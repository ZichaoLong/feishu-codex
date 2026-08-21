import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  collectFilePathAliases,
  findFilePathLinks,
  parseFilePathLinkCandidate,
} from '../src/lib/filePathLinks';
import { buildDiffLines } from '../src/lib/diffLines';
import { buildEditDiffLines } from '../src/lib/toolDiff';
import { normalizeToolName, toolSummary } from '../src/lib/toolMeta';
import { collapsePrompt, humanizeCron } from '../src/lib/cronHumanize';
import {
  commitLevel,
  defaultThinkingLevelFor,
  effortLabel,
  modelThinkingAvailability,
  segmentsFor,
} from '../src/lib/modelThinking';
import { resolveToolRenderer } from '../src/components/chat/tool-calls/toolRegistry';
import AgentTool from '../src/components/chat/tool-calls/AgentTool.vue';
import EditTool from '../src/components/chat/tool-calls/EditTool.vue';
import GenericTool from '../src/components/chat/tool-calls/GenericTool.vue';
import type { AppModel, ToolCall } from '../src/types';
import {
  clearTrace,
  installClientErrorCapture,
  sanitizeForTrace,
  sessionExportTraceToJsonl,
  traceEntries,
  traceKeyEvent,
  tracePaused,
  traceRestRequest,
  traceToJsonl,
  traceWsIn,
} from '../src/debug/trace';

// The trace tests exercise its exported recording/serialization contract:
// session exports receive only bounded, explicitly selected metadata.

describe('bounded Web trace', () => {
  beforeEach(() => {
    tracePaused.value = false;
    clearTrace();
  });

  afterEach(() => {
    clearTrace();
    vi.unstubAllGlobals();
  });

  it('copies only allowlisted key-path metadata into the independent export ring', () => {
    const secret = 'PROMPT_TEXT_MUST_NOT_BE_EXPORTED';
    const metadata = {
      sessionId: 'sess_1',
      contentCount: 2,
      mediaCount: 1,
      text: secret,
      apiKey: secret,
    };
    traceKeyEvent('prompt:start', metadata);

    metadata.sessionId = 'changed_after_recording';

    expect(traceEntries()).toHaveLength(1);
    expect(JSON.parse(sessionExportTraceToJsonl())).toEqual({
      ts: expect.any(Number),
      event: 'prompt:start',
      sessionId: 'sess_1',
      contentCount: 2,
      mediaCount: 1,
    });
    expect(sessionExportTraceToJsonl()).not.toContain(secret);
  });

  it('records export metadata even while the full debug panel is paused', () => {
    tracePaused.value = true;

    traceKeyEvent('ws:connection', { status: 'connected' });

    expect(traceEntries()).toHaveLength(0);
    expect(JSON.parse(sessionExportTraceToJsonl())).toMatchObject({
      event: 'ws:connection',
      status: 'connected',
    });
  });

  it('caps object keys and reports how many were omitted', () => {
    const input = Object.fromEntries(Array.from({ length: 60 }, (_, index) => [`key${index}`, index]));

    const result = sanitizeForTrace(input) as Record<string, unknown>;

    expect(result['_truncatedKeys']).toBe(10);
    expect(Object.keys(result)).toHaveLength(51);
  });

  it('keeps at most 500 of the newest export entries', () => {
    for (let index = 0; index < 501; index++) {
      traceKeyEvent('ws:connection', { status: String(index) });
    }

    const exported = sessionExportTraceToJsonl().split('\n').map((line) => JSON.parse(line));
    expect(exported).toHaveLength(500);
    expect(exported[0]).toMatchObject({ status: '1' });
    expect(exported.at(-1)).toMatchObject({ status: '500' });
  });

  it('keeps export JSONL within the 256 KiB UTF-8 budget including newlines', () => {
    for (let index = 0; index < 500; index++) {
      traceKeyEvent('export:failed', {
        sessionId: `sess-${index}-${'😀'.repeat(200)}`,
        status: '😀'.repeat(200),
        promptId: '😀'.repeat(200),
        errorName: '😀'.repeat(200),
        requestId: '😀'.repeat(200),
        phase: '😀'.repeat(200),
      });
    }

    const jsonl = sessionExportTraceToJsonl();
    expect(new TextEncoder().encode(jsonl).byteLength).toBeLessThanOrEqual(256 * 1024);
    expect(JSON.parse(jsonl.split('\n').at(-1)!)).toMatchObject({
      sessionId: expect.stringMatching(/^sess-499-/),
    });
  });

  it('never copies prompt, WebSocket, or console content from the full debug trace', () => {
    vi.stubGlobal('location', { search: '?debug=1' });
    const promptSecret = 'PROMPT_SECRET_9fdb1a';
    const wsSecret = 'WS_PAYLOAD_SECRET_b84c7e';
    const consoleSecret = 'CONSOLE_SECRET_a2d693';

    traceRestRequest({
      method: 'POST',
      path: '/sessions/sess_1/prompts',
      url: 'http://daemon.test/api/v1/sessions/sess_1/prompts',
      requestId: 'req_1',
      body: { prompt: promptSecret },
    });
    traceWsIn({
      type: 'event',
      session_id: 'sess_1',
      seq: 4,
      payload: { text: wsSecret },
    });

    const originalLog = console.log;
    console.log = vi.fn();
    const dispose = installClientErrorCapture();
    try {
      console.log(consoleSecret, { value: consoleSecret });
    } finally {
      dispose();
      console.log = originalLog;
    }

    traceKeyEvent('prompt:start', {
      sessionId: 'sess_1',
      contentCount: 1,
      mediaCount: 0,
      text: promptSecret,
    });

    const fullDebugTrace = traceToJsonl();
    expect(fullDebugTrace).toContain(promptSecret);
    expect(fullDebugTrace).toContain(wsSecret);
    expect(fullDebugTrace).toContain(consoleSecret);

    const sessionExportTrace = sessionExportTraceToJsonl();
    expect(sessionExportTrace).not.toContain(promptSecret);
    expect(sessionExportTrace).not.toContain(wsSecret);
    expect(sessionExportTrace).not.toContain(consoleSecret);
    expect(JSON.parse(sessionExportTrace)).toEqual({
      ts: expect.any(Number),
      event: 'prompt:start',
      sessionId: 'sess_1',
      contentCount: 1,
      mediaCount: 0,
    });
  });
});

describe('buildDiffLines', () => {
  it('lines up context, deletions and additions with old/new line numbers', () => {
    const before = 'a\nb\nc';
    const after = 'a\nB\nc\nd';
    expect(buildDiffLines(before, after)).toEqual([
      { type: 'context', text: 'a', oldNo: 1, newNo: 1 },
      { type: 'del', text: 'b', oldNo: 2 },
      { type: 'add', text: 'B', newNo: 2 },
      { type: 'context', text: 'c', oldNo: 3, newNo: 3 },
      { type: 'add', text: 'd', newNo: 4 },
    ]);
  });

  it('treats an empty before as an all-addition write', () => {
    expect(buildDiffLines('', 'x\ny')).toEqual([
      { type: 'add', text: 'x', newNo: 1 },
      { type: 'add', text: 'y', newNo: 2 },
    ]);
  });

  it('returns all context for identical texts and empty for two empties', () => {
    expect(buildDiffLines('a\nb', 'a\nb')).toEqual([
      { type: 'context', text: 'a', oldNo: 1, newNo: 1 },
      { type: 'context', text: 'b', oldNo: 2, newNo: 2 },
    ]);
    expect(buildDiffLines('', '')).toEqual([]);
  });

  it('returns null when the LCS matrix would be too large', () => {
    const big = Array.from({ length: 2000 }, (_, i) => `line${i}`).join('\n');
    expect(buildDiffLines(big, `${big}\nextra`)).toBeNull();
  });

  it('returns null when one side is huge even though the matrix is small', () => {
    const huge = Array.from({ length: 6000 }, (_, i) => `line${i}`).join('\n');
    expect(buildDiffLines('one line', huge)).toBeNull();
  });
});

describe('buildEditDiffLines', () => {
  it('builds a diff for a single Edit', () => {
    const arg = JSON.stringify({ path: 'a.ts', old_string: 'a\nb', new_string: 'a\nB' });
    expect(buildEditDiffLines({ name: 'Edit', arg })).toEqual([
      { type: 'context', text: 'a', oldNo: 1, newNo: 1 },
      { type: 'del', text: 'b', oldNo: 2 },
      { type: 'add', text: 'B', newNo: 2 },
    ]);
  });

  it('falls back to output for replace_all edits', () => {
    const arg = JSON.stringify({ path: 'a.ts', old_string: 'a', new_string: 'b', replace_all: true });
    expect(buildEditDiffLines({ name: 'Edit', arg })).toBeNull();
  });

  it('falls back to output for every Write (new file or overwrite)', () => {
    expect(buildEditDiffLines({ name: 'Write', arg: JSON.stringify({ path: 'a.ts', content: 'x' }) })).toBeNull();
    expect(
      buildEditDiffLines({ name: 'Write', arg: JSON.stringify({ path: 'a.ts', content: 'x', mode: 'append' }) }),
    ).toBeNull();
  });

  it('returns null for non-edit/write tools', () => {
    expect(buildEditDiffLines({ name: 'Bash', arg: JSON.stringify({ command: 'ls' }) })).toBeNull();
  });
});

describe('filePathLinks', () => {
  it('rejects URLs and bare unknown filenames', () => {
    expect(parseFilePathLinkCandidate('https://example.com/a.ts')).toBeNull();
    expect(parseFilePathLinkCandidate('e2e-success.png')).toBeNull();
  });

  it('finds path links with line numbers and resolves aliases', () => {
    const aliases = collectFilePathAliases('<img src="/assets/demo.png">');
    expect(aliases.get('demo.png')).toBe('/assets/demo.png');

    expect(
      findFilePathLinks('Open src/a.ts#L12 and demo.png.', { aliases }),
    ).toMatchObject([
      { path: 'src/a.ts', line: 12, text: 'src/a.ts#L12' },
      { path: '/assets/demo.png', text: 'demo.png' },
    ]);
  });
});

describe('toolMeta', () => {
  it('normalizes common tool aliases', () => {
    expect(normalizeToolName('WebFetch')).toBe('web_fetch');
    expect(normalizeToolName('MultiEdit')).toBe('multi_edit');
    expect(normalizeToolName('TodoWrite')).toBe('todo');
    expect(normalizeToolName('rg')).toBe('grep');
  });

  it('summarizes tool arguments for card headers', () => {
    expect(
      toolSummary('Read', JSON.stringify({ path: 'src/a.ts', offset: 10, limit: 5 })),
    ).toBe('src/a.ts:10-15');
    expect(toolSummary('Read', '{}')).toBe('');
    expect(toolSummary('Bash', JSON.stringify({ command: 'pnpm test' }))).toBe('pnpm test');
    expect(
      toolSummary('WebFetch', JSON.stringify({ url: 'https://example.com/path/to' })),
    ).toBe('example.com/path');
  });
});

describe('resolveToolRenderer', () => {
  // Minimal ToolCall factory — resolveToolRenderer only reads `name`, `status`
  // and `media`, so the rest is filled with placeholders.
  const tool = (name: string, status: ToolCall['status'] = 'running'): ToolCall => ({
    id: 't1',
    name,
    arg: '',
    status,
  });

  // Regression: normalizeToolName() folds `agent`/`subagent` into the canonical
  // `task` kind, so the renderer must match on `task`. If it matched on the raw
  // `agent` string these calls would fall through to GenericTool and lose the
  // inline "Open" button for the subagent detail panel.
  it('routes Agent / subagent calls to the Agent renderer', () => {
    expect(resolveToolRenderer(tool('agent'))).toBe(AgentTool);
    expect(resolveToolRenderer(tool('Agent'))).toBe(AgentTool);
    expect(resolveToolRenderer(tool('subagent'))).toBe(AgentTool);
    expect(resolveToolRenderer(tool('task'))).toBe(AgentTool);
  });

  it('routes edit-like calls to the Edit renderer', () => {
    expect(resolveToolRenderer(tool('edit'))).toBe(EditTool);
    expect(resolveToolRenderer(tool('write'))).toBe(EditTool);
    expect(resolveToolRenderer(tool('multi_edit'))).toBe(EditTool);
  });

  it('falls back to the Generic renderer for unknown tools', () => {
    expect(resolveToolRenderer(tool('bash'))).toBe(GenericTool);
    expect(resolveToolRenderer(tool('read'))).toBe(GenericTool);
  });
});

describe('modelThinking', () => {
  const effortModel = (over: Partial<AppModel> = {}): AppModel => ({
    id: 'k',
    provider: 'p',
    model: 'k',
    maxContextSize: 1,
    capabilities: ['thinking'],
    supportEfforts: ['low', 'high', 'max'],
    defaultEffort: 'high',
    ...over,
  });
  const booleanModel = (capabilities: string[] = ['thinking']): AppModel => ({
    id: 'b',
    provider: 'p',
    model: 'b',
    maxContextSize: 1,
    capabilities,
  });
  const unsupportedModel = (): AppModel => ({
    id: 'u',
    provider: 'p',
    model: 'u',
    maxContextSize: 1,
    capabilities: [],
  });

  describe('modelThinkingAvailability', () => {
    it('toggle when model has thinking capability', () => {
      expect(modelThinkingAvailability(booleanModel())).toBe('toggle');
    });
    it('always-on when model has always_thinking', () => {
      expect(modelThinkingAvailability(booleanModel(['always_thinking']))).toBe('always-on');
    });
    it('unsupported when model lacks thinking capability', () => {
      expect(modelThinkingAvailability(unsupportedModel())).toBe('unsupported');
    });
    it('toggle when adaptiveThinking is set', () => {
      expect(modelThinkingAvailability({ ...unsupportedModel(), adaptiveThinking: true })).toBe('toggle');
    });
  });

  describe('defaultThinkingLevelFor', () => {
    it('effort model returns defaultEffort', () => {
      expect(defaultThinkingLevelFor(effortModel())).toBe('high');
    });
    it('effort model without defaultEffort returns middle effort', () => {
      expect(defaultThinkingLevelFor(effortModel({ defaultEffort: undefined }))).toBe('high');
    });
    it('boolean model returns on', () => {
      expect(defaultThinkingLevelFor(booleanModel())).toBe('on');
    });
    it('unsupported model returns off', () => {
      expect(defaultThinkingLevelFor(unsupportedModel())).toBe('off');
    });
  });

  describe('segmentsFor', () => {
    it('effort toggle → off + efforts (off left)', () => {
      expect(segmentsFor(effortModel())).toEqual(['off', 'low', 'high', 'max']);
    });
    it('effort always-on → efforts only (no off)', () => {
      expect(segmentsFor(effortModel({ capabilities: ['thinking', 'always_thinking'] }))).toEqual([
        'low',
        'high',
        'max',
      ]);
    });
    it('boolean toggle → on/off (on left)', () => {
      expect(segmentsFor(booleanModel())).toEqual(['on', 'off']);
    });
    it('boolean always-on → on', () => {
      expect(segmentsFor(booleanModel(['always_thinking']))).toEqual(['on']);
    });
    it('unsupported → off', () => {
      expect(segmentsFor(unsupportedModel())).toEqual(['off']);
    });
  });

  describe('commitLevel', () => {
    it('on normalizes to the model default', () => {
      expect(commitLevel(effortModel(), 'on')).toBe('high');
      expect(commitLevel(booleanModel(), 'on')).toBe('on');
    });
    it('off stays off', () => {
      expect(commitLevel(effortModel(), 'off')).toBe('off');
    });
    it('concrete effort passes through', () => {
      expect(commitLevel(effortModel(), 'max')).toBe('max');
    });
  });

  describe('effortLabel', () => {
    it('capitalizes the first letter', () => {
      expect(effortLabel('max')).toBe('Max');
      expect(effortLabel('off')).toBe('Off');
      expect(effortLabel('xhigh')).toBe('Xhigh');
    });
  });
});

describe('humanizeCron', () => {
  const dict: Record<string, string> = {
    'conversation.cron.everyMinute': 'Every minute',
    'conversation.cron.everyNMinutes': 'Every {n} minutes',
    'conversation.cron.everyHour': 'Every hour',
    'conversation.cron.everyNHours': 'Every {n} hours',
    'conversation.cron.dailyAt': 'Daily at {time}',
    'conversation.cron.weekdaysAt': 'Weekdays at {time}',
  };
  const t = (key: string, params?: Record<string, unknown>): string => {
    let s = dict[key] ?? key;
    if (params) for (const [k, v] of Object.entries(params)) s = s.replace(`{${k}}`, String(v));
    return s;
  };

  it('labels the common cadences', () => {
    expect(humanizeCron('* * * * *', t)).toBe('Every minute');
    expect(humanizeCron('*/5 * * * *', t)).toBe('Every 5 minutes');
    expect(humanizeCron('*/1 * * * *', t)).toBe('Every minute');
    expect(humanizeCron('0 * * * *', t)).toBe('Every hour');
    expect(humanizeCron('0 */2 * * *', t)).toBe('Every 2 hours');
  });

  it('labels fixed daily and weekday times', () => {
    expect(humanizeCron('5 9 * * *', t)).toBe('Daily at 9:05');
    expect(humanizeCron('0 9 * * 1-5', t)).toBe('Weekdays at 9:00');
  });

  it('falls back to the raw expression for unrecognized shapes', () => {
    expect(humanizeCron('0 9 1 * *', t)).toBe('0 9 1 * *');
    expect(humanizeCron('bad', t)).toBe('bad');
  });
});

describe('collapsePrompt', () => {
  it('keeps a short single-line prompt intact with no expand toggle', () => {
    expect(collapsePrompt('Check the deploy status')).toEqual({
      text: 'Check the deploy status',
      hasMore: false,
    });
  });

  it('truncates a long one-line prompt with an ellipsis and reports hasMore', () => {
    const long = 'a'.repeat(150);
    const result = collapsePrompt(long, 120);
    expect(result.hasMore).toBe(true);
    expect(result.text.length).toBeLessThan(long.length);
    expect(result.text.endsWith('…')).toBe(true);
  });

  it('shows only the first line for a multi-line prompt', () => {
    expect(collapsePrompt('first line\nsecond line\nthird line')).toEqual({
      text: 'first line',
      hasMore: true,
    });
  });
});
