import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { compileStyle, parse } from '@vue/compiler-sfc';
import { describe, expect, it } from 'vitest';

const filename = fileURLToPath(new URL('../src/components/chat/Markdown.vue', import.meta.url));
const markdown = readFileSync(filename, 'utf8');
const descriptor = parse(markdown, { filename }).descriptor;
const scopedStyle = descriptor.styles.find((style) => style.scoped);
if (!scopedStyle) throw new Error('Markdown.vue must retain its scoped style');
const compiledCss = compileStyle({
  id: 'data-v-markdown-heading-test',
  filename,
  source: scopedStyle.content,
  scoped: true,
}).code;

describe('Markdown heading hierarchy', () => {
  it('keeps headings bold and second- and third-level headings distinct from prose', () => {
    expect(markdown).toMatch(
      /\.md :deep\(h1\),\s*\.md :deep\(h2\),\s*\.md :deep\(h3\),\s*\.md :deep\(h4\) \{[\s\S]*?font-weight:\s*var\(--weight-semibold\);[\s\S]*?\}/u,
    );
    expect(compiledCss).toMatch(
      /h2[^{]*\{[\s\S]*?font-size:\s*max\(var\(--text-xl\), calc\(var\(--content-font-size\) \+ 4px\)\);[\s\S]*?margin-top:\s*1\.1em;/u,
    );
    expect(compiledCss).toMatch(
      /h3[^{]*\{[\s\S]*?font-size:\s*max\(var\(--text-lg\), calc\(var\(--content-font-size\) \+ 2px\)\);[\s\S]*?margin-top:\s*1em;/u,
    );
  });
});
