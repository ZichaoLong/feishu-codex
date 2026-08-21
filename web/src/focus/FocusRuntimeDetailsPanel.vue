<script setup lang="ts">
import { computed } from 'vue';
import { useI18n } from 'vue-i18n';
import Banner from '../components/ui/Banner.vue';
import PanelHeader from '../components/ui/PanelHeader.vue';
import type { FocusActiveTurnSetting } from './types';
import FocusOperatorWarnings from './FocusOperatorWarnings.vue';
import FocusRuntimeNotices from './FocusRuntimeNotices.vue';
import type { RuntimeDetailsPresentation } from './runtimeDetailsPresentation';

const props = defineProps<{ presentation: RuntimeDetailsPresentation }>();
const emit = defineEmits<{ close: [] }>();
const { t } = useI18n();

const activeTurn = computed(() => props.presentation.activeTurnContext);
const hasRuntimeNotices = computed(() => Boolean(
  props.presentation.runtimeNotices.retry
  || props.presentation.runtimeNotices.notices.length > 0,
));
const hasOperatorDetails = computed(() => (
  props.presentation.operatorStatus.warningCount > 0
  || props.presentation.operatorStatus.degradedWithoutDetails
  || props.presentation.operatorStatusStale
));

const activeTurnInitiator = computed(() => {
  const initiator = activeTurn.value?.initiator;
  if (!initiator) return '';
  if (initiator.kind === 'feishu') {
    return t('focus.activeTurnInitiatorFeishu', { bindingId: initiator.binding_id });
  }
  if (initiator.kind === 'web') return 'Focus Web';
  if (initiator.kind === 'fcodex') return 'fcodex';
  return t('focus.activeTurnInitiatorAutonomousOrUnknown');
});

const activeTurnAudience = computed(() => {
  const audience = activeTurn.value?.feishu_audience ?? [];
  return audience.length > 0
    ? audience.join(', ')
    : t('focus.activeTurnFeishuAudienceNone');
});

function activeTurnSettingValue(setting: FocusActiveTurnSetting): string {
  return setting.source === 'unknown' ? t('focus.unknown') : setting.value;
}

function activeTurnSettingSource(setting: FocusActiveTurnSetting): string {
  if (setting.source === 'unknown') return t('focus.activeTurnSettingEvidenceUnknown');
  return t(setting.source === 'active_reroute'
    ? 'focus.activeTurnSettingSourceExact'
    : 'focus.activeTurnSettingSourceInherited');
}
</script>

<template>
  <div class="runtime-details-panel">
    <PanelHeader
      :title="t('focus.runtimeDetailsTitle')"
      :close-label="t('tasks.closePanel')"
      @close="emit('close')"
    />

    <div class="runtime-details-body">
      <section class="runtime-details-section">
        <h3>{{ t('focus.runtimeDetailsConnection') }}</h3>
        <Banner v-if="presentation.connection === 'disconnected'" variant="warning">
          {{ t('focus.disconnected') }}
        </Banner>
        <dl class="runtime-details-grid">
          <div><dt>{{ t('focus.instance') }}</dt><dd>{{ presentation.instance || t('focus.unknown') }}</dd></div>
          <div><dt>{{ t('focus.connection') }}</dt><dd>{{ presentation.connection }}</dd></div>
          <div><dt>{{ t('focus.writer') }}</dt><dd>{{ presentation.owner.label || t('focus.noWriter') }}</dd></div>
          <div><dt>{{ t('focus.epoch') }}</dt><dd>{{ presentation.runtimeEpoch || t('focus.unknown') }}</dd></div>
          <div><dt>{{ t('focus.revision') }}</dt><dd>{{ presentation.revision }}</dd></div>
        </dl>
        <Banner v-if="presentation.owner.relation === 'other'" variant="info">
          {{ t('focus.writerOther', { owner: presentation.owner.label }) }}
        </Banner>
      </section>

      <section v-if="activeTurn" class="runtime-details-section">
        <h3>{{ t('focus.activeTurnContextTitle') }}</h3>
        <dl class="runtime-details-grid">
          <div><dt>{{ t('focus.runtimeDetailsInitiator') }}</dt><dd>{{ activeTurnInitiator }}</dd></div>
          <div><dt>{{ t('focus.runtimeDetailsFeishuAudience') }}</dt><dd>{{ activeTurnAudience }}</dd></div>
        </dl>
        <div class="runtime-settings">
          <article
            v-for="item in [
              { key: 'model', label: t('focus.activeTurnSettingModel'), setting: activeTurn.settings.model },
              { key: 'reasoning', label: t('focus.activeTurnSettingReasoningEffort'), setting: activeTurn.settings.reasoning_effort },
              { key: 'approval', label: t('focus.activeTurnSettingApprovalPolicy'), setting: activeTurn.settings.approval_policy },
              { key: 'permissions', label: t('focus.activeTurnSettingPermissions'), setting: activeTurn.settings.permissions_profile_id },
            ]"
            :key="item.key"
            class="runtime-setting"
          >
            <span>{{ item.label }}</span>
            <strong>{{ activeTurnSettingValue(item.setting) }}</strong>
            <small>{{ activeTurnSettingSource(item.setting) }}</small>
          </article>
        </div>
      </section>

      <section v-if="hasRuntimeNotices" class="runtime-details-section">
        <h3>{{ t('focus.runtimeDetailsRuntimeMessages') }}</h3>
        <FocusRuntimeNotices :presentation="presentation.runtimeNotices" />
      </section>

      <section v-if="hasOperatorDetails" class="runtime-details-section">
        <h3>{{ t('focus.runtimeDetailsOperatorHealth') }}</h3>
        <Banner v-if="presentation.operatorStatusStale" variant="warning">
          {{ t('focus.operatorStatusUnavailable') }}
        </Banner>
        <Banner v-if="presentation.operatorStatus.degradedWithoutDetails" variant="warning">
          {{ t('focus.operatorStatusDegraded') }}
        </Banner>
        <FocusOperatorWarnings
          v-if="presentation.operatorStatus.warningCount > 0"
          :presentation="presentation.operatorStatus"
        />
      </section>

      <p
        v-if="!activeTurn && !hasRuntimeNotices && !hasOperatorDetails"
        class="runtime-details-empty"
      >
        {{ t('focus.runtimeDetailsNoAdditionalEvents') }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.runtime-details-panel {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-bg);
}
.runtime-details-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: var(--space-3);
}
.runtime-details-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-3) 0 var(--space-4);
  border-bottom: 1px solid var(--color-line);
}
.runtime-details-section:first-child { padding-top: 0; }
.runtime-details-section:last-child { border-bottom: 0; }
.runtime-details-section h3 {
  margin: 0;
  color: var(--color-text);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
}
.runtime-details-grid {
  display: grid;
  gap: var(--space-2);
  margin: 0;
}
.runtime-details-grid > div {
  display: grid;
  grid-template-columns: minmax(88px, auto) minmax(0, 1fr);
  gap: var(--space-3);
}
.runtime-details-grid dt { color: var(--color-text-muted); }
.runtime-details-grid dd {
  min-width: 0;
  margin: 0;
  color: var(--color-text);
  overflow-wrap: anywhere;
}
.runtime-settings {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-2);
}
.runtime-setting {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-3);
  border: 1px solid var(--color-line);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}
.runtime-setting > span,
.runtime-setting > small {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
.runtime-setting > strong {
  color: var(--color-text);
  overflow-wrap: anywhere;
}
.runtime-details-empty {
  margin: 0;
  padding: var(--space-4) 0;
  color: var(--color-text-muted);
  text-align: center;
}
@media (max-width: 640px) {
  .runtime-settings { grid-template-columns: minmax(0, 1fr); }
  .runtime-details-grid > div { grid-template-columns: minmax(0, 1fr); gap: 0; }
}
</style>
