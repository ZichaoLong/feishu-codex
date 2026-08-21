import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { ChatTurn, ToolCall, TurnBlock } from '../src/types';
import {
  assistantRenderBlocks,
  formatDuration,
  formatTokens,
  rendersToolCard,
  renderBlockKey,
  toolStackPosition,
  turnBlocks,
  turnFinalText,
  turnToMarkdown,
} from '../src/components/chatTurnRendering';

function tool(id: string, over: Partial<ToolCall> = {}): ToolCall {
  return { id, name: 'read', arg: `· ${id}.ts`, status: 'ok', ...over };
}

function toolBlock(id: string, over: Partial<ToolCall> = {}): Extract<TurnBlock, { kind: 'tool' }> {
  return { kind: 'tool', tool: tool(id, over) };
}

function assistantTurn(blocks: TurnBlock[], over: Partial<ChatTurn> = {}): ChatTurn {
  return { id: 't1', role: 'assistant', no: 1, text: '', blocks, ...over };
}

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('formatTokens', () => {
  it('keeps counts under 1024 verbatim and uses 1024-based k / M units', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1000)).toBe('1000');
    expect(formatTokens(1500)).toBe('1.5k');
    expect(formatTokens(1_000_000)).toBe('977k');
    expect(formatTokens(2_500_000)).toBe('2.4M');
  });
});

describe('formatDuration', () => {
  it('switches units at the 1s and 1m boundaries', () => {
    expect(formatDuration(999)).toBe('999ms');
    expect(formatDuration(1000)).toBe('1.0s');
    expect(formatDuration(59_999)).toBe('60.0s');
    expect(formatDuration(60_000)).toBe('1m0.0s');
    expect(formatDuration(90_500)).toBe('1m30.5s');
  });
});

describe('turnBlocks', () => {
  it('returns the ordered blocks as-is when present', () => {
    const blocks: TurnBlock[] = [{ kind: 'text', text: 'hi' }];
    expect(turnBlocks(assistantTurn(blocks))).toBe(blocks);
  });

  it('falls back to thinking -> text -> tools order when blocks are absent', () => {
    const turn: ChatTurn = {
      id: 't1',
      role: 'assistant',
      no: 1,
      text: 'answer',
      thinking: 'plan',
      tools: [tool('a')],
    };
    expect(turnBlocks(turn)).toEqual([
      { kind: 'thinking', thinking: 'plan' },
      { kind: 'text', text: 'answer' },
      { kind: 'tool', tool: tool('a') },
    ]);
  });
});

describe('rendersToolCard', () => {
  it('hides the card only for a successful tool that carries inline media', () => {
    expect(rendersToolCard(toolBlock('a'))).toBe(true);
    expect(rendersToolCard(toolBlock('r', { status: 'running' }))).toBe(true);
    expect(
      rendersToolCard(toolBlock('m', { status: 'ok', media: { kind: 'image', url: 'x' } })),
    ).toBe(false);
    // media but errored -> still rendered as a card
    expect(
      rendersToolCard(toolBlock('e', { status: 'error', media: { kind: 'image', url: 'x' } })),
    ).toBe(true);
  });
});

describe('toolStackPosition', () => {
  it('marks a lone tool single and otherwise reports first/middle/last', () => {
    expect(toolStackPosition(0, 1)).toBe('single');
    expect(toolStackPosition(0, 0)).toBe('single');
    expect(toolStackPosition(0, 3)).toBe('first');
    expect(toolStackPosition(1, 3)).toBe('middle');
    expect(toolStackPosition(2, 3)).toBe('last');
  });
});

describe('assistantRenderBlocks', () => {
  it('groups consecutive renderable tools into one tool-stack', () => {
    const rendered = assistantRenderBlocks(assistantTurn([toolBlock('a'), toolBlock('b')]));
    expect(rendered).toHaveLength(1);
    expect(rendered[0]).toMatchObject({ kind: 'tool-stack' });
    if (rendered[0]?.kind === 'tool-stack') {
      expect(rendered[0].tools.map((t) => t.tool.id)).toEqual(['a', 'b']);
      expect(rendered[0].tools.map((t) => t.sourceIndex)).toEqual([0, 1]);
    }
  });

  it('renders a lone tool as a standalone tool, not a stack', () => {
    const rendered = assistantRenderBlocks(assistantTurn([toolBlock('a')]));
    expect(rendered).toEqual([{ kind: 'tool', tool: tool('a'), sourceIndex: 0 }]);
  });

  it('breaks the stack and separates the reply when text interrupts the run', () => {
    const rendered = assistantRenderBlocks(
      assistantTurn([toolBlock('a'), { kind: 'text', text: 'x' }, toolBlock('b')]),
    );
    expect(rendered.map((b) => b.kind)).toEqual([
      'tool',
      'reply-separator',
      'text',
      'tool',
    ]);
  });

  it('breaks the stack when a media tool (no card) interrupts the run', () => {
    const rendered = assistantRenderBlocks(
      assistantTurn([
        toolBlock('a'),
        toolBlock('b'),
        toolBlock('c', { status: 'ok', media: { kind: 'image', url: 'x' } }),
      ]),
    );
    expect(rendered.map((b) => b.kind)).toEqual(['tool-stack', 'tool']);
    if (rendered[0]?.kind === 'tool-stack') {
      expect(rendered[0].tools.map((t) => t.tool.id)).toEqual(['a', 'b']);
    }
  });

  it('separates the next non-empty reply after non-empty thinking', () => {
    const rendered = assistantRenderBlocks(
      assistantTurn([
        { kind: 'thinking', thinking: 'plan' },
        { kind: 'text', text: 'answer' },
      ]),
    );
    expect(rendered).toEqual([
      { kind: 'thinking', thinking: 'plan', sourceIndex: 0 },
      { kind: 'reply-separator', sourceIndex: 1 },
      { kind: 'text', text: 'answer', sourceIndex: 1 },
    ]);
  });

  it('keeps thinking on the work side of a tool-to-reply boundary', () => {
    const rendered = assistantRenderBlocks(
      assistantTurn([
        toolBlock('a'),
        { kind: 'thinking', thinking: 'checking result' },
        { kind: 'text', text: 'answer' },
      ]),
    );

    expect(rendered.map((block) => block.kind)).toEqual([
      'tool',
      'thinking',
      'reply-separator',
      'text',
    ]);
  });

  it('does not insert a boundary for empty work, pure text, or tool-only turns', () => {
    expect(assistantRenderBlocks(assistantTurn([
      { kind: 'thinking', thinking: '  \n ' },
      { kind: 'text', text: '' },
      { kind: 'text', text: 'answer' },
    ])).map((block) => block.kind)).toEqual(['thinking', 'text', 'text']);

    expect(assistantRenderBlocks(assistantTurn([
      { kind: 'text', text: 'answer' },
    ])).map((block) => block.kind)).toEqual(['text']);

    expect(assistantRenderBlocks(assistantTurn([
      toolBlock('a'),
    ])).map((block) => block.kind)).toEqual(['tool']);
  });

  it('does not let empty text consume a pending work-to-reply boundary', () => {
    const rendered = assistantRenderBlocks(assistantTurn([
      toolBlock('a'),
      { kind: 'text', text: ' \n ' },
      { kind: 'text', text: 'answer' },
    ]));

    expect(rendered.map((block) => block.kind)).toEqual([
      'tool',
      'text',
      'reply-separator',
      'text',
    ]);
  });

  it('resets the boundary at each assistant turn and after each reply transition', () => {
    const first = assistantRenderBlocks(assistantTurn([
      toolBlock('a'),
    ]));
    const nextTurn = assistantRenderBlocks(assistantTurn([
      { kind: 'text', text: 'next turn reply' },
    ], { id: 't2' }));
    const repeated = assistantRenderBlocks(assistantTurn([
      { kind: 'text', text: 'first reply' },
      toolBlock('a'),
      { kind: 'text', text: 'second reply' },
      { kind: 'thinking', thinking: 'more work' },
      { kind: 'text', text: 'third reply' },
    ]));

    expect(first.map((block) => block.kind)).toEqual(['tool']);
    expect(nextTurn.map((block) => block.kind)).toEqual(['text']);
    expect(repeated.map((block) => block.kind)).toEqual([
      'text',
      'tool',
      'reply-separator',
      'text',
      'thinking',
      'reply-separator',
      'text',
    ]);
  });

  it('treats an ungrouped media tool as visible work activity', () => {
    const rendered = assistantRenderBlocks(assistantTurn([
      toolBlock('image', { status: 'ok', media: { kind: 'image', url: '/image' } }),
      { kind: 'text', text: 'image ready' },
    ]));

    expect(rendered.map((block) => block.kind)).toEqual([
      'tool',
      'reply-separator',
      'text',
    ]);
  });
});

describe('assistant reply separator surface', () => {
  it('renders the derived boundary as one unlabeled separator line', () => {
    const chatPane = source('../src/components/chat/ChatPane.vue');
    const separator = chatPane.match(
      /<div\s+v-else-if="blk\.kind === 'reply-separator'"\s+class="assistant-reply-separator"\s+role="separator"\s*\/>/,
    );

    expect(separator?.[0]).toBeDefined();
    expect(separator?.[0]).not.toContain('{{');
    expect(chatPane).toContain('.assistant-reply-separator {');
    expect(chatPane).toMatch(
      /\.assistant-reply-separator \{[\s\S]*?width:\s*100%;[\s\S]*?height:\s*2px;[\s\S]*?background:\s*var\(--color-line-strong\);[\s\S]*?\}/u,
    );
  });
});

describe('turnFinalText', () => {
  it('joins only the text blocks, dropping thinking and tools', () => {
    const turn = assistantTurn([
      { kind: 'thinking', thinking: 'plan' },
      { kind: 'text', text: 'first' },
      toolBlock('a'),
      { kind: 'text', text: 'second' },
    ]);
    expect(turnFinalText(turn)).toBe('first\n\nsecond');
  });
});

describe('turnToMarkdown', () => {
  it('renders thinking as a quote, text verbatim, and tool output as a fenced block', () => {
    const turn = assistantTurn([
      { kind: 'thinking', thinking: 'line1\nline2' },
      { kind: 'text', text: 'hello' },
      toolBlock('a', { name: 'bash', output: ['out1', 'out2'] }),
    ]);
    expect(turnToMarkdown(turn)).toBe(
      ['> **Thinking**\n> line1\n> line2', 'hello', '```\n[bash]\nout1\nout2\n```'].join('\n\n'),
    );
  });
});

describe('renderBlockKey', () => {
  it('derives stable keys per block kind', () => {
    expect(renderBlockKey({ kind: 'text', text: 'x', sourceIndex: 2 }, 0)).toBe('text-2');
    expect(renderBlockKey({ kind: 'reply-separator', sourceIndex: 2 }, 0)).toBe(
      'reply-separator-2',
    );
    expect(renderBlockKey({ kind: 'tool', tool: tool('a'), sourceIndex: 3 }, 0)).toBe('a');
    expect(
      renderBlockKey({ kind: 'tool-stack', tools: [{ tool: tool('a'), sourceIndex: 5 }] }, 0),
    ).toBe('tool-stack-5');
  });
});
