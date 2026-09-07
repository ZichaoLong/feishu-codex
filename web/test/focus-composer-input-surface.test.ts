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

  it('keeps the mobile compact editor at four lines until it is explicitly expanded', () => {
    const composer = source('../src/components/chat/Composer.vue');
    const mobileStart = composer.lastIndexOf('@media (max-width: 640px)');
    expect(mobileStart).toBeGreaterThanOrEqual(0);
    const mobileStyles = composer.slice(mobileStart);

    expect(mobileStyles).toContain('min-height: min(96px, 16dvh);');
    expect(mobileStyles).toContain('max-height: min(96px, 16dvh);');
    expect(mobileStyles).toContain(`.composer.expanded .ph {
    min-height: min(280px, 34dvh);
    max-height: min(280px, 34dvh);
  }`);
    expect(composer).toContain('v-if="mobile || expanded || isGrown"');
    expect(composer).toContain('max-height: calc(100vh / 4);');
    expect(composer).toContain('overflow-y: auto;');
  });

  it('keeps one parent-owned mobile surface state while hiding only presentation', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const types = source('../src/types.ts');

    expect(types).toContain(
      "export type ComposerSurfaceMode = 'compact' | 'expanded' | 'hidden';",
    );
    expect(pane).toContain(
      "const composerSurfaceMode = ref<ComposerSurfaceMode>('compact');",
    );
    expect(pane.match(/:surface-mode="composerSurfaceMode"/gu)).toHaveLength(2);
    expect(pane.match(/@surface-mode-change="setComposerSurfaceMode"/gu)).toHaveLength(2);
    expect(pane.match(/@draft-state="handleComposerDraftState"/gu)).toHaveLength(2);
    expect(dock).toContain(':surface-mode="surfaceMode"');
    expect(dock).toContain("@surface-mode-change=\"emit('surfaceModeChange', $event)\"");
    expect(dock).toContain("@draft-state=\"emit('draftState', $event)\"");
    expect(composer).toContain("'surface-hidden': surfaceMode === 'hidden'");
    expect(composer).toContain('.composer.surface-hidden {\n  display: none;\n}');
    expect(composer).toContain(
      "watch(hasDraft, (present) => emit('draftState', present), { immediate: true });",
    );

    // Question/approval interactions keep visual priority without unmounting
    // the exact Composer submission owner while an async mutation settles.
    expect(dock).toContain('<QuestionCard\n      v-if="pendingQuestion"');
    expect(dock).toContain('<ApprovalCard\n      v-else-if="pendingApproval"');
    expect(dock).toContain('<Composer\n      v-show="!pendingQuestion && !pendingApproval"');
    expect(dock).toContain(':interaction-pending="!!pendingQuestion || !!pendingApproval"');
    expect(dock).toContain('if (props.pendingQuestion || props.pendingApproval) return null;');
    expect(dock).not.toContain('v-if="surfaceMode');
  });

  it('restores hidden input for every imperative draft/focus path and new attachments', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const composer = source('../src/components/chat/Composer.vue');

    expect(pane).toContain("if (composerSurfaceMode.value === 'hidden') {");
    expect(pane).toContain("setComposerSurfaceMode('compact');");
    expect(pane).toContain('function loadComposerForEdit(');
    expect(pane).toContain('function loadComposerRecovery(');
    expect(pane).toContain('function focusComposer(): void {');
    expect(pane).toMatch(
      /function focusComposer\(\): void \{\s+\/\/[\s\S]*?if \(pendingQuestion\.value \|\| pendingApproval\.value\) return;/u,
    );
    expect(pane).toContain('watch(attachmentUpload.isDragOver');
    expect(pane).toContain('watch(() => attachmentUpload.attachments.value.length');
    expect(pane).toContain("watch(() => props.composerSessionId ?? props.sessionId ?? '',");
    expect(pane).toContain('watch(() => props.mobile,');
    expect(pane).not.toMatch(
      /watch\(\(\) => props\.composerSessionId[\s\S]*?composerHasDraft\.value = false;/u,
    );
    expect(composer).toMatch(
      /function loadForEdit\(value: string\): void \{\s+requestSurfaceMode\('compact'\);/u,
    );
    expect(composer).toMatch(
      /function focus\(\): void \{\s+requestSurfaceMode\('compact'\);/u,
    );
    expect(composer).toContain('loadForEdit(value);\n  return true;');
  });

  it('offers a safe mobile hide/restore surface with a separate hidden Stop action', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');
    const toc = source('../src/components/chat/ConversationToc.vue');
    const en = source('../src/i18n/locales/en/composer.ts');
    const zh = source('../src/i18n/locales/zh/composer.ts');

    expect(composer).toContain('v-if="allowHide"');
    expect(composer).toContain(':disabled="starting || submissionPending"');
    expect(composer).toContain('@click="hideComposer"');
    expect(dock).toContain(':allow-hide="allowHide"');
    expect(pane.match(/:allow-hide="allowComposerHide"/gu)).toHaveLength(1);
    expect(pane).toContain(`const allowComposerHide = computed(() => (
  props.mobile === true
  && !showTargetlessComposer.value
  && !props.sessionLoading
  && props.turns.length > 0
));`);
    expect(pane).toContain('const showComposerRestore = computed(() => (');
    expect(pane).toContain("if (mode === 'hidden' && !allowComposerHide.value) return;");
    expect(pane).toContain("?.querySelector<HTMLButtonElement>('.mobile-composer-restore')");
    expect(pane).toContain('mobile-composer-restore');
    expect(pane).toContain(':class="{ \'has-draft\': composerHasDraft }"');
    expect(pane).toContain("t(composerHasDraft ? 'composer.continueInput' : 'composer.showInput')");
    expect(pane).toContain('class="mobile-composer-stop"');
    expect(pane).toContain('class="mobile-composer-stop"\n          size="lg"');
    expect(pane).toContain('@click="handleInterrupt"');
    expect(pane).toContain(`const showHiddenComposerInterrupt = computed(() => (
  showComposerRestore.value
  && (props.running || props.starting || props.interruptEnabled)
  && props.composerCapabilities?.interrupt !== false
));`);
    expect(pane).toContain('<Tooltip v-if="showHiddenComposerInterrupt"');
    expect(pane).toContain('left: max(var(--space-4), var(--safe-left));');
    expect(toc).toContain('.toc-compact-trigger.is-mobile { top: var(--space-3); }');
    expect(toc).toContain('right: var(--space-4);');
    expect(pane).toContain('.mobile-composer-restore {\n  min-height: 44px;\n}');
    expect(pane).toContain('@media (max-width: 360px)');
    expect(pane).toContain('flex-direction: column;');
    expect(dock).toContain("'composer-hidden': surfaceMode === 'hidden'");
    expect(dock).toContain('padding-bottom: max(var(--space-3), var(--safe-bottom));');
    expect(composer).toContain('id="composer-input"');
    expect(composer.match(/aria-controls="composer-input"/gu)).toHaveLength(2);
    expect(pane).toContain('aria-controls="composer-input"');
    expect(composer).toContain(':aria-label="t(\'composer.inputLabel\')"');
    expect(composer).toContain(`.expand-btn,
  .hide-input-btn {
    width: 44px;
    height: 44px;
  }`);
    expect(composer).toContain("closeSurfaceOverlays();\n  requestSurfaceMode('hidden');");
    expect(composer).toContain(
      '[() => props.surfaceMode, () => props.mobile, () => props.interactionPending]',
    );
    expect(composer).toContain('Parent-driven resets and desktop/mobile transitions');
    expect(composer).toMatch(
      /function collapseAndRefit\(\): void \{[\s\S]*?requestSurfaceMode\('compact'\);/u,
    );

    expect(en).toContain("inputLabel: 'Message input'");
    expect(en).toContain("collapseTitle: 'Return to the four-line input'");
    expect(en).toContain("hideInput: 'Hide input'");
    expect(en).toContain("showInput: 'Show input'");
    expect(en).toContain("continueInput: 'Continue input'");
    expect(zh).toContain("inputLabel: '消息输入框'");
    expect(zh).toContain("collapseTitle: '收拢为四行输入框'");
    expect(zh).toContain("hideInput: '隐藏输入框'");
    expect(zh).toContain("showInput: '显示输入框'");
    expect(zh).toContain("continueInput: '继续输入'");
  });
});
