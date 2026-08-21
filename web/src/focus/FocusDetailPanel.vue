<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import AgentDetailPanel from '../components/chat/AgentDetailPanel.vue';
import ThinkingPanel from '../components/chat/ThinkingPanel.vue';
import ToolDiffPanel from '../components/chat/ToolDiffPanel.vue';
import Button from '../components/ui/Button.vue';
import type { AgentMember, ToolCall, ToolMedia } from '../types';
import FocusConversationSearchPanel from './FocusConversationSearchPanel.vue';
import FocusMediaPanel from './FocusMediaPanel.vue';
import FocusRuntimeDetailsPanel from './FocusRuntimeDetailsPanel.vue';
import FocusToolSourceDetailPanel from './FocusToolSourceDetailPanel.vue';
import type { RuntimeDetailsPresentation } from './runtimeDetailsPresentation';
import type {
  FocusConversationSearchOccurrence,
  FocusThreadConversationSearchPage,
  FocusThreadToolDetailPayload,
  FocusThreadInspectionUnavailableReason,
} from './types';

const props = defineProps<{
  target: 'runtimeDetails' | 'thinking' | 'toolDiff' | 'conversationSearch' | 'media' | 'agent';
  runtimeDetailsPresentation: RuntimeDetailsPresentation;
  thinkingText: string;
  tool: ToolCall | null;
  toolDetail: FocusThreadToolDetailPayload | null;
  toolDetailChangeIndex: number | null;
  toolDetailLoading: boolean;
  toolDetailError: boolean;
  toolDetailScanStatus: 'idle' | 'scanning' | 'not_found' | 'found' | 'cancelled' | 'error';
  toolDetailScannedItems: number;
  toolDetailUnavailableReason: FocusThreadInspectionUnavailableReason | null;
  conversationSearchUnavailableReason: FocusThreadInspectionUnavailableReason | null;
  conversationSearchLoading: boolean;
  conversationSearchError: boolean;
  conversationSearchPage: FocusThreadConversationSearchPage | null;
  mediaTarget: ToolMedia | null;
  agentMember: AgentMember | null;
}>();

const { t } = useI18n();
const toolDetailUnavailableMessage = computed(() => {
  const reason = props.toolDetailUnavailableReason;
  if (reason === null) return '';
  const keys: Record<FocusThreadInspectionUnavailableReason, string> = {
    build_unsupported: 'tools.detail.unavailableBuild',
    document_unavailable: 'tools.detail.unavailableDocument',
    legacy_history: 'tools.detail.unavailableLegacy',
    no_active_thread: 'tools.detail.unavailableNoThread',
    runtime_unsupported: 'tools.detail.unavailableRuntime',
    thread_not_materialized: 'tools.detail.unavailableMaterializing',
    unknown_history: 'tools.detail.unavailableUnknown',
  };
  return t(keys[reason]);
});
const fullToolDetailSource = computed(() => (
  props.toolDetail?.view === 'full' ? props.toolDetail.source : null
));
const previewReady = computed(() => props.toolDetail?.view === 'preview');

const emit = defineEmits<{
  close: [];
  cancelToolDetail: [];
  loadFullToolDetail: [];
  searchConversation: [query: string];
  nextConversationSearchPage: [];
  selectConversationSearchOccurrence: [occurrence: FocusConversationSearchOccurrence];
}>();
</script>

<template>
  <FocusRuntimeDetailsPanel
    v-if="target === 'runtimeDetails'"
    :presentation="runtimeDetailsPresentation"
    @close="emit('close')"
  />
  <ThinkingPanel
    v-else-if="target === 'thinking'"
    :text="thinkingText"
    @close="emit('close')"
  />
  <FocusToolSourceDetailPanel
    v-else-if="target === 'toolDiff' && fullToolDetailSource"
    :source="fullToolDetailSource"
    :change-index="toolDetailChangeIndex"
    @close="emit('close')"
  />
  <div v-else-if="target === 'toolDiff' && tool" class="focus-tool-detail-preview">
    <ToolDiffPanel
      :tool="tool"
      :loading="toolDetailLoading"
      :error="toolDetailError"
      :scan-status="toolDetailScanStatus"
      :scanned-items="toolDetailScannedItems"
      :unavailable-message="toolDetailUnavailableMessage"
      @close="emit('close')"
      @cancel-tool-detail="emit('cancelToolDetail')"
    />
    <div v-if="previewReady" class="focus-tool-detail-preview-actions">
      <p>{{ t('tools.detail.preview') }}</p>
      <Button
        size="sm"
        variant="secondary"
        :loading="toolDetailLoading"
        @click="emit('loadFullToolDetail')"
      >
        {{ t(toolDetailLoading ? 'tools.detail.fullLoading' : 'tools.detail.viewFull') }}
      </Button>
    </div>
  </div>
  <FocusConversationSearchPanel
    v-else-if="target === 'conversationSearch'"
    :unavailable-reason="conversationSearchUnavailableReason"
    :loading="conversationSearchLoading"
    :error="conversationSearchError"
    :page="conversationSearchPage"
    @close="emit('close')"
    @search="emit('searchConversation', $event)"
    @next="emit('nextConversationSearchPage')"
    @select="emit('selectConversationSearchOccurrence', $event)"
  />
  <FocusMediaPanel
    v-else-if="target === 'media' && mediaTarget"
    :media="mediaTarget"
    @close="emit('close')"
  />
  <AgentDetailPanel
    v-else-if="target === 'agent' && agentMember"
    :member="agentMember"
    @close="emit('close')"
  />
</template>

<style scoped>
.focus-tool-detail-preview {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.focus-tool-detail-preview :deep(.tdp) {
  flex: 1;
  min-height: 0;
  height: auto;
}
.focus-tool-detail-preview-actions {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3) var(--space-3);
  border-top: 1px solid var(--color-line);
  background: var(--color-surface);
}
.focus-tool-detail-preview-actions p {
  min-width: 0;
  margin: 0;
  color: var(--color-text-muted);
  font: var(--text-xs) / var(--leading-relaxed) var(--font-ui);
}
</style>
