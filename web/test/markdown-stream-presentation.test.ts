import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const markdown = readFileSync(
  fileURLToPath(new URL('../src/components/chat/Markdown.vue', import.meta.url)),
  'utf8',
);

describe('Markdown stream presentation boundary', () => {
  it('keeps turn finality without adding a second smooth-stream queue', () => {
    expect(markdown).toMatch(/:final="final"/u);
    expect(markdown).toMatch(/:smooth-streaming="false"/u);
    expect(markdown).not.toMatch(/:smooth-streaming="streaming"/u);
  });
});
