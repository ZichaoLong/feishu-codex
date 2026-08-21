import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

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

describe('ConversationPane bounded history navigation surface', () => {
  it('accepts an external outline while retaining the local-turn fallback', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');

    expect(pane).toContain('conversationTocItems?: ConversationTocItem[];');
    expect(pane).toContain(
      'resolveConversationTocTarget?: (turnId: string) => Promise<boolean>;',
    );
    expect(pane).toContain(
      '() => props.conversationTocItems ?? localConversationTocItems.value',
    );
    expect(pane).toContain(':items="displayedConversationTocItems"');
  });

  it('surfaces the external outline limit in both locales', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const toc = source('../src/components/chat/ConversationToc.vue');
    const en = source('../src/i18n/locales/en/conversation.ts');
    const zh = source('../src/i18n/locales/zh/conversation.ts');

    expect(pane).toContain('conversationTocTruncated?: boolean;');
    expect(pane).toContain(':truncated="conversationTocTruncated"');
    expect(toc).toContain('truncated?: boolean;');
    expect(toc).toContain('v-if="truncated" class="toc-truncated"');
    expect(toc).toContain("t('conversation.tocTruncated')");
    expect(toc).toContain("t('conversation.loadMoreOutline')");
    expect(pane).toContain(':has-more="conversationTocHasMore"');
    expect(pane).toContain('@load-more="loadMoreConversationToc?.()"');
    expect(en).toContain("tocTruncated: 'Only the latest 200 prompts are shown'");
    expect(zh).toContain("tocTruncated: '仅显示最近 200 条 Prompt'");
  });

  it('keeps the wide rail and adds one shared compact Prompt history dialog', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const toc = source('../src/components/chat/ConversationToc.vue');
    const en = source('../src/i18n/locales/en/conversation.ts');
    const zh = source('../src/i18n/locales/zh/conversation.ts');

    expect(toc).toContain('const railVisible = computed(');
    expect(toc).toContain('const compactVisible = computed(');
    expect(toc).toContain('(props.mobile || !fits.value)');
    expect(toc).toContain('v-if="railVisible"');
    expect(toc).toContain('v-if="compactVisible"');
    expect(toc).toContain('class="toc-compact-trigger"');
    expect(toc).toContain('<Dialog');
    expect(toc).toContain('<Menu class="toc-compact-list"');
    expect(toc).toContain('<MenuItem');
    expect(toc).toContain("t('conversation.promptHistory')");
    expect(pane).toContain(':select-target="scrollToTurn"');
    expect(en).toContain("promptHistory: 'Prompt history'");
    expect(zh).toContain("promptHistory: 'Prompt 历史'");

    const compactVisibility = between(
      toc,
      'const compactVisible = computed(',
      'async function selectItem',
    );
    expect(compactVisibility).not.toContain('occluded');
  });

  it('closes the compact dialog only after the latest target is installed', () => {
    const toc = source('../src/components/chat/ConversationToc.vue');
    const handler = between(
      toc,
      'async function selectItem(turnId: string): Promise<void> {',
      '// The nav is rendered only while',
    );

    const awaitResolver = handler.indexOf(
      'installed = await props.selectTarget(turnId);',
    );
    const closeAfterReceipt = handler.indexOf(
      'if (installed && generation === selectionGeneration) compactOpen.value = false;',
    );

    expect(awaitResolver).toBeGreaterThanOrEqual(0);
    expect(closeAfterReceipt).toBeGreaterThan(awaitResolver);
    expect(handler).toContain("else emit('select', turnId);");

    const dismissalFence = between(
      toc,
      '// A historical resolver may outlive an Esc/overlay/close-button dismissal.',
      'onBeforeUnmount(() =>',
    );
    expect(dismissalFence).toContain('watch(');
    expect(dismissalFence).toContain('compactOpen,');
    expect(dismissalFence).toContain('if (wasOpen && !isOpen) selectionGeneration += 1;');
    expect(dismissalFence).toContain("{ flush: 'sync' }");
  });

  it('keeps the mobile Prompt trigger below the separate mobile top bar', () => {
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const toc = source('../src/components/chat/ConversationToc.vue');
    const topBar = source('../src/components/mobile/MobileTopBar.vue');

    const mobileTopBar = app.indexOf('<MobileTopBar');
    const main = app.indexOf('<main class="focus-main">', mobileTopBar);
    const conversationPane = app.indexOf('<ConversationPane', main);
    expect(mobileTopBar).toBeGreaterThanOrEqual(0);
    expect(main).toBeGreaterThan(mobileTopBar);
    expect(conversationPane).toBeGreaterThan(main);
    expect(app).toContain('.focus-app.mobile {\n  display: flex;\n  flex-direction: column;');
    expect(topBar).toContain('height: calc(50px + var(--safe-top));');
    expect(pane).toContain('position: relative;\n  container-type: inline-size;');
    expect(toc).toContain('.toc-compact-trigger.is-mobile { top: var(--space-3); }');
  });

  it('sends every target intent to the owner before locating and scrolling its render', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const handler = between(
      pane,
      'async function scrollToTurn(turnId: string): Promise<boolean> {',
      'function currentLayoutKey()',
    );

    const resolveTarget = handler.indexOf('const installed = await resolver(turnId);');
    const receiptGuard = handler.indexOf('if (!installed) return false;', resolveTarget);
    const renderTick = handler.indexOf('await nextTick();', resolveTarget);
    const resolvedLookup = handler.indexOf(
      'const target = findTurnTarget(renderedPane, turnId);',
      renderTick,
    );
    const scroll = handler.indexOf(
      "target.scrollIntoView({ behavior: 'smooth', block: 'center' });",
      resolvedLookup,
    );

    expect(resolveTarget).toBeGreaterThanOrEqual(0);
    expect(receiptGuard).toBeGreaterThan(resolveTarget);
    expect(renderTick).toBeGreaterThan(receiptGuard);
    expect(resolvedLookup).toBeGreaterThan(renderTick);
    expect(scroll).toBeGreaterThan(resolvedLookup);
    expect(handler).toContain('return true;');
  });

  it('leaves a replacement history window before following the rendered live tail', () => {
    const client = source('../src/focus/useFocusWebClient.ts');
    const app = source('../src/focus/FocusApp.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');

    const clientHandler = between(
      client,
      'function returnToLiveTail(): void {',
      'let turnWindowChangeGeneration',
    );
    expect(clientHandler).toContain('historyNavigation.clearHistoryWindow();');
    expect(client).toContain('returnToLiveTail,');
    expect(app).toContain(':return-to-live-tail="client.returnToLiveTail"');
    expect(pane).toContain('returnToLiveTail?: () => void;');

    const paneHandler = between(
      pane,
      'async function handleReturnToLiveTail(): Promise<void> {',
      'async function handleLoadOlderMessages',
    );
    const clearWindow = paneHandler.indexOf('props.returnToLiveTail?.();');
    const renderTick = paneHandler.indexOf('await nextTick();', clearWindow);
    const scroll = paneHandler.indexOf('scrollToBottom(true);', renderTick);
    const settle = paneHandler.indexOf('scheduleStableFollow(16);', scroll);
    expect(clearWindow).toBeGreaterThanOrEqual(0);
    expect(renderTick).toBeGreaterThan(clearWindow);
    expect(scroll).toBeGreaterThan(renderTick);
    expect(settle).toBeGreaterThan(scroll);
    expect(pane).toContain('@click="handleReturnToLiveTail"');
  });

  it('keeps an invisible DOM anchor for a Prompt with no visible user content', () => {
    const pane = source('../src/components/chat/ChatPane.vue');

    expect(pane).toContain('function userTurnHasPresentation(turn: ChatTurn): boolean');
    expect(pane).toContain('v-if="userTurnHasPresentation(turn)" class="u-turn"');
    expect(pane).toContain('class="turn-anchor empty-user-anchor"');
    expect(pane).toContain(':data-turn-id="turn.id"');
    expect(pane).toContain('aria-hidden="true"');
  });

  it('lands replacement-style top loads at the new page bottom without prepend restoration', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const handler = between(
      pane,
      'async function handleLoadOlderMessages(): Promise<void> {',
      'function attrEscape(value: string)',
    );

    const load = handler.indexOf('const installed = await props.loadOlderMessages(requestedSessionId);');
    const renderTick = handler.indexOf('await nextTick();', load);
    const sessionGuard = handler.indexOf(
      'if (props.sessionId !== requestedSessionId) return;',
      renderTick,
    );
    const receiptGuard = handler.indexOf('if (!installed) return;', sessionGuard);
    const bottom = handler.indexOf('el2.scrollTop = el2.scrollHeight;', receiptGuard);

    expect(load).toBeGreaterThanOrEqual(0);
    expect(renderTick).toBeGreaterThan(load);
    expect(sessionGuard).toBeGreaterThan(renderTick);
    expect(receiptGuard).toBeGreaterThan(sessionGuard);
    expect(bottom).toBeGreaterThan(receiptGuard);
    expect(pane).toContain("'history-loading': historyLoadInProgress");
    expect(pane).toContain('if (historyLoadInProgress.value) return;');
    expect(pane).not.toContain('HistoryScrollSnapshot');
    expect(pane).not.toContain('pendingHistoryRestoreBySession');
    expect(pane).not.toContain('historyScrollDelta');
    expect(pane).not.toContain('history-prepending');
  });
});
