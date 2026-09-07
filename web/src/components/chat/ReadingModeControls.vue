<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import Button from '../ui/Button.vue';
import Icon from '../ui/Icon.vue';
import IconButton from '../ui/IconButton.vue';

withDefaults(defineProps<{
  mobile?: boolean;
  sessionTitle: string;
  switcherOpen?: boolean;
}>(), {
  mobile: false,
  switcherOpen: false,
});

const emit = defineEmits<{
  exit: [];
  switchSession: [];
}>();

const { t } = useI18n();
</script>

<template>
  <div class="reading-mode-controls" :class="{ 'is-mobile': mobile }">
    <IconButton
      class="reading-mode-exit"
      size="sm"
      :label="t('focus.exitReadingMode')"
      :aria-pressed="true"
      data-reading-mode-toggle
      @click="emit('exit')"
    >
      <Icon name="close" size="sm" />
    </IconButton>
    <Button
      class="reading-session-switch"
      size="sm"
      variant="secondary"
      :aria-label="t('mobile.openSwitcher')"
      aria-haspopup="dialog"
      :aria-expanded="switcherOpen"
      @click="emit('switchSession')"
    >
      <span class="reading-session-title">{{ sessionTitle }}</span>
      <Icon name="chevron-down" size="sm" />
    </Button>
  </div>
</template>

<style scoped>
.reading-mode-controls {
  z-index: var(--z-sticky);
  height: 48px;
  flex: none;
  display: flex;
  flex-direction: row-reverse;
  align-items: center;
  gap: var(--space-2);
  padding: 0 var(--space-4);
}
.reading-mode-controls.is-mobile {
  height: calc(50px + var(--safe-top));
  flex-direction: row;
  padding: var(--safe-top) max(12px, var(--safe-right)) 0 max(12px, var(--safe-left));
}
.reading-mode-exit {
  width: 30px;
  height: 30px;
  background: var(--color-surface-raised);
  border-color: var(--color-line-strong);
  box-shadow: var(--shadow-xs);
}
.reading-session-switch {
  max-width: min(52vw, 240px);
}
.reading-session-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.reading-session-switch :deep(.ui-button__content) {
  min-width: 0;
}
</style>
