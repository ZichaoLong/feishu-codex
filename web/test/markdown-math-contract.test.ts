import { renderToString } from '@vue/server-renderer';
import { createSSRApp, h } from 'vue';
import { createI18n } from 'vue-i18n';
import { describe, expect, it } from 'vitest';
import {
  getMarkdown,
  parseMarkdownToStructure,
  type MarkdownIt,
} from 'markstream-vue';
import Markdown from '../src/components/chat/Markdown.vue';
import {
  configureFocusMarkdownMath,
  containsFocusMarkdownMath,
} from '../src/lib/markdownMath';

interface TokenShape {
  type: string;
  content?: string;
  markup?: string;
  raw?: string;
  children?: TokenShape[] | null;
}

let parserId = 0;

function parser(): MarkdownIt {
  parserId += 1;
  return configureFocusMarkdownMath(getMarkdown(`focus-math-contract-${parserId}`));
}

function tokens(source: string): TokenShape[] {
  return parser().parse(source, { __markstreamFinal: true }) as TokenShape[];
}

function flatten(sourceTokens: TokenShape[]): TokenShape[] {
  return sourceTokens.flatMap((token) => [token, ...flatten(token.children ?? [])]);
}

function mathTokens(source: string): TokenShape[] {
  return flatten(tokens(source)).filter(
    (token) => token.type === 'math_inline' || token.type === 'math_block',
  );
}

function renderedText(source: string): string {
  const nodes = parseMarkdownToStructure(source, parser(), {
    final: true,
    streamParse: false,
  }) as TokenShape[];
  return flatten(nodes)
    .filter((node) => node.type === 'text')
    .map((node) => node.content ?? '')
    .join('');
}

function markdownApp(source: string) {
  const app = createSSRApp({
    render: () => h(Markdown, { text: source }),
  });
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: { en: {} },
  }));
  app.provide('resolveImage', undefined);
  return app;
}

describe('Focus Markdown math contract', () => {
  it('accepts only backslash-delimited inline math', () => {
    expect(mathTokens(String.raw`before \(x + y\) after`)).toMatchObject([{
      type: 'math_inline',
      content: 'x + y',
      markup: String.raw`\(\)`,
      raw: String.raw`\(x + y\)`,
    }]);
  });

  it('accepts single-line and multiline bracket and dollar blocks', () => {
    const cases = [
      String.raw`\[x + y\]`,
      [String.raw`\[`, 'x + y', String.raw`\]`].join('\n'),
      '$$x + y$$',
      ['$$', 'x + y', '$$'].join('\n'),
    ];
    for (const source of cases) {
      const parsed = mathTokens(source);
      expect(parsed).toHaveLength(1);
      expect(parsed[0]).toMatchObject({ type: 'math_block', raw: source });
    }
  });

  it('keeps every single-dollar form and prose-position double dollars literal', () => {
    const source = '$PATH ${HOME} $5 $x$ before $$x + y$$ after and a trailing $';
    expect(mathTokens(source)).toEqual([]);
    expect(renderedText(source)).toBe(source);
    expect(renderedText('$')).toBe('$');
  });

  it('keeps code, escaped delimiters, and unclosed delimiters literal', () => {
    expect(mathTokens(`code: ${'`'}${String.raw`\(x\)`}${'`'}`)).toEqual([]);
    expect(mathTokens(['```txt', '$$x$$', '```'].join('\n'))).toEqual([]);
    expect(mathTokens(String.raw`\\(x\\)`)).toEqual([]);
    expect(renderedText(String.raw`\\(x\\)`)).toBe(String.raw`\(x\)`);
    expect(renderedText(String.raw`\(unclosed`)).toBe(String.raw`\(unclosed`);
    expect(renderedText(String.raw`before \[x\] after`)).toBe(String.raw`before \[x\] after`);
    expect(renderedText(String.raw`\[unclosed`)).toBe(String.raw`\[unclosed`);
    expect(renderedText(String.raw`stray \) and \]`)).toBe(String.raw`stray \) and \]`);

    const code = parseMarkdownToStructure(
      String.raw`\(x ` + '`code`' + String.raw` y\)`,
      parser(),
      { final: true, streamParse: false },
    ) as TokenShape[];
    expect(flatten(code).filter((node) => node.type === 'text').map((node) => node.content)).toEqual([
      String.raw`\(x `,
      String.raw` y\)`,
    ]);
  });

  it('keeps nested block math while rejecting indented code', () => {
    expect(mathTokens('    $$indented$$')).toEqual([]);
    expect(mathTokens('> $$quoted$$')).toHaveLength(1);
    expect(mathTokens('- $$listed$$')).toHaveLength(1);
    expect(mathTokens(['> $$', '> quoted', 'outside', '$$'].join('\n'))).toEqual([]);
    expect(mathTokens(['- $$', '  listed', 'outside', '$$'].join('\n'))).toEqual([]);

    const quoteBoundary = parseMarkdownToStructure(
      ['> $$', '> quoted', 'outside', '$$'].join('\n'),
      parser(),
      { final: true, streamParse: false },
    ) as TokenShape[];
    expect(quoteBoundary.map((node) => node.type)).toEqual(['blockquote', 'paragraph']);
    expect(quoteBoundary[1]?.raw).toBe('$$');
  });

  it('uses the same exact grammar for lazy runtime detection', () => {
    expect(containsFocusMarkdownMath(String.raw`\(x\)`)).toBe(true);
    expect(containsFocusMarkdownMath(String.raw`\[x\]`)).toBe(true);
    expect(containsFocusMarkdownMath(['$$', 'x', '$$'].join('\n'))).toBe(true);
    expect(containsFocusMarkdownMath('$x$ before $$x$$ after')).toBe(false);
    expect(containsFocusMarkdownMath(String.raw`\\(x\\)`)).toBe(false);
    expect(containsFocusMarkdownMath(String.raw`\(unclosed`)).toBe(false);
    expect(containsFocusMarkdownMath(['```ts', String.raw`\(x\)`, '```'].join('\n'))).toBe(false);
    expect(containsFocusMarkdownMath('    $$indented$$')).toBe(false);
    expect(containsFocusMarkdownMath('> $$quoted$$')).toBe(true);
    expect(containsFocusMarkdownMath('- $$listed$$')).toBe(true);
    expect(containsFocusMarkdownMath(['> $$', '> quoted', 'outside', '$$'].join('\n'))).toBe(false);
    expect(containsFocusMarkdownMath(['- $$', '  listed', 'outside', '$$'].join('\n'))).toBe(false);
    expect(containsFocusMarkdownMath(['`open', String.raw`\(x\)`, 'close`'].join('\n'))).toBe(false);
  });

  it('keeps detector admission identical to parser admission for candidate syntax', () => {
    const cases = [
      String.raw`\(x\)`,
      String.raw`\(unclosed`,
      String.raw`\\(escaped\\)`,
      String.raw`before \[x\] after`,
      String.raw`\[x\]`,
      '$$x$$',
      'before $$x$$ after',
      '> $$quoted$$',
      '- $$listed$$',
      ['> $$', '> quoted', 'outside', '$$'].join('\n'),
      ['- $$', '  listed', 'outside', '$$'].join('\n'),
      '    $$indented$$',
      ['```sh', String.raw`\(x\)`, '```'].join('\n'),
      ['`open', String.raw`\(x\)`, 'close`'].join('\n'),
    ];
    for (const source of cases) {
      expect(containsFocusMarkdownMath(source), source).toBe(mathTokens(source).length > 0);
    }
  });

  it('wires the exact grammar into the real renderer and preserves source fallback', async () => {
    const literal = await renderToString(markdownApp('$PATH $x$ before $$x$$ after'));
    expect(literal).not.toContain('data-markstream-math');
    expect(literal).toContain('$PATH $x$ before $$x$$ after');

    const inline = await renderToString(markdownApp(String.raw`\(x + y\)`));
    expect(inline).toContain('data-markstream-math="inline"');
    expect(inline).toContain(String.raw`\(x + y\)`);

    const block = await renderToString(markdownApp('$$x + y$$'));
    expect(block).toContain('data-markstream-math="block"');
    expect(block).toContain('$$x + y$$');

    const unsupported = await renderToString(markdownApp(String.raw`\(unclosed and \[inline\]`));
    expect(unsupported).not.toContain('data-markstream-math');
    expect(unsupported).toContain(String.raw`\(unclosed and \[inline\]`);
  });
});
