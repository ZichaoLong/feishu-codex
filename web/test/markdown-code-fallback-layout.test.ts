import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { compileStyle, parse } from '@vue/compiler-sfc';
import { describe, expect, it } from 'vitest';

const filename = fileURLToPath(new URL('../src/components/chat/Markdown.vue', import.meta.url));
const markdown = readFileSync(
  filename,
  'utf8',
);
const descriptor = parse(markdown, { filename }).descriptor;
const scopedStyle = descriptor.styles.find((style) => style.scoped);
if (!scopedStyle) throw new Error('Markdown.vue must retain its scoped style');
const compiledCss = compileStyle({
  id: 'data-v-markdown-test',
  filename,
  source: scopedStyle.content,
  scoped: true,
}).code;

describe('Markdown code fallback layout', () => {
  it('keeps markstream line numbers out of the fallback code column', () => {
    expect(compiledCss).toMatch(
      /pre\.code-pre-fallback\.markstream-pre--line-numbers[\s\S]*?--markstream-pre-line-number-top:\s*12px;[\s\S]*?--markstream-code-padding-left:\s*calc\(6ch \+ 2px\);[\s\S]*?padding-left:\s*var\(--markstream-code-padding-left\);/u,
    );
  });

  it('gives the stand-alone fallback the same readable code surface', () => {
    expect(compiledCss).toMatch(
      /pre\.code-pre-fallback[^{]*\{[\s\S]*?box-sizing:\s*border-box;[\s\S]*?padding:\s*12px 14px;[\s\S]*?background:\s*var\(--color-surface-sunken\);[\s\S]*?font-size:\s*var\(--ui-font-size\) !important;[\s\S]*?line-height:\s*calc\(var\(--ui-font-size\) \* 1\.65\) !important;/u,
    );
  });

  it('does not add a second frame around fallback inside the normal code container', () => {
    expect(compiledCss).toMatch(
      /\.md\[data-v-markdown-test\] \.code-block-container pre\.code-pre-fallback\s*\{[\s\S]*?margin:\s*0;[\s\S]*?border:\s*0;[\s\S]*?border-radius:\s*0;/,
    );
    expect(compiledCss.indexOf('.code-block-container pre.code-pre-fallback')).toBeGreaterThan(
      compiledCss.indexOf('pre.code-pre-fallback {'),
    );
  });
});
