import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

describe('Focus Composer input surface', () => {
  it('keeps Enter and the send button on one server-routed prompt chain', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const submission = source('../src/focus/focusComposerSubmission.ts');
    const submissionOwner = source('../src/components/chat/composerSubmission.ts');

    expect(composer).toContain("if (e.key === 'Enter' && !e.shiftKey) {");
    expect(composer).toContain('@click="handleSubmit()"');
    expect(composer).toContain("emit('submit', submission)");
    expect(composer).toContain('submissionPending.value = true');
    expect(composer).toContain('useComposerSubmissionRevision({');
    expect(composer).toContain('submissionRevision.isCurrent(sourceRevision)');
    expect(composer).not.toContain('const sourceText = text.value');
    expect(composer).not.toContain('const sourceAttachmentIds =');
    expect(composer).toContain('if (!props.deferSubmitClear) submission.commit()');
    expect(composer).not.toContain('function handleSteer');
    expect(composer).not.toContain("emit('steer'");
    expect(composer).not.toContain('capabilities.value.steer');
    expect(composer).not.toContain("e.key === 's'");
    expect(composer).not.toContain('expanded.value && !(e.metaKey || e.ctrlKey)');
    expect(composer).not.toMatch(/enqueues?/iu);
    expect(composer).not.toContain('Enter inserting newlines');

    expect(app).toContain('@submit="handleSubmit"');
    expect(app).toContain(':defer-submit-clear="true"');
    expect(submission).toContain(
      'if (await submit(payload.text, payload.attachments)) payload.commit()',
    );
    expect(app).not.toContain('@steer="handleSubmit"');
    expect(pane.match(/@submit="handleComposerSubmit"/gu)).toHaveLength(2);
    expect(pane).not.toContain('@steer=');
    expect(dock).toContain("@submit=\"emit('submit', $event)\"");
    expect(dock).not.toContain('@steer=');

    const handler = app.indexOf('async function handleSubmit(');
    const handlerEnd = app.indexOf('\n}\n\nasync function loadComposerDraft(', handler);
    const handlerSource = app.slice(handler, handlerEnd);
    expect(handler).toBeGreaterThanOrEqual(0);
    expect(handlerEnd).toBeGreaterThan(handler);
    expect(handlerSource).toContain('await dispatchFocusComposerPayload(');
    expect(handlerSource).toContain('(text, attachments) => client.submit(');
    expect(handlerSource).toContain('() => payload.retainTextOnly()');
    expect(handlerSource).not.toContain('captureFocusPromptSubmissionIntent');
    expect(handlerSource).not.toContain('submissionIntent');
    expect(handlerSource).not.toContain('active_turn_id');
    expect(submissionOwner).toContain('clearCurrentAttachments: () => void');
    expect(submissionOwner).toContain('function retainTextOnly(): boolean');
  });

  it('keeps Stop visible for running, submitting, and identity-unknown interruption', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const composer = source('../src/components/chat/Composer.vue');

    expect(app).toContain('interrupt: client.canInterrupt.value');
    expect(app).toContain(':starting="client.starting.value"');
    expect(pane).toContain(':interrupt-enabled="interruptEnabled"');
    expect(composer).toContain(
      'v-if="(running || starting || interruptEnabled) && capabilities.interrupt"',
    );
    expect(composer).toContain('@click="emit(\'interrupt\')"');
  });

  it('leaves Shift+Enter as newline and removes Composer queue/shortcut vocabulary', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const types = source('../src/types.ts');
    const en = source('../src/i18n/locales/en/composer.ts');
    const zh = source('../src/i18n/locales/zh/composer.ts');

    expect(composer).toContain("if (e.key === 'Enter' && !e.shiftKey) {");
    expect(composer).not.toContain('queued?: QueuedPromptView[]');
    expect(types).not.toContain('steer: boolean;');
    expect(pane.match(/:queued="queued"/gu)).toHaveLength(1);
    expect(en).toContain("placeholderRunning: 'Enter steers the current turn'");
    expect(zh).toContain("placeholderRunning: 'Enter 插入当前回合'");
    expect(en).not.toContain('Ctrl+S');
    expect(zh).not.toContain('Ctrl+S');
  });
});
