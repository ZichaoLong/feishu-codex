import { describe, expect, it } from 'vitest';
import type { ChatTurn, ToolCall } from '../src/types';
import {
  admitBoundedToolOutputForPage,
  appendBoundedToolOutput,
  toolOutputCodePointLength,
  toolOutputWindowFitsAggregate,
} from '../src/focus/toolOutputPresentation';

const MARKER = /^\[Focus Web omitted (\d+) characters of tool output; showing a bounded head and tail\.\]$/;

describe('bounded live tool-output presentation', () => {
  it('leaves ordinary line output unchanged', () => {
    expect(appendBoundedToolOutput(['first'], '\nsecond')).toEqual({
      lines: ['first', 'second'],
      omittedChars: 0,
      headLineCount: 0,
    });
  });

  it('bounds one giant line while retaining its head and tail', () => {
    const giant = `${'H'.repeat(20_000)}${'M'.repeat(80_000)}${'T'.repeat(20_000)}`;
    const result = appendBoundedToolOutput([], giant);
    const markerIndex = result.headLineCount;

    expect(result.omittedChars).toBe(120_000 - 65_536);
    expect(markerIndex).toBeGreaterThanOrEqual(0);
    expect(result.lines.slice(0, markerIndex).join('\n')).toHaveLength(16_384);
    expect(result.lines.slice(markerIndex + 1).join('\n')).toHaveLength(49_152);
    expect(result.lines[0]?.startsWith('H')).toBe(true);
    expect(result.lines.at(-1)?.endsWith('T')).toBe(true);
  });

  it('keeps exactly 65,536 supplementary-plane characters without truncation', () => {
    const output = '😀'.repeat(65_536);
    const result = appendBoundedToolOutput([], output);

    expect(toolOutputCodePointLength(output)).toBe(65_536);
    expect(result).toEqual({
      lines: [output],
      omittedChars: 0,
      headLineCount: 0,
    });
  });

  it('keeps a fixed tail and advances the honest omission count on later deltas', () => {
    const initial = appendBoundedToolOutput([], 'A'.repeat(100_000));
    const next = appendBoundedToolOutput(
      initial.lines,
      `\n${'Z'.repeat(20_000)}`,
      initial.omittedChars,
      initial.headLineCount,
    );
    const marker = next.lines.find((line) => MARKER.test(line));

    expect(next.omittedChars).toBe(initial.omittedChars + 20_001);
    expect(marker).toBe(
      `[Focus Web omitted ${next.omittedChars} characters of tool output; showing a bounded head and tail.]`,
    );
    expect(next.lines.at(-1)?.endsWith('Z'.repeat(20_000))).toBe(true);
    expect(next.lines.join('\n').length).toBeLessThan(66_000);
  });

  it('counts supplementary-plane deltas by code point after a truncated snapshot', () => {
    const snapshot = appendBoundedToolOutput([], 'A'.repeat(100_000));
    const addition = '😀'.repeat(20_000);
    const next = appendBoundedToolOutput(
      snapshot.lines,
      addition,
      snapshot.omittedChars,
      snapshot.headLineCount,
    );

    expect(next.omittedChars).toBe(snapshot.omittedChars + 20_000);
    expect(next.lines[next.headLineCount]).toBe(
      `[Focus Web omitted ${next.omittedChars} characters of tool output; showing a bounded head and tail.]`,
    );
    expect(toolOutputCodePointLength(next.lines.at(-1) ?? '')).toBe(49_152);
    expect(next.lines.at(-1)?.endsWith(addition)).toBe(true);
  });

  it('does not interpret marker-like user output without truncation metadata', () => {
    const userLine = '[Focus Web omitted 12 characters of tool output; showing a bounded head and tail.]';
    const result = appendBoundedToolOutput([userLine], '\nordinary', 0);

    expect(result).toEqual({
      lines: [userLine, 'ordinary'],
      omittedChars: 0,
      headLineCount: 0,
    });
  });

  it('does not let an exact-count spoof marker capture later deltas', () => {
    const spoof = '[Focus Web omitted 1000 characters of tool output; showing a bounded head and tail.]';
    const original = `${spoof}\n${'A'.repeat(65_536 + 1_000 - spoof.length - 1)}`;
    const initial = appendBoundedToolOutput([], original);
    const next = appendBoundedToolOutput(
      initial.lines,
      `\n${'Z'.repeat(20_000)}`,
      initial.omittedChars,
      initial.headLineCount,
    );

    expect(next.lines[0]).toBe(spoof);
    expect(initial.omittedChars).toBe(1_000);
    expect(next.omittedChars).toBe(initial.omittedChars + 20_001);
    expect(next.lines[next.headLineCount]).toBe(
      `[Focus Web omitted ${next.omittedChars} characters of tool output; showing a bounded head and tail.]`,
    );
    expect(next.lines.at(-1)?.endsWith('Z'.repeat(20_000))).toBe(true);
  });

  it('joins arbitrary raw chunks without adding or deleting line boundaries', () => {
    const first = appendBoundedToolOutput([], 'hel');
    const second = appendBoundedToolOutput(first.lines, 'lo');
    const third = appendBoundedToolOutput(second.lines, '\n\n');

    expect(second.lines).toEqual(['hello']);
    expect(third.lines).toEqual(['hello', '', '']);
    expect(third.omittedChars).toBe(0);
  });

  it('keeps an aggregate-omitted output empty while counting later raw chunks', () => {
    const next = appendBoundedToolOutput([], '\nnew output', 100_000, 0);

    expect(next).toEqual({
      lines: [],
      omittedChars: 100_011,
      headLineCount: 0,
    });
  });

  it('uses a zero-boundary omission when the page aggregate is exhausted', () => {
    const bounded = appendBoundedToolOutput([], 'x'.repeat(100_000));
    const omitted = admitBoundedToolOutputForPage(bounded, 0, 0);

    expect(omitted).toEqual({
      lines: [],
      omittedChars: 100_000,
      headLineCount: 0,
    });
  });

  it('applies one aggregate across mixed tools and blocks surfaces', () => {
    const tool = (id: string): ToolCall => ({
      id,
      name: 'exec_command',
      arg: '',
      status: 'ok',
      output: ['x'],
    });
    const blockTools = Array.from({ length: 8 }, (_, index) => tool(`block-${index}`));
    const directTools = Array.from({ length: 9 }, (_, index) => tool(`direct-${index}`));
    const turns: ChatTurn[] = [
      {
        id: 'raw-1:assistant', role: 'assistant', no: 1, text: '',
        blocks: blockTools.map((entry) => ({ kind: 'tool', tool: entry })),
      },
      {
        id: 'raw-2:assistant', role: 'assistant', no: 1, text: '',
        tools: directTools,
      },
    ];

    expect(toolOutputWindowFitsAggregate([
      turns[0]!,
      { ...turns[1]!, tools: directTools.slice(0, 8) },
    ])).toBe(true);
    expect(toolOutputWindowFitsAggregate(turns)).toBe(false);
  });
});
