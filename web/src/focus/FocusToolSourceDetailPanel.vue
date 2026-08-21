<script lang="ts">
import type { FocusThreadToolDetailSource } from './types';

/**
 * Preserve every admitted full-source field when a user copies the detail.
 * This is a typed projection, not a reconstruction of raw app-server JSON.
 */
export function formatFullToolDetailSource(source: FocusThreadToolDetailSource): string {
  return JSON.stringify(source, null, 2);
}
</script>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { useI18n } from 'vue-i18n';
import Button from '../components/ui/Button.vue';
import PanelHeader from '../components/ui/PanelHeader.vue';
import { copyTextToClipboard } from '../lib/clipboard';
import FocusCompleteDiff from './FocusCompleteDiff.vue';
import type {
  FocusCommandExecutionSourceDetail,
  FocusFileChangeSourceDetail,
  FocusThreadToolDetailSource as ToolDetailSource,
} from './types';

const props = defineProps<{
  source: ToolDetailSource;
  changeIndex: number | null;
}>();

const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();
const copied = ref(false);
const copyFailed = ref(false);
const fileChangePresentation = ref<'source' | 'diff'>('source');
const changeElements = new Map<number, HTMLElement>();
let copiedTimer: ReturnType<typeof window.setTimeout> | null = null;

const commandSource = computed<FocusCommandExecutionSourceDetail | null>(() => (
  props.source.type === 'commandExecution' ? props.source : null
));
const fileSource = computed<FocusFileChangeSourceDetail | null>(() => (
  props.source.type === 'fileChange' ? props.source : null
));
const title = computed(() => (
  commandSource.value
    ? t('tools.detail.fullCommandTitle')
    : t('tools.detail.fullFileChangeTitle')
));

function setChangeElement(index: number, element: unknown): void {
  if (element instanceof HTMLElement) changeElements.set(index, element);
  else changeElements.delete(index);
}

function focusInitialFileChange(): void {
  if (typeof window === 'undefined' || !fileSource.value || props.changeIndex === null) return;
  void nextTick().then(() => {
    const element = changeElements.get(props.changeIndex ?? -1);
    if (!element) return;
    element.scrollIntoView({ block: 'nearest' });
    element.focus({ preventScroll: true });
  });
}

async function copyFullSource(): Promise<void> {
  const ok = await copyTextToClipboard(formatFullToolDetailSource(props.source));
  copied.value = ok;
  copyFailed.value = !ok;
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
  copiedTimer = window.setTimeout(() => {
    copied.value = false;
    copyFailed.value = false;
    copiedTimer = null;
  }, 1_500);
}

function toggleFileChangePresentation(): void {
  fileChangePresentation.value = fileChangePresentation.value === 'source'
    ? 'diff'
    : 'source';
}

watch(
  () => [props.source, props.changeIndex] as const,
  focusInitialFileChange,
  { flush: 'post' },
);
watch(() => props.source, () => {
  fileChangePresentation.value = 'source';
});
onMounted(focusInitialFileChange);
onUnmounted(() => {
  if (copiedTimer !== null) window.clearTimeout(copiedTimer);
});
</script>

<template>
  <div class="focus-tool-source-detail">
    <PanelHeader
      :title="title"
      :close-label="t('thinking.close')"
      wrap
      @close="emit('close')"
    >
      <Button
        v-if="fileSource"
        size="sm"
        variant="secondary"
        @click="toggleFileChangePresentation"
      >
        {{ t(fileChangePresentation === 'diff'
          ? 'tools.detail.viewFullSourceText'
          : 'tools.detail.viewFullDiff') }}
      </Button>
      <Button size="sm" variant="secondary" @click="copyFullSource">
        {{ copyFailed
          ? t('tools.detail.copyFailed')
          : copied
            ? t('tools.detail.copySucceeded')
            : t('tools.detail.copyFull') }}
      </Button>
    </PanelHeader>
    <div class="focus-tool-source-body">
      <p class="focus-tool-source-notice">
        {{ t('tools.detail.fullSourceNotice') }}
      </p>

      <template v-if="commandSource">
        <dl class="focus-tool-source-facts">
          <dt>type</dt><dd>{{ commandSource.type }}</dd>
          <dt>id</dt><dd>{{ commandSource.id }}</dd>
          <dt>pluginId</dt><dd>{{ commandSource.pluginId ?? 'null' }}</dd>
          <dt>scriptPath</dt><dd>{{ commandSource.scriptPath ?? 'null' }}</dd>
          <dt>command</dt><dd>{{ commandSource.command }}</dd>
          <dt>cwd</dt><dd>{{ commandSource.cwd }}</dd>
          <dt>processId</dt><dd>{{ commandSource.processId ?? 'null' }}</dd>
          <dt>source</dt><dd>{{ commandSource.source }}</dd>
          <dt>status</dt><dd>{{ commandSource.status }}</dd>
          <dt>exitCode</dt><dd>{{ commandSource.exitCode ?? 'null' }}</dd>
          <dt>durationMs</dt><dd>{{ commandSource.durationMs ?? 'null' }}</dd>
        </dl>

        <section v-if="commandSource.commandActions.length > 0" class="focus-tool-source-section">
          <h2>{{ t('tools.detail.fullCommandActions') }}</h2>
          <pre class="focus-tool-source-text">{{ JSON.stringify(commandSource.commandActions, null, 2) }}</pre>
        </section>
        <section class="focus-tool-source-section">
          <h2>{{ t('tools.detail.fullCommandOutput') }}</h2>
          <pre class="focus-tool-source-text">{{ commandSource.aggregatedOutput ?? t('tools.detail.fullNoCommandOutput') }}</pre>
        </section>
      </template>

      <template v-if="fileSource">
        <dl class="focus-tool-source-facts">
          <dt>type</dt><dd>{{ fileSource.type }}</dd>
          <dt>id</dt><dd>{{ fileSource.id }}</dd>
          <dt>status</dt><dd>{{ fileSource.status }}</dd>
        </dl>
        <section
          v-for="(change, index) in fileSource.changes"
          :key="`${index}:${change.path}`"
          :ref="(element) => setChangeElement(index, element)"
          class="focus-tool-source-change"
          tabindex="-1"
        >
          <div class="focus-tool-source-change-header">
            <strong>{{ t('tools.detail.fullFileChangeNumber', { current: index + 1, total: fileSource.changes.length }) }}</strong>
            <span>{{ change.kind.type }}</span>
          </div>
          <p class="focus-tool-source-path">{{ change.path }}</p>
          <p v-if="change.kind.type === 'update'" class="focus-tool-source-move">
            {{ t('tools.detail.fullMovePath', { path: change.kind.movePath ?? 'null' }) }}
          </p>
          <FocusCompleteDiff
            v-if="fileChangePresentation === 'diff'"
            :source="change.diff"
            :kind="change.kind"
          />
          <pre v-else class="focus-tool-source-text focus-tool-source-raw">{{ change.diff }}</pre>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.focus-tool-source-detail {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
}
.focus-tool-source-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--space-3);
}
.focus-tool-source-notice {
  margin: 0 0 var(--space-3);
  color: var(--color-text-muted);
  font: var(--text-sm) / var(--leading-relaxed) var(--font-ui);
}
.focus-tool-source-facts {
  display: grid;
  grid-template-columns: minmax(7rem, auto) minmax(0, 1fr);
  gap: var(--space-2) var(--space-3);
  margin: 0 0 var(--space-4);
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  font: var(--text-xs) / var(--leading-relaxed) var(--font-mono);
}
.focus-tool-source-facts dt { color: var(--color-text-muted); }
.focus-tool-source-facts dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: var(--color-text);
}
.focus-tool-source-section,
.focus-tool-source-change {
  margin: 0 0 var(--space-4);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface-raised);
  overflow: hidden;
}
.focus-tool-source-section h2 {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-line);
  color: var(--color-text);
  font: var(--weight-semibold) var(--text-xs) var(--font-mono);
}
.focus-tool-source-change:focus-visible {
  outline: none;
  box-shadow: var(--p-focus-ring-strong);
}
.focus-tool-source-change-header {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-line);
  color: var(--color-text);
  font: var(--text-xs) var(--font-mono);
}
.focus-tool-source-change-header span { color: var(--color-text-muted); }
.focus-tool-source-path,
.focus-tool-source-move {
  margin: var(--space-2) var(--space-3);
  overflow-wrap: anywhere;
  color: var(--color-text-muted);
  font: var(--text-xs) / var(--leading-relaxed) var(--font-mono);
}
.focus-tool-source-text {
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  white-space: pre;
  color: var(--color-text);
  background: var(--color-surface-sunken);
  font: var(--text-xs) / var(--leading-relaxed) var(--font-mono);
}
</style>
