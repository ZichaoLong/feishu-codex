<script lang="ts">
import type { Component } from 'vue';

let loadedMarkdown: Component | null = null;
let markdownLoad: Promise<Component> | null = null;
let markdownLoadError: unknown = null;

function loadMarkdown(): Promise<Component> {
  if (loadedMarkdown) return Promise.resolve(loadedMarkdown);
  if (markdownLoadError) return Promise.reject(markdownLoadError);
  if (!markdownLoad) {
    markdownLoad = import('./Markdown.vue')
      .then((module) => {
        loadedMarkdown = module.default;
        return loadedMarkdown;
      })
      .catch((error: unknown) => {
        markdownLoadError = error;
        throw error;
      })
      .finally(() => {
        markdownLoad = null;
      });
  }
  return markdownLoad;
}
</script>

<script setup lang="ts">
import { shallowRef, ref } from 'vue';
import { useI18n } from 'vue-i18n';
import type { FilePreviewRequest } from '../../types';

defineProps<{
  text: string;
  openFile?: (target: FilePreviewRequest) => void;
  streaming?: boolean;
}>();

const { t } = useI18n();
const richMarkdown = shallowRef<Component | null>(loadedMarkdown);
const needsMarkdownLoad = loadedMarkdown === null;
const loadState = ref<'loading' | 'ready' | 'error'>(
  loadedMarkdown ? 'ready' : markdownLoadError ? 'error' : 'loading',
);

if (needsMarkdownLoad) {
  void loadMarkdown()
    .then((component) => {
      richMarkdown.value = component;
      loadState.value = 'ready';
    })
    .catch(() => {
      loadState.value = 'error';
    });
}

function reloadPage(): void {
  window.location.reload();
}
</script>

<template>
  <component
    :is="richMarkdown"
    v-if="richMarkdown"
    :text="text"
    :open-file="openFile"
    :streaming="streaming"
  />
  <div v-else class="markdown-source-fallback" :aria-busy="loadState === 'loading'">
    <div
      v-if="loadState === 'error'"
      class="markdown-load-error"
      role="status"
    >
      <span>{{ t('focus.markdownUnavailable') }}</span>
      <button type="button" @click="reloadPage">{{ t('focus.reloadPage') }}</button>
    </div>
    <pre>{{ text }}</pre>
  </div>
</template>

<style scoped>
.markdown-source-fallback {
  color: var(--color-text);
  font: 400 var(--content-font-size)/1.6 var(--font-ui);
  overflow-wrap: anywhere;
}
.markdown-source-fallback pre {
  margin: 0;
  overflow: visible;
  color: inherit;
  font: inherit;
  white-space: pre-wrap;
}
.markdown-load-error {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
.markdown-load-error button {
  border: 0;
  padding: 0;
  background: transparent;
  color: var(--color-accent-hover);
  font: inherit;
  text-decoration: underline;
  text-underline-offset: 2px;
  cursor: pointer;
}
</style>
