import { createSSRApp, h } from 'vue';
import { createI18n } from 'vue-i18n';
import { renderToString } from '@vue/server-renderer';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  buildMarkdownDiffPresentationRows,
  copyCompleteMarkdownDiffFence,
  MARKDOWN_DIFF_HEAD_LINE_COUNT,
  MARKDOWN_DIFF_TAIL_LINE_COUNT,
} from '../src/lib/markdownDiffFence';
import Markdown from '../src/components/chat/Markdown.vue';

vi.mock('markstream-vue', () => ({
  clearKaTeXWorker: vi.fn(),
  clearMermaidWorker: vi.fn(),
  disableKatex: vi.fn(),
  disableMermaid: vi.fn(),
  enableKatex: vi.fn(),
  enableMermaid: vi.fn(),
  getMarkdown: vi.fn(),
  MarkdownRender: { render: () => null },
  normalizeStandaloneBackslashT: vi.fn((value: string) => value),
  setDefaultMathOptions: vi.fn(),
  setKaTeXWorker: vi.fn(),
  setMermaidWorker: vi.fn(),
}));
vi.mock('markstream-vue/index.px.css', () => ({}));

function sourceLines(count: number): string[] {
  return Array.from(
    { length: count },
    (_, index) => `+line-${index.toString().padStart(6, '0')}`,
  );
}

function markdownApp(source: string) {
  const app = createSSRApp({
    render: () => h(Markdown, { text: `\`\`\`diff\n${source}\n\`\`\`` }),
  });
  app.use(createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        filePreview: { copyCode: 'Copy code' },
        tools: { output: { linesOmitted: '{count} lines omitted from this browser view.' } },
      },
    },
  }));
  app.provide('resolveImage', undefined);
  return app;
}

afterEach(() => {
  vi.clearAllMocks();
});

describe('bounded assistant Markdown diff fences', () => {
  it('keeps all fifty rows without an omission', () => {
    const rows = buildMarkdownDiffPresentationRows(sourceLines(50).join('\n'));

    expect(rows).toHaveLength(MARKDOWN_DIFF_HEAD_LINE_COUNT + MARKDOWN_DIFF_TAIL_LINE_COUNT);
    expect(rows.some((row) => row.type === 'omission')).toBe(false);
  });

  it('switches at fifty-one lines to 25 head, one omission, and 25 tail rows', () => {
    const rows = buildMarkdownDiffPresentationRows(sourceLines(51).join('\n'));

    expect(rows).toHaveLength(51);
    expect(rows[24]).toMatchObject({ type: 'add', text: 'line-000024' });
    expect(rows[25]).toEqual({ type: 'omission', sign: '', text: '', omittedLineCount: 1 });
    expect(rows[26]).toMatchObject({ type: 'add', text: 'line-000026' });
  });

  it('mounts only the fixed DOM window for one hundred thousand source lines', async () => {
    const source = sourceLines(100_000).join('\n');
    const html = await renderToString(markdownApp(source));

    expect(html.match(/class="[^"]*\bdiff-add\b[^"]*"/gu)).toHaveLength(50);
    expect(html.match(/class="[^"]*\bdiff-omission\b[^"]*"/gu)).toHaveLength(1);
    expect(html).toContain('line-000000');
    expect(html).toContain('line-000024');
    expect(html).not.toContain('line-000025');
    expect(html).not.toContain('line-099974');
    expect(html).toContain('line-099975');
    expect(html).toContain('line-099999');
    expect(html).toContain('99950 lines omitted from this browser view.');
  });

  it('copies the complete fence source, including its unmounted middle', async () => {
    const source = sourceLines(100_000).join('\n');
    const write = vi.fn().mockResolvedValue(true);

    expect(source).toContain('line-050000');
    await expect(copyCompleteMarkdownDiffFence(source, write)).resolves.toBe(true);
    expect(write).toHaveBeenCalledOnce();
    expect(write).toHaveBeenCalledWith(source);
    expect(String(write.mock.calls[0]?.[0])).not.toContain(
      'lines omitted from this browser view',
    );
  });

  it('offers copy but no full-expand action', async () => {
    const html = await renderToString(markdownApp(sourceLines(51).join('\n')));

    expect(html).toContain('aria-label="Copy code"');
    expect(html).not.toMatch(/expand|show all|full output/iu);
  });
});
