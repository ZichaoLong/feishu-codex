import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('ConversationPane attachment ownership', () => {
  it('creates one session-map owner and shares it with empty and docked Composer instances', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const dock = source('../src/components/chat/ChatDock.vue');

    expect(pane.match(/useAttachmentUpload\(/gu)).toHaveLength(1);
    expect(pane.match(/:attachment-upload="attachmentUpload"/gu)).toHaveLength(2);
    expect(composer).toContain('props.attachmentUpload ?? useAttachmentUpload');
    expect(dock).toContain(':attachment-upload="attachmentUpload"');
  });

  it('keeps that owner mounted while confirmed writer-scope readiness gates every input path', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');

    expect(app).toContain(':composer-ready="client.scopeReady.value"');
    expect(app).toContain('if (!client.scopeReady.value) {');
    expect(app).toContain('payload.retain();');
    expect(app).toContain(':defer-submit-clear="true"');
    expect(pane.match(/:composer-ready="composerReady"/gu)).toHaveLength(2);
    expect(dock).toContain(':composer-ready="composerReady"');

    // Readiness is its own contract; it is not inferred from starting/loading.
    expect(pane).toContain('composerReady: true');
    expect(dock).toContain('composerReady: true');
    expect(composer).toContain('composerReady: true');
    expect(pane).toContain('uploadImage: () => props.composerReady ? props.uploadImage : undefined');
    expect(pane).toContain('if (!props.composerReady) return false;');
    expect(composer).toContain('if (sendDisabled.value) return;');
    expect(composer).not.toContain('function handleSteer');
    expect(composer).toContain(':disabled="starting || !composerReady"');
    expect(composer.match(/:disabled="!composerReady"/gu)).toHaveLength(2);

    // The readiness transition must not create a second attachment controller
    // or conditionally unmount either visual Composer site.
    expect(pane.match(/useAttachmentUpload\(/gu)).toHaveLength(1);
    expect(pane).not.toMatch(/v-if="composerReady"[^>]*>\s*<Composer/gu);
    expect(dock).not.toMatch(/v-if="composerReady"[^>]*>\s*<Composer/gu);
  });
});
