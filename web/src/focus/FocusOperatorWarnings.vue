<script setup lang="ts">
import { ref } from 'vue';
import { useI18n } from 'vue-i18n';
import Banner from '../components/ui/Banner.vue';
import Button from '../components/ui/Button.vue';
import type { OperatorStatusPresentation } from './operatorWarningPresentation';

defineProps<{
  presentation: OperatorStatusPresentation;
}>();

const { t } = useI18n();
const expanded = ref(false);

function formatObservedAt(seconds: number): string {
  const milliseconds = seconds * 1_000;
  if (!Number.isFinite(milliseconds)) return t('focus.unknown');
  const observedAt = new Date(milliseconds);
  return Number.isNaN(observedAt.getTime())
    ? t('focus.unknown')
    : observedAt.toLocaleString();
}
</script>

<template>
  <section class="operator-warnings">
    <Banner variant="warning">
      <span class="operator-warnings-summary">
        <span>
          {{ t(
            presentation.warningsAreLastKnown
              ? 'focus.operatorWarningsLastKnown'
              : 'focus.operatorWarnings',
            { count: presentation.warningCount },
          ) }}
        </span>
        <Button
          size="sm"
          variant="ghost"
          :aria-expanded="expanded"
          @click="expanded = !expanded"
        >
          {{ t(expanded ? 'focus.operatorWarningsHideDetails' : 'focus.operatorWarningsShowDetails') }}
        </Button>
      </span>
    </Banner>

    <div
      v-if="expanded"
      class="operator-warning-list"
      role="region"
      :aria-label="t('focus.operatorWarningsDetailRegion')"
    >
      <article
        v-for="(warning, index) in presentation.warnings"
        :key="`${index}:${warning.code}:${warning.source}:${warning.firstSeenAt}`"
        class="operator-warning-row"
      >
        <header>
          <strong>{{ warning.message }}</strong>
          <span
            class="operator-warning-severity"
            :class="{ error: warning.severity === 'error' }"
          >
            {{ warning.severity }}
          </span>
          <span
            class="operator-warning-attention"
            :class="warning.attention"
          >
            {{ t(warning.attention === 'advisory'
              ? 'focus.operatorWarningAttentionAdvisory'
              : 'focus.operatorWarningAttentionCorrectness') }}
          </span>
        </header>
        <dl>
          <div>
            <dt>{{ t('focus.operatorWarningCode') }}</dt>
            <dd><code>{{ warning.code }}</code></dd>
          </div>
          <div>
            <dt>{{ t('focus.operatorWarningSource') }}</dt>
            <dd>{{ warning.source }}</dd>
          </div>
          <div>
            <dt>{{ t('focus.operatorWarningOccurrences') }}</dt>
            <dd>{{ warning.occurrences }}</dd>
          </div>
          <div>
            <dt>{{ t('focus.operatorWarningFirstSeen') }}</dt>
            <dd>{{ formatObservedAt(warning.firstSeenAt) }}</dd>
          </div>
          <div>
            <dt>{{ t('focus.operatorWarningLastSeen') }}</dt>
            <dd>{{ formatObservedAt(warning.lastSeenAt) }}</dd>
          </div>
          <div
            v-for="(detail, detailIndex) in warning.details"
            :key="`${detailIndex}:${detail.key}`"
          >
            <dt>{{ detail.key }}</dt>
            <dd><code>{{ detail.value }}</code></dd>
          </div>
        </dl>
        <p v-if="warning.detailsOmitted" class="operator-warning-omitted">
          {{ t('focus.operatorWarningDetailsOmitted') }}
        </p>
      </article>
      <p v-if="presentation.omittedWarningCount > 0" class="operator-warning-omitted">
        {{ t('focus.operatorWarningsOmitted', { count: presentation.omittedWarningCount }) }}
      </p>
    </div>
  </section>
</template>

<style scoped>
.operator-warnings {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.operator-warnings-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  width: 100%;
}
.operator-warning-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: min(42vh, 420px);
  overflow: auto;
  padding: var(--space-2);
  border: 1px solid var(--color-warning-bd);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.operator-warning-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--color-warning-soft);
  overflow-wrap: anywhere;
}
.operator-warning-row header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.operator-warning-row strong {
  color: var(--color-text);
  font-weight: var(--weight-medium);
}
.operator-warning-severity {
  flex: none;
  color: var(--color-warning);
  font-size: var(--text-xs);
  text-transform: uppercase;
}
.operator-warning-severity.error { color: var(--color-danger); }
.operator-warning-attention {
  flex: none;
  color: var(--color-warning);
  font-size: var(--text-xs);
}
.operator-warning-attention.advisory { color: var(--color-text-muted); }
.operator-warning-row dl {
  display: grid;
  gap: var(--space-1);
  margin: 0;
}
.operator-warning-row dl > div {
  display: grid;
  grid-template-columns: minmax(88px, auto) minmax(0, 1fr);
  gap: var(--space-2);
}
.operator-warning-row dt { color: var(--color-text-muted); }
.operator-warning-row dd { margin: 0; color: var(--color-text); }
.operator-warning-row code {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.operator-warning-omitted {
  margin: 0;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
@media (max-width: 640px) {
  .operator-warnings-summary { align-items: flex-start; }
  .operator-warning-row dl > div { grid-template-columns: 1fr; gap: 0; }
}
</style>
