import { createSSRApp, h } from 'vue';
import { createI18n } from 'vue-i18n';
import { renderToString } from '@vue/server-renderer';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import FocusConversationSearchPanel, {
  splitConversationSearchSnippet,
} from '../src/focus/FocusConversationSearchPanel.vue';
import FocusDetailPanel from '../src/focus/FocusDetailPanel.vue';
import enFocus from '../src/i18n/locales/en/focus';
import enTools from '../src/i18n/locales/en/tools';
import zhFocus from '../src/i18n/locales/zh/focus';
import zhTools from '../src/i18n/locales/zh/tools';
import EditTool from '../src/components/chat/tool-calls/EditTool.vue';
import GenericTool from '../src/components/chat/tool-calls/GenericTool.vue';
import { resolveToolRenderer } from '../src/components/chat/tool-calls/toolRegistry';
import type {
  FocusConversationSearchOccurrence,
  FocusThreadConversationSearchPage,
} from '../src/focus/types';
import type { ToolCall } from '../src/types';

function source(relativePath: string): string {
  return readFileSync(fileURLToPath(new URL(relativePath, import.meta.url)), 'utf8');
}

const occurrence: FocusConversationSearchOccurrence = {
  turn_id: 'raw-turn',
  item_id: 'final-message',
  snippet: '😀 needle <img src=x onerror=boom>',
  snippet_match_range: { start: 3, end: 9 },
  turn_cursor: 'opaque cursor',
};

function searchPage(): FocusThreadConversationSearchPage {
  return {
    runtime_epoch: 'epoch-1',
    revision: 1,
    thread_id: 'thread-1',
    query: 'needle',
    cursor: null,
    occurrences: [occurrence],
    next_cursor: 'next cursor',
  };
}

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'en',
    messages: {
      en: {
        focus: {
          conversationSearchTitle: 'Search conversation',
          conversationSearchPlaceholder: 'Search prompts and final replies',
          conversationSearchSubmit: 'Search',
          conversationSearchScope: 'Bounded snippets only',
          conversationSearchQueryTooLong: 'Too long',
          conversationSearchUnavailableBuild: 'Build unavailable',
          conversationSearchUnavailableDocument: 'Document unavailable',
          conversationSearchUnavailableLegacy: 'Legacy history cannot be searched',
          conversationSearchUnavailableNoThread: 'No thread selected',
          conversationSearchUnavailableRuntime: 'Runtime unsupported',
          conversationSearchUnavailableMaterializing: 'Thread not materialized',
          conversationSearchUnavailableUnknown: 'Unknown history mode',
          conversationSearchFailed: 'Failed',
          conversationSearchEmpty: 'No results',
          conversationSearchNext: 'Next results',
        },
        thinking: { close: 'Close' },
        tools: {
          label: { bash: 'Run', edit: 'Edit' },
          detail: {
            load: 'Load detail',
            unavailable: 'More detail unavailable',
            loading: 'Loading saved detail…',
          },
          output: {
            linesOmitted: '{count} lines omitted',
            boundedOmitted: '{count} characters omitted from the middle',
            aggregateOmitted: 'All {count} characters omitted by page budget',
          },
          chip: { lines: '{count} lines', edited: 'edited' },
        },
      },
    },
  });
}

describe('bounded conversation inspection surface', () => {
  it('uses admitted UTF-16 offsets and Vue text escaping for search highlights', async () => {
    expect(splitConversationSearchSnippet(occurrence)).toEqual({
      before: '😀 ',
      match: 'needle',
      after: ' <img src=x onerror=boom>',
    });

    const app = createSSRApp({
      render: () => h(FocusConversationSearchPanel, {
        unavailableReason: null,
        loading: false,
        error: false,
        page: searchPage(),
      }),
    });
    app.use(i18n());
    const html = await renderToString(app);

    expect(html).toMatch(/<mark[^>]*>needle<\/mark>/);
    expect(html).toContain('&lt;img src=x onerror=boom&gt;');
    expect(html).not.toContain('<img src=x');
    expect(source('../src/focus/FocusConversationSearchPanel.vue')).not.toContain('v-html');
  });

  it('keeps legacy threads visible as an explicitly unavailable search surface', async () => {
    const app = createSSRApp({
      render: () => h(FocusConversationSearchPanel, {
        unavailableReason: 'legacy_history',
        loading: false,
        error: false,
        page: null,
      }),
    });
    app.use(i18n());
    const html = await renderToString(app);

    expect(html).toContain('Legacy history cannot be searched');
    expect(html).toMatch(/<input[^>]*disabled/);
    expect(html).toMatch(/<button[^>]*disabled/);
  });

  it('prioritizes the closed unavailable reason over stale validation and request errors', async () => {
    const app = createSSRApp({
      render: () => h(FocusConversationSearchPanel, {
        unavailableReason: 'runtime_unsupported',
        loading: false,
        error: true,
        page: null,
      }),
    });
    app.use(i18n());
    const html = await renderToString(app);

    expect(html).toContain('Runtime unsupported');
    expect(html).not.toContain('Failed');
    const panelSource = source('../src/focus/FocusConversationSearchPanel.vue');
    expect(panelSource.indexOf('v-if="unavailableReason"')).toBeLessThan(
      panelSource.indexOf('v-else-if="queryInvalid"'),
    );
  });

  it('renders the exact bilingual legacy explanations for search and omitted detail', async () => {
    expect(zhFocus.conversationSearchUnavailableLegacy).toBe(
      '当前对话使用旧版历史格式，无法搜索。通过当前版本的 Focus 成功新建的对话支持搜索；旧对话不会自动迁移。',
    );
    expect(enFocus.conversationSearchUnavailableLegacy).toContain('older history format');
    expect(zhTools.detail.unavailableLegacy).toContain('旧版对话无法加载更多工具详情');
    expect(enTools.detail.unavailableLegacy).toContain('Older conversations cannot load more tool detail');

    const omittedTool: ToolCall = {
      id: 'legacy-command',
      name: 'exec_command',
      arg: 'long command',
      status: 'ok',
      output: [],
      outputOmittedChars: 90_000,
      outputHeadLineCount: 0,
      inspectionLocator: {
        turn_id: 'raw-turn',
        item_id: 'command-item',
        kind: 'commandExecution',
        change_index: null,
      },
    };
    const app = createSSRApp({
      render: () => h(FocusDetailPanel, {
        target: 'toolDiff',
        thinkingText: '',
        tool: omittedTool,
        toolDetail: null,
        toolDetailChangeIndex: null,
        toolDetailLoading: false,
        toolDetailError: false,
        toolDetailScanStatus: 'idle',
        toolDetailScannedItems: 0,
        toolDetailUnavailableReason: 'legacy_history',
        conversationSearchUnavailableReason: 'legacy_history',
        conversationSearchLoading: false,
        conversationSearchError: false,
        conversationSearchPage: null,
        mediaTarget: null,
        agentMember: null,
      }),
    });
    app.use(createI18n({
      legacy: false,
      locale: 'zh',
      messages: {
        zh: {
          focus: zhFocus,
          tools: zhTools,
          thinking: { close: '关闭' },
          diff: { noDiff: '无详情' },
        },
      },
    }));
    const html = await renderToString(app);
    expect(html).toContain('旧版对话无法加载更多工具详情');
    expect(html).not.toContain('正在加载已保存详情');
  });

  it('keeps the complete file-change presentation toggle bilingual', () => {
    expect(enTools.detail.viewFullDiff).toBe('Diff view');
    expect(enTools.detail.viewFullSourceText).toBe('Source text');
    expect(zhTools.detail.viewFullDiff).toBe('差异视图');
    expect(zhTools.detail.viewFullSourceText).toBe('原始文本');
  });

  it('exposes one responsive search entry through Prompt history on desktop and mobile', () => {
    const toc = source('../src/components/chat/ConversationToc.vue');
    const pane = source('../src/components/chat/ConversationPane.vue');
    const app = source('../src/focus/FocusApp.vue');

    expect(toc).toContain('searchVisible?: boolean;');
    expect(toc).toContain('class="toc-search"');
    expect(toc).toContain('v-if="searchVisible"');
    expect(toc).toContain("t('conversation.searchConversation')");
    expect(toc).toContain("emit('search')");
    expect(pane).toContain(':search-visible="conversationSearchVisible"');
    expect(pane).toContain("@search=\"emit('searchConversation')\"");
    expect(app).toContain('Boolean(client.activeThreadId.value)');
    expect(app).not.toContain('conversationSearchSupported');
    expect(app).toContain('!client.documentReloadRequired.value');
    expect(app).toContain('if (!conversationSearchVisible.value) return;');
    expect(app).toContain('watch(conversationSearchVisible');
    expect(app).toContain('@search-conversation="openConversationSearch"');
  });

  it('navigates an installed search cursor through a DOM-only anchor path', () => {
    const pane = source('../src/components/chat/ConversationPane.vue');
    const app = source('../src/focus/FocusApp.vue');

    expect(pane).toContain('function scrollToRenderedTurn(turnId: string): boolean {');
    expect(pane).toContain('scrollToRenderedTurn,');
    expect(app).toContain('await client.resolveConversationSearchOccurrence(occurrence)');
    expect(app).toContain('conversationPaneRef.value?.scrollToRenderedTurn(anchorId);');
    expect(app).toContain('client.clearConversationSearch();');
  });

  it('retires a search cursor before starting a newer Prompt navigation', () => {
    const app = source('../src/focus/FocusApp.vue');
    const start = app.indexOf('function resolveConversationTocTarget');
    const end = app.indexOf('\n}', start);
    const resolver = app.slice(start, end);

    expect(start).toBeGreaterThanOrEqual(0);
    expect(resolver).toContain("detailSelection.value?.kind === 'conversationSearch'");
    expect(resolver.indexOf('closeDetail();')).toBeLessThan(
      resolver.indexOf('return client.resolveHistoryPromptTarget(turnId);'),
    );
    expect(app).toContain(':resolve-conversation-toc-target="resolveConversationTocTarget"');
  });

  it('routes exact locator kinds before synthetic names and offers every terminal exact detail', async () => {
    const command: ToolCall = {
      id: 'synthetic-command',
      name: 'Edit',
      arg: 'printf detail',
      status: 'ok',
      output: [],
      inspectionLocator: {
        turn_id: 'raw-turn',
        item_id: 'command-item',
        kind: 'commandExecution',
        change_index: null,
      },
    };
    const fileChange: ToolCall = {
      ...command,
      id: 'synthetic-file',
      name: 'exec_command',
      inspectionLocator: {
        turn_id: 'raw-turn',
        item_id: 'file-item',
        kind: 'fileChange',
        change_index: 0,
      },
    };

    expect(resolveToolRenderer(command)).toBe(GenericTool);
    expect(resolveToolRenderer(fileChange)).toBe(EditTool);

    const app = createSSRApp({
      render: () => h(GenericTool, {
        tool: command,
        toolDiffPanel: true,
        toolDetailAvailable: true,
      }),
    });
    app.use(i18n());
    const html = await renderToString(app);
    expect(html).toContain('Load detail');

    const unavailableApp = createSSRApp({
      render: () => h(GenericTool, {
        tool: command,
        toolDiffPanel: true,
        toolDetailAvailable: false,
      }),
    });
    unavailableApp.use(i18n());
    const unavailableHtml = await renderToString(unavailableApp);
    expect(unavailableHtml).not.toContain('Load detail');
    expect(unavailableHtml).toContain('More detail unavailable');

    const focusApp = source('../src/focus/FocusApp.vue');
    expect(focusApp).toContain('const inspectionLocator = inspectableToolDetailLocator(current);');
    expect(focusApp).toContain('void client.readToolDetail(inspectionLocator);');
  });
});
