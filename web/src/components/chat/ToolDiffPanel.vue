<!-- apps/kimi-web/src/components/chat/ToolDiffPanel.vue -->
<!-- Right-side detail panel for one semantic tool call. File changes show the
     structured diff; commands preserve their execution facts and saved output. -->
<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { CommandExecutionAction, ToolCall } from '../../types';
import DiffLines from './DiffLines.vue';
import PanelHeader from '../ui/PanelHeader.vue';
import ToolOutputBlock, { hasPresentedToolOutput } from './tool-calls/ToolOutputBlock.vue';

const props = withDefaults(defineProps<{
  tool: ToolCall;
  loading?: boolean;
  error?: boolean;
  unavailableMessage?: string;
  scanStatus?: 'idle' | 'scanning' | 'not_found' | 'found' | 'cancelled' | 'error';
  scannedItems?: number;
}>(), { loading: false, scanStatus: 'idle', scannedItems: 0 });

const emit = defineEmits<{
  close: [];
  cancelToolDetail: [];
}>();

const { t } = useI18n();
const hasDiff = computed(() => (
  (props.tool.diff?.lines.length ?? 0) > 0
  || (props.tool.diff?.omittedChars ?? 0) > 0
));
const hasOutput = computed(() => hasPresentedToolOutput(
  props.tool.output,
  props.tool.outputOmittedChars,
));
const hasInspectableDetail = computed(() => (
  props.tool.status !== 'running'
  && props.tool.inspectionLocator !== undefined
));
const hasCommandFacts = computed(() => {
  const facts = props.tool.commandExecution;
  return Boolean(
    facts?.cwd
    || facts?.source
    || facts?.processId
    || facts?.exitCode !== undefined
    || (facts?.commandActions?.length ?? 0) > 0
  );
});

function commandActionLine(action: CommandExecutionAction): string {
  const kind = action.type?.trim() || 'action';
  const details = [
    action.command ? `$ ${action.command}` : '',
    action.name ? `name: ${action.name}` : '',
    action.path ? `path: ${action.path}` : '',
    action.query ? `query: ${action.query}` : '',
  ].filter(Boolean);
  return details.length > 0 ? `action (${kind}): ${details.join(' · ')}` : `action (${kind})`;
}
</script>

<template>
  <div class="tdp">
    <PanelHeader
      :title="tool.name"
      :subtitle="tool.diff?.path"
      :close-label="t('thinking.close')"
      @close="emit('close')"
    />
    <div class="tdp-body">
      <p
        v-if="unavailableMessage && hasInspectableDetail"
        class="tdp-unavailable"
        role="note"
      >
        {{ unavailableMessage }}
      </p>
      <p v-else-if="loading && scanStatus === 'scanning'" class="tdp-loading" role="status">
        {{ t('tools.detail.scanning', { count: scannedItems }) }}
        <button type="button" class="tdp-cancel" @click="emit('cancelToolDetail')">
          {{ t('tools.detail.cancel') }}
        </button>
      </p>
      <p v-else-if="scanStatus === 'not_found'" class="tdp-error" role="status">
        {{ t('tools.detail.notFound', { count: scannedItems }) }}
      </p>
      <p v-else-if="scanStatus === 'cancelled'" class="tdp-error" role="status">
        {{ t('tools.detail.cancelled') }}
      </p>
      <p v-else-if="error" class="tdp-error" role="alert">
        {{ t('tools.detail.failed') }}
      </p>
      <div v-if="hasCommandFacts" class="tdp-command-facts">
        <div v-if="tool.commandExecution?.cwd">cwd: {{ tool.commandExecution.cwd }}</div>
        <div v-if="tool.commandExecution?.source">source: {{ tool.commandExecution.source }}</div>
        <div v-if="tool.commandExecution?.processId">process: {{ tool.commandExecution.processId }}</div>
        <div v-if="tool.commandExecution?.exitCode !== undefined">
          exit code: {{ tool.commandExecution.exitCode ?? 'pending' }}
        </div>
        <div
          v-for="(action, index) in tool.commandExecution?.commandActions ?? []"
          :key="index"
        >
          {{ commandActionLine(action) }}
        </div>
      </div>
      <DiffLines
        v-if="hasDiff"
        :lines="tool.diff?.lines ?? []"
        :omitted-chars="tool.diff?.omittedChars"
        :omission-line-index="tool.diff?.omissionLineIndex"
      />
      <ToolOutputBlock
        v-else-if="hasOutput"
        :lines="tool.output"
        :omitted-chars="tool.outputOmittedChars"
        :head-line-count="tool.outputHeadLineCount"
      />
      <div v-else-if="!hasCommandFacts && !loading" class="tdp-empty">
        {{ t('diff.noDiff') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.tdp {
  height: 100%;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--bg);
}
.tdp-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  font-family: var(--mono);
}
.tdp-empty {
  padding: 32px 20px;
  color: var(--muted, #9098a0);
  font-size: var(--ui-font-size);
  text-align: center;
}
.tdp-loading {
  margin: 0;
  padding: var(--space-3);
  color: var(--color-text-muted);
  font: var(--text-sm) var(--font-ui);
}
.tdp-cancel {
  margin-inline-start: var(--space-2);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-sm);
  background: var(--color-surface-raised);
  color: var(--color-text);
  cursor: pointer;
  font: inherit;
  padding: 2px 8px;
}
.tdp-unavailable {
  margin: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  color: var(--color-text-muted);
  font: var(--text-sm) / var(--leading-relaxed) var(--font-ui);
}
.tdp-error {
  margin: 0;
  padding: var(--space-3);
  color: var(--color-danger);
  font: var(--text-sm) var(--font-ui);
}
.tdp-command-facts {
  margin: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
</style>
