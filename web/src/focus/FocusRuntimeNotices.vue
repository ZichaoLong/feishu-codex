<script setup lang="ts">
import { useI18n } from 'vue-i18n';
import Banner from '../components/ui/Banner.vue';
import type { RuntimeNoticePresentation } from './client-state/runtime-notices';

defineProps<{ presentation: RuntimeNoticePresentation }>();

const { t } = useI18n();
</script>

<template>
  <section
    v-if="presentation.retry || presentation.notices.length > 0"
    class="runtime-notices"
    :aria-label="t('focus.runtimeNoticesRegion')"
  >
    <Banner v-if="presentation.retry" variant="warning">
      <span class="runtime-notice-copy">
        <strong>{{ t('focus.runtimeRetrying') }}</strong>
        <span>{{ presentation.retry.message }}</span>
        <span
          v-if="presentation.retry.additionalDetails"
          class="runtime-notice-details"
        >{{ presentation.retry.additionalDetails }}</span>
      </span>
    </Banner>
    <Banner
      v-for="notice in presentation.notices"
      :key="notice.id"
      :variant="notice.method === 'error' ? 'danger' : 'warning'"
    >
      <span class="runtime-notice-copy">
        <strong>{{ t(notice.method === 'error'
          ? 'focus.runtimeError'
          : 'focus.runtimeWarning') }}</strong>
        <span>{{ notice.message }}</span>
        <span
          v-if="notice.additionalDetails"
          class="runtime-notice-details"
        >{{ notice.additionalDetails }}</span>
      </span>
    </Banner>
  </section>
</template>

<style scoped>
.runtime-notices {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.runtime-notice-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: var(--space-1);
  overflow-wrap: anywhere;
}
.runtime-notice-copy strong {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}
.runtime-notice-details {
  white-space: pre-wrap;
  color: var(--color-text-secondary);
}
</style>
