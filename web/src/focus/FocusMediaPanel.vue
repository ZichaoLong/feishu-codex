<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import type { ToolMedia } from '../types';
import PanelHeader from '../components/ui/PanelHeader.vue';

const props = defineProps<{ media: ToolMedia }>();
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();

const title = computed(() => props.media.path?.split(/[\\/]/).at(-1) || t('focus.mediaPreview'));
</script>

<template>
  <div class="focus-media-panel">
    <PanelHeader :title="title" :close-label="t('thinking.close')" @close="emit('close')" />
    <div class="focus-media-body">
      <img class="focus-media" :src="media.url" :alt="title" />
    </div>
  </div>
</template>

<style scoped>
.focus-media-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}
.focus-media-body {
  flex: 1;
  min-height: 0;
  display: grid;
  place-items: center;
  overflow: auto;
  padding: var(--space-4);
}
.focus-media {
  display: block;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
</style>
