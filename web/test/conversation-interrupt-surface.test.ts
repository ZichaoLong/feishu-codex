import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Conversation interrupt surface', () => {
  it('keeps interrupt behind the explicit Stop action without a document-global Escape path', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const composer = source('../src/components/chat/Composer.vue');

    expect(pane).not.toContain("document.addEventListener('keydown'");
    expect(pane).not.toContain("event.key === 'Escape'");
    expect(pane).not.toContain('manuallyAborted');
    expect(pane).toContain("emit('interrupt')");
    expect(composer).toContain("@click=\"emit('interrupt')\"");
  });
});
