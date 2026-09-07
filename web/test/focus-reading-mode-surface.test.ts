import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import enFocus from '../src/i18n/locales/en/focus';
import zhFocus from '../src/i18n/locales/zh/focus';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

function between(value: string, start: string, end: string): string {
  const startIndex = value.indexOf(start);
  const endIndex = value.indexOf(end, startIndex);
  expect(startIndex).toBeGreaterThanOrEqual(0);
  expect(endIndex).toBeGreaterThan(startIndex);
  return value.slice(startIndex, endIndex);
}

describe('Focus page-level reading mode surface', () => {
  it('keeps the ordinary session switch surfaces while adding one page-owned mode', () => {
    const app = source('../src/focus/FocusApp.vue');
    const header = source('../src/components/chat/ChatHeader.vue');
    const mobileTopBar = source('../src/components/mobile/MobileTopBar.vue');

    expect(app).toContain("type FocusPresentationMode = 'normal' | 'reading';");
    expect(app).toContain("const presentationMode = ref<FocusPresentationMode>('normal');");
    expect(app).toContain(
      "const readingMode = computed(() => presentationMode.value === 'reading');",
    );
    expect(app).not.toMatch(/readingMode[\s\S]{0,80}(localStorage|STORAGE_KEYS)/u);

    const mobileTopBarIndex = app.indexOf('<MobileTopBar');
    const mainIndex = app.indexOf('<main class="focus-main">', mobileTopBarIndex);
    const conversationPaneIndex = app.indexOf('<ConversationPane', mainIndex);
    expect(mobileTopBarIndex).toBeGreaterThanOrEqual(0);
    expect(mainIndex).toBeGreaterThan(mobileTopBarIndex);
    expect(conversationPaneIndex).toBeGreaterThan(mainIndex);

    expect(mobileTopBar).toContain('class="tb-mid"');
    expect(mobileTopBar).toContain("@click=\"emit('openSwitcher')\"");
    expect(mobileTopBar).toContain('readingModeEnabled?: boolean;');
    expect(mobileTopBar).toContain("@click=\"emit('enterReadingMode')\"");
    expect(header).toContain('readingModeEnabled?: boolean;');
    expect(header).toContain("@click=\"emit('enterReadingMode')\"");
    expect(app).toContain(':reading-mode-enabled="canEnterReadingMode"');
    expect(app).toContain('@enter-reading-mode="enterReadingMode"');
  });

  it('hides shell chrome but keeps the transcript and its body actions mounted', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');

    expect(app).toContain("'reading-mode': readingMode");
    expect(app).toMatch(/<Sidebar\s+v-show="!readingMode"/u);
    expect(app).toContain('v-show="!sidebarCollapsed && !readingMode"');
    expect(app).toMatch(/<MobileTopBar\s+v-else\s+v-show="!readingMode"/u);
    expect(app).toContain('v-if="isMobile && !readingMode"');
    expect(app).toMatch(/<FocusPrimaryNotices\s+v-if="!readingMode"/u);
    expect(app).toContain('v-if="unsupportedNotice && !readingMode"');

    expect(pane).toContain("<section class=\"con\" :class=\"{ mobile, 'reading-mode': readingMode }\">");
    expect(pane).toMatch(/<ChatHeader[\s\S]*?v-show="!readingMode"/u);
    expect(pane).toMatch(/<ConversationToc\s+v-if="conversationToc && !readingMode"/u);
    expect(pane).toMatch(/<ChatDock\s+v-if="!showTargetlessComposer"\s+v-show="!readingMode"/u);
    expect(pane).toContain(':surface-mode="readingMode ? \'hidden\' : composerSurfaceMode"');
    expect(pane).not.toContain('.con.reading-mode :deep(.chat)');

    // The exact async submission and attachment owners stay mounted even while
    // their presentation is hidden.
    expect(dock).toContain('<Composer\n      v-show="!pendingQuestion && !pendingApproval"');
    expect(pane.match(/useAttachmentUpload\(/gu)).toHaveLength(1);
    expect(pane).not.toMatch(/v-if="!readingMode"[^>]*>\s*<ChatDock/gu);

    expect(pane).toContain('<ChatPane');
    expect(pane).toContain('@open-tool-diff="emit(\'openToolDiff\', $event)"');
    expect(pane).toContain('v-if="showPill"');
    expect(pane).toContain(':style="{ bottom: `${readingMode ? 12 : dockHeight + 12}px` }"');
    expect(app).toContain('<aside\n        v-if="detailOpen"');
    expect(app).not.toContain('v-if="detailOpen && !readingMode"');
  });

  it('offers separate exit and conversation-switch actions at the reading edge', () => {
    const app = source('../src/focus/FocusApp.vue');
    const controls = source('../src/components/chat/ReadingModeControls.vue');

    expect(app).toMatch(/<ReadingModeControls\s+v-if="readingMode"/u);
    expect(app).toContain('@exit="exitReadingMode"');
    expect(app).toContain('@switch-session="showMobileSwitcher = true"');
    expect(controls).toContain('class="reading-mode-exit"');
    expect(controls).toContain("@click=\"emit('exit')\"");
    expect(controls).toContain("t('focus.exitReadingMode')");
    expect(controls).toContain('data-reading-mode-toggle');
    expect(controls).toContain('class="reading-session-switch"');
    expect(controls).toContain("@click=\"emit('switchSession')\"");
    expect(controls).toContain("t('mobile.openSwitcher')");
    expect(controls).toContain('aria-haspopup="dialog"');
    expect(controls).toContain(':aria-expanded="switcherOpen"');
    expect(controls).toContain('{{ sessionTitle }}');
    expect(controls).toContain('height: 48px;');
    expect(controls).toContain('flex: none;');
    expect(controls).toContain('.reading-mode-controls.is-mobile {');
    expect(controls).toContain('height: calc(50px + var(--safe-top));');
    expect(controls).toContain(
      'padding: var(--safe-top) max(12px, var(--safe-right)) 0 max(12px, var(--safe-left));',
    );

    const switcher = between(app, '<MobileSwitcherSheet', '</MobileSwitcherSheet>');
    expect(switcher).toContain('v-if="isMobile || readingMode"');
    expect(switcher).toContain('@select="client.selectThread($event)"');
    expect(switcher).toContain(':allow-create="!readingMode"');
    expect(switcher).toContain(':allow-session-actions="!readingMode"');

    const exit = between(
      app,
      'function exitReadingMode(): void {',
      '\n}\nconst conversationSearchVisible',
    );
    expect(exit.indexOf('showMobileSwitcher.value = false;')).toBeLessThan(
      exit.indexOf("presentationMode.value = 'normal';"),
    );

    expect(enFocus.enterReadingMode).toBe('Enter reading mode');
    expect(enFocus.exitReadingMode).toBe('Exit reading mode');
    expect(zhFocus.enterReadingMode).toBe('进入阅读模式');
    expect(zhFocus.exitReadingMode).toBe('退出阅读模式');
    expect(Object.keys(enFocus).sort()).toEqual(Object.keys(zhFocus).sort());
  });

  it('keeps reading across a loaded-session switch and exits only for a settled empty target', () => {
    const app = source('../src/focus/FocusApp.vue');

    expect(app).toContain(`const canEnterReadingMode = computed(() => (
  currentDocumentAccessAvailable.value
  && Boolean(client.activeThreadId.value)
  && !client.conversationLoading.value
  && client.turns.value.length > 0
));`);

    const activeThreadWatcher = between(
      app,
      'watch(client.activeThreadId,',
      'watch(\n  [() => client.conversationLoading.value',
    );
    expect(activeThreadWatcher).toContain('if (!threadId) exitReadingMode();');
    expect(activeThreadWatcher).not.toContain("presentationMode.value = 'normal'");

    const settledEmptyWatcher = between(
      app,
      'watch(\n  [() => client.conversationLoading.value',
      'watch(currentDocumentAccessAvailable',
    );
    expect(settledEmptyWatcher).toContain(
      'if (readingMode.value && !loading && turnCount === 0) exitReadingMode();',
    );
    expect(settledEmptyWatcher).not.toContain("flush: 'sync'");

    const openDraft = between(
      app,
      'async function openWorkspaceDraft(workspace: string)',
      '\n}\n\nasync function handleSubmit',
    );
    expect(openDraft.indexOf('exitReadingMode();')).toBeLessThan(
      openDraft.indexOf('await client.openWorkspaceDraft(workspace);'),
    );

    const accessWatcher = between(
      app,
      'watch(currentDocumentAccessAvailable',
      'watch(conversationSearchVisible',
    );
    expect(accessWatcher).toContain('exitReadingMode();');
    expect(app).toContain("presentationMode.value = 'reading';");
    expect(app).toContain("presentationMode.value = 'normal';");
  });

  it('keeps draft/submission owners mounted and reveals input before imperative focus', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const dock = source('../src/components/chat/ChatDock.vue');
    const composer = source('../src/components/chat/Composer.vue');

    expect(composer).toContain('requestInput: [];');
    expect(dock).toContain('requestInput: [];');
    expect(dock).toContain("@request-input=\"emit('requestInput')\"");
    expect(pane.match(/@request-input="emit\('exitReadingMode'\)"/gu)).toHaveLength(2);

    expect(composer).toMatch(
      /function loadForEdit\(value: string\): void \{\s+emit\('requestInput'\);\s+requestSurfaceMode\('compact'\);/u,
    );
    expect(composer).toMatch(
      /function focus\(\): void \{\s+emit\('requestInput'\);\s+requestSurfaceMode\('compact'\);\s+void nextTick/u,
    );
    const recovery = between(
      composer,
      'function loadRecovery(value: string): boolean {',
      '\n}\ndefineExpose',
    );
    expect(recovery.indexOf("if (text.value !== '' || attachments.value.length > 0) return false;"))
      .toBeLessThan(recovery.indexOf('loadForEdit(value);'));

    expect(pane).toContain(
      "if (dragging && props.readingMode) emit('exitReadingMode');",
    );
    expect(pane).toContain(
      "if (count > previous && props.readingMode) emit('exitReadingMode');",
    );
    expect(pane).toContain(':surface-mode="readingMode ? \'hidden\' : composerSurfaceMode"');
    expect(pane).toContain('const composerSurfaceMode = ref<ComposerSurfaceMode>(\'compact\');');

    const enter = between(app, 'function enterReadingMode(): void {', '\n}\n\nfunction exitReadingMode');
    const exit = between(app, 'function exitReadingMode(): void {', '\n}\nconst conversationSearchVisible');
    expect(app).toContain('.find((button) => button.offsetParent !== null)');
    expect(enter).not.toContain('focusComposer');
    expect(exit).not.toContain('focusComposer');
  });
});
